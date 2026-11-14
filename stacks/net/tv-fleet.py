#!/usr/bin/env python3
"""Fire TV fleet management for ladybird (docs/tv-fleet.md).

Everything reads the inventory, tv-fleet.yml. Subcommands:

  provision [NAME]   connect, install/upgrade the Ladybird APK, grant the
                     overlay, set the screensaver + timeout, push the app's
                     config intent. Idempotent: each step reads first and
                     writes only on drift.
  check [NAME]       the watchdog (tv-fleet-check.timer). Same desired state as
                     provision, minus upgrades and minus the config intent
                     (that one launches the app over whatever is on screen).
                     Off/unreachable TVs are skipped silently with a
                     consecutive-failure counter; repairs and real failures go
                     to the phone ntfy at low priority, deduplicated.
  status [--loop S]  foreground app + playing, per TV, published to MQTT with
                     HA discovery (tv-fleet-status.service runs --loop 30).
  notify TOPIC MSG   send a test banner through the LAN ntfy.

Must run as the admin user (thomas), never root: adb authenticates with
~/.android/adbkey, and a different user = a different key = every TV prompts
"Allow USB debugging?" again. The systemd units set User=thomas for this.

Each provisioning step is its own function with a read -> compare -> write ->
verify shape, so each maps 1:1 onto an Ansible task later (docs/tv-fleet.md
"Ansible later").

Secrets come from tv-fleet.env (gitignored, mode 600, vaulted):
  NTFY_TV_TOKEN       read-only token pushed to the TVs
  NTFY_PUBLISH_TOKEN  write-only token for `notify`
  NTFY_PHONE_URL      https://ntfy.sh/<topic> for watchdog reports
Environment variables of the same name override the file.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_INVENTORY = os.environ.get("TV_FLEET_INVENTORY", str(HERE / "tv-fleet.yml"))
DEFAULT_ENV = os.environ.get("TV_FLEET_ENV", str(HERE / "tv-fleet.env"))
DEFAULT_STATE = os.environ.get("TV_FLEET_STATE", "/var/lib/tv-fleet/state.json")

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
SECRET_KEYS = ("NTFY_TV_TOKEN", "NTFY_PUBLISH_TOKEN", "NTFY_PHONE_URL")
# Foreground packages during which the watchdog may launch the app to re-push
# its config without stealing the screen from someone watching something.
IDLE_PACKAGES = {"com.amazon.tv.launcher", "com.amazon.ftv.profilepicker", "com.amazon.ftv.screensaver"}


# --------------------------------------------------------------------------- config

def load_inventory(path: str) -> tuple[dict, list[dict]]:
    import yaml  # python3-yaml (base role); imported late so tests stay light

    with open(path, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    return parse_inventory(doc)


def parse_inventory(doc: dict) -> tuple[dict, list[dict]]:
    defaults = dict(doc.get("defaults") or {})
    defaults.setdefault("package", "com.thomas.ladybird")
    defaults.setdefault("house_topic", "ladybird-house")
    defaults.setdefault("adb_port", 5555)
    defaults.setdefault("screensaver_timeout_min", 10)
    defaults.setdefault("mqtt_host", "127.0.0.1")
    defaults.setdefault("mqtt_port", 1883)
    defaults.setdefault("app_names", {})
    tvs, seen = [], set()
    for raw in doc.get("tvs") or []:
        tv = {**defaults, **raw}
        for key in ("name", "room", "ip"):
            if not tv.get(key):
                raise ValueError(f"inventory: a TV is missing '{key}': {raw}")
        if not NAME_RE.match(tv["name"]) or not NAME_RE.match(tv["room"]):
            raise ValueError(f"inventory: name/room must be lowercase slugs: {raw}")
        if tv["name"] in seen:
            raise ValueError(f"inventory: duplicate TV name {tv['name']}")
        seen.add(tv["name"])
        tv["serial"] = f"{tv['ip']}:{tv['adb_port']}"
        tv["topics"] = tv_topics(tv)
        tvs.append(tv)
    return defaults, tvs


def tv_topics(tv: dict) -> list[str]:
    topics = [tv["house_topic"], f"ladybird-tv-{tv['room']}"]
    for t in tv.get("topics") or []:
        if t not in topics:
            topics.append(t)
    return topics


def load_secrets(path: str) -> dict:
    out = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip("'\"")
    for k in SECRET_KEYS:
        if os.environ.get(k):
            out[k] = os.environ[k]
    return out


def select(tvs: list[dict], name: str | None) -> list[dict]:
    if not name:
        return tvs
    hit = [t for t in tvs if t["name"] == name]
    if not hit:
        raise SystemExit(f"no TV named '{name}' in the inventory (have: {', '.join(t['name'] for t in tvs)})")
    return hit


# --------------------------------------------------------------------------- adb

class AdbError(Exception):
    pass


class Unreachable(AdbError):
    """TV off, asleep with networking down, or not on the network. Not alert-worthy."""


class Unauthorized(AdbError):
    """TV answers but hasn't accepted this host's adb key. Needs a human at the TV."""


class Adb:
    def __init__(self, serial: str, runner=subprocess.run):
        self.serial = serial
        self._run = runner

    def _exec(self, args: list[str], timeout: float) -> subprocess.CompletedProcess:
        try:
            return self._run(["adb", *args], capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise Unreachable(f"adb {args[0] if args[0] != '-s' else args[2]} timed out after {timeout:.0f}s") from exc
        except FileNotFoundError as exc:
            raise AdbError("adb is not installed (apt install adb - base role)") from exc

    def connect(self) -> None:
        out = self._connect_once()
        state = self.get_state()
        if state == "offline":
            # A stale transport from before the TV slept: drop it and retry once.
            self._exec(["disconnect", self.serial], timeout=5)
            out = self._connect_once()
            state = self.get_state()
        if state == "device":
            return
        if state == "unauthorized" or "authenticate" in out or "unauthorized" in out:
            raise Unauthorized(
                f"{self.serial} has not authorized this host - accept 'Allow USB debugging?' "
                "on the TV (tick 'Always allow from this computer')"
            )
        raise Unreachable(f"{self.serial}: {out.strip() or state or 'no response'}")

    def _connect_once(self) -> str:
        p = self._exec(["connect", self.serial], timeout=8)
        out = (p.stdout + p.stderr).strip()
        if "connected to" not in out and ("failed" in out or "cannot" in out or "refused" in out):
            if "authenticate" in out or "unauthorized" in out:
                raise Unauthorized(f"{self.serial}: {out}")
            raise Unreachable(f"{self.serial}: {out}")
        return out

    def get_state(self) -> str:
        p = self._exec(["-s", self.serial, "get-state"], timeout=5)
        text = (p.stdout + p.stderr).strip()
        for s in ("device", "unauthorized", "offline"):
            if s in text:
                return s
        return text

    def shell(self, cmd: str, timeout: float = 20) -> str:
        p = self._exec(["-s", self.serial, "shell", cmd], timeout=timeout)
        err = p.stderr.strip()
        if p.returncode != 0 and ("not found" in err or "offline" in err or "closed" in err):
            raise Unreachable(f"{self.serial}: {err}")
        return p.stdout

    def install(self, apk: str) -> None:
        p = self._exec(["-s", self.serial, "install", "-r", apk], timeout=300)
        if "Success" not in p.stdout:
            raise AdbError(f"install failed: {(p.stdout + p.stderr).strip()[-300:]}")


# --------------------------------------------------------------------------- parsers

def parse_focus(text: str) -> tuple[str | None, str | None]:
    """`dumpsys window | grep mCurrentFocus` -> (package, activity)."""
    m = re.search(r"mCurrentFocus=Window\{\S+ \S+ ([\w.]+)/([\w.$]+)\}", text)
    if not m:
        return None, None
    pkg, act = m.group(1), m.group(2)
    if act.startswith("."):
        act = pkg + act
    return pkg, act


def parse_media_sessions(text: str) -> list[dict]:
    """`dumpsys media_session` -> [{package, active, state}] (state None if absent).

    Works on the raw dump and on the grep-filtered one tv-fleet.py requests.
    """
    sessions: list[dict] = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("package="):
            sessions.append({"package": s.split("=", 1)[1].strip(), "active": False, "state": None})
        elif not sessions:
            continue
        elif s.startswith("active="):
            sessions[-1]["active"] = s.split("=", 1)[1].strip() == "true"
        elif s.startswith("state=PlaybackState"):
            m = re.search(r"\{state=(\d+)", s)
            if m:
                sessions[-1]["state"] = int(m.group(1))
    return sessions


def playing_session(sessions: list[dict], foreground: str | None) -> str | None:
    """Package of a session that is really playing, else None.

    state=3 alone is NOT enough on Fire OS: verified on TV 1, YouTube keeps
    reporting state=3 with active=false while the profile picker is on screen.
    So a playing session must also be active, or be the foreground app.
    """
    for s in sessions:
        if s["state"] == 3 and (s["active"] or s["package"] == foreground):
            return s["package"]
    return None


def parse_appop(text: str) -> str | None:
    m = re.search(r"SYSTEM_ALERT_WINDOW:\s*(\w+)", text)
    return m.group(1) if m else None


# --------------------------------------------------------------------------- desired state

def desired_settings(tv: dict) -> list[tuple[str, str, str]]:
    return [
        ("secure", "screensaver_components", tv["screensaver_component"]),
        ("secure", "screensaver_enabled", "1"),
        ("system", "screen_off_timeout", str(int(tv["screensaver_timeout_min"]) * 60_000)),
    ]


@dataclass
class Report:
    name: str
    ok: list[str] = field(default_factory=list)       # already correct
    changed: list[str] = field(default_factory=list)  # drift repaired / work done
    warnings: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    skipped: str | None = None                        # unreachable: why

    @property
    def status(self) -> str:
        if self.skipped:
            return "SKIPPED"
        if self.failures:
            return "FAILED"
        return "CHANGED" if self.changed else "OK"

    def render(self) -> str:
        lines = [f"== {self.name}: {self.status}"]
        if self.skipped:
            lines.append(f"   skipped   {self.skipped}")
        lines += [f"   ok        {x}" for x in self.ok]
        lines += [f"   changed   {x}" for x in self.changed]
        lines += [f"   warning   {x}" for x in self.warnings]
        lines += [f"   FAILED    {x}" for x in self.failures]
        return "\n".join(lines)


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def is_installed(adb: Adb, pkg: str) -> bool:
    return "package:" in adb.shell(f"pm path {pkg}")


def step_install(adb: Adb, tv: dict, rep: Report, tv_state: dict, *, upgrade: bool, force: bool) -> bool:
    """Returns True when the app was freshly installed (prefs empty -> config must be pushed)."""
    pkg, apk = tv["package"], tv.get("apk")
    installed = is_installed(adb, pkg)
    have_apk = bool(apk) and os.path.isfile(apk)
    if not have_apk:
        if installed:
            rep.ok.append(f"{pkg} installed")
            rep.warnings.append(f"APK {apk} not found - can't check for upgrades")
        else:
            rep.failures.append(f"{pkg} not installed and APK {apk} not found")
        return False
    digest = sha256(apk)
    # The watchdog (upgrade=False) only reinstalls a missing app. Provision
    # reinstalls when the APK differs from the last one it installed - including
    # an app put on by hand that tv-fleet never recorded (-r keeps its prefs).
    if installed and not force and (not upgrade or tv_state.get("apk_sha256") == digest):
        rep.ok.append(f"{pkg} installed")
        return False
    adb.install(apk)
    if not is_installed(adb, pkg):
        rep.failures.append(f"install reported success but {pkg} is still missing")
        return False
    tv_state["apk_sha256"] = digest
    rep.changed.append(f"{'upgraded' if installed else 'installed'} {pkg} from {apk} ({digest[:12]})")
    return not installed


def step_overlay(adb: Adb, tv: dict, rep: Report) -> None:
    pkg = tv["package"]
    if parse_appop(adb.shell(f"appops get {pkg} SYSTEM_ALERT_WINDOW")) == "allow":
        rep.ok.append("overlay (SYSTEM_ALERT_WINDOW) allowed")
        return
    adb.shell(f"appops set {pkg} SYSTEM_ALERT_WINDOW allow")
    if parse_appop(adb.shell(f"appops get {pkg} SYSTEM_ALERT_WINDOW")) == "allow":
        rep.changed.append("granted overlay (SYSTEM_ALERT_WINDOW)")
    else:
        rep.failures.append("overlay grant did not stick")


def step_settings(adb: Adb, tv: dict, rep: Report) -> None:
    for ns, key, want in desired_settings(tv):
        have = adb.shell(f"settings get {ns} {key}").strip()
        if have == want:
            rep.ok.append(f"{ns} {key}={want}")
            continue
        adb.shell(f"settings put {ns} {key} {shlex.quote(want)}")
        after = adb.shell(f"settings get {ns} {key}").strip()
        if after == want:
            rep.changed.append(f"{ns} {key}: {have} -> {want}")
        else:
            rep.failures.append(f"{ns} {key} won't stick (wanted {want}, reads {after})")
    sleep = adb.shell("settings get secure sleep_timeout").strip()
    want_ms = int(tv["screensaver_timeout_min"]) * 60_000
    if sleep.isdigit() and 0 < int(sleep) <= want_ms:
        rep.warnings.append(
            f"secure sleep_timeout={sleep} ms <= screensaver start {want_ms} ms - "
            "the TV will sleep before the screensaver starts"
        )


def config_intent(tv: dict, secrets: dict) -> tuple[str, bool]:
    """The CONTRACT.md provisioning intent as one adb shell string.

    Returns (command, has_token). Values are shell-quoted for the TV's sh.
    Without NTFY_TV_TOKEN the extra is omitted, which per the contract keeps
    whatever token the app already stored.
    """
    extras = [
        ("dashboard_url", tv["dashboard_url"]),
        ("room", tv["room"]),
        ("ntfy_url", tv["ntfy_url"]),
        ("ntfy_topics", ",".join(tv["topics"])),
    ]
    token = secrets.get("NTFY_TV_TOKEN")
    if token:
        extras.append(("ntfy_token", token))
    parts = ["am", "start", "-n", f"{tv['package']}/.MainActivity"]
    for k, v in extras:
        parts += ["--es", k, shlex.quote(str(v))]
    return " ".join(parts), bool(token)


def step_push_config(adb: Adb, tv: dict, secrets: dict, rep: Report, *, go_home: bool) -> None:
    cmd, has_token = config_intent(tv, secrets)
    out = adb.shell(cmd)
    if "Error" in out:
        rep.failures.append(f"config intent rejected: {out.strip()[-200:]}")
        return
    if go_home:
        time.sleep(3)
        adb.shell("input keyevent KEYCODE_HOME")
    rep.changed.append(f"pushed config (room={tv['room']}, topics={','.join(tv['topics'])})")
    if not has_token:
        rep.warnings.append("NTFY_TV_TOKEN unset - token extra omitted (the app keeps any stored one)")


def converge(adb: Adb, tv: dict, secrets: dict, tv_state: dict, *, mode: str, force_install: bool = False) -> Report:
    """mode: 'provision' (upgrade + always push config) or 'check' (watchdog)."""
    rep = Report(tv["name"])
    try:
        adb.connect()
    except Unreachable as exc:
        rep.skipped = str(exc)
        return rep
    except Unauthorized as exc:
        rep.failures.append(str(exc))
        return rep
    try:
        fresh = step_install(adb, tv, rep, tv_state, upgrade=(mode == "provision"), force=force_install)
        if not is_installed(adb, tv["package"]):
            return rep  # nothing below works without the app
        step_overlay(adb, tv, rep)
        step_settings(adb, tv, rep)
        if mode == "provision":
            step_push_config(adb, tv, secrets, rep, go_home=False)
            tv_state.pop("pending_config", None)
        elif fresh or tv_state.get("pending_config"):
            # The app came back with empty prefs. The config intent launches it
            # full-screen, so only do it while nobody is watching anything.
            fg, _ = parse_focus(adb.shell("dumpsys window | grep mCurrentFocus"))
            if fg in IDLE_PACKAGES or fg == tv["package"]:
                step_push_config(adb, tv, secrets, rep, go_home=True)
                tv_state.pop("pending_config", None)
            else:
                tv_state["pending_config"] = True
                rep.warnings.append(f"config push deferred - {fg or 'unknown app'} is on screen")
    except Unreachable as exc:
        rep.skipped = f"went away mid-run: {exc}"
    except AdbError as exc:
        rep.failures.append(str(exc))
    return rep


# --------------------------------------------------------------------------- state + ntfy

def load_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"tvs": {}}


def save_state(path: str, state: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(p)


def post_phone(url: str | None, title: str, message: str, priority: str, tags: str) -> None:
    """Watchdog reports -> the phone's ntfy.sh topic (same one HA and Kuma use)."""
    print(f"[ntfy {priority}] {title}: {message}")
    if not url:
        print("  (NTFY_PHONE_URL unset - not sent)")
        return
    req = urllib.request.Request(
        url, data=message.encode(), method="POST",
        headers={"Title": title, "Priority": priority, "Tags": tags},
    )
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except OSError as exc:
        print(f"  ntfy post failed: {exc}", file=sys.stderr)


def record_check(rep: Report, tv_state: dict, now: float) -> list[tuple[str, str, str]]:
    """Update the per-TV counters; return the phone notifications to send.

    Unreachable never notifies - a TV that's off is normal. Failures notify
    once per distinct failure set (no 10-minute spam), repairs always notify
    (Fire OS resetting something is worth knowing), recovery notifies once.
    """
    out = []
    if rep.skipped:
        tv_state["consecutive_unreachable"] = tv_state.get("consecutive_unreachable", 0) + 1
        tv_state["last_unreachable"] = rep.skipped
        return out
    tv_state["consecutive_unreachable"] = 0
    tv_state["last_seen"] = now
    if rep.failures:
        tv_state["consecutive_failures"] = tv_state.get("consecutive_failures", 0) + 1
        sig = "; ".join(rep.failures)
        if tv_state.get("alerted") != sig:
            tv_state["alerted"] = sig
            out.append((f"TV {rep.name}: watchdog failure", sig, "low"))
    else:
        tv_state["consecutive_failures"] = 0
        if tv_state.pop("alerted", None):
            out.append((f"TV {rep.name}: recovered", "All Ladybird checks pass again.", "min"))
    if rep.changed:
        out.append((f"TV {rep.name}: re-applied", "; ".join(rep.changed), "min"))
    return out


def publish_ntfy(base: str, token: str | None, topic: str, message: str, *,
                 title: str | None = None, priority: int = 3, attach: str | None = None,
                 tags: list[str] | None = None) -> None:
    body = {"topic": topic, "message": message, "priority": int(priority)}
    if title:
        body["title"] = title
    if attach:
        body["attach"] = attach
    if tags:
        body["tags"] = tags
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(base.rstrip("/") + "/", data=json.dumps(body).encode(),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()


# --------------------------------------------------------------------------- status / MQTT

def mqtt_id(tv: dict) -> str:
    return tv["name"].replace("-", "_")


def discovery_messages(tv: dict) -> list[tuple[str, str]]:
    """HA MQTT discovery configs -> sensor.tv_<name>_app, binary_sensor.tv_<name>_playing.

    Entity ids come from HA's device-name + entity-name rule ("TV living" +
    "App"), so no object_id / default_entity_id key is needed (object_id is
    deprecated in recent HA).
    """
    mid = mqtt_id(tv)
    base = f"ladybird/tv/{tv['name']}"
    device = {
        "identifiers": [f"ladybird_tv_{mid}"],
        "name": f"TV {tv['name']}",
        "manufacturer": "Amazon",
        "model": tv.get("model") or "Fire TV",
        "suggested_area": tv["room"],
    }
    common = {
        "state_topic": f"{base}/state",
        "availability_topic": f"{base}/availability",
        "device": device,
    }
    app = {
        **common,
        "name": "App",
        "unique_id": f"ladybird_tv_{mid}_app",
        "icon": "mdi:television-play",
        "value_template": "{{ value_json.app }}",
        "json_attributes_topic": f"{base}/state",
    }
    playing = {
        **common,
        "name": "Playing",
        "unique_id": f"ladybird_tv_{mid}_playing",
        "icon": "mdi:play-circle-outline",
        "value_template": "{{ 'ON' if value_json.playing else 'OFF' }}",
        "json_attributes_topic": f"{base}/state",
    }
    return [
        (f"homeassistant/sensor/ladybird_tv_{mid}/app/config", json.dumps(app)),
        (f"homeassistant/binary_sensor/ladybird_tv_{mid}/playing/config", json.dumps(playing)),
    ]


def read_status(adb: Adb, tv: dict) -> dict:
    fg, act = parse_focus(adb.shell("dumpsys window | grep mCurrentFocus"))
    sessions = parse_media_sessions(adb.shell(
        "dumpsys media_session | grep -E '^ *(package=|active=|state=PlaybackState)'"))
    playing_pkg = playing_session(sessions, fg)
    names = tv.get("app_names") or {}
    return {
        "app": names.get(fg, fg or "unknown"),
        "package": fg,
        "activity": act,
        "playing": playing_pkg is not None,
        "playing_package": playing_pkg,
    }


def status_round(tvs: list[dict], publish, runner=subprocess.run, quiet: bool = False) -> None:
    msgs = []
    for tv in tvs:
        base = f"ladybird/tv/{tv['name']}"
        adb = Adb(tv["serial"], runner)
        try:
            adb.connect()
            st = read_status(adb, tv)
        except AdbError as exc:
            msgs.append((f"{base}/availability", "offline"))
            if not quiet:
                print(f"{tv['name']}: offline ({exc})")
            continue
        msgs.append((f"{base}/availability", "online"))
        msgs.append((f"{base}/state", json.dumps(st)))
        if not quiet:
            print(f"{tv['name']}: {st['app']} playing={st['playing']}")
    publish(msgs)


def mqtt_publisher(host: str, port: int):
    import paho.mqtt.publish as mqtt_publish  # python3-paho-mqtt (base role)

    def publish(msgs):
        if msgs:
            mqtt_publish.multiple(
                [{"topic": t, "payload": p, "retain": True} for t, p in msgs],
                hostname=host, port=int(port), client_id="tv-fleet",
            )
    return publish


# --------------------------------------------------------------------------- CLI

def cmd_converge(args, mode: str) -> int:
    _, tvs = load_inventory(args.inventory)
    secrets = load_secrets(args.env)
    state = load_state(args.state)
    rc = 0
    for tv in select(tvs, args.name):
        tv_state = state["tvs"].setdefault(tv["name"], {})
        rep = converge(Adb(tv["serial"]), tv, secrets, tv_state, mode=mode,
                       force_install=getattr(args, "force_install", False))
        print(rep.render())
        if mode == "check":
            for title, msg, prio in record_check(rep, tv_state, time.time()):
                post_phone(secrets.get("NTFY_PHONE_URL"), title, msg, prio, "tv")
            if rep.skipped:
                print(f"   (unreachable {tv_state['consecutive_unreachable']} run(s) in a row - not alerting)")
        if rep.failures:
            rc = 1
        if mode == "provision" and rep.skipped:
            rc = 1  # provisioning an unreachable TV is a failure for the operator
    save_state(args.state, state)
    return rc


def cmd_status(args) -> int:
    defaults, tvs = load_inventory(args.inventory)
    tvs = select(tvs, args.name)
    publish = (lambda msgs: [print(f"  {t} {p}") for t, p in msgs]) if args.dry_run \
        else mqtt_publisher(defaults["mqtt_host"], defaults["mqtt_port"])
    publish([m for tv in tvs for m in discovery_messages(tv)])
    while True:
        status_round(tvs, publish, quiet=bool(args.loop))
        if not args.loop:
            return 0
        time.sleep(args.loop)


def cmd_notify(args) -> int:
    defaults, tvs = load_inventory(args.inventory)
    secrets = load_secrets(args.env)
    topic = args.topic
    if topic.startswith("tv:"):
        tv = select(tvs, topic[3:])[0]
        topic = f"ladybird-tv-{tv['room']}"
    elif topic == "house":
        topic = defaults["house_topic"]
    base = defaults.get("ntfy_url") or "http://127.0.0.1:8090"
    publish_ntfy(base, secrets.get("NTFY_PUBLISH_TOKEN"), topic, args.message,
                 title=args.title, priority=args.priority, attach=args.attach)
    print(f"sent priority {args.priority} to {topic} via {base}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--inventory", default=DEFAULT_INVENTORY)
    ap.add_argument("--env", default=DEFAULT_ENV, help="secrets file (KEY=VALUE)")
    ap.add_argument("--state", default=DEFAULT_STATE)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("provision", help="install + configure one TV or all")
    p.add_argument("name", nargs="?")
    p.add_argument("--force-install", action="store_true", help="reinstall even if the APK is unchanged")

    p = sub.add_parser("check", help="watchdog: verify and re-apply, report drift")
    p.add_argument("name", nargs="?")

    p = sub.add_parser("status", help="publish foreground app + playing to MQTT")
    p.add_argument("name", nargs="?")
    p.add_argument("--loop", type=int, default=0, metavar="SECONDS")
    p.add_argument("--dry-run", action="store_true", help="print MQTT messages instead of publishing")

    p = sub.add_parser("notify", help="send a test banner via the LAN ntfy")
    p.add_argument("topic", help="a topic, 'house', or 'tv:<name>'")
    p.add_argument("message")
    p.add_argument("--priority", type=int, default=3, choices=range(1, 6))
    p.add_argument("--title", default="Test")
    p.add_argument("--attach", help="image URL shown in the banner")

    args = ap.parse_args(argv)
    if getattr(os, "geteuid", lambda: 1)() == 0 and args.cmd != "notify":
        print("refusing to run as root: root has a different adb key, every TV would "
              "re-prompt. Run as the admin user.", file=sys.stderr)
        return 2
    if args.cmd == "provision":
        return cmd_converge(args, "provision")
    if args.cmd == "check":
        return cmd_converge(args, "check")
    if args.cmd == "status":
        return cmd_status(args)
    return cmd_notify(args)


if __name__ == "__main__":
    sys.exit(main())
