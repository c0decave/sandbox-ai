"""The in-depth audit of the command line and the core before the 2026-11-21 release (audit/cli-core): each function
driven as a user does - the real ./sandbox-ai with live_fakes.py's host (Podman, tmux, npm faked) - and what it SAYS
read: every message true, naming what exists, with a fix that works. One block per finding."""
import importlib.util
import json
import subprocess
import time

import pytest

from live_fakes import Host, ROOT

_SPEC = importlib.util.spec_from_file_location("runtime_audit_cli_core_test", ROOT / "sandbox" / "runtime.py")
RUNTIME = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(RUNTIME)

ENOSPC_RUNDIR = "Error: creating pod: mkdir /run/user/1000/libpod/tmp/pod-x: no space left on device"


@pytest.fixture
def host(tmp_path):
    return Host(tmp_path)


# --- Podman's "no space left on device" under /run/user was told "log in by ssh" ------------------------------------

def test_a_full_runtime_directory_is_named_full_not_missing(host):
    """Podman's first command of a start (the host lookup in the image) failed with ENOSPC in its run directory
    (/run/user/UID, a tmpfs): the remedy said "Podman has no runtime directory: log in by ssh or on the desktop" -
    the directory was there, it was full. Now it names a full file system and where to look."""
    result = host.run("claude", "--name", "full1", env={"FAKE_PODMAN_LOOKUP_FAIL": "125:" + ENOSPC_RUNDIR})
    assert result.returncode != 0
    text = " ".join(result.stderr.split())
    assert "no space left on device" in text, text
    assert "no runtime directory" not in text and "log in by ssh" not in text, text
    assert "is full" in text and "df -h" in text, text


def test_a_pod_podman_cannot_create_is_said_in_words_not_as_a_python_exception(host):
    """`podman pod create` failed (exit status 125, a full run directory): the start ended with Python's
    "Command '['podman', 'pod', 'create', ...]' returned non-zero exit status 125." Now: Podman's own reason, that the
    pod was not created, and what helps."""
    result = host.run("claude", "--name", "full2",
                      env={"FAKE_PODMAN_RUN": "0:0", "FAKE_PODMAN_POD_CREATE_FAIL": "125:" + ENOSPC_RUNDIR})
    assert result.returncode != 0
    assert "Command '[" not in result.stderr and "returned non-zero exit status" not in result.stderr, result.stderr
    text = " ".join(result.stderr.split())
    assert "Podman did not create the pod agent-claude-" in text and "(exit status 125; its reason is above)" in text
    assert result.stderr.count("no space left on device") == 1, result.stderr  # (Podman's line, said once)
    assert "no space left on device" in text and "is full" in text and "log in by ssh" not in text, text


def test_a_pod_create_failure_with_another_reason_keeps_its_hint(host):
    """The same for another reason: its own hint (the image), and still no Python exception text."""
    result = host.run("claude", "--name", "full3",
                      env={"FAKE_PODMAN_RUN": "0:0",
                           "FAKE_PODMAN_POD_CREATE_FAIL": "125:Error: localhost/claude-sandbox:latest: image not known"})
    assert result.returncode != 0 and "Command '[" not in result.stderr
    assert "sandbox-ai build claude" in result.stderr.split("Podman did not create the pod", 1)[1], result.stderr


# --- resume NAME --mount SRC:DEST (documented [:ro|rw]) failed in its supervisor: "Mount syntax: SRC:DEST:ro|rw" -----

def test_resume_with_a_mount_without_its_mode_starts_it_read_only(host):
    """`sandbox-ai resume NAME --mount SOURCE:DESTINATION[:ro|rw]` (README, resume --help): given without :ro or :rw
    the start failed in its supervisor ("Mount syntax: SRC:DEST:ro|rw", then "Session failed to start (status 1)").
    Without a mode the mount is read-only (as the app's Board makes one), kept for its later starts."""
    made = host.run("claude", "--name", "kept1", env={"FAKE_PODMAN_RUN": "0:0"})
    assert "kept1 keeps its HOME" in made.stderr, made.stderr
    data = host.directory / "data"
    data.mkdir()
    resumed = host.run("resume", "kept1", "--mount", f"{data}:/mnt/data", env={"FAKE_PODMAN_RUN": "0:0"})
    assert "Mount syntax" not in resumed.stderr, resumed.stderr
    runs = [call for call in host.calls() if call[:2] == ["run", "--rm"] and "--pod" in call]
    assert len(runs) == 2, "the resumed start got to its sandbox: " + resumed.stderr[-2000:]
    assert f"type=bind,src={data},dst=/mnt/data,ro,nodev,nosuid" in runs[-1]


# --- a start whose own build of its image failed told to run that build, as if it had not been tried ----------------

def test_a_start_whose_build_failed_says_so_with_its_fix(host):
    """The start built the missing image itself and the build failed: the last line said "The image ... is not built
    on this host ...: sandbox-ai build codex" - the very build that had just failed, as if it had not been tried. Now
    it says that the build failed just now and that its cause comes first; a start whose image is not built by it
    (SANDBOX_AI_NO_AUTO_BUILD=1) is told the fix alone, as before."""
    failed = host.run("codex", env={"FAKE_PODMAN_BUILD_FAIL": "1"})
    assert failed.returncode != 0 and len(host.builds()) == 1
    # (the fix ends its line: the app's Board offers that build from it - BUILD_OFFER)
    assert "(nothing is pulled at a start): sandbox-ai build codex\n  (its build failed just now - what it said is " \
           "above: fix that first)" in failed.stderr, failed.stderr
    untried = host.run("codex", env={"SANDBOX_AI_NO_AUTO_BUILD": "1"})
    assert untried.returncode != 0 and len(host.builds()) == 1
    assert "(nothing is pulled at a start): sandbox-ai build codex" in untried.stderr
    assert "its build failed" not in untried.stderr


# --- launcher.log named a command that does not exist ---------------------------------------------------------------

def test_a_session_start_logs_where_its_output_is_not_a_command_that_does_not_exist(host):
    """launcher.log said of every session start "its output: sandbox-ai sessions log, supervisor.log" - there is no
    `sandbox-ai sessions` command (sandbox-ai: unknown command 'sessions'). It names the session's supervisor.log."""
    host.run("claude", "--session", "logged", env={"FAKE_PODMAN_RUN": "0:0"})
    written = (host.home / ".local/state/sandbox-ai/launcher.log").read_text()
    assert "started as a session" in written
    assert "sandbox-ai sessions log" not in written
    assert str(host.home / ".local/state/sandbox-ai/sessions/logged/supervisor.log") in written
    assert "unknown command 'sessions'" in host.run("sessions", "log").stderr  # (still no such command)


# --- `mcp start NAME` said "started" for a server whose image is not on this host ------------------------------------

def test_mcp_start_of_a_server_whose_image_is_not_there_is_refused_with_the_fix(host):
    """`sandbox-ai mcp add hub:radare2 --no-pull` (its image not pulled), then `sandbox-ai mcp start radare2`:
    "MCP server radare2 started (container ghcr.io/...)" - its bridge ran, but every connection's container would
    fail (--pull=never). An agent start with --mcp radare2 refuses it with the fix; `mcp start` checks the same now
    and starts nothing."""
    folder = host.directory / "samples"
    folder.mkdir()
    added = host.run("mcp", "add", "hub:radare2", "--no-pull", "--mount", f"/work={folder}:ro")
    assert added.returncode == 0, added.stderr
    started = host.run("mcp", "start", "radare2")
    assert started.returncode != 0, started.stderr
    assert "started" not in started.stderr.replace("nothing was started", ""), started.stderr
    text = " ".join(started.stderr.split())
    assert "its image ghcr.io/c0decave/axross-mcp-radare2@sha256:" in text and "is not there" in text, text
    assert "podman pull ghcr.io/c0decave/axross-mcp-radare2@sha256:" in text, text
    assert host.leftovers() == {}
    listed = host.run("mcp", "list")
    assert "radare2  stopped" in listed.stdout, listed.stdout
    agent = host.run("claude", "--mcp", "radare2")  # the agent start's own check: the same words
    assert "its image ghcr.io/c0decave/axross-mcp-radare2@sha256:" in " ".join(agent.stderr.split())


# --- `team list` without a team printed nothing at all ---------------------------------------------------------------

def test_team_list_without_a_team_says_so_and_how_to_start_one(host):
    """`sandbox-ai team list` with no team: no output, exit status 0 - every other list says it is empty. Said on
    standard error (the app and the web read its standard output, which stays empty)."""
    listed = host.run("team", "list")
    assert listed.returncode == 0 and listed.stdout == ""
    assert "no teams; start one with: sandbox-ai team start NAME --agents" in listed.stderr, listed.stderr


# --- `stop NAME` while NAME still starts: "try again in a moment", for as long as the start may take ----------------

def test_stop_while_the_session_still_starts_says_what_holds_it(host):
    """A session's start holds it while it waits for its terminal - as long as it says new steps, up to 20 minutes (a
    Tor bootstrap, an image). `sandbox-ai stop NAME` (the app's Stop) meanwhile said "is being started or stopped by
    another command; try again in a moment". Now it says that it is still starting, how long that may take, and how
    to end it now."""
    hold = host.directory / "release-pod"
    first = host.popen("claude", "--session", "st1", env={"FAKE_PODMAN_POD_HOLD": str(hold)})
    try:
        deadline = time.monotonic() + 60
        while host.stopped_at() is None and time.monotonic() < deadline:
            time.sleep(0.1)
        assert host.stopped_at() is not None, "the start never got to its pod"
        stopped = host.run("stop", "st1")
        assert stopped.returncode != 0
        text = " ".join(stopped.stderr.split())
        assert "try again in a moment" not in text, text
        assert "Session st1 is still starting" in text and "up to 20 minutes" in text, text
        assert "Ctrl-C" in text and "sandbox-ai stop st1" in text, text
    finally:
        hold.touch()
        try:
            first.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            first.kill()
            first.communicate()


# --- a start under a kept session's name with other options: "Use a new session name" - resume NAME OPTIONS too -----

def test_a_start_with_other_options_under_a_kept_name_names_resume_with_options(host):
    """`sandbox-ai claude --session s1 --cpus 2` for a stopped s1 started it with its saved options and said "ignored
    now: --cpus 2. Use a new session name for other options." - `sandbox-ai resume s1 --cpus 2` changes them (a
    rebirth, README); the warning says so, and that command does what it says."""
    assert "s1 keeps its HOME" in host.run("claude", "--name", "s1", env={"FAKE_PODMAN_RUN": "0:0"}).stderr
    again = host.run("claude", "--session", "s1", "--cpus", "2", env={"FAKE_PODMAN_RUN": "0:0"})
    assert "ignored now: --cpus 2" in again.stderr, again.stderr
    assert "or change its own: sandbox-ai resume s1 OPTIONS" in " ".join(again.stderr.split()), again.stderr
    changed = host.run("resume", "s1", "--cpus", "2", env={"FAKE_PODMAN_RUN": "0:0"})
    assert "ignored" not in changed.stderr
    assert "--cpus 2" in changed.stderr and "kept for its later starts" in changed.stderr, changed.stderr


# --- `secrets rename` of a plain secret: "no primary password is set" -----------------------------------------------

def test_secrets_rename_keeps_a_plain_secret_plain(host):
    """`sandbox-ai secrets set FOO --plain`, then `sandbox-ai secrets rename FOO BAR`: refused with "no primary
    password is set: these private files would hold the secret unencrypted ... sandbox-ai secrets set NAME --plain" -
    the look whether BAR is taken forgot that FOO was plain. Renamed as it was kept (plain), its value the same.
    (Without the tests' SANDBOX_AI_SECRETS_PLAIN=1, as a user has it: the secret's own --plain is the choice.)"""
    host.env.pop("SANDBOX_AI_SECRETS_PLAIN", None)
    kept = subprocess.run(host.command("secrets", "set", "FOO", "--plain"), input="sk-audit-1234", text=True,
                          capture_output=True, env=host.env, cwd=host.workspace, timeout=60)
    assert kept.returncode == 0, kept.stderr
    renamed = host.run("secrets", "rename", "FOO", "BAR")
    assert renamed.returncode == 0, renamed.stderr
    assert "FOO is now BAR (file)" in renamed.stdout + renamed.stderr
    listed = host.run("secrets", "list").stdout
    assert "BAR" in listed and "FOO" not in listed, listed
    found = [path for path in host.home.rglob("BAR") if path.is_file()]
    assert len(found) == 1 and found[0].read_text().strip() == "sk-audit-1234", found


# --- a name freed by `rename` was still refused, naming a start `list` no longer shows --------------------------------

def test_a_renamed_starts_old_name_is_refused_with_its_new_name(host):
    """`rename s1 other`, then `claude --name s1`: "The name s1 belongs to a kept start (stopped); continue it with:
    sandbox-ai resume s1, or remove it: sandbox-ai delete s1" - `list` shows no s1 (s1 stays the start's id, its
    folder). The refusal names the start as `list` shows it."""
    assert "s1 keeps its HOME" in host.run("claude", "--name", "s1", env={"FAKE_PODMAN_RUN": "0:0"}).stderr
    assert host.run("rename", "s1", "other").returncode == 0
    assert "other" in host.run("list", "--all").stdout
    again = host.run("claude", "--name", "s1", env={"FAKE_PODMAN_RUN": "0:0"})
    assert again.returncode != 0
    text = " ".join(again.stderr.split())
    assert "The name s1 belongs to the kept start other (stopped; s1 is its id" in text, text
    assert "sandbox-ai resume other" in text and "sandbox-ai delete other" in text, text


# --- `session copy NAME HOST` to a host SSH cannot reach: "NAME cannot start on HOST as it is" ----------------------

def test_session_copy_to_an_unreachable_host_says_it_was_not_reached(host):
    """`sandbox-ai session copy s1 ct1` with ct1 unreachable (ssh: exit status 255, "No route to host"): "s1 cannot
    start on ct1 as it is: ... (folders of this machine that lie elsewhere there: --map LOCAL=REMOTE)" - nothing was
    checked there, the session is not at fault and --map does not help. Now: ct1 was not reached, nothing was
    copied, and how to check the connection."""
    assert "s1 keeps its HOME" in host.run("claude", "--name", "s1", env={"FAKE_PODMAN_RUN": "0:0"}).stderr
    assert host.run("hosts", "add", "ct1", "root@192.0.2.9").returncode == 0
    copied = host.run("session", "copy", "s1", "ct1", timeout=200)
    assert copied.returncode != 0
    text = " ".join((copied.stdout + copied.stderr).split())
    assert "cannot start on ct1 as it is" not in text and "--map LOCAL=REMOTE" not in text, text
    assert "ct1 was not reached over SSH (exit status 255): nothing was copied" in text, text
    assert "No route to host" in text and "sandbox-ai hosts test ct1" in text, text


# --- a start on a full XDG_RUNTIME_DIR (a small tmpfs): "[Errno 28] No space left on device: PATH" -----------------

FULLSHIM = '''"""python3 whose writes below FAKE_FULL fail with ENOSPC, as on a full file system (os.geteuid as UIDSHIM)."""
import builtins, errno, functools, io, os, runpy, sys
prefix = os.environ["FAKE_FULL"].rstrip("/")
def under(path):
    try:
        return os.path.abspath(os.fsdecode(path)) == prefix or os.path.abspath(os.fsdecode(path)).startswith(prefix + "/")
    except TypeError:
        return False
def full(path):
    raise OSError(errno.ENOSPC, "No space left on device", os.fsdecode(path))
_open, _osopen, _mkdir = builtins.open, os.open, os.mkdir
def open_(file, mode="r", *args, **kwargs):  # (a new file, or writing one: what needs room; r+ of one there does not)
    if isinstance(file, (str, bytes, os.PathLike)) and any(c in mode for c in "wax") and under(file):
        full(file)
    return _open(file, mode, *args, **kwargs)
def osopen(path, flags, *args, **kwargs):
    if kwargs.get("dir_fd") is None and under(path) and (
            flags & os.O_CREAT and not os.path.exists(path) or flags & (os.O_TRUNC | os.O_APPEND)):
        full(path)
    return _osopen(path, flags, *args, **kwargs)
def mkdir(path, *args, **kwargs):
    if under(path) and not os.path.isdir(path):
        full(path)
    return _mkdir(path, *args, **kwargs)
# functools.partial, not the functions: Python 3.9's pathlib takes os.open and os.mkdir as attributes of its
# _NormalAccessor class, where a plain function is bound as a method (TypeError: open() takes at most 3
# positional arguments (4 given)); a partial is never bound.
builtins.open, io.open = open_, open_
os.open, os.mkdir = functools.partial(osopen), functools.partial(mkdir)
if os.environ.get("FAKE_FULL_EUID"):
    os.geteuid = lambda: 1000
sys.executable = os.environ["FAKE_PYTHON"]
args = sys.argv[1:]
while args and args[0].startswith("-") and args[0] not in ("-c", "-m"):
    args = args[1:]
if args[:1] == ["-c"]:
    sys.argv = ["-c", *args[2:]]
    exec(compile(args[1], "<string>", "exec"), {"__name__": "__main__"})
elif args[:1] == ["-m"]:
    sys.argv = [args[1], *args[2:]]
    runpy.run_module(args[1], run_name="__main__", alter_sys=True)
else:
    sys.argv = args
    runpy.run_path(args[0], run_name="__main__")
'''


def full_below(host, folder):
    """HOST's python3 (the launcher's) with every write below FOLDER failing with ENOSPC."""
    import sys
    (host.bin / "fullshim.py").write_text(FULLSHIM)
    (host.bin / "python3").write_text(f'#!/bin/sh\nexec {sys.executable} -I "{host.bin}/fullshim.py" "$@"\n')
    (host.bin / "python3").chmod(0o755)
    host.env.update(FAKE_FULL=str(folder), FAKE_PYTHON=str(host.bin / "python3"))
    if host.shimmed:
        host.env["FAKE_FULL_EUID"] = "1"


def test_a_start_on_a_full_runtime_directory_names_the_file_system(host):
    """XDG_RUNTIME_DIR (/run/user/UID: a tmpfs of a few hundred MiB) full: the start ended with "[Errno 28] No space
    left on device: '/run/user/1000/sandbox-ai/runs/claude-...'" - nothing said that a file system was full, which
    one, or what to do. Now it names the file system with its free space and the fix."""
    host.add_image("localhost/claude-sandbox:latest")
    full_below(host, host.runtime)
    result = host.run("claude", "--name", "full4", env={"FAKE_PODMAN_RUN": "0:0"})
    assert result.returncode != 0
    text = " ".join(result.stderr.split())
    assert "No space left on device" in text and str(host.runtime / "sandbox-ai/runs/") in text, text
    assert "is full" in text and "free" in text and "sandbox-ai gc --check" in text, text
    assert "[Errno 28]" not in text, text


def test_rename_and_resume_on_a_full_state_folder_name_the_file_system(host):
    """The same for the commands on starts (rename, resume, stop ...): "[Errno 28] No space left on device:
    .../sessions/NAME/.session-XXXX" before; now which file system is full and what to do."""
    host.add_image("localhost/claude-sandbox:latest")
    assert "kept1 keeps its HOME" in host.run("claude", "--name", "kept1", env={"FAKE_PODMAN_RUN": "0:0"}).stderr
    full_below(host, host.home / ".local/state/sandbox-ai")
    for command in (("rename", "kept1", "kept2"), ("resume", "kept1")):
        result = host.run(*command, env={"FAKE_PODMAN_RUN": "0:0"})
        text = " ".join(result.stderr.split())
        assert result.returncode != 0 and "[Errno 28]" not in text, text
        assert "could not be written (No space left on device): a file system sandbox-ai writes is full" in text, text


@pytest.mark.parametrize("command", [("defaults", "set", "ask_timeout", "30"),
                                     ("notify", "add", "n", "--ntfy", "https://ntfy.sh/audit-topic"),
                                     ("identities", "add", "w", "--name", "A B", "--email", "a@example.org"),
                                     ("spend", "limit", "set", "--all", "--day", "5"),
                                     ("update", "mode", "off")])
def test_a_setting_written_on_a_full_file_system_is_said_not_a_traceback(host, command):
    """`defaults set`, `notify add`, `identities add`, `git-logins add`, `spend limit set` ... with the settings' file
    system full: a Python traceback ending in "OSError: [Errno 28] No space left on device" (each caught only its own
    ValueError). Now one line: what could not be written, which file system, the fix."""
    full_below(host, host.home)
    result = host.run(*command)
    assert result.returncode != 0
    assert "Traceback" not in result.stderr, result.stderr
    text = " ".join(result.stderr.split())
    assert f"sandbox-ai {command[0]}: " in text and "could not be written (No space left on device): a file system " \
           "sandbox-ai writes is full" in text, text


# --- rename of a damaged start: "delete it or repair it first" - and delete refuses it ------------------------------

def test_rename_of_a_damaged_start_gives_the_fix_delete_gives(host):
    """A start whose session.json is damaged: `rename NAME NEW` said "delete it or repair it first", while
    `sandbox-ai delete NAME --yes` refuses it ("remove that directory"). It names the file and the directory."""
    folder = host.home / ".local/state/sandbox-ai/sessions/broken"
    folder.mkdir(parents=True)
    (folder / "session.json").write_text('{"agent": "claude", "options": [')
    deleted = host.run("delete", "broken", "--yes")
    assert deleted.returncode != 0 and "remove that directory" in deleted.stderr, deleted.stderr
    renamed = host.run("rename", "broken", "fine")
    assert renamed.returncode != 0
    assert "delete it" not in renamed.stderr, renamed.stderr
    assert f"see {folder}/session.json); repair that file, or remove that directory" in renamed.stderr, renamed.stderr


# --- checkpoint restore's preview: "this changes: ... 3 deleted" for files the restore writes back ------------------

def test_checkpoint_restore_says_what_it_undoes_not_that_it_deletes(host):
    """The agent deleted everything in /work; `sandbox-ai checkpoint restore NAME` showed "Restore ID ... - this
    changes: /work (...): 3 deleted  D /work/a.txt ..." - and then wrote those 3 back ("3 written"): the preview is
    what changed since the checkpoint, which reads as what the restore would do. It says so now."""
    (host.workspace / "a.txt").write_text("important\n")
    (host.workspace / "sub").mkdir()
    (host.workspace / "sub" / "b.txt").write_text("x\n")
    host.run("claude", "--name", "cw1", env={"FAKE_PODMAN_RUN": "0:0", "FAKE_PODMAN_WIPE_WORK": "1"})
    assert not (host.workspace / "a.txt").exists()
    preview = host.run("checkpoint", "restore", "cw1", "--dry-run")
    assert preview.returncode == 0, preview.stderr
    text = " ".join(preview.stdout.split())
    assert "this changes:" not in text, text
    assert "what changed since then, which the restore undoes (changed and deleted: written again as they were, " \
           "added: removed): /work" in text and "3 deleted" in text, text
    restored = host.run("checkpoint", "restore", "cw1", "--yes")
    assert "3 written, 0 removed" in restored.stdout and (host.workspace / "a.txt").read_text() == "important\n"


# --- a command MCP server (demo, a team's board) runs in the codex image: missing, a Python exception ----------------

def test_a_start_with_the_demo_server_without_the_codex_image_is_refused_with_its_build(host):
    """A user who built only claude's image: `sandbox-ai claude --mcp demo` (and `team start`, whose board is such a
    server) ended with "MCP server sandbox failed to start (status 1): ... Command '['podman', 'image', 'exists',
    'localhost/codex-sandbox:latest']' returned non-zero exit status 1." - a command server runs in an agent's image
    (codex unless its definition names another). The start's check of its servers says it first, with the build."""
    host.add_image("localhost/claude-sandbox:latest")
    result = host.run("claude", "--mcp", "demo", env={"FAKE_PODMAN_RUN": "0:0"})
    assert result.returncode != 0
    text = " ".join(result.stderr.split())
    assert "returned non-zero exit status" not in text and "Command '[" not in text, text
    assert "MCP server demo: it runs in the image of codex (localhost/codex-sandbox:latest), which is not built " \
           "here: sandbox-ai build codex" in text, text
    started = host.run("mcp", "start", "demo")
    assert started.returncode != 0 and "sandbox-ai build codex" in started.stderr, started.stderr
    assert host.leftovers() == {}


def test_a_server_sandbox_whose_image_is_gone_says_so_in_words(host):
    """Past the check (an image removed between the check and the server's start): the server's own launcher said
    "Command '['podman', 'image', 'exists', ...]' returned non-zero exit status 1"; now what is missing and the
    build."""
    host.add_image("localhost/claude-sandbox:latest")
    result = host.run("mcp", "start", "demo", env={"SANDBOX_AI_MCP_NO_IMAGE_CHECK": "1", "FAKE_PODMAN_RUN": "0:0"})
    assert result.returncode != 0
    text = " ".join(result.stderr.split())
    assert "Command '[" not in text, text
    assert "the image localhost/codex-sandbox:latest is not on this host: sandbox-ai build codex" in text, text
    assert host.leftovers() == {}


def test_a_team_whose_first_member_fails_does_not_say_members_keep_running(host):
    """`team start` whose lead did not start: "the members that started keep running; stop them with: sandbox-ai
    team stop tm1" - none had started. Said as it is, with how to try again or remove the team."""
    host.add_image("localhost/claude-sandbox:latest", "localhost/codex-sandbox:latest")
    result = host.run("team", "start", "tm1", "--agents", "claude,codex", "--workspace", host.workspace,
                      env={"FAKE_PODMAN_RUN": "1:0"}, timeout=300)
    text = " ".join((result.stdout + result.stderr).split())
    assert result.returncode != 0
    assert "the members that started keep running" not in text, text
    assert "no member of tm1 started" in text and "sandbox-ai team delete tm1 --yes" in text, text
    host.run("team", "delete", "tm1", "--yes")


# --- `vpn add` without a primary password: "or keep plain files: sandbox-ai secrets set NAME --plain" ---------------

def test_a_command_that_keeps_a_secret_without_a_primary_password_names_a_fix_that_works_for_it(host):
    """`sandbox-ai vpn add wg1 --wireguard FILE` where secrets are private files without a primary password: refused
    with "Set one first: sandbox-ai secrets password set (then keep the secret again); or keep plain files, your
    choice: sandbox-ai secrets set NAME --plain" - vpn add keeps its key itself and has no --plain. The fix names
    repeating the command, and SANDBOX_AI_SECRETS_PLAIN=1 for such a command - which then works."""
    host.env.pop("SANDBOX_AI_SECRETS_PLAIN", None)
    conf = host.directory / "wg.conf"
    conf.write_text("[Interface]\nPrivateKey = aGVsbG9oZWxsb2hlbGxvaGVsbG9oZWxsb2hlbGxvaGU=\nAddress = 10.64.0.2/32\n"
                    "DNS = 10.64.0.1\n[Peer]\nPublicKey = aGVsbG9oZWxsb2hlbGxvaGVsbG9oZWxsb2hlbGxvaGU=\n"
                    "Endpoint = 198.51.100.9:51820\nAllowedIPs = 0.0.0.0/0\n")
    conf.chmod(0o600)
    refused = host.run("vpn", "add", "wg1", "--wireguard", conf)
    assert refused.returncode != 0
    text = " ".join(refused.stderr.split())
    assert "then keep the secret again" not in text, text
    assert "sandbox-ai secrets password set, then repeat the command" in text, text
    assert "SANDBOX_AI_SECRETS_PLAIN=1 in front of the command" in text, text
    added = host.run("vpn", "add", "wg1", "--wireguard", conf, env={"SANDBOX_AI_SECRETS_PLAIN": "1"})
    assert added.returncode == 0, added.stderr
    assert "wg1" in host.run("vpn", "list").stdout


# --- `net allow --keep` made a project file for the run's agent alone, and did not say so ----------------------------

@pytest.fixture
def running(host):
    """A running session "web" of claude as `sandbox-ai net` finds it: its supervisor a process of the test, its run
    registered, its control socket answered by a thread of the test (every request "ok"; REQUESTS: what was asked)."""
    import socket
    import sys
    import threading
    from types import SimpleNamespace
    state = host.home / ".local/state/sandbox-ai/sessions/web"
    for folder in (state / "home", state / "tmux"):
        folder.mkdir(parents=True)
    supervisor = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)", "agent_sandbox.py",
                                   "--tmux-socket-dir", str(state / "tmux")])
    (state / "session.json").write_text(json.dumps({
        "agent": "claude", "options": [], "agent_args": [], "backend": "podman", "workspace": str(host.workspace),
        "cwd": str(host.workspace), "created": "2026-11-20T10:00:00Z", "pod": "agent-session-web", "name": "web",
        "pid": supervisor.pid, "started": "2026-11-20T10:00:01Z", "ready": True, "run": "session-web"}))
    run = host.runtime / "sandbox-ai/runs/session-web"
    run.mkdir(parents=True)
    session_dir = host.directory / "session-web"  # (the run's session directory: its hosts file)
    session_dir.mkdir()
    (session_dir / "hosts").write_text("127.0.0.1 localhost\n")
    (run / "run.json").write_text(json.dumps({"run": "session-web", "agent": "claude", "kind": "agent", "name": "web",
                                              "pid": supervisor.pid, "start": "web", "session_dir": str(session_dir),
                                              "workspace": str(host.workspace)}))
    server = socket.socket(socket.AF_UNIX)
    # As the supervisor binds it (runtime.listen_unix): a long tmp path (CI's /tmp/pytest-of-runner/...) is too long
    # a name for bind().
    RUNTIME.bind_long(server, run / "control.sock")
    server.listen(4)
    requests = []

    def serve():
        while True:
            try:
                connection, _ = server.accept()
            except OSError:
                return
            with connection:
                data = b""
                while not data.endswith(b"\n"):
                    chunk = connection.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                requests.append(json.loads(data or b"{}"))
                connection.sendall(b'{"ok": true, "allowed": [], "denied": []}\n')
    threading.Thread(target=serve, daemon=True).start()
    yield SimpleNamespace(supervisor=supervisor, requests=requests)
    server.close()
    supervisor.kill()
    supervisor.wait()


def test_net_allow_keep_says_that_its_new_project_file_is_for_that_agent(host, running):
    """`sandbox-ai net allow web --allow-tcp 192.0.2.5:443 --keep` where the project had no .sandbox-ai.json: one
    was made with "agent": "claude" - and from then on `sandbox-ai codex` there was refused ("... is for claude:
    start it with sandbox-ai claude, or start codex with --no-config"), unannounced. --keep says so when it makes
    the file."""
    kept = host.run("net", "allow", "web", "--allow-tcp", "192.0.2.5:443", "--keep")
    assert kept.returncode == 0, kept.stderr
    text = " ".join((kept.stdout + kept.stderr).split())
    project = host.workspace / ".sandbox-ai.json"
    assert json.loads(project.read_text())["agent"] == "claude"
    assert f"kept in {project} (trusted): --allow-tcp 192.0.2.5:443" in text, text
    assert "a new project file, for claude: another agent starts in this project with --no-config" in text, text
    again = host.run("net", "allow", "web", "--allow-tcp", "192.0.2.6:443", "--keep")
    assert "a new project file" not in again.stdout + again.stderr
    refused = host.run("codex", "--dry-run")
    assert "is for claude" in refused.stderr and "--no-config" in refused.stderr


# --- resume of a start whose folder is gone: "[Errno 2] No such file or directory: PATH" ----------------------------

def test_resume_of_a_start_whose_folder_is_gone_says_what_is_missing_and_the_fix(host):
    """A kept start made in a folder that was deleted (or moved) since: `sandbox-ai resume NAME` said only
    "[Errno 2] No such file or directory: '/…/gone'". It says which folder of the start is gone and what to do."""
    gone = host.directory / "gone"
    gone.mkdir()
    made = host.run("claude", "--name", "gw1", env={"FAKE_PODMAN_RUN": "0:0"}, cwd=gone)
    assert "gw1 keeps its HOME" in made.stderr, made.stderr
    gone.rmdir()
    resumed = host.run("resume", "gw1", env={"FAKE_PODMAN_RUN": "0:0"})
    assert resumed.returncode != 0
    text = " ".join(resumed.stderr.split())
    assert "[Errno 2]" not in text, text
    assert f"gw1 was started in {gone}, which is not there any more (moved or deleted?)" in text, text
    assert f"mkdir -p {gone}" in text and "sandbox-ai delete gw1 --yes" in text, text


def test_resume_of_a_start_whose_mount_source_is_gone_names_drop_option(host):
    """The same for a --mount whose source is gone: "Mount source unavailable: [Errno 2] No such file or directory:
    PATH", then "Session failed to start (status 1)". It names the mount and that --drop-option leaves it out."""
    source = host.directory / "mnt1"
    source.mkdir()
    made = host.run("claude", "--name", "gm1", "--mount", f"{source}:/mnt/m1:ro", env={"FAKE_PODMAN_RUN": "0:0"})
    assert "gm1 keeps its HOME" in made.stderr, made.stderr
    source.rmdir()
    resumed = host.run("resume", "gm1", env={"FAKE_PODMAN_RUN": "0:0"})
    assert resumed.returncode != 0
    text = " ".join(resumed.stderr.split())
    assert "[Errno 2]" not in text, text
    assert f"--mount {source}:/mnt/m1:ro: {source} is not there" in text, text
    assert "sandbox-ai resume NAME --drop-option --mount" in text, text


# --- a start's --mcp-mount whose folder is missing was told as "--mount NAME:TARGET=HOST" ---------------------------

def test_a_start_mcp_mount_problem_names_the_option_given(host):
    """`sandbox-ai claude --mcp radare2 --mcp-mount radare2:/work=/nonexistent:ro`: "--mount radare2:/work=
    /nonexistent:ro: the folder /nonexistent is not there ... --mount TARGET=HOST[:ro|rw]: ..." - the user gave
    --mcp-mount (NAME:TARGET=HOST); `--mount` is the start's own option, with another syntax. Named as given."""
    folder = host.directory / "samples"
    folder.mkdir()
    assert host.run("mcp", "add", "hub:radare2", "--no-pull", "--mount", f"/work={folder}:ro").returncode == 0
    result = host.run("claude", "--mcp", "radare2", "--mcp-mount", "radare2:/work=/nonexistent-audit:ro",
                      "--dry-run")
    assert result.returncode != 0
    text = " ".join(result.stderr.split())
    assert "--mcp-mount radare2:/work=/nonexistent-audit:ro: the folder /nonexistent-audit is not there" in text, text
    assert "--mount radare2:" not in text and "--mount TARGET=HOST" not in text, text
    assert "--mcp-mount NAME:TARGET=HOST[:ro|rw]" in text, text


def test_hosts_test_local_does_not_claim_ssh(host):
    """`sandbox-ai hosts test local` said "local: ssh ok, sandbox-ai X" - this machine is not reached over SSH."""
    tested = host.run("hosts", "test", "local")
    assert "local: this machine, no SSH, sandbox-ai " in tested.stdout, tested.stdout + tested.stderr
    assert "ssh ok" not in tested.stdout


def test_secrets_delete_names_what_still_names_the_secret(host):
    """`sandbox-ai secrets delete GH` of the secret a Git login names: "GH deleted" and nothing more - the next
    start leaves the login out ("git-logins: https://github.com left out: no secret GH"). It says what still names
    it, as `secrets rename` does."""
    kept = subprocess.run(host.command("secrets", "set", "GH"), input="ghp_audit1234567", text=True,
                          capture_output=True, env=host.env, cwd=host.workspace, timeout=60)
    assert kept.returncode == 0, kept.stderr
    assert host.run("git-logins", "add", "https://github.com", "--secret", "GH").returncode == 0
    deleted = host.run("secrets", "delete", "GH")
    assert deleted.returncode == 0
    text = " ".join(deleted.stdout.split())
    assert "GH deleted" in text and "note: still names GH: the Git login for https://github.com" in text, text
    alone = subprocess.run(host.command("secrets", "set", "SOLO"), input="x-audit-1", text=True,
                           capture_output=True, env=host.env, cwd=host.workspace, timeout=60)
    assert alone.returncode == 0
    assert "still names" not in host.run("secrets", "delete", "SOLO").stdout


# --- a pod an earlier run left behind that Podman does not remove: Python's CalledProcessError text ---------------

def test_a_left_pod_podman_does_not_remove_is_said_in_words(monkeypatch):
    """A session's restart removes the pod its supervisor left behind (crash, kill -9). When `podman pod rm` failed
    (a full disk, a stuck container), `sandbox-ai resume` said "Command '['podman', 'pod', 'rm', '--force', ...]'
    returned non-zero exit status 125". (The fake Podman labels no pod, so this is driven at the function.)"""
    import importlib.util
    from types import SimpleNamespace
    from live_fakes import ROOT
    spec = importlib.util.spec_from_file_location("agent_sandbox_for_audit_pod", ROOT / "sandbox/agent_sandbox.py")
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)

    def run(command, **kwargs):
        if command[1:3] == ["pod", "inspect"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps({launcher.SESSION_LABEL: "/own"}), stderr="")
        if kwargs.get("check"):
            raise subprocess.CalledProcessError(125, command)
        return SimpleNamespace(returncode=125, stdout="", stderr="")
    monkeypatch.setattr(launcher.subprocess, "run", run)
    with pytest.raises(ValueError) as refused:
        launcher.remove_stale_pod(SimpleNamespace(podman_url=None), "agent-claude-1234", "/own")
    text = str(refused.value)
    assert "Command '[" not in text
    assert text.startswith("Podman did not remove the pod agent-claude-1234 an earlier run of this start left behind "
                           "(exit status 125")
    assert "podman pod rm --force agent-claude-1234" in text


# --- `pause` where Podman cannot pause (rootless without cgroups v2 / a user session): a Python exception text ------

CGROUP_V1 = "125:Error: pausing containers is not supported on rootless containers with cgroup v1"


def test_pause_that_podman_refuses_says_why_and_that_it_runs_on(host):
    """`sandbox-ai pause NAME` (the app's Pause) on a host whose rootless Podman cannot freeze a pod: "sandbox-ai pause:
    Command '['podman', 'pod', 'pause', 'agent-…']' returned non-zero exit status 125." Now Podman's reason, what
    helps, and that it runs on; it is not marked paused."""
    state = host.home / ".local/state/sandbox-ai/sessions/pw1"
    (state / "tmux").mkdir(parents=True)
    (state / "home").mkdir()
    import sys
    supervisor = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)", "agent_sandbox.py",
                                   "--tmux-socket-dir", str(state / "tmux")])
    try:
        (state / "session.json").write_text(json.dumps({
            "agent": "claude", "options": [], "agent_args": [], "backend": "podman", "workspace": str(host.workspace),
            "cwd": str(host.workspace), "created": "2026-11-20T10:00:00Z", "pod": "agent-claude-pw1", "name": "pw1",
            "pid": supervisor.pid, "started": "2026-11-20T10:00:01Z", "ready": True, "run": "claude-pw1"}))
        paused = host.run("pause", "pw1", env={"FAKE_PODMAN_POD_PAUSE_FAIL": CGROUP_V1})
        assert paused.returncode != 0
        text = " ".join(paused.stderr.split())
        assert "Command '[" not in text and "returned non-zero exit status" not in text, text
        assert "Podman did not pause the pod agent-claude-pw1 (exit status 125): Error: pausing containers is not " \
               "supported on rootless containers with cgroup v1" in text, text
        assert "-> Podman cannot use cgroups here" in text and "(it runs on, not paused)" in text, text
        assert not (state / "paused").exists()
        assert "pw1" in host.run("list").stdout and "paused" not in host.run("list").stdout
    finally:
        supervisor.kill()
        supervisor.wait()


def test_net_allow_before_the_control_socket_is_up_says_so(host, running):
    """`sandbox-ai net allow NAME ...` right after a start, before its supervisor listens: "the sandbox does not
    answer on …/control.sock: No such file or directory". It says that it is still starting (or has ended)."""
    control = host.runtime / "sandbox-ai/runs/session-web/control.sock"
    control.unlink()
    refused = host.run("net", "allow", "web", "--allow-tcp", "192.0.2.5:443")
    assert refused.returncode != 0
    text = " ".join(refused.stderr.split())
    assert "No such file or directory" not in text, text
    assert f"the sandbox does not answer yet: its control socket {control} is not there - it is still starting" \
           in text, text


# --- `report --project` of a folder that is not there: a whole empty report, "Nothing was refused" ------------------

def test_report_of_a_project_or_log_folder_that_is_not_there_says_so(host):
    """`sandbox-ai report --project ~/src/apq` (a typo) wrote a whole report of 0 runs - "Nothing was refused, held or
    reported." - for a project that is not there. Refused, naming what is missing; a project that is there with no
    runs yet still gets its (empty) report."""
    missing = host.directory / "no-such-project"
    refused = host.run("report", "--project", missing)
    assert refused.returncode != 0 and "Nothing was refused" not in refused.stdout
    assert f"no project {missing}: no such folder, and no runs of it logged here" in refused.stderr, refused.stderr
    gone = host.run("report", "--log-dir", host.directory / "no-such-logs")
    assert gone.returncode != 0 and "no log folder" in gone.stderr, gone.stderr
    there = host.run("report", "--project", host.workspace)
    assert there.returncode == 0 and "Runs | 0" in there.stdout, there.stderr
    # A project deleted since, whose runs are logged: reported as before.
    gone_project = host.directory / "gone-project"
    gone_project.mkdir()
    host.run("claude", "--name", "gp1", env={"FAKE_PODMAN_RUN": "0:0"}, cwd=gone_project)
    gone_project.rmdir()
    assert host.run("report", "--project", gone_project).returncode == 0


# --- `list`: "running (no terminal: see its log)" - which log, and what to do ----------------------------------------

def test_list_names_the_log_and_the_fix_of_a_start_without_a_terminal(host):
    """A session that runs without a terminal to attach to (its start gave up, its tmux session never came): `list`
    said "running (no terminal: see its log)" and nothing more - which log? As for an orphaned start, a line below the
    table names its log and what to do."""
    import sys
    state = host.home / ".local/state/sandbox-ai/sessions/nt1"
    (state / "tmux").mkdir(parents=True)
    (state / "home").mkdir()
    supervisor = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)", "agent_sandbox.py",
                                   "--tmux-socket-dir", str(state / "tmux")])
    try:
        (state / "session.json").write_text(json.dumps({
            "agent": "claude", "options": [], "agent_args": [], "backend": "podman", "workspace": str(host.workspace),
            "cwd": str(host.workspace), "created": "2026-11-20T10:00:00Z", "pod": "agent-claude-nt1", "name": "nt1",
            "pid": supervisor.pid, "started": "2026-11-20T10:00:01Z", "run": "claude-nt1", "mode": "tmux"}))
        listed = host.run("list")
        text = " ".join(listed.stdout.split())
        assert "running (no terminal: see its log)" in text, text
        assert f"nt1 runs without a terminal to attach to; its log: {state / 'supervisor.log'}; end it with: " \
               "sandbox-ai stop nt1, then start it again: sandbox-ai resume nt1" in text, text
    finally:
        supervisor.kill()
        supervisor.wait()
