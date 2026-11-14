"""Qwen Code and DeepSeek Harness in the app: the agent choice and their model and effort lists, --qwen-enable on the
Board's agent node (a problem for another agent, its reach in the capabilities), their words in both languages, the
login browser's DeepSeek and Qwen pages (no API host), the help sections (exported to the web page too), the
presets of the category Agents in the gallery."""
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


@pytest.mark.parametrize("agent,title", [("qwen", "Qwen Code"), ("dsh", "DeepSeek Harness")])
def test_both_are_agents_with_their_model_and_effort(sai, agent, title):
    assert agent in sai.agents and agent in backend.AGENTS and agent in B.AGENTS
    question = next(q for q in guide.questions("agent") if q.key == "agent")
    assert (agent, title) in question.choices
    assert guide.AGENT_EFFORTS[agent] == sai.launcher.AGENT_EFFORTS[agent]
    assert guide.AGENT_MODELS[agent] == sai.launcher.AGENT_MODELS[agent]
    assert T.PROVIDERS[agent] == sai.launcher.PROVIDERS[agent]


def test_qwen_enable_on_the_board(sai):
    assert "--qwen-enable" in B.KINDS["agent"].options and B.OWNER["--qwen-enable"] == "agent"
    assert "--qwen-enable" in labels.OPTION_LABELS and labels.OPTION_LABELS["--qwen-enable"][1].startswith("Qwen Code:")

    def errors(agent, argv):
        return [p.text for p in B.problems(sai, B.from_argv(sai, agent, argv)) if p.level == "error"]
    assert any("for Qwen Code only" in text for text in errors("claude", ["--qwen-enable", "updates"]))
    assert not any("for Qwen Code only" in text for text in errors("qwen", ["--qwen-enable", "updates"]))
    board = B.from_argv(sai, "qwen", ["--qwen-enable", "updates", "--qwen-enable", "extensions"])
    assert B.values(board, sai)[1]["--qwen-enable"] == ["updates", "extensions"]
    assert B.to_argv(sai, board).count("--qwen-enable") == 2
    reach = {c.text: c.level for c in B.capabilities(board) if c.area == "reach" and "Qwen" in c.text}
    assert reach == {"Qwen Code: updates turned on (their hosts open)": "info",
                     "Qwen Code: extensions turned on (their hosts open)": "info"}


def test_the_new_words_have_german():
    for key in ("DeepSeek Harness", "Qwen Code", "--qwen-enable is for Qwen Code only.",
                "Qwen Code: {features} turned on (their hosts open)"):
        assert key in i18n.TEXTS and i18n.TEXTS[key]["de"]


def test_the_login_browser_offers_the_sign_in_pages_not_the_apis(sai):
    links = {(agent, host): url for agent, host, url in W.quick_links(sai.launcher) if agent in ("dsh", "qwen")}
    assert links == {("dsh", "platform.deepseek.com"): "https://platform.deepseek.com/sign_in",
                     ("qwen", "chat.qwen.ai"): "https://chat.qwen.ai/auth"}
    assert T.login_agent(["sandbox-ai", "login", "qwen"]) == "qwen"


def test_the_help_has_both_in_both_languages():
    sections = {s.id: s for s in help_content.SECTIONS}
    qwen, dsh = sections["qwen"], sections["dsh"]
    for name in ("updates", "model-catalog", "extensions", "*.rum.aliyuncs.com", "/etc/qwen-code/settings.json"):
        assert name in qwen.body.en and name in qwen.body.de, name
    for text in ("127.0.0.1:3080", "headless", "dsh-otel-collector.deepseeksvc.com", "/etc/dsh/sandbox-ai.patch.yml"):
        assert text in dsh.body.en and text in dsh.body.de, text
    assert "qwen" in sections["dsh"].see and "dsh" in sections["qwen"].see
    assert "`qwen`" in sections["llm-openrouter"].body.en and "`qwen`" in sections["llm-openrouter"].body.de
    exported = json.loads((REPO / "sandbox/web/help.json").read_text())
    assert {"qwen", "dsh"} <= {section["id"] for section in exported["sections"]}


def test_the_agents_presets_are_in_their_category(sai):
    catalog = sai.launcher.load_presets_module().catalog()
    agents = sorted(name for name, preset in catalog.items() if preset["category"] == "agents")
    assert {"coding-qwen", "qwen-local-llm", "coding-dsh", "dsh-local-llm", "coding-claude"} <= set(agents)
    info = sai.launcher.load_presets_module().CATEGORY_INFO["agents"]
    assert info["title"] == {"en": "Agents", "de": "Agenten"}
