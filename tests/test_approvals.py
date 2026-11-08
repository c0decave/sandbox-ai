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


SUPERVISORS = []  # the stand-ins started by the test that runs; stopped after it (stop_supervisors)


@pytest.fixture(autouse=True)
def stop_supervisors():
    """Every stand-in supervisor ends with its test: left running, dozens of them glob every 20 ms for the rest of
    the session, and on a two-core CI runner that starved every later test (make_dist ran out of its 5 minutes)."""
    yield
    while SUPERVISORS:
        SUPERVISORS.pop().set()


def supervisor(run, result=None, stop=None):
    """A stand-in for the sandbox's supervisor: every answer gets its .done (RESULT, or the answer itself)."""
    directory = Path(run["claude-1"]["directory"]) / "approvals"
    stop = stop or threading.Event()
    SUPERVISORS.append(stop)

    def loop():
        while not stop.wait(0.02) and directory.is_dir():
            for path in directory.glob("*.answer"):
                done = path.with_suffix(".done")
                if done.exists():
                    continue
                try:  # an answer being written is read again at the next look, the loop never ends on it
                    data = json.dumps(result or json.loads(path.read_text()))
                except (OSError, ValueError):
                    continue
                temporary = done.with_name(f".{done.name}")
                temporary.write_text(data)
                os.replace(temporary, done)  # whole, as the supervisor writes it
    threading.Thread(target=loop, daemon=True).start()
    return stop


def test_list_describe_and_history(run):
    ask(run, "1-1-1", kind="connect", destination="192.0.2.7:443", address="192.0.2.7", port=443, host="api.example.org")
    directory = ask(run, "1-1-2", kind="dns", name="pypi.org")
    (directory / "1-1-2.done").write_text(json.dumps({"decision": "deny", "reason": "no answer within 60 s"}))
    open_ = ap.questions(run)
    assert [q["id"] for _, _, q in open_] == ["1-1-1"]
    assert ap.describe(open_[0][2]) == "connect to 192.0.2.7:443, named api.example.org by the agent (not verified)"
    history = ap.questions(run, answered=True)
    assert history[1][2]["result"]["reason"] == "no answer within 60 s"
    (directory / "../x.json").resolve()  # names that are no question ids are never read
    (directory / "evil.json").write_text("{}")
    assert [q["id"] for _, _, q in ap.questions(run)] == ["1-1-1"]


def test_questions_name_the_sandbox_that_asks(run, tmp_path, monkeypatch):
    """Several sandboxes may ask at once: notifications and the list say which one."""
    monkeypatch.setattr(ap.Path, "home", classmethod(lambda cls: tmp_path))
    record = dict(run["claude-1"], session="web\x1b[2J")
    assert ap.origin(record) == r"claude-1 (~/app, session web\x1b[2J)"
    assert ap.origin({}) == ""
    directory = Path(record["directory"])
    (directory / "run.json").write_text(json.dumps({k: v for k, v in record.items() if k != "directory"}))
    titles, stop = [], threading.Event()
    thread = threading.Thread(target=ap.watch, args=(directory / "approvals", stop,
                                                     lambda title, text: titles.append(title), 0.05))
    thread.start()
    ask(run, "1-1-1", kind="dns", name="pypi.org")
    deadline = time.monotonic() + 5
    while not titles and time.monotonic() < deadline:
        time.sleep(0.05)
    stop.set()
    thread.join(5)
    assert titles == [r"sandbox-ai asks: claude-1 (~/app, session web\x1b[2J)"]
    questions, lines = ap.questions, []
    monkeypatch.setattr(ap, "questions", lambda: questions(run))
    assert ap.main(["list"], out=lines.append) == 0
    assert lines[0].split() == ["ID", "SANDBOX", "QUESTION"]
    assert [cell.strip() for cell in lines[1].split("  ") if cell.strip()] == ["1-1-1", "claude-1 (~/app)", "look up pypi.org"]
    assert lines[1].index("claude-1") == lines[0].index("SANDBOX"), "columns line up"
    assert lines[2].startswith("answer: sandbox-ai approvals allow|deny ID")


def test_answers(run, tmp_path):
    supervisor(run)
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


def test_always_is_remembered_only_once_the_sandbox_applied_it(run, tmp_path):
    """A question that timed out meanwhile, or an answer the supervisor could not apply, opens no later start."""
    stop = supervisor(run, {"decision": "deny", "scope": "once", "reason": "could not be applied: nft"})
    ask(run, "1-1-1", kind="connect", destination="192.0.2.9:5432", address="192.0.2.9", port=5432)
    lines = []
    ap.answer("1-1-1", "allow", "always", runs=run, out=lines.append)
    assert ap.approved_for(tmp_path / "app")["tcp"] == []
    assert "not remembered for the project" in lines[0] and "could not be applied" in lines[0]
    stop.set()
    ask(run, "1-1-2", kind="connect", destination="192.0.2.10:5432", address="192.0.2.10", port=5432)
    ap.answer("1-1-2", "allow", "always", runs=run, out=lines.append, wait=0.2)  # no supervisor at all
    assert ap.approved_for(tmp_path / "app")["tcp"] == []


def test_browser_urls_with_a_user_part_are_refused(run):
    """https://github.com@evil.example/ reads as github.com but opens evil.example."""
    assert not ap.URL.fullmatch("https://github.com@evil.example/login")
    assert ap.URL.fullmatch("https://example.org/a@b")  # an @ in the path is fine
    assert ap.describe({"kind": "browser", "url": "https://login.example/x"}).endswith("(host login.example)")


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


def test_questions_cannot_inject_lines_or_escapes(run, capsys):
    """What the agent chose (a TLS name, a push URL, refs) is escaped wherever it is printed."""
    ident = "1-1-1"
    ask(run, ident, kind="connect", destination="203.0.113.7:443", address="203.0.113.7", port=443,
        host="pypi.org\n1-1-2  claude-x  look up pypi.org\x1b[8m")
    ask(run, "1-1-3", kind="push", url="https://x.example/%1b\x1b]0;t\x07", refs=["refs/heads/\u202emain"])
    lines = []
    for name, _, question in ap.questions(run):
        lines.append(ap.describe(question))
    text = "\n".join(lines)
    assert "\x1b" not in text and "\u202e" not in text and "\x07" not in text
    assert all("\n" not in ap.describe(q) for _, _, q in ap.questions(run))
    assert "\\n1-1-2" in text and "\\x1b[8m" in text


def test_answer_refuses_ambiguous_ids(run, tmp_path):
    other = tmp_path / "runs" / "codex-2"
    (other / "approvals").mkdir(parents=True)
    runs = {**run, "codex-2": {**run["claude-1"], "run": "codex-2", "directory": str(other)}}
    ask(run, "5-5-5", kind="dns", name="pypi.org")
    (other / "approvals" / "5-5-5.json").write_text(json.dumps({"id": "5-5-5", "kind": "dns", "name": "evil.example"}))
    with pytest.raises(ValueError, match="several sandboxes"):
        ap.answer("5-5-5", "allow", "always", runs=runs, resolver=lambda n: ["151.101.0.223"], out=lambda line: None)
    assert not list((other / "approvals").glob("*.answer"))


def test_lookup_opens_only_public_addresses(run, tmp_path):
    ask(run, "1-1-1", kind="dns", name="x.attacker.example")
    with pytest.raises(ValueError, match="non-public"):
        ap.answer("1-1-1", "allow", "run", runs=run, resolver=lambda n: ["10.0.0.5", "127.0.0.1"], out=lambda l: None)
    assert "x.attacker.example" not in (tmp_path / "session/hosts").read_text()
    lines = []
    reply = ap.answer("1-1-1", "allow", "once", runs=run, resolver=lambda n: ["192.168.1.1", "151.101.0.223"],
                      out=lines.append)
    assert reply["addresses"] == ["151.101.0.223"] and reply["scope"] == "run"  # once means run for a name
    assert "192.168.1.1" not in (tmp_path / "session/hosts").read_text() and "not public" in lines[0]


def test_always_remembers_the_address_not_the_claimed_name(run, tmp_path):
    supervisor(run)
    ask(run, "1-1-1", kind="connect", destination="203.0.113.7:443", address="203.0.113.7", port=443,
        host="registry.npmjs.org")
    ap.answer("1-1-1", "allow", "always", runs=run, out=lambda line: None)
    assert ap.approved_for(tmp_path / "app") == {"hosts": [], "tcp": ["203.0.113.7:443"]}
    with pytest.raises(ValueError):
        ap.remember(tmp_path / "app", host="a..b")
    (tmp_path / "config/sandbox-ai/approved.json").write_text(json.dumps(
        {str((tmp_path / "app").resolve()): {"hosts": ["-x", "a..b", "ok.example"], "tcp": ["999.1.1.1:80", "1.2.3.4:0"]}}))
    assert ap.approved_for(tmp_path / "app") == {"hosts": ["ok.example"], "tcp": []}


def test_push_always_is_only_for_the_run_and_deny_is_remembered(run):
    ask(run, "1-1-1", kind="push", url="https://github.com/a/b", refs=["refs/heads/main"], message="warn")
    assert ap.answer("1-1-1", "allow", "always", runs=run, out=lambda line: None)["scope"] == "run"
    # A push cannot be blocked always (only names and addresses are): it is denied for the run.
    ask(run, "1-1-2", kind="push", url="https://github.com/a/b", refs=["refs/heads/main"], message="warn")
    lines = []
    assert ap.answer("1-1-2", "deny", "always", runs=run, out=lines.append)["scope"] == "run"
    assert "only names and addresses are blocked always" in lines[0]
    parsed = []
    original = ap.answer
    try:
        ap.answer = lambda ident, decision, scope, out=None: parsed.append((decision, scope))
        ap.main(["deny", "1-1-9"])
        ap.main(["deny", "1-1-9", "--scope", "once"])
    finally:
        ap.answer = original
    assert parsed == [("deny", "run"), ("deny", "once")]


def test_open_browser_never_uses_open(monkeypatch):
    found = []
    monkeypatch.setattr(ap.shutil, "which", lambda name: found.append(name) or None)
    with pytest.raises(ValueError):
        ap.open_browser("https://example.org/")
    assert "open" not in found


def test_browser_listener_survives_nesting_and_slow_senders(run, tmp_path):
    import socket as socket_module
    import tempfile
    spec = importlib.util.spec_from_file_location("runtime_for_browser_test3", ROOT / "sandbox/runtime.py")
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
    try:
        with socket_module.socket(socket_module.AF_UNIX) as client:
            client.settimeout(10)
            client.connect(str(short / "open.sock"))
            client.sendall(b"[" * 8000 + b"\n")
            assert json.loads(client.makefile("r").readline())["ok"] is False
        slow = socket_module.socket(socket_module.AF_UNIX)
        slow.connect(str(short / "open.sock"))
        slow.sendall(b"{")  # and nothing more
        started = time.monotonic()
        with socket_module.socket(socket_module.AF_UNIX) as client:
            client.settimeout(15)
            client.connect(str(short / "open.sock"))
            client.sendall(b'{"url": "https://example.org/a"}\n')
            assert json.loads(client.makefile("r").readline())["ok"] is True
        assert time.monotonic() - started < 6  # the slow sender held the listener at most ~3 s
        slow.close()
        assert thread.is_alive()
        with socket_module.socket(socket_module.AF_UNIX) as client:
            client.settimeout(10)
            client.connect(str(short / "open.sock"))
            client.sendall(b'{"url": "https://ex\xc3\xa4mple.org/"}\n')  # not printable ASCII
            assert json.loads(client.makefile("r").readline())["ok"] is False
    finally:
        stop.set()
        thread.join(5)


def test_watch_groups_bursts_and_leaves_browser_requests_to_their_listener(run):
    directory = Path(run["claude-1"]["directory"]) / "approvals"
    notes, stop = [], threading.Event()
    for n in range(5):
        ask(run, f"1-1-{n + 1}", kind="dns", name=f"a{n}.example")
    ask(run, "1-1-9", kind="browser", url="https://example.org/")
    thread = threading.Thread(target=ap.watch, args=(directory, stop, lambda t, text: notes.append(text), 0.05))
    thread.start()
    deadline = time.monotonic() + 5
    while not notes and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.2)
    stop.set()
    thread.join(5)
    assert notes == ["5 new questions\nsandbox-ai approvals list"]


def test_remembered_and_forget(run, tmp_path):
    """What was allowed "always" can be listed and forgotten again."""
    ap.remember(tmp_path / "app", host="pypi.org")
    ap.remember(tmp_path / "app", tcp="192.0.2.9:5432")
    project = str((tmp_path / "app").resolve())
    known = ap.remembered()[project]
    assert {k: v for k, v in known.items() if k != "details"} == {
        "hosts": ["pypi.org"], "tcp": ["192.0.2.9:5432"], "blocked_hosts": [], "blocked_tcp": []}
    assert set(known["details"]) == {"hosts:pypi.org", "tcp:192.0.2.9:5432"}  # each with the time it was added
    lines = []
    assert ap.main(["remembered"], out=lines.append) == 0 and lines[0] == project
    assert [line.split("  ")[1] for line in lines[1:]] == ["allow pypi.org", "allow 192.0.2.9:5432"]
    assert lines[1].split("  ")[2].startswith("name; added 20") and lines[2].split("  ")[2].startswith("address; added")
    assert ap.main(["forget", project, "pypi.org"], out=lines.append) == 0
    assert ap.approved_for(tmp_path / "app") == {"hosts": [], "tcp": ["192.0.2.9:5432"]}
    assert ap.main(["forget", project, "pypi.org"], out=lines.append) == 1  # not there any more
    assert ap.forget(project, "192.0.2.9:5432") and ap.remembered() == {}
    lines.clear()
    assert ap.main(["remembered", "--json"], out=lines.append) == 0 and json.loads(lines[0]) == {}


def test_block_always_is_remembered_and_refused_without_a_question(run, tmp_path):
    """deny --scope always: denied for the run now, refused at once at every later start of the project."""
    supervisor(run)
    ask(run, "1-1-1", kind="connect", destination="192.0.2.9:5432", address="192.0.2.9", port=5432)
    reply = ap.answer("1-1-1", "deny", "always", runs=run, out=lambda line: None)
    assert reply == {"decision": "deny", "scope": "run"}
    assert ap.blocked_for(tmp_path / "app") == {"hosts": [], "tcp": ["192.0.2.9:5432"]}
    ask(run, "1-1-2", kind="dns", name="tracker.example")
    ap.answer("1-1-2", "deny", "always", runs=run, out=lambda line: None)
    assert ap.blocked_for(tmp_path / "app")["hosts"] == ["tracker.example"]
    # Allowing it always later moves it from blocked to allowed (one excludes the other).
    ap.remember(tmp_path / "app", host="tracker.example")
    assert ap.blocked_for(tmp_path / "app")["hosts"] == [] and ap.approved_for(tmp_path / "app")["hosts"] == ["tracker.example"]
    project = str((tmp_path / "app").resolve())
    assert ap.remembered()[project]["blocked_tcp"] == ["192.0.2.9:5432"]
    lines = []
    ap.main(["remembered"], out=lines.append)
    assert any(line.startswith("  block 192.0.2.9:5432  address; added ") and "from question 1-1-1" in line
               for line in lines)
    assert any(line.startswith("  allow tracker.example  name; added ") for line in lines)
    assert ap.forget(project, "192.0.2.9:5432") and ap.blocked_for(tmp_path / "app")["tcp"] == []


def test_buttons_in_the_notification_answer_the_question(monkeypatch):
    """Allow / Allow always / Block / Block always in the notification; the click answers as the CLI would."""
    monkeypatch.setattr(ap, "policy_scope", lambda: "always")
    answered, commands = [], []

    def runner(command, **kwargs):
        commands.append(command)
        return type("Done", (), {"stdout": "never\n"})()
    chosen = ap.desktop_question("sandbox-ai asks: claude-1", "connect to 192.0.2.9:5432", "1-1-1", runner=runner,
                                 answer_with=lambda ident, decision, scope, out: answered.append((ident, decision, scope)))
    assert chosen == "never" and answered == [("1-1-1", "deny", "always")]
    assert "--action=always=Allow always" in commands[0] and "--wait" in commands[0]
    # A policy capping the scope: no "Allow always" button.
    monkeypatch.setattr(ap, "policy_scope", lambda: "run")
    ap.desktop_question("t", "x", "1-1-2", runner=runner, answer_with=lambda *a, **k: None)
    assert "--action=always=Allow always" not in commands[1] and "--action=never=Block always" in commands[1]


@pytest.mark.parametrize("mode, cap", [("high", "run"), ("paranoid", "once")])
def test_answers_keep_to_the_mode_of_the_run(run, tmp_path, mode, cap):
    """A run in a strict security mode (run.json "mode"): an answer wider than its scope counts as the widest it
    allows, and nothing is remembered for the project."""
    run["claude-1"]["mode"] = {"name": mode, "origin": "--mode", "refuses": ["--allow-host"], "approval_scope": cap}
    supervisor(run)
    ask(run, "1-1-1", kind="connect", destination="192.0.2.9:5432", address="192.0.2.9", port=5432)
    lines = []
    reply = ap.answer("1-1-1", "allow", "always", runs=run, out=lines.append)
    assert reply["scope"] == cap and ap.approved_for(tmp_path / "app")["tcp"] == []
    assert any(f"security mode {mode} of this run approves at most" in line for line in lines)
    ask(run, "1-1-2", kind="connect", destination="192.0.2.10:5432", address="192.0.2.10", port=5432)
    assert ap.answer("1-1-2", "deny", "always", runs=run, out=lambda line: None)["decision"] == "deny"


def test_list_as_json_for_the_web_interface(run, monkeypatch):
    """approvals list --json: the same questions as the table, escaped the same way."""
    ask(run, "1-1-1", kind="dns", name="pypi.org\x1b[2J")
    real = ap.questions
    monkeypatch.setattr(ap, "questions", lambda runs=None, answered=False: real(run, answered))
    lines = []
    assert ap.main(["list", "--json"], out=lines.append) == 0
    (entry,) = json.loads(lines[0])
    assert entry["id"] == "1-1-1" and entry["run"] == "claude-1" and entry["kind"] == "dns"
    assert "\x1b" not in entry["question"] and "pypi.org" in entry["question"] and "\\x1b" in entry["question"]
    assert entry["sandbox"].startswith("claude-1")


# --- host wildcards: the launcher answers the lookups below them -------------------------------------------------


def wildcard_run(tmp_path, wildcards=("*.example.org",), added=()):
    directory = tmp_path / "run"
    (directory / "approvals").mkdir(parents=True)
    session = tmp_path / "session"
    session.mkdir()
    (session / "hosts").write_text("127.0.0.1 localhost\n")
    record = {"run": "claude-1", "pid": os.getpid(), "workspace": str(tmp_path), "session_dir": str(session),
              "allow_wildcards": list(wildcards)}
    (directory / "run.json").write_text(json.dumps(record))
    if added:
        (directory / "wildcards.json").write_text(json.dumps(list(added)))
    return {**record, "directory": str(directory)}


def test_a_lookup_below_a_wildcard_is_matched_and_capped(tmp_path, monkeypatch):
    record = wildcard_run(tmp_path, added=["*.added.example"])
    dns = lambda name: {"kind": "dns", "name": name, "id": "1-2-3"}  # noqa: E731
    assert ap.under_wildcard(dns("api.example.org"), record) == "*.example.org"
    assert ap.under_wildcard(dns("x.added.example"), record) == "*.added.example"
    assert ap.under_wildcard(dns("example.org"), record) is None  # not the domain itself
    assert ap.under_wildcard(dns("evilexample.org"), record) is None
    assert ap.under_wildcard({"kind": "connect", "name": "api.example.org"}, record) is None
    assert ap.under_wildcard(dns("bad name.example.org"), record) is None
    # The agent chooses the names: past the cap they are asked of the human, as any lookup.
    monkeypatch.setattr(ap, "WILDCARD_NAMES", 3)
    ap.net().update_hosts(record, {f"n{i}.example.org": [f"192.0.2.{i}"] for i in range(3)})
    assert ap.under_wildcard(dns("n9.example.org"), record) is None
    assert ap.under_wildcard(dns("x.added.example"), record) == "*.added.example"  # each wildcard its own


def test_the_answer_below_a_wildcard_keeps_to_the_policy(tmp_path, monkeypatch):
    record = wildcard_run(tmp_path)
    answered = []
    fake = lambda ident, decision, scope, runs=None, out=None: answered.append((ident, decision, scope)) or \
        {"decision": decision}  # noqa: E731
    monkeypatch.setattr(ap, "policy_allows_host", lambda name: True)
    ap.answer_wildcard({"id": "1-2-3", "kind": "dns", "name": "api.example.org"}, record, "*.example.org", fake)
    assert answered == [("1-2-3", "allow", "run")]
    monkeypatch.setattr(ap, "policy_allows_host", lambda name: False)
    result = ap.answer_wildcard({"id": "1-2-4", "kind": "dns", "name": "api.example.org"}, record, "*.example.org", fake)
    assert answered[-1] == ("1-2-4", "deny", "run") and "policy" in result["reason"]  # the held lookup ends now


def test_the_watch_answers_wildcard_lookups_itself_and_tells_nobody(tmp_path):
    record = wildcard_run(tmp_path)
    approvals = Path(record["directory"]) / "approvals"
    (approvals / "1700000000-1-1.json").write_text(json.dumps({"id": "1700000000-1-1", "kind": "dns",
                                                               "name": "api.example.org"}))
    (approvals / "1700000000-1-2.json").write_text(json.dumps({"id": "1700000000-1-2", "kind": "dns",
                                                               "name": "other.example"}))
    stop, notes, auto = threading.Event(), [], []
    thread = threading.Thread(target=ap.watch, args=(approvals, stop), kwargs={
        "notify": lambda title, text: notes.append(text), "interval": 0.05,
        "wildcards": lambda question, rec, wildcard: auto.append((question["name"], wildcard))})
    thread.start()
    deadline = time.monotonic() + 5
    while (not auto or not notes) and time.monotonic() < deadline:
        time.sleep(0.05)
    stop.set()
    thread.join(5)
    assert auto == [("api.example.org", "*.example.org")]
    assert len(notes) == 1 and "other.example" in notes[0]  # only the other one is announced
