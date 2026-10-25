"""Client HTTP unique de l'application (inv. 1).

Toute requête sortante passe par `make_client` : le transport vérifie chaque requête contre la
liste blanche (`allowlist.py`) avant de l'envoyer. Les redirections ne sont pas suivies. Créer
un client httpx ailleurs est interdit (test d'architecture).
"""

import httpx

from app.net.allowlist import ALLOWLIST, Rule, find_rule

DEFAULT_TIMEOUT = httpx.Timeout(20, connect=5)
USER_AGENT = "finances-perso (lecture seule)"


class ForbiddenRequestError(RuntimeError):
    """Requête absente de la liste blanche : jamais envoyée."""


class AllowlistTransport(httpx.BaseTransport):
    def __init__(self, inner: httpx.BaseTransport, allowlist: tuple[Rule, ...]) -> None:
        self.inner = inner
        self.allowlist = allowlist

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        rule = find_rule(
            url.scheme, url.host, request.method, url.path, self.allowlist, port=url.port
        )
        if rule is None:
            raise ForbiddenRequestError(
                f"requête hors liste blanche refusée : {request.method} {url.scheme}://{url.host}{url.path}"
            )
        return self.inner.handle_request(request)

    def close(self) -> None:
        self.inner.close()


def make_client(
    *,
    transport: httpx.BaseTransport | None = None,
    allowlist: tuple[Rule, ...] = ALLOWLIST,
    timeout: httpx.Timeout = DEFAULT_TIMEOUT,
) -> httpx.Client:
    """`transport` sert aux tests (httpx.MockTransport) ; la liste blanche s'applique toujours."""
    inner = transport if transport is not None else httpx.HTTPTransport(retries=0)
    return httpx.Client(
        transport=AllowlistTransport(inner, allowlist),
        timeout=timeout,
        follow_redirects=False,
        headers={"User-Agent": USER_AGENT},
    )
