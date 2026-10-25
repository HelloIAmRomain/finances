"""Comptes : création d'un compte alimenté par fichiers et vue d'ensemble (COL-02, COL-07, O1)."""

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import Engine, insert, select, update
from sqlalchemy.orm import Session

from app.alerts.engine import resolve_by_key
from app.audit import audit
from app.crypto import blind_index
from app.db import inserted_id, write_transaction
from app.iban import is_valid_iban, normalize_iban
from app.ledger.balances import reference_balances
from app.models import Account, AccountSyncState, Connection
from app.models.ledger import AccountType, BalanceType, ConnectionStatus


class AccountError(ValueError):
    """Saisie de compte invalide."""


@dataclass(frozen=True)
class AccountOverview:
    id: int
    name: str
    type: AccountType
    iban_masked: str | None
    institution: str | None
    connection_status: ConnectionStatus | None
    consent_expires_at: datetime | None
    last_success_at: datetime | None
    last_attempt_at: datetime | None
    last_error: str | None
    balance_cents: int | None
    balance_date: date | None
    balance_type: BalanceType | None
    counted: bool  # compté (patrimoine, recettes, dépenses) ou consultation seule
    description: str | None  # rôle du compte, pour l'IA
    syncs_per_day: int
    ignored: bool  # ignoré (Q19) : ni synchro, ni affichage ailleurs que dans « Comptes ignorés »


def mask_iban(iban: str | None) -> str | None:
    if not iban:
        return None
    compact = normalize_iban(iban)
    return f"{compact[:4]} •••• {compact[-4:]}"


def create_file_account(
    engine: Engine,
    *,
    name: str,
    type: AccountType,
    iban: str | None,
    include_in_net_worth: bool,
    actor_user_id: int | None,
    now: datetime,
) -> int:
    name = name.strip()
    if not name:
        raise AccountError("Le nom du compte est obligatoire.")
    iban = normalize_iban(iban) if iban and iban.strip() else None
    if iban is not None and not is_valid_iban(iban):
        raise AccountError("IBAN invalide (clé de contrôle incorrecte).")
    with write_transaction(engine) as connection:
        result = connection.execute(
            insert(Account).values(
                name=name,
                type=type,
                iban_enc=iban,
                iban_hash=blind_index(iban, context="iban") if iban else None,
                currency="EUR",
                include_in_net_worth=include_in_net_worth,
                syncs_per_day=1,
            )
        )
        account_id = inserted_id(result)
        audit(
            connection,
            "account.created",
            ("account", account_id),
            now=now,
            actor_user_id=actor_user_id,
            details={"name": name},
        )
    return account_id


MAX_DESCRIPTION = 500
MAX_NAME = 128


def rename_account(
    engine: Engine, account_id: int, name: str, *, actor_user_id: int | None, now: datetime
) -> bool:
    """Donne un nom au compte (celui de la banque est souvent générique) ; audité. Le compte
    garde son identité : seul l'affichage change. Renvoie False si le nom est inchangé."""
    name = " ".join(name.split())
    if not name:
        raise AccountError("Le nom du compte ne peut pas être vide.")
    if len(name) > MAX_NAME:
        raise AccountError(f"Nom trop long ({MAX_NAME} caractères au plus).")
    with write_transaction(engine) as connection:
        before = connection.scalar(select(Account.name).where(Account.id == account_id))
        if before is None:
            raise AccountError("Compte introuvable.")
        if before == name:
            return False
        connection.execute(update(Account).where(Account.id == account_id).values(name=name))
        audit(
            connection,
            "account.renamed",
            ("account", account_id),
            now=now,
            actor_user_id=actor_user_id,
            details={"avant": before, "apres": name},
        )
    return True


def describe_account(
    engine: Engine, account_id: int, description: str, *, actor_user_id: int | None, now: datetime
) -> bool:
    """Rôle du compte en clair (« compte courant principal, salaire et dépenses ») : aide l'IA à
    comprendre ce qu'il contient. Vide : effacé. Audité, sans le texte (il peut contenir un nom)."""
    text = " ".join(description.split()) or None
    if text is not None and len(text) > MAX_DESCRIPTION:
        raise AccountError(f"Rôle trop long ({MAX_DESCRIPTION} caractères au plus).")
    with write_transaction(engine) as connection:
        row = connection.execute(
            select(Account.description).where(Account.id == account_id)
        ).first()
        if row is None:
            raise AccountError("Compte introuvable.")
        if row.description == text:
            return False
        connection.execute(update(Account).where(Account.id == account_id).values(description=text))
        audit(
            connection,
            "account.described",
            ("account", account_id),
            now=now,
            actor_user_id=actor_user_id,
            details={"longueur": len(text or "")},
        )
    return True


def update_account_settings(
    engine: Engine,
    account_id: int,
    *,
    counted: bool,
    syncs_per_day: int,
    actor_user_id: int | None,
    now: datetime,
    ignored: bool = False,
) -> None:
    """Compté, consultation seule ou ignoré ; synchros automatiques par jour (1 à 4, inv. 10).
    Réglage du compte, jamais de la donnée importée (inv. 4) ; chaque changement est audité.
    Un compte ignoré n'est plus synchronisé : son alerte de retard de synchro est résolue."""
    if not 1 <= syncs_per_day <= 4:  # noqa: PLR2004 (plafond DSP2)
        raise AccountError("De 1 à 4 mises à jour par jour (plafond fixé par les banques, DSP2).")
    with write_transaction(engine) as connection:
        before = connection.execute(
            select(Account.include_in_net_worth, Account.syncs_per_day, Account.ignored_at).where(
                Account.id == account_id
            )
        ).one_or_none()
        if before is None:
            raise AccountError("Compte introuvable.")
        was_ignored = before.ignored_at is not None
        if (before.include_in_net_worth, before.syncs_per_day, was_ignored) == (
            counted,
            syncs_per_day,
            ignored,
        ):
            return
        connection.execute(
            update(Account)
            .where(Account.id == account_id)
            .values(
                include_in_net_worth=counted,
                syncs_per_day=syncs_per_day,
                ignored_at=(before.ignored_at or now) if ignored else None,
            )
        )
        audit(
            connection,
            "account.settings",
            ("account", account_id),
            now=now,
            actor_user_id=actor_user_id,
            details={
                "compte": counted,
                "ignore": ignored,
                "synchros_par_jour": syncs_per_day,
                "avant": {
                    "compte": before.include_in_net_worth,
                    "ignore": was_ignored,
                    "synchros_par_jour": before.syncs_per_day,
                },
            },
        )
    if ignored:
        resolve_by_key(engine, f"sync_stale:{account_id}", now=now)


def accounts_overview(engine: Engine) -> list[AccountOverview]:
    balance = reference_balances()
    query = (
        select(
            Account,
            Connection.institution_name,
            Connection.status,
            Connection.consent_expires_at,
            AccountSyncState.last_success_at,
            AccountSyncState.last_attempt_at,
            AccountSyncState.last_error,
            balance.c.amount_cents,
            balance.c.as_of_date,
            balance.c.balance_type,
        )
        .outerjoin(Connection, Connection.id == Account.connection_id)
        .outerjoin(AccountSyncState, AccountSyncState.account_id == Account.id)
        .outerjoin(balance, balance.c.account_id == Account.id)
        .where(Account.closed_at.is_(None))
        .order_by(Account.name, Account.id)
    )
    with Session(engine) as session:
        rows = session.execute(query).all()
    return [
        AccountOverview(
            id=account.id,
            name=account.name,
            type=account.type,
            iban_masked=mask_iban(account.iban_enc),
            institution=institution,
            connection_status=status,
            consent_expires_at=expires,
            last_success_at=last_success,
            last_attempt_at=last_attempt,
            last_error=last_error,
            balance_cents=balance,
            balance_date=balance_date,
            balance_type=balance_type,
            counted=account.include_in_net_worth,
            description=account.description,
            syncs_per_day=account.syncs_per_day,
            ignored=account.ignored_at is not None,
        )
        for (
            account,
            institution,
            status,
            expires,
            last_success,
            last_attempt,
            last_error,
            balance,
            balance_date,
            balance_type,
        ) in rows
    ]
