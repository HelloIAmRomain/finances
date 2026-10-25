"""Lancement et suivi d'un tour de l'assistant, communs à la page web et à l'API v1."""

from typing import Any

from fastapi import Request

from app.assistant.chat import begin, conversations, run
from app.assistant.runs import RunBusyError, RunRegistry, RunView
from app.web.templating import markdown


def start_turn(
    request: Request, *, user_id: int, conversation_id: int | None, text: str
) -> tuple[str, int, str]:
    """Enregistre la question (AssistantError si refusée), lance la réponse en arrière-plan ;
    renvoie (tour, conversation, titre). RunBusyError si une réponse est déjà en cours."""
    state = request.app.state
    registry: RunRegistry = state.assistant_runs
    if conversation_id is not None and registry.active(user_id, conversation_id):
        raise RunBusyError("Réponse en cours dans cette conversation : patienter.")
    engine, clock = state.engine, state.now
    started = begin(
        engine,
        state.secrets,
        user_id=user_id,
        conversation_id=conversation_id,
        text=text,
        now=clock(),
    )
    transport = getattr(state, "ai_transport", None)
    run_id = registry.start(
        user_id=user_id,
        conversation_id=started.conversation_id,
        work=lambda report: run(
            engine, started, user_id=user_id, now=clock(), transport=transport, report=report
        ),
    )
    title = next(
        (c.title for c in conversations(engine, user_id) if c.id == started.conversation_id), ""
    )
    return run_id, started.conversation_id, title


def turn_payload(view: RunView, *, html: bool = False) -> dict[str, Any]:
    reply = view.reply
    payload: dict[str, Any] = {
        "tour": view.id,
        "conversation": view.conversation_id,
        "etat": view.status,
        "evenements": view.events,
        "texte_partiel": view.partial,
        "reponse": reply.text if reply else None,
        "outils": reply.tools if reply else [],
        "modifications": reply.changes if reply else [],
        "erreur": view.error,
    }
    if html:
        payload["html"] = str(markdown(reply.text if reply else view.partial))
    return payload
