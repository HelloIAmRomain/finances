"""Agrégats du tableau de bord (ANA-09), calculés en SQL sur des centimes entiers.

- Seuls comptent les comptes « comptés » (`include_in_net_worth`) : un compte en consultation
  seule (compte joint, compte en indivision, carte à débit immédiat qui répète le compte courant)
  est visible mais n'entre ni dans le patrimoine, ni dans les recettes et dépenses.
- Seules comptent les transactions effectives (doublons confirmés exclus) hors virements internes
  confirmés entre deux comptes comptés (COL-06). Un virement vers un compte en consultation seule
  (versement sur le compte joint, par exemple) reste une dépense.
- Une transaction catégorisée compte par ses lignes (ventilation, ANA-04) ; les lignes de catégorie
  « transfert » (épargne…) ou « neutre » ne sont ni des recettes ni des dépenses.
- Une transaction non catégorisée compte pour son montant.
- Taux d'épargne en pour mille, arrondi une seule fois (inv. 2).
"""

from dataclasses import dataclass
from datetime import date
from fractions import Fraction

from sqlalchemy import (
    ColumnElement,
    CompoundSelect,
    Engine,
    Select,
    Subquery,
    and_,
    case,
    exists,
    func,
    literal,
    select,
    union,
    union_all,
)
from sqlalchemy.orm import InstrumentedAttribute, aliased

from app.ledger.balances import reference_balances
from app.ledger.views import is_effective
from app.models import (
    Account,
    Categorization,
    Category,
    LedgerTransaction,
    TransferLink,
)
from app.models.categorize import CategoryKind
from app.models.ledger import TransferStatus
from app.money import round_to_cents
from app.months import month_range

EXCLUDED_KINDS = (CategoryKind.TRANSFER, CategoryKind.NEUTRAL)
UNCATEGORIZED = "Non catégorisé"


@dataclass(frozen=True)
class MonthFlow:
    month: str  # « AAAA-MM »
    income: int
    expenses: int  # négatif

    @property
    def net(self) -> int:
        return self.income + self.expenses

    @property
    def savings_rate_permille(self) -> int | None:
        if self.income <= 0:
            return None
        return round_to_cents(Fraction(self.net * 1000, self.income))


@dataclass(frozen=True)
class NetWorth:
    total: int
    accounts_without_balance: list[str]


def _month(column: ColumnElement[date] | InstrumentedAttribute[date]) -> ColumnElement[str]:
    return func.strftime("%Y-%m", column)


def counted_accounts() -> Select[int]:
    return select(Account.id).where(
        Account.include_in_net_worth.is_(True), Account.ignored_at.is_(None)
    )


def internal_transfer_legs() -> CompoundSelect[int]:
    """Jambes des virements internes confirmés dont les **deux** comptes sont comptés."""
    debit, credit = aliased(LedgerTransaction), aliased(LedgerTransaction)
    between_counted = (
        select(TransferLink.debit_tx_id, TransferLink.credit_tx_id)
        .join(debit, debit.id == TransferLink.debit_tx_id)
        .join(credit, credit.id == TransferLink.credit_tx_id)
        .where(
            TransferLink.status == TransferStatus.CONFIRMED,
            debit.account_id.in_(counted_accounts()),
            credit.account_id.in_(counted_accounts()),
        )
        .subquery()
    )
    return union(select(between_counted.c.debit_tx_id), select(between_counted.c.credit_tx_id))


def counted_lines() -> Subquery:
    """Chaque ligne comptée, catégorisée ou non : mois, montant, catégorie racine, chemin
    (« Parent › Enfant »), catégorie (nulle si non catégorisée), transaction, date."""
    effective = and_(
        LedgerTransaction.account_id.in_(counted_accounts()),
        is_effective(),
        LedgerTransaction.id.not_in(internal_transfer_legs()),
    )
    parent = Category.__table__.alias("parent")
    active = and_(
        Categorization.transaction_id == LedgerTransaction.id,
        Categorization.superseded_at.is_(None),
    )
    categorized = (
        select(
            _month(LedgerTransaction.booking_date).label("month"),
            Categorization.amount_cents.label("amount"),
            func.coalesce(parent.c.name, Category.name).label("root"),
            func.coalesce(parent.c.name + " › " + Category.name, Category.name).label("path"),
            Category.id.label("category_id"),
            LedgerTransaction.id.label("transaction_id"),
            LedgerTransaction.booking_date.label("booking_date"),
        )
        .select_from(LedgerTransaction)
        .join(Categorization, active)
        .join(Category, Category.id == Categorization.category_id)
        .outerjoin(parent, parent.c.id == Category.parent_id)
        .where(effective, Category.kind.not_in(EXCLUDED_KINDS))
    )
    uncategorized = select(
        _month(LedgerTransaction.booking_date).label("month"),
        LedgerTransaction.amount_cents.label("amount"),
        literal(UNCATEGORIZED).label("root"),
        literal(UNCATEGORIZED).label("path"),
        literal(None).label("category_id"),
        LedgerTransaction.id.label("transaction_id"),
        LedgerTransaction.booking_date.label("booking_date"),
    ).where(effective, ~exists().where(active))
    return union_all(categorized, uncategorized).subquery()


_counted_lines = counted_lines


def monthly_flows(engine: Engine, first_month: str, last_month: str) -> list[MonthFlow]:
    lines = _counted_lines()
    query = (
        select(
            lines.c.month,
            func.sum(case((lines.c.amount > 0, lines.c.amount), else_=0)),
            func.sum(case((lines.c.amount < 0, lines.c.amount), else_=0)),
        )
        .where(lines.c.month >= first_month, lines.c.month <= last_month)
        .group_by(lines.c.month)
    )
    with engine.connect() as connection:
        found = {
            month: (int(income), int(expenses))
            for month, income, expenses in connection.execute(query)
        }
    return [
        MonthFlow(month, *found.get(month, (0, 0)))
        for month in month_range(first_month, last_month)
    ]


def expenses_by_category(engine: Engine, month: str) -> list[tuple[str, int]]:
    """Dépenses du mois par catégorie racine, de la plus forte à la plus faible (négatives)."""
    lines = _counted_lines()
    query = (
        select(lines.c.root, func.sum(lines.c.amount))
        .where(lines.c.month == month, lines.c.amount < 0)
        .group_by(lines.c.root)
        .order_by(func.sum(lines.c.amount))
    )
    with engine.connect() as connection:
        return [(root, int(total)) for root, total in connection.execute(query)]


def net_worth(engine: Engine) -> NetWorth:
    """Somme des soldes de référence (comptable, sinon prévisionnel) des comptes comptés."""
    balance = reference_balances()
    query = (
        select(Account.name, balance.c.amount_cents)
        .outerjoin(balance, balance.c.account_id == Account.id)
        .where(
            Account.include_in_net_worth.is_(True),
            Account.ignored_at.is_(None),
            Account.closed_at.is_(None),
        )
    )
    with engine.connect() as connection:
        rows = connection.execute(query).all()
    return NetWorth(
        total=sum(amount for _, amount in rows if amount is not None),
        accounts_without_balance=sorted(name for name, amount in rows if amount is None),
    )
