"""Socle web : PWA, formats français, en-têtes de sécurité, rien d'externe (UI-01, UI-05)."""

import hashlib
import json
import re
import struct
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.web.main import STATIC_DIR
from app.web.security import MAX_REQUEST_BYTES
from app.web.templating import TEMPLATES_DIR, amount_class, date_fr, datetime_fr, eur, markdown
from tests.web.conftest import Web


@pytest.fixture
def client(web: Web) -> TestClient:
    web.login()
    return web.client


def test_home_is_french_and_uses_local_scripts(client: TestClient) -> None:
    page = client.get("/").text
    assert '<html lang="fr">' in page
    assert "/static/vendor/htmx-2.0.11.min.js" in page
    assert "lecture seule" in page


def test_assets_use_relative_urls(client: TestClient) -> None:
    """Derrière `tailscale serve` (HTTPS), une URL absolue en http:// serait bloquée par le
    navigateur (contenu mixte) : page sans styles ni scripts. Toutes les ressources sont
    donc référencées par un chemin relatif."""
    for path in ("/", "/comptes", "/connexions", "/transactions"):
        page = client.get(path).text
        assert re.findall(r'(?:src|href)="https?://', page) == [], path


def test_security_headers(client: TestClient) -> None:
    response = client.get("/")
    csp = response.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert "unsafe" not in csp
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["Cache-Control"] == "no-store"


def test_security_headers_on_errors_too(web: Web, caplog: pytest.LogCaptureFixture) -> None:
    def boom() -> None:
        raise RuntimeError("panne simulée")

    web.app.get("/panne-de-test")(boom)
    web.login()
    missing = web.client.get("/page-inexistante")
    failed = web.client.get("/panne-de-test")
    assert (missing.status_code, failed.status_code) == (404, 500)
    assert failed.text.startswith("Erreur inattendue")  # en français, avec la marche à suivre
    assert "panne simulée" not in failed.text and "Traceback" not in failed.text
    assert "panne simulée" in caplog.text  # détail réservé au journal
    for response in (missing, failed):
        assert response.headers["X-Frame-Options"] == "DENY"
        assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
        assert response.headers["Cache-Control"] == "no-store"


def test_oversized_requests_are_refused_before_authentication(web: Web) -> None:
    too_big = MAX_REQUEST_BYTES + 1
    declared = web.client.post(
        "/import/apercu", content=b"x" * too_big, headers={"Content-Type": "text/plain"}
    )
    assert declared.status_code == 413 and declared.text.startswith("Envoi trop volumineux")
    assert declared.headers["X-Frame-Options"] == "DENY"

    def chunks() -> Iterator[bytes]:  # sans Content-Length : compté à la lecture
        yield b'--limite\r\nContent-Disposition: form-data; name="fichier"; filename="r"\r\n\r\n'
        for _ in range(too_big // 65_536 + 1):
            yield b"x" * 65_536

    streamed = web.client.post(
        "/import/apercu",
        content=chunks(),
        headers={"Content-Type": "multipart/form-data; boundary=limite"},
    )
    assert streamed.status_code == 413
    lying = web.client.post("/mcp", content=b"{}", headers={"Content-Length": "abc"})
    assert lying.status_code in (400, 413)
    assert web.client.post("/connexion", data={"identifiant": "x"}).status_code == 422


def test_static_files_may_be_cached(client: TestClient) -> None:
    response = client.get("/static/app.css")
    assert response.status_code == 200
    assert response.headers.get("Cache-Control") != "no-store"


def _png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def test_manifest_is_installable(client: TestClient) -> None:
    response = client.get("/manifest.webmanifest")
    assert response.headers["content-type"].startswith("application/manifest+json")
    manifest = json.loads(response.text)
    assert manifest["display"] == "standalone" and manifest["lang"] == "fr"
    sizes = {icon["sizes"] for icon in manifest["icons"]}
    assert {"192x192", "512x512"} <= sizes
    for icon in manifest["icons"]:
        path = STATIC_DIR / icon["src"].removeprefix("/static/")
        assert _png_size(path) == tuple(int(n) for n in icon["sizes"].split("x"))


def test_service_worker_caches_nothing(client: TestClient) -> None:
    response = client.get("/sw.js")
    assert response.headers["content-type"].startswith("text/javascript")
    assert "caches." not in response.text  # aucune donnée financière conservée hors ligne


def test_templates_reference_no_external_resource() -> None:
    for template in TEMPLATES_DIR.rglob("*.html"):
        text = template.read_text(encoding="utf-8")
        assert not re.search(r"(src|href)=[\"']?(https?:)?//", text), template.name


def test_no_inline_script_or_style() -> None:
    """CSP stricte : aucun script ni style en ligne dans les gabarits."""
    for template in TEMPLATES_DIR.rglob("*.html"):
        text = template.read_text(encoding="utf-8")
        # Seuls les blocs de données JSON (non exécutés) sont admis en ligne.
        assert not re.search(r"<script(?![^>]*\b(src=|type=\"application/json\"))[^>]*>", text), (
            template.name
        )
        assert "<style" not in text and " style=" not in text, template.name
        assert not re.search(r"\son[a-z]+=", text), template.name  # onclick=…


def test_vendored_files_match_checksums() -> None:
    vendor = STATIC_DIR / "vendor"
    for line in (vendor / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split()
        assert hashlib.sha256((vendor / name).read_bytes()).hexdigest() == digest, name


def test_filters() -> None:
    assert eur(-123456) == "-1\u202f234,56\u00a0€"
    assert date_fr(date(2026, 9, 7)) == "07/09/2026"
    assert date_fr(None) == ""
    # 22:30 UTC en été = 00:30 le lendemain à Paris.
    assert datetime_fr(datetime(2026, 7, 14, 22, 30, tzinfo=UTC)) == "15/07/2026 à 00:30"
    assert date_fr(datetime(2026, 7, 14, 22, 30, tzinfo=UTC)) == "15/07/2026"
    assert (amount_class(-1), amount_class(0), amount_class(5)) == ("negatif", "", "positif")


def test_sentence_keeps_acronyms_and_proper_nouns() -> None:
    """Bug : `capitalize` mettait la suite en minuscules (« indice t2 2026 manquant »)."""
    from app.web.templating import sentence  # noqa: PLC0415

    assert sentence("indice T2 2026 manquant (IRL)") == "Indice T2 2026 manquant (IRL)"
    assert sentence("") == "" and sentence(None) == ""


def test_navigation_sections() -> None:
    from app.web.templating import NAVIGATION, current_section  # noqa: PLC0415

    titles = [title for title, _ in NAVIGATION]
    assert titles[current_section("/") or 0] == "Tableau de bord"
    assert titles[current_section("/transactions") or 0] == "Mon argent"
    assert titles[current_section("/loyers/3/irl/indice") or 0] == "Location et impôts"
    assert titles[current_section("/biens/2") or 0] == "Location et impôts"
    assert titles[current_section("/ia/modifications") or 0] == "Réglages"
    assert titles[current_section("/alertes/7") or 0] == "À traiter"
    assert current_section("/connexion") is None  # page de connexion, sans rubrique
    assert current_section("/echeancesX") is None


def test_grouped_navigation_is_rendered(web: Web) -> None:
    web.login()
    page = web.client.get("/budgets").text
    assert 'class="rubrique-active">Mon argent</a>' in page
    assert '<nav class="sous-navigation" aria-label="Mon argent">' in page
    assert 'href="/budgets" aria-current="page"' in page
    dashboard = web.client.get("/").text
    assert 'href="/" aria-current="page">Tableau de bord' in dashboard
    assert "sous-navigation" not in dashboard


def test_phone_notifications_are_reachable_from_settings(web: Web) -> None:
    """Réglages › Notifications mène aux réglages de la page Alertes (essai du téléphone)."""
    web.login()
    page = web.client.get("/securite").text
    assert 'href="/alertes#telephone"' in page and ">Notifications</a>" in page
    assert 'id="telephone"' in web.client.get("/alertes").text


def test_assistant_answers_carry_no_clickable_link_or_image() -> None:
    """Une injection dans un libellé pourrait faire écrire à l'IA un lien qui emporte des
    données au clic : liens, adresses et images restent du texte."""
    html = str(
        markdown(
            "[ici](https://evil.example/?d=x) <https://x.example> ![i](https://e/i.png) **ok**"
        )
    )
    assert "<a " not in html and "<img" not in html
    assert "https://evil.example/?d=x" in html and "<strong>ok</strong>" in html
