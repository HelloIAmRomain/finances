"""Serveur MCP pour le client du PC (docs/spec-mcp.md §3-4) : JSON-RPC 2.0 sur HTTP.

Transport « Streamable HTTP » de MCP (poignée de main `initialize`, révisions 2025-03-26 à
2025-11-25), en mode sans état : chaque message arrive par
`POST /mcp`, la réponse est un objet JSON (pas de flux SSE, pas de session). Implémentation
propre du sous-ensemble utile (`initialize`, `ping`, `tools/*`, `prompts/*`) ; la conformité
est vérifiée dans les tests par le client du SDK officiel.

- Authentification : `Authorization: Bearer fia_…` seulement (jamais de cookie, donc pas de
  CSRF) ; en-tête `Origin` étranger refusé (DNS rebinding).
- Interrupteur « Accès IA » coupé : 403. Quotas dépassés : 429. Chaque appel d'outil est
  journalisé. Tailscale seulement (inv. 7), comme le reste de l'application.
"""

import json
import logging
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

import app.assistant.catalog  # noqa: F401 (enregistrement des outils)
from app.assistant.access import (
    ENABLED,
    WRITES,
    TokenInfo,
    authenticate,
    over_quota,
    switches,
)
from app.assistant.instructions import INSTRUCTIONS
from app.assistant.invoke import invoke
from app.assistant.prompts import PROMPTS, render_prompt
from app.assistant.terms import load_pseudonymizer
from app.assistant.tools import TOOLS, Context
from app.config import Settings
from app.web.deps import engine_of, now_of

router = APIRouter()
LOGGER = logging.getLogger(__name__)
MAX_BODY = 1_000_000  # octets : un message JSON-RPC reste petit

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
SERVER_INFO = {"name": "finances", "title": "Finances perso", "version": "1.0.0"}
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32700, -32600, -32601, -32602


def _error(message_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}


def _result(message_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def _refused(status: int, message: str) -> JSONResponse:
    headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None  # noqa: PLR2004
    return JSONResponse(_error(None, INVALID_REQUEST, message), status_code=status, headers=headers)


def _origin_allowed(request: Request) -> bool:
    origin = request.headers.get("origin")
    if origin is None:
        return True
    settings: Settings = request.app.state.settings
    expected = urlsplit(settings.public_base_url)
    return origin.rstrip("/") == f"{expected.scheme}://{expected.netloc}"


def _rows(result: dict[str, Any]) -> int:
    return sum(len(value) for value in result.values() if isinstance(value, list))


def _call(
    request: Request, token: TokenInfo, params: dict[str, Any], state: dict[str, bool]
) -> dict[str, Any]:
    engine, now = engine_of(request), now_of(request)
    name = str(params.get("name", ""))
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        arguments = {}
    context = Context(
        engine=engine, token=token, now=now, masker=load_pseudonymizer(engine), switches=state
    )
    outcome = invoke(context, name, arguments)
    if outcome.result is None:
        return {"content": [{"type": "text", "text": outcome.error}], "isError": True}
    return {
        "content": [{"type": "text", "text": json.dumps(outcome.result, ensure_ascii=False)}],
        "structuredContent": outcome.result,
        "isError": False,
    }


def _visible_tools(token: TokenInfo, state: dict[str, bool]) -> list[dict[str, Any]]:
    writes = token.can_write and state.get(WRITES, False)
    return [t.descriptor() for t in TOOLS.values() if writes or not t.write]


def handle(  # noqa: PLR0911 (une réponse par méthode JSON-RPC)
    request: Request, token: TokenInfo, message: dict[str, Any]
) -> dict[str, Any] | None:
    """Réponse JSON-RPC à un message ; None pour une notification (pas de réponse)."""
    method = message.get("method")
    message_id = message.get("id")
    params = message.get("params") or {}
    if (
        message.get("jsonrpc") != "2.0"
        or not isinstance(method, str)
        or not isinstance(params, dict)
    ):
        return _error(message_id, INVALID_REQUEST, "Requête JSON-RPC invalide.")
    if "id" not in message:
        return None  # notification (notifications/initialized, annulation…)
    state = switches(engine_of(request))
    if method == "initialize":
        asked = params.get("protocolVersion")
        return _result(
            message_id,
            {
                "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                "capabilities": {
                    "tools": {"listChanged": False},
                    "prompts": {"listChanged": False},
                },
                "serverInfo": SERVER_INFO,
                "instructions": INSTRUCTIONS,
            },
        )
    if method == "ping":
        return _result(message_id, {})
    if method == "tools/list":
        return _result(message_id, {"tools": _visible_tools(token, state)})
    if method == "tools/call":
        if not isinstance(params.get("name"), str):
            return _error(message_id, INVALID_PARAMS, "Nom d'outil manquant.")
        return _result(message_id, _call(request, token, params, state))
    if method == "prompts/list":
        return _result(message_id, {"prompts": [p.descriptor() for p in PROMPTS.values()]})
    if method == "prompts/get":
        rendered = render_prompt(str(params.get("name", "")), params.get("arguments") or {})
        if rendered is None:
            return _error(message_id, INVALID_PARAMS, "Invite inconnue ou arguments invalides.")
        return _result(message_id, rendered)
    return _error(message_id, METHOD_NOT_FOUND, f"Méthode non prise en charge : {method}")


@router.post("/mcp")
async def mcp(request: Request) -> Response:  # noqa: PLR0911 (un refus par contrôle)
    if not _origin_allowed(request):
        return _refused(403, "Origine refusée.")
    engine, now = engine_of(request), now_of(request)
    header = request.headers.get("authorization", "")
    token = authenticate(engine, header[7:] if header.startswith("Bearer ") else None, now=now)
    if token is None:
        return _refused(401, "Jeton absent, inconnu ou révoqué.")
    if not switches(engine)[ENABLED]:
        return _refused(403, "Accès IA désactivé (page Sécurité de l'application).")
    body = await request.body()
    if len(body) > MAX_BODY:
        return JSONResponse(_error(None, INVALID_REQUEST, "Message trop long."), status_code=413)
    try:
        message = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return JSONResponse(_error(None, PARSE_ERROR, "JSON illisible."), status_code=400)
    if not isinstance(message, dict):
        return JSONResponse(
            _error(None, INVALID_REQUEST, "Un seul message JSON-RPC par requête."), status_code=400
        )
    if message.get("method") == "tools/call" and over_quota(engine, token.id, now=now):
        return _refused(429, "Trop d'appels : réessayer plus tard.")
    answer = await run_in_threadpool(handle, request, token, message)
    if answer is None:
        return Response(status_code=202)
    return JSONResponse(answer)


@router.get("/mcp")
@router.delete("/mcp")
def mcp_other() -> Response:
    """Ni flux SSE ni session : serveur sans état."""
    return Response(status_code=405, headers={"Allow": "POST"})
