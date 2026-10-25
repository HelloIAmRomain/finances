"""Planificateur du worker : créneaux à l'heure de Paris, rattrapage unique (NF-RES-03)."""

import threading
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, insert, select, update

import app.models  # noqa: F401
from app.db import Base, make_engine
from app.models import JobRun
from app.models.ops import JobStatus
from app.ops.jobs import JobContext
from app.ops.scheduler import DailyAt, Every, Job, Scheduler
from app.timeutil import PARIS

utc = UTC


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    engine = make_engine(tmp_path / "sched.db")
    Base.metadata.create_all(engine)
    return engine


# --- Créneaux -------------------------------------------------------------------------------


def test_daily_slots_follow_paris_time_across_dst() -> None:
    schedule = DailyAt(time(7, 0))
    # Hiver (UTC+1) : 07:00 Paris = 06:00 UTC ; été (UTC+2) : 05:00 UTC.
    assert schedule.latest(datetime(2026, 1, 15, 12, 0, tzinfo=utc)) == datetime(
        2026, 1, 15, 6, 0, tzinfo=utc
    )
    assert schedule.latest(datetime(2026, 7, 15, 12, 0, tzinfo=utc)) == datetime(
        2026, 7, 15, 5, 0, tzinfo=utc
    )


def test_daily_latest_is_previous_day_before_first_slot() -> None:
    schedule = DailyAt(time(7, 0), time(19, 0))
    assert schedule.latest(datetime(2026, 7, 15, 4, 0, tzinfo=utc)) == datetime(
        2026, 7, 14, 17, 0, tzinfo=utc
    )


def test_daily_several_slots() -> None:
    schedule = DailyAt(time(6, 30), time(12, 30), time(18, 30), time(23, 0))
    now = datetime(2026, 7, 15, 12, 0, tzinfo=utc)  # 14:00 à Paris
    assert schedule.latest(now) == datetime(2026, 7, 15, 10, 30, tzinfo=utc)  # 12:30 Paris


def test_every_aligns_on_utc_multiples() -> None:
    schedule = Every(timedelta(minutes=15))
    assert schedule.latest(datetime(2026, 7, 15, 12, 44, 59, tzinfo=utc)) == datetime(
        2026, 7, 15, 12, 30, tzinfo=utc
    )
    assert schedule.latest(datetime(2026, 7, 15, 12, 45, tzinfo=utc)) == datetime(
        2026, 7, 15, 12, 45, tzinfo=utc
    )


def test_schedule_rejects_naive_now() -> None:
    with pytest.raises(ValueError, match="sans fuseau"):
        Every(timedelta(minutes=15)).latest(datetime(2026, 7, 15))  # noqa: DTZ001


# --- Boucle -----------------------------------------------------------------------------------


class Clock:
    def __init__(self, start: datetime) -> None:
        self.current = start

    def __call__(self) -> datetime:
        return self.current


def _counting_job(name: str, schedule: DailyAt | Every, calls: list[datetime]) -> Job:
    def run(ctx: JobContext) -> dict[str, int]:
        calls.append(ctx.slot_start)
        return {}

    return Job(name=name, schedule=schedule, run=run)


def test_tick_runs_due_job_once(engine: Engine) -> None:
    calls: list[datetime] = []
    clock = Clock(datetime(2026, 7, 15, 5, 0, 10, tzinfo=utc))
    scheduler = Scheduler(engine, [_counting_job("sync", DailyAt(time(7, 0)), calls)], now=clock)
    scheduler.tick()
    clock.current += timedelta(minutes=1)
    scheduler.tick()
    assert calls == [datetime(2026, 7, 15, 5, 0, tzinfo=utc)]


def test_catch_up_after_two_day_downtime_runs_latest_slot_once(engine: Engine) -> None:
    """Deux jours d'arrêt (plusieurs créneaux quotidiens manqués) : un seul rattrapage."""
    calls: list[datetime] = []
    job = _counting_job("sync", DailyAt(time(6, 30), time(18, 30)), calls)
    clock = Clock(datetime(2026, 7, 15, 4, 31, tzinfo=utc))  # 06:31 Paris
    Scheduler(engine, [job], now=clock).tick()
    clock.current += timedelta(days=2, hours=1)  # arrêt : 4 créneaux manqués
    Scheduler(engine, [job], now=clock).tick()
    Scheduler(engine, [job], now=clock).tick()
    assert calls == [
        datetime(2026, 7, 15, 4, 30, tzinfo=utc),
        datetime(2026, 7, 17, 4, 30, tzinfo=utc),  # seul le plus récent est rattrapé
    ]


def test_job_overrunning_its_slot_is_not_started_twice(engine: Engine) -> None:
    """Une exécution toujours vivante bloque le créneau suivant de la même tâche."""
    calls: list[datetime] = []
    job = _counting_job("long", Every(timedelta(minutes=15)), calls)
    start = datetime(2026, 7, 15, 12, 0, 5, tzinfo=utc)
    with engine.begin() as connection:
        connection.execute(
            insert(JobRun).values(
                job_name="long",
                slot_start=datetime(2026, 7, 15, 12, 0, tzinfo=utc),
                status=JobStatus.RUNNING,
                started_at=start,
                heartbeat_at=start,
                attempt=1,
                stats={},
            )
        )
    clock = Clock(datetime(2026, 7, 15, 12, 15, 30, tzinfo=utc))
    with engine.begin() as connection:  # la tâche longue signale qu'elle est vivante
        connection.execute(update(JobRun).values(heartbeat_at=clock.current))
    Scheduler(engine, [job], now=clock).tick()
    assert calls == []  # pas de seconde exécution en parallèle
    with engine.begin() as connection:
        connection.execute(update(JobRun).values(status=JobStatus.SUCCESS))
    clock.current += timedelta(minutes=1)
    Scheduler(engine, [job], now=clock).tick()
    assert calls == [datetime(2026, 7, 15, 12, 15, tzinfo=utc)]


@pytest.mark.parametrize(
    ("day", "label"),
    [
        (date(2026, 3, 29), "passage à l'heure d'été"),
        (date(2026, 10, 25), "retour à l'heure d'hiver"),
    ],
)
def test_daylight_saving_changes_skip_or_double_nothing(
    engine: Engine, day: date, label: str
) -> None:
    """Tour toutes les 5 min sur trois jours autour du changement d'heure, avec un horaire dans
    l'heure qui disparaît (au printemps) ou se répète (en automne) : chaque jour, exactement un
    passage par horaire."""
    calls: list[datetime] = []
    job = _counting_job("quotidien", DailyAt(time(2, 30), time(7, 0)), calls)
    clock = Clock(datetime.combine(day - timedelta(days=1), time(0, 0), tzinfo=utc))
    scheduler = Scheduler(engine, [job], now=clock)
    end = clock.current + timedelta(days=3)
    while clock.current < end:
        scheduler.tick()
        clock.current += timedelta(minutes=5)
    per_day: dict[date, int] = {}
    for slot in calls:
        paris_day = slot.astimezone(PARIS).date()
        per_day[paris_day] = per_day.get(paris_day, 0) + 1
    assert per_day[day] == 2, label
    assert len(calls) == len(set(calls))


def test_jitter_is_bounded_deterministic_and_spread() -> None:
    jitter = timedelta(minutes=5)
    first = DailyAt(time(6, 30), jitter=jitter, seed="bank.sync.1")
    again = DailyAt(time(6, 30), jitter=jitter, seed="bank.sync.1")
    assert first.offset == again.offset <= jitter
    offsets = {DailyAt(time(6, 30), jitter=jitter, seed=f"bank.sync.{n}").offset for n in range(10)}
    assert len(offsets) > 1  # deux comptes n'interrogent pas leur banque à la même seconde
    now = datetime(2026, 7, 15, 12, 0, tzinfo=utc)
    assert first.latest(now) == datetime(2026, 7, 15, 4, 30, tzinfo=utc) + first.offset


def test_catch_up_after_downtime_runs_once(engine: Engine) -> None:
    """Six heures d'arrêt couvrant plusieurs créneaux : une seule exécution, le plus récent."""
    calls: list[datetime] = []
    clock = Clock(datetime(2026, 7, 15, 12, 7, tzinfo=utc))
    job = _counting_job("backup", Every(timedelta(minutes=15)), calls)
    Scheduler(engine, [job], now=clock).tick()
    assert calls == [datetime(2026, 7, 15, 12, 0, tzinfo=utc)]


def test_restart_does_not_rerun_done_slot(engine: Engine) -> None:
    calls: list[datetime] = []
    clock = Clock(datetime(2026, 7, 15, 5, 0, 10, tzinfo=utc))
    job = _counting_job("sync", DailyAt(time(7, 0)), calls)
    Scheduler(engine, [job], now=clock).tick()
    Scheduler(engine, [job], now=clock).tick()  # nouveau processus après redémarrage
    assert len(calls) == 1
    with engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(JobRun)).scalar_one() == 1


def test_failing_job_does_not_block_others(engine: Engine) -> None:
    calls: list[datetime] = []

    def broken(ctx: JobContext) -> dict[str, int]:
        raise RuntimeError("panne")

    jobs = [
        Job(name="broken", schedule=Every(timedelta(minutes=15)), run=broken),
        _counting_job("ok", Every(timedelta(minutes=15)), calls),
    ]
    Scheduler(engine, jobs, now=Clock(datetime(2026, 7, 15, 12, 7, tzinfo=utc))).tick()
    assert len(calls) == 1


def test_duplicate_job_names_rejected(engine: Engine) -> None:
    job = _counting_job("sync", Every(timedelta(minutes=15)), [])
    with pytest.raises(ValueError, match="en double"):
        Scheduler(engine, [job, job])


def test_run_forever_stops_on_event(engine: Engine) -> None:
    calls: list[datetime] = []
    stop = threading.Event()
    scheduler = Scheduler(
        engine,
        [_counting_job("tick", Every(timedelta(minutes=15)), calls)],
        tick_interval=timedelta(milliseconds=10),
    )
    timer = threading.Timer(0.1, stop.set)
    timer.start()
    try:
        ticks = scheduler.run_forever(stop)
    finally:
        timer.cancel()
    assert ticks >= 1
    assert len(calls) == 1  # même créneau à chaque tour : une seule exécution
