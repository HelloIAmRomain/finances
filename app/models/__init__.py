"""Modèle de données du lot 1 (PLAN.md §5). Importer ce paquet enregistre toutes les tables.

Les déclencheurs d'immuabilité sont attachés ici à `create_all` et repris tels quels par les
migrations : une base de test n'existe jamais sans eux.
"""

from typing import Any

from sqlalchemy import Connection as DBConnection
from sqlalchemy import Table, event

from app.models.alerts import Alert, Notification
from app.models.assistant import (
    AiChangeSet,
    AiConfirmation,
    AiSetting,
    AiTerm,
    AiToken,
    AssistantConversation,
)
from app.models.auth import (
    AppUser,
    Device,
    LinkNonce,
    PairingCode,
    SecurityKey,
    UserSession,
    WebAuthnChallenge,
)
from app.models.categorize import Budget, Categorization, Category, ReviewItem, Rule
from app.models.ledger import (
    LEDGER_TRANSACTION_IMMUTABLE_SQL,
    Account,
    AccountSyncState,
    AssetValuation,
    BalanceSnapshot,
    Connection,
    DuplicateLink,
    ImportBatch,
    ImportProfile,
    LedgerTransaction,
    ManualAsset,
    ProviderCall,
    RawPayload,
    TransferLink,
)
from app.models.ops import AUDIT_LOG_APPEND_ONLY_SQL, AppSetting, AuditLog, JobRun
from app.models.rental import (
    DepositEvent,
    IrlIndex,
    Lease,
    Overpayment,
    OwnerShare,
    Party,
    Property,
    PropertyExpense,
    PropertyExpenseRule,
    RentAllocation,
    RentDue,
    RentIncident,
)
from app.models.tax import Deadline

TRIGGERS = (LEDGER_TRANSACTION_IMMUTABLE_SQL, *AUDIT_LOG_APPEND_ONLY_SQL)


def _create_triggers(*statements: str) -> Any:
    def listener(_table: Table, connection: DBConnection, **_kw: Any) -> None:
        for statement in statements:
            connection.exec_driver_sql(statement)

    return listener


event.listen(
    LedgerTransaction.__table__, "after_create", _create_triggers(LEDGER_TRANSACTION_IMMUTABLE_SQL)
)
event.listen(AuditLog.__table__, "after_create", _create_triggers(*AUDIT_LOG_APPEND_ONLY_SQL))

__all__ = [
    "TRIGGERS",
    "Account",
    "AccountSyncState",
    "AiChangeSet",
    "AiConfirmation",
    "AiSetting",
    "AiTerm",
    "AiToken",
    "Alert",
    "AppSetting",
    "AppUser",
    "AssetValuation",
    "AssistantConversation",
    "AuditLog",
    "BalanceSnapshot",
    "Budget",
    "Categorization",
    "Category",
    "Connection",
    "Deadline",
    "DepositEvent",
    "Device",
    "DuplicateLink",
    "ImportBatch",
    "ImportProfile",
    "IrlIndex",
    "JobRun",
    "Lease",
    "LedgerTransaction",
    "LinkNonce",
    "ManualAsset",
    "Notification",
    "Overpayment",
    "OwnerShare",
    "PairingCode",
    "Party",
    "Property",
    "PropertyExpense",
    "PropertyExpenseRule",
    "ProviderCall",
    "RawPayload",
    "RentAllocation",
    "RentDue",
    "RentIncident",
    "ReviewItem",
    "Rule",
    "SecurityKey",
    "TransferLink",
    "UserSession",
    "WebAuthnChallenge",
]
