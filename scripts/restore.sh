#!/usr/bin/env bash
# Restore a backup produced by scripts/backup.sh.
#
#   ./scripts/restore.sh backups/rssfriends-20260101-030000.sqlite.gz
#
# Refuses to overwrite an existing DB unless FORCE=1. The existing DB
# is moved aside to <name>.bak-<timestamp> so nothing is lost.

set -euo pipefail

SRC="${1:-}"
DB_PATH="${RSSFRIENDS_DB_PATH:-./data/rssfriends.db}"

if [[ -z "$SRC" ]]; then
  echo "usage: $0 <path-to-backup.sqlite.gz>" >&2
  exit 2
fi
if [[ ! -f "$SRC" ]]; then
  echo "restore: source not found: $SRC" >&2
  exit 1
fi

if [[ -f "$DB_PATH" && "${FORCE:-0}" != "1" ]]; then
  aside="$DB_PATH.bak-$(date -u +%Y%m%d-%H%M%S)"
  echo "restore: existing DB moved aside → $aside"
  mv "$DB_PATH" "$aside"
  rm -f "$DB_PATH-wal" "$DB_PATH-shm"
fi

mkdir -p "$(dirname "$DB_PATH")"
if [[ "$SRC" == *.gz ]]; then
  gunzip -c "$SRC" > "$DB_PATH"
else
  cp "$SRC" "$DB_PATH"
fi

# Integrity check before declaring success.
if ! sqlite3 "$DB_PATH" 'PRAGMA integrity_check;' | grep -q '^ok$'; then
  echo "restore: integrity_check failed" >&2
  exit 1
fi

echo "restore ok: $DB_PATH"
