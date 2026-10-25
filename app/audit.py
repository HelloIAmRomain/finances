"""Journal d'audit (NF-SEC-06) : une ligne par décision ou modification, en ajout seul (un
déclencheur refuse toute modification ou suppression)."""

from collections.abc import Mapping
from datetime import datetime

from sqlalchemy import Connection, insert

from app.models import AuditLog


def audit(
    connection: Connection,
    action: str,
    target: tuple[str, int | None] | None,
    *,
    now: datetime,
    actor_user_id: int | None,
    details: Mapping[str, object] | None = None,
) -> None:
    """`action` : « domaine.verbe » (libellés dans `app.ops.journal`) ; `target` : (table,
    identifiant) de l'objet concerné ; `actor_user_id` : None pour une action automatique."""
    object_type, object_id = target if target is not None else (None, None)
    connection.execute(
        insert(AuditLog).values(
            at=now,
            actor_user_id=actor_user_id,
            action=action,
            object_type=object_type,
            object_id=object_id,
            details=dict(details or {}),
        )
    )
