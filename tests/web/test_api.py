"""API JSON v1 de l'application Android : appairage, jeton d'appareil, lecture et décisions."""

import re
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, insert, select

from app.alerts.engine import raise_alert
from app.auth.devices import IDLE_EXPIRY, MAX_FAILURES
from app.categorize.seed import seed_categories
from app.models import AssistantConversation, AuditLog, Category, LedgerTransaction, PairingCode
from app.models.alerts import Severity
from app.models.rental import LeaseKind, PartyRole
from app.rental.engine import run_rental
from app.rental.leases import LeaseTerms, add_party, create_lease
from tests.web.bank_helpers import bank_web_session, csrf_of, link_bank
from tests.web.conftest import PASSWORD, T0, Web


def _code(web: Web) -> str:
    csrf = csrf_of(web.client.get("/appareils").text)
    page = web.client.post("/appareils/code", data={"csrf": csrf, "mot_de_passe": PASSWORD}).text
    return page.split('class="code-appairage">', 1)[1].split("<", 1)[0]


def test_pairing_code_requires_the_password(web: Web) -> None:
    web.login()
    csrf = csrf_of(web.client.get("/appareils").text)
    refused = web.client.post("/appareils/code", data={"csrf": csrf, "mot_de_passe": "faux"})
    assert "Identifiants incorrects" in refused.text
    assert 'class="code-appairage"' not in refused.text
    with web.engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(PairingCode)) == 0
        assert "login.failed" in connection.scalars(select(AuditLog.action)).all()


def _paired(web: Web, name: str = "Pixel de test") -> dict[str, str]:
    response = web.client.post("/api/v1/appairage", json={"code": _code(web), "nom": name})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['jeton']}"}


@pytest.fixture
def api(web: Web) -> tuple[Web, dict[str, str]]:
    web.login()
    return web, _paired(web)


def test_pairing_is_single_use_and_revocable(web: Web) -> None:
    web.login()
    code = _code(web)
    assert len(code) == 9 and code[4] == "-"
    anonymous = TestClient(web.app, base_url="https://testserver")
    first = anonymous.post("/api/v1/appairage", json={"code": code.lower(), "nom": "Téléphone"})
    assert first.status_code == 200
    headers = {"Authorization": f"Bearer {first.json()['jeton']}"}
    assert anonymous.get("/api/v1/moi", headers=headers).json()["version"] == 1
    again = anonymous.post("/api/v1/appairage", json={"code": code, "nom": "Autre"})
    assert again.status_code == 403
    page = web.client.get("/appareils").text
    assert "Téléphone" in page
    [device] = re.findall(r"/appareils/(\d+)/revoquer", page)
    web.client.post(f"/appareils/{device}/revoquer", data={"csrf": csrf_of(page)})
    assert anonymous.get("/api/v1/moi", headers=headers).status_code == 401
    assert anonymous.get("/api/v1/moi").status_code == 401
    assert anonymous.get("/api/v1/moi", headers={"Authorization": "Bearer faux"}).status_code == 401
    again = web.client.post(f"/appareils/{device}/revoquer", data={"csrf": csrf_of(page)})
    # Un refus s'affiche comme une erreur (alerte rouge), jamais comme une confirmation.
    refusal = again.text.split("déjà révoqué")[0].rsplit("<p ", 1)[1]
    assert 'class="negatif" role="alert"' in refusal


def test_device_token_expires_after_long_inactivity(web: Web) -> None:
    web.login()
    headers = _paired(web)
    anonymous = TestClient(web.app, base_url="https://testserver")
    web.clock.current += IDLE_EXPIRY - timedelta(minutes=1)
    assert anonymous.get("/api/v1/moi", headers=headers).status_code == 200  # utilisé : prolongé
    web.clock.current += IDLE_EXPIRY - timedelta(minutes=1)
    assert anonymous.get("/api/v1/moi", headers=headers).status_code == 200
    web.clock.current += IDLE_EXPIRY + timedelta(minutes=1)
    assert anonymous.get("/api/v1/moi", headers=headers).status_code == 401
    web.login()
    assert "appairez-le de nouveau" in web.client.get("/appareils").text


def test_pairing_code_expires_and_brute_force_is_stopped(web: Web) -> None:
    web.login()
    code = _code(web)
    web.clock.current += timedelta(minutes=11)
    assert web.client.post("/api/v1/appairage", json={"code": code, "nom": "x"}).status_code == 403
    for _ in range(MAX_FAILURES):
        web.client.post("/api/v1/appairage", json={"code": "AAAA-AAAA", "nom": "x"})
    fresh = _code(web)
    blocked = web.client.post("/api/v1/appairage", json={"code": fresh, "nom": "x"})
    assert blocked.status_code == 403 and "Trop d'essais" in blocked.json()["detail"]
    web.clock.current += timedelta(minutes=11)
    assert (
        web.client.post("/api/v1/appairage", json={"code": _code(web), "nom": "x"}).status_code
        == 200
    )
    with web.engine.connect() as connection:
        actions = list(connection.scalars(select(AuditLog.action)))
    assert actions.count("device.pairing_failed") >= MAX_FAILURES and "device.paired" in actions


def _tx(engine: Engine, amount: int, label: str, day: date) -> int:
    with engine.begin() as connection:
        result = connection.execute(
            insert(LedgerTransaction).values(
                account_id=1,
                dedupe_key=f"ext:api:{label}:{amount}",
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
    return int(result.inserted_primary_key[0])  # type: ignore[index]


def test_dashboard_transactions_and_categorization(api: tuple[Web, dict[str, str]]) -> None:
    web, headers = api
    seed_categories(web.engine)
    tx = _tx(web.engine, -4520, "SUPERMARCHE DEMO", date(2026, 9, 3))
    _tx(web.engine, 250000, "SALAIRE DEMO", date(2026, 9, 1))
    board = web.client.get("/api/v1/tableau-de-bord?mois=2026-09", headers=headers).json()
    assert (board["recettes"], board["depenses"], board["solde"]) == (250000, -4520, 245480)
    assert len(board["flux_12_mois"]) == 12 and board["loyers"] == []
    assert web.client.get("/api/v1/tableau-de-bord?mois=sept", headers=headers).status_code == 422
    found = web.client.get("/api/v1/transactions?texte=supermarche", headers=headers).json()
    assert found["total"] == 1 and found["transactions"][0]["montant"] == -4520
    with web.engine.connect() as connection:
        courses = connection.scalars(select(Category.id).where(Category.name == "Courses")).one()
    assert any(
        c["id"] == courses for c in web.client.get("/api/v1/categories", headers=headers).json()
    )
    ok = web.client.post(
        f"/api/v1/transactions/{tx}/categorie", json={"categorie": courses}, headers=headers
    )
    assert ok.status_code == 200
    again = web.client.get("/api/v1/transactions?texte=supermarche", headers=headers).json()
    assert again["transactions"][0]["categories"] == ["Alimentation › Courses"]
    assert (
        web.client.post(
            "/api/v1/transactions/999/categorie", json={"categorie": courses}, headers=headers
        ).status_code
        == 404
    )
    assert (
        web.client.post(
            f"/api/v1/transactions/{tx}/categorie", json={"categorie": 99999}, headers=headers
        ).status_code
        == 422
    )


def test_rental_and_alerts(api: tuple[Web, dict[str, str]]) -> None:
    web, headers = api
    lease = create_lease(
        web.engine,
        LeaseTerms(
            kind=LeaseKind.LOAN,
            label="Prêt",
            account_id=1,
            start_date=date(2026, 9, 1),
            rent_cents=20000,
        ),
        now=T0,
    )
    add_party(web.engine, lease, role=PartyRole.BORROWER, name="Chloé Démo")
    _tx(web.engine, 20000, "VIR INCONNU", date(2026, 9, 2))
    run_rental(web.engine, now=T0)
    data = web.client.get("/api/v1/loyers", headers=headers).json()
    september = next(d for d in data["baux"][0]["echeances"] if d["mois"] == "2026-09")
    assert data["baux"][0]["nom"] == "Prêt" and september["etat"] == "impaye"
    [proposal] = data["a_valider"]
    confirmed = web.client.post(
        f"/api/v1/loyers/propositions/{proposal['id']}/confirmer", headers=headers
    )
    assert confirmed.status_code == 200
    data = web.client.get("/api/v1/loyers", headers=headers).json()
    september = next(d for d in data["baux"][0]["echeances"] if d["mois"] == "2026-09")
    assert september["etat"] == "paye" and data["a_valider"] == []
    assert (
        web.client.post(
            f"/api/v1/loyers/propositions/{proposal['id']}/refuser", headers=headers
        ).status_code
        == 409
    )
    raise_alert(
        web.engine, type="test", severity=Severity.WARNING, key="k", title="Alerte de test", now=T0
    )
    [alert] = web.client.get("/api/v1/alertes", headers=headers).json()
    assert alert["titre"] == "Alerte de test" and alert["etat"] == "open"
    assert web.client.post(f"/api/v1/alertes/{alert['id']}/vue", headers=headers).status_code == 200
    assert (
        web.client.post(f"/api/v1/alertes/{alert['id']}/resoudre", headers=headers).status_code
        == 200
    )
    assert web.client.get("/api/v1/alertes", headers=headers).json() == []
    assert len(web.client.get("/api/v1/alertes?toutes=true", headers=headers).json()) >= 1
    assert (
        web.client.post(f"/api/v1/alertes/{alert['id']}/resoudre", headers=headers).status_code
        == 409
    )


def test_accounts_and_manual_sync(engine: Engine) -> None:
    with bank_web_session(engine) as (web, fake):
        link_bank(web, fake)
        headers = _paired(web)
        accounts = web.client.get("/api/v1/comptes", headers=headers).json()
        current = next(a for a in accounts if a["nom"] == "Compte courant")
        assert current["connexion"] == "active" and current["synchro_manuelle_impossible"] is None
        done = web.client.post(f"/api/v1/comptes/{current['id']}/synchroniser", headers=headers)
        assert done.status_code == 200 and done.json()["message"] == "Mise à jour terminée"
        too_soon = web.client.post(f"/api/v1/comptes/{current['id']}/synchroniser", headers=headers)
        assert too_soon.status_code == 429
        unlinked = next(a for a in accounts if a["banque"] is None)
        assert unlinked["synchro_manuelle_impossible"]


def test_lease_can_be_renamed_from_the_phone(api: tuple[Web, dict[str, str]]) -> None:
    web, headers = api
    lease = create_lease(
        web.engine,
        LeaseTerms(
            kind=LeaseKind.LOAN, label="Prêt", account_id=1, start_date=date(2026, 9, 1),
            rent_cents=20000,
        ),
        now=T0,
    )  # fmt: skip
    renamed = web.client.post(
        f"/api/v1/loyers/{lease}/renommer", json={"nom": "  Prêt   à C. "}, headers=headers
    )
    assert renamed.status_code == 200 and renamed.json()["message"] == "Renommé en « Prêt à C. »"
    assert web.client.get("/api/v1/loyers", headers=headers).json()["baux"][0]["nom"] == "Prêt à C."
    empty = web.client.post(f"/api/v1/loyers/{lease}/renommer", json={"nom": " "}, headers=headers)
    assert empty.status_code == 422 and "Nom vide" in empty.json()["detail"]
    too_long = web.client.post(
        f"/api/v1/loyers/{lease}/renommer", json={"nom": "x" * 129}, headers=headers
    )
    assert too_long.status_code == 422
    missing = web.client.post("/api/v1/loyers/999/renommer", json={"nom": "X"}, headers=headers)
    assert missing.status_code == 404
    with web.engine.connect() as connection:
        assert "lease.updated" in set(connection.scalars(select(AuditLog.action)))
    anonymous = web.client.post(f"/api/v1/loyers/{lease}/renommer", json={"nom": "Pirate"})
    assert anonymous.status_code == 401


def test_conversation_older_than_the_hundred_listed_still_opens(
    api: tuple[Web, dict[str, str]],
) -> None:
    """La liste s'arrête aux 100 plus récentes : en ouvrir une plus ancienne donnait une 500."""
    web, headers = api
    with web.engine.begin() as connection:
        for k in range(101):
            connection.execute(
                insert(AssistantConversation).values(
                    user_id=1, messages_enc="[]", title=f"Question {k}",
                    created_at=T0 + timedelta(minutes=k), updated_at=T0 + timedelta(minutes=k),
                )
            )  # fmt: skip
    oldest = web.client.get("/api/v1/assistant/conversations/1", headers=headers)
    assert oldest.status_code == 200 and oldest.json()["titre"] == "Question 0"
    assert "Question 0" in web.client.get("/assistant?conversation=1").text
    missing = web.client.get("/api/v1/assistant/conversations/999", headers=headers)
    assert missing.status_code == 404
