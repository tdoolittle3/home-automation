#!/bin/bash
# External heartbeat for Uptime Kuma itself.
#
# Uptime Kuma cannot monitor its own death - if the container stops, so does every
# check it runs, silently. This script verifies Kuma is actually answering and only
# then pings an EXTERNAL service (healthchecks.io or equivalent). If Kuma dies, the
# host dies, or the power goes out, the ping stops and the external service alerts.
#
# Setup: create a check at healthchecks.io, put its ping URL in
#   /opt/stacks/net/healthchecks-url.txt   (chmod 600 - the URL is a secret)
# and set its period slightly longer than this timer's interval.
set -uo pipefail

KUMA_URL=${KUMA_URL:-http://127.0.0.1:3001}
HC_URL_FILE=/opt/stacks/net/healthchecks-url.txt

[ -s "$HC_URL_FILE" ] || { echo "kuma-watchdog: no ping URL at $HC_URL_FILE - nothing to do"; exit 0; }
HC=$(head -1 "$HC_URL_FILE")

# Kuma redirects / -> /dashboard, so 200 and 302 are both healthy.
CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$KUMA_URL/" 2>/dev/null)

if [ "$CODE" = "200" ] || [ "$CODE" = "302" ]; then
  curl -fsS --max-time 10 "$HC" -o /dev/null \
    && echo "kuma-watchdog: kuma http $CODE - heartbeat sent" \
    || echo "kuma-watchdog: kuma http $CODE - heartbeat FAILED to send"
  exit 0
fi

# Deliberately do NOT ping on failure - let the external check go silent and alert.
# Signal the failure explicitly too, so the alert arrives now rather than at timeout.
curl -fsS --max-time 10 "$HC/fail" -o /dev/null || true
echo "kuma-watchdog: kuma unhealthy (http ${CODE:-no-response}) - sent /fail"
exit 1
