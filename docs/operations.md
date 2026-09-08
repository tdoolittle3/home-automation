# Operations runbook

Day-to-day commands for `ladybird` (192.168.0.13). Connect with `ssh thomas@192.168.0.13`.

---

## Health check

```bash
docker ps                                    # every container should be Up (healthy)
df -h /srv/storage                           # disk headroom
docker logs --tail 50 frigate                # recent Frigate activity
/opt/stacks/net/disk-guard.sh                # one-line storage summary
sudo intel_gpu_top                           # Video engine active during transcode/decode
```

Both cameras and the detector in one call:

```bash
curl -s http://127.0.0.1:5000/api/stats | python3 -c "
import json,sys
s=json.load(sys.stdin)
for n,c in sorted(s['cameras'].items()):
    print(f\"{n:10} fps={c['camera_fps']} detect={c['detection_fps']}\")
print('inference ms:', s['detectors']['ov']['inference_speed'])"
```

Expect ~5.0 fps per camera and ~10 ms inference. A camera at 0.0 fps means the stream is down —
check credentials, then the camera itself.

---

## Storage

The storage guard runs every 10 minutes via `disk-guard.timer` and publishes to MQTT, which
surfaces in Home Assistant as:

- `sensor.ladybird_storage_disk_used` (%)
- `sensor.ladybird_storage_disk_free` (GB)
- `sensor.ladybird_storage_frigate_recordings` (GB)
- `sensor.ladybird_storage_media_library` (GB)
- `binary_sensor.ladybird_storage_storage_problem` — build notifications on this; the reason is
  exposed as an attribute

Thresholds live in `/opt/stacks/net/disk-guard.conf` and are re-read every run, so **edits need no
restart**. It **alerts only and never deletes.**

```bash
systemctl status disk-guard.timer
journalctl -u disk-guard.service --since "1 day ago"
du -sh /srv/storage/frigate                  # actual recording footprint
```

To measure the real growth rate — worth doing a few days after any camera is mounted, since a
static bench scene compresses to almost nothing compared to a real one:

```bash
A=$(du -sb /srv/storage/frigate | cut -f1); sleep 300
B=$(du -sb /srv/storage/frigate | cut -f1)
echo "$(( (B-A)*288/1000000 )) MB/day"
```

External watchdog: the guard beats the Uptime Kuma push monitor "Storage guard" (id 12, 900 s
interval) only while healthy, so a hung or dead server alerts by silence. It reads the URL from
`/opt/stacks/net/kuma-push-url.txt` and appends `&status=up&msg=...` to the first line, so the file
must end in `?ping=`. The file is gitignored (it holds the push token); recreate it after a rebuild:

```bash
TOKEN=$(docker exec uptime-kuma sqlite3 /app/data/kuma.db   "select push_token from monitor where name='Storage guard'")
printf 'http://127.0.0.1:3001/api/push/%s?ping=
' "$TOKEN" > /opt/stacks/net/kuma-push-url.txt
chmod 600 /opt/stacks/net/kuma-push-url.txt
curl -fsS "$(head -1 /opt/stacks/net/kuma-push-url.txt)&status=up&msg=test"   # expect {"ok":true}
```

`/opt/stacks/net` is owned by the login user, so this needs no sudo. **Symptom when the file is
missing:** the monitor shows a single manual "up" beat and then stays down, while
`disk-guard.service` exits 0 every 10 minutes and the MQTT state is fresh — the guard is healthy, it
just has nowhere to report. This is exactly what happened 2026-09-05 → 09-06. A second, quieter
failure was found the same day: the script's `msg=` carried a raw space, and curl 8.x rejects the
whole URL as malformed (exit 3) — hidden by the `|| true`. Fixed by using `+` as the separator; if
beats ever stop while the service still exits 0, run the script with `bash -x` and copy the traced
curl line by hand.

---

## Dashboard

The custom UI at `http://ladybird/` (`http://192.168.0.13/`, port 80). The application lives in the separate
`home-dashboard` repo; only its compose file lives here. It is a read-mostly client of Home
Assistant's WebSocket API, so it holds no state of its own — losing it loses nothing.

```bash
curl -s http://127.0.0.1/api/health          # ha.connected, plus the last HA error if any
docker logs --tail 50 home-dashboard
```

`ha.connected: false` with `authFailed: true` means the long-lived access token was revoked or
expired. Issue a new one in HA, update `HA_TOKEN` in `/opt/stacks/dash/.env`, then **force-recreate**
— a plain restart does not reload environment variables:

```bash
cd /opt/stacks/dash && docker compose up -d --force-recreate
```

**Update after changing the app:** copy the new source into `~/src/home-dashboard`, then rebuild.
There is no git remote, so this is a tarball copy — and compose will not rebuild on its own.

```bash
# from a workstation, in the home-dashboard repo
git archive --format=tar HEAD > /tmp/hd.tar && scp /tmp/hd.tar thomas@192.168.0.13:/tmp/

# on the server
rm -rf ~/src/home-dashboard && mkdir -p ~/src/home-dashboard
tar -xf /tmp/hd.tar -C ~/src/home-dashboard
cd /opt/stacks/dash && docker compose up -d --build
```

**Change which panels appear:** edit `config/dashboard.json` in the `home-dashboard` checkout and
rebuild. The file is validated at startup, so a typo fails loudly in the logs rather than rendering
an empty panel. Its `controls` panels are also the write allowlist — an entity not listed there
cannot be actuated through the dashboard, by design. For quick iteration without rebuilds,
uncomment the bind-mount in `stacks/dash/docker-compose.yml`.

---

## Common tasks

**Restart a stack**

```bash
cd /opt/stacks/frigate && docker compose restart
```

**Pick up a changed `.env`** — a plain restart does *not* reload environment variables:

```bash
cd /opt/stacks/frigate && docker compose up -d --force-recreate
```

**Edit Frigate's config** — insert cameras inside the `cameras:` map and **before** the trailing
`version:` key, then validate before restarting:

```bash
python3 -c "import yaml;print(list(yaml.safe_load(open('/opt/stacks/frigate/config/config.yml'))['cameras']))"
```

**Reach a camera's web UI** (from a workstation, not the server):

```bash
ssh -L 8082:10.10.10.201:80 -N thomas@192.168.0.13
# then browse http://localhost:8082
```

**Commit config changes** — `/opt/stacks` is a git repo; commit after every working change:

```bash
cd /opt/stacks && git add -A && git commit -m "what changed"
```

**Pull configs back into this repo** — use an allowlist, never a blanket copy, so credential files
cannot ride along. See the export approach in the repo history.

---

## Renaming a camera

More than a config edit — four places hold the old name:

1. `config.yml` — rename the key under `cameras:`
2. Recordings — `/srv/storage/frigate/recordings/*/*/<oldname>/` becomes orphaned. Frigate no
   longer knows that camera exists, so it will **not** expire them. Delete manually (as root —
   the container writes as root).
3. MQTT — retained `frigate/<oldname>/*` topics linger. Clear each with an empty retained publish:
   `docker exec mosquitto mosquitto_pub -h localhost -t '<topic>' -r -n`
4. Home Assistant — restart it so the Frigate integration drops stale entities and creates new ones.

Cheapest immediately after adding a camera, before recordings accumulate.

---

## Verifying camera isolation

The cameras must never reach the internet. Confirm from the server:

```bash
sudo tcpdump -i enp45s0 -n "ip and not host 10.10.10.50"
```

Expect near silence. Failed ARP for the nonexistent gateway is normal and healthy. Any successful
outbound flow means something is misconfigured — check that the camera's gateway is still
`10.10.10.1` (an address that deliberately does not exist).

---

## Remote access

Tailscale, with the server as a subnet router for `192.168.0.0/24`:

```bash
tailscale status
tailscale ip -4
```

Every service is reachable at its normal `192.168.0.13` address from any tailnet device once the
subnet route is approved in the admin console. **Never port-forward Frigate** — the API on 5000 is
unauthenticated.

---

## After a crash

If the box is unreachable but powered, assume a kernel panic. Power-cycle it, then, once it is back:

```bash
journalctl --list-boots                          # the previous boot is -1
journalctl -k -b -1 -n 100 --no-pager            # last kernel lines before the freeze
ls /var/lib/systemd/pstore/                      # one <epoch> dir per panic, moved from EFI at boot
sudo cat /var/lib/systemd/pstore/*/001/dmesg.txt # the panic text; parts are stored newest-first
sudo ras-mc-ctl --errors                         # decoded machine checks (rasdaemon; lives in /usr/sbin)
```

`thomas` is in `systemd-journal` and `adm`, so `journalctl -k -b -1` needs no sudo; the pstore
copies are root-only. Uptime Kuma's own database gives the freeze time to the second:

```bash
docker exec uptime-kuma sqlite3 /app/data/kuma.db   "select m.name, max(h.time) from heartbeat h join monitor m on m.id=h.monitor_id group by m.name"
```

Times in that DB are UTC. Frigate's nginx log (`docker logs frigate`) is in local time.

Since 2026-09-06 the host reboots itself 10 s after a panic (`kernel.panic=10`) and runs with
`pcie_aspm=off`; `rasdaemon` records any future machine check. If a panic recurs, the first question is
whether `journalctl -k -b -1` again shows an `igc ... NETDEV WATCHDOG` on `enp45s0` just before it.

---

## Backups

Worth capturing periodically, none of it in git:

| What | Where |
|---|---|
| Stack configs | `/opt/stacks` (already a git repo) |
| HA configuration + state | `/opt/stacks/home/homeassistant/` including `.storage/` |
| Frigate event database | `/opt/stacks/frigate/config/*.db` |
| Camera credentials | `.env` here, `/opt/stacks/frigate/.env`, `.camcreds` |

Recordings in `/srv/storage/frigate` are intentionally *not* backed up — they age out by design.

---

## Phase 2 — drive migration

When the larger NVMe arrives, the Stage 3 layout makes this a remount rather than a
reconfiguration; no container paths change.

```bash
lsblk                                        # identify the new disk
sudo mkfs.ext4 /dev/nvme1n1
sudo mkdir /mnt/new && sudo mount /dev/nvme1n1 /mnt/new
cd /opt/stacks && docker compose -f frigate/docker-compose.yml down
sudo rsync -aHAX --info=progress2 /srv/storage/ /mnt/new/
sudo blkid /dev/nvme1n1                      # add UUID to /etc/fstab at /srv/storage
sudo umount /mnt/new
sudo mv /srv/storage /srv/storage.old && sudo mkdir /srv/storage && sudo mount -a
docker compose -f frigate/docker-compose.yml up -d
```

Verify recordings appear, then remove `/srv/storage.old`.

This is also the right moment to give `/srv/storage/frigate` its **own filesystem** — the only way
to get a genuine hard cap, since Frigate has no GB quota and a growing media library can otherwise
starve recordings.
