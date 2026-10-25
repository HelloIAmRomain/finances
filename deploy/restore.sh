#!/usr/bin/env bash
# Restaure la base depuis le dernier instantané restic (ou celui passé en argument).
# Refuse d'écraser une base existante : la déplacer d'abord, en connaissance de cause.
#
# Usage : restore.sh DESTINATION [INSTANTANÉ]      (défaut : latest)
set -euo pipefail
# shellcheck source=deploy/lib.sh
source "$(dirname "$(readlink -f "$0")")/lib.sh"

dest="${1:?usage : restore.sh DESTINATION [INSTANTANÉ]}"
snapshot_id="${2:-latest}"
work="${FINANCES_BACKUP_TMP:-/run/finances-backup}"

[[ -e "$dest" ]] && die "$dest existe déjà : restauration refusée"
# Un journal WAL resté en place serait rejoué sur la base restaurée et la corromprait.
for leftover in "$dest-wal" "$dest-shm" "$dest-journal"; do
	[[ -e "$leftover" ]] && die "$leftover existe encore : le déplacer avec l'ancienne base"
done
setup_restic
mkdir -p "$work"
require_memory_fs "$work"

umask 077
restored="$(mktemp "$work/restore.XXXXXX.db")"
trap 'rm -f "$restored"' EXIT

restic dump --quiet --host finances --tag db "$snapshot_id" finances.db >"$restored"
[[ -s "$restored" ]] || die "instantané vide"
sqlite_integrity_ok "$restored" || die "instantané corrompu : restauration refusée"

mkdir -p "$(dirname "$dest")"
cp "$restored" "$dest.partial"
mv "$dest.partial" "$dest"
# Marqueur lu par le worker : réserve le budget DSP2 après restauration (PLAN §1.5).
date -u +%Y-%m-%dT%H:%M:%S+00:00 >"$(dirname "$dest")/.restored-at"
log "base restaurée dans $dest (instantané $snapshot_id)"
