"""Tests de deploy/backup-maintenance.sh et deploy/restore-test.sh (dépôt restic local jetable)."""

import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tests.deploy.helpers import DEPLOY, ResticSetup, run

BACKUP = DEPLOY / "backup.sh"
MAINTENANCE = DEPLOY / "backup-maintenance.sh"
RESTORE_TEST = DEPLOY / "restore-test.sh"


def _snapshot_at(setup: ResticSetup, data: bytes, when: datetime) -> None:
    """Instantané daté artificiellement (restic --time attend l'heure locale)."""
    stamp = when.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    args = ["restic", "backup", "--quiet", "--host", "finances", "--tag", "db", "--time", stamp]
    result = subprocess.run(  # noqa: S603
        [*args, "--stdin", "--stdin-filename", "finances.db"],
        input=data,
        capture_output=True,
        check=False,
        env=setup.env,
    )
    assert result.returncode == 0, result.stderr


def _valid_database_bytes(setup: ResticSetup, tmp_path: Path) -> bytes:
    copy = tmp_path / "copy.db"
    target = sqlite3.connect(copy)
    setup.writer.backup(target)
    target.execute("PRAGMA journal_mode=DELETE")
    target.close()
    return copy.read_bytes()


def test_retention_keeps_recent_and_one_per_day(setup: ResticSetup, tmp_path: Path) -> None:
    data = _valid_database_bytes(setup, tmp_path)
    now = datetime.now(UTC).replace(microsecond=0)
    three_days_ago = (now - timedelta(days=3)).replace(hour=10, minute=0, second=0)
    for when in (
        now,
        now - timedelta(hours=1),
        three_days_ago,
        three_days_ago + timedelta(hours=1),
        now - timedelta(days=60),
    ):
        _snapshot_at(setup, data, when)
    assert len(setup.snapshots()) == 5

    result = run([MAINTENANCE], setup.env)
    assert result.returncode == 0, result.stderr

    # Seul le premier des deux instantanés du même jour ancien disparaît.
    remaining = setup.snapshots()
    assert len(remaining) == 4
    removed = three_days_ago.astimezone().strftime("%Y-%m-%dT%H:%M")
    assert not any(str(snapshot["time"]).startswith(removed) for snapshot in remaining)


def test_restore_test_passes_on_fresh_backup(setup: ResticSetup) -> None:
    assert run([BACKUP], setup.env).returncode == 0
    result = run([RESTORE_TEST], setup.env)
    assert result.returncode == 0, result.stderr
    assert "intégrité OK" in result.stdout


def test_restore_test_leaves_work_directory_empty(setup: ResticSetup) -> None:
    assert run([BACKUP], setup.env).returncode == 0
    assert run([RESTORE_TEST], setup.env).returncode == 0
    assert list(setup.work.iterdir()) == []


def test_restore_test_fails_without_snapshot(setup: ResticSetup) -> None:
    result = run([RESTORE_TEST], setup.env)
    assert result.returncode != 0
    assert "aucun instantané" in result.stderr


def test_restore_test_fails_on_stale_snapshot(setup: ResticSetup, tmp_path: Path) -> None:
    data = _valid_database_bytes(setup, tmp_path)
    _snapshot_at(setup, data, datetime.now(UTC) - timedelta(hours=2))
    result = run([RESTORE_TEST], setup.env)
    assert result.returncode != 0
    assert "trop ancien" in result.stderr


def test_restore_test_fails_on_corrupted_snapshot(setup: ResticSetup) -> None:
    _snapshot_at(setup, b"pas une base sqlite" * 200, datetime.now(UTC))
    result = run([RESTORE_TEST], setup.env)
    assert result.returncode != 0
    assert "corrompu" in result.stderr
