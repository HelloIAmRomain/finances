"""Connexion et déconnexion (NF-SEC-04)."""

from typing import Annotated
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.auth.users import AuthError, authenticate, close_session, open_session
from app.web.deps import SESSION_COOKIE, CurrentUser, current_user, engine_of, now_of, templates_of
from app.web.flash import FLASH_PARAMS, SEAL_PARAM

router = APIRouter()
FIRST_PRINTABLE, DELETE = 0x20, 0x7F


def safe_next(path: str | None) -> str:
    """Redirection interne uniquement : un chemin absolu de l'app, jamais « //hôte », URL absolue,
    barre oblique inverse ni caractère de contrôle ou espace (le navigateur retire tabulations et
    retours à la ligne : « /\t/hôte » deviendrait « //hôte »).

    Les messages de page (`message`, `erreur`) sont retirés : la redirection après connexion est
    scellée comme une adresse de l'application, un lien piégé ferait sinon afficher son texte
    comme un avertissement authentique."""
    if not (
        path
        and path.startswith("/")
        and not path.startswith("//")
        and "\\" not in path
        and all(FIRST_PRINTABLE < ord(char) != DELETE for char in path)
    ):
        return "/"
    parts = urlsplit(path)
    query = parse_qsl(parts.query, keep_blank_values=True)
    kept = [(name, value) for name, value in query if name not in (*FLASH_PARAMS, SEAL_PARAM)]
    if len(kept) == len(query):
        return path
    return urlunsplit(("", "", parts.path, urlencode(kept), parts.fragment))


@router.get("/connexion", response_class=HTMLResponse)
def login_form(request: Request, suivant: str = "/") -> Response:
    templates = templates_of(request)
    return templates.TemplateResponse(
        request, "login.html", {"suivant": safe_next(suivant), "erreur": None}
    )


@router.post("/connexion", response_class=HTMLResponse)
def login(
    request: Request,
    identifiant: Annotated[str, Form()],
    mot_de_passe: Annotated[str, Form()],
    code: Annotated[str, Form()],
    suivant: Annotated[str, Form()] = "/",
) -> Response:
    engine, now = engine_of(request), now_of(request)
    try:
        user_id = authenticate(
            engine, username=identifiant, password=mot_de_passe, totp_code=code, now=now
        )
    except AuthError as exc:
        templates = templates_of(request)
        return templates.TemplateResponse(
            request,
            "login.html",
            {"suivant": safe_next(suivant), "erreur": str(exc)},
            status_code=401,
        )
    response = RedirectResponse(safe_next(suivant), status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        open_session(engine, user_id, now=now),
        httponly=True,
        secure=True,
        samesite="strict",
        path="/",
    )
    return response


@router.post("/deconnexion")
def logout(request: Request, user: Annotated[CurrentUser, Depends(current_user)]) -> Response:
    close_session(engine_of(request), user.session_token, now=now_of(request))
    response = RedirectResponse("/connexion", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="strict")
    return response
