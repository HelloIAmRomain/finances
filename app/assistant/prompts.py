"""Invites proposées dans le client MCP (docs/spec-mcp.md §14) : des questions toutes prêtes."""

from dataclasses import dataclass
from typing import Any

from app.months import is_month


@dataclass(frozen=True)
class Prompt:
    name: str
    title: str
    description: str
    arguments: tuple[tuple[str, str, bool], ...]  # (nom, description, obligatoire)
    template: str

    def descriptor(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "arguments": [
                {"name": name, "description": text, "required": required}
                for name, text, required in self.arguments
            ],
        }


PROMPTS = {
    prompt.name: prompt
    for prompt in (
        Prompt(
            "bilan_du_mois",
            "Bilan du mois",
            "Revenus, dépenses, postes marquants et comparaison avec le mois précédent.",
            (("mois", "Mois au format AAAA-MM (défaut : mois dernier)", False),),
            "Fais le bilan de {mois} : revenus, dépenses et épargne, dépenses par catégorie "
            "comparées au mois précédent, principaux postes, et ce qui sort de l'ordinaire. "
            "Cite les transactions marquantes.",
        ),
        Prompt(
            "comparer_deux_mois",
            "Comparer deux mois",
            "Écarts par catégorie entre deux mois, expliqués par les opérations.",
            (("mois_a", "Premier mois (AAAA-MM)", True), ("mois_b", "Second mois (AAAA-MM)", True)),
            "Compare mes dépenses de {mois_a} et de {mois_b} par catégorie, explique les plus "
            "gros écarts par les opérations concernées.",
        ),
        Prompt(
            "chasser_les_abonnements",
            "Chasser les abonnements",
            "Abonnements et prélèvements récurrents, coût annuel, hausses de prix.",
            (),
            "Liste mes abonnements et prélèvements récurrents avec leur coût annuel, signale les "
            "hausses de prix et ceux qui semblent inutilisés ou en double.",
        ),
        Prompt(
            "ou_economiser",
            "Où économiser ?",
            "Pistes d'économies chiffrées à partir des 3 derniers mois.",
            (),
            "À partir de mes 3 derniers mois, propose des pistes d'économies concrètes et "
            "chiffrées, appuyées sur les opérations.",
        ),
        Prompt(
            "ranger_non_categorisees",
            "Ranger les opérations non catégorisées",
            "Propose un classement et des règles, appliqués seulement après accord.",
            (),
            "Cherche mes opérations non catégorisées des 3 derniers mois, propose pour chacune "
            "une catégorie et, quand c'est répétitif, une règle. Montre la proposition et "
            "n'applique rien avant mon accord explicite.",
        ),
        Prompt(
            "point_loyers",
            "Point loyers",
            "Échéances des baux, retards, paiements à rapprocher.",
            (),
            "Fais le point sur mes loyers et remboursements attendus : échéances des 3 derniers "
            "mois, retards, propositions de rapprochement et trop-perçus en attente.",
        ),
    )
}


def render_prompt(name: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
    prompt = PROMPTS.get(name)
    if prompt is None:
        return None
    values: dict[str, str] = {}
    for argument, _, required in prompt.arguments:
        value = arguments.get(argument)
        if value is None or value == "":
            if required:
                return None
            values[argument] = "le mois dernier"
        elif not isinstance(value, str) or not is_month(value):
            return None
        else:
            values[argument] = value
    return {
        "description": prompt.description,
        "messages": [
            {"role": "user", "content": {"type": "text", "text": prompt.template.format(**values)}}
        ],
    }
