"""Transactions « effectives » : celles qui comptent dans les agrégats, la catégorisation et le
rapprochement des loyers. Une transaction marquée doublon (lien confirmé) en est exclue ; elle
reste en base, intacte (inv. 4), et réapparaît si le lien est annulé."""

from sqlalchemy import ColumnElement, CompoundSelect, Connection, Select, select

from app.models import Account, DuplicateLink, LedgerTransaction, TransferLink
from app.models.ledger import DuplicateStatus, TransferStatus


def visible_accounts() -> Select[int]:
    """Comptes non ignorés (Q19) : seuls leurs transactions et soldes apparaissent."""
    return select(Account.id).where(Account.ignored_at.is_(None))


def confirmed_duplicates() -> Select[int]:
    return select(DuplicateLink.duplicate_tx_id).where(
        DuplicateLink.status == DuplicateStatus.CONFIRMED
    )


def is_effective() -> ColumnElement[bool]:
    """Condition d'un WHERE : la transaction n'est pas un doublon confirmé."""
    return LedgerTransaction.id.not_in(confirmed_duplicates())


def confirmed_transfer_legs() -> CompoundSelect[int]:
    confirmed = TransferLink.status == TransferStatus.CONFIRMED
    return (
        select(TransferLink.debit_tx_id)
        .where(confirmed)
        .union(select(TransferLink.credit_tx_id).where(confirmed))
    )


def confirmed_transfer_pairs(connection: Connection) -> dict[int, int]:
    """Jambe d'un virement interne confirmé → transaction de l'autre jambe."""
    pairs: dict[int, int] = {}
    for debit, credit in connection.execute(
        select(TransferLink.debit_tx_id, TransferLink.credit_tx_id).where(
            TransferLink.status == TransferStatus.CONFIRMED
        )
    ):
        pairs[debit], pairs[credit] = credit, debit
    return pairs


def non_transfer_transactions() -> Select[int]:
    """Transactions effectives hors virements internes confirmés : revenus et dépenses (COL-06)."""
    return select(LedgerTransaction.id).where(
        is_effective(),
        LedgerTransaction.id.not_in(confirmed_transfer_legs()),
    )
