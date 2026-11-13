#!/usr/bin/env bash
# The installer and the packages on other distributions, each in a container of its own (what release.yml's
# e2e-appimage job does on a few, here on more and on your machine or in the sandbox-ai-dev sandbox):
#
#   tests/devtools/distro_matrix.sh [--engine podman|docker] [--appimage FILE] [--dry-run] [IMAGE...]
#
# Without --appimage: install.sh --from the checkout (read-only at /src) as an ordinary user, then sandbox-ai
# --version, presets list and a dry run of claude. With --appimage: scripts/e2e_package.sh appimage FILE
# --extract-and-run --cli-only, as CI runs it. Images default to DISTROS below; each line of the summary is
# "ok IMAGE" or "FAILED IMAGE". --dry-run prints the commands only.
#
# The images are pinned by digest: each digest is the one docker-library/repo-info recorded at least 14 days before
# it was pinned here (the repository's rule for third-party releases; repo-info revisions of 2026-08-26 to 2026-10-21,
# looked up on 2026-11-11). Fedora 42 is no longer an official image: Fedora 43 instead. Images given on the command
# line are taken as given. Inside sandbox-ai-dev the distributions' own mirrors (Fedora's, Arch's) are not on the
# allow-list: they are asked about, or add them.
set -euo pipefail

DISTROS=(
  docker.io/library/debian:13@sha256:4c25275446ca371766b8b093c696782eabce06263fccacb8d5d813b3dc44b308
  docker.io/library/debian:12@sha256:854dd8ab51d62866dbd8f5431d15ce77b12983a2a4a1e1fe43a2395dc7ef9f61
  docker.io/library/ubuntu:22.04@sha256:0a9887931b0e38af2e3487ed39e29436b0535c4f3f52e57745baaf79d1898956
  docker.io/library/ubuntu:24.04@sha256:68ee22004a9109b44973313b02ff7b1de250f1de0f5ecb628d73f579aaff33ac
  docker.io/library/fedora:43@sha256:a651ddf48ea28a06ed4e1e6519f51c9f47e7a5a138722ade87369b8fbb7e5b42
  docker.io/library/archlinux:base@sha256:52dfbd5afb23a220d5ee92dafcb4c788d96c026ed04179890c23adef4c897fa0
)
engine=podman appimage="" dry=0 images=()
while [[ $# -gt 0 ]]; do
  case $1 in
    --engine) engine=${2:?}; shift 2 ;;
    --appimage) appimage=${2:?}; shift 2 ;;
    --dry-run) dry=1; shift ;;
    -h|--help) sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) echo "distro_matrix: unknown option $1" >&2; exit 2 ;;
    *) images+=("$1"); shift ;;
  esac
done
[[ ${#images[@]} -gt 0 ]] || images=("${DISTROS[@]}")
checkout=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)

# The packages a distribution needs for the smoke (Python, Bash, the app's libraries for the AppImage).
prepare() {
  case $1 in
    *debian*|*ubuntu*) echo 'apt-get update -q >/dev/null && apt-get install -y -q --no-install-recommends ca-certificates python3 bash libegl1 libgl1 libopengl0 libxkbcommon0 libfontconfig1 libglib2.0-0 libdbus-1-3 >/dev/null' ;;
    *fedora*) echo 'dnf install -y -q python3 bash shadow-utils libglvnd-egl libglvnd-opengl libxkbcommon fontconfig glib2 dbus-libs >/dev/null' ;;
    *archlinux*) echo 'pacman -Sy --noconfirm --quiet python bash libglvnd libxkbcommon fontconfig glib2 dbus >/dev/null' ;;
    *) echo 'true' ;;
  esac
}

smoke() {
  if [[ -n $appimage ]]; then
    # The file's name quoted: it goes into su -c as one word, whatever it holds.
    printf '/src/scripts/e2e_package.sh appimage %q --extract-and-run --cli-only' "/pkg/$(basename -- "$appimage")"
  else
    echo 'cp -r /src /tmp/checkout && cd /tmp/checkout && ./install.sh --from . --no-app --no-modify-rc >/dev/null && export PATH="$HOME/.local/bin:$PATH" && sandbox-ai --version && sandbox-ai presets list >/dev/null && mkdir -p ~/w && sandbox-ai claude -n -w ~/w --no-auth >/dev/null && echo smoke-ok'
  fi
}

status=0 summary=()
for image in "${images[@]}"; do
  mounts=(-v "$checkout:/src:ro")
  [[ -n $appimage ]] && mounts+=(-v "$(cd -- "$(dirname -- "$appimage")" && pwd):/pkg:ro")
  script="set -eu; $(prepare "$image"); useradd -m tester; su tester -c $(printf '%q' "$(smoke)")"
  command=("$engine" run --rm "${mounts[@]}" "$image" bash -c "$script")
  if [[ $dry == 1 ]]; then
    printf '%q ' "${command[@]}"; echo
    continue
  fi
  if "${command[@]}"; then summary+=("ok $image"); else summary+=("FAILED $image"); status=1; fi
done
[[ $dry == 1 ]] || printf '%s\n' "${summary[@]}"
exit "$status"
