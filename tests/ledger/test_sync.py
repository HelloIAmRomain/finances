"""Synchronisation bancaire (COL-01, COL-07, NF-RES-01, NF-RES-03, inv. 10)."""

from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from sqlalchemy import Engine, func, insert, select, update

from app.connectors.bank_profiles import DEFAULT_LIMIT
from app.connectors.base import AccountInaccessibleError, RemoteBalance, RemoteTransaction
from app.connectors.enable_banking.client import EnableBankingClient, EnableBankingError
from app.connectors.enable_banking.connector import EnableBankingConnector
from app.connectors.requester import BankRequester, BankUnavailableError
from app.ledger.sync import SyncDeferred, SyncError, SyncResult, linked_accounts, sync_account
from app.models import (
    Account,
    AccountSyncState,
    Alert,
    BalanceSnapshot,
    Connection,
    LedgerTransaction,
    ProviderCall,
)
from app.models.ledger import BalanceType, ConnectionStatus, Provider
from app.money import Cents
from app.net.client import make_client
from app.ops.checks import check_sync_freshness
from tests.connectors.eb_fake import FakeEnableBanking, credentials
from tests.fakes.connector import FakeBankConnector, InjectedFailureError

NOW = datetime(2026, 9, 27, 4, 30, tzinfo=UTC)  # 06:30 à Paris
UID = "00000000-0000-4000-8000-0000000000c1"


class Clock:
    def __init__(self) -> None:
        self.current = NOW

    def __call__(self) -> datetime:
        return self.current


@pytest.fixture
def linked(engine: Engine) -> Engine:
    with engine.begin() as connection:
        connection.execute(
            insert(Connection).values(
                id=1,
                provider=Provider.ENABLE_BANKING,
                institution_name="Banque Exemple",
                country="FR",
                status=ConnectionStatus.ACTIVE,
                consent_expires_at=NOW + timedelta(days=150),
                created_at=NOW,
                updated_at=NOW,
            )
        )
        connection.execute(
            update(Account)
            .where(Account.id == 1)
            .values(connection_id=1, provider_account_uid=UID, syncs_per_day=4)
        )
    return engine


def _client(fake: FakeEnableBanking, clock: Clock) -> EnableBankingConnector:
    requester = BankRequester(
        make_client(transport=fake.transport()), sleep=lambda _: None, now=clock
    )
    return EnableBankingConnector(EnableBankingClient(requester, credentials(), now=clock))


def _count(engine: Engine, model: type) -> int:
    with engine.connect() as connection:
        return int(connection.execute(select(func.count()).select_from(model)).scalar_one())


def test_first_sync_fetches_ninety_days_balances_and_transactions(linked: Engine) -> None:
    fake, clock = FakeEnableBanking(), Clock()
    result = sync_account(linked, _client(fake, clock), 1, unattended=True, now=clock())
    assert isinstance(result, SyncResult) and (result.inserted, result.balances) == (4, 2)
    first_page = next(r for r in fake.requests if r.url.path.endswith("/transactions"))
    assert first_page.url.params["date_from"] == "2026-06-29"  # 90 jours
    with linked.connect() as connection:
        state = connection.execute(select(AccountSyncState)).one()
        booked = connection.execute(
            select(BalanceSnapshot.amount_cents).where(BalanceSnapshot.balance_type == "booked")
        ).scalar_one()
    assert state.last_success_at == NOW and state.last_booking_date_seen == date(2026, 9, 12)
    assert booked == 123456


def test_next_sync_overlaps_seven_days_without_duplicates(linked: Engine) -> None:
    fake, clock = FakeEnableBanking(), Clock()
    client = _client(fake, clock)
    sync_account(linked, client, 1, unattended=True, now=clock())
    clock.current += timedelta(hours=5)
    fake.requests.clear()
    result = sync_account(linked, client, 1, unattended=True, now=clock())
    assert isinstance(result, SyncResult) and result.inserted == 0
    first_page = next(r for r in fake.requests if r.url.path.endswith("/transactions"))
    assert first_page.url.params["date_from"] == "2026-09-05"  # 12/09 - 7 jours
    assert _count(linked, LedgerTransaction) == 4


def test_power_cut_during_sync_loses_and_duplicates_nothing(linked: Engine) -> None:
    """Scénario d'acceptation du CDC : « Coupure électrique pendant une synchro »."""
    fake, clock = FakeEnableBanking(fail_second_page=True), Clock()
    with pytest.raises(BankUnavailableError):
        sync_account(linked, _client(fake, clock), 1, unattended=True, now=clock())
    assert _count(linked, LedgerTransaction) == 0  # tout ou rien : rien de partiel
    with linked.connect() as connection:
        assert connection.execute(select(AccountSyncState.last_error)).scalar_one()
    clock.current += timedelta(days=1, hours=1)  # créneau suivant, budget renouvelé
    fake.fail_second_page = False
    result = sync_account(linked, _client(fake, clock), 1, unattended=True, now=clock())
    assert isinstance(result, SyncResult) and result.inserted == 4
    assert _count(linked, LedgerTransaction) == 4


def test_refused_consent_expires_connection_and_alerts(linked: Engine) -> None:
    fake = FakeEnableBanking(status={f"/accounts/{UID}/balances": 401})
    clock = Clock()
    assert sync_account(linked, _client(fake, clock), 1, unattended=True, now=clock()) is None
    with linked.connect() as connection:
        assert (
            connection.execute(select(Connection.status)).scalar_one() is ConnectionStatus.EXPIRED
        )
        alert = connection.execute(select(Alert.dedupe_key, Alert.severity)).one()
    assert alert.dedupe_key == "consent_refused:1" and str(alert.severity) == "critical"
    assert linked_accounts(linked) == []  # plus de synchro automatique sur ce compte
    with pytest.raises(SyncError):
        sync_account(linked, _client(fake, clock), 1, unattended=True, now=clock())


def _state(engine: Engine) -> tuple[datetime | None, str | None]:
    with engine.connect() as connection:
        row = connection.execute(
            select(AccountSyncState.last_success_at, AccountSyncState.last_error)
        ).one()
    return row.last_success_at, row.last_error


def _alert_keys(engine: Engine) -> list[str]:
    with engine.connect() as connection:
        return list(connection.scalars(select(Alert.dedupe_key)))


def test_budget_defers_syncs_manual_ones_included(linked: Engine) -> None:
    """Le plafond compte toutes les requêtes ; atteint, la synchro est reportée sans erreur."""
    fake, clock = FakeEnableBanking(), Clock()
    client = _client(fake, clock)
    for unattended in (True, False):  # 2 pages de transactions par synchro
        sync_account(linked, client, 1, unattended=unattended, now=clock())
        clock.current += timedelta(minutes=30)
    for unattended in (True, False):
        result = sync_account(linked, client, 1, unattended=unattended, now=clock())
        assert isinstance(result, SyncDeferred) and "plafond DSP2" in result.reason
    with linked.connect() as connection:
        sent = connection.execute(
            select(func.count()).where(ProviderCall.path_template == "transactions")
        ).scalar_one()
    assert sent == DEFAULT_LIMIT


def test_rate_limit_defers_without_retry_and_alerts_only_after_24h(linked: Engine) -> None:
    """Q14 : un 429 ne se retente pas avant le créneau suivant, n'est pas une panne et
    l'alerte « synchro en échec » n'apparaît qu'après 24 h sans succès."""
    fake, clock = FakeEnableBanking(), Clock()
    sync_account(linked, _client(fake, clock), 1, unattended=True, now=clock())
    success = clock()
    fake.status[f"/accounts/{UID}/transactions"] = 429
    reason = None
    for hours in (5, 10, 15, 20):  # créneaux suivants, tous refusés par la banque
        clock.current = success + timedelta(hours=hours)
        fake.requests.clear()
        result = sync_account(linked, _client(fake, clock), 1, unattended=True, now=clock())
        # 429 envoyés : ils comptent dans le budget, qui finit par bloquer localement.
        assert isinstance(result, SyncDeferred)
        assert "429" in result.reason or "plafond DSP2" in result.reason
        assert sum(r.url.path.endswith("/transactions") for r in fake.requests) <= 1
        reason = result.reason
        check_sync_freshness(linked, now=clock())
        assert _alert_keys(linked) == []
    assert _state(linked) == (success, reason)
    check_sync_freshness(linked, now=success + timedelta(hours=24, minutes=1))
    assert _alert_keys(linked) == ["sync_stale:1"]


def test_quota_error_in_body_is_a_rate_limit(linked: Engine) -> None:
    fake, clock = FakeEnableBanking(), Clock()

    def quota_on_balances(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/balances"):
            return httpx.Response(422, json={"error": "ASPSP_RATE_LIMIT_EXCEEDED"})
        return fake(request)

    requester = BankRequester(
        make_client(transport=httpx.MockTransport(quota_on_balances)), now=clock
    )
    client = EnableBankingConnector(EnableBankingClient(requester, credentials(), now=clock))
    result = sync_account(linked, client, 1, unattended=True, now=clock())
    assert isinstance(result, SyncDeferred) and "quota" in result.reason


def test_ignored_account_is_never_synced(linked: Engine) -> None:
    """Q19 : un compte ignoré sort des créneaux automatiques et refuse la synchro manuelle."""
    with linked.begin() as connection:
        connection.execute(update(Account).where(Account.id == 1).values(ignored_at=NOW))
    assert linked_accounts(linked) == []
    fake, clock = FakeEnableBanking(), Clock()
    with pytest.raises(SyncError, match="ignoré"):
        sync_account(linked, _client(fake, clock), 1, unattended=False, now=clock())
    assert fake.requests == []  # aucune requête vers la banque


def test_refused_balance_does_not_block_transactions(linked: Engine) -> None:
    """Cas réel d'octobre 2026 : soldes de deux cartes refusés (400) ; les opérations sont
    quand même récupérées et le refus reste visible, avec le code de l'agrégateur."""
    fake, clock = FakeEnableBanking(), Clock()

    def refuse_balances(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/balances"):
            return httpx.Response(400, json={"error": "ASPSP_ERROR"})
        return fake(request)

    requester = BankRequester(
        make_client(transport=httpx.MockTransport(refuse_balances)), now=clock
    )
    client = EnableBankingConnector(EnableBankingClient(requester, credentials(), now=clock))
    result = sync_account(linked, client, 1, unattended=True, now=clock())
    assert not isinstance(result, SyncDeferred) and result is not None
    assert result.inserted > 0 and result.balances == 0
    success, error = _state(linked)
    assert success == clock() and error is not None
    assert "solde indisponible" in error and "ASPSP_ERROR" in error


def test_client_error_on_transactions_is_final_for_the_slot(linked: Engine) -> None:
    fake, clock = FakeEnableBanking(), Clock()
    fake.status[f"/accounts/{UID}/transactions"] = 400
    with pytest.raises(EnableBankingError, match="erreur 400") as caught:
        sync_account(linked, _client(fake, clock), 1, unattended=True, now=clock())
    assert caught.value.retryable is False


def test_exhausted_balance_budget_still_fetches_transactions(linked: Engine) -> None:
    """Budget DSP2 compté par endpoint : soldes épuisés, opérations encore permises."""
    from app.connectors.budget import acquire  # noqa: PLC0415

    fake, clock = FakeEnableBanking(), Clock()
    for _ in range(DEFAULT_LIMIT):
        acquire(
            linked, account_id=1, endpoint="balances", unattended=True, method="GET", now=clock()
        )
    result = sync_account(linked, _client(fake, clock), 1, unattended=True, now=clock())
    assert isinstance(result, SyncResult) and result.inserted > 0 and result.balances == 0
    assert not any(r.url.path.endswith("/balances") for r in fake.requests)
    assert "solde reporté" in (_state(linked)[1] or "")


def test_inaccessible_account_alerts_once_and_pauses(linked: Engine) -> None:
    """Cas réel du 6 octobre 2026 : cartes Boursorama renouvelées, la banque répond
    ASPSP_ACCOUNT_NOT_ACCESSIBLE ; alerte explicite, pas de nouvel essai avant 24 h."""
    fake, clock = FakeEnableBanking(), Clock()
    sync_account(linked, _client(fake, clock), 1, unattended=True, now=clock())  # tout va bien

    def refused(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "ASPSP_ACCOUNT_NOT_ACCESSIBLE"})

    requester = BankRequester(make_client(transport=httpx.MockTransport(refused)), now=clock)
    client = EnableBankingConnector(EnableBankingClient(requester, credentials(), now=clock))
    clock.current += timedelta(hours=12)
    with pytest.raises(AccountInaccessibleError, match="ASPSP_ACCOUNT_NOT_ACCESSIBLE"):
        sync_account(linked, client, 1, unattended=True, now=clock())
    assert _alert_keys(linked) == ["account_inaccessible:1"]
    clock.current += timedelta(hours=12)
    paused = sync_account(linked, client, 1, unattended=True, now=clock())
    assert isinstance(paused, SyncDeferred) and "inaccessible" in paused.reason
    clock.current += timedelta(hours=13)  # 24 h après l'essai : nouvel essai, banque rétablie
    fake.requests.clear()
    result = sync_account(linked, _client(fake, clock), 1, unattended=True, now=clock())
    assert isinstance(result, SyncResult)
    with linked.connect() as connection:
        statuses = connection.scalars(
            select(Alert.status).where(Alert.dedupe_key == "account_inaccessible:1")
        ).all()
    assert [str(s) for s in statuses] == ["resolved"]


def _remote(number: int, day: date) -> RemoteTransaction:
    return RemoteTransaction(
        external_id=f"ref-{number}",
        booking_date=day,
        value_date=None,
        amount=Cents(-1_000 * number),
        currency="EUR",
        label=f"CB COMMERCE {number}",
        counterparty_name=None,
        counterparty_iban=None,
        bank_tx_code=None,
    )


def test_sync_only_needs_the_common_connector_interface(linked: Engine) -> None:
    """NF-EXP-03 : un autre agrégateur s'ajoute par un adaptateur, sans toucher à la synchro."""
    connector = FakeBankConnector(
        balance_items={
            UID: [RemoteBalance(date(2026, 9, 26), BalanceType.BOOKED, Cents(5_00), "EUR")]
        },
        transaction_items={UID: [_remote(n, date(2026, 9, n)) for n in range(1, 6)]},
        page_size=2,
    )
    result = sync_account(linked, connector, 1, unattended=True, now=NOW)
    assert isinstance(result, SyncResult) and (result.inserted, result.balances) == (5, 1)
    assert connector.calls == [f"balances:{UID}"] + [f"transactions:{UID}:{i}" for i in range(3)]
    again = sync_account(linked, connector, 1, unattended=True, now=NOW + timedelta(hours=6))
    assert isinstance(again, SyncResult) and again.inserted == 0  # idempotent (inv. 3)


def test_sync_interrupted_mid_pages_stores_nothing_partial(linked: Engine) -> None:
    connector = FakeBankConnector(
        transaction_items={UID: [_remote(n, date(2026, 9, n)) for n in range(1, 6)]},
        page_size=2,
        fail_on_page=1,
    )
    with pytest.raises(InjectedFailureError):
        sync_account(linked, connector, 1, unattended=True, now=NOW)
    assert _count(linked, LedgerTransaction) == 0
    with linked.connect() as connection:
        assert "InjectedFailureError" in (
            connection.scalar(select(AccountSyncState.last_error)) or ""
        )
