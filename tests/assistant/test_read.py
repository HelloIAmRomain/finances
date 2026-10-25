"""Outils de lecture de l'IA (docs/spec-mcp.md §6-7) sur la base de démonstration."""

import json
from datetime import date
from typing import Any

import pytest

import app.assistant.catalog  # noqa: F401 (enregistre les outils)
from app.assistant.tools import TOOLS, Context, ToolError, call_tool, public_id
from app.categorize.service import categorize_manually
from tests.assistant.conftest import NOW, Demo

NAMES = ("ALICE", "MARTINEZ", "HUGO", "BLANCHARD")


def _call(context: Context, name: str, **arguments: Any) -> dict[str, Any]:
    result = call_tool(context, name, arguments)
    shown = json.dumps(result, ensure_ascii=False).upper()
    for word in NAMES:  # jamais un nom de personne en toutes lettres (inv. 8)
        assert word not in shown, (name, word)
    return result


def test_every_read_tool_answers_with_defaults(context: Context) -> None:
    read_tools = [t for t in TOOLS.values() if not t.write]
    assert len(read_tools) == 20  # 14 de base, vue d'ensemble et 5 outils d'analyse
    for found in read_tools:
        if found.name in ("transaction", "evolution_categorie", "detail_compte", "bilan_locatif"):
            continue
        _call(context, found.name)


def test_accounts_hide_ignored_and_show_status(context: Context) -> None:
    rows = _call(context, "comptes")["comptes"]
    assert [(r["nom"], r["statut"]) for r in rows] == [
        ("Carte", "carte (répète le compte courant)"),
        ("Courant Bourso", "compté"),
        ("Joint A.", "consultation seule"),
    ]
    assert rows[1]["historique_depuis"] == "2026-07-15"


def test_monthly_summary_counts_only_counted_accounts(context: Context) -> None:
    months = _call(context, "synthese_mensuelle", du="2026-07", au="2026-09")["mois"]
    assert [(m["mois"], m["revenus"]["centimes"], m["depenses"]["centimes"]) for m in months] == [
        ("2026-07", 0, -1399),
        ("2026-08", 250000, -12039),
        ("2026-09", 83000, -15369),
    ]
    assert months[1]["taux_epargne_pour_mille"] == 952


def test_categories_and_comparison(context: Context, demo: Demo) -> None:
    restaurants = demo.category("Restaurants")
    for key, amount in (("uber_aout", -2390), ("uber_sept", -1850)):
        categorize_manually(
            demo.engine, demo.tx[key], [(restaurants, amount)], actor_user_id=None, now=NOW
        )
    result = _call(context, "depenses_par_categorie", du="2026-09", niveau="detail")
    rows = {r["categorie"]: r for r in result["categories"]}
    assert rows["Alimentation › Restaurants"]["montant"]["centimes"] == -1850
    assert rows["Alimentation › Restaurants"]["comparaison"]["centimes"] == -2390
    assert rows["Alimentation › Restaurants"]["ecart"]["centimes"] == 540
    assert result["comparaison"] == {"du": "2026-08", "au": "2026-08"}
    trend = _call(
        context, "evolution_categorie", categorie="alimentation", du="2026-07", au="2026-09"
    )
    assert [m["montant"]["centimes"] for m in trend["mois"]] == [0, -2390, -1850]


def test_top_items_group_by_merchant(context: Context) -> None:
    top = _call(context, "principaux_postes", du="2026-08", au="2026-09")["postes"]
    assert top[0]["libelle"] == "CARTE CARREFOUR CITY"
    assert (top[0]["total"]["centimes"], top[0]["operations"]) == (-17370, 2)


def test_search_excludes_cards_and_masks_labels(context: Context) -> None:
    result = _call(context, "rechercher_transactions")
    assert result["total"] == 12 and not result["tronque"]
    labels = {t["libelle"] for t in result["transactions"]}
    assert "VIR INST MME A. M. LOYER SEPT" in labels
    assert "VIR INST M H. B. R." in labels
    assert all(t["id"].startswith("t_") for t in result["transactions"])
    assert _call(context, "rechercher_transactions", inclure_cartes=True)["total"] == 13
    uber = _call(context, "rechercher_transactions", texte="uber", tri="montant_asc")
    assert [t["montant"]["centimes"] for t in uber["transactions"]] == [-2390, -1850]
    two = _call(context, "rechercher_transactions", limite=2)
    assert two["tronque"] and len(two["transactions"]) == 2
    assert (
        _call(context, "rechercher_transactions", non_categorisees=True, compte="Joint A.")["total"]
        == 1
    )


def test_transaction_detail_shows_rent_allocation(context: Context, demo: Demo) -> None:
    rows = _call(context, "rechercher_transactions", texte="LOYER")["transactions"]
    detail = _call(context, "transaction", id=rows[0]["id"])
    assert detail["loyers"][0]["periode"] == "2026-09"
    with pytest.raises(ToolError, match="invalide"):
        call_tool(context, "transaction", {"id": "t_123"})


def test_recurring_detects_monthly_subscription(context: Context) -> None:
    found = _call(context, "recurrents")["recurrents"]
    netflix = next(r for r in found if "NETFLIX" in r["libelle"])
    assert netflix["frequence"] == "mensuelle"
    assert netflix["cout_annuel_estime"]["centimes"] == -1399 * 12
    assert netflix["prochaine_date_estimee"] == "2026-10-15"


def test_rents_use_abbreviated_names(context: Context) -> None:
    result = _call(context, "loyers", du="2026-07", au="2026-09")
    lease = result["baux"][0]
    assert (lease["nom"], lease["bien"]) == ("Bail M.", "Studio M.")
    # Virement de 800 € pour 850 € dus, reconnu par le motif : reste 50 € après le 10.
    (due,) = lease["echeances"]
    assert (due["periode"], due["statut"], due["reste"]["centimes"]) == ("2026-09", "impayé", 5000)
    assert due["paiements"][0]["methode"] == "auto"


def test_parameters_are_validated_and_bounded(context: Context) -> None:
    for name, arguments, message in (
        ("synthese_mensuelle", {"du": "2026-13"}, "Paramètres invalides"),
        ("synthese_mensuelle", {"du": "2020-01", "au": "2026-09"}, "trop longue"),
        ("rechercher_transactions", {"limite": 500}, "Paramètres invalides"),
        ("rechercher_transactions", {"inconnu": 1}, "Paramètres invalides"),
        ("rechercher_transactions", {"categorie": "Nimporte"}, "Catégorie inconnue"),
        ("rechercher_transactions", {"compte": "c_x"}, "Compte inconnu"),
        ("outil_absent", {}, "Outil inconnu"),
    ):
        with pytest.raises(ToolError, match=message):
            call_tool(context, name, arguments)


def test_prompts_render_and_validate() -> None:
    from app.assistant.prompts import PROMPTS, render_prompt  # noqa: PLC0415

    assert len(PROMPTS) == 6
    month = render_prompt("bilan_du_mois", {})
    assert month is not None and "le mois dernier" in month["messages"][0]["content"]["text"]
    assert render_prompt("bilan_du_mois", {"mois": "2026-13"}) is None
    assert render_prompt("bilan_du_mois", {"mois": 202608}) is None
    assert render_prompt("comparer_deux_mois", {"mois_a": "2026-07"}) is None
    both = render_prompt("comparer_deux_mois", {"mois_a": "2026-07", "mois_b": "2026-08"})
    assert both is not None and "2026-07" in both["messages"][0]["content"]["text"]
    assert render_prompt("inconnue", {}) is None


def test_rules_view_shows_every_condition(context: Context, demo: Demo) -> None:
    from app.categorize.service import create_rule  # noqa: PLC0415

    conditions = {
        "label_contains": ["UBER EATS"],
        "label_regex": "^CARTE",
        "counterparty_contains": "ALICE MARTINEZ",
        "direction": "debit",
        "amount_min": -5000,
        "amount_max": -100,
        "account_ids": [1],
    }
    create_rule(
        demo.engine,
        name="Complète",
        priority=1,
        conditions=conditions,
        category_id=demo.category("Restaurants"),
        actor_user_id=None,
        now=NOW,
    )
    shown = _call(context, "regles")["regles"][0]["conditions"]
    assert shown["contrepartie_contient"] == "A. M." and shown["sens"] == "debit"
    assert shown["montant_min"]["centimes"] == -5000 and shown["comptes"][0].startswith("c_")
    assert shown["libelle_regex"] == "^CARTE"


def test_year_over_year_comparison_and_income(context: Context) -> None:
    result = _call(
        context,
        "depenses_par_categorie",
        du="2026-08",
        au="2026-09",
        sens="revenus",
        comparer_a="annee_precedente",
    )
    assert result["comparaison"] == {"du": "2025-08", "au": "2025-09"}
    assert result["total"]["centimes"] == 250000 + 83000
    none = _call(context, "depenses_par_categorie", du="2026-09", comparer_a="aucune")
    assert none["comparaison"] is None and "comparaison" not in none["categories"][0]


def test_top_items_of_one_category(context: Context, demo: Demo) -> None:
    categorize_manually(
        demo.engine,
        demo.tx["uber_aout"],
        [(demo.category("Restaurants"), -2390)],
        actor_user_id=None,
        now=NOW,
    )
    top = _call(context, "principaux_postes", du="2026-08", au="2026-09", categorie="Alimentation")[
        "postes"
    ]
    assert [p["libelle"] for p in top] == ["CARTE UBER EATS"]


def test_net_worth_by_month_end(context: Context, demo: Demo) -> None:
    from sqlalchemy import insert  # noqa: PLC0415

    from app.models import BalanceSnapshot  # noqa: PLC0415

    with demo.engine.begin() as connection:
        for day, amount in (
            (date(2026, 7, 31), 100000),
            (date(2026, 8, 31), 150000),
            (date(2026, 9, 15), 180000),
        ):
            connection.execute(
                insert(BalanceSnapshot).values(
                    account_id=1,
                    as_of_date=day,
                    balance_type="booked",
                    amount_cents=amount,
                    fetched_at=NOW,
                )
            )
    result = _call(context, "patrimoine", du="2026-06", au="2026-09")
    assert [(m["fin_de_mois"], m["total"]["centimes"]) for m in result["fins_de_mois"]] == [
        ("2026-07-31", 100000),
        ("2026-08-31", 150000),
        ("2026-09-30", 180000),
    ]


def test_transaction_detail_links(context: Context, demo: Demo) -> None:
    from sqlalchemy import insert  # noqa: PLC0415

    from app.models import DuplicateLink, TransferLink  # noqa: PLC0415

    with demo.engine.begin() as connection:
        connection.execute(
            insert(TransferLink).values(
                debit_tx_id=demo.tx["courses_sept"],
                credit_tx_id=demo.tx["ami_rembourse"],
                method="manual",
                status="confirmed",
                created_at=NOW,
            )
        )
        connection.execute(
            insert(DuplicateLink).values(
                canonical_tx_id=demo.tx["courses_sept"],
                duplicate_tx_id=demo.tx["carte"],
                mode="manual",
                reason="exact",
                status="confirmed",
                created_at=NOW,
            )
        )
    detail = _call(context, "transaction", id=public_id("transaction", demo.tx["courses_sept"]))
    assert detail["virement_interne_avec"] == public_id("transaction", demo.tx["ami_rembourse"])
    assert detail["doublon"] == {
        "role": "conservée",
        "autre": public_id("transaction", demo.tx["carte"]),
    }
    with pytest.raises(ToolError, match="introuvable"):
        call_tool(context, "transaction", {"id": public_id("transaction", demo.tx["ignore"])})
