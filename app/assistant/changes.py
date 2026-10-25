"""Lots de modifications faites via l'IA : journal avant / après, annulation, aperçu, confirmation
(docs/spec-mcp.md §4).

- Une écriture appelle les **services existants** (mêmes règles, même audit que le web). Les
  tables d'état (catégories, règles, catégorisations, file à valider, liens, baux, loyers,
  charges) sont photographiées avant et après : la différence, ligne par ligne, forme le lot,
  chiffré en base (il peut contenir des noms).
- **Annulation** : chaque ligne reprend sa valeur d'avant, à condition de ne pas avoir changé
  depuis (sinon refus motivé). Les échéances et incidents de loyer, recalculés par le moteur,
  sont restaurés sans ce contrôle, puis le moteur repasse.
- **Aperçu** : l'opération est d'abord jouée sur une copie jetable de la base (tmpfs) ; au-delà
  de `MASS_THRESHOLD` opérations touchées, elle n'est appliquée qu'avec le code de confirmation
  rendu par l'aperçu (10 min, usage unique, lié au jeton, à l'outil et aux paramètres).

Limite connue : une tâche planifiée qui écrirait pendant les quelques millisecondes d'une
écriture de l'IA verrait ses lignes comptées dans le lot ; le contrôle d'annulation les protège.
"""

import base64
import hashlib
import json
import secrets
import sqlite3
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, Engine, insert, select, update

from app.audit import audit
from app.db import Base, inserted_id, make_engine, write_transaction
from app.models import AiChangeSet, AiConfirmation
from app.rental.engine import run_rental
from app.timeutil import ensure_utc

TRACKED = frozenset(
    {
        "category",
        "rule",
        "categorization",
        "review_item",
        "duplicate_link",
        "transfer_link",
        "property",
        "owner_share",
        "lease",
        "party",
        "rent_due",
        "rent_allocation",
        "overpayment",
        "rent_incident",
        "property_expense_rule",
        "property_expense",
    }
)
DERIVED = frozenset({"rent_due", "rent_incident"})  # recalculées par le moteur de loyers
RENTAL = frozenset(
    {
        "property",
        "owner_share",
        "lease",
        "party",
        "rent_due",
        "rent_allocation",
        "overpayment",
        "rent_incident",
        "property_expense_rule",
        "property_expense",
    }
)
ORDER = [table.name for table in Base.metadata.sorted_tables if table.name in TRACKED]
MASS_THRESHOLD = 20
CONFIRMATION_LIFETIME = timedelta(minutes=10)
TABLE_NAMES = {
    "category": "catégorie",
    "rule": "règle",
    "categorization": "catégorisation",
    "review_item": "élément à valider",
    "duplicate_link": "doublon",
    "transfer_link": "virement interne",
    "property": "bien",
    "owner_share": "quote-part",
    "lease": "bail",
    "party": "payeur",
    "rent_due": "échéance",
    "rent_allocation": "affectation de loyer",
    "overpayment": "trop-perçu",
    "rent_incident": "incident de loyer",
    "property_expense_rule": "règle de charge",
    "property_expense": "charge du bien",
}

Row = dict[str, Any]
Snapshot = dict[str, dict[int, Row]]


class ChangeError(ValueError):
    """Annulation ou confirmation impossible (message affichable)."""


@dataclass(frozen=True)
class RowChange:
    table: str
    id: int
    before: Row | None
    after: Row | None


@dataclass(frozen=True)
class ChangeSetRow:
    id: int
    tool: str
    summary: str
    row_count: int
    created_at: datetime
    undone_at: datetime | None


# --- Photographies ----------------------------------------------------------------------------


def _encode(value: Any) -> Any:
    return {"$b": base64.b64encode(value).decode()} if isinstance(value, bytes) else value


def _decode(value: Any) -> Any:
    if isinstance(value, dict) and set(value) == {"$b"}:
        return base64.b64decode(value["$b"])
    return value


def _rows(
    connection: Connection, table: str, where: str = "", params: tuple[Any, ...] = ()
) -> dict[int, Row]:
    cursor = connection.exec_driver_sql(f"SELECT * FROM {table}{where}", params)  # noqa: S608 (noms de tables fixes)
    columns = [column[0] for column in cursor.cursor.description]
    return {
        int(row[columns.index("id")]): {c: _encode(v) for c, v in zip(columns, row, strict=True)}
        for row in cursor.fetchall()
    }


def snapshot(engine: Engine) -> Snapshot:
    with engine.connect() as connection, connection.begin():
        return {table: _rows(connection, table) for table in ORDER}


def diff(before: Snapshot, after: Snapshot) -> list[RowChange]:
    changes = []
    for table in ORDER:
        old, new = before[table], after[table]
        for row_id in sorted(old.keys() | new.keys()):
            if old.get(row_id) != new.get(row_id):
                changes.append(RowChange(table, row_id, old.get(row_id), new.get(row_id)))
    return changes


@dataclass(frozen=True)
class Outcome[T]:
    result: T
    changes: list[RowChange]
    before: Snapshot
    after: Snapshot


def tracked[T](engine: Engine, operation: Callable[[Engine], T]) -> Outcome[T]:
    before = snapshot(engine)
    result = operation(engine)
    after = snapshot(engine)
    return Outcome(result, diff(before, after), before, after)


def simulate[T](engine: Engine, operation: Callable[[Engine], T]) -> Outcome[T]:
    """Joue l'opération sur une copie jetable de la base (tmpfs) : rien n'est écrit pour de vrai."""
    with tempfile.TemporaryDirectory(prefix="finances-apercu-") as directory:
        path = Path(directory) / "copie.db"
        source = engine.raw_connection()
        try:
            target = sqlite3.connect(path)
            try:
                source.driver_connection.backup(target)  # type: ignore[union-attr]
            finally:
                target.close()
        finally:
            source.close()
        copy = make_engine(path)
        try:
            return tracked(copy, operation)
        finally:
            copy.dispose()


# --- Lots -------------------------------------------------------------------------------------


def _serialize(changes: list[RowChange]) -> str:
    return json.dumps(
        [[c.table, c.id, c.before, c.after] for c in changes], ensure_ascii=False, sort_keys=True
    )


def _check(table: str, row: Row | None) -> None:
    """Les noms de table et de colonne entrent dans le SQL de l'annulation : seules les tables
    suivies et leurs vraies colonnes sont admises (défense en profondeur ; le journal est déjà
    chiffré et authentifié)."""
    if table not in TRACKED:
        raise ChangeError("Modification illisible : table inattendue.")
    if row is not None and not set(row) <= set(Base.metadata.tables[table].columns.keys()):
        raise ChangeError("Modification illisible : colonne inattendue.")


def _deserialize(text: str) -> list[RowChange]:
    changes = []
    for table, row_id, before, after in json.loads(text):
        _check(table, before)
        _check(table, after)
        changes.append(RowChange(table, int(row_id), before, after))
    return changes


def save(
    engine: Engine,
    changes: list[RowChange],
    *,
    token_id: int | None,
    actor_user_id: int | None,
    tool: str,
    summary: str,
    now: datetime,
) -> int | None:
    if not changes:
        return None
    with write_transaction(engine) as connection:
        result = connection.execute(
            insert(AiChangeSet).values(
                token_id=token_id,
                tool=tool,
                summary=summary[:255],
                changes_enc=_serialize(changes),
                row_count=len(changes),
                created_at=now,
            )
        )
        change_id = inserted_id(result)
        audit(
            connection,
            "ai.change_set",
            ("ai_change_set", change_id),
            now=now,
            actor_user_id=actor_user_id,
            details={"outil": tool, "lignes": len(changes)},
        )
    return change_id


def change_sets(engine: Engine, limit: int = 50) -> list[ChangeSetRow]:
    with engine.connect() as connection:
        rows = connection.execute(
            select(
                AiChangeSet.id,
                AiChangeSet.tool,
                AiChangeSet.summary,
                AiChangeSet.row_count,
                AiChangeSet.created_at,
                AiChangeSet.undone_at,
            )
            .order_by(AiChangeSet.created_at.desc(), AiChangeSet.id.desc())
            .limit(limit)
        ).all()
    return [ChangeSetRow(*row) for row in rows]


def changes_of(engine: Engine, change_id: int) -> list[RowChange]:
    with engine.connect() as connection:
        text = connection.scalar(select(AiChangeSet.changes_enc).where(AiChangeSet.id == change_id))
    if text is None:
        raise ChangeError("Modification introuvable.")
    return _deserialize(text)


def _restore(connection: Connection, change: RowChange) -> None:
    table = change.table
    if change.before is None:
        connection.exec_driver_sql(f"DELETE FROM {table} WHERE id = ?", (change.id,))  # noqa: S608
        return
    values = {column: _decode(value) for column, value in change.before.items()}
    columns = list(values)
    exists = connection.exec_driver_sql(f"SELECT 1 FROM {table} WHERE id = ?", (change.id,)).first()  # noqa: S608
    if exists:
        assignments = ", ".join(f"{c} = ?" for c in columns if c != "id")
        connection.exec_driver_sql(
            f"UPDATE {table} SET {assignments} WHERE id = ?",  # noqa: S608
            (*(values[c] for c in columns if c != "id"), change.id),
        )
    else:
        verb = "INSERT OR IGNORE" if table in DERIVED else "INSERT"
        marks = ", ".join("?" for _ in columns)
        connection.exec_driver_sql(
            f"{verb} INTO {table} ({', '.join(columns)}) VALUES ({marks})",
            tuple(values[c] for c in columns),
        )


def undo(
    engine: Engine, change_id: int, *, actor_user_id: int | None, now: datetime
) -> list[RowChange]:
    """Rétablit l'état d'avant le lot ; refuse si une ligne concernée a changé depuis."""
    with write_transaction(engine) as connection:
        row = connection.execute(
            select(AiChangeSet.changes_enc, AiChangeSet.undone_at).where(
                AiChangeSet.id == change_id
            )
        ).first()
        if row is None:
            raise ChangeError("Modification introuvable.")
        if row.undone_at is not None:
            raise ChangeError("Modification déjà annulée.")
        changes = _deserialize(row.changes_enc)
        conflicts = sorted(
            {
                TABLE_NAMES[c.table]
                for c in changes
                if c.table not in DERIVED
                and _rows(connection, c.table, " WHERE id = ?", (c.id,)).get(c.id) != c.after
            }
        )
        if conflicts:
            raise ChangeError(
                "Annulation impossible : modifié depuis (" + ", ".join(conflicts) + ")."
            )
        # Contrôle des clés étrangères à la validation : l'ordre des restaurations est libre.
        connection.exec_driver_sql("PRAGMA defer_foreign_keys = ON")
        for change in changes:
            _restore(connection, change)
        connection.execute(
            update(AiChangeSet).where(AiChangeSet.id == change_id).values(undone_at=now)
        )
        audit(
            connection,
            "ai.change_undone",
            ("ai_change_set", change_id),
            now=now,
            actor_user_id=actor_user_id,
            details={"lignes": len(changes)},
        )
    if any(c.table in RENTAL for c in changes):
        run_rental(engine, now=now)
    return changes


# --- Confirmation des écritures de masse ------------------------------------------------------


def _params_hash(tool: str, params: dict[str, Any]) -> str:
    canonical = json.dumps(
        {k: v for k, v in params.items() if k != "confirmation"}, sort_keys=True, default=str
    )
    return hashlib.sha256(f"{tool}\x00{canonical}".encode()).hexdigest()


def _code_hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def issue_confirmation(
    engine: Engine, *, token_id: int, tool: str, params: dict[str, Any], now: datetime
) -> str:
    code = "c_" + base64.b32encode(secrets.token_bytes(16)).decode().rstrip("=").lower()
    with write_transaction(engine) as connection:
        connection.execute(
            insert(AiConfirmation).values(
                token_id=token_id,
                tool=tool,
                params_hash=_params_hash(tool, params),
                code_hash=_code_hash(code),
                created_at=now,
                expires_at=now + CONFIRMATION_LIFETIME,
            )
        )
    return code


def consume_confirmation(
    engine: Engine, *, token_id: int, tool: str, params: dict[str, Any], code: str, now: datetime
) -> None:
    with write_transaction(engine) as connection:
        row = connection.execute(
            select(AiConfirmation).where(AiConfirmation.code_hash == _code_hash(code.strip()))
        ).first()
        valid = (
            row is not None
            and row.used_at is None
            and ensure_utc(row.expires_at) > now
            and row.token_id == token_id
            and row.tool == tool
            and row.params_hash == _params_hash(tool, params)
        )
        if not valid or row is None:
            raise ChangeError(
                "Code de confirmation invalide, expiré, déjà utilisé ou pour d'autres paramètres :"
                " refaire l'aperçu."
            )
        connection.execute(
            update(AiConfirmation).where(AiConfirmation.id == row.id).values(used_at=now)
        )
