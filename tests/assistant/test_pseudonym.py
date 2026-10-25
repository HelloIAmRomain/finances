"""Pseudonymisation de ce que reçoit l'IA (inv. 8, NF-SEC-07, docs/spec-mcp.md §5).

Libellés inspirés de la **forme** des libellés réels des deux banques (relevée sans les lire :
lettres et chiffres masqués) ; noms et références inventés.
"""

import re

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.assistant.pseudonym import LeakError, Pseudonymizer, initials, looks_pseudonymized
from tests.assistant.conftest import Demo

FAKE_IBAN = "FR26 9999 9000 0100 0000 0000 045"
PERSONS = ("Alice Martinez", "Bruno Lefort", "Chloé Dubreuil")


@pytest.fixture
def masker() -> Pseudonymizer:
    return Pseudonymizer(persons=PERSONS, emitters=("ACME INDUSTRIES", "CAF"))


def test_initials() -> None:
    assert initials("DUPONT") == "D."
    assert initials("Jean-Pierre") == "J.-P."
    assert initials("é") == "E."
    assert initials("J.") == "J."


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Paiements par carte : commerçant conservé, date gardée, fin de numéro de carte masquée
        ("CARTE 12/09/26 CARREFOUR CITY CB*4521", "CARTE 12/09/26 CARREFOUR CITY CB*#"),
        ("CARTE 03/09/26 SNCF INTERNET 2 CB*4521", "CARTE 03/09/26 SNCF INTERNET 2 CB*#"),
        ("RETRAIT DAB 01/09/26 PARIS CB*4521", "RETRAIT DAB 01/09/26 PARIS CB*#"),
        # Virements Boursorama : civilité puis nom → initiales
        ("VIR INST M ALBERT DURAND", "VIR INST M A. D."),
        ("VIR INST MME ZOE GARNIER", "VIR INST MME Z. G."),
        (
            "VIR LOYER SEPTEMBRE M PAUL ROUX OU MME LEA ROUX : R12AB",
            "VIR LOYER SEPTEMBRE M P. R. OU MME L. R. : R12AB",
        ),
        # Virement sans civilité : tout mot hors vocabulaire est abrégé
        ("VIR VIREMENT DE HUGO BLANCHARD", "VIR VIREMENT DE H. B."),
        ("VIR INST ACME INDUSTRIES SALAIRE AOUT", "VIR INST ACME INDUSTRIES SALAIRE AOUT"),
        ("VIR SEPA CAF PRESTATIONS", "VIR SEPA CAF PRESTATIONS"),
        # Société Générale : champs DE: / POUR:, références masquées
        (
            "000123 VIR INSTANTANE EMIS WEB POUR: MME NINA PERRIN DATE: 02/09/2026 10:15 "
            "REF: 12345678",
            "# VIR INSTANTANE EMIS WEB POUR: MME N. P. DATE: 02/09/2026 10:15 REF: #",
        ),
        (
            "VIR RECU 9A DE: M OU MME TOM LAMBERT MOTIF: PRET",
            "VIR RECU 9A DE: M OU MME T. L. MOTIF: PRET",
        ),
        # Prélèvements : le créancier est une entreprise, gardé ; identifiants masqués
        (
            "PRLV SEPA FREE MOBILE ECH/150926 ID EMETTEUR/FR12ZZZ456789 MDT/ABC1234567",
            "PRLV SEPA FREE MOBILE ECH/# ID EMETTEUR/# MDT/ABC#",
        ),
        # Noms de la liste : abrégés partout, même hors virement, sans tenir compte des accents
        ("CARTE 05/09/26 CADEAU CHLOE DUBREUIL", "CARTE 05/09/26 CADEAU C. D."),
        ("REMBOURSEMENT BRUNO", "REMBOURSEMENT B."),
    ],
)
def test_labels(masker: Pseudonymizer, raw: str, expected: str) -> None:
    assert masker.label(raw) == expected


def test_identifiers_contacts_and_links_are_removed(masker: Pseudonymizer) -> None:
    raw = f"VIR SEPA {FAKE_IBAN} alice@example.org 06 12 34 56 78 https://exemple.fr/x"
    cleaned = masker.label(raw)
    assert "9999" not in cleaned and "@" not in cleaned and "06 12" not in cleaned
    assert "exemple" not in cleaned and "[lien]" in cleaned


def test_long_labels_are_truncated(masker: Pseudonymizer) -> None:
    assert len(masker.label("CARTE " + "SUPERMARCHE " * 20)) <= 80


def test_free_text_keeps_case_but_masks_listed_names(masker: Pseudonymizer) -> None:
    assert masker.text("Compte joint Alice") == "Compte joint A."
    assert masker.text("Studio Lefort (bail meublé)") == "Studio L. (bail meublé)"
    assert masker.text("Courses") == "Courses"


def test_guard_scrubs_names_and_rejects_identifiers(masker: Pseudonymizer) -> None:
    masker.check(
        {"lignes": [{"libelle": "VIR INST M A. D.", "id": "t_abcdefghijklmnopqrstuvwxyz"}]}
    )
    masker.check({"code": "c_2345672345672345672345672a", "montant_eur": "12 345,67 €"})
    for leak in ({"libelle": FAKE_IBAN}, {"libelle": "REF 123456"}, [{"x": ["000123"]}]):
        with pytest.raises(LeakError):
            masker.check(leak)
    scrubbed = masker.scrub(
        {"categorie": "Cadeaux › Martinez", "liste": ["pour Bruno", ("Jean-Lefort", 3)], "n": 4}
    )
    assert scrubbed == {"categorie": "Cadeaux › M.", "liste": ["pour B.", ["J.-L.", 3]], "n": 4}
    assert masker.scrub("t_abcdefghijklmnopqrstuvwxyz") == "t_abcdefghijklmnopqrstuvwxyz"
    assert Pseudonymizer(persons=(), emitters=()).scrub("Bruno") == "Bruno"
    assert masker.scrub({"categorie": "Épargne 100000", "compte": FAKE_IBAN}) == {
        "categorie": "Épargne #",
        "compte": "#",
    }


def test_looks_pseudonymized() -> None:
    assert looks_pseudonymized("VIR M A. D.")
    assert looks_pseudonymized("ECH/#")
    assert not looks_pseudonymized("UBER EATS")
    assert not looks_pseudonymized("SNCF 2")


_WORD = st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ", min_size=1, max_size=10)
_DIGITS = st.integers(min_value=10_000, max_value=10**12).map(str)
_TOKEN = st.one_of(
    _WORD,
    _DIGITS,
    st.sampled_from(["VIR", "INST", "M", "MME", "DE:", "POUR:", "CARTE", "PRLV", "CB*1234", ":"]),
    st.sampled_from([FAKE_IBAN, "ALICE MARTINEZ", "Lefort", "dubreuil", "a.b@example.com"]),
)


@given(st.lists(_TOKEN, max_size=20))
def test_no_identifier_or_listed_name_survives(tokens: list[str]) -> None:
    masker = Pseudonymizer(persons=PERSONS, emitters=())
    cleaned = masker.label(" ".join(tokens))
    assert not re.search(r"\d{5}", cleaned)
    assert "@" not in cleaned
    for word in ("ALICE", "MARTINEZ", "BRUNO", "LEFORT", "CHLOE", "DUBREUIL"):
        assert not re.search(rf"\b{word}\b", cleaned.upper())
    masker.check({"libelle": cleaned})
    assert masker.scrub(cleaned) == cleaned  # rien à rattraper après le nettoyage


def test_names_are_found_for_the_server_only(masker: Pseudonymizer) -> None:
    assert masker.names("VIR INST MME ZOE GARNIER LOYER SEPT") == ["ZOE GARNIER"]
    assert masker.names("VIR M PAUL ROUX OU MME LEA ROUX : R12AB") == ["PAUL ROUX", "LEA ROUX"]
    # Audit du 30/09 : le motif qui suit le nom ne fait pas partie du payeur.
    assert masker.names("VIR INST M HUGO BLANCHARD RESTO") == ["HUGO BLANCHARD"]
    assert masker.names("CARTE 12/09/26 CARREFOUR CITY CB*4521") == []


def test_payer_patterns_are_not_names(demo: Demo) -> None:
    """Audit du 30/09 : un motif de reconnaissance (« LOYER STUDIO ») ne rend pas « Studio »
    abrégeable ; seuls les noms des payeurs le sont."""
    from app.assistant.terms import load_pseudonymizer  # noqa: PLC0415
    from app.models.rental import PartyRole  # noqa: PLC0415
    from app.rental.leases import add_party  # noqa: PLC0415

    add_party(
        demo.engine,
        demo.lease_id,
        role=PartyRole.GUARANTOR,
        name="Zoé Garnier",
        patterns=["LOYER STUDIO"],
    )
    masker = load_pseudonymizer(demo.engine)
    assert masker.text("Studio de Zoé Garnier") == "Studio de Z. G."
