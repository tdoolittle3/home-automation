# `snibbly` — the LLM box

A second machine, separate from ladybird: a desktop with two NVIDIA cards at `192.168.0.19` that
serves local LLMs to the LAN, plus (in progress) the camera [vision pipeline](vision.md). The
`snibbly/` tree in this repo mirrors its config files the same way `host/` and `stacks/` mirror
ladybird's.

| | |
|---|---|
| **Machine** | i7-9700KF, 30 GB RAM, Ubuntu 26.04 LTS, kernel 7.0 |
| **GPU 0** | RTX 2070 Super, 8 GB — the fast card, the LLMs' card |
| **GPU 1** | GTX 1050 Ti, 4 GB — reserved for the vision pipeline since 2026-10-20 (a quarter of GPU 0's memory bandwidth; LLM inference on it was dismal, vision CNNs fit it fine) |
| **Router** | [llama-swap](https://github.com/mostlygeek/llama-swap) v265 at `/opt/llama-swap`, listening on `0.0.0.0:8080` |
| **Backend** | llama.cpp CUDA build (b11382) at `/opt/llama.cpp/build-cuda/bin/llama-server` |
| **Models** | GGUF files in `/opt/models/` (thomas-writable) |

llama-swap is the only door: it owns `:8080`, speaks the OpenAI API, and starts/stops llama-server
backends on demand. The backends bind to `127.0.0.1` on ephemeral ports (5800+) with no auth of
their own. One API key guards everything — see [Secrets](#secrets--not-in-this-repo).

Config lives at `/etc/llama-swap/config.yaml` ([tracked copy](../snibbly/etc/llama-swap/config.yaml)),
run by the `llama-swap.service` systemd unit as user `llama`. The `-watch-config` flag means config
edits apply on the next model load — no restart needed.

## The models

| Name on the API | What it is | Card | ~VRAM |
|---|---|---|---|
| `fast-8b` | Qwen3-8B Q4_K_M, 16k ctx, 2 slots | GPU 0 | 5.8 GB |
| `big-30b-thinking` | Qwen3-30B-A3B-Thinking Q4_K_M, MoE layers on CPU | GPU 0 + RAM | — |
| `big-14b` | Qwen3-14B Q4_K_M, split 7/1 across both cards | both | — |
| `8b-abliterated` | Qwen3-8B abliterated (uncensored), 8k ctx, 2 slots | GPU 0 | 5.5 GB |

The 4B models (`small-4b`, `small-4b-abliterated`) that used to occupy GPU 1 were **removed
2026-10-20** to hand the card to the vision pipeline. They were never fast anyway — measured
2026-10-15: ~72 tok/s for an 8B on GPU 0, ~22 tok/s for a 4B on GPU 1, because the 1050 Ti's
memory bandwidth is the bottleneck. Their GGUFs are still in `/opt/models/` if they're ever
needed back. `big-14b` still borrows ~1.1 GB of GPU 1 through its 7/1 split; that coexists with
the vision models' ~2 GB but with little headroom — see the comment in
[config.yaml](../snibbly/etc/llama-swap/config.yaml).

The abliterated model answers noticeably worse than its stock twin — ablation (surgically
removing the refusal direction from the weights, with no retraining to heal the damage) costs
coherence. Expected, not a config problem.

## Groups: what evicts what

Requesting a model loads **its whole group** and evicts every other group (`exclusive: true` on all
three). Group-switching costs ~10–20 s of model loading.

| Group | Members |
|---|---|
| `dual` | `fast-8b` |
| `ablit` | `8b-abliterated` |
| `big` | `big-30b-thinking` ↔ `big-14b` (they swap with each other) |

Two 8B-class models can never share GPU 0; that is why the stock and abliterated models are separate
exclusive groups rather than one resident set.

**Trap: never have a model call a model in another group.** A request to the `dual` model while
`ablit` is serving (or vice versa) triggers an eviction *of the caller* — if the caller is blocked
waiting on that response, that's a deadlock until timeout. Anything agent-like must delegate only
within its own group (since 2026-10-20 that means the same model's second slot).

## Day to day

```bash
# what's loaded right now
nvidia-smi --query-gpu=index,memory.used,memory.total --format=csv,noheader
# llama-swap logs
journalctl -u llama-swap -f
# talk to it (any OpenAI client works the same way)
curl http://192.168.0.19:8080/v1/chat/completions \
  -H "Authorization: Bearer $LLAMA_API_KEY" -H "Content-Type: application/json" \
  -d '{"model":"fast-8b","messages":[{"role":"user","content":"hi"}]}'
```

Qwen3-8B is a *thinking* model: it spends tokens inside `<think>…</think>` before answering. A
request with a small `max_tokens` can burn the whole budget thinking and return empty content —
raise the budget, don't debug the model.

## The agent runner

[snibbly/agent-runner/runner.py](../snibbly/agent-runner/runner.py) (deployed to
`/opt/agent-runner/` on the box, stdlib-only) turns a model into a two-tier agent: the 8B
orchestrates with tool calling, and a `delegate` tool runs closed subtasks (summarize, extract,
classify, reformat) on the **same model's second slot** (every 8B runs `-np 2`). `delegate` with
`wait=false` plus a later `collect` lets a subtask grind on slot 2 while the orchestrator keeps
reasoning on slot 1 — slot concurrency, and keeping bulk text out of the orchestrator's context,
is the whole point. Until 2026-10-20 the worker was a 4B on GPU 1; that card now belongs to the
vision pipeline, and same-model delegation replaced it (each slot sees half the context: 8k for
`fast-8b`, 4k for `8b-abliterated`). Delegation stays inside the group by construction, which
sidesteps the cross-group eviction trap.

```bash
# on snibbly (key auto-read via sudo), stock pair:
/opt/agent-runner/runner.py --verbose "task..."
# uncensored pair, from anywhere on the LAN:
LLAMA_API_KEY=... ./runner.py --pair ablit --base-url http://192.168.0.19:8080/v1 "task..."
```

## Secrets — not in this repo

One secret: `LLAMA_API_KEY` in `/etc/llama/llama.env` (root:root, mode 600 — read it with
`sudo cat`). Template at [snibbly/etc/llama/llama.env.example](../snibbly/etc/llama/llama.env.example).
It is the only thing standing between the LAN and the models; the backends have no auth.

## Rebuild sketch

1. Ubuntu + NVIDIA driver; verify both cards in `nvidia-smi`.
2. Build llama.cpp with CUDA into `/opt/llama.cpp/build-cuda`; download a llama-swap release to
   `/opt/llama-swap`.
3. Create user `llama`; copy the three files under [snibbly/etc/](../snibbly/etc/) into `/etc/`,
   fill in a fresh `LLAMA_API_KEY`.
4. Re-download the GGUFs in the table above into `/opt/models/` (names must match
   `config.yaml`).
5. `systemctl enable --now llama-swap`, then curl a completion as above.
