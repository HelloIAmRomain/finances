"""Vérification de la conversion sur les réponses stockées et échantillons anonymisés (T1.10)."""

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine, insert, update

from app.connectors.enable_banking.client import EnableBankingClient
from app.connectors.enable_banking.connector import EnableBankingConnector
from app.connectors.enable_banking.samples import anonymized_samples, conversion_report
from app.connectors.requester import BankRequester
from app.ledger.sync import sync_account
from app.models import Account, Connection
from app.models.ledger import ConnectionStatus, Provider
from app.net.client import make_client
from tests.connectors.eb_fake import FakeEnableBanking, credentials, load

NOW = datetime(2026, 9, 27, 4, 30, tzinfo=UTC)
UID = "00000000-0000-4000-8000-0000000000c1"


def _synced(engine: Engine, times: int = 2) -> Engine:
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
    fake = FakeEnableBanking()
    for n in range(times):  # la même page relue à chaque synchro
        now = NOW + timedelta(hours=6 * n)

        def clock(now: datetime = now) -> datetime:
            return now

        requester = BankRequester(make_client(transport=fake.transport()), now=clock)
        client = EnableBankingClient(requester, credentials(), now=clock)
        sync_account(engine, EnableBankingConnector(client), 1, unattended=True, now=now)
    return engine


def test_conversion_report_counts_without_content(engine: Engine) -> None:
    report = conversion_report(_synced(engine))["Banque Exemple"]
    # Réponse identique relue : stockée une seule fois (empreinte du contenu, COL-08).
    assert report["erreurs"] == {} and report["reponses_transactions"] == 2
    assert report["converties"] == report["en_base"]  # aucune perte, aucun doublon
    assert report["operations_distinctes"] <= report["en_base"]
    assert set(report["statuts"]) <= {"BOOK", "PDNG"}
    dumped = json.dumps(report, ensure_ascii=False)
    for item in load("transactions_page1.json")["transactions"]:
        for text in item.get("remittance_information") or []:
            assert text not in dumped  # que des statistiques, jamais de contenu


def test_anonymized_samples_keep_shape_but_no_content(engine: Engine) -> None:
    samples = anonymized_samples(_synced(engine))
    checking = samples["Banque Exemple"]["checking"]
    assert {"soldes", "transactions"} <= checking.keys()
    dumped = json.dumps(samples, ensure_ascii=False)
    for name in ("transactions_page1.json", "balances.json"):
        document = load(name)
        for item in document.get("transactions", []):
            for text in item.get("remittance_information") or []:
                assert text not in dumped
            assert (
                item["transaction_amount"]["amount"] not in dumped
                or len(item["transaction_amount"]["amount"]) < 4
            )
    assert checking["transactions"]["transactions"][0]["status"] in {"BOOK", "PDNG"}
