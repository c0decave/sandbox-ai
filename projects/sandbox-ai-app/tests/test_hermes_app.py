"""Hermes Agent in the app: the agent choice and its model and effort lists, --hermes-enable on the Board's agent node
(a problem for another agent, its reach in the capabilities), its words in both languages, the login browser's Nous
Portal page, and the help sections Hermes and OpenRouter (exported to the web page too)."""
import json
import os
from pathlib import Path

import pytest

from sandbox_ai_app import backend, board as B, guide, help_content, i18n, labels, terminal_text as T
from sandbox_ai_app import web_browser as W

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]
pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


@pytest.fixture(scope="module")
def sai():
    return backend.SandboxAI()


def test_hermes_is_an_agent_with_its_model_and_effort(sai):
    assert "hermes" in sai.agents and "hermes" in backend.AGENTS and "hermes" in B.AGENTS
    question = next(q for q in guide.questions("agent") if q.key == "agent")
    assert ("hermes", "Hermes Agent") in question.choices
    assert guide.AGENT_EFFORTS["hermes"] == sai.launcher.AGENT_EFFORTS["hermes"]
    assert "--hermes-enable" in B.KINDS["agent"].options and B.OWNER["--hermes-enable"] == "agent"
    assert "--hermes-enable" in labels.OPTION_LABELS


def test_hermes_enable_on_the_board(sai):
    def errors(agent, argv):
        return [p.text for p in B.problems(sai, B.from_argv(sai, agent, argv)) if p.level == "error"]
    assert any("for Hermes only" in text for text in errors("claude", ["--hermes-enable", "kanban"]))
    assert not any("for Hermes only" in text for text in errors("hermes", ["--hermes-enable", "kanban"]))
    board = B.from_argv(sai, "hermes", ["--hermes-enable", "gateway:telegram", "--hermes-enable", "kanban"])
    assert B.values(board, sai)[1]["--hermes-enable"] == ["gateway:telegram", "kanban"]
    assert B.to_argv(sai, board).count("--hermes-enable") == 2
    reach = {c.text: c.level for c in B.capabilities(board) if c.area == "reach" and "Hermes" in c.text}
    assert reach == {"Hermes: gateway:telegram turned on (their hosts open)": "warn",
                     "Hermes: kanban turned on (their hosts open)": "info"}


def test_the_new_words_have_german(monkeypatch):
    for key in ("Hermes Agent", "--hermes-enable is for Hermes only.",
                "Hermes: {features} turned on (their hosts open)"):
        assert key in i18n.TEXTS and i18n.TEXTS[key]["de"]
    assert labels.OPTION_LABELS["--hermes-enable"][1].startswith("Hermes:")


def test_the_login_browser_offers_the_nous_portal_not_its_api(sai):
    assert T.PROVIDERS["hermes"] == sai.launcher.PROVIDERS["hermes"]
    links = [(agent, host, url) for agent, host, url in W.quick_links(sai.launcher) if agent == "hermes"]
    assert links == [("hermes", "portal.nousresearch.com", "https://portal.nousresearch.com/")]
    assert T.login_agent(["sandbox-ai", "login", "hermes"]) == "hermes"


def test_the_help_has_hermes_and_openrouter_in_both_languages():
    sections = {s.id: s for s in help_content.SECTIONS}
    openrouter, hermes = sections["llm-openrouter"], sections["hermes"]
    command = "sandbox-ai llm add openrouter -k openai-compatible -u https://openrouter.ai/api/v1 -s SECRET -m MODEL"
    assert command in openrouter.cli and command in openrouter.body.en and command in openrouter.body.de
    assert "https://openrouter.ai/api" in openrouter.body.en and "-a" in openrouter.body.de
    for name in ("gateway:telegram", "kanban", "skills-hub", "lazy-installs"):
        assert name in hermes.body.en and name in hermes.body.de
    assert "llm-openrouter" in sections["llm"].see and "hermes" in openrouter.see
    exported = json.loads((REPO / "sandbox/web/help.json").read_text())
    assert {"llm-openrouter", "hermes"} <= {section["id"] for section in exported["sections"]}
