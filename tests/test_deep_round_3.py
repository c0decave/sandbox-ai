"""Deep test round 3: chaos, concurrency and hostile input across the features of 0.1.93-0.1.95, cut by failure mode
(two at once, interrupted, exhausted, damaged or hostile input, time) rather than by feature. One test per bug, named
after it; the e2e ones drive the real ./sandbox-ai entry point as a subprocess (live_fakes.py).
"""
import importlib.util
import json
import os
from pathlib import Path
import threading
import time

import pytest

from live_fakes import Host, ROOT


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def host(tmp_path):
    return Host(tmp_path)


# --- (a) two at once ----------------------------------------------------------------------------------------------

def test_two_images_extend_at_once_lose_no_package(host):
    """`sandbox-ai images extend v --add-apt pkgN` run side by side (the app and a terminal, two terminals): each read
    image.json, added its package and wrote the whole file back - one of the packages was silently lost."""
    assert host.run("images", "new", "v", "--from", "claude").returncode == 0
    processes = [host.popen("images", "extend", "v", "--add-apt", f"pkg{i}") for i in range(12)]
    for process in processes:
        process.communicate(timeout=120)
        assert process.returncode == 0
    document = json.loads((host.home / ".config/sandbox-ai/images/variants/claude/v/image.json").read_text())
    assert sorted(document["apt"]) == sorted(f"pkg{i}" for i in range(12))


def test_images_extend_waits_for_another_extend_of_the_same_extension(tmp_path, monkeypatch):
    """The same, made certain: the second extend reads the extension while the first is between its read and its
    write. With the lock the second waits for the first and keeps its package."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    images = load("images_deep3", "sandbox/images.py")
    images.new_variant("v", "claude")
    first_read, second_read = threading.Event(), threading.Event()
    real_load = images.load

    def slow_load(target, strict=True):
        result = real_load(target, strict)
        if threading.current_thread().name == "first":
            first_read.set()
            second_read.wait(1.5)  # without a lock the second reads now, the same document
        else:
            second_read.set()
        return result

    monkeypatch.setattr(images, "load", slow_load)
    errors = []

    def extend(package):
        try:
            images.extend("v", add={"apt": [package]})
        except Exception as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    first = threading.Thread(target=extend, args=("one",), name="first")
    first.start()
    assert first_read.wait(10)
    second = threading.Thread(target=extend, args=("two",), name="second")
    second.start()
    first.join(20)
    second.join(20)
    assert not errors
    assert sorted(real_load("v")[0]["apt"]) == ["one", "two"]


def test_two_images_built_at_once_are_both_recorded(host):
    """`sandbox-ai images build-image a` and `... b` (or `images pull`) in two terminals: each read the record of your
    images before its build or pull (minutes) and wrote it back whole after it - the image built first was built
    but not recorded ("no image a of yours")."""
    context = host.directory / "context"
    context.mkdir()
    (context / "Containerfile").write_text("FROM scratch\nCOPY x /x\n")
    (context / "x").write_text("x")
    processes = [host.popen("images", "build-image", name, "--file", context / "Containerfile",
                            env={"FAKE_PODMAN_BUILD_SECONDS": str(seconds)})
                 for name, seconds in (("a", 2), ("b", 0.5), ("c", 1))]
    for process in processes:
        process.communicate(timeout=120)
        assert process.returncode == 0
    registry = json.loads((host.home / ".config/sandbox-ai/user-images.json").read_text())
    assert sorted(registry["images"]) == ["a", "b", "c"]
    done = host.run("images", "remove", "b", "--keep-image")
    assert done.returncode == 0, done.stderr
    registry = json.loads((host.home / ".config/sandbox-ai/user-images.json").read_text())
    assert sorted(registry["images"]) == ["a", "c"]


def test_kit_plugins_added_at_once_are_all_kept(host):
    """`sandbox-ai kits add-plugin k1 -a opencode pkgN` side by side (the app's Kits page and a terminal): each read
    the kit's marketplaces.json and wrote it back whole - one of the plugins was silently lost."""
    assert host.run("kits", "new", "k1").returncode == 0
    processes = [host.popen("kits", "add-plugin", "k1", "-a", "opencode", f"pkg{i}") for i in range(12)]
    for process in processes:
        process.communicate(timeout=120)
        assert process.returncode == 0
    shown = host.run("kits", "show", "k1").stdout
    line = next(line for line in shown.splitlines() if "npm plugins" in line)
    assert sorted(line.split(":", 1)[1].replace(",", " ").split()) == sorted(f"pkg{i}" for i in range(12))


def test_mcp_set_of_two_servers_at_once_loses_neither(host):
    """`sandbox-ai mcp set A --memory 2g` and `mcp set B ...` at the same moment (the app's MCP page and a terminal),
    or `mcp add` beside `mcp set`: each read mcp.json and wrote it back whole, so one server's change was lost."""
    config = host.home / ".config/sandbox-ai/mcp.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({f"s{i}": {"command": ["srv"]} for i in range(12)}))
    processes = [host.popen("mcp", "set", f"s{i}", "--memory", "2g") for i in range(12)]
    for process in processes:
        process.communicate(timeout=120)
        assert process.returncode == 0
    document = json.loads(config.read_text())
    assert sorted(name for name, definition in document.items() if definition.get("options")) == \
        sorted(f"s{i}" for i in range(12))


def test_mcp_json_is_written_under_a_lock(tmp_path, monkeypatch):
    """The same, made certain: a second write of mcp.json waits while the first is between its read and its write."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    mcp = load("mcp_deep3", "sandbox/mcp.py")
    mcp.write_definition("a", {"command": ["srv"]})
    first_read, second_read = threading.Event(), threading.Event()
    real = mcp.read_config

    def slow_read():
        result = real()
        if threading.current_thread().name == "first":
            first_read.set()
            second_read.wait(1.5)
        else:
            second_read.set()
        return result

    monkeypatch.setattr(mcp, "read_config", slow_read)
    first = threading.Thread(target=mcp.write_definition, args=("b", {"command": ["b"]}), name="first")
    first.start()
    assert first_read.wait(10)
    second = threading.Thread(target=mcp.write_definition, args=("c", {"command": ["c"]}), name="second")
    second.start()
    first.join(20)
    second.join(20)
    assert sorted(json.loads((tmp_path / "config/sandbox-ai/mcp.json").read_text())) == ["a", "b", "c"]


# --- (b) interrupted ----------------------------------------------------------------------------------------------

def start_held_at_its_pod(host, *args):
    """`sandbox-ai ARGS` started and held at `podman pod create` (FAKE_PODMAN_POD_HOLD); (process, release file)."""
    host.add_image("localhost/claude-sandbox:latest")
    release = host.directory / "release"
    process = host.popen(*args, env={"FAKE_PODMAN_RUN": "0:0", "FAKE_PODMAN_POD_HOLD": str(release)})
    for _ in range(600):
        if any(call[:2] == ["pod", "create"] for call in host.calls()):
            return process, release
        time.sleep(0.05)
    process.kill()
    raise AssertionError("the start never got to its pod")


def test_a_start_renamed_while_it_runs_ends_naming_its_new_name(host):
    """`sandbox-ai rename s1 s2` while the start s1 runs: at its end it said "s1 keeps its HOME; continue it with:
    sandbox-ai resume s1", a name that is no longer there ("nothing named s1"). It names s2 now."""
    process, release = start_held_at_its_pod(host, "claude", "--name", "s1", "--keep")
    done = host.run("rename", "s1", "s2")
    assert done.returncode == 0, done.stderr
    release.write_text("go")
    _out, err = process.communicate(timeout=60)
    assert process.returncode == 0, err
    assert "sandbox-ai resume s2" in err and "resume s1" not in err, err


def test_a_killed_launchers_proxy_folder_with_the_keys_goes_with_its_run(host, tmp_path):
    """A launcher killed with SIGKILL (the OOM killer, kill -9) left its temporary folders behind for good: the
    proxy's (proxy.json with the plain --api-key keys, the Git and LLM tokens) and the run's session folder (the
    tokens given at run time). Once the run is known to have ended (`sandbox-ai list`, gc) they go with its run
    directory."""
    temporary = tmp_path / "tmp"
    temporary.mkdir()
    host.env["TMPDIR"] = str(temporary)
    process, release = start_held_at_its_pod(host, "claude", "--name", "s1", "--keep")
    made = sorted(path.name.startswith("agent-sandbox-proxy-") for path in temporary.iterdir())
    assert made == [False, True]  # the session's folder and the proxy's
    process.kill()
    process.communicate(timeout=30)
    release.write_text("go")
    done = host.run("list", "-a")
    assert done.returncode == 0, done.stderr
    assert list(temporary.iterdir()) == []


def test_discarding_a_run_removes_only_its_own_temporary_folders(tmp_path, monkeypatch):
    """The folders a damaged or planted run.json names are removed only when they are what a launcher makes: a
    folder (not a link) named agent-sandbox-..., of this user."""
    liveness = load("liveness_deep3", "sandbox/liveness.py")
    runs, other = tmp_path / "runs", tmp_path / "keep"
    other.mkdir()
    (other / "file").write_text("x")
    link = tmp_path / "agent-sandbox-link"
    link.symlink_to(other)
    for named in (str(other), str(link), ["x"], 5, None, "relative/agent-sandbox-x"):
        run = runs / "r"
        run.mkdir(parents=True)
        (run / "run.json").write_text(json.dumps({"run": "r", "session_dir": named, "proxy_dir": named}))
        liveness.discard(run)
        assert not run.exists()
    assert (other / "file").exists() and link.is_symlink()
    (runs / "r").mkdir()
    (runs / "r" / "run.json").write_text("{damaged")
    liveness.discard(runs / "r")
    assert not (runs / "r").exists()


# --- (c) exhausted -------------------------------------------------------------------------------------------------

def test_a_full_disk_leaves_no_dot_file_beside_an_image_extension_or_your_images(tmp_path, monkeypatch):
    """`images extend`, `images new`, `images pull` and `build-image` on a full disk: the error was said, but a
    .image.json.PID.tmp (or .user-images.json.PID.tmp) stayed beside the file each time."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    images = load("images_deep3_full", "sandbox/images.py")
    images.new_variant("v", "claude")
    real = os.fdopen

    class Full:
        def __init__(self, fd):
            self.stream = real(fd, "wb")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.stream.close()

        def write(self, data):
            raise OSError(28, "No space left on device")

    monkeypatch.setattr(images.os, "fdopen", lambda fd, mode="r", *a, **k: Full(fd) if mode == "wb" else real(fd, mode, *a, **k))
    with pytest.raises(OSError):
        images.extend("v", add={"apt": ["jq"]})
    with pytest.raises(OSError):
        images.write_private(tmp_path / "config" / "sandbox-ai" / "user-images.json", b"{}")
    monkeypatch.undo()
    left = [path.name for path in (tmp_path / "config").rglob(".*.tmp")]
    assert left == []


def test_llm_traffic_of_one_request_reads_a_big_log_in_little_memory(tmp_path):
    """The app shows a request by `sandbox-ai llm traffic RUN --request N --json`: the whole log (up to a GiB) was
    read into a list first - conversations of many short messages took five times the log's size in memory (a log
    of 130 MB: 650 MB), for each click. Now one record at a time; also with --follow's bookkeeping, a request
    number that is no number is no TypeError."""
    import tracemalloc
    traffic = load("llm_traffic_deep3_big", "sandbox/llm_traffic.py")
    logs = tmp_path / "logs"
    logs.mkdir()
    with open(logs / "llm-traffic-20261113-100000-run-1.jsonl", "w") as stream:
        for n in range(1, 61):
            stream.write(json.dumps({"version": 1, "n": n if n != 7 else "x", "run": "run-1", "api": "chat",
                                     "request": {"messages": [{"role": "user", "content": "x" * 40}] * 1000},
                                     "response": {"text": f"answer {n}"}}) + "\n")
    lines = []
    tracemalloc.start()
    try:
        assert traffic.cli(["--log-dir", str(logs), "--request", "30", "--json"], out=lines.append) == 0
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert len(lines) == 1 and json.loads(lines[0])["record"]["n"] == 30
    assert (logs / "llm-traffic-20261113-100000-run-1.jsonl").stat().st_size > 3_000_000
    assert peak < 8_000_000, peak  # (all of it: about 22 MB; one record: about 1)
    with pytest.raises(ValueError, match="no request 99"):
        traffic.cli(["--log-dir", str(logs), "--request", "99"], out=lines.append)


# --- (e) time: what is slow or never answers ----------------------------------------------------------------------

def test_doctor_with_a_podman_that_never_answers_ends_soon_and_says_so(tmp_path, monkeypatch):
    """`sandbox-ai doctor` (and the app's Doctor page) with a Podman that hangs (a stuck podman process, a remote
    engine gone): each of its checks waited 30 s - podman info, then every agent's image and the VPN images - more
    than four minutes, and the verdict was "podman info failed". Podman is asked once; the checks that need it are
    skipped, and the verdict says it did not answer."""
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "podman").write_text("#!/bin/sh\nexec sleep 60\n")
    (fake / "podman").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}:/usr/bin:/bin")
    doctor = load("doctor_deep3", "sandbox/doctor.py")
    monkeypatch.setattr(doctor, "PODMAN_TIMEOUT", 2, raising=False)
    begun = time.monotonic()
    found = [doctor.check_podman()] + [doctor.check_image(agent) for agent in doctor.AGENTS]
    assert time.monotonic() - begun < 10
    assert found[0][0] == "fail" and "did not answer" in found[0][2]
    assert all(result[0] == "skip" and "does not answer" in result[2] for result in found[1:])


# --- (d) damaged or hostile input ---------------------------------------------------------------------------------

@pytest.mark.parametrize("kind", [["ollama"], {"a": 1}])
def test_llm_json_with_a_kind_that_is_no_string_is_named_not_a_traceback(host, kind):
    """llm.json edited by hand with "kind": [...] or {...}: `llm list`, `llm show` and a start with --llm ended with
    "TypeError: unhashable type" instead of naming the endpoint and its field."""
    store = host.home / ".config/sandbox-ai/llm.json"
    store.parent.mkdir(parents=True)
    store.write_text(json.dumps({"version": 1, "endpoints": {"lm": {"kind": kind, "url": "http://10.0.0.1:1234"}}}))
    store.chmod(0o600)
    for args in (["llm", "list"], ["llm", "show", "lm"], ["codex", "--dry-run", "--llm", "lm"]):
        done = host.run(*args)
        assert "Traceback" not in done.stderr, (args, done.stderr)
        assert "lm" in done.stdout + done.stderr and "kind" in done.stdout + done.stderr, (args, done.stderr)
    done = host.run("llm", "status")
    assert "Traceback" not in done.stderr, done.stderr


@pytest.mark.parametrize("field,value", [("tor", None), ("tor", "x"), ("tor", []), ("tor.bridges", [1]),
                                         ("tor.transports", 5), ("tor.exit_country", 5), ("tor.exit_country", [1]),
                                         ("dns", "1.1.1.1"), ("checks", [1])])
def test_a_damaged_vpn_profile_is_named_by_vpn_list_show_and_a_start(host, field, value):
    """A VPN profile (vpn/NAME.json) of the right syntax and the wrong shape - its "tor" section null or a list,
    "bridges" a list, "transports" a number - ended `vpn list`, `vpn show` and a start with --vpn with an
    AttributeError or TypeError traceback."""
    assert host.run("vpn", "add", "t1", "--tor").returncode == 0
    path = host.home / ".config/sandbox-ai/vpn/t1.json"
    profile = json.loads(path.read_text())
    keys = field.split(".")
    (profile[keys[0]] if len(keys) == 2 else profile)[keys[-1]] = value
    path.write_text(json.dumps(profile))
    for args in (["vpn", "list"], ["vpn", "show", "t1"], ["claude", "--dry-run", "--vpn", "t1"]):
        done = host.run(*args)
        assert "Traceback" not in done.stderr, (args, done.stderr)
        assert "t1" in done.stdout + done.stderr, (args, done.stderr)
    assert host.run("vpn", "show", "t1").returncode != 0
    done = host.run("vpn", "remove", "t1")  # (a damaged profile could not be removed, whatever its damage)
    assert done.returncode == 0 and not path.exists(), done.stderr


@pytest.mark.parametrize("field,value", [("agent", ["claude"]), ("agent", None), ("options", 5), ("options", None),
                                         ("options", [None]), ("options", [5]), ("agent_args", 5),
                                         ("agent_args", None), ("cwd", ["/"]), ("cwd", 5)])
def test_a_damaged_session_json_is_named_by_resume_copy_and_report(host, field, value):
    """A session.json of the right syntax and the wrong shape (an "options" that is a number, an "agent" that is a
    list, a "cwd" that is a list...): resume, session copy and report ended with a TypeError, KeyError or
    AttributeError traceback. `sandbox-ai list` already called such a start damaged; these now say so too."""
    host.add_image("localhost/claude-sandbox:latest")
    done = host.run("claude", "--name", "s1", "--keep", env={"FAKE_PODMAN_RUN": "0:0"})
    assert done.returncode == 0, done.stderr
    record = next((host.home / ".local/state/sandbox-ai/sessions").glob("*/session.json"))
    data = json.loads(record.read_text())
    data[field] = value
    record.write_text(json.dumps(data))
    assert host.run("hosts", "add", "h1", "u@ct1").returncode == 0
    for args in (["resume", "s1"], ["session", "copy", "s1", "h1"], ["report", "s1", "-o", os.devnull]):
        done = host.run(*args)
        assert "Traceback" not in done.stderr, (args, done.stderr)
        assert done.returncode != 0 or args[0] == "report", (args, done.stderr)


@pytest.mark.parametrize("state", [[], "x", 5, None])
def test_a_damaged_mcp_server_state_is_none_for_mcp_list_and_stop(host, state):
    """An MCP server's state file (mcp/NAME/mcp.json) of valid JSON and another shape: `sandbox-ai mcp list` ended
    with "AttributeError: 'list' object has no attribute 'get'" - for every server, also on the app's MCP page - and
    so did `mcp stop NAME`. It counts as no state (the server not running)."""
    folder = host.home / ".local/state/sandbox-ai/mcp/demo"
    (folder / "sock").mkdir(parents=True)
    (folder / "mcp.json").write_text(json.dumps(state))
    listed = host.run("mcp", "list")
    assert listed.returncode == 0 and "demo" in listed.stdout and "stopped" in listed.stdout, listed.stderr
    stopped = host.run("mcp", "stop", "demo")
    assert "Traceback" not in stopped.stderr, stopped.stderr


def test_canaries_list_with_a_damaged_record_and_one_that_goes_meanwhile(host):
    """`sandbox-ai canaries list`: a run's record whose "canaries" is a number or null ended it with "TypeError:
    object of type 'int' has no len()"; a record removed while it listed (a start drops records older than 30 days
    at the same moment; here: a link to nothing) with FileNotFoundError."""
    runs = host.home / ".local/state/sandbox-ai/canaries/runs"
    runs.mkdir(parents=True)
    (runs / "claude-11111111.json").write_text(json.dumps({"run": "claude-11111111", "status": "ended",
                                                           "canaries": 5}))
    (runs / "claude-22222222.json").write_text(json.dumps({"run": ["x"], "status": "ended", "canaries": None}))
    (runs / "claude-33333333.json").symlink_to(runs / "gone.json")
    for args in (["canaries", "list"], ["canaries", "list", "--json"]):
        done = host.run(*args)
        assert done.returncode == 0 and "Traceback" not in done.stderr, done.stderr
        assert "claude-11111111" in done.stdout and "claude-22222222" in done.stdout
    done = host.run("canaries", "log")  # (it sorted them the same way)
    assert done.returncode == 0 and "Traceback" not in done.stderr, done.stderr


def test_safeguards_and_publish_logins_skip_a_kind_that_is_no_string(host):
    """The same TypeError in `sandbox-ai safeguards --all` (a safeguards.jsonl line whose "kind" is a list) and in
    every command reading publish-logins.json (a login whose "kind" is a list)."""
    logs = host.home / ".local/state/sandbox-ai/logs/ws-0123456789ab"
    logs.mkdir(parents=True)
    (logs / "safeguards.jsonl").write_text(json.dumps({"kind": ["memory"], "run": "r"}) + "\n")
    done = host.run("safeguards", "--all")
    assert "Traceback" not in done.stderr and done.returncode == 0, done.stderr
    logins = host.home / ".config/sandbox-ai/publish-logins.json"
    logins.parent.mkdir(parents=True)
    logins.write_text(json.dumps({"version": 1, "logins": [{"kind": ["npm"], "host": "x", "secret": "s"}]}))
    logins.chmod(0o600)
    done = host.run("publish-logins", "list")
    assert "Traceback" not in done.stderr and "publish-logins.json" in done.stderr, done.stderr


@pytest.mark.parametrize("changes", [5, "x", {"field": "messages"}])
def test_llm_traffic_with_changes_that_are_no_list_shows_the_requests(tmp_path, changes):
    """An LLM traffic record whose "changes" is no list (a damaged line, a log of another version): `llm traffic
    --summary` ended with "TypeError: 'int' object is not iterable"; the view and the summary take it as none."""
    traffic = load("llm_traffic_deep3", "sandbox/llm_traffic.py")
    logs = tmp_path / "logs"
    logs.mkdir()
    record = {"version": 1, "n": 1, "time": "2026-11-13T10:00:01.000Z", "run": "run-1", "api": "chat", "model": "m",
              "status": 200, "request": {"messages": [{"role": "user", "content": "hello"}]},
              "response": {"text": "hi"}, "changes": changes}
    (logs / "llm-traffic-20261113-100000-run-1.jsonl").write_text(json.dumps(record) + "\n")
    for flags in ([], ["--summary"], ["--json"], ["--markdown"], ["--full"]):
        lines = []
        assert traffic.cli(["--log-dir", str(logs), *flags], out=lines.append) == 0
        assert "hi" in "\n".join(lines), flags


def test_report_into_a_device_or_a_pipe_is_written_there(host, tmp_path):
    """`sandbox-ai report --all -o /dev/null` (or /dev/stdout, a named pipe): the report was written beside it as a
    temporary file to be renamed over it - "[Errno 16] Device or resource busy: '/dev/.report-...' -> '/dev/null'"
    (as root), "[Errno 13] Permission denied: '/dev/.report-...'" (as anyone else). What is no regular file is
    written into as it is."""
    done = host.run("report", "--all", "-o", os.devnull)
    assert done.returncode == 0, done.stderr
    pipe = tmp_path / "pipe"
    os.mkfifo(pipe)
    received = []
    reader = threading.Thread(target=lambda: received.append(pipe.read_bytes()))
    reader.start()
    done = host.run("report", "--all", "-f", "md", "-o", pipe)
    reader.join(30)
    assert done.returncode == 0, done.stderr
    assert received and b"# sandbox-ai report" in received[0] and pipe.exists() and not pipe.is_file()
    assert not list(tmp_path.glob(".report-*"))


# --- e2e checks of what held (each passed when it was written; kept so that it keeps holding) -------------------------

def test_two_starts_of_one_name_at_the_same_moment_make_one(host):
    """`sandbox-ai claude --name same` twice at once (two terminals, the app and a terminal): one runs, the other is
    refused by name - never two sandboxes of one name, never a traceback."""
    host.add_image("localhost/claude-sandbox:latest")
    processes = [host.popen("claude", "--name", "same", "--keep", env={"FAKE_PODMAN_RUN": "0:2"}) for _ in range(2)]
    results = [(process.wait(timeout=120), process.communicate()[1]) for process in processes]
    assert sorted(status for status, _ in results) == [0, 1], results
    refused = next(err for status, err in results if status == 1)
    # Which refusal depends on where the second start meets the first: the name in use, taken meanwhile, or the
    # session's lock held by the first start's command (state locking) - each names it, none is a traceback.
    assert "same" in refused and ("in use" in refused or "taken meanwhile" in refused
                                  or "being started or stopped by another command" in refused), refused
    assert "Traceback" not in refused


def test_stop_while_a_start_is_making_its_pod_ends_it_cleanly(host, tmp_path):
    """`sandbox-ai stop NAME` while the start still makes its pod: the start ends (SIGTERM), its run is recorded as
    stopped and kept, its temporary folders go, and the name can be resumed."""
    temporary = tmp_path / "tmp"
    temporary.mkdir()
    host.env["TMPDIR"] = str(temporary)
    process, release = start_held_at_its_pod(host, "claude", "--name", "s1", "--keep")
    done = host.run("stop", "s1")
    assert done.returncode == 0 and "stopped" in done.stderr, done.stderr
    release.write_text("go")
    process.communicate(timeout=60)
    listed = host.run("list", "-a")
    assert "s1" in listed.stdout and "stopped" in listed.stdout, listed.stdout
    assert list(temporary.iterdir()) == []


def test_a_launcher_killed_at_its_pod_leaves_a_start_that_resumes(host):
    """SIGKILL to the launcher while its pod is being made: `list` shows the start stopped and kept, `gc` removes
    none of it, a new start of that name is refused with what to do, and resume goes on to make its pod again."""
    process, release = start_held_at_its_pod(host, "claude", "--name", "s1", "--keep")
    process.kill()
    process.communicate(timeout=30)
    release.write_text("go")
    listed = host.run("list", "-a")
    assert "s1" in listed.stdout and "stopped" in listed.stdout and "kept" in listed.stdout, listed.stdout
    assert host.run("gc").returncode == 0
    again = host.run("claude", "--name", "s1")
    assert again.returncode == 1 and "resume s1" in again.stderr, again.stderr
    resumed = host.run("resume", "s1")
    assert "Traceback" not in resumed.stderr and "pod create" in resumed.stderr, resumed.stderr


@pytest.mark.parametrize("command,listing", [
    (["llm", "add", "l{i}", "-k", "openai-compatible", "-u", "http://10.0.0.{i}:1234", "-m", "m"], ["llm", "list"]),
    (["identities", "add", "i{i}", "--name", "N", "--email", "a@b.c"], ["identities", "list"]),
    (["hosts", "add", "h{i}", "u@c{i}"], ["hosts", "list"]),
    (["kits", "new", "k{i}"], ["kits", "list"]),
])
def test_eight_adds_at_once_are_all_kept(host, command, listing):
    """Eight `sandbox-ai ... add NAME` at the same moment (scripts, the app and a terminal): each one is kept."""
    processes = [host.popen(*[arg.replace("{i}", str(i)) for arg in command]) for i in range(8)]
    for process in processes:
        process.communicate(timeout=120)
        assert process.returncode == 0
    shown = host.run(*listing, timeout=120).stdout
    assert all(command[2].replace("{i}", str(i)) in shown for i in range(8)), shown


def test_defaults_set_of_several_keys_at_once_keeps_all(host):
    keys = (("keep_days", "30"), ("name_style", "project"), ("llm_traffic", "off"), ("canaries", "false"),
            ("desktop_notifications", "none"), ("mode", "medium"))
    processes = [host.popen("defaults", "set", key, value) for key, value in keys]
    for process in processes:
        process.communicate(timeout=60)
        assert process.returncode == 0
    document = json.loads((host.home / ".config/sandbox-ai/defaults.json").read_text())
    assert set(document) >= {key for key, _ in keys}


EVIL = "x\x1b]0;pwn\x07\x1b[31mred‮gnp.exe\nsecond\rline"


@pytest.mark.parametrize("args", [
    ["identities", "add", "i1", "--name", EVIL, "--email", "a@b.c"], ["hosts", "add", "h1", "u@ct1", "--description", EVIL],
    ["llm", "add", "lm", "-k", "ollama", "-u", "http://10.0.0.1:11434", "-m", EVIL], ["claude", "--name", EVIL, "--dry-run"],
    ["kits", "new", EVIL], ["images", "new", EVIL, "--from", "claude"], ["secrets", "set", EVIL],
    ["presets", "show", EVIL], ["report", EVIL], ["llm", "traffic", EVIL]])
def test_control_and_bidi_characters_in_names_are_refused_and_never_echoed_raw(host, args):
    """Escape sequences (a terminal title, colours), a bidi override and line breaks in a name: refused, and what is
    said shows none of them raw."""
    done = host.run(*args)
    said = done.stdout + done.stderr
    assert done.returncode != 0 and "Traceback" not in said, said
    assert not any(raw in said for raw in ("\x1b", "‮", "\x07", "\r")), repr(said)


@pytest.mark.parametrize("folder", ["with space", "co:lon", "ünï code", "-dash", "eq=ual", "quote'\"", "$(x)"])
def test_a_start_in_a_workspace_of_an_odd_name_gets_to_its_pod(host, folder):
    """Workspaces with spaces, a colon, Unicode, a leading dash, quotes or $(...) in their path start; a comma or a
    line break (which Podman's mount options cannot carry) is refused, saying why."""
    host.add_image("localhost/claude-sandbox:latest")
    workspace = host.directory / "spaces" / folder
    workspace.mkdir(parents=True)
    done = host.run("claude", "--name", "odd", "--keep", cwd=workspace, env={"FAKE_PODMAN_RUN": "0:0"})
    assert done.returncode == 0, done.stderr
    created = next(call for call in host.calls() if call[:2] == ["pod", "create"])
    assert created
    assert "odd" in host.run("list", "-a").stdout


@pytest.mark.parametrize("folder", ["com,ma", "new\nline"])
def test_a_workspace_podman_cannot_mount_is_refused_saying_why(host, folder):
    host.add_image("localhost/claude-sandbox:latest")
    workspace = host.directory / "spaces" / folder
    workspace.mkdir(parents=True)
    done = host.run("claude", "--dry-run", cwd=workspace)
    assert done.returncode == 1 and "without commas/control characters" in done.stderr, done.stderr
