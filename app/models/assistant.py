"""Pilotage par IA via MCP (docs/spec-mcp.md) : jetons, réglages, noms à abréger, modifications."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.db_types import EncryptedString, UTCDateTime
from app.models._common import created_at_column


class AiToken(Base):
    """Jeton d'accès du client MCP du PC : empreinte seulement, portée lecture ou écriture."""

    __tablename__ = "ai_token"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(64))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    can_write: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = created_at_column()
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class AiSetting(Base):
    """Interrupteurs « Accès IA » et « Modifications par l'IA » (désactivés par défaut)."""

    __tablename__ = "ai_setting"

    key: Mapped[str] = mapped_column(String(32), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)
    updated_at: Mapped[datetime] = created_at_column()


class AiTerm(Base):
    """Nom de personne à abréger, ou émetteur non personnel à garder en clair ; chiffré."""

    __tablename__ = "ai_term"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))  # « personne » ou « emetteur »
    value_enc: Mapped[str] = mapped_column(EncryptedString("ai_term.value"))
    value_hash: Mapped[str] = mapped_column(String(64), unique=True)  # index aveugle (doublons)
    created_at: Mapped[datetime] = created_at_column()


class AiChangeSet(Base):
    """Lot de modifications faites via l'IA : lignes avant / après (chiffrées), annulable."""

    __tablename__ = "ai_change_set"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_id: Mapped[int | None] = mapped_column(ForeignKey("ai_token.id", ondelete="SET NULL"))
    tool: Mapped[str] = mapped_column(String(64))
    summary: Mapped[str] = mapped_column(String(255))
    changes_enc: Mapped[str] = mapped_column(EncryptedString("ai_change_set.changes"))
    row_count: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = created_at_column()
    undone_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class AiConfirmation(Base):
    """Écriture de masse en attente : l'aperçu donne un code, valable 10 min, à usage unique."""

    __tablename__ = "ai_confirmation"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_id: Mapped[int] = mapped_column(ForeignKey("ai_token.id", ondelete="CASCADE"))
    tool: Mapped[str] = mapped_column(String(64))
    params_hash: Mapped[str] = mapped_column(String(64))
    code_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = created_at_column()
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class AssistantConversation(Base):
    """Conversation de l'assistant intégré : messages et appels d'outils, chiffrés (JSON)."""

    __tablename__ = "assistant_conversation"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id", ondelete="CASCADE"))
    messages_enc: Mapped[str] = mapped_column(EncryptedString("assistant_conversation.messages"))
    title: Mapped[str | None] = mapped_column(String(120))
    pinned_at: Mapped[datetime | None] = mapped_column(UTCDateTime)  # épinglée : en tête de liste
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = created_at_column()
