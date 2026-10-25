"""Client HTTP unique à liste blanche (inv. 1) : rien d'autre ne sort."""

import itertools

import httpx
import pytest

from app.net.allowlist import (
    ALLOWLIST,
    ANTHROPIC,
    ENABLE_BANKING,
    HEALTHCHECKS,
    NTFY_INTERNAL,
    OPENAI,
    OVH_AI,
)
from app.net.client import ForbiddenRequestError, make_client

PAYMENT_PATHS = [
    "/payments",
    "/payments/abc",
    "/payments/abc/transactions",
    "/payments/abc/status",
    "/bulk-payments",
    "/periodic-payments",
    "/v1/payments/sepa-credit-transfers",
    "/funds-confirmations",
    "/accounts/abc/payments",
    "/accounts/abc/transfers",
    "/orders",
    "/withdrawals",
]
METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]
CHECK_UUID = "0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b"  # factice
HOSTS = [
    ("https", ENABLE_BANKING),
    ("https", HEALTHCHECKS),
    ("http", NTFY_INTERNAL),
    ("https", "api.coinbase.com"),
    ("https", ANTHROPIC),
    ("https", OPENAI),
    ("https", OVH_AI),
]


class Recorder:
    def __init__(self, status: int = 200, headers: dict[str, str] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.status = status
        self.headers = headers or {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, headers=self.headers, json={})


def _client(recorder: Recorder) -> httpx.Client:
    return make_client(transport=httpx.MockTransport(recorder))


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("GET", f"https://{ENABLE_BANKING}/aspsps?country=FR"),
        ("GET", f"https://{ENABLE_BANKING}:443/aspsps"),  # port par défaut explicite
        ("POST", f"https://{ENABLE_BANKING}/auth"),
        ("POST", f"https://{ENABLE_BANKING}/sessions"),
        ("GET", f"https://{ENABLE_BANKING}/sessions/0b1c-2d3e"),
        ("DELETE", f"https://{ENABLE_BANKING}/sessions/0b1c-2d3e"),
        ("GET", f"https://{ENABLE_BANKING}/accounts/uid-1/balances"),
        ("GET", f"https://{ENABLE_BANKING}/accounts/uid-1/transactions?date_from=2026-09-01"),
        ("GET", f"https://{HEALTHCHECKS}/{CHECK_UUID}/fail"),
        ("POST", f"https://{ANTHROPIC}/v1/messages"),
        ("POST", f"https://{OPENAI}/v1/chat/completions"),
        ("POST", f"https://{OVH_AI}/v1/chat/completions"),
        ("GET", f"https://{HEALTHCHECKS}/{CHECK_UUID}"),
        ("POST", f"http://{NTFY_INTERNAL}:8080/finances-alertes"),
    ],
)
def test_allowed_requests_reach_transport(method: str, url: str) -> None:
    recorder = Recorder()
    with _client(recorder) as client:
        assert client.request(method, url).status_code == 200
    assert len(recorder.requests) == 1


@pytest.mark.parametrize(
    ("scheme_host", "method", "path"),
    list(itertools.product(HOSTS, METHODS, PAYMENT_PATHS)),
)
def test_payment_endpoints_are_never_callable(
    scheme_host: tuple[str, str], method: str, path: str
) -> None:
    scheme, host = scheme_host
    recorder = Recorder()
    with _client(recorder) as client, pytest.raises(ForbiddenRequestError):
        client.request(method, f"{scheme}://{host}{path}")
    assert recorder.requests == []  # rien n'a quitté le processus


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("GET", f"http://{ENABLE_BANKING}/aspsps"),  # HTTP en clair
        ("GET", "https://evil.example/aspsps"),  # hôte inconnu
        ("GET", f"https://{ENABLE_BANKING}.evil.example/aspsps"),
        ("DELETE", f"https://{ENABLE_BANKING}/accounts/uid-1"),
        ("PUT", f"https://{ENABLE_BANKING}/sessions/abc"),
        ("GET", f"https://{ENABLE_BANKING}/accounts/a/b/transactions"),  # segment en trop
        ("GET", f"https://{ENABLE_BANKING}/accounts/x/transactions/../../payments"),
        ("GET", f"https://{ENABLE_BANKING}/accounts/a%2Fb/transactions"),  # slash encodé
        ("GET", f"https://{ENABLE_BANKING}/accounts//transactions"),  # segment vide
        ("POST", f"https://{HEALTHCHECKS}/{CHECK_UUID}"),
        ("GET", f"https://{HEALTHCHECKS}/pas-un-uuid"),
        ("POST", f"http://{NTFY_INTERNAL}:8080/autre-sujet"),
        ("GET", f"https://{ENABLE_BANKING}:8443/aspsps"),  # autre port
        ("POST", f"https://{ANTHROPIC}:444/v1/messages"),
        ("POST", f"http://{NTFY_INTERNAL}/finances-alertes"),  # port 80 au lieu de 8080
        ("POST", f"http://{NTFY_INTERNAL}:2375/finances-alertes"),
    ],
)
def test_other_requests_are_refused(method: str, url: str) -> None:
    recorder = Recorder()
    with _client(recorder) as client, pytest.raises(ForbiddenRequestError):
        client.request(method, url)
    assert recorder.requests == []


def test_redirects_are_not_followed() -> None:
    recorder = Recorder(status=302, headers={"Location": f"https://{ENABLE_BANKING}/payments"})
    with _client(recorder) as client:
        response = client.get(f"https://{ENABLE_BANKING}/aspsps")
    assert response.status_code == 302
    assert len(recorder.requests) == 1


def test_no_allowlisted_template_looks_like_a_money_movement() -> None:
    forbidden = ("payment", "transfer", "order", "withdraw", "fund", "trade")
    for rule in ALLOWLIST:
        assert not any(word in rule.path.lower() for word in forbidden), rule


def test_enable_banking_writes_are_limited_to_consent_lifecycle() -> None:
    writes = {
        (r.method, r.path) for r in ALLOWLIST if r.host == ENABLE_BANKING and r.method != "GET"
    }
    assert writes == {
        ("POST", "/auth"),
        ("POST", "/sessions"),
        ("DELETE", "/sessions/{session_id}"),
    }
