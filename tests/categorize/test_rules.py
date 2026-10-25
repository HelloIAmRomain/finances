"""Catégorisation par règles et manuelle (ANA-01, ANA-03, ANA-04, NF-SEC-06)."""

import time
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import Engine, func, insert, select

from app.categorize.rules import RuleConditions, matches
from app.categorize.seed import seed_categories
from app.categorize.service import (
    CategorizationError,
    apply_rules,
    categorize_manually,
    create_rule,
    deactivate_rule,
    suggest_rule,
    update_rule,
)
from app.models import AuditLog, Categorization, Category, LedgerTransaction
from app.models.categorize import CategorizationSource

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
_ids = iter(range(1, 10_000))


def _tx(engine: Engine, label: str, amount: int, account: int = 1) -> int:
    tx_id = next(_ids)
    with engine.begin() as connection:
        connection.execute(
            insert(LedgerTransaction).values(
                id=tx_id,
                account_id=account,
                dedupe_key=f"ext:test:{tx_id}",
                fingerprint="x",
                occurrence=1,
                booking_date=date(2026, 9, 3),
                amount_cents=amount,
                currency="EUR",
                label_raw=label,
                label_clean=label.upper(),
                created_at=NOW,
            )
        )
    return tx_id


def _category(engine: Engine, name: str) -> int:
    with engine.connect() as connection:
        return int(connection.scalars(select(Category.id).where(Category.name == name)).one())


def _current(engine: Engine, tx_id: int) -> list[tuple[str, int, str]]:
    with engine.connect() as connection:
        rows = connection.execute(
            select(Category.name, Categorization.amount_cents, Categorization.source)
            .join(Category, Category.id == Categorization.category_id)
            .where(Categorization.transaction_id == tx_id, Categorization.superseded_at.is_(None))
            .order_by(Categorization.id)
        ).all()
    return [(name, amount, str(source)) for name, amount, source in rows]


@pytest.fixture
def db(engine: Engine) -> Engine:
    seed_categories(engine)
    return engine


# --- Conditions -------------------------------------------------------------------------------

LABEL = "CB CARREFOUR MARKET"


@pytest.mark.parametrize(
    ("conditions", "expected"),
    [
        ({"label_contains": ["carrefour"]}, True),
        ({"label_contains": ["auchan", "carrefour"]}, True),  # l'un des mots
        ({"label_contains": ["auchan"]}, False),
        ({"label_regex": r"^CB CARREF"}, True),
        ({"amount_max": -1000}, True),
        ({"amount_min": 0}, False),
        ({"direction": "debit"}, True),
        ({"direction": "credit"}, False),
        ({"account_ids": [1]}, True),
        ({"account_ids": [2]}, False),
        ({"label_contains": ["carrefour"], "amount_min": -2000}, True),  # toutes les conditions
        ({"label_contains": ["carrefour"], "amount_min": -1000}, False),
    ],
)
def test_conditions(conditions: dict[str, object], expected: bool) -> None:
    rule = RuleConditions.model_validate(conditions)
    assert matches(rule, label=LABEL, amount=-1500, account_id=1, counterparty=None) is expected


def test_empty_conditions_are_refused() -> None:
    with pytest.raises(ValueError, match="au moins une condition"):
        RuleConditions.model_validate({})


def test_invalid_regex_is_refused() -> None:
    with pytest.raises(ValueError, match="expression"):
        RuleConditions.model_validate({"label_regex": "(("})


# --- Application ----------------------------------------------------------------------------


@pytest.mark.parametrize("pattern", ["(a|aa)+$", "^(1|11)+$", "a" * 201])
def test_runaway_or_huge_regex_is_refused(pattern: str) -> None:
    """Une expression qui s'emballe (retour arrière exponentiel) bloquerait le serveur : elle
    est refusée à la création, depuis la page Règles comme depuis l'IA."""
    with pytest.raises(ValueError, match=r"trop coûteuse|trop longue"):
        RuleConditions(label_regex=pattern)


def test_slow_regex_on_a_label_is_cut_short(monkeypatch: pytest.MonkeyPatch) -> None:
    # Expression passée à la création (avant ce garde-fou) : la recherche reste bornée.
    conditions = RuleConditions.model_construct(label_regex="(a|aa)+$")
    started = time.monotonic()
    found = matches(conditions, label="a" * 60 + "!", amount=-100, account_id=1, counterparty=None)
    assert not found and time.monotonic() - started < 1


def test_first_rule_by_priority_wins(db: Engine) -> None:
    tx = _tx(db, "CB CARREFOUR MARKET", -1500)
    create_rule(
        db,
        name="Courses",
        priority=20,
        conditions={"label_contains": ["carrefour"]},
        category_id=_category(db, "Courses"),
        actor_user_id=None,
        now=NOW,
    )
    create_rule(
        db,
        name="Restos",
        priority=10,
        conditions={"label_contains": ["market"]},
        category_id=_category(db, "Restaurants"),
        actor_user_id=None,
        now=NOW,
    )
    apply_rules(db, now=NOW)
    assert _current(db, tx) == [("Restaurants", -1500, "rule")]


def test_manual_categorization_is_never_overridden(db: Engine) -> None:
    tx = _tx(db, "CB CARREFOUR MARKET", -1500)
    categorize_manually(
        db, tx, [(_category(db, "Restaurants"), -1500)], actor_user_id=None, now=NOW
    )
    create_rule(
        db,
        name="Courses",
        priority=1,
        conditions={"label_contains": ["carrefour"]},
        category_id=_category(db, "Courses"),
        actor_user_id=None,
        now=NOW,
    )
    apply_rules(db, now=NOW)
    assert _current(db, tx) == [("Restaurants", -1500, "manual")]


def test_rule_change_reapplies_to_non_manual_only(db: Engine) -> None:
    auto = _tx(db, "CB CARREFOUR", -1500)
    manual = _tx(db, "CB CARREFOUR CITY", -800)
    rule_id = create_rule(
        db,
        name="Courses",
        priority=1,
        conditions={"label_contains": ["carrefour"]},
        category_id=_category(db, "Courses"),
        actor_user_id=None,
        now=NOW,
    )
    apply_rules(db, now=NOW)
    categorize_manually(
        db, manual, [(_category(db, "Restaurants"), -800)], actor_user_id=None, now=NOW
    )
    update_rule(db, rule_id, category_id=_category(db, "Restaurants"), actor_user_id=None, now=NOW)
    assert _current(db, auto) == [("Restaurants", -1500, "rule")]
    assert _current(db, manual) == [("Restaurants", -800, "manual")]


def test_deactivated_rule_uncategorizes_its_transactions(db: Engine) -> None:
    tx = _tx(db, "CB CARREFOUR", -1500)
    rule_id = create_rule(
        db,
        name="Courses",
        priority=1,
        conditions={"label_contains": ["carrefour"]},
        category_id=_category(db, "Courses"),
        actor_user_id=None,
        now=NOW,
    )
    apply_rules(db, now=NOW)
    deactivate_rule(db, rule_id, actor_user_id=None, now=NOW)
    assert _current(db, tx) == []


def test_apply_rules_is_idempotent(db: Engine) -> None:
    _tx(db, "CB CARREFOUR", -1500)
    create_rule(
        db,
        name="Courses",
        priority=1,
        conditions={"label_contains": ["carrefour"]},
        category_id=_category(db, "Courses"),
        actor_user_id=None,
        now=NOW,
    )
    apply_rules(db, now=NOW)
    apply_rules(db, now=NOW)
    with db.connect() as connection:
        assert (
            connection.execute(select(func.count()).select_from(Categorization)).scalar_one() == 1
        )


def test_split_must_sum_to_transaction_amount(db: Engine) -> None:
    tx = _tx(db, "CB SUPERMARCHE", -10000)
    categorize_manually(
        db,
        tx,
        [(_category(db, "Courses"), -7000), (_category(db, "Santé"), -3000)],
        actor_user_id=None,
        now=NOW,
    )
    assert _current(db, tx) == [("Courses", -7000, "manual"), ("Santé", -3000, "manual")]
    with pytest.raises(CategorizationError, match="somme"):
        categorize_manually(
            db, tx, [(_category(db, "Courses"), -9000)], actor_user_id=None, now=NOW
        )


def test_manual_recategorization_keeps_history(db: Engine) -> None:
    tx = _tx(db, "CB SUPERMARCHE", -10000)
    categorize_manually(db, tx, [(_category(db, "Courses"), -10000)], actor_user_id=None, now=NOW)
    categorize_manually(db, tx, [(_category(db, "Santé"), -10000)], actor_user_id=None, now=NOW)
    with db.connect() as connection:
        total = connection.execute(select(func.count()).select_from(Categorization)).scalar_one()
    assert total == 2  # l'ancienne ligne est marquée remplacée, jamais effacée
    assert _current(db, tx) == [("Santé", -10000, "manual")]


def test_rule_changes_and_manual_categorizations_are_audited(db: Engine) -> None:
    tx = _tx(db, "CB CARREFOUR", -1500)
    rule_id = create_rule(
        db,
        name="Courses",
        priority=1,
        conditions={"label_contains": ["carrefour"]},
        category_id=_category(db, "Courses"),
        actor_user_id=None,
        now=NOW,
    )
    update_rule(db, rule_id, priority=2, actor_user_id=None, now=NOW)
    deactivate_rule(db, rule_id, actor_user_id=None, now=NOW)
    categorize_manually(db, tx, [(_category(db, "Courses"), -1500)], actor_user_id=None, now=NOW)
    with db.connect() as connection:
        actions = connection.scalars(select(AuditLog.action).order_by(AuditLog.id)).all()
    assert actions == ["rule.created", "rule.updated", "rule.deactivated", "categorization.manual"]


def test_suggest_rule_after_manual_correction(db: Engine) -> None:
    tx = _tx(db, "PRLV SEPA ENERGIE EXEMPLE ECH/100926", -8000)
    suggestion = suggest_rule(db, tx)
    assert suggestion.conditions.label_contains == ["PRLV SEPA ENERGIE EXEMPLE"]
    assert suggestion.conditions.direction == "debit"


# --- Arborescence par défaut ------------------------------------------------------------------


def test_seed_is_idempotent(engine: Engine) -> None:
    first = seed_categories(engine)
    second = seed_categories(engine)
    assert first > 20 and second == 0
    with engine.connect() as connection:
        kinds = dict(
            connection.execute(
                select(Category.name, Category.kind).where(Category.parent_id.is_(None))
            ).all()
        )
    assert str(kinds["Revenus"]) == "income"
    assert str(kinds["Virements internes"]) == "transfer"


def test_categorization_source_order() -> None:
    assert [s.value for s in CategorizationSource] == ["manual", "rule", "ai"]
