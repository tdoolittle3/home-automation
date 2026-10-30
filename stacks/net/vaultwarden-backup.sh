#!/bin/bash
# Vaultwarden backup for ladybird (docs/vaultwarden.md).
# Nightly snapshot of the vault: a sqlite3 online .backup of the database
# plus a tar of everything else in /data (attachments, sends, the JWT
# signing keys), kept locally under BACKUP_DIR and optionally rsynced
# off-box. Beats an Uptime Kuma push monitor ONLY on full success, so a
# failing or hung backup surfaces as silence - same pattern as disk-guard.
set -uo pipefail

CONF=/opt/stacks/net/vaultwarden-backup.conf
[ -r "$CONF" ] && . "$CONF"

: "${DATA_DIR:=/opt/stacks/vaultwarden/vw-data}"
: "${BACKUP_DIR:=/srv/storage/backups/vaultwarden}"
: "${KEEP_DAYS:=30}"
: "${REMOTE_DEST:=}"
KUMA_URL_FILE=/opt/stacks/net/kuma-vw-backup-push-url.txt

fail() { echo "vaultwarden-backup: $*" >&2; exit 1; }

[ -f "$DATA_DIR/db.sqlite3" ] || fail "no database at $DATA_DIR/db.sqlite3"
command -v sqlite3 >/dev/null || fail "sqlite3 missing (the base role installs it)"

umask 077
mkdir -p "$BACKUP_DIR"

stamp=$(date +%Y%m%d-%H%M%S)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

# .backup takes a consistent snapshot while the container keeps writing -
# a plain cp of a live SQLite file is a corrupt backup waiting to restore.
sqlite3 "$DATA_DIR/db.sqlite3" ".backup '$work/db.sqlite3'" \
  || fail "sqlite3 .backup failed"

# The rest of /data matters too: attachments and sends are the files
# themselves, rsa_key* signs every client session, config.json carries
# admin-panel overrides. The live db and its -wal/-shm siblings are
# excluded - the snapshot above already has the database.
tar -C "$DATA_DIR" --exclude='./db.sqlite3*' --exclude='./tmp' \
  -cf "$work/data.tar" . || fail "tar of $DATA_DIR failed"

tar -C "$work" -czf "$BACKUP_DIR/vaultwarden-$stamp.tar.gz" db.sqlite3 data.tar \
  || fail "writing $BACKUP_DIR/vaultwarden-$stamp.tar.gz failed"

find "$BACKUP_DIR" -name 'vaultwarden-*.tar.gz' -mtime +"$KEEP_DAYS" -delete

# Off-box leg - a backup that dies with the disk is half a backup.
# Deliberately no --delete and --ignore-existing: local pruning never
# propagates, and a compromised ladybird cannot overwrite or empty the
# remote copies. One-time ssh key setup is in docs/vaultwarden.md.
if [ -n "$REMOTE_DEST" ]; then
  rsync -a --ignore-existing "$BACKUP_DIR"/vaultwarden-*.tar.gz "$REMOTE_DEST" \
    || fail "rsync to $REMOTE_DEST failed"
fi

# Beat Kuma only now - every failure path above exited before this line.
if [ -r "$KUMA_URL_FILE" ]; then
  curl -fsS -m 10 --retry 2 "$(cat "$KUMA_URL_FILE")?status=up&msg=backup+ok" \
    >/dev/null 2>&1 || true
fi

echo "vaultwarden-backup: ok (vaultwarden-$stamp.tar.gz)"
