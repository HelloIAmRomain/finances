"""Notification d'essai (page Alertes) : résultat par canal, avec la marche à suivre."""

import httpx

from app.alerts.channels import Message, NtfySender, SendError
from app.alerts.dispatch import send_test
from app.config import Settings
from app.models.alerts import Channel, Severity
from app.net.client import make_client

BASE_URL = "https://finances.exemple.ts.net"


class Fake:
    def __init__(self, error: SendError | None = None) -> None:
        self.error = error
        self.sent: list[Message] = []

    def send(self, message: Message) -> None:
        if self.error is not None:
            raise self.error
        self.sent.append(message)


def test_success_on_every_configured_channel() -> None:
    ntfy, email = Fake(), Fake()
    outcome = send_test({Channel.NTFY: ntfy, Channel.EMAIL: email}, base_url=BASE_URL + "/")
    assert outcome == {Channel.NTFY: None, Channel.EMAIL: None}
    [message] = ntfy.sent
    assert message.severity is Severity.INFO
    assert message.link == f"{BASE_URL}/alertes"
    assert message.title.isascii()


def test_failures_are_explained() -> None:
    outcome = send_test(
        {
            Channel.NTFY: Fake(SendError("x", status=401)),
            Channel.EMAIL: Fake(SendError("SMTP : timeout")),
        },
        base_url=BASE_URL,
    )
    assert "ntfy token add finances" in str(outcome[Channel.NTFY])
    assert "smtp_" in str(outcome[Channel.EMAIL])


def test_unreachable_and_other_refusals() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refusé", request=request)

    unreachable = NtfySender(
        make_client(transport=httpx.MockTransport(down)),
        base_url="http://ntfy:8080",
        topic="finances-alertes",
        token="t",  # noqa: S106 (factice)
    )
    outcome = send_test({Channel.NTFY: unreachable}, base_url=BASE_URL) | send_test(
        {Channel.EMAIL: Fake()}, base_url=BASE_URL
    )
    assert "injoignable" in str(outcome[Channel.NTFY])
    refused = send_test({Channel.NTFY: Fake(SendError("x", status=413))}, base_url=BASE_URL)
    assert "413" in str(refused[Channel.NTFY])


def test_phone_ntfy_url() -> None:
    assert Settings(public_base_url="https://pi.tail.ts.net").phone_ntfy_url() == (
        "https://pi.tail.ts.net:8443"
    )
    assert Settings(ntfy_public_url="https://autre:9000/").phone_ntfy_url() == "https://autre:9000"
