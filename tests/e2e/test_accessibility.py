"""Accessibilité (charte d'ergonomie §3, UI-01) : chaque page passe le contrôle automatique axe-core
(WCAG 2.1 A et AA), en thème clair et en thème sombre. Aucune violation grave ou critique."""

import json
from pathlib import Path

import pytest
from playwright.sync_api import Page

from tests.e2e.conftest import Site, login
from tests.e2e.test_browser import PAGES

AXE = (Path(__file__).parent / "vendor" / "axe-4.14.0.min.js").read_text(encoding="utf-8")
BLOCKING = {"serious", "critical"}


def _violations(page: Page) -> list[dict[str, object]]:
    page.evaluate(AXE)  # hors CSP : évaluation par l'outil de test, pas par la page
    result = page.evaluate(
        """async () => (await axe.run(document, {
            runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'] },
        })).violations.map(v => ({
            id: v.id, impact: v.impact, help: v.help,
            cibles: v.nodes.slice(0, 3).map(n => n.target.join(' ')),
        }))"""
    )
    return [v for v in result if v["impact"] in BLOCKING]


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("path", PAGES)
def test_page_has_no_serious_accessibility_violation(
    page: Page, site: Site, path: str, scheme: str
) -> None:
    page.emulate_media(color_scheme=scheme)  # type: ignore[arg-type]
    login(page, site)
    page.goto(f"{site.base_url}{path}")
    page.wait_for_load_state("networkidle")
    violations = _violations(page)
    assert violations == [], f"{path} ({scheme}) : {json.dumps(violations, ensure_ascii=False)}"


def test_login_page_is_accessible(page: Page, site: Site) -> None:
    page.goto(f"{site.base_url}/connexion")
    assert _violations(page) == []
