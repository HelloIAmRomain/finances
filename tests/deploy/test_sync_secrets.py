"""deploy/sync-secrets.sh et encrypted_secrets_file : dépôt privé des secrets, à côté du code."""

import os
import subprocess
from pathlib import Path

import pytest

from tests.deploy.helpers import DEPLOY, require_tool, run

SYNC = DEPLOY / "sync-secrets.sh"


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(  # noqa: S603
        ["git", "-C", str(cwd), *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        },
    )
    return result.stdout.strip()


def _commit(repo: Path, content: str) -> str:
    target = repo / "deploy" / "secrets.enc.yaml"
    target.parent.mkdir(exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", content)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repos(tmp_path: Path) -> tuple[Path, Path]:
    """(dépôt d'origine, clone du Pi) avec un premier commit de secrets chiffrés factices."""
    require_tool("git")
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "--quiet", "--initial-branch=main")
    _commit(origin, "v1")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "--quiet", str(origin), str(clone)], check=True)  # noqa: S603, S607
    return origin, clone


def _env(repo: Path) -> dict[str, str]:
    return {
        "PATH": os.environ["PATH"],
        "HOME": str(repo.parent),
        "FINANCES_SECRETS_REPO_DIR": str(repo),
    }


def test_without_secrets_repo_does_nothing(tmp_path: Path) -> None:
    result = run([SYNC], _env(tmp_path / "absent"))
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


def test_up_to_date_repo_is_silent(repos: tuple[Path, Path]) -> None:
    _, clone = repos
    result = run([SYNC], _env(clone))
    assert (result.returncode, result.stdout) == (0, "")


def test_new_secrets_are_fast_forwarded(repos: tuple[Path, Path]) -> None:
    origin, clone = repos
    head = _commit(origin, "v2")
    result = run([SYNC], _env(clone))
    assert result.returncode == 0, result.stderr
    assert f"secrets mis à jour ({head[:7]})" in result.stdout
    assert (clone / "deploy" / "secrets.enc.yaml").read_text(encoding="utf-8") == "v2"


def test_diverged_history_is_refused(repos: tuple[Path, Path]) -> None:
    origin, clone = repos
    _commit(origin, "v2")
    _commit(clone, "modification locale")
    result = run([SYNC], _env(clone))
    assert result.returncode == 1
    assert "historique divergent" in result.stderr
    assert (clone / "deploy" / "secrets.enc.yaml").read_text(
        encoding="utf-8"
    ) == "modification locale"


def test_unreachable_origin_is_an_error(repos: tuple[Path, Path]) -> None:
    origin, clone = repos
    origin.rename(origin.with_name("disparu"))
    result = run([SYNC], _env(clone))
    assert result.returncode == 1
    assert "injoignable" in result.stderr


@pytest.mark.parametrize("separate", [True, False])
def test_encrypted_secrets_file(tmp_path: Path, separate: bool) -> None:
    secrets_repo, code = tmp_path / "secrets", tmp_path / "code"
    if separate:
        (secrets_repo / "deploy").mkdir(parents=True)
        (secrets_repo / "deploy" / "secrets.enc.yaml").touch()
    script = f'source "{DEPLOY / "lib.sh"}" && encrypted_secrets_file "{code}"'
    result = run(["bash", "-c", script], _env(secrets_repo))
    expected = (secrets_repo if separate else code) / "deploy" / "secrets.enc.yaml"
    assert result.stdout.strip() == str(expected)
