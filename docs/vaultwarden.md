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

**The off-box leg** sends every snapshot to snibbly —
`REMOTE_DEST=thomas@192.168.0.19:backups/vaultwarden/` in the conf, landing under
`~thomas/backups/vaultwarden/` (wired and verified 2026-11-07). The rsync uses
`--ignore-existing` and never deletes: local pruning does not propagate, and a
compromised ladybird cannot rewrite or empty the remote copies. Nothing prunes the
snibbly side; at ~10 KB a night that is years of headroom before it matters.

The key setup behind it is one-time, done as root on ladybird, and must be repeated on a
rebuild of either box (the private key and the target's `authorized_keys` are not in this
repo):

```bash
sudo ssh-keygen -t ed25519 -f /root/.ssh/id_ed25519 -N ''
sudo cat /root/.ssh/id_ed25519.pub   # append to thomas@snibbly's ~/.ssh/authorized_keys
sudo ssh thomas@192.168.0.19 true    # accept the host key once
```

`rsync` must exist on both ends — the base role installs it on ladybird (it is not in
Debian's default install, which is how the first wired run failed).

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

The convention that makes everything below mechanical: **each secret-bearing file is one
Secure Note whose name is `<host>:<absolute path>`** — `ladybird:/opt/stacks/dash/.env`,
`snibbly:/etc/llama/llama.env` — and the note body *is* the file. Human-facing
credentials (Kuma admin, AdGuard, camera, router) are additionally proper login items.
Things that are not files on a box — GitHub PATs, router admin, the Tailscale CI key's
renewal note (~2027-01-26), the vault's own 2FA recovery code — are ordinary entries,
added by hand.

**One-time harvest:** [vault-seed-build.py](../stacks/net/vault-seed-build.py), run as
root on ladybird, gathers every known secret file (this box, snibbly's two over the
backup leg's ssh key, and the control node's repo `.env` if staged at
`/root/repo-dotenv`) into `/root/vault-seed.json` — an *unencrypted* Bitwarden import
file, mode 600. From a tailnet device: web vault → Tools → **Import data** → Bitwarden
(.json). Then destroy it:

```bash
sudo shred -u /root/vault-seed.json /root/repo-dotenv
```

**Rotate as you migrate.** Every one of these values has been sitting in plaintext;
entering the vault is the natural moment to mint a new value, update the vault entry,
and re-render (below). After rotation, the harvested originals are dead values and the
window where the seed file existed stops mattering.

## Rendering files from the vault

[vault-render.sh](../stacks/net/vault-render.sh) is the other direction: it rewrites this
host's secret files from their vault notes, which makes the on-disk files disposable
copies the vault can always regenerate. It needs the `bw` CLI once per box:

```bash
curl -fsSL -o /tmp/bw.zip 'https://vault.bitwarden.com/download/?app=cli&platform=linux'
sudo unzip -o /tmp/bw.zip -d /usr/local/bin && sudo chmod 755 /usr/local/bin/bw
sudo -i bw config server https://ladybird.tail0e6756.ts.net:8443
sudo -i bw login            # once; email + master password + TOTP
```

then per session:

```bash
sudo -i
export BW_SESSION=$(bw unlock --raw)
/opt/stacks/net/vault-render.sh              # or a specific path
```

**Rotation drill:** edit the note in any Bitwarden client → `vault-render.sh` → restart
the stacks it lists (compose reads `.env` only at `up`). **Restore drill, worth running
once:** delete a low-stakes file (`/opt/stacks/sdr/.env`, say), render, `docker compose
up -d`, watch the service come back healthy. A regeneration path that has never run is a
hope, not a path.

## Why the files stay

Removing the `.env` files entirely — "no plaintext on the box" — is a **non-goal**, for
two structural reasons. Vaultwarden does not implement Bitwarden's machine accounts
(`bws`), so any automated fetch authenticates with a `bw` credential that would itself
sit in plaintext on the same disk: the secret does not disappear, it just changes name.
And the vault runs *on* ladybird — making every container's startup depend on it
recreates exactly the boot-order fragility this design avoids (services must come up
after a power cut with no human and no vault). The endpoint is instead: vault as the
durable source of truth, every value rotated, and the files as mode-600 machine-written
artifacts that `vault-render.sh` recreates in seconds.

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
