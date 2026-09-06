# Uptime Kuma

The watchdog at `http://192.168.0.13:3001`. It watches everything else on
`ladybird` and pushes to a phone when something stops answering.

Monitor definitions live in
[stacks/net/kuma-monitors.yml](../stacks/net/kuma-monitors.yml) — **that file is
the source of truth**, this page is how to apply it.

---

## Why there is no provisioning script

Kuma 1.x has no config-as-code path. Monitors, notifications, and settings all
live in a SQLite database at `net/uptime-kuma/kuma.db`, which is gitignored
because it also holds the admin password hash and the ntfy token. The socket.io
API can be driven from a script, but it is unversioned and shifts between point
releases — a script written today breaks on an upgrade you did not know changed
anything, and it breaks silently, leaving you with a monitoring system you
believe is configured.

Eleven monitors and three groups is twenty minutes of clicking, once. Change
the YAML first, then mirror it in the UI.

---

## 1. First run

Open `http://192.168.0.13:3001`. The first visit asks you to create the admin
account; there is no default login. Use a password from your manager and store
it there — it is not recoverable from this repo, and it is not in `.env`.

Then **Settings → General**:

| Setting | Value | Why |
|---|---|---|
| Primary Base URL | `http://192.168.0.13:3001` | otherwise notification links point nowhere |
| Display Timezone | `America/Denver` | matches the host |
| Check Update | off | the `:1` image tag already pins the major version |

**Settings → Monitor History** → keep 90 days. The default 180 grows the
database for history nobody reads.

Leave authentication **on**. Kuma is http-only on the LAN; disabling its login
would leave the one service that knows the shape of your whole network open to
anything that reaches port 3001.

---

## 2. ntfy notification

Kuma alerts through [ntfy.sh](https://ntfy.sh) — a public relay, no account, no
SMTP to babysit.

**Do not self-host ntfy on this box.** The notifier has to survive the thing it
is notifying about; an ntfy container next to Kuma goes down in exactly the
outage you needed it for. A public relay is the point.

The tradeoff is that on `ntfy.sh`, **the topic name is the only secret** —
anyone who guesses it reads every alert, and your alerts name your services and
IP addresses. So generate a real one rather than picking something memorable:

```bash
echo "ladybird-$(head -c 12 /dev/urandom | base64 | tr -dc 'a-z0-9')"
```

Record it in the repo's gitignored `.env` as `NTFY_TOPIC=` so a rebuild can
recreate it, then subscribe the phone app to that exact topic.

In Kuma, **Settings → Notifications → Setup Notification**:

- Notification Type: **ntfy**
- Friendly Name: `ntfy`
- Server URL: `https://ntfy.sh`
- Topic: the generated string
- Priority: `4` (high — arrives with sound)
- **Default enabled** and **Apply on all existing monitors**: both on

Hit **Test** before saving. A test that does not reach the phone means the topic
is mistyped, and you will not find out later — a notifier that never fires looks
exactly like a network that never breaks.

---

## 3. Create the monitors

Work down [kuma-monitors.yml](../stacks/net/kuma-monitors.yml). Create the three
groups first (Add New Monitor → type **Group**), then each monitor with its
`group` set as the parent. Groups are organisational only — they do not suppress
a child's alerts when the parent is down.

Everything is addressed as `192.168.0.13`, never `localhost`: Kuma is in a
bridge container, and Home Assistant runs with host networking, so from Kuma's
perspective every other service is out on the LAN.

Three that will bite if you skip the detail:

- **Frigate UI** needs *Ignore TLS/SSL error* on. Frigate self-signs, so without
  it the monitor is permanently down. Turn *Certificate Expiry Notification* off
  in the same panel, or the self-signed cert nags forever.
- **Dashboard** is a **HTTP(s) - Json Query** monitor, not a plain HTTP one: json
  path `$.ha.connected`, expected value `true`. The dashboard returns a cheerful
  200 with a revoked `HA_TOKEN` while every panel sits empty, so a status-code
  check would call that healthy.
- **Cameras** are ping only. Nothing but the server authenticates to them.

The camera monitors reach `10.10.10.x` from a bridge container by way of the
host's second interface. Confirm both go green before trusting them — if they
sit down, check from the host first:

```bash
ping -c1 10.10.10.201
```

```bash
docker exec uptime-kuma ping -c1 10.10.10.201
```

Host works but container does not means Docker is not routing to the camera
island. Monitor the cameras from a `network_mode: host` sidecar in that case,
rather than poking holes in the isolation.

---

## 4. Wire the storage guard's push monitor

This is the one that catches a dead server, so it is worth doing carefully.

Create the **Storage guard** push monitor from the YAML, then copy the push URL
Kuma shows on the monitor page onto the server:

```bash
printf '%s\n' "http://192.168.0.13:3001/api/push/XXXXXXXXXX" > /opt/stacks/net/kuma-push-url.txt
```

```bash
chmod 600 /opt/stacks/net/kuma-push-url.txt && /opt/stacks/net/disk-guard.sh
```

The monitor should flip to Up within seconds. The file is gitignored — the token
lets anything holding it fake a healthy heartbeat.

The logic is inverted on purpose, and it is the whole reason this monitor
exists: `disk-guard.sh` beats the URL **only when status is ok**. A disk warning,
a disk critical, a hung timer, a crashed Docker, and an unplugged server all
produce the same signal — silence — and silence is what Kuma alerts on. Nothing
has to successfully report its own failure.

Heartbeat interval is 900s against a guard that runs every 10 minutes, so one
missed run is tolerated and two are not.

Verify the failure path once, now, rather than discovering it during an outage:

```bash
sudo systemctl stop disk-guard.timer
```

Wait about fifteen minutes and expect a red ntfy push, then:

```bash
sudo systemctl start disk-guard.timer
```

---

## 5. Optional additions

**A status page.** Kuma can publish a read-only page listing every monitor. It is
genuinely useful on a phone, but it is unauthenticated by default and lists your
internal service names and addresses. If you make one, leave it **unpublished**
unless you have set a password on it, and never expose 3001 beyond the LAN and
tailnet.

**Deeper MQTT check.** The port monitor in the YAML proves the broker accepts
TCP, not that anything is publishing. An **MQTT** monitor against
`192.168.0.13:1883` on topic `ladybird/storage/state`, keyword `status`, matches
instantly off the retained message and so proves broker *and* guard in one check.
Add it if you want the broker covered independently of the push monitor.

**Docker container monitors.** Kuma can watch containers directly, which catches
a restart loop that still answers HTTP between crashes. It needs
`/var/run/docker.sock` mounted into the Kuma container, and that socket is
root-equivalent on the host — it hands anything that compromises Kuma full
control of the machine. Not worth it here: every container in the stack already
has an HTTP or port check in front of it.

---

## Rebuilding after a wipe

`net/uptime-kuma/` is neither backed up nor in git. After a rebuild, redo this
page top to bottom: new admin account, ntfy notification with the `NTFY_TOPIC`
from `.env`, monitors from the YAML, and a fresh push URL into
`kuma-push-url.txt`.

**The old push token does not survive.** The guard will keep POSTing to a URL
that no longer exists — quietly, forever, with no error anywhere. Check it after
any Kuma rebuild:

```bash
cat /opt/stacks/net/kuma-push-url.txt && /opt/stacks/net/disk-guard.sh
```

Monitor history is lost with the database. That is fine — the monitors are cheap
to recreate, and the history is not evidence of anything.
