#!/usr/bin/env bash
# Devbox connectivity checks — run inside the container:
#   docker compose exec devbox check.sh
# Exits non-zero if anything fails. docs/devbox.md "First run".
set -u

fail=0
check() { # check <name> <command...>
  local name="$1"; shift
  if "$@" >/dev/null 2>&1; then
    echo "ok    $name"
  else
    echo "FAIL  $name"
    fail=1
  fi
}

# 1. The LLM door answers AND serves the configured model. litellm's /models
#    also lists catalogue noise (openai/* entries), so assert ours is present
#    rather than eyeballing "a model list".
llm_models() {
  curl -fsS -m 10 -H "Authorization: Bearer ${LLM_API_KEY}" \
    "${LLM_BASE_URL}/models" | grep -q "\"${LLM_MODEL}\""
}
check "llm: ${LLM_MODEL} listed at ${LLM_BASE_URL}/models" llm_models

# 2. The GitHub token is alive.
check "gh auth status (GH_TOKEN)" gh auth status

# 3. A private repo is reachable THROUGH the token — proves the fine-grained
#    scope actually covers it, which `gh auth status` alone does not.
if [ -n "${DEVBOX_CHECK_REPO:-}" ]; then
  check "git ls-remote ${DEVBOX_CHECK_REPO}" git ls-remote --heads "${DEVBOX_CHECK_REPO}"
else
  echo "skip  git ls-remote (set DEVBOX_CHECK_REPO in .env to enable)"
fi

# 4. The herdr server is up (its socket answers).
check "herdr server socket" herdr status server

# 5. collie answers on the container port — the phone chain's first link.
check "collie on :${COLLIE_PORT:-8787}" \
  curl -fsS -m 5 "http://127.0.0.1:${COLLIE_PORT:-8787}/"

exit "$fail"
