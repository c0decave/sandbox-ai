#!/usr/bin/env bash
# The key that signs sandbox-ai releases: an SSH key kept outside the repository (see RELEASING.md).
#
#   scripts/release_key.sh new [--no-passphrase] [FILE]   create an ed25519 key pair (mode 0600)
#   scripts/release_key.sh show [FILE]                    print its line for sandbox/release-signers
#
# FILE defaults to $SANDBOX_AI_RELEASE_KEY or ~/.config/sandbox-ai/release-key. `show` also takes the .pub
# of a key in ssh-agent or on a FIDO token (ssh-keygen -t ed25519-sk). Register a key with
#   scripts/release_key.sh show >> sandbox/release-signers
# and commit that; the next release must still be signed by a key the installed versions already trust.
set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
DEFAULT=${SANDBOX_AI_RELEASE_KEY:-${XDG_CONFIG_HOME:-$HOME/.config}/sandbox-ai/release-key}

die() { echo "release_key.sh: $*" >&2; exit 1; }

usage() {
  sed -n '2,10s/^# \{0,1\}//p' "${BASH_SOURCE[0]}"
}

show() {
  local file=$1 pub kind key
  pub=$file
  [[ $pub == *.pub ]] || pub="$file.pub"
  [[ -f $pub ]] || die "no public key $pub (create one with: scripts/release_key.sh new)"
  read -r kind key _ < "$pub" || true
  case "$kind" in
    ssh-ed25519|sk-ssh-ed25519@openssh.com|ecdsa-sha2-nistp256|ecdsa-sha2-nistp384|ecdsa-sha2-nistp521|\
    sk-ecdsa-sha2-nistp256@openssh.com|ssh-rsa) ;;
    *) die "$pub is not an SSH public key" ;;
  esac
  [[ $key =~ ^[A-Za-z0-9+/]+={0,2}$ ]] || die "$pub is not an SSH public key"
  printf 'release@sandbox-ai namespaces="sandbox-ai-release" %s %s\n' "$kind" "$key"
  echo "release_key.sh: $(ssh-keygen -l -f "$pub")" >&2
  if ! grep -qF -- "$key" "$ROOT/sandbox/release-signers" 2>/dev/null; then
    echo "release_key.sh: not yet in sandbox/release-signers; add it with: scripts/release_key.sh show >> sandbox/release-signers" >&2
  fi
}

new() {
  local passphrase=() file
  if [[ ${1:-} == --no-passphrase ]]; then passphrase=(-N ""); shift; fi
  file=${1:-$DEFAULT}
  [[ -e $file || -e $file.pub ]] && die "$file exists already; show it with: scripts/release_key.sh show $file"
  command -v ssh-keygen >/dev/null || die "ssh-keygen (OpenSSH) is required"
  file=$(python3 -I -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$file")
  case "$file" in
    "$(cd -- "$ROOT" && pwd -P)/"*) die "the key must live outside the repository ($ROOT)" ;;
  esac
  mkdir -p -m 700 -- "$(dirname -- "$file")"
  umask 077
  ssh-keygen -q -t ed25519 -a 100 -C "sandbox-ai release key $(date +%Y-%m-%d)" -f "$file" "${passphrase[@]}"
  chmod 600 -- "$file"
  echo "release_key.sh: created $file (keep it, and a backup, off the repository and off CI)" >&2
  show "$file"
}

case "${1:-}" in
  new) shift; new "$@" ;;
  show) shift; show "${1:-$DEFAULT}" ;;
  -h|--help|"") usage; [[ -n "${1:-}" ]] ;;
  *) usage >&2; exit 2 ;;
esac
