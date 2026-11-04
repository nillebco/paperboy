#!/bin/sh
# Nightly Matins backup: a consistent SQLite snapshot plus the rest of the data
# volume (issues, images), one .tgz per night, the newest $KEEP kept.
#
# Copying app.db while the app writes can tear the WAL, so the database goes
# through sqlite3's online backup API inside the app container and is checked
# before it is archived. ENCRYPTION_KEY is NOT in here: without it the stored
# tokens cannot be decrypted, so keep it in the password manager.
#
# Restore: stop the app, extract into the volume, then
#   mv .snapshot/app.db app.db && rm -rf .snapshot
set -eu
DEST=${MATINS_BACKUP_DIR:-/srv/matins/backups}
KEEP=${MATINS_BACKUP_KEEP:-7}
OWNER=${MATINS_BACKUP_OWNER:-ubuntu}
STAMP=$(date -u +%Y-%m-%dT%H%MZ)
cd "$(dirname "$0")"

install -d -m 700 -o "$OWNER" "$DEST"
docker compose -p matins exec -T matins python - <<'PY'
import os, sqlite3
os.makedirs("/data/.snapshot", exist_ok=True)
target = "/data/.snapshot/app.db"
if os.path.exists(target):
    os.remove(target)
src, dst = sqlite3.connect("/data/app.db"), sqlite3.connect(target)
src.backup(dst)
src.close()
ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
dst.close()
if ok != "ok":
    raise SystemExit(f"snapshot failed integrity_check: {ok}")
PY
docker run --rm -v matins_matins-data:/data -v "$DEST":/backup alpine sh -c "
  tar czf /backup/.matins-$STAMP.tgz -C /data \
    --exclude=./app.db --exclude=./app.db-wal --exclude=./app.db-shm . &&
  rm -rf /data/.snapshot"
mv "$DEST/.matins-$STAMP.tgz" "$DEST/matins-$STAMP.tgz"
chown "$OWNER" "$DEST/matins-$STAMP.tgz"
chmod 600 "$DEST/matins-$STAMP.tgz"
ls -1t "$DEST"/matins-*.tgz | tail -n +"$((KEEP + 1))" | xargs -r rm -f
echo "backup: $DEST/matins-$STAMP.tgz ($(du -h "$DEST/matins-$STAMP.tgz" | cut -f1))"
