"""The Board's LLM part and local models' new settings (0.1.98, decided 2026-11-17): every model profile's ceiling
for one answer (max_tokens) is shown with the profile's values; Claude Code's tools that local models never use are
left out by the connection proxy (--llm-tools core), and the LLM part has the field to keep all or choose
(--llm-tools all|core|TOOL[,TOOL...]), which the real ./sandbox-ai takes and its dry run shows; a profile's own
"tools" is shown with "your value wins" when the board sets it."""
import json
import os
from pathlib import Path
import subprocess

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, board as B, i18n, labels, model_profile as MP, widgets  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")
ROOT = Path(os.environ.get("SANDBOX_AI_HOME") or Path(__file__).resolve().parents[3])
QCN = "qwen/qwen3-coder-next"


@pytest.fixture
def homes(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    (tmp_path / "cwd").mkdir()
    monkeypatch.chdir(tmp_path / "cwd")
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    return tmp_path


@pytest.fixture
def page(qapp, homes):
    from sandbox_ai_app.board_view import BoardPage
    sai = backend.SandboxAI()
    assert sai.module("llm").main(["add", "studio", "--kind", "lmstudio", "--url", "http://127.0.0.1:9", "--model",
                                   QCN], out=lambda line: None) == 0
    view = BoardPage(sai, lambda *a, **kw: None, lambda: {"board_grid": 20})
    view.llm_servers = [{"name": "studio", "kind": "lmstudio", "model": QCN, "models": [QCN], "params": {},
                         "loop_guard_own": {}}]
    yield view
    view.deleteLater()


def with_model(page, agent, model):
    B.switch_agent(page.board.first("agent").props, agent)
    page.choose_llm_server({"name": "studio", "model": model, "only": True})
    page.inspect(page.board.first("llm"))


def dry_run(agent, argv):
    done = subprocess.run([str(ROOT / "sandbox-ai"), agent, *argv, "--dry-run"], capture_output=True, text=True,
                          timeout=120, env={**os.environ, "SANDBOX_AI_LLM_NO_CONTEXT_FETCH": "1"})
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout), done.stderr


def test_the_profile_shows_the_ceiling_of_one_answer(page):
    with_model(page, "opencode", QCN)
    assert "max. output tokens (max_tokens): 16384" in page.profile_values.text().splitlines()


def test_the_llm_part_chooses_claude_codes_tools_and_the_real_launcher_takes_it(page):
    assert "--llm-tools" in B.KINDS["llm"].options and "--llm-tools" in labels.OPTION_LABELS
    with_model(page, "claude", QCN)
    plan, stderr = dry_run("claude", B.to_argv(page.sai, page.board))
    assert plan["llm"]["claude_tools"] == {"tools": "core", "from": "default",
                                           "kept": ["Bash", "Read", "Edit", "Write", "Glob", "Grep", "TodoWrite",
                                                    "ListMcpResourcesTool", "ReadMcpResourceTool",
                                                    "ReadMcpResourceDirTool"]}
    assert "Claude Code's tools that local models do not use are left out" in stderr
    page.set_prop(page.board.first("llm").id, "--llm-tools", "all")
    argv = B.to_argv(page.sai, page.board)
    assert argv[argv.index("--llm-tools") + 1] == "all"
    plan, _ = dry_run("claude", argv)
    assert plan["llm"]["claude_tools"]["tools"] == "all" and plan["llm"]["claude_tools"]["kept"] is None
    again = B.from_argv(page.sai, "claude", argv)
    assert again.first("llm").props.get("--llm-tools") == "all", "back from the command line (a project file)"


def test_a_profiles_tools_are_shown_and_yours_win():
    profile = {"name": "x × claude", "agent": "claude", "params": {"max_tokens": 8192}, "loop_guard": {}, "env": {},
               "tools": ["core", "WebFetch"]}
    lines = MP.value_lines(profile, mine={"tools"})
    assert "Claude Code's tools (--llm-tools) (tools): core,WebFetch — your value wins" in lines
    assert "max. output tokens (max_tokens): 8192" in lines
    assert "tools" not in MP.settings({**profile, "agent": "opencode"}), "Claude Code's alone"


def test_the_new_texts_are_in_both_languages():
    for key in ("Claude Code's tools (--llm-tools)",
                "The very same request the server rejected this many times (HTTP 4xx) is not sent again: the agent "
                "gets sandbox-ai's error at once (0: off)"):
        assert i18n.TEXTS[key]["de"], key
    assert labels.OPTION_LABELS["--llm-tools"][1].startswith("Tools von Claude Code")
