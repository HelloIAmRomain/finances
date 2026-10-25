"""Faux connecteur bancaire en mémoire (NF-EXP-03) : aucune requête réseau.

Implémente l'interface commune (`app.connectors.base.BankConnector`) : la synchronisation doit
fonctionner avec lui comme avec Enable Banking. Simule pagination, pannes à une page donnée et
réponses brutes (COL-08). Le budget DSP2 n'est pas consommé (aucune requête réelle).
"""

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date

from app.connectors.base import Page, RawResponse, RemoteBalance, RemoteTransaction
from app.connectors.requester import Budget
from app.models.ledger import ImportSource


class InjectedFailureError(RuntimeError):
    """Panne simulée (coupure réseau, courant…) au milieu d'une synchro."""


@dataclass
class FakeBankConnector:
    balance_items: dict[str, list[RemoteBalance]] = field(default_factory=dict)
    transaction_items: dict[str, list[RemoteTransaction]] = field(default_factory=dict)
    page_size: int = 50
    fail_on_page: int | None = None  # index (à partir de 0) de la page qui échoue
    calls: list[str] = field(default_factory=list)

    source: ImportSource = ImportSource.ENABLE_BANKING

    def balances(self, account_uid: str, budget: Budget, *, today: date) -> Page[RemoteBalance]:
        self.calls.append(f"balances:{account_uid}")
        items = self.balance_items.get(account_uid, [])
        return Page(items, _raw({"balances": [str(b.amount) for b in items]}))

    def transactions(
        self, account_uid: str, date_from: date, budget: Budget
    ) -> Iterator[Page[RemoteTransaction]]:
        items = [
            t for t in self.transaction_items.get(account_uid, []) if t.booking_date >= date_from
        ]
        for index, start in enumerate(range(0, max(len(items), 1), self.page_size)):
            self.calls.append(f"transactions:{account_uid}:{index}")
            if self.fail_on_page == index:
                raise InjectedFailureError(f"panne simulée à la page {index}")
            chunk = items[start : start + self.page_size]
            yield Page(chunk, _raw({"page": index, "ids": [t.external_id for t in chunk]}))


def _raw(document: object) -> RawResponse:
    return RawResponse(json.dumps(document).encode(), "application/json")
