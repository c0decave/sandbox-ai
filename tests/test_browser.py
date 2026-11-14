"""The sandbox's own browser (--browser [chromium|firefox], --browser-control, --browser-profile), with podman replaced
by recorded answers: the options and their short forms, the launcher's checks (backend, remote engine, Firefox and
the agent's control, macOS, profiles, ports), the browser's podman line (the pod's network, its mounts, the CA, no
TCP port, DevTools only for agent and both), the handshake with the supervisor (the CA shared before the go), the dry
run, run.json, the agent's environment, the security modes, project files and presets, the image's build (its base
pinned by digest), and the runtime inside its container (browser_runtime.py)."""
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import home_guard  # noqa: E402

home_guard.ensure()


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


s = load("launcher_for_browser_tests", "sandbox/agent_sandbox.py")
b = load("browser_tests", "sandbox/browser.py")
sv = load("services_for_browser_tests", "sandbox/services.py")
rt = load("browser_runtime_tests", "sandbox/browser_runtime.py")
b._launcher = s
sv._launcher = s
sv._browser = b
IMAGE_ID = "e" * 64
CA = b"-----BEGIN CERTIFICATE-----\nMIIBfake\n-----END CERTIFICATE-----\n"
KEY_BLOCK = b"-----BEGIN " + b"PRIVATE KEY-----\nx\n-----END " + b"PRIVATE KEY-----\n"  # (split: no key marker in the source)


@pytest.fixture(autouse=True)
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    (tmp_path / "runtime").mkdir()
    (tmp_path / "state" / "sandbox-ai").mkdir(parents=True)
    (tmp_path / "state" / "sandbox-ai" / "exposed.json").write_text("[]")
    monkeypatch.setattr(s, "load_browser_module", lambda: b)
    monkeypatch.setattr(s, "load_services_module", lambda: sv)
    monkeypatch.setattr(s, "on_machine", lambda a: False)
    return tmp_path


def start(*options, tor=False, keep=False):
    a = s.parse_args(["claude", *options])
    a.vpn_profile = {"type": "tor", "name": "t"} if tor else None
    a.run_id = "claude-1234abcd"
    a.keep_home = keep
    a.services = []
    return a


# -- the options --

def test_browser_alone_is_chromium_and_the_kind_can_be_named():
    assert s.parse_args(["claude", "--browser"]).browser == "chromium"
    assert s.parse_args(["claude", "--browser", "firefox"]).browser == "firefox"
    assert s.parse_args(["claude", "-bw", "firefox", "-bc", "human", "-bp", "session"]).browser_profile == "session"
    plain = s.parse_args(["claude"])
    assert plain.browser is None and plain.browser_control is None and plain.browser_profile is None
    with pytest.raises(SystemExit):
        s.parse_args(["claude", "--browser", "opera"])
    with pytest.raises(SystemExit):
        s.parse_args(["claude", "--browser", "chromium", "--browser-control", "robot"])


def test_canonical_options_never_take_the_next_option_for_the_browsers_kind():
    """--browser has an optional value: written --browser=chromium when it has none, so that whatever walks the options
    (project files, the modes, sessions, the app) never takes the next option for it."""
    assert s.canonical_options(["--browser", "--gui"]) == ["--browser=chromium", "--gui"]
    assert s.canonical_options(["--browser"]) == ["--browser=chromium"]
    assert s.canonical_options(["-bw", "--", "-p", "x"]) == ["--browser=chromium", "--", "-p", "x"]
    assert s.canonical_options(["--browser", "firefox", "-bc", "agent"]) == ["--browser", "firefox",
                                                                              "--browser-control", "agent"]
    assert s.canonical_options(["-bw", "firefox"]) == ["--browser", "firefox"]
    assert s.parse_args(["claude", *s.canonical_options(["--browser", "--gui"])]).browser == "chromium"
    modes = s.MODES
    assert modes.option_items(s, ["--browser", "--gui"]) == [("--browser", "chromium"), ("--gui", None)]


def test_help_names_the_options_in_their_group():
    text = s.build_parser().format_help()
    assert "-bw, --browser [KIND]" in text and "-bc, --browser-control" in text and "-bp, --browser-profile" in text


# -- the launcher's checks --

def test_check_happy():
    a = start("--browser")
    found = b.check(a, s)
    assert found is a.browser_container and found["name"] == "browser" and found["browser"]
    assert found["control"] == "human" and found["ports"] == [] and found["ready"] == "none"
    assert found["user"] == "0:0" and found["profile"] == "fresh"
    agent = start("--browser", "--browser-control", "agent")
    found = b.check(agent, s)
    assert found["ports"] == [9222] and found["ready"] == 9222 and found["ready_timeout"] == b.READY_TIMEOUT
    assert b.check(start(), s) is None and start().browser is None


@pytest.mark.parametrize("options, match", [
    (["--backend", "bwrap"], "Podman backend"),
    (["--podman-url", "unix:///x.sock"], "--podman-url"),
    (["--browser-control", "agent"], None),
])
def test_check_refuses_the_setups_it_cannot_serve(options, match):
    a = start("--browser", *options)
    if match is None:
        assert b.check(a, s)["control"] == "agent"
    else:
        with pytest.raises(ValueError, match=match):
            b.check(a, s)


@pytest.mark.parametrize("options, match", [
    (["--browser", "firefox", "--browser-control", "agent"], "needs Chromium"),
    (["--browser", "firefox", "--browser-control", "both"], "needs Chromium"),
    (["--browser-control", "agent"], "add --browser"),
    (["--browser-profile", "session"], "add --browser"),
    (["--browser", "--browser-profile", "session"], "keeps none"),
    (["--browser", "--browser-profile", "relative/dir"], "absolute folder"),
    (["--browser", "--browser-profile", "/a,b"], "absolute folder"),
    (["--browser", "--browser-control", "agent", "--allow-host-port", "9222"], "port of the host"),
])
def test_check_refuses(options, match):
    with pytest.raises(ValueError, match=match):
        b.check(start(*options), s)


def test_check_edges(tmp_path, monkeypatch):
    # a profile of yours: a folder outside the agent's reach (not a workspace, not in one)
    folder = tmp_path / "profiles" / "chromium"
    a = start("--browser", "--browser-profile", str(folder), "--workspace", str(tmp_path / "work"))
    assert b.check(a, s)["profile_dir"] == str(folder)
    inside = start("--browser", "--browser-profile", str(tmp_path / "work" / "p"), "--workspace", str(tmp_path / "work"))
    with pytest.raises(ValueError, match="outside the workspace"):
        b.check(inside, s)
    (tmp_path / "file").write_text("x")
    with pytest.raises(ValueError, match="not a folder"):
        b.check(start("--browser", "--browser-profile", str(tmp_path / "file")), s)
    # a session profile in a start that keeps its HOME (a session, a kept start)
    assert b.check(start("--browser", "--browser-profile", "session", keep=True), s)["profile"] == "session"
    supervisor = start("--browser", "--browser-profile", "session", "--state-dir", str(tmp_path / "s" / "home"))
    supervisor.tmux_socket_dir, supervisor.start_id = tmp_path / "s" / "tmux", "brisk-otter"
    assert b.check(supervisor, s)["profile"] == "session"
    # macOS: the live view is Linux-only; the agent's control works there
    monkeypatch.setattr(s, "on_machine", lambda a: True)
    with pytest.raises(ValueError, match="Linux-only"):
        b.check(start("--browser"), s)
    with pytest.raises(ValueError, match="Linux-only"):
        b.check(start("--browser", "--browser-control", "both"), s)
    assert b.check(start("--browser", "--browser-control", "agent"), s)["control"] == "agent"
    monkeypatch.setattr(s, "on_machine", lambda a: False)
    # behind Tor it is allowed, and said not to be Tor Browser
    tor = start("--browser", tor=True)
    b.check(tor, s)
    assert any("not Tor Browser" in note for note in b.notes(tor))
    # an MCP server's or a helper's sandbox has none
    helper = start("--browser")
    helper.helper_of = "lead"
    with pytest.raises(ValueError, match="an agent's start"):
        b.check(helper, s)


def test_a_service_cannot_take_the_browsers_name_or_port():
    a = start("--browser", "--browser-control", "agent")
    a.services = [{"name": "browser", "ports": []}]
    with pytest.raises(ValueError, match="the sandbox's browser's"):
        b.check(a, s)
    a.services = [{"name": "devtools", "ports": [9222]}]
    with pytest.raises(ValueError, match="DevTools port"):
        b.check(a, s)
    human = start("--browser")
    human.services = [{"name": "devtools", "ports": [9222]}]
    assert b.check(human, s)  # no DevTools port without the agent's control


def test_notes_say_who_drives_it_and_where_its_traffic_goes(tmp_path):
    for control, words in (("human", "for you only"), ("agent", "driven by the agent"), ("both", "the agent can use")):
        a = start("--browser", "--browser-control", control)
        b.check(a, s)
        found = " ".join(b.notes(a))
        assert words in found and "passes the proxy" in found and "credentials redacted" in found
    kept = start("--browser", "--browser-profile", str(tmp_path / "p"))
    b.check(kept, s)
    assert any(str(tmp_path / "p") in note for note in b.notes(kept))
    assert b.notes(start()) == []


# -- the container --

def checked(*options, **kwargs):
    a = start(*options, **kwargs)
    b.check(a, s)
    a.browser_container["record"]["id"] = IMAGE_ID
    return a


def test_create_command_runs_in_the_pods_network_without_a_port(tmp_path):
    a = checked("--browser")
    a.control_dir = tmp_path / "runs" / "claude-1234abcd"
    a.control_dir.mkdir(parents=True)
    argv, env = b.create_command(a, "agent-claude-1234abcd", a.browser_container, tmp_path, s,
                                 {"memory", "cpus", "pids"})
    joined = " ".join(argv)
    assert argv[:2] == ["podman", "create"] and "--pull=never" in argv and env == {}
    for flag in ("--cap-drop=ALL", "--security-opt=no-new-privileges", "--read-only", "--init"):
        assert flag in argv
    assert "--pod agent-claude-1234abcd" in joined and "--name agent-claude-1234abcd-service-browser" in joined
    assert "--user 0:0" in joined and "--memory 2g" in joined and "--pids-limit 1024" in joined
    # the pod's network: no network of its own, no port published, no host network
    for word in ("--network", "--publish", "-p ", "--net=", "--expose", "--cap-add", "--privileged"):
        assert word not in joined
    assert f"type=bind,src={tmp_path}/resolv.conf,dst=/etc/resolv.conf,ro,nodev" in joined
    assert f"type=bind,src={tmp_path}/hosts,dst=/etc/hosts,ro,nodev" in joined
    assert f"src={tmp_path}/browser.json,dst=/sandbox-browser/config.json,ro" in joined
    assert f"src={a.control_dir.with_name('claude-1234abcd.browser-vnc')},dst=/sandbox-browser/vnc,rw" in joined
    assert "dst=/sandbox-browser/runtime.py,ro" in joined and "dst=/sandbox-browser/browser_runtime.py,ro" in joined
    assert "ca.pem" not in joined  # no CA shared (no proxy): nothing mounted
    assert "dst=/sandbox-browser/profile" not in joined  # fresh: a tmpfs in its container
    assert argv[-3:] == [IMAGE_ID, "-I", "/sandbox-browser/browser_runtime.py"]
    argv, _ = b.create_command(a, "pod", a.browser_container, tmp_path, s, set())
    assert "--memory" not in argv and "--cpus" not in argv


def test_create_command_mounts_the_runs_ca_and_the_profile(tmp_path):
    a = checked("--browser", "--browser-profile", str(tmp_path / "profile"))
    a.control_dir = tmp_path / "runs" / "claude-1234abcd"
    a.control_dir.mkdir(parents=True)
    (a.control_dir / b.CA_FILE).write_bytes(CA)
    session = tmp_path / "session"
    session.mkdir()
    argv, _ = b.create_command(a, "pod", a.browser_container, session, s, set())
    joined = " ".join(argv)
    assert f"src={session}/browser-ca.pem,dst=/sandbox-browser/ca.pem,ro" in joined
    assert (session / "browser-ca.pem").read_bytes() == CA
    assert f"src={tmp_path / 'profile'},dst=/sandbox-browser/profile,rw" in joined
    with pytest.raises(ValueError, match="sandbox-ai build browser"):
        a.browser_container["record"]["id"] = None
        b.create_command(a, "pod", a.browser_container, session, s, set())


@pytest.mark.parametrize("data, ok", [
    (CA, True),
    (CA + KEY_BLOCK, False),  # never a key
    (CA + CA, False),  # one certificate only
    (b"junk", False),
    (b"-----BEGIN CERTIFICATE-----\n" + b"A" * (70 << 10) + b"\n-----END CERTIFICATE-----\n", False),
])
def test_read_ca(tmp_path, data, ok):
    (tmp_path / b.CA_FILE).write_bytes(data)
    assert (b.read_ca(tmp_path) == data) is ok
    assert b.read_ca(tmp_path / "none") is None
    (tmp_path / b.CA_FILE).unlink()
    (tmp_path / b.CA_FILE).symlink_to(tmp_path / "elsewhere")
    assert b.read_ca(tmp_path) is None


def test_prepare_needs_sandbox_ais_browser_image(tmp_path):
    a = start("--browser")
    b.check(a, s)

    def inspect(answer, code=0):
        return lambda argv, **kwargs: subprocess.CompletedProcess(argv, code, answer, "")
    assert b.prepare(a, s, inspect(f"sha256:{IMAGE_ID} browser\n")) == IMAGE_ID
    assert a.browser_container["record"]["id"] == IMAGE_ID
    with pytest.raises(ValueError, match="sandbox-ai build browser"):
        b.prepare(a, s, inspect("", 125))
    with pytest.raises(ValueError, match="sandbox-ai build browser"):
        b.prepare(a, s, inspect(f"sha256:{IMAGE_ID} vpn\n"))  # another image under that tag: not the browser
    with pytest.raises(ValueError, match="sandbox-ai build browser"):
        b.prepare(a, s, inspect("nothing\n"))
    assert b.prepare(start(), s, inspect("")) is None


def test_the_supervisor_waits_for_devtools_only_when_the_agent_drives_it():
    human = checked("--browser")
    assert sv.supervisor_config(human) == [{"name": "browser", "ready": None, "timeout": 90, "ca": True}]
    agent = checked("--browser", "--browser-control", "both")
    assert sv.supervisor_config(agent) == [{"name": "browser", "ready": 9222, "timeout": 90, "ca": True}]
    assert sv.members(start()) == []


def test_launch_starts_the_browser_like_a_service(tmp_path):
    a = checked("--browser")
    a.control_dir = tmp_path / "run"
    a.control_dir.mkdir()
    (a.control_dir / s.RUNTIME.SERVICES_GO).touch()
    (a.control_dir / b.CA_FILE).write_bytes(CA)
    calls = []

    def fake(argv, **kwargs):
        calls.append(argv)
        args = argv[1:]
        if args[:2] == ["pod", "inspect"]:
            return subprocess.CompletedProcess(argv, 0, "f" * 64 + "\n", "")
        if args[:3] == ["inspect", "--type", "container"]:
            return subprocess.CompletedProcess(argv, 0, "100\n200\n", "")
        return subprocess.CompletedProcess(argv, 0, "id\n", "")
    answer = sv.launch(a, "agent-x", tmp_path, lambda: True, s, fake, out=lambda _: None,
                       readlink=lambda path: "user:[1]")
    assert answer == {"ok": True, "services": ["browser"]}
    verbs = [c[1] for c in calls if c[1] in ("create", "init", "start")]
    assert verbs == ["create", "init", "start"]
    create = next(c for c in calls if c[1] == "create")
    assert "agent-x-service-browser" in create and "dst=/sandbox-browser/ca.pem,ro" in " ".join(create)


# -- the supervisor's half: the CA shared before the go --

def test_share_browser_ca(tmp_path):
    ca_dir, run = tmp_path / "ca", tmp_path / "run"
    ca_dir.mkdir()
    run.mkdir()
    assert s.RUNTIME.share_browser_ca(run, ca_dir) is False  # no proxy: no CA
    (ca_dir / "ca.pem").write_bytes(CA)
    assert s.RUNTIME.share_browser_ca(run, ca_dir) is True
    assert (run / "browser-ca.pem").read_bytes() == CA and s.RUNTIME.BROWSER_CA == b.CA_FILE
    (ca_dir / "ca.pem").write_bytes(CA + KEY_BLOCK)
    assert s.RUNTIME.share_browser_ca(tmp_path / "gone", ca_dir) is False


def test_await_services_shares_the_ca_before_the_go(tmp_path, monkeypatch):
    shared = []
    monkeypatch.setattr(s.RUNTIME, "share_browser_ca", lambda directory: shared.append(
        (directory / s.RUNTIME.SERVICES_GO).exists()))
    (tmp_path / s.RUNTIME.SERVICES_STARTED).write_text(json.dumps({"ok": True}))
    assert s.RUNTIME.await_services([{"name": "browser", "ready": None, "timeout": 90, "ca": True}], tmp_path) is None
    assert shared == [False]  # before the go: the launcher reads it once the go is there
    shared.clear()
    assert s.RUNTIME.await_services([{"name": "db", "ready": None, "timeout": 5}], tmp_path) is None
    assert shared == []


def test_the_browsers_display_has_no_abstract_socket_and_no_shared_memory(monkeypatch, tmp_path):
    started = []

    class Process:
        def poll(self):
            return None
    monkeypatch.setattr(s.RUNTIME.subprocess, "Popen", lambda argv, **kwargs: started.append(argv) or Process())
    monkeypatch.setattr(s.RUNTIME, "x_socket", lambda display: tmp_path)
    monkeypatch.setattr(s.RUNTIME, "wait_for_unix_listener", lambda *args: None)
    monkeypatch.setattr(s.RUNTIME, "VNC_DIR", tmp_path / "vnc")
    s.RUNTIME.start_gui({"display": ":42", "screen": "1280x800x24", "vnc": True, "private": True,
                         "selection": True})
    xvfb, vnc = started
    assert xvfb[xvfb.index("Xvfb"):][:4] == ["Xvfb", ":42", "-screen", "0"]
    assert "-nolisten" in xvfb and "local" in xvfb and "tcp" in xvfb and "MIT-SHM" in xvfb
    assert "-noshm" in vnc and "-nosel" not in vnc and "-rfbport" in vnc and vnc[vnc.index("-rfbport") + 1] == "0"
    assert str(tmp_path / "vnc" / "vnc.sock") in vnc
    started.clear()
    s.RUNTIME.start_gui({"display": ":99", "screen": "1920x1080x24", "vnc": True})  # the agent's: as before
    xvfb, vnc = started
    assert "local" not in xvfb and "-nosel" in vnc and "-noshm" not in vnc


# -- the dry run, run.json, the agent's environment --

def dry_run(capsys, *options):
    assert s.main(["claude", "--dry-run", *options]) == 0
    captured = capsys.readouterr()
    return json.loads(captured.out), captured.err


def test_dry_run_shows_the_browser(capsys, tmp_path, monkeypatch):
    (tmp_path / "work").mkdir()
    monkeypatch.chdir(tmp_path / "work")
    plan, err = dry_run(capsys, "--browser")
    browser = plan["browser"]
    assert browser["kind"] == "chromium" and browser["control"] == "human" and browser["image"] == b.IMAGE
    assert browser["network"].startswith("the pod's own") and browser["tcp_ports"] == [] and browser["cdp"] is None
    assert browser["display"]["tcp"] is False and browser["display"]["vnc_socket"].endswith(".browser-vnc/vnc.sock")
    assert browser["ca"].startswith("the run's CA") and "NSS" in browser["ca"]
    mounts = {m["container"]: m for m in browser["mounts"]}
    assert mounts["/sandbox-browser/vnc"]["mode"] == "rw" and mounts["/etc/resolv.conf"]["mode"] == "ro"
    assert mounts["/sandbox-browser/ca.pem"]["mode"] == "ro" and "/sandbox-browser/profile" not in mounts
    assert browser["container"].endswith("-service-browser") and browser["profile"]["kind"] == "fresh"
    assert "for you only" in err and "passes the proxy" in err
    plan, _ = dry_run(capsys, "--browser", "--browser-control", "agent")
    assert plan["browser"]["tcp_ports"] == [9222] and plan["browser"]["cdp"] == "http://127.0.0.1:9222"
    plan, _ = dry_run(capsys, "--browser", "firefox", "--no-proxy")
    assert plan["browser"]["ca"] is None and "/sandbox-browser/ca.pem" not in {
        m["container"] for m in plan["browser"]["mounts"]}
    plan, _ = dry_run(capsys)
    assert plan["browser"] is None
    plan, _ = dry_run(capsys, "--browser", "--browser-profile", str(tmp_path / "mine"))
    assert plan["browser"]["profile"] == {"kind": "dir", "where": str(tmp_path / "mine")}
    assert {m["container"]: m["host"] for m in plan["browser"]["mounts"]}["/sandbox-browser/profile"] == \
        str(tmp_path / "mine")


def test_the_agent_is_told_whether_it_may_drive_it():
    human = checked("--browser")
    env = s.agent_environment(human)
    assert env["SANDBOX_AI_BROWSER"] == "chromium" and env["SANDBOX_AI_BROWSER_CONTROL"] == "human"
    assert "SANDBOX_AI_BROWSER_CDP" not in env and "SANDBOX_AI_SERVICES" not in env
    agent = checked("--browser", "--browser-control", "agent")
    assert s.agent_environment(agent)["SANDBOX_AI_BROWSER_CDP"] == "http://127.0.0.1:9222"
    assert "SANDBOX_AI_BROWSER" not in s.agent_environment(start())


def test_record_and_starts_listing(tmp_path):
    st = load("starts_for_browser_tests", "sandbox/starts.py")
    a = checked("--browser", "--browser-control", "both")
    a.control_dir = tmp_path / "runs" / "claude-1234abcd"
    found = b.record(a, "agent-claude-1234abcd")
    assert found == {"kind": "chromium", "control": "both", "profile": "fresh",
                     "vnc": str(tmp_path / "runs" / "claude-1234abcd.browser-vnc" / "vnc.sock"),
                     "cdp": "http://127.0.0.1:9222", "container": "agent-claude-1234abcd-service-browser",
                     "tor": False}
    assert st.record_browser({"browser": found}) == found
    agent = checked("--browser", "--browser-control", "agent")
    agent.control_dir = a.control_dir
    assert b.record(agent, "pod")["vnc"] is None  # nothing for you to see: the app offers no Browser button
    for damaged in ({"browser": {"kind": "opera", "control": "human"}}, {"browser": "x"}, {},
                    {"browser": {"kind": "chromium", "control": "robot"}}):
        assert st.record_browser(damaged) is None
    odd = st.record_browser({"browser": {**found, "vnc": "relative/vnc.sock", "cdp": "http://evil:1"}})
    assert odd["vnc"] is None and odd["cdp"] is None


def test_a_start_with_the_browser(tmp_path, monkeypatch):
    """A real start (the pod replaced): the image checked before anything runs, the supervisor told to share the CA,
    run.json naming the browser and its socket, the agent told, the VNC folder made and removed with the run."""
    monkeypatch.setattr(s.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(s.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(s, "prepare_home", lambda *args: [])
    monkeypatch.setattr(s, "resolve_pod_hosts", lambda a, hosts: {host: ["203.0.113.5"] for host in hosts})
    prepared, seen = [], {}
    monkeypatch.setattr(b, "prepare", lambda a, l=None, run=None: prepared.append(a.browser_container["name"]))

    def run(a, session, home):
        seen["config"] = json.loads((session / "config.json").read_text())
        seen["browser.json"] = json.loads((session / "browser.json").read_text())
        seen["record"] = json.loads((a.control_dir / "run.json").read_text())
        seen["vnc"] = a.browser_vnc_dir
        seen["vnc_mode"] = oct(a.browser_vnc_dir.stat().st_mode & 0o777)
        return 0
    monkeypatch.setattr(s, "run_pod", run)
    work = tmp_path / "work"
    assert s.main(["claude", "--workspace", str(work), "--no-auth", "--no-tmux", "--no-keep", "--browser",
                   "--browser-control", "both"]) == 0
    assert prepared == ["browser"]
    assert seen["config"]["services"] == [{"name": "browser", "ready": 9222, "timeout": 90, "ca": True}]
    assert seen["config"]["env"]["SANDBOX_AI_BROWSER_CDP"] == "http://127.0.0.1:9222"
    assert seen["browser.json"] == {"kind": "chromium", "control": "both", "screen": "1280x800x24", "display": ":42",
                                    "cdp_port": 9222, "profile": "fresh", "tor": False}
    assert seen["record"]["browser"]["vnc"] == str(seen["vnc"] / "vnc.sock") and seen["vnc_mode"] == "0o700"
    assert not seen["vnc"].exists()  # gone with the run
    with pytest.raises(ValueError, match="Podman backend"):
        s.main(["claude", "--workspace", str(work), "--backend", "bwrap", "--browser"])


def test_the_pod_gets_room_for_chromium(tmp_path):
    a = checked("--browser")
    a.kit_mounts = []
    create, _ = s.pod_commands(a, "agent-x", tmp_path, tmp_path)
    assert "--shm-size=1g" in create


def test_a_session_with_the_browser_waits_longer_for_its_start():
    se = load("sessions_for_browser_tests", "sandbox/sessions.py")
    assert se.services_wait(s.parse_args(["claude", "--browser"])) == 120
    assert se.services_wait(s.parse_args(["claude"])) == 0


# -- security modes, project files, presets --

def test_paranoid_refuses_the_browser_and_high_its_folder_for_the_agent(capsys, tmp_path, monkeypatch):
    (tmp_path / "work").mkdir()
    monkeypatch.chdir(tmp_path / "work")
    with pytest.raises(ValueError, match="paranoid"):
        s.main(["claude", "--dry-run", "--mode", "paranoid", "--browser"])
    plan, _ = dry_run(capsys, "--mode", "high", "--browser")
    assert plan["browser"]["control"] == "human"
    plan, _ = dry_run(capsys, "--mode", "high", "--browser", "--browser-control", "agent")
    assert plan["browser"]["control"] == "agent"
    with pytest.raises(ValueError, match="folder of yours through its profile"):
        s.main(["claude", "--dry-run", "--mode", "high", "--browser", "--browser-control", "agent",
                "--browser-profile", str(tmp_path / "p")])
    plan, _ = dry_run(capsys, "--mode", "high", "--browser", "--browser-profile", str(tmp_path / "p"))
    assert plan["browser"]["profile"]["kind"] == "dir"  # you drive it: your folder, your browser
    for option in ("--browser", "--browser-control", "--browser-profile"):
        assert option in s.MODES.WEAKENING_OPTIONS


# -- the image --

def test_the_containerfile_builds_sandbox_ais_browser():
    text = (ROOT / "sandbox" / "Containerfile.browser").read_text()
    assert "LABEL sandbox-ai.role=browser" in text and "ARG DEBIAN_IMAGE=docker.io/library/debian:bookworm-slim" in text
    for package in ("chromium", "firefox-esr", "xvfb", "x11vnc", "libnss3-tools", "python3", "util-linux"):
        assert f" {package}" in text
    assert '"Certificates": {"Install": ["/sandbox-browser/ca.pem"]}' in text
    assert 'ENTRYPOINT ["/usr/bin/python3", "-I", "/sandbox-browser/browser_runtime.py"]' in text
    assert "browser" in load("images_for_browser_tests", "sandbox/images.py").BUILD_TARGETS


def test_sandbox_ai_build_browser_pins_its_base_by_digest(tmp_path, monkeypatch):
    log = tmp_path / "podman.log"
    script = tmp_path / "bin" / "podman"
    script.parent.mkdir()
    digest = "sha256:" + "d" * 64
    script.write_text(f"""#!{sys.executable}
import json, sys
args = [a for a in sys.argv[1:] if not a.startswith("--cgroup-manager")]
with open({str(log)!r}, "a") as handle:
    handle.write(json.dumps(args) + "\\n")
if args[:2] == ["image", "inspect"]:
    print({digest!r})
""")
    script.chmod(0o700)
    monkeypatch.setenv("PATH", f"{script.parent}:/usr/bin:/bin")
    monkeypatch.setenv("SANDBOX_AI_PYTHON", sys.executable)
    monkeypatch.setenv("SANDBOX_AI_BUILD_NO_DNS_CHECK", "1")
    result = subprocess.run([str(ROOT / "sandbox-ai"), "build", "browser"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert calls[0][:2] == ["pull", "-q"] and calls[0][-1] == "docker.io/library/debian:bookworm-slim"
    build = next(c for c in calls if c[:1] == ["build"])
    assert "--pull=never" in build and f"DEBIAN_IMAGE=docker.io/library/debian@{digest}" in build
    assert build[build.index("--tag") + 1] == "localhost/sandbox-ai-browser:latest"
    assert "sandbox-ai.role=browser" in build and build[-1] == str(ROOT / "sandbox")
    assert build[build.index("--file") + 1] == str(ROOT / "sandbox" / "Containerfile.browser")
    # a base given is used as given
    log.unlink()
    result = subprocess.run([str(ROOT / "sandbox-ai"), "build", "browser", "--build-arg",
                             f"DEBIAN_IMAGE=docker.io/library/debian@{digest}"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert not any(c[:1] == ["pull"] for c in calls)
    result = subprocess.run([str(ROOT / "sandbox-ai"), "build", "--podman-url", "unix:///x", "browser"],
                            capture_output=True, text=True)
    assert result.returncode == 2 and "without --podman-url" in result.stderr


# -- the runtime in its container --

def test_chromium_command():
    cfg = {"kind": "chromium", "screen": "1280x800x24", "display": ":42", "cdp_port": None}
    argv = rt.chromium_command(cfg, Path("/tmp/home/profile"))
    assert argv[0] == "chromium" and "--user-data-dir=/tmp/home/profile" in argv and "--no-sandbox" in argv
    assert "--disable-dev-shm-usage" in argv and "--window-size=1280,800" in argv and argv[-1] == "about:blank"
    assert not any(word.startswith("--remote-debugging") for word in argv)  # human: no DevTools port at all
    argv = rt.chromium_command({**cfg, "cdp_port": 9222}, Path("/p"))
    assert "--remote-debugging-port=9222" in argv and "--remote-debugging-address=127.0.0.1" in argv


def test_firefox_command():
    argv = rt.command({"kind": "firefox", "screen": "1024x768x24", "cdp_port": None}, Path("/p"))
    assert argv[:3] == ["firefox-esr", "--profile", "/p"] and "-width" in argv and "1024" in argv
    assert not any("remote-debugging" in word for word in argv)


def test_trust_ca(tmp_path):
    calls = []
    assert rt.trust_ca({"kind": "chromium"}, tmp_path, tmp_path / "none.pem", calls.append) is False
    (tmp_path / "ca.pem").write_bytes(CA)
    assert rt.trust_ca({"kind": "chromium"}, tmp_path, tmp_path / "ca.pem",
                       lambda argv, **k: calls.append(argv)) is True
    assert calls[0][:2] == ["certutil", "-N"] and calls[1][:2] == ["certutil", "-A"] and "C,," in calls[1]
    assert f"sql:{tmp_path / '.pki' / 'nssdb'}" in calls[1]
    calls.clear()
    assert rt.trust_ca({"kind": "firefox"}, tmp_path, tmp_path / "ca.pem", calls.append) is True and calls == []


def test_clear_locks_and_restarts(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    for name in ("SingletonLock", "SingletonSocket", "Default"):
        (profile / name).symlink_to("elsewhere-1234") if name != "Default" else (profile / name).mkdir()
    rt.clear_locks("chromium", profile)
    assert sorted(p.name for p in profile.iterdir()) == ["Default"]
    now = [0.0]
    restarts = rt.Restarts(limit=2, window=10, clock=lambda: now[0])
    assert restarts.allowed() and restarts.allowed() and not restarts.allowed()
    now[0] = 100.0
    assert restarts.allowed()  # an old burst no longer counts


def test_runtime_main_starts_display_then_browser_and_stops_on_sigterm(tmp_path, monkeypatch):
    mount = tmp_path / "mount"
    (mount / "vnc").mkdir(parents=True)
    (mount / "config.json").write_text(json.dumps({"kind": "chromium", "control": "human", "screen": "800x600x24",
                                                   "display": ":42", "cdp_port": None, "profile": "fresh"}))
    events, stopped = [], []

    class Process:
        def __init__(self, name):
            self.name = name

        def poll(self):
            return None

    class Runtime:
        VNC_DIR = None

        @staticmethod
        def start_gui(cfg):
            events.append(("gui", cfg["private"], cfg["selection"], str(Runtime.VNC_DIR)))
            return [Process("xvfb"), Process("x11vnc")]

        @staticmethod
        def stop_process(process):
            stopped.append(process.name)
    handlers = {}
    monkeypatch.setattr(rt.signal, "signal", lambda number, handler: handlers.__setitem__(number, handler))
    monkeypatch.setattr(rt.os, "environ", dict(os.environ))

    def popen(argv, **kwargs):
        events.append(("browser", argv[0]))
        return Process("browser")

    def sleep(_):
        handlers[signal.SIGTERM]()  # the run ends
    assert rt.main(mount, tmp_path / "home", Runtime, popen, sleep) == 0
    assert events == [("gui", True, True, str(mount / "vnc")), ("browser", "chromium")]
    assert stopped == ["browser", "x11vnc", "xvfb"]
    assert (tmp_path / "home" / "profile").is_dir()  # fresh: in HOME, a tmpfs of its container
    (mount / "config.json").write_text(json.dumps({"kind": "opera"}))
    assert rt.main(mount, tmp_path / "home", Runtime, popen, sleep) == 2
