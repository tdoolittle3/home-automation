#!/bin/bash
# Regenerate this host's secret files from the Vaultwarden vault
# (docs/vaultwarden.md -> "Seeding the vault"). The vault holds one Secure
# Note per file, named "<host>:<absolute path>"; this writes each note's body
# back to its path, so the files on disk are disposable copies the vault can
# always recreate. Run as root with an unlocked bw session:
#
#   sudo -i
#   export BW_SESSION=$(bw unlock --raw)
#   /opt/stacks/net/vault-render.sh            # every <host>:* note
#   /opt/stacks/net/vault-render.sh /opt/stacks/dash/.env   # just one
#
# Compose only reads .env at `docker compose up -d`, so restart the stacks
# whose files changed - the script lists them. Deliberately no restart here:
# rendering must stay safe to run while everything is up.
set -euo pipefail

HOST=$(hostname)

command -v bw >/dev/null \
  || { echo "bw CLI missing - install per docs/vaultwarden.md" >&2; exit 1; }
[ -n "${BW_SESSION:-}" ] \
  || { echo 'BW_SESSION unset - run: export BW_SESSION=$(bw unlock --raw)' >&2; exit 1; }

bw sync >/dev/null

# Captured to a variable, not piped: the heredoc below owns python's stdin
# (piping bw into `python3 -` would be silently discarded), and a failed bw
# call dies here under set -e instead of surfacing as a JSON traceback.
items_json=$(bw list items --search "$HOST:")

HOST="$HOST" ITEMS_JSON="$items_json" python3 - "$@" <<'PY'
import json, os, sys

host = os.environ["HOST"]
items = json.loads(os.environ["ITEMS_JSON"])
only = {os.path.abspath(a) for a in sys.argv[1:]}
changed, seen = [], set()

for it in items:
    name = it.get("name", "")
    if it.get("type") != 2 or not name.startswith(host + ":/"):
        continue
    path = name.split(":", 1)[1]
    seen.add(path)
    if only and path not in only:
        continue
    body = it.get("notes") or ""
    if not body.endswith("\n"):
        body += "\n"

    current = None
    if os.path.exists(path):
        with open(path) as f:
            current = f.read()
    if current == body:
        print(f"ok       {path}")
        continue

    stat = os.stat(path) if current is not None else None
    tmp = path + ".vault-new"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(body)
    if stat:
        os.chown(tmp, stat.st_uid, stat.st_gid)
    os.replace(tmp, path)
    print(("updated  " if current is not None else "created  ") + path)
    changed.append(path)

missing = only - seen
for path in sorted(missing):
    print(f"no vault note named {host}:{path}", file=sys.stderr)

stacks = sorted({p.split("/")[3] for p in changed
                 if p.startswith("/opt/stacks/")})
if stacks:
    print("\nchanged files load on restart:")
    for s in stacks:
        print(f"  cd /opt/stacks/{s} && docker compose up -d")
sys.exit(1 if missing else 0)
PY
