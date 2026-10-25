"""Conversion des réponses Enable Banking vers les types communs (NF-EXP-03).

- Seules les opérations comptabilisées (`BOOK`) sont retenues (I-09) ; les autres restent dans la
  donnée brute conservée.
- Le montant est toujours positif chez l'agrégateur : le signe vient de `credit_debit_indicator`.
- La contrepartie est le créancier pour un débit, le débiteur pour un crédit.
- Identifiant externe : `entry_reference`, sinon `transaction_id`, sinon aucun (empreinte).
"""

from datetime import date
from typing import Any

from app.connectors.base import RemoteAccount, RemoteBalance, RemoteTransaction
from app.models.ledger import AccountType, BalanceType
from app.money import Cents, parse_amount

ACCOUNT_TYPES = {
    "CACC": AccountType.CHECKING,
    "SVGS": AccountType.SAVINGS,
    "CARD": AccountType.CARD,
}
BOOKED = {"CLBD", "ITBD", "OPBD", "PRCD"}
AVAILABLE = {"ITAV", "CLAV", "FWAV", "XPCD"}


def _amount(block: dict[str, Any]) -> Cents:
    return parse_amount(str(block["amount"]), decimal_sep=".")


def to_account(document: dict[str, Any]) -> RemoteAccount:
    return RemoteAccount(
        uid=str(document["uid"]),
        name=str(document.get("name") or document.get("product") or "Compte"),
        iban=(document.get("account_id") or {}).get("iban"),
        currency=str(document.get("currency") or "EUR"),
        type=ACCOUNT_TYPES.get(str(document.get("cash_account_type")), AccountType.OTHER),
        identification_hash=document.get("identification_hash") or None,
    )


def to_balances(document: dict[str, Any], fallback_day: date) -> list[RemoteBalance]:
    balances = []
    for item in document.get("balances", []):
        code = str(item.get("balance_type", ""))
        kind = (
            BalanceType.BOOKED
            if code in BOOKED
            else BalanceType.AVAILABLE
            if code in AVAILABLE
            else BalanceType.OTHER
        )
        reference = item.get("reference_date")
        balances.append(
            RemoteBalance(
                as_of=date.fromisoformat(reference) if reference else fallback_day,
                type=kind,
                amount=_amount(item["balance_amount"]),
                currency=str(item["balance_amount"].get("currency", "EUR")),
            )
        )
    return balances


def to_transactions(document: dict[str, Any]) -> list[RemoteTransaction]:
    transactions = []
    for item in document.get("transactions", []):
        if item.get("status") != "BOOK" or not item.get("booking_date"):
            continue
        debit = item.get("credit_debit_indicator") == "DBIT"
        magnitude = abs(_amount(item["transaction_amount"]))
        party = (item.get("creditor") if debit else item.get("debtor")) or {}
        party_account = (
            item.get("creditor_account") if debit else item.get("debtor_account")
        ) or {}
        remittance = " ".join(part for part in item.get("remittance_information") or [] if part)
        code_block = item.get("bank_transaction_code") or {}
        code = "/".join(
            str(part) for part in (code_block.get("code"), code_block.get("sub_code")) if part
        )
        value_date = item.get("value_date")
        transactions.append(
            RemoteTransaction(
                external_id=item.get("entry_reference") or item.get("transaction_id") or None,
                booking_date=date.fromisoformat(item["booking_date"]),
                value_date=date.fromisoformat(value_date) if value_date else None,
                amount=Cents(-magnitude if debit else magnitude),
                currency=str(item["transaction_amount"].get("currency", "EUR")),
                label=remittance
                or party.get("name")
                or code_block.get("description")
                or "Opération",
                counterparty_name=party.get("name"),
                counterparty_iban=party_account.get("iban"),
                bank_tx_code=code or None,
            )
        )
    return transactions
