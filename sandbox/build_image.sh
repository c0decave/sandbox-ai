#!/usr/bin/env bash
set -euo pipefail
script_path=$(readlink -f -- "${BASH_SOURCE[0]}")
script_dir=$(dirname -- "$script_path")
usage='Usage: sandbox-ai build [-pu|--podman-url URL] [-p|--pin VERSION] [-u|--uv VERSION] [-n|--node IMAGE]
                        [-e|--extension-only] codex|claude|opencode|all|VARIANT [podman build options]
       sandbox-ai build [-pu|--podman-url URL] [-u|--uv VERSION] [-n|--node IMAGE] team [podman build options]
       sandbox-ai build [-pu|--podman-url URL] [-e|--extension-only] --variant VARIANT [podman build options]
       sandbox-ai build [-pu|--podman-url URL] vpn|tor [podman build options]

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
that variant only; --variant VARIANT names one whose name is also a target here (the
variant tor of the recipe tor: build tor is the image of the Tor VPN). Each layer is
labelled with the hash of its extension. A variant on one of your images (images new
NAME --base IMAGE) gets the agent'"'"'s layers on that image instead (Debian 12; nothing
but uv'"'"'s image is pulled), then its extension.

  --extension-only  build only the extension layers, on the base image there is

team builds localhost/team-sandbox:latest with Claude Code, Codex and OpenCode, the
image of a start with --with AGENT (each agent at its newest release, or
--build-arg CLAUDE_VERSION=X.Y.Z and so on; no extensions).

vpn builds localhost/sandbox-ai-vpn:latest, the container of --vpn with a
WireGuard, OpenVPN, Mullvad or Tailscale profile (WireGuard, wireguard-go,
OpenVPN, Tailscale, nftables); pin its parts with --build-arg
WIREGUARD_GO_VERSION=v0.0.YYYYMMDD (or none) or
TAILSCALE_IMAGE=docker.io/tailscale/tailscale:vX.Y.Z. wireguard-go is only the
fallback for a host without the wireguard module: when Go'"'"'s module proxy cannot
be reached the image is built without it, and the build says so at the end.

tor builds localhost/sandbox-ai-tor:latest, the container of --vpn with a Tor
profile (vpn add NAME --tor): tor, obfs4proxy, the GeoIP files, nftables. Tor is
not a VPN; the two images share no software.'
die_usage() { echo "$usage" >&2; exit 2; }

podman_cmd=(podman) podman_url="" pin="" uv="" node="" agents=() variant="" variant_kind="" extension_only="" vpn="" team=""
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
    vpn) vpn=vpn; shift; break ;;
    tor) vpn=tor; shift; break ;;
    team) team=1; shift; break ;;
    --variant)
      # The variant NAME even when NAME is also a target above (the recipe "tor" makes the variant tor, while
      # "build tor" is the Tor VPN's image).
      [[ $# -ge 2 && "$2" =~ ^[a-z0-9][a-z0-9_.-]{0,29}$ ]] || die_usage
      plan=$("${images[@]}" build-plan "$2" 2>/dev/null) || {
        why=$("${images[@]}" build-plan "$2" 2>&1 >/dev/null | tail -n 1) || true
        echo "sandbox-ai build: $2 is not a usable variant${why:+ (${why#sandbox-ai images: })}" >&2
        exit 2; }
      agents=("${plan%% *}") variant=$2; read -r _ variant_kind _ <<<"$plan"; shift 2; break ;;
    [a-z0-9]*)
      # A variant (sandbox-ai images new NAME --from AGENT): its agent's base, then the variant only.
      [[ "$1" =~ ^[a-z0-9][a-z0-9_.-]{0,29}$ ]] || die_usage
      # images.py's own refusal (no such variant, a damaged one) is said once, in this command's name
      plan=$("${images[@]}" build-plan "$1" 2>/dev/null) || {
        why=$("${images[@]}" build-plan "$1" 2>&1 >/dev/null | tail -n 1) || true
        echo "sandbox-ai build: $1 is neither codex, claude, opencode, all, team, vpn, tor nor a usable variant${why:+ (${why#sandbox-ai images: })}" >&2
        exit 2; }
      agents=("${plan%% *}") variant=$1; read -r _ variant_kind _ <<<"$plan"; shift; break ;;
    *) die_usage ;;
  esac
done
[[ ${#agents[@]} -gt 0 || -n "$vpn" || -n "$team" ]] || die_usage
# Built as root for someone else (sudo): Podman keeps images per user - root's store is not the one the user's
# sandboxes (and the app, run as that user) look in. Said, not refused: root may well use sandbox-ai itself.
if [[ -z "$podman_url" && "$(id -u)" == 0 && -n "${SUDO_USER:-}" && "${SUDO_USER}" != root ]]; then
  echo "sandbox-ai build: warning: building as root (sudo): the images go to root's Podman store; sandboxes" \
       "${SUDO_USER} starts (and the app) use ${SUDO_USER}'s own - build as ${SUDO_USER} for those" >&2
fi
# Without a systemd user session (no user bus: an unprivileged LXC or Proxmox container, a login without systemd)
# the RUN steps of a build ask systemd for their cgroup and fail ("sd-bus call: Permission denied"); Podman falls
# back to cgroupfs for its own runs, not for those. Build with cgroupfs then.
# (macOS: podman is the client of podman machine's VM, which has its own systemd; never there.)
if [[ -z "$podman_url" && "$(uname -s)" == Linux && ! -S "/run/user/$(id -u)/bus" ]]; then
  podman_cmd+=(--cgroup-manager=cgroupfs)
  echo "sandbox-ai build: no systemd user session: building with --cgroup-manager=cgroupfs" \
       "(sudo loginctl enable-linger $(id -un) gives one)" >&2
fi
# Names during the build: its RUN steps (apt-get, npm, go) resolve through Podman's own network, which on some hosts
# answers no name at all (a local resolver such as systemd-resolved's 127.0.0.53 or dnsmasq that the build's
# network does not reach): every image then fails with "Temporary failure resolving deb.debian.org". Checked first,
# in a container of the base image (pulled by Podman itself, on the host's network); when only the host's network
# resolves, the build uses it (the RUN steps are this repository's own Containerfiles, never an agent's code) and
# says so; when neither does, it stops before anything is built and says what to look at. --network or --dns given
# to the build: left as given. SANDBOX_AI_BUILD_NO_DNS_CHECK=1 skips the check.
build_net=()
given_network() {
  local arg
  for arg in "$@"; do
    case "$arg" in --network|--network=*|--net|--net=*|--dns|--dns=*) return 0 ;; esac
  done
  return 1
}
probe_image=${SANDBOX_AI_BUILD_PROBE_IMAGE:-docker.io/library/debian:bookworm-slim}
dns_works() {
  timeout 60 "${podman_cmd[@]}" run --rm --pull=never "$@" "$probe_image" getent hosts deb.debian.org >/dev/null 2>&1
}
if [[ -z "${SANDBOX_AI_BUILD_NO_DNS_CHECK:-}" ]] && ! given_network "$@"; then
  if "${podman_cmd[@]}" image exists "$probe_image" >/dev/null 2>&1 \
      || "${podman_cmd[@]}" pull -q "$probe_image" >/dev/null 2>&1; then
    if ! dns_works; then
      if dns_works --network=host; then
        build_net=(--network=host)
        export SANDBOX_AI_BUILD_NETWORK=host  # the extension layers (images.py) build the same way
        echo "sandbox-ai build: Podman's build network resolves no names here (deb.debian.org); building with the" \
             "host's network (--network=host): the RUN steps are sandbox-ai's own Containerfiles" >&2
      else
        echo "sandbox-ai build: no name resolves in a container, not even on the host's network (deb.debian.org)." >&2
        echo "  Look at: /etc/resolv.conf (a resolver only on 127.0.0.x?), podman info (networkBackend, pasta or" >&2
        echo "  slirp4netns), a firewall or VPN on this machine. Give a resolver: sandbox-ai build ... --dns IP" >&2
        echo "  (SANDBOX_AI_BUILD_NO_DNS_CHECK=1 builds without this check)" >&2
        exit 1
      fi
    fi
  else
    echo "sandbox-ai build: $probe_image could not be pulled to check the build's name resolution; building anyway" >&2
  fi
fi
# Every podman build and pull from here on says what it does as it goes, bounded and readable (progress.py: its
# steps as "building the image X (step 4/12): ...", a few lines of each step's output, the time it took, the whole
# end of a failure); every other podman command runs as it is. SANDBOX_AI_PODMAN_OUTPUT=full: podman's own output.
podman_cmd=("$python" -I "$script_dir/progress.py" podman "${podman_cmd[@]}")
if [[ -n "$vpn" ]]; then
  [[ -z "$pin$uv$node$extension_only" ]] || {
    echo "sandbox-ai build: --pin, --uv, --node and --extension-only apply to the agent images, not $vpn" >&2; exit 2; }
  image=localhost/sandbox-ai-$vpn:latest
  [[ "$vpn" == vpn ]] || exec "${podman_cmd[@]}" build ${build_net[@]+"${build_net[@]}"} --pull=newer --tag "$image" --label "sandbox-ai.role=$vpn" \
    --file "$script_dir/Containerfile.$vpn" "$@" "$script_dir"
  "${podman_cmd[@]}" build ${build_net[@]+"${build_net[@]}"} --pull=newer --tag "$image" --label "sandbox-ai.role=$vpn" \
    --file "$script_dir/Containerfile.$vpn" "$@" "$script_dir"
  # wireguard-go is optional (the fallback without the host's wireguard module): say at the end whether it is in.
  wireguard_go=$("${podman_cmd[@]}" run --rm --pull=never --network=none --entrypoint=/bin/cat "$image" \
    /usr/local/share/sandbox-ai/wireguard-go 2>/dev/null) || wireguard_go=""
  case "$wireguard_go" in
    built*) echo "sandbox-ai build: $image: wireguard-go $wireguard_go" >&2 ;;
    "") echo "sandbox-ai build: $image built; whether it has wireguard-go could not be read" >&2 ;;
    *) echo "sandbox-ai build: WARNING: $image has no wireguard-go ($wireguard_go). WireGuard profiles then need" \
            "the host's kernel module (sudo modprobe wireguard; check with sandbox-ai doctor), or rebuild with" \
            "network for Go's module proxy (or --build-arg GOPROXY=URL)." >&2 ;;
  esac
  exit 0
fi
semver='^[0-9]+\.[0-9]+\.[0-9]+([.+-][0-9A-Za-z.+-]+)?$'
if [[ -n "$pin" ]]; then
  [[ ${#agents[@]} == 1 ]] || { echo "sandbox-ai build: --pin applies to one agent, not $([[ -n "$team" ]] && echo team || echo all)" >&2; exit 2; }
  [[ "$pin" =~ $semver ]] || { echo "sandbox-ai build: --pin expects X.Y.Z: $pin" >&2; exit 2; }
fi
[[ -z "$uv" || "$uv" =~ ^[0-9A-Za-z._-]+$ ]] || { echo "sandbox-ai build: --uv expects a tag: $uv" >&2; exit 2; }
[[ -z "$node" || "$node" =~ ^[A-Za-z0-9./:@_-]+$ ]] || { echo "sandbox-ai build: --node expects an image: $node" >&2; exit 2; }
if [[ "$variant_kind" == based ]]; then
  # A variant on one of your images (sandbox-ai images new NAME --base IMAGE): that image is its Node.js base.
  [[ -z "$node" ]] || { echo "sandbox-ai build: --node: the variant $variant is built on your own image" >&2; exit 2; }
  [[ -z "$podman_url" ]] || { echo "sandbox-ai build: the variant $variant is built on your own image, which is recorded on this machine: build it without --podman-url" >&2; exit 2; }
fi

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
    echo "sandbox-ai build: --pin and --extension-only apply to one agent's image, not team" >&2; exit 2; }
  for agent in claude codex opencode; do
    given=$(explicit_version "$agent" "$@")
    if [[ -n "$given" ]]; then
      version=${given#set:}
      [[ "$version" =~ $semver ]] || { echo "sandbox-ai build: ${agent^^}_VERSION expects X.Y.Z: '$version'" >&2; exit 2; }
    else
      version=$(latest_version "$(package "$agent")")
      extra+=(--build-arg "${agent^^}_VERSION=$version")
    fi
    echo "sandbox-ai build: team: $agent -> $version" >&2
    labels+=(--label "sandbox-ai.$agent-version=$version")
  done
  [[ -z "$uv" ]] || extra+=(--build-arg "UV_VERSION=$uv")
  [[ -z "$node" ]] || extra+=(--build-arg "NODE_IMAGE=$node")
  exec "${podman_cmd[@]}" build ${build_net[@]+"${build_net[@]}"} --tag localhost/team-sandbox:latest --label sandbox-ai.role=team "${labels[@]}" \
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
    echo "sandbox-ai build: unexpected version for $1: $version" >&2; exit 1; }
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
    echo "sandbox-ai build: fix the extension of $agent first (sandbox-ai images check $agent)" >&2; exit 1; }
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
    echo "sandbox-ai build: --pin $pin conflicts with --build-arg ${agent^^}_VERSION=$given" >&2; exit 2
  fi
  if [[ -n "$given_set" && ! "$given" =~ $semver ]]; then
    echo "sandbox-ai build: ${agent^^}_VERSION expects X.Y.Z: '$given'" >&2; exit 2
  fi
  if [[ -n "$pin" ]]; then
    versions[$agent]=$pin pinned[$agent]=1
  elif [[ -n "$given_set" ]]; then
    versions[$agent]=$given pinned[$agent]=given
  else
    versions[$agent]=$(latest_version "$(package "$agent")")
  fi
  echo "sandbox-ai build: $agent -> ${versions[$agent]}${pinned[$agent]:+ (pinned)}" >&2
done

status=0
for agent in "${agents[@]}"; do
  if [[ -z "$extension_only" && "$variant_kind" == based ]]; then
    # The agent's layers on your image (made ready first: images.py prep), with nothing pulled implicitly: uv's image
    # is pulled here as the other builds refresh it, the rest is local (--pull=never).
    prep=$("${images[@]}" prep "$variant" | tail -n 1) || exit 1
    "${podman_cmd[@]}" pull "ghcr.io/astral-sh/uv:${uv:-latest}" >/dev/null || {
      echo "sandbox-ai build: the uv image ghcr.io/astral-sh/uv:${uv:-latest} could not be pulled" >&2; exit 1; }
    extra=(--pull=never --build-arg "NODE_IMAGE=$prep")
    [[ "${pinned[$agent]:-}" == given ]] || extra+=(--build-arg "${agent^^}_VERSION=${versions[$agent]}")
    [[ -z "$uv" ]] || extra+=(--build-arg "UV_VERSION=$uv")
    extra+=(--label "sandbox-ai.agent-version=${versions[$agent]}")
    "${podman_cmd[@]}" build ${build_net[@]+"${build_net[@]}"} --tag "localhost/${agent}-sandbox-${variant}:base" \
      --file "$script_dir/Containerfile.$agent" "${extra[@]}" "$@" "$script_dir"
  elif [[ -z "$extension_only" ]]; then
    # Floating base tags are refreshed; pinned ones stay what they name.
    extra=(--pull=newer)
    [[ "${pinned[$agent]:-}" == given ]] || extra+=(--build-arg "${agent^^}_VERSION=${versions[$agent]}")
    [[ -z "$uv" ]] || extra+=(--build-arg "UV_VERSION=$uv")
    [[ -z "$node" ]] || extra+=(--build-arg "NODE_IMAGE=$node")
    extra+=(--label "sandbox-ai.agent-version=${versions[$agent]}")
    # Without an extension the base is the agent's image; with one, the layer below it.
    tags=(--tag "localhost/${agent}-sandbox:base")
    [[ "${kinds[$agent]}" != plain ]] || tags=(--tag "localhost/${agent}-sandbox:latest" "${tags[@]}")
    "${podman_cmd[@]}" build ${build_net[@]+"${build_net[@]}"} "${tags[@]}" --file "$script_dir/Containerfile.$agent" \
      "${extra[@]}" "$@" "$script_dir"
  fi
  layer=(layer "$agent" --variants)
  [[ -z "$variant" ]] || layer=(layer "$variant")
  if [[ -n "$variant" || -n "$extension_only" || "${kinds[$agent]}" != plain || "${variant_counts[$agent]}" != 0 ]]; then
    "${images[@]}" ${podman_url:+--podman-url "$podman_url"} "${layer[@]}" || status=1
  fi
done
exit $status
