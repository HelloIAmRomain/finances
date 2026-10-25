"""Budgets mensuels par catégorie (ANA-06) : enveloppe, dépense du mois, alertes à 80 et 100 %.

- La dépense d'un budget est la **dépense nette** du mois dans sa catégorie (et ses
  sous-catégories pour une racine), calculée sur les mêmes lignes que le tableau de bord
  (comptes comptés, doublons et virements internes exclus, ventilation comprise) : un
  remboursement réduit la dépense, qui ne descend jamais sous zéro.
- Alertes : « 80 % atteint » (info) puis « dépassé » (avertissement), une fois par budget et par
  mois (inv. 3) ; résolues si la dépense repasse sous le seuil (remboursement, correction de
  catégorie) ou une fois le mois terminé. Titres sans montant (DT §6).
- Pourcentages entiers arrondis vers le bas : jamais de flottant (inv. 2).
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Engine, delete, func, insert, select, update

from app.alerts.engine import raise_alert, resolve_by_key
from app.audit import audit
from app.db import inserted_id, write_transaction
from app.ledger.aggregates import counted_lines
from app.ledger.search import category_choices
from app.models import Alert, Budget, Category
from app.models.alerts import AlertStatus, Severity
from app.models.categorize import CategoryKind
from app.months import month_of, shift_month
from app.timeutil import paris_date

WARN_PERCENT, OVER_PERCENT = 80, 100
AVERAGE_MONTHS = 3


class BudgetError(ValueError):
    """Budget refusé (message affiché tel quel)."""


@dataclass(frozen=True)
class BudgetStatus:
    id: int
    category_id: int
    name: str  # « Parent › Enfant » ou racine
    monthly_cents: int
    spent_cents: int  # dépense nette du mois, positive

    @property
    def remaining_cents(self) -> int:
        return self.monthly_cents - self.spent_cents

    @property
    def percent(self) -> int:
        return self.spent_cents * 100 // self.monthly_cents

    @property
    def level(self) -> str:
        """« ok », « proche » (≥ 80 %) ou « depasse » (> 100 %)."""
        if self.spent_cents > self.monthly_cents:
            return "depasse"
        return "proche" if self.percent >= WARN_PERCENT else "ok"


def _categories(engine: Engine) -> dict[int, tuple[int | None, str, CategoryKind]]:
    with engine.connect() as connection:
        return {
            row.id: (row.parent_id, row.name, row.kind)
            for row in connection.execute(
                select(Category.id, Category.parent_id, Category.name, Category.kind)
            )
        }


def category_path(categories: dict[int, tuple[int | None, str, CategoryKind]], cid: int) -> str:
    parent_id, name, _ = categories[cid]
    return f"{categories[parent_id][1]} › {name}" if parent_id is not None else name


def _covered(categories: dict[int, tuple[int | None, str, CategoryKind]], cid: int) -> set[int]:
    return {cid} | {other for other, (parent, _, _) in categories.items() if parent == cid}


def set_budget(
    engine: Engine,
    category_id: int,
    monthly_cents: int,
    *,
    actor_user_id: int | None,
    now: datetime,
) -> int:
    """Crée ou modifie l'enveloppe d'une catégorie de dépenses."""
    if monthly_cents <= 0:
        raise BudgetError("Le budget doit être un montant positif.")
    with write_transaction(engine) as connection:
        kind = connection.scalar(select(Category.kind).where(Category.id == category_id))
        if kind is None:
            raise BudgetError("Catégorie introuvable.")
        if kind is not CategoryKind.EXPENSE:
            raise BudgetError("Un budget porte sur une catégorie de dépenses.")
        existing = connection.scalar(select(Budget.id).where(Budget.category_id == category_id))
        if existing is None:
            budget_id = inserted_id(
                connection.execute(
                    insert(Budget).values(
                        category_id=category_id,
                        monthly_cents=monthly_cents,
                        created_at=now,
                        updated_at=now,
                    )
                )
            )
            action = "budget.created"
        else:
            budget_id = int(existing)
            connection.execute(
                update(Budget)
                .where(Budget.id == budget_id)
                .values(monthly_cents=monthly_cents, updated_at=now)
            )
            action = "budget.updated"
        audit(
            connection,
            action,
            ("budget", budget_id),
            now=now,
            actor_user_id=actor_user_id,
            details={"categorie": category_id, "mensuel": monthly_cents},
        )
    return budget_id


def remove_budget(
    engine: Engine, budget_id: int, *, actor_user_id: int | None, now: datetime
) -> None:
    with write_transaction(engine) as connection:
        category_id = connection.scalar(select(Budget.category_id).where(Budget.id == budget_id))
        if category_id is None:
            raise BudgetError("Budget introuvable.")
        connection.execute(delete(Budget).where(Budget.id == budget_id))
        audit(
            connection,
            "budget.deleted",
            ("budget", budget_id),
            now=now,
            actor_user_id=actor_user_id,
            details={"categorie": category_id},
        )
    _resolve_open(engine, budget_id=budget_id, before_month=None, now=now)


def _sums(engine: Engine, months: list[str]) -> dict[tuple[int, str], int]:
    """Somme des lignes catégorisées par (catégorie, mois), en une seule requête."""
    lines = counted_lines()
    query = (
        select(lines.c.category_id, lines.c.month, func.sum(lines.c.amount))
        .where(lines.c.category_id.is_not(None), lines.c.month.in_(months))
        .group_by(lines.c.category_id, lines.c.month)
    )
    with engine.connect() as connection:
        return {(int(cid), month): int(total) for cid, month, total in connection.execute(query)}


def _net(sums: dict[tuple[int, str], int], category_ids: set[int], month: str) -> int:
    """Dépense nette (positive, jamais négative) de ces catégories pour ce mois."""
    return max(0, -sum(sums.get((cid, month), 0) for cid in category_ids))


def budget_statuses(engine: Engine, month: str) -> list[BudgetStatus]:
    categories = _categories(engine)
    with engine.connect() as connection:
        budgets = connection.execute(
            select(Budget.id, Budget.category_id, Budget.monthly_cents)
        ).all()
    sums = _sums(engine, [month])
    statuses = [
        BudgetStatus(
            id=row.id,
            category_id=row.category_id,
            name=category_path(categories, row.category_id),
            monthly_cents=row.monthly_cents,
            spent_cents=_net(sums, _covered(categories, row.category_id), month),
        )
        for row in budgets
    ]
    return sorted(statuses, key=lambda s: (-s.percent, s.name))


def average_spending(engine: Engine, month: str, *, months: int = AVERAGE_MONTHS) -> dict[int, int]:
    """Dépense nette mensuelle moyenne des `months` mois précédant `month`, par catégorie de
    dépenses (sous-catégories comprises pour une racine) : aide à fixer un budget."""
    categories = _categories(engine)
    previous = [shift_month(month, -k) for k in range(1, months + 1)]
    sums = _sums(engine, previous)
    averages = {}
    for cid, (_, _, kind) in categories.items():
        if kind is not CategoryKind.EXPENSE:
            continue
        covered = _covered(categories, cid)
        total = sum(_net(sums, covered, m) for m in previous)
        if total:
            averages[cid] = total // months
    return averages


def _resolve_open(
    engine: Engine, *, budget_id: int | None, before_month: str | None, now: datetime
) -> None:
    """Résout les alertes de budget d'un budget supprimé, ou des mois terminés."""
    with engine.connect() as connection:
        rows = connection.execute(
            select(Alert.dedupe_key).where(
                Alert.type == "budget", Alert.status != AlertStatus.RESOLVED
            )
        ).all()
    for (key,) in rows:
        _, raw_id, month, _level = key.partition("#")[0].split(":")
        if (budget_id is not None and int(raw_id) == budget_id) or (
            before_month is not None and month < before_month
        ):
            resolve_by_key(engine, key.partition("#")[0], now=now)


def _title(name: str, text: str) -> list[str]:
    return [f"Budget « {name[:60]} » : {text}", f"Budget : {text}"]


def check_budgets(engine: Engine, *, now: datetime) -> int:
    """Lève ou résout les alertes du mois en cours ; renvoie le nombre de budgets signalés."""
    month = month_of(paris_date(now))
    _resolve_open(engine, budget_id=None, before_month=month, now=now)
    signalled = 0
    for status in budget_statuses(engine, month):
        for threshold, level, severity, text in (
            (WARN_PERCENT, "80", Severity.INFO, "80 % atteint"),
            (OVER_PERCENT, "100", Severity.WARNING, "dépassé"),
        ):
            key = f"budget:{status.id}:{month}:{level}"
            reached = (
                status.spent_cents > status.monthly_cents
                if threshold == OVER_PERCENT
                else status.percent >= threshold
            )
            if not reached:
                resolve_by_key(engine, key, now=now)
                continue
            for title in _title(status.name, text):
                try:
                    raise_alert(
                        engine,
                        type="budget",
                        severity=severity,
                        key=key,
                        title=title,
                        subject=("budget", status.id),
                        once=True,
                        details={"pourcentage": status.percent, "mois": month},
                        now=now,
                    )
                    break
                except ValueError:  # nom de catégorie qui ressemble à un montant
                    continue
        signalled += status.level != "ok"
    return signalled


def expense_category_choices(engine: Engine) -> list[tuple[int, str]]:
    """Catégories de dépenses actives, dans l'ordre de l'arbre (« Parent › Enfant »)."""
    categories = _categories(engine)
    return [
        (cid, label)
        for cid, label in category_choices(engine)
        if categories[cid][2] is CategoryKind.EXPENSE
    ]
