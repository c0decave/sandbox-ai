#!/bin/sh
# install.sh LOCK XMLDIR - the downloads of the image recipe android-dev (sandbox-ai), run once while the image is
# built: every row of LOCK (sdk.lock) is fetched over HTTPS, checked against its size, its SHA-1 (Google's own
# checksum) and its SHA-256, unpacked to its folder and, for an Android SDK package, given the package.xml from
# XMLDIR that sdkmanager would have written. Any difference stops the build before anything of that file is
# unpacked. A Google archive whose SHA-256 is not in the lock yet gets it printed ("unconfirmed SHA-256").
# Each archive is removed right after it is unpacked (one RUN: no layer keeps the archives).
set -eu

lock=$1
xmldir=$2
work=${ANDROID_DEV_WORK:-/var/tmp/android-dev-install}
prefix=${ANDROID_DEV_PREFIX:-/opt}  # (every destination lies below it; tests unpack into a folder of theirs)
say() { printf 'sandbox-ai android-dev: %s\n' "$*"; }
fail() { printf 'sandbox-ai android-dev: ERROR: %s\n' "$*" >&2; rm -rf "$work"; exit 1; }

case "$(uname -m)" in
    x86_64|amd64) ;;
    *) fail "the Android SDK's Linux tools (build-tools, the NDK, the emulator) exist for x86_64 only, not for $(uname -m)" ;;
esac

say "by building this image you accept the Android Software Development Kit License Agreement"
say "(https://developer.android.com/studio/terms): every Android SDK package below is published under it."

rm -rf "$work"
mkdir -p "$work"
unconfirmed=""
count=0
# shellcheck disable=SC2034  # (version: for people reading the lock)
while read -r package version dest size sha1 sha256 url rest; do
    case "$package" in ''|'#'*) continue ;; esac
    [ -n "$url" ] && [ -z "$rest" ] || fail "malformed line in $lock: $package $version $dest ..."
    case "$url" in https://*) ;; *) fail "$url: only https:// downloads" ;; esac
    case "$dest" in "$prefix"/*) ;; *) fail "$dest: a destination below $prefix" ;; esac
    [ "$sha1" != "-" ] || [ "$sha256" != "-" ] || fail "$url has no checksum in $lock"
    [ ! -e "$dest" ] || fail "$dest exists already"
    archive="$work/download.zip"
    say "fetching $url ($size bytes)"
    curl --fail --location --proto '=https' --proto-redir '=https' --tlsv1.2 --retry 3 --silent --show-error \
        --output "$archive" "$url" </dev/null || fail "$url: the download failed"
    got=$(wc -c <"$archive" | tr -d ' ')
    [ "$got" = "$size" ] || fail "SIZE MISMATCH for $url: the lock says $size bytes, the download has $got"
    if [ "$sha1" != "-" ]; then
        got=$(sha1sum "$archive" | cut -d ' ' -f 1)
        [ "$got" = "$sha1" ] || fail "CHECKSUM MISMATCH for $url: SHA-1 $sha1 expected, $got downloaded"
    fi
    got=$(sha256sum "$archive" | cut -d ' ' -f 1)
    if [ "$sha256" = "-" ]; then
        say "unconfirmed SHA-256 $got of $url (its size and SHA-1 match the lock)"
        unconfirmed="$unconfirmed
  $got  $url"
    elif [ "$got" != "$sha256" ]; then
        fail "CHECKSUM MISMATCH for $url: SHA-256 $sha256 expected, $got downloaded"
    fi
    # Unpacked beside it, then moved into place: most archives hold one folder (its contents are the package),
    # some hold the package's files themselves.
    rm -rf "$work/unpacked"
    mkdir "$work/unpacked"
    unzip -q "$archive" -d "$work/unpacked" </dev/null || fail "$url: unzip failed"
    rm -f "$archive"
    entries=$(ls -A "$work/unpacked" | wc -l | tr -d ' ')
    first=$(ls -A "$work/unpacked" | head -n 1)
    if [ "$entries" = 1 ] && [ -d "$work/unpacked/$first" ] && [ ! -L "$work/unpacked/$first" ]; then
        source_dir="$work/unpacked/$first"
    else
        source_dir="$work/unpacked"
    fi
    mkdir -p "$(dirname "$dest")"
    mv "$source_dir" "$dest"
    rm -rf "$work/unpacked"
    if [ "$package" != "-" ] && [ ! -f "$dest/package.xml" ]; then
        xml="$xmldir/$(printf '%s' "$package" | tr ';' '_').xml"
        [ -f "$xml" ] || fail "no package.xml for $package ($xml)"
        cp "$xml" "$dest/package.xml"
        chmod 0644 "$dest/package.xml"
    fi
    count=$((count + 1))
    say "installed $package $version in $dest"
done <"$lock"
rm -rf "$work"
[ "$count" -gt 0 ] || fail "$lock names no download"
if [ -n "$unconfirmed" ]; then
    say "unconfirmed SHA-256 (size and Google's SHA-1 matched; add these to sandbox/recipes/android-dev/sdk.lock):$unconfirmed"
fi
say "$count downloads checked and installed"
