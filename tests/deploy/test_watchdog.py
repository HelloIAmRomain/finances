"""Chien de garde du disque (NF-RES-05, PLAN P-3) : signal de vie seulement après une écriture."""

import configparser
import os
from pathlib import Path

from tests.deploy.helpers import DEPLOY, run

SCRIPT = DEPLOY / "watchdog.sh"
UNIT = DEPLOY / "systemd" / "finances-watchdog.service"


def _fake_notify(tmp_path: Path) -> tuple[dict[str, str], Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "notify.log"
    notify = bin_dir / "systemd-notify"
    notify.write_text(f'#!/bin/sh\necho "$*" >> {log}\n', encoding="utf-8")
    notify.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FINANCES_WATCHDOG_ITERATIONS": "2",
        "FINANCES_WATCHDOG_INTERVAL": "0",
    }
    return env, log


def test_signals_life_after_each_successful_write(tmp_path: Path) -> None:
    env, log = _fake_notify(tmp_path)
    state = tmp_path / "etat"
    state.mkdir()
    result = run([SCRIPT], {**env, "FINANCES_WATCHDOG_DIR": str(state)})
    assert result.returncode == 0, result.stderr
    lines = log.read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("--ready")
    assert lines[1:] == ["WATCHDOG=1", "WATCHDOG=1"]
    # Fichier réécrit sur place : taille fixe, aucun bloc ajouté (disque plein sans effet).
    assert (state / "sonde").stat().st_size == 512


def test_no_life_signal_when_the_disk_refuses_writes(tmp_path: Path) -> None:
    env, log = _fake_notify(tmp_path)
    state = tmp_path / "etat"
    (state / "sonde").mkdir(parents=True)  # écriture impossible, même pour root
    (state / "sonde" / "x").write_text("x", encoding="utf-8")
    result = run([SCRIPT], {**env, "FINANCES_WATCHDOG_DIR": str(state)})
    assert result.returncode == 0
    assert "WATCHDOG=1" not in log.read_text(encoding="utf-8")
    assert "écriture impossible" in result.stderr


def test_unit_reboots_without_life_signal() -> None:
    unit = configparser.ConfigParser(strict=False, interpolation=None)
    unit.optionxform = str  # type: ignore[assignment,method-assign]
    unit.read(UNIT, encoding="utf-8")
    assert unit["Unit"]["FailureAction"] == "reboot"
    assert unit["Unit"]["ConditionPathExists"] == "!/etc/finances/no-watchdog"
    service = unit["Service"]
    assert service["Type"] == "notify"
    assert service["NotifyAccess"] == "all"  # signal envoyé par systemd-notify, processus fils
    assert service["WatchdogSec"] == "5min"
    assert service["Restart"] == "no"
    assert service["ExecStart"] == "/srv/finances/deploy/watchdog.sh"
    assert unit["Install"]["WantedBy"] == "multi-user.target"


def test_bootstrap_keeps_the_journal_and_enables_the_watchdog() -> None:
    bootstrap = (DEPLOY / "bootstrap.sh").read_text(encoding="utf-8")
    # Lu après 40-rpi-volatile-storage.conf de Raspberry Pi OS, qui garde le journal en mémoire.
    assert "/etc/systemd/journald.conf.d/90-finances.conf" in bootstrap
    assert "Storage=persistent" in bootstrap
    assert "RuntimeWatchdogSec=15s" in bootstrap
    assert "RebootWatchdogSec=2min" in bootstrap
    assert "systemctl enable --now finances-watchdog.service" in bootstrap
