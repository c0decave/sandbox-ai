#!/usr/bin/env bash
set -euo pipefail
script_path=$(readlink -f -- "${BASH_SOURCE[0]}")
script_dir=$(dirname -- "$script_path")
usage='Usage: sandbox-ai build [-pu|--podman-url URL] [-p|--pin VERSION] [-u|--uv VERSION] [-n|--node IMAGE]
                        [-e|--extension-only] codex|claude|opencode|all|VARIANT [podman build options]
       sandbox-ai build [-pu|--podman-url URL] [-u|--uv VERSION] [-n|--node IMAGE] team [podman build options]
       sandbox-ai build [-pu|--podman-url URL] vpn [podman build options]

Every build uses the newest releases unless you pin them: the newest npm release
of the agent (looked up first, so the cached install layer is rebuilt), the
newest uv image and the current Node.js LTS image (--pull=newer). No image is
built if a lookup fails.

  --pin VERSION    install exactly this agent version (one agent only); also
                   --build-arg <AGENT>_VERSION=VERSION
  --uv VERSION     use this uv image tag instead of latest
  --node IMAGE     use this Node.js base image, e.g. a tag or an @sha256 digest

The image is labelled with the installed agent version (sandbox-ai.agent-version).

Extensions (sandbox-ai images extend/new): the base image is tagged
localhost/AGENT-sandbox:base; an agent with an extension gets it as a layer on
top (localhost/AGENT-sandbox:latest), and building an agent also builds its
variants (localhost/AGENT-sandbox-NAME). A VARIANT builds its agent'"'"'s base and
that variant only. Each layer is labelled with the hash of its extension.

  --extension-only  build only the extension layers, on the base image there is

team builds localhost/team-sandbox:latest with Claude Code, Codex and OpenCode, the
image of a start with --with AGENT (each agent at its newest release, or
--build-arg CLAUDE_VERSION=X.Y.Z and so on; no extensions).

vpn builds localhost/sandbox-ai-vpn:latest, the container of --vpn (WireGuard,
wireguard-go, OpenVPN, Tailscale, nftables); pin its parts with --build-arg
WIREGUARD_GO_VERSION=v0.0.YYYYMMDD or TAILSCALE_IMAGE=docker.io/tailscale/tailscale:vX.Y.Z.'
die_usage() { echo "$usage" >&2; exit 2; }

podman_cmd=(podman) podman_url="" pin="" uv="" node="" agents=() variant="" extension_only="" vpn="" team=""
python=${SANDBOX_AI_PYTHON:-python3}
images=("$python" -I "$script_dir/images.py")
while [[ $# -gt 0 ]]; do
  case "$1" in
    -V|--version)
      version=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$script_dir/agent_sandbox.py")
      echo "sandbox-ai $version"; exit 0 ;;
    -h|--help) echo "$usage"; exit 0 ;;
    -pu|--podman-url) [[ $# -ge 2 && -n "$2" ]] || die_usage; podman_cmd+=(--remote --url "$2"); podman_url=$2; shift 2 ;;
    -e|--extension-only) extension_only=1; shift ;;
    -p|--pin) [[ $# -ge 2 && -n "$2" ]] || die_usage; pin=$2; shift 2 ;;
    -u|--uv) [[ $# -ge 2 && -n "$2" ]] || die_usage; uv=$2; shift 2 ;;
    -n|--node) [[ $# -ge 2 && -n "$2" ]] || die_usage; node=$2; shift 2 ;;
    codex|claude|opencode) agents=("$1"); shift; break ;;
    all) agents=(codex claude opencode); shift; break ;;
    vpn) vpn=1; shift; break ;;
    team) team=1; shift; break ;;
    [a-z0-9]*)
      # A variant (sandbox-ai images new NAME --from AGENT): its agent's base, then the variant only.
      [[ "$1" =~ ^[a-z0-9][a-z0-9_.-]{0,29}$ ]] || die_usage
      plan=$("${images[@]}" build-plan "$1") || {
        echo "sandbox-build-image: $1 is neither codex, claude, opencode, all, vpn nor a usable variant" >&2; exit 2; }
      agents=("${plan%% *}") variant=$1; shift; break ;;
    *) die_usage ;;
  esac
done
[[ ${#agents[@]} -gt 0 || -n "$vpn" || -n "$team" ]] || die_usage
# Without a systemd user session (no user bus: an unprivileged LXC or Proxmox container, a login without systemd)
# the RUN steps of a build ask systemd for their cgroup and fail ("sd-bus call: Permission denied"); Podman falls
# back to cgroupfs for its own runs, not for those. Build with cgroupfs then.
# (macOS: podman is the client of podman machine's VM, which has its own systemd; never there.)
if [[ -z "$podman_url" && "$(uname -s)" == Linux && ! -S "/run/user/$(id -u)/bus" ]]; then
  podman_cmd+=(--cgroup-manager=cgroupfs)
  echo "sandbox-build-image: no systemd user session: building with --cgroup-manager=cgroupfs" \
       "(sudo loginctl enable-linger $(id -un) gives one)" >&2
fi
if [[ -n "$vpn" ]]; then
  [[ -z "$pin$uv$node$extension_only" ]] || {
    echo "sandbox-build-image: --pin, --uv, --node and --extension-only apply to the agent images, not vpn" >&2; exit 2; }
  exec "${podman_cmd[@]}" build --pull=newer --tag localhost/sandbox-ai-vpn:latest --label sandbox-ai.role=vpn \
    --file "$script_dir/Containerfile.vpn" "$@" "$script_dir"
fi
semver='^[0-9]+\.[0-9]+\.[0-9]+([.+-][0-9A-Za-z.+-]+)?$'
if [[ -n "$pin" ]]; then
  [[ ${#agents[@]} == 1 ]] || { echo "sandbox-build-image: --pin applies to one agent, not $([[ -n "$team" ]] && echo team || echo all)" >&2; exit 2; }
  [[ "$pin" =~ $semver ]] || { echo "sandbox-build-image: --pin expects X.Y.Z: $pin" >&2; exit 2; }
fi
[[ -z "$uv" || "$uv" =~ ^[0-9A-Za-z._-]+$ ]] || { echo "sandbox-build-image: --uv expects a tag: $uv" >&2; exit 2; }
[[ -z "$node" || "$node" =~ ^[A-Za-z0-9./:@_-]+$ ]] || { echo "sandbox-build-image: --node expects an image: $node" >&2; exit 2; }

package() {
  case "$1" in
    codex) echo "@openai/codex" ;;
    claude) echo "@anthropic-ai/claude-code" ;;
    opencode) echo "opencode-ai" ;;
  esac
}

team_build() {
  # Every agent at its newest release unless a --build-arg pins it; one image, no extensions.
  local extra=(--pull=newer) labels=() agent given version
  [[ -z "$pin$extension_only" ]] || {
    echo "sandbox-build-image: --pin and --extension-only apply to one agent's image, not team" >&2; exit 2; }
  for agent in claude codex opencode; do
    given=$(explicit_version "$agent" "$@")
    if [[ -n "$given" ]]; then
      version=${given#set:}
      [[ "$version" =~ $semver ]] || { echo "sandbox-build-image: ${agent^^}_VERSION expects X.Y.Z: '$version'" >&2; exit 2; }
    else
      version=$(latest_version "$(package "$agent")")
      extra+=(--build-arg "${agent^^}_VERSION=$version")
    fi
    echo "sandbox-build-image: team: $agent -> $version" >&2
    labels+=(--label "sandbox-ai.$agent-version=$version")
  done
  [[ -z "$uv" ]] || extra+=(--build-arg "UV_VERSION=$uv")
  [[ -z "$node" ]] || extra+=(--build-arg "NODE_IMAGE=$node")
  exec "${podman_cmd[@]}" build --tag localhost/team-sandbox:latest --label sandbox-ai.role=team "${labels[@]}" \
    --file "$script_dir/Containerfile.team" "${extra[@]}" "$@" "$script_dir"
}

latest_version() {
  local version
  if command -v npm >/dev/null; then
    version=$(npm view "$1" version)
  else
    version=$(python3 -I -c 'import json,sys,urllib.request
print(json.load(urllib.request.urlopen(sys.argv[1], timeout=30))["version"])' "https://registry.npmjs.org/$1/latest")
  fi
  # The value becomes a build argument; accept plain semver only.
  [[ "$version" =~ $semver ]] || {
    echo "sandbox-build-image: unexpected version for $1: $version" >&2; exit 1; }
  echo "$version"
}

# A version given as podman build argument pins too.
# Prints "set:VALUE" when the argument is present (VALUE may be empty), nothing otherwise.
explicit_version() {
  local key="${1^^}_VERSION=" previous="" arg found=""
  for arg in "${@:2}"; do
    if [[ "$previous" == --build-arg && "$arg" == "$key"* ]]; then found="set:${arg#"$key"}"; fi
    if [[ "$arg" == --build-arg="$key"* ]]; then found="set:${arg#--build-arg="$key"}"; fi
    previous=$arg
  done
  echo "$found"
}

if [[ -n "$team" ]]; then
  team_build "$@"
fi

# Check every extension and resolve every version before building anything.
declare -A kinds=() variant_counts=()
for agent in "${agents[@]}"; do
  plan=$("${images[@]}" build-plan "$agent") || {
    echo "sandbox-build-image: fix the extension of $agent first (sandbox-ai images check $agent)" >&2; exit 1; }
  read -r _ kind count <<<"$plan"
  kinds[$agent]=$kind variant_counts[$agent]=$count
done
declare -A versions=() pinned=()
for agent in "${agents[@]}"; do
  [[ -z "$extension_only" ]] || break
  given=$(explicit_version "$agent" "$@")
  given_set=${given:+1}
  given=${given#set:}
  if [[ -n "$pin" && -n "$given" && "$given" != "$pin" ]]; then
    echo "sandbox-build-image: --pin $pin conflicts with --build-arg ${agent^^}_VERSION=$given" >&2; exit 2
  fi
  if [[ -n "$given_set" && ! "$given" =~ $semver ]]; then
    echo "sandbox-build-image: ${agent^^}_VERSION expects X.Y.Z: '$given'" >&2; exit 2
  fi
  if [[ -n "$pin" ]]; then
    versions[$agent]=$pin pinned[$agent]=1
  elif [[ -n "$given_set" ]]; then
    versions[$agent]=$given pinned[$agent]=given
  else
    versions[$agent]=$(latest_version "$(package "$agent")")
  fi
  echo "sandbox-build-image: $agent -> ${versions[$agent]}${pinned[$agent]:+ (pinned)}" >&2
done

status=0
for agent in "${agents[@]}"; do
  if [[ -z "$extension_only" ]]; then
    # Floating base tags are refreshed; pinned ones stay what they name.
    extra=(--pull=newer)
    [[ "${pinned[$agent]:-}" == given ]] || extra+=(--build-arg "${agent^^}_VERSION=${versions[$agent]}")
    [[ -z "$uv" ]] || extra+=(--build-arg "UV_VERSION=$uv")
    [[ -z "$node" ]] || extra+=(--build-arg "NODE_IMAGE=$node")
    extra+=(--label "sandbox-ai.agent-version=${versions[$agent]}")
    # Without an extension the base is the agent's image; with one, the layer below it.
    tags=(--tag "localhost/${agent}-sandbox:base")
    [[ "${kinds[$agent]}" != plain ]] || tags=(--tag "localhost/${agent}-sandbox:latest" "${tags[@]}")
    "${podman_cmd[@]}" build "${tags[@]}" --file "$script_dir/Containerfile.$agent" \
      "${extra[@]}" "$@" "$script_dir"
  fi
  layer=(layer "$agent" --variants)
  [[ -z "$variant" ]] || layer=(layer "$variant")
  if [[ -n "$variant" || -n "$extension_only" || "${kinds[$agent]}" != plain || "${variant_counts[$agent]}" != 0 ]]; then
    "${images[@]}" ${podman_url:+--podman-url "$podman_url"} "${layer[@]}" || status=1
  fi
done
exit $status
