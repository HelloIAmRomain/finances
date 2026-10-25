"""Page « Alertes » (ALR-06) : en cours puis résolues ; ouvrir une alerte la marque vue."""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select

from app.alerts.digest import digest_enabled, set_digest_enabled
from app.alerts.dispatch import CHANNEL_LABELS, send_test
from app.alerts.engine import (
    RESOLVED_SHOWN,
    AlertTransitionError,
    current_alerts,
    mark_seen,
    recently_resolved,
    resolve,
)
from app.alerts.preferences import PreferenceError, quiet_hours, set_quiet_hours
from app.config import Settings
from app.models import Alert
from app.models.alerts import Channel
from app.web.deps import (
    CurrentUser,
    current_user,
    engine_of,
    now_of,
    redirect_with_message,
    templates_of,
)
from app.web.flash import flash

router = APIRouter()
SEVERITY_LABELS = {"info": "Information", "warning": "Avertissement", "critical": "Critique"}
STATUS_LABELS = {"open": "nouvelle", "seen": "vue", "resolved": "résolue"}


def _render(request: Request, template: str, context: dict[str, object]) -> Response:
    templates = templates_of(request)
    return templates.TemplateResponse(
        request,
        template,
        {"severities": SEVERITY_LABELS, "statuses": STATUS_LABELS, **context},
    )


@router.get("/alertes", response_class=HTMLResponse)
def alerts_page(request: Request, user: Annotated[CurrentUser, Depends(current_user)]) -> Response:
    settings: Settings = request.app.state.settings
    engine = engine_of(request)
    return _render(
        request,
        "alerts.html",
        {
            "user": user,
            "current": current_alerts(engine),
            "resolved": recently_resolved(engine, RESOLVED_SHOWN),
            "quiet": quiet_hours(engine),
            "digest": digest_enabled(engine),
            "message": flash(request),
            "channels": list(request.app.state.senders),
            "phone_ready": Channel.NTFY in request.app.state.senders,
            "topic": settings.ntfy_topic,
            "ntfy_public": settings.phone_ntfy_url(),
        },
    )


@router.post("/alertes/essai")
def test_notification(
    request: Request, user: Annotated[CurrentUser, Depends(current_user)]
) -> Response:
    """Notification d'essai sur chaque canal configuré (ALR-02), pour vérifier le téléphone."""
    del user  # authentification et CSRF vérifiés par la dépendance
    settings: Settings = request.app.state.settings
    outcome = send_test(request.app.state.senders, base_url=settings.public_base_url)
    parts = [
        f"{CHANNEL_LABELS[channel]} : "
        + (
            "notification d'essai envoyée, elle doit apparaître d'ici une minute"
            if error is None
            else error
        )
        for channel, error in outcome.items()
    ]
    message = " ; ".join(parts) or "aucun envoi configuré"
    return redirect_with_message("/alertes", message, anchor="telephone")


@router.post("/alertes/heures-silencieuses")
def quiet_hours_form(
    request: Request,
    user: Annotated[CurrentUser, Depends(current_user)],
    *,
    debut: Annotated[str, Form()],
    fin: Annotated[str, Form()],
    actif: Annotated[str | None, Form()] = None,
) -> Response:
    """Heures silencieuses (ALR-03, ALR-04) : les alertes critiques passent toujours."""
    try:
        set_quiet_hours(
            engine_of(request),
            enabled=actif == "1",
            start=debut,
            end=fin,
            actor_user_id=user.id,
            now=now_of(request),
        )
        message = "heures silencieuses enregistrées"
    except PreferenceError as exc:
        message = str(exc)
    return redirect_with_message("/alertes", message)


@router.post("/alertes/bilan")
def digest_form(
    request: Request,
    user: Annotated[CurrentUser, Depends(current_user)],
    actif: Annotated[str | None, Form()] = None,
) -> Response:
    """Bilan hebdomadaire du lundi (ALR-05)."""
    set_digest_enabled(engine_of(request), actif == "1", actor_user_id=user.id, now=now_of(request))
    message = "bilan hebdomadaire " + ("activé" if actif == "1" else "désactivé")
    return redirect_with_message("/alertes", message)


@router.get("/alertes/{alert_id}", response_class=HTMLResponse)
def alert_detail(
    request: Request, alert_id: int, user: Annotated[CurrentUser, Depends(current_user)]
) -> Response:
    engine = engine_of(request)
    try:
        mark_seen(
            engine, alert_id, now=now_of(request)
        )  # lien d'une notification : l'alerte est vue
    except AlertTransitionError:
        pass
    with engine.connect() as connection:
        alert = connection.execute(select(Alert).where(Alert.id == alert_id)).first()
    if alert is None:
        raise HTTPException(status_code=404, detail="Alerte introuvable.")
    return _render(request, "alert_detail.html", {"user": user, "alert": alert})


@router.post("/alertes/{alert_id}/resoudre")
def resolve_alert(
    request: Request, alert_id: int, user: Annotated[CurrentUser, Depends(current_user)]
) -> Response:
    try:
        resolve(engine_of(request), alert_id, now=now_of(request))
    except AlertTransitionError:
        pass
    return RedirectResponse("/alertes", status_code=303)
