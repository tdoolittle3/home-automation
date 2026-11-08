#!/usr/bin/env python3
"""Validate OpenAI-style tool calling through the litellm -> llama-swap -> llama.cpp
stack. This is the make-or-break capability for the Hermes agent harness: if a
model won't reliably emit well-formed tool_calls here, it can't drive an agent.

    LLAMA_API_KEY=... python3 toolcall_test.py fast-8b big-30b-thinking

Prints, per model: whether it chose the right tool, with valid JSON args, and
whether it declined to hallucinate an answer instead of calling the tool.
"""
import json
import os
import sys
import urllib.request

BASE = os.environ.get("LLAMA_BASE", "http://127.0.0.1:8080")
KEY = os.environ.get("LLAMA_API_KEY", "")

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_garage_door_state",
            "description": "Return whether the garage door is currently open or closed.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_light",
            "description": "Turn a named light on or off.",
            "parameters": {
                "type": "object",
                "properties": {
                    "area": {"type": "string", "description": "room name, e.g. kitchen"},
                    "state": {"type": "string", "enum": ["on", "off"]},
                },
                "required": ["area", "state"],
            },
        },
    },
]

PROMPT = "Is the garage door open? Also please turn off the kitchen light."


def call(model):
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": PROMPT}],
        "tools": TOOLS,
        "tool_choice": "auto",
        "temperature": 0.2,
        "max_tokens": 512,
    }).encode()
    req = urllib.request.Request(
        BASE.rstrip("/") + "/v1/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + KEY},
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.load(r)


def grade(model):
    print(f"\n=== {model}")
    try:
        out = call(model)
    except Exception as e:
        print(f"  REQUEST FAILED: {e}")
        return False
    msg = out["choices"][0]["message"]
    calls = msg.get("tool_calls") or []
    if not calls:
        print(f"  NO TOOL CALLS. content: {(msg.get('content') or '')[:200]!r}")
        return False
    names, ok_args = [], True
    for c in calls:
        fn = c["function"]["name"]
        names.append(fn)
        try:
            args = json.loads(c["function"]["arguments"] or "{}")
        except json.JSONDecodeError:
            print(f"  {fn}: INVALID JSON ARGS: {c['function']['arguments']!r}")
            ok_args = False
            continue
        print(f"  {fn}({json.dumps(args)})")
    got_garage = "get_garage_door_state" in names
    light = next((c for c in calls if c["function"]["name"] == "set_light"), None)
    light_ok = False
    if light:
        a = json.loads(light["function"]["arguments"] or "{}")
        light_ok = a.get("area", "").lower() == "kitchen" and a.get("state") == "off"
    ft = out.get("timings", {})
    if ft:
        print(f"  [prefill {ft.get('prompt_per_second',0):.0f} tok/s, "
              f"decode {ft.get('predicted_per_second',0):.1f} tok/s]")
    verdict = got_garage and light_ok and ok_args
    print(f"  VERDICT: {'PASS' if verdict else 'PARTIAL'} "
          f"(garage={got_garage}, set_light kitchen/off={light_ok}, valid_args={ok_args})")
    return verdict


if __name__ == "__main__":
    models = sys.argv[1:] or ["fast-8b"]
    if not KEY:
        sys.exit("set LLAMA_API_KEY")
    results = {m: grade(m) for m in models}
    print("\n=== summary:", {m: ("PASS" if v else "FAIL/PARTIAL") for m, v in results.items()})
