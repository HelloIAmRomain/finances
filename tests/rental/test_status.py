"""État d'une échéance, dérivé des allocations confirmées et de la date (regles-loyer.md, Q16)."""

from datetime import date

from hypothesis import given
from hypothesis import strategies as st

from app.rental.status import DueTerms, RentState, compute_status

SEPTEMBER = DueTerms(period="2026-09", amount=85000, alert_day=5, unpaid_day=10)


def _state(paid: list[int], day: date) -> RentState:
    return compute_status(SEPTEMBER, paid, day).state


def test_nothing_received() -> None:
    assert _state([], date(2026, 8, 28)) is RentState.ATTENDU  # paiement anticipé possible
    assert _state([], date(2026, 9, 4)) is RentState.ATTENDU
    assert _state([], date(2026, 9, 5)) is RentState.EN_RETARD  # alerte 1 le 5
    assert _state([], date(2026, 9, 9)) is RentState.EN_RETARD
    assert _state([], date(2026, 9, 10)) is RentState.IMPAYE  # alerte 2 le 10
    assert _state([], date(2026, 11, 1)) is RentState.IMPAYE


def test_partial_payment() -> None:
    status = compute_status(SEPTEMBER, [50000], date(2026, 9, 2))
    assert (status.state, status.paid, status.remaining) == (RentState.PARTIEL, 50000, 35000)
    assert _state([50000], date(2026, 9, 5)) is RentState.EN_RETARD
    assert _state([50000], date(2026, 9, 10)) is RentState.IMPAYE


def test_full_payment_closes_the_due_whatever_the_date() -> None:
    for day in (date(2026, 8, 27), date(2026, 9, 3), date(2026, 9, 12)):
        status = compute_status(SEPTEMBER, [85000], day)
        assert (status.state, status.remaining) == (RentState.PAYE, 0)


def test_two_partial_transfers_add_up() -> None:
    assert _state([50000, 35000], date(2026, 9, 7)) is RentState.PAYE


def test_thresholds_are_per_lease() -> None:
    lenient = DueTerms(period="2026-09", amount=85000, alert_day=8, unpaid_day=20)
    assert compute_status(lenient, [], date(2026, 9, 7)).state is RentState.ATTENDU
    assert compute_status(lenient, [], date(2026, 9, 19)).state is RentState.EN_RETARD


@given(
    paid=st.lists(st.integers(min_value=1, max_value=100000), max_size=4),
    day=st.dates(min_value=date(2026, 8, 1), max_value=date(2026, 12, 31)),
)
def test_properties(paid: list[int], day: date) -> None:
    status = compute_status(SEPTEMBER, paid, day)
    total = sum(paid)
    assert status.paid == total
    assert status.remaining == max(SEPTEMBER.amount - total, 0)
    assert (status.state is RentState.PAYE) == (total >= SEPTEMBER.amount)
    if status.state is not RentState.PAYE:
        # Le temps ne fait qu'aggraver l'état tant que rien n'est reçu en plus.
        order = [RentState.ATTENDU, RentState.PARTIEL, RentState.EN_RETARD, RentState.IMPAYE]
        later = compute_status(SEPTEMBER, paid, date(2027, 1, 1)).state
        assert order.index(later) >= order.index(status.state)
        assert later is RentState.IMPAYE
