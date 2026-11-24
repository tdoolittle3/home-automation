#!/usr/bin/env python3
"""Head-to-head model comparison on snibbly: speed + auto-graded quality.

Runs the same fixed suite against each llama-swap model and writes a JSON
result file plus a markdown summary table. Stdlib only.

    LLAMA_API_KEY=... python3 compare_models.py \
        inkling-mini coder-30b big-30b-thinking fast-8b vlm-8b \
        --out results-2026-11-23.json

A model spec may carry a reasoning-effort suffix, e.g. ``inkling-mini@low``;
it is sent as ``reasoning_effort`` (top level and as a chat-template kwarg).

Defaults to llama-swap direct on :9292, so bench traffic stays out of
Langfuse. Suites:
  speed   - one ~1.5k-token prompt, 256 tokens out; server-reported
            prefill/decode tok/s, plus cold-load wall time
  quality - math, logic, code (executed against unit tests), instruction
            following, tool calling
  vision  - generated PNGs with known content (only for --vision models)
"""
import argparse
import base64
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
import zlib

# ---------------------------------------------------------------- transport

def chat(args, model, messages, max_tokens, effort=None, tools=None, temperature=0.0, cache=True):
    body = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "seed": 1234,
    }
    if not cache:  # llama.cpp prompt cache would otherwise skip the prefill we want to time
        body["cache_prompt"] = False
    if effort:
        body["reasoning_effort"] = effort
        body["chat_template_kwargs"] = {"reasoning_effort": effort}
    if tools:
        body["tools"] = tools
    req = urllib.request.Request(
        args.base_url.rstrip("/") + "/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + args.key},
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=args.request_timeout) as r:
        out = json.load(r)
    out["_wall"] = time.time() - t0
    return out


def content_of(out):
    msg = out["choices"][0]["message"]
    text = msg.get("content") or ""
    # Some templates leave reasoning inline; strip it before grading.
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    text = re.sub(r"^.*?</think>", "", text, flags=re.S)
    return text.strip()


def final_answer(text):
    m = re.findall(r"ANSWER:\s*(.+)", text)
    return (m[-1] if m else text.strip().splitlines()[-1] if text.strip() else "").strip()


def num(s):
    m = re.findall(r"-?\d[\d,]*\.?\d*", s)
    if not m:
        return None
    try:
        return float(m[-1].replace(",", "").rstrip("."))
    except ValueError:
        return None

# ---------------------------------------------------------------- quality suite

SUFFIX = "\n\nEnd your reply with a final line of the form `ANSWER: <answer>`."

MATH = [
    ("A store sells pens at 3 for $4. How many dollars do 27 pens cost?", 36),
    ("A train leaves at 9:40 and arrives at 13:15 the same day. How many minutes is the trip?", 215),
    ("What is the sum of all integers from 1 to 200 that are divisible by 3 or 5?", 9368),
    ("A tank holds 240 liters. Pipe A fills it in 6 hours, pipe B empties it in 10 hours. "
     "Starting empty with both open, how many hours until it is full?", 15),
    ("If 5 machines make 5 widgets in 5 minutes, how many minutes do 100 machines need to make 100 widgets?", 5),
    ("How many positive divisors does 360 have?", 24),
    ("Compute 17^3 - 13^3.", 2716),
    ("A shirt is discounted 20%, then the sale price is discounted another 15%. "
     "What single percentage discount is that equivalent to?", 32),
]

LOGIC = [
    ("Alice is taller than Bob. Carol is shorter than Bob. Dave is taller than Alice. "
     "Who is the second shortest? Answer with just the name.", "bob"),
    ("I have two coins totaling 30 cents, and one of them is not a nickel. "
     "What are the two coins? Answer as two words separated by 'and'.", "quarter and nickel"),
    ("A farmer has 17 sheep. All but 9 run away. How many sheep does the farmer have left? Answer with a number.", "9"),
    ("If it is Wednesday today, what day of the week will it be 100 days from now? Answer with the day name.", "friday"),
]

CODE = [
    ("Write a Python function `is_balanced(s: str) -> bool` that returns True iff every bracket "
     "in s among ()[]{} is properly matched and nested. Other characters are ignored.",
     "assert is_balanced('a(b[c]{d}e)f')\nassert not is_balanced('(]')\nassert not is_balanced('((')\n"
     "assert is_balanced('')\nassert not is_balanced('}{')\nassert is_balanced('{[()()]}')\n"),
    ("Write a Python function `merge_intervals(iv: list[list[int]]) -> list[list[int]]` that merges "
     "overlapping closed intervals and returns them sorted by start. Touching intervals like [1,2],[2,3] merge.",
     "assert merge_intervals([[1,3],[2,6],[8,10],[15,18]])==[[1,6],[8,10],[15,18]]\n"
     "assert merge_intervals([[1,4],[4,5]])==[[1,5]]\nassert merge_intervals([])==[]\n"
     "assert merge_intervals([[5,6],[1,2]])==[[1,2],[5,6]]\nassert merge_intervals([[1,10],[2,3]])==[[1,10]]\n"),
    ("Write a Python function `roman(n: int) -> str` converting 1 <= n <= 3999 to a Roman numeral.",
     "assert roman(1)=='I'\nassert roman(4)=='IV'\nassert roman(9)=='IX'\nassert roman(58)=='LVIII'\n"
     "assert roman(1994)=='MCMXCIV'\nassert roman(3999)=='MMMCMXCIX'\n"),
    ("Write a Python function `top_k_words(text: str, k: int) -> list[str]` returning the k most frequent "
     "lowercase words (split on non-letters), ties broken alphabetically.",
     "assert top_k_words('b a b c a b', 2)==['b','a']\nassert top_k_words('Dog cat, dog! CAT bird', 3)==['cat','dog','bird']\n"
     "assert top_k_words('', 3)==[]\nassert top_k_words('x y z', 2)==['x','y']\n"),
]

# ---- hard tier: added after the 2026-11-23 old-build baseline scored 21/21 and
# 24/24 — the easy tier alone can't separate models. Math keys verified by
# computation, not recall.
MATH_HARD = [
    ("How many trailing zeros does 1000! have?", 249),
    ("What is the remainder when 2^100 is divided by 13?", 3),
    ("In how many ways can a 2x10 rectangle be tiled with 1x2 dominoes?", 89),
    ("What is the sum of the decimal digits of 2^50?", 76),
    ("What is the smallest positive integer n such that n! ends in at least 100 zeros?", 405),
    ("How many monotonic lattice paths go from (0,0) to (6,6) using unit right/up steps "
     "that never rise above the line y = x?", 132),
    ("How many 4-digit palindromes are divisible by 7?", 18),
    ("What is the expected number of rolls of a fair six-sided die until every face has "
     "appeared at least once? Give a decimal.", 14.7),
]

LOGIC_HARD = [
    ("On an island, knights always tell the truth and knaves always lie. A says: 'B is a knave.' "
     "B says: 'A and C are the same type.' Is C a knight or a knave? Answer with one word.", "knave"),
    ("Ann, Ben and Cal each own exactly one pet: a cat, a dog or a fish. Ann is female; Ben and Cal "
     "are male. Ann does not own the dog. The fish belongs to Ben's sister. Cal does not own the cat. "
     "Who owns the dog? Answer with the name.", "cal"),
    ("What day of the week was 1 January 2000? Answer with the day name.", "saturday"),
    ("Five people sit in a row of seats numbered 1-5. Eve is in seat 3. Finn sits immediately left of "
     "Gus (lower number). Hal is not at either end. Ivy is in a higher-numbered seat than Gus. "
     "Who is in seat 1? Answer with the name.", "finn"),
]

CODE_HARD = [
    ("Write a Python function `eval_expr(s: str) -> int` that evaluates an integer arithmetic "
     "expression with + - * /, parentheses, unary minus and arbitrary spaces. `/` is integer division "
     "truncating toward zero (like C). Do not use eval or exec.",
     "assert eval_expr('1 + 2 * 3')==7\nassert eval_expr('(1+2)*3')==9\nassert eval_expr('-7/2')==-3\n"
     "assert eval_expr('2*(3+4)*-1')==-14\nassert eval_expr(' 10 - 2 - 3 ')==5\n"
     "assert eval_expr('-(2+3)*-(1+1)')==10\nassert eval_expr('7/-2')==-3\n"
     "import builtins\nbuiltins.eval=builtins.exec=None\nassert eval_expr('8/3*3')==6\n"),
    ("Write a Python class `LRUCache` with `__init__(self, capacity: int)`, `get(self, key) -> int` "
     "(returns -1 if missing) and `put(self, key, value) -> None`; evict the least recently used key "
     "when over capacity. get and put both count as use.",
     "c=LRUCache(2)\nc.put(1,1);c.put(2,2)\nassert c.get(1)==1\nc.put(3,3)\nassert c.get(2)==-1\n"
     "c.put(4,4)\nassert c.get(1)==-1 and c.get(3)==3 and c.get(4)==4\nc.put(3,30)\nc.put(5,5)\n"
     "assert c.get(4)==-1 and c.get(3)==30\n"),
    ("Write a Python function `edit_distance(a: str, b: str) -> int` returning the Levenshtein distance.",
     "assert edit_distance('kitten','sitting')==3\nassert edit_distance('','abc')==3\n"
     "assert edit_distance('flaw','lawn')==2\nassert edit_distance('same','same')==0\n"
     "assert edit_distance('intention','execution')==5\n"),
    ("Write a Python function `longest_palindrome(s: str) -> str` returning the longest palindromic "
     "substring; if several share the max length return the leftmost.",
     "assert longest_palindrome('babad')=='bab'\nassert longest_palindrome('cbbd')=='bb'\n"
     "assert longest_palindrome('')==''\nassert longest_palindrome('abacdfgdcaba')=='aba'\n"
     "assert longest_palindrome('forgeeksskeegfor')=='geeksskeeg'\n"),
]

TOOLS = [
    {"type": "function", "function": {
        "name": "get_garage_door_state",
        "description": "Return whether the garage door is currently open or closed.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "set_light",
        "description": "Turn a named light on or off.",
        "parameters": {"type": "object", "properties": {
            "area": {"type": "string", "description": "room name, e.g. kitchen"},
            "state": {"type": "string", "enum": ["on", "off"]}},
            "required": ["area", "state"]}}},
    {"type": "function", "function": {
        "name": "search_camera_events",
        "description": "Search recorded camera detections.",
        "parameters": {"type": "object", "properties": {
            "label": {"type": "string", "description": "object label, e.g. person, car"},
            "camera": {"type": "string"},
            "since_hours": {"type": "integer"}},
            "required": ["label"]}}},
]

def _calls(out):
    calls = out["choices"][0]["message"].get("tool_calls") or []
    parsed = []
    for c in calls:
        try:
            parsed.append((c["function"]["name"], json.loads(c["function"].get("arguments") or "{}")))
        except json.JSONDecodeError:
            parsed.append((c["function"]["name"], None))
    return parsed


def _one(name, want):
    def g(calls):
        return (len(calls) >= 1 and calls[0][0] == name and calls[0][1] is not None
                and all(str(calls[0][1].get(k, "")).lower() == str(v).lower() for k, v in want.items()))
    return g


def _two_lights(calls):
    areas = {(a or {}).get("area", "").lower().replace(" ", "") for n, a in calls if n == "set_light"
             and (a or {}).get("state") == "on"}
    return {"kitchen", "livingroom"} <= areas


TOOL_CASES = [  # (tier, prompt, grader over [(name, args)])
    ("easy", "Is the garage door open right now?", _one("get_garage_door_state", {})),
    ("easy", "Turn off the kitchen light please.", _one("set_light", {"area": "kitchen", "state": "off"})),
    ("easy", "Were there any people on the driveway camera in the last 6 hours?",
     _one("search_camera_events", {"label": "person", "since_hours": 6})),
    ("hard", "Turn on the lights in the kitchen and the living room.", _two_lights),
    ("hard", "What's the capital of Australia? Answer directly.", lambda calls: len(calls) == 0),
    ("hard", "Any cars show up on the backyard camera since this time yesterday?",
     _one("search_camera_events", {"label": "car", "camera": "backyard", "since_hours": 24})),
]

# Long-context retrieval: one fact buried in ~4.5k tokens of near-duplicate log
# lines. Sized to fit the smallest per-slot context in the lineup (vlm-8b 8k).
NEEDLE = "2026-11-04T13:07:00Z keypad[2211]: garage keypad code changed to 4721 by admin"
HAYSTACK = "\n".join(
    (NEEDLE if i == 97 else
     f"2026-11-0{i % 9 + 1}T{i % 24:02d}:{i % 60:02d}:00Z keypad[{2000 + i}]: "
     f"garage keypad {'unlock ok' if i % 4 else 'bad code attempt'} user={i % 7}")
    for i in range(160))
LONG_CASES = [
    ("Below is a keypad log.\n\n" + HAYSTACK + "\n\nWhat is the garage keypad code it was changed to?"
     + SUFFIX, lambda t: num(final_answer(t)) == 4721),
    ("Below is a keypad log.\n\n" + HAYSTACK + "\n\nHow many 'bad code attempt' lines are there?" + SUFFIX,
     lambda t: num(final_answer(t)) == sum(1 for i in range(160) if i != 97 and i % 4 == 0)),
]


def grade_code(text, tests):
    blocks = re.findall(r"```(?:python)?\n(.*?)```", text, flags=re.S)
    src = max(blocks, key=len) if blocks else text
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "t.py")
        with open(p, "w") as f:
            f.write(src + "\n\n" + tests)
        try:
            r = subprocess.run([sys.executable, "-I", p], cwd=d, capture_output=True, timeout=20)
            return r.returncode == 0
        except subprocess.TimeoutExpired:
            return False


def run_quality(args, model, effort, rec):
    items = []

    def ask(kind, prompt, grader, max_tokens, tier="easy"):
        try:
            out = chat(args, model, [{"role": "user", "content": prompt}], max_tokens, effort)
            text = content_of(out)
            ok = bool(grader(text))
            toks = out.get("usage", {}).get("completion_tokens", 0)
            wall = out["_wall"]
            err = None
        except Exception as e:  # timeouts, 500s
            ok, toks, wall, err, text = False, 0, 0.0, repr(e)[:200], ""
        items.append({"kind": kind, "tier": tier, "ok": ok, "tokens": toks, "wall": round(wall, 1),
                      "err": err, "tail": text[-200:]})
        print(f"    {kind:6} {tier:4} {'PASS' if ok else 'fail'} {toks:5} tok {wall:6.1f}s", flush=True)

    for tier, qs in (("easy", MATH), ("hard", MATH_HARD)):
        for q, a in qs:
            ask("math", q + SUFFIX,
                lambda t, a=a: (v := num(final_answer(t))) is not None and abs(v - a) < 1e-6,
                args.max_tokens, tier)
    for tier, qs in (("easy", LOGIC), ("hard", LOGIC_HARD)):
        for q, a in qs:
            # every word of the key must appear ("a quarter and a nickel" passes)
            ask("logic", q + SUFFIX,
                lambda t, a=a: all(w in re.sub(r"[^a-z0-9 ]", " ", final_answer(t).lower()).split()
                                   for w in a.split() if w != "and"), args.max_tokens, tier)
    for tier, qs in (("easy", CODE), ("hard", CODE_HARD)):
        for q, tests in qs:
            ask("code", q + " Reply with a single ```python code block containing only the code.",
                lambda t, tests=tests: grade_code(t, tests), args.max_tokens, tier)
    for q, grader in LONG_CASES:
        ask("long", q, grader, args.max_tokens, "hard")
    ask("instr", "List exactly three primary colors of light as a JSON array of lowercase strings. "
        "Output only the JSON, no prose, no code fences.",
        lambda t: _json_eq(t, ["red", "green", "blue"]), args.max_tokens)
    ask("instr", "Reply with exactly the word 'acknowledged' in uppercase and nothing else.",
        lambda t: t.strip().strip(".") == "ACKNOWLEDGED", args.max_tokens)

    for tier, q, grader in TOOL_CASES:
        try:
            out = chat(args, model, [{"role": "user", "content": q}], args.max_tokens, effort, tools=TOOLS)
            calls = _calls(out)
            ok = bool(grader(calls))
            toks, wall, err = out.get("usage", {}).get("completion_tokens", 0), out["_wall"], None
        except Exception as e:
            ok, toks, wall, err, calls = False, 0, 0.0, repr(e)[:200], []
        items.append({"kind": "tool", "tier": tier, "ok": ok, "tokens": toks, "wall": round(wall, 1),
                      "err": err, "calls": calls})
        print(f"    tool   {tier:4} {'PASS' if ok else 'fail'} {toks:5} tok {wall:6.1f}s", flush=True)
    rec["quality"] = items


def _json_eq(t, want):
    try:
        return json.loads(t.strip().strip("`")) == want
    except Exception:
        return False

# ---------------------------------------------------------------- vision suite

def png(width, height, pixels):
    raw = b"".join(b"\x00" + bytes(c for px in row for c in px) for row in pixels)
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def squares_image(squares, w=480, h=240):
    px = [[(255, 255, 255)] * w for _ in range(h)]
    for (x, y, s, col) in squares:
        for yy in range(y, y + s):
            for xx in range(x, x + s):
                px[yy][xx] = col
    return png(w, h, px)


RED, BLUE, GREEN, YELLOW = (220, 30, 30), (30, 60, 220), (30, 170, 60), (240, 200, 20)
VISION = [
    (squares_image([(30, 80, 70, RED), (150, 80, 70, BLUE), (270, 80, 70, GREEN), (390, 80, 70, RED)]),
     "How many colored squares are in this image? Answer with a number.", lambda a: num(a) == 4),
    (squares_image([(40, 60, 120, YELLOW), (300, 60, 120, BLUE)]),
     "What color is the square on the LEFT? Answer with one color word.", lambda a: "yellow" in a.lower()),
    (squares_image([(40, 60, 60, GREEN), (200, 60, 60, GREEN), (360, 60, 60, RED)]),
     "Which color appears on exactly one square? Answer with one color word.", lambda a: "red" in a.lower()),
]
VISION =[(img, q, g, "easy") for img, q, g in VISION]


def grid_image(rows, cell=70, gap=14):
    n_r, n_c = len(rows), len(rows[0])
    w, h = n_c * (cell + gap) + gap, n_r * (cell + gap) + gap
    return squares_image([(gap + c * (cell + gap), gap + r * (cell + gap), cell, col)
                          for r, row in enumerate(rows) for c, col in enumerate(row)], w, h)


_GRID = [[RED, BLUE, GREEN], [YELLOW, RED, BLUE], [GREEN, GREEN, RED]]
_SCATTER = [[BLUE, RED, BLUE, YELLOW, GREEN], [RED, BLUE, GREEN, BLUE, RED]]
VISION += [
    (grid_image(_GRID), "This is a 3x3 grid of colored squares. What color is the square directly "
     "below the blue square in the TOP row? Answer with one color word.", lambda a: "red" in a.lower(), "hard"),
    (grid_image(_GRID), "This is a 3x3 grid of colored squares. What color is the MIDDLE square of the "
     "BOTTOM row? Answer with one color word.", lambda a: "green" in a.lower(), "hard"),
    (grid_image(_SCATTER), "How many BLUE squares are in this image? Answer with a number.",
     lambda a: num(a) == 4, "hard"),
]


def run_vision(args, model, effort, rec):
    items = []
    for img, q, grader, tier in VISION:
        url = "data:image/png;base64," + base64.b64encode(img).decode()
        msg = [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": url}},
            {"type": "text", "text": q + SUFFIX}]}]
        try:
            out = chat(args, model, msg, args.max_tokens, effort)
            t = content_of(out)
            ok = bool(grader(final_answer(t)))
            toks, wall, err = out.get("usage", {}).get("completion_tokens", 0), out["_wall"], None
        except Exception as e:
            ok, toks, wall, err = False, 0, 0.0, repr(e)[:200]
        items.append({"kind": "vision", "tier": tier, "ok": ok, "tokens": toks, "wall": round(wall, 1),
                      "err": err})
        print(f"    vision {tier:4} {'PASS' if ok else 'fail'} {toks:5} tok {wall:6.1f}s", flush=True)
    rec["vision"] = items

# ---------------------------------------------------------------- speed suite

LONG_PROMPT = ("Here is a log excerpt from a home server.\n" + "\n".join(
    f"2026-11-0{i % 9 + 1}T{i % 24:02d}:{i % 60:02d}:00Z frigate[{1000 + i}]: camera=driveway "
    f"event=motion score=0.{i % 97:02d} zone={'street' if i % 3 else 'driveway'} duration={i % 40}s"
    for i in range(70)) + "\n\nSummarize the activity patterns in a few paragraphs.")


def run_speed(args, model, effort, rec):
    t0 = time.time()
    first = chat(args, model, [{"role": "user", "content": "Say hi."}], 16, effort)
    rec["load_and_first_s"] = round(time.time() - t0, 1)
    runs = []
    for i in range(args.speed_runs):
        out = chat(args, model, [{"role": "user", "content": LONG_PROMPT}], 256, effort,
                   temperature=0.7, cache=False)
        tm = out.get("timings", {})
        runs.append({"prompt_n": tm.get("prompt_n"), "prefill_tps": tm.get("prompt_per_second"),
                     "decode_tps": tm.get("predicted_per_second"),
                     "completion_tokens": out.get("usage", {}).get("completion_tokens")})
        print(f"    speed prefill {tm.get('prompt_n')} tok @ {tm.get('prompt_per_second', 0):.0f} t/s, "
              f"decode {tm.get('predicted_per_second', 0):.1f} t/s", flush=True)
    rec["speed"] = runs
    del first

# ---------------------------------------------------------------- report

def summarize(results):
    lines = ["| model | load+1st (s) | prefill t/s | decode t/s | math | logic | code | instr | tool | long | vision | easy | hard | total | avg tok/item | quality wall (min) |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        sp = r.get("speed") or []
        med = lambda k: sorted(x[k] for x in sp if x.get(k))[len(sp) // 2] if sp else 0
        q = r.get("quality", []) + r.get("vision", [])
        def frac(kind):
            xs = [x for x in q if x["kind"] == kind]
            return f"{sum(x['ok'] for x in xs)}/{len(xs)}" if xs else "—"
        def tier(t):
            xs = [x for x in q if x.get("tier", "easy") == t]
            return f"{sum(x['ok'] for x in xs)}/{len(xs)}"
        total = f"{sum(x['ok'] for x in q)}/{len(q)}"
        avg = sum(x["tokens"] for x in q) / max(1, len(q))
        wall = sum(x["wall"] for x in q) / 60
        lines.append(f"| {r['spec']} | {r.get('load_and_first_s', '—')} | {med('prefill_tps'):.0f} | "
                     f"{med('decode_tps'):.1f} | {frac('math')} | {frac('logic')} | {frac('code')} | "
                     f"{frac('instr')} | {frac('tool')} | {frac('long')} | {frac('vision')} | "
                     f"{tier('easy')} | {tier('hard')} | {total} | {avg:.0f} | {wall:.1f} |")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("models", nargs="+")
    ap.add_argument("--base-url", default=os.environ.get("LLAMA_BASE", "http://127.0.0.1:9292"))
    ap.add_argument("--key", default=os.environ.get("LLAMA_API_KEY", ""))
    ap.add_argument("--vision", default="inkling-mini,vlm-8b",
                    help="comma list of base model names that get the vision suite")
    ap.add_argument("--max-tokens", type=int, default=4096)
    ap.add_argument("--speed-runs", type=int, default=3)
    ap.add_argument("--request-timeout", type=int, default=900)
    ap.add_argument("--out", default="compare_results.json")
    ap.add_argument("--only", default="speed,quality,vision")
    args = ap.parse_args()
    suites = set(args.only.split(","))
    vision_models = set(args.vision.split(","))

    results = []
    for spec in args.models:
        model, _, effort = spec.partition("@")
        print(f"== {spec}", flush=True)
        rec = {"spec": spec, "model": model, "effort": effort or None,
               "started": time.strftime("%Y-%m-%dT%H:%M:%S")}
        try:
            if "speed" in suites:
                run_speed(args, model, effort or None, rec)
            if "quality" in suites:
                run_quality(args, model, effort or None, rec)
            if "vision" in suites and model in vision_models:
                run_vision(args, model, effort or None, rec)
        except Exception as e:
            rec["fatal"] = repr(e)[:300]
            print(f"    FATAL {e!r}", flush=True)
        results.append(rec)
        with open(args.out, "w") as f:
            json.dump(results, f, indent=1)
    print("\n" + summarize(results))


if __name__ == "__main__":
    main()
