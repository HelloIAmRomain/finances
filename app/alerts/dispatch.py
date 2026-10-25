"""Tâche d'envoi des alertes (worker, chaque minute) : rappels, secours, puis envois."""

from datetime import datetime

from sqlalchemy import Engine

from app.alerts.channels import EmailSender, Message, NtfySender, Sender, SendError
from app.alerts.engine import send_due_reminders
from app.alerts.outbox import enqueue_critical_fallbacks, process_outbox
from app.alerts.preferences import quiet_hours
from app.config import Secrets, Settings
from app.models.alerts import Channel, Severity
from app.net.client import make_client


def build_senders(settings: Settings, secrets: Secrets) -> dict[Channel, Sender]:
    senders: dict[Channel, Sender] = {}
    if secrets.ntfy_token is not None:
        senders[Channel.NTFY] = NtfySender(
            make_client(),
            base_url=settings.ntfy_url,
            topic=settings.ntfy_topic,
            token=secrets.ntfy_token.get_secret_value(),
        )
    smtp = (
        secrets.smtp_host,
        secrets.smtp_username,
        secrets.smtp_password,
        secrets.alert_email_from,
        secrets.alert_email_to,
    )
    if all(value is not None for value in smtp):
        host, username, password, sender, recipient = smtp
        senders[Channel.EMAIL] = EmailSender(
            host=str(host),
            port=secrets.smtp_port,
            username=str(username),
            password=password.get_secret_value() if password else "",
            sender=str(sender),
            recipient=str(recipient),
        )
    return senders


def dispatch(
    engine: Engine, senders: dict[Channel, Sender], *, base_url: str, now: datetime
) -> dict[str, int]:
    return {
        "reminders": send_due_reminders(engine, now=now),
        "fallbacks": enqueue_critical_fallbacks(engine, now=now),
        "sent": process_outbox(
            engine, senders, base_url=base_url, now=now, quiet=quiet_hours(engine)
        ),
    }


CHANNEL_LABELS = {Channel.NTFY: "téléphone (application ntfy)", Channel.EMAIL: "e-mail"}
UNAUTHORIZED = (401, 403)


def _explain(channel: Channel, error: SendError) -> str:
    """Raison d'un échec d'essai, avec la marche à suivre (docs/exploitation.md)."""
    if channel is Channel.EMAIL:
        return "envoi refusé ou serveur injoignable : vérifiez les secrets smtp_*"
    if error.status in UNAUTHORIZED:
        return (
            "le serveur de notification refuse le jeton : recréez-le "
            "(ntfy token add finances) puis enregistrez-le comme secret ntfy_token"
        )
    if error.status is None:
        return "serveur de notification injoignable : vérifiez que le service ntfy tourne"
    return f"le serveur de notification a refusé le message (code {error.status})"


def send_test(senders: dict[Channel, Sender], *, base_url: str) -> dict[Channel, str | None]:
    """Notification d'essai sur chaque canal configuré : None si partie, sinon la raison."""
    link = f"{base_url.rstrip('/')}/alertes"
    message = Message(
        title="Essai de notification",
        body=(
            "Les notifications de Finances arrivent bien sur cet appareil. "
            f"Touchez ce message pour ouvrir la page Alertes : {link}"
        ),
        severity=Severity.INFO,
        link=link,
    )
    outcome: dict[Channel, str | None] = {}
    for channel, sender in senders.items():
        try:
            sender.send(message)
        except SendError as exc:
            outcome[channel] = _explain(channel, exc)
        else:
            outcome[channel] = None
    return outcome
