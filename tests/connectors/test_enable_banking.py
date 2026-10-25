"""Client Enable Banking et conversion des réponses (COL-01, inv. 1, inv. 10)."""

from datetime import UTC, date, datetime

import jwt
import pytest
from sqlalchemy import Engine, select

from app.connectors.enable_banking.client import ConsentExpiredError, EnableBankingClient
from app.connectors.enable_banking.mapping import to_account, to_balances, to_transactions
from app.connectors.requester import BankRequester, Budget
from app.models import ProviderCall
from app.models.ledger import AccountType, BalanceType
from app.net.client import make_client
from tests.connectors.eb_fake import (
    APPLICATION_ID,
    FakeEnableBanking,
    credentials,
    load,
    public_key_pem,
)
from tests.factories import fake_iban

NOW = datetime(2026, 9, 27, 6, 30, tzinfo=UTC)
UID = "00000000-0000-4000-8000-0000000000c1"


def _client(fake: FakeEnableBanking) -> EnableBankingClient:
    requester = BankRequester(
        make_client(transport=fake.transport()), sleep=lambda _: None, now=lambda: NOW
    )
    return EnableBankingClient(requester, credentials(), now=lambda: NOW)


def test_requests_carry_a_valid_short_lived_jwt() -> None:
    fake = FakeEnableBanking()
    _client(fake).aspsps("FR")
    token = fake.requests[0].headers["Authorization"].removeprefix("Bearer ")
    assert jwt.get_unverified_header(token)["kid"] == APPLICATION_ID
    claims = jwt.decode(
        token,
        public_key_pem(),
        algorithms=["RS256"],
        audience="api.enablebanking.com",
        options={"verify_exp": False},
    )
    assert claims["iss"] == "enablebanking.com" and claims["exp"] - claims["iat"] == 3600
    assert "private_key_pem=**masqué**" in repr(credentials())


def test_consent_flow_calls() -> None:
    fake = FakeEnableBanking()
    client = _client(fake)
    url = client.start_authorization(
        aspsp_name="Banque Exemple",
        country="FR",
        valid_until=NOW,
        state="etat",
        redirect_url="https://x/retour",
    )
    assert url.startswith("https://tilisy.enablebanking.com/")
    session = client.create_session("code-factice")
    client.delete_session(session["session_id"])
    assert [(r.method, r.url.path) for r in fake.requests] == [
        ("POST", "/auth"),
        ("POST", "/sessions"),
        ("DELETE", "/sessions/00000000-0000-4000-8000-0000000000aa"),
    ]


def test_transactions_follow_pagination_and_consume_budget(engine: Engine) -> None:
    fake = FakeEnableBanking()
    budget = Budget(engine=engine, account_id=1, unattended=True)
    pages = list(_client(fake).transactions(UID, date(2026, 9, 1), budget))
    assert len(pages) == 2
    assert fake.requests[1].url.params["continuation_key"] == "page-2"
    with engine.connect() as connection:
        calls = connection.execute(
            select(ProviderCall.path_template, ProviderCall.http_status)
        ).all()
    assert calls == [("transactions", 200), ("transactions", 200)]  # chaque page compte (inv. 10)


def test_expired_consent_is_reported() -> None:
    fake = FakeEnableBanking(status={f"/accounts/{UID}/balances": 401})
    with pytest.raises(ConsentExpiredError):
        _client(fake).balances(UID, None)


def test_account_mapping() -> None:
    accounts = [to_account(a) for a in load("session.json")["accounts"]]
    assert [(a.name, a.type, a.iban) for a in accounts] == [
        ("Compte courant", AccountType.CHECKING, fake_iban(201)),
        ("Livret", AccountType.SAVINGS, fake_iban(202)),
    ]


def test_balance_mapping() -> None:
    balances = to_balances(load("balances.json"), fallback_day=date(2026, 9, 27))
    assert [(b.type, b.amount, b.as_of) for b in balances] == [
        (BalanceType.BOOKED, 123456, date(2026, 9, 26)),
        (BalanceType.AVAILABLE, 120000, date(2026, 9, 27)),
    ]


def test_transaction_mapping() -> None:
    first = to_transactions(load("transactions_page1.json"))
    second = to_transactions(load("transactions_page2.json"))
    assert [(t.external_id, t.amount) for t in first] == [
        ("E-0001", -420),
        ("E-0002", 85000),
    ]  # PDNG ignorée
    rent = first[1]
    assert (rent.counterparty_name, rent.counterparty_iban) == ("ALICE EXEMPLE", fake_iban(301))
    assert rent.label == "LOYER SEPTEMBRE" and rent.bank_tx_code == "RCDT"
    assert (
        first[0].counterparty_name == "BOULANGERIE EXEMPLE" and first[0].bank_tx_code == "PMNT/CCRD"
    )
    assert [(t.external_id, t.amount, t.label) for t in second] == [
        ("T-9", -3590, "PRLV SEPA ASSURANCE ECH/100926"),
        (None, -110235, "Opération"),
    ]
