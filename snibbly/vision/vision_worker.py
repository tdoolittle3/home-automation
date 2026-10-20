#!/usr/bin/env python3
"""Vision pipeline ingest worker (Phase 1) — see docs/vision.md.

Two ingest paths feed one SQLite database:

  * fast path — MQTT `frigate/events` on ladybird; every ended event's
    full-resolution clip is fetched from Frigate's API and sampled at a few fps.
  * sweep path — the continuous recordings (full quality, 2-day retention) are
    pulled in chunks and sampled at 1 fps, independent of Frigate's opinion of
    what moved. The cameras record a 1-second GOP, so 1 fps sampling is
    keyframe-only extraction (`-skip_frame nokey`) — no full decode.

Phase 1 stops at frames: event frames are kept on disk and recorded in the DB;
sweep frames are counted and discarded (`[sweep] keep_frames = false`) because
without the Phase-2 detector, keeping them would be ~17 GB/day of mostly-empty
driveway. Phase 2 replaces the sweep stub with detection, keeping only frames
that contain something.

Deployed to /opt/vision/vision_worker.py, run by the `vision-worker` systemd
unit as user `vision`, config at /etc/vision/config.toml. ffmpeg reads clip
URLs directly — nothing is buffered to disk. Health/status JSON on :8098.

Deps beyond stdlib: paho-mqtt >= 2 (Phase 2 adds the torch stack to the same
venv; see docs/vision.md for the uv incantation).
"""

import json
import logging
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CONFIG_PATH = os.environ.get("VISION_CONFIG", "/etc/vision/config.toml")

DEFAULTS = {
    "frigate": {"base_url": "http://192.168.0.13:5000", "cameras": ["driveway", "backyard"]},
    "mqtt": {"host": "192.168.0.13", "port": 1883, "topic": "frigate/events"},
    "http": {"bind": "0.0.0.0", "port": 8098},
    "storage": {"data_dir": "/opt/vision/data",
                "event_frames_retain_days": 30, "sweep_frames_retain_days": 7},
    "events": {"fps": 2.0, "clip_wait_secs": 15},
    "sweep": {"enabled": True, "chunk_minutes": 20, "lag_minutes": 10,
              "start_offset_hours": 6, "keep_frames": False},
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS events(
  id TEXT PRIMARY KEY,
  source TEXT NOT NULL,              -- 'frigate' | 'sweep' (sweep events arrive in phase 2)
  camera TEXT NOT NULL,
  label TEXT,
  start_ts REAL,
  end_ts REAL,
  top_score REAL,
  status TEXT NOT NULL DEFAULT 'pending',   -- pending | sampled | error
  error TEXT,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS frames(
  id INTEGER PRIMARY KEY,
  event_id TEXT REFERENCES events(id),      -- NULL for kept sweep frames
  camera TEXT NOT NULL,
  ts REAL NOT NULL,
  path TEXT NOT NULL,
  analyzed INTEGER NOT NULL DEFAULT 0       -- phase 2 flips this
);
CREATE INDEX IF NOT EXISTS frames_event ON frames(event_id);
CREATE TABLE IF NOT EXISTS jobs(
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,                       -- 'event' | 'sweep'
  payload TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'queued',     -- queued | running | done | error
  attempts INTEGER NOT NULL DEFAULT 0,
  not_before REAL NOT NULL DEFAULT 0,
  error TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state, not_before);
CREATE TABLE IF NOT EXISTS sweep_progress(
  camera TEXT PRIMARY KEY,
  swept_until REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS counters(
  key TEXT PRIMARY KEY,
  value REAL NOT NULL
);
"""

RETRY_BACKOFF = [15, 60, 180, 600, 1800]  # seconds; len() == max attempts - 1

log = logging.getLogger("vision")
shutdown = threading.Event()


def load_config():
    cfg = {k: dict(v) for k, v in DEFAULTS.items()}
    with open(CONFIG_PATH, "rb") as f:
        for section, values in tomllib.load(f).items():
            cfg.setdefault(section, {}).update(values)
    return cfg


class DB:
    """One shared connection, writes serialized by a lock — traffic is tiny."""

    def __init__(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("PRAGMA journal_mode=WAL;" + SCHEMA)
        self.lock = threading.Lock()

    def execute(self, sql, params=()):
        with self.lock, self.conn:
            return self.conn.execute(sql, params)

    def query(self, sql, params=()):
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def bump(self, key, n=1):
        self.execute("INSERT INTO counters(key, value) VALUES(?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = value + ?", (key, n, n))


def enqueue(db, kind, payload, not_before=0.0):
    now = time.time()
    db.execute("INSERT INTO jobs(kind, payload, not_before, created_at, updated_at) "
               "VALUES(?,?,?,?,?)", (kind, json.dumps(payload), not_before, now, now))


# ---- Frigate API ----

def api_json(cfg, path, timeout=10):
    with urllib.request.urlopen(cfg["frigate"]["base_url"] + path, timeout=timeout) as r:
        return json.load(r)


def sample_clip(url, out_dir, fps=None, keyframes_only=False, timeout=900):
    """ffmpeg samples frames straight from the URL into out_dir. Returns count."""
    os.makedirs(out_dir, exist_ok=True)
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error"]
    if keyframes_only:
        cmd += ["-skip_frame", "nokey", "-i", url, "-fps_mode", "passthrough"]
    else:
        cmd += ["-i", url, "-vf", f"fps={fps}"]
    cmd += ["-q:v", "4", os.path.join(out_dir, "%06d.jpg")]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg: {proc.stderr.strip()[:300]}")
    return len(os.listdir(out_dir))


class Retry(Exception):
    """Job failed in a way that time may fix."""


# ---- job handlers ----

def handle_event(cfg, db, p):
    event_id, camera = p["id"], p["camera"]
    try:
        meta = api_json(cfg, f"/api/events/{event_id}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise Retry(f"event {event_id} not in Frigate yet")
        raise
    if meta.get("has_clip"):
        url = f"{cfg['frigate']['base_url']}/api/events/{event_id}/clip.mp4"
        keyframes = False
    else:
        # detect-only event, or clip not assembled: sample the recordings range
        start = p.get("start_ts") or time.time()
        end = p.get("end_ts") or start + 10
        if time.time() - end < 120:
            raise Retry(f"event {event_id} has no clip yet")
        url = (f"{cfg['frigate']['base_url']}/api/{camera}"
               f"/start/{start:.3f}/end/{end:.3f}/clip.mp4")
        keyframes = True
    out_dir = os.path.join(cfg["storage"]["data_dir"], "frames", "events",
                           camera, event_id)
    n = sample_clip(url, out_dir, fps=cfg["events"]["fps"], keyframes_only=keyframes)
    if n == 0:
        shutil.rmtree(out_dir, ignore_errors=True)
        raise Retry(f"event {event_id}: clip produced 0 frames")
    start_ts = p.get("start_ts") or time.time()
    step = 1.0 if keyframes else 1.0 / cfg["events"]["fps"]
    with db.lock, db.conn:
        for i, name in enumerate(sorted(os.listdir(out_dir))):
            db.conn.execute(
                "INSERT INTO frames(event_id, camera, ts, path) VALUES(?,?,?,?)",
                (event_id, camera, start_ts + i * step, os.path.join(out_dir, name)))
        db.conn.execute("UPDATE events SET status='sampled' WHERE id=?", (event_id,))
    db.bump("event_frames_kept", n)
    log.info("event %s (%s/%s): %d frames", event_id, camera, p.get("label"), n)


def handle_sweep(cfg, db, p):
    camera, start, end = p["camera"], p["start"], p["end"]
    url = (f"{cfg['frigate']['base_url']}/api/{camera}"
           f"/start/{start:.0f}/end/{end:.0f}/clip.mp4")
    keep = cfg["sweep"]["keep_frames"]
    data_dir = cfg["storage"]["data_dir"]
    if keep:
        stamp = time.strftime("%Y%m%d-%H%M", time.localtime(start))
        out_dir = os.path.join(data_dir, "frames", "sweep", camera, stamp)
        tmp_ctx = None
    else:
        tmp_ctx = tempfile.TemporaryDirectory(dir=os.path.join(data_dir, "tmp"))
        out_dir = tmp_ctx.name
    try:
        try:
            n = sample_clip(url, out_dir, keyframes_only=True)
        except RuntimeError as e:
            # no recordings in this span (camera offline, or pre-retention)
            if "404" in str(e) or "Not Found" in str(e):
                n = 0
            else:
                raise
        if keep and n:
            with db.lock, db.conn:
                for i, name in enumerate(sorted(os.listdir(out_dir))):
                    db.conn.execute(
                        "INSERT INTO frames(camera, ts, path) VALUES(?,?,?)",
                        (camera, start + i, os.path.join(out_dir, name)))
        db.bump("sweep_frames_seen", n)
        db.bump("sweep_chunks_done")
        log.info("sweep %s %s..%s: %d frames%s", camera,
                 time.strftime("%H:%M", time.localtime(start)),
                 time.strftime("%H:%M", time.localtime(end)), n,
                 "" if keep else " (discarded — phase 1 stub)")
    finally:
        if tmp_ctx:
            tmp_ctx.cleanup()
        elif keep and not os.listdir(out_dir):
            os.rmdir(out_dir)


def job_runner(cfg, db):
    handlers = {"event": handle_event, "sweep": handle_sweep}
    while not shutdown.is_set():
        rows = db.query(
            "SELECT * FROM jobs WHERE state='queued' AND not_before <= ? "
            "ORDER BY kind='event' DESC, created_at LIMIT 1", (time.time(),))
        if not rows:
            shutdown.wait(5)
            continue
        job = rows[0]
        db.execute("UPDATE jobs SET state='running', updated_at=? WHERE id=?",
                   (time.time(), job["id"]))
        payload = json.loads(job["payload"])
        try:
            handlers[job["kind"]](cfg, db, payload)
            db.execute("UPDATE jobs SET state='done', updated_at=? WHERE id=?",
                       (time.time(), job["id"]))
        except Exception as e:
            attempts = job["attempts"] + 1
            if isinstance(e, Retry) and attempts <= len(RETRY_BACKOFF):
                delay = RETRY_BACKOFF[attempts - 1]
                log.info("job %d retry %d in %ds: %s", job["id"], attempts, delay, e)
                db.execute("UPDATE jobs SET state='queued', attempts=?, not_before=?, "
                           "updated_at=? WHERE id=?",
                           (attempts, time.time() + delay, time.time(), job["id"]))
            else:
                log.error("job %d (%s) failed permanently: %s", job["id"], job["kind"], e)
                db.execute("UPDATE jobs SET state='error', attempts=?, error=?, "
                           "updated_at=? WHERE id=?",
                           (attempts, str(e)[:500], time.time(), job["id"]))
                if job["kind"] == "event":
                    db.execute("UPDATE events SET status='error', error=? WHERE id=?",
                               (str(e)[:500], payload.get("id")))


# ---- MQTT fast path ----

def start_mqtt(cfg, db, state):
    import paho.mqtt.client as mqtt

    def on_connect(client, userdata, flags, reason_code, properties):
        state["mqtt_connected"] = not reason_code.is_failure
        log.info("mqtt connect: %s", reason_code)
        client.subscribe(cfg["mqtt"]["topic"])

    def on_disconnect(client, userdata, flags, reason_code, properties):
        state["mqtt_connected"] = False
        log.warning("mqtt disconnected: %s", reason_code)

    def on_message(client, userdata, msg):
        try:
            payload = json.loads(msg.payload)
        except json.JSONDecodeError:
            return
        if payload.get("type") != "end":
            return
        after = payload.get("after") or {}
        event_id = after.get("id")
        if not event_id:
            return
        if db.query("SELECT 1 FROM events WHERE id=? AND status='sampled'", (event_id,)):
            return  # duplicate end message for an event we already have
        top = after.get("top_score")
        if top is None:
            top = (after.get("data") or {}).get("top_score")
        db.execute(
            "INSERT OR REPLACE INTO events"
            "(id, source, camera, label, start_ts, end_ts, top_score, created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (event_id, "frigate", after.get("camera"), after.get("label"),
             after.get("start_time"), after.get("end_time"), top, time.time()))
        enqueue(db, "event",
                {"id": event_id, "camera": after.get("camera"),
                 "label": after.get("label"), "start_ts": after.get("start_time"),
                 "end_ts": after.get("end_time")},
                not_before=time.time() + cfg["events"]["clip_wait_secs"])
        db.bump("mqtt_events_seen")
        log.info("mqtt: event %s ended (%s/%s)", event_id,
                 after.get("camera"), after.get("label"))

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="vision-worker")
    client.on_connect, client.on_disconnect, client.on_message = \
        on_connect, on_disconnect, on_message
    client.reconnect_delay_set(min_delay=1, max_delay=60)
    client.connect_async(cfg["mqtt"]["host"], cfg["mqtt"]["port"], keepalive=60)
    client.loop_start()  # paho owns its thread and reconnects forever
    return client


# ---- sweep scheduler + retention ----

def sweep_scheduler(cfg, db):
    chunk = cfg["sweep"]["chunk_minutes"] * 60
    lag = cfg["sweep"]["lag_minutes"] * 60
    while not shutdown.is_set():
        for camera in cfg["frigate"]["cameras"]:
            rows = db.query("SELECT swept_until FROM sweep_progress WHERE camera=?",
                            (camera,))
            until = (rows[0]["swept_until"] if rows
                     else time.time() - cfg["sweep"]["start_offset_hours"] * 3600)
            while until + chunk <= time.time() - lag and not shutdown.is_set():
                enqueue(db, "sweep", {"camera": camera, "start": until,
                                      "end": until + chunk})
                until += chunk
                db.execute("INSERT OR REPLACE INTO sweep_progress VALUES(?,?)",
                           (camera, until))
        retention(cfg, db)
        shutdown.wait(60)


def retention(cfg, db):
    rows = db.query("SELECT value FROM counters WHERE key='last_retention'")
    if rows and time.time() - rows[0]["value"] < 86400:
        return
    db.execute("INSERT OR REPLACE INTO counters VALUES('last_retention', ?)",
               (time.time(),))
    for event_id, cutoff_days in ((True, cfg["storage"]["event_frames_retain_days"]),
                                  (False, cfg["storage"]["sweep_frames_retain_days"])):
        cutoff = time.time() - cutoff_days * 86400
        cond = "event_id IS NOT NULL" if event_id else "event_id IS NULL"
        old = db.query(f"SELECT id, path FROM frames WHERE {cond} AND ts < ?", (cutoff,))
        for row in old:
            try:
                os.unlink(row["path"])
            except FileNotFoundError:
                pass
            db.execute("DELETE FROM frames WHERE id=?", (row["id"],))
        if old:
            log.info("retention: dropped %d %s frames", len(old),
                     "event" if event_id else "sweep")
    db.execute("DELETE FROM jobs WHERE state='done' AND updated_at < ?",
               (time.time() - 7 * 86400,))


# ---- health endpoint ----

def start_health(cfg, db, state):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            jobs = {r["state"]: r["n"] for r in db.query(
                "SELECT state, COUNT(*) n FROM jobs GROUP BY state")}
            sweep = {r["camera"]: round(time.time() - r["swept_until"])
                     for r in db.query("SELECT * FROM sweep_progress")}
            counters = {r["key"]: r["value"] for r in db.query("SELECT * FROM counters")}
            body = json.dumps({
                "ok": state["mqtt_connected"] and jobs.get("queued", 0) < 500,
                "mqtt_connected": state["mqtt_connected"],
                "uptime_secs": round(time.time() - state["started"]),
                "jobs": jobs,
                "sweep_lag_secs": sweep,
                "counters": counters,
            }, indent=2).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer((cfg["http"]["bind"], cfg["http"]["port"]), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config()
    data_dir = cfg["storage"]["data_dir"]
    os.makedirs(os.path.join(data_dir, "tmp"), exist_ok=True)
    db = DB(os.path.join(data_dir, "vision.db"))
    db.execute("UPDATE jobs SET state='queued' WHERE state='running'")  # crash recovery

    state = {"mqtt_connected": False, "started": time.time()}
    start_health(cfg, db, state)
    mqtt_client = start_mqtt(cfg, db, state)
    threads = [threading.Thread(target=job_runner, args=(cfg, db), daemon=True),
               threading.Thread(target=sweep_scheduler, args=(cfg, db), daemon=True)]
    if not cfg["sweep"]["enabled"]:
        threads.pop()
    for t in threads:
        t.start()
    log.info("vision-worker up: sweep=%s, health on :%d",
             cfg["sweep"]["enabled"], cfg["http"]["port"])

    signal.signal(signal.SIGTERM, lambda *a: shutdown.set())
    signal.signal(signal.SIGINT, lambda *a: shutdown.set())
    shutdown.wait()
    mqtt_client.loop_stop()
    log.info("vision-worker stopped")


if __name__ == "__main__":
    main()
