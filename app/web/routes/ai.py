"""Réglages de l'accès IA (page Sécurité), extension MCP du PC et page « Modifications par
l'IA » (docs/spec-mcp.md §4, §11). Toutes les actions sont faites par moi, connecté, et auditées."""

import io
import json
import zipfile
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import Engine
from starlette.concurrency import run_in_threadpool

import app.assistant.catalog  # noqa: F401 (enregistrement des outils)
from app.assistant.access import (
    ENABLED,
    MODEL,
    MONTHLY_CAP,
    PROVIDER,
    STREAM,
    WRITES,
    AccessError,
    assistant_settings,
    create_token,
    recent_calls,
    revoke_token,
    set_assistant_settings,
    set_switch,
    switches,
    tokens,
)
from app.assistant.changes import TABLE_NAMES, ChangeError, change_sets, changes_of, undo
from app.assistant.chat import AssistantError, conversations, delete, history, pin, rename
from app.assistant.chat import conversation as find_conversation
from app.assistant.prompts import PROMPTS
from app.assistant.providers import PROVIDERS
from app.assistant.runs import RunBusyError
from app.assistant.terms import TermError, add_term, preview, remove_term, terms
from app.assistant.tools import TOOLS
from app.auth.security_keys import keys
from app.categorize.ai import AUTO, THRESHOLD, AiCategorizationError
from app.categorize.ai import set_settings as set_categorization_settings
from app.categorize.ai import settings as categorization_settings
from app.config import Secrets, Settings
from app.web.assistant_turns import start_turn, turn_payload
from app.web.deps import (
    CurrentUser,
    current_user,
    engine_of,
    now_of,
    redirect_with_message,
    templates_of,
)
from app.web.flash import flash

router = APIRouter()
User = Annotated[CurrentUser, Depends(current_user)]
EXTENSION = Path(__file__).resolve().parents[2] / "assistant" / "extension"


def ai_context(engine: Engine, secrets: Secrets | None = None) -> dict[str, Any]:
    token_rows = tokens(engine)
    names = {t.id: t.name for t in tokens(engine, include_internal=True)}
    return {
        "ai_switches": switches(engine),
        "ai_tokens": token_rows,
        "ai_terms": terms(engine),
        "ai_calls": [(call, names.get(call.token_id or 0, "?")) for call in recent_calls(engine)],
        "ai_preview": preview(engine),
        "ai_enabled_key": ENABLED,
        "assistant": assistant_settings(engine),
        "assistant_keys": {
            "provider": PROVIDER,
            "model": MODEL,
            "cap": MONTHLY_CAP,
            "stream": STREAM,
        },
        "providers": PROVIDERS,
        "provider_keys": {
            name: secrets is not None and getattr(secrets, spec["secret"], None) is not None
            for name, spec in PROVIDERS.items()
        },
        "ai_writes_key": WRITES,
        "categorization": categorization_settings(engine),
        "categorization_keys": {"auto": AUTO, "threshold": THRESHOLD},
    }


def _back(message: str, anchor: str = "acces-ia") -> RedirectResponse:
    return redirect_with_message("/securite", message, anchor=anchor)


@router.post("/securite/ia/interrupteurs")
def set_switches(
    request: Request,
    user: User,
    acces: Annotated[str | None, Form()] = None,
    ecriture: Annotated[str | None, Form()] = None,
) -> Response:
    engine, now = engine_of(request), now_of(request)
    set_switch(engine, ENABLED, acces == "1", actor_user_id=user.id, now=now)
    set_switch(engine, WRITES, ecriture == "1", actor_user_id=user.id, now=now)
    return _back("réglages de l'accès IA enregistrés")


@router.post("/securite/ia/jetons", response_class=HTMLResponse)
def new_token(
    request: Request,
    user: User,
    mot_de_passe: Annotated[str, Form()],
    nom: Annotated[str, Form()] = "PC",
    ecriture: Annotated[str | None, Form()] = None,
) -> Response:
    engine, now = engine_of(request), now_of(request)
    try:
        token = create_token(
            engine,
            user_id=user.id,
            password=mot_de_passe,
            name=nom,
            can_write=ecriture == "1",
            now=now,
        )
    except AccessError as exc:
        return _back(str(exc))
    templates = templates_of(request)
    response = templates.TemplateResponse(
        request,
        "security.html",
        {
            "user": user,
            "keys": keys(engine, user.id),
            "message": "jeton créé : copiez-le maintenant, il ne sera plus affiché",
            "new_token": token,
            **ai_context(engine, request.app.state.secrets),
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/securite/ia/jetons/{token_id}/revoquer")
def revoke(request: Request, token_id: int, user: User) -> Response:
    try:
        revoke_token(engine_of(request), token_id, actor_user_id=user.id, now=now_of(request))
    except AccessError as exc:
        return _back(str(exc))
    return _back("jeton révoqué")


@router.post("/securite/ia/termes")
def new_term(
    request: Request,
    user: User,
    type: Annotated[str, Form()],
    valeur: Annotated[str, Form()],
) -> Response:
    if type not in ("personne", "emetteur"):
        return _back("Type inconnu.", "noms-ia")
    try:
        add_term(engine_of(request), type, valeur, actor_user_id=user.id, now=now_of(request))  # type: ignore[arg-type]
    except TermError as exc:
        return _back(str(exc), "noms-ia")
    return _back("ajouté à la liste", "noms-ia")


@router.post("/securite/ia/termes/{term_id}/supprimer")
def delete_term(request: Request, term_id: int, user: User) -> Response:
    try:
        remove_term(engine_of(request), term_id, actor_user_id=user.id, now=now_of(request))
    except TermError as exc:
        return _back(str(exc), "noms-ia")
    return _back("retiré de la liste", "noms-ia")


def extension_bundle(settings: Settings) -> bytes:
    """Extension MCP du PC (.mcpb, archive zip) : adresse du Pi préremplie, outils listés."""
    manifest = json.loads((EXTENSION / "manifest.json").read_text(encoding="utf-8"))
    manifest["user_config"]["adresse"]["default"] = settings.public_base_url.rstrip("/") + "/mcp"
    manifest["tools"] = [{"name": t.name, "description": t.title} for t in TOOLS.values()]
    manifest["prompts"] = [
        {"name": p.name, "description": p.description, "text": p.template} for p in PROMPTS.values()
    ]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        bundle.write(EXTENSION / "server" / "index.js", "server/index.js")
    return buffer.getvalue()


@router.get("/ia/extension.mcpb")
def download_extension(request: Request, user: User) -> Response:
    return Response(
        extension_bundle(request.app.state.settings),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="finances.mcpb"'},
    )


@router.get("/ia/modifications", response_class=HTMLResponse)
def changes_page(request: Request, user: User) -> Response:
    engine = engine_of(request)
    detail = request.query_params.get("detail")
    shown: list[tuple[str, str, int]] = []
    if detail and detail.isdigit():
        counts: dict[tuple[str, str], int] = {}
        try:
            for change in changes_of(engine, int(detail)):
                kind = (
                    "ajout"
                    if change.before is None
                    else "retrait"
                    if change.after is None
                    else "modification"
                )
                key = (TABLE_NAMES[change.table], kind)
                counts[key] = counts.get(key, 0) + 1
        except ChangeError:
            counts = {}
        shown = [(table, kind, count) for (table, kind), count in sorted(counts.items())]
    templates = templates_of(request)
    return templates.TemplateResponse(
        request,
        "ai_changes.html",
        {
            "user": user,
            "changes": change_sets(engine, 100),
            "detail": int(detail) if detail and detail.isdigit() else None,
            "detail_rows": shown,
            "message": flash(request),
        },
    )


@router.post("/ia/modifications/{change_id}/annuler")
def undo_change(request: Request, change_id: int, user: User) -> Response:
    try:
        undo(engine_of(request), change_id, actor_user_id=user.id, now=now_of(request))
        message = "modification annulée"
    except ChangeError as exc:
        message = str(exc)
    return redirect_with_message("/ia/modifications", message)


@router.post("/securite/ia/assistant")
def assistant_settings_form(
    request: Request,
    user: User,
    *,
    fournisseur: Annotated[str, Form()],
    plafond: Annotated[int, Form(ge=0, le=100_000_000)],
    modele: Annotated[str, Form()] = "",
    en_direct: Annotated[str | None, Form()] = None,
) -> Response:
    if fournisseur not in PROVIDERS:
        return _back("Fournisseur inconnu.", "assistant-ia")
    set_assistant_settings(
        engine_of(request),
        provider=fournisseur,
        model=modele,
        cap=plafond,
        actor_user_id=user.id,
        now=now_of(request),
        stream=en_direct == "1",
    )
    return _back("réglages de l'assistant enregistrés", "assistant-ia")


@router.post("/securite/ia/categorisation")
def categorization_form(
    request: Request,
    user: User,
    *,
    seuil: Annotated[int, Form()],
    actif: Annotated[str | None, Form()] = None,
) -> Response:
    """Catégorisation automatique par l'IA (ANA-02) : interrupteur et seuil de confiance (%)."""
    try:
        set_categorization_settings(
            engine_of(request),
            enabled=actif == "1",
            threshold=seuil * 10,
            actor_user_id=user.id,
            now=now_of(request),
        )
    except AiCategorizationError as exc:
        return _back(str(exc), "categorisation-ia")
    return _back("catégorisation automatique enregistrée", "categorisation-ia")


@router.get("/assistant", response_class=HTMLResponse)
def assistant_page(request: Request, user: User) -> Response:
    engine = engine_of(request)
    conversation = request.query_params.get("conversation")
    exchanges: list[dict[str, Any]] = []
    error = flash(request, "erreur")
    current: int | None = None
    shown = None
    if conversation and conversation.isdigit():
        try:
            exchanges = history(engine, user.id, int(conversation))
            shown = find_conversation(engine, user.id, int(conversation))
            current = shown.id
        except AssistantError as exc:
            error = str(exc)
    listed = conversations(engine, user.id)
    running = (
        request.app.state.assistant_runs.active(user.id, current) if current is not None else None
    )
    if running and exchanges and exchanges[-1].get("interrupted"):
        exchanges = exchanges[:-1]  # pas « interrompue » : la réponse est en train de s'écrire
    templates = templates_of(request)
    return templates.TemplateResponse(
        request,
        "assistant.html",
        {
            "user": user,
            "exchanges": exchanges,
            "conversation": current,
            "title": shown.title if shown else None,
            "current_pinned": bool(shown and shown.pinned),
            "conversations": listed,
            "running": running,
            "erreur": error,
            "message": flash(request),
        },
    )


def _shown(conversation_id: int, retour: int | None) -> RedirectResponse:
    """Retour à la page affichée : `retour` est la conversation ouverte (0 : aucune), à défaut
    celle qu'on vient de modifier."""
    shown = conversation_id if retour is None else retour
    target = f"/assistant?conversation={shown}" if shown else "/assistant"
    return RedirectResponse(target, status_code=303)


@router.post("/assistant/{conversation_id}/renommer")
def rename_conversation(
    request: Request,
    conversation_id: int,
    user: User,
    titre: Annotated[str, Form()],
    retour: Annotated[int | None, Form()] = None,
) -> Response:
    try:
        rename(engine_of(request), user.id, conversation_id, titre, now=now_of(request))
    except AssistantError as exc:
        return RedirectResponse(f"/assistant?{urlencode({'erreur': str(exc)})}", status_code=303)
    return _shown(conversation_id, retour)


@router.post("/assistant/{conversation_id}/epingler")
def pin_conversation(
    request: Request,
    conversation_id: int,
    user: User,
    epingle: Annotated[bool, Form()],
    retour: Annotated[int | None, Form()] = None,
) -> Response:
    try:
        pin(engine_of(request), user.id, conversation_id, epingle, now=now_of(request))
    except AssistantError as exc:
        return RedirectResponse(f"/assistant?{urlencode({'erreur': str(exc)})}", status_code=303)
    return _shown(conversation_id, retour)


@router.post("/assistant/{conversation_id}/supprimer")
def delete_conversation(
    request: Request,
    conversation_id: int,
    user: User,
    retour: Annotated[int | None, Form()] = None,
) -> Response:
    try:
        delete(engine_of(request), user.id, conversation_id, now=now_of(request))
        query = {"message": "conversation supprimée"}
        if retour and retour != conversation_id:
            query["conversation"] = str(retour)
    except AssistantError as exc:
        query = {"erreur": str(exc)}
    return RedirectResponse(f"/assistant?{urlencode(query)}", status_code=303)


@router.post("/assistant")
def assistant_post(
    request: Request,
    user: User,
    texte: Annotated[str, Form()],
    conversation: Annotated[str, Form()] = "",
) -> Response:
    """Formulaire sans JavaScript : la question est enregistrée, la réponse s'écrit en arrière-
    plan et la page se recharge toute seule tant qu'elle n'est pas terminée."""
    current = int(conversation) if conversation.isdigit() else None
    try:
        _, conversation_id, _ = start_turn(
            request, user_id=user.id, conversation_id=current, text=texte
        )
    except (AssistantError, RunBusyError) as exc:
        query = {"erreur": str(exc), **({"conversation": current} if current else {})}
        return RedirectResponse(f"/assistant?{urlencode(query)}", status_code=303)
    return RedirectResponse(f"/assistant?conversation={conversation_id}#fin", status_code=303)


@router.post("/assistant/messages")
async def assistant_message(request: Request, user: User) -> Response:
    """Envoi depuis la page (JavaScript) : réponse immédiate avec l'identifiant du tour."""
    try:
        body = await request.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    raw = body.get("conversation")
    current = raw if isinstance(raw, int) else None
    try:
        run_id, conversation_id, title = await run_in_threadpool(
            start_turn,
            request,
            user_id=user.id,
            conversation_id=current,
            text=str(body.get("texte", "")),
        )
    except RunBusyError as exc:
        return JSONResponse({"erreur": str(exc)}, status_code=409)
    except AssistantError as exc:
        return JSONResponse({"erreur": str(exc)}, status_code=400)
    return JSONResponse(
        {"tour": run_id, "conversation": conversation_id, "titre": title}, status_code=202
    )


@router.get("/assistant/tours/{run_id}")
def assistant_turn(request: Request, run_id: str, user: User, depuis: int = 0) -> Response:
    view = request.app.state.assistant_runs.view(run_id, user.id, since=depuis)
    if view is None:
        return JSONResponse({"erreur": "Réponse introuvable ou expirée."}, status_code=404)
    return JSONResponse(turn_payload(view, html=True))
