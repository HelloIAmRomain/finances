"""Décisions manuelles du rapprochement (LOC-06, L-05, L-06), validation des baux (LOC-02) et
exclusions du moteur (compte ignoré, doublon confirmé)."""

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import Engine, func, insert, select, update

from app.alerts.engine import raise_alert
from app.models import (
    Account,
    Alert,
    AuditLog,
    DuplicateLink,
    LedgerTransaction,
    Overpayment,
    Party,
    RentAllocation,
    RentDue,
    RentIncident,
)
from app.models.ledger import DuplicateReason, DuplicateStatus, LinkMode
from app.models.rental import IncidentKind, LeaseKind, OverpaymentDecision, PartyRole
from app.rental.allocations import (
    AllocationError,
    allocate_manually,
    confirm_proposal,
    decide_overpayment,
    dissociate,
    reject_proposal,
)
from app.rental.engine import run_rental
from app.rental.leases import (
    LeaseError,
    LeaseTerms,
    add_party,
    create_lease,
    create_property,
    end_lease,
    normalize_label,
)
from tests.factories import fake_iban

NOW = datetime(2026, 9, 2, 10, 0, tzinfo=UTC)
_ids = iter(range(1, 100_000))


def _lease(engine: Engine, kind: LeaseKind = LeaseKind.LOAN) -> int:
    lease_id = create_lease(
        engine,
        LeaseTerms(
            kind=kind,
            label="Prêt de test",
            account_id=1,
            start_date=date(2026, 9, 1),
            rent_cents=20000,
        ),
        now=NOW,
    )
    add_party(engine, lease_id, role=PartyRole.BORROWER, name="Chloé Démo")
    return lease_id


def _tx(
    engine: Engine, amount: int, label: str, day: date = date(2026, 9, 2), account: int = 1
) -> int:
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


def _allocations(engine: Engine) -> list[tuple[int, int, str]]:
    with engine.connect() as connection:
        return [
            (row.transaction_id, row.amount_cents, str(row.status))
            for row in connection.execute(select(RentAllocation).order_by(RentAllocation.id))
        ]


def _due_id(engine: Engine, period: str = "2026-09") -> int:
    with engine.connect() as connection:
        return int(connection.scalars(select(RentDue.id).where(RentDue.period == period)).one())


def test_normalize_label_and_patterns() -> None:
    assert normalize_label("Vir. de M. Élie-Dürr  ") == "VIR DE M ELIE DURR"


def test_party_name_is_a_pattern_and_personal_data_is_encrypted(engine: Engine) -> None:
    lease_id = _lease(engine)
    tx = _tx(engine, 20000, "VIR SEPA CHLOE DEMO")
    run_rental(engine, now=NOW)
    assert _allocations(engine) == [(tx, 20000, "confirmed")]
    with engine.connect() as connection:
        raw: bytes = connection.exec_driver_sql("SELECT name_enc FROM party").scalar_one()
        assert b"Chlo" not in raw
        assert connection.scalar(select(Party.lease_id)) == lease_id


def test_lease_validation(engine: Engine) -> None:
    good = LeaseTerms(
        kind=LeaseKind.LOAN, label="x", account_id=1, start_date=date(2026, 9, 1), rent_cents=1
    )
    for wrong, message in (
        (LeaseTerms(**{**good.__dict__, "label": " "}), "nom"),
        (LeaseTerms(**{**good.__dict__, "kind": LeaseKind.RENT}), "bien"),
        (LeaseTerms(**{**good.__dict__, "rent_cents": 0}), "Montants"),
        (LeaseTerms(**{**good.__dict__, "alert_day": 12}), "Jours"),
        (LeaseTerms(**{**good.__dict__, "due_day": 6}), "Jours"),
        (LeaseTerms(**{**good.__dict__, "end_date": date(2026, 8, 1)}), "fin"),
        (LeaseTerms(**{**good.__dict__, "account_id": 99}), "introuvable"),
    ):
        with pytest.raises(LeaseError, match=message):
            create_lease(engine, wrong, now=NOW)
    with pytest.raises(LeaseError, match="100 %"):
        create_property(
            engine, name="Bien", address=None, furnished=True, shares=[("moi", 4000)], now=NOW
        )
    lease_id = create_lease(engine, good, now=NOW)
    wrong_iban = fake_iban(1)[:2] + ("00" if fake_iban(1)[2:4] != "00" else "01") + fake_iban(1)[4:]
    with pytest.raises(LeaseError, match="IBAN"):
        add_party(engine, lease_id, role=PartyRole.TENANT, name="X", iban=wrong_iban)
    with pytest.raises(LeaseError, match="Bail introuvable"):
        add_party(engine, 999, role=PartyRole.TENANT, name="X")
    with pytest.raises(LeaseError, match="fin"):
        end_lease(engine, lease_id, end_date=date(2026, 1, 1), now=NOW, actor_user_id=None)
    end_lease(engine, lease_id, end_date=date(2026, 12, 31), now=NOW, actor_user_id=None)
    with engine.connect() as connection:
        actions = set(connection.scalars(select(AuditLog.action)))
    assert {"lease.created", "lease.ended"} <= actions


def test_proposal_can_be_rejected_and_is_not_proposed_again(engine: Engine) -> None:
    _lease(engine)
    tx = _tx(engine, 20000, "VIR SEPA INCONNU")
    run_rental(engine, now=NOW)
    [(_, _, status)] = _allocations(engine)
    assert status == "proposed"
    with engine.connect() as connection:
        proposal = int(connection.scalar(select(RentAllocation.id)) or 0)
    reject_proposal(engine, proposal, actor_user_id=None, now=NOW)
    run_rental(engine, now=NOW)
    assert _allocations(engine) == [(tx, 20000, "rejected")]
    with pytest.raises(AllocationError):
        reject_proposal(engine, proposal, actor_user_id=None, now=NOW)
    with pytest.raises(AllocationError):
        confirm_proposal(engine, proposal, actor_user_id=None, now=NOW)


def test_manual_allocation_dissociation_and_limits(engine: Engine) -> None:
    _lease(engine)
    run_rental(engine, now=NOW)
    due = _due_id(engine)
    cash = _tx(engine, 15000, "DEPOT ESPECES")
    debit = _tx(engine, -5000, "PRLV")
    with pytest.raises(AllocationError, match="crédit"):
        allocate_manually(
            engine,
            due_id=due,
            transaction_id=debit,
            amount_cents=None,
            note=None,
            actor_user_id=None,
            now=NOW,
        )
    with pytest.raises(AllocationError, match="trop élevé"):
        allocate_manually(
            engine,
            due_id=due,
            transaction_id=cash,
            amount_cents=16000,
            note=None,
            actor_user_id=None,
            now=NOW,
        )
    allocation = allocate_manually(
        engine,
        due_id=due,
        transaction_id=cash,
        amount_cents=None,
        note="dépôt en espèces",
        actor_user_id=None,
        now=NOW,
    )
    assert _allocations(engine) == [(cash, 15000, "confirmed")]
    run_rental(engine, now=NOW)  # le moteur ne touche pas à une affectation manuelle
    assert _allocations(engine) == [(cash, 15000, "confirmed")]
    dissociate(engine, allocation, note="erreur", actor_user_id=None, now=NOW)
    run_rental(engine, now=NOW)  # ni ne la repropose
    assert _allocations(engine) == [(cash, 15000, "rejected")]
    with pytest.raises(AllocationError):
        dissociate(engine, allocation, note=None, actor_user_id=None, now=NOW)


def test_overpayment_carried_forward_or_refunded(engine: Engine) -> None:
    _lease(engine)
    _tx(engine, 25000, "VIR DE CHLOE DEMO")
    run_rental(engine, now=NOW)
    with engine.connect() as connection:
        over = int(connection.scalar(select(Overpayment.id)) or 0)
    with pytest.raises(AllocationError, match="Aucune échéance ouverte"):
        decide_overpayment(
            engine, over, OverpaymentDecision.CARRY_FORWARD, actor_user_id=None, now=NOW
        )
    october = datetime(2026, 9, 26, 10, 0, tzinfo=UTC)
    run_rental(engine, now=october)  # l'échéance d'octobre s'ouvre le 25
    decide_overpayment(
        engine, over, OverpaymentDecision.CARRY_FORWARD, actor_user_id=None, now=october
    )
    with engine.connect() as connection:
        carried = connection.execute(
            select(RentAllocation.amount_cents, RentDue.period)
            .join(RentDue, RentDue.id == RentAllocation.due_id)
            .where(RentAllocation.reason == "report")
        ).one()
    assert tuple(carried) == (5000, "2026-10")
    with pytest.raises(AllocationError, match="déjà traité"):
        decide_overpayment(engine, over, OverpaymentDecision.REFUND, actor_user_id=None, now=NOW)
    with pytest.raises(AllocationError, match="report ou remboursement"):
        decide_overpayment(engine, over, OverpaymentDecision.PENDING, actor_user_id=None, now=NOW)


def test_ignored_account_and_confirmed_duplicates_are_left_out(engine: Engine) -> None:
    _lease(engine)
    original = _tx(engine, 20000, "VIR DE CHLOE DEMO")
    copy = _tx(engine, 20000, "VIR DE CHLOE DEMO")
    with engine.begin() as connection:
        connection.execute(
            insert(DuplicateLink).values(
                canonical_tx_id=original,
                duplicate_tx_id=copy,
                mode=LinkMode.AUTO,
                reason=DuplicateReason.EXACT,
                status=DuplicateStatus.CONFIRMED,
                created_at=NOW,
            )
        )
    run_rental(engine, now=NOW)
    assert _allocations(engine) == [(original, 20000, "confirmed")]  # pas de trop-perçu fantôme
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Overpayment)) == 0
    with engine.begin() as connection:
        connection.execute(update(Account).where(Account.id == 1).values(ignored_at=NOW))
    late = _tx(engine, 20000, "VIR DE CHLOE DEMO", day=date(2026, 9, 3))
    run_rental(engine, now=NOW)
    assert all(tx != late for tx, _, _ in _allocations(engine))


def test_alert_titles_carry_no_amount_nor_name(engine: Engine) -> None:
    _lease(engine)
    _tx(engine, 25000, "VIR DE CHLOE DEMO")
    _tx(engine, 20000, "VIR INCONNU")
    run_rental(engine, now=datetime(2026, 11, 20, 10, 0, tzinfo=UTC))
    with engine.connect() as connection:
        titles = list(connection.scalars(select(Alert.title)))
    assert titles and all("€" not in t and "Chlo" not in t and "CHLOE" not in t for t in titles)


def test_allocated_payment_later_found_duplicate_is_not_counted_twice(engine: Engine) -> None:
    """L-12 : le virement affecté (API) est ensuite reconnu comme doublon d'une ligne de relevé
    importée (canonique) : l'échéance reste payée une fois, sans trop-perçu, et l'annulation du
    lien de doublon ne change rien au rapprochement."""
    _lease(engine)
    api = _tx(engine, 20000, "VIR DE CHLOE DEMO")
    run_rental(engine, now=NOW)
    csv = _tx(engine, 20000, "VIR CHLOE DEMO (RELEVE)")
    with engine.begin() as connection:
        connection.execute(
            insert(DuplicateLink).values(
                canonical_tx_id=csv,
                duplicate_tx_id=api,
                mode=LinkMode.AUTO,
                reason=DuplicateReason.EXACT,
                status=DuplicateStatus.CONFIRMED,
                created_at=NOW,
            )
        )
    for _ in range(2):
        run_rental(engine, now=NOW)
    assert _allocations(engine) == [(api, 20000, "confirmed")]
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Overpayment)) == 0


def test_rejection_survives_a_stop_before_its_alert(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Arrêt juste après l'annulation d'un paiement rejeté : l'incident est écrit avec elle et
    l'alerte part au passage suivant (elle était perdue : le rejet passait pour traité)."""
    import app.rental.engine as rental_engine  # noqa: PLC0415

    _lease(engine)
    _tx(engine, 20000, "VIR DE CHLOE DEMO")
    run_rental(engine, now=NOW)
    _tx(engine, -20000, "RETOUR VIR IMPAYE CHLOE DEMO", day=date(2026, 9, 4))
    real = raise_alert

    def crash(*args: object, **kwargs: object) -> None:
        raise RuntimeError("arrêt du worker")

    monkeypatch.setattr(rental_engine, "raise_alert", crash)
    with pytest.raises(RuntimeError):
        run_rental(engine, now=NOW)
    monkeypatch.setattr(rental_engine, "raise_alert", real)
    for _ in range(2):
        run_rental(engine, now=NOW)
    with engine.connect() as connection:
        incidents = connection.scalar(
            select(func.count()).where(RentIncident.kind == IncidentKind.REJECTED)
        )
        alerts = connection.scalar(select(func.count()).where(Alert.type == "loyer_rejete"))
    assert (incidents, alerts) == (1, 1)
