"""Clés de sécurité WebAuthn

Révision : 0009
Précédente : 0008
Créée le : 2026-09-29 22:56:46.010883
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:

    op.create_table(
        "security_key",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("credential_id", sa.String(length=512), nullable=False),
        sa.Column("public_key", sa.LargeBinary(), nullable=False),
        sa.Column("sign_count", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_security_key_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_security_key")),
        sa.UniqueConstraint("credential_id", name=op.f("uq_security_key_credential_id")),
    )
    op.create_table(
        "webauthn_challenge",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("purpose", sa.String(length=16), nullable=False),
        sa.Column("challenge_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_webauthn_challenge_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webauthn_challenge")),
        sa.UniqueConstraint("challenge_hash", name=op.f("uq_webauthn_challenge_challenge_hash")),
    )
    with op.batch_alter_table("webauthn_challenge", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_webauthn_challenge_expires_at"), ["expires_at"], unique=False
        )


def downgrade() -> None:

    with op.batch_alter_table("webauthn_challenge", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_webauthn_challenge_expires_at"))

    op.drop_table("webauthn_challenge")
    op.drop_table("security_key")
