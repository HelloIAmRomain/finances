"""Page « Comptes » : état des connexions, fraîcheur, soldes ; création de compte (COL-07, O1)."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.connectors.budget import (
    ManualSyncRefusedError,
    check_manual_sync,
    manual_sync_blocker,
    usage,
)
from app.connectors.enable_banking.connector import EnableBankingConnector
from app.ledger.accounts import (
    AccountError,
    accounts_overview,
    create_file_account,
    describe_account,
    rename_account,
    update_account_settings,
)
from app.ledger.sync import SyncDeferred, SyncError, sync_account
from app.models.ledger import AccountType, ConnectionStatus
from app.ops.checks import SYNC_STALE_AFTER
from app.rental.engine import run_rental
from app.timeutil import ensure_utc
from app.web.deps import (
    CurrentUser,
    current_user,
    engine_of,
    now_of,
    redirect_with_message,
    templates_of,
)
from app.web.flash import flash
from app.web.routes.connections import BANK_ERRORS, bank_of

router = APIRouter()

ENDPOINT_LABELS = {"balances": "soldes", "transactions": "transactions"}
ACCOUNT_TYPES = {
    AccountType.CHECKING: "Compte courant",
    AccountType.SAVINGS: "Livret",
    AccountType.CARD: "Carte",
    AccountType.OTHER: "Autre",
}


def _page(
    request: Request, user: CurrentUser, error: str | None = None, status: int = 200
) -> Response:
    templates = templates_of(request)
    engine, now = engine_of(request), now_of(request)
    accounts = accounts_overview(engine)
    blockers, budgets, stale = {}, {}, set()
    for account in accounts:
        if account.ignored or account.connection_status is not ConnectionStatus.ACTIVE:
            continue
        refusal = manual_sync_blocker(engine, account_id=account.id, now=now)
        if refusal is not None:
            blockers[account.id] = str(refusal)
        budgets[account.id] = usage(engine, account_id=account.id, now=now)
        last = account.last_success_at
        if last is None or now - ensure_utc(last) > SYNC_STALE_AFTER:
            stale.add(account.id)
    return templates.TemplateResponse(
        request,
        "accounts.html",
        {
            "user": user,
            "accounts": accounts,
            "sync_blockers": blockers,
            "budgets": budgets,
            "stale": stale,
            "endpoint_labels": ENDPOINT_LABELS,
            "account_types": ACCOUNT_TYPES,
            "now": now,
            "erreur": error,
            "message": flash(request),
        },
        status_code=status,
    )


@router.get("/comptes", response_class=HTMLResponse)
def accounts_page(
    request: Request, user: Annotated[CurrentUser, Depends(current_user)]
) -> Response:
    return _page(request, user)


@router.post("/comptes", response_class=HTMLResponse)
def create_account(
    request: Request,
    *,
    user: Annotated[CurrentUser, Depends(current_user)],
    nom: Annotated[str, Form()],
    type: Annotated[AccountType, Form()],
    iban: Annotated[str, Form()] = "",
    patrimoine: Annotated[bool, Form()] = False,
) -> Response:
    try:
        create_file_account(
            engine_of(request),
            name=nom,
            type=type,
            iban=iban,
            include_in_net_worth=patrimoine,
            actor_user_id=user.id,
            now=now_of(request),
        )
    except AccountError as exc:
        return _page(request, user, error=str(exc), status=422)
    return RedirectResponse("/comptes", status_code=303)


@router.post("/comptes/{account_id}/reglages")
def account_settings(
    request: Request,
    account_id: int,
    *,
    user: Annotated[CurrentUser, Depends(current_user)],
    suivi: Annotated[Literal["compte", "consultation", "ignore"], Form()],
    synchros: Annotated[int, Form()],
    nom: Annotated[str, Form()] = "",
    role: Annotated[str | None, Form()] = None,
) -> Response:
    """Compté, consultation seule (compte joint, carte) ou ignoré (Q19) ; synchros par jour."""
    try:
        if nom.strip():
            rename_account(
                engine_of(request), account_id, nom, actor_user_id=user.id, now=now_of(request)
            )
        if role is not None:
            describe_account(
                engine_of(request), account_id, role, actor_user_id=user.id, now=now_of(request)
            )
        update_account_settings(
            engine_of(request),
            account_id,
            counted=suivi == "compte",
            ignored=suivi == "ignore",
            syncs_per_day=synchros,
            actor_user_id=user.id,
            now=now_of(request),
        )
    except AccountError as exc:
        return _page(request, user, error=str(exc), status=422)
    message = "réglages enregistrés"
    return redirect_with_message("/comptes", message)


@router.post("/comptes/{account_id}/synchroniser")
def manual_sync(
    request: Request, account_id: int, user: Annotated[CurrentUser, Depends(current_user)]
) -> Response:
    """Synchro à la demande, une par quart d'heure et par compte ; elle consomme le budget DSP2
    et n'est pas lancée s'il ne reste qu'une requête sur 24 h (gardée pour le créneau suivant)."""
    bank = bank_of(request)
    engine, now = engine_of(request), now_of(request)
    message = "mise à jour terminée"
    try:
        if bank is None:
            raise SyncError("liaison bancaire non configurée sur le serveur")
        check_manual_sync(engine, account_id=account_id, now=now)
        result = sync_account(
            engine, EnableBankingConnector(bank), account_id, unattended=False, now=now
        )
        if isinstance(result, SyncDeferred):
            message = f"mise à jour {result.reason}"
        else:
            run_rental(engine, now=now)  # loyers rapprochés sans attendre le worker
    except ManualSyncRefusedError as exc:
        message = str(exc)
    except BANK_ERRORS as exc:
        message = f"échec : {exc}"
    return redirect_with_message("/comptes", message)
