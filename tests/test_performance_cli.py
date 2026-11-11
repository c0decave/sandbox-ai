"""What a `sandbox-ai` command costs before it does anything (performance round 1): the app reads other hosts through
`sandbox-ai list --json`, `approvals list --json`, `mcp list --json` ... every few seconds, the web interface runs a
command per request. main.py ran each command's script in a second Python (os.execv), which compiled the script anew
every time (a script run as __main__ never uses __pycache__): about 25-30 ms of the 60-90 ms of such a command. Now
the script runs in main.py's own process, from its cached bytecode; only the agents' launcher keeps a process of its
own (a running start is recognised by `agent_sandbox.py AGENT` in /proc)."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import statistics
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "sandbox" / "main.py"
ROUNDS = 7


def load_main():
    spec = importlib.util.spec_from_file_location("sandbox_ai_main_for_performance", MAIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache")):
        (tmp_path / sub).mkdir()
        monkeypatch.setenv(name, str(tmp_path / sub))
    return {**os.environ}


class Executed(Exception):
    pass


def test_a_command_runs_in_mains_own_process(isolated, monkeypatch, capsys):
    main = load_main()

    def refuse(path, argv):
        raise Executed(argv)
    monkeypatch.setattr(main.os, "execv", refuse)
    monkeypatch.setattr(sys, "argv", list(sys.argv))
    monkeypatch.setitem(sys.modules, "__main__", sys.modules["__main__"])
    with pytest.raises(SystemExit) as ended:
        main.main(["list", "--json"])
    assert ended.value.code in (0, None)
    assert capsys.readouterr().out.strip() == "[]"
    # The script ran as `python -I starts.py list --json` would: its own path first, its prefix, the arguments.
    assert sys.argv == [str(MAIN.parent / "starts.py"), "list", "--json"]
    assert sys.modules["__main__"].__name__ == "__main__"
    assert sys.modules["__main__"].__file__ == str(MAIN.parent / "starts.py")


def test_the_agents_launcher_keeps_a_process_of_its_own(monkeypatch):
    main = load_main()
    seen = []

    def execv(path, argv):
        seen.append(argv)
        raise Executed(argv)
    monkeypatch.setattr(main.os, "execv", execv)
    for agent in ("claude", "codex", "opencode"):
        with pytest.raises(Executed):
            main.main([agent, "--dry-run"])
        # (round 2: through launcher/agent_sandbox.py, which runs agent_sandbox.py from its cached bytecode)
        assert seen[-1][1:] == ["-I", str(MAIN.parent / "launcher" / "agent_sandbox.py"), agent, "--dry-run"]
    assert main.EXECUTED == ("agent_sandbox.py",)
    assert main.executed_script("agent_sandbox.py") == load("agent_sandbox").launcher_script()
    monkeypatch.setattr(main, "HERE", MAIN.parent / "launcher")  # a copy without the stub: the script itself
    assert main.executed_script("agent_sandbox.py") == MAIN.parent / "launcher" / "agent_sandbox.py"


# --- performance round 2: the launcher's process runs agent_sandbox.py from its cached bytecode ------------------
# A script Python runs itself is compiled anew every time: 55 ms of every agent start (and of every session resume
# twice: its check and its start). launcher/agent_sandbox.py runs it as __main__ from __pycache__, in the same process,
# under the same name in /proc. A copy of the stub with a fake launcher beside it shows it behaves as the script.

FAKE_LAUNCHER = r'''
import json, os, signal, sys, time
print(json.dumps({"name": __name__, "file": __file__, "argv": sys.argv, "isolated": sys.flags.isolated,
                  "path": sys.path, "main": sys.modules["__main__"] is sys.modules.get(__name__), "here": __file__}),
      flush=True)
mode = sys.argv[1]
if mode == "exit":
    sys.exit(int(sys.argv[2]))
if mode == "raise":
    raise ValueError("boom")
if mode == "term":
    signal.signal(signal.SIGTERM, lambda number, frame: sys.exit(128 + number))
if mode in ("term", "default", "interrupt"):
    print("ready", flush=True)
    time.sleep(60)
'''


@pytest.fixture
def stub_copy(tmp_path):
    tree = tmp_path / "copy" / "sandbox"
    (tree / "launcher").mkdir(parents=True)
    shutil.copy(MAIN.parent / "launcher" / "agent_sandbox.py", tree / "launcher" / "agent_sandbox.py")
    (tree / "agent_sandbox.py").write_text(FAKE_LAUNCHER)
    return tree


def run_both(tree, *args):
    results = []
    for script in (tree / "agent_sandbox.py", tree / "launcher" / "agent_sandbox.py"):
        done = subprocess.run([sys.executable, "-I", str(script), *args], capture_output=True, text=True, timeout=120)
        results.append((done.returncode, done.stdout, done.stderr))
    return results


def test_the_stub_runs_the_launcher_as_its_script_would(stub_copy):
    direct, stub = run_both(stub_copy, "exit", "3")
    assert direct == stub and direct[0] == 3
    seen = json.loads(stub[1])
    assert seen["name"] == "__main__" and seen["main"] and seen["isolated"] == 1
    assert seen["argv"] == [str(stub_copy / "agent_sandbox.py"), "exit", "3"] and seen["file"] == seen["argv"][0]
    assert str(stub_copy) not in " ".join(seen["path"])  # -I: neither folder is on sys.path
    assert (stub_copy / "__pycache__").is_dir()  # the bytecode the next start runs from
    direct, stub = run_both(stub_copy, "raise")
    assert direct[0] == stub[0] == 1 and direct[1] == stub[1]
    assert direct[2].strip().splitlines()[-1] == stub[2].strip().splitlines()[-1] == "ValueError: boom"


@pytest.mark.parametrize("mode, sent, status", [("term", signal.SIGTERM, 128 + signal.SIGTERM),
                                                 ("default", signal.SIGTERM, -signal.SIGTERM),
                                                 ("interrupt", signal.SIGINT, -signal.SIGINT)])
def test_the_stubs_process_is_a_launcher_in_proc_and_ends_as_one(stub_copy, mode, sent, status):
    net, sessions = load("net"), load("sessions")
    for script in (stub_copy / "agent_sandbox.py", stub_copy / "launcher" / "agent_sandbox.py"):
        process = subprocess.Popen([sys.executable, "-I", str(script), mode], stdout=subprocess.PIPE, text=True)
        try:
            assert process.stdout.readline() and process.stdout.readline() == "ready\n"
            cmdline = Path(f"/proc/{process.pid}/cmdline").read_bytes().split(b"\0")
            # as net.launcher_process and sessions.alive (a start in the foreground) recognise a launcher
            assert any(arg.endswith(b"agent_sandbox.py") for arg in cmdline)
            assert any(os.path.basename(arg) == b"agent_sandbox.py" for arg in cmdline)
            assert sessions.alive("x", {"mode": "foreground", "pid": process.pid,
                                        "pid_start": net.process_start(process.pid)})
            process.send_signal(sent)
            assert process.wait(timeout=60) == status
        finally:
            process.kill()
            process.wait()


def test_exit_status_and_errors_are_the_scripts(isolated, tmp_path):
    missing = tmp_path / "no-such-log-dir"
    result = subprocess.run([sys.executable, "-I", str(MAIN), "logs", str(missing), "--json"], capture_output=True,
                            text=True, env=isolated, timeout=120)
    direct = subprocess.run([sys.executable, "-I", str(MAIN.parent / "logs.py"), str(missing), "--json"],
                            capture_output=True, text=True, env=isolated, timeout=120)
    assert (result.returncode, result.stdout, result.stderr) == (direct.returncode, direct.stdout, direct.stderr)
    assert result.returncode != 0 and result.stderr
    usage = subprocess.run([sys.executable, "-I", str(MAIN), "ps", "--help"], capture_output=True, text=True,
                           env=isolated, timeout=120)
    assert usage.returncode == 0 and usage.stdout.startswith("usage: sandbox-ai ps ")


def fastest(argv, env):
    """The fastest of ROUNDS runs of ARGV, in seconds (the least disturbed by whatever else this machine does)."""
    took = []
    for _ in range(ROUNDS):
        begin = time.perf_counter()
        subprocess.run(argv, capture_output=True, env=env, timeout=120, check=True)
        took.append(time.perf_counter() - begin)
    return min(took), statistics.median(took)


@pytest.mark.parametrize("command", [["list", "--json"], ["approvals", "list", "--json"], ["net", "list"]])
def test_a_command_through_main_costs_no_more_than_its_script_alone(isolated, command):
    """Through main.py a command costs about what its script run directly costs (one Python start each; main.py's
    own imports are the script's too, and the script's bytecode is cached) - it was 1.4 times as much (two Python
    starts, the script compiled each time); here it is 0.8-0.9 times. Bound: 1.1 times."""
    script, *prefix = load_main().PYTHON_COMMANDS[command[0]]
    subprocess.run([sys.executable, "-I", str(MAIN), *command], capture_output=True, env=isolated, timeout=120)
    via_main, via_main_median = fastest([sys.executable, "-I", str(MAIN), *command], isolated)
    alone, alone_median = fastest([sys.executable, "-I", str(MAIN.parent / script), *prefix, *command[1:]], isolated)
    assert via_main < alone * 1.1, (f"through main.py {via_main * 1000:.0f} ms (median {via_main_median * 1000:.0f}), "
                                     f"the script alone {alone * 1000:.0f} ms (median {alone_median * 1000:.0f})")


# --- performance round 2: what the app and the web interface ask every few seconds loads no launcher ------------
# The launcher (agent_sandbox.py and the runtime it loads) took 12-20 ms of each `list --json`, `net list`, `mcp
# list`, `approvals list` and `session list`: the listings only need a few of its names and helpers, which the modules
# now have themselves (the same as the launcher's: the tests below), and use the launcher's once it is loaded. `llm
# list` and `vpn list` no longer load urllib.request, http.client and ssl (about 15 ms), the launcher's runtime loads
# ssl where a function needs it, the launcher uuid where it names something (5-6 ms of every command that loads it).

def load(name, as_name=None):
    spec = importlib.util.spec_from_file_location(as_name or f"sandbox_{name}_for_performance", MAIN.parent / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TRICKY = ["plain", "tab\tand\nnewline", "\x1b[31mred\x1b[0m", "bidi ‮ reversed", "\x00\x7f\x9f",
          "zero​width", "emoji \U0001f600", "\udcff surrogate", "", 42, None]


def test_the_copies_are_the_launchers():
    launcher, net, sessions, mcp = load("agent_sandbox"), load("net"), load("sessions"), load("mcp")
    starts, procinfo = load("starts"), load("procinfo")
    activity, hosts, logs, safeguards = load("activity"), load("hosts"), load("logs"), load("safeguards")
    for text in TRICKY:
        assert net.printable(text) == sessions.printable(text) == activity.printable(text) == hosts.printable(text) \
            == launcher.printable(text)
    for project in (Path("/srv/app"), Path("/"), Path("/srv/ä b"), Path("/srv/app/")):
        assert activity.default_log_dir(project) == logs.default_log_dir(project) == \
            safeguards.default_log_dir(project) == launcher.default_log_dir(project)
    assert (sessions.SESSION_NAME, sessions.START_ID) == (launcher.SESSION_NAME, launcher.START_ID)
    assert sorted(starts.AGENTS) == sorted(launcher.AUTH_TARGETS)
    assert mcp._launcher is None and mcp.socket_dir("web-search") == launcher.mcp_socket_dir("web-search")
    me, start = os.getpid(), launcher.process_start(os.getpid())
    assert net._launcher is None and net.process_start(me) == start is not None
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    for pid, recorded in ((me, start), (me, start + 1), (me, None), (child.pid, None), (None, None), (0, 1)):
        assert procinfo.recorded_cmdline(pid, recorded) == launcher.process_cmdline(pid, recorded), (pid, recorded)
        assert mcp.process_cmdline(pid, recorded, mcp.PROC) == sessions.process_cmdline(pid, recorded, sessions.PROC) \
            == launcher.process_cmdline(pid, recorded)
    assert mcp._launcher is None and sessions._launcher is None and net._launcher is None


def test_net_parses_without_the_launchers_options_as_with_them():
    net = load("net")
    full, lean = net.parser(), net.parser(lean=True)
    assert lean.format_help() == full.format_help()
    for argv in (["list"], ["list", "--json"], ["list", "-j"], ["show"], ["show", "run-1", "--keep-all", "-j"],
                 ["untaint", "r"], ["stop", "r", "--wait", "3"]):
        assert vars(lean.parse_args(argv)) == vars(full.parse_args(argv))
    for argv in (["list", "--allow-github"], ["show", "--dns"], ["stop"]):
        with pytest.raises(SystemExit):
            lean.parse_args(argv)
        with pytest.raises(SystemExit):
            full.parse_args(argv)


def start_ticks(pid):
    return int(Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[19])


@pytest.fixture
def populated(isolated, tmp_path):
    """Two running starts (a session and one in the foreground, their supervisors real processes), two stopped kept
    ones and a running MCP server, as their launchers record them."""
    state, runs = tmp_path / "state" / "sandbox-ai", tmp_path / "runtime" / "sandbox-ai" / "runs"
    sleepers = []

    def sleeper(*words):
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)", *map(str, words)])
        sleepers.append(process)
        for _ in range(500):
            if b"time.sleep" in Path(f"/proc/{process.pid}/cmdline").read_bytes():
                break
            time.sleep(0.01)
        return process.pid, start_ticks(process.pid)

    for key, running, foreground in (("alpha", True, False), ("beta", True, True), ("gamma", False, False),
                                     ("delta", False, False)):
        directory = state / "sessions" / key
        (directory / "tmux").mkdir(parents=True)
        data = {"agent": "claude", "options": ["--backend", "bwrap", "--mcp", "web-search"], "agent_args": [],
                "backend": "bwrap", "workspace": f"/srv/{key}", "cwd": "/", "created": "2026-11-01T10:00:00Z",
                "name": key, "run": f"claude-{key}", "keep": True, **({"mode": "foreground"} if foreground else {})}
        if running:
            pid, start = sleeper("agent_sandbox.py", "--tmux-socket-dir", directory / "tmux")
            data.update(pid=pid, pid_start=start, started="2026-11-02T09:30:00Z")
            (runs / data["run"]).mkdir(parents=True)
            (runs / data["run"] / "run.json").write_text(json.dumps({
                "run": data["run"], "agent": "claude", "backend": "bwrap", "workspace": data["workspace"], "pid": pid,
                "pid_start": start, "start": key, "started": data["started"], "session": key,
                "mcp": ["web-search"], "companions": ["codex"]}))
        (directory / "session.json").write_text(json.dumps(data))
    server = state / "mcp" / "web-search"
    (server / "sock").mkdir(parents=True)
    pid, start = sleeper("agent_sandbox.py", "--internal-mcp", server / "sock")
    (server / "mcp.json").write_text(json.dumps({"pid": pid, "pid_start": start, "backend": "bwrap",
                                                  "started": "2026-11-02T09:30:00Z", "started_for": None}))
    yield isolated
    for process in sleepers:
        process.kill()
        process.wait()


def test_listings_read_the_same_without_the_launcher(populated):
    """What `list`, `session list`, `net list` and `mcp list` read, once without the launcher and once with it loaded
    (as in the app, which has it loaded): the same."""
    def read(with_launcher):
        starts = load("starts", f"starts_for_performance_{with_launcher}")
        if with_launcher:
            starts.launcher()
        found = starts.entries(True, prune=False)
        net, sessions, mcp = starts.module("net"), starts.module("sessions"), starts.module("mcp")
        runs = net.running()
        result = (found, sessions.rows(), runs, net.labels(runs), mcp.status("web-search"))
        assert ("agent_sandbox" in starts._modules) is with_launcher
        return result
    without, loaded = read(False), read(True)
    assert without == loaded
    found, rows, runs, labels, server = without
    assert sorted((e["name"], e["state"]) for e in found) == [("alpha", "running"), ("beta", "running"),
                                                               ("delta", "stopped"), ("gamma", "stopped"),
                                                               ("mcp-web-search", "running")]
    assert sorted(labels.values()) == ["alpha", "beta"] and server == "running"
    assert [row[3] for row in sorted(rows)] == ["running", "running", "stopped", "stopped"]


PROBE = r"""
import importlib.util, json, os, sys
loaded, real = [], importlib.util.spec_from_file_location
importlib.util.spec_from_file_location = lambda name, location=None, *a, **k: (
    loaded.append(os.path.basename(str(location))), real(name, location, *a, **k))[1]
main = real("sandbox_ai_main_probe", sys.argv[1])
module = importlib.util.module_from_spec(main)
main.loader.exec_module(module)
try:
    module.main(sys.argv[2:])
except SystemExit:
    pass
heavy = [name for name in ("ssl", "uuid", "http.client", "urllib.request", "calendar") if name in sys.modules]
with open(os.environ["PROBE_OUT"], "w") as stream:
    json.dump({"loaded": loaded, "heavy": heavy}, stream)
"""


@pytest.mark.parametrize("command", [["list", "--json"], ["list"], ["net", "list"], ["net", "list", "--json"],
                                     ["approvals", "list", "--json"], ["mcp", "list", "--json"], ["session", "list"],
                                     ["llm", "list", "--json"], ["vpn", "list"], ["hosts", "list"],
                                     ["safeguards", "--json", "--tail", "50", "--project", "/srv/alpha"],
                                     ["activity", "--json", "--tail", "50", "--project", "/srv/alpha"],
                                     ["logs", "--json", "--project", "/srv/alpha"]])
def test_what_is_polled_loads_no_launcher(populated, tmp_path, command):
    report = tmp_path / "probe.json"
    result = subprocess.run([sys.executable, "-I", "-c", PROBE, str(MAIN), *command], capture_output=True, text=True,
                            env={**populated, "PROBE_OUT": str(report)}, timeout=120)
    assert result.returncode == 0 or command[0] in ("activity", "logs") and "/srv/alpha" in result.stderr, \
        result.stderr  # (nothing logged for that project)
    probe = json.loads(report.read_text())
    assert "agent_sandbox.py" not in probe["loaded"] and "runtime.py" not in probe["loaded"], probe
    assert probe["heavy"] == [], probe
    if command[:2] == ["list", "--json"]:
        assert sorted(entry["name"] for entry in json.loads(result.stdout)) == [
            "alpha", "beta", "delta", "gamma", "mcp-web-search"]


def test_a_launcher_loaded_later_is_shared(populated):
    """starts.py's modules share one launcher, loaded when the first of them needs it (a listing needs none)."""
    starts = load("starts")
    starts.entries(True)
    sessions, net, mcp = starts.module("sessions"), starts.module("net"), starts.module("mcp")
    assert "agent_sandbox" not in starts._modules and sessions._launcher is None
    shared = sessions.launcher()  # the first that needs it
    assert starts.launcher() is shared and net.launcher() is shared and mcp.launcher() is shared


def test_the_version_options_ask_the_launcher_when_used(isolated):
    version = load("agent_sandbox").__version__
    for command in (["mcp", "-V"], ["session", "--version"]):
        result = subprocess.run([sys.executable, "-I", str(MAIN), *command], capture_output=True, text=True,
                                env=isolated, timeout=120)
        assert (result.returncode, result.stdout) == (0, f"sandbox-ai {version}\n")


LAUNCH_PROBE = r"""
import contextlib, importlib.util, io, json, os, runpy, sys
loaded, real = [], importlib.util.spec_from_file_location
importlib.util.spec_from_file_location = lambda name, location=None, *a, **k: (
    loaded.append(os.path.basename(str(location))), real(name, location, *a, **k))[1]
sys.argv = sys.argv[1:]
status = None
try:
    with contextlib.redirect_stdout(io.StringIO()) as out:
        runpy.run_path(sys.argv[0], run_name="__main__")
except SystemExit as ended:
    status = ended.code
with open(os.environ["PROBE_OUT"], "w") as stream:
    json.dump({"loaded": loaded, "status": status, "plan": out.getvalue()}, stream)
"""


def test_a_start_loads_the_launcher_and_its_runtime_once(isolated, tmp_path):
    """The launcher's starts.py (the names of a start) and its modules (sessions, net, mcp, team) loaded
    agent_sandbox.py and the runtime a second time, about 20 ms of each start; they are handed the running one."""
    workspace, report = tmp_path / "w", tmp_path / "probe.json"
    workspace.mkdir()
    subprocess.run([sys.executable, "-I", "-c", LAUNCH_PROBE, str(MAIN.parent / "launcher" / "agent_sandbox.py"),
                    "claude", "--dry-run", "--backend", "bwrap"], cwd=workspace, env={**isolated, "PROBE_OUT": str(report)},
                   capture_output=True, text=True, timeout=120)
    probe = json.loads(report.read_text())
    assert probe["status"] in (0, None) and json.loads(probe["plan"])["agent"] == "claude", probe
    assert probe["loaded"].count("agent_sandbox.py") == 1 and probe["loaded"].count("runtime.py") == 1, probe["loaded"]
    assert "starts.py" in probe["loaded"] and "sessions.py" in probe["loaded"]
