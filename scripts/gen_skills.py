#!/usr/bin/env python3
"""Write the sandbox-ai skill and the example skill of every agent from one source.

  scripts/gen_skills.py           write sandbox/builtin/AGENT/skills/sandbox-ai/SKILL.md and
                                  sandbox/templates/AGENT/skills/sandbox-example/SKILL.md for every agent
  scripts/gen_skills.py --check   write nothing; list the files that differ from what would be written (exit 1)

The source (scripts/skill_source/, not shipped: the generated files are): sandbox-ai.md.in and
sandbox-example.md.in, the text every agent reads; the facts of each agent in agents.json (its name, memory,
skills folder, how it takes up an MCP server or a skill while it runs, its one-shot command); its own section in
agents/AGENT.md ("TITLE in this sandbox"). The generated files stay committed: agents read them from the image and
the kits (tests/test_skill_sync.py fails when one differs from what this makes, and when an agent has no facts).

In a template, @@KEY@@ is replaced by the agent's value, and the lines between `<!-- if FEATURE -->` and
`<!-- end FEATURE -->` are kept only when FEATURE is there (FEATURES: toolchains, once sandbox/toolchains.py is).
Standard library only.
"""
import argparse
import json
from pathlib import Path
import re
import sys
import textwrap

ROOT = Path(__file__).resolve().parents[1]
SANDBOX = ROOT / "sandbox"
SOURCE = ROOT / "scripts" / "skill_source"
SKILL_TEMPLATE = SOURCE / "sandbox-ai.md.in"
EXAMPLE_TEMPLATE = SOURCE / "sandbox-example.md.in"
FACTS = SOURCE / "agents.json"
SECTIONS = SOURCE / "agents"
KEYS = ("title", "memory", "skills", "mcp_live", "skill_reload", "helper", "example_kits")
# What a feature block needs to be kept: the module that brings the feature (the skill names its commands only once
# sandbox-ai has them; a test parses every command line of the skill with the real parsers).
FEATURES = {"toolchains": SANDBOX / "toolchains.py"}
BLOCK = re.compile(r"^<!-- if ([a-z-]+) -->\n(.*?)^<!-- end \1 -->\n", re.M | re.S)
TOKEN = re.compile(r"@@([A-Z_]+)@@")
WIDTH = 116  # characters of a line, as the hand-written copies had them
HOLD = "\x00"  # a space inside a code span while a paragraph is filled
RESTART = "`sandbox-ai resume NAME --restart`, the conversation continues"
KIT_RESTART = "`sandbox-ai kits restart-agent $SANDBOX_AI_RUN`, the conversation continues"


def facts():
    """{agent: its facts} of agents.json (the keys starting with _ are comments)."""
    data = json.loads(FACTS.read_text(encoding="utf-8"))
    return {agent: value for agent, value in data.items() if not agent.startswith("_")}


def features():
    """The names of FEATURES whose module is there."""
    return {name for name, path in FEATURES.items() if path.is_file()}


def blocks(text, present):
    """TEXT with each feature block kept (without its marker lines) when its feature is in PRESENT, else dropped."""
    def keep(match):
        if match[1] not in FEATURES:
            raise ValueError(f"unknown feature block {match[1]!r} (FEATURES names {', '.join(sorted(FEATURES))})")
        return match[2] if match[1] in present else ""
    result = BLOCK.sub(keep, text)
    if "<!-- if " in result or "<!-- end " in result:
        raise ValueError("a feature block without its end, or nested")
    return result


def mcp_live(fact):
    """What the skill says of an MCP server attached while the agent runs."""
    if fact["mcp_live"] == "maybe":
        return (f"{fact['title']} may take up a server added while it runs: look for it in `/mcp`; if it is not "
                f"listed, the human starts you again in place ({RESTART}).")
    return (f"You read your MCP servers only when you start: a server attached while you run is yours once the "
            f"human starts you again in place ({RESTART}); until then you do not see it.")


def mcp_live_short(fact):
    if fact["mcp_live"] == "maybe":
        return "look for it in `/mcp`; if it is not there: `sandbox-ai resume NAME --restart`"
    return f"{fact['title']} sees it after `sandbox-ai resume NAME --restart`, the conversation continues"


def bridge_live(fact):
    if fact["mcp_live"] == "maybe":
        return ("Each side gets an MCP server `bridge-PEER`, attached while it runs: Claude Code takes it up (look "
                "for it in `/mcp`).")
    return (f"Each side gets an MCP server `bridge-PEER`, attached while it runs - but {fact['title']} reads its MCP "
            "servers only when it starts, and a restart ends a bridge, so `bridge-PEER` does not reach you (bridges "
            "are made for Claude Code). The peer may still read your files or watch you where the human granted "
            "it; if the human expects you to use a bridge, tell them so.")


def bridge_short(fact):
    if fact["mcp_live"] == "maybe":
        return "`bridge-PEER` appears among your tools"
    return f"its `bridge-PEER` does not reach {fact['title']}, which reads its MCP servers only when it starts"


def kit_reload(fact):
    """How the agent takes up a skill `sandbox-ai kits attach` gives it while it runs (kits_live.RELOAD)."""
    how, what = fact["skill_reload"]
    again = f"the human starts you again in place ({KIT_RESTART})"
    if how == "auto":
        return f"you see it at once; if you do not, {again}"
    if how == "command" and what.startswith("/"):
        return f"you take it up after `{what}` is typed in you (ask the human); else {again}"
    if how == "command":
        return f"you take it up in {what}; else {again}"
    if how == "restart":
        return f"you take it up only when {again}"
    raise ValueError(f"{fact['title']}: skill_reload {how!r} is none of auto, command, restart")


def helper_line(agent, fact):
    return (f"- As another agent's `--helper {agent}` (or beside it, `--with {agent}`) you run one task at a time, "
            f"as `{fact['helper']}`.")


def section(agent, fact):
    """The agent's own section (agents/AGENT.md) with its helper line."""
    text = (SECTIONS / f"{agent}.md").read_text(encoding="utf-8").rstrip("\n")
    heading = f"## {fact['title']} in this sandbox"
    if not text.startswith(heading + "\n"):
        raise ValueError(f"agents/{agent}.md does not start with {heading!r}")
    return text + "\n" + helper_line(agent, fact)


def substitute(text, values, name):
    def value(match):
        if match[1] not in values:
            raise ValueError(f"{name}: no value for @@{match[1]}@@")
        return values[match[1]]
    return wrap(TOKEN.sub(value, text))


def wrap(text):
    """TEXT with each paragraph and list item filled to WIDTH (a list item's lines indented under its text), so a
    value put in reads as if written there; the front matter, headings and table rows stay as they are (a table
    row and a front matter field must be one line)."""
    lines, para, front = [], [], 0

    def flush():
        if para:
            indent = "  " if para[0].startswith("- ") else ""
            # A code span is never broken: a command line stays one line to copy (its spaces held while filling).
            joined = re.sub(r"`[^`]*`", lambda span: span[0].replace(" ", HOLD), " ".join(para))
            lines.extend(line.replace(HOLD, " ") for line in textwrap.wrap(
                joined, WIDTH, subsequent_indent=indent, break_long_words=False, break_on_hyphens=False))
            para.clear()
    for line in text.split("\n"):
        if front < 2:
            front += line == "---"
            lines.append(line)
        elif not line.strip() or line.startswith(("#", "|")):
            flush()
            lines.append(line)
        elif line.startswith("- ") or (para and para[0].startswith("- ") and not line.startswith(" ")):
            flush()
            para.append(line.strip())
        else:
            para.append(line.strip())
    flush()
    return "\n".join(lines)


def values_of(agent, fact):
    missing = [key for key in KEYS if key not in fact]
    if missing:
        raise ValueError(f"agents.json: {agent} has no {', '.join(missing)}")
    return {"AGENT": agent, "TITLE": fact["title"], "MEMORY": fact["memory"], "SKILLS_DIR": fact["skills"],
            "MCP_LIVE": mcp_live(fact), "MCP_LIVE_SHORT": mcp_live_short(fact), "BRIDGE_LIVE": bridge_live(fact),
            "BRIDGE_SHORT": bridge_short(fact), "KIT_RELOAD": kit_reload(fact), "EXAMPLE_KITS": fact["example_kits"],
            "AGENT_SECTION": section(agent, fact)}


def render(agents=None, present=None):
    """{path below ROOT: text} of every generated file. AGENTS: the agents (default: those sandbox-ai has,
    kits.AGENTS); PRESENT: the features to include (default: those that are there)."""
    data = facts()
    agents = list(agents if agents is not None else known_agents())
    present = features() if present is None else set(present)
    unknown = sorted(set(agents) - set(data))
    if unknown:
        raise ValueError(f"agents.json has no facts for {', '.join(unknown)} (add them, and agents/AGENT.md)")
    skill = blocks(SKILL_TEMPLATE.read_text(encoding="utf-8"), present)
    example = blocks(EXAMPLE_TEMPLATE.read_text(encoding="utf-8"), present)
    found = {}
    for agent in agents:
        values = values_of(agent, data[agent])
        found[f"sandbox/builtin/{agent}/skills/sandbox-ai/SKILL.md"] = substitute(skill, values, agent)
        found[f"sandbox/templates/{agent}/skills/sandbox-example/SKILL.md"] = substitute(example, values, agent)
    return found


def known_agents():
    """kits.AGENTS, read from kits.py without importing it (a tuple of names on one line)."""
    match = re.search(r"^AGENTS = \(([^)]*)\)", (SANDBOX / "kits.py").read_text(encoding="utf-8"), re.M)
    return re.findall(r'"([a-z0-9]+)"', match[1])


def differing(found):
    """The paths of FOUND whose file is missing or differs."""
    result = []
    for path, text in found.items():
        target = ROOT / path
        if not target.is_file() or target.read_text(encoding="utf-8") != text:
            result.append(path)
    return result


def main(argv=None, out=print):
    parser = argparse.ArgumentParser(description="Write every agent's sandbox-ai skill and example skill from "
                                                 "scripts/skill_source.")
    parser.add_argument("--check", action="store_true", help="write nothing; list the files that differ (exit 1)")
    args = parser.parse_args(argv)
    found = render()
    stale = differing(found)
    if args.check:
        for path in stale:
            out(f"{path}: differs from what scripts/gen_skills.py writes")
        return 1 if stale else 0
    for path in stale:
        target = ROOT / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(found[path], encoding="utf-8")
        out(f"wrote {path}")
    if not stale:
        out("every skill is up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
