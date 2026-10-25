"""Patrimoine : actifs et dettes saisis à la main (COL-05), patrimoine net (ANA-09).

- Livrets, assurance-vie, PEA, PER, titres, bien immobilier (ma part en indivision), crédit
  (capital restant dû, compté en négatif), prêt accordé à un proche.
- Une valeur par actif et par date (corrigée si saisie de nouveau le même jour) ; l'historique
  donne l'évolution. Montants en centimes (inv. 2).
- Patrimoine net = soldes des comptes comptés + dernières valeurs des actifs comptés.
- Toute saisie est auditée ; un actif n'est jamais supprimé, il est archivé.
"""

from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy import Engine, insert, select, update

from app.audit import audit
from app.db import inserted_id, write_transaction
from app.ledger.aggregates import net_worth
from app.models import AssetValuation, ManualAsset, Property
from app.models.ledger import AssetKind

KIND_LABELS = {
    AssetKind.SAVINGS: "Livret d'épargne",
    AssetKind.LIFE_INSURANCE: "Assurance-vie",
    AssetKind.PEA: "PEA",
    AssetKind.RETIREMENT: "PER",
    AssetKind.SECURITIES: "Compte-titres",
    AssetKind.REAL_ESTATE: "Bien immobilier",
    AssetKind.LOAN: "Crédit (capital restant dû)",
    AssetKind.LOAN_GIVEN: "Prêt accordé",
    AssetKind.OTHER: "Autre",
}
MAX_NAME = 128


class AssetError(ValueError):
    """Saisie refusée (message affiché tel quel)."""


@dataclass(frozen=True)
class Valuation:
    as_of_date: date
    value_cents: int


@dataclass
class AssetRow:
    id: int
    name: str
    kind: AssetKind
    counted: bool
    property_name: str | None
    history: list[Valuation] = field(default_factory=list)  # la plus récente d'abord

    @property
    def latest(self) -> Valuation | None:
        return self.history[0] if self.history else None

    @property
    def change_cents(self) -> int | None:
        """Évolution depuis la valeur précédente."""
        if len(self.history) < 2:  # noqa: PLR2004
            return None
        return self.history[0].value_cents - self.history[1].value_cents


@dataclass(frozen=True)
class Wealth:
    accounts_cents: int
    assets_cents: int  # actifs comptés (positifs)
    debts_cents: int  # dettes comptées (négatives)

    @property
    def total_cents(self) -> int:
        return self.accounts_cents + self.assets_cents + self.debts_cents


def create_asset(
    engine: Engine,
    *,
    name: str,
    kind: AssetKind,
    counted: bool = True,
    property_id: int | None = None,
    actor_user_id: int | None,
    now: datetime,
) -> int:
    cleaned = " ".join(name.split())
    if not cleaned or len(cleaned) > MAX_NAME:
        raise AssetError(f"Nom obligatoire ({MAX_NAME} caractères au plus).")
    with write_transaction(engine) as connection:
        if property_id is not None and (
            connection.scalar(select(Property.id).where(Property.id == property_id)) is None
        ):
            raise AssetError("Bien introuvable.")
        asset_id = inserted_id(
            connection.execute(
                insert(ManualAsset).values(
                    name=cleaned,
                    kind=kind,
                    counted=counted,
                    property_id=property_id,
                    created_at=now,
                )
            )
        )
        audit(
            connection,
            "asset.created",
            ("manual_asset", asset_id),
            now=now,
            actor_user_id=actor_user_id,
            details={"nature": str(kind), "compte": counted},
        )
    return asset_id


def record_value(
    engine: Engine,
    asset_id: int,
    *,
    as_of: date,
    value_cents: int,
    actor_user_id: int | None,
    now: datetime,
) -> None:
    """Valeur à une date ; pour un crédit, le capital restant dû est compté en négatif."""
    with write_transaction(engine) as connection:
        kind = connection.scalar(
            select(ManualAsset.kind).where(
                ManualAsset.id == asset_id, ManualAsset.archived_at.is_(None)
            )
        )
        if kind is None:
            raise AssetError("Actif introuvable.")
        value = -abs(value_cents) if kind is AssetKind.LOAN else value_cents
        existing = connection.scalar(
            select(AssetValuation.id).where(
                AssetValuation.asset_id == asset_id, AssetValuation.as_of_date == as_of
            )
        )
        if existing is None:
            connection.execute(
                insert(AssetValuation).values(
                    asset_id=asset_id, as_of_date=as_of, value_cents=value, created_at=now
                )
            )
        else:
            connection.execute(
                update(AssetValuation)
                .where(AssetValuation.id == existing)
                .values(value_cents=value)
            )
        audit(
            connection,
            "asset.valued",
            ("manual_asset", asset_id),
            now=now,
            actor_user_id=actor_user_id,
            details={"date": as_of.isoformat(), "valeur": value},
        )


def archive_asset(
    engine: Engine, asset_id: int, *, actor_user_id: int | None, now: datetime
) -> None:
    with write_transaction(engine) as connection:
        result = connection.execute(
            update(ManualAsset)
            .where(ManualAsset.id == asset_id, ManualAsset.archived_at.is_(None))
            .values(archived_at=now)
        )
        if result.rowcount != 1:
            raise AssetError("Actif introuvable.")
        audit(
            connection,
            "asset.archived",
            ("manual_asset", asset_id),
            now=now,
            actor_user_id=actor_user_id,
        )


def assets(engine: Engine) -> list[AssetRow]:
    with engine.connect() as connection:
        rows = connection.execute(
            select(
                ManualAsset.id,
                ManualAsset.name,
                ManualAsset.kind,
                ManualAsset.counted,
                Property.name.label("property_name"),
            )
            .outerjoin(Property, Property.id == ManualAsset.property_id)
            .where(ManualAsset.archived_at.is_(None))
            .order_by(ManualAsset.kind, ManualAsset.name)
        ).all()
        values = connection.execute(
            select(
                AssetValuation.asset_id, AssetValuation.as_of_date, AssetValuation.value_cents
            ).order_by(AssetValuation.as_of_date.desc())
        ).all()
    found = {
        row.id: AssetRow(row.id, row.name, row.kind, bool(row.counted), row.property_name)
        for row in rows
    }
    for asset_id, as_of, value in values:
        if asset_id in found:
            found[asset_id].history.append(Valuation(as_of, value))
    return list(found.values())


def wealth(engine: Engine, rows: list[AssetRow] | None = None) -> Wealth:
    rows = assets(engine) if rows is None else rows
    latest = [r.latest.value_cents for r in rows if r.counted and r.latest is not None]
    return Wealth(
        accounts_cents=net_worth(engine).total,
        assets_cents=sum(v for v in latest if v > 0),
        debts_cents=sum(v for v in latest if v < 0),
    )
