"""Processus worker : exécute les tâches planifiées (collecte, rapprochement, alertes…)."""

import logging
import signal
import threading
from datetime import time, timedelta
from functools import partial

from sqlalchemy import Engine

from app.alerts.channels import Sender
from app.alerts.digest import weekly_digest
from app.alerts.dispatch import build_senders, dispatch
from app.categorize.ai import AiCategorizationError, categorize_pending
from app.categorize.budgets import check_budgets
from app.config import DEFAULT_SECRETS_DIR, Secrets, Settings, load_secrets
from app.connectors.bank_profiles import configure_profiles
from app.connectors.base import BankConnector
from app.connectors.enable_banking.client import EnableBankingClient
from app.connectors.enable_banking.connector import EnableBankingConnector
from app.connectors.enable_banking.factory import enable_banking_client
from app.crypto import Keyring, configure_keyring
from app.db import make_engine
from app.ledger.forecast import check_low_balances
from app.ledger.subscriptions import check_recurring
from app.ledger.sync import SCHEDULES, SyncDeferred, linked_accounts, sync_account
from app.models.alerts import Channel
from app.net.client import make_client
from app.ops import checks
from app.ops.healthchecks import HealthcheckPinger
from app.ops.jobs import JobContext, run_once
from app.ops.maintenance import maintain
from app.ops.restore_marker import apply_restore_marker
from app.ops.scheduler import DailyAt, Every, Job, Scheduler
from app.ops.startup_guard import check_startup
from app.rental.deposit import remind_deposits
from app.rental.engine import run_rental
from app.tax.calendar import remind_leases
from app.tax.deadlines import match_payments, remind
from app.timeutil import utcnow

log = logging.getLogger(__name__)
BANK_ATTEMPTS = 2

BACKUP_MARKER = ".backup-last-success"
BANK_JITTER = timedelta(minutes=5)


# --- Tâches : une fonction par tâche planifiée, statistiques gardées dans job_run --------------


def _heartbeat(ctx: JobContext, pinger: HealthcheckPinger | None) -> dict[str, bool]:
    log.info("worker vivant (créneau %s)", ctx.slot_start.isoformat())
    return {"pinged": pinger.ping() if pinger else False}


def _alerts(ctx: JobContext, senders: dict[Channel, Sender], base_url: str) -> dict[str, int]:
    return dispatch(ctx.engine, senders, base_url=base_url, now=ctx.now())


def _technical_checks(ctx: JobContext, settings: Settings) -> dict[str, int]:
    now, data_dir = ctx.now(), settings.database_path.parent
    checks.check_sync_freshness(ctx.engine, now=now)
    checks.check_consents(ctx.engine, now=now)
    checks.check_disk(ctx.engine, usage=checks.disk_usage(data_dir), now=now)
    if settings.backup_expected:
        checks.check_backup_freshness(ctx.engine, marker=data_dir / BACKUP_MARKER, now=now)
    return {}


def _rental(ctx: JobContext) -> dict[str, int]:
    stats = run_rental(ctx.engine, now=ctx.now())
    return {
        "echeances_creees": stats.dues_created,
        "rapprochees": stats.confirmed,
        "proposees": stats.proposed,
        "alertes": stats.alerts,
    }


def _deadlines_match(ctx: JobContext) -> dict[str, int]:
    return {"reglees": match_payments(ctx.engine, now=ctx.now())}


def _deadlines_remind(ctx: JobContext) -> dict[str, int]:
    now = ctx.now()
    return {
        "echeances": remind(ctx.engine, now=now),
        "baux": remind_leases(ctx.engine, now=now),
        "depots": remind_deposits(ctx.engine, now=now),
    }


def _budgets(ctx: JobContext) -> dict[str, int]:
    return {"signales": check_budgets(ctx.engine, now=ctx.now())}


def _recurring(ctx: JobContext) -> dict[str, int]:
    return {"signales": check_recurring(ctx.engine, now=ctx.now())}


def _categorize_ai(ctx: JobContext, secrets: Secrets | None) -> dict[str, int]:
    """ANA-02 : désactivée par défaut ; une panne du fournisseur ne marque rien."""
    if secrets is None:
        return {"envoyees": 0}
    try:
        result = categorize_pending(ctx.engine, secrets, now=ctx.now())
    except AiCategorizationError as exc:
        log.info("catégorisation IA non lancée : %s", exc)
        return {"envoyees": 0}
    return {
        "envoyees": result.sent,
        "categorisees": result.categorized,
        "a_valider": result.to_review,
    }


def _digest(ctx: JobContext) -> dict[str, int]:
    return {"bilan": int(weekly_digest(ctx.engine, now=ctx.now()))}


def _low_balances(ctx: JobContext) -> dict[str, int]:
    return {"comptes": check_low_balances(ctx.engine, now=ctx.now())}


def _maintenance(ctx: JobContext) -> dict[str, int]:
    return maintain(ctx.engine, now=ctx.now())


def _integrity(ctx: JobContext) -> dict[str, int]:
    checks.check_database_integrity(ctx.engine, now=ctx.now())
    return {}


def _sync_one(ctx: JobContext, connector: BankConnector, account_id: int) -> dict[str, int]:
    result = sync_account(
        ctx.engine, connector, account_id, unattended=True, now=ctx.now(), job_run_id=ctx.run_id
    )
    if isinstance(result, SyncDeferred):
        return {"nouvelles": 0, "reportee": 1}
    return {"nouvelles": result.inserted if result else 0}


def sync_bank_accounts(ctx: JobContext, connector: BankConnector | None) -> dict[str, int]:
    """Chaque compte relié à ses propres créneaux (1 à 4 par jour), une fois par créneau."""
    if connector is None:
        return {"comptes": 0}
    outcomes: dict[str, int] = {}
    for account_id, per_day in linked_accounts(ctx.engine):
        # Décalage stable par compte : les banques ne sont pas interrogées toutes à HH:30:00.
        schedule = DailyAt(*SCHEDULES[per_day], jitter=BANK_JITTER, seed=f"bank.sync.{account_id}")
        # Deux tentatives au plus par créneau : chaque reprise est une requête comptée dans le
        # budget DSP2 (inv. 10).
        outcome = run_once(
            ctx.engine,
            f"bank.sync.{account_id}",
            schedule.latest(ctx.now()),
            partial(_sync_one, connector=connector, account_id=account_id),
            now=ctx.now,
            max_attempts=BANK_ATTEMPTS,
        )
        outcomes[outcome.outcome.value] = outcomes.get(outcome.outcome.value, 0) + 1
    return outcomes


def build_jobs(
    settings: Settings,
    senders: dict[Channel, Sender],
    pinger: HealthcheckPinger | None = None,
    bank: EnableBankingClient | None = None,
    *,
    secrets: Secrets | None = None,
) -> list[Job]:
    connector = EnableBankingConnector(bank) if bank is not None else None
    return [
        Job("worker.heartbeat", Every(timedelta(minutes=15)), partial(_heartbeat, pinger=pinger)),
        Job(
            "alerts.dispatch",
            Every(timedelta(minutes=1)),
            partial(_alerts, senders=senders, base_url=settings.public_base_url),
        ),
        Job(
            "ops.checks",
            Every(timedelta(minutes=15)),
            partial(_technical_checks, settings=settings),
        ),
        Job("ops.integrity", DailyAt(time(3, 30)), _integrity),
        Job("ops.maintenance", DailyAt(time(4, 20)), _maintenance),
        Job(
            "bank.sync",
            Every(timedelta(minutes=5)),
            partial(sync_bank_accounts, connector=connector),
        ),
        # Après les synchros : échéances, rapprochement, états et alertes de loyer (LOC-05 :
        # alerte dans l'heure suivant la synchro).
        Job("rental.run", Every(timedelta(minutes=15)), _rental),
        # Échéances (FIS-04) : paiement reconnu après chaque synchro ; rappels en journée.
        Job("deadlines.match", Every(timedelta(minutes=15)), _deadlines_match),
        Job("deadlines.remind", DailyAt(time(9, 0)), _deadlines_remind),
        Job("budgets.check", Every(timedelta(minutes=15)), _budgets),
        Job("categorize.ai", Every(timedelta(hours=1)), partial(_categorize_ai, secrets=secrets)),
        # Bilan hebdomadaire (ALR-05) : le lundi, après les heures silencieuses.
        Job("alerts.digest", DailyAt(time(8, 10)), _digest),
        # Solde bas prévu (ALR-07), après les synchros du matin et du soir.
        Job("forecast.check", DailyAt(time(9, 30), time(18, 30)), _low_balances),
        # Abonnements (hausse, manque) et anomalies (ANA-07, ANA-08) : deux fois par jour.
        Job("recurring.check", DailyAt(time(9, 0), time(18, 0)), _recurring),
    ]


def build_scheduler(
    engine: Engine,
    settings: Settings,
    senders: dict[Channel, Sender],
    pinger: HealthcheckPinger | None = None,
    bank: EnableBankingClient | None = None,
    *,
    secrets: Secrets | None = None,
) -> Scheduler:
    return Scheduler(engine, build_jobs(settings, senders, pinger, bank, secrets=secrets))


def configure_logging() -> None:
    """Journal du worker. httpx écrit chaque adresse appelée en INFO : celle d'un ping
    Healthchecks est un secret, celles de la banque portent des identifiants de compte (inv. 5)."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main() -> None:
    configure_logging()
    check_startup(DEFAULT_SECRETS_DIR)  # inv. 1 : refus si une clé crypto n'est pas vérifiée
    settings = Settings()
    secrets = load_secrets(DEFAULT_SECRETS_DIR)
    configure_keyring(Keyring.from_secrets(secrets))
    configure_profiles(settings.bank_budgets)
    senders = build_senders(settings, secrets)
    log.info("canaux de notification : %s", ", ".join(sorted(senders)) or "aucun")
    pinger = (
        HealthcheckPinger(make_client(), secrets.healthchecks_worker_url.get_secret_value())
        if secrets.healthchecks_worker_url
        else None
    )
    engine = make_engine(settings.database_path)
    apply_restore_marker(engine, settings.database_path.parent, now=utcnow())
    bank = enable_banking_client(secrets)
    log.info("synchro bancaire : %s", "active" if bank else "non configurée")
    scheduler = build_scheduler(engine, settings, senders, pinger, bank, secrets=secrets)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    scheduler.run_forever(stop)


if __name__ == "__main__":
    main()
