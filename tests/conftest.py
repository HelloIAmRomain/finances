import base64
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from hypothesis import HealthCheck, settings
from sqlalchemy import Engine, text

import app.models  # noqa: F401
from app.crypto import Keyring, configure_keyring, reset_keyring
from app.db import Base, make_engine

# Sans délai par exemple : beaucoup de propriétés écrivent en base, leur durée dépend de la
# charge de la machine (un dépassement ponctuel ferait échouer un test correct).
settings.register_profile("dev", max_examples=50, deadline=None)
settings.register_profile(
    "ci",
    max_examples=300,
    derandomize=True,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))


@pytest.fixture(autouse=True)
def keyring() -> Iterator[None]:
    """Trousseau de clés jetable pour chaque test (colonnes chiffrées)."""
    configure_keyring(
        Keyring.from_base64(
            data_key=base64.b64encode(os.urandom(32)).decode(),
            index_key=base64.b64encode(os.urandom(32)).decode(),
        )
    )
    yield
    reset_keyring()


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    """Base vide créée depuis les modèles, avec deux comptes (id 1 et 2)."""
    engine = make_engine(tmp_path / "ledger.db")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        for account_id in (1, 2):
            connection.execute(
                text(
                    "INSERT INTO account (id, name, type, currency, include_in_net_worth,"
                    " syncs_per_day) VALUES (:id, 'Compte', 'checking', 'EUR', 1, 4)"
                ),
                {"id": account_id},
            )
    return engine


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--e2e",
        action="store_true",
        help="lancer aussi les tests de bout en bout (navigateur, tests/e2e)",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Les tests de bout en bout (vrai navigateur, vrai serveur HTTPS local) ne tournent que sur
    demande (`--e2e`) : ils demandent Chromium et ouvrent des sockets locales."""
    run_e2e = config.getoption("--e2e")
    for item in items:
        if "e2e" in item.path.parts:
            item.add_marker(pytest.mark.e2e)
            item.add_marker(pytest.mark.enable_socket)
            if not run_e2e:
                item.add_marker(pytest.mark.skip(reason="tests de bout en bout : option --e2e"))
