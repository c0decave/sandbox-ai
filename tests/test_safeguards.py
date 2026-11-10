"""Safeguard records (safeguards.py): every safeguard that refuses or ends something becomes one record with why and
the fix; the launcher's watch follows the run's logs; notifications are grouped and rate-limited; mute; the CLI."""
import importlib.util
import json
import os
from pathlib import Path
import threading

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / "sandbox" / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


S = load("safeguards_under_test", "safeguards.py")


@pytest.fixture(autouse=True)
def own_config(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path_factory.mktemp("config")))


def redact():
    return S.Redactor(["s3cr3t-value-42"])


# -- what each log line becomes ------------------------------------------------------------------------

def test_a_dropped_destination_and_a_refused_lookup_name_the_exact_command():
    entry = S.from_egress({"address": "203.0.113.7", "protocol": "tcp", "port": 443, "time": "2026-11-07T10:00:00Z",
                           "process": {"comm": "curl", "pid": 41, "argv": ["curl", "https://x.example"]}},
                          "claude-abc", redact())
    assert entry["kind"] == "firewall" and entry["level"] == "warning" and entry["time"] == "2026-11-07T10:00:00Z"
    assert entry["fix"] == "sandbox-ai net allow claude-abc --allow-tcp 203.0.113.7:443"
    assert entry["process"].startswith("curl (pid 41)") and entry["target"] == "203.0.113.7" and not entry["stays"]
    six = S.from_egress({"address": "2001:db8::1", "protocol": "tcp", "port": 22}, "r", redact())
    assert six["fix"].endswith("--allow-tcp [2001:db8::1]:22")
    udp = S.from_egress({"address": "198.51.100.1", "protocol": "udp", "port": 123}, "r", redact())
    assert udp["fix"].endswith("--allow 198.51.100.1")
    lookup = S.from_egress({"host": "pypi.org", "reason": "not in allowed hosts"}, "r", redact())
    assert lookup["kind"] == "dns" and lookup["fix"] == "sandbox-ai net allow r --allow-host pypi.org"
    odd = S.from_egress({"host": "x; rm -rf ~", "reason": "not in allowed hosts"}, "r", redact())
    assert odd["stays"] and "--allow-host" not in odd["fix"]  # never a command made of what the agent chose
    assert S.from_egress({"something": 1}, "r", redact()) is None


def test_proxy_refusals_are_told_apart():
    r = redact()
    plain = S.from_connection({"event": "denied", "destination": "192.0.2.5:443", "host": "evil.example",
                               "reason": "not allowed by the network options"}, "run1", r)
    assert plain["kind"] == "network" and plain["fix"] == "sandbox-ai net allow run1 --allow-host evil.example"
    ruled = S.from_connection({"event": "denied", "method": "DELETE", "url": "https://api.example/x", "rule": "no-del",
                               "ruleset": "rules", "reason": "DELETE /x on api.example is denied by rule no-del (rules)"},
                              "run1", r)
    assert ruled["kind"] == "rule" and ruled["rule"] == "rule no-del of rules" and ruled["target"] == ruled["rule"]
    assert "~/.config/sandbox-ai/rules.json" in ruled["fix"]
    publish = S.from_connection({"event": "denied", "method": "POST", "url": "https://upload.pypi.org/legacy/",
                                 "rule": "upload", "ruleset": "publish",
                                 "reason": "not approved (asking is off (--ask off or no approvals)): upload"}, "run1", r)
    assert publish["kind"] == "publish" and "--ask on" in publish["fix"]
    policy = S.from_connection({"event": "denied", "method": "GET", "url": "https://x.example/", "rule": "p1",
                                "ruleset": "policy", "reason": "denied by rule p1 (policy)"}, "run1", r)
    assert policy["kind"] == "policy" and policy["stays"]
    tainted = S.from_connection({"event": "denied", "method": "POST", "url": "https://x.example/", "rule": "t",
                                 "ruleset": "rules", "reason": "POST / on x.example is denied by rule t (rules): untrusted "
                                 "content from issue tracker entered this run at 2026-11-07T10:00:00Z"}, "run1", r)
    assert tainted["kind"] == "taint" and "sandbox-ai net untaint run1" in tainted["fix"]
    timeout = S.from_connection({"event": "denied", "destination": "192.0.2.9:443",
                                 "approval": {"decision": "deny", "scope": "run", "reason": "no answer within 60 s"},
                                 "reason": "not approved (no answer within 60 s)"}, "run1", r)
    assert timeout["kind"] == "approval-timeout"
    # The human's own "deny" is an answer, not a safeguard.
    assert S.from_connection({"event": "denied", "destination": "192.0.2.9:443", "reason": "not approved (answered)",
                              "approval": {"decision": "deny", "scope": "once", "reason": "answered"}}, "run1", r) is None
    assert S.from_connection({"event": "http", "url": "https://ok.example/"}, "run1", r) is None


def test_a_refused_push_a_canary_push_and_the_taint():
    r = redact()
    push = S.from_connection({"event": "http", "url": "https://github.com/me/repo.git/git-receive-pack",
                              "push": {"ok": False, "message": "src/secret.txt: an API key"}}, "run1", r)
    assert push["kind"] == "push" and "privacy-check" in push["fix"] and push["why"] == "src/secret.txt: an API key"
    canary = S.from_connection({"event": "http", "url": "https://github.com/me/repo.git/git-receive-pack",
                                "push": {"ok": False, "canary": ["aws"], "message": "carries canary tokens"}}, "run1", r)
    assert canary["kind"] == "canary" and canary["level"] == "error" and canary["announced"]
    taint = S.from_connection({"event": "taint", "taint": {"tainted": True, "source": {"what": "web page", "rule": "u1",
                                                                                         "ruleset": "rules"}}}, "run1", r)
    assert taint["kind"] == "taint" and "web page" in taint["what"] and taint["rule"] == "rule u1 of rules"


def test_tool_calls_devices_questions_and_the_supervisor():
    r = redact()
    tool = S.from_tool_call({"agent": "claude", "event": "PreToolUse", "tool": "Bash", "decision": "deny",
                             "rule": "no-rm", "ruleset": "--rules", "reason": "rm is denied"}, "run1", r, "tool")
    assert tool["kind"] == "rule" and tool["what"] == "claude tool Bash" and "the --rules file" in tool["fix"]
    assert S.from_tool_call({"agent": "claude", "event": "PreToolUse", "tool": "Bash", "decision": "allow"},
                            "run1", r, "tool") is None
    answered = S.from_tool_call({"tool": "Bash", "decision": "ask", "rule": "a", "ruleset": "rules",
                                 "approval": {"decision": "deny", "reason": "answered"},
                                 "reason": "not approved (answered): a"}, "run1", r, "tool")
    assert answered is None
    mcp = S.from_tool_call({"event": "call", "server": "github", "tool": "delete_repo", "decision": "deny",
                            "rule": "d", "ruleset": "policy", "reason": "denied"}, "run1", r, "mcp")
    assert mcp["kind"] == "policy" and mcp["what"] == "MCP tool github/delete_repo"
    device = S.from_device({"event": "refuse", "run": "run1", "value": "/dev/ttyUSB0", "reason": "not a USB serial"},
                           "run1", r)
    assert device["kind"] == "device" and "sandbox-ai devices add run1 /dev/ttyUSB0" in device["fix"]
    assert S.from_device({"event": "refuse", "run": "other", "value": "/dev/x", "reason": "r"}, "run1", r) is None
    canary = S.from_question({"kind": "canary", "canary": "aws", "summary": "a decoy file was read", "event": "read"},
                             None, "run1", r)
    assert canary["kind"] == "canary" and canary["announced"] and "canaries log run1" in canary["fix"]
    dns = S.from_question({"kind": "dns", "name": "pypi.org"}, {"reason": "no answer within 60 s"}, "run1", r)
    assert dns["kind"] == "approval-timeout" and "pypi.org" in dns["what"]
    # A connection's question that timed out is told by the connection log, with its process: not twice.
    assert S.from_question({"kind": "connect"}, {"reason": "no answer within 60 s"}, "run1", r) is None
    mode = S.from_supervisor({"kind": "mode", "what": "sandbox-ai net allow --allow-host", "why": "widens high",
                              "mode": "high", "option": "--allow-host"}, "run1", r)
    assert mode["kind"] == "mode" and "--mode medium" in mode["fix"] and not mode["stays"]
    assert S.from_supervisor({"kind": "canary", "what": "forged"}, "run1", r) is None  # not the supervisor's to say
    capped = S.from_supervisor({"kind": "policy", "what": "the answer", "why": "capped", "target": "approval scope"},
                               "run1", r)
    assert "narrower scope" in capped["fix"]
    shed = S.from_supervisor_memory({"rss": 3 << 30, "shed": ["file log", "attribution cache"]}, "run1", r)
    assert shed["kind"] == "supervisor-memory" and "3.0 GiB" in shed["why"]


def test_records_never_hold_secret_values():
    r = redact()
    token = "ghp" + "_" + "abcdefghijklmnopqrstuvwxyz0123456789"  # made here: no token-shaped text in the source
    entry = S.from_connection({"event": "denied", "method": "GET", "rule": "x", "ruleset": "rules",
                               "url": "https://api.example/?token=s3cr3t-value-42&q=1",
                               "reason": f"Authorization: Bearer {token} API_KEY=hunter22"}, "run1", r)
    text = json.dumps(entry)
    assert "s3cr3t-value-42" not in text and token not in text
    assert "hunter22" not in text and "[redacted]" in text
    # One line, plain text, bounded.
    long = S.record("rule", "x\n\x1b[31m" + "a" * 5000, "why", "fix", "r", redact=r)
    assert "\n" not in long["what"] and "\x1b" not in long["what"] and len(long["what"]) <= S.TEXT_LIMIT


# -- grouping, the rate limit, mute ---------------------------------------------------------------------

class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def firewall(address="203.0.113.7", run="run1"):
    return S.from_egress({"address": address, "protocol": "tcp", "port": 443}, run, redact())


def test_twelve_equal_refusals_are_one_notification_and_one_summary():
    sent, clock = [], Clock()
    n = S.Notifier(sent.append, clock=clock, where="sandbox-ai safeguards run1")
    for _ in range(12):
        n.add(firewall())
    n.flush()
    assert len(sent) == 1 and sent[0]["level"] == "warning" and "dropped by the firewall" in sent[0]["title"]
    assert "To allow it: sandbox-ai net allow run1 --allow-tcp 203.0.113.7:443" in sent[0]["text"]
    clock.now += S.WINDOW
    n.flush()
    assert len(sent) == 2
    assert sent[1]["title"] == "sandbox-ai run1: 12 connections to 203.0.113.7 dropped by the firewall in the last minute"
    assert "Why:" in sent[1]["text"] and "All of them: sandbox-ai safeguards run1" in sent[1]["text"]
    clock.now += S.WINDOW  # nothing new: the group is done, the next refusal is told at once again
    n.flush()
    n.add(firewall())
    assert len(sent) == 3 and sent[2]["count"] == 1


def test_the_rate_limit_holds_warnings_back_but_never_errors():
    sent, clock = [], Clock()
    n = S.Notifier(sent.append, clock=clock)
    for index in range(7):
        n.add(firewall(f"203.0.113.{index}"))
    assert len(sent) == S.RATE
    n.add(S.record("oom", "the kernel ended 1 process", "out of memory", "--memory 16g", "run1"))
    assert sent[-1]["level"] == "error" and len(sent) == S.RATE + 1
    clock.now += S.WINDOW
    n.flush()
    assert sent[-1]["kind"] == "summary" and sent[-1]["count"] == 3 and "3 more safeguard events" in sent[-1]["title"]


def test_muted_kinds_and_announced_records_are_not_told():
    sent = []
    n = S.Notifier(sent.append, muted={"firewall"}, clock=Clock())
    n.add(firewall())
    n.add(S.record("canary", "a decoy was read", "decoy", "nothing", "run1", announced=True))
    n.flush(final=True)
    assert sent == []
    n.add(S.from_egress({"host": "pypi.org"}, "run1", redact()))
    assert len(sent) == 1 and sent[0]["kind"] == "dns"


def test_the_mute_setting_is_checked_and_read(tmp_path, monkeypatch):
    assert S.check_mute(["firewall", "dns", "dns"]) == ["firewall", "dns"]
    for bad in ("firewall", ["nope"], [1]):
        with pytest.raises(ValueError):
            S.check_mute(bad)
    config = Path(os.environ["XDG_CONFIG_HOME"]) / "sandbox-ai"
    config.mkdir(parents=True)
    (config / "defaults.json").write_text(json.dumps({"version": 1, "safeguard_mute": ["dns"]}))
    assert S.muted() == {"dns"}
    launcher = load("agent_sandbox_for_safeguards_test", "agent_sandbox.py")
    a = launcher.parse_args(["claude", "--no-config", "--workspace", str(tmp_path)])
    assert launcher.load_user_defaults(a)["safeguard_mute"] == ["dns"]
    (config / "defaults.json").write_text(json.dumps({"version": 1, "safeguard_mute": ["loud"]}))
    with pytest.raises(ValueError, match="safeguard_mute"):
        launcher.load_user_defaults(a)


# -- the launcher's watch -------------------------------------------------------------------------------

def append(path, *records):
    with open(path, "a") as stream:
        for entry in records:
            stream.write(json.dumps(entry) + "\n")


def test_the_watch_follows_the_runs_logs_writes_records_and_tells(tmp_path):
    logs, control = tmp_path / "logs", tmp_path / "control"
    (control / "approvals").mkdir(parents=True)
    logs.mkdir()
    append(logs / "egress-denied.jsonl", {"address": "192.0.2.1", "protocol": "tcp", "port": 80})  # an earlier run's
    sent = []
    w = S.Watch("run1", log_dir=logs, control_dir=control, connection_log="connections-1.jsonl", notify=sent.append,
                secrets=["s3cr3t-value-42"], muted_kinds=set(), interval=60)
    append(logs / "egress-denied.jsonl", *[{"address": "203.0.113.7", "protocol": "tcp", "port": 443}] * 3,
           {"host": "pypi.org", "reason": "not in allowed hosts"})
    append(logs / "connections-1.jsonl", {"event": "denied", "method": "GET", "url": "https://x.example/?k=s3cr3t-value-42",
                                          "rule": "r1", "ruleset": "rules", "reason": "denied by rule r1"})
    append(control / "safeguard-events.jsonl", {"kind": "mode", "what": "sandbox-ai net allow --allow-host",
                                                "why": "widens high", "mode": "high"})
    (control / "approvals" / "1-2-3.json").write_text(json.dumps({"id": "1-2-3", "kind": "canary", "canary": "aws",
                                                                  "summary": "a decoy file was read"}))
    assert w.poll() == 7
    w.report("memory", "ended pytest (PID 9, 2.0 GiB)", "memory nearly full", "--memory 16g", process="pytest (pid 9)")
    records = S.read(logs / S.FILE)
    assert sorted(r["kind"] for r in records) == sorted(["firewall"] * 3 + ["dns", "rule", "mode", "canary", "memory"])
    assert all(r["run"] == "run1" for r in records) and "s3cr3t-value-42" not in (logs / S.FILE).read_text()
    assert oct((logs / S.FILE).stat().st_mode & 0o777) == "0o600"
    # Told: the first firewall refusal (the others wait for the window), dns, rule, mode; the canary told itself;
    # the memory record is the fifth warning within the minute: held back by the rate limit.
    assert [n["kind"] for n in sent] == ["firewall", "dns", "rule", "mode"]
    lines = w.stop()
    assert len(lines) == 1 and "8 refused or ended" in lines[0] and "sandbox-ai safeguards run1" in lines[0]
    # The end tells the rest of the group and what the rate held back.
    assert any(n["kind"] == "firewall" and n["count"] == 3 for n in sent)
    assert sent[-1]["kind"] == "summary" and "memory" in sent[-1]["text"]


def test_without_a_log_directory_the_records_go_to_the_control_directory(tmp_path):
    w = S.Watch("run2", control_dir=tmp_path, notify=lambda n: None, muted_kinds=set())
    w.report("oom", "the kernel ended 1 process", "out of memory", "--memory 16g")
    assert S.read(tmp_path / S.FILE)[0]["level"] == "error"
    assert S.run_files({"directory": str(tmp_path), "log_dir": None}) == [tmp_path / S.FILE]


def test_the_supervisor_writes_its_own_events(tmp_path):
    rt = load("runtime_for_safeguards_test", "runtime.py")
    rt.safeguard_event("mode", "sandbox-ai net allow --allow-host", "widens\nthe mode", directory=tmp_path,
                       mode="high", option=None)
    entry = json.loads((tmp_path / "safeguard-events.jsonl").read_text())
    assert entry["kind"] == "mode" and entry["mode"] == "high" and "option" not in entry and "\n" not in entry["why"]
    rt.safeguard_event("mode", "x", "y", directory=tmp_path / "missing")  # never raises
    # A strict mode's refusal at the control socket becomes a record.
    import socket
    import time as clock
    path, stop = tmp_path / "c.sock", threading.Event()
    network = rt.RuntimeNetwork({}, None, lambda script: None)
    rt.CONTROL_DIR = tmp_path
    thread = threading.Thread(target=rt.control_server, args=(path, network, stop),
                              kwargs={"mode": {"name": "high", "refuses": ["--allow-host"]}}, daemon=True)
    thread.start()
    deadline = clock.monotonic() + 5
    while not path.exists() and clock.monotonic() < deadline:
        clock.sleep(0.02)
    try:
        with socket.socket(socket.AF_UNIX) as client:
            client.connect(str(path))
            client.sendall(json.dumps({"op": "allow", "https": ["192.0.2.7"]}).encode() + b"\n")
            assert not json.loads(client.makefile().readline())["ok"]
    finally:
        stop.set()
        thread.join(5)
    lines = [json.loads(line) for line in (tmp_path / "safeguard-events.jsonl").read_text().splitlines()]
    assert lines[-1]["kind"] == "mode" and lines[-1]["option"] == "--allow-host"


def test_the_broker_records_an_answer_wider_than_the_mode_allows(tmp_path):
    rt = load("runtime_for_safeguards_broker", "runtime.py")
    broker = rt.ApprovalBroker(tmp_path / "approvals", None, max_scope="once")
    broker.capped_by = ("mode", "high")
    (tmp_path / "approvals" / "1-1-1.answer").write_text(json.dumps({"decision": "allow", "scope": "always"}))
    assert broker._read_answer("1-1-1")["scope"] == "once"
    entry = json.loads((tmp_path / "safeguard-events.jsonl").read_text())
    assert entry["kind"] == "mode" and entry["target"] == "approval scope" and "security mode high" in entry["why"]


def test_the_memory_watch_reports_what_it_ends_and_oom_kills(tmp_path):
    launcher = load("agent_sandbox_for_safeguards_memory", "agent_sandbox.py")
    told = []
    w = launcher.MemoryWatch(lambda: None, "run1", safeguard=lambda *a, **k: told.append((a, k)))
    w.first_oom = w.oom_told = 0
    w.tell_oom(2)
    assert told[0][0][0] == "oom" and "2 processes" in told[0][0][1] and "--memory" in told[0][0][3]
    w.tell("memory", "ended x", "why", "fix", process="x (pid 1)")
    assert told[1][0][0] == "memory" and told[1][1]["process"] == "x (pid 1)"


def test_desktop_notifications_have_the_levels_urgency(monkeypatch):
    calls = []

    class Approvals:
        @staticmethod
        def desktop_notification(title, text, urgency="critical"):
            calls.append((title, urgency))
    send = S.desktop_sender(Approvals)
    send({"level": "warning", "title": "w", "text": ""})
    send({"level": "error", "title": "e", "text": ""})
    assert calls == [("w", "normal"), ("e", "critical")]
    ap = load("approvals_for_safeguards_test", "approvals.py")
    argv = []
    monkeypatch.setattr(ap.shutil, "which", lambda name: "/usr/bin/notify-send")
    monkeypatch.setattr(ap.subprocess, "run", lambda command, **kw: argv.append(command))
    ap.desktop_notification("t", "x", urgency="normal")
    ap.desktop_notification("t", "x", urgency="loud")
    assert "--urgency=normal" in argv[0] and "--urgency=critical" in argv[1]


# -- the command ----------------------------------------------------------------------------------------

def test_the_command_lists_filters_and_explains(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    S.append(logs / S.FILE, [firewall(), S.from_egress({"host": "pypi.org"}, "run1", redact()),
                             S.record("oom", "the kernel ended 1 process", "out of memory", "--memory 16g", "run2")])
    out = []
    assert S.cli(["--log-dir", str(logs)], out.append) == 0
    text = "\n".join(out)
    assert "firewall" in text and "allow:   sandbox-ai net allow run1 --allow-tcp 203.0.113.7:443" in text
    S.append(logs / S.FILE, [firewall(), firewall()])  # equal ones in the same second: each is a record
    out = []
    S.cli(["--log-dir", str(logs), "--json", "--kind", "firewall"], out.append)
    assert len(out) == 3
    out = []
    S.cli(["--log-dir", str(logs), "--json", "--kind", "oom"], out.append)
    assert [json.loads(line)["run"] for line in out] == ["run2"]
    out = []
    S.cli(["run2", "--log-dir", str(logs), "--json"], out.append)
    assert len(out) == 1
    out = []
    S.cli(["kinds", "--json"], out.append)
    kinds = json.loads("\n".join(out))
    assert {k["kind"] for k in kinds} == set(S.KINDS) and {k["level"] for k in kinds} == {"warning", "error"}
    with pytest.raises(ValueError):
        S.cli(["--log-dir", str(logs), "--kind", "nope"], out.append)
    main = load("main_for_safeguards_test", "main.py")
    assert main.PYTHON_COMMANDS["safeguards"] == ("safeguards.py",)


def test_follow_shows_what_is_appended(tmp_path):
    path = tmp_path / S.FILE
    S.append(path, [firewall()])
    shown, stop = [], threading.Event()
    thread = threading.Thread(target=S.follow, args=(lambda: [path], lambda e: True, shown.append, stop, 0.05),
                              daemon=True)
    thread.start()
    import time
    time.sleep(0.2)
    S.append(path, [S.from_egress({"host": "pypi.org"}, "run1", redact())])
    deadline = time.monotonic() + 5
    while not shown and time.monotonic() < deadline:
        time.sleep(0.05)
    stop.set()
    thread.join(5)
    assert [e["kind"] for e in shown] == ["dns"]


def test_the_launcher_starts_a_watch_with_the_runs_secrets_and_none_for_a_probe(tmp_path, monkeypatch):
    launcher = load("agent_sandbox_for_safeguards_start", "agent_sandbox.py")
    from types import SimpleNamespace
    a = SimpleNamespace(probe=False, run_id="claude-x", log_dir=tmp_path, control_dir=None)
    cfg = {"env": {"GITHUB_TOKEN": "ghp-value-123456", "PLAIN": "visible-value", "MINE": "chosen-secret-1"},
           "secret_names": ["MINE"]}
    watch = launcher.start_safeguards(a, None, cfg)
    try:
        assert watch is not None and watch.path == tmp_path / S.FILE
        assert set(watch.redact.values) == {"ghp-value-123456", "chosen-secret-1"}
        watch.report("guard", "x chosen-secret-1", "y", "z")
        assert "chosen-secret-1" not in (tmp_path / S.FILE).read_text()
    finally:
        watch.stop()
    assert launcher.start_safeguards(SimpleNamespace(**{**vars(a), "probe": True}), None, cfg) is None


def test_tail_reads_only_what_is_new_and_a_new_runs_file_from_its_start(tmp_path):
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    S.append(one / S.FILE, [firewall()])
    tail = S.Tail()
    runs = {"a": {"log_dir": str(one)}}
    assert tail.running(runs) == []  # there before the first look
    S.append(one / S.FILE, [firewall("192.0.2.1")])
    S.append(two / S.FILE, [firewall("192.0.2.2", run="b")])
    runs["b"] = {"log_dir": None, "directory": str(two)}
    assert [e["target"] for e in tail.running(runs)] == ["192.0.2.1", "192.0.2.2"]
    assert tail.running(runs) == []
    (one / S.FILE).write_text("")  # cut: read from its start again
    S.append(one / S.FILE, [firewall("192.0.2.3")])
    assert [e["target"] for e in tail.running(runs)] == ["192.0.2.3"]
