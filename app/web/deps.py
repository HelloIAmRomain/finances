"""Dépendances FastAPI : utilisateur connecté, protection CSRF, horloge."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlencode

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import Engine

from app.auth.users import session_user
from app.config import Secrets
from app.web.csrf import csrf_token, csrf_valid

SESSION_COOKIE = "session"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class LoginRequiredError(Exception):
    """Aucune session valide : redirection vers la page de connexion."""

    def __init__(self, next_path: str) -> None:
        self.next_path = next_path


@dataclass(frozen=True)
class CurrentUser:
    id: int
    session_token: str
    csrf: str


def engine_of(request: Request) -> Engine:
    engine: Engine = request.app.state.engine
    return engine


def templates_of(request: Request) -> Jinja2Templates:
    templates: Jinja2Templates = request.app.state.templates
    return templates


def now_of(request: Request) -> datetime:
    clock: Callable[[], datetime] = request.app.state.now
    return clock()


async def _submitted_csrf(request: Request) -> str | None:
    header = request.headers.get("X-CSRF-Token")
    if header:
        return header
    if request.headers.get("content-type", "").startswith(
        ("application/x-www-form-urlencoded", "multipart/form-data")
    ):
        value = (await request.form()).get("csrf")  # formulaire mis en cache pour la route
        return value if isinstance(value, str) else None
    return None


async def current_user(request: Request) -> CurrentUser:
    token = request.cookies.get(SESSION_COOKIE)
    user_id = session_user(engine_of(request), token, now=now_of(request))
    if user_id is None or token is None:
        raise LoginRequiredError(request.url.path)
    secrets: Secrets = request.app.state.secrets
    key = secrets.app_secret_key.get_secret_value()
    if request.method not in SAFE_METHODS:
        if not csrf_valid(key, token, await _submitted_csrf(request)):
            raise HTTPException(status_code=403, detail="Jeton CSRF invalide.")
    return CurrentUser(id=user_id, session_token=token, csrf=csrf_token(key, token))


def redirect_with_message(
    path: str, message: str, *, anchor: str | None = None, **params: str
) -> RedirectResponse:
    """Retour à une page après un formulaire (303), le message affiché en haut ; texte encodé
    (accents, « & », « # » compris) et section visée par `anchor`."""
    query = urlencode({"message": message, **params})
    return RedirectResponse(f"{path}?{query}" + (f"#{anchor}" if anchor else ""), status_code=303)


def redirect_with_error(
    path: str, message: str, *, anchor: str | None = None, **params: str
) -> RedirectResponse:
    """Comme `redirect_with_message`, pour un refus : affiché comme une erreur (rôle « alert »,
    en rouge), jamais comme une confirmation."""
    query = urlencode({"erreur": message, **params})
    return RedirectResponse(f"{path}?{query}" + (f"#{anchor}" if anchor else ""), status_code=303)
