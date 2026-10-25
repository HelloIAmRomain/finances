"""Authentification forte (NF-SEC-04) et journal des connexions (NF-SEC-06).

- Mot de passe haché en argon2id ; second facteur au choix : code TOTP (secret chiffré en base)
  ou clé de sécurité FIDO2 / WebAuthn (`security_keys.py`).
- Réponse d'échec unique (« identifiants incorrects ») : rien n'indique quel facteur est faux.
- Anti-force brute : 5 échecs en 15 min pour un identifiant le bloquent 15 min. Le décompte vient
  du journal d'audit, donc il survit à un redémarrage.
- Session : jeton aléatoire (seule son empreinte est stockée), expiration après 12 h
  d'inactivité, et au plus 7 jours.
- Le jeton CSRF, dérivé de la session, relève de l'interface web (`app.web.csrf`).
"""

import hashlib
import hmac
import secrets
import threading
from datetime import datetime, timedelta
from typing import Any

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import Engine, func, insert, or_, select, update

from app.audit import audit
from app.db import inserted_id, write_transaction
from app.models import AppUser, AuditLog, UserSession
from app.models.auth import UserRole
from app.timeutil import ensure_utc

HASHER = PasswordHasher()
_ATTEMPTS = threading.Lock()  # un seul processus web (Dockerfile : uvicorn sans --workers)
IDLE_TIMEOUT = timedelta(hours=12)
ABSOLUTE_TIMEOUT = timedelta(days=7)
MAX_FAILURES = 5
LOCK_WINDOW = timedelta(minutes=15)
MIN_PASSWORD_LENGTH = 12
TOTP_ISSUER = "Finances"
TOTP_DRIFT_STEPS = (0, -1, 1)  # pas courant, puis précédent et suivant (30 s chacun)


class AuthError(Exception):
    """Identifiants incorrects (message volontairement générique)."""

    def __init__(self, message: str = "Identifiants incorrects.") -> None:
        super().__init__(message)


class LockedError(AuthError):
    def __init__(self) -> None:
        super().__init__("Trop d'essais : réessayer dans 15 minutes.")


def _audit(
    engine: Engine, action: str, *, now: datetime, user_id: int | None, details: dict[str, str]
) -> None:
    with write_transaction(engine) as connection:
        audit(
            connection,
            action,
            ("app_user", user_id),
            now=now,
            actor_user_id=user_id,
            details=details,
        )


def create_user(
    engine: Engine, *, username: str, password: str, now: datetime, role: UserRole = UserRole.ADMIN
) -> str:
    """Crée un utilisateur ; renvoie son secret TOTP (à saisir dans l'application TOTP)."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"mot de passe trop court ({MIN_PASSWORD_LENGTH} caractères au moins)")
    totp_secret = pyotp.random_base32()
    with write_transaction(engine) as connection:
        if connection.execute(select(AppUser.id).where(AppUser.username == username)).first():
            raise ValueError(f"l'utilisateur {username!r} existe déjà")
        result = connection.execute(
            insert(AppUser).values(
                username=username,
                password_hash=HASHER.hash(password),
                totp_secret_enc=totp_secret,
                role=role,
                created_at=now,
            )
        )
        user_id = inserted_id(result)
        audit(
            connection,
            "user.created",
            ("app_user", user_id),
            now=now,
            actor_user_id=None,
            details={"username": username, "role": str(role)},
        )
    return totp_secret


def totp_uri(username: str, totp_secret: str) -> str:
    return pyotp.TOTP(totp_secret).provisioning_uri(name=username, issuer_name=TOTP_ISSUER)


def _recent_failures(engine: Engine, username: str, now: datetime) -> int:
    with engine.connect() as connection:
        return int(
            connection.execute(
                select(func.count()).where(
                    AuditLog.action == "login.failed",
                    AuditLog.at > now - LOCK_WINDOW,
                    func.json_extract(AuditLog.details, "$.username") == username,
                )
            ).scalar_one()
        )


def check_password(engine: Engine, *, username: str, password: str, now: datetime) -> Any:
    """Premier facteur, commun au code TOTP et à la clé de sécurité : renvoie l'utilisateur, ou
    lève AuthError (échec journalisé, pris en compte par l'anti-force brute).

    Un essai à la fois : sans ce verrou, des requêtes parallèles liraient toutes un compteur
    sous le seuil avant que la première ne soit enregistrée, et dépasseraient les 5 essais."""
    with _ATTEMPTS:
        return _check_password(engine, username=username, password=password, now=now)


def _check_password(engine: Engine, *, username: str, password: str, now: datetime) -> Any:
    if _recent_failures(engine, username, now) >= MAX_FAILURES:
        _audit(engine, "login.locked", now=now, user_id=None, details={"username": username})
        raise LockedError
    with engine.connect() as connection:
        user = connection.execute(select(AppUser).where(AppUser.username == username)).first()
    valid = False
    if user is not None:
        try:
            valid = HASHER.verify(user.password_hash, password)
        except (VerificationError, InvalidHashError):
            valid = False
    else:
        HASHER.hash(password)  # même coût qu'un vrai essai : pas d'indice sur l'existence du compte
    if not valid or user is None:
        record_failure(engine, username, now=now)
        raise AuthError
    return user


def confirm_password(engine: Engine, *, user_id: int, password: str, now: datetime) -> None:
    """Nouvelle saisie du mot de passe avant une action sensible faite depuis une session
    ouverte (appairer un téléphone, créer un accès IA) : une session volée ou laissée ouverte
    ne suffit pas. Lève AuthError, comptée par l'anti-force brute."""
    with engine.connect() as connection:
        username = connection.scalar(select(AppUser.username).where(AppUser.id == user_id))
    if username is None:
        raise AuthError
    check_password(engine, username=username, password=password, now=now)


def record_failure(engine: Engine, username: str, *, now: datetime) -> None:
    _audit(engine, "login.failed", now=now, user_id=None, details={"username": username})


def record_success(
    engine: Engine, user: Any, *, password: str | None, method: str, now: datetime
) -> int:
    values: dict[str, object] = {"last_login_at": now}
    if password is not None and HASHER.check_needs_rehash(user.password_hash):
        values["password_hash"] = HASHER.hash(password)
    with write_transaction(engine) as connection:
        connection.execute(update(AppUser).where(AppUser.id == user.id).values(**values))
    _audit(
        engine,
        "login.success",
        now=now,
        user_id=user.id,
        details={"username": user.username, "facteur": method},
    )
    return int(user.id)


def _totp_step(secret: str | None, code: str, now: datetime) -> int | None:
    """Pas de temps du code s'il est valide (pas courant, précédent ou suivant : dérive d'horloge
    du téléphone tolérée), sinon None."""
    if secret is None or not code.isdigit():
        return None
    totp = pyotp.TOTP(secret)
    current = totp.timecode(now)
    for offset in TOTP_DRIFT_STEPS:
        if hmac.compare_digest(totp.at(now, counter_offset=offset), code):
            return current + offset
    return None


def _consume_totp_step(engine: Engine, user_id: int, step: int) -> bool:
    """Retient le pas utilisé ; faux si ce code (ou un plus récent) a déjà servi : un code vu
    par-dessus l'épaule ne rouvre pas de session dans sa minute et demie de validité."""
    with write_transaction(engine) as connection:
        consumed = connection.execute(
            update(AppUser)
            .where(
                AppUser.id == user_id,
                or_(AppUser.last_totp_step.is_(None), AppUser.last_totp_step < step),
            )
            .values(last_totp_step=step)
        )
    return bool(consumed.rowcount)


def authenticate(
    engine: Engine, *, username: str, password: str, totp_code: str, now: datetime
) -> int:
    """Mot de passe et code TOTP ; renvoie l'identifiant de l'utilisateur ou lève AuthError."""
    user = check_password(engine, username=username, password=password, now=now)
    step = _totp_step(user.totp_secret_enc, totp_code.strip(), now)
    if step is None or not _consume_totp_step(engine, user.id, step):
        record_failure(engine, username, now=now)
        raise AuthError
    return record_success(engine, user, password=password, method="totp", now=now)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def open_session(engine: Engine, user_id: int, *, now: datetime) -> str:
    token = secrets.token_urlsafe(32)
    with write_transaction(engine) as connection:
        connection.execute(
            insert(UserSession).values(
                user_id=user_id,
                token_hash=_token_hash(token),
                created_at=now,
                last_seen_at=now,
                expires_at=now + ABSOLUTE_TIMEOUT,
            )
        )
    return token


def session_user(engine: Engine, token: str | None, *, now: datetime) -> int | None:
    """Utilisateur de la session si elle est valide (et la prolonge), sinon None."""
    if not token:
        return None
    with write_transaction(engine) as connection:
        row = connection.execute(
            select(UserSession).where(UserSession.token_hash == _token_hash(token))
        ).first()
        if (
            row is None
            or row.revoked_at is not None
            or now >= ensure_utc(row.expires_at)
            or now - ensure_utc(row.last_seen_at) > IDLE_TIMEOUT
        ):
            return None
        connection.execute(
            update(UserSession).where(UserSession.id == row.id).values(last_seen_at=now)
        )
        return int(row.user_id)


def close_session(engine: Engine, token: str | None, *, now: datetime) -> None:
    if not token:
        return
    with write_transaction(engine) as connection:
        row = connection.execute(
            select(UserSession.id, UserSession.user_id).where(
                UserSession.token_hash == _token_hash(token)
            )
        ).first()
        if row is None:
            return
        connection.execute(
            update(UserSession).where(UserSession.id == row.id).values(revoked_at=now)
        )
    _audit(engine, "logout", now=now, user_id=int(row.user_id), details={})


def close_all_sessions(engine: Engine, *, now: datetime) -> int:
    """Ferme toutes les sessions ouvertes (machine volée, mot de passe exposé) ; audité."""
    with write_transaction(engine) as connection:
        closed = connection.execute(
            update(UserSession).where(UserSession.revoked_at.is_(None)).values(revoked_at=now)
        ).rowcount
        audit(
            connection,
            "user.sessions_closed",
            ("app_user", None),
            now=now,
            actor_user_id=None,
            details={"sessions": closed},
        )
    return int(closed)
