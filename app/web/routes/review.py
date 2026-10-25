"""Page « À valider » : doublons probables, virements proposés, conflits ; annulation des
doublons reconnus (Q5, T1.16b, COL-06). Chaque décision est auditée."""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select

from app.categorize.review import (
    KIND_LABELS,
    ReviewError,
    decide_ai_suggestion,
    dismiss_ai_suggestion,
    keep_current_transfer,
    open_items,
    resolve_category_conflict,
)
from app.categorize.service import create_rule_from_transaction
from app.ledger.duplicates import DuplicateDecisionError, confirm_link, reject_link, revert_link
from app.ledger.search import category_choices
from app.ledger.transfers import TransferDecisionError, decide_transfer
from app.models import DuplicateLink
from app.models.ledger import DuplicateStatus
from app.web.deps import CurrentUser, current_user, engine_of, now_of, templates_of

router = APIRouter()
RECENT_DUPLICATES = 20
_ERRORS = (ReviewError, DuplicateDecisionError, TransferDecisionError)


def _back() -> Response:
    return RedirectResponse("/a-valider", status_code=303)


@router.get("/a-valider", response_class=HTMLResponse)
def review_page(request: Request, user: Annotated[CurrentUser, Depends(current_user)]) -> Response:
    engine = engine_of(request)
    with engine.connect() as connection:
        recent = connection.execute(
            select(DuplicateLink)
            .where(DuplicateLink.status == DuplicateStatus.CONFIRMED)
            .order_by(DuplicateLink.decided_at.desc(), DuplicateLink.id.desc())
            .limit(RECENT_DUPLICATES)
        ).all()
    templates = templates_of(request)
    return templates.TemplateResponse(
        request,
        "review.html",
        {
            "user": user,
            "items": open_items(engine),
            "labels": KIND_LABELS,
            "recent": recent,
            "categories": category_choices(engine),
        },
    )


@router.post("/a-valider/doublons/{link_id}/{decision}")
def duplicate_decision(
    request: Request,
    link_id: int,
    decision: str,
    user: Annotated[CurrentUser, Depends(current_user)],
) -> Response:
    actions = {"confirmer": confirm_link, "rejeter": reject_link, "annuler": revert_link}
    action = actions.get(decision)
    if action is not None:
        try:
            action(engine_of(request), link_id, actor_user_id=user.id, now=now_of(request))
        except _ERRORS:
            pass
    return _back()


@router.post("/a-valider/virements/{link_id}/{decision}")
def transfer_decision(
    request: Request,
    link_id: int,
    decision: str,
    user: Annotated[CurrentUser, Depends(current_user)],
) -> Response:
    if decision in {"confirmer", "rejeter"}:
        try:
            decide_transfer(
                engine_of(request),
                link_id,
                confirm=decision == "confirmer",
                actor_user_id=user.id,
                now=now_of(request),
            )
        except _ERRORS:
            pass
    return _back()


@router.post("/a-valider/conflits/{item_id}/categorie")
def category_conflict(
    request: Request,
    item_id: int,
    user: Annotated[CurrentUser, Depends(current_user)],
    choix: Annotated[str, Form()],
) -> Response:
    try:
        resolve_category_conflict(
            engine_of(request),
            item_id,
            take_duplicate=choix == "doublon",
            actor_user_id=user.id,
            now=now_of(request),
        )
    except _ERRORS:
        pass
    return _back()


@router.post("/a-valider/conflits/{item_id}/virement")
def transfer_conflict(
    request: Request, item_id: int, user: Annotated[CurrentUser, Depends(current_user)]
) -> Response:
    try:
        keep_current_transfer(
            engine_of(request), item_id, actor_user_id=user.id, now=now_of(request)
        )
    except _ERRORS:
        pass
    return _back()


@router.post("/a-valider/suggestions/{item_id}")
def ai_suggestion(
    request: Request,
    item_id: int,
    user: Annotated[CurrentUser, Depends(current_user)],
    *,
    decision: Annotated[str, Form()],
    categorie: Annotated[str, Form()] = "",
    regle: Annotated[str | None, Form()] = None,
) -> Response:
    """Catégorie de l'IA acceptée, autre catégorie choisie, ou opération laissée telle quelle ;
    avec la case « règle », une règle est créée pour les opérations semblables (ANA-03)."""
    engine, now = engine_of(request), now_of(request)
    try:
        if decision == "ignorer":
            dismiss_ai_suggestion(engine, item_id, actor_user_id=user.id, now=now)
            return _back()
        chosen = int(categorie) if decision == "autre" and categorie.isdigit() else None
        if decision == "autre" and chosen is None:
            return _back()
        transaction_id = decide_ai_suggestion(
            engine, item_id, category_id=chosen, actor_user_id=user.id, now=now
        )
    except _ERRORS:
        return _back()
    if regle is not None:
        create_rule_from_transaction(engine, transaction_id, actor_user_id=user.id, now=now)
    return _back()
