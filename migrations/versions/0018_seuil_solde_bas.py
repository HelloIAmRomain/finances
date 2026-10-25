"""Seuil d'alerte « solde bas prévu » par compte (ALR-07)

Révision : 0018
Précédente : 0017
Créée le : 2026-10-06 14:34:07.667717
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("account", schema=None) as batch_op:
        batch_op.add_column(sa.Column("low_balance_cents", sa.Integer(), nullable=True))


def downgrade() -> None:
    # DROP COLUMN natif (SQLite ≥ 3.35) : recréer `account`, parent de nombreuses tables,
    # déclencherait les actions de leurs clés étrangères.
    op.drop_column("account", "low_balance_cents")
