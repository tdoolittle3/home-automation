#!/usr/bin/env python3
"""Daily stock report — deterministic generator with ONE bounded LLM call.

Run by Hermes cron (`--script daily_stock_report.py --no-agent`). Pulls the
portfolio-advisor MCP tools over HTTP, renders an HTML dashboard into the served
reports dir, asks the local model for a short "what to look at" note (the only
LLM step — bounded, so no agentic-loop hallucination), and pushes an ntfy link.

Research aid, NOT financial advice. Uses only values the tools return.

Stdlib only. Env: OPENAI_API_KEY (litellm), NTFY_URL. Writes to /opt/data/reports
(mounted to the host reports dir that nginx serves on :9123).
"""
import datetime
import json
import os
import urllib.request

MCP_URL = os.environ.get("PORTFOLIO_MCP_URL", "http://192.168.0.13:8765/mcp")
LLM_URL = os.environ.get("OPENAI_BASE_URL", "http://192.168.0.19:8080/v1").rstrip("/") + "/chat/completions"
LLM_KEY = os.environ.get("OPENAI_API_KEY", "")
LLM_MODEL = os.environ.get("REPORT_LLM_MODEL", "coder-30b")
NTFY_URL = os.environ.get("NTFY_URL", "")
REPORT_DIR = os.environ.get("REPORT_DIR", "/opt/data/reports")
PUBLIC_BASE = "https://ladybird.tail0e6756.ts.net:9123"
TODAY = datetime.date.today().isoformat()

# ---------- minimal MCP streamable-http client ----------
_sess = {"id": None, "n": 0}


def _mcp_post(payload):
    h = {"Content-Type": "application/json",
         "Accept": "application/json, text/event-stream"}
    if _sess["id"]:
        h["mcp-session-id"] = _sess["id"]
    req = urllib.request.Request(MCP_URL, data=json.dumps(payload).encode(), headers=h)
    with urllib.request.urlopen(req, timeout=90) as r:
        sid = r.headers.get("mcp-session-id")
        if sid:
            _sess["id"] = sid
        ct = r.headers.get("Content-Type", "")
        body = r.read().decode()
    if "text/event-stream" in ct:
        for line in body.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        return None
    return json.loads(body) if body.strip() else None


def _rpc(method, params=None):
    _sess["n"] += 1
    return _mcp_post({"jsonrpc": "2.0", "id": _sess["n"], "method": method,
                      "params": params or {}})


def mcp_init():
    _rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                        "clientInfo": {"name": "daily-stock-report", "version": "1"}})
    _mcp_post({"jsonrpc": "2.0", "method": "notifications/initialized"})


def tool(name, **args):
    """Call an MCP tool; return its text payload (or an error marker string)."""
    try:
        res = _rpc("tools/call", {"name": name, "arguments": args})
    except Exception as exc:  # noqa: BLE001
        return f"[error calling {name}: {exc}]"
    if not res or "result" not in res:
        err = (res or {}).get("error", {})
        return f"[no result from {name}: {err}]"
    texts = [c.get("text", "") for c in res["result"].get("content", [])
             if c.get("type") == "text"]
    return "\n".join(texts).strip() or "[empty]"


def jtool(name, **args):
    """Call a tool whose text payload is JSON; return parsed obj or None."""
    raw = tool(name, **args)
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


# ---------- one bounded LLM call ----------
def llm_note(context):
    if not LLM_KEY:
        return "(commentary skipped: no model key)"
    sys = ("You are a portfolio research assistant — research only, NOT financial "
           "advice. Use ONLY the data provided; never invent numbers. Always compare "
           "ideas to simply holding the benchmark. Be concise.")
    usr = ("From this portfolio data, write a SHORT 'what to look at today' note: "
           "4-6 bullets covering underweight-vs-target names worth researching, any "
           "tax-lot timing, and what would change the view. No trading advice.\n\n"
           + context[:12000])
    body = json.dumps({"model": LLM_MODEL,
                       "messages": [{"role": "system", "content": sys},
                                    {"role": "user", "content": usr}],
                       "max_tokens": 600, "temperature": 0.3}).encode()
    req = urllib.request.Request(LLM_URL, data=body, headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + LLM_KEY})
    try:
        with urllib.request.urlopen(req, timeout=240) as r:
            d = json.load(r)
        return (d["choices"][0]["message"].get("content") or "").strip() or "(empty)"
    except Exception as exc:  # noqa: BLE001
        return f"(commentary unavailable: {exc})"


# ---------- html ----------
def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def section(title, body_html):
    return f'<section><h2>{esc(title)}</h2>{body_html}</section>'


def pre(text):
    return f'<pre>{esc(text)}</pre>'


def render(parts, note, stale):
    banner = ('<div class="warn">⚠ Data may be stale (E*TRADE token likely '
              'expired overnight — renew for fresh numbers).</div>' if stale else '')
    body = "\n".join(section(t, b) for t, b in parts)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Daily Stock Report — {TODAY}</title>
<style>
 body{{font-family:system-ui,sans-serif;background:#0b0f17;color:#e5e7eb;margin:0;padding:1.2rem;line-height:1.5}}
 h1{{font-size:1.4rem;margin:.2rem 0}} h2{{font-size:1.05rem;color:#93c5fd;border-bottom:1px solid #1f2937;padding-bottom:.3rem;margin-top:1.6rem}}
 .meta{{color:#9ca3af;font-size:.85rem}} .warn{{background:#7f1d1d;color:#fecaca;padding:.6rem .8rem;border-radius:.4rem;margin:.8rem 0}}
 pre{{white-space:pre-wrap;background:#111827;padding:.8rem;border-radius:.4rem;font-size:.82rem;overflow-x:auto}}
 .note{{background:#0f1b2d;border-left:3px solid #3b82f6;padding:.8rem;border-radius:.3rem}}
 footer{{color:#6b7280;font-size:.8rem;margin-top:2rem;border-top:1px solid #1f2937;padding-top:.6rem}}
</style></head><body>
<h1>\U0001f4c8 Daily Stock Report</h1>
<div class="meta">{TODAY} · generated by the homelab agent · research aid, not advice</div>
{banner}
{section("What to look at today (AI note)", f'<div class="note"><pre>{esc(note)}</pre></div>')}
{body}
<footer>Research aid, not financial advice. Numbers come straight from the
portfolio-advisor tools; the "what to look at" note is model-generated commentary.
Past performance does not predict future results.</footer>
</body></html>"""


# ---------- main ----------
def main():
    mcp_init()
    conn = tool("get_connection_status")
    latest = tool("get_latest_report")
    positions = tool("get_positions")
    tax = tool("get_tax_summary")
    strategy = tool("get_strategy")
    track = tool("get_track_record")

    stale = "expired" in conn.lower() or "stale" in conn.lower() or "fail" in conn.lower()

    parts = [
        ("Connection / freshness", pre(conn)),
        ("Positions", pre(positions)),
        ("Latest report", pre(latest)),
        ("Tax summary", pre(tax)),
        ("Strategy (targets + benchmark)", pre(strategy)),
        ("Track record", pre(track)),
    ]

    context = (f"CONNECTION:\n{conn}\n\nPOSITIONS:\n{positions}\n\n"
               f"LATEST REPORT:\n{latest}\n\nTAX:\n{tax}\n\nSTRATEGY:\n{strategy}\n\n"
               f"TRACK RECORD:\n{track}")
    note = llm_note(context)

    html = render(parts, note, stale)
    os.makedirs(REPORT_DIR, exist_ok=True)
    dated = os.path.join(REPORT_DIR, f"stock-report-{TODAY}.html")
    for path in (dated, os.path.join(REPORT_DIR, "latest.html")):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(html)

    url = f"{PUBLIC_BASE}/stock-report-{TODAY}.html"
    summary = note.splitlines()[0] if note else "Daily portfolio digest ready."

    if NTFY_URL:
        try:
            req = urllib.request.Request(
                NTFY_URL, data=f"{summary}\n\nFull report: {url}".encode(),
                headers={"Title": f"Daily Stock Report - {TODAY}",
                         "Click": url, "Tags": "chart_with_upwards_trend"})
            urllib.request.urlopen(req, timeout=30).read()
            ntfy_ok = "sent"
        except Exception as exc:  # noqa: BLE001
            ntfy_ok = f"FAILED ({exc})"
    else:
        ntfy_ok = "skipped (no NTFY_URL)"

    # stdout -> cron delivery (Telegram)
    print(f"Daily Stock Report {TODAY} ready{' (stale data)' if stale else ''}.")
    print(f"Dashboard: {url}")
    print(f"ntfy: {ntfy_ok}")
    print()
    print(note)


if __name__ == "__main__":
    main()
