import logging
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, func, insert, select, update

from app.alerts.dispatch import build_senders
from app.config import Secrets, Settings
from app.connectors.enable_banking.client import EnableBankingClient
from app.connectors.requester import BankRequester
from app.models import Account, Connection, JobRun, ProviderCall
from app.models.alerts import Channel
from app.models.ledger import ConnectionStatus, Provider
from app.net.client import make_client
from app.ops.healthchecks import HealthcheckPinger
from app.ops.restore_marker import MARKER, apply_restore_marker
from app.ops.scheduler import Scheduler
from app.ops.worker import build_jobs, build_scheduler, configure_logging
from tests.connectors.eb_fake import FakeEnableBanking, credentials

KEY = "a" * 44


def _secrets(**extra: Any) -> Secrets:
    return Secrets.model_construct(
        app_secret_key=SecretStr("x" * 40),
        data_encryption_key=SecretStr(KEY),
        blind_index_key=SecretStr(KEY),
        **extra,
    )


def test_job_names_are_unique() -> None:
    names = [job.name for job in build_jobs(Settings(), {})]
    assert len(names) == len(set(names))


def test_worker_jobs_run_against_database(engine: Engine, tmp_path: Path) -> None:
    now = datetime(2026, 9, 27, 12, 7, tzinfo=UTC)
    settings = Settings(database_path=tmp_path / "finances.db")
    Scheduler(engine, build_jobs(settings, {}), now=lambda: now).tick()
    with engine.connect() as connection:
        rows = connection.execute(select(JobRun.job_name, JobRun.status)).all()
    assert {(name, str(status)) for name, status in rows} == {
        ("worker.heartbeat", "success"),
        ("alerts.dispatch", "success"),
        ("ops.checks", "success"),
        ("ops.integrity", "success"),
        ("ops.maintenance", "success"),
        ("bank.sync", "success"),
        ("rental.run", "success"),
        ("deadlines.match", "success"),
        ("deadlines.remind", "success"),
        ("budgets.check", "success"),
        ("recurring.check", "success"),
        ("categorize.ai", "success"),
        ("alerts.digest", "success"),
        ("forecast.check", "success"),
    }


def test_heartbeat_pings_healthchecks(engine: Engine, tmp_path: Path) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200)

    url = "https://hc-ping.com/0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b"
    pinger = HealthcheckPinger(make_client(transport=httpx.MockTransport(handler)), url)
    now = datetime(2026, 9, 27, 12, 7, tzinfo=UTC)
    settings = Settings(database_path=tmp_path / "finances.db")
    Scheduler(engine, build_jobs(settings, {}, pinger), now=lambda: now).tick()
    assert seen == [url]


def test_worker_stops_cleanly(engine: Engine) -> None:
    stop = threading.Event()
    stop.set()
    scheduler = build_scheduler(engine, Settings(), {})
    scheduler.tick_interval = timedelta(milliseconds=10)
    assert scheduler.run_forever(stop) == 0


def test_channels_are_optional() -> None:
    assert build_senders(Settings(), _secrets()) == {}


@pytest.mark.parametrize("complete", [True, False])
def test_email_channel_needs_every_smtp_setting(complete: bool) -> None:
    smtp = {
        "smtp_host": "smtp.example.org",
        "smtp_username": "alertes@example.org",
        "smtp_password": SecretStr("factice"),
        "alert_email_from": "alertes@example.org",
        "alert_email_to": "admin@example.org" if complete else None,
        "smtp_port": 465,
        "ntfy_token": SecretStr("jeton"),
    }
    senders = build_senders(Settings(), _secrets(**smtp))
    assert (Channel.EMAIL in senders) is complete
    assert Channel.NTFY in senders


def test_bank_sync_runs_each_account_once_per_slot(engine: Engine, tmp_path: Path) -> None:
    now = datetime(2026, 9, 27, 4, 35, tzinfo=UTC)  # juste après le créneau de 06:30 (Paris)
    with engine.begin() as connection:
        connection.execute(
            insert(Connection).values(
                id=1,
                provider=Provider.ENABLE_BANKING,
                institution_name="Banque Exemple",
                country="FR",
                status=ConnectionStatus.ACTIVE,
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            update(Account)
            .where(Account.id == 1)
            .values(
                connection_id=1,
                provider_account_uid="00000000-0000-4000-8000-0000000000c1",
                syncs_per_day=2,
            )
        )
    fake = FakeEnableBanking()
    requester = BankRequester(
        make_client(transport=fake.transport()), sleep=lambda _: None, now=lambda: now
    )
    bank = EnableBankingClient(requester, credentials(), now=lambda: now)
    settings = Settings(database_path=tmp_path / "finances.db")
    scheduler = Scheduler(engine, build_jobs(settings, {}, None, bank), now=lambda: now)
    scheduler.tick()
    first = len(fake.requests)
    later = now + timedelta(minutes=20)
    Scheduler(engine, build_jobs(settings, {}, None, bank), now=lambda: later).tick()
    assert first == 3 and len(fake.requests) == first  # soldes + 2 pages, pas de second passage
    with engine.connect() as connection:
        names = set(connection.scalars(select(JobRun.job_name)))
    assert "bank.sync.1" in names


def test_restore_marker_reserves_budget(engine: Engine, tmp_path: Path) -> None:
    now = datetime(2026, 9, 27, 4, 35, tzinfo=UTC)
    with engine.begin() as connection:
        connection.execute(
            insert(Connection).values(
                id=1,
                provider=Provider.ENABLE_BANKING,
                institution_name="Banque Exemple",
                country="FR",
                status=ConnectionStatus.ACTIVE,
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            update(Account)
            .where(Account.id == 1)
            .values(connection_id=1, provider_account_uid="uid")
        )
    assert apply_restore_marker(engine, tmp_path, now=now) is False
    (tmp_path / MARKER).write_text(now.isoformat())
    assert apply_restore_marker(engine, tmp_path, now=now) is True
    assert not (tmp_path / MARKER).exists()
    with engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(ProviderCall)).scalar_one() == 2


def test_bank_sync_tries_each_slot_twice_at_most() -> None:
    """Chaque reprise d'une synchro est une requête comptée (inv. 10) : deux tentatives par
    créneau au plus, au lieu de trois pour les autres tâches."""
    from app.ops import worker  # noqa: PLC0415

    assert worker.BANK_ATTEMPTS == 2


def test_worker_logs_never_carry_called_addresses(caplog: pytest.LogCaptureFixture) -> None:
    """Inv. 5 : l'adresse d'un ping Healthchecks est un secret ; httpx la journalise en INFO."""
    configure_logging()
    with caplog.at_level(logging.INFO):
        logging.getLogger("httpx").info("HTTP Request: GET https://hc-ping.com/secret")
    assert "hc-ping" not in caplog.text
    assert logging.getLogger("httpcore").getEffectiveLevel() == logging.WARNING
