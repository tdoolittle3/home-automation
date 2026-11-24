#!/usr/bin/env python3
"""Build the self-contained HTML comparison report from compare_models.py output.

    python make_report.py results/compare-2026-11-23.json \
        --baseline results/baseline-oldbuild-2026-11-23.json \
        --findings findings-2026-11-23.html --build 2392423 --out report-2026-11-23.html

--findings is an HTML fragment of <li> items (written by hand after reading
the numbers). Stdlib only; charts are drawn client-side from the embedded JSON.
"""
import argparse
import json
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("results")
    ap.add_argument("--baseline")
    ap.add_argument("--limit", help="same models at a lower answer cap, for the 4k-vs-16k table")
    ap.add_argument("--findings")
    ap.add_argument("--build", default="")
    ap.add_argument("--date", default=time.strftime("%Y-%m-%d"))
    ap.add_argument("--out", default="report.html")
    a = ap.parse_args()

    def load(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)

    tpl = open(os.path.join(HERE, "report_template.html"), encoding="utf-8").read()
    findings = open(a.findings, encoding="utf-8").read() if a.findings else ""
    out = (tpl.replace("__DATA__", json.dumps(load(a.results)))
              .replace("__BASELINE__", json.dumps(load(a.baseline) if a.baseline else []))
              .replace("__LIMIT__", json.dumps(load(a.limit) if a.limit else []))
              .replace("__FINDINGS__", findings)
              .replace("__BUILD__", a.build)
              .replace("__DATE__", a.date))
    with open(a.out, "w", encoding="utf-8", newline="\n") as f:
        f.write(out)
    print(f"wrote {a.out} ({len(out) // 1024} KB)")


if __name__ == "__main__":
    main()
