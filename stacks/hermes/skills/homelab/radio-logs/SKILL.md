---
name: radio-logs
description: Answer questions about aircraft/planes/flights that flew over or near the house (ADS-B from our own SDR receiver — lowest plane, closest approach, callsigns, what's overhead), Meshtastic mesh chat messages and nodes, and outdoor temperature/tire/utility-meter history (rtl_433), from JSONL files mounted read-only in the workspace. Use this for planes — NOT the camera/vision tools and NOT Home Assistant.
version: 1.0.0
author: homelab
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [radio, meshtastic, adsb, aircraft, rtl433, sensors, logs, homelab]
    category: homelab
    requires_toolsets: [file]
---

# Radio logs

The house runs three radio receivers and logs everything they hear to
newline-delimited JSON under `workspace/radiolog/` (read-only; one file per
day, local Denver time; files older than ~90 days are pruned). Use the file
tools to read them; for counting/filtering, grep-style line scans beat loading
whole files.

## When to use

Questions like: "any mesh messages today?", "what did the outdoor sensor read
overnight?", "what planes flew over this morning?", "who flew over the house?",
"when was the lowest plane?", "what's that plane overhead?", "has node X been
heard from?".

**Routing:** aircraft are detected by the radio receiver (SDR), never by the
cameras — do not search the vision archive for planes, and Home Assistant has
no aircraft data. Start here, then (for right-now traffic) the live feed below.

## Layout

```
workspace/radiolog/
  mesh/messages-YYYY-MM-DD.jsonl   Meshtastic text (chat) messages
  mesh/packets-YYYY-MM-DD.jsonl    all other mesh packets, one-line summaries
  rtl433/YYYY-MM-DD.jsonl          ISM-band decodes (433/315/912 MHz)
  adsb/YYYY-MM-DD.jsonl            aircraft sightings (1090 MHz)
```

## Schemas

### mesh/messages-*.jsonl — mesh chat

```json
{"ts":"2026-11-08T14:02:11-07:00","from":"!1bbeef5c","from_name":"Ladybird",
 "to":"^all","channel":0,"channel_name":"LongFast","snr":6.5,"rssi":-91,
 "hops":1,"text":"anyone on?"}
```

`channel_name` "LongFast" = the public/general Denver mesh channel;
"homemesh" = the private house channel. `to` is `^all` for broadcasts.
`mesh/packets-*.jsonl` has the same fields minus `text`, plus `port`
(POSITION_APP, NODEINFO_APP, TELEMETRY_APP…) — useful for "which nodes are
out there", not for conversation.

### rtl433/*.jsonl — ISM sensors

Raw rtl_433 decode plus `received_at`. The regulars:

- `"model":"Acurite-Tower","id":14893` — the outdoor temp/humidity sensor
  (`temperature_C`, `humidity`, `battery_ok`), one line ~every 30 s.
- TPMS models (Toyota, Schrader…) — tire pressure sensors from passing/parked
  cars, opportunistic.
- `"model":"SCMplus"` — utility meter readings (cumulative counters).

### adsb/*.jsonl — aircraft sightings

One record per aircraft per visit, written ~5 min AFTER it leaves coverage —
so very recent and currently-overhead traffic is NOT in the file yet; fetch
`http://192.168.0.13:8080/data/aircraft.json` (web tools) for live.

```json
{"hex":"a1b2c3","flight":"SWA1234","category":"A3",
 "first_seen":"2026-11-08T09:14:02-07:00","last_seen":"2026-11-08T09:21:32-07:00",
 "min_dist_nm":3.2,"min_alt":11200,"max_alt":36000,"max_gs":447,"polls":15}
```

`hex` = ICAO transponder id (stable per airframe), `flight` = callsign (null
for many GA/military), altitudes in ft, `min_dist_nm` = closest approach to
the house in nautical miles, `max_gs` = ground speed in knots.

## Aircraft summary — run this, don't eyeball it

For ANY "how many / lowest / closest / fastest / which planes" question, run
the snippet below in the terminal (sandbox has python3) and report ITS output.
Scanning dozens of JSON lines by eye gets the min/max wrong. Optional first
argument = a date (`YYYY-MM-DD`); default = newest file. Records with a null
`min_alt`/`min_dist_nm` (no position fix) are skipped for those rankings.

```bash
python3 - [YYYY-MM-DD] <<'EOF'
import glob, json, sys
d = "/opt/data/workspace/radiolog/adsb/"
f = d + sys.argv[1] + ".jsonl" if len(sys.argv) > 1 else sorted(glob.glob(d + "*.jsonl"))[-1]
r = [json.loads(l) for l in open(f) if l.strip()]
def show(x):
    return (f'{x.get("flight") or x["hex"]} hex={x["hex"]} alt={x["min_alt"]}ft '
            f'dist={x["min_dist_nm"]}nm gs={x["max_gs"]}kt {x["first_seen"][11:16]}-{x["last_seen"][11:16]}')
def top(key, rev=False):
    return sorted((x for x in r if x.get(key) is not None), key=lambda x: x[key], reverse=rev)[:5]
print(f, "-", len(r), "aircraft,", sum(1 for x in r if x.get("flight")), "with callsign")
print("LOWEST:");  [print(" ", show(x)) for x in top("min_alt")]
print("CLOSEST:"); [print(" ", show(x)) for x in top("min_dist_nm")]
print("FASTEST:"); [print(" ", show(x)) for x in top("max_gs", True)]
EOF
```

## Tips

- "Lowest/closest plane today": use the snippet above (smallest `min_alt` /
  `min_dist_nm`).
- Mesh may legitimately be silent for hours; an empty messages file for today
  is normal, not an error. The file for a day is only created on first write.
- These mounts are read-only — never try to write, rotate, or clean them.
