"""Révision annuelle du loyer selon l'IRL (LOC-09)."""

from datetime import UTC, date, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import Engine, select

from app.models import Lease, RentDue
from app.models.rental import LeaseKind
from app.rental.engine import run_rental
from app.rental.irl import (
    IrlError,
    apply_revision,
    indices,
    next_year,
    proposal,
    publication_hint,
    quarter_label,
    revised_rent,
    set_index,
    set_reference,
)
from app.rental.leases import LeaseTerms, create_lease, create_property

NOW = datetime(2026, 10, 7, 8, 0, tzinfo=UTC)


def _lease(
    engine: Engine, start: date = date(2025, 11, 1), kind: LeaseKind = LeaseKind.RENT
) -> int:
    property_id = create_property(
        engine, name="Studio", address=None, furnished=True, shares=[("moi", 10000)], now=NOW
    )
    return create_lease(
        engine,
        LeaseTerms(
            kind=kind, label="Bail", account_id=1, start_date=start, rent_cents=70_000,
            charges_cents=5_000, property_id=property_id if kind is LeaseKind.RENT else None,
        ),
        now=NOW,
    )  # fmt: skip


def test_formula_rounds_once() -> None:
    assert revised_rent(70_000, 14_517, 14_812) == 71_422  # 714,2219… → 714,22
    assert revised_rent(70_000, 14_517, 14_517) == 70_000
    assert revised_rent(70_000, 14_812, 14_517) == 68_606  # l'indice peut baisser


@given(
    rent=st.integers(min_value=1, max_value=10**7),
    old=st.integers(min_value=10_000, max_value=20_000),
    new=st.integers(min_value=10_000, max_value=20_000),
)
def test_revised_rent_is_the_exact_value_to_the_cent(rent: int, old: int, new: int) -> None:
    result = revised_rent(rent, old, new)
    assert abs(result * old - rent * new) * 2 <= old  # à un demi-centime près


def test_quarter_helpers() -> None:
    assert next_year("2025-T2") == "2026-T2"
    assert quarter_label("2026-T4") == "T4 2026"
    assert publication_hint("2026-T4") == "publié mi-janvier 2027"
    assert publication_hint("2026-T2") == "publié mi-juillet 2026"
    with pytest.raises(IrlError, match="T2 2026"):
        next_year("2026-Q2")


def test_proposal_then_application_reprices_future_dues(engine: Engine) -> None:
    lease_id = _lease(engine)
    assert proposal(engine, lease_id, date(2026, 10, 7)) is None  # pas de référence
    set_reference(engine, lease_id, "2025-T2", actor_user_id=None, now=NOW)
    assert proposal(engine, lease_id, date(2026, 9, 15)) is None  # plus de 30 jours avant
    waiting = proposal(engine, lease_id, date(2026, 10, 7))
    assert waiting is not None and not waiting.ready
    assert waiting.missing_quarter == "2025-T2"
    set_index(engine, "2025-T2", 14_517, actor_user_id=None, now=NOW)
    with pytest.raises(IrlError, match="Indice T2 2026 manquant"):
        apply_revision(engine, lease_id, today=date(2026, 10, 7), actor_user_id=None, now=NOW)
    set_index(engine, "2026-t2", 14_812, actor_user_id=None, now=NOW)
    ready = proposal(engine, lease_id, date(2026, 10, 7))
    assert ready is not None and ready.ready
    assert (ready.new_rent_cents, ready.effective_from) == (71_422, date(2026, 11, 1))

    run_rental(engine, now=datetime(2026, 10, 27, 8, 0, tzinfo=UTC))  # échéance de novembre créée
    apply_revision(engine, lease_id, today=date(2026, 10, 27), actor_user_id=None, now=NOW)
    with engine.connect() as connection:
        lease = connection.execute(select(Lease).where(Lease.id == lease_id)).one()
        dues = dict(connection.execute(select(RentDue.period, RentDue.amount_cents)).all())
    assert (lease.rent_cents, lease.charges_cents, lease.irl_reference_quarter) == (
        71_422,
        5_000,
        "2026-T2",
    )
    assert dues["2026-10"] == 75_000 and dues["2026-11"] == 76_422
    # Appliquée : plus rien à proposer pour cet anniversaire.
    assert proposal(engine, lease_id, date(2026, 11, 3)) is None


def test_late_revision_starts_this_month_without_retroactivity(engine: Engine) -> None:
    lease_id = _lease(engine, start=date(2025, 6, 1))
    set_reference(engine, lease_id, "2025-T1", actor_user_id=None, now=NOW)
    set_index(engine, "2025-T1", 14_500, actor_user_id=None, now=NOW)
    set_index(engine, "2026-T1", 14_600, actor_user_id=None, now=NOW)
    late = proposal(engine, lease_id, date(2026, 10, 7))
    assert late is not None and late.effective_from == date(2026, 10, 1)


def test_errors_and_listing(engine: Engine) -> None:
    loan = _lease(engine, kind=LeaseKind.LOAN)
    with pytest.raises(IrlError, match="bail de location"):
        set_reference(engine, loan, "2025-T2", actor_user_id=None, now=NOW)
    with pytest.raises(IrlError, match="positif"):
        set_index(engine, "2025-T2", 0, actor_user_id=None, now=NOW)
    with pytest.raises(IrlError, match="illisible"):
        set_index(engine, "deuxième trimestre", 14_000, actor_user_id=None, now=NOW)
    lease_id = _lease(engine)
    with pytest.raises(IrlError, match="Aucune révision"):
        apply_revision(engine, lease_id, today=date(2026, 10, 7), actor_user_id=None, now=NOW)
    set_reference(engine, lease_id, "", actor_user_id=None, now=NOW)  # retirée
    set_index(engine, "2025-T3", 14_000, actor_user_id=None, now=NOW)
    set_index(engine, "2025-T3", 14_100, actor_user_id=None, now=NOW)  # corrigé
    assert indices(engine) == {"2025-T3": 14_100}


def test_quarter_is_read_as_displayed() -> None:
    from app.rental.irl import check_quarter  # noqa: PLC0415

    assert check_quarter("T2 2026") == "2026-T2"
    assert check_quarter(" t3-2025 ") == "2025-T3"
    assert check_quarter("2026 T4") == "2026-T4"
    assert check_quarter("2026-T1") == "2026-T1"
