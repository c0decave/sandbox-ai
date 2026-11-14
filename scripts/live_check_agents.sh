#!/bin/sh
# Live check of the agents and parts that have not been live-tested before (Hermes, Qwen Code, DSH, Goose, Cairn and
# the sandbox's own browser), on this host with your own Podman. Run it on the host, in a checkout of sandbox-ai, not
# inside a sandbox:
#
#   scripts/live_check_agents.sh                  build their images, then check all of them
#   scripts/live_check_agents.sh hermes qwen      only these (hermes, qwen, dsh, goose, cairn, browser)
#   scripts/live_check_agents.sh --no-build ...   use the images you have (each check that lacks one says so)
#
# Each agent starts in a real sandbox against a scripted model server on this host's loopback (no provider, no key,
# nothing leaves this machine for a model), answers one prompt, calls a tool of sandbox-ai's demo MCP server through
# the bridge, and must reach no telemetry host; each of its switches must open exactly its hosts. Cairn: its server on
# the sandbox's loopback and one task of its dispatcher in local mode. Your configuration stays untouched (each check
# has its own). The report - ~/.local/state/sandbox-ai/live-check/DATE-TIME/report.md, its path printed at the end -
# is the short text to send back. Building every image takes a while (and the network for their packages).
# The same as: scripts/live_check.py --agents NAME,... --build (scripts/live_check.py --help).
set -eu
here=$(cd "$(dirname "$0")" && pwd)
build=--build
if [ "${1:-}" = "--no-build" ]; then
    build=
    shift
fi
agents=$(printf '%s,' "$@")
agents=${agents%,}
exec python3 "$here/live_check.py" --agents "${agents:-all}" $build
