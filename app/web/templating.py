"""Gabarits Jinja2 et filtres d'affichage à la française (UI-05)."""

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt
from markupsafe import Markup

from app.money import Cents, format_decimal_fr, format_eur, format_share_bp
from app.months import month_label
from app.timeutil import format_date_fr, to_paris
from app.web.flash import flash

TEMPLATES_DIR = Path(__file__).parent / "templates"

# Navigation regroupée par besoin (docs/charte-ergonomie.md §3) : une rubrique ouvre sa
# première page ; les pages de la rubrique en cours s'affichent sur une seconde ligne.
NAVIGATION: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("Tableau de bord", (("/", "Tableau de bord"),)),
    (
        "Mon argent",
        (
            ("/comptes", "Comptes"),
            ("/transactions", "Opérations"),
            ("/budgets", "Budgets"),
            ("/abonnements", "Abonnements"),
            ("/prevision", "Prévision"),
            ("/patrimoine", "Patrimoine"),
        ),
    ),
    ("Location et impôts", (("/loyers", "Loyers"), ("/echeances", "Échéances"))),
    ("À traiter", (("/a-valider", "À valider"), ("/alertes", "Alertes"))),
    ("Assistant", (("/assistant", "Assistant"),)),
    (
        "Réglages",
        (
            ("/connexions", "Banques"),
            ("/import", "Importer un relevé"),
            ("/regles", "Règles de catégorie"),
            ("/alertes#telephone", "Notifications"),  # réglages de la page Alertes
            ("/securite", "Sécurité"),
            ("/systeme", "État du système"),
        ),
    ),
)
# Pages sans entrée propre, rattachées à une rubrique (fiche d'un bien, journal de l'IA…).
SECTION_ALIASES = {
    "/biens": "/loyers",
    "/ia": "/securite",
    "/appareils": "/securite",
    "/journal": "/systeme",
}


def current_section(path: str) -> int | None:
    """Rubrique de la page affichée (rang dans NAVIGATION), par préfixe de chemin."""
    for alias, target in SECTION_ALIASES.items():
        if path == alias or path.startswith(alias + "/"):
            path = target
    best: tuple[int, int] | None = None  # (longueur du préfixe, rang)
    for rank, (_, pages) in enumerate(NAVIGATION):
        for href, _label in pages:
            matches = path == href if href == "/" else path == href or path.startswith(href + "/")
            if matches and (best is None or len(href) > best[0]):
                best = (len(href), rank)
    return best[1] if best else None


CONNECTION_LABELS = {
    "pending": "liaison en cours",
    "active": "active",
    "expired": "accès expiré : à renouveler",
    "error": "en erreur",
    "revoked": "déliée",
}


def eur(amount: int) -> str:
    return format_eur(Cents(amount))


def date_fr(value: date | datetime | None) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return format_date_fr(to_paris(value).date())
    return format_date_fr(value)


def datetime_fr(value: datetime | None) -> str:
    """Instant stocké en UTC, affiché à l'heure de Paris (inv. 9)."""
    if value is None:
        return ""
    return to_paris(value).strftime("%d/%m/%Y à %H:%M")


def sentence(text: str | None) -> str:
    """Majuscule initiale, le reste intact (sigles, noms propres : « T2 2026 », « IRL ») ; le
    filtre `capitalize` de Jinja mettait tout le reste en minuscules."""
    text = text or ""
    return text[:1].upper() + text[1:]


def amount_class(amount: int) -> str:
    return "negatif" if amount < 0 else "positif" if amount > 0 else ""


_MARKDOWN = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False})
_MARKDOWN.enable(["table", "strikethrough"])
# Aucun lien ni image cliquable : une instruction cachée dans un libellé bancaire pourrait
# faire écrire à l'IA un lien qui emporte des données au clic. L'adresse reste lisible en texte.
_MARKDOWN.disable(["link", "image", "autolink"])


def markdown(text: str | None) -> Markup:
    """Réponse de l'assistant en HTML : HTML brut désactivé (échappé), liens et images laissés
    en texte ; tableaux et listes rendus."""
    return Markup(_MARKDOWN.render(text or ""))  # noqa: S704 (sortie de markdown-it, HTML échappé)


_JSON_IN_HTML = str.maketrans({"<": "\\u003c", ">": "\\u003e", "&": "\\u0026"})


def json_for_script(value: Any) -> Markup:
    """JSON à placer dans `<script type="application/json">` : `<`, `>` et `&` sont échappés
    en `\\u003c`… pour qu'aucune donnée (nom de catégorie…) ne puisse fermer la balise."""
    text = json.dumps(value, ensure_ascii=False).translate(_JSON_IN_HTML)
    return Markup(text)  # noqa: S704 (caractères de balisage échappés ci-dessus)


def make_templates() -> Jinja2Templates:
    templates = Jinja2Templates(directory=TEMPLATES_DIR)
    templates.env.filters.update(
        eur=eur,
        date_fr=date_fr,
        datetime_fr=datetime_fr,
        amount_class=amount_class,
        sentence=sentence,
        markdown=markdown,
        json_for_script=json_for_script,
        saisie=format_decimal_fr,  # montant prérempli dans un champ : « 850,00 », jamais un float
        pourcent=format_share_bp,
        mois_fr=month_label,  # « 2026-10 » → « octobre 2026 »
    )
    templates.env.globals.update(
        connection_labels=CONNECTION_LABELS,
        nav=NAVIGATION,
        current_section=current_section,
        flash=flash,
    )
    return templates
