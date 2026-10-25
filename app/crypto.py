"""Chiffrement applicatif des données sensibles (NF-SEC-03 reformulé, décision Q3).

- `encrypt` / `decrypt` : AES-256-GCM, nonce aléatoire, contexte authentifié (nom de la colonne) :
  une valeur chiffrée ne peut pas être recopiée d'une colonne à une autre.
- `blind_index` : HMAC-SHA256 avec une **autre** clé, pour retrouver une valeur chiffrée
  (ex. IBAN émetteur d'un loyer) sans la déchiffrer.
- `opaque_id` / `parse_opaque_id` : identifiant montré à l'IA (« t_… ») à la place de la clé
  primaire : chiffrement déterministe d'un seul bloc AES, stable, non devinable, réversible par
  le serveur seulement (clé dérivée de la clé d'index).

Format d'un jeton : octet de version, nonce (12 octets), texte chiffré + étiquette GCM.
Les clés viennent des secrets (tmpfs) et sont installées au démarrage par `configure_keyring`.
"""

import base64
import hashlib
import hmac
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import Secrets

_VERSION = b"\x01"
_NONCE_BYTES = 12
_KEY_BYTES = 32


class KeyringNotConfiguredError(RuntimeError):
    """Les clés de chiffrement n'ont pas été chargées au démarrage."""


class DecryptionError(ValueError):
    """Jeton illisible : clé, contexte ou contenu incorrects."""


class Keyring:
    """Clés de chiffrement et d'index, jamais affichées."""

    def __init__(self, data_key: bytes, index_key: bytes) -> None:
        for name, key in (("chiffrement", data_key), ("index", index_key)):
            if len(key) != _KEY_BYTES:
                raise ValueError(f"clé de {name} : 32 octets attendus")
        if hmac.compare_digest(data_key, index_key):
            raise ValueError("les clés de chiffrement et d'index doivent être distinctes")
        self._data_key = data_key
        self._index_key = index_key

    @classmethod
    def from_base64(cls, *, data_key: str, index_key: str) -> "Keyring":
        return cls(
            base64.b64decode(data_key, validate=True), base64.b64decode(index_key, validate=True)
        )

    @classmethod
    def from_secrets(cls, secrets: Secrets) -> "Keyring":
        return cls.from_base64(
            data_key=secrets.data_encryption_key.get_secret_value().strip(),
            index_key=secrets.blind_index_key.get_secret_value().strip(),
        )

    def __repr__(self) -> str:
        return "Keyring(**masqué**)"

    __str__ = __repr__


_keyring: Keyring | None = None


def configure_keyring(keyring: Keyring) -> None:
    global _keyring  # noqa: PLW0603 (état de processus, installé une fois au démarrage)
    _keyring = keyring


def reset_keyring() -> None:
    global _keyring  # noqa: PLW0603
    _keyring = None


def _current() -> Keyring:
    if _keyring is None:
        raise KeyringNotConfiguredError("clés de chiffrement non chargées")
    return _keyring


def encrypt_bytes(plaintext: bytes, *, context: str) -> bytes:
    nonce = os.urandom(_NONCE_BYTES)
    sealed = AESGCM(_current()._data_key).encrypt(nonce, plaintext, context.encode("utf-8"))
    return _VERSION + nonce + sealed


def decrypt_bytes(token: bytes, *, context: str) -> bytes:
    if not token.startswith(_VERSION) or len(token) <= 1 + _NONCE_BYTES:
        raise DecryptionError("jeton de format inconnu")
    nonce, sealed = token[1 : 1 + _NONCE_BYTES], token[1 + _NONCE_BYTES :]
    try:
        return AESGCM(_current()._data_key).decrypt(nonce, sealed, context.encode("utf-8"))
    except InvalidTag:
        raise DecryptionError("déchiffrement impossible (clé ou contexte incorrect)") from None


def encrypt(plaintext: str, *, context: str) -> bytes:
    return encrypt_bytes(plaintext.encode("utf-8"), context=context)


def decrypt(token: bytes, *, context: str) -> str:
    return decrypt_bytes(token, context=context).decode("utf-8")


def blind_index(normalized_value: str, *, context: str) -> str:
    """Empreinte déterministe (hexadécimale) pour la recherche d'égalité sur une valeur chiffrée."""
    message = context.encode("utf-8") + b"\x00" + normalized_value.encode("utf-8")
    return hmac.new(_current()._index_key, message, hashlib.sha256).hexdigest()


_OPAQUE_PADDING = bytes(7)
_OPAQUE_LENGTH = 26  # 16 octets en base32 sans remplissage


def _opaque_cipher() -> Cipher[modes.ECB]:
    key = hmac.new(_current()._index_key, b"assistant.opaque-ids", hashlib.sha256).digest()
    # Un seul bloc par identifiant : ECB est ici la permutation pseudo-aléatoire voulue.
    return Cipher(algorithms.AES(key), modes.ECB())  # noqa: S305


def opaque_id(kind: str, value: int) -> str:
    """« t_… » pour la transaction `value` : même entrée, même sortie ; rien de lisible."""
    if len(kind) != 1 or not kind.isascii() or not kind.isalpha():
        raise ValueError("type d'identifiant : une lettre")
    block = kind.encode() + _OPAQUE_PADDING + value.to_bytes(8, "big", signed=False)
    encryptor = _opaque_cipher().encryptor()
    sealed = encryptor.update(block) + encryptor.finalize()
    return f"{kind}_" + base64.b32encode(sealed).decode().rstrip("=").lower()


def parse_opaque_id(kind: str, text: str) -> int | None:
    """Identifiant d'origine, ou None si le texte n'est pas un identifiant `kind` valide."""
    prefix = f"{kind}_"
    body = text.strip()
    if not body.startswith(prefix) or len(body) != len(prefix) + _OPAQUE_LENGTH:
        return None
    encoded = body[len(prefix) :]
    try:
        sealed = base64.b32decode(encoded.upper() + "======")
    except ValueError:
        return None
    # Forme canonique seulement : le dernier caractère porte 2 bits ignorés au décodage.
    if base64.b32encode(sealed).decode().rstrip("=").lower() != encoded:
        return None
    decryptor = _opaque_cipher().decryptor()
    block = decryptor.update(sealed) + decryptor.finalize()
    if block[:1] != kind.encode() or block[1:8] != _OPAQUE_PADDING:
        return None
    return int.from_bytes(block[8:], "big")
