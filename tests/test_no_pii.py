"""Aucune donnée personnelle dans les fichiers versionnés (inv. 5).

- IBAN à clé valide : seuls les IBAN fictifs (code banque 99999) sont admis ;
- e-mails : seulement des domaines d'exemple ou techniques ;
- noms : si FINANCES_PII_NAMES_FILE désigne une liste locale (un nom par ligne, jamais
  versionnée), aucun de ces noms ne doit apparaître.
"""

import os
import re
import subprocess
from pathlib import Path

import pytest

from app.iban import is_valid_iban, normalize_iban
from tests.factories import FAKE_BANK_CODE

ROOT = Path(__file__).resolve().parents[1]
IBAN_CANDIDATE = re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,3})?\b")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})")
ALLOWED_EMAIL_DOMAINS = {
    "example.org",
    "example.com",
    "github.com",  # URL SSH git@github.com
    "users.noreply.github.com",
    "anthropic.com",
}
SKIPPED = {"uv.lock"}


def _tracked_text_files() -> list[Path]:
    listed = subprocess.run(
        ["git", "ls-files"],  # noqa: S607
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    files = []
    for name in listed:
        path = ROOT / name
        if name in SKIPPED or not path.is_file():
            continue
        try:
            path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        files.append(path)
    return files


FILES = _tracked_text_files()


def _ids(path: Path) -> str:
    return str(path.relative_to(ROOT))


@pytest.mark.parametrize("path", FILES, ids=_ids)
def test_no_real_iban(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    real = [
        match.group(0)
        for match in IBAN_CANDIDATE.finditer(text)
        if is_valid_iban(match.group(0)) and normalize_iban(match.group(0))[4:9] != FAKE_BANK_CODE
    ]
    assert real == [], f"IBAN au format réel dans {_ids(path)}"


@pytest.mark.parametrize("path", FILES, ids=_ids)
def test_no_personal_email(path: Path) -> None:
    domains = {m.group(1).lower() for m in EMAIL.finditer(path.read_text(encoding="utf-8"))}
    assert domains <= ALLOWED_EMAIL_DOMAINS, f"e-mail non factice dans {_ids(path)} : {domains}"


def test_no_listed_names() -> None:
    names_file = os.environ.get("FINANCES_PII_NAMES_FILE")
    if not names_file:
        pytest.skip("aucune liste locale de noms (FINANCES_PII_NAMES_FILE)")
    names = [
        n.strip().lower() for n in Path(names_file).read_text("utf-8").splitlines() if n.strip()
    ]
    found = [
        (_ids(path), name)
        for path in FILES
        for name in names
        if name in path.read_text(encoding="utf-8").lower()
    ]
    assert found == []
