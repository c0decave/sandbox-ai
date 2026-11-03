"""sandbox-ai approvals: the host side of the questions running sandboxes ask."""
import importlib.util
import json
import os
from pathlib import Path
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sandbox_approvals_tests", ROOT / "sandbox/approvals.py")
ap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ap)


@pytest.fixture
def run(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    session = tmp_path / "session"
    session.mkdir()
    (session / "hosts").write_text("127.0.0.1 localhost\n")
    directory = tmp_path / "runs" / "claude-1"
    (directory / "approvals").mkdir(parents=True)
    record = {"run": "claude-1", "directory": str(directory), "session_dir": str(session),
              "workspace": str(tmp_path / "app"), "pid": os.getpid()}
    return {"claude-1": record}


def ask(run, ident, **question):
    directory = Path(run["claude-1"]["directory"]) / "approvals"
    (directory / f"{ident}.json").write_text(json.dumps({"id": ident, "time": "now", **question}))
    return directory


def test_list_describe_and_history(run):
    ask(run, "1-1-1", kind="connect", destination="192.0.2.7:443", address="192.0.2.7", port=443, host="api.example.org")
    directory = ask(run, "1-1-2", kind="dns", name="pypi.org")
    (directory / "1-1-2.done").write_text(json.dumps({"decision": "deny", "reason": "no answer within 60 s"}))
    open_ = ap.questions(run)
    assert [q["id"] for _, _, q in open_] == ["1-1-1"]
    assert ap.describe(open_[0][2]) == "connect to 192.0.2.7:443 (api.example.org)"
    history = ap.questions(run, answered=True)
    assert history[1][2]["result"]["reason"] == "no answer within 60 s"
    (directory / "../x.json").resolve()  # names that are no question ids are never read
    (directory / "evil.json").write_text("{}")
    assert [q["id"] for _, _, q in ap.questions(run)] == ["1-1-1"]


def test_answers(run, tmp_path):
    directory = ask(run, "1-1-1", kind="dns", name="pypi.org")
    reply = ap.answer("1-1-1", "allow", "always", runs=run, resolver=lambda name: ["151.101.0.223"], out=lambda line: None)
    assert reply == {"decision": "allow", "scope": "always", "addresses": ["151.101.0.223"]}
    assert json.loads((directory / "1-1-1.answer").read_text()) == reply
    # Pinned in the sandbox's /etc/hosts at once, and remembered for the project.
    assert "151.101.0.223 pypi.org" in (tmp_path / "session/hosts").read_text()
    assert ap.approved_for(tmp_path / "app") == {"hosts": ["pypi.org"], "tcp": []}
    ask(run, "1-1-2", kind="connect", destination="192.0.2.9:5432", address="192.0.2.9", port=5432)
    ap.answer("1-1-2", "allow", "always", runs=run, out=lambda line: None)
    assert ap.approved_for(tmp_path / "app")["tcp"] == ["192.0.2.9:5432"]
    ask(run, "1-1-3", kind="push", url="https://github.com/a/b", refs=["refs/heads/main"], message="warn")
    assert ap.answer("1-1-3", "deny", runs=run, out=lambda line: None)["decision"] == "deny"
    with pytest.raises(ValueError, match="no open question"):
        ap.answer("1-1-3", "allow", runs=run, out=lambda line: None)  # answered already
    with pytest.raises(ValueError):
        ap.answer("1-1-9", "maybe", runs=run)


def test_browser_requests_open_only_http_after_the_answer(run):
    opened = []
    ask(run, "1-1-1", kind="browser", url="https://claude.ai/oauth?code=1")
    ap.answer("1-1-1", "allow", runs=run, opener=opened.append, out=lambda line: None)
    assert opened == ["https://claude.ai/oauth?code=1"]
    ask(run, "1-1-2", kind="browser", url="file:///etc/passwd")
    with pytest.raises(ValueError, match="only http"):
        ap.answer("1-1-2", "allow", runs=run, opener=opened.append, out=lambda line: None)
    assert len(opened) == 1


def test_watch_notifies_once_per_question(run):
    directory = Path(run["claude-1"]["directory"]) / "approvals"
    notes, stop = [], threading.Event()
    thread = threading.Thread(target=ap.watch, args=(directory, stop, lambda t, text: notes.append(text), 0.05))
    thread.start()
    ask(run, "1-1-1", kind="dns", name="pypi.org")
    deadline = time.monotonic() + 5
    while not notes and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.2)
    stop.set()
    thread.join(5)
    assert notes == ["look up pypi.org\nsandbox-ai approvals allow 1-1-1"]


def test_browser_request_round_trip(run, tmp_path):
    """xdg-open in the sandbox -> the host's listener -> a question -> allowed -> opened."""
    spec = importlib.util.spec_from_file_location("runtime_for_browser_test", ROOT / "sandbox/runtime.py")
    runtime = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runtime)
    import io
    import tempfile
    short = Path(tempfile.mkdtemp(prefix="br-", dir="/tmp"))
    approvals_dir = Path(run["claude-1"]["directory"]) / "approvals"
    notes, stop = [], threading.Event()
    thread = threading.Thread(target=ap.serve_browser, args=(short / "open.sock", approvals_dir, stop),
                              kwargs={"notify": lambda t, text: notes.append(text), "listen": runtime.listen_unix})
    thread.start()
    deadline = time.monotonic() + 5
    while not (short / "open.sock").exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    try:
        out = io.StringIO()
        assert runtime.open_request(["https://claude.ai/oauth/authorize?x=1"], str(short / "open.sock"), out) == 0
        assert "nothing opens without their answer" in out.getvalue()
        assert runtime.open_request(["file:///etc/passwd"], str(short / "open.sock"), io.StringIO()) == 1
        assert runtime.open_request(["javascript:alert(1)"], str(short / "open.sock"), io.StringIO()) == 1
        question = ap.questions(run)[0][2]
        assert question["kind"] == "browser" and notes and "claude.ai" in notes[0]
        opened = []
        ap.answer(question["id"], "allow", runs=run, opener=opened.append, out=lambda line: None)
        assert opened == ["https://claude.ai/oauth/authorize?x=1"] and ap.questions(run) == []
    finally:
        stop.set()
        thread.join(5)


def test_browser_request_too_long_or_not_an_object_is_refused(run, tmp_path):
    import socket as socket_module
    import tempfile
    spec = importlib.util.spec_from_file_location("runtime_for_browser_test2", ROOT / "sandbox/runtime.py")
    runtime = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runtime)
    short = Path(tempfile.mkdtemp(prefix="br-", dir="/tmp"))
    approvals_dir = Path(run["claude-1"]["directory"]) / "approvals"
    stop = threading.Event()
    thread = threading.Thread(target=ap.serve_browser, args=(short / "open.sock", approvals_dir, stop),
                              kwargs={"notify": lambda t, text: None, "listen": runtime.listen_unix})
    thread.start()
    deadline = time.monotonic() + 5
    while not (short / "open.sock").exists() and time.monotonic() < deadline:
        time.sleep(0.02)

    def send(payload):
        with socket_module.socket(socket_module.AF_UNIX) as client:
            client.settimeout(10)
            client.connect(str(short / "open.sock"))
            client.sendall(payload)
            return json.loads(client.makefile("r").readline())
    try:
        reply = send(b'{"url": "https://example.org/' + b"a" * (ap.REQUEST_LIMIT + 10) + b'"}\n')
        assert reply["ok"] is False and "too long" in reply["message"]
        assert send(b'["https://example.org/"]\n')["ok"] is False
        assert ap.questions(run) == []
        assert send(b'{"url": "https://example.org/ok"}\n')["ok"] is True
    finally:
        stop.set()
        thread.join(5)


def test_desktop_notification_escapes_markup(monkeypatch):
    calls = []
    monkeypatch.setattr(ap.shutil, "which", lambda name: "/usr/bin/notify-send")
    monkeypatch.setattr(ap.subprocess, "run", lambda argv, **kw: calls.append(argv))
    ap.desktop_notification("sandbox-ai asks", 'open https://x.example/<img src="http://evil.example/a">\x1b[31m\nline')
    body = calls[0][-1]
    assert "<img" not in body and "&lt;img" in body and "\x1b" not in body and "\nline" in body
