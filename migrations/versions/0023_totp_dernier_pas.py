"""Code TOTP à usage unique : dernier pas de temps accepté (NF-SEC-04)

Révision : 0023
Précédente : 0022
Créée le : 2026-10-07 22:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("app_user") as batch:
        batch.add_column(sa.Column("last_totp_step", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("app_user") as batch:
        batch.drop_column("last_totp_step")
