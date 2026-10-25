"""Cache des pages coûteuses (tableau de bord, vue d'ensemble de l'API, abonnements) : un
résultat est réutilisé tant que la base n'a pas changé, et au plus `ttl`.

La « signature » de la base est la date de modification et la taille du fichier et de son journal
WAL : toute écriture (mise à jour bancaire, rapprochement, saisie) la change, une lecture non.
Une base sans fichier (tests en mémoire) n'est jamais mise en cache.
"""

import threading
from collections import OrderedDict
from collections.abc import Callable, Hashable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import Engine

Signature = tuple[tuple[int, int], ...]


def data_signature(engine: Engine) -> Signature | None:
    database = engine.url.database
    if not database:
        return None
    path = Path(database)
    stamps = []
    for candidate in (path, path.with_name(path.name + "-wal")):
        try:
            stat = candidate.stat()
        except FileNotFoundError:
            stamps.append((0, 0))
        else:
            stamps.append((stat.st_mtime_ns, stat.st_size))
    return tuple(stamps)


@dataclass(frozen=True)
class _Entry:
    signature: Signature
    stored_at: datetime
    value: Any


class DataCache:
    def __init__(self, *, ttl: timedelta = timedelta(minutes=10), size: int = 32) -> None:
        self.ttl = ttl
        self.size = size
        self._entries: OrderedDict[Hashable, _Entry] = OrderedDict()
        self._lock = threading.Lock()

    def get[T](
        self, engine: Engine, key: Hashable, compute: Callable[[], T], *, now: datetime
    ) -> T:
        signature = data_signature(engine)
        if signature is None:
            return compute()
        with self._lock:
            entry = self._entries.get(key)
            if (
                entry is not None
                and entry.signature == signature
                and now - entry.stored_at < self.ttl
            ):
                self._entries.move_to_end(key)
                value: T = entry.value
                return value
        value = compute()
        with self._lock:
            self._entries[key] = _Entry(signature, now, value)
            self._entries.move_to_end(key)
            while len(self._entries) > self.size:
                self._entries.popitem(last=False)
        return value
