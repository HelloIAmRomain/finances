"""Parcours réels dans Chromium, en HTTPS (UI-01, UI-02, UI-05, COL-01, COL-07, ANA-09, ANA-11).

Chaque bug d'affichage ou de navigateur rencontré en production a son test ici : feuille de style
bloquée comme contenu mixte, bouton de liaison bloqué par la CSP, cookies SameSite au retour de la
banque.
"""

import json
from datetime import UTC, datetime
from urllib.parse import quote

import pytest
from playwright.sync_api import Page, Route, expect
from sqlalchemy import insert, select

from app.models import Account, LedgerTransaction, RentDue
from app.models.rental import LeaseKind, PartyRole
from app.rental.allocations import allocate_manually
from app.rental.engine import run_rental
from app.rental.leases import LeaseTerms, add_party, create_lease, create_property
from app.rental.receipts import set_details
from tests.e2e.conftest import PASSWORD, USERNAME, Site, login, watch

PAGES = (
    "/",
    "/comptes",
    "/loyers",
    "/loyers/nouveau",
    "/echeances",
    "/connexions",
    "/transactions",
    "/budgets",
    "/abonnements",
    "/prevision",
    "/patrimoine",
    "/import",
    "/regles",
    "/a-valider",
    "/alertes",
    "/securite",
    "/systeme",
    "/journal",
    "/ia/modifications",
    "/assistant",
)


def _styled(page: Page) -> bool:
    """La feuille de style est appliquée : en-tête collant, fond de page coloré."""
    result: bool = page.evaluate(
        """() => getComputedStyle(document.querySelector('header')).position === 'sticky'
                && getComputedStyle(document.body).backgroundColor !== 'rgba(0, 0, 0, 0)'"""
    )
    return result


def test_login_page_is_styled_and_clean(page: Page, site: Site) -> None:
    seen = watch(page, site.base_url)
    page.goto(f"{site.base_url}/connexion")
    assert _styled(page), "feuille de style non appliquée"
    assert seen.clean(), seen.report()


def test_wrong_code_is_refused(page: Page, site: Site) -> None:
    page.goto(f"{site.base_url}/connexion")
    page.fill("input[name=identifiant]", "admin")
    page.fill("input[name=mot_de_passe]", "mauvais-mot-de-passe")
    page.fill("input[name=code]", "000000")
    page.click("button[type=submit]")
    expect(page).to_have_url(f"{site.base_url}/connexion")
    assert page.locator("[role=alert]").count() == 1


@pytest.mark.parametrize("path", PAGES)
def test_every_page_is_styled_without_errors(page: Page, site: Site, path: str) -> None:
    """Aucune erreur de console, de script ou de requête, aucune violation de CSP, et la feuille
    de style est bien chargée (elle était bloquée comme contenu mixte derrière Tailscale)."""
    login(page, site)
    seen = watch(page, site.base_url)
    page.goto(f"{site.base_url}{path}")
    page.wait_for_load_state("networkidle")
    assert _styled(page), f"{path} : feuille de style non appliquée"
    assert seen.clean(), f"{path} : {seen.report()}"
    assert page.locator("html").get_attribute("lang") == "fr"


@pytest.mark.parametrize("path", PAGES)
def test_pages_fit_a_phone_screen(page: Page, site: Site, path: str) -> None:
    page.set_viewport_size({"width": 360, "height": 780})  # téléphone Android courant
    login(page, site)
    page.goto(f"{site.base_url}{path}")
    page.wait_for_load_state("networkidle")
    overflow = page.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    culprits = page.evaluate(
        """() => [...document.querySelectorAll('main *')]
            .filter(e => e.getBoundingClientRect().right > document.documentElement.clientWidth + 1)
            .filter(e => ![...e.children].some(c => c.getBoundingClientRect().right
                         > document.documentElement.clientWidth + 1))
            .slice(0, 5)
            .map(e => e.tagName + (e.name ? '[name=' + e.name + ']' : '') + '.' + e.className
                      + ' ' + Math.round(e.getBoundingClientRect().width) + 'px')"""
    )
    assert overflow <= 1, f"{path} : défilement horizontal de {overflow} px ; en cause : {culprits}"


def test_dashboard_draws_both_charts(page: Page, site: Site) -> None:
    login(page, site)
    page.wait_for_load_state("networkidle")
    for canvas in ("#graphique-flux", "#graphique-categories"):
        drawn = page.evaluate(
            """(sel) => {
                const c = document.querySelector(sel);
                const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
                for (let i = 3; i < d.length; i += 4) if (d[i] !== 0) return true;
                return false;
            }""",
            canvas,
        )
        assert drawn, f"{canvas} : graphique vide (script bloqué ?)"
    expect(page.locator(".tuiles")).to_contain_text("1 234,56")


def _bank_redirects_back(site: Site):  # type: ignore[no-untyped-def]
    """La « banque » renvoie vers le retour de l'application avec le state reçu par /auth."""

    def handle(route: Route) -> None:
        auth = [r for r in site.fake_bank.requests if r.url.path == "/auth"][-1]
        state = json.loads(auth.content)["state"]
        target = f"{site.base_url}/connexions/retour?code=code-e2e&state={quote(state)}"
        route.fulfill(status=302, headers={"Location": target})

    return handle


def test_bank_linking_with_a_normal_click(page: Page, site: Site) -> None:
    """Le clic normal sur « Autoriser l'accès en lecture » mène à la banque (il était bloqué par
    la CSP form-action), puis le retour aboutit, connexion active et comptes créés."""
    login(page, site)
    seen = watch(page, site.base_url)
    page.route(f"https://{'tilisy.enablebanking.com'}/**", _bank_redirects_back(site))
    page.goto(f"{site.base_url}/connexions")
    page.click("text=Autoriser l'accès en lecture")
    page.wait_for_load_state("networkidle")
    expect(page.locator("body")).to_contain_text("Banque Exemple")
    expect(page.locator("body")).to_contain_text("active")
    assert seen.clean(), seen.report()
    page.goto(f"{site.base_url}/comptes")
    expect(page.locator("body")).to_contain_text("Compte courant")


def test_manual_sync_and_account_settings(page: Page, site: Site) -> None:
    login(page, site)
    page.route("https://tilisy.enablebanking.com/**", _bank_redirects_back(site))
    page.goto(f"{site.base_url}/connexions")
    page.click("text=Autoriser l'accès en lecture")
    page.wait_for_load_state("networkidle")
    page.goto(f"{site.base_url}/comptes")
    row = page.locator("tr", has_text="Compte courant")
    row.locator("button", has_text="Mettre à jour maintenant").click()
    expect(page.locator("[role=status]")).to_contain_text("Mise à jour terminée")
    row = page.locator("tr", has_text="Compte courant")
    row.locator("select[name=suivi]").select_option("consultation")
    row.locator("button", has_text="Enregistrer").click()
    expect(page.locator("[role=status]")).to_contain_text("Réglages enregistrés")
    expect(page.locator("tr", has_text="Compte courant")).to_contain_text("consultation seule")


def test_transactions_filter_classify_and_export(page: Page, site: Site) -> None:
    login(page, site)
    page.goto(f"{site.base_url}/transactions")
    page.fill("input[name=texte]", "SUPERMARCHE")
    page.click("button:has-text('Filtrer')")
    expect(page.locator("tbody tr").first).to_contain_text("SUPERMARCHE")
    assert "SALAIRE" not in page.locator("tbody").inner_text()
    first = page.locator("tbody tr").first
    first.locator("select").select_option(label="Alimentation › Courses")
    first.locator("button", has_text="Classer").click()
    expect(page.locator("tbody tr").first).to_contain_text("Courses")
    with page.expect_download() as download:
        page.click("text=Exporter en CSV")
    assert download.value.suggested_filename.endswith(".csv")


def test_pwa_manifest_and_service_worker(page: Page, site: Site) -> None:
    login(page, site)
    manifest = page.request.get(f"{site.base_url}/manifest.webmanifest")
    assert manifest.ok and manifest.json()["display"] == "standalone"
    registered = page.evaluate("() => navigator.serviceWorker.ready.then(r => r.active !== null)")
    assert registered


def test_logout(page: Page, site: Site) -> None:
    login(page, site)
    page.click("button:has-text('Déconnexion')")
    page.goto(f"{site.base_url}/comptes")
    expect(page).to_have_url(f"{site.base_url}/connexion?suivant=/comptes")


def test_create_a_lease_in_the_browser(page: Page, site: Site) -> None:
    """Formulaire complet d'un bail, puis fiche du bail, sans erreur ni débordement."""
    login(page, site)
    seen = watch(page, site.base_url)
    page.goto(f"{site.base_url}/loyers/nouveau")
    page.fill("input[name=nom]", "Studio démo")
    page.fill("input[name=nouveau_bien]", "Bien démo")
    page.fill("input[name=montant]", "800,00")
    page.fill("input[name=charges]", "50")
    page.fill("input[name=payeur_nom]", "Alice Exemple")
    page.fill("input[name=payeur_motif]", "ALICE EXEMPLE")
    page.click("button:has-text('Enregistrer')")
    expect(page.locator("h1")).to_contain_text("Studio démo")
    expect(page.locator("body")).to_contain_text("Alice Exemple")
    expect(page.locator("[role=status]")).to_contain_text("Enregistré")
    page.goto(f"{site.base_url}/loyers")
    expect(page.locator("body")).to_contain_text("Studio démo")
    page.click("text=Bien démo")
    expect(page.locator("h1")).to_contain_text("Bien démo")
    expect(page.locator("body")).to_contain_text("Charges")
    assert seen.clean(), seen.report()


def test_add_a_property_tax_in_the_browser(page: Page, site: Site) -> None:
    """Saisie d'une taxe foncière, fiche, export ICS : sans erreur ni débordement (FIS-04)."""
    login(page, site)
    seen = watch(page, site.base_url)
    page.goto(f"{site.base_url}/echeances")
    page.select_option("select[name=nature]", "taxe_fonciere")
    page.fill("input[name=echeance]", "2026-10-15")
    page.fill("input[name=montant]", "1 234,00")
    page.fill("input[name=recuperable]", "150")
    page.click("form[action='/echeances'] button[type=submit]")
    expect(page.locator("h1")).to_contain_text("Taxe foncière")
    expect(page.locator("[role=status]")).to_contain_text("Échéance ajoutée")
    expect(page.locator("body")).to_contain_text("dont récupérable sur le locataire")
    page.goto(f"{site.base_url}/echeances")
    expect(page.locator("body")).to_contain_text("À venir")
    with page.expect_download() as download:
        page.click("text=Ajouter à l'agenda du téléphone")
    assert download.value.suggested_filename == "echeances.ics"
    assert seen.clean(), seen.report()


def _overflow(page: Page) -> int:
    result: int = page.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    return result


def test_rent_receipt_on_a_phone(page: Page, site: Site) -> None:
    """Mois payé → aperçu de la quittance et PDF téléchargé, au format téléphone (LOC-07)."""
    now = datetime.now(UTC)
    start = now.date().replace(day=1)
    with site.engine.connect() as connection:
        account = connection.scalars(select(Account.id)).first()
    assert account is not None
    property_id = create_property(
        site.engine, name="Bien démo", address=None, furnished=True, shares=[("moi", 10000)],
        now=now,
    )  # fmt: skip
    set_details(
        site.engine, property_id, landlord="A. Démo\n1 rue de l'Exemple, 75000 Paris",
        address="3 avenue Fictive, 69000 Lyon", actor_user_id=None, now=now,
    )  # fmt: skip
    lease_id = create_lease(
        site.engine,
        LeaseTerms(
            kind=LeaseKind.RENT, label="Studio démo", account_id=account, start_date=start,
            rent_cents=80_000, charges_cents=5_000, property_id=property_id,
        ),
        now=now,
    )  # fmt: skip
    add_party(site.engine, lease_id, role=PartyRole.TENANT, name="Alice Exemple")
    run_rental(site.engine, now=now)
    with site.engine.begin() as connection:
        tx_id = connection.execute(
            insert(LedgerTransaction).values(
                account_id=account, dedupe_key="ext:e2e:loyer", fingerprint="x", occurrence=1,
                booking_date=start, amount_cents=85_000, currency="EUR",
                label_raw="VIR LOYER DEMO", label_clean="VIR LOYER DEMO", created_at=now,
            ).returning(LedgerTransaction.id)
        ).scalar_one()  # fmt: skip
        due_id = connection.scalar(
            select(RentDue.id).where(
                RentDue.lease_id == lease_id, RentDue.period == f"{start:%Y-%m}"
            )
        )
    assert due_id is not None
    allocate_manually(
        site.engine, due_id=due_id, transaction_id=tx_id, amount_cents=None, note=None,
        actor_user_id=None, now=now,
    )  # fmt: skip
    page.set_viewport_size({"width": 360, "height": 780})
    login(page, site)
    seen = watch(page, site.base_url)
    page.goto(f"{site.base_url}/loyers/{lease_id}")
    expect(page.locator("#depot")).to_contain_text("Dépôt de garantie")
    assert _overflow(page) <= 1  # fiche du bail (quittances, dépôt de garantie) au téléphone
    page.click("text=Quittance >> nth=0")
    expect(page.locator("h1")).to_contain_text("Quittance de loyer")
    expect(page.locator("body")).to_contain_text("Alice Exemple")
    assert _overflow(page) <= 1
    with page.expect_download() as download:
        page.click("text=Télécharger la quittance")
    assert download.value.suggested_filename == f"quittance-{start:%Y-%m}.pdf"
    page.goto(f"{site.base_url}/biens/{property_id}")
    expect(page.locator("textarea[name=adresse]")).to_have_value("3 avenue Fictive, 69000 Lyon")
    assert seen.clean(), seen.report()


def test_security_key_enrolment_then_login_without_code(page: Page, site: Site) -> None:
    """Clé FIDO2 simulée par l'authentificateur virtuel de Chromium (USB, sans code PIN) :
    inscription sur « Sécurité », puis connexion avec la clé à la place du code à 6 chiffres."""
    cdp = page.context.new_cdp_session(page)
    cdp.send("WebAuthn.enable")
    cdp.send(
        "WebAuthn.addVirtualAuthenticator",
        {
            "options": {
                "protocol": "ctap2",
                "transport": "usb",
                "hasResidentKey": False,
                "hasUserVerification": False,
                "automaticPresenceSimulation": True,
            }
        },
    )
    login(page, site)
    seen = watch(page, site.base_url)
    page.goto(f"{site.base_url}/securite")
    page.fill("input[name=nom]", "YubiKey noire")
    page.fill("#formulaire-cle input[name=mot_de_passe]", PASSWORD)
    page.click("#ajouter-cle")
    expect(page.locator("body")).to_contain_text("YubiKey noire")
    expect(page.locator("[role=status]").first).to_contain_text("Clé enregistrée")
    page.click("button:has-text('Déconnexion')")
    page.goto(f"{site.base_url}/connexion?suivant=/loyers")
    page.fill("input[name=identifiant]", USERNAME)
    page.fill("input[name=mot_de_passe]", PASSWORD)
    page.click("#connexion-cle")
    page.wait_for_url(f"{site.base_url}/loyers")
    assert seen.clean(), seen.report()


def test_assistant_answer_streams_in_without_blocking_the_page(page: Page, site: Site) -> None:
    """La question s'affiche aussitôt, l'indicateur « réfléchit » pendant que l'IA travaille,
    puis la réponse (Markdown rendu) ; sans erreur de console ni violation de CSP."""
    import time  # noqa: PLC0415

    import httpx  # noqa: PLC0415
    from pydantic import SecretStr  # noqa: PLC0415

    from app.assistant.access import ENABLED, set_switch  # noqa: PLC0415

    def slow(request: httpx.Request) -> httpx.Response:
        time.sleep(1.5)  # fournisseur lent
        text = "## Bilan\n\n- **Courses** : 120 €"
        return httpx.Response(200, json={"content": [{"type": "text", "text": text}], "usage": {}})

    site.app.state.secrets = site.app.state.secrets.model_copy(
        update={"anthropic_api_key": SecretStr("cle")}
    )
    site.app.state.ai_transport = httpx.MockTransport(slow)
    set_switch(site.engine, ENABLED, True, actor_user_id=1, now=datetime.now(UTC))
    login(page, site)
    seen = watch(page, site.base_url)
    page.goto(f"{site.base_url}/assistant")
    page.fill("textarea[name=texte]", "Bilan du mois ?")
    page.click("#formulaire-assistant button[type=submit]")
    expect(page.locator(".question").last).to_contain_text("Bilan du mois ?", timeout=1000)
    expect(page.locator(".reponse.en-cours")).to_contain_text("réfléchit", timeout=1000)
    expect(page.locator(".reponse h2")).to_have_text("Bilan", timeout=15000)
    expect(page.locator(".reponse.en-cours")).to_have_count(0)
    expect(page.locator(".conversations")).to_contain_text("Bilan du mois ?")
    assert seen.clean(), seen.report()
