"""sandbox-ai net against a real control socket (the supervisor's control_server, firewall mocked)."""
import importlib.util
import json
import os
import shutil
import tempfile
from pathlib import Path
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sandbox_net", ROOT / "sandbox/net.py")
net = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(net)
rt = net.launcher().RUNTIME


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """A registered run whose supervisor is a real control_server thread (in a short directory: the
    supervisor binds there, and socket paths are limited to 107 bytes)."""
    short = Path(tempfile.mkdtemp(prefix="net-", dir="/tmp"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(short))
    session = tmp_path / "session"
    session.mkdir()
    (session / "hosts").write_text("127.0.0.1 localhost\n192.0.2.1 api.openai.com\n")
    run = net.launcher().runs_dir() / "codex-1234abcd"
    run.mkdir(parents=True)
    (run / "run.json").write_text(json.dumps({"run": "codex-1234abcd", "agent": "codex", "pid": os.getpid(),
                                              "workspace": str(tmp_path / "app"), "session_dir": str(session),
                                              "started": "now", "session": None}))
    applied, stop = [], threading.Event()
    allow = tmp_path / "allow.json"
    thread = threading.Thread(target=rt.control_server, args=(run / "control.sock", {"loopback": []}, allow, stop,
                                                              applied.append), daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not (run / "control.sock").exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    yield {"run": run, "hosts": session / "hosts", "applied": applied, "allow": allow, "inode": (session / "hosts").stat().st_ino}
    stop.set()
    thread.join(5)
    shutil.rmtree(short, ignore_errors=True)


def fake_resolver(hosts):
    return {host: ["192.0.2.50"] for host in hosts}


def run_net(argv):
    lines = []
    status = net.main(argv, out=lines.append, resolver=fake_resolver)
    return status, lines


def test_list_and_show(sandbox, tmp_path):
    assert run_net(["list"]) == (0, [f"codex-1234abcd: codex in {tmp_path / 'app'} (since now)"])
    assert run_net(["show"]) == (0, ["allowed during this run: nothing"])


def test_allow_host_reaches_firewall_proxy_and_hosts_in_place(sandbox):
    status, lines = run_net(["allow", "--allow-host", "example.org", "--allow-tcp", "192.0.2.9:5432"])
    assert status == 0
    assert "192.0.2.50/32 (TCP/443)" in lines[0] and "192.0.2.9:5432 (TCP)" in lines[0]
    assert "start with: --allow-host example.org --allow-tcp 192.0.2.9:5432" in lines[1]
    assert "tcp dport 443 accept" in sandbox["applied"][-1]
    assert json.loads(sandbox["allow"].read_text())["https"] == ["192.0.2.50/32"]
    # Same file (the sandbox has it bound), the new name added, the old ones kept.
    assert sandbox["hosts"].stat().st_ino == sandbox["inode"]
    assert sandbox["hosts"].read_text().splitlines() == ["127.0.0.1 localhost", "192.0.2.1 api.openai.com",
                                                         "192.0.2.50 example.org"]
    status, lines = run_net(["revoke", "--allow-host", "example.org"])
    assert status == 0 and "192.0.2.50" not in lines[0]
    assert "example.org" not in sandbox["hosts"].read_text() and "api.openai.com" in sandbox["hosts"].read_text()


def test_refusals(sandbox, capsys):
    assert run_net(["allow"])[0] == 1
    assert "nothing to allow" in capsys.readouterr().err
    assert run_net(["allow", "--allow-host", "bad host"])[0] == 1
    assert run_net(["allow", "--allow", "10.0.0.0/8; flush ruleset"])[0] == 1
    assert run_net(["revoke", "--allow-host", "never.example.org"])[0] == 1
    assert "was not allowed during this run" in capsys.readouterr().err
    assert run_net(["show", "other-run"])[0] == 1
    assert sandbox["applied"] == []


def test_choose_and_stale_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    runs = net.launcher().runs_dir()
    for name, pid, workspace in (("a", os.getpid(), "/w/a"), ("b", os.getpid(), "/w/b"), ("dead", 2 ** 22 + 7, "/w/c")):
        (runs / name).mkdir(parents=True)
        (runs / name / "run.json").write_text(json.dumps({"run": name, "pid": pid, "workspace": workspace}))
    found = net.running()
    assert sorted(found) == ["a", "b"] and not (runs / "dead").exists()
    assert net.choose(found, None, cwd="/w/b")["run"] == "b"
    with pytest.raises(ValueError, match="several sandboxes"):
        net.choose(found, None, cwd="/elsewhere")
    with pytest.raises(ValueError, match="no running sandbox"):
        net.choose(found, "zzz")


def test_revoke_keeps_start_entries_and_names_sharing_an_address(sandbox, monkeypatch):
    # api.openai.com was pinned at the start; allowing and revoking it during the run leaves that entry.
    lines = []
    assert net.main(["allow", "-A", "api.openai.com"], out=lines.append, resolver=lambda hosts: {h: ["192.0.2.1"] for h in hosts}) == 0
    assert net.main(["revoke", "-A", "api.openai.com"], out=lines.append) == 0
    assert "192.0.2.1 api.openai.com" in sandbox["hosts"].read_text()
    # Two names on one address (a CDN): revoking one keeps the address for the other.
    shared = lambda hosts: {h: ["192.0.2.60"] for h in hosts}
    assert net.main(["allow", "-A", "a.example.org", "-A", "b.example.org"], out=lines.append, resolver=shared) == 0
    assert net.main(["revoke", "-A", "a.example.org"], out=lines.append) == 0
    assert json.loads(sandbox["allow"].read_text())["https"] == ["192.0.2.60/32"]
    assert "b.example.org" in sandbox["hosts"].read_text() and "a.example.org" not in sandbox["hosts"].read_text()
