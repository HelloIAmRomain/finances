"""Budgets mensuels par catégorie (ANA-06)

Révision : 0015
Précédente : 0014
Créée le : 2026-10-06 13:55:26.621330
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "budget",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("category_id", sa.Integer(), nullable=False),
        sa.Column("monthly_cents", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("monthly_cents > 0", name=op.f("ck_budget_monthly_positive")),
        sa.ForeignKeyConstraint(
            ["category_id"], ["category.id"], name=op.f("fk_budget_category_id_category")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_budget")),
        sa.UniqueConstraint("category_id", name=op.f("uq_budget_category_id")),
    )


def downgrade() -> None:
    op.drop_table("budget")
