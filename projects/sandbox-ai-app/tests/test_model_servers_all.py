"""Every registered model server in the agent's Model server choice (the Board's agent part, the launch page): a
server without --model whose models are not known (LM Studio not running, say) is listed with why - it used to vanish
-, the models of all servers are asked at once with a short timeout (one after another with a minute each, a few
servers that were down took longer than the whole read and the choice listed none), and the list shows before they
answered."""
import os
import threading
import time

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, widgets, wizard_llm as WL  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")
UP = {"name": "ollama", "kind": "ollama", "url": "http://127.0.0.1:11434", "models": ["qwen3"],
      "agents": ["claude", "codex", "opencode"]}
DOWN = {"name": "studio", "kind": "lmstudio", "url": "http://127.0.0.1:1234", "models": [],
        "agents": ["claude", "codex", "opencode"]}
DEFAULT = {"name": "gpu", "kind": "vllm", "url": "http://10.0.0.2:8000", "models": [], "model": "qwen3-coder",
           "agents": ["claude", "codex", "opencode"]}


@pytest.fixture(params=["happy", "sad", "edge"])
def scenario(request):
    return request.param


@pytest.fixture
def sai(qapp, tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    sai = backend.SandboxAI()
    return sai


def test_model_choices_list_every_server(scenario):
    if scenario == "happy":
        choices = WL.model_choices([UP, DOWN, DEFAULT], "codex")
        values = [value for _, value, _ in choices]
        assert values == ["", "ollama", "ollama:qwen3", "studio", "gpu"], "every registered server"
        hints = {value: hint for _, value, hint in choices}
        assert hints["ollama"] == "" and hints["gpu"] == "", "its only model, its default model: --llm NAME starts"
        assert "No models known yet" in hints["studio"] and "sandbox-ai llm check studio" in hints["studio"]
        assert "no models known" in [label for label, value, _ in choices if value == "studio"][0]
    elif scenario == "sad":
        # It was asked and did not answer: said so, with what to do.
        hint = {value: hint for _, value, hint in WL.model_choices([{**DOWN, "answered": False}], "codex")}["studio"]
        assert hint == ("No models known: the server did not answer - start it, or: sandbox-ai llm check studio")
        # The agent cannot use it at all: that reason comes first.
        claude = {**DOWN, "agents": ["codex", "opencode"]}
        assert "Anthropic" in {v: h for _, v, h in WL.model_choices([claude], "claude")}["studio"]
    else:
        # Several models, no default: NAME alone is refused by the launcher - only NAME:MODEL; nothing invalid listed.
        several = {**UP, "models": ["a", "b"]}
        assert [v for _, v, _ in WL.model_choices([several, {"name": "-bad"}, "junk"], "codex")] == \
            ["", "ollama:a", "ollama:b"]
        assert WL.launcher_takes_name(DEFAULT) and WL.launcher_takes_name(UP) and not WL.launcher_takes_name(DOWN)


def test_host_llm_servers_asks_every_server_at_once(sai, scenario, monkeypatch):
    names = [f"s{i}" for i in range(8)]
    monkeypatch.setattr(sai, "host_llm_endpoints", lambda host: [{"name": n, "kind": "ollama", "models": []}
                                                                  for n in names])
    running, most, asked = [0], [0], []
    lock = threading.Lock()

    def models(host, name, timeout=60):
        with lock:
            running[0] += 1
            most[0] = max(most[0], running[0])
            asked.append((host, name, timeout))
        try:
            time.sleep(0.3)
            if scenario == "sad" and name in ("s1", "s5"):
                raise ValueError(f"{name} does not answer")
            return [f"{name}-model"]
        finally:
            with lock:
                running[0] -= 1
    monkeypatch.setattr(sai, "host_llm_models", models)
    began = time.monotonic()
    found = sai.host_llm_servers("local" if scenario != "edge" else "ct1")
    took = time.monotonic() - began
    assert [e["name"] for e in found] == names
    assert most[0] > 1 and took < 0.3 * len(names) / 2, "asked at once, not one after another"
    timeout = backend.SandboxAI.LLM_MODELS_SECONDS
    if scenario == "happy":
        assert all(e["models"] == [e["name"] + "-model"] and e["answered"] for e in found)
        assert {t for _, _, t in asked} == {timeout}, "a short timeout each"
    elif scenario == "sad":
        down = {e["name"]: e for e in found if not e["answered"]}
        assert set(down) == {"s1", "s5"} and down["s1"]["models"] == [] and "does not answer" in down["s1"][
            "models_error"]
    else:
        assert {t for _, _, t in asked} == {timeout + 10}, "another host: a little more for SSH"
        assert sai.host_llm_servers("ct1", endpoints=[]) == []


def test_the_board_lists_the_servers_before_they_answered(sai, wait, monkeypatch):
    from sandbox_ai_app.board_view import BoardPage
    monkeypatch.setattr(sai, "llm_endpoints", lambda: [dict(UP), dict(DOWN)])
    release = threading.Event()

    def slow(host, endpoints=None, timeout=None):
        release.wait(10)
        return [{**dict(e), "answered": e["name"] == "ollama"} for e in endpoints]
    monkeypatch.setattr(sai, "host_llm_servers", slow)
    page = BoardPage(sai, lambda *a, **k: None, lambda: {"board_grid": 20})
    page.inspect(page.board.first("agent"))
    wait(lambda: page.llm_servers is not None)
    page.inspect(page.board.first("agent"))
    combo = page.llm_combo
    data = [combo.itemData(i) for i in range(combo.count())]
    assert data == ["", "ollama", "ollama:qwen3", "studio"], "listed at once"
    assert not combo.model().item(data.index("studio")).isEnabled()
    assert page.llm_asking is True
    release.set()
    wait(lambda: page.llm_asking is False)
    page.inspect(page.board.first("agent"))
    from PySide6.QtCore import Qt
    tip = page.llm_combo.itemData(3, Qt.ItemDataRole.ToolTipRole)
    assert "the server did not answer" in tip
    page.deleteLater()


def test_the_launch_page_lists_the_servers_before_they_answered(sai, wait, monkeypatch):
    from sandbox_ai_app.ui import LaunchPage
    monkeypatch.setattr(sai, "llm_endpoints", lambda: [dict(UP), dict(DOWN), dict(DEFAULT)])
    asked = []
    monkeypatch.setattr(sai, "host_llm_servers", lambda host, endpoints=None, timeout=None: asked.append(
        [e["name"] for e in endpoints]) or [{**e, "answered": False} for e in endpoints])
    page = LaunchPage(sai, lambda *a, **k: None)
    wait(lambda: asked and not page.llm_loading)
    page.agent.setCurrentText("codex")
    data = [page.model_choice.itemData(i) for i in range(page.model_choice.count())]
    assert data == ["", "ollama", "ollama:qwen3", "studio", "gpu"] and asked == [["ollama", "studio", "gpu"]]
    page.deleteLater()
