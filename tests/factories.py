"""Données factices pour les tests (inv. 5) : jamais d'IBAN, de nom ni d'e-mail réels.

Les IBAN fictifs utilisent le code banque 99999, qui n'existe pas ; ce sont les seuls que le
hook gitleaks et `tests/test_no_pii.py` acceptent dans le dépôt.
"""

from app.iban import with_check_digits

FAKE_BANK_CODE = "99999"
FAKE_NAMES = ("Alice Exemple", "Bruno Fictif", "Chloé Démo", "SCI Imaginaire")
FAKE_EMAIL_DOMAIN = "example.org"


def fake_iban(number: int, country: str = "FR") -> str:
    """IBAN français fictif, à clé valide, déterministe (même numéro → même IBAN)."""
    if not 0 <= number < 10**11:
        raise ValueError("numéro hors plage")
    bban = f"{FAKE_BANK_CODE}00001{number:011d}00"  # banque, guichet, compte, clé RIB factice
    return with_check_digits(country, bban)


def fake_name(number: int) -> str:
    return FAKE_NAMES[number % len(FAKE_NAMES)]


def fake_email(number: int) -> str:
    return f"personne{number}@{FAKE_EMAIL_DOMAIN}"
