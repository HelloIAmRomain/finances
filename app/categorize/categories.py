"""Arborescence des catégories (deux niveaux) : création, renommage, déplacement, audités.

Pas de suppression : une catégorie utilisée garde son historique (inv. 4 par extension).
"""

from datetime import datetime

from sqlalchemy import Engine, func, insert, select, update

from app.audit import audit
from app.db import inserted_id, write_transaction
from app.models import Category
from app.models.categorize import CategoryKind

MAX_NAME = 64


class CategoryError(ValueError):
    """Opération refusée sur l'arborescence."""


def _name(name: str) -> str:
    cleaned = " ".join(name.split())
    if not cleaned or len(cleaned) > MAX_NAME or "›" in cleaned:
        raise CategoryError(f"Nom de catégorie : 1 à {MAX_NAME} caractères, sans « › ».")
    return cleaned


def create_category(
    engine: Engine,
    *,
    name: str,
    parent_id: int | None,
    kind: CategoryKind | None,
    actor_user_id: int | None,
    now: datetime,
) -> int:
    """Une sous-catégorie hérite du type de sa racine ; une racine doit en préciser un."""
    cleaned = _name(name)
    with write_transaction(engine) as connection:
        if parent_id is not None:
            parent = connection.execute(
                select(Category.parent_id, Category.kind).where(Category.id == parent_id)
            ).first()
            if parent is None:
                raise CategoryError("Catégorie parente inconnue.")
            if parent.parent_id is not None:
                raise CategoryError("Deux niveaux seulement : choisir une catégorie racine.")
            kind = parent.kind
        elif kind is None:
            raise CategoryError("Une catégorie racine doit avoir un type.")
        if connection.scalar(
            select(Category.id).where(
                Category.parent_id.is_(parent_id)
                if parent_id is None
                else Category.parent_id == parent_id,
                Category.name == cleaned,
            )
        ):
            raise CategoryError("Cette catégorie existe déjà.")
        order = connection.scalar(select(func.coalesce(func.max(Category.sort_order), 0) + 1))
        result = connection.execute(
            insert(Category).values(parent_id=parent_id, name=cleaned, kind=kind, sort_order=order)
        )
        category_id = inserted_id(result)
        audit(
            connection,
            "category.created",
            ("category", category_id),
            now=now,
            actor_user_id=actor_user_id,
            details={"nom": cleaned, "parent": parent_id},
        )
    return category_id


def rename_category(
    engine: Engine, category_id: int, name: str, *, actor_user_id: int | None, now: datetime
) -> None:
    cleaned = _name(name)
    with write_transaction(engine) as connection:
        current = connection.execute(
            select(Category.parent_id, Category.name).where(Category.id == category_id)
        ).first()
        if current is None:
            raise CategoryError("Catégorie inconnue.")
        clash = connection.scalar(
            select(Category.id).where(
                Category.parent_id.is_(None)
                if current.parent_id is None
                else Category.parent_id == current.parent_id,
                Category.name == cleaned,
                Category.id != category_id,
            )
        )
        if clash:
            raise CategoryError("Une catégorie de ce nom existe déjà au même niveau.")
        connection.execute(update(Category).where(Category.id == category_id).values(name=cleaned))
        audit(
            connection,
            "category.renamed",
            ("category", category_id),
            now=now,
            actor_user_id=actor_user_id,
            details={"avant": current.name, "apres": cleaned},
        )


def move_category(
    engine: Engine,
    category_id: int,
    parent_id: int | None,
    *,
    actor_user_id: int | None,
    now: datetime,
) -> None:
    """Déplace une sous-catégorie sous une autre racine, ou en fait une racine (type conservé)."""
    with write_transaction(engine) as connection:
        current = connection.execute(
            select(Category.name, Category.parent_id).where(Category.id == category_id)
        ).first()
        if current is None:
            raise CategoryError("Catégorie inconnue.")
        if connection.scalar(select(Category.id).where(Category.parent_id == category_id)):
            raise CategoryError("Cette catégorie a des sous-catégories : elle reste une racine.")
        kind = None
        if parent_id is not None:
            parent = connection.execute(
                select(Category.parent_id, Category.kind).where(Category.id == parent_id)
            ).first()
            if parent is None or parent_id == category_id:
                raise CategoryError("Catégorie parente inconnue.")
            if parent.parent_id is not None:
                raise CategoryError("Deux niveaux seulement : choisir une catégorie racine.")
            kind = parent.kind
        clash = connection.scalar(
            select(Category.id).where(
                Category.parent_id.is_(None)
                if parent_id is None
                else Category.parent_id == parent_id,
                Category.name == current.name,
                Category.id != category_id,
            )
        )
        if clash:
            raise CategoryError("Une catégorie de ce nom existe déjà à cet endroit.")
        values: dict[str, object] = {"parent_id": parent_id}
        if kind is not None:
            values["kind"] = kind
        connection.execute(update(Category).where(Category.id == category_id).values(**values))
        audit(
            connection,
            "category.moved",
            ("category", category_id),
            now=now,
            actor_user_id=actor_user_id,
            details={"avant": current.parent_id, "apres": parent_id},
        )
