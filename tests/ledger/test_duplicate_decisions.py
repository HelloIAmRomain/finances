"""Décisions sur les doublons (T1.16b) : report, conflits, annulation exacte, exclusion."""

from collections import Counter
from datetime import UTC, date, datetime
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import Connection, Engine, delete, insert, select

from app.ledger.duplicates import (
    DuplicateDecisionError,
    confirm_link,
    reject_link,
    revert_link,
)
from app.ledger.views import is_effective
from app.models import (
    AuditLog,
    Categorization,
    Category,
    DuplicateLink,
    LedgerTransaction,
    ReviewItem,
    TransferLink,
)
from app.models.categorize import CategorizationSource, CategoryKind, ReviewKind, ReviewStatus
from app.models.ledger import DuplicateReason, DuplicateStatus, LinkMode, TransferStatus


def effective_transaction_ids(connection: Connection) -> set[int]:
    return set(connection.scalars(select(LedgerTransaction.id).where(is_effective())))


def effective_state(engine: Engine, tx_ids: list[int]) -> dict[str, Any]:
    """Ce que voit l'utilisateur pour ces transactions : catégorisations et virements actifs,
    conflits ouverts (aller-retour : confirmer puis annuler = identité)."""
    with engine.connect() as connection:
        categorizations = Counter(
            (row.transaction_id, row.category_id, row.amount_cents, str(row.source))
            for row in connection.execute(
                select(Categorization).where(
                    Categorization.transaction_id.in_(tx_ids),
                    Categorization.superseded_at.is_(None),
                )
            )
        )
        transfers = Counter(
            (row.debit_tx_id, row.credit_tx_id, str(row.method), str(row.status))
            for row in connection.execute(
                select(TransferLink).where(
                    TransferLink.status.in_((TransferStatus.PROPOSED, TransferStatus.CONFIRMED))
                )
            )
        )
        reviews = Counter(
            str(kind)
            for kind in connection.scalars(
                select(ReviewItem.kind).where(ReviewItem.status == ReviewStatus.OPEN)
            )
        )
    return {"categorizations": categorizations, "transfers": transfers, "reviews": reviews}


NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
CANONICAL, DUPLICATE, OTHER_1, OTHER_2 = 1, 2, 3, 4


def _setup(engine: Engine) -> None:
    with engine.begin() as connection:
        for tx_id, account, amount, key in (
            (CANONICAL, 1, -420, "ext:enable_banking:E1"),
            (DUPLICATE, 1, -420, "fp:csv:x:1"),
            (OTHER_1, 2, 420, "ext:enable_banking:E9"),
            (OTHER_2, 2, 420, "ext:enable_banking:E8"),
        ):
            connection.execute(
                insert(LedgerTransaction).values(
                    id=tx_id,
                    account_id=account,
                    dedupe_key=key,
                    fingerprint="x",
                    occurrence=1,
                    booking_date=date(2026, 9, 3),
                    amount_cents=amount,
                    currency="EUR",
                    label_raw="CB BOULANGERIE",
                    label_clean="CB BOULANGERIE",
                    created_at=NOW,
                )
            )
        for category_id in (1, 2, 3):
            connection.execute(
                insert(Category).values(
                    id=category_id, name=f"Catégorie {category_id}", kind=CategoryKind.EXPENSE
                )
            )
        connection.execute(
            insert(DuplicateLink).values(
                id=1,
                canonical_tx_id=CANONICAL,
                duplicate_tx_id=DUPLICATE,
                mode=LinkMode.AUTO,
                reason=DuplicateReason.PROBABLE,
                status=DuplicateStatus.PROPOSED,
                created_at=NOW,
            )
        )


def _categorize(connection: Connection, tx_id: int, category_id: int, source: str) -> None:
    connection.execute(
        insert(Categorization).values(
            transaction_id=tx_id,
            category_id=category_id,
            amount_cents=-420,
            source=CategorizationSource(source),
            created_at=NOW,
        )
    )


def _transfer(connection: Connection, tx_id: int, other: int, method: str, status: str) -> None:
    connection.execute(
        insert(TransferLink).values(
            debit_tx_id=tx_id,
            credit_tx_id=other,
            method=LinkMode(method),
            status=TransferStatus(status),
            created_at=NOW,
        )
    )


@pytest.fixture
def db(engine: Engine) -> Engine:
    _setup(engine)
    return engine


def _active_categories(engine: Engine, tx_id: int) -> list[tuple[int, str]]:
    with engine.connect() as connection:
        rows = connection.execute(
            select(Categorization.category_id, Categorization.source)
            .where(Categorization.transaction_id == tx_id, Categorization.superseded_at.is_(None))
            .order_by(Categorization.id)
        ).all()
    return [(category, str(source)) for category, source in rows]


def _open_reviews(engine: Engine) -> list[str]:
    with engine.connect() as connection:
        return [
            str(kind)
            for kind in connection.scalars(
                select(ReviewItem.kind).where(ReviewItem.status == ReviewStatus.OPEN)
            )
        ]


# --- Report des catégorisations ------------------------------------------------------------


def test_category_carried_to_uncategorized_canonical(db: Engine) -> None:
    with db.begin() as connection:
        _categorize(connection, DUPLICATE, 2, "manual")
    confirm_link(db, 1, actor_user_id=None, now=NOW)
    assert _active_categories(db, CANONICAL) == [(2, "manual")]


def test_manual_beats_ai_on_the_duplicate_side(db: Engine) -> None:
    with db.begin() as connection:
        _categorize(connection, CANONICAL, 1, "ai")
        _categorize(connection, DUPLICATE, 2, "manual")
    confirm_link(db, 1, actor_user_id=None, now=NOW)
    assert _active_categories(db, CANONICAL) == [(2, "manual")]


def test_canonical_kept_when_it_ranks_higher(db: Engine) -> None:
    with db.begin() as connection:
        _categorize(connection, CANONICAL, 1, "rule")
        _categorize(connection, DUPLICATE, 2, "ai")
    confirm_link(db, 1, actor_user_id=None, now=NOW)
    assert _active_categories(db, CANONICAL) == [(1, "rule")]
    assert _open_reviews(db) == []


def test_two_different_manual_categories_open_a_conflict(db: Engine) -> None:
    with db.begin() as connection:
        _categorize(connection, CANONICAL, 1, "manual")
        _categorize(connection, DUPLICATE, 2, "manual")
    confirm_link(db, 1, actor_user_id=None, now=NOW)
    assert _active_categories(db, CANONICAL) == [(1, "manual")]
    assert _open_reviews(db) == ["category_conflict"]


def test_same_manual_category_is_no_conflict(db: Engine) -> None:
    with db.begin() as connection:
        _categorize(connection, CANONICAL, 1, "manual")
        _categorize(connection, DUPLICATE, 1, "manual")
    confirm_link(db, 1, actor_user_id=None, now=NOW)
    assert _open_reviews(db) == []


# --- Report des virements internes -----------------------------------------------------------


def test_transfer_link_moves_to_canonical(db: Engine) -> None:
    with db.begin() as connection:
        _transfer(connection, DUPLICATE, OTHER_1, "manual", "confirmed")
    confirm_link(db, 1, actor_user_id=None, now=NOW)
    with db.connect() as connection:
        active = connection.execute(
            select(TransferLink.debit_tx_id, TransferLink.credit_tx_id).where(
                TransferLink.status.in_([TransferStatus.PROPOSED, TransferStatus.CONFIRMED])
            )
        ).all()
    assert active == [(CANONICAL, OTHER_1)]


def test_two_different_manual_transfers_open_a_conflict(db: Engine) -> None:
    with db.begin() as connection:
        _transfer(connection, CANONICAL, OTHER_1, "manual", "confirmed")
        _transfer(connection, DUPLICATE, OTHER_2, "manual", "confirmed")
    confirm_link(db, 1, actor_user_id=None, now=NOW)
    assert _open_reviews(db) == ["transfer_conflict"]


# --- Décisions et annulation -----------------------------------------------------------------


def test_confirmed_duplicate_is_excluded_then_back_after_revert(db: Engine) -> None:
    with db.connect() as connection:
        assert DUPLICATE in effective_transaction_ids(connection)
    confirm_link(db, 1, actor_user_id=None, now=NOW)
    with db.connect() as connection:
        assert DUPLICATE not in effective_transaction_ids(connection)
        assert CANONICAL in effective_transaction_ids(connection)
    revert_link(db, 1, actor_user_id=None, now=NOW)
    with db.connect() as connection:
        assert DUPLICATE in effective_transaction_ids(connection)


def test_reject_closes_review_and_keeps_both(db: Engine) -> None:
    with db.begin() as connection:
        connection.execute(
            insert(ReviewItem).values(
                kind=ReviewKind.DUPLICATE,
                dedupe_key="duplicate:1",
                duplicate_link_id=1,
                status=ReviewStatus.OPEN,
                created_at=NOW,
            )
        )
    reject_link(db, 1, actor_user_id=None, now=NOW)
    assert _open_reviews(db) == []
    with db.connect() as connection:
        assert DUPLICATE in effective_transaction_ids(connection)


def test_decisions_are_audited(db: Engine) -> None:
    confirm_link(db, 1, actor_user_id=None, now=NOW)
    revert_link(db, 1, actor_user_id=None, now=NOW)
    with db.connect() as connection:
        actions = connection.scalars(select(AuditLog.action).order_by(AuditLog.id)).all()
    assert actions == ["duplicate.confirmed", "duplicate.reverted"]


def test_invalid_transitions_are_refused(db: Engine) -> None:
    with pytest.raises(DuplicateDecisionError):
        revert_link(db, 1, actor_user_id=None, now=NOW)  # pas encore confirmé
    reject_link(db, 1, actor_user_id=None, now=NOW)
    with pytest.raises(DuplicateDecisionError):
        confirm_link(db, 1, actor_user_id=None, now=NOW)  # déjà rejeté


def test_confirm_twice_is_confirm_once(db: Engine) -> None:
    with db.begin() as connection:
        _categorize(connection, DUPLICATE, 2, "manual")
    confirm_link(db, 1, actor_user_id=None, now=NOW)
    state = effective_state(db, [CANONICAL, DUPLICATE])
    confirm_link(db, 1, actor_user_id=None, now=NOW)
    assert effective_state(db, [CANONICAL, DUPLICATE]) == state


# --- Propriété : confirmer puis annuler = identité --------------------------------------------

sources = st.sampled_from(["manual", "rule", "ai"])
categorizations = st.lists(
    st.tuples(st.sampled_from([CANONICAL, DUPLICATE]), st.sampled_from([1, 2, 3]), sources),
    max_size=4,
)
transfers = st.lists(
    st.tuples(
        st.sampled_from([CANONICAL, DUPLICATE]),
        st.sampled_from([OTHER_1, OTHER_2]),
        st.sampled_from(["manual", "auto"]),
        st.sampled_from(["proposed", "confirmed"]),
    ),
    max_size=2,
    unique_by=(lambda t: t[0], lambda t: t[1]),  # au plus un lien actif par transaction
)


@settings(max_examples=60, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(initial_categories=categorizations, initial_transfers=transfers)
def test_confirm_then_revert_is_identity(
    db: Engine,
    initial_categories: list[tuple[int, int, str]],
    initial_transfers: list[tuple[int, int, str, str]],
) -> None:
    with db.begin() as connection:
        for model in (Categorization, TransferLink, ReviewItem):
            connection.execute(delete(model))
        connection.execute(
            DuplicateLink.__table__.update().values(status=DuplicateStatus.PROPOSED)  # type: ignore[attr-defined]
        )
        for tx_id, category, source in initial_categories:
            _categorize(connection, tx_id, category, source)
        for tx_id, other, method, status in initial_transfers:
            _transfer(connection, tx_id, other, method, status)
    tx_ids = [CANONICAL, DUPLICATE, OTHER_1, OTHER_2]
    before: Any = effective_state(db, tx_ids)
    confirm_link(db, 1, actor_user_id=None, now=NOW)
    revert_link(db, 1, actor_user_id=None, now=NOW)
    assert effective_state(db, tx_ids) == before
