"""Page « Abonnements » (ANA-07) : dépenses et recettes récurrentes, hausses de prix, manques."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response

from app.ledger.subscriptions import recurring_view
from app.timeutil import paris_date
from app.web.cache import DataCache
from app.web.deps import CurrentUser, current_user, engine_of, now_of, templates_of

router = APIRouter()


@router.get("/abonnements", response_class=HTMLResponse)
def subscriptions_page(
    request: Request, user: Annotated[CurrentUser, Depends(current_user)]
) -> Response:
    engine, now = engine_of(request), now_of(request)
    today = paris_date(now)
    cache: DataCache = request.app.state.page_cache
    view = cache.get(engine, ("abonnements", today), lambda: recurring_view(engine, today), now=now)
    return templates_of(request).TemplateResponse(
        request, "subscriptions.html", {"user": user, "today": today, "view": view}
    )
