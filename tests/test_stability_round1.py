"""Stability round 1 (0.1.95): what a long or busy run met - each test failed before its fix."""
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import resource
import select
import socket
import threading

import pytest

from test_proxy import Handler, Proxy, https_request, pki, rt, serve  # noqa: F401 - pki is a fixture

ROOT = Path(__file__).resolve().parents[1]
HIGH_FD = 1100  # above FD_SETSIZE (1024): select.select() refuses such a descriptor with ValueError


@contextlib.contextmanager
def descriptors_above(number):
    """Hold enough open descriptors that every new one is numbered above NUMBER (the soft limit raised as far as
    the hard one allows, else the test is skipped): a busy proxy - a few hundred connections, four descriptors each
    (both sides and their duplicates for the kill switch) - gets there."""
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    wanted = number + 200
    if soft != resource.RLIM_INFINITY and soft < wanted:
        if hard != resource.RLIM_INFINITY and hard < wanted:
            pytest.skip(f"RLIMIT_NOFILE {hard} is too low to reach descriptor {number}")
        resource.setrlimit(resource.RLIMIT_NOFILE, (wanted, hard))
    held = []
    try:
        while True:
            fd = os.open(os.devnull, os.O_RDONLY | os.O_CLOEXEC)
            held.append(fd)
            if fd >= number:
                break
        yield
    finally:
        for fd in held:
            os.close(fd)
        if soft != resource.RLIM_INFINITY and soft < wanted:
            resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


def test_the_proxy_serves_connections_whose_descriptors_are_above_1024(tmp_path, pki):  # noqa: F811
    """A proxy with a few hundred open connections: HTTPS with keep-alive (two requests on one connection) and a
    plain TCP relay still work. Before, every select() on such a descriptor raised ValueError ("filedescriptor out of
    range in select()"): the TLS connection ended with an error, the second request of a kept-alive one found the
    connection closed, and a TCP relay ended at once."""
    upstream = serve(Handler, pki["server"])
    proxy = Proxy(tmp_path, pki, upstream.server_address[1])
    echo = socket.socket()
    echo.bind(("127.0.0.1", 0))
    echo.listen()

    def answer():
        connection, _ = echo.accept()
        with connection:
            connection.sendall(b"220 ready\r\n")
            connection.sendall(connection.recv(100).upper())
    try:
        with descriptors_above(HIGH_FD):
            request = b"GET /one HTTP/1.1\r\nHost: example.test\r\n\r\nGET /two HTTP/1.1\r\nHost: example.test\r\n" \
                      b"Connection: close\r\n\r\n"
            data = https_request(proxy.port, pki["ca"], request)
            assert data.count(b"HTTP/1.1 200") == 2, data[:300]
            assert b'"/two"' in data
            records = proxy.records(wait=5, until=lambda r: str(r.get("url", "")).endswith("/two") or r.get("event") == "error")
            assert not [r for r in records if r.get("event") == "error"], records

            (tmp_path / "tcp").mkdir()
            other = Proxy(tmp_path / "tcp", pki, echo.getsockname()[1])
            threading.Thread(target=answer, daemon=True).start()
            try:
                with socket.create_connection(("127.0.0.1", other.port)) as client:
                    client.settimeout(10)
                    got = b""
                    while b"\r\n" not in got:
                        got += client.recv(100)
                    client.sendall(b"quit")
                    assert client.recv(100) == b"QUIT"
                record = other.records(wait=5)[0]
                assert record["event"] == "tcp" and record["bytes_in"] == 15, record
            finally:
                other.close()
    finally:
        proxy.close()
        upstream.shutdown()
        echo.close()


def test_wait_ready_reports_like_select_for_any_descriptor():
    """rt.wait_ready: readable, writable, a closed peer counts as readable (recv then reads its end), a timeout."""
    left, right = socket.socketpair()
    try:
        with descriptors_above(HIGH_FD):
            a, b = socket.socketpair()
            try:
                assert a.fileno() > 1024
                assert rt.wait_ready([a], [], 0.05) == ([], [])
                assert rt.wait_ready([a], [a], 0.05) == ([], [a])
                b.sendall(b"x")
                assert rt.wait_ready([a, left], [], 1) == ([a], [])
                a.recv(1)
                b.close()
                assert rt.wait_ready([a], [], 1) == ([a], [])
                assert a.recv(1) == b""
            finally:
                a.close()
                b.close()
    finally:
        left.close()
        right.close()


def load(name):
    spec = importlib.util.spec_from_file_location(f"sandbox_{name}_stability1", ROOT / "sandbox" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_damaged_run_record_does_not_break_every_command_that_lists_the_runs(tmp_path, monkeypatch):
    """One run.json with a PID beyond any number (OverflowError in os.kill) or nested deeper than Python reads
    (RecursionError) broke sandbox-ai list, net list, approvals, mcp list, gc, usage, devices, workspace and rules -
    every command that looks at the runs - with a traceback, until the file was removed by hand. A PID of 0 or -1
    asked about a whole process group or every process instead of the run."""
    net = load("net")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    runs = net.own_runs_dir()
    own = net.launcher().process_start(os.getpid())
    damaged = {"huge-pid": json.dumps({"run": "huge-pid", "pid": 10 ** 30}),
               "deep": "[" * 100000 + "]" * 100000,
               "group-pid": json.dumps({"run": "group-pid", "pid": 0}),
               "every-pid": json.dumps({"run": "every-pid", "pid": -1}),
               "live": json.dumps({"run": "live", "pid": os.getpid(), "pid_start": own})}
    for name, text in damaged.items():
        (runs / name).mkdir(parents=True)
        (runs / name / "run.json").write_text(text)
    asked = []
    real_kill = os.kill
    monkeypatch.setattr(net.os, "kill", lambda pid, sig: asked.append(pid) or real_kill(pid, sig))
    found = net.running()
    assert "live" in found
    assert all(pid > 0 for pid in asked), asked  # never a process group nor every process
    assert net.alive(10 ** 30) and net.alive(0) and net.alive(-1)  # unknown, as "abc": never taken for ended
    for name in damaged:  # nothing removed on a guess
        assert (runs / name / "run.json").exists()


def test_damaged_lines_of_a_log_are_skipped_by_its_readers(tmp_path):
    """A line nested deeper than Python reads (RecursionError: about a thousand brackets) or a line that is JSON but
    no record: sandbox-ai safeguards ended with a traceback, and the proxy's summary at the end of a run
    (connection_summary) with an AttributeError."""
    safeguards = load("safeguards")
    deep = "[" * 5000 + "]" * 5000
    record = {"kind": sorted(safeguards.KINDS)[0], "time": "2026-11-03T10:00:00Z"}
    log = tmp_path / "safeguards.jsonl"
    log.write_text("\n".join([deep, json.dumps(record), "5", deep]) + "\n")
    assert safeguards.read(log) == [record]
    assert safeguards.read(log, tail=5) == [record]
    connections = tmp_path / "connections.jsonl"
    connections.write_text("\n".join([deep, "5", "[]", json.dumps({"event": "http"}), json.dumps({"event": "denied"})])
                           + "\n")
    launcher = load("agent_sandbox")
    assert launcher.connection_summary(connections)[0].startswith(
        "proxy: 1 HTTP request(s), 0 other connection(s), 1 refused")


@pytest.mark.skipif(not hasattr(select, "POLLRDHUP"), reason="POLLRDHUP is Linux's")
def test_a_client_gone_after_part_of_its_first_bytes_frees_its_thread_at_once(tmp_path, pki):  # noqa: F811
    """A program that sends part of a ClientHello (or of a request line) and goes away: the proxy waited the full 31 s
    for the rest that could never come, a thread held each time - a program opening and dropping connections in a
    loop (a scanner, a happy-eyeballs race) held hundreds of them. Its end is seen now, and the connection refused as
    incomplete at once."""
    import time
    upstream = serve(Handler, pki["server"])
    proxy = Proxy(tmp_path, pki, upstream.server_address[1])
    try:
        started = time.monotonic()
        before = threading.active_count()
        for _ in range(20):
            client = socket.create_connection(("127.0.0.1", proxy.port))
            client.sendall(b"\x16\x03\x01\x02\x00\x01")  # a record of 512 bytes that never comes
            client.close()
        while len(proxy.records(wait=0)) < 20 and time.monotonic() - started < 10:
            time.sleep(0.05)
        records = proxy.records(wait=0)
        assert len(records) == 20 and time.monotonic() - started < 10, records
        assert all(r["event"] == "denied" and "incomplete" in r["reason"] for r in records), records
        deadline = time.monotonic() + 5
        while threading.active_count() > before and time.monotonic() < deadline:
            time.sleep(0.05)
        assert threading.active_count() <= before
    finally:
        proxy.close()
        upstream.shutdown()


def test_two_answers_to_one_question_at_the_same_moment_do_not_fail(tmp_path, monkeypatch):
    """The app (its notification and its page) and `sandbox-ai approvals allow` can answer one question at the same
    moment: both wrote the same temporary file (.ID.answer), so one answer's rename found it gone (FileNotFoundError:
    a traceback) and the file the sandbox reads could hold the two writes run into each other."""
    approvals = load("approvals")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    directory = tmp_path / "runs" / "claude-1"
    (directory / "approvals").mkdir(parents=True)
    runs = {"claude-1": {"run": "claude-1", "directory": str(directory), "workspace": str(tmp_path / "app"),
                         "pid": os.getpid()}}
    (directory / "approvals" / "1-1-1.json").write_text(json.dumps(
        {"id": "1-1-1", "time": "now", "kind": "connect", "destination": "192.0.2.9:443", "address": "192.0.2.9",
         "port": 443}))
    together = threading.Barrier(8)
    found = approvals.questions

    def questions(*args, **kwargs):  # every answer has found the question open before any writes
        listed = found(*args, **kwargs)
        together.wait(5)
        return listed
    monkeypatch.setattr(approvals, "questions", questions)
    failed = []

    def answer(decision):
        try:
            approvals.answer("1-1-1", decision, runs=runs, out=lambda line: None, wait=0)
        except Exception as exc:  # noqa: BLE001
            failed.append(repr(exc))
    threads = [threading.Thread(target=answer, args=("allow" if i % 2 else "deny",)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert not failed, failed
    assert json.loads((directory / "approvals" / "1-1-1.answer").read_text())["decision"] in ("allow", "deny")
    assert sorted(p.name for p in (directory / "approvals").iterdir()) == ["1-1-1.answer", "1-1-1.json"]


def test_a_run_whose_launcher_is_gone_is_recorded_as_ended_once_by_commands_at_the_same_moment(tmp_path,
                                                                                              monkeypatch):
    """The app lists the runs every few seconds, often several commands at once, and the human runs sandbox-ai list
    beside it: each that found a run whose launcher was killed recorded its end (half the runs of a soak twice in
    their history: two ends of one run) before one of them removed its directory."""
    net = load("net")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "runtime").mkdir()
    runs = net.own_runs_dir()
    for index in range(12):
        directory = runs / f"claude-gone{index}"
        directory.mkdir(parents=True)
        (directory / "run.json").write_text(json.dumps({
            "run": f"claude-gone{index}", "agent": "claude", "backend": "bwrap", "workspace": str(tmp_path),
            "pid": 2 ** 22 + index, "name": f"gone{index}", "start": None,
            "pid_ns": os.readlink("/proc/self/ns/pid")}))
    starts = net.launcher().load_starts_module()
    real = starts.record
    together = threading.Barrier(6)

    def record(name, event, *args, **kwargs):  # every command has looked at the history before any writes
        if event == "exit":
            with contextlib.suppress(threading.BrokenBarrierError):
                together.wait(2)
        return real(name, event, *args, **kwargs)
    monkeypatch.setattr(starts, "record", record)
    threads = [threading.Thread(target=net.running) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    for index in range(12):
        ends = [e for e in starts.history(f"gone{index}") if e.get("event") == "exit"]
        assert len(ends) == 1, (index, ends)
    assert not list(runs.iterdir())
