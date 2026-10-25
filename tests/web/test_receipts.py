"""Quittances depuis l'interface (LOC-07) : informations du bien, aperçu, PDF, rien d'envoyé."""

from datetime import date

from sqlalchemy import select

from app.models import AuditLog, Lease, RentDue
from tests.web.conftest import Web
from tests.web.test_rental import _create, _csrf, _tx


def _paid_lease(web: Web) -> tuple[int, int, int]:
    web.login()
    _create(web)
    _tx(web.engine, 85000, "VIR DE ALICE EXEMPLE", date(2026, 9, 2))
    web.client.post("/loyers/actualiser", data={"csrf": _csrf(web.client.get("/loyers").text)})
    with web.engine.connect() as connection:
        lease_id, property_id = connection.execute(select(Lease.id, Lease.property_id)).one()
        due_id = connection.scalar(select(RentDue.id).where(RentDue.period == "2026-09"))
    assert property_id is not None and due_id is not None
    return lease_id, property_id, due_id


def test_missing_details_are_explained_then_receipt_is_downloadable(web: Web) -> None:
    lease_id, property_id, due_id = _paid_lease(web)
    page = web.client.get(f"/loyers/{lease_id}").text
    assert "Quittances" in page and "nom et l&#39;adresse du bailleur" in page
    assert f"/loyers/{lease_id}/echeances/{due_id}/quittance" in page
    refused = web.client.get(f"/loyers/{lease_id}/echeances/{due_id}/quittance")
    assert "renseignez d&#39;abord" in refused.text  # retour au bail, avec la marche à suivre

    csrf = _csrf(web.client.get(f"/biens/{property_id}").text)
    saved = web.client.post(
        f"/biens/{property_id}/quittances",
        data={
            "csrf": csrf,
            "bailleur": "A. Démo et B. Exemple\n1 rue de l'Exemple, 75000 Paris",
            "adresse": "3 avenue Fictive, 69000 Lyon",
        },
    )
    assert "Informations des quittances enregistrées" in saved.text
    assert "3 avenue Fictive, 69000 Lyon" in saved.text  # formulaire pré-rempli
    assert "nom et l&#39;adresse du bailleur" not in web.client.get(f"/loyers/{lease_id}").text

    preview = web.client.get(f"/loyers/{lease_id}/echeances/{due_id}/quittance").text
    assert "<title>Quittance 2026-09 · Finances</title>" in preview
    assert "Période du 01/09/2026 au 30/09/2026" in preview and "Alice Exemple" in preview
    assert "Rien n'est envoyé" in preview

    pdf = web.client.get(f"/loyers/{lease_id}/echeances/{due_id}/quittance.pdf")
    assert pdf.status_code == 200
    assert pdf.headers["content-type"] == "application/pdf"
    assert 'filename="quittance-2026-09.pdf"' in pdf.headers["content-disposition"]
    assert pdf.content.startswith(b"%PDF-1.4") and b"Alice Exemple" in pdf.content
    with web.engine.connect() as connection:
        assert "receipt.issued" in set(connection.scalars(select(AuditLog.action)))


def test_unpaid_month_has_no_receipt(web: Web) -> None:
    lease_id, _, _ = _paid_lease(web)
    with web.engine.connect() as connection:
        october = connection.scalar(select(RentDue.id).where(RentDue.period == "2026-10"))
    assert october is not None  # échéance d'octobre créée en avance (fin septembre)
    page = web.client.get(f"/loyers/{lease_id}").text
    assert f"/echeances/{october}/quittance" not in page
    response = web.client.get(f"/loyers/{lease_id}/echeances/{october}/quittance.pdf")
    assert "pas encore entièrement payée" in response.text


def test_too_long_details_are_refused(web: Web) -> None:
    _, property_id, _ = _paid_lease(web)
    csrf = _csrf(web.client.get(f"/biens/{property_id}").text)
    page = web.client.post(
        f"/biens/{property_id}/quittances", data={"csrf": csrf, "bailleur": "x" * 401}
    ).text
    assert "Texte trop long" in page
