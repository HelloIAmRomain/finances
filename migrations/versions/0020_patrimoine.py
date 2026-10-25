"""Patrimoine : actifs et dettes saisis à la main, historique de valeur (COL-05)

Révision : 0020
Précédente : 0019
Créée le : 2026-10-06 14:53:18.074287
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "manual_asset",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=13), nullable=False),
        sa.Column("counted", sa.Boolean(), nullable=False),
        sa.Column("property_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("archived_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "kind IN ('livret', 'assurance_vie', 'pea', 'per', 'titres', 'immobilier', 'credit',"
            " 'pret_accorde', 'autre')",
            name=op.f("ck_manual_asset_asset_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["property_id"],
            ["property.id"],
            name=op.f("fk_manual_asset_property_id_property"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_manual_asset")),
    )
    op.create_table(
        "asset_valuation",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("asset_id", sa.Integer(), nullable=False),
        sa.Column("as_of_date", sa.Date(), nullable=False),
        sa.Column("value_cents", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["manual_asset.id"],
            name=op.f("fk_asset_valuation_asset_id_manual_asset"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_asset_valuation")),
        sa.UniqueConstraint(
            "asset_id", "as_of_date", name=op.f("uq_asset_valuation_asset_id_as_of_date")
        ),
    )


def downgrade() -> None:
    op.drop_table("asset_valuation")
    op.drop_table("manual_asset")
