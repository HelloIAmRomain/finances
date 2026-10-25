"""Page « Comptes » (COL-07, O1) et création de compte alimenté par fichiers."""

from datetime import date, timedelta

from sqlalchemy import func, insert, select, update

from app.ledger.accounts import mask_iban
from app.models import (
    Account,
    AccountSyncState,
    Alert,
    AuditLog,
    BalanceSnapshot,
    Connection,
    LedgerTransaction,
)
from app.models.ledger import BalanceType, ConnectionStatus, Provider
from tests.factories import fake_iban
from tests.web.conftest import T0, Web


def _csrf(page: str) -> str:
    return page.split('name="csrf" value="', 1)[1].split('"', 1)[0]


def test_accounts_page_shows_connection_freshness_and_balance(web: Web) -> None:
    with web.engine.begin() as connection:
        connection.execute(
            insert(Connection).values(
                id=1,
                provider=Provider.ENABLE_BANKING,
                institution_name="Banque Exemple",
                country="FR",
                status=ConnectionStatus.ACTIVE,
                consent_expires_at=T0 + timedelta(days=120),
                created_at=T0,
                updated_at=T0,
            )
        )
        connection.execute(
            update(Account)
            .where(Account.id == 1)
            .values(name="Compte courant", connection_id=1, iban_enc=fake_iban(5))
        )
        connection.execute(insert(AccountSyncState).values(account_id=1, last_success_at=T0))
        connection.execute(
            insert(BalanceSnapshot).values(
                account_id=1,
                as_of_date=date(2026, 9, 26),
                balance_type=BalanceType.BOOKED,
                amount_cents=123456,
                currency="EUR",
                fetched_at=T0,
            )
        )
    web.login()
    page = web.client.get("/comptes").text
    assert "Banque Exemple" in page and "active" in page
    assert "jusqu'au 25/01/2027" in page
    assert "27/09/2026 à 12:00" in page  # 10:00 UTC → heure de Paris
    assert "1\u202f234,56\u00a0€" in page and "au 26/09/2026" in page
    masked = mask_iban(fake_iban(5))
    assert masked is not None and masked in page
    assert fake_iban(5) not in page


def test_create_file_account(web: Web) -> None:
    web.login()
    csrf = _csrf(web.client.get("/comptes").text)
    response = web.client.post(
        "/comptes",
        data={
            "csrf": csrf,
            "nom": "Livret A",
            "type": "savings",
            "iban": fake_iban(9).lower(),
            "patrimoine": "true",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    with web.engine.connect() as connection:
        account = connection.execute(select(Account).where(Account.name == "Livret A")).one()
        raw: bytes = connection.exec_driver_sql(
            "SELECT iban_enc FROM account WHERE name = 'Livret A'"
        ).scalar_one()
        actions = list(connection.scalars(select(AuditLog.action)))
    assert account.iban_enc == fake_iban(9) and account.iban_hash
    assert fake_iban(9).encode() not in raw
    assert "account.created" in actions


def test_invalid_iban_is_refused(web: Web) -> None:
    web.login()
    csrf = _csrf(web.client.get("/comptes").text)
    wrong = fake_iban(9)[:-1] + ("0" if fake_iban(9)[-1] != "0" else "1")
    response = web.client.post(
        "/comptes", data={"csrf": csrf, "nom": "X", "type": "checking", "iban": wrong}
    )
    assert response.status_code == 422 and "IBAN invalide" in response.text


def test_account_settings_consultation_only_and_sync_frequency(web: Web) -> None:
    """Compte joint ou carte : consultation seule ; compte d'encaissement : 4 synchros par jour.
    Chaque changement est audité ; un réglage identique ne produit rien."""
    web.login()
    csrf = _csrf(web.client.get("/comptes").text)
    settings = {"csrf": csrf, "suivi": "consultation", "synchros": "4"}
    for _ in range(2):
        response = web.client.post("/comptes/2/reglages", data=settings, follow_redirects=False)
        assert response.status_code == 303
    with web.engine.connect() as connection:
        account = connection.execute(select(Account).where(Account.id == 2)).one()
        audits = connection.execute(
            select(AuditLog.details).where(AuditLog.action == "account.settings")
        ).all()
    assert (account.include_in_net_worth, account.syncs_per_day) == (False, 4)
    assert len(audits) == 1 and audits[0].details["avant"]["compte"] is True
    page = web.client.get("/comptes").text
    assert "consultation seule</span>" in page
    too_many = web.client.post("/comptes/2/reglages", data={**settings, "synchros": "5"})
    assert too_many.status_code == 422 and "plafond fixé par les banques" in too_many.text
    unknown = web.client.post("/comptes/99/reglages", data=settings)
    assert unknown.status_code == 422 and "introuvable" in unknown.text
    assert web.client.post("/comptes/2/reglages", data={**settings, "csrf": ""}).status_code == 403


def test_ignored_account_disappears_and_can_be_reactivated(web: Web) -> None:
    """Q19 : compte ignoré → plus de transactions visibles, ni recherche ni export, section
    « Comptes ignorés », alerte de retard résolue ; réactivation possible, données intactes."""
    with web.engine.begin() as connection:
        connection.execute(update(Account).where(Account.id == 2).values(name="Compte indivision"))
        connection.execute(
            insert(LedgerTransaction).values(
                id=1,
                account_id=2,
                dedupe_key="ext:t:1",
                fingerprint="x",
                occurrence=1,
                booking_date=date(2026, 9, 3),
                amount_cents=-4200,
                currency="EUR",
                label_raw="OPERATION INDIVISION",
                label_clean="OPERATION INDIVISION",
                created_at=T0,
            )
        )
        connection.execute(
            insert(Alert).values(
                type="sync_stale",
                severity="warning",
                status="open",
                dedupe_key="sync_stale:2",
                title="Aucune synchro bancaire réussie depuis 24 h",
                details={},
                opened_at=T0,
            )
        )
    web.login()
    assert "OPERATION INDIVISION" in web.client.get("/transactions").text
    csrf = _csrf(web.client.get("/comptes").text)
    ignore = {"csrf": csrf, "suivi": "ignore", "synchros": "2"}
    assert web.client.post("/comptes/2/reglages", data=ignore).status_code == 200
    assert "OPERATION INDIVISION" not in web.client.get("/transactions").text
    assert "OPERATION INDIVISION" not in web.client.get("/transactions/export.csv").text
    page = web.client.get("/comptes").text
    ignored_section = page.split("Comptes ignorés (1)", 1)[1]
    assert "Compte indivision" in ignored_section
    assert "Compte indivision" not in page.split("Comptes ignorés (1)", 1)[0].split("<tbody>", 1)[1]
    with web.engine.connect() as connection:
        assert (
            connection.scalar(select(Alert.status).where(Alert.dedupe_key == "sync_stale:2"))
            == "resolved"
        )
        assert connection.scalar(select(Account.ignored_at).where(Account.id == 2)) is not None
        assert connection.scalar(select(func.count()).select_from(LedgerTransaction)) == 1
    reactivate = {**ignore, "suivi": "compte"}
    web.client.post("/comptes/2/reglages", data=reactivate)
    assert "OPERATION INDIVISION" in web.client.get("/transactions").text


def test_create_requires_csrf(web: Web) -> None:
    web.login()
    response = web.client.post("/comptes", data={"nom": "X", "type": "checking"})
    assert response.status_code == 403
