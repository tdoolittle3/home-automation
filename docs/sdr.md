# SDR — ADS-B receiving on `ladybird`

An RTL-SDR Blog V3 on a 1090 MHz antenna feeding **ultrafeeder**: decodes
ADS-B, serves the **tar1090** map, and feeds adsb.fi and adsb.lol.

Stack lives at `stacks/sdr/` -> `/opt/stacks/sdr/`.

| What | Where |
|---|---|
| tar1090 map | http://192.168.0.13:8080 |
| graphs1090 stats | http://192.168.0.13:8080/graphs1090 |
| Beast output (loopback) | `127.0.0.1:30005` |
| rtl_tcp (only when started by hand) | `192.168.0.13:1234` |

Port 8080 was chosen because 80, 3000, 3001, 5000, 5678, 8096, 8123, 8554,
8555 and 8971 are all already bound on this host.

---

## The one rule: one process owns the dongle

librtlsdr takes an **exclusive USB claim**. Exactly one of ultrafeeder,
rtl_tcp, or rtl_433 can use the dongle at a time — the loser dies with
`usb_claim_interface error -6`.

Ultrafeeder is the default owner. `rtl_tcp` is behind a compose profile so
it never autostarts, and `rtl_433` is commented out entirely (no 433 MHz
antenna yet, and it would need a second dongle anyway).

---

## 1. Host preparation — needs root, run once

Two host-level things must happen before Docker can see the dongle:

1. **Blacklist the DVB-T driver.** The kernel sees the RTL2832U as a TV
   tuner and `dvb_usb_rtl28xxu` grabs it at plug-in.
2. **udev rule** so the device is usable without root.

Both are scripted:

```bash
sudo bash /opt/stacks/sdr/../../home-automation/host/scripts/sdr-host-prep.sh
```

Or from a checkout of this repo on the server:

```bash
sudo bash host/scripts/sdr-host-prep.sh
```

It installs `rtl-sdr`, writes `/etc/modprobe.d/blacklist-rtl-sdr.conf` and
`/etc/udev/rules.d/99-rtl-sdr.rules`, rebuilds the initramfs, re-triggers
udev, then runs `lsusb` and `rtl_test -t` to prove the dongle is free.

**If the driver was already loaded, reboot.** The blacklist is baked into
the initramfs and only takes effect on the next boot.

Expected `rtl_test -t` output:

```
Found 1 device(s):
  0:  Realtek, RTL2838UHIDIR, SN: 00000001
Supported gain values (29): 0.0 0.9 1.4 ... 49.6
```

If it instead says `usb_claim_interface error -6`, the DVB driver still has
it — check `lsmod | grep dvb` and reboot.

---

## 2. Set the antenna position

```bash
cd /opt/stacks/sdr
cp .env.example .env
nano .env
```

`ADSB_LAT` / `ADSB_LON` / `ADSB_ALT` are **mandatory and must be accurate.**

MLAT multilateration solves an aircraft's position by comparing signal
arrival times across many receivers. A receiver reporting a wrong position
degrades solutions for every station in the region, and aggregators do
detect it and drop the feed. Use the real antenna spot to 5+ decimal
places, and an altitude **above mean sea level** (ground elevation plus
mounting height), not height above ground.

---

## 3. Bring it up

```bash
cd /opt/stacks/sdr
docker compose up -d
docker compose logs -f ultrafeeder
```

Healthy startup looks like:

```
[readsb] ... rtlsdr: using device 0: Generic RTL2832U OEM
[readsb] ... Detected Rafael Micro R820T tuner
[readsb] ... gain set to  49.6 dB
[mlat-client] ... connected to feed.adsb.fi
```

Aircraft are arriving once you see lines like:

```
[readsb] ... 12 aircraft, 340 positions
```

Then open **http://192.168.0.13:8080**.

Nothing at all after a few minutes usually means antenna, not software —
1090 MHz is line-of-sight, so an indoor antenna in a basement may genuinely
hear nothing. Check `/graphs1090` for a noise floor before suspecting config.

---

## 4. Using SDR++ from a laptop

Stop ultrafeeder first — it holds the dongle.

```bash
cd /opt/stacks/sdr
docker compose stop ultrafeeder
docker compose --profile rtltcp up -d rtl_tcp
```

In SDR++: **Source -> RTL-TCP**, host `192.168.0.13`, port `1234`.

`rtl_tcp` has no authentication — anyone on the LAN can retune it. Never
port-forward 1234.

Hand the dongle back when done:

```bash
docker compose --profile rtltcp down rtl_tcp
docker compose up -d ultrafeeder
```

There is also a **Beast stream on `127.0.0.1:30005`**. For decoded aircraft
data (Virtual Radar Server, a Home Assistant feed) prefer that over rtl_tcp
— it does not require stopping ultrafeeder. Reach it from a workstation
with an SSH tunnel:

```bash
ssh -L 30005:127.0.0.1:30005 -N thomas@192.168.0.13
```

---

## 5. Adding rtl_433 later

The service is written and commented out at the bottom of
`stacks/sdr/docker-compose.yml`, with MQTT already pointed at the Mosquitto
on the `home` stack (`192.168.0.13:1883`, anonymous — no credentials).

Before enabling you need **a 433 MHz antenna and a second dongle**. The
1090 MHz ADS-B antenna is resonant at the wrong frequency and will hear
almost nothing at 433, and the dongle-exclusivity rule above means it
cannot share with ultrafeeder.

With a second dongle, give each a distinct serial so they can be addressed
individually:

```bash
rtl_eeprom -d 1 -s 00000002
```

Then set that serial in the `rtl_433` command line and uncomment.

---

## Backing this out

```bash
cd /opt/stacks/sdr && docker compose --profile rtltcp down
sudo rm /etc/modprobe.d/blacklist-rtl-sdr.conf
sudo rm /etc/udev/rules.d/99-rtl-sdr.rules
sudo update-initramfs -u
sudo udevadm control --reload-rules
sudo apt-get remove --purge rtl-sdr
```

Reboot to let the DVB-T driver bind the dongle again.

---

## Gotchas

- **The initramfs step is not optional.** Writing the modprobe blacklist
  without `update-initramfs -u` leaves the driver binding at early boot.
  Everything looks configured and nothing works.
- **`udevadm trigger` alone does not re-permission an already-plugged
  device.** It needs `--subsystem-match=usb --action=add`.
- **Don't use a `devices:` mapping for the dongle.** A literal
  `/dev/bus/usb/001/004` path changes on every replug and reboot; the
  `device_cgroup_rules` + `/dev/bus/usb` bind mount survives that.
- **`ULTRAFEEDER_CONFIG` must be a single YAML line.** Both a folded block
  (`>-`) and escaped-newline double quotes leave the continuation lines'
  indentation inside the value, producing ` mlat,...` entries with a
  leading space that the parser rejects. Verify with `docker compose config`.
- **adsb.lol ingests on `in.adsb.lol`**, not `feed.adsb.lol` — unlike
  adsb.fi. Easy to get wrong by symmetry.
- **`/run` is tmpfs on purpose.** readsb rewrites `aircraft.json` several
  times a second; on disk it is the heaviest write-amplifier in the stack.
