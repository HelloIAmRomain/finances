"""Chargement des scénarios YAML (tests/scenarios/<thème>/*.yaml), partagé avec le lot 2."""

from pathlib import Path
from typing import Any

import pytest
import yaml

SCENARIOS = Path(__file__).resolve().parent


def load(theme: str) -> list[Any]:
    """Un paramètre pytest par fichier, identifié par son nom."""
    params = []
    for path in sorted((SCENARIOS / theme).glob("*.yaml")):
        scenario = yaml.safe_load(path.read_text(encoding="utf-8"))
        params.append(pytest.param(scenario, id=path.stem))
    return params
