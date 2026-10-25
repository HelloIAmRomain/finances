"""Points d'entrée : commandes d'administration (création du compte, statistiques) et
démarrage du worker, sans réseau ni vrais secrets."""

import base64
import getpass
import json
import os
import signal
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select

import app.cli
import app.ops.worker
from app.config import Secrets
from app.db import Base, make_engine
from app.models import AppUser
from app.ops.healthchecks import HealthcheckPinger
from app.ops.scheduler import Scheduler

PASSWORD = "phrase-de-passe-des-entrees"  # noqa: S105 (factice)


def _secrets() -> Secrets:
    return Secrets.model_construct(
        app_secret_key=SecretStr("x" * 48),
        data_encryption_key=SecretStr(base64.b64encode(os.urandom(32)).decode()),
        blind_index_key=SecretStr(base64.b64encode(os.urandom(32)).decode()),
        healthchecks_worker_url=SecretStr(
            "https://hc-ping.com/0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b"
        ),
    )


@pytest.fixture
def database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "entrees.db"
    Base.metadata.create_all(make_engine(db))
    monkeypatch.setenv("FINANCES_DATABASE_PATH", str(db))
    secrets = _secrets()
    monkeypatch.setattr(app.cli, "load_secrets", lambda _dir: secrets)
    monkeypatch.setattr(app.ops.worker, "load_secrets", lambda _dir: secrets)
    return db


def test_create_admin_shows_totp_once(
    database: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    answers = iter([PASSWORD, PASSWORD])
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": next(answers))
    app.cli.main(["create-admin", "--identifiant", "moi"])
    out = capsys.readouterr().out
    assert "Compte créé" in out and "otpauth://totp/" in out and "plus jamais affiché" in out
    with make_engine(database).connect() as connection:
        assert connection.scalar(select(AppUser.username)) == "moi"


def test_create_admin_refuses_mismatch(database: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    answers = iter([PASSWORD, "autre"])
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": next(answers))
    with pytest.raises(SystemExit, match="diffèrent"):
        app.cli.main(["create-admin"])


@pytest.mark.parametrize("command", ["verifier-conversion", "echantillons-anonymises"])
def test_statistics_commands_print_json(
    database: Path, command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    app.cli.main([command])
    json.loads(capsys.readouterr().out)  # sortie JSON valide, même base vide


def test_worker_main_starts_and_stops(database: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[threading.Event] = []
    monkeypatch.setattr(app.ops.worker, "check_startup", lambda _dir: None)
    monkeypatch.setattr(app.ops.worker, "enable_banking_client", lambda secrets: None)
    monkeypatch.setattr(Scheduler, "run_forever", lambda self, stop: started.append(stop))
    monkeypatch.setattr(signal, "signal", lambda *args: None)
    app.ops.worker.main()
    assert len(started) == 1 and not started[0].is_set()


def test_healthcheck_ping_survives_network_errors() -> None:
    def fail(request: httpx.Request) -> Any:
        raise httpx.ConnectError("hors ligne")

    pinger = HealthcheckPinger(
        httpx.Client(transport=httpx.MockTransport(fail)), "https://hc-ping.com/x/"
    )
    assert pinger.ping() is False
