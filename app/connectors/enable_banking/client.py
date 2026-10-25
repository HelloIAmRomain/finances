"""Client Enable Banking (COL-01) : lecture de comptes et cycle de vie du consentement.

- Authentification : JWT RS256 signé avec la clé privée de l'application (jamais transmise),
  `kid` = identifiant de l'application, valable une heure.
- Toutes les requêtes passent par `BankRequester` : liste blanche (inv. 1) puis budget DSP2
  pour les lectures de compte (inv. 10).
- 401 / 403 sur une session ou un compte : consentement expiré ou révoqué (`ConsentExpiredError`).
- Refus de quota signalé dans le corps d'une erreur (`ASPSP_RATE_LIMIT_EXCEEDED`, code à confirmer
  sur de vraies réponses) : traité comme un 429 (`BankRateLimitedError`).
"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import httpx
import jwt

from app.connectors import base
from app.connectors.base import ConnectorError
from app.connectors.requester import BankRateLimitedError, BankRequester, Budget
from app.timeutil import utcnow

BASE_URL = "https://api.enablebanking.com"
JWT_LIFETIME = timedelta(hours=1)
JWT_REFRESH_MARGIN = timedelta(minutes=5)
HTTP_BAD_REQUEST = 400
CONSENT_STATUSES = {401, 403}
RATE_LIMIT_ERRORS = {"ASPSP_RATE_LIMIT_EXCEEDED"}


class EnableBankingError(ConnectorError):
    """Refus de l'agrégateur ou de la banque (4xx)."""


class ConsentExpiredError(EnableBankingError, base.ConsentExpiredError):
    """Session refusée par l'agrégateur : consentement expiré ou révoqué, liaison à refaire."""


@dataclass(frozen=True)
class Credentials:
    application_id: str
    private_key_pem: str

    def __repr__(self) -> str:
        return f"Credentials(application_id={self.application_id!r}, private_key_pem=**masqué**)"


@dataclass(frozen=True)
class RawJson:
    document: dict[str, Any]
    content: bytes


def _error_code(response: httpx.Response) -> str | None:
    try:
        document = response.json()
    except ValueError:
        return None
    code = document.get("error") if isinstance(document, dict) else None
    return code if isinstance(code, str) else None


class EnableBankingClient:
    def __init__(
        self,
        requester: BankRequester,
        credentials: Credentials,
        *,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self.requester = requester
        self.credentials = credentials
        self.now = now
        self._token: tuple[str, datetime] | None = None

    def _authorization(self) -> str:
        now = self.now()
        if self._token is None or now >= self._token[1] - JWT_REFRESH_MARGIN:
            expires = now + JWT_LIFETIME
            token = jwt.encode(
                {
                    "iss": "enablebanking.com",
                    "aud": "api.enablebanking.com",
                    "iat": int(now.timestamp()),
                    "exp": int(expires.timestamp()),
                },
                self.credentials.private_key_pem,
                algorithm="RS256",
                headers={"kid": self.credentials.application_id},
            )
            self._token = (token, expires)
        return f"Bearer {self._token[0]}"

    def _call(
        self,
        method: str,
        path: str,
        *,
        endpoint: str,
        budget: Budget | None = None,
        params: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> RawJson:
        response = self.requester.send(
            method,
            f"{BASE_URL}{path}",
            endpoint=endpoint,
            budget=budget,
            headers={"Authorization": self._authorization()},
            params=params,
            json=body,
        )
        if response.status_code >= HTTP_BAD_REQUEST and _error_code(response) in RATE_LIMIT_ERRORS:
            raise BankRateLimitedError(f"{endpoint} : quota de la banque atteint")
        if response.status_code in CONSENT_STATUSES:
            raise ConsentExpiredError(
                f"{endpoint} : accès refusé ({response.status_code})", response.status_code
            )
        if response.status_code >= HTTP_BAD_REQUEST:
            # Code d'erreur de l'agrégateur (« ASPSP_ERROR »…) : technique, sans donnée personnelle.
            code = _error_code(response)
            detail = f" ({code})" if code else ""
            raise EnableBankingError(
                f"{endpoint} : erreur {response.status_code}{detail}", response.status_code, code
            )
        document = response.json() if response.content else {}
        return RawJson(document=document, content=response.content)

    # --- Consentement (utilisateur présent : hors budget DSP2) -----------------------------------

    def aspsps(self, country: str) -> list[dict[str, Any]]:
        banks: list[dict[str, Any]] = self._call(
            "GET", "/aspsps", endpoint="aspsps", params={"country": country}
        ).document.get("aspsps", [])
        return banks

    def start_authorization(
        self, *, aspsp_name: str, country: str, valid_until: datetime, state: str, redirect_url: str
    ) -> str:
        """Renvoie l'adresse où l'utilisateur s'authentifie auprès de sa banque."""
        document = self._call(
            "POST",
            "/auth",
            endpoint="auth",
            body={
                "access": {"valid_until": valid_until.isoformat()},
                "aspsp": {"name": aspsp_name, "country": country},
                "state": state,
                "redirect_url": redirect_url,
                "psu_type": "personal",
            },
        ).document
        return str(document["url"])

    def create_session(self, code: str) -> dict[str, Any]:
        return self._call("POST", "/sessions", endpoint="sessions", body={"code": code}).document

    def delete_session(self, session_id: str) -> None:
        self._call("DELETE", f"/sessions/{session_id}", endpoint="sessions")

    # --- Lecture de compte (budget DSP2) ------------------------------------------------------

    def balances(self, account_uid: str, budget: Budget | None) -> RawJson:
        return self._call(
            "GET", f"/accounts/{account_uid}/balances", endpoint="balances", budget=budget
        )

    def transactions(
        self, account_uid: str, date_from: date, budget: Budget | None
    ) -> Iterator[RawJson]:
        """Pages de transactions depuis `date_from` ; chaque page consomme le budget."""
        params = {"date_from": date_from.isoformat()}
        while True:
            page = self._call(
                "GET",
                f"/accounts/{account_uid}/transactions",
                endpoint="transactions",
                budget=budget,
                params=params,
            )
            yield page
            key = page.document.get("continuation_key")
            if not key:
                return
            params = {"date_from": date_from.isoformat(), "continuation_key": str(key)}
