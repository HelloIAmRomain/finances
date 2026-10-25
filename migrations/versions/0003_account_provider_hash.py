"""Empreinte de l'identifiant stable d'un compte chez le fournisseur (cartes sans IBAN).

Révision : 0003
Précédente : 0002
Créée le : 2026-09-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("account", schema=None) as batch_op:
        batch_op.add_column(sa.Column("provider_account_hash", sa.String(length=64), nullable=True))
        batch_op.create_index(
            batch_op.f("ix_account_provider_account_hash"), ["provider_account_hash"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("account", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_account_provider_account_hash"))
        batch_op.drop_column("provider_account_hash")
