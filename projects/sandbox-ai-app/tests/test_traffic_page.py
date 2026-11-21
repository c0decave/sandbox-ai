"""The LLM traffic page, offscreen: the requests come from the real `sandbox-ai llm traffic` of the checkout in
SANDBOX_AI_HOME, read from a log folder written here (no sandbox runs): the list with its model, tokens and loop-guard
marks, a request as a chat with what is new, folding, the parameters with the relay's changes, the raw record, search
across the run, export as Markdown and JSON, follow, a window of its own (its [x] and Esc), escaped texts."""
import hashlib
import json
import os
from pathlib import Path

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6 import QtCore, QtGui  # noqa: E402
import shiboken6  # noqa: E402
from sandbox_ai_app import backend, traffic_page, widgets  # noqa: E402
from sandbox_ai_app.traffic_page import TrafficModel, chat_html, parameter_rows, traffic_runs  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
                          .encode("utf-8", "replace")).hexdigest()[:12]


def records(run="opencode-1a2b"):
    """Three requests of one conversation (the last cut by the loop guard), then one of another."""
    base = [{"role": "system", "content": "be brief"}, {"role": "user", "content": "list the files"}]
    second = base + [{"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "bash", "arguments": "{\"c\":\"ls\"}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "a.py\nb.py"}]
    third = second + [{"role": "assistant", "content": "a.py and b.py"},
                      {"role": "user", "content": "thanks\x1b[31m <b>bold</b>"}]
    found = []
    for number, (messages, answer) in enumerate(zip(
            (base, second, third), ({"tool_calls": [{"id": "c1", "name": "bash", "arguments": "{\"c\":\"ls\"}"}],
                                     "finish_reason": "tool_calls"},
                                    {"text": "a.py and b.py", "finish_reason": "stop"},
                                    {"text": "You're welcome", "finish_reason": "stop"})), 1):
        found.append({"version": 1, "n": number, "time": f"2026-11-13T10:00:0{number}.500Z", "run": run,
                      "source": "llm", "endpoint": "studio", "host": "127.0.0.1:1234", "api": "chat",
                      "model": "qwen3-coder", "stream": True, "status": 200, "ms": 1500 * number,
                      "digests": [digest("")] + [digest(m) for m in messages],
                      "request": {"model": "qwen3-coder", "temperature": 0.7, "max_tokens": 2048, "stream": True,
                                  "messages": messages},
                      "response": {**answer, "usage": {"prompt_tokens": 100 * number, "completion_tokens": number}},
                      "changes": [{"field": "temperature", "from": 0.1, "to": 0.7, "why": "parameter"},
                                  {"field": "max_tokens", "to": 2048, "why": "parameter"}]})
    found[2]["loop"] = {"action": "answer stopped", "kind": "text", "detail": "repeated"}
    found.append({"version": 1, "n": 4, "time": "2026-11-13T10:00:09.000Z", "run": run, "api": "chat",
                  "model": "qwen3-coder", "status": 500, "ms": 30, "request": {"model": "qwen3-coder",
                                                                               "messages": [{"role": "user",
                                                                                             "content": "title?"}]},
                  "response": {"error": {"message": "overloaded"}}})
    return found


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    return tmp_path


@pytest.fixture
def window(qapp, env, monkeypatch):
    from sandbox_ai_app import terminal
    from sandbox_ai_app.ui import MainWindow
    monkeypatch.setattr(terminal, "available", lambda web=None: (False, "not in this test"))
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    w = MainWindow(backend.SandboxAI(), check_updates=False, approvals_interval=0)
    if w.tray is not None:
        w.tray.hide()
        w.tray = None
    yield w
    w.shutdown()
    w.close()
    w.deleteLater()
    QtWidgets.QApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)


def project(env, run="opencode-1a2b"):
    logs = env / "state" / "sandbox-ai" / "logs" / "app-1234"
    logs.mkdir(parents=True, exist_ok=True)
    path = logs / f"llm-traffic-20261113-100000-{run}.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records(run)) + "junk\n")
    return logs, path


def column(page, number):
    model = page.table.model()
    return [model.data(model.index(row, number)) for row in range(model.rowCount())]


def choose(page, n, wait):
    model = page.table.model()
    for row in range(model.rowCount()):
        if model.data(model.index(row, 0)) == str(n):
            page.table.selectRow(row)
            return wait(lambda: page.detail is not None and page.detail["record"]["n"] == n, timeout=30)
    return False


# --- the parts ------------------------------------------------------------------------------------------------------

def test_chat_html_folds_escapes_and_shows_what_is_new(qapp):
    view = {"system": "be brief", "messages": [
        {"role": "user", "kind": "text", "text": "list <files>", "message": 0, "new": False},
        {"role": "assistant", "kind": "tool_call", "text": "{\"c\":\"ls\"}", "name": "bash", "id": "c1",
         "message": 1, "new": True},
        {"role": "tool", "kind": "tool_result", "text": "a.py\x1b[31m\nb.py", "id": "c1", "message": 2, "new": True,
         "relay": "[sandbox-ai loop guard] stop"}],
        "answer": [{"role": "assistant", "kind": "text", "text": "x" * (traffic_page.TEXT_LIMIT + 5)}]}
    page = chat_html(view)
    assert "list &lt;files&gt;" not in page and "be brief" not in page  # only what is new; the system part with it
    assert "toggle:m1-0" in page and "tool call bash" in page and "\\x1b[31m" in page and "\x1b" not in page
    assert "<pre" not in page.split("Answer")[0]  # tool call and result folded: one line each
    assert "the relay added a note" in page and "whole:a0" in page and "New in this request: 2 of 3" in page
    whole = chat_html(view, new_only=False, toggled={"m2-2", "s"}, whole={"a0"})
    assert "list &lt;files&gt;" in whole and "earlier" in whole and "be brief" in whole and "b.py" in whole
    assert "whole:a0" not in whole and "x" * (traffic_page.TEXT_LIMIT + 5) in whole
    assert "No answer recorded." in chat_html({"messages": [], "answer": []})


def test_parameter_rows_mark_the_relays_changes(qapp):
    view = {"params": {"model": "m", "temperature": 0.7, "extra_params": {"top_k": 20}},
            "changes": [{"field": "temperature", "from": 0.1, "to": 0.7, "why": "parameter"},
                        {"field": "extra_params.top_k", "to": 20, "why": "boost"},
                        {"field": "tools", "from": "3 tools", "why": "loop guard", "note": "taken away"},
                        {"field": "messages", "why": "loop guard", "note": "[note]", "where": "a new message"}]}
    rows = {field: (value, change) for field, value, change in parameter_rows(view)}
    assert rows["model"] == ('"m"', None) and rows["temperature"][1]["from"] == 0.1
    assert rows["extra_params.top_k"][1]["why"] == "boost" and rows["tools"][0] == "(removed)"
    assert rows["messages"][0] == "[note]"
    assert "Was 0.1" in traffic_page.change_text(rows["temperature"][1])
    assert "Not set by the agent" in traffic_page.change_text(rows["extra_params.top_k"][1])
    assert "a new message" in traffic_page.change_text(rows["messages"][1])


def test_the_model_and_the_runs_found(qapp, tmp_path):
    model = TrafficModel()
    summary = {"n": 1, "run": "r", "time": "2026-11-13T10:00:00.000Z", "model": "m\x1b", "tokens_in": 1200,
               "tokens_out": 3, "ms": 2500, "status": 200, "finish_reason": "stop", "new": 2, "messages": 5,
               "loop": ["note"], "truncated": ["answer"]}
    assert model.add([summary, summary]) == 1  # read again: its row kept
    assert [model.data(model.index(0, c)) for c in (0, 2, 4, 5, 6)] == ["1", "m\\x1b", "2.5 s", "200 (stop)", "+2 / 5"]
    assert "note" in model.data(model.index(0, 7)) and "truncated" in model.data(model.index(0, 7))
    failed = {**summary, "n": 2, "status": None, "error": "closed", "loop": [], "truncated": None}
    model.add([failed])
    assert model.data(model.index(1, 5)) == "no answer" and model.data(model.index(1, 7)) == ""
    assert model.data(model.index(1, 5), QtCore.Qt.ItemDataRole.ForegroundRole) is not None
    for name in ("llm-traffic-20261112-090000-a.jsonl", "llm-traffic-20261113-090000-b.jsonl",
                 "llm-traffic-20261113-090000-b.jsonl.1", "connections-20261113-090000-b.jsonl"):
        (tmp_path / name).write_text("")
    assert [run for _, run, _ in traffic_runs([tmp_path, tmp_path / "missing"])] == ["b", "a"]
    assert [run for _, run, _ in traffic_runs([tmp_path], running={"a"})] == ["a", "b"]  # the running first


# --- the page ---------------------------------------------------------------------------------------------------------

def test_the_page_lists_reads_a_request_and_follows(window, env, wait):
    logs, path = project(env)
    page = window.traffic
    window.show_page("traffic")
    assert wait(lambda: page.model.rowCount() == 4, timeout=30)
    assert page.source.currentData() == {"args": ["--log-dir", str(logs)], "run": "opencode-1a2b"}
    assert column(page, 0) == ["1", "2", "3", "4"] and column(page, 2)[0] == "qwen3-coder"
    assert column(page, 3)[1] == "200 / 2" and column(page, 6) == ["+2 / 2", "+2 / 4", "+2 / 6", "+1 / 1"]
    assert "answer stopped" in column(page, 7)[2] and column(page, 5)[3] == "500"
    # A request: what is new in it, folded tool calls, the relay's changes, the raw record.
    assert choose(page, 2, wait)
    shown = page.chat.toPlainText()
    assert "tool result [c1]" in shown and "list the files" not in shown and "a.py and b.py" in shown
    assert "a.py\nb.py" not in shown  # folded: one line
    page.clicked(QtCore.QUrl("toggle:m3-1"))
    assert "a.py\nb.py" in page.chat.toPlainText()
    page.new_only.setChecked(False)
    assert "list the files" in page.chat.toPlainText()
    page.new_only.setChecked(True)
    fields = {page.params.item(row, 0).text(): page.params.item(row, 2).text() for row in range(page.params.rowCount())}
    assert "Was 0.1" in fields["temperature"] and "Not set by the agent" in fields["max_tokens"] and \
        fields["model"] == ""
    assert '"n": 2' in page.raw.toPlainText()
    assert choose(page, 3, wait)
    assert "\\x1b[31m <b>bold</b>" in page.chat.toPlainText()  # escaped, and no markup
    # In a window of its own, closed with Esc.
    chat = page.show_window()
    assert chat is not None and chat.isVisible() and "welcome" in chat.browser.toPlainText()
    QtWidgets.QApplication.sendEvent(chat, QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_Escape,
                                                           QtCore.Qt.KeyboardModifier.NoModifier))
    assert wait(lambda: not shiboken6.isValid(chat) or not chat.isVisible())  # (closed, it goes: test_leaks_perf_1)
    # A new request arrives while following.
    with open(path, "a") as stream:
        stream.write(json.dumps({**records()[0], "n": 5, "time": "2026-11-13T10:01:00.000Z"}) + "\n")
    assert wait(lambda: page.model.rowCount() == 5, timeout=30)


def test_search_and_export(window, env, wait, monkeypatch):
    logs, _ = project(env)
    page = window.traffic
    window.show_page("traffic")
    assert wait(lambda: page.model.rowCount() == 4, timeout=30)
    page.search.setText("WELCOME")
    page.restart()
    assert wait(lambda: column(page, 0) == ["3"], timeout=30)
    assert "contain" in page.status.text()
    page.search.setText("")
    page.restart()
    assert wait(lambda: page.model.rowCount() == 4, timeout=30)
    target = env / "run.md"
    monkeypatch.setattr("sandbox_ai_app.state.run_dialog", lambda dialog: [str(target)])
    assert page.export("md", False) == str(target)
    assert wait(lambda: "Exported to" in page.status.text(), timeout=60)
    text = target.read_text()
    assert text.startswith("# LLM traffic of opencode-1a2b") and "### Answer" in text and \
        target.stat().st_mode & 0o777 == 0o600
    assert choose(page, 2, wait)
    one = env / "one.jsonl"
    monkeypatch.setattr("sandbox_ai_app.state.run_dialog", lambda dialog: [str(one)])
    page.export("json", True)
    assert wait(lambda: "Exported to" in page.status.text() and one.exists() and one.stat().st_size, timeout=60)
    assert json.loads(one.read_text())["record"]["n"] == 2
    (env / "link.md").symlink_to(env / "elsewhere.md")
    monkeypatch.setattr("sandbox_ai_app.state.run_dialog", lambda dialog: [str(env / "link.md")])
    assert page.export("md", False) is None and not (env / "elsewhere.md").exists()


def test_a_run_compressed_by_gc_is_still_shown(window, env, wait):
    """sandbox-ai gc compresses the logs of runs that ended (NAME.gz): listed and read as before."""
    import gzip
    logs, path = project(env)
    packed = path.with_name(path.name + ".gz")
    packed.write_bytes(gzip.compress(path.read_bytes()))
    path.unlink()
    assert [run for _, run, _ in traffic_runs([logs])] == ["opencode-1a2b"]
    page = window.traffic
    window.show_page("traffic")
    assert wait(lambda: page.model.rowCount() == 4, timeout=30)
    assert column(page, 0) == ["1", "2", "3", "4"]


def test_nothing_recorded_yet(window, env, wait):
    page = window.traffic
    window.show_page("traffic")
    assert wait(lambda: "No LLM traffic recorded yet" in page.status.text(), timeout=30)
    assert page.model.rowCount() == 0 and not page.export_button.isEnabled()
    assert Path(page.sai.directory, "llm_traffic.py").is_file()


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_the_status_line_is_never_cut(qapp, window, env, wait, mode):
    """CI 2026-11-14 (tests/test_layout.py, dark, German, the smallest window): "Noch kein LLM-Verkehr aufgezeichnet:
    ..." wraps to two lines and had the height of one - the text came in an event (restart) and the page's layout
    gave the label its height for it only in a later pass; a word-wrapped label's own minimum is one line. The status
    line is as high as its text at its width as soon as it has the text, whatever its length."""
    from sandbox_ai_app import theme
    theme.apply(mode, qapp)
    try:
        page = window.traffic
        window.resize(window.minimumSizeHint())
        window.show()
        window.show_page("traffic")
        assert wait(lambda: "No LLM traffic recorded yet" in page.status.text(), timeout=30)
        for text in ("x", page.status.text() * 4, "Reading…", page.status.text() * 6, page.status.text()):
            QtCore.QTimer.singleShot(0, lambda text=text: page.status.setText(text))  # (as restart(): in an event)
            QtWidgets.QApplication.processEvents()
            assert page.status.text() == text
            assert page.status.height() + 2 >= page.status.heightForWidth(page.status.width()), (
                text[:30], page.status.heightForWidth(page.status.width()), page.status.height())
            window.resize(window.minimumSizeHint().width(), 1)  # (Qt keeps it at its minimum: no room to spare)
            QtWidgets.QApplication.processEvents()
            assert page.status.height() + 2 >= page.status.heightForWidth(page.status.width()), text[:30]
        assert page.status.height() >= 2 * page.status.fontMetrics().height()  # (wrapped: more than one line)
    finally:
        theme.apply("system", qapp)
