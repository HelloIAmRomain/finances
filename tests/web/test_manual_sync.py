"""Synchro manuelle depuis la page « Comptes » (inv. 10)."""

from collections.abc import Iterator
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import Engine, select

from app.models import Account
from tests.web.bank_helpers import bank_web_session, csrf_of, link_bank
from tests.web.conftest import Web


@pytest.fixture
def linked(engine: Engine) -> Iterator[tuple[Web, int]]:
    with bank_web_session(engine) as (web, fake):
        link_bank(web, fake)
        with web.engine.connect() as connection:
            account_id = connection.scalars(
                select(Account.id).where(Account.name == "Compte courant")
            ).one()
        yield web, account_id


def _message(response_location: str) -> str:
    return parse_qs(urlparse(response_location).query)["message"][0]


def test_manual_sync_then_too_soon(linked: tuple[Web, int]) -> None:
    web, account_id = linked
    page = web.client.get("/comptes").text
    assert f"/comptes/{account_id}/synchroniser" in page
    first = web.client.post(
        f"/comptes/{account_id}/synchroniser", data={"csrf": csrf_of(page)}, follow_redirects=False
    )
    assert _message(first.headers["location"]) == "mise à jour terminée"
    second = web.client.post(
        f"/comptes/{account_id}/synchroniser", data={"csrf": csrf_of(page)}, follow_redirects=False
    )
    assert "possible dans" in _message(second.headers["location"])


def test_manual_sync_requires_csrf(linked: tuple[Web, int]) -> None:
    web, account_id = linked
    assert web.client.post(f"/comptes/{account_id}/synchroniser").status_code == 403


def test_button_disabled_when_budget_only_covers_next_automatic_sync(
    linked: tuple[Web, int],
) -> None:
    """Une synchro de 2 pages sur un plafond de 4 : une manuelle ne laisserait rien au créneau
    automatique suivant, le bouton est désactivé avec son explication."""
    web, account_id = linked
    page = web.client.get("/comptes").text
    assert "disabled" not in page
    web.client.post(f"/comptes/{account_id}/synchroniser", data={"csrf": csrf_of(page)})
    web.clock.current += timedelta(minutes=20)
    page = web.client.get("/comptes").text
    assert f'disabled aria-describedby="refus-{account_id}"' in page
    assert (
        "Mise à jour manuelle indisponible" in page and "mises à jour manuelles comprises" in page
    )
    refused = web.client.post(
        f"/comptes/{account_id}/synchroniser", data={"csrf": csrf_of(page)}, follow_redirects=False
    )
    assert "Mise à jour manuelle indisponible" in _message(refused.headers["location"])


def _row(page: str, name: str) -> str:
    return next(row for row in page.split("<tr>") if f"<td>{name}<br>" in row)


def test_accounts_page_shows_connector_state(engine: Engine) -> None:
    """COL-07 : dernière erreur, retard de synchro et accès consommés sur 24 h."""
    with bank_web_session(engine) as (web, fake):
        link_bank(web, fake)
        with web.engine.connect() as connection:
            account_id = connection.scalars(
                select(Account.id).where(Account.name == "Compte courant")
            ).one()
        page = web.client.get("/comptes").text
        row = _row(page, "Compte courant")
        assert "aucune mise à jour réussie depuis 24 h" in row
        assert "accès sur 24 h : soldes 0/4, transactions 0/4" in row
        web.client.post(f"/comptes/{account_id}/synchroniser", data={"csrf": csrf_of(page)})
        page = web.client.get("/comptes").text
        row = _row(page, "Compte courant")
        assert "aucune mise à jour réussie depuis 24 h" not in row
        assert "soldes 1/4, transactions 2/4" in row
        web.clock.current += timedelta(hours=25)
        web.login()  # la session a expiré entre-temps
        page = web.client.get("/comptes").text
        uid = next(r.url.path for r in fake.requests if r.url.path.endswith("/balances"))
        fake.status[uid.replace("/balances", "/transactions")] = 429
        web.client.post(f"/comptes/{account_id}/synchroniser", data={"csrf": csrf_of(page)})
        page = web.client.get("/comptes").text
        row = _row(page, "Compte courant")
        assert "aucune mise à jour réussie depuis 24 h" in row
        assert "reportée : transactions : limite de requêtes atteinte (429)" in row
