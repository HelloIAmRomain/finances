"""Nonce à usage unique des demandes de liaison bancaire (T1.12).

Révision : 0002
Précédente : 0001
Créée le : 2026-09-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "link_nonce",
        sa.Column("nonce_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("nonce_hash", name=op.f("pk_link_nonce")),
    )
    with op.batch_alter_table("link_nonce", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_link_nonce_expires_at"), ["expires_at"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("link_nonce", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_link_nonce_expires_at"))

    op.drop_table("link_nonce")
