"""Tests de bout en bout : application en HTTP derrière un relais HTTPS (comme
`tailscale serve`), vrai navigateur (Chromium via Playwright), faux Enable Banking. Ils couvrent
ce que les tests en TestClient ne voient pas : CSP, contenu mixte, cookies SameSite, scripts,
rendu, mobile.

Lancement : `scripts/e2e.sh` (image Playwright), ou `uv run pytest tests/e2e --e2e` après
`uv run playwright install --with-deps chromium`.
"""

import ipaddress
import socket
import ssl
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pyotp
import pytest
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fastapi import FastAPI
from playwright.sync_api import ConsoleMessage, Page, Request, Response
from sqlalchemy import Engine, insert

import app.models  # noqa: F401
from app.auth.users import create_user
from app.categorize.seed import seed_categories
from app.config import Settings
from app.connectors.enable_banking.client import EnableBankingClient
from app.connectors.requester import BankRequester
from app.db import Base, make_engine
from app.ledger.accounts import create_file_account
from app.models import BalanceSnapshot, LedgerTransaction
from app.models.ledger import AccountType, BalanceType
from app.net.client import make_client
from app.timeutil import paris_date
from app.web.main import create_app
from tests.connectors.eb_fake import FakeEnableBanking, credentials
from tests.web.conftest import fake_secrets

USERNAME = "admin"
PASSWORD = "phrase-de-passe-e2e-assez-longue"  # noqa: S105 (factice)
BANK_HOST = "tilisy.enablebanking.com"  # adresse d'autorisation renvoyée par le faux /auth


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _self_signed(directory: Path) -> tuple[Path, Path]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = directory / "cert.pem", directory / "key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


class TlsRelay:
    """Termine le HTTPS et relaie en HTTP simple vers l'application, sans en-tête ajouté : comme
    `tailscale serve` en production. L'application ne sait donc pas qu'elle est vue en HTTPS
    (c'est ce qui rendait la page sans style : URL absolues en http:// bloquées)."""

    def __init__(self, port: int, target: int, cert: Path, key: Path) -> None:
        self.target = target
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(cert, key)
        self.listener = socket.create_server(("127.0.0.1", port))
        self.closed = False
        self.sockets: list[socket.socket] = []
        self.lock = threading.Lock()
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while not self.closed:
            try:
                raw, _ = self.listener.accept()
            except OSError:
                return  # écoute fermée
            try:
                client = self.context.wrap_socket(raw, server_side=True)
            except (OSError, ssl.SSLError):
                raw.close()
                continue
            upstream = socket.create_connection(("127.0.0.1", self.target))
            with self.lock:
                self.sockets += [client, upstream]
            pair = (client, upstream)
            threading.Thread(target=self._pump, args=(client, upstream, pair), daemon=True).start()
            threading.Thread(target=self._pump, args=(upstream, client, pair), daemon=True).start()

    @staticmethod
    def _pump(
        source: socket.socket, sink: socket.socket, pair: tuple[socket.socket, socket.socket]
    ) -> None:
        try:
            while data := source.recv(65536):
                sink.sendall(data)
        except OSError:
            pass
        finally:
            for side in pair:  # une extrémité fermée ferme la paire
                try:
                    side.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                side.close()

    def close(self) -> None:
        self.closed = True
        self.listener.close()
        with self.lock:
            for sock in self.sockets:
                sock.close()


def _demo_data(engine: Engine) -> None:
    """Un compte alimenté par fichiers, douze mois d'opérations et un solde : de quoi dessiner
    les graphiques. Libellés et montants factices (inv. 5)."""
    seed_categories(engine)
    account = create_file_account(
        engine,
        name="Livret démo",  # pas « Compte courant » : les tests visent la ligne liée
        type=AccountType.SAVINGS,
        iban=None,
        include_in_net_worth=True,
        actor_user_id=None,
        now=datetime.now(UTC),
    )
    today = paris_date(datetime.now(UTC))
    rows = []
    for month in range(12):
        day = (today.replace(day=1) - timedelta(days=31 * month)).replace(day=10)
        for n, (label, amount) in enumerate((("SALAIRE DEMO", 250000), ("SUPERMARCHE", -8000))):
            tx_id = month * 10 + n + 1
            rows.append(
                {
                    "id": tx_id,
                    "account_id": account,
                    "dedupe_key": f"ext:e2e:{tx_id}",
                    "fingerprint": "x",
                    "occurrence": 1,
                    "booking_date": day,
                    "amount_cents": amount,
                    "currency": "EUR",
                    "label_raw": label,
                    "label_clean": label,
                    "created_at": datetime.now(UTC),
                }
            )
    with engine.begin() as connection:
        connection.execute(insert(LedgerTransaction), rows)
        connection.execute(
            insert(BalanceSnapshot).values(
                account_id=account,
                as_of_date=today,
                balance_type=BalanceType.BOOKED,
                amount_cents=123456,
                currency="EUR",
                fetched_at=datetime.now(UTC),
            )
        )


@dataclass
class Site:
    base_url: str
    engine: Engine
    app: FastAPI
    fake_bank: FakeEnableBanking
    totp_secret: str

    def code(self) -> str:
        return pyotp.TOTP(self.totp_secret).now()


@pytest.fixture
def site(tmp_path: Path) -> Iterator[Site]:
    port, app_port = _free_port(), _free_port()
    base_url = f"https://localhost:{port}"
    engine = make_engine(tmp_path / "e2e.db")
    Base.metadata.create_all(engine)
    fake = FakeEnableBanking()
    bank = EnableBankingClient(
        BankRequester(make_client(transport=fake.transport()), sleep=lambda _: None),
        credentials(),
    )
    settings = Settings(database_path=tmp_path / "e2e.db", public_base_url=base_url)
    api = create_app(settings=settings, secrets=fake_secrets(), engine=engine, bank=bank)
    secret = create_user(engine, username=USERNAME, password=PASSWORD, now=datetime.now(UTC))
    _demo_data(engine)
    cert, key = _self_signed(tmp_path)
    server = uvicorn.Server(
        uvicorn.Config(
            api,
            host="127.0.0.1",
            port=app_port,
            log_level="warning",
            proxy_headers=True,
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("serveur e2e non démarré")
        time.sleep(0.05)
    relay = TlsRelay(port, app_port, cert, key)
    yield Site(base_url=base_url, engine=engine, app=api, fake_bank=fake, totp_secret=secret)
    relay.close()
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args: dict[str, Any]) -> dict[str, Any]:
    # Certificat autosigné du serveur local : sans cela, le service worker refuse de s'installer
    # (l'option de contexte ignore_https_errors ne s'applique pas à lui).
    return {**browser_type_launch_args, "args": ["--ignore-certificate-errors"]}


@pytest.fixture
def browser_context_args(browser_context_args: dict[str, Any]) -> dict[str, Any]:
    return {**browser_context_args, "ignore_https_errors": True, "locale": "fr-FR"}


@dataclass
class Watch:
    """Tout ce qu'un utilisateur ne verrait pas mais qui casse la page."""

    console_errors: list[str] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    def clean(self) -> bool:
        return not (self.console_errors or self.page_errors or self.failed)

    def report(self) -> str:
        return f"console={self.console_errors} js={self.page_errors} échecs={self.failed}"


def watch(page: Page, base_url: str) -> Watch:
    seen = Watch()

    def on_console(message: ConsoleMessage) -> None:
        if message.type == "error":
            seen.console_errors.append(message.text)

    def on_response(response: Response) -> None:
        if response.url.startswith(base_url) and response.status >= 400:
            seen.failed.append(f"{response.status} {response.url}")

    def on_failed(request: Request) -> None:
        if request.url.startswith(base_url):
            seen.failed.append(f"échec {request.url} ({request.failure})")

    page.on("console", on_console)
    page.on("pageerror", lambda error: seen.page_errors.append(str(error)))
    page.on("response", on_response)
    page.on("requestfailed", on_failed)
    return seen


def login(page: Page, site: Site) -> None:
    page.goto(f"{site.base_url}/connexion")
    page.fill("input[name=identifiant]", USERNAME)
    page.fill("input[name=mot_de_passe]", PASSWORD)
    page.fill("input[name=code]", site.code())
    page.click("button[type=submit]")
    page.wait_for_url(f"{site.base_url}/")
