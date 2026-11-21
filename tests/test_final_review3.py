"""Final review 3 (robustness, concurrency, regressions): the regression tests of what it found.

- Descriptors of 1024 and above: select.select() refuses them (ValueError), which long-running processes reach - the
  sandbox's supervisor with a busy proxy (its terminal and shell relays, the keep-alive of a local model's stream),
  the web interface (its terminals), the app (its SFTP connections). They wait with poll() now.
- Goose's settings nested deeper than Python reads stopped the telemetry guard with a traceback.
"""
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import sys
import threading

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))


def load(name, path=None):
    spec = importlib.util.spec_from_file_location(f"final3_{name}", path or ROOT / "sandbox" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextlib.contextmanager
def high(fd, at=1500):
    """FD duplicated to a number select() refuses (closed again after)."""
    high_fd = os.dup2(fd, at)
    try:
        yield high_fd
    finally:
        os.close(high_fd)


# --- descriptors of any number -----------------------------------------------------------------------------------------

def test_the_keepalive_of_a_local_models_stream_beats_with_a_high_descriptor():
    """llm_kept_alive's select() raised ValueError for the server's socket in a busy proxy, which it swallowed: no
    keep-alive at all, and Claude Code gave the request up after 300 s of silence."""
    rt = load("runtime")
    server, client = socket.socketpair()
    sock = socket.socket(fileno=os.dup2(client.fileno(), 1500))
    client.close()
    try:
        assert sock.fileno() >= 1024
        stream = rt.Stream(sock)
        pieces = rt.llm_kept_alive(rt.whole_chunks(stream.body(("chunked",))), stream, 0.05, rt.llm_beat("chat"))
        got = []
        reader = threading.Thread(target=lambda: got.append(next(pieces)), daemon=True)
        reader.start()
        reader.join(5)
        assert got and got[0][1] is None, "a beat while the server is silent"
        server.sendall(b"0\r\n\r\n")
        assert list(pieces)[-1] == (b"0\r\n\r\n", b"")
    finally:
        sock.close()
        server.close()


def test_the_supervisors_waits_take_descriptors_of_any_number():
    """No select.select() left in the sandbox's supervisor where a busy proxy's descriptor numbers reach it (only the
    clients' own terminals on the host wait with it: few descriptors, and poll() does not take a macOS terminal)."""
    import ast
    tree = ast.parse((ROOT / "sandbox" / "runtime.py").read_text())
    calls = {}
    for function in ast.walk(tree):
        if isinstance(function, ast.FunctionDef):
            for node in ast.walk(function):
                if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "select" and \
                        getattr(node.func.value, "id", None) == "select":
                    calls.setdefault(function.name, 0)
                    calls[function.name] += 1
    nested = {name for name in calls if name not in ("attach_client", "shell_client", "resized")}
    assert nested == set(), calls


def test_the_web_interfaces_terminal_waits_take_high_descriptors():
    webui = load("webui")
    r, w = os.pipe()
    try:
        with high(r, 1600) as hr, high(w, 1601) as hw:
            assert webui.ready_fds([hr], (), 0) == ([], [])
            assert webui.ready_fds((), [hw], 0.1) == ([], [hw])
            os.write(hw, b"x")
            assert webui.ready_fds([hr], [hw], 0.1) == ([hr], [hw])
        os.close(w)
        w = None
        assert webui.ready_fds([r], (), 0.1)[0] == [r], "the end of the other side counts as readable"
    finally:
        os.close(r)
        if w is not None:
            os.close(w)


def test_the_apps_sftp_waits_take_high_descriptors():
    sys.path.insert(0, str(ROOT / "projects" / "sandbox-ai-app"))
    try:
        remote = pytest.importorskip("sandbox_ai_app.remote")
    finally:
        sys.path.pop(0)
    r, w = os.pipe()
    try:
        with high(r, 1700) as hr, high(w, 1701) as hw:
            assert remote.fd_ready(hr, False, 0) is False
            assert remote.fd_ready(hw, True, 0.1) is True
            os.write(hw, b"x")
            assert remote.fd_ready(hr, False, 0.1) is True
    finally:
        os.close(r)
        os.close(w)


# --- damaged files -----------------------------------------------------------------------------------------------------

def test_goose_settings_nested_too_deep_are_said_not_a_traceback(tmp_path, monkeypatch):
    guard = load("telemetry_guard")
    plugin = tmp_path / "plugin"
    (plugin / "hooks").mkdir(parents=True)
    (plugin / "hooks" / "hooks.json").write_text("{}")
    monkeypatch.setattr(guard, "GOOSE_PLUGIN", str(plugin))
    settings = tmp_path / "home" / ".config" / "goose" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text("[" * 100_000 + "]" * 100_000)
    said = guard.goose_hooks({}, home=str(tmp_path / "home"), cwd=str(tmp_path))
    assert said and "cannot be read" in said


# --- secrets: a step cut off ------------------------------------------------------------------------------------------

@pytest.fixture
def secrets_store(tmp_path, monkeypatch):
    ss = load("secret_store")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    monkeypatch.setenv("SANDBOX_AI_SECRETS_PLAIN", "1")
    monkeypatch.setattr(ss, "terminal", lambda: False)
    return ss


class Killed(BaseException):
    """The process ends here (kill -9): nothing after this step runs."""


@pytest.mark.parametrize("step", ["wipe", "unlink"])
def test_a_delete_cut_off_never_leaves_a_secret_of_zeros(secrets_store, monkeypatch, step):
    """`secrets delete` wrote the file over with zeros and then removed it: killed in between, the store kept a file of
    NUL bytes, read back as the secret's value (a plain file) by every start that uses it. Now the name goes first
    and the blocks are written over through the descriptor opened before: either step cut off leaves the secret as
    it was or gone."""
    ss = secrets_store
    store = ss.FileStore()
    store.set("GH_PAT", "ghp_" + "a" * 36, plain=True)
    with monkeypatch.context() as patch:
        if step == "wipe":
            real = ss.wipe

            def wipe(fd):
                real(fd)
                raise Killed
            patch.setattr(ss, "wipe", wipe)
        else:
            def unlink(*args, **kwargs):
                raise Killed
            patch.setattr(ss.os, "unlink", unlink)
            patch.setattr(ss.Path, "unlink", unlink)
        with pytest.raises(Killed):
            store.delete("GH_PAT")
    value = ss.FileStore().get("GH_PAT")
    assert value in (None, "ghp_" + "a" * 36), repr(value)


def test_a_secret_kept_while_the_password_is_removed_is_never_lost(secrets_store, monkeypatch):
    """`secrets set` sealed its value with the store's key and wrote it without the store's lock: a `secrets password
    remove` in between (another terminal, the app) decrypted every file and removed the header - and then the new
    sealed file came, which nothing can open any more (the key is gone with the header). The write now checks under
    the store's lock that the store is as it was when the value was sealed; else it says so and writes nothing."""
    ss = secrets_store
    monkeypatch.setattr(ss, "SCRYPT", {"n": 2 ** 12, "r": 8, "p": 1})
    monkeypatch.setattr(ss, "agent_call", lambda *args, **kwargs: None)
    store = ss.FileStore()
    store.set("OLD", "old-value", plain=True)
    key = ss.set_password("correct horse battery")
    ss.remember_key(store.directory, key)
    real = ss.FileStore.ready

    def ready(self, plain=False):
        holder = real(self, plain)
        ss.remove_password("correct horse battery", self.directory)  # (another process, just then)
        return holder
    monkeypatch.setattr(ss.FileStore, "ready", ready)
    with pytest.raises(ValueError, match="meanwhile"):
        store.set("NEW", "new-value")
    monkeypatch.setattr(ss.FileStore, "ready", real)
    assert ss.FileStore().get("NEW") is None
    assert ss.FileStore().get("OLD") == "old-value"
    store.set("NEW", "new-value", plain=True)
    assert ss.FileStore().get("NEW") == "new-value"


# --- gc: a compaction killed ------------------------------------------------------------------------------------------

def test_a_compaction_killed_leaves_no_temporary_file_for_good(tmp_path):
    """gc's compression and split write .NAME.gz.PID.tmp and .NAME.rest.PID.tmp next to the log; killed (the daily
    background pass at a shutdown, kill -9) they stayed for good - as large as the log they were made of, counted
    by gc --usage and removed by nothing. The next compaction removes such a file of a process that is gone, once
    it was not written for an hour; one of a compaction that still runs stays."""
    sd = load("statedirs")
    folder = tmp_path / "logs" / "proj-1"
    folder.mkdir(parents=True)
    gone = 2 ** 22 + 12345  # (above any pid_max: no such process)
    dead = [folder / f".files.jsonl.rest.{gone}.tmp", folder / f".connections-20261101-100000-a.jsonl.gz.{gone}.tmp",
            folder / f".files.jsonl.20261101-100000.gz.{gone}.tmp"]
    alive = folder / f".files.jsonl.rest.{os.getpid()}.tmp"
    fresh = folder / f".dns.jsonl.rest.{gone + 1}.tmp"
    other = folder / ".something-else.tmp"
    for path in (*dead, alive, fresh, other):
        path.write_bytes(b"x" * 1000)
        if path is not fresh:
            os.utime(path, (1, 1))
    done = sd.compact(tmp_path / "logs", now=10_000)
    assert not any(path.exists() for path in dead)
    assert alive.exists() and fresh.exists() and other.exists()
    assert sorted(done["dropped"]) == sorted(path.name for path in dead) and done["saved"] > 0
    assert sd.compact(tmp_path / "logs", now=10_000, dry_run=True)["dropped"] == []


# --- mcp copy: the image's pipe -----------------------------------------------------------------------------------------

def test_an_image_copy_whose_sender_says_much_does_not_hang(monkeypatch):
    """copy_image read the receiving side's output to the end before anything of the sending side's standard error:
    a sender that says more than a pipe holds (64 KiB: podman save's progress, ssh's warnings) stopped there, its
    image half sent, and the copy waited the whole hour of IMAGE_TIMEOUT. Its standard error goes to a file now."""
    mc = load("mcp_copy")
    sender = ("import sys; sys.stderr.write('Copying blob sha256:' + 'f' * 64 + ' done\\n' * 1) ; "
              "[sys.stderr.write('x' * 1000 + '\\n') for _ in range(300)]; sys.stderr.flush(); "
              "sys.stdout.buffer.write(b'IMAGE' * 1000)")
    receiver = "import sys; data = sys.stdin.buffer.read(); print(len(data))"
    argvs = {"src": [sys.executable, "-c", sender], "dst": [sys.executable, "-c", receiver]}
    monkeypatch.setattr(mc, "hosts_module", lambda: type("H", (), {"argv": staticmethod(lambda host, args: argvs[host])}))
    monkeypatch.setattr(mc, "host_label", lambda host: host)
    monkeypatch.setattr(mc, "IMAGE_TIMEOUT", 20)
    document = {"name": "files", "image": {"ref": "example.org/x@sha256:" + "0" * 64, "size": 5000}}
    said = mc.copy_image("src", "dst", document, out=lambda *args: None)
    assert said.strip() == b"5000"


# --- the proxy's questions to the supervisor ---------------------------------------------------------------------------

@pytest.mark.parametrize("how", ["closed", "reset"])
def test_questions_waiting_when_the_supervisors_channel_ends_are_denied_at_once(how):
    """The proxy's reader of the supervisor's answers ended with the channel (an unhandled ConnectionResetError on a
    reset) and left every question already asked waiting its whole APPROVAL_SECONDS + 10 for an answer that could not
    come any more. Now they are answered "no answer" (denied) as soon as the channel ends."""
    import struct
    import time
    rt = load("runtime")
    proxy, supervisor = socket.socketpair()
    channel = rt.AskChannel(proxy, timeout=60)
    slot = channel.request("connect", {"host": "example.org"})
    assert supervisor.recv(4096)
    started = time.monotonic()
    if how == "reset":
        supervisor.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    supervisor.close()
    assert slot.result(10) is None and time.monotonic() - started < 5
    assert channel("connect", {"host": "example.org"})["decision"] == "deny"
    proxy.close()


# --- a remote folder whose host is gone -----------------------------------------------------------------------------

def test_disk_usage_never_looks_into_a_mounted_folder(tmp_path, monkeypatch):
    """gc --usage, the app's Disk usage and uninstall's table measured the state directory with lstat() on every
    folder - the mount points of remote folders (state/sandbox-ai/remote/RUN/N/LABEL) too: one whose host is gone
    (an NFS mount, hard by default; sshfs reconnecting) holds lstat() for as long as the host stays away, and the
    scan with it, whatever its time budget says. A mount point (mountinfo) is now skipped without being looked at."""
    sd = load("statedirs")
    state = tmp_path / "state"
    point = state / "remote" / "run-1" / "1" / "data"
    point.mkdir(parents=True)
    (point / "big").write_bytes(b"x" * 100_000)
    (state / "remote" / "run-1" / "state.json").write_text("{}")
    real, looked = os.lstat, []

    def lstat(path, *args, **kwargs):
        looked.append(os.path.abspath(path))
        return real(path, *args, **kwargs)
    monkeypatch.setattr(sd, "mount_points", lambda: {str(point), "/"})
    scan = sd.Scan()
    with monkeypatch.context() as patch:
        patch.setattr(sd.os, "lstat", lstat)
        size = scan.size(state)
    assert str(point) not in looked and str(state) in looked
    assert 0 < size < 100_000 and scan.partial


def test_releasing_remote_folders_never_looks_into_a_mount_point(tmp_path, monkeypatch):
    """remotefs.release (a run's end, gc) asked is_dir() and exists() of each mount point before unmounting it: on a
    folder whose host is gone that waited as long as the host stays away, before the unmount that ends it."""
    rf = load("remotefs")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    folder = rf.run_dir("run-1")
    point = folder / "1" / "data"
    point.mkdir(parents=True)
    rf.save("run-1", [{"source": "nfs://nas/export", "point": str(point), "state": "mounted"}])
    real_stat = Path.stat

    def stat(self, *args, **kwargs):
        assert str(self) != str(point), "the mount point was looked at"
        return real_stat(self, *args, **kwargs)
    monkeypatch.setattr(rf.Path, "stat", stat)
    unmounted = []

    def runner(command, **kwargs):
        unmounted.append(command[-1])
        return type("Done", (), {"returncode": 0, "stderr": ""})()
    result = rf.release("run-1", runner=runner, which=lambda name: f"/usr/bin/{name}", check=lambda p: False)
    assert unmounted == [str(point)] and result == [("nfs://nas/export", str(point), None)]


# --- bridges across hosts: no thread to be had ------------------------------------------------------------------------

def test_a_bridge_call_without_a_thread_is_refused_and_the_link_reads_on(monkeypatch):
    """A call across a bridge is served in a thread of its own; where none could be started (the process limit),
    RuntimeError ended the link's reader silently - its call slot kept, nothing read from the other half again, the
    bridge hanging until LINK_IDLE. Now that call is answered with the reason and the reader goes on."""
    import io
    br = load("bridge_remote")
    written = io.BytesIO()
    link = br.Link(None, written, "test")
    real_start = threading.Thread.start

    def start(self):
        if getattr(self, "_target", None) == link.serve_one:
            raise RuntimeError("can't start new thread")
        return real_start(self)
    monkeypatch.setattr(threading.Thread, "start", start)
    for ident in range(1, br.LINK_IN_FLIGHT + 3):
        link.dispatch(None, {"t": "req", "id": ident, "op": "call", "body": {}})
    answers = [json.loads(line) for line in written.getvalue().splitlines()]
    assert len(answers) == br.LINK_IN_FLIGHT + 2
    assert all(not answer["body"]["ok"] and "thread" in answer["body"]["text"] for answer in answers)


def test_the_egress_rules_child_loads_no_library_after_the_fork(monkeypatch):
    """load_into_sandbox's child (preexec_fn, between fork and exec) loaded libc with ctypes.CDLL - a dlopen() after
    a fork of a process with threads, which can wait forever on a loader lock held at the fork. libc is loaded in
    the parent now and handed to the child."""
    eg = load("mcp_egress")
    loaded, entered = [], []
    monkeypatch.setattr(eg.ctypes, "CDLL", lambda *args, **kwargs: loaded.append(args) or "libc")
    monkeypatch.setattr(eg, "enter_namespaces", lambda pid, libc=None: entered.append((pid, libc)))

    def run(command, preexec_fn=None, **options):
        before = len(loaded)
        preexec_fn()  # (what the child runs)
        assert len(loaded) == before, "a library loaded in the child"
        return type("Done", (), {"returncode": 1, "stderr": "stop here", "stdout": ""})()
    monkeypatch.setattr(eg.subprocess, "run", run)
    monkeypatch.setattr(eg.shutil, "which", lambda name: "/usr/sbin/nft")
    with pytest.raises(ValueError, match="stop here"):
        eg.load_into_sandbox(1234, "table inet x {}", "full")
    assert entered == [(1234, "libc")] and len(loaded) == 1


def test_bridges_of_hosts_a_host_too_slow_is_said_and_never_sorted_into(monkeypatch):
    """`bridge list --hosts` waited HOSTS_WAIT + 15 s for each host in turn after the first that did not end, and sorted
    the list while a late host's thread could still add to it ("list modified during sort"). One wait for all now;
    a host still answering then is said, and what it adds later reaches nothing."""
    import time
    import types
    bridge = load("bridge")
    br = bridge.remote() if hasattr(bridge, "remote") else load("bridge_remote")
    release = threading.Event()

    def run_json(host, argv, timeout):
        if host == "slow":
            release.wait(10)
        return [{"id": f"{host}-1", "created": "2026-11-17T00:00:00Z"}]
    hosts = types.SimpleNamespace(LOCAL="local", load=lambda: ["fast", "slow"], run_json=run_json)
    monkeypatch.setattr(br.b, "module", lambda name: hosts)
    monkeypatch.setattr(br.b, "listing", lambda include_all=False: [])
    monkeypatch.setattr(br, "HOSTS_WAIT", -14.5)  # (one wait of half a second for all)
    monkeypatch.setattr(br, "host_rows", lambda host, rows: [{**row, "host": host} for row in rows])
    monkeypatch.setattr(br, "merged_rows", lambda local, remote: remote)
    problems = []
    started = time.monotonic()
    rows = br.listing_of_hosts(problems=problems)
    assert time.monotonic() - started < 3
    assert [row["host"] for row in rows] == ["fast"]
    assert [host for host, _ in problems] == ["slow"] and "no answer" in problems[0][1]
    release.set()
