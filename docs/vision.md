# Vision pipeline — second-stage camera analysis on snibbly

A deeper detection/classification stage for the Frigate cameras, running on snibbly's
GTX 1050 Ti (GPU 1). Frigate stays the real-time motion detector; this pipeline is for what
Frigate misses (its 300×300 SSDLite detector skips plenty — raccoons, notably) and for what it
can't do: re-identifying recurring cars/people/animals and custom tagging, browsable in its own
web view.

**Status: Phases 0–2 complete (2026-10-21).** GPU 1 is carved out, the ingest worker runs on
snibbly, and every frame now gets detection, embeddings, entity clustering, and tag scoring.
No UI yet — the data accrues in SQLite until Phase 3.

## The plan

| Phase | What | Status |
|---|---|---|
| 0 | Hand GPU 1 to vision (drop the 4B LLMs), validate an sm_61 runtime | **done 2026-10-20** |
| 1 | Ingest worker: MQTT fast path + recordings sweep, frame sampling, job queue | **done 2026-10-20** |
| 2 | Detection (YOLO11s), embeddings (CLIP), entity clustering, zero-shot + manual tags | **done 2026-10-21** |
| 3 | Web view: event browser, entity pages, tag management (FastAPI + htmx, on snibbly) | — |
| 4 | Kuma monitor, backfill, ops polish | — |

Two ingest paths feed one database, each event marked by source:

* **Fast path** — subscribe to `frigate/events` on ladybird's Mosquitto; on event end, pull the
  full-resolution clip (`http://192.168.0.13:5000/api/events/<id>/clip.mp4` — the record stream:
  driveway 2688×1520 H.265, backyard 2MP; snapshots are detect-stream res and not worth
  analyzing) and sample frames densely around the event.
* **Sweep path** — periodically pull the continuous recordings (retained 2 days at full quality)
  and sample **1 fps**, running detection on everything, Frigate's opinion not required. The
  cameras record with a 1-second GOP, so 1 fps sampling is keyframe-only extraction — no full
  decode. Where detections cluster in time, the sweep creates its own events. Downtime is
  caught up from the retention window, which a live-RTSP tap could not do.

## How it runs (Phase 1)

One systemd service on snibbly, `vision-worker`, user `vision`
([unit](../snibbly/etc/systemd/system/vision-worker.service),
[worker](../snibbly/vision/vision_worker.py)). Config at `/etc/vision/config.toml`
([tracked copy](../snibbly/etc/vision/config.toml)) — read once at startup, so restart after
editing. No secrets: the Frigate API and Mosquitto are unauthenticated LAN services.

```bash
curl http://192.168.0.19:8098/            # health + queue depth + sweep lag + counters
sudo journalctl -u vision-worker -f       # one line per event sampled / sweep chunk done
```

Layout on the box: `/opt/vision/venv` (uv Python 3.12 + paho-mqtt; Phase 2 adds torch),
`/opt/vision/vision_worker.py`, and `/opt/vision/data/` (vision-owned) holding `vision.db`
(SQLite, WAL) and `frames/events/<camera>/<event_id>/*.jpg`. Everything flows through a jobs
table in the DB — queue survives restarts, event jobs outrank sweep jobs, failures retry on a
backoff then park as `error`.

Verified at deploy (2026-10-20): a real driveway car event sampled to 2688×1520 frames
(record-stream resolution — the whole point), sweep chunks each yield 1199 keyframes
(1/s exactly) and take ~2 min of mostly-network time, and steady-state sweeping costs ~12 min
of wall-time per hour. Sweep frames are counted and **discarded** until Phase 2's detector
decides which are worth keeping (`keep_frames = false`); event frames are kept 30 days, future
sweep keepers 7 (retention sweep runs daily).

Operational facts that bit once already:

* **Snibbly is on WiFi** (`wlo1`; `eno2` is unplugged). The 2026-10-20 evening "outage" — box
  unreachable for hours, no crash evidence — was almost certainly a wireless drop, and the sweep
  streams gigabytes per hour over that link. Cable it.
* The worker's `:8098` health endpoint answers locally but snibbly's **ufw blocks it from the
  LAN**. Before pointing Kuma at it:
  `sudo ufw allow from 192.168.0.0/24 to any port 8098 proto tcp comment "vision-worker health"`

Two deploy gotchas, both already baked into the tracked files:

* The unit sets `ProtectHome=true`, and a uv venv symlinks its interpreter into
  `~/.local/share/uv` by default — the service dies with `status=203/EXEC`. The managed
  interpreter must live outside /home: `UV_PYTHON_INSTALL_DIR=/opt/uv-python uv python
  install 3.12`, then create the venv with that same env var set.
* `/tmp` on snibbly is a 16 GB **tmpfs** — don't put venvs or sweep scratch there. The worker
  scratches under `/opt/vision/data/tmp`.

## What analysis produces (Phase 2)

`analyzer.py` runs on everything Phase 1 ingests, fp32 on the 1050 Ti (the unit sets
`CUDA_VISIBLE_DEVICES=1`, so `cuda:0` in code is always the vision card; YOLO11s + CLIP
ViT-B/16 load in ~2 s and hold ~700 MiB VRAM):

* **Detections** — YOLO11s over every frame; each detection ≥0.4 confidence becomes a row with
  box, crop JPEG (`data/crops/…`, kept indefinitely — they're ~20 KB and feed the galleries),
  and a 512-d CLIP embedding.
* **Entities** ("have I seen this car before") — each embedding cosine-matches per-label entity
  centroids: ≥0.85 joins and nudges the centroid (running mean); below founds a new unnamed
  entity. Naming/merging is Phase 3 UI. It works: the first live run clustered two distinct
  recurring people (347 and 111 sightings) unprompted. Persons use CLIP like everything else —
  OSNet was the plan but torchreid won't install on py3.12/torch-2.14, so person matching is
  clothing-level for now.
* **Zero-shot tags** — any row in the `tags` table with a `prompt` is CLIP-text-embedded and
  scored against every crop; scores ≥ the tag's threshold are recorded in `detection_tags`.
  Manual tags use the same table with `prompt NULL` (Phase 3 UI). Expect thresholds to need
  tuning per prompt (~0.22–0.30 cosine is the useful range).
* **Sweep events** — sweep keyframes with a confident detection survive and cluster (90 s gap)
  into `sweep`-source events; this is the Frigate-independent path, so things Frigate never
  noticed become first-class events.

**Scenery suppression** (`static_presence`, the lesson of the first live run): YOLO on a static
scene is confidently wrong — a backyard fixture read as "surfboard" in every keyframe, so the
first chunk kept 1200/1200 frames as one 20-minute "surfboard event", and a parked car was
re-detected 470 times. A detection whose label+box (IoU ≥ 0.6) recurs in ≥50 % of a batch's
frames is scenery: suppressed from keeps, events, and entities. Same chunk after the fix:
2 frames kept, one real 28-second car event. Suppression needs ≥8 frames of context, so short
event batches are untouched.

Known rough edges: brief detect-only Frigate events can 404 from its API before the worker
fetches them (they park as `error`; the sweep covers those spans anyway), and OpenAI CLIP
weights must load as `ViT-B-16-quickgelu` — the plain variant silently uses the wrong
activation.

## Phase 0 results — what runs on a Pascal card in 2026

Everything below was measured on snibbly 2026-10-20
([bench_sm61.py](../snibbly/vision/bench_sm61.py); scratch venv in `/tmp/torch-venv`, the
durable install comes with Phase 1).

**The stack: uv-managed Python 3.12 + `torch 2.14.1+cu126`.** Two dead ends first:

* **onnxruntime-gpu ≥ 1.31** — built against CUDA 13, which removed Pascal entirely.
* **onnxruntime-gpu 1.29 CUDA-12 build** (ms aiinfra `onnxruntime-cuda-12` pip feed) — the CUDA
  EP initializes on the 1050 Ti, but every Conv fails inside cuDNN 9's graph/frontend API, which
  requires Volta+. No runtime fallback to the legacy path exists.
* **torch cu126** still ships sm_50/sm_60 SASS (sm_61 executes sm_60 binaries), and its conv path
  works on Pascal. The cu126 wheel line is the last one that does — later lines (cu128+) are
  Volta+. Snibbly's system python is 3.14, which cu126 wheels don't cover; `uv` supplies 3.12.

| Measurement | Result |
|---|---|
| Synthetic conv stack @640, fp32 | 7.3 ms/frame, max abs diff vs CPU 7e-08 |
| YOLO11s @640, fp32, end-to-end | 26.5 ms/frame (37.8 fps), 224 MiB VRAM |
| Daily sweep (2 cams × 86.4k keyframes) | ~76 min of GPU time |

**Stay fp32.** Pascal runs fp16 at 1/64 rate — half precision here is a ~60× slowdown, not an
optimization. The VRAM numbers above are fp32 and tiny; the full Phase-2 working set (YOLO +
OSNet + CLIP ViT-B/16, all fp32) projects to ~2 GB of the 4 GB card.

## What Phase 0 changed on the LLM side

GPU 1's previous tenants — `small-4b` and `small-4b-abliterated` — were removed from llama-swap
(GGUFs remain in `/opt/models/`). The agent-runner's `delegate` tool now runs subtasks on the
**same 8B's second slot** (`-np 2`) instead of a 4B partner: slot concurrency and context
hygiene survive, the weaker model doesn't. Each slot sees half the context (8k for `fast-8b`).
Verified live: `fast-8b` serves, `small-4b` 404s, a delegated task round-trips.

One residual coupling, documented in [config.yaml](../snibbly/etc/llama-swap/config.yaml):
`big-14b`'s 7/1 tensor split still borrows ~1.1 GB of GPU 1. Alongside vision's ~2 GB that fits,
but with little headroom — if vision ever OOMs while `big-14b` is loaded, that split is the
first suspect (fix: drop `big-14b` or rebalance its split).

## Rollback

On snibbly: `sudo cp /etc/llama-swap/config.yaml.bak-20261020 /etc/llama-swap/config.yaml` and
`cp /tmp/runner.py.bak-20261020 /opt/agent-runner/runner.py` (the runner backup is in /tmp and
dies on reboot; the git history of this repo is the durable copy).
