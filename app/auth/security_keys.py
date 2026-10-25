"""Clés de sécurité FIDO2 / WebAuthn (YubiKey en USB-C ou en NFC) : second facteur au choix avec
le code TOTP (NF-SEC-04).

- Inscription depuis une session ouverte, après confirmation du mot de passe.
- Connexion : mot de passe d'abord (même anti-force brute que le TOTP), puis la clé.
- Défis aléatoires à usage unique, valables 5 minutes, liés à l'utilisateur et à leur usage ;
  seule leur empreinte est en base. Une réponse rejouée ou détournée est refusée.
- Partie de confiance : le nom Tailscale du serveur (`public_base_url`) ; le navigateur refuse
  toute clé présentée à un autre site (hameçonnage impossible).
- Compteur de signatures vérifié : une clé clonée est détectée.
- Aucune attestation demandée : on ne collecte pas le modèle exact de la clé.
"""

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import Engine, delete, insert, select, update
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.exceptions import InvalidAuthenticationResponse, InvalidRegistrationResponse
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from app.audit import audit
from app.auth.users import AuthError, check_password, record_failure, record_success
from app.db import inserted_id, write_transaction
from app.models import AppUser, SecurityKey, WebAuthnChallenge
from app.timeutil import ensure_utc

RP_NAME = "Finances"
CHALLENGE_LIFETIME = timedelta(minutes=5)
REGISTER = "inscription"
LOGIN = "connexion"
MAX_KEY_NAME = 64


class SecurityKeyError(ValueError):
    """Inscription ou connexion par clé impossible (message lisible)."""


@dataclass(frozen=True)
class RelyingParty:
    """Le site auquel les clés sont liées : nom d'hôte et origine exacte."""

    rp_id: str
    origin: str

    @classmethod
    def from_base_url(cls, base_url: str) -> "RelyingParty":
        parts = urlsplit(base_url.rstrip("/"))
        if parts.scheme != "https" or not parts.hostname:
            raise SecurityKeyError("Les clés de sécurité exigent une adresse HTTPS.")
        return cls(rp_id=parts.hostname, origin=f"{parts.scheme}://{parts.netloc}")


@dataclass(frozen=True)
class KeyRow:
    id: int
    name: str
    created_at: datetime
    last_used_at: datetime | None


def _hash(challenge: bytes) -> str:
    return hashlib.sha256(challenge).hexdigest()


def _store_challenge(engine: Engine, user_id: int, purpose: str, now: datetime) -> bytes:
    challenge = secrets.token_bytes(32)
    with write_transaction(engine) as connection:
        connection.execute(delete(WebAuthnChallenge).where(WebAuthnChallenge.expires_at < now))
        connection.execute(
            insert(WebAuthnChallenge).values(
                user_id=user_id,
                purpose=purpose,
                challenge_hash=_hash(challenge),
                created_at=now,
                expires_at=now + CHALLENGE_LIFETIME,
            )
        )
    return challenge


def _consume_challenge(
    engine: Engine, credential: dict[str, Any], purpose: str, now: datetime
) -> tuple[int, bytes]:
    """Retrouve le défi signé par le navigateur et l'invalide ; renvoie (utilisateur, défi)."""
    try:
        client_data = json.loads(base64url_to_bytes(credential["response"]["clientDataJSON"]))
        challenge = base64url_to_bytes(client_data["challenge"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SecurityKeyError("Réponse de la clé illisible.") from exc
    with write_transaction(engine) as connection:
        row = connection.execute(
            select(WebAuthnChallenge).where(WebAuthnChallenge.challenge_hash == _hash(challenge))
        ).first()
        if (
            row is None
            or row.purpose != purpose
            or row.used_at is not None
            or ensure_utc(row.expires_at) <= now
        ):
            raise SecurityKeyError("Demande expirée ou déjà utilisée : recommencer.")
        connection.execute(
            update(WebAuthnChallenge).where(WebAuthnChallenge.id == row.id).values(used_at=now)
        )
    return int(row.user_id), challenge


def _descriptors(engine: Engine, user_id: int) -> list[PublicKeyCredentialDescriptor]:
    with engine.connect() as connection:
        ids = connection.scalars(
            select(SecurityKey.credential_id).where(SecurityKey.user_id == user_id)
        ).all()
    return [PublicKeyCredentialDescriptor(id=base64url_to_bytes(i)) for i in ids]


# --- Inscription ------------------------------------------------------------------------------


def registration_options(
    engine: Engine, *, user_id: int, password: str, rp: RelyingParty, now: datetime
) -> str:
    """Options pour `navigator.credentials.create` (JSON), après confirmation du mot de passe."""
    with engine.connect() as connection:
        username = connection.scalar(select(AppUser.username).where(AppUser.id == user_id))
    if username is None:
        raise SecurityKeyError("Utilisateur introuvable.")
    try:
        check_password(engine, username=username, password=password, now=now)
    except AuthError as exc:
        raise SecurityKeyError(str(exc)) from exc
    options = generate_registration_options(
        rp_id=rp.rp_id,
        rp_name=RP_NAME,
        user_name=username,
        user_id=str(user_id).encode(),
        challenge=_store_challenge(engine, user_id, REGISTER, now),
        exclude_credentials=_descriptors(engine, user_id),
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.DISCOURAGED,
            user_verification=UserVerificationRequirement.DISCOURAGED,
        ),
    )
    return options_to_json(options)


def register(
    engine: Engine,
    *,
    user_id: int,
    credential: dict[str, Any],
    name: str,
    rp: RelyingParty,
    now: datetime,
) -> int:
    name = " ".join(name.split())[:MAX_KEY_NAME] or "Clé de sécurité"
    owner, challenge = _consume_challenge(engine, credential, REGISTER, now)
    if owner != user_id:
        raise SecurityKeyError("Demande d'inscription d'un autre utilisateur.")
    try:
        verified = verify_registration_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=rp.rp_id,
            expected_origin=rp.origin,
        )
    except InvalidRegistrationResponse as exc:
        raise SecurityKeyError(f"Clé refusée : {exc}") from exc
    credential_id = bytes_to_base64url(verified.credential_id)
    with write_transaction(engine) as connection:
        if connection.scalar(
            select(SecurityKey.id).where(SecurityKey.credential_id == credential_id)
        ):
            raise SecurityKeyError("Cette clé est déjà enregistrée.")
        result = connection.execute(
            insert(SecurityKey).values(
                user_id=user_id,
                credential_id=credential_id,
                public_key=verified.credential_public_key,
                sign_count=verified.sign_count,
                name=name,
                created_at=now,
            )
        )
        key_id = inserted_id(result)
        audit(
            connection,
            "security_key.registered",
            ("security_key", key_id),
            now=now,
            actor_user_id=user_id,
            details={"nom": name},
        )
    return key_id


def keys(engine: Engine, user_id: int) -> list[KeyRow]:
    with engine.connect() as connection:
        return [
            KeyRow(r.id, r.name, r.created_at, r.last_used_at)
            for r in connection.execute(
                select(SecurityKey).where(SecurityKey.user_id == user_id).order_by(SecurityKey.id)
            )
        ]


def remove(engine: Engine, *, user_id: int, key_id: int, now: datetime) -> None:
    """Retire une clé (perte) ; le code TOTP reste toujours utilisable."""
    with write_transaction(engine) as connection:
        result = connection.execute(
            delete(SecurityKey).where(SecurityKey.id == key_id, SecurityKey.user_id == user_id)
        )
        if result.rowcount != 1:
            raise SecurityKeyError("Clé introuvable.")
        audit(
            connection,
            "security_key.removed",
            ("security_key", key_id),
            now=now,
            actor_user_id=user_id,
        )


# --- Connexion --------------------------------------------------------------------------------


def login_options(
    engine: Engine, *, username: str, password: str, rp: RelyingParty, now: datetime
) -> str:
    """Après le mot de passe : options pour `navigator.credentials.get` (JSON)."""
    user = check_password(engine, username=username, password=password, now=now)
    allowed = _descriptors(engine, int(user.id))
    if not allowed:
        raise SecurityKeyError(
            "Aucune clé de sécurité enregistrée : utiliser le code à 6 chiffres."
        )
    options = generate_authentication_options(
        rp_id=rp.rp_id,
        challenge=_store_challenge(engine, int(user.id), LOGIN, now),
        allow_credentials=allowed,
        user_verification=UserVerificationRequirement.DISCOURAGED,
    )
    return options_to_json(options)


def decoy_login_options(rp: RelyingParty) -> str:
    """Options de même forme pour un mot de passe faux (ou un compte sans clé) : la réponse ne
    révèle pas si le mot de passe était bon. Le défi n'est pas retenu, la connexion échouera."""
    options = generate_authentication_options(
        rp_id=rp.rp_id,
        challenge=secrets.token_bytes(64),
        allow_credentials=[PublicKeyCredentialDescriptor(id=secrets.token_bytes(64))],
        user_verification=UserVerificationRequirement.DISCOURAGED,
    )
    return options_to_json(options)


def login(engine: Engine, *, credential: dict[str, Any], rp: RelyingParty, now: datetime) -> int:
    """Vérifie la signature de la clé ; renvoie l'utilisateur connecté."""
    user_id, challenge = _consume_challenge(engine, credential, LOGIN, now)
    with engine.connect() as connection:
        user = connection.execute(select(AppUser).where(AppUser.id == user_id)).one()
        key = connection.execute(
            select(SecurityKey).where(
                SecurityKey.user_id == user_id,
                SecurityKey.credential_id == str(credential.get("id", "")),
            )
        ).first()
    if key is None:
        record_failure(engine, user.username, now=now)
        raise SecurityKeyError("Clé inconnue pour ce compte.")
    try:
        verified = verify_authentication_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=rp.rp_id,
            expected_origin=rp.origin,
            credential_public_key=key.public_key,
            credential_current_sign_count=key.sign_count,
        )
    except InvalidAuthenticationResponse as exc:
        record_failure(engine, user.username, now=now)
        raise SecurityKeyError("Signature de la clé refusée.") from exc
    with write_transaction(engine) as connection:
        connection.execute(
            update(SecurityKey)
            .where(SecurityKey.id == key.id)
            .values(sign_count=verified.new_sign_count, last_used_at=now)
        )
    return record_success(engine, user, password=None, method="cle", now=now)
