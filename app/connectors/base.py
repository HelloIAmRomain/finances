"""Interface commune des connecteurs bancaires (NF-EXP-03) : types normalisés et erreurs
indépendants du fournisseur.

Un connecteur ne connaît ni la base ni le grand livre : il renvoie des données normalisées
(montants déjà en centimes) accompagnées de la réponse brute, conservée pour l'audit (COL-08).
La synchronisation (`app.ledger.sync`) ne dépend que de cette interface : un autre agrégateur
s'ajoute par un nouvel adaptateur, sans toucher au reste.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from app.connectors.requester import Budget
from app.models.ledger import AccountType, BalanceType, ImportSource
from app.money import Cents


class ConnectorError(RuntimeError):
    """Refus de la source (agrégateur ou banque) : il ne changera pas en quelques minutes, la
    tâche n'est pas reprise sur le même créneau (`retryable`)."""

    retryable = False

    def __init__(self, message: str, status: int | None = None, code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class ConsentExpiredError(ConnectorError):
    """Consentement expiré ou révoqué : la banque est à relier de nouveau."""


class AccountInaccessibleError(ConnectorError):
    """Le consentement ne couvre plus ce compte (carte renouvelée ou remplacée)."""


@dataclass(frozen=True)
class RawResponse:
    content: bytes
    content_type: str


@dataclass(frozen=True)
class RemoteAccount:
    uid: str
    name: str
    iban: str | None
    currency: str
    type: AccountType
    # Identifiant stable d'une session à l'autre (Enable Banking : `identification_hash`) ;
    # indispensable pour retrouver une carte, qui n'a pas d'IBAN, au renouvellement du consentement.
    identification_hash: str | None = None


@dataclass(frozen=True)
class RemoteBalance:
    as_of: date
    type: BalanceType
    amount: Cents
    currency: str


@dataclass(frozen=True)
class RemoteTransaction:
    external_id: str | None
    booking_date: date
    value_date: date | None
    amount: Cents
    currency: str
    label: str
    counterparty_name: str | None
    counterparty_iban: str | None
    bank_tx_code: str | None


@dataclass(frozen=True)
class Page[T]:
    items: list[T]
    raw: RawResponse


class BankConnector(Protocol):
    """Lecture des soldes et des opérations d'un compte ; chaque requête consomme `budget`
    (inv. 10). Lève `ConsentExpiredError`, `AccountInaccessibleError` ou `ConnectorError`."""

    source: ImportSource

    def balances(self, account_uid: str, budget: Budget, *, today: date) -> Page[RemoteBalance]:
        """`today` : date retenue pour un solde que la source ne date pas."""
        ...

    def transactions(
        self, account_uid: str, date_from: date, budget: Budget
    ) -> Iterator[Page[RemoteTransaction]]: ...
