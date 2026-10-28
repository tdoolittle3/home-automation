# Ansible

The executable half of what [rebuild.md](rebuild.md) used to spell out as shell blocks. The
playbooks under [../ansible/](../ansible/) are the source of truth for everything they cover;
rebuild.md keeps the ordering, the verifications, and the steps that are genuinely manual.

Ansible is agentless: nothing runs on ladybird or snibbly except during a play — the managed
hosts need only SSH and Python, which they already have. All state is this repo.

---

## Control node

Anywhere with SSH access to the boxes. Ansible cannot run **from** Windows, so on the desktop
that means WSL; ladybird itself also works in a pinch (clone the repo there — but remember that
if ladybird is the only control node, the rebuild tool lives on the box being rebuilt).

```bash
# in WSL (or any Linux box on the LAN/tailnet)
pipx install --include-deps ansible      # or: pip install ansible in a venv
cd ansible
ansible ladybird -m ping                 # SSH + Python reachable?
```

SSH key auth to `thomas@192.168.0.13` / `thomas@192.168.0.19` is assumed (ladybird already uses
it). Sudo passwords are never stored: pass `-K` (`--ask-become-pass`) on every run.

## Running it

```bash
cd ansible
ansible-playbook site.yml -K --check --diff    # drift report — changes NOTHING, read it first
ansible-playbook site.yml -K                   # converge both hosts
ansible-playbook ladybird.yml -K -t stacks     # just sync + compose up ladybird's stacks
ansible-playbook snibbly.yml -K -t etc         # just snibbly's /etc files
```

**Always run `--check --diff` before a real run on a box that may have drifted** — the copy
tasks overwrite tracked files, so an uncommitted live edit (the historical failure mode here)
loses silently. The check run is also the answer to "does what's running match git?".

Tags mirror the role names: `base`, `docker`, `storage`, `host_files`, `stacks`, `nut`,
`guards`, `samba`, `tailscale` on ladybird; `etc`, `agent_runner`, `stacks` on snibbly.

## What it covers, what it doesn't

| Managed by ansible | Still manual (and why) |
|---|---|
| ladybird: apt sources + base packages, docker + daemon.json, directory layout, camera-island interface + chrony allow, sysctl/grub/rasdaemon crash forensics, NUT (password generated on-host, once), guard + playlist systemd timers, samba share, tailscale install, stack sync + `compose up` for the safe set | **Secrets** — every `.env`, push URL and token in rebuild.md's table; machine-local by design, ansible never reads or writes them |
| snibbly: /etc configs (litellm, llama-swap, vision, sysctl), service units, litellm/llama-swap enabled, agent runner, langfuse stack | **DNS stack cutover** — can take the whole house offline; [dns.md](dns.md) is the runbook |
| | **SDR host prep** — one-time hardware setup, stays [host/scripts/sdr-host-prep.sh](../host/scripts/sdr-host-prep.sh) + a real lat/long `.env` ([sdr.md](sdr.md)) |
| | **dash image build** — built from the home-dashboard clone ([operations.md](operations.md#dashboard)); compose file syncs, build doesn't |
| | **Interactive onboarding** — HA, Immich first account, Frigate first-boot password, n8n owner + credential re-entry, Kuma admin + [kuma-provision.py](../stacks/net/kuma-provision.py), `tailscale up`, `smbpasswd -a`, camera provisioning, BIOS |
| | **snibbly base** — NVIDIA driver, llama.cpp CUDA build, llama-swap binary, GGUF downloads, vision pipeline ([snibbly.md](snibbly.md), [vision.md](vision.md)) |

Behavior worth knowing before the first run on the live boxes:

- **Copy never deletes.** Gitignored live files (`.env`, `kuma-push-url*.txt`, `n8n-data/`,
  HA's `.storage/`, Kuma's database) survive every sync.
- **Seeds stay seeds.** Frigate's `config.yml` and NUT's credential files deploy with
  `force: false` — placed on a fresh box, never touched again, because the live copies
  rewrite themselves (Frigate) or hold a machine-local secret (NUT).
- **Restarts are change-triggered only.** A `daemon.json` change restarts dockerd and with it
  every container including house DNS; a litellm config change blips the LLM door. Unchanged
  files fire nothing, and `docker compose up -d` is a no-op for unchanged services.
- **vision-worker** gets its config synced but its run-state is not managed — starting the
  sweep is an operator decision while [vision.md](vision.md) is still in flux.
