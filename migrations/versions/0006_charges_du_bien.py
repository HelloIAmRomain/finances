"""Charges du bien (LOC-10)

Révision : 0006
Précédente : 0005
Créée le : 2026-09-29 01:40:24.992533
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "property_expense_rule",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("property_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=13), nullable=False),
        sa.Column("pattern", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('copropriete', 'taxe_fonciere', 'assurance', 'travaux', 'credit',"
            " 'entretien', 'autre')",
            name=op.f("ck_property_expense_rule_expense_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["property_id"],
            ["property.id"],
            name=op.f("fk_property_expense_rule_property_id_property"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_property_expense_rule")),
        sa.UniqueConstraint(
            "property_id", "pattern", name=op.f("uq_property_expense_rule_property_id_pattern")
        ),
    )
    op.create_table(
        "property_expense",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("property_id", sa.Integer(), nullable=False),
        sa.Column("transaction_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=13), nullable=False),
        sa.Column("rule_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('copropriete', 'taxe_fonciere', 'assurance', 'travaux', 'credit',"
            " 'entretien', 'autre')",
            name=op.f("ck_property_expense_property_expense_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["property_id"],
            ["property.id"],
            name=op.f("fk_property_expense_property_id_property"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["rule_id"],
            ["property_expense_rule.id"],
            name=op.f("fk_property_expense_rule_id_property_expense_rule"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["ledger_transaction.id"],
            name=op.f("fk_property_expense_transaction_id_ledger_transaction"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_property_expense")),
        sa.UniqueConstraint("transaction_id", name=op.f("uq_property_expense_transaction_id")),
    )


def downgrade() -> None:
    op.drop_table("property_expense")
    op.drop_table("property_expense_rule")
