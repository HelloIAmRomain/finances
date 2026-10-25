"""Règles statiques sur le Dockerfile (vérifiées sans Docker)."""

import re
from pathlib import Path

DOCKERFILE = Path(__file__).resolve().parents[2] / "Dockerfile"
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}\b")


def _instructions() -> list[str]:
    text = DOCKERFILE.read_text(encoding="utf-8").replace("\\\n", " ")
    return [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]


def test_base_images_pinned_by_digest() -> None:
    froms = [line for line in _instructions() if line.upper().startswith("FROM ")]
    assert froms
    for line in froms:
        assert DIGEST.search(line), f"image non épinglée : {line}"


def test_runtime_runs_as_non_root() -> None:
    users = [line.split()[1] for line in _instructions() if line.upper().startswith("USER ")]
    assert users, "aucune instruction USER"
    assert users[-1] not in {"root", "0"}


def test_healthcheck_declared() -> None:
    assert any(line.upper().startswith("HEALTHCHECK ") for line in _instructions())


def test_no_remote_add() -> None:
    for line in _instructions():
        assert not re.match(r"ADD\s+https?://", line, re.IGNORECASE), line
