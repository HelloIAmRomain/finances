"""Petit générateur de PDF texte (quittances), sans dépendance ni bibliothèque système.

- Polices standard du lecteur (Helvetica, Helvetica-Bold), encodage WinAnsi : accents français,
  « € », guillemets ; un caractère hors de cet encodage est remplacé par « ? ».
- Format A4, paragraphes coupés à la largeur de la page, lignes « libellé … montant » alignées à
  droite, filets ; nouvelle page quand la place manque.
- Sortie déterministe (aucune date de création) : le même contenu donne les mêmes octets.
"""

import unicodedata
from dataclasses import dataclass, field

PAGE_WIDTH, PAGE_HEIGHT = 595, 842  # A4 en points
MARGIN = 64
LINE_PERCENT = 135  # interligne : 135 % du corps
BOLD_PERCENT = 108  # Helvetica-Bold est un peu plus large : marge de sécurité
FIRST_PRINTABLE = 32
MILLI = 1000  # positions et largeurs en millièmes de point (entiers, pas de float : inv. 2)

# Chasses d'Helvetica (millièmes de corps) pour les caractères 32 à 126 (métriques Adobe).
_ASCII_WIDTHS = (
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
    1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
    667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
    333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
    556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584,
)  # fmt: skip
_OTHER_WIDTHS = {
    "€": 556, "\xa0": 278, "\u2019": 222, "\u2018": 222, "«": 556, "»": 556, "—": 1000,
    "\u2013": 556, "…": 1000, "°": 400, "œ": 944, "Œ": 1000, "æ": 889, "Æ": 1000, "•": 350,
}  # fmt: skip


def _char_width(char: str) -> int:
    code = ord(char)
    if FIRST_PRINTABLE <= code < FIRST_PRINTABLE + len(_ASCII_WIDTHS):
        return _ASCII_WIDTHS[code - FIRST_PRINTABLE]
    if char in _OTHER_WIDTHS:
        return _OTHER_WIDTHS[char]
    base = unicodedata.normalize("NFD", char)[0]  # « é » a la chasse de « e »
    if base != char:
        return _char_width(base)
    return 556


def text_width(text: str, size: int, *, bold: bool = False) -> int:
    """Largeur en millièmes de point."""
    width = sum(_char_width(char) for char in _normalize(text)) * size
    return width * BOLD_PERCENT // 100 if bold else width


def _coordinate(milli: int) -> bytes:
    return b"%d.%03d" % divmod(milli, MILLI)


def _normalize(text: str) -> str:
    # Espaces fines insécables (montants, UI-05) : absentes de WinAnsi, espace insécable à la place.
    return text.replace("\u202f", "\xa0").replace("\u2009", " ")


def _literal(text: str) -> bytes:
    raw = _normalize(text).encode("cp1252", errors="replace")
    return b"(" + raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)") + b")"


def wrap(text: str, size: int, width: int, *, bold: bool = False) -> list[str]:
    """Coupe un paragraphe aux espaces pour tenir dans `width` millièmes de point."""
    lines: list[str] = []
    current = ""
    for word in text.split(" "):
        candidate = f"{current} {word}" if current else word
        if current and text_width(candidate, size, bold=bold) > width:
            lines.append(current)
            current = word
        else:
            current = candidate
    lines.append(current)
    return lines


@dataclass
class Document:
    """Document A4 construit de haut en bas (positions en millièmes de point)."""

    pages: list[list[bytes]] = field(default_factory=lambda: [[]])
    y: int = (PAGE_HEIGHT - MARGIN) * MILLI

    @property
    def width(self) -> int:
        return (PAGE_WIDTH - 2 * MARGIN) * MILLI

    def _line(self, size: int) -> None:
        height = size * LINE_PERCENT * MILLI // 100
        if self.y - height < MARGIN * MILLI:
            self.pages.append([])
            self.y = (PAGE_HEIGHT - MARGIN) * MILLI
        self.y -= height

    def _show(self, text: str, x: int, size: int, *, bold: bool) -> None:
        font = b"/F2" if bold else b"/F1"
        self.pages[-1].append(
            b"BT %s %d Tf %s %s Td %s Tj ET"
            % (font, size, _coordinate(x), _coordinate(self.y), _literal(text))
        )

    def space(self, points: int) -> None:
        self.y -= points * MILLI

    def paragraph(self, text: str, *, size: int = 11, bold: bool = False) -> None:
        for line in wrap(text, size, self.width, bold=bold):
            self._line(size)
            self._show(line, MARGIN * MILLI, size, bold=bold)

    def row(self, label: str, value: str, *, size: int = 11, bold: bool = False) -> None:
        """Libellé à gauche, valeur alignée à droite (montants)."""
        self._line(size)
        self._show(label, MARGIN * MILLI, size, bold=bold)
        # Chiffres, espaces et « € » ont la même chasse en gras : largeur exacte sans majoration.
        right = (PAGE_WIDTH - MARGIN) * MILLI - text_width(value, size)
        self._show(value, right, size, bold=bold)

    def rule(self) -> None:
        self._line(6)
        y = _coordinate(self.y)
        self.pages[-1].append(b"%d %s m %d %s l 0.5 w S" % (MARGIN, y, PAGE_WIDTH - MARGIN, y))

    def render(self, *, title: str) -> bytes:
        objects: list[bytes] = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"",  # pages, rempli plus bas
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold"
            b" /Encoding /WinAnsiEncoding >>",
            b"<< /Title " + _literal(title) + b" /Producer (Finances) >>",
        ]
        kids: list[int] = []
        for operations in self.pages:
            stream = b"\n".join(operations)
            objects.append(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream))
            content = len(objects)
            objects.append(
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d]"
                b" /Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents %d 0 R >>"
                % (PAGE_WIDTH, PAGE_HEIGHT, content)
            )
            kids.append(len(objects))
        objects[1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (
            b" ".join(b"%d 0 R" % kid for kid in kids),
            len(kids),
        )
        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for number, body in enumerate(objects, start=1):
            offsets.append(len(out))
            out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
        xref = len(out)
        out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
        out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
        out += b"trailer\n<< /Size %d /Root 1 0 R /Info 5 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
            len(objects) + 1,
            xref,
        )
        return bytes(out)
