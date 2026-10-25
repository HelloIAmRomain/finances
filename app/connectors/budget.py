"""Budget DSP2 (inv. 10) : chaque requête vers une banque est comptée dans `provider_call`.

- Au plus `limit` requêtes par compte et par type d'endpoint (soldes, transactions…) sur 24 h
  glissantes, **toutes requêtes comprises** : synchros automatiques et manuelles, reprises,
  rattrapages, pages. Le plafond vient du profil de la banque (`bank_profiles`, hypothèse Q14).
- Une synchro manuelle consomme ce budget : les en-têtes PSU (IP de l'utilisateur) ne sont pas
  fiables derrière Tailscale, la banque ne peut donc pas la distinguer d'une synchro automatique.
  Elle est refusée s'il ne reste qu'une requête sur 24 h (gardée pour le prochain créneau) et
  reste limitée à une par quart d'heure et par compte.
- Une requête refusée n'est pas enregistrée ; une requête acceptée l'est **avant** d'être envoyée
  (une coupure pendant l'envoi ne la fait pas oublier).
- Après une restauration, les requêtes des 15 dernières minutes sont inconnues : une requête
  est réservée par compte et endpoint (au plus un créneau tombe dans la fenêtre perdue, §1.5).
- Une réponse 429 est journalisée avec le nombre de requêtes envoyées dans les 24 h qui
  précèdent : c'est la limite réellement observée (`observed_limits`).
"""

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import Connection, Engine, func, insert, select, update

from app.connectors.bank_profiles import profile_for
from app.db import write_transaction
from app.models import Account, ProviderCall
from app.models import Connection as BankConnection
from app.timeutil import ensure_utc, to_paris

log = logging.getLogger(__name__)

WINDOW = timedelta(hours=24)
MANUAL_MIN_INTERVAL = timedelta(minutes=15)
RESTORE_METHOD = "RESTORE"
SYNC_ENDPOINTS = ("balances", "transactions")
SYNC_BURST = timedelta(minutes=5)
HTTP_TOO_MANY_REQUESTS = 429


class BudgetExceededError(RuntimeError):
    """Plafond DSP2 atteint pour ce compte et cet endpoint."""


class ManualSyncRefusedError(RuntimeError):
    """Synchro manuelle impossible pour l'instant."""


class ManualSyncTooSoonError(ManualSyncRefusedError):
    """Synchro manuelle relancée moins d'un quart d'heure après la précédente."""


class ManualSyncBudgetError(ManualSyncRefusedError):
    """Il ne reste qu'une requête sur 24 h : elle est gardée pour la synchro automatique."""


@dataclass(frozen=True)
class ObservedLimit:
    at: datetime
    bank: str
    account_id: int
    endpoint: str
    calls_in_window: int  # requêtes envoyées sur les 24 h précédentes, la refusée comprise


def _bank_of(connection: Connection, account_id: int) -> str | None:
    return connection.execute(
        select(BankConnection.institution_name)
        .join(Account, Account.connection_id == BankConnection.id)
        .where(Account.id == account_id)
    ).scalar_one_or_none()


def _limit(connection: Connection, account_id: int, endpoint: str) -> int:
    return profile_for(_bank_of(connection, account_id)).limit(endpoint)


def _calls(connection: Connection, account_id: int, endpoint: str, now: datetime) -> list[datetime]:
    """Instants des requêtes de la fenêtre, de la plus ancienne à la plus récente."""
    return [
        ensure_utc(at)
        for at in connection.scalars(
            select(ProviderCall.requested_at)
            .where(
                ProviderCall.account_id == account_id,
                ProviderCall.path_template == endpoint,
                ProviderCall.requested_at > now - WINDOW,
            )
            .order_by(ProviderCall.requested_at)
        )
    ]


def remaining(engine: Engine, *, account_id: int, endpoint: str, now: datetime) -> int:
    now = ensure_utc(now)
    with engine.connect() as connection:
        used = len(_calls(connection, account_id, endpoint, now))
        return max(_limit(connection, account_id, endpoint) - used, 0)


def usage(engine: Engine, *, account_id: int, now: datetime) -> list[tuple[str, int, int]]:
    """(endpoint, requêtes des 24 dernières heures, plafond) pour les endpoints d'une synchro."""
    now = ensure_utc(now)
    with engine.connect() as connection:
        return [
            (
                endpoint,
                len(_calls(connection, account_id, endpoint, now)),
                _limit(connection, account_id, endpoint),
            )
            for endpoint in SYNC_ENDPOINTS
        ]


def acquire(
    engine: Engine,
    *,
    account_id: int,
    endpoint: str,
    unattended: bool,
    method: str,
    now: datetime,
    connection_id: int | None = None,
    job_run_id: int | None = None,
) -> int:
    """Enregistre une requête sur le point d'être envoyée ; lève `BudgetExceededError` sinon."""
    now = ensure_utc(now)
    with write_transaction(engine) as connection:
        limit = _limit(connection, account_id, endpoint)
        if len(_calls(connection, account_id, endpoint, now)) >= limit:
            raise BudgetExceededError(
                f"plafond DSP2 atteint : compte {account_id}, {endpoint}, {limit} requêtes sur 24 h"
            )
        result = connection.execute(
            insert(ProviderCall).values(
                account_id=account_id,
                connection_id=connection_id,
                requested_at=now,
                unattended=unattended,
                method=method,
                path_template=endpoint,
                job_run_id=job_run_id,
            )
        )
        key = result.inserted_primary_key
        if key is None:
            raise RuntimeError("identifiant de provider_call non renvoyé par la base")
        return int(key[0])


def manual_sync_blocker(
    engine: Engine, *, account_id: int, now: datetime
) -> ManualSyncRefusedError | None:
    """Raison pour laquelle une synchro manuelle est impossible maintenant, ou None."""
    now = ensure_utc(now)
    with engine.connect() as connection:
        last = connection.execute(
            select(func.max(ProviderCall.requested_at)).where(
                ProviderCall.account_id == account_id, ProviderCall.unattended.is_(False)
            )
        ).scalar_one()
        if last is not None and now - ensure_utc(last) < MANUAL_MIN_INTERVAL:
            wait = MANUAL_MIN_INTERVAL - (now - ensure_utc(last))
            minutes = -(-int(wait.total_seconds()) // 60)  # arrondi à la minute supérieure
            return ManualSyncTooSoonError(
                "Une mise à jour manuelle par quart d'heure au plus : nouvel essai possible dans "
                f"{minutes} min."
            )
        blocked, never, available = False, False, []
        for endpoint in SYNC_ENDPOINTS:
            calls = _calls(connection, account_id, endpoint, now)
            # Il doit rester de quoi faire cette synchro **et** une synchro automatique après.
            excess = len(calls) - (_limit(connection, account_id, endpoint) - 2 * _sync_cost(calls))
            if excess > len(calls):
                blocked = never = True  # plafond trop bas pour deux synchros
            elif excess > 0:
                blocked = True
                available.append(calls[excess - 1] + WINDOW)
    if not blocked:
        return None
    when = (
        "Cette banque n'accepte pas assez d'accès par jour pour une mise à jour manuelle : les"
        " mises à jour automatiques suffisent."
        if never
        else f"Nouvel essai possible après {to_paris(max(available)):%H:%M}."
    )
    return ManualSyncBudgetError(
        "Mise à jour manuelle indisponible pour l'instant : la banque limite le nombre d'accès "
        "par 24 h (réglementation européenne DSP2), mises à jour manuelles comprises, et il ne "
        f"reste que celui de la prochaine mise à jour automatique. {when}"
    )


def _sync_cost(calls: list[datetime]) -> int:
    """Requêtes d'une synchro pour cet endpoint (pages comprises), estimées d'après la dernière :
    celles envoyées dans les minutes qui précèdent la plus récente."""
    if not calls:
        return 1
    return sum(1 for at in calls if at > calls[-1] - SYNC_BURST)


def check_manual_sync(engine: Engine, *, account_id: int, now: datetime) -> None:
    refusal = manual_sync_blocker(engine, account_id=account_id, now=now)
    if refusal is not None:
        raise refusal


def record_response(engine: Engine, call_id: int, http_status: int) -> None:
    with write_transaction(engine) as connection:
        connection.execute(
            update(ProviderCall).where(ProviderCall.id == call_id).values(http_status=http_status)
        )
    if http_status == HTTP_TOO_MANY_REQUESTS:
        for limit in observed_limits(engine, call_id=call_id):
            log.warning(
                "limite DSP2 observée : %s, %s, refus après %d requêtes sur 24 h (compte %d)",
                limit.bank,
                limit.endpoint,
                limit.calls_in_window,
                limit.account_id,
            )


def observed_limits(
    engine: Engine, *, since: datetime | None = None, call_id: int | None = None
) -> list[ObservedLimit]:
    """Refus de quota (429) constatés, avec le nombre de requêtes des 24 h qui précèdent."""
    query = (
        select(
            ProviderCall.id,
            ProviderCall.requested_at,
            ProviderCall.account_id,
            ProviderCall.path_template,
        )
        .where(ProviderCall.http_status == HTTP_TOO_MANY_REQUESTS)
        .order_by(ProviderCall.requested_at)
    )
    if since is not None:
        query = query.where(ProviderCall.requested_at >= ensure_utc(since))
    if call_id is not None:
        query = query.where(ProviderCall.id == call_id)
    limits = []
    with engine.connect() as connection:
        for row in connection.execute(query).all():
            at = ensure_utc(row.requested_at)
            calls = connection.execute(
                select(func.count()).where(
                    ProviderCall.account_id == row.account_id,
                    ProviderCall.path_template == row.path_template,
                    ProviderCall.requested_at > at - WINDOW,
                    ProviderCall.requested_at <= at,
                    ProviderCall.method != RESTORE_METHOD,
                )
            ).scalar_one()
            bank = _bank_of(connection, row.account_id) or "inconnue"
            limits.append(ObservedLimit(at, bank, row.account_id, row.path_template, int(calls)))
    return limits


def reserve_after_restore(
    engine: Engine, *, account_ids: Iterable[int], endpoints: Iterable[str], now: datetime
) -> None:
    now = ensure_utc(now)
    endpoint_list = list(endpoints)
    with write_transaction(engine) as connection:
        for account_id in account_ids:
            for endpoint in endpoint_list:
                connection.execute(
                    insert(ProviderCall).values(
                        account_id=account_id,
                        requested_at=now,
                        unattended=True,
                        method=RESTORE_METHOD,
                        path_template=endpoint,
                    )
                )
