"""État d'une échéance (regles-loyer.md) : fonction pure des montants confirmés et de la date.

- `PAYE` dès que les allocations confirmées couvrent le montant dû, quelle que soit la date ;
- sinon, à partir du jour d'impayé (le 10 par défaut) : `IMPAYE` ;
- sinon, à partir du jour d'alerte (le 5 par défaut) : `EN_RETARD` ;
- sinon `PARTIEL` si quelque chose a été reçu, `ATTENDU` sinon.

Les jours d'alerte et d'impayé se lisent dans le mois de l'échéance (Q16), réglables par bail.
Les allocations proposées (à valider) n'entrent pas dans le calcul.
"""

import enum
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date


class RentState(enum.StrEnum):
    ATTENDU = "attendu"
    PARTIEL = "partiel"
    EN_RETARD = "en_retard"
    IMPAYE = "impaye"
    PAYE = "paye"


@dataclass(frozen=True)
class DueTerms:
    period: str  # « AAAA-MM »
    amount: int  # centimes
    alert_day: int
    unpaid_day: int

    def day(self, number: int) -> date:
        year, month = map(int, self.period.split("-"))
        return date(year, month, number)

    @property
    def alert_date(self) -> date:
        return self.day(self.alert_day)

    @property
    def unpaid_date(self) -> date:
        return self.day(self.unpaid_day)


@dataclass(frozen=True)
class DueStatus:
    state: RentState
    paid: int
    remaining: int


def compute_status(terms: DueTerms, confirmed: Iterable[int], today: date) -> DueStatus:
    paid = sum(confirmed)
    remaining = max(terms.amount - paid, 0)
    if remaining == 0:
        state = RentState.PAYE
    elif today >= terms.unpaid_date:
        state = RentState.IMPAYE
    elif today >= terms.alert_date:
        state = RentState.EN_RETARD
    else:
        state = RentState.PARTIEL if paid > 0 else RentState.ATTENDU
    return DueStatus(state, paid, remaining)
