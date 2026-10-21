"""GPU analysis for the vision worker (Phase 2) — see docs/vision.md.

Runs YOLO11s detection and CLIP ViT-B/16 embeddings on the 1050 Ti, fp32 only
(Pascal fp16 is 1/64 rate). The systemd unit sets CUDA_VISIBLE_DEVICES=1, so
"cuda:0" here IS the vision card. One lock serializes GPU users: the analyze
job runner and the sweep handler contend for it while event downloads proceed.

What a detection becomes:
  * a crop JPEG under data/crops/,
  * a 512-dim L2-normalized CLIP embedding (BLOB in the DB),
  * an entity assignment — cosine match against per-label entity centroids,
    above the threshold joins (running-mean centroid update), below founds a
    new unnamed entity (the Phase-3 UI names/merges them),
  * zero-shot tag scores against every enabled prompt in the tags table.

Person re-ID note: the plan called for OSNet on person crops; torchreid does
not install cleanly on py3.12/torch-2.14, so persons use CLIP like everything
else for now (clothing-level matching). Revisit if person clustering
disappoints.

Models live in /opt/vision/models (pre-downloaded; HF_HOME points inside it).
"""

import json
import logging
import os
import threading
import time

import numpy as np

log = logging.getLogger("vision")

YOLO_WEIGHTS = "/opt/vision/models/yolo11s.pt"
# the -quickgelu variant matters: OpenAI weights use QuickGELU, and plain
# ViT-B-16 silently runs them with the wrong activation (open_clip warns)
CLIP_MODEL, CLIP_PRETRAINED = "ViT-B-16-quickgelu", "openai"


def _to_blob(vec):
    return vec.astype(np.float32).tobytes()


def _from_blob(blob):
    return np.frombuffer(blob, dtype=np.float32)


def _iou(a, b):
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    if not inter:
        return 0.0
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    return inter / (area_a + area_b - inter)


def _static_mask(per_frame, min_presence):
    """Scenery suppression. A detection that sits in (nearly) the same box
    with the same label across >= min_presence of the frames is a static
    object — a parked car, a grill YOLO calls a suitcase — not an event.
    Returns, per frame, the set of detection indices to suppress. Needs a
    decent frame count to judge; short batches are left untouched."""
    n = len(per_frame)
    masks = [set() for _ in per_frame]
    if n < 8:
        return masks
    all_dets = [(fi, di, lab, box) for fi, dets in enumerate(per_frame)
                for di, (lab, _conf, box) in enumerate(dets)]
    assigned = set()
    for i, (fi, di, lab, box) in enumerate(all_dets):
        if (fi, di) in assigned:
            continue
        members, frames_seen = [(fi, di)], {fi}
        for fj, dj, lab2, box2 in all_dets[i + 1:]:
            if (fj, dj) in assigned or lab2 != lab:
                continue
            if _iou(box, box2) >= 0.6:
                members.append((fj, dj))
                frames_seen.add(fj)
        if len(frames_seen) >= min_presence * n:
            for m in members:
                assigned.add(m)
                masks[m[0]].add(m[1])
    return masks


class Analyzer:
    def __init__(self, cfg, db):
        self.cfg = cfg["analyze"]
        self.data_dir = cfg["storage"]["data_dir"]
        self.db = db
        self.gpu_lock = threading.Lock()
        self._loaded = False
        self._tags_loaded_at = 0.0
        self._tags = []  # (id, name, threshold, unit text vec)

    # ---- models ----

    def _ensure_models(self):
        if self._loaded:
            return
        import torch
        import open_clip
        from ultralytics import YOLO
        t0 = time.time()
        self.torch = torch
        self.yolo = YOLO(YOLO_WEIGHTS)
        self.clip, _, self.clip_pre = open_clip.create_model_and_transforms(
            CLIP_MODEL, pretrained=CLIP_PRETRAINED, device="cuda:0")
        self.clip.eval()
        self.clip_tok = open_clip.get_tokenizer(CLIP_MODEL)
        self._loaded = True
        free, total = torch.cuda.mem_get_info(0)
        log.info("models loaded in %.1fs (%d MiB VRAM in use)",
                 time.time() - t0, (total - free) // 2**20)

    def _detect(self, paths):
        """Batched YOLO over image paths -> per-image [(label, conf, xyxy)]."""
        out = []
        bs = self.cfg["batch_size"]
        for i in range(0, len(paths), bs):
            results = self.yolo.predict(paths[i:i + bs], device=0, verbose=False,
                                        conf=self.cfg["conf_threshold"], imgsz=640)
            for r in results:
                dets = []
                for b in r.boxes:
                    box = [round(v) for v in b.xyxy[0].tolist()]
                    if box[2] - box[0] < 4 or box[3] - box[1] < 4:
                        continue  # degenerate sliver; CLIP preprocess would choke
                    dets.append((r.names[int(b.cls)], float(b.conf), box))
                out.append(dets)
        return out

    def _embed_crops(self, pil_images):
        import torch
        batch = torch.stack([self.clip_pre(im) for im in pil_images]).to("cuda:0")
        with torch.no_grad():
            feats = self.clip.encode_image(batch)
        feats = feats / feats.norm(dim=-1, keepdim=True)
        return feats.cpu().numpy()

    def _text_embed(self, prompts):
        import torch
        toks = self.clip_tok(prompts).to("cuda:0")
        with torch.no_grad():
            feats = self.clip.encode_text(toks)
        feats = feats / feats.norm(dim=-1, keepdim=True)
        return feats.cpu().numpy()

    # ---- entities ----

    def _match_entity(self, label, vec, ts):
        rows = self.db.query(
            "SELECT id, centroid, n FROM entities WHERE label=?", (label,))
        best_id, best_sim = None, 0.0
        for r in rows:
            sim = float(np.dot(vec, _from_blob(r["centroid"])))
            if sim > best_sim:
                best_id, best_sim = r["id"], sim
        if best_id and best_sim >= self.cfg["entity_match_threshold"]:
            row = next(r for r in rows if r["id"] == best_id)
            c = _from_blob(row["centroid"]) * row["n"] + vec
            c = c / np.linalg.norm(c)
            self.db.execute("UPDATE entities SET centroid=?, n=n+1, last_seen=? "
                            "WHERE id=?", (_to_blob(c), ts, best_id))
            return best_id
        cur = self.db.execute(
            "INSERT INTO entities(label, centroid, n, first_seen, last_seen) "
            "VALUES(?,?,1,?,?)", (label, _to_blob(vec), ts, ts))
        return cur.lastrowid

    # ---- zero-shot tags ----

    def _tag_vectors(self):
        rows = self.db.query("SELECT id, name, prompt, threshold FROM tags "
                             "WHERE enabled=1 AND prompt IS NOT NULL")
        stamp = hash(tuple((r["id"], r["prompt"], r["threshold"]) for r in rows))
        if stamp != self._tags_loaded_at:
            self._tags = []
            if rows:
                vecs = self._text_embed([r["prompt"] for r in rows])
                self._tags = [(r["id"], r["name"], r["threshold"], v)
                              for r, v in zip(rows, vecs)]
            self._tags_loaded_at = stamp
        return self._tags

    # ---- the main entry points ----

    def _persist(self, frame, dets, event_id=None):
        """Crops, embeddings, entity match, tag scores, DB rows for one
        frame's (non-static) detections. Caller holds gpu_lock."""
        from PIL import Image
        img = Image.open(frame["path"]).convert("RGB")
        crops = [img.crop(box) for _, _, box in dets]
        vecs = self._embed_crops(crops)
        tags = self._tag_vectors()
        crop_dir = os.path.join(self.data_dir, "crops", frame["camera"],
                                time.strftime("%Y%m%d", time.localtime(frame["ts"])))
        os.makedirs(crop_dir, exist_ok=True)
        for i, ((label, conf, box), vec, crop) in enumerate(zip(dets, vecs, crops)):
            crop_path = os.path.join(crop_dir, f"{frame['id']}-{i}.jpg")
            crop.save(crop_path, quality=85)
            entity_id = self._match_entity(label, vec, frame["ts"])
            cur = self.db.execute(
                "INSERT INTO detections(frame_id, event_id, camera, ts, label,"
                " conf, box, crop_path, embedding, entity_id)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (frame["id"], event_id, frame["camera"], frame["ts"], label,
                 conf, json.dumps(box), crop_path, _to_blob(vec), entity_id))
            det_id = cur.lastrowid
            for tag_id, _name, thr, tvec in tags:
                score = float(np.dot(vec, tvec))
                if score >= thr:
                    self.db.execute(
                        "INSERT OR IGNORE INTO detection_tags"
                        "(detection_id, tag_id, score, source)"
                        " VALUES(?,?,?,'auto')", (det_id, tag_id, score))

    def analyze_frames(self, frames, event_id=None):
        """frames: rows with id/camera/ts/path. Detect (with scenery
        suppression across the batch), embed, match, tag. Returns the frames
        that had at least one live detection."""
        self._ensure_models()
        with self.gpu_lock:
            per_frame = self._detect([f["path"] for f in frames])
            masks = _static_mask(per_frame, self.cfg["static_presence"])
            hits = []
            for frame, dets, static in zip(frames, per_frame, masks):
                live = [d for k, d in enumerate(dets) if k not in static]
                if live:
                    hits.append(frame)
                    self._persist(frame, live, event_id)
                self.db.execute("UPDATE frames SET analyzed=1 WHERE id=?", (frame["id"],))
        return hits

    def analyze_sweep_chunk(self, camera, start, tmp_dir):
        """Detect over a sweep chunk's keyframes. Frames with a confident
        non-static detection are kept (moved into data/frames/sweep/...),
        recorded, and clustered into sweep-source events; the rest stay in
        tmp_dir and die with it. Returns (seen, kept, events_created)."""
        names = sorted(os.listdir(tmp_dir))
        if not names:
            return 0, 0, 0
        self._ensure_models()
        keep_thr = self.cfg["sweep_keep_threshold"]
        with self.gpu_lock:
            per_frame = self._detect([os.path.join(tmp_dir, n) for n in names])
        masks = _static_mask(per_frame, self.cfg["static_presence"])
        kept = []  # (frame row, live dets)
        stamp = time.strftime("%Y%m%d-%H%M", time.localtime(start))
        keep_dir = os.path.join(self.data_dir, "frames", "sweep", camera, stamp)
        for i, (name, dets, static) in enumerate(zip(names, per_frame, masks)):
            live = [d for k, d in enumerate(dets) if k not in static]
            if not any(conf >= keep_thr for _, conf, _ in live):
                continue
            os.makedirs(keep_dir, exist_ok=True)
            path = os.path.join(keep_dir, name)
            os.replace(os.path.join(tmp_dir, name), path)
            ts = start + i  # keyframes are 1/s
            cur = self.db.execute(
                "INSERT INTO frames(camera, ts, path, analyzed) VALUES(?,?,?,1)",
                (camera, ts, path))
            kept.append(({"id": cur.lastrowid, "camera": camera, "ts": ts,
                          "path": path}, live))
        if not kept:
            return len(names), 0, 0
        with self.gpu_lock:
            for frame, live in kept:
                self._persist(frame, live)  # already detected; no second pass
        events = self._cluster_sweep_events(camera, [f for f, _ in kept])
        return len(names), len(kept), events

    def _cluster_sweep_events(self, camera, kept):
        gap = self.cfg["sweep_event_gap_secs"]
        groups, current = [], [kept[0]]
        for f in kept[1:]:
            if f["ts"] - current[-1]["ts"] > gap:
                groups.append(current)
                current = [f]
            else:
                current.append(f)
        groups.append(current)
        for g in groups:
            start, end = g[0]["ts"], g[-1]["ts"]
            event_id = f"sweep-{camera}-{int(start)}"
            ids = [f["id"] for f in g]
            marks = ",".join("?" * len(ids))
            labels = self.db.query(
                f"SELECT label, COUNT(*) n, MAX(conf) top FROM detections "
                f"WHERE frame_id IN ({marks}) GROUP BY label ORDER BY n DESC", ids)
            self.db.execute(
                "INSERT OR REPLACE INTO events(id, source, camera, label, start_ts,"
                " end_ts, top_score, status, created_at)"
                " VALUES(?,?,?,?,?,?,?,'analyzed',?)",
                (event_id, "sweep", camera, labels[0]["label"] if labels else None,
                 start, end, labels[0]["top"] if labels else None, time.time()))
            self.db.execute(f"UPDATE frames SET event_id=? WHERE id IN ({marks})",
                            [event_id] + ids)
            self.db.execute(f"UPDATE detections SET event_id=? WHERE frame_id IN ({marks})",
                            [event_id] + ids)
            log.info("sweep event %s: %s, %d frames, %ds",
                     event_id, labels[0]["label"] if labels else "?", len(g),
                     int(end - start))
        return len(groups)
