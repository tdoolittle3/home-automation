# Meshtastic — LoRa mesh bridged to the LAN

An ESP32 Meshtastic node on the house Wi‑Fi acts as the **gateway** between
the LoRa mesh and the Mosquitto broker in the `home` stack. It carries the
public **LongFast** channel (primary — regional chat, and what makes the node
a useful relay for the mesh) plus the private **homemesh** channel
(secondary); the dashboard renders messages and node telemetry, and can send
onto `homemesh`. There is **no public-MQTT uplink**: `mqtt.address` points at
`192.168.0.13`, never `mqtt.meshtastic.org`, and remote access is
Tailscale → dashboard like everything else here. Public participation is
RF-only.

> **Why LongFast must be the primary.** The LoRa frequency slot is hashed
> from the *primary channel's name*. From 2026-09-27 to 2026-10-27 the
> primary was named `homemesh`, which put the radio on slot 60
> (916.875 MHz) while the entire public US mesh sits on LongFast's slot 20
> (906.875 MHz) — the node transmitted beacons nobody heard and received
> **zero** packets for a month. Private channel = secondary, always.

The broker already exists (`stacks/home/mosquitto/`, anonymous, LAN-only)
and the panel lives in the
[home-dashboard](https://github.com/tdoolittle3/home-dashboard) repo. What
this repo carries is the node runbook, a containerised CLI wrapper
(`stacks/mesh/` — see §1), the dashboard's mesh environment in
`stacks/dash/docker-compose.yml`, and the Kuma monitor.

| What | Where |
|---|---|
| Uplink (mesh → broker), decoded JSON | `msh/US/2/json/{homemesh,LongFast}/!<gatewayid>` |
| Downlink (broker → mesh), JSON envelope | `msh/US/2/json/mqtt/` |
| Protobuf twin of the uplink | `msh/US/2/e/{homemesh,LongFast}/!<gatewayid>` |
| Dashboard panel | `Mesh` on http://192.168.0.13/ |

Channel layout since 2026-10-27 (index matters — downlink envelopes address
channels by index, see §3):

| Index | Name | PSK | Uplink | Downlink |
|---|---|---|---|---|
| 0 (primary) | `LongFast` | default (`AQ==`) — public | ✓ | ✓ |
| 1 | `mqtt` | (unused — JSON-downlink listener only) | | ✓ |
| 2 | `homemesh` | private 256-bit | ✓ | ✓ |

Topic scheme per the
[Meshtastic MQTT docs](https://meshtastic.org/docs/software/integrations/mqtt/).
The docs also describe a retained liveness topic (`msh/<root>/2/stat/<id>`);
**firmware 2.7.26 does not publish it** — verified against a live subscriber
— which is why Kuma pings the node's IP instead and the dashboard's gateway
dot reads "unknown".

## The deployed node

| | |
|---|---|
| Hardware | Heltec V4 (ESP32-S3) — Wi‑Fi, so `mqtt.json_enabled` works |
| Node id / number | `!1bbeef5c` / `465497948` (this is `MESH_GATEWAY_NODE`) |
| Wi‑Fi MAC / IP | `f8:5b:1b:be:ef:5c` / `192.168.0.16` — **DHCP-reserve this on the router** |
| Firmware at provisioning | 2.7.26, 2026-09-27 |
| Physically | Since 2026-10-27: on a USB power bank atop the server rack. **No serial path** — admin is `--host 192.168.0.16` over TCP (see §1) |
| On-device web UI | http://192.168.0.16/ (and https, self-signed) — serves the full web client |
| Config backup (PSK + Wi‑Fi password) | `/home/thomas/meshtastic-node1.yaml`, mode 600, **never in git** |

---

## 1. Provision the node

`thomas` is not in `dialout` and the node no longer hangs off ladybird's USB
anyway, so the CLI runs containerised — nothing installs on the host.
`stacks/mesh/` holds a one-service compose file (profile-gated, never part of
`docker compose up`) that wraps the pinned Meshtastic CLI. Admin goes over
TCP to the node's Wi‑Fi IP (`--port /dev/ttyACM0` still works if the node is
ever back on a USB *data* port):

```bash
cd /opt/stacks/mesh
docker compose --profile cli build          # once, and after a wipe
docker compose run --rm cli --host 192.168.0.16 --info
```

Every `meshtastic ...` command below is really
`docker compose run --rm cli --host 192.168.0.16 ...`. Order matters: the
node reboots after each config write (expect a harmless `BrokenPipeError` as
it drops the TCP session — **verify each write landed with `--info` before
the next one**; a write occasionally gets lost in the reboot), and the
channel URL changes when the channel set does, so share the QR only after
step 2.

```bash
# region first; the radio will not transmit without one
meshtastic --set lora.region US

# the primary stays LongFast (default PSK): the frequency slot is hashed
# from the PRIMARY channel's name, so this is what keeps the radio on the
# public mesh's slot (US slot 20, 906.875 MHz). Uplink feeds the dashboard;
# downlink lets the dashboard send to regional chat.
meshtastic --ch-index 0 --ch-set uplink_enabled true --ch-set downlink_enabled true

# JSON-downlink control channel: the firmware only listens on .../json/mqtt/
# when a channel literally named "mqtt" has downlink enabled
meshtastic --ch-add mqtt
meshtastic --ch-set downlink_enabled true --ch-index 1

# the private channel, as a SECONDARY. --ch-add invents a random PSK; if
# re-provisioning, overwrite it with the existing one from the config backup
# (psk base64:<...>) so previously-joined nodes keep working.
meshtastic --ch-add homemesh
meshtastic --ch-index 2 --ch-set psk random --ch-set uplink_enabled true --ch-set downlink_enabled true

# Wi-Fi. On ESP32 this disables Bluetooth - provisioning from here on is
# USB/CLI or the node's HTTP UI, not the phone-BLE path.
meshtastic --set network.wifi_enabled true \
  --set network.wifi_ssid "<SSID>" --set network.wifi_psk "<wifi password>"

# MQTT module -> the existing Mosquitto (anonymous: blank credentials).
# Set mqtt.root explicitly so the dashboard env cannot drift from a firmware default.
meshtastic --set mqtt.enabled true \
  --set mqtt.address 192.168.0.13 \
  --set mqtt.username "" --set mqtt.password "" \
  --set mqtt.encryption_enabled false \
  --set mqtt.json_enabled true \
  --set mqtt.root "msh/US"

# a sleeping ESP32 drops MQTT and flaps the stat topic
meshtastic --set power.is_power_saving false
```

Then record the identity and back up:

```bash
meshtastic --info    # note "myNodeNum" (decimal) -> MESH_GATEWAY_NODE below
meshtastic --qr      # scan from the phone app to join other nodes to homemesh
meshtastic --export-config > meshtastic-node1.yaml
```

**`meshtastic-node1.yaml` never goes in git** — it holds the channel PSK and
the Wi‑Fi password. Keep it with the other off-repo secrets.

---

## 2. Verify at the broker — before touching the dashboard

This isolates node config from dashboard config. From any LAN machine with
`mosquitto-clients`:

```bash
# nodeinfo/telemetry JSON appears within a few minutes of the node booting
mosquitto_sub -h 192.168.0.13 -t 'msh/#' -v

# just one channel's decoded JSON (homemesh or LongFast)
mosquitto_sub -h 192.168.0.13 -t 'msh/US/2/json/homemesh/+' -v
```

(Do not wait for a retained `stat` frame — 2.7.26 never sends one.)

Send a text from the phone app on `homemesh` — it appears as `"type":"text"`
JSON. Then prove the downlink path (this is the step that validates the
`mqtt`-named channel requirement — note the firmware version you tested):

```bash
mosquitto_pub -h 192.168.0.13 -t 'msh/US/2/json/mqtt/' \
  -m '{"from": <myNodeNum>, "type": "sendtext", "payload": "hello from broker", "channel": 2}'
```

The message should show on the node/app. A wrong `from` — anything but the
gateway's own node number — is **dropped without an error anywhere**.

---

## 3. Wire up the dashboard

The five `MESH_*` variables sit in the inline `environment:` block of
`stacks/dash/docker-compose.yml` (none are secrets). Fill in
`MESH_GATEWAY_NODE` with `myNodeNum` from step 1, then:

```bash
cd /opt/stacks/dash && docker compose up -d --force-recreate
```

`MESH_CHANNEL` is the channel **name** (an uplink topic segment);
`MESH_CHANNEL_INDEX` is the numeric **index** sends go out on. Same channel,
two representations — a drift between them sends dashboard messages out the
wrong channel, silently. Leaving `MESH_GATEWAY_NODE` or
`MESH_CHANNEL_INDEX` unset makes the panel read-only.

The Kuma monitor (`Meshtastic gateway` in `stacks/net/kuma-monitors.yml`)
pings the node's reserved IP — the stat-topic MQTT check the Meshtastic docs
suggest is not possible on this firmware (no stat topic). Provision it as in
[uptime-kuma.md](uptime-kuma.md).

---

## Security posture — read before "improving" it

**The JSON topics are plaintext on the broker, always.** Meshtastic never
encrypts the JSON plane; `mqtt.encryption_enabled` only governs the protobuf
`2/e/` topics. That is accepted here because the broker is LAN-only (never
port-forwarded), remote access is Tailscale → dashboard only, and the RF
side stays PSK-encrypted end to end.

The corollary: **anyone on the LAN can publish a downlink** and transmit on
the mesh — since 2026-10-27 that includes `channel: 0`, the **public**
LongFast channel heard by the whole region. That is the same trust boundary
as the rest of the anonymous broker — Frigate events and the guards live
under it too. If the LAN stops being trusted, the fix is Mosquitto auth for
everything, not a special case here.

The gateway only JSON-serializes packets on channels **whose key it holds**.
Today that is `homemesh` (it owns the channel) and `LongFast` (the
well-known default key). A channel added later will never appear as JSON
until its key is loaded onto the gateway node.

---

## Gotchas

- **The primary channel's NAME picks the radio frequency.** Renaming the
  primary (or demoting LongFast from index 0) moves the node to a hashed
  frequency slot where the public mesh cannot hear it — and nothing errors.
  This silently deafened the node for a month (see the intro note). After
  any channel surgery, confirm `curl http://192.168.0.16/json/report` shows
  `lora_channel: 20` and a non-zero `rx_all_log` within the hour.
- **Firmware 2.7.26 stops retrying the MQTT broker.** After the broker was
  unreachable (2026-10-04 ladybird outage) the node never attempted another
  connection — across reboots, for 23 days. Fix: toggle `mqtt.enabled`
  false → true. Verify with `docker logs mosquitto | grep '!1bbeef5c'`.
- **JSON is ESP32-only.** `mqtt.json_enabled` does nothing on nRF52/RP2040
  boards — a future board swap silently loses the entire JSON plane and the
  dashboard goes blank while the protobuf topics keep flowing.
- **Name vs index.** Uplink topics use the channel NAME; the downlink
  envelope's `channel` field uses the INDEX. Keep `MESH_CHANNEL` and
  `MESH_CHANNEL_INDEX` describing the same channel.
- **`from` must be the gateway's node number** in downlink envelopes; wrong
  values are dropped silently.
- **The `mqtt`-named channel requirement** for JSON downlink has shifted
  across firmware versions. If downlink stops working after an upgrade,
  re-run the step 2 `mosquitto_pub` test first.
- **No stat topic on 2.7.26** — the retained `msh/<root>/2/stat/<id>`
  liveness frame in the Meshtastic docs simply is not published. If a later
  firmware starts sending it, the Kuma monitor could go back to the MQTT
  check (the provisioner already understands `type: mqtt`) and the
  dashboard's gateway dot would come alive on its own; until then both lean
  on ping/traffic. If a stale retained frame ever appears after a re-flash,
  clear it: `mosquitto_pub -h 192.168.0.13 -r -n -t 'msh/US/2/stat/!<oldid>'`.
- **Unsynced clocks send `timestamp: 0`.** The dashboard substitutes its
  receive time, so ordering can be approximate right after a node boots.
- **LoRa airtime is shared.** The dashboard caps sends at 200 bytes and one
  per 2 s on purpose; do not "fix" that.
- **Wi‑Fi disables Bluetooth on ESP32.** Provisioning after step 1 is
  USB/CLI or the node's HTTP UI.
