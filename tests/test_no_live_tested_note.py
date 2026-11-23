"""No agent, preset or option says it is "not live-tested yet" any more (the user, 2026-11-22: "BITTE "noch nicht live
getestet" überall entfernen!" - and they were tested: CI's agents-live jobs, the local-model research rounds, the live
check's harnesses-live). Nothing in the sources a user or an agent sees carries the words, in English or German: the
launcher and its agent modules, the presets, the web interface, the app, the skills; `presets list` and `show` (text
and JSON) carry no badge; a start of each agent says nothing of it, nor does its dry run."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import home_guard  # noqa: E402

home_guard.ensure()

ROOT = Path(__file__).resolve().parents[1]
# Where a user or an agent reads what sandbox-ai says (the launcher, its modules and presets, the web interface, the
# skills it gives the agents and their source, the app).
SOURCES = ("sandbox", "projects/sandbox-ai-app/sandbox_ai_app", "scripts/skill_source")
WORDS = ("live-tested yet", "live getestet", "not live-tested", "nicht live getestet", "[not live-tested",
         "experimental_badge", "EXPERIMENTAL_PARTS", "experimental_note")
# The presets of the agents that carried the badge (and Cairn's): every one still listed, none marked.
AGENT_PRESETS = {"coding-hermes", "hermes-claude", "hermes-kanban", "hermes-local-llm", "coding-qwen", "qwen-local-llm",
                 "coding-dsh", "dsh-local-llm", "coding-goose", "goose-local-llm", "coding-openhands",
                 "openhands-local-llm", "coding-nanobot", "nanobot-local-llm", "coding-letta", "letta-local-llm",
                 "cairn-pentest-board", "cairn-local-llm", "default-openhands", "default-nanobot", "default-letta",
                 "default-hermes", "default-qwen", "default-dsh", "default-goose", "default-opencode2",
                 "coding-opencode2", "opencode2-local-llm", "coding-pi", "pi-local-llm", "default-pi"}


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / "sandbox" / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


s = load("agent_sandbox_no_note_test", "agent_sandbox.py")
p = load("presets_no_note_test", "presets.py")


@pytest.fixture(autouse=True)
def own_config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))


def test_no_source_file_says_not_live_tested_yet():
    found = []
    for top in SOURCES:
        for path in sorted((ROOT / top).rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts or path.suffix in (".png", ".svg", ".ico", ".gz"):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            lowered = text.lower()
            found += [f"{path.relative_to(ROOT)}: {word}" for word in WORDS if word.lower() in lowered]
    assert found == []


def test_the_launcher_and_its_agent_modules_keep_no_such_list_or_note():
    assert not hasattr(s, "EXPERIMENTAL_PARTS") and not hasattr(s, "experimental_badge")
    assert not hasattr(s, "experimental_needs") and not hasattr(s, "check_experimental_agents")
    assert not hasattr(p, "experimental") and not hasattr(p, "EXPERIMENTAL_BADGE")
    for agent in ("opencode2", "hermes", "dsh", "qwen", "goose", "openhands", "nanobot", "letta", "pi"):
        module = s.agent_module(agent)
        assert not hasattr(module, "EXPERIMENTAL") and not hasattr(module, "experimental_note"), agent


def test_every_preset_is_listed_and_none_is_marked():
    lines = []
    assert p.main(["list"], out=lines.append) == 0
    rows = {line.split()[0]: line for line in lines if line.startswith("  ")}
    assert AGENT_PRESETS <= set(rows)
    assert not any("live-tested" in line or "SANDBOX_AI_EXPERIMENTAL" in line for line in lines)
    lines = []
    assert p.main(["list", "--json"], out=lines.append) == 0
    listed = {preset["id"]: preset for preset in json.loads(lines[0])}
    assert AGENT_PRESETS <= set(listed) and not [ident for ident, preset in listed.items() if "experimental" in preset]
    for ident in ("cairn-pentest-board", "coding-goose"):
        lines = []
        assert p.main(["show", ident], out=lines.append) == 0
        assert not any("live-tested" in line for line in lines), ident
        assert any(line.startswith("  agents:") for line in lines)
    lines = []
    assert p.main(["show", "--json", "hermes-claude"], out=lines.append) == 0
    shown = json.loads(lines[0])
    assert "experimental" not in shown and not any("live-tested" in note for note in shown.get("notes", []))


def test_the_presets_notes_still_say_how_to_get_the_image():
    for ident in sorted(AGENT_PRESETS - {"cairn-pentest-board", "cairn-local-llm"}):
        notes = p.load(ident).get("notes", [])
        agent = (p.load(ident).get("agents") or [""])[0]
        assert any(f"sandbox-ai build {agent}" in note for note in notes), ident


@pytest.mark.parametrize("agent", sorted(s.AUTH_TARGETS))
def test_a_start_of_each_agent_says_nothing_of_live_tests(tmp_path, agent):
    """The real entry point, a dry run of each agent (the launcher's notes are said for a dry run too): no note,
    and no "experimental" in its plan."""
    env = {**os.environ, "XDG_CONFIG_HOME": str(tmp_path / "c"), "XDG_STATE_HOME": str(tmp_path / "s"),
           "XDG_DATA_HOME": str(tmp_path / "d"), "XDG_CACHE_HOME": str(tmp_path / "k")}
    (tmp_path / "w").mkdir()
    done = subprocess.run([str(ROOT / "sandbox-ai"), agent, "--backend", "podman", "--workspace", str(tmp_path / "w"),
                           "--no-auth", "--dry-run"], capture_output=True, text=True, env=env, timeout=300,
                          stdin=subprocess.DEVNULL)
    assert done.returncode == 0, done.stderr[-3000:]
    assert "live-tested" not in done.stderr and "live-tested" not in done.stdout
    plan = json.loads(done.stdout)
    assert not isinstance(plan.get(agent), dict) or "experimental" not in plan[agent], plan.get(agent)


def test_nested_containers_starts_without_the_note(tmp_path):
    """--nested-containers too: its help, its dry run and its notes say nothing of a live test."""
    assert "live-tested" not in s.option_actions()["--nested-containers"].help.lower()
    a = s.parse_args(["claude", "--nested-containers", "--workspace", str(tmp_path)])
    assert not any("live-tested" in note for note in s.nested_notes(a))
