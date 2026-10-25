"""Outils d'écriture (docs/spec-mcp.md §8) : chaque outil appelle les services existants.

Garde-fous communs (§4) : jeton à portée écriture et interrupteur « Modifications par l'IA »
(vérifiés par `call_tool`) ; lot de modifications annulable ; audit du service avec l'acteur ;
au-delà de `MASS_THRESHOLD` opérations touchées, aperçu puis code de confirmation. Jamais
d'argent, d'accès, de réglage de compte ni d'envoi au locataire (§2).
"""

from collections import defaultdict
from collections.abc import Callable
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Engine, func, select
from sqlalchemy.exc import IntegrityError

from app.assistant.changes import (
    MASS_THRESHOLD,
    TABLE_NAMES,
    ChangeError,
    Outcome,
    Snapshot,
    consume_confirmation,
    issue_confirmation,
    save,
    simulate,
    tracked,
    undo,
)
from app.assistant.pseudonym import looks_pseudonymized
from app.assistant.read import transaction_rows
from app.assistant.tools import (
    MAX_ROWS,
    Categories,
    Context,
    Params,
    ToolError,
    accounts,
    private_id,
    public_id,
    resolve_account,
    tool,
)
from app.categorize.categories import create_category, move_category, rename_category
from app.categorize.review import keep_current_transfer, resolve_category_conflict
from app.categorize.rules import RuleConditions
from app.categorize.service import (
    categorize_manually,
    clear_manual,
    create_rule,
    deactivate_rule,
    suggest_rule,
    update_rule,
)
from app.ledger.duplicates import confirm_link, reject_link
from app.ledger.transfers import decide_transfer
from app.models import LedgerTransaction, ReviewItem, Rule
from app.models.categorize import CategoryKind, ReviewKind, ReviewStatus
from app.models.rental import ExpenseKind, LeaseKind, OverpaymentDecision, PartyRole
from app.rental.allocations import (
    allocate_manually,
    confirm_proposal,
    decide_overpayment,
    dissociate,
    reject_proposal,
)
from app.rental.engine import run_rental
from app.rental.expenses import link, unlink
from app.rental.leases import (
    LeaseChanges,
    LeaseTerms,
    NewParty,
    add_party,
    create_lease_with_parties,
    end_lease,
    normalize_label,
    update_lease,
)

SHOWN_CHANGES = 50


class Confirmable(Params):
    confirmation: str | None = Field(
        None,
        description="Code rendu par l'aperçu d'une écriture de masse ; à ne fournir qu'après "
        "m'avoir montré l'aperçu et obtenu mon accord.",
    )


# --- Exécution, aperçu, description -----------------------------------------------------------


def _active(snapshot: Snapshot) -> dict[int, list[int]]:
    lines: dict[int, list[int]] = defaultdict(list)
    for row in sorted(snapshot["categorization"].values(), key=lambda r: r["id"]):
        if row["superseded_at"] is None:
            lines[row["transaction_id"]].append(row["category_id"])
    return lines


def _paths(snapshot: Snapshot) -> dict[int, str]:
    rows = snapshot["category"]
    return {
        cid: f"{rows[r['parent_id']]['name']} › {r['name']}" if r["parent_id"] else r["name"]
        for cid, r in rows.items()
    }


def _reclassified(outcome: Outcome[Any]) -> list[int]:
    before, after = _active(outcome.before), _active(outcome.after)
    touched = {
        row["transaction_id"]
        for change in outcome.changes
        if change.table == "categorization"
        for row in (change.before, change.after)
        if row is not None
    }
    return sorted(tx for tx in touched if before.get(tx, []) != after.get(tx, []))


def affected(outcome: Outcome[Any]) -> int:
    rent = {c.id for c in outcome.changes if c.table in ("rent_allocation", "property_expense")}
    return len(_reclassified(outcome)) + len(rent)


def describe(context: Context, outcome: Outcome[Any]) -> dict[str, Any]:
    counts: dict[str, dict[str, int]] = defaultdict(
        lambda: {"ajoutees": 0, "modifiees": 0, "retirees": 0}
    )
    for change in outcome.changes:
        kind = (
            "ajoutees"
            if change.before is None
            else "retirees"
            if change.after is None
            else "modifiees"
        )
        counts[TABLE_NAMES[change.table]][kind] += 1
    reclassified = _reclassified(outcome)
    before, after = _active(outcome.before), _active(outcome.after)
    old_paths, new_paths = _paths(outcome.before), _paths(outcome.after)
    shown = reclassified[:SHOWN_CHANGES]
    rows = {r["id"]: r for r in transaction_rows(context, shown)} if shown else {}
    return {
        "lignes": {table: {k: v for k, v in c.items() if v} for table, c in counts.items()},
        "operations_reclassees": len(reclassified),
        "detail": [
            {
                "transaction": public_id("transaction", tx),
                "date": rows[public_id("transaction", tx)]["date"],
                "libelle": rows[public_id("transaction", tx)]["libelle"],
                "montant": rows[public_id("transaction", tx)]["montant"],
                "avant": [old_paths.get(c) for c in before.get(tx, [])] or ["non catégorisée"],
                "apres": [new_paths.get(c) for c in after.get(tx, [])] or ["non catégorisée"],
            }
            for tx in shown
        ],
    }


def _guarded[T](operation: Callable[[Engine], T]) -> Callable[[Engine], T]:
    def run(engine: Engine) -> T:
        try:
            return operation(engine)
        except (ValueError, ChangeError) as exc:  # erreurs métier des services : expliquées
            raise ToolError(str(exc)) from exc
        except IntegrityError:  # référence inconnue (bien, échéance…) : jamais le détail SQL
            raise ToolError("Opération impossible : un objet désigné n'existe pas.") from None

    return run


def apply(
    context: Context,
    params: BaseModel,
    *,
    tool_name: str,
    summary: str,
    operation: Callable[[Engine], Any],
    preview: bool = False,
) -> dict[str, Any]:
    """Applique l'opération et enregistre le lot ; aperçu d'abord si elle peut être massive."""
    arguments = params.model_dump(mode="json")
    code = arguments.get("confirmation")
    guarded = _guarded(operation)
    if code:
        try:
            consume_confirmation(
                context.engine,
                token_id=context.token.id,
                tool=tool_name,
                params=arguments,
                code=code,
                now=context.now,
            )
        except ChangeError as exc:
            raise ToolError(str(exc)) from exc
    elif preview:
        rehearsal = simulate(context.engine, guarded)
        if affected(rehearsal) > MASS_THRESHOLD:
            return {
                "applique": False,
                "apercu": describe(context, rehearsal),
                "confirmation": issue_confirmation(
                    context.engine,
                    token_id=context.token.id,
                    tool=tool_name,
                    params=arguments,
                    now=context.now,
                ),
                "a_faire": "Montrer cet aperçu et demander mon accord ; si j'accepte, rappeler le "
                "même outil avec les mêmes paramètres et « confirmation » (valable 10 minutes).",
            }
    outcome = tracked(context.engine, guarded)
    change_id = save(
        context.engine,
        outcome.changes,
        token_id=context.token.id,
        actor_user_id=context.token.user_id,
        tool=tool_name,
        summary=summary,
        now=context.now,
    )
    return {
        "applique": True,
        "modification": public_id("modification", change_id) if change_id else None,
        "resume": context.masker.text(summary),
        "effets": describe(context, outcome),
        "annulable": change_id is not None,
    }


def _visible_tx(context: Context, ids: list[str]) -> list[int]:
    wanted = [private_id("transaction", text) for text in ids]
    known = accounts(context)
    with context.engine.connect() as connection:
        found = dict(
            connection.execute(
                select(LedgerTransaction.id, LedgerTransaction.account_id).where(
                    LedgerTransaction.id.in_(wanted)
                )
            ).all()
        )
    if any(found.get(tx) not in known for tx in wanted):
        raise ToolError("Transaction introuvable.")
    return wanted


def _amounts(context: Context, tx_ids: list[int]) -> dict[int, int]:
    with context.engine.connect() as connection:
        return dict(
            connection.execute(
                select(LedgerTransaction.id, LedgerTransaction.amount_cents).where(
                    LedgerTransaction.id.in_(tx_ids)
                )
            ).all()
        )


def _label_clean(context: Context, tx_id: int) -> tuple[str, str, int]:
    with context.engine.connect() as connection:
        row = connection.execute(
            select(
                LedgerTransaction.label_raw,
                LedgerTransaction.label_clean,
                LedgerTransaction.amount_cents,
            ).where(LedgerTransaction.id == tx_id)
        ).one()
    return row.label_raw, row.label_clean, row.amount_cents


def _check_fragments(fragments: list[str] | None, regex: str | None) -> None:
    for text in [*(fragments or []), *([regex] if regex else [])]:
        if looks_pseudonymized(text):
            raise ToolError(
                "Motif inutilisable : il contient des initiales ou « # » (données masquées). "
                "Utiliser « a_partir_de_transaction » : le serveur tirera le motif du vrai libellé."
            )


def _then_rental[T](operation: Callable[[Engine], T], context: Context) -> Callable[[Engine], T]:
    def run(engine: Engine) -> T:
        result = operation(engine)
        run_rental(engine, now=context.now)
        return result

    return run


# --- Catégorisation ---------------------------------------------------------------------------


class Split(BaseModel):
    model_config = ConfigDict(extra="forbid")
    categorie: str
    montant: int = Field(description="En centimes, signé comme la transaction.")


class Categorize(Confirmable):
    transactions: list[str] = Field(
        min_length=1, max_length=MAX_ROWS, description="Identifiants « t_… »."
    )
    categorie: str | None = Field(
        None, description="Catégorie pour toutes (nom ou « Parent › Enfant »)."
    )
    ventilation: list[Split] | None = Field(
        None, description="Ventilation d'une seule transaction ; la somme doit égaler son montant."
    )


@tool(
    "categoriser",
    "Catégoriser",
    "Classe une ou plusieurs transactions dans une catégorie, ou ventile une transaction sur "
    "plusieurs catégories. Classement manuel : prioritaire sur les règles. Au-delà de 20 "
    "opérations, rend d'abord un aperçu et un code de confirmation.",
    write=True,
)
def categoriser(context: Context, params: Categorize) -> dict[str, Any]:
    tree = Categories.load(context.engine)
    ids = _visible_tx(context, params.transactions)
    if (params.categorie is None) == (params.ventilation is None):
        raise ToolError("Donner soit « categorie », soit « ventilation ».")
    if params.ventilation is not None:
        if len(ids) != 1:
            raise ToolError("Une ventilation porte sur une seule transaction.")
        lines = {ids[0]: [(tree.resolve(s.categorie), s.montant) for s in params.ventilation]}
        summary = "Ventilation d'une opération"
    else:
        category = tree.resolve(params.categorie or "")
        amounts = _amounts(context, ids)
        lines = {tx: [(category, amounts[tx])] for tx in ids}
        summary = f"{len(ids)} opération(s) classée(s) en « {tree.paths[category]} »"

    def operation(engine: Engine) -> None:
        for tx, tx_lines in lines.items():
            categorize_manually(
                engine, tx, tx_lines, actor_user_id=context.token.user_id, now=context.now
            )

    return apply(
        context,
        params,
        tool_name="categoriser",
        summary=summary,
        operation=operation,
        preview=len(ids) > MASS_THRESHOLD,
    )


class Uncategorize(Params):
    transactions: list[str] = Field(min_length=1, max_length=MAX_ROWS)


@tool(
    "decategoriser",
    "Retirer un classement manuel",
    "Retire le classement manuel de transactions : les règles s'appliquent de nouveau.",
    write=True,
)
def decategoriser(context: Context, params: Uncategorize) -> dict[str, Any]:
    ids = _visible_tx(context, params.transactions)

    def operation(engine: Engine) -> int:
        return sum(
            clear_manual(engine, tx, actor_user_id=context.token.user_id, now=context.now)
            for tx in ids
        )

    return apply(
        context,
        params,
        tool_name="decategoriser",
        summary=f"Classement manuel retiré de {len(ids)} opération(s)",
        operation=operation,
    )


KIND_NAMES = {
    "depense": CategoryKind.EXPENSE,
    "revenu": CategoryKind.INCOME,
    "transfert": CategoryKind.TRANSFER,
    "neutre": CategoryKind.NEUTRAL,
}


class NewCategory(Params):
    nom: str = Field(max_length=64)
    parent: str | None = Field(None, description="Catégorie racine ; absent : nouvelle racine.")
    type: Literal["depense", "revenu", "transfert", "neutre"] | None = Field(
        None, description="Obligatoire pour une racine (transfert et neutre : hors totaux)."
    )


@tool(
    "creer_categorie",
    "Créer une catégorie",
    "Crée une catégorie (racine, ou sous-catégorie d'une racine : deux niveaux).",
    write=True,
)
def creer_categorie(context: Context, params: NewCategory) -> dict[str, Any]:
    tree = Categories.load(context.engine)
    parent = tree.resolve(params.parent) if params.parent else None
    kind = KIND_NAMES[params.type] if params.type else None
    return apply(
        context,
        params,
        tool_name="creer_categorie",
        summary=f"Catégorie « {params.nom} » créée",
        operation=lambda engine: create_category(
            engine,
            name=params.nom,
            parent_id=parent,
            kind=kind,
            actor_user_id=context.token.user_id,
            now=context.now,
        ),
    )


class RenameCategory(Params):
    categorie: str
    nouveau_nom: str = Field(max_length=64)


@tool("renommer_categorie", "Renommer une catégorie", "Renomme une catégorie.", write=True)
def renommer_categorie(context: Context, params: RenameCategory) -> dict[str, Any]:
    category = Categories.load(context.engine).resolve(params.categorie)
    return apply(
        context,
        params,
        tool_name="renommer_categorie",
        summary=f"Catégorie renommée en « {params.nouveau_nom} »",
        operation=lambda engine: rename_category(
            engine,
            category,
            params.nouveau_nom,
            actor_user_id=context.token.user_id,
            now=context.now,
        ),
    )


class MoveCategory(Params):
    categorie: str
    nouveau_parent: str | None = Field(
        None, description="Racine d'accueil ; absent : devient une racine."
    )


@tool(
    "deplacer_categorie",
    "Déplacer une catégorie",
    "Déplace une sous-catégorie sous une autre racine, ou en fait une racine.",
    write=True,
)
def deplacer_categorie(context: Context, params: MoveCategory) -> dict[str, Any]:
    tree = Categories.load(context.engine)
    category = tree.resolve(params.categorie)
    parent = tree.resolve(params.nouveau_parent) if params.nouveau_parent else None
    return apply(
        context,
        params,
        tool_name="deplacer_categorie",
        summary=f"Catégorie « {tree.paths[category]} » déplacée",
        operation=lambda engine: move_category(
            engine, category, parent, actor_user_id=context.token.user_id, now=context.now
        ),
    )


# --- Règles -----------------------------------------------------------------------------------


class RuleFields(Confirmable):
    libelle_contient: list[str] | None = Field(
        None, max_length=20, description="Fragments (l'un d'eux suffit)."
    )
    libelle_regex: str | None = Field(None, max_length=200)
    sens: Literal["debit", "credit"] | None = None
    montant_min: int | None = Field(None, description="Centimes, signé, borne incluse.")
    montant_max: int | None = Field(None, description="Centimes, signé, borne incluse.")
    comptes: list[str] | None = Field(None, description="Comptes (« c_… » ou noms).")
    priorite: int | None = Field(None, ge=0, le=100000, description="Plus petit = évalué d'abord.")


class NewRule(RuleFields):
    nom: str = Field(max_length=128)
    categorie: str
    a_partir_de_transaction: str | None = Field(
        None,
        description="« t_… » : le serveur ajoute un fragment tiré du vrai libellé de cette "
        "opération (utile quand le libellé montré contient des initiales) et son sens.",
    )


def _conditions(context: Context, params: RuleFields, base: dict[str, Any]) -> dict[str, Any]:
    _check_fragments(params.libelle_contient, params.libelle_regex)
    conditions = dict(base)
    if params.libelle_contient is not None:
        conditions["label_contains"] = [
            *conditions.get("label_contains", []),
            *params.libelle_contient,
        ]
    for key, value in (
        ("label_regex", params.libelle_regex),
        ("direction", params.sens),
        ("amount_min", params.montant_min),
        ("amount_max", params.montant_max),
    ):
        if value is not None:
            conditions[key] = value
    if params.comptes is not None:
        conditions["account_ids"] = [resolve_account(context, c) for c in params.comptes]
    try:
        return RuleConditions.model_validate(conditions).model_dump(exclude_none=True)
    except ValueError as exc:
        raise ToolError(f"Conditions invalides : {exc}") from None


@tool(
    "creer_regle",
    "Créer une règle",
    "Crée une règle de catégorisation, appliquée aussitôt à tout l'historique (les classements "
    "manuels restent). Aperçu et code de confirmation si plus de 20 opérations changent.",
    write=True,
)
def creer_regle(context: Context, params: NewRule) -> dict[str, Any]:
    category = Categories.load(context.engine).resolve(params.categorie)
    base: dict[str, Any] = {}
    if params.a_partir_de_transaction:
        (tx,) = _visible_tx(context, [params.a_partir_de_transaction])
        suggestion = suggest_rule(context.engine, tx)
        base = suggestion.conditions.model_dump(exclude_none=True)
    conditions = _conditions(context, params, base)
    with context.engine.connect() as connection:
        priority = params.priorite
        if priority is None:
            priority = (
                int(connection.scalar(select(func.coalesce(func.max(Rule.priority), 0))) or 0) + 10
            )
    name = context.masker.text(params.nom)
    return apply(
        context,
        params,
        tool_name="creer_regle",
        summary=f"Règle « {name} » créée",
        preview=True,
        operation=lambda engine: create_rule(
            engine,
            name=params.nom,
            priority=priority,
            conditions=conditions,
            category_id=category,
            actor_user_id=context.token.user_id,
            now=context.now,
        ),
    )


class ChangeRule(RuleFields):
    regle: str = Field(description="Identifiant « r_… ».")
    nom: str | None = Field(None, max_length=128)
    categorie: str | None = None
    remplacer_conditions: bool = Field(
        False, description="Vrai : les conditions données remplacent toutes les anciennes."
    )


def _rule(context: Context, text: str) -> Any:
    rule_id = private_id("règle", text)
    with context.engine.connect() as connection:
        rule = connection.execute(select(Rule).where(Rule.id == rule_id)).first()
    if rule is None:
        raise ToolError("Règle introuvable.")
    return rule


@tool(
    "modifier_regle",
    "Modifier une règle",
    "Modifie le nom, la catégorie, la priorité ou les conditions d'une règle ; ré-applique les "
    "règles à l'historique (aperçu si plus de 20 opérations changent).",
    write=True,
)
def modifier_regle(context: Context, params: ChangeRule) -> dict[str, Any]:
    rule = _rule(context, params.regle)
    category = (
        Categories.load(context.engine).resolve(params.categorie) if params.categorie else None
    )
    touched = params.model_fields_set & {
        "libelle_contient",
        "libelle_regex",
        "sens",
        "montant_min",
        "montant_max",
        "comptes",
    }
    conditions = None
    if touched:
        conditions = _conditions(
            context, params, {} if params.remplacer_conditions else dict(rule.conditions)
        )
    return apply(
        context,
        params,
        tool_name="modifier_regle",
        summary=f"Règle « {context.masker.text(rule.name)} » modifiée",
        preview=True,
        operation=lambda engine: update_rule(
            engine,
            rule.id,
            actor_user_id=context.token.user_id,
            now=context.now,
            name=params.nom,
            priority=params.priorite,
            conditions=conditions,
            category_id=category,
        ),
    )


class OneRule(Confirmable):
    regle: str = Field(description="Identifiant « r_… ».")


@tool(
    "supprimer_regle",
    "Supprimer une règle",
    "Désactive une règle (annulable) : les opérations qu'elle classait redeviennent classables "
    "par les autres règles. Aperçu si plus de 20 opérations changent.",
    write=True,
    destructive=True,
)
def supprimer_regle(context: Context, params: OneRule) -> dict[str, Any]:
    rule = _rule(context, params.regle)
    return apply(
        context,
        params,
        tool_name="supprimer_regle",
        summary=f"Règle « {context.masker.text(rule.name)} » désactivée",
        preview=True,
        operation=lambda engine: deactivate_rule(
            engine, rule.id, actor_user_id=context.token.user_id, now=context.now
        ),
    )


# --- File « À valider » -----------------------------------------------------------------------


class Decide(Params):
    element: str = Field(description="Identifiant « v_… » (outil « a_valider »).")
    decision: Literal["confirmer", "rejeter", "garder_actuelle", "prendre_doublon"] = Field(
        description="Doublon ou virement proposé : confirmer / rejeter. Conflit de catégorie : "
        "garder_actuelle / prendre_doublon. Conflit de virement : garder_actuelle."
    )


@tool(
    "resoudre_a_valider",
    "Décider un élément « À valider »",
    "Tranche un doublon probable, un virement interne proposé ou un conflit.",
    write=True,
)
def resoudre_a_valider(context: Context, params: Decide) -> dict[str, Any]:
    item_id = private_id("élément à valider", params.element)
    with context.engine.connect() as connection:
        item = connection.execute(select(ReviewItem).where(ReviewItem.id == item_id)).first()
    if item is None or item.status is not ReviewStatus.OPEN:
        raise ToolError("Élément introuvable ou déjà traité.")
    actor, now, decision = context.token.user_id, context.now, params.decision
    link_id = item.duplicate_link_id
    transfer_id = item.details.get("transfer_link_id")
    possible = {
        ReviewKind.DUPLICATE: ("confirmer", "rejeter") if link_id else (),
        ReviewKind.TRANSFER_CANDIDATE: ("confirmer", "rejeter") if transfer_id else (),
        ReviewKind.CATEGORY_CONFLICT: ("garder_actuelle", "prendre_doublon"),
        ReviewKind.TRANSFER_CONFLICT: ("garder_actuelle",),
    }
    if decision not in possible.get(item.kind, ()):
        raise ToolError("Décision impossible pour ce type d'élément.")

    def operation(engine: Engine) -> None:
        if item.kind is ReviewKind.DUPLICATE:
            decide = confirm_link if decision == "confirmer" else reject_link
            decide(engine, link_id, actor_user_id=actor, now=now)
        elif item.kind is ReviewKind.TRANSFER_CANDIDATE:
            confirm = decision == "confirmer"
            decide_transfer(engine, int(transfer_id), confirm=confirm, actor_user_id=actor, now=now)
        elif item.kind is ReviewKind.CATEGORY_CONFLICT:
            take = decision == "prendre_doublon"
            resolve_category_conflict(
                engine, item_id, take_duplicate=take, actor_user_id=actor, now=now
            )
        else:
            keep_current_transfer(engine, item_id, actor_user_id=actor, now=now)

    return apply(
        context,
        params,
        tool_name="resoudre_a_valider",
        summary=f"Élément à valider : {decision}",
        operation=_then_rental(operation, context),
    )


# --- Loyers -----------------------------------------------------------------------------------


class Assign(Params):
    echeance: str = Field(description="Identifiant « e_… » (outil « loyers »).")
    transaction: str = Field(description="Encaissement « t_… ».")
    montant: int | None = Field(
        None, gt=0, description="Centimes ; défaut : le reste dû ou disponible."
    )
    note: str | None = Field(None, max_length=200, description="Justification d'un écart.")


@tool(
    "affecter_loyer",
    "Affecter un paiement à une échéance",
    "Affecte tout ou partie d'un encaissement à une échéance de loyer ou de remboursement.",
    write=True,
)
def affecter_loyer(context: Context, params: Assign) -> dict[str, Any]:
    due = private_id("échéance", params.echeance)
    (tx,) = _visible_tx(context, [params.transaction])
    return apply(
        context,
        params,
        tool_name="affecter_loyer",
        summary="Paiement affecté à une échéance",
        operation=_then_rental(
            lambda engine: allocate_manually(
                engine,
                due_id=due,
                transaction_id=tx,
                amount_cents=params.montant,
                note=params.note,
                actor_user_id=context.token.user_id,
                now=context.now,
            ),
            context,
        ),
    )


class Unassign(Params):
    affectation: str = Field(description="Identifiant « a_… ».")
    note: str | None = Field(None, max_length=200)


@tool(
    "desaffecter_loyer",
    "Retirer une affectation",
    "Retire l'affectation d'un paiement à une échéance (le moteur ne la reproposera pas).",
    write=True,
    destructive=True,
)
def desaffecter_loyer(context: Context, params: Unassign) -> dict[str, Any]:
    allocation = private_id("affectation", params.affectation)
    return apply(
        context,
        params,
        tool_name="desaffecter_loyer",
        summary="Affectation de loyer retirée",
        operation=_then_rental(
            lambda engine: dissociate(
                engine,
                allocation,
                note=params.note,
                actor_user_id=context.token.user_id,
                now=context.now,
            ),
            context,
        ),
    )


class Proposal(Params):
    affectation: str = Field(description="Proposition « a_… » (outil « loyers »).")
    decision: Literal["confirmer", "rejeter"]


@tool(
    "decider_proposition",
    "Décider une proposition de rapprochement",
    "Confirme ou rejette un rapprochement proposé par le moteur.",
    write=True,
)
def decider_proposition(context: Context, params: Proposal) -> dict[str, Any]:
    allocation = private_id("affectation", params.affectation)
    decide = confirm_proposal if params.decision == "confirmer" else reject_proposal
    return apply(
        context,
        params,
        tool_name="decider_proposition",
        summary=f"Proposition de rapprochement : {params.decision}",
        operation=_then_rental(
            lambda engine: decide(
                engine, allocation, actor_user_id=context.token.user_id, now=context.now
            ),
            context,
        ),
    )


class Overpaid(Params):
    trop_percu: str = Field(description="Identifiant « o_… ».")
    decision: Literal["reporter", "rembourser"] = Field(
        description="Reporter sur l'échéance suivante, ou noter qu'il est à rembourser "
        "(rien n'est viré)."
    )


@tool(
    "decider_trop_percu",
    "Décider un trop-perçu",
    "Reporte un trop-perçu sur l'échéance suivante ou le note à rembourser ; aucun virement "
    "n'est fait.",
    write=True,
)
def decider_trop_percu(context: Context, params: Overpaid) -> dict[str, Any]:
    overpayment = private_id("trop-perçu", params.trop_percu)
    decision = (
        OverpaymentDecision.CARRY_FORWARD
        if params.decision == "reporter"
        else OverpaymentDecision.REFUND
    )
    return apply(
        context,
        params,
        tool_name="decider_trop_percu",
        summary=f"Trop-perçu : {params.decision}",
        operation=_then_rental(
            lambda engine: decide_overpayment(
                engine, overpayment, decision, actor_user_id=context.token.user_id, now=context.now
            ),
            context,
        ),
    )


ROLES = {
    "locataire": PartyRole.TENANT,
    "caution": PartyRole.GUARANTOR,
    "tiers_payeur": PartyRole.THIRD_PAYER,
    "emprunteur": PartyRole.BORROWER,
}


def _payer(context: Context, text: str) -> tuple[str, str]:
    """(nom, motif) du payeur, tirés **par le serveur** du vrai libellé d'une transaction."""
    (tx,) = _visible_tx(context, [text])
    raw, _, amount = _label_clean(context, tx)
    if amount <= 0:
        raise ToolError("Choisir un encaissement (crédit) du payeur.")
    names = context.masker.names(raw)
    if len(names) != 1:
        raise ToolError(
            "Payeur introuvable ou ambigu dans ce libellé : ajouter ce bail ou ce payeur depuis "
            "l'interface web."
        )
    return names[0].title(), normalize_label(names[0])


class NewLease(Params):
    nom: str = Field(max_length=128)
    type: Literal["loyer", "remboursement"] = "loyer"
    bien: str | None = Field(None, description="Bien existant « p_… » (outil « loyers »).")
    nouveau_bien: str | None = Field(None, max_length=128, description="Nom d'un nouveau bien.")
    ma_part: int = Field(
        100, ge=1, le=100, description="Ma quote-part du nouveau bien en % (le reste : indivision)."
    )
    compte: str = Field(description="Compte d'encaissement (« c_… » ou nom).")
    debut: date
    suivi_depuis: date | None = Field(
        None, description="Premier mois suivi (historique disponible)."
    )
    loyer: int = Field(gt=0, description="Loyer hors charges ou mensualité, en centimes.")
    charges: int = Field(0, ge=0)
    jour_echeance: int = Field(1, ge=1, le=28)
    jour_alerte: int = Field(5, ge=1, le=28)
    jour_impaye: int = Field(10, ge=1, le=28)
    depot: int = Field(0, ge=0)
    payeur_transaction: str = Field(
        description="Un encaissement « t_… » du payeur : son nom en est tiré par le serveur."
    )
    role: Literal["locataire", "caution", "tiers_payeur", "emprunteur"] = "locataire"


@tool(
    "creer_bail",
    "Créer un bail ou un remboursement attendu",
    "Crée un bail (ou un remboursement attendu), son bien si besoin et son payeur, reconnu "
    "ensuite dans les libellés ; les alertes suivent les jours donnés.",
    write=True,
)
def creer_bail(context: Context, params: NewLease) -> dict[str, Any]:
    if params.type == "loyer" and not (params.bien or params.nouveau_bien):
        raise ToolError("Un loyer se rattache à un bien : « bien » ou « nouveau_bien ».")
    account = resolve_account(context, params.compte)
    property_id = private_id("bien", params.bien) if params.bien else None
    name, pattern = _payer(context, params.payeur_transaction)
    actor, now = context.token.user_id, context.now

    def operation(engine: Engine) -> int:
        lease = create_lease_with_parties(
            engine,
            LeaseTerms(
                kind=LeaseKind.RENT if params.type == "loyer" else LeaseKind.LOAN,
                label=params.nom,
                account_id=account,
                start_date=params.debut,
                rent_cents=params.loyer,
                property_id=property_id,
                charges_cents=params.charges,
                due_day=params.jour_echeance,
                alert_day=params.jour_alerte,
                unpaid_day=params.jour_impaye,
                deposit_cents=params.depot,
                tracking_start=params.suivi_depuis,
            ),
            new_property=(params.nouveau_bien, params.ma_part) if params.nouveau_bien else None,
            parties=[NewParty(ROLES[params.role], name, patterns=(pattern,))],
            now=now,
            actor_user_id=actor,
        )
        run_rental(engine, now=now)
        return lease

    result = apply(
        context,
        params,
        tool_name="creer_bail",
        summary=f"Bail « {params.nom} » créé",
        operation=operation,
    )
    return result


class ChangeLease(Confirmable):
    bail: str = Field(description="Identifiant « b_… ».")
    nom: str | None = Field(None, max_length=128)
    suivi_depuis: str | None = Field(
        None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$", description="Premier mois suivi, AAAA-MM."
    )
    loyer: int | None = Field(None, gt=0)
    charges: int | None = Field(None, ge=0)
    a_partir_de: str | None = Field(
        None,
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
        description="Mois du nouveau montant (obligatoire avec loyer ou charges).",
    )
    jour_echeance: int | None = Field(None, ge=1, le=28)
    jour_alerte: int | None = Field(None, ge=1, le=28)
    jour_impaye: int | None = Field(None, ge=1, le=28)


@tool(
    "modifier_bail",
    "Modifier un bail",
    "Nom, premier mois suivi (les échéances antérieures sont retirées), montants à partir d'un "
    "mois, jours d'échéance et d'alertes.",
    write=True,
)
def modifier_bail(context: Context, params: ChangeLease) -> dict[str, Any]:
    lease = private_id("bail", params.bail)
    if (params.loyer is not None or params.charges is not None) and not params.a_partir_de:
        raise ToolError("Nouveau montant : préciser « a_partir_de » (AAAA-MM).")
    changes = LeaseChanges(
        label=params.nom,
        tracking_start=date.fromisoformat(params.suivi_depuis + "-01")
        if params.suivi_depuis
        else None,
        rent_cents=params.loyer,
        charges_cents=params.charges,
        amounts_from=params.a_partir_de,
        due_day=params.jour_echeance,
        alert_day=params.jour_alerte,
        unpaid_day=params.jour_impaye,
    )
    return apply(
        context,
        params,
        tool_name="modifier_bail",
        summary="Bail modifié",
        preview=True,
        operation=_then_rental(
            lambda engine: update_lease(
                engine, lease, changes, actor_user_id=context.token.user_id, now=context.now
            ),
            context,
        ),
    )


class EndLease(Params):
    bail: str = Field(description="Identifiant « b_… ».")
    fin: date


@tool(
    "terminer_bail",
    "Terminer un bail",
    "Fixe la date de fin d'un bail (plus d'échéance après) ; rien n'est effacé.",
    write=True,
)
def terminer_bail(context: Context, params: EndLease) -> dict[str, Any]:
    lease = private_id("bail", params.bail)
    return apply(
        context,
        params,
        tool_name="terminer_bail",
        summary="Bail terminé",
        operation=_then_rental(
            lambda engine: end_lease(
                engine,
                lease,
                end_date=params.fin,
                now=context.now,
                actor_user_id=context.token.user_id,
            ),
            context,
        ),
    )


class NewPayer(Params):
    bail: str = Field(description="Identifiant « b_… ».")
    transaction: str = Field(description="Un encaissement « t_… » de ce payeur.")
    role: Literal["locataire", "caution", "tiers_payeur", "emprunteur"] = "tiers_payeur"


@tool(
    "ajouter_payeur",
    "Ajouter un payeur",
    "Ajoute un payeur à un bail, reconnu ensuite dans les libellés ; son nom est tiré par le "
    "serveur d'un de ses encaissements.",
    write=True,
)
def ajouter_payeur(context: Context, params: NewPayer) -> dict[str, Any]:
    lease = private_id("bail", params.bail)
    name, pattern = _payer(context, params.transaction)
    return apply(
        context,
        params,
        tool_name="ajouter_payeur",
        summary="Payeur ajouté",
        operation=_then_rental(
            lambda engine: add_party(
                engine, lease, role=ROLES[params.role], name=name, patterns=[pattern]
            ),
            context,
        ),
    )


EXPENSE_KINDS = {kind.value: kind for kind in ExpenseKind}


class LinkExpense(Params):
    bien: str = Field(description="Identifiant « p_… ».")
    transaction: str = Field(description="Dépense « t_… ».")
    type: Literal[
        "copropriete", "taxe_fonciere", "assurance", "travaux", "credit", "entretien", "autre"
    ]


@tool(
    "affecter_charge",
    "Rattacher une charge au bien",
    "Rattache une dépense aux charges d'un bien (LOC-10).",
    write=True,
)
def affecter_charge(context: Context, params: LinkExpense) -> dict[str, Any]:
    property_id = private_id("bien", params.bien)
    (tx,) = _visible_tx(context, [params.transaction])
    return apply(
        context,
        params,
        tool_name="affecter_charge",
        summary="Charge rattachée au bien",
        operation=lambda engine: link(
            engine,
            property_id,
            tx,
            kind=EXPENSE_KINDS[params.type],
            actor_user_id=context.token.user_id,
            now=context.now,
        ),
    )


class UnlinkExpense(Params):
    charge: str = Field(description="Identifiant « h_… » (outil « transaction »).")


@tool(
    "retirer_charge",
    "Retirer une charge du bien",
    "Retire une dépense des charges d'un bien.",
    write=True,
    destructive=True,
)
def retirer_charge(context: Context, params: UnlinkExpense) -> dict[str, Any]:
    expense = private_id("charge", params.charge)
    return apply(
        context,
        params,
        tool_name="retirer_charge",
        summary="Charge retirée du bien",
        operation=lambda engine: unlink(
            engine, expense, actor_user_id=context.token.user_id, now=context.now
        ),
    )


# --- Annulation -------------------------------------------------------------------------------


class UndoParams(Params):
    modification: str = Field(description="Identifiant « m_… » (outil « modifications »).")


@tool(
    "annuler_modification",
    "Annuler une modification",
    "Rétablit l'état d'avant un lot de modifications fait via l'IA ; refusé si les objets ont "
    "été modifiés depuis.",
    write=True,
)
def annuler_modification(context: Context, params: UndoParams) -> dict[str, Any]:
    change_id = private_id("modification", params.modification)
    try:
        changes = undo(
            context.engine, change_id, actor_user_id=context.token.user_id, now=context.now
        )
    except ChangeError as exc:
        raise ToolError(str(exc)) from exc
    return {"annulee": True, "lignes_retablies": len(changes)}
