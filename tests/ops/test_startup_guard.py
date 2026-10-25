"""Garde de démarrage : une clé crypto non vérifiée empêche le démarrage (inv. 1)."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from app.ops import startup_guard
from app.ops.startup_guard import UnsafeKeyError, check_startup


@pytest.fixture(autouse=True)
def no_verifiers() -> Iterator[None]:
    saved = dict(startup_guard.VERIFIERS)
    startup_guard.VERIFIERS.clear()
    yield
    startup_guard.VERIFIERS.clear()
    startup_guard.VERIFIERS.update(saved)


def test_no_crypto_key_starts(tmp_path: Path) -> None:
    (tmp_path / "app_secret_key").write_text("x")
    check_startup(tmp_path)
    check_startup(tmp_path / "absent")


@pytest.mark.parametrize("name", ["coinbase_api_key", "okx_api_key"])
def test_unverified_crypto_key_refused(tmp_path: Path, name: str) -> None:
    (tmp_path / name).write_text("factice")
    with pytest.raises(UnsafeKeyError, match="démarrage refusé"):
        check_startup(tmp_path)


def test_registered_verifier_is_called(tmp_path: Path) -> None:
    (tmp_path / "okx_api_key").write_text("factice")
    seen: list[Path] = []
    startup_guard.VERIFIERS["okx"] = seen.append
    check_startup(tmp_path)
    assert seen == [tmp_path]


def test_verifier_can_refuse(tmp_path: Path) -> None:
    (tmp_path / "coinbase_api_key").write_text("factice")

    def refuse(_: Path) -> None:
        raise UnsafeKeyError("la clé peut trader")

    startup_guard.VERIFIERS["coinbase"] = refuse
    with pytest.raises(UnsafeKeyError, match="trader"):
        check_startup(tmp_path)
