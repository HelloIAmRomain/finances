"""En-têtes de sécurité HTTP : aucune ressource externe, aucun cadre, aucune mise en cache ;
messages des redirections scellés (`app.web.flash`) ; taille des envois bornée."""

import logging
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from fastapi.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.ledger.imports import MAX_FILE_BYTES
from app.web.flash import seal_location

LOGGER = logging.getLogger(__name__)
UNEXPECTED_ERROR = (
    "Erreur inattendue : réessayez dans un instant. Si elle revient, la marche à suivre est dans "
    "le guide d'exploitation (rubrique « Dépannage »)."
)

CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "connect-src 'self'",
        "font-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    )
)

SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Strict-Transport-Security": "max-age=31536000",
    "Cross-Origin-Opener-Policy": "same-origin",
}


async def security_headers(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    try:
        response = await call_next(request)
    except Exception:
        # Réponse d'erreur produite ici pour qu'elle porte aussi les en-têtes ci-dessous (sinon
        # Starlette répond plus haut, sans eux). Valeurs SQL masquées (hide_parameters, inv. 5).
        LOGGER.exception("erreur inattendue : %s %s", request.method, request.url.path)
        response = PlainTextResponse(UNEXPECTED_ERROR, status_code=500)
    response.headers.update(SECURITY_HEADERS)
    if "location" in response.headers:
        response.headers["location"] = seal_location(request, response.headers["location"])
    if not request.url.path.startswith("/static/"):
        # Pages de données financières : jamais conservées par le navigateur ni un proxy.
        response.headers["Cache-Control"] = "no-store"
    return response


# Le plus gros envoi légitime est un relevé importé (5 Mo) avec l'enveloppe du formulaire.
MAX_REQUEST_BYTES = MAX_FILE_BYTES + 1024 * 1024
TOO_LARGE = (
    "Envoi trop volumineux (6 Mo au plus) : pour un relevé, exportez une période plus courte "
    "depuis la banque."
)


class _TooLargeError(Exception):
    pass


class BodySizeLimit:
    """Refuse (413) toute requête de plus de `max_bytes`, annoncée ou constatée en lisant le
    corps : FastAPI lit les formulaires avant de vérifier la session, un envoi géant d'un appareil
    du tailnet remplirait sinon le disque avant le refus."""

    def __init__(self, app: ASGIApp, max_bytes: int = MAX_REQUEST_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = dict(scope["headers"]).get(b"content-length", b"0")
        if not declared.isdigit() or int(declared) > self.max_bytes:
            await PlainTextResponse(TOO_LARGE, status_code=413)(scope, receive, send)
            return
        received = 0
        exceeded = started = False

        async def limited_receive() -> Message:
            nonlocal received, exceeded
            message = await receive()
            received += len(message.get("body", b""))
            if received > self.max_bytes:
                exceeded = True
                raise _TooLargeError
            return message

        async def guarded_send(message: Message) -> None:
            # L'application transforme l'arrêt de lecture en erreur 400 : remplacée par le 413.
            nonlocal started
            if exceeded and not started:
                return
            started = started or message["type"] == "http.response.start"
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except _TooLargeError:
            pass
        if exceeded and not started:
            await PlainTextResponse(TOO_LARGE, status_code=413)(scope, receive, send)
