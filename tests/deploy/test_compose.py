"""Règles de sécurité et d'exploitation sur deploy/compose.yml (inv. 7 et 11, NF-PERF-01)."""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

COMPOSE = Path(__file__).resolve().parents[2] / "deploy" / "compose.yml"
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")
APP_IMAGE_VARIABLE = re.compile(r"^\$\{FINANCES_IMAGE:\?.+\}$")
LOCALHOST_PORT = re.compile(r"^127\.0\.0\.1:(\d+|\$\{[A-Z_]+:-\d+\}):\d+$")
# Plafond mémoire total pour un Pi 4 Go dédié (OS, restic et cache disque en plus).
MEMORY_BUDGET_MB = 2048


def _services() -> dict[str, dict[str, Any]]:
    document = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    services: dict[str, dict[str, Any]] = document["services"]
    return services


def _megabytes(value: str) -> int:
    number, unit = int(value[:-1]), value[-1].lower()
    return {"m": number, "g": number * 1024}[unit]


SERVICES = _services()


@pytest.mark.parametrize("name", sorted(SERVICES))
def test_image_pinned(name: str) -> None:
    image = SERVICES[name]["image"]
    assert DIGEST.search(image) or APP_IMAGE_VARIABLE.match(image), image


@pytest.mark.parametrize("name", sorted(SERVICES))
def test_ports_only_on_localhost(name: str) -> None:
    for port in SERVICES[name].get("ports", []):
        assert isinstance(port, str), "syntaxe courte attendue pour vérifier l'adresse"
        assert LOCALHOST_PORT.match(port), port


@pytest.mark.parametrize("name", sorted(SERVICES))
def test_no_privilege_escalation(name: str) -> None:
    service = SERVICES[name]
    assert not service.get("privileged", False)
    assert service.get("network_mode") != "host"
    assert service.get("pid") != "host"
    assert service.get("cap_drop") == ["ALL"]
    assert not service.get("cap_add")
    assert "no-new-privileges:true" in service.get("security_opt", [])
    assert service.get("read_only") is True
    user = str(service.get("user", "0"))
    assert user.split(":", maxsplit=1)[0] not in {"0", "root"}


@pytest.mark.parametrize("name", sorted(SERVICES))
def test_no_docker_socket(name: str) -> None:
    for volume in SERVICES[name].get("volumes", []):
        text = volume if isinstance(volume, str) else str(volume.get("source", ""))
        assert "docker.sock" not in text


@pytest.mark.parametrize("name", sorted(SERVICES))
def test_memory_limited(name: str) -> None:
    assert "mem_limit" in SERVICES[name]


def test_memory_budget_fits_dedicated_pi() -> None:
    total = sum(_megabytes(service["mem_limit"]) for service in SERVICES.values())
    assert total <= MEMORY_BUDGET_MB


def test_long_running_services_restart() -> None:
    for name, service in SERVICES.items():
        if service.get("restart") != "no":
            assert service.get("restart") == "unless-stopped", name
