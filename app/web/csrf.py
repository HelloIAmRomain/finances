"""Jeton CSRF (NF-SEC-04) : dérivé de la session par HMAC avec `app_secret_key`, exigé sur toute
écriture faite depuis une session (`deps.current_user`)."""

import hashlib
import hmac


def csrf_token(secret_key: str, session_token: str) -> str:
    return hmac.new(
        secret_key.encode(), f"csrf:{session_token}".encode(), hashlib.sha256
    ).hexdigest()


def csrf_valid(secret_key: str, session_token: str | None, candidate: str | None) -> bool:
    if not session_token or not candidate:
        return False
    return hmac.compare_digest(csrf_token(secret_key, session_token), candidate)
