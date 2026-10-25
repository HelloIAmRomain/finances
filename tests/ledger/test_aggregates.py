"""Agrégats du tableau de bord (ANA-09, NF-PERF-02)."""

import time
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import Engine, insert, select, update

from app.categorize.seed import seed_categories
from app.categorize.service import categorize_manually
from app.ledger.aggregates import (
    UNCATEGORIZED,
    MonthFlow,
    expenses_by_category,
    monthly_flows,
    net_worth,
)
from app.models import (
    Account,
    BalanceSnapshot,
    Category,
    DuplicateLink,
    LedgerTransaction,
    TransferLink,
)
from app.models.ledger import (
    BalanceType,
    DuplicateReason,
    DuplicateStatus,
    LinkMode,
    TransferStatus,
)
from app.months import month_range, shift_month

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
_ids = iter(range(1, 10_000_000))


def _tx(engine: Engine, amount: int, day: date, account: int = 1, label: str = "OP") -> int:
    tx_id = next(_ids)
    with engine.begin() as connection:
        connection.execute(
            insert(LedgerTransaction).values(
                id=tx_id,
                account_id=account,
                dedupe_key=f"ext:t:{tx_id}",
                fingerprint="x",
                occurrence=1,
                booking_date=day,
                amount_cents=amount,
                currency="EUR",
                label_raw=label,
                label_clean=label,
                created_at=NOW,
            )
        )
    return tx_id


def _cat(engine: Engine, name: str) -> int:
    with engine.connect() as connection:
        return int(connection.scalars(select(Category.id).where(Category.name == name)).one())


@pytest.fixture
def db(engine: Engine) -> Engine:
    seed_categories(engine)
    return engine


def test_month_helpers() -> None:
    assert month_range("2025-11", "2026-02") == ["2025-11", "2025-12", "2026-01", "2026-02"]
    assert shift_month("2026-01", -1) == "2025-12" and shift_month("2026-09", -12) == "2025-09"


def test_flows_exclude_transfers_duplicates_and_savings(db: Engine) -> None:
    salary = _tx(db, 250000, date(2026, 9, 1), account=2)
    groceries = _tx(db, -4520, date(2026, 9, 3))
    _tx(db, -1000, date(2026, 9, 5))  # non catégorisée : compte quand même
    out = _tx(db, -50000, date(2026, 9, 10))
    into = _tx(db, 50000, date(2026, 9, 10), account=2)
    copy = _tx(db, -4520, date(2026, 9, 3))
    savings = _tx(db, -20000, date(2026, 9, 15))
    categorize_manually(db, salary, [(_cat(db, "Salaire"), 250000)], actor_user_id=None, now=NOW)
    categorize_manually(db, groceries, [(_cat(db, "Courses"), -4520)], actor_user_id=None, now=NOW)
    categorize_manually(
        db, savings, [(_cat(db, "Versement épargne"), -20000)], actor_user_id=None, now=NOW
    )
    with db.begin() as connection:
        connection.execute(
            insert(TransferLink).values(
                debit_tx_id=out,
                credit_tx_id=into,
                method=LinkMode.AUTO,
                status=TransferStatus.CONFIRMED,
                created_at=NOW,
            )
        )
        connection.execute(
            insert(DuplicateLink).values(
                canonical_tx_id=groceries,
                duplicate_tx_id=copy,
                mode=LinkMode.AUTO,
                reason=DuplicateReason.EXACT,
                status=DuplicateStatus.CONFIRMED,
                created_at=NOW,
            )
        )
    [september] = monthly_flows(db, "2026-09", "2026-09")
    assert september == MonthFlow("2026-09", income=250000, expenses=-5520)
    assert september.net == 244480
    assert september.savings_rate_permille == 978  # 244480 / 250000 = 97,792 % → 978 ‰


def test_split_transaction_counts_by_lines(db: Engine) -> None:
    tx = _tx(db, -10000, date(2026, 9, 3))
    categorize_manually(
        db,
        tx,
        [(_cat(db, "Courses"), -7000), (_cat(db, "Pharmacie"), -3000)],
        actor_user_id=None,
        now=NOW,
    )
    _tx(db, -500, date(2026, 9, 4))
    assert expenses_by_category(db, "2026-09") == [
        ("Alimentation", -7000),
        ("Santé", -3000),
        (UNCATEGORIZED, -500),
    ]


def test_empty_months_are_zero(db: Engine) -> None:
    _tx(db, -100, date(2026, 7, 1))
    flows = monthly_flows(db, "2026-06", "2026-08")
    assert [(f.month, f.expenses) for f in flows] == [
        ("2026-06", 0),
        ("2026-07", -100),
        ("2026-08", 0),
    ]
    assert flows[0].savings_rate_permille is None


def test_net_worth_uses_latest_booked_balance(db: Engine) -> None:
    with db.begin() as connection:
        for account, day, amount in ((1, 25, 100000), (1, 26, 120000), (2, 26, 5000)):
            connection.execute(
                insert(BalanceSnapshot).values(
                    account_id=account,
                    as_of_date=date(2026, 9, day),
                    balance_type=BalanceType.BOOKED,
                    amount_cents=amount,
                    currency="EUR",
                    fetched_at=NOW,
                )
            )
        connection.execute(
            insert(Account).values(
                id=3,
                name="Livret sans solde",
                type="savings",
                currency="EUR",
                include_in_net_worth=True,
                syncs_per_day=1,
            )
        )
        connection.execute(
            insert(Account).values(
                id=4,
                name="Hors patrimoine",
                type="other",
                currency="EUR",
                include_in_net_worth=False,
                syncs_per_day=1,
            )
        )
        connection.execute(update(Account).where(Account.id == 2).values(include_in_net_worth=True))
    worth = net_worth(db)
    assert worth.total == 125000
    assert worth.accounts_without_balance == ["Livret sans solde"]


def _consultation_only(engine: Engine, account_id: int) -> None:
    with engine.begin() as connection:
        connection.execute(
            update(Account).where(Account.id == account_id).values(include_in_net_worth=False)
        )


def _confirmed_transfer(engine: Engine, debit: int, credit: int) -> None:
    with engine.begin() as connection:
        connection.execute(
            insert(TransferLink).values(
                debit_tx_id=debit,
                credit_tx_id=credit,
                method=LinkMode.AUTO,
                status=TransferStatus.CONFIRMED,
                created_at=NOW,
            )
        )


def test_consultation_only_account_is_not_added(db: Engine) -> None:
    """Carte à débit immédiat : ses paiements répètent ceux du compte courant ; en consultation
    seule, elle ne double pas les dépenses et son solde n'entre pas dans le patrimoine."""
    _tx(db, -4520, date(2026, 9, 3), account=1)
    _tx(db, -4520, date(2026, 9, 3), account=2)  # la même dépense vue sur la carte
    _consultation_only(db, 2)
    with db.begin() as connection:
        for account, amount in ((1, 100000), (2, -4520)):
            connection.execute(
                insert(BalanceSnapshot).values(
                    account_id=account,
                    as_of_date=date(2026, 9, 26),
                    balance_type=BalanceType.BOOKED,
                    amount_cents=amount,
                    currency="EUR",
                    fetched_at=NOW,
                )
            )
    assert monthly_flows(db, "2026-09", "2026-09")[0].expenses == -4520
    assert expenses_by_category(db, "2026-09") == [(UNCATEGORIZED, -4520)]
    assert net_worth(db).total == 100000


def test_transfer_to_consultation_only_account_stays_an_expense(db: Engine) -> None:
    """Un versement sur le compte joint (consultation seule) est une dépense ; entre deux comptes
    comptés, un virement interne n'est ni une recette ni une dépense."""
    to_joint = _tx(db, -30000, date(2026, 9, 5), account=1)
    joint_in = _tx(db, 30000, date(2026, 9, 5), account=2)
    _confirmed_transfer(db, to_joint, joint_in)
    assert monthly_flows(db, "2026-09", "2026-09")[0].expenses == 0  # les deux sont comptés
    _consultation_only(db, 2)
    [september] = monthly_flows(db, "2026-09", "2026-09")
    assert (september.income, september.expenses) == (0, -30000)


@pytest.mark.slow
def test_ten_years_under_two_seconds(db: Engine) -> None:
    """NF-PERF-02 : tableau de bord en moins de 2 s sur 10 ans d'historique (~60 000 opérations)."""
    start = date(2016, 10, 1)
    rows = [
        {
            "id": 1_000_000 + n,
            "account_id": 1 + n % 2,
            "dedupe_key": f"ext:p:{n}",
            "fingerprint": "x",
            "occurrence": 1,
            "booking_date": start + timedelta(days=n // 16),
            "amount_cents": -(n % 9000) - 1,
            "currency": "EUR",
            "label_raw": "CB",
            "label_clean": "CB",
            "created_at": NOW,
        }
        for n in range(60_000)
    ]
    with db.begin() as connection:
        connection.execute(insert(LedgerTransaction), rows)
    began = time.perf_counter()
    monthly_flows(db, shift_month("2026-09", -11), "2026-09")
    monthly_flows(db, shift_month("2025-09", -11), "2025-09")
    expenses_by_category(db, "2026-09")
    net_worth(db)
    assert time.perf_counter() - began < 2


def test_ignored_account_is_left_out_everywhere(db: Engine) -> None:
    """Q19 : un compte ignoré, même marqué « compté », n'entre ni dans les flux ni dans le
    patrimoine."""
    _tx(db, -1000, date(2026, 9, 3), account=1)
    _tx(db, -99999, date(2026, 9, 3), account=2)
    with db.begin() as connection:
        connection.execute(update(Account).where(Account.id == 2).values(ignored_at=NOW))
        connection.execute(
            insert(BalanceSnapshot).values(
                account_id=2,
                as_of_date=date(2026, 9, 26),
                balance_type=BalanceType.BOOKED,
                amount_cents=500000,
                currency="EUR",
                fetched_at=NOW,
            )
        )
    assert monthly_flows(db, "2026-09", "2026-09")[0].expenses == -1000
    assert net_worth(db).total == 0


def test_net_worth_falls_back_to_expected_balance(db: Engine) -> None:
    """La SG ne fournit qu'un solde prévisionnel (XPCD) : il sert de référence faute de solde
    comptable ; un solde comptable, même plus ancien, reste prioritaire."""
    with db.begin() as connection:
        for account, day, kind, amount in (
            (1, 20, BalanceType.BOOKED, 100000),
            (1, 26, BalanceType.AVAILABLE, 999999),  # ignoré : un comptable existe
            (2, 26, BalanceType.AVAILABLE, 5000),
            (2, 25, BalanceType.OTHER, 1),
        ):
            connection.execute(
                insert(BalanceSnapshot).values(
                    account_id=account,
                    as_of_date=date(2026, 9, day),
                    balance_type=kind,
                    amount_cents=amount,
                    currency="EUR",
                    fetched_at=NOW,
                )
            )
    worth = net_worth(db)
    assert (worth.total, worth.accounts_without_balance) == (105000, [])
