"""Assistant intégré (phase B2) : boucle d'appels d'outils avec des fournisseurs simulés
(httpx.MockTransport, aucun appel réseau), garde-fous et conservation des conversations."""

import json
from collections.abc import Callable, Sequence
from datetime import timedelta
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select

from app.assistant.access import ENABLED, WRITES, set_assistant_settings, set_switch
from app.assistant.chat import AssistantError, history, send
from app.config import Secrets
from app.models import AssistantConversation, AuditLog
from tests.assistant.conftest import NOW, Demo

Script = Callable[[dict[str, Any], int], dict[str, Any]]


def _secrets(**keys: str) -> Secrets:
    values: dict[str, Any] = {name: SecretStr(value) for name, value in keys.items()}
    return Secrets.model_construct(**values)


class FakeProvider:
    def __init__(self, script: Script, status: int = 200) -> None:
        self.script = script
        self.status = status
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = json.loads(request.content)
        return httpx.Response(self.status, json=self.script(body, len(self.requests)))

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def body(self, index: int) -> dict[str, Any]:
        return dict(json.loads(self.requests[index].content))


def _anthropic_script(body: dict[str, Any], turn: int) -> dict[str, Any]:
    if turn == 1:
        return {
            "content": [
                {"type": "text", "text": "Je regarde."},
                {
                    "type": "tool_use",
                    "id": "tu_1",
                    "name": "synthese_mensuelle",
                    "input": {"du": "2026-08", "au": "2026-08"},
                },
            ],
            "usage": {"input_tokens": 1000, "output_tokens": 50},
        }
    return {
        "content": [{"type": "text", "text": "En août : 2 500 € de revenus."}],
        "usage": {"input_tokens": 1200, "output_tokens": 30},
    }


def _enable(demo: Demo, writes: bool = False) -> None:
    set_switch(demo.engine, ENABLED, True, actor_user_id=demo.user_id, now=NOW)
    set_switch(demo.engine, WRITES, writes, actor_user_id=demo.user_id, now=NOW)


def test_anthropic_tool_loop(demo: Demo) -> None:
    _enable(demo)
    fake = FakeProvider(_anthropic_script)
    reply = send(
        demo.engine,
        _secrets(anthropic_api_key="cle-factice"),
        user_id=demo.user_id,
        conversation_id=None,
        text="Combien ai-je gagné en août ?",
        now=NOW,
        transport=fake.transport,
    )
    assert reply.text == "En août : 2 500 € de revenus." and reply.tools == ["synthese_mensuelle"]
    first, second = fake.body(0), fake.body(1)
    assert fake.requests[0].url == "https://api.anthropic.com/v1/messages"
    assert fake.requests[0].headers["x-api-key"] == "cle-factice"
    assert first["model"] == "claude-sonnet-5-5" and "JAMAIS" in first["system"]
    names = {t["name"] for t in first["tools"]}
    assert "comptes" in names and "categoriser" not in names  # écriture coupée
    result = second["messages"][-1]["content"][0]
    assert result["type"] == "tool_result" and result["tool_use_id"] == "tu_1"
    assert json.loads(result["content"])["mois"][0]["revenus"]["centimes"] == 250000
    assert history(demo.engine, demo.user_id, reply.conversation_id) == [
        {"role": "user", "text": "Combien ai-je gagné en août ?"},
        {
            "role": "assistant",
            "text": "En août : 2 500 € de revenus.",
            "tools": ["synthese_mensuelle"],
        },
    ]
    assert "# Situation actuelle" in first["system"] and "## Comptes" in first["system"]
    with demo.engine.connect() as connection:
        details: Sequence[dict[str, Any]] = connection.scalars(
            select(AuditLog.details).where(AuditLog.action == "assistant.message")
        ).all()
        raw: bytes = connection.exec_driver_sql(
            "SELECT messages_enc FROM assistant_conversation"
        ).scalar_one()
    assert details[0]["jetons"] == 2280 and details[0]["fournisseur"] == "anthropic"
    assert b"2026-08" not in raw  # conversation chiffrée


def test_openai_compatible_flow_with_write_and_follow_up(demo: Demo) -> None:
    _enable(demo, writes=True)
    set_assistant_settings(
        demo.engine,
        provider="ovh",
        model="Mistral-Test",
        cap=10**6,
        actor_user_id=demo.user_id,
        now=NOW,
    )
    state: dict[str, Any] = {}

    def script(body: dict[str, Any], turn: int) -> dict[str, Any]:
        if turn == 1:
            call = {
                "id": "c1",
                "type": "function",
                "function": {
                    "name": "rechercher_transactions",
                    "arguments": json.dumps({"texte": "UBER"}),
                },
            }
            return {
                "choices": [{"message": {"content": None, "tool_calls": [call]}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }
        if turn == 2:
            found = json.loads(body["messages"][-1]["content"])
            state["tx"] = found["transactions"][0]["id"]
            call = {
                "id": "c2",
                "type": "function",
                "function": {
                    "name": "categoriser",
                    "arguments": json.dumps(
                        {"transactions": [state["tx"]], "categorie": "Restaurants"}
                    ),
                },
            }
            return {"choices": [{"message": {"content": "", "tool_calls": [call]}}], "usage": {}}
        return {"choices": [{"message": {"content": "Classée en Restaurants."}}], "usage": {}}

    fake = FakeProvider(script)
    reply = send(
        demo.engine,
        _secrets(ovh_ai_api_key="cle-ovh"),
        user_id=demo.user_id,
        conversation_id=None,
        text="Range Uber Eats",
        now=NOW,
        transport=fake.transport,
    )
    assert (
        fake.requests[0].url == "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1/chat/completions"
    )
    assert fake.requests[0].headers["authorization"] == "Bearer cle-ovh"
    assert (
        fake.body(0)["model"] == "Mistral-Test" and fake.body(0)["messages"][0]["role"] == "system"
    )
    assert reply.tools == ["rechercher_transactions", "categoriser"] and len(reply.changes) == 1
    assert reply.changes[0].startswith("m_")
    # La suite de la conversation renvoie l'historique complet, appels d'outils compris.
    follow = FakeProvider(lambda body, turn: {"choices": [{"message": {"content": "D'accord."}}]})
    send(
        demo.engine,
        _secrets(ovh_ai_api_key="cle-ovh"),
        user_id=demo.user_id,
        conversation_id=reply.conversation_id,
        text="Merci",
        now=NOW,
        transport=follow.transport,
    )
    roles = [m["role"] for m in follow.body(0)["messages"]]
    assert roles == [
        "system",
        "user",
        "assistant",
        "tool",
        "assistant",
        "tool",
        "assistant",
        "user",
    ]


def test_refusals(demo: Demo) -> None:
    secrets = _secrets(anthropic_api_key="cle")
    with pytest.raises(AssistantError, match="désactivé"):
        send(
            demo.engine,
            secrets,
            user_id=demo.user_id,
            conversation_id=None,
            text="Bonjour",
            now=NOW,
        )
    _enable(demo)
    with pytest.raises(AssistantError, match="vide"):
        send(demo.engine, secrets, user_id=demo.user_id, conversation_id=None, text="  ", now=NOW)
    with pytest.raises(AssistantError, match="openai_api_key"):
        set_assistant_settings(
            demo.engine, provider="openai", model=None, cap=100, actor_user_id=demo.user_id, now=NOW
        )
        send(
            demo.engine,
            secrets,
            user_id=demo.user_id,
            conversation_id=None,
            text="Bonjour",
            now=NOW,
        )
    set_assistant_settings(
        demo.engine, provider="anthropic", model=None, cap=100, actor_user_id=demo.user_id, now=NOW
    )
    refused = FakeProvider(lambda body, turn: {"error": {}}, status=401)
    with pytest.raises(AssistantError, match="Clé API refusée"):
        send(
            demo.engine,
            secrets,
            user_id=demo.user_id,
            conversation_id=None,
            text="Bonjour",
            now=NOW,
            transport=refused.transport,
        )
    spend = FakeProvider(
        lambda body, turn: {
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 100},
        }
    )
    send(
        demo.engine,
        secrets,
        user_id=demo.user_id,
        conversation_id=None,
        text="Bonjour",
        now=NOW,
        transport=spend.transport,
    )
    with pytest.raises(AssistantError, match="Plafond"):
        send(
            demo.engine,
            secrets,
            user_id=demo.user_id,
            conversation_id=None,
            text="Encore",
            now=NOW,
            transport=spend.transport,
        )
    with pytest.raises(AssistantError, match="introuvable"):
        history(demo.engine, demo.user_id, 999)


def test_tool_errors_are_returned_to_the_model(demo: Demo) -> None:
    _enable(demo)

    def script(body: dict[str, Any], turn: int) -> dict[str, Any]:
        if turn <= 2:  # outil d'écriture alors que l'écriture est coupée, puis outil inconnu
            name = "categoriser" if turn == 1 else "inexistant"
            return {"content": [{"type": "tool_use", "id": f"t{turn}", "name": name, "input": {}}]}
        return {"content": [{"type": "text", "text": "Impossible."}]}

    fake = FakeProvider(script)
    reply = send(
        demo.engine,
        _secrets(anthropic_api_key="cle"),
        user_id=demo.user_id,
        conversation_id=None,
        text="Classe tout",
        now=NOW,
        transport=fake.transport,
    )
    assert reply.text == "Impossible."
    errors = [
        json.loads(m["content"][0]["content"])["erreur"]
        for m in fake.body(2)["messages"]
        if m["role"] == "user" and isinstance(m["content"], list)
    ]
    assert "désactivées" in errors[0] and "inconnu" in errors[1]


def test_endless_tool_loop_is_cut(demo: Demo) -> None:
    _enable(demo)
    fake = FakeProvider(
        lambda body, turn: {
            "content": [{"type": "tool_use", "id": f"t{turn}", "name": "comptes", "input": {}}]
        }
    )
    reply = send(
        demo.engine,
        _secrets(anthropic_api_key="cle"),
        user_id=demo.user_id,
        conversation_id=None,
        text="Boucle",
        now=NOW,
        transport=fake.transport,
    )
    assert len(fake.requests) == 8 and "pas pu terminer" in reply.text
    with demo.engine.connect() as connection:
        assert connection.scalar(select(AssistantConversation.id)) == reply.conversation_id


def test_empty_answer_then_follow_up_stays_valid(demo: Demo) -> None:
    _enable(demo)
    empty = FakeProvider(lambda body, turn: {"content": [], "usage": {}})
    secrets = _secrets(anthropic_api_key="cle")
    reply = send(
        demo.engine,
        secrets,
        user_id=demo.user_id,
        conversation_id=None,
        text="?",
        now=NOW,
        transport=empty.transport,
    )
    assert reply.text == "(réponse vide)"
    follow = FakeProvider(lambda body, turn: {"content": [{"type": "text", "text": "ok"}]})
    send(
        demo.engine,
        secrets,
        user_id=demo.user_id,
        conversation_id=reply.conversation_id,
        text="Encore",
        now=NOW,
        transport=follow.transport,
    )
    assistant_turns = [m for m in follow.body(0)["messages"] if m["role"] == "assistant"]
    assert assistant_turns == [
        {"role": "assistant", "content": [{"type": "text", "text": "(réponse vide)"}]}
    ]


def test_exhausted_loop_is_closed_by_a_text(demo: Demo) -> None:
    _enable(demo)
    secrets = _secrets(anthropic_api_key="cle")
    loop = FakeProvider(
        lambda body, turn: {
            "content": [{"type": "tool_use", "id": f"t{turn}", "name": "comptes", "input": {}}]
        }
    )
    reply = send(
        demo.engine,
        secrets,
        user_id=demo.user_id,
        conversation_id=None,
        text="Boucle",
        now=NOW,
        transport=loop.transport,
    )
    follow = FakeProvider(lambda body, turn: {"content": [{"type": "text", "text": "ok"}]})
    send(
        demo.engine,
        secrets,
        user_id=demo.user_id,
        conversation_id=reply.conversation_id,
        text="Et alors ?",
        now=NOW,
        transport=follow.transport,
    )
    messages = follow.body(0)["messages"]
    assert (
        messages[-2]["role"] == "assistant"
        and "pas pu terminer" in messages[-2]["content"][0]["text"]
    )


def test_tool_crash_does_not_break_the_conversation(
    demo: Demo, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace  # noqa: PLC0415

    from app.assistant.tools import TOOLS  # noqa: PLC0415

    def broken(context: Any, params: Any) -> dict[str, Any]:
        raise RuntimeError("panne")

    monkeypatch.setitem(TOOLS, "comptes", replace(TOOLS["comptes"], run=broken))
    _enable(demo)
    fake = FakeProvider(
        lambda body, turn: (
            {"content": [{"type": "tool_use", "id": "t", "name": "comptes", "input": {}}]}
            if turn == 1
            else {"content": [{"type": "text", "text": "Désolé."}]}
        )
    )
    reply = send(
        demo.engine,
        _secrets(anthropic_api_key="cle"),
        user_id=demo.user_id,
        conversation_id=None,
        text="Mes comptes ?",
        now=NOW,
        transport=fake.transport,
    )
    assert reply.text == "Désolé."
    result = fake.body(1)["messages"][-1]["content"][0]["content"]
    assert json.loads(result) == {"erreur": "Erreur interne de l'outil."}


def test_conversations_are_listed_renamed_and_deleted(demo: Demo) -> None:
    from app.assistant.chat import conversations, delete, rename  # noqa: PLC0415

    _enable(demo)
    secrets = _secrets(anthropic_api_key="cle")
    answer = FakeProvider(lambda body, turn: {"content": [{"type": "text", "text": "ok"}]})
    first = send(
        demo.engine,
        secrets,
        user_id=demo.user_id,
        conversation_id=None,
        text="Quel est mon solde actuel sur tous les comptes, en détail et par banque ?",
        now=NOW,
        transport=answer.transport,
    )
    second = send(
        demo.engine,
        secrets,
        user_id=demo.user_id,
        conversation_id=None,
        text="Abonnements ?",
        now=NOW + timedelta(minutes=5),
        transport=answer.transport,
    )
    listed = conversations(demo.engine, demo.user_id)
    assert [c.id for c in listed] == [second.conversation_id, first.conversation_id]
    assert listed[1].title == "Quel est mon solde actuel sur tous les comptes, en détail…"
    rename(demo.engine, demo.user_id, first.conversation_id, "  Soldes   par banque ", now=NOW)
    assert conversations(demo.engine, demo.user_id)[1].title == "Soldes par banque"
    attempts: list[Callable[[], None]] = [
        lambda: rename(demo.engine, demo.user_id, first.conversation_id, "  ", now=NOW),
        lambda: rename(demo.engine, demo.user_id, 999, "x", now=NOW),
        lambda: rename(demo.engine, demo.user_id + 1, first.conversation_id, "x", now=NOW),
        lambda: delete(demo.engine, demo.user_id, 999, now=NOW),
    ]
    for bad in attempts:
        with pytest.raises(AssistantError):
            bad()
    delete(demo.engine, demo.user_id, first.conversation_id, now=NOW)
    assert [c.id for c in conversations(demo.engine, demo.user_id)] == [second.conversation_id]
    with pytest.raises(AssistantError, match="introuvable"):
        history(demo.engine, demo.user_id, first.conversation_id)
    with demo.engine.connect() as connection:
        actions = connection.scalars(
            select(AuditLog.action).where(AuditLog.object_type == "assistant_conversation")
        ).all()
    assert {"assistant.renamed", "assistant.deleted"} <= set(actions)


def test_pinned_conversations_come_first(demo: Demo) -> None:
    from app.assistant.chat import conversations, pin  # noqa: PLC0415

    _enable(demo)
    secrets = _secrets(anthropic_api_key="cle")
    answer = FakeProvider(lambda body, turn: {"content": [{"type": "text", "text": "ok"}]})
    ids = [
        send(
            demo.engine,
            secrets,
            user_id=demo.user_id,
            conversation_id=None,
            text=f"Question {n}",
            now=NOW + timedelta(minutes=n),
            transport=answer.transport,
        ).conversation_id
        for n in range(3)
    ]
    assert [c.id for c in conversations(demo.engine, demo.user_id)] == ids[::-1]
    pin(demo.engine, demo.user_id, ids[0], True, now=NOW)
    pin(demo.engine, demo.user_id, ids[1], True, now=NOW + timedelta(hours=1))
    listed = conversations(demo.engine, demo.user_id)
    # Dernière épinglée en tête, puis l'autre épinglée, puis les autres par date.
    assert [(c.id, c.pinned) for c in listed] == [(ids[1], True), (ids[0], True), (ids[2], False)]
    pin(demo.engine, demo.user_id, ids[1], False, now=NOW)
    assert [c.id for c in conversations(demo.engine, demo.user_id)] == [ids[0], ids[2], ids[1]]
    for owner, conversation in ((demo.user_id, 999), (demo.user_id + 1, ids[0])):
        with pytest.raises(AssistantError, match="introuvable"):
            pin(demo.engine, owner, conversation, True, now=NOW)
    with demo.engine.connect() as connection:
        actions = connection.scalars(
            select(AuditLog.action).where(AuditLog.object_type == "assistant_conversation")
        ).all()
    assert {"assistant.pinned", "assistant.unpinned"} <= set(actions)


def test_past_tool_results_are_not_resent_and_big_results_are_cut(demo: Demo) -> None:
    """Cas réel du 06/10/2026 : 830 000 jetons pour une question, faute d'alléger l'historique."""
    from app.assistant import chat  # noqa: PLC0415

    _enable(demo)
    secrets = _secrets(anthropic_api_key="cle")
    asked = FakeProvider(
        lambda body, turn: (
            {
                "content": [
                    {
                        "type": "tool_use",
                        "id": f"t{turn}",
                        "name": "rechercher_transactions",
                        "input": {"limite": 200},
                    }
                ]
            }
            if turn == 1
            else {"content": [{"type": "text", "text": "Voilà."}]}
        )
    )
    first = send(
        demo.engine,
        secrets,
        user_id=demo.user_id,
        conversation_id=None,
        text="Toutes mes opérations ?",
        now=NOW,
        transport=asked.transport,
    )
    current_round = asked.body(1)["messages"][-1]["content"][0]["content"]
    assert "transactions" in current_round  # question en cours : résultat complet
    follow = FakeProvider(lambda body, turn: {"content": [{"type": "text", "text": "ok"}]})
    send(
        demo.engine,
        secrets,
        user_id=demo.user_id,
        conversation_id=first.conversation_id,
        text="Merci",
        now=NOW,
        transport=follow.transport,
    )
    earlier = [
        m
        for m in follow.body(0)["messages"]
        if isinstance(m["content"], list) and m["content"][0].get("type") == "tool_result"
    ]
    assert earlier and earlier[0]["content"][0]["content"] == chat.PAST_RESULT
    huge = {"lignes": ["x" * 100] * 500}
    assert len(json.dumps(huge)) > chat.MAX_TOOL_RESULT


def test_oversized_tool_result_is_truncated(demo: Demo, monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import replace  # noqa: PLC0415

    from app.assistant import chat  # noqa: PLC0415
    from app.assistant.tools import TOOLS  # noqa: PLC0415

    def huge(context: Any, params: Any) -> dict[str, Any]:
        return {"lignes": ["opération"] * 5000}

    monkeypatch.setitem(TOOLS, "comptes", replace(TOOLS["comptes"], run=huge))
    _enable(demo)
    fake = FakeProvider(
        lambda body, turn: (
            {"content": [{"type": "tool_use", "id": "t", "name": "comptes", "input": {}}]}
            if turn == 1
            else {"content": [{"type": "text", "text": "ok"}]}
        )
    )
    send(
        demo.engine,
        _secrets(anthropic_api_key="cle"),
        user_id=demo.user_id,
        conversation_id=None,
        text="Comptes ?",
        now=NOW,
        transport=fake.transport,
    )
    sent = fake.body(1)["messages"][-1]["content"][0]["content"]
    assert len(sent) <= chat.MAX_TOOL_RESULT + len(chat.TRUNCATED) and sent.endswith(chat.TRUNCATED)


def test_leak_guard_in_the_assistant_alerts_like_the_mcp_server(
    demo: Demo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Garde de confidentialité (inv. 8) : réponse bloquée et alerte, comme via le client MCP."""
    from dataclasses import replace  # noqa: PLC0415

    from app.assistant.pseudonym import Pseudonymizer  # noqa: PLC0415
    from app.assistant.tools import TOOLS  # noqa: PLC0415
    from app.models import Alert  # noqa: PLC0415

    def leaky(context: Any, params: Any) -> dict[str, Any]:
        return {"libelle": "VIR DE ALICE MARTINEZ FR26 9999 9000 0100 0000 0000 045"}

    monkeypatch.setitem(TOOLS, "comptes", replace(TOOLS["comptes"], run=leaky))
    monkeypatch.setattr(Pseudonymizer, "scrub", lambda self, payload: payload)
    _enable(demo)
    fake = FakeProvider(
        lambda body, turn: (
            {"content": [{"type": "tool_use", "id": "t", "name": "comptes", "input": {}}]}
            if turn == 1
            else {"content": [{"type": "text", "text": "Je ne peux pas."}]}
        )
    )
    send(
        demo.engine,
        _secrets(anthropic_api_key="cle"),
        user_id=demo.user_id,
        conversation_id=None,
        text="Mes comptes ?",
        now=NOW,
        transport=fake.transport,
    )
    sent = json.dumps(fake.body(1), ensure_ascii=False)
    assert "ALICE" not in sent and "9999" not in sent
    assert "bloquée par la garde" in sent
    with demo.engine.connect() as connection:
        assert "ai_leak" in connection.scalars(select(Alert.type)).all()


def test_question_reaches_the_provider_pseudonymized(demo: Demo) -> None:
    """Inv. 8 : la question saisie suit la même règle que les données (nom du locataire abrégé,
    IBAN et numéros masqués) ; l'historique affiché la garde telle quelle."""
    _enable(demo)
    fake = FakeProvider(lambda body, turn: {"content": [{"type": "text", "text": "Oui."}]})
    question = (
        "Alice Martinez a-t-elle payé ? IBAN FR76 9999 9000 0112 3456 7890 100, dossier 123456"
    )
    reply = send(
        demo.engine,
        _secrets(anthropic_api_key="cle"),
        user_id=demo.user_id,
        conversation_id=None,
        text=question,
        now=NOW,
        transport=fake.transport,
    )
    sent = json.dumps(fake.body(0)["messages"], ensure_ascii=False)
    for secret in ("Martinez", "FR76", "3456", "123456"):
        assert secret not in sent
    assert "A. M." in sent
    assert history(demo.engine, demo.user_id, reply.conversation_id)[0]["text"] == question
