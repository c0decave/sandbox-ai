"""Performance round 2 (0.1.95): what a start, a session and the connection proxy no longer do twice - each test counts
what is done (Podman containers, openssl runs, polls), never wall-clock time, and compares with the result before."""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, as_name=None):
    spec = importlib.util.spec_from_file_location(as_name or f"perf2_{name}", ROOT / "sandbox" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


s = load("agent_sandbox")


# --- a start's names: one Podman container for all of them ------------------------------------------------------

def counting_lookup(fails=()):
    """A stand-in for one Podman lookup (resolve_pod_hosts): each call noted; a name in FAILS fails the whole call, as
    the lookup's script does."""
    calls = []

    def resolve(names):
        calls.append(list(names))
        bad = [name for name in names if name in fails]
        if bad:
            raise ValueError(f"Podman failed (exit status 1) before the sandbox started: {bad[0]}")
        return {name: [f"203.0.113.{len(name) % 200}"] for name in names}
    return resolve, calls


def test_the_names_of_a_start_are_looked_up_in_one_container():
    resolve, calls = counting_lookup()
    lookup = s.looked_up_together(resolve, ["api.anthropic.com", "claude.ai"], ["pypi.org"], ["llm.lan"])
    assert calls == [["api.anthropic.com", "claude.ai", "pypi.org", "llm.lan"]]
    assert lookup(["api.anthropic.com", "claude.ai"]) == resolve(["api.anthropic.com", "claude.ai"])
    assert lookup(["pypi.org"]) == resolve(["pypi.org"]) and lookup(["llm.lan"]) == resolve(["llm.lan"])
    assert lookup([]) == {}
    assert len(calls) == 4  # the three above were the comparisons' own; the lookups asked nothing more
    lookup(["other.example.org"])  # a name not asked together: asked as before
    assert calls[-1] == ["other.example.org"]


def test_one_group_alone_is_asked_as_before():
    resolve, calls = counting_lookup()
    assert s.looked_up_together(resolve, ["api.anthropic.com"], [], []) is resolve
    assert s.looked_up_together(resolve, [], ["pypi.org"], []) is resolve
    assert calls == []


def test_a_name_that_does_not_resolve_leaves_each_group_to_its_own_lookup():
    """The one lookup fails (a remembered name that no longer resolves): nothing of it is used, each group is asked as
    before - the allowed hosts' error, the remembered names left out one by one (remembered_pins)."""
    resolve, calls = counting_lookup(fails=("gone.example.org",))
    lookup = s.looked_up_together(resolve, ["api.anthropic.com"], ["pypi.org", "gone.example.org"])
    assert lookup is resolve and calls == [["api.anthropic.com", "pypi.org", "gone.example.org"]]
    a = s.parse_args(["claude"])
    pins, notes = s.remembered_pins(a, ["pypi.org", "gone.example.org"], lambda names: {
        name: ["93.184.215.14"] for name in resolve(names)})
    assert pins == {"pypi.org": ["93.184.215.14"]} and "gone.example.org" in notes[0]


def test_a_start_with_names_approved_always_runs_one_lookup(tmp_path, monkeypatch):
    """A start in a project with names approved always ran one lookup container for the allowed hosts and another
    for the approved names; now one, and the sandbox gets the same addresses."""
    work = tmp_path / "work"
    work.mkdir()
    config = tmp_path / "config"
    (config / "sandbox-ai").mkdir(parents=True)
    (config / "sandbox-ai" / "approved.json").write_text(json.dumps({str(work.resolve()): {"hosts": ["pypi.org"]}}))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setattr(s.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(s.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(s, "prepare_home", lambda *args: [])
    calls, seen = [], {}

    def resolve(a, names):
        calls.append(list(names))
        return {name: ["93.184.215.14"] for name in names}
    monkeypatch.setattr(s, "resolve_pod_hosts", resolve)

    def run(a, session, home):
        seen["hosts"] = (session / "hosts").read_text()
        seen["pinned"] = json.loads((session / "config.json").read_text())["pinned"]
        return 0
    monkeypatch.setattr(s, "run_pod", run)
    assert s.main(["claude", "--workspace", str(work), "--no-auth", "--no-tmux", "--no-keep"]) == 0
    assert len(calls) == 1 and "pypi.org" in calls[0] and "api.anthropic.com" in calls[0], calls
    assert "93.184.215.14 pypi.org" in seen["hosts"] and "93.184.215.14 api.anthropic.com" in seen["hosts"]
    assert "pypi.org" in seen["pinned"] and "api.anthropic.com" in seen["pinned"]



def mcp_start(tmp_path, monkeypatch, wait):
    """A start with the MCP server demo whose sandbox comes up when WAIT() returns (or raises), the lookups counted:
    (main's status or the exception, the order of what happened)."""
    import threading
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setattr(s.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(s.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(s, "prepare_home", lambda *args: [])
    order, begun = [], threading.Event()

    def resolve(a, names):
        order.append("lookup")
        begun.set()
        return {name: ["93.184.215.14"] for name in names}
    monkeypatch.setattr(s, "resolve_pod_hosts", resolve)
    mcp = s.load_mcp_module()
    monkeypatch.setattr(mcp, "begin_running", lambda name, a, vpn=None: lambda: wait(order, begun))
    monkeypatch.setattr(s, "load_mcp_module", lambda: mcp)
    monkeypatch.setattr(s, "run_pod", lambda a, session, home: order.append("pod") or 0)
    try:
        result = s.main(["claude", "--workspace", str(work), "--no-auth", "--no-tmux", "--no-keep", "--mcp", "demo"])
    except Exception as exc:  # noqa: BLE001 - the test looks at it
        result = exc
    return result, order


def test_a_starts_names_are_looked_up_while_its_mcp_servers_come_up(tmp_path, monkeypatch):
    """The lookup container began only once every MCP server's sandbox was up (seconds each): now it runs meanwhile.
    The server here comes up only once the lookup has begun (or after 5 s): before, it waited the 5 s."""
    def wait(order, begun):
        order.append(("server up, the lookup begun meanwhile", begun.wait(5)))
    result, order = mcp_start(tmp_path, monkeypatch, wait)
    assert result == 0 and order == ["lookup", ("server up, the lookup begun meanwhile", True), "pod"], order


def test_a_server_that_fails_is_said_once_the_lookup_is_done(tmp_path, monkeypatch):
    def wait(order, begun):
        begun.wait(5)
        raise ValueError("MCP server demo did not come up")
    result, order = mcp_start(tmp_path, monkeypatch, wait)
    assert isinstance(result, ValueError) and "did not come up" in str(result) and order == ["lookup"]


def test_a_start_loads_the_launcher_its_runtime_and_the_privacy_check_once(tmp_path, monkeypatch):
    """A kept start loaded privacy.py ten times (3 ms each) and agent_sandbox.py with its runtime twice more (sessions'
    and mcp.py's own copies, about 25 ms each); now each once."""
    import collections
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setattr(s.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(s.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(s, "prepare_home", lambda *args: [])
    monkeypatch.setattr(s, "resolve_pod_hosts", lambda a, names: {name: ["93.184.215.14"] for name in names})
    monkeypatch.setattr(s, "run_pod", lambda a, session, home: 0)
    monkeypatch.setattr(s, "_privacy_module", None)
    loaded, real = collections.Counter(), importlib.util.spec_from_file_location

    def spy(name, location=None, *args, **kwargs):
        loaded[Path(str(location)).name] += 1
        return real(name, location, *args, **kwargs)
    monkeypatch.setattr(importlib.util, "spec_from_file_location", spy)
    assert s.main(["claude", "--workspace", str(work), "--no-auth", "--no-tmux"]) == 0
    assert loaded["privacy.py"] == 1 and loaded["agent_sandbox.py"] == 0 and loaded["runtime.py"] == 0, loaded
    assert loaded["sessions.py"] >= 1  # (a kept start: sessions.begin ran)


# --- the connection proxy: one certificate per host, made once ---------------------------------------------------

rt = load("runtime", "perf2_runtime")


def test_connections_at_once_to_a_new_host_make_its_certificate_once(tmp_path, monkeypatch):
    """Eight connections at once to a host new to the run (a browser's, an agent's parallel requests) ran openssl
    eight times for the same certificate; now once, the others wait for it and get the same file."""
    import threading
    import time
    rt.create_ca(tmp_path / "run", tmp_path / "ca", bundles=())
    runs, real = [], rt.run_openssl

    def counted(args, san="DNS:sandbox-ai"):
        runs.append(san)
        time.sleep(0.2)  # (an openssl run takes a while: the others arrive meanwhile)
        return real(args, san)
    monkeypatch.setattr(rt, "run_openssl", counted)
    barrier, paths = threading.Barrier(8), []

    def connect():
        barrier.wait()
        paths.append(rt.server_certificate(tmp_path / "run", "api.example.org"))
    threads = [threading.Thread(target=connect) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert runs == ["DNS:api.example.org"] and len(paths) == 8 and len(set(paths)) == 1
    assert b"BEGIN CERTIFICATE" in paths[0].read_bytes()
    assert rt._CERTIFICATES_MADE == {}  # nothing kept once it is made
    rt.server_certificate(tmp_path / "run", "api.example.org")
    rt.server_certificate(tmp_path / "run", "other.example.org")
    assert runs == ["DNS:api.example.org", "DNS:other.example.org"]


def test_a_certificate_that_could_not_be_made_is_tried_again(tmp_path, monkeypatch):
    rt.create_ca(tmp_path / "run", tmp_path / "ca", bundles=())
    real, calls = rt.run_openssl, []

    def fails_once(args, san="DNS:sandbox-ai"):
        calls.append(san)
        if len(calls) == 1:
            raise RuntimeError("openssl req failed: out of memory")
        return real(args, san)
    monkeypatch.setattr(rt, "run_openssl", fails_once)
    with pytest.raises(RuntimeError):
        rt.server_certificate(tmp_path / "run", "api.example.org")
    assert rt._CERTIFICATES_MADE == {}
    assert rt.server_certificate(tmp_path / "run", "api.example.org").exists() and len(calls) == 2


# --- a session's start: its terminal noticed soon after it is up ---------------------------------------------------

def test_a_sessions_terminal_is_noticed_soon_after_it_is_up(tmp_path, monkeypatch):
    """wait_ready looked for the terminal relay's socket every 0.2 s: a session that was up waited 0.1 s on average
    (up to 0.2 s) more before its terminal opened. Now a look every 0.05 s (a stat); a READY() of its own (macOS: a
    podman exec each time) is asked as often as before."""
    from unittest.mock import Mock
    sessions = load("sessions")
    directory = tmp_path / "s"
    (directory / "tmux").mkdir(parents=True)
    (directory / "supervisor.log").write_text("")
    pauses = []

    def sleep(seconds):
        pauses.append(seconds)
        if len(pauses) == 3:
            (directory / "tmux" / "attach.sock").touch()  # the relay is up during the third pause
    # wait_ready's own SLEEP: time.sleep patched for the whole process also counted other threads' pauses (CI: 0.25)
    sessions.wait_ready(directory, Mock(poll=Mock(return_value=None)), sleep=sleep)
    assert pauses == [0.05] * 3  # noticed at most 0.05 s after it was there
    pauses.clear()
    looks = iter([False, False, True])
    sessions.wait_ready(directory, Mock(poll=Mock(return_value=None)), ready=lambda: next(looks), sleep=sleep)
    assert pauses == [0.2, 0.2]
