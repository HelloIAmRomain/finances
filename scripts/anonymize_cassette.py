"""Anonymise une réponse d'API enregistrée avant de la verser dans tests/cassettes (inv. 5).

Remplace, de façon cohérente dans tout le fichier :
- chaque IBAN à clé valide par un IBAN fictif (code banque 99999) ;
- chaque adresse e-mail par une adresse @example.org ;
- chaque nom de la liste locale (un par ligne, fichier **jamais versionné**) par un nom fictif ;
- les numéros de téléphone français par un numéro fictif.

Usage : uv run python scripts/anonymize_cassette.py ENTRÉE SORTIE --names ~/noms-a-masquer.txt
Relire toujours le résultat : `tests/test_no_pii.py` le vérifie aussi en CI.
"""

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.iban import is_valid_iban, normalize_iban
from tests.factories import FAKE_BANK_CODE, fake_email, fake_iban, fake_name

IBAN_CANDIDATE = re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,3})?\b")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_FR = re.compile(r"(?:\+33\s?|\b0)[1-9](?:[\s.-]?\d{2}){4}\b")


def anonymize(text: str, names: list[str]) -> str:
    ibans: dict[str, str] = {}

    def replace_iban(match: re.Match[str]) -> str:
        found = match.group(0)
        compact = normalize_iban(found)
        if not is_valid_iban(compact) or compact[4:9] == FAKE_BANK_CODE:
            return found
        return ibans.setdefault(compact, fake_iban(len(ibans) + 1))

    emails: dict[str, str] = {}
    text = IBAN_CANDIDATE.sub(replace_iban, text)
    text = EMAIL.sub(lambda m: emails.setdefault(m.group(0), fake_email(len(emails) + 1)), text)
    text = PHONE_FR.sub("0100000000", text)
    for index, name in enumerate(sorted(names, key=len, reverse=True)):
        text = re.sub(re.escape(name), fake_name(index), text, flags=re.IGNORECASE)
    return text


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    parser.add_argument("--names", type=Path, help="noms à masquer, un par ligne (non versionné)")
    args = parser.parse_args()
    names = []
    if args.names:
        names = [
            line.strip() for line in args.names.read_text("utf-8").splitlines() if line.strip()
        ]
    args.target.write_text(anonymize(args.source.read_text("utf-8"), names), "utf-8")
    print(f"anonymisé : {args.target} (relire avant de versionner)")


if __name__ == "__main__":
    main()
