"""Doublons entre sources (Q5, T1.16a) : une même opération reçue par l'API et par un fichier.

Règles, sur un même compte et entre sources **différentes** :
1. **référence** : même identifiant bancaire (`entryReference`, `FITID`…) → doublon certain ;
2. **exact / multiset** : même montant et même date comptable. Si les effectifs des deux côtés
   sont égaux (1 contre 1, ou n contre n pour des opérations identiques le même jour), les
   transactions sont appariées dans l'ordre → doublons certains. Sinon, elles sont appariées
   autant que possible mais **proposées** à la validation ;
3. **probable** : même montant, dates à ±3 jours → proposé, avec le candidat le plus proche.

La transaction canonique est celle de l'API quand elle existe ; entre deux fichiers, celle du
premier import. Rien n'est supprimé : tout passe par `duplicate_link`. Un lien rejeté ou annulé
n'est jamais reproposé. La détection est idempotente.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import Connection, Engine, insert, select

from app.audit import audit
from app.db import write_transaction
from app.ledger.duplicates import carry_over
from app.models import DuplicateLink, ImportBatch, LedgerTransaction, ReviewItem
from app.models.categorize import ReviewKind, ReviewStatus
from app.models.ledger import DuplicateReason, DuplicateStatus, ImportSource, LinkMode
from app.timeutil import utcnow

PROBABLE_WINDOW = timedelta(days=3)
_ACTIVE = (DuplicateStatus.PROPOSED, DuplicateStatus.CONFIRMED)


@dataclass(frozen=True)
class Candidate:
    id: int
    source: ImportSource
    batch_id: int
    external_id: str | None
    booking_date: date
    amount: int

    @property
    def is_api(self) -> bool:
        return self.source is ImportSource.ENABLE_BANKING


@dataclass(frozen=True)
class DetectionStats:
    confirmed: int
    proposed: int


def _canonical_first(a: Candidate, b: Candidate) -> tuple[Candidate, Candidate]:
    """API d'abord ; entre deux fichiers, le premier importé."""
    if a.is_api != b.is_api:
        return (a, b) if a.is_api else (b, a)
    return (a, b) if (a.batch_id, a.id) <= (b.batch_id, b.id) else (b, a)


class _Detection:
    def __init__(self, connection: Connection, account_id: int) -> None:
        self.connection = connection
        self.now = utcnow()
        self.linked: set[int] = set()
        self.refused: set[tuple[int, int]] = set()
        for canonical, duplicate, status in connection.execute(
            select(
                DuplicateLink.canonical_tx_id, DuplicateLink.duplicate_tx_id, DuplicateLink.status
            )
            .join(LedgerTransaction, LedgerTransaction.id == DuplicateLink.duplicate_tx_id)
            .where(LedgerTransaction.account_id == account_id)
        ):
            if status in _ACTIVE:
                self.linked |= {canonical, duplicate}
            else:
                self.refused.add((canonical, duplicate))
        rows = connection.execute(
            select(
                LedgerTransaction.id,
                ImportBatch.source,
                ImportBatch.id,
                LedgerTransaction.external_id,
                LedgerTransaction.booking_date,
                LedgerTransaction.amount_cents,
            )
            .join(ImportBatch, ImportBatch.id == LedgerTransaction.import_batch_id)
            .where(LedgerTransaction.account_id == account_id)
            .order_by(LedgerTransaction.id)
        )
        self.candidates = [Candidate(*row) for row in rows if row[0] not in self.linked]
        self.stats = {"confirmed": 0, "proposed": 0}

    def free(self, candidate: Candidate) -> bool:
        return candidate.id not in self.linked

    def link(self, a: Candidate, b: Candidate, reason: DuplicateReason, *, certain: bool) -> bool:
        canonical, duplicate = _canonical_first(a, b)
        if (canonical.id, duplicate.id) in self.refused or not (self.free(a) and self.free(b)):
            return False
        status = DuplicateStatus.CONFIRMED if certain else DuplicateStatus.PROPOSED
        result = self.connection.execute(
            insert(DuplicateLink).values(
                canonical_tx_id=canonical.id,
                duplicate_tx_id=duplicate.id,
                mode=LinkMode.AUTO,
                reason=reason,
                status=status,
                created_at=self.now,
                decided_at=self.now if certain else None,
            )
        )
        key = result.inserted_primary_key
        if key is None:
            raise RuntimeError("identifiant de lien non renvoyé")
        link_id = int(key[0])
        self.linked |= {canonical.id, duplicate.id}
        if certain:
            changes = carry_over(
                self.connection,
                link_id=link_id,
                canonical=canonical.id,
                duplicate=duplicate.id,
                now=self.now,
            )
            audit(
                self.connection,
                "duplicate.auto_confirmed",
                ("duplicate_link", link_id),
                now=self.now,
                actor_user_id=None,
                details={"reason": str(reason), "changes": changes},
            )
            self.stats["confirmed"] += 1
        else:
            self.connection.execute(
                insert(ReviewItem).values(
                    kind=ReviewKind.DUPLICATE,
                    dedupe_key=f"duplicate:{link_id}",
                    duplicate_link_id=link_id,
                    transaction_id=duplicate.id,
                    other_transaction_id=canonical.id,
                    status=ReviewStatus.OPEN,
                    details={"reason": str(reason)},
                    created_at=self.now,
                )
            )
            self.stats["proposed"] += 1
        return True

    def by_reference(self) -> None:
        groups: dict[str, list[Candidate]] = defaultdict(list)
        for candidate in self.candidates:
            if candidate.external_id:
                groups[candidate.external_id].append(candidate)
        for group in groups.values():
            sources = {c.source for c in group}
            if len(group) == 2 and len(sources) == 2:  # noqa: PLR2004 (une paire, deux sources)
                self.link(group[0], group[1], DuplicateReason.REFERENCE, certain=True)

    def by_date_and_amount(self) -> None:
        groups: dict[tuple[date, int], list[Candidate]] = defaultdict(list)
        for candidate in self.candidates:
            if self.free(candidate):
                groups[(candidate.booking_date, candidate.amount)].append(candidate)
        for group in groups.values():
            canonical_side, duplicate_side = self._split(group)
            if not canonical_side or not duplicate_side:
                continue
            certain = len(canonical_side) == len(duplicate_side)
            reason = (
                DuplicateReason.EXACT
                if certain and len(canonical_side) == 1
                else DuplicateReason.MULTISET
            )
            for canonical, duplicate in zip(canonical_side, duplicate_side, strict=False):
                self.link(canonical, duplicate, reason, certain=certain)

    def probable(self) -> None:
        """Comparaison limitée aux opérations de même montant (indexées) : le coût reste
        proportionnel à l'historique du compte, même après des années."""
        free = [c for c in self.candidates if self.free(c)]
        if len({c.source for c in free}) < 2:  # noqa: PLR2004 (une seule source : rien à apparier)
            return
        by_amount: dict[int, list[Candidate]] = defaultdict(list)
        for candidate in free:
            by_amount[candidate.amount].append(candidate)
        for candidate in free:
            if not self.free(candidate):
                continue
            others = [
                other
                for other in by_amount[candidate.amount]
                if self.free(other)
                and other.source != candidate.source
                and abs(other.booking_date - candidate.booking_date) <= PROBABLE_WINDOW
            ]
            if others:
                closest = min(
                    others, key=lambda o: (abs(o.booking_date - candidate.booking_date), o.id)
                )
                self.link(candidate, closest, DuplicateReason.PROBABLE, certain=False)

    @staticmethod
    def _split(group: list[Candidate]) -> tuple[list[Candidate], list[Candidate]]:
        """Côté canonique (API, sinon première source importée) et côté doublons (les autres)."""
        if any(c.is_api for c in group):
            return [c for c in group if c.is_api], [c for c in group if not c.is_api]
        first_source = min(group, key=lambda c: (c.batch_id, c.id)).source
        return (
            [c for c in group if c.source == first_source],
            [c for c in group if c.source != first_source],
        )


def detect_duplicates(engine: Engine, *, account_id: int) -> DetectionStats:
    with write_transaction(engine) as connection:
        detection = _Detection(connection, account_id)
        detection.by_reference()
        detection.by_date_and_amount()
        detection.probable()
        return DetectionStats(**detection.stats)
