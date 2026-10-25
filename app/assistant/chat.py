"""Assistant intégré (phase B2) : conversation en langage naturel, depuis l'application Android
ou le web, avec les mêmes outils que le serveur MCP (lecture, écriture annulable).

- Un abonnement à une application d'IA ne s'utilise pas par API : le fournisseur (Anthropic,
  OpenAI, OVH) et sa clé sont choisis sur la page Sécurité ; plafond mensuel de jetons.
- Mêmes garde-fous que le MCP : interrupteurs, pseudonymisation et garde de sortie, aperçu puis
  confirmation des écritures de masse (la conversation garde le code), journal d'appels.
- Les messages (et les résultats d'outils) sont gardés chiffrés en base, par conversation.
"""

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy import Engine, insert, select, update
from sqlalchemy import delete as sa_delete

import app.assistant.catalog  # noqa: F401 (enregistrement des outils)
from app.assistant.access import (
    ENABLED,
    MODEL,
    MONTHLY_CAP,
    PROVIDER,
    STREAM,
    WRITES,
    assistant_settings,
    internal_token,
    switches,
    tokens_this_month,
)
from app.assistant.context import overview
from app.assistant.instructions import INSTRUCTIONS
from app.assistant.invoke import invoke
from app.assistant.providers import PROVIDERS, Completion, Exchange, ProviderError, complete
from app.assistant.pseudonym import Pseudonymizer
from app.assistant.terms import load_pseudonymizer
from app.assistant.tools import TOOLS, Context
from app.audit import audit
from app.config import Secrets
from app.db import inserted_id, write_transaction
from app.models import AssistantConversation

LOGGER = logging.getLogger(__name__)
MAX_ROUNDS = 8
MAX_TEXT = 2000
TITLE_LENGTH = 60
INTERRUPTED = "Pas de réponse (erreur ou interruption) : renvoyer la question."
KEPT_TURNS = 20
MAX_TOOL_RESULT = 12_000  # caractères (~3 000 jetons) par résultat d'outil
TRUNCATED = (
    "… [résultat tronqué : trop long. Préciser la demande : période plus courte, filtre, "
    "limite plus basse]"
)
PAST_RESULT = '{"note":"résultat d\'une question précédente, omis : rappeler l\'outil si besoin"}'
MOBILE = (
    "\nTu réponds en français, en Markdown simple (titres courts, listes, gras, tableaux de 4 "
    "colonnes au plus) : la réponse s'affiche aussi sur un téléphone. Sois précis et concis ; "
    "donne les montants en euros. Aujourd'hui : {today}."
)


class AssistantError(Exception):
    """Refus expliqué à l'utilisateur (accès coupé, clé absente, plafond atteint…)."""


@dataclass(frozen=True)
class Reply:
    conversation_id: int
    text: str
    tools: list[str]
    changes: list[str]


def _key(secrets: Secrets, provider: str) -> str:
    secret = getattr(secrets, PROVIDERS[provider]["secret"], None)
    if secret is None or not secret.get_secret_value().strip():
        raise AssistantError(
            f"Clé API absente pour {PROVIDERS[provider]['label']} : ajouter le secret "
            f"« {PROVIDERS[provider]['secret']} » (deploy/set-secret.sh) ou changer de "
            "fournisseur."
        )
    return str(secret.get_secret_value().strip())


def _load(engine: Engine, user_id: int, conversation_id: int | None) -> list[dict[str, Any]]:
    if conversation_id is None:
        return []
    with engine.connect() as connection:
        text = connection.scalar(
            select(AssistantConversation.messages_enc).where(
                AssistantConversation.id == conversation_id,
                AssistantConversation.user_id == user_id,
            )
        )
    if text is None:
        raise AssistantError("Conversation introuvable : en commencer une nouvelle.")
    messages: list[dict[str, Any]] = json.loads(text)
    return messages


def _save(
    engine: Engine,
    user_id: int,
    conversation_id: int | None,
    messages: list[dict[str, Any]],
    now: datetime,
) -> int:
    payload = json.dumps(messages, ensure_ascii=False)
    with write_transaction(engine) as connection:
        if conversation_id is None:
            first = next((m["text"] for m in messages if m["role"] == "user"), "")
            result = connection.execute(
                insert(AssistantConversation).values(
                    user_id=user_id,
                    messages_enc=payload,
                    title=_title(first),
                    created_at=now,
                    updated_at=now,
                )
            )
            return inserted_id(result)
        connection.execute(
            update(AssistantConversation)
            .where(AssistantConversation.id == conversation_id)
            .values(messages_enc=payload, updated_at=now)
        )
    return conversation_id


def _window(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ce qui est renvoyé au fournisseur à chaque appel (coût et rapidité) :
    - les `KEPT_TURNS` derniers tours, en coupant toujours avant une question ;
    - les résultats d'outils des **questions précédentes** remplacés par une mention courte :
      la réponse donnée alors les résume, et les renvoyer à chaque appel faisait dépasser
      800 000 jetons pour une seule question (constaté le 06/10/2026)."""
    starts = [i for i, m in enumerate(messages) if m["role"] == "user"]
    kept = messages[starts[-KEPT_TURNS] :] if len(starts) > KEPT_TURNS else messages
    last_question = max((i for i, m in enumerate(kept) if m["role"] == "user"), default=0)
    return [
        {**m, "content": PAST_RESULT} if m["role"] == "tool" and i < last_question else m
        for i, m in enumerate(kept)
    ]


def _masked(messages: list[dict[str, Any]], masker: Pseudonymizer) -> list[dict[str, Any]]:
    """Questions telles que l'IA les reçoit (inv. 8) : noms connus abrégés, IBAN et numéros
    masqués. Le texte saisi reste en clair dans l'historique affiché, jamais chez le fournisseur."""
    return [
        {**m, "text": masker.scrub(masker.text(m["text"]))} if m["role"] == "user" else m
        for m in messages
    ]


def _title(text: str) -> str:
    """Titre par défaut : début de la première question."""
    words = " ".join(text.split())
    if len(words) <= TITLE_LENGTH:
        return words
    cut = words[: TITLE_LENGTH - 1].rsplit(" ", 1)[0].rstrip(" ,;:")
    return cut + "…"


def history(engine: Engine, user_id: int, conversation_id: int) -> list[dict[str, Any]]:
    """Échanges visibles : chaque question, puis la réponse finale de l'assistant à ce tour et
    les outils qu'il a consultés (les textes intermédiaires, « je regarde… », sont omis)."""
    turns: list[dict[str, Any]] = []
    for message in _load(engine, user_id, conversation_id):
        if message["role"] == "user":
            turns.append({"role": "user", "text": message["text"]})
            turns.append({"role": "assistant", "text": "", "tools": []})
        elif turns and message["role"] == "assistant":
            if message.get("text"):
                turns[-1]["text"] = message["text"]
            turns[-1]["tools"] += [call["name"] for call in message.get("calls", [])]
    for turn in turns:  # question restée sans réponse : erreur, ou réponse en cours
        if turn["role"] == "assistant" and not turn["text"]:
            turn["text"], turn["interrupted"] = INTERRUPTED, True
    return turns


@dataclass(frozen=True)
class ConversationRow:
    id: int
    title: str
    created_at: datetime
    updated_at: datetime
    pinned: bool = False


def conversation(engine: Engine, user_id: int, conversation_id: int) -> ConversationRow:
    """Une conversation, lue directement (la liste s'arrête aux 100 plus récentes)."""
    with engine.connect() as connection:
        row = connection.execute(
            select(AssistantConversation).where(
                AssistantConversation.id == conversation_id,
                AssistantConversation.user_id == user_id,
            )
        ).first()
    if row is None:
        raise AssistantError("Conversation introuvable.")
    return ConversationRow(
        row.id,
        row.title or "Conversation",
        row.created_at,
        row.updated_at,
        pinned=row.pinned_at is not None,
    )


def conversations(engine: Engine, user_id: int, limit: int = 100) -> list[ConversationRow]:
    """Conversations : épinglées d'abord (dernière épinglée en tête), puis de la plus récente
    à la plus ancienne."""
    with engine.connect() as connection:
        rows = connection.execute(
            select(
                AssistantConversation.id,
                AssistantConversation.title,
                AssistantConversation.created_at,
                AssistantConversation.updated_at,
                AssistantConversation.pinned_at,
            )
            .where(AssistantConversation.user_id == user_id)
            .order_by(
                AssistantConversation.pinned_at.is_(None),
                AssistantConversation.pinned_at.desc(),
                AssistantConversation.updated_at.desc(),
                AssistantConversation.id.desc(),
            )
            .limit(limit)
        ).all()
    return [
        ConversationRow(
            row.id,
            row.title or "Conversation",
            row.created_at,
            row.updated_at,
            pinned=row.pinned_at is not None,
        )
        for row in rows
    ]


def pin(engine: Engine, user_id: int, conversation_id: int, pinned: bool, *, now: datetime) -> None:
    """Épingle (en tête de liste) ou désépingle une conversation."""
    with write_transaction(engine) as connection:
        result = connection.execute(
            update(AssistantConversation)
            .where(
                AssistantConversation.id == conversation_id,
                AssistantConversation.user_id == user_id,
            )
            .values(pinned_at=now if pinned else None)
        )
        if result.rowcount != 1:
            raise AssistantError("Conversation introuvable.")
        audit(
            connection,
            "assistant.pinned" if pinned else "assistant.unpinned",
            ("assistant_conversation", conversation_id),
            now=now,
            actor_user_id=user_id,
        )


def rename(
    engine: Engine, user_id: int, conversation_id: int, title: str, *, now: datetime
) -> None:
    cleaned = " ".join(title.split())[:TITLE_LENGTH]
    if not cleaned:
        raise AssistantError("Titre vide.")
    with write_transaction(engine) as connection:
        result = connection.execute(
            update(AssistantConversation)
            .where(
                AssistantConversation.id == conversation_id,
                AssistantConversation.user_id == user_id,
            )
            .values(title=cleaned)
        )
        if result.rowcount != 1:
            raise AssistantError("Conversation introuvable.")
        audit(
            connection,
            "assistant.renamed",
            ("assistant_conversation", conversation_id),
            now=now,
            actor_user_id=user_id,
        )


def delete(engine: Engine, user_id: int, conversation_id: int, *, now: datetime) -> None:
    """Suppression définitive (le contenu chiffré disparaît ; le journal garde la trace)."""
    with write_transaction(engine) as connection:
        result = connection.execute(
            sa_delete(AssistantConversation).where(
                AssistantConversation.id == conversation_id,
                AssistantConversation.user_id == user_id,
            )
        )
        if result.rowcount != 1:
            raise AssistantError("Conversation introuvable.")
        audit(
            connection,
            "assistant.deleted",
            ("assistant_conversation", conversation_id),
            now=now,
            actor_user_id=user_id,
        )


Report = Callable[[str, str], None]


def _ignore(kind: str, text: str) -> None:
    """Pas d'écoute de la progression (appel synchrone)."""


@dataclass(frozen=True)
class Started:
    """Question enregistrée, prête à être traitée (éventuellement en arrière-plan)."""

    conversation_id: int
    provider: str
    key: str
    model: str
    state: dict[str, bool]
    stream: bool


def begin(
    engine: Engine,
    secrets: Secrets,
    *,
    user_id: int,
    conversation_id: int | None,
    text: str,
    now: datetime,
) -> Started:
    """Contrôles (accès, clé, plafond) puis enregistrement immédiat de la question : la
    conversation existe et s'affiche avant même que l'IA réponde."""
    question = " ".join(text.split())[:MAX_TEXT]
    if not question:
        raise AssistantError("Message vide.")
    state = switches(engine)
    if not state[ENABLED]:
        raise AssistantError("Accès IA désactivé (page Sécurité de l'application).")
    settings = assistant_settings(engine)
    provider = str(settings[PROVIDER])
    if provider not in PROVIDERS:
        raise AssistantError("Fournisseur d'IA inconnu : le choisir sur la page Sécurité.")
    key = _key(secrets, provider)
    model = str(settings[MODEL] or PROVIDERS[provider]["model"])
    if tokens_this_month(engine, now=now) >= int(settings[MONTHLY_CAP]):
        raise AssistantError("Plafond mensuel de l'assistant atteint (page Sécurité).")
    messages = _load(engine, user_id, conversation_id)
    messages.append({"role": "user", "text": question})
    saved = _save(engine, user_id, conversation_id, messages, now)
    return Started(saved, provider, key, model, state, bool(settings[STREAM]))


def run(
    engine: Engine,
    started: Started,
    *,
    user_id: int,
    now: datetime,
    transport: httpx.BaseTransport | None = None,
    report: Report = _ignore,
) -> Reply:
    """Boucle fournisseur ↔ outils. `report(type, texte)` signale la progression : « etape »
    avant chaque outil, « texte » pour chaque morceau de réponse reçu en direct."""
    provider, model = started.provider, started.model
    messages = _load(engine, user_id, started.conversation_id)
    token = internal_token(engine, user_id, now=now)
    context = Context(
        engine=engine,
        token=token,
        now=now,
        masker=load_pseudonymizer(engine),
        switches=started.state,
    )
    writes = started.state.get(WRITES, False)
    tools = [t.descriptor() for t in TOOLS.values() if writes or not t.write]
    system = (
        INSTRUCTIONS
        + MOBILE.format(today=context.today.isoformat())
        + "\n\n# Situation actuelle (données pseudonymisées)\n\n"
        + _overview(context)
    )
    used: list[str] = []
    changes: list[str] = []
    spent = 0
    answer = "Je n'ai pas pu terminer : reformuler ou découper la demande."
    on_text = (lambda delta: report("texte", delta)) if started.stream else None
    try:
        for _ in range(MAX_ROUNDS):
            exchange = Exchange(
                started.key, model, system, _window(_masked(messages, context.masker)), tools
            )
            completion = complete(provider, exchange, transport, on_text=on_text)
            spent += completion.tokens
            if not completion.calls and not completion.text:
                completion = Completion("(réponse vide)")  # un tour vide serait refusé ensuite
            messages.append(
                {
                    "role": "assistant",
                    "text": completion.text,
                    "calls": [
                        {"id": c.id, "name": c.name, "arguments": c.arguments}
                        for c in completion.calls
                    ],
                }
            )
            if not completion.calls:
                answer = completion.text
                break
            for call in completion.calls:
                report("etape", _step_label(call.name))
                content = _run_tool(context, call.name, call.arguments, changes)
                used.append(call.name)
                messages.append(
                    {"role": "tool", "id": call.id, "name": call.name, "content": content}
                )
        else:  # tours épuisés : on clôt par un texte, pour que la suite reste valide
            messages.append({"role": "assistant", "text": answer, "calls": []})
    except ProviderError as exc:
        raise AssistantError(str(exc)) from exc
    finally:
        with write_transaction(engine) as connection:
            audit(
                connection,
                "assistant.message",
                ("assistant_conversation", started.conversation_id),
                now=now,
                actor_user_id=user_id,
                details={
                    "fournisseur": provider,
                    "modele": model,
                    "jetons": spent,
                    "outils": len(used),
                },
            )
    _save(engine, user_id, started.conversation_id, messages, now)
    return Reply(started.conversation_id, answer, used, changes)


def send(
    engine: Engine,
    secrets: Secrets,
    *,
    user_id: int,
    conversation_id: int | None,
    text: str,
    now: datetime,
    transport: httpx.BaseTransport | None = None,
) -> Reply:
    """Échange complet, en attendant la réponse (API v1 historique, tests)."""
    started = begin(
        engine, secrets, user_id=user_id, conversation_id=conversation_id, text=text, now=now
    )
    return run(engine, started, user_id=user_id, now=now, transport=transport)


def _step_label(name: str) -> str:
    """Libellé d'étape montré pendant que l'IA travaille (jamais de donnée)."""
    found = TOOLS.get(name)
    if found is None:
        return "Consulte un outil inconnu"
    return f"{'Modifie' if found.write else 'Consulte'} : {found.title}"


def _overview(context: Context) -> str:
    """Vue d'ensemble ; si elle échoue, l'échange continue sans elle (l'IA a l'outil)."""
    try:
        return overview(context)
    except Exception as exc:  # panne d'un outil : contexte réduit, pas d'échec
        LOGGER.error("vue d'ensemble indisponible : %s", type(exc).__name__)
        return "(vue d'ensemble indisponible : appeler l'outil « vue_ensemble »)"


def _run_tool(context: Context, name: str, arguments: dict[str, Any], changes: list[str]) -> str:
    outcome = invoke(context, name, arguments, via="assistant")
    if outcome.result is None:
        return json.dumps({"erreur": outcome.error}, ensure_ascii=False)
    if outcome.result.get("modification"):
        changes.append(str(outcome.result["modification"]))
    content = json.dumps(outcome.result, ensure_ascii=False, separators=(",", ":"))
    if len(content) > MAX_TOOL_RESULT:
        content = content[:MAX_TOOL_RESULT] + TRUNCATED
    return content
