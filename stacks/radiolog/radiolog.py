#!/usr/bin/env python3
"""radiolog -- append-only JSONL logs of everything the house radios hear.

One script, three subcommands, one container each (see docker-compose.yml):

  mesh    Meshtastic text messages + compact packet summaries, read from the
          node over its USB serial port. Serial, NOT the node's MQTT uplink:
          firmware 2.7.26 stopped talking to the broker (docs/meshtastic.md)
          and the serial PhoneAPI is a separate transport from the phone's
          TCP slot, so this coexists with the phone app.
  rtl433  ISM-band decodes (outdoor temp sensor, TPMS, water meter) from the
          rtl_433/events MQTT topic (JSON, one message per decode -- the
          per-field rtl_433/<model>/<id>/* topics stay untouched for HA).
  adsb    Aircraft "sightings" condensed from readsb's aircraft.json: one
          record per aircraft per visit, not one per poll, so a day is a few
          hundred lines instead of a hundred thousand.

Files land in DATA_DIR/<kind>/[prefix]YYYY-MM-DD.jsonl, one JSON object per
line, timestamps local (the containers mount /etc/localtime). A prune pass
deletes files older than RETENTION_DAYS; it runs on the first write of each
day, so an idle logger never wakes the disk just to prune.

Consumed by the Hermes agent: /opt/stacks/radiolog/data is mounted read-only
into its workspace (stacks/hermes/docker-compose.yml) and the radio-logs
skill documents the schemas. Keep field names stable -- the skill quotes them.
"""

import json
import os
import re
import signal
import sys
import time
from datetime import date, datetime, timedelta

DATA_DIR = os.environ.get("DATA_DIR", "/data")
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "90"))

DATED = re.compile(r"(\d{4}-\d{2}-\d{2})\.jsonl$")


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


class DayFiles:
    """Append JSONL to DATA_DIR/<sub>/<prefix>YYYY-MM-DD.jsonl with pruning."""

    def __init__(self, sub):
        self.dir = os.path.join(DATA_DIR, sub)
        os.makedirs(self.dir, exist_ok=True)
        self._pruned_on = None

    def append(self, obj, prefix=""):
        day = date.today()
        path = os.path.join(self.dir, f"{prefix}{day.isoformat()}.jsonl")
        line = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
        # One open-append-close per record: a logger that crashes mid-day
        # loses at most the record in flight, and `tail -f` just works.
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        if self._pruned_on != day:
            self._pruned_on = day
            self._prune(day)

    def _prune(self, today):
        cutoff = (today - timedelta(days=RETENTION_DAYS)).isoformat()
        for name in os.listdir(self.dir):
            m = DATED.search(name)
            if m and m.group(1) < cutoff:
                try:
                    os.remove(os.path.join(self.dir, name))
                except OSError as e:
                    print(f"prune: {name}: {e}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# mesh -- Meshtastic over USB serial
# --------------------------------------------------------------------------

def run_mesh():
    from meshtastic.serial_interface import SerialInterface
    from pubsub import pub

    dev = os.environ.get("MESH_DEV", "/dev/ttyACM0")
    files = DayFiles("mesh")
    channels = {}

    def node_name(interface, num):
        try:
            user = interface.nodesByNum[num]["user"]
            return user.get("longName") or user.get("shortName")
        except (AttributeError, KeyError, TypeError):
            return None

    def on_receive(packet, interface):
        try:
            decoded = packet.get("decoded") or {}
            port = decoded.get("portnum")
            # hopStart appeared in fw 2.3; both present -> hops travelled.
            hops = None
            if packet.get("hopStart") is not None and packet.get("hopLimit") is not None:
                hops = packet["hopStart"] - packet["hopLimit"]
            rec = {
                "ts": now_iso(),
                "from": packet.get("fromId"),
                "from_name": node_name(interface, packet.get("from")),
                "to": packet.get("toId"),
                "channel": packet.get("channel", 0),
                "channel_name": channels.get(packet.get("channel", 0)),
                "snr": packet.get("rxSnr"),
                "rssi": packet.get("rxRssi"),
                "hops": hops,
            }
            if port == "TEXT_MESSAGE_APP":
                rec["text"] = decoded.get("text")
                files.append(rec, prefix="messages-")
            else:
                # Everything else (positions, nodeinfo, telemetry, traceroute)
                # as a one-line summary -- "who is out on the mesh" without
                # storing payloads.
                rec["port"] = port
                files.append(rec, prefix="packets-")
        except Exception as e:  # never let one bad packet kill the logger
            print(f"mesh: log error: {e}", file=sys.stderr, flush=True)

    def on_lost(interface=None):
        # The node rebooted or the USB device went away. The container's
        # /dev/ttyACM0 bind points at the OLD device node, so reopening from
        # inside is hopeless -- exit and let docker's restart policy re-bind
        # the fresh node (devices are resolved at container start).
        print("mesh: connection lost, exiting for restart", file=sys.stderr, flush=True)
        os._exit(1)

    pub.subscribe(on_receive, "meshtastic.receive")
    pub.subscribe(on_lost, "meshtastic.connection.lost")

    iface = SerialInterface(devPath=dev)  # blocks until config is fetched

    for ch in getattr(iface.localNode, "channels", None) or []:
        if ch.role:  # 0 = DISABLED
            # A blank primary-channel name means "use the modem preset name";
            # ours is LongFast (docs/meshtastic.md).
            channels[ch.index] = ch.settings.name or (
                "LongFast" if ch.role == 1 else f"ch{ch.index}"
            )
    me = getattr(iface, "myInfo", None)
    print(f"mesh: connected on {dev}, node={getattr(me, 'my_node_num', '?')}, "
          f"channels={channels}", file=sys.stderr, flush=True)

    # Idle mesh = silent serial line, and a dead line looks identical. The
    # heartbeat forces traffic so a stale connection surfaces as an exception
    # (-> exit -> restart) instead of a logger that just quietly stopped.
    while True:
        time.sleep(300)
        try:
            iface.sendHeartbeat()
        except Exception as e:
            print(f"mesh: heartbeat failed ({e}), exiting for restart",
                  file=sys.stderr, flush=True)
            os._exit(1)


# --------------------------------------------------------------------------
# rtl433 -- MQTT events topic
# --------------------------------------------------------------------------

def run_rtl433():
    import paho.mqtt.client as mqtt

    files = DayFiles("rtl433")
    host = os.environ.get("MQTT_HOST", "192.168.0.13")
    port = int(os.environ.get("MQTT_PORT", "1883"))
    topic = os.environ.get("RTL433_TOPIC", "rtl_433/events")
    last = {}  # (model, id, channel) -> monotonic time of last logged event

    def on_connect(client, userdata, flags, reason_code, properties=None):
        client.subscribe(topic)  # here, so it re-subscribes after reconnects
        print(f"rtl433: connected to {host}:{port}, subscribed {topic}",
              file=sys.stderr, flush=True)

    def on_message(client, userdata, msg):
        try:
            evt = json.loads(msg.payload)
        except ValueError:
            return
        # Sensors repeat each transmission several times and rtl_433 decodes
        # every copy; collapse bursts so the log has one line per reading.
        key = (evt.get("model"), evt.get("id"), evt.get("channel"))
        now = time.monotonic()
        if key in last and now - last[key] < 2.0:
            return
        last[key] = now
        evt["received_at"] = now_iso()
        files.append(evt)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(host, port, keepalive=60)
    client.loop_forever(retry_first_connection=True)


# --------------------------------------------------------------------------
# adsb -- poll readsb's aircraft.json, write one record per visit
# --------------------------------------------------------------------------

def run_adsb():
    import requests

    files = DayFiles("adsb")
    url = os.environ.get("AIRCRAFT_URL", "http://192.168.0.13:8080/data/aircraft.json")
    poll = int(os.environ.get("POLL_SECS", "30"))
    gone_after = int(os.environ.get("GONE_SECS", "300"))
    sightings = {}  # hex -> open sighting dict ("_last" = monotonic last-seen)

    def close(hexid):
        s = sightings.pop(hexid)
        s.pop("_last", None)
        files.append(s)

    def flush_and_exit(signum=None, frame=None):
        # SIGTERM (compose down / host shutdown): write out whatever is still
        # overhead so a restart doesn't swallow in-progress sightings.
        for h in list(sightings):
            close(h)
        sys.exit(0)

    signal.signal(signal.SIGTERM, flush_and_exit)

    while True:
        try:
            aircraft = requests.get(url, timeout=10).json().get("aircraft", [])
        except Exception as e:
            print(f"adsb: poll failed: {e}", file=sys.stderr, flush=True)
            time.sleep(poll)
            continue
        now = time.monotonic()
        for a in aircraft:
            # readsb keeps entries around after the last message; only count
            # aircraft actually heard since the previous poll.
            if not a.get("hex") or a.get("seen", 999) > 60:
                continue
            s = sightings.get(a["hex"])
            if s is None:
                s = sightings[a["hex"]] = {
                    "hex": a["hex"],
                    "flight": None,
                    "category": a.get("category"),
                    "first_seen": now_iso(),
                    "last_seen": None,
                    "min_dist_nm": None,
                    "min_alt": None,
                    "max_alt": None,
                    "max_gs": None,
                    "polls": 0,
                }
            s["last_seen"] = now_iso()
            s["_last"] = now
            s["polls"] += 1
            flight = (a.get("flight") or "").strip()
            if flight:
                s["flight"] = flight
            if not s["category"] and a.get("category"):
                s["category"] = a["category"]
            alt = a.get("alt_baro")  # can be the string "ground"
            if isinstance(alt, (int, float)):
                s["min_alt"] = alt if s["min_alt"] is None else min(s["min_alt"], alt)
                s["max_alt"] = alt if s["max_alt"] is None else max(s["max_alt"], alt)
            dst = a.get("r_dst")  # nm from the antenna; present because READSB_LAT/LON are set
            if isinstance(dst, (int, float)):
                s["min_dist_nm"] = round(dst if s["min_dist_nm"] is None
                                         else min(s["min_dist_nm"], dst), 1)
            gs = a.get("gs")
            if isinstance(gs, (int, float)):
                s["max_gs"] = gs if s["max_gs"] is None else max(s["max_gs"], gs)
        for h in [h for h, s in sightings.items() if now - s["_last"] > gone_after]:
            close(h)
        time.sleep(poll)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "mesh":
        run_mesh()
    elif cmd == "rtl433":
        run_rtl433()
    elif cmd == "adsb":
        run_adsb()
    else:
        sys.exit("usage: radiolog.py mesh|rtl433|adsb")


if __name__ == "__main__":
    main()
