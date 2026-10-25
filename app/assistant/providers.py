"""Fournisseurs d'IA de l'assistant intégré (phase B2) : API Anthropic, OpenAI, OVH AI Endpoints.

Messages dans un format neutre, traduit pour chaque API :
- `{"role": "user", "text": …}`
- `{"role": "assistant", "text": …, "calls": [{"id", "name", "arguments"}]}`
- `{"role": "tool", "id": …, "name": …, "content": "<JSON>"}`

Requêtes par le client HTTP unique, sur la liste blanche (inv. 1) ; la clé n'apparaît jamais
dans un message d'erreur ni dans un journal.
"""

import json
import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.net.allowlist import ANTHROPIC, OPENAI, OVH_AI
from app.net.client import make_client

TIMEOUT = httpx.Timeout(90, connect=10)
LOGGER = logging.getLogger(__name__)
STREAM_FALLBACK = {400, 404, 415, 422}  # flux non pris en charge : réponse en une fois
TextSink = Callable[[str], None]
MAX_OUTPUT_TOKENS = 2048

PROVIDERS = {
    "anthropic": {
        "label": "Anthropic",
        "secret": "anthropic_api_key",
        "model": "claude-sonnet-5-5",
    },
    "openai": {"label": "OpenAI", "secret": "openai_api_key", "model": "gpt-5-mini"},
    "ovh": {
        "label": "OVH AI Endpoints",
        "secret": "ovh_ai_api_key",
        "model": "Qwen3.5-397B-A17B",
    },
}


class ProviderError(Exception):
    """Refus ou panne du fournisseur (message affichable, sans secret)."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Call:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class Exchange:
    """Ce qu'on envoie : clé, modèle, consignes, messages (format neutre), outils (MCP)."""

    key: str
    model: str
    system: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]]


@dataclass(frozen=True)
class Completion:
    text: str
    calls: list[Call] = field(default_factory=list)
    tokens: int = 0


def _post(
    url: str, headers: dict[str, str], body: dict[str, Any], transport: httpx.BaseTransport | None
) -> dict[str, Any]:
    with make_client(transport=transport, timeout=TIMEOUT) as client:
        try:
            response = client.post(url, headers=headers, json=body)
        except httpx.HTTPError:
            raise ProviderError("Fournisseur d'IA injoignable : réessayer plus tard.") from None
    _check_status(response.status_code)
    try:
        data: dict[str, Any] = response.json()
    except ValueError:
        raise ProviderError("Réponse illisible du fournisseur d'IA.") from None
    return data


def _check_status(status: int) -> None:
    if status in (401, 403):
        raise ProviderError("Clé API refusée par le fournisseur : la vérifier.", status)
    if status == 429:  # noqa: PLR2004
        raise ProviderError("Quota du fournisseur dépassé : réessayer plus tard.", status)
    if status >= 400:  # noqa: PLR2004
        raise ProviderError(f"Erreur du fournisseur d'IA (HTTP {status}).", status)


def _stream(
    url: str, headers: dict[str, str], body: dict[str, Any], transport: httpx.BaseTransport | None
) -> Iterator[dict[str, Any]]:
    """Événements d'une réponse diffusée (lignes « data: … » au format SSE)."""
    with make_client(transport=transport, timeout=TIMEOUT) as client:
        try:
            with client.stream("POST", url, headers=headers, json=body) as response:
                if response.status_code >= 400:  # noqa: PLR2004
                    response.read()
                    _check_status(response.status_code)
                if response.headers.get("content-type", "").startswith("application/json"):
                    # Flux ignoré par le fournisseur : réponse complète, traitée comme telle.
                    try:
                        document = json.loads(response.read())
                    except ValueError:
                        raise ProviderError("Réponse illisible du fournisseur d'IA.") from None
                    yield {"_document": document if isinstance(document, dict) else {}}
                    return
                for line in response.iter_lines():
                    data = line[5:].strip() if line.startswith("data:") else ""
                    if not data or data == "[DONE]":
                        continue
                    try:
                        event = json.loads(data)
                    except ValueError:
                        continue
                    if isinstance(event, dict):
                        yield event
        except httpx.HTTPError:
            raise ProviderError("Fournisseur d'IA injoignable : réessayer plus tard.") from None


def _relayed(completion: Completion, on_text: TextSink) -> Completion:
    """Réponse arrivée d'un bloc : le texte est relayé en une fois."""
    if completion.text:
        on_text(completion.text)
    return completion


def _only_if(name: str, value: list[Any]) -> dict[str, Any]:
    """Champ omis s'il est vide : OVH refuse une liste d'outils vide (HTTP 400)."""
    return {name: value} if value else {}


def _arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


# --- Anthropic --------------------------------------------------------------------------------


def _anthropic_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for message in messages:
        if message["role"] == "user":
            out.append({"role": "user", "content": message["text"]})
        elif message["role"] == "assistant":
            blocks: list[dict[str, Any]] = []
            if message.get("text"):
                blocks.append({"type": "text", "text": message["text"]})
            blocks += [
                {"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["arguments"]}
                for c in message.get("calls", [])
            ]
            out.append({"role": "assistant", "content": blocks})
        else:
            result = {
                "type": "tool_result",
                "tool_use_id": message["id"],
                "content": message["content"],
            }
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                out[-1]["content"].append(result)
            else:
                out.append({"role": "user", "content": [result]})
    return out


def _anthropic_request(exchange: Exchange) -> tuple[str, dict[str, str], dict[str, Any]]:
    return (
        f"https://{ANTHROPIC}/v1/messages",
        {"x-api-key": exchange.key, "anthropic-version": "2023-06-01"},
        {
            "model": exchange.model,
            "max_tokens": MAX_OUTPUT_TOKENS,
            "system": exchange.system,
            "messages": _anthropic_messages(exchange.messages),
            **_only_if(
                "tools",
                [
                    {
                        "name": t["name"],
                        "description": t["description"],
                        "input_schema": t["inputSchema"],
                    }
                    for t in exchange.tools
                ],
            ),
        },
    )


def anthropic(exchange: Exchange, transport: httpx.BaseTransport | None = None) -> Completion:
    return _anthropic_completion(_post(*_anthropic_request(exchange), transport))


def _anthropic_completion(data: dict[str, Any]) -> Completion:
    blocks = data.get("content") or []
    usage = data.get("usage") or {}
    return Completion(
        text="\n".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip(),
        calls=[
            Call(str(b.get("id")), str(b.get("name")), _arguments(b.get("input")))
            for b in blocks
            if b.get("type") == "tool_use"
        ],
        tokens=int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0)),
    )


def anthropic_stream(
    exchange: Exchange, on_text: TextSink, transport: httpx.BaseTransport | None = None
) -> Completion:
    url, headers, body = _anthropic_request(exchange)
    texts: dict[int, list[str]] = {}
    tools: dict[int, dict[str, Any]] = {}
    tokens_in = tokens_out = 0
    for event in _stream(url, headers, {**body, "stream": True}, transport):
        if "_document" in event:
            return _relayed(_anthropic_completion(event["_document"]), on_text)
        kind, index = event.get("type"), int(event.get("index", 0))
        if kind == "message_start":
            usage = (event.get("message") or {}).get("usage") or {}
            tokens_in = int(usage.get("input_tokens", 0))
        elif kind == "content_block_start":
            block = event.get("content_block") or {}
            if block.get("type") == "tool_use":
                tools[index] = {"id": block.get("id"), "name": block.get("name"), "json": []}
            elif block.get("type") == "text":
                if texts:
                    on_text("\n")  # nouveau bloc de texte
                texts[index] = []
        elif kind == "content_block_delta":
            delta = event.get("delta") or {}
            if delta.get("type") == "text_delta" and delta.get("text"):
                texts.setdefault(index, []).append(delta["text"])
                on_text(delta["text"])
            elif delta.get("type") == "input_json_delta" and index in tools:
                tools[index]["json"].append(delta.get("partial_json", ""))
        elif kind == "message_delta":
            tokens_out = int((event.get("usage") or {}).get("output_tokens", tokens_out))
        elif kind == "error":
            raise ProviderError("Erreur du fournisseur d'IA pendant la réponse.")
    return Completion(
        text="\n".join("".join(parts) for _, parts in sorted(texts.items())).strip(),
        calls=[
            Call(str(t["id"]), str(t["name"]), _arguments("".join(t["json"]) or "{}"))
            for _, t in sorted(tools.items())
        ],
        tokens=tokens_in + tokens_out,
    )


# --- OpenAI et compatibles (OVH AI Endpoints) --------------------------------------------------


def _openai_messages(system: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for message in messages:
        if message["role"] == "user":
            out.append({"role": "user", "content": message["text"]})
        elif message["role"] == "assistant":
            entry: dict[str, Any] = {"role": "assistant", "content": message.get("text") or None}
            if message.get("calls"):
                entry["tool_calls"] = [
                    {
                        "id": c["id"],
                        "type": "function",
                        "function": {
                            "name": c["name"],
                            "arguments": json.dumps(c["arguments"], ensure_ascii=False),
                        },
                    }
                    for c in message["calls"]
                ]
            out.append(entry)
        else:
            out.append(
                {"role": "tool", "tool_call_id": message["id"], "content": message["content"]}
            )
    return out


def _openai_request(host: str, exchange: Exchange) -> tuple[str, dict[str, str], dict[str, Any]]:
    return (
        f"https://{host}/v1/chat/completions",
        {"Authorization": f"Bearer {exchange.key}"},
        {
            "model": exchange.model,
            "messages": _openai_messages(exchange.system, exchange.messages),
            **_only_if(
                "tools",
                [
                    {
                        "type": "function",
                        "function": {
                            "name": t["name"],
                            "description": t["description"],
                            "parameters": t["inputSchema"],
                        },
                    }
                    for t in exchange.tools
                ],
            ),
        },
    )


def openai_compatible(
    host: str, exchange: Exchange, transport: httpx.BaseTransport | None = None
) -> Completion:
    return _openai_completion(_post(*_openai_request(host, exchange), transport))


def _openai_completion(data: dict[str, Any]) -> Completion:
    choices = data.get("choices") or [{}]
    message = choices[0].get("message") or {}
    usage = data.get("usage") or {}
    return Completion(
        text=(message.get("content") or "").strip(),
        calls=[
            Call(
                str(c.get("id")),
                str((c.get("function") or {}).get("name")),
                _arguments((c.get("function") or {}).get("arguments")),
            )
            for c in message.get("tool_calls") or []
        ],
        tokens=int(usage.get("prompt_tokens", 0)) + int(usage.get("completion_tokens", 0)),
    )


def openai_stream(
    host: str, exchange: Exchange, on_text: TextSink, transport: httpx.BaseTransport | None = None
) -> Completion:
    url, headers, body = _openai_request(host, exchange)
    body = {**body, "stream": True, "stream_options": {"include_usage": True}}
    text: list[str] = []
    calls: dict[int, dict[str, Any]] = {}
    tokens = 0
    for event in _stream(url, headers, body, transport):
        if "_document" in event:
            return _relayed(_openai_completion(event["_document"]), on_text)
        usage = event.get("usage") or {}
        if usage:
            tokens = int(usage.get("prompt_tokens", 0)) + int(usage.get("completion_tokens", 0))
        for choice in event.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("content"):
                text.append(delta["content"])
                on_text(delta["content"])
            for part in delta.get("tool_calls") or []:
                entry = calls.setdefault(
                    int(part.get("index", 0)), {"id": "", "name": "", "args": []}
                )
                function = part.get("function") or {}
                entry["id"] = part.get("id") or entry["id"]
                entry["name"] = function.get("name") or entry["name"]
                entry["args"].append(function.get("arguments") or "")
    content = "".join(text)
    if not tokens:  # usage non renvoyé : estimation (≈ 4 caractères par jeton) pour le plafond
        tokens = (len(json.dumps(body, ensure_ascii=False)) + len(content)) // 4
    return Completion(
        text=content.strip(),
        calls=[
            Call(str(c["id"]), str(c["name"]), _arguments("".join(c["args"]) or "{}"))
            for _, c in sorted(calls.items())
        ],
        tokens=tokens,
    )


def complete(
    provider: str,
    exchange: Exchange,
    transport: httpx.BaseTransport | None = None,
    *,
    on_text: TextSink | None = None,
) -> Completion:
    """Réponse complète ; avec `on_text`, en direct (flux), le texte étant relayé au fil de
    l'eau. Si le fournisseur refuse le flux avant tout texte, nouvel essai sans flux."""
    host = {"openai": OPENAI, "ovh": OVH_AI}.get(provider)
    if provider != "anthropic" and host is None:
        raise ProviderError("Fournisseur d'IA inconnu.")
    if on_text is not None:
        received: list[str] = []

        def relay(delta: str) -> None:
            received.append(delta)
            on_text(delta)

        try:
            if provider == "anthropic":
                return anthropic_stream(exchange, relay, transport)
            return openai_stream(str(host), exchange, relay, transport)
        except ProviderError as exc:
            if received or exc.status not in STREAM_FALLBACK:
                raise
            LOGGER.warning(
                "flux refusé par le fournisseur (HTTP %s) : réponse en une fois", exc.status
            )
    if provider == "anthropic":
        return anthropic(exchange, transport)
    return openai_compatible(str(host), exchange, transport)
