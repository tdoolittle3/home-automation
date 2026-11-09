#!/usr/bin/env python3
"""Build a Bitwarden-import JSON seeding the homelab vault (one-time).

Run as root on ladybird. Reads the secret files already on this box (and
snibbly's two, over the existing root->thomas key), writes an UNENCRYPTED
Bitwarden import file to /root/vault-seed.json, mode 600. Import it from a
home-tailnet device: web vault -> Tools -> Import data -> Bitwarden (json),
then DELETE it: shred -u /root/vault-seed.json

Convention: each file becomes a Secure Note named "<host>:<absolute path>" in
folder "homelab files" - vault-render.sh regenerates files from those names.
Human-facing credentials additionally become proper login items.

No secret is printed to stdout.
"""
import glob
import json
import os
import subprocess
import sys
import uuid

OUT = "/root/vault-seed.json"

folders = [
    {"id": str(uuid.uuid4()), "name": "homelab files"},
    {"id": str(uuid.uuid4()), "name": "homelab logins"},
]
FILES_ID, LOGINS_ID = folders[0]["id"], folders[1]["id"]
items = []
skipped = []


def read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError as exc:
        skipped.append(f"{path}: {exc.strerror}")
        return None


def snibbly(cmd):
    r = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "thomas@192.168.0.19", cmd],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        skipped.append(f"snibbly {cmd!r}: {r.stderr.strip()[:120]}")
        return None
    return r.stdout


def note(name, content):
    if content is None:
        return
    items.append({
        "id": str(uuid.uuid4()), "organizationId": None, "folderId": FILES_ID,
        "type": 2, "reprompt": 0, "name": name, "notes": content,
        "favorite": False, "secureNote": {"type": 0}, "collectionIds": None,
    })


def login(name, username, password, uri=None, notes=None):
    if not password:
        skipped.append(f"login {name!r}: value not found")
        return
    items.append({
        "id": str(uuid.uuid4()), "organizationId": None, "folderId": LOGINS_ID,
        "type": 1, "reprompt": 0, "name": name, "notes": notes,
        "favorite": False, "collectionIds": None,
        "login": {
            "uris": [{"match": None, "uri": uri}] if uri else [],
            "username": username, "password": password, "totp": None,
        },
    })


def parse_env(content):
    out = {}
    for line in (content or "").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


LADYBIRD_FILES = [
    "/opt/stacks/dash/.env",
    "/opt/stacks/frigate/.env",
    "/opt/stacks/immich/.env",
    "/opt/stacks/sdr/.env",
    "/opt/stacks/home/homeassistant/secrets.yaml",
    "/opt/stacks/net/kuma-push-url.txt",
    "/opt/stacks/net/kuma-push-url-ups.txt",
    "/opt/stacks/net/kuma-frigate-push-url.txt",
    "/opt/stacks/net/kuma-vw-backup-push-url.txt",
    "/opt/stacks/n8n/n8n-data/config",      # n8n encryption key
    "/home/thomas/portfolio-advisor/.env",  # outside the /opt/stacks tree
]
# Sweep per-stack data/.env files (e.g. hermes/data/.env) so a NEW stack's
# secrets are harvested automatically instead of being forgotten here. The
# vaultwarden stack's own .env is intentionally never captured (it holds no
# secret, and the vault must not bootstrap from itself).
LADYBIRD_FILES += sorted(set(glob.glob("/opt/stacks/*/data/.env")) - set(LADYBIRD_FILES))

texts = {}
for path in LADYBIRD_FILES:
    texts[path] = read(path)
    note("ladybird:" + path, texts[path])

# The control node's repo .env, staged next to this script by hand (optional).
repo_env_text = read("/root/repo-dotenv") if os.path.exists("/root/repo-dotenv") else None
if repo_env_text is None:
    skipped.append("repo .env not staged at /root/repo-dotenv (logins from it skipped)")
note("repo:home-automation/.env (control node)", repo_env_text)

note("snibbly:/etc/llama/llama.env", snibbly("sudo -n cat /etc/llama/llama.env"))
note("snibbly:/opt/stacks/langfuse/.env",
     snibbly("cat /opt/stacks/langfuse/.env 2>/dev/null || sudo -n cat /opt/stacks/langfuse/.env"))

repo_env = parse_env(repo_env_text)
dash_env = parse_env(texts.get("/opt/stacks/dash/.env"))

login("Uptime Kuma admin", "admin", repo_env.get("KUMA_PW"),
      "http://192.168.0.13:3001")
login("AdGuard Home admin", dash_env.get("ADGUARD_USERNAME"),
      dash_env.get("ADGUARD_PASSWORD"), dash_env.get("ADGUARD_BASE_URL"))
login("Camera admin", "admin", repo_env.get("CAMERA_PW"),
      notes="Camera web admin. Frigate's RTSP credential is in "
            "ladybird:/opt/stacks/frigate/.env")
login("ladybird ssh + sudo", repo_env.get("SSH_USER", "thomas"),
      repo_env.get("SSH_PW"), "ssh://192.168.0.13")

os.umask(0o177)
with open(OUT, "w") as f:
    json.dump({"encrypted": False, "folders": folders, "items": items}, f, indent=1)

notes_n = sum(1 for i in items if i["type"] == 2)
logins_n = sum(1 for i in items if i["type"] == 1)
print(f"wrote {OUT}: {notes_n} file notes + {logins_n} logins")
for s in skipped:
    print(f"  skipped: {s}")
print("import via web vault (Tools -> Import data -> Bitwarden .json), then:")
print(f"  shred -u {OUT} /root/repo-dotenv")
