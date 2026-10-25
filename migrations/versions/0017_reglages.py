"""Réglages de l'instance modifiables depuis l'interface (PROD-01, ALR-04)

Révision : 0017
Précédente : 0016
Créée le : 2026-10-06 14:27:00.879550
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "app_setting",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_app_setting")),
    )


def downgrade() -> None:
    op.drop_table("app_setting")
