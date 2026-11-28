#!/bin/bash
# Frigate full-footage archive: ladybird -> snibbly (docs/operations.md ->
# Frigate recordings archive).
#
# Frigate keeps one day of full footage here (record.continuous.days: 1), then
# thins each hour down to its motion segments. This script, run hourly, copies
# each hour directory to snibbly while it is still complete, and deletes
# snibbly's copies once they are KEEP_HOURS old. Net effect: KEEP_HOURS of full
# footage exists, the newest day on ladybird and the rest on snibbly, for one
# day's worth of ladybird disk.
#
# The copies are plain files, recordings/<UTC date>/<UTC hour>/<camera>/MM.SS.mp4,
# the layout Frigate writes. They are NOT in Frigate's timeline - play them with
# any video player, or concatenate an hour with ffmpeg.
#
# An hour is copied once it is ARCHIVE_AFTER_HOURS old (nothing writes to it any
# more) and for ARCHIVE_WINDOW_HOURS after that. If snibbly is unreachable for
# longer than (24 - ARCHIVE_AFTER_HOURS) hours, the hours that aged out in the
# meantime are archived motion-only, or not at all - Kuma will have paged long
# before that.
#
# Beats an Uptime Kuma push monitor ONLY on full success.
#
#   frigate-archive.sh            the real run (what the timer does)
#   frigate-archive.sh --dry-run  rsync -n + list what the prune would remove
set -uo pipefail

DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

CONF=/opt/stacks/net/frigate-archive.conf
[ -r "$CONF" ] && . "$CONF"

: "${RECORDINGS_DIR:=/srv/storage/frigate/recordings}"
: "${ARCHIVE_AFTER_HOURS:=18}"
: "${ARCHIVE_WINDOW_HOURS:=8}"
: "${KEEP_HOURS:=72}"
: "${MIN_REMOTE_FREE_GB:=60}"
: "${REMOTE_HOST:=}"
: "${REMOTE_DIR:=backups/frigate-recordings}"
KUMA_URL_FILE=/opt/stacks/net/kuma-frigate-archive-push-url.txt

fail() { echo "frigate-archive: $*" >&2; exit 1; }

[ -n "$REMOTE_HOST" ] || fail "REMOTE_HOST is empty in $CONF - nowhere to archive to"
[ -n "$REMOTE_DIR" ] || fail "REMOTE_DIR is empty in $CONF"
[ -d "$RECORDINGS_DIR" ] || fail "no $RECORDINGS_DIR - wrong RECORDINGS_DIR?"
[ "$KEEP_HOURS" -gt "$((ARCHIVE_AFTER_HOURS + ARCHIVE_WINDOW_HOURS))" ] \
  || fail "KEEP_HOURS ($KEEP_HOURS) must exceed ARCHIVE_AFTER_HOURS + ARCHIVE_WINDOW_HOURS, or the prune deletes what the copy just sent"
command -v rsync >/dev/null || fail "rsync missing (the base role installs it)"

SSH=(ssh -o BatchMode=yes -o ConnectTimeout=15)

# --- 1. room on snibbly --------------------------------------------------------
# snibbly's disk also holds the models and the vision archive. Refuse to copy
# into the last MIN_REMOTE_FREE_GB rather than fill it.
free_gb=$("${SSH[@]}" "$REMOTE_HOST" "mkdir -p '$REMOTE_DIR' && df --output=avail -BG '$REMOTE_DIR' | tail -1 | tr -dc 0-9") \
  || fail "cannot reach $REMOTE_HOST"
[ -n "$free_gb" ] || fail "could not read free space on $REMOTE_HOST"
[ "$free_gb" -ge "$MIN_REMOTE_FREE_GB" ] \
  || fail "$REMOTE_HOST has ${free_gb}G free (floor ${MIN_REMOTE_FREE_GB}G) - not copying"

# --- 2. copy the hours that are complete and not yet thinned -------------------
cd "$RECORDINGS_DIR" || fail "cannot cd to $RECORDINGS_DIR"
hours=()
for ((h = ARCHIVE_AFTER_HOURS; h < ARCHIVE_AFTER_HOURS + ARCHIVE_WINDOW_HOURS; h++)); do
  d=$(date -u -d "-$h hours" +%Y-%m-%d/%H)
  [ -d "$d" ] && hours+=("$d")
done
[ "${#hours[@]}" -gt 0 ] || fail "no hour directories between ${ARCHIVE_AFTER_HOURS}h and $((ARCHIVE_AFTER_HOURS + ARCHIVE_WINDOW_HOURS))h old - is Frigate recording?"

# --ignore-existing, never --delete: segments are immutable once written, and
# Frigate thinning an hour here must not thin the copy. -R keeps <date>/<hour>/.
RSYNC=(rsync -a -R --ignore-existing --timeout=600 -e "${SSH[*]}")
[ "$DRY" = 1 ] && RSYNC+=(--dry-run --stats)
"${RSYNC[@]}" "${hours[@]}" "$REMOTE_HOST:$REMOTE_DIR/" \
  || fail "rsync of ${#hours[@]} hour(s) to $REMOTE_HOST:$REMOTE_DIR failed"

# --- 3. prune snibbly ----------------------------------------------------------
# By directory NAME (UTC date/hour), not mtime: rsync -a preserves times, and a
# name cannot be confused with anything else under REMOTE_DIR. Only paths shaped
# exactly like <date>/<hour> are ever removed.
cutoff=$(date -u -d "-$KEEP_HOURS hours" +%Y-%m-%d/%H)
pruned=$("${SSH[@]}" "$REMOTE_HOST" bash -s -- "$REMOTE_DIR" "$cutoff" "$DRY" <<'EOS'
set -u
cd "$1" || exit 1
n=0
for d in [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]/[0-9][0-9]; do
  [ -d "$d" ] || continue
  if [[ "$d" < "$2" ]]; then
    if [ "$3" = 1 ]; then echo "would prune $d" >&2; else rm -rf -- "$d"; fi
    n=$((n + 1))
  fi
done
[ "$3" = 1 ] || rmdir --ignore-fail-on-non-empty [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] 2>/dev/null
echo "$n"
EOS
) || fail "prune on $REMOTE_HOST failed"

if [ "$DRY" = 1 ]; then
  echo "frigate-archive: dry run ok (${#hours[@]} hour(s) in window, $pruned would be pruned, ${free_gb}G free on remote) - nothing copied, Kuma not beaten"
  exit 0
fi

# Beat Kuma only now - every failure path above exited before this line.
if [ -r "$KUMA_URL_FILE" ]; then
  url=$(head -1 "$KUMA_URL_FILE"); url=${url%%\?*}
  curl -fsS -m 10 --retry 2 "$url?status=up&msg=archive+ok" >/dev/null 2>&1 || true
fi

echo "frigate-archive: ok (${#hours[@]} hour(s) in window, $pruned pruned, ${free_gb}G free on remote)"
