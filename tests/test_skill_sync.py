"""The sandbox-ai skill of every agent comes from one source (scripts/skill_source, scripts/gen_skills.py): the
twelve copies had drifted (2026-11-22: six agents were told their tool rules were not enforced and a start with
them refused, long after Hermes, Goose, Letta Code, Pi, OpenCode 2 and DSH got their hooks; every agent was told
"in Claude: /mcp" and that a new MCP server "appears in your MCP list", which only Claude Code may do; two helper
lines named one-shot commands that are not the ones a helper runs). These tests keep it one: the files are what the
generator writes, every agent has its facts and they are the code's, every `sandbox-ai ...` command line of the
skill parses with sandbox-ai's own parsers, every option it names exists, its front matter is what the agents'
loaders take, the hidden-text checker finds nothing in it, and it stays within its size budget."""
import argparse
import contextlib
import importlib.util
import io
from pathlib import Path
import re
import shlex
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gen = load("gen_skills_for_tests", "scripts/gen_skills.py")
kits = load("kits_for_skill_sync", "sandbox/kits.py")
kits_live = load("kits_live_for_skill_sync", "sandbox/kits_live.py")
lint = load("lint_for_skill_sync", "sandbox/lint.py")
completion = load("completion_for_skill_sync", "sandbox/completion.py")
s = load("sandbox_for_skill_sync", "sandbox/agent_sandbox.py")

# What an agent pays for in tokens each time it reads the skill (it had 497-527 lines before the sync).
MAX_LINES = 650
MAX_BYTES = 64 << 10
# Options the skill names that are not sandbox-ai's: the agents' and other programs' own (each with where it is).
FOREIGN_OPTIONS = {
    "--no-verify": "git", "--cert": "pip", "--socks5-hostname": "curl", "--proxy-server": "Chromium",
    "--device-auth": "codex login", "--output-format": "claude -p", "--skip-git-repo-check": "codex exec",
    "--oneshot": "hermes", "--prompt": "qwen", "--quiet": "goose run", "--text": "goose run, openhands task",
    "--no-markdown": "nanobot agent", "--message": "nanobot agent", "--new": "letta -p",
    "--cdp-endpoint": "Playwright's MCP server", "--extensions": "qwen", "--yolo": "letta", "--backend": "letta",
    "--channels": "letta server", "--remote": "opencode pair", "--session-id": "dsh headless",
    "--sandbox": "qwen", "--safe-mode": "hermes", "--telemetry": "the agents' own (refused)",
    "--write": "runtime.py --untrusted", "--untrusted": "runtime.py", "--env": "runtime.py --untrusted, start",
    "--continue": "pi", "--mcp-config": "claude",
}


@pytest.fixture(scope="module")
def rendered():
    return gen.render()


def skills(rendered):
    return {path.split("/")[2]: text for path, text in rendered.items() if path.endswith("sandbox-ai/SKILL.md")}


# --- one source ------------------------------------------------------------------------------------------------

def test_every_generated_file_is_what_the_generator_writes(rendered):
    stale = gen.differing(rendered)
    assert not stale, f"run scripts/gen_skills.py (these differ from scripts/skill_source): {stale}"
    assert len(rendered) == 2 * len(kits.AGENTS)
    assert gen.main(["--check"], out=lambda line: None) == 0


def test_check_names_a_file_that_differs(rendered):
    path = next(iter(rendered))
    changed = {**rendered, path: rendered[path] + "a line edited by hand\n"}
    assert gen.differing(changed) == [path]


def test_every_agent_has_its_facts_and_its_section():
    facts = gen.facts()
    assert sorted(facts) == sorted(kits.AGENTS) == sorted(gen.known_agents())
    for agent, fact in facts.items():
        assert set(gen.KEYS) <= set(fact), agent
        assert (gen.SECTIONS / f"{agent}.md").read_text().startswith(f"## {fact['title']} in this sandbox\n")


def test_an_agent_without_facts_is_refused():
    with pytest.raises(ValueError, match="no facts for newagent"):
        gen.render(agents=[*kits.AGENTS, "newagent"])


def test_the_facts_are_the_codes():
    """Each fact is the one the launcher acts on: the agent's name, memory file, skills folder, how it takes up an
    MCP server and a skill while it runs, and the one-shot command a helper or companion runs."""
    mcp = load("mcp_for_skill_sync", "sandbox/mcp.py")
    for agent, fact in gen.facts().items():
        assert fact["title"] == kits.AGENT_NAMES[agent], agent
        assert f"`~/{kits.MEMORY_FILE[agent][1]}`" in fact["memory"], agent
        assert fact["skills"] == f"~/{kits.LAYOUT[agent]['skills']}", agent
        assert fact["mcp_live"] == mcp.live_agent(agent)["restart"], agent
        assert tuple(fact["skill_reload"]) == kits_live.RELOAD[agent]["skills"], agent
        assert fact["helper"] == s.COMPANION_COMMANDS[agent], agent


def test_the_tool_rules_line_says_what_the_launcher_does(rendered):
    """Rules for the agent's own tools hold for every agent whose hooks sandbox-ai installs; only OpenHands and
    nanobot refuse a start with them (openhands.check, nanobot.check)."""
    hooked = set(s.OWN_CONFIG_HOOK_AGENTS) | set(s.MANAGED_DIRS) | {"opencode"}
    for agent, text in skills(rendered).items():
        own = text.split(f"## {kits.AGENT_NAMES[agent]} in this sandbox", 1)[1].split("\n## ", 1)[0]
        line = re.search(r"- Rules for your own tools.*?(?=\n- |\Z)", own, re.S)
        assert line, agent
        if agent in hooked:
            assert "hold through" in line[0] and "refused" not in line[0].split("hold through")[0], agent
            assert "not enforced" not in own, agent
        else:
            assert f"cannot be enforced for {kits.AGENT_NAMES[agent]}" in line[0] and "start with them is refused" \
                in line[0], agent


def test_only_claude_code_is_told_it_takes_up_a_server_while_it_runs(rendered):
    for agent, text in skills(rendered).items():
        if agent == "claude":
            assert "look for it in `/mcp`" in text and "does not reach you" not in text
        else:
            assert "/mcp" not in text, agent
            assert "only when you start" in text and "`bridge-PEER` does not reach you" in text, agent


def test_the_example_skill_names_the_agents_own_folders(rendered):
    for agent in kits.AGENTS:
        text = rendered[f"sandbox/templates/{agent}/skills/sandbox-example/SKILL.md"]
        assert f"home/{agent}/" in text and f"~/{kits.MEMORY_FILE[agent][1]}" in text, agent
        assert "is discarded after the run" not in text, "HOME is kept by default since sessions became starts"
        assert text.startswith("---\nname: sandbox-example\ndescription: ")


def test_a_feature_block_is_kept_only_with_its_feature():
    """The toolchains part waits for sandbox/toolchains.py (feature/toolchains): its commands must parse."""
    with_it = gen.render(agents=["codex"], present={"toolchains"})["sandbox/builtin/codex/skills/sandbox-ai/SKILL.md"]
    without = gen.render(agents=["codex"], present=set())["sandbox/builtin/codex/skills/sandbox-ai/SKILL.md"]
    assert "## Toolchains" in with_it and "sandbox-ai toolchain attach $SANDBOX_AI_RUN NAME" in with_it
    assert "toolchain" not in without.replace("Another toolchain as your base", "")
    assert "<!--" not in with_it and "<!--" not in without and "@@" not in with_it + without
    assert gen.features() == ({"toolchains"} if (ROOT / "sandbox/toolchains.py").is_file() else set())
    with pytest.raises(ValueError, match="unknown feature block"):
        gen.blocks("<!-- if nothing -->\nx\n<!-- end nothing -->\n", set())


# --- what the agents' loaders take -----------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ("sandbox-ai", "sandbox-example"))
def test_the_front_matter_is_valid_for_every_loader(rendered, kind):
    """name and description on one line each, as the Agent Skills format has them (Claude Code, Codex, OpenCode,
    Hermes, Goose, Qwen Code, Pi, Letta Code, OpenHands, nanobot, DSH read the same): a name of lower-case letters,
    digits and hyphens that is the folder's, a description of at most 1024 characters that a YAML loader reads as
    one plain string (no ': ' or ' #' in it)."""
    for path, text in rendered.items():
        if f"/{kind}/SKILL.md" not in path:
            continue
        match = re.match(r"---\nname: ([^\n]*)\ndescription: ([^\n]*)\n---\n\n# ", text)
        assert match, path
        name, description = match[1], match[2]
        assert name == kind and re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name) and len(name) <= 64
        assert 0 < len(description) <= 1024 and ": " not in description and " #" not in description, path
        assert not re.match(r"[-?:,\[\]{}#&*!|>'\"%@`]", description), path


def test_the_hidden_text_checker_finds_nothing(rendered):
    """sandbox-ai scan, kits add-skill and kits attach would refuse a copy of the skill with findings: none, not
    even a URL to a bare address or a credential path."""
    for path, text in rendered.items():
        assert lint.check_text(text, path) == [], path


def test_the_skill_stays_within_its_budget(rendered):
    for agent, text in skills(rendered).items():
        assert text.count("\n") <= MAX_LINES and len(text.encode()) <= MAX_BYTES, agent
    for feature in gen.FEATURES:  # with every feature that is still to come too
        for agent, text in skills(gen.render(present=set(gen.FEATURES))).items():
            assert text.count("\n") <= MAX_LINES, (agent, feature)


def test_the_first_rules_come_first(rendered):
    for agent, text in skills(rendered).items():
        head = text.split("\n## ", 1)[0]
        assert "**Never try to get around them**" in head and "exact command" in head, agent


def test_the_bridge_tools_it_names_are_the_bridges(rendered):
    bridge = load("bridge_for_skill_sync", "sandbox/bridge.py")
    text = skills(rendered)["claude"]
    part = text.split("## Bridges to other sessions", 1)[1].split("\n## ", 1)[0]
    named = set(re.findall(r"`([a-z]+(?:_[a-z]+)*)`", part)) - {"watch", "observer", "reconnecting"}
    assert named and named <= set(bridge.TOOL_SPECS), sorted(named - set(bridge.TOOL_SPECS))
    assert set(bridge.TOOL_SPECS) <= named | {"info"}


# --- every command line parses -----------------------------------------------------------------------------------

# Placeholders a parser checks the value of, as a value it takes.
SAMPLE = {"$SANDBOX_AI_RUN": "brisk-otter-lantern", "PORT": "8080", "KIND": "pypi"}


def command_lines(text):
    """[(argv after `sandbox-ai`, the span)] of every inline code span that is a `sandbox-ai COMMAND ...` line (an
    environment assignment before it is left out)."""
    found = []
    for span in re.findall(r"`([^`\n]+)`", " ".join(text.split())):
        line = re.sub(r"^(?:[A-Z_]+=\S+ )+", "", span)
        if not re.match(r"sandbox-ai [a-z]", line):
            continue
        found.append(([SAMPLE.get(word, word) for word in shlex.split(line)[1:]], span))
    return found


_parsers = {}


def parser(command):
    if command not in _parsers:
        _parsers[command] = completion.parser_of(command)
    return _parsers[command]


def parse_error(argv):
    """None when sandbox-ai's own parser of ARGV's command takes ARGV, else what it said."""
    command = argv[0]
    if command in completion.AGENTS:
        try:
            with contextlib.redirect_stderr(io.StringIO()) as err:
                s.parse_args(argv)
        except SystemExit:
            return err.getvalue().strip() or "refused"
        return None
    if command in ("build", "update"):  # main.py's own: their options are in their --help
        shown = subprocess.run([sys.executable, str(ROOT / "sandbox/main.py"), command, "--help"],
                               capture_output=True, text=True, timeout=60).stdout
        wrong = [word for word in argv[1:] if word.startswith("--") and word not in shown]
        return f"not in sandbox-ai {command} --help: {wrong}" if wrong else None
    if "--" in argv:  # what follows is the command's own (sandbox-ai shell NAME -- COMMAND)
        argv = argv[:argv.index("--")]
    if command == "plugins" and argv[1:2] in (["attach"], ["detach"]):  # plugins.cli hands these to kits_live
        error = parse_only(lambda: kits_live.plugins_cli(argv[1:]))
    else:
        found = parser(command)
        if found is None:
            return f"no parser for sandbox-ai {command}: {completion.NOT_FOUND.get(command)}"
        error = parse_only(lambda: found.parse_args(argv[1:]))
    left = re.search(r"unrecognized arguments: (.*)$", error or "")
    if command == "resume" and left:  # starts.main hands what resume does not know on as start options
        return parse_error(["claude", *left[1].split()])
    if error and "the following arguments are required" in error and not any(
            word.startswith("-") for word in argv[1:]):
        return None  # a command named, not a command line: `sandbox-ai checkpoint diff` (its words are its own)
    return error


class Parsed(Exception):
    pass


def parse_only(function):
    """None when FUNCTION's parsing (whichever argparse method it calls) takes its arguments, else what argparse
    said; nothing after the parsing runs."""
    original, depth = argparse.ArgumentParser.parse_known_args, []

    def parse(self, args=None, namespace=None):
        depth.append(self)  # a subcommand's parser is called from within its parent's
        try:
            result = original(self, args, namespace)
        finally:
            depth.pop()
        if not depth and not result[1]:
            raise Parsed()
        return result  # (parse_args then refuses what was left over)
    argparse.ArgumentParser.parse_known_args = parse
    try:
        with contextlib.redirect_stderr(io.StringIO()) as err, contextlib.redirect_stdout(io.StringIO()):
            function()
    except Parsed:
        return None
    except SystemExit:
        return err.getvalue().strip() or "refused"
    finally:
        argparse.ArgumentParser.parse_known_args = original
    return "parsed nothing"


@pytest.mark.parametrize("agent", ("claude", "hermes"))
def test_every_command_line_of_the_skill_parses(rendered, agent):
    """Each `sandbox-ai ...` the skill tells an agent to give the human parses with that command's own parser (the
    agents' copies differ only in their own section and the agent's name in a command: two of them suffice)."""
    lines = command_lines(skills(rendered)[agent]) + command_lines(
        rendered[f"sandbox/templates/{agent}/skills/sandbox-example/SKILL.md"])
    assert len(lines) > 80
    failed = [(span, error) for argv, span in lines for error in [parse_error(argv)] if error]
    assert not failed, "\n".join(f"{span}: {error}" for span, error in failed)


def test_the_check_of_command_lines_catches_a_wrong_one():
    assert parse_error(["net", "allow", "run1", "--allow-hosts", "x"]) is not None
    assert parse_error(["net", "allow", "run1", "--allow-host", "x"]) is None
    assert parse_error(["claude", "--no-such-option"]) is not None
    assert parse_error(["resume", "web", "--restart", "--gui"]) is None  # a start option, handed on
    assert parse_error(["resume", "web", "--restart", "--no-such-option"]) is not None
    assert parse_error(["checkpoint", "diff"]) is None and parse_error(["checkpoint", "diffs"]) is not None


def every_option():
    found = set()

    def walk(found_parser):
        for action in found_parser._actions:
            found.update(option for option in action.option_strings if option.startswith("--"))
            if isinstance(action, argparse._SubParsersAction):
                for sub in action.choices.values():
                    walk(sub)
    for command in completion.commands():
        if parser(command) is not None:
            walk(parser(command))
    for command in ("build", "update"):
        shown = subprocess.run([sys.executable, str(ROOT / "sandbox/main.py"), command, "--help"],
                               capture_output=True, text=True, timeout=60).stdout
        found.update(re.findall(r"--[a-z][a-z0-9-]*", shown))
    return found


def test_every_option_it_names_exists(rendered):
    """No stale option name: each `--option` of the skill is one of sandbox-ai's (any command's) or one of
    FOREIGN_OPTIONS, another program's."""
    known = every_option()
    for agent, text in skills(rendered).items():
        named = set(re.findall(r"(?<![\w-])--[a-z][a-z0-9-]*", text))
        unknown = sorted(named - known - set(FOREIGN_OPTIONS))
        assert not unknown, f"{agent}: options that are no sandbox-ai option (or add them to FOREIGN_OPTIONS): {unknown}"


# --- the real entry points ---------------------------------------------------------------------------------------

def test_the_generator_writes_every_file_and_check_finds_an_edit(tmp_path, monkeypatch):
    monkeypatch.setattr(gen, "ROOT", tmp_path)
    said = []
    assert gen.main([], out=said.append) == 0 and len(said) == 2 * len(kits.AGENTS)
    assert gen.main(["--check"], out=said.append) == 0
    edited = tmp_path / "sandbox/builtin/pi/skills/sandbox-ai/SKILL.md"
    edited.write_text(edited.read_text() + "an edit by hand\n")
    said.clear()
    assert gen.main(["--check"], out=said.append) == 1
    assert said == ["sandbox/builtin/pi/skills/sandbox-ai/SKILL.md: differs from what scripts/gen_skills.py writes"]
    assert gen.main([], out=said.append) == 0 and gen.main(["--check"], out=said.append) == 0


def test_the_commands_say_it_is_in_sync_and_clean():
    """scripts/gen_skills.py --check and sandbox-ai scan, as a developer and a user run them."""
    check = subprocess.run([sys.executable, str(ROOT / "scripts/gen_skills.py"), "--check"], capture_output=True,
                           text=True, timeout=120)
    assert check.returncode == 0 and check.stdout == "", check.stdout
    folders = [str(path.parent) for path in sorted((ROOT / "sandbox").glob("*/*/skills/sandbox-*/SKILL.md"))]
    assert len(folders) == 2 * len(kits.AGENTS)
    scan = subprocess.run([sys.executable, str(ROOT / "sandbox/main.py"), "scan", *folders], capture_output=True,
                          text=True, timeout=120)
    assert scan.returncode == 0 and scan.stdout.count("no findings") == len(folders), scan.stdout
