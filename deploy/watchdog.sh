#!/usr/bin/env bash
# Chien de garde du système (NF-RES-05, PLAN P-3). Le chien de garde matériel (systemd,
# RuntimeWatchdogSec) ne voit que le noyau et systemd : quand le support de stockage se fige
# (clé USB défaillante, pont USB en panne), ils tournent encore en mémoire et le Pi reste muet
# sans redémarrer. Ce script réécrit un petit fichier sur le disque, avec fsync, toutes les
# INTERVAL secondes, et ne signale « vivant » à systemd (WATCHDOG=1) qu'après une écriture
# réussie. Sans signal pendant WatchdogSec, systemd arrête l'unité et redémarre la machine
# (FailureAction=reboot) ; si l'arrêt se fige à son tour, le chien de garde matériel
# (RebootWatchdogSec) force le redémarrage.
#
# Le fichier est réécrit sur place (conv=notrunc) : aucun bloc nouveau n'est alloué, un disque
# plein ne déclenche donc pas de redémarrage (l'application le signale déjà, ALR-08).
# Un système de fichiers passé en lecture seule, lui, en déclenche un (fsck au démarrage).
#
# Variables : FINANCES_WATCHDOG_DIR (répertoire sondé, défaut $STATE_DIRECTORY),
#             FINANCES_WATCHDOG_INTERVAL (secondes, défaut 30),
#             FINANCES_WATCHDOG_ITERATIONS (tests : nombre de tours, défaut illimité).
set -euo pipefail

dir="${FINANCES_WATCHDOG_DIR:-${STATE_DIRECTORY:-/var/lib/finances-watchdog}}"
interval="${FINANCES_WATCHDOG_INTERVAL:-30}"
iterations="${FINANCES_WATCHDOG_ITERATIONS:-0}"
probe="$dir/sonde"

# Fichier préalloué une fois pour toutes (512 octets).
if [[ ! -s "$probe" ]]; then
	dd if=/dev/zero of="$probe" bs=512 count=1 conv=fsync status=none
fi

alive() {
	# Une écriture figée bloque ici (état D, insensible aux signaux) : plus aucun signal de vie.
	date +%s | dd of="$probe" bs=512 count=1 conv=notrunc,fsync status=none 2>/dev/null
}

systemd-notify --ready --status="sonde : $probe"
count=0
while ((iterations == 0 || count < iterations)); do
	if alive; then
		systemd-notify WATCHDOG=1
	else
		echo "écriture impossible sur $probe : pas de signal de vie" >&2
	fi
	count=$((count + 1))
	((iterations != 0 && count >= iterations)) || sleep "$interval"
done
