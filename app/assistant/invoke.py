"""Appel d'un outil de l'IA, commun au serveur MCP (client du PC) et à l'assistant intégré
(page Assistant, application Android) : mêmes erreurs, même garde de confidentialité avec son
alerte, même journal des appels (`docs/spec-mcp.md`, inv. 8).

Chaque interface ne fait que mettre en forme le résultat (contenu JSON-RPC ou texte tronqué).
"""

import logging
import time
from dataclasses import dataclass
from typing import Any

from app.alerts.engine import raise_alert
from app.assistant.access import record_call
from app.assistant.pseudonym import LeakError
from app.assistant.tools import Context, ToolError, call_tool
from app.models.alerts import Severity

LOGGER = logging.getLogger(__name__)
BLOCKED = "Réponse bloquée par la garde de confidentialité."
INTERNAL = "Erreur interne de l'outil."


@dataclass(frozen=True)
class Outcome:
    """Résultat de l'outil, ou message d'erreur à transmettre à l'IA (jamais les deux)."""

    result: dict[str, Any] | None
    error: str | None = None


def invoke(
    context: Context, name: str, arguments: dict[str, Any], *, via: str | None = None
) -> Outcome:
    started = time.monotonic()
    ok, rows = False, None
    try:
        result = call_tool(context, name, arguments)
        ok, rows = True, sum(len(v) for v in result.values() if isinstance(v, list))
        outcome = Outcome(result)
    except ToolError as exc:  # le message peut reprendre ce que l'IA a envoyé : masqué aussi
        outcome = Outcome(None, str(context.masker.scrub(str(exc))))
    except LeakError:
        raise_alert(
            context.engine,
            type="ai_leak",
            severity=Severity.WARNING,
            key=f"ai:leak:{name[:64]}",
            title="Réponse à l'IA bloquée : donnée personnelle non masquée",
            now=context.now,
            details={"outil": name[:64]},
        )
        outcome = Outcome(None, BLOCKED)
    except Exception as exc:  # panne d'un outil : l'appel échoue, l'échange continue
        # Type seulement : le message d'une erreur SQL peut contenir des données (inv. 5).
        LOGGER.error("outil %s en échec : %s", name[:64], type(exc).__name__)
        outcome = Outcome(None, INTERNAL)
    details: dict[str, Any] = {"parametres": sorted(str(key)[:32] for key in arguments)[:20]}
    if via is not None:
        details["via"] = via
    record_call(
        context.engine,
        context.token,
        tool=name[:64],
        ok=ok,
        rows=rows,
        duration_ms=int((time.monotonic() - started) * 1000),
        details=details,
        now=context.now,
    )
    return outcome
