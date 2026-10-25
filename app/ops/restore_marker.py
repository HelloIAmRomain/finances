"""Après une restauration de la base (PLAN §1.5) : les requêtes bancaires des 15 dernières
minutes avant la panne sont inconnues. `restore.sh` dépose un marqueur ; au démarrage, le worker
réserve une requête par compte et par endpoint dans le budget DSP2 (inv. 10), puis l'efface."""

import logging
from datetime import datetime
from pathlib import Path

from sqlalchemy import Engine

from app.connectors.budget import reserve_after_restore
from app.ledger.sync import linked_accounts

log = logging.getLogger(__name__)

MARKER = ".restored-at"
ENDPOINTS = ("balances", "transactions")


def apply_restore_marker(engine: Engine, data_dir: Path, *, now: datetime) -> bool:
    marker = data_dir / MARKER
    if not marker.exists():
        return False
    accounts = [account_id for account_id, _ in linked_accounts(engine)]
    reserve_after_restore(engine, account_ids=accounts, endpoints=ENDPOINTS, now=now)
    marker.unlink()
    log.warning("base restaurée : budget DSP2 réservé pour %d compte(s)", len(accounts))
    return True
