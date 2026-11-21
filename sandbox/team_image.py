#!/usr/bin/env python3
"""The team image (--with AGENT): the agents of one start in one image.

  localhost/team-sandbox:latest           Claude Code, Codex and OpenCode (Containerfile.team: sandbox-ai build team)
  localhost/team-A-B-sandbox:latest       that image with the agents A and B as layers on it
                                          (sandbox-ai build team --with A,B)

The other agents (Hermes, DSH, Qwen Code, Goose, OpenHands, nanobot, Letta Code, OpenCode 2, Pi) come as layers on
the base, each made from its own Containerfile: what that file installs after the common base (the same reviewed
release, the same lock file, the same checks and the same telemetry guard), never a copy that could drift. An image
holds exactly the layers it was asked for, in one order (LAYERS: sorted), so images that begin with the same agents
share those layers on disk, and every one shares the base: `sandbox-ai build team` builds the base again and then
the team images with layers that are there, on it.

Few changes are made to a layer (each must apply exactly once, or the build stops):
- an ENV of uv's own variables (UV_*) that only the next step needs becomes an export of that step: they would
  otherwise hold for the other agents' uv too (Claude Code's MCP servers through uvx: no cache, no configuration);
- OpenCode 2 is installed as opencode2 only: opencode stays OpenCode 1 (its command, its guard);
- a package the agent's base has beyond the team's (Goose: bzip2) is installed first.

    python3 team_image.py containerfile AGENT...   the Containerfile of the image with these agents
    python3 team_image.py image AGENT...           its name
    python3 team_image.py layers LIST              the layer agents of LIST (a,b,c, "all"), in their order
    python3 team_image.py layers-of IMAGE          the layer agents of the team image IMAGE (none: the base)
    python3 team_image.py versions AGENT...        "AGENT VERSION" of each layer agent (its reviewed release)

Standard library only.
"""
from pathlib import Path
import re
import sys

HERE = Path(__file__).resolve().parent
BASE_AGENTS = ("claude", "codex", "opencode")  # Containerfile.team
LAYERS = ("dsh", "goose", "hermes", "letta", "nanobot", "opencode2", "openhands", "pi", "qwen")  # their order
AGENTS = (*BASE_AGENTS, *LAYERS)
BASE_IMAGE = "localhost/team-sandbox:latest"
NAMES = {"claude": "Claude Code", "codex": "Codex", "opencode": "OpenCode", "dsh": "DeepSeek Harness",
         "goose": "Goose", "hermes": "Hermes", "letta": "Letta Code", "nanobot": "nanobot", "opencode2": "OpenCode 2",
         "openhands": "OpenHands", "pi": "Pi", "qwen": "Qwen Code"}
IMAGE = re.compile(r"(?:localhost/)?team-((?:[a-z0-9]+-)*?[a-z0-9]+)-sandbox(?::latest)?")
BODY_START = "RUN python3 -m pip --version && uv --version"  # the end of the common base in every agent's file
BODY_END = "ENV HOME=/root"
# The changes a layer needs in a team image: (old, new), each found exactly once in the agent's own file.
CHANGES = {
    "opencode2": [
        # OpenCode 1 is `opencode` here: OpenCode 2 only under its second name.
        ("    && install -m 0755 package/bin/opencode /usr/local/bin/opencode \\\n"
         "    && ln -s opencode /usr/local/bin/opencode2 \\\n",
         "    && install -m 0755 package/bin/opencode /usr/local/bin/opencode2 \\\n"),
        ("RUN HOME=/tmp/opencode2-check opencode2 --version && HOME=/tmp/opencode2-check opencode --version \\\n",
         "RUN HOME=/tmp/opencode2-check opencode2 --version \\\n"),
    ],
}


def layers(agents):
    """The layer agents of AGENTS (any of AGENTS), each once, in LAYERS' order. ValueError names an unknown one."""
    agents = list(agents)
    unknown = [agent for agent in agents if agent not in AGENTS]
    if unknown:
        raise ValueError(f"{unknown[0]} is no agent of the team image ({', '.join(AGENTS)})")
    return [agent for agent in LAYERS if agent in agents]


def parse(values):
    """The agents of --with VALUE[,VALUE...] (repeatable; "all": every layer), as layers() orders them."""
    names = [name.strip() for value in values for name in str(value).split(",") if name.strip()]
    if not names:
        raise ValueError(f"--with: one or more of {', '.join(LAYERS)}, or all")
    return list(LAYERS) if "all" in names else layers(names)


def image(agents):
    """The team image that holds AGENTS: the base, or the base with their layers."""
    extra = layers(agents)
    return BASE_IMAGE if not extra else f"localhost/team-{'-'.join(extra)}-sandbox:latest"


def agents_of(name):
    """The agents a team image NAME holds (the base's, then its layers), or None for another image (or a name no
    build gives: layers out of order, twice, unknown)."""
    name = str(name)
    if name in (BASE_IMAGE, "localhost/team-sandbox", "team-sandbox", "team-sandbox:latest"):
        return list(BASE_AGENTS)
    match = IMAGE.fullmatch(name)
    if not match:
        return None
    extra = match.group(1).split("-")
    try:
        if layers(extra) != extra:
            return None
    except ValueError:
        return None
    return [*BASE_AGENTS, *extra]


def build_command(agents):
    """How the team image of AGENTS is built."""
    extra = layers(agents)
    return "sandbox-ai build team" + (f" --with {','.join(extra)}" if extra else "")


def containerfile_of(agent, here=HERE):
    return (Path(here) / f"Containerfile.{agent}").read_text()


def stage(text):
    """The agent's own stage of its Containerfile (after its last FROM)."""
    lines = text.splitlines(keepends=True)
    starts = [index for index, line in enumerate(lines) if line.startswith("FROM ")]
    if not starts:
        raise ValueError("no FROM")
    return lines[starts[-1] + 1:]


def apt_packages(lines):
    """The packages the apt-get install steps of LINES install."""
    found, inside = [], False
    for line in lines:
        text = line.strip()
        if text.startswith("RUN apt-get update && apt-get install"):
            inside = True
            continue
        if inside:
            words = text.rstrip("\\").split()
            if words[:1] == ["&&"]:
                inside = False
                continue
            found += [word for word in words if not word.startswith("-")]
            if not text.endswith("\\"):
                inside = False
    return found


def base_lines(lines):
    """LINES up to the end of the common base (BODY_START)."""
    marks = [index for index, line in enumerate(lines) if line.rstrip("\n") == BODY_START]
    if len(marks) != 1:
        raise ValueError(f"the end of the common base ({BODY_START}) is not there once")
    return lines[:marks[0] + 1], lines[marks[0] + 1:]


def fold_uv_env(text):
    """ENV lines of uv's own variables (UV_*) followed by a RUN: an export at the start of that RUN instead."""
    lines, out, index = text.splitlines(keepends=True), [], 0
    while index < len(lines):
        line = lines[index]
        words = line.split()
        if words[:1] == ["ENV"] and len(words) > 1 and not line.rstrip().endswith("\\") \
                and all(re.fullmatch(r"UV_[A-Z_]+=\S+", word) for word in words[1:]) \
                and index + 1 < len(lines) and lines[index + 1].startswith("RUN "):
            out.append(f"RUN export {' '.join(words[1:])} \\\n    && {lines[index + 1][len('RUN '):]}")
            index += 2
            continue
        out.append(line)
        index += 1
    return "".join(out)


def layer(agent, here=HERE, base_text=None):
    """The text of AGENT's layer on the team image: its ARGs (the reviewed release), the packages its base has
    beyond the team's, what its own Containerfile installs after the common base (CHANGES applied, uv's variables
    folded)."""
    if agent not in LAYERS:
        raise ValueError(f"{agent} is no layer of the team image ({', '.join(LAYERS)})")
    lines = stage(containerfile_of(agent, here))
    base, body = base_lines(lines)
    ends = [index for index, line in enumerate(body) if line.rstrip("\n") == BODY_END]
    if not ends:
        raise ValueError(f"Containerfile.{agent}: no {BODY_END}")
    body = "".join(body[:ends[-1]])
    for old, new in CHANGES.get(agent, []):
        if body.count(old) != 1:
            raise ValueError(f"Containerfile.{agent} changed: a change for the team image no longer applies "
                             f"({old.strip()[:60]}...); update team_image.CHANGES")
        body = body.replace(old, new)
    body = fold_uv_env(body)
    args = [line for line in base if line.startswith("ARG ")]
    team_base = stage(base_text if base_text is not None else (Path(here) / "Containerfile.team").read_text())
    have = set(apt_packages(team_base))
    extra = [package for package in dict.fromkeys(apt_packages(base)) if package not in have]
    head = [f"# --- {NAMES[agent]}: what Containerfile.{agent} installs after the common base (its reviewed "
            "release, its lock) ---\n", *args]
    if extra:
        head.append(f"RUN apt-get update && apt-get install -y --no-install-recommends {' '.join(extra)} \\\n"
                    "    && rm -rf /var/lib/apt/lists/*\n")
    return "".join(head) + body


def containerfile(agents, here=HERE):
    """The Containerfile of the team image with AGENTS: on the base (TEAM_BASE, built first), each layer in LAYERS'
    order, then a check that the base's agents still answer."""
    extra = layers(agents)
    if not extra:
        raise ValueError("the base team image is Containerfile.team")
    parts = ["# sandbox-ai's team image with " + ", ".join(NAMES[agent] for agent in extra) + " (team_image.py: made "
             "from each agent's own Containerfile; sandbox-ai build team --with " + ",".join(extra) + ").\n",
             f"ARG TEAM_BASE={BASE_IMAGE}\n", "FROM ${TEAM_BASE}\n"]
    parts += [layer(agent, here) for agent in extra]
    parts.append("# The base's agents are still there as they were (OpenCode 1 is `opencode`).\n"
                 "RUN claude --version && codex --version && opencode --version\n")
    parts.append("ENV HOME=/root\nWORKDIR /work\n")
    return "".join(parts)


def versions(agents, here=HERE):
    """{agent: its reviewed release} of the layer agents of AGENTS (the ARG <AGENT>_VERSION of its Containerfile)."""
    found = {}
    for agent in layers(agents):
        match = re.search(rf"^ARG {agent.upper()}_VERSION=(\S+)$", containerfile_of(agent, here), re.M)
        if not match:
            raise ValueError(f"Containerfile.{agent}: no ARG {agent.upper()}_VERSION")
        found[agent] = match.group(1)
    return found


def main(argv):
    try:
        if argv[:1] == ["containerfile"] and argv[1:]:
            sys.stdout.write(containerfile(argv[1:]))
        elif argv[:1] == ["image"]:
            print(image(argv[1:]))
        elif argv[:1] == ["layers"] and len(argv) == 2:
            print(" ".join(parse([argv[1]])))
        elif argv[:1] == ["layers-of"] and len(argv) == 2:
            agents = agents_of(argv[1])
            if agents is None:
                raise ValueError(f"{argv[1]} is no team image")
            print(" ".join(layers(agents)))
        elif argv[:1] == ["versions"]:
            for agent, version in versions(argv[1:]).items():
                print(agent, version)
        else:
            print("usage: team_image.py containerfile AGENT... | image AGENT... | layers LIST | layers-of IMAGE | "
                  "versions AGENT...",
                  file=sys.stderr)
            return 2
    except (OSError, ValueError) as exc:
        print(f"sandbox-ai build: team: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
