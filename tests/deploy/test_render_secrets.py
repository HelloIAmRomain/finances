"""Test de bout en bout de deploy/render-secrets.sh avec une clé age jetable."""

import base64
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from app.config import load_secrets

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "render-secrets.sh"
FAKE_KEY = "cle-de-test-" + "y" * 32
FAKE_PEM = "-----BEGIN TEST-----\nligne1\nligne2\n-----END TEST-----\n"
EXPECTED = ["app_secret_key", "blind_index_key", "data_encryption_key", "enable_banking_key"]
HOST_EXPECTED = ["b2_application_key", "ghcr_token", "restic_password"]
FAKE_RESTIC = "mot-de-passe-restic-factice"


def _tool(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        if os.environ.get("FINANCES_REQUIRE_TOOLS") == "1":
            pytest.fail(f"{name} requis en CI")
        pytest.skip(f"{name} absent")
    return path


def _run(
    args: list[str], env: dict[str, str] | None = None, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        args, capture_output=True, text=True, check=False, env=env, cwd=cwd
    )


@pytest.fixture
def encrypted(tmp_path: Path) -> tuple[Path, Path]:
    """Renvoie (fichier chiffré, clé age) pour des secrets factices."""
    sops, keygen = _tool("sops"), _tool("age-keygen")
    key = tmp_path / "age.key"
    assert _run([keygen, "-o", str(key)]).returncode == 0
    recipient = _run([keygen, "-y", str(key)]).stdout.strip()

    plain = tmp_path / "secrets.yaml"
    pem = "\n".join("  " + line for line in FAKE_PEM.splitlines())
    data_key, index_key = (base64.b64encode(os.urandom(32)).decode() for _ in range(2))
    plain.write_text(
        f"app_secret_key: {FAKE_KEY}\ndata_encryption_key: {data_key}\n"
        f"blind_index_key: {index_key}\nenable_banking_key: |\n{pem}\n"
        f"restic_password: {FAKE_RESTIC}\nb2_application_key: cle-b2-factice\n"
        "ghcr_token: jeton-factice\n",
        "utf-8",
    )
    enc = tmp_path / "secrets.enc.yaml"
    # Hors du dépôt : sa `.sops.yaml` ne s'applique pas à ces secrets factices.
    result = _run(
        [sops, "encrypt", "--age", recipient, "--output", str(enc), str(plain)], cwd=tmp_path
    )
    assert result.returncode == 0, result.stderr
    plain.unlink()
    return enc, key


def _render(
    enc: Path, key: Path, dest: Path, allow_disk: bool = True
) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ["PATH"],
        "SOPS_AGE_KEY_FILE": str(key),
        "FINANCES_SECRETS_DIR": str(dest),
        "FINANCES_HOST_SECRETS_DIR": str(dest.parent / "host-secrets"),
    }
    if allow_disk:
        env["FINANCES_SECRETS_ALLOW_DISK"] = "1"
    return _run(["bash", str(SCRIPT), str(enc)], env=env)


def test_encrypted_file_has_no_plaintext(encrypted: tuple[Path, Path]) -> None:
    enc, _ = encrypted
    content = enc.read_text("utf-8")
    assert FAKE_KEY not in content
    assert "ligne1" not in content


def test_renders_one_file_per_secret(encrypted: tuple[Path, Path], tmp_path: Path) -> None:
    enc, key = encrypted
    dest = tmp_path / "run" / "secrets"
    result = _render(enc, key, dest)
    assert result.returncode == 0, result.stderr
    assert FAKE_KEY not in result.stdout + result.stderr

    assert sorted(p.name for p in dest.iterdir()) == EXPECTED
    assert (dest / "enable_banking_key").read_text("utf-8") == FAKE_PEM
    assert stat.S_IMODE(dest.stat().st_mode) == 0o500
    for path in dest.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o400

    assert load_secrets(dest).app_secret_key.get_secret_value() == FAKE_KEY


def test_rerun_replaces_previous_secrets(encrypted: tuple[Path, Path], tmp_path: Path) -> None:
    enc, key = encrypted
    dest = tmp_path / "run" / "secrets"
    assert _render(enc, key, dest).returncode == 0
    assert _render(enc, key, dest).returncode == 0
    assert sorted(p.name for p in dest.iterdir()) == EXPECTED
    assert not [p for p in dest.parent.iterdir() if p.name.startswith(".secrets")]


def test_refuses_destination_on_disk(encrypted: tuple[Path, Path], tmp_path: Path) -> None:
    enc, key = encrypted
    dest = tmp_path / "run" / "secrets"
    fstype = _run(["findmnt", "-no", "FSTYPE", "--target", str(tmp_path)]).stdout.strip()
    if fstype in {"tmpfs", "ramfs"}:
        pytest.skip("le répertoire temporaire est déjà en mémoire")
    result = _render(enc, key, dest, allow_disk=False)
    assert result.returncode != 0
    assert "n'est pas en mémoire" in result.stderr
    assert not dest.exists()


def test_wrong_key_fails_without_output(encrypted: tuple[Path, Path], tmp_path: Path) -> None:
    enc, _ = encrypted
    other = tmp_path / "other.key"
    assert _run([_tool("age-keygen"), "-o", str(other)]).returncode == 0
    dest = tmp_path / "run" / "secrets"
    assert _render(enc, other, dest).returncode != 0
    assert not dest.exists()


def test_host_secrets_never_reach_the_containers(
    encrypted: tuple[Path, Path], tmp_path: Path
) -> None:
    """Sauvegarde, B2 et registre : dans le répertoire de l'hôte seulement. Le répertoire monté
    dans les conteneurs n'en contient aucun (un conteneur compromis ne peut pas les lire)."""
    enc, key = encrypted
    dest = tmp_path / "run" / "secrets"
    result = _render(enc, key, dest)
    assert result.returncode == 0, result.stderr
    host = dest.parent / "host-secrets"
    assert sorted(p.name for p in dest.iterdir()) == EXPECTED
    assert sorted(p.name for p in host.iterdir()) == HOST_EXPECTED
    assert (host / "restic_password").read_text("utf-8") == FAKE_RESTIC
    assert stat.S_IMODE(host.stat().st_mode) == 0o500
    assert FAKE_RESTIC not in result.stdout + result.stderr


@pytest.mark.parametrize("existing", [False, True])
def test_parent_directory_can_be_traversed(
    encrypted: tuple[Path, Path], tmp_path: Path, existing: bool
) -> None:
    """Régression (sauvegardes arrêtées du 19 au 26/10/2026) : le parent, créé sous umask 077 ou
    resté en 0700, empêchait les scripts de l'hôte (utilisateur finances) d'atteindre leurs
    secrets ; traversée permise, contenu toujours non listable."""
    enc, key = encrypted
    dest = tmp_path / "run" / "secrets"
    if existing:
        dest.parent.mkdir(mode=0o700)
    result = _render(enc, key, dest)
    assert result.returncode == 0, result.stderr
    mode = stat.S_IMODE(dest.parent.stat().st_mode)
    assert mode & 0o011 == 0o011
    assert mode & 0o044 == 0
