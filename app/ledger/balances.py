"""Solde de référence d'un compte (ANA-09, COL-07).

Toutes les banques ne fournissent pas un solde comptable : sur les réponses réelles, la Société
Générale ne renvoie que le solde prévisionnel (`XPCD`), une carte Boursorama un solde `OTHR`.
Le solde retenu est donc, par ordre de préférence : le dernier solde **comptable**, sinon le
dernier solde **disponible ou prévisionnel**, sinon le dernier autre solde. Le type retenu est
renvoyé pour être affiché (« prévisionnel »).
"""

from sqlalchemy import Subquery, case, func, select

from app.models import BalanceSnapshot
from app.models.ledger import BalanceType

PREFERENCE = case(
    (BalanceSnapshot.balance_type == BalanceType.BOOKED, 0),
    (BalanceSnapshot.balance_type == BalanceType.AVAILABLE, 1),
    else_=2,
)


def reference_balances() -> Subquery:
    """(account_id, amount_cents, as_of_date, balance_type) : un solde par compte au plus."""
    ranked = select(
        BalanceSnapshot.account_id,
        BalanceSnapshot.amount_cents,
        BalanceSnapshot.as_of_date,
        BalanceSnapshot.balance_type,
        func.row_number()
        .over(
            partition_by=BalanceSnapshot.account_id,
            order_by=(PREFERENCE, BalanceSnapshot.as_of_date.desc(), BalanceSnapshot.id.desc()),
        )
        .label("rang"),
    ).subquery()
    return (
        select(
            ranked.c.account_id,
            ranked.c.amount_cents,
            ranked.c.as_of_date,
            ranked.c.balance_type,
        )
        .where(ranked.c.rang == 1)
        .subquery()
    )
