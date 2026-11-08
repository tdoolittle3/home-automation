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

Nothing here auto-deploys — `hermes` is intentionally left out of the ansible
`stacks_whole_dirs` / `stacks_auto_up` lists, so `converge` ignores it until you
opt in. First run is manual.

### 1. Build the image (no registry pull)

```
git clone https://github.com/NousResearch/hermes-agent ~/src/hermes-agent
cd ~/src/hermes-agent && docker build -t hermes-agent:latest .
```

### 2. Create the limited Home Assistant user + token  ← the safety boundary

In HA: **Settings → People → Users → Add**, a *non-admin* user (e.g.
`hermes-agent`). Log in as that user, then **Profile → Long-lived access tokens
→ Create**. Whatever that user can see and control is the ceiling on what the
agent can do, regardless of what the model asks for. Put the token in `data/.env`
as `HASS_TOKEN`.

### 3. Configure

```
cd /opt/stacks/hermes            # after the stack dir is synced here
mkdir -p data
cp .env.example data/.env && chmod 600 data/.env   # fill in keys/tokens
cp config.yaml.example data/config.yaml            # or run `hermes setup`
```

`data/.env` holds the litellm key, HA token, Telegram token.
`data/config.yaml` points the `snibbly` provider at litellm and selects
`coder-30b`. Running `docker compose run --rm gateway hermes setup` interactively
is the authoritative way to write the provider block (it confirms the api_key var
name).

### 4. Sandbox — choose the agent's shell backend

The agent's general shell should not run with the gateway's credentials. Two
supported options (set `terminal.backend` in `config.yaml`):

- **`ssh`** (recommended): a dedicated low-priv user on snibbly. The agent can't
  read ladybird's `.env` or edit its own code; the remote user is the blast
  radius. Set `TERMINAL_SSH_*` in `.env`.
- **`docker`**: ephemeral containers on ladybird. Faster to set up but needs the
  `/var/run/docker.sock` mount in `docker-compose.yml`, which is
  **root-equivalent** on ladybird. Only enable if you accept that.

Privileged actions (HA calls, service restarts) are **separate scoped tools**,
not the general shell — so the shell stays sandboxed while narrow, allowlisted
tools do the risky work.

### 5. First run + gateway

```
HERMES_UID=$(id -u) HERMES_GID=$(id -g) docker compose up -d
docker compose logs -f gateway
docker compose run --rm gateway hermes gateway setup   # Telegram: checks the bot token, allowlists you
```

Message your bot: "is the garage door open?" should trigger a tool call and a
real answer. Watch the trace land in Langfuse on snibbly.

### 6. Make it permanent (optional)

To have `converge` manage it, add `hermes` to `stacks_whole_dirs` and
`stacks_auto_up` in the ansible vars. Until then it's a manual stack.

## Scoped tools — the build-out

- **Home Assistant plugin**: `hermes plugins install homeassistant` → the four
  HA tools (list entities, get state, list services, call service). The limited
  user is the ceiling.
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
