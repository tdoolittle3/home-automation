#!/usr/bin/env python3
"""Nightly Hermes eval — golden questions against the live agent.

Run by Hermes cron (`--script hermes_eval.py --no-agent`, see docs/hermes.md
"Nightly eval"). For each question in hermes_eval_questions.json it:

  1. asks the real agent (`hermes chat -q ... --format stream-json`: same
     config, SOUL.md, skills and tools as Telegram) and records the tools it
     called, the answer, and the time taken;
  2. fetches ground truth at eval time — vision-mcp tools for camera facts,
     the Home Assistant REST API for device state — or uses fixed facts for
     anchored history ("the garbage truck came 09:53 on Wed Nov 11");
  3. grades it: deterministic checks first (which tools were called, a time
     within a few minutes, the right day, regexes), then for meaning-level
     criteria ONE bounded LLM-judge call (coder-30b, temperature 0, JSON
     verdict) — labelled as model-judged in the report.

Writes /opt/data/reports/hermes-eval/<date>.json + .html + latest.html and
appends history.jsonl (the nginx `reports` service serves the dir on :9123).
Prints a 2-4 line summary, which cron delivers to Telegram. The eval's own
sessions are deleted afterwards so they don't pollute session_search.

Stdlib only. Env (already in the gateway's .env): OPENAI_BASE_URL/KEY,
HASS_URL/TOKEN. Optional: VISION_MCP_URL, EVAL_ONLY=id1,id2 (subset),
EVAL_JUDGE_MODEL, EVAL_TIMEOUT (per question, s), REPORT_DIR.
"""
import datetime
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
QUESTIONS = os.path.join(HERE, "hermes_eval_questions.json")
VISION_MCP = os.environ.get("VISION_MCP_URL", "http://192.168.0.19:8097/mcp")
LLM_URL = os.environ.get("OPENAI_BASE_URL", "http://192.168.0.19:8080/v1").rstrip("/") + "/chat/completions"
LLM_KEY = os.environ.get("OPENAI_API_KEY", "")
JUDGE_MODEL = os.environ.get("EVAL_JUDGE_MODEL", "coder-30b")
HASS_URL = os.environ.get("HASS_URL", "").rstrip("/")
HASS_TOKEN = os.environ.get("HASS_TOKEN", "")
REPORT_DIR = os.path.join(os.environ.get("REPORT_DIR", "/opt/data/reports"), "hermes-eval")
PUBLIC = "https://ladybird.tail0e6756.ts.net:9123/hermes-eval"
TZ = ZoneInfo(os.environ.get("EVAL_TZ", "America/Denver"))
Q_TIMEOUT = int(os.environ.get("EVAL_TIMEOUT", "420"))
TIME_TOLERANCE_MIN = 3


# ---------------- vision-mcp: minimal streamable-http client ----------------

class MCP:
    def __init__(self, url):
        self.url, self.sid, self.n = url, None, 0

    def _post(self, payload):
        h = {"Content-Type": "application/json",
             "Accept": "application/json, text/event-stream"}
        if self.sid:
            h["mcp-session-id"] = self.sid
        req = urllib.request.Request(self.url, json.dumps(payload).encode(), h)
        with urllib.request.urlopen(req, timeout=120) as r:
            self.sid = r.headers.get("mcp-session-id") or self.sid
            ct, body = r.headers.get("Content-Type", ""), r.read().decode()
        if "text/event-stream" in ct:
            for line in body.splitlines():
                if line.startswith("data:"):
                    return json.loads(line[5:].strip())
            return None
        return json.loads(body) if body.strip() else None

    def init(self):
        self.n += 1
        self._post({"jsonrpc": "2.0", "id": self.n, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "hermes-eval", "version": "1"}}})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return self

    def call(self, name, **args):
        self.n += 1
        res = self._post({"jsonrpc": "2.0", "id": self.n, "method": "tools/call",
                          "params": {"name": name, "arguments": args}})
        res = (res or {}).get("result") or {}
        if res.get("structuredContent"):
            sc = res["structuredContent"]
            return sc.get("result", sc) if isinstance(sc, dict) else sc
        text = "".join(c.get("text", "") for c in res.get("content", []))
        return json.loads(text) if text.strip().startswith(("{", "[")) else {"text": text}


# ---------------- ground truth ----------------

def parse_local(s):
    """'Wed Nov 11 09:54' (vision-mcp _local) -> aware datetime this year
    (or last year, if that date would be in the future)."""
    now = datetime.datetime.now(TZ)
    dt = datetime.datetime.strptime(f"{now.year} {s}", "%Y %a %b %d %H:%M")
    dt = dt.replace(tzinfo=TZ)
    if dt > now + datetime.timedelta(days=1):
        dt = dt.replace(year=now.year - 1)
    return dt


def truth_for(spec, mcp):
    """Returns {"facts": <jsonable>, "times": [...HH:MM], "when": [...local
    strings], "answer": 'yes'|'no'|None, "word": str|None}."""
    fn = spec.get("fn", "none")
    if fn == "none":
        return {"facts": None}
    if fn == "fixed":
        return {"facts": spec["facts"], "times": spec.get("times", []),
                "answer": spec.get("answer")}
    if fn == "tag_latest":
        tags = mcp.call("tags")
        tag = next((t for t in tags.get("tags", []) if t["name"] == spec["tag"]), None)
        seen = (tag or {}).get("recent_sightings") or []
        if not seen:
            return {"facts": f"No {spec['tag']} has ever been tagged.", "answer": "no"}
        latest = seen[0]
        return {"facts": {"latest": latest, "previous": seen[1:4]},
                "times": [latest["when"][-5:]], "when": [latest["when"]], "answer": "yes"}
    if fn == "tag_in_window":
        r = mcp.call("recent_events", tag=spec["tag"], since=spec["since"],
                     until=spec.get("until", ""))
        evs = r.get("events", [])
        return {"facts": {"window": r.get("window"), "events": [
                    {k: e.get(k) for k in ("when", "camera", "caption", "tags")} for e in evs]},
                "times": [e["when"][-5:] for e in evs], "answer": "yes" if evs else "no"}
    if fn == "timeline":
        # the judge must see EVERY entry the agent could have seen (the agent's
        # timeline default is 60) — a truncated truth made true details look
        # invented in the first dry run
        r = mcp.call("timeline", since=spec["since"], limit=100)
        keep = {k: r.get(k) for k in ("window", "archive_processed_until",
                                      "not_yet_described", "nothing_notable")}
        keep["entries"] = [f"{e.get('when')} {e.get('camera')}: {e.get('caption')}"
                           for e in r.get("entries", [])]
        return {"facts": keep}
    if fn == "adsb_recent":
        # the agent's "last hour" ended anywhere inside its run; the log also
        # lags ~5 min — widen so nothing it could legitimately cite is missing
        return adsb_recent(spec.get("minutes", 60) + spec.get("run_secs", 0) / 60 + 5)
    if fn == "ha_state":
        req = urllib.request.Request(f"{HASS_URL}/api/states/{spec['entity']}",
                                     headers={"Authorization": f"Bearer {HASS_TOKEN}"})
        with urllib.request.urlopen(req, timeout=20) as r:
            state = json.load(r)["state"]
        word = spec.get("words", {}).get(state, state)
        return {"facts": f"{spec['entity']} is '{state}' ({word}) right now.", "word": word}
    raise ValueError(f"unknown truth fn {fn!r}")


ADSB_DIR = os.environ.get("RADIOLOG_ADSB_DIR", "/opt/data/workspace/radiolog/adsb")


def adsb_recent(minutes, now=None, adsb_dir=None):
    """Aircraft whose last_seen falls in the last `minutes`, from the same
    radio-log JSONL the agent's radio-logs skill reads (one file per local
    day, one line per aircraft pass)."""
    now = now or datetime.datetime.now(TZ)
    adsb_dir = adsb_dir or ADSB_DIR
    cutoff = now - datetime.timedelta(minutes=minutes)
    flights, every = [], set()  # every = all callsigns in the files, any time
    for day in {cutoff.date(), now.date()}:
        path = os.path.join(adsb_dir, f"{day.isoformat()}.jsonl")
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    a = json.loads(line)
                    seen = datetime.datetime.fromisoformat(a["last_seen"])
                except (ValueError, KeyError):
                    continue
                if (a.get("flight") or "").strip():
                    every.add(a["flight"].strip())
                if cutoff <= seen <= now:
                    flights.append({"flight": (a.get("flight") or a.get("hex", "")).strip(),
                                    "last_seen": a["last_seen"][11:16], "_ts": seen,
                                    "min_dist_nm": a.get("min_dist_nm"),
                                    "max_alt": a.get("max_alt")})
    flights.sort(key=lambda a: a.pop("_ts"), reverse=True)  # full time: midnight-safe
    # every aircraft, compact: a 30-row cap made real recent flights look
    # invented to the judge (2026-11-12, ~60 aircraft/hour here)
    return {"facts": {"window_minutes": round(minutes),
                      "aircraft": [f"{a['flight']} last_seen {a['last_seen']}" for a in flights],
                      "count": len(flights)},
            "tokens": [a["flight"] for a in flights if a["flight"]],
            "all_tokens": sorted(every),
            "answer": "yes" if flights else "no"}


# ---------------- running the agent ----------------

def flatten_tools(events):
    """Real tool names from stream-json tool_use events. Hermes's lazy
    loader wraps calls in a `tool_call` meta-tool ({"calls": [{"name": ...}]})
    and discovers them via `tool_search`; both wrappers are unwrapped/dropped."""
    out = []
    for ev in events:
        if ev.get("type") != "tool_use":
            continue
        name, inp = ev.get("name", ""), ev.get("input") or {}
        if name == "tool_call":
            out += [c.get("name", "") for c in inp.get("calls", []) if isinstance(c, dict)]
        elif name != "tool_search":
            out.append(name)
    return out


def parse_stream(lines):
    events = []
    for line in lines:
        line = line.strip()
        if line.startswith("{"):
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    result = next((e for e in reversed(events) if e.get("type") == "result"), {})
    text = result.get("text")
    if text is None:
        text = "".join(e.get("text", "") for e in events if e.get("type") == "text")
    return {"answer": (text or "").strip(), "tools": flatten_tools(events),
            "calls": call_transcript(events),
            "session_id": result.get("session_id"), "exit_code": result.get("exit_code")}


def call_transcript(events, cap=400):
    """Each tool call with its (truncated) input and output, for the report:
    the eval deletes its sessions, so this is the only record of what the
    agent actually ran — e.g. a heredoc blocked by the approval guard."""
    outputs = {e.get("tool_call_id"): str(e.get("output", ""))
               for e in events if e.get("type") == "tool_result"}
    calls = []
    for e in events:
        if e.get("type") != "tool_use" or e.get("name") == "tool_search":
            continue
        out = outputs.get(e.get("tool_call_id"), "")
        calls.append({"name": e.get("name"),
                      "input": json.dumps(e.get("input"), ensure_ascii=False)[:cap],
                      "output": out[:cap],
                      "blocked": '"status": "blocked"' in out or "BLOCKED:" in out})
    return calls


def ask_hermes(prompt):
    t0 = time.time()
    try:
        p = subprocess.run(["hermes", "chat", "-q", prompt, "--format", "stream-json"],
                           capture_output=True, text=True, timeout=Q_TIMEOUT)
        out = parse_stream(p.stdout.splitlines())
        if not out["answer"] and p.returncode:
            out["error"] = (p.stderr or "")[-400:]
    except subprocess.TimeoutExpired:
        out = {"answer": "", "tools": [], "session_id": None, "error": f"timeout {Q_TIMEOUT}s"}
    out["secs"] = round(time.time() - t0, 1)
    return out


def delete_session(sid):
    if sid:
        subprocess.run(["hermes", "sessions", "delete", sid, "--yes"],
                       capture_output=True, text=True, timeout=60)


# ---------------- grading ----------------

TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})\s*([ap])?\.?\s*m?\.?", re.I)


def answer_minutes(text):
    """Every clock time in the answer as minutes after midnight; a bare
    12-hour time with no am/pm counts both ways."""
    out = []
    for h, m, ap in TIME_RE.findall(text):
        h, m = int(h), int(m)
        if h > 23 or m > 59:
            continue
        if ap:
            h = h % 12 + (12 if ap.lower() == "p" else 0)
            out.append(h * 60 + m)
        else:
            out.append(h * 60 + m)
            if h < 12:
                out.append((h + 12) * 60 + m)
    return out


def check_time(answer, times, tol=TIME_TOLERANCE_MIN):
    if not times:
        return True, "no expected time"
    want = [int(t[:2]) * 60 + int(t[3:5]) for t in times]
    got = answer_minutes(answer)
    ok = any(abs(g - w) <= tol for g in got for w in want)
    seen = [f"{g // 60:02d}:{g % 60:02d}" for g in sorted(set(got))][:6]
    return ok, f"want ~{'/'.join(times)} (±{tol}m), answer has {seen or 'no times'}"


def day_tokens(dt, today):
    toks = {dt.strftime("%A").lower(), dt.strftime("%a").lower() + " ",
            f"{dt.strftime('%b').lower()} {dt.day}", f"{dt.strftime('%B').lower()} {dt.day}",
            f"{dt.month}/{dt.day}", f"{dt.day}th", f"{dt.day}st", f"{dt.day}nd", f"{dt.day}rd"}
    delta = (today - dt.date()).days
    if delta == 0:
        toks |= {"today", "this morning", "this afternoon", "this evening"}
    elif delta == 1:
        toks |= {"yesterday", "last night"}
    return toks


def check_day(answer, when, today):
    if not when:
        return True, "no expected day"
    a = answer.lower() + " "
    for w in when:
        toks = day_tokens(parse_local(w), today)
        if any(t in a for t in toks):
            return True, f"names {w[:10]}"
    return False, f"want day of {', '.join(when)}"


def tools_check(tools, any_=None, none=None):
    notes, ok = [], True
    if any_:
        hit = any(t.startswith(tuple(any_)) for t in tools)
        ok &= hit
        notes.append(f"{'used' if hit else 'never used'} {'/'.join(any_)}")
    if none:
        bad = [t for t in tools if t.startswith(tuple(none))]
        ok &= not bad
        notes.append(f"wrongly used {bad}" if bad else f"avoided {'/'.join(none)}")
    return ok, "; ".join(notes)


def judge(question, answer, facts, rubric, call=None):
    """One bounded LLM-judge call. Returns (pass, reason)."""
    prompt = (
        "You are grading a home assistant's answer against ground truth from its own "
        "databases. Grade only against the ground truth and the rubric; do not use "
        "outside knowledge.\n\n"
        # ~24k chars ≈ 6k tokens: fits coder-30b's 64k with room; truncation
        # here makes the judge call true details "invented", so keep it roomy
        f"QUESTION: {question}\n\nGROUND TRUTH (JSON):\n{json.dumps(facts, ensure_ascii=False)[:24000]}\n\n"
        f"ANSWER:\n{answer[:4000]}\n\nRUBRIC: {rubric}\n\n"
        'Reply with ONLY a JSON object: {"pass": true or false, "reason": "<one sentence>"}')
    raw = (call or _llm)(prompt)
    m = re.search(r"\{.*\}", raw, re.S)
    try:
        v = json.loads(m.group(0)) if m else {}
        return bool(v.get("pass")), str(v.get("reason", ""))[:300]
    except json.JSONDecodeError:
        return False, f"unparseable judge output: {raw[:120]}"


def _llm(prompt):
    body = {"model": JUDGE_MODEL, "temperature": 0, "max_tokens": 200,
            "messages": [{"role": "user", "content": prompt}]}
    req = urllib.request.Request(LLM_URL, json.dumps(body).encode(),
                                 {"Content-Type": "application/json",
                                  "Authorization": f"Bearer {LLM_KEY}"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.load(r)["choices"][0]["message"]["content"]


def grade(q, run, truth, today, judge_call=None):
    """Returns [(check, ok, note, how)] — how = 'rule' | 'judge'."""
    c, a, out = q.get("checks", {}), run["answer"], []
    if run.get("error") or not a:
        return [("ran", False, run.get("error") or "empty answer", "rule")]
    if c.get("tools_any") or c.get("tools_none"):
        ok, note = tools_check(run["tools"], c.get("tools_any"), c.get("tools_none"))
        out.append(("tools", ok, note, "rule"))
    if c.get("time") and truth.get("answer") != "no":
        out.append(("time", *check_time(a, truth.get("times", [])), "rule"))
    if c.get("day"):
        out.append(("day", *check_day(a, truth.get("when", []), today), "rule"))
    for pat in c.get("regex_all", []):
        out.append((f"says /{pat}/", bool(re.search(pat, a, re.I)), "", "rule"))
    for pat in c.get("regex_none", []):
        hit = re.search(pat, a, re.I)
        out.append((f"avoids /{pat}/", not hit, hit.group(0) if hit else "", "rule"))
    if c.get("truth_any") and truth.get("tokens"):
        hit = [t for t in truth["tokens"] if re.search(rf"\b{re.escape(t)}\b", a, re.I)]
        out.append(("names a real one", bool(hit),
                    f"mentions {hit[:3]}" if hit else
                    f"none of {truth['tokens'][:5]}{'…' if len(truth['tokens']) > 5 else ''}",
                    "rule"))
    if c.get("no_stale") and truth.get("all_tokens") is not None:
        # names the answer uses that the logs know, but not inside the window:
        # exact and cheap, where an LLM judge miscounted a 60-callsign set
        fresh = set(truth.get("tokens", []))
        named = {t for t in truth["all_tokens"] if re.search(rf"\b{re.escape(t)}\b", a, re.I)}
        stale = sorted(named - fresh)
        out.append(("nothing stale", not stale,
                    f"outside the window: {stale[:5]}" if stale else f"{len(named)} named, all in window",
                    "rule"))
    if c.get("truth_word"):
        w = truth.get("word") or ""
        other = {"open": "closed", "closed": "open"}.get(w)
        ok = bool(re.search(rf"\b{w}\b", a, re.I)) and not (
            other and re.search(rf"\bis {other}\b", a, re.I))
        out.append(("state", ok, f"want '{w}'", "rule"))
    if c.get("judge"):
        rubric = c["judge"]
        if truth.get("answer"):
            rubric += f" (Ground-truth yes/no: {truth['answer']}.)"
        try:
            ok, why = judge(q["prompt"], a, truth.get("facts"), rubric, judge_call)
        except Exception as e:  # noqa: BLE001 — judge down ≠ agent failed
            ok, why = None, f"judge unavailable: {e}"
        out.append(("judge", ok, why, "judge"))
    return out


def verdict(checks):
    vals = [ok for _, ok, _, _ in checks if ok is not None]
    return bool(vals) and all(vals)


# ---------------- report ----------------

def history_tail(n=14):
    path = os.path.join(REPORT_DIR, "history.jsonl")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        rows = [json.loads(l) for l in f if l.strip()]
    return rows[-n:]


def render_html(run):
    esc = html.escape
    rows = []
    for r in run["results"]:
        checks = "".join(
            f"<li class={'ok' if ok else ('na' if ok is None else 'bad')}>"
            f"{'✓' if ok else ('–' if ok is None else '✗')} {esc(name)}"
            f"{' <em>(model-judged)</em>' if how == 'judge' else ''}"
            f"{': ' + esc(note) if note else ''}</li>"
            for name, ok, note, how in r["checks"])
        rows.append(f"""
<section class="q {'pass' if r['pass'] else 'fail'}">
  <h2>{'PASS' if r['pass'] else 'FAIL'} · {esc(r['id'])} <span class=muted>{r['secs']}s</span></h2>
  <p class=muted>{esc(r['why'])}</p>
  <p><strong>Q:</strong> {esc(r['prompt'])}</p>
  <ul>{checks}</ul>
  <p class=muted>tools: {esc(', '.join(r['tools']) or 'none')}</p>
  <details><summary>tool calls ({len(r.get('calls', []))}{', ' + str(sum(c['blocked'] for c in r.get('calls', []))) + ' BLOCKED' if any(c['blocked'] for c in r.get('calls', [])) else ''})</summary>
  {''.join(f"<pre>{'⛔ ' if c['blocked'] else ''}{esc(c['name'])} {esc(c['input'])}\n→ {esc(c['output'])}</pre>" for c in r.get('calls', []))}</details>
  <details><summary>answer</summary><pre>{esc(r['answer'])}</pre></details>
  <details><summary>ground truth</summary><pre>{esc(json.dumps(r['truth'], indent=1, ensure_ascii=False)[:5000])}</pre></details>
</section>""")
    trend = " ".join(f"{h['date'][5:]}:{h['passed']}/{h['total']}" for h in run["trend"])
    return f"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Hermes eval {run['date']}</title>
<style>
:root{{--bg:#11151b;--fg:#e6e9ee;--muted:#8b93a1;--ok:#3fb950;--bad:#f85149;--card:#1a2029}}
@media (prefers-color-scheme: light){{:root{{--bg:#f6f7f9;--fg:#1d232b;--muted:#5d6673;--card:#fff}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif;margin:0 auto;max-width:900px;padding:16px}}
.q{{background:var(--card);border-left:4px solid var(--bad);border-radius:6px;padding:8px 14px;margin:12px 0}}
.q.pass{{border-color:var(--ok)}} h2{{font-size:16px;margin:6px 0}} .muted{{color:var(--muted);font-size:13px}}
li.ok{{color:var(--ok)}} li.bad{{color:var(--bad)}} li.na{{color:var(--muted)}} pre{{white-space:pre-wrap;font-size:13px}}
</style></head><body>
<h1>Hermes eval — {run['date']}: {run['passed']}/{run['total']} passed</h1>
<p class=muted>model {esc(run['model'])} · judge {esc(JUDGE_MODEL)} (labelled where used) · {run['secs']}s total</p>
<p class=muted>trend: {esc(trend) or 'first run'}</p>
{''.join(rows)}
</body></html>"""


def summary_lines(run, prev):
    delta = ""
    if prev:
        d = run["passed"] - prev["passed"]
        delta = f" ({'+' if d >= 0 else ''}{d} vs {prev['date'][5:]})"
    lines = [f"Hermes eval {run['date']}: {run['passed']}/{run['total']} passed{delta}"]
    for r in run["results"]:
        if not r["pass"]:
            bad = next(((n, note) for n, ok, note, _ in r["checks"] if ok is False), ("?", ""))
            lines.append(f"✗ {r['id']} — {bad[0]}: {bad[1][:110]}")
    lines.append(f"{PUBLIC}/{run['date']}.html")
    return lines


def main():
    with open(QUESTIONS, encoding="utf-8") as f:
        questions = json.load(f)["questions"]
    only = {s for s in os.environ.get("EVAL_ONLY", "").split(",") if s}
    if only:
        questions = [q for q in questions if q["id"] in only]
    today = datetime.datetime.now(TZ).date()
    t0 = time.time()
    mcp = MCP(VISION_MCP).init()
    results = []
    for q in questions:
        run = ask_hermes(q["prompt"])
        delete_session(run.get("session_id"))
        # truth AFTER the answer: a run takes 1-5 min, and a live window ("last
        # hour") fetched before it drifted past what the agent saw. Windows
        # widen by the run time (see truth_for).
        spec = dict(q.get("truth", {}), run_secs=run["secs"])
        try:
            truth = truth_for(spec, mcp)
        except Exception as e:  # noqa: BLE001 — record, keep going
            truth = {"facts": f"[ground truth unavailable: {e}]"}
        checks = grade(q, run, truth, today)
        results.append({"id": q["id"], "why": q.get("why", ""), "prompt": q["prompt"],
                        "pass": verdict(checks), "checks": checks, "answer": run["answer"],
                        "tools": run["tools"], "calls": run.get("calls", []),
                        "secs": run["secs"], "truth": truth})
    prev = (history_tail(1) or [None])[-1]
    run = {"date": today.isoformat(), "model": os.environ.get("EVAL_AGENT_MODEL", "config default"),
           "total": len(results), "passed": sum(r["pass"] for r in results),
           "secs": round(time.time() - t0), "results": results}
    os.makedirs(REPORT_DIR, exist_ok=True)
    with open(os.path.join(REPORT_DIR, "history.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"date": run["date"], "passed": run["passed"], "total": run["total"],
                            "failed": [r["id"] for r in results if not r["pass"]]}) + "\n")
    run["trend"] = history_tail()
    with open(os.path.join(REPORT_DIR, f"{run['date']}.json"), "w", encoding="utf-8") as f:
        json.dump(run, f, indent=1, ensure_ascii=False)
    page = render_html(run)
    for name in (f"{run['date']}.html", "latest.html"):
        with open(os.path.join(REPORT_DIR, name), "w", encoding="utf-8") as f:
            f.write(page)
    print("\n".join(summary_lines(run, prev)))


if __name__ == "__main__":
    sys.exit(main())
