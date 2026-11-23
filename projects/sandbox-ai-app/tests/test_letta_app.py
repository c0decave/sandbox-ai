"""Letta Code in the app: in the agent choice (no model list or effort of its own), --letta-enable on the Board's agent
node (a problem for another agent, its reach in the capabilities: Letta Cloud warned), its words in both languages,
no sign-in page of its own in the login browser and no Credentials row (no login sandbox-ai takes in), the help
section Letta Code (exported to the web page too), its presets in the category Agents (no badge: the user,
2026-11-22)."""
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


def test_letta_is_an_agent(sai):
    assert "letta" in sai.agents and "letta" in backend.AGENTS and "letta" in B.AGENTS
    question = next(q for q in guide.questions("agent") if q.key == "agent")
    assert ("letta", "Letta Code") in question.choices
    assert guide.AGENT_EFFORTS["letta"] == sai.launcher.AGENT_EFFORTS["letta"] == ()
    assert guide.AGENT_MODELS["letta"] == sai.launcher.AGENT_MODELS["letta"]
    assert T.PROVIDERS["letta"] == sai.launcher.PROVIDERS["letta"] == []
    assert ui.HELPER_TITLES["letta"] == "Letta Code" and W.AGENT_NAMES["letta"] == "Letta Code"


def test_letta_enable_on_the_board(sai):
    assert "--letta-enable" in B.KINDS["agent"].options and B.OWNER["--letta-enable"] == "agent"
    assert labels.OPTION_LABELS["--letta-enable"][1].startswith("Letta Code:")
    assert "--letta-enable" in backend.GROUPS["Agent"]

    def errors(agent, argv):
        return [p.text for p in B.problems(sai, B.from_argv(sai, agent, argv)) if p.level == "error"]
    assert any("for Letta Code only" in text for text in errors("claude", ["--letta-enable", "updates"]))
    assert not any("for Letta Code only" in text for text in errors("letta", ["--letta-enable", "updates"]))
    board = B.from_argv(sai, "letta", ["--letta-enable", "updates", "--letta-enable", "cloud"])
    assert B.values(board, sai)[1]["--letta-enable"] == ["updates", "cloud"]
    assert B.to_argv(sai, board).count("--letta-enable") == 2
    reach = {c.text: c.level for c in B.capabilities(board) if c.area == "reach" and "Letta Code" in c.text}
    assert reach == {"Letta Code: updates turned on (their hosts open)": "info",
                     "Letta Code: cloud turned on (their hosts open)": "warn"}


def test_the_new_words_have_german():
    for key in ("Letta Code", "--letta-enable is for Letta Code only.",
                "Letta Code: {features} turned on (their hosts open)"):
        assert key in i18n.TEXTS and i18n.TEXTS[key]["de"]


def test_no_login_page_and_no_credentials_row_for_letta(sai):
    """Letta Code has no login sandbox-ai takes in (its Letta Cloud sign-in stays in the sandbox) and no provider of
    its own: nothing to sign in to, no `sandbox-ai login letta`."""
    assert not [link for link in W.quick_links(sai.launcher) if link[0] == "letta"]
    assert "letta" not in backend.LOGIN_AGENTS_ANYWHERE


def test_the_help_has_letta_in_both_languages():
    sections = {s.id: s for s in help_content.SECTIONS}
    letta = sections["letta"]
    for name in ("cloud", "updates", "skills-install", "~/.letta/lc-local-backend", "coding-letta", "letta-local-llm",
                 "docs/LETTA_REVIEW.md", "api.letta.com"):
        assert name in letta.body.en and name in letta.body.de, name
    assert "`letta`" in sections["llm-openrouter"].body.en and "`letta`" in sections["llm-openrouter"].body.de
    exported = json.loads((REPO / "sandbox/web/help.json").read_text())
    assert "letta" in {section["id"] for section in exported["sections"]}


def test_the_presets_are_agents_and_not_marked(sai):
    presets = sai.launcher.load_presets_module()
    catalog = presets.catalog()
    for name in ("coding-letta", "letta-local-llm"):
        assert catalog[name]["category"] == "agents" and catalog[name]["agents"] == ["letta"]
        assert "experimental" not in presets.public(catalog[name])
    assert not hasattr(sai.launcher, "experimental_needs")
