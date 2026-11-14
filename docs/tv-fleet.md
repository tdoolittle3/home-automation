# TV fleet — Fire TVs as house alert screens

The Fire TVs run the **Ladybird TV app** (built in the `home-dashboard` repo,
`tv-app/`, package `com.thomas.ladybird`). It is the screensaver, showing the
dashboard's `/tv` page. It also keeps a stream open to a **LAN ntfy server** and
draws alert banners over whatever is playing. This doc covers how the fleet
is inventoried, provisioned and watched, how house events become TV banners,
and the topic and priority contract both sides depend on.

The app's own contract is `tv-app/CONTRACT.md` in the dashboard repo. This
side relies on its provisioning intent, its topic convention and its priority
table.

```
 tv-fleet.yml ──► tv-fleet.py provision ──adb──► TV: APK, overlay, screensaver, config intent
      │           tv-fleet-check.timer  ──adb──► TV: verify + re-apply ──► phone ntfy.sh (low)
      │           tv-fleet-status.service ─adb─► TV: focus + media_session ──► MQTT ──► HA sensors
      ▼
 HA automations ──rest_command.ntfy_tv──► ntfy :8090 (LAN, token auth) ──stream──► TV banners
 (Frigate zone, ZHA leak, ZHA doors)
```

## What it connects to

| Thing | Where | Role |
|---|---|---|
| Inventory | [stacks/net/tv-fleet.yml](../stacks/net/tv-fleet.yml) → `/opt/stacks/net/` | the one list of TVs (name, room, IP, extra topics) + fleet defaults |
| Fleet tool | [stacks/net/tv-fleet.py](../stacks/net/tv-fleet.py) | `provision` / `check` / `status` / `notify` |
| Watchdog | `tv-fleet-check.timer` (every 10 min) | re-applies drift, reports to the phone |
| Status poller | `tv-fleet-status.service` (every 30 s) | foreground app + playing → MQTT → HA |
| TV ntfy | [stacks/ntfy](../stacks/ntfy/) → `http://192.168.0.13:8090` | the bus the TVs subscribe to |
| Phone ntfy | ntfy.sh (the existing topic) | watchdog failure/repair reports only |
| HA | `rest_command.ntfy_tv`, 3 `tv_*` automations, `script.tv_test_notify` | event → banner routing |
| Frigate | `driveway` camera, `front_yard` zone; `:5000/api/driveway/latest.jpg` | person trigger + snapshot |
| ZHA | `binary_sensor.sink_moisture_detector`, the two door contacts | leak + door triggers |
| Secrets | `/opt/stacks/net/tv-fleet.env`, HA `secrets.yaml` | ntfy tokens, phone URL |

## First deploy — order matters

`configuration.yaml` references `!secret ntfy_tv_auth`. **If that key is missing
from `secrets.yaml` when HA restarts, HA fails to load its config.** Converge
restarts HA on any config change, so the secret has to be in place first:

1. Bring up only the ntfy stack and create its users and tokens (see
   "ntfy server" below). Run `docker compose up -d` in `/opt/stacks/ntfy` after
   copying `stacks/ntfy/` there, or wait for the converge in step 3. HA must
   not restart before step 2.
2. Add `ntfy_tv_auth: "Bearer <ha token>"` to HA's `secrets.yaml` (via
   `docker exec`, see the gotcha). Write `/opt/stacks/net/tv-fleet.env` with
   `chmod 600` and owner thomas. Vault both.
3. Converge (base → stacks → guards). This installs `adb`, `python3-yaml` and
   `python3-paho-mqtt`, syncs the script and inventory, starts ntfy, restarts
   HA, and enables `tv-fleet-check.timer` and `tv-fleet-status.service`.
4. Copy the APK to `/opt/ladybird-tv/ladybird.apk`, then run
   `tv-fleet.py provision`. Until the APK exists, the watchdog sends one `low`
   "not installed and APK not found" push. That's expected.
5. Send a test banner with `script.tv_test_notify` or `tv-fleet.py notify`.

## The fleet

| name | room | IP | model | Fire OS |
|---|---|---|---|---|
| `living` | living | 192.168.0.18 (DHCP reservation for .17 pending) | AMZ_K42NE5_4K55N400A | 7.7.1.7, WebView 138 |

The IP lives **only** in `tv-fleet.yml`. When the .17 reservation applies,
change that one line. Nothing else holds TV addresses. To add a TV:

1. On the TV: Settings → My Fire TV → Developer options → **ADB debugging on**
   (and Network debugging, if the model lists it separately). Give it a DHCP reservation.
2. Add an entry under `tvs:` in `tv-fleet.yml`. `name` and `room` must be
   lowercase slugs. `room` must match a `tv.rooms` key in the dashboard's
   `config/dashboard.json`, or the `/tv` page falls back to the default view.
3. Converge, or copy the file to `/opt/stacks/net/`. Then run
   `tv-fleet.py provision <name>` on ladybird **with the TV on and someone in
   front of it**. The first connection pops "Allow USB debugging?". Tick
   *Always allow from this computer*.
4. HA picks up the TV's MQTT sensors on the next status poll. No HA config change is needed.

## Provisioning — `tv-fleet.py provision [name]`

Run on ladybird **as thomas**, never with sudo (see the adb-key gotcha below):

```bash
/opt/stacks/net/tv-fleet.py provision          # every TV
/opt/stacks/net/tv-fleet.py provision living   # one TV
```

Every step reads first, writes only on drift, and reads back to verify:

| Step | Read | Write |
|---|---|---|
| connect | `adb connect <ip>:5555`, `get-state` | — (stale `offline` transport: disconnect + retry once) |
| install | `pm path com.thomas.ladybird` + sha256 of the APK vs. the last one installed | `adb install -r <apk>` |
| overlay | `appops get … SYSTEM_ALERT_WINDOW` | `appops set … SYSTEM_ALERT_WINDOW allow` |
| screensaver | `settings get secure screensaver_components` | `… com.thomas.ladybird/.LadybirdDream` |
| | `settings get secure screensaver_enabled` | `1` |
| | `settings get system screen_off_timeout` | `screensaver_timeout_min × 60000` (default 10 min) |
| config | — | `am start -n …/.MainActivity --es dashboard_url … room … ntfy_url … ntfy_topics … ntfy_token …` |

It prints one block per TV: `OK` / `CHANGED` / `FAILED` / `SKIPPED`, each with
the per-step lines. The exit code is 1 if any TV failed or was unreachable.

Notes:

- **Unauthorized** fails with a clear "accept 'Allow USB debugging?' on the
  TV" message. **Unreachable** (off, asleep, wrong IP) is `SKIPPED`.
- **Screensaver timeout key.** On Fire OS, "screensaver start time" is
  `system screen_off_timeout`. Verified on TV 1, where the factory 5 minutes
  reads back as `300000`. Device sleep is a separate setting,
  `secure sleep_timeout` (14 min on TV 1). If the screensaver timeout is at or
  above the sleep timeout, the TV sleeps first. The tool warns about this but
  doesn't change the sleep setting.
- **The config intent launches the app full-screen.** That's expected on a
  provision run. The watchdog therefore never sends it. See below.
- The token goes over adb as a command-line argument. That's acceptable on a
  LAN host you own. It is never written to disk outside the app's prefs.

### Ansible later

Each step is a separate function with the same read → compare → write → verify
shape, so a `tv_fleet` role is a direct translation. Loop over
`tv-fleet.yml`'s `tvs` and make one `command` task per step, with
`changed_when` keyed off the read-back. Until then, the guards role installs
the units and the stacks role ships the script and inventory. Provisioning stays
a manual step, because a human has to accept the adb prompt on each new TV anyway.

### Updating the APK

The APK is a build artifact of the dashboard repo and is not in git:

```bash
scp tv-app/app/build/outputs/apk/release/app-release.apk thomas@192.168.0.13:/opt/ladybird-tv/ladybird.apk
ssh thomas@192.168.0.13 /opt/stacks/net/tv-fleet.py provision
```

`provision` reinstalls only where the APK's sha256 differs from the last one it
installed on that TV. `-r` keeps the app's stored config. `--force-install`
reinstalls regardless. The watchdog **never upgrades**. It only reinstalls an
app that has disappeared.

## Watchdog — `tv-fleet-check.timer`

Every 10 minutes ([unit](../host/etc/systemd/system/tv-fleet-check.service)),
`tv-fleet.py check` runs the same desired state as provision, with two
differences: no upgrades, and no config intent. It catches Fire OS updates that
reset the screensaver, revoke the overlay, or remove the app.

| TV state | What happens | Phone ntfy (ntfy.sh topic) |
|---|---|---|
| off / unreachable | skipped, `consecutive_unreachable += 1` | **none** |
| all correct | counters reset | none |
| drift repaired | re-applied, listed | `min`: "TV living: re-applied …" |
| repair failed / unauthorized / app gone with no APK | `consecutive_failures += 1` | `low`, **once per distinct failure** (no 10-minute spam) |
| failure clears | counters reset | `min`: "TV living: recovered" |

If the app was **reinstalled**, its stored config is gone and needs the config
intent. That launches the app over the screen, so the watchdog sends it only
while the TV shows the home screen, profile picker or screensaver. Otherwise it
sets `pending_config` and tries again on the next run.

State is kept in `/var/lib/tv-fleet/state.json` (systemd `StateDirectory`, owned
by thomas). Inspect it with:

```bash
cat /var/lib/tv-fleet/state.json; journalctl -u tv-fleet-check -n 30
```

## HA sensors — `tv-fleet-status.service`

Every 30 s the poller reads each TV over adb and publishes to MQTT (retained)
with HA discovery. The sensors come from the inventory, with no per-TV YAML:

| Entity | State | Attributes |
|---|---|---|
| `sensor.tv_<name>_app` | friendly app name (`Home`, `YouTube`, `Sling`, … from `app_names`, else the package) | `package`, `activity`, `playing`, `playing_package` |
| `binary_sensor.tv_<name>_playing` | on/off | same |

An off or unreachable TV is published as `offline` on
`ladybird/tv/<name>/availability`, so both entities show *unavailable*.

**Playing = `state=3` AND (session `active=true` OR its package is the foreground app).**
A bare "any media_session state=3" is wrong on Fire OS. TV 1 showed YouTube
holding `state=3` with `active=false` while the profile picker was on screen,
which would have read as *playing* on an idle TV. `state=2` (paused) and
`state=0` (none) never count.

Sources:
- foreground: `dumpsys window | grep mCurrentFocus`. Home is `com.amazon.tv.launcher/…HomeActivity_vNext`.
- playback: `dumpsys media_session`.

Debug without MQTT:

```bash
/opt/stacks/net/tv-fleet.py status --dry-run
```

### Why not the androidtv integration

Home Assistant's **Android TV (ADB)** and **Android TV Remote** integrations
can only be set up through the UI, with no YAML import, so they can't live
in this repo. The ADB one would also be a *second* adb client holding its own
key, which every TV has to accept separately. The MQTT poller covers the two
sensors asked for and stays code. If you later want a `media_player` (remote
control, app launching), add it by hand:

1. Settings → Devices & services → Add integration → **Android TV**.
2. Host = the TV's IP from `tv-fleet.yml`, port 5555, ADB server left blank (Python ADB).
3. Accept the new "Allow USB debugging?" prompt on the TV. HA's key is stored in `/config/.storage`.
4. Repeat per TV. These entries are **not** in git and must be recreated by hand on rebuild.

## Notification routing → ntfy

### ntfy server

Phone alerts and Kuma use **public ntfy.sh**, where the topic name is the only
secret. The TV topics are guessable by design (`ladybird-house`), so they can't
go there. Anyone could read "door opened at 02:14" or put fake leak banners on
the TVs. They run on a LAN ntfy, [stacks/ntfy](../stacks/ntfy/), at
`http://192.168.0.13:8090`:

- `auth-default-access: deny-all`: nothing is readable or writable without a token.
- Users and ACLs: HA publishes with a **write-only** token, the TVs subscribe
  with a **read-only** one. Both are scoped to `ladybird-*`.
- A 24 h message cache, so a TV that slept replays what it missed via `since=<id>`.
- Uploads are off. Only external attachment URLs are allowed (the Frigate snapshot).

[uptime-kuma.md](uptime-kuma.md) says *don't self-host ntfy on this box*. That
rule is about **phone** alerts, which must survive ladybird dying. It doesn't
apply here. Every producer of TV alerts (HA, Frigate) runs on ladybird, so if
it is down there's nothing to route. Phone alerts are unchanged and don't
depend on this server. Kuma watches it as *ntfy (TV bus)*.

**One-time setup** after the stack is first up (users and tokens live in
`ntfy/data/user.db`, gitignored):

```bash
docker exec -it ntfy ntfy user add ha        # prompts for a password (unused after; store in the vault)
docker exec -it ntfy ntfy user add tv
docker exec ntfy ntfy access ha 'ladybird-*' write-only
docker exec ntfy ntfy access tv 'ladybird-*' read-only
docker exec ntfy ntfy token add ha           # -> HA secrets.yaml ntfy_tv_auth + tv-fleet.env NTFY_PUBLISH_TOKEN
docker exec ntfy ntfy token add tv           # -> tv-fleet.env NTFY_TV_TOKEN
```

### Topics

| Topic | Who subscribes | Use |
|---|---|---|
| `ladybird-house` | every TV | house-wide events |
| `ladybird-tv-<room>` | the one TV in `<room>` | targeted messages |

Each TV gets `ladybird-house,ladybird-tv-<room>` plus any `topics:` extras from
the inventory, pushed as `ntfy_topics`.

### Priority contract (the TV app keys banner behavior off this)

| Priority | Meaning | TV behavior |
|---|---|---|
| **5** | leak or other urgent | red banner, stays until a newer message (60 s max) |
| **4** | person at the door / driveway | banner, 10 s |
| **1–3** | informational | banner, 5 s |

In HA, `rest_command.ntfy_tv` takes a **numeric** `priority`, plus `topic`,
`title`, `message` and optional `tags` and `attach` (an image URL the TV
fetches itself, so it must be reachable from the LAN). The phone's
`rest_command.ntfy_alert` is untouched and still uses names (`high`, `urgent`).

### Automations ([automations.yaml](../stacks/home/homeassistant/automations.yaml))

| id | Trigger | Topic, priority | Notes |
|---|---|---|---|
| `tv_person_front_yard` | `binary_sensor.front_yard_person_occupancy` → on | house, **4** | attaches `http://192.168.0.13:5000/api/driveway/latest.jpg?h=480&bbox=1&ts=…`. Gated on the `front_yard` zone, the same filter the Frigate review alerts use, so street traffic never fires. It's a person trigger, so the household's parked cars can't fire it. At most one per 2 min. |
| `tv_water_leak` | `binary_sensor.sink_moisture_detector` → on | house, **5** | re-sent every minute while wet (the app caps a red banner at 60 s), 30 min max. Runs alongside the phone's `water_leak_ntfy`. |
| `tv_door_open_overnight` | front door / sun-room door off → on, 00:00–05:30 | house, **4** | stands in for "open while nobody's home". There's no presence tracking yet (no `person.*` / `device_tracker.*` entities). Once phones are tracked, switch the condition to `zone.home == 0`. |

**Porch:** there's no porch camera. The driveway `front_yard` zone is the
front of the house. When a porch camera arrives, add its zone occupancy sensor
to `tv_person_front_yard`'s trigger.

### Test notification

- **HA:** Developer tools → Actions → `script.tv_test_notify`, with topic, priority (1–5) and message.
- **Shell** (on ladybird, uses `NTFY_PUBLISH_TOKEN`):

  ```bash
  /opt/stacks/net/tv-fleet.py notify house "hello from ladybird" --priority 3
  /opt/stacks/net/tv-fleet.py notify tv:living "red test" --priority 5
  /opt/stacks/net/tv-fleet.py notify ladybird-house "person test" --priority 4 \
      --attach "http://192.168.0.13:5000/api/driveway/latest.jpg?h=480"
  ```

## Secrets

Nothing secret is in git. Both files below go into Vaultwarden as Secure Notes
named `<host>:<path>` ([vaultwarden.md](vaultwarden.md)), so `vault-render.sh`
can recreate them.

| File | Keys | Owner / mode |
|---|---|---|
| `/opt/stacks/net/tv-fleet.env` | `NTFY_TV_TOKEN` (read-only, pushed to TVs), `NTFY_PUBLISH_TOKEN` (write-only, for `notify`), `NTFY_PHONE_URL` (`https://ntfy.sh/<the existing topic>`) | thomas:thomas 600, because the units run as thomas |
| `/opt/stacks/home/homeassistant/secrets.yaml` | `ntfy_tv_auth: "Bearer tk_…"` (HA's write-only token) | root-owned. Edit via `docker exec`, see [gotchas.md](gotchas.md) |

`tv-fleet.env` matches the repo's `*.env` ignore rule.

## Gotchas

- **Run tv-fleet as thomas, never root.** adb authenticates with
  `~/.android/adbkey`. Root has a different key, so every TV would show the
  "Allow USB debugging?" prompt again, and an unattended timer can't click it. The
  script refuses to run as root, and the units set `User=thomas`.
- **A stale `offline` adb transport** after a TV sleeps is dropped and
  reconnected automatically. If a TV is stuck, run `adb disconnect <ip>:5555` as thomas.
- **Fire OS updates** have historically reset the screensaver component. That's
  what the watchdog is for, and the `min` "re-applied" pushes tell you it happened.
- **The config intent steals the screen.** The watchdog only sends it when the TV is idle.
