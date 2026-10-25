"""Noms à abréger et émetteurs à garder en clair (page Sécurité), chiffrés en base.

Les noms des payeurs des baux s'y ajoutent automatiquement.
Rien de tout cela n'est jamais écrit dans le dépôt (inv. 5).
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import Engine, delete, insert, select

from app.assistant.pseudonym import Pseudonymizer, normalize
from app.audit import audit
from app.crypto import blind_index
from app.db import inserted_id, write_transaction
from app.models import AiTerm, LedgerTransaction, Party

TermKind = Literal["personne", "emetteur"]
MAX_TERM = 64


class TermError(ValueError):
    """Saisie refusée."""


@dataclass(frozen=True)
class Term:
    id: int
    kind: str
    value: str


def _hash(kind: str, value: str) -> str:
    return blind_index(f"{kind}:{normalize(value)}", context="ai_term.value")


def add_term(
    engine: Engine, kind: TermKind, value: str, *, actor_user_id: int, now: datetime
) -> int:
    cleaned = " ".join(value.split())
    if not cleaned or len(cleaned) > MAX_TERM:
        raise TermError(f"Entre 1 et {MAX_TERM} caractères.")
    with write_transaction(engine) as connection:
        if connection.scalar(select(AiTerm.id).where(AiTerm.value_hash == _hash(kind, cleaned))):
            raise TermError("Déjà dans la liste.")
        result = connection.execute(
            insert(AiTerm).values(
                kind=kind, value_enc=cleaned, value_hash=_hash(kind, cleaned), created_at=now
            )
        )
        term_id = inserted_id(result)
        audit(
            connection,
            "ai.term_added",
            ("ai_term", term_id),
            now=now,
            actor_user_id=actor_user_id,
            details={"type": kind},
        )
    return term_id


def remove_term(engine: Engine, term_id: int, *, actor_user_id: int, now: datetime) -> None:
    with write_transaction(engine) as connection:
        if connection.execute(delete(AiTerm).where(AiTerm.id == term_id)).rowcount != 1:
            raise TermError("Élément introuvable.")
        audit(
            connection,
            "ai.term_removed",
            ("ai_term", term_id),
            now=now,
            actor_user_id=actor_user_id,
        )


def terms(engine: Engine) -> list[Term]:
    with engine.connect() as connection:
        rows = connection.execute(select(AiTerm.id, AiTerm.kind, AiTerm.value_enc)).all()
    return sorted((Term(*row) for row in rows), key=lambda t: (t.kind, normalize(t.value)))


def load_pseudonymizer(engine: Engine) -> Pseudonymizer:
    persons: list[str] = []
    emitters: list[str] = []
    with engine.connect() as connection:
        for kind, value in connection.execute(select(AiTerm.kind, AiTerm.value_enc)):
            (persons if kind == "personne" else emitters).append(value)
        # Noms des payeurs seulement : leurs motifs contiennent souvent des mots courants
        # (« LOYER STUDIO ») qui, pris pour des noms, abrégeraient tout.
        persons.extend(connection.scalars(select(Party.name_enc)))
    return Pseudonymizer(persons=persons, emitters=emitters)


def preview(engine: Engine, limit: int = 20) -> list[tuple[str, str]]:
    """(libellé d'origine, libellé envoyé à l'IA) des dernières opérations : pour vérifier."""
    masker = load_pseudonymizer(engine)
    with engine.connect() as connection:
        labels = connection.scalars(
            select(LedgerTransaction.label_raw)
            .order_by(LedgerTransaction.booking_date.desc(), LedgerTransaction.id.desc())
            .limit(limit)
        ).all()
    return [(label, masker.label(label)) for label in labels]
