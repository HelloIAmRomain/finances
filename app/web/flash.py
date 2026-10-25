"""Messages affichés après une action (« ?message=… », « ?erreur=… ») : seuls ceux que
l'application a produits s'affichent.

Sans cela, un lien piégé afficherait un faux avertissement sur une page de confiance (« votre clé
est compromise, appelez le … »). Toute redirection de l'application qui porte un message est
scellée au passage par l'intercepteur de `app.web.security` (`seal_location`) ; une page
n'affiche le message que si le sceau correspond (`flash`). Le sceau est un HMAC dérivé de la clé
de l'application : il ne se fabrique pas hors du serveur.
"""

import hashlib
import hmac
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import Request

from app.config import Secrets

FLASH_PARAMS = ("message", "erreur")
SEAL_PARAM = "sceau"
SEAL_LENGTH = 32  # caractères hexadécimaux : 128 bits


def _key(request: Request) -> bytes:
    secrets: Secrets = request.app.state.secrets
    secret = secrets.app_secret_key.get_secret_value().encode()
    return hmac.new(secret, b"messages-affiches", hashlib.sha256).digest()


def _seal(key: bytes, flashed: list[tuple[str, str]]) -> str:
    text = "\n".join(f"{name}={value}" for name, value in flashed)
    return hmac.new(key, text.encode(), hashlib.sha256).hexdigest()[:SEAL_LENGTH]


def seal_location(request: Request, location: str) -> str:
    """Ajoute le sceau à une adresse interne qui porte un message ; toute autre est rendue telle
    quelle."""
    parts = urlsplit(location)
    if parts.scheme or parts.netloc:
        return location
    query = [(n, v) for n, v in parse_qsl(parts.query, keep_blank_values=True) if n != SEAL_PARAM]
    flashed = [(name, value) for name, value in query if name in FLASH_PARAMS]
    if not flashed:
        return location
    query.append((SEAL_PARAM, _seal(_key(request), flashed)))
    return urlunsplit(parts._replace(query=urlencode(query)))


def flash(request: Request, name: str = "message") -> str | None:
    """Le message `name` de l'adresse s'il vient de l'application, sinon None."""
    params = request.query_params
    flashed = [(n, v) for n, v in params.multi_items() if n in FLASH_PARAMS]
    if not flashed:
        return None
    if not hmac.compare_digest(params.get(SEAL_PARAM, ""), _seal(_key(request), flashed)):
        return None
    return params.get(name)
