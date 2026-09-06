#!/usr/bin/env python3
"""Apply kuma-monitors.yml to a running Uptime Kuma.

The YAML stays the source of truth; this pushes it into Kuma's socket.io API so
the monitors do not have to be clicked in by hand. Idempotent: monitors are
matched by name, created when missing, updated when they have drifted.

Read docs/uptime-kuma.md before running. In particular this script deliberately
does NOT touch Settings (base URL, timezone, retention) - that call replaces the
whole settings object, and getting it wrong is a worse outage than clicking
three fields once.

This is an API client, not a plugin - run it anywhere that can reach port 3001.
NOT inside the Kuma container: that is a Node image, and anything installed in
it is lost on the next `docker compose pull`.

On the host, Debian 13 marks its Python externally-managed (PEP 668), so a bare
`pip install` refuses. Use a venv:

    sudo apt install -y python3-venv
    python3 -m venv /opt/stacks/net/.venv
    /opt/stacks/net/.venv/bin/pip install uptime-kuma-api pyyaml

    export KUMA_PASSWORD='...'          # never pass this as an argument - argv is world-readable in ps
    export NTFY_TOPIC='ladybird-xxxxx'  # optional; creates + attaches the ntfy notification

    /opt/stacks/net/.venv/bin/python /opt/stacks/net/kuma-provision.py           # dry run: changes nothing
    /opt/stacks/net/.venv/bin/python /opt/stacks/net/kuma-provision.py --apply   # actually writes

Environment:
    KUMA_URL       default http://192.168.0.13:3001
    KUMA_USERNAME  default admin
    KUMA_PASSWORD  required
    NTFY_TOPIC     optional
"""

import os
import sys
from pathlib import Path

try:
    import yaml
    from uptime_kuma_api import UptimeKumaApi, MonitorType, NotificationType
except ImportError as exc:
    sys.exit(f"missing dependency: {exc}\n  pip install uptime-kuma-api pyyaml")

DEFINITION = Path(__file__).with_name("kuma-monitors.yml")

# The YAML's type names, kept short and readable there, mapped to Kuma's.
# JSON_QUERY arrived in Kuma 1.23; fall back to the raw wire value so an older
# client library does not break the whole run.
TYPES = {
    "http": MonitorType.HTTP,
    "keyword": MonitorType.KEYWORD,
    "json-query": getattr(MonitorType, "JSON_QUERY", "json-query"),
    "port": MonitorType.PORT,
    "ping": MonitorType.PING,
    "push": MonitorType.PUSH,
}


def build_kwargs(m, parent_id, notification_ids):
    """Translate one YAML monitor block into Kuma's field names."""
    kw = {
        "name": m["name"],
        "interval": m.get("interval", m.get("heartbeat_interval", 60)),
        "maxretries": m.get("retries", 0),
        "retryInterval": m.get("retry_interval", 60),
    }
    if parent_id is not None:
        kw["parent"] = parent_id
    if notification_ids:
        kw["notificationIDList"] = {str(i): True for i in notification_ids}

    for src, dst in (
        ("url", "url"),
        ("hostname", "hostname"),
        ("port", "port"),
        ("keyword", "keyword"),
        ("json_path", "jsonPath"),
        ("expected_value", "expectedValue"),
        ("ignore_tls", "ignoreTls"),
        ("expiry_notification", "expiryNotification"),
    ):
        if src in m:
            kw[dst] = m[src]

    if "accepted_status" in m:
        kw["accepted_statuscodes"] = [str(c) for c in m["accepted_status"]]

    return kw


def main():
    apply = "--apply" in sys.argv
    url = os.environ.get("KUMA_URL", "http://192.168.0.13:3001")
    username = os.environ.get("KUMA_USERNAME", "admin")
    password = os.environ.get("KUMA_PASSWORD")
    ntfy_topic = os.environ.get("NTFY_TOPIC")

    if not password:
        sys.exit("KUMA_PASSWORD is not set. Export it rather than passing it as an argument.")

    spec = yaml.safe_load(DEFINITION.read_text())
    if not apply:
        print("DRY RUN - nothing will be written. Re-run with --apply.\n")

    api = UptimeKumaApi(url)
    api.login(username, password)
    try:
        existing = {mon["name"]: mon for mon in api.get_monitors()}

        # ---- notification -------------------------------------------------
        notification_ids = []
        if ntfy_topic:
            found = next((n for n in api.get_notifications() if n["name"] == "ntfy"), None)
            if found:
                notification_ids = [found["id"]]
                print("notification  ntfy: exists")
            elif apply:
                res = api.add_notification(
                    name="ntfy",
                    type=NotificationType.NTFY,
                    isDefault=True,
                    applyExisting=True,
                    ntfyserverurl=spec["notifications"][0]["server"],
                    ntfytopic=ntfy_topic,
                    ntfyPriority=spec["notifications"][0]["priority"],
                    ntfyAuthenticationMethod="none",
                )
                notification_ids = [res["id"]]
                print("notification  ntfy: created - send a test from the UI before trusting it")
            else:
                print("notification  ntfy: would create")
        else:
            print("notification  ntfy: skipped (NTFY_TOPIC unset) - monitors will alert nowhere")

        # ---- groups first, so children have a parent to point at ----------
        group_ids = {}
        for g in spec.get("groups", []):
            if g["name"] in existing:
                group_ids[g["name"]] = existing[g["name"]]["id"]
                print(f"group         {g['name']}: exists")
            elif apply:
                res = api.add_monitor(type=MonitorType.GROUP, name=g["name"])
                group_ids[g["name"]] = res["monitorID"]
                print(f"group         {g['name']}: created")
            else:
                print(f"group         {g['name']}: would create")

        # ---- monitors ------------------------------------------------------
        for m in spec["monitors"]:
            kw = build_kwargs(m, group_ids.get(m.get("group")), notification_ids)
            name = m["name"]
            if name in existing:
                if apply:
                    api.edit_monitor(existing[name]["id"], **kw)
                    print(f"monitor       {name}: updated")
                else:
                    print(f"monitor       {name}: would update")
            elif apply:
                api.add_monitor(type=TYPES[m["type"]], **kw)
                print(f"monitor       {name}: created")
            else:
                print(f"monitor       {name}: would create ({m['type']})")

        # ---- the push URL the storage guard needs --------------------------
        if apply:
            guard = next((mon for mon in api.get_monitors() if mon["name"] == "Storage guard"), None)
            if guard and guard.get("pushToken"):
                print(
                    f"\nStorage guard push URL:\n  {url}/api/push/{guard['pushToken']}\n\n"
                    "Put it on the server, then beat it once:\n"
                    "  printf '%s\\n' '<url above>' > /opt/stacks/net/kuma-push-url.txt\n"
                    "  chmod 600 /opt/stacks/net/kuma-push-url.txt\n"
                    "  /opt/stacks/net/disk-guard.sh"
                )
    finally:
        api.disconnect()


if __name__ == "__main__":
    main()
