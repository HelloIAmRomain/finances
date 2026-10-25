"""Fournisseurs d'IA : traduction des messages, erreurs lisibles, sans réseau ni secret."""

import json
from typing import Any

import httpx
import pytest

from app.assistant.providers import Exchange, ProviderError, _arguments, complete

EXCHANGE = Exchange(
    key="cle-secrete-factice",
    model="m",
    system="consignes",
    messages=[
        {"role": "user", "text": "Q"},
        {
            "role": "assistant",
            "text": "",
            "calls": [
                {"id": "a", "name": "comptes", "arguments": {}},
                {"id": "b", "name": "categories", "arguments": {}},
            ],
        },
        {"role": "tool", "id": "a", "name": "comptes", "content": "{}"},
        {"role": "tool", "id": "b", "name": "categories", "content": "{}"},
    ],
    tools=[{"name": "comptes", "description": "d", "inputSchema": {"type": "object"}}],
)


def _transport(handler: Any) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


@pytest.mark.parametrize(
    ("status", "message"),
    [(401, "Clé API refusée"), (403, "Clé API refusée"), (429, "Quota"), (500, "HTTP 500")],
)
def test_http_errors_are_readable(status: int, message: str) -> None:
    transport = _transport(
        lambda request: httpx.Response(status, json={"error": "cle-secrete-factice"})
    )
    with pytest.raises(ProviderError, match=message) as caught:
        complete("openai", EXCHANGE, transport)
    assert "cle-secrete" not in str(caught.value)


def test_network_and_parsing_errors() -> None:
    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("hors ligne")

    with pytest.raises(ProviderError, match="injoignable"):
        complete("anthropic", EXCHANGE, _transport(offline))
    with pytest.raises(ProviderError, match="illisible"):
        complete("anthropic", EXCHANGE, _transport(lambda r: httpx.Response(200, text="<html>")))
    with pytest.raises(ProviderError, match="inconnu"):
        complete("autre", EXCHANGE)


def test_anthropic_groups_tool_results() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"content": [{"type": "text", "text": " ok "}]})

    assert complete("anthropic", EXCHANGE, _transport(handler)).text == "ok"
    messages = seen[0]["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert [b["tool_use_id"] for b in messages[2]["content"]] == ["a", "b"]
    assert messages[1]["content"][0]["type"] == "tool_use"  # pas de bloc texte vide


def test_openai_tool_calls_with_bad_arguments() -> None:
    reply = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {"id": "x", "function": {"name": "comptes", "arguments": "pas du json"}},
                        {"id": "y", "function": {"name": "comptes", "arguments": "[1]"}},
                    ],
                }
            }
        ],
    }
    completion = complete("openai", EXCHANGE, _transport(lambda r: httpx.Response(200, json=reply)))
    assert [c.arguments for c in completion.calls] == [{}, {}] and completion.text == ""
    assert _arguments({"a": 1}) == {"a": 1} and _arguments(None) == {}


def test_empty_tool_list_is_omitted() -> None:
    """OVH répond HTTP 400 à « tools: [] » (constaté le 05/10/2026)."""
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        if "anthropic" in str(request.url):
            return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    bare = Exchange(
        key="k", model="m", system="s", messages=[{"role": "user", "text": "Q"}], tools=[]
    )
    complete("ovh", bare, _transport(handler))
    complete("anthropic", bare, _transport(handler))
    assert all("tools" not in body for body in seen)


def _sse(*events: dict[str, Any]) -> httpx.Response:
    body = "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())


def test_anthropic_stream_relays_text_and_rebuilds_tool_calls() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return _sse(
            {"type": "message_start", "message": {"usage": {"input_tokens": 100}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "Je "},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "regarde."},
            },
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "tool_use", "id": "t1", "name": "comptes"},
            },
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": '{"mo'},
            },
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": 'is": 3}'},
            },
            {"type": "message_delta", "usage": {"output_tokens": 20}},
        )

    pieces: list[str] = []
    completion = complete("anthropic", EXCHANGE, _transport(handler), on_text=pieces.append)
    assert seen[0]["stream"] is True
    assert pieces == ["Je ", "regarde."] and completion.text == "Je regarde."
    assert [(c.id, c.name, c.arguments) for c in completion.calls] == [
        ("t1", "comptes", {"mois": 3})
    ]
    assert completion.tokens == 120


def test_openai_stream_with_fragmented_tool_calls_and_estimated_tokens() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return _sse(
            {"choices": [{"delta": {"content": "Voici "}}]},
            {"choices": [{"delta": {"content": "le bilan."}}]},
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "c1",
                                    "function": {"name": "comptes", "arguments": '{"a"'},
                                }
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {"delta": {"tool_calls": [{"index": 0, "function": {"arguments": ": 1}"}}]}}
                ]
            },
        )

    pieces: list[str] = []
    completion = complete("ovh", EXCHANGE, _transport(handler), on_text=pieces.append)
    assert seen[0]["stream"] is True and seen[0]["stream_options"] == {"include_usage": True}
    assert "".join(pieces) == "Voici le bilan." and completion.text == "Voici le bilan."
    assert [(c.id, c.name, c.arguments) for c in completion.calls] == [("c1", "comptes", {"a": 1})]
    assert completion.tokens > 0  # usage absent : estimation pour le plafond


def test_stream_falls_back_when_refused_before_any_text() -> None:
    calls: list[bool] = []

    def handler(request: httpx.Request) -> httpx.Response:
        streaming = bool(json.loads(request.content).get("stream"))
        calls.append(streaming)
        if streaming:
            return httpx.Response(400, json={"error": "stream_options not supported"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    pieces: list[str] = []
    completion = complete("openai", EXCHANGE, _transport(handler), on_text=pieces.append)
    assert calls == [True, False] and completion.text == "ok"


def test_stream_errors_are_not_retried_when_unrelated_to_streaming() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={})

    with pytest.raises(ProviderError, match="Clé API refusée"):
        complete("anthropic", EXCHANGE, _transport(handler), on_text=lambda _: None)

    def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/json"}, content=b"<html>")

    with pytest.raises(ProviderError, match="illisible"):
        complete("anthropic", EXCHANGE, _transport(broken), on_text=lambda _: None)

    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("hors ligne")

    with pytest.raises(ProviderError, match="injoignable"):
        complete("ovh", EXCHANGE, _transport(offline), on_text=lambda _: None)


def test_stream_ignored_by_provider_still_answers() -> None:
    reply = {"content": [{"type": "text", "text": "D'un bloc."}], "usage": {"input_tokens": 3}}
    pieces: list[str] = []
    completion = complete(
        "anthropic",
        EXCHANGE,
        _transport(lambda r: httpx.Response(200, json=reply)),
        on_text=pieces.append,
    )
    assert pieces == ["D'un bloc."] and completion.text == "D'un bloc."
