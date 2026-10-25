"""Mois « AAAA-MM » : un seul jeu d'outils (app/months.py)."""

from datetime import date, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.months import (
    add_months,
    is_month,
    month_end,
    month_label,
    month_of,
    month_range,
    month_start,
    months_between,
    next_month,
    shift_month,
)

days = st.dates(min_value=date(1900, 1, 1), max_value=date(2200, 12, 31))


@pytest.mark.parametrize(
    ("text", "valid"),
    [("2026-10", True), ("2026-13", False), ("2026-00", False), ("2026-1", False), ("26-10", False),
     ("2026-10-01", False), ("", False)],
)  # fmt: skip
def test_is_month(text: str, valid: bool) -> None:
    assert is_month(text) is valid


def test_shift_and_range_cross_years() -> None:
    assert shift_month("2026-01", -1) == "2025-12"
    assert shift_month("2025-12", 1) == next_month("2025-12") == "2026-01"
    assert shift_month("2026-03", -26) == "2024-01"
    assert month_range("2025-11", "2026-02") == ["2025-11", "2025-12", "2026-01", "2026-02"]
    assert month_range("2026-02", "2026-01") == []
    assert months_between("2025-11", "2026-02") == 4
    assert months_between("2026-02", "2026-01") == 0


def test_bounds_and_labels() -> None:
    assert month_start("2024-02") == date(2024, 2, 1)
    assert month_end("2024-02") == date(2024, 2, 29)  # bissextile
    assert month_end("2026-12") == date(2026, 12, 31)
    assert month_label("2026-08") == "août 2026"


def test_add_months_clamps_to_month_end() -> None:
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)
    assert add_months(date(2026, 3, 15), -14) == date(2025, 1, 15)


@given(days, st.integers(min_value=-600, max_value=600))
def test_shift_is_consistent(day: date, delta: int) -> None:
    month = month_of(day)
    assert is_month(month)
    shifted = shift_month(month, delta)
    assert shift_month(shifted, -delta) == month
    assert month_of(add_months(day, delta)) == shifted
    assert month_start(month) <= day <= month_end(month)
    assert month_end(month) + timedelta(days=1) == month_start(next_month(month))
