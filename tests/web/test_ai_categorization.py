"""Catégorisation par IA côté web (ANA-02, ANA-03) : réglage, suggestions « À valider »."""

from datetime import date

from sqlalchemy import Engine, insert, select

from app.categorize.ai import settings
from app.categorize.seed import seed_categories
from app.models import Categorization, Category, LedgerTransaction, ReviewItem, Rule
from app.models.categorize import CategorizationSource, ReviewKind, ReviewStatus
from tests.web.conftest import T0, Web


def _csrf(page: str) -> str:
    return page.split('name="csrf" value="', 1)[1].split('"', 1)[0]


def _cat(engine: Engine, name: str) -> int:
    with engine.connect() as connection:
        return int(connection.scalars(select(Category.id).where(Category.name == name)).one())


def _suggestion(engine: Engine, tx_id: int, label: str, details: dict[str, int]) -> int:
    with engine.begin() as connection:
        connection.execute(
            insert(LedgerTransaction).values(
                id=tx_id, account_id=1, dedupe_key=f"ext:t:{tx_id}", fingerprint="x",
                occurrence=1, booking_date=date(2026, 9, 20), amount_cents=-1500,
                currency="EUR", label_raw=label, label_clean=label, created_at=T0,
            )
        )  # fmt: skip
        result = connection.execute(
            insert(ReviewItem).values(
                kind=ReviewKind.AI_SUGGESTION, dedupe_key=f"ai:{tx_id}", transaction_id=tx_id,
                status=ReviewStatus.OPEN, details=details, created_at=T0,
            )
        )  # fmt: skip
    return int(result.inserted_primary_key[0])  # type: ignore[index]


def _post(web: Web, path: str, **form: str) -> None:
    csrf = _csrf(web.client.get("/a-valider").text)
    response = web.client.post(path, data={"csrf": csrf, **form}, follow_redirects=False)
    assert response.status_code == 303, response.text


def _manual(engine: Engine, tx_id: int) -> int | None:
    with engine.connect() as connection:
        return connection.scalar(
            select(Categorization.category_id).where(
                Categorization.transaction_id == tx_id,
                Categorization.superseded_at.is_(None),
                Categorization.source == CategorizationSource.MANUAL,
            )
        )


def test_review_page_accept_choose_dismiss_and_rule(web: Web) -> None:
    seed_categories(web.engine)
    web.login()
    gifts = _cat(web.engine, "Cadeaux")
    first = _suggestion(
        web.engine, 1, "CB BOUTIQUE CADEAUX 12/09", {"categorie": gifts, "confiance": 550}
    )
    second = _suggestion(web.engine, 2, "CB INCONNU", {})
    third = _suggestion(web.engine, 3, "CB AUTRE", {})
    page = web.client.get("/a-valider").text
    assert "L'IA propose <strong>Achats › Cadeaux</strong> (confiance 55 %)" in page
    assert "L'IA n'a pas trouvé de catégorie" in page

    _post(web, f"/a-valider/suggestions/{first}", decision="accepter", regle="1")
    assert _manual(web.engine, 1) == gifts
    with web.engine.connect() as connection:
        rule = connection.execute(select(Rule)).one()
    assert rule.category_id == gifts and rule.conditions["label_contains"] == [
        "CB BOUTIQUE CADEAUX"
    ]

    sport = _cat(web.engine, "Sport")
    _post(web, f"/a-valider/suggestions/{second}", decision="autre", categorie=str(sport))
    assert _manual(web.engine, 2) == sport
    _post(web, f"/a-valider/suggestions/{third}", decision="autre", categorie="")  # sans choix
    _post(web, f"/a-valider/suggestions/{third}", decision="accepter")  # rien à accepter
    assert _manual(web.engine, 3) is None
    _post(web, f"/a-valider/suggestions/{third}", decision="ignorer")
    _post(web, f"/a-valider/suggestions/{third}", decision="ignorer")  # déjà traitée
    assert "Rien à valider" in web.client.get("/a-valider").text


def test_security_page_setting(web: Web) -> None:
    web.login()
    page = web.client.get("/securite").text
    assert "Catégorisation automatique par l'IA" in page
    csrf = _csrf(page)
    response = web.client.post(
        "/securite/ia/categorisation",
        data={"csrf": csrf, "actif": "1", "seuil": "70"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert settings(web.engine) == {"categorisation_auto": True, "seuil_confiance": 700}
    response = web.client.post(
        "/securite/ia/categorisation", data={"csrf": csrf, "seuil": "150"}, follow_redirects=True
    )
    assert "Seuil de confiance entre 0 et 100" in response.text
    assert settings(web.engine)["categorisation_auto"] is True
