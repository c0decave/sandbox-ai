"""The approvals rework (user 2026-11-17: "dieser 60s timeout fuer freigaben nervt total, weil man es auch nicht
rueckgaengig machen kann"): a question nobody answered in time is no refusal of its own - it stays open (state
"timed out") and answerable; a later answer counts for the next try of the same request (a name: pinned for the run
at once); nothing is refused at once for not being answered; the wait is --ask-timeout SECONDS; every answer can be
changed from the list (`approvals list --state ...`, allow|deny|ask-again ID on any entry)."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sandbox_net_rework", ROOT / "sandbox/net.py")
net = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(net)
rt = net.launcher().RUNTIME
SPEC = importlib.util.spec_from_file_location("sandbox_approvals_rework", ROOT / "sandbox/approvals.py")
ap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ap)

HTTP = {"rule": "writes", "ruleset": "--rules tor-researcher.rules.json", "method": "POST",
        "host": "registry.npmjs.org", "path": "/-/npm/v1/security/advisories/bulk",
        "url": "https://registry.npmjs.org/-/npm/v1/security/advisories/bulk",
        "reason": "POST /-/npm/v1/security/advisories/bulk on registry.npmjs.org is asked by rule writes"}
CONNECT = {"destination": "192.0.2.7:8443", "address": "192.0.2.7", "port": 8443, "protocol": "tls"}
DNS = {"name": "pypi.org"}


def wait_for(condition, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


class Network(rt.RuntimeNetwork):
    """The run's network, its changes recorded (allow_once and change)."""

    def __init__(self):
        super().__init__({}, None, lambda state: None)
        self.changes = []

    def allow_once(self, address, port, timer=threading.Timer):
        self.changes.append(("once", address, int(port)))

    def change(self, request, ssh=True):
        self.changes.append(("change", json.dumps(request, sort_keys=True)))
        return {"ok": True}


def broker(tmp_path, **kwargs):
    Path(tmp_path).mkdir(parents=True, exist_ok=True)
    return rt.ApprovalBroker(tmp_path / "approvals", kwargs.pop("network", None) or Network(),
                             **{"timeout": 0.3, "poll": 0.02, **kwargs})


def questions_in(directory):
    return sorted(Path(directory).glob("*.json"))


def answer_file(directory, ident, decision, scope, **extra):
    (Path(directory) / f"{ident}.answer").write_text(json.dumps({"decision": decision, "scope": scope, **extra}))


# --- the supervisor: a question that timed out stays answerable --------------------------------------------------

def test_a_timed_out_question_stays_open_and_its_refusal_says_the_human_may_still_allow_it(tmp_path):
    made = broker(tmp_path)
    first = made.ask("http", HTTP)
    assert first["decision"] == "deny" and first["scope"] == "once"
    assert first["reason"] == "not answered yet - the human may still allow it; retry later"
    [path] = questions_in(tmp_path / "approvals")
    ident = json.loads(path.read_text())["id"]
    assert first["question"] == ident and f"sandbox-ai approvals allow {ident}" in first["why"]
    assert "stays open" in first["why"] and "refused without asking" not in first["why"]
    # No decision was written: the question is still answerable, marked "timed out" for the lists.
    assert not path.with_suffix(".done").exists()
    assert json.loads(path.with_suffix(".state").read_text())["state"] == "timeout"
    assert not made.decided and not made.pending


def test_the_next_try_waits_for_the_same_question_instead_of_being_refused_at_once(tmp_path):
    made = broker(tmp_path, timeout=0.4)
    made.ask("http", HTTP)
    [path] = questions_in(tmp_path / "approvals")
    ident = json.loads(path.read_text())["id"]
    results = []
    thread = threading.Thread(target=lambda: results.append(made.ask("http", HTTP)))
    started = time.monotonic()
    thread.start()
    # It waits (no instant refusal) and asks no second question: the same one is open again.
    assert wait_for(lambda: json.loads(path.with_suffix(".state").read_text())["state"] == "open", 2)
    assert not results and len(questions_in(tmp_path / "approvals")) == 1
    answer_file(tmp_path / "approvals", ident, "allow", "once")
    thread.join(5)
    assert results[0]["decision"] == "allow" and time.monotonic() - started < 2
    assert json.loads(path.with_suffix(".done").read_text())["decision"] == "allow"


def test_a_late_allow_for_the_run_is_applied_at_once_and_the_next_try_passes(tmp_path):
    made = broker(tmp_path)
    assert made.ask("connect", CONNECT)["decision"] == "deny"
    [path] = questions_in(tmp_path / "approvals")
    ident = json.loads(path.read_text())["id"]
    answer_file(tmp_path / "approvals", ident, "allow", "run")
    # Applied without a new try: the firewall is opened, the decision kept for the run, the .done written.
    assert wait_for(lambda: path.with_suffix(".done").exists())
    assert ("change", json.dumps({"op": "allow", "tcp": [["192.0.2.7", 8443]]}, sort_keys=True)) in \
        made.network.changes
    started = time.monotonic()
    assert made.ask("connect", CONNECT)["decision"] == "allow" and time.monotonic() - started < 0.2


def test_a_late_allow_once_is_kept_for_the_next_try_and_used_up_by_it(tmp_path):
    made = broker(tmp_path)
    made.ask("connect", CONNECT)
    [path] = questions_in(tmp_path / "approvals")
    ident = json.loads(path.read_text())["id"]
    answer_file(tmp_path / "approvals", ident, "allow", "once")
    assert wait_for(lambda: path.with_suffix(".done").exists())
    assert made.network.changes == [], "nothing opened before the next try: once is for that try"
    allowed = made.ask("connect", CONNECT)
    assert allowed["decision"] == "allow" and ("once", "192.0.2.7", 8443) in made.network.changes
    # Used up: the try after that is asked anew (a new question).
    made.ask("connect", CONNECT)
    assert len(questions_in(tmp_path / "approvals")) == 2


def test_a_late_allow_of_a_lookup_opens_its_addresses_for_the_run(tmp_path):
    made = broker(tmp_path)
    made.ask("dns", DNS)
    [path] = questions_in(tmp_path / "approvals")
    ident = json.loads(path.read_text())["id"]
    # (the host pinned the name: approvals.answer; the supervisor opens its public addresses)
    answer_file(tmp_path / "approvals", ident, "allow", "run", addresses=["151.101.0.223"])
    assert wait_for(lambda: path.with_suffix(".done").exists())
    assert ("change", json.dumps({"op": "allow", "https": ["151.101.0.223"]}, sort_keys=True)) in made.network.changes


def test_a_late_deny_once_leaves_the_next_try_to_a_new_question_and_for_the_run_is_kept(tmp_path):
    made = broker(tmp_path)
    made.ask("http", HTTP)
    ident = json.loads(questions_in(tmp_path / "approvals")[0].read_text())["id"]
    answer_file(tmp_path / "approvals", ident, "deny", "run")
    assert wait_for(lambda: (tmp_path / "approvals" / f"{ident}.done").exists())
    denied = made.ask("http", HTTP)
    assert denied["decision"] == "deny" and denied["question"] == ident and "you denied it" in denied["why"]
    assert "Approvals page (Refused)" in denied["why"]


def test_no_question_counts_as_open_while_it_waits_for_nobody(tmp_path):
    """Timed-out questions are not open ones: twenty of them leave room for the next question."""
    made = broker(tmp_path, timeout=0.05)
    for port in range(1, rt.OPEN_QUESTIONS + 3):
        made.ask("connect", {**CONNECT, "destination": f"192.0.2.7:{port}", "port": port})
    assert len(questions_in(tmp_path / "approvals")) == rt.OPEN_QUESTIONS + 2


def test_the_broker_waits_as_long_as_the_run_was_told(tmp_path):
    made = broker(tmp_path, timeout=0.6)
    started = time.monotonic()
    made.ask("http", HTTP)
    assert time.monotonic() - started >= 0.55
    question = json.loads(questions_in(tmp_path / "approvals")[0].read_text())
    assert question["timeout"] == 0.6


def test_decide_lists_a_timed_out_question_and_ask_again_withdraws_nothing(tmp_path):
    made = broker(tmp_path)
    made.ask("http", HTTP)
    ident = json.loads(questions_in(tmp_path / "approvals")[0].read_text())["id"]
    listed = made.control({"op": "decide", "action": "list"})
    assert [(e["question"], e["decision"], e["scope"]) for e in listed["decided"]] == [(ident, None, "timeout")]
    assert "pause" not in listed


# --- an MCP server of its own -------------------------------------------------------------------------------------

def test_an_mcp_server_of_its_own_keeps_an_unanswered_question_answerable(tmp_path):
    lines = []

    class Out:
        def write(self, text):
            lines.append(text)

        def flush(self):
            pass
    gateway = rt.McpGateway({"server": "files", "approvals": str(tmp_path), "timeout": 0.3}, out=Out())
    verdict = {"rule": "deletes", "ruleset": "rules.json", "reason": "asked"}
    first = gateway.ask("delete_file", "{}", verdict)
    assert first["reason"] == "not answered yet - the human may still allow it; retry later"
    ident = first["question"]
    # The next try waits again for the same question (no refusal at once); answered while it waits: allowed.
    results = []
    thread = threading.Thread(target=lambda: results.append(gateway.ask("delete_file", "{}", verdict)))
    thread.start()
    time.sleep(0.1)
    assert not results
    (tmp_path / f"{ident}.answer").write_text(json.dumps({"decision": "allow", "scope": "once"}))
    thread.join(5)
    assert results[0]["decision"] == "allow" and results[0]["question"] == ident
    asks = [json.loads(line.split(" ", 1)[1]) for line in "".join(lines).splitlines()
            if line.startswith("sandbox-ai-mcp-ask ")]
    assert [a["id"] for a in asks] == [ident, ident], "asked again under its own ID: the host shows it open again"


def test_an_mcp_server_of_its_own_takes_a_late_answer_at_the_next_call(tmp_path):
    gateway = rt.McpGateway({"server": "files", "approvals": str(tmp_path), "timeout": 0.2},
                            out=open(os.devnull, "w"))
    verdict = {"rule": "deletes", "ruleset": "rules.json", "reason": "asked"}
    ident = gateway.ask("delete_file", "{}", verdict)["question"]
    (tmp_path / f"{ident}.answer").write_text(json.dumps({"decision": "allow", "scope": "run"}))
    started = time.monotonic()
    allowed = gateway.ask("delete_file", "{}", verdict)
    assert allowed["decision"] == "allow" and time.monotonic() - started < 0.15
    assert gateway.ask("delete_file", "{}", verdict)["decision"] == "allow", "for the run"


# --- the host: list --state, and allow|deny|ask-again on any question -------------------------------------------

def short_dir(prefix):
    return Path(tempfile.mkdtemp(prefix=prefix, dir="/tmp"))


def serve(directory, made):
    (directory / "control.sock").unlink(missing_ok=True)
    stop = threading.Event()
    thread = threading.Thread(target=rt.control_server, args=(directory / "control.sock",
                                                              rt.RuntimeNetwork({}, None, lambda s: None), stop),
                              kwargs={"broker": made}, daemon=True)
    thread.start()
    assert wait_for(lambda: (directory / "control.sock").exists())
    return stop, thread


@pytest.fixture
def live(tmp_path, monkeypatch):
    """A registered run (this process stands in for its launcher) with a real broker and control socket."""
    monkeypatch.setattr(net, "USER_RUNTIME", tmp_path / "run-user")
    monkeypatch.setattr(net.liveness(), "inside_sandbox", lambda *a, **k: False)
    monkeypatch.setattr(ap, "net", lambda: net)
    monkeypatch.setattr(ap, "POLICY_FILE", tmp_path / "no-policy.json")
    short = short_dir("rework-")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(short))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    directory = net.launcher().runs_dir() / "claude-1"
    directory.mkdir(parents=True)
    record = {"run": "claude-1", "agent": "claude", "pid": os.getpid(), "workspace": str(tmp_path / "work"),
              "session_dir": str(tmp_path), "started": "2026-11-17T04:00:00Z", "session": None}
    (directory / "run.json").write_text(json.dumps(record))
    made = broker(directory, timeout=0.3)
    stop, thread = serve(directory, made)
    yield {"directory": directory, "broker": made, "runtime": short, "approvals": directory / "approvals"}
    stop.set()
    thread.join(5)
    shutil.rmtree(short, ignore_errors=True)


def cli(*argv):
    lines = []
    status = ap.main(list(argv), out=lines.append)
    return status, lines


def listed(state):
    status, lines = cli("list", "--state", state, "--json")
    assert status == 0
    return json.loads("\n".join(lines))


def test_list_by_state_and_what_each_entry_offers(live):
    made = live["broker"]
    made.ask("connect", CONNECT)  # times out
    timed_out = listed("timeout")
    assert [e["state"] for e in timed_out] == ["timeout"] and timed_out[0]["answerable"] is True
    assert timed_out[0]["actions"] == ["allow", "deny", "deny-once", "block"] and timed_out[0]["max_scope"] == "always"
    assert listed("open") == [], "nobody waits for it: not counted as open"
    ident = timed_out[0]["id"]
    # Allowed later (for the run): listed as allowed, now offering deny and ask-again (no new question).
    status, lines = cli("allow", ident, "--scope", "run")
    assert status == 0, lines
    assert wait_for(lambda: (live["approvals"] / f"{ident}.done").exists())
    allowed = listed("allowed")
    assert [e["id"] for e in allowed] == [ident] and allowed[0]["actions"] == ["deny", "ask-again"]
    assert allowed[0]["result"]["decision"] == "allow" and allowed[0]["result"]["scope"] == "run"
    assert made.ask("connect", CONNECT)["decision"] == "allow"
    # Denied now (reversible): refused, offering allow (for the run or always) and ask-again.
    status, lines = cli("deny", ident)
    assert status == 0, lines
    assert made.ask("connect", CONNECT)["decision"] == "deny"
    # The list says what the run decides now.
    refused = listed("refused")
    assert [e["id"] for e in refused] == [ident] and refused[0]["actions"] == ["allow", "ask-again"]
    assert refused[0]["result"]["changed"]
    status, lines = cli("allow", ident)  # an answered one: for the run by default
    assert status == 0, lines
    assert made.ask("connect", CONNECT)["decision"] == "allow" and [e["id"] for e in listed("allowed")] == [ident]
    # Asked again: its next request is asked anew; the entry says so and offers no second ask-again.
    assert cli("ask-again", ident)[0] == 0
    [entry] = listed("all")
    assert entry["asked_again"] is True and "ask-again" not in entry["actions"]


def test_a_refused_question_offers_allow_and_ask_again_and_the_text_list_says_the_state(live):
    made = live["broker"]
    results = []
    thread = threading.Thread(target=lambda: results.append(made.ask("http", HTTP)))
    made.timeout = 5
    thread.start()
    assert wait_for(lambda: listed("open"))
    ident = listed("open")[0]["id"]
    assert cli("deny", ident)[0] == 0
    thread.join(5)
    refused = listed("refused")
    assert [e["id"] for e in refused] == [ident] and refused[0]["actions"] == ["allow", "ask-again"]
    assert refused[0]["max_scope"] == "always"
    status, lines = cli("list", "--state", "all")
    assert status == 0 and "refused (run)" in "\n".join(lines) and "allow|deny|ask-again ID" in "\n".join(lines)
    # Undone: allowed for the run.
    assert cli("allow", ident)[0] == 0
    assert made.ask("http", HTTP)["decision"] == "allow"


def test_ask_again_on_a_question_still_open_says_so(live):
    live["broker"].ask("http", HTTP)
    ident = listed("timeout")[0]["id"]
    status, lines = cli("ask-again", ident)
    assert status == 0 and "still open" in lines[0]


def test_the_plain_list_points_at_the_questions_nobody_answered_in_time(live):
    live["broker"].ask("http", HTTP)
    status, lines = cli("list")
    assert status == 0 and "no open questions" in lines[0]
    assert "1 more nobody answered in time, still answerable: sandbox-ai approvals list --state timeout" in lines[-1]


# --- e2e: the real entry point (./sandbox-ai), only Podman faked ---------------------------------------------------

sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_fakes import Host  # noqa: E402


def test_e2e_a_question_times_out_is_allowed_later_and_the_next_try_passes(live, tmp_path):
    """User 2026-11-17: "dieser 60s timeout fuer freigaben nervt total, weil man es auch nicht rueckgaengig machen
    kann" - the timed-out question is still listed, answered later through ./sandbox-ai, and the next try passes."""
    host = Host(tmp_path / "host")
    env = {"XDG_RUNTIME_DIR": str(live["runtime"])}
    made = live["broker"]
    first = made.ask("connect", CONNECT)
    assert first["reason"] == rt.NOT_ANSWERED
    done = host.run("approvals", "list", "--state", "timeout", "--json", env=env, timeout=60)
    assert done.returncode == 0, done.stderr
    [entry] = json.loads(done.stdout)
    assert entry["id"] == first["question"] and entry["state"] == "timeout" and "allow" in entry["actions"]
    done = host.run("approvals", "allow", entry["id"], env=env, timeout=60)
    assert done.returncode == 0, done.stderr
    assert "allow (once)" in done.stdout
    assert wait_for(lambda: (live["approvals"] / f"{entry['id']}.done").exists())
    started = time.monotonic()
    assert made.ask("connect", CONNECT)["decision"] == "allow" and time.monotonic() - started < 0.2
    done = host.run("approvals", "list", "--state", "allowed", "--json", env=env, timeout=60)
    assert [e["id"] for e in json.loads(done.stdout)] == [entry["id"]]


def test_e2e_a_deny_is_undone_from_the_list(live, tmp_path):
    host = Host(tmp_path / "host")
    env = {"XDG_RUNTIME_DIR": str(live["runtime"])}
    made = live["broker"]
    made.timeout = 10
    results = []
    thread = threading.Thread(target=lambda: results.append(made.ask("http", HTTP)))
    thread.start()
    assert wait_for(lambda: listed("open"))
    ident = listed("open")[0]["id"]
    done = host.run("approvals", "deny", ident, env=env, timeout=60)
    assert done.returncode == 0, done.stderr
    thread.join(5)
    assert results[0]["decision"] == "deny" and made.ask("http", HTTP)["decision"] == "deny"
    done = host.run("approvals", "list", "--state", "refused", "--json", env=env, timeout=60)
    assert [e["actions"] for e in json.loads(done.stdout)] == [["allow", "ask-again"]]
    done = host.run("approvals", "allow", ident, env=env, timeout=60)
    assert done.returncode == 0, done.stderr
    assert "allowed for the rest of the run" in done.stdout
    assert made.ask("http", HTTP)["decision"] == "allow"
    done = host.run("approvals", "ask-again", ident, env=env, timeout=60)
    assert done.returncode == 0 and "asked again" in done.stdout


# --- --ask-timeout ------------------------------------------------------------------------------------------------

def test_ask_timeout_is_a_start_option_a_default_and_reaches_the_supervisor(tmp_path, monkeypatch):
    L = net.launcher()
    assert L.ASK_TIMEOUT_DEFAULT == rt.APPROVAL_SECONDS and L.ASK_TIMEOUT_RANGE == rt.ASK_TIMEOUT_RANGE
    a = L.parse_args(["claude", "--no-config", "--ask-timeout", "300", "--workspace", str(tmp_path)])
    assert a.ask_timeout == 300 and L.ask_timeout(a) == 300
    assert L.parse_args(["claude", "--no-config", "-at", "90", "--workspace", str(tmp_path)]).ask_timeout == 90
    for bad in ("4", "3601", "soon"):
        with pytest.raises(SystemExit):
            L.parse_args(["claude", "--no-config", "--ask-timeout", bad, "--workspace", str(tmp_path)])
    # The defaults file: checked as the launcher reads it.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    (tmp_path / "config" / "sandbox-ai").mkdir(parents=True)
    (tmp_path / "config" / "sandbox-ai" / "defaults.json").write_text(json.dumps({"version": 1, "ask_timeout": 120}))
    assert L.load_user_defaults(L.parse_args(["claude", "--no-config", "--workspace", str(tmp_path)]))[
        "ask_timeout"] == 120
    (tmp_path / "config" / "sandbox-ai" / "defaults.json").write_text(json.dumps({"version": 1, "ask_timeout": 1}))
    with pytest.raises(ValueError, match="ask_timeout is a number of seconds, 5-3600"):
        L.load_user_defaults(L.parse_args(["claude", "--no-config", "--workspace", str(tmp_path)]))
    # Not refused in a preset, a project file's options, and no weakening option.
    assert "--ask-timeout" not in L.load_presets_module().REFUSED
    assert "--ask-timeout" not in L.MODES.WEAKENING_OPTIONS


# --- 24h review A (2026-11-17): taking an allowed name back ------------------------------------------------------------

def allowed_name(live, tmp_path, monkeypatch, name="evil.example.org", address="93.184.216.34"):
    """A lookup of NAME asked and allowed for the run, as the page's Allow does: the name pinned in the sandbox's
    /etc/hosts, its address open. (question ID, the hosts file)"""
    hosts = tmp_path / "hosts"
    hosts.write_text("127.0.0.1 localhost\n")
    made = live["broker"]
    made.timeout = 10
    results = []
    thread = threading.Thread(target=lambda: results.append(made.ask("dns", {"name": name})))
    thread.start()
    assert wait_for(lambda: listed("open"))
    ident = listed("open")[0]["id"]
    ap.answer(ident, "allow", "run", resolver=lambda host: [address], out=lambda line: None)
    thread.join(5)
    assert results[0]["decision"] == "allow" and f"{address} {name}" in hosts.read_text()
    # (the fixture's broker records its firewall changes; the run's own network, behind the control socket, gets it here)
    net.ask(str(live["directory"]), {"op": "allow", "https": [f"{address}/32"]})
    assert f"{address}/32" in net.ask(str(live["directory"]), {"op": "show"})["https"]
    return ident, hosts


@pytest.mark.parametrize("command", ["ask-again", "deny"])
def test_taking_an_allowed_name_back_closes_it(live, tmp_path, monkeypatch, command):
    """Undo after "Allow" on a lookup (the app, the web: ask-again), or `approvals deny ID` on it: the run forgot the
    answer - and the name stayed pinned in the sandbox's /etc/hosts with its addresses open for the rest of the run
    ("Undone." on the page; the note that said so was the command's second line, which the web did not show). What
    the answer opened is closed again now, as for a connection."""
    ident, hosts = allowed_name(live, tmp_path, monkeypatch)
    status, lines = cli(command, ident)
    assert status == 0, lines
    assert "evil.example.org" not in hosts.read_text() and "localhost" in hosts.read_text()
    assert "evil.example.org" not in net.remembered(net.running()["claude-1"])
    assert "93.184.216.34/32" not in net.ask(str(live["directory"]), {"op": "show"})["https"]
    said = "\n".join(lines)
    assert "stays pinned" not in said and "no longer pinned" in said, said
    # (and the run decides anew: asked again, or denied for the rest of it)
    made = live["broker"]
    made.timeout = 0.3
    again = made.ask("dns", {"name": "evil.example.org"})
    assert again["decision"] == "deny" and (again["reason"] == rt.NOT_ANSWERED) == (command == "ask-again")


def test_allowing_an_allowed_name_again_keeps_it_open(live, tmp_path, monkeypatch):
    """`approvals allow ID --scope run` on a name that is allowed (decide_again: net allow, then the run forgets what
    it had decided) must not take back what it just opened."""
    ident, hosts = allowed_name(live, tmp_path, monkeypatch)
    monkeypatch.setattr(ap, "resolve", lambda name: ["93.184.216.34"])
    status, lines = cli("allow", ident, "--scope", "run")
    assert status == 0, lines
    assert "93.184.216.34 evil.example.org" in hosts.read_text()
    assert "93.184.216.34/32" in net.ask(str(live["directory"]), {"op": "show"})["https"]
