"""--ask allow (README: Approvals and browser requests): a sandbox that asks nobody. Every question it would put to the
human is answered "allow" by the launcher on the host, at once, for the run, applied and recorded as a human's answer
("allowed automatically (--ask allow)"); what is no question stays as it is (deny rules, the privacy check's blocking
findings, a canary's alarm). The start warns, a strict mode the human chose is answered too, one a floor chose refuses
it; sandbox-ai list, the trust review and the web mark it; an older host is told to update. Nothing here writes a
login (HOME is moved for the session by conftest.py, and the matrix fixture moves it again)."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import time
import types

import pytest

import test_feature_matrix as fm
from test_feature_matrix import m  # noqa: F401  (fixture)
import test_proxy as tp
from test_proxy import pki  # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parents[1]
rt = tp.rt
s = fm.s


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / "sandbox" / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ap = load("approvals_ask_allow_test", "approvals.py")
devices = load("devices_ask_allow_test", "devices.py")
activity = load("activity_ask_allow_test", "activity.py")
starts = load("starts_ask_allow_test", "starts.py")
needs_openssl = pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
PUBLIC = "151.101.0.223"


# --- the host's side: every kind of question ---------------------------------------------------------------------

@pytest.fixture
def run(tmp_path, monkeypatch):
    """A run's control directory (with run.json and its session's hosts file), the administrator's policy open, a
    browser that only records, a resolver that answers a public address."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    session = tmp_path / "session"
    session.mkdir()
    (session / "hosts").write_text("127.0.0.1 localhost\n")
    directory = tmp_path / "runs" / "claude-1"
    (directory / "approvals").mkdir(parents=True)
    record = {"run": "claude-1", "directory": str(directory), "session_dir": str(session),
              "workspace": str(tmp_path / "app"), "pid": os.getpid(), "ask": "allow"}
    (directory / "run.json").write_text(json.dumps({k: v for k, v in record.items() if k != "directory"}))
    opened = []
    monkeypatch.setattr(ap, "open_browser", opened.append)
    monkeypatch.setattr(ap, "resolve", lambda name: [PUBLIC])
    monkeypatch.setattr(ap, "policy_scope", lambda: "always")
    monkeypatch.setattr(ap, "policy_allows_host", lambda name: True)
    return types.SimpleNamespace(record=record, approvals=directory / "approvals", opened=opened, session=session)


def ask(run, ident, **question):
    (run.approvals / f"{ident}.json").write_text(json.dumps({"id": ident, "time": "2026-11-09T10:00:00Z", **question}))


def answered(run, ident):
    path = run.approvals / f"{ident}.answer"
    return json.loads(path.read_text()) if path.exists() else None


QUESTIONS = {
    "connect": {"destination": "192.0.2.7:5432", "address": "192.0.2.7", "port": 5432},
    "dns": {"name": "pypi.org"},
    "push": {"url": "https://github.com/a/b", "refs": ["refs/heads/main"], "message": "warn: a name"},
    "http": {"method": "POST", "url": "https://api.example/x", "host": "api.example", "path": "/x", "rule": "mode-writes",
             "ruleset": "security mode high"},
    "mcp": {"server": "files", "tool": "write_file", "arguments": {}, "rule": "tainted", "ruleset": "rules.json",
            "taint": {"tainted": True, "source": {"what": "news.example"}, "since": "2026-11-09T09:00:00Z"}},
    "tool": {"agent": "claude", "tool": "Bash", "arguments": {}, "rule": "mode-tainted-tools", "ruleset": "paranoid"},
    "device": {"value": "/dev/ttyUSB0", "nodes": ["/dev/ttyUSB0"], "rule": "serial", "ruleset": "rules.json"},
    "browser": {"url": "https://login.example/device"},
}


@pytest.mark.parametrize("kind", sorted(QUESTIONS))
def test_every_kind_of_question_is_allowed_at_once_for_the_run(run, kind):
    """Each kind sandbox-ai asks about is answered "allow" for the run, marked as given by --ask allow (the sandbox
    records that as the reason), with what a human's allow does on the host: a name resolved and pinned, a URL
    opened. Nothing is remembered for later starts."""
    ask(run, "1-1-1", kind=kind, **QUESTIONS[kind])
    lines = []
    reply = ap.auto_answer({"id": "1-1-1", "kind": kind, **QUESTIONS[kind]}, run.record, out=lines.append)
    assert reply["decision"] == "allow"
    assert answered(run, "1-1-1") == {"decision": "allow", "scope": "run", "by": "ask-allow",
                                      **({"addresses": [PUBLIC]} if kind == "dns" else {})}
    if kind == "dns":
        assert f"{PUBLIC} pypi.org" in (run.session / "hosts").read_text()
    if kind == "browser":  # nobody waits in the sandbox: its result is written here, with the reason
        assert run.opened == ["https://login.example/device"]
        assert json.loads((run.approvals / "1-1-1.done").read_text())["reason"] == ap.AUTO_REASON
    assert ap.approved_for(Path(run.record["workspace"])) == {"hosts": [], "tcp": []}, "never always"
    assert "automatically: --ask allow" in lines[0]


def test_a_canary_alarm_and_a_url_with_a_canary_token_stay_with_the_human(run):
    """Not questions to answer unseen: a decoy file was read (the sandbox is paused until the human says continue or
    stop), or a URL would carry a canary token of the run out through the host's browser."""
    ask(run, "1-1-1", kind="canary", canary="c1", summary="a decoy was read")
    assert ap.auto_answer({"id": "1-1-1", "kind": "canary"}, run.record) is None
    (Path(run.record["directory"]) / "canary.json").write_text(json.dumps({"names": [["tok123canary", "c1", "plain"]]}))
    question = {"id": "1-1-2", "kind": "browser", "url": "https://x.example/?q=tok123canary"}
    ask(run, "1-1-2", **{k: v for k, v in question.items() if k != "id"})
    assert ap.auto_answer(question, run.record) is None
    assert answered(run, "1-1-1") is None and answered(run, "1-1-2") is None and run.opened == []


def test_what_no_answer_may_allow_is_refused_at_once_with_why(run, monkeypatch):
    """A lookup in a run that approves only once (paranoid), outside the administrator's allow_hosts, or of a name
    without a public address: refused for the run at once (the agent does not wait a minute), saying why."""
    paranoid = {**run.record, "mode": {"name": "paranoid", "approval_scope": "once"}}
    ask(run, "1-1-1", kind="dns", name="pypi.org")
    reply = ap.auto_answer({"id": "1-1-1", "kind": "dns", "name": "pypi.org"}, paranoid)
    assert reply["decision"] == "deny" and "approves at most once" in reply["reason"]
    assert answered(run, "1-1-1") == {"decision": "deny", "scope": "run", "by": "ask-allow",
                                      "why": ap.shown(ap.DNS_ONCE, 300)}
    monkeypatch.setattr(ap, "policy_allows_host", lambda name: False)
    ask(run, "1-1-2", kind="dns", name="other.example")
    assert "outside the administrator's policy" in ap.auto_answer({"id": "1-1-2", "kind": "dns",
                                                                   "name": "other.example"}, run.record)["reason"]
    monkeypatch.setattr(ap, "policy_allows_host", lambda name: True)
    monkeypatch.setattr(ap, "resolve", lambda name: ["10.0.0.5"])
    ask(run, "1-1-3", kind="dns", name="intranet.example")
    assert "non-public" in ap.auto_answer({"id": "1-1-3", "kind": "dns", "name": "intranet.example"},
                                          run.record)["reason"]
    assert answered(run, "1-1-3")["decision"] == "deny"
    assert ap.answered_reason(answered(run, "1-1-3")).startswith(ap.AUTO_REFUSED)


def test_answers_keep_to_the_mode_and_the_policy(run, monkeypatch):
    connect = {"kind": "connect", **QUESTIONS["connect"]}
    for ident, mode, policy, scope in (("1-1-1", "high", "always", "run"), ("1-1-2", "paranoid", "always", "once"),
                                       ("1-1-3", "medium", "once", "once")):
        monkeypatch.setattr(ap, "policy_scope", lambda policy=policy: policy)
        record = {**run.record, "mode": {"name": mode, "approval_scope": dict(
            high="run", paranoid="once").get(mode, "always")}}
        ask(run, ident, **connect)
        ap.auto_answer({"id": ident, **connect}, record)
        assert answered(run, ident)["scope"] == scope, (mode, policy)


def test_the_watch_answers_by_itself_and_notifies_nobody(run):
    """approvals.watch with auto (the launcher's own option): every new question goes to auto_answer, no
    notification; a canary's alarm is left to the human (it notifies itself)."""
    answered_, notes, stop = [], [], threading.Event()
    thread = threading.Thread(target=ap.watch, args=(run.approvals, stop), kwargs={
        "notify": lambda title, text: notes.append(text), "interval": 0.02, "auto": True,
        "autos": lambda question, record: answered_.append((question["kind"], record["run"]))})
    thread.start()
    for number, kind in enumerate(("connect", "dns", "canary", "push"), 1):
        ask(run, f"1-1-{number}", kind=kind, **QUESTIONS.get(kind, {}))
    deadline = time.monotonic() + 5
    while len(answered_) < 3 and time.monotonic() < deadline:
        time.sleep(0.02)
    stop.set()
    thread.join(5)
    assert sorted(answered_) == [("connect", "claude-1"), ("dns", "claude-1"), ("push", "claude-1")]
    assert notes == []


def test_a_browser_request_opens_without_a_question(run, tmp_path):
    """serve_browser with auto: the URL is opened at once (recorded as answered by --ask allow) and the agent's
    xdg-open is told so; a URL with a canary token of the run is put to the human as before."""
    socket_path, stop = tmp_path / "open.sock", threading.Event()
    notes = []
    listener = threading.Thread(target=ap.serve_browser, args=(socket_path, run.approvals, stop),
                                kwargs={"notify": lambda title, text: notes.append(text), "auto": True,
                                        "listen": rt.listen_unix})
    listener.start()
    try:
        deadline = time.monotonic() + 5
        while not socket_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)

        def request(url):
            with socket.socket(socket.AF_UNIX) as client:
                client.connect(str(socket_path))
                client.sendall(json.dumps({"url": url}).encode() + b"\n")
                return json.loads(client.makefile().readline())
        reply = request("https://login.example/device")
        assert reply["ok"] and "without a question (--ask allow" in reply["message"]
        assert run.opened == ["https://login.example/device"] and notes == []
        (Path(run.record["directory"]) / "canary.json").write_text(json.dumps({"names": [["tok123canary", "c", "p"]]}))
        reply = request("https://x.example/?t=tok123canary")
        assert reply["ok"] and "asked the human" in reply["message"] and len(notes) == 1
        assert run.opened == ["https://login.example/device"]
    finally:
        stop.set()
        listener.join(5)


# --- the sandbox's side: the answer recorded as given --------------------------------------------------------------

def test_the_sandbox_records_the_launchers_answer_as_such(tmp_path):
    """The supervisor's broker and the device watcher read who answered: the reason says --ask allow."""
    broker = rt.ApprovalBroker(tmp_path / "approvals", rt.RuntimeNetwork({}, None, lambda rules: None), timeout=5,
                               poll=0.02)
    auto, refused = "allowed automatically (--ask allow)", "refused automatically: --ask allow could not allow it"
    for reply, reason, device_reason in (
            ({"decision": "allow", "scope": "run", "by": "ask-allow"}, auto, auto),
            ({"decision": "allow", "scope": "run"}, "answered", "answered"),
            ({"decision": "deny", "scope": "run", "by": "ask-allow", "why": "x\nFAKE"}, f"{refused} (x%0AFAKE)", refused)):
        (tmp_path / "approvals" / "9-9-9.answer").write_text(json.dumps(reply))
        assert broker._read_answer("9-9-9")["reason"] == reason
        assert devices.read_answer({"directory": str(tmp_path)}, "9-9-9")["reason"] == device_reason
    assert rt.AUTO_REASON == ap.AUTO_REASON and rt.AUTO_REFUSED == ap.AUTO_REFUSED and rt.AUTO_BY == ap.AUTO_BY


def test_the_timeline_says_who_answered(tmp_path):
    control = tmp_path / "control"
    (control / "approvals").mkdir(parents=True)
    records = []
    collector = activity.Collector(log_dir=tmp_path / "logs", run="claude-1", agent="claude", session=None,
                                   control_dir=control, connection_log=None, sink=records.extend)
    collector.start({"summary": "claude started", "agent": "claude"})
    (control / "approvals" / "1-2-3.json").write_text(json.dumps({"id": "1-2-3", "kind": "dns", "name": "pypi.org"}))
    (control / "approvals" / "1-2-3.done").write_text(json.dumps({"decision": "allow", "scope": "run",
                                                                  "reason": ap.AUTO_REASON}))
    collector.poll()
    collector.finish(status=0)
    approval = next(r for r in records if r["kind"] == "approval")
    assert approval["summary"] == "allow (run) automatically (--ask allow): look up pypi.org"
    assert approval["details"]["automatic"] is True


@needs_openssl
def test_through_the_proxy_ask_rules_pass_and_deny_rules_still_deny(tmp_path, pki, monkeypatch):  # noqa: F811
    """End to end: the proxy, the supervisor's broker and the launcher's watch with auto. A request an ask rule
    holds goes through without anyone answering, its log record names the automatic answer, approvals history says
    so; a request a deny rule refuses is refused, and no question is filed for it."""
    monkeypatch.setattr(ap, "policy_scope", lambda: "always")
    rules = rt.Ruleset.validated({"version": 1, "http": [
        {"id": "releases", "host": "*", "methods": ["POST"], "path": "/releases", "decision": "ask"},
        {"id": "no-deletes", "host": "*", "methods": ["DELETE"], "path": "/**", "decision": "deny"}]}, "mine")
    upstream = tp.serve(tp.Handler)
    proxy = tp.Proxy(tmp_path, pki, upstream.server_address[1], {"rules": [{"name": "mine", **rules}]})
    directory = tmp_path / "runs" / "claude-1"
    directory.mkdir(parents=True)
    (directory / "run.json").write_text(json.dumps({"run": "claude-1", "workspace": str(tmp_path / "app"),
                                                    "pid": os.getpid()}))
    broker = rt.ApprovalBroker(directory / "approvals", rt.RuntimeNetwork({}, None, lambda r: None), timeout=10,
                               poll=0.02)
    proxy_end, supervisor_end = socket.socketpair()
    threading.Thread(target=rt.serve_asks, args=(supervisor_end, broker), daemon=True).start()
    proxy.server.asker = proxy.server.who = rt.AskChannel(proxy_end)
    stop = threading.Event()
    watcher = threading.Thread(target=ap.watch, args=(directory / "approvals", stop),
                               kwargs={"interval": 0.05, "auto": True, "notify": lambda *a: pytest.fail("notified")})
    watcher.start()

    def send(method, path):
        with socket.create_connection(("127.0.0.1", proxy.port)) as client:
            client.sendall(f"{method} {path} HTTP/1.1\r\nHost: api.example\r\nContent-Length: 2\r\n"
                           "Connection: close\r\n\r\n{}".encode())
            client.settimeout(10)
            data = b""
            while chunk := client.recv(65536):
                data += chunk
            return data
    try:
        started = time.monotonic()
        assert send("POST", "/releases").startswith(b"HTTP/1.1 201")
        assert time.monotonic() - started < 5, "answered at once, not after a timeout"
        record = proxy.records(until=lambda r: r.get("rule") == "releases" and r.get("approval"))[-1]
        assert record["approval"] == {"decision": "allow", "scope": "run", "reason": ap.AUTO_REASON}
        assert send("DELETE", "/releases/1").startswith(b"HTTP/1.1 403")
        filed = sorted(p.name for p in (directory / "approvals").glob("*.json"))
        assert len(filed) == 1, filed  # the deny rule asked nothing
        lines, original = [], ap.questions
        monkeypatch.setattr(ap, "questions", lambda runs=None, answered=False: original(
            {"claude-1": {"run": "claude-1", "directory": str(directory)}}, answered))
        assert ap.main(["history"], out=lines.append) == 0
        assert any(ap.AUTO_REASON in line for line in lines), lines
    finally:
        stop.set()
        watcher.join(5)
        proxy.close()
        upstream.shutdown()


def test_blocking_privacy_findings_are_no_question(tmp_path, pki):  # noqa: F811
    """The privacy check's blocking findings refuse the push without a question (so --ask allow cannot pass them);
    warnings are a question, which an automatic answer allows."""
    line = b"0" * 40 + b" " + b"1" * 40 + b" refs/heads/main\0report-status\n"
    body = b"%04x" % (len(line) + 4) + line + b"0000"
    for verdict, asked_expected, ok in (({"ok": False, "message": "blocking: a key"}, False, False),
                                        ({"ok": True, "warnings": ["a name"], "message": "warn: a name"}, True, True)):
        stand_in = tmp_path / "checker.py"
        stand_in.write_text(f"import json, sys\nfor line in sys.stdin:\n    print(json.dumps({verdict!r}), flush=True)\n")
        checker = subprocess.Popen([sys.executable, "-I", str(stand_in)], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        proxy = tp.Proxy(tmp_path, pki, 1, {"push_check": True}, checker=checker)
        asked = []
        proxy.server.asker = lambda kind, details: asked.append(kind) or {
            "decision": "allow", "scope": "run", "reason": ap.AUTO_REASON}
        try:
            found = proxy.server.check_push("http://example.test/repo.git/git-receive-pack", [], [(body, body)], [],
                                            set())[0]
            assert bool(asked) == asked_expected and found["ok"] == ok, (verdict, found)
        finally:
            proxy.close()
            checker.stdin.close()
            checker.wait(5)


# --- the start ---------------------------------------------------------------------------------------------------

def recorded_threads(m):
    """The threads the start starts, with their keyword arguments (the matrix records only their names)."""
    started, real = [], threading.Thread

    def thread(*args, target=None, kwargs=None, **rest):
        started.append((getattr(target, "__name__", str(target)), kwargs or {}))
        return real(target=lambda *a: None, daemon=True)
    m.monkeypatch.setattr(s.threading, "Thread", thread)
    return started


def test_a_start_with_ask_allow(m):
    threads = recorded_threads(m)
    run = m.run("--ask", "allow")
    assert run.config["ask"] is True and run.run_record["ask"] == "allow"
    assert "--ask allow: nothing is asked, every question is allowed at once" in run.err
    assert "a canary's alarm still pauses the sandbox" in run.err
    watch = dict(threads)["watch"]
    assert watch == {"auto": True, "interval": 0.2} and dict(threads)["serve_browser"] == {"auto": True}
    assert m.plan("--ask", "allow")["ask"] == "allow" and m.plan()["ask"] == "on" and m.plan("-Q", "off")["ask"] == "off"
    plain = recorded_threads(m)
    assert m.run().run_record["ask"] == "on" and dict(plain)["watch"] == {} and dict(plain)["serve_browser"] == {
        "auto": False}
    nothing = m.run("--no-proxy", "--ask", "allow")
    assert nothing.run_record["ask"] is None and "--ask allow does nothing here" in nothing.err


def test_a_strict_mode_the_human_chose_has_its_questions_answered_too(m):
    run = m.run("--mode", "high", "--ask", "allow")
    assert run.run_record["ask"] == "allow" and run.config["ask_max_scope"] == "run"
    assert "answers the questions of the security mode high (--mode) too" in run.err
    m.project_file({"mode": "paranoid", "options": ["--ask", "allow"]})
    run = m.run()
    assert "security mode paranoid (the project file" in run.err and "approve once only" in run.err


def test_a_strict_mode_a_floor_chose_refuses_it(m):
    m.refused("--mode-at-least", "high", "--ask", "allow", match=r"a floor \(--mode-at-least high\), not your choice")
    assert m.run("--mode", "high", "--mode-at-least", "high", "--ask", "allow").run_record["ask"] == "allow"
    assert m.run("--mode-at-least", "high").run_record["ask"] == "on"
    m.policy(min_mode="high")
    m.refused("--ask", "allow", match=r"the administrator's policy .* requires at least the mode high")
    m.policy(min_mode="low")
    assert m.run("--ask", "allow").run_record["ask"] == "allow"
    m.policy(forbid=["--ask=allow"])
    m.refused("--ask", "allow", match=r"--ask=allow is not allowed by the administrator's policy")
    assert m.run("--ask", "off").run_record["ask"] == "off"


def test_it_is_marked_where_a_project_file_is_trusted():
    marks = s.MODES.project_marks(s, {"options": ["--ask", "allow"]})
    assert {"key": "options", "why": "option", "option": "--ask", "value": "allow"} in marks
    assert s.MODES.weakening_options(s, ["-Q=allow"]) == [("--ask", "allow")]


def test_the_agent_cannot_switch_it_on():
    """A start option only: no `sandbox-ai net allow` option, no control request of the supervisor."""
    net = load("net_ask_allow_test", "net.py")
    allow = next(action for action in net.parser()._actions if getattr(action, "choices", None) and "allow" in
                 action.choices).choices["allow"]
    assert not [o for o in allow._option_string_actions if o.startswith("--ask") or o == "-Q"]
    # What a run-time change may carry (the control socket's allow and revoke): destinations, never the questions.
    assert rt.runtime_change({"nets": set(), "https": set(), "tcp": set()}, {"op": "allow", "ask": "allow"}) == \
        rt.runtime_change({"nets": set(), "https": set(), "tcp": set()}, {"op": "allow"})


def test_sandbox_ai_list_marks_it():
    entry = {"name": "web", "kind": "run", "agent": "claude", "state": "running", "home": "discarded", "ask": "allow",
             "started": "2026-11-09T10:00:00Z", "workspace": "/w"}
    assert starts.ASK_ALLOW_STATE in starts.row(entry, False)[3]
    assert starts.ASK_ALLOW_STATE not in starts.row({**entry, "ask": "on"}, False)[3]
    assert starts.ask_of({"ask": "allow"}) == "allow" and starts.ask_of({"ask": "maybe"}) is None
    assert starts.ask_of({}) is None


def test_the_option_completes():
    completion = load("completion_ask_allow_test", "completion.py")
    assert {"on", "off", "allow"} <= set(completion.complete(["claude", "--ask", ""]))


def test_an_older_host_is_told_to_update(monkeypatch):
    webui = load("webui_ask_allow_test", "webui.py")
    for args in (["claude", "--ask", "allow"], ["codex", "--ask=allow"], ["opencode", "-Q", "allow"],
                 ["team", "start", "t", "--agents=claude,codex", "--", "--ask", "allow"]):
        assert webui.asks_allow(args), args
    for args in (["claude", "--ask", "on"], ["claude", "--", "--ask", "allow"], ["net", "allow", "--ask", "allow"],
                 ["team", "start", "t", "--ask", "allow"]):
        assert not webui.asks_allow(args), args
    assert next(since for since, what, _ in webui.NEWER if what == "--ask allow") == (0, 1, 80)
