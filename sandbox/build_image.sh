#!/usr/bin/env bash
set -euo pipefail
script_path=$(readlink -f -- "${BASH_SOURCE[0]}")
script_dir=$(dirname -- "$script_path")
usage='Usage: sandbox-build-image [--podman-url URL] [--pin VERSION] [--uv VERSION] [--node IMAGE]
                           codex|claude|opencode|all [podman build options]

Every build uses the newest releases unless you pin them: the newest npm release
of the agent (looked up first, so the cached install layer is rebuilt), the
newest uv image and the current Node.js LTS image (--pull=newer). No image is
built if a lookup fails.

  --pin VERSION    install exactly this agent version (one agent only); also
                   --build-arg <AGENT>_VERSION=VERSION
  --uv VERSION     use this uv image tag instead of latest
  --node IMAGE     use this Node.js base image, e.g. a tag or an @sha256 digest

The image is labelled with the installed agent version (sandbox-ai.agent-version).'
die_usage() { echo "$usage" >&2; exit 2; }

podman_cmd=(podman) pin="" uv="" node="" agents=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --version)
      version=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$script_dir/agent_sandbox.py")
      echo "sandbox-ai $version"; exit 0 ;;
    -h|--help) echo "$usage"; exit 0 ;;
    --podman-url) [[ $# -ge 2 && -n "$2" ]] || die_usage; podman_cmd+=(--remote --url "$2"); shift 2 ;;
    --pin) [[ $# -ge 2 && -n "$2" ]] || die_usage; pin=$2; shift 2 ;;
    --uv) [[ $# -ge 2 && -n "$2" ]] || die_usage; uv=$2; shift 2 ;;
    --node) [[ $# -ge 2 && -n "$2" ]] || die_usage; node=$2; shift 2 ;;
    codex|claude|opencode) agents=("$1"); shift; break ;;
    all) agents=(codex claude opencode); shift; break ;;
    *) die_usage ;;
  esac
done
[[ ${#agents[@]} -gt 0 ]] || die_usage
semver='^[0-9]+\.[0-9]+\.[0-9]+([.+-][0-9A-Za-z.+-]+)?$'
if [[ -n "$pin" ]]; then
  [[ ${#agents[@]} == 1 ]] || { echo "sandbox-build-image: --pin applies to one agent, not all" >&2; exit 2; }
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

# Resolve every version before building anything.
declare -A versions=() pinned=()
for agent in "${agents[@]}"; do
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

for agent in "${agents[@]}"; do
  # Floating base tags are refreshed; pinned ones stay what they name.
  extra=(--pull=newer)
  [[ "${pinned[$agent]:-}" == given ]] || extra+=(--build-arg "${agent^^}_VERSION=${versions[$agent]}")
  [[ -z "$uv" ]] || extra+=(--build-arg "UV_VERSION=$uv")
  [[ -z "$node" ]] || extra+=(--build-arg "NODE_IMAGE=$node")
  extra+=(--label "sandbox-ai.agent-version=${versions[$agent]}")
  "${podman_cmd[@]}" build --tag "localhost/${agent}-sandbox:latest" --file "$script_dir/Containerfile.$agent" \
    "${extra[@]}" "$@" "$script_dir"
done
