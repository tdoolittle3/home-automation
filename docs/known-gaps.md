# Known gaps

What is deliberately unfinished, and what it would cost to leave it that way. Honest list — if
something here bites, it bit on purpose.

---

## Backups

- **Immich has no automated backup, and no off-box destination exists.** The photo library is
  irreplaceable in a way recordings are not, and a copy of the Postgres directory does not count —
  it needs a logical dump. The procedure is in [operations.md](operations.md#immich); it is not on a
  timer, and until an off-box location exists, even a scheduled dump dies with the disk. **This is
  the biggest gap on the list.**
- **n8n's database is not backed up.** Workflows can be re-imported from
  [stacks/n8n/workflows/](../stacks/n8n/workflows/), but the credentials and the encryption key
  exist only in `/opt/stacks/n8n/n8n-data/` — losing it means recreating every credential by hand.
- **Uptime Kuma's configuration is not backed up.** The monitors are defined in
  [stacks/net/kuma-monitors.yml](../stacks/net/kuma-monitors.yml) and applied by
  [kuma-provision.py](../stacks/net/kuma-provision.py), but Kuma 1.x keeps the live set in a
  gitignored SQLite database. A wipe means re-running the script *and* reissuing the guards' push
  tokens, which do not survive. See [uptime-kuma.md](uptime-kuma.md).
- **The Kuma push monitors need a file that is not in this repo.** The storage guard beats its push
  monitor only if `/opt/stacks/net/kuma-push-url.txt` exists (same for the UPS guard). Those files
  hold push tokens, so they are gitignored and must be recreated on a rebuild — until then those
  monitors, and their "Host and network" group, show down.

## Nothing watches ladybird from outside ladybird

Uptime Kuma runs on the machine it is monitoring. It notices a stale guard heartbeat while it is
running; it cannot say anything once the host is gone. This matters more now that the whole house's
DNS lives here.

The Raspberry Pi 2 is earmarked for the job — installed, cabled, powered, and currently serving only
as the DNS rollback. The watchdog role is still not configured, and the hard part is not the
monitoring but the alert path: it must reach the internet without ladybird forwarding traffic. See
[planned Pi watchdog](diagrams/README.md#planned-pi-watchdog).

## Storage has no hard cap

Frigate has no GB quota — retention is days-only, and its internal `StorageMaintainer` only steps in
at under one hour of free space, which is too late to rely on. The storage guard alerts but
deliberately never deletes. A real cap means a separate filesystem for `/srv/storage`, which belongs
with the Phase 2 drive ([operations.md](operations.md#phase-2--drive-migration)).

Steady state is roughly 183 GB, about 55% of the disk.

## Single points of failure

- **One disk.** Root, app state, recordings, photos and the file share all live on the same NVMe.
- **DNS.** Ad filtering for the whole house is one container on one machine — the Pi-hole at
  `192.168.0.14` is the rollback for as long as it stays powered. Before ever retiring it, export
  its allowlist with Teleporter and port it into AdGuard's custom rules: that list is years of
  accumulated "this broke, so I unblocked it" and exists nowhere else.
- **The DHCP reservation for MAC `38:05:25:35:71:69` is mandatory.** If ladybird comes back on a
  different address, the house loses DNS.

## Camera clocks

The DST end rule on both cameras reads `Day=2` where Sunday would be `0`. Verify the camera clocks
in early November.

---

## Closed

- ~~UPS monitoring (NUT) not configured~~ — **done 2026-09-08.** The rack's CyberPower CP1000AVRLCDa
  is on USB, NUT and `ups-guard.timer` report to the "UPS power" Kuma monitor, and `upsmon` halts
  the box cleanly at low battery. See [operations.md](operations.md) → UPS and power.
- ~~DNS cutover to AdGuard Home pending~~ — **done 2026-09-20.** The router hands out
  `192.168.0.13`; the Pi-hole is now the rollback, not the resolver.
