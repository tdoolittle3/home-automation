#!/usr/bin/env python3
"""Render the bench GPU telemetry CSV to a standalone SVG (no deps).

Reads raw/gpu_telemetry_<date>.csv (nvidia-smi CSV: timestamp, index, power,
temp, util, mem) and draws GPU0 power + temperature over time, with the two
bench phases shaded. Pure stdlib so it runs anywhere.

    python plot_telemetry.py raw/gpu_telemetry_2026-11-07.csv gpu_power_temp.svg
"""
import sys
from datetime import datetime

# bench phase clock times (from bench_llm.py stdout, 2026-11-07)
PHASES = [
    ("fast-8b x2", "22:43:05", "22:47:22", "#3b82f6"),
    ("big-30b-thinking", "22:47:33", "22:53:36", "#8b5cf6"),
]


def parse(path):
    rows = []
    with open(path) as f:
        for line in f:
            p = [c.strip() for c in line.split(",")]
            if len(p) < 6 or p[1] != "0":
                continue
            try:
                t = datetime.strptime(p[0], "%Y/%m/%d %H:%M:%S.%f")
                power = float(p[2].split()[0])
                temp = float(p[3])
                mem = float(p[5].split()[0])
            except (ValueError, IndexError):
                continue
            rows.append((t, power, temp, mem))
    return rows


def hhmmss(rows, clock):
    base = rows[0][0]
    h, m, s = map(int, clock.split(":"))
    target = base.replace(hour=h, minute=m, second=s, microsecond=0)
    return (target - base).total_seconds()


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else "raw/gpu_telemetry_2026-11-07.csv"
    out = sys.argv[2] if len(sys.argv) > 2 else "gpu_power_temp.svg"
    rows = parse(src)
    if not rows:
        sys.exit("no GPU0 rows parsed")

    t0 = rows[0][0]
    xs = [(r[0] - t0).total_seconds() for r in rows]
    pw = [r[1] for r in rows]
    tp = [r[2] for r in rows]
    xmax = max(xs)
    PW_MAX, TP_MAX = 250.0, 100.0  # fixed axes: 2070S TDP ~215W, thermal 100C

    W, H = 960, 420
    L, R, T, B = 60, 55, 50, 45
    pw_, ph_ = W - L - R, H - T - B

    def px(x):
        return L + (x / xmax) * pw_

    def py_pw(v):
        return T + ph_ - (v / PW_MAX) * ph_

    def py_tp(v):
        return T + ph_ - (v / TP_MAX) * ph_

    s = []
    s.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
             f'font-family="system-ui,sans-serif" font-size="12">')
    s.append(f'<rect width="{W}" height="{H}" fill="#0b0f17"/>')
    s.append(f'<text x="{W/2}" y="24" fill="#e5e7eb" font-size="16" '
             f'text-anchor="middle">snibbly LLM bench — GPU0 (RTX 2070 Super) — 2026-11-07</text>')

    # phase shading
    for name, c0, c1, color in PHASES:
        x0, x1 = px(hhmmss(rows, c0)), px(hhmmss(rows, c1))
        s.append(f'<rect x="{x0:.1f}" y="{T}" width="{x1-x0:.1f}" height="{ph_}" '
                 f'fill="{color}" opacity="0.10"/>')
        s.append(f'<text x="{(x0+x1)/2:.1f}" y="{T+14}" fill="{color}" '
                 f'text-anchor="middle" font-size="11">{name}</text>')

    # gridlines + left axis (power) + right axis (temp)
    for frac in (0, .25, .5, .75, 1):
        y = T + ph_ - frac * ph_
        s.append(f'<line x1="{L}" y1="{y:.1f}" x2="{W-R}" y2="{y:.1f}" stroke="#1f2937"/>')
        s.append(f'<text x="{L-8}" y="{y+4:.1f}" fill="#60a5fa" text-anchor="end">'
                 f'{frac*PW_MAX:.0f}</text>')
        s.append(f'<text x="{W-R+8}" y="{y+4:.1f}" fill="#f59e0b">{frac*TP_MAX:.0f}</text>')
    s.append(f'<text x="{L-8}" y="{T-10}" fill="#60a5fa" text-anchor="end">Watts</text>')
    s.append(f'<text x="{W-R+8}" y="{T-10}" fill="#f59e0b">°C</text>')

    # x ticks every 120s
    tick = 120
    n = int(xmax // tick)
    for i in range(n + 1):
        x = px(i * tick)
        s.append(f'<line x1="{x:.1f}" y1="{T+ph_}" x2="{x:.1f}" y2="{T+ph_+5}" stroke="#4b5563"/>')
        s.append(f'<text x="{x:.1f}" y="{T+ph_+20}" fill="#9ca3af" text-anchor="middle">'
                 f'{i*tick//60}m</text>')

    def poly(vals, projy, color):
        pts = " ".join(f"{px(x):.1f},{projy(v):.1f}" for x, v in zip(xs, vals))
        s.append(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2"/>')

    poly(pw, py_pw, "#60a5fa")   # power
    poly(tp, py_tp, "#f59e0b")   # temp

    # peak annotations
    pmax_i = pw.index(max(pw))
    tmax_i = tp.index(max(tp))
    s.append(f'<circle cx="{px(xs[pmax_i]):.1f}" cy="{py_pw(pw[pmax_i]):.1f}" r="3" fill="#60a5fa"/>')
    s.append(f'<text x="{px(xs[pmax_i]):.1f}" y="{py_pw(pw[pmax_i])-8:.1f}" '
             f'fill="#93c5fd" text-anchor="middle">peak {pw[pmax_i]:.0f} W</text>')
    s.append(f'<text x="{px(xs[tmax_i]):.1f}" y="{py_tp(tp[tmax_i])-8:.1f}" '
             f'fill="#fcd34d" text-anchor="middle">peak {tp[tmax_i]:.0f}°C</text>')

    # legend
    s.append(f'<rect x="{L+10}" y="{H-28}" width="12" height="12" fill="#60a5fa"/>')
    s.append(f'<text x="{L+28}" y="{H-18}" fill="#cbd5e1">power (W)</text>')
    s.append(f'<rect x="{L+120}" y="{H-28}" width="12" height="12" fill="#f59e0b"/>')
    s.append(f'<text x="{L+138}" y="{H-18}" fill="#cbd5e1">temp (°C)</text>')
    s.append('</svg>')

    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(s))
    print(f"wrote {out}: {len(rows)} samples, span {xmax:.0f}s, "
          f"peak {max(pw):.0f}W / {max(tp):.0f}C, "
          f"idle {min(pw):.1f}W, VRAM max {max(r[3] for r in rows):.0f}MiB")


if __name__ == "__main__":
    main()
