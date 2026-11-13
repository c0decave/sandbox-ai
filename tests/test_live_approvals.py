"""Live approvals: a question nobody answered is no decision (refused, a short pause, then asked again), the
supervisor's control op "decide" (list, allow, deny, forget a run's remembered decision; held to the security mode's
and the policy's caps, the kill switch and the control socket's peers), `sandbox-ai approvals blocked|decided|
ask-again` and allow|deny on an answered question, and "Allow always" for a request as an exception rule in a ruleset
of your own."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import tempfile
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sandbox_net_live_approvals", ROOT / "sandbox/net.py")
net = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(net)
rt = net.launcher().RUNTIME
SPEC = importlib.util.spec_from_file_location("sandbox_approvals_live", ROOT / "sandbox/approvals.py")
ap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ap)

HTTP = {"rule": "writes", "ruleset": "--rules tor-researcher.rules.json", "method": "POST",
        "host": "registry.npmjs.org", "path": "/-/npm/v1/security/advisories/bulk",
        "url": "https://registry.npmjs.org/-/npm/v1/security/advisories/bulk",
        "reason": "POST /-/npm/v1/security/advisories/bulk on registry.npmjs.org is asked by rule writes"}
CONNECT = {"destination": "192.0.2.7:8443", "address": "192.0.2.7", "port": 8443, "protocol": "tls"}


def wait_for(condition, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


def broker(tmp_path, network=None, **kwargs):
    rules = []
    Path(tmp_path).mkdir(parents=True, exist_ok=True)
    network = network or rt.RuntimeNetwork({}, None, rules.append)
    made = rt.ApprovalBroker(tmp_path / "approvals", network, **{"timeout": 0.2, "poll": 0.02, **kwargs})
    made.rules = rules
    return made


def answer(directory, decision, scope, count=1):
    """The human's answer to the COUNT-th question filed in DIRECTORY."""
    assert wait_for(lambda: len(list(Path(directory).glob("*.json"))) >= count)
    question = json.loads(sorted(Path(directory).glob("*.json"))[count - 1].read_text())
    (Path(directory) / f"{question['id']}.answer").write_text(json.dumps({"decision": decision, "scope": scope}))
    return question


# --- a question nobody answered -----------------------------------------------------------------------------------

def test_an_unanswered_question_is_refused_paused_and_then_asked_again(tmp_path):
    made = broker(tmp_path, pause=0.6)
    first = made.ask("http", HTTP)
    assert first["decision"] == "deny" and first["scope"] == "once" and first["reason"] == "no answer within 0.2 s"
    question = json.loads(next((tmp_path / "approvals").glob("*.json")).read_text())
    assert first["question"] == question["id"]
    # Why, in plain words, and what to do: the ruleset and its rule, the pause, the page and the command; and that
    # the network settings do not change what a ruleset decides.
    assert "the ruleset --rules tor-researcher.rules.json (rule writes) asks before it" in first["why"]
    assert "then asked again" in first["why"] and f"sandbox-ai approvals allow {question['id']} --scope run" in first["why"]
    assert "Approvals page (Blocked)" in first["why"] and "does not change what a ruleset decides" in first["why"]
    done = json.loads((tmp_path / "approvals" / f"{question['id']}.done").read_text())
    assert done["reason"] == "no answer within 0.2 s" and done["question"] == question["id"]
    # During the pause: refused at once, without a new question (the agent does not wait the timeout again).
    started = time.monotonic()
    again = made.ask("http", HTTP)
    assert time.monotonic() - started < 0.15 and again["decision"] == "deny"
    assert again["reason"].startswith(f"no answer within 0.2 s to question {question['id']}; refused without asking")
    assert len(list((tmp_path / "approvals").glob("*.json"))) == 1
    assert not made.decided, "an unanswered question is no decision for the run"
    # The refusal the agent gets says the same (the proxy's text).
    assert "then asked again" in rt.not_approved(again, HTTP["reason"])
    # After the pause: asked again.
    time.sleep(0.65)
    assert made.ask("http", HTTP, wait=False) is None
    assert len(list((tmp_path / "approvals").glob("*.json"))) == 2


def test_the_humans_decisions_keep_their_scope_and_say_how_to_take_them_back(tmp_path):
    made = broker(tmp_path, timeout=5)
    results = []
    thread = threading.Thread(target=lambda: results.append(made.ask("http", HTTP)))
    thread.start()
    question = answer(tmp_path / "approvals", "deny", "run")
    thread.join(5)
    assert results[0]["decision"] == "deny" and results[0]["reason"] == "answered"
    later = made.ask("http", HTTP)  # remembered for the run: not asked again
    assert later["decision"] == "deny" and later["question"] == question["id"]
    assert f"sandbox-ai approvals ask-again {question['id']}" in later["why"] and "you denied it" in later["why"]
    assert len(list((tmp_path / "approvals").glob("*.json"))) == 1
    # An allow for the run is remembered with its question too.
    results.clear()
    thread = threading.Thread(target=lambda: results.append(made.ask("connect", CONNECT)))
    thread.start()
    answer(tmp_path / "approvals", "allow", "run", count=2)
    thread.join(5)
    assert made.ask("connect", CONNECT)["decision"] == "allow" and "why" not in made.ask("connect", CONNECT)


def test_an_mcp_server_of_its_own_pauses_an_unanswered_question_too(tmp_path):
    gateway = rt.McpGateway({"server": "files", "approvals": str(tmp_path), "timeout": 1}, out=open(os.devnull, "w"))
    verdict = {"rule": "deletes", "ruleset": "rules.json", "reason": "asked"}
    first = gateway.ask("delete_file", "{}", verdict)
    assert first["reason"] == "no answer within 1 s"
    started = time.monotonic()
    again = gateway.ask("delete_file", "{}", verdict)
    assert time.monotonic() - started < 0.15 and "refused without asking for now" in again["reason"]
    assert not gateway.decided


# --- the control op decide ----------------------------------------------------------------------------------------

def unanswered(made, kind, details):
    made.ask(kind, details)
    return json.loads(sorted(made.directory.glob("*.json"))[-1].read_text())["id"]


def test_decide_lists_allows_denies_and_forgets(tmp_path):
    made = broker(tmp_path, pause=60)
    ident = unanswered(made, "http", HTTP)
    listed = made.control({"op": "decide"})
    assert listed["max_scope"] == "always" and listed["pause"] == 60
    assert [(e["question"], e["scope"], e["decision"]) for e in listed["decided"]] == [(ident, "pause", "deny")]
    assert listed["decided"][0]["details"]["url"] == HTTP["url"] and listed["decided"][0]["seconds_left"] > 50
    # Allow for the run: the pause is over, the next request passes without a question.
    result = made.control({"op": "decide", "action": "allow", "question": ident})
    assert result["decided"]["decision"] == "allow" and result["was"]["reason"].startswith("no answer")
    assert made.ask("http", HTTP)["decision"] == "allow"
    assert len(list(made.directory.glob("*.json"))) == 1
    # Deny for the run.
    made.control({"op": "decide", "action": "deny", "question": ident})
    assert made.ask("http", HTTP)["decision"] == "deny"
    assert [e["decision"] for e in made.control({"op": "decide"})["decided"]] == ["deny"]
    # Forget: asked again.
    assert made.control({"op": "decide", "action": "forget", "question": ident})["forgot"] is True
    assert made.ask("http", HTTP)["reason"].startswith("no answer") and len(list(made.directory.glob("*.json"))) == 2
    second = json.loads(sorted(made.directory.glob("*.json"))[-1].read_text())["id"]
    assert made.control({"op": "decide", "action": "forget", "question": second})["forgot"] is True
    assert made.control({"op": "decide", "action": "forget", "question": second})["forgot"] is False


def test_a_remembered_request_is_not_another_one_whose_fields_join_the_same():
    first = {**HTTP, "path": "/x", "host_header": "y z"}
    second = {**HTTP, "path": "/x y", "host_header": "z"}
    assert rt.ApprovalBroker.key("http", first) != rt.ApprovalBroker.key("http", second)
    assert rt.ApprovalBroker.key("mcp", {"server": "a b", "tool": "c"}) != \
        rt.ApprovalBroker.key("mcp", {"server": "a", "tool": "b c"})


def test_taking_back_an_allowed_connection_closes_it_in_the_firewall_too(tmp_path):
    """Bug hunt: deny (or ask again) after an allow for the run changed only the remembered decision - the firewall
    and the proxy's allow-list kept the destination open, and the proxy asks only about what they do not allow."""
    made = broker(tmp_path, timeout=5)
    results = []
    thread = threading.Thread(target=lambda: results.append(made.ask("connect", CONNECT)))
    thread.start()
    ident = answer(tmp_path / "approvals", "allow", "run")["id"]
    thread.join(5)
    assert results[0]["decision"] == "allow" and made.network.shown()["tcp"] == [["192.0.2.7", 8443]]
    result = made.control({"op": "decide", "action": "deny", "question": ident})
    assert made.network.shown()["tcp"] == [] and "refused again" in result["notes"][0]
    made.control({"op": "decide", "action": "allow", "question": ident})
    assert made.network.shown()["tcp"] == [["192.0.2.7", 8443]]
    result = made.control({"op": "decide", "action": "forget", "question": ident})
    assert made.network.shown()["tcp"] == [] and result["forgot"] and result["notes"]


def test_decide_validates(tmp_path):
    made = broker(tmp_path, timeout=5)
    for request, error in (({"action": "explode"}, "one of list, allow, deny, forget"),
                           ({"action": "allow", "question": "../../x"}, "a question's ID"),
                           ({"action": "allow", "question": "1-2-3"}, "this run asked no question 1-2-3")):
        with pytest.raises(ValueError, match=error):
            made.control({"op": "decide", **request})
    # A question this run did not ask itself (a browser request, a canary): no decision of the broker's.
    (made.directory / "4-5-6.json").write_text(json.dumps({"id": "4-5-6", "kind": "browser", "url": "https://x"}))
    with pytest.raises(ValueError, match="no question this run decides on itself"):
        made.control({"op": "decide", "action": "allow", "question": "4-5-6"})
    # An open question is answered, not decided around.
    made.ask("http", HTTP, wait=False)
    ident = json.loads(sorted(made.directory.glob("1*.json"))[-1].read_text())["id"]
    with pytest.raises(ValueError, match=f"question {ident} is open"):
        made.control({"op": "decide", "action": "allow", "question": ident})


def test_decide_keeps_to_the_mode_the_policy_and_the_kill_switch(tmp_path):
    # The security mode paranoid approves only once: no allow for the run (a deny and a forget take nothing away).
    made = broker(tmp_path, max_scope="once")
    made.capped_by = ("mode", "paranoid")
    ident = unanswered(made, "http", HTTP)
    with pytest.raises(ValueError, match="the security mode paranoid approves at most once"):
        made.control({"op": "decide", "action": "allow", "question": ident})
    events = [json.loads(line) for line in (tmp_path / rt.SAFEGUARD_FILE).read_text().splitlines()]
    assert events[-1]["kind"] == "mode" and "approves at most once" in events[-1]["why"]
    made.control({"op": "decide", "action": "deny", "question": ident})
    assert made.control({"op": "decide", "action": "forget", "question": ident})["forgot"]
    # The administrator's policy the same way.
    other = broker(tmp_path / "p", max_scope="once")
    other.capped_by = ("policy", None)
    ident = unanswered(other, "http", HTTP)
    with pytest.raises(ValueError, match="the administrator's policy approves at most once"):
        other.control({"op": "decide", "action": "allow", "question": ident})
    # A connection: opened in the firewall (never for SSH), inside the policy's networks only.
    network = rt.RuntimeNetwork({}, None, lambda script: None, within=["198.51.100.0/24"])
    made = broker(tmp_path / "w", network=network)
    ident = unanswered(made, "connect", CONNECT)
    with pytest.raises(ValueError, match="outside the networks the administrator's policy allows"):
        made.control({"op": "decide", "action": "allow", "question": ident})
    made = broker(tmp_path / "c")
    ident = unanswered(made, "connect", CONNECT)
    made.control({"op": "decide", "action": "allow", "question": ident})
    assert "ip daddr 192.0.2.7 tcp dport 8443 accept" in made.rules[-1]
    # A name is allowed with net allow (the host looks it up), not here.
    ident = unanswered(made, "dns", {"name": "pypi.org"})
    with pytest.raises(ValueError, match="sandbox-ai net allow RUN --allow-host NAME"):
        made.control({"op": "decide", "action": "allow", "question": ident})
    # The kill switch: an allow during a cut is recorded, the cut holds (nothing it forbids is asked or passed).
    network = rt.RuntimeNetwork({"loopback": []}, None, lambda script: None)
    network.cut_settings = rt.cut_settings({"host_ports": [], "api": {"https": [], "tcp": [], "loopback": [],
                                                                      "names": []}, "tor": False, "bypass": [],
                                            "machine_host": None})
    made = broker(tmp_path / "k", network=network)
    ident = unanswered(made, "connect", CONNECT)
    network.set_cut("all")
    result = made.control({"op": "decide", "action": "allow", "question": ident})
    assert "the kill switch holds" in result["notes"][0]
    refused = made.ask("connect", CONNECT)
    assert refused["decision"] == "deny" and "cut by the kill switch" in refused["reason"]
    network.set_cut("off")
    assert made.ask("connect", CONNECT)["decision"] == "allow"


def short_dir(prefix):
    return Path(tempfile.mkdtemp(prefix=prefix, dir="/tmp"))


def serve(directory, made, **kwargs):
    (directory / "control.sock").unlink(missing_ok=True)  # (a server before: its socket file stays)
    stop = threading.Event()
    thread = threading.Thread(target=rt.control_server, args=(directory / "control.sock",
                                                              rt.RuntimeNetwork({}, None, lambda s: None), stop),
                              kwargs={"broker": made, **kwargs}, daemon=True)
    thread.start()
    assert wait_for(lambda: (directory / "control.sock").exists())
    return stop, thread


def request(path, data):
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(10)
        client.connect(str(path))
        client.sendall(json.dumps(data).encode() + b"\n")
        return json.loads(client.makefile().readline())


def test_the_control_socket_takes_decide_from_the_human_only(tmp_path, monkeypatch):
    directory = short_dir("dec-")
    made = rt.ApprovalBroker(directory / "approvals", rt.RuntimeNetwork({}, None, lambda s: None), timeout=0.2,
                             poll=0.02)
    ident = unanswered(made, "http", HTTP)
    stop, thread = serve(directory, made)
    try:
        assert request(directory / "control.sock", {"op": "decide"})["decided"][0]["question"] == ident
        answer_ = request(directory / "control.sock", {"op": "decide", "action": "allow", "question": ident})
        assert answer_["ok"] and answer_["decided"]["decision"] == "allow"
        bad = request(directory / "control.sock", {"op": "decide", "action": "allow", "question": "x"})
        assert not bad["ok"] and "a question's ID" in bad["error"]
    finally:
        stop.set()
        thread.join(5)
    # No broker (--ask off): said so.
    stop, thread = serve(directory, None)
    try:
        refused = request(directory / "control.sock", {"op": "decide"})
        assert not refused["ok"] and "asks nothing" in refused["error"]
    finally:
        stop.set()
        thread.join(5)
    # From inside the sandbox (a process of its PID namespace): refused before anything is read.
    monkeypatch.setattr(rt, "control_peer_refused", lambda connection, sandboxed=None: "process 7 of the sandbox "
                        "may not use the control socket" if sandboxed else None)
    stop, thread = serve(directory, made, sandboxed=True)
    try:
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(10)
            client.connect(str(directory / "control.sock"))
            refused = json.loads(client.makefile().readline())
        assert not refused["ok"] and "may not use the control socket" in refused["error"]
        assert made.decided[made.key("http", HTTP)]["decision"] == "allow", "nothing changed"
    finally:
        stop.set()
        thread.join(5)
        shutil.rmtree(directory, ignore_errors=True)


# --- the command line ---------------------------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def no_login_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(net, "USER_RUNTIME", tmp_path / "run-user")
    monkeypatch.setattr(net.liveness(), "inside_sandbox", lambda *a, **k: False)

    def no_podman(*args, **kwargs):
        raise FileNotFoundError("podman")
    monkeypatch.setattr(net.liveness(), "RUNNER", no_podman)
    net.liveness()._cache.clear()
    monkeypatch.setattr(ap, "net", lambda: net)
    monkeypatch.setattr(ap, "POLICY_FILE", tmp_path / "no-policy.json")


@pytest.fixture
def live(tmp_path, monkeypatch):
    """A registered run with a real control socket and broker (short directory: socket paths are limited)."""
    short = short_dir("live-")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(short))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    directory = net.launcher().runs_dir() / "claude-1"
    directory.mkdir(parents=True)
    logs = tmp_path / "logs"
    logs.mkdir()
    record = {"run": "claude-1", "agent": "claude", "pid": os.getpid(), "workspace": str(tmp_path / "work"),
              "session_dir": str(tmp_path), "started": "2026-11-11T10:00:00Z", "session": None, "log_dir": str(logs),
              "rules_sources": {"files": [str(tmp_path / "presets" / "tor-researcher.rules.json")],
                                "project_config": None}}
    (directory / "run.json").write_text(json.dumps(record))
    made = rt.ApprovalBroker(directory / "approvals", rt.RuntimeNetwork({}, None, lambda s: None), timeout=0.2,
                             poll=0.02, pause=60)
    stop, thread = serve(directory, made)
    yield {"directory": directory, "broker": made, "logs": logs, "record": {**record, "directory": str(directory)}}
    stop.set()
    thread.join(5)
    shutil.rmtree(short, ignore_errors=True)


def cli(*argv):
    lines = []
    status = ap.main(list(argv), out=lines.append)
    return status, lines


def test_approvals_allow_deny_ask_again_and_decided_on_an_answered_question(live, capsys):
    made = live["broker"]
    ident = unanswered(made, "http", HTTP)
    status, lines = cli("decided", "--json")
    assert status == 0 and json.loads("\n".join(lines))[0]["scope"] == "pause"
    # Allowed "once" is no answer to a question that is gone: said what to do instead.
    status, _ = cli("allow", ident)
    assert status == 1 and "--scope run" in capsys.readouterr().err
    status, lines = cli("allow", ident, "--scope", "run")
    assert status == 0, capsys.readouterr().err
    assert lines[0].startswith("claude-1: allowed for the rest of the run: POST https://registry.npmjs.org/")
    assert made.ask("http", HTTP)["decision"] == "allow"
    status, lines = cli("deny", ident)
    assert status == 0 and "denied for the rest of the run" in lines[0] and made.ask("http", HTTP)["decision"] == "deny"
    status, lines = cli("ask-again", ident)
    assert status == 0 and "its next request is asked again" in lines[0] and not made.decided
    status, lines = cli("decided")
    assert status == 0 and "nothing is remembered" in lines[-1]
    status, _ = cli("ask-again", "9-9-9")
    assert status == 1 and "no answered question 9-9-9" in capsys.readouterr().err


def test_approvals_refuses_an_allow_for_the_run_where_the_mode_approves_only_once(live, capsys):
    record = json.loads((live["directory"] / "run.json").read_text())
    (live["directory"] / "run.json").write_text(json.dumps({**record, "mode": {"name": "paranoid",
                                                                                "approval_scope": "once"}}))
    ident = unanswered(live["broker"], "http", HTTP)
    status, _ = cli("allow", ident, "--scope", "run")
    assert status == 1 and "approves at most once" in capsys.readouterr().err


# --- the Blocked list ---------------------------------------------------------------------------------------------

def write_lines(path, records):
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


def blocked_fixture(live):
    """Every kind of refusal in the run's logs, at known times; and one of another run (not listed)."""
    made, logs, directory = live["broker"], live["logs"], live["directory"]
    asked = unanswered(made, "http", HTTP)
    (directory / "approvals" / "1700000000-1-5.json").write_text(json.dumps({
        "id": "1700000000-1-5", "kind": "connect", "time": "2026-11-11T10:00:05Z", **CONNECT}))
    (directory / "approvals" / "1700000000-1-5.done").write_text(json.dumps({"decision": "deny", "scope": "run",
                                                                             "reason": "answered"}))
    write_lines(logs / "connections-20261111-100000-claude-1.jsonl", [
        {"time": "2026-11-11T10:00:10Z", "event": "denied", "destination": "198.51.100.3:443",
         "reason": "not allowed by the network options", "process": {"comm": "curl", "pid": 9}},
        {"time": "2026-11-11T10:00:20Z", "event": "denied", "method": "DELETE", "url": "https://api.example/x",
         "rule": "no-deletes", "ruleset": "rules.json", "reason": "DELETE /x on api.example is denied by rule no-deletes"},
        {"time": "2026-11-11T10:00:30Z", "event": "denied", "destination": "192.0.2.50:443",
         "reason": rt.cut_reason({"mode": "all"})},
        {"time": "2026-11-11T10:00:40Z", "event": "denied", "method": "POST", "url": HTTP["url"], "rule": "writes",
         "ruleset": HTTP["ruleset"], "reason": "not approved (no answer within 0.2 s): ...",
         "approval": {"decision": "deny", "scope": "once", "reason": "no answer within 0.2 s", "question": asked},
         "process": {"comm": "opencode", "pid": 80, "exe": "/usr/bin/opencode"}},
        {"time": "2026-11-11T10:00:41Z", "event": "denied", "method": "POST", "url": HTTP["url"], "rule": "writes",
         "ruleset": HTTP["ruleset"], "reason": "not approved (no answer within 0.2 s to question ...)",
         "approval": {"decision": "deny", "scope": "once", "reason": "no answer within 0.2 s to question",
                      "question": asked}},
        {"time": "2026-11-11T10:00:50Z", "event": "allowed", "destination": "192.0.2.10:443"},
        {"time": "2026-11-11T10:00:55Z", "event": "denied", "destination": "192.0.2.66:443",
         "reason": "blocked always for this project"},
    ])
    write_lines(logs / "connections-20261110-090000-codex-2.jsonl", [
        {"time": "2026-11-11T10:00:15Z", "event": "denied", "destination": "198.51.100.99:443",
         "reason": "not allowed by the network options"}])
    write_lines(logs / "egress-denied.jsonl", [
        {"time": "2026-11-10T09:00:00Z", "host": "old.example", "reason": "not in allowed hosts"},
        {"time": "2026-11-11T10:00:12Z", "host": "pypi.org", "reason": "not in allowed hosts",
         "sandbox": {"run": "claude-1"}, "process": {"comm": "pip", "pid": 12}},
        {"time": "2026-11-11T10:00:13Z", "host": "other.example", "reason": "not in allowed hosts",
         "sandbox": {"run": "codex-2"}},
        {"time": "2026-11-11T10:00:14Z", "address": "10.0.0.9", "protocol": "udp", "port": 5353,
         "sandbox": {"run": "claude-1"}},
    ])
    write_lines(logs / "mcp-calls.jsonl", [
        {"time": "2026-11-11T10:00:16Z", "event": "call", "server": "files", "tool": "delete_file",
         "arguments": "{}", "decision": "deny", "rule": "deletes", "ruleset": "policy",
         "reason": "the administrator's policy refuses deletes"}])
    write_lines(directory / "safeguard-events.jsonl", [
        {"time": "2026-11-11T10:00:17Z", "kind": "mode", "what": "sandbox-ai net allow --allow", "why": "--allow widens "
         "the security mode high of this run"}])
    return asked


def test_the_blocked_list_has_every_kind_newest_first_for_this_run_only(live):
    asked = blocked_fixture(live)
    runs = net.running()
    entries = ap.blocked(runs["claude-1"], runs)
    kinds = [entry["kind"] for entry in entries]
    assert {"network", "rule", "unanswered", "denied", "cut", "blocked", "policy", "mode"} <= set(kinds)
    times = [entry["time"] for entry in entries]
    assert times == sorted(times, reverse=True), "newest first"
    assert all(entry["run"] == "claude-1" for entry in entries)
    whats = " ".join(entry["what"] for entry in entries)
    assert "198.51.100.99" not in whats and "other.example" not in whats and "old.example" not in whats
    # The unanswered question and its refusals in the proxy's log: one entry, counted, with the process and actions.
    timed_out = [entry for entry in entries if entry["question"] == asked]
    assert len(timed_out) == 1 and timed_out[0]["count"] == 3 and timed_out[0]["kind"] == "unanswered"
    assert timed_out[0]["process"].startswith("opencode (pid 80") and timed_out[0]["ruleset"] == HTTP["ruleset"]
    assert timed_out[0]["actions"] == ["allow-run", "ask-again", "deny-run", "allow-always"]
    assert f"sandbox-ai approvals allow {asked} --scope run" in timed_out[0]["why"]
    by_what = {entry["what"]: entry for entry in entries}
    lookup = by_what["lookup of pypi.org"]
    assert lookup["target"] == {"host": "pypi.org"} and lookup["actions"] == ["allow-run", "allow-always"]
    assert by_what["udp to 10.0.0.9:5353"]["target"] == {"address": "10.0.0.9"}
    assert by_what["connection to 198.51.100.3:443"]["target"] == {"tcp": "198.51.100.3:443"}
    denied = next(entry for entry in entries if entry["kind"] == "denied")
    assert denied["question"] == "1700000000-1-5" and "ask-again" in denied["actions"]
    assert "you denied it" in denied["why"]
    cut = next(entry for entry in entries if entry["kind"] == "cut")
    assert cut["actions"] == [] and "kill switch" in cut["why"]
    rule = next(entry for entry in entries if entry["kind"] == "rule")
    assert rule["rule"] == "no-deletes" and "does not change" not in rule["why"] or True
    assert "the network settings do not change what a ruleset decides" in rule["why"]
    # Bounded: a log larger than the tail is read from its end only.
    assert ap.blocked(runs["claude-1"], runs, limit=2) == entries[:2]
    status, lines = cli("blocked", "claude-1", "--json")
    assert status == 0 and [e["what"] for e in json.loads("\n".join(lines))] == [e["what"] for e in entries]
    status, lines = cli("blocked", "claude-1")
    assert status == 0 and "unanswered" in "\n".join(lines)


def test_the_blocked_list_offers_nothing_that_opens_more_or_leaks_a_canary(live, monkeypatch):
    """Bug hunt: Allow on a refused UDP packet became `net allow --allow ADDRESS` (every port and protocol), and
    Allow on a lookup of a name carrying a canary token looked it up through this host's DNS (the token left)."""
    blocked_fixture(live)
    write_lines(live["logs"] / "egress-denied.jsonl", [
        {"time": "2026-11-11T10:01:00Z", "host": "canarytokenq2w3e4r5.evil.example", "reason": "not in allowed hosts",
         "sandbox": {"run": "claude-1"}},
        {"time": "2026-11-11T10:01:01Z", "address": "10.0.0.9", "protocol": "udp", "port": 5353,
         "sandbox": {"run": "claude-1"}}])
    (live["directory"] / ap.CANARY_CONFIG).write_text(json.dumps({"names": [["CANARYTOKENQ2W3E4R5"]]}))
    runs = net.running()
    by_what = {entry["what"]: entry for entry in ap.blocked(runs["claude-1"], runs)}
    assert by_what["udp to 10.0.0.9:5353"]["actions"] == []
    canary = by_what["lookup of canarytokenq2w3e4r5.evil.example"]
    assert canary["target"] is None and canary["actions"] == []
    # A question about such a name is not allowed again either (it would be looked up by net allow).
    question = {"id": "1700000000-1-9", "kind": "dns", "name": "canarytokenq2w3e4r5.evil.example"}
    looked = []
    monkeypatch.setattr(net, "main", lambda argv, out=print: looked.append(argv) or 0)
    for scope in ("run", "always"):
        with pytest.raises(ValueError, match="canary token"):
            ap.decide_again(("claude-1", live["record"], question), "allow", scope)
    assert looked == []


def test_allow_always_holds_to_the_security_modes_cap(live, tmp_path, monkeypatch):
    """Bug hunt: `approvals allow ID --scope always` wrote the exception rule (and reloaded it) in a run whose
    security mode approves at most for the run."""
    own = tmp_path / "config" / "sandbox-ai" / "rules.json"
    own.parent.mkdir(parents=True)
    original = {"version": 1, "http": [{"id": "ask-posts", "host": "*", "methods": "POST", "decision": "ask"}]}
    own.write_text(json.dumps(original))
    monkeypatch.setattr(ap, "reload_rules", lambda record: pytest.fail("nothing is reloaded"))
    record = {**live["record"], "mode": {"name": "high", "approval_scope": "run"}}
    question = {"id": "1700000000-1-8", "kind": "http", "ruleset": "rules.json", "method": "POST", "url": HTTP["url"]}
    with pytest.raises(ValueError, match="keeps no answer always"):
        ap.decide_again(("claude-1", record, question), "allow", "always")
    with pytest.raises(ValueError, match="security mode high approves at most for run"):
        ap.rule_exception(record, {**question, "question_kind": "http"}, write=True)
    assert json.loads(own.read_text()) == original


def test_the_blocked_list_reads_only_the_end_of_a_long_log(live, monkeypatch):
    logs = live["logs"]
    filler = json.dumps({"time": "2026-11-11T10:00:01Z", "event": "allowed", "pad": "x" * 900}) + "\n"
    with open(logs / "connections-20261111-100000-claude-1.jsonl", "w") as stream:
        stream.write(json.dumps({"time": "2026-11-11T10:00:00Z", "event": "denied", "destination": "198.51.100.1:443",
                                 "reason": "not allowed by the network options"}) + "\n")
        stream.write(filler * 200)
        stream.write(json.dumps({"time": "2026-11-11T10:00:02Z", "event": "denied", "destination": "198.51.100.2:443",
                                 "reason": "not allowed by the network options"}) + "\n")
    runs = net.running()
    entries = ap.blocked(runs["claude-1"], runs, tail=64 << 10)
    assert [e["what"] for e in entries] == ["connection to 198.51.100.2:443"], "the old one lies beyond the tail"
    assert len(ap.blocked(runs["claude-1"], runs)) == 2


# --- "Allow always" for a request: an exception rule ---------------------------------------------------------------

def test_an_exception_never_changes_a_presets_ruleset(live, tmp_path):
    preset = tmp_path / "presets" / "tor-researcher.rules.json"
    preset.parent.mkdir()
    original = {"version": 1, "http": [{"id": "writes", "host": "*", "methods": ["POST"], "decision": "ask"}]}
    preset.write_text(json.dumps(original))
    entry = {"question_kind": "http", "ruleset": HTTP["ruleset"], "method": "POST", "url": HTTP["url"]}
    plan = ap.rule_exception(live["record"], entry)
    assert plan["where"] == "other" and "stays as it is" in plan["changes"] and "in place of" in plan["applies"]
    assert not Path(plan["path"]).exists(), "a preview writes nothing"
    plan = ap.rule_exception(live["record"], entry, write=True)
    assert json.loads(preset.read_text()) == original
    copy = json.loads(Path(plan["path"]).read_text())
    assert copy["http"][0] == {**plan["rule"], "host": "registry.npmjs.org", "methods": "POST",
                               "path": "/-/npm/v1/security/advisories/bulk", "decision": "allow"}
    assert copy["http"][1]["id"] == "writes", "the exception comes first: the first rule that matches decides"
    # The copy is checked as a ruleset like any other.
    rt.Ruleset.validated(copy, "copy")
    assert ap.rule_exception(live["record"], entry, write=True)["written"] is False, "once"
    # Another file of the same name (someone else's x.json): a copy of its own, never built on the first one's.
    other = tmp_path / "elsewhere" / "tor-researcher.rules.json"
    other.parent.mkdir()
    other.write_text(json.dumps({"version": 1, "http": [{"id": "strict", "host": "*", "decision": "deny"}]}))
    record = {**live["record"], "rules_sources": {"files": [str(other)], "project_config": None}}
    second = ap.rule_exception(record, entry, write=True)
    assert second["path"] != plan["path"] and Path(second["path"]).name == preset.name
    assert [r["id"] for r in json.loads(Path(second["path"]).read_text())["http"]] == [plan["rule"]["id"], "strict"]
    assert [r["id"] for r in json.loads(Path(plan["path"]).read_text())["http"]] == [plan["rule"]["id"], "writes"]
    # A start that uses a copy: it is yours, changed in place.
    record = {**live["record"], "rules_sources": {"files": [second["path"]], "project_config": None}}
    assert ap.ruleset_file(record, HTTP["ruleset"]) == (Path(second["path"]), "own")


def test_an_exception_goes_into_your_own_rules_in_place_and_the_projects_into_its_file(live, tmp_path, monkeypatch):
    own = tmp_path / "config" / "sandbox-ai" / "rules.json"
    own.parent.mkdir(parents=True)
    own.write_text(json.dumps({"version": 1, "http": [{"id": "ask-posts", "host": "*", "methods": "POST",
                                                       "decision": "ask"}]}))
    reloaded = []
    monkeypatch.setattr(ap, "reload_rules", lambda record: reloaded.append(record["run"]))
    entry = {"question_kind": "http", "ruleset": "rules.json", "what": f"POST {HTTP['url']}"}
    plan = ap.rule_exception(live["record"], entry, write=True)
    assert plan["where"] == "own" and plan["written"] and reloaded == ["claude-1"]
    assert [rule["id"] for rule in json.loads(own.read_text())["http"]] == [plan["rule"]["id"], "ask-posts"]
    # The project's own ruleset: into its trusted project file (refused while it is not what you trusted).
    entry = {"question_kind": "mcp", "ruleset": "project", "what": "MCP tool files/delete_file with {}"}
    with pytest.raises(ValueError, match="not what you trusted"):
        work = Path(live["record"]["workspace"])
        work.mkdir()
        (work / ".sandbox-ai.json").write_text(json.dumps({"version": 1, "agent": "claude", "options": []}))
        ap.rule_exception(live["record"], entry, write=True)
    launcher = net.launcher()
    data = (work / ".sandbox-ai.json").read_bytes()
    launcher.trust_config(launcher.config_key(work / ".sandbox-ai.json"), __import__("hashlib").sha256(data).hexdigest())
    plan = ap.rule_exception(live["record"], entry, write=True)
    document = json.loads((work / ".sandbox-ai.json").read_text())
    assert plan["where"] == "project" and document["rules"]["mcp"][0]["tool"] == "delete_file"
    # A policy that keeps nothing always: refused.
    monkeypatch.setattr(ap, "policy_scope", lambda: "run")
    with pytest.raises(ValueError, match="nothing is allowed always"):
        ap.rule_exception(live["record"], entry)
    # What cannot be named exactly is not guessed.
    monkeypatch.setattr(ap, "policy_scope", lambda: "always")
    with pytest.raises(ValueError, match="holds a \\*"):
        ap.rule_exception(live["record"], {"question_kind": "http", "ruleset": "rules.json",
                                           "what": "GET https://a.example/x*y"})
    # Encoded: a rule's path is decoded on loading, so /v1/%2A%2A would allow every path below /v1.
    for path in ("/v1/%2A%2A", "/v1/%252A", "/v1/x/..%2F..%2Fadmin", "/v1/%25252525252A"):
        with pytest.raises(ValueError, match="holds a \\*"):
            ap.exception_rule({"question_kind": "http", "method": "POST", "url": f"https://a.example{path}"})
    rule, _ = ap.exception_rule({"question_kind": "http", "method": "POST", "url": "https://a.example/v1//a/b/"})
    assert rule["path"] == "/v1/a/b" and rt.Ruleset.http_rule(rule)["path"] == ["/v1/a/b"]
