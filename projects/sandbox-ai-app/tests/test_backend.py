"""The app's view of sandbox-ai, without Qt, against the sandbox-ai checkout in SANDBOX_AI_HOME."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sandbox_ai_app import backend  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


@pytest.fixture
def sai(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    (tmp_path / "runtime").mkdir()
    return backend.SandboxAI()


def test_every_launcher_option_is_in_the_form(sai):
    options = sai.options()
    names = {o.long for o in options}
    parser = sai.launcher.build_parser()
    visible = {next(s for s in a.option_strings if s.startswith("--")) for a in parser._actions
               if a.option_strings and a.help != "==SUPPRESS=="}
    assert visible - names == backend.HIDDEN & visible
    by = {o.long: o for o in options}
    assert by["--dangerously-skip-permissions"].kind == "flag" and by["--dangerously-skip-permissions"].short == "-D"
    assert by["--profile"].kind == "multi" and "development" in by["--profile"].choices
    assert by["--allow-host"].kind == "list" and by["--gpu"].kind == "choice" and by["--log-dir"].kind == "value"
    assert by["--device"].group == "Hardware" and by["--workspace"].is_path
    assert not [o.long for o in options if o.group == "More"], "every option belongs to a group"


def test_values_and_argv_round_trip(sai):
    options = sai.options()
    values = {"--workspace": ["/tmp/a", "/tmp/b"], "--allow-host": ["example.org"], "--profile": ["development"],
              "--dangerously-skip-permissions": True, "--gpu": "", "--backend": "podman", "--log-dir": " /tmp/logs ",
              "--offline": False}
    argv = backend.argv_from_values(options, values)
    # In the launcher's own order; the default backend and empty values are left out.
    assert argv == ["--workspace", "/tmp/a", "--workspace", "/tmp/b", "--profile", "development", "--allow-host",
                    "example.org", "--dangerously-skip-permissions", "--log-dir", "/tmp/logs"]
    back, unknown = backend.values_from_argv(options, argv, sai.launcher.canonical_options)
    assert unknown == [] and back["--workspace"] == ["/tmp/a", "/tmp/b"] and back["--dangerously-skip-permissions"]
    # Short forms, bundles and attached values are read like the launcher reads them.
    short, unknown = backend.values_from_argv(options, ["-Dg", "-w/tmp/c", "-A", "x.org", "--bogus"],
                                              sai.launcher.canonical_options)
    assert short == {"--dangerously-skip-permissions": True, "--allow-github": True, "--workspace": ["/tmp/c"],
                     "--allow-host": ["x.org"]} and unknown == ["--bogus"]


def test_dry_run_and_launch_lines(sai, tmp_path):
    options = sai.options()
    argv = backend.argv_from_values(options, {"--workspace": [str(tmp_path / "app")], "--allow-github": True})
    plan, message = sai.dry_run("codex", argv)
    assert plan["agent"] == "codex" and "github.com" in plan["https_hosts"], message
    plan, message = sai.dry_run("codex", argv + ["--device", "/dev/null"])
    assert plan is None and "refused" in message
    line = sai.launch_argv("claude", ["-D"], ["--model", "x"], session="s1")
    assert line[-7:] == ["claude", "-D", "--session", "s1", "--", "--model", "x"]
    terminal = sai.in_terminal(["echo", "hi there"])
    if terminal:
        assert terminal[-1].startswith("echo 'hi there'; status=$?")


def test_profiles_and_project_configuration(sai, tmp_path):
    sai.save_profile("work", "claude", ["-D", "--allow-github"], ["--model", "x"])
    assert sai.profiles() == {"work": {"agent": "claude", "options": ["-D", "--allow-github"], "agent_args": ["--model", "x"]}}
    assert oct(sai.profiles_file().stat().st_mode & 0o777) == "0o600"
    sai.delete_profile("work")
    assert sai.profiles() == {}
    project, other = tmp_path / "project", tmp_path / "lib"
    project.mkdir()
    options = sai.options()
    values = {"--workspace": [str(project), str(other)], "--allow-pypi": True, "--config": "/x.json"}
    document, main = sai.project_document("codex", options, values, ["--model", "m"])
    assert main == str(project) and document == {"version": 1, "agent": "codex", "options": ["--allow-pypi"],
                                                 "workspaces": [str(other)], "agent_args": ["--model", "m"]}
    path = sai.write_project(project, document)
    # Trusted exactly as written: the launcher loads it.
    a = sai.launcher.parse_args(["codex", "--workspace", str(project)])
    _, loaded = sai.launcher.load_project_config(a)
    assert loaded["options"] == ["--allow-pypi"] and path.name == ".sandbox-ai.json"
    review = sai.review_project(project)
    assert review.trusted and review.usable and review.agent == "codex" and review.workspaces == [str(other)]


def test_connection_log_reading_and_summary(tmp_path):
    log = tmp_path / "connections-x.jsonl"
    log.write_text("\n".join(json.dumps(r) for r in [
        {"time": "t1", "event": "http", "method": "GET", "url": "https://a/x", "status": 200, "bytes_out": 10, "bytes_in": 5},
        {"time": "t2", "event": "http", "method": "POST", "url": "https://github.com/o/r.git/git-receive-pack",
         "status": 200, "push": {"ok": False, "refs": ["refs/heads/main"]}},
        {"time": "t3", "event": "denied", "destination": "192.0.2.1:22", "reason": "SSH refused"},
    ]) + "\nbroken\n")
    records = backend.read_connections(log)
    assert len(records) == 3 and backend.read_connections(tmp_path / "missing") == []
    assert backend.summary(records[0]) == ("t1", "http", "GET", "200", "https://a/x", "15")
    assert backend.summary(records[1])[1] == "push REFUSED"
    assert backend.summary(records[2])[4] == "192.0.2.1:22  (SSH refused)"
    assert len(backend.read_connections(log, limit=2)) == 2


def test_state_readers_work_on_an_empty_host(sai):
    assert sai.runs() == {} and sai.sessions() == [] and sai.log_directories() == []
    assert any(name == "demo" for name, _, _ in sai.mcp_servers())
    assert sai.version().count(".") == 2


# -- approvals, statistics, credentials, files, updates --

def fake_run(tmp_path, name="claude-abc", workspace=None, session=None):
    """A registered run of this (living) test process, as the launcher writes it."""
    directory = tmp_path / "runtime" / "sandbox-ai" / "runs" / name
    (directory / "approvals").mkdir(parents=True)
    workspace = workspace or tmp_path / "project"
    Path(workspace).mkdir(exist_ok=True)
    (directory / "run.json").write_text(json.dumps({
        "run": name, "agent": "claude", "backend": "podman", "workspace": str(workspace), "pid": os.getpid(),
        "session": session, "session_dir": str(tmp_path / "s"), "started": "2026-11-03T10:00:00Z", "proxy": True}))
    return directory


def ask(directory, ident, **question):
    (directory / "approvals" / f"{ident}.json").write_text(json.dumps({"id": ident, "time": "2026-11-03T10:00:00Z",
                                                                        "timeout": 60, **question}))


def test_question_timing_and_url():
    asked = backend.parse_time("2026-11-03T10:00:00Z")
    assert backend.question_timing({"kind": "dns", "time": "2026-11-03T10:00:00Z"}, asked + 15) == (15, 45)
    assert backend.question_timing({"kind": "connect", "time": "2026-11-03T10:00:00Z", "timeout": 30}, asked + 40) == (40, 0)
    assert backend.question_timing({"kind": "browser", "time": "2026-11-03T10:00:00Z"}, asked + 500) == (500, None)
    assert backend.question_timing({"kind": "dns", "time": "garbage"}) == (None, None)
    assert backend.question_url({"kind": "browser", "url": "https://x.example/\x1b[2J"}) == "https://x.example/\\x1b[2J"
    assert backend.question_url({"kind": "dns"}) is None


def test_questions_and_answers_through_the_command_line(sai, tmp_path):
    run = fake_run(tmp_path)
    ask(run, "1700000000-1-1", kind="dns", name="pypi.org")
    ask(run, "1700000000-1-2", kind="connect", destination="192.0.2.7:5432", address="192.0.2.7", port=5432)
    pending = sai.questions()
    assert [(r, q["id"]) for r, _, q in pending] == [("claude-abc", "1700000000-1-1"), ("claude-abc", "1700000000-1-2")]
    assert sai.describe(pending[1][2]).startswith("connect to 192.0.2.7:5432")
    status, out, err = sai.answer("1700000000-1-2", "deny", "once")
    assert status == 0, err
    assert json.loads((run / "approvals" / "1700000000-1-2.answer").read_text()) == {"decision": "deny", "scope": "once"}
    assert [q["id"] for _, _, q in sai.questions()] == ["1700000000-1-1"]
    (run / "approvals" / "1700000000-1-2.done").write_text(json.dumps({"decision": "deny", "reason": "answered"}))
    history = sai.questions(answered=True)
    assert {q["id"]: q.get("result", {}).get("decision") for _, _, q in history} == {
        "1700000000-1-1": None, "1700000000-1-2": "deny"}
    assert sai.answer("1; rm -rf /", "allow")[0] == 2 and sai.answer("1-1-1", "allow", "forever")[0] == 2
    status, _, err = sai.answer("1700000000-9-9", "allow", "run")
    assert status == 1 and "no open question" in err


def test_answer_command_lines():
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "", "")
    s = backend.SandboxAI(runner=runner)
    s.answer("1-2-3", "allow", "always")
    s.answer("1-2-3", "deny", "always")
    assert calls[0][0][-5:] == ["approvals", "allow", "1-2-3", "--scope", "always"]
    assert calls[1][0][-5:] == ["approvals", "deny", "1-2-3", "--scope", "always"]  # blocked for the project


def test_the_policy_caps_answers_and_lists_what_it_forbids(monkeypatch, tmp_path):
    calls = []
    s = backend.SandboxAI(runner=lambda argv, **k: calls.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""))
    policy = tmp_path / "policy.json"
    policy.write_text('{"version": 1, "forbid": ["--no-proxy", "--ask=off"], "max_approval_scope": "run"}')
    monkeypatch.setattr(s.launcher, "POLICY_FILE", policy)
    monkeypatch.setattr(s.launcher, "load_policy", lambda path=None: json.loads(policy.read_text()))
    assert s.max_scope() == "run" and s.forbidden() == {"--no-proxy": {None}, "--ask": {"off"}}
    assert s.answer("1-2-3", "allow", "always")[0] == 2 and calls == [], "wider than the policy: not even asked"
    assert s.answer("1-2-3", "deny", "run")[0] == 0 and calls[-1][-1] == "run"
    found, problem, text, path = s.policy()
    assert problem is None and '"max_approval_scope": "run"' in text and path == policy
    monkeypatch.setattr(s.launcher, "load_policy", lambda path=None: (_ for _ in ()).throw(ValueError("broken")))
    assert s.policy()[:2] == ({}, "broken") and s.max_scope() == "always"


def test_secrets_go_on_stdin_never_into_argv(tmp_path):
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "kept", "")
    s = backend.SandboxAI(runner=runner)
    assert s.set_secret("API_TOKEN", "s3cr3t-value")[0] == 0
    argv, kwargs = calls[0]
    assert argv[-3:] == ["secrets", "set", "API_TOKEN"] and kwargs["input"] == "s3cr3t-value\n"
    assert "s3cr3t-value" not in " ".join(argv) and "stdin" not in kwargs
    for name, value in (("1BAD", "x"), ("A B", "x"), ("OK", ""), ("OK", "two\nlines"), ("OK", "x" * 9000)):
        assert s.set_secret(name, value)[0] == 2
    assert len(calls) == 1 and s.delete_secret("../x")[0] == 2


def test_secrets_round_trip_with_the_file_store(sai, tmp_path, monkeypatch):
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert sai.secrets() == ("file", [])
    status, out, err = sai.set_secret("DEMO_TOKEN", "value-123")
    assert status == 0, err
    assert "value-123" not in out + err
    assert sai.secrets() == ("file", [("DEMO_TOKEN", "file")])
    assert sai.delete_secret("DEMO_TOKEN")[0] == 0 and sai.secrets()[1] == []


def test_login_status_rows(sai):
    rows = sai.login_status()
    assert [r[0] for r in rows] == list(sai.agents)  # the launcher's order
    assert all(r[1] in ("store", "host", "?") for r in rows)


def test_paths_of_runs_and_sessions(sai, tmp_path):
    launcher = sai.launcher
    paths = backend.paths_from_options(launcher, ["-w", "/a,/b", "--workspace=rel", "-S", "home", "-D", "--", "-w", "/x"],
                                       "/base")
    assert paths == {"workspaces": ["/a", "/b", "/base/rel"], "state_dir": "/base/home", "mounts": []}
    # --workspace-ro (-W) is a flag: the option after it is read as an option, not as its value.
    paths = backend.paths_from_options(launcher, ["-W", "-w", "/ro", "--workspace-ro", "--state-dir", "st"], "/base")
    assert paths == {"workspaces": ["/ro"], "state_dir": "/base/st", "mounts": []}
    # rw mount sources are folders the sandbox writes; ro ones are not.
    assert backend.paths_from_options(launcher, ["-m", "/data:/mnt/data:rw", "--mount=rel:/x:rw", "--mount",
                                                 "/etc/ssl:/etc/ssl:ro"], "/base")["mounts"] == ["/data", "/base/rel"]
    # A value of another option is never taken for a workspace.
    assert backend.paths_from_options(launcher, ["--env", "-w", "-w", "/a"], "/base")["workspaces"] == ["/a"]
    assert backend.option_arity(launcher)["--workspace-ro"] == 0 and backend.option_arity(launcher)["-w"] is None
    proc = tmp_path / "proc" / "4242"
    proc.mkdir(parents=True)
    (proc / "cmdline").write_bytes(b"\0".join([b"python3", b"-I", b"/x/agent_sandbox.py", b"codex", b"-w",
                                                str(tmp_path / "w2").encode(), b"--state-dir", b"st", b""]))
    (proc / "cwd").symlink_to(tmp_path)
    assert backend.run_paths(launcher, 4242, tmp_path / "proc") == {"workspaces": [str(tmp_path / "w2")],
                                                                    "state_dir": str(tmp_path / "st"), "mounts": []}
    assert backend.run_paths(launcher, "nope", tmp_path / "proc")["workspaces"] == []


def test_file_roots_of_runs_and_sessions(sai, tmp_path):
    fake_run(tmp_path, workspace=tmp_path / "project", session="work")
    session = tmp_path / "state" / "sandbox-ai" / "sessions" / "work"
    (session / "home").mkdir(parents=True)
    (tmp_path / "lib").mkdir()
    (session / "session.json").write_text(json.dumps({
        "agent": "codex", "options": ["-w", f"{tmp_path / 'project'},{tmp_path / 'lib'}"], "backend": "podman",
        "workspace": str(tmp_path / "project"), "cwd": str(tmp_path), "created": "2026-11-03T10:00:00Z"}))
    roots = sai.file_roots(extra=[("project", "current project", str(tmp_path / "project")),
                                  ("project", "missing", str(tmp_path / "missing"))])
    assert [(kind, label, Path(path).name) for kind, label, path in roots] == [
        ("run", "work", "project"), ("session-home", "work", "home"), ("session", "work", "lib")]  # a session's run: its name


def test_statistics_of_a_log_directory(sai, tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "connections-1.jsonl").write_text("\n".join(json.dumps(r) for r in [
        {"event": "http", "host": "api.anthropic.com", "model": {"model": "claude-x", "usage": {"input_tokens": 10,
                                                                                             "output_tokens": 5}},
         "credentials": ["x-api-key"]},
        {"event": "http", "host": "github.com", "push": {"ok": False}, "credentials": ["Authorization"]},
        {"event": "denied", "destination": "192.0.2.1:22"}]) + "\n")
    (logs / "processes.jsonl").write_text(json.dumps({"command": "/usr/bin/git push"}) + "\n")
    mcp = tmp_path / "state" / "sandbox-ai" / "mcp" / "files"
    mcp.mkdir(parents=True)
    (mcp / "supervisor.log").write_text('sandbox-ai-mcp-call {"time": "t", "tool": "read_file"}\n')
    data = sai.statistics(logs)
    assert data["requests"] == 2 and data["model_calls"] == {"claude-x": 1} and data["tokens"]["input_tokens"] == 10
    assert data["pushes"] == {"ok": 0, "refused": 1} and data["programs"] == [("git", 1)]
    assert [call[:2] for call in data["tool_calls"]["files"]] == [("t", "read_file")]
    assert backend.split_credential(data["credentials"][0][0]) in (("x-api-key", "api.anthropic.com"),
                                                                    ("Authorization", "github.com"))


def test_update_check_and_versions():
    assert backend.parse_update_check(0, "installed: 0.9.1 (/x)\navailable: 0.10.0 (https://github.com/x)\n") == \
        ("0.9.1", "0.10.0")
    assert backend.parse_update_check(1, "sandbox-ai update: this copy was not installed by install.sh") is None
    assert backend.parse_update_check(0, "installed: none (/x)\navailable: ? \n") is None
    assert backend.newer("0.10.0", "0.9.1") and not backend.newer("0.9.1", "0.9.1") and not backend.newer("x", "0.1")


def test_image_and_package_rows():
    rows = backend.image_rows([{"agent": "claude", "present": True, "agent_version": "2.1.0",
                                "created": "2026-11-01T10:00:00", "size": 1_500_000_000, "image": "localhost/c"},
                               {"agent": "codex", "present": False, "image": "localhost/x"}, "junk"])
    assert rows == [("claude", True, "2.1.0", "2026-11-01 10:00:00", "1.50 GB", "localhost/c"),
                    ("codex", False, "", "", "", "localhost/x")]
    assert backend.package_rows([{"kind": "deb", "name": "git", "version": "1"}, 3]) == [("deb", "git", "1")]


def test_settings_are_private(sai):
    sai.save_setting("theme", "dark")
    assert sai.settings() == {"theme": "dark"}
    assert oct(sai.settings_file().stat().st_mode & 0o777) == "0o600"


def test_modules_load_once_even_from_several_threads(monkeypatch):
    import threading
    import time as clock
    s = backend.SandboxAI()
    real = backend.importlib.util.module_from_spec
    made = []

    def slow(spec):  # widens the window between "not loaded yet" and "loaded"
        made.append(spec.name)
        clock.sleep(0.05)
        return real(spec)
    monkeypatch.setattr(backend.importlib.util, "module_from_spec", slow)
    found = []
    threads = [threading.Thread(target=lambda: found.append(s.module("logs"))) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert len(found) == 8 and len({id(m) for m in found}) == 1 and made == ["sandbox_ai_app_logs"]


def test_reading_a_log_strictly_reports_what_cannot_be_read(tmp_path):
    os.mkfifo(tmp_path / "connections-fifo.jsonl")
    assert backend.read_connections(tmp_path / "connections-fifo.jsonl") == []
    with pytest.raises(OSError):
        backend.read_connections(tmp_path / "connections-fifo.jsonl", strict=True)
    with pytest.raises(OSError):
        backend.read_connections(tmp_path / "missing.jsonl", strict=True)


SPAWNER = """
import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
open(sys.argv[1], "w").write(str(child.pid))
time.sleep(120)
"""


def gone(pid, timeout=10):
    import time as clock
    deadline = clock.monotonic() + timeout
    while clock.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        try:  # a zombie of an orphan counts as gone
            if Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[0] == "Z":
                return True
        except OSError:
            return True
        clock.sleep(0.05)
    return False


def test_commands_are_stopped_with_everything_they_started(tmp_path):
    import threading
    import time as clock
    s = backend.SandboxAI()
    pid_file = tmp_path / "child"
    s.command = lambda *args: [sys.executable, "-c", SPAWNER, str(pid_file)]
    results = []
    worker = threading.Thread(target=lambda: results.append(s.call("mcp", "add", timeout=300)))
    worker.start()
    deadline = clock.monotonic() + 20
    while not pid_file.exists() or not pid_file.read_text():
        assert clock.monotonic() < deadline
        clock.sleep(0.02)
    child = int(pid_file.read_text())
    assert s.stop_commands(grace=2) == 1
    worker.join(10)
    assert results and results[0][0] != 0, "the command ended (by a signal)"
    assert gone(child), "its child was in its process group and ended too"
    assert s.stop_commands() == 0


def test_a_command_past_its_timeout_ends_with_its_children(tmp_path):
    s = backend.SandboxAI()
    pid_file = tmp_path / "child"
    s.command = lambda *args: [sys.executable, "-c", SPAWNER, str(pid_file)]
    status, out, err = s.call("privacy-check", "/x", timeout=1.5)
    assert status == 124 and "no answer within 1.5 s" in err
    assert gone(int(pid_file.read_text()))



DAEMON = """
import subprocess, sys
# A descendant that leaves the process group (setsid, like a daemon, ssh or dbus) and keeps stdout and stderr.
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
open(sys.argv[1], "w").write(str(child.pid))
print("started", flush=True)
"""


def test_a_command_whose_daemon_keeps_its_output_still_ends_at_its_timeout(tmp_path):
    """Regression: the readers of stdout and stderr kept blocking on the pipes the daemon holds, and closing a
    stream under a read() blocked forever on its lock: call() never returned (buttons stayed grey)."""
    import threading
    import time as clock
    s = backend.SandboxAI()
    pid_file = tmp_path / "daemon"
    s.command = lambda *args: [sys.executable, "-c", DAEMON, str(pid_file)]
    results = []
    began = clock.monotonic()
    worker = threading.Thread(target=lambda: results.append(s.call("hosts", "test", timeout=2)), daemon=True)
    worker.start()
    try:
        worker.join(25)
        assert results, "call() returned"
        assert clock.monotonic() - began < 17, "within the timeout and a short drain"
        status, out, err = results[0]
        assert status == 124 and "no answer within 2 s" in err and "started" in out, "what it printed is kept"
        assert s.stop_commands() == 0
    finally:
        if pid_file.exists() and pid_file.read_text():
            try:
                os.kill(int(pid_file.read_text()), 9)
            except ProcessLookupError:
                pass


STUBBORN = """
import os, signal, subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c",
                          "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(120)"])
open(sys.argv[1], "w").write(str(child.pid))
"""  # the lead exits at once; its child ignores SIGTERM and keeps the output open


def test_stopping_kills_the_group_even_after_its_lead_exited_within_one_grace_period(tmp_path):
    import threading
    import time as clock
    s = backend.SandboxAI()
    pid_file = tmp_path / "child"
    s.command = lambda *args: [sys.executable, "-c", STUBBORN, str(pid_file)]
    results = []
    worker = threading.Thread(target=lambda: results.append(s.call("session", "delete", timeout=300)))
    worker.start()
    deadline = clock.monotonic() + 20
    while not pid_file.exists() or not pid_file.read_text():
        assert clock.monotonic() < deadline
        clock.sleep(0.02)
    child = int(pid_file.read_text())
    clock.sleep(0.3)  # the lead has exited by now (a zombie: not reaped while the output is open)
    began = clock.monotonic()
    assert s.stop_commands(grace=1) == 1
    assert clock.monotonic() - began < 2.5, "one grace period, then SIGKILL"
    worker.join(10)
    assert results and gone(child), "the child ignoring SIGTERM was killed with its group"
    assert s.stop_commands() == 0, "a reaped group is never signalled again"


def test_what_was_allowed_always_is_listed_and_forgotten(sai, tmp_path):
    approvals = sai.module("approvals")
    if not hasattr(approvals, "remembered"):
        pytest.skip("this sandbox-ai has no approvals.remembered")
    project = tmp_path / "project"
    project.mkdir()
    approvals.remember(project, host="pypi.org")
    approvals.remember(project, tcp="192.0.2.5:5432")
    rows = sai.remembered()
    assert rows == [(str(project.resolve()), "pypi.org", "host"), (str(project.resolve()), "192.0.2.5:5432", "tcp")]
    status, out, err = sai.forget_remembered(str(project.resolve()), "pypi.org")
    assert status == 0, err
    assert sai.remembered() == [(str(project.resolve()), "192.0.2.5:5432", "tcp")]
    if "blocked" in approvals.remember.__code__.co_varnames:  # Block always (newer sandbox-ai)
        approvals.remember(project, host="tracker.example", blocked=True)
        assert (str(project.resolve()), "tracker.example", "blocked-host") in sai.remembered()
    assert sai.forget_remembered("--x", "pypi.org")[0] == 2


def test_question_context_resolves_names_and_finds_the_runs_connections(sai, tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    record = {"workspace": str(workspace)}
    logs = sai.launcher.default_log_dir(workspace)
    logs.mkdir(parents=True)
    (logs / "connections-20261103-100000-claude-abc.jsonl").write_text("".join(json.dumps(r) + "\n" for r in [
        {"time": f"t{i}", "event": "denied", "destination": "192.0.2.7:5432", "reason": "not allowed"}
        for i in range(7)] + [{"time": "t-other", "event": "http", "url": "https://elsewhere.example/x"},
                              {"time": "t-name", "event": "http", "url": "https://db.example/q"}]))
    context = sai.question_context("claude-abc", record, {"kind": "connect", "destination": "192.0.2.7:5432",
                                                          "address": "192.0.2.7", "port": 5432, "protocol": "tls",
                                                          "host": "db.example"})
    assert (context["address"], context["port"], context["protocol"], context["host"]) == \
        ("192.0.2.7", 5432, "tls", "db.example")
    assert [row[0] for row in context["recent"]] == ["t3", "t4", "t5", "t6", "t-name"], "the last five to it"
    assert sai.question_context("other-run", record, {"kind": "connect", "destination": "x"})["recent"] == []
    dns = sai.question_context("claude-abc", record, {"kind": "dns", "name": "db.example"})
    assert dns["name"] == "db.example" and [row[0] for row in dns["recent"]] == ["t-name"]
    assert "addresses" not in dns


def test_question_context_never_looks_anything_up(sai, tmp_path, monkeypatch):
    """The name in a question is the agent's (<secret>.attacker.example): resolving it before the human
    allows it would send it out anyway."""
    import socket

    def leak(*args, **kwargs):
        raise AssertionError("a DNS lookup of an agent-chosen name")
    approvals = sai.module("approvals")
    for owner, name in ((socket, "getaddrinfo"), (socket, "gethostbyname"), (socket, "gethostbyname_ex"),
                        (socket, "create_connection"), (approvals, "resolve"), (approvals, "public_addresses")):
        monkeypatch.setattr(owner, name, leak)
    record = {"workspace": str(tmp_path)}
    for question in ({"kind": "dns", "name": "c2VjcmV0.attacker.example"},
                     {"kind": "connect", "destination": "192.0.2.7:443", "address": "192.0.2.7", "port": 443,
                      "protocol": "tls", "host": "c2VjcmV0.attacker.example"}):
        context = sai.question_context("claude-abc", record, question)
        assert context["kind"] == question["kind"]


def test_autostart_of_an_installation_starts_its_stable_command(tmp_path, monkeypatch):
    """Started by sandbox-ai's sandbox-ai-app from DATA/current: the entry survives updates (the versioned
    folder this copy runs from is removed two updates later)."""
    data = tmp_path / "data"
    version = data / "versions" / "1.2.3"
    (version / "projects" / "sandbox-ai-app").mkdir(parents=True)
    (version / "projects" / "sandbox-ai-app" / "sandbox_ai_app").symlink_to(backend.PACKAGE_DIR)
    (version / "sandbox-ai-app").write_text("#!/bin/sh\n")
    (data / "current").symlink_to("versions/1.2.3")
    launcher = str(data / "current" / "sandbox-ai-app")
    # The test's copy of the package is reached through a link: compare against what that link resolves to.
    monkeypatch.setattr(backend, "PACKAGE_DIR", backend.PACKAGE_DIR.resolve())
    monkeypatch.setenv("SANDBOX_AI_APP_LAUNCHER", launcher)
    assert backend.autostart_command() == [launcher, "--hidden"]
    assert launcher in backend.autostart_paths() and str(version / "sandbox-ai-app") in backend.autostart_paths()
    # A launcher of another copy, a relative path or another name: the app's own python -I command.
    for other in (str(tmp_path / "elsewhere" / "sandbox-ai-app"), "current/sandbox-ai-app", str(version / "x")):
        monkeypatch.setenv("SANDBOX_AI_APP_LAUNCHER", other)
        assert backend.autostart_command()[1:3] == ["-I", "-c"], other


def test_autostart_starts_this_copy_by_absolute_paths_never_from_path(tmp_path, monkeypatch):
    monkeypatch.delenv("SANDBOX_AI_APP_LAUNCHER", raising=False)
    monkeypatch.setattr(backend.shutil, "which", lambda *a: pytest.fail("looked up in $PATH"))
    command = backend.autostart_command()
    assert command[0] == os.path.abspath(sys.executable) and command[1:3] == ["-I", "-c"] and command[-1] == "--hidden"
    assert repr(str(backend.PACKAGE_DIR.parent)) in command[3]
    # From a folder holding a planted package of the same name: -I keeps the current directory off the path.
    (tmp_path / "sandbox_ai_app").mkdir()
    (tmp_path / "sandbox_ai_app" / "__init__.py").write_text("raise SystemExit('planted package imported')\n")
    probe = command[3].replace("from sandbox_ai_app.main import main; sys.exit(main())",
                               "import sandbox_ai_app.backend as b; print(b.__file__)")
    runs = subprocess.run([command[0], "-I", "-c", probe], capture_output=True, text=True, cwd=tmp_path, timeout=60,
                          env={**os.environ, "PYTHONPATH": str(tmp_path)})
    assert runs.stdout.strip() == str(backend.PACKAGE_DIR / "backend.py"), runs.stderr
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    with pytest.raises(backend.InsideSandbox):
        backend.set_autostart(True, forbidden=[str(backend.PACKAGE_DIR.parent)])
    assert not backend.autostart_enabled(), "nothing written for a program a sandbox could change"
    installation = tmp_path / "sandbox-ai"
    installation.mkdir()
    with pytest.raises(backend.InsideSandbox):  # the sandbox-ai the app loads in-process counts too
        backend.set_autostart(True, forbidden=[str(tmp_path)], also=[str(installation)])
    assert not backend.autostart_enabled()
    with pytest.raises(ValueError):
        backend.set_autostart(True, ["/opt/app\nExec=evil", "--hidden"])
    assert not backend.autostart_enabled()
    assert backend.set_autostart(True, forbidden=[str(tmp_path / "workspace")]).is_file()


def test_the_start_check_needs_no_module_and_knows_what_sandboxes_wrote(tmp_path, monkeypatch):
    state, runtime = tmp_path / "state", tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    s = backend.SandboxAI()
    assert s.startup_risk() is None and s._modules == {}, "decided without loading sandbox-ai's modules"
    (state / "sandbox-ai").mkdir(parents=True)
    (state / "sandbox-ai" / "exposed.json").write_text(json.dumps([str(s.directory), "relative"]))
    risk = s.startup_risk()
    assert isinstance(risk, backend.InsideSandbox) and risk.path == str(s.directory) and s._modules == {}
    (state / "sandbox-ai" / "exposed.json").write_text("[]")
    run = runtime / "sandbox-ai" / "runs" / "claude-abc"
    run.mkdir(parents=True)
    (run / "run.json").write_text(json.dumps({"workspace": str(backend.PACKAGE_DIR.parent),
                                              "session_dir": str(tmp_path / "s")}))
    session = state / "sandbox-ai" / "sessions" / "work"
    session.mkdir(parents=True)
    (session / "session.json").write_text(json.dumps({"workspace": str(tmp_path / "w")}))
    trees = backend.preload_trees()
    assert {str(backend.PACKAGE_DIR.parent.resolve()), str(tmp_path / "s" / "home"), str(tmp_path / "w"),
            str(session / "home")} <= set(trees)
    assert isinstance(s.startup_risk(), backend.InsideSandbox), "the app itself lies in a running sandbox's workspace"


def test_autostart_is_written_on_request_and_removed(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    assert not backend.autostart_enabled()
    path = backend.set_autostart(True, ["/opt/my apps/sandbox-ai-app", "--hidden"])
    text = path.read_text()
    assert 'Exec="/opt/my apps/sandbox-ai-app" --hidden\n' in text and "Type=Application" in text
    assert path == tmp_path / "config" / "autostart" / "sandbox-ai-app.desktop" and backend.autostart_enabled()
    victim = tmp_path / "victim"
    victim.write_text("keep")
    path.unlink()
    path.symlink_to(victim)
    backend.set_autostart(True, ["x"])
    assert victim.read_text() == "keep" and not path.is_symlink(), "a link there is replaced, not written through"
    backend.set_autostart(False)
    assert not path.exists() and not backend.autostart_enabled()
    backend.set_autostart(False)  # nothing to remove: fine


def test_with_sandbox_ai_home_set_the_command_on_path_is_never_looked_up(tmp_path, monkeypatch):
    monkeypatch.setenv("SANDBOX_AI_HOME", os.environ.get("SANDBOX_AI_HOME") or str(backend.locate()))
    found = backend.locate(which=lambda name: pytest.fail("looked up on PATH"),
                           run=lambda *a, **k: pytest.fail("ran something"))
    assert (found / "agent_sandbox.py").is_file()
    monkeypatch.setenv("SANDBOX_AI_HOME", str(tmp_path))
    with pytest.raises(backend.Unavailable, match="SANDBOX_AI_HOME="):
        backend.locate(which=lambda name: pytest.fail("looked up on PATH"))


def test_a_sandbox_ai_command_in_a_sandbox_folder_is_never_run(tmp_path, monkeypatch):
    monkeypatch.delenv("SANDBOX_AI_HOME", raising=False)
    workspace = tmp_path / "workspace"
    (workspace / "bin").mkdir(parents=True)
    command = workspace / "bin" / "sandbox-ai"
    command.write_text(f"#!{sys.executable}\nprint('planted')\n")
    ran = []
    with pytest.raises(backend.UnsafeInstallation, match="was not run"):
        backend.locate(which=lambda name: str(command), run=lambda *a, **k: ran.append(a), forbidden=[str(workspace)])
    assert ran == [], "neither the command nor its interpreter ran"
    # Its interpreter counts too: a launcher outside whose shebang points into a sandbox folder.
    outside = tmp_path / "bin" / "sandbox-ai"
    outside.parent.mkdir()
    (workspace / "python3").symlink_to(sys.executable)
    outside.write_text(f"#!{workspace / 'python3'}\n")
    with pytest.raises(backend.UnsafeInstallation):
        backend.locate(which=lambda name: str(outside), run=lambda *a, **k: ran.append(a), forbidden=[str(workspace)])
    assert ran == []
    # Elsewhere it is asked for its package as before.
    outside.write_text(f"#!{sys.executable}\n")
    try:
        backend.locate(which=lambda name: str(outside),
                       run=lambda *a, **k: ran.append(a) or subprocess.CompletedProcess(a, 0, "", ""), forbidden=[])
    except backend.Unavailable as exc:
        assert not isinstance(exc, backend.UnsafeInstallation)
    assert len(ran) == 1


def test_devices_and_limits_of_a_running_sandbox_are_commands(monkeypatch, tmp_path):
    calls = []
    s = backend.SandboxAI(runner=lambda argv, **k: calls.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""))
    assert s.add_device("claude-abc", "/dev/video0")[0] == 0 and calls[-1][-4:] == ["devices", "add", "claude-abc",
                                                                                     "/dev/video0"]
    assert s.remove_device("claude-abc", "/dev/video0")[0] == 0 and calls[-1][-3] == "remove"
    assert s.set_limits("claude-abc", "4g", "", "none")[0] == 0
    assert calls[-1][-6:] == ["limits", "claude-abc", "--memory", "4g", "--pids", "none"]
    count = len(calls)
    for bad in (s.add_device("--evil", "/dev/x"), s.add_device("run", "/etc/passwd"), s.set_limits("run"),
                s.set_limits("run", memory="4g; rm"), s.set_limits("-x", memory="1g")):
        assert bad[0] == 2
    assert len(calls) == count, "nothing invalid reaches sandbox-ai"
    (tmp_path / "video0").write_text("")
    os.mkfifo(tmp_path / "mem")
    candidates = s.device_candidates(tmp_path)
    assert all(path.startswith(str(tmp_path)) for path, _, _ in candidates)


def test_workspaces_of_a_running_sandbox_are_commands():
    calls = []
    s = backend.SandboxAI(runner=lambda argv, **k: calls.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""))
    assert s.add_workspace("claude-abc", "/home/me/docs")[0] == 0
    assert calls[-1][-4:] == ["workspace", "add", "claude-abc", "/home/me/docs"]
    assert s.add_workspace("claude-abc", "/home/me/docs", True)[0] == 0 and calls[-1][-1] == "--ro"
    assert s.remove_workspace("claude-abc", "/workspaces/docs")[0] == 0
    assert calls[-1][-4:] == ["workspace", "remove", "claude-abc", "/workspaces/docs"]
    assert s.remove_workspace("claude-abc", "docs", True)[0] == 0 and calls[-1][-2:] == ["docs", "--force"]
    count = len(calls)
    for bad in (s.add_workspace("--evil", "/x"), s.add_workspace("run", "--ro"), s.add_workspace("run", "relative"),
                s.add_workspace("run", "/a\nb"), s.remove_workspace("run", "--force"), s.remove_workspace("-r", "x")):
        assert bad[0] == 2
    assert len(calls) == count, "nothing invalid reaches sandbox-ai"


def test_device_candidates_mark_what_the_launcher_refuses(sai):
    found = {path: (why, note) for path, why, note in sai.device_candidates()}
    if "/dev/mem" in found:
        assert found["/dev/mem"][0], "raw memory is refused"
    if "/dev/null" in found:
        assert found["/dev/null"][0]
    if hasattr(sai.module("devices") if (sai.directory / "devices.py").is_file() else object(), "checked_device"):
        with pytest.raises(ValueError):
            sai.device_check("/dev/mem")


def test_settings_have_typed_defaults_and_the_checkout_override(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert backend.setting("approvals_interval") == 1000 and backend.setting("tray") is True
    backend.write_private_json(backend.settings_file(), {"approvals_interval": "fast", "tray": "yes",
                                                         "board_grid": 10, "sandbox_ai_home": "/opt/sandbox-ai"})
    assert backend.setting("approvals_interval") == 1000 and backend.setting("tray") is True, "damaged: the default"
    assert backend.setting("board_grid") == 10
    monkeypatch.delenv("SANDBOX_AI_HOME", raising=False)
    assert backend.sandbox_ai_home_override() == "/opt/sandbox-ai"
    monkeypatch.setenv("SANDBOX_AI_HOME", "/elsewhere")
    assert backend.sandbox_ai_home_override() is None, "$SANDBOX_AI_HOME wins"


# -- other hosts (sandbox-ai hosts): their questions, keys, limits, HOME reviews, live changes --


def json_runner(calls, answers):
    """A runner for SandboxAI that records the argv and answers by the sandbox-ai command (the words after --)."""
    def run(argv, **kwargs):
        calls.append(argv)
        words = argv[argv.index("--") + 1:] if "--" in argv else argv[3:]
        status, out, err = answers.get(words[0], (0, "", ""))
        return subprocess.CompletedProcess(argv, status, out, err)
    return run


def test_the_open_questions_of_another_host_are_checked_and_count_down():
    items = [{"id": "1700000000-1-1", "run": "claude-abc", "sandbox": "claude-abc (~/p)", "kind": "connect",
              "time": "2026-11-08T10:00:00Z", "question": "connect to 192.0.2.7:443 \x1b[31m", "timeout": 60,
              "seconds_left": 42, "max_scope": "run"},
             {"id": "1700000000-1-2", "run": "claude-abc", "kind": "browser", "url": "https://login.example/x",
              "question": "open https://login.example/x in your browser", "timeout": 60, "seconds_left": 50,
              "max_scope": "once"},
             {"id": "--evil", "run": "claude-abc"}, {"id": "1-2-3", "run": "-x"}, "junk",
             {"id": "1-2-4", "run": "r", "kind": "dns", "max_scope": "bogus", "seconds_left": True,
              "url": "https://not.a.browser.question", "extra": "dropped"}]
    calls = []
    s = backend.SandboxAI(runner=json_runner(calls, {"approvals": (0, json.dumps(items), "")}))
    found = s.host_questions("ct1")
    assert calls[-1][3:] == ["hosts", "run", "ct1", "--", "approvals", "list", "--json"]
    assert [q["id"] for q in found] == ["1700000000-1-1", "1700000000-1-2", "1-2-4"], "ids and runs that read as options go"
    assert all(isinstance(q, backend.RemoteQuestion) and q.host == "ct1" for q in found)
    assert "\x1b" not in found[0]["question"] and found[0]["max_scope"] == "run"
    assert found[2]["max_scope"] == "always" and found[2]["seconds_left"] is None and "extra" not in found[2]
    assert "url" not in found[2] and backend.question_url(found[1]) == "https://login.example/x"
    # The time left is the other host's (its clock never matters here): counted on from when it was read.
    question = backend.RemoteQuestion("ct1", {"id": "1-1-1", "run": "r", "kind": "dns", "timeout": 60,
                                              "seconds_left": 42}, read_at=1000.0)
    assert question.timing(now=1010.0) == (28, 32) and question.timing(now=5000.0)[1] == 0
    browser = backend.RemoteQuestion("ct1", {"kind": "browser", "timeout": 60, "seconds_left": 50}, read_at=1000.0)
    assert browser.timing(now=1001.0) == (11, None), "a browser question waits until it is answered"
    for answer in ((0, "{}", ""), (0, "not json", ""), (255, "", "ssh: connect to host: timed out")):
        s = backend.SandboxAI(runner=json_runner([], {"approvals": answer}))
        with pytest.raises(ValueError):
            s.host_questions("ct1")


def test_a_slow_read_of_another_host_never_shows_more_time_than_there_is(monkeypatch):
    """Regression: the time left of another host's question was counted from when its SSH read ended; that host said
    seconds_left during the round trip, so a read of 20 s showed up to 20 s more than the question had."""
    clock = [1000.0]
    item = {"id": "1700000000-1-1", "run": "claude-abc", "kind": "dns", "timeout": 60, "seconds_left": 40}

    def run(argv, **kwargs):
        clock[0] += 20  # the read takes 20 s
        return subprocess.CompletedProcess(argv, 0, json.dumps([item]), "")
    monkeypatch.setattr(backend.time, "time", lambda: clock[0])
    s = backend.SandboxAI(runner=run)
    found = s.host_questions("ct1")
    assert found[0].read_at == 1000.0, "counted from the start of the read"
    assert found[0].timing(now=clock[0])[1] == 20


def test_what_another_host_refuses_and_its_null_deadlines_are_kept():
    """The CLI contract of `approvals list --json`: null times are no deadline (never expired); max_scope null with
    allow_refused: only a deny; a browser question without its url (url_refused) names where it leads (url_host);
    url_truncated is kept; a long URL is no longer cut."""
    long = "https://login.example/" + "a" * 5000
    found = backend.remote_questions("ct1", [
        {"id": "1-1-1", "run": "r", "kind": "browser", "timeout": None, "seconds_left": None, "url": long,
         "url_truncated": True, "max_scope": "once"},
        {"id": "1-1-2", "run": "r", "kind": "connect", "timeout": 60, "seconds_left": 30, "max_scope": None,
         "allow_refused": "the security mode paranoid approves nothing"},
        {"id": "1-1-3", "run": "r", "kind": "browser", "timeout": None, "seconds_left": None, "max_scope": "once",
         "url_refused": "a file: URL", "url_host": "intranet.example"},
        {"id": "1-1-4", "run": "r", "kind": "dns", "url_refused": "x", "url_host": "y", "url_truncated": True}])
    browser, refused, hidden, dns = found
    assert browser.timing(now=browser.read_at + 3600)[1] is None, "no deadline, never expired"
    assert browser["url"] == long and browser["url_truncated"] is True
    assert refused["max_scope"] is None and "paranoid" in refused["allow_refused"]
    assert "url" not in hidden and hidden["url_refused"] == "a file: URL" and hidden["url_host"] == "intranet.example"
    assert not {"url_refused", "url_host", "url_truncated"} & set(dns), "only a browser question has a URL"
    assert backend.RemoteQuestion("ct1", {"id": "1-1-5", "run": "r", "max_scope": None})["allow_refused"]


def test_the_remembered_answers_of_another_host_and_what_gc_and_history_say():
    calls = []
    remembered = {"/srv/app": {"hosts": ["pypi.org", 7], "tcp": ["192.0.2.5:5432"], "blocked_hosts": ["evil.example"],
                               "blocked_tcp": [], "details": {}}, "/bad": "x"}
    s = backend.SandboxAI(runner=json_runner(calls, {"approvals": (0, json.dumps(remembered), ""),
                                                     "gc": (0, json.dumps({"removed": [], "dry_run": True}), ""),
                                                     "list": (0, json.dumps([{"event": "start"}, "junk"]), "")}))
    assert s.host_remembered("ct1") == [("/srv/app", "pypi.org", "host"), ("/srv/app", "192.0.2.5:5432", "tcp"),
                                         ("/srv/app", "evil.example", "blocked-host")]
    assert calls[-1][3:] == ["hosts", "run", "ct1", "--", "approvals", "remembered", "--json"]
    assert s.forget_remembered("/srv/app", "-x", host="ct1")[0] == 2
    assert s.collect("ct1", True) == {"removed": [], "dry_run": True}
    assert calls[-1][3:] == ["hosts", "run", "ct1", "--", "gc", "--dry-run", "--json"]
    assert s.start_history("web", "ct1") == [{"event": "start"}]
    assert calls[-1][3:] == ["hosts", "run", "ct1", "--", "list", "--history", "web", "--json"]
    with pytest.raises(ValueError):
        s.start_history("--all", "ct1")


def test_the_keys_of_other_hosts_starts_never_meet_this_machines():
    """A host may be called "session" or "run": its starts' keys live in a namespace of their own."""
    listing = [{"name": "web", "kind": "session", "state": "running"}, {"name": "--evil"}]
    s = backend.SandboxAI(runner=json_runner([], {"list": (0, json.dumps(listing), "")}))
    for host in ("session", "run", "mcp", "pid", "ct1"):
        found = s.host_instances(host)
        assert [i.key for i in found] == [f"host:{host}:web"] and found[0].host == host
        assert backend.remote_key(host, "web") == f"host:{host}:web"
        assert not found[0].key.startswith(("session:", "run:", "mcp:", "pid:", "project:"))


def test_limits_shown_and_the_review_of_a_kept_home():
    calls = []
    review = {"name": "work", "id": "work", "state": "stopped", "pending": True,
              "review": {"since": "2026-11-07T10:00:00Z", "changes": [{"path": ".bashrc", "change": "changed"}],
                         "complete": True}}
    answers = {"limits": (0, json.dumps({"memory": 8 << 30, "memory_high": None, "cpus": 1.5, "pids": True}), ""),
               "resume": (0, json.dumps(review), "")}
    s = backend.SandboxAI(runner=json_runner(calls, answers))
    assert s.limits_shown("claude-abc") == {"memory": 8 << 30, "memory_high": None, "cpus": 1.5, "pids": None}
    assert calls[-1][3:] == ["limits", "claude-abc", "--show", "--json"]
    s.limits_shown("web", "ct1")
    assert calls[-1][3:] == ["hosts", "run", "ct1", "--", "limits", "web", "--show", "--json"]
    assert s.review_home("work") == review and calls[-1][3:] == ["resume", "work", "--review-home", "--json"]
    s.review_home("web", "ct1")
    assert calls[-1][3:] == ["hosts", "run", "ct1", "--", "resume", "web", "--review-home", "--json"]
    count = len(calls)
    for bad in (lambda: s.limits_shown("--all"), lambda: s.review_home("-x")):
        with pytest.raises(ValueError):
            bad()
    assert len(calls) == count, "nothing invalid reaches sandbox-ai"
    s = backend.SandboxAI(runner=json_runner([], {"resume": (1, "", "sandbox-ai: claude-1 keeps no HOME: there is "
                                                                    "nothing to review\n")}))
    with pytest.raises(ValueError, match="keeps no HOME"):
        s.review_home("claude-1")
    s = backend.SandboxAI(runner=json_runner([], {"limits": (2, "", "unrecognized arguments: --show")}))
    with pytest.raises(ValueError, match="unrecognized"):
        s.limits_shown("claude-abc")


def test_devices_workspaces_and_limits_of_a_sandbox_on_another_host_are_its_commands():
    calls = []
    s = backend.SandboxAI(runner=json_runner(calls, {}))
    tail = ["hosts", "run", "ct1", "--"]
    assert s.add_device("web", "/dev/video0", host="ct1")[0] == 0
    assert calls[-1][3:] == tail + ["devices", "add", "web", "/dev/video0"]
    assert s.remove_device("web", "/dev/video0", host="ct1")[0] == 0 and calls[-1][3:][4:6] == ["devices", "remove"]
    assert s.add_workspace("web", "/srv/data", True, host="ct1")[0] == 0
    assert calls[-1][3:] == tail + ["workspace", "add", "web", "/srv/data", "--ro"]
    assert s.remove_workspace("web", "data", True, host="ct1")[0] == 0
    assert calls[-1][3:] == tail + ["workspace", "remove", "web", "data", "--force"]
    assert s.set_limits("web", "4g", "", "none", host="ct1")[0] == 0
    assert calls[-1][3:] == tail + ["limits", "web", "--memory", "4g", "--pids", "none"]
    count = len(calls)
    for bad in (s.add_device("web", "/dev/../etc/passwd", host="ct1"), s.add_device("web", "/dev/x\n", host="ct1"),
                s.add_workspace("web", "relative", host="ct1"), s.set_limits("--x", "1g", host="ct1")):
        assert bad[0] == 2
    assert len(calls) == count, "nothing invalid reaches sandbox-ai"


# -- review round 3: one confirmation policy, gc as shown, other hosts' secrets, lists and history, ids --


def web_constants():
    """NEEDS_CONFIRM, MODE_ORDER, TELEMETRY_ORDER, DEFAULT_MODE and DEFAULT_TELEMETRY of sandbox-ai's webui.py, read
    from its source (never imported: the web interface is a server)."""
    import ast
    path = backend.SandboxAI().directory / "webui.py"
    if not path.exists():
        pytest.skip("this sandbox-ai has no web interface")
    found = {}
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                names = [target] if isinstance(target, ast.Name) else list(getattr(target, "elts", []))
                values = [node.value] if isinstance(target, ast.Name) else list(getattr(node.value, "elts", []))
                for name, value in zip(names, values):
                    if isinstance(name, ast.Name):
                        try:
                            found[name.id] = ast.literal_eval(value)
                        except ValueError:
                            pass
    return found


def test_the_apps_confirmation_policy_is_the_web_interfaces():
    """One table: what the web interface refuses without {"confirm": true}, the app asks first, for the same
    reasons, with the same orders of protection; the app's own (APP_NEEDS_CONFIRM) are what no web route does."""
    web = web_constants()
    assert set(backend.NEEDS_CONFIRM) == set(web["NEEDS_CONFIRM"]), "the same actions as webui.py's NEEDS_CONFIRM"
    assert not set(backend.APP_NEEDS_CONFIRM) & set(web["NEEDS_CONFIRM"]), "the app's own are what no web route does"
    assert backend.MODE_ORDER == web["MODE_ORDER"] and backend.TELEMETRY_ORDER == web["TELEMETRY_ORDER"]
    assert (backend.DEFAULT_MODE, backend.DEFAULT_TELEMETRY) == (web["DEFAULT_MODE"], web["DEFAULT_TELEMETRY"])
    for action in (*backend.NEEDS_CONFIRM, *backend.APP_NEEDS_CONFIRM):
        assert backend.needs_confirm(action, {}) or action in ("limits-raise", "defaults-lower")
    with pytest.raises(KeyError):
        backend.needs_confirm("rename")  # not in the table: a bug in the caller, never a pass


def test_what_needs_a_confirmation_depends_on_what_it_does():
    raise_ = lambda current, **values: backend.needs_confirm("limits-raise", {"current": current, "values": values})  # noqa: E731
    now = {"memory": 8 << 30, "memory_high": None, "cpus": 2, "pids": None}
    assert raise_(now, memory="4g") is None and raise_(now, cpus="1.5", pids="4096") is None, "lower: no question"
    assert raise_(now, memory="16g") and raise_(now, cpus="3") and raise_(now, memory="none")
    assert raise_(None, memory="1g"), "the limits now unknown: asked"
    assert backend.raised_limits(now, {"memory": "9000m", "cpus": "", "pids": "100"}) == ["memory"]
    lower = lambda old, new, default=None: backend.needs_confirm("defaults-lower", {  # noqa: E731
        "old": old, "new": new, "canary_default": default})
    assert lower({"mode": "high"}, {"mode": "low"}) and not lower({"mode": "low"}, {"mode": "high"})
    assert lower({"telemetry": "strict"}, {}), "off (the default) is less private than strict"
    assert not lower({"telemetry": "on"}, {"telemetry": "off"}) and lower({}, {"telemetry": "on"})
    assert lower({}, {"canaries": ["credentials", "home"]}, ["credentials", "home", "workspace"]), "the host's own default"
    assert not lower({}, {"canaries": ["credentials", "home"]}, ["credentials", "home"])
    assert lower({"canaries": "all"}, {"canaries": "credentials,home"}) and lower({}, {"canaries": False})
    assert lower({}, {"keep_days": 30}) and lower({"keep_days": 30}, {"keep_days": 7})
    assert not lower({"keep_days": 7}, {"keep_days": 30}) and not lower({"keep_days": 7}, {})
    assert backend.needs_confirm("workspace-writable", {"read_only": True}) is None
    assert backend.needs_confirm("workspace-writable", {"read_only": False})
    assert backend.needs_confirm("secret-replace", {"exists": False}) is None
    assert backend.needs_confirm("secret-replace", {"exists": True})
    assert backend.needs_confirm("secret-replace", {"exists": None}), "not known whether it exists: asked"


def test_clean_up_removes_what_was_shown_with_the_same_days_or_nothing():
    calls = []
    plan = {"removed": [{"name": "old"}], "leftovers": [], "histories": ["gone"], "days": 30, "dry_run": True}
    s = backend.SandboxAI(runner=json_runner(calls, {"gc": (0, json.dumps(plan), "")}))
    s.collect_as_shown("ct1", dict(plan))
    assert [argv[argv.index("--") + 1:] for argv in calls] == [["gc", "--older-than=30", "--dry-run", "--json"],
                                                               ["gc", "--older-than=30", "--json"]]
    calls.clear()
    with pytest.raises(backend.GcChanged):
        s.collect_as_shown("ct1", {**plan, "histories": []})  # a dry run now lists more than was shown
    assert len(calls) == 1 and "--dry-run" in calls[0], "nothing removed"
    with pytest.raises(ValueError):
        s.collect("local", False, days=-1)


def stdin_runner(calls, answers):
    """Like json_runner, recording (argv, standard input)."""
    def run(argv, **kwargs):
        calls.append((argv, kwargs.get("input")))
        words = argv[argv.index("--") + 1:] if "--" in argv else argv[3:]
        status, out, err = answers.get(tuple(words[:2]), answers.get(words[0], (0, "", "")))
        return subprocess.CompletedProcess(argv, status, out, err)
    return run


def test_another_hosts_secrets_are_listed_kept_on_standard_input_and_deleted_there():
    calls = []
    table = "NAME      STORE\nLLM_KEY   keyring\n--evil    file\nBAD NAME x\n"
    s = backend.SandboxAI(runner=stdin_runner(calls, {("secrets", "list"): (0, table, "")}))
    assert s.host_secrets("ct1") == ("keyring", [("LLM_KEY", "keyring")])
    assert s.secret_exists("LLM_KEY", "ct1") and not s.secret_exists("OTHER", "ct1")
    s.set_secret("LLM_KEY", "sk-secret-value", host="ct1")
    argv, given = calls[-1]
    assert argv[3:] == ["hosts", "run", "ct1", "--", "secrets", "set", "LLM_KEY"] and given == "sk-secret-value\n"
    assert "sk-secret-value" not in json.dumps([argv for argv, _ in calls]), "never an argument"
    s.delete_secret("LLM_KEY", host="ct1")
    assert calls[-1][0][3:] == ["hosts", "run", "ct1", "--", "secrets", "delete", "LLM_KEY"]
    s = backend.SandboxAI(runner=stdin_runner(calls, {("secrets", "list"): (0, "no secrets\n", "")}))
    assert s.host_secrets("ct1")[1] == []
    s = backend.SandboxAI(runner=stdin_runner(calls, {("secrets", "list"): (255, "", "ssh: timed out")}))
    with pytest.raises(ValueError):
        s.secret_exists("LLM_KEY", "ct1")


def test_what_was_added_to_a_start_on_another_host_and_its_approvals_history():
    calls = []
    devices = [{"id": "1", "value": "/dev/ttyUSB0", "source": "added", "state": "attached", "nodes": ["/dev/ttyUSB0"]},
               {"id": "2", "value": "/dev/ttyUSB1", "source": "added", "state": "removed", "nodes": []},
               {"id": "3", "value": "/dev/video0", "source": "start", "state": "attached", "nodes": []},
               {"id": "--x", "value": "/dev/x", "source": "added", "state": "attached"}]
    workspaces = [{"host": "/srv/data", "sandbox": "/workspaces/data", "mode": "ro", "source": "added",
                   "note": "in use\x1b[2J"}, "junk"]
    s = backend.SandboxAI(runner=stdin_runner(calls, {
        ("devices", "list"): (0, json.dumps(devices), ""), ("workspace", "list"): (0, json.dumps(workspaces), ""),
        ("approvals", "history"): (0, "TIME  SANDBOX  ANSWER\n2026-11-08  web  allow\x07\n", "")}))
    assert s.host_devices_added("ct1", "web") == [("1", "/dev/ttyUSB0 (/dev/ttyUSB0)")]
    assert calls[-1][0][3:] == ["hosts", "run", "ct1", "--", "devices", "list", "web", "--json"]
    found = s.host_workspaces("ct1", "web")
    assert found[0]["sandbox"] == "/workspaces/data" and "\x1b" not in found[0]["note"] and len(found) == 1
    assert s.remove_device("web", "1", host="ct1")[0] == 0, "a device by its id"
    assert calls[-1][0][3:] == ["hosts", "run", "ct1", "--", "devices", "remove", "web", "1"]
    assert s.remove_device("web", "-1", host="ct1")[0] == 2
    text = s.host_approvals_history("ct1")
    assert "allow\\x07" in text and calls[-1][0][3:] == ["hosts", "run", "ct1", "--", "approvals", "history"]
    with pytest.raises(ValueError):
        s.host_devices_added("ct1", "--all")


def test_a_start_of_another_host_is_keyed_by_its_id_and_keeps_its_backend():
    """Review round 3 (A9): a rename gave a start another key, so its row, its selection and its tab came loose."""
    listing = [{"name": "web-2", "id": "web", "kind": "session", "state": "running", "backend": "podman"},
               {"name": "old", "kind": "run", "state": "running"}]
    s = backend.SandboxAI(runner=json_runner([], {"list": (0, json.dumps(listing), "")}))
    found = s.host_instances("ct1")
    assert [(i.key, i.name, i.ident, i.backend) for i in found] == [
        ("host:ct1:web", "web-2", "web", "podman"), ("host:ct1:old", "old", "old", "")]


def test_a_kept_start_and_its_run_are_one_row(tmp_path, monkeypatch):
    """User report: a new start showed as two rows, a stopped one and a running one. A kept start (a task or a
    session) names its run by run.json's "start" (its start id, since 0.1.30) - not by "session" - so the app listed
    its start directory and its run apart. They are merged as sandbox-ai list merges them (starts.entries)."""
    assert backend.runs_of_starts({"claude-web-1a2b": {"start": "web-1a2b"}, "session-old-9": {},
                                   "codex-x": {"session": "agent-session-work"}, "plain": {}},
                                  {"web": "web-1a2b", "old": "old-9", "work": "work"}) == {
        "web": "claude-web-1a2b", "old": "session-old-9", "work": "codex-x"}
    s = backend.SandboxAI()
    runs = {"claude-web-1a2b": {"run": "claude-web-1a2b", "start": "web-1a2b", "agent": "claude", "workspace": "/w",
                                "pid": os.getpid(), "started": "2026-11-08T10:00:00Z", "backend": "podman"},
            "claude-plain-77": {"run": "claude-plain-77", "agent": "claude", "workspace": "/w", "pid": os.getpid(),
                                "started": "2026-11-08T11:00:00Z"}}
    monkeypatch.setattr(s, "runs", lambda: runs)
    monkeypatch.setattr(s, "sessions", lambda: [["web", "claude", "podman", "running", "/w", "2026-11-08T09:59:00Z"]])
    monkeypatch.setattr(s, "session_ids", lambda: {"web": "web-1a2b"})
    monkeypatch.setattr(s, "foreground_start", lambda name, ident=None: False)
    real_module = s.module
    monkeypatch.setattr(s, "module", lambda name: (_ for _ in ()).throw(ValueError("no MCP servers here"))
                        if name == "mcp" else real_module(name))
    (tmp_path / "proc").mkdir()
    found, _ = s.instances(proc=tmp_path / "proc")
    web = [i for i in found if i.run == "claude-web-1a2b" or i.name == "web"]
    assert [(i.kind, i.key, i.state, i.run) for i in web] == [("session", "session:web-1a2b", "running",
                                                               "claude-web-1a2b")], "one row, running"
    assert [i.key for i in found if i.kind == "run"] == ["run:claude-plain-77"], "a run without a start stays apart"


def test_the_recent_project_folders_are_merged_newest_first_each_once(tmp_path, monkeypatch):
    """The wizard's list of recent folders: the workspaces of kept and running starts and the folders opened in the
    app, each folder once with its latest use, only those that exist, newest first, at most ten."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    folders = []
    for index in range(12):
        folder = tmp_path / f"p{index}"
        folder.mkdir()
        folders.append(str(folder))
    link = tmp_path / "link"
    link.symlink_to(folders[0])
    merged = backend.merge_recent([(folders[1], 100), (str(link), 300), (folders[0], 200), (str(tmp_path / "gone"), 999),
                                   (folders[2], None), (None, 5), (folders[3], 50)])
    assert merged == [(folders[0], 300), (folders[1], 100), (folders[3], 50), (folders[2], None)]
    assert len(backend.merge_recent([(f, i) for i, f in enumerate(folders)])) == 10
    assert backend.merge_recent([(f, i) for i, f in enumerate(folders)])[0] == (folders[11], 11), "newest first"
    s = backend.SandboxAI()
    monkeypatch.setattr(s, "module", lambda name: type("Starts", (), {"entries": staticmethod(lambda *a, **k: [
        {"kind": "session", "workspace": folders[4], "last_used": "2030-01-01T00:00:00Z"},
        {"kind": "mcp", "workspace": folders[5], "started": "2031-01-01T00:00:00Z"},
        {"kind": "run", "workspace": folders[6], "started": "2020-01-01T00:00:00Z"}])})())
    s.remember_project(folders[7])
    found = [path for path, _ in s.recent_folders()]
    assert found[:3] == [folders[4], folders[7], folders[6]] and folders[5] not in found, "no MCP server's folder"
    assert backend.home_short(str(Path.home() / "work")) == "~/work" and backend.home_short("/srv") == "/srv"
