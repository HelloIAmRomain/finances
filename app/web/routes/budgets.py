"""Page « Budgets » (ANA-06) : enveloppes mensuelles par catégorie, dépense du mois, alertes."""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.categorize.budgets import (
    BudgetError,
    average_spending,
    budget_statuses,
    check_budgets,
    expense_category_choices,
    remove_budget,
    set_budget,
)
from app.money import InvalidAmountError, parse_user_amount
from app.months import is_month, month_label, month_of, shift_month
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


def _back(message: str, month: str | None = None) -> RedirectResponse:
    return redirect_with_message("/budgets", message, **({"mois": month} if month else {}))


def _amount(text: str) -> int:
    try:
        return parse_user_amount(text)
    except InvalidAmountError as exc:
        raise BudgetError(str(exc)) from exc


def _suggestions(
    choices: list[tuple[int, str]], averages: dict[int, int], budgeted: set[int]
) -> list[tuple[str, int, int]]:
    """Catégories racines sans budget où l'on dépense le plus en moyenne : (nom, moyenne, id)."""
    found = [
        (label, averages[cid], cid)
        for cid, label in choices
        if cid in averages and cid not in budgeted and " › " not in label
    ]
    return sorted(found, key=lambda s: -s[1])[:5]


@router.get("/budgets", response_class=HTMLResponse)
def budgets_page(request: Request, user: User) -> Response:
    engine = engine_of(request)
    current = month_of(paris_date(now_of(request)))
    requested = request.query_params.get("mois", "")
    month = requested if is_month(requested) else current
    statuses = budget_statuses(engine, month)
    budgeted = {s.category_id for s in statuses}
    averages = average_spending(engine, month)
    choices = expense_category_choices(engine)
    templates = templates_of(request)
    return templates.TemplateResponse(
        request,
        "budgets.html",
        {
            "user": user,
            "message": flash(request),
            "month": month,
            "month_label": month_label(month),
            "is_current": month == current,
            "previous_month": shift_month(month, -1),
            "next_month": shift_month(month, 1),
            "statuses": statuses,
            "total_budget": sum(s.monthly_cents for s in statuses),
            "total_spent": sum(s.spent_cents for s in statuses),
            "choices": [(cid, label) for cid, label in choices if cid not in budgeted],
            "averages": averages,
            "suggestions": _suggestions(choices, averages, budgeted),
        },
    )


@router.post("/budgets")
def set_route(
    request: Request,
    user: User,
    categorie: Annotated[int, Form()],
    montant: Annotated[str, Form()],
) -> Response:
    engine, now = engine_of(request), now_of(request)
    try:
        set_budget(engine, categorie, _amount(montant), actor_user_id=user.id, now=now)
    except BudgetError as exc:
        return _back(str(exc))
    check_budgets(engine, now=now)
    return _back("budget enregistré")


@router.post("/budgets/{budget_id}/supprimer")
def remove_route(request: Request, budget_id: int, user: User) -> Response:
    try:
        remove_budget(engine_of(request), budget_id, actor_user_id=user.id, now=now_of(request))
    except BudgetError as exc:
        return _back(str(exc))
    return _back("budget supprimé")
