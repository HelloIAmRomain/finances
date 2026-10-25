"""Pages « Bien » : charges rattachées (LOC-10), récapitulatif annuel réparti entre indivisaires
et son export (FIS-01, FIS-06, LOC-12)."""

from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import select

from app.audit import audit
from app.db import write_transaction
from app.models import OwnerShare, Property, PropertyExpenseRule
from app.models.rental import ExpenseKind
from app.rental.expenses import EXPENSE_LABELS, ExpenseError, add_rule, expenses, totals_by_kind
from app.rental.expenses import link as link_charge
from app.rental.expenses import unlink as unlink_charge
from app.rental.views import recent_debits
from app.spreadsheet import csv_lines
from app.tax.summary import SOURCES as SUMMARY_SOURCES
from app.tax.summary import SummaryError, property_year, summary_csv_rows
from app.timeutil import paris_date
from app.web.deps import engine_of, now_of, redirect_with_error, redirect_with_message, templates_of
from app.web.routes.rental_shared import User, page_context, refresh_rental

router = APIRouter()


@router.get("/biens/{property_id}", response_class=HTMLResponse)
def property_page(request: Request, property_id: int, user: User) -> Response:
    engine, today = engine_of(request), paris_date(now_of(request))
    year = int(request.query_params.get("annee") or today.year)
    with engine.connect() as connection:
        found = connection.execute(select(Property).where(Property.id == property_id)).first()
        rules = connection.execute(
            select(PropertyExpenseRule)
            .where(PropertyExpenseRule.property_id == property_id)
            .order_by(PropertyExpenseRule.pattern)
        ).all()
        shares = connection.execute(
            select(OwnerShare.owner_label, OwnerShare.share_bp)
            .where(OwnerShare.property_id == property_id)
            .order_by(OwnerShare.owner_label)
        ).all()
    if found is None:
        return redirect_with_message("/loyers", "bien introuvable")
    rows = expenses(engine, property_id, year)
    return templates_of(request).TemplateResponse(
        request,
        "property.html",
        {
            **page_context(request, user),
            "bien": found,
            "shares": shares,
            "year": year,
            "expenses": rows,
            "totals": totals_by_kind(rows),
            "total": sum(r.amount_cents for r in rows),
            "rules": rules,
            "debits": recent_debits(engine, today),
            "expense_labels": EXPENSE_LABELS,
            "summary": property_year(engine, property_id, year),
            "summary_sources": SUMMARY_SOURCES,
        },
    )


@router.get("/biens/{property_id}/recapitulatif.csv")
def summary_csv(request: Request, property_id: int, user: User) -> Response:
    """Récapitulatif annuel du bien, réparti entre indivisaires (FIS-01, FIS-06, LOC-12)."""
    engine = engine_of(request)
    raw_year = request.query_params.get("annee", "")
    year = int(raw_year) if raw_year.isdigit() else paris_date(now_of(request)).year
    try:
        summary = property_year(engine, property_id, year)
    except SummaryError as exc:
        return redirect_with_error("/loyers", str(exc))
    with write_transaction(engine) as connection:
        audit(
            connection,
            "export.property_summary_csv",
            ("property", property_id),
            now=now_of(request),
            actor_user_id=user.id,
            details={"annee": year},
        )
    return Response(
        "".join(csv_lines(summary_csv_rows(summary, EXPENSE_LABELS))),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="recapitulatif-{year}.csv"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/biens/{property_id}/charges")
def link_expense(
    request: Request,
    property_id: int,
    user: User,
    operation: Annotated[int, Form()],
    nature: Annotated[ExpenseKind, Form()],
) -> Response:
    try:
        link_charge(
            engine_of(request),
            property_id,
            operation,
            kind=nature,
            actor_user_id=user.id,
            now=now_of(request),
        )
    except ExpenseError as exc:
        return redirect_with_error(f"/biens/{property_id}", str(exc))
    return redirect_with_message(f"/biens/{property_id}", "charge rattachée")


@router.post("/biens/{property_id}/charges/{expense_id}/retirer")
def unlink_expense(request: Request, property_id: int, expense_id: int, user: User) -> Response:
    try:
        unlink_charge(engine_of(request), expense_id, actor_user_id=user.id, now=now_of(request))
    except ExpenseError as exc:
        return redirect_with_error(f"/biens/{property_id}", str(exc))
    return redirect_with_message(f"/biens/{property_id}", "charge retirée")


@router.post("/biens/{property_id}/regles")
def add_expense_rule(
    request: Request,
    property_id: int,
    user: User,
    motif: Annotated[str, Form()],
    nature: Annotated[ExpenseKind, Form()],
) -> Response:
    try:
        add_rule(
            engine_of(request),
            property_id,
            kind=nature,
            pattern=motif,
            actor_user_id=user.id,
            now=now_of(request),
        )
    except ExpenseError as exc:
        return redirect_with_error(f"/biens/{property_id}", str(exc))
    refresh_rental(request)
    return redirect_with_message(f"/biens/{property_id}", "règle ajoutée et appliquée")
