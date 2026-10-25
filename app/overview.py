"""Vue d'ensemble d'un mois (ANA-09, LOC-05) : flux des 12 derniers mois, comparaison avec le même
mois un an plus tôt, dépenses par catégorie, patrimoine et loyers du mois. Partagée par le
tableau de bord web et l'API de l'application Android."""

from dataclasses import dataclass
from datetime import date

from sqlalchemy import Engine

from app.ledger.aggregates import (
    MonthFlow,
    NetWorth,
    expenses_by_category,
    monthly_flows,
    net_worth,
)
from app.months import shift_month
from app.rental.status import RentState
from app.rental.views import lease_views

HISTORY_MONTHS = 12


@dataclass(frozen=True)
class RentOfMonth:
    lease: str
    state: RentState
    remaining_cents: int


@dataclass(frozen=True)
class MonthOverview:
    month: str
    flows: list[MonthFlow]  # 12 mois, le dernier est `month`
    previous_year: MonthFlow
    categories: list[tuple[str, int]]
    worth: NetWorth
    rents: list[RentOfMonth]

    @property
    def current(self) -> MonthFlow:
        return self.flows[-1]


def month_overview(engine: Engine, month: str, today: date) -> MonthOverview:
    flows = monthly_flows(engine, shift_month(month, 1 - HISTORY_MONTHS), month)
    year_ago = shift_month(month, -HISTORY_MONTHS)
    [previous_year] = monthly_flows(engine, year_ago, year_ago)
    rents = [
        RentOfMonth(lease.label, due.state, due.remaining_cents)
        for lease in lease_views(engine, today)
        for due in lease.dues
        if due.period == month
    ]
    return MonthOverview(
        month=month,
        flows=flows,
        previous_year=previous_year,
        categories=expenses_by_category(engine, month),
        worth=net_worth(engine),
        rents=rents,
    )
