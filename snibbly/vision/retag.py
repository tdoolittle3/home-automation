#!/usr/bin/env python3
"""Seed the default zero-shot tags and (re)score stored detections against
every enabled tag — see docs/vision.md "Tags".

The worker only tags detections as it analyzes them, so a tag added (or
retuned) later would never touch history. Every detection already keeps its
CLIP embedding, so re-scoring is a matrix multiply on CPU: no images, no GPU.

    retag.py --seed            add any missing default tags, then retag 14d
    retag.py --since 30        retag the last 30 days
    retag.py --dry-run         print hit counts and top hits, write nothing

Only source='auto' rows are replaced; manual tags from the web UI stay.
Run as the vision user (or root, then chown vision:vision vision.db*).
"""

import argparse
import datetime
import sqlite3
import sys
import time
from zoneinfo import ZoneInfo

import numpy as np

from analyzer import CLIP_MODEL, CLIP_PRETRAINED, NEGATIVE_PROMPTS, tag_hits
from vision_worker import load_config

# name, CLIP prompt, cosine floor. The softmax gate in analyzer.tag_hits does
# most of the work; the floor just drops crops CLIP has no opinion about.
# Vehicle *kinds* only: they compete cleanly in one softmax. Person-activity
# tags ("delivery person carrying a package", "person walking a dog") were
# tried 2026-11-12 and fired on anyone holding a phone or a bag — a whole-person
# crop doesn't carry that; it needs the VLM caption stage. Same-frame
# person+dog is frames_with's job. Verified hits: garbage truck 11-04 09:38,
# 11-06 07:59, 11-10 07:21, 11-11 09:54 (Mountain High Disposal / WM trucks).
DEFAULT_TAGS = [
    ("garbage truck", "a photo of a garbage truck", 0.22),
    ("delivery van", "a photo of a delivery van", 0.22),
    ("mail truck", "a photo of a white USPS mail truck", 0.22),
    ("school bus", "a photo of a yellow school bus", 0.22),
]
BATCH = 20_000


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", action="store_true", help="add missing default tags")
    ap.add_argument("--since", type=float, default=14, help="days back to retag")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    tz = ZoneInfo(cfg["mcp"]["timezone"])
    labels = cfg["analyze"].get("labels") or []
    c = sqlite3.connect(cfg["storage"]["data_dir"] + "/vision.db", timeout=60)
    c.row_factory = sqlite3.Row

    if args.seed and not args.dry_run:
        with c:
            for name, prompt, thr in DEFAULT_TAGS:
                c.execute("INSERT OR IGNORE INTO tags(name, prompt, threshold, enabled,"
                          " created_at) VALUES(?,?,?,1,?)", (name, prompt, thr, time.time()))
    tags = c.execute("SELECT id, name, prompt, threshold FROM tags "
                     "WHERE enabled=1 AND prompt IS NOT NULL").fetchall()
    if args.seed and args.dry_run:  # score the defaults without inserting them
        have = {t["name"] for t in tags}
        tags = list(tags) + [{"id": -i - 1, "name": n, "prompt": p, "threshold": t}
                             for i, (n, p, t) in enumerate(DEFAULT_TAGS) if n not in have]
    if not tags:
        sys.exit("no enabled tags with prompts")

    import open_clip
    import torch
    model, _, _ = open_clip.create_model_and_transforms(
        CLIP_MODEL, pretrained=CLIP_PRETRAINED, device="cpu")
    tok = open_clip.get_tokenizer(CLIP_MODEL)
    with torch.no_grad():
        f = model.encode_text(tok([t["prompt"] for t in tags] + NEGATIVE_PROMPTS))
    f = (f / f.norm(dim=-1, keepdim=True)).numpy()
    tvecs, neg_vecs = f[:len(tags)], f[len(tags):]
    spec = [(t["id"], t["prompt"], t["threshold"], v) for t, v in zip(tags, tvecs)]
    names = {t["id"]: t["name"] for t in tags}

    since = time.time() - args.since * 86400
    where = "embedding IS NOT NULL AND ts >= ?"
    params = [since]
    if labels:
        where += f" AND label IN ({','.join('?' * len(labels))})"
        params += labels
    cur = c.execute(f"SELECT id, ts, camera, label, embedding FROM detections "
                    f"WHERE {where} ORDER BY id", params)
    scanned, found = 0, []  # (det id, tag id, cos, prob, ts, camera, label)
    while True:
        rows = cur.fetchmany(BATCH)
        if not rows:
            break
        scanned += len(rows)
        mat = np.frombuffer(b"".join(r["embedding"] for r in rows),
                            dtype=np.float32).reshape(len(rows), -1)
        for r, hits in zip(rows, tag_hits(mat, spec, NEGATIVE_PROMPTS, neg_vecs)):
            found += [(r["id"], tid, cos, p, r["ts"], r["camera"], r["label"])
                      for tid, cos, p in hits]

    print(f"scanned {scanned} detections since {args.since:g}d")
    for tid, name in names.items():
        mine = sorted((h for h in found if h[1] == tid), key=lambda h: -h[3])
        moments = sorted({datetime.datetime.fromtimestamp(h[4], tz)
                          .strftime("%a %m-%d %H:%M") + f" {h[5]}/{h[6]}" for h in mine})
        print(f"  {name}: {len(mine)} detections, {len(moments)} distinct minutes")
        for m in moments[:12]:
            print(f"      {m}")

    if args.dry_run:
        return
    ids = [(r[0],) for r in c.execute(f"SELECT id FROM detections WHERE {where}", params)]
    with c:
        c.execute("CREATE TEMP TABLE scope(id INTEGER PRIMARY KEY)")
        c.executemany("INSERT INTO scope VALUES(?)", ids)
        c.execute("DELETE FROM detection_tags WHERE source='auto' "
                  "AND detection_id IN (SELECT id FROM scope)")
        c.executemany("INSERT OR IGNORE INTO detection_tags(detection_id, tag_id, score, source)"
                      " VALUES(?,?,?,'auto')", [(h[0], h[1], h[2]) for h in found])
    print(f"wrote {len(found)} auto tag rows")


if __name__ == "__main__":
    main()
