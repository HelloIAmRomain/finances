"""Envoi des requêtes bancaires : liste blanche, budget DSP2, reprises et disjoncteur.

Chaque **tentative** (reprises comprises) passe par le budget DSP2 avant d'être envoyée
(inv. 10) ; une requête hors liste blanche est refusée avant de consommer le budget (inv. 1).
- connexion impossible (la requête n'a pas pu partir) : reprises espacées, 3 tentatives au plus ;
- délai dépassé ou 5xx (la requête a atteint la banque) : **pas de reprise immédiate**, qui
  brûlerait le budget DSP2 pour un résultat probablement identique (constaté chez Boursorama les
  1er et 2 octobre 2026 : trois délais dépassés par créneau) ; la tâche réessaiera plus tard ;
- 429 : jamais retenté, ni compté comme une panne par le disjoncteur (la banque limite, elle
  n'est pas en panne) ; la synchro attend son prochain créneau (Q14) ;
- 401/403 et autres : renvoyés au connecteur (consentement expiré, etc.) ;
- disjoncteur **par connexion bancaire** (et un pour les appels sans compte) : après 5 échecs
  consécutifs, il s'ouvre 30 min pour cette banque seulement.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import Engine
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.connectors.budget import acquire, record_response
from app.net.allowlist import find_rule
from app.net.client import ForbiddenRequestError
from app.timeutil import utcnow

MAX_ATTEMPTS = 3
FAILURE_THRESHOLD = 5
COOLDOWN = timedelta(minutes=30)
HTTP_TOO_MANY_REQUESTS = 429
HTTP_SERVER_ERROR = 500


class BankUnavailableError(RuntimeError):
    """Erreur réseau ou 5xx persistante après les reprises."""


class BankRateLimitedError(RuntimeError):
    """La banque ou l'agrégateur a répondu 429."""


class CircuitOpenError(RuntimeError):
    """Trop d'échecs récents : le connecteur est suspendu."""


class _RetryableError(Exception):
    pass


class CircuitBreaker:
    def __init__(
        self,
        *,
        threshold: int = FAILURE_THRESHOLD,
        cooldown: timedelta = COOLDOWN,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self.threshold = threshold
        self.cooldown = cooldown
        self.now = now
        self.failures = 0
        self.opened_at: datetime | None = None

    def check(self) -> None:
        if self.opened_at is None:
            return
        if self.now() - self.opened_at < self.cooldown:
            raise CircuitOpenError(f"connecteur suspendu jusqu'à {self.opened_at + self.cooldown}")
        self.opened_at = None  # demi-ouverture : une tentative est autorisée

    def record_success(self) -> None:
        self.failures = 0
        self.opened_at = None

    def record_failure(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            self.opened_at = self.now()


@dataclass(frozen=True)
class Budget:
    """Contexte de comptage DSP2 d'une requête bancaire."""

    engine: Engine
    account_id: int
    unattended: bool
    connection_id: int | None = None
    job_run_id: int | None = None


class BankRequester:
    def __init__(
        self,
        client: httpx.Client,
        *,
        breaker: CircuitBreaker | None = None,
        max_attempts: int = MAX_ATTEMPTS,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self.client = client
        self.breaker = breaker or CircuitBreaker(now=now)  # modèle, et appels sans connexion
        self._breakers: dict[int, CircuitBreaker] = {}
        self.max_attempts = max_attempts
        self.sleep = sleep
        self.now = now

    def send(
        self,
        method: str,
        url: str,
        *,
        endpoint: str,
        budget: Budget | None,
        **kwargs: Any,
    ) -> httpx.Response:
        parsed = httpx.URL(url)
        if find_rule(parsed.scheme, parsed.host, method, parsed.path, port=parsed.port) is None:
            raise ForbiddenRequestError(
                f"requête hors liste blanche : {method} {parsed.host}{parsed.path}"
            )
        breaker = self._breaker_for(budget)
        breaker.check()
        retrying = Retrying(
            stop=stop_after_attempt(self.max_attempts),
            wait=wait_exponential(multiplier=2, max=30),
            retry=retry_if_exception_type(_RetryableError),
            sleep=self.sleep,
            reraise=True,
        )
        try:
            for attempt in retrying:
                with attempt:
                    return self._attempt(method, url, endpoint, budget, kwargs, breaker=breaker)
        except _RetryableError as exc:
            raise BankUnavailableError(str(exc)) from exc
        raise AssertionError("inatteignable")  # pragma: no cover

    def _breaker_for(self, budget: Budget | None) -> CircuitBreaker:
        """Disjoncteur de la connexion bancaire : une banque lente ne bloque pas les autres."""
        if budget is None or budget.connection_id is None:
            return self.breaker
        if budget.connection_id not in self._breakers:
            template = self.breaker
            self._breakers[budget.connection_id] = CircuitBreaker(
                threshold=template.threshold, cooldown=template.cooldown, now=template.now
            )
        return self._breakers[budget.connection_id]

    def _attempt(
        self,
        method: str,
        url: str,
        endpoint: str,
        budget: Budget | None,
        kwargs: dict[str, Any],
        *,
        breaker: CircuitBreaker,
    ) -> httpx.Response:
        call_id = None
        if budget is not None:
            call_id = acquire(
                budget.engine,
                account_id=budget.account_id,
                endpoint=endpoint,
                unattended=budget.unattended,
                method=method,
                now=self.now(),
                connection_id=budget.connection_id,
                job_run_id=budget.job_run_id,
            )
        try:
            response = self.client.request(method, url, **kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            breaker.record_failure()  # rien n'est parti : une reprise ne coûte rien à la banque
            raise _RetryableError(f"connexion impossible : {type(exc).__name__}") from exc
        except httpx.TransportError as exc:
            breaker.record_failure()
            raise BankUnavailableError(f"{endpoint} : {type(exc).__name__}") from exc
        if budget is not None and call_id is not None:
            record_response(budget.engine, call_id, response.status_code)
        if response.status_code == HTTP_TOO_MANY_REQUESTS:
            raise BankRateLimitedError(f"{endpoint} : limite de requêtes atteinte (429)")
        if response.status_code >= HTTP_SERVER_ERROR:
            breaker.record_failure()
            raise BankUnavailableError(f"{endpoint} : erreur serveur {response.status_code}")
        breaker.record_success()
        return response
