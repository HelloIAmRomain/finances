"""Canaux de notification (ALR-02) : ntfy auto-hébergé et e-mail de secours.

Les messages ne contiennent que le titre de l'alerte (sans montant ni nom) et un lien vers
l'interface (DT §6). Au lot 1, le seul destinataire possible est l'administrateur (inv. 6 :
aucun envoi au locataire).
"""

import smtplib
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from email.header import Header
from email.message import EmailMessage
from typing import Any, Protocol

import httpx

from app.models.alerts import Severity

NTFY_PRIORITY = {Severity.INFO: "3", Severity.WARNING: "4", Severity.CRITICAL: "5"}
SMTP_TIMEOUT_SECONDS = 20


class SendError(RuntimeError):
    """Envoi impossible pour l'instant (sera retenté)."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status  # code HTTP du serveur de notification, s'il a répondu


@dataclass(frozen=True)
class Message:
    title: str
    body: str
    severity: Severity
    link: str | None = None  # page ouverte en touchant la notification


class Sender(Protocol):
    def send(self, message: Message) -> None: ...


class NtfySender:
    def __init__(self, client: httpx.Client, *, base_url: str, topic: str, token: str) -> None:
        self.client = client
        self.url = f"{base_url.rstrip('/')}/{topic}"
        self.token = token

    def send(self, message: Message) -> None:
        title = (
            message.title if message.title.isascii() else Header(message.title, "utf-8").encode()
        )
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Title": title,
            "Priority": NTFY_PRIORITY[message.severity],
            "Tags": "bank",
        }
        if message.link:
            headers["Click"] = message.link
        try:
            response = self.client.post(
                self.url, content=message.body.encode("utf-8"), headers=headers
            )
        except httpx.TransportError as exc:
            raise SendError(f"ntfy injoignable : {type(exc).__name__}") from exc
        if response.status_code >= 400:  # noqa: PLR2004
            raise SendError(f"ntfy a répondu {response.status_code}", status=response.status_code)


class EmailSender:
    """Envoi SMTP (TLS implicite, port 465) vers **un seul** destinataire : l'administrateur."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        sender: str,
        recipient: str,
        smtp_factory: Callable[..., Any] = smtplib.SMTP_SSL,
    ) -> None:
        self.host, self.port = host, port
        self.username, self.password = username, password
        self.sender, self.recipient = sender, recipient
        self.smtp_factory = smtp_factory

    def send(self, message: Message) -> None:
        email = EmailMessage()
        email["From"] = self.sender
        email["To"] = self.recipient
        email["Subject"] = f"[Finances] {message.title}"
        email.set_content(message.body)
        try:
            # Contexte explicite : sans lui, SMTP_SSL ne vérifie ni le certificat ni le nom
            # du serveur, et le mot de passe partirait vers n'importe quel intermédiaire.
            context = ssl.create_default_context()
            with self.smtp_factory(
                self.host, self.port, timeout=SMTP_TIMEOUT_SECONDS, context=context
            ) as smtp:
                smtp.login(self.username, self.password)
                smtp.send_message(email)
        except (OSError, smtplib.SMTPException) as exc:
            raise SendError(f"SMTP : {type(exc).__name__}") from exc
