# Upgrades

How every layer of both machines gets upgraded: what triggers it, what to do before merging, how
to check it, and how to roll it back. The short version: **container images upgrade through
Renovate PRs and the normal deploy; the OS and hand-built parts upgrade in a monthly patch
window.**

---

## Where versions live

| Layer | Pinned in | Who proposes a bump |
|---|---|---|
| Every registry image (both hosts) | its `docker-compose.yml`, as `tag@sha256:digest` | Renovate, first of the month |
| Hand-installed snibbly parts (llama-swap, litellm, langfuse SDK, vision venv) | [VERSIONS.txt](../VERSIONS.txt) | Renovate (regex manager) — the PR is a reminder + record, merging installs nothing |
| Locally built images (hermes-agent, home-dashboard) | the source ref in [VERSIONS.txt](../VERSIONS.txt) | you (hermes: Renovate once the ref is a release tag) |
| radiolog, mesh-cli, devbox | their source in this repo | normal PRs |
| OS, kernel, Docker, Tailscale, NVIDIA | the hosts' apt state, recorded in [VERSIONS.txt](../VERSIONS.txt) | unattended-upgrades (security) + the patch window |

The digest is what is actually pulled; the tag is there so humans and Renovate can read the
version. A rebuild therefore gets exactly what was running, not whatever `latest` means that day.

## The container upgrade flow

1. **Renovate opens PRs** on the 1st of the month (security fixes can come any time). The
   "Dependency Dashboard" issue (repo Issues tab) lists everything pending, and lets you
   request a PR early. Immich (server + ML) and Langfuse (web + worker) come as one
   PR each; all digest-only refreshes (the `main`/`master`/`latest` images, upstream rebuilds of a
   pinned tag) come as one monthly PR.
2. **Read the PR's notes.** Risky services carry a "Before merging" note — do that step first
   (table below). Read the upstream release notes for anything flagged `major`.
3. **Merge → approve the deploy.** Converge's `docker compose up -d` sees the new image string,
   pulls, and recreates that one service.
4. **Stacks outside `stacks_auto_up`** (`dns`, `sdr`, `hermes`) only get the new compose file —
   run `docker compose up -d` in `/opt/stacks/<stack>` yourself. The PR says so.
5. **Rollback** for anything without a data migration: revert the PR, approve the deploy — the
   old digest comes back. For the services marked *one-way* below, the revert alone is not enough;
   restore the backup taken in step 2.

Deliberately blocked in [renovate.json](../renovate.json): Uptime Kuma 2.x (needs
`kuma-provision.py` ported off the 1.x API — a project, not a merge), Postgres/Redis/ClickHouse
majors (dump + restore, not a tag bump), langfuse SDK 5.x (capture fails open), Immich's database
and redis images (they follow upstream's compose file), and the locally built images.

## Per-service notes

| Service | Before merging | After | One-way? |
|---|---|---|---|
| **Immich** | dump the database; diff upstream's compose for database/redis digest moves — [operations.md → Upgrading](operations.md#upgrading) | `docker logs -f immich_server` until migrations finish | **yes** — restore the dump |
| **Frigate** | `sudo tar czf ~/frigate-config-$(date +%F).tgz -C /opt/stacks/frigate config`; read the release notes for config changes | cameras + detections in the UI; then copy the (possibly migrated) live `config.yml` back into [the repo seed](../stacks/frigate/config/config.yml) by PR, or a rebuild gets an outdated seed | **yes** — config + db rewrite themselves |
| **Home Assistant** | Settings → System → Backups → create; check HACS Frigate + Dahua support the release | integrations page shows no errors; automations still fire | **yes** — restore the backup |
| **AdGuard (dns)** | read [dns.md](dns.md); pick a quiet hour | `docker compose up -d` in `/opt/stacks/dns`, then `nslookup doubleclick.net 192.168.0.13` → `0.0.0.0` | no |
| **Vaultwarden** | nothing — the nightly snapshot is the backup | log in from a client | **yes** — restore a snapshot ([vaultwarden.md](vaultwarden.md)) |
| **n8n** | copy `/opt/stacks/n8n/n8n-data/` | UI loads | **yes** — DB migrations; it runs no workflows, consider retiring it instead |
| **Uptime Kuma** | 1.x patches only (2.x blocked) | all monitors green | no |
| **Langfuse** | for big jumps, read the v3 changelog for migration notes | traces still arriving from litellm | **yes** — ClickHouse/Postgres migrations |
| everything else | — | container healthy, its Kuma monitor green | no |

## Hand-built components (snibbly + hermes)

Renovate's PRs against [VERSIONS.txt](../VERSIONS.txt) are the trigger; install first, then merge
the PR so the record matches the box.

- **llama.cpp** — side-directory build and atomic swap: [snibbly.md](snibbly.md) "Upgrading".
  CUDA must stay on 12.x with `CMAKE_CUDA_ARCHITECTURES="61;75"`: the GTX 1050 Ti is Pascal
  (sm_61), which CUDA 13 no longer targets.
- **llama-swap** — download the release binary next to the old one, swap
  `/opt/llama-swap/llama-swap`, `sudo systemctl restart llama-swap`, check `/running` on `:9292`.
- **litellm / langfuse SDK** — `sudo /opt/litellm/venv/bin/pip install 'litellm[proxy]==<v>' 'langfuse==<v>'`,
  restart `litellm`, then check `journalctl -u litellm` for Langfuse errors — capture fails open
  ([langfuse.md](langfuse.md)). Rollback = reinstall the old versions.
- **vision venv** — `uv pip install --python /opt/vision/venv/bin/python '<pkg>==<v>'`, restart
  the `vision-*` units, run the pytest suite ([vision.md](vision.md)).
- **hermes-agent** — check out the new ref in `~/src/hermes-agent`, `docker build -t hermes-agent:latest .`,
  `docker compose up -d` in `/opt/stacks/hermes`, then run the eval ([hermes.md](hermes.md)).
  Rollback = rebuild the previous ref. Its config schema has changed between versions before —
  read the changelog.
- **home-dashboard** — [operations.md → Dashboard](operations.md#dashboard); record the commit
  you built in VERSIONS.txt.

## The monthly patch window — OS, kernel, Docker, NVIDIA

unattended-upgrades installs Debian/Ubuntu security updates nightly on both hosts, **but nothing
reboots**, so kernel and libc fixes sit installed-but-inactive until someone does. And on
ladybird, Docker and Tailscale come from their own apt repos, which unattended-upgrades does not
touch. Hence one window a month (the weekend after Renovate's PRs is a good fit). Check whether
a host needs it with `ls /var/run/reboot-required`.

**snibbly first** (nothing house-critical runs there):

```bash
sudo apt update && sudo apt full-upgrade      # nvidia-driver-580 stays held
sudo reboot
nvidia-smi                                   # both cards, driver 580.x
dkms status                                  # nvidia module built for the new kernel
systemctl is-active llama-swap litellm vision-worker vision-captioner vision-mcp vision-web
```

then a completion through `:8080` ([snibbly.md](snibbly.md)) and a fresh trace in Langfuse.

**then ladybird** — pause the Kuma monitors first ([operations.md](operations.md)); the house
loses DNS for the minutes this takes:

```bash
sudo apt update && sudo apt full-upgrade      # includes docker-ce + tailscale: dockerd restarts
sudo reboot
docker ps --format '{{.Names}}\t{{.Status}}'  # everything Up
nslookup doubleclick.net 192.168.0.13        # 0.0.0.0 — house DNS back
vainfo | head -3                             # iGPU still there (Frigate, Jellyfin)
```

Resume the Kuma monitors, check camera live views, update the host lines in
[VERSIONS.txt](../VERSIONS.txt).

**The NVIDIA driver is held on purpose.** The 580 branch is the last one that supports Pascal,
so the GTX 1050 Ti keeps snibbly on 580.x for good. Take 580 point releases deliberately
(`apt-mark unhold`, install, re-hold, reboot, checks above); do not move to a newer branch while
the 1050 Ti is in the box. A new kernel that the 580 DKMS module can't build against shows up as
`nvidia-smi` failing after the reboot — boot the previous kernel from GRUB and wait for a driver
point release.

**OS release upgrades** are not on the horizon: Debian 13 (ladybird) has security support into
2028 and LTS beyond; Ubuntu 26.04 LTS (snibbly) is supported into 2031.

## One-time setup

Install the **Renovate** GitHub App (Mend, free) on `tdoolittle3/home-automation` only. Because
[renovate.json](../renovate.json) already exists, it skips the onboarding PR and opens the
Dependency Dashboard issue, then the first batch of PRs on the next schedule window. Renovate PRs
go through the same CI and branch protection as any other PR; nothing auto-merges.
