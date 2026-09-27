# Meshtastic — LoRa mesh bridged to the LAN

An ESP32 Meshtastic node on the house Wi‑Fi acts as the **gateway** between
the LoRa mesh and the Mosquitto broker in the `home` stack. It carries one
private channel; the dashboard renders the channel's messages and node
telemetry, and can send onto it. There is **no public-mesh uplink**:
`mqtt.address` points at `192.168.0.13`, never `mqtt.meshtastic.org`, and
remote access is Tailscale → dashboard like everything else here.

No new stack — the broker already exists (`stacks/home/mosquitto/`,
anonymous, LAN-only) and the panel lives in the
[home-dashboard](https://github.com/tdoolittle3/home-dashboard) repo. What
this repo carries is the node runbook, the dashboard's mesh environment in
`stacks/dash/docker-compose.yml`, and the Kuma monitor.

| What | Where |
|---|---|
| Uplink (mesh → broker), decoded JSON | `msh/US/2/json/homemesh/!<gatewayid>` |
| Downlink (broker → mesh), JSON envelope | `msh/US/2/json/mqtt/` |
| Gateway liveness (retained, LWT) | `msh/US/2/stat/!<gatewayid>` |
| Protobuf twin of the uplink | `msh/US/2/e/homemesh/!<gatewayid>` |
| Dashboard panel | `Mesh` on http://192.168.0.13/ |

`homemesh` is the private channel's name. Topic scheme per the
[Meshtastic MQTT docs](https://meshtastic.org/docs/software/integrations/mqtt/).

---

## 1. Provision the node — USB, once

Needs the Python CLI (`pip install meshtastic`) and the node on USB. Order
matters: the node reboots after each config write, and the channel URL
changes when the PSK does, so share the QR only after step 2.

```bash
# region first; the radio will not transmit without one
meshtastic --set lora.region US

# the private channel: rename the primary, fresh 256-bit PSK, MQTT both ways.
# The name lands in a topic path - lowercase, no spaces.
meshtastic --ch-set name homemesh --ch-index 0
meshtastic --ch-set psk random --ch-index 0
meshtastic --ch-set uplink_enabled true --ch-set downlink_enabled true --ch-index 0

# JSON-downlink control channel: the firmware only listens on .../json/mqtt/
# when a channel literally named "mqtt" has downlink enabled
meshtastic --ch-add mqtt
meshtastic --ch-set downlink_enabled true --ch-index 1

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
# everything, including the retained "online" on the stat topic - should
# print immediately on connect; nodeinfo/telemetry JSON follows within minutes
mosquitto_sub -h 192.168.0.13 -t 'msh/#' -v

# just the private channel's decoded JSON
mosquitto_sub -h 192.168.0.13 -t 'msh/US/2/json/homemesh/+' -v
```

Send a text from the phone app on `homemesh` — it appears as `"type":"text"`
JSON. Then prove the downlink path (this is the step that validates the
`mqtt`-named channel requirement — note the firmware version you tested):

```bash
mosquitto_pub -h 192.168.0.13 -t 'msh/US/2/json/mqtt/' \
  -m '{"from": <myNodeNum>, "type": "sendtext", "payload": "hello from broker", "channel": 0}'
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
subscribes to the retained stat topic and expects `online`. Fill in the
gateway id, then provision as in [uptime-kuma.md](uptime-kuma.md).

---

## Security posture — read before "improving" it

**The JSON topics are plaintext on the broker, always.** Meshtastic never
encrypts the JSON plane; `mqtt.encryption_enabled` only governs the protobuf
`2/e/` topics. That is accepted here because the broker is LAN-only (never
port-forwarded), remote access is Tailscale → dashboard only, and the RF
side stays PSK-encrypted end to end.

The corollary: **anyone on the LAN can publish a downlink** and transmit on
the mesh. That is the same trust boundary as the rest of the anonymous
broker — Frigate events and the guards live under it too. If the LAN stops
being trusted, the fix is Mosquitto auth for everything, not a special case
here.

The gateway only JSON-serializes packets on channels **whose key it holds**.
Today that is `homemesh` (it owns the channel). A second channel added later
will never appear as JSON until its key is loaded onto the gateway node.

---

## Gotchas

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
- **Stale retained stat.** After re-flashing or renumbering the node, the old
  id's retained `online`/`offline` lingers. Clear it:
  `mosquitto_pub -h 192.168.0.13 -r -n -t 'msh/US/2/stat/!<oldid>'`.
  The LWT also lags ~30 s, so "online" trails reality slightly.
- **Unsynced clocks send `timestamp: 0`.** The dashboard substitutes its
  receive time, so ordering can be approximate right after a node boots.
- **LoRa airtime is shared.** The dashboard caps sends at 200 bytes and one
  per 2 s on purpose; do not "fix" that.
- **Wi‑Fi disables Bluetooth on ESP32.** Provisioning after step 1 is
  USB/CLI or the node's HTTP UI.
