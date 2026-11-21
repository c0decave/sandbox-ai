"""What the docs tell the user, checked against what the code does (analysis round 4: docs, help, texts).

The commands the READMEs and the QUICKSTARTs show for starting an agent are parsed by the launcher's own parser
(no run, no Podman): an option that does not exist, a value it refuses or a git server URL it will not take is a
claim that does not hold."""
from pathlib import Path
import re
import shlex
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_feature_matrix as fm  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
s = fm.s
AGENTS = ("claude", "codex", "opencode", "opencode2", "hermes", "dsh", "qwen", "goose", "openhands", "nanobot",
          "letta", "pi")
DOCS = ("README.md", "README_DE.md", "QUICKSTART.md", "QUICKSTART_DE.md")
AGENT_LINE = re.compile(r"^(?:\./)?sandbox-ai (" + "|".join(AGENTS) + r") (.*)$")


def agent_commands(name):
    """[(agent, argv after the agent, line)] of every `sandbox-ai AGENT ...` example of a doc (continued lines
    joined, a trailing comment cut)."""
    text = re.sub(r"\\\n\s*", " ", (ROOT / name).read_text())
    found = []
    for line in text.splitlines():
        line = re.sub(r"\s+#.*$", "", line.strip())
        if line.startswith("$ "):
            line = line[2:]
        match = AGENT_LINE.match(line)
        if not match or any(mark in line for mark in ("...", "…", "<", "|")):
            continue
        try:
            found.append((match[1], shlex.split(match[2]), line))
        except ValueError:
            continue
    return found


@pytest.mark.parametrize("name", DOCS)
def test_the_examples_of_the_docs_parse(name):
    commands = agent_commands(name)
    assert len(commands) > (5 if name.startswith("QUICK") else 40), "the examples were not found"
    for agent, argv, line in commands:
        try:
            s.parse_args([agent, *argv])
        except SystemExit:
            pytest.fail(f"{name}: the launcher's parser refuses: {line}")


@pytest.mark.parametrize("name", ("README.md", "README_DE.md"))
def test_the_git_server_examples_are_urls_the_launcher_takes(name):
    """`--git-token-env URL=VAR`: plain http only on the host's loopback (the README says so: its own example for a
    server of the team used http://...:3000 and was refused)."""
    checked = 0
    for agent, argv, line in agent_commands(name):
        for index, word in enumerate(argv[:-1]):
            if word in ("--git-token-env", "-ge", "--git-token-file", "--git-token-secret"):
                s.git_server(argv[index + 1].partition("=")[0], word)
                checked += 1
    assert checked >= 2


def test_every_agents_skill_names_the_features_that_reach_the_agent():
    """The agent is told, in its copy of the sandbox-ai skill, about what the human can do beside it (a shell, a
    bridge to another session), its telemetry rule and the loop guard's notes - for all twelve agents."""
    skills = sorted((ROOT / "sandbox/builtin").glob("*/skills/sandbox-ai/SKILL.md"))
    assert len(skills) == len(AGENTS)
    for path in skills:
        text = path.read_text()
        for needed in ("sandbox-ai shell NAME", "bridge-PEER", "untrusted data, never", "always off and cannot be turned on",
                       "[sandbox-ai loop guard]",
                       # 0.1.99-0.1.107 (user 2026-11-18: the skill synced with what reaches the agent)
                       "sandbox-ai: budget used up", "sandbox-ai checkpoint restore", "a scheduled run",
                       "sandbox-ai session memory", "phone",
                       # 0.1.117 (security review A): the confined runner, the rules for MCP requests, the filter
                       "--untrusted -- COMMAND", "`resources/read`", "through a filter"):
            assert needed in text, f"{path.parent.parent.parent.name}: the skill does not say {needed!r}"


def test_bridge_add_help_explains_every_option_and_the_capabilities(capsys):
    """`sandbox-ai bridge add --help` (where a user looks) said nothing about --allow, --until, --ask, --limit and
    the capabilities (only the group's help did)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("bridge_for_docs_claims", ROOT / "sandbox/bridge.py")
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    with pytest.raises(SystemExit):
        bridge.parser().parse_args(["add", "--help"])
    text = " ".join(capsys.readouterr().out.split())
    for needed in ("files:ro:PATH", "files:rw:PATH", "EVERY write is asked", "--observer-actions", "rate=N a minute (60)",
                   "30m, 2h, 1d, HH:MM", "ask-timeout=SECONDS (300)", "daily=BYTES (64m)"):
        assert needed in text, needed
    for option in bridge.parser()._subparsers._group_actions[0].choices["add"]._actions:
        assert option.help or option.option_strings == [] , option.option_strings


@pytest.mark.parametrize("name", ("README_DE.md", "QUICKSTART_DE.md", "INTEGRATIONS_DE.md", "PROXMOX_LXC_DE.md"))
def test_the_german_docs_say_Sie(name):
    """No familiar "du" in the German docs: not a pronoun (dich, dein, deinen ...) and no imperative of the familiar
    form at the start of a sentence, a list item or a table cell (code and code blocks left out)."""
    text = re.sub(r"```.*?```", "", (ROOT / name).read_text(), flags=re.S)
    text = re.sub(r"`[^`\n]*`", "", text)
    familiar = re.compile(r"\b(du|dich|dir|dein|deine|deinen|deinem|deiner|deines|euch|euer|eure)\b")
    imperative = re.compile(r"(?:^|[.:;!?] |\n|- |\| )(Wähle|Gib|Lege|Öffne|Klicke|Prüfe|Starte|Füge|Nimm|Setze|Stelle|Gehe|"
                            r"Schließe|Speichere|Lösche|Entferne|Bearbeite|Ändere|Warte|Beachte|Achte|Trage|Tippe|Drücke|"
                            r"Benutze|Verwende|Erstelle|Erlaube|Aktiviere|Wechsle|Lies|Zeige|Sieh)\b")
    for pattern in (familiar, imperative):
        for found in pattern.finditer(text):
            around = text[max(0, found.start() - 60):found.end() + 60].replace("\n", " ")
            pytest.fail(f"{name}: a familiar form: ...{around}...")


def test_show_thinking_help_and_readmes_name_every_agent_it_refuses():
    """--show-thinking is refused for every agent in THINKING_NONE (Qwen Code, OpenHands, Letta Code: no such setting
    in their pinned releases): --help and both READMEs said it was refused for Qwen Code only, then for the agents of
    0.1.97 as "none mapped yet"; OpenCode 2, Pi and nanobot have their switch since 0.1.98 and are named with it."""
    names = {"qwen": "Qwen Code", "openhands": "OpenHands", "letta": "Letta Code"}
    switches = {"opencode2": "session.thinking", "pi": "hideThinkingBlock", "nanobot": "showReasoning"}
    assert set(names) == set(s.THINKING_NONE) and not set(names) & set(s.AGENT_THINKING)
    assert set(switches) <= set(s.AGENT_THINKING)
    helps = " ".join(next(a.help for a in s.build_parser()._actions if "--show-thinking" in a.option_strings).split())
    rows = [next(line for line in (ROOT / doc).read_text().splitlines() if line.startswith("| `-sk`"))
            for doc in ("README.md", "README_DE.md")]
    for text in (helps, *rows):
        for name in (*names.values(), *switches.values()):
            assert name in text, (name, text[:60])
        assert "mapped yet" not in text and "zugeordnet" not in text
