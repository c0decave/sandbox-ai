"""Approval questions for names that do not exist (user 2026-11-20: `sandbox-ai approvals allow` on a lookup of
proxy.example said "proxy.example has no address ...: there is nothing to allow ... Deny it, or leave it" - "und doch
gibt es das als Freigabe-Frage, wie gehen wir damit um?"). A name is never looked up on the host before the human
said yes (a lookup is itself a way out), so:

1. a reserved name that never exists (RFC 2606/6761: *.example, *.invalid, *.test, *.localhost - not example.com) is
   never asked about: the sandbox refuses its lookup at once, logs it with why, and the Blocked list says so;
   a canary token in such a name still raises its alarm first;
2. any other name the human allows that turns out not to exist (no such name, no address) closes its question:
   denied for the run, said in one line, exit status 0, nothing opened - a lookup that failed for now (EAI_AGAIN, a
   timeout, offline) keeps the question open and says "try again";
3. "allow --scope always" for such a name keeps nothing for the project."""
import importlib.util
import json
import os
from pathlib import Path
import socket
import struct
import sys
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


import test_approvals_rework as rework  # noqa: E402 - its live run (a real broker and control socket)
from test_approvals_rework import live  # noqa: E402,F401 - the fixture
from live_fakes import Host  # noqa: E402

net, rt, ap = rework.net, rework.rt, rework.ap
canary = load("sandbox_canary_no_address", ROOT / "sandbox/canary.py")
safeguards = load("sandbox_safeguards_no_address", ROOT / "sandbox/safeguards.py")
logs = load("sandbox_logs_no_address", ROOT / "sandbox/logs.py")
vpn = load("sandbox_vpn_no_address", ROOT / "sandbox/vpn.py")
vpn_runtime = load("sandbox_vpn_runtime_no_address", ROOT / "sandbox/vpn_runtime.py")

NONAME = socket.gaierror(socket.EAI_NONAME, "Name or service not known")
NODATA = socket.gaierror(getattr(socket, "EAI_NODATA", -5), "No address associated with hostname")
AGAIN = socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")


def stamp(seconds=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if seconds is None else seconds))


# --- 1. reserved names ----------------------------------------------------------------------------------------------

def test_reserved_names_are_the_special_use_domains_and_not_the_example_domains_that_resolve():
    for name in ("proxy.example", "PROXY.Example.", "example", "a.b.invalid", "collect.r5.test", "test",
                 "app.localhost", "localhost"):
        assert rt.reserved_name(name) and ap.reserved_name(name), name
    for name in ("example.com", "proxy.example.org", "example.net", "testing.org", "latest", "my-test", "invalid.io",
                 "localhost.example.com", "pypi.org"):
        assert not rt.reserved_name(name) and not ap.reserved_name(name), name
    assert rt.RESERVED_NAMES == ap.RESERVED_NAMES, "the sandbox's side and the host's side refuse the same names"


def test_the_supervisor_refuses_a_reserved_name_without_a_question(tmp_path):
    made = rework.broker(tmp_path)
    refused = made.ask("dns", {"name": "proxy.example"}, wait=False)
    assert refused["decision"] == "deny" and refused["reason"] == rt.RESERVED_REASON
    assert "never exists" in refused["reason"]
    assert rework.questions_in(tmp_path / "approvals") == [], "nothing asked"
    assert made.counter == 0, "it uses up none of the run's questions"
    # Any other name is asked as before.
    assert made.ask("dns", {"name": "proxy.example.org"}, wait=False) is None
    assert len(rework.questions_in(tmp_path / "approvals")) == 1


class Asks:
    def __init__(self):
        self.asked = []

    def ask(self, kind, details, wait=True, late=None):
        self.asked.append((kind, details["name"]))


def lookup(port, host):
    question = b"".join(bytes([len(p)]) + p.encode() for p in host.split(".")) + b"\0\0\1\0\1"
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
        client.settimeout(2)
        client.sendto(struct.pack("!HHHHHH", 7, 0x100, 1, 0, 0, 0) + question, ("127.0.0.1", port))
        return client.recv(512)


def test_the_dns_stub_raises_a_canary_alarm_before_the_reserved_name_shortcut(tmp_path, monkeypatch):
    """A canary token in a reserved name (the research's exfiltration hosts are *.test, e.g. collect.r5.test) still
    raises the alarm, a plain one is still logged as a refused lookup (what the research counts), and neither is
    asked about; any other name is asked as before."""
    token = canary.github_token()
    control = tmp_path / "control"
    control.mkdir()
    (control / rt.CANARY_CONFIG).write_text(json.dumps({"files": [], "names": [
        [text, "gh", e] for text, e in canary.name_forms(token).items()]}))
    canaries = rt.Canaries.load(control)
    monkeypatch.setattr(rt, "LOG_DIR", tmp_path)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    stop, asks = threading.Event(), Asks()
    thread = threading.Thread(target=rt.dns_logger, args=(set(), port, stop, asks, (), None, canaries), daemon=True)
    thread.start()
    time.sleep(0.2)
    hexed = token.encode().hex()
    try:
        assert lookup(port, f"{hexed[:60]}.{hexed[60:]}.collect.r5.test")[3] & 0xF == 3
        assert lookup(port, "collect.r5.test")[3] & 0xF == 3
        assert lookup(port, "proxy.example")[3] & 0xF == 3
        assert lookup(port, "pypi.org")[3] & 0xF == 3
        found = []
        deadline = time.monotonic() + 3
        while not found and time.monotonic() < deadline:
            path = control / rt.CANARY_EVENTS
            found = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
            time.sleep(0.05)
        assert [(e["event"], e["source"], e["canary"]) for e in found] == [("send", "dns", "gh")]
    finally:
        stop.set()
        thread.join(3)
    assert asks.asked == [("dns", "pypi.org")], "a reserved name is never asked about"
    logged = [json.loads(line) for line in (tmp_path / "egress-denied.jsonl").read_text().splitlines()]
    by_host = {entry["host"]: entry for entry in logged}
    assert set(by_host) == {f"{hexed[:60]}.{hexed[60:]}.collect.r5.test", "collect.r5.test", "proxy.example",
                            "pypi.org"}
    assert by_host["collect.r5.test"]["reserved"] is True and by_host["collect.r5.test"]["reason"] == rt.RESERVED_REASON
    assert by_host["pypi.org"]["reason"] == "not in allowed hosts" and "reserved" not in by_host["pypi.org"]


def test_a_reserved_name_below_an_allowed_wildcard_is_refused_not_held(tmp_path, monkeypatch):
    """*.r5.test allowed: the host would look such a name up by itself - it is refused here instead."""
    monkeypatch.setattr(rt, "LOG_DIR", tmp_path)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    stop, asks = threading.Event(), Asks()
    thread = threading.Thread(target=rt.dns_logger, args=(set(), port, stop, asks), daemon=True,
                              kwargs={"wildcards": lambda: {"r5.test"}})
    thread.start()
    time.sleep(0.2)
    try:
        assert lookup(port, "collect.r5.test")[3] & 0xF == 3
    finally:
        stop.set()
        thread.join(3)
    assert asks.asked == []
    assert json.loads((tmp_path / "egress-denied.jsonl").read_text())["reserved"] is True


def test_a_reserved_lookup_is_in_the_blocked_list_and_the_report_without_an_allow(tmp_path):
    """The Blocked list: why in plain words, nothing to allow (no target, no actions); the report's line and the
    log summary offer no --allow-host for it."""
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    (tmp_path / "approvals").mkdir()
    (logs_dir / "egress-denied.jsonl").write_text(
        json.dumps({"time": stamp(), "host": "collect.r5.test", "reason": rt.RESERVED_REASON, "reserved": True}) + "\n"
        + json.dumps({"time": stamp(), "host": "pypi.org", "reason": "not in allowed hosts"}) + "\n")
    record = {"run": "r1", "directory": str(tmp_path), "log_dir": str(logs_dir), "workspace": str(tmp_path)}
    entries = {e["what"]: e for e in ap.blocked(record, runs={})}
    reserved = entries["lookup of collect.r5.test"]
    assert reserved["target"] is None and reserved["actions"] == []
    assert "never exists" in reserved["why"] and "reserved" in reserved["why"]
    assert entries["lookup of pypi.org"]["actions"], "any other refused name is still offered"
    found = safeguards.from_egress({"host": "collect.r5.test", "reason": rt.RESERVED_REASON, "reserved": True},
                                   "r1", None)
    assert "--allow-host" not in json.dumps(found) and "never exists" in json.dumps(found)
    key, option, why = logs.suggestion({"host": "collect.r5.test", "reserved": True})
    assert option is None and "never exists" in why


# --- 2. a name allowed that does not exist closes its question -----------------------------------------------------

@pytest.fixture
def asked(tmp_path, monkeypatch):
    """One run with an open lookup question for proxy.example.org (as listed by approvals.questions)."""
    record = {"run": "r1", "directory": str(tmp_path), "workspace": str(tmp_path / "work")}
    (tmp_path / "approvals").mkdir()
    (tmp_path / "work").mkdir()
    monkeypatch.setattr(ap, "POLICY_FILE", tmp_path / "no-policy.json")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    pending = {}

    def add(ident, name):
        question = {"id": ident, "kind": "dns", "name": name, "time": stamp(), "timeout": 60}
        (tmp_path / "approvals" / f"{ident}.json").write_text(json.dumps(question))
        pending[ident] = question
    add("1-1-1", "proxy.example.org")
    monkeypatch.setattr(ap, "questions", lambda runs=None, answered=False, late=False: [
        ("r1", record, q) for ident, q in pending.items()
        if not (tmp_path / "approvals" / f"{ident}.answer").exists()])
    monkeypatch.setattr(ap.net(), "update_hosts", lambda *a, **k: pytest.fail("nothing is pinned"))
    return {"dir": tmp_path / "approvals", "record": record, "add": add}


def raising(error):
    def resolver(name):
        raise error
    return resolver


@pytest.mark.parametrize("error, fact", [(NONAME, "does not exist (Name or service not known)"),
                                         (NODATA, "has no address (No address associated with hostname)")])
def test_allowing_a_name_that_does_not_exist_closes_its_question(asked, error, fact):
    lines = []
    reply = ap.answer("1-1-1", "allow", "run", resolver=raising(error), out=lines.append)
    assert reply["decision"] == "deny" and reply["scope"] == "run"
    written = json.loads((asked["dir"] / "1-1-1.answer").read_text())
    assert written["decision"] == "deny" and written["scope"] == "run" and fact in written["why"]
    text = "\n".join(lines)
    assert f"{ap.CLOSED_NOTE}proxy.example.org {fact}" in text, text
    assert "nothing was opened" in text and "ask-again 1-1-1" in text
    assert ap.closed_note(text).startswith(f"proxy.example.org {fact}")
    assert "Deny it" not in text and "Errno" not in text


def test_allow_always_for_a_name_that_does_not_exist_keeps_nothing_for_the_project(asked):
    lines = []
    ap.answer("1-1-1", "allow", "always", resolver=raising(NONAME), out=lines.append)
    assert "not remembered for the project" in "\n".join(lines)
    assert ap.remembered() == {}, "not pinned for the project"
    assert json.loads((asked["dir"] / "1-1-1.answer").read_text())["scope"] == "run"


@pytest.mark.parametrize("error", [AGAIN, socket.timeout("timed out"), OSError("the VPN's resolver: no answer")])
def test_a_lookup_that_failed_for_now_keeps_the_question_open_and_says_try_again(asked, error):
    with pytest.raises(ValueError) as refused:
        ap.answer("1-1-1", "allow", "run", resolver=raising(error), out=lambda line: None)
    text = str(refused.value)
    assert "proxy.example.org could not be looked up" in text and "try again" in text, text
    assert "the question stays open" in text and "Deny it" not in text
    assert not (asked["dir"] / "1-1-1.answer").exists(), "still open"


def test_a_reserved_name_asked_by_an_older_sandbox_is_closed_without_a_lookup(asked):
    asked["add"]("1-1-2", "proxy.example")
    lines = []
    reply = ap.answer("1-1-2", "allow", "once", resolver=lambda name: pytest.fail(f"{name} was looked up"),
                      out=lines.append)
    assert reply["decision"] == "deny" and reply["scope"] == "run"
    assert f"{ap.CLOSED_NOTE}proxy.example is a reserved name" in "\n".join(lines)
    assert "never exists" in json.loads((asked["dir"] / "1-1-2.answer").read_text())["why"]


def test_a_canary_name_is_still_refused_before_any_lookup(asked, monkeypatch):
    monkeypatch.setattr(ap, "canary_in_name", lambda record, name: True)
    with pytest.raises(ValueError, match="canary token"):
        ap.answer("1-1-1", "allow", "run", resolver=lambda name: pytest.fail("looked up"), out=lambda line: None)


def test_the_vpn_resolver_tells_a_name_that_does_not_exist_from_a_failure(tmp_path, monkeypatch):
    """Through the run's VPN: NXDOMAIN or no address is a definite answer (the container says no_address), a
    resolver that did not answer is not."""
    class Done:
        def __init__(self, out):
            self.stdout, self.stderr = out, ""
    record = {"vpn_container": "sandbox-ai-vpn-1"}
    gone = vpn.resolver_for_run(record, run=lambda *a, **k: Done(json.dumps({
        "error": "Cannot resolve allowed host gone.example.org through the VPN", "no_address": True})))
    with pytest.raises(socket.gaierror) as raised:
        gone("gone.example.org")
    assert raised.value.errno == socket.EAI_NONAME
    # (a container started by an older version says it in words only)
    older = vpn.resolver_for_run(record, run=lambda *a, **k: Done(json.dumps({
        "error": "Cannot resolve allowed host gone.example.org through the VPN"})))
    with pytest.raises(socket.gaierror):
        older("gone.example.org")
    failed = vpn.resolver_for_run(record, run=lambda *a, **k: Done(json.dumps({"error": "10.0.0.1: timed out"})))
    with pytest.raises(OSError) as raised:
        failed("gone.example.org")
    assert not isinstance(raised.value, socket.gaierror)
    # The container's side: resolve_hosts' "no address" is marked as such.
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"dns": ["10.0.0.1"]}))

    def nothing(names, tunnel, tailnet=False, lookup=None):
        raise vpn_runtime.NoAddress("Cannot resolve allowed host gone.example.org through the VPN")
    monkeypatch.setattr(vpn_runtime, "resolve_hosts", nothing)
    monkeypatch.setattr(vpn_runtime, "use_check_mark", lambda: None)
    out = []

    class Out:
        write = out.append
    import io
    assert vpn_runtime.resolve_command(io.StringIO('["gone.example.org"]'), Out, path=state) == 1
    assert json.loads(out[0])["no_address"] is True


def test_resolve_hosts_raises_no_address_only_when_every_family_had_none():
    tunnel = {"dns": ["10.0.0.1"], "ipv4": True}
    with pytest.raises(vpn_runtime.NoAddress):
        vpn_runtime.resolve_hosts(["gone.example.org"], tunnel, lookup=lambda name, server, qtype: [])

    def failing(name, server, qtype):
        raise TimeoutError("no answer from 10.0.0.1")
    with pytest.raises(OSError):
        vpn_runtime.resolve_hosts(["gone.example.org"], tunnel, lookup=failing)


def test_the_supervisor_keeps_why_a_lookup_was_closed_for_the_blocked_list(tmp_path):
    made = rework.broker(tmp_path, timeout=5)
    made.ask("dns", {"name": "gone.example.org"}, wait=False)
    [path] = rework.questions_in(tmp_path / "approvals")
    ident = json.loads(path.read_text())["id"]
    why = "gone.example.org does not exist (Name or service not known)"
    rework.answer_file(tmp_path / "approvals", ident, "deny", "run", why=why)
    assert rework.wait_for(lambda: path.with_suffix(".done").exists())
    done = json.loads(path.with_suffix(".done").read_text())
    assert done["decision"] == "deny" and done["reason"] == "answered" and done["why"] == why
    assert made.ask("dns", {"name": "gone.example.org"}, wait=False)["decision"] == "deny", "for the run"
    assert len(rework.questions_in(tmp_path / "approvals")) == 1, "not asked again"
    record = {"run": "r1", "directory": str(tmp_path), "workspace": str(tmp_path)}
    [entry] = ap.blocked(record, runs={})
    assert entry["kind"] == "denied" and why in entry["why"] and "ask-again" in entry["actions"]
    assert f"sandbox-ai approvals ask-again {ident}" in entry["why"]


# --- end to end: ./sandbox-ai with a fake resolver, a real broker and control socket -------------------------------

def test_e2e_allow_of_a_name_that_does_not_exist_closes_the_question(live, tmp_path):
    host = Host(tmp_path / "host")
    looked_up = host.fake_resolver({"flaky.example.org": "AGAIN"})
    env = {"XDG_RUNTIME_DIR": str(live["runtime"])}
    made = live["broker"]
    made.timeout = 30
    made.ask("dns", {"name": "gone.example.org"}, wait=False)
    made.ask("dns", {"name": "flaky.example.org"}, wait=False)
    # A reserved name: not asked at all.
    assert made.ask("dns", {"name": "proxy.example"}, wait=False)["reason"] == rt.RESERVED_REASON
    listed = host.run("approvals", "list", "--json", env=env, timeout=60)
    assert listed.returncode == 0, listed.stderr
    names = ("gone.example.org", "flaky.example.org")
    ids = {next(n for n in names if n in q["question"]): q["id"] for q in json.loads(listed.stdout)}
    assert set(ids) == set(names)
    # Allowed (always): it does not exist - the question is closed, said in one line, exit status 0.
    done = host.run("approvals", "allow", ids["gone.example.org"], "--scope", "always", env=env, timeout=90)
    assert done.returncode == 0, done.stderr
    assert "denied for the run: gone.example.org does not exist (Name or service not known)" in done.stdout
    assert "not remembered for the project" in done.stdout
    assert rework.wait_for(lambda: (live["approvals"] / f"{ids['gone.example.org']}.done").exists())
    assert made.ask("dns", {"name": "gone.example.org"}, wait=False)["decision"] == "deny"
    remembered = host.run("approvals", "remembered", "--json", env=env, timeout=60)
    assert json.loads(remembered.stdout) == {}, "nothing pinned for the project"
    # A lookup that failed for now: the question stays open, and it says try again.
    flaky = host.run("approvals", "allow", ids["flaky.example.org"], env=env, timeout=90)
    assert flaky.returncode == 1 and "try again" in flaky.stderr and "Deny it" not in flaky.stderr, flaky.stderr
    listed = host.run("approvals", "list", "--json", env=env, timeout=60)
    assert [q["id"] for q in json.loads(listed.stdout)] == [ids["flaky.example.org"]]
    closed = host.run("approvals", "list", "--state", "refused", "--json", env=env, timeout=60)
    [entry] = json.loads(closed.stdout)
    assert entry["id"] == ids["gone.example.org"] and "does not exist" in entry["result"]["why"]
    assert looked_up.read_text().split() == ["gone.example.org", "flaky.example.org"], "nothing else looked up"
    # The Blocked list: why, and the way back (ask again, e.g. once the name exists).
    blocked = host.run("approvals", "blocked", "--json", env=env, timeout=60)
    assert blocked.returncode == 0, blocked.stderr
    [entry] = [e for e in json.loads(blocked.stdout) if e.get("question") == ids["gone.example.org"]]
    assert entry["kind"] == "denied" and "does not exist" in entry["why"] and "ask-again" in entry["actions"]
    again = host.run("approvals", "ask-again", ids["gone.example.org"], env=env, timeout=60)
    assert again.returncode == 0, again.stderr
    assert made.ask("dns", {"name": "gone.example.org"}, wait=False) is None, "asked anew"


def test_e2e_a_reserved_name_an_older_sandbox_asked_about_is_closed_without_a_lookup(live, tmp_path):
    host = Host(tmp_path / "host")
    looked_up = host.fake_resolver({})
    env = {"XDG_RUNTIME_DIR": str(live["runtime"])}
    ident = "1763600000-1-1"
    (live["approvals"] / f"{ident}.json").write_text(json.dumps({"id": ident, "kind": "dns", "name": "proxy.example",
                                                                 "time": stamp(), "timeout": 60}))
    done = host.run("approvals", "allow", ident, env=env, timeout=90)
    assert done.returncode == 0, done.stderr
    assert "denied for the run: proxy.example is a reserved name" in done.stdout and "never exists" in done.stdout
    assert looked_up.read_text() == "", "never looked up"
    assert json.loads((live["approvals"] / f"{ident}.answer").read_text())["decision"] == "deny"


def test_the_desktop_notifications_button_says_the_question_was_closed_or_to_try_again(monkeypatch):
    """The launcher's own notification (a run in a terminal, no app): its Allow button closes the question of a name
    that does not exist and says so in one more notification; a lookup that failed for now says try again."""
    monkeypatch.setattr(ap, "policy_scope", lambda: "always")
    monkeypatch.setattr(ap, "shows", lambda alarm=False: True)
    shown = []
    monkeypatch.setattr(ap, "desktop_notification", lambda title, text, **kw: shown.append((title, text)))

    def runner(command, **kwargs):
        return type("Done", (), {"stdout": "once\n"})()

    def closing(ident, decision, scope, out):
        out("r1: deny (run): lookup of gone.example.org\n  " + ap.CLOSED_NOTE + "gone.example.org does not exist "
            "(Name or service not known) - nothing was opened and the question is closed")
    assert ap.desktop_question("sandbox-ai asks: r1", "lookup of gone.example.org", "1-1-1", runner=runner,
                               answer_with=closing) == "once"
    assert shown == [("sandbox-ai asks: r1", "Closed, not allowed: gone.example.org does not exist (Name or service "
                                             "not known) - nothing was opened and the question is closed")]

    def failing(ident, decision, scope, out):
        raise ValueError("flaky.example.org could not be looked up (on this host: Temporary failure in name "
                         "resolution): nothing was allowed, the question stays open - try again")
    shown.clear()
    ap.desktop_question("sandbox-ai asks: r1", "lookup of flaky.example.org", "1-1-2", runner=runner,
                        answer_with=failing)
    assert shown[0][0] == "Not answered: sandbox-ai asks: r1" and "try again" in shown[0][1]
