"""Faux serveur Enable Banking (réponses synthétiques de tests/cassettes/enable_banking)."""

import json
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.connectors.enable_banking.client import Credentials

CASSETTES = Path(__file__).resolve().parents[1] / "cassettes" / "enable_banking"
APPLICATION_ID = "00000000-0000-4000-8000-00000000a991"


@cache
def _private_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def credentials() -> Credentials:
    pem = _private_key().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    return Credentials(application_id=APPLICATION_ID, private_key_pem=pem.decode())


def public_key_pem() -> str:
    return (
        _private_key()
        .public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )


def load(name: str) -> dict[str, Any]:
    document: dict[str, Any] = json.loads((CASSETTES / name).read_text(encoding="utf-8"))
    return document


@dataclass
class FakeEnableBanking:
    """Sert les réponses enregistrées ; `status` force un code HTTP pour un chemin donné."""

    status: dict[str, int] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)
    fail_second_page: bool = False  # coupure simulée au milieu de la pagination
    session: dict[str, Any] | None = None  # remplace session.json (autres comptes)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path in self.status:
            return httpx.Response(self.status[path], json={"error": "forcé"})
        if self.session is not None and (request.method, path) == ("POST", "/sessions"):
            return httpx.Response(200, json=self.session)
        if self.fail_second_page and request.url.params.get("continuation_key"):
            raise httpx.ConnectError("coupure simulée")
        name = self._fixture(request.method, path, request.url.params.get("continuation_key"))
        if name is None:
            return httpx.Response(404, json={"error": "inconnu"})
        return httpx.Response(200, json=load(name) if name.endswith(".json") else {})

    @staticmethod
    def _fixture(method: str, path: str, continuation: str | None) -> str | None:
        page = "2" if continuation == "page-2" else "1"
        routes = {
            ("GET", "/aspsps"): "aspsps.json",
            ("POST", "/auth"): "auth.json",
            ("POST", "/sessions"): "session.json",
        }
        if (method, path) in routes:
            return routes[(method, path)]
        if method == "DELETE" and path.startswith("/sessions/"):
            return "vide"
        if path.endswith("/balances"):
            return "balances.json"
        if path.endswith("/transactions"):
            return f"transactions_page{page}.json"
        return None

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)
