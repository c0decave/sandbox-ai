#!/usr/bin/env bash
# A throwaway Gitea for push and privacy-check end-to-end tests: on 127.0.0.1:PORT of whoever starts it (the host,
# or the sandbox-ai-dev sandbox with Podman inside), SQLite, no mail, registration closed, an admin user made at the
# start; everything in the container (gone with `stop`).
#
#   tests/devtools/gitea.sh start [--port 3080] [--engine podman|docker] [--dry-run]
#   tests/devtools/gitea.sh stop  [--engine podman|docker]
#
# `start` prints the URL and the admin's name and password (made up for this container only) as JSON. The image is
# pinned by its tag (GITEA_IMAGE overrides it, e.g. with a digest); the tag is a release well older than 14 days.
set -euo pipefail

IMAGE=${GITEA_IMAGE:-docker.io/gitea/gitea:1.24.6}
NAME=sandbox-ai-test-gitea
action=${1:-}; shift || true
port=3080 engine=podman dry=0
while [[ $# -gt 0 ]]; do
  case $1 in
    --port) port=${2:?}; shift 2 ;;
    --engine) engine=${2:?}; shift 2 ;;
    --dry-run) dry=1; shift ;;
    *) echo "gitea.sh: unknown option $1" >&2; exit 2 ;;
  esac
done
[[ $port =~ ^[0-9]+$ ]] || { echo "gitea.sh: --port expects a number" >&2; exit 2; }

run() {  # --dry-run: the command on standard error, nothing run
  if [[ $dry == 1 ]]; then { printf '%q ' "$@"; echo; } >&2; else "$@"; fi
}

case $action in
  start)
    password=$(head -c 18 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 20)
    run "$engine" run -d --rm --name "$NAME" -p "127.0.0.1:$port:3000" \
      -e GITEA__database__DB_TYPE=sqlite3 -e GITEA__security__INSTALL_LOCK=true \
      -e GITEA__service__DISABLE_REGISTRATION=true -e GITEA__mailer__ENABLED=false \
      -e "GITEA__server__ROOT_URL=http://localhost:$port/" "$IMAGE" >/dev/null
    if [[ $dry == 0 ]]; then
      for _ in $(seq 60); do
        curl -fsS "http://127.0.0.1:$port/api/healthz" >/dev/null 2>&1 && break
        sleep 1
      done
    fi
    run "$engine" exec -u git "$NAME" gitea admin user create --admin --username tester --password "$password" \
      --email tester@example.org --must-change-password=false >/dev/null
    printf '{"url": "http://localhost:%s", "user": "tester", "password": "%s"}\n' "$port" "$password"
    ;;
  stop)
    run "$engine" rm -f "$NAME" >/dev/null
    ;;
  *)
    sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
    ;;
esac
