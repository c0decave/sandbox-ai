"""sandbox-ai bridge between two sessions that both run on other hosts (sandbox/bridge_remote.py: Relay, delegate_add).

Sessions of two other hosts (host1:alpha, host2:beta): each host runs its own half (the real `bridge-stub` of a copy
of bridge.py with that host's state, in a thread, over a fake SSH connection), and this machine relays the frames
between the two halves; it has no side of its own. Two sessions of one other host (host1:alpha, host1:beta): the
bridge is made there (`bridge add alpha beta` on host1, over a fake `hosts.run` that runs host1's bridge.py), and
listed, shown, revoked and resumed as host1:ID. Hostile cases: a half that calls what its side was not granted, a
host that answers as the other one (the same machine), older halves, a dropped leg.
"""
import contextlib
import io
import json
import threading
import time
from types import SimpleNamespace

import pytest

import test_bridge as tb
from test_bridge import world  # noqa: F401  (fixture)
from test_bridge import call, text, wait_for
from test_bridge_remote import FakeSsh, Peer

b, ap = tb.b, tb.ap
HOSTS = ("host1", "host2")


def other_host(monkeypatch, tmp_path, host):
    """HOST's sandbox-ai: a copy of bridge.py with that host's own state (its bridges, its identity); the sessions
    are the world's (each half finds its own there)."""
    rb = tb.load(f"sandbox_bridge_{host}_relay_test", "bridge.py")
    root = tmp_path / f"{host}-state"
    monkeypatch.setattr(rb, "state_root", lambda: root / "sandbox-ai" / "bridges")
    monkeypatch.setattr(rb, "inside_sandbox", lambda: False)
    monkeypatch.setattr(rb, "_modules", {key: b._modules[key] for key in ("mcp", "net", "starts", "runtime",
                                                                         "approvals")})
    notes, halves, threads = [], [], []
    monkeypatch.setattr(rb, "notify", lambda title, body, alarm=False: notes.append((title, body, alarm)))

    class Tracked(rb.Broker):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            halves.append(self)
    monkeypatch.setattr(rb, "Broker", Tracked)

    def stop(data):
        for half in halves:
            if half.ident == data["id"]:
                half.stopping.set()
    monkeypatch.setattr(rb, "stop_broker", stop)

    def spawn(ident):  # (a bridge made on this host by `bridge add` there: its broker in a thread)
        broker = rb.make_broker(ident)
        broker.record_broker()
        broker.start()
        thread = threading.Thread(target=broker.run, kwargs={"interval": 0.05}, daemon=True)
        thread.start()
        threads.append(thread)
    monkeypatch.setattr(rb, "spawn_broker", spawn)
    there = rb.remote()
    monkeypatch.setattr(there, "origin_name", lambda: host)
    return SimpleNamespace(rb=rb, there=there, halves=halves, root=root, notes=notes, threads=threads,
                           questions=lambda: ap.questions(ap.bridge_runs(root)))


def answer_on(root, decision="allow", scope="once", count=1, seconds=20):
    """Answer COUNT questions of the bridges under ROOT (a host's state) as they come, in a thread."""
    answered = []

    def run():
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and len(answered) < count:
            for _, _, question in ap.questions(ap.bridge_runs(root)):
                if question["id"] not in [q["id"] for q in answered]:
                    ap.answer(question["id"], decision, scope, runs=ap.bridge_runs(root), out=lambda line: None)
                    answered.append(question)
            time.sleep(0.02)
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return answered, thread


@pytest.fixture
def three(world, monkeypatch, tmp_path):  # noqa: F811
    """This machine (the world's bridge.py, no session of the bridge here), host1 and host2 (each its own bridge.py
    and state), joined by fake SSH connections from this machine."""
    hosts = b.module("hosts")
    hosts.save({host: {"ssh": f"me@{host}.example", "port": 22} for host in HOSTS})
    asked = []
    monkeypatch.setattr(hosts, "ask_version", lambda name, *args, **kwargs: asked.append(name) or ("0.1.98",
                                                                                                    (0, 1, 98)))
    others = {host: other_host(monkeypatch, tmp_path, host) for host in HOSTS}
    here = b.remote()
    monkeypatch.setattr(here, "origin_name", lambda: "laptop")
    connections, scripts, ran = [], {}, []

    def connect(host, ident):
        assert host in HOSTS
        run = scripts.get(host) or (lambda reader, writer, h=host: others[h].there.stub_main(
            [ident], reader=reader, writer=writer, signals=False))
        connections.append((host, FakeSsh(run)))
        return connections[-1][1]
    monkeypatch.setattr(here, "connect", connect)

    def run_there(name, args, input=None, timeout=120, path=None, runner=None, sudo=None):
        """hosts.run: `sandbox-ai ARGS` on host NAME - its bridge.py's main, its output and errors."""
        ran.append((name, list(args)))
        assert name in HOSTS and args[0] == "bridge"
        said, errors = [], io.StringIO()
        with contextlib.redirect_stderr(errors):
            status = others[name].rb.main(list(args[1:]), out=said.append)
        return status, "".join(line + "\n" for line in said), errors.getvalue()
    monkeypatch.setattr(hosts, "run", run_there)
    yield SimpleNamespace(hosts=hosts, others=others, here=here, connections=connections, scripts=scripts,
                          asked=asked, ran=ran)
    for other in others.values():
        for half in other.halves:
            half.stopping.set()
    for _, connection in connections:
        connection.drop()
        connection.thread.join(10)
    for other in others.values():
        for thread in other.threads:
            thread.join(10)


def state(three, host, ident):
    return three.others[host].rb.load(ident)


# --- two other hosts: the relay -------------------------------------------------------------------------------------

def test_sessions_of_two_other_hosts_are_bridged_through_this_machine(three, world):  # noqa: F811
    alpha, beta = world.make("alpha"), world.make("beta")
    (beta.folder / "src").mkdir()
    (beta.folder / "src/app.py").write_text("print('one')\n")
    data = world.add("host1:alpha", "host2:beta", "message,ask,files:rw:src")
    ident = data["id"]
    assert data["state"] == "active" and data["relay"]
    assert (data["a"]["host"], data["b"]["host"]) == ("host1", "host2")
    assert (data["a"]["run"], data["b"]["run"]) == ("session-alpha", "session-beta")
    assert data["a"]["server"] == "bridge-host2-beta" and data["b"]["server"] == "bridge-host1-alpha"
    assert alpha.relays.names() == ["bridge-host2-beta"] and beta.relays.names() == ["bridge-host1-alpha"]
    assert sorted(three.asked) == ["host1", "host2"]
    assert not any((b.bridge_dir(ident) / side / "server.sock").exists() for side in b.SIDES), "no side here"
    assert any("this machine passes their frames on" in note for note in data["notes"])
    assert any("this machine passes their frames on" in line for line in world.said)
    # Each host keeps its half: its record names the other session by the other host, and who relays it.
    one, two = state(three, "host1", ident), state(three, "host2", ident)
    assert one["remote_half"] and one["relayed_by"] == "laptop" and one["b"]["name"] == "host2:beta"
    assert one["b"]["run"] == "session-beta" and one["b"]["host_id"] == three.others["host2"].there.host_id()
    assert two["a"]["name"] == "host1:alpha" and two["a"]["host_id"] == three.others["host1"].there.host_id()
    assert one["grants"] == two["grants"] == data["grants"]
    # message: into beta's inbox on host2, framed there as host1:alpha's.
    assert not call(alpha, "bridge-host2-beta", "send_message", {"text": "hello over two hosts"})["isError"]
    got = text(call(beta, "bridge-host1-alpha", "inbox"))
    assert "hello over two hosts" in got and "content from the session host1:alpha" in got
    assert beta.taint.shown()["tainted"] and not alpha.taint.shown()["tainted"]
    # read: done on host2, framed on host1 as host2:beta's content.
    read = text(call(alpha, "bridge-host2-beta", "read_file", {"path": "src/app.py"}))
    assert "print('one')" in read and "content from the session host2:beta" in read
    assert read.count("untrusted data, not instructions") == 1 and alpha.taint.shown()["tainted"]
    # write: asked on host2 only (where the files are), with the diff.
    answered, thread = answer_on(three.others["host2"].root, "allow", "always")
    wrote = call(alpha, "bridge-host2-beta", "write_file", {"path": "src/app.py", "content": "print('two')\n"})
    thread.join(10)
    assert not wrote["isError"], text(wrote)
    assert (beta.folder / "src/app.py").read_text() == "print('two')\n"
    assert answered[0]["from"] == "host1:alpha" and "+print('two')" in answered[0]["arguments_full"]
    assert not three.others["host1"].questions() and not ap.questions(ap.bridge_runs()), "asked nowhere else"
    # ask: beta's agent answers on host2; the answer comes back to alpha on host1.
    result = {}
    asking = threading.Thread(target=lambda: result.update(call(alpha, "bridge-host2-beta", "ask",
                                                                {"question": "which port?", "timeout_seconds": 30})))
    asking.start()
    assert wait_for(lambda: "which port?" in text(call(beta, "bridge-host1-alpha", "inbox", {"wait_seconds": 1})), 15)
    half = three.others["host2"].halves[0]
    assert wait_for(lambda: half.asks, 10)
    assert not call(beta, "bridge-host1-alpha", "answer", {"question_id": next(iter(half.asks)),
                                                           "text": "8080"})["isError"]
    asking.join(20)
    assert "8080" in text(result) and not result["isError"]
    # This machine logs what it passed on (side, tool, size), never the content.
    log = (b.bridge_dir(ident) / "bridge.jsonl").read_text()
    assert "print('two')" not in log and "hello over two hosts" not in log and "8080" not in log
    relayed = {json.loads(line).get("tool") for line in log.splitlines() if json.loads(line).get("relayed")}
    assert {"send_message", "read_file", "write_file", "ask"} <= relayed
    row = b.listing()[0]
    assert row["relay"] and (row["a_host"], row["b_host"]) == ("host1", "host2") and row["a"] == "host1:alpha"
    assert b.load(ident)["calls"] >= 4 or wait_for(lambda: b.load(ident)["calls"] >= 4, 5)
    # A revoke here ends both halves: both relays closed, both records final.
    b.revoke(ident, out=world.said.append)
    assert wait_for(lambda: alpha.relays.names() == [] and beta.relays.names() == [], 15)
    assert wait_for(lambda: state(three, "host1", ident)["state"] == "ended" and
                    state(three, "host2", ident)["state"] == "ended", 15)
    assert "revoked" in state(three, "host1", ident)["reason"]
    assert b.load(ident)["state"] == "revoked"


def pretending(script, name="alpha", run="session-alpha", host_id="ab" * 8, features=("describe",)):
    """A half that says hello, describes and opens as a well-behaved one does, then runs SCRIPT(peer)."""
    def main(reader, writer):
        peer = Peer(reader, writer)
        peer.send({"t": "hello", "protocol": 1, "version": "0.1.98", "host_id": host_id, "features": list(features)})
        threading.Thread(target=peer.read_all, daemon=True).start()
        asked = peer.wait(lambda f: f.get("op") == "describe", 20)
        if asked is None:
            return 1
        side = {"name": name, "run": run, "agent": "claude", "workspace_mode": "rw"}
        peer.send({"t": "res", "id": asked["id"], "body": {"ok": True, "side": side}})
        opened = peer.wait(lambda f: f.get("op") == "open", 20)
        if opened is None:
            return 1
        peer.opened = opened["body"]
        peer.send({"t": "res", "id": opened["id"], "body": {"ok": True, "side": {**side, "server": "bridge-x"},
                                                            "network": None}})
        script(peer)
        peer.wait(lambda f: f.get("op") == "end", 60)
        return 0
    return main


def test_the_relay_passes_on_only_what_the_side_was_granted(three, world):  # noqa: F811
    """A half on host1 that calls what alpha was not granted, beyond its path, or not a call at all: refused on this
    machine, never sent to host2 (which would refuse it as well); what was granted goes through."""
    beta = world.make("beta")
    (beta.folder / "src").mkdir()
    (beta.folder / "src/app.py").write_text("granted\n")
    (beta.folder / "secret.txt").write_text("SECRET\n")
    results, done = {}, threading.Event()

    def script(peer):
        results["opened"] = peer.opened
        results["write"] = peer.call("write_file", {"path": "src/app.py", "content": "x"})
        results["outside"] = peer.call("read_file", {"path": "secret.txt"})
        results["up"] = peer.call("read_file", {"path": "src/../secret.txt"})
        results["message"] = peer.call("send_message", {"text": "not granted"})
        results["info"] = peer.call("info")
        results["open"] = peer.request("open", {"id": "0badf00d"})
        results["read"] = peer.call("read_file", {"path": "src/app.py"})
        done.set()
    three.scripts["host1"] = pretending(script)
    data = world.add("host1:alpha", "host2:beta", "files:ro:src")
    assert done.wait(30)
    body = results["opened"]
    assert body["side"] == "a" and body["origin"] == "host2" and body["relay"] == "laptop"
    assert body["peer"]["run"] == "session-beta" and body["peer"]["host_id"] == three.others["host2"].there.host_id()
    for key, why in (("write", "not granted"), ("outside", "not granted"), ("up", "'..'"), ("message", "not granted"),
                     ("info", "not served"), ("open", "no operation open")):
        assert results[key] and results[key]["ok"] is False and why in results[key]["text"], (key, results[key])
        assert results[key]["text"].startswith("sandbox-ai bridge (relay):")
    assert results["read"]["ok"] and "granted" in results["read"]["text"]
    there = [json.loads(line) for line in (three.others["host2"].rb.bridge_dir(data["id"]) / "bridge.jsonl")
             .read_text().splitlines()]
    assert [r["tool"] for r in there if r.get("tool")] == ["read_file"], "only the granted call reached host2"
    assert (beta.folder / "src/app.py").read_text() == "granted\n"
    assert text(call(beta, "bridge-host1-alpha", "info"))  # (beta has its server; nothing in its inbox to read)
    here = [json.loads(line) for line in (b.bridge_dir(data["id"]) / "bridge.jsonl").read_text().splitlines()]
    assert sum(1 for r in here if r.get("relayed") is False) == 6


def test_a_suspension_on_one_host_suspends_all_three_until_the_human_lifts_it(three, world):  # noqa: F811
    alpha, beta = world.make("alpha"), world.make("beta")
    data = world.add("host1:alpha", "host2:beta", "message", both=True)
    ident = data["id"]
    (beta.run / "cut.json").write_text(json.dumps({"mode": "all", "cuts": {"session-beta": "all"}}))
    assert wait_for(lambda: b.load(ident)["state"] == "suspended", 15), "told through the relay"
    assert "host2" in b.load(ident)["reason"] and "kill switch of beta" in b.load(ident)["reason"]
    assert wait_for(lambda: state(three, "host1", ident)["state"] == "suspended", 15)
    refused = call(alpha, "bridge-host2-beta", "send_message", {"text": "x"})
    assert refused["isError"] and "suspended" in text(refused)
    # Lifted here while the cut on host2 holds: host2 says so, and all stay suspended.
    b.resume(ident, out=world.said.append)
    assert wait_for(lambda: b.load(ident)["state"] == "suspended" and
                    "still holds" in (b.load(ident).get("reason") or ""), 15)
    assert wait_for(lambda: state(three, "host1", ident)["state"] == "suspended", 15)
    (beta.run / "cut.json").unlink()
    b.resume(ident, out=world.said.append)
    assert wait_for(lambda: not call(alpha, "bridge-host2-beta", "send_message", {"text": "y"})["isError"], 15)
    assert wait_for(lambda: state(three, "host2", ident)["state"] == "active", 10)
    assert "y" in text(call(beta, "bridge-host1-alpha", "inbox"))


def test_a_dropped_leg_is_re_established_while_both_sessions_run(three, world):  # noqa: F811
    """Backlog: a relayed bridge ended when this machine slept (or a leg dropped). Now a lost link (its end without
    a word) is re-established: both halves started again and rejoined with the same grant (each checks it against
    its own record), the record says "reconnecting" meanwhile and counts the reconnect; calls go through again."""
    alpha, beta = world.make("alpha"), world.make("beta")
    data = world.add("host1:alpha", "host2:beta", "message")
    ident = data["id"]
    host, connection = three.connections[0]
    connection.drop()
    assert wait_for(lambda: b.load(ident).get("reconnects") == 1, 30), b.load(ident)
    record = b.load(ident)
    assert record["state"] == "active" and not record.get("link")
    assert len(three.connections) == 4, "both halves started again"
    for name in HOSTS:
        there = state(three, name, ident)
        assert there["state"] == "active" and there["rejoins"] == 1 and there["grants"] == data["grants"]
    log = [json.loads(line) for line in (b.bridge_dir(ident) / "bridge.jsonl").read_text().splitlines()]
    events = [entry.get("event") for entry in log]
    assert "reconnecting" in events and "reconnected" in events
    reconnecting = next(entry for entry in log if entry.get("event") == "reconnecting")
    assert f"connection to {host} was lost" in reconnecting["reason"]
    assert wait_for(lambda: alpha.relays.names() == ["bridge-host2-beta"] and
                    beta.relays.names() == ["bridge-host1-alpha"], 15), "each side attached again"
    assert wait_for(lambda: not call(alpha, "bridge-host2-beta", "send_message", {"text": "after the gap"})["isError"],
                    15)
    assert "after the gap" in text(call(beta, "bridge-host1-alpha", "inbox"))
    row = b.listing()[0]
    assert row["reconnects"] == 1 and not row["reconnecting"]
    shown = []
    b.show(ident, out=shown.append)
    assert any("re-established 1 time(s)" in line for line in shown)


def test_this_machines_sleep_is_seen_and_the_bridge_re_established(three, world):  # noqa: F811
    """A sleep of this machine (a fake clock: the wall clock 10 minutes ahead of the monotonic one, which stops while
    it sleeps) is seen at once - longer than the halves wait (LINK_IDLE): both re-established without waiting for
    silence."""
    world.make("alpha")
    world.make("beta")
    data = world.add("host1:alpha", "host2:beta", "message")
    ident = data["id"]
    relay = world.broker(ident)
    assert wait_for(lambda: relay.opened, 15)
    offset = [0.0]
    relay.clock = lambda: time.time() + offset[0]
    wait_for(lambda: relay.tick is not None and relay.tick[0] >= time.time() - 1, 5)
    offset[0] = 600.0  # 10 minutes asleep
    assert wait_for(lambda: b.load(ident).get("reconnects") == 1, 30), b.load(ident)
    log = [json.loads(line) for line in (b.bridge_dir(ident) / "bridge.jsonl").read_text().splitlines()]
    slept = next(entry for entry in log if entry.get("event") == "slept")
    assert slept["seconds"] >= 590
    reason = next(entry for entry in log if entry.get("event") == "reconnecting")["reason"]
    assert reason.startswith("this machine slept for ")
    assert b.load(ident)["state"] == "active"


def test_a_lost_leg_ends_the_bridge_when_a_session_no_longer_runs(three, world):  # noqa: F811
    """Not re-established when one of the sessions ended meanwhile (the leg to the other host lost, then beta ends):
    the bridge ends on every host, with why - never joined to another run."""
    alpha, beta = world.make("alpha"), world.make("beta")
    data = world.add("host1:alpha", "host2:beta", "message")
    ident = data["id"]
    relay = world.broker(ident)
    real = relay.establish

    def gap(rejoin=False):  # (beta ends while this machine is away)
        if rejoin:
            record = json.loads((beta.run / "run.json").read_text())
            (beta.run / "run.json").write_text(json.dumps({**record, "pid": 999999999}))
        return real(rejoin)
    relay.establish = gap
    three.connections[0][1].drop()
    assert wait_for(lambda: b.load(ident)["state"] == "ended", 60), b.load(ident)
    reason = b.load(ident)["reason"]
    assert "could not be re-established" in reason and "beta" in reason, reason
    assert not b.load(ident).get("reconnects")
    assert wait_for(lambda: alpha.relays.names() == [] and beta.relays.names() == [], 15)
    for name in HOSTS:
        assert wait_for(lambda n=name: state(three, n, ident)["state"] in ("ended", "failed"), 15)


def test_a_half_rejoins_only_what_it_had(three, world):  # noqa: F811
    """rejoinable: the half on its host checks a rejoin against its own record - only a relayed bridge whose link
    was lost, recently, for the same side, sessions, runs and exactly the grant: a widened grant, another run of the
    peer, a revoked or timed-out bridge, an old loss are refused."""
    world.make("alpha")
    world.make("beta")
    data = world.add("host1:alpha", "host2:beta", "message")
    ident = data["id"]
    there = three.others["host1"].there
    old = state(three, "host1", ident)
    old.update(state="ended", link_lost=True, ended=three.others["host1"].rb.now_text())
    peer = {"name": old["b"]["start_name"], "run": old["b"]["run"], "host_id": old["b"]["host_id"]}
    body = {"side": "a", "name": old["a"]["name"], "peer": peer, "grants": data["grants"], "ask": old["ask"],
            "limits": old["limits"], "until": old.get("until"), "rejoin": True}
    assert there.rejoinable(old, body) is None
    wider = json.loads(json.dumps(data["grants"]))
    wider["a"]["ask"] = True
    assert "not the grant" in there.rejoinable(old, {**body, "grants": wider})
    assert "another run" in there.rejoinable(old, {**body, "peer": {**peer, "run": "session-beta-2"}})
    assert "not the session" in there.rejoinable(old, {**body, "name": "gamma"})
    assert "not the side" in there.rejoinable(old, {**body, "side": "b"})
    assert "ended for good" in there.rejoinable({**old, "state": "revoked"}, body)
    assert "ended for good" in there.rejoinable({**old, "link_lost": None}, body)
    assert "more than 24 h ago" in there.rejoinable(old, body, now=time.time() + 25 * 3600)
    assert "not relayed" in there.rejoinable({**old, "relayed_by": None}, body)
    assert "questions, limits or time" in there.rejoinable(old, {**body, "ask": "always"})
    assert "time ran out" in there.rejoinable({**old, "until": "2020-01-01T00:00:00Z"},
                                              {**body, "until": "2020-01-01T00:00:00Z"})


def test_an_older_half_that_cannot_rejoin_ends_the_bridge(three, world):  # noqa: F811
    world.make("alpha")
    world.make("beta")
    data = world.add("host1:alpha", "host2:beta", "message")
    ident = data["id"]
    three.scripts["host1"] = pretending(lambda peer: None, features=("describe",))  # (0.1.98: no rejoin)
    three.connections[0][1].drop()
    assert wait_for(lambda: b.load(ident)["state"] == "ended", 30)
    assert "cannot rejoin a bridge" in b.load(ident)["reason"] and "hosts update host1" in b.load(ident)["reason"]


def test_a_revoke_on_one_host_ends_the_relay_and_the_other_half(three, world):  # noqa: F811
    alpha, beta = world.make("alpha"), world.make("beta")
    data = world.add("host1:alpha", "host2:beta", "message")
    three.others["host2"].rb.revoke(data["id"], out=lambda line: None)
    assert wait_for(lambda: b.load(data["id"])["state"] == "ended", 15)
    assert "its half on host2 ended it" in b.load(data["id"])["reason"] and "revoked" in b.load(data["id"])["reason"]
    assert wait_for(lambda: state(three, "host1", data["id"])["state"] == "ended", 15)
    assert wait_for(lambda: alpha.relays.names() == [] and beta.relays.names() == [], 15)


def test_what_cannot_be_relayed_is_refused_and_leaves_nothing(three, world, monkeypatch):  # noqa: F811
    alpha, beta = world.make("alpha"), world.make("beta", workspace_mode="ro")
    # Known to be older than 0.1.98: refused before anything connects.
    monkeypatch.setattr(three.hosts, "ask_version", lambda name, *a, **k: ("0.1.97", (0, 1, 97)))
    with pytest.raises(ValueError, match=r"two other hosts needs 0\.1\.98 or later on both.*hosts update host1"):
        world.add("host1:alpha", "host2:beta", "message")
    assert not three.connections
    # Not known: the hello decides; a half without "describe" (0.1.97) is refused, the other told to end.
    monkeypatch.setattr(three.hosts, "ask_version", lambda name, *a, **k: (None, None))
    three.scripts["host1"] = pretending(lambda peer: None, features=())
    with pytest.raises(ValueError, match=r"0\.1\.98 or later on both"):
        world.add("host1:alpha", "host2:beta", "message")
    assert b.load(b.all_ids()[-1])["state"] == "failed"
    three.scripts.clear()
    # Two names of one machine (the same bridge identity): refused, bridge them there.
    own = three.others["host2"].there.host_id
    monkeypatch.setattr(three.others["host2"].there, "host_id", three.others["host1"].there.host_id)
    with pytest.raises(ValueError, match="same machine.*bridge add host1:A host1:B"):
        world.add("host1:alpha", "host2:beta", "message")
    monkeypatch.setattr(three.others["host2"].there, "host_id", own)
    # files:rw into a read-only workspace: refused before either half opens.
    with pytest.raises(ValueError, match="read-only for it"):
        world.add("host1:alpha", "host2:beta", "files:rw:.")
    assert alpha.relays.names() == [] and beta.relays.names() == []
    assert not three.others["host1"].rb.all_ids() and not three.others["host2"].rb.all_ids(), "no half made"
    # A dry run names both hosts and the relay, and starts nothing.
    before = len(three.connections)
    plan = world.add("host1:alpha", "host2:beta", "message", dry_run=True)
    assert plan["id"] is None and len(plan["notes"]) == 3 and "this machine passes" in plan["notes"][-1]
    assert len(three.connections) == before


# --- two sessions of one other host: made there ---------------------------------------------------------------------

def test_two_sessions_of_one_other_host_are_bridged_there(three, world):  # noqa: F811
    alpha, beta = world.make("alpha"), world.make("beta")
    data = world.add("host1:alpha", "host1:beta", "message,files:ro:/work/src", until="10:30", ask="never")
    ident = data["id"]
    assert data["host"] == "host1" and data["a"]["name"] == "host1:alpha" and data["b"]["name"] == "host1:beta"
    host, args = three.ran[-1]
    assert host == "host1" and args[:4] == ["bridge", "add", "alpha", "beta"] and args[-1] == "--json"
    assert "--allow=message,files:ro:src" in args and "--ask=never" in args
    until = next(arg for arg in args if arg.startswith("--until="))
    assert until.endswith("Z") and "T" in until, "the time it names here, never HH:MM on the other host's clock"
    assert not b.all_ids() and not three.connections, "nothing of it on this machine, no hop through it"
    assert ident in three.others["host1"].rb.all_ids()
    assert alpha.relays.names() == ["bridge-beta"] and beta.relays.names() == ["bridge-alpha"]
    assert any(f"show|revoke|resume host1:{ident}" in line for line in world.said)
    assert not call(alpha, "bridge-beta", "send_message", {"text": "same host"})["isError"]
    assert "same host" in text(call(beta, "bridge-alpha", "inbox"))
    # Listed here with the hosts' bridges, shown, and revoked as host1:ID.
    said = []
    assert b.main(["list", "--hosts", "--json"], out=said.append) == 0
    rows = json.loads(said[-1])
    assert [(row["host"], row["id"], row["a"], row["b"]) for row in rows] == [("host1", ident, "host1:alpha",
                                                                               "host1:beta")]
    said.clear()
    assert b.main(["list", "--hosts"], out=said.append) == 0
    assert said[0].startswith(f"host1:{ident}  active")
    said.clear()
    assert b.main(["show", f"host1:{ident}", "--json"], out=said.append) == 0
    assert json.loads(said[-1])["host"] == "host1" and json.loads(said[-1])["id"] == ident
    said.clear()
    assert b.main(["revoke", f"host1:{ident}"], out=said.append) == 0
    assert any(f"bridge {ident} revoked" in line for line in said)
    assert wait_for(lambda: alpha.relays.names() == [] and beta.relays.names() == [], 15)
    assert three.others["host1"].rb.load(ident)["state"] == "revoked"
    # What host1 refuses is said with its host; a dry run makes nothing there.
    with pytest.raises(ValueError, match="host1: nothing named zeta runs"):
        world.add("host1:alpha", "host1:zeta", "message")
    plan = world.add("host1:alpha", "host1:beta", "message", dry_run=True)
    assert plan["id"] is None and plan["host"] == "host1" and len(three.others["host1"].rb.all_ids()) == 1
    errors = io.StringIO()
    with contextlib.redirect_stderr(errors):
        assert b.main(["show", "host9:0123abcd"], out=said.append) == 1
    assert "no host host9" in errors.getvalue()


def test_resume_of_a_bridge_made_on_another_host(three, world):  # noqa: F811
    world.make("alpha"), world.make("beta")
    beta = world.runs["beta"]
    data = world.add("host1:alpha", "host1:beta", "message")
    (beta.run / "cut.json").write_text(json.dumps({"mode": "all", "cuts": {"session-beta": "all"}}))
    rb = three.others["host1"].rb
    assert wait_for(lambda: rb.load(data["id"])["state"] == "suspended", 15)
    said = []
    assert b.main(["resume", f"host1:{data['id']}"], out=said.append) == 1, "the cut still holds there"
    (beta.run / "cut.json").unlink()
    assert b.main(["resume", f"host1:{data['id']}"], out=said.append) == 0
    assert rb.load(data["id"])["state"] == "active" and any("active again" in line for line in said)


# --- this machine's view of the hosts' bridges ----------------------------------------------------------------------

def test_the_hosts_bridges_are_listed_once_each(three, world):  # noqa: F811
    r = three.here
    local = [{"id": "aaaa0001", "state": "active", "a": "alpha", "b": "host1:beta", "a_host": None, "b_host": "host1",
              "remote_half": False}]
    host1 = r.host_rows("host1", [
        {"id": "aaaa0001", "state": "active", "a": "beta", "b": "laptop:alpha", "a_host": None, "b_host": "laptop",
         "remote_half": True},                                   # the other half of this machine's bridge
        {"id": "bbbb0002", "state": "active", "a": "x", "b": "y", "remote_half": False},   # made on host1
        {"id": "cccc0003", "state": "active", "a": "x", "b": "host2:z", "b_host": "host2", "remote_half": False},
        {"id": "not an id"}, "junk"])
    host2 = r.host_rows("host2", [
        {"id": "cccc0003", "state": "active", "a": "z", "b": "host1:x", "b_host": "host1", "remote_half": True},
        {"id": "dddd0004", "state": "active", "a": "q", "b": "other:w", "b_host": "other", "remote_half": True}])
    rows = r.merged_rows(local, host2 + host1)
    assert [(row["host"], row["id"]) for row in rows] == [(None, "aaaa0001"), ("host1", "bbbb0002"),
                                                          ("host1", "cccc0003"), ("host2", "dddd0004")]
    made_there = rows[1]
    assert (made_there["a"], made_there["a_host"], made_there["b"]) == ("host1:x", "host1", "host1:y")
    assert rows[2]["b"] == "host2:z", "a session of a third host keeps the name its host gave it"
    # IDs: ID or HOST:ID, HOST one of your hosts; local:ID is this machine's.
    assert r.qualified_id("0123abcd") == (None, "0123abcd") and r.qualified_id("local:0123abcd") == (None, "0123abcd")
    assert r.qualified_id("host2:0123abcd") == ("host2", "0123abcd")
    for bad in ("0123abc", "me@x:0123abcd", "host1:../x", "-o:0123abcd"):
        with pytest.raises(ValueError, match="HOST:ID"):
            r.qualified_id(bad)
    with pytest.raises(ValueError, match="no host host9"):
        r.qualified_id("host9:0123abcd")
    # A host that does not answer is said, the others listed.
    def silent(name, args, input=None, timeout=120, path=None, runner=None, sudo=None):
        return (255, "", "ssh: connect to host host2: Connection refused") if name == "host2" else (0, "[]", "")
    three.hosts.run = silent
    said, errors = [], io.StringIO()
    with contextlib.redirect_stderr(errors):
        assert b.main(["list", "--hosts"], out=said.append) == 0
    assert said == ["no bridges (--all: ended ones too)"]
    assert "host2 did not answer" in errors.getvalue()


def test_a_half_that_floods_the_relay_grows_this_machines_log_no_faster_than_the_rate(three, world):  # noqa: F811
    """A half on host1 that ignores the bridge's rate: the relay refuses beyond twice the rate, and logs those
    refusals as one line a minute - never a line per frame on this machine."""
    world.make("beta")
    results, done = [], threading.Event()

    def script(peer):
        for number in range(150):
            results.append(peer.request("call", {"tool": "watch_activity", "arguments": {}}, 10))  # (not granted)
        done.set()
    three.scripts["host1"] = pretending(script)
    data = world.add("host1:alpha", "host2:beta", "message", limits=["rate=6"])
    assert done.wait(60)
    over = [r for r in results if r and "too many calls" in r["text"]]
    assert len(over) >= 100 and all(not r["ok"] for r in results)
    lines = (b.bridge_dir(data["id"]) / "bridge.jsonl").read_text().splitlines()
    assert len(lines) < 30, len(lines)
    assert any(json.loads(line).get("event") == "rate" for line in lines)


def test_this_hosts_identity_made_by_many_at_once_is_one_and_never_read_empty(three):
    """The relay checks both hellos at once, each against this machine's bridge identity: the first ones to ask made
    it together, and one of them read the file the other had made but not written yet."""
    here = three.here
    path = b.state_root().parent / "bridge-host-id"
    for _ in range(20):
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        found, errors = [], []

        def ask():
            try:
                found.append(here.host_id())
            except ValueError as exc:
                errors.append(exc)
        threads = [threading.Thread(target=ask) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert not errors and len(set(found)) == 1 and path.read_text().strip() == found[0]
    assert [p.name for p in path.parent.iterdir() if p.name.startswith(".bridge-host-id")] == []
    assert path.stat().st_mode & 0o777 == 0o600


def test_hops_and_loops_are_counted_through_the_relay(three, world):  # noqa: F811
    """The relay is no hop (it carries no run): content of beta's run that passed two bridges on host2 already cannot
    pass this one; alpha's own content coming back to it from host2 over another bridge is a loop - named by host1's
    identity, which the relay passed on untouched."""
    alpha, beta = world.make("alpha"), world.make("beta")
    data = world.add("host1:alpha", "host2:beta", "message", both=True)
    rb2 = three.others["host2"].rb
    record = json.loads((beta.run / "run.json").read_text())

    def exposure(origins):
        with rb2.exposure_lock() as directory:
            rb2.write_private(directory / "session-beta.json", {"pid": record["pid"], "pid_start": None,
                                                                "origins": origins})
    exposure({"session-zeta": {"hops": 2, "bridges": ["0000aaaa"]}})
    refused = call(beta, "bridge-host1-alpha", "send_message", {"text": "relay"})
    assert refused["isError"] and "pass 3 bridges" in text(refused)
    assert not call(alpha, "bridge-host2-beta", "send_message", {"text": "fine"})["isError"]
    host1 = three.others["host1"].there.host_id()
    for bridges, loop in (([data["id"]], False), (["0000bbbb"], True)):
        exposure({f"{host1}:session-alpha": {"hops": 1, "bridges": bridges}})
        result = call(beta, "bridge-host1-alpha", "send_message", {"text": "reply"})
        assert result["isError"] == loop and (not loop or "close a loop" in text(result)), text(result)
    # What beta sent reached alpha's run on host1 with beta's origin named by host2's identity, one hop.
    rb1 = three.others["host1"].rb
    with rb1.exposure_lock() as directory:
        origins = rb1.read_exposure(directory, rb1.load(data["id"])["a"])
    host2 = three.others["host2"].there.host_id()
    assert origins.get(f"{host2}:session-beta", {}).get("hops") == 1, origins


def test_a_link_ended_for_a_reason_is_never_a_lost_one(three):
    """The lead: a bridge that ENDED for a reason (a flood, a malformed answer, a revoke) must never be rejoined -
    only a lost link or this machine's sleep is. The "end" frame told to the other half could fail to be written (it
    stopped reading) - that failure marked the link lost, and the half's record rejoinable. Now a link ended on
    purpose stays ended for its reason, whatever the write of its "end" frame does."""
    here = three.here

    class Broken:
        def write(self, data):
            raise BrokenPipeError("the other half reads nothing")

        def flush(self):
            pass

        def close(self):
            pass
    link = here.Link(io.BytesIO(b""), Broken(), "host1")
    link.end("host1 broke the bridge's protocol (more than 10 changes of state a minute)")
    assert link.closed.is_set() and link.lost is False and "changes of state" in link.reason
    lost = here.Link(io.BytesIO(b""), Broken(), "host1")
    lost.event("ping", {})
    assert lost.closed.is_set() and lost.lost is True, "a write that fails by itself: the connection is lost"
