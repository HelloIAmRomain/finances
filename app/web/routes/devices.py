"""Page « Appareils » : appairage de l'application Android et révocation (NF-SEC-04)."""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response

from app.auth.devices import (
    CODE_LIFETIME,
    IDLE_EXPIRY,
    PairingError,
    create_pairing_code,
    devices,
    revoke_device,
)
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


def _page(request: Request, user: CurrentUser, code: str | None = None) -> Response:
    templates = templates_of(request)
    return templates.TemplateResponse(
        request,
        "devices.html",
        {
            "user": user,
            "devices": devices(engine_of(request)),
            "now": now_of(request),
            "idle_days": IDLE_EXPIRY.days,
            "code": code,
            "minutes": int(CODE_LIFETIME.total_seconds() // 60),
            "server": str(request.base_url).rstrip("/").replace("http://", "https://"),
            "message": flash(request),
        },
    )


@router.get("/appareils", response_class=HTMLResponse)
def devices_page(request: Request, user: User) -> Response:
    return _page(request, user)


@router.post("/appareils/code", response_class=HTMLResponse)
def new_code(request: Request, user: User, mot_de_passe: Annotated[str, Form()]) -> Response:
    try:
        code = create_pairing_code(
            engine_of(request), user_id=user.id, password=mot_de_passe, now=now_of(request)
        )
    except PairingError as exc:
        return redirect_with_error("/appareils", str(exc))
    return _page(request, user, code)


@router.post("/appareils/{device_id}/revoquer")
def revoke(request: Request, device_id: int, user: User) -> Response:
    try:
        revoke_device(engine_of(request), device_id, actor_user_id=user.id, now=now_of(request))
    except PairingError as exc:
        return redirect_with_error("/appareils", str(exc))
    return redirect_with_message("/appareils", "appareil révoqué")
