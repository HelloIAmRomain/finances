"""Page « Connexions bancaires » : lier une banque, retour, révocation (COL-01, COL-07)."""

import re
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select

from app.config import Secrets, Settings
from app.connectors.budget import BudgetExceededError
from app.connectors.enable_banking.client import EnableBankingClient, EnableBankingError
from app.connectors.enable_banking.linking import (
    LinkingError,
    complete_link,
    consent_validity,
    issue_state,
    read_state,
    revoke,
)
from app.connectors.requester import BankRateLimitedError, BankUnavailableError, CircuitOpenError
from app.ledger.sync import SyncError
from app.models import Connection
from app.web.deps import CurrentUser, current_user, engine_of, now_of, templates_of
from app.web.flash import flash

router = APIRouter()
REVOKE_FAILED = (
    "La banque n'a pas confirmé le retrait de l'accès. Réessayez plus tard ; sinon, l'accès "
    "prendra fin de lui-même à sa date d'expiration (affichée ci-dessous)."
)
LINK_FAILED = (
    "La banque ou le service de connexion bancaire n'a pas répondu : aucune connexion n'a été "
    "créée. Réessayez dans quelques minutes."
)
ERROR_CODE = re.compile(r"[A-Za-z_]{1,40}")
STATE_COOKIE = "liaison"
COUNTRY = "FR"
BANK_ERRORS = (
    EnableBankingError,
    BankUnavailableError,
    BankRateLimitedError,
    CircuitOpenError,
    BudgetExceededError,
    SyncError,
)


def bank_of(request: Request) -> EnableBankingClient | None:
    bank: EnableBankingClient | None = request.app.state.bank
    return bank


def _secret_key(request: Request) -> str:
    secrets: Secrets = request.app.state.secrets
    return secrets.app_secret_key.get_secret_value()


def _render(
    request: Request, template: str, context: dict[str, object], status: int = 200
) -> Response:
    templates = templates_of(request)
    return templates.TemplateResponse(request, template, context, status_code=status)


@router.get("/connexions", response_class=HTMLResponse)
def connections_page(
    request: Request, user: Annotated[CurrentUser, Depends(current_user)]
) -> Response:
    with engine_of(request).connect() as connection:
        connections = connection.execute(
            select(Connection).order_by(Connection.created_at.desc())
        ).all()
    bank = bank_of(request)
    banks: list[str] = []
    error = None
    if bank is not None:
        try:
            banks = sorted(str(b["name"]) for b in bank.aspsps(COUNTRY))
        except BANK_ERRORS as exc:
            error = f"Liste des banques indisponible : {exc}"
    return _render(
        request,
        "connections.html",
        {
            "user": user,
            "connections": connections,
            "banks": banks,
            "configured": bank is not None,
            "erreur": flash(request, "erreur") or error,  # résultat de l'action avant tout
        },
    )


@router.post("/connexions/lier")
def start_link(
    request: Request,
    user: Annotated[CurrentUser, Depends(current_user)],
    banque: Annotated[str, Form()],
) -> Response:
    bank = bank_of(request)
    if bank is None:
        return RedirectResponse("/connexions", status_code=303)
    now = now_of(request)
    try:
        aspsp = next((b for b in bank.aspsps(COUNTRY) if b.get("name") == banque), None)
    except BANK_ERRORS:
        return _back_with_error(LINK_FAILED)
    state, token = issue_state(
        engine_of(request),
        _secret_key(request),
        aspsp=banque,
        country=COUNTRY,
        user_id=user.id,
        now=now,
    )
    settings: Settings = request.app.state.settings
    try:
        url = bank.start_authorization(
            aspsp_name=banque,
            country=COUNTRY,
            valid_until=now + consent_validity(aspsp),
            state=state,
            redirect_url=f"{settings.public_base_url.rstrip('/')}/connexions/retour",
        )
    except BANK_ERRORS:  # le jeton d'état émis expire seul (15 min) : rien à nettoyer
        return _back_with_error(LINK_FAILED)
    if not url.startswith("https://"):
        # Adresse fournie par l'agrégateur : jamais de lien autre que HTTPS (ni `javascript:`).
        return _back_with_error("Adresse de la banque invalide : réessayer.")
    # Pas de redirection HTTP vers la banque : la CSP `form-action 'self'` interdit à un formulaire
    # d'aboutir sur un autre site. Une page intermédiaire y mène (rafraîchissement ou lien).
    response = _render(request, "link_redirect.html", {"url": url, "banque": banque})
    # SameSite=Lax : le cookie doit revenir avec la redirection de la banque (navigation externe).
    response.set_cookie(
        STATE_COOKIE,
        token,
        max_age=900,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/connexions",
    )
    return response


@router.get("/connexions/retour", response_class=HTMLResponse)
def link_callback(request: Request, code: str = "", state: str = "", error: str = "") -> Response:
    """Retour depuis la banque : sans session (cookie Strict), authentifié par le jeton signé."""
    bank = bank_of(request)
    now = now_of(request)
    try:
        if error:
            # Page accessible sans session : seul un code technique court est repris, jamais un
            # texte libre (sinon un lien piégé ferait afficher n'importe quel message ici).
            code_shown = f" (code {error})" if ERROR_CODE.fullmatch(error) else ""
            raise LinkingError(
                f"La banque a refusé ou annulé l'autorisation{code_shown}. Relancez la connexion"
                " depuis la page Connexions si vous voulez réessayer."
            )
        pending = read_state(
            engine_of(request),
            _secret_key(request),
            request.cookies.get(STATE_COOKIE),
            state,
            now=now,
        )
        if bank is None or not code:
            raise LinkingError("Retour incomplet de la banque.")
        complete_link(engine_of(request), bank, pending, code, now=now)
    except (LinkingError, *BANK_ERRORS) as exc:
        response = _render(request, "link_error.html", {"message": str(exc)}, status=400)
    else:
        response = RedirectResponse("/connexions", status_code=303)
    response.delete_cookie(STATE_COOKIE, path="/connexions")
    return response


@router.post("/connexions/{connection_id}/revoquer")
def revoke_connection(
    request: Request, connection_id: int, user: Annotated[CurrentUser, Depends(current_user)]
) -> Response:
    bank = bank_of(request)
    if bank is not None:
        try:
            revoke(
                engine_of(request), bank, connection_id, actor_user_id=user.id, now=now_of(request)
            )
        except LinkingError as exc:
            return _back_with_error(str(exc))
        except BANK_ERRORS:
            return _back_with_error(REVOKE_FAILED)
    return RedirectResponse("/connexions", status_code=303)


def _back_with_error(message: str) -> Response:
    return RedirectResponse(f"/connexions?{urlencode({'erreur': message})}", status_code=303)
