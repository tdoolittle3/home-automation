# Home Automation — `ladybird`

Frigate NVR, Home Assistant, and supporting services on a single Debian box, with cameras on a
physically isolated network. This repo is the rebuild plan: every config file needed to reproduce
the deployment, minus secrets.

**Host:** Minisforum M1 Plus — i5-12600H (12C/16T), Iris Xe 80EU, 16GB DDR5, 512GB NVMe, dual 2.5GbE
**OS:** Debian 13 (Trixie), kernel 6.12 · **Hostname:** `ladybird` · **LAN:** 192.168.0.13

See [VERSIONS.txt](VERSIONS.txt) for exact image digests at time of capture.

---

## Architecture

Two physical networks. The cameras sit on their own segment with **no gateway**, so they cannot
reach the internet regardless of what their firmware wants to do. Firmware toggles (P2P, UPnP,
DDNS) are policy; the missing route is enforcement.

```
    Internet
        |
   [ Router ]  192.168.0.1
        |
        |  LAN 192.168.0.0/24
        |
   [ ladybird  192.168.0.13 ]           enp44s0  (LAN)
     Frigate · Home Assistant
     Mosquitto · Jellyfin · Immich      enp45s0  (camera island)
     Uptime Kuma · Tailscale            10.10.10.50/24 — no gateway
     Dashboard
        |
        |  10.10.10.0/24
   [ TL-SG105MPE PoE switch ]
        |               |
    driveway         backyard
   10.10.10.201     10.10.10.202
   IPC-T54PRO-ZE    IPC-T24IR-AS
```

The server straddles both networks. It is the cameras' only peer — it serves them NTP via chrony
and pulls their RTSP streams. Nothing on the LAN reaches them directly.

### Services

| Service | Address | Notes |
|---|---|---|
| Frigate UI | `https://192.168.0.13:8971` | authenticated |
| Frigate API | `http://192.168.0.13:5000` | unauthenticated — **LAN only, never port-forward** |
| Home Assistant | `http://192.168.0.13:8123` | host networking; **http, not https** |
| Jellyfin | `http://192.168.0.13:8096` | |
| Immich | `http://192.168.0.13:2283` | photo library; the first account created becomes **admin** |
| Uptime Kuma | `http://192.168.0.13:3001` | watchdog — see [docs/uptime-kuma.md](docs/uptime-kuma.md) |
| Dashboard | `http://ladybird/` · `http://192.168.0.13/` | port 80, so the bare hostname works; custom UI over the HA API — built from the `home-dashboard` repo |
| Mosquitto | `192.168.0.13:1883` | anonymous, LAN only |
| Samba | `//192.168.0.13/files` | serves `/srv/storage/files` |

Remote access is via **Tailscale** (subnet router advertising `192.168.0.0/24`). Do not
port-forward Frigate.

### Detection

Object detection runs on the Intel iGPU via OpenVINO (`ssdlite_mobilenet_v2`) at roughly 10 ms per
inference; Frigate decodes with VAAPI. The driveway camera feeds a 1280×720 detect stream (its
third stream, `subtype=2`) at 5 fps; the backyard camera feeds 704×480 at 5 fps. Both record a
2688×1520 main stream. The driveway streams go through go2rtc so the UI live view can show the
full 4MP main stream or the 720p sub stream.

### Retention

Tiered, so the expensive tier is short and the cheap tier is long:

| Tier | Window | Mode |
|---|---|---|
| Continuous | 2 days | everything |
| Detections | 7 days | motion only |
| Alerts | 14 days | motion only |
| Snapshots | 60 days (person 180, car 90) | JPEG, negligible size |

---

## Repo layout

```
stacks/          -> deploys to /opt/stacks on the server
  dash/          the custom dashboard (image built from the home-dashboard repo)
  frigate/       docker-compose.yml + config/config.yml
  home/          Home Assistant + Mosquitto
  media/         Jellyfin
  immich/        Immich photo library (its own stack)
  net/           Uptime Kuma + the storage and UPS guards (kuma-monitors.yml defines the monitor set)
host/etc/        -> deploys to /etc on the server
host/snippets/   fragments to append to existing system files
docs/            camera provisioning, operations runbook, Uptime Kuma setup
```

---

## Redeploy from scratch

Assumes a minimal Debian 13 install. Follow in order — later stages depend on earlier ones, and
each has a verification step. Do not skip the verifications; they are where problems surface
cheaply.

### 1. Base system

Replace `/etc/apt/sources.list` with [host/etc/apt/sources.list](host/etc/apt/sources.list) — it
includes `non-free`, which the Intel media driver requires.

```bash
apt update && apt install -y sudo openssh-server curl ca-certificates gnupg git vim htop \
  intel-gpu-tools vainfo unattended-upgrades chrony intel-media-va-driver-non-free firmware-misc-nonfree
systemctl enable --now ssh
usermod -aG sudo,render <user>
```

### 2. Verify the iGPU — this gates everything

```bash
ls -l /dev/dri/          # expect card0 and renderD128
vainfo                   # expect VAProfileH264*/HEVC* with VLD and EncSlice
```

If `renderD128` is missing the non-free firmware did not install. Stop and fix it — Frigate
detection and Jellyfin transcoding both depend on this.

### 3. Docker

```bash
curl -fsSL https://get.docker.com | sh
usermod -aG docker <user>
```

Copy [host/etc/docker/daemon.json](host/etc/docker/daemon.json) into place. **This is required** —
containers cannot resolve DNS on this host without it.

```bash
systemctl restart docker
docker run --rm --device /dev/dri:/dev/dri debian:trixie \
  sh -c "apt update -qq && apt install -y -qq vainfo && vainfo"    # must list profiles
```

### 4. Storage layout

A single root, so the Phase 2 drive swap is a remount rather than a reconfiguration.

```bash
mkdir -p /srv/storage/{frigate,media,photos,files,archive}
mkdir -p /opt/stacks/{frigate,media,immich,home,net,dash}
chown -R <user>:<user> /srv/storage /opt/stacks
```

### 5. Camera island and NTP

```bash
cp host/etc/network/interfaces.d/enp45s0 /etc/network/interfaces.d/   # static, NO gateway
systemctl restart networking
cat host/snippets/chrony-allow.conf >> /etc/chrony/chrony.conf
systemctl restart chrony
```

Verify isolation with `tcpdump -i enp45s0 -n "ip and not host 10.10.10.50"` — expect near
silence. Failed ARP for a nonexistent gateway is the healthy state.

### 6. Provision the cameras

See [docs/cameras.md](docs/cameras.md). Nearly all of it can be done over the HTTP API; only the
initial admin password needs a browser.

### 7. Deploy the stacks

```bash
cp -r stacks/* /opt/stacks/
printf 'FRIGATE_RTSP_PASSWORD=<camera admin password>\n' > /opt/stacks/frigate/.env
chmod 600 /opt/stacks/frigate/.env

# Immich needs a generated database password before its first start
cd /opt/stacks/immich && cp .env.example .env && chmod 600 .env
sed -i "s/^DB_PASSWORD=.*/DB_PASSWORD=$(openssl rand -hex 24)/" .env

for d in frigate home media immich net; do (cd /opt/stacks/$d && docker compose up -d); done
```

Immich pulls roughly 4 GB of images and needs a few minutes to report healthy on its first
start while Postgres initialises. Then open `http://<host>:2283` — **the first account created
becomes the admin**, so create it before telling anyone else the address.

Frigate prints a generated admin password on first boot — capture it from `docker logs frigate`.

### 8. Home Assistant

Onboard at `http://<host>:8123`, then install HACS:

```bash
docker exec homeassistant bash -c 'wget -q -O - https://get.hacs.xyz | bash -'
docker restart homeassistant
```

Add integrations in this order: **MQTT** (`192.168.0.13`, port 1883, no credentials) →
**HACS** (needs a GitHub device-code authorization) → install *Frigate* and *Dahua* from HACS →
**Frigate** (`http://192.168.0.13:5000`) → **Dahua** (one per camera).

Then recreate the credential file for white-light control:

```bash
printf 'user = "admin:<camera password>"\ndigest\n' > /opt/stacks/home/homeassistant/.camcreds
chmod 600 /opt/stacks/home/homeassistant/.camcreds
```

### 9. Storage guard

```bash
cp host/etc/systemd/system/disk-guard.* /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now disk-guard.timer
/opt/stacks/net/disk-guard.sh --discovery    # publishes MQTT discovery so HA creates the sensors
```

### 10. Remaining host services

```bash
# Tailscale
curl -fsSL https://tailscale.com/install.sh | sh
cp host/etc/sysctl.d/99-tailscale.conf /etc/sysctl.d/ && sysctl --system
tailscale up --advertise-routes=192.168.0.0/24 --accept-routes
# then approve the subnet route in the Tailscale admin console

# Samba
apt install -y samba
cat host/snippets/samba-files-share.conf >> /etc/samba/smb.conf
smbpasswd -a <user> && systemctl restart smbd

# NUT + UPS guard (full walkthrough: docs/operations.md -> UPS and power)
apt install -y nut
cp host/etc/nut/{nut.conf,ups.conf,upsd.conf,upsd.users,upsmon.conf} /etc/nut/
NUTPASS=$(head -c 16 /dev/urandom | base64 | tr -dc 'a-z0-9')
sed -i "s/__NUT_MON_PASSWORD__/$NUTPASS/" /etc/nut/upsd.users /etc/nut/upsmon.conf
chown root:nut /etc/nut/*.conf /etc/nut/upsd.users && chmod 640 /etc/nut/*.conf /etc/nut/upsd.users
systemctl restart nut-server nut-monitor && upsc cyberpower | grep ups.status   # expect OL
cp host/etc/systemd/system/ups-guard.* /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now ups-guard.timer
/opt/stacks/net/ups-guard.sh --discovery
```

### 11. Dashboard

The dashboard is a separate application — see the `home-dashboard` repo. It has no registry image
and no git remote, so the source is copied to the host and built there. The checkout is only a
build input: nothing runs from it, and the host has no Node installed at all.

```bash
# from a workstation with the home-dashboard repo checked out
git archive --format=tar HEAD > /tmp/hd.tar
scp /tmp/hd.tar <user>@192.168.0.13:/tmp/
```

```bash
# on the server
mkdir -p ~/src/home-dashboard && tar -xf /tmp/hd.tar -C ~/src/home-dashboard
cp ~/src/home-dashboard/.env.example /opt/stacks/dash/.env
chmod 600 /opt/stacks/dash/.env
```

The compose file already sets the service URLs, so `.env` only needs the tokens. `HA_TOKEN` comes
from Home Assistant → your profile → Security → **Long-lived access tokens**. That token is full
control of HA — it stays in the container and never reaches a browser.

```bash
cd /opt/stacks/dash && docker compose up -d --build
curl -s http://127.0.0.1/api/health           # expect ha.connected true
```

**The dashboard has no login of its own.** Anyone who can reach port 80 can read every panel and
toggle whatever its `controls` panels list. Keep it on the LAN, reach it over Tailscale, and never
port-forward it.

### 12. Uptime Kuma

The container comes up with the `net` stack in step 7, but an unconfigured Kuma watches nothing.
Apply [stacks/net/kuma-monitors.yml](stacks/net/kuma-monitors.yml) by hand in the UI —
Kuma 1.x stores its configuration in a gitignored SQLite database, so there is no import to run.
Full walkthrough in [docs/uptime-kuma.md](docs/uptime-kuma.md); the load-bearing part is the push
monitor that the storage guard beats only while healthy, so that a dead server alerts by silence.

---

### 12. Crash resilience

Added after the 2026-09-06 kernel panic (see `docs/operations.md` → "After a crash").

```bash
cp host/etc/sysctl.d/99-panic-reboot.conf /etc/sysctl.d/ && sysctl --system   # reboot 10 s after a panic
cp host/etc/default/grub.d/pcie-aspm.cfg /etc/default/grub.d/ && update-grub   # pcie_aspm=off, needs a reboot
apt install -y rasdaemon && systemctl enable --now rasdaemon                 # decode + log machine checks
```

Also set **"Power on after AC loss"** in the BIOS (not scriptable) and add a router DHCP
reservation for `38:05:25:35:71:69` so the box comes back at `192.168.0.13` after any outage.

## Secrets — not in this repo

Recreate these by hand on a fresh deploy. Nothing here is recoverable from this repo by design.

| File | Contents |
|---|---|
| `.env` (this repo) | `SSH_PW`, `CAMERA_PW`, `NTFY_TOPIC` — see [.env.example](.env.example) |
| `/opt/stacks/net/kuma-push-url.txt` | the storage guard's Kuma push URL, mode 600 — regenerated per Kuma rebuild |
| `/opt/stacks/net/kuma-push-url-ups.txt` | the UPS guard's Kuma push URL, mode 600 — regenerated per Kuma rebuild |
| `/etc/nut/upsd.users`, `/etc/nut/upsmon.conf` | the NUT monitor password — machine-local, generated at install, never stored elsewhere |
| Uptime Kuma admin account | created on first visit to port 3001; store it in your password manager |
| `/opt/stacks/frigate/.env` | `FRIGATE_RTSP_PASSWORD=` the camera admin password, mode 600 |
| `/opt/stacks/home/homeassistant/.camcreds` | curl digest config for camera control, mode 600 |
| `/opt/stacks/dash/.env` | `HA_TOKEN` (a long-lived access token) and `JELLYFIN_API_KEY`, mode 600 |
| `/opt/stacks/immich/.env` | `DB_PASSWORD=` a generated random string, mode 600 — see [.env.example](stacks/immich/.env.example) |
| HA `.storage/`, `secrets.yaml` | integration configs and tokens — recreated by re-adding integrations |
| Frigate `.jwt_secret`, `*.db` | regenerated automatically on first start |

Camera admin passwords exist only on the cameras themselves and in `.env`.

---

## Hard-won gotchas

- **A frozen box with LEDs on is a kernel panic, not a network fault.** 2026-09-06: the camera-side
  I226-V NIC (`igc`, enp45s0) took a transmit-queue watchdog reset, and 7 s later the kernel panicked on
  an MCE broadcast timeout (`Not all CPUs entered broadcast exception handler`). With the stock
  `kernel.panic=0` it hung until power-cycled. The crash record survives in EFI pstore and is copied to
  `/var/lib/systemd/pstore/<epoch>/` at the next boot — read it before guessing.
Each of these cost real debugging time. Read before changing anything.

- **Frigate 0.17 needs an explicit global `model:` block** pointing at
  `/openvino-model/ssdlite_mobilenet_v2.xml`. Without it the detector dies at startup with
  `TypeError: stat: path should be string... not NoneType`, and the UI shows a camera with no
  detection rather than an obvious error.
- **Frigate rewrites `config.yml` and appends a top-level `version:` key at the end.** Never
  append a camera block to the end of the file — it lands outside the `cameras:` map and Frigate
  refuses to start with `yaml: mapping values are not allowed in this context`. Insert *before*
  the `version:` line.
- **Containers cannot resolve DNS without `daemon.json`.** Symptom is every `apt` inside a
  container failing with `Temporary failure resolving`.
- **A camera reports saved encoder settings back correctly while still streaming the old codec.**
  H.265→H.264 does not take effect until the camera is rebooted. Verify with `ffprobe`, never by
  trusting the camera's web UI.
- **The Dahua integration's `light.*_illuminator` drives the infrared emitter**, not the white
  light, on these models. The white light lives at `Lighting_V2[0][0][1]` — see
  [docs/cameras.md](docs/cameras.md). This is not a permissions problem, and changing camera
  accounts does not fix it.
- **Cameras require digest auth and reject basic auth**, so HA's `rest_command` cannot drive them.
  Use `command_line` with `curl`.
- **`curl` needs `-g`** for Dahua URLs, or it interprets `[0][0]` as glob ranges.
- **`docker exec -i` inside a `while read` loop consumes the caller's heredoc**, silently
  truncating the loop. Drop `-i` and redirect `</dev/null`.
- **Recordings are owned by root** (the container writes as root), so removing them from the host
  needs sudo.
- **Sub stream 1 (`subtype=1`) caps at 704×480 (D1) on both cameras**, but the T54PRO-ZE's
  sub stream 2 (`subtype=2`) goes to 720p/1080p. Frigate's `detect` block must match whichever
  stream it reads, or detection silently runs on a mismatched frame size.
- **The camera web UI can report "saved" without writing anything.** Enabling sub stream 2 from
  the UI returned success and changed nothing; the API call worked first time. Always read the
  config back over the API after a UI change.

---

## Known gaps

- **No hard storage cap.** Frigate has no GB quota — retention is days-only, and its internal
  `StorageMaintainer` only intervenes at under one hour of free space, which is too late to rely
  on. The storage guard alerts but deliberately never deletes. A real cap means a separate
  filesystem for `/srv/storage`, which belongs with the Phase 2 drive.
- **Immich has no automated backup.** The library is irreplaceable in a way recordings are not, and a copy of the Postgres directory does not count — it needs a logical dump. The procedure is in [docs/operations.md](docs/operations.md#immich); it is not yet on a timer.
- ~~UPS monitoring (NUT) not configured~~ Done 2026-09-08: the rack's CyberPower PR1500LCDRT2U is
  on USB, NUT + `ups-guard.timer` report to the "UPS power" Kuma monitor, and `upsmon` halts the
  box cleanly at low battery. See `docs/operations.md` → UPS and power.
- **LAN address is DHCP.** Set a router reservation for MAC `38:05:25:35:71:69`.
- **Uptime Kuma push monitor needs a file that is not in this repo.** The storage guard beats the
  "Storage guard" push monitor only if `/opt/stacks/net/kuma-push-url.txt` exists (see
  `docs/operations.md` → Storage). It holds the monitor's push token, so it is gitignored and must
  be recreated on a rebuild — until then that monitor, and its "Host and network" group, show down.
- **Uptime Kuma's config is not backed up.** Monitors are live, applied from
  [stacks/net/kuma-monitors.yml](stacks/net/kuma-monitors.yml) by
  [kuma-provision.py](stacks/net/kuma-provision.py), but Kuma 1.x keeps them in a gitignored
  SQLite database — so a wipe means re-running the script and reissuing the storage guard's push
  token, which does not survive. See [docs/uptime-kuma.md](docs/uptime-kuma.md).
- **DST end rule** on both cameras reads `Day=2` where Sunday would be `0`. Verify camera clocks
  in early November 2026.

---

## Operations

Day-to-day commands, health checks, and camera access are in
[docs/operations.md](docs/operations.md).
