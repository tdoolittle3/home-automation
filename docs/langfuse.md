# Langfuse — what the LLMs are actually being asked

Every conversation with the models on [snibbly](snibbly.md) is captured: the prompt, the
response, token counts, latency, and failures. Two pieces, both on snibbly:

- **[Langfuse](https://langfuse.com) v3** (self-hosted, `/opt/stacks/langfuse`,
  [tracked compose](../snibbly/stacks/langfuse/docker-compose.yml)) stores and displays the
  traces at `http://192.168.0.19:3000`.
- **A [LiteLLM](https://docs.litellm.ai) proxy** (`litellm.service`, config under
  [snibbly/etc/litellm/](../snibbly/etc/litellm/)) took over `0.0.0.0:8080` — the address and
  API key every client already used — and passes everything through to llama-swap, which moved
  to `127.0.0.1:9292`. Its Langfuse callback logs each request/response pair, successes and
  failures both.

```
client (dashboard, agent-runner, curl…)
   │  :8080, same key as always
   ▼
LiteLLM proxy ──────────────► Langfuse web :3000 ─► Postgres / ClickHouse /
   │  127.0.0.1:9292              (traces)            Redis / MinIO (no LAN ports)
   ▼
llama-swap ─► llama-server backends (127.0.0.1:5800+)
```

No client changed: same port, same key value, same model names (the proxy routes `*` through
untouched, so llama-swap's groups and the
[cross-group eviction trap](snibbly.md#groups-what-evicts-what) behave exactly as before).

Why snibbly and not ladybird: ClickHouse is the heavy piece, snibbly has 30 GB of RAM and a
mostly idle CPU while the GPUs do the work, and ladybird (16 GB, carrying Frigate, Immich and
the house DNS) should stay lean. Same-box capture also means the proxy logs over loopback —
a trace is never lost to the LAN.

## Day to day

```bash
# the traces
open http://192.168.0.19:3000          # login = LANGFUSE_INIT_USER_* from the stack .env
# the proxy
journalctl -u litellm -f
curl http://192.168.0.19:8080/health/liveliness   # no auth; this is what Kuma watches
# the stack
docker compose -f /opt/stacks/langfuse/docker-compose.yml ps
curl http://192.168.0.19:3000/api/public/health
```

A trace appears a few seconds after the response — the proxy flushes its Langfuse batch
asynchronously, so capture adds no latency to the completion itself.

## Deploy / rebuild

Langfuse first (so the proxy has somewhere to log), then the proxy, then the llama-swap move.

1. **The stack.** Docker + compose plugin on snibbly (`apt install docker.io docker-compose-v2`
   or the upstream packages — this box had no Docker before this stack). Copy
   `snibbly/stacks/langfuse/` to `/opt/stacks/langfuse/`, create `.env` from
   [.env.example](../snibbly/stacks/langfuse/.env.example) (it documents how to mint each
   secret), `docker compose up -d`, and wait for the health endpoint above to go green. The
   `LANGFUSE_INIT_*` vars create the org, project, admin login and API keys on first start —
   no click-through needed.
2. **The proxy.** Create user `litellm`; then:

   ```bash
   sudo python3 -m venv /opt/litellm/venv       # system python, NOT a uv-managed one —
                                                # ProtectHome=true in the unit can't read
                                                # interpreters under ~/.local (the 203/EXEC
                                                # lesson from the vision worker)
   sudo /opt/litellm/venv/bin/pip install 'litellm[proxy]' 'langfuse>=2,<3'
   sudo mkdir -p /opt/litellm/cache && sudo chown -R litellm:litellm /opt/litellm/cache
   ```

   Copy [config.yaml](../snibbly/etc/litellm/config.yaml) to `/etc/litellm/`, create
   `/etc/litellm/litellm.env` from the
   [example](../snibbly/etc/litellm/litellm.env.example) (root:root, mode 600): the master
   key gets the same VALUE as `LLAMA_API_KEY`, and the Langfuse keys come from the stack's
   `.env`. Install [litellm.service](../snibbly/etc/systemd/system/litellm.service).
3. **The swap.** Copy the updated
   [llama-swap.service](../snibbly/etc/systemd/system/llama-swap.service) (listen moved to
   `127.0.0.1:9292`), then:

   ```bash
   sudo systemctl daemon-reload
   sudo systemctl restart llama-swap
   sudo systemctl enable --now litellm
   ```

   The LAN door is closed for the seconds between those two commands; nothing else notices.
4. **Verify end to end.** Run the usual curl from [snibbly.md](snibbly.md#day-to-day)
   (unchanged — same port, same key), then confirm the trace shows up at
   `http://192.168.0.19:3000` with the full prompt and response. Check a failure captures
   too: curl with a bogus model name and look for the red trace.

## Secrets — not in this repo

| Where | What |
|---|---|
| `/opt/stacks/langfuse/.env` | datastore passwords, Langfuse crypto, UI login, the `pk-lf-`/`sk-lf-` project keys — all minted at deploy, template is [.env.example](../snibbly/stacks/langfuse/.env.example) |
| `/etc/litellm/litellm.env` | the client-facing master key, the upstream `LLAMA_API_KEY`, and a copy of the two project keys |

**Rotation is now a three-file story**: the model key lives in `/etc/llama/llama.env`
(upstream) and twice in `/etc/litellm/litellm.env` (upstream copy + client-facing master key,
same value by convention). Change all three lines, restart `llama-swap` and `litellm`.

## Gotchas

- **Pin `langfuse>=2,<3` in the proxy's venv.** LiteLLM's `langfuse` callback is built on the
  v2 SDK; the v3 SDK moved to OpenTelemetry and needs the separate `langfuse_otel` callback.
  A v2 SDK talking to the v3 *server* is fine — that's the supported pairing here. If a
  litellm upgrade ever drops v2 support, switch the callback name and the pin together.
- **Port 3000 means different things per box.** On ladybird it's the EPG container; on
  snibbly it's Langfuse. Neither conflicts, but don't "fix" one to match the other.
- **The proxy is the door now.** If `litellm` is down, the models are unreachable from the
  LAN even though llama-swap is healthy on loopback — that's what the Kuma "LLM proxy"
  monitor watches. Conversely, if the Langfuse stack is down the proxy keeps serving and
  just drops traces (callbacks are fire-and-forget) — models never hostage to observability.
- **ClickHouse is the RAM hog.** Idles around 1 GB and spikes during ingestion. Snibbly has
  headroom even with the 30B MoE offloading to CPU RAM, but if the box ever swaps, this
  stack is the first suspect — cap it or stop it before blaming the models.
