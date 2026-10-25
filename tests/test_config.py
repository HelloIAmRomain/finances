import base64
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import MissingSecretError, Settings, load_secrets
from app.crypto import Keyring

FAKE_KEY = "cle-de-test-" + "x" * 32


def _write_secret(directory: Path, name: str, value: str) -> None:
    (directory / name).write_text(value, encoding="utf-8")


def _write_all(directory: Path) -> None:
    _write_secret(directory, "app_secret_key", FAKE_KEY)
    _write_secret(directory, "data_encryption_key", base64.b64encode(os.urandom(32)).decode())
    _write_secret(directory, "blind_index_key", base64.b64encode(os.urandom(32)).decode())


def test_loads_secret_from_file(tmp_path: Path) -> None:
    _write_all(tmp_path)
    secrets = load_secrets(tmp_path)
    assert secrets.app_secret_key.get_secret_value() == FAKE_KEY


def test_secret_never_shown_in_repr_or_str(tmp_path: Path) -> None:
    _write_all(tmp_path)
    secrets = load_secrets(tmp_path)
    assert FAKE_KEY not in repr(secrets)
    assert FAKE_KEY not in str(secrets)
    assert FAKE_KEY not in secrets.model_dump_json()


def test_missing_secret_names_the_file(tmp_path: Path) -> None:
    with pytest.raises(MissingSecretError, match=r"app_secret_key"):
        load_secrets(tmp_path)


def test_missing_directory_is_explicit(tmp_path: Path) -> None:
    with pytest.raises(MissingSecretError, match="introuvable"):
        load_secrets(tmp_path / "absent")


def test_short_secret_rejected_without_leaking_it(tmp_path: Path) -> None:
    _write_all(tmp_path)
    _write_secret(tmp_path, "app_secret_key", "trop-court")
    with pytest.raises(MissingSecretError) as excinfo:
        load_secrets(tmp_path)
    assert "trop-court" not in str(excinfo.value)


def test_secret_from_environment_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP_SECRET_KEY", FAKE_KEY)
    monkeypatch.setenv("FINANCES_APP_SECRET_KEY", FAKE_KEY)
    with pytest.raises(MissingSecretError):
        load_secrets(tmp_path)


def test_settings_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FINANCES_DATABASE_PATH", "/srv/test/finances.db")
    assert Settings().database_path == Path("/srv/test/finances.db")


def test_keyring_from_secrets(tmp_path: Path) -> None:
    _write_all(tmp_path)
    keyring = Keyring.from_secrets(load_secrets(tmp_path))
    assert repr(keyring) == "Keyring(**masqué**)"


@pytest.mark.parametrize(
    ("name", "value", "explained"),
    [
        ("FINANCES_NTFY_TOPIC", "Alertes Maison", "finances-"),
        ("FINANCES_NTFY_URL", "https://ntfy.sh", "http://ntfy:8080"),
        ("FINANCES_PUBLIC_BASE_URL", "http://pi.local", "HTTPS"),
    ],
)
def test_settings_refused_at_startup_with_the_fix(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str, explained: str
) -> None:
    """Une valeur que la liste blanche refuserait : refus au démarrage, avec la marche à suivre,
    plutôt que des notifications qui échouent en silence."""
    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError, match=explained):
        Settings()


def test_settings_are_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FINANCES_PUBLIC_BASE_URL", "https://pi.exemple.ts.net/")
    monkeypatch.setenv("FINANCES_NTFY_URL", "http://ntfy:8080/")
    settings = Settings()
    assert settings.public_base_url == "https://pi.exemple.ts.net"
    assert settings.ntfy_url == "http://ntfy:8080"
