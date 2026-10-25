"""Virements internes entre comptes du périmètre (COL-06)."""

import time
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import Engine, func, insert, select, update

from app.crypto import blind_index
from app.iban import normalize_iban
from app.ledger.duplicates import confirm_link
from app.ledger.transfers import detect_transfers
from app.ledger.views import non_transfer_transactions
from app.models import (
    Account,
    DuplicateLink,
    LedgerTransaction,
    ReviewItem,
    TransferLink,
)
from app.models.categorize import ReviewKind
from app.models.ledger import DuplicateReason, DuplicateStatus, LinkMode, TransferStatus
from tests.factories import fake_iban

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
_next_id = iter(range(1, 10_000))


def _set_iban(engine: Engine, account_id: int, iban: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            update(Account)
            .where(Account.id == account_id)
            .values(iban_enc=iban, iban_hash=blind_index(normalize_iban(iban), context="iban"))
        )


def _tx(
    engine: Engine, account: int, amount: int, day: int, counterparty_iban: str | None = None
) -> int:
    tx_id = next(_next_id)
    with engine.begin() as connection:
        connection.execute(
            insert(LedgerTransaction).values(
                id=tx_id,
                account_id=account,
                dedupe_key=f"ext:test:{tx_id}",
                fingerprint="x",
                occurrence=1,
                booking_date=date(2026, 9, day),
                amount_cents=amount,
                currency="EUR",
                counterparty_iban_enc=counterparty_iban,
                counterparty_iban_hash=(
                    blind_index(normalize_iban(counterparty_iban), context="iban")
                    if counterparty_iban
                    else None
                ),
                label_raw="VIR",
                label_clean="VIR",
                created_at=NOW,
            )
        )
    return tx_id


def _links(engine: Engine) -> list[tuple[int, int, str, str]]:
    with engine.connect() as connection:
        rows = connection.execute(
            select(
                TransferLink.debit_tx_id,
                TransferLink.credit_tx_id,
                TransferLink.method,
                TransferLink.status,
            ).order_by(TransferLink.id)
        ).all()
    return [(d, c, str(m), str(s)) for d, c, m, s in rows]


@pytest.fixture
def two_accounts(engine: Engine) -> Engine:
    _set_iban(engine, 1, fake_iban(101))
    _set_iban(engine, 2, fake_iban(102))
    return engine


def test_strong_match_is_linked_automatically(two_accounts: Engine) -> None:
    out = _tx(two_accounts, 1, -50000, 10, counterparty_iban=fake_iban(102))
    incoming = _tx(two_accounts, 2, 50000, 11, counterparty_iban=fake_iban(101))
    detect_transfers(two_accounts)
    assert _links(two_accounts) == [(out, incoming, "auto", "confirmed")]


def test_without_iban_evidence_only_a_proposal(two_accounts: Engine) -> None:
    out = _tx(two_accounts, 1, -50000, 10)
    incoming = _tx(two_accounts, 2, 50000, 10)
    detect_transfers(two_accounts)
    assert _links(two_accounts) == [(out, incoming, "auto", "proposed")]
    with two_accounts.connect() as connection:
        kinds = connection.scalars(select(ReviewItem.kind)).all()
    assert kinds == [ReviewKind.TRANSFER_CANDIDATE]


def test_ambiguous_match_is_never_automatic(two_accounts: Engine) -> None:
    out = _tx(two_accounts, 1, -50000, 10, counterparty_iban=fake_iban(102))
    first = _tx(two_accounts, 2, 50000, 10, counterparty_iban=fake_iban(101))
    _tx(two_accounts, 2, 50000, 11, counterparty_iban=fake_iban(101))
    detect_transfers(two_accounts)
    assert _links(two_accounts) == [(out, first, "auto", "proposed")]  # le plus proche, proposé


def test_same_account_amount_or_distant_dates_are_not_transfers(two_accounts: Engine) -> None:
    _tx(two_accounts, 1, -50000, 10)
    _tx(two_accounts, 1, 50000, 10)  # même compte
    _tx(two_accounts, 1, -1000, 1)
    _tx(two_accounts, 2, 1000, 9)  # 8 jours d'écart
    _tx(two_accounts, 1, -2000, 5)
    _tx(two_accounts, 2, 2001, 5)  # montants différents
    detect_transfers(two_accounts)
    assert _links(two_accounts) == []


def test_detection_is_idempotent_and_rejections_stick(two_accounts: Engine) -> None:
    _tx(two_accounts, 1, -50000, 10)
    _tx(two_accounts, 2, 50000, 10)
    detect_transfers(two_accounts)
    detect_transfers(two_accounts)
    assert len(_links(two_accounts)) == 1
    with two_accounts.begin() as connection:
        connection.execute(update(TransferLink).values(status=TransferStatus.REJECTED))
    detect_transfers(two_accounts)
    assert len(_links(two_accounts)) == 1


def test_each_transaction_in_at_most_one_active_link(two_accounts: Engine) -> None:
    out = _tx(two_accounts, 1, -50000, 10, counterparty_iban=fake_iban(102))
    _tx(two_accounts, 2, 50000, 10, counterparty_iban=fake_iban(101))
    _tx(two_accounts, 2, 50000, 12)
    detect_transfers(two_accounts)
    with two_accounts.connect() as connection:
        count = connection.execute(
            select(func.count())
            .select_from(TransferLink)
            .where(
                TransferLink.debit_tx_id == out,
                TransferLink.status.in_([TransferStatus.PROPOSED, TransferStatus.CONFIRMED]),
            )
        ).scalar_one()
    assert count == 1


def test_confirmed_transfers_are_excluded_from_income_and_expenses(two_accounts: Engine) -> None:
    out = _tx(two_accounts, 1, -50000, 10, counterparty_iban=fake_iban(102))
    incoming = _tx(two_accounts, 2, 50000, 10, counterparty_iban=fake_iban(101))
    salary = _tx(two_accounts, 2, 250000, 1)
    detect_transfers(two_accounts)
    with two_accounts.connect() as connection:
        ids = set(connection.scalars(non_transfer_transactions()))
    assert salary in ids
    assert out not in ids and incoming not in ids


def test_confirmed_duplicates_are_ignored(two_accounts: Engine) -> None:
    out = _tx(two_accounts, 1, -50000, 10)
    copy = _tx(two_accounts, 1, -50000, 10)
    incoming = _tx(two_accounts, 2, 50000, 10)
    with two_accounts.begin() as connection:
        connection.execute(
            insert(DuplicateLink).values(
                id=1,
                canonical_tx_id=out,
                duplicate_tx_id=copy,
                mode=LinkMode.AUTO,
                reason=DuplicateReason.EXACT,
                status=DuplicateStatus.PROPOSED,
                created_at=NOW,
            )
        )
    confirm_link(two_accounts, 1, actor_user_id=None, now=NOW)
    detect_transfers(two_accounts)
    assert [link[:2] for link in _links(two_accounts)] == [(out, incoming)]


@pytest.mark.slow
def test_detection_stays_fast_on_ten_years_of_history(two_accounts: Engine) -> None:
    """Après chaque synchro : 60 000 opérations (dont 10 000 crédits sans contrepartie) en moins
    de 2 s sur le Pi ; l'ancienne version comparait chaque débit à chaque crédit."""
    start = date(2016, 10, 1)
    rows = [
        {
            "id": 1_000_000 + n,
            "account_id": 1 + n % 2,
            "dedupe_key": f"ext:perf:{n}",
            "fingerprint": "x",
            "occurrence": 1,
            "booking_date": start + timedelta(days=n // 16),
            "amount_cents": (n % 7919) + 1 if n % 6 == 0 else -((n % 9001) + 1),
            "currency": "EUR",
            "label_raw": "CB",
            "label_clean": "CB",
            "created_at": NOW,
        }
        for n in range(60_000)
    ]
    with two_accounts.begin() as connection:
        connection.execute(insert(LedgerTransaction), rows)
    began = time.perf_counter()
    detect_transfers(two_accounts)
    assert time.perf_counter() - began < 2
