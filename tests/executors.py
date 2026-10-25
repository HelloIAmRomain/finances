"""Exécuteurs pour les tests des tours de l'assistant (aucun fil d'exécution réel)."""

from collections.abc import Callable
from concurrent.futures import Executor, Future
from typing import Any


class InlineExecutor(Executor):
    """Exécute aussitôt : la réponse est prête dès le retour de l'envoi."""

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future[Any]:
        future: Future[Any] = Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except BaseException as exc:  # relayée par le Future, comme un vrai exécuteur
            future.set_exception(exc)
        return future


class ManualExecutor(Executor):
    """Garde les tâches jusqu'à `run_all()` : pour observer un tour « en cours »."""

    def __init__(self) -> None:
        self.pending: list[tuple[Callable[..., Any], tuple[Any, ...], dict[str, Any]]] = []

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future[Any]:
        self.pending.append((fn, args, kwargs))
        return Future()

    def run_all(self) -> None:
        while self.pending:
            fn, args, kwargs = self.pending.pop(0)
            fn(*args, **kwargs)
