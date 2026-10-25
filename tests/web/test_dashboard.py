"""Tableau de bord (ANA-09)."""

import json
import re
from datetime import date

from sqlalchemy import Engine, insert, select

from app.categorize.seed import seed_categories
from app.categorize.service import categorize_manually
from app.models import Category, LedgerTransaction
from tests.web.conftest import T0, Web

_ids = iter(range(1, 100_000))


def _tx(engine: Engine, amount: int, day: date) -> int:
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
                label_raw="OP",
                label_clean="OP",
                created_at=T0,
            )
        )
    return tx_id


def test_dashboard_for_current_month(web: Web) -> None:
    seed_categories(web.engine)
    with web.engine.connect() as connection:
        courses = connection.scalars(select(Category.id).where(Category.name == "Courses")).one()
    _tx(web.engine, 250000, date(2026, 9, 1))
    groceries = _tx(web.engine, -4520, date(2026, 9, 3))
    categorize_manually(web.engine, groceries, [(courses, -4520)], actor_user_id=None, now=T0)
    _tx(web.engine, -100000, date(2025, 9, 3))
    web.login()
    page = web.client.get("/").text
    assert "Septembre 2026" in page
    assert "2\u202f500,00\u00a0€" in page and "-45,20\u00a0€" in page
    assert "septembre 2025 : -1\u202f000,00\u00a0€" in page  # comparaison N-1
    assert "Taux d'épargne : 98,2 %" in page  # 245 480 / 250 000 = 98,19 %
    data = json.loads(re.search(r'id="donnees-graphiques">(.*?)</script>', page, re.S).group(1))  # type: ignore[union-attr]
    assert len(data["mois"]) == 12 and data["mois"][-1] == "septembre 2026"
    assert data["recettes"][-1] == 250000 and data["depenses"][-1] == 4520
    assert data["categories"] == ["Alimentation"]


def test_month_navigation_and_invalid_month(web: Web) -> None:
    web.login()
    assert "Août 2026" in web.client.get("/?mois=2026-08").text
    assert "Septembre 2026" in web.client.get("/?mois=2026-13").text  # mois invalide : mois courant
    assert "Aucune dépense ce mois-ci." in web.client.get("/?mois=2026-08").text


def test_chart_data_cannot_close_the_script_tag(web: Web) -> None:
    """Un nom de catégorie (saisi, ou créé par l'IA) ne peut pas sortir du bloc JSON."""
    with web.engine.begin() as connection:
        connection.execute(
            insert(Category).values(
                name="</script><script>alert(1)</script>", kind="expense", sort_order=1
            )
        )
        evil = connection.scalar(select(Category.id))
    expense = _tx(web.engine, -1000, date(2026, 9, 3))
    categorize_manually(web.engine, expense, [(int(evil or 0), -1000)], actor_user_id=None, now=T0)
    web.login()
    page = web.client.get("/").text
    block = page.split('id="donnees-graphiques">', 1)[1].split("</script>", 1)[0]
    assert "\\u003c/script\\u003e" in block and "<" not in block
    assert json.loads(block)["categories"] == ["</script><script>alert(1)</script>"]
