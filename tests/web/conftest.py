import base64
import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime

import pyotp
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, update

from app.assistant.runs import RunRegistry
from app.auth.users import create_user
from app.config import Secrets
from app.models import AppUser
from app.web.main import create_app
from tests.executors import InlineExecutor

T0 = datetime(2026, 9, 27, 10, 0, tzinfo=UTC)
USERNAME = "admin"
PASSWORD = "phrase-de-passe-de-test"  # noqa: S105 (factice)


class Clock:
    def __init__(self) -> None:
        self.current = T0

    def __call__(self) -> datetime:
        return self.current


@dataclass
class Web:
    app: FastAPI
    client: TestClient
    clock: Clock
    engine: Engine
    totp_secret: str

    def code(self) -> str:
        return pyotp.TOTP(self.totp_secret).at(self.clock.current)

    def login(self, *, password: str = PASSWORD, code: str | None = None, suivant: str = "/"):  # type: ignore[no-untyped-def]
        # Horloge figée : un code TOTP ne sert qu'une fois par pas de 30 s ; on l'oublie pour
        # pouvoir se reconnecter (le refus d'un code rejoué est testé dans test_auth.py).
        with self.engine.begin() as connection:
            connection.execute(update(AppUser).values(last_totp_step=None))
        return self.client.post(
            "/connexion",
            data={
                "identifiant": USERNAME,
                "mot_de_passe": password,
                "code": code if code is not None else self.code(),
                "suivant": suivant,
            },
            follow_redirects=False,
        )


def fake_secrets() -> Secrets:
    return Secrets.model_construct(
        app_secret_key=SecretStr(base64.b64encode(os.urandom(48)).decode()),
        data_encryption_key=SecretStr(base64.b64encode(os.urandom(32)).decode()),
        blind_index_key=SecretStr(base64.b64encode(os.urandom(32)).decode()),
        ntfy_token=None,
        healthchecks_worker_url=None,
    )


@pytest.fixture
def web(engine: Engine) -> Iterator[Web]:
    clock = Clock()
    app = create_app(
        secrets=fake_secrets(),
        engine=engine,
        now=clock,
        runs=RunRegistry(executor=InlineExecutor()),
    )
    secret = create_user(engine, username=USERNAME, password=PASSWORD, now=T0)
    with TestClient(app, base_url="https://testserver") as client:
        yield Web(app=app, client=client, clock=clock, engine=engine, totp_secret=secret)
