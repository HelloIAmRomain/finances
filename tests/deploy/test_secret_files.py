"""Aucun secret en clair versionné ; tout fichier *.enc.yaml est chiffré (inv. 5)."""

import subprocess
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files"],  # noqa: S607
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.splitlines()


def _leaves(node: Any) -> list[Any]:
    if isinstance(node, dict):
        return [leaf for value in node.values() for leaf in _leaves(value)]
    if isinstance(node, list):
        return [leaf for value in node for leaf in _leaves(value)]
    return [node]


def test_no_decrypted_secrets_tracked() -> None:
    forbidden = [
        path
        for path in _tracked_files()
        if path.endswith((".dec.yaml", ".pem", ".key")) or Path(path).name.startswith(".env")
    ]
    assert forbidden == []


def test_encrypted_files_are_fully_encrypted() -> None:
    for path in (p for p in _tracked_files() if p.endswith(".enc.yaml")):
        document = yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))
        assert "sops" in document, f"{path} : métadonnées sops absentes"
        data = {key: value for key, value in document.items() if key != "sops"}
        for leaf in _leaves(data):
            assert isinstance(leaf, str), f"{path} : valeur non chiffrée"
            assert leaf.startswith("ENC["), f"{path} : valeur en clair"
