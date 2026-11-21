"""Root sees every user's starts (sandbox-ai list --all-users; stop/logs/report/list -H with USER:NAME or --user):
each user's state read as data only, Podman asked as that user, every action run as that user with root's own copy.

Nothing here touches the machine's users, HOMEs or Podman: the users come from a fake passwd file
(SANDBOX_AI_PASSWD), their HOMEs and runtime directories are temporary (SANDBOX_AI_USER_RUNTIME), and runuser and
podman are fakes on PATH that record what they were asked. These tests run as root (the sandbox's), so the fake
users have the uid that owns the temporary files; a foreign owner is faked where it matters (owner_ok)."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sandbox_allusers_test", ROOT / "sandbox/allusers.py")
au = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(au)
ME = os.geteuid()
SECRET = "SECRET-shadow-line-4711"


@pytest.fixture(params=["happy", "sad", "edge"])
def scenario(request):
    return request.param


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data))
    return path


class Box:
    """A fake host: a passwd file, users' HOMEs, /run/user, fake runuser and podman on PATH, root's own state."""

    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.homes = tmp_path / "home"
        self.runtime = tmp_path / "run-user"
        self.bin = tmp_path / "bin"
        self.passwd = tmp_path / "passwd"
        self.users = []
        self.bin.mkdir()
        self.runtime.mkdir()
        # runuser: records how it was called, then runs what follows "--" (as the caller: the test's own uid).
        write(self.bin / "runuser", '#!/bin/sh\nprintf "%s\\n" "$*" >> "$(dirname "$0")/runuser.log"\n'
                                    'while [ "$1" != "--" ]; do shift; done\nshift\nexec "$@"\n')
        # podman: records its arguments (and HOME, XDG_RUNTIME_DIR), answers pod ps from pods-USER.json.
        write(self.bin / "podman", '#!/bin/sh\nd=$(dirname "$0")\n'
                                   'printf "%s|%s|%s\\n" "$HOME" "$XDG_RUNTIME_DIR" "$*" >> "$d/podman.log"\n'
                                   'if [ "$1 $2" = "pod ps" ]; then\n'
                                   '  f="$d/pods-$(basename "$HOME").json"\n'
                                   '  if [ -f "$f" ]; then cat "$f"; else echo "[]"; fi\n'
                                   'fi\n')
        for name in ("runuser", "podman"):
            (self.bin / name).chmod(0o755)

    def user(self, name, logged_in=True):
        home = self.homes / name
        state = home / ".local/state/sandbox-ai"
        state.mkdir(parents=True)
        self.users.append(f"{name}:x:{ME}:{ME}:{name}:{home}:/bin/sh")
        self.passwd.write_text("\n".join(self.users) + "\n")
        if logged_in:
            (self.runtime / str(ME)).mkdir(exist_ok=True)
        return home

    def session(self, home, key, pid=None, ended=False, **fields):
        directory = home / ".local/state/sandbox-ai/sessions" / key
        data = {"agent": "claude", "options": ["--backend", "podman"], "backend": "podman", "workspace": f"/srv/{key}",
                "created": "2026-11-10T10:00:00Z", "started": "2026-11-15T10:00:00Z", "run": f"claude-{key}",
                "pod": f"agent-claude-{key}", **fields}
        if pid:
            data.update(pid=pid, pid_start=au.procinfo().start_time(pid))
        write(directory / "session.json", data)
        if ended:
            write(directory / "exit.json", {"run": data["run"], "status": 0, "ended": "2026-11-15T11:00:00Z"})
        return directory

    def pods(self, user, *names, status="Running"):
        write(self.bin / f"pods-{user}.json",
              [{"Name": name, "Status": status, "InfraId": "i", "Containers": [
                  {"Id": "i", "Status": "running"}, {"Id": "c", "Status": status.lower()}]} for name in names])

    def env(self, **more):
        return {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "SANDBOX_AI_PASSWD": str(self.passwd),
                "SANDBOX_AI_USER_RUNTIME": str(self.runtime), "XDG_STATE_HOME": str(self.tmp / "root-state"),
                "XDG_RUNTIME_DIR": str(self.tmp / "root-runtime"), "NO_COLOR": "1", **more}

    def run(self, *argv, **more):
        return subprocess.run([str(ROOT / "sandbox-ai"), *argv], capture_output=True, text=True, env=self.env(**more),
                              cwd=self.tmp, timeout=120)

    def log(self, name):
        path = self.bin / name
        return path.read_text() if path.exists() else ""


@pytest.fixture
def box(tmp_path, monkeypatch):
    found = Box(tmp_path)
    for key, value in found.env().items():
        monkeypatch.setenv(key, value)
    return found


def plant_own_copy(home):
    """A sandbox-ai of the user's own in their HOME (and a podman there): run as root, each would leave a mark."""
    mark = home / "EXECUTED"
    for path in (home / ".local/bin/sandbox-ai", home / ".local/bin/podman", home / ".local/bin/runuser",
                 home / ".local/share/sandbox-ai/sandbox-ai", home / ".local/share/sandbox-ai/sandbox/main.py"):
        write(path, f"#!/bin/sh\ntouch {mark}\n").chmod(0o755)
    return mark


# --- unit contracts ------------------------------------------------------------------------------------------------

def test_users_come_from_the_passwd_file(scenario, box):
    if scenario == "happy":
        box.user("alice")
        assert [(u["name"], u["uid"], u["home"]) for u in au.users()] == [("alice", ME, str(box.homes / "alice"))]
    elif scenario == "sad":  # damaged lines, odd names and numbers are left out, never passed to runuser
        box.passwd.write_text("garbage\nbad name:x:1:1::/h:/bin/sh\nx:x:notanumber:1::/h:/bin/sh\n"
                              "-rf:x:5:5::/h:/bin/sh\nok:x:7:7::/h/ok:/bin/sh\n:x:8:8::/h:/bin/sh\n")
        assert [u["name"] for u in au.users()] == ["ok"]
    else:  # no passwd file at all: no users
        box.passwd.unlink(missing_ok=True)
        assert au.users() == []


def test_root_only(scenario, monkeypatch):
    if scenario == "happy":
        monkeypatch.setattr(au.os, "geteuid", lambda: 0)
        monkeypatch.delenv(au.EUID_ENV, raising=False)
        au.require_root("list --all-users")
    elif scenario == "sad":
        monkeypatch.setattr(au.os, "geteuid", lambda: 1000)
        with pytest.raises(ValueError, match="only root.*list --all-users"):
            au.require_root("list --all-users")
    else:  # the tests' seam only ever takes root away, never gives it
        monkeypatch.setattr(au.os, "geteuid", lambda: 1000)
        monkeypatch.setenv(au.EUID_ENV, "0")
        with pytest.raises(ValueError, match="only root"):
            au.require_root("stop USER:NAME")
        monkeypatch.setattr(au.os, "geteuid", lambda: 0)
        monkeypatch.setenv(au.EUID_ENV, "1000")
        with pytest.raises(ValueError, match="only root"):
            au.require_root("stop USER:NAME")


def test_state_is_read_as_data_and_never_through_a_link(scenario, box, monkeypatch):
    home = box.user("alice")
    shadow = write(box.tmp / "etc-shadow", {"agent": "claude", "options": [], "name": SECRET})
    user = au.users()[0]
    if scenario == "happy":
        box.session(home, "kept1", ended=True)
        found = au.read_state(user)
        assert [key for key, *_ in found["sessions"]] == ["kept1"]
        assert found["sessions"][0][1]["workspace"] == "/srv/kept1"
    elif scenario == "sad":
        # session.json a link to a root file; a session directory a link; the state directory's parent a link
        directory = box.session(home, "linked")
        (directory / "session.json").unlink()
        (directory / "session.json").symlink_to(shadow)
        write(box.tmp / "rootdir/sessions/evil/session.json", {"agent": "claude", "options": [], "name": SECRET})
        (home / ".local/state/sandbox-ai/sessions/evil2").symlink_to(box.tmp / "rootdir/sessions/evil")
        found = au.read_state(user)
        assert SECRET not in json.dumps(found)
        state = home / ".local/state"
        os.rename(state, home / "state-real")
        state.symlink_to(box.tmp / "rootdir")
        assert au.read_state(user) is None  # a planted link instead of a directory: nothing is read
    else:
        # a hard link (or any file) of another owner, a FIFO (never waited on), a file beyond the size limit
        directory = box.session(home, "big")
        write(directory / "session.json", "{" + " " * (au.FILE_LIMIT + 10) + "}")
        fifo = box.session(home, "fifo")
        (fifo / "session.json").unlink()
        os.mkfifo(fifo / "session.json")
        other = box.session(home, "foreign")
        real_owner_ok = au.owner_ok
        monkeypatch.setattr(au, "owner_ok", lambda info, uid: real_owner_ok(info, uid) and
                            not (au.stat.S_ISREG(info.st_mode) and info.st_ino == (other / "session.json")
                                 .stat().st_ino))
        found = au.read_state(user)
        assert {key for key, data, *_ in found["sessions"] if data} == set()


def test_entries_of_a_user(scenario, box):
    home = box.user("alice", logged_in=scenario != "edge")
    box.session(home, "kept1", ended=True)
    box.session(home, "live1")
    box.session(home, "mine", pid=os.getpid(), backend="bwrap")  # its supervisor runs (this process)
    write(home / ".local/state/sandbox-ai/mcp/fs/mcp.json", {"backend": "podman", "started": "2026-11-15T09:00:00Z"})
    if scenario == "happy":
        box.pods("alice", "agent-claude-live1", "agent-mcp-fs")
        runs = box.runtime / str(ME) / "sandbox-ai/runs"
        write(runs / "codex-0a1b2c3d/run.json", {"run": "codex-0a1b2c3d", "name": "task1", "agent": "codex",
                                                 "backend": "bwrap", "pid": os.getpid(),
                                                 "pid_start": au.procinfo().start_time(os.getpid())})
        write(runs / "codex-dead/run.json", {"run": "codex-dead", "backend": "bwrap", "pid": 999999999})
    elif scenario == "sad":
        write(box.bin / "pods-alice.json", "not json")
    found = {e["name"]: e for e in au.user_entries(au.users()[0])}
    assert found["kept1"]["state"] == "stopped" and found["mine"]["state"] == "running"
    assert all(e["user"] == "alice" for e in found.values())
    if scenario == "happy":
        assert found["live1"]["state"] == "orphaned" and found["mcp-fs"]["state"] == "running"
        assert (found["task1"]["kind"], found["task1"]["state"]) == ("run", "running")
        assert found["codex-dead"]["state"] == "ended"
        assert "-u alice --" in box.log("runuser.log")
        assert f"{home}|{box.runtime / str(ME)}|pod ps --format json" in box.log("podman.log")
    elif scenario == "sad":
        assert found["live1"]["state"] == "unknown (Podman did not answer)"
    else:  # not logged in: no runtime directory, Podman not asked, what is not known says so
        assert found["live1"]["state"] == "unknown (not logged in)"
        assert box.log("podman.log") == ""


def test_drop_command(scenario):
    user = {"name": "alice", "uid": 1001, "gid": 1001, "home": "/home/alice"}
    which = {"happy": {"runuser": "/usr/sbin/runuser"}, "sad": {}, "edge": {"setpriv": "/usr/bin/setpriv",
                                                                          "su": "/usr/bin/su"}}[scenario]
    look = lambda name: which.get(name)  # noqa: E731
    if scenario == "happy":
        assert au.drop_command(user, ["/usr/bin/podman", "ps"], which=look, platform="linux") == \
            ["/usr/sbin/runuser", "-u", "alice", "--", "/usr/bin/podman", "ps"]
        assert au.drop_command(user, ["x"], which=lambda n: "/usr/bin/sudo" if n == "sudo" else None,
                               platform="darwin") == ["/usr/bin/sudo", "-n", "-H", "-u", "alice", "--", "x"]
    elif scenario == "sad":
        with pytest.raises(ValueError, match="runuser, setpriv or su"):
            au.drop_command(user, ["x"], which=look, platform="linux")
    else:
        assert au.drop_command(user, ["x", "a b"], which=look, platform="linux") == \
            ["/usr/bin/setpriv", "--reuid=1001", "--regid=1001", "--init-groups", "--", "x", "a b"]
        assert au.drop_command(user, ["x", "a b"], which=lambda n: "/usr/bin/su" if n == "su" else None,
                               platform="linux") == ["/usr/bin/su", "-s", "/bin/sh", "-c", "exec x 'a b'", "alice"]


def test_own_copy_must_be_roots_and_reachable(scenario, tmp_path):
    copy = tmp_path / "opt/sandbox-ai"
    write(copy / "sandbox/main.py", "print()")
    write(copy / "sandbox/other.py", "print()")
    for path in (tmp_path, tmp_path / "opt", copy, copy / "sandbox"):
        path.chmod(0o755)
    if scenario == "happy":
        assert au.copy_problem(copy / "sandbox", owner=ME, top=tmp_path) is None
    elif scenario == "sad":  # a file others may change: what root hands a user to run must be root's alone
        (copy / "sandbox/other.py").chmod(0o666)
        assert "writable" in au.copy_problem(copy / "sandbox", owner=ME, top=tmp_path)
        (copy / "sandbox/other.py").chmod(0o644)
        assert "not owned by" in au.copy_problem(copy / "sandbox", owner=ME + 1, top=tmp_path)
        cached = write(copy / "sandbox/__pycache__/other.cpython-311.pyc", "")
        (copy / "sandbox/__pycache__").chmod(0o755)
        cached.chmod(0o646)  # what Python loads instead of other.py
        assert "writable" in au.copy_problem(copy / "sandbox", owner=ME, top=tmp_path)
    else:  # a copy in root's HOME (0700): the user could not run it
        (tmp_path / "opt").chmod(0o700)
        assert "cannot reach" in au.copy_problem(copy / "sandbox", owner=ME, top=tmp_path)
        (tmp_path / "opt").chmod(0o755)


def test_target_of_a_command(scenario, box, monkeypatch):
    monkeypatch.chdir(box.tmp)
    box.user("alice")
    box.user("bob")
    if scenario == "happy":
        assert au.target("stop", ["alice:brisk", "--wait", "3"]) == ("alice", ["brisk", "--wait", "3"])
        assert au.target("report", ["--user", "bob", "x", "--json"]) == ("bob", ["x", "--json"])
        assert au.target("list", ["-H", "alice:brisk"]) == ("alice", ["-H", "brisk"])
        assert au.target("list", ["--history=alice:brisk"]) == ("alice", ["--history=brisk"])
        assert au.target("logs", ["--user=alice", "brisk"]) == ("alice", ["brisk"])
    elif scenario == "sad":
        with pytest.raises(ValueError, match="alice.*bob"):
            au.target("stop", ["--user", "alice", "bob:brisk"])
        with pytest.raises(ValueError, match="--user USER"):
            au.target("stop", ["--user"])
    else:
        assert au.target("stop", ["brisk"]) == (None, ["brisk"])  # nothing of another user
        assert au.target("stop", ["nobody-here:brisk"]) == (None, ["nobody-here:brisk"])  # no such user: a name
        write(box.tmp / "alice:dir/x", "")
        assert au.target("logs", ["alice:dir"]) == (None, ["alice:dir"])  # a folder of that name is a folder
        assert au.target("list", ["--user", "alice"]) == (None, ["--user", "alice"])  # list's own filter


# --- through ./sandbox-ai ------------------------------------------------------------------------------------------

def test_list_all_users_e2e(box):
    alice, bob = box.user("alice"), box.user("bob")
    marks = [plant_own_copy(alice), plant_own_copy(bob)]
    box.session(alice, "a-live")
    box.session(alice, "a-kept", ended=True)
    box.session(bob, "b-live")
    write(bob / ".local/state/sandbox-ai/mcp/fs/mcp.json", {"backend": "podman"})
    box.pods("alice", "agent-claude-a-live")
    box.pods("bob", "agent-claude-b-live", "agent-mcp-fs")
    done = box.run("list", "--all-users")
    assert done.returncode == 0, done.stderr
    lines = done.stdout.splitlines()
    assert lines[0].split()[:3] == ["USER", "NAME", "KIND"]
    assert any(line.split()[:2] == ["alice", "a-live"] and "orphaned" in line for line in lines)
    assert any(line.split()[:2] == ["bob", "b-live"] for line in lines)
    assert "a-kept" not in done.stdout and "mcp-fs" not in done.stdout  # running by default, as plain list
    assert "1 stopped start (--stopped)" in done.stdout and "1 MCP server (--mcp)" in done.stdout
    assert "a-kept" in box.run("list", "--all-users", "--stopped").stdout
    assert "mcp-fs" in box.run("list", "--all-users", "--mcp").stdout
    everything = json.loads(box.run("list", "--all-users", "-a", "--json").stdout)
    assert {(e["user"], e["name"]) for e in everything} == {("alice", "a-live"), ("alice", "a-kept"),
                                                             ("bob", "b-live"), ("bob", "mcp-fs")}
    only = json.loads(box.run("list", "--user", "bob", "--json", "-a").stdout)
    assert {e["name"] for e in only} == {"b-live", "mcp-fs"}
    assert not any(mark.exists() for mark in marks)  # nothing of the users' own was executed
    assert all(" -- " in line and "/.local/" not in line.split(" -- ")[1].split()[0]
               for line in box.log("runuser.log").splitlines())


def test_list_all_users_without_a_runtime_dir_says_unknown_e2e(box):
    alice = box.user("alice", logged_in=False)
    box.session(alice, "a-live")
    box.session(alice, "a-kept", ended=True)
    done = box.run("list", "--all-users")
    assert done.returncode == 0, done.stderr
    assert "unknown (not logged in)" in done.stdout and "a-kept" not in done.stdout
    assert box.log("podman.log") == "" and box.log("runuser.log") == ""


def test_non_root_is_refused_e2e(box):
    box.user("alice")
    for argv in (["list", "--all-users"], ["list", "--user", "alice"], ["stop", "alice:x"], ["logs", "--user", "alice",
                                                                                           "x"], ["report", "alice:x"]):
        done = box.run(*argv, SANDBOX_AI_TEST_EUID="1000")
        assert done.returncode != 0 and "only root" in done.stderr, (argv, done.stderr)
    assert box.log("runuser.log") == "" and box.log("podman.log") == ""


def test_symlink_attack_is_not_read_e2e(box):
    """A user links their state file to a root-only file (as /etc/shadow): root never reads it."""
    alice = box.user("alice")
    shadow = write(box.tmp / "shadow", {"agent": "claude", "options": [], "name": SECRET, "workspace": SECRET})
    shadow.chmod(0o600)
    directory = box.session(alice, "linked")
    (directory / "session.json").unlink()
    (directory / "session.json").symlink_to(shadow)
    (directory / "exit.json").symlink_to(shadow)
    write(box.tmp / "rootdir/sessions/evil/session.json", {"agent": "claude", "options": [], "name": SECRET})
    (alice / ".local/state/sandbox-ai/sessions/evil").symlink_to(box.tmp / "rootdir/sessions/evil")
    (alice / ".local/state/sandbox-ai/mcp").symlink_to(box.tmp / "rootdir")
    done = box.run("list", "--all-users", "-a", "--json")
    assert done.returncode == 0, done.stderr
    assert SECRET not in done.stdout + done.stderr


def test_actions_run_as_the_user_with_roots_own_copy_e2e(box):
    alice = box.user("alice")
    mark = plant_own_copy(alice)
    box.session(alice, "a-kept", ended=True)
    done = box.run("stop", "alice:a-kept")
    calls = box.log("runuser.log")
    assert calls.startswith("-u alice -- ")
    assert f"{ROOT / 'sandbox/main.py'} stop a-kept" in calls  # root's own copy, never the user's
    assert "env -i" in calls or "/env -i " in calls
    assert f"HOME={alice}" in calls and f"XDG_RUNTIME_DIR={box.runtime / str(ME)}" in calls
    assert "a-kept" in done.stderr and "not running" in done.stderr.lower() or done.returncode == 0, done.stderr
    history = box.run("list", "-H", "alice:a-kept")
    assert history.returncode == 0, history.stderr
    report = box.run("report", "--user", "alice", "a-kept", "--json")
    assert report.returncode == 0 and isinstance(json.loads(report.stdout), dict), report.stderr
    assert "report a-kept --json" in box.log("runuser.log")
    assert not mark.exists()


def test_unknown_user_is_said_e2e(box):
    box.user("alice")
    done = box.run("stop", "--user", "carol", "x")
    assert done.returncode != 0 and "carol" in done.stderr and "no such user" in done.stderr
    assert box.log("runuser.log") == ""


def test_completion_offers_other_users_starts_to_root_only(scenario, box, monkeypatch):
    """Tab after `stop`/`report` (as root): USER:NAME of the other users' starts too, from their records alone
    (Podman is not asked on a Tab); for anybody else nothing of other users."""
    spec = importlib.util.spec_from_file_location("sandbox_completion_allusers", ROOT / "sandbox/completion.py")
    completion = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(completion)
    alice = box.user("alice")
    box.session(alice, "a-live")
    if scenario == "sad":
        monkeypatch.setenv(au.EUID_ENV, "1000")
    if scenario == "edge":
        box.passwd.write_text("")
    found = completion.other_users_starts()
    assert found == (["alice:a-live"] if scenario == "happy" else [])
    assert box.log("podman.log") == "" and box.log("runuser.log") == ""
    if scenario == "happy":
        assert "alice:a-live" in completion.named("stop", None, "name")
        assert "alice:a-live" in completion.named("report", None, "name")


class FakeTTY:
    """A terminal root's output goes to: bytes written, isatty True."""

    def __init__(self, tty=True):
        self.data, self.tty = b"", tty
        self.buffer = self

    def write(self, data):
        self.data += data

    def flush(self):
        pass

    def isatty(self):
        return self.tty


def test_actions_get_no_terminal_of_roots(scenario, box, monkeypatch):
    """What runs as the user never gets root's terminal: a session of its own (no controlling terminal, so no
    TIOCSTI into root's shell), no stdin, its output relayed - control characters escaped on a terminal - and it
    changes into the user's HOME itself, after the drop (root never hands it a directory)."""
    alice = box.user("alice")
    seen = {}
    real_popen = subprocess.Popen

    def popen(argv, **kwargs):
        seen.update(argv=argv, **kwargs)
        return real_popen(["/bin/sh", "-c", 'printf "ok \\033]52;c;evil\\007 \\033[31mred\\n"; echo err >&2; '
                                          'exit 3'], **kwargs)
    monkeypatch.setattr(au.subprocess, "Popen", popen)
    out, err = FakeTTY(scenario != "edge"), FakeTTY()
    status = au.run_as("alice", "stop", ["x"], stdout=out, stderr=err)
    assert status == 3
    assert seen["start_new_session"] is True and seen["stdin"] == subprocess.DEVNULL and seen["cwd"] == "/"
    assert seen["argv"][seen["argv"].index("-i") - 1].endswith("env")
    shell = seen["argv"].index("/bin/sh")
    assert seen["argv"][shell + 1:shell + 3] == ["-c", 'cd "$HOME" 2>/dev/null || cd /; exec "$@"']
    assert "NO_COLOR=1" in seen["argv"]
    assert err.data == b"err\n"
    if scenario == "edge":  # into a file or a pipe: as it came
        assert out.data == b"ok \x1b]52;c;evil\x07 \x1b[31mred\n"
    else:
        assert b"\x1b" not in out.data and b"\x07" not in out.data and out.data.startswith(b"ok \\x1b]52")
    assert str(alice) in " ".join(seen["argv"])


def test_podman_is_asked_without_roots_terminal(box):
    box.user("alice")
    seen = {}

    def runner(argv, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, b"[]", b"")
    assert au.podman_pods(au.users()[0], None, runner=runner) == {}
    assert seen["start_new_session"] is True and seen["stdin"] == subprocess.DEVNULL and seen["cwd"] == "/"
