"""Page « Transactions » : recherche (ANA-11), catégorisation manuelle et suggestion de règle
(ANA-01, ANA-03), export CSV (FIS-06, audité)."""

import itertools
from datetime import date
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from sqlalchemy import select

from app.audit import audit
from app.categorize.service import (
    CategorizationError,
    categorize_manually,
    create_rule_from_transaction,
    suggest_rule,
)
from app.db import write_transaction
from app.ledger.search import TransactionFilters, category_choices, iter_all, search
from app.models import Account, LedgerTransaction
from app.money import InvalidAmountError, parse_optional_user_amount
from app.spreadsheet import csv_lines
from app.timeutil import format_date_fr
from app.web.deps import CurrentUser, current_user, engine_of, now_of, templates_of
from app.web.flash import flash

router = APIRouter()
PAGE_SIZE = 50
CATEGORY_NOT_SAVED = (
    "Catégorie non enregistrée : l'opération ou la catégorie n'existe plus. Rechargez la page,"
    " puis choisissez une catégorie de la liste."
)


def _amount(text: str | None) -> int | None:
    """Filtre de montant ; un texte illisible est ignoré pour que la recherche reste utilisable."""
    try:
        return parse_optional_user_amount(text)
    except InvalidAmountError:
        return None


def _date(text: str | None) -> date | None:
    try:
        return date.fromisoformat(text) if text else None
    except ValueError:
        return None


def _int(text: str | None) -> int | None:
    return int(text) if text and text.strip().lstrip("-").isdigit() else None


FILTER_NAMES = {"du": "Du", "au": "Au", "min": "Montant min.", "max": "Montant max."}


def _ignored_filters(request: Request, filters: TransactionFilters) -> str | None:
    """Filtres saisis mais illisibles : la recherche se fait sans eux, et on le dit."""
    parsed = {
        "du": filters.date_from,
        "au": filters.date_to,
        "min": filters.amount_min,
        "max": filters.amount_max,
    }
    ignored = [
        FILTER_NAMES[name]
        for name, value in parsed.items()
        if value is None and (request.query_params.get(name) or "").strip()
    ]
    if not ignored:
        return None
    return (
        f"Filtre ignoré, saisie illisible : {', '.join(ignored)}. Écrivez un montant comme "
        "1 234,56 et une date avec le calendrier."
    )


def _filters(request: Request) -> TransactionFilters:
    params = request.query_params
    return TransactionFilters(
        account_id=_int(params.get("compte")),
        date_from=_date(params.get("du")),
        date_to=_date(params.get("au")),
        text=(params.get("texte") or "").strip() or None,
        amount_min=_amount(params.get("min")),
        amount_max=_amount(params.get("max")),
        category_id=_int(params.get("categorie")),
        direction=params.get("sens") if params.get("sens") in {"debit", "credit"} else None,
    )


@router.get("/transactions", response_class=HTMLResponse)
def transactions_page(
    request: Request, user: Annotated[CurrentUser, Depends(current_user)]
) -> Response:
    engine = engine_of(request)
    page = max(_int(request.query_params.get("page")) or 1, 1)
    filters = _filters(request)
    rows, total = search(engine, filters, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE)
    with engine.connect() as connection:
        accounts = connection.execute(
            select(Account.id, Account.name)
            .where(Account.ignored_at.is_(None))
            .order_by(Account.name)
        ).all()
    suggestion_id = _int(request.query_params.get("suggestion"))
    suggestion = suggest_rule(engine, suggestion_id) if suggestion_id else None
    query = {k: v for k, v in request.query_params.items() if k not in {"page", "suggestion"}}
    templates = templates_of(request)
    return templates.TemplateResponse(
        request,
        "transactions.html",
        {
            "user": user,
            "rows": rows,
            "total": total,
            "page": page,
            "pages": max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1),
            "params": request.query_params,
            "query": urlencode(query),
            "accounts": accounts,
            "categories": category_choices(engine),
            "suggestion": suggestion,
            "suggestion_id": suggestion_id,
            "erreur": flash(request, "erreur") or _ignored_filters(request, filters),
        },
    )


@router.post("/transactions/{transaction_id}/categorie")
def set_category(
    request: Request,
    transaction_id: int,
    *,
    user: Annotated[CurrentUser, Depends(current_user)],
    categorie: Annotated[int, Form()],
    retour: Annotated[str, Form()] = "",
) -> Response:
    engine = engine_of(request)
    with engine.connect() as connection:
        amount = connection.execute(
            select(LedgerTransaction.amount_cents).where(LedgerTransaction.id == transaction_id)
        ).scalar_one_or_none()
    try:
        if amount is None:
            raise CategorizationError("transaction inconnue")
        categorize_manually(
            engine,
            transaction_id,
            [(categorie, amount)],
            actor_user_id=user.id,
            now=now_of(request),
        )
    except CategorizationError:
        failed: str | None = urlencode({"erreur": CATEGORY_NOT_SAVED})
    else:
        failed = None
    back = retour if retour.startswith("/transactions") else "/transactions"
    separator = "&" if "?" in back else "?"
    if failed:
        return RedirectResponse(f"{back}{separator}{failed}", status_code=303)
    return RedirectResponse(f"{back}{separator}suggestion={transaction_id}", status_code=303)


@router.post("/regles/depuis-transaction/{transaction_id}")
def rule_from_transaction(
    request: Request,
    transaction_id: int,
    user: Annotated[CurrentUser, Depends(current_user)],
) -> Response:
    """Crée la règle suggérée après une correction manuelle (ANA-03)."""
    create_rule_from_transaction(
        engine_of(request),
        transaction_id,
        actor_user_id=user.id,
        now=now_of(request),
        manual_only=True,
    )
    return RedirectResponse("/regles", status_code=303)


@router.get("/transactions/export.csv")
def export_csv(request: Request, user: Annotated[CurrentUser, Depends(current_user)]) -> Response:
    engine, filters = engine_of(request), _filters(request)
    with write_transaction(engine) as connection:
        audit(
            connection,
            "export.transactions_csv",
            ("ledger_transaction", None),
            now=now_of(request),
            actor_user_id=user.id,
            details={"filtres": filters.as_query()},
        )

    header = ["Date", "Compte", "Libellé", "Catégories", "Montant (EUR)", "Virement interne"]
    rows = (
        [
            format_date_fr(row.booking_date),
            row.account_name,
            row.label,
            " + ".join(row.categories),
            row.amount_cents,
            "oui" if row.is_transfer else "",
        ]
        for row in iter_all(engine, filters)
    )

    return StreamingResponse(
        csv_lines(itertools.chain([header], rows)),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="transactions.csv"'},
    )
