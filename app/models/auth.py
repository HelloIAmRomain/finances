"""Utilisateurs et sessions (NF-SEC-04)."""

import enum
from datetime import datetime

from sqlalchemy import ForeignKey, Integer, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.db_types import EncryptedString, UTCDateTime
from app.models._common import created_at_column, text_enum


class UserRole(enum.StrEnum):
    ADMIN = "admin"
    CO_OWNER = "co_owner"  # UI-04 (lot 4)


class AppUser(Base):
    __tablename__ = "app_user"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    totp_secret_enc: Mapped[str | None] = mapped_column(EncryptedString("app_user.totp_secret"))
    role: Mapped[UserRole] = mapped_column(text_enum(UserRole, "user_role"))
    created_at: Mapped[datetime] = created_at_column()
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # Pas de temps (30 s) du dernier code TOTP accepté : un code ne sert qu'une fois.
    last_totp_step: Mapped[int | None] = mapped_column(Integer)


class UserSession(Base):
    __tablename__ = "user_session"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = created_at_column()
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class LinkNonce(Base):
    """Nonce d'une demande de liaison bancaire (T1.12) : le retour de la banque n'est accepté
    qu'une fois. On garde l'empreinte du `state`, jamais sa valeur."""

    __tablename__ = "link_nonce"

    nonce_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime] = created_at_column()
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class Device(Base):
    """Appareil appairé (application Android) : jeton propre, révocable, jamais stocké en clair."""

    __tablename__ = "device"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(64))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = created_at_column()
    last_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class PairingCode(Base):
    """Code d'appairage à usage unique, court (10 min) et limité en essais."""

    __tablename__ = "pairing_code"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id", ondelete="CASCADE"))
    code_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = created_at_column()
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class SecurityKey(Base):
    """Clé de sécurité FIDO2 / WebAuthn (YubiKey…), second facteur au choix avec le TOTP.
    Seule la clé publique est stockée ; la clé privée ne quitte jamais l'appareil."""

    __tablename__ = "security_key"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id", ondelete="CASCADE"))
    credential_id: Mapped[str] = mapped_column(String(512), unique=True)  # base64url
    public_key: Mapped[bytes] = mapped_column(LargeBinary)  # COSE
    sign_count: Mapped[int] = mapped_column(Integer, default=0)
    name: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = created_at_column()
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class WebAuthnChallenge(Base):
    """Défi WebAuthn à usage unique (5 min) : empreinte seulement, invalidé à la première
    vérification (rejeu impossible)."""

    __tablename__ = "webauthn_challenge"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id", ondelete="CASCADE"))
    purpose: Mapped[str] = mapped_column(String(16))  # « inscription » ou « connexion »
    challenge_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = created_at_column()
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
