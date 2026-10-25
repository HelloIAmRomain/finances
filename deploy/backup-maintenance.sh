#!/usr/bin/env bash
# Maintenance hebdomadaire du dépôt restic : rétention, purge et vérification (PLAN.md §1.5).
# Rétention : tout sur 24 h, puis un instantané par jour sur 30 jours, puis un par mois sur 12 mois.
set -euo pipefail
# shellcheck source=deploy/lib.sh
source "$(dirname "$(readlink -f "$0")")/lib.sh"

finish() {
	local status=$?
	if [[ $status -ne 0 ]]; then
		healthcheck_ping healthchecks_maintenance_url fail
	fi
}
trap finish EXIT

setup_restic

restic forget --quiet --host finances --tag db \
	--keep-within 24h --keep-daily 30 --keep-monthly 12 --prune
restic check --quiet --read-data-subset="${FINANCES_CHECK_SUBSET:-5%}"

log "rétention appliquée et dépôt vérifié"
healthcheck_ping healthchecks_maintenance_url
