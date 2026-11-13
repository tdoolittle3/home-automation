#!/usr/bin/env python3
"""ADS-B summary for the radio-logs skill — run it, don't eyeball the JSONL.

    python3 /opt/data/skills/homelab/radio-logs/scripts/adsb_summary.py 60          # last 60 minutes
    python3 /opt/data/skills/homelab/radio-logs/scripts/adsb_summary.py 2026-11-11  # that whole day
    python3 /opt/data/skills/homelab/radio-logs/scripts/adsb_summary.py             # newest day file

A file, not a heredoc snippet: Hermes's approval guard blocks `python3 - <<EOF`
(and `python -c`) as script execution, so the inline version never ran
unattended (nightly eval, 2026-11-12) and the agent fell back to `tail` and
guessed at "the last hour". Reads the read-only mount the radiolog stack
writes: one JSONL per local day, one line per aircraft visit, written ~5 min
after the aircraft leaves coverage.
"""
import datetime
import glob
import json
import os
import sys

ADSB_DIR = os.environ.get("ADSB_DIR", "/opt/data/workspace/radiolog/adsb")


def load(arg, now=None, adsb_dir=ADSB_DIR):
    """Returns (records newest-first, cutoff or None, files read)."""
    files = sorted(glob.glob(os.path.join(adsb_dir, "*.jsonl")))
    cutoff = None
    if arg.isdigit():  # last N minutes: newest two day-files, so midnight is covered
        now = now or datetime.datetime.now(datetime.timezone.utc)
        files, cutoff = files[-2:], now - datetime.timedelta(minutes=int(arg))
    elif arg:
        files = [os.path.join(adsb_dir, arg + ".jsonl")]
    else:
        files = files[-1:]
    recs = []
    for f in files:
        if not os.path.exists(f):
            continue
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                try:
                    x = json.loads(line)
                    seen = datetime.datetime.fromisoformat(x["last_seen"])
                except (ValueError, KeyError):
                    continue
                if cutoff is None or seen >= cutoff:
                    x["_seen"] = seen
                    recs.append(x)
    recs.sort(key=lambda x: x["_seen"], reverse=True)
    return recs, cutoff, files


def show(x):
    def v(key, unit):
        return f"{x[key]}{unit}" if x.get(key) is not None else "?"
    return (f'{x.get("flight") or x["hex"]} hex={x["hex"]} alt={v("min_alt", "ft")} '
            f'dist={v("min_dist_nm", "nm")} gs={v("max_gs", "kt")} '
            f'{x["first_seen"][11:16]}-{x["last_seen"][11:16]}')


def report(recs, cutoff, files):
    lines = []
    if cutoff is not None:
        local = recs[0]["_seen"].tzinfo if recs else None  # house time, from the data
        start = cutoff.astimezone(local) if local else cutoff
        lines.append(f"last {int((datetime.datetime.now(datetime.timezone.utc) - cutoff).total_seconds() // 60)} min "
                     f"(since {start:%H:%M} house time) - {len(recs)} aircraft, "
                     f"{sum(1 for x in recs if x.get('flight'))} with callsign")
        lines.append("ALL IN WINDOW (newest first):")
        lines += [f"  {show(x)}" for x in recs[:40]]
        if len(recs) > 40:
            lines.append(f"  ... {len(recs) - 40} more")
    else:
        lines.append(f"{', '.join(os.path.basename(f) for f in files)} - {len(recs)} aircraft, "
                     f"{sum(1 for x in recs if x.get('flight'))} with callsign")

    def top(key, rev=False):
        return sorted((x for x in recs if x.get(key) is not None),
                      key=lambda x: x[key], reverse=rev)[:5]
    for title, key, rev in (("LOWEST", "min_alt", False), ("CLOSEST", "min_dist_nm", False),
                            ("FASTEST", "max_gs", True)):
        lines.append(f"{title}:")
        lines += [f"  {show(x)}" for x in top(key, rev)]
    return "\n".join(lines)


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    print(report(*load(arg)))
