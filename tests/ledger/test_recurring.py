"""Récurrents (ANA-07) et anomalies (ANA-08) : domaine pur."""

from datetime import date, timedelta

from hypothesis import given
from hypothesis import strategies as st

from app.ledger.recurring import (
    Operation,
    Series,
    anomalies,
    detect,
    merchant_key,
)

_ids = iter(range(1, 10_000_000))


def op(day: date, amount: int, key: str = "NETFLIX", label: str = "") -> Operation:
    return Operation(next(_ids), day, amount, key, 1, label or key)


def monthly(key: str, amounts: list[int], start: date = date(2026, 1, 15)) -> list[Operation]:
    return [op(start + timedelta(days=30 * k), a, key) for k, a in enumerate(amounts)]


def only(series: list[Series], key: str) -> Series:
    [found] = [s for s in series if s.key == key]
    return found


def test_merchant_key_drops_dates_and_references() -> None:
    assert merchant_key("Prlv Netflix 15/09 REF123") == "PRLV NETFLIX"


def test_monthly_subscription_with_jitter() -> None:
    operations = [
        op(date(2026, 6, 15), -1399),
        op(date(2026, 7, 17), -1399),  # week-end : deux jours plus tard
        op(date(2026, 8, 14), -1399),
        op(date(2026, 9, 15), -1399),
    ]
    series = only(detect(operations), "NETFLIX")
    assert (series.frequency.label, series.current_cents, series.previous_cents) == (
        "mensuelle",
        -1399,
        None,
    )
    assert series.annual_cents == -1399 * 12
    assert series.monthly_cents == -1399
    assert series.next_date == date(2026, 10, 15)
    assert series.active(date(2026, 10, 20))  # tolérance de 5 jours
    assert not series.active(date(2026, 10, 21))
    assert series.missed(date(2026, 10, 21))
    assert not series.missed(date(2026, 12, 1))  # disparue depuis longtemps


def test_price_increase_is_tolerated_and_reported() -> None:
    series = only(detect(monthly("SPOTIFY", [-1099, -1099, -1099, -1299, -1299])), "SPOTIFY")
    assert (series.current_cents, series.previous_cents) == (-1299, -1099)
    assert series.change_date == date(2026, 1, 15) + timedelta(days=90)


def test_latest_charge_alone_at_a_new_price() -> None:
    series = only(detect(monthly("BOX", [-2999, -2999, -2999, -3499])), "BOX")
    assert (series.current_cents, series.previous_cents) == (-3499, -2999)


def test_irregular_or_unstable_series_are_ignored() -> None:
    assert detect(monthly("A", [-1000, -1000])) == []  # trop peu
    assert detect(monthly("B", [-1000, -3000, -1000, -3000])) == []  # montants instables
    assert detect(monthly("C", [-1000, -1000, -1000, -5000])) == []  # saut de plus de 50 %
    gaps = [op(date(2026, 1, 1), -500, "D"), op(date(2026, 1, 9), -500, "D")]
    gaps.append(op(date(2026, 3, 1), -500, "D"))
    assert detect(gaps) == []  # intervalles irréguliers


def test_credits_and_other_frequencies() -> None:
    salary = monthly("SALAIRE", [250000, 250000, 251000])
    weekly = [op(date(2026, 9, 1) + timedelta(days=7 * k), -800, "MARCHE") for k in range(4)]
    yearly = [op(date(2023 + k, 3, 1), -12000, "ASSURANCE") for k in range(3)]
    found = {s.key: s for s in detect(salary + weekly + yearly)}
    assert found["SALAIRE"].current_cents == 250000 and not found["SALAIRE"].debit
    assert found["MARCHE"].frequency.label == "hebdomadaire"
    assert found["ASSURANCE"].frequency.label == "annuelle"
    assert found["ASSURANCE"].monthly_cents == -1000
    assert found["SALAIRE"].monthly_cents == 250000


@given(
    amount=st.integers(min_value=1, max_value=10**7),
    count=st.integers(min_value=3, max_value=24),
    offset=st.integers(min_value=0, max_value=3000),
    debit=st.booleans(),
)
def test_perfect_monthly_series_is_always_detected(
    amount: int, count: int, offset: int, debit: bool
) -> None:
    signed = -amount if debit else amount
    start = date(2020, 1, 1) + timedelta(days=offset)
    [series] = detect(monthly("X", [signed] * count, start))
    assert series.current_cents == signed
    assert series.annual_cents == signed * 12
    assert len(series.operations) == count


def test_anomalies() -> None:
    since = date(2026, 9, 1)
    history = [op(date(2026, m, 2), -4500, "SALLE DE SPORT") for m in (6, 7, 8)]
    unusual = op(date(2026, 9, 2), -15000, "SALLE DE SPORT")
    fee = op(date(2026, 9, 5), -890, "FRAIS TENUE DE COMPTE")
    new = op(date(2026, 9, 7), -25000, "BIJOUTERIE")
    small_new = op(date(2026, 9, 7), -2500, "LIBRAIRIE")
    twice = [op(date(2026, 9, 9), -1200, "BOULANGERIE") for _ in range(2)]
    old_fee = op(date(2026, 8, 5), -890, "FRAIS TENUE DE COMPTE")
    found = anomalies([*history, unusual, fee, new, small_new, *twice, old_fee], since)
    assert [(a.kind, a.operation.id) for a in found] == [
        ("montant_inhabituel", unusual.id),
        ("frais", fee.id),
        ("nouveau_crediteur", new.id),
        ("doublon", twice[1].id),
    ]
    assert found[0].usual_cents == 4500
