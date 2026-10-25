"""Requêtes bancaires : chaque tentative passe par la liste blanche et le budget DSP2."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import Engine, select, text

import app.models  # noqa: F401
from app.connectors.bank_profiles import DEFAULT_LIMIT
from app.connectors.budget import BudgetExceededError, acquire
from app.connectors.requester import (
    FAILURE_THRESHOLD,
    BankRateLimitedError,
    BankRequester,
    BankUnavailableError,
    Budget,
    CircuitBreaker,
    CircuitOpenError,
)
from app.db import Base, make_engine
from app.models import ProviderCall
from app.net.client import ForbiddenRequestError, make_client

T0 = datetime(2026, 9, 27, 6, 0, tzinfo=UTC)
URL = "https://api.enablebanking.com/accounts/uid-1/transactions"


class Clock:
    def __init__(self) -> None:
        self.current = T0

    def __call__(self) -> datetime:
        return self.current


class Scripted:
    """Transport factice : renvoie une suite de réponses (ou d'exceptions)."""

    def __init__(self, *steps: int | Exception) -> None:
        self.steps = list(steps)
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return httpx.Response(step, json={})


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    engine = make_engine(tmp_path / "req.db")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO account (id, name, type, currency, include_in_net_worth,"
                " syncs_per_day) VALUES (1, 'Courant', 'checking', 'EUR', 1, 4)"
            )
        )
    return engine


def _requester(transport: Scripted, clock: Clock, sleeps: list[float]) -> BankRequester:
    return BankRequester(
        make_client(transport=httpx.MockTransport(transport)),
        sleep=sleeps.append,
        now=clock,
        breaker=CircuitBreaker(now=clock),
    )


def _calls(engine: Engine) -> list[int | None]:
    with engine.connect() as connection:
        return list(connection.scalars(select(ProviderCall.http_status).order_by(ProviderCall.id)))


def _budget(engine: Engine) -> Budget:
    return Budget(engine=engine, account_id=1, unattended=True)


def test_success_is_counted_with_status(engine: Engine) -> None:
    sleeps: list[float] = []
    requester = _requester(Scripted(200), Clock(), sleeps)
    response = requester.send("GET", URL, endpoint="transactions", budget=_budget(engine))
    assert response.status_code == 200
    assert _calls(engine) == [200]
    assert sleeps == []


def test_server_error_is_not_retried_at_once(engine: Engine) -> None:
    """La requête a atteint la banque : une reprise immédiate brûlerait le budget DSP2."""
    sleeps: list[float] = []
    transport = Scripted(503, 200)
    with pytest.raises(BankUnavailableError, match="503"):
        _requester(transport, Clock(), sleeps).send(
            "GET", URL, endpoint="transactions", budget=_budget(engine)
        )
    assert transport.calls == 1 and _calls(engine) == [503] and sleeps == []


def test_read_timeout_is_not_retried(engine: Engine) -> None:
    """Cas réel du 1er octobre 2026 : soldes Boursorama sans réponse en 20 s, trois fois."""
    transport = Scripted(httpx.ReadTimeout("lent"), 200)
    with pytest.raises(BankUnavailableError, match="ReadTimeout"):
        _requester(transport, Clock(), []).send(
            "GET", URL, endpoint="balances", budget=_budget(engine)
        )
    assert transport.calls == 1 and _calls(engine) == [None]


def test_connection_failure_is_retried(engine: Engine) -> None:
    sleeps: list[float] = []
    transport = Scripted(httpx.ConnectError("injoignable"), httpx.ConnectTimeout("lent"), 200)
    response = _requester(transport, Clock(), sleeps).send(
        "GET", URL, endpoint="transactions", budget=_budget(engine)
    )
    assert response.status_code == 200 and transport.calls == 3 and len(sleeps) == 2


def test_network_errors_exhaust_attempts(engine: Engine) -> None:
    transport = Scripted(*(httpx.ConnectError("injoignable") for _ in range(3)))
    requester = _requester(transport, Clock(), [])
    with pytest.raises(BankUnavailableError):
        requester.send("GET", URL, endpoint="transactions", budget=_budget(engine))
    assert transport.calls == 3
    assert _calls(engine) == [None, None, None]


def test_retries_stop_when_budget_is_exhausted(engine: Engine) -> None:
    clock = Clock()
    for _ in range(DEFAULT_LIMIT - 1):
        acquire(
            engine, account_id=1, endpoint="transactions", unattended=True, method="GET", now=T0
        )
    transport = Scripted(httpx.ConnectError("injoignable"), 200)
    with pytest.raises(BudgetExceededError):
        _requester(transport, clock, []).send(
            "GET", URL, endpoint="transactions", budget=_budget(engine)
        )
    assert transport.calls == 1  # la seconde tentative n'a jamais été envoyée


def test_rate_limit_is_not_retried_nor_a_failure(engine: Engine) -> None:
    """Q14 : un 429 n'est pas retenté et n'ouvre pas le disjoncteur (ce n'est pas une panne)."""
    transport = Scripted(*(429 for _ in range(FAILURE_THRESHOLD + 1)))
    requester = _requester(transport, Clock(), [])
    for _ in range(FAILURE_THRESHOLD + 1):
        with pytest.raises(BankRateLimitedError):
            requester.send("GET", URL, endpoint="transactions", budget=None)
    assert transport.calls == FAILURE_THRESHOLD + 1
    assert requester.breaker.failures == 0 and requester.breaker.opened_at is None
    transport = Scripted(429, 200)
    with pytest.raises(BankRateLimitedError):
        _requester(transport, Clock(), []).send(
            "GET", URL, endpoint="transactions", budget=_budget(engine)
        )
    assert transport.calls == 1
    assert _calls(engine) == [429]


def test_client_errors_are_returned_to_caller(engine: Engine) -> None:
    response = _requester(Scripted(401), Clock(), []).send(
        "GET", URL, endpoint="transactions", budget=_budget(engine)
    )
    assert response.status_code == 401


def test_forbidden_request_consumes_no_budget(engine: Engine) -> None:
    transport = Scripted(200)
    with pytest.raises(ForbiddenRequestError):
        _requester(transport, Clock(), []).send(
            "POST",
            "https://api.enablebanking.com/payments",
            endpoint="payments",
            budget=_budget(engine),
        )
    assert transport.calls == 0
    assert _calls(engine) == []


def test_request_without_budget_is_not_counted(engine: Engine) -> None:
    requester = _requester(Scripted(200), Clock(), [])
    requester.send(
        "GET",
        "https://hc-ping.com/0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b",
        endpoint="healthchecks",
        budget=None,
    )
    assert _calls(engine) == []


def test_circuit_opens_after_consecutive_failures_then_recovers(engine: Engine) -> None:
    clock = Clock()
    transport = Scripted(*([503] * 5), 200)
    requester = BankRequester(
        make_client(transport=httpx.MockTransport(transport)),
        sleep=lambda _: None,
        now=clock,
        max_attempts=1,
        breaker=CircuitBreaker(threshold=5, cooldown=timedelta(minutes=30), now=clock),
    )
    for _ in range(5):
        with pytest.raises(BankUnavailableError):
            requester.send("GET", URL, endpoint="balances", budget=None)
    with pytest.raises(CircuitOpenError):
        requester.send("GET", URL, endpoint="balances", budget=None)
    assert transport.calls == 5
    clock.current += timedelta(minutes=31)
    assert requester.send("GET", URL, endpoint="balances", budget=None).status_code == 200


def _connection(engine: Engine, connection_id: int, account_id: int) -> Budget:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT OR IGNORE INTO connection (id, provider, status, institution_name, country,"
                " created_at, updated_at) VALUES (:id, 'enable_banking', 'active', :bank, 'FR',"
                " '2026-09-27 06:00:00', '2026-09-27 06:00:00')"
            ),
            {"id": connection_id, "bank": f"Banque {connection_id}"},
        )
        connection.execute(
            text(
                "INSERT OR IGNORE INTO account (id, name, type, currency, include_in_net_worth,"
                " syncs_per_day, connection_id) VALUES (:id, 'C', 'checking', 'EUR', 1, 4, :c)"
            ),
            {"id": account_id, "c": connection_id},
        )
    return Budget(
        engine=engine, account_id=account_id, unattended=False, connection_id=connection_id
    )


def test_circuit_is_per_bank_connection(engine: Engine) -> None:
    """Cas réel du 1er octobre 2026 : une banque lente ne suspend plus les autres."""
    clock = Clock()
    other = _connection(engine, 20, 3)
    slow = [_connection(engine, 10, account) for account in range(10, 10 + FAILURE_THRESHOLD)]
    transport = Scripted(*([503] * FAILURE_THRESHOLD), 200, 200)
    requester = BankRequester(
        make_client(transport=httpx.MockTransport(transport)),
        sleep=lambda _: None,
        now=clock,
        breaker=CircuitBreaker(now=clock),
    )
    for budget in slow:  # cinq comptes de la même banque, sans réponse
        with pytest.raises(BankUnavailableError):
            requester.send("GET", URL, endpoint="balances", budget=budget)
    with pytest.raises(CircuitOpenError):
        requester.send("GET", URL, endpoint="balances", budget=slow[0])
    assert requester.send("GET", URL, endpoint="balances", budget=other).status_code == 200
    assert requester.send("GET", URL, endpoint="balances", budget=None).status_code == 200
