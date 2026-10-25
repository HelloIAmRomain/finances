"""Synchronisation bancaire d'un compte (COL-01, COL-07, NF-RES-03, inv. 10).

Pour un compte relié à une banque active :
1. soldes (conservés bruts, un par jour et par type) ;
2. transactions depuis la dernière date comptable vue moins 7 jours de recouvrement (90 jours au
   premier passage) : une coupure ou une écriture tardive est rattrapée, l'ingestion écarte les
   doublons (inv. 3) ;
3. doublons entre sources, virements internes, règles sur les nouvelles transactions.

Consentement refusé par la banque : connexion « expirée », alerte critique, pas de reprise.
Quota atteint (429 de la banque ou plafond DSP2 local) : la synchro est **reportée** au créneau
suivant, sans reprise et sans compter comme une panne (Q14) ; seule l'alerte « aucune synchro
réussie depuis 24 h » (`check_sync_freshness`) finit par prévenir.
Chaque requête passe par le budget DSP2 (automatique ou manuelle, inv. 10).
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from sqlalchemy import Engine, func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.alerts.engine import raise_alert, resolve_by_key
from app.categorize.service import apply_rules
from app.connectors.base import (
    AccountInaccessibleError,
    BankConnector,
    ConnectorError,
    ConsentExpiredError,
    Page,
    RemoteBalance,
    RemoteTransaction,
)
from app.connectors.budget import BudgetExceededError
from app.connectors.requester import BankRateLimitedError, Budget
from app.db import write_transaction
from app.ledger.cross_dedupe import detect_duplicates
from app.ledger.ingest import ingest, store_raw_payload
from app.ledger.transfers import detect_transfers
from app.models import Account, AccountSyncState, BalanceSnapshot, Connection, LedgerTransaction
from app.models.alerts import Severity
from app.models.ledger import ConnectionStatus, ImportSource, Provider
from app.timeutil import ensure_utc, paris_date

log = logging.getLogger(__name__)

OVERLAP = timedelta(days=7)
INITIAL_HISTORY = timedelta(days=90)
ERROR_MAX_LENGTH = 500
INACCESSIBLE_PAUSE = timedelta(hours=24)
INACCESSIBLE_MARK = AccountInaccessibleError.__name__  # début de last_error, voir sync_account
INACCESSIBLE_HELP = (
    "La banque ne donne plus accès à ce compte avec le consentement actuel. Cas habituel : carte "
    "renouvelée ou remplacée (la nouvelle carte est un nouveau compte). Relier la banque à "
    "nouveau (page Banques) ; l'ancien compte peut ensuite être ignoré."
)
# Heures de synchro automatique (Paris) selon la fréquence du compte (DT §3).
SCHEDULES: dict[int, tuple[time, ...]] = {
    1: (time(6, 30),),
    2: (time(6, 30), time(18, 30)),
    3: (time(6, 30), time(12, 30), time(18, 30)),
    4: (time(6, 30), time(11, 30), time(16, 30), time(21, 30)),
}


class SyncError(RuntimeError):
    """Compte impossible à synchroniser (pas de connexion active, pas d'identifiant)."""


@dataclass(frozen=True)
class SyncResult:
    account_id: int
    seen: int
    inserted: int
    balances: int


@dataclass(frozen=True)
class SyncDeferred:
    """Synchro reportée au créneau suivant : quota de la banque ou plafond DSP2 atteint."""

    account_id: int
    reason: str


@dataclass(frozen=True)
class _Target:
    account_id: int
    uid: str
    connection_id: int
    last_booking_date: date | None


def linked_accounts(engine: Engine) -> list[tuple[int, int]]:
    """(compte, synchros par jour) des comptes reliés à une connexion Enable Banking active."""
    with engine.connect() as connection:
        rows = connection.execute(
            select(Account.id, Account.syncs_per_day)
            .join(Connection, Connection.id == Account.connection_id)
            .where(
                Connection.provider == Provider.ENABLE_BANKING,
                Connection.status == ConnectionStatus.ACTIVE,
                Account.provider_account_uid.is_not(None),
                Account.closed_at.is_(None),
                Account.ignored_at.is_(None),
            )
            .order_by(Account.id)
        ).all()
    return [(account_id, per_day) for account_id, per_day in rows]


def _target(engine: Engine, account_id: int) -> _Target:
    with engine.connect() as connection:
        row = connection.execute(
            select(
                Account.provider_account_uid,
                Account.connection_id,
                Connection.status,
                AccountSyncState.last_booking_date_seen,
                Account.ignored_at,
            )
            .join(Connection, Connection.id == Account.connection_id)
            .outerjoin(AccountSyncState, AccountSyncState.account_id == Account.id)
            .where(Account.id == account_id)
        ).first()
    if row is None or row.provider_account_uid is None or row.connection_id is None:
        raise SyncError(f"compte {account_id} non relié à une banque")
    if row.ignored_at is not None:
        raise SyncError(f"compte {account_id} ignoré : aucune synchro")
    if row.status is not ConnectionStatus.ACTIVE:
        raise SyncError(f"compte {account_id} : connexion {row.status}")
    return _Target(
        account_id, row.provider_account_uid, row.connection_id, row.last_booking_date_seen
    )


def _save_state(engine: Engine, account_id: int, **values: object) -> None:
    with write_transaction(engine) as connection:
        statement = sqlite_insert(AccountSyncState).values(account_id=account_id, **values)
        connection.execute(
            statement.on_conflict_do_update(index_elements=["account_id"], set_=values)
        )


def _store_balances(
    engine: Engine, account_id: int, page: Page[RemoteBalance], source: ImportSource, now: datetime
) -> int:
    raw_id = store_raw_payload(engine, None, source, page.raw)
    with write_transaction(engine) as connection:
        for balance in page.items:
            statement = sqlite_insert(BalanceSnapshot).values(
                account_id=account_id,
                as_of_date=balance.as_of,
                balance_type=balance.type,
                amount_cents=balance.amount,
                currency=balance.currency,
                fetched_at=now,
                raw_payload_id=raw_id,
            )
            connection.execute(
                statement.on_conflict_do_update(
                    index_elements=["account_id", "as_of_date", "balance_type"],
                    set_={
                        "amount_cents": balance.amount,
                        "fetched_at": now,
                        "raw_payload_id": raw_id,
                    },
                )
            )
    return len(page.items)


@dataclass(frozen=True)
class _Fetched:
    balances: int
    balance_error: str | None
    pages: list[Page[RemoteTransaction]]


def _fetch(
    engine: Engine,
    connector: BankConnector,
    target: _Target,
    budget: Budget,
    *,
    today: date,
    now: datetime,
) -> _Fetched:
    """Soldes (enregistrés tout de suite) puis pages d'opérations, toutes lues avant d'en
    enregistrer une : une coupure au milieu ne laisse pas d'import partiel."""
    balance_error: str | None = None
    try:
        page = connector.balances(target.uid, budget, today=today)
        balances = _store_balances(engine, target.account_id, page, connector.source, now)
    except ConsentExpiredError:
        raise
    except BudgetExceededError as exc:
        # Budget DSP2 des soldes épuisé : celui des opérations est compté à part (inv. 10).
        balances, balance_error = 0, f"solde reporté : {exc}"[:ERROR_MAX_LENGTH]
    except ConnectorError as exc:
        # Solde refusé (vu sur deux cartes Boursorama en octobre 2026) : les opérations restent
        # lisibles, on les récupère quand même ; l'erreur reste visible.
        balances, balance_error = 0, f"solde indisponible : {exc}"[:ERROR_MAX_LENGTH]
    if balance_error:
        log.warning("synchro du compte %d : %s", target.account_id, balance_error)
    date_from = (
        target.last_booking_date - OVERLAP if target.last_booking_date else today - INITIAL_HISTORY
    )
    pages = list(connector.transactions(target.uid, date_from, budget))
    return _Fetched(balances, balance_error, pages)


def _record_failure(engine: Engine, account_id: int, exc: Exception, now: datetime) -> None:
    _save_state(engine, account_id, last_error=f"{type(exc).__name__}: {exc}"[:ERROR_MAX_LENGTH])
    if isinstance(exc, AccountInaccessibleError):
        raise_alert(
            engine,
            type="account_inaccessible",
            severity=Severity.WARNING,
            key=f"account_inaccessible:{account_id}",
            title="Un compte n'est plus accessible chez la banque (carte renouvelée ?)",
            subject=("account", account_id),
            now=now,
            details={"explication": INACCESSIBLE_HELP},
        )


def _ingest(
    engine: Engine,
    connector: BankConnector,
    account_id: int,
    fetched: _Fetched,
    *,
    now: datetime,
    job_run_id: int | None,
) -> SyncResult:
    """Opérations enregistrées sans doublon, puis doublons entre sources, virements internes et
    règles de catégorie ; état du compte et alertes de retard mis à jour."""
    stats = ingest(
        engine,
        account_id=account_id,
        source=connector.source,
        pages=fetched.pages,
        job_run_id=job_run_id,
    )
    detect_duplicates(engine, account_id=account_id)
    detect_transfers(engine)
    with engine.connect() as connection:
        new_ids = list(
            connection.scalars(
                select(LedgerTransaction.id).where(
                    LedgerTransaction.import_batch_id == stats.batch_id
                )
            )
        )
        last_booking = connection.execute(
            select(func.max(LedgerTransaction.booking_date)).where(
                LedgerTransaction.account_id == account_id,
                LedgerTransaction.dedupe_key.like(f"%:{connector.source}:%"),
            )
        ).scalar_one()
    apply_rules(engine, now=now, transaction_ids=new_ids)
    _save_state(
        engine,
        account_id,
        last_success_at=now,
        last_booking_date_seen=last_booking,
        last_error=fetched.balance_error,
    )
    resolve_by_key(engine, f"sync_stale:{account_id}", now=now)
    resolve_by_key(engine, f"account_inaccessible:{account_id}", now=now)
    return SyncResult(account_id, stats.seen, stats.inserted, fetched.balances)


def sync_account(
    engine: Engine,
    connector: BankConnector,
    account_id: int,
    *,
    unattended: bool,
    now: datetime,
    job_run_id: int | None = None,
) -> SyncResult | SyncDeferred | None:
    """Synchronise un compte ; renvoie None si la banque a refusé le consentement."""
    target = _target(engine, account_id)
    pause = _inaccessible_pause(engine, account_id, now) if unattended else None
    if pause is not None:
        return SyncDeferred(account_id, pause)
    budget = Budget(
        engine=engine,
        account_id=account_id,
        unattended=unattended,
        connection_id=target.connection_id,
        job_run_id=job_run_id,
    )
    _save_state(engine, account_id, last_attempt_at=now)
    try:
        fetched = _fetch(engine, connector, target, budget, today=paris_date(now), now=now)
    except ConsentExpiredError:
        _consent_refused(engine, target, now)
        return None
    except (BankRateLimitedError, BudgetExceededError) as exc:
        # Pas de reprise (la tâche réussit) ni de panne : on attend le prochain créneau.
        reason = f"reportée : {exc}"[:ERROR_MAX_LENGTH]
        log.warning("synchro du compte %d %s", account_id, reason)
        _save_state(engine, account_id, last_error=reason)
        return SyncDeferred(account_id, reason)
    except Exception as exc:
        _record_failure(engine, account_id, exc, now)
        raise
    return _ingest(engine, connector, account_id, fetched, now=now, job_run_id=job_run_id)


def _inaccessible_pause(engine: Engine, account_id: int, now: datetime) -> str | None:
    """Compte refusé par la banque (`AccountInaccessibleError`) il y a moins de 24 h : pas de
    nouvel essai automatique, chaque essai coûtant deux requêtes du budget DSP2 (inv. 10)."""
    with engine.connect() as connection:
        row = connection.execute(
            select(AccountSyncState.last_attempt_at, AccountSyncState.last_error).where(
                AccountSyncState.account_id == account_id
            )
        ).first()
    if (
        row is None
        or row.last_attempt_at is None
        or INACCESSIBLE_MARK not in (row.last_error or "")
    ):
        return None
    if now - ensure_utc(row.last_attempt_at) >= INACCESSIBLE_PAUSE:
        return None
    return "compte inaccessible chez la banque : nouvel essai 24 h après le dernier"


def _consent_refused(engine: Engine, target: _Target, now: datetime) -> None:
    with write_transaction(engine) as connection:
        connection.execute(
            update(Connection)
            .where(Connection.id == target.connection_id)
            .values(status=ConnectionStatus.EXPIRED, updated_at=now)
        )
    _save_state(engine, target.account_id, last_error="consentement refusé par la banque")
    raise_alert(
        engine,
        type="consent",
        severity=Severity.CRITICAL,
        key=f"consent_refused:{target.connection_id}",
        title="Consentement bancaire refusé : liaison à refaire",
        subject=("connection", target.connection_id),
        now=now,
    )
