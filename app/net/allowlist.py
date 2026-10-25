"""Liste blanche des requêtes sortantes (inv. 1) : hôte, port, méthode et gabarit de chemin.

Tout ce qui n'est pas listé ici est refusé par le client HTTP **avant** l'envoi. Ajouter une
entrée demande une revue : aucune ne doit permettre d'initier un paiement, un virement, un ordre
ou un retrait. Les écritures autorisées chez Enable Banking se limitent à l'ouverture d'une
autorisation (`POST /auth`), à la création de la session (`POST /sessions`) et à sa révocation
(`DELETE /sessions/{id}`).

Assistant intégré (phase B2, docs/spec-mcp.md §13) : un seul point d'entrée par fournisseur d'IA,
qui reçoit des données déjà pseudonymisées (inv. 8) et ne touche à aucune banque.
"""

import re
from dataclasses import dataclass
from functools import cached_property

ENABLE_BANKING = "api.enablebanking.com"
HEALTHCHECKS = "hc-ping.com"
NTFY_INTERNAL = "ntfy"  # service compose, réseau interne
NTFY_INTERNAL_PORT = 8080
ANTHROPIC = "api.anthropic.com"
OPENAI = "api.openai.com"
OVH_AI = "oai.endpoints.kepler.ai.cloud.ovh.net"

# Variables de chemin typées : un gabarit ne doit jamais accepter un chemin arbitraire
# (sinon « /{id} » accepterait aussi « /payments »).
SAFE_SEGMENT = r"[A-Za-z0-9._~-]+"  # identifiant opaque, sans « / » ni « % »
PLACEHOLDERS = {
    "check_uuid": r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    "topic": r"finances-[a-z0-9-]{1,64}",
}


DEFAULT_PORTS = {"https": 443, "http": 80}


@dataclass(frozen=True)
class Rule:
    scheme: str
    host: str
    method: str
    path: str  # gabarit : « {nom} » est une variable typée (voir PLACEHOLDERS)
    port: int | None = None  # None : port par défaut du schéma

    @property
    def expected_port(self) -> int:
        return self.port if self.port is not None else DEFAULT_PORTS[self.scheme]

    @cached_property
    def pattern(self) -> re.Pattern[str]:
        parts = re.split(r"(\{[a-z_]+\})", self.path)
        regex = "".join(
            PLACEHOLDERS.get(part[1:-1], SAFE_SEGMENT) if part.startswith("{") else re.escape(part)
            for part in parts
        )
        return re.compile(regex)

    def matches(self, scheme: str, host: str, port: int | None, method: str, path: str) -> bool:
        """`port` : None pour le port par défaut du schéma (comme `httpx.URL.port`)."""
        actual_port = port if port is not None else DEFAULT_PORTS.get(scheme)
        return (scheme, host, actual_port, method) == (
            self.scheme,
            self.host,
            self.expected_port,
            self.method,
        ) and self.pattern.fullmatch(path) is not None


ALLOWLIST: tuple[Rule, ...] = (
    # Enable Banking : lecture de comptes + cycle de vie du consentement.
    Rule("https", ENABLE_BANKING, "GET", "/aspsps"),
    Rule("https", ENABLE_BANKING, "POST", "/auth"),
    Rule("https", ENABLE_BANKING, "POST", "/sessions"),
    Rule("https", ENABLE_BANKING, "GET", "/sessions/{session_id}"),
    Rule("https", ENABLE_BANKING, "DELETE", "/sessions/{session_id}"),
    Rule("https", ENABLE_BANKING, "GET", "/accounts/{account_id}/details"),
    Rule("https", ENABLE_BANKING, "GET", "/accounts/{account_id}/balances"),
    Rule("https", ENABLE_BANKING, "GET", "/accounts/{account_id}/transactions"),
    # Supervision : signes de vie.
    Rule("https", HEALTHCHECKS, "GET", "/{check_uuid}"),
    Rule("https", HEALTHCHECKS, "GET", "/{check_uuid}/start"),
    Rule("https", HEALTHCHECKS, "GET", "/{check_uuid}/fail"),
    # Notifications vers le serveur ntfy interne.
    Rule("http", NTFY_INTERNAL, "POST", "/{topic}", port=NTFY_INTERNAL_PORT),
    # Assistant intégré : génération de texte seulement.
    Rule("https", ANTHROPIC, "POST", "/v1/messages"),
    Rule("https", OPENAI, "POST", "/v1/chat/completions"),
    Rule("https", OVH_AI, "POST", "/v1/chat/completions"),
)


def find_rule(
    scheme: str,
    host: str,
    method: str,
    path: str,
    allowlist: tuple[Rule, ...] = ALLOWLIST,
    *,
    port: int | None = None,
) -> Rule | None:
    return next(
        (rule for rule in allowlist if rule.matches(scheme, host, port, method, path)), None
    )
