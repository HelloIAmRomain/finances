"""Socle du lot 1 : comptes, transactions, liaisons, catégorisation, alertes, exécutions, audit.

Modèle décrit dans docs/PLAN.md §5. Les déclencheurs d'immuabilité (inv. 4, NF-SEC-06) sont
créés ici en SQL brut, figé dans la migration.

Révision : 0001
Précédente : aucune
Créée le : 2026-09-27 22:22:31.436740
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

TRIGGERS = {
    "ledger_transaction_immutable": """
CREATE TRIGGER ledger_transaction_immutable
BEFORE UPDATE ON ledger_transaction
BEGIN
    SELECT RAISE(ABORT, 'ledger_transaction est immuable (invariant 4)');
END
""",
    "audit_log_no_update": """
CREATE TRIGGER audit_log_no_update
BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log est en ajout seul (NF-SEC-06)');
END
""",
    "audit_log_no_delete": """
CREATE TRIGGER audit_log_no_delete
BEFORE DELETE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log est en ajout seul (NF-SEC-06)');
END
""",
}


revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "alert",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(length=64), nullable=False),
        sa.Column("severity", sa.String(length=8), nullable=False),
        sa.Column("status", sa.String(length=8), nullable=False),
        sa.Column("dedupe_key", sa.String(length=160), nullable=False),
        sa.Column("subject_type", sa.String(length=64), nullable=True),
        sa.Column("subject_id", sa.Integer(), nullable=True),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("opened_at", sa.DateTime(), nullable=False),
        sa.Column("seen_at", sa.DateTime(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("last_reminded_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "severity IN ('info', 'warning', 'critical')", name=op.f("ck_alert_severity")
        ),
        sa.CheckConstraint(
            "status IN ('open', 'seen', 'resolved')", name=op.f("ck_alert_alert_status")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_alert")),
        sa.UniqueConstraint("dedupe_key", name=op.f("uq_alert_dedupe_key")),
    )
    with op.batch_alter_table("alert", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_alert_status_severity"), ["status", "severity"], unique=False
        )

    op.create_table(
        "app_user",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("totp_secret_enc", sa.LargeBinary(), nullable=True),
        sa.Column("role", sa.String(length=8), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_login_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("role IN ('admin', 'co_owner')", name=op.f("ck_app_user_user_role")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_app_user")),
        sa.UniqueConstraint("username", name=op.f("uq_app_user_username")),
    )
    op.create_table(
        "category",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("parent_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("archived_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "kind IN ('expense', 'income', 'transfer', 'neutral')",
            name=op.f("ck_category_category_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["parent_id"], ["category.id"], name=op.f("fk_category_parent_id_category")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_category")),
        sa.UniqueConstraint("parent_id", "name", name=op.f("uq_category_parent_id_name")),
    )
    op.create_table(
        "connection",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=14), nullable=False),
        sa.Column("institution_name", sa.String(length=128), nullable=False),
        sa.Column("country", sa.String(length=2), nullable=False),
        sa.Column("status", sa.String(length=7), nullable=False),
        sa.Column("provider_session_enc", sa.LargeBinary(), nullable=True),
        sa.Column("consent_expires_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "provider IN ('enable_banking', 'file')", name=op.f("ck_connection_provider")
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'active', 'expired', 'error', 'revoked')",
            name=op.f("ck_connection_connection_status"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_connection")),
    )
    with op.batch_alter_table("connection", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_connection_consent_expires_at"), ["consent_expires_at"], unique=False
        )

    op.create_table(
        "import_profile",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("institution_name", sa.String(length=128), nullable=False),
        sa.Column("format", sa.String(length=3), nullable=False),
        sa.Column("encoding", sa.String(length=32), nullable=False),
        sa.Column("delimiter", sa.String(length=1), nullable=False),
        sa.Column("decimal_sep", sa.String(length=1), nullable=False),
        sa.Column("date_format", sa.String(length=32), nullable=False),
        sa.Column("skip_rows", sa.Integer(), nullable=False),
        sa.Column("column_map", sa.JSON(), nullable=False),
        sa.CheckConstraint("format IN ('csv', 'ofx')", name=op.f("ck_import_profile_file_format")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_import_profile")),
        sa.UniqueConstraint("name", name=op.f("uq_import_profile_name")),
    )
    op.create_table(
        "job_run",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_name", sa.String(length=64), nullable=False),
        sa.Column("slot_start", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(length=7), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("stats", sa.JSON(), nullable=False),
        sa.CheckConstraint(
            "status IN ('running', 'success', 'failed', 'skipped')",
            name=op.f("ck_job_run_job_status"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_run")),
        sa.UniqueConstraint("job_name", "slot_start", name=op.f("uq_job_run_job_name_slot_start")),
    )
    with op.batch_alter_table("job_run", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_job_run_status"), ["status"], unique=False)

    op.create_table(
        "account",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("connection_id", sa.Integer(), nullable=True),
        sa.Column("provider_account_uid", sa.String(length=128), nullable=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("type", sa.String(length=8), nullable=False),
        sa.Column("iban_enc", sa.LargeBinary(), nullable=True),
        sa.Column("iban_hash", sa.String(length=64), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("include_in_net_worth", sa.Boolean(), nullable=False),
        sa.Column("syncs_per_day", sa.Integer(), nullable=False),
        sa.Column("opened_at", sa.Date(), nullable=True),
        sa.Column("closed_at", sa.Date(), nullable=True),
        sa.CheckConstraint(
            "type IN ('checking', 'savings', 'card', 'other')", name=op.f("ck_account_account_type")
        ),
        sa.CheckConstraint("syncs_per_day BETWEEN 1 AND 4", name=op.f("ck_account_syncs_per_day")),
        sa.ForeignKeyConstraint(
            ["connection_id"], ["connection.id"], name=op.f("fk_account_connection_id_connection")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_account")),
        sa.UniqueConstraint(
            "connection_id",
            "provider_account_uid",
            name=op.f("uq_account_connection_id_provider_account_uid"),
        ),
    )
    with op.batch_alter_table("account", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_account_iban_hash"), ["iban_hash"], unique=False)

    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("at", sa.DateTime(), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("object_type", sa.String(length=64), nullable=True),
        sa.Column("object_id", sa.Integer(), nullable=True),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["actor_user_id"], ["app_user.id"], name=op.f("fk_audit_log_actor_user_id_app_user")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
    )
    with op.batch_alter_table("audit_log", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_audit_log_at"), ["at"], unique=False)

    op.create_table(
        "notification",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("alert_id", sa.Integer(), nullable=False),
        sa.Column("channel", sa.String(length=5), nullable=False),
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(length=7), nullable=False),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.CheckConstraint("channel IN ('ntfy', 'email')", name=op.f("ck_notification_channel")),
        sa.CheckConstraint(
            "kind IN ('initial', 'reminder', 'fallback')",
            name=op.f("ck_notification_notification_kind"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'sent', 'failed')",
            name=op.f("ck_notification_notification_status"),
        ),
        sa.ForeignKeyConstraint(
            ["alert_id"], ["alert.id"], name=op.f("fk_notification_alert_id_alert")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification")),
        sa.UniqueConstraint(
            "alert_id",
            "channel",
            "kind",
            "sequence",
            name=op.f("uq_notification_alert_id_channel_kind_sequence"),
        ),
    )
    op.create_table(
        "rule",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("conditions", sa.JSON(), nullable=False),
        sa.Column("category_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["category_id"], ["category.id"], name=op.f("fk_rule_category_id_category")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rule")),
    )
    with op.batch_alter_table("rule", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_rule_is_active_priority"), ["is_active", "priority"], unique=False
        )

    op.create_table(
        "user_session",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_user_session_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_session")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_user_session_token_hash")),
    )
    with op.batch_alter_table("user_session", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_user_session_expires_at"), ["expires_at"], unique=False
        )

    op.create_table(
        "account_sync_state",
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("last_success_at", sa.DateTime(), nullable=True),
        sa.Column("last_booking_date_seen", sa.Date(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["account_id"], ["account.id"], name=op.f("fk_account_sync_state_account_id_account")
        ),
        sa.PrimaryKeyConstraint("account_id", name=op.f("pk_account_sync_state")),
    )
    op.create_table(
        "import_batch",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=14), nullable=False),
        sa.Column("profile_id", sa.Integer(), nullable=True),
        sa.Column("file_name", sa.String(length=255), nullable=True),
        sa.Column("file_sha256", sa.String(length=64), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(length=7), nullable=False),
        sa.Column("n_seen", sa.Integer(), nullable=False),
        sa.Column("n_inserted", sa.Integer(), nullable=False),
        sa.Column("n_duplicates", sa.Integer(), nullable=False),
        sa.Column("job_run_id", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "source IN ('enable_banking', 'csv', 'ofx')", name=op.f("ck_import_batch_import_source")
        ),
        sa.CheckConstraint(
            "status IN ('running', 'success', 'failed')", name=op.f("ck_import_batch_import_status")
        ),
        sa.ForeignKeyConstraint(
            ["account_id"], ["account.id"], name=op.f("fk_import_batch_account_id_account")
        ),
        sa.ForeignKeyConstraint(
            ["job_run_id"], ["job_run.id"], name=op.f("fk_import_batch_job_run_id_job_run")
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["import_profile.id"],
            name=op.f("fk_import_batch_profile_id_import_profile"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_import_batch")),
    )
    with op.batch_alter_table("import_batch", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_import_batch_account_id_started_at"),
            ["account_id", "started_at"],
            unique=False,
        )

    op.create_table(
        "provider_call",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("connection_id", sa.Integer(), nullable=True),
        sa.Column("requested_at", sa.DateTime(), nullable=False),
        sa.Column("unattended", sa.Boolean(), nullable=False),
        sa.Column("method", sa.String(length=8), nullable=False),
        sa.Column("path_template", sa.String(length=255), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("job_run_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["account_id"], ["account.id"], name=op.f("fk_provider_call_account_id_account")
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["connection.id"],
            name=op.f("fk_provider_call_connection_id_connection"),
        ),
        sa.ForeignKeyConstraint(
            ["job_run_id"], ["job_run.id"], name=op.f("fk_provider_call_job_run_id_job_run")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_provider_call")),
    )
    with op.batch_alter_table("provider_call", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_provider_call_account_id_requested_at"),
            ["account_id", "requested_at"],
            unique=False,
        )

    op.create_table(
        "raw_payload",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("import_batch_id", sa.Integer(), nullable=True),
        sa.Column("source", sa.String(length=14), nullable=False),
        sa.Column("received_at", sa.DateTime(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("content_type", sa.String(length=64), nullable=False),
        sa.Column("content_enc", sa.LargeBinary(), nullable=False),
        sa.CheckConstraint(
            "source IN ('enable_banking', 'csv', 'ofx')",
            name=op.f("ck_raw_payload_raw_payload_source"),
        ),
        sa.ForeignKeyConstraint(
            ["import_batch_id"],
            ["import_batch.id"],
            name=op.f("fk_raw_payload_import_batch_id_import_batch"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_raw_payload")),
        sa.UniqueConstraint("content_sha256", name=op.f("uq_raw_payload_content_sha256")),
    )
    op.create_table(
        "balance_snapshot",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("as_of_date", sa.Date(), nullable=False),
        sa.Column("balance_type", sa.String(length=9), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.Column("raw_payload_id", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "balance_type IN ('booked', 'available', 'other')",
            name=op.f("ck_balance_snapshot_balance_type"),
        ),
        sa.ForeignKeyConstraint(
            ["account_id"], ["account.id"], name=op.f("fk_balance_snapshot_account_id_account")
        ),
        sa.ForeignKeyConstraint(
            ["raw_payload_id"],
            ["raw_payload.id"],
            name=op.f("fk_balance_snapshot_raw_payload_id_raw_payload"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_balance_snapshot")),
        sa.UniqueConstraint(
            "account_id",
            "as_of_date",
            "balance_type",
            name=op.f("uq_balance_snapshot_account_id_as_of_date_balance_type"),
        ),
    )
    op.create_table(
        "ledger_transaction",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("dedupe_key", sa.String(length=160), nullable=False),
        sa.Column("external_id", sa.String(length=128), nullable=True),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("occurrence", sa.Integer(), nullable=False),
        sa.Column("booking_date", sa.Date(), nullable=False),
        sa.Column("value_date", sa.Date(), nullable=True),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("counterparty_name_enc", sa.LargeBinary(), nullable=True),
        sa.Column("counterparty_iban_enc", sa.LargeBinary(), nullable=True),
        sa.Column("counterparty_iban_hash", sa.String(length=64), nullable=True),
        sa.Column("label_raw", sa.Text(), nullable=False),
        sa.Column("label_clean", sa.Text(), nullable=False),
        sa.Column("bank_tx_code", sa.String(length=64), nullable=True),
        sa.Column("raw_payload_id", sa.Integer(), nullable=True),
        sa.Column("import_batch_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id"], ["account.id"], name=op.f("fk_ledger_transaction_account_id_account")
        ),
        sa.ForeignKeyConstraint(
            ["import_batch_id"],
            ["import_batch.id"],
            name=op.f("fk_ledger_transaction_import_batch_id_import_batch"),
        ),
        sa.ForeignKeyConstraint(
            ["raw_payload_id"],
            ["raw_payload.id"],
            name=op.f("fk_ledger_transaction_raw_payload_id_raw_payload"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ledger_transaction")),
        sa.UniqueConstraint(
            "account_id", "dedupe_key", name=op.f("uq_ledger_transaction_account_id_dedupe_key")
        ),
    )
    with op.batch_alter_table("ledger_transaction", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_ledger_transaction_account_id_booking_date"),
            ["account_id", "booking_date"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_ledger_transaction_booking_date"), ["booking_date"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_ledger_transaction_counterparty_iban_hash"),
            ["counterparty_iban_hash"],
            unique=False,
        )

    op.create_table(
        "duplicate_link",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("canonical_tx_id", sa.Integer(), nullable=False),
        sa.Column("duplicate_tx_id", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(length=6), nullable=False),
        sa.Column("reason", sa.String(length=9), nullable=False),
        sa.Column("status", sa.String(length=9), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "mode IN ('auto', 'manual')", name=op.f("ck_duplicate_link_duplicate_mode")
        ),
        sa.CheckConstraint(
            "reason IN ('reference', 'exact', 'multiset', 'probable')",
            name=op.f("ck_duplicate_link_duplicate_reason"),
        ),
        sa.CheckConstraint(
            "status IN ('proposed', 'confirmed', 'rejected', 'reverted')",
            name=op.f("ck_duplicate_link_duplicate_status"),
        ),
        sa.CheckConstraint(
            "canonical_tx_id <> duplicate_tx_id",
            name=op.f("ck_duplicate_link_distinct_transactions"),
        ),
        sa.ForeignKeyConstraint(
            ["canonical_tx_id"],
            ["ledger_transaction.id"],
            name=op.f("fk_duplicate_link_canonical_tx_id_ledger_transaction"),
        ),
        sa.ForeignKeyConstraint(
            ["duplicate_tx_id"],
            ["ledger_transaction.id"],
            name=op.f("fk_duplicate_link_duplicate_tx_id_ledger_transaction"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_duplicate_link")),
    )
    with op.batch_alter_table("duplicate_link", schema=None) as batch_op:
        batch_op.create_index(
            "uq_duplicate_link_active_duplicate",
            ["duplicate_tx_id"],
            unique=True,
            sqlite_where=sa.text("status IN ('proposed', 'confirmed')"),
        )

    op.create_table(
        "categorization",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("transaction_id", sa.Integer(), nullable=False),
        sa.Column("category_id", sa.Integer(), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=6), nullable=False),
        sa.Column("rule_id", sa.Integer(), nullable=True),
        sa.Column("confidence_permille", sa.Integer(), nullable=True),
        sa.Column("carried_by_link_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("superseded_at", sa.DateTime(), nullable=True),
        sa.Column("superseded_by_link_id", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "source IN ('manual', 'rule', 'ai')",
            name=op.f("ck_categorization_categorization_source"),
        ),
        sa.CheckConstraint(
            "confidence_permille IS NULL OR confidence_permille BETWEEN 0 AND 1000",
            name=op.f("ck_categorization_confidence_range"),
        ),
        sa.ForeignKeyConstraint(
            ["carried_by_link_id"],
            ["duplicate_link.id"],
            name=op.f("fk_categorization_carried_by_link_id_duplicate_link"),
        ),
        sa.ForeignKeyConstraint(
            ["category_id"], ["category.id"], name=op.f("fk_categorization_category_id_category")
        ),
        sa.ForeignKeyConstraint(
            ["rule_id"], ["rule.id"], name=op.f("fk_categorization_rule_id_rule")
        ),
        sa.ForeignKeyConstraint(
            ["superseded_by_link_id"],
            ["duplicate_link.id"],
            name=op.f("fk_categorization_superseded_by_link_id_duplicate_link"),
        ),
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["ledger_transaction.id"],
            name=op.f("fk_categorization_transaction_id_ledger_transaction"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_categorization")),
    )
    with op.batch_alter_table("categorization", schema=None) as batch_op:
        batch_op.create_index(
            "ix_categorization_active_transaction",
            ["transaction_id"],
            unique=False,
            sqlite_where=sa.text("superseded_at IS NULL"),
        )

    op.create_table(
        "review_item",
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
        sa.CheckConstraint(
            "kind IN ('duplicate', 'category_conflict', 'transfer_conflict', 'transfer_candidate')",
            name=op.f("ck_review_item_review_kind"),
        ),
        sa.CheckConstraint(
            "status IN ('open', 'resolved', 'dismissed')", name=op.f("ck_review_item_review_status")
        ),
        sa.ForeignKeyConstraint(
            ["duplicate_link_id"],
            ["duplicate_link.id"],
            name=op.f("fk_review_item_duplicate_link_id_duplicate_link"),
        ),
        sa.ForeignKeyConstraint(
            ["other_transaction_id"],
            ["ledger_transaction.id"],
            name=op.f("fk_review_item_other_transaction_id_ledger_transaction"),
        ),
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["ledger_transaction.id"],
            name=op.f("fk_review_item_transaction_id_ledger_transaction"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_review_item")),
        sa.UniqueConstraint("dedupe_key", name=op.f("uq_review_item_dedupe_key")),
    )
    with op.batch_alter_table("review_item", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_review_item_status_kind"), ["status", "kind"], unique=False
        )

    op.create_table(
        "transfer_link",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("debit_tx_id", sa.Integer(), nullable=False),
        sa.Column("credit_tx_id", sa.Integer(), nullable=False),
        sa.Column("method", sa.String(length=6), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("carried_by_link_id", sa.Integer(), nullable=True),
        sa.Column("superseded_by_link_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "method IN ('auto', 'manual')", name=op.f("ck_transfer_link_transfer_method")
        ),
        sa.CheckConstraint(
            "status IN ('proposed', 'confirmed', 'rejected', 'superseded')",
            name=op.f("ck_transfer_link_transfer_status"),
        ),
        sa.CheckConstraint(
            "debit_tx_id <> credit_tx_id", name=op.f("ck_transfer_link_distinct_transactions")
        ),
        sa.ForeignKeyConstraint(
            ["carried_by_link_id"],
            ["duplicate_link.id"],
            name=op.f("fk_transfer_link_carried_by_link_id_duplicate_link"),
        ),
        sa.ForeignKeyConstraint(
            ["credit_tx_id"],
            ["ledger_transaction.id"],
            name=op.f("fk_transfer_link_credit_tx_id_ledger_transaction"),
        ),
        sa.ForeignKeyConstraint(
            ["debit_tx_id"],
            ["ledger_transaction.id"],
            name=op.f("fk_transfer_link_debit_tx_id_ledger_transaction"),
        ),
        sa.ForeignKeyConstraint(
            ["superseded_by_link_id"],
            ["duplicate_link.id"],
            name=op.f("fk_transfer_link_superseded_by_link_id_duplicate_link"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_transfer_link")),
    )
    with op.batch_alter_table("transfer_link", schema=None) as batch_op:
        batch_op.create_index(
            "uq_transfer_link_active_credit",
            ["credit_tx_id"],
            unique=True,
            sqlite_where=sa.text("status IN ('proposed', 'confirmed')"),
        )
        batch_op.create_index(
            "uq_transfer_link_active_debit",
            ["debit_tx_id"],
            unique=True,
            sqlite_where=sa.text("status IN ('proposed', 'confirmed')"),
        )

    for statement in TRIGGERS.values():
        op.execute(statement)


def downgrade() -> None:
    for name in TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
    with op.batch_alter_table("transfer_link", schema=None) as batch_op:
        batch_op.drop_index(
            "uq_transfer_link_active_debit",
            sqlite_where=sa.text("status IN ('proposed', 'confirmed')"),
        )
        batch_op.drop_index(
            "uq_transfer_link_active_credit",
            sqlite_where=sa.text("status IN ('proposed', 'confirmed')"),
        )

    op.drop_table("transfer_link")
    with op.batch_alter_table("review_item", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_review_item_status_kind"))

    op.drop_table("review_item")
    with op.batch_alter_table("categorization", schema=None) as batch_op:
        batch_op.drop_index(
            "ix_categorization_active_transaction", sqlite_where=sa.text("superseded_at IS NULL")
        )

    op.drop_table("categorization")
    with op.batch_alter_table("duplicate_link", schema=None) as batch_op:
        batch_op.drop_index(
            "uq_duplicate_link_active_duplicate",
            sqlite_where=sa.text("status IN ('proposed', 'confirmed')"),
        )

    op.drop_table("duplicate_link")
    with op.batch_alter_table("ledger_transaction", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_ledger_transaction_counterparty_iban_hash"))
        batch_op.drop_index(batch_op.f("ix_ledger_transaction_booking_date"))
        batch_op.drop_index(batch_op.f("ix_ledger_transaction_account_id_booking_date"))

    op.drop_table("ledger_transaction")
    op.drop_table("balance_snapshot")
    op.drop_table("raw_payload")
    with op.batch_alter_table("provider_call", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_provider_call_account_id_requested_at"))

    op.drop_table("provider_call")
    with op.batch_alter_table("import_batch", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_import_batch_account_id_started_at"))

    op.drop_table("import_batch")
    op.drop_table("account_sync_state")
    with op.batch_alter_table("user_session", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_user_session_expires_at"))

    op.drop_table("user_session")
    with op.batch_alter_table("rule", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_rule_is_active_priority"))

    op.drop_table("rule")
    op.drop_table("notification")
    with op.batch_alter_table("audit_log", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_audit_log_at"))

    op.drop_table("audit_log")
    with op.batch_alter_table("account", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_account_iban_hash"))

    op.drop_table("account")
    with op.batch_alter_table("job_run", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_job_run_status"))

    op.drop_table("job_run")
    op.drop_table("import_profile")
    with op.batch_alter_table("connection", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_connection_consent_expires_at"))

    op.drop_table("connection")
    op.drop_table("category")
    op.drop_table("app_user")
    with op.batch_alter_table("alert", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_alert_status_severity"))

    op.drop_table("alert")
