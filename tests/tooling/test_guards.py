"""Vérifie que les garde-fous de l'outillage restent actifs."""

import socket
import subprocess
from pathlib import Path

import pytest
from pytest_socket import SocketBlockedError
from ruff.__main__ import find_ruff_bin

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.filterwarnings("ignore:A test tried to use socket")
def test_network_is_blocked() -> None:
    with pytest.raises(SocketBlockedError):
        socket.create_connection(("192.0.2.1", 443), timeout=1)


def test_ruff_flags_naive_datetime(tmp_path: Path) -> None:
    sample = tmp_path / "naive.py"
    sample.write_text("import datetime\n\nNOW = datetime.datetime.now()\n", encoding="utf-8")
    result = subprocess.run(  # noqa: S603
        [find_ruff_bin(), "check", "--config", str(ROOT / "pyproject.toml"), str(sample)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "DTZ005" in result.stdout
