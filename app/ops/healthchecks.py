"""Pings Healthchecks.io (NF-RES-04) : signes de vie envoyés par le client HTTP unique."""

import logging

import httpx

log = logging.getLogger(__name__)


class HealthcheckPinger:
    def __init__(self, client: httpx.Client, url: str) -> None:
        self.client = client
        self.url = url.rstrip("/")

    def ping(self, *, failed: bool = False) -> bool:
        """Envoie le signe de vie ; un échec est journalisé sans interrompre la tâche."""
        try:
            response = self.client.get(f"{self.url}/fail" if failed else self.url)
        except httpx.TransportError as exc:
            log.warning("ping Healthchecks impossible : %s", type(exc).__name__)
            return False
        return response.status_code < 400  # noqa: PLR2004
