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

Deps: mcp >= 1.9 (pip name `mcp`) plus the worker venv's torch + open_clip.
"""

import logging
import sqlite3
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
from mcp.server.fastmcp import FastMCP

from analyzer import CLIP_MODEL, CLIP_PRETRAINED
from vision_worker import load_config

cfg = load_config()
MCP_CFG = cfg["mcp"]
DB_PATH = cfg["storage"]["data_dir"] + "/vision.db"
WEB = MCP_CFG["web_base_url"].rstrip("/")
TZ = ZoneInfo(MCP_CFG["timezone"])
EMB_CHUNK = 20_000  # detections per scoring batch; bounds memory as the DB grows

log = logging.getLogger("vision-mcp")

# stateless: every request stands alone, so a restart here never strands the
# session Hermes opened at its own startup
server = FastMCP("vision", host=MCP_CFG["bind"], port=MCP_CFG["port"],
                 stateless_http=True)

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
        raise ValueError(f"can't parse {name}={value!r}: use '6h'/'2d'/'90m', "
                         "'YYYY-MM-DD HH:MM' (local), or a unix epoch")


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
    return {
        "event_id": ev["id"],
        "camera": ev["camera"],
        "source": ev["source"],      # 'frigate' or 'sweep' (sweep = our own find)
        "label": ev["label"],
        "when": _local(ev["start_ts"]),
        "duration_secs": round((ev["end_ts"] or ev["start_ts"] or 0)
                               - (ev["start_ts"] or 0)),
        "detections": n,
        "night": _is_night(ev["start_ts"]) if ev["start_ts"] else None,
        "url": f"{WEB}/event/{ev['id']}",
    }


@server.tool()
def search(query: str, since: str = "24h", until: str = "", camera: str = "",
           top_k: int = 8, min_score: float = 0.22) -> dict:
    """Semantic search over everything the cameras detected: describe what to
    look for in plain language ("white delivery van", "person carrying a box")
    and get back matching events ranked by CLIP similarity, with links.

    Matches single concepts within one detection crop. For "X together with Y"
    (e.g. a person AND a dog) use frames_with instead. For plain time/label
    listings ("what happened last night") use recent_events — it's cheaper.

    since/until: '6h'/'2d'/'90m' ago, 'YYYY-MM-DD HH:MM' local, or epoch.
    camera: '' = all. min_score: CLIP cosine cutoff — 0.2 loose, 0.3 strict.
    """
    t_from = _parse_when(since, "since")
    t_to = _parse_when(until, "until") if until else time.time()
    qvec = _text_embed(query)

    where = "embedding IS NOT NULL AND ts >= ? AND ts <= ?"
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

    return {"query": query, "window": [_local(t_from), _local(t_to)],
            "detections_scanned": scanned, "results": results,
            "note": ("no matches — try a lower min_score, a wider window, or "
                     "recent_events" if not results else "")}


@server.tool()
def recent_events(since: str = "24h", until: str = "", camera: str = "",
                  label: str = "", source: str = "", night_only: bool = False,
                  limit: int = 40) -> dict:
    """List camera events by time and metadata — no ML, just the record.
    This answers "any motion last night?", "what did the backyard camera see
    today?", "all car events this week".

    label: a YOLO/Frigate class like person, car, truck, dog, cat.
    source: 'frigate' (Frigate's own detections) or 'sweep' (our recordings
    sweep — catches what Frigate missed); '' = both.
    night_only: keep only events starting at night — 21:00 to 07:00 local by
    default (config [mcp] night_start_hour/night_end_hour).
    since/until: '6h'/'2d', 'YYYY-MM-DD HH:MM' local, or epoch.
    """
    t_from = _parse_when(since, "since")
    t_to = _parse_when(until, "until") if until else time.time()
    where, params = ["start_ts >= ?", "start_ts <= ?"], [t_from, t_to]
    for col, val in (("camera", camera), ("label", label), ("source", source)):
        if val:
            where.append(f"{col} = ?")
            params.append(val)
    with db() as c:
        rows = c.execute(
            f"SELECT id FROM events WHERE {' AND '.join(where)} "
            f"ORDER BY start_ts DESC", params).fetchall()
        events, truncated = [], 0
        for r in rows:
            ev = _event_summary(c, r["id"])
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
    """Find moments where ALL the given object labels appear in the SAME frame
    — the right tool for compositional queries like a person walking a dog
    (labels=["person","dog"]) or a person next to a truck.

    Returns the matching events with how many frames co-occurred. Labels are
    YOLO classes: person, dog, cat, car, truck, bicycle, backpack, ...
    """
    labels = sorted({lab.strip().lower() for lab in labels if lab.strip()})
    if len(labels) < 2:
        return {"error": "give at least two labels; for one label use "
                         "recent_events or search"}
    t_from = _parse_when(since, "since")
    t_to = _parse_when(until, "until") if until else time.time()
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
def entities(label: str = "") -> dict:
    """List recurring entities — the same individual person/vehicle/animal
    re-identified across events by embedding similarity. Named ones ("mail
    truck") were labeled by the owner; unnamed ones are clusters awaiting a
    name. Use this for "when did the mail truck last come?" — find the entity
    here, then its recent events via its url.

    label: filter by YOLO class (person, car, truck, dog...); '' = all.
    """
    where, params = "", []
    if label:
        where = "WHERE label = ?"
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
    server.run(transport="streamable-http")
