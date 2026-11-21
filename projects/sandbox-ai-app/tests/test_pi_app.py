"""Pi in the app: in the agent choice (its thinking levels as efforts), --pi-enable on the Board's agent node (a problem
for another agent, its reach in the capabilities: packages and share warned), its words in both languages, its login
(`sandbox-ai login pi`: a Credentials row and the frontier profile's sign-in), the help section Pi (exported to the web
page too), its presets in the category Agents with the "not live-tested yet" badge, a note that never gates a start."""
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


def test_pi_is_an_agent(sai):
    assert "pi" in sai.agents and "pi" in backend.AGENTS and "pi" in B.AGENTS
    question = next(q for q in guide.questions("agent") if q.key == "agent")
    assert ("pi", "Pi") in question.choices
    assert guide.AGENT_EFFORTS["pi"] == sai.launcher.AGENT_EFFORTS["pi"]
    assert "xhigh" in guide.AGENT_EFFORTS["pi"] and "off" in guide.AGENT_EFFORTS["pi"]
    assert guide.AGENT_MODELS["pi"] == sai.launcher.AGENT_MODELS["pi"]
    assert T.PROVIDERS["pi"] == sai.launcher.PROVIDERS["pi"] == []
    assert ui.HELPER_TITLES["pi"] == "Pi" and W.AGENT_NAMES["pi"] == "Pi"


def test_pi_enable_on_the_board(sai):
    assert "--pi-enable" in B.KINDS["agent"].options and B.OWNER["--pi-enable"] == "agent"
    assert labels.OPTION_LABELS["--pi-enable"][1].startswith("Pi:")
    assert "--pi-enable" in backend.GROUPS["Agent"]

    def errors(agent, argv):
        return [p.text for p in B.problems(sai, B.from_argv(sai, agent, argv)) if p.level == "error"]
    assert any("for Pi only" in text for text in errors("claude", ["--pi-enable", "updates"]))
    assert not any("for Pi only" in text for text in errors("pi", ["--pi-enable", "updates"]))
    board = B.from_argv(sai, "pi", ["--pi-enable", "updates", "--pi-enable", "packages"])
    assert B.values(board, sai)[1]["--pi-enable"] == ["updates", "packages"]
    assert B.to_argv(sai, board).count("--pi-enable") == 2
    reach = {c.text: c.level for c in B.capabilities(board) if c.area == "reach" and c.text.startswith("Pi:")}
    assert reach == {"Pi: updates turned on (their hosts open)": "info",
                     "Pi: packages turned on (their hosts open)": "warn"}


def test_the_new_words_have_german():
    for key in ("Pi", "--pi-enable is for Pi only.", "Pi: {features} turned on (their hosts open)"):
        assert key in i18n.TEXTS and i18n.TEXTS[key]["de"]


def test_pi_has_a_login_row_and_the_frontier_sign_in(sai):
    """Pi signs in with its /login (sandbox-ai login pi keeps it once for every Pi sandbox): a Credentials row, and
    the login sandbox gets the frontier profile's hosts as OpenCode's does."""
    assert "pi" in sai.login_agents() and "pi" in backend.LOGIN_AGENTS_ANYWHERE
    assert any(row[0] == "pi" for row in sai.login_status())
    assert "pi" in T.LOGIN_FRONTIER and sai.launcher.load_login_module().LOGIN_OPTIONS["pi"] == ["--profile",
                                                                                                "frontier"]
    assert sai.login_argv("local", "pi")[-1] == "pi"


def test_the_help_has_pi_in_both_languages():
    sections = {s.id: s for s in help_content.SECTIONS}
    pi = sections["pi"]
    for name in ("model-catalog", "updates", "packages", "share", "PI_TELEMETRY=0", "coding-pi", "pi-local-llm",
                 "docs/PI_REVIEW.md", "sandbox-ai login pi", "badlogic/pi-mono"):
        assert name in pi.body.en and name in pi.body.de, name
    assert "`pi`" in sections["llm-openrouter"].body.en and "`pi`" in sections["llm-openrouter"].body.de
    exported = json.loads((REPO / "sandbox/web/help.json").read_text())
    assert "pi" in {section["id"] for section in exported["sections"]}


def test_the_presets_are_agents_and_marked_not_live_tested(sai):
    presets = sai.launcher.load_presets_module()
    catalog = presets.catalog()
    for name in ("default-pi", "coding-pi", "pi-local-llm"):
        assert catalog[name]["category"] == "agents" and catalog[name]["agents"] == ["pi"]
        assert presets.experimental(catalog[name]) == {"names": ["pi"]}, "a badge, never a gate"
    assert sai.launcher.experimental_needs(["pi"]) == ["pi"]
