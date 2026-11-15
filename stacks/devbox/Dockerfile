# Devbox — the agent development workbench (docs/devbox.md).
#
# One image holds the whole loop: herdr (terminal workspace server) owns the
# agent terminals, pi (coding agent CLI) runs inside herdr panes against
# snibbly's LLM door, collie serves the phone PWA off herdr's local socket.
# They share this container on purpose — collie speaks to herdr over a unix
# socket, so splitting them would mean sharing a socket directory anyway.
#
# Everything is version-pinned. herdr and collie both ship self-updaters, but
# they install here root-owned under /usr/local/bin while the container runs
# as dev, so the self-update fails and the pin actually holds. Bump the ARGs
# deliberately, rebuild, recreate — sessions die on recreate, state does not
# (both volumes survive; docs/devbox.md "Rebuilding without losing state").
ARG HERDR_VERSION=0.9.5
ARG COLLIE_VERSION=1.28.4
ARG PI_VERSION=1.5.2

FROM debian:bookworm-slim
ARG HERDR_VERSION
ARG COLLIE_VERSION
ARG PI_VERSION

# Base toolchain for whatever the agents clone: compilers, python, git.
# ncurses-term carries the terminfo entries a TUI full of terminals needs.
RUN apt-get update && apt-get install -y --no-install-recommends \
      git curl wget ca-certificates gnupg \
      build-essential pkg-config \
      python3 python3-pip python3-venv \
      locales ncurses-base ncurses-term \
      jq procps less openssh-client \
    && sed -i 's/^# *en_US.UTF-8/en_US.UTF-8/' /etc/locale.gen \
    && locale-gen \
    && rm -rf /var/lib/apt/lists/*
ENV LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8

# Node.js 22 (pi's npm package needs >=22; Debian's packaged node is too old).
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# GitHub CLI from its apt repo — gh reads GH_TOKEN from the environment, so
# no auth state is baked into the image or written outside /home/dev.
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# herdr — pinned release binary, NOT the curl|sh installer (reproducible
# builds; the installer always fetches latest).
RUN curl -fsSL -o /usr/local/bin/herdr \
      "https://github.com/herdrdev/herdr/releases/download/v${HERDR_VERSION}/herdr-linux-x86_64" \
    && chmod 0755 /usr/local/bin/herdr

# collie — pinned release tarball (ships a single binary inside).
RUN curl -fsSL -o /tmp/collie.tgz \
      "https://github.com/AltanS/collie/releases/download/v${COLLIE_VERSION}/collie-${COLLIE_VERSION}-linux-x64.tar.gz" \
    && mkdir /tmp/collie && tar -xzf /tmp/collie.tgz -C /tmp/collie \
    && install -m 0755 "$(find /tmp/collie -type f -name collie | head -n1)" /usr/local/bin/collie \
    && rm -rf /tmp/collie /tmp/collie.tgz

# pi — the coding agent the panes run. The @mariozechner/pi-coding-agent
# package is deprecated; @earendil-works is the live line.
RUN npm install -g "@earendil-works/pi-coding-agent@${PI_VERSION}" \
    && npm cache clean --force

# Room for more agents later — one pinned line each, then a one-time login
# inside the container (docs/devbox.md "Adding Claude Code or Codex"):
#   RUN npm install -g @anthropic-ai/claude-code@<version>
#   RUN npm install -g @openai/codex@<version>

# dev owns both volumes. /workspace must exist in the image with this
# ownership — docker copies it onto the named volume on first use; created
# only at runtime it would come up root-owned under `user: dev`.
RUN useradd --uid 1000 --create-home --shell /bin/bash dev \
    && mkdir -p /workspace && chown dev:dev /workspace

COPY --chmod=0755 entrypoint.sh /usr/local/bin/devbox-entrypoint
COPY --chmod=0755 check.sh /usr/local/bin/check.sh

USER dev
WORKDIR /workspace
ENTRYPOINT ["/usr/local/bin/devbox-entrypoint"]
