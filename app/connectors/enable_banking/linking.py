"""Liaison d'une banque et révocation du consentement (COL-01, COL-07, runbook de vol).

- L'état de la demande (`state`) voyage dans un jeton signé (HMAC, 15 min) : le retour depuis la
  banque arrive sans cookie de session (`SameSite=Strict`), il est authentifié par ce jeton.
  Le `state` est aussi un nonce à **usage unique** : son empreinte est enregistrée à l'émission et
  invalidée au premier retour, un rejeu du même retour (même dans les 15 min) est refusé.
- À la création de la session : une connexion active, et un compte par compte bancaire, retrouvé
  par son IBAN, sinon par l'identifiant stable du fournisseur (index HMAC dans les deux cas), pour
  ne jamais créer de doublon de compte lors d'un renouvellement. Une carte (sans IBAN) est créée
  en consultation seule : ses paiements figurent déjà sur le compte courant.
- Révocation : `DELETE /sessions/{id}`, connexion « révoquée », auditée.
"""

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Connection as DBConnection
from sqlalchemy import Engine, delete, insert, select, update

from app.audit import audit
from app.connectors.base import RemoteAccount
from app.connectors.enable_banking.client import EnableBankingClient
from app.connectors.enable_banking.mapping import to_account
from app.crypto import blind_index
from app.db import inserted_id, write_transaction
from app.iban import normalize_iban
from app.models import Account, Connection, LinkNonce
from app.models.ledger import AccountType, ConnectionStatus, Provider
from app.timeutil import ensure_utc

STATE_LIFETIME = timedelta(minutes=15)
DEFAULT_CONSENT = timedelta(days=180)
DEFAULT_SYNCS_PER_DAY = 2


class LinkingError(ValueError):
    """Retour de la banque invalide (état, délai, erreur signalée)."""


@dataclass(frozen=True)
class PendingLink:
    state: str
    aspsp: str
    country: str
    user_id: int
    expires_at: datetime


def _sign(secret_key: str, payload: bytes) -> str:
    return hmac.new(secret_key.encode(), payload, hashlib.sha256).hexdigest()


def _nonce_hash(state: str) -> str:
    return hashlib.sha256(state.encode()).hexdigest()


def issue_state(
    engine: Engine,
    secret_key: str,
    *,
    aspsp: str,
    country: str,
    user_id: int,
    now: datetime,
) -> tuple[str, str]:
    """Renvoie (state à transmettre à la banque, jeton signé à garder en cookie)."""
    state = secrets.token_urlsafe(24)
    with write_transaction(engine) as connection:
        # Ménage : les nonces périmés depuis un jour ne servent plus à rien.
        connection.execute(delete(LinkNonce).where(LinkNonce.expires_at < now - timedelta(days=1)))
        connection.execute(
            insert(LinkNonce).values(
                nonce_hash=_nonce_hash(state), created_at=now, expires_at=now + STATE_LIFETIME
            )
        )
    payload = json.dumps(
        {
            "s": state,
            "a": aspsp,
            "c": country,
            "u": user_id,
            "e": (now + STATE_LIFETIME).isoformat(),
        },
        separators=(",", ":"),
    ).encode()
    token = base64.urlsafe_b64encode(payload).decode() + "." + _sign(secret_key, payload)
    return state, token


def read_state(
    engine: Engine, secret_key: str, token: str | None, state: str | None, *, now: datetime
) -> PendingLink:
    """Vérifie le retour de la banque et **consomme** le nonce : un second appel échoue."""
    if not token or not state or "." not in token:
        raise LinkingError("Demande de liaison inconnue ou expirée : recommencer.")
    encoded, signature = token.rsplit(".", 1)
    try:
        payload = base64.urlsafe_b64decode(encoded.encode())
    except ValueError:
        raise LinkingError("Jeton de liaison illisible.") from None
    if not hmac.compare_digest(_sign(secret_key, payload), signature):
        raise LinkingError("Jeton de liaison falsifié.")
    data = json.loads(payload)
    if not hmac.compare_digest(str(data["s"]), state):
        raise LinkingError("État de retour inattendu (possible tentative de détournement).")
    expires = ensure_utc(datetime.fromisoformat(data["e"]))
    if now > expires:
        raise LinkingError("Demande de liaison expirée : recommencer.")
    with write_transaction(engine) as connection:
        consumed = connection.execute(
            update(LinkNonce)
            .where(
                LinkNonce.nonce_hash == _nonce_hash(state),
                LinkNonce.used_at.is_(None),
                LinkNonce.expires_at >= now,
            )
            .values(used_at=now)
        ).rowcount
    if consumed != 1:
        raise LinkingError("Demande de liaison déjà utilisée ou inconnue : recommencer.")
    return PendingLink(data["s"], data["a"], data["c"], int(data["u"]), expires)


def consent_validity(aspsp: dict[str, Any] | None) -> timedelta:
    seconds = (aspsp or {}).get("maximum_consent_validity")
    return timedelta(seconds=int(seconds)) if seconds else DEFAULT_CONSENT


def _existing_account(
    connection: DBConnection,
    aspsp: str,
    remote: RemoteAccount,
    iban_hash: str | None,
    provider_hash: str | None,
) -> int | None:
    """Compte déjà connu : par IBAN, sinon par identifiant stable du fournisseur, sinon (compte sans
    IBAN créé avant cet identifiant, carte par exemple) par banque, nom, type et devise, s'il n'y
    a qu'un seul candidat. Évite de dupliquer un compte à chaque renouvellement."""
    if iban_hash:
        found = connection.execute(select(Account.id).where(Account.iban_hash == iban_hash))
        return found.scalar()
    if provider_hash:
        found = connection.execute(
            select(Account.id).where(Account.provider_account_hash == provider_hash)
        )
        if (account_id := found.scalar()) is not None:
            return account_id
    candidates = connection.scalars(
        select(Account.id)
        .join(Connection, Connection.id == Account.connection_id)
        .where(
            Connection.provider == Provider.ENABLE_BANKING,
            Connection.institution_name == aspsp,
            Account.iban_hash.is_(None),
            Account.provider_account_hash.is_(None),
            Account.name == remote.name,
            Account.type == remote.type,
            Account.currency == remote.currency,
        )
    ).all()
    return candidates[0] if len(candidates) == 1 else None


def complete_link(
    engine: Engine, client: EnableBankingClient, pending: PendingLink, code: str, *, now: datetime
) -> int:
    """Crée la session chez l'agrégateur, la connexion et les comptes ; renvoie la connexion."""
    session = client.create_session(code)
    valid_until = ensure_utc(datetime.fromisoformat(session["access"]["valid_until"]))
    accounts = [to_account(item) for item in session.get("accounts", [])]
    with write_transaction(engine) as connection:
        result = connection.execute(
            insert(Connection).values(
                provider=Provider.ENABLE_BANKING,
                institution_name=pending.aspsp,
                country=pending.country,
                status=ConnectionStatus.ACTIVE,
                provider_session_enc=str(session["session_id"]),
                consent_expires_at=valid_until,
                created_at=now,
                updated_at=now,
            )
        )
        connection_id = inserted_id(result)
        for remote in accounts:
            iban_hash = (
                blind_index(normalize_iban(remote.iban), context="iban") if remote.iban else None
            )
            provider_hash = (
                blind_index(remote.identification_hash, context="provider_account")
                if remote.identification_hash
                else None
            )
            existing = _existing_account(
                connection, pending.aspsp, remote, iban_hash, provider_hash
            )
            if existing is not None:
                connection.execute(
                    update(Account)
                    .where(Account.id == existing)
                    .values(
                        connection_id=connection_id,
                        provider_account_uid=remote.uid,
                        provider_account_hash=provider_hash,
                    )
                )
            else:
                connection.execute(
                    insert(Account).values(
                        connection_id=connection_id,
                        provider_account_uid=remote.uid,
                        name=remote.name,
                        type=remote.type,
                        iban_enc=normalize_iban(remote.iban) if remote.iban else None,
                        iban_hash=iban_hash,
                        provider_account_hash=provider_hash,
                        currency=remote.currency,
                        # Carte à débit immédiat : ses opérations répètent celles du compte
                        # courant, elle est donc en consultation seule par défaut.
                        include_in_net_worth=remote.type is not AccountType.CARD,
                        syncs_per_day=DEFAULT_SYNCS_PER_DAY,
                    )
                )
        # Un renouvellement remplace l'ancienne connexion à la même banque.
        connection.execute(
            update(Connection)
            .where(
                Connection.id != connection_id,
                Connection.provider == Provider.ENABLE_BANKING,
                Connection.institution_name == pending.aspsp,
                Connection.status.in_([ConnectionStatus.ACTIVE, ConnectionStatus.EXPIRED]),
            )
            .values(status=ConnectionStatus.REVOKED, updated_at=now)
        )
        audit(
            connection,
            "connection.linked",
            ("connection", connection_id),
            now=now,
            actor_user_id=pending.user_id,
            details={"banque": pending.aspsp, "comptes": len(accounts)},
        )
    return connection_id


def revoke(
    engine: Engine,
    client: EnableBankingClient,
    connection_id: int,
    *,
    actor_user_id: int | None,
    now: datetime,
) -> None:
    with engine.connect() as connection:
        row = connection.execute(
            select(Connection.provider_session_enc, Connection.status).where(
                Connection.id == connection_id
            )
        ).first()
    if row is None:
        raise LinkingError("Connexion introuvable.")
    if row.status is not ConnectionStatus.REVOKED and row.provider_session_enc:
        client.delete_session(row.provider_session_enc)
    with write_transaction(engine) as connection:
        connection.execute(
            update(Connection)
            .where(Connection.id == connection_id)
            .values(status=ConnectionStatus.REVOKED, updated_at=now)
        )
        audit(
            connection,
            "connection.revoked",
            ("connection", connection_id),
            now=now,
            actor_user_id=actor_user_id,
        )
