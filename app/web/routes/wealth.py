"""Page « Patrimoine » (COL-05, ANA-09) : comptes, actifs et dettes saisis, patrimoine net."""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select

from app.ledger.assets import (
    KIND_LABELS,
    AssetError,
    archive_asset,
    assets,
    create_asset,
    record_value,
    wealth,
)
from app.models import Property
from app.models.ledger import AssetKind
from app.money import InvalidAmountError, parse_user_amount
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


def _back(message: str) -> RedirectResponse:
    return redirect_with_message("/patrimoine", message)


def _amount(text: str) -> int:
    try:
        return parse_user_amount(text)
    except InvalidAmountError as exc:
        raise AssetError(str(exc)) from exc


@router.get("/patrimoine", response_class=HTMLResponse)
def wealth_page(request: Request, user: User) -> Response:
    engine = engine_of(request)
    rows = assets(engine)
    with engine.connect() as connection:
        properties = connection.execute(
            select(Property.id, Property.name).where(Property.archived_at.is_(None))
        ).all()
    templates = templates_of(request)
    return templates.TemplateResponse(
        request,
        "wealth.html",
        {
            "user": user,
            "message": flash(request),
            "assets": rows,
            "wealth": wealth(engine, rows),
            "kind_labels": KIND_LABELS,
            "properties": properties,
            "today": paris_date(now_of(request)),
        },
    )


@router.post("/patrimoine")
def create_route(
    request: Request,
    user: User,
    *,
    nom: Annotated[str, Form()],
    nature: Annotated[str, Form()],
    valeur: Annotated[str, Form()] = "",
    bien: Annotated[str, Form()] = "",
    compte: Annotated[str | None, Form()] = None,
) -> Response:
    engine, now = engine_of(request), now_of(request)
    try:
        kind = AssetKind(nature)
    except ValueError:
        return _back("Nature inconnue.")
    try:
        value = _amount(valeur) if valeur.strip() else None
        asset_id = create_asset(
            engine,
            name=nom,
            kind=kind,
            counted=compte is not None,
            property_id=int(bien) if bien.isdigit() else None,
            actor_user_id=user.id,
            now=now,
        )
        if value is not None:
            record_value(
                engine,
                asset_id,
                as_of=paris_date(now),
                value_cents=value,
                actor_user_id=user.id,
                now=now,
            )
    except AssetError as exc:
        return _back(str(exc))
    return _back("actif ajouté")


@router.post("/patrimoine/{asset_id}/valeur")
def value_route(
    request: Request,
    asset_id: int,
    user: User,
    *,
    valeur: Annotated[str, Form()] = "",
    date_valeur: Annotated[date | None, Form()] = None,
) -> Response:
    now = now_of(request)
    try:
        record_value(
            engine_of(request),
            asset_id,
            as_of=date_valeur or paris_date(now),
            value_cents=_amount(valeur),
            actor_user_id=user.id,
            now=now,
        )
    except AssetError as exc:
        return _back(str(exc))
    return _back("valeur enregistrée")


@router.post("/patrimoine/{asset_id}/archiver")
def archive_route(request: Request, asset_id: int, user: User) -> Response:
    try:
        archive_asset(engine_of(request), asset_id, actor_user_id=user.id, now=now_of(request))
    except AssetError as exc:
        return _back(str(exc))
    return _back("actif archivé")
