"""Révision annuelle du loyer selon l'IRL (LOC-09, LEG-04 ; loi du 6 juillet 1989, art. 17-1).

- Nouveau loyer hors charges = loyer * IRL du même trimestre un an plus tard / IRL de référence,
  arrondi au centime une seule fois (inv. 2). Les charges ne sont pas révisées.
- La révision prend effet à la date anniversaire du bail ; appliquée en retard, elle part du mois
  en cours (pas de rétroactivité).
- Rien n'est appliqué sans validation ; le bail et ses échéances à venir sont mis à jour par
  `update_lease`, l'indice appliqué devient la nouvelle référence. Tout est audité.
- Indices saisis à la main depuis insee.fr (série 001515333), en centièmes (145,17 → 14517).
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from fractions import Fraction
from typing import Any

from sqlalchemy import Engine, insert, select, update

from app.audit import audit
from app.db import write_transaction
from app.models import AuditLog, IrlIndex, Lease
from app.models.rental import LeaseKind
from app.money import round_to_cents
from app.months import month_of
from app.rental.leases import LeaseChanges, update_lease
from app.tax.calendar import anniversary

QUARTER = re.compile(r"(\d{4})-T([1-4])")
NOTICE = timedelta(days=30)
# Publication de l'INSEE : le trimestre T est connu vers le milieu du mois qui suit sa fin.
PUBLISHED_IN = {1: "avril", 2: "juillet", 3: "octobre", 4: "janvier"}
LAST_QUARTER = 4  # publié l'année suivante


class IrlError(ValueError):
    """Indice ou révision refusés (message affiché tel quel)."""


@dataclass(frozen=True)
class Proposal:
    lease_id: int
    reference_quarter: str
    reference_value: int | None
    new_quarter: str
    new_value: int | None
    rent_cents: int
    new_rent_cents: int | None  # None : indice manquant
    effective_from: date

    @property
    def ready(self) -> bool:
        return self.new_rent_cents is not None

    @property
    def missing_quarter(self) -> str | None:
        if self.reference_value is None:
            return self.reference_quarter
        if self.new_value is None:
            return self.new_quarter
        return None


_LOOSE = (
    re.compile(r"(\d{4})\s*[-/ ]?\s*T\s*([1-4])"),  # « 2026-T2 », « 2026 T2 »
    re.compile(r"T\s*([1-4])\s*[-/ ]?\s*(\d{4})"),  # « T2 2026 », comme à l'affichage
)


def check_quarter(quarter: str) -> str:
    """Trimestre normalisé « 2026-T2 » ; accepte aussi « T2 2026 » (forme affichée)."""
    text = " ".join(quarter.strip().upper().split())
    first, second = _LOOSE
    if match := first.fullmatch(text):
        return f"{match.group(1)}-T{match.group(2)}"
    if match := second.fullmatch(text):
        return f"{match.group(2)}-T{match.group(1)}"
    raise IrlError("Trimestre illisible : écrivez par exemple T2 2026.")


def _parts(quarter: str) -> tuple[int, int]:
    match = QUARTER.fullmatch(check_quarter(quarter))
    assert match is not None  # noqa: S101 (vérifié par check_quarter)
    return int(match.group(1)), int(match.group(2))


def next_year(quarter: str) -> str:
    year, number = _parts(quarter)
    return f"{year + 1}-T{number}"


def quarter_label(quarter: str) -> str:
    year, number = _parts(quarter)
    return f"T{number} {year}"


def publication_hint(quarter: str) -> str:
    year, number = _parts(quarter)
    return f"publié mi-{PUBLISHED_IN[number]} {year + 1 if number == LAST_QUARTER else year}"


def revised_rent(rent_cents: int, reference: int, new: int) -> int:
    """Loyer révisé, arrondi une seule fois au centime."""
    return int(round_to_cents(Fraction(rent_cents * new, reference)))


def set_index(
    engine: Engine, quarter: str, value_hundredths: int, *, actor_user_id: int | None, now: datetime
) -> None:
    quarter = check_quarter(quarter)
    if value_hundredths <= 0:
        raise IrlError("Indice positif attendu, par exemple 145,17.")
    with write_transaction(engine) as connection:
        if connection.scalar(select(IrlIndex.quarter).where(IrlIndex.quarter == quarter)) is None:
            connection.execute(
                insert(IrlIndex).values(
                    quarter=quarter, value_hundredths=value_hundredths, created_at=now
                )
            )
        else:
            connection.execute(
                update(IrlIndex)
                .where(IrlIndex.quarter == quarter)
                .values(value_hundredths=value_hundredths)
            )
        audit(
            connection,
            "irl.index",
            ("irl_index", int(quarter[:4]) * 10 + int(quarter[-1])),
            now=now,
            actor_user_id=actor_user_id,
            details={"trimestre": quarter, "valeur": value_hundredths},
        )


def indices(engine: Engine) -> dict[str, int]:
    with engine.connect() as connection:
        return {
            row.quarter: row.value_hundredths
            for row in connection.execute(select(IrlIndex).order_by(IrlIndex.quarter.desc()))
        }


def set_reference(
    engine: Engine, lease_id: int, quarter: str | None, *, actor_user_id: int | None, now: datetime
) -> None:
    value = check_quarter(quarter) if quarter and quarter.strip() else None
    with write_transaction(engine) as connection:
        kind = connection.scalar(select(Lease.kind).where(Lease.id == lease_id))
        if kind is not LeaseKind.RENT:
            raise IrlError("Seul un bail de location se révise selon l'IRL.")
        connection.execute(
            update(Lease).where(Lease.id == lease_id).values(irl_reference_quarter=value)
        )
        audit(
            connection,
            "lease.irl_reference",
            ("lease", lease_id),
            now=now,
            actor_user_id=actor_user_id,
            details={"trimestre": value},
        )


def _applied_from(engine: Engine, lease_id: int) -> str:
    """Mois d'effet de la dernière révision appliquée (« AAAA-MM »), ou chaîne vide."""
    with engine.connect() as connection:
        rows: Sequence[dict[str, Any]] = connection.scalars(
            select(AuditLog.details).where(
                AuditLog.action == "lease.irl_revision",
                AuditLog.object_type == "lease",
                AuditLog.object_id == lease_id,
            )
        ).all()
    return max((str(details.get("a_partir_de", "")) for details in rows), default="")


def proposal(engine: Engine, lease_id: int, today: date) -> Proposal | None:
    """Révision de l'anniversaire en cours ou à venir dans 30 jours ; None hors de cette période
    ou si le bail ne suit pas l'IRL."""
    with engine.connect() as connection:
        lease = connection.execute(select(Lease).where(Lease.id == lease_id)).first()
    if lease is None or lease.kind is not LeaseKind.RENT or not lease.irl_reference_quarter:
        return None
    upcoming = anniversary(lease.start_date, today)
    previous = anniversary(lease.start_date, today - timedelta(days=365))
    if upcoming - today <= NOTICE:
        due = upcoming
    elif previous <= today:
        due = previous  # anniversaire passé, révision pas encore appliquée
    else:
        return None
    if lease.end_date is not None and due > lease.end_date:
        return None
    if _applied_from(engine, lease_id) >= month_of(due):
        return None  # révision de cet anniversaire déjà appliquée
    known = indices(engine)
    reference = lease.irl_reference_quarter
    new_quarter = next_year(reference)
    reference_value, new_value = known.get(reference), known.get(new_quarter)
    new_rent = (
        revised_rent(lease.rent_cents, reference_value, new_value)
        if reference_value is not None and new_value is not None
        else None
    )
    return Proposal(
        lease_id=lease_id,
        reference_quarter=reference,
        reference_value=reference_value,
        new_quarter=new_quarter,
        new_value=new_value,
        rent_cents=lease.rent_cents,
        new_rent_cents=new_rent,
        effective_from=max(due, today.replace(day=1)) if due < today else due,
    )


def apply_revision(
    engine: Engine, lease_id: int, *, today: date, actor_user_id: int | None, now: datetime
) -> Proposal:
    """Applique la révision proposée (après validation) : nouveau loyer à partir du mois d'effet,
    l'indice appliqué devient la référence."""
    found = proposal(engine, lease_id, today)
    if found is None:
        raise IrlError("Aucune révision à appliquer pour ce bail en ce moment.")
    if found.new_rent_cents is None:
        missing = found.missing_quarter or found.new_quarter
        raise IrlError(
            f"Indice {quarter_label(missing)} manquant ({publication_hint(missing)}) : "
            "le saisir d'abord."
        )
    month = month_of(found.effective_from)
    update_lease(
        engine,
        lease_id,
        LeaseChanges(rent_cents=found.new_rent_cents, amounts_from=month),
        actor_user_id=actor_user_id,
        now=now,
    )
    with write_transaction(engine) as connection:
        connection.execute(
            update(Lease)
            .where(Lease.id == lease_id)
            .values(irl_reference_quarter=found.new_quarter)
        )
        audit(
            connection,
            "lease.irl_revision",
            ("lease", lease_id),
            now=now,
            actor_user_id=actor_user_id,
            details={
                "ancien": found.rent_cents,
                "nouveau": found.new_rent_cents,
                "indices": [found.reference_quarter, found.new_quarter],
                "a_partir_de": month,
            },
        )
    return found
