"""OpenHands in the app: OpenHands in the agent choice with its model and effort lists, --openhands-enable on the
Board's agent node (a problem for another agent, its reach in the capabilities: the automation backend warned), its
words in both languages, no sign-in page and no login row of its own, the help section OpenHands (exported to the web
page too), its presets in the category Agents with the "not live-tested yet" badge (a note, never a gate)."""
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


def test_openhands_is_an_agent_with_its_model_and_effort(sai):
    assert "openhands" in sai.agents and "openhands" in backend.AGENTS and "openhands" in B.AGENTS
    question = next(q for q in guide.questions("agent") if q.key == "agent")
    assert ("openhands", "OpenHands") in question.choices
    assert guide.AGENT_EFFORTS["openhands"] == sai.launcher.AGENT_EFFORTS["openhands"] == (
        "none", "low", "medium", "high", "xhigh")
    assert guide.AGENT_MODELS["openhands"] == sai.launcher.AGENT_MODELS["openhands"]
    assert T.PROVIDERS["openhands"] == sai.launcher.PROVIDERS["openhands"] == []
    assert ui.HELPER_TITLES["openhands"] == "OpenHands" and W.AGENT_NAMES["openhands"] == "OpenHands"


def test_openhands_enable_on_the_board(sai):
    assert "--openhands-enable" in B.KINDS["agent"].options and B.OWNER["--openhands-enable"] == "agent"
    assert labels.OPTION_LABELS["--openhands-enable"][1].startswith("OpenHands:")
    assert "--openhands-enable" in backend.GROUPS["Agent"]

    def errors(agent, argv):
        return [p.text for p in B.problems(sai, B.from_argv(sai, agent, argv)) if p.level == "error"]
    assert any("for OpenHands only" in text for text in errors("claude", ["--openhands-enable", "automations"]))
    assert not any("for OpenHands only" in text for text in errors("openhands", ["--openhands-enable", "automations"]))
    board = B.from_argv(sai, "openhands", ["--openhands-enable", "model-catalog", "--openhands-enable", "automations"])
    assert B.values(board, sai)[1]["--openhands-enable"] == ["model-catalog", "automations"]
    assert B.to_argv(sai, board).count("--openhands-enable") == 2
    reach = {c.text: c.level for c in B.capabilities(board) if c.area == "reach" and "OpenHands" in c.text}
    assert reach == {"OpenHands: model-catalog turned on (their hosts open)": "info",
                     "OpenHands: automations turned on (their hosts open)": "warn"}


def test_the_new_words_have_german():
    for key in ("--openhands-enable is for OpenHands only.", "OpenHands: {features} turned on (their hosts open)"):
        assert key in i18n.TEXTS and i18n.TEXTS[key]["de"]


def test_no_login_and_no_sign_in_page_for_openhands(sai):
    """OpenHands has no login and no provider of its own: nothing to sign in to, no row on the Credentials page."""
    assert not [link for link in W.quick_links(sai.launcher) if link[0] == "openhands"]
    assert "openhands" not in sai.module("login").AGENTS
    assert "openhands" not in {row[0] for row in sai.login_status()}
    with pytest.raises(ValueError):
        sai.login_argv("ct1", "openhands")


def test_the_help_has_openhands_in_both_languages():
    sections = {s.id: s for s in help_content.SECTIONS}
    openhands = sections["openhands"]
    for name in ("automations", "model-catalog", "z.openhands.dev", "*.posthog.com", "/etc/openhands/settings.json",
                 "coding-openhands", "openhands-local-llm", "127.0.0.1:8000", "--browser", "Docker"):
        assert name in openhands.body.en and name in openhands.body.de, name
    assert "`openhands`" in sections["llm-openrouter"].body.en and "`openhands`" in sections["llm-openrouter"].body.de
    exported = json.loads((REPO / "sandbox/web/help.json").read_text())
    assert "openhands" in {section["id"] for section in exported["sections"]}


def test_the_presets_are_in_the_agents_category_with_their_badge(sai):
    catalog = sai.launcher.load_presets_module().catalog()
    agents = {name for name, preset in catalog.items() if preset["category"] == "agents"}
    assert {"coding-openhands", "openhands-local-llm"} <= agents
    presets = sai.launcher.load_presets_module()
    for name in ("coding-openhands", "openhands-local-llm"):
        assert presets.experimental(catalog[name]) == {"names": ["openhands"]}, name
    assert catalog["coding-openhands"]["translations"]["de"]["title"] == "Programmieren mit OpenHands"
