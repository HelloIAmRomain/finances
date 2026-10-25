"""Tableau de bord (ANA-09) : patrimoine, mois choisi et comparaison N-1, 12 mois, catégories."""

from datetime import date, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import Engine

from app.categorize.budgets import budget_statuses
from app.ledger.assets import wealth
from app.months import is_month, month_label, month_of, shift_month
from app.overview import month_overview
from app.rental.views import STATE_LABELS
from app.tax.calendar import CalendarEvent, calendar
from app.timeutil import paris_date
from app.web.cache import DataCache
from app.web.deps import CurrentUser, current_user, engine_of, now_of, templates_of

router = APIRouter()
DEADLINE_HORIZON = timedelta(days=30)


def _next_deadlines(engine: Engine, today: date) -> list[CalendarEvent]:
    """Échéances en retard ou des 30 prochains jours (FIS-04), 4 au plus."""
    return [event for event in calendar(engine, today, horizon=DEADLINE_HORIZON) if not event.done][
        :4
    ]


def _dashboard_data(engine: Engine, month: str, today: date) -> dict[str, Any]:
    """Tout ce que la page calcule : mis en cache entre deux écritures en base."""
    overview = month_overview(engine, month, today)
    return {
        "month": month,
        "month_label": month_label(month),
        "previous_month": shift_month(month, -1),
        "next_month": shift_month(month, 1),
        "current": overview.current,
        "previous_year": overview.previous_year,
        "previous_year_label": month_label(overview.previous_year.month),
        "categories": overview.categories,
        "worth": overview.worth,
        "wealth": wealth(engine),
        "rents": overview.rents,
        "state_labels": STATE_LABELS,
        "deadlines": _next_deadlines(engine, today),
        "budgets": budget_statuses(engine, month)[:4],
        "today": today,
        "chart_data": {
            "mois": [month_label(f.month) for f in overview.flows],
            "recettes": [f.income for f in overview.flows],
            "depenses": [-f.expenses for f in overview.flows],
            "categories": [name for name, _ in overview.categories],
            "montants_categories": [-total for _, total in overview.categories],
        },
    }


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, user: Annotated[CurrentUser, Depends(current_user)]) -> Response:
    engine, now = engine_of(request), now_of(request)
    today = paris_date(now)
    requested = request.query_params.get("mois", "")
    month = requested if is_month(requested) else month_of(today)
    cache: DataCache = request.app.state.page_cache
    data = cache.get(
        engine, ("dashboard", month, today), lambda: _dashboard_data(engine, month, today), now=now
    )
    templates = templates_of(request)
    return templates.TemplateResponse(request, "dashboard.html", {"user": user, **data})
