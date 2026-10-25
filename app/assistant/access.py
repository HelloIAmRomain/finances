"""Accès de l'IA (docs/spec-mcp.md §4) : interrupteurs, jetons à portée, quotas, journal d'appels.

- Deux interrupteurs, **désactivés par défaut** : « Accès IA » (tout refusé sinon) et
  « Modifications par l'IA » (outils d'écriture refusés sinon).
- Jeton aléatoire (256 bits) montré une seule fois ; seule son empreinte est en base. Portée
  lecture, ou lecture + écriture. Création après confirmation du mot de passe ; révocation
  immédiate. Un jeton d'appareil Android n'est pas un jeton IA, et inversement.
- Quotas par jeton, comptés sur le journal d'audit : 60 appels par minute, 2 000 par jour.
"""

import hashlib
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Engine, func, insert, select, update

from app.audit import audit
from app.auth.users import AuthError, confirm_password
from app.db import inserted_id, write_transaction
from app.models import AiSetting, AiToken, AuditLog
from app.timeutil import ensure_utc

ENABLED = "acces"
WRITES = "ecriture"
TOKEN_PREFIX = "fia_"  # noqa: S105 (préfixe, pas un secret)
MAX_NAME = 64
PER_MINUTE = 60
PER_DAY = 2000
SEEN_THROTTLE = timedelta(minutes=1)


class AccessError(ValueError):
    """Opération refusée (message affichable)."""


@dataclass(frozen=True)
class TokenInfo:
    id: int
    user_id: int
    name: str
    can_write: bool


@dataclass(frozen=True)
class TokenRow:
    id: int
    name: str
    can_write: bool
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


@dataclass(frozen=True)
class CallRow:
    at: datetime
    tool: str
    ok: bool
    rows: int | None
    token_id: int | None


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# --- Interrupteurs --------------------------------------------------------------------------------


def switches(engine: Engine) -> dict[str, bool]:
    with engine.connect() as connection:
        stored: dict[str, Any] = dict(
            connection.execute(select(AiSetting.key, AiSetting.value)).all()
        )
    return {ENABLED: stored.get(ENABLED) is True, WRITES: stored.get(WRITES) is True}


def set_switch(engine: Engine, key: str, value: bool, *, actor_user_id: int, now: datetime) -> None:
    if key not in (ENABLED, WRITES):
        raise AccessError("Réglage inconnu.")
    with write_transaction(engine) as connection:
        if connection.scalar(select(AiSetting.key).where(AiSetting.key == key)) is None:
            connection.execute(insert(AiSetting).values(key=key, value=value, updated_at=now))
        else:
            connection.execute(
                update(AiSetting).where(AiSetting.key == key).values(value=value, updated_at=now)
            )
        audit(
            connection,
            "ai.switch",
            ("ai_setting", None),
            now=now,
            actor_user_id=actor_user_id,
            details={"reglage": key, "actif": value},
        )


# --- Jetons ---------------------------------------------------------------------------------------


def create_token(
    engine: Engine, *, user_id: int, password: str, name: str, can_write: bool, now: datetime
) -> str:
    """Renvoie le jeton, à copier dans l'extension du PC : il n'est plus jamais affiché."""
    try:
        confirm_password(engine, user_id=user_id, password=password, now=now)
    except AuthError as exc:
        raise AccessError(str(exc)) from exc
    label = " ".join(name.split())[:MAX_NAME] or "PC"
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    with write_transaction(engine) as connection:
        result = connection.execute(
            insert(AiToken).values(
                user_id=user_id,
                name=label,
                token_hash=_hash(token),
                can_write=can_write,
                created_at=now,
            )
        )
        token_id = inserted_id(result)
        audit(
            connection,
            "ai.token_created",
            ("ai_token", token_id),
            now=now,
            actor_user_id=user_id,
            details={"nom": label, "ecriture": can_write},
        )
    return token


def revoke_token(engine: Engine, token_id: int, *, actor_user_id: int, now: datetime) -> None:
    with write_transaction(engine) as connection:
        result = connection.execute(
            update(AiToken)
            .where(AiToken.id == token_id, AiToken.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        if result.rowcount != 1:
            raise AccessError("Jeton introuvable ou déjà révoqué.")
        audit(
            connection,
            "ai.token_revoked",
            ("ai_token", token_id),
            now=now,
            actor_user_id=actor_user_id,
        )


def tokens(engine: Engine, *, include_internal: bool = False) -> list[TokenRow]:
    """Jetons du client MCP du PC ; le jeton interne de l'assistant intégré est caché (il ne sert
    pas par Bearer et se coupe par les interrupteurs)."""
    hidden = [] if include_internal else [_hash(INTERNAL_TOKEN)]
    with engine.connect() as connection:
        rows = connection.execute(
            select(
                AiToken.id,
                AiToken.name,
                AiToken.can_write,
                AiToken.created_at,
                AiToken.last_used_at,
                AiToken.revoked_at,
            )
            .where(AiToken.token_hash.not_in(hidden))
            .order_by(AiToken.created_at.desc(), AiToken.id.desc())
        ).all()
    return [TokenRow(*row) for row in rows]


def authenticate(engine: Engine, token: str | None, *, now: datetime) -> TokenInfo | None:
    if not token or not token.startswith(TOKEN_PREFIX):
        return None
    with engine.connect() as connection:
        row = connection.execute(
            select(
                AiToken.id, AiToken.user_id, AiToken.name, AiToken.can_write, AiToken.last_used_at
            ).where(AiToken.token_hash == _hash(token), AiToken.revoked_at.is_(None))
        ).first()
    if row is None:
        return None
    if row.last_used_at is None or now - ensure_utc(row.last_used_at) > SEEN_THROTTLE:
        with write_transaction(engine) as connection:
            connection.execute(update(AiToken).where(AiToken.id == row.id).values(last_used_at=now))
    return TokenInfo(int(row.id), int(row.user_id), row.name, bool(row.can_write))


# --- Quotas et journal ----------------------------------------------------------------------------


def over_quota(engine: Engine, token_id: int, *, now: datetime) -> bool:
    calls = select(func.count()).where(
        AuditLog.action == "mcp.call", AuditLog.object_id == token_id
    )
    with engine.connect() as connection:
        minute = connection.scalar(calls.where(AuditLog.at > now - timedelta(minutes=1)))
        day = connection.scalar(calls.where(AuditLog.at > now - timedelta(days=1)))
    return int(minute or 0) >= PER_MINUTE or int(day or 0) >= PER_DAY


def record_call(
    engine: Engine,
    token: TokenInfo,
    *,
    tool: str,
    ok: bool,
    rows: int | None,
    duration_ms: int,
    details: dict[str, Any],
    now: datetime,
) -> None:
    """Un appel d'outil dans le journal d'audit (NF-SEC-06) ; jamais de texte cherché en clair."""
    with write_transaction(engine) as connection:
        audit(
            connection,
            "mcp.call",
            ("ai_token", token.id),
            now=now,
            actor_user_id=token.user_id,
            details={"outil": tool, "ok": ok, "lignes": rows, "ms": duration_ms, **details},
        )


def recent_calls(engine: Engine, limit: int = 50) -> list[CallRow]:
    with engine.connect() as connection:
        rows = connection.execute(
            select(AuditLog.at, AuditLog.details, AuditLog.object_id)
            .where(AuditLog.action == "mcp.call")
            .order_by(AuditLog.at.desc(), AuditLog.id.desc())
            .limit(limit)
        ).all()
    return [
        CallRow(
            at=at,
            tool=str(details.get("outil", "?")),
            ok=bool(details.get("ok")),
            rows=details.get("lignes"),
            token_id=token_id,
        )
        for at, details, token_id in rows
    ]


# --- Assistant intégré (phase B2) -----------------------------------------------------------------

PROVIDER, MODEL, MONTHLY_CAP, STREAM = "fournisseur", "modele", "plafond_mensuel", "en_direct"
DEFAULT_CAP = 2_000_000  # jetons de texte par mois, entrée et sortie comprises
INTERNAL_TOKEN = "interne:assistant-integre"  # noqa: S105 (sans préfixe fia_ : inutilisable par Bearer)


def assistant_settings(engine: Engine) -> dict[str, Any]:
    with engine.connect() as connection:
        stored: dict[str, Any] = dict(
            connection.execute(select(AiSetting.key, AiSetting.value)).all()
        )
    return {
        PROVIDER: stored.get(PROVIDER) or "anthropic",
        MODEL: stored.get(MODEL) or None,
        MONTHLY_CAP: int(stored.get(MONTHLY_CAP) or DEFAULT_CAP),
        STREAM: stored.get(STREAM) is not False,  # réponse en direct, activée par défaut
    }


def set_assistant_settings(
    engine: Engine,
    *,
    provider: str,
    model: str | None,
    cap: int,
    actor_user_id: int,
    now: datetime,
    stream: bool = True,
) -> None:
    values = {
        PROVIDER: provider,
        MODEL: (model or "").strip()[:100] or None,
        MONTHLY_CAP: cap,
        STREAM: stream,
    }
    with write_transaction(engine) as connection:
        for key, value in values.items():
            if connection.scalar(select(AiSetting.key).where(AiSetting.key == key)) is None:
                connection.execute(insert(AiSetting).values(key=key, value=value, updated_at=now))
            else:
                connection.execute(
                    update(AiSetting)
                    .where(AiSetting.key == key)
                    .values(value=value, updated_at=now)
                )
        audit(
            connection,
            "ai.assistant_settings",
            ("ai_setting", None),
            now=now,
            actor_user_id=actor_user_id,
            details={
                "fournisseur": provider,
                "modele": values[MODEL],
                "plafond": cap,
                "en_direct": stream,
            },
        )


def internal_token(engine: Engine, user_id: int, *, now: datetime) -> TokenInfo:
    """Jeton interne de l'assistant intégré (journal, confirmations) ; écriture selon
    l'interrupteur « Modifications par l'IA »."""
    digest = _hash(INTERNAL_TOKEN)
    with write_transaction(engine) as connection:
        row = connection.execute(select(AiToken.id).where(AiToken.token_hash == digest)).first()
        if row is None:
            result = connection.execute(
                insert(AiToken).values(
                    user_id=user_id,
                    name="Assistant intégré",
                    token_hash=digest,
                    can_write=True,
                    created_at=now,
                )
            )
            token_id = inserted_id(result)
        else:
            token_id = int(row.id)
    return TokenInfo(token_id, user_id, "Assistant intégré", True)


def tokens_this_month(engine: Engine, *, now: datetime) -> int:
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    with engine.connect() as connection:
        rows: Sequence[dict[str, Any]] = connection.scalars(
            select(AuditLog.details).where(
                # Assistant et catégorisation automatique (ANA-02) partagent le plafond.
                AuditLog.action.in_(("assistant.message", "categorize.ai")),
                AuditLog.at >= start,
            )
        ).all()
    return sum(int(details.get("jetons", 0)) for details in rows)


def revoke_all_tokens(engine: Engine, *, now: datetime) -> int:
    """Retire tous les accès IA (client MCP du PC…), sauf le jeton interne de l'assistant intégré,
    qui n'est pas un secret (inutilisable de l'extérieur) ; audité."""
    with write_transaction(engine) as connection:
        revoked = connection.execute(
            update(AiToken)
            .where(AiToken.revoked_at.is_(None), AiToken.token_hash != _hash(INTERNAL_TOKEN))
            .values(revoked_at=now)
        ).rowcount
        audit(
            connection,
            "ai.tokens_revoked_all",
            ("ai_token", None),
            now=now,
            actor_user_id=None,
            details={"acces": revoked},
        )
    return int(revoked)
