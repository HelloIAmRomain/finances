"""Page « Règles » : règles de catégorisation par priorité (ANA-01), modifications auditées."""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel, ValidationError
from sqlalchemy import select

from app.categorize.rules import RuleConditions
from app.categorize.service import create_rule, deactivate_rule, update_rule
from app.ledger.search import category_choices
from app.models import Rule
from app.money import Cents, InvalidAmountError, format_eur, parse_optional_user_amount
from app.web.deps import CurrentUser, current_user, engine_of, now_of, templates_of

router = APIRouter()


def describe(conditions: RuleConditions) -> str:
    """Conditions en clair, pour l'affichage."""
    parts = []
    if conditions.label_contains:
        parts.append(
            "libellé contient " + " ou ".join(f"« {f} »" for f in conditions.label_contains)
        )
    if conditions.label_regex:
        parts.append(f"libellé correspond à /{conditions.label_regex}/")
    if conditions.counterparty_contains:
        parts.append(f"contrepartie contient « {conditions.counterparty_contains} »")
    if conditions.direction:
        parts.append("dépense" if conditions.direction == "debit" else "recette")
    if conditions.amount_min is not None:
        parts.append(f"montant ≥ {format_eur(Cents(conditions.amount_min))}")
    if conditions.amount_max is not None:
        parts.append(f"montant ≤ {format_eur(Cents(conditions.amount_max))}")
    if conditions.account_ids:
        parts.append(f"comptes {', '.join(map(str, conditions.account_ids))}")
    return " et ".join(parts)


def _page(
    request: Request, user: CurrentUser, error: str | None = None, status: int = 200
) -> Response:
    engine = engine_of(request)
    with engine.connect() as connection:
        rules = connection.execute(
            select(Rule).order_by(Rule.is_active.desc(), Rule.priority, Rule.id)
        ).all()
    categories = category_choices(engine)
    templates = templates_of(request)
    return templates.TemplateResponse(
        request,
        "rules.html",
        {
            "user": user,
            "rules": [
                (rule, describe(RuleConditions.model_validate(rule.conditions))) for rule in rules
            ],
            "categories": categories,
            "category_names": dict(categories),
            "erreur": error,
        },
        status_code=status,
    )


@router.get("/regles", response_class=HTMLResponse)
def rules_page(request: Request, user: Annotated[CurrentUser, Depends(current_user)]) -> Response:
    return _page(request, user)


class RuleForm(BaseModel):
    nom: str
    priorite: int
    categorie: int
    contient: str = ""
    expression: str = ""
    sens: str = ""
    montant_min: str = ""
    montant_max: str = ""
    csrf: str = ""


@router.post("/regles", response_class=HTMLResponse)
def new_rule(
    request: Request,
    user: Annotated[CurrentUser, Depends(current_user)],
    form: Annotated[RuleForm, Form()],
) -> Response:
    try:
        conditions = {
            "label_contains": [f.strip() for f in form.contient.split(",") if f.strip()] or None,
            "label_regex": form.expression.strip() or None,
            "direction": form.sens or None,
            "amount_min": parse_optional_user_amount(form.montant_min),
            "amount_max": parse_optional_user_amount(form.montant_max),
        }
        create_rule(
            engine_of(request),
            name=form.nom.strip() or "Règle",
            priority=form.priorite,
            conditions={k: v for k, v in conditions.items() if v is not None},
            category_id=form.categorie,
            actor_user_id=user.id,
            now=now_of(request),
        )
    except (ValidationError, InvalidAmountError, ValueError) as exc:
        message = exc.errors()[0]["msg"] if isinstance(exc, ValidationError) else str(exc)
        return _page(request, user, error=f"Règle refusée : {message}", status=422)
    return RedirectResponse("/regles", status_code=303)


@router.post("/regles/{rule_id}/modifier")
def edit_rule(
    request: Request,
    rule_id: int,
    *,
    user: Annotated[CurrentUser, Depends(current_user)],
    priorite: Annotated[int, Form()],
    categorie: Annotated[int, Form()],
) -> Response:
    update_rule(
        engine_of(request),
        rule_id,
        priority=priorite,
        category_id=categorie,
        actor_user_id=user.id,
        now=now_of(request),
    )
    return RedirectResponse("/regles", status_code=303)


@router.post("/regles/{rule_id}/desactiver")
def disable_rule(
    request: Request, rule_id: int, user: Annotated[CurrentUser, Depends(current_user)]
) -> Response:
    deactivate_rule(engine_of(request), rule_id, actor_user_id=user.id, now=now_of(request))
    return RedirectResponse("/regles", status_code=303)
