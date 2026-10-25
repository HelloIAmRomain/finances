"""Contrat entre le serveur et l'application Android : les réponses de référence des tests
Android (android/app/src/sharedTest/resources/api) doivent être valides pour les modèles de
l'API, sans champ inconnu ni manquant. Si l'API change, ce test échoue avant l'application."""

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, TypeAdapter

from app.web.api import (
    AccountOut,
    AlertOut,
    AssistantOut,
    CategoryOut,
    ConversationDetailOut,
    ConversationOut,
    DashboardOut,
    PairingResponse,
    RentalOut,
    TransactionsOut,
    TurnOut,
    TurnStartOut,
)

ANDROID = Path(__file__).resolve().parents[2] / "android"
FIXTURES = ANDROID / "app" / "src" / "sharedTest" / "resources" / "api"
CONTRACT: dict[str, Any] = {
    "tableau-de-bord": DashboardOut,
    "comptes": list[AccountOut],
    "transactions": TransactionsOut,
    "categories": list[CategoryOut],
    "loyers": RentalOut,
    "alertes": list[AlertOut],
    "appairage": PairingResponse,
    "assistant": AssistantOut,
    "conversations": list[ConversationOut],
    "conversation": ConversationDetailOut,
    "assistant-envoi": TurnStartOut,
    "assistant-tour": TurnOut,
}


def _models(annotation: Any) -> list[type[BaseModel]]:
    """Modèles pydantic imbriqués dans une annotation (listes, champs)."""
    found: list[type[BaseModel]] = []
    for arg in getattr(annotation, "__args__", ()):
        found += _models(arg)
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        found.append(annotation)
    return found


def _check_keys(value: Any, annotation: Any, where: str) -> None:
    if isinstance(value, list):
        inner = getattr(annotation, "__args__", (annotation,))[0]
        for item in value:
            _check_keys(item, inner, where)
        return
    models = _models(annotation)
    if not isinstance(value, dict) or not models:
        return
    model = models[-1]
    assert set(value) == set(model.model_fields), (
        f"{where} : {set(value) ^ set(model.model_fields)}"
    )
    for key, field in model.model_fields.items():
        _check_keys(value[key], field.annotation, f"{where}.{key}")


def test_every_endpoint_has_a_reference_response() -> None:
    assert {p.stem for p in FIXTURES.glob("*.json")} == set(CONTRACT)


@pytest.mark.parametrize("name", sorted(CONTRACT))
def test_reference_responses_match_the_api(name: str) -> None:
    document = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    TypeAdapter(CONTRACT[name]).validate_python(document)
    _check_keys(document, CONTRACT[name], name)
