"""Ingestion : données brutes, normalisation, dédoublonnage (COL-03, COL-08, inv. 3 et 4)."""

from collections.abc import Iterator
from dataclasses import replace
from datetime import date

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import Engine, delete, func, select

from app.connectors.base import Page, RawResponse, RemoteTransaction
from app.crypto import blind_index
from app.iban import normalize_iban
from app.ledger.ingest import dedupe_keys, fingerprint, ingest, load_raw_payloads
from app.models import ImportBatch, LedgerTransaction, RawPayload
from app.models.ledger import ImportSource, ImportStatus
from app.money import Cents
from tests.factories import fake_iban, fake_name


def _tx(
    number: int,
    *,
    external_id: str | None = None,
    amount: int = -250,
    label: str = "CB CAFE DU PORT",
    day: int = 5,
) -> RemoteTransaction:
    return RemoteTransaction(
        external_id=external_id,
        booking_date=date(2026, 9, day),
        value_date=date(2026, 9, day),
        amount=Cents(amount),
        currency="EUR",
        label=label,
        counterparty_name=fake_name(number),
        counterparty_iban=fake_iban(number),
        bank_tx_code=None,
    )


def _page(items: list[RemoteTransaction], tag: str = "p") -> Page[RemoteTransaction]:
    return Page(items, RawResponse(f"brut-{tag}-{len(items)}".encode(), "application/json"))


def _rows(engine: Engine) -> set[tuple[str, int, date, str]]:
    with engine.connect() as connection:
        rows = connection.execute(
            select(
                LedgerTransaction.dedupe_key,
                LedgerTransaction.amount_cents,
                LedgerTransaction.booking_date,
                LedgerTransaction.label_clean,
            )
        )
        return {tuple(row) for row in rows}  # type: ignore[misc]


def test_dedupe_keys_use_external_id_then_fingerprint_rank() -> None:
    coffee = _tx(1)
    keys = dedupe_keys(
        [_tx(1, external_id="A1"), coffee, coffee, _tx(2, label="CB PAIN")], ImportSource.CSV
    )
    fp_coffee = fingerprint(coffee)
    assert keys == [
        "ext:csv:A1",
        f"fp:csv:{fp_coffee}:1",
        f"fp:csv:{fp_coffee}:2",  # deux cafés identiques le même jour : deux transactions (I-07)
        f"fp:csv:{fingerprint(_tx(2, label='CB PAIN'))}:1",
    ]


def test_same_operation_from_two_sources_is_kept_twice(engine: Engine) -> None:
    """Deux sources ne se confondent jamais à l'ingestion : le lien passe par duplicate_link."""
    for source in (ImportSource.CSV, ImportSource.ENABLE_BANKING):
        ingest(engine, account_id=1, source=source, pages=[_page([_tx(1, external_id="R1")])])
    with engine.connect() as connection:
        assert (
            connection.execute(select(func.count()).select_from(LedgerTransaction)).scalar_one()
            == 2
        )


def test_fingerprint_ignores_cosmetic_label_changes() -> None:
    assert fingerprint(_tx(1, label="cb  cafe du port")) == fingerprint(_tx(1))
    assert fingerprint(_tx(1, amount=-251)) != fingerprint(_tx(1))


def test_two_identical_purchases_same_day_are_both_kept(engine: Engine) -> None:
    stats = ingest(engine, account_id=1, source=ImportSource.CSV, pages=[_page([_tx(1), _tx(1)])])
    assert stats.inserted == 2
    assert len(_rows(engine)) == 2


def test_same_file_imported_twice_creates_no_duplicate(engine: Engine) -> None:
    """Scénario d'acceptation du CDC : « Même fichier CSV importé deux fois »."""
    pages = [_page([_tx(1), _tx(2, label="CB PAIN"), _tx(3, external_id="X")])]
    first = ingest(engine, account_id=1, source=ImportSource.CSV, pages=pages)
    second = ingest(engine, account_id=1, source=ImportSource.CSV, pages=pages)
    assert (first.inserted, first.duplicates) == (3, 0)
    assert (second.inserted, second.duplicates) == (0, 3)
    assert len(_rows(engine)) == 3
    with engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(RawPayload)).scalar_one() == 1
        statuses = connection.scalars(select(ImportBatch.status)).all()
    assert statuses == [ImportStatus.SUCCESS, ImportStatus.SUCCESS]


def test_later_fetch_with_more_identical_purchases_adds_only_new(engine: Engine) -> None:
    ingest(engine, account_id=1, source=ImportSource.ENABLE_BANKING, pages=[_page([_tx(1)])])
    stats = ingest(
        engine, account_id=1, source=ImportSource.ENABLE_BANKING, pages=[_page([_tx(1), _tx(1)])]
    )
    assert stats.inserted == 1
    assert len(_rows(engine)) == 2


def test_same_key_on_other_account_is_distinct(engine: Engine) -> None:
    for account_id in (1, 2):
        ingest(engine, account_id=account_id, source=ImportSource.CSV, pages=[_page([_tx(1)])])
    assert len(_rows(engine)) == 1  # même contenu (clé), mais…
    with engine.connect() as connection:
        assert (
            connection.execute(select(func.count()).select_from(LedgerTransaction)).scalar_one()
            == 2
        )


def test_sensitive_fields_encrypted_and_indexed(engine: Engine) -> None:
    ingest(engine, account_id=1, source=ImportSource.CSV, pages=[_page([_tx(7)])])
    with engine.connect() as connection:
        raw = connection.exec_driver_sql(
            "SELECT counterparty_iban_enc, counterparty_name_enc FROM ledger_transaction"
        ).one()
        loaded = connection.execute(
            select(
                LedgerTransaction.counterparty_iban_enc, LedgerTransaction.counterparty_iban_hash
            )
        ).one()
    assert fake_iban(7).encode() not in raw[0]
    assert fake_name(7).encode() not in raw[1]
    assert loaded[0] == fake_iban(7)
    assert loaded[1] == blind_index(normalize_iban(fake_iban(7)), context="iban")


def test_raw_payload_is_compressed_encrypted_and_replayable(engine: Engine) -> None:
    content = b'{"transactions": ["' + b"x" * 5000 + b'"]}'
    page = Page([_tx(1)], RawResponse(content, "application/json"))
    stats = ingest(engine, account_id=1, source=ImportSource.ENABLE_BANKING, pages=[page])
    with engine.connect() as connection:
        stored: bytes = connection.exec_driver_sql(
            "SELECT content_enc FROM raw_payload"
        ).scalar_one()
    assert b"xxxx" not in stored
    assert len(stored) < len(content) / 5  # compressé avant chiffrement
    assert load_raw_payloads(engine, stats.batch_id) == [(content, "application/json")]


def test_failure_marks_batch_failed_and_inserts_nothing(engine: Engine) -> None:
    def pages() -> Iterator[Page[RemoteTransaction]]:
        yield _page([_tx(1)])
        raise ConnectionError("coupure pendant la lecture de la page 2")

    with pytest.raises(ConnectionError):
        ingest(engine, account_id=1, source=ImportSource.ENABLE_BANKING, pages=pages())
    assert _rows(engine) == set()
    with engine.connect() as connection:
        assert connection.scalars(select(ImportBatch.status)).all() == [ImportStatus.FAILED]


transactions = st.lists(
    st.builds(
        _tx,
        st.integers(min_value=1, max_value=3),
        external_id=st.one_of(st.none(), st.sampled_from(["A", "B", "C"])),
        amount=st.sampled_from([-250, -1000, 120000]),
        label=st.sampled_from(["CB CAFE", "VIR LOYER", "PRLV EDF"]),
        day=st.integers(min_value=1, max_value=3),
    ),
    max_size=12,
)


@settings(max_examples=40, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(items=transactions, repeats=st.integers(min_value=1, max_value=3), data=st.data())
def test_reingestion_is_idempotent_whatever_the_order(
    engine: Engine, items: list[RemoteTransaction], repeats: int, data: st.DataObject
) -> None:
    """Ingérer N fois le même relevé, dans n'importe quel ordre, donne le même ensemble."""
    # Une banque ne réutilise jamais un identifiant pour deux opérations différentes.
    items = [
        replace(tx, external_id=f"{tx.external_id}{i}") if tx.external_id else tx
        for i, tx in enumerate(items)
    ]
    with engine.begin() as connection:
        connection.execute(delete(LedgerTransaction))
    ingest(engine, account_id=1, source=ImportSource.CSV, pages=[_page(items, "ref")])
    reference = _rows(engine)
    for index in range(repeats):
        shuffled = data.draw(st.permutations(items))
        ingest(engine, account_id=1, source=ImportSource.CSV, pages=[_page(shuffled, str(index))])
    assert _rows(engine) == reference
