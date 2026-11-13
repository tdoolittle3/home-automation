"""Tests for the vision pipeline's pure logic — see docs/vision.md.

Covers _iou/_static_mask, the cross-batch static-object registry, sweep
event clustering, and the webapp routes, all against in-memory/throwaway
SQLite built from vision_worker.SCHEMA. No GPU, ffmpeg, or MQTT.

Deps: pytest, numpy, fastapi, jinja2, python-multipart, httpx, mcp>=2
(the vision-mcp test skips without it).
"""

import json
import os
import sqlite3
import sys
import tempfile
import threading

import pytest

# webapp calls load_config() at import, reading the path vision_worker bound
# from VISION_CONFIG at *its* import — point both at a throwaway config first.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_TMP = tempfile.mkdtemp(prefix="vision-test-")
with open(os.path.join(_TMP, "config.toml"), "w", newline="\n") as f:
    f.write(f'[storage]\ndata_dir = "{_TMP.replace(os.sep, "/")}"\n')
os.environ["VISION_CONFIG"] = os.path.join(_TMP, "config.toml")

import vision_worker
from analyzer import Analyzer, _iou, _static_mask

CAR = [100, 100, 200, 200]


class MemDB(vision_worker.DB):
    """vision_worker.DB on :memory: — skips the real constructor's mkdir/WAL."""

    def __init__(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(vision_worker.SCHEMA)
        self.lock = threading.Lock()


@pytest.fixture
def ana(tmp_path):
    # defaults: static_registry_iou 0.5, static_presence 0.5, expire 3, gap 90
    cfg = {"analyze": dict(vision_worker.DEFAULTS["analyze"]),
           "storage": {"data_dir": str(tmp_path)}}
    return Analyzer(cfg, MemDB())


# ---- _iou ----

def test_iou_identical_boxes():
    assert _iou(CAR, CAR) == 1.0


def test_iou_disjoint_boxes():
    assert _iou([0, 0, 10, 10], [20, 20, 30, 30]) == 0.0


def test_iou_partial_overlap():
    # 50 px² shared of 150 px² union
    assert _iou([0, 0, 10, 10], [5, 0, 15, 10]) == pytest.approx(1 / 3)


# ---- _static_mask ----

def test_static_mask_suppresses_persistent_object():
    # car parked in 12 of 20 frames (60% ≥ 50%); person walks through frame 5
    per_frame = [[("car", 0.9, CAR)] if i < 12 else [] for i in range(20)]
    per_frame[5] = [("car", 0.9, CAR), ("person", 0.8, [300, 300, 350, 400])]
    masks, clusters = _static_mask(per_frame, 0.5)
    assert clusters == [("car", CAR)]
    assert all(0 in masks[i] for i in range(12))  # masked wherever it appears
    assert 1 not in masks[5]                      # the transient stays live
    assert all(not m for m in masks[12:])


def test_static_mask_short_batch_untouched():
    masks, clusters = _static_mask([[("car", 0.9, CAR)] for _ in range(5)], 0.5)
    assert all(m == set() for m in masks) and clusters == []


# ---- static-object registry ----

def test_registry_learn_inserts_then_refreshes(ana):
    ana._registry_learn("driveway", [("car", CAR)], ts=1000)
    drifted = [104, 100, 204, 200]  # IoU ~0.92 — same object, sun moved
    ana._registry_learn("driveway", [("car", drifted)], ts=2000)
    rows = ana.db.query("SELECT * FROM static_objects")
    assert len(rows) == 1
    assert json.loads(rows[0]["box"]) == drifted
    assert rows[0]["last_seen"] == 2000


def test_registry_filter_suppresses_matches_and_passes_rest(ana):
    (sid,) = ana._registry_learn("driveway", [("car", CAR)], ts=1000)
    ana.db.execute("UPDATE static_objects SET misses=2 WHERE id=?", (sid,))
    person = ("person", 0.8, [300, 300, 350, 400])
    latest = [106, 100, 206, 200]
    per_frame = [[("car", 0.9, [104, 100, 204, 200]), person],
                 [("car", 0.9, latest)]]
    out, matched = ana._registry_filter("driveway", per_frame, [1100, 1101])
    assert out == [[person], []]
    assert matched == {sid}
    row = ana.db.query("SELECT * FROM static_objects")[0]
    assert json.loads(row["box"]) == latest    # refreshed to the latest match
    assert row["last_seen"] == 1101
    assert row["hits"] == 2 and row["misses"] == 0
    assert ana.db.query("SELECT value FROM counters WHERE key='static_suppressed'"
                        )[0]["value"] == 2


def test_registry_per_camera_isolation(ana):
    ana._registry_learn("driveway", [("car", CAR)], ts=1000)
    out, matched = ana._registry_filter("backyard", [[("car", 0.9, CAR)]], [1100])
    assert out == [[("car", 0.9, CAR)]] and matched == set()


def test_registry_moved_box_not_suppressed(ana):
    ana._registry_learn("driveway", [("car", CAR)], ts=1000)
    moved = ("car", 0.9, [400, 100, 500, 200])  # IoU 0 — the departure event
    out, matched = ana._registry_filter("driveway", [[moved]], [1100])
    assert out == [[moved]] and matched == set()


def test_registry_expire_deletes_after_consecutive_misses(ana):
    ana._registry_learn("driveway", [("car", CAR)], ts=1000)
    ana._registry_expire("driveway", set())
    ana._registry_expire("driveway", set())
    assert ana.db.query("SELECT misses FROM static_objects")[0]["misses"] == 2
    ana._registry_expire("driveway", set())  # third strike (static_expire_chunks=3)
    assert ana.db.query("SELECT * FROM static_objects") == []


def test_registry_expire_skips_active_entry(ana):
    (sid,) = ana._registry_learn("driveway", [("car", CAR)], ts=1000)
    for _ in range(4):
        ana._registry_expire("driveway", {sid})
    assert ana.db.query("SELECT misses FROM static_objects")[0]["misses"] == 0


# ---- sweep event clustering ----

def test_cluster_sweep_events_splits_on_time_gap(ana):
    db, kept = ana.db, []
    for ts, label in ((1000, "person"), (1010, "person"), (1020, "person"),
                      (1200, "car")):  # 180s gap > 90s default
        fid = db.execute("INSERT INTO frames(camera, ts, path, analyzed) "
                         "VALUES('driveway', ?, 'x.jpg', 1)", (ts,)).lastrowid
        db.execute("INSERT INTO detections(frame_id, camera, ts, label, conf, box)"
                   " VALUES(?, 'driveway', ?, ?, 0.9, '[0,0,10,10]')",
                   (fid, ts, label))
        kept.append({"id": fid, "ts": ts})
    assert ana._cluster_sweep_events("driveway", kept) == 2
    evs = db.query("SELECT * FROM events ORDER BY start_ts")
    assert [e["id"] for e in evs] == ["sweep-driveway-1000", "sweep-driveway-1200"]
    assert all(e["source"] == "sweep" for e in evs)
    assert (evs[0]["label"], evs[0]["start_ts"], evs[0]["end_ts"]) == ("person", 1000, 1020)
    assert db.query("SELECT COUNT(*) n FROM frames "
                    "WHERE event_id='sweep-driveway-1000'")[0]["n"] == 3
    assert db.query("SELECT COUNT(*) n FROM detections "
                    "WHERE event_id='sweep-driveway-1200'")[0]["n"] == 1


# ---- webapp ----

@pytest.fixture(scope="session")
def web():
    import webapp
    from fastapi.testclient import TestClient

    conn = sqlite3.connect(webapp.DB_PATH)
    conn.executescript(vision_worker.SCHEMA)
    for eid, label in ((1, "person"), (2, "person"), (3, "car")):
        conn.execute("INSERT INTO entities(id, label, centroid, n) VALUES(?,?,?,1)",
                     (eid, label, b"\0"))
    frames = {}
    for ev, label, t0 in (("ev1", "person", 1000), ("ev2", "car", 2000)):
        conn.execute(
            "INSERT INTO events(id, source, camera, label, start_ts, end_ts,"
            " top_score, status, created_at) "
            "VALUES(?, 'frigate', 'driveway', ?, ?, ?, 0.9, 'analyzed', ?)",
            (ev, label, t0, t0 + 10, t0))
        frames[ev] = conn.execute(
            "INSERT INTO frames(event_id, camera, ts, path, analyzed) "
            "VALUES(?, 'driveway', ?, ?, 1)",
            (ev, t0, os.path.join(webapp.DATA_DIR, "frames", "f.jpg"))).lastrowid

    def det(ev, label, entity_id):
        conn.execute(
            "INSERT INTO detections(frame_id, event_id, camera, ts, label, conf,"
            " box, entity_id) VALUES(?, ?, 'driveway', 1000, ?, 0.9,"
            " '[0,0,10,10]', ?)", (frames[ev], ev, label, entity_id))

    # ev1: 3 person dets over 2 entities + 1 unclustered car -> "person ×2 · car"
    for e in (1, 2, 1):
        det("ev1", "person", e)
    det("ev1", "car", None)
    # ev2: 10 near-identical car sightings of one entity + 1 person
    for _ in range(10):
        det("ev2", "car", 3)
    det("ev2", "person", None)
    conn.execute("INSERT INTO static_objects(camera, label, box, first_seen,"
                 " last_seen, hits, misses) "
                 "VALUES('driveway', 'car', '[10,10,50,50]', 1000, 2000, 5, 0)")
    conn.commit()
    conn.close()
    return TestClient(webapp.app)


def test_events_page_renders_composition_line(web):
    r = web.get("/")
    assert r.status_code == 200
    assert "person ×2" in r.text  # 3 person dets collapse to 2 individuals
    assert "car ×" not in r.text  # a single individual gets no count


def test_event_page_groups_near_identical_detections(web):
    r = web.get("/event/ev2")
    assert r.status_code == 200
    assert "2 distinct" in r.text and "11 total" in r.text
    assert "10×" in r.text  # the car group's sighting count, not 10 rows


def test_entities_page_lists_and_deletes_static_object(web):
    import webapp
    r = web.get("/entities")
    assert r.status_code == 200
    assert "Static right now" in r.text and "driveway" in r.text
    sid = sqlite3.connect(webapp.DB_PATH).execute(
        "SELECT id FROM static_objects").fetchone()[0]
    r = web.post(f"/static/{sid}/delete", follow_redirects=False)
    assert r.status_code == 303
    assert sqlite3.connect(webapp.DB_PATH).execute(
        "SELECT COUNT(*) FROM static_objects").fetchone()[0] == 0


# ---- frame cap + frames.path migration (2026-11-12 disk-full fix) ----

def _fake_clip(monkeypatch, meta, n_at_base):
    """Stub Frigate + ffmpeg: the clip yields duration*fps frames, or
    n_at_base frames when no fps cap is requested."""
    calls = {}

    def sample(url, out_dir, fps=None, keyframes_only=False, timeout=900):
        calls["fps"] = fps
        os.makedirs(out_dir, exist_ok=True)
        dur = (meta.get("end_time") or 0) - (meta.get("start_time") or 0)
        n = int(dur * fps) + 1 if (fps and dur) else n_at_base
        for i in range(n):
            open(os.path.join(out_dir, f"{i + 1:06d}.jpg"), "w").close()
        return n

    monkeypatch.setattr(vision_worker, "sample_clip", sample)
    monkeypatch.setattr(vision_worker, "api_json", lambda cfg, path, timeout=10: meta)
    monkeypatch.setattr(vision_worker, "enqueue", lambda *a, **k: None)
    return calls


def _event_cfg(tmp_path):
    cfg = vision_worker.load_config()
    cfg["storage"] = dict(cfg["storage"], data_dir=str(tmp_path))
    return cfg


def test_handle_event_caps_frames_over_long_event(tmp_path, monkeypatch):
    # a parked car Frigate tracked for 2 h used to become ~15k full-res JPGs
    db = MemDB()
    calls = _fake_clip(monkeypatch, {"has_clip": True, "start_time": 1000.0,
                                     "end_time": 8553.0}, 0)
    vision_worker.handle_event(_event_cfg(tmp_path), db,
                               {"id": "ev", "camera": "driveway", "start_ts": 1000.0})
    rows = db.query("SELECT ts FROM frames WHERE event_id='ev' ORDER BY ts")
    assert len(rows) == 120
    assert calls["fps"] == pytest.approx(120 / 7553)
    assert rows[0]["ts"] == 1000.0 and rows[-1]["ts"] > 8000  # spans the event
    assert len(os.listdir(tmp_path / "frames/events/driveway/ev")) == 120


def test_handle_event_short_event_keeps_full_fps(tmp_path, monkeypatch):
    db = MemDB()
    calls = _fake_clip(monkeypatch, {"has_clip": True, "start_time": 0.0,
                                     "end_time": 20.0}, 0)
    vision_worker.handle_event(_event_cfg(tmp_path), db,
                               {"id": "ev", "camera": "driveway", "start_ts": 0.0})
    assert calls["fps"] == 2.0
    assert db.query("SELECT COUNT(*) n FROM frames")[0]["n"] == 41


def test_handle_event_backstop_thins_when_duration_unknown(tmp_path, monkeypatch):
    db = MemDB()
    _fake_clip(monkeypatch, {"has_clip": True}, 5000)
    vision_worker.handle_event(_event_cfg(tmp_path), db,
                               {"id": "ev", "camera": "driveway", "start_ts": 0.0})
    rows = db.query("SELECT path FROM frames WHERE event_id='ev'")
    assert len(rows) == 120
    assert len(os.listdir(tmp_path / "frames/events/driveway/ev")) == 120
    assert all(os.path.exists(r["path"]) for r in rows)


def test_db_migrates_frames_path_to_nullable(tmp_path):
    # pre-2026-11-12 DBs had path NOT NULL, so retention's SET path=NULL threw
    path = str(tmp_path / "vision.db")
    old = sqlite3.connect(path)
    old.executescript(vision_worker.SCHEMA.replace(
        "path TEXT,", "path TEXT NOT NULL,", 1))
    old.execute("INSERT INTO frames(id, event_id, camera, ts, path) "
                "VALUES(7, 'e', 'driveway', 1, 'a.jpg')")
    old.commit()
    old.close()
    db = vision_worker.DB(path)
    notnull = [c["notnull"] for c in db.query("PRAGMA table_info(frames)")
               if c["name"] == "path"]
    assert notnull == [0]
    db.execute("UPDATE frames SET path=NULL WHERE id=7")  # retention's write
    assert db.query("SELECT event_id, path FROM frames WHERE id=7")[0]["path"] is None
    vision_worker.DB(path)  # second open: no-op, no error


# ---- label allowlist + competitive zero-shot tags ----

def _unit(v):
    import numpy as np
    v = np.asarray(v, dtype=np.float32)
    return v / np.linalg.norm(v)


def test_tag_hits_tag_must_beat_backgrounds_and_siblings():
    import numpy as np
    from analyzer import tag_hits
    e = np.eye(8, dtype=np.float32)  # orthogonal concepts: ties are exact
    truck, van = e[0], e[1]
    negs = ["bg-a", "bg-b", "bg-c"]
    neg_vecs = [e[2], e[3], e[4]]
    tags = [(1, "garbage truck", 0.2, truck), (2, "delivery van", 0.2, van)]
    crops = np.vstack([
        _unit(truck + 0.1 * e[5]),     # clearly a garbage truck
        _unit(e[2] + 0.1 * e[5]),      # background
        _unit(truck + van),            # torn between the two kinds
    ])
    hits = tag_hits(crops, tags, negs, neg_vecs)
    assert [h[0] for h in hits[0]] == [1]
    assert hits[1] == []
    assert hits[2] == []  # neither kind takes >= 0.5 of the vote


def test_tag_hits_cosine_floor_still_applies():
    import numpy as np
    from analyzer import tag_hits
    t = _unit([1, 0, 0, 0])
    crop = _unit([0.3, 1, 1, 1])  # wins the softmax (no rival near it) but cos < 0.5
    hits = tag_hits(crop, [(1, "t", 0.5, t)], ["bg"], [_unit([0, 0, 0, 1])])
    assert hits == [[]]


def test_detect_drops_labels_outside_allowlist(ana):
    class Box:
        def __init__(self, cls, xyxy):
            self.cls, self.conf = cls, 0.9
            self.xyxy = [type("T", (), {"tolist": lambda s, v=xyxy: v})()]

    class Result:
        names = {0: "person", 1: "bench"}
        boxes = [Box(0, [0, 0, 50, 90]), Box(1, [100, 100, 300, 200])]

    ana._labels = {"person", "car"}
    ana.yolo = type("Y", (), {"predict": lambda s, paths, **k: [Result()] * len(paths)})()
    assert [[d[0] for d in f] for f in ana._detect(["a.jpg"])] == [["person"]]


# ---- vision-mcp: named-day windows (local model got weekday maths wrong) ----

def test_named_spans_resolve_in_house_time():
    pytest.importorskip("mcp.server.mcpserver")  # mcp>=2; CI installs it
    from datetime import datetime, timedelta
    import mcp_server as m
    today = datetime.now(m.TZ).date()
    midnight = datetime.combine(today, datetime.min.time(), m.TZ)

    s, e = m._window("yesterday", "")
    assert s == (midnight - timedelta(days=1)).timestamp()
    assert e == midnight.timestamp()

    s, e = m._window("wed morning", "")
    start = datetime.fromtimestamp(s, m.TZ)
    assert start.weekday() == 2 and start.hour == 5
    assert 0 <= (today - start.date()).days < 7
    assert datetime.fromtimestamp(e, m.TZ).hour == 12 or e <= datetime.now().timestamp()

    s, _ = m._window("last night", "")
    assert datetime.fromtimestamp(s, m.TZ) == midnight - timedelta(days=1) + timedelta(
        hours=m.MCP_CFG["night_start_hour"])

    assert m._named_span("2d") is None
    with pytest.raises(ValueError, match="named span"):
        m._window("someday", "")
