"""Échéances administratives et fiscales (FIS-04, LOC-10) ; nature de charge « CFE »

Révision : 0014
Précédente : 0013
Créée le : 2026-10-07 13:39:59.472810
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_KINDS = "'copropriete', 'taxe_fonciere', 'assurance', 'travaux', 'credit', 'entretien', 'autre'"
NEW_KINDS = (
    "'copropriete', 'taxe_fonciere', 'assurance', 'travaux', 'credit', 'entretien', 'cfe', 'autre'"
)


def _tables(kinds: str) -> tuple[sa.Table, sa.Table]:
    """Définition complète des deux tables (celle de 0006), avec la liste de natures voulue :
    SQLite ne relit pas les contraintes CHECK, la recréation part donc de cette définition."""
    metadata = sa.MetaData()
    sa.Table("property", metadata, sa.Column("id", sa.Integer(), primary_key=True))
    sa.Table("ledger_transaction", metadata, sa.Column("id", sa.Integer(), primary_key=True))
    rule = sa.Table(
        "property_expense_rule",
        metadata,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("property_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=13), nullable=False),
        sa.Column("pattern", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(f"kind IN ({kinds})", name="ck_property_expense_rule_expense_kind"),
        sa.ForeignKeyConstraint(
            ["property_id"],
            ["property.id"],
            name="fk_property_expense_rule_property_id_property",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_property_expense_rule"),
        sa.UniqueConstraint(
            "property_id", "pattern", name="uq_property_expense_rule_property_id_pattern"
        ),
    )
    expense = sa.Table(
        "property_expense",
        metadata,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("property_id", sa.Integer(), nullable=False),
        sa.Column("transaction_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=13), nullable=False),
        sa.Column("rule_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(f"kind IN ({kinds})", name="ck_property_expense_property_expense_kind"),
        sa.ForeignKeyConstraint(
            ["property_id"],
            ["property.id"],
            name="fk_property_expense_property_id_property",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["rule_id"],
            ["property_expense_rule.id"],
            name="fk_property_expense_rule_id_property_expense_rule",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["ledger_transaction.id"],
            name="fk_property_expense_transaction_id_ledger_transaction",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_property_expense"),
        sa.UniqueConstraint("transaction_id", name="uq_property_expense_transaction_id"),
    )
    return rule, expense


def _expense_kinds(kinds: str) -> None:
    """Contrainte CHECK des natures de charge, sur les deux tables (recréées sous SQLite).

    Recréer `property_expense_rule` la supprime : avec les clés étrangères actives, SQLite met
    alors à NULL `property_expense.rule_id` (ON DELETE SET NULL). Les liens sont donc mis de côté
    puis rétablis (les identifiants sont conservés par la copie)."""
    op.execute(
        "CREATE TEMPORARY TABLE _expense_rule_links AS"
        " SELECT id, rule_id FROM property_expense WHERE rule_id IS NOT NULL"
    )
    for table in _tables(kinds):
        with op.batch_alter_table(table.name, copy_from=table, recreate="always"):
            pass
    op.execute(
        "UPDATE property_expense SET rule_id = (SELECT l.rule_id FROM _expense_rule_links l"
        " WHERE l.id = property_expense.id) WHERE id IN (SELECT id FROM _expense_rule_links)"
    )
    op.execute("DROP TABLE _expense_rule_links")


def upgrade() -> None:
    op.create_table(
        "deadline",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=13), nullable=False),
        sa.Column("label", sa.String(length=128), nullable=True),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("property_id", sa.Integer(), nullable=True),
        sa.Column("amount_cents", sa.Integer(), nullable=True),
        sa.Column("recoverable_cents", sa.Integer(), nullable=True),
        sa.Column("remind_days", sa.Integer(), nullable=False),
        sa.Column("yearly", sa.Boolean(), nullable=False),
        sa.Column("transaction_id", sa.Integer(), nullable=True),
        sa.Column("done_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('taxe_fonciere', 'cfe', 'declaration', 'impot', 'assurance', 'autre')",
            name=op.f("ck_deadline_deadline_kind"),
        ),
        sa.CheckConstraint(
            "amount_cents IS NULL OR amount_cents > 0", name=op.f("ck_deadline_amount")
        ),
        sa.CheckConstraint(
            "recoverable_cents IS NULL OR (amount_cents IS NOT NULL"
            " AND recoverable_cents BETWEEN 0 AND amount_cents)",
            name=op.f("ck_deadline_recoverable"),
        ),
        sa.CheckConstraint("remind_days BETWEEN 0 AND 90", name=op.f("ck_deadline_remind_days")),
        sa.ForeignKeyConstraint(
            ["property_id"],
            ["property.id"],
            name=op.f("fk_deadline_property_id_property"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["ledger_transaction.id"],
            name=op.f("fk_deadline_transaction_id_ledger_transaction"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_deadline")),
        sa.UniqueConstraint("transaction_id", name=op.f("uq_deadline_transaction_id")),
    )
    with op.batch_alter_table("deadline", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_deadline_due_date"), ["due_date"], unique=False)
        batch_op.create_index(batch_op.f("ix_deadline_property_id"), ["property_id"], unique=False)
    _expense_kinds(NEW_KINDS)


def downgrade() -> None:
    op.execute("UPDATE property_expense SET kind = 'autre' WHERE kind = 'cfe'")
    op.execute("UPDATE property_expense_rule SET kind = 'autre' WHERE kind = 'cfe'")
    _expense_kinds(OLD_KINDS)
    with op.batch_alter_table("deadline", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_deadline_property_id"))
        batch_op.drop_index(batch_op.f("ix_deadline_due_date"))
    op.drop_table("deadline")
