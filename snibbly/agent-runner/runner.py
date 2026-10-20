#!/usr/bin/env python3
"""Two-tier agent runner for snibbly's llama-swap models.

The 8B orchestrates; delegated subtasks run on the *same* model's second
parallel slot (-np 2). GPU 1 belongs to the vision pipeline since 2026-10-20,
so the old 4B pair partner is gone — same-model delegation keeps the two
things that mattered: bulk text stays out of the orchestrator's context, and
a wait=false delegation grinds on slot 2 while the orchestrator reasons on
slot 1. Delegation never leaves the orchestrator's own group — cross-group
calls make llama-swap evict the caller mid-wait (see docs/snibbly.md).
Stdlib only; deploys as a single file.

  LLAMA_API_KEY=... ./runner.py "task..."
  ./runner.py --pair ablit --verbose "task..."
"""

import argparse
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

PAIRS = {
    "dual": {"orchestrator": "fast-8b", "worker": "fast-8b"},
    "ablit": {"orchestrator": "8b-abliterated", "worker": "8b-abliterated"},
}

# Each -np 2 slot gets half the model's context (fast-8b: 16k/2 = 8k per slot);
# leave room for instruction + output.
WORKER_INPUT_MAX_CHARS = 20_000

SYSTEM_PROMPT = """\
You are the orchestrator. A worker ({worker}) is available on a second parallel
slot of your backend. Solve the user's task, delegating self-contained subtasks
to the worker when that keeps your own context clean or lets work proceed in
parallel with your own reasoning.

The worker is stateless and literal. Give it closed tasks with all needed input
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
        # The backend has 2 slots and the orchestrator holds one only while it
        # is itself generating; 2 threads let a queued task start the moment a
        # slot frees without the runner in the way.
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

    def submit(self, tasks):
        tickets = []
        for t in tasks:
            self.counter += 1
            ticket = f"ticket-{self.counter}"
            self.futures[ticket] = self.pool.submit(
                self._run, t.get("instruction", ""), t.get("input", ""))
            tickets.append(ticket)
            self.client.log(f"worker <- {ticket}: {t.get('instruction', '')[:80]}")
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


def execute_tool(name, args, workers, client):
    if name == "delegate":
        tickets = workers.submit(args.get("tasks", []))
        if args.get("wait", True):
            return [{"ticket": t, "result": workers.result(t)} for t in tickets]
        return {"tickets": tickets, "note": "running in background; call collect"}
    if name == "collect":
        return [{"ticket": t, "result": workers.result(t)} for t in args.get("tickets", [])]
    return f"unknown tool {name}"


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("task", help="the task, or - to read it from stdin")
    ap.add_argument("--pair", choices=PAIRS, default="dual")
    ap.add_argument("--base-url", default="http://localhost:8080/v1")
    ap.add_argument("--api-key", default=None)
    ap.add_argument("--max-steps", type=int, default=10)
    ap.add_argument("--worker-tokens", type=int, default=1024)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    task = sys.stdin.read() if args.task == "-" else args.task
    pair = PAIRS[args.pair]
    client = Client(args.base_url, resolve_api_key(args), args.verbose)
    workers = WorkerPool(client, pair["worker"], args.worker_tokens)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT.format(worker=pair["worker"])},
        {"role": "user", "content": task},
    ]

    for step in range(args.max_steps):
        # A degraded model can emit tool-call JSON bad enough that the
        # server itself 500s - worth a couple of fresh samples.
        for attempt in range(3):
            try:
                msg = client.chat(pair["orchestrator"], messages, tools=TOOLS)
                break
            except RuntimeError as e:
                client.log(f"orchestrator call failed (attempt {attempt + 1}/3): {e}")
        else:
            sys.exit("orchestrator kept failing; giving up")
        tool_calls = msg.get("tool_calls")
        if not tool_calls:
            print(THINK_RE.sub("", msg.get("content") or "").strip())
            return
        seen = set()
        deduped = [c for c in tool_calls
                   if (k := (c["function"]["name"], c["function"].get("arguments")))
                   not in seen and not seen.add(k)]
        if len(deduped) > 8 or len(deduped) < len(tool_calls):
            client.log(f"capped {len(tool_calls)} tool calls to {min(len(deduped), 8)}")
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
            client.log(f"orchestrator -> {fn['name']}({json.dumps(fn_args)[:120]})")
            result = execute_tool(fn["name"], fn_args, workers, client)
            messages.append({"role": "tool",
                             "tool_call_id": call.get("id", fn["name"]),
                             "content": json.dumps(result)})
    sys.exit(f"gave up after {args.max_steps} steps without a final answer")


if __name__ == "__main__":
    main()
