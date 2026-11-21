"""A start's tunnel lent to its own MCP servers (vpn_runtime.Lender / Borrower): a borrowing VPN container that came
up while its lender's 512 connections were all in use (the start's other servers) waited 15 s and then said "the
start's tunnel is not lent here: ... refused it: too many connections" - a lender that is there and lends, called
missing. Now a full lender still answers a borrower's look on places of its own (LEND_SPARE; only the borrowers' VPN
containers send looks, a server's program reaches the lender through its relay, which asks for connections and
lookups only), and a borrower whose look is refused all the same waits with a backoff and then says the lender is
busy. With real Unix sockets, and end to end through the real ./sandbox-ai with a lender that is full
(tests/live_fakes.py FAKE_VPN_LEND_FULL)."""
import importlib.util
import json
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rt = load("vpn_runtime_lent_busy", "sandbox/vpn_runtime.py")
TOKEN = "5a" * 32


@pytest.fixture(autouse=True)
def run_token(monkeypatch):
    monkeypatch.setattr(rt, "_lend_token", TOKEN)


@pytest.fixture
def folder():
    short = Path(tempfile.mkdtemp(prefix="lend-"))
    yield short
    shutil.rmtree(short, ignore_errors=True)


def full_lender(folder, limit=1):
    """A lender whose LIMIT connections are all held (the far ends kept open), and what holds them."""
    far, held = [], []

    def connect(target):
        near, other = socket.socketpair()
        far.append(other)
        return near
    lender = rt.Lender({"interface": "wg0", "dns": ["10.66.0.1"]}, "wg", path=folder / "lend.sock", connect=connect,
                       local=lambda address: False, limit=limit).start()
    for _ in range(limit):
        held.append(rt.ask_lender({"op": "tcp", "address": "93.184.216.34", "port": 443}, folder / "lend.sock")[1])
    return lender, far + held


def test_a_full_lender_still_answers_a_look_and_refuses_anything_else(folder):
    lender, open_ = full_lender(folder)
    try:
        for _ in range(3 * rt.LEND_SPARE):  # one after the other: each spare place comes back
            facts, sock = rt.ask_lender({"op": "ping"}, folder / "lend.sock")
            sock.close()
            assert facts["profile"] == "wg" and facts["interface"] == "wg0"
        for op in ({"op": "tcp", "address": "93.184.216.34", "port": 443},
                   {"op": "dns", "query": "AAAAAAAAAAAAAAAA"}):
            with pytest.raises(rt.LenderRefused, match="too many connections"):
                rt.ask_lender(op, folder / "lend.sock")
        assert rt.lender_facts("wg", folder / "lend.sock", wait=0)["profile"] == "wg"
        assert rt.lender_gone("wg", folder / "lend.sock") is None
        # A look without the run's token is denied on a spare place too.
        with pytest.raises(rt.LenderDenied):
            rt.ask_lender({"op": "ping"}, folder / "lend.sock", token="6b" * 32)
        assert lender.spare._value == rt.LEND_SPARE  # (every spare place given back)
    finally:
        for sock in open_:
            sock.close()
        lender.close()


def test_the_spare_places_are_bounded_too(folder):
    """Looks that hold every spare place (they never send their line): one more is refused, as before."""
    lender, open_ = full_lender(folder)
    silent = []
    try:
        for _ in range(rt.LEND_SPARE):
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(str(folder / "lend.sock"))
            silent.append(sock)
        deadline = time.monotonic() + 5
        while lender.spare._value and time.monotonic() < deadline:
            time.sleep(0.01)
        with pytest.raises(rt.LenderRefused, match="too many connections"):
            rt.ask_lender({"op": "ping"}, folder / "lend.sock")
    finally:
        for sock in silent + open_:
            sock.close()
        lender.close()


class Clock:
    def __init__(self):
        self.now, self.slept = 0.0, []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def test_a_borrower_whose_look_is_refused_backs_off_and_then_says_busy():
    clock, asked = Clock(), []

    def refused(request, path, timeout=5):
        asked.append(request["op"])
        raise rt.LenderRefused(f"the start's lent tunnel refused it: {rt.BUSY}")
    with pytest.raises(ValueError) as caught:
        rt.lender_facts("wg", ask=refused, clock=clock, sleep=clock.sleep)
    text = str(caught.value)
    assert "is busy" in text and "512 connections" in text and "the start's other MCP servers" in text, text
    assert "not lent here" not in text
    assert clock.slept[:5] == [0.5, 1, 2, 4, 5] and set(clock.slept[5:]) <= {5}
    assert 55 <= clock.now <= rt.LEND_BUSY and len(asked) == len(clock.slept) + 1


def test_a_busy_lender_that_frees_a_place_is_borrowed():
    clock, tries = Clock(), []

    class Sock:
        def close(self):
            pass

    def busy_twice(request, path, timeout=5):
        tries.append(1)
        if len(tries) <= 2:
            raise rt.LenderRefused("the start's lent tunnel refused it: too many")
        return {"ok": True, "profile": "wg", "interface": "wg0"}, Sock()
    assert rt.lender_facts("wg", ask=busy_twice, clock=clock, sleep=clock.sleep)["interface"] == "wg0"
    assert clock.slept == [0.5, 1]


def test_a_lender_that_is_not_there_is_still_said_so_after_its_wait():
    clock = Clock()

    def none(request, path, timeout=5):
        raise OSError("the start's lent tunnel did not answer (No such file or directory)")
    with pytest.raises(ValueError, match="is not lent here"):
        rt.lender_facts("wg", ask=none, clock=clock, sleep=clock.sleep)
    assert clock.now >= rt.LEND_READY and clock.now < rt.LEND_READY + 1


# --- end to end: the real ./sandbox-ai -------------------------------------------------------------------------------

def test_e2e_a_server_borrows_the_starts_tunnel_while_every_lent_connection_is_in_use(tmp_path):
    from test_hotfix_mcp_lent_vpn import Host, add_fetch, add_wireguard, events
    host = Host(tmp_path / "host")
    host.env["SANDBOX_AI_SECRETS"] = "file"
    host.env["FAKE_VPN_DIR"] = str(host.directory / "vpn-containers")
    add_wireguard(host)
    add_fetch(host)
    host.add_image("localhost/codex-sandbox:latest", "localhost/sandbox-ai-vpn:latest")
    done = host.run("codex", "--no-tmux", "--no-auth", "--vpn", "wg", "--mcp", "fetch",
                    env={"FAKE_PODMAN_RUN": "0:0", "FAKE_VPN_LEND_FULL": "1"}, timeout=300)
    said = done.stdout + done.stderr
    assert "not lent here" not in said and "is busy" not in said, said[-3000:]
    seen = events(host)
    assert any(event.get("lender") == "full" for event in seen), seen
    assert any("borrowed" in event for event in seen), seen
