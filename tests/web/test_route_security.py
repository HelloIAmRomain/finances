"""Protection de chaque route, énumérées automatiquement (NF-SEC-04).

Toute nouvelle page ou API est couverte d'office : sans session (ou sans jeton d'appareil pour
l'API v1, sans jeton d'IA pour /mcp) elle renvoie vers la connexion ou répond 401 ; avec une
session mais sans jeton CSRF, toute écriture est refusée (403). Seules les routes qui servent à se
connecter ou à appairer sont publiques, et la liste en est figée ici.
"""

import re

import pytest
from fastapi.routing import APIRoute

from app.web.main import ROUTERS
from tests.web.conftest import Web

PUBLIC = {
    ("GET", "/connexion"),
    ("POST", "/connexion"),
    ("POST", "/connexion/cle/options"),
    ("POST", "/connexion/cle"),
    ("GET", "/connexions/retour"),  # retour de la banque : état signé et nonce à usage unique
    ("POST", "/api/v1/appairage"),  # code d'appairage à usage unique, tentatives limitées
}
# Toujours 405, sans rien lire (MCP en HTTP « streamable » sans flux SSE).
REFUSED = {("GET", "/mcp"), ("DELETE", "/mcp")}
BEARER_PREFIXES = ("/api/v1/", "/mcp")  # jeton d'appareil ou d'IA, jamais le cookie de session
SAFE = {"GET", "HEAD", "OPTIONS"}


def _routes() -> list[tuple[str, str]]:
    found = {
        (method, re.sub(r"\{[^}]+\}", "1", route.path))
        for router in ROUTERS
        for route in router.routes
        if isinstance(route, APIRoute)
        for method in route.methods or ()
    }
    return sorted(found)


ROUTES = _routes()
PROTECTED = [route for route in ROUTES if route not in PUBLIC | REFUSED]


def test_public_routes_still_exist() -> None:
    # Une route publique renommée ne doit pas laisser une entrée morte dans la liste blanche.
    assert PUBLIC | REFUSED <= set(ROUTES)
    assert len(PROTECTED) > 100


@pytest.mark.parametrize(("method", "path"), sorted(REFUSED))
def test_refused_methods_answer_nothing(web: Web, method: str, path: str) -> None:
    assert web.client.request(method, path).status_code == 405


@pytest.mark.parametrize(("method", "path"), PROTECTED)
def test_every_route_requires_authentication(web: Web, method: str, path: str) -> None:
    response = web.client.request(method, path, follow_redirects=False)
    if path.startswith(BEARER_PREFIXES):
        assert response.status_code == 401, response.text
    else:
        assert response.status_code == 303, response.text
        assert response.headers["location"].startswith("/connexion?suivant=")


@pytest.mark.parametrize(("method", "path"), [r for r in PROTECTED if r[1].startswith("/api/v1/")])
def test_session_cookie_does_not_open_the_api(web: Web, method: str, path: str) -> None:
    web.login()
    assert web.client.request(method, path, follow_redirects=False).status_code == 401


@pytest.mark.parametrize(
    ("method", "path"),
    [r for r in PROTECTED if r[0] not in SAFE and not r[1].startswith(BEARER_PREFIXES)],
)
def test_every_write_requires_the_csrf_token(web: Web, method: str, path: str) -> None:
    web.login()
    for headers in ({}, {"X-CSRF-Token": "faux"}):
        response = web.client.request(method, path, headers=headers, follow_redirects=False)
        assert response.status_code == 403, response.text
    form = web.client.request(method, path, data={"csrf": "faux"}, follow_redirects=False)
    assert form.status_code == 403
