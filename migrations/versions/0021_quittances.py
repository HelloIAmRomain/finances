"""Quittances : part des charges par échéance, bailleur du bien (LOC-07)

Révision : 0021
Précédente : 0020
Créée le : 2026-10-07 09:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Colonnes ajoutées sans recréer les tables : leurs tables filles garderaient leurs liens,
    # mais SQLite déclencherait les actions des clés étrangères.
    op.add_column(
        "rent_due",
        sa.Column("charges_cents", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column("property", sa.Column("landlord_enc", sa.LargeBinary(), nullable=True))
    # Échéances existantes : charges du bail, plafonnées au montant dû.
    op.execute(
        """
        UPDATE rent_due SET charges_cents = (
            SELECT MIN(lease.charges_cents, rent_due.amount_cents)
            FROM lease WHERE lease.id = rent_due.lease_id
        )
        """
    )


def downgrade() -> None:
    op.drop_column("property", "landlord_enc")
    op.drop_column("rent_due", "charges_cents")
