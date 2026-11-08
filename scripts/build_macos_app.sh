#!/usr/bin/env bash
# Build sandbox-ai.app (the app with sandbox-ai inside, its own Python and PySide6) and its disk image on macOS,
# for this Mac's architecture (arm64 or x86_64).
#
#   scripts/build_macos_app.sh --bundle dist/sandbox-ai-bundle-X.Y.Z.tar.gz [--out DIR] [--downloads DIR]
#
# Writes OUT/sandbox-ai-ARCH.dmg (default OUT: dist/), with sandbox-ai.app and a link to /Applications in it.
# Contents/MacOS/sandbox-ai-app is the app (the bundle's executable), Contents/MacOS/sandbox-ai the command line.
# Every download is pinned by SHA-256 (packaging/pins.sh, packaging/requirements-app.txt). The app is not signed
# by a developer ID and not notarised (ad-hoc signature only): see README, "Install", for the first start.
# Needs: bash (3.2 is enough), curl, tar, shasum, hdiutil, iconutil, codesign (all part of macOS).
set -euo pipefail

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=../packaging/pins.sh
. "$root/packaging/pins.sh"

die() { echo "build_macos_app: $*" >&2; exit 1; }
note() { echo "build_macos_app: $*" >&2; }

bundle="" out="$root/dist" downloads="${XDG_CACHE_HOME:-$HOME/.cache}/sandbox-ai-build" keep=0
while [[ $# -gt 0 ]]; do
  case $1 in
    --bundle) bundle=${2:?}; shift 2 ;;
    --out) out=${2:?}; shift 2 ;;
    --downloads) downloads=${2:?}; shift 2 ;;
    --keep-app) keep=1; shift ;;
    -h|--help) sed -n '2,12p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) die "unknown option $1 (see --help)" ;;
  esac
done
[[ $(uname -s) == Darwin ]] || die "the macOS app is built on macOS"
[[ -f $bundle ]] || die "--bundle sandbox-ai-bundle-X.Y.Z.tar.gz is required (python3 scripts/make_dist.py builds it)"

arch=$(uname -m)
case $arch in
  arm64) pbs=$PBS_MACOS_ARM64 pbs_sha=$PBS_MACOS_ARM64_SHA256 ;;
  x86_64) pbs=$PBS_MACOS_X86_64 pbs_sha=$PBS_MACOS_X86_64_SHA256 ;;
  *) die "no macOS app for $arch" ;;
esac

sha256_of() { shasum -a 256 -- "$1" | cut -d' ' -f1; }

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

fetch() {
  local url=$1 name=$2 want=$3 file="$downloads/$2" got
  mkdir -p -- "$downloads"
  if [[ -f $file && ! -L $file ]] && [[ $(sha256_of "$file") == "$want" ]]; then
    printf '%s\n' "$file"; return 0
  fi
  rm -f -- "$file" "$file.part"
  note "downloading $url"
  curl -fsSL --proto '=https' --tlsv1.2 --retry 3 --retry-delay 2 -o "$file.part" -- "$url" \
    || github_asset "$url" "$file.part" \
    || die "download failed: $url"
  got=$(sha256_of "$file.part")
  if [[ $got != "$want" ]]; then
    rm -f -- "$file.part"
    die "$name: SHA-256 $got, pinned $want; refused"
  fi
  mv -f -- "$file.part" "$file"
  printf '%s\n' "$file"
}

work=$(mktemp -d "${TMPDIR:-/tmp}/sandbox-ai-macos.XXXXXX")
cleanup() { if [[ $keep == 0 ]]; then chmod -R u+w -- "$work" 2>/dev/null || true; rm -rf -- "$work"; fi; }
trap cleanup EXIT

app="$work/stage/sandbox-ai.app"
contents="$app/Contents"
resources="$contents/Resources"
mkdir -p -- "$contents/MacOS" "$resources"

# 1. Python (relocatable), without what sandbox-ai and the app never use.
pbs_file=$(fetch "$PBS_BASE/${pbs//+/%2B}" "$pbs" "$pbs_sha")
tar -xzf "$pbs_file" -C "$resources"
py="$resources/python/bin/python3"
[[ -x $py ]] || die "$pbs has no python/bin/python3"
stdlib="$resources/python/lib/python$PBS_PYTHON"
rm -rf -- "$stdlib/test" "$stdlib/idlelib" "$stdlib/tkinter" "$stdlib/turtledemo" "$stdlib/turtle.py" \
  "$stdlib/lib2to3" "$stdlib/ensurepip" "$stdlib"/lib-dynload/_tkinter* "$resources/python/share" \
  "$resources/python/include" "$resources/python/lib/pkgconfig" "$resources/python/lib"/tcl* \
  "$resources/python/lib"/tk* "$resources/python/lib"/itcl* "$resources/python/lib"/thread* \
  "$resources/python/lib"/libpython*.a "$resources/python/lib"/libtcl* "$resources/python/lib"/libtk* \
  "$resources/python/bin"/idle3* "$resources/python/bin"/pydoc3* "$resources/python/bin"/python*-config
"$py" -I -c 'import ssl, sqlite3, zlib, ctypes' || die "the Python of $pbs does not run"

# 2. PySide6 (universal2 wheels), by hash.
"$py" -I -m pip install --quiet --disable-pip-version-check --root-user-action ignore --no-cache-dir --require-hashes --only-binary :all: \
  --no-deps --target "$resources/pyside" -r "$root/packaging/requirements-app.txt" || die "pip could not install PySide6"
rm -rf -- "$stdlib/site-packages"/pip "$stdlib/site-packages"/pip-* "$resources/python/bin"/pip*
rm -rf -- "$resources/pyside/bin"  # pip's pyside6-* commands: never run here, and their #! lines name this build
# ... and out of pip's RECORD files, which list them with their hashes (and so with that folder).
sed -i.orig '/^\.\.\/\.\.\/bin\//d' "$resources/pyside"/*.dist-info/RECORD && rm -f -- "$resources/pyside"/*.dist-info/RECORD.orig
# What the app never loads: Qt's developer tools, QML, type stubs, headers and the tools' translations (the
# libraries and plugins stay whole: the platform plugins of every desktop need them).
ps="$resources/pyside/PySide6"
rm -rf -- "$ps"/{assistant,designer,linguist,lrelease,lupdate,qmlformat,qmllint,qmlls,svgtoqml}{,.app} "$ps"/*.app \
  "$ps"/*.pyi "$ps/include" "$ps/typesystems" "$ps/glue" "$ps/scripts" "$ps/Qt/qml" "$ps/Qt/libexec" \
  "$ps/Qt/metatypes" "$ps/Qt/plugins/designer" "$ps/Qt/plugins/qmltooling" \
  "$ps/Qt/translations"/{assistant,designer,linguist,qt_help,qtdeclarative}_*

# 3. sandbox-ai with the app, as the bundle has it.
mkdir -p -- "$work/bundle"
tar -xzf "$bundle" -C "$work/bundle"
tops=("$work/bundle"/*)
[[ ${#tops[@]} == 1 && -d ${tops[0]} ]] || die "$bundle must contain one top folder"
mv -- "${tops[0]}" "$resources/sandbox-ai"
[[ -f "$resources/sandbox-ai/projects/sandbox-ai-app/sandbox_ai_app/__main__.py" ]] \
  || die "$bundle lacks the app (projects/sandbox-ai-app): build dist/ from a checkout that has it"
version=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$resources/sandbox-ai/sandbox/agent_sandbox.py")
[[ -n $version ]] || die "no version in the bundle"
printf 'macos-app\n' > "$resources/sandbox-ai/PACKAGING"
find "$resources/sandbox-ai" -name '__pycache__' -type d -prune -exec rm -rf {} +
# Compiled here: nothing is written into the app later (unchecked-hash: valid wherever the app is copied to).
find "$stdlib" "$resources/pyside" -name '__pycache__' -type d -prune -exec rm -rf {} +  # pip's run wrote some
compile_all() {
  "$py" -I -m compileall -q "$@" --invalidation-mode unchecked-hash -x '[/\\]tests[/\\]' \
    -s "$resources" -p /Applications/sandbox-ai.app/Contents/Resources \
    "$stdlib" "$resources/pyside" "$resources/sandbox-ai/sandbox" "$resources/sandbox-ai/projects" >/dev/null 2>&1
}
compile_all -f -j 0 || compile_all -f -j 1 || note "some modules did not compile (they are compiled when loaded)"
# Every .pyc must be hash-based: one tied to a file time (pip writes those) is stale once the app is copied.
"$py" -I - "$resources" <<'PY' || die "modules not compiled hash-based (see above)"
import pathlib, sys
stale = [str(p) for p in pathlib.Path(sys.argv[1]).rglob("*.pyc") if p.read_bytes()[4:8] == bytes(4)]
print("\n".join(stale[:5]), file=sys.stderr)
sys.exit(1 if stale else 0)
PY
[[ -n $(find "$resources/sandbox-ai/sandbox/__pycache__" -name 'agent_sandbox.*.pyc' 2>/dev/null) ]] \
  || die "sandbox-ai was not compiled"

# Nothing may name this build's folder (the .pyc name /Applications/sandbox-ai.app); every file time is the
# bundle's (SOURCE_DATE_EPOCH, else the newest time in the bundle).
if leaks=$(grep -rlF -- "$work" "$resources"); then
  die "the app names the build folder $work in: $(head -n 5 <<<"$leaks")"
fi
epoch=${SOURCE_DATE_EPOCH:-$("$py" -I -c 'import sys, tarfile
print(max(m.mtime for m in tarfile.open(sys.argv[1]).getmembers()))' "$bundle")}

# 4. Launchers, Info.plist, icon.
install -m 0755 "$root/packaging/macos-app" "$contents/MacOS/sandbox-ai-app"
install -m 0755 "$root/packaging/macos-cli" "$contents/MacOS/sandbox-ai"
sed "s/@VERSION@/$version/g" "$root/packaging/Info.plist.in" > "$contents/Info.plist"
plutil -lint "$contents/Info.plist" >/dev/null || die "Info.plist is not valid"
"$py" -I "$root/scripts/render_icon.py" --pyside "$resources/pyside" --svg "$root/sandbox/web/icon.svg" \
  --iconset "$work/sandbox-ai.iconset"
iconutil -c icns -o "$resources/sandbox-ai.icns" "$work/sandbox-ai.iconset"

stamp=$(date -u -r "$epoch" +%Y%m%d%H%M.%S)  # BSD touch has no -d @EPOCH: -t in UTC instead
find "$app" -exec env TZ=UTC touch -h -t "$stamp" {} +

# 5. An ad-hoc signature (no developer ID): Apple silicon runs only signed code, and a signed bundle can be
#    opened with System Settings > Privacy & Security > Open Anyway; without it macOS calls it "damaged".
if ! codesign --force --deep --sign - "$app" 2>"$work/codesign.log"; then
  note "ad-hoc signing failed (the app still runs after: xattr -dr com.apple.quarantine sandbox-ai.app):"
  cat "$work/codesign.log" >&2
fi

# 6. The disk image: the app and a link to /Applications to drag it onto.
ln -s /Applications "$work/stage/Applications"
mkdir -p -- "$out"
target="$out/sandbox-ai-$arch.dmg"
rm -f -- "$target"
hdiutil create -quiet -volname "sandbox-ai $version" -srcfolder "$work/stage" -fs HFS+ -format UDZO -ov "$target" \
  || die "hdiutil create failed"
note "sandbox-ai $version: $target ($(du -h "$target" | cut -f1); the app: $(du -sh "$app" | cut -f1))"
