"""Montants en centimes entiers (inv. 2) : lecture, arrondi unique, répartition, affichage."""

from decimal import Decimal
from fractions import Fraction

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.money import (
    Cents,
    DecimalSeparator,
    InvalidAmountError,
    allocate,
    format_decimal_fr,
    format_eur,
    format_share_bp,
    parse_amount,
    parse_optional_user_amount,
    parse_user_amount,
    round_to_cents,
)

cents = st.integers(min_value=-(10**12), max_value=10**12)


@pytest.mark.parametrize(
    ("text", "decimal_sep", "expected"),
    [
        ("12.34", ".", 1234),
        ("-12.34", ".", -1234),
        ("+0.05", ".", 5),
        ("1234", ".", 123400),
        ("12.3", ".", 1230),
        ("1,234.56", ".", 123456),
        ("12,34", ",", 1234),
        ("-1 234,56", ",", -123456),
        ("1\u00a0234,56", ",", 123456),
        ("1\u202f234,56", ",", 123456),
        ("1.234,56", ",", 123456),
        ("  42,00  ", ",", 4200),
        ("0,10", ",", 10),
        ("12.340", ".", 1234),
    ],
)
def test_parse_amount(text: str, decimal_sep: DecimalSeparator, expected: int) -> None:
    assert parse_amount(text, decimal_sep=decimal_sep) == expected


@pytest.mark.parametrize(
    ("text", "decimal_sep"),
    [
        ("", "."),
        ("abc", "."),
        ("12.345", "."),  # fraction de centime : jamais d'arrondi silencieux
        ("12,34", "."),  # mauvais séparateur décimal
        ("1.234.56", "."),
        ("1 23,45", ","),  # groupe de milliers mal formé
        ("12.34 €", "."),
        ("--12", "."),
        ("1e3", "."),
        ("NaN", "."),
        ("12.", "."),
    ],
)
def test_parse_amount_rejects(text: str, decimal_sep: DecimalSeparator) -> None:
    with pytest.raises(InvalidAmountError):
        parse_amount(text, decimal_sep=decimal_sep)


@pytest.mark.parametrize("value", [12.34, 1234, Decimal("12.34")])
def test_parse_amount_only_accepts_text(value: object) -> None:
    with pytest.raises(TypeError):
        parse_amount(value, decimal_sep=".")  # type: ignore[arg-type]


@given(cents)
def test_parse_format_roundtrip(value: int) -> None:
    text = format_eur(Cents(value)).removesuffix("\u00a0€")
    assert parse_amount(text, decimal_sep=",") == value


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, "0,00\u00a0€"),
        (5, "0,05\u00a0€"),
        (-5, "-0,05\u00a0€"),
        (123456, "1\u202f234,56\u00a0€"),
        (-100000000, "-1\u202f000\u202f000,00\u00a0€"),
    ],
)
def test_format_eur(value: int, expected: str) -> None:
    assert format_eur(Cents(value)) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Fraction(1, 2), 1),  # demi-centime : au-dessus (ROUND_HALF_UP)
        (Fraction(-1, 2), -1),  # symétrique pour les négatifs
        (Fraction(149, 100), 1),
        (Decimal("2.5"), 3),
        (Decimal("-2.5"), -3),
        (Decimal("2.4999"), 2),
        (7, 7),
    ],
)
def test_round_to_cents(value: Fraction | Decimal | int, expected: int) -> None:
    assert round_to_cents(value) == expected


def test_round_to_cents_rejects_float() -> None:
    with pytest.raises(TypeError):
        round_to_cents(0.5)  # type: ignore[arg-type]


@given(
    total=cents,
    weights=st.lists(st.integers(min_value=0, max_value=10_000), min_size=1, max_size=12).filter(
        any
    ),
)
def test_allocate_preserves_total(total: int, weights: list[int]) -> None:
    parts = allocate(Cents(total), weights)
    assert sum(parts) == total  # aucune somme ne se crée ni ne se perd
    assert len(parts) == len(weights)
    for part, weight in zip(parts, weights, strict=True):
        exact = Fraction(total * weight, sum(weights))
        assert abs(part - exact) < 1  # chaque part est à moins d'un centime de sa valeur exacte
        if weight == 0:
            assert part == 0


def test_allocate_half_half() -> None:
    # Quote-parts 50/50 en dix-millièmes (DT §8) sur un montant impair.
    assert allocate(Cents(10001), [5000, 5000]) == [5001, 5000]


def test_allocate_is_deterministic() -> None:
    assert allocate(Cents(100), [1, 1, 1]) == [34, 33, 33]
    assert allocate(Cents(-100), [1, 1, 1]) == [-34, -33, -33]


def test_allocate_rejects_bad_weights() -> None:
    with pytest.raises(ValueError, match="poids"):
        allocate(Cents(100), [])
    with pytest.raises(ValueError, match="poids"):
        allocate(Cents(100), [0, 0])
    with pytest.raises(ValueError, match="poids"):
        allocate(Cents(100), [1, -1])


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1 234,56", 123_456),
        ("1\u202f234,56\u00a0€", 123_456),  # copié depuis l'affichage (format_eur)
        ("1234.5", 123_450),
        ("-12 €", -1_200),
        ("1.234,56", 123_456),
        ("  ", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_optional_user_amount(text: str | None, expected: int | None) -> None:
    assert parse_optional_user_amount(text) == expected


@pytest.mark.parametrize("text", ["beaucoup", "12,345", "1,234.56", "1,2,3", "--1"])
def test_user_amount_explains_the_expected_format(text: str) -> None:
    with pytest.raises(InvalidAmountError, match=r"Montant illisible .* 1 234,56\.") as error:
        parse_optional_user_amount(f" {text} ")
    assert f"({text})" in str(error.value)  # la saisie est rappelée, sans les espaces autour


def test_user_amount_required() -> None:
    assert parse_user_amount("0") == 0
    with pytest.raises(InvalidAmountError, match="Montant obligatoire"):
        parse_user_amount(" € ")


@given(cents)
def test_user_amount_reads_the_display(value: int) -> None:
    assert parse_user_amount(format_eur(Cents(value))) == value
    assert parse_user_amount(format_decimal_fr(Cents(value))) == value


@pytest.mark.parametrize(
    ("value", "expected"), [(0, "0,00"), (-5, "-0,05"), (123_456_789, "1234567,89")]
)
def test_format_decimal_fr(value: int, expected: str) -> None:
    assert format_decimal_fr(Cents(value)) == expected


@pytest.mark.parametrize(
    ("share", "shown"), [(5000, "50"), (3333, "33,33"), (10_000, "100"), (5, "0,05")]
)
def test_format_share_bp(share: int, shown: str) -> None:
    assert format_share_bp(share) == shown
