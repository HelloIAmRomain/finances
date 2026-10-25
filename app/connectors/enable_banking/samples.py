"""Validation de la conversion Enable Banking sur les vraies réponses stockées (porte du lot 1).

- `conversion_report` : passe **toutes** les réponses brutes conservées (COL-08) dans la
  conversion et n'en sort que des statistiques (comptes, codes, champs présents) : rien de
  personnel, exécutable sur la machine de production.
- `anonymized_samples` : un échantillon par banque et par type de compte (soldes + première page
  de transactions), anonymisé par `anonymize` avant de sortir de la base.
"""

import json
import zlib
from collections import Counter
from typing import Any

from sqlalchemy import Engine, func, select

from app.connectors.enable_banking.anonymize import Anonymizer
from app.connectors.enable_banking.mapping import to_balances, to_transactions
from app.crypto import decrypt_bytes
from app.ledger.ingest import RAW_CONTEXT
from app.models import (
    Account,
    BalanceSnapshot,
    Connection,
    ImportBatch,
    LedgerTransaction,
    RawPayload,
)
from app.models.ledger import ImportSource
from app.timeutil import utcnow


def _document(content: bytes) -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads(
        zlib.decompress(decrypt_bytes(content, context=RAW_CONTEXT))
    )
    return parsed


def _payload_owners(engine: Engine) -> dict[int, tuple[str, str, int]]:
    """raw_payload → (banque, type de compte, compte)."""
    owners: dict[int, tuple[str, str, int]] = {}
    with engine.connect() as connection:
        for raw_id, bank, kind, account_id in connection.execute(
            select(RawPayload.id, Connection.institution_name, Account.type, Account.id)
            .join(ImportBatch, ImportBatch.id == RawPayload.import_batch_id)
            .join(Account, Account.id == ImportBatch.account_id)
            .join(Connection, Connection.id == Account.connection_id)
        ):
            owners[raw_id] = (bank, str(kind), account_id)
        for balance_raw_id, bank, kind, account_id in connection.execute(
            select(
                BalanceSnapshot.raw_payload_id,
                Connection.institution_name,
                Account.type,
                Account.id,
            )
            .join(Account, Account.id == BalanceSnapshot.account_id)
            .join(Connection, Connection.id == Account.connection_id)
            .where(BalanceSnapshot.raw_payload_id.is_not(None))
        ):
            if balance_raw_id is not None:
                owners[balance_raw_id] = (bank, str(kind), account_id)
    return owners


def _raw_payloads(engine: Engine) -> list[tuple[int, bytes]]:
    with engine.connect() as connection:
        return [
            (raw_id, content)
            for raw_id, content in connection.execute(
                select(RawPayload.id, RawPayload.content_enc)
                .where(RawPayload.source == ImportSource.ENABLE_BANKING)
                .order_by(RawPayload.id)
            )
        ]


def _tally_balances(stats: dict[str, Any], document: dict[str, Any]) -> None:
    stats["reponses_soldes"] += 1
    for item in document.get("balances", []):
        stats["codes_solde"][str(item.get("balance_type"))] += 1
    try:
        to_balances(document, fallback_day=utcnow().date())
    except Exception as exc:  # on compte, on n'arrête pas
        stats["erreurs"][f"soldes:{type(exc).__name__}"] += 1


def _tally_transactions(stats: dict[str, Any], document: dict[str, Any]) -> None:
    stats["reponses_transactions"] += 1
    items = document.get("transactions", [])
    stats["transactions"] += len(items)
    for item in items:
        stats["statuts"][str(item.get("status"))] += 1
        if item.get("status") == "BOOK" and item.get("entry_reference"):
            stats["references"].add(str(item["entry_reference"]))
        stats["sens"][str(item.get("credit_debit_indicator"))] += 1
        for field, value in item.items():
            if value not in (None, "", [], {}):
                stats["champs_transaction"][field] += 1
    try:
        stats["converties"] += len(to_transactions(document))
    except Exception as exc:  # on compte, on n'arrête pas
        stats["erreurs"][f"transactions:{type(exc).__name__}"] += 1


def conversion_report(engine: Engine) -> dict[str, Any]:
    owners = _payload_owners(engine)
    report: dict[str, Any] = {}
    for raw_id, content in _raw_payloads(engine):
        bank, kind, _ = owners.get(raw_id, ("inconnue", "inconnu", 0))
        stats = report.setdefault(
            bank,
            {
                "types_de_compte": Counter(),
                "reponses_soldes": 0,
                "reponses_transactions": 0,
                "transactions": 0,
                "converties": 0,
                "statuts": Counter(),
                "references": set(),
                "sens": Counter(),
                "codes_solde": Counter(),
                "champs_transaction": Counter(),
                "erreurs": Counter(),
            },
        )
        stats["types_de_compte"][kind] += 1
        try:
            document = _document(content)
        except Exception as exc:
            stats["erreurs"][f"lecture:{type(exc).__name__}"] += 1
            continue
        if "balances" in document:
            _tally_balances(stats, document)
        if "transactions" in document:
            _tally_transactions(stats, document)
    with engine.connect() as connection:
        stored = connection.execute(
            select(Connection.institution_name, func.count(LedgerTransaction.id))
            .join(Account, Account.connection_id == Connection.id)
            .join(LedgerTransaction, LedgerTransaction.account_id == Account.id)
            .group_by(Connection.institution_name)
        ).all()
    for bank, count in stored:
        report.setdefault(bank, {})["en_base"] = int(count)
    for stats in report.values():
        # Opérations distinctes vues (par référence) : doit égaler « en_base » si le
        # dédoublonnage est juste (pages relues à chaque synchro).
        stats["operations_distinctes"] = len(stats.pop("references", set()))
    return {
        bank: {
            key: dict(value) if isinstance(value, Counter) else value for key, value in s.items()
        }
        for bank, s in report.items()
    }


def anonymized_samples(engine: Engine, *, max_transactions: int = 60) -> dict[str, Any]:
    """{banque: {type de compte: {"soldes": ..., "transactions": ...}}}, anonymisé."""
    owners = _payload_owners(engine)
    anonymizer = Anonymizer()
    samples: dict[str, Any] = {}
    for raw_id, content in reversed(_raw_payloads(engine)):  # les plus récentes d'abord
        owner = owners.get(raw_id)
        if owner is None:
            continue
        bank, kind, _ = owner
        slot = samples.setdefault(bank, {}).setdefault(kind, {})
        document = _document(content)
        if "balances" in document and "soldes" not in slot:
            slot["soldes"] = anonymizer(document)
        elif "transactions" in document and "transactions" not in slot:
            trimmed = {**document, "transactions": document["transactions"][:max_transactions]}
            slot["transactions"] = anonymizer(trimmed)
    return samples
