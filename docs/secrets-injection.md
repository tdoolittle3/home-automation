# Converge-time secrets injection (planning)

**Status:** planned, not started. This is the design for getting plaintext
secrets *off disk at rest* on the homelab. Companion to
[vaultwarden.md](vaultwarden.md) (the human/recovery vault, which stays).

## Goal and the one honest constraint

Today every stack reads a plaintext `.env` / `secrets.yaml` from disk at runtime
(~13 files across ladybird + snibbly — see the list below). Vaultwarden backs
them up, but the live files are cleartext on the boxes.

**The goal:** no decrypted secret persisted on disk — encrypted sources only,
decrypted into RAM at deploy/boot.

**The constraint that can't be wished away:** automating the decrypt needs *one*
credential, and it has to live somewhere the machine can read unattended. So the
honest end state is **not "zero secrets at rest"** — it's **"one small,
protected, rotatable bootstrap key instead of ~13 scattered plaintext files,"**
with the decrypted secrets living only in RAM. Vaultwarden can't close this gap
itself: it has no machine accounts (no Bitwarden Secrets Manager / `bws`), its
only programmatic access is `bw` unlocked by the master password, and it *runs on
ladybird* — so a reboot would need secrets before the vault is even up. One
bootstrap key is the floor.

## Chosen design — SOPS + age + tmpfs

1. **Encrypt each secret file** with [`sops`](https://github.com/getsops/sops)
   using an [`age`](https://github.com/FiloSottile/age) key:
   `stacks/dash/.env` → `stacks/dash/.env.sops`, etc. The *encrypted* files are
   safe to commit to the repo — source-of-truth is versioned and reproducible,
   and plaintext never lives in git or on disk at rest.
2. **At converge**, an ansible role runs `sops -d` and writes each decrypted
   `.env` into a **tmpfs** dir (RAM-only, e.g. `/run/secrets/<stack>/.env`);
   each stack's compose `env_file:` points there. tmpfs = the decrypted secret
   never touches disk and is gone on reboot.
3. **Boot survives reboots**: a systemd oneshot (`Before=docker.service`,
   `RequiresMountsFor=/run/secrets`) re-decrypts into tmpfs on boot, so services
   come up after a power loss without waiting for a manual converge.
4. **The one bootstrap secret** = the age private key, stored in two places:
   - in Vaultwarden (your recovery copy / rebuild source), and
   - on each box at `/root/.age/keys.txt` (mode 600) so converge and the boot
     unit can decrypt non-interactively.

```
 repo:  stacks/<x>/.env.sops   (encrypted, committed)
            │  sops -d  (uses /root/.age/keys.txt)
            ▼
 box:   /run/secrets/<x>/.env  (tmpfs — RAM only, never on disk)
            │  compose env_file:
            ▼
 container reads it at start
```

**Result:** encrypted sources in git + decrypted secrets only in RAM + one age
key per box (also vaulted). Vaultwarden stays as the human vault *and* holds the
age key. This is the strongest posture realistically achievable here.

## Why not the alternatives
- **`bw` at converge** (fetch from Vaultwarden during deploy — the old Phase B):
  works, but the master-password bootstrap plus vault-runs-on-ladybird boot-order
  make it fragile, and the `.env` still sits on disk between converges. SOPS
  decouples deploy from the vault being up.
- **External backend** (Infisical / OpenBao) with real machine tokens: the
  "correct" tool for true zero-at-rest, but a whole new service to run, and
  paid/cloud was ruled out. Overkill for a homelab.
- The existing **`vault-render.sh`** (renders files *from* Vaultwarden) stays as
  the disaster-recovery path (rebuild a file by hand from the vault); SOPS is the
  everyday machine-injection layer. They don't conflict.

## Files in scope (~13)
ladybird: `dash/.env`, `frigate/.env`, `immich/.env`, `sdr/.env`,
`home/homeassistant/secrets.yaml`, `hermes/data/.env`, `n8n/n8n-data/config`,
`~/portfolio-advisor/.env`, and the 4 `net/kuma-*push-url*.txt`.
snibbly: `/etc/llama/llama.env`, `/opt/stacks/langfuse/.env`.
Out of scope: `vaultwarden/.env` (no secret; the vault must not bootstrap from
itself) and the age key itself.

## Phased plan
1. **Key + tooling.** Generate an age keypair; put the private key in Vaultwarden
   and at `/root/.age/keys.txt` (600) on ladybird + snibbly; install `sops` +
   `age` (ansible base packages). Add `.sops.yaml` (recipient = the age pubkey).
2. **Encrypt one stack end-to-end** (pick a low-blast one, e.g. `sdr`): commit
   `.env.sops`, write the decrypt-to-tmpfs ansible task, repoint its compose
   `env_file`, converge, confirm the container is healthy reading from
   `/run/secrets/sdr/.env`. This proves the whole path before touching the rest.
3. **Boot unit.** systemd oneshot that decrypts all `.sops` → tmpfs on boot,
   ordered before docker. Test with a real reboot.
4. **Migrate the rest** stack by stack (low-blast first; Hermes/HA/snibbly last).
   Delete each plaintext `.env` from disk only after its `.sops` + tmpfs path is
   verified.
5. **Rotation workflow.** `sops` edit (re-encrypts in place) → commit → converge.
   Document age-key rotation (`sops updatekeys`) too.
6. **Validate.** For each stack: delete the decrypted `/run/secrets/.../.env` →
   converge (or reboot) → container healthy. Update [known-gaps.md](known-gaps.md).

## Risks / gotchas to respect
- **Boot-order is the sharp edge.** If the decrypt oneshot doesn't run before
  docker (or tmpfs isn't mounted), services start with no secrets after a reboot.
  Stage it carefully; keep a plaintext fallback until a real reboot test passes.
- **The age key is the crown jewel.** One key decrypts everything — protect it
  (600, root-only), vault it, and have a rotation plan. Losing it = re-seed from
  Vaultwarden; leaking it = rotate every secret.
- **snibbly reachability.** The control node (WSL) reaches the boxes over
  Tailscale; the boot-time decrypt is local (uses the on-box age key), so it
  doesn't depend on the control node or the tailnet.
- Keep this its **own effort** — it touches every stack; don't bolt it onto
  feature work.

## End state (what this does and doesn't buy)
- ✅ No plaintext secret persisted on disk (encrypted `.sops` + RAM-only decrypt).
- ✅ ~13 scattered secrets collapse to one age key; sources versioned + reproducible.
- ✅ Survives reboots unattended; no dependency on the vault being up.
- ❌ Not literally zero secrets at rest — the age key is the irreducible
  bootstrap, by design.
