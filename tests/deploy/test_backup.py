"""Tests de bout en bout de deploy/backup.sh et deploy/restore.sh (dépôt restic local jetable)."""

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from tests.deploy.helpers import DEPLOY, PASSWORD, ResticSetup, is_memory_fs, require_tool, run

BACKUP = DEPLOY / "backup.sh"
RESTORE = DEPLOY / "restore.sh"

PING_SERVER = """
import http.server, sys
log = open(sys.argv[1], "a", buffering=1)
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        log.write(self.path + "\\n")
        self.send_response(200)
        self.end_headers()
    def log_message(self, *args):
        pass
server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
print(server.server_port, flush=True)
server.serve_forever()
"""


def _dump(path: Path) -> list[str]:
    connection = sqlite3.connect(path)
    try:
        return list(connection.iterdump())
    finally:
        connection.close()


def test_backup_then_restore_roundtrip(setup: ResticSetup, tmp_path: Path) -> None:
    assert (setup.db.parent / "finances.db-wal").stat().st_size > 0
    result = run([BACKUP], setup.env)
    assert result.returncode == 0, result.stderr
    assert len(setup.snapshots()) == 1

    restored = tmp_path / "restored" / "finances.db"
    result = run([RESTORE, restored], setup.env)
    assert result.returncode == 0, result.stderr
    assert _dump(restored) == _dump(setup.db)
    connection = sqlite3.connect(restored)
    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    connection.close()


def test_work_directory_left_empty(setup: ResticSetup) -> None:
    assert run([BACKUP], setup.env).returncode == 0
    assert list(setup.work.iterdir()) == []


def test_missing_database_fails(setup: ResticSetup, tmp_path: Path) -> None:
    env = {**setup.env, "FINANCES_DB": str(tmp_path / "absente.db")}
    result = run([BACKUP], env)
    assert result.returncode != 0
    assert "base absente" in result.stderr


def test_corrupted_database_is_not_uploaded(setup: ResticSetup, tmp_path: Path) -> None:
    corrupted = tmp_path / "corrupted.db"
    corrupted.write_bytes(b"ceci n'est pas une base sqlite" * 100)
    result = run([BACKUP], {**setup.env, "FINANCES_DB": str(corrupted)})
    assert result.returncode != 0
    assert setup.snapshots() == []


def test_refuses_work_directory_on_disk(setup: ResticSetup) -> None:
    setup.work.mkdir()
    if is_memory_fs(setup.work):
        pytest.skip("le répertoire temporaire est déjà en mémoire")
    env = {key: value for key, value in setup.env.items() if key != "FINANCES_ALLOW_DISK"}
    result = run([BACKUP], env)
    assert result.returncode != 0
    assert "n'est pas en mémoire" in result.stderr
    assert setup.snapshots() == []


def test_password_never_printed(setup: ResticSetup, tmp_path: Path) -> None:
    backup = run([BACKUP], setup.env)
    restore = run([RESTORE, tmp_path / "r.db"], setup.env)
    for result in (backup, restore):
        assert PASSWORD not in result.stdout + result.stderr


def test_restore_refuses_to_overwrite(setup: ResticSetup) -> None:
    assert run([BACKUP], setup.env).returncode == 0
    result = run([RESTORE, setup.db], setup.env)
    assert result.returncode != 0
    assert "existe déjà" in result.stderr


def test_restore_refuses_leftover_wal(setup: ResticSetup, tmp_path: Path) -> None:
    assert run([BACKUP], setup.env).returncode == 0
    dest = tmp_path / "restored" / "finances.db"
    dest.parent.mkdir()
    (dest.parent / "finances.db-wal").write_bytes(b"ancien journal")
    result = run([RESTORE, dest], setup.env)
    assert result.returncode != 0
    assert "-wal existe encore" in result.stderr
    assert not dest.exists()


def test_healthchecks_pinged_on_success_and_failure(setup: ResticSetup, tmp_path: Path) -> None:
    require_tool("curl")
    log = tmp_path / "pings.log"
    server = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", PING_SERVER, str(log)], stdout=subprocess.PIPE, text=True
    )
    try:
        assert server.stdout is not None
        port = server.stdout.readline().strip()
        url = f"http://127.0.0.1:{port}/check-uuid"
        env = {**setup.env, "FINANCES_HEALTHCHECK_URL": url}
        assert run([BACKUP], env).returncode == 0
        missing = {**env, "FINANCES_DB": str(tmp_path / "absente.db")}
        assert run([BACKUP], missing).returncode != 0
    finally:
        server.terminate()
        server.wait(timeout=5)
        if server.stdout is not None:
            server.stdout.close()
    assert log.read_text(encoding="utf-8").splitlines() == ["/check-uuid", "/check-uuid/fail"]
