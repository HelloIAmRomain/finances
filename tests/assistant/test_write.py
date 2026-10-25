"""Outils d'écriture de l'IA (docs/spec-mcp.md §4, §8) : mêmes services que le web, lots
annulables, aperçu puis confirmation des écritures de masse, droits et interrupteurs."""

import json
from dataclasses import replace
from datetime import date
from typing import Any

import pytest
from sqlalchemy import insert, select

import app.assistant.read
import app.assistant.write  # noqa: F401 (enregistre les outils)
from app.assistant.access import WRITES
from app.assistant.changes import snapshot
from app.assistant.tools import TOOLS, Context, ToolError, call_tool, public_id
from app.ledger.transfers import detect_transfers
from app.models import AuditLog, Lease, Party, Rule
from tests.assistant.conftest import NOW, Demo, add_tx

NAMES = ("ALICE", "MARTINEZ", "HUGO", "BLANCHARD")


def _call(context: Context, name: str, **arguments: Any) -> dict[str, Any]:
    result = call_tool(context, name, arguments)
    shown = json.dumps(result, ensure_ascii=False).upper()
    for word in NAMES:
        assert word not in shown, (name, word)
    return result


def _find(context: Context, text: str) -> str:
    rows = _call(context, "rechercher_transactions", texte=text)["transactions"]
    return str(rows[0]["id"])


def _categories(context: Context, tx: str) -> list[str]:
    return [line["categorie"] for line in _call(context, "transaction", id=tx)["categories"]]


def test_catalogue() -> None:
    writes = sorted(t.name for t in TOOLS.values() if t.write)
    assert len(writes) == 20
    descriptor = TOOLS["supprimer_regle"].descriptor()
    assert descriptor["annotations"]["destructiveHint"] is True
    assert descriptor["annotations"]["readOnlyHint"] is False
    assert TOOLS["comptes"].descriptor()["annotations"]["readOnlyHint"] is True


def test_write_needs_scope_and_switch(context: Context) -> None:
    tx = _find(context, "UBER")
    arguments = {"transactions": [tx], "categorie": "Restaurants"}
    read_only = replace(context, token=replace(context.token, can_write=False))
    with pytest.raises(ToolError, match="lecture seule"):
        call_tool(read_only, "categoriser", arguments)
    switched_off = replace(context, switches={**context.switches, WRITES: False})
    with pytest.raises(ToolError, match="désactivées"):
        call_tool(switched_off, "categoriser", arguments)


def test_categorize_then_undo(context: Context, demo: Demo) -> None:
    before = snapshot(demo.engine)
    tx = _find(context, "UBER")
    result = _call(context, "categoriser", transactions=[tx], categorie="Restaurants")
    assert result["applique"] and result["effets"]["operations_reclassees"] == 1
    assert result["effets"]["detail"][0]["apres"] == ["Alimentation › Restaurants"]
    assert _categories(context, tx) == ["Alimentation › Restaurants"]
    listed = _call(context, "modifications")["modifications"]
    assert listed[0]["id"] == result["modification"] and not listed[0]["annulee"]
    _call(context, "annuler_modification", modification=result["modification"])
    assert snapshot(demo.engine) == before
    with pytest.raises(ToolError, match="déjà annulée"):
        call_tool(context, "annuler_modification", {"modification": result["modification"]})
    with demo.engine.connect() as connection:
        actions = set(connection.scalars(select(AuditLog.action)))
    assert {"categorization.manual", "ai.change_set", "ai.change_undone"} <= actions


def test_split_and_uncategorize(context: Context) -> None:
    tx = _find(context, "CARREFOUR")
    amount = _call(context, "transaction", id=tx)["montant"]["centimes"]
    split = [
        {"categorie": "Courses", "montant": amount + 1000},
        {"categorie": "Achats › Cadeaux", "montant": -1000},
    ]
    _call(context, "categoriser", transactions=[tx], ventilation=split)
    assert _categories(context, tx) == ["Alimentation › Courses", "Achats › Cadeaux"]
    with pytest.raises(ToolError, match="somme"):
        call_tool(context, "categoriser", {"transactions": [tx], "ventilation": split[:1]})
    _call(context, "decategoriser", transactions=[tx])
    assert _categories(context, tx) == []


def test_mass_write_needs_preview_and_confirmation(context: Context, demo: Demo) -> None:
    for day in range(1, 26):
        add_tx(
            demo.engine, 1, date(2026, 8, day), -500, f"CARTE {day:02d}/08/26 BOULANGERIE DU COIN"
        )
    ids = [
        t["id"]
        for t in _call(context, "rechercher_transactions", texte="BOULANGERIE", limite=200)[
            "transactions"
        ]
    ]
    arguments = {"transactions": ids, "categorie": "Boulangerie"}
    preview = _call(context, "categoriser", **arguments)
    assert not preview["applique"] and preview["apercu"]["operations_reclassees"] == 25
    assert _categories(context, ids[0]) == []  # rien d'appliqué
    with pytest.raises(ToolError, match="Code de confirmation"):
        call_tool(
            context,
            "categoriser",
            {**arguments, "categorie": "Courses", "confirmation": preview["confirmation"]},
        )
    with pytest.raises(
        ToolError, match="Code de confirmation"
    ):  # code usé par l'essai refusé ? non : paramètres
        call_tool(context, "categoriser", {**arguments, "confirmation": "c_" + "a" * 26})
    done = _call(context, "categoriser", **arguments, confirmation=preview["confirmation"])
    assert done["applique"] and done["effets"]["operations_reclassees"] == 25
    with pytest.raises(ToolError, match="Code de confirmation"):
        call_tool(context, "categoriser", {**arguments, "confirmation": preview["confirmation"]})


def test_rules_create_modify_delete(context: Context, demo: Demo) -> None:
    created = _call(
        context,
        "creer_regle",
        nom="Uber Eats",
        categorie="Restaurants",
        libelle_contient=["UBER EATS"],
    )
    assert created["effets"]["operations_reclassees"] == 2
    rule = _call(context, "regles")["regles"][0]
    assert (rule["nom"], rule["operations_classees"], rule["categorie"]) == (
        "Uber Eats",
        2,
        "Alimentation › Restaurants",
    )
    tx = _find(context, "UBER")
    assert _call(context, "transaction", id=tx)["categories"][0]["source"] == "règle"
    _call(context, "modifier_regle", regle=rule["id"], categorie="Sorties")
    assert _categories(context, tx) == ["Loisirs › Sorties"]
    _call(context, "modifier_regle", regle=rule["id"], montant_max=-2000)
    assert _call(context, "regles")["regles"][0]["operations_classees"] == 1
    _call(context, "supprimer_regle", regle=rule["id"])
    assert _call(context, "regles")["regles"][0]["active"] is False
    assert _categories(context, tx) == []
    with pytest.raises(ToolError, match="Règle introuvable"):
        call_tool(context, "supprimer_regle", {"regle": public_id("règle", 999)})


def test_rule_patterns_never_use_masked_text(context: Context, demo: Demo) -> None:
    with pytest.raises(ToolError, match="a_partir_de_transaction"):
        call_tool(
            context,
            "creer_regle",
            {"nom": "Ami", "categorie": "Sorties", "libelle_contient": ["M H. B."]},
        )
    tx = _find(context, "RESTO")
    _call(context, "creer_regle", nom="Ami", categorie="Sorties", a_partir_de_transaction=tx)
    with demo.engine.connect() as connection:
        conditions = connection.scalar(select(Rule.conditions))
    assert conditions is not None and conditions["label_contains"] == [
        "VIR INST M HUGO"
    ]  # vrai libellé, côté serveur
    shown = _call(context, "regles")["regles"][0]["conditions"]["libelle_contient"]
    assert shown == ["VIR INST M H."]
    assert _categories(context, tx) == ["Loisirs › Sorties"]


def test_category_tree(context: Context) -> None:
    _call(context, "creer_categorie", nom="Animaux", type="depense")
    _call(context, "creer_categorie", nom="Vétérinaire", parent="Animaux")
    _call(context, "renommer_categorie", categorie="Vétérinaire", nouveau_nom="Véto")
    _call(context, "deplacer_categorie", categorie="Véto", nouveau_parent="Santé")
    paths = [c["categorie"] for c in _call(context, "categories")["categories"]]
    assert "Santé › Véto" in paths and "Animaux" in paths
    with pytest.raises(ToolError, match="Deux niveaux"):
        call_tool(context, "creer_categorie", {"nom": "X", "parent": "Santé › Véto"})
    with pytest.raises(ToolError, match="type"):
        call_tool(context, "creer_categorie", {"nom": "Sans type"})


def test_rent_tools(context: Context, demo: Demo) -> None:
    lease = _call(context, "loyers")["baux"][0]
    due = lease["echeances"][-1]["id"]
    refund = _find(context, "REMBT")
    assigned = _call(context, "affecter_loyer", echeance=due, transaction=refund, note="complément")
    assert assigned["applique"]
    after = _call(context, "loyers")["baux"][0]["echeances"][-1]
    assert after["reste"]["centimes"] == 2000  # 800 € + 30 € sur 850 €
    allocation = next(p["affectation"] for p in after["paiements"] if p["transaction"] == refund)
    _call(context, "desaffecter_loyer", affectation=allocation)
    assert _call(context, "loyers")["baux"][0]["echeances"][-1]["statut"] == "impayé"
    _call(context, "modifier_bail", bail=lease["id"], loyer=75000, a_partir_de="2026-09")
    assert _call(context, "loyers")["baux"][0]["echeances"][-1]["statut"] == "payé"
    with pytest.raises(ToolError, match="a_partir_de"):
        call_tool(context, "modifier_bail", {"bail": lease["id"], "loyer": 1})
    _call(context, "terminer_bail", bail=lease["id"], fin="2026-12-31")
    assert _call(context, "loyers")["baux"][0]["fin"] == "2026-12-31"


def test_new_lease_takes_payer_from_real_label(context: Context, demo: Demo) -> None:
    refund = _find(context, "REMBT")
    created = _call(
        context,
        "creer_bail",
        nom="Prêt ami",
        type="remboursement",
        compte="Courant Bourso",
        debut="2026-09-01",
        loyer=3000,
        payeur_transaction=refund,
    )
    assert created["applique"]
    with demo.engine.connect() as connection:
        lease_id = connection.scalar(select(Lease.id).where(Lease.label == "Prêt ami"))
        names = connection.scalars(select(Party.name_enc).where(Party.lease_id == lease_id)).all()
    assert names == ["Hugo Blanchard"]  # en clair côté serveur seulement
    lease = next(b for b in _call(context, "loyers")["baux"] if b["nom"] == "Prêt ami")
    assert lease["echeances"][-1]["statut"] == "payé"
    with pytest.raises(ToolError, match="encaissement"):
        call_tool(
            context, "ajouter_payeur", {"bail": lease["id"], "transaction": _find(context, "RESTO")}
        )
    with pytest.raises(ToolError, match="bien"):
        call_tool(
            context,
            "creer_bail",
            {
                "nom": "x",
                "compte": "Courant Bourso",
                "debut": "2026-09-01",
                "loyer": 1,
                "payeur_transaction": refund,
            },
        )


def test_property_expenses(context: Context) -> None:
    lease = _call(context, "loyers")["baux"][0]
    tx = _find(context, "NETFLIX")
    _call(context, "affecter_charge", bien=lease["bien_id"], transaction=tx, type="autre")
    (charge,) = _call(context, "transaction", id=tx)["charge_du_bien"]
    _call(context, "retirer_charge", charge=charge["id"])
    assert _call(context, "transaction", id=tx)["charge_du_bien"] == []


def test_review_queue_decision(context: Context, demo: Demo) -> None:
    add_tx(demo.engine, 1, date(2026, 9, 12), -5000, "VIR VERS COMPTE JOINT")
    add_tx(demo.engine, 2, date(2026, 9, 12), 5000, "VIR DEPUIS COMPTE COURANT")
    detect_transfers(demo.engine)
    items = _call(context, "a_valider")["elements"]
    if not items:  # virement confirmé d'emblée : rien à décider
        pytest.skip("aucune proposition de virement à valider")
    item = items[0]
    with pytest.raises(ToolError, match="impossible"):
        call_tool(
            context, "resoudre_a_valider", {"element": item["id"], "decision": "prendre_doublon"}
        )
    _call(context, "resoudre_a_valider", element=item["id"], decision="confirmer")
    assert _call(context, "a_valider")["elements"] == []


def test_unknown_reference_is_explained_without_sql(context: Context) -> None:
    refund = _find(context, "REMBT")
    arguments = {
        "nom": "x",
        "nouveau_bien": None,
        "bien": public_id("bien", 999),
        "compte": "Courant Bourso",
        "debut": "2026-09-01",
        "loyer": 1000,
        "payeur_transaction": refund,
    }
    with pytest.raises(ToolError, match="n'existe pas"):
        call_tool(context, "creer_bail", arguments)


def test_categorize_argument_checks(context: Context, demo: Demo) -> None:
    uber, courses = _find(context, "UBER"), _find(context, "CARREFOUR")
    split = [{"categorie": "Courses", "montant": -100}]
    for arguments, message in (
        ({"transactions": [uber]}, "soit"),
        ({"transactions": [uber], "categorie": "Courses", "ventilation": split}, "soit"),
        ({"transactions": [uber, courses], "ventilation": split}, "une seule"),
        (
            {"transactions": [public_id("transaction", demo.tx["ignore"])], "categorie": "Courses"},
            "introuvable",
        ),
    ):
        with pytest.raises(ToolError, match=message):
            call_tool(context, "categoriser", arguments)


def test_rule_conditions_are_validated(context: Context) -> None:
    with pytest.raises(ToolError, match="Conditions invalides"):
        call_tool(
            context, "creer_regle", {"nom": "x", "categorie": "Courses", "libelle_regex": "("}
        )
    _call(
        context,
        "creer_regle",
        nom="Courant",
        categorie="Courses",
        libelle_contient=["CARREFOUR"],
        comptes=["Courant Bourso"],
        sens="debit",
    )
    rule = _call(context, "regles")["regles"][0]
    assert rule["conditions"]["comptes"] and rule["operations_classees"] == 2
    _call(
        context,
        "modifier_regle",
        regle=rule["id"],
        libelle_contient=["UBER"],
        remplacer_conditions=True,
    )
    assert _call(context, "regles")["regles"][0]["conditions"] == {"libelle_contient": ["UBER"]}


def _review(demo: Demo, kind: str, **values: Any) -> str:
    from app.models import ReviewItem  # noqa: PLC0415

    with demo.engine.begin() as connection:
        result = connection.execute(
            insert(ReviewItem).values(
                kind=kind, dedupe_key=f"test:{kind}", status="open", created_at=NOW, **values
            )
        )
    return public_id("élément à valider", int(result.inserted_primary_key[0]))  # type: ignore[index]


def test_review_decisions_by_kind(context: Context, demo: Demo) -> None:
    from app.models import DuplicateLink  # noqa: PLC0415

    with demo.engine.begin() as connection:
        link = connection.execute(
            insert(DuplicateLink).values(
                canonical_tx_id=demo.tx["courses_sept"],
                duplicate_tx_id=demo.tx["carte"],
                mode="auto",
                reason="probable",
                status="proposed",
                created_at=NOW,
            )
        ).inserted_primary_key[0]  # type: ignore[index]
    duplicate = _review(
        demo,
        "duplicate",
        duplicate_link_id=link,
        transaction_id=demo.tx["carte"],
        other_transaction_id=demo.tx["courses_sept"],
    )
    _call(context, "resoudre_a_valider", element=duplicate, decision="rejeter")
    categorize_lines = [(demo.category("Courses"), -9120)]
    from app.categorize.service import categorize_manually  # noqa: PLC0415

    categorize_manually(
        demo.engine, demo.tx["carte"], categorize_lines, actor_user_id=None, now=NOW
    )
    conflict = _review(
        demo,
        "category_conflict",
        details={"canonical": demo.tx["courses_sept"], "duplicate": demo.tx["carte"]},
    )
    _call(context, "resoudre_a_valider", element=conflict, decision="prendre_doublon")
    assert _categories(context, public_id("transaction", demo.tx["courses_sept"])) == [
        "Alimentation › Courses"
    ]
    transfer = _review(
        demo,
        "transfer_conflict",
        details={"canonical": demo.tx["courses_sept"], "duplicate": demo.tx["carte"]},
    )
    _call(context, "resoudre_a_valider", element=transfer, decision="garder_actuelle")
    assert _call(context, "a_valider")["elements"] == []
    with pytest.raises(ToolError, match="déjà traité"):
        call_tool(
            context, "resoudre_a_valider", {"element": transfer, "decision": "garder_actuelle"}
        )


def test_proposals_and_overpayments(context: Context, demo: Demo) -> None:
    from app.models import Overpayment, RentAllocation, RentDue  # noqa: PLC0415

    with demo.engine.begin() as connection:
        due = connection.scalar(select(RentDue.id))
        proposal = connection.execute(
            insert(RentAllocation).values(
                due_id=due,
                transaction_id=demo.tx["ami_rembourse"],
                amount_cents=3000,
                status="proposed",
                method="auto",
                created_at=NOW,
            )
        ).inserted_primary_key[0]  # type: ignore[index]
        other = connection.execute(
            insert(RentAllocation).values(
                due_id=due,
                transaction_id=demo.tx["salaire_aout"],
                amount_cents=2000,
                status="proposed",
                method="auto",
                created_at=NOW,
            )
        ).inserted_primary_key[0]  # type: ignore[index]
        overpaid = connection.execute(
            insert(Overpayment).values(
                lease_id=demo.lease_id,
                transaction_id=demo.tx["loyer_sept"],
                amount_cents=1000,
                decision="pending",
                created_at=NOW,
            )
        ).inserted_primary_key[0]  # type: ignore[index]
    pending = _call(context, "loyers")
    assert (
        len(pending["propositions_en_attente"]) == 2 and len(pending["trop_percus_en_attente"]) == 1
    )
    _call(
        context,
        "decider_proposition",
        affectation=public_id("affectation", proposal),
        decision="confirmer",
    )
    _call(
        context,
        "decider_proposition",
        affectation=public_id("affectation", other),
        decision="rejeter",
    )
    _call(
        context,
        "decider_trop_percu",
        trop_percu=public_id("trop-perçu", overpaid),
        decision="rembourser",
    )
    after = _call(context, "loyers")
    assert after["propositions_en_attente"] == [] and after["trop_percus_en_attente"] == []


def test_payer_detection_limits_and_new_property(context: Context, demo: Demo) -> None:
    two = add_tx(demo.engine, 1, date(2026, 9, 14), 2000, "VIR M PAUL ROUX OU MME LEA ROUX")
    with pytest.raises(ToolError, match="ambigu"):
        call_tool(
            context,
            "creer_bail",
            {
                "nom": "x",
                "nouveau_bien": "Garage",
                "compte": "Courant Bourso",
                "debut": "2026-09-01",
                "loyer": 1000,
                "payeur_transaction": public_id("transaction", two),
            },
        )
    refund = _find(context, "REMBT")
    _call(
        context,
        "creer_bail",
        nom="Garage",
        nouveau_bien="Garage rue X",
        compte="Courant Bourso",
        debut="2026-09-01",
        loyer=3000,
        payeur_transaction=refund,
    )
    lease = next(b for b in _call(context, "loyers")["baux"] if b["nom"] == "Garage")
    assert lease["bien"] == "Garage rue X" and lease["bien_id"]
    _call(
        context,
        "ajouter_payeur",
        bail=lease["id"],
        transaction=_find(context, "LOYER SEPT"),
        role="caution",
    )
    from app.models import Party  # noqa: PLC0415

    with demo.engine.connect() as connection:
        names = sorted(connection.scalars(select(Party.name_enc)).all())
    assert names == ["Alice Martinez", "Alice Martinez", "Hugo Blanchard"]
