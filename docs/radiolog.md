# radiolog — JSONL logs of what the house radios hear

`stacks/radiolog/` keeps an append-only, greppable history of all three
receivers, and feeds it to the Hermes agent. Before this stack, none of it was
persisted anywhere useful: mesh messages evaporated (the node's MQTT uplink has
been dead since Oct 4 — docs/meshtastic.md), rtl_433 readings only survived as
Home Assistant sensor history (~10 days, current-state oriented), and ADS-B
kept only tar1090's binary trace blobs.

## What gets logged

One container per source (one shared image; `radiolog.py <role>`):

| Container | Source | Writes |
|---|---|---|
| `mesh-logger` | Meshtastic node over USB serial (`/dev/ttyACM0`) | `data/mesh/messages-YYYY-MM-DD.jsonl` (chat), `data/mesh/packets-…` (position/nodeinfo/telemetry summaries) |
| `rtl433-logger` | MQTT topic `rtl_433/events` (JSON per decode) | `data/rtl433/YYYY-MM-DD.jsonl` — outdoor temp sensor, TPMS, utility meters |
| `adsb-logger` | `aircraft.json` polled every 30 s from tar1090 | `data/adsb/YYYY-MM-DD.jsonl` — one *sighting* per aircraft visit (first/last seen, closest approach, altitude band), written ~5 min after it leaves coverage |

All files are newline-delimited JSON, local time, one file per day, pruned
after `RETENTION_DAYS` (90). Volume is trivial: ~1 MB/day rtl433, well under
that for the others.

Design notes:

- **Mesh comes off the serial port, not MQTT.** Firmware 2.7.26 never
  reconnects to the broker (docs/meshtastic.md), and serial is a separate
  PhoneAPI transport from the phone's TCP slot — both coexist. This does NOT
  revive the dashboard's MQTT channel tabs; a follow-up could have
  `mesh-logger` republish to `msh/US/2/json/...` and fix those too.
- **`rtl_433/events` is new** — added alongside the per-field `devices=`
  topics in `stacks/sdr/docker-compose.yml` (HA's sensors hang off the
  per-field topics; don't touch those). The logger collapses the 2–3 duplicate
  decodes per sensor transmission into one line.
- **ADS-B logs sightings, not polls.** Logging every aircraft every 30 s would
  be ~100k lines/day of noise; a visit record is a few hundred.

## Hermes access

`/opt/stacks/radiolog/data` is mounted **read-only** at
`workspace/radiolog/` in the Hermes gateway (stacks/hermes/docker-compose.yml),
and the `radio-logs` skill (stacks/hermes/skills/homelab/radio-logs/SKILL.md)
tells the agent the layout and schemas. The converge installs the skill into
the gitignored `data/skills/homelab/` (stacks role — no hand copy).

After a compose change, recreate the gateway (`docker compose up -d` in
/opt/stacks/hermes) so the new mount appears.

## Deploy / update

File-synced by ansible (`stacks_whole_dirs`) but not auto-up'd — the image is
built in place:

```
cd /opt/stacks/radiolog
docker compose up -d --build
```

The rtl_433 side needs the sdr stack recreated once to pick up the `events=`
output: `cd /opt/stacks/sdr && docker compose up -d rtl_433`.

## Gotchas

- **mesh-logger owns /dev/ttyACM0.** `stacks/mesh`'s
  `docker compose run cli --port /dev/ttyACM0` fails while it runs — stop
  mesh-logger first, or (current practice) admin over the network with
  `--host 192.168.0.16`.
- **Node reboot = stale device bind.** The container exits on a lost serial
  connection *on purpose*; docker's restart re-resolves `/dev/ttyACM0`. A
  crash-looping mesh-logger usually means the node is actually offline or
  enumerated at a new path — check `ls -l /dev/serial/by-id/`.
- **An empty/missing mesh messages file is normal** — the mesh can be quiet
  all day, and a day's file is only created on its first record.
- **Render-owner note:** the loggers run as root, so the JSONL files are
  root-owned; Hermes reads them fine (0644), just don't expect to edit them
  as thomas without sudo.
