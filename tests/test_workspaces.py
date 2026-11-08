"""sandbox-ai workspace: a host directory added to a running sandbox, and taken back (the namespace work is faked
here; the live tests run it for real)."""
import ctypes
import errno
import importlib.util
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sandbox_workspaces_tests", ROOT / "sandbox/workspaces.py")
w = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(w)
L = w.module("agent_sandbox")


class FakeLibc:
    """Records the calls; FAIL_MOVE: the move_mount that fails (1 = the first); BUSY: a strict unmount fails with
    EBUSY (something still holds the tree)."""

    def __init__(self, fail_move=None, setattr_errno=None, busy=False):
        self.calls, self.next_fd, self.moves = [], 900, 0
        self.fail_move, self.setattr_errno, self.busy = fail_move, setattr_errno, busy

    def setns(self, fd, kind):
        self.calls.append(("setns", kind))
        return 0

    def unshare(self, kind):
        self.calls.append(("unshare", kind))
        return 0

    def syscall(self, number, *args):
        self.calls.append(("syscall", number))
        if number == w.SYS_OPEN_TREE:
            self.next_fd += 1
            return os.open("/", os.O_PATH)  # a real descriptor: clone() may close it
        if number == w.SYS_MOUNT_SETATTR and self.setattr_errno:
            ctypes.set_errno(self.setattr_errno)
            return -1
        if number == w.SYS_MOVE_MOUNT:
            self.moves += 1
            if self.moves == self.fail_move:
                ctypes.set_errno(errno.EPERM)
                return -1
        return 0

    def umount2(self, path, flags):
        self.calls.append(("umount2", path, flags))
        if self.busy and not flags & w.MNT_DETACH:
            ctypes.set_errno(errno.EBUSY)
            return -1
        return 0


@pytest.fixture
def space(tmp_path, monkeypatch):
    """A fake /proc with a supervisor and the agent's Firejail namespace, a /workspaces of the sandbox (shared by
    both namespaces, as the tmpfs is) and a host directory to add."""
    proc = tmp_path / "proc"
    for pid in (100, 101):
        (proc / str(pid) / "ns").mkdir(parents=True)
        for kind in ("user", "mnt"):
            (proc / str(pid) / "ns" / kind).write_text("")  # os.open needs files where /proc has links
    mounts = tmp_path / "sandbox-workspaces"
    mounts.mkdir()
    monkeypatch.setattr(w, "WORKSPACES_DIR", str(mounts))
    source = tmp_path / "project"
    source.mkdir()
    info = source.stat()
    return proc, mounts, source, [info.st_dev, info.st_ino]


def kinds(calls):
    return [call[0] if call[0] != "syscall" else call[1] for call in calls]


def test_the_helper_clones_the_checked_directory_once_per_namespace_and_mounts_it(space):
    proc, mounts, source, identity = space
    c = FakeLibc()
    w.helper("add", 100, "project", [100, 101], str(source), identity, "ro", proc, c)
    assert kinds(c.calls) == ["setns", "unshare", w.SYS_OPEN_TREE, w.SYS_MOUNT_SETATTR, w.SYS_OPEN_TREE,
                              w.SYS_MOUNT_SETATTR, "setns", w.SYS_MOVE_MOUNT, "setns", w.SYS_MOVE_MOUNT]
    assert c.calls[0] == ("setns", w.CLONE_NEWUSER) and c.calls[1] == ("unshare", w.CLONE_NEWNS)
    assert (mounts / "project").is_dir()


def test_a_directory_swapped_after_the_check_is_not_mounted(space, tmp_path):
    proc, mounts, source, identity = space
    source.rename(tmp_path / "moved")
    source.mkdir()  # another directory under the checked name
    c = FakeLibc()
    with pytest.raises(OSError, match="changed after it was checked"):
        w.helper("add", 100, "project", [100], str(source), identity, "rw", proc, c)
    assert w.SYS_MOVE_MOUNT not in kinds(c.calls)
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "moved")
    with pytest.raises(OSError):
        w.helper("add", 100, "project", [100], str(link), identity, "rw", proc, FakeLibc())  # no link followed


def test_read_only_needs_mount_setattr_and_rw_goes_without(space):
    proc, _, source, identity = space
    with pytest.raises(OSError, match="5.12"):
        w.helper("add", 100, "project", [100], str(source), identity, "ro", proc, FakeLibc(setattr_errno=errno.ENOSYS))
    w.helper("add", 100, "project", [100], str(source), identity, "rw", proc, FakeLibc(setattr_errno=errno.ENOSYS))
    with pytest.raises(OSError, match="nosuid"):
        w.helper("add", 100, "project", [100], str(source), identity, "rw", proc, FakeLibc(setattr_errno=errno.EPERM))


def test_an_add_failing_in_one_namespace_is_undone_in_the_others(space):
    proc, mounts, source, identity = space
    c = FakeLibc(fail_move=2)
    with pytest.raises(OSError, match="Operation not permitted"):
        w.helper("add", 100, "project", [100, 101], str(source), identity, "rw", proc, c)
    # Back into the first namespace to take it out again (nothing really mounted here: only the mount point goes).
    assert kinds(c.calls)[-1] == "setns" and not (mounts / "project").exists()


def test_a_mount_point_made_a_link_by_the_agent_is_refused(space, tmp_path):
    proc, mounts, source, identity = space
    (mounts / "project").symlink_to(tmp_path)
    c = FakeLibc()
    with pytest.raises(OSError, match="no directory in the sandbox"):
        w.helper("add", 100, "project", [100], str(source), identity, "rw", proc, c)
    assert w.SYS_MOVE_MOUNT not in kinds(c.calls)


def test_a_sandbox_without_its_workspaces_tmpfs_says_how_to_go_on(space, monkeypatch, tmp_path):
    proc, _, source, identity = space
    monkeypatch.setattr(w, "WORKSPACES_DIR", str(tmp_path / "none"))
    with pytest.raises(OSError, match="restart it with the workspace"):
        w.helper("add", 100, "project", [100], str(source), identity, "rw", proc, FakeLibc())


def test_remove_unmounts_only_what_is_mounted(space):
    proc, mounts, _, _ = space
    (mounts / "project").mkdir()
    c = FakeLibc()
    w.helper("remove", 100, "project", [100, 101], proc=proc, c=c)
    # Nothing is mounted at the fake mount point (same device as /workspaces): no unmount, the directory goes.
    assert "umount2" not in kinds(c.calls) and not (mounts / "project").exists()


# --- the checks and the registry -----------------------------------------------------------------------------


@pytest.fixture
def run(tmp_path, monkeypatch):
    home = tmp_path / "sandbox-home"
    run_dir, logs, session = tmp_path / "run", tmp_path / "logs", tmp_path / "session"
    for directory in (home, run_dir, logs, session):
        directory.mkdir()
    start = tmp_path / "start"
    start.mkdir()
    store = tmp_path / "kits"
    record = {"run": "mine", "backend": "podman", "directory": str(run_dir), "log_dir": str(logs),
              "session_dir": str(session), "workspace": str(start),
              "workspace_checks": {"workspaces": [{"host": str(start), "sandbox": "/work", "mode": "rw"},
                                                  {"host": str(tmp_path / "other" / "lib"),
                                                   "sandbox": "/workspaces/lib", "mode": "rw"}],
                                   "read_only": False, "trees": [str(home)],
                                   "held": [[str(tmp_path / "config" / "rules.json"), "Your rules (XDG_CONFIG_HOME)"]],
                                   "private": [[str(logs), "--log-dir"]],
                                   "stores": [[str(store), "the kits directory"]]}}
    monkeypatch.setattr(L, "load_policy", lambda path=None: {})
    monkeypatch.setattr(L, "check_workspace_scope", lambda path: None)
    monkeypatch.setattr(L, "exposed_before", lambda: [])
    return record, tmp_path


def make(path):
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def test_a_workspace_gets_the_checks_of_a_start(run, monkeypatch):
    record, tmp = run
    judged = lambda value, ro=False: w.judged(record, Path(value).resolve(), ro)  # noqa: E731
    assert judged(make(tmp / "fresh")) == "rw" and judged(make(tmp / "fresh"), True) == "ro"
    with pytest.raises(ValueError, match="must not overlap"):
        judged(make(tmp / "start" / "sub"))
    with pytest.raises(ValueError, match="must not overlap"):
        judged(make(tmp / "other"))  # holds the start's /workspaces/lib
    with pytest.raises(ValueError, match="HOME of the run"):
        judged(make(tmp / "sandbox-home" / "x"))
    with pytest.raises(ValueError, match="Your rules"):
        judged(make(tmp / "config"))  # holds the rules file
    with pytest.raises(ValueError, match="--log-dir"):
        judged(make(tmp / "logs" / "inner"))
    with pytest.raises(ValueError, match="kits directory"):
        judged(make(tmp / "kits" / "mine"))
    with pytest.raises(ValueError, match="administrator's policy"):
        monkeypatch.setattr(L, "load_policy", lambda path=None: {"forbid": ["--workspace"]})
        judged(make(tmp / "fresh"))
    monkeypatch.setattr(L, "load_policy", lambda path=None: {})
    record["workspace_checks"]["read_only"] = True  # --workspace-ro, or the mode high
    assert judged(make(tmp / "fresh")) == "ro"
    with pytest.raises(ValueError, match="0.1.75 or older"):
        w.judged({"run": "old"}, tmp / "fresh")


def test_the_scope_checks_of_a_start_hold(run, monkeypatch):
    record, tmp = run
    calls = []
    monkeypatch.setattr(L, "check_workspace_scope", lambda path: calls.append(path) or (_ for _ in ()).throw(
        ValueError("is or contains your HOME directory")))
    with pytest.raises(ValueError, match="HOME directory"):
        w.judged(record, Path(make(tmp / "fresh")))
    assert calls == [Path(make(tmp / "fresh"))]


def test_a_planted_link_or_odd_path_is_refused_before_anything_is_opened(run, monkeypatch):
    _, tmp = run
    (tmp / "start" / "planted").symlink_to(make(tmp / "secret"))
    monkeypatch.setattr(L, "exposed_before", lambda: [tmp / "start"])
    with pytest.raises(ValueError, match="planted"):
        w.opened(str(tmp / "start" / "planted"))
    with pytest.raises(ValueError, match="commas"):
        w.opened(make(tmp / "a,b"))
    with pytest.raises(ValueError, match="no such directory"):
        w.opened(str(tmp / "missing"))
    (tmp / "file").write_text("")
    with pytest.raises(ValueError, match="not a directory"):
        w.opened(str(tmp / "file"))
    fd, real, identity = w.opened(str(tmp / "start" / ".." / "secret"))
    os.close(fd)
    assert real == tmp / "secret" and identity == [real.stat().st_dev, real.stat().st_ino]


@pytest.fixture(autouse=True)
def no_supervisor(monkeypatch):
    """No run answers on a control socket here: the file log is asked through watch_file_log."""
    monkeypatch.setattr(w, "watch_file_log", lambda record, path, out=print: False)
    monkeypatch.setattr(w, "copies", lambda found, path, proc=None, group=None: [])  # no real /proc behind the fake processes


def fake_found(record):
    return {"pids": [100, 101], "mntns": {"a": 100, "b": 101}, "user": 100, "cgroup": "/x"}


def test_add_list_remove(run):
    record, tmp = run
    calls, lines, remembered = [], [], []
    runner = lambda op, found, name, *rest, **kw: calls.append((op, name, *rest)) or (0, "")  # noqa: E731
    path = make(tmp / "elsewhere" / "lib")
    sandbox = w.add(record, path, out=lines.append, runner=runner, finder=fake_found, remember=remembered.extend)
    # /workspaces/lib is the start's: the next name, as a restart would give it.
    assert sandbox == "/workspaces/lib-2" and calls[0][:4] == ("add", "lib-2", path, [os.stat(path).st_dev,
                                                                                     os.stat(path).st_ino])
    assert calls[0][4] == "rw" and remembered == [Path(path)]
    assert "push" in lines[-1]
    entries = w.listing(record)
    assert [(e["sandbox"], e["source"]) for e in entries] == [("/work", "start"), ("/workspaces/lib", "start"),
                                                              ("/workspaces/lib-2", "added")]
    assert "lib-2" in "\n".join(w.list_lines(record, entries))
    with pytest.raises(ValueError, match="must not overlap"):
        w.add(record, path, runner=runner, finder=fake_found, remember=remembered.extend)
    with pytest.raises(ValueError, match="workspace of the start"):
        w.remove(record, "/work", runner=runner, finder=fake_found, using=lambda found, p: [])
    with pytest.raises(ValueError, match="in use in the sandbox: 101 bash"):
        w.remove(record, "lib-2", runner=runner, finder=fake_found, using=lambda found, p: [(101, "bash", "cwd")])
    w.remove(record, path, out=lines.append, runner=runner, finder=fake_found, using=lambda found, p: [])
    assert calls[-1] == ("remove", "lib-2") and w.added_workspaces(record) == []
    with pytest.raises(ValueError, match="not added during this run"):
        w.remove(record, "lib-2", runner=runner, finder=fake_found)
    log = [json.loads(line) for line in (tmp / "logs" / "workspaces.jsonl").read_text().splitlines()]
    assert [entry["event"] for entry in log] == ["add", "remove"] and log[0]["sandbox"] == "/workspaces/lib-2"


def test_force_removes_a_busy_workspace_and_says_what_stays_open(run):
    record, tmp = run
    runner = lambda op, found, name, *rest, **kw: (0, "")  # noqa: E731
    w.add(record, make(tmp / "fresh"), out=lambda line: None, runner=runner, finder=fake_found,
          remember=lambda trees: None)
    lines = []
    w.remove(record, "/workspaces/fresh", force=True, out=lines.append, runner=runner, finder=fake_found,
             using=lambda found, p: [(101, "python3", "an open file")])
    assert "stays usable, also for writing" in lines[0] and "101" in lines[0]
    log = [json.loads(line) for line in (tmp / "logs" / "workspaces.jsonl").read_text().splitlines()]
    assert log[-1]["forced"] is True and log[-1]["open"] == [101]


def test_a_failed_mount_records_nothing_and_a_read_only_one_is_not_remembered(run):
    record, tmp = run
    with pytest.raises(ValueError, match="Operation not permitted"):
        w.add(record, make(tmp / "fresh"), runner=lambda *a, **kw: (1, "Operation not permitted"), finder=fake_found,
              remember=lambda trees: None)
    assert w.added_workspaces(record) == []
    remembered = []
    w.add(record, make(tmp / "fresh"), read_only=True, out=lambda line: None, runner=lambda *a, **kw: (0, ""),
          finder=fake_found, remember=remembered.extend)
    assert remembered == [] and w.added_workspaces(record)[0]["mode"] == "ro"
    with pytest.raises(ValueError, match="has it ended"):
        w.add(record, make(tmp / "third"), runner=lambda *a, **kw: (0, ""), finder=lambda r: None,
              remember=lambda trees: None)


def test_users_are_found_through_their_cwd_files_and_mappings(tmp_path):
    proc = tmp_path / "proc"
    for pid, command in ((100, "python3"), (101, "bash"), (102, "vim")):
        (proc / str(pid) / "fd").mkdir(parents=True)
        (proc / str(pid) / "comm").write_text(command + "\n")
        (proc / str(pid) / "root").symlink_to("/")
    (proc / "100" / "cwd").symlink_to("/work")
    (proc / "100" / "maps").write_text("7f0-7f1 r-xp 00000000 00:2a 12 /workspaces/lib/x.so\n")
    (proc / "101" / "cwd").symlink_to("/workspaces/lib/src")
    (proc / "102" / "cwd").symlink_to("/work")
    (proc / "102" / "fd" / "3").symlink_to("/workspaces/library/notes.txt")  # another workspace
    found = {"pids": [100, 101, 102]}
    assert w.users(found, "/workspaces/lib", proc) == [(100, "python3", "a mapped file"), (101, "bash", "cwd")]


def test_a_session_keeps_what_was_added_for_its_next_start(run, monkeypatch):
    record, tmp = run
    saved = {"options": ["--workspace", str(tmp / "start")], "agent": "claude", "cwd": str(tmp)}
    sessions = w.module("sessions")
    monkeypatch.setattr(w, "session_of", lambda r: "s1")
    monkeypatch.setattr(sessions, "session_lock", lambda name: __import__("contextlib").nullcontext())
    monkeypatch.setattr(sessions, "load", lambda name: json.loads(json.dumps(saved)))
    monkeypatch.setattr(sessions, "save", lambda name, data: saved.update(data))
    monkeypatch.setattr(sessions, "added_options", lambda data, workspaces=(): data["options"] + sum(
        (["--workspace", path] for path in workspaces), []))
    lines = []
    path = make(tmp / "fresh")
    w.add(record, path, out=lines.append, runner=lambda *a, **kw: (0, ""), finder=fake_found, remember=lambda trees: None)
    assert saved["options"][-2:] == ["--workspace", path] and "kept for the session" in lines[-1]
    assert w.listing(record)[-1]["kept"] is True
    w.remove(record, "fresh", out=lines.append, runner=lambda *a, **kw: (0, ""), finder=fake_found, using=lambda f, p: [])
    assert saved["options"] == ["--workspace", str(tmp / "start")] and "session options" in lines[-1]


def test_the_command_line(run, monkeypatch, capsys):
    record, tmp = run
    net = w.module("net")
    monkeypatch.setattr(net, "running", lambda: {"mine": record})
    added = []
    monkeypatch.setattr(w, "add", lambda r, value, ro, out=print: added.append((r["run"], value, ro)))
    assert w.main(["add", "mine", str(tmp), "--ro"]) == 0 and added == [("mine", str(tmp), True)]
    assert w.main(["add", str(tmp)]) == 0 and added[-1] == ("mine", str(tmp), False)
    assert w.main(["add", "a", "b", "c"]) == 1 and "[RUN] DIR" in capsys.readouterr().err
    lines = []
    assert w.main(["list", "--json"], out=lines.append) == 0
    assert json.loads(lines[0])[0] == {"host": str(tmp / "start"), "sandbox": "/work", "mode": "rw",
                                       "source": "start"}
    main = w.module("main")
    assert main.PYTHON_COMMANDS["workspace"] == ("workspaces.py",) and "sandbox-ai workspace" in main.__doc__


def test_the_start_records_what_a_later_workspace_is_checked_against(tmp_path, monkeypatch):
    workspace, other, state = tmp_path / "w", tmp_path / "x" / "lib", tmp_path / "state"
    for directory in (workspace, other, state, tmp_path / "data"):
        directory.mkdir(parents=True)
    monkeypatch.setattr(L, "check_workspace_scope", lambda path: None)
    a = L.parse_args(["claude", "--workspace", f"{workspace},{other}", "--state-dir", str(state),
                      "--mount", f"{tmp_path}/data:/mnt/data:ro", "--workspace-ro"])
    a.sandbox_home = tmp_path / "home"
    a.mode_other_workspaces_ro = False
    checks = L.workspace_checks(a, tmp_path / "session")
    assert checks["workspaces"] == [{"host": str(workspace), "sandbox": "/work", "mode": "ro"},
                                    {"host": str(other), "sandbox": "/workspaces/lib", "mode": "ro"}]
    assert checks["read_only"] is True
    assert {str(state), f"{tmp_path}/data", str(tmp_path / "home")} <= set(checks["trees"])
    assert any(label.startswith("The run directory") for _, label in checks["held"])
    assert [str(tmp_path / "session"), "The run's session directory"] in checks["private"]
    json.dumps(checks)


def test_every_start_has_a_small_tmpfs_for_workspaces_added_later():
    assert L.WORKSPACES_DIR == w.WORKSPACES_DIR == "/workspaces"
    assert "size=1m" in L.WORKSPACES_TMPFS and "noexec" in L.WORKSPACES_TMPFS
    source = (ROOT / "sandbox/agent_sandbox.py").read_text()
    assert 'f"--tmpfs={WORKSPACES_DIR}:{WORKSPACES_TMPFS}"' in source and '"--tmpfs", WORKSPACES_DIR]' in source


def test_the_file_log_is_asked_to_watch_an_added_workspace(run, monkeypatch, tmp_path):
    record, tmp = run
    net = w.module("net")
    asked = []
    monkeypatch.setattr(net, "ask", lambda directory, request: asked.append(request) or {"ok": True, "watched": True})
    real = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(real)
    real._modules["net"] = net
    assert real.watch_file_log(record, "/workspaces/x") is True and asked == [{"op": "watch", "path": "/workspaces/x"}]
    lines = []
    monkeypatch.setattr(net, "ask", lambda directory, request: (_ for _ in ()).throw(ValueError("refused: op")))
    assert real.watch_file_log(record, "/workspaces/x", lines.append) is None and "not in the file log" in lines[0]
    watched = []
    w.add(record, make(tmp / "fresh"), out=lambda line: None, runner=lambda *a, **kw: (0, ""), finder=fake_found,
          remember=lambda trees: None, watcher=lambda r, path, out: watched.append(path) or True)
    assert watched == ["/workspaces/fresh"]
    log = [json.loads(line) for line in (tmp / "logs" / "workspaces.jsonl").read_text().splitlines()]
    assert log[-1]["watched"] is True


def test_remove_unmounts_strictly_and_detaches_a_busy_tree_only_by_force(space, monkeypatch):
    """A strict unmount first: the kernel refuses while anything holds the tree (also what /proc does not show, a
    descriptor in flight, io_uring); --force detaches it then."""
    proc, mounts, _, _ = space
    (mounts / "project").mkdir()
    monkeypatch.setattr(w, "mounted", lambda base, target: True)
    c = FakeLibc(busy=True)
    with pytest.raises(OSError, match="still in use"):
        w.helper("remove", 100, "project", [100], proc=proc, c=c)
    assert [call[2] for call in c.calls if call[0] == "umount2"] == [w.UMOUNT_NOFOLLOW]
    c = FakeLibc(busy=True)
    w.helper("remove", 100, "project", [100], proc=proc, c=c, force=True)
    assert [call[2] for call in c.calls if call[0] == "umount2"] == [w.UMOUNT_NOFOLLOW, w.MNT_DETACH | w.UMOUNT_NOFOLLOW]
    (mounts / "project").mkdir()  # the mount point went with the detached tree
    c = FakeLibc()
    w.helper("remove", 100, "project", [100], proc=proc, c=c)
    assert [call[2] for call in c.calls if call[0] == "umount2"] == [w.UMOUNT_NOFOLLOW]  # not busy: no detaching


def test_only_the_sandboxs_own_mount_namespaces_get_the_mount(tmp_path):
    """A user and mount namespace the agent made itself (it holds every right there and could undo read-only) is
    never a target: only those of the sandbox's own user namespace."""
    proc = tmp_path / "proc"
    for pid, user in ((100, "user:[1]"), (101, "user:[1]"), (102, "user:[9]")):
        (proc / str(pid) / "ns").mkdir(parents=True)
        (proc / str(pid) / "ns" / "user").symlink_to(user)
    found = {"user": 100, "mntns": {"a": 100, "b": 101, "agent-made": 102}}
    assert w.own_namespaces(found, proc) == [100, 101]
    assert w.own_namespaces({**found, "user": 999}, proc) == []  # the anchor gone: nothing, never everything


def test_the_sandbox_is_frozen_while_its_use_is_checked(tmp_path):
    group = tmp_path / "cgroup" / "user.slice" / "pod"
    group.mkdir(parents=True)
    (group / "cgroup.freeze").write_text("0\n")
    (group / "cgroup.events").write_text("populated 1\nfrozen 1\n")
    found = {"cgroup": "/user.slice/pod"}
    with w.frozen(found, root=tmp_path / "cgroup", group="/user.slice/pod") as thawed:
        assert thawed and (group / "cgroup.freeze").read_text() == "1"
    assert (group / "cgroup.freeze").read_text() == "0"
    (group / "cgroup.freeze").write_text("1\n")  # paused by sandbox-ai pause: it stays paused
    with w.frozen(found, root=tmp_path / "cgroup", group="/user.slice/pod") as thawed:
        assert not thawed
    assert (group / "cgroup.freeze").read_text().strip() == "1"
    with w.frozen({"cgroup": "/nowhere"}, root=tmp_path / "cgroup", group="/nowhere") as thawed:
        assert not thawed  # not delegated or gone: checked without freezing
    (group / "cgroup.freeze").write_text("0\n")
    with w.frozen(found, root=tmp_path / "cgroup", group=None) as thawed:
        assert not thawed and (group / "cgroup.freeze").read_text().strip() == "0"  # not the run's own: never
    # Bubblewrap without a scope of its own runs in the launcher's cgroup (a terminal's, tmux's): never frozen.
    assert w.own_cgroup({"backend": "bwrap"}, found) is None
    assert w.own_cgroup({"backend": "bwrap", "own_cgroup": True}, found) == "/user.slice/pod"
    assert w.own_cgroup({"backend": "podman"}, found) == "/user.slice/pod"


def test_a_workspace_holding_a_path_directory_is_refused(run, monkeypatch):
    record, tmp = run
    tools = Path(make(tmp / "proj" / ".venv" / "bin"))
    monkeypatch.setattr(L, "private_locations", lambda: ([], [tools]))
    with pytest.raises(ValueError, match="directory on your PATH"):
        w.judged(record, tmp / "proj")
    assert w.judged(record, Path(make(tmp / "other-proj"))) == "rw"


def test_a_read_only_add_is_not_kept_for_a_restart(run, monkeypatch):
    """Kept as --workspace it would come back writable after a restart: a --ro add lasts for the run only (unless
    the run keeps every further workspace read-only anyway)."""
    record, tmp = run
    kept = []
    monkeypatch.setattr(w, "session_of", lambda r: "s1")
    monkeypatch.setattr(w, "keep", lambda r, host, add=True: kept.append(host) or True)
    lines = []
    w.add(record, make(tmp / "fresh"), read_only=True, out=lines.append, runner=lambda *a, **kw: (0, ""),
          finder=fake_found, remember=lambda trees: None)
    assert kept == [] and any("added read-only" in line for line in lines)
    record["workspace_checks"]["read_only"] = True
    w.add(record, make(tmp / "second"), out=lines.append, runner=lambda *a, **kw: (0, ""), finder=fake_found,
          remember=lambda trees: None)
    assert kept == [str(tmp / "second")]  # read-only at a restart too: kept


def test_a_copy_the_agent_took_into_its_own_namespace_is_found(tmp_path):
    """unshare -Urm in the sandbox gives the agent a mount namespace of its own with a copy of /workspaces/NAME, which
    no unmount of ours reaches: found by device and root in every process of the sandbox's cgroup."""
    proc = tmp_path / "proc"
    line = "{id} 1 0:52 /home/me/proj {point} rw,relatime - ext4 /dev/sda1 rw\n"
    def process(pid, user, mnt, mounts, cgroup="/pod/libpod-x.scope"):
        (proc / str(pid) / "ns").mkdir(parents=True)
        (proc / str(pid) / "ns" / "user").symlink_to(f"user:[{user}]")
        (proc / str(pid) / "ns" / "mnt").symlink_to(f"mnt:[{mnt}]")
        (proc / str(pid) / "cgroup").write_text(f"0::{cgroup}\n")
        (proc / str(pid) / "mountinfo").write_text("".join(line.format(id=i, point=p) for i, p in enumerate(mounts)))
    process(100, 1, 10, ["/workspaces/proj"])          # the supervisor's
    process(101, 1, 11, ["/workspaces/proj"])          # the agent under Firejail
    process(102, 1, 11, [])                            # same namespace: read once
    process(103, 9, 12, ["/tmp/mine"])                 # the agent's own namespace, the copy moved elsewhere
    process(104, 1, 13, ["/elsewhere"], cgroup="/other.scope")  # another sandbox: not looked at
    found = {"user": 100, "mntns": {"a": 100, "b": 101}, "pids": [100, 101, 102], "cgroup": "/pod"}
    copies = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(copies)
    copies = copies.copies  # the module's own, not the autouse stand-in
    assert copies(found, "/workspaces/proj", proc, group="/pod") == [(103, "/tmp/mine")]
    (proc / "103" / "mountinfo").write_text(line.format(id=1, point="/") .replace("/home/me/proj", "/home/me/other"))
    assert copies(found, "/workspaces/proj", proc, group="/pod") == []  # another tree of the same disk is no copy
    # A name with a space is written \\040 in mountinfo: still found (never "no copies" for want of a match).
    for pid in (100, 101):
        (proc / str(pid) / "mountinfo").write_text("1 1 0:52 /home/me/my\\040lib /workspaces/my\\040lib rw - ext4 x rw\n")
    (proc / "103" / "mountinfo").write_text("1 1 0:52 /home/me/my\\040lib /tmp/x rw - ext4 x rw\n")
    assert copies(found, "/workspaces/my lib", proc, group="/pod") == [(103, "/tmp/x")]
    # The sandbox's own mount not found at all: nothing can be told, and the caller is told so.
    assert copies(found, "/workspaces/gone", proc, group="/pod")[0][0] is None
    # Bubblewrap without a cgroup of the run's own: the agent's namespaces cannot be found, which is said too.
    assert copies(found, "/workspaces/my lib", proc, group=None)[0][0] is None


def test_remove_refuses_while_the_agent_holds_a_copy(run):
    record, tmp = run
    runner = lambda op, found, name, *rest, **kw: (0, "")  # noqa: E731
    w.add(record, make(tmp / "fresh"), out=lambda line: None, runner=runner, finder=fake_found,
          remember=lambda trees: None)
    with pytest.raises(ValueError, match="only a\\s+restart"):
        w.remove(record, "fresh", runner=runner, finder=fake_found, using=lambda f, p: [],
                 taken=lambda f, p, group=None: [(103, "/tmp/mine")])
    lines = []
    w.remove(record, "fresh", force=True, out=lines.append, runner=runner, finder=fake_found,
             using=lambda f, p: [], taken=lambda f, p, group=None: [(103, "/tmp/mine")])
    assert any("namespace of its own (process 103)" in line for line in lines)
