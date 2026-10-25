"""Révision annuelle du loyer selon l'IRL, après validation (LOC-09, LEG-04)."""

from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import Response

from app.rental.irl import IrlError, apply_revision, set_index, set_reference
from app.rental.leases import LeaseError
from app.timeutil import paris_date
from app.web.deps import engine_of, now_of, redirect_with_error, redirect_with_message
from app.web.routes.rental_shared import User, amount_or_error, refresh_rental

router = APIRouter()


@router.post("/loyers/{lease_id}/irl/reference")
def irl_reference(
    request: Request, lease_id: int, user: User, trimestre: Annotated[str, Form()] = ""
) -> Response:
    try:
        set_reference(
            engine_of(request), lease_id, trimestre, actor_user_id=user.id, now=now_of(request)
        )
    except IrlError as exc:
        return redirect_with_error(f"/loyers/{lease_id}", str(exc))
    return redirect_with_message(f"/loyers/{lease_id}", "indice de référence enregistré")


@router.post("/loyers/{lease_id}/irl/indice")
def irl_index(
    request: Request,
    lease_id: int,
    user: User,
    trimestre: Annotated[str, Form()] = "",
    valeur: Annotated[str, Form()] = "",
) -> Response:
    try:
        value = amount_or_error(valeur)
        set_index(engine_of(request), trimestre, value, actor_user_id=user.id, now=now_of(request))
    except (IrlError, LeaseError) as exc:
        return redirect_with_error(f"/loyers/{lease_id}", str(exc))
    return redirect_with_message(f"/loyers/{lease_id}", "indice enregistré")


@router.post("/loyers/{lease_id}/irl/appliquer")
def irl_apply(request: Request, lease_id: int, user: User) -> Response:
    engine, now = engine_of(request), now_of(request)
    try:
        applied = apply_revision(
            engine, lease_id, today=paris_date(now), actor_user_id=user.id, now=now
        )
    except IrlError as exc:
        return redirect_with_error(f"/loyers/{lease_id}", str(exc))
    refresh_rental(request)
    return redirect_with_message(
        f"/loyers/{lease_id}",
        f"loyer révisé à partir du {applied.effective_from.strftime('%d/%m/%Y')} ; prévenez le"
        " locataire du nouveau montant",
    )
