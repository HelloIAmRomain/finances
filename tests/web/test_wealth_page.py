"""Page « Patrimoine » (COL-05)."""

from sqlalchemy import select

from app.models import AssetValuation, ManualAsset
from tests.web.conftest import Web


def _csrf(page: str) -> str:
    return page.split('name="csrf" value="', 1)[1].split('"', 1)[0]


def test_add_value_and_archive(web: Web) -> None:
    web.login()
    page = web.client.get("/patrimoine").text
    assert "Rien de saisi" in page
    csrf = _csrf(page)

    def post(path: str, **form: str) -> str:
        return web.client.post(path, data={"csrf": csrf, **form}).text

    assert "Actif ajouté" in post(
        "/patrimoine", nom="Livret A", nature="livret", valeur="10 000,00", compte="1"
    )
    assert "Nature inconnue" in post("/patrimoine", nom="X", nature="bitcoin")
    assert "Montant illisible" in post("/patrimoine", nom="X", nature="pea", valeur="beaucoup")
    with web.engine.connect() as connection:
        asset_id = connection.execute(
            select(ManualAsset.id).where(ManualAsset.name == "Livret A")
        ).scalar_one()
    assert "Valeur enregistrée" in post(
        f"/patrimoine/{asset_id}/valeur", valeur="10 250,00", date_valeur="2026-10-01"
    )
    page = web.client.get("/patrimoine").text
    assert "10\u202f250,00" in page and "Historique" in page
    assert "Patrimoine net : " in web.client.get("/").text
    assert "Montant obligatoire" in post(f"/patrimoine/{asset_id}/valeur", valeur="")
    assert "Actif archivé" in post(f"/patrimoine/{asset_id}/archiver")
    assert "Actif introuvable" in post(f"/patrimoine/{asset_id}/archiver")
    with web.engine.connect() as connection:
        assert len(connection.execute(select(AssetValuation)).all()) == 2
