#!/bin/bash
# Storage guard for ladybird.
# Publishes disk/usage metrics to MQTT so Home Assistant surfaces them, and
# optionally reports to an Uptime Kuma push monitor (up while healthy, an
# active down carrying the reason otherwise).
# This script ALERTS ONLY - it never deletes recordings or media.
set -uo pipefail

CONF=/opt/stacks/net/disk-guard.conf
[ -r "$CONF" ] && . "$CONF"

: "${STORAGE_ROOT:=/srv/storage}"
: "${WARN_PCT:=75}"
: "${CRIT_PCT:=85}"
: "${CLEAR_MARGIN_PCT:=2}"   # a disk warning clears only below WARN_PCT - this (stops 75<->74 flapping)
: "${MIN_FREE_GB:=40}"
: "${FRIGATE_CAP_GB:=200}"
: "${MEDIA_CAP_GB:=150}"
: "${PHOTOS_CAP_GB:=100}"
MQTT_CONTAINER=mosquitto
STATE_TOPIC=ladybird/storage/state
DISC_PREFIX=homeassistant
KUMA_URL_FILE=/opt/stacks/net/kuma-push-url.txt
SENTINEL=/var/lib/disk-guard/discovery-sent
LAST_STATUS_FILE=/var/lib/disk-guard/last-status
DEVICE='{"identifiers":["ladybird_storage"],"name":"Ladybird Storage","manufacturer":"Minisforum","model":"M1 Plus"}'

mqtt_pub() {
  local topic="$1" payload="$2" retain="${3:-}"
  # NOTE: no -i, and stdin closed - docker exec otherwise eats the caller's heredoc
  if [ "$retain" = "retain" ]; then
    docker exec "$MQTT_CONTAINER" mosquitto_pub -h localhost -t "$topic" -m "$payload" -r 2>/dev/null </dev/null
  else
    docker exec "$MQTT_CONTAINER" mosquitto_pub -h localhost -t "$topic" -m "$payload" 2>/dev/null </dev/null
  fi
}

publish_discovery() {
  local key name unit icon cls topic payload oid
  # key|name|unit|icon|device_class
  # object_id pins the entity_id to ladybird_storage_<slugified name>, which is
  # what the dashboard's config/dashboard.json references.
  while IFS='|' read -r key name unit icon cls; do
    [ -z "$key" ] && continue
    oid="ladybird_storage_$(echo "$name" | tr 'A-Z ' 'a-z_')"
    topic="$DISC_PREFIX/sensor/ladybird_storage/$key/config"
    payload="{\"name\":\"$name\",\"unique_id\":\"ladybird_storage_$key\",\"object_id\":\"$oid\",\"state_topic\":\"$STATE_TOPIC\",\"unit_of_measurement\":\"$unit\",\"value_template\":\"{{ value_json.$key }}\",\"icon\":\"$icon\",\"state_class\":\"measurement\"${cls:+,\"device_class\":\"$cls\"},\"device\":$DEVICE}"
    mqtt_pub "$topic" "$payload" retain
  done <<EOF
disk_pct|Disk Used|%|mdi:harddisk|
free_gb|Disk Free|GB|mdi:database|
frigate_gb|Frigate Recordings|GB|mdi:cctv|
media_gb|Media Library|GB|mdi:filmstrip|
photos_gb|Immich Library|GB|mdi:image-multiple|
nvme_c|NVMe Temperature|°C|mdi:thermometer|temperature
EOF

  # problem binary sensor
  topic="$DISC_PREFIX/binary_sensor/ladybird_storage/status/config"
  payload="{\"name\":\"Storage Problem\",\"unique_id\":\"ladybird_storage_status\",\"object_id\":\"ladybird_storage_storage_problem\",\"state_topic\":\"$STATE_TOPIC\",\"value_template\":\"{{ 'ON' if value_json.status != 'ok' else 'OFF' }}\",\"device_class\":\"problem\",\"json_attributes_topic\":\"$STATE_TOPIC\",\"device\":$DEVICE}"
  mqtt_pub "$topic" "$payload" retain
}

# ---------- gather ----------
read -r _fs SIZE_B USED_B AVAIL_B _pct _mnt < <(df -PB1 "$STORAGE_ROOT" | tail -1)
DISK_PCT=$(( USED_B * 100 / SIZE_B ))
FREE_GB=$(awk -v b="$AVAIL_B" 'BEGIN{printf "%.1f", b/1000000000}')

dirsize_gb() {
  local d="$1"
  [ -d "$d" ] || { echo "0.0"; return; }
  local b
  b=$(du -sxb "$d" 2>/dev/null | cut -f1)
  awk -v x="${b:-0}" 'BEGIN{printf "%.1f", x/1000000000}'
}
FRIGATE_GB=$(dirsize_gb "$STORAGE_ROOT/frigate")
MEDIA_GB=$(dirsize_gb "$STORAGE_ROOT/media")
PHOTOS_GB=$(dirsize_gb "$STORAGE_ROOT/photos")
FILES_GB=$(dirsize_gb "$STORAGE_ROOT/files")

# NVMe temperature via the kernel hwmon interface - works unprivileged, unlike smartctl.
NVME_C=null
for h in /sys/class/hwmon/hwmon*; do
  if [ "$(cat "$h/name" 2>/dev/null)" = nvme ]; then
    t=$(cat "$h/temp1_input" 2>/dev/null) && [ -n "$t" ] && NVME_C=$(( t / 1000 ))
    break
  fi
done

# ---------- evaluate ----------
STATUS=ok
REASON=""
add_reason() { REASON="${REASON:+$REASON; }$1"; }
urlencode() {   # pure bash: the host has no jq
  local s="$1" out="" c i
  for (( i = 0; i < ${#s}; i++ )); do
    c=${s:i:1}
    case "$c" in
      [a-zA-Z0-9.~_-]) out+="$c" ;;
      ' ') out+="+" ;;
      *) out+=$(printf '%%%02X' "'$c") ;;
    esac
  done
  printf '%s' "$out"
}
over() { awk -v a="$1" -v b="$2" 'BEGIN{exit !(a>b)}'; }

LAST_STATUS=$(cat "$LAST_STATUS_FILE" 2>/dev/null || echo ok)
CLEAR_PCT=$(( WARN_PCT - CLEAR_MARGIN_PCT ))

if [ "$DISK_PCT" -ge "$CRIT_PCT" ]; then
  STATUS=critical; add_reason "disk ${DISK_PCT}% >= ${CRIT_PCT}%"
elif [ "$DISK_PCT" -ge "$WARN_PCT" ]; then
  STATUS=warning; add_reason "disk ${DISK_PCT}% >= ${WARN_PCT}%"
elif [ "$LAST_STATUS" != ok ] && [ "$DISK_PCT" -ge "$CLEAR_PCT" ]; then
  # Hysteresis. Frigate's hourly expiry swings usage by ~1%, so a disk sitting
  # at the threshold used to flip warning<->ok every half hour, and every flip
  # was a fresh down/up pair on the phone. Hold the warning until it is clearly
  # gone.
  STATUS=warning; add_reason "disk ${DISK_PCT}% (warning holds until < ${CLEAR_PCT}%)"
fi
if over "$MIN_FREE_GB" "$FREE_GB"; then
  STATUS=critical; add_reason "free ${FREE_GB}GB < ${MIN_FREE_GB}GB floor"
fi
if over "$FRIGATE_GB" "$FRIGATE_CAP_GB"; then
  [ "$STATUS" = ok ] && STATUS=warning; add_reason "frigate ${FRIGATE_GB}GB > ${FRIGATE_CAP_GB}GB cap"
fi
if over "$MEDIA_GB" "$MEDIA_CAP_GB"; then
  [ "$STATUS" = ok ] && STATUS=warning; add_reason "media ${MEDIA_GB}GB > ${MEDIA_CAP_GB}GB cap"
fi
if over "$PHOTOS_GB" "$PHOTOS_CAP_GB"; then
  [ "$STATUS" = ok ] && STATUS=warning; add_reason "photos ${PHOTOS_GB}GB > ${PHOTOS_CAP_GB}GB cap"
fi
[ -z "$REASON" ] && REASON="all thresholds nominal"

# ---------- publish ----------
[ -f "$SENTINEL" ] || { publish_discovery && touch "$SENTINEL"; }
[ "${1:-}" = "--discovery" ] && publish_discovery

PAYLOAD="{\"disk_pct\":$DISK_PCT,\"free_gb\":$FREE_GB,\"frigate_gb\":$FRIGATE_GB,\"media_gb\":$MEDIA_GB,\"photos_gb\":$PHOTOS_GB,\"files_gb\":$FILES_GB,\"nvme_c\":$NVME_C,\"status\":\"$STATUS\",\"reason\":\"$REASON\"}"
mqtt_pub "$STATE_TOPIC" "$PAYLOAD" retain

mkdir -p "$(dirname "$LAST_STATUS_FILE")" && echo "$STATUS" > "$LAST_STATUS_FILE"

# Uptime Kuma. A warning or critical pushes status=down ACTIVELY, carrying the
# reason, so the phone alert reads "storage warning: disk 76% >= 75%" instead
# of a bare "No heartbeat in the time window" - the guard knows exactly what is
# wrong, so it says so. Silence stays the fallback for a hung timer or a dead
# server. Kuma only notifies on a state change, so repeated down pushes while
# the problem persists do not re-alert.
if [ -s "$KUMA_URL_FILE" ]; then
  if [ "$STATUS" = ok ]; then
    PUSH_STATUS=up
    PUSH_MSG="ok: disk ${DISK_PCT}% free ${FREE_GB}GB"
  else
    PUSH_STATUS=down
    PUSH_MSG="storage ${STATUS}: ${REASON} (free ${FREE_GB}GB)"
  fi
  # msg must contain no raw spaces: curl >= 8.x rejects the whole URL as malformed
  # (exit 3, silently swallowed by the || true) - so URL-encode it.
  PUSH_MSG=$(urlencode "$PUSH_MSG")
  curl -fsS --max-time 10 "$(head -1 "$KUMA_URL_FILE")&status=${PUSH_STATUS}&msg=${PUSH_MSG}" -o /dev/null || true
fi

echo "storage-guard: $STATUS | disk ${DISK_PCT}% | free ${FREE_GB}GB | frigate ${FRIGATE_GB}GB | media ${MEDIA_GB}GB | photos ${PHOTOS_GB}GB | nvme ${NVME_C}C | $REASON"
[ "$STATUS" = ok ]
