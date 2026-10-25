"""Scénarios de loyer en YAML (tests/scenarios/loyers), joués de bout en bout sur la base :
génération des échéances, rapprochement, rejets, états, incidents et alertes (CDC §5 et §9).

Format d'un scénario : `bail` et `payeurs` facultatifs (bail par défaut : 800 € + 50 € de
charges au 1er du mois, sur le compte 1, locataire et caution fictifs) ; `etapes` datées avec des
`operations` (montant en centimes, libellé, `iban` = numéro d'IBAN fictif, `compte`, `reference`),
des `actions` (`confirmer` : confirme les propositions), puis ce qui est `attendu`.
"""

from datetime import date, datetime, time
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy import Engine, func, insert, select

from app.crypto import blind_index
from app.iban import normalize_iban
from app.models import Alert, LedgerTransaction, Overpayment, RentAllocation, RentDue, RentIncident
from app.models.alerts import AlertStatus
from app.models.rental import AllocationStatus, LeaseKind, OverpaymentDecision, PartyRole
from app.rental.allocations import confirm_proposal
from app.rental.engine import run_rental
from app.rental.leases import LeaseTerms, add_party, create_lease, create_property
from app.rental.status import DueTerms, compute_status
from app.timeutil import PARIS
from tests.factories import fake_iban

SCENARIOS = sorted((Path(__file__).parents[1] / "scenarios" / "loyers").glob("*.yaml"))
DEFAULT_LEASE = {"type": "rent", "loyer": 80000, "charges": 5000}
DEFAULT_PAYERS = [
    {"role": "tenant", "nom": "Alice Exemple", "iban": 1, "motifs": ["ALICE EXEMPLE"]},
    {"role": "guarantor", "nom": "Bruno Fictif", "motifs": ["BRUNO FICTIF"]},
]


def _noon(day: date) -> datetime:
    return datetime.combine(day, time(12), tzinfo=PARIS)


def _setup(engine: Engine, scenario: dict[str, Any]) -> int:
    lease = {**DEFAULT_LEASE, **scenario.get("bail", {})}
    kind = LeaseKind(lease["type"])
    property_id = (
        create_property(
            engine,
            name="Bien de test",
            address=None,
            furnished=True,
            shares=[("moi", 5000), ("co-indivisaire", 5000)],
            now=_noon(date(2026, 8, 1)),
        )
        if kind is LeaseKind.RENT
        else None
    )
    lease_id = create_lease(
        engine,
        LeaseTerms(
            kind=kind,
            label="Bail de test",
            property_id=property_id,
            account_id=1,
            start_date=date(2026, 9, 1),
            rent_cents=lease["loyer"],
            charges_cents=lease["charges"],
            end_date=date.fromisoformat(str(lease["fin"])) if "fin" in lease else None,
        ),
        now=_noon(date(2026, 8, 1)),
    )
    for payer in scenario.get("payeurs", DEFAULT_PAYERS):
        add_party(
            engine,
            lease_id,
            role=PartyRole(payer["role"]),
            name=payer["nom"],
            iban=fake_iban(payer["iban"]) if "iban" in payer else None,
            patterns=payer.get("motifs", []),
        )
    return lease_id


def _insert(
    engine: Engine, day: date, operations: list[dict[str, Any]], counter: list[int]
) -> None:
    with engine.begin() as connection:
        for operation in operations:
            counter[0] += 1
            iban = fake_iban(operation["iban"]) if "iban" in operation else None
            connection.execute(
                insert(LedgerTransaction).values(
                    id=counter[0],
                    account_id=operation.get("compte", 1),
                    dedupe_key=f"ext:t:{counter[0]}",
                    external_id=operation.get("reference"),
                    fingerprint="x",
                    occurrence=1,
                    booking_date=date.fromisoformat(str(operation.get("date", day))),
                    amount_cents=operation["montant"],
                    currency="EUR",
                    counterparty_iban_enc=iban,
                    counterparty_iban_hash=(
                        blind_index(normalize_iban(iban), context="iban") if iban else None
                    ),
                    label_raw=operation["libelle"],
                    label_clean=operation["libelle"],
                    created_at=_noon(day),
                )
            )


def _observed(engine: Engine, day: date) -> dict[str, Any]:
    with engine.connect() as connection:
        dues = connection.execute(select(RentDue)).all()
        states, remaining = {}, {}
        for due in dues:
            confirmed = connection.scalars(
                select(RentAllocation.amount_cents).where(
                    RentAllocation.due_id == due.id,
                    RentAllocation.status == AllocationStatus.CONFIRMED,
                )
            ).all()
            terms = DueTerms(due.period, due.amount_cents, alert_day=5, unpaid_day=10)
            status = compute_status(terms, confirmed, day)
            states[due.period], remaining[due.period] = str(status.state), status.remaining
        alerts = sorted(
            f"{kind}:{severity}"
            for kind, severity in connection.execute(
                select(Alert.type, Alert.severity).where(Alert.status != AlertStatus.RESOLVED)
            )
        )
        proposals = connection.scalar(
            select(func.count()).where(RentAllocation.status == AllocationStatus.PROPOSED)
        )
        incidents = sorted(str(kind) for kind in connection.scalars(select(RentIncident.kind)))
        overpaid = connection.scalar(
            select(func.coalesce(func.sum(Overpayment.amount_cents), 0)).where(
                Overpayment.decision == OverpaymentDecision.PENDING
            )
        )
    return {
        "echeances": states,
        "reste_du": remaining,
        "alertes": alerts,
        "propositions": proposals,
        "incidents": incidents,
        "trop_percu": overpaid,
    }


@pytest.mark.parametrize("path", SCENARIOS, ids=[p.stem for p in SCENARIOS])
def test_scenario(engine: Engine, path: Path) -> None:
    scenario = yaml.safe_load(path.read_text(encoding="utf-8"))
    _setup(engine, scenario)
    counter = [0]
    for step in scenario["etapes"]:
        day = date.fromisoformat(str(step["date"]))
        _insert(engine, day, step.get("operations", []), counter)
        for action in step.get("actions", []):
            assert action == "confirmer"
            with engine.connect() as connection:
                pending = connection.scalars(
                    select(RentAllocation.id).where(
                        RentAllocation.status == AllocationStatus.PROPOSED
                    )
                ).all()
            for allocation_id in pending:
                confirm_proposal(engine, allocation_id, actor_user_id=None, now=_noon(day))
        run_rental(engine, now=_noon(day))
        run_rental(engine, now=_noon(day))  # rejouable sans effet (inv. 3)
        observed = _observed(engine, day)
        for key, raw in step.get("attendu", {}).items():
            actual = observed[key]
            expected = raw
            if key in ("echeances", "reste_du"):
                expected = {str(period): value for period, value in raw.items()}
                actual = {period: actual.get(period) for period in expected}
            elif isinstance(raw, list):
                expected = sorted(raw)
            assert actual == expected, f"{path.stem}, le {day} : {key}"
