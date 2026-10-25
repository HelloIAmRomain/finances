"""Utilitaires communs aux tests des scripts d'exploitation."""

import json
import os
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"
PASSWORD = "mot-de-passe-restic-de-test-123456"  # noqa: S105 (factice)


def require_tool(name: str) -> str:
    """Chemin de l'outil ; saute le test s'il manque (échoue en CI, FINANCES_REQUIRE_TOOLS=1)."""
    path = shutil.which(name)
    if path is None:
        if os.environ.get("FINANCES_REQUIRE_TOOLS") == "1":
            pytest.fail(f"{name} requis en CI")
        pytest.skip(f"{name} absent")
    return path


def run(args: list[str | Path], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [str(arg) for arg in args], capture_output=True, text=True, check=False, env=env
    )


def is_memory_fs(path: Path) -> bool:
    fstype = subprocess.run(  # noqa: S603
        ["findmnt", "-no", "FSTYPE", "--target", str(path)],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    return fstype in {"tmpfs", "ramfs"}


@dataclass
class ResticSetup:
    """Dépôt restic local jetable et base SQLite en WAL avec des données non « checkpointées »."""

    env: dict[str, str]
    db: Path
    work: Path
    writer: sqlite3.Connection

    def snapshots(self) -> list[dict[str, object]]:
        result = run(["restic", "snapshots", "--json"], self.env)
        assert result.returncode == 0, result.stderr
        snapshots: list[dict[str, object]] = json.loads(result.stdout)
        return snapshots

    def restic(self, *args: str | Path) -> subprocess.CompletedProcess[str]:
        return run(["restic", *args], self.env)


def make_restic_setup(tmp_path: Path) -> ResticSetup:
    require_tool("restic")
    password_file = tmp_path / "restic-password"
    password_file.write_text(PASSWORD, encoding="utf-8")
    env = {
        "PATH": os.environ["PATH"],
        "RESTIC_REPOSITORY": str(tmp_path / "repo"),
        "RESTIC_PASSWORD_FILE": str(password_file),
        "RESTIC_CACHE_DIR": str(tmp_path / "cache"),
        "FINANCES_DB": str(tmp_path / "data" / "finances.db"),
        "FINANCES_BACKUP_TMP": str(tmp_path / "work"),
        "FINANCES_HOST_SECRETS_DIR": str(tmp_path / "no-secrets"),
        "FINANCES_ALLOW_DISK": "1",
    }
    init = run(["restic", "init"], env)
    assert init.returncode == 0, init.stderr

    db = tmp_path / "data" / "finances.db"
    db.parent.mkdir()
    writer = sqlite3.connect(db)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")  # garder des données dans le WAL
    writer.execute("CREATE TABLE ledger (id INTEGER PRIMARY KEY, amount_cents INTEGER)")
    writer.executemany("INSERT INTO ledger (amount_cents) VALUES (?)", [(i,) for i in range(500)])
    writer.commit()
    return ResticSetup(env=env, db=db, work=tmp_path / "work", writer=writer)
