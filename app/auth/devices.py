"""Appareils appairés (application Android) : code à usage unique, jeton révocable (NF-SEC-04).

- L'administrateur, connecté (mot de passe + TOTP), demande un code d'appairage : 8 caractères
  sans ambiguïté (ni 0/O ni 1/I), valable 10 minutes, un seul actif à la fois, jamais stocké en
  clair.
- L'application envoie ce code et un nom d'appareil ; elle reçoit un jeton aléatoire (256 bits)
  qu'elle garde dans le magasin de clés Android. Seule son empreinte est en base.
- Anti-force brute : au-delà de 10 échecs en 10 minutes, tout appairage est refusé.
- Révocation immédiate depuis la page « Appareils » ; toute action est auditée.
- Un jeton inutilisé depuis 90 jours n'est plus accepté (téléphone perdu ou oublié) : il suffit
  d'appairer de nouveau. Le widget et l'application l'utilisent chaque heure, il n'expire donc
  pas tant que le téléphone sert.
"""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import Engine, func, insert, select, update

from app.audit import audit
from app.auth.users import AuthError, confirm_password
from app.db import inserted_id, write_transaction
from app.models import AuditLog, Device, PairingCode
from app.timeutil import ensure_utc

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8
CODE_LIFETIME = timedelta(minutes=10)
FAILURE_WINDOW = timedelta(minutes=10)
MAX_FAILURES = 10
SEEN_THROTTLE = timedelta(minutes=5)
IDLE_EXPIRY = timedelta(days=90)
MAX_NAME = 64


class PairingError(ValueError):
    """Appairage refusé."""


@dataclass(frozen=True)
class DeviceRow:
    id: int
    name: str
    created_at: datetime
    last_seen_at: datetime | None
    revoked_at: datetime | None

    def expired(self, now: datetime) -> bool:
        return self.revoked_at is None and _idle(self.last_seen_at or self.created_at, now)


@dataclass(frozen=True)
class Paired:
    device_id: int
    token: str


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _idle(last_seen_at: datetime, now: datetime) -> bool:
    return now - ensure_utc(last_seen_at) > IDLE_EXPIRY


def _normalize_code(code: str) -> str:
    return "".join(c for c in code.upper() if c in ALPHABET)


def create_pairing_code(engine: Engine, *, user_id: int, password: str, now: datetime) -> str:
    """Renvoie le code à afficher (« ABCD-EFGH ») ; les codes précédents non utilisés expirent.
    Mot de passe redemandé : le téléphone reçoit un accès durable à toutes les données."""
    try:
        confirm_password(engine, user_id=user_id, password=password, now=now)
    except AuthError as exc:
        raise PairingError(str(exc)) from exc
    code = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))
    with write_transaction(engine) as connection:
        connection.execute(
            update(PairingCode)
            .where(PairingCode.user_id == user_id, PairingCode.used_at.is_(None))
            .values(expires_at=now)
        )
        connection.execute(
            insert(PairingCode).values(
                user_id=user_id,
                code_hash=_hash(code),
                created_at=now,
                expires_at=now + CODE_LIFETIME,
            )
        )
        audit(
            connection, "device.pairing_code", ("app_user", user_id), now=now, actor_user_id=user_id
        )
    return f"{code[:4]}-{code[4:]}"


def pair(engine: Engine, *, code: str, device_name: str, now: datetime) -> Paired:
    name = " ".join(device_name.split())[:MAX_NAME] or "Téléphone"
    with write_transaction(engine) as connection:
        failures = connection.scalar(
            select(func.count()).where(
                AuditLog.action == "device.pairing_failed", AuditLog.at > now - FAILURE_WINDOW
            )
        )
        if int(failures or 0) >= MAX_FAILURES:
            raise PairingError("Trop d'essais : réessayer dans quelques minutes.")
        row = connection.execute(
            select(PairingCode).where(PairingCode.code_hash == _hash(_normalize_code(code)))
        ).first()
        if row is None or row.used_at is not None or ensure_utc(row.expires_at) <= now:
            audit(
                connection, "device.pairing_failed", ("device", None), now=now, actor_user_id=None
            )
            failed = True
        else:
            failed = False
            token = secrets.token_urlsafe(32)
            connection.execute(
                update(PairingCode).where(PairingCode.id == row.id).values(used_at=now)
            )
            result = connection.execute(
                insert(Device).values(
                    user_id=row.user_id,
                    name=name,
                    token_hash=_hash(token),
                    created_at=now,
                    last_seen_at=now,
                )
            )
            device_id = inserted_id(result)
            audit(
                connection,
                "device.paired",
                ("device", device_id),
                now=now,
                actor_user_id=row.user_id,
                details={"nom": name},
            )
    if failed:  # l'échec est journalisé (transaction validée) avant d'être signalé
        raise PairingError("Code inconnu, déjà utilisé ou expiré.")
    return Paired(device_id, token)


def device_user(engine: Engine, token: str | None, *, now: datetime) -> tuple[int, int] | None:
    """(appareil, utilisateur) pour un jeton valide, non révoqué et servi depuis moins de
    `IDLE_EXPIRY`, sinon None."""
    if not token:
        return None
    with engine.connect() as connection:
        row = connection.execute(
            select(Device.id, Device.user_id, Device.last_seen_at).where(
                Device.token_hash == _hash(token), Device.revoked_at.is_(None)
            )
        ).first()
    if row is None or (row.last_seen_at is not None and _idle(row.last_seen_at, now)):
        return None
    if row.last_seen_at is None or now - ensure_utc(row.last_seen_at) > SEEN_THROTTLE:
        with write_transaction(engine) as connection:
            connection.execute(update(Device).where(Device.id == row.id).values(last_seen_at=now))
    return int(row.id), int(row.user_id)


def revoke_device(engine: Engine, device_id: int, *, actor_user_id: int, now: datetime) -> None:
    with write_transaction(engine) as connection:
        result = connection.execute(
            update(Device)
            .where(Device.id == device_id, Device.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        if result.rowcount != 1:
            raise PairingError("Appareil introuvable ou déjà révoqué.")
        audit(
            connection,
            "device.revoked",
            ("device", device_id),
            now=now,
            actor_user_id=actor_user_id,
        )


def devices(engine: Engine) -> list[DeviceRow]:
    with engine.connect() as connection:
        return [
            DeviceRow(r.id, r.name, r.created_at, r.last_seen_at, r.revoked_at)
            for r in connection.execute(select(Device).order_by(Device.created_at.desc()))
        ]


def revoke_all_devices(engine: Engine, *, now: datetime) -> int:
    """Retire tous les téléphones appairés (il faudra les appairer de nouveau) ; audité."""
    with write_transaction(engine) as connection:
        revoked = connection.execute(
            update(Device).where(Device.revoked_at.is_(None)).values(revoked_at=now)
        ).rowcount
        audit(
            connection,
            "device.all_revoked",
            ("device", None),
            now=now,
            actor_user_id=None,
            details={"appareils": revoked},
        )
    return int(revoked)
