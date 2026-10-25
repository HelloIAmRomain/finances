"""Générateur de PDF texte (quittances) : structure valide, accents, coupure, pages."""

import re

from hypothesis import given
from hypothesis import strategies as st

from app.pdf import MILLI, Document, text_width, wrap


def _check_structure(pdf: bytes) -> None:
    assert pdf.startswith(b"%PDF-1.4\n")
    assert pdf.endswith(b"%%EOF\n")
    start = int(re.search(rb"startxref\n(\d+)\n", pdf).group(1))  # type: ignore[union-attr]
    assert pdf[start:].startswith(b"xref\n")
    offsets = [int(o) for o in re.findall(rb"(\d{10}) 00000 n ", pdf)]
    for number, offset in enumerate(offsets, start=1):
        assert pdf[offset:].startswith(b"%d 0 obj\n" % number)  # table de références exacte
    for length, body in re.findall(rb"<< /Length (\d+) >>\nstream\n(.*?)\nendstream", pdf, re.S):
        assert int(length) == len(body)


def test_document_is_well_formed_and_deterministic() -> None:
    doc = Document()
    doc.paragraph("Quittance de loyer", size=20, bold=True)
    doc.paragraph("Reçu : 1\u202f234,56\u00a0€ (loyer) — « payé »")
    doc.rule()
    doc.row("Total", "1\u202f234,56\u00a0€", bold=True)
    pdf = doc.render(title="Essai")
    _check_structure(pdf)
    assert "Reçu : 1\xa0234,56\xa0€ \\(loyer\\)".encode("cp1252") in pdf  # WinAnsi, échappé
    assert b"/Count 1" in pdf
    again = Document()
    again.paragraph("Quittance de loyer", size=20, bold=True)
    again.paragraph("Reçu : 1\u202f234,56\u00a0€ (loyer) — « payé »")
    again.rule()
    again.row("Total", "1\u202f234,56\u00a0€", bold=True)
    assert again.render(title="Essai") == pdf


def test_unknown_characters_are_replaced_not_fatal() -> None:
    doc = Document()
    doc.paragraph("Emoji 🏠 et idéogramme 家")
    pdf = doc.render(title="x")
    assert b"(Emoji ? et id\xe9ogramme ?)" in pdf
    assert text_width("家", 10) == 5560  # chasse par défaut


def test_long_text_wraps_and_spills_onto_new_pages() -> None:
    doc = Document()
    for _ in range(60):
        doc.paragraph("mot " * 60)
    pdf = doc.render(title="long")
    _check_structure(pdf)
    assert b"/Count " in pdf and b"/Count 1 " not in pdf
    assert int(re.search(rb"/Count (\d+)", pdf).group(1)) >= 2  # type: ignore[union-attr]


def test_width_of_accented_letters_and_bold() -> None:
    assert text_width("é", 10) == text_width("e", 10)
    assert text_width("Total", 10, bold=True) > text_width("Total", 10)


@given(st.lists(st.text(alphabet="abcdéèàç€ ", min_size=1, max_size=12), max_size=30))
def test_wrapped_lines_fit_or_are_single_words(words: list[str]) -> None:
    text = " ".join(words)
    width = 200 * MILLI
    lines = wrap(text, 11, width)
    assert " ".join(lines).split() == text.split()  # aucun mot perdu
    for line in lines:
        assert text_width(line, 11) <= width or " " not in line.strip()
