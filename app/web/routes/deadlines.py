"""Pages « Échéances » : taxe foncière, CFE, déclaration, consentements bancaires, baux
(FIS-04, LOC-09, LOC-10, LOC-13, COL-07) ; export vers l'agenda du téléphone (ICS)."""

from datetime import date, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import Engine, select

from app.config import Settings
from app.models import Deadline, LedgerTransaction, Property
from app.models.tax import DeadlineKind
from app.money import InvalidAmountError, parse_optional_user_amount
from app.tax.calendar import calendar
from app.tax.deadlines import (
    DEFAULT_REMIND_DAYS,
    KIND_LABELS,
    DeadlineError,
    DeadlineInput,
    apply_templates,
    candidates,
    create,
    delete_deadline,
    mark_done,
    match_payments,
    reopen,
    update_deadline,
)
from app.tax.ics import to_ics
from app.timeutil import paris_date
from app.web.deps import (
    CurrentUser,
    current_user,
    engine_of,
    now_of,
    redirect_with_error,
    redirect_with_message,
    templates_of,
)
from app.web.flash import flash

router = APIRouter()
User = Annotated[CurrentUser, Depends(current_user)]
CATEGORY_LABELS = {"fiscal": "Impôts et charges", "bail": "Bail", "banque": "Banque"}


def _amount(text: str) -> int | None:
    try:
        return parse_optional_user_amount(text)
    except InvalidAmountError as exc:
        raise DeadlineError(str(exc)) from exc


def _properties(engine: Engine) -> list[tuple[int, str]]:
    with engine.connect() as connection:
        return [
            (row.id, row.name)
            for row in connection.execute(
                select(Property.id, Property.name)
                .where(Property.archived_at.is_(None))
                .order_by(Property.name)
            )
        ]


def _input(
    *,
    nature: str,
    echeance: date,
    bien: str,
    precision: str,
    montant: str,
    recuperable: str,
    rappel: int,
    annuelle: str | None,
) -> DeadlineInput:
    try:
        kind = DeadlineKind(nature)
    except ValueError as exc:
        raise DeadlineError("Nature d'échéance inconnue.") from exc
    if bien.strip() and not bien.strip().isdigit():
        raise DeadlineError("Bien introuvable.")
    return DeadlineInput(
        kind=kind,
        due_date=echeance,
        label=precision,
        property_id=int(bien) if bien.strip() else None,
        amount_cents=_amount(montant),
        recoverable_cents=_amount(recuperable),
        remind_days=rappel,
        yearly=annuelle is not None,
    )


Nature = Annotated[str, Form()]
Day = Annotated[date, Form()]
Text = Annotated[str, Form()]
Remind = Annotated[int, Form()]
Yearly = Annotated[str | None, Form()]


@router.get("/echeances", response_class=HTMLResponse)
def deadlines_page(request: Request, user: User) -> Response:
    engine, today = engine_of(request), paris_date(now_of(request))
    events = calendar(engine, today)
    return templates_of(request).TemplateResponse(
        request,
        "deadlines.html",
        {
            "user": user,
            "message": flash(request),
            "today": today,
            "late": [e for e in events if not e.done and e.day < today],
            "upcoming": [e for e in events if not e.done and e.day >= today],
            "done": [e for e in events if e.done],
            "kind_labels": KIND_LABELS,
            "category_labels": CATEGORY_LABELS,
            "properties": _properties(engine),
            "default_remind": DEFAULT_REMIND_DAYS,
        },
    )


@router.post("/echeances")
def create_route(  # noqa: PLR0913 (formulaire d'une échéance)
    request: Request,
    user: User,
    *,
    nature: Nature,
    echeance: Day,
    bien: Text = "",
    precision: Text = "",
    montant: Text = "",
    recuperable: Text = "",
    rappel: Remind = DEFAULT_REMIND_DAYS,
    annuelle: Yearly = None,
) -> Response:
    engine, now = engine_of(request), now_of(request)
    try:
        data = _input(
            nature=nature,
            echeance=echeance,
            bien=bien,
            precision=precision,
            montant=montant,
            recuperable=recuperable,
            rappel=rappel,
            annuelle=annuelle,
        )
        deadline_id = create(engine, data, actor_user_id=user.id, now=now)
    except DeadlineError as exc:
        return redirect_with_error("/echeances", str(exc))
    match_payments(engine, now=now)
    return redirect_with_message(f"/echeances/{deadline_id}", "échéance ajoutée")


@router.post("/echeances/habituelles")
def templates_route(request: Request, user: User) -> Response:
    created = apply_templates(engine_of(request), actor_user_id=user.id, now=now_of(request))
    if not created:
        return redirect_with_message(
            "/echeances", "les échéances habituelles sont déjà au calendrier"
        )
    return redirect_with_message(
        "/echeances", f"{created} échéance(s) ajoutée(s) : vérifiez dates et montants"
    )


@router.get("/echeances.ics")
def ics_route(request: Request, user: User) -> Response:
    engine, now = engine_of(request), now_of(request)
    settings: Settings = request.app.state.settings
    body = to_ics(calendar(engine, paris_date(now)), base_url=settings.public_base_url, now=now)
    return Response(
        body,
        media_type="text/calendar; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="echeances.ics"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/echeances/{deadline_id}", response_class=HTMLResponse)
def deadline_page(request: Request, deadline_id: int, user: User) -> Response:
    engine = engine_of(request)
    with engine.connect() as connection:
        deadline = connection.execute(select(Deadline).where(Deadline.id == deadline_id)).first()
        payment = (
            connection.execute(
                select(LedgerTransaction).where(LedgerTransaction.id == deadline.transaction_id)
            ).first()
            if deadline is not None and deadline.transaction_id is not None
            else None
        )
    if deadline is None:
        return redirect_with_message("/echeances", "échéance introuvable")
    today = paris_date(now_of(request))
    return templates_of(request).TemplateResponse(
        request,
        "deadline_detail.html",
        {
            "user": user,
            "message": flash(request),
            "d": deadline,
            "payment": payment,
            "days_left": (deadline.due_date - today).days,
            "candidates": [] if deadline.done_at else candidates(engine, deadline_id),
            "kind_labels": KIND_LABELS,
            "properties": _properties(engine),
            "window_start": deadline.due_date - timedelta(days=60),
            "window_end": deadline.due_date + timedelta(days=30),
        },
    )


@router.post("/echeances/{deadline_id}/modifier")
def update_route(  # noqa: PLR0913 (formulaire d'une échéance)
    request: Request,
    deadline_id: int,
    user: User,
    *,
    nature: Nature,
    echeance: Day,
    bien: Text = "",
    precision: Text = "",
    montant: Text = "",
    recuperable: Text = "",
    rappel: Remind = DEFAULT_REMIND_DAYS,
    annuelle: Yearly = None,
) -> Response:
    engine, now = engine_of(request), now_of(request)
    try:
        data = _input(
            nature=nature,
            echeance=echeance,
            bien=bien,
            precision=precision,
            montant=montant,
            recuperable=recuperable,
            rappel=rappel,
            annuelle=annuelle,
        )
        update_deadline(engine, deadline_id, data, actor_user_id=user.id, now=now)
    except DeadlineError as exc:
        return redirect_with_error(f"/echeances/{deadline_id}", str(exc))
    match_payments(engine, now=now)
    return redirect_with_message(f"/echeances/{deadline_id}", "échéance modifiée")


@router.post("/echeances/{deadline_id}/regler")
def settle_route(request: Request, deadline_id: int, user: User, operation: Text = "") -> Response:
    if operation.strip() and not operation.strip().isdigit():
        return redirect_with_message(f"/echeances/{deadline_id}", "Opération introuvable.")
    try:
        next_id = mark_done(
            engine_of(request),
            deadline_id,
            transaction_id=int(operation) if operation.strip() else None,
            actor_user_id=user.id,
            now=now_of(request),
        )
    except DeadlineError as exc:
        return redirect_with_error(f"/echeances/{deadline_id}", str(exc))
    message = "échéance réglée"
    if next_id is not None:
        message += " ; celle de l'an prochain est au calendrier (montant à saisir)"
    return redirect_with_message(f"/echeances/{deadline_id}", message)


@router.post("/echeances/{deadline_id}/rouvrir")
def reopen_route(request: Request, deadline_id: int, user: User) -> Response:
    try:
        reopen(engine_of(request), deadline_id, actor_user_id=user.id, now=now_of(request))
    except DeadlineError as exc:
        return redirect_with_error(f"/echeances/{deadline_id}", str(exc))
    return redirect_with_message(
        f"/echeances/{deadline_id}",
        "règlement annulé ; si l'opération était aussi une charge du bien, retirez-la sur la page"
        " du bien",
    )


@router.post("/echeances/{deadline_id}/supprimer")
def delete_route(request: Request, deadline_id: int, user: User) -> Response:
    try:
        delete_deadline(engine_of(request), deadline_id, actor_user_id=user.id, now=now_of(request))
    except DeadlineError as exc:
        return redirect_with_error("/echeances", str(exc))
    return redirect_with_message("/echeances", "échéance supprimée")
