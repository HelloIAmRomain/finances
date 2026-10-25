"""Page « Import » (COL-02) : aperçu, confirmation, réimport sans doublon."""

import re
from pathlib import Path

from sqlalchemy import func, select

from app.models import LedgerTransaction
from tests.web.conftest import Web

DATA = Path(__file__).resolve().parents[1] / "data"


def _hidden(page: str, name: str) -> str:
    match = re.search(rf'name="{name}" value="([^"]*)"', page)
    assert match, name
    return match.group(1)


def _preview(web: Web, path: Path, file_format: str) -> str:
    csrf = _hidden(web.client.get("/import").text, "csrf")
    response = web.client.post(
        "/import/apercu",
        data={"csrf": csrf, "compte": "1", "format": file_format},
        files={"fichier": (path.name, path.read_bytes(), "text/csv")},
    )
    return response.text


def _confirm(web: Web, preview: str) -> str:
    fields = ("csrf", "compte", "format", "nom_fichier", "contenu", "empreinte")
    return web.client.post("/import/confirmer", data={f: _hidden(preview, f) for f in fields}).text


def _count(web: Web) -> int:
    with web.engine.connect() as connection:
        return int(
            connection.execute(select(func.count()).select_from(LedgerTransaction)).scalar_one()
        )


def test_preview_then_confirm_then_reimport(web: Web) -> None:
    web.login()
    preview = _preview(web, DATA / "csv" / "boursorama_exemple.csv", "boursorama")
    assert "4 opération(s) lue(s)" in preview
    assert _count(web) == 0  # l'aperçu n'écrit rien
    done = _confirm(web, preview)
    assert "4 nouvelle(s) opération(s) sur 4 lue(s)" in done
    again = _confirm(web, _preview(web, DATA / "csv" / "boursorama_exemple.csv", "boursorama"))
    assert "0 nouvelle(s) opération(s) sur 4 lue(s)" in again
    assert _count(web) == 4


def test_ofx_import(web: Web) -> None:
    web.login()
    done = _confirm(web, _preview(web, DATA / "ofx" / "releve_sgml_exemple.ofx", "ofx"))
    assert "3 nouvelle(s) opération(s)" in done


def test_wrong_profile_gives_clear_error(web: Web) -> None:
    web.login()
    page = _preview(web, DATA / "csv" / "societe_generale_exemple.csv", "boursorama")
    assert "en-tête introuvable" in page or "encodage" in page
    assert _count(web) == 0


def test_tampered_content_is_refused(web: Web) -> None:
    web.login()
    preview = _preview(web, DATA / "csv" / "boursorama_exemple.csv", "boursorama")
    fields = {
        f: _hidden(preview, f) for f in ("csrf", "compte", "format", "nom_fichier", "empreinte")
    }
    fields["contenu"] = "ZmF1eA=="  # « faux »
    response = web.client.post("/import/confirmer", data=fields)
    assert response.status_code == 422 and "altéré" in response.text
    assert _count(web) == 0


def test_upload_requires_csrf(web: Web) -> None:
    web.login()
    path = DATA / "csv" / "boursorama_exemple.csv"
    response = web.client.post(
        "/import/apercu",
        data={"compte": "1", "format": "boursorama"},
        files={"fichier": (path.name, path.read_bytes(), "text/csv")},
    )
    assert response.status_code == 403
