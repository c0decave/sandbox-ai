# Goose (aaif-goose/goose, formerly block/goose, Apache-2.0) as sandbox-ai's agent goose (sandbox-ai build goose).
# Only the reviewed release (docs/GOOSE_REVIEW.md, sandbox/goose.py): the release's Linux binary (glibc, without
# Vulkan), downloaded from the release and checked against its SHA-256 in Containerfile.goose.lock before it is
# unpacked; nothing is piped into a shell (never upstream's download_cli.sh). Goose cannot update itself: its binary
# is read-only in the sandbox (a new release is reviewed, then built here). No desktop app.
# uv ships as a static binary in its official image; COPY --from takes only it (MCP servers and kits use uvx).
ARG UV_VERSION=0.13.5
# Floating tag: the current Active LTS line with its newest patch release (the agents' common base: Node.js runs MCP
# servers and Goose's npx extensions). Pin with --build-arg NODE_IMAGE=docker.io/library/node:<version>-bookworm-slim.
ARG NODE_IMAGE=docker.io/library/node:lts-bookworm-slim
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM ${NODE_IMAGE}

# The reviewed release; another one has no digest in the lock (the check below fails).
ARG GOOSE_VERSION=1.57.0
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates bash git python3 python3-pip python3-venv firejail nftables ripgrep util-linux \
    vim netcat-traditional bind9-dnsutils iputils-ping iproute2 traceroute curl wget tmux \
    openssh-client jq openssl bzip2 \
    && rm -rf /var/lib/apt/lists/*
# GitHub CLI from GitHub's own apt repository (Debian's gh is years behind), the key over HTTPS.
RUN mkdir -p -m 755 /etc/apt/keyrings \
    && wget -nv -O /etc/apt/keyrings/githubcli-archive-keyring.gpg \
       https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
       > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*
RUN gh --version
# GUI base (--gui): virtual display, VNC over a Unix socket, fonts and the runtime libraries Playwright's Chromium
# needs (fetched at run time, e.g. `uvx playwright install chromium`).
RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb x11vnc x11-utils fonts-liberation fonts-dejavu-core \
    libasound2 libatk-bridge2.0-0 libatk1.0-0 libatspi2.0-0 libcairo2 libcairo-gobject2 libcups2 \
    libdbus-1-3 libdbus-glib-1-2 libdrm2 libgbm1 libglib2.0-0 libgtk-3-0 libnspr4 libnss3 \
    libpango-1.0-0 libpangocairo-1.0-0 libx11-6 libx11-xcb1 libxcb1 libxcb-shm0 libxcomposite1 \
    libxcursor1 libxdamage1 libxext6 libxfixes3 libxi6 libxkbcommon0 libxrandr2 libxrender1 libxtst6 \
    libegl1 libgl1 libopengl0 libxkbcommon-x11-0 libxcb-cursor0 libxshmfence1 libxkbfile1 libfontconfig1 gdb \
    && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /uvx /usr/local/bin/
RUN python3 -m pip --version && uv --version
# Goose exactly as locked: the tarball of this machine's architecture from the release, its SHA-256 checked against
# the lock before anything of it is unpacked, the one binary it holds installed read-only.
COPY Containerfile.goose.lock /opt/goose/SHA256SUMS
RUN case "$(dpkg --print-architecture)" in amd64) arch=x86_64 ;; arm64) arch=aarch64 ;; \
       *) echo "no Goose release for $(dpkg --print-architecture)" >&2; exit 1 ;; esac \
    && asset="goose-${arch}-unknown-linux-gnu.tar.gz" \
    && mkdir -p /tmp/goose-dl && cd /tmp/goose-dl \
    && curl -fsSL --proto '=https' --tlsv1.2 -o "$asset" \
       "https://github.com/aaif-goose/goose/releases/download/v${GOOSE_VERSION}/${asset}" \
    && grep -E "^[0-9a-f]{64}  ${asset}\$" /opt/goose/SHA256SUMS > SHA256SUMS \
    && sha256sum -c --strict SHA256SUMS \
    && tar -xzf "$asset" ./goose \
    && install -m 0755 goose /usr/local/bin/goose \
    && cd / && rm -rf /tmp/goose-dl \
    && test "$(HOME=/tmp/goose-check /usr/local/bin/goose --version | tr -d ' ')" = "${GOOSE_VERSION}" \
    && rm -rf /tmp/goose-check
# sandbox-ai's telemetry guard (telemetry_guard.py): `goose` is a shim that checks, then becomes Goose (its binary
# moves to /usr/local/lib/sandbox-ai/real). Whatever starts the agent in the sandbox - the launcher, a shell, another
# agent - it refuses what would turn its telemetry on and pins the opt-out variables first; the launcher mounts its
# current copy read-only over this one.
COPY telemetry_guard.py /usr/local/lib/sandbox-ai/telemetry_guard.py
RUN python3 -IS /usr/local/lib/sandbox-ai/telemetry_guard.py --install goose
# Its system configuration is mounted here by the launcher (read-only, sandbox/goose.py).
RUN mkdir -p -m 755 /etc/goose && HOME=/tmp/goose-check goose --version && rm -rf /tmp/goose-check
ENV HOME=/root
WORKDIR /work
