"""Serveur MCP `/mcp` (docs/spec-mcp.md §3-4) : conformité vérifiée avec le client du SDK
officiel, authentification, interrupteurs, origine, quotas, journal."""

import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import Engine, select

from app.assistant.access import ENABLED, WRITES, create_token, revoke_token, set_switch
from app.assistant.pseudonym import Pseudonymizer
from app.assistant.tools import TOOLS
from app.models import Alert, AuditLog
from app.web.main import create_app
from tests.assistant.conftest import NOW, PASSWORD, Demo, build_demo
from tests.web.conftest import fake_secrets

URL = "https://finances.invalid/mcp"


@dataclass
class Server:
    app: FastAPI
    client: TestClient
    demo: Demo
    token: str

    def post(self, message: dict[str, Any], token: str | None = None, **headers: str) -> Any:
        auth = {"Authorization": f"Bearer {token or self.token}"}
        return self.client.post("/mcp", json=message, headers={**auth, **headers})

    def rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        response = self.post({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
        assert response.status_code == 200, response.text
        return response.json()


@pytest.fixture
def server(engine: Engine) -> Iterator[Server]:
    app = create_app(secrets=fake_secrets(), engine=engine, now=lambda: NOW)
    demo = build_demo(engine)  # après create_app : même trousseau de clés
    token = create_token(
        engine, user_id=demo.user_id, password=PASSWORD, name="PC", can_write=True, now=NOW
    )
    for key in (ENABLED, WRITES):
        set_switch(engine, key, True, actor_user_id=demo.user_id, now=NOW)
    with TestClient(app, base_url="https://finances.invalid") as client:
        yield Server(app, client, demo, token)


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_official_sdk_client(server: Server, anyio_backend: str) -> None:
    transport = httpx2.ASGITransport(app=server.app)
    headers = {"Authorization": f"Bearer {server.token}"}
    async with (
        httpx2.AsyncClient(
            transport=transport, headers=headers, base_url="https://finances.invalid"
        ) as http,
        streamable_http_client(URL, http_client=http) as streams,
        ClientSession(streams[0], streams[1]) as session,
    ):
        init = await session.initialize()
        assert init.server_info.name == "finances"
        assert "JAMAIS" in (init.instructions or "")
        tools = await session.list_tools()
        names = {t.name for t in tools.tools}
        assert {
            "comptes",
            "rechercher_transactions",
            "categoriser",
            "annuler_modification",
        } <= names
        result = await session.call_tool("synthese_mensuelle", {"du": "2026-08", "au": "2026-09"})
        assert not result.is_error
        assert result.structured_content is not None
        assert result.structured_content["mois"][0]["revenus"]["centimes"] == 250000
        refused = await session.call_tool("synthese_mensuelle", {"du": "2026-13"})
        assert refused.is_error
        prompts = await session.list_prompts()
        assert len(prompts.prompts) == 6
        prompt = await session.get_prompt("bilan_du_mois", {"mois": "2026-08"})
        assert "2026-08" in json.dumps(prompt.model_dump(mode="json"))


def test_authentication(server: Server) -> None:
    message = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    assert server.client.post("/mcp", json=message).status_code == 401
    unknown = "fia_inconnu"
    assert server.post(message, unknown).status_code == 401
    response = server.client.post(
        "/mcp", json=message, headers={"Authorization": "Bearer appareil"}
    )
    assert response.status_code == 401 and response.headers["www-authenticate"] == "Bearer"
    assert server.post(message).json() == {"jsonrpc": "2.0", "id": 1, "result": {}}
    revoke_token(server.demo.engine, 1, actor_user_id=server.demo.user_id, now=NOW)
    assert server.post(message).status_code == 401


def test_switches_scope_and_origin(server: Server) -> None:
    message = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    assert server.post(message, Origin="https://pirate.example").status_code == 403
    assert server.post(message, Origin="https://finances.invalid").status_code == 200
    set_switch(server.demo.engine, WRITES, False, actor_user_id=1, now=NOW)
    tools = server.rpc("tools/list")["result"]["tools"]
    assert tools and all(t["annotations"]["readOnlyHint"] for t in tools)  # écriture masquée
    set_switch(server.demo.engine, ENABLED, False, actor_user_id=1, now=NOW)
    assert server.post(message).status_code == 403


def test_protocol_errors(server: Server) -> None:
    auth = {"Authorization": f"Bearer {server.token}"}
    assert server.client.post("/mcp", content=b"{", headers=auth).status_code == 400
    assert server.client.post("/mcp", json=[{"jsonrpc": "2.0"}], headers=auth).status_code == 400
    assert server.rpc("resources/list")["error"]["code"] == -32601
    assert server.rpc("server/discover")["error"]["code"] == -32601
    assert (
        server.post({"jsonrpc": "1.0", "id": 1, "method": "ping"}).json()["error"]["code"] == -32600
    )
    assert server.rpc("tools/call", {"arguments": {}})["error"]["code"] == -32602
    assert server.rpc("prompts/get", {"name": "inconnue"})["error"]["code"] == -32602
    assert server.rpc("prompts/get", {"name": "comparer_deux_mois", "arguments": {"mois_a": "x"}})[
        "error"
    ]
    notification = server.post({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert notification.status_code == 202 and notification.content == b""
    assert server.client.get("/mcp", headers=auth).status_code == 405
    negotiated = server.rpc("initialize", {"protocolVersion": "2030-01-01", "capabilities": {}})
    assert negotiated["result"]["protocolVersion"] == "2025-11-25"


def test_calls_are_audited_and_rate_limited(server: Server) -> None:
    for _ in range(3):
        server.rpc("tools/call", {"name": "comptes", "arguments": {}})
    with server.demo.engine.connect() as connection:
        calls: Sequence[dict[str, Any]] = connection.scalars(
            select(AuditLog.details).where(AuditLog.action == "mcp.call")
        ).all()
    assert len(calls) == 3 and calls[0]["outil"] == "comptes" and calls[0]["ok"] is True
    for _ in range(57):
        server.rpc("tools/call", {"name": "ping_inexistant", "arguments": {"texte": "secret"}})
    with server.demo.engine.connect() as connection:
        last = connection.scalars(
            select(AuditLog.details)
            .where(AuditLog.action == "mcp.call")
            .order_by(AuditLog.id.desc())
        ).first()
    assert last is not None
    assert last == {
        "outil": "ping_inexistant",
        "ok": False,
        "lignes": None,
        "ms": last["ms"],
        "parametres": ["texte"],
    }
    limited = server.post(
        {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "comptes"}}
    )
    assert limited.status_code == 429


def test_leak_guard_blocks_the_answer_and_alerts(
    server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    def leaky(context: Any, params: Any) -> dict[str, Any]:
        return {"libelle": "VIR DE ALICE MARTINEZ FR26 9999 9000 0100 0000 0000 045"}

    monkeypatch.setitem(TOOLS, "comptes", replace(TOOLS["comptes"], run=leaky))
    # Le rattrapage masque d'ordinaire l'IBAN : on le neutralise pour éprouver la garde finale.
    monkeypatch.setattr(Pseudonymizer, "scrub", lambda self, payload: payload)
    answer = server.rpc("tools/call", {"name": "comptes", "arguments": {}})["result"]
    assert answer["isError"] and "bloquée" in answer["content"][0]["text"]
    assert "ALICE" not in json.dumps(answer) and "9999" not in json.dumps(answer)
    with server.demo.engine.connect() as connection:
        assert "ai_leak" in connection.scalars(select(Alert.type)).all()


def test_write_through_protocol_then_undo(server: Server) -> None:
    rows = server.rpc(
        "tools/call", {"name": "rechercher_transactions", "arguments": {"texte": "UBER"}}
    )
    tx = rows["result"]["structuredContent"]["transactions"][0]["id"]
    done = server.rpc(
        "tools/call",
        {"name": "categoriser", "arguments": {"transactions": [tx], "categorie": "Restaurants"}},
    )
    change = done["result"]["structuredContent"]["modification"]
    undone = server.rpc(
        "tools/call", {"name": "annuler_modification", "arguments": {"modification": change}}
    )
    assert undone["result"]["structuredContent"]["annulee"] is True


def test_names_left_in_clear_are_abbreviated(
    server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    def named(context: Any, params: Any) -> dict[str, Any]:
        return {"categorie": "Cadeaux pour Alice Martinez"}

    monkeypatch.setitem(TOOLS, "comptes", replace(TOOLS["comptes"], run=named))
    answer = server.rpc("tools/call", {"name": "comptes", "arguments": {}})["result"]
    assert not answer["isError"] and answer["structuredContent"] == {
        "categorie": "Cadeaux pour A. M."
    }


def test_tool_crash_is_contained(
    server: Server, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(context: Any, params: Any) -> dict[str, Any]:
        raise RuntimeError("détail sensible ALICE")

    monkeypatch.setitem(TOOLS, "comptes", replace(TOOLS["comptes"], run=broken))
    answer = server.rpc("tools/call", {"name": "comptes", "arguments": {}})["result"]
    assert answer["isError"] and answer["content"][0]["text"] == "Erreur interne de l'outil."
    assert "RuntimeError" in caplog.text and "ALICE" not in caplog.text
    assert server.rpc("ping")["result"] == {}  # le serveur répond toujours


def test_oversized_message_is_refused(server: Server) -> None:
    huge = {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {"x": "a" * 1_100_000}}
    assert server.post(huge).status_code == 413
