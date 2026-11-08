#!/usr/bin/env python3
"""Sustained LLM generation load — the freeze repro from 2026-10-28.

Drives concurrent long generations through the litellm door (:8080) for a
fixed duration and prints per-request throughput. Freeze #5 killed the box
in under 60 seconds of this load profile with RAM at XMP 3600 MT/s; a clean
multi-minute run after the BIOS drop to 2666 is the pass signal.

Stdlib only — no venv needed. Reads the API key from $LLAMA_API_KEY or --key.

    LLAMA_API_KEY=... python3 bench_llm.py --model big-30b-thinking --duration 360
"""
import argparse
import json
import os
import sys
import threading
import time
import urllib.request

PROMPT = (
    "Write an exhaustive, detailed history of computing from the 1940s to "
    "today. Cover every decade thoroughly: hardware, software, networking, "
    "and the people involved. Keep going until you run out of room."
)


def worker(wid, args, deadline, stats, lock):
    n = 0
    while time.time() < deadline:
        body = json.dumps({
            "model": args.model,
            "messages": [{"role": "user", "content": PROMPT}],
            "max_tokens": args.max_tokens,
            "temperature": 0.8,
        }).encode()
        req = urllib.request.Request(
            args.base_url.rstrip("/") + "/v1/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + args.key,
            },
        )
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=args.request_timeout) as r:
                out = json.load(r)
            dt = time.time() - t0
            toks = out.get("usage", {}).get("completion_tokens") or 0
            with lock:
                stats["requests"] += 1
                stats["tokens"] += toks
            print(
                f"[{time.strftime('%H:%M:%S')}] w{wid} req{n}: "
                f"{toks} tok in {dt:.1f}s ({toks / dt:.1f} tok/s)",
                flush=True,
            )
        except Exception as e:
            with lock:
                stats["errors"] += 1
            print(f"[{time.strftime('%H:%M:%S')}] w{wid} ERROR: {e}", flush=True)
            time.sleep(2)
        n += 1


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-url", default="http://127.0.0.1:8080")
    p.add_argument("--model", default="fast-8b")
    p.add_argument("--concurrency", type=int, default=2)
    p.add_argument("--duration", type=int, default=240, help="seconds of load")
    p.add_argument("--max-tokens", type=int, default=1024)
    p.add_argument("--request-timeout", type=int, default=300,
                   help="per-request timeout; first request includes model load")
    p.add_argument("--key", default=os.environ.get("LLAMA_API_KEY", ""))
    args = p.parse_args()
    if not args.key:
        sys.exit("no API key: set LLAMA_API_KEY or pass --key")

    stats = {"requests": 0, "tokens": 0, "errors": 0}
    lock = threading.Lock()
    start = time.time()
    deadline = start + args.duration
    print(
        f"[{time.strftime('%H:%M:%S')}] bench start: model={args.model} "
        f"concurrency={args.concurrency} duration={args.duration}s",
        flush=True,
    )
    threads = [
        threading.Thread(target=worker, args=(i, args, deadline, stats, lock))
        for i in range(args.concurrency)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.time() - start
    print(
        f"[{time.strftime('%H:%M:%S')}] bench done: {stats['requests']} requests, "
        f"{stats['tokens']} tokens, {stats['errors']} errors in {elapsed:.0f}s "
        f"({stats['tokens'] / elapsed:.1f} tok/s aggregate)",
        flush=True,
    )
    sys.exit(1 if stats["errors"] and not stats["requests"] else 0)


if __name__ == "__main__":
    main()
