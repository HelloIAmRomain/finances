"""Page « Budgets » (ANA-06) : définir, suivre, supprimer ; tuile du tableau de bord."""

from datetime import date

from sqlalchemy import Engine, insert, select

from app.categorize.seed import seed_categories
from app.categorize.service import categorize_manually
from app.models import Alert, Budget, Category, LedgerTransaction
from tests.web.conftest import T0, Web

_ids = iter(range(1, 100_000))


def _csrf(page: str) -> str:
    return page.split('name="csrf" value="', 1)[1].split('"', 1)[0]


def _cat(engine: Engine, name: str) -> int:
    with engine.connect() as connection:
        return int(connection.scalars(select(Category.id).where(Category.name == name)).one())


def _spend(engine: Engine, amount: int, day: date, category: str) -> None:
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
                label_raw="CB RESTO",
                label_clean="RESTO",
                created_at=T0,
            )
        )
    categorize_manually(
        engine, tx_id, [(_cat(engine, category), amount)], actor_user_id=None, now=T0
    )


def _post(web: Web, path: str, **form: str) -> str:
    csrf = _csrf(web.client.get("/budgets").text)
    response = web.client.post(path, data={"csrf": csrf, **form}, follow_redirects=False)
    assert response.status_code == 303, response.text
    return web.client.get(response.headers["location"]).text


def test_set_follow_and_remove_a_budget(web: Web) -> None:
    seed_categories(web.engine)
    web.login()
    _spend(web.engine, -12_000, date(2026, 6, 10), "Restaurants")  # moyenne de juin à août
    _spend(web.engine, -9_000, date(2026, 9, 3), "Restaurants")
    empty = web.client.get("/budgets").text
    assert "Aucun budget" in empty
    assert "moyenne" in web.client.get("/budgets?mois=2026-09").text

    page = _post(web, "/budgets", categorie=str(_cat(web.engine, "Alimentation")), montant="100")
    assert "Budget enregistré" in page
    assert "90 %" in page  # 90 € sur 100 € en septembre (mois de T0)
    with web.engine.connect() as connection:
        assert connection.execute(select(Alert.title)).scalar_one() == (
            "Budget « Alimentation » : 80 % atteint"
        )
    dashboard = web.client.get("/").text
    assert "Budgets" in dashboard and "90 %" in dashboard
    assert "Ce mois-ci" in web.client.get("/budgets?mois=2026-13").text  # mois invalide ignoré

    with web.engine.connect() as connection:
        budget_id = connection.execute(select(Budget.id)).scalar_one()
    assert "Budget supprimé" in _post(web, f"/budgets/{budget_id}/supprimer")
    assert "Budget introuvable" in _post(web, f"/budgets/{budget_id}/supprimer")


def test_errors_are_explained(web: Web) -> None:
    seed_categories(web.engine)
    web.login()
    courses = str(_cat(web.engine, "Courses"))
    assert "Montant illisible" in _post(web, "/budgets", categorie=courses, montant="beaucoup")
    assert "positif" in _post(web, "/budgets", categorie=courses, montant="0")
    salary = str(_cat(web.engine, "Salaire"))
    assert "catégorie de dépenses" in _post(web, "/budgets", categorie=salary, montant="10")
