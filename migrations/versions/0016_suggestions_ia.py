"""Suggestions de catégorie de l'IA dans la file « À valider » (ANA-02)

Révision : 0016
Précédente : 0015
Créée le : 2026-10-07 16:10:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_KINDS = "'duplicate', 'category_conflict', 'transfer_conflict', 'transfer_candidate'"
NEW_KINDS = OLD_KINDS + ", 'ai_suggestion'"


def _table(kinds: str) -> sa.Table:
    """Définition de 0001 avec la liste de types voulue : SQLite ne relit pas les CHECK."""
    metadata = sa.MetaData()
    for name in ("duplicate_link", "ledger_transaction"):
        sa.Table(name, metadata, sa.Column("id", sa.Integer(), primary_key=True))
    return sa.Table(
        "review_item",
        metadata,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=18), nullable=False),
        sa.Column("dedupe_key", sa.String(length=160), nullable=False),
        sa.Column("duplicate_link_id", sa.Integer(), nullable=True),
        sa.Column("transaction_id", sa.Integer(), nullable=True),
        sa.Column("other_transaction_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=9), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(f"kind IN ({kinds})", name="ck_review_item_review_kind"),
        sa.CheckConstraint(
            "status IN ('open', 'resolved', 'dismissed')", name="ck_review_item_review_status"
        ),
        sa.ForeignKeyConstraint(
            ["duplicate_link_id"],
            ["duplicate_link.id"],
            name="fk_review_item_duplicate_link_id_duplicate_link",
        ),
        sa.ForeignKeyConstraint(
            ["other_transaction_id"],
            ["ledger_transaction.id"],
            name="fk_review_item_other_transaction_id_ledger_transaction",
        ),
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["ledger_transaction.id"],
            name="fk_review_item_transaction_id_ledger_transaction",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_review_item"),
        sa.UniqueConstraint("dedupe_key", name="uq_review_item_dedupe_key"),
        sa.Index("ix_review_item_status_kind", "status", "kind"),
    )


def upgrade() -> None:
    table = _table(NEW_KINDS)
    with op.batch_alter_table("review_item", copy_from=table, recreate="always"):
        pass


def downgrade() -> None:
    op.execute("DELETE FROM review_item WHERE kind = 'ai_suggestion'")
    table = _table(OLD_KINDS)
    with op.batch_alter_table("review_item", copy_from=table, recreate="always"):
        pass
