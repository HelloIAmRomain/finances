#!/usr/bin/env bash
# Sauvegarde de la base (PLAN.md §1.5) : copie cohérente en mémoire par l'API backup de SQLite,
# vérification d'intégrité, puis envoi chiffré par restic. Rien ne transite par le SSD.
# Lancé toutes les 15 min par finances-backup.timer, sous l'utilisateur finances.
#
# Variables : FINANCES_DB (base), FINANCES_BACKUP_TMP (répertoire en mémoire),
#             FINANCES_BACKUP_TAG (étiquette restic, défaut « db »).
set -euo pipefail
# shellcheck source=deploy/lib.sh
source "$(dirname "$(readlink -f "$0")")/lib.sh"

db="${FINANCES_DB:-/srv/finances/data/finances.db}"
work="${FINANCES_BACKUP_TMP:-/run/finances-backup}"
tag="${FINANCES_BACKUP_TAG:-db}"

snapshot=""
finish() {
	local status=$?
	[[ -n "$snapshot" ]] && rm -f "$snapshot" "$snapshot-journal"
	if [[ $status -ne 0 ]]; then
		healthcheck_ping healthchecks_backup_url fail
	fi
}
trap finish EXIT

[[ -s "$db" ]] || die "base absente ou vide : $db"
setup_restic
mkdir -p "$work"
require_memory_fs "$work"

umask 077
snapshot="$(mktemp "$work/finances.XXXXXX.db")"

python3 - "$db" "$snapshot" <<'PY'
import sqlite3, sys
source = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
target = sqlite3.connect(sys.argv[2])
source.backup(target)
# Fichier autonome (sans WAL) pour qu'une restauration soit un simple fichier.
target.execute("PRAGMA journal_mode=DELETE")
target.close()
source.close()
PY

sqlite_integrity_ok "$snapshot" || die "copie corrompue : sauvegarde annulée"

restic backup --quiet --host finances --tag "$tag" \
	--stdin --stdin-filename finances.db <"$snapshot"

# Trace lue par le worker (alerte « aucune sauvegarde depuis 1 h », T1.21).
date -u +%Y-%m-%dT%H:%M:%S+00:00 >"$(dirname "$db")/.backup-last-success"
log "sauvegarde envoyée (étiquette $tag)"
healthcheck_ping healthchecks_backup_url
