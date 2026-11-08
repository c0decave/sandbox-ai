#!/usr/bin/env bash
# Build the sandbox-ai AppImage for this machine's architecture (x86_64 or aarch64): the app (sandbox-ai-app)
# with sandbox-ai inside, its own Python (python-build-standalone) and PySide6.
#
#   scripts/build_appimage.sh --bundle dist/sandbox-ai-bundle-X.Y.Z.tar.gz [--out DIR] [--downloads DIR]
#
# The bundle is the one make_dist.py builds (with projects/sandbox-ai-app in it), so the AppImage carries exactly
# what install.sh would install. Every download is pinned (packaging/pins.sh, packaging/requirements-app.txt) and
# checked by SHA-256 before it is used; downloads are kept in --downloads (default:
# ${XDG_CACHE_HOME:-~/.cache}/sandbox-ai-build) and checked again on reuse.
#
# Writes OUT/sandbox-ai-ARCH.AppImage (default OUT: dist/). Needs: bash, curl, tar, sha256sum, file (appimagetool
# calls it); no FUSE (appimagetool runs with APPIMAGE_EXTRACT_AND_RUN=1).
set -euo pipefail

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=../packaging/pins.sh
. "$root/packaging/pins.sh"

die() { echo "build_appimage: $*" >&2; exit 1; }
note() { echo "build_appimage: $*" >&2; }

bundle="" out="$root/dist" downloads="${XDG_CACHE_HOME:-$HOME/.cache}/sandbox-ai-build" keep=0
while [[ $# -gt 0 ]]; do
  case $1 in
    --bundle) bundle=${2:?}; shift 2 ;;
    --out) out=${2:?}; shift 2 ;;
    --downloads) downloads=${2:?}; shift 2 ;;
    --keep-appdir) keep=1; shift ;;
    -h|--help) sed -n '2,13p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) die "unknown option $1 (see --help)" ;;
  esac
done
[[ -f $bundle ]] || die "--bundle sandbox-ai-bundle-X.Y.Z.tar.gz is required (python3 scripts/make_dist.py builds it)"

arch=$(uname -m)
case $arch in
  x86_64) pbs=$PBS_LINUX_X86_64 pbs_sha=$PBS_LINUX_X86_64_SHA256
          tool=$APPIMAGETOOL_X86_64 tool_sha=$APPIMAGETOOL_X86_64_SHA256
          runtime=$RUNTIME_X86_64 runtime_sha=$RUNTIME_X86_64_SHA256 ;;
  aarch64|arm64) arch=aarch64
          pbs=$PBS_LINUX_AARCH64 pbs_sha=$PBS_LINUX_AARCH64_SHA256
          tool=$APPIMAGETOOL_AARCH64 tool_sha=$APPIMAGETOOL_AARCH64_SHA256
          runtime=$RUNTIME_AARCH64 runtime_sha=$RUNTIME_AARCH64_SHA256 ;;
  *) die "no AppImage for $arch (x86_64 and aarch64 only)" ;;
esac
[[ $(uname -s) == Linux ]] || die "AppImages are built on Linux"
command -v file >/dev/null || die "appimagetool needs the file command (Debian/Ubuntu: apt install file)"

sha256_of() { sha256sum -- "$1" | cut -d' ' -f1; }

# A release file of github.com through its API, when GitHub's /releases/download/ path fails (it answered 504 to
# every download for hours on 2026-11-08, on CI runners as elsewhere): the same file by another route, and the
# SHA-256 pin alone decides, as for any download. GITHUB_TOKEN (the workflow's read token) only lifts the API's
# rate limit; it never goes on to the download host the API redirects to.
github_asset() {
  local url=$1 out=$2 owner repo tag name id
  [[ $url =~ ^https://github\.com/([^/]+)/([^/]+)/releases/download/([^/]+)/([^/]+)$ ]] || return 1
  owner=${BASH_REMATCH[1]} repo=${BASH_REMATCH[2]} tag=${BASH_REMATCH[3]} name=${BASH_REMATCH[4]}
  name=$(python3 -I -c 'import sys, urllib.parse; print(urllib.parse.unquote(sys.argv[1]))' "$name") || return 1
  local auth=()
  [[ -n ${GITHUB_TOKEN:-} ]] && auth=(-H "Authorization: Bearer $GITHUB_TOKEN")
  id=$(curl -fsSL --proto '=https' --tlsv1.2 --retry 3 --retry-delay 2 -H "Accept: application/vnd.github+json" \
         ${auth[@]+"${auth[@]}"} -- "https://api.github.com/repos/$owner/$repo/releases/tags/$tag" \
       | python3 -I -c 'import json, sys
print(next((str(a["id"]) for a in json.load(sys.stdin).get("assets", []) if a.get("name") == sys.argv[1]), ""))' \
         "$name") || return 1
  [[ $id =~ ^[0-9]+$ ]] || return 1
  note "downloading $name through the GitHub API (asset $id)"
  curl -fsSL --proto '=https' --tlsv1.2 --retry 3 --retry-delay 2 -H "Accept: application/octet-stream" \
    ${auth[@]+"${auth[@]}"} -o "$out" -- "https://api.github.com/repos/$owner/$repo/releases/assets/$id"
}

# fetch URL NAME SHA256: $downloads/NAME, downloaded once (into a .part file, renamed when its SHA-256 matches).
fetch() {
  local url=$1 name=$2 want=$3 file="$downloads/$2"
  mkdir -p -- "$downloads"
  if [[ -f $file && ! -L $file ]] && [[ $(sha256_of "$file") == "$want" ]]; then
    printf '%s\n' "$file"; return 0
  fi
  rm -f -- "$file" "$file.part"
  note "downloading $url"
  curl -fsSL --proto '=https' --tlsv1.2 --retry 3 --retry-delay 2 -o "$file.part" -- "$url" \
    || github_asset "$url" "$file.part" \
    || die "download failed: $url"
  local got
  got=$(sha256_of "$file.part")
  if [[ $got != "$want" ]]; then
    rm -f -- "$file.part"
    die "$name: SHA-256 $got, pinned $want; refused"
  fi
  mv -f -- "$file.part" "$file"
  printf '%s\n' "$file"
}

work=$(mktemp -d "${TMPDIR:-/tmp}/sandbox-ai-appimage.XXXXXX")
cleanup() { if [[ $keep == 0 ]]; then chmod -R u+w -- "$work" 2>/dev/null || true; rm -rf -- "$work"; fi; }
trap cleanup EXIT

appdir="$work/AppDir"
lib="$appdir/usr/lib/sandbox-ai"
tree="$lib/tree"
mkdir -p -- "$tree"

# 1. Python (relocatable), without what sandbox-ai and the app never use.
pbs_file=$(fetch "$PBS_BASE/${pbs//+/%2B}" "$pbs" "$pbs_sha")
tar -xzf "$pbs_file" -C "$tree" --no-same-owner
py="$tree/python/bin/python3"
[[ -x $py ]] || die "$pbs has no python/bin/python3"
stdlib="$tree/python/lib/python$PBS_PYTHON"
# bin/python3.13 is linked statically (checked below): the libpython for embedding goes, as do Tcl/Tk, IDLE, pydoc.
rm -rf -- "$stdlib/test" "$stdlib/idlelib" "$stdlib/tkinter" "$stdlib/turtledemo" "$stdlib/turtle.py" \
  "$stdlib/lib2to3" "$stdlib/ensurepip" "$stdlib"/lib-dynload/_tkinter* "$tree/python/share" \
  "$tree/python/include" "$tree/python/lib/pkgconfig" "$tree/python/lib"/tcl* "$tree/python/lib"/tk* \
  "$tree/python/lib"/itcl* "$tree/python/lib"/thread* "$tree/python/lib"/libpython* "$tree/python/lib"/libtcl* \
  "$tree/python/lib"/libtk* "$tree/python/bin"/idle3* "$tree/python/bin"/pydoc3* "$tree/python/bin"/python*-config
"$py" -I -c 'import ssl, sqlite3, zlib, ctypes' || die "the Python of $pbs does not run without its libpython"

# 2. PySide6 (QtWidgets, QtGui, QtSvg, QtNetwork ...), from PyPI by hash, beside the Python: only the app loads it,
#    so it stays in the AppImage and is not copied into the cache.
"$py" -I -m pip install --quiet --disable-pip-version-check --root-user-action ignore --no-cache-dir --require-hashes --only-binary :all: \
  --no-deps --target "$lib/pyside" -r "$root/packaging/requirements-app.txt" || die "pip could not install PySide6"
rm -rf -- "$stdlib/site-packages"/pip "$stdlib/site-packages"/pip-* "$tree/python/bin"/pip*
rm -rf -- "$lib/pyside/bin"  # pip's pyside6-* commands: never run here, and their #! lines name this build's folder
# ... and out of pip's RECORD files, which list them with their hashes (and so with that folder).
sed -i.orig '/^\.\.\/\.\.\/bin\//d' "$lib/pyside"/*.dist-info/RECORD && rm -f -- "$lib/pyside"/*.dist-info/RECORD.orig
# What the app never loads: Qt's developer tools, QML, type stubs, headers and the tools' translations (the
# libraries and plugins stay whole: the platform plugins of every desktop need them).
ps="$lib/pyside/PySide6"
rm -rf -- "$ps"/{assistant,designer,linguist,lrelease,lupdate,qmlformat,qmllint,qmlls,svgtoqml}{,.app} "$ps"/*.app \
  "$ps"/*.pyi "$ps/include" "$ps/typesystems" "$ps/glue" "$ps/scripts" "$ps/Qt/qml" "$ps/Qt/libexec" \
  "$ps/Qt/metatypes" "$ps/Qt/plugins/designer" "$ps/Qt/plugins/qmltooling" \
  "$ps/Qt/translations"/{assistant,designer,linguist,qt_help,qtdeclarative}_*

# 3. sandbox-ai with the app, as the bundle has it.
mkdir -p -- "$work/bundle"
tar -xzf "$bundle" -C "$work/bundle" --no-same-owner
tops=("$work/bundle"/*)
[[ ${#tops[@]} == 1 && -d ${tops[0]} ]] || die "$bundle must contain one top folder"
mv -- "${tops[0]}" "$tree/sandbox-ai"
[[ -f "$tree/sandbox-ai/projects/sandbox-ai-app/sandbox_ai_app/__main__.py" ]] \
  || die "$bundle lacks the app (projects/sandbox-ai-app): build dist/ from a checkout that has it"
version=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$tree/sandbox-ai/sandbox/agent_sandbox.py")
[[ -n $version ]] || die "no version in the bundle"
printf 'appimage\n' > "$tree/sandbox-ai/PACKAGING"
find "$tree/sandbox-ai" -name '__pycache__' -type d -prune -exec rm -rf {} +

# Compiled once here, valid wherever the copy lands (unchecked-hash: not tied to file times; the start's check of
# the copy covers changes). Tracebacks name /sandbox-ai.AppImage/...; Python still shows the source lines.
# In parallel first; what that leaves (its worker start can fail, e.g. a TMPDIR too long for a socket) one by one.
# PySide6 too: pip wrote file-time .pyc there that name this build's folder.
find "$stdlib" "$lib/pyside" -name '__pycache__' -type d -prune -exec rm -rf {} +  # pip's run wrote some
compile_all() {
  "$py" -I -m compileall -q "$@" --invalidation-mode unchecked-hash -x '[/\\]tests[/\\]' \
    -s "$appdir" -p /sandbox-ai.AppImage "$stdlib" "$lib/pyside" "$tree/sandbox-ai/sandbox" \
    "$tree/sandbox-ai/projects" >/dev/null 2>&1
}
compile_all -f -j 0 || compile_all -f -j 1 || note "some modules did not compile (they are compiled when loaded)"
# Every .pyc must be hash-based: one tied to a file time (written by pip's own run, say) is stale in the copy.
"$py" -I - "$lib" <<'PY' || die "modules not compiled hash-based (see above)"
import pathlib, sys
stale = [str(p) for p in pathlib.Path(sys.argv[1]).rglob("*.pyc") if p.read_bytes()[4:8] == bytes(4)]
print("\n".join(stale[:5]), file=sys.stderr)
sys.exit(1 if stale else 0)
PY
[[ -n $(find "$tree/sandbox-ai/sandbox/__pycache__" -name 'agent_sandbox.*.pyc' 2>/dev/null) ]] \
  || die "sandbox-ai was not compiled"

# 4. The start: boot.py and its manifest (every file of tree/ with its SHA-256), AppRun, menu entry, icon.
install -m 0644 "$root/scripts/dist_boot.py" "$lib/boot.py"
"$py" -I "$root/scripts/make_pyz.py" --manifest --tree "$tree" --kind appimage --home sandbox-ai \
  --python python/bin/python3 --out "$lib/manifest.json"
install -m 0755 "$root/packaging/AppRun" "$appdir/AppRun"
install -m 0644 "$root/packaging/sandbox-ai.desktop" "$appdir/sandbox-ai.desktop"
install -m 0644 "$root/sandbox/web/icon.svg" "$appdir/sandbox-ai.svg"
install -D -m 0644 "$root/packaging/sandbox-ai.desktop" "$appdir/usr/share/applications/sandbox-ai.desktop"
install -D -m 0644 "$root/sandbox/web/icon.svg" "$appdir/usr/share/icons/hicolor/scalable/apps/sandbox-ai.svg"
ln -s sandbox-ai.svg "$appdir/.DirIcon"

# Reproducible: nothing may name this build's folder, every file time is the bundle's (SOURCE_DATE_EPOCH, else the
# newest time in the bundle), and mksquashfs takes the same time for the image.
if leaks=$(grep -rlF -- "$work" "$appdir"); then
  die "the AppDir names the build folder $work in: $(head -n 5 <<<"$leaks")"
fi
epoch=${SOURCE_DATE_EPOCH:-$("$py" -I -c 'import sys, tarfile
print(max(m.mtime for m in tarfile.open(sys.argv[1]).getmembers()))' "$bundle")}
find "$appdir" -exec touch -h -d "@$epoch" {} +
export SOURCE_DATE_EPOCH=$epoch

# 5. The AppImage: the pinned appimagetool with the pinned runtime (it would fetch "continuous" otherwise),
#    extracted and run in the private work folder (never a shared /tmp: the runtime reuses what it finds there).
tool_file=$(fetch "$APPIMAGETOOL_BASE/$tool" "$tool" "$tool_sha")
runtime_file=$(fetch "$RUNTIME_BASE/$runtime" "$runtime" "$runtime_sha")
chmod 0755 "$tool_file"
mkdir -p -- "$out"
target="$out/sandbox-ai-$arch.AppImage"
rm -f -- "$target"
note "packing $target"
TMPDIR="$work" APPIMAGE_EXTRACT_AND_RUN=1 ARCH=$arch NO_APPSTREAM=1 "$tool_file" --no-appstream --runtime-file "$runtime_file" \
  "$appdir" "$target" >"$work/appimagetool.log" 2>&1 || { cat "$work/appimagetool.log" >&2; die "appimagetool failed"; }
chmod 0755 "$target"
note "sandbox-ai $version: $target ($(du -h "$target" | cut -f1); the copy in the cache: $(du -sh "$tree" | cut -f1))"
