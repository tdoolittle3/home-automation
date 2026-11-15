# Devbox — coding agents on the server, managed from the phone

One container on ladybird that runs software development so no generated code ever touches the
host. [Herdr](https://github.com/herdrdev/herdr) owns the agent terminals, long-lived, surviving
every disconnect; [Pi](https://github.com/earendil-works/pi) is the coding agent that runs inside
those terminals against snibbly's LLMs; [Collie](https://github.com/AltanS/collie) is the
self-hosted PWA that puts the whole thing on the phone over Tailscale. Hermes is untouched — it
keeps Telegram and its ephemeral sandboxes; the devbox is the *persistent* workbench
([architecture note below](#how-this-relates-to-hermes)).

| | |
|---|---|
| Container | `devbox` — `stacks/devbox/`, image built on the box |
| Terminals | herdr server (pinned release binary) — the process the container lives and dies with |
| Agent | pi (`@earendil-works/pi-coding-agent`, pinned) → `coder-30b` on snibbly via litellm `:8080` |
| Phone UI | collie (pinned) on `127.0.0.1:8787` (host loopback), fronted by `tailscale serve --https=8787` |
| Repos | `devbox_workspace` named volume → `/workspace` |
| State | `devbox_home` named volume → `/home/dev` (pi config, herdr/collie state, git config) |
| Secrets | `stacks/devbox/.env` (gitignored, template in `.env.example`, real copy in Vaultwarden) |

## The hard rules

- **No docker socket.** Socket access is root on ladybird. This container *is* the sandbox; it does
  not get to make more. If a task ever truly needs container-level isolation, that's a job for
  Hermes' sandbox, not a reason to mount the socket here.
- **No host bind mounts.** The two named volumes are the only storage. Agents cannot see `$HOME`,
  `/opt/stacks`, or any other service's data.
- **Loopback + `tailscale serve` only. Never `tailscale funnel`.** Collie's port is bound to
  `127.0.0.1` on the host; only the tailnet reaches it, with Tailscale's own identity in front.
- **Non-root.** Everything runs as `dev` (uid 1000).
- **Capped.** 2 CPUs, 3 GB — ladybird has 16 GB shared with Frigate, Immich, HA and Hermes. Raise
  deliberately, not because an `npm install` felt slow once.

## First run

Converge syncs `stacks/devbox/` to `/opt/stacks/devbox/` but deliberately does **not** build or
start it (same reasoning as hermes — local image, secrets-first, and recreating the container kills
live sessions). On the box:

```bash
cd /opt/stacks/devbox
cp .env.example .env && nano .env        # fill in; then add the file to Vaultwarden
docker compose build                     # pulls pinned herdr/collie/pi versions
docker compose up -d
docker compose exec devbox check.sh      # all five checks must pass
```

Then the tailnet front, once (survives reboots like the other serve entries):

```bash
tailscale serve --bg --https=8787 http://127.0.0.1:8787
tailscale serve status    # expect :8787 -> http://127.0.0.1:8787
```

Phone: open `https://ladybird.tail0e6756.ts.net:8787`, **add to home screen** (iOS only delivers
web push to home-screen PWAs), and enable notifications in collie's settings — it generates its
VAPID keys on first use and keeps them on the `devbox_home` volume. If the URL doesn't load from a
phone on the tailnet, it is almost always the serve entry, not the container: `tailscale serve
status` on ladybird, then the healthcheck (`docker ps` shows `(healthy)`).

**Two upstream details to eyeball on the very first boot** (both chosen from docs, neither
verified against a live install yet — `docker logs devbox` tells you immediately):

- `collie start` is expected to stay in the foreground. If the log shows collie exiting and
  rebinding every 5 s, it daemonized — switch the entrypoint loop to its internal foreground
  server, `collie _exec-bridge` (what its own systemd units run).
- pi's provider file is seeded at `~/.pi/agent/models.json`. Open a pane, run `pi`, then `/model` —
  `snibbly/coder-30b` should be listed. If not, check where this pi version wants `models.json`
  (`pi --help`, `PI_CODING_AGENT_DIR`).

## Daily use

From the phone: collie shows every herdr session and each agent's state (working / needs input /
done). Open a session, type at pi, detach — the pane keeps running.

From a desktop on the tailnet, the same sessions over SSH:

```bash
ssh thomas@192.168.0.13 -t docker exec -it devbox herdr
```

A typical ask, inside a pane: `pi`, then *"clone tdoolittle3/home-dashboard into /workspace,
install dependencies, run the tests."* Pi has `gh` and git credentials already wired (entrypoint
runs `gh auth setup-git` each boot). **Prefer a fresh pi session per task** — small local models
anchor hard on whatever an old session did before (the exact failure mode the radiolog Telegram
sessions hit), and fresh sessions route noticeably better.

Snibbly serves one request at a time, so parallel panes queue behind each other. That's fine for
small-scope work. If the box ever serves concurrent slots (`llama.cpp --parallel N` — mind the KV
cache split in 8 GB VRAM), the parallel-agents pattern is git worktrees inside `/workspace`, one
pane each.

Every pi conversation goes through litellm, so **Langfuse on snibbly captures all of it**
([langfuse.md](langfuse.md)) — free observability for judging how the agent setup is doing.

## Rebuilding without losing state

Image changes (version bumps, new baked tools):

```bash
cd /opt/stacks/devbox && docker compose build && docker compose up -d
```

Both volumes survive. What does *not* survive is **live sessions** — the panes are processes, so
recreate the container only between tasks. Unpushed work in `/workspace` survives restarts but has
no other copy anywhere; the standing instruction to agents is commit and push early. Tools agents
install ad hoc (`apt`, `npm -g`) die with the image — anything recurring gets a pinned line in the
Dockerfile instead.

Keep pi's accumulating config (skills, instructions, `models.json` edits) in a small git repo in
`/workspace`, symlinked into `~/.pi/` — versioned and pushable beats loose files on a volume.

## Rotating GH_TOKEN

Mint the new fine-grained PAT (Contents read/write on exactly the chosen repos), then:

```bash
cd /opt/stacks/devbox && nano .env       # swap GH_TOKEN
docker compose up -d                     # recreate — do it between tasks
docker compose exec devbox check.sh      # gh + ls-remote checks confirm
```

Update the Vaultwarden copy of `.env` in the same breath, then revoke the old token on GitHub.
`gh` reads the token from the environment only — nothing on the volumes caches it.

## Adding Claude Code or Codex later

One pinned install line in the Dockerfile (the commented block is waiting), rebuild, then a
one-time login inside the container:

```bash
docker compose exec devbox claude login    # or: codex login
```

Their auth lands in `/home/dev`, so it survives rebuilds like everything else there.

## How this relates to Hermes

Two isolation models, both deliberate. Hermes lives *outside* the code it runs — its orchestrator
spawns an ephemeral sandbox container per task and throws it away. The devbox inverts that: the
agent lives *inside* a persistent container, and the container is the boundary. Hermes is
ops/chat/home-awareness over Telegram; the devbox is the persistent development workbench with
repos, long-lived sessions and a phone UI. Neither should grow the other's privileges.

## Risks, accepted and otherwise

- **LAN egress is open.** The devbox sits on its own bridge network (no shared docker network, no
  resolving other containers by name), but NAT still reaches the LAN — an agent could curl HA or
  Frigate on `192.168.0.13`. Accepted for now: tasks are human-driven and watched through collie,
  and no HA token ever goes into this container. If agents ever run more autonomously, the fix is
  a `DOCKER-USER` iptables chain allowing `192.168.0.19:8080`, DNS and WAN, dropping the rest of
  the LAN — an ansible-managed rule, its own PR.
- **One container sees every repo in `/workspace`** and one PAT covers them all. Accepted for a
  home dev box; the fine-grained token is the blast-radius cap on the GitHub side.
- **No Kuma monitor.** Collie binds host loopback only, which is exactly what makes it
  tailnet-safe — and also unreachable from the Kuma container (same trade the hermes dashboard
  made). The compose healthcheck covers the container side; a dead *serve* front shows up as the
  phone URL failing, fix in [First run](#first-run).
- **`/workspace` is the only copy of unpushed work.** No off-box backup. Push early.
