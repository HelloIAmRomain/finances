"""Page « Prévision » (ANA-10, ALR-07)."""

from datetime import date

from sqlalchemy import insert, select

from app.models import Account, BalanceSnapshot
from app.models.ledger import BalanceType
from tests.web.conftest import T0, Web


def _csrf(page: str) -> str:
    return page.split('name="csrf" value="', 1)[1].split('"', 1)[0]


def test_forecast_page_and_threshold(web: Web) -> None:
    web.login()
    page = web.client.get("/prevision").text
    assert "Solde inconnu" in page
    assert "Aucune opération régulière" in page
    with web.engine.begin() as connection:
        connection.execute(
            insert(BalanceSnapshot).values(
                account_id=1, as_of_date=date(2026, 9, 27), balance_type=BalanceType.BOOKED,
                amount_cents=123_400, currency="EUR", fetched_at=T0,
            )
        )  # fmt: skip
    page = web.client.get("/prevision?jours=90").text
    assert "<strong>90 jours</strong>" in page and "Dans 90 jours" in page
    assert "Comptes suivis, dans 90 jours" in page
    csrf = _csrf(page)
    response = web.client.post(
        "/prevision/1/seuil", data={"csrf": csrf, "seuil": "500,00"}, follow_redirects=True
    )
    assert "Seuil enregistré" in response.text
    with web.engine.connect() as connection:
        assert connection.scalar(select(Account.low_balance_cents).where(Account.id == 1)) == 50_000
    response = web.client.post(
        "/prevision/1/seuil", data={"csrf": csrf, "seuil": "abc"}, follow_redirects=True
    )
    assert "Montant illisible" in response.text
    response = web.client.post(
        "/prevision/1/seuil", data={"csrf": csrf, "seuil": ""}, follow_redirects=True
    )
    assert "Alerte de solde bas désactivée" in response.text
    response = web.client.post(
        "/prevision/99/seuil", data={"csrf": csrf, "seuil": "1"}, follow_redirects=True
    )
    assert "Compte introuvable" in response.text
    assert "<strong>30 jours</strong>" in web.client.get("/prevision?jours=7").text
