#!/usr/bin/env python3
"""Vision pipeline MCP server — camera-archive search tools for the Hermes
agent (docs/vision.md "Agent search", docs/hermes.md).

Exposes the worker's SQLite DB as read-only MCP tools over streamable HTTP on
:8097. Hermes on ladybird lists this server under `mcp_servers:` and registers
the tools at startup as `mcp_vision_*`, so the Operator model can answer
"did anyone walk a dog past the driveway today?" with links into vision-web.

Semantic search rides on what Phase 2 already stores: a 512-dim L2-normalized
CLIP embedding per detection crop. A text query goes through the same CLIP
text tower — on CPU here, the GPU belongs to the worker — and ranks crops by
cosine, grouped into events. Compositional queries ("person AND dog") don't
suit crop embeddings (a person crop doesn't contain the dog), so frames_with()
does that as SQL: frames where all the labels co-occur.

Read-only by design: the DB is opened mode=ro and there are no mutation tools.
No auth — LAN-only by the same policy as Frigate :5000 and vision-web :8099.

Deps: mcp >= 2 (pip name `mcp`; the 2.x SDK — FastMCP became MCPServer) plus
the worker venv's torch + open_clip.
"""

import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
from mcp.server.mcpserver import MCPServer

from analyzer import CLIP_MODEL, CLIP_PRETRAINED
from vision_worker import load_config

cfg = load_config()
MCP_CFG = cfg["mcp"]
DB_PATH = cfg["storage"]["data_dir"] + "/vision.db"
WEB = MCP_CFG["web_base_url"].rstrip("/")
TZ = ZoneInfo(MCP_CFG["timezone"])
EMB_CHUNK = 20_000  # detections per scoring batch; bounds memory as the DB grows
# the worker's YOLO class allowlist; older rows still hold scenery classes
# (bench, suitcase, boat...) from before it existed, so queries apply it too
LABELS = list(cfg["analyze"].get("labels") or [])
LABEL_SQL = f"label IN ({','.join(repr(lab) for lab in LABELS)})" if LABELS else "1"

log = logging.getLogger("vision-mcp")

# the instructions + tool docstrings below are agent-facing routing text: the
# live problem they solve is the model answering camera questions from Home
# Assistant's coarse frigate occupancy sensors instead of coming here
server = MCPServer("vision", instructions=(
    "Security-camera archive for the house (driveway + backyard cameras). "
    "Use these tools for ANY question about what or who the cameras saw — "
    "'who was by the driveway?', 'any people last night?', 'when did the "
    "mail truck come?'. Do NOT answer camera questions from Home Assistant: "
    "its frigate sensors only expose coarse occupancy counts, while this "
    "archive holds every detection with timestamps, re-identified "
    "individuals, and links to the footage."))

_clip_lock = threading.Lock()
_clip = {}  # model/tokenizer, lazy — only semantic search pays the load cost


def db():
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def _text_embed(prompt):
    with _clip_lock:
        if not _clip:
            import open_clip
            import torch
            t0 = time.time()
            model, _, _ = open_clip.create_model_and_transforms(
                CLIP_MODEL, pretrained=CLIP_PRETRAINED, device="cpu")
            model.eval()
            _clip["model"], _clip["torch"] = model, torch
            _clip["tok"] = open_clip.get_tokenizer(CLIP_MODEL)
            log.info("CLIP text tower loaded in %.1fs", time.time() - t0)
        torch = _clip["torch"]
        with torch.no_grad():
            feats = _clip["model"].encode_text(_clip["tok"]([prompt]))
        feats = feats / feats.norm(dim=-1, keepdim=True)
        return feats.numpy()[0]


def _parse_when(value, name):
    """'6h' / '2d' / '90m' ago, ISO like '2026-11-08 17:00' (camera-local
    time), or a unix epoch. Returns epoch seconds."""
    s = str(value).strip()
    try:
        if s and s[-1] in "mhd" and s[:-1].replace(".", "").isdigit():
            secs = float(s[:-1]) * {"m": 60, "h": 3600, "d": 86400}[s[-1]]
            return time.time() - secs
        if s.replace(".", "").isdigit():
            return float(s)
        dt = datetime.fromisoformat(s)
        return (dt if dt.tzinfo else dt.replace(tzinfo=TZ)).timestamp()
    except (ValueError, KeyError):
        raise ValueError(f"can't parse {name}={value!r}: use a named span like "
                         "'yesterday' / 'wednesday morning' / 'last night', "
                         "'6h'/'2d'/'90m', 'YYYY-MM-DD HH:MM' (local), or a unix epoch")


WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday",
            "saturday", "sunday"]
PARTS = {"morning": (5, 12), "afternoon": (12, 17), "evening": (17, 21)}


def _named_span(text):
    """'yesterday', 'wednesday morning', 'today afternoon', 'last night',
    'tuesday night' -> (start, end) epoch in house-local time, else None.
    A weekday means the most recent one, today included. Local models get
    calendar arithmetic wrong ("Wednesday" -> today's date), so the agent is
    told to pass the words and let this resolve them."""
    words = str(text).strip().lower().replace(",", " ").split()
    if not words:
        return None
    if words[0] == "last" and len(words) > 1:
        words = words[1:]
        if words == ["night"]:
            words = ["yesterday", "night"]
    today = datetime.now(TZ).date()
    day_word, part = words[0], (words[1] if len(words) > 1 else "")
    if day_word in ("tonight",):
        day_word, part = "today", "night"
    if day_word == "today":
        day = today
    elif day_word == "yesterday":
        day = today - timedelta(days=1)
    elif len(day_word) >= 3 and any(w.startswith(day_word) for w in WEEKDAYS):
        # full name or a prefix: 'wednesday', 'wed', 'thurs'
        idx = next(i for i, w in enumerate(WEEKDAYS) if w.startswith(day_word))
        day = today - timedelta(days=(today.weekday() - idx) % 7)
    else:
        return None
    if len(words) > 2 or (part and part not in PARTS and part != "night"):
        return None
    start = datetime.combine(day, datetime.min.time(), TZ)
    if part == "night":
        s = start + timedelta(hours=MCP_CFG["night_start_hour"])
        e = start + timedelta(days=1, hours=MCP_CFG["night_end_hour"])
    elif part:
        a, b = PARTS[part]
        s, e = start + timedelta(hours=a), start + timedelta(hours=b)
    else:
        s, e = start, start + timedelta(days=1)
    return s.timestamp(), min(e.timestamp(), time.time())


def _window(since, until):
    """Resolve a tool's since/until. A named span in `since` ('wednesday
    morning') supplies both ends unless `until` is given explicitly."""
    span = _named_span(since)
    if span:
        t_from, t_to = span
    else:
        t_from, t_to = _parse_when(since, "since"), time.time()
    if until:
        t_to = (_named_span(until) or (None, _parse_when(until, "until")))[1]
    return t_from, t_to


def _local(ts):
    return (datetime.fromtimestamp(ts, TZ).strftime("%a %b %d %H:%M")
            if ts else "?")


def _is_night(ts):
    h = datetime.fromtimestamp(ts, TZ).hour
    start, end = MCP_CFG["night_start_hour"], MCP_CFG["night_end_hour"]
    return h >= start or h < end


def _event_summary(c, event_id):
    ev = c.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    if not ev:
        return None
    n = c.execute("SELECT COUNT(*) FROM detections WHERE event_id=?",
                  (event_id,)).fetchone()[0]
    # what was actually in frame, as individuals: "person ×2 (one of them the
    # named entity), car ×1" — far more useful to relay than a detection count
    comp = c.execute(
        f"""SELECT d.label, COUNT(DISTINCT COALESCE(d.entity_id, -d.id)) individuals,
                  GROUP_CONCAT(DISTINCT e.name) named
           FROM detections d LEFT JOIN entities e ON e.id=d.entity_id
           WHERE d.event_id=? AND d.{LABEL_SQL} GROUP BY d.label
           ORDER BY individuals DESC""", (event_id,)).fetchall()
    tags = [r[0] for r in c.execute(
        """SELECT t.name FROM detection_tags dt
           JOIN detections d ON d.id=dt.detection_id JOIN tags t ON t.id=dt.tag_id
           WHERE d.event_id=? AND t.enabled=1 GROUP BY t.name
           ORDER BY MAX(dt.score) DESC""", (event_id,))]
    label = ev["label"]
    if LABELS and label not in LABELS:  # a scenery-labelled sweep event
        label = comp[0]["label"] if comp else None
    return {
        "event_id": ev["id"],
        "camera": ev["camera"],
        "source": ev["source"],      # 'frigate' or 'sweep' (sweep = our own find)
        "label": label,
        "tags": tags,                # zero-shot kinds, e.g. 'garbage truck'
        "when": _local(ev["start_ts"]),
        "duration_secs": round((ev["end_ts"] or ev["start_ts"] or 0)
                               - (ev["start_ts"] or 0)),
        "detections": n,
        "composition": [
            {"label": r["label"], "individuals": r["individuals"],
             **({"named": r["named"].split(",")} if r["named"] else {})}
            for r in comp],
        "night": _is_night(ev["start_ts"]) if ev["start_ts"] else None,
        "url": f"{WEB}/event/{ev['id']}",
    }


@server.tool()
def search(query: str, since: str = "24h", until: str = "", camera: str = "",
           top_k: int = 8, min_score: float = 0.22) -> dict:
    """Search the security-camera archive (driveway, backyard) by plain-text
    description — "white delivery van", "person carrying a box" — and get
    matching events ranked by visual similarity, each with a footage link.
    Prefer this over Home Assistant for anything about what the cameras saw.

    Matches single concepts within one detection crop. For "X together with Y"
    (e.g. a person AND a dog) use frames_with instead. For plain time/label
    listings ("who was by the driveway today" = label 'person') use
    recent_events — it's cheaper and complete.

    since: a named span — 'yesterday', 'wednesday morning', 'last night',
    'today afternoon' (resolved server-side in house time; covers that whole
    span, so leave until empty) — or '6h'/'2d'/'90m' ago, 'YYYY-MM-DD HH:MM'
    local, or epoch.
    camera: '' = all. min_score: CLIP cosine cutoff — 0.2 loose, 0.3 strict.
    """
    t_from, t_to = _window(since, until)
    qvec = _text_embed(query)

    where = f"embedding IS NOT NULL AND ts >= ? AND ts <= ? AND {LABEL_SQL}"
    params = [t_from, t_to]
    if camera:
        where += " AND camera = ?"
        params.append(camera)

    best = {}  # event key -> (score, label, det_id)
    scanned = 0
    with db() as c:
        cur = c.execute(f"SELECT id, event_id, label, ts, embedding "
                        f"FROM detections WHERE {where} ORDER BY id", params)
        while True:
            rows = cur.fetchmany(EMB_CHUNK)
            if not rows:
                break
            scanned += len(rows)
            mat = np.frombuffer(b"".join(r["embedding"] for r in rows),
                                dtype=np.float32).reshape(len(rows), -1)
            scores = mat @ qvec
            for r, s in zip(rows, scores):
                s = float(s)
                if s < min_score:
                    continue
                key = r["event_id"] or f"frame-det-{r['id']}"
                if key not in best or s > best[key][0]:
                    best[key] = (s, r["label"], r["id"])

        ranked = sorted(best.items(), key=lambda kv: -kv[1][0])[:top_k]
        results = []
        for key, (score, label, det_id) in ranked:
            if key.startswith("frame-det-"):
                d = c.execute("SELECT camera, ts FROM detections WHERE id=?",
                              (det_id,)).fetchone()
                results.append({"event_id": None, "camera": d["camera"],
                                "when": _local(d["ts"]), "url": WEB})
            else:
                results.append(_event_summary(c, key) or {})
            results[-1].update(score=round(score, 3), matched_label=label)

        # the query names a recognized kind ("garbage truck") — those tag hits
        # are precise where crop similarity is fuzzy, so lead with them, and
        # show the latest sightings outside the window too: models routinely
        # leave `since` at its 24h default for "yesterday morning"
        q = query.strip().lower()
        tag = next((t for t in c.execute(
            "SELECT id, name FROM tags WHERE enabled=1").fetchall()
            if t["name"].lower() in q), None)
        tag_block = {}
        if tag:
            tag_block = {
                "tag": tag["name"],
                "tag_hits_in_window": _tag_sightings(c, tag["id"], t_from, t_to, 20),
                "latest_sightings_any_time": _tag_sightings(c, tag["id"], limit=5),
            }

    note = ""
    if tag_block:
        note = (f"'{tag['name']}' is a recognized kind: tag_hits_in_window are "
                "the reliable answer for this window; results below are fuzzy "
                "visual similarity. Window wrong? pass since='wednesday morning' etc.")
    elif not results:
        note = "no matches — try a lower min_score, a wider window, or recent_events"
    return {"query": query, "window": [_local(t_from), _local(t_to)],
            **tag_block, "detections_scanned": scanned, "results": results,
            "note": note}


@server.tool()
def recent_events(since: str = "24h", until: str = "", camera: str = "",
                  label: str = "", tag: str = "", source: str = "",
                  night_only: bool = False, limit: int = 40) -> dict:
    """List security-camera events — who/what was seen, when, on which camera,
    with a footage link each. The first tool for "who was by the driveway
    today?" (label='person', camera='driveway'), "any motion last night?"
    (night_only=true), "what did the backyard camera see?", and for specific
    vehicle kinds: "did the garbage truck come Wednesday?" → tag='garbage truck'
    with since='wednesday morning'. Far more specific than Home
    Assistant's frigate occupancy sensors — prefer this.

    label: a YOLO class like person, car, truck, dog, cat.
    tag: a zero-shot kind such as 'garbage truck', 'delivery van', 'mail truck',
    'school bus' — call tags() for the current list. Precise; prefer it over
    search() when the thing asked about is one of the tags.
    source: 'frigate' (Frigate's own detections) or 'sweep' (our recordings
    sweep — catches what Frigate missed); '' = both.
    night_only: keep only events starting at night — 21:00 to 07:00 local by
    default (config [mcp] night_start_hour/night_end_hour).
    since: for a named day pass the words — 'yesterday', 'wednesday morning',
    'last night', 'today afternoon' — and leave until empty: the server
    resolves them in house time. Never compute dates yourself. Otherwise
    '6h'/'2d' ago, 'YYYY-MM-DD HH:MM' local, or epoch. The default (last 24h)
    is NOT "yesterday".
    """
    t_from, t_to = _window(since, until)
    where, params = ["start_ts >= ?", "start_ts <= ?"], [t_from, t_to]
    for col, val in (("camera", camera), ("label", label), ("source", source)):
        if val:
            where.append(f"{col} = ?")
            params.append(val)
    if tag:
        where.append("""EXISTS(SELECT 1 FROM detections d
                          JOIN detection_tags dt ON dt.detection_id=d.id
                          JOIN tags t ON t.id=dt.tag_id
                          WHERE d.event_id=events.id AND t.name = ? COLLATE NOCASE)""")
        params.append(tag.strip())
    with db() as c:
        rows = c.execute(
            f"SELECT id FROM events WHERE {' AND '.join(where)} "
            f"ORDER BY start_ts DESC", params).fetchall()
        events, truncated = [], 0
        for r in rows:
            ev = _event_summary(c, r["id"])
            if ev["label"] is None:  # nothing but scenery classes
                continue
            if night_only and not ev["night"]:
                continue
            if len(events) >= limit:
                truncated += 1
                continue
            events.append(ev)
    return {"window": [_local(t_from), _local(t_to)], "events": events,
            "more_not_shown": truncated}


@server.tool()
def frames_with(labels: list[str], since: str = "7d", until: str = "",
                camera: str = "", limit: int = 20) -> dict:
    """Find camera moments where ALL the given object labels appear in the
    SAME frame — the right tool for compositional queries like a person
    walking a dog (labels=["person","dog"]) or a person next to a truck.

    Returns the matching events with how many frames co-occurred. Labels are
    YOLO classes: person, dog, cat, car, truck, bicycle, backpack, ...
    """
    labels = sorted({lab.strip().lower() for lab in labels if lab.strip()})
    if len(labels) < 2:
        return {"error": "give at least two labels; for one label use "
                         "recent_events or search"}
    t_from, t_to = _window(since, until)
    params = [t_from, t_to] + labels + [len(labels)]
    extra = ""
    if camera:
        extra = " AND camera = ?"
        params.insert(2, camera)
    marks = ",".join("?" * len(labels))
    with db() as c:
        rows = c.execute(
            f"""SELECT frame_id, event_id, MIN(ts) ts FROM detections
                WHERE ts >= ? AND ts <= ?{extra} AND label IN ({marks})
                GROUP BY frame_id HAVING COUNT(DISTINCT label) = ?
                ORDER BY ts DESC""", params).fetchall()
        by_event = {}
        for r in rows:
            key = r["event_id"] or f"frame-{r['frame_id']}"
            by_event.setdefault(key, []).append(r)
        results = []
        for key, hits in list(by_event.items())[:limit]:
            if key.startswith("frame-"):
                results.append({"event_id": None, "when": _local(hits[0]["ts"]),
                                "co_occurring_frames": len(hits), "url": WEB})
            else:
                ev = _event_summary(c, key) or {}
                ev["co_occurring_frames"] = len(hits)
                results.append(ev)
    return {"labels": labels, "window": [_local(t_from), _local(t_to)],
            "results": results, "more_not_shown": max(0, len(by_event) - limit)}


@server.tool()
def tags() -> dict:
    """List the zero-shot kinds the archive recognizes on detections (e.g.
    'garbage truck', 'delivery van', 'mail truck', 'school bus'), with how many
    events carry each and when it was last seen. Use the name as
    recent_events(tag=...). A kind not in this list isn't tagged — fall back
    to search() with a description."""
    with db() as c:
        rows = c.execute(
            """SELECT t.id, t.name, COUNT(DISTINCT d.event_id) events
               FROM tags t
               LEFT JOIN detection_tags dt ON dt.tag_id=t.id
               LEFT JOIN detections d ON d.id=dt.detection_id
               WHERE t.enabled=1 GROUP BY t.id ORDER BY t.name""").fetchall()
        out = []
        for r in rows:
            out.append({"name": r["name"], "events": r["events"],
                        "recent_sightings": _tag_sightings(c, r["id"], limit=8)})
    return {"tags": out,
            "note": "recent_sightings are the newest first, house-local. For a "
                    "specific day call recent_events(tag=<name>, "
                    "since='wednesday morning')."}


def _tag_sightings(c, tag_id, since=0.0, until=None, limit=8):
    """Distinct events carrying a tag, newest first: when/camera/url."""
    until = until or time.time()
    rows = c.execute(
        """SELECT d.event_id, MIN(d.ts) ts, d.camera, MAX(dt.score) score
           FROM detection_tags dt JOIN detections d ON d.id=dt.detection_id
           WHERE dt.tag_id=? AND d.ts BETWEEN ? AND ?
           GROUP BY COALESCE(d.event_id, d.id) ORDER BY ts DESC LIMIT ?""",
        (tag_id, since, until, limit)).fetchall()
    return [{"when": _local(r["ts"]), "camera": r["camera"],
             "url": f"{WEB}/event/{r['event_id']}" if r["event_id"] else WEB}
            for r in rows]


@server.tool()
def entities(label: str = "") -> dict:
    """List recurring individuals the cameras re-identified across days — the
    same specific person/vehicle/animal clustered by appearance. Named ones
    ("mail truck") were labeled by the owner; unnamed ones await a name. Use
    for "when did the mail truck last come?", "who are the regulars by the
    driveway?", "have we seen that person before?".

    label: filter by YOLO class (person, car, truck, dog...); '' = all.
    """
    where, params = f"WHERE {LABEL_SQL}", []
    if label:
        where += " AND label = ?"
        params = [label.strip().lower()]
    with db() as c:
        rows = c.execute(
            f"""SELECT id, label, name, n, first_seen, last_seen
                FROM entities {where} ORDER BY n DESC LIMIT 100""",
            params).fetchall()
    return {"entities": [{
        "id": r["id"], "label": r["label"], "name": r["name"],
        "sightings": r["n"], "first_seen": _local(r["first_seen"]),
        "last_seen": _local(r["last_seen"]), "url": f"{WEB}/entity/{r['id']}",
    } for r in rows]}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    # stateless: every request stands alone, so a restart here never strands
    # the session Hermes opened at its own startup
    server.run(transport="streamable-http", host=MCP_CFG["bind"],
               port=MCP_CFG["port"], stateless_http=True)
