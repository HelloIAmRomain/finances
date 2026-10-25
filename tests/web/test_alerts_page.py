"""Page « Alertes » (ALR-06)."""

from datetime import timedelta

from sqlalchemy import select

from app.alerts.channels import Message, SendError
from app.alerts.engine import raise_alert
from app.alerts.outbox import enqueue_critical_fallbacks
from app.models import Alert
from app.models.alerts import Channel, Severity
from tests.web.conftest import T0, Web


def _raise(web: Web, key: str, severity: Severity, title: str) -> int:
    return raise_alert(
        web.engine, type="t", severity=severity, key=key, title=title, now=T0
    ).alert_id


def test_current_alerts_sorted_by_severity(web: Web) -> None:
    _raise(web, "a", Severity.INFO, "Information banale")
    _raise(web, "b", Severity.CRITICAL, "Synchro en échec")
    web.login()
    page = web.client.get("/alertes").text
    assert page.index("Synchro en échec") < page.index("Information banale")
    assert "Critique" in page and "nouvelle" in page


def test_opening_an_alert_marks_it_seen_and_stops_fallback(web: Web) -> None:
    alert_id = _raise(web, "c", Severity.CRITICAL, "Loyer : alerte ouverte")
    web.login()
    page = web.client.get(f"/alertes/{alert_id}").text
    assert "Loyer : alerte ouverte" in page and "vue" in page
    assert enqueue_critical_fallbacks(web.engine, now=T0 + timedelta(hours=1)) == 0


def test_resolve_from_the_list(web: Web) -> None:
    alert_id = _raise(web, "d", Severity.WARNING, "Disque presque plein")
    web.login()
    page = web.client.get("/alertes").text
    csrf = page.split('name="csrf" value="', 1)[1].split('"', 1)[0]
    web.client.post(f"/alertes/{alert_id}/resoudre", data={"csrf": csrf})
    with web.engine.connect() as connection:
        assert str(connection.scalars(select(Alert.status)).one()) == "resolved"
    assert "Aucune alerte en cours." in web.client.get("/alertes").text


def test_unknown_alert_is_404(web: Web) -> None:
    web.login()
    assert web.client.get("/alertes/999").status_code == 404


def test_quiet_hours_form(web: Web) -> None:
    from app.alerts.preferences import quiet_hours  # noqa: PLC0415

    web.login()
    page = web.client.get("/alertes").text
    assert "Heures silencieuses" in page and 'value="22:00"' in page
    csrf = page.split('name="csrf" value="', 1)[1].split('"', 1)[0]
    response = web.client.post(
        "/alertes/heures-silencieuses",
        data={"csrf": csrf, "actif": "1", "debut": "21:30", "fin": "07:00"},
        follow_redirects=True,
    )
    assert "Heures silencieuses enregistrées" in response.text
    assert quiet_hours(web.engine).start.strftime("%H:%M") == "21:30"
    response = web.client.post(
        "/alertes/heures-silencieuses",
        data={"csrf": csrf, "debut": "tard", "fin": "07:00"},
        follow_redirects=True,
    )
    assert "Heures attendues au format 22:00" in response.text
    assert quiet_hours(web.engine).enabled


def test_digest_toggle(web: Web) -> None:
    from app.alerts.digest import digest_enabled  # noqa: PLC0415

    web.login()
    page = web.client.get("/alertes").text
    assert "Recevoir le bilan hebdomadaire" in page
    csrf = page.split('name="csrf" value="', 1)[1].split('"', 1)[0]
    response = web.client.post("/alertes/bilan", data={"csrf": csrf}, follow_redirects=True)
    assert "Bilan hebdomadaire désactivé" in response.text
    assert not digest_enabled(web.engine)
    web.client.post("/alertes/bilan", data={"csrf": csrf, "actif": "1"})
    assert digest_enabled(web.engine)


class PhoneSender:
    def __init__(self, error: SendError | None = None) -> None:
        self.sent: list[Message] = []
        self.error = error

    def send(self, message: Message) -> None:
        if self.error is not None:
            raise self.error
        self.sent.append(message)


def _csrf(page: str) -> str:
    return page.split('name="csrf" value="', 1)[1].split('"', 1)[0]


def test_page_title_is_plain_text(web: Web) -> None:
    """Régression : les réglages s'étaient glissés dans la balise <title>."""
    web.login()
    page = web.client.get("/alertes").text
    title = page.split("<title>", 1)[1].split("</title>", 1)[0]
    assert title == "Alertes · Finances"


def test_phone_not_configured_says_so_without_test_button(web: Web) -> None:
    web.login()
    page = web.client.get("/alertes").text
    assert "Envoi non configuré" in page
    assert "https://finances.invalid:8443" in page  # adresse à saisir dans l'application ntfy
    assert "finances-alertes" in page
    assert "/alertes/essai" not in page


def test_test_notification_reaches_the_phone(web: Web) -> None:
    phone = PhoneSender()
    web.app.state.senders = {Channel.NTFY: phone}
    web.login()
    page = web.client.get("/alertes").text
    assert "Envoi configuré" in page
    response = web.client.post("/alertes/essai", data={"csrf": _csrf(page)})
    assert response.status_code == 200  # redirection suivie
    assert "Téléphone (application ntfy) : notification d&#39;essai envoyée" in response.text
    [message] = phone.sent
    assert message.link == "https://finances.invalid/alertes"
    assert "€" not in message.title + message.body


def test_test_notification_failure_explains_what_to_do(web: Web) -> None:
    web.app.state.senders = {Channel.NTFY: PhoneSender(SendError("refus", status=403))}
    web.login()
    csrf = _csrf(web.client.get("/alertes").text)
    page = web.client.post("/alertes/essai", data={"csrf": csrf}).text
    assert "ntfy token add finances" in page and "ntfy_token" in page


def test_test_notification_requires_csrf(web: Web) -> None:
    phone = PhoneSender()
    web.app.state.senders = {Channel.NTFY: phone}
    web.login()
    response = web.client.post("/alertes/essai", data={"csrf": "faux"})
    assert response.status_code in {400, 403}
    assert phone.sent == []
