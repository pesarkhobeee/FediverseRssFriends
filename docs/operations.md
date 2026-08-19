# Operations: backup, restore, admin queries

## Backup + restore

The SQLite file lives at `./data/rssfriends.db` by default. Two scripts
handle WAL-safe backups with a rolling retention window:

```bash
make backup                            # snapshot → ./backups/rssfriends-<ts>.sqlite.gz
RSSFRIENDS_BACKUP_KEEP=30 make backup  # keep 30 archives (default 14)
make restore FROM=backups/rssfriends-20260101-030000.sqlite.gz
```

Backups use `sqlite3 .backup`, which is WAL-safe (a plain `cp` would
tear if the app is writing). Each archive is gzip-tested before it's
counted as a keeper, and pruning only touches files matching the exact
naming pattern — your own files in `backups/` are left alone.

Cron on the host:

```
15 3 * * * cd /srv/rssfriends && ./scripts/backup.sh >> /var/log/rssfriends-backup.log 2>&1
```

`restore.sh` moves the existing DB aside (`*.bak-<ts>`) before
overwriting, so an accidental restore never destroys data.

## Admin queries

There's no admin UI yet (`docs/instance-config.md` outlines the plan).
Meanwhile, poke at the SQLite file directly. The DB lives at
`data/rssfriends.db` locally, `/srv/rssfriends/data/rssfriends.db` on
the production host. All queries below assume you're on the host —
prepend `ssh user@your-host` and quote the whole `sqlite3 …`
invocation if you're running remotely.

### How many users?

```bash
sqlite3 data/rssfriends.db -header -column "
SELECT
  COUNT(*)                            AS total,
  SUM(visibility='public')            AS public,
  SUM(visibility='unlisted')          AS unlisted,
  SUM(visibility='private')           AS private
FROM user;
"
```

### Per-user breakdown (feeds, stars, follower graph)

```bash
sqlite3 data/rssfriends.db -header -column "
SELECT
  u.handle,
  u.email,
  u.visibility,
  substr(u.created_at, 1, 16) AS joined,
  (SELECT COUNT(*) FROM subscription s WHERE s.user_id = u.id) AS feeds,
  (SELECT COUNT(*) FROM favorite   f WHERE f.user_id = u.id) AS stars,
  (SELECT COUNT(*) FROM follow WHERE followee_id = u.id AND approved_at IS NOT NULL) AS followers,
  (SELECT COUNT(*) FROM follow WHERE follower_id = u.id AND approved_at IS NOT NULL) AS following
FROM user u
ORDER BY u.created_at;
"
```

### Fleet-wide feed health

```bash
sqlite3 data/rssfriends.db -header -column "
SELECT substr(url, 1, 70) AS url, consecutive_failures AS fails, substr(last_error, 1, 40) AS err
FROM feed
WHERE consecutive_failures > 0
ORDER BY consecutive_failures DESC
LIMIT 20;
"
```

### Recent activity across the instance

```bash
sqlite3 data/rssfriends.db -header -column "
SELECT
  substr(a.created_at, 1, 16) AS at,
  u.handle,
  a.kind,
  substr(a.subject, 1, 40) AS subject
FROM activity a
JOIN user u ON u.id = a.user_id
ORDER BY a.created_at DESC
LIMIT 20;
"
```

### Grant / revoke admin status

`is_admin` isn't a column yet (planned in
[`docs/instance-config.md`](instance-config.md)). For now, "admin" is
*whoever has SSH access to the box*.

## Safety notes

- `sqlite3` on a live DB is safe for `SELECT` — WAL mode means readers
  don't block writers. UPDATEs should still be preceded by a backup:
  `./scripts/backup.sh` first.
- For destructive changes (delete a user, purge failing feeds), always
  run `BEGIN; … ROLLBACK;` first to preview the row count. Then re-run
  with `COMMIT;`.
