"""No host package for a container's PID 1 (sandbox/hostinit.py, pid1.py). User, 2026-11-22: "agent-sandbox: the
service cairn did not start: Error: lookup init binary: exec: "catatonit": executable file not found in $PATH" - and
"why needs it to be installed on the host when cairn runs in a container???". Podman's --init mounts the HOST's
catatonit into the container. So: sandbox-ai's own containers (the browser's, Cairn's) run sandbox-ai's own init
(pid1.py) and never --init; an image that is not sandbox-ai's (an MCP image server, a --service) gets --init only
where the host has the init binary, runs without where it brings an init itself, and is refused with the package
command otherwise - never started weaker without a word; `sandbox-ai doctor` says it."""
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import home_guard  # noqa: E402

home_guard.ensure()

ROOT = Path(__file__).resolve().parents[1]


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / "sandbox" / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


h = load("sandbox_ai_hostinit_tests", "hostinit.py")
m = load("sandbox_ai_mcp_hostinit_tests", "mcp.py")
doctor = load("sandbox_ai_doctor_hostinit_tests", "doctor.py")
IMAGE = "ghcr.io/example/server@sha256:" + "a" * 64
PID1 = str(ROOT / "sandbox" / "pid1.py")


@pytest.fixture
def no_override(monkeypatch):
    """This machine's own answer: not the tests' default (conftest: every test host has its init binary)."""
    monkeypatch.delenv(h.VARIABLE, raising=False)
    monkeypatch.delenv("CONTAINERS_CONF", raising=False)
    monkeypatch.delenv("CONTAINERS_HELPER_BINARY_DIR", raising=False)


def executable(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


def nowhere(name):
    return None


# --- where Podman finds its init binary -------------------------------------------------------------------------

def test_it_is_found_where_podman_looks_in_its_order(tmp_path, no_override):
    root, home = tmp_path / "root", tmp_path / "config"
    find = lambda **more: h.init_binary(root=root, home=home, which=more.pop("which", nowhere), **more)  # noqa: E731
    assert find() is None
    assert find(which=lambda name: "/opt/bin/catatonit" if name == "catatonit" else None) == "/opt/bin/catatonit"
    helper = executable(root / "usr/lib/podman/catatonit")
    assert find(which=lambda name: "/opt/bin/catatonit") == str(helper)  # (its helper folders before PATH)
    local = executable(root / "usr/local/libexec/podman/catatonit")
    assert find() == str(local)
    own = executable(tmp_path / "helpers/catatonit")
    assert find(environ={"CONTAINERS_HELPER_BINARY_DIR": str(own.parent)}) == str(own)
    default = executable(root / "usr/libexec/podman/catatonit")
    assert find(environ={"CONTAINERS_HELPER_BINARY_DIR": str(own.parent)}) == str(default)
    (root / "usr/lib/podman/not-executable").write_text("")
    (root / "etc/containers").mkdir(parents=True)
    (root / "etc/containers/containers.conf").write_text('[engine]\n# init_path = "/commented"\ninit_path = "/usr/bin/tini"\n')
    assert find() == "/usr/bin/tini"  # (containers.conf's init_path wins, as Podman takes it)
    (home / "containers").mkdir(parents=True)
    (home / "containers/containers.conf").write_text("[engine]\ninit_path = '/home/me/bin/catatonit'  # mine\n")
    assert find() == "/home/me/bin/catatonit"  # (yours after the system's)
    other = tmp_path / "other.conf"
    other.write_text("[engine]\n")
    assert find(environ={"CONTAINERS_CONF": str(other)}) == str(default)  # (CONTAINERS_CONF alone, when set)


def test_a_file_that_cannot_run_is_no_init_binary(tmp_path, no_override):
    (tmp_path / "usr/libexec/podman").mkdir(parents=True)
    (tmp_path / "usr/libexec/podman/catatonit").write_text("not executable")
    assert h.init_binary(root=tmp_path, home=tmp_path / "config", which=nowhere) is None


def test_the_variable_says_where_it_is_or_that_none_is_used(tmp_path):
    assert h.init_binary(environ={h.VARIABLE: "/x/catatonit"}, root=tmp_path, which=nowhere) == "/x/catatonit"
    assert h.init_binary(environ={h.VARIABLE: "none"}, root=tmp_path,
                         which=lambda name: "/usr/bin/catatonit") is None


# --- what a container gets ---------------------------------------------------------------------------------------

def inspect(entrypoint, status=0):
    """subprocess.run stood in for: `podman image inspect` answers ENTRYPOINT."""
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, status, json.dumps(entrypoint), "")
    run.calls = calls
    return run


def test_init_only_where_the_host_has_it_else_the_images_own_else_refused():
    untouched = inspect(None)
    assert h.wants_init("MCP server x", image=IMAGE, found="/usr/bin/catatonit", run=untouched) is True
    assert h.wants_init("MCP server x", image=IMAGE, own_pid1=True, found="", run=untouched) is False
    assert h.wants_init("MCP server x", image=IMAGE, local=False, found="", run=untouched) is True  # (the engine's)
    assert untouched.calls == []  # (the image is asked about only where the host has no init binary)
    for entrypoint in (["/usr/bin/tini", "--", "server"], ["/sbin/tini-static", "-g", "--"], ["dumb-init", "x"],
                       ["/usr/bin/catatonit", "--"]):
        asked = inspect(entrypoint)
        assert h.wants_init("--service db", image=IMAGE, found="", run=asked) is False, entrypoint
        assert asked.calls[0][-4:] == ["inspect", "--format", "{{json .Config.Entrypoint}}", IMAGE]
    # (an older Podman's answer, its words joined)
    assert h.wants_init("--service db", image=IMAGE, found="", run=inspect("/usr/bin/tini -- server")) is False
    for entrypoint in (["/usr/bin/python3", "server.py"], ["/bin/sh", "-c", "tini -- x"], None, [], "sh tini"):
        with pytest.raises(ValueError) as refused:
            h.wants_init("MCP server jadx", image=IMAGE, found="", run=inspect(entrypoint))
        text = str(refused.value)
        assert text.startswith("MCP server jadx needs an init process as its PID 1, which Podman's --init takes from "
                               "this host: its init binary catatonit is not installed here")
        assert "it is not started" in text and "never runs sudo" in text
    with pytest.raises(ValueError):  # (Podman cannot say: never a start without an init on a guess)
        h.wants_init("MCP server x", image=IMAGE, found="", run=inspect(["tini"], status=125))


@pytest.mark.parametrize("release, command", [
    ("ID=arch\n", "sudo pacman -S --needed catatonit"), ("ID=ubuntu\nID_LIKE=debian\n", "sudo apt install -y catatonit"),
    ("ID=fedora\n", "sudo dnf install -y catatonit"), ('ID="opensuse-leap"\nID_LIKE="suse opensuse"\n',
                                                         "sudo zypper install catatonit"),
    ("ID=alpine\n", "sudo apk add catatonit")])
def test_the_refusal_names_the_distributions_own_command(tmp_path, release, command):
    path = tmp_path / "os-release"
    path.write_text(release)
    manager = h.package_manager(path, "linux", which=nowhere)
    assert f"install it: {command} (" in h.missing_text("MCP server x", manager)
    unknown = h.missing_text("MCP server x", h.package_manager(tmp_path / "none", "linux", which=nowhere))
    assert "install the package catatonit with your distribution's package manager" in unknown


def test_the_package_command_of_several_tools_and_of_a_manager_found_on_path(tmp_path):
    apt = ("apt", ["sudo", "apt", "install", "-y"])
    assert h.install_command(["podman", "pasta", "newuidmap", "newgidmap"], apt) == [
        "sudo", "apt", "install", "-y", "podman", "passt", "uidmap"]
    assert h.install_command(["nft", "nsenter"], ("dnf", ["sudo", "dnf", "install", "-y"]))[-2:] == [
        "nftables", "util-linux"]
    assert h.install_command(["catatonit"], ("brew", ["brew", "install"])) == []  # (podman machine's VM has its own)
    assert h.install_command(["podman"], (None, [])) == []
    assert h.package_manager(tmp_path / "none", "linux", which=lambda name: "/usr/bin/dnf" if name == "dnf" else None)[0] == "dnf"
    assert h.package_manager(tmp_path / "none", "darwin")[0] == "brew"


# --- MCP image servers ------------------------------------------------------------------------------------------

def image(**more):
    return m.validated_image({"image": IMAGE, **more}, "t")


def test_an_mcp_server_keeps_init_where_the_host_has_catatonit(monkeypatch):
    monkeypatch.setenv(h.VARIABLE, "/usr/libexec/podman/catatonit")
    assert "--init" in m.image_command(m.server_network("t", image()))
    assert "--init" not in m.image_command(m.server_network("t", image(pid1="server")))


def test_without_it_a_server_that_is_its_own_pid1_or_brings_an_init_starts_and_any_other_is_refused(monkeypatch):
    monkeypatch.setenv(h.VARIABLE, "none")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(m.hostinit_module(), "image_entrypoint", lambda *args, **kwargs: ["/srv/server"])
    assert "--init" not in m.image_command(m.server_network("t", image(pid1="server")))
    with pytest.raises(ValueError, match="MCP server t needs an init process as its PID 1"):
        m.image_command(m.server_network("t", image()))
    monkeypatch.setattr(m.hostinit_module(), "image_entrypoint", lambda *args, **kwargs: ["/usr/bin/tini", "--", "s"])
    command = m.image_command(m.server_network("t", image()))
    assert "--init" not in command and command[-1] == IMAGE
    # nothing else of its hardening changes with it
    monkeypatch.setenv(h.VARIABLE, "/usr/libexec/podman/catatonit")
    assert [flag for flag in m.image_command(m.server_network("t", image())) if flag != "--init"] == command


def test_the_start_says_it_before_anything_is_started(monkeypatch, tmp_path):
    """start_problems, the check of a start's servers before anything is made: the server that would not start is
    named with the reason and the package command - not Podman's "lookup init binary" after the session was made."""
    monkeypatch.setenv(h.VARIABLE, "none")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(m.hostinit_module(), "image_entrypoint", lambda *args, **kwargs: ["/srv/server"])
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    s = m.launcher()
    a = s.parse_args(["claude", "--mcp", "plain", "--mcp", "own"])
    a.mode = "medium"
    servers = {"plain": image(), "own": image(pid1="server")}
    problems, _instances, _notes = m.start_problems(a, ["plain", "own"], servers, present=lambda ref: True)
    assert len(problems) == 1 and problems[0].startswith("MCP server plain needs an init process as its PID 1")
    assert "catatonit is not installed here" in problems[0]
    monkeypatch.setenv(h.VARIABLE, "/usr/libexec/podman/catatonit")
    assert m.start_problems(a, ["plain", "own"], servers, present=lambda ref: True)[0] == []


# --- sandbox-ai's own init --------------------------------------------------------------------------------------

def run_pid1(*argv, **kwargs):
    return subprocess.run([sys.executable, "-I", PID1, *argv], capture_output=True, text=True, timeout=60, **kwargs)


def test_pid1_ends_with_the_programs_status_and_its_output():
    done = run_pid1(sys.executable, "-c", "import sys; print('ran'); sys.exit(7)")
    assert done.returncode == 7 and done.stdout == "ran\n"
    assert run_pid1(sys.executable, "-c", "pass").returncode == 0
    assert run_pid1(sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGKILL)").returncode == 137
    assert run_pid1("/nonexistent/program").returncode == 127
    assert run_pid1().returncode == 2


def test_pid1_passes_signals_on_to_the_program(tmp_path):
    """`podman stop` sends SIGTERM to PID 1: the program gets it (its own handler ends it in order)."""
    ready = tmp_path / "ready"
    program = ("import signal, sys, time, pathlib\n"
               "signal.signal(signal.SIGTERM, lambda *_: (print('got TERM', flush=True), sys.exit(5)))\n"
               f"pathlib.Path({str(ready)!r}).write_text('x')\n"
               "time.sleep(60)\n")
    process = subprocess.Popen([sys.executable, "-I", PID1, sys.executable, "-c", program], stdout=subprocess.PIPE,
                               text=True)
    deadline = time.monotonic() + 30
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    process.send_signal(signal.SIGTERM)
    out, _ = process.communicate(timeout=30)
    assert out == "got TERM\n" and process.returncode == 5
    # a program without a handler is ended by the signal: 128 + 15, as a shell says it
    process = subprocess.Popen([sys.executable, "-I", PID1, "sleep", "60"])
    time.sleep(0.5)
    process.send_signal(signal.SIGTERM)
    assert process.wait(timeout=30) == 128 + signal.SIGTERM


@pytest.mark.skipif(not hasattr(os, "fork") or sys.platform != "linux", reason="a PID namespace's rules: Linux")
def test_pid1_reaps_what_is_handed_to_it(tmp_path):
    """A grandchild whose parent went is handed to PID 1 and stays a zombie unless it is reaped: as a child subreaper
    (what PID 1 is in a container) pid1.py leaves none. Its program ends last, with its own status."""
    report = tmp_path / "report"
    program = (
        "import ctypes, os, subprocess, sys, time\n"
        # the test's stand-in for being PID 1: orphans of this process tree are handed to pid1.py
        "orphaner = 'import subprocess, sys; subprocess.Popen([sys.executable, \"-c\", \"import time; time.sleep(0.3)\"])'\n"
        "subprocess.run([sys.executable, '-c', orphaner])\n"
        "time.sleep(1.5)\n"
        "parent = os.getppid()\n"
        "zombies = []\n"
        "for entry in os.listdir('/proc'):\n"
        "    if entry.isdigit():\n"
        "        try:\n"
        "            fields = open(f'/proc/{entry}/stat').read().rpartition(')')[2].split()\n"
        "        except OSError:\n"
        "            continue\n"
        "        if fields[0] == 'Z' and int(fields[1]) == parent:\n"
        "            zombies.append(entry)\n"
        f"open({str(report)!r}, 'w').write(repr(zombies))\n"
        "sys.exit(3)\n")
    wrapper = ("import ctypes, os, sys\n"
               "ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0)\n"  # PR_SET_CHILD_SUBREAPER
               f"os.execv(sys.executable, [sys.executable, '-I', {PID1!r}, sys.executable, '-c', sys.argv[1]])\n")
    done = subprocess.run([sys.executable, "-c", wrapper, program], capture_output=True, text=True, timeout=60)
    assert done.returncode == 3, done.stderr
    assert report.read_text() == "[]"


# --- sandbox-ai doctor ------------------------------------------------------------------------------------------

def test_doctor_says_whether_podmans_init_is_there_and_how_to_install_it():
    status, title, detail, hint = doctor.check_init(found="/usr/libexec/podman/catatonit")
    assert (status, title, hint) == ("ok", "Podman init", "") and "/usr/libexec/podman/catatonit" in detail
    status, title, detail, hint = doctor.check_init(found="", manager=("pacman", ["sudo", "pacman", "-S", "--needed"]))
    assert status == "warn" and hint == "sudo pacman -S --needed catatonit"
    assert "catatonit not found" in detail and "the agents, the browser and Cairn need none" in detail
    assert doctor.check_init(found="", manager=(None, []))[3] == "install the package catatonit"
    assert doctor.check_init(podman_url="unix:///run/x.sock")[0] == "skip"
    assert doctor.group_of("Podman init") == "Engine"
    assert any(title == "Podman init" for _, title, _, _ in doctor.checks("podman"))


# --- end to end: ./sandbox-ai with Podman faked ------------------------------------------------------------------

def host_with_server(tmp_path, **definition):
    import live_fakes as F
    host = F.Host(tmp_path / "h")
    host.add_image("localhost/claude-sandbox:latest", IMAGE)
    config = host.home / ".config/sandbox-ai"
    config.mkdir(parents=True, exist_ok=True)
    (config / "mcp.json").write_text(json.dumps({"files": {"image": IMAGE, "network": "none", **definition}}))
    (config / "mcp.json").chmod(0o600)
    return host


def test_a_start_on_a_host_without_catatonit_is_told_before_anything_is_made(tmp_path):
    """The user's start failed after its session was made ("lookup init binary ... was not kept: its first start
    failed"). Now the check before the start says which server it is about, why, and the command - and nothing is
    made; with the init binary there, and for a server that is its own PID 1, the start goes on as before."""
    host = host_with_server(tmp_path)
    refused = host.run("claude", "--mcp", "files", env={h.VARIABLE: "none"})
    text = " ".join(refused.stderr.split())
    assert refused.returncode != 0 and host.stopped_at() is None, refused.stderr[-800:]
    assert "MCP server files needs an init process as its PID 1" in text
    assert "its init binary catatonit is not installed here" in text and "install" in text
    assert not any(call[:1] == ["run"] or "create" in call[:2] for call in host.calls())  # (nothing was started)
    there = host.run("claude", "--mcp", "files")  # (the tests' hosts have it: live_fakes)
    assert "needs an init process" not in there.stderr and host.stopped_at() is not None, there.stderr[-800:]
    assert host.run("mcp", "stop", "files").returncode == 0  # (the server that start started: ended)


def test_a_server_that_is_its_own_pid_1_starts_on_a_host_without_catatonit(tmp_path):
    host = host_with_server(tmp_path, pid1="server")
    done = host.run("claude", "--mcp", "files", env={h.VARIABLE: "none"})
    assert "needs an init process" not in done.stderr and host.stopped_at() is not None, done.stderr[-800:]
    assert host.run("mcp", "stop", "files").returncode == 0


def test_doctor_names_the_package_through_the_real_command(tmp_path):
    import live_fakes as F
    host = F.Host(tmp_path / "h")
    found = {row["check"]: row for row in json.loads(host.run("doctor", "--json", env={h.VARIABLE: "none"}).stdout)}
    assert found["Podman init"]["status"] == "warn" and "catatonit not found" in found["Podman init"]["detail"]
    assert found["Podman init"]["group"] == "Engine" and "catatonit" in found["Podman init"]["hint"]
    found = {row["check"]: row for row in json.loads(host.run("doctor", "--json").stdout)}
    assert found["Podman init"]["status"] == "ok"
