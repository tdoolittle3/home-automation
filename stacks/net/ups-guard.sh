#!/bin/bash
# UPS guard for ladybird.
# Reads the CyberPower UPS over NUT (upsc), publishes power metrics to MQTT so
# Home Assistant surfaces them, and reports to the "UPS power" Uptime Kuma push
# monitor.
#
# Unlike disk-guard, this one pushes status=down ACTIVELY. The server survives
# a power cut - that is what the UPS is for - so it can say "on battery"
# itself, and the ntfy alert fires immediately instead of after a silence
# window. Silence still works as the fallback: a hung timer, a crashed NUT, or
# a dead server stops the beats and trips Kuma's heartbeat window.
set -uo pipefail

CONF=/opt/stacks/net/ups-guard.conf
[ -r "$CONF" ] && . "$CONF"

: "${UPS_NAME:=cyberpower}"
: "${CHARGE_WARN_PCT:=80}"     # warn if charge sits below this while on line power
: "${RUNTIME_WARN_MIN:=10}"    # warn if a full-ish battery predicts less runtime than this
MQTT_CONTAINER=mosquitto
STATE_TOPIC=ladybird/ups/state
DISC_PREFIX=homeassistant
KUMA_URL_FILE=/opt/stacks/net/kuma-push-url-ups.txt
SENTINEL=/var/lib/ups-guard/discovery-sent
DEVICE='{"identifiers":["ladybird_ups"],"name":"Ladybird UPS","manufacturer":"CyberPower","model":"PR1500LCDRT2U"}'

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
  local key name unit icon cls topic payload extra
  # key|name|unit|icon|device_class
  while IFS='|' read -r key name unit icon cls; do
    [ -z "$key" ] && continue
    extra=""
    [ -n "$cls" ] && extra=",\"device_class\":\"$cls\""
    topic="$DISC_PREFIX/sensor/ladybird_ups/$key/config"
    payload="{\"name\":\"$name\",\"unique_id\":\"ladybird_ups_$key\",\"state_topic\":\"$STATE_TOPIC\",\"unit_of_measurement\":\"$unit\",\"value_template\":\"{{ value_json.$key }}\",\"icon\":\"$icon\",\"state_class\":\"measurement\"$extra,\"device\":$DEVICE}"
    mqtt_pub "$topic" "$payload" retain
  done <<EOF
battery_charge|Battery Charge|%|mdi:battery|battery
runtime_min|Battery Runtime|min|mdi:timer-outline|duration
load_pct|UPS Load|%|mdi:gauge|
input_voltage|Input Voltage|V|mdi:sine-wave|voltage
EOF

  # problem binary sensor - fires on any non-ok status, reason in attributes
  topic="$DISC_PREFIX/binary_sensor/ladybird_ups/status/config"
  payload="{\"name\":\"Power Problem\",\"unique_id\":\"ladybird_ups_status\",\"state_topic\":\"$STATE_TOPIC\",\"value_template\":\"{{ 'ON' if value_json.status != 'ok' else 'OFF' }}\",\"device_class\":\"problem\",\"json_attributes_topic\":\"$STATE_TOPIC\",\"device\":$DEVICE}"
  mqtt_pub "$topic" "$payload" retain
}

# ---------- gather ----------
UPSC_OUT=$(upsc "$UPS_NAME" 2>/dev/null)
getval() { printf '%s\n' "$UPSC_OUT" | awk -F': ' -v k="$1" '$1==k{print $2; exit}'; }

UPS_STATUS=$(getval ups.status)
CHARGE=$(getval battery.charge)
RUNTIME_S=$(getval battery.runtime)
LOAD=$(getval ups.load)
INPUT_V=$(getval input.voltage)

# numeric fallbacks so the JSON stays valid when the UPS is unreachable
: "${CHARGE:=0}"; : "${RUNTIME_S:=0}"; : "${LOAD:=0}"; : "${INPUT_V:=0}"
RUNTIME_MIN=$(awk -v s="$RUNTIME_S" 'BEGIN{printf "%.0f", s/60}')

# ---------- evaluate ----------
STATUS=ok
REASON=""
add_reason() { REASON="${REASON:+$REASON; }$1"; }
has_token() { [[ " $UPS_STATUS " == *" $1 "* ]]; }
below() { awk -v a="$1" -v b="$2" 'BEGIN{exit !(a<b)}'; }

if [ -z "$UPS_STATUS" ]; then
  STATUS=critical; add_reason "UPS unreachable - NUT down or USB unplugged"
else
  # critical: running on battery, battery exhausted or dead, or output cut.
  # These push Kuma down, which is an immediate ntfy alert.
  has_token OB  && { STATUS=critical; add_reason "on battery - utility power lost"; }
  has_token LB  && { STATUS=critical; add_reason "LOW battery - shutdown imminent"; }
  has_token FSD && { STATUS=critical; add_reason "forced shutdown in progress"; }
  has_token RB  && { STATUS=critical; add_reason "battery failed self-test - replace it"; }
  has_token OFF && { STATUS=critical; add_reason "UPS output is off"; }

  # warning: degraded but still protected. Kuma stays up (the msg carries the
  # detail); the HA problem sensor turns on.
  if [ "$STATUS" = ok ]; then
    has_token OVER   && { STATUS=warning; add_reason "UPS overloaded"; }
    has_token BYPASS && { STATUS=warning; add_reason "on bypass - no battery protection"; }
    if below "$CHARGE" "$CHARGE_WARN_PCT"; then
      STATUS=warning; add_reason "charge ${CHARGE}% < ${CHARGE_WARN_PCT}% on line power"
    elif below "$RUNTIME_MIN" "$RUNTIME_WARN_MIN"; then
      # only meaningful on a charged battery - a recovering one always reads low
      STATUS=warning; add_reason "runtime ${RUNTIME_MIN}min < ${RUNTIME_WARN_MIN}min at ${CHARGE}% charge"
    fi
  fi
fi
[ -z "$REASON" ] && REASON="on line power, battery ${CHARGE}%"

# ---------- publish ----------
[ -f "$SENTINEL" ] || { publish_discovery && touch "$SENTINEL"; }
[ "${1:-}" = "--discovery" ] && publish_discovery

PAYLOAD="{\"battery_charge\":$CHARGE,\"runtime_min\":$RUNTIME_MIN,\"load_pct\":$LOAD,\"input_voltage\":$INPUT_V,\"ups_status\":\"$UPS_STATUS\",\"status\":\"$STATUS\",\"reason\":\"$REASON\"}"
mqtt_pub "$STATE_TOPIC" "$PAYLOAD" retain

# Uptime Kuma: up while protected (ok or warning), down on critical.
if [ -s "$KUMA_URL_FILE" ]; then
  PUSH_STATUS=up
  [ "$STATUS" = critical ] && PUSH_STATUS=down
  # msg must contain no raw spaces: curl >= 8.x rejects the whole URL as
  # malformed (exit 3, swallowed by the || true). Encode % first, then spaces.
  MSG=$(printf '%s' "$STATUS: $REASON (charge ${CHARGE}% runtime ${RUNTIME_MIN}min)" | sed 's/%/%25/g; s/ /+/g; s/;/%3B/g')
  curl -fsS --max-time 10 "$(head -1 "$KUMA_URL_FILE")&status=$PUSH_STATUS&msg=$MSG" -o /dev/null || true
fi

echo "ups-guard: $STATUS | $UPS_STATUS | charge ${CHARGE}% | runtime ${RUNTIME_MIN}min | load ${LOAD}% | input ${INPUT_V}V | $REASON"
[ "$STATUS" = ok ]
