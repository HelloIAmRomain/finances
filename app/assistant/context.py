"""Vue d'ensemble donnée à l'IA en début d'échange (docs/spec-mcp.md) : qui sont les comptes, à quoi
ils servent, ce qui compte dans les totaux, la fraîcheur des données, les baux, les catégories.

Construite à partir des outils de lecture (donc pseudonymisée et bornée comme eux), en texte
court : elle part dans les consignes de l'assistant intégré et sert l'outil MCP `vue_ensemble`.
"""

from typing import Any

from app.assistant.access import WRITES
from app.assistant.tools import Context, Params, call_tool, tool
from app.months import month_of, shift_month

MAX_LEASES = 10


def _eur(amount: dict[str, Any] | None) -> str:
    return str(amount["eur"]) if amount else "inconnu"


def overview(context: Context) -> str:
    """Résumé en Markdown, à jour à l'instant de l'appel."""
    lines = [f"Aujourd'hui : {context.today.isoformat()} (heure de Paris)."]

    accounts = call_tool(context, "comptes", {})["comptes"]
    lines.append("\n## Comptes")
    for account in accounts:
        role = f" — {account['role']}" if account.get("role") else ""
        freshness = account["derniere_synchro"][:10] if account["derniere_synchro"] else "jamais"
        lines.append(
            f"- **{account['nom']}** ({account['banque'] or 'import'}, {account['type']}, "
            f"{account['statut']}){role} : solde {_eur(account['solde'])}"
            f" au {account['solde_au'] or '?'} ; synchro {freshness} ; historique depuis "
            f"{account['historique_depuis'] or '?'}"
        )

    current = month_of(context.today)
    summary = call_tool(
        context, "synthese_mensuelle", {"du": shift_month(current, -3), "au": current}
    )
    lines.append("\n## Flux des comptes comptés")
    for month in summary["mois"]:
        partial = " (mois en cours, partiel)" if month["mois"] == current else ""
        lines.append(
            f"- {month['mois']}{partial} : revenus {month['revenus']['eur']}, "
            f"dépenses {month['depenses']['eur']}"
        )

    rents = call_tool(context, "loyers", {"du": current, "au": current})
    if rents["baux"]:
        lines.append("\n## Baux et remboursements attendus")
        for lease in rents["baux"][:MAX_LEASES]:
            due = lease["echeances"][-1] if lease["echeances"] else None
            state = f" ; {due['periode']} : {due['statut']}" if due else ""
            lines.append(
                f"- {lease['nom']} ({lease['type']}, compte {lease['compte']}) : "
                f"{lease['mensuel']['eur']} par mois{state}"
            )
    pending = len(rents["propositions_en_attente"]) + len(rents["trop_percus_en_attente"])

    roots = sorted(
        {c["categorie"].split(" › ")[0] for c in call_tool(context, "categories", {})["categories"]}
    )
    to_review = len(call_tool(context, "a_valider", {})["elements"])
    lines.append("\n## Divers")
    lines.append(f"- Catégories racines : {', '.join(roots)}.")
    lines.append(
        f"- En attente de décision : {to_review} élément(s) « À valider », "
        f"{pending} proposition(s) de loyer ou trop-perçu(s)."
    )
    lines.append(
        "- Modifications par l'IA : "
        + ("autorisées (avec confirmation des écritures de masse)." if context.switches.get(WRITES)
           else "désactivées : lecture seule.")
    )  # fmt: skip
    return "\n".join(lines)


class NoParams(Params):
    pass


@tool(
    "vue_ensemble",
    "Vue d'ensemble",
    "À appeler en premier : comptes (rôle, statut, solde, fraîcheur des données), flux des "
    "derniers mois, baux, catégories, éléments en attente. Donne le contexte nécessaire pour "
    "bien interpréter les autres outils.",
)
def vue_ensemble(context: Context, params: NoParams) -> dict[str, Any]:
    return {"resume": overview(context)}
