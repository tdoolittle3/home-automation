# Rebuilding ladybird from scratch

Everything needed to take a blank Debian 13 install back to the running system described in the
[README](../README.md). Follow the steps in order — later ones depend on earlier ones, and each has
a verification step. **Do not skip the verifications**; they are where problems surface cheaply.

If you only want to understand how the finished system fits together, read
[architecture.md](architecture.md) instead. If something is behaving oddly, check
[gotchas.md](gotchas.md) before changing anything.

---

## 1. Base system

Replace `/etc/apt/sources.list` with [host/etc/apt/sources.list](../host/etc/apt/sources.list) — it
includes `non-free`, which the Intel media driver requires.

```bash
apt update && apt install -y sudo openssh-server curl ca-certificates gnupg git vim htop \
  intel-gpu-tools vainfo unattended-upgrades chrony intel-media-va-driver-non-free firmware-misc-nonfree
systemctl enable --now ssh
usermod -aG sudo,render <user>
```

## 2. Verify the iGPU — this gates everything

```bash
ls -l /dev/dri/          # expect card0 and renderD128
vainfo                   # expect VAProfileH264*/HEVC* with VLD and EncSlice
```

If `renderD128` is missing, the non-free firmware did not install. Stop and fix it — Frigate
detection and Jellyfin transcoding both depend on this.

## 3. Docker

```bash
curl -fsSL https://get.docker.com | sh
usermod -aG docker <user>
```

Copy [host/etc/docker/daemon.json](../host/etc/docker/daemon.json) into place. **This is required**
— containers cannot resolve DNS on this host without it.

```bash
systemctl restart docker
docker run --rm --device /dev/dri:/dev/dri debian:trixie \
  sh -c "apt update -qq && apt install -y -qq vainfo && vainfo"    # must list profiles
```

## 4. Storage layout

A single root, so the Phase 2 drive swap is a remount rather than a reconfiguration.

```bash
mkdir -p /srv/storage/{frigate,media,photos,files,archive}
mkdir -p /opt/stacks/{frigate,media,immich,home,net,n8n,sdr,dns,dash}
chown -R <user>:<user> /srv/storage /opt/stacks
```

## 5. Camera island and NTP

```bash
cp host/etc/network/interfaces.d/enp45s0 /etc/network/interfaces.d/   # static, NO gateway
systemctl restart networking
cat host/snippets/chrony-allow.conf >> /etc/chrony/chrony.conf
systemctl restart chrony
```

Verify isolation with `tcpdump -i enp45s0 -n "ip and not host 10.10.10.50"` — expect near silence.
Failed ARP for a nonexistent gateway is the healthy state.

## 6. Provision the cameras

See [cameras.md](cameras.md). Nearly all of it can be done over the HTTP API; only the initial admin
password needs a browser.

## 7. Deploy the stacks

```bash
cp -r stacks/* /opt/stacks/
printf 'FRIGATE_RTSP_PASSWORD=<camera admin password>\n' > /opt/stacks/frigate/.env
chmod 600 /opt/stacks/frigate/.env

# Immich needs a generated database password before its first start
cd /opt/stacks/immich && cp .env.example .env && chmod 600 .env
sed -i "s/^DB_PASSWORD=.*/DB_PASSWORD=$(openssl rand -hex 24)/" .env

# n8n runs as UID 1000 inside the container and needs its data dir owned by it
mkdir -p /opt/stacks/n8n/n8n-data && chown 1000:1000 /opt/stacks/n8n/n8n-data

for d in frigate home media immich net n8n; do (cd /opt/stacks/$d && docker compose up -d); done
```

Three things only happen on a first start:

- **Immich** pulls roughly 4 GB of images and takes a few minutes to report healthy while Postgres
  initialises. Then open `http://<host>:2283` — **the first account created becomes the admin**, so
  create it before telling anyone else the address.
- **Frigate** prints a generated admin password on first boot — capture it from `docker logs frigate`.
- **n8n**'s first visit at `http://<host>:5678` creates the owner account. Import
  [stacks/n8n/workflows/frigate-person-llm-notify.json](../stacks/n8n/workflows/frigate-person-llm-notify.json)
  through the UI and recreate its MQTT and HA-token credentials by hand — credentials and the
  encryption key live in n8n's SQLite database under `n8n-data/`, which is gitignored (see the
  backups table in [operations.md](operations.md#backups)).

The `sdr` and `dns` stacks are deliberately **not** in that loop. SDR needs host preparation (DVB-T
driver blacklist plus a udev rule, via `sudo bash host/scripts/sdr-host-prep.sh`) and a real antenna
latitude/longitude/altitude in `/opt/stacks/sdr/.env` before it can start — full walkthrough in
[sdr.md](sdr.md). DNS is step 14, last, because it is the one stack that can take the whole house
offline rather than one service.

## 8. Home Assistant

Onboard at `http://<host>:8123`, then install HACS:

```bash
docker exec homeassistant bash -c 'wget -q -O - https://get.hacs.xyz | bash -'
docker restart homeassistant
```

Add integrations in this order: **MQTT** (`192.168.0.13`, port 1883, no credentials) → **HACS**
(needs a GitHub device-code authorization) → install *Frigate* and *Dahua* from HACS → **Frigate**
(`http://192.168.0.13:5000`) → **Dahua** (one per camera).

Then add **System Monitor** — the dashboard's System panel reads its sensors. Nearly all of its
entities are disabled by default; on the System Monitor device page enable *Processor use*, *Memory
usage*, *Swap usage*, *Processor temperature*, *Load (1 min)*, and *Network throughput in/out
enp44s0*, then rename `sensor.system_monitor_uptime` to `sensor.system_monitor_last_boot` (the
entity ID the dashboard's config expects; there is no `last_boot` sensor in current HA).

Then recreate the credential file for white-light control:

```bash
printf 'user = "admin:<camera password>"\ndigest\n' > /opt/stacks/home/homeassistant/.camcreds
chmod 600 /opt/stacks/home/homeassistant/.camcreds
```

## 9. Storage guard

```bash
cp host/etc/systemd/system/disk-guard.* /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now disk-guard.timer
/opt/stacks/net/disk-guard.sh --discovery    # publishes MQTT discovery so HA creates the sensors
```

`disk-guard.sh` has to be owned by root; systemd refuses to run it otherwise.

## 10. Remaining host services

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

# IPTV playlist for Jellyfin Live TV - daily rebuild of /srv/storage/media/iptv/fast.m3u8
# (the .service runs as user thomas; edit it if the username differs)
cp host/etc/systemd/system/iptv-playlist.* /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now iptv-playlist.timer
sudo -u <user> /opt/stacks/net/iptv-playlist.py      # first build now, not tomorrow

# NUT + UPS guard (full walkthrough: operations.md -> UPS and power)
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

Jellyfin's Live TV tuner and its XMLTV listing (`http://192.168.0.13:3000/guide.xml`, served by the
`epg` container) live in `config/livetv.xml`, which is runtime state and not in this repo — recreate
the M3U tuner pointing at `/media/iptv/fast.m3u8` (the container path), then trigger **Dashboard →
Scheduled Tasks → Refresh Guide** by hand. Channels never appear on their own: the refresh does not
run on a timer here, and zero programmes is not evidence the config is wrong.

## 11. Dashboard

The dashboard is a separate application at
[github.com/tdoolittle3/home-dashboard](https://github.com/tdoolittle3/home-dashboard). It has no
registry image, so the source is cloned to the host and built there. The checkout is only a build
input: nothing runs from it, and the host has no Node installed at all.

```bash
# on the server
git clone https://github.com/tdoolittle3/home-dashboard ~/src/home-dashboard
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

## 12. Uptime Kuma

The container comes up with the `net` stack in step 7, but an unconfigured Kuma watches nothing. The
monitor set lives in [stacks/net/kuma-monitors.yml](../stacks/net/kuma-monitors.yml) and is applied
by [kuma-provision.py](../stacks/net/kuma-provision.py) — Kuma 1.x keeps its configuration in a
gitignored SQLite database, so there is no import file to hand it. Full walkthrough in
[uptime-kuma.md](uptime-kuma.md).

The load-bearing part is the push monitor that the storage guard beats only while healthy, so that a
dead server alerts by silence instead of by nobody noticing.

## 13. Crash resilience

Added after the 2026-09-06 kernel panic (see [operations.md](operations.md) → "After a crash").

```bash
cp host/etc/sysctl.d/99-panic-reboot.conf /etc/sysctl.d/ && sysctl --system   # reboot 10 s after a panic
cp host/etc/default/grub.d/pcie-aspm.cfg /etc/default/grub.d/ && update-grub   # pcie_aspm=off, needs a reboot
apt install -y rasdaemon && systemctl enable --now rasdaemon                  # decode + log machine checks
```

Also set **"Power on after AC loss"** in the BIOS (not scriptable) and add a router DHCP reservation
for MAC `38:05:25:35:71:69` so the box comes back at `192.168.0.13` after any outage. That
reservation is not optional any more: the LAN's DNS answers on this address, and a changed address
takes the whole house's name resolution with it.

## 14. DNS — AdGuard Home

Last, because the cutover is the one step that can take the whole house offline rather than one
service. Full runbook, migration and rollback: **[dns.md](dns.md)** — read it first.

```bash
systemctl is-active systemd-resolved   # must be inactive, or nothing can bind port 53
cd /opt/stacks/dns
mkdir -p conf work && cp AdGuardHome.yaml conf/AdGuardHome.yaml
# insert the admin password hash - dns.md -> First deploy
docker compose up -d
docker exec adguardhome nslookup doubleclick.net 127.0.0.1   # expect 0.0.0.0
```

Only then point the router's DHCP DNS at `192.168.0.13`, and set container DNS to the same address
in [host/etc/docker/daemon.json](../host/etc/docker/daemon.json). Leave the Pi-hole at
`192.168.0.14` powered — it is the rollback.

---

## Secrets — not in this repo

Recreate these by hand on a fresh deploy. Nothing here is recoverable from this repo, by design.

| File | Contents |
|---|---|
| `.env` (repo root) | `SSH_PW`, `CAMERA_PW`, `NTFY_TOPIC` — see [.env.example](../.env.example) |
| `/opt/stacks/net/kuma-push-url.txt` | the storage guard's Kuma push URL, mode 600 — regenerated per Kuma rebuild |
| `/opt/stacks/net/kuma-push-url-ups.txt` | the UPS guard's Kuma push URL, mode 600 — regenerated per Kuma rebuild |
| `/etc/nut/upsd.users`, `/etc/nut/upsmon.conf` | the NUT monitor password — machine-local, generated at install, never stored elsewhere |
| Uptime Kuma admin account | created on first visit to port 3001; store it in your password manager |
| `/opt/stacks/frigate/.env` | `FRIGATE_RTSP_PASSWORD=` the camera admin password, mode 600 |
| `/opt/stacks/home/homeassistant/.camcreds` | curl digest config for camera control, mode 600 |
| `/opt/stacks/dash/.env` | `HA_TOKEN` (a long-lived access token) and `JELLYFIN_API_KEY`, mode 600 |
| `/opt/stacks/immich/.env` | `DB_PASSWORD=` a generated random string, mode 600 — see [.env.example](../stacks/immich/.env.example) |
| `/opt/stacks/dns/conf/AdGuardHome.yaml` | the AdGuard admin password hash — the live file is gitignored; the tracked seed carries a placeholder |
| HA `.storage/`, `secrets.yaml` | integration configs and tokens — recreated by re-adding integrations |
| Frigate `.jwt_secret`, `*.db` | regenerated automatically on first start |

Camera admin passwords exist only on the cameras themselves and in `.env`.
