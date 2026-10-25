import importlib

import pytest

SUBPACKAGES = [
    "connectors",
    "ledger",
    "categorize",
    "rental",
    "alerts",
    "tax",
    "web",
    "ops",
    "net",
]


def test_app_importable() -> None:
    assert importlib.import_module("app").__doc__


@pytest.mark.parametrize("name", SUBPACKAGES)
def test_subpackage_importable(name: str) -> None:
    module = importlib.import_module(f"app.{name}")
    assert module.__doc__
