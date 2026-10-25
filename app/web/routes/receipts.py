"""Quittances de loyer (LOC-07) : informations du bailleur, aperçu et PDF après paiement complet."""

from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response

from app.rental.receipts import ReceiptError, issue, receipt
from app.rental.receipts import set_details as set_receipt_details
from app.timeutil import paris_date
from app.web.deps import engine_of, now_of, redirect_with_error, redirect_with_message, templates_of
from app.web.routes.rental_shared import User, page_context

router = APIRouter()


@router.post("/biens/{property_id}/quittances")
def receipt_details(
    request: Request,
    property_id: int,
    user: User,
    bailleur: Annotated[str, Form()] = "",
    adresse: Annotated[str, Form()] = "",
) -> Response:
    """Bailleur(s) et adresse du logement imprimés sur les quittances (LOC-07)."""
    try:
        set_receipt_details(
            engine_of(request),
            property_id,
            landlord=bailleur,
            address=adresse,
            actor_user_id=user.id,
            now=now_of(request),
        )
    except ReceiptError as exc:
        return redirect_with_error(f"/biens/{property_id}", str(exc))
    return redirect_with_message(
        f"/biens/{property_id}", "informations des quittances enregistrées"
    )


@router.get("/loyers/{lease_id}/echeances/{due_id}/quittance", response_class=HTMLResponse)
def receipt_page(request: Request, lease_id: int, due_id: int, user: User) -> Response:
    """Aperçu de la quittance avant téléchargement ; rien n'est envoyé au locataire (inv. 6)."""
    engine = engine_of(request)
    try:
        found = receipt(engine, lease_id, due_id)
    except ReceiptError as exc:
        return redirect_with_error(f"/loyers/{lease_id}", str(exc))
    return templates_of(request).TemplateResponse(
        request,
        "rental_receipt.html",
        {
            **page_context(request, user),
            "r": found,
            "issued_on": paris_date(now_of(request)),
        },
    )


@router.get("/loyers/{lease_id}/echeances/{due_id}/quittance.pdf")
def receipt_pdf(request: Request, lease_id: int, due_id: int, user: User) -> Response:
    try:
        found, pdf = issue(
            engine_of(request),
            lease_id,
            due_id,
            issued_on=paris_date(now_of(request)),
            actor_user_id=user.id,
            now=now_of(request),
        )
    except ReceiptError as exc:
        return redirect_with_error(f"/loyers/{lease_id}", str(exc))
    return Response(
        pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{found.filename}"'},
    )
