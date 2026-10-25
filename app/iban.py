"""IBAN : normalisation et contrôle de la clé (ISO 13616, modulo 97)."""

import re

_IBAN_SHAPE = re.compile(r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}")


def normalize_iban(iban: str) -> str:
    return re.sub(r"\s+", "", iban).upper()


def is_valid_iban(iban: str) -> bool:
    compact = normalize_iban(iban)
    if not _IBAN_SHAPE.fullmatch(compact):
        return False
    rearranged = compact[4:] + compact[:4]
    return int("".join(str(int(char, 36)) for char in rearranged)) % 97 == 1


def with_check_digits(country: str, bban: str) -> str:
    """IBAN complet à partir du pays et du BBAN (calcule la clé)."""
    numeric = "".join(str(int(char, 36)) for char in bban + country + "00")
    return f"{country}{98 - int(numeric) % 97:02d}{bban}"
