"""Dépôt de garantie : encaissement, retenues, restitution (LEG-04)

Révision : 0022
Précédente : 0021
Créée le : 2026-10-07 10:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "deposit_event",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("lease_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("on_date", sa.Date(), nullable=False),
        sa.Column("transaction_id", sa.Integer(), nullable=True),
        sa.Column("note", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('received', 'withheld', 'returned')",
            name=op.f("ck_deposit_event_deposit_kind"),
        ),
        sa.CheckConstraint("amount_cents > 0", name=op.f("ck_deposit_event_amount_positive")),
        sa.ForeignKeyConstraint(
            ["lease_id"],
            ["lease.id"],
            name=op.f("fk_deposit_event_lease_id_lease"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["ledger_transaction.id"],
            name=op.f("fk_deposit_event_transaction_id_ledger_transaction"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_deposit_event")),
        sa.UniqueConstraint("transaction_id", name=op.f("uq_deposit_event_transaction_id")),
    )
    op.create_index(op.f("ix_deposit_event_lease_id"), "deposit_event", ["lease_id"])
    # Colonnes ajoutées sans recréer `lease` (ses tables filles déclencheraient leurs actions).
    op.add_column("lease", sa.Column("keys_returned_on", sa.Date(), nullable=True))
    op.add_column("lease", sa.Column("exit_inspection_ok", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("lease", "exit_inspection_ok")
    op.drop_column("lease", "keys_returned_on")
    op.drop_index(op.f("ix_deposit_event_lease_id"), table_name="deposit_event")
    op.drop_table("deposit_event")
