#!/usr/bin/env bash
# End-to-end check of a ready-made package, as a user would start it (the release workflow runs it for each one).
#
#   scripts/e2e_package.sh pyz FILE.pyz [--python PYTHON]
#   scripts/e2e_package.sh appimage FILE.AppImage [--extract-and-run] [--cli-only]
#   scripts/e2e_package.sh macos-app PATH/sandbox-ai.app [--cli-only]
#
# Checks: --version and version --verbose (the package kind), presets list, doctor (it may report a missing
# Podman), a dry run of claude in a fresh workspace, that `update` points to the download instead of running
# install.sh; for the .pyz and the AppImage that the copy in the cache is made once, reused, and refused once it
# was changed; for the AppImage the command line by the name sandbox-ai; for the AppImage and the macOS app the
# app's --self-test offscreen (the main window built against the sandbox-ai inside). Everything runs with private
# XDG folders below a temporary folder (WORK, default: a new one under TMPDIR).
# --extract-and-run: the AppImage without FUSE (containers). --cli-only: no app self-test.
set -euo pipefail

kind=${1:?kind: pyz, appimage or macos-app} package=${2:?the package file}
shift 2
python=python3 extract=0 cli_only=0
while [[ $# -gt 0 ]]; do
  case $1 in
    --python) python=${2:?}; shift 2 ;;
    --extract-and-run) extract=1; shift ;;
    --cli-only) cli_only=1; shift ;;
    *) echo "e2e_package: unknown option $1" >&2; exit 2 ;;
  esac
done
case $kind in pyz|appimage|macos-app) ;; *) echo "e2e_package: unknown kind $kind" >&2; exit 2 ;; esac
[[ -e $package ]] || { echo "e2e_package: no $package" >&2; exit 2; }
package=$(cd -- "$(dirname -- "$package")" && pwd)/$(basename -- "$package")

work=${WORK:-$(mktemp -d "${TMPDIR:-/tmp}/sandbox-ai-e2e.XXXXXX")}
mkdir -p -- "$work"
export XDG_CACHE_HOME="$work/cache" XDG_CONFIG_HOME="$work/config" XDG_STATE_HOME="$work/state" \
  XDG_DATA_HOME="$work/data" SANDBOX_AI_NO_DEPRECATION=1
unset SANDBOX_AI_HOME
# Extract-and-run unpacks into $TMPDIR/appimage_extracted_<hash> and reuses it: a private TMPDIR, never /tmp.
if [[ $extract == 1 ]]; then
  mkdir -p -m 0700 -- "$work/tmp"
  export APPIMAGE_EXTRACT_AND_RUN=1 TMPDIR="$work/tmp"
fi
failures=0
fail() { echo "FAIL: $*" >&2; failures=$((failures + 1)); }
pass() { echo "ok: $*"; }
now() { "$python" -c 'import time; print(time.monotonic())' 2>/dev/null || date +%s; }
elapsed() { "$python" -c 'import sys; print(f"{float(sys.argv[2]) - float(sys.argv[1]):.2f} s")' "$1" "$(now)" 2>/dev/null || true; }

cli() {
  case $kind in
    pyz) "$python" "$package" "$@" ;;
    appimage) "$package" cli "$@" ;;
    macos-app) "$package/Contents/MacOS/sandbox-ai" "$@" ;;
  esac
}
app() {
  case $kind in
    appimage) "$package" "$@" ;;
    macos-app) "$package/Contents/MacOS/sandbox-ai-app" "$@" ;;
  esac
}

# 1. Version: the first start (the copy is made), then a second one (reused).
start=$(now)
if out=$(cli --version 2>&1) && [[ $out == "sandbox-ai "* ]]; then pass "first start: $out ($(elapsed "$start"))"
else fail "cli --version: $out"; fi
start=$(now)
if out=$(cli --version 2>&1) && [[ $out == "sandbox-ai "* ]]; then pass "second start ($(elapsed "$start"))"
else fail "cli --version (second): $out"; fi
if out=$(cli version --verbose 2>&1) && grep -q "^package: $kind" <<<"$out"; then pass "version --verbose names the package"
else fail "version --verbose: $out"; fi

# 2. Commands that need no container engine.
if out=$(cli presets list 2>&1) && [[ $(wc -l <<<"$out") -gt 3 ]]; then pass "presets list ($(wc -l <<<"$out") lines)"
else fail "presets list: $out"; fi
out=$(cli doctor 2>&1) && status=0 || status=$?
echo "doctor (exit $status, a missing Podman or Bubblewrap is allowed here):"
tail -n 8 <<<"$out" | sed 's/^/  /'
grep -qi "traceback" <<<"$out" && fail "doctor crashed"
mkdir -p -- "$work/ws"
start=$(now)
if out=$(cli claude --dry-run --no-config -w "$work/ws" 2>&1) && grep -q '"agent": "claude"' <<<"$out" \
   && grep -qF "\"workspace\": \"$work/ws\"" <<<"$out"; then pass "claude --dry-run ($(elapsed "$start"))"
else fail "claude --dry-run: $(tail -n 20 <<<"$out")"; fi
if out=$(cli update 2>&1); then fail "update ran: $out"
elif grep -q "download the new one" <<<"$out"; then pass "update points to the download"
else fail "update: $out"; fi

# 3. The copy in the cache: one, reused, refused when changed (the .pyz and the AppImage).
if [[ $kind != macos-app ]]; then
  copies=("$XDG_CACHE_HOME/sandbox-ai/$kind"/*)
  if [[ ${#copies[@]} == 1 && -f ${copies[0]}/.sandbox-ai-extract.json ]]; then pass "one copy: ${copies[0]##*/}"
  else fail "copies in the cache: ${copies[*]}"; fi
  copy=${copies[0]}
  [[ $(stat -c %a "$XDG_CACHE_HOME/sandbox-ai/$kind" 2>/dev/null || stat -f %Lp "$XDG_CACHE_HOME/sandbox-ai/$kind") == 700 ]] \
    && pass "the cache folder is private" || fail "the cache folder is not 0700"
  main_py=$(find "$copy" -path '*/sandbox/main.py' | head -n 1)
  if [[ -w $main_py ]]; then fail "$main_py is writable"; else pass "the copy is read-only"; fi
  chmod u+w -- "$(dirname -- "$main_py")" "$main_py"
  printf '\n# changed\n' >> "$main_py"
  if out=$(cli --version 2>&1); then fail "a changed copy was run: $out"
  elif grep -q "refused, nothing run" <<<"$out"; then pass "a changed copy is refused"
  else fail "changed copy: $out"; fi
  chmod -R u+w -- "$copy" && rm -rf -- "$copy"
  if out=$(cli --version 2>&1); then pass "extracted again after removing it"; else fail "after removal: $out"; fi
fi

# 4. The AppImage by the name sandbox-ai (a link), and the app itself; nothing in it names the builder's
#    temporary folder (it is built to be reproducible).
if [[ $kind == appimage ]]; then
  mkdir -p -m 0700 -- "$work/extract"
  if (cd -- "$work/extract" && "$package" --appimage-extract >/dev/null 2>&1); then
    leaks=$(grep -rlE 'sandbox-ai-appimage\.[A-Za-z0-9]{6}/' "$work/extract/squashfs-root" | head -n 5 || true)
    if [[ -z $leaks ]]; then pass "no build path inside"; else fail "build path inside: $leaks"; fi
  else
    fail "--appimage-extract"
  fi
  chmod -R u+w -- "$work/extract" && rm -rf -- "$work/extract"
  mkdir -p -- "$work/bin"
  ln -sf -- "$package" "$work/bin/sandbox-ai"
  if out=$("$work/bin/sandbox-ai" --version 2>&1) && [[ $out == "sandbox-ai "* ]]; then pass "called as sandbox-ai: $out"
  else fail "called as sandbox-ai: $out"; fi
fi
if [[ $kind != pyz && $cli_only == 0 ]]; then
  start=$(now)
  if out=$(QT_QPA_PLATFORM=offscreen app --self-test 2>&1) && grep -q "self-test: ok" <<<"$out"; then
    pass "app --self-test ($(elapsed "$start")): $(grep 'self-test: ok' <<<"$out")"
  else fail "app --self-test: $(tail -n 30 <<<"$out")"; fi
fi

[[ -z ${WORK:-} ]] && { chmod -R u+w -- "$work" 2>/dev/null || true; rm -rf -- "$work"; }
if [[ $failures -gt 0 ]]; then echo "e2e_package: $failures check(s) failed" >&2; exit 1; fi
echo "e2e_package: $kind: all checks passed"
