"""OpenCode 2 in the app: in the agent choice beside OpenCode 1 (no model list or effort of its own), --opencode2-enable
on the Board's agent node (a problem for another agent, its reach in the capabilities: plugins from npm warned), its
words in both languages, no Credentials row (no login sandbox-ai takes in), the help section OpenCode 2 (exported to
the web page too), its presets in the category Agents (no badge: the user, 2026-11-22)."""
import json
import os
from pathlib import Path

import pytest

from sandbox_ai_app import backend, board as B, guide, help_content, i18n, labels, terminal_text as T, ui
from sandbox_ai_app import web_browser as W

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]
pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


@pytest.fixture(scope="module")
def sai():
    return backend.SandboxAI()


def test_opencode2_is_an_agent_beside_opencode(sai):
    assert "opencode2" in sai.agents and "opencode2" in backend.AGENTS and "opencode2" in B.AGENTS
    assert "opencode" in sai.agents, "OpenCode 1 stays"
    question = next(q for q in guide.questions("agent") if q.key == "agent")
    assert ("opencode2", "OpenCode 2") in question.choices and ("opencode", "OpenCode") in question.choices
    assert guide.AGENT_EFFORTS["opencode2"] == sai.launcher.AGENT_EFFORTS["opencode2"] == ()
    assert guide.AGENT_MODELS["opencode2"] == sai.launcher.AGENT_MODELS["opencode2"]
    assert T.PROVIDERS["opencode2"] == sai.launcher.PROVIDERS["opencode2"] == ["opencode.ai"]
    assert ui.HELPER_TITLES["opencode2"] == "OpenCode 2" and W.AGENT_NAMES["opencode2"] == "OpenCode 2"


def test_opencode2_enable_on_the_board(sai):
    assert "--opencode2-enable" in B.KINDS["agent"].options and B.OWNER["--opencode2-enable"] == "agent"
    assert labels.OPTION_LABELS["--opencode2-enable"][1].startswith("OpenCode 2:")
    assert "--opencode2-enable" in backend.GROUPS["Agent"]

    def errors(agent, argv):
        return [p.text for p in B.problems(sai, B.from_argv(sai, agent, argv)) if p.level == "error"]
    assert any("for OpenCode 2 only" in text for text in errors("opencode", ["--opencode2-enable", "updates"]))
    assert not any("for OpenCode 2 only" in text for text in errors("opencode2", ["--opencode2-enable", "updates"]))
    board = B.from_argv(sai, "opencode2", ["--opencode2-enable", "updates", "--opencode2-enable", "plugins"])
    assert B.values(board, sai)[1]["--opencode2-enable"] == ["updates", "plugins"]
    assert B.to_argv(sai, board).count("--opencode2-enable") == 2
    reach = {c.text: c.level for c in B.capabilities(board) if c.area == "reach" and "OpenCode 2" in c.text}
    assert reach == {"OpenCode 2: updates turned on (their hosts open)": "info",
                     "OpenCode 2: plugins turned on (their hosts open)": "warn"}


def test_the_new_words_have_german():
    for key in ("OpenCode 2", "--opencode2-enable is for OpenCode 2 only.",
                "OpenCode 2: {features} turned on (their hosts open)"):
        assert key in i18n.TEXTS and i18n.TEXTS[key]["de"]


def test_no_credentials_row_for_opencode2():
    """OpenCode 2 has no login sandbox-ai takes in (its /connect accounts stay in the sandbox's HOME): no
    `sandbox-ai login opencode2`, OpenCode 1's row stays."""
    assert "opencode2" not in backend.LOGIN_AGENTS_ANYWHERE and "opencode" in backend.LOGIN_AGENTS_ANYWHERE


def test_the_help_has_opencode2_in_both_languages():
    sections = {s.id: s for s in help_content.SECTIONS}
    section = sections["opencode2"]
    for name in ("models-catalog", "updates", "plugins", "web-search", "bun.report", "default-opencode2",
                 "coding-opencode2", "opencode2-local-llm", "docs/OPENCODE2_REVIEW.md", "sandbox-ai opencode"):
        assert name in section.body.en and name in section.body.de, name
    exported = json.loads((REPO / "sandbox/web/help.json").read_text())
    assert "opencode2" in {entry["id"] for entry in exported["sections"]}


def test_the_presets_are_agents_and_not_marked(sai):
    presets = sai.launcher.load_presets_module()
    catalog = presets.catalog()
    for name in ("default-opencode2", "coding-opencode2", "opencode2-local-llm"):
        assert catalog[name]["category"] == "agents" and catalog[name]["agents"] == ["opencode2"]
        assert "experimental" not in presets.public(catalog[name])
