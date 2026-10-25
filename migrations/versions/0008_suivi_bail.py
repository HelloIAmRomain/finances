"""Suivi d'un bail à partir d'un mois

Révision : 0008
Précédente : 0007
Créée le : 2026-09-29 22:46:06.746023
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:

    with op.batch_alter_table("lease", schema=None) as batch_op:
        batch_op.add_column(sa.Column("tracking_start", sa.Date(), nullable=True))


def downgrade() -> None:

    with op.batch_alter_table("lease", schema=None) as batch_op:
        batch_op.drop_column("tracking_start")
