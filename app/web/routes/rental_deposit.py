"""Dépôt de garantie d'un bail : encaissement, retenues, restitution, remise des clés (LEG-04)."""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import Response

from app.models.rental import DepositEventKind
from app.rental.deposit import DepositError, record, set_exit
from app.rental.deposit import delete_event as delete_deposit_event
from app.rental.leases import LeaseError
from app.web.deps import engine_of, now_of, redirect_with_error, redirect_with_message
from app.web.routes.rental_shared import User, amount_or_error, refresh_rental

router = APIRouter()


def _optional_date(text: str) -> date | None:
    try:
        return date.fromisoformat(text) if text.strip() else None
    except ValueError as exc:
        raise DepositError("Date illisible : choisissez-la dans le calendrier.") from exc


@router.post("/loyers/{lease_id}/depot")
def deposit_movement(
    request: Request,
    lease_id: int,
    user: User,
    *,
    nature: Annotated[DepositEventKind, Form()],
    montant: Annotated[str, Form()] = "",
    date_mouvement: Annotated[str, Form(alias="date")] = "",
    operation: Annotated[str, Form()] = "",
    motif: Annotated[str, Form()] = "",
) -> Response:
    """Encaissement, retenue ou restitution du dépôt de garantie (LEG-04)."""
    back = f"/loyers/{lease_id}"
    try:
        record(
            engine_of(request),
            lease_id,
            nature,
            amount_cents=amount_or_error(montant, required=False) or None,
            on_date=_optional_date(date_mouvement),
            transaction_id=int(operation) if operation.strip().isdigit() else None,
            note=motif,
            actor_user_id=user.id,
            now=now_of(request),
        )
    except (DepositError, LeaseError) as exc:
        return redirect_with_error(back, str(exc), anchor="depot")
    refresh_rental(request)
    done = {
        DepositEventKind.RECEIVED: "dépôt encaissé enregistré",
        DepositEventKind.WITHHELD: "retenue enregistrée ; gardez le justificatif (devis, facture)",
        DepositEventKind.RETURNED: "restitution enregistrée",
    }
    return redirect_with_message(back, done[nature], anchor="depot")


@router.post("/loyers/{lease_id}/depot/{event_id}/supprimer")
def deposit_delete(request: Request, lease_id: int, event_id: int, user: User) -> Response:
    try:
        delete_deposit_event(
            engine_of(request), lease_id, event_id, actor_user_id=user.id, now=now_of(request)
        )
    except DepositError as exc:
        return redirect_with_error(f"/loyers/{lease_id}", str(exc), anchor="depot")
    refresh_rental(request)
    return redirect_with_message(f"/loyers/{lease_id}", "mouvement retiré", anchor="depot")


@router.post("/loyers/{lease_id}/sortie")
def lease_exit(
    request: Request,
    lease_id: int,
    user: User,
    remise_cles: Annotated[str, Form()] = "",
    etat_des_lieux: Annotated[str, Form()] = "",
) -> Response:
    """Remise des clés et état des lieux de sortie : fixent le délai de restitution du dépôt."""
    inspection = {"conforme": True, "non_conforme": False}.get(etat_des_lieux)
    try:
        set_exit(
            engine_of(request),
            lease_id,
            keys_returned_on=_optional_date(remise_cles),
            inspection_ok=inspection,
            actor_user_id=user.id,
            now=now_of(request),
        )
    except DepositError as exc:
        return redirect_with_error(f"/loyers/{lease_id}", str(exc), anchor="depot")
    return redirect_with_message(
        f"/loyers/{lease_id}", "fin de location enregistrée", anchor="depot"
    )
