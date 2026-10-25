"""Tests de bout en bout de deploy/update.sh : déploiement et retour arrière (Docker requis)."""

import base64
import fcntl
import os
import sqlite3
import subprocess
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.deploy.helpers import DEPLOY, ROOT, require_tool, run

UPDATE = DEPLOY / "update.sh"
GOOD, GOOD_V2, CRASH, BAD_MIGRATION = (
    "finances:test-good",
    "finances:test-good-v2",
    "finances:test-crash",
    "finances:test-bad-migration",
)

BROKEN_MIGRATION = '''"""Migration volontairement cassée : crée une table puis échoue."""

import sqlalchemy as sa
from alembic import op

revision = "casse"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("marqueur", sa.Column("id", sa.Integer, primary_key=True))
    raise RuntimeError("migration cassée")


def downgrade() -> None:
    pass
'''


def _docker(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["docker", *args],  # noqa: S607
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )


def _build(tag: str, context: Path, dockerfile: str | None = None) -> None:
    args = ["build", "--quiet", "--tag", tag]
    if dockerfile is not None:
        args += ["--file", "-"]
    result = _docker(*args, str(context), stdin=dockerfile)
    assert result.returncode == 0, result.stderr


def _image_id(tag: str) -> str:
    """Même référence épinglée que update.sh (condensat du registre, sinon identifiant)."""
    template = "{{if .RepoDigests}}{{index .RepoDigests 0}}{{else}}{{.Id}}{{end}}"
    return _docker("image", "inspect", "--format", template, tag).stdout.strip()


@pytest.fixture(scope="session")
def images(tmp_path_factory: pytest.TempPathFactory) -> None:
    require_tool("docker")
    if _docker("info").returncode != 0:
        if os.environ.get("FINANCES_REQUIRE_TOOLS") == "1":
            pytest.fail("démon Docker requis en CI")
        pytest.skip("démon Docker indisponible")
    _build(GOOD, ROOT)
    context = tmp_path_factory.mktemp("images")
    _build(GOOD_V2, context, f'FROM {GOOD}\nLABEL variante="v2"\n')
    _build(CRASH, context, f'FROM {GOOD}\nCMD ["python", "-c", "raise SystemExit(1)"]\n')
    (context / "casse.py").write_text(BROKEN_MIGRATION, encoding="utf-8")
    _build(BAD_MIGRATION, context, f"FROM {GOOD}\nCOPY casse.py migrations/versions/casse.py\n")


@dataclass
class Stack:
    env: dict[str, str]
    data: Path
    state: Path
    web_port: int

    def update(self, image: str) -> subprocess.CompletedProcess[str]:
        return run([UPDATE, image], self.env)

    def current(self) -> str:
        return (self.state / "current-image").read_text(encoding="utf-8").strip()

    def web_healthy(self) -> bool:
        url = f"http://127.0.0.1:{self.web_port}/health"
        return run(["curl", "-fsS", "-m", "5", url], self.env).returncode == 0

    def compose(self, *args: str) -> subprocess.CompletedProcess[str]:
        return run(
            ["docker", "compose", "-f", DEPLOY / "compose.yml", *args],
            {**self.env, "FINANCES_IMAGE": GOOD},
        )

    def tables(self) -> set[str]:
        connection = sqlite3.connect(f"file:{self.data / 'finances.db'}?mode=ro", uri=True)
        try:
            rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            return {row[0] for row in rows}
        finally:
            connection.close()


@pytest.fixture
def stack(images: None, tmp_path: Path) -> Iterator[Stack]:
    offset = zlib.crc32(str(tmp_path).encode()) % 900
    web_port = 18000 + offset
    data, ntfy, state, secrets, host = (
        tmp_path / name for name in ("data", "ntfy", "state", "s", "hs")
    )
    for directory in (data, ntfy, secrets, host):
        directory.mkdir()
        directory.chmod(0o777)  # écriture par les uid 10001 et 10002 des conteneurs
    fake_secrets = (("app_secret_key", 48), ("data_encryption_key", 32), ("blind_index_key", 32))
    for name, size in fake_secrets:
        (secrets / name).write_text(base64.b64encode(os.urandom(size)).decode())
        (secrets / name).chmod(0o444)
    env = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ.get("HOME", "/tmp"),  # noqa: S108 (config Docker du client)
        "COMPOSE_PROJECT_NAME": f"finances-test-{os.getpid()}-{offset}",
        "FINANCES_DIR": str(ROOT),
        "FINANCES_STATE_DIR": str(state),
        "FINANCES_DATA_DIR": str(data),
        "FINANCES_NTFY_DIR": str(ntfy),
        "FINANCES_SECRETS_DIR": str(secrets),
        "FINANCES_HOST_SECRETS_DIR": str(host),
        "FINANCES_WEB_PORT": str(web_port),
        "FINANCES_NTFY_PORT": str(web_port + 1000),
        "FINANCES_NO_GIT": "1",
        "FINANCES_NO_PULL": "1",
        "FINANCES_PRE_DEPLOY_BACKUP": "true",
        "FINANCES_HEALTH_TIMEOUT": "30",
    }
    stack = Stack(env=env, data=data, state=state, web_port=web_port)
    yield stack
    stack.compose("down", "--remove-orphans", "--timeout", "5")
    # Fichiers créés par l'uid 10001 : les rendre supprimables par le nettoyage de pytest.
    _docker(
        "run", "--rm", "--user", "0", "-v", f"{tmp_path}:/t", GOOD, "chmod", "-R", "a+rwX", "/t"
    )


def test_first_deploy(stack: Stack) -> None:
    result = stack.update(GOOD)
    assert result.returncode == 0, result.stderr
    assert stack.current() == _image_id(GOOD)
    assert stack.web_healthy()
    assert (stack.data / "finances.db").exists()
    # Le retour arrière relancera la version actuelle avec le fichier compose qui l'a démarrée.
    saved = (stack.state / "current-compose.yml").read_text(encoding="utf-8")
    assert saved == (DEPLOY / "compose.yml").read_text(encoding="utf-8")


def test_same_image_is_a_no_op(stack: Stack) -> None:
    assert stack.update(GOOD).returncode == 0
    web_before = stack.compose("ps", "-q", "web").stdout.strip()
    result = stack.update(GOOD)
    assert result.returncode == 0, result.stderr
    assert "déjà à jour" in result.stdout
    assert stack.compose("ps", "-q", "web").stdout.strip() == web_before


def test_upgrade_keeps_previous_and_cleans_snapshot(stack: Stack) -> None:
    assert stack.update(GOOD).returncode == 0
    result = stack.update(GOOD_V2)
    assert result.returncode == 0, result.stderr
    assert stack.current() == _image_id(GOOD_V2)
    assert (stack.state / "previous-image").read_text().strip() == _image_id(GOOD)
    assert not (stack.data / "pre-deploy.db").exists()
    assert stack.web_healthy()


def test_failed_migration_rolls_back_image_and_database(stack: Stack) -> None:
    assert stack.update(GOOD).returncode == 0
    result = stack.update(BAD_MIGRATION)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "retour à" in result.stderr
    assert stack.current() == _image_id(GOOD)
    assert stack.web_healthy()
    # La table créée par la migration cassée a disparu avec la restauration de l'instantané…
    assert "marqueur" not in stack.tables()
    # … et la base en échec est conservée pour analyse.
    kept = list(stack.data.glob("echec-*/finances.db"))
    assert len(kept) == 1


def test_crashing_image_rolls_back(stack: Stack) -> None:
    assert stack.update(GOOD).returncode == 0
    result = stack.update(CRASH)
    assert result.returncode == 1, result.stdout + result.stderr
    assert stack.current() == _image_id(GOOD)
    assert stack.web_healthy()


def test_missing_host_secrets_are_rendered_before_anything_else(stack: Stack) -> None:
    """Bascule du 16/10/2026 : fichiers mis à jour par l'ancien script, secrets de l'hôte jamais
    déchiffrés, sauvegarde avant déploiement en échec chaque nuit."""
    assert stack.update(GOOD).returncode == 0
    host = Path(stack.env["FINANCES_HOST_SECRETS_DIR"])
    host.rmdir()
    render = f"mkdir -p {host} && touch {host}/restic_repository"
    result = run([UPDATE, GOOD_V2], {**stack.env, "FINANCES_RENDER_SECRETS": render})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "secrets de l'hôte déchiffrés" in result.stdout
    assert (host / "restic_repository").exists() and stack.current() == _image_id(GOOD_V2)


def test_undecryptable_host_secrets_change_nothing(stack: Stack) -> None:
    assert stack.update(GOOD).returncode == 0
    Path(stack.env["FINANCES_HOST_SECRETS_DIR"]).rmdir()
    result = run([UPDATE, GOOD_V2], {**stack.env, "FINANCES_RENDER_SECRETS": "false"})
    assert result.returncode == 1
    assert "non déchiffrables" in result.stderr
    assert stack.current() == _image_id(GOOD) and stack.web_healthy()


def test_failing_pre_deploy_backup_cancels(stack: Stack) -> None:
    assert stack.update(GOOD).returncode == 0
    result = run([UPDATE, GOOD_V2], {**stack.env, "FINANCES_PRE_DEPLOY_BACKUP": "false"})
    assert result.returncode == 1
    assert "déploiement annulé" in result.stderr
    assert stack.current() == _image_id(GOOD)
    assert stack.web_healthy()


def test_a_second_update_waits_for_the_first(tmp_path: Path) -> None:
    """Minuterie et lancement manuel ne déploient jamais en même temps (sans Docker)."""
    state = tmp_path / "state"
    state.mkdir()
    env = {"PATH": os.environ["PATH"], "FINANCES_DIR": str(ROOT), "FINANCES_STATE_DIR": str(state)}
    with (state / "update.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = run([UPDATE, "--no-git", "image:inexistante"], env)
    assert result.returncode == 1
    assert "déjà en cours" in result.stderr
