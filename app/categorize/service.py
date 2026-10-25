"""Catégorisation par règles et manuelle (ANA-01, ANA-03, ANA-04).

- Les règles sont évaluées par priorité croissante (puis par identifiant) ; la première qui
  correspond l'emporte.
- Une catégorisation **manuelle** n'est jamais remplacée par une règle ; une règle remplace une
  catégorisation IA ; une règle désactivée retire ses catégorisations.
- Historique par remplacement : une ligne n'est jamais effacée, elle est marquée remplacée.
- Toute modification de règle et toute catégorisation manuelle sont auditées (NF-SEC-06).
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, Engine, insert, select, update

from app.audit import audit
from app.categorize.rules import RuleConditions, matches
from app.db import inserted_id, write_transaction
from app.ledger.views import is_effective
from app.models import Categorization, Category, LedgerTransaction, Rule
from app.models.categorize import CategorizationSource


class CategorizationError(ValueError):
    """Catégorisation incohérente (somme des lignes, transaction inconnue…)."""


@dataclass(frozen=True)
class RuleSuggestion:
    name: str
    conditions: RuleConditions


def _supersede(connection: Connection, line_ids: list[int], now: datetime) -> None:
    if line_ids:
        connection.execute(
            update(Categorization).where(Categorization.id.in_(line_ids)).values(superseded_at=now)
        )


def _apply(connection: Connection, now: datetime, transaction_ids: list[int] | None = None) -> int:
    rules = [
        (rule.id, rule.category_id, RuleConditions.model_validate(rule.conditions))
        for rule in connection.execute(
            select(Rule).where(Rule.is_active.is_(True)).order_by(Rule.priority, Rule.id)
        )
    ]
    needs_counterparty = any(c.counterparty_contains for _, _, c in rules)
    query = select(LedgerTransaction).where(is_effective())
    if transaction_ids is not None:
        query = query.where(LedgerTransaction.id.in_(transaction_ids))
    active: dict[int, list[Any]] = {}
    for line in connection.execute(
        select(Categorization).where(Categorization.superseded_at.is_(None))
    ):
        active.setdefault(line.transaction_id, []).append(line)

    changed = 0
    for tx in connection.execute(query):
        lines = active.get(tx.id, [])
        if any(line.source is CategorizationSource.MANUAL for line in lines):
            continue
        counterparty = tx.counterparty_name_enc if needs_counterparty else None
        winner = next(
            (
                (rule_id, category_id)
                for rule_id, category_id, conditions in rules
                if matches(
                    conditions,
                    label=tx.label_clean,
                    amount=tx.amount_cents,
                    account_id=tx.account_id,
                    counterparty=counterparty,
                )
            ),
            None,
        )
        current_rule = [line for line in lines if line.source is CategorizationSource.RULE]
        if winner is None:
            if current_rule:  # la règle ne correspond plus (modifiée ou désactivée)
                _supersede(connection, [line.id for line in current_rule], now)
                changed += 1
            continue
        rule_id, category_id = winner
        if [(line.rule_id, line.category_id) for line in lines] == [(rule_id, category_id)]:
            continue
        _supersede(connection, [line.id for line in lines], now)
        connection.execute(
            insert(Categorization).values(
                transaction_id=tx.id,
                category_id=category_id,
                amount_cents=tx.amount_cents,
                source=CategorizationSource.RULE,
                rule_id=rule_id,
                created_at=now,
            )
        )
        changed += 1
    return changed


def apply_rules(engine: Engine, *, now: datetime, transaction_ids: list[int] | None = None) -> int:
    """Applique les règles actives ; renvoie le nombre de transactions modifiées."""
    with write_transaction(engine) as connection:
        return _apply(connection, now, transaction_ids)


SUGGESTED_RULE_PRIORITY = 100  # règles créées d'un clic : après les règles écrites à la main


def create_rule(
    engine: Engine,
    *,
    name: str,
    priority: int,
    conditions: dict[str, Any],
    category_id: int,
    actor_user_id: int | None,
    now: datetime,
) -> int:
    validated = RuleConditions.model_validate(conditions)
    with write_transaction(engine) as connection:
        result = connection.execute(
            insert(Rule).values(
                name=name,
                priority=priority,
                is_active=True,
                conditions=validated.model_dump(exclude_none=True),
                category_id=category_id,
                created_at=now,
                updated_at=now,
            )
        )
        rule_id = inserted_id(result)
        audit(
            connection,
            "rule.created",
            ("rule", rule_id),
            now=now,
            actor_user_id=actor_user_id,
            details={"name": name, "priority": priority, "category_id": category_id},
        )
        _apply(connection, now)
    return rule_id


def update_rule(
    engine: Engine,
    rule_id: int,
    *,
    actor_user_id: int | None,
    now: datetime,
    name: str | None = None,
    priority: int | None = None,
    conditions: dict[str, Any] | None = None,
    category_id: int | None = None,
) -> None:
    values: dict[str, Any] = {"updated_at": now}
    if name is not None:
        values["name"] = name
    if priority is not None:
        values["priority"] = priority
    if conditions is not None:
        values["conditions"] = RuleConditions.model_validate(conditions).model_dump(
            exclude_none=True
        )
    if category_id is not None:
        values["category_id"] = category_id
    with write_transaction(engine) as connection:
        connection.execute(update(Rule).where(Rule.id == rule_id).values(**values))
        changes = {key: value for key, value in values.items() if key != "updated_at"}
        audit(
            connection,
            "rule.updated",
            ("rule", rule_id),
            now=now,
            actor_user_id=actor_user_id,
            details=changes,
        )
        _apply(connection, now)


def deactivate_rule(
    engine: Engine, rule_id: int, *, actor_user_id: int | None, now: datetime
) -> None:
    with write_transaction(engine) as connection:
        connection.execute(
            update(Rule).where(Rule.id == rule_id).values(is_active=False, updated_at=now)
        )
        audit(
            connection, "rule.deactivated", ("rule", rule_id), now=now, actor_user_id=actor_user_id
        )
        _apply(connection, now)


def categorize_manually(
    engine: Engine,
    transaction_id: int,
    lines: list[tuple[int, int]],
    *,
    actor_user_id: int | None,
    now: datetime,
) -> None:
    """Catégorise (ou ventile) une transaction : `lines` = [(catégorie, montant en centimes)]."""
    if not lines:
        raise CategorizationError("au moins une ligne de catégorisation")
    with write_transaction(engine) as connection:
        amount = connection.execute(
            select(LedgerTransaction.amount_cents).where(LedgerTransaction.id == transaction_id)
        ).scalar_one_or_none()
        if amount is None:
            raise CategorizationError(f"transaction {transaction_id} inconnue")
        wanted = {category for category, _ in lines}
        known = set(
            connection.scalars(
                select(Category.id).where(Category.id.in_(wanted), Category.archived_at.is_(None))
            )
        )
        if wanted - known:
            raise CategorizationError("catégorie inconnue ou archivée")
        total = sum(line_amount for _, line_amount in lines)
        if total != amount:
            raise CategorizationError(
                f"la somme des lignes ({total}) doit égaler le montant ({amount})"
            )
        current = connection.scalars(
            select(Categorization.id).where(
                Categorization.transaction_id == transaction_id,
                Categorization.superseded_at.is_(None),
            )
        ).all()
        _supersede(connection, list(current), now)
        for category_id, line_amount in lines:
            connection.execute(
                insert(Categorization).values(
                    transaction_id=transaction_id,
                    category_id=category_id,
                    amount_cents=line_amount,
                    source=CategorizationSource.MANUAL,
                    created_at=now,
                )
            )
        audit(
            connection,
            "categorization.manual",
            ("ledger_transaction", transaction_id),
            now=now,
            actor_user_id=actor_user_id,
            details={"lines": [list(line) for line in lines]},
        )


def clear_manual(
    engine: Engine, transaction_id: int, *, actor_user_id: int | None, now: datetime
) -> bool:
    """Retire la catégorisation manuelle ; les règles s'appliquent de nouveau. Faux si aucune."""
    with write_transaction(engine) as connection:
        manual = connection.scalars(
            select(Categorization.id).where(
                Categorization.transaction_id == transaction_id,
                Categorization.superseded_at.is_(None),
                Categorization.source == CategorizationSource.MANUAL,
            )
        ).all()
        if not manual:
            return False
        _supersede(connection, list(manual), now)
        audit(
            connection,
            "categorization.cleared",
            ("ledger_transaction", transaction_id),
            now=now,
            actor_user_id=actor_user_id,
        )
        _apply(connection, now, [transaction_id])
    return True


def suggest_rule(engine: Engine, transaction_id: int) -> RuleSuggestion:
    """Règle proposée après une correction manuelle (ANA-03) : début stable du libellé
    (sans références ni dates), et sens de l'opération."""
    with engine.connect() as connection:
        tx = connection.execute(
            select(LedgerTransaction.label_clean, LedgerTransaction.amount_cents).where(
                LedgerTransaction.id == transaction_id
            )
        ).one()
    words: list[str] = []
    for word in tx.label_clean.split():
        if any(char.isdigit() for char in word) or len(words) == 4:  # noqa: PLR2004
            break
        words.append(word)
    fragment = " ".join(words) or tx.label_clean
    conditions = RuleConditions(
        label_contains=[fragment], direction="debit" if tx.amount_cents < 0 else "credit"
    )
    return RuleSuggestion(name=fragment.title(), conditions=conditions)


def create_rule_from_transaction(
    engine: Engine,
    transaction_id: int,
    *,
    actor_user_id: int | None,
    now: datetime,
    manual_only: bool = False,
) -> int | None:
    """Crée la règle suggérée (ANA-03) vers la catégorie actuelle de l'opération ; None si
    l'opération n'est pas catégorisée (ou, avec `manual_only`, pas à la main)."""
    query = select(Categorization.category_id).where(
        Categorization.transaction_id == transaction_id, Categorization.superseded_at.is_(None)
    )
    if manual_only:
        query = query.where(Categorization.source == CategorizationSource.MANUAL)
    with engine.connect() as connection:
        category_id = connection.scalars(query).first()
    if category_id is None:
        return None
    suggestion = suggest_rule(engine, transaction_id)
    return create_rule(
        engine,
        name=suggestion.name,
        priority=SUGGESTED_RULE_PRIORITY,
        conditions=suggestion.conditions.model_dump(exclude_none=True),
        category_id=category_id,
        actor_user_id=actor_user_id,
        now=now,
    )
