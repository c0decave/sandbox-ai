"""sandbox-ai rules check|show|reload: the rulesets of a start, checked, shown with their source, and read anew by a
running sandbox's proxy (the start's own - policy, mode, publish, telemetry, --allow-http - kept)."""
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sandbox_rules_cmd_test", ROOT / "sandbox/rules_cmd.py")
rc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rc)
L = rc.launcher()
rt = L.RUNTIME

GITHUB = {"version": 1, "http": [{"id": "gh-read", "host": "api.github.com", "methods": ["GET"],
                                  "path": "/repos/**", "decision": "allow"}],
          "defaults": {"http": "deny"}}
POLICY = {"version": 1, "http": [{"id": "no-paste", "host": "pastebin.com", "methods": ["*"], "path": "/**",
                                  "decision": "deny"}]}


@pytest.fixture(autouse=True)
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setattr(L, "load_policy", lambda path=None: {})
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    return work


def write(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document) if not isinstance(document, str) else document)
    return path


def test_check_files_and_the_default_ones(tmp_path, config, monkeypatch):
    good, bad = write(tmp_path / "good.json", GITHUB), write(tmp_path / "bad.json", {"version": 1, "http": [{}]})
    lines = []
    assert rc.main(["check", str(good)], out=lines.append) == 0
    assert lines == [f"ok     --rules good.json ({good}): 1 HTTP, 0 MCP, 0 tool, 0 device rules; defaults: http deny"]
    lines.clear()
    assert rc.main(["check", str(good), str(bad), "--json"], out=lines.append) == 1
    result = json.loads("\n".join(lines))
    assert not result["ok"] and result["rulesets"][1]["ok"] is False and "required" in result["rulesets"][1]["error"]
    # Without files: yours, the policy's and the project file's (an untrusted one is said to be so).
    write(L.user_rules_file(), GITHUB)
    monkeypatch.setattr(L, "load_policy", lambda path=None: {"version": 1, "rules": POLICY})
    write(config / ".sandbox-ai.json", {"version": 1, "agent": "codex", "rules": {"version": 1, "mcp": [
        {"id": "no-delete", "server": "files", "tool": "delete*", "decision": "deny"}]}})
    lines.clear()
    assert rc.main(["check"], out=lines.append) == 0
    assert [line.split()[1] for line in lines] == ["rules.json", "policy", "project"]
    assert "1 MCP" in lines[2] and "not trusted" in lines[2]
    L.trust_config(config / ".sandbox-ai.json")
    lines.clear()
    assert rc.main(["check", "--project", str(config)], out=lines.append) == 0 and "not trusted" not in lines[2]
    write(config / ".sandbox-ai.json", {"version": 1, "agent": "codex", "rules": {"version": 2}})
    assert rc.main(["check"], out=lines.append) == 1


def test_show_names_each_ruleset_and_where_it_comes_from(tmp_path, config, monkeypatch):
    mine = write(tmp_path / "mine.json", GITHUB)
    write(L.user_rules_file(), POLICY)
    write(config / ".sandbox-ai.json", {"version": 1, "agent": "codex", "rules": {"version": 1, "http": [
        {"id": "docs", "host": "docs.example.org", "methods": ["GET"], "path": "/**", "decision": "allow"}]}})
    L.trust_config(config / ".sandbox-ai.json")
    lines = []
    assert rc.main(["show", "--json", "--", "--rules", str(mine), "--allow-http", "api.example.org GET /v1/**",
                    "--telemetry", "on"], out=lines.append) == 0
    shown = json.loads("\n".join(lines))
    assert shown["agent"] == "codex" and shown["project_config"] == str(config / ".sandbox-ai.json")
    assert [(s["name"], s["from"]) for s in shown["rulesets"]] == [
        ("rules.json", str(L.user_rules_file())), ("--rules mine.json", str(mine)),
        ("--allow-http", "the --allow-http options"), ("project", str(config / ".sandbox-ai.json"))]
    lines.clear()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert rc.main(["show", "--agent", "claude", "--project", str(elsewhere), "--", "--rules", str(mine)],
                   out=lines.append) == 0
    assert lines[0].startswith("rulesets of a start of claude here")
    assert any(line.startswith(f"--rules mine.json (from {mine}): 1 HTTP") for line in lines)
    assert any("gh-read: allow" in line for line in lines)
    # The same rulesets as the launcher's own plan of that start (no drift between the two).
    plan = subprocess.run([sys.executable, "-I", str(ROOT / "sandbox/agent_sandbox.py"), "codex", "--rules", str(mine),
                           "--telemetry", "on", "--dry-run"], capture_output=True, text=True, cwd=config,
                          env={**os.environ, "XDG_CONFIG_HOME": str(tmp_path / "cfg")}, timeout=120)
    assert plan.returncode == 0, plan.stderr
    _, sources = rc.start_rulesets("codex", ["--rules", str(mine), "--telemetry", "on"])
    assert [(s["name"], s["http"]) for s in json.loads(plan.stdout)["rulesets"]] == \
        [(s["name"], [r["id"] for r in s["http"]]) for s in sources]
    # A project file that changed since it was trusted: refused as a start would be.
    write(config / ".sandbox-ai.json", {"version": 1, "agent": "codex"})
    assert rc.main(["show"]) == 1


# --- the supervisor's side: RunRules and the control socket ----------------------------------------------------

def started_rules(*names):
    """proxy.json's "rules" of a start: the policy's, a mode's, the human's (rules.json) and the telemetry's."""
    documents = {"policy": POLICY, "mode high": {"version": 1, "defaults": {"http": "ask"}},
                 "rules.json": GITHUB, "telemetry": {"version": 1, "http": [
                     {"id": "t", "host": "chatgpt.com", "methods": ["*"], "path": "/analytics/**", "decision": "deny"}]}}
    return rt.http_sources([{"name": name, **rt.Ruleset.validated(documents[name], name)} for name in names])


def test_a_reload_keeps_the_starts_own_rulesets_and_refuses_what_cannot_hold(tmp_path, monkeypatch):
    path = tmp_path / "rules.json"
    rules = rt.RunRules({"rules": started_rules("policy", "mode high", "rules.json", "telemetry"),
                         "no_inspect": ["*.internal.example"]}, path, forbid=["--rules"])
    new = {"name": "rules.json", **rt.Ruleset.validated({"version": 1, "http": [
        {"id": "pypi", "host": "pypi.org", "methods": ["GET"], "path": "/**", "decision": "allow"}],
        "mcp": [{"id": "m", "server": "files", "tool": "*", "decision": "ask"}]}, "x")}
    answer = rules.reload([new])
    assert [s["name"] for s in json.loads(path.read_text())["rulesets"]] == ["policy", "mode high", "rules.json",
                                                                             "telemetry"]
    assert json.loads(path.read_text())["rulesets"][2]["http"][0]["id"] == "pypi"
    assert [(s["name"], s["kept"]) for s in answer["rules"]] == [("policy", True), ("mode high", True),
                                                                  ("rules.json", False), ("telemetry", True)]
    assert answer["not_reloaded"] == ["rules.json"] and answer["reloaded"]  # its MCP rule: at the next start
    before = path.read_text()
    for sources, match in (([{**new, "name": "policy"}], "start's own"), ([{**new, "name": "mode lax"}], "start's own"),
                           ([{**new, "name": "--rules x.json"}], "administrator's policy"),
                           ([{"name": "rules.json", "version": 1, "http": [{"id": "x"}]}], "required"),
                           ([new, new], "name of its own"), ("nope", "a list"),
                           ([{"name": "rules.json", **rt.Ruleset.validated({"version": 1, "http": [
                               {"id": "i", "host": "api.internal.example", "methods": ["GET"], "path": "/**",
                                "decision": "allow"}]}, "x")}], "--no-inspect")):
        with pytest.raises(ValueError, match=match):
            rules.reload(sources)
    assert path.read_text() == before  # nothing written by a refusal
    # The policy as it is now joins the start's when it changed; the same is not doubled.
    same = {"name": "policy (now)", **rt.Ruleset.validated(POLICY, "p")}
    assert [s["name"] for s in rules.reload([same])["rules"]] == ["policy", "mode high", "telemetry"]
    with pytest.raises(ValueError, match="without the connection proxy"):
        rt.RunRules({"rules": []}, None).reload([])


def test_the_proxy_holds_the_reloaded_rules(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, "RULES_CHECK_SECONDS", 0)
    path = tmp_path / "reloaded.json"
    fd = os.open(tmp_path / "log.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    server = rt.ProxyServer({"body_limit": 65536, "rules": started_rules("policy", "rules.json"),
                             "rules_file": str(path), "http_rules": [
                                 {"host": "api.example.org", "methods": ["GET"], "path": "/v1/**"}]}, fd,
                            run_dir=tmp_path)
    assert [s["name"] for s in server.rules.sources] == ["policy", "rules.json", "--allow-http"]
    rt.RunRules({"rules": started_rules("policy", "rules.json")}, path).reload([])
    assert [s["name"] for s in server.rules.sources] == ["policy", "--allow-http"]  # --allow-http stays
    path.write_text("[{\"name\": \"broken\"}]")  # not a ruleset: the rules stay as they were
    time.sleep(0.01)
    os.utime(path)
    assert [s["name"] for s in server.rules.sources] == ["policy", "--allow-http"]
    os.close(fd)


def serve(tmp_path, rules):
    """A run of this process (alive for net.running()) whose control socket serves RULES."""
    directory = L.runs_dir() / "claude-1"
    directory.mkdir(parents=True)
    stop = threading.Event()
    network = rt.RuntimeNetwork({}, None, lambda script: None)
    thread = threading.Thread(target=rt.control_server, args=(directory / "control.sock", network, stop),
                              kwargs={"rules": rules}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not (directory / "control.sock").exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    return directory, stop


def test_reload_reads_the_humans_rulesets_again_and_the_run_keeps_its_own(tmp_path, config, monkeypatch):
    mine = write(tmp_path / "mine.json", GITHUB)
    write(config / ".sandbox-ai.json", {"version": 1, "agent": "claude", "rules": POLICY})
    L.trust_config(config / ".sandbox-ai.json")
    target = tmp_path / "proxy-rules.json"
    directory, stop = serve(tmp_path, rt.RunRules({"rules": started_rules("policy", "telemetry")}, target))
    record = {"run": "claude-1", "agent": "claude", "pid": os.getpid(), "workspace": str(config), "name": "web",
              "started": "2026-11-10T08:00:00Z", "proxy": True,
              "rules_sources": {"files": [str(mine)], "project_config": str(config / ".sandbox-ai.json")}}
    (directory / "run.json").write_text(json.dumps(record))
    try:
        lines = []
        assert rc.main(["reload", "web"], out=lines.append) == 0
        assert [s["name"] for s in json.loads(target.read_text())["rulesets"]] == ["policy", "--rules mine.json",
                                                                                   "project", "telemetry"]
        assert lines[0] == "web: reloaded --rules mine.json, project" and "kept from its start" in lines[2]
        lines.clear()
        assert rc.main(["show", "web", "--json"], out=lines.append) == 0
        assert [s["name"] for s in json.loads("\n".join(lines))["rules"]][1] == "--rules mine.json"
        # A broken file, or a project file changed since it was trusted: refused, the rules stay.
        before = target.read_text()
        mine.write_text("{")
        assert rc.main(["reload", "web"]) == 1 and target.read_text() == before
        mine.write_text(json.dumps(GITHUB))
        write(config / ".sandbox-ai.json", {"version": 1, "agent": "claude", "rules": {"version": 1}})
        assert rc.main(["reload", "web"]) == 1 and target.read_text() == before
        # Started by an older version (no record of its rulesets), or without the proxy: said so.
        (directory / "run.json").write_text(json.dumps({**record, "rules_sources": None}))
        with pytest.raises(ValueError, match="older sandbox-ai"):
            rc.reload("web", print, False)
        (directory / "run.json").write_text(json.dumps({**record, "proxy": False}))
        with pytest.raises(ValueError, match="--no-proxy"):
            rc.reload("web", print, False)
    finally:
        stop.set()


def test_sandbox_ai_dispatches_rules():
    result = subprocess.run([str(ROOT / "sandbox-ai"), "rules", "--help"], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0 and "check" in result.stdout and "reload" in result.stdout


def test_reloaded_rules_hold_for_the_bare_address_of_a_name_allowed_during_the_run(tmp_path):
    """A run that started without HTTP rules, a rule reloaded later for a host the run reaches (allowed during the
    run): a request to that host's bare address meets the rule as one by name does."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("rt_pins", ROOT / "sandbox/runtime.py")
    rt = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rt)
    path = tmp_path / "rules.json"
    rule = {"name": "rules.json", **rt.Ruleset.validated({"version": 1, "http": [
        {"id": "no-admin", "host": "internal.example.com", "decision": "deny", "methods": ["POST"],
         "path": "/admin*"}]}, "x")}
    runs = rt.RunRules({"allow": {}}, path)
    runs.reload([rule], {"Internal.Example.com": ["10.0.0.5", "not an address"], "bad": "x"})
    written = json.loads(path.read_text())
    assert written["pinned"] == {"internal.example.com": ["10.0.0.5"]}
    proxy = type("P", (), {})()
    proxy.rules_file, proxy.rules_extra, proxy.rules_pinned = path, [], {}
    proxy.rules_providers, proxy.rules_mtime, proxy.rules_checked, proxy._rules = [], None, -1e9, None
    rules = rt.ProxyServer.rules.fget(proxy)
    request = rt.Ruleset.request("10.0.0.5:80", "POST", "/admin", [["Host", "10.0.0.5"]], "10.0.0.5")
    assert rules.pins("10.0.0.5") and rules.http(request)["decision"] == "deny"
    # A file of a supervisor before 0.1.92 (a bare list) is still read.
    path.write_text(json.dumps(written["rulesets"]))
    proxy.rules_checked = -1e9
    assert rt.ProxyServer.rules.fget(proxy) is not None
