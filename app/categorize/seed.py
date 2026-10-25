"""Arborescence de catégories par défaut, en français (idempotente)."""

from sqlalchemy import Engine, insert, select

from app.db import inserted_id, write_transaction
from app.models import Category
from app.models.categorize import CategoryKind

EXPENSE, INCOME, TRANSFER, NEUTRAL = (
    CategoryKind.EXPENSE,
    CategoryKind.INCOME,
    CategoryKind.TRANSFER,
    CategoryKind.NEUTRAL,
)

DEFAULT_TREE: list[tuple[str, CategoryKind, list[str]]] = [
    ("Alimentation", EXPENSE, ["Courses", "Restaurants", "Boulangerie"]),
    ("Logement", EXPENSE, ["Énergie", "Eau", "Assurance habitation", "Travaux", "Équipement"]),
    ("Transport", EXPENSE, ["Carburant", "Transports en commun", "Péage et parking", "Entretien"]),
    ("Santé", EXPENSE, ["Médecin", "Pharmacie", "Mutuelle"]),
    ("Loisirs", EXPENSE, ["Sorties", "Voyages", "Sport", "Culture"]),
    ("Abonnements", EXPENSE, ["Téléphone et internet", "Streaming", "Presse"]),
    ("Achats", EXPENSE, ["Vêtements", "High-tech", "Cadeaux"]),
    ("Banque", EXPENSE, ["Frais bancaires", "Intérêts"]),
    (
        "Impôts et taxes",
        EXPENSE,
        ["Impôt sur le revenu", "Taxe foncière", "CFE", "Prélèvements sociaux"],
    ),
    (
        "Bien locatif",
        EXPENSE,
        ["Charges de copropriété", "Assurance PNO", "Travaux du bien", "Frais de gestion"],
    ),
    ("Revenus", INCOME, ["Salaire", "Loyers perçus", "Remboursements", "Autres revenus"]),
    ("Épargne et placements", TRANSFER, ["Versement épargne", "Crypto"]),
    ("Virements internes", TRANSFER, []),
    ("Divers", EXPENSE, ["À classer"]),
]


def seed_categories(engine: Engine) -> int:
    """Crée les catégories manquantes ; renvoie le nombre de catégories créées."""
    created = 0
    with write_transaction(engine) as connection:
        existing = {
            (parent, name): category_id
            for category_id, parent, name in connection.execute(
                select(Category.id, Category.parent_id, Category.name)
            )
        }

        def ensure(parent_id: int | None, name: str, kind: CategoryKind, order: int) -> int:
            nonlocal created
            if (parent_id, name) in existing:
                return existing[(parent_id, name)]
            result = connection.execute(
                insert(Category).values(parent_id=parent_id, name=name, kind=kind, sort_order=order)
            )
            created += 1
            category_id = inserted_id(result)
            existing[(parent_id, name)] = category_id
            return category_id

        for order, (name, kind, children) in enumerate(DEFAULT_TREE):
            parent_id = ensure(None, name, kind, order)
            for child_order, child in enumerate(children):
                ensure(parent_id, child, kind, child_order)
    return created
