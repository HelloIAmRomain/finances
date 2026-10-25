"""Ingestion des transactions (COL-03, COL-08, inv. 3 et 4).

- Chaque réponse brute est conservée, compressée puis chiffrée, une seule fois (empreinte).
- Clé de dédoublonnage (I-07), propre à la source : `ext:<source>:<identifiant externe>` si la
  banque en fournit un, sinon `fp:<source>:<empreinte>:<rang>` où le rang distingue des
  opérations identiques du même lot (deux cafés le même jour sont deux transactions).
- Insertion `ON CONFLICT DO NOTHING` sur (compte, clé) : rejouer un import n'ajoute rien.
- Tout ou rien : les transactions d'un lot sont insérées dans une seule transaction, après la
  lecture de toutes les pages ; une coupure en cours de lecture n'insère rien (le lot est marqué
  en échec et sera rejoué).
- Une transaction insérée n'est jamais modifiée (déclencheur `ledger_transaction_immutable`).
"""

import hashlib
import zlib
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import Engine, insert, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.connectors.base import Page, RawResponse, RemoteTransaction
from app.crypto import blind_index, decrypt_bytes, encrypt_bytes
from app.db import write_transaction
from app.iban import normalize_iban
from app.ledger.labels import clean_label
from app.models import ImportBatch, LedgerTransaction, RawPayload
from app.models.ledger import ImportSource, ImportStatus
from app.timeutil import utcnow

RAW_CONTEXT = "raw_payload.content"
IBAN_INDEX_CONTEXT = "iban"


@dataclass(frozen=True)
class IngestStats:
    batch_id: int
    seen: int
    inserted: int

    @property
    def duplicates(self) -> int:
        return self.seen - self.inserted


def fingerprint(tx: RemoteTransaction) -> str:
    material = "|".join(
        (tx.booking_date.isoformat(), str(tx.amount), tx.currency, clean_label(tx.label))
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def dedupe_keys(transactions: list[RemoteTransaction], source: ImportSource) -> list[str]:
    """Clés propres à une source : le même relevé réimporté donne les mêmes clés, mais deux
    sources ne se confondent jamais ici (les doublons entre sources passent par
    `duplicate_link`, avec l'API canonique, T1.16)."""
    ranks: Counter[str] = Counter()
    keys = []
    for tx in transactions:
        if tx.external_id:
            keys.append(f"ext:{source}:{tx.external_id}")
        else:
            fp = fingerprint(tx)
            ranks[fp] += 1
            keys.append(f"fp:{source}:{fp}:{ranks[fp]}")
    return keys


def ingest(
    engine: Engine,
    *,
    account_id: int,
    source: ImportSource,
    pages: Iterable[Page[RemoteTransaction]],
    file_name: str | None = None,
    profile_id: int | None = None,
    job_run_id: int | None = None,
) -> IngestStats:
    batch_id = _open_batch(
        engine,
        account_id=account_id,
        source=source,
        file_name=file_name,
        profile_id=profile_id,
        job_run_id=job_run_id,
    )
    try:
        items: list[tuple[RemoteTransaction, int]] = []
        for page in pages:
            raw_id = store_raw_payload(engine, batch_id, source, page.raw)
            items.extend((tx, raw_id) for tx in page.items)
        inserted = _insert_transactions(engine, account_id, batch_id, source, items)
    except BaseException:
        _close_batch(engine, batch_id, ImportStatus.FAILED)
        raise
    stats = IngestStats(batch_id=batch_id, seen=len(items), inserted=inserted)
    _close_batch(engine, batch_id, ImportStatus.SUCCESS, stats)
    return stats


def load_raw_payloads(engine: Engine, batch_id: int) -> list[tuple[bytes, str]]:
    """Réponses brutes d'un lot, déchiffrées et décompressées, pour rejouer un import."""
    with engine.connect() as connection:
        rows = connection.execute(
            select(RawPayload.content_enc, RawPayload.content_type)
            .where(RawPayload.import_batch_id == batch_id)
            .order_by(RawPayload.id)
        ).all()
    return [
        (zlib.decompress(decrypt_bytes(content, context=RAW_CONTEXT)), content_type)
        for content, content_type in rows
    ]


def _open_batch(
    engine: Engine,
    *,
    account_id: int,
    source: ImportSource,
    file_name: str | None,
    profile_id: int | None,
    job_run_id: int | None,
) -> int:
    with write_transaction(engine) as connection:
        result = connection.execute(
            insert(ImportBatch).values(
                account_id=account_id,
                source=source,
                profile_id=profile_id,
                file_name=file_name,
                started_at=utcnow(),
                status=ImportStatus.RUNNING,
                job_run_id=job_run_id,
            )
        )
        key = result.inserted_primary_key
        if key is None:
            raise RuntimeError("identifiant de lot non renvoyé")
        return int(key[0])


def store_raw_payload(
    engine: Engine, batch_id: int | None, source: ImportSource, raw: RawResponse
) -> int:
    """Conserve une réponse brute (compressée, chiffrée) une seule fois ; renvoie son id."""
    digest = hashlib.sha256(raw.content).hexdigest()
    with write_transaction(engine) as connection:
        existing = connection.execute(
            select(RawPayload.id).where(RawPayload.content_sha256 == digest)
        ).scalar_one_or_none()
        if existing is not None:
            return int(existing)
        result = connection.execute(
            insert(RawPayload).values(
                import_batch_id=batch_id,
                source=source,
                received_at=utcnow(),
                content_sha256=digest,
                content_type=raw.content_type,
                content_enc=encrypt_bytes(zlib.compress(raw.content, 9), context=RAW_CONTEXT),
            )
        )
        key = result.inserted_primary_key
        if key is None:
            raise RuntimeError("identifiant de donnée brute non renvoyé")
        return int(key[0])


def _insert_transactions(
    engine: Engine,
    account_id: int,
    batch_id: int,
    source: ImportSource,
    items: list[tuple[RemoteTransaction, int]],
) -> int:
    if not items:
        return 0
    keys = dedupe_keys([tx for tx, _ in items], source)
    now = utcnow()
    rows = [
        {
            "account_id": account_id,
            "dedupe_key": key,
            "external_id": tx.external_id,
            "fingerprint": fingerprint(tx),
            "occurrence": int(key.rsplit(":", 1)[1]) if key.startswith("fp:") else 1,
            "booking_date": tx.booking_date,
            "value_date": tx.value_date,
            "amount_cents": tx.amount,
            "currency": tx.currency,
            "counterparty_name_enc": tx.counterparty_name,
            "counterparty_iban_enc": tx.counterparty_iban,
            "counterparty_iban_hash": (
                blind_index(normalize_iban(tx.counterparty_iban), context=IBAN_INDEX_CONTEXT)
                if tx.counterparty_iban
                else None
            ),
            "label_raw": tx.label,
            "label_clean": clean_label(tx.label),
            "bank_tx_code": tx.bank_tx_code,
            "raw_payload_id": raw_id,
            "import_batch_id": batch_id,
            "created_at": now,
        }
        for (tx, raw_id), key in zip(items, keys, strict=True)
    ]
    statement = sqlite_insert(LedgerTransaction).on_conflict_do_nothing(
        index_elements=["account_id", "dedupe_key"]
    )
    inserted = 0
    with write_transaction(engine) as connection:
        for row in rows:
            inserted += connection.execute(statement, row).rowcount
    return inserted


def _close_batch(
    engine: Engine, batch_id: int, status: ImportStatus, stats: IngestStats | None = None
) -> None:
    values: dict[str, object] = {"status": status, "finished_at": utcnow()}
    if stats is not None:
        values |= {
            "n_seen": stats.seen,
            "n_inserted": stats.inserted,
            "n_duplicates": stats.duplicates,
        }
    with write_transaction(engine) as connection:
        connection.execute(update(ImportBatch).where(ImportBatch.id == batch_id).values(**values))
