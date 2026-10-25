"""Adaptateur Enable Banking vers l'interface commune des connecteurs (NF-EXP-03)."""

from collections.abc import Callable, Iterator
from datetime import date

from app.connectors.base import (
    AccountInaccessibleError,
    Page,
    RawResponse,
    RemoteBalance,
    RemoteTransaction,
)
from app.connectors.enable_banking.client import EnableBankingClient, EnableBankingError, RawJson
from app.connectors.enable_banking.mapping import to_balances, to_transactions
from app.connectors.requester import Budget
from app.models.ledger import ImportSource

NOT_ACCESSIBLE = "ASPSP_ACCOUNT_NOT_ACCESSIBLE"


def _raw(page: RawJson) -> RawResponse:
    return RawResponse(page.content, "application/json")


def _call[T](request: Callable[[], T]) -> T:
    try:
        return request()
    except EnableBankingError as exc:
        if exc.code == NOT_ACCESSIBLE:
            raise AccountInaccessibleError(str(exc), exc.status, exc.code) from exc
        raise


class EnableBankingConnector:
    source = ImportSource.ENABLE_BANKING

    def __init__(self, client: EnableBankingClient) -> None:
        self._client = client

    def balances(self, account_uid: str, budget: Budget, *, today: date) -> Page[RemoteBalance]:
        page = _call(lambda: self._client.balances(account_uid, budget))
        return Page(to_balances(page.document, fallback_day=today), _raw(page))

    def transactions(
        self, account_uid: str, date_from: date, budget: Budget
    ) -> Iterator[Page[RemoteTransaction]]:
        pages = self._client.transactions(account_uid, date_from, budget)
        while True:
            page = _call(lambda: next(pages, None))
            if page is None:
                return
            yield Page(to_transactions(page.document), _raw(page))
