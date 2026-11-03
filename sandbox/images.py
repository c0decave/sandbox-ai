#!/usr/bin/env python3
"""sandbox-ai images: the agent images on this host and what is in them.

  sandbox-ai images [--json] [--podman-url URL]        image, agent version, created, size
  sandbox-ai images packages AGENT [--json]            Debian packages and global npm packages

Build or update them with sandbox-ai build AGENT|all. Standard library only.
"""
import argparse
import json
import subprocess
import sys

AGENTS = ("claude", "codex", "opencode")
LABEL = "sandbox-ai.agent-version"


def podman(podman_url=None):
    return ["podman", *(["--remote", "--url", podman_url] if podman_url else [])]


def images(podman_url=None, run=subprocess.run):
    """[{agent, image, id, created, size, agent_version}] of the localhost/<agent>-sandbox images."""
    found = []
    for agent in AGENTS:
        name = f"localhost/{agent}-sandbox:latest"
        result = run(podman(podman_url) + ["image", "inspect", "--format", "json", name], capture_output=True, text=True,
                     timeout=60, stdin=subprocess.DEVNULL)
        if result.returncode != 0:
            found.append({"agent": agent, "image": name, "present": False})
            continue
        try:
            data = json.loads(result.stdout)[0]
        except (ValueError, IndexError, KeyError, TypeError):
            found.append({"agent": agent, "image": name, "present": False})
            continue
        labels = (data.get("Config") or {}).get("Labels") or data.get("Labels") or {}
        found.append({"agent": agent, "image": name, "present": True, "id": str(data.get("Id", ""))[:12],
                      "created": str(data.get("Created", ""))[:19], "size": int(data.get("Size") or 0),
                      "agent_version": labels.get(LABEL)})
    return found


PACKAGES_SCRIPT = ("dpkg-query -W -f '${Package}\\t${Version}\\n' 2>/dev/null | sed 's/^/deb\\t/'; "
                   "npm ls -g --depth 0 --parseable --long 2>/dev/null | sed -n 's#.*/node_modules/\\(.*\\):\\(.*\\)@\\(.*\\):.*#npm\\t\\2\\t\\3#p'; "
                   "for tool in uv gh git python3 node; do v=$($tool --version 2>/dev/null | head -1); "
                   "[ -n \"$v\" ] && printf 'tool\\t%s\\t%s\\n' \"$tool\" \"$v\"; done")


def packages(agent, podman_url=None, run=subprocess.run):
    """[(kind, name, version)] installed in AGENT's image (deb, npm, tool), read by running it offline."""
    if agent not in AGENTS:
        raise ValueError(f"agent must be one of {', '.join(AGENTS)}")
    result = run(podman(podman_url) + ["run", "--rm", "--pull=never", "--network=none", "--read-only",
                                       "--cap-drop=ALL", "--security-opt=no-new-privileges", "--entrypoint=/bin/sh",
                                       f"localhost/{agent}-sandbox:latest", "-c", PACKAGES_SCRIPT],
                 capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        raise ValueError(f"cannot read the image of {agent}: {result.stderr.strip()[-300:] or 'not built'}"
                         f" (sandbox-ai build {agent})")
    rows = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[0] in ("deb", "npm", "tool"):
            rows.append(tuple(parts))
    return rows


def main(argv, out=print, run=subprocess.run):
    parser = argparse.ArgumentParser(prog="sandbox-ai images", description=__doc__.split("\n\n")[0])
    parser.add_argument("-j", "--json", action="store_true")
    parser.add_argument("-pu", "--podman-url")
    sub = parser.add_subparsers(dest="command")
    listing = sub.add_parser("packages", help="what an image holds")
    listing.add_argument("agent", choices=AGENTS)
    listing.add_argument("-j", "--json", action="store_true", dest="json_packages")
    args = parser.parse_args(argv)
    try:
        if args.command == "packages":
            rows = packages(args.agent, args.podman_url, run)
            if args.json or args.json_packages:
                out(json.dumps([{"kind": k, "name": n, "version": v} for k, n, v in rows], indent=2))
            else:
                for kind, name, version in rows:
                    out(f"{kind:5} {name:40} {version}")
            return 0
        found = images(args.podman_url, run)
        if args.json:
            out(json.dumps(found, indent=2))
            return 0
        for image in found:
            if not image["present"]:
                out(f"{image['agent']:9} not built (sandbox-ai build {image['agent']})")
            else:
                size = f"{image['size'] / 1e9:.1f} GB"
                out(f"{image['agent']:9} {image['image']:38} {image['agent_version'] or '?':12} {image['created']}  {size}")
        return 0
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f"sandbox-ai images: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
