"""Opérations récurrentes (ANA-07) et anomalies (ANA-08) : domaine pur, sans base ni réseau.

Entrée : des opérations déjà filtrées par l'appelant (transactions effectives, hors cartes qui
répètent le compte et hors virements internes), triées par date, avec une **clé de
regroupement** (libellé sans les mots contenant des chiffres : dates, références).

Récurrents : au moins 3 opérations de même clé et de même sens, à intervalle régulier (hebdo,
mensuel, trimestriel, annuel, à `max(5 j, période/6)` près), montant stable à 10 % près. Une
**hausse ou baisse de prix** est admise : la série peut changer une fois de niveau (deux paliers
stables, écart d'au plus 50 %). Médianes basses : jamais de flottant (inv. 2).
"""

import itertools
import re
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

MIN_OCCURRENCES = 3
_DIGIT_WORD = re.compile(r"\S*\d\S*")


def merchant_key(label: str) -> str:
    """Clé de regroupement : libellé sans les mots contenant des chiffres."""
    return " ".join(_DIGIT_WORD.sub(" ", label).split()).upper()


@dataclass(frozen=True)
class Operation:
    id: int
    booking_date: date
    amount_cents: int
    key: str  # clé de regroupement (commerçant)
    account_id: int
    label: str = ""  # libellé nettoyé (frais bancaires)


@dataclass(frozen=True)
class Frequency:
    days: int
    label: str
    per_year: int


FREQUENCIES = (
    Frequency(7, "hebdomadaire", 52),
    Frequency(30, "mensuelle", 12),
    Frequency(91, "trimestrielle", 4),
    Frequency(365, "annuelle", 1),
)


@dataclass(frozen=True)
class Series:
    key: str
    frequency: Frequency
    operations: tuple[Operation, ...]
    current_cents: int  # montant habituel actuel (signé : négatif pour une dépense)
    previous_cents: int | None  # montant avant le changement de prix, s'il y en a eu un
    change_date: date | None  # première opération au nouveau prix

    @property
    def debit(self) -> bool:
        return self.current_cents < 0

    @property
    def first(self) -> Operation:
        return self.operations[0]

    @property
    def last(self) -> Operation:
        return self.operations[-1]

    @property
    def tolerance(self) -> timedelta:
        return timedelta(days=max(5, self.frequency.days // 6))

    @property
    def next_date(self) -> date:
        return self.last.booking_date + timedelta(days=self.frequency.days)

    @property
    def annual_cents(self) -> int:
        return self.current_cents * self.frequency.per_year

    @property
    def monthly_cents(self) -> int:
        """Équivalent mensuel, arrondi vers zéro."""
        annual = self.annual_cents
        return -(-annual // 12) if annual < 0 else annual // 12

    def active(self, today: date) -> bool:
        """Encore attendue : la prochaine date (avec tolérance) n'est pas dépassée."""
        return self.next_date + self.tolerance >= today

    def missed(self, today: date) -> bool:
        """Disparue récemment : une seule échéance manquée (pas une série ancienne)."""
        late = today - (self.next_date + self.tolerance)
        return timedelta(0) < late <= timedelta(days=self.frequency.days)


def _stable(amounts: Sequence[int]) -> int | None:
    """Médiane basse si tous les montants sont à 10 % près de celle-ci, sinon None."""
    reference = statistics.median_low(amounts)
    if all(abs(a - reference) * 10 <= reference for a in amounts):
        return reference
    return None


def _levels(amounts: Sequence[int]) -> tuple[int, int | None, int] | None:
    """(montant actuel, montant précédent ou None, rang du changement) ; None si instable."""
    whole = _stable(amounts)
    if whole is not None:
        return whole, None, 0
    for cut in range(len(amounts) - 1, 1, -1):  # le changement le plus récent d'abord
        before, after = _stable(amounts[:cut]), _stable(amounts[cut:])
        if before is None or after is None:
            continue
        if abs(after - before) * 2 <= before:  # écart d'au plus 50 %
            return after, before, cut
    return None


def _frequency(operations: Sequence[Operation]) -> Frequency | None:
    gaps = [(b.booking_date - a.booking_date).days for a, b in itertools.pairwise(operations)]
    median_gap = statistics.median_low(gaps)
    frequency = min(FREQUENCIES, key=lambda f: abs(f.days - median_gap))
    tolerance = max(5, frequency.days // 6)
    if any(abs(gap - frequency.days) > tolerance for gap in gaps):
        return None
    return frequency


def detect(operations: Iterable[Operation]) -> list[Series]:
    """Séries récurrentes, actives ou non (l'appelant juge avec `active` et `missed`)."""
    groups: dict[tuple[str, bool], list[Operation]] = defaultdict(list)
    for operation in sorted(operations, key=lambda o: (o.booking_date, o.id)):
        if operation.amount_cents != 0 and operation.key:
            groups[(operation.key, operation.amount_cents < 0)].append(operation)
    found = []
    for (key, debit), items in groups.items():
        if len(items) < MIN_OCCURRENCES:
            continue
        frequency = _frequency(items)
        levels = _levels([abs(o.amount_cents) for o in items])
        if frequency is None or levels is None:
            continue
        current, previous, cut = levels
        sign = -1 if debit else 1
        found.append(
            Series(
                key=key,
                frequency=frequency,
                operations=tuple(items),
                current_cents=sign * current,
                previous_cents=sign * previous if previous is not None else None,
                change_date=items[cut].booking_date if previous is not None else None,
            )
        )
    return sorted(found, key=lambda s: (s.annual_cents, s.key))


# --- Anomalies (ANA-08) --------------------------------------------------------------------------

FEE_WORDS = ("FRAIS", "COMMISSION", "COTIS", "AGIOS", "INTERETS DEBITEURS")
UNUSUAL_MIN_GAP_CENTS = 2000  # au moins 20 € au-dessus de l'habitude
NEW_CREDITOR_MIN_CENTS = 10000  # nouveau commerçant : 100 € ou plus


@dataclass(frozen=True)
class Anomaly:
    kind: str  # « frais », « montant_inhabituel », « nouveau_crediteur », « doublon »
    operation: Operation
    usual_cents: int | None = None  # montant habituel (montant inhabituel)


def anomalies(operations: Iterable[Operation], since: date) -> list[Anomaly]:
    """Anomalies des opérations datées de `since` ou après, au regard de l'historique fourni
    (un an conseillé) : frais bancaires, montant inhabituel (plus de 1,5 fois la médiane et au
    moins 20 € d'écart, 3 opérations passées au moins), nouveau commerçant (100 € ou plus),
    doublon possible (même commerçant, même montant, même jour)."""
    history: dict[str, list[int]] = defaultdict(list)
    same_day: Counter[tuple[str, int, date]] = Counter()
    found = []
    for operation in sorted(operations, key=lambda o: (o.booking_date, o.id)):
        past = history[operation.key]
        if operation.booking_date >= since:
            amount = abs(operation.amount_cents)
            label = operation.label.upper()
            if operation.amount_cents < 0 and any(word in label for word in FEE_WORDS):
                found.append(Anomaly("frais", operation))
            elif len(past) >= MIN_OCCURRENCES:
                usual = statistics.median_low([abs(a) for a in past])
                if amount * 2 > usual * 3 and amount - usual > UNUSUAL_MIN_GAP_CENTS:
                    found.append(Anomaly("montant_inhabituel", operation, usual))
            elif not past and operation.amount_cents <= -NEW_CREDITOR_MIN_CENTS:
                found.append(Anomaly("nouveau_crediteur", operation))
            day_key = (operation.key, operation.amount_cents, operation.booking_date)
            same_day[day_key] += 1
            if same_day[day_key] == 2:  # noqa: PLR2004 (deuxième occurrence)
                found.append(Anomaly("doublon", operation))
        past.append(operation.amount_cents)
    return found
