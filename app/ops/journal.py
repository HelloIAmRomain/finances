"""Journal d'audit lisible (optimisation « journal consultable », NF-SEC-06) : chaque ligne du
journal (ajout seul) traduite en une phrase, son domaine et son auteur.

Les détails techniques (identifiants, paramètres) ne sont pas affichés : le journal dit ce qui a
été fait, quand et par qui. Les modifications faites par l'IA s'annulent depuis la page
« Modifications par l'IA ».
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Engine, or_, select

from app.models.ops import AuditLog

PAGE_SIZE = 50

DOMAINS = {
    "comptes": ("Comptes et banques", ("account.", "connection.", "duplicate.", "transfer.")),
    "categories": (
        "Catégories et budgets",
        ("categorization.", "category.", "review.", "rule.", "budget."),
    ),
    "locations": (
        "Locations",
        ("lease.", "rent.", "deposit.", "receipt.", "property.", "irl.", "party."),
    ),
    "echeances": ("Échéances", ("deadline.",)),
    "patrimoine": ("Patrimoine", ("asset.",)),
    "ia": ("Intelligence artificielle", ("ai.", "mcp.", "assistant.")),
    "securite": ("Connexion et sécurité", ("login.", "user.", "device.", "security_key.")),
    "autres": ("Alertes et exports", ("alerts.", "export.")),
}

LABELS = {
    "account.created": "Compte ajouté",
    "account.described": "Rôle d'un compte décrit",
    "account.low_balance": "Seuil de solde bas modifié",
    "account.renamed": "Compte renommé",
    "account.settings": "Réglages d'un compte modifiés",
    "connection.linked": "Banque reliée",
    "connection.revoked": "Accès à une banque retiré",
    "duplicate.auto_confirmed": "Doublon reconnu automatiquement",
    "duplicate.confirmed": "Doublon confirmé",
    "duplicate.rejected": "Doublon écarté",
    "duplicate.reverted": "Doublon annulé",
    "transfer.auto_confirmed": "Virement entre vos comptes reconnu automatiquement",
    "transfer.confirmed": "Virement entre vos comptes confirmé",
    "transfer.rejected": "Virement entre vos comptes écarté",
    "categorization.manual": "Opération catégorisée",
    "categorization.cleared": "Catégorie d'une opération retirée",
    "category.created": "Catégorie créée",
    "category.moved": "Catégorie déplacée",
    "category.renamed": "Catégorie renommée",
    "review.resolved": "Opération à valider traitée",
    "review.dismissed": "Opération à valider écartée",
    "rule.created": "Règle de catégorie créée",
    "rule.updated": "Règle de catégorie modifiée",
    "rule.deactivated": "Règle de catégorie désactivée",
    "budget.created": "Budget créé",
    "budget.updated": "Budget modifié",
    "budget.deleted": "Budget supprimé",
    "lease.created": "Bail ou remboursement créé",
    "lease.updated": "Bail ou remboursement modifié",
    "lease.ended": "Fin de bail enregistrée",
    "lease.exit": "Remise des clés enregistrée",
    "lease.irl_reference": "Indice de référence du bail modifié",
    "lease.irl_revision": "Révision du loyer appliquée",
    "irl.index": "Indice IRL saisi",
    "rent.allocated": "Paiement affecté à une échéance",
    "rent.dissociated": "Paiement dissocié d'une échéance",
    "rent.overpayment": "Trop-perçu traité",
    "rent.proposal_confirmed": "Paiement proposé confirmé",
    "rent.proposal_rejected": "Paiement proposé écarté",
    "deposit.received": "Dépôt de garantie encaissé",
    "deposit.withheld": "Retenue sur le dépôt de garantie",
    "deposit.returned": "Dépôt de garantie restitué",
    "deposit.deleted": "Mouvement du dépôt de garantie retiré",
    "receipt.issued": "Quittance établie",
    "property.created": "Bien créé",
    "property.expense_linked": "Charge rattachée à un bien",
    "property.expense_unlinked": "Charge retirée d'un bien",
    "property.expense_rule": "Règle de charge créée",
    "property.receipt_details": "Informations des quittances modifiées",
    "deadline.created": "Échéance ajoutée",
    "deadline.updated": "Échéance modifiée",
    "deadline.deleted": "Échéance supprimée",
    "deadline.done": "Échéance marquée réglée",
    "deadline.reopened": "Règlement d'échéance annulé",
    "asset.created": "Élément de patrimoine ajouté",
    "asset.valued": "Valeur d'un élément de patrimoine saisie",
    "asset.archived": "Élément de patrimoine archivé",
    "ai.change_set": "Modification faite par l'IA",
    "ai.change_undone": "Modification de l'IA annulée",
    "ai.switch": "Accès de l'IA activé ou coupé",
    "ai.token_created": "Accès IA créé",
    "ai.token_revoked": "Accès IA retiré",
    "ai.tokens_revoked_all": "Tous les accès IA retirés",
    "ai.term_added": "Nom à abréger pour l'IA ajouté",
    "ai.term_removed": "Nom à abréger pour l'IA retiré",
    "ai.assistant_settings": "Réglages de l'assistant modifiés",
    "ai.categorization_settings": "Catégorisation par l'IA réglée",
    "mcp.call": "Lecture de vos données par l'IA",
    "assistant.message": "Question posée à l'assistant",
    "assistant.renamed": "Conversation renommée",
    "assistant.deleted": "Conversation supprimée",
    "assistant.pinned": "Conversation épinglée",
    "assistant.unpinned": "Conversation désépinglée",
    "login.success": "Connexion réussie",
    "login.failed": "Connexion refusée",
    "login.locked": "Connexion bloquée après plusieurs échecs",
    "user.created": "Administrateur créé",
    "device.paired": "Téléphone appairé",
    "device.pairing_code": "Code d'appairage créé",
    "device.pairing_failed": "Appairage refusé",
    "device.revoked": "Téléphone retiré",
    "device.all_revoked": "Tous les téléphones retirés",
    "user.sessions_closed": "Toutes les sessions fermées",
    "security_key.registered": "Clé de sécurité ajoutée",
    "security_key.removed": "Clé de sécurité retirée",
    "alerts.quiet_hours": "Heures silencieuses modifiées",
    "alerts.digest": "Bilan hebdomadaire activé ou coupé",
    "export.transactions_csv": "Opérations exportées",
    "export.property_summary_csv": "Récapitulatif d'un bien exporté",
}


def domain_of(action: str) -> str:
    for key, (_, prefixes) in DOMAINS.items():
        if action.startswith(prefixes):
            return key
    return "autres"


def label_of(action: str) -> str:
    if action in LABELS:
        return LABELS[action]
    return "Autre modification"


def author_of(action: str, actor_user_id: int | None) -> str:
    if action.startswith(("ai.change_set", "mcp.")):
        return "l'IA"
    return "vous" if actor_user_id is not None else "automatique"


@dataclass(frozen=True)
class Entry:
    id: int
    at: datetime
    label: str
    domain: str
    author: str


def entries(
    engine: Engine, *, domain: str | None = None, before_id: int | None = None
) -> list[Entry]:
    """Les plus récentes d'abord, par pages ; `domain` : clé de DOMAINS ou None pour tout."""
    query = select(AuditLog.id, AuditLog.at, AuditLog.action, AuditLog.actor_user_id)
    if domain in DOMAINS:
        query = query.where(
            or_(*(AuditLog.action.startswith(prefix) for prefix in DOMAINS[domain][1]))
        )
    if before_id is not None:
        query = query.where(AuditLog.id < before_id)
    with engine.connect() as connection:
        rows = connection.execute(query.order_by(AuditLog.id.desc()).limit(PAGE_SIZE)).all()
    return [
        Entry(
            id=row.id,
            at=row.at,
            label=label_of(row.action),
            domain=DOMAINS[domain_of(row.action)][0],
            author=author_of(row.action, row.actor_user_id),
        )
        for row in rows
    ]
