"""Lot 2 : biens, baux, échéances, rapprochement, incidents

Révision : 0005
Précédente : 0004
Créée le : 2026-09-29 01:21:52.722916
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:

    op.create_table(
        "property",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("address_enc", sa.LargeBinary(), nullable=True),
        sa.Column("furnished", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("archived_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_property")),
    )
    op.create_table(
        "owner_share",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("property_id", sa.Integer(), nullable=False),
        sa.Column("owner_label", sa.String(length=64), nullable=False),
        sa.Column("share_bp", sa.Integer(), nullable=False),
        sa.CheckConstraint("share_bp BETWEEN 1 AND 10000", name=op.f("ck_owner_share_share_bp")),
        sa.ForeignKeyConstraint(
            ["property_id"],
            ["property.id"],
            name=op.f("fk_owner_share_property_id_property"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_owner_share")),
        sa.UniqueConstraint(
            "property_id", "owner_label", name=op.f("uq_owner_share_property_id_owner_label")
        ),
    )
    op.create_table(
        "lease",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=4), nullable=False),
        sa.Column("label", sa.String(length=128), nullable=False),
        sa.Column("property_id", sa.Integer(), nullable=True),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("rent_cents", sa.Integer(), nullable=False),
        sa.Column("charges_cents", sa.Integer(), nullable=False),
        sa.Column("due_day", sa.Integer(), nullable=False),
        sa.Column("alert_day", sa.Integer(), nullable=False),
        sa.Column("unpaid_day", sa.Integer(), nullable=False),
        sa.Column("deposit_cents", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("kind IN ('rent', 'loan')", name=op.f("ck_lease_lease_kind")),
        sa.CheckConstraint("alert_day < unpaid_day", name=op.f("ck_lease_alert_before_unpaid")),
        sa.CheckConstraint("alert_day BETWEEN 1 AND 28", name=op.f("ck_lease_alert_day")),
        sa.CheckConstraint("due_day <= alert_day", name=op.f("ck_lease_due_before_alert")),
        sa.CheckConstraint("due_day BETWEEN 1 AND 28", name=op.f("ck_lease_due_day")),
        sa.CheckConstraint("rent_cents > 0 AND charges_cents >= 0", name=op.f("ck_lease_amounts")),
        sa.CheckConstraint("unpaid_day BETWEEN 1 AND 28", name=op.f("ck_lease_unpaid_day")),
        sa.ForeignKeyConstraint(
            ["account_id"], ["account.id"], name=op.f("fk_lease_account_id_account")
        ),
        sa.ForeignKeyConstraint(
            ["property_id"], ["property.id"], name=op.f("fk_lease_property_id_property")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_lease")),
    )
    op.create_table(
        "party",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("lease_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=11), nullable=False),
        sa.Column("name_enc", sa.LargeBinary(), nullable=False),
        sa.Column("iban_enc", sa.LargeBinary(), nullable=True),
        sa.Column("iban_hash", sa.String(length=64), nullable=True),
        sa.Column("label_patterns_enc", sa.LargeBinary(), nullable=True),
        sa.CheckConstraint(
            "role IN ('tenant', 'guarantor', 'third_payer', 'borrower')",
            name=op.f("ck_party_party_role"),
        ),
        sa.ForeignKeyConstraint(
            ["lease_id"], ["lease.id"], name=op.f("fk_party_lease_id_lease"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_party")),
    )
    with op.batch_alter_table("party", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_party_iban_hash"), ["iban_hash"], unique=False)
        batch_op.create_index(batch_op.f("ix_party_lease_id"), ["lease_id"], unique=False)

    op.create_table(
        "rent_due",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("lease_id", sa.Integer(), nullable=False),
        sa.Column("period", sa.String(length=7), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["lease_id"], ["lease.id"], name=op.f("fk_rent_due_lease_id_lease"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rent_due")),
        sa.UniqueConstraint("lease_id", "period", name=op.f("uq_rent_due_lease_id_period")),
    )
    op.create_table(
        "rent_incident",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("lease_id", sa.Integer(), nullable=False),
        sa.Column("due_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=9), nullable=False),
        sa.Column("occurrence", sa.Integer(), nullable=False),
        sa.Column("occurred_on", sa.Date(), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('late', 'unpaid', 'guarantor', 'rejected', 'overpaid')",
            name=op.f("ck_rent_incident_incident_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["due_id"],
            ["rent_due.id"],
            name=op.f("fk_rent_incident_due_id_rent_due"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["lease_id"],
            ["lease.id"],
            name=op.f("fk_rent_incident_lease_id_lease"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rent_incident")),
        sa.UniqueConstraint(
            "due_id", "kind", "occurrence", name=op.f("uq_rent_incident_due_id_kind_occurrence")
        ),
    )
    with op.batch_alter_table("rent_incident", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_rent_incident_lease_id"), ["lease_id"], unique=False)

    op.create_table(
        "overpayment",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("lease_id", sa.Integer(), nullable=False),
        sa.Column("transaction_id", sa.Integer(), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("decision", sa.String(length=13), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "decision IN ('pending', 'carry_forward', 'refund')",
            name=op.f("ck_overpayment_overpayment_decision"),
        ),
        sa.ForeignKeyConstraint(
            ["lease_id"],
            ["lease.id"],
            name=op.f("fk_overpayment_lease_id_lease"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["ledger_transaction.id"],
            name=op.f("fk_overpayment_transaction_id_ledger_transaction"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_overpayment")),
        sa.UniqueConstraint("transaction_id", name=op.f("uq_overpayment_transaction_id")),
    )
    op.create_table(
        "rent_allocation",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("due_id", sa.Integer(), nullable=False),
        sa.Column("transaction_id", sa.Integer(), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=9), nullable=False),
        sa.Column("method", sa.String(length=6), nullable=False),
        sa.Column("payer_role", sa.String(length=11), nullable=True),
        sa.Column("reason", sa.String(length=64), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("cancelled_by_transaction_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "method IN ('auto', 'manual')", name=op.f("ck_rent_allocation_allocation_method")
        ),
        sa.CheckConstraint(
            "payer_role IN ('tenant', 'guarantor', 'third_payer', 'borrower')",
            name=op.f("ck_rent_allocation_payer_role"),
        ),
        sa.CheckConstraint(
            "status IN ('proposed', 'confirmed', 'rejected', 'cancelled')",
            name=op.f("ck_rent_allocation_allocation_status"),
        ),
        sa.CheckConstraint("amount_cents > 0", name=op.f("ck_rent_allocation_amount_positive")),
        sa.ForeignKeyConstraint(
            ["cancelled_by_transaction_id"],
            ["ledger_transaction.id"],
            name=op.f("fk_rent_allocation_cancelled_by_transaction_id_ledger_transaction"),
        ),
        sa.ForeignKeyConstraint(
            ["due_id"],
            ["rent_due.id"],
            name=op.f("fk_rent_allocation_due_id_rent_due"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["ledger_transaction.id"],
            name=op.f("fk_rent_allocation_transaction_id_ledger_transaction"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rent_allocation")),
        sa.UniqueConstraint(
            "cancelled_by_transaction_id",
            name=op.f("uq_rent_allocation_cancelled_by_transaction_id"),
        ),
    )
    with op.batch_alter_table("rent_allocation", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_rent_allocation_due_id_status"), ["due_id", "status"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_rent_allocation_transaction_id"), ["transaction_id"], unique=False
        )


def downgrade() -> None:

    with op.batch_alter_table("rent_allocation", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_rent_allocation_transaction_id"))
        batch_op.drop_index(batch_op.f("ix_rent_allocation_due_id_status"))

    op.drop_table("rent_allocation")
    op.drop_table("overpayment")
    with op.batch_alter_table("rent_incident", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_rent_incident_lease_id"))

    op.drop_table("rent_incident")
    op.drop_table("rent_due")
    with op.batch_alter_table("party", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_party_lease_id"))
        batch_op.drop_index(batch_op.f("ix_party_iban_hash"))

    op.drop_table("party")
    op.drop_table("lease")
    op.drop_table("owner_share")
    op.drop_table("property")
