#!/usr/bin/env python3
"""Caption stage for the vision pipeline — see docs/vision.md "Captions".

YOLO + CLIP say *what classes* were in frame; this says what was happening:
"A person in a red top is walking several black dogs on leashes down the
driveway." One representative full-res frame per event goes to Qwen3-VL-8B
(llama-swap model `vlm-8b`), and the sentence lands on the event row, where
vision-mcp serves it to the Hermes agent (event summaries, `timeline`,
caption matches in `search`) and vision-web shows it.

How it shares the GPU: `vlm-8b` sits in its own non-exclusive llama-swap
group with `--fit on`, so it squeezes into whatever VRAM the resident agent
model leaves (~2.2 GB beside coder-30b, ~8-13 s per caption) and is unloaded
the moment any exclusive group loads. A failed or evicted call just retries
later; this loop is the low-priority tenant, never Hermes.

Order: newest events first (fresh questions matter most), back to
`max_age_days`. Talks to llama-swap on 127.0.0.1 directly rather than the
LiteLLM door, so thousands of captions don't flood Langfuse.
"""

import base64
import io
import json
import logging
import os
import signal
import sys
import threading
import time
import urllib.error
import urllib.request

from vision_worker import DB, load_config

log = logging.getLogger("vision-caption")
stop = threading.Event()

# people and animals make a frame more worth describing than a lone car
LIVELY = ("person", "dog", "cat", "bird", "horse", "sheep", "cow", "bear", "bicycle")

PROMPT = (
    "This is one frame from a home security camera ({camera} camera). {notes}"
    "In one or two sentences, describe what is happening: any people (appearance, "
    "what they are carrying or doing), any vehicle that is moving or just arrived "
    "(type, color, company name or markings — e.g. garbage truck, delivery van, mail "
    "truck) and where it is, and any animals. Mention only what is there — never "
    "list what is absent (no \"no animals are visible\"). Do not describe the house, "
    "trees, or cars that are simply parked. If nothing is happening, say "
    "\"Nothing notable.\""
)


def build_prompt(camera, notes):
    note = (notes or {}).get(camera, "").strip()
    return PROMPT.format(camera=camera, notes=(note + " ") if note else "")


def pick_frame(db, event_id, labels):
    """The event's most informative surviving frame: one holding a tagged
    detection (a recognized kind like 'garbage truck') beats one with a person
    or animal, which beats the frame with the most allowlisted detections.
    Returns a frames row (id, camera, ts, path) or None."""
    marks = ",".join("?" * len(labels)) or "''"
    lively = ",".join("?" * len(LIVELY))
    rows = db.query(
        f"""SELECT f.id, f.camera, f.ts, f.path,
                   MAX(EXISTS(SELECT 1 FROM detection_tags dt
                              WHERE dt.detection_id = d.id)) tagged,
                   MAX(d.label IN ({lively})) lively,
                   COUNT(DISTINCT d.label) kinds, COUNT(*) n, MAX(d.conf) conf
            FROM frames f JOIN detections d ON d.frame_id = f.id
            WHERE d.event_id = ? AND f.path IS NOT NULL
              {f"AND d.label IN ({marks})" if labels else ""}
            GROUP BY f.id
            ORDER BY tagged DESC, lively DESC, kinds DESC, n DESC, conf DESC
            LIMIT 1""",
        list(LIVELY) + [event_id] + list(labels))
    return rows[0] if rows else None


def next_events(db, max_age_days, limit=20):
    """Uncaptioned, fully analyzed events inside the window, newest first."""
    return db.query(
        """SELECT id, camera, start_ts FROM events
           WHERE caption IS NULL AND status = 'analyzed' AND start_ts >= ?
           ORDER BY start_ts DESC LIMIT ?""",
        (time.time() - max_age_days * 86400, limit))


def encode_image(path, max_side):
    from PIL import Image
    im = Image.open(path).convert("RGB")
    im.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=88)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def ask_vlm(cfg, image_url, prompt):
    body = {"model": cfg["model"],
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": image_url}},
                {"type": "text", "text": prompt}]}],
            "max_tokens": cfg["max_tokens"], "temperature": 0.2}
    headers = {"Content-Type": "application/json"}
    key = os.environ.get("LLAMA_API_KEY")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(cfg["base_url"].rstrip("/") + "/chat/completions",
                                 json.dumps(body).encode(), headers)
    # generous: a cold vlm-8b load (llama-swap) takes ~10-20 s before the answer
    with urllib.request.urlopen(req, timeout=600) as r:
        out = json.load(r)
    return out["choices"][0]["message"]["content"].strip()


def clean(text):
    text = " ".join(text.split())
    return text[:600]


def caption_one(db, cfg, labels, ev, ask=ask_vlm):
    """Caption one event row. Returns the stored caption ('' when the event
    has no usable frame). Raises on VLM/network errors so the caller backs off
    without marking the event done."""
    frame = pick_frame(db, ev["id"], labels)
    if frame is None:
        db.execute("UPDATE events SET caption='', caption_ts=? WHERE id=?",
                   (time.time(), ev["id"]))
        return ""
    if not os.path.exists(frame["path"]):
        db.execute("UPDATE events SET caption='', caption_ts=? WHERE id=?",
                   (time.time(), ev["id"]))
        return ""
    text = clean(ask(cfg, encode_image(frame["path"], cfg["max_side"]),
                     build_prompt(ev["camera"], cfg.get("camera_notes"))))
    db.execute("UPDATE events SET caption=?, caption_frame_id=?, caption_model=?, "
               "caption_ts=? WHERE id=?",
               (text, frame["id"], cfg["model"], time.time(), ev["id"]))
    return text


def run(db, cfg, labels):
    done = 0
    while not stop.is_set():
        batch = next_events(db, cfg["max_age_days"])
        if not batch:
            stop.wait(cfg["idle_secs"])
            continue
        for ev in batch:
            if stop.is_set():
                break
            t0 = time.time()
            try:
                text = caption_one(db, cfg, labels, ev)
            except (urllib.error.URLError, TimeoutError, OSError, KeyError,
                    ValueError) as e:
                # llama-swap busy swapping, model evicted, box restarting —
                # leave the event uncaptioned and come back later
                log.warning("caption %s failed (%s); retry in %ds", ev["id"], e,
                            cfg["retry_secs"])
                stop.wait(cfg["retry_secs"])
                break
            done += 1
            log.info("caption %s (%s) %.1fs: %s", ev["id"], ev["camera"],
                     time.time() - t0, text or "<no frame>")
    return done


def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config()
    if not cfg["caption"]["enabled"]:
        log.info("captions disabled in config; exiting")
        return
    db = DB(os.path.join(cfg["storage"]["data_dir"], "vision.db"))  # runs migrations
    labels = list(cfg["analyze"].get("labels") or [])
    signal.signal(signal.SIGTERM, lambda *a: stop.set())
    signal.signal(signal.SIGINT, lambda *a: stop.set())
    log.info("vision-captioner up: model=%s via %s, last %gd",
             cfg["caption"]["model"], cfg["caption"]["base_url"],
             cfg["caption"]["max_age_days"])
    run(db, cfg["caption"], labels)
    log.info("vision-captioner stopped")


if __name__ == "__main__":
    main()
