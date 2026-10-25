"""Suivi à partir d'une date et modification d'un bail (LOC-02, LOC-03, LOC-05).

Cas réel du 30/09/2026 : bail commencé en septembre 2025, historique bancaire disponible
seulement depuis juillet 2026 (DSP2 : 90 jours). Sans date de suivi, les échéances 2025 non
vérifiables passaient pour impayées et absorbaient les virements récents (« au plus ancien
d'abord »).
"""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import Engine, insert, select

from app.models import Alert, LedgerTransaction, RentAllocation, RentDue
from app.models.alerts import AlertStatus
from app.models.rental import LeaseKind, PartyRole
from app.rental.allocations import allocate_manually
from app.rental.engine import run_rental
from app.rental.leases import (
    LeaseChanges,
    LeaseError,
    LeaseTerms,
    UntrackedDues,
    add_party,
    create_lease,
    create_property,
    untracked_dues,
    update_lease,
)

NOW = datetime(2026, 9, 20, 10, 0, tzinfo=UTC)
_ids = iter(range(1, 100_000))


def _tx(engine: Engine, amount: int, day: date, label: str = "VIR DE ALICE EXEMPLE") -> int:
    tx_id = next(_ids)
    with engine.begin() as connection:
        connection.execute(
            insert(LedgerTransaction).values(
                id=tx_id, account_id=1, dedupe_key=f"ext:t:{tx_id}", fingerprint="x",
                occurrence=1, booking_date=day, amount_cents=amount, currency="EUR",
                label_raw=label, label_clean=label, created_at=NOW,
            )
        )  # fmt: skip
    return tx_id


def _lease(engine: Engine, tracking_start: date | None = None) -> int:
    bien = create_property(
        engine, name="Bien", address=None, furnished=True, shares=[("moi", 10000)], now=NOW
    )
    lease = create_lease(
        engine,
        LeaseTerms(
            kind=LeaseKind.RENT,
            label="Studio",
            property_id=bien,
            account_id=1,
            start_date=date(2025, 9, 1),
            tracking_start=tracking_start,
            rent_cents=80000,
            charges_cents=5000,
        ),
        now=NOW,
    )
    add_party(engine, lease, role=PartyRole.TENANT, name="Alice Exemple")
    return lease


def _periods(engine: Engine) -> dict[str, list[int]]:
    """échéance → montants confirmés."""
    with engine.connect() as connection:
        dues = connection.execute(select(RentDue.id, RentDue.period).order_by(RentDue.period)).all()
        return {
            period: list(
                connection.scalars(
                    select(RentAllocation.amount_cents).where(
                        RentAllocation.due_id == due_id, RentAllocation.status == "confirmed"
                    )
                )
            )
            for due_id, period in dues
        }


def _open_alerts(engine: Engine) -> list[str]:
    with engine.connect() as connection:
        return sorted(
            connection.scalars(select(Alert.type).where(Alert.status != AlertStatus.RESOLVED))
        )


def test_tracking_start_ignores_unverifiable_months(engine: Engine) -> None:
    _lease(engine, tracking_start=date(2026, 7, 1))
    for month in (7, 8, 9):
        _tx(engine, 85000, date(2026, month, 3))
    run_rental(engine, now=NOW)
    assert _periods(engine) == {"2026-07": [85000], "2026-08": [85000], "2026-09": [85000]}
    assert _open_alerts(engine) == []


def test_moving_tracking_start_cleans_phantom_dues_and_rematches(engine: Engine) -> None:
    """Sans date de suivi, le virement de juillet 2026 solde septembre 2025 ; en fixant le
    suivi à juillet 2026, les échéances fantômes et leurs alertes disparaissent et chaque
    virement retrouve son mois."""
    lease = _lease(engine)
    for month in (7, 8, 9):
        _tx(engine, 85000, date(2026, month, 3))
    run_rental(engine, now=NOW)
    periods = _periods(engine)
    assert periods["2025-09"] == [85000] and periods["2026-09"] == []
    assert "loyer_impaye" in _open_alerts(engine)
    update_lease(
        engine, lease, LeaseChanges(tracking_start=date(2026, 7, 1)), actor_user_id=None, now=NOW
    )
    run_rental(engine, now=NOW)
    assert _periods(engine) == {"2026-07": [85000], "2026-08": [85000], "2026-09": [85000]}
    assert _open_alerts(engine) == []


def test_new_amount_applies_from_a_month(engine: Engine) -> None:
    lease = _lease(engine, tracking_start=date(2026, 7, 1))
    run_rental(engine, now=NOW)
    update_lease(
        engine,
        lease,
        LeaseChanges(rent_cents=82000, charges_cents=5000, amounts_from="2026-09"),
        actor_user_id=None,
        now=NOW,
    )
    with engine.connect() as connection:
        amounts = dict(connection.execute(select(RentDue.period, RentDue.amount_cents)).all())
    assert amounts == {"2026-07": 85000, "2026-08": 85000, "2026-09": 87000}
    _tx(engine, 85000, date(2026, 7, 3))
    _tx(engine, 85000, date(2026, 8, 3))
    _tx(engine, 87000, date(2026, 9, 3))
    run_rental(engine, now=NOW)
    assert _periods(engine)["2026-09"] == [87000]


def test_changes_are_validated(engine: Engine) -> None:
    lease = _lease(engine, tracking_start=date(2026, 7, 1))
    cash = _tx(engine, 85000, date(2026, 7, 3), label="DEPOT ESPECES")
    run_rental(engine, now=NOW)
    with engine.connect() as connection:
        july = connection.scalars(select(RentDue.id).where(RentDue.period == "2026-07")).one()
    allocate_manually(
        engine,
        due_id=july,
        transaction_id=cash,
        amount_cents=None,
        note=None,
        actor_user_id=None,
        now=NOW,
    )
    with pytest.raises(LeaseError, match="à la main"):
        update_lease(
            engine,
            lease,
            LeaseChanges(tracking_start=date(2026, 8, 1)),
            actor_user_id=None,
            now=NOW,
        )
    with pytest.raises(LeaseError, match="dépasse"):
        update_lease(
            engine,
            lease,
            LeaseChanges(rent_cents=10000, charges_cents=0, amounts_from="2026-07"),
            actor_user_id=None,
            now=NOW,
        )
    with pytest.raises(LeaseError, match="Jours"):
        update_lease(engine, lease, LeaseChanges(alert_day=12), actor_user_id=None, now=NOW)
    with pytest.raises(LeaseError, match="introuvable"):
        update_lease(engine, 999, LeaseChanges(label="x"), actor_user_id=None, now=NOW)
    update_lease(engine, lease, LeaseChanges(label="Studio rénové"), actor_user_id=None, now=NOW)


def test_lease_without_tracking_start_is_flagged_and_fixed_in_one_step(engine: Engine) -> None:
    """Cas réel d'octobre 2026 : bail créé sans suivi, historique depuis le 30/06 ; le virement
    d'octobre partait sur septembre 2025 et octobre restait impayé."""
    lease = _lease(engine)
    _tx(engine, -1200, date(2026, 6, 30), label="CB BOULANGERIE")  # début de l'historique
    days = [date(2026, 7, 6), date(2026, 8, 5), date(2026, 9, 7), date(2026, 10, 5)]
    for day in days:
        _tx(engine, 85000, day)
    october = datetime(2026, 10, 6, 8, 0, tzinfo=UTC)
    run_rental(engine, now=october)
    assert _periods(engine)["2026-10"] == []  # le symptôme : rangé sur un mois de 2025
    assert untracked_dues(engine, lease) == UntrackedDues(
        10, "2025-09", "2026-06", suggested_start=date(2026, 7, 1)
    )
    update_lease(
        engine,
        lease,
        LeaseChanges(tracking_start=date(2026, 7, 1)),
        actor_user_id=None,
        now=october + timedelta(minutes=1),
    )
    run_rental(engine, now=october + timedelta(minutes=2))
    assert _periods(engine) == {f"2026-{m:02d}": [85000] for m in (7, 8, 9, 10)}
    assert untracked_dues(engine, lease) is None


def test_untracked_dues_needs_a_bank_history(engine: Engine) -> None:
    lease = _lease(engine)
    assert untracked_dues(engine, lease) is None  # aucune opération : rien à proposer
    assert untracked_dues(engine, 999) is None
