"""Outils des tests web qui passent par le faux serveur Enable Banking (liaison, synchro)."""

import html
import json
import re
from collections.abc import Iterator
from contextlib import contextmanager

from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.auth.users import create_user
from app.connectors.enable_banking.client import EnableBankingClient
from app.connectors.requester import BankRequester
from app.net.client import make_client
from app.web.main import create_app
from tests.connectors.eb_fake import FakeEnableBanking, credentials
from tests.web.conftest import PASSWORD, T0, USERNAME, Clock, Web, fake_secrets


@contextmanager
def bank_web_session(engine: Engine) -> Iterator[tuple[Web, FakeEnableBanking]]:
    clock = Clock()
    fake = FakeEnableBanking()
    requester = BankRequester(
        make_client(transport=fake.transport()), sleep=lambda _: None, now=clock
    )
    bank = EnableBankingClient(requester, credentials(), now=clock)
    app = create_app(secrets=fake_secrets(), engine=engine, now=clock, bank=bank)
    secret = create_user(engine, username=USERNAME, password=PASSWORD, now=T0)
    with TestClient(app, base_url="https://testserver") as client:
        web = Web(app=app, client=client, clock=clock, engine=engine, totp_secret=secret)
        web.login()
        yield web, fake


def csrf_of(page: str) -> str:
    return page.split('name="csrf" value="', 1)[1].split('"', 1)[0]


def start_link(web: Web) -> str:
    csrf = csrf_of(web.client.get("/connexions").text)
    response = web.client.post(
        "/connexions/lier", data={"csrf": csrf, "banque": "Banque Exemple"}, follow_redirects=False
    )
    # Page intermédiaire, pas de 303 : la CSP `form-action 'self'` bloquerait la redirection.
    assert response.status_code == 200
    assert "samesite=lax" in response.headers["set-cookie"].lower()
    assert "form-action 'self'" in response.headers["content-security-policy"]
    url = html.unescape(re.search(r'href="(https://[^"]+)"', response.text).group(1))  # type: ignore[union-attr]
    assert f'content="0;url={html.escape(url)}"' in response.text  # rafraîchissement immédiat
    return url


def last_state(fake: FakeEnableBanking) -> str:
    auth = [r for r in fake.requests if r.url.path == "/auth"][-1]
    body = json.loads(auth.content)
    assert body["redirect_url"] == "https://finances.invalid/connexions/retour"
    assert body["access"]["valid_until"].startswith("2027-03-26")  # 180 jours (validité maximale)
    return str(body["state"])


def bank_callback(web: Web, state: str) -> int:
    """Retour de la banque : sans cookie de session (Strict), avec le cookie de liaison (Lax)."""
    web.client.cookies.delete("session")
    response = web.client.get(
        f"/connexions/retour?code=code-factice&state={state}", follow_redirects=False
    )
    web.login()
    return response.status_code


def link_bank(web: Web, fake: FakeEnableBanking) -> None:
    start_link(web)
    assert bank_callback(web, last_state(fake)) == 303
