#!/usr/bin/env python3
"""Two-tier agent runner for snibbly's llama-swap pairs.

The big model of a pair (8B, fast card) orchestrates; the small one (4B, slow
card) takes delegated subtasks. Delegation only ever targets the orchestrator's
own pair partner - cross-group calls make llama-swap evict the caller mid-wait
(see docs/snibbly.md). Stdlib only; deploys as a single file.

One-shot CLI:
  LLAMA_API_KEY=... ./runner.py "task..."
  ./runner.py --pair ablit --verbose "task..."

Server mode - speaks enough OpenAI API for llama-swap to run it as the
"agent" model (chat with it from any UI; delegation trace streams as
reasoning_content):
  ./runner.py --serve 5900 --pair dual
"""

import argparse
import concurrent.futures
import http.server
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

PAIRS = {
    "dual": {"orchestrator": "fast-8b", "worker": "small-4b"},
    "ablit": {"orchestrator": "8b-abliterated", "worker": "small-4b-abliterated"},
}

# Worker context is 8k tokens; leave room for instruction + output.
WORKER_INPUT_MAX_CHARS = 20_000

SYSTEM_PROMPT = """\
You are the orchestrator, a capable model paired with a smaller, weaker worker
model ({worker}) running on a second GPU. Solve the user's task, delegating
self-contained subtasks to the worker when that keeps your own context clean or
lets work proceed in parallel.

The worker is mediocre and literal. Give it closed tasks with all needed input
inline ("summarize the following to 3 bullets", "extract all dates as a JSON
array") - never open-ended ones, and never assume it remembers anything between
calls. It cannot see this conversation.

Tools:
- delegate(tasks, wait): run subtasks on the worker. With wait=true you get the
  results back directly. With wait=false you get ticket ids immediately and the
  worker runs in the background while you continue - use this when you have
  other reasoning to do meanwhile, then call collect.
- collect(tickets): wait for and fetch the results of wait=false delegations.

Delegate bulk text processing (summarize, extract, classify, reformat) instead
of doing it yourself. Do small or quality-critical steps yourself. When the
task is done, reply with the final answer as plain text and no tool calls."""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "delegate",
            "description": "Run one or more self-contained subtasks on the worker model.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tasks": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "instruction": {"type": "string", "description": "What to do, fully self-contained."},
                                "input": {"type": "string", "description": "The material to work on. May be empty."},
                            },
                            "required": ["instruction"],
                        },
                    },
                    "wait": {
                        "type": "boolean",
                        "description": "true: return results now. false: return ticket ids, fetch later with collect.",
                        "default": True,
                    },
                },
                "required": ["tasks"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "collect",
            "description": "Wait for and return results of earlier wait=false delegations.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tickets": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["tickets"],
            },
        },
    },
]

THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)

# Served at / so llama-swap's per-model chat link works for "agent" the way
# it does for llama-server backends (which ship their own web UI).
AGENT_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>agent - two-tier runner</title><style>
body{font:15px/1.5 system-ui;margin:0;background:#111;color:#ddd;display:flex;flex-direction:column;height:100vh}
header{padding:8px 14px;background:#1a1a1a;display:flex;gap:10px;align-items:center}
header b{color:#7ac}header input{flex:1;max-width:320px;background:#222;border:1px solid #333;color:#ddd;padding:4px 8px;border-radius:6px}
#log{flex:1;overflow-y:auto;padding:14px;max-width:860px;width:100%;margin:0 auto;box-sizing:border-box}
.msg{margin:10px 0;padding:10px 14px;border-radius:10px;white-space:pre-wrap;word-break:break-word}
.user{background:#1d3a5f;margin-left:15%}.bot{background:#222;margin-right:15%}
details{margin:6px 0;color:#998}details pre{white-space:pre-wrap;color:#887;margin:4px 0;font-size:13px}
form{display:flex;gap:8px;padding:12px;max-width:860px;width:100%;margin:0 auto;box-sizing:border-box}
textarea{flex:1;background:#1a1a1a;border:1px solid #333;color:#ddd;border-radius:8px;padding:8px;resize:none;font:inherit}
button{background:#2a6;border:0;color:#fff;border-radius:8px;padding:0 18px;cursor:pointer}button:disabled{background:#444}
.err{color:#e77}</style></head><body>
<header><b>agent</b><span style="color:#777">8B orchestrator + 4B worker</span>
<input id="key" type="password" placeholder="API key"></header>
<div id="log"></div>
<form id="f"><textarea id="q" rows="2" placeholder="Ask the agent... (Enter to send)"></textarea>
<button id="go">Send</button></form>
<script>
const log=document.getElementById('log'),q=document.getElementById('q'),go=document.getElementById('go'),
key=document.getElementById('key'),hist=[],base=location.pathname.endsWith('/')?location.pathname:location.pathname+'/';
key.value=localStorage.getItem('agent-key')||'';key.onchange=()=>localStorage.setItem('agent-key',key.value);
function div(cls,txt){const d=document.createElement('div');d.className='msg '+cls;d.textContent=txt;log.appendChild(d);
log.scrollTop=log.scrollHeight;return d}
async function send(ev){ev.preventDefault();const text=q.value.trim();if(!text||go.disabled)return;
q.value='';go.disabled=true;hist.push({role:'user',content:text});div('user',text);
const bot=div('bot','...'),det=document.createElement('details'),pre=document.createElement('pre');
det.innerHTML='<summary>orchestration</summary>';det.appendChild(pre);bot.before(det);let answer='';
try{
const r=await fetch(base+'v1/chat/completions',{method:'POST',headers:{'Content-Type':'application/json',
'Authorization':'Bearer '+key.value},body:JSON.stringify({model:'agent',stream:true,messages:hist})});
if(!r.ok)throw new Error('HTTP '+r.status+(r.status==401?' - set the API key (top right)':''));
const rd=r.body.getReader(),dec=new TextDecoder();let buf='';
for(;;){const{done,value}=await rd.read();if(done)break;buf+=dec.decode(value,{stream:true});
const parts=buf.split('\\n\\n');buf=parts.pop();
for(const p of parts){const line=p.trim();if(!line.startsWith('data: ')||line==='data: [DONE]')continue;
const d=JSON.parse(line.slice(6)).choices[0].delta;
if(d.reasoning_content){pre.textContent+=d.reasoning_content;det.open=true}
if(d.content){answer+=d.content;bot.textContent=answer}log.scrollTop=log.scrollHeight}}
det.open=false;hist.push({role:'assistant',content:answer});
}catch(e){bot.className='msg bot err';bot.textContent='error: '+e.message;hist.pop()}
go.disabled=false;q.focus()}
document.getElementById('f').onsubmit=send;
q.onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey)send(e)};
</script></body></html>"""


def resolve_api_key(args):
    key = os.environ.get("LLAMA_API_KEY") or args.api_key
    if not key:
        try:
            out = subprocess.run(
                ["sudo", "-n", "cat", "/etc/llama/llama.env"],
                capture_output=True, text=True, timeout=10,
            ).stdout
            key = next((l.split("=", 1)[1] for l in out.splitlines()
                        if l.startswith("LLAMA_API_KEY=")), None)
        except Exception:
            pass
    if not key:
        sys.exit("No API key: set LLAMA_API_KEY, pass --api-key, or run where sudo can read /etc/llama/llama.env")
    return key.strip()


class Client:
    def __init__(self, base_url, api_key, verbose):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.verbose = verbose

    def chat(self, model, messages, tools=None, max_tokens=2048):
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0.7,
            "top_p": 0.8,
            # The abliterated models are repetition-prone (e.g. emitting the
            # same tool call dozens of times in one response).
            "presence_penalty": 1.0,
        }
        if tools:
            payload["tools"] = tools
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                return json.load(resp)["choices"][0]["message"]
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"{model}: HTTP {e.code}: {e.read().decode()[:500]}") from e

    def log(self, msg):
        if self.verbose:
            print(f"[runner] {msg}", file=sys.stderr)


class WorkerPool:
    """Background delegation to the worker model, ticketed."""

    def __init__(self, client, model, max_tokens):
        self.client = client
        self.model = model
        self.max_tokens = max_tokens
        # Worker backend is -np 1 anyway; 2 lets a queued task start the
        # moment the first finishes without the runner in the way.
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        self.futures = {}
        self.counter = 0

    def _run(self, instruction, material):
        if len(material) > WORKER_INPUT_MAX_CHARS:
            material = material[:WORKER_INPUT_MAX_CHARS] + "\n[...input truncated...]"
        content = instruction if not material else f"{instruction}\n\nINPUT:\n{material}"
        msg = self.client.chat(self.model, [{"role": "user", "content": content}],
                               max_tokens=self.max_tokens)
        return THINK_RE.sub("", msg.get("content") or "").strip()

    def submit(self, tasks, emit):
        tickets = []
        for t in tasks:
            self.counter += 1
            ticket = f"ticket-{self.counter}"
            self.futures[ticket] = self.pool.submit(
                self._run, t.get("instruction", ""), t.get("input", ""))
            tickets.append(ticket)
            emit(f"worker <- {ticket}: {t.get('instruction', '')[:80]}")
        return tickets

    def result(self, ticket):
        # Models paraphrase ids ("ticket1", "Ticket #2") - match by number.
        m = re.search(r"(\d+)", str(ticket))
        fut = self.futures.get(f"ticket-{m.group(1)}") if m else None
        if fut is None and len(self.futures) == 1:
            fut = next(iter(self.futures.values()))
        if fut is None:
            return f"unknown ticket {ticket} (valid: {', '.join(self.futures)})"
        try:
            return fut.result(timeout=600)
        except Exception as e:
            return f"worker error: {e}"


def execute_tool(name, args, workers, emit):
    if name == "delegate":
        tickets = workers.submit(args.get("tasks", []), emit)
        if args.get("wait", True):
            return [{"ticket": t, "result": workers.result(t)} for t in tickets]
        return {"tickets": tickets, "note": "running in background; call collect"}
    if name == "collect":
        return [{"ticket": t, "result": workers.result(t)} for t in args.get("tickets", [])]
    return f"unknown tool {name}"


def run_agent(client, pair, chat_messages, max_steps, worker_tokens, emit=lambda s: None):
    """Drive the orchestrator's tool loop. Returns the final answer text."""
    workers = WorkerPool(client, pair["worker"], worker_tokens)
    messages = [{"role": "system",
                 "content": SYSTEM_PROMPT.format(worker=pair["worker"])}]
    messages += [m for m in chat_messages if m.get("role") in ("user", "assistant")]

    for step in range(max_steps):
        # A degraded model can emit tool-call JSON bad enough that the
        # server itself 500s - worth a couple of fresh samples.
        for attempt in range(3):
            try:
                msg = client.chat(pair["orchestrator"], messages, tools=TOOLS)
                break
            except RuntimeError as e:
                emit(f"orchestrator call failed (attempt {attempt + 1}/3): {e}")
        else:
            raise RuntimeError("orchestrator kept failing")
        tool_calls = msg.get("tool_calls")
        if not tool_calls:
            return THINK_RE.sub("", msg.get("content") or "").strip()
        seen = set()
        deduped = [c for c in tool_calls
                   if (k := (c["function"]["name"], c["function"].get("arguments")))
                   not in seen and not seen.add(k)]
        if len(deduped) > 8 or len(deduped) < len(tool_calls):
            emit(f"capped {len(tool_calls)} tool calls to {min(len(deduped), 8)}")
        tool_calls = deduped[:8]
        messages.append({"role": "assistant",
                         "content": msg.get("content") or "",
                         "tool_calls": tool_calls})
        for call in tool_calls:
            fn = call["function"]
            try:
                fn_args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                fn_args = {}
            emit(f"orchestrator -> {fn['name']}({json.dumps(fn_args)[:120]})")
            result = execute_tool(fn["name"], fn_args, workers, emit)
            messages.append({"role": "tool",
                             "tool_call_id": call.get("id", fn["name"]),
                             "content": json.dumps(result)})
    raise RuntimeError(f"gave up after {max_steps} steps without a final answer")


def make_handler(client, pair, run_args):
    """OpenAI-compatible facade so llama-swap can run this as a model."""

    def chunk(delta, finish=None):
        return ("data: " + json.dumps({
            "id": "agentcmpl", "object": "chat.completion.chunk",
            "created": int(time.time()), "model": "agent",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }) + "\n\n").encode()

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, fmt, *a):
            client.log(f"http: {fmt % a}")

        def _json(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                body = AGENT_PAGE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/health":
                self._json(200, {"status": "ok"})
            elif self.path == "/v1/models":
                self._json(200, {"object": "list",
                                 "data": [{"id": "agent", "object": "model"}]})
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            if self.path not in ("/v1/chat/completions", "/chat/completions"):
                return self._json(404, {"error": "not found"})
            try:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            except Exception:
                return self._json(400, {"error": "bad json"})
            msgs = body.get("messages", [])
            if body.get("stream"):
                self._stream(msgs)
            else:
                try:
                    text = run_agent(client, pair, msgs, **run_args)
                except Exception as e:
                    return self._json(500, {"error": {"message": str(e)}})
                self._json(200, {
                    "id": "agentcmpl", "object": "chat.completion",
                    "created": int(time.time()), "model": "agent",
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": text}}],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                })

        def _stream(self, msgs):
            # Progress goes out as reasoning_content deltas - the llama.cpp
            # web UI shows them as a live collapsible "thinking" block.
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            events = queue.Queue()
            box = {}

            def work():
                try:
                    box["text"] = run_agent(client, pair, msgs, **run_args,
                                            emit=lambda s: events.put(s))
                except Exception as e:
                    box["text"] = f"agent error: {e}"
                events.put(None)

            threading.Thread(target=work, daemon=True).start()
            self.wfile.write(chunk({"role": "assistant"}))
            while (ev := events.get()) is not None:
                self.wfile.write(chunk({"reasoning_content": ev + "\n"}))
                self.wfile.flush()
            self.wfile.write(chunk({"content": box.get("text", "")}))
            self.wfile.write(chunk({}, finish="stop"))
            self.wfile.write(b"data: [DONE]\n\n")

    return Handler


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("task", nargs="?", help="the task, or - to read it from stdin")
    ap.add_argument("--pair", choices=PAIRS, default="dual")
    ap.add_argument("--base-url", default="http://localhost:8080/v1")
    ap.add_argument("--api-key", default=None)
    ap.add_argument("--max-steps", type=int, default=10)
    ap.add_argument("--worker-tokens", type=int, default=1024)
    ap.add_argument("--serve", type=int, metavar="PORT",
                    help="run as an OpenAI-compatible server instead of one-shot")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    client = Client(args.base_url, resolve_api_key(args), args.verbose)
    pair = PAIRS[args.pair]
    run_args = {"max_steps": args.max_steps, "worker_tokens": args.worker_tokens}

    if args.serve:
        server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", args.serve), make_handler(client, pair, run_args))
        client.log(f"agent server on 127.0.0.1:{args.serve} (pair: {args.pair})")
        server.serve_forever()
        return

    if not args.task:
        ap.error("a task is required unless --serve is given")
    task = sys.stdin.read() if args.task == "-" else args.task
    emit = client.log if args.verbose else (lambda s: None)
    try:
        print(run_agent(client, pair, [{"role": "user", "content": task}],
                        emit=emit, **run_args))
    except RuntimeError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
