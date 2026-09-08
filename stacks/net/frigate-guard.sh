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
# nothing reaches disk. This script ALERTS ONLY - it never deletes or restarts.
set -uo pipefail

CONF=/opt/stacks/net/frigate-guard.conf
[ -r "$CONF" ] && . "$CONF"

: "${API:=http://127.0.0.1:5000}"
: "${REC_ROOT:=/srv/storage/frigate/recordings}"
: "${MAX_AGE_MIN:=5}"
: "${REQUIRE_DETECTION:=1}"
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

# Beat Kuma only while fully healthy.
if [ "$STATUS" = ok ] && [ -s "$KUMA_URL_FILE" ]; then
  curl -fsS --max-time 10 \
    "$(head -1 "$KUMA_URL_FILE")&status=up&msg=$(printf '%s' "$REASON" | sed 's/ /%20/g')" \
    -o /dev/null || true
fi

echo "frigate-guard: $STATUS | $REASON"
[ "$STATUS" = ok ]
