# Vaultwarden — the password vault

`stacks/vaultwarden/` runs [Vaultwarden](https://github.com/dani-garcia/vaultwarden), a
self-hosted server speaking the Bitwarden protocol. It is where the lab's secrets live —
every password, token and push URL that [rebuild.md](rebuild.md#secrets--not-in-this-repo)
says must be recreated by hand now has exactly one durable home. The clients are the
official Bitwarden apps (desktop, mobile, browser extension, `bw` CLI) pointed at this
server instead of bitwarden.com.

Two properties do the heavy lifting:

- **The server never sees a usable secret.** Vault items are encrypted client-side with a
  key derived from the master password; the SQLite database, the backups of it, and anyone
  reading them hold ciphertext. That is why the backup tarballs can sit on a disk without
  ceremony.
- **Clients keep an offline encrypted cache.** When ladybird is down, every enrolled device
  can still *unlock and read* the vault — it just cannot sync changes until the server
  returns. The secret needed to fix the dead box is never locked inside the dead box.

The corollary, and the single most important line in this document: **the master password
has no reset.** Not by the admin panel, not by anyone. Write it on paper when the account
is created and store the paper like a passport. Losing it loses the vault; the backups are
ciphertext without it.

## The shape of it

| | |
|---|---|
| Container | `vaultwarden`, port `8222` (plain http) on the LAN |
| Real door | `https://ladybird.tail0e6756.ts.net:8443` — `tailscale serve` proxying to `127.0.0.1:8222` |
| Data | `/opt/stacks/vaultwarden/vw-data/` — gitignored, this *is* the vault |
| Backup | `vaultwarden-backup.timer`, nightly 03:30 → `/srv/storage/backups/vaultwarden/` |
| Monitors | `Vaultwarden` (http `/alive`) and `Vaultwarden backup` (push) — [kuma-monitors.yml](../stacks/net/kuma-monitors.yml) |

Bitwarden clients require HTTPS, so the tailnet front is not a convenience — it is the only
address that works. Port `8222` exists as the serve proxy target and for the Kuma check.
**Never log in over it**: the login request carries the master-password hash, and on plain
http that crosses the LAN in the clear. Browsing to `:8222` shows the web vault but the
crypto APIs refuse to run outside a secure context, which is the reminder.

Devices therefore need Tailscale (with MagicDNS) to reach the vault — same posture as
everything else here: nothing port-forwarded, outside access through the tailnet only.

## First run

The converge deploys the stack, the backup units and the `sqlite3` dependency
([ansible.md](ansible.md)); what follows is one-time by hand, same spirit as the Kuma and
AdGuard first runs.

1. **Front it with HTTPS** (on ladybird; serve state survives reboots):

   ```bash
   tailscale serve --bg --https=8443 http://127.0.0.1:8222
   tailscale serve status        # expect :8443 -> http://127.0.0.1:8222
   ```

2. **Open signups for yourself only.** In `/opt/stacks/vaultwarden/`:

   ```bash
   cp .env.example .env && chmod 600 .env
   # set SIGNUPS_ALLOWED=true in .env, then:
   docker compose up -d
   ```

3. **Create the account** at `https://ladybird.tail0e6756.ts.net:8443` from a tailnet
   device. Pick a long master password you can type on a phone. **Write it on paper now.**

4. **Close signups.** Set `SIGNUPS_ALLOWED=false` in `.env`, `docker compose up -d` again.
   Confirm the register page refuses.

5. **Turn on 2FA** (web vault → Settings → Security → Two-step login → Authenticator app).
   Store the recovery code on the same paper as the master password.

6. **Wire the monitors.** Re-run `kuma-provision.py` for the two new monitors
   ([uptime-kuma.md](uptime-kuma.md)), then put the push monitor's URL in
   `/opt/stacks/net/kuma-vw-backup-push-url.txt`, mode 600 — same drill as the storage
   guard's.

7. **Enroll the devices**: install the Bitwarden app/extension, choose *self-hosted* and
   set the server URL to `https://ladybird.tail0e6756.ts.net:8443`, log in.

The `/admin` panel stays disabled (no `ADMIN_TOKEN` set). It can reset any account's 2FA
and list every user, and nothing in normal operation needs it — enable it in `.env` for a
task, then disable it again.

## Backups

[vaultwarden-backup.sh](../stacks/net/vaultwarden-backup.sh) runs nightly as root:

- `sqlite3 .backup` takes a **consistent online snapshot** of the database — a plain `cp`
  of a live SQLite file is the classic corrupt backup.
- Everything else in `vw-data/` (attachments, sends, the `rsa_key*` that signs client
  sessions) goes into the same timestamped `vaultwarden-*.tar.gz`.
- Local retention 30 days under `/srv/storage/backups/vaultwarden/`, mode 700.
- The Kuma push monitor is beaten **only on full success** — a broken backup is silence,
  and silence pages.

Settings live in [vaultwarden-backup.conf](../stacks/net/vaultwarden-backup.conf); the
timer re-reads it every run.

**The off-box leg** is a conf value away and starts unwired
([known-gaps.md](known-gaps.md#backups)). To send snapshots to another machine (snibbly,
say), set up a key as root on ladybird once:

```bash
sudo ssh-keygen -t ed25519 -f /root/.ssh/id_ed25519 -N ''
sudo cat /root/.ssh/id_ed25519.pub   # append to ~/.ssh/authorized_keys on the target
sudo ssh thomas@192.168.0.19 true    # accept the host key once
```

then set `REMOTE_DEST=thomas@192.168.0.19:backups/vaultwarden/` in the conf. The rsync
uses `--ignore-existing` and never deletes: local pruning does not propagate, and a
compromised ladybird cannot rewrite or empty the remote copies.

**Belt and suspenders:** every month or so, desktop app → Export vault →
**.json (encrypted)**, stored anywhere. It is useless without the master password and
restores into any Bitwarden server, including a fresh one.

### Restore drill

On a rebuilt box, after the converge has placed the stack:

```bash
cd /opt/stacks/vaultwarden && docker compose down
mkdir -p vw-data && cd vw-data
tar -xzf /srv/storage/backups/vaultwarden/vaultwarden-<stamp>.tar.gz
tar -xf data.tar && rm data.tar          # attachments, keys, config.json
# db.sqlite3 from the tarball is already the snapshot - leave it in place
cd .. && docker compose up -d
```

Re-run the `tailscale serve` line from First run, log a client in, confirm the entries are
there. Do this once *before* it matters — an untested backup is a hope, not a backup.

## Seeding the vault

Move in everything [rebuild.md](rebuild.md#secrets--not-in-this-repo) lists as
hand-recreated, plus the other scattered credentials:

- the repo `.env` values: `SSH_PW`, `CAMERA_PW`, `NTFY_TOPIC`
- camera admin password (lives only on the cameras and in `.env` today)
- Uptime Kuma admin login; AdGuard admin login; Langfuse login (snibbly)
- HA long-lived token + Jellyfin API key (`/opt/stacks/dash/.env`)
- Immich `DB_PASSWORD`; snibbly's llama/LiteLLM API key
- Kuma push URLs and the healthchecks URL (they authenticate the beat)
- GitHub PATs, the Tailscale CI auth key's renewal note (~2027-01-26)
- this box's sudo passwords, router admin, and the vault's own 2FA recovery code — on
  paper too

**Rotate as you migrate.** Each of these has been sitting in a plaintext file; adding it
to the vault is the natural moment to mint a new value and update the consumer. The vault
entry's *name* can be the path it feeds (e.g. `/opt/stacks/dash/.env HA_TOKEN`), so the
`.env` files on disk become plumbing that the vault can always regenerate.

The `.env` files themselves stay — services read environment files, not vaults, and that
is deliberate: no running service depends on Vaultwarden being up. The vault is the
durable source of truth; the files are disposable copies, mode 600.

## Gotchas

- **`DOMAIN` must match the serve URL exactly**, port included — it is baked into client
  registrations and WebAuthn. Changing it later means re-logging-in every client. It is
  set in [docker-compose.yml](../stacks/vaultwarden/docker-compose.yml), not `.env`,
  precisely because it should never drift casually.
- **The Kuma check watches the container, not the front.** If `tailscale serve` state is
  lost, Kuma stays green while every client fails to sync. The symptom "all devices say
  cannot reach server, Kuma says Vaultwarden is up" means run
  `tailscale serve status` on ladybird and re-apply the First-run serve line.
- **`vw-data/` is root-owned** (the container runs as root). Inspect it with `sudo`;
  never loosen its mode — it is the vault.
- **Signups stay closed.** Anyone who can register on the tailnet gets a vault on your
  hardware. One account exists; opening signups is a deliberate two-step via `.env`.
