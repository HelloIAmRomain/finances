"""Création d'un bail complet, commune à la page web et à l'IA (LOC-01, LOC-02, LOC-12)."""

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import Engine, insert, select

from app.models import Lease, LedgerTransaction, OwnerShare, Party
from app.models.rental import LeaseKind, PartyRole
from app.rental.leases import (
    LeaseError,
    LeaseTerms,
    NewParty,
    create_lease_with_parties,
    first_history_month,
    lease_account,
    owner_shares,
)

NOW = datetime(2026, 9, 20, 10, 0, tzinfo=UTC)
TENANT = NewParty(PartyRole.TENANT, "Alice Exemple", patterns=("VIR DE ALICE",))


def _terms(kind: LeaseKind = LeaseKind.RENT, **changes: object) -> LeaseTerms:
    values: dict[str, object] = {
        "kind": kind,
        "label": "Studio",
        "account_id": 1,
        "start_date": date(2025, 9, 1),
        "rent_cents": 80_000,
    }
    return LeaseTerms(**(values | changes))  # type: ignore[arg-type]


def _history_from(engine: Engine, day: date) -> None:
    with engine.begin() as connection:
        connection.execute(
            insert(LedgerTransaction).values(
                account_id=1, dedupe_key="ext:t:1", fingerprint="x", occurrence=1,
                booking_date=day, amount_cents=-100, currency="EUR", label_raw="CB",
                label_clean="CB", created_at=NOW,
            )
        )  # fmt: skip


@pytest.mark.parametrize(
    ("percent", "expected"),
    [(100, [("moi", 10_000)]), (50, [("moi", 5_000), ("co-indivisaire", 5_000)]),
     (1, [("moi", 100), ("co-indivisaire", 9_900)])],
)  # fmt: skip
def test_owner_shares_always_total_one_hundred_percent(
    percent: int, expected: list[tuple[str, int]]
) -> None:
    assert owner_shares(percent) == expected


@pytest.mark.parametrize("percent", [0, 101, -5])
def test_owner_share_out_of_range_is_refused(percent: int) -> None:
    with pytest.raises(LeaseError, match="Quote-part"):
        owner_shares(percent)


def test_rent_with_new_property_shares_and_parties(engine: Engine) -> None:
    _history_from(engine, date(2026, 7, 14))
    guarantor = NewParty(PartyRole.GUARANTOR, "Bob Exemple")
    lease_id = create_lease_with_parties(
        engine,
        _terms(),
        new_property=("Studio Exemple", 50),
        parties=[TENANT, guarantor],
        now=NOW,
        actor_user_id=None,
    )
    with engine.connect() as connection:
        lease = connection.execute(select(Lease).where(Lease.id == lease_id)).one()
        shares = connection.execute(select(OwnerShare.owner_label, OwnerShare.share_bp)).all()
        roles = connection.scalars(select(Party.role).where(Party.lease_id == lease_id)).all()
    assert sorted(shares) == [("co-indivisaire", 5_000), ("moi", 5_000)]
    # Historique depuis le 14/07 : le loyer de juillet est invisible, le suivi part d'août.
    assert lease.tracking_start == date(2026, 8, 1)
    assert sorted(roles) == sorted([PartyRole.TENANT, PartyRole.GUARANTOR])
    assert lease_account(engine, lease_id) == 1
    assert lease_account(engine, 999) is None


def test_explicit_tracking_and_loan_without_property(engine: Engine) -> None:
    lease_id = create_lease_with_parties(
        engine,
        _terms(LeaseKind.LOAN, tracking_start=date(2026, 1, 1)),
        new_property=("ignoré pour un remboursement", 100),
        parties=[NewParty(PartyRole.BORROWER, "Carole Exemple")],
        now=NOW,
        actor_user_id=None,
    )
    with engine.connect() as connection:
        lease = connection.execute(select(Lease).where(Lease.id == lease_id)).one()
        assert connection.scalar(select(OwnerShare.id)) is None
    assert (lease.property_id, lease.tracking_start) == (None, date(2026, 1, 1))


@pytest.mark.parametrize(
    ("new_property", "parties", "message"),
    [(("Studio", 100), [], "payeur"), (None, [TENANT], "bien")],
)
def test_incomplete_leases_are_refused(
    engine: Engine,
    new_property: tuple[str, int] | None,
    parties: list[NewParty],
    message: str,
) -> None:
    with pytest.raises(LeaseError, match=message):
        create_lease_with_parties(
            engine, _terms(), new_property=new_property, parties=parties, now=NOW, actor_user_id=1
        )
    with engine.connect() as connection:
        assert connection.scalar(select(Lease.id)) is None


def test_first_history_month_without_history(engine: Engine) -> None:
    assert first_history_month(engine, 1) is None


@pytest.mark.parametrize(
    ("first", "expected"),
    [(date(2026, 7, 1), date(2026, 7, 1)), (date(2026, 6, 30), date(2026, 7, 1)),
     (date(2026, 12, 2), date(2027, 1, 1))],
)  # fmt: skip
def test_first_history_month_is_the_first_complete_month(
    engine: Engine, first: date, expected: date
) -> None:
    _history_from(engine, first)
    assert first_history_month(engine, 1) == expected
