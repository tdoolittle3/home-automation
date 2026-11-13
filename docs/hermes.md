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

- **Operator** (the default profile): the *clean* `coder-30b` (and later the
  clean 35B). Gets real tools — HA control, scoped service-restart, preview
  deploys.
- **Lab** (`profiles/lab/` in this stack): DeepHat / heretic / abliterated
  models. **Chat-only** — no terminal backend, no plugins, no MCP servers, and
  the shell toolsets disabled inside the profile. See "The Lab profile" below.

Abliterated models are kept away from tools on purpose: stripping refusals also
degrades tool-calling discipline and removes the safety margin that matters when
a tool can actuate. The real guardrail is always the **tool allowlist and the
limited HA user**, not the prompt.

## Deploy

Since 2026-11-08 the stack dir **is ansible-managed**: `hermes` is in
`stacks_whole_dirs`, so every converge syncs `docker-compose.yml`, the
`*.example` files, `SOUL.md` and `skills/` to `/opt/stacks/hermes/`, and
installs `SOUL.md` + `skills/homelab/` into `data/` (the rest of the
gitignored `data/` dir survives — copy never deletes). It is deliberately **not** in
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
```

`data/SOUL.md` and `data/skills/homelab/` are **not** copied by hand: the
converge installs them from `stacks/hermes/SOUL.md` and
`stacks/hermes/skills/homelab/` (see "Tool routing" below).

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
  `docker` (the deployed posture; needs the `docker.sock` mount in
  docker-compose.yml — root-equivalent on ladybird) or `ssh` (a low-priv sandbox user; set
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

Tracked-file changes (compose, examples, SOUL.md, skills) land via PR →
converge, then `docker compose up -d` in `/opt/stacks/hermes` to apply a
compose change (not auto-up'd — see the Deploy intro). Other `data/` changes
(config.yaml, .env, enabling plugins) are live edits on the box, same as ever. Upstream agent updates = re-pull the
`~/src/hermes-agent` clone, rebuild the image, `up -d`.

## Scoped tools — the build-out

- **Home Assistant plugin** (VALIDATED 2026-11-08): `hermes plugins enable
  homeassistant` registers ha_list_entities / ha_get_state / ha_list_services /
  ha_call_service. The non-admin user + optional HASS_ENTITY_ALLOWLIST/DENYLIST
  are the ceiling.
- **Radio logs** (read-only data, not a tool): `/opt/stacks/radiolog/data`
  (Meshtastic chat, ISM sensor decodes, ADS-B sightings — docs/radiolog.md) is
  mounted at `workspace/radiolog/` in the gateway, and the `radio-logs` skill
  (`stacks/hermes/skills/homelab/radio-logs/`, installed into `data/skills/homelab/` by the converge)
  teaches the agent the file layout and schemas. The `:ro` mount is the
  guarantee that a confused agent can't rewrite history. **The compose mount
  alone is not enough:** with `terminal.backend: docker` the agent's commands run
  in a sandbox child container that only gets `terminal.docker_volumes`, so the
  same host path must be listed there too (config.yaml.example). Same for the
  Frigate incident bundles. Symptom when missing: the skill is chosen correctly
  but the agent reports the directory doesn't exist.
  Known limit: the 30B model mis-scans dozens of JSONL records by eye (asked
  for lowest/closest plane it named the wrong aircraft) — aggregation wants a
  helper script, not eyeballing — the radio-logs skill now carries a ready-to-run
  summary snippet (lowest/closest/fastest), verified to give correct answers.
- **Vision search** (MCP, LIVE 2026-11-09): the camera archive on snibbly answers
  natural-language queries — `vision-mcp` on `:8097` exposes `mcp_vision_search` /
  `recent_events` / `frames_with` / `entities` via Hermes's native MCP client
  (the `mcp_servers:` block in config.yaml.example). Read-only by construction;
  see [docs/vision.md](vision.md) "Agent search" for deploy + validation.
  Tool routing: the model's first instinct for "who was by the driveway?" was
  HA's coarse frigate sensors — countered twice, in the server's tool
  descriptions/instructions and in the `camera-archive` skill
  (`skills/homelab/camera-archive/`, installed to `data/skills/homelab/` like
  radio-logs). If routing regresses, sharpen those, not the prompt.
- **Service restart**: a narrow tool/skill with a fixed allowlist of unit names,
  not raw shell. Services on ladybird restart locally; snibbly services
  (llama-swap, vision) restart over SSH — the tool must know which host owns each.
- **Preview deploys**: the agent writes a site to a previews dir that a small web
  server (Caddy) serves, and hands back the URL. (Separate stack, TBD.)

## Telegram layout

Three places to talk to the one bot, each with a job:

- **DM** — interactive Operator chat. Topics are enabled (Bot API 9.4
  "Threaded Mode" turned on in BotFather, then `/topic` in the DM): each topic
  is an isolated session, which is the standing fix for long-session anchoring
  (a chat full of camera answers steering a 30B toward camera tools for every
  question). Start a topic per theme; `/new` still resets within one.
- **"Ladybird tasks" group** — the home channel (`/sethome` was sent there):
  cron output, gateway announcements and background-task updates land here.
- **"Ladybird Lab" group** — routed to the Lab profile (next section). Clean
  chats never see it; it never sees tools.

## The Lab profile — one gateway, two brains

One bot token, one gateway process; `gateway.multiplex_profiles` +
`gateway.profile_routes` (in the **Operator's** `data/config.yaml`) route the
Lab group's `chat_id` to the `lab` profile. Every unrouted chat falls through
to the Operator. Each profile has its own config, SOUL, skills, sessions and
memory — the isolation is structural, not prompt-level. (Feature shipped
upstream Jul 2026 as `profile_routes`; if the gateway logs an unknown-key
error on restart, the image predates it — rebuild from upstream.)

Bring-up (owner, on ladybird, in `/opt/stacks/hermes`):

```
# 1. create the Lab group in Telegram, add the bot, send one message in it,
#    then read the group's chat id:
cat data/channel_directory.json
# 2. create the profile and install its config + soul (examples are synced
#    into this dir by the converge):
docker compose run --rm gateway hermes profiles create lab
cp profiles/lab/config.yaml.example data/profiles/lab/config.yaml
cp profiles/lab/SOUL.md.example    data/profiles/lab/SOUL.md
docker compose run --rm gateway hermes -p lab tools disable terminal code_execution computer_use
# 3. in data/config.yaml: add the cron:/gateway: blocks from
#    config.yaml.example, with the real chat_id; then
docker compose restart gateway
# 4. verify: message the Lab group (expect the abliterated voice, and a
#    refusal-by-design if asked to touch HA); message the DM (Operator
#    unchanged). `hermes -p lab tools list` should show no shell toolsets.
```

The Lab model (`8b-abliterated`) lives in llama-swap's `ablit` group, so a Lab
conversation evicts whatever the `big` group had loaded — Operator replies
stall for one model-load (~tens of seconds) after Lab use, and vice versa. The
two brains time-share the GPU; that's the accepted cost.

## Scheduled jobs — the thinking engine

The long-term value here is standing scans, not ad-hoc chat: cron jobs the
agent sets up itself (`/cron add "every morning at 7" "…"`, or just ask it)
that read the radio logs, camera archive and HA history and report to the home
channel. Jobs run unattended, so they get the smarter-but-slower model:
`cron.model: openai/big-30b-thinking` in `data/config.yaml` points every job
that doesn't pin its own model at the Thinking tune (same 30B weights as
coder, extended reasoning — and now 64k context in llama-swap, same full-MoE-
offload recipe as coder-30b, since an agent loop doesn't fit in 16k).

Mechanics worth knowing:

- Model resolution per job: per-job pin (`--model`/`--pin`) → `cron.model` →
  the profile default. Delivery default is the chat that created the job;
  `deliver: telegram` sends to the home channel ("Ladybird tasks").
- llama-swap group-swaps `big` members: a firing job evicts `coder-30b`, the
  next DM message swaps it back. Each swap costs a model load, so point heavy
  scans at quiet hours and batch them rather than scattering hourly one-liners.
- Jobs live in `data/cron/jobs.json`; `hermes cron list` / `/cron list` to
  audit what the agent has scheduled for itself.

## Nightly eval — is the agent getting better or worse?

Every change to tools, skills, SOUL.md or the model can quietly break an answer
that used to work. [`scripts/hermes_eval.py`](../stacks/hermes/scripts/hermes_eval.py)
asks the live agent a fixed set of golden questions
([`hermes_eval_questions.json`](../stacks/hermes/scripts/hermes_eval_questions.json))
every night and grades the answers against ground truth fetched at eval time.

- **How a question runs:** `hermes chat -q … --format stream-json`, the same config,
  SOUL.md, skills and tools as Telegram. The JSONL stream gives the final answer and
  every tool called (the lazy loader's `tool_call` wrapper is unwrapped). The eval's
  sessions are deleted afterwards so they don't show up in `session_search`.
- **Ground truth:** vision-mcp (`tags`, `recent_events`, `timeline`) for camera facts,
  the HA REST API for device state, the radio-log JSONL for aircraft, or fixed facts for
  anchored history ("garbage truck 09:53, Wed Nov 11") that must stay answerable forever.
- **Grading:** rule checks first. These cover which tools were (not) called, a clock
  time within ±3 min (12/24 h aware), the right day (weekday, date or
  "yesterday"), a real callsign or state word, and regexes. Meaning-level
  criteria get **one** bounded judge call (coder-30b, temperature 0, JSON verdict), shown
  as *model-judged* in the report. A judge outage marks that check "–" rather than failing
  the agent.
- **First dry run (2026-11-12): 6/9.** The two genuine failures are the useful part. "Anything
  last night?" got `recent_events` noise ("4:00–4:10 AM: multiple car detections") instead of the
  caption `timeline`. "Aircraft in the last hour?" mixed real recent flights with stale claims.
  The third failure was the harness's fault (truncated ground truth), fixed before scheduling.
  **Fixes, same day:**
  - "Last night": the `recent_events` description and the camera skill had steered
    open questions there. Both now point at `timeline`, and unfiltered `recent_events` results
    carry a note redirecting to it.
  - Aircraft: the stream showed the radio-logs skill's `python3 - <<EOF` snippet **blocked by
    Hermes's approval guard** ("script execution via heredoc", with no user present to approve).
    The agent fell back to `tail` and guessed.
- **Rule for the harness: rules over the judge for anything countable.** coder-30b as
  judge mis-compared a ~60-callsign set and miscounted a time window, so the aircraft
  question uses `no_stale` instead: every callsign the answer names must be in the window.
  Live truth is fetched *after* the answer, widened by the run time. Every report keeps each
  tool call's input and output (⛔ marks calls the approval guard blocked), since the eval
  deletes its sessions.
- **Rule for skills: ship scripts as files, never inline.** The approval guard
  (`tools/approval_detection.py`) flags `python -c`/`-e` and `python <<` in cron and `-q`
  runs, and asks for approval in Telegram, but leaves `python3 <file>` alone. Skills now ship
  helpers under `skills/<cat>/<skill>/scripts/`, and the sandbox mounts
  `data/skills` read-only at `/opt/data/skills` (`terminal.docker_volumes`). The first one is
  `radio-logs/scripts/adsb_summary.py N` (last N minutes, midnight-safe).
- **Questions cover:** camera history (tags, captions, named days, an honest "no"),
  role knowledge (vision worker vs Frigate), and routing (garage state → HA,
  aircraft → radio logs, never the cameras). Each has a `why` saying what a failure means.
  Add one when a real conversation goes wrong: a failing question is the regression test.
- **Output:** `reports/hermes-eval/<date>.html` + `.json`, `latest.html` and
  `history.jsonl` (trend), served at
  `https://ladybird.tail0e6756.ts.net:9123/hermes-eval/latest.html`. The 2–4 line
  stdout summary (score, delta vs the last run, each failure's reason, link) is the
  cron delivery to the home channel.

Schedule (`--no-agent` = the script *is* the job, so no cron model swap):

```bash
docker exec hermes hermes cron create "15 3 * * *" --script hermes_eval.py \
  --no-agent --name hermes-eval --deliver telegram --timezone America/Denver
# one-off / subset:  docker exec -u 1000 -e EVAL_ONLY=garage_state hermes python3 /opt/data/scripts/hermes_eval.py
```

Live since 2026-11-12 as job `84b75d43c47d`. 03:15 is quiet: ~9 questions × 1–5 min each fits well inside the 1 h cron script timeout
(`HERMES_CRON_SCRIPT_TIMEOUT`).

## Tool routing — the homelab map

Skills only load when a request matches their description, and the model
describes itself from its *loaded tool list* — which is mostly the ~66 Home
Assistant service domains. Left alone, Hermes calls itself a Home Assistant
assistant and routes everything there (2026-11-09: "who flew over the house?"
went to the vision archive; "what can you access?" listed only HA).

`stacks/hermes/SOUL.md` is the fix: a short identity + routing table (question →
source, with "not this" columns, and an explicit "cannot reach" list) that is
in the system prompt on every turn. The whole file is the prompt — no header
comments, every line costs context on every turn. **Keep the table in step
with reality** — add a row when a new data source or MCP server lands (and its
skill's description), drop rows for things that are removed.

**Git is the source of truth, and the owner is the only author.** The converge
copies `stacks/hermes/SOUL.md` → `data/SOUL.md` and `skills/homelab/` →
`data/skills/homelab/` (stacks role), so a change is PR → converge, then
`docker compose restart gateway` if it doesn't pick up on the next message.
A live edit on the box — by hand or by the agent — is overwritten on the next
converge and shows up in the `--check --diff` plan first, which doubles as the
"did the agent touch its own prompt?" check.

The agent does not get to change SOUL.md, in git or live, on purpose: it is
the text that tells the agent what it may not do, and the agent reads
untrusted input (mesh chat from strangers, camera captions, news). A
self-editable prompt turns one injected message into a permanent one. What the
agent learns belongs in its own memory under `data/`; if it thinks the map is
wrong it should say so in chat and the owner makes the PR.

If routing regresses, in order: fix the table row, sharpen the skill
description (user-phrased triggers + a "not this tool" line), sharpen the MCP
server's tool descriptions. Don't grow the prompt.

## Related

- `snibbly/etc/llama-swap/config.yaml` — the `coder-30b` and `big-30b-thinking`
  entries (both 64k, full MoE offload, `--jinja`) and the `8b-abliterated` Lab
  model.
- `snibbly/bench/` — tool-calling validation (`toolcall_test.py`) and the bench
  that proved the box stable enough for standing agent duty.
- `docs/langfuse.md` — where the agent's every model call is traced.
