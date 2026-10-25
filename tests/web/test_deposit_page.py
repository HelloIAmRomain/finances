"""Dépôt de garantie depuis la fiche du bail (LEG-04)."""

from datetime import date

from sqlalchemy import select

from app.models import DepositEvent, Lease, RentAllocation
from tests.web.conftest import Web
from tests.web.test_rental import _create, _csrf, _tx


def _post(web: Web, path: str, **form: str) -> str:
    csrf = _csrf(web.client.get("/loyers").text)
    return web.client.post(path, data={"csrf": csrf, **form}).text


def test_full_deposit_cycle_from_the_lease_page(web: Web) -> None:
    web.login()
    _create(web, depot="1600")
    with web.engine.connect() as connection:
        lease_id = connection.scalar(select(Lease.id))
    page = web.client.get(f"/loyers/{lease_id}").text
    assert "Dépôt de garantie" in page and "à encaisser" in page and "Aucun mouvement" in page
    deposit_tx = _tx(web.engine, 160000, "VIR ALICE EXEMPLE DEPOT", date(2026, 9, 1))
    page = web.client.get(f"/loyers/{lease_id}").text
    assert f'<option value="{deposit_tx}">' in page  # proposée à l'encaissement

    page = _post(web, f"/loyers/{lease_id}/depot", nature="received", operation=str(deposit_tx))
    assert "Dépôt encaissé enregistré" in page and "1\u202f600,00\u00a0€" in page
    with web.engine.connect() as connection:
        allocated = set(connection.scalars(select(RentAllocation.transaction_id)))
    assert deposit_tx not in allocated  # le dépôt n'est pas pris pour un loyer

    page = _post(web, f"/loyers/{lease_id}/sortie", remise_cles="2026-09-20",
                 etat_des_lieux="non_conforme")  # fmt: skip
    assert "Fin de location enregistrée" in page and "20/11/2026" in page

    page = _post(web, f"/loyers/{lease_id}/depot", nature="withheld", montant="100", motif="")
    assert "Indiquez le motif" in page
    page = _post(web, f"/loyers/{lease_id}/depot", nature="withheld", montant="100,00",
                 motif="Store cassé (facture)")  # fmt: skip
    assert "gardez le justificatif" in page and "Store cassé (facture)" in page
    page = _post(web, f"/loyers/{lease_id}/depot", nature="returned", montant="1 500",
                 date="2026-10-01")  # fmt: skip
    assert "Restitution enregistrée" in page and "soldé" in page

    with web.engine.connect() as connection:
        event_id = connection.scalar(select(DepositEvent.id).where(DepositEvent.kind == "returned"))
    page = _post(web, f"/loyers/{lease_id}/depot/{event_id}/supprimer")
    assert "Mouvement retiré" in page and "à restituer" in page


def test_errors_are_explained(web: Web) -> None:
    web.login()
    _create(web, depot="1600")
    with web.engine.connect() as connection:
        lease_id = connection.scalar(select(Lease.id))
    page = _post(web, f"/loyers/{lease_id}/depot", nature="received")
    assert "Indiquez un montant positif" in page
    page = _post(web, f"/loyers/{lease_id}/depot", nature="received", montant="beaucoup")
    assert "écrivez par exemple 1 234,56" in page
    page = _post(web, f"/loyers/{lease_id}/depot", nature="received", montant="10", date="hier")
    assert "Date illisible" in page
    page = _post(web, f"/loyers/{lease_id}/sortie", remise_cles="31/12")
    assert "Date illisible" in page
    page = _post(web, f"/loyers/{lease_id}/depot/999/supprimer")
    assert "introuvable" in page


def test_deposit_above_legal_maximum_is_flagged(web: Web) -> None:
    web.login()
    _create(web, depot="2000")  # loyer 800 € hors charges, meublé : 1 600 € au plus
    with web.engine.connect() as connection:
        lease_id = connection.scalar(select(Lease.id))
    page = web.client.get(f"/loyers/{lease_id}").text
    assert "supérieur au maximum légal" in page
