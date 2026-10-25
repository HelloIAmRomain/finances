"""Page « Abonnements » (ANA-07)."""

from datetime import date, timedelta

from sqlalchemy import insert

from app.models import LedgerTransaction
from tests.web.conftest import T0, Web


def _tx(web: Web, tx_id: int, day: date, amount: int, label: str) -> None:
    with web.engine.begin() as connection:
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


def test_empty_then_listed_with_price_change_and_missed(web: Web) -> None:
    web.login()
    assert "Aucune dépense régulière détectée" in web.client.get("/abonnements").text
    for k, amount in enumerate([-1099, -1099, -1099, -1299]):  # dernier : 15/09 (T0 : 27/09)
        _tx(web, 1 + k, date(2026, 9, 15) - timedelta(days=30 * (3 - k)), amount, "PRLV MUSIQUE")
    for k in range(3):  # attendu le 05/09 : manqué
        _tx(web, 10 + k, date(2026, 8, 6) - timedelta(days=30 * k), -4500, "PRLV ASSURANCE")
    page = web.client.get("/abonnements").text
    assert "PRLV MUSIQUE" in page
    assert "hausse depuis le" in page
    assert "Attendus et non reçus" in page and "PRLV ASSURANCE" in page
    assert 'href="/abonnements" aria-current="page"' in page
