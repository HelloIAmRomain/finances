"""Page « Sécurité » et connexion par clé de sécurité, de bout en bout par les routes web."""

import json

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import AuditLog
from tests.web.conftest import PASSWORD, USERNAME, Web
from tests.web.webauthn_helpers import SoftKey

ORIGIN = "https://finances.invalid"  # public_base_url par défaut des tests


def _csrf(page: str) -> str:
    return page.split('name="csrf" value="', 1)[1].split('"', 1)[0]


def _enrol(web: Web, key: SoftKey) -> None:
    csrf = _csrf(web.client.get("/securite").text)
    options = web.client.post(
        "/securite/cles/options", json={"mot_de_passe": PASSWORD}, headers={"X-CSRF-Token": csrf}
    )
    assert options.status_code == 200, options.text
    done = web.client.post(
        "/securite/cles",
        json={"credential": key.create(json.dumps(options.json()), ORIGIN), "nom": "YubiKey noire"},
        headers={"X-CSRF-Token": csrf},
    )
    assert done.status_code == 200, done.text


def test_enrol_then_log_in_with_the_key_instead_of_the_code(web: Web) -> None:
    web.login()
    key = SoftKey()
    _enrol(web, key)
    assert "YubiKey noire" in web.client.get("/securite").text
    fresh = TestClient(web.app, base_url="https://testserver")
    login_page = fresh.get("/connexion").text
    assert "Se connecter avec ma clé de sécurité" in login_page and "cle-securite.js" in login_page
    options = fresh.post(
        "/connexion/cle/options", json={"identifiant": USERNAME, "mot_de_passe": PASSWORD}
    )
    assert options.status_code == 200
    done = fresh.post(
        "/connexion/cle",
        json={"credential": key.get(json.dumps(options.json()), ORIGIN), "suivant": "/loyers"},
    )
    assert done.status_code == 200 and done.json() == {"redirection": "/loyers"}
    assert fresh.get("/loyers", follow_redirects=False).status_code == 200  # session ouverte


def test_wrong_password_or_missing_key_or_bad_answer(web: Web) -> None:
    web.login()
    fresh = TestClient(web.app, base_url="https://testserver")
    wrong = fresh.post(
        "/connexion/cle/options", json={"identifiant": USERNAME, "mot_de_passe": "x"}
    )
    none = fresh.post(
        "/connexion/cle/options", json={"identifiant": USERNAME, "mot_de_passe": PASSWORD}
    )
    # Même réponse, de même forme, que le mot de passe soit faux ou bon (sans clé enregistrée) :
    # aucun moyen de vérifier un mot de passe sans second facteur.
    for answer in (wrong, none):
        assert answer.status_code == 200
        assert set(answer.json()) == {"challenge", "timeout", "rpId", "allowCredentials",
                                      "userVerification"}  # fmt: skip
    with web.engine.connect() as connection:
        assert "login.failed" in connection.scalars(select(AuditLog.action)).all()
    for _ in range(5):  # le blocage reste annoncé (il ne dit rien du mot de passe)
        fresh.post("/connexion/cle/options", json={"identifiant": USERNAME, "mot_de_passe": "x"})
    locked = fresh.post(
        "/connexion/cle/options", json={"identifiant": USERNAME, "mot_de_passe": PASSWORD}
    )
    assert locked.status_code == 401 and "15 minutes" in locked.json()["erreur"]
    assert fresh.post("/connexion/cle", json={"credential": "x"}).status_code == 400
    assert fresh.post("/connexion/cle", content=b"pas du json").status_code == 400
    garbled = fresh.post("/connexion/cle", json={"credential": {"response": {}}})
    assert garbled.status_code == 401 and "illisible" in garbled.json()["erreur"]


def test_enrolment_requires_csrf_password_and_can_be_undone(web: Web) -> None:
    web.login()
    assert (
        web.client.post("/securite/cles/options", json={"mot_de_passe": PASSWORD}).status_code
        == 403
    )
    csrf = _csrf(web.client.get("/securite").text)
    bad = web.client.post(
        "/securite/cles/options", json={"mot_de_passe": "x"}, headers={"X-CSRF-Token": csrf}
    )
    assert bad.status_code == 400
    assert (
        web.client.post("/securite/cles", json={}, headers={"X-CSRF-Token": csrf}).status_code
        == 400
    )
    _enrol(web, SoftKey())
    page = web.client.get("/securite").text
    key_id = page.split("/securite/cles/", 1)[1].split("/", 1)[0]
    response = web.client.post(
        f"/securite/cles/{key_id}/supprimer", data={"csrf": csrf}, follow_redirects=True
    )
    assert "Aucune clé enregistrée" in response.text
    again = web.client.post(
        f"/securite/cles/{key_id}/supprimer", data={"csrf": csrf}, follow_redirects=True
    )
    assert "introuvable" in again.text
