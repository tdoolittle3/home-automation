# Hermes Agent — the homelab "Operator" harness

Hermes Agent ([NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent),
MIT) turns the local LLM from a chat box into an **agent**: it loops
call-tool → observe-result → decide-next-step, so it can actually *do* things —
answer "is the garage open?", build a dashboard and hand back a URL, restart a
service — instead of only describing how.

Stack files: `stacks/hermes/`. This doc is the deploy walkthrough.

## Architecture

```
  you (Telegram / dashboard)
        │
        ▼
  Hermes gateway  ──────────────►  Home Assistant (.13:8123)   [tools execute here, on ladybird]
  (ladybird, this stack)           scoped service-restart, etc.
        │
        │  OpenAI /v1/chat/completions
        ▼
  litellm (snibbly .19:8080) ─► llama-swap ─► coder-30b   [inference + Langfuse trace]
```

The **brain** is snibbly (the coder model at 64k context). The **loop and the
hands** are Hermes on ladybird — lightweight, no GPU. Model calls flow through
litellm, so every step is captured in Langfuse.

## Trust model — read before adding tools

Two profiles, **never mixed**:

- **Operator** (this stack): the *clean* `coder-30b` (and later the clean 35B).
  Gets real tools — HA control, scoped service-restart, preview deploys.
- **Lab** (not this stack): DeepHat / heretic / abliterated models. Chat-only or
  an isolated sandbox with **no** prod/HA/file-write access.

Abliterated models are kept away from tools on purpose: stripping refusals also
degrades tool-calling discipline and removes the safety margin that matters when
a tool can actuate. The real guardrail is always the **tool allowlist and the
limited HA user**, not the prompt.

## Deploy

Since 2026-11-08 the stack dir **is ansible-managed**: `hermes` is in
`stacks_whole_dirs`, so every converge syncs `docker-compose.yml`, the
`*.example` files and `skills/` to `/opt/stacks/hermes/` (the gitignored
`data/` dir survives — copy never deletes). It is deliberately **not** in
`stacks_auto_up`: the image is built locally and the gateway can't start
before `data/.env` exists, so after a synced compose change you still run
`docker compose up -d` there yourself ([ansible.md](ansible.md)).

The steps below are the one-time bring-up on a fresh box (they were first run
manually 2026-11-08; converge now keeps step-3's tracked inputs in sync).

### 1. Build the image (no registry pull)

```
git clone https://github.com/NousResearch/hermes-agent ~/src/hermes-agent
cd ~/src/hermes-agent && docker build -t hermes-agent:latest .
```

### 2. Create the limited Home Assistant user + token  ← the safety boundary

In HA: **Settings → People → Users → Add**, a *non-admin* user (e.g. `Snibbly`).
Then **log in as that user** — the token UI only appears on your own profile, not
the admin user-management screen — and go to **Profile → Security → Long-Lived
Access Tokens → Create** (direct link: `http://192.168.0.13:8123/profile/security`;
use a desktop browser, not the phone app). Whatever that user can see and control
is the ceiling on what the agent can do. Put the token in `data/.env` as
`HASS_TOKEN`. (Optionally tighten with `HASS_ENTITY_ALLOWLIST`/`DENYLIST` — see
.env.example.)

### 3. Configure

```
cd /opt/stacks/hermes            # stack dir (ansible-synced; data/ is yours)
mkdir -p data
cp .env.example data/.env && chmod 600 data/.env   # fill in keys/tokens
cp config.yaml.example data/config.yaml
# homelab skills ship in the synced skills/ dir; hermes loads them from data/
mkdir -p data/skills/homelab
cp -r skills/homelab/* data/skills/homelab/
```

`data/config.yaml` sets `model.default: openai/coder-30b`; `data/.env` sets
`OPENAI_BASE_URL` (snibbly litellm) + `OPENAI_API_KEY` (the litellm key) plus the
HA and Telegram secrets. Hermes's schema is `model.default` + OpenAI env vars —
**not** a `providers:` block.

**litellm alias (required once):** Hermes sends the model as `openai/coder-30b`,
prefix and all. litellm's wildcard would double-prefix that, so add an explicit
alias mapping `openai/coder-30b` → the bare `coder-30b`, plus `drop_params: true`
(the coder rejects Hermes's `reasoning.effort` param otherwise). Both live in
`snibbly/etc/litellm/config.yaml`; deploy and restart litellm.

### 4. Enable Home Assistant + pick the shell posture

The HA plugin ships **installed but disabled** — enabling it is the step that
registers the `ha_*` tools (this is easy to miss):

```
docker compose run --rm gateway hermes plugins enable homeassistant
```

Then choose the agent's code-execution posture:

- **No-shell** (HA/chat only — safe first phase): disable the shell toolsets —
  `docker compose run --rm gateway hermes tools disable terminal code_execution computer_use`
- **Sandboxed shell** (for dashboard/code work): set `terminal.backend` to
  `docker` (uncomment the `docker.sock` mount in docker-compose.yml —
  root-equivalent on ladybird) or `ssh` (a low-priv sandbox user; set
  `TERMINAL_SSH_*`). Backend `none` is invalid and just logs errors.

Privileged homelab actions (HA calls, service restarts) are **separate scoped
tools**, not the general shell — so the shell stays sandboxed while narrow,
allowlisted tools do the risky work.

### 5. First run + validate

```
HERMES_UID=$(id -u) HERMES_GID=$(id -g) docker compose up -d
# one-shot headless validation (no messaging gateway needed):
docker compose run --rm gateway hermes -z "Reply with one word: pong"
docker compose run --rm gateway hermes -z "Using Home Assistant, is the garage door open? Read-only."
# messaging gateway (Telegram), when ready:
docker compose run --rm gateway hermes gateway setup
```

Each `docker compose run` re-runs the container init (~20s, noisy — filter the
s6/cont-init lines). Model calls trace into Langfuse on snibbly.

### 6. Day-two changes

Tracked-file changes (compose, examples, skills) land via PR → converge, then
`docker compose up -d` in `/opt/stacks/hermes` to apply (not auto-up'd — see
the Deploy intro). `data/` changes (config.yaml, .env, enabling plugins) are
live edits on the box, same as ever. Upstream agent updates = re-pull the
`~/src/hermes-agent` clone, rebuild the image, `up -d`.

## Scoped tools — the build-out

- **Home Assistant plugin** (VALIDATED 2026-11-08): `hermes plugins enable
  homeassistant` registers ha_list_entities / ha_get_state / ha_list_services /
  ha_call_service. The non-admin user + optional HASS_ENTITY_ALLOWLIST/DENYLIST
  are the ceiling.
- **Radio logs** (read-only data, not a tool): `/opt/stacks/radiolog/data`
  (Meshtastic chat, ISM sensor decodes, ADS-B sightings — docs/radiolog.md) is
  mounted at `workspace/radiolog/` in the gateway, and the `radio-logs` skill
  (`stacks/hermes/skills/homelab/radio-logs/`, copied into `data/skills/homelab/`)
  teaches the agent the file layout and schemas. The `:ro` mount is the
  guarantee that a confused agent can't rewrite history.
- **Vision search** (MCP, LIVE 2026-11-09): the camera archive on snibbly answers
  natural-language queries — `vision-mcp` on `:8097` exposes `mcp_vision_search` /
  `recent_events` / `frames_with` / `entities` via Hermes's native MCP client
  (the `mcp_servers:` block in config.yaml.example). Read-only by construction;
  see [docs/vision.md](vision.md) "Agent search" for deploy + validation.
  Tool routing: the model's first instinct for "who was by the driveway?" was
  HA's coarse frigate sensors — countered twice, in the server's tool
  descriptions/instructions and in the `camera-archive` skill
  (`skills/homelab/camera-archive/`, copied to `data/skills/homelab/` like
  radio-logs). If routing regresses, sharpen those, not the prompt.
- **Service restart**: a narrow tool/skill with a fixed allowlist of unit names,
  not raw shell. Services on ladybird restart locally; snibbly services
  (llama-swap, vision) restart over SSH — the tool must know which host owns each.
- **Preview deploys**: the agent writes a site to a previews dir that a small web
  server (Caddy) serves, and hands back the URL. (Separate stack, TBD.)

## Related

- `snibbly/etc/llama-swap/config.yaml` — the `coder-30b` entry (64k, full MoE
  offload, `--jinja`).
- `snibbly/bench/` — tool-calling validation (`toolcall_test.py`) and the bench
  that proved the box stable enough for standing agent duty.
- `docs/langfuse.md` — where the agent's every model call is traced.
