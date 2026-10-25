"""Pages « Échéances » (FIS-04, LOC-10) : saisie, règlement, export ICS, tableau de bord."""

from datetime import UTC, date, datetime

from sqlalchemy import Engine, insert, select

from app.models import Alert, Deadline, LedgerTransaction, PropertyExpense
from app.models.alerts import Severity
from app.models.tax import DeadlineKind
from app.rental.leases import create_property
from app.tax.deadlines import remind
from tests.web.conftest import T0, Web

_ids = iter(range(1, 100_000))


def _csrf(page: str) -> str:
    return page.split('name="csrf" value="', 1)[1].split('"', 1)[0]


def _tx(engine: Engine, amount: int, day: date) -> int:
    tx_id = next(_ids)
    with engine.begin() as connection:
        connection.execute(
            insert(LedgerTransaction).values(
                id=tx_id,
                account_id=1,
                dedupe_key=f"ext:t:{tx_id}",
                fingerprint="x",
                occurrence=1,
                booking_date=day,
                amount_cents=amount,
                currency="EUR",
                label_raw="PRLV DGFIP IMPOT FONCIER",
                label_clean="DGFIP IMPOT FONCIER",
                created_at=T0,
            )
        )
    return tx_id


def _post(web: Web, path: str, **form: str) -> str:
    csrf = _csrf(web.client.get("/echeances").text)
    response = web.client.post(path, data={"csrf": csrf, **form}, follow_redirects=False)
    assert response.status_code == 303, response.text
    return str(response.headers["location"])


def _property(web: Web) -> int:
    return create_property(
        web.engine, name="Studio", address=None, furnished=True, shares=[("moi", 10000)], now=T0
    )


def test_create_property_tax_and_settle_it_automatically(web: Web) -> None:
    web.login()
    property_id = _property(web)
    paid = _tx(web.engine, -123_400, date(2026, 10, 14))
    location = _post(
        web,
        "/echeances",
        nature="taxe_fonciere",
        echeance="2026-10-15",
        bien=str(property_id),
        precision="avis 2026",
        montant="1\u202f234,00\u00a0€",
        recuperable="150",
        rappel="7",
        annuelle="1",
    )
    assert location.startswith("/echeances/")
    with web.engine.connect() as connection:
        rows = connection.execute(select(Deadline).order_by(Deadline.due_date)).all()
        charge = connection.execute(select(PropertyExpense)).one()
    # Le paiement du montant exact est reconnu tout de suite ; l'an prochain est au calendrier.
    assert [(r.due_date, r.transaction_id, r.done_at is not None) for r in rows] == [
        (date(2026, 10, 15), paid, True),
        (date(2027, 10, 15), None, False),
    ]
    assert (rows[0].amount_cents, rows[0].recoverable_cents) == (123_400, 15_000)
    assert charge.transaction_id == paid

    detail = web.client.get(location.split("?")[0]).text
    assert "réglée le" in detail
    assert "DGFIP IMPOT FONCIER" in detail
    page = web.client.get("/echeances").text
    assert "Réglées récemment" in page
    assert "Taxe foncière · Studio" in page

    reopened = _post(web, f"{location.split('?')[0]}/rouvrir")
    assert "Règlement annulé" in web.client.get(reopened).text


def test_manual_settlement_edit_and_delete(web: Web) -> None:
    web.login()
    location = _post(web, "/echeances", nature="cfe", echeance="2026-12-15", montant="300")
    path = location.split("?")[0]
    paid = _tx(web.engine, -29_000, date(2026, 12, 1))
    page = web.client.get(path).text
    assert f'value="{paid}"' in page  # proposée, montant proche
    _post(
        web,
        f"{path}/modifier",
        nature="cfe",
        echeance="2026-12-16",
        montant="290,00",
        rappel="10",
    )
    with web.engine.connect() as connection:
        row = connection.execute(select(Deadline)).one()
    # Montant corrigé : le paiement exact est reconnu ; sans case « annuelle », pas de suivante.
    assert (row.due_date, row.amount_cents, row.remind_days, row.yearly) == (
        date(2026, 12, 16),
        29_000,
        10,
        False,
    )
    assert row.transaction_id == paid
    _post(web, f"{path}/rouvrir")
    assert "règlement" in web.client.get(_post(web, f"{path}/regler", operation="")).text
    _post(web, f"{path}/supprimer")
    with web.engine.connect() as connection:
        assert connection.execute(select(Deadline)).all() == []


def test_errors_are_explained(web: Web) -> None:
    web.login()

    def message(path: str, **form: str) -> str:
        return web.client.get(_post(web, path, **form)).text

    new = "/echeances"
    assert "Montant illisible" in message(new, nature="cfe", echeance="2026-10-15", montant="a")
    assert "Nature d&#39;échéance inconnue" in message(new, nature="x", echeance="2026-10-15")
    assert "Bien introuvable" in message(new, nature="cfe", echeance="2026-10-15", bien="x")
    path = _post(web, new, nature="cfe", echeance="2026-10-15").split("?")[0]
    assert "Opération introuvable" in message(f"{path}/regler", operation="abc")
    assert "Seule une dépense" in message(f"{path}/regler", operation="999")
    edit = f"{path}/modifier"
    assert "Bien introuvable" in message(edit, nature="cfe", echeance="2026-10-15", bien="7")
    _post(web, new, nature="cfe", echeance="2026-12-15")
    assert "existe déjà" in message(edit, nature="cfe", echeance="2026-12-15")
    assert "Échéance non réglée" in message(f"{path}/rouvrir")
    assert "introuvable" in message("/echeances/999/supprimer")
    assert "introuvable" in web.client.get("/echeances/999").text


def test_usual_deadlines_and_ics_export(web: Web) -> None:
    web.login()
    _property(web)
    assert "3 échéance(s) ajoutée(s)" in web.client.get(_post(web, "/echeances/habituelles")).text
    assert "déjà au calendrier" in web.client.get(_post(web, "/echeances/habituelles")).text
    response = web.client.get("/echeances.ics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/calendar")
    assert "attachment" in response.headers["content-disposition"]
    body = response.text
    assert body.count("BEGIN:VEVENT") == 3
    assert "SUMMARY:Taxe foncière · Studio" in body


def test_dashboard_tile_and_alert_link(web: Web) -> None:
    web.login()
    property_id = _property(web)
    location = _post(
        web, "/echeances", nature="taxe_fonciere", echeance="2026-10-01", bien=str(property_id)
    )
    deadline_id = int(location.split("?")[0].rsplit("/", 1)[1])
    page = web.client.get("/").text
    assert "Prochaines échéances" in page
    assert f'href="/echeances/{deadline_id}"' in page
    remind(web.engine, now=datetime(2026, 9, 27, 10, 0, tzinfo=UTC))
    with web.engine.connect() as connection:
        alert = connection.execute(select(Alert)).one()
    assert alert.severity is Severity.WARNING
    detail = web.client.get(f"/alertes/{alert.id}").text
    assert f'href="/echeances/{deadline_id}"' in detail
    assert "Voir l&#39;échéance" in detail


def test_deadline_pages_require_login(web: Web) -> None:
    for path in ("/echeances", "/echeances.ics", "/echeances/1"):
        response = web.client.get(path, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"].startswith("/connexion")


def test_navigation_lists_deadlines(web: Web) -> None:
    web.login()
    assert 'href="/echeances" aria-current="page"' in web.client.get("/echeances").text
    assert DeadlineKind.PROPERTY_TAX.value in web.client.get("/echeances").text
