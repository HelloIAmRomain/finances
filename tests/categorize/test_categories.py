"""Arborescence des catégories (deux niveaux) : création, renommage, déplacement, refus."""

from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, select

from app.categorize.categories import CategoryError, create_category, move_category, rename_category
from app.categorize.seed import seed_categories
from app.models import AuditLog, Category
from app.models.categorize import CategoryKind

NOW = datetime(2026, 9, 30, tzinfo=UTC)


def _id(engine: Engine, name: str) -> int:
    with engine.connect() as connection:
        return int(connection.scalar(select(Category.id).where(Category.name == name)) or 0)


def test_create_rename_move(engine: Engine) -> None:
    seed_categories(engine)
    root = create_category(
        engine,
        name="  Animaux ",
        parent_id=None,
        kind=CategoryKind.EXPENSE,
        actor_user_id=None,
        now=NOW,
    )
    child = create_category(
        engine, name="Véto", parent_id=root, kind=None, actor_user_id=None, now=NOW
    )
    rename_category(engine, child, "Vétérinaire", actor_user_id=None, now=NOW)
    move_category(engine, child, _id(engine, "Santé"), actor_user_id=None, now=NOW)
    move_category(
        engine, child, None, actor_user_id=None, now=NOW
    )  # devient une racine, type gardé
    with engine.connect() as connection:
        row = connection.execute(select(Category).where(Category.id == child)).one()
        actions = connection.scalars(
            select(AuditLog.action).where(AuditLog.object_type == "category")
        ).all()
    assert (row.name, row.parent_id, row.kind) == ("Vétérinaire", None, CategoryKind.EXPENSE)
    assert actions == [
        "category.created",
        "category.created",
        "category.renamed",
        "category.moved",
        "category.moved",
    ]


def test_refusals(engine: Engine) -> None:
    seed_categories(engine)
    food, courses = _id(engine, "Alimentation"), _id(engine, "Courses")
    cases: list[tuple[Callable[[], object], str]] = [
        (
            lambda: create_category(
                engine,
                name="",
                parent_id=None,
                kind=CategoryKind.EXPENSE,
                actor_user_id=None,
                now=NOW,
            ),
            "Nom",
        ),
        (
            lambda: create_category(
                engine,
                name="a › b",
                parent_id=None,
                kind=CategoryKind.EXPENSE,
                actor_user_id=None,
                now=NOW,
            ),
            "Nom",
        ),
        (
            lambda: create_category(
                engine, name="X", parent_id=None, kind=None, actor_user_id=None, now=NOW
            ),
            "type",
        ),
        (
            lambda: create_category(
                engine, name="X", parent_id=999, kind=None, actor_user_id=None, now=NOW
            ),
            "parente inconnue",
        ),
        (
            lambda: create_category(
                engine, name="X", parent_id=courses, kind=None, actor_user_id=None, now=NOW
            ),
            "Deux niveaux",
        ),
        (
            lambda: create_category(
                engine, name="Courses", parent_id=food, kind=None, actor_user_id=None, now=NOW
            ),
            "existe déjà",
        ),
        (
            lambda: create_category(
                engine,
                name="Alimentation",
                parent_id=None,
                kind=CategoryKind.EXPENSE,
                actor_user_id=None,
                now=NOW,
            ),
            "existe déjà",
        ),
        (lambda: rename_category(engine, 999, "X", actor_user_id=None, now=NOW), "inconnue"),
        (
            lambda: rename_category(engine, courses, "Restaurants", actor_user_id=None, now=NOW),
            "existe déjà",
        ),
        (
            lambda: rename_category(engine, food, "Logement", actor_user_id=None, now=NOW),
            "existe déjà",
        ),
        (lambda: move_category(engine, 999, None, actor_user_id=None, now=NOW), "inconnue"),
        (lambda: move_category(engine, food, None, actor_user_id=None, now=NOW), "sous-catégories"),
        (
            lambda: move_category(engine, courses, 999, actor_user_id=None, now=NOW),
            "parente inconnue",
        ),
        (
            lambda: move_category(
                engine, courses, _id(engine, "Restaurants"), actor_user_id=None, now=NOW
            ),
            "Deux niveaux",
        ),
    ]
    for action, message in cases:
        with pytest.raises(CategoryError, match=message):
            action()
    create_category(
        engine,
        name="Courses",
        parent_id=_id(engine, "Divers"),
        kind=None,
        actor_user_id=None,
        now=NOW,
    )
    with pytest.raises(CategoryError, match="à cet endroit"):
        move_category(engine, courses, _id(engine, "Divers"), actor_user_id=None, now=NOW)
