#!/bin/bash
# Frigate liveness guard for ladybird.
#
# A port check on 5000 passes while recording is silently broken. This checks the
# things that actually matter and beats an Uptime Kuma push monitor only while ALL
# of them hold, so silence becomes the alert.
#
# Checks, per camera:
#   1. camera_fps > 0            - the RTSP stream is being pulled
#   2. detection_enabled == true - object detection is actually running
#   3. a recording segment was written in the last MAX_AGE_MIN minutes
#
# Check 3 is the one that catches a broken writer: fps can look perfect while
# nothing reaches disk. It never deletes anything. Its one remedial action: if the
# Frigate API stays unreachable (container "running" but wedged - Docker's
# restart policy and healthcheck do NOT act on that) for RESTART_AFTER
# consecutive runs, it restarts the container, at most once per RESTART_COOLDOWN_MIN.
set -uo pipefail

CONF=/opt/stacks/net/frigate-guard.conf
[ -r "$CONF" ] && . "$CONF"

: "${API:=http://127.0.0.1:5000}"
: "${REC_ROOT:=/srv/storage/frigate/recordings}"
: "${MAX_AGE_MIN:=5}"
: "${REQUIRE_DETECTION:=1}"
: "${AUTO_RESTART:=1}"
: "${RESTART_AFTER:=3}"
: "${RESTART_COOLDOWN_MIN:=30}"
: "${CONTAINER:=frigate}"
STATE_DIR=/run/frigate-guard
# Persistent, world-readable incident bundles; mounted read-only into Hermes.
: "${INCIDENT_ROOT:=/var/lib/frigate-incidents}"
KUMA_URL_FILE=/opt/stacks/net/kuma-frigate-push-url.txt

STATUS=ok
REASON=""
add() { REASON="${REASON:+$REASON; }$1"; }
fail() { STATUS=fail; add "$1"; }

STATS=$(curl -fsS --max-time 10 "$API/api/stats" 2>/dev/null)
if [ -z "$STATS" ]; then
  STATUS=fail
  REASON="frigate API unreachable at $API"
else
  # Emit "<camera> <camera_fps> <detection_enabled>" per camera.
  NCAM=0
  while read -r cam fps det; do
    [ -z "$cam" ] && continue
    NCAM=$((NCAM+1))

    awk -v f="$fps" 'BEGIN{exit !(f>0)}' || fail "$cam stream down (camera_fps=$fps)"

    if [ "$REQUIRE_DETECTION" = "1" ] && [ "$det" != "True" ]; then
      fail "$cam detection disabled"
    fi

    # Newest recording segment for this camera, anywhere in the date/hour tree.
    if [ -d "$REC_ROOT" ]; then
      if ! find "$REC_ROOT" -type f -name '*.mp4' -path "*/$cam/*" \
             -newermt "-${MAX_AGE_MIN} min" -print -quit 2>/dev/null | grep -q .; then
        fail "$cam no recording segment written in ${MAX_AGE_MIN}m"
      fi
    else
      fail "recordings root $REC_ROOT missing"
    fi
  done < <(printf '%s' "$STATS" | python3 -c "
import json,sys
s=json.load(sys.stdin)
for n,c in sorted(s.get('cameras',{}).items()):
    print(n, c.get('camera_fps',0), c.get('detection_enabled'))
" 2>/dev/null)

  if [ "$NCAM" -eq 0 ]; then
    fail "API returned no cameras"
  elif [ "$STATUS" = ok ]; then
    REASON="$NCAM cameras streaming, detecting and recording"
  fi
fi

# Auto-restart a wedged container (API unreachable N runs in a row).
# Camera-level failures are NOT restarted: they are usually the camera/network,
# and a restart would not fix them.
if [ "$AUTO_RESTART" = "1" ]; then
  mkdir -p "$STATE_DIR"
  if [ -z "$STATS" ]; then
    N=$(( $(cat "$STATE_DIR/api_fails" 2>/dev/null || echo 0) + 1 ))
    echo "$N" > "$STATE_DIR/api_fails"
    LAST=$(cat "$STATE_DIR/last_restart" 2>/dev/null || echo 0)
    NOW=$(date +%s)
    if [ "$N" -ge "$RESTART_AFTER" ] && [ $((NOW-LAST)) -ge $((RESTART_COOLDOWN_MIN*60)) ]; then
      # Evidence first: Frigate's real logs live in /dev/shm inside the container.
      # Camera credentials are redacted. Hermes reads these (read-only) to diagnose.
      INC="$INCIDENT_ROOT/$(date +%Y%m%dT%H%M%S)"
      mkdir -p "$INC" && chmod 755 "$INCIDENT_ROOT" "$INC"
      red() { sed -E 's#://[^/@[:space:]]+:[^/@[:space:]]+@#://***:***@#g'; }
      docker inspect "$CONTAINER" --format '{{json .State}}' 2>&1 | red > "$INC/container-state.json"
      docker stats --no-stream "$CONTAINER" > "$INC/docker-stats.txt" 2>&1
      for svc in frigate nginx go2rtc; do
        docker exec "$CONTAINER" tail -n 300 "/dev/shm/logs/$svc/current" 2>&1 | red > "$INC/$svc.log"
      done
      { uptime; free -m; df -h / /dev/shm; } > "$INC/host.txt" 2>&1
      chmod 644 "$INC"/*
      # Keep the 30 newest bundles.
      ls -1d "$INCIDENT_ROOT"/*/ 2>/dev/null | head -n -30 | xargs -r rm -rf
      if docker restart "$CONTAINER" >/dev/null 2>&1; then
        echo "$NOW" > "$STATE_DIR/last_restart"
        echo 0 > "$STATE_DIR/api_fails"
        add "auto-restarted $CONTAINER after $N consecutive API failures (evidence in $INC)"
      else
        add "auto-restart of $CONTAINER FAILED"
      fi
    fi
  else
    echo 0 > "$STATE_DIR/api_fails"
  fi
fi

# Beat Kuma only while fully healthy.
if [ "$STATUS" = ok ] && [ -s "$KUMA_URL_FILE" ]; then
  curl -fsS --max-time 10 \
    "$(head -1 "$KUMA_URL_FILE")&status=up&msg=$(printf '%s' "$REASON" | sed 's/ /%20/g')" \
    -o /dev/null || true
fi

echo "frigate-guard: $STATUS | $REASON"
[ "$STATUS" = ok ]
