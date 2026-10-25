"""Outils d'analyse de l'IA : contenu d'un compte, flux, prévision, anomalies, bilan locatif,
vue d'ensemble (docs/spec-mcp.md §7)."""

import json
from datetime import date
from typing import Any

import pytest
from sqlalchemy import insert, update

import app.assistant.catalog  # noqa: F401 (enregistre les outils)
from app.assistant.context import overview
from app.assistant.tools import Context, ToolError, call_tool
from app.categorize.service import categorize_manually
from app.models import Account, TransferLink
from app.models.rental import ExpenseKind
from app.rental.expenses import link
from tests.assistant.conftest import NOW, Demo, add_tx

NAMES = ("ALICE", "MARTINEZ", "HUGO", "BLANCHARD")


def _call(context: Context, name: str, **arguments: Any) -> dict[str, Any]:
    result = call_tool(context, name, arguments)
    shown = json.dumps(result, ensure_ascii=False).upper()
    for word in NAMES:
        assert word not in shown, (name, word)
    return result


def test_account_detail_explains_its_content(context: Context, demo: Demo) -> None:
    with demo.engine.begin() as connection:
        connection.execute(
            update(Account)
            .where(Account.id == 1)
            .values(description="Compte courant principal, salaire et loyer de Alice Martinez")
        )
    categorize_manually(
        demo.engine,
        demo.tx["uber_aout"],
        [(demo.category("Restaurants"), -2390)],
        actor_user_id=None,
        now=NOW,
    )
    detail = _call(context, "detail_compte", compte="Courant Bourso", mois=3)
    assert detail["compte"]["role"] == "Compte courant principal, salaire et loyer de A. M."
    assert [m["mois"] for m in detail["mois"]] == ["2026-07", "2026-08", "2026-09"]
    august = detail["mois"][1]
    assert (august["entrees"]["centimes"], august["sorties"]["centimes"]) == (250000, -12039)
    assert detail["categories_depenses"][0] == {
        "categorie": "Alimentation › Restaurants",
        "montant": {"centimes": -2390, "eur": detail["categories_depenses"][0]["montant"]["eur"]},
    }
    assert detail["principales_recettes"][0]["libelle"].startswith("VIR SEPA")
    assert detail["non_categorisees"] == detail["operations"] - 1
    joint = _call(context, "detail_compte", compte="Joint A.")  # consultation seule : visible
    assert joint["compte"]["statut"] == "consultation seule" and joint["operations"] == 1


def test_flows_between_accounts(context: Context, demo: Demo) -> None:
    debit = add_tx(demo.engine, 1, date(2026, 9, 12), -50000, "VIR VERS COMPTE JOINT")
    credit = add_tx(demo.engine, 2, date(2026, 9, 12), 50000, "VIR DEPUIS COMPTE COURANT")
    with demo.engine.begin() as connection:
        connection.execute(
            insert(TransferLink).values(
                debit_tx_id=debit,
                credit_tx_id=credit,
                method="manual",
                status="confirmed",
                created_at=NOW,
            )
        )
    flows = _call(context, "flux_entre_comptes")["flux"]
    assert flows == [
        {"de": "Courant Bourso", "vers": "Joint A.", "total": flows[0]["total"], "virements": 1}
    ]
    assert flows[0]["total"]["centimes"] == 50000
    detail = _call(context, "detail_compte", compte="Courant Bourso")
    assert detail["virements_internes"][0]["sens"] == "envoyé vers"


def test_cash_flow_forecast(context: Context) -> None:
    forecast = _call(context, "prevision_tresorerie", jours=45)
    labels = [e["libelle"] for e in forecast["evenements"]]
    assert any("NETFLIX" in label for label in labels)  # abonnement mensuel attendu
    assert any(label == "Bail M." for label in labels)  # loyer attendu, pas encore récurrent
    netflix = [e for e in forecast["evenements"] if "NETFLIX" in e["libelle"]]
    assert netflix[0]["date"] == "2026-10-15" and netflix[0]["montant"]["centimes"] == -1399
    assert forecast["point_bas"]["montant"]["centimes"] <= forecast["solde_actuel"]["centimes"]


def test_anomalies(context: Context, demo: Demo) -> None:
    for month in (6, 7, 8):
        add_tx(demo.engine, 1, date(2026, month, 2), -4500, f"CARTE 02/0{month}/26 SALLE DE SPORT")
    unusual = add_tx(demo.engine, 1, date(2026, 9, 2), -15000, "CARTE 02/09/26 SALLE DE SPORT")
    add_tx(demo.engine, 1, date(2026, 9, 5), -890, "FRAIS TENUE DE COMPTE")
    add_tx(demo.engine, 1, date(2026, 9, 7), -25000, "CARTE 07/09/26 BIJOUTERIE NOUVELLE")
    for _ in range(2):
        add_tx(demo.engine, 1, date(2026, 9, 9), -1200, "CARTE 09/09/26 BOULANGERIE")
    found = _call(context, "anomalies")["anomalies"]
    kinds = {a["type"] for a in found}
    assert kinds == {
        "montant inhabituel",
        "frais bancaires",
        "nouveau commerçant",
        "doublon possible",
    }
    assert any(
        a["type"] == "montant inhabituel" and a["montant"]["centimes"] == -15000 for a in found
    )
    assert unusual  # identifiant utilisé pour la lisibilité du scénario


def test_rental_year(context: Context, demo: Demo) -> None:
    from sqlalchemy import select  # noqa: PLC0415

    from app.models import Property  # noqa: PLC0415

    with demo.engine.connect() as connection:
        bien = int(connection.scalar(select(Property.id)) or 0)
    link(
        demo.engine,
        bien,
        demo.tx["netflix_sept"],
        kind=ExpenseKind.OTHER,
        actor_user_id=None,
        now=NOW,
    )
    result = _call(context, "bilan_locatif", annee=2026)
    (lease,) = result["baux"]
    assert (lease["bail"], lease["du"]["centimes"], lease["recu"]["centimes"]) == (
        "Bail M.",
        85000,
        80000,
    )
    (prop,) = result["biens"]
    assert prop["bien"] == "Studio M." and prop["total_charges"]["centimes"] == -1399
    assert prop["resultat"]["centimes"] == 80000 - 1399
    empty = _call(context, "bilan_locatif", annee=2020)
    assert empty["baux"][0]["du"]["centimes"] == 0


def test_overview_is_pseudonymized_and_complete(context: Context, demo: Demo) -> None:
    """Un nom tapé dans la description d'un compte est abrégé s'il est dans la liste des noms
    (page Sécurité) ; la garde de sortie le rattrape alors partout."""
    from dataclasses import replace  # noqa: PLC0415

    from app.assistant.terms import add_term, load_pseudonymizer  # noqa: PLC0415

    add_term(demo.engine, "personne", "Hugo Blanchard", actor_user_id=demo.user_id, now=NOW)
    context = replace(context, masker=load_pseudonymizer(demo.engine))
    with demo.engine.begin() as connection:
        connection.execute(
            update(Account)
            .where(Account.id == 2)
            .values(description="Compte joint avec Hugo Blanchard")
        )
    text = overview(context)
    for section in ("## Comptes", "## Flux des comptes comptés", "## Baux", "## Divers"):
        assert section in text
    assert "Compte joint avec H. B." in text and "consultation seule" in text
    assert not any(word in text.upper() for word in NAMES)
    assert _call(context, "vue_ensemble")["resume"].startswith("Aujourd'hui : 2026-09-20")


def test_unknown_account_is_refused(context: Context) -> None:
    with pytest.raises(ToolError, match="Compte inconnu"):
        call_tool(context, "detail_compte", {"compte": "Gestion pilotée"})  # ignoré : invisible
