"""Independent feature review 2 of sandbox-ai 0.1.95, the app's part: each test failed before its fix
(docs/releases/v0.1.95.md, "Feature review 2"). The core's tests are in the checkout's tests/test_feature_review2.py."""
import json
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, traffic_page  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    return tmp_path


def test_the_traffic_page_shows_a_request_of_any_shape_the_agent_sent(qapp, env):
    """The page reads a request with `sandbox-ai llm traffic ... --request N --json` and lays it out: a request whose
    message had a tool_calls that is no list (or a thinking block that is no text, a content nested hundreds of levels)
    - the agent's own JSON - ended that command in a traceback, and the page said only "TypeError: ..." for it."""
    logs = env / "logs"
    logs.mkdir()
    hostile = [{"role": "assistant", "tool_calls": 5, "content": "calls"},
               {"role": "assistant", "content": [{"type": "text", "text": "x"}], "tool_calls": True},
               {"role": "user", "content": json.loads("[" * 400 + '"deep"' + "]" * 400)}]
    record = {"version": 1, "n": 1, "time": "2026-11-13T10:00:00Z", "run": "r1", "api": "chat", "model": "m",
              "status": 200, "request": {"model": "m", "messages": hostile}, "response": {"text": "ok"}}
    (logs / "llm-traffic-20261113-100000-r1.jsonl").write_text(json.dumps(record) + "\n")
    page = traffic_page.TrafficPage.__new__(traffic_page.TrafficPage)
    page.sai = backend.SandboxAI()
    document, raw = page.fetch(["llm", "traffic", "--log-dir", str(logs), "r1", "--request", "1", "--json"])
    shown = traffic_page.chat_html(document["view"], new_only=False, toggled=set(), whole=set())
    assert "calls" in shown and "ok" in shown and "[nested too deep]" in shown
    assert '"n": 1' in raw
