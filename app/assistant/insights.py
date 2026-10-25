"""Outils d'analyse de l'IA (docs/spec-mcp.md §7) : contenu d'un compte, flux entre comptes,
prévision de trésorerie (ANA-10), anomalies (ANA-08), bilan locatif annuel (LOC, FIS).

Mêmes règles que les autres outils de lecture : pseudonymisation, comptes ignorés invisibles,
doublons confirmés exclus, bornes sur les périodes et le nombre de lignes.
"""

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from pydantic import Field
from sqlalchemy import and_, func, select

from app.assistant.tools import (
    Categories,
    Context,
    Month,
    Params,
    ToolError,
    accounts,
    as_amount,
    call_tool,
    check_months,
    merchant,
    public_id,
    resolve_account,
    tool,
)
from app.ledger.recurring import Operation
from app.ledger.recurring import anomalies as detect_anomalies
from app.ledger.views import confirmed_transfer_pairs, is_effective
from app.models import (
    Account,
    BalanceSnapshot,
    Categorization,
    Lease,
    LedgerTransaction,
    Property,
    RentAllocation,
    RentDue,
    TransferLink,
)
from app.models.ledger import TransferStatus
from app.models.rental import AllocationStatus
from app.months import month_of, month_range, month_start, shift_month
from app.rental.expenses import expenses, totals_by_kind

TOP = 8
ANOMALY_LABELS = {
    "frais": ("frais bancaires", "libellé de frais ou de commission"),
    "montant_inhabituel": ("montant inhabituel", ""),
    "nouveau_crediteur": (
        "nouveau commerçant",
        "première opération chez ce commerçant depuis un an",
    ),
    "doublon": ("doublon possible", "même montant, même commerçant, même jour"),
}


def _effective(account_ids: list[int], since: date) -> Any:
    return (
        select(
            LedgerTransaction.id,
            LedgerTransaction.account_id,
            LedgerTransaction.booking_date,
            LedgerTransaction.amount_cents,
            LedgerTransaction.label_clean,
        )
        .where(
            LedgerTransaction.account_id.in_(account_ids),
            LedgerTransaction.booking_date >= since,
            is_effective(),
        )
        .order_by(LedgerTransaction.booking_date, LedgerTransaction.id)
    )


# --- Contenu d'un compte -----------------------------------------------------------------------


class AccountDetail(Params):
    compte: str = Field(description="Compte (identifiant « c_… » ou nom).")
    mois: int = Field(6, ge=1, le=24, description="Nombre de mois analysés.")


@dataclass(frozen=True)
class _Activity:
    """Données brutes d'un compte sur la période (lecture seule, une connexion)."""

    role: str | None
    rows: list[Any]  # transactions effectives
    pairs: dict[int, int]  # jambe de virement interne → autre jambe
    other_account: dict[int, int]  # autre jambe → son compte
    lines: list[Any]  # lignes de catégorisation actives
    balances: list[Any]


def _activity(context: Context, account_id: int, since: date) -> _Activity:
    with context.engine.connect() as connection:
        role = connection.scalar(select(Account.description).where(Account.id == account_id))
        rows = list(connection.execute(_effective([account_id], since)).all())
        pairs = confirmed_transfer_pairs(connection)
        other_account = dict(
            connection.execute(
                select(LedgerTransaction.id, LedgerTransaction.account_id).where(
                    LedgerTransaction.id.in_(list(pairs.values()) or [0])
                )
            ).all()
        )
        lines = list(
            connection.execute(
                select(
                    Categorization.transaction_id,
                    Categorization.category_id,
                    Categorization.amount_cents,
                ).where(
                    Categorization.transaction_id.in_([r.id for r in rows] or [0]),
                    Categorization.superseded_at.is_(None),
                )
            ).all()
        )
        balances = list(
            connection.execute(
                select(BalanceSnapshot.as_of_date, BalanceSnapshot.amount_cents)
                .where(
                    BalanceSnapshot.account_id == account_id, BalanceSnapshot.as_of_date >= since
                )
                .order_by(BalanceSnapshot.as_of_date, BalanceSnapshot.id)
            ).all()
        )
    return _Activity(role, rows, pairs, other_account, lines, balances)


def _ranked(groups: dict[str, list[int]]) -> list[dict[str, Any]]:
    """Groupes (libellé → montants) du plus gros total au plus petit, `TOP` au plus."""
    ranked = sorted(groups.items(), key=lambda item: abs(sum(item[1])), reverse=True)
    return [
        {"libelle": label, "total": as_amount(sum(amounts)), "operations": len(amounts)}
        for label, amounts in ranked[:TOP]
    ]


def _category_totals(per_category: Counter[str], *, expenses: bool) -> list[dict[str, Any]]:
    ordered = sorted(per_category.items(), key=lambda kv: kv[1] if expenses else -kv[1])
    return [
        {"categorie": name, "montant": as_amount(total)}
        for name, total in ordered
        if (total < 0 if expenses else total > 0)
    ][:TOP]


@tool(
    "detail_compte",
    "Contenu d'un compte",
    "Ce que contient un compte, quel que soit son statut : rôle, solde et son évolution, flux "
    "mensuels, principales catégories et principaux postes de dépense et de recette, virements "
    "internes avec les autres comptes, opérations non catégorisées. Pour comprendre à quoi sert "
    "un compte avant de l'interpréter.",
)
def detail_compte(context: Context, params: AccountDetail) -> dict[str, Any]:
    account_id = resolve_account(context, params.compte)
    known = accounts(context)
    info = known[account_id]
    first = shift_month(month_of(context.today), -(params.mois - 1))
    since = month_start(first)
    tree = Categories.load(context.engine)
    activity = _activity(context, account_id, since)

    by_month: dict[str, list[int]] = defaultdict(lambda: [0, 0])  # [entrées, sorties]
    spending: dict[str, list[int]] = defaultdict(list)
    income: dict[str, list[int]] = defaultdict(list)
    transfers: dict[tuple[str, str], list[int]] = defaultdict(list)
    for row in activity.rows:
        by_month[month_of(row.booking_date)][0 if row.amount_cents > 0 else 1] += row.amount_cents
        if row.id in activity.pairs:
            other = known.get(activity.other_account.get(activity.pairs[row.id], 0))
            direction = "reçu de" if row.amount_cents > 0 else "envoyé vers"
            transfers[(direction, other.name if other else "compte ignoré")].append(
                row.amount_cents
            )
        else:
            target = spending if row.amount_cents < 0 else income
            target[merchant(context, row.label_clean)].append(row.amount_cents)
    categorized = {tx for tx, _, _ in activity.lines}
    per_category: Counter[str] = Counter()
    for _, category, amount in activity.lines:
        per_category[tree.paths.get(category, "?")] += amount
    month_end = {month_of(as_of): int(amount) for as_of, amount in activity.balances}

    return {
        "compte": {
            "id": info.public,
            "nom": info.name,
            "type": info.type,
            "statut": "carte"
            if info.card
            else ("compté" if info.counted else "consultation seule"),
            "role": context.masker.text(activity.role) if activity.role else None,
        },
        "periode": {"du": since.isoformat(), "au": context.today.isoformat()},
        "operations": len(activity.rows),
        "non_categorisees": sum(
            1 for r in activity.rows if r.id not in categorized and r.id not in activity.pairs
        ),
        "mois": [
            {
                "mois": m,
                "entrees": as_amount(by_month[m][0]),
                "sorties": as_amount(by_month[m][1]),
                "solde_fin_de_mois": as_amount(month_end[m]) if m in month_end else None,
            }
            for m in month_range(first, month_of(context.today))
        ],
        "categories_depenses": _category_totals(per_category, expenses=True),
        "categories_recettes": _category_totals(per_category, expenses=False),
        "principales_depenses": _ranked(spending),
        "principales_recettes": _ranked(income),
        "virements_internes": [
            {
                "sens": direction,
                "autre_compte": other,
                "total": as_amount(sum(a)),
                "operations": len(a),
            }
            for (direction, other), a in sorted(transfers.items())
        ],
    }


# --- Flux entre comptes --------------------------------------------------------------------------


class Flows(Params):
    du: Month | None = None
    au: Month | None = None


@tool(
    "flux_entre_comptes",
    "Flux entre comptes",
    "Virements internes confirmés entre mes comptes sur une période (défaut : 3 derniers mois) : "
    "de quel compte vers lequel, total et nombre. Montre comment l'argent circule (salaire → "
    "compte joint, épargne…).",
)
def flux_entre_comptes(context: Context, params: Flows) -> dict[str, Any]:
    last = params.au or month_of(context.today)
    first = params.du or shift_month(last, -2)
    check_months(first, last)
    start = date.fromisoformat(first + "-01")
    end = date.fromisoformat(shift_month(last, 1) + "-01")
    known = accounts(context)
    debit, credit = LedgerTransaction.__table__.alias("d"), LedgerTransaction.__table__.alias("c")
    with context.engine.connect() as connection:
        rows = connection.execute(
            select(
                debit.c.account_id,
                credit.c.account_id,
                func.sum(credit.c.amount_cents),
                func.count(),
            )
            .select_from(TransferLink)
            .join(debit, debit.c.id == TransferLink.debit_tx_id)
            .join(credit, credit.c.id == TransferLink.credit_tx_id)
            .where(
                TransferLink.status == TransferStatus.CONFIRMED,
                and_(debit.c.booking_date >= start, debit.c.booking_date < end),
            )
            .group_by(debit.c.account_id, credit.c.account_id)
        ).all()
    flows: list[dict[str, Any]] = [
        {
            "de": known[source].name,
            "vers": known[target].name,
            "total": as_amount(int(total)),
            "virements": int(count),
        }
        for source, target, total, count in rows
        if source in known and target in known
    ]
    flows.sort(key=lambda f: -int(f["total"]["centimes"]))
    return {"periode": {"du": first, "au": last}, "flux": flows}


# --- Prévision de trésorerie ----------------------------------------------------------------


class Forecast(Params):
    jours: int = Field(30, ge=7, le=90, description="Horizon en jours.")


def _occurrences(start: date, step: int, until: date) -> list[date]:
    dates, current = [], start
    while current <= until:
        dates.append(current)
        current += timedelta(days=step)
    return dates


@tool(
    "prevision_tresorerie",
    "Prévision de trésorerie",
    "Solde projeté des comptes comptés sur 7 à 90 jours : solde actuel, opérations récurrentes "
    "attendues (abonnements, salaire, prélèvements) et loyers attendus non encore reçus ; point "
    "bas et sa date. Estimation : ne tient pas compte des dépenses ponctuelles.",
)
def prevision_tresorerie(context: Context, params: Forecast) -> dict[str, Any]:
    horizon = context.today + timedelta(days=params.jours)
    start = call_tool(context, "patrimoine", {})["aujourd_hui"]["centimes"]
    steps = {"hebdomadaire": 7, "mensuelle": 30, "trimestrielle": 91, "annuelle": 365}
    events: list[tuple[date, int, str]] = []
    recurring_credits: list[int] = []
    for item in call_tool(context, "recurrents", {"mois_analyses": 6})["recurrents"]:
        if not item["compte_compte"]:
            continue
        amount = item["dernier"]["montant"]["centimes"]
        if amount > 0:
            recurring_credits.append(amount)
        first = date.fromisoformat(item["prochaine_date_estimee"])
        for day in _occurrences(max(first, context.today), steps[item["frequence"]], horizon):
            events.append((day, amount, item["libelle"]))
    for lease in call_tool(context, "loyers", {})["baux"]:
        monthly = lease["mensuel"]["centimes"]
        if any(abs(c - monthly) * 10 <= monthly for c in recurring_credits):
            continue  # déjà vu comme recette récurrente
        for due in lease["echeances"]:
            if due["reste"]["centimes"] > 0:
                events.append(
                    (context.today, due["reste"]["centimes"], f"{lease['nom']} {due['periode']}")
                )
        month = shift_month(month_of(context.today), 1)
        while True:
            day = date.fromisoformat(f"{month}-{lease['jour_echeance']:02d}")
            if day > horizon or (lease["fin"] and day > date.fromisoformat(lease["fin"])):
                break
            events.append((day, monthly, lease["nom"]))
            month = shift_month(month, 1)
    events.sort()
    balance, lowest, lowest_day = start, start, context.today
    for day, amount, _ in events:
        balance += amount
        if balance < lowest:
            lowest, lowest_day = balance, day
    return {
        "solde_actuel": as_amount(start),
        "horizon": horizon.isoformat(),
        "solde_projete": as_amount(balance),
        "point_bas": {"montant": as_amount(lowest), "date": lowest_day.isoformat()},
        "evenements": [
            {"date": day.isoformat(), "montant": as_amount(amount), "libelle": label}
            for day, amount, label in events[:60]
        ],
        "perimetre": "Comptes comptés ; récurrents détectés sur 6 mois et loyers attendus.",
    }


# --- Anomalies ---------------------------------------------------------------------------------


class Anomalies(Params):
    jours: int = Field(30, ge=7, le=120, description="Période examinée, en jours.")


@tool(
    "anomalies",
    "Anomalies",
    "Opérations récentes qui sortent de l'ordinaire : montant inhabituel pour un commerçant "
    "connu, nouveau commerçant avec un montant notable, frais bancaires, doublons possibles "
    "(même montant, même commerçant, même jour). Cartes exclues (elles répètent le compte).",
)
def anomalies(context: Context, params: Anomalies) -> dict[str, Any]:
    known = {a.id: a for a in accounts(context).values() if not a.card}
    recent_from = context.today - timedelta(days=params.jours)
    with context.engine.connect() as connection:
        pairs = confirmed_transfer_pairs(connection)
        rows = connection.execute(
            _effective(list(known), context.today - timedelta(days=365))
        ).all()
    operations = [
        Operation(
            row.id,
            row.booking_date,
            row.amount_cents,
            merchant(context, row.label_clean),
            row.account_id,
            row.label_clean,
        )
        for row in rows
        if row.id not in pairs
    ]
    found = []
    for anomaly in detect_anomalies(operations, recent_from):
        row = anomaly.operation
        kind, detail = ANOMALY_LABELS[anomaly.kind]
        if anomaly.usual_cents is not None:
            detail = f"habituellement {as_amount(anomaly.usual_cents)['eur']}"
        found.append(
            {
                "type": kind,
                "transaction": public_id("transaction", row.id),
                "date": row.booking_date.isoformat(),
                "montant": as_amount(row.amount_cents),
                "libelle": context.masker.label(row.label),
                "compte": known[row.account_id].name,
                "detail": detail,
            }
        )
    return {"depuis": recent_from.isoformat(), "anomalies": found[-100:]}


# --- Bilan locatif annuel -----------------------------------------------------------------------


class RentalYear(Params):
    annee: int = Field(description="Année civile, par exemple 2026.", ge=2000, le=2100)


@tool(
    "bilan_locatif",
    "Bilan locatif annuel",
    "Pour une année : par bail, loyers dus, reçus et restant dus ; par bien, charges rattachées "
    "par nature (copropriété, taxe foncière, assurance, travaux…) et résultat (reçu moins "
    "charges). "
    "Base pour la déclaration des revenus locatifs ; à vérifier avec un professionnel.",
)
def bilan_locatif(context: Context, params: RentalYear) -> dict[str, Any]:
    year = str(params.annee)
    with context.engine.connect() as connection:
        leases = connection.execute(
            select(Lease.id, Lease.label, Lease.property_id, Lease.kind)
        ).all()
        dues = connection.execute(
            select(RentDue.lease_id, func.sum(RentDue.amount_cents))
            .where(RentDue.period.like(f"{year}-%"))
            .group_by(RentDue.lease_id)
        ).all()
        received = connection.execute(
            select(RentDue.lease_id, func.sum(RentAllocation.amount_cents))
            .join(RentDue, RentDue.id == RentAllocation.due_id)
            .where(
                RentDue.period.like(f"{year}-%"),
                RentAllocation.status == AllocationStatus.CONFIRMED,
            )
            .group_by(RentDue.lease_id)
        ).all()
        properties = connection.execute(select(Property.id, Property.name)).all()
    due_by, got_by = dict(dues), dict(received)
    lease_rows = [
        {
            "bail": context.masker.text(label),
            "id": public_id("bail", lease_id),
            "type": "loyer" if str(kind) == "rent" else "remboursement",
            "du": as_amount(int(due_by.get(lease_id, 0))),
            "recu": as_amount(int(got_by.get(lease_id, 0))),
            "reste": as_amount(int(due_by.get(lease_id, 0)) - int(got_by.get(lease_id, 0))),
        }
        for lease_id, label, _, kind in leases
    ]
    property_rows = []
    for property_id, name in properties:
        rows = expenses(context.engine, property_id, params.annee)
        totals = totals_by_kind(rows)
        income = sum(
            int(got_by.get(lease_id, 0))
            for lease_id, _, prop, kind in leases
            if prop == property_id and str(kind) == "rent"
        )
        charges = sum(totals.values())
        property_rows.append(
            {
                "bien": context.masker.text(name),
                "id": public_id("bien", property_id),
                "loyers_recus": as_amount(income),
                "charges": {str(kind): as_amount(total) for kind, total in totals.items()},
                "total_charges": as_amount(charges),
                "resultat": as_amount(income + charges),
            }
        )
    if not lease_rows and not property_rows:
        raise ToolError("Aucun bail ni bien enregistré.")
    return {"annee": params.annee, "baux": lease_rows, "biens": property_rows}
