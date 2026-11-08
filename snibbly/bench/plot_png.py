#!/usr/bin/env python3
"""Render the bench GPU telemetry CSV to a PNG (matplotlib).

Run on snibbly with the vision venv (has matplotlib):
    /opt/vision/venv/bin/python plot_png.py ~/gpu_bench.log gpu_power_temp.png
"""
import sys
from datetime import datetime
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

PHASES = [
    ("fast-8b x2", "22:43:05", "22:47:22", "#3b82f6"),
    ("big-30b-thinking", "22:47:33", "22:53:36", "#8b5cf6"),
]


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else "/home/thomas/gpu_bench.log"
    out = sys.argv[2] if len(sys.argv) > 2 else "gpu_power_temp.png"
    ts, pw, tp = [], [], []
    with open(src) as f:
        for line in f:
            p = [c.strip() for c in line.split(",")]
            if len(p) < 6 or p[1] != "0":
                continue
            try:
                t = datetime.strptime(p[0], "%Y/%m/%d %H:%M:%S.%f")
                ts.append(t)
                pw.append(float(p[2].split()[0]))
                tp.append(float(p[3]))
            except (ValueError, IndexError):
                continue
    t0 = ts[0]
    x = [(t - t0).total_seconds() / 60 for t in ts]  # minutes

    fig, ax1 = plt.subplots(figsize=(10, 4.4), dpi=150)
    fig.patch.set_facecolor("#0b0f17")
    ax1.set_facecolor("#0b0f17")

    for name, c0, c1, color in PHASES:
        def rel(c):
            h, m, s = map(int, c.split(":"))
            return (t0.replace(hour=h, minute=m, second=s, microsecond=0) - t0).total_seconds() / 60
        ax1.axvspan(rel(c0), rel(c1), color=color, alpha=0.12)
        ax1.text((rel(c0) + rel(c1)) / 2, 243, name, color=color, ha="center", fontsize=9)

    ax1.plot(x, pw, color="#60a5fa", lw=1.8, label="power (W)")
    ax1.set_ylabel("Power (W)", color="#60a5fa")
    ax1.set_ylim(0, 250)
    ax1.tick_params(colors="#9ca3af")
    ax1.set_xlabel("Elapsed (minutes)", color="#9ca3af")
    ax1.axhline(215, color="#60a5fa", ls=":", lw=0.8, alpha=0.5)

    ax2 = ax1.twinx()
    ax2.plot(x, tp, color="#f59e0b", lw=1.8, label="temp (°C)")
    ax2.set_ylabel("Temp (°C)", color="#f59e0b")
    ax2.set_ylim(0, 100)
    ax2.tick_params(colors="#f59e0b")

    ax1.annotate(f"peak {max(pw):.0f} W", (x[pw.index(max(pw))], max(pw)),
                 color="#93c5fd", fontsize=9, xytext=(0, 6), textcoords="offset points", ha="center")
    ax2.annotate(f"peak {max(tp):.0f}°C", (x[tp.index(max(tp))], max(tp)),
                 color="#fcd34d", fontsize=9, xytext=(0, 6), textcoords="offset points", ha="center")

    for sp in ax1.spines.values():
        sp.set_color("#374151")
    ax1.set_title("snibbly LLM bench — GPU0 (RTX 2070 Super) — 2026-11-07", color="#e5e7eb")
    fig.tight_layout()
    fig.savefig(out, facecolor=fig.get_facecolor())
    print(f"wrote {out}: {len(x)} samples, peak {max(pw):.0f}W / {max(tp):.0f}C")


if __name__ == "__main__":
    main()
