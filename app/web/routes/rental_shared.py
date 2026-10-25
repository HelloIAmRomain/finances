"""Aides communes aux pages des loyers, biens, dépôts et quittances."""

from typing import Annotated

from fastapi import Depends, Request

from app.money import InvalidAmountError, parse_optional_user_amount, parse_user_amount
from app.rental.engine import run_rental
from app.rental.leases import LeaseError
from app.rental.status import RentState
from app.rental.views import INCIDENT_LABELS, KIND_LABELS, ROLE_LABELS, STATE_LABELS
from app.web.deps import CurrentUser, current_user, engine_of, now_of
from app.web.flash import flash

User = Annotated[CurrentUser, Depends(current_user)]


def amount_or_error(text: str, *, required: bool = True) -> int:
    try:
        return parse_user_amount(text) if required else parse_optional_user_amount(text) or 0
    except InvalidAmountError as exc:
        raise LeaseError(str(exc)) from exc


def page_context(request: Request, user: CurrentUser) -> dict[str, object]:
    return {
        "user": user,
        "state_labels": STATE_LABELS,
        "incident_labels": INCIDENT_LABELS,
        "role_labels": ROLE_LABELS,
        "kind_labels": KIND_LABELS,
        "states": RentState,
        "message": flash(request),
    }


def refresh_rental(request: Request) -> None:
    run_rental(engine_of(request), now=now_of(request))
