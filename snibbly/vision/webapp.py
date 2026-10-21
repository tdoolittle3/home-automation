#!/usr/bin/env python3
"""Vision pipeline web view (Phase 3) — see docs/vision.md.

A small server-rendered UI over the worker's SQLite DB: event feed with
filters, event detail with frames/detections/tags, entity galleries with
rename/merge/delete, and tag management (zero-shot prompts + manual tags).
Runs as the `vision-web` systemd service on :8099, user `vision`, sharing
/opt/vision/data with the worker (WAL handles the two processes).

No auth — LAN-only by the same policy as Frigate's :5000. Writes are
housekeeping (names, merges, tags), never media.

Deps: fastapi, uvicorn, jinja2, python-multipart (same venv as the worker).
"""

import json
import os
import sqlite3
import time

import numpy as np
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from vision_worker import load_config

cfg = load_config()
DATA_DIR = os.path.realpath(cfg["storage"]["data_dir"])
DB_PATH = os.path.join(DATA_DIR, "vision.db")
FRIGATE = cfg["frigate"]["base_url"]
PAGE = 48

app = FastAPI()
templates = Jinja2Templates(
    directory=os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates"))
templates.env.filters["dt"] = lambda ts: (
    time.strftime("%a %b %d %H:%M", time.localtime(ts)) if ts else "?")
templates.env.filters["media"] = lambda p: (
    "/media/" + os.path.relpath(p, DATA_DIR).replace(os.sep, "/") if p else "")


def db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def render(name, request, **ctx):
    return templates.TemplateResponse(request, name, ctx)


# ---- media ----

@app.get("/media/{path:path}")
def media(path: str):
    full = os.path.realpath(os.path.join(DATA_DIR, path))
    if not full.startswith(DATA_DIR + os.sep) or not os.path.isfile(full):
        raise HTTPException(404)
    return FileResponse(full)


# ---- events ----

@app.get("/")
def events(request: Request, camera: str = "", source: str = "", label: str = "",
           entity: int = 0, tag: int = 0, page: int = 0):
    where, params = ["1=1"], []
    for col, val in (("camera", camera), ("source", source), ("label", label)):
        if val:
            where.append(f"e.{col} = ?")
            params.append(val)
    if entity:
        where.append("EXISTS(SELECT 1 FROM detections d WHERE d.event_id=e.id"
                     " AND d.entity_id=?)")
        params.append(entity)
    if tag:
        where.append("EXISTS(SELECT 1 FROM detections d JOIN detection_tags dt"
                     " ON dt.detection_id=d.id WHERE d.event_id=e.id AND dt.tag_id=?)")
        params.append(tag)
    with db() as c:
        rows = c.execute(
            f"""SELECT e.*,
                 (SELECT COUNT(*) FROM detections d WHERE d.event_id=e.id) dets,
                 (SELECT crop_path FROM detections d WHERE d.event_id=e.id
                   ORDER BY conf DESC LIMIT 1) thumb
                FROM events e WHERE {' AND '.join(where)}
                ORDER BY e.start_ts DESC LIMIT ? OFFSET ?""",
            params + [PAGE, page * PAGE]).fetchall()
        cameras = [r[0] for r in c.execute("SELECT DISTINCT camera FROM events")]
        labels = [r[0] for r in c.execute(
            "SELECT DISTINCT label FROM events WHERE label IS NOT NULL ORDER BY label")]
        tags = c.execute("SELECT id, name FROM tags ORDER BY name").fetchall()
    return render("events.html", request, events=rows, cameras=cameras, labels=labels,
                  tags=tags, f={"camera": camera, "source": source, "label": label,
                                "entity": entity, "tag": tag}, page=page)


@app.get("/event/{event_id}")
def event(request: Request, event_id: str):
    with db() as c:
        ev = c.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        if not ev:
            raise HTTPException(404)
        dets = c.execute(
            """SELECT d.*, e.name entity_name FROM detections d
               LEFT JOIN entities e ON e.id=d.entity_id
               WHERE d.event_id=? ORDER BY d.conf DESC LIMIT 200""",
            (event_id,)).fetchall()
        det_tags = {}
        for r in c.execute(
                """SELECT dt.detection_id, dt.tag_id, dt.source, t.name,
                          ROUND(dt.score, 3) score
                   FROM detection_tags dt JOIN tags t ON t.id=dt.tag_id
                   WHERE dt.detection_id IN (SELECT id FROM detections WHERE event_id=?)""",
                (event_id,)):
            det_tags.setdefault(r["detection_id"], []).append(r)
        frames = c.execute(
            "SELECT * FROM frames WHERE event_id=? AND path IS NOT NULL "
            "ORDER BY ts LIMIT 24", (event_id,)).fetchall()
        all_tags = c.execute("SELECT id, name FROM tags ORDER BY name").fetchall()
    clip = (f"{FRIGATE}/api/events/{event_id}/clip.mp4"
            if ev["source"] == "frigate" else None)
    return render("event.html", request, ev=ev, dets=dets, det_tags=det_tags,
                  frames=frames, all_tags=all_tags, clip=clip)


# ---- entities ----

@app.get("/entities")
def entities(request: Request):
    with db() as c:
        rows = c.execute(
            """SELECT e.*,
                 (SELECT crop_path FROM detections d WHERE d.entity_id=e.id
                   ORDER BY d.ts DESC LIMIT 1) thumb
               FROM entities e ORDER BY e.label, e.n DESC""").fetchall()
    groups = {}
    for r in rows:
        groups.setdefault(r["label"], []).append(r)
    return render("entities.html", request, groups=groups)


@app.get("/entity/{entity_id}")
def entity(request: Request, entity_id: int, page: int = 0):
    with db() as c:
        ent = c.execute("SELECT * FROM entities WHERE id=?", (entity_id,)).fetchone()
        if not ent:
            raise HTTPException(404)
        dets = c.execute(
            """SELECT d.*, ROUND(d.conf, 2) c2 FROM detections d WHERE d.entity_id=?
               ORDER BY d.ts DESC LIMIT ? OFFSET ?""",
            (entity_id, PAGE * 2, page * PAGE * 2)).fetchall()
        others = c.execute(
            "SELECT id, label, name, n FROM entities WHERE id != ? ORDER BY n DESC",
            (entity_id,)).fetchall()
    return render("entity.html", request, ent=ent, dets=dets, others=others, page=page)


@app.post("/entity/{entity_id}/rename")
def entity_rename(entity_id: int, name: str = Form("")):
    with db() as c:
        c.execute("UPDATE entities SET name=? WHERE id=?",
                  (name.strip() or None, entity_id))
    return RedirectResponse(f"/entity/{entity_id}", status_code=303)


@app.post("/entity/{entity_id}/merge")
def entity_merge(entity_id: int, into: int = Form(...)):
    """Absorb entity_id into `into`: detections move, centroids blend by
    weight, the absorbed row disappears."""
    with db() as c:
        a = c.execute("SELECT * FROM entities WHERE id=?", (into,)).fetchone()
        b = c.execute("SELECT * FROM entities WHERE id=?", (entity_id,)).fetchone()
        if not a or not b:
            raise HTTPException(404)
        ca = np.frombuffer(a["centroid"], dtype=np.float32) * a["n"]
        cb = np.frombuffer(b["centroid"], dtype=np.float32) * b["n"]
        merged = ca + cb
        merged = merged / np.linalg.norm(merged)
        c.execute("UPDATE detections SET entity_id=? WHERE entity_id=?",
                  (into, entity_id))
        c.execute("UPDATE entities SET centroid=?, n=n+?, "
                  "first_seen=MIN(first_seen, ?), last_seen=MAX(last_seen, ?) WHERE id=?",
                  (merged.astype(np.float32).tobytes(), b["n"],
                   b["first_seen"], b["last_seen"], into))
        c.execute("DELETE FROM entities WHERE id=?", (entity_id,))
    return RedirectResponse(f"/entity/{into}", status_code=303)


@app.post("/entity/{entity_id}/delete")
def entity_delete(entity_id: int):
    with db() as c:
        c.execute("UPDATE detections SET entity_id=NULL WHERE entity_id=?", (entity_id,))
        c.execute("DELETE FROM entities WHERE id=?", (entity_id,))
    return RedirectResponse("/entities", status_code=303)


# ---- tags ----

@app.get("/tags")
def tags(request: Request):
    with db() as c:
        rows = c.execute(
            """SELECT t.*, (SELECT COUNT(*) FROM detection_tags dt
                             WHERE dt.tag_id=t.id) hits
               FROM tags t ORDER BY t.name""").fetchall()
    return render("tags.html", request, tags=rows)


@app.post("/tags/create")
def tag_create(name: str = Form(...), prompt: str = Form(""), threshold: float = Form(0.25)):
    with db() as c:
        c.execute("INSERT OR IGNORE INTO tags(name, prompt, threshold, created_at)"
                  " VALUES(?,?,?,?)",
                  (name.strip(), prompt.strip() or None, threshold, time.time()))
    return RedirectResponse("/tags", status_code=303)


@app.post("/tags/{tag_id}/update")
def tag_update(tag_id: int, prompt: str = Form(""), threshold: float = Form(0.25),
               enabled: int = Form(0)):
    with db() as c:
        c.execute("UPDATE tags SET prompt=?, threshold=?, enabled=? WHERE id=?",
                  (prompt.strip() or None, threshold, enabled, tag_id))
    return RedirectResponse("/tags", status_code=303)


@app.post("/tags/{tag_id}/delete")
def tag_delete(tag_id: int):
    with db() as c:
        c.execute("DELETE FROM detection_tags WHERE tag_id=?", (tag_id,))
        c.execute("DELETE FROM tags WHERE id=?", (tag_id,))
    return RedirectResponse("/tags", status_code=303)


@app.post("/detection/{det_id}/tag")
def detection_tag(det_id: int, tag_id: int = Form(...), back: str = Form("/")):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO detection_tags"
                  "(detection_id, tag_id, score, source) VALUES(?,?,NULL,'manual')",
                  (det_id, tag_id))
    return RedirectResponse(back, status_code=303)


@app.post("/detection/{det_id}/untag")
def detection_untag(det_id: int, tag_id: int = Form(...), back: str = Form("/")):
    with db() as c:
        c.execute("DELETE FROM detection_tags WHERE detection_id=? AND tag_id=?",
                  (det_id, tag_id))
    return RedirectResponse(back, status_code=303)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=cfg["web"]["bind"], port=cfg["web"]["port"], log_level="warning")
