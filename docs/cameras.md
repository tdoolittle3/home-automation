# Camera provisioning

Both cameras are Dahua-family (Amcrest OEM) and expose the same HTTP CGI API. **Almost all
configuration can be done over that API from the server** — far faster and more repeatable than
clicking through the web UI. Only the initial admin-password setup needs a browser.

## Inventory

| Frigate name | Model | IP | Notes |
|---|---|---|---|
| `driveway` | IPC-T54PRO-ZE | 10.10.10.201 | has a controllable white light |
| `backyard` | IPC-T24IR-AS-2.8mm-S2 | 10.10.10.202 | IR; white light not verified |

Both use the **admin** account. The same password is `CAMERA_PW` in `.env` and
`FRIGATE_RTSP_PASSWORD` in `/opt/stacks/frigate/.env`.

Sub stream 1 (`ExtraFormat[0]`, RTSP `subtype=1`) caps at **704×480 (D1)** on both cameras — the
only other options are VGA and CIF. The T54PRO-ZE also has a **sub stream 2** (`ExtraFormat[1]`,
RTSP `subtype=2`) that supports 1080P and 720P; the driveway camera runs it at 1280×720 for
Frigate detection. Check what a camera offers with `encode.cgi?action=getConfigCaps&channel=1`.
Frigate's `detect` block must match the stream it reads.

---

## Reaching a camera's web UI

The camera island has no route to the LAN, so cameras are not reachable from a workstation
directly. Tunnel through the server:

```bash
ssh -L 8082:10.10.10.201:80 -N thomas@192.168.0.13
```

Then browse to `http://localhost:8082`. Use `-L 8082:10.10.10.202:80` for the backyard camera.

A **factory-default** camera sits at `192.168.1.108`, which is on neither network. To reach it,
temporarily add a bridge address to the camera NIC on the server:

```bash
sudo ip addr add 192.168.1.60/24 dev enp45s0
# ... provision the camera, moving it to 10.10.10.20x last ...
sudo ip addr del 192.168.1.60/24 dev enp45s0
```

---

## Provisioning a new camera

### Step 1 — initialize (browser, unavoidable)

Tunnel to `192.168.1.108` as above and set the **admin password** to the `CAMERA_PW` value.
Decline P2P/Easy4IP and auto-update while you are there. Stop; do nothing else in the UI.

### Step 2 — everything else (API)

Set a helper, then apply. `-g` is **required** — without it curl treats `[0][0]` as glob ranges.

```bash
CAM=192.168.1.108
PW='<camera admin password>'
S() { curl -s -g --digest -u "admin:$PW" "http://$CAM/cgi-bin/configManager.cgi?action=setConfig&$1"; echo; }
```

**Main stream** — 2688×1520 15 fps, VBR quality 5 capped at 6144 kbps, 1-second GOP. The
driveway camera uses H.265 (about half the storage of H.264 at this quality); note that Firefox
cannot play H.265 in the Frigate UI, while Chrome, Edge, and Safari can. Use H.264 if that matters:

```bash
S "Encode[0].MainFormat[0].Video.Compression=H.265"
S "Encode[0].MainFormat[0].Video.Width=2688&Encode[0].MainFormat[0].Video.Height=1520"
S "Encode[0].MainFormat[0].Video.FPS=15&Encode[0].MainFormat[0].Video.GOP=15"
S "Encode[0].MainFormat[0].Video.BitRateControl=VBR&Encode[0].MainFormat[0].Video.Quality=5&Encode[0].MainFormat[0].Video.BitRate=6144"
```

**Sub stream 1** — 704×480 H.264 5 fps CBR 512 kbps. The backyard camera runs detection on this:

```bash
S "Encode[0].ExtraFormat[0].Video.Compression=H.264"
S "Encode[0].ExtraFormat[0].Video.Width=704&Encode[0].ExtraFormat[0].Video.Height=480"
S "Encode[0].ExtraFormat[0].Video.FPS=5&Encode[0].ExtraFormat[0].Video.GOP=5"
S "Encode[0].ExtraFormat[0].Video.BitRateControl=CBR&Encode[0].ExtraFormat[0].Video.BitRate=512"
```

**Sub stream 2** (T54PRO-ZE only) — 1280×720 H.264 5 fps CBR 1024 kbps, disabled from the
factory. The driveway camera runs detection on this. It starts streaming as soon as it is enabled,
no reboot needed. Do this over the API: the web UI reported success for the same change and wrote
nothing.

```bash
S "Encode[0].ExtraFormat[1].VideoEnable=true"
S "Encode[0].ExtraFormat[1].Video.Compression=H.264"
S "Encode[0].ExtraFormat[1].Video.Width=1280&Encode[0].ExtraFormat[1].Video.Height=720"
S "Encode[0].ExtraFormat[1].Video.FPS=5&Encode[0].ExtraFormat[1].Video.GOP=5"
S "Encode[0].ExtraFormat[1].Video.BitRateControl=CBR&Encode[0].ExtraFormat[1].Video.BitRate=1024"
```

**Time** — the server is the only NTP source these cameras can reach. TimeZone 27 is Mountain:

```bash
S "NTP.Enable=true&NTP.Address=10.10.10.50&NTP.Port=123&NTP.UpdatePeriod=10&NTP.TimeZone=27"
S "Locales.DSTEnable=true"
S "Locales.DSTStart.Month=3&Locales.DSTStart.Week=2&Locales.DSTStart.Day=0&Locales.DSTStart.Hour=0&Locales.DSTStart.Minute=0"
S "Locales.DSTEnd.Month=11&Locales.DSTEnd.Week=1&Locales.DSTEnd.Day=2&Locales.DSTEnd.Hour=0&Locales.DSTEnd.Minute=0"
```

> Getting DST right *matters*: without it the camera runs exactly one hour behind for most of the
> year and Frigate's timestamps go strange. Note `DSTEnd.Day=2` — Sunday would be `0`, so verify
> the clocks in early November.

**Silence the phone-home features.** These are policy; the missing gateway is the real
enforcement, but there is no reason to leave them on:

```bash
S "T2UServer.Enable=false"     # P2P / Easy4IP
S "Email.Enable=false"         # often ON by default
S "DDNS[0].Enable=false"
S "Multicast.RTP[0].Enable=false&Multicast.RTP[1].Enable=false"
S "VideoStandard=NTSC"
```

**Network — always last**, since it cuts your connection:

```bash
S "Network.eth0.IPAddress=10.10.10.202&Network.eth0.SubnetMask=255.255.255.0&Network.eth0.DefaultGateway=10.10.10.1&Network.eth0.DhcpEnable=false&Network.eth0.DnsServers[0]=10.10.10.50&Network.eth0.DnsServers[1]=10.10.10.50"
```

The gateway `10.10.10.1` does not exist and never will — the camera ARPs for it, gets silence,
and its packets die on the wire. Some firmware refuses to accept a blank or `0.0.0.0` gateway,
which is why a deliberately nonexistent address is used instead.

### Step 3 — reboot, then verify with ffprobe

```bash
curl -s -g --digest -u "admin:$PW" "http://10.10.10.202/cgi-bin/magicBox.cgi?action=reboot"
```

**The reboot is not optional.** A camera will report the new codec back through its API and web UI
while still streaming the old one. Always confirm from outside:

```bash
docker exec frigate sh -c 'FF=$(ls /usr/lib/ffmpeg/*/bin/ffprobe|head -1); \
  "$FF" -v error -rtsp_transport tcp -show_entries stream=codec_name,width,height,avg_frame_rate \
  -of default=nw=1 "rtsp://admin:${FRIGATE_RTSP_PASSWORD}@10.10.10.202:554/cam/realmonitor?channel=1&subtype=1"'
```

### Step 4 — add to Frigate

Insert into `config/config.yml` **inside the `cameras:` map and before the trailing `version:`
key** (see the gotcha in the README):

```yaml
  backyard:
    ffmpeg:
      inputs:
        - path: rtsp://admin:{FRIGATE_RTSP_PASSWORD}@10.10.10.202:554/cam/realmonitor?channel=1&subtype=1
          roles: [detect]
        - path: rtsp://admin:{FRIGATE_RTSP_PASSWORD}@10.10.10.202:554/cam/realmonitor?channel=1&subtype=0
          roles: [record]
    detect:
      width: 704
      height: 480
      fps: 5
```

RTSP paths are `subtype=0` for the main stream, `subtype=1` for sub stream 1, and `subtype=2` for
sub stream 2.

The driveway camera is wired differently: both of its streams are declared under a top-level
`go2rtc:` block and Frigate reads them from `rtsp://127.0.0.1:8554/driveway` (main) and
`rtsp://127.0.0.1:8554/driveway_sub` (720p) with `input_args: preset-rtsp-restream`. The camera
then serves each stream once, and the `live: streams:` block lets the Frigate UI switch between the
4MP main stream and the 720p sub stream instead of showing the detect stream.

---

## White-light control

**The Home Assistant Dahua integration cannot control the white light on these models.** Its
`light.<name>_illuminator` entity writes the V1 `Lighting[0][0]` table, which on this hardware is
the *infrared* emitter. Toggling it appears to do nothing because it is commanding the wrong LEDs.

This is **not** a permissions problem. The limited `frigate` account and the `admin` account were
measured to have identical API access; swapping accounts changes nothing.

The white light lives in the V2 table. Confirm which index it is:

```bash
curl -s -g --digest -u "admin:$PW" \
  "http://10.10.10.201/cgi-bin/configManager.cgi?action=getConfig&name=Lighting_V2" | grep LightType
# table.Lighting_V2[0][0][0].LightType=InfraredLight
# table.Lighting_V2[0][0][1].LightType=WhiteLight     <- index 1
```

Control it directly:

```bash
# on
.../configManager.cgi?action=setConfig&Lighting_V2[0][0][1].Mode=Manual&Lighting_V2[0][0][1].NearLight[0].Light=100
# off
.../configManager.cgi?action=setConfig&Lighting_V2[0][0][1].Mode=Off
```

This is wired into Home Assistant as `switch.frontcam_white_light` via a `command_line` switch in
[configuration.yaml](../stacks/home/homeassistant/configuration.yaml). It uses `command_line`
rather than `rest_command` because **the cameras reject basic auth and require digest**, which
`rest_command` cannot do. Credentials live in `/config/.camcreds` (mode 600) so they stay out of
YAML.

**Verifying it physically:** the API returns `OK` whether or not anything illuminates. Measure
average frame luminance instead:

```bash
curl -s -o /tmp/f.jpg "http://127.0.0.1:5000/api/driveway/latest.jpg"
docker cp /tmp/f.jpg frigate:/tmp/m.jpg
docker exec frigate sh -c 'FP=$(ls /usr/lib/ffmpeg/*/bin/ffprobe|head -1); \
  "$FP" -v error -f lavfi -i "movie=/tmp/m.jpg,signalstats" \
  -show_entries frame_tags=lavfi.signalstats.YAVG -of csv=p=0 | head -1'
```

Toggle the light between samples. A clean swing that returns to baseline proves the light responds
— indoors under room lighting expect a modest change (~150 → ~155), outdoors at night it is
dramatic.

---

## Useful config table names

Discovered by querying `configManager.cgi?action=getConfig&name=<Table>`:

| Table | Holds |
|---|---|
| `Encode` | stream resolution, codec, fps, bitrate, GOP, audio |
| `NTP` | server address, port, timezone index, update period |
| `Locales` | DST enable and start/end rules |
| `Network` | `eth0` address, mask, gateway, DNS, DHCP flag |
| `T2UServer` | P2P / Easy4IP cloud |
| `DDNS`, `Email`, `Multicast` | phone-home services |
| `VideoStandard` | NTSC / PAL |
| `Lighting`, `Lighting_V2` | IR and white light |
| `AutoMaintain` | scheduled reboot |

`UPnP` returns `Bad Request` on these models — there is no such table.

Other useful endpoints:

```
/cgi-bin/magicBox.cgi?action=getDeviceType        # model
/cgi-bin/magicBox.cgi?action=getSoftwareVersion   # firmware
/cgi-bin/magicBox.cgi?action=reboot
/cgi-bin/global.cgi?action=getCurrentTime         # clock check
/cgi-bin/encode.cgi?action=getConfigCaps&channel=1  # valid resolutions and codecs
```

---

## Finding a camera you cannot locate

Every device on the island shows up in a capture, even without an IP:

```bash
sudo tcpdump -i enp45s0 -n -e | awk '{print $2}' | sort -u        # MAC census
sudo tcpdump -i enp45s0 -n -vv "port 67 or port 68"               # DHCP requests
```

DHCP requests carry the device's **hostname and vendor class**, which identifies hardware without
touching it. That is how the mystery device broadcasting on this segment was identified as the
`TL-SG105MPE` switch's own management interface rather than a camera.
