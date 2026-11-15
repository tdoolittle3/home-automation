#!/usr/bin/env bash
# Devbox entrypoint — seed config, then run the two long-lived processes.
#
# Lifetime design (docs/devbox.md): herdr is what the container lives and dies
# with. It owns the agent terminals — kill it and every running session dies —
# so it runs in the foreground as the main process, and its death ends the
# container (restart: unless-stopped brings it back). collie is only the phone
# UI: it can crash or restart without touching a session, so it runs in a
# supervised background loop instead of being the thing that keeps the
# container alive.
set -u

: "${LLM_BASE_URL:?set in .env}"
: "${LLM_MODEL:?set in .env}"
: "${LLM_API_KEY:?set in .env}"
: "${GIT_USER_NAME:?set in .env}"
: "${GIT_USER_EMAIL:?set in .env}"
: "${COLLIE_PORT:=8787}"

# --- pi: register snibbly as an OpenAI-compatible provider -------------------
# Seeded once; after that the copy on the devbox_home volume is the truth, so
# live edits (new models, per-model options) survive restarts and rebuilds.
# apiKey stays an env reference — pi interpolates ${LLM_API_KEY} from its own
# environment (set via env_file), so the key is never written to the volume.
PI_MODELS="$HOME/.pi/agent/models.json"
if [ ! -f "$PI_MODELS" ]; then
  mkdir -p "$(dirname "$PI_MODELS")"
  cat > "$PI_MODELS" <<EOF
{
  "\$schema": "https://pi.dev/schemas/models.schema.json",
  "providers": {
    "snibbly": {
      "baseUrl": "${LLM_BASE_URL}",
      "api": "openai-completions",
      "apiKey": "\${LLM_API_KEY}",
      "models": [{ "id": "${LLM_MODEL}" }]
    }
  }
}
EOF
  echo "seeded $PI_MODELS (provider snibbly, model ${LLM_MODEL})"
fi

# --- git + gh -----------------------------------------------------------------
# Idempotent every boot. gh reads GH_TOKEN straight from the environment —
# rotation is: edit .env, docker compose up -d. setup-git routes git's HTTPS
# credentials through gh so agents can clone/push private repos by name.
git config --global user.name "${GIT_USER_NAME}"
git config --global user.email "${GIT_USER_EMAIL}"
git config --global init.defaultBranch master
if ! gh auth setup-git 2>/dev/null; then
  echo "WARN: gh auth setup-git failed — GH_TOKEN missing or dead? check.sh will say." >&2
fi

# --- collie: supervised, restartable ------------------------------------------
# COLLIE_HOST/COLLIE_PORT/COLLIE_MUX come from the compose environment. If
# `collie start` turns out to daemonize instead of staying foreground, the
# loop shows it in `docker logs devbox` as a rebind failure every 5s — the
# fallback is its internal foreground server (`collie _exec-bridge`), see
# docs/devbox.md "First run".
(
  while :; do
    collie start
    echo "collie exited ($?) — restarting in 5s" >&2
    sleep 5
  done
) &

# --- herdr: the main process ---------------------------------------------------
exec herdr server
