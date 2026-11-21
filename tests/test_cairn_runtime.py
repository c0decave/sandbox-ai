"""The runtime of Cairn's container (sandbox/cairn_runtime.py) and the browser that opens Cairn's board
(browser_runtime.start_page): the workers' shims, the start's project made once, the dispatcher paused after failed
tasks, and the browser started on the board once it answers.

The user: "sein Board öffnet sich automatisch im Browser-Tab, die Agents der Session sind seine Worker." """
import importlib.util
import io
import json
from pathlib import Path
import socket
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rt = load("cairn_runtime_tests", "sandbox/cairn_runtime.py")
browser_rt = load("browser_runtime_for_cairn_tests", "sandbox/browser_runtime.py")
FAILED = ("[2026-11-15 11:12:03] WARNING scheduler.loop task finished project=proj_001 task=bootstrap "
          "worker=sandbox-claude outcome=failed\n")


def test_the_workers_on_cairn_s_path_are_the_relay_s_shims(tmp_path):
    folder = rt.shims(tmp_path / "bin", python="/opt/cairn/venv/bin/python", relay=Path("/sandbox-cairn/cairn_relay.py"))
    for agent in ("claude", "codex"):
        text = (folder / agent).read_text()
        assert text == f'#!/bin/sh\nexec /opt/cairn/venv/bin/python -I /sandbox-cairn/cairn_relay.py worker {agent} "$@"\n'
        assert (folder / agent).stat().st_mode & 0o755 == 0o755
    assert not (folder / "pi").exists()
    env = rt.environment({"LANG": "C.UTF-8", "ANTHROPIC_API_KEY": "x", "PATH": "/x"})
    assert env["PATH"].startswith(f"{rt.BIN}:") and "ANTHROPIC_API_KEY" not in env and env["LANG"] == "C.UTF-8"


class Answers:
    """urlopen for Cairn's API: the projects there, what was posted."""

    def __init__(self, projects):
        self.projects, self.posted = projects, []

    def __call__(self, request, timeout):
        if request.get_method() == "POST":
            body = json.loads(request.data)
            self.posted.append(body)
            answer = {"project": {"id": "proj_002", "title": body["title"]}}
        else:
            answer = self.projects
        return io.BytesIO(json.dumps(answer).encode())


class Stream(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


def opener(answers):
    def call(request, timeout):
        found = answers(request, timeout)
        return Stream(found.getvalue())
    return call


def test_the_start_s_project_is_made_once():
    cfg = {"port": 8000, "project": {"title": "sandbox-ai: lab", "origin": "o", "goal": "g",
                                     "hints": [{"content": "h", "creator": "human"}]}}
    answers = Answers([])
    assert rt.make_project(cfg, opener=opener(answers)) == "proj_002"
    assert answers.posted == [{"title": "sandbox-ai: lab", "origin": "o", "goal": "g",
                               "hints": [{"content": "h", "creator": "human"}]}]
    kept = Answers([{"id": "proj_001", "title": "sandbox-ai: lab"}])  # a board kept with the session
    assert rt.make_project(cfg, opener=opener(kept)) == "proj_001" and kept.posted == []
    assert rt.make_project({"port": 8000, "project": None}, opener=opener(answers)) is None


def test_failed_tasks_pause_the_dispatcher_twice_as_long_each_time():
    now = [0.0]
    watch = rt.FailureWatch(clock=lambda: now[0], failures=3, window=600, pause=60, limit=900)
    assert not watch.line("[...] INFO scheduler.loop dispatched bootstrap project=proj_001\n")
    assert not watch.line(FAILED) and not watch.line(FAILED)
    assert watch.line(FAILED) and watch.tripped()
    assert watch.next_pause() == 60 and not watch.tripped()
    for _ in range(3):
        watch.line(FAILED)
    assert watch.next_pause() == 120
    for _ in range(2):
        watch.line(FAILED)
    now[0] = 700  # older failures leave the window
    assert not watch.line(FAILED)
    out = io.StringIO()
    rt.forward(io.StringIO(FAILED * 3), watch, out)
    assert out.getvalue() == FAILED * 3 and watch.tripped(), "its log still reaches podman logs"


class Child:
    def __init__(self, argv, ended=None):
        self.argv, self.returncode, self.stdout = argv, ended, io.StringIO("")
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated, self.returncode = True, -15

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


def test_main_serves_waits_for_the_relay_and_dispatches_with_sandbox_ai_s_configuration(tmp_path, monkeypatch):
    mount = tmp_path / "mount"
    mount.mkdir()
    cfg = {"port": 18999, "data": str(tmp_path / "data"), "workers": ["claude"],
           "socket": str(tmp_path / "relay.sock"), "project": None,
           "dispatch": {"server": "http://127.0.0.1:18999", "runtime": {"execution": "local"},
                        "local": {"workspace_root": str(tmp_path / "runs")}, "workers": []}}
    (mount / "config.json").write_text(json.dumps(cfg))
    monkeypatch.setattr(rt, "BIN", tmp_path / "bin")
    monkeypatch.setattr(rt, "HOME", tmp_path / "home")
    monkeypatch.setattr(rt, "DISPATCH", tmp_path / "dispatch.yaml")
    monkeypatch.setattr(rt.signal, "signal", lambda *a: None)
    monkeypatch.setattr(rt, "port_open", lambda port: True)
    looks = []
    monkeypatch.setattr(rt, "relay_ready", lambda path: looks.append(path) or len(looks) > 2)
    started = []

    def popen(argv, **kw):
        child = Child(argv)
        started.append((argv, kw, child))
        if "dispatch" in argv:
            server = started[0][2]
            server.returncode = 0  # the server ends after the dispatcher started: main ends too
        return child
    assert rt.main(mount=mount, popen=popen, sleep=lambda s: None) == 1
    serve, dispatch = started[0][0], started[1][0]
    assert serve[serve.index("serve") + 1:] == ["--host", "127.0.0.1", "--port", "18999", "--db-path",
                                                f"{tmp_path / 'data'}/cairn.db", "--no-access-log"]
    assert dispatch[-3:] == ["dispatch", "--config", str(tmp_path / "dispatch.yaml")] and len(looks) == 3
    assert json.loads((tmp_path / "dispatch.yaml").read_text()) == cfg["dispatch"]
    env = started[1][1]["env"]
    assert env["SANDBOX_AI_CAIRN_RELAY"] == cfg["socket"] and env["PATH"].startswith(f"{tmp_path / 'bin'}:")
    assert (tmp_path / "bin" / "claude").exists() and str(mount / "cairn_relay.py") in (tmp_path / "bin" / "claude").read_text()


def test_the_browser_starts_on_the_board_once_it_answers():
    """"sein Board öffnet sich automatisch im Browser-Tab"."""
    cfg = {"kind": "chromium", "screen": "1280x800x24", "start_url": "http://127.0.0.1:8000/"}
    assert browser_rt.chromium_command(cfg, "/p")[-1] == "http://127.0.0.1:8000/"
    assert browser_rt.firefox_command({**cfg, "kind": "firefox"}, "/p")[-1] == "http://127.0.0.1:8000/"
    for other in ("https://example.org/", "http://127.0.0.1:8000/../x", "file:///etc/passwd", None):
        assert browser_rt.chromium_command({**cfg, "start_url": other}, "/p")[-1] == "about:blank", other
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    assert browser_rt.wait_for_start({"start_url": f"http://127.0.0.1:{port}/"}, sleep=lambda s: None)
    server.close()
    clock = iter(range(0, 1000, 30))
    assert not browser_rt.wait_for_start({"start_url": f"http://127.0.0.1:{port}/"}, sleep=lambda s: None,
                                         clock=lambda: next(clock), wait=60)
    assert not browser_rt.wait_for_start({}, sleep=lambda s: None)


def test_the_runtimes_need_only_the_standard_library():
    for path in ("sandbox/cairn_runtime.py", "sandbox/cairn_relay.py", "sandbox/cairn.py"):
        done = subprocess.run([sys.executable, "-I", "-S", "-c", f"import runpy; runpy.run_path({str(ROOT / path)!r})"],
                              capture_output=True, text=True)
        assert done.returncode in (0, 2), (path, done.stderr)
