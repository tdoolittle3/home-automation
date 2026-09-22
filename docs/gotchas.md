# Hard-won gotchas

Every item here cost real debugging time. Read the relevant ones before changing anything.

## Host and hardware

- **A frozen box with its LEDs on is a kernel panic, not a network fault.** On 2026-09-06 the
  camera-side I226-V NIC (`igc`, enp45s0) took a transmit-queue watchdog reset, and 7 s later the
  kernel panicked on an MCE broadcast timeout (`Not all CPUs entered broadcast exception handler`).
  With the stock `kernel.panic=0` it hung until power-cycled. The crash record survives in EFI
  pstore and is copied to `/var/lib/systemd/pstore/<epoch>/` at the next boot — read it before
  guessing. Mitigations are in [rebuild.md](rebuild.md#13-crash-resilience).
- **Recordings are owned by root** (the container writes as root), so removing them from the host
  needs sudo.
- **`disk-guard.sh` must be owned by root**, or systemd refuses to run the timer's service.

## Docker

- **Containers cannot resolve DNS without `daemon.json`.** The symptom is every `apt` inside a
  container failing with `Temporary failure resolving`.
- **`docker exec -i` inside a `while read` loop consumes the caller's heredoc**, silently
  truncating the loop. Drop `-i` and redirect `</dev/null`.

## Frigate

- **Frigate 0.17 needs an explicit global `model:` block** pointing at
  `/openvino-model/ssdlite_mobilenet_v2.xml`. Without it the detector dies at startup with
  `TypeError: stat: path should be string... not NoneType`, and the UI shows a camera with no
  detection rather than an obvious error.
- **Frigate rewrites `config.yml` and appends a top-level `version:` key at the end.** Never append
  a camera block to the end of the file — it lands outside the `cameras:` map and Frigate refuses to
  start with `yaml: mapping values are not allowed in this context`. Insert *before* the `version:`
  line.
- **Frigate 0.17 defaults `detect.enabled` to false.** Without the explicit global `detect:` block
  the cameras record happily and detect nothing, with no error anywhere. Zero events is not proof
  that the driveway was quiet.

## Cameras

- **A camera reports saved encoder settings back correctly while still streaming the old codec.**
  H.265→H.264 does not take effect until the camera is rebooted. Verify with `ffprobe`, never by
  trusting the camera's web UI.
- **The camera web UI can report "saved" without writing anything.** Enabling sub stream 2 from the
  UI returned success and changed nothing; the same call over the API worked first time. Always read
  the config back over the API after a UI change.
- **Sub stream 1 (`subtype=1`) caps at 704×480 (D1) on both cameras**, but the T54PRO-ZE's sub
  stream 2 (`subtype=2`) goes to 720p/1080p. Frigate's `detect` block must match whichever stream it
  reads, or detection silently runs on a mismatched frame size.
- **The Dahua integration's `light.*_illuminator` drives the infrared emitter**, not the white
  light, on these models. The white light lives at `Lighting_V2[0][0][1]` — see
  [cameras.md](cameras.md). This is not a permissions problem, and changing camera accounts does not
  fix it.
- **Cameras require digest auth and reject basic auth**, so HA's `rest_command` cannot drive them.
  Use `command_line` with `curl`.
- **`curl` needs `-g`** for Dahua URLs, or it interprets `[0][0]` as glob ranges.
- **The DST end rule** on both cameras reads `Day=2` where Sunday would be `0`. Verify camera clocks
  in early November.

## DNS

- **AdGuard's `bootstrap_dns` must be plain IPs, never hostnames.** They resolve the DoH upstreams
  before a resolver exists; a hostname there (or this server's own address) deadlocks AdGuard at
  startup and takes DNS down for the whole LAN, without saying so plainly in the log.
- **A containerised DNS server needs host networking, not a `ports:` mapping.** Docker's userland
  proxy rewrites the source address of inbound UDP, so every query looks like it came from the
  bridge gateway and per-client logs, rules and stats all collapse into one useless row.
- **AdGuard rewrites its own config and strips every comment**, exactly like Frigate's `config.yml`.
  The tracked `stacks/dns/AdGuardHome.yaml` is the seed; the live file under `conf/` is gitignored.
  Pull UI changes back by hand — and never paste the live file in, it carries the password hash.
- **`hostsfile_enabled` must be off** and **rewrites need `enabled: true`** — found the hard way at
  first deploy. See [dns.md](dns.md).

## Monitoring

- **A monitor defined in `kuma-monitors.yml` is not a monitor that exists.** The YAML is the
  intended set; it only becomes real when [kuma-provision.py](../stacks/net/kuma-provision.py) is
  run against the live instance. Things sat in that file unapplied for weeks without anyone
  noticing, because an absent monitor looks exactly like a passing one.
- **Do not self-host the notifier on this box.** The thing that tells you the server died has to
  outlive the server; that is why alerts go through public ntfy.sh. See
  [uptime-kuma.md](uptime-kuma.md).

## SDR

- **One process owns the dongle** — readsb, rtl_tcp or rtl_433, never two at once.
- **The initramfs step is not optional.** Writing the modprobe blacklist without
  `update-initramfs -u` leaves the DVB-T driver binding at early boot: everything looks configured
  and nothing works.
- **`udevadm trigger` alone does not re-permission an already-plugged device.** It needs
  `--subsystem-match=usb --action=add`.
- **`ULTRAFEEDER_CONFIG` must be a single YAML line**, and **adsb.lol ingests on `in.adsb.lol`**,
  not `feed.adsb.lol`. Full list in [sdr.md](sdr.md#gotchas).
