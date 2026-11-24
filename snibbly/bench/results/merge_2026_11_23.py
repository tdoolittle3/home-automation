#!/usr/bin/env python3
"""Merge the 2026-11-23 comparison runs (raw-2026-11-23/) into the report inputs.

The run was split after the first pass showed thinking models hitting the
4096-token answer cap, so the final set mixes caps on purpose:
  16k cap  — inkling-mini (+@low), qwen36-35b, qwen36-35b-heretic,
             big-30b-thinking, fast-8b (quality from the one-slot fast-8b-32k
             variant; speed/load from the production fast-8b 4k run)
  4k cap   — vl-30b, coder-30b, vlm-8b (instruct models; a cap they hit
             is a loop, not unfinished thinking)
coder-30b's speed comes from a separate speed-only run with the vision
captioner paused; in the main run the captioner shared the CPU (prefill 235
vs 305 t/s).

Writes compare-2026-11-23.json (one row per model) and
compare-2026-11-23-4k.json (the 4k first-pass rows, for the limit table).
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "raw-2026-11-23")


def runs(name):
    with open(os.path.join(RAW, f"compare-20261123-{name}.json"), encoding="utf-8") as f:
        return {r["spec"]: r for r in json.load(f)}


part1, part2 = runs("part1"), runs("part2")
q16, ink16, big16 = runs("qwen16k"), runs("ink16k"), runs("big16k")
vlm, fast16 = runs("vlm"), runs("fast16k")
coder_speed = runs("coderspeed")["coder-30b"]  # re-measured with the captioner paused


def cap(r, n):
    return dict(r, max_tokens=n)


fast = cap(fast16["fast-8b-32k"], 16384)
fast.update(spec="fast-8b", model="fast-8b", speed=part2["fast-8b"]["speed"],
            load_and_first_s=part2["fast-8b"]["load_and_first_s"],
            note="quality via fast-8b-32k (1 slot, 32k ctx); speed from production fast-8b")

main = [
    cap(ink16["inkling-mini"], 16384),
    cap(ink16["inkling-mini@low"], 16384),
    cap(q16["qwen36-35b"], 16384),
    cap(q16["qwen36-35b-heretic"], 16384),
    cap(big16["big-30b-thinking"], 16384),
    cap(part2["vl-30b"], 4096),
    dict(cap(part2["coder-30b"], 4096), speed=coder_speed["speed"],
         note="speed re-measured with the vision captioner paused (it shared the CPU in the main run)"),
    fast,
    cap(vlm["vlm-8b"], 4096),
]
limit = [cap(part1["inkling-mini"], 4096), cap(part1["inkling-mini@low"], 4096),
         cap(part1["qwen36-35b"], 4096), cap(part2["fast-8b"], 4096)]

for name, data in [("compare-2026-11-23.json", main), ("compare-2026-11-23-4k.json", limit)]:
    with open(os.path.join(HERE, name), "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, indent=1)
    print("wrote", name, [r["spec"] for r in data])
