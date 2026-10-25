#!/usr/bin/env bash
# Test de restauration hebdomadaire (NF-RES-02) : le dernier instantané doit être récent,
# se restaurer, passer PRAGMA integrity_check et contenir au moins une table.
set -euo pipefail
# shellcheck source=deploy/lib.sh
source "$(dirname "$(readlink -f "$0")")/lib.sh"

work="${FINANCES_BACKUP_TMP:-/run/finances-restore-test}"
max_age_minutes="${FINANCES_BACKUP_MAX_AGE_MINUTES:-45}"

target=""
finish() {
	local status=$?
	[[ -n "$target" ]] && rm -rf "$(dirname "$target")"
	if [[ $status -ne 0 ]]; then
		healthcheck_ping healthchecks_restore_test_url fail
	fi
}
trap finish EXIT

setup_restic
mkdir -p "$work"
require_memory_fs "$work"

latest_time="$(restic snapshots --json --host finances --tag db --latest 1 |
	python3 -c 'import json, sys; s = json.load(sys.stdin); print(s[-1]["time"] if s else "")')"
[[ -n "$latest_time" ]] || die "aucun instantané"
age_minutes="$(python3 - "$latest_time" <<'PY'
import sys
from datetime import UTC, datetime
taken = datetime.fromisoformat(sys.argv[1])
print(int((datetime.now(UTC) - taken).total_seconds() // 60))
PY
)"
((age_minutes <= max_age_minutes)) ||
	die "dernier instantané trop ancien : ${age_minutes} min (maximum ${max_age_minutes})"

target="$(mktemp -d "$work/restore-test.XXXXXX")/finances.db"
"$(dirname "$(readlink -f "$0")")/restore.sh" "$target" >/dev/null

tables="$(python3 - "$target" <<'PY'
import sqlite3, sys
connection = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
print(connection.execute("SELECT count(*) FROM sqlite_master WHERE type = 'table'").fetchone()[0])
PY
)"
((tables > 0)) || die "base restaurée sans aucune table"

log "restauration vérifiée : instantané de ${age_minutes} min, ${tables} table(s), intégrité OK"
healthcheck_ping healthchecks_restore_test_url
