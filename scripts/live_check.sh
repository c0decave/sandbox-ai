#!/bin/sh
# Every live check the development sandbox cannot run, in one command, on your own Linux machine with rootless
# Podman - or on your Mac (docs/LIVE_CHECK.md). Run it in a checkout of sandbox-ai, as yourself - never with root or
# sudo, never inside a sandbox:
#
#   scripts/live_check.sh                 the default checks; the report goes to live-check/REPORT-DATE.md (+ .json)
#   scripts/live_check.sh --list          what each check proves, needs and sends where
#   scripts/live_check.sh --only NAME     only that check (comma list or repeated)
#   scripts/live_check.sh --help          every option
#
# On a Mac the macOS checks run (macos-doctor, macos-devices, macos-app, macos-update) and the Linux ones are
# skipped, saying so. The app's update and rollback wants two disk images - built here (scripts/build_macos_app.sh)
# or downloaded, each with its release's SHA256SUMS and SHA256SUMS.sig beside it:
#
#   scripts/live_check.sh --dmg-old ~/Downloads/sandbox-ai-0.1.154 --dmg-new ~/Downloads/sandbox-ai-0.1.155
#   scripts/live_check.sh --app /Applications/sandbox-ai.app --only macos-app,macos-doctor
#
# Your sandbox-ai stays untouched (its own HOME, configuration and sessions in a temporary folder, removed at the
# end; on a Mac the app is put into that folder, never into /Applications, and the releases are served on 127.0.0.1);
# the exit status is 0 whatever the checks found - the report says passed, failed or skipped per check.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
for python in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$python" >/dev/null 2>&1 &&
        "$python" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
        exec "$python" "$here/live_gaps.py" "$@"
    fi
done
echo "live check: needs Python 3.10 or later (python3; on a Mac: brew install python, or python.org's)" >&2
exit 2
