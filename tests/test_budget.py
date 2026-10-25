"""Budget DSP2 (inv. 10, Q14) : plafond par compte, endpoint et 24 h, selon le profil de la banque,
synchros manuelles comprises."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import Engine, delete, insert, select, text

import app.models  # noqa: F401
from app.connectors.bank_profiles import DEFAULT_LIMIT, configure_profiles, profile_for
from app.connectors.budget import (
    WINDOW,
    BudgetExceededError,
    ManualSyncBudgetError,
    ManualSyncTooSoonError,
    acquire,
    check_manual_sync,
    manual_sync_blocker,
    observed_limits,
    record_response,
    remaining,
    reserve_after_restore,
)
from app.db import Base, make_engine
from app.models import Connection, ProviderCall
from app.models.ledger import ConnectionStatus, Provider

T0 = datetime(2026, 9, 27, 6, 0, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    engine = make_engine(tmp_path / "budget.db")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        for account_id in (1, 2):
            connection.execute(
                text(
                    "INSERT INTO account (id, name, type, currency, include_in_net_worth,"
                    " syncs_per_day) VALUES (:id, 'Compte', 'checking', 'EUR', 1, 4)"
                ),
                {"id": account_id},
            )
    return engine


def _auto(engine: Engine, when: datetime, account: int = 1, endpoint: str = "transactions") -> int:
    return acquire(
        engine, account_id=account, endpoint=endpoint, unattended=True, method="GET", now=when
    )


def test_four_unattended_calls_then_refused(engine: Engine) -> None:
    for hour in range(4):
        _auto(engine, T0 + timedelta(hours=hour))
    with pytest.raises(BudgetExceededError):
        _auto(engine, T0 + timedelta(hours=5))


def test_budget_is_per_account_and_endpoint(engine: Engine) -> None:
    for hour in range(4):
        _auto(engine, T0 + timedelta(hours=hour))
    _auto(engine, T0 + timedelta(hours=5), endpoint="balances")
    _auto(engine, T0 + timedelta(hours=5), account=2)


def test_window_is_rolling(engine: Engine) -> None:
    for hour in range(4):
        _auto(engine, T0 + timedelta(hours=hour))
    with pytest.raises(BudgetExceededError):
        _auto(engine, T0 + WINDOW - timedelta(seconds=1))
    _auto(engine, T0 + WINDOW + timedelta(seconds=1))  # le premier appel est sorti de la fenêtre


def test_refused_call_is_not_recorded(engine: Engine) -> None:
    for hour in range(4):
        _auto(engine, T0 + timedelta(hours=hour))
    with pytest.raises(BudgetExceededError):
        _auto(engine, T0 + timedelta(hours=5))
    with engine.connect() as connection:
        assert len(connection.execute(select(ProviderCall.id)).all()) == 4


def test_remaining(engine: Engine) -> None:
    assert remaining(engine, account_id=1, endpoint="transactions", now=T0) == DEFAULT_LIMIT
    _auto(engine, T0)
    assert remaining(engine, account_id=1, endpoint="transactions", now=T0) == DEFAULT_LIMIT - 1


def _manual(engine: Engine, when: datetime, endpoint: str = "transactions") -> int:
    return acquire(
        engine, account_id=1, endpoint=endpoint, unattended=False, method="GET", now=when
    )


def test_manual_calls_consume_the_same_budget(engine: Engine) -> None:
    """Derrière Tailscale, la banque ne distingue pas une synchro manuelle : elle compte."""
    _manual(engine, T0)
    _manual(engine, T0 + timedelta(minutes=20))
    _auto(engine, T0 + timedelta(hours=1))
    _auto(engine, T0 + timedelta(hours=2))
    with pytest.raises(BudgetExceededError):
        _auto(engine, T0 + timedelta(hours=3))
    with pytest.raises(BudgetExceededError):
        _manual(engine, T0 + timedelta(hours=3))


def test_manual_sync_refused_when_only_one_automatic_sync_remains(engine: Engine) -> None:
    for endpoint in ("balances", "transactions"):
        _auto(engine, T0, endpoint=endpoint)
        _auto(engine, T0 + timedelta(hours=5), endpoint=endpoint)
    later = T0 + timedelta(hours=6)
    assert manual_sync_blocker(engine, account_id=1, now=later) is None  # 2 restantes
    _auto(engine, T0 + timedelta(hours=5, minutes=30))  # 1 seule restante sur transactions
    refusal = manual_sync_blocker(engine, account_id=1, now=later)
    assert isinstance(refusal, ManualSyncBudgetError)
    assert "08:00" in str(refusal)  # le premier appel (06:00 UTC) sort de la fenêtre à 08:00 Paris
    with pytest.raises(ManualSyncBudgetError):
        check_manual_sync(engine, account_id=1, now=later)
    assert manual_sync_blocker(engine, account_id=1, now=T0 + WINDOW + timedelta(seconds=1)) is None
    assert manual_sync_blocker(engine, account_id=2, now=later) is None  # autre compte


def test_manual_sync_accounts_for_paginated_syncs(engine: Engine) -> None:
    """Une synchro de 2 pages en coûte 2 : avec 2 requêtes restantes, la manuelle ne laisserait
    rien au créneau automatique suivant."""
    _auto(engine, T0, endpoint="balances")
    _auto(engine, T0, endpoint="transactions")
    _auto(engine, T0 + timedelta(seconds=2), endpoint="transactions")
    refusal = manual_sync_blocker(engine, account_id=1, now=T0 + timedelta(hours=1))
    assert isinstance(refusal, ManualSyncBudgetError)


def test_bank_profiles_set_the_limit(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            insert(Connection).values(
                id=1,
                provider=Provider.ENABLE_BANKING,
                institution_name="Société Générale",
                country="FR",
                status=ConnectionStatus.ACTIVE,
                created_at=T0,
                updated_at=T0,
            )
        )
        connection.execute(text("UPDATE account SET connection_id = 1 WHERE id = 1"))
    assert profile_for("SOCIETE GENERALE").name == profile_for("Société Générale").name
    assert profile_for("BoursoBank").name == "boursorama"
    try:
        configure_profiles({"Société Générale": {"transactions": 2}})
        _auto(engine, T0)
        _auto(engine, T0 + timedelta(hours=1))
        with pytest.raises(BudgetExceededError):
            _auto(engine, T0 + timedelta(hours=2))
        assert remaining(engine, account_id=1, endpoint="balances", now=T0) == DEFAULT_LIMIT
        _auto(engine, T0 + timedelta(hours=2), account=2)  # banque inconnue : profil par défaut
        with pytest.raises(ValueError, match="invalide"):
            configure_profiles({"Boursorama": {"transactions": 0}})
    finally:
        configure_profiles({})


def test_observed_limits_are_recorded_per_bank(engine: Engine) -> None:
    _auto(engine, T0)
    refused = _auto(engine, T0 + timedelta(hours=2))
    record_response(engine, refused, 429)
    _auto(engine, T0 + timedelta(hours=3), account=2)
    [limit] = observed_limits(engine)
    assert (limit.account_id, limit.endpoint, limit.calls_in_window) == (1, "transactions", 2)
    assert limit.bank == "inconnue" and limit.at == T0 + timedelta(hours=2)
    assert observed_limits(engine, since=T0 + timedelta(hours=3)) == []


def test_manual_sync_limited_to_one_per_quarter_hour(engine: Engine) -> None:
    check_manual_sync(engine, account_id=1, now=T0)
    acquire(engine, account_id=1, endpoint="balances", unattended=False, method="GET", now=T0)
    with pytest.raises(ManualSyncTooSoonError):
        check_manual_sync(engine, account_id=1, now=T0 + timedelta(minutes=14))
    check_manual_sync(engine, account_id=1, now=T0 + timedelta(minutes=15))
    check_manual_sync(engine, account_id=2, now=T0)  # autre compte : indépendant


def test_reserve_after_restore_consumes_one_call(engine: Engine) -> None:
    """Après une restauration, les requêtes des 15 dernières minutes sont inconnues : on en
    réserve une par compte et endpoint (au plus un créneau tombe dans la fenêtre perdue)."""
    for hour in range(3):
        _auto(engine, T0 + timedelta(hours=hour))
    reserve_after_restore(
        engine,
        account_ids=[1, 2],
        endpoints=["transactions", "balances"],
        now=T0 + timedelta(hours=3),
    )
    with pytest.raises(BudgetExceededError):
        _auto(engine, T0 + timedelta(hours=4))
    assert remaining(engine, account_id=2, endpoint="balances", now=T0 + timedelta(hours=4)) == 3


events = st.lists(
    st.tuples(
        st.integers(min_value=0, max_value=72 * 60),  # minute de la tentative sur 3 jours
        st.sampled_from([1, 2]),
        st.sampled_from(["transactions", "balances"]),
        st.booleans(),  # automatique ou manuelle
    ),
    max_size=80,
)


@settings(max_examples=60, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(events=events)
def test_never_more_than_four_unattended_in_any_window(
    engine: Engine, events: list[tuple[int, int, str, bool]]
) -> None:
    """Quelle que soit la suite de tentatives (reprises, rattrapages, synchros manuelles),
    aucune fenêtre de 24 h ne contient plus de 4 requêtes par compte et endpoint."""
    with engine.begin() as connection:
        connection.execute(delete(ProviderCall))
    accepted: list[tuple[datetime, int, str]] = []
    for minute, account, endpoint, unattended in sorted(events):
        when = T0 + timedelta(minutes=minute)
        try:
            acquire(
                engine,
                account_id=account,
                endpoint=endpoint,
                unattended=unattended,
                method="GET",
                now=when,
            )
        except BudgetExceededError:
            continue
        accepted.append((when, account, endpoint))
    for start, account, endpoint in accepted:
        in_window = [
            call
            for call in accepted
            if call[1:] == (account, endpoint) and start <= call[0] < start + WINDOW
        ]
        assert len(in_window) <= DEFAULT_LIMIT


def test_record_response(engine: Engine) -> None:
    call_id = _auto(engine, T0)
    record_response(engine, call_id, 429)
    with engine.connect() as connection:
        status = connection.execute(
            select(ProviderCall.http_status).where(ProviderCall.id == call_id)
        ).scalar_one()
    assert status == 429


def test_a_call_leaves_the_window_exactly_24_hours_later(engine: Engine) -> None:
    for hour in range(4):
        _auto(engine, T0 + timedelta(hours=hour))
    assert remaining(engine, account_id=1, endpoint="transactions", now=T0 + WINDOW) == 1
    _auto(engine, T0 + WINDOW)  # l'appel de T0 n'est plus compté à T0 + 24 h pile


def test_remaining_never_negative_after_a_restore_reservation(engine: Engine) -> None:
    for hour in range(4):
        _auto(engine, T0 + timedelta(hours=hour))
    reserve_after_restore(
        engine, account_ids=[1], endpoints=["transactions"], now=T0 + timedelta(hours=4)
    )
    assert (
        remaining(engine, account_id=1, endpoint="transactions", now=T0 + timedelta(hours=5)) == 0
    )
    # La réservation est automatique : elle ne compte pas comme une mise à jour manuelle récente.
    assert not isinstance(
        manual_sync_blocker(engine, account_id=2, now=T0 + timedelta(hours=4)),
        ManualSyncTooSoonError,
    )


@pytest.mark.parametrize(
    ("elapsed", "shown"),
    [(timedelta(minutes=10), "5 min"), (timedelta(minutes=10, seconds=30), "5 min"),
     (timedelta(minutes=13, seconds=59), "2 min"), (timedelta(minutes=14, seconds=59), "1 min")],
)  # fmt: skip
def test_manual_sync_wait_is_rounded_up_to_the_minute(
    engine: Engine, elapsed: timedelta, shown: str
) -> None:
    _manual(engine, T0)
    refusal = manual_sync_blocker(engine, account_id=1, now=T0 + elapsed)
    assert isinstance(refusal, ManualSyncTooSoonError) and f"dans {shown}." in str(refusal)


def test_bank_too_strict_for_any_manual_sync(engine: Engine) -> None:
    """Une synchro de 3 pages, plafond de 4 : jamais de quoi faire une manuelle et la suivante."""
    for seconds in (0, 20, 40):
        _auto(engine, T0 + timedelta(seconds=seconds))
    refusal = manual_sync_blocker(engine, account_id=1, now=T0 + timedelta(hours=1))
    assert isinstance(refusal, ManualSyncBudgetError)
    assert "n'accepte pas assez d'accès" in str(refusal)


def test_bank_allowing_exactly_two_syncs_gives_a_time(engine: Engine) -> None:
    """Synchro de 2 pages, plafond de 4 : une manuelle redevient possible une fois la synchro
    sortie de la fenêtre ; on annonce donc une heure, pas un refus définitif."""
    _auto(engine, T0)
    _auto(engine, T0 + timedelta(seconds=20))
    refusal = manual_sync_blocker(engine, account_id=1, now=T0 + timedelta(hours=1))
    assert isinstance(refusal, ManualSyncBudgetError)
    assert "Nouvel essai possible après 08:00." in str(refusal)  # T0 + 24 h, heure de Paris


@pytest.mark.parametrize("gap", [timedelta(minutes=5), timedelta(minutes=5, seconds=30)])
def test_requests_five_minutes_apart_or_more_are_separate_syncs(
    engine: Engine, gap: timedelta
) -> None:
    _auto(engine, T0, endpoint="balances")
    _auto(engine, T0, endpoint="transactions")
    _auto(engine, T0 + gap, endpoint="transactions")
    # Deux synchros d'une page chacune : il reste 2 requêtes, une manuelle et une automatique.
    assert manual_sync_blocker(engine, account_id=1, now=T0 + timedelta(hours=1)) is None


def test_observed_limit_counts_only_the_24_hours_before_the_refusal(
    engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    refused_at = T0 + WINDOW
    _auto(engine, T0)  # exactement 24 h avant : hors fenêtre
    _auto(engine, T0 + timedelta(hours=1))
    reserve_after_restore(
        engine, account_ids=[1], endpoints=["transactions"], now=T0 + timedelta(hours=2)
    )
    other = _auto(engine, T0 + timedelta(hours=3))
    refused = _auto(engine, refused_at)
    record_response(engine, refused, 429)
    record_response(engine, other, 200)
    [limit] = observed_limits(engine, since=refused_at)
    assert limit.calls_in_window == 3  # 1 h, 3 h et le refus ; ni T0 ni la réservation
    assert observed_limits(engine, since=refused_at + timedelta(seconds=1)) == []
    assert "limite DSP2 observée" in caplog.text and "3 requêtes" in caplog.text
