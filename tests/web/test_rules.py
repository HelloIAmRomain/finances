"""Page « Règles » (ANA-01) : création, modification, désactivation, erreurs de saisie."""

from datetime import date

import pytest
from sqlalchemy import Engine, insert, select

from app.categorize.seed import seed_categories
from app.models import AuditLog, Categorization, Category, LedgerTransaction, Rule
from tests.web.conftest import T0, Web


def _category(engine: Engine, name: str) -> int:
    with engine.connect() as connection:
        return int(connection.scalars(select(Category.id).where(Category.name == name)).one())


def _csrf(page: str) -> str:
    return page.split('name="csrf" value="', 1)[1].split('"', 1)[0]


@pytest.fixture
def ready(web: Web) -> Web:
    seed_categories(web.engine)
    with web.engine.begin() as connection:
        connection.execute(
            insert(LedgerTransaction).values(
                id=1,
                account_id=1,
                dedupe_key="ext:test:1",
                fingerprint="x",
                occurrence=1,
                booking_date=date(2026, 9, 3),
                amount_cents=-4520,
                currency="EUR",
                label_raw="CB CARREFOUR",
                label_clean="CB CARREFOUR",
                created_at=T0,
            )
        )
    web.login()
    return web


def _create(web: Web, **fields: str) -> str:
    csrf = _csrf(web.client.get("/regles").text)
    data = {
        "csrf": csrf,
        "nom": "Courses",
        "priorite": "10",
        "categorie": str(_category(web.engine, "Courses")),
    }
    return web.client.post("/regles", data=data | fields).text


def _categories(web: Web) -> list[int]:
    with web.engine.connect() as connection:
        return list(
            connection.scalars(
                select(Categorization.category_id).where(Categorization.superseded_at.is_(None))
            )
        )


def test_create_rule_applies_immediately(ready: Web) -> None:
    page = _create(ready, contient="carrefour, auchan", sens="debit", montant_max="-10")
    assert (
        "libellé contient « carrefour » ou « auchan » et dépense et montant ≤ -10,00\u00a0€" in page
    )
    assert _categories(ready) == [_category(ready.engine, "Courses")]


def test_invalid_rules_are_refused_with_a_message(ready: Web) -> None:
    assert "au moins une condition" in _create(ready)
    assert "expression régulière invalide" in _create(ready, expression="((")


def test_edit_and_deactivate(ready: Web) -> None:
    _create(ready, contient="carrefour")
    with ready.engine.connect() as connection:
        rule_id = connection.scalars(select(Rule.id)).one()
    csrf = _csrf(ready.client.get("/regles").text)
    ready.client.post(
        f"/regles/{rule_id}/modifier",
        data={
            "csrf": csrf,
            "priorite": "5",
            "categorie": str(_category(ready.engine, "Restaurants")),
        },
    )
    assert _categories(ready) == [_category(ready.engine, "Restaurants")]
    ready.client.post(f"/regles/{rule_id}/desactiver", data={"csrf": csrf})
    assert _categories(ready) == []
    assert "(désactivée)" in ready.client.get("/regles").text
    with ready.engine.connect() as connection:
        actions = list(
            connection.scalars(select(AuditLog.action).where(AuditLog.action.like("rule.%")))
        )
    assert actions == ["rule.created", "rule.updated", "rule.deactivated"]
