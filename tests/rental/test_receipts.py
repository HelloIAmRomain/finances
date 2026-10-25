"""Quittances de loyer (LOC-07, LEG-04) : après paiement complet, loyer et charges distingués,
rien n'est envoyé (inv. 6), téléchargement journalisé."""

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import Engine, insert, select

from app.models import AuditLog, LedgerTransaction, Property, RentDue
from app.models.rental import LeaseKind, PartyRole
from app.rental.allocations import allocate_manually
from app.rental.engine import run_rental
from app.rental.leases import (
    LeaseChanges,
    LeaseTerms,
    add_party,
    create_lease,
    create_property,
    update_lease,
)
from app.rental.receipts import (
    ReceiptError,
    issue,
    missing,
    receipt,
    render,
    set_details,
)

NOW = datetime(2026, 10, 7, 8, 0, tzinfo=UTC)
LANDLORD = "A. Démo et B. Exemple\n1 rue de l'Exemple, 75000 Paris"
ADDRESS = "Appartement 2\n3 avenue Fictive, 69000 Lyon"
_ids = iter(range(1, 100_000))


def _lease(engine: Engine, *, furnished: bool = True) -> tuple[int, int]:
    property_id = create_property(
        engine, name="Studio", address=None, furnished=furnished, shares=[("moi", 10000)], now=NOW
    )
    lease_id = create_lease(
        engine,
        LeaseTerms(
            kind=LeaseKind.RENT, label="Bail", account_id=1, start_date=date(2026, 9, 1),
            rent_cents=70_000, charges_cents=5_000, property_id=property_id,
        ),
        now=NOW,
    )  # fmt: skip
    run_rental(engine, now=NOW)
    return property_id, lease_id


def _due(engine: Engine, period: str = "2026-09") -> int:
    with engine.connect() as connection:
        return int(connection.scalars(select(RentDue.id).where(RentDue.period == period)).one())


def _pay(engine: Engine, due_id: int, amount: int, day: date) -> None:
    tx_id = next(_ids)
    with engine.begin() as connection:
        connection.execute(
            insert(LedgerTransaction).values(
                id=tx_id, account_id=1, dedupe_key=f"ext:r:{tx_id}", fingerprint="x",
                occurrence=1, booking_date=day, amount_cents=amount, currency="EUR",
                label_raw="VIREMENT LOYER", label_clean="VIREMENT LOYER", created_at=NOW,
            )
        )  # fmt: skip
    allocate_manually(
        engine,
        due_id=due_id,
        transaction_id=tx_id,
        amount_cents=amount,
        note=None,
        actor_user_id=None,
        now=NOW,
    )


def _ready(engine: Engine) -> tuple[int, int]:
    property_id, lease_id = _lease(engine)
    set_details(
        engine, property_id, landlord=LANDLORD, address=ADDRESS, actor_user_id=None, now=NOW
    )
    add_party(engine, lease_id, role=PartyRole.TENANT, name="C. Locataire")
    return property_id, lease_id


def test_receipt_after_full_payment_distinguishes_rent_and_charges(engine: Engine) -> None:
    _, lease_id = _ready(engine)
    due_id = _due(engine)
    _pay(engine, due_id, 40_000, date(2026, 9, 2))
    with pytest.raises(ReceiptError, match="pas encore entièrement payée"):
        receipt(engine, lease_id, due_id)  # paiement partiel : pas de quittance
    _pay(engine, due_id, 35_000, date(2026, 9, 6))
    found = receipt(engine, lease_id, due_id)
    assert (found.rent_cents, found.charges_cents, found.total_cents) == (70_000, 5_000, 75_000)
    assert found.paid_on == date(2026, 9, 6)  # dernier paiement
    assert (found.period_start, found.period_end) == (date(2026, 9, 1), date(2026, 9, 30))
    assert found.tenants == ("C. Locataire",)
    assert found.filename == "quittance-2026-09.pdf"
    pdf = render(found, date(2026, 10, 7))
    for text in (
        "Quittance de loyer",
        "Période du 01/09/2026 au 30/09/2026",
        "C. Locataire",
        "1 rue de l'Exemple, 75000 Paris",
        "3 avenue Fictive, 69000 Lyon",
        "Logement loué \\(location meublée\\)",
        "Loyer hors charges",
        "700,00\xa0€",
        "50,00\xa0€",
        "750,00\xa0€",
        "Établie le 07/10/2026.",
    ):
        assert text.encode("cp1252") in pdf, text


def test_issue_is_audited_and_nothing_is_sent(engine: Engine) -> None:
    _, lease_id = _ready(engine)
    due_id = _due(engine)
    _pay(engine, due_id, 75_000, date(2026, 9, 3))
    found, pdf = issue(
        engine, lease_id, due_id, issued_on=date(2026, 10, 7), actor_user_id=None, now=NOW
    )
    assert pdf.startswith(b"%PDF") and found.period == "2026-09"
    with engine.connect() as connection:
        details = connection.scalars(
            select(AuditLog.details).where(AuditLog.action == "receipt.issued")
        ).one()
    assert details == {"bail": lease_id, "periode": "2026-09", "total": 75_000}
    assert "Locataire" not in str(details)  # aucun nom dans le journal


def test_missing_information_is_explained(engine: Engine) -> None:
    property_id, lease_id = _lease(engine, furnished=False)
    due_id = _due(engine)
    _pay(engine, due_id, 75_000, date(2026, 9, 3))
    gaps = missing(engine, lease_id)
    assert gaps.landlord and gaps.address and gaps.tenant
    with pytest.raises(ReceiptError, match="nom et l'adresse du bailleur") as error:
        receipt(engine, lease_id, due_id)
    assert "adresse du logement" in str(error.value) and "locataire" in str(error.value)
    set_details(engine, property_id, landlord=LANDLORD, address="", actor_user_id=None, now=NOW)
    add_party(engine, lease_id, role=PartyRole.TENANT, name="C. Locataire")
    assert missing(engine, lease_id).address and not missing(engine, lease_id).landlord
    with pytest.raises(ReceiptError, match="adresse du logement"):
        receipt(engine, lease_id, due_id)
    set_details(
        engine, property_id, landlord=LANDLORD, address=ADDRESS, actor_user_id=None, now=NOW
    )
    assert not missing(engine, lease_id).any
    pdf = render(receipt(engine, lease_id, due_id), date(2026, 10, 7))
    assert b"Logement lou\xe9\n" not in pdf and b"(Logement lou\xe9)" in pdf  # non meublé


def test_details_are_encrypted_and_validated(engine: Engine) -> None:
    property_id, _ = _lease(engine)
    with pytest.raises(ReceiptError, match="trop long"):
        set_details(
            engine, property_id, landlord="x" * 401, address="", actor_user_id=None, now=NOW
        )
    with pytest.raises(ReceiptError, match="introuvable"):
        set_details(engine, 999, landlord="x", address="y", actor_user_id=None, now=NOW)
    set_details(
        engine, property_id, landlord=LANDLORD, address=ADDRESS, actor_user_id=None, now=NOW
    )
    with engine.connect() as connection:
        raw = connection.exec_driver_sql("SELECT landlord_enc FROM property").scalar_one()
        assert b"Exemple" not in raw  # chiffré en base
        assert connection.scalar(select(Property.landlord_enc)) == LANDLORD


def test_charges_follow_lease_changes_and_loans_are_refused(engine: Engine) -> None:
    _, lease_id = _ready(engine)
    update_lease(
        engine,
        lease_id,
        LeaseChanges(charges_cents=6_000, amounts_from="2026-10"),
        actor_user_id=None,
        now=NOW,
    )
    with engine.connect() as connection:
        dues = dict(connection.execute(select(RentDue.period, RentDue.charges_cents)).all())
    assert dues["2026-09"] == 5_000 and dues["2026-10"] == 6_000
    loan = create_lease(
        engine,
        LeaseTerms(
            kind=LeaseKind.LOAN, label="Prêt", account_id=1, start_date=date(2026, 9, 1),
            rent_cents=10_000,
        ),
        now=NOW,
    )  # fmt: skip
    run_rental(engine, now=NOW)
    with engine.connect() as connection:
        loan_due = connection.scalars(
            select(RentDue.id).where(RentDue.lease_id == loan).order_by(RentDue.period)
        ).first()
    assert loan_due is not None
    with pytest.raises(ReceiptError, match="loyer d'un bien"):
        receipt(engine, loan, loan_due)
    with pytest.raises(ReceiptError, match="introuvable"):
        receipt(engine, lease_id, 999_999)
