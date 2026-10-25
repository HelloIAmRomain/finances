"""Catégorisation automatique par IA (ANA-02) : opérations sans catégorie classées par le
fournisseur choisi pour l'assistant ; sous le seuil de confiance, suggestion « À valider ».

- **Désactivée par défaut** : activée sur la page Sécurité, et seulement si l'accès IA l'est.
- Données envoyées (inv. 8) : libellé **pseudonymisé** (noms en initiales, identifiants retirés),
  montant, date, sens ; la liste des catégories. Jamais d'IBAN, de compte ni de nom en clair ;
  la garde de sortie (`Pseudonymizer.check`) refuse tout lot où un identifiant subsisterait.
- Priorité (ANA-01) : manuel > règle > IA. Une ligne IA n'est créée que sur une opération sans
  catégorie ; une règle la remplace ensuite (`app.categorize.service`).
- Rejouable (inv. 3) : une opération déjà catégorisée ou déjà soumise (élément « À valider »,
  ouvert ou traité) n'est jamais renvoyée ; une panne du fournisseur ne marque rien.
- Coût : lots bornés, une requête par passage, jetons comptés dans le plafond mensuel de
  l'assistant ; chaque passage est audité.
"""

import enum
import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from sqlalchemy import Connection, Engine, exists, insert, select, update

from app.assistant.access import (
    ENABLED,
    MODEL,
    MONTHLY_CAP,
    PROVIDER,
    assistant_settings,
    switches,
    tokens_this_month,
)
from app.assistant.providers import PROVIDERS, Exchange, ProviderError, complete
from app.assistant.pseudonym import LeakError
from app.assistant.terms import load_pseudonymizer
from app.audit import audit
from app.config import Secrets
from app.db import write_transaction
from app.ledger.views import (
    confirmed_transfer_legs,
    is_effective,
    visible_accounts,
)
from app.models import (
    Account,
    AiSetting,
    Categorization,
    Category,
    LedgerTransaction,
    ReviewItem,
)
from app.models.categorize import (
    CategorizationSource,
    CategoryKind,
    ReviewKind,
    ReviewStatus,
)
from app.models.ledger import AccountType
from app.money import Cents, format_eur
from app.timeutil import paris_date

LOGGER = logging.getLogger(__name__)
AUTO, THRESHOLD = "categorisation_auto", "seuil_confiance"
DEFAULT_THRESHOLD = 800  # pour mille
BATCH = 40
LOOKBACK = timedelta(days=120)
AUDIT_ACTION = "categorize.ai"
SYSTEM = (
    "Tu classes les opérations bancaires d'un particulier français dans une liste FERMÉE de "
    "catégories. Les noms de personnes sont abrégés en initiales et les références retirées. "
    "Pour chaque opération, choisis la catégorie la plus probable, recopiée exactement, et une "
    "confiance de 0 à 100 (100 : certain ; moins de 50 : tu devines). Si rien ne convient, "
    "categorie vaut null. Un virement de ou vers une personne est souvent incertain. "
    'Réponds UNIQUEMENT par un tableau JSON : [{"n": 1, "categorie": "Parent › Enfant", '
    '"confiance": 90}, …], une entrée par opération, sans texte autour.'
)


class AiCategorizationError(Exception):
    """Catégorisation impossible (accès coupé, clé absente, plafond atteint), message affichable."""


@dataclass(frozen=True)
class RunResult:
    sent: int
    categorized: int
    to_review: int
    tokens: int


@dataclass(frozen=True)
class Choice:
    category_id: int | None
    confidence_permille: int


# --- Réglages ----------------------------------------------------------------------------------


def settings(engine: Engine) -> dict[str, Any]:
    with engine.connect() as connection:
        stored: dict[str, Any] = dict(
            connection.execute(
                select(AiSetting.key, AiSetting.value).where(AiSetting.key.in_([AUTO, THRESHOLD]))
            ).all()
        )
    threshold = stored.get(THRESHOLD)
    return {
        AUTO: stored.get(AUTO) is True,
        THRESHOLD: int(threshold) if isinstance(threshold, int) else DEFAULT_THRESHOLD,
    }


def set_settings(
    engine: Engine, *, enabled: bool, threshold: int, actor_user_id: int, now: datetime
) -> None:
    if not 0 <= threshold <= 1000:  # noqa: PLR2004
        raise AiCategorizationError("Seuil de confiance entre 0 et 100 %.")
    with write_transaction(engine) as connection:
        for key, value in ((AUTO, enabled), (THRESHOLD, threshold)):
            if connection.scalar(select(AiSetting.key).where(AiSetting.key == key)) is None:
                connection.execute(insert(AiSetting).values(key=key, value=value, updated_at=now))
            else:
                connection.execute(
                    update(AiSetting)
                    .where(AiSetting.key == key)
                    .values(value=value, updated_at=now)
                )
        audit(
            connection,
            "ai.categorization_settings",
            ("ai_setting", None),
            now=now,
            actor_user_id=actor_user_id,
            details={"actif": enabled, "seuil": threshold},
        )


# --- Sélection des opérations ------------------------------------------------------------------


def pending(engine: Engine, today: date, *, limit: int = BATCH) -> list[Any]:
    """Opérations récentes sans catégorie, jamais soumises, des comptes visibles hors cartes."""
    categorized = exists().where(
        Categorization.transaction_id == LedgerTransaction.id,
        Categorization.superseded_at.is_(None),
    )
    submitted = exists().where(
        ReviewItem.transaction_id == LedgerTransaction.id,
        ReviewItem.kind == ReviewKind.AI_SUGGESTION,
    )
    with engine.connect() as connection:
        return list(
            connection.execute(
                select(
                    LedgerTransaction.id,
                    LedgerTransaction.booking_date,
                    LedgerTransaction.amount_cents,
                    LedgerTransaction.label_clean,
                )
                .where(
                    LedgerTransaction.booking_date >= today - LOOKBACK,
                    LedgerTransaction.amount_cents != 0,
                    LedgerTransaction.account_id.in_(visible_accounts()),
                    LedgerTransaction.account_id.in_(
                        select(Account.id).where(Account.type != AccountType.CARD)
                    ),
                    is_effective(),
                    LedgerTransaction.id.not_in(confirmed_transfer_legs()),
                    ~categorized,
                    ~submitted,
                )
                .order_by(LedgerTransaction.booking_date.desc(), LedgerTransaction.id.desc())
                .limit(limit)
            ).all()
        )


def _categories(engine: Engine) -> dict[str, tuple[int, CategoryKind]]:
    """« Parent › Enfant » (ou racine) → (identifiant, type), catégories actives."""
    parent = Category.__table__.alias("parent")
    with engine.connect() as connection:
        rows = connection.execute(
            select(Category.id, Category.name, Category.kind, parent.c.name)
            .outerjoin(parent, parent.c.id == Category.parent_id)
            .where(Category.archived_at.is_(None))
        ).all()
    return {(f"{row[3]} › {row[1]}" if row[3] else row[1]): (row[0], row[2]) for row in rows}


# --- Échange avec le fournisseur ---------------------------------------------------------------


def build_prompt(paths: list[str], items: list[dict[str, Any]]) -> str:
    lines = "\n".join(f"- {path}" for path in sorted(paths))
    operations = "\n".join(json.dumps(item, ensure_ascii=False) for item in items)
    return f"Catégories :\n{lines}\n\nOpérations :\n{operations}"


def _confidence(value: Any) -> int | None:
    """Confiance en pour mille ; accepte 0-100 ou 0-1, refuse le reste."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    if number < 0 or number > 100:  # noqa: PLR2004
        return None
    if number <= 1 and "." in str(value):
        number *= 100
    return int(number * 10)


def parse_answer(
    text: str, count: int, categories: dict[str, tuple[int, CategoryKind]]
) -> dict[int, Choice]:
    """Réponse JSON du modèle → choix par rang (1 à `count`) ; une entrée illisible est ignorée
    (l'opération part alors « À valider » sans suggestion)."""
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if match is None:
        return {}
    try:
        data: list[Any] = json.loads(match.group(0), parse_float=Decimal)  # « […] » : une liste
    except ValueError:
        return {}
    by_lower = {known.casefold(): known for known in categories}
    by_leaf: dict[str, list[str]] = {}
    for known in categories:
        by_leaf.setdefault(known.rsplit(" › ", 1)[-1].casefold(), []).append(known)
    choices: dict[int, Choice] = {}
    for entry in data:
        if not isinstance(entry, dict) or not isinstance(entry.get("n"), int):
            continue
        rank = entry["n"]
        if not 1 <= rank <= count or rank in choices:
            continue
        name = entry.get("categorie")
        path: str | None = None
        if isinstance(name, str):
            wanted = " ".join(name.replace(">", "›").split()).casefold()
            path = by_lower.get(wanted)
            if path is None and len(by_leaf.get(wanted, [])) == 1:
                path = by_leaf[wanted][0]
        confidence = _confidence(entry.get("confiance"))
        choices[rank] = Choice(
            categories[path][0] if path else None,
            confidence if path and confidence is not None else 0,
        )
    return choices


def _compatible(kind: CategoryKind, amount_cents: int) -> bool:
    if kind is CategoryKind.EXPENSE:
        return amount_cents < 0
    if kind is CategoryKind.INCOME:
        return amount_cents > 0
    return True


class Recorded(enum.Enum):
    CATEGORIZED = "categorisee"
    TO_REVIEW = "a_valider"
    SKIPPED = "deja_categorisee"


def _record(  # noqa: PLR0917 (contexte d'une écriture)
    connection: Connection,
    row: Any,
    choice: Choice | None,
    kinds: dict[int, CategoryKind],
    threshold: int,
    now: datetime,
) -> Recorded:
    """Catégorise, ou soumet « À valider » ; rien si elle a été catégorisée entre-temps (à la
    main pendant l'appel à l'IA) : ce cas n'est compté ni comme l'un ni comme l'autre."""
    if connection.scalar(
        select(Categorization.id).where(
            Categorization.transaction_id == row.id, Categorization.superseded_at.is_(None)
        )
    ):
        return Recorded.SKIPPED
    usable = (
        choice is not None
        and choice.category_id is not None
        and _compatible(kinds[choice.category_id], row.amount_cents)
    )
    if usable and choice is not None and choice.confidence_permille >= threshold:
        connection.execute(
            insert(Categorization).values(
                transaction_id=row.id,
                category_id=choice.category_id,
                amount_cents=row.amount_cents,
                source=CategorizationSource.AI,
                confidence_permille=choice.confidence_permille,
                created_at=now,
            )
        )
        return Recorded.CATEGORIZED
    details: dict[str, Any] = {}
    if usable and choice is not None:
        details = {"categorie": choice.category_id, "confiance": choice.confidence_permille}
    connection.execute(
        insert(ReviewItem).values(
            kind=ReviewKind.AI_SUGGESTION,
            dedupe_key=f"ai:{row.id}",
            transaction_id=row.id,
            status=ReviewStatus.OPEN,
            details=details,
            created_at=now,
        )
    )
    return Recorded.TO_REVIEW


def categorize_pending(
    engine: Engine,
    secrets: Secrets,
    *,
    now: datetime,
    transport: httpx.BaseTransport | None = None,
    limit: int = BATCH,
) -> RunResult:
    """Un passage : un lot d'opérations, une requête. Lève `AiCategorizationError` si la
    catégorisation n'est pas possible ; rien n'est marqué si le fournisseur échoue."""
    options = settings(engine)
    if not options[AUTO]:
        raise AiCategorizationError("Catégorisation automatique désactivée (page Sécurité).")
    if not switches(engine)[ENABLED]:
        raise AiCategorizationError("Accès IA désactivé (page Sécurité).")
    assistant = assistant_settings(engine)
    provider = str(assistant[PROVIDER])
    spec = PROVIDERS.get(provider)
    secret = getattr(secrets, spec["secret"], None) if spec else None
    if spec is None or secret is None or not secret.get_secret_value().strip():
        raise AiCategorizationError("Clé API du fournisseur d'IA absente (page Sécurité).")
    if tokens_this_month(engine, now=now) >= int(assistant[MONTHLY_CAP]):
        raise AiCategorizationError("Plafond mensuel de l'IA atteint (page Sécurité).")
    rows = pending(engine, paris_date(now), limit=limit)
    if not rows:
        return RunResult(0, 0, 0, 0)
    categories = _categories(engine)
    masker = load_pseudonymizer(engine)
    items = [
        {
            "n": rank,
            "libelle": masker.label(row.label_clean or ""),
            "montant": format_eur(Cents(row.amount_cents)),
            "date": row.booking_date.isoformat(),
            "sens": "dépense" if row.amount_cents < 0 else "recette",
        }
        for rank, row in enumerate(rows, start=1)
    ]
    try:
        masker.check([item["libelle"] for item in items])
    except LeakError:
        LOGGER.warning("catégorisation IA : identifiant non masqué, lot non envoyé")
        raise AiCategorizationError("Libellé non pseudonymisable : lot non envoyé.") from None
    exchange = Exchange(
        key=secret.get_secret_value().strip(),
        model=str(assistant[MODEL] or spec["model"]),
        system=SYSTEM,
        messages=[{"role": "user", "text": build_prompt(list(categories), items)}],
        tools=[],
    )
    try:
        completion = complete(provider, exchange, transport)
    except ProviderError as exc:
        raise AiCategorizationError(str(exc)) from exc
    choices = parse_answer(completion.text, len(rows), categories)
    kinds = {category_id: kind for category_id, kind in categories.values()}
    categorized = to_review = 0
    with write_transaction(engine) as connection:
        for rank, row in enumerate(rows, start=1):
            recorded = _record(connection, row, choices.get(rank), kinds, options[THRESHOLD], now)
            categorized += recorded is Recorded.CATEGORIZED
            to_review += recorded is Recorded.TO_REVIEW
        audit(
            connection,
            AUDIT_ACTION,
            ("ai_setting", None),
            now=now,
            actor_user_id=None,
            details={
                "fournisseur": provider,
                "modele": exchange.model,
                "jetons": completion.tokens,
                "envoyees": len(rows),
                "categorisees": categorized,
                "a_valider": to_review,
            },
        )
    return RunResult(len(rows), categorized, to_review, completion.tokens)
