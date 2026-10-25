"""Doublons entre sources, appariement automatique (Q5, T1.16a) : scénarios YAML."""

import time
from datetime import date, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, func, select, update

from app.connectors.base import Page, RawResponse, RemoteTransaction
from app.ledger.cross_dedupe import detect_duplicates
from app.ledger.ingest import ingest
from app.models import AuditLog, DuplicateLink, LedgerTransaction, ReviewItem
from app.models.ledger import DuplicateStatus, ImportSource
from app.money import Cents
from tests.scenarios.runner import load


def _import(engine: Engine, source: str, transactions: list[dict[str, Any]]) -> dict[str, int]:
    remote = [
        RemoteTransaction(
            external_id=t.get("external_id"),
            booking_date=t["date"]
            if isinstance(t["date"], date)
            else date.fromisoformat(t["date"]),
            value_date=None,
            amount=Cents(t["amount"]),
            currency="EUR",
            label=t["label"],
            counterparty_name=None,
            counterparty_iban=None,
            bank_tx_code=None,
        )
        for t in transactions
    ]
    raw = RawResponse(repr(transactions).encode(), "application/json")
    stats = ingest(engine, account_id=1, source=ImportSource(source), pages=[Page(remote, raw)])
    with engine.connect() as connection:
        ids = connection.scalars(
            select(LedgerTransaction.id)
            .where(LedgerTransaction.import_batch_id == stats.batch_id)
            .order_by(LedgerTransaction.id)
        ).all()
    assert len(ids) == len(transactions), "le scénario suppose des transactions distinctes"
    return {t["id"]: tx_id for t, tx_id in zip(transactions, ids, strict=True)}


def _links(engine: Engine, names: dict[int, str]) -> list[dict[str, str]]:
    with engine.connect() as connection:
        links = connection.execute(select(DuplicateLink).order_by(DuplicateLink.id)).all()
    return [
        {
            "canonical": names[link.canonical_tx_id],
            "duplicate": names[link.duplicate_tx_id],
            "reason": str(link.reason),
            "status": str(link.status),
            "mode": str(link.mode),
        }
        for link in links
    ]


def _count(engine: Engine, model: type[Any]) -> int:
    with engine.connect() as connection:
        return int(connection.execute(select(func.count()).select_from(model)).scalar_one())


@pytest.mark.parametrize("scenario", load("dedupe"))
def test_scenario(engine: Engine, scenario: dict[str, Any]) -> None:
    ids: dict[str, int] = {}
    for batch in scenario["imports"]:
        ids |= _import(engine, batch["source"], batch["transactions"])
    detect_duplicates(engine, account_id=1)
    names = {tx_id: name for name, tx_id in ids.items()}
    assert _links(engine, names) == scenario["expected_links"]
    assert _count(engine, ReviewItem) == scenario.get("expected_review_items", 0)


@pytest.mark.parametrize("scenario", load("dedupe"))
def test_detection_is_idempotent(engine: Engine, scenario: dict[str, Any]) -> None:
    for batch in scenario["imports"]:
        _import(engine, batch["source"], batch["transactions"])
    detect_duplicates(engine, account_id=1)
    before = (_count(engine, DuplicateLink), _count(engine, ReviewItem), _count(engine, AuditLog))
    detect_duplicates(engine, account_id=1)
    assert (
        _count(engine, DuplicateLink),
        _count(engine, ReviewItem),
        _count(engine, AuditLog),
    ) == before


def test_reimporting_the_same_file_changes_nothing(engine: Engine) -> None:
    scenario = next(p.values[0] for p in load("dedupe") if p.id == "01_csv_chevauche_api")
    for batch in scenario["imports"]:
        _import(engine, batch["source"], batch["transactions"])
    detect_duplicates(engine, account_id=1)
    csv_batch = scenario["imports"][1]
    remote_again = [dict(t) for t in csv_batch["transactions"]]
    raw = RawResponse(repr(remote_again).encode(), "application/json")
    ingest(
        engine,
        account_id=1,
        source=ImportSource.CSV,
        pages=[
            Page(
                [
                    RemoteTransaction(
                        external_id=None,
                        booking_date=t["date"],
                        value_date=None,
                        amount=Cents(t["amount"]),
                        currency="EUR",
                        label=t["label"],
                        counterparty_name=None,
                        counterparty_iban=None,
                        bank_tx_code=None,
                    )
                    for t in remote_again
                ],
                raw,
            )
        ],
    )
    detect_duplicates(engine, account_id=1)
    assert _count(engine, DuplicateLink) == 2


def test_rejected_link_is_never_proposed_again(engine: Engine) -> None:
    scenario = next(p.values[0] for p in load("dedupe") if p.id == "04_decalage_de_dates")
    for batch in scenario["imports"]:
        _import(engine, batch["source"], batch["transactions"])
    detect_duplicates(engine, account_id=1)
    with engine.begin() as connection:
        connection.execute(update(DuplicateLink).values(status=DuplicateStatus.REJECTED))
    detect_duplicates(engine, account_id=1)
    assert _count(engine, DuplicateLink) == 1


def test_automatic_links_are_audited(engine: Engine) -> None:
    scenario = next(p.values[0] for p in load("dedupe") if p.id == "01_csv_chevauche_api")
    for batch in scenario["imports"]:
        _import(engine, batch["source"], batch["transactions"])
    detect_duplicates(engine, account_id=1)
    with engine.connect() as connection:
        actions = connection.scalars(select(AuditLog.action)).all()
    assert actions == ["duplicate.auto_confirmed", "duplicate.auto_confirmed"]


@pytest.mark.slow
def test_detection_stays_fast_on_a_long_history(engine: Engine) -> None:
    """Après chaque synchro : 20 000 opérations de la banque et 2 000 d'un relevé importé, en
    moins de 2 s ; l'ancienne version comparait chaque opération libre à toutes les autres."""
    start = date(2021, 1, 1)
    bank = [
        {"id": n, "date": start + timedelta(days=n // 10), "amount": -((n * 37) % 50_000) - 1,
         "label": f"CB {n}", "external_id": f"eb-{n}"}
        for n in range(20_000)
    ]  # fmt: skip
    statement = [
        {"id": 100_000 + n, "date": start + timedelta(days=n // 10 + 1),
         "amount": -((n * 41) % 50_000) - 7, "label": f"RELEVE {n}"}
        for n in range(2_000)
    ]  # fmt: skip
    _import(engine, "enable_banking", bank)
    _import(engine, "csv", statement)
    began = time.perf_counter()
    detect_duplicates(engine, account_id=1)
    assert time.perf_counter() - began < 2
