"""Régression : un titre de page (balise <title>) est du texte seul, jamais du balisage."""

import re
from pathlib import Path

TEMPLATES = Path(__file__).parents[2] / "app" / "web" / "templates"
TITLE = re.compile(r"{% block title %}(.*?){% endblock %}", re.DOTALL)


def test_every_title_block_is_plain_text() -> None:
    checked = 0
    for template in sorted(TEMPLATES.glob("*.html")):
        for title in TITLE.findall(template.read_text(encoding="utf-8")):
            assert "<" not in title and "\n" not in title, template.name
            checked += 1
    assert checked > 10  # les pages ont bien un titre
