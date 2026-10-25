"""Page « Sécurité » et connexion par clé FIDO2 / WebAuthn (NF-SEC-04).

Échanges en JSON avec `static/cle-securite.js` ; en session, le jeton CSRF voyage dans l'en-tête
`X-CSRF-Token`. La connexion par clé redonne le même cookie de session que le code TOTP.
"""

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from app.auth.security_keys import (
    RelyingParty,
    SecurityKeyError,
    decoy_login_options,
    keys,
    login,
    login_options,
    register,
    registration_options,
    remove,
)
from app.auth.users import AuthError, LockedError, open_session
from app.config import Settings
from app.web.deps import (
    SESSION_COOKIE,
    CurrentUser,
    current_user,
    engine_of,
    now_of,
    redirect_with_message,
    templates_of,
)
from app.web.flash import flash, seal_location
from app.web.routes.ai import ai_context
from app.web.routes.auth import safe_next

router = APIRouter()
User = Annotated[CurrentUser, Depends(current_user)]


def _rp(request: Request) -> RelyingParty:
    settings: Settings = request.app.state.settings
    return RelyingParty.from_base_url(settings.public_base_url)


def _refused(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"erreur": message}, status_code=status)


async def _body(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except (ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


@router.get("/securite", response_class=HTMLResponse)
def security_page(request: Request, user: User) -> Response:
    templates = templates_of(request)
    return templates.TemplateResponse(
        request,
        "security.html",
        {
            "user": user,
            "keys": keys(engine_of(request), user.id),
            "message": flash(request),
            **ai_context(engine_of(request), request.app.state.secrets),
        },
    )


@router.post("/securite/cles/options")
async def enrolment_options(request: Request, user: User) -> Response:
    body = await _body(request)
    try:
        options = registration_options(
            engine_of(request),
            user_id=user.id,
            password=str(body.get("mot_de_passe", "")),
            rp=_rp(request),
            now=now_of(request),
        )
    except SecurityKeyError as exc:
        return _refused(str(exc))
    return JSONResponse(json.loads(options))


@router.post("/securite/cles")
async def enrol(request: Request, user: User) -> Response:
    body = await _body(request)
    credential = body.get("credential")
    if not isinstance(credential, dict):
        return _refused("Réponse de la clé manquante.")
    try:
        register(
            engine_of(request),
            user_id=user.id,
            credential=credential,
            name=str(body.get("nom", "")),
            rp=_rp(request),
            now=now_of(request),
        )
    except SecurityKeyError as exc:
        return _refused(str(exc))
    redirection = seal_location(request, "/securite?message=clé+enregistrée")
    return JSONResponse({"message": "Clé enregistrée.", "redirection": redirection})


@router.post("/securite/cles/{key_id}/supprimer")
def delete_key(request: Request, key_id: int, user: User) -> Response:
    try:
        remove(engine_of(request), user_id=user.id, key_id=key_id, now=now_of(request))
        message = "clé retirée"
    except SecurityKeyError as exc:
        message = str(exc)
    return redirect_with_message("/securite", message)


@router.post("/connexion/cle/options")
async def key_login_options(request: Request) -> Response:
    body = await _body(request)
    try:
        options = login_options(
            engine_of(request),
            username=str(body.get("identifiant", "")),
            password=str(body.get("mot_de_passe", "")),
            rp=_rp(request),
            now=now_of(request),
        )
    except LockedError as exc:  # même réponse quel que soit le mot de passe
        return _refused(str(exc), 401)
    except (AuthError, SecurityKeyError):
        # Mot de passe faux ou compte sans clé : réponse identique à un succès (échec compté
        # par l'anti-force brute), pour ne pas valider un mot de passe sans second facteur.
        options = decoy_login_options(_rp(request))
    return JSONResponse(json.loads(options))


@router.post("/connexion/cle")
async def key_login(request: Request) -> Response:
    body = await _body(request)
    credential = body.get("credential")
    if not isinstance(credential, dict):
        return _refused("Réponse de la clé manquante.")
    engine, now = engine_of(request), now_of(request)
    try:
        user_id = login(engine, credential=credential, rp=_rp(request), now=now)
    except SecurityKeyError as exc:
        return _refused(str(exc), 401)
    response = JSONResponse({"redirection": safe_next(str(body.get("suivant", "/")))})
    response.set_cookie(
        SESSION_COOKIE,
        open_session(engine, user_id, now=now),
        httponly=True,
        secure=True,
        samesite="strict",
        path="/",
    )
    return response
