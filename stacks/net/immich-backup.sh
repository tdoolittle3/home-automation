#!/bin/bash
# Immich off-box backup for ladybird (docs/operations.md -> Immich -> Backing it up).
#
# Immich already writes a nightly logical database dump itself (its "Database
# Backup" job, 02:00, into <UPLOAD_LOCATION>/backups, 14 kept) - the format its
# own restore procedure expects. What it does not do is get anything off this
# disk. This script, run after that job:
#   1. checks the newest dump is fresh and a valid gzip (a disabled or failing
#      job must page, not quietly leave us with a weeks-old database),
#   2. rsyncs the originals (upload/, library/, profile/) to REMOTE_DEST,
#   3. then rsyncs the dumps (db/ on the remote).
# Dump-before-files is the order Immich asks for: every row in the 02:00 dump
# points at a file that exists by the time this runs.
#
# Beats an Uptime Kuma push monitor ONLY on full success, so a stale dump, a
# failed transfer or a hung run all surface as silence - same pattern as
# vaultwarden-backup.sh.
#
#   immich-backup.sh            the real run (what the timer does)
#   immich-backup.sh --dry-run  all checks + rsync -n (lists what would copy),
#                               no transfer, no Kuma beat
set -uo pipefail

DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

CONF=/opt/stacks/net/immich-backup.conf
[ -r "$CONF" ] && . "$CONF"

: "${PHOTOS_DIR:=/srv/storage/photos}"
: "${MAX_DUMP_AGE_HOURS:=26}"
: "${REMOTE_DEST:=}"
KUMA_URL_FILE=/opt/stacks/net/kuma-immich-backup-push-url.txt

fail() { echo "immich-backup: $*" >&2; exit 1; }

[ -n "$REMOTE_DEST" ] || fail "REMOTE_DEST is empty in $CONF - nothing to back up to"
[ -d "$PHOTOS_DIR/upload" ] || fail "no $PHOTOS_DIR/upload - wrong PHOTOS_DIR?"
command -v rsync >/dev/null || fail "rsync missing (the base role installs it)"

# --- 1. the database dump Immich wrote ----------------------------------------
dumps="$PHOTOS_DIR/backups"
latest=$(ls -1t "$dumps"/immich-db-backup-*.sql.gz 2>/dev/null | head -1)
[ -n "$latest" ] || fail "no immich-db-backup-*.sql.gz in $dumps - is Administration -> Settings -> Backup -> Database Backup enabled?"

age_h=$(( ($(date +%s) - $(stat -c %Y "$latest")) / 3600 ))
[ "$age_h" -lt "$MAX_DUMP_AGE_HOURS" ] \
  || fail "newest dump $(basename "$latest") is ${age_h}h old (limit ${MAX_DUMP_AGE_HOURS}h) - Immich's backup job is not running"
gzip -t "$latest" 2>/dev/null || fail "newest dump $(basename "$latest") is not a valid gzip"

# --- 2+3. off-box -------------------------------------------------------------
# --ignore-existing and never --delete, like vaultwarden-backup.sh: a photo
# deleted (or encrypted by something nasty) on ladybird never propagates, and
# a compromised ladybird cannot overwrite the copies. Originals are immutable,
# so nothing legitimate needs overwriting. --timeout turns a dead link into a
# failure instead of a hang.
RSYNC=(rsync -a --ignore-existing --mkpath --timeout=600)
[ "$DRY" = 1 ] && RSYNC+=(--dry-run --stats)

items=()
for d in upload library profile; do
  [ -d "$PHOTOS_DIR/$d" ] && items+=("$PHOTOS_DIR/$d")
done
"${RSYNC[@]}" "${items[@]}" "$REMOTE_DEST" \
  || fail "rsync of originals to $REMOTE_DEST failed"

"${RSYNC[@]}" "$dumps"/immich-db-backup-*.sql.gz "${REMOTE_DEST%/}/db/" \
  || fail "rsync of database dumps to ${REMOTE_DEST%/}/db/ failed"

if [ "$DRY" = 1 ]; then
  echo "immich-backup: dry run ok (dump $(basename "$latest"), age ${age_h}h) - nothing copied, Kuma not beaten"
  exit 0
fi

# Beat Kuma only now - every failure path above exited before this line.
# The file may hold the bare push URL or kuma-provision's "...?ping=" form;
# drop any query string and add ours.
if [ -r "$KUMA_URL_FILE" ]; then
  url=$(head -1 "$KUMA_URL_FILE"); url=${url%%\?*}
  curl -fsS -m 10 --retry 2 "$url?status=up&msg=backup+ok" >/dev/null 2>&1 || true
fi

echo "immich-backup: ok (dump $(basename "$latest"), age ${age_h}h)"
