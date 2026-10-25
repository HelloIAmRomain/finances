"""Chiffrement applicatif des données sensibles et index de recherche (NF-SEC-03, Q3)."""

import base64
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, insert, select, text

from app.crypto import (
    DecryptionError,
    Keyring,
    KeyringNotConfiguredError,
    blind_index,
    configure_keyring,
    decrypt,
    encrypt,
    opaque_id,
    parse_opaque_id,
    reset_keyring,
)
from app.db import make_engine
from app.db_types import EncryptedString
from app.iban import normalize_iban

# IBAN fictif (clé valide, banque inexistante) : jamais d'IBAN réel dans les tests (inv. 5).
FAKE_IBAN = "FR26 9999 9000 0100 0000 0000 045"


def _key() -> str:
    return base64.b64encode(os.urandom(32)).decode()


@pytest.fixture(autouse=True)
def keyring() -> Iterator[Keyring]:
    ring = Keyring.from_base64(data_key=_key(), index_key=_key())
    configure_keyring(ring)
    yield ring
    reset_keyring()


def test_roundtrip() -> None:
    token = encrypt("Jeanne Test", context="party.name")
    assert decrypt(token, context="party.name") == "Jeanne Test"


def test_ciphertext_hides_plaintext_and_is_randomized() -> None:
    first = encrypt(FAKE_IBAN, context="account.iban")
    second = encrypt(FAKE_IBAN, context="account.iban")
    assert first != second  # nonce aléatoire
    assert FAKE_IBAN.encode() not in first
    assert b"9999" not in first


def test_context_is_authenticated() -> None:
    token = encrypt(FAKE_IBAN, context="account.iban")
    with pytest.raises(DecryptionError):
        decrypt(token, context="party.iban")  # une valeur ne se déplace pas d'une colonne à l'autre


def test_wrong_key_fails() -> None:
    token = encrypt(FAKE_IBAN, context="account.iban")
    configure_keyring(Keyring.from_base64(data_key=_key(), index_key=_key()))
    with pytest.raises(DecryptionError):
        decrypt(token, context="account.iban")


def test_tampering_detected() -> None:
    token = bytearray(encrypt(FAKE_IBAN, context="account.iban"))
    token[-1] ^= 1
    with pytest.raises(DecryptionError):
        decrypt(bytes(token), context="account.iban")


def test_blind_index_is_deterministic_and_normalized() -> None:
    compact = blind_index(normalize_iban("fr2699999000010000000000045"), context="iban")
    spaced = blind_index(normalize_iban(FAKE_IBAN), context="iban")
    assert compact == spaced
    assert len(compact) == 64
    assert "9999" not in compact


def test_blind_index_depends_on_context_and_key() -> None:
    value = normalize_iban(FAKE_IBAN)
    assert blind_index(value, context="iban") != blind_index(value, context="autre")
    before = blind_index(value, context="iban")
    configure_keyring(Keyring.from_base64(data_key=_key(), index_key=_key()))
    assert blind_index(value, context="iban") != before


def test_keys_must_be_distinct_and_32_bytes() -> None:
    key = _key()
    with pytest.raises(ValueError, match="distinctes"):
        Keyring.from_base64(data_key=key, index_key=key)
    with pytest.raises(ValueError, match="32 octets"):
        Keyring.from_base64(data_key=base64.b64encode(b"court").decode(), index_key=_key())


def test_keyring_repr_hides_keys(keyring: Keyring) -> None:
    shown = repr(keyring) + str(keyring)
    assert shown == "Keyring(**masqué**)" * 2


def test_not_configured() -> None:
    reset_keyring()
    with pytest.raises(KeyringNotConfiguredError):
        encrypt("x", context="c")


def test_encrypted_column(tmp_path: Path) -> None:
    metadata = MetaData()
    accounts = Table(
        "accounts",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("iban_enc", EncryptedString("account.iban")),
    )
    engine = make_engine(tmp_path / "t.db")
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(insert(accounts).values(id=1, iban_enc=FAKE_IBAN))
        connection.execute(insert(accounts).values(id=2, iban_enc=None))
        raw: bytes = connection.execute(
            text("SELECT iban_enc FROM accounts WHERE id = 1")
        ).scalar_one()
        loaded = connection.execute(select(accounts.c.iban_enc).order_by(accounts.c.id)).all()
    assert FAKE_IBAN.encode() not in raw
    assert [row[0] for row in loaded] == [FAKE_IBAN, None]


@pytest.mark.parametrize("value", [0, 1, 7, 574, 10**6, 2**63 - 1])
@pytest.mark.parametrize("kind", ["t", "r", "b", "e"])
def test_opaque_id_roundtrip(value: int, kind: str) -> None:
    shown = opaque_id(kind, value)
    assert shown.startswith(f"{kind}_") and len(shown) == 28
    assert opaque_id(kind, value) == shown  # stable
    assert parse_opaque_id(kind, shown) == value
    assert parse_opaque_id(kind, f" {shown} ") == value


def test_opaque_id_rejects_forgeries_and_other_kinds() -> None:
    shown = opaque_id("t", 42)
    assert parse_opaque_id("r", "r" + shown[1:]) is None  # même corps, autre type
    for last in "abcdefghijklmnopqrstuvwxyz234567":  # une seule forme acceptée
        forged = shown[:-1] + last
        assert parse_opaque_id("t", forged) == (42 if forged == shown else None)
    assert parse_opaque_id("t", "t_42") is None
    assert parse_opaque_id("t", "t_" + "!" * 26) is None
    assert opaque_id("t", 1) != opaque_id("t", 2)
    with pytest.raises(ValueError, match="une lettre"):
        opaque_id("tx", 1)
