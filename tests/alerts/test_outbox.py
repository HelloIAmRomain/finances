"""File d'envoi des notifications, canaux ntfy et e-mail (ALR-02, I-19, inv. 6)."""

import ssl
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from typing import ClassVar

import httpx
import pytest
from sqlalchemy import Engine, select, update

from app.alerts.channels import EmailSender, Message, NtfySender, SendError
from app.alerts.engine import mark_seen, raise_alert, resolve, send_due_reminders
from app.alerts.outbox import enqueue_critical_fallbacks, process_outbox
from app.models import Notification
from app.models.alerts import Channel, Severity
from app.net.client import make_client

T0 = datetime(2026, 9, 27, 8, 0, tzinfo=UTC)
BASE_URL = "https://finances.exemple.ts.net"


class FakeSender:
    def __init__(self, failures: int = 0) -> None:
        self.sent: list[Message] = []
        self.failures = failures

    def send(self, message: Message) -> None:
        if self.failures:
            self.failures -= 1
            raise SendError("indisponible")
        self.sent.append(message)


def _statuses(engine: Engine) -> list[tuple[str, str, int]]:
    with engine.connect() as connection:
        rows = connection.execute(
            select(Notification.channel, Notification.status, Notification.attempt_count).order_by(
                Notification.id
            )
        ).all()
    return [(str(c), str(s), n) for c, s, n in rows]


def test_pending_notifications_are_sent_once(engine: Engine) -> None:
    raise_alert(
        engine, type="t", severity=Severity.CRITICAL, key="k", title="Synchro en échec", now=T0
    )
    ntfy, email = FakeSender(), FakeSender()
    senders = {Channel.NTFY: ntfy, Channel.EMAIL: email}
    assert process_outbox(engine, senders, base_url=BASE_URL, now=T0) == 2
    assert process_outbox(engine, senders, base_url=BASE_URL, now=T0 + timedelta(hours=1)) == 0
    assert len(ntfy.sent) == len(email.sent) == 1
    assert _statuses(engine) == [("ntfy", "sent", 1), ("email", "sent", 1)]


def test_message_has_no_amount_and_links_to_interface(engine: Engine) -> None:
    result = raise_alert(
        engine, type="t", severity=Severity.WARNING, key="k", title="Loyer : alerte ouverte", now=T0
    )
    sender = FakeSender()
    process_outbox(engine, {Channel.NTFY: sender}, base_url=BASE_URL, now=T0)
    [message] = sender.sent
    assert message.title == "Loyer : alerte ouverte"
    assert f"{BASE_URL}/alertes/{result.alert_id}" in message.body
    assert "€" not in message.title + message.body
    assert message.severity is Severity.WARNING
    assert message.link == f"{BASE_URL}/alertes/{result.alert_id}"  # toucher ouvre l'alerte


def test_alert_resolved_before_sending_is_not_sent(engine: Engine) -> None:
    """Canal configuré tard : pas de rafale d'alertes déjà résolues."""
    old = raise_alert(engine, type="t", severity=Severity.WARNING, key="a", title="x", now=T0)
    raise_alert(engine, type="t", severity=Severity.WARNING, key="b", title="y", now=T0)
    resolve(engine, old.alert_id, now=T0 + timedelta(minutes=1))
    sender = FakeSender()
    later = T0 + timedelta(days=30)
    assert process_outbox(engine, {Channel.NTFY: sender}, base_url=BASE_URL, now=later) == 1
    [message] = sender.sent
    assert message.title == "y"
    with engine.connect() as connection:
        rows = connection.execute(
            select(Notification.alert_id, Notification.status, Notification.last_error)
        ).all()
    dropped = {row.alert_id: row for row in rows}[old.alert_id]
    assert str(dropped.status) == "failed" and "résolue" in str(dropped.last_error)
    assert process_outbox(engine, {Channel.NTFY: sender}, base_url=BASE_URL, now=later) == 0


def test_failures_are_retried_with_backoff_then_abandoned(engine: Engine) -> None:
    raise_alert(engine, type="t", severity=Severity.WARNING, key="k", title="x", now=T0)
    sender = FakeSender(failures=10)
    senders = {Channel.NTFY: sender}
    process_outbox(engine, senders, base_url=BASE_URL, now=T0)
    assert _statuses(engine) == [("ntfy", "pending", 1)]
    process_outbox(engine, senders, base_url=BASE_URL, now=T0 + timedelta(seconds=30))
    assert _statuses(engine) == [("ntfy", "pending", 1)]  # trop tôt pour réessayer
    when = T0
    for _ in range(10):
        when += timedelta(hours=2)
        process_outbox(engine, senders, base_url=BASE_URL, now=when)
    assert _statuses(engine) == [("ntfy", "failed", 6)]


def test_missing_channel_is_left_pending(engine: Engine) -> None:
    raise_alert(engine, type="t", severity=Severity.CRITICAL, key="k", title="x", now=T0)
    process_outbox(engine, {Channel.NTFY: FakeSender()}, base_url=BASE_URL, now=T0)
    assert _statuses(engine) == [("ntfy", "sent", 1), ("email", "pending", 0)]


def test_unseen_critical_alert_falls_back_to_email_once(engine: Engine) -> None:
    critical = raise_alert(engine, type="t", severity=Severity.CRITICAL, key="c", title="x", now=T0)
    seen = raise_alert(engine, type="t", severity=Severity.CRITICAL, key="s", title="y", now=T0)
    raise_alert(engine, type="t", severity=Severity.WARNING, key="w", title="z", now=T0)
    mark_seen(engine, seen.alert_id, now=T0 + timedelta(minutes=5))
    assert enqueue_critical_fallbacks(engine, now=T0 + timedelta(minutes=29)) == 0
    assert enqueue_critical_fallbacks(engine, now=T0 + timedelta(minutes=30)) == 1
    assert enqueue_critical_fallbacks(engine, now=T0 + timedelta(hours=5)) == 0
    with engine.connect() as connection:
        fallback = connection.execute(
            select(Notification.alert_id, Notification.channel).where(
                Notification.kind == "fallback"
            )
        ).all()
    assert fallback == [(critical.alert_id, Channel.EMAIL)]


# --- Canaux ---------------------------------------------------------------------------------


def test_ntfy_sender_posts_to_internal_topic_with_token() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    sender = NtfySender(
        make_client(transport=httpx.MockTransport(handler)),
        base_url="http://ntfy:8080",
        topic="finances-alertes",
        token="jeton-factice",  # noqa: S106 (factice)
    )
    sender.send(
        Message(title="Synchro en échec", body="Détails : lien", severity=Severity.CRITICAL)
    )
    [request] = requests
    assert str(request.url) == "http://ntfy:8080/finances-alertes"
    assert request.headers["Authorization"] == "Bearer jeton-factice"
    assert request.headers["Priority"] == "5"
    assert request.headers["Title"].startswith("=?utf-8?")  # RFC 2047 : accents dans l'en-tête
    assert request.content == "Détails : lien".encode()
    assert "Click" not in request.headers


def test_ntfy_click_opens_the_link() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    sender = NtfySender(
        make_client(transport=httpx.MockTransport(handler)),
        base_url="http://ntfy:8080",
        topic="finances-alertes",
        token="t",  # noqa: S106 (factice)
    )
    link = f"{BASE_URL}/alertes/3"
    sender.send(Message(title="x", body="y", severity=Severity.INFO, link=link))
    assert requests[0].headers["Click"] == link


def test_ntfy_error_raises_send_error() -> None:
    sender = NtfySender(
        make_client(transport=httpx.MockTransport(lambda r: httpx.Response(403))),
        base_url="http://ntfy:8080",
        topic="finances-alertes",
        token="t",  # noqa: S106 (factice)
    )
    with pytest.raises(SendError) as error:
        sender.send(Message(title="x", body="y", severity=Severity.INFO))
    assert error.value.status == 403


class FakeSmtp:
    instances: ClassVar[list["FakeSmtp"]] = []

    def __init__(self, host: str, port: int, timeout: float, context: ssl.SSLContext) -> None:
        self.host, self.port, self.context = host, port, context
        self.logged_in: tuple[str, str] | None = None
        self.messages: list[EmailMessage] = []
        FakeSmtp.instances.append(self)

    def __enter__(self) -> "FakeSmtp":
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def login(self, user: str, password: str) -> None:
        self.logged_in = (user, password)

    def send_message(self, message: EmailMessage) -> None:
        self.messages.append(message)


def test_email_sender_goes_only_to_the_administrator() -> None:
    FakeSmtp.instances.clear()
    sender = EmailSender(
        host="smtp.example.org",
        port=465,
        username="alertes@example.org",
        password="mot-de-passe-factice",  # noqa: S106
        sender="alertes@example.org",
        recipient="admin@example.org",
        smtp_factory=FakeSmtp,
    )
    sender.send(
        Message(title="Synchro en échec", body="Détails : lien", severity=Severity.CRITICAL)
    )
    [smtp] = FakeSmtp.instances
    [message] = smtp.messages
    assert message["To"] == "admin@example.org"
    assert message["Subject"] == "[Finances] Synchro en échec"
    assert smtp.logged_in == ("alertes@example.org", "mot-de-passe-factice")
    # Certificat et nom du serveur vérifiés : sans eux, le mot de passe SMTP est interceptable.
    assert smtp.context.verify_mode is ssl.CERT_REQUIRED and smtp.context.check_hostname


def test_backlog_when_channel_configured_late_is_one_summary_not_a_burst(engine: Engine) -> None:
    """Régression : ntfy configuré des jours après l'installation, chaque alerte critique et
    son rappel partaient un par un (téléphone qui sonne sans arrêt)."""
    for key in ("a", "b", "c"):
        raise_alert(engine, type="t", severity=Severity.CRITICAL, key=key, title=key, now=T0)
    raise_alert(engine, type="t", severity=Severity.WARNING, key="w", title="w", now=T0)
    later = T0 + timedelta(days=5)
    assert send_due_reminders(engine, now=later) == 4  # rappels en file, canal absent
    sender = FakeSender()
    assert process_outbox(engine, {Channel.NTFY: sender}, base_url=BASE_URL, now=later) == 1
    [summary] = sender.sent
    assert summary.title == "4 nouvelles alertes"
    assert summary.severity is Severity.WARNING  # un récapitulatif ne force pas la sonnerie
    assert summary.body.count("•") == 4  # une ligne par alerte, rappel compris
    with engine.connect() as connection:
        pending = connection.scalars(
            select(Notification.id).where(
                Notification.channel == Channel.NTFY, Notification.status == "pending"
            )
        ).all()
    assert pending == []


def test_fresh_critical_and_its_reminder_due_together_are_sent_once(engine: Engine) -> None:
    result = raise_alert(engine, type="t", severity=Severity.CRITICAL, key="k", title="x", now=T0)
    with engine.begin() as connection:  # notification initiale retentée, rappel dû en même temps
        connection.execute(
            update(Notification)
            .where(Notification.alert_id == result.alert_id)
            .values(attempt_count=1)
        )
    send_due_reminders(engine, now=T0 + timedelta(hours=48))
    sender = FakeSender()
    sent = process_outbox(
        engine, {Channel.NTFY: sender}, base_url=BASE_URL, now=T0 + timedelta(hours=48)
    )
    assert sent == 1 and sender.sent[0].title == "x"


# --- Au plus un message par 24 h et par canal (décision du 17/10/2026) ----------------------


def _critical(engine: Engine, key: str, at: datetime) -> None:
    raise_alert(engine, type="t", severity=Severity.CRITICAL, key=key, title=key, now=at)


def test_at_most_one_message_per_day_and_channel_the_rest_is_grouped(engine: Engine) -> None:
    ntfy, email = FakeSender(), FakeSender()
    senders = {Channel.NTFY: ntfy, Channel.EMAIL: email}
    _critical(engine, "a", T0)
    process_outbox(engine, senders, base_url=BASE_URL, now=T0)
    for hours, key in ((1, "b"), (5, "c")):
        _critical(engine, key, T0 + timedelta(hours=hours))
        process_outbox(engine, senders, base_url=BASE_URL, now=T0 + timedelta(hours=hours))
    almost = T0 + timedelta(hours=24) - timedelta(minutes=1)
    process_outbox(engine, senders, base_url=BASE_URL, now=almost)
    for sender in (ntfy, email):
        assert [m.title for m in sender.sent] == ["a"]  # b et c attendent, même critiques
    process_outbox(engine, senders, base_url=BASE_URL, now=T0 + timedelta(hours=24))
    for sender in (ntfy, email):
        assert len(sender.sent) == 2 and sender.sent[1].title == "2 nouvelles alertes"
        assert sender.sent[1].body.count("•") == 2
    process_outbox(engine, senders, base_url=BASE_URL, now=T0 + timedelta(hours=30))
    assert len(ntfy.sent) == len(email.sent) == 2


def test_cadence_can_be_lifted_per_channel(engine: Engine) -> None:
    sender = FakeSender()
    for key, hours in (("a", 0), ("b", 1)):
        at = T0 + timedelta(hours=hours)
        _critical(engine, key, at)
        process_outbox(engine, {Channel.NTFY: sender}, base_url=BASE_URL, now=at, min_interval={})
    assert [m.title for m in sender.sent] == ["a", "b"]


def test_messages_due_together_leave_as_one(engine: Engine) -> None:
    for key in ("a", "b"):
        _critical(engine, key, T0)
    email = FakeSender()
    assert process_outbox(engine, {Channel.EMAIL: email}, base_url=BASE_URL, now=T0) == 1
    assert [m.title for m in email.sent] == ["2 nouvelles alertes"]


def test_a_failed_email_does_not_open_the_daily_window(engine: Engine) -> None:
    _critical(engine, "a", T0)
    email = FakeSender(failures=1)
    process_outbox(engine, {Channel.EMAIL: email}, base_url=BASE_URL, now=T0)
    process_outbox(engine, {Channel.EMAIL: email}, base_url=BASE_URL, now=T0 + timedelta(minutes=1))
    assert [m.title for m in email.sent] == ["a"]  # reprise normale après l'échec
