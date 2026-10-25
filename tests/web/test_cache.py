"""Cache du tableau de bord : réutilisé tant que la base ne change pas, jamais périmé."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import Engine, create_engine, insert

from app.models.ops import AuditLog
from app.web.cache import DataCache, data_signature
from tests.web.conftest import Web

NOW = datetime(2026, 10, 7, 8, 0, tzinfo=UTC)


class Counter:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> int:
        self.calls += 1
        return self.calls


def _write(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(insert(AuditLog).values(at=NOW, action="essai.cache", details={}))


def test_reused_until_the_database_changes(engine: Engine) -> None:
    cache, compute = DataCache(), Counter()
    assert cache.get(engine, "k", compute, now=NOW) == 1
    assert cache.get(engine, "k", compute, now=NOW + timedelta(minutes=1)) == 1
    _write(engine)
    assert cache.get(engine, "k", compute, now=NOW + timedelta(minutes=2)) == 2  # écriture


def test_expires_and_evicts(engine: Engine) -> None:
    cache, compute = DataCache(ttl=timedelta(minutes=10), size=2), Counter()
    cache.get(engine, "a", compute, now=NOW)
    assert cache.get(engine, "a", compute, now=NOW + timedelta(minutes=10)) == 2  # délai écoulé
    cache.get(engine, "b", compute, now=NOW)
    cache.get(engine, "c", compute, now=NOW)  # « a » sort du cache (le plus ancien)
    assert cache.get(engine, "a", compute, now=NOW) == 5


def test_missing_files_have_an_empty_signature(tmp_path: Path) -> None:
    absent = create_engine(f"sqlite:///{tmp_path / 'absente.db'}")
    assert data_signature(absent) == ((0, 0), (0, 0))


def test_no_file_no_cache() -> None:
    memory = create_engine("sqlite://")
    assert data_signature(memory) is None
    cache, compute = DataCache(), Counter()
    cache.get(memory, "k", compute, now=NOW)
    assert cache.get(memory, "k", compute, now=NOW) == 2


def test_dashboard_shows_new_data_immediately(web: Web) -> None:
    """Régression possible du cache : une opération ajoutée apparaît au rechargement."""
    from datetime import date  # noqa: PLC0415

    from tests.web.test_rental import _tx  # noqa: PLC0415

    web.login()
    before = web.client.get("/").text
    _tx(web.engine, -12345, "ACHAT TEST CACHE", date(2026, 9, 20))
    after = web.client.get("/").text
    assert before != after and "123,45" in after
