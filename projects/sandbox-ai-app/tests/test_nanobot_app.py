"""nanobot in the app: nanobot in the agent choice with its model and effort lists, --nanobot-enable on the Board's
agent node (a problem for another agent, its reach in the capabilities: the Telegram channel and the scheduler
warned), its words in both languages, no sign-in page of its own in the login browser and no row on the Credentials
page (it has no login), its presets without a badge (the user, 2026-11-22), the
help section nanobot (exported to the web page too), its presets in the category Agents."""
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


def test_nanobot_is_an_agent_with_its_model_and_effort(sai):
    assert "nanobot" in sai.agents and "nanobot" in backend.AGENTS and "nanobot" in B.AGENTS
    question = next(q for q in guide.questions("agent") if q.key == "agent")
    assert ("nanobot", "nanobot") in question.choices
    assert guide.AGENT_EFFORTS["nanobot"] == sai.launcher.AGENT_EFFORTS["nanobot"] == (
        "none", "low", "medium", "high", "xhigh", "max")
    assert guide.AGENT_MODELS["nanobot"] == sai.launcher.AGENT_MODELS["nanobot"]
    assert T.PROVIDERS["nanobot"] == sai.launcher.PROVIDERS["nanobot"] == []
    assert ui.HELPER_TITLES["nanobot"] == "nanobot" and W.AGENT_NAMES["nanobot"] == "nanobot"


def test_nanobot_enable_on_the_board(sai):
    assert "--nanobot-enable" in B.KINDS["agent"].options and B.OWNER["--nanobot-enable"] == "agent"
    assert labels.OPTION_LABELS["--nanobot-enable"][1].startswith("nanobot:")
    assert "--nanobot-enable" in backend.GROUPS["Agent"]

    def errors(agent, argv):
        return [p.text for p in B.problems(sai, B.from_argv(sai, agent, argv)) if p.level == "error"]
    assert any("for nanobot only" in text for text in errors("claude", ["--nanobot-enable", "updates"]))
    assert not any("for nanobot only" in text for text in errors("nanobot", ["--nanobot-enable", "updates"]))
    board = B.from_argv(sai, "nanobot", ["--nanobot-enable", "web-search", "--nanobot-enable", "gateway:telegram",
                                         "--nanobot-enable", "scheduler"])
    assert B.values(board, sai)[1]["--nanobot-enable"] == ["web-search", "gateway:telegram", "scheduler"]
    assert B.to_argv(sai, board).count("--nanobot-enable") == 3
    reach = {c.text: c.level for c in B.capabilities(board) if c.area == "reach" and "nanobot" in c.text}
    assert reach == {"nanobot: web-search turned on (their hosts open)": "info",
                     "nanobot: gateway:telegram turned on (their hosts open)": "warn",
                     "nanobot: scheduler turned on (their hosts open)": "warn"}


def test_the_new_words_have_german():
    for key in ("nanobot", "--nanobot-enable is for nanobot only.", "nanobot: {features} turned on (their hosts open)"):
        assert key in i18n.TEXTS and i18n.TEXTS[key]["de"]


def test_nanobot_has_no_sign_in_page_and_no_credentials_row(sai):
    """nanobot has no login in sandbox-ai and no provider of its own: nothing to sign in to, no Log in row."""
    assert not [link for link in W.quick_links(sai.launcher) if link[0] == "nanobot"]
    assert "nanobot" not in sai.module("login").AGENTS and "nanobot" not in backend.LOGIN_AGENTS_ANYWHERE
    assert "nanobot" not in {row[0] for row in sai.login_status()}
    with pytest.raises(ValueError):
        sai.login_argv("ct1", "nanobot")


def test_nanobot_is_not_marked(sai):
    """No list of parts not live-tested in the launcher, no badge on its presets."""
    assert not hasattr(sai.launcher, "EXPERIMENTAL_PARTS")
    presets = sai.launcher.load_presets_module()
    catalog = presets.catalog()
    for name in ("coding-nanobot", "nanobot-local-llm"):
        assert "experimental" not in presets.public(catalog[name]), name


def test_the_help_has_nanobot_in_both_languages():
    sections = {s.id: s for s in help_content.SECTIONS}
    nanobot = sections["nanobot"]
    for name in ("scheduler", "gateway:telegram", "web-search", "web-reader", "updates", "*.langfuse.com",
                 "/etc/nanobot/sandbox-ai.json", "127.0.0.1:8765", "coding-nanobot", "nanobot-local-llm"):
        assert name in nanobot.body.en and name in nanobot.body.de, name
    assert "`nanobot`" in sections["llm-openrouter"].body.en and "`nanobot`" in sections["llm-openrouter"].body.de
    exported = json.loads((REPO / "sandbox/web/help.json").read_text())
    assert "nanobot" in {section["id"] for section in exported["sections"]}


def test_the_presets_are_in_their_category(sai):
    catalog = sai.launcher.load_presets_module().catalog()
    agents = {name for name, preset in catalog.items() if preset["category"] == "agents"}
    assert {"coding-nanobot", "nanobot-local-llm"} <= agents
    assert catalog["coding-nanobot"]["translations"]["de"]["title"] == "Programmieren mit nanobot"
