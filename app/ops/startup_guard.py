"""Garde de démarrage (inv. 1) : aucune clé crypto n'est acceptée sans vérification de ses droits.

La crypto est hors périmètre (29/09/2026) : aucun vérificateur n'est enregistré, la simple présence
d'une clé crypto dans les secrets empêche donc le démarrage. Si des connecteurs reviennent, ils
enregistrent ici un vérificateur qui lève une erreur si la clé peut trader ou retirer.
"""

from collections.abc import Callable
from pathlib import Path

CRYPTO_PROVIDERS = ("coinbase", "okx")

# Fournisseur → fonction qui vérifie les droits de la clé (lève UnsafeKeyError si trop large).
VERIFIERS: dict[str, Callable[[Path], None]] = {}


class UnsafeKeyError(RuntimeError):
    """Une clé pourrait déplacer de l'argent, ou ses droits n'ont pas été vérifiés."""


def check_startup(secrets_dir: Path) -> None:
    if not secrets_dir.is_dir():
        return
    for provider in CRYPTO_PROVIDERS:
        if not any(secrets_dir.glob(f"{provider}_*")):
            continue
        verifier = VERIFIERS.get(provider)
        if verifier is None:
            raise UnsafeKeyError(
                f"clé {provider} présente sans vérification de ses droits : démarrage refusé"
            )
        verifier(secrets_dir)
