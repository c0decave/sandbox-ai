"""Goose and Cairn in the app: Goose in the agent choice with its model and effort lists, --goose-enable on the Board's
agent node (a problem for another agent, its reach in the capabilities: the gateway and the scheduler warned), its
words in both languages, no sign-in page of its own in the login browser, the help sections Goose and Cairn (exported to
the web page too), its presets in the category Agents and Cairn's preset in Security."""
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


def test_goose_is_an_agent_with_its_model_and_effort(sai):
    assert "goose" in sai.agents and "goose" in backend.AGENTS and "goose" in B.AGENTS
    question = next(q for q in guide.questions("agent") if q.key == "agent")
    assert ("goose", "Goose") in question.choices
    assert guide.AGENT_EFFORTS["goose"] == sai.launcher.AGENT_EFFORTS["goose"] == ("off", "low", "medium", "high",
                                                                                   "max")
    assert guide.AGENT_MODELS["goose"] == sai.launcher.AGENT_MODELS["goose"]
    assert T.PROVIDERS["goose"] == sai.launcher.PROVIDERS["goose"] == []
    assert ui.HELPER_TITLES["goose"] == "Goose" and W.AGENT_NAMES["goose"] == "Goose"


def test_goose_enable_on_the_board(sai):
    assert "--goose-enable" in B.KINDS["agent"].options and B.OWNER["--goose-enable"] == "agent"
    assert labels.OPTION_LABELS["--goose-enable"][1].startswith("Goose:")
    assert "--goose-enable" in backend.GROUPS["Agent"]

    def errors(agent, argv):
        return [p.text for p in B.problems(sai, B.from_argv(sai, agent, argv)) if p.level == "error"]
    assert any("for Goose only" in text for text in errors("claude", ["--goose-enable", "updates"]))
    assert not any("for Goose only" in text for text in errors("goose", ["--goose-enable", "updates"]))
    board = B.from_argv(sai, "goose", ["--goose-enable", "model-catalog", "--goose-enable", "gateway:telegram",
                                       "--goose-enable", "scheduler"])
    assert B.values(board, sai)[1]["--goose-enable"] == ["model-catalog", "gateway:telegram", "scheduler"]
    assert B.to_argv(sai, board).count("--goose-enable") == 3
    reach = {c.text: c.level for c in B.capabilities(board) if c.area == "reach" and "Goose" in c.text}
    assert reach == {"Goose: model-catalog turned on (their hosts open)": "info",
                     "Goose: gateway:telegram turned on (their hosts open)": "warn",
                     "Goose: scheduler turned on (their hosts open)": "warn"}


def test_the_new_words_have_german():
    for key in ("Goose", "--goose-enable is for Goose only.", "Goose: {features} turned on (their hosts open)"):
        assert key in i18n.TEXTS and i18n.TEXTS[key]["de"]


def test_the_login_browser_has_no_page_for_goose(sai):
    """Goose has no login and no provider of its own: nothing to sign in to."""
    assert not [link for link in W.quick_links(sai.launcher) if link[0] == "goose"]


def test_the_help_has_goose_and_cairn_in_both_languages():
    sections = {s.id: s for s in help_content.SECTIONS}
    goose, cairn = sections["goose"], sections["cairn"]
    for name in ("scheduler", "gateway:telegram", "local-inference", "model-catalog", "updates", "*.posthog.com",
                 "/etc/goose/config.yaml", "coding-goose", "goose-local-llm"):
        assert name in goose.body.en and name in goose.body.de, name
    for text in ("cairn-pentest-board", "127.0.0.1:8000", "AGPL", "Docker", "sandbox-ai build cairn"):
        assert text in cairn.body.en and text in cairn.body.de, text
    assert "`goose`" in sections["llm-openrouter"].body.en and "`goose`" in sections["llm-openrouter"].body.de
    exported = json.loads((REPO / "sandbox/web/help.json").read_text())
    assert {"goose", "cairn"} <= {section["id"] for section in exported["sections"]}


def test_the_presets_are_in_their_categories(sai):
    catalog = sai.launcher.load_presets_module().catalog()
    agents = {name for name, preset in catalog.items() if preset["category"] == "agents"}
    assert {"coding-goose", "goose-local-llm"} <= agents
    assert catalog["cairn-pentest-board"]["category"] == "security"
    assert catalog["cairn-pentest-board"]["translations"]["de"]["title"].startswith("Cairn")
