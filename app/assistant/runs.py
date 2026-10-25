"""Tours de conversation en arrière-plan : la question est enregistrée tout de suite, la
réponse se construit pendant que l'interface interroge son avancement (étapes, texte partiel).

- Un tour par conversation à la fois ; registre en mémoire du processus web, sous verrou.
- Événements numérotés (« etape », « fin », « erreur ») : le client ne demande que les nouveaux
  (`depuis`) ; le texte de la réponse, reçu en direct, est renvoyé en entier à chaque fois.
  Seuls des textes destinés à l'utilisateur y figurent.
- Tours terminés oubliés après 15 min : la conversation, elle, garde la réponse en base. Un
  redémarrage pendant un tour laisse la question sans réponse, signalée dans l'historique.
"""

import logging
import secrets
import threading
import time
from collections.abc import Callable
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from app.assistant.chat import AssistantError, Reply

LOGGER = logging.getLogger(__name__)
KEEP_SECONDS = 15 * 60
RUNNING, DONE, FAILED = "en_cours", "termine", "erreur"

Work = Callable[[Callable[[str, str], None]], Reply]


class RunBusyError(Exception):
    """Une réponse est déjà en cours dans cette conversation."""


@dataclass
class _Run:
    id: str
    user_id: int
    conversation_id: int
    status: str = RUNNING
    events: list[dict[str, Any]] = field(default_factory=list)
    partial: str = ""
    reply: Reply | None = None
    error: str | None = None
    finished: int | None = None  # horloge monotone, en nanosecondes


@dataclass(frozen=True)
class RunView:
    id: str
    conversation_id: int
    status: str
    events: list[dict[str, Any]]
    partial: str
    reply: Reply | None
    error: str | None


class RunRegistry:
    def __init__(
        self,
        executor: Executor | None = None,
        *,
        clock: Callable[[], int] = time.monotonic_ns,
        keep_seconds: int = KEEP_SECONDS,
    ) -> None:
        self._executor = executor or ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="assistant"
        )
        self._clock = clock
        self._keep = keep_seconds
        self._runs: dict[str, _Run] = {}
        self._lock = threading.Lock()

    def start(self, *, user_id: int, conversation_id: int, work: Work) -> str:
        with self._lock:
            self._purge()
            if self._active(user_id, conversation_id) is not None:
                raise RunBusyError("Réponse en cours dans cette conversation : patienter.")
            run = _Run(secrets.token_urlsafe(16), user_id, conversation_id)
            self._runs[run.id] = run
        self._executor.submit(self._execute, run, work)
        return run.id

    def active(self, user_id: int, conversation_id: int) -> str | None:
        with self._lock:
            return self._active(user_id, conversation_id)

    def view(self, run_id: str, user_id: int, *, since: int = 0) -> RunView | None:
        """Avancement d'un tour ; None s'il est inconnu, expiré ou à un autre utilisateur."""
        with self._lock:
            run = self._runs.get(run_id)
            if run is None or run.user_id != user_id:
                return None
            return RunView(
                id=run.id,
                conversation_id=run.conversation_id,
                status=run.status,
                events=[dict(e) for e in run.events if e["n"] > since],
                partial=run.partial,
                reply=run.reply,
                error=run.error,
            )

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    # --- interne ----------------------------------------------------------------------------

    def _active(self, user_id: int, conversation_id: int) -> str | None:
        return next(
            (
                r.id
                for r in self._runs.values()
                if r.user_id == user_id
                and r.conversation_id == conversation_id
                and r.status == RUNNING
            ),
            None,
        )

    def _purge(self) -> None:
        limit = self._clock() - self._keep * 1_000_000_000
        for run_id in [i for i, r in self._runs.items() if r.finished and r.finished < limit]:
            del self._runs[run_id]

    def _event(self, run: _Run, kind: str, text: str) -> None:
        with self._lock:
            if kind == "texte":  # le texte partiel complet est renvoyé à chaque interrogation
                run.partial += text
                return
            run.partial = ""  # texte intermédiaire (« je regarde… ») remplacé par l'étape
            run.events.append({"n": len(run.events) + 1, "type": kind, "texte": text})

    def _execute(self, run: _Run, work: Work) -> None:
        try:
            reply = work(lambda kind, text: self._event(run, kind, text))
        except AssistantError as exc:
            self._finish(run, FAILED, error=str(exc))
        except Exception as exc:  # panne inattendue : le tour s'arrête, le serveur continue
            LOGGER.error("tour de l'assistant en échec : %s", type(exc).__name__)
            self._finish(run, FAILED, error="Erreur interne de l'assistant : réessayer.")
        else:
            self._finish(run, DONE, reply=reply)

    def _finish(
        self, run: _Run, status: str, *, reply: Reply | None = None, error: str | None = None
    ) -> None:
        with self._lock:
            run.status, run.reply, run.error = status, reply, error
            if reply is not None:
                run.partial = reply.text
            run.events.append(
                {
                    "n": len(run.events) + 1,
                    "type": "fin" if reply else "erreur",
                    "texte": error or "",
                }
            )
            run.finished = self._clock()
