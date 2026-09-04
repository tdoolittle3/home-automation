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
- `sensor.ladybird_storage_immich_library` (GB)
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

Optional external watchdog: create a Push monitor in Uptime Kuma and drop its URL into
`/opt/stacks/net/kuma-push-url.txt`. The guard beats it only while healthy, so a hung or dead
server alerts by silence.

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

## Immich

Photo library at `http://192.168.0.13:2283`. Four containers: `immich_server`,
`immich_machine_learning`, `immich_redis`, `immich_postgres`.

```bash
cd /opt/stacks/immich && docker compose ps
docker logs --tail 50 immich_server
curl -s http://127.0.0.1:2283/api/server/ping     # expect {"res":"pong"}
```

### Backing it up

**Copying the `postgres/` directory is not a valid backup.** Immich's database carries vector
extension state; restore it from a logical dump, not a file copy. Dump the database *first*, then
the library — the other order leaves database rows pointing at files that were not captured yet.

```bash
cd /opt/stacks/immich
docker exec -t immich_postgres pg_dump --clean --if-exists --dbname=immich --username=postgres | gzip > /srv/storage/archive/immich-db.sql.gz
```

Then the originals. `/srv/storage/photos/library`, `upload`, and `profile` are the three that
matter; `thumbs` and `encoded-video` are regenerable.

```bash
rsync -aHAX /srv/storage/photos/{library,upload,profile} /path/to/backup/
```

To restore, per Immich's documented procedure — note the `sed`, which is required, not optional:

```bash
cd /opt/stacks/immich
docker compose down -v && docker compose pull && docker compose create
docker start immich_postgres && sleep 10
SEDFIX="s/SELECT pg_catalog.set_config('search_path', '', false);/SELECT pg_catalog.set_config('search_path', 'public, pg_catalog', true);/g"
gunzip --stdout /srv/storage/archive/immich-db.sql.gz | sed "$SEDFIX" | docker exec -i immich_postgres psql --dbname=immich --username=postgres --single-transaction --set ON_ERROR_STOP=on
docker compose up -d
```

### Upgrading

`IMMICH_VERSION` is pinned to an exact release in `.env`, deliberately — Immich ships breaking
changes between minor versions and the database image is version-matched to the server. So an
upgrade is a two-part edit, and the database dump comes first.

1. Dump the database (above).
2. Read the release notes for every version between the current pin and the target.
3. Diff the upstream compose against ours — the `database` and `redis` image digests move:
   `curl -sL https://github.com/immich-app/immich/releases/latest/download/docker-compose.yml`
4. Bump `IMMICH_VERSION` in `.env` and the two pinned digests in `docker-compose.yml` together.
5. `docker compose pull && docker compose up -d`, then watch `docker logs -f immich_server` for
   the migration to complete.

### Machine learning runs on CPU, on purpose

`immich-machine-learning` uses the CPU build. The OpenVINO build would share the iGPU with
Frigate's object detector, and Immich's initial import is a long saturating batch — Frigate is the
workload that must not stall. To switch it later, once the library has finished importing:

```bash
# in docker-compose.yml, on immich-machine-learning:
#   image: ghcr.io/immich-app/immich-machine-learning:${IMMICH_VERSION:-release}-openvino
#   device_cgroup_rules: ['c 189:* rmw']
#   devices: [/dev/dri:/dev/dri]
#   volumes: [model-cache:/cache, /dev/bus/usb:/dev/bus/usb]
```

Watch `sudo intel_gpu_top` and Frigate's inference speed afterwards. If Frigate's ~10 ms
inference degrades, revert — the photo library can wait, the cameras cannot.

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

## Backups

Worth capturing periodically, none of it in git:

| What | Where |
|---|---|
| Stack configs | `/opt/stacks` (already a git repo) |
| HA configuration + state | `/opt/stacks/home/homeassistant/` including `.storage/` |
| Frigate event database | `/opt/stacks/frigate/config/*.db` |
| Immich database + library | see the Immich section above — **a copy of `postgres/` is not a valid backup** |
| Camera credentials | `.env` here, `/opt/stacks/frigate/.env`, `.camcreds` |
| Immich database password | `/opt/stacks/immich/.env` |

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
