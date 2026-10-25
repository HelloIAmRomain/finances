import pytest

from app.ledger.labels import clean_label


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  cb   Boulangerie  ", "CB BOULANGERIE"),
        ("CB CARREFOUR 12/09 CARTE X1234", "CB CARREFOUR"),
        ("PRLV SEPA EDF CLIENTS PARTICULIERS", "PRLV SEPA EDF CLIENTS PARTICULIERS"),
        ("VIR SEPA LOYER SEPTEMBRE 2026", "VIR SEPA LOYER SEPTEMBRE 2026"),
        ("CB SNCF 03/10/26", "CB SNCF"),
        ("Café\tdu  Port", "CAFÉ DU PORT"),
        ("", ""),
    ],
)
def test_clean_label(raw: str, expected: str) -> None:
    assert clean_label(raw) == expected


def test_clean_label_is_idempotent() -> None:
    once = clean_label("CB CARREFOUR 12/09 CARTE X1234")
    assert clean_label(once) == once
