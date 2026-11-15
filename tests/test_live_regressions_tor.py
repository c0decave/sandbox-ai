"""Regression from live use (2026-11-14): a session's start through Tor, given up while tor still bootstrapped.

User's session start through Tor:

    agent-sandbox: vpn: bringing up tor
    agent-sandbox: vpn: tor: bootstrapped 0%
    agent-sandbox: vpn: tor: bootstrapped 5% (Connecting to a relay)
    … 14% … 30% … 50% … 55% … 61% …
    agent-sandbox: vpn: tor: bootstrapped 69% (Loading relay descriptors)
    agent-sandbox: [####----------------] 2/5 VPN: bringing up the VPN tor-test (its container, its tunnel and kill swit… (240 s)
    agent-sandbox: Session did not become ready within 240 s; see …/sessions/upbeat-peacock-toffee/supervisor.log

User: "Timeout zu kurz? U.U. wenn es am Bootstrap liegt längerer Timeout oder noch 3× danach versuchen zu starten?"

Why: the terminal a session was started in waited as long as its supervisor wrote new lines (7c7b68bc), but tor
said a line only for a new percentage, and it stood at 69% for longer than the 90 s of silence that wait allowed past
its first 120 s (here 240 s with what else the start had): it gave up while tor - with 240 s of its own, not even
used up - still bootstrapped. And a tor that stalls stayed stalled: there was no second attempt.

Now, on both layers: the session's wait goes on while its supervisor says new lines and gives up only after 120 s
without one (or 20 minutes, more for a Tor's own settings), saying which; tor says its percentage, every 30 s one
that stays, and an attempt that stalls (90 s without a new percentage) starts tor again with a fresh guard - up to 3
attempts of 300 s each (longer while it still rises), settings of the profile - behind the kill switch that stays.
MCP servers behind a Tor of their own: the same, and their bootstrap is said in the start's progress.

End to end: the real ./sandbox-ai with only Podman stood in for (live_fakes.py); its Tor container runs the real
vpn_runtime.py with a fake tor whose bootstrap is slow, stalls or progresses (live_fakes.FAKE_TOR)."""
import importlib.util
import json
from pathlib import Path
import re
import sys
import time
import types

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_fakes import Host  # noqa: E402

TOR_IMAGE = "localhost/sandbox-ai-tor:latest"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sessions = load("sandbox_sessions_tor_regression", "sandbox/sessions.py")
progress = load("sandbox_progress_tor_regression", "sandbox/progress.py")
rt = load("sandbox_vpn_runtime_tor_regression", "sandbox/vpn_runtime.py")
mcp = load("sandbox_mcp_tor_regression", "sandbox/mcp.py")

USERS_LINES = ["vpn: bringing up tor", "vpn: tor: bootstrapped 0%", "vpn: tor: bootstrapped 5% (Connecting to a relay)",
               "vpn: tor: bootstrapped 14% (Handshaking with a relay)",
               "vpn: tor: bootstrapped 30% (Loading networkstatus consensus)",
               "vpn: tor: bootstrapped 50% (Loading relay descriptors)",
               "vpn: tor: bootstrapped 55% (Loading relay descriptors)",
               "vpn: tor: bootstrapped 61% (Loading relay descriptors)",
               "vpn: tor: bootstrapped 69% (Loading relay descriptors)"]


# --- the layers, on a clock of their own ---------------------------------------------------------------------------

class Timeline:
    """A session's supervisor.log written at the times LINES gives ([(seconds, line)]); its terminal relay up at READY
    (None: never). time.sleep of the session's wait moves this clock."""

    def __init__(self, directory, lines, ready=None):
        self.directory, self.lines, self.ready, self.now = Path(directory), sorted(lines), ready, 0.0
        (self.directory / "tmux").mkdir(parents=True, exist_ok=True)
        (self.directory / "supervisor.log").write_text("")
        self.sleep(0)

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(seconds, 1.0)  # (a second per look: minutes of a start in a few thousand looks)
        with open(self.directory / "supervisor.log", "a") as log:
            while self.lines and self.lines[0][0] <= self.now:
                log.write(f"agent-sandbox: {self.lines.pop(0)[1]}\n")
        if self.ready is not None and self.now >= self.ready:
            (self.directory / "tmux" / "attach.sock").touch()


SUPERVISOR = types.SimpleNamespace(poll=lambda: None, returncode=None, pid=4242)


def tor_timeline(*schedules, stall=90, timeout=300, attempts=3):
    """What the VPN container says (the real bootstrap with its retries, vpn_runtime.bootstrap) of a tor whose
    attempts go as SCHEDULES says, with the time of each line: [(seconds, "vpn: ...")], and when tor got to 100%
    (None: never)."""
    sim = TorSim(schedules)
    said = []
    stream = types.SimpleNamespace(write=lambda text: said.append((sim.now, json.loads(text)["message"])),
                                   flush=lambda: None)
    done = None
    try:
        rt.bootstrap(sim.start, sim.log, Listing(), {"timeout": timeout, "attempts": attempts, "stall": stall}, stream,
                     clock=sim.clock, sleep=sim.sleep)
        done = sim.now
    except RuntimeError as exc:
        said.append((sim.now, f"the VPN did not come up: {exc}"))
    sim.close()
    return [(at, f"vpn: {text}") for at, text in said], done


class TorSim:
    def __init__(self, schedules):
        import tempfile
        self.folder = tempfile.TemporaryDirectory()
        self.log = Path(self.folder.name) / "tor.log"
        self.log.write_text("")
        self.schedules, self.now, self.started, self.stopped = list(schedules), 0.0, [], []
        self.guard = rt.fresh_guard
        rt.fresh_guard = lambda uid: None

    def close(self):
        rt.fresh_guard = self.guard
        self.folder.cleanup()

    def clock(self):
        return self.now

    def start(self):
        number = len(self.started)
        self.started.append(self.now)
        return types.SimpleNamespace(args=["tor"], returncode=None, poll=lambda: None,
                                     terminate=lambda: self.stopped.append(number), wait=lambda timeout=None: 0)

    def sleep(self, seconds):
        before, self.now = self.now, self.now + seconds
        attempt = len(self.started) - 1
        begun = self.started[attempt]
        with open(self.log, "a") as stream:
            for at, percent, summary in self.schedules[min(attempt, len(self.schedules) - 1)]:
                if before < begun + at <= self.now:
                    stream.write(f"[notice] Bootstrapped {percent}% (step): {summary}\n")


class Listing:
    """The container's nft: its table listed as loaded."""
    children = []

    def run(self, command, data=None, check=True):
        return types.SimpleNamespace(returncode=0, stderr="", stdout=(
            f"table inet {rt.TABLE} {{\n chain output {{ type filter hook output priority -5; policy drop;\n }}\n"
            " chain input { type filter hook input priority -5; policy drop;\n }\n"
            " chain forward { type filter hook forward priority -5; policy drop; }\n}\n"))


USERS_TOR = [(2, 5, "Connecting to a relay"), (10, 14, "Handshaking with a relay"),
             (25, 30, "Loading networkstatus consensus"), (45, 50, "Loading relay descriptors"),
             (60, 55, "Loading relay descriptors"), (75, 61, "Loading relay descriptors"),
             (100, 69, "Loading relay descriptors")]  # and then nothing: the user's tor
SECOND_TRY = [(3, 10, "Connecting to a relay"), (40, 75, "Connecting to a relay to build circuits"),
              (70, 90, "Establishing a Tor circuit"), (95, 100, "Done")]


def test_the_users_start_is_waited_for_through_a_stall_and_a_second_attempt(tmp_path):
    """User: 'Timeout zu kurz? U.U. wenn es am Bootstrap liegt längerer Timeout oder noch 3× danach versuchen zu
    starten?' - their tor stood at 69% (Loading relay descriptors). Now: it says that it stands (every 30 s), tries
    again after 90 s without a new percentage, the second attempt gets to 100%, and the terminal the session was
    started in waits all along - no "did not become ready within 240 s"."""
    lines, done = tor_timeline(USERS_TOR, SECOND_TRY)
    assert done is not None
    said = [text for _, text in lines]
    assert "vpn: tor: bootstrapped 69% (Loading relay descriptors), attempt 1 of 3" in said
    assert "vpn: Tor bootstrap stalled at 69% (Loading relay descriptors) for 90 s; trying again (2 of 3)" in said
    assert "vpn: tor: bootstrapped 100% (Done), attempt 2 of 3" in said
    # Never 120 s without a line while tor bootstraps: what the session's wait sees as progress.
    times = [0.0] + [at for at, _ in lines]
    assert max(b - a for a, b in zip(times, times[1:])) < sessions.START_IDLE
    # The session: its VPN step, tor's lines from 60 s on (an image, the pod before), its terminal 30 s after 100%.
    timeline = Timeline(tmp_path, [(1, "bringing up the VPN tor-test (its container, its tunnel and kill switch)")]
                        + [(60 + at, text) for at, text in lines], ready=60 + done + 30)
    out = []
    sessions.wait_ready(tmp_path, SUPERVISOR, timeout=240, follow=0, out=out.append, clock=timeline.clock, bar=False,
                        sleep=timeline.sleep)
    assert timeline.now >= 60 + done + 30 > 240  # past the 240 s the user's start was given up after
    assert any("trying again (2 of 3)" in line for line in out)


def test_a_supervisor_silent_for_120_s_is_given_up_and_the_message_says_since_when_and_where(tmp_path):
    """A start that really is stuck still ends: past the time it was given, 120 s without a new line - and the
    message says so, since when, and the last line (the step it stood at), not "within 240 s"."""
    timeline = Timeline(tmp_path, [(1, "bringing up the VPN tor-test (its container, its tunnel and kill switch)"),
                                   (150, "vpn: tor: bootstrapped 69% (Loading relay descriptors), attempt 1 of 3")])
    with pytest.raises(RuntimeError) as error:
        sessions.wait_ready(tmp_path, SUPERVISOR, timeout=240, follow=0, out=lambda text: None, clock=timeline.clock,
                            bar=False, sleep=timeline.sleep)
    text = str(error.value)
    assert 270 <= timeline.now <= 275
    assert text.startswith("Session did not become ready: it said nothing new for 12")
    assert "s after it began); its last line: agent-sandbox: vpn: tor: bootstrapped 69% (Loading relay descriptors)" \
        in text and "since " in text and "supervisor.log" in text


def test_a_start_that_goes_on_and_on_ends_at_its_cap_and_says_so(tmp_path):
    lines = [(i * 30, f"vpn: tor: still at 69% (Loading relay descriptors) for {i * 30} s") for i in range(1, 100)]
    timeline = Timeline(tmp_path, lines)
    # The test's own sleep, not time.sleep patched for the whole process: the release build's run had other
    # threads (left by earlier tests) whose sleeps moved this clock too, and "it took 1200 s" did not match.
    with pytest.raises(RuntimeError, match=r"it took 1200 s, the longest a start may take \(1200 s\), though it "
                                           r"still said new steps; its last line: .*still at 69%"):
        sessions.wait_ready(tmp_path, SUPERVISOR, timeout=120, follow=0, out=lambda text: None, clock=timeline.clock,
                            bar=False, sleep=timeline.sleep)


def test_the_longest_start_follows_the_tor_profiles_settings(monkeypatch):
    """20 minutes, unless the start's Tor may take longer by its settings (each attempt at its longest), twice that
    with MCP servers behind it."""
    vpn = types.SimpleNamespace(load_profile=lambda name: {"name": name, "type": "tor",
                                                           "tor": {"bootstrap_timeout": 900, "bootstrap_attempts": 4}},
                                ready_timeout=lambda profile: 120 + rt.bootstrap_seconds(
                                    rt.bootstrap_settings(profile["tor"])))
    monkeypatch.setattr(sessions, "launcher", lambda: types.SimpleNamespace(load_vpn_module=lambda: vpn))
    assert sessions.start_cap(types.SimpleNamespace(vpn=None, mcp=[]), 120) == 1200
    assert sessions.start_cap(types.SimpleNamespace(vpn="t", mcp=[]), 120) == 120 + 120 + 4 * (1800 + 15)
    assert sessions.start_cap(types.SimpleNamespace(vpn="t", mcp=["fetch"]), 120) == 120 + 2 * (120 + 4 * 1815)


def test_the_bar_shows_tors_percentage_not_a_frozen_step():
    """The bar's line (a terminal): "VPN: Tor 69% (Loading relay descriptors), attempt 1 of 3" instead of the frozen
    "bringing up the VPN tor-test (its container, its tunnel and kill swit…"; a retry is said above it."""
    written, now = [], [0.0]
    bar = progress.Bar(["vpn", "kits", "pod", "terminal"], write=written.append, clock=lambda: now[0])
    bar.line("bringing up the VPN tor-test (its container, its tunnel and kill switch)")
    now[0] = 240
    bar.line("vpn: tor: bootstrapped 69% (Loading relay descriptors), attempt 1 of 3")
    assert "1/4 VPN: Tor 69% (Loading relay descriptors), attempt 1 of 3 (240 s)" in bar.text()
    assert not any("\n" in text for text in written)  # nothing printed above for a percentage
    bar.line("vpn: Tor bootstrap stalled at 69% (Loading relay descriptors) for 90 s; trying again (2 of 3)")
    assert any("trying again (2 of 3)\n" in text for text in written)
    bar.line("vpn: tor: still at 10% (Connecting to a relay) for 30 s, attempt 2 of 3")
    assert "VPN: Tor 10% (Connecting to a relay) for 30 s, attempt 2 of 3" in bar.text()
    mcp_bar = progress.Bar(["mcp", "vpn", "pod", "terminal"], write=written.append, clock=lambda: 0.0)
    mcp_bar.line("MCP server fetch: vpn: tor: bootstrapped 30% (Loading networkstatus consensus), attempt 1 of 3")
    assert "1/4 MCP servers: fetch: Tor 30% (Loading networkstatus consensus), attempt 1 of 3" in mcp_bar.text()


def test_an_mcp_server_behind_its_own_tor_is_waited_for_and_its_bootstrap_said(tmp_path):
    """MCP servers behind Tor (each its own): their wait was a fixed 120 s. Now it goes on while the server's log
    says new lines, and its Tor's bootstrap is said in the start's own progress (MCP server NAME: vpn: tor: ...)."""
    lines, done = tor_timeline(USERS_TOR, SECOND_TRY)
    log = tmp_path / "supervisor.log"
    log.write_text("")
    pending = sorted((at, f"sandbox-ai mcp: {text}") for at, text in lines)
    now, socket_path = [0.0], tmp_path / "server.sock"

    def sleep(seconds):
        now[0] += 1.0
        with open(log, "a") as stream:
            while pending and pending[0][0] <= now[0]:
                stream.write(pending.pop(0)[1] + "\n")
        if now[0] >= done + 20:
            socket_path.touch()
    said = []
    mcp.wait_for_socket(socket_path, log, SUPERVISOR, timeout=120, name="fetch", clock=lambda: now[0], sleep=sleep,
                        say=said.append)
    assert now[0] >= done + 20 > 120
    assert "agent-sandbox: MCP server fetch: vpn: tor: bootstrapped 69% (Loading relay descriptors), attempt 1 of 3" \
        in said
    assert "agent-sandbox: MCP server fetch: vpn: Tor bootstrap stalled at 69% (Loading relay descriptors) for 90 s; " \
           "trying again (2 of 3)" in said
    # Stuck for good: given up once its log says nothing new for 120 s past its time, with its last line.
    socket_path.unlink()
    log.write_text("sandbox-ai mcp: vpn: tor: bootstrapped 5% (Connecting to a relay)\n")
    now[0] = 0.0
    with pytest.raises(RuntimeError, match=r"MCP server fetch did not become ready: it said nothing new for 12[0-9] s.*"
                                           r"its last line: sandbox-ai mcp: vpn: tor: bootstrapped 5%"):
        mcp.wait_for_socket(socket_path, log, SUPERVISOR, timeout=120, name="fetch", clock=lambda: now[0],
                            sleep=lambda seconds: now.__setitem__(0, now[0] + 1.0), say=said.append)
    assert 120 <= now[0] <= 122


# --- end to end: ./sandbox-ai vpn check with a Tor container whose tor is slow, stalls or progresses -------------

@pytest.fixture
def host(tmp_path):
    made = Host(tmp_path)
    made.add_image(TOR_IMAGE)
    added = made.run("vpn", "add", "tor-test", "--tor", "--check-url", "https://localhost/ip", "--no-dns-probe")
    assert added.returncode == 0, added.stderr
    return made


def tor_events(host):
    path = host.directory / "tor-container" / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def check(host, *schedules, speed=60, timeout=240):
    begun = time.monotonic()
    result = host.run("vpn", "check", "tor-test", timeout=timeout,
                      env={"FAKE_TOR_SCHEDULE": json.dumps(schedules), "FAKE_TOR_SPEED": str(speed)})
    return result, time.monotonic() - begun


def test_e2e_a_stalled_tor_is_started_again_behind_its_kill_switch_and_comes_up(host):
    result, _ = check(host, USERS_TOR, SECOND_TRY)
    out = result.stdout
    assert "vpn: tor: bootstrapped 69% (Loading relay descriptors), attempt 1 of 3" in out, result.stderr
    assert "vpn: Tor bootstrap stalled at 69% (Loading relay descriptors) for 9" in out  # (90 s, give or take a look)
    assert "; trying again (2 of 3)" in out
    assert "vpn: tor: bootstrapped 100% (Done), attempt 2 of 3" in out
    assert "attempt 3 of 3" not in out
    assert "tor-test: tor (tor, TransPort 127.0.0.1:9040) is up" in out
    events = tor_events(host)
    starts = [i for i, e in enumerate(events) if "tor" in e]
    assert len(starts) == 2
    # The kill switch: loaded once, before the first tor; between the two only looked at - never flushed, deleted
    # or loaded again: nothing leaves past Tor while tor starts again.
    nft = [(i, e["nft"]) for i, e in enumerate(events) if "nft" in e]
    loads = [i for i, command in nft if command[:2] == ["nft", "-f"]]
    assert len(loads) == 1 and loads[0] < starts[0]
    between = [command for i, command in nft if starts[0] < i < starts[1]]
    assert between == [["nft", "list", "table", "inet", rt.TABLE]]
    assert not any(command[1] in ("flush", "delete") for _, command in nft)
    # The first tor ended before the second began; the second chose a new guard (tor's state file was gone).
    ended = next(i for i, e in enumerate(events) if e.get("tor_ended") == 1)
    assert ended < starts[1] and events[starts[1]]["guard_kept"] is False


def test_e2e_three_stalls_end_with_what_was_tried_and_what_helps(host):
    result, _ = check(host, USERS_TOR, USERS_TOR, [(2, 5, "Connecting to a relay")])
    text = result.stdout + result.stderr
    assert result.returncode != 0
    assert text.count("trying again") == 2 and "trying again (3 of 3)" in text
    assert "Tor did not finish its bootstrap in 3 attempts (each 300 s, up to 600 s while it still rose" in text
    assert "attempt 1 stalled at 69% (Loading relay descriptors)" in text
    assert "attempt 3 stalled at 5% (Connecting to a relay)" in text
    assert "Its last bootstrap line:" in text and "Bootstrapped 5% (fake): Connecting to a relay" in text
    assert "--bridges" in text and "clock" in text and "--bootstrap-attempts" in text
    assert "did not come up in time" not in text  # the container said why, before the launcher stopped waiting
    assert len([e for e in tor_events(host) if "tor" in e]) == 3


def test_e2e_a_slow_tor_that_still_rises_gets_its_time_and_no_second_attempt(host):
    """A slow network: a new percentage every 80 s, 100% after 560 s - past the 300 s of an attempt, but never 90 s
    without a new percentage: one attempt, no retry."""
    slow = [(2, 5, "Connecting to a relay")] + [(80 * i, 5 + 13 * i, f"Step {i}") for i in range(1, 7)] + \
        [(560, 100, "Done")]
    result, _ = check(host, slow)
    out = result.stdout
    assert "vpn: tor: bootstrapped 100% (Done), attempt 1 of 3" in out, result.stderr
    assert "trying again" not in out and "still at" in out
    assert len([e for e in tor_events(host) if "tor" in e]) == 1


def test_e2e_the_profiles_settings_reach_the_tor_container(host):
    """vpn set: one attempt only, a stall after 60 s - the container follows the profile."""
    assert host.run("vpn", "set", "tor-test", "--bootstrap-attempts", "1", "--bootstrap-stall", "60").returncode == 0
    result, _ = check(host, USERS_TOR)
    text = result.stdout + result.stderr
    assert "Tor did not finish its bootstrap in 1 attempt (each 300 s, up to 600 s while it still rose" in text
    assert "stalled at 69% (Loading relay descriptors) for 6" in text and "trying again" not in text
    shown = host.run("vpn", "show", "tor-test")
    assert "bootstrap: 300 s per attempt (up to 600 s while its percentage still rises), 1 attempt, tried again " \
           "after 60 s" in shown.stdout


def test_e2e_the_users_start_through_tor_goes_on_after_a_stall_and_says_it_in_launcher_log(host):
    """The user's start itself (./sandbox-ai claude --vpn tor-test): tor stands at 69%, starts again, comes up, the
    agent's sandbox runs. The terminal says each step; launcher.log has the stall and the attempt that made it."""
    host.add_image("localhost/claude-sandbox:latest")
    result = host.run("claude", "--vpn", "tor-test", env={"FAKE_PODMAN_RUN": "0:0",
                                                          "FAKE_TOR_SCHEDULE": json.dumps([USERS_TOR, SECOND_TRY])})
    assert result.returncode == 0, result.stderr
    err = result.stderr
    assert "agent-sandbox: vpn: tor: bootstrapped 69% (Loading relay descriptors), attempt 1 of 3" in err
    assert "agent-sandbox: vpn: tor: still at 69% (Loading relay descriptors) for 30 s, attempt 1 of 3" in err
    assert "; trying again (2 of 3)" in err
    assert "agent-sandbox: vpn: tor: bootstrapped 100% (Done), attempt 2 of 3" in err
    assert "fake podman: the sandbox runs" in err
    log = (Path(host.env["XDG_STATE_HOME"]) / "sandbox-ai" / "launcher.log").read_text()
    assert "WARNING vpn: Tor bootstrap stalled at 69% (Loading relay descriptors) for 9" in log
    assert "INFO    vpn: the container says: tor: bootstrapped 100% (Done), attempt 2 of 3" in log
    assert "its tunnel is up after" in log


# == MCP servers behind Tor (2026-11-14) ===========================================================================
#
# User: "angeblich können einige MCPs z.B. whatweb nicht Tor benutzen obwohl Tor als Protokoll aktiviert wurde beim
# MCP-Setup - bitte in-depth untersuchen ob alle MCPs, wenn Tor aktiviert ist, das auch nutzen können".
#
# Found (tests/test_mcp_tor_matrix.py holds every server of both catalogs to it): every catalog server that reaches
# the network runs behind its Tor (its own Tor container's namespace, no way around it); whatweb works there (Ruby's
# resolver asks port 53, which tor answers; HTTP over TCP). What does not: nmap's connect scans through Tor's
# transparent proxy see every port open (tor's TransPort accepts each connection before an exit tries it) - it is
# refused behind Tor now, with the reason, instead of a server that lies; servers that ask DNS for record types tor's
# resolver does not answer (osint-mcp, openosint, dnstwist, nuclei) or need a server the exits cannot reach (searxng
# with an instance of your own network) are said with what does not work, when added and when they start.

DIGEST = "sha256:" + "c" * 64


def tor_catalog(host):
    path = host.directory / "catalog.json"
    entries = [{"id": f"{prefix}:{name}", "name": name, "title": name, "kind": "oci", "digest": DIGEST,
                "network": "full", "image": f"ghcr.io/c0decave/{prefix}-{name}"}
               for prefix, name in (("hub", "nmap"), ("mcp", "whatweb"), ("mcp", "osint-mcp"))]
    path.write_text(json.dumps({"schema": "axross-mcp-catalog", "version": 1, "entries": entries}))
    return path


def test_e2e_mcp_servers_set_up_behind_tor_as_the_app_does(host):
    """The app's MCP setup with Tor runs `sandbox-ai mcp add NAME --vpn TOR`: whatweb is added behind it, osint-mcp
    with what does not work behind Tor said, nmap refused with the reason; a start with --vpn TOR refuses nmap added
    without a VPN before anything starts."""
    catalog = tor_catalog(host)
    host.add_image(*[f"ghcr.io/c0decave/mcp-{name}@{DIGEST}" for name in ("whatweb", "osint-mcp")],
                   f"ghcr.io/c0decave/hub-nmap@{DIGEST}", "localhost/claude-sandbox:latest")
    added = {name: host.run("mcp", "add", name, "--vpn", "tor-test", "--catalog", catalog, "--unsigned", "--no-pull")
             for name in ("whatweb", "osint-mcp", "nmap")}
    assert added["whatweb"].returncode == 0, added["whatweb"].stderr
    assert "behind Tor" not in added["whatweb"].stdout + added["whatweb"].stderr
    assert added["osint-mcp"].returncode == 0
    assert "MCP server osint-mcp behind Tor: dns_lookup of record types other than A, AAAA and PTR" in \
        added["osint-mcp"].stdout
    assert added["nmap"].returncode != 0
    assert "MCP server nmap cannot work behind the Tor profile tor-test: its connect scans would see every port " \
           "open" in added["nmap"].stderr
    servers = json.loads((host.home / ".config/sandbox-ai/mcp.json").read_text())
    assert servers["whatweb"]["vpn"] == servers["osint-mcp"]["vpn"] == "tor-test" and "nmap" not in servers
    assert host.run("mcp", "add", "nmap", "--catalog", catalog, "--unsigned", "--no-pull").returncode == 0
    started = host.run("claude", "--vpn", "tor-test", "--mcp", "nmap", "--dry-run")
    assert started.returncode != 0
    assert "MCP server nmap cannot work behind the Tor profile tor-test" in started.stderr
    log = (Path(host.env["XDG_STATE_HOME"]) / "sandbox-ai" / "launcher.log").read_text()
    assert "refused: MCP server nmap cannot work behind the Tor profile tor-test" in log


# --- regression from live use (2026-11-14): "no answer from 127.0.53.1" right after the bootstrap -------------------
#
# The user's start through Tor, right after "bootstrapped 100% (Done)":
#
#     the VPN did not come up: 127.0.53.1: no answer from 127.0.53.1
#
# Which check said it: "SERVER: no answer from SERVER" is resolver_for's error around dns_lookup's timeout, raised -
# not recorded as a check - only by resolve_hosts, the lookups of the agent's names before the container says
# "ready". Before a5cf6273 those asked tor's DNSPort once, raw: tor gave no answer within 4 s right after its
# bootstrap and the start ended with exactly this message (shown below with that release's vpn_runtime.py). Since
# a5cf6273 they go through TorDns, which takes "no answer" as "not yet" like SERVFAIL; the current code cannot say
# this message for the start any more - the user's install was older. Left raw until now: the two .onion probes of
# the checks (a "no answer" there failed the start's checks) and the lookups of the launcher's requests while the
# tunnel serves (handle_request: sandbox-ai llm check through Tor). Both ask patiently now, within their budgets; a
# wrong answer is still a failure; the final failure says what was asked how often. The kill switch is untouched.

OLD_RUNTIME_COMMIT = "a5cf6273^"  # the last vpn_runtime.py before lookups through Tor became patient


def old_runtime(directory):
    """vpn_runtime.py as it was before a5cf6273, from git; None without that history (a shallow clone)."""
    import subprocess
    done = subprocess.run(["git", "-C", str(ROOT), "show", f"{OLD_RUNTIME_COMMIT}:sandbox/vpn_runtime.py"],
                          capture_output=True, text=True)
    if done.returncode != 0:
        return None
    path = directory / "old_vpn_runtime.py"
    path.write_text(done.stdout)
    return path


def start_through_tor(host, dns, runtime=None, timeout=240):
    host.add_image("localhost/claude-sandbox:latest")
    return host.run("claude", "--vpn", "tor-test", timeout=timeout, env={
        "FAKE_PODMAN_RUN": "0:0", "FAKE_TOR_SCHEDULE": json.dumps([[[1, 50, "Loading relay descriptors"],
                                                                    [3, 100, "Done"]]]),
        "FAKE_TOR_SPEED": "20", "FAKE_TOR_DNS": json.dumps(dns),
        **({"FAKE_TOR_RUNTIME": str(runtime)} if runtime else {})})


def test_e2e_before_the_fix_tor_without_an_answer_was_the_users_message(host, tmp_path):
    """The user's message, reproduced with the vpn_runtime.py from before a5cf6273: tor's DNSPort gives no answer
    after "bootstrapped 100% (Done)" - one raw try per name, and the start ended so."""
    runtime = old_runtime(tmp_path)
    if runtime is None:
        pytest.skip(f"no git history for {OLD_RUNTIME_COMMIT}")
    result = start_through_tor(host, {"silent_for": 100000}, runtime)
    err = result.stderr
    assert result.returncode != 0
    assert "vpn: tor: bootstrapped 100% (Done)" in err
    assert "the VPN did not come up: 127.0.53.1: no answer from 127.0.53.1" in err


def test_e2e_tor_without_an_answer_at_first_is_waited_for_and_the_start_goes_on(host):
    """Now: tor's DNSPort silent for 20 s after the bootstrap (the .onion probes' lookups too) - asked again, said
    as progress, and the sandbox runs."""
    result = start_through_tor(host, {"silent_for": 20})
    err = result.stderr
    assert result.returncode == 0, err
    assert "no answer from 127.0.53.1" not in err
    assert "Tor: waiting for its first circuit (DNS through Tor: no answer, try 1)" in err
    assert "fake podman: the sandbox runs" in err
    asked = [e for e in tor_events(host) if "dns" in e]
    assert any(e["answer"] == "none" for e in asked) and asked[-1]["answer"] == "ok"
    # Every lookup went to tor's DNSPort (the .onion probe's outside resolver is redirected there by the rules).
    assert {e["server"] for e in asked if not e["dns"].endswith(".onion")} == {rt.TOR_DNS}


def test_e2e_tor_that_never_answers_says_what_was_asked_how_often(host):
    result = start_through_tor(host, {"silent_for": 100000})
    err = result.stderr
    assert result.returncode != 0 and "fake podman: the sandbox runs" not in err
    assert "the VPN did not come up: 127.0.53.1: no answer from 127.0.53.1" not in err
    assert "tor has bootstrapped, but its DNSPort 127.0.53.1 answered no lookup of" in err
    # What was asked how often: the agent's name, and every unanswered lookup since the first.
    assert re.search(r"\(\d+ tr(y|ies) in \d+ s: no answer \d+x; \d+ lookups of any name in \d+ s since the first "
                     r"unanswered one: no answer \d+x\)", err), err


class SilentPort:
    """tor's DNSPort right after its bootstrap: no answer for the first SILENT lookups (dns_lookup's timeout), then
    ANSWER for .onion names (its own mapping) and 192.0.2.1 for others."""

    def __init__(self, clock, silent, answer=("10.192.0.7",)):
        self.clock, self.silent, self.answer, self.asked = clock, silent, list(answer), []

    def __call__(self, name, server, qtype, timeout=4.0, port=53):
        self.asked.append((name, server))
        self.clock.now += timeout
        if len(self.asked) <= self.silent:
            raise TimeoutError(f"no answer from {server}")
        return list(self.answer) if name.endswith(".onion") else ["192.0.2.1"]


class FakeClock:
    def __init__(self):
        self.now, self.slept = 0.0, []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


TOR_TUNNEL = {"interface": f"TransPort 127.0.0.1:{rt.TOR_TRANS_PORT}", "implementation": "tor", "ipv4": True,
              "ipv6": False, "dns": [rt.TOR_DNS], "tor": {"uid": rt.TOR_UID, "bridges": 0}}


def onion_checks(monkeypatch, port, clock):
    monkeypatch.setattr(rt, "dns_lookup", port)
    monkeypatch.setattr(rt, "probe_ipv6", lambda timeout: {"error": "Operation not permitted", "kind": "blocked"})
    dns = rt.TorDns(clock=clock, sleep=clock.sleep)
    return rt.gather_checks({"checks": {"ip": False, "timeout": 2}}, TOR_TUNNEL, None, dns=dns), dns


def test_the_onion_probes_wait_for_an_answer_too(monkeypatch):
    """The .onion probes asked tor's DNSPort once, raw: right after the bootstrap "no answer from 127.0.53.1" failed
    the start's checks. They are asked again now, as every lookup through tor; an answer maps them as before."""
    clock = FakeClock()
    port = SilentPort(clock, silent=3)
    result, dns = onion_checks(monkeypatch, port, clock)
    assert result["dns"]["onion"] == {"tor": ["10.192.0.7"], "outside": ["10.192.0.7"]}
    assert result["dns"]["answers"] == {rt.TOR_DNS: True}
    assert clock.slept and all(server in (rt.TOR_DNS, rt.OUTSIDE_RESOLVER) for _, server in port.asked)
    vpn = load("sandbox_vpn_tor_regression", "sandbox/vpn.py")
    assert vpn.tor_dns_failures(result["dns"]) == []
    # An .onion answer proves no circuit to an exit: the wait for the first circuit keeps its own patience.
    clock = FakeClock()
    alone = rt.TorDns(clock=clock, sleep=clock.sleep, ask=SilentPort(clock, silent=0))
    alone.lookup(rt.ONION_PROBE, rt.TOR_DNS, 1)
    assert alone.circuit is False


def test_a_wrong_onion_answer_is_still_a_failure_and_no_answer_says_how_often(monkeypatch):
    clock = FakeClock()
    result, _ = onion_checks(monkeypatch, SilentPort(clock, silent=0, answer=("192.0.2.66",)), clock)
    vpn = load("sandbox_vpn_tor_regression_wrong", "sandbox/vpn.py")
    failures = vpn.tor_dns_failures(result["dns"])
    assert any("did not map" in failure and "192.0.2.66" in failure for failure in failures), failures
    clock = FakeClock()
    result, _ = onion_checks(monkeypatch, SilentPort(clock, silent=10 ** 6), clock)
    onion = result["dns"]["onion"]["tor"]
    assert isinstance(onion, str) and onion != "no answer from 127.0.53.1"
    assert f"tor's DNSPort {rt.TOR_DNS} did not map {rt.ONION_PROBE}" in onion
    assert re.search(r"\(\d+ tr(y|ies) in \d+ s: no answer \d+x; \d+ lookups of any name in \d+ s", onion), onion
    assert "kill switch held" in onion
    assert "redirected there" in result["dns"]["onion"]["outside"]


def test_the_launchers_requests_through_tor_wait_within_their_budget(monkeypatch):
    """handle_request's lookups (sandbox-ai llm check through Tor) asked tor's DNSPort once, raw; now patiently,
    within TOR_DNS_REQUEST_BUDGET (the launcher waits 15 s beyond the request's own timeout)."""
    clock = FakeClock()
    monkeypatch.setattr(rt, "time", types.SimpleNamespace(monotonic=clock, sleep=clock.sleep))
    port = SilentPort(clock, silent=1)
    monkeypatch.setattr(rt, "dns_lookup", port)
    answer = rt.handle_request(json.dumps({"op": "resolve", "id": 1, "names": ["api.example.org"]}), TOR_TUNNEL,
                               False)
    assert answer == {"event": "answer", "id": 1, "hosts": {"api.example.org": ["192.0.2.1"]}}
    assert len(port.asked) == 2
    clock = FakeClock()
    monkeypatch.setattr(rt, "time", types.SimpleNamespace(monotonic=clock, sleep=clock.sleep))
    monkeypatch.setattr(rt, "dns_lookup", SilentPort(clock, silent=10 ** 6))
    answer = rt.handle_request(json.dumps({"op": "resolve", "id": 2, "names": ["api.example.org"]}), TOR_TUNNEL,
                               False)
    assert "tor resolves other names, but not api.example.org" in answer["error"]
    assert "no answer" in answer["error"] and clock.now <= rt.TOR_DNS_REQUEST_BUDGET + 4
    # Without Tor nothing changes: one try, its error as it is.
    plain = {**TOR_TUNNEL, "tor": None, "dns": ["10.64.0.1"]}
    answer = rt.handle_request(json.dumps({"op": "resolve", "id": 3, "names": ["a.example"]}), plain, False)
    assert answer["error"] == "10.64.0.1: no answer from 10.64.0.1"
