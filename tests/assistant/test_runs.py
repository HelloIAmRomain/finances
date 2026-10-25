"""Tours de l'assistant en arrière-plan : registre, étapes, texte en direct, interruptions."""

import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.assistant.access import ENABLED, set_switch
from app.assistant.chat import AssistantError, Reply, begin, history, run
from app.assistant.runs import DONE, FAILED, RUNNING, RunBusyError, RunRegistry
from app.config import Secrets
from tests.assistant.conftest import NOW, Demo
from tests.executors import InlineExecutor, ManualExecutor


def _secrets() -> Secrets:
    values: dict[str, Any] = {"anthropic_api_key": SecretStr("cle")}
    return Secrets.model_construct(**values)


def _sse(*events: dict[str, Any]) -> httpx.Response:
    body = "".join(f"data: {json.dumps(e)}\n\n" for e in events)
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())


def _script(request: httpx.Request) -> httpx.Response:
    """Premier appel : un outil ; second appel : la réponse, en deux morceaux."""
    body = json.loads(request.content)
    if not any(isinstance(m["content"], list) for m in body["messages"]):
        return _sse(
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "Je regarde."},
            },
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "tool_use", "id": "t", "name": "comptes"},
            },
        )
    return _sse(
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "**Trois** "},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "comptes."},
        },
    )


def test_run_reports_steps_and_live_text(demo: Demo) -> None:
    set_switch(demo.engine, ENABLED, True, actor_user_id=demo.user_id, now=NOW)
    started = begin(
        demo.engine,
        _secrets(),
        user_id=demo.user_id,
        conversation_id=None,
        text="Mes comptes ?",
        now=NOW,
    )
    # La question est déjà enregistrée, avant toute réponse.
    assert history(demo.engine, demo.user_id, started.conversation_id)[0]["text"] == "Mes comptes ?"
    events: list[tuple[str, str]] = []
    reply = run(
        demo.engine,
        started,
        user_id=demo.user_id,
        now=NOW,
        transport=httpx.MockTransport(_script),
        report=lambda k, t: events.append((k, t)),
    )
    assert reply.text == "**Trois** comptes." and reply.tools == ["comptes"]
    assert events == [
        ("texte", "Je regarde."),
        ("etape", "Consulte : Comptes"),
        ("texte", "**Trois** "),
        ("texte", "comptes."),
    ]


def test_unanswered_question_is_marked_interrupted(demo: Demo) -> None:
    set_switch(demo.engine, ENABLED, True, actor_user_id=demo.user_id, now=NOW)
    started = begin(
        demo.engine, _secrets(), user_id=demo.user_id, conversation_id=None, text="Bonjour", now=NOW
    )
    turns = history(demo.engine, demo.user_id, started.conversation_id)
    assert turns[1]["interrupted"] is True and "renvoyer la question" in turns[1]["text"]


def _reply(text: str) -> Reply:
    return Reply(7, text, ["comptes"], [])


def test_registry_lifecycle() -> None:
    clock = [1_000_000_000_000]
    executor = ManualExecutor()
    registry = RunRegistry(executor, clock=lambda: clock[0], keep_seconds=60)

    def work(report: Any) -> Reply:
        report("etape", "Consulte : Comptes")
        report("texte", "Rép")
        report("texte", "onse")
        return _reply("Réponse")

    run_id = registry.start(user_id=1, conversation_id=7, work=work)
    view = registry.view(run_id, 1)
    assert view is not None and view.status == RUNNING and view.events == []
    with pytest.raises(RunBusyError):
        registry.start(user_id=1, conversation_id=7, work=work)
    assert registry.active(1, 7) == run_id and registry.active(1, 8) is None
    assert registry.view(run_id, 2) is None  # un autre utilisateur ne voit rien
    executor.run_all()
    done = registry.view(run_id, 1)
    assert done is not None and done.status == DONE and done.partial == "Réponse"
    assert [e["type"] for e in done.events] == ["etape", "fin"]
    later = registry.view(run_id, 1, since=1)
    assert later is not None and [e["type"] for e in later.events] == ["fin"]
    assert registry.active(1, 7) is None
    registry.start(user_id=1, conversation_id=9, work=work)  # nouvelle réponse possible
    clock[0] += 61 * 1_000_000_000
    registry.start(user_id=1, conversation_id=10, work=work)  # purge des tours terminés
    assert registry.view(run_id, 1) is None
    registry.shutdown()


def test_registry_reports_errors() -> None:
    registry = RunRegistry(InlineExecutor())

    def refused(report: Any) -> Reply:
        raise AssistantError("Plafond mensuel atteint.")

    def broken(report: Any) -> Reply:
        raise RuntimeError("détail interne")

    first = registry.view(registry.start(user_id=1, conversation_id=1, work=refused), 1)
    assert (
        first is not None and first.status == FAILED and first.error == "Plafond mensuel atteint."
    )
    second = registry.view(registry.start(user_id=1, conversation_id=2, work=broken), 1)
    assert second is not None and second.error == "Erreur interne de l'assistant : réessayer."
    assert [e["type"] for e in second.events] == ["erreur"]
