"""Page « Prévision » (ANA-10, ALR-07) : solde projeté par compte, point bas, seuil d'alerte."""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response

from app.ledger.forecast import (
    ForecastError,
    check_low_balances,
    forecast,
    set_threshold,
    total_projection,
)
from app.money import InvalidAmountError, parse_optional_user_amount
from app.timeutil import paris_date
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
HORIZONS = (30, 90)


def _amount(text: str) -> int | None:
    try:
        return parse_optional_user_amount(text)
    except InvalidAmountError as exc:
        raise ForecastError(str(exc)) from exc


@router.get("/prevision", response_class=HTMLResponse)
def forecast_page(request: Request, user: User) -> Response:
    today = paris_date(now_of(request))
    requested = request.query_params.get("jours", "")
    days = int(requested) if requested.isdigit() and int(requested) in HORIZONS else HORIZONS[0]
    accounts = forecast(engine_of(request), today, days=days)
    templates = templates_of(request)
    return templates.TemplateResponse(
        request,
        "forecast.html",
        {
            "user": user,
            "message": flash(request),
            "days": days,
            "horizons": HORIZONS,
            "today": today,
            "accounts": accounts,
            "total": total_projection(accounts, today),
            "events": sorted(
                (e for a in accounts for e in a.events), key=lambda e: (e.day, e.amount_cents)
            ),
            "names": {a.account_id: a.name for a in accounts},
        },
    )


@router.post("/prevision/{account_id}/seuil")
def threshold_route(
    request: Request, account_id: int, user: User, seuil: Annotated[str, Form()] = ""
) -> Response:
    engine, now = engine_of(request), now_of(request)
    try:
        set_threshold(engine, account_id, _amount(seuil), actor_user_id=user.id, now=now)
    except ForecastError as exc:
        message = str(exc)
    else:
        check_low_balances(engine, now=now)
        message = "seuil enregistré" if seuil.strip() else "alerte de solde bas désactivée"
    return redirect_with_message("/prevision", message)
