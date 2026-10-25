"""Révision des loyers : indices IRL et trimestre de référence du bail (LOC-09)

Révision : 0019
Précédente : 0018
Créée le : 2026-10-06 14:47:07.036209
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "irl_index",
        sa.Column("quarter", sa.String(length=7), nullable=False),
        sa.Column("value_hundredths", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("value_hundredths > 0", name=op.f("ck_irl_index_value_positive")),
        sa.PrimaryKeyConstraint("quarter", name=op.f("pk_irl_index")),
    )
    with op.batch_alter_table("lease", schema=None) as batch_op:
        batch_op.add_column(sa.Column("irl_reference_quarter", sa.String(length=7), nullable=True))


def downgrade() -> None:
    # DROP COLUMN natif : recréer `lease` déclencherait les clés étrangères de ses tables filles.
    op.drop_column("lease", "irl_reference_quarter")

    op.drop_table("irl_index")
