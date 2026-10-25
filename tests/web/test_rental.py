"""Pages « Loyers » (LOC-01 à LOC-06, LOC-14, Q17) : création, suivi, décisions manuelles."""

import re
from collections.abc import Sequence
from datetime import date

from sqlalchemy import Engine, insert, select

from app.models import (
    Account,
    AuditLog,
    Lease,
    LedgerTransaction,
    OwnerShare,
    Party,
    RentAllocation,
    RentDue,
)
from tests.web.conftest import T0, Web

_ids = iter(range(1, 100_000))


def _csrf(page: str) -> str:
    return page.split('name="csrf" value="', 1)[1].split('"', 1)[0]


def _tx(engine: Engine, amount: int, label: str, day: date) -> int:
    tx_id = next(_ids)
    with engine.begin() as connection:
        connection.execute(
            insert(LedgerTransaction).values(
                id=tx_id,
                account_id=1,
                dedupe_key=f"ext:t:{tx_id}",
                fingerprint="x",
                occurrence=1,
                booking_date=day,
                amount_cents=amount,
                currency="EUR",
                label_raw=label,
                label_clean=label,
                created_at=T0,
            )
        )
    return tx_id


def _create(web: Web, **overrides: str) -> str:
    csrf = _csrf(web.client.get("/loyers/nouveau").text)
    form = {
        "csrf": csrf,
        "type": "rent",
        "nom": "Studio",
        "nouveau_bien": "Appartement",
        "ma_part": "50",
        "compte": "1",
        "debut": "2026-09-01",
        "montant": "800,00",
        "charges": "50",
        "jour_echeance": "1",
        "jour_alerte": "5",
        "jour_impaye": "10",
        "payeur_nom": "Alice Exemple",
        "payeur_motif": "ALICE EXEMPLE",
        "caution_nom": "Bruno Fictif",
        **overrides,
    }
    response = web.client.post("/loyers", data=form, follow_redirects=False)
    assert response.status_code == 303, response.text
    return str(response.headers["location"])


def test_create_rent_lease_and_follow_payment(web: Web) -> None:
    web.login()
    location = _create(web)
    assert location.startswith("/loyers/")
    with web.engine.connect() as connection:
        lease = connection.execute(select(Lease)).one()
        shares = sorted(connection.execute(select(OwnerShare.owner_label, OwnerShare.share_bp)))
        roles = sorted(str(r) for r in connection.scalars(select(Party.role)))
        actions = set(connection.scalars(select(AuditLog.action)))
    assert (lease.rent_cents, lease.charges_cents) == (80000, 5000)
    assert shares == [("co-indivisaire", 5000), ("moi", 5000)]
    assert roles == ["guarantor", "tenant"]
    assert {"property.created", "lease.created"} <= actions
    page = web.client.get(location.split("?")[0]).text
    assert "Studio" in page and "attendu" in page and "Alice Exemple" in page
    _tx(web.engine, 85000, "VIR DE ALICE EXEMPLE", date(2026, 9, 2))
    csrf = _csrf(web.client.get("/loyers").text)
    web.client.post("/loyers/actualiser", data={"csrf": csrf})
    listing = web.client.get("/loyers").text
    assert 'etat-paye">payé' in listing


def test_loan_repayment_needs_no_property(web: Web) -> None:
    web.login()
    _create(web, type="loan", nom="Prêt", nouveau_bien="", caution_nom="", payeur_nom="Chloé Démo")
    with web.engine.connect() as connection:
        lease = connection.execute(select(Lease)).one()
    assert lease.property_id is None and str(lease.kind) == "loan"
    assert "Remboursement attendu" in web.client.get("/loyers").text


def test_invalid_forms_are_refused(web: Web) -> None:
    web.login()
    csrf = _csrf(web.client.get("/loyers/nouveau").text)
    base = {
        "csrf": csrf,
        "type": "rent",
        "nom": "X",
        "compte": "1",
        "debut": "2026-09-01",
        "montant": "800",
        "payeur_nom": "Alice Exemple",
    }
    for wrong, message in (
        ({"montant": "abc"}, "illisible"),
        ({"jour_alerte": "12"}, "Jours invalides"),
        ({"payeur_nom": " "}, "payeur"),
        ({"ma_part": "0"}, "Quote-part"),
        ({"payeur_iban": "FR00123"}, "IBAN"),
    ):
        response = web.client.post("/loyers", data={**base, **wrong})
        assert response.status_code == 422 and message in response.text, message


def test_proposal_overpayment_manual_assignment_and_end(web: Web) -> None:
    web.login()
    location = _create(web).split("?")[0]
    lease_id = int(location.rsplit("/", 1)[1])
    unknown = _tx(web.engine, 85000, "VIR SEPA INCONNU", date(2026, 9, 2))
    csrf = _csrf(web.client.get("/loyers").text)
    web.client.post("/loyers/actualiser", data={"csrf": csrf})
    listing = web.client.get("/loyers").text
    assert "À valider (1)" in listing and "VIR SEPA INCONNU" in listing
    with web.engine.connect() as connection:
        proposal = connection.scalars(select(RentAllocation.id)).one()
    assert (
        "introuvable"
        not in web.client.post(f"/loyers/propositions/{proposal}/refuser", data={"csrf": csrf}).text
    )
    # Affectation manuelle de l'opération refusée, avec justification, puis dissociation.
    page = web.client.get(location).text
    assign_path = re.findall(r'href="(/loyers/\d+/echeances/\d+/affecter)"', page)[-1]
    assign = web.client.get(assign_path).text
    assert "VIR SEPA INCONNU" in assign  # proposition refusée : de nouveau affectable
    due_id = assign.split('name="echeance" value="', 1)[1].split('"', 1)[0]
    response = web.client.post(
        f"/loyers/{lease_id}/affecter",
        data={"csrf": csrf, "echeance": due_id, "operation": str(unknown), "note": "voisin"},
        follow_redirects=True,
    )
    assert "Opération affectée" in response.text and "voisin" in response.text
    allocation = response.text.split("/affectations/", 1)[1].split("/", 1)[0]
    response = web.client.post(
        f"/loyers/{lease_id}/affectations/{allocation}/dissocier",
        data={"csrf": csrf},
        follow_redirects=True,
    )
    assert "Opération dissociée" in response.text
    # Trop-perçu du locataire, remboursé.
    _tx(web.engine, 90000, "VIR DE ALICE EXEMPLE", date(2026, 9, 3))
    web.client.post("/loyers/actualiser", data={"csrf": csrf})
    listing = web.client.get("/loyers").text
    assert "Trop-perçus à traiter (1)" in listing
    over = listing.split("/loyers/trop-percus/", 1)[1].split("/", 1)[0]
    response = web.client.post(
        f"/loyers/trop-percus/{over}/rembourser", data={"csrf": csrf}, follow_redirects=True
    )
    assert "Trop-perçu traité" in response.text
    assert (
        "inconnue"
        in web.client.post(
            f"/loyers/trop-percus/{over}/offrir", data={"csrf": csrf}, follow_redirects=True
        ).text
    )
    # Payeur supplémentaire et fin du bail.
    response = web.client.post(
        f"/loyers/{lease_id}/payeurs",
        data={"csrf": csrf, "role": "third_payer", "nom": "SCI Imaginaire", "motif": "SCI IMAG"},
        follow_redirects=True,
    )
    assert "Payeur ajouté" in response.text and "tiers payeur" in response.text
    response = web.client.post(
        f"/loyers/{lease_id}/terminer",
        data={"csrf": csrf, "fin": "2026-12-31"},
        follow_redirects=True,
    )
    assert "Fin enregistrée" in response.text and "31/12/2026" in response.text
    assert web.client.get("/loyers/999", follow_redirects=True).text.count("introuvable") == 1


def test_rental_pages_require_login_and_csrf(web: Web) -> None:
    assert web.client.get("/loyers", follow_redirects=False).status_code == 303
    web.login()
    assert web.client.post("/loyers/actualiser").status_code == 403


def test_property_charges_page(web: Web) -> None:
    web.login()
    _create(web)
    listing = web.client.get("/loyers").text
    property_path = "/biens/" + listing.split('href="/biens/', 1)[1].split('"', 1)[0]
    csrf = _csrf(listing)
    tax = _tx(web.engine, -98000, "DGFIP TAXE FONCIERE", date(2026, 9, 15))
    _tx(web.engine, -45000, "PRLV SYNDIC DEMO", date(2026, 9, 5))
    page = web.client.get(property_path).text
    assert "moi : 50 %" in page and "Aucune charge rattachée pour 2026" in page
    response = web.client.post(
        f"{property_path}/charges",
        data={"csrf": csrf, "operation": str(tax), "nature": "taxe_fonciere"},
        follow_redirects=True,
    )
    assert "Charge rattachée" in response.text and "DGFIP TAXE FONCIERE" in response.text
    response = web.client.post(
        f"{property_path}/regles",
        data={"csrf": csrf, "motif": "syndic demo", "nature": "copropriete"},
        follow_redirects=True,
    )
    assert "Règle ajoutée" in response.text and "PRLV SYNDIC DEMO" in response.text
    visible = response.text.replace(chr(0x202F), " ").replace(chr(0xA0), " ")
    assert "taxe foncière : -980,00" in visible
    expense = response.text.split("/charges/", 1)[1].split("/", 1)[0]
    response = web.client.post(
        f"{property_path}/charges/{expense}/retirer", data={"csrf": csrf}, follow_redirects=True
    )
    assert "Charge retirée" in response.text
    again = web.client.post(
        f"{property_path}/regles",
        data={"csrf": csrf, "motif": "SYNDIC DEMO", "nature": "copropriete"},
        follow_redirects=True,
    )
    assert "existe déjà" in again.text
    assert "introuvable" in web.client.get("/biens/999", follow_redirects=True).text


def test_dashboard_shows_the_month_rents(web: Web) -> None:
    web.login()
    _create(web)
    web.clock.current = web.clock.current.replace(month=9, day=27)
    page = web.client.get("/?mois=2026-09").text
    assert "Loyers et remboursements du mois" in page and "Studio" in page
    assert "impayé" in page  # rien reçu en septembre, après le 10


def _tx_on(engine: Engine, account: int, amount: int, label: str, day: date) -> int:
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
                created_at=T0,
            )
        )
    return tx_id


def test_assign_page_searches_by_account_period_text_and_amount(web: Web) -> None:
    web.login()
    location = _create(web, debut="2026-07-01").split("?")[0]
    lease_id = int(location.rsplit("/", 1)[1])
    _tx(web.engine, 12000, "REMBOURSEMENT MUTUELLE", date(2026, 7, 10))
    other = _tx_on(web.engine, 2, 85000, "VIR SEPA LOCATAIRE AUTRE COMPTE", date(2026, 7, 4))
    page = web.client.get(location).text
    assign_path = re.findall(r'href="(/loyers/\d+/echeances/\d+/affecter)"', page)[-1]
    default = web.client.get(assign_path).text
    assert "REMBOURSEMENT MUTUELLE" in default and "AUTRE COMPTE" not in default
    everywhere = web.client.get(assign_path + "?compte=tous&du=2026-07-01&texte=locataire").text
    assert "AUTRE COMPTE" in everywhere and "MUTUELLE" not in everywhere
    by_amount = web.client.get(assign_path + "?compte=tous&du=2026-06-01&montant=850,00").text
    assert "AUTRE COMPTE" in by_amount and "MUTUELLE" not in by_amount
    assert "illisible" in web.client.get(assign_path + "?montant=abc").text
    assert "Date illisible : choisissez-la" in web.client.get(assign_path + "?du=hier").text
    due_id = by_amount.split('name="echeance" value="', 1)[1].split('"', 1)[0]
    csrf = _csrf(by_amount)
    response = web.client.post(
        f"/loyers/{lease_id}/affecter",
        data={"csrf": csrf, "echeance": due_id, "operation": str(other), "retour": assign_path},
        follow_redirects=True,
    )
    assert "Opération affectée" in response.text
    assert (
        "AUTRE COMPTE" not in web.client.get(assign_path + "?compte=tous").text
    )  # plus rien à affecter
    assert (
        "introuvable"
        in web.client.get(f"/loyers/{lease_id}/echeances/999/affecter", follow_redirects=True).text
    )


def test_tracking_defaults_to_bank_history_and_can_be_moved(web: Web) -> None:
    web.login()
    _tx(web.engine, 1000, "PREMIERE OPERATION CONNUE", date(2026, 7, 1))
    location = _create(web, debut="2025-09-01").split("?")[0]
    lease_id = int(location.rsplit("/", 1)[1])
    page = web.client.get(location).text
    assert "suivi depuis 07/2026" in page and "2025-09" not in page
    csrf = _csrf(page)
    response = web.client.post(
        f"/loyers/{lease_id}/modifier",
        data={"csrf": csrf, "suivi": "2026-08"},
        follow_redirects=True,
    )
    assert "1 échéance(s) antérieure(s) au suivi retirée(s)" in response.text
    response = web.client.post(
        f"/loyers/{lease_id}/modifier",
        data={"csrf": csrf, "montant": "820", "charges": "50"},
        follow_redirects=True,
    )
    assert "premier mois concerné" in response.text
    response = web.client.post(
        f"/loyers/{lease_id}/modifier",
        data={
            "csrf": csrf,
            "montant": "820",
            "charges": "50",
            "a_partir_de": "2026-09",
            "nom": "Studio T1",
        },
        follow_redirects=True,
    )
    assert "Bail modifié" in response.text and "Studio T1" in response.text
    with web.engine.connect() as connection:
        amounts = dict(connection.execute(select(RentDue.period, RentDue.amount_cents)).all())
    assert amounts["2026-08"] == 85000 and amounts["2026-09"] == 87000


def test_accounts_can_be_renamed(web: Web) -> None:
    web.login()
    csrf = _csrf(web.client.get("/comptes").text)
    form = {"csrf": csrf, "suivi": "compte", "synchros": "2", "nom": "  Compte   joint  "}
    web.client.post("/comptes/2/reglages", data=form)
    with web.engine.connect() as connection:
        assert connection.scalar(select(Account.name).where(Account.id == 2)) == "Compte joint"
        actions = list(connection.scalars(select(AuditLog.action)))
    assert actions.count("account.renamed") == 1
    web.client.post("/comptes/2/reglages", data=form)  # inchangé : rien de plus
    with web.engine.connect() as connection:
        assert list(connection.scalars(select(AuditLog.action))).count("account.renamed") == 1
    too_long = web.client.post("/comptes/2/reglages", data={**form, "nom": "x" * 200})
    assert too_long.status_code == 422 and "trop long" in too_long.text


def test_account_role_for_the_assistant(web: Web) -> None:
    web.login()
    page = web.client.get("/comptes").text
    assert "Rôle du compte, pour l'assistant" in page
    form = {
        "csrf": _csrf(page),
        "suivi": "consultation",
        "synchros": "2",
        "role": "  Compte joint : courses   du foyer ",
    }
    web.client.post("/comptes/2/reglages", data=form)
    with web.engine.connect() as connection:
        assert (
            connection.scalar(select(Account.description).where(Account.id == 2))
            == "Compte joint : courses du foyer"
        )
        details: Sequence[dict[str, int]] = connection.scalars(
            select(AuditLog.details).where(AuditLog.action == "account.described")
        ).all()
    assert details == [{"longueur": 31}]  # jamais le texte (il peut contenir un nom)
    assert 'value="Compte joint : courses du foyer"' in web.client.get("/comptes").text
    web.client.post("/comptes/2/reglages", data={**form, "role": " "})
    with web.engine.connect() as connection:
        assert connection.scalar(select(Account.description).where(Account.id == 2)) is None
    too_long = web.client.post("/comptes/2/reglages", data={**form, "role": "x" * 600})
    assert too_long.status_code == 422 and "trop long" in too_long.text


def test_property_year_summary_and_csv_export(web: Web) -> None:
    web.login()
    _create(web)
    with web.engine.connect() as connection:
        property_id = connection.execute(select(OwnerShare.property_id)).scalars().first()
    page = web.client.get(f"/biens/{property_id}?annee=2026").text
    assert "Récapitulatif 2026" in page
    assert "moi (50 %)" in page and "Indicatif" in page
    response = web.client.get(f"/biens/{property_id}/recapitulatif.csv?annee=2026")
    assert response.status_code == 200
    assert (
        response.headers["content-disposition"] == 'attachment; filename="recapitulatif-2026.csv"'
    )
    assert response.text.startswith("﻿Poste;Total;")
    with web.engine.connect() as connection:
        actions = list(connection.scalars(select(AuditLog.action)))
    assert "export.property_summary_csv" in actions
    missing = web.client.get("/biens/999/recapitulatif.csv", follow_redirects=False)
    assert missing.status_code == 303


def test_irl_revision_from_the_lease_page(web: Web) -> None:
    web.login()
    location = _create(web, debut="2025-10-15")
    path = location.split("?")[0]
    page = web.client.get(path).text
    assert "Révision annuelle du loyer (IRL)" in page
    assert "Indiquez le trimestre" in page

    def post(action: str, **form: str) -> str:
        csrf = _csrf(web.client.get(path).text)
        response = web.client.post(f"{path}/irl/{action}", data={"csrf": csrf, **form})
        return response.text

    assert "Trimestre illisible" in post("reference", trimestre="deuxième")
    assert "Indice de référence enregistré" in post("reference", trimestre="2025-T2")
    assert "indice T2 2025 à saisir" in web.client.get(path).text  # T0 : 27/09, anniversaire 15/10
    assert "Montant illisible" in post("indice", trimestre="2025-T2", valeur="abc")
    post("indice", trimestre="2025-T2", valeur="145,17")
    assert "Montant obligatoire" in post("indice", trimestre="2026-T2", valeur="")
    page = post("indice", trimestre="2026-T2", valeur="148,12")
    assert "800,00" in page and "816,26" in page  # 800 * 148,12 / 145,17
    assert "T2 2025 : 145,17" in page
    page = post("appliquer")
    assert "Loyer révisé à partir du 15/10/2026" in page
    with web.engine.connect() as connection:
        lease = connection.execute(select(Lease)).one()
    assert (lease.rent_cents, lease.irl_reference_quarter) == (81_626, "2026-T2")
    assert "Aucune révision à appliquer" in post("appliquer")


def test_months_before_bank_history_are_flagged_with_a_one_click_fix(web: Web) -> None:
    web.login()
    _tx(web.engine, 1000, "PREMIERE OPERATION CONNUE", date(2026, 6, 30))
    location = _create(web, debut="2025-09-01", suivi="2025-09").split("?")[0]
    lease_id = int(location.rsplit("/", 1)[1])
    page = web.client.get(location).text
    assert "Des mois anciens faussent le suivi" in page
    assert "10 échéances (septembre 2025 à juin 2026)" in page
    assert "complet qu'à partir de juillet 2026" in page
    assert "Suivre à partir de juillet 2026" in page
    fixed = web.client.post(
        f"/loyers/{lease_id}/modifier",
        data={"csrf": _csrf(page), "suivi": "2026-07"},
        follow_redirects=True,
    )
    assert "10 échéance(s) antérieure(s) au suivi retirée(s)" in fixed.text
    assert "Des mois anciens faussent le suivi" not in fixed.text
