"""Hotfix 0.1.96: the second, independent check of the sandbox's own browser (--browser) and the browser behind Tor
(user: "bitte auch den Browser / Browser mit Tor-Support in-depth prüfen"). One test per bug found, named after it.

Where something can be executed here it is: the browser container's display is the real Xvfb and the real x11vnc of
the image's Debian release, started by the real runtime.start_gui and browser_runtime.main (only the browser binary is
a stand-in: there is no Chromium or Firefox here), and what another process of the pod - the agent - can reach of it
is probed with real sockets. The start itself goes through the real ./sandbox-ai with only Podman faked
(live_fakes.py)."""
import importlib.util
import json
import os
from pathlib import Path
import random
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import types

import pytest

from live_fakes import Host, ROOT


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runtime = load("sandbox_runtime_hotfix_browser", "sandbox/runtime.py")
browser_runtime = load("sandbox_browser_runtime_hotfix_browser", "sandbox/browser_runtime.py")

DISPLAY_TOOLS = all(shutil.which(tool) for tool in ("Xvfb", "x11vnc", "setpriv"))
needs_display = pytest.mark.skipif(not DISPLAY_TOOLS, reason="needs Xvfb, x11vnc and setpriv (the browser image's)")


def free_display():
    """A display number no X server of this machine uses (the X socket's folder is the machine's)."""
    for _ in range(200):
        number = random.randint(150, 900)
        if not Path(f"/tmp/.X11-unix/X{number}").exists() and not Path(f"/tmp/.X{number}-lock").exists():
            return f":{number}"
    pytest.skip("no free display number")


@pytest.fixture
def vnc_dir():
    """A short folder for the VNC socket (a Unix socket's path is at most 107 bytes), only this user's."""
    folder = Path(tempfile.mkdtemp(prefix="hb-vnc-"))
    folder.chmod(0o700)
    yield folder
    shutil.rmtree(folder, ignore_errors=True)


def inet_sockets(pid):
    """[(protocol, local address)] of the TCP listeners and bound UDP sockets of process PID (/proc: its sockets'
    inodes in this network namespace's tables)."""
    inodes = set()
    for entry in os.listdir(f"/proc/{pid}/fd"):
        try:
            target = os.readlink(f"/proc/{pid}/fd/{entry}")
        except OSError:
            continue
        if target.startswith("socket:["):
            inodes.add(target[8:-1])
    found = []
    for table in ("tcp", "tcp6", "udp", "udp6"):
        try:
            lines = Path(f"/proc/net/{table}").read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if fields[9] in inodes and (table.startswith("udp") or fields[3] == "0A"):
                found.append((table, fields[1]))
    return found


def abstract_listeners(pid):
    """The abstract Unix sockets (reached from every process of the network namespace) of process PID."""
    inodes = set()
    for entry in os.listdir(f"/proc/{pid}/fd"):
        try:
            target = os.readlink(f"/proc/{pid}/fd/{entry}")
        except OSError:
            continue
        if target.startswith("socket:["):
            inodes.add(target[8:-1])
    found = []
    for line in Path("/proc/net/unix").read_text().splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 8 and fields[6] in inodes and fields[7].startswith("@"):
            found.append(fields[7])
    return found


def rfb_banner(family, address, timeout=2):
    """The first bytes a TCP server at ADDRESS sends (an RFB server: "RFB 003.008\\n"), or None when nothing
    answers there."""
    with socket.socket(family) as probe:
        probe.settimeout(timeout)
        try:
            probe.connect(address)
            return probe.recv(12)
        except OSError:
            return None


# == The agent reached the display of the browser that is only yours (--browser-control human) =====================

@needs_display
def test_the_agent_cannot_reach_the_display_of_a_browser_that_is_only_yours(vnc_dir, monkeypatch):
    """--browser-control human promises "the agent reaches nothing of it: no DevTools port, no X socket, no VNC
    socket". x11vnc was started with -rfbport 0 for "no TCP listener" - which ends only its IPv4 listener: it still
    listened on [::]:5900, without a password, in the pod's network namespace the agent shares. The agent connected
    to [::1]:5900 and saw your browser's screen and drove its mouse and keyboard (your signed-in sites included).
    Run here with the real Xvfb and x11vnc as the browser's container starts them: no TCP or UDP socket of either,
    no abstract socket, nothing answers on 5900 - only the private Unix socket."""
    monkeypatch.setattr(runtime, "VNC_DIR", vnc_dir)
    display = free_display()
    processes = runtime.start_gui({"display": display, "screen": "320x200x24", "vnc": True, "private": True,
                                   "selection": True})
    try:
        assert len(processes) == 2
        for process in processes:
            assert process.poll() is None
            assert inet_sockets(process.pid) == [], (process.args, inet_sockets(process.pid))
            assert abstract_listeners(process.pid) == [], (process.args, abstract_listeners(process.pid))
        for family, address in ((socket.AF_INET6, ("::1", 5900)), (socket.AF_INET, ("127.0.0.1", 5900))):
            banner = rfb_banner(family, address)
            assert banner is None or not inet_sockets_of_port(processes, 5900), (address, banner)
        with socket.socket(socket.AF_UNIX) as own:  # the app's way in: the socket in the folder only you can open
            own.settimeout(5)
            own.connect(str(vnc_dir / "vnc.sock"))
            assert own.recv(12) == b"RFB 003.008\n"
    finally:
        for process in reversed(processes):
            runtime.stop_process(process)


def inet_sockets_of_port(processes, port):
    return [entry for process in processes for entry in inet_sockets(process.pid)
            if int(entry[1].rsplit(":", 1)[1], 16) == port]


@needs_display
@pytest.mark.parametrize("victim", ["x11vnc", "Xvfb"])
def test_the_browsers_display_comes_back_when_its_x11vnc_or_xvfb_ends(victim, vnc_dir, tmp_path, monkeypatch):
    """The browser's container watched only the browser (started again when it ends): when x11vnc or Xvfb ended (a
    crash, the out-of-memory killer in a container of 2 GB beside a browser), the container ran on without a display -
    the app's Browser tab said "closed", Reconnect was refused, for the rest of the run. Run here with the real
    browser_runtime.main, its real display and a stand-in browser: the display is started again (its stale sockets
    gone first), the browser with it, and the VNC socket answers again."""
    mount = tmp_path / "mount"
    mount.mkdir()
    (mount / "vnc").symlink_to(vnc_dir)  # (the socket's path must stay short)
    display = free_display()
    (mount / "config.json").write_text(json.dumps({
        "kind": "chromium", "control": "human", "screen": "320x200x24", "display": display, "cdp_port": None,
        "profile": "fresh", "tor": False}))
    real = load("sandbox_runtime_hotfix_browser_main", "sandbox/runtime.py")
    displays, browsers, handlers = [], [], []
    start_gui = real.start_gui
    real.start_gui = lambda cfg: displays.append(start_gui(cfg)) or displays[-1]

    class Browser:
        def __init__(self, argv, **_kwargs):
            assert os.environ["DISPLAY"] == display
            self.argv, self.ended = argv, None
            browsers.append(self)

        def poll(self):
            return self.ended

        def terminate(self):
            self.ended = -15

        def wait(self, timeout=None):
            return self.ended

        stdin = None
    monkeypatch.setattr(browser_runtime.signal, "signal", lambda number, handler: handlers.append(handler))
    monkeypatch.setattr(browser_runtime.os, "environ", dict(browser_runtime.os.environ))
    steps = {"killed": None, "deadline": time.monotonic() + 40, "seen": []}

    def answers():
        with socket.socket(socket.AF_UNIX) as own:
            own.settimeout(2)
            try:
                own.connect(str(vnc_dir / "vnc.sock"))
                return own.recv(12) == b"RFB 003.008\n"
            except OSError:
                return False

    def tick(_seconds):
        time.sleep(0.2)
        assert time.monotonic() < steps["deadline"], f"the display did not come back: {steps}"
        if steps["killed"] is None and answers():
            process = displays[-1][0 if victim == "Xvfb" else 1]
            steps["killed"] = process.pid
            process.kill()
            process.wait()
        elif steps["killed"] is not None and len(displays) == 2 and answers() and len(browsers) == 2:
            handlers[0]()  # SIGTERM: the run ends
    try:
        assert browser_runtime.main(mount, tmp_path / "home", real, Browser, tick) == 0
    finally:
        for processes in displays:
            for process in processes:
                real.stop_process(process)
        for stale in (Path(f"/tmp/.X11-unix/X{display[1:]}"), Path(f"/tmp/.X{display[1:]}-lock")):
            if stale.exists() and not any(process.poll() is None for processes in displays for process in processes):
                stale.unlink()
    assert len(displays) == 2, "the display was started again"
    assert all(process.poll() is not None for process in displays[0] + displays[1])
    assert browsers[0].ended is not None and len(browsers) == 2, "the browser on the new display"


def test_a_display_that_listens_on_the_network_stops_the_start(monkeypatch, tmp_path):
    """Fail closed: should an x11vnc (or Xvfb) still open a TCP or UDP socket - another release, another default - the
    display is not used: start_gui stops what it started and the start ends saying what listened where."""
    class Process:
        def __init__(self, argv):
            self.args, self.pid = argv, 4242 + len(started)

        def poll(self):
            return None
    started, stopped = [], []
    monkeypatch.setattr(runtime.subprocess, "Popen", lambda argv, **kwargs: started.append(Process(argv)) or
                        started[-1])
    monkeypatch.setattr(runtime, "x_socket", lambda display: tmp_path)
    monkeypatch.setattr(runtime, "wait_for_unix_listener", lambda *args: None)
    monkeypatch.setattr(runtime, "stop_process", stopped.append)
    monkeypatch.setattr(runtime, "VNC_DIR", tmp_path / "vnc")
    monkeypatch.setattr(runtime, "inet_listeners", lambda pid: ["tcp6 [::]:5900"] if pid == 4243 else [])
    with pytest.raises(RuntimeError, match=r"x11vnc listens on the network \(tcp6 \[::\]:5900\)"):
        runtime.start_gui({"display": ":42", "screen": "320x200x24", "vnc": True, "private": True})
    assert stopped == started and len(started) == 2


def test_the_listeners_of_a_process_are_read_from_proc():
    """inet_listeners (the check above) on a real process: its TCP listener and its bound UDP socket, named."""
    child = subprocess.Popen([sys.executable, "-c", "import socket, sys, time\n"
                              "t = socket.socket(socket.AF_INET6); t.bind(('::1', 0)); t.listen()\n"
                              "u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); u.bind(('127.0.0.1', 0))\n"
                              "print(t.getsockname()[1], u.getsockname()[1], flush=True); time.sleep(30)"],
                             stdout=subprocess.PIPE, text=True)
    try:
        tcp, udp = child.stdout.readline().split()
        assert sorted(runtime.inet_listeners(child.pid)) == sorted([f"tcp6 [::1]:{tcp}", f"udp 127.0.0.1:{udp}"])
    finally:
        child.kill()
        child.wait()
    assert runtime.inet_listeners(child.pid)[0].startswith("unknown")  # gone: cannot be told, so not trusted


# == The browser's profile: a folder of yours (--browser-profile DIR) ==============================================

@pytest.fixture
def browser_host(tmp_path):
    made = Host(tmp_path)
    made.add_image("localhost/claude-sandbox:latest")
    made.add_image("localhost/sandbox-ai-browser:latest", labels={"sandbox-ai.role": "browser"})
    return made


def running_browsers(host):
    """The browser display folders of the runs that run now (RUN.browser-vnc beside each run's directory)."""
    runs = host.runtime / "sandbox-ai" / "runs"
    return list(runs.glob("*.browser-vnc")) if runs.is_dir() else []


def test_e2e_a_folder_profile_is_used_by_one_run_at_a_time(browser_host, tmp_path):
    """Two starts with the same --browser-profile DIR (two sessions of one project, say) both ran, and each
    browser's runtime cleared the other's profile lock at its start ("a lock of an earlier run"): two Chromiums
    wrote one profile at once - its cookies, logins and history databases - and corrupted it without a word. Through
    the real ./sandbox-ai: the second start is refused while the first runs, naming it; after it, the folder is free
    again."""
    profile = tmp_path / "profile"
    profile.mkdir(mode=0o700)
    first = browser_host.popen("claude", "--browser", "--browser-profile", profile, env={"FAKE_PODMAN_RUN": "0:6"})
    try:
        deadline = time.monotonic() + 60
        while not running_browsers(browser_host) and first.poll() is None and time.monotonic() < deadline:
            time.sleep(0.1)
        assert running_browsers(browser_host), "the first start runs"
        second = browser_host.run("claude", "--browser", "--browser-profile", profile, env={"FAKE_PODMAN_RUN": "0:0"})
        assert second.returncode != 0, second.stderr[-800:]
        assert f"--browser-profile {profile} is in use by another run (PID " in second.stderr, second.stderr[-800:]
        assert "fake podman: the sandbox runs" not in second.stderr
    finally:
        _, err = first.communicate(timeout=120)
    assert first.returncode == 0 and "fake podman: the sandbox runs" in err, err[-800:]
    third = browser_host.run("claude", "--browser", "--browser-profile", profile, env={"FAKE_PODMAN_RUN": "0:0"})
    assert third.returncode == 0 and "fake podman: the sandbox runs" in third.stderr, third.stderr[-800:]


def test_e2e_a_folder_profile_others_could_open_is_made_yours_alone(browser_host, tmp_path):
    """A folder of yours as the profile, made with a plain mkdir (0755): the browser keeps your logins, cookies and
    history there - Firefox's databases readable by every user of the machine (0644 in a folder others can open).
    The start makes it yours alone (0700), as it does with a profile it makes itself."""
    profile = tmp_path / "profile"
    profile.mkdir()
    profile.chmod(0o755)
    done = browser_host.run("claude", "--browser", "firefox", "--browser-profile", profile,
                            env={"FAKE_PODMAN_RUN": "0:0"})
    assert done.returncode == 0, done.stderr[-800:]
    assert oct(profile.stat().st_mode & 0o777) == "0o700"


# == What the start says of the browser ============================================================================

def test_e2e_the_start_of_a_browser_only_the_agent_drives_does_not_promise_the_browser_button(browser_host):
    """--browser-control agent: the start said "driven by the agent through the DevTools Protocol at ... (the app's
    Browser button shows it)" - but its run records no display for you (run.json "vnc": null) and the app's Browser
    says "Only the agent drives this browser: it has no display for you" (as the README says). The start now says
    that, and how to watch it (both)."""
    done = browser_host.run("claude", "--browser", "--browser-control", "agent", "--dry-run")
    assert done.returncode == 0, done.stderr[-800:]
    said = [line for line in done.stderr.splitlines() if line.startswith("agent-sandbox: browser: Chromium")]
    assert len(said) == 1 and "Browser button shows it" not in said[0], said
    assert "no display for you" in said[0] and "--browser-control both" in said[0], said
    browser = load("sandbox_browser_hotfix_browser", "sandbox/browser.py")
    a = types.SimpleNamespace(browser="chromium", browser_control="agent", browser_profile=None,
                              browser_container={"name": "browser"}, control_dir=Path("/run/x/runs/r"), run_id="r")
    assert browser.record(a, "pod")["vnc"] is None


@pytest.fixture
def tor_host(browser_host):
    browser_host.add_image("localhost/sandbox-ai-tor:latest")
    added = browser_host.run("vpn", "add", "tor-test", "--tor", "--check-url", "https://check.torproject.org/api/ip",
                             "--no-dns-probe")
    assert added.returncode == 0, added.stderr
    return browser_host


@pytest.mark.parametrize("profile", ["session", "DIR"])
def test_e2e_behind_tor_a_kept_profile_is_said_to_link_the_runs(tor_host, tmp_path, profile):
    """Behind Tor the start says the browser is not Tor Browser (fingerprinting, signing in). A kept profile
    (--browser-profile session or a folder) is worse and was not said: it brings the cookies, logins, history and
    cache of the earlier runs - with Tor or without, from your own address - into this one, so a site links this
    run's visits to them at once. Said now, with what helps (fresh)."""
    value = str(tmp_path / "profile") if profile == "DIR" else profile
    options = ["--browser", "firefox", "--browser-profile", value] + (["--session", "shop"] if profile == "session"
                                                                       else [])
    done = tor_host.run("claude", "--vpn", "tor-test", *options, "--dry-run")
    assert done.returncode == 0, done.stderr[-800:]
    said = " ".join(line for line in done.stderr.splitlines() if line.startswith("agent-sandbox: browser:"))
    assert "not Tor Browser" in said
    assert "earlier runs" in said and "--browser-profile fresh" in said, said
    fresh = tor_host.run("claude", "--vpn", "tor-test", "--browser", "firefox", "--dry-run")
    assert "earlier runs" not in fresh.stderr  # fresh: nothing to link
    plain = tor_host.run("claude", "--browser", "firefox", "--browser-profile", str(tmp_path / "p2"), "--dry-run")
    assert "earlier runs" not in plain.stderr  # without Tor nothing is hidden that a kept profile gives away


# == Firefox: nothing of its own on the network ====================================================================

def firefox_prefs_of(cfg, tmp_path, monkeypatch):
    """user.js as browser_runtime.main writes it for CFG (the launcher's configuration) into a fresh profile."""
    mount, home = tmp_path / "mount", tmp_path / "home"
    (mount / "vnc").mkdir(parents=True)
    (mount / "config.json").write_text(json.dumps(cfg))
    handlers = []
    runtime_stub = types.SimpleNamespace(start_gui=lambda c: [], stop_process=lambda p: None, VNC_DIR=None)
    monkeypatch.setattr(browser_runtime.signal, "signal", lambda number, handler: handlers.append(handler))
    monkeypatch.setattr(browser_runtime.os, "environ", dict(browser_runtime.os.environ))
    assert browser_runtime.main(mount, home, runtime_stub, lambda argv, **kw: types.SimpleNamespace(
        poll=lambda: None), lambda _: handlers[0]()) == 0
    found = {}
    for line in (home / "profile" / "user.js").read_text().splitlines():
        if line.startswith("user_pref("):
            name, _, value = line[len("user_pref("):-2].partition(", ")
            found[json.loads(name)] = json.loads(value)
    return found


def test_e2e_firefox_asks_no_mozilla_server_for_remote_settings_by_itself(tor_host, tmp_path, monkeypatch):
    """"Nothing of its own on the network" (browser.py; the README: the browsers' own background traffic is
    switched off): Firefox ESR still fetched Remote Settings by itself (firefox.settings.services.mozilla.com and
    its attachments' CDN: blocklists, certificate intermediates and CRLite, the translation models, query
    stripping lists - an approval question for a host you never opened, and behind Tor a call every run makes),
    connected to mitm-detection.services.mozilla.com after a certificate error (the TLS-inspecting proxy is what
    it looks for), and sent its private attribution reports (Firefox 128's PPA) by itself. The runtime's user.js
    points Remote Settings nowhere (as Mozilla's own test profiles do) and turns the others off - behind Tor and
    without. Configuration only: there is no Firefox here (see the release notes for the live check)."""
    for options in (["--vpn", "tor-test"], []):
        done = tor_host.run("claude", *options, "--browser", "firefox", "--dry-run")
        assert done.returncode == 0, done.stderr[-800:]
        cfg = json.loads(done.stdout)["browser"]["config"]
        prefs = firefox_prefs_of(cfg, tmp_path / str(len(options)), monkeypatch)
        assert prefs["services.settings.server"] == "data:,#remote-settings-dummy/v1"
        assert prefs["security.certerrors.mitm.priming.enabled"] is False
        assert prefs["dom.private-attribution.submission.enabled"] is False
        assert prefs["browser.translations.enable"] is False and prefs["browser.ml.enable"] is False
        assert prefs["captivedetect.canonicalURL"] == "" and prefs["browser.topsites.contile.enabled"] is False
