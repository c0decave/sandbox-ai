"""The presets of agents and parts that have not passed their live test yet stay visible and start like any other,
marked "not live-tested yet": `presets list` (text and JSON), `presets show`, and from the JSON the web and the app. A
badge only - there is no variable, nothing to enable. It comes from one place, the launcher's EXPERIMENTAL_PARTS."""
import importlib.util
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import home_guard  # noqa: E402

home_guard.ensure()

ROOT = Path(__file__).resolve().parents[1]


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / "sandbox" / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


s = load("agent_sandbox_badges_test", "agent_sandbox.py")
p = load("presets_badges_test", "presets.py")

# The presets of Hermes, Qwen Code, DSH, Goose, OpenHands, Letta Code, Pi and Cairn.
EXPERIMENTAL_PRESETS = {"coding-hermes": "hermes", "hermes-claude": "hermes", "hermes-kanban": "hermes",
                        "hermes-local-llm": "hermes", "coding-qwen": "qwen", "qwen-local-llm": "qwen",
                        "coding-dsh": "dsh", "dsh-local-llm": "dsh", "coding-goose": "goose",
                        "goose-local-llm": "goose", "coding-openhands": "openhands",
                        "openhands-local-llm": "openhands", "coding-nanobot": "nanobot",
                        "nanobot-local-llm": "nanobot", "coding-letta": "letta", "letta-local-llm": "letta",
                        "cairn-pentest-board": "cairn", "cairn-local-llm": "cairn", "default-openhands": "openhands", "default-nanobot": "nanobot", "default-letta": "letta", "default-hermes": "hermes", "default-qwen": "qwen", "default-dsh": "dsh", "default-goose": "goose", "default-opencode2": "opencode2", "coding-opencode2": "opencode2", "opencode2-local-llm": "opencode2",
                        "coding-pi": "pi", "pi-local-llm": "pi", "default-pi": "pi"}


@pytest.fixture(autouse=True)
def own_config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))


def test_the_list_names_what_sandbox_ai_has():
    """Its agents are agents of the launcher, Cairn is its own part (--cairn: cairn.py)."""
    agents = {part["agent"] for part in s.EXPERIMENTAL_PARTS.values() if "agent" in part}
    assert agents == {"opencode2", "hermes", "dsh", "qwen", "goose", "openhands", "nanobot", "letta", "pi"} and \
        agents <= set(s.AUTH_TARGETS)
    cairn = load("cairn_badges_test", "cairn.py")
    assert s.EXPERIMENTAL_PARTS["cairn"] == {"option": "--cairn"} and cairn.IMAGE == s.CAIRN_IMAGE
    assert s.experimental_needs(["claude"], ["--cairn"]) == ["cairn"] == s.experimental_needs(["claude"], ["-cr"])


def test_exactly_these_presets_are_marked():
    marked = {}
    for preset in p.catalog().values():
        badge = p.experimental(preset)
        if badge:
            marked[preset["id"]] = badge["names"]
    assert marked == {ident: [name] for ident, name in EXPERIMENTAL_PRESETS.items()}


def test_list_show_and_json_carry_the_badge():
    lines = []
    assert p.main(["list"], out=lines.append) == 0
    rows = {line.split()[0]: line for line in lines if line.startswith("  ")}
    for ident in EXPERIMENTAL_PRESETS:
        assert rows[ident].endswith("[not live-tested yet]")  # listed, marked
    assert "live-tested" not in rows["coding-claude"]
    assert not any("SANDBOX_AI_EXPERIMENTAL" in line for line in lines)  # nothing to enable
    lines = []
    assert p.main(["list", "--json"], out=lines.append) == 0
    listed = {preset["id"]: preset for preset in json.loads(lines[0])}
    assert listed["coding-goose"]["experimental"] == {"names": ["goose"]}
    assert "experimental" not in listed["coding-claude"]
    lines = []
    assert p.main(["show", "cairn-pentest-board"], out=lines.append) == 0
    assert "  not live-tested yet (cairn)" in lines
    lines = []
    assert p.main(["show", "--json", "hermes-claude"], out=lines.append) == 0
    assert json.loads(lines[0])["experimental"] == {"names": ["hermes"]}


def test_no_preset_asks_for_a_variable_to_enable_it():
    for preset in p.catalog().values():
        assert "SANDBOX_AI_EXPERIMENTAL" not in json.dumps(p.public(preset)), preset["id"]


def test_the_launchers_presets_module_gets_the_badge_from_the_launcher():
    module = s.load_presets_module()
    assert module.EXPERIMENTAL_BADGE is s.experimental_badge
    assert module.experimental(module.load("coding-qwen")) == {"names": ["qwen"]}
