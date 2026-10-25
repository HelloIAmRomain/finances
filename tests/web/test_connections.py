"""Liaison bancaire, retour de la banque, révocation (COL-01, COL-07)."""

from collections.abc import Iterator
from datetime import timedelta

import pytest
from sqlalchemy import Engine, func, select

from app.models import Account, AuditLog, Connection, LinkNonce
from app.models.ledger import ConnectionStatus
from tests.connectors.eb_fake import FakeEnableBanking, load
from tests.factories import fake_iban
from tests.web.bank_helpers import (
    bank_callback,
    bank_web_session,
    csrf_of,
    last_state,
    start_link,
)
from tests.web.conftest import Web


@pytest.fixture
def bank_web(engine: Engine) -> Iterator[tuple[Web, FakeEnableBanking]]:
    with bank_web_session(engine) as session:
        yield session


def _count(engine: Engine, model: type) -> int:
    with engine.connect() as connection:
        return int(connection.execute(select(func.count()).select_from(model)).scalar_one())


def test_page_lists_banks(bank_web: tuple[Web, FakeEnableBanking]) -> None:
    web, _ = bank_web
    page = web.client.get("/connexions").text
    assert "Banque Exemple" in page and "Autre Banque Exemple" in page


def test_full_link_creates_connection_and_accounts(bank_web: tuple[Web, FakeEnableBanking]) -> None:
    web, fake = bank_web
    assert start_link(web).startswith("https://tilisy.enablebanking.com/")
    assert bank_callback(web, last_state(fake)) == 303
    with web.engine.connect() as connection:
        link = connection.execute(select(Connection)).one()
        accounts = connection.execute(
            select(Account.name, Account.provider_account_uid, Account.iban_enc).where(
                Account.connection_id == link.id
            )
        ).all()
        audit = connection.scalars(select(AuditLog.action)).all()
    assert link.status is ConnectionStatus.ACTIVE
    assert link.provider_session_enc == "00000000-0000-4000-8000-0000000000aa"
    assert [(a.name, a.iban_enc) for a in accounts] == [
        ("Compte courant", fake_iban(201)),
        ("Livret", fake_iban(202)),
    ]
    assert "connection.linked" in audit


def test_forged_or_late_callback_is_refused(bank_web: tuple[Web, FakeEnableBanking]) -> None:
    web, fake = bank_web
    start_link(web)
    assert bank_callback(web, "etat-falsifie") == 400
    start_link(web)
    state = last_state(fake)
    web.clock.current += timedelta(minutes=16)
    assert bank_callback(web, state) == 400
    assert _count(web.engine, Connection) == 0


def test_callback_replay_is_refused(bank_web: tuple[Web, FakeEnableBanking]) -> None:
    """T1.12 : le jeton de retour est à usage unique, même rejoué dans les 15 minutes avec le même
    cookie et le même `state`."""
    web, fake = bank_web
    start_link(web)
    state, token = last_state(fake), web.client.cookies["liaison"]
    assert bank_callback(web, state) == 303
    web.clock.current += timedelta(minutes=1)
    web.client.cookies.set("liaison", token, path="/connexions")
    web.client.cookies.delete("session")
    replay = web.client.get(f"/connexions/retour?code=code-factice&state={state}")
    assert replay.status_code == 400 and "déjà utilisée" in replay.text
    assert _count(web.engine, Connection) == 1
    assert sum(r.url.path == "/sessions" for r in fake.requests) == 1  # banque non rappelée
    with web.engine.connect() as connection:
        used = connection.execute(select(LinkNonce.used_at, LinkNonce.nonce_hash)).one()
    assert used.used_at is not None and state not in used.nonce_hash  # empreinte seulement


def test_bank_error_is_shown(bank_web: tuple[Web, FakeEnableBanking]) -> None:
    web, _ = bank_web
    start_link(web)
    response = web.client.get("/connexions/retour?error=access_denied&state=x")
    assert response.status_code == 400 and "access_denied" in response.text


def test_relinking_reuses_accounts_and_revokes_old_connection(
    bank_web: tuple[Web, FakeEnableBanking],
) -> None:
    web, fake = bank_web
    for _ in range(2):
        fake.requests.clear()
        start_link(web)
        bank_callback(web, last_state(fake))
    with web.engine.connect() as connection:
        statuses = [
            str(s) for s in connection.scalars(select(Connection.status).order_by(Connection.id))
        ]
    assert statuses == ["revoked", "active"]
    assert (
        _count(web.engine, Account) == 4
    )  # 2 comptes de la fixture + 2 comptes bancaires, sans doublon


def _card(identification_hash: str | None) -> dict[str, object]:
    card: dict[str, object] = {
        "uid": "00000000-0000-4000-8000-0000000000c9",
        "name": "Carte Visa",
        "currency": "EUR",
        "cash_account_type": "CARD",
    }
    if identification_hash:
        card["identification_hash"] = identification_hash
    return card


def test_card_is_consultation_only_and_not_duplicated_on_renewal(
    bank_web: tuple[Web, FakeEnableBanking],
) -> None:
    """Une carte n'a pas d'IBAN : au renouvellement, elle est retrouvée (par nom et type la
    première fois, puis par l'identifiant stable du fournisseur), jamais recréée."""
    web, fake = bank_web
    base = load("session.json")
    for identification_hash in (None, "hash-stable-carte", "hash-stable-carte"):
        fake.session = {**base, "accounts": [*base["accounts"], _card(identification_hash)]}
        fake.requests.clear()
        start_link(web)
        assert bank_callback(web, last_state(fake)) == 303
    with web.engine.connect() as connection:
        cards = connection.execute(select(Account).where(Account.type == "card")).all()
        assert _count(web.engine, Account) == 5  # 2 de la fixture + courant, livret, carte
    [card] = cards
    assert card.include_in_net_worth is False  # ses paiements figurent sur le compte courant
    assert card.provider_account_hash and "hash-stable-carte" not in card.provider_account_hash


def test_revocation_deletes_session(bank_web: tuple[Web, FakeEnableBanking]) -> None:
    web, fake = bank_web
    start_link(web)
    bank_callback(web, last_state(fake))
    csrf = csrf_of(web.client.get("/connexions").text)
    web.client.post("/connexions/1/revoquer", data={"csrf": csrf})
    assert ("DELETE", "/sessions/00000000-0000-4000-8000-0000000000aa") in [
        (r.method, r.url.path) for r in fake.requests
    ]
    with web.engine.connect() as connection:
        assert (
            connection.execute(select(Connection.status)).scalar_one() is ConnectionStatus.REVOKED
        )


def test_non_https_bank_address_is_never_linked(bank_web: tuple[Web, FakeEnableBanking]) -> None:
    """Défense en profondeur (Semgrep, 06/10/2026) : l'adresse donnée par l'agrégateur n'est
    affichée que si elle est en HTTPS (jamais de lien `javascript:`)."""
    web, _ = bank_web
    web.app.state.bank.start_authorization = lambda **_: "javascript:alert(1)"
    csrf = web.client.get("/connexions").text.split('name="csrf" value="', 1)[1].split('"', 1)[0]
    response = web.client.post(
        "/connexions/lier", data={"csrf": csrf, "banque": "Banque Exemple"}, follow_redirects=False
    )
    assert response.status_code == 303 and "erreur=" in response.headers["location"]
    assert "javascript:" not in web.client.get(response.headers["location"]).text


def test_incomplete_bank_return_is_explained(bank_web: tuple[Web, FakeEnableBanking]) -> None:
    web, fake = bank_web
    start_link(web)
    response = web.client.get(f"/connexions/retour?state={last_state(fake)}")  # sans code
    assert response.status_code == 400 and "Retour incomplet de la banque" in response.text
    assert _count(web.engine, Connection) == 0


def test_revocation_failures_are_explained_in_french(
    bank_web: tuple[Web, FakeEnableBanking],
) -> None:
    web, fake = bank_web
    start_link(web)
    bank_callback(web, last_state(fake))
    csrf = csrf_of(web.client.get("/connexions").text)
    unknown = web.client.post("/connexions/999/revoquer", data={"csrf": csrf})
    assert "Connexion introuvable" in unknown.text and "LinkingError" not in unknown.text
    fake.status["/sessions/00000000-0000-4000-8000-0000000000aa"] = 400
    refused = web.client.post("/connexions/1/revoquer", data={"csrf": csrf})
    assert "La banque n&#39;a pas confirmé le retrait" in refused.text
    assert "EnableBankingError" not in refused.text
    with web.engine.connect() as connection:
        assert connection.execute(select(Connection.status)).scalar_one() is ConnectionStatus.ACTIVE


@pytest.mark.parametrize("path", ["/aspsps", "/auth"])
def test_bank_down_while_linking_is_explained(
    bank_web: tuple[Web, FakeEnableBanking], path: str
) -> None:
    web, fake = bank_web
    csrf = csrf_of(web.client.get("/connexions").text)
    fake.status[path] = 503
    response = web.client.post("/connexions/lier", data={"csrf": csrf, "banque": "Banque Exemple"})
    assert response.status_code == 200
    assert "aucune connexion n&#39;a été créée" in response.text
    assert _count(web.engine, Connection) == 0


def test_bank_refusal_never_echoes_free_text(bank_web: tuple[Web, FakeEnableBanking]) -> None:
    """Page sans session : un lien piégé ne peut pas y faire afficher son propre texte."""
    web, _ = bank_web
    forged = web.client.get("/connexions/retour?error=Appelez+le+01+23+45+67+89+pour+débloquer")
    assert forged.status_code == 400 and "Appelez" not in forged.text
    assert "refusé ou annulé" in forged.text
    coded = web.client.get("/connexions/retour?error=access_denied")
    assert "(code access_denied)" in coded.text
