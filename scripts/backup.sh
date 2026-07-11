#!/usr/bin/env bash
# WAL-safe SQLite backup: uses sqlite3 ".backup", gzips, prunes to a
# rolling window. Designed for a cron job on the host machine.
#
#   ./scripts/backup.sh                              # backup default DB
#   RSSRADAR_BACKUP_KEEP=30 ./scripts/backup.sh      # keep 30 backups
#   RSSRADAR_BACKUP_DIR=/mnt/vol ./scripts/backup.sh # different target
#
# Cron example (daily at 03:15 UTC):
#   15 3 * * * cd /srv/rssradar && ./scripts/backup.sh >>/var/log/rssradar-backup.log 2>&1

set -euo pipefail

DB_PATH="${RSSRADAR_DB_PATH:-./data/rssradar.db}"
BACKUP_DIR="${RSSRADAR_BACKUP_DIR:-./backups}"
KEEP="${RSSRADAR_BACKUP_KEEP:-14}"

if ! command -v sqlite3 >/dev/null 2>&1; then
  echo "backup: sqlite3 not found on PATH" >&2
  exit 1
fi

if [[ ! -f "$DB_PATH" ]]; then
  echo "backup: DB not found at $DB_PATH" >&2
  exit 1
fi

mkdir -p "$BACKUP_DIR"
ts="$(date -u +%Y%m%d-%H%M%S)"
out="$BACKUP_DIR/rssradar-$ts.sqlite"

# .backup is WAL-safe — copies a consistent snapshot even while the
# app is writing. A plain `cp` would tear.
sqlite3 "$DB_PATH" ".backup '$out'"
gzip -f "$out"

# Integrity check on the archived copy before we count it as a keeper.
if ! gunzip -t "$out.gz" 2>/dev/null; then
  echo "backup: written archive failed gzip -t: $out.gz" >&2
  rm -f "$out.gz"
  exit 1
fi

# Prune: keep the newest $KEEP archives, delete the rest.
if compgen -G "$BACKUP_DIR"/rssradar-*.sqlite.gz > /dev/null; then
  # shellcheck disable=SC2012
  ls -1t "$BACKUP_DIR"/rssradar-*.sqlite.gz \
    | tail -n +$((KEEP + 1)) \
    | xargs -r rm -f
fi

bytes="$(wc -c <"$out.gz" | tr -d ' ')"
echo "backup ok: $out.gz ($bytes bytes)"
