"""Security review SAI-18 and SAI-21, the part that holds today: `runtime.py --untrusted COMMAND` runs a command of
the sandbox without the agent's own - by the kernel (Landlock), although it is the agent's user.

The agent and its commands are one user: file modes keep a test suite from nothing of the agent's. An untrusted
command is confined for good, with everything it starts: no access to the agents' state and the credentials below
HOME nor to the run's folders, HOME read-only, no way into the processes outside it, and - where the kernel has the
right - no connection to a Unix socket it has no access to (tmux, the hooks, the MCP relays).

Real processes under the real Landlock of this kernel, against a made-up HOME with fixture credentials (never the
real ones); skipped where the kernel has no Landlock. The parts the kernel may lack are checked where it has them."""
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "sandbox" / "runtime.py"


def load(name, as_name):
    spec = importlib.util.spec_from_file_location(as_name, ROOT / "sandbox" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rt = load("runtime", "sandbox_runtime_review_untrusted_test")

try:
    SUPPORT = rt.landlock_supported()
except OSError:
    SUPPORT = None
needs_landlock = pytest.mark.skipif(SUPPORT is None, reason="this kernel has no Landlock")
FIXTURE_TOKEN = "fixture-token-not-a-credential"


@pytest.fixture
def home(untrusted_place):
    """A sandbox HOME with an agent's login and state, credentials, and what a project needs of a HOME - outside the
    folders an untrusted command may change, as /root is (untrusted_place, conftest.py)."""
    made = untrusted_place / "home"
    for folder in (".codex/sessions", ".claude/projects", ".config/gh", ".config/git", ".config/tool", ".ssh",
                   ".local/bin", ".local/share/opencode", ".cache"):
        (made / folder).mkdir(parents=True)
    (made / ".codex" / "auth.json").write_text(json.dumps({"tokens": {"access_token": FIXTURE_TOKEN}}))
    (made / ".codex" / "sessions" / "rollout.jsonl").write_text('{"prompt": "private"}\n')
    (made / ".claude" / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": FIXTURE_TOKEN}}))
    (made / ".claude.json").write_text("{}")
    (made / ".config" / "gh" / "hosts.yml").write_text(f"oauth_token: {FIXTURE_TOKEN}\n")
    (made / ".config" / "git" / "credentials").write_text(f"fixture line {FIXTURE_TOKEN}\n")
    (made / ".config" / "git" / "config").write_text("[user]\n\tname = Fixture\n")
    (made / ".config" / "tool" / "settings").write_text("plain settings\n")
    (made / ".ssh" / "id_ed25519").write_text("fixture key\n")
    (made / ".git-credentials").write_text(f"fixture line {FIXTURE_TOKEN}\n")
    (made / ".bashrc").write_text("# the agent's shell reads this\n")
    (made / ".local" / "bin" / "tool").write_text("#!/bin/sh\necho tool\n")
    (made / ".local" / "bin" / "tool").chmod(0o755)
    return made


def run(home, *command, options=(), env=None, cwd=None, input=None):
    """`runtime.py --untrusted OPTIONS -- COMMAND` with HOME, as a process of the sandbox starts it."""
    return subprocess.run([sys.executable, "-I", str(RUNTIME), "--untrusted", *options, "--", *command],
                          env={"PATH": os.environ["PATH"], "HOME": str(home), "LANG": "C.UTF-8", **(env or {})},
                          cwd=cwd, capture_output=True, text=True, timeout=60, input=input)


READ = "import sys\ntry:\n    print(open(sys.argv[1]).read())\nexcept OSError as exc:\n    print('REFUSED', exc.errno)\n"


@needs_landlock
def test_an_untrusted_command_cannot_read_the_agents_logins_state_or_credentials(home):
    for secret in (".codex/auth.json", ".codex/sessions/rollout.jsonl", ".claude/.credentials.json", ".claude.json",
                   ".config/gh/hosts.yml", ".config/git/credentials", ".ssh/id_ed25519", ".git-credentials"):
        done = run(home, sys.executable, "-c", READ, str(home / secret))
        assert done.returncode == 0 and done.stdout.strip() == "REFUSED 13", (secret, done.stdout, done.stderr)
        assert FIXTURE_TOKEN not in done.stdout + done.stderr
    # Nor list, replace, rename or link them, by any of its names.
    script = ("import os, sys\nhome = sys.argv[1]\nfor name, call in (\n"
              " ('list', lambda: os.listdir(home + '/.codex')),\n"
              " ('stat-walk', lambda: open(home + '/.codex/../.codex/auth.json').read()),\n"
              " ('write', lambda: open(home + '/.codex/auth.json', 'w')),\n"
              " ('rename', lambda: os.rename(home + '/.codex', home + '/.cache/x')),\n"
              " ('link', lambda: os.link(home + '/.codex/auth.json', '/tmp/untrusted-link-' + str(os.getpid()))),\n"
              " ('symlink', lambda: (os.symlink(home + '/.codex/auth.json', '/tmp/untrusted-sym-' + str(os.getpid())),\n"
              "                       open('/tmp/untrusted-sym-' + str(os.getpid())).read())),\n"
              " ('unlink', lambda: os.unlink(home + '/.git-credentials'))):\n"
              "    try:\n        call()\n        print(name, 'DONE')\n"
              "    except OSError as exc:\n        print(name, 'REFUSED')\n")
    done = run(home, sys.executable, "-c", script, str(home))
    assert done.stdout.split() == [word for name in ("list", "stat-walk", "write", "rename", "link", "symlink",
                                                     "unlink") for word in (name, "REFUSED")], done.stdout + done.stderr
    assert (home / ".codex" / "auth.json").exists() and (home / ".git-credentials").exists()
    for leftover in Path("/tmp").glob("untrusted-sym-*"):
        leftover.unlink()


@needs_landlock
def test_an_untrusted_command_reads_the_rest_of_home_and_changes_none_of_it(home):
    """What a project needs of a HOME is there to read and run; what the agent's own shell reads at its next start
    cannot be changed by it."""
    done = run(home, "sh", "-c", f"cat {home}/.config/tool/settings {home}/.config/git/config && {home}/.local/bin/tool")
    assert done.returncode == 0 and "plain settings" in done.stdout and "Fixture" in done.stdout
    assert done.stdout.strip().endswith("tool")
    for target in (".bashrc", ".local/bin/tool", ".config/tool/settings", ".local/bin/new", ".cache/x", "new-file"):
        done = run(home, "sh", "-c", f"echo poisoned >> {home}/{target}")
        assert done.returncode != 0 and "poisoned" not in (home / target).read_text() if (home / target).exists() \
            else done.returncode != 0, (target, done.stderr)
    assert (home / ".bashrc").read_text() == "# the agent's shell reads this\n"


@needs_landlock
def test_an_untrusted_command_works_in_the_workspace_and_tmp(home, untrusted_place):
    """The workspace (here: a folder given with --write, as /work is by itself) and /tmp are its to change; its exit
    status and output are the command's."""
    work = untrusted_place / "work"  # (outside /tmp: else it would be writable without --write)
    work.mkdir()
    done = run(home, "sh", "-c", "echo built > out.txt && mkdir -p sub/dir && mv out.txt sub/dir/ && cat sub/dir/out.txt"
               " && t=$(mktemp) && echo ok > $t && cat $t && rm $t && exit 7", options=("--write", str(work)), cwd=work)
    assert (done.returncode, done.stdout.split()) == (7, ["built", "ok"]), done.stderr
    assert (work / "sub" / "dir" / "out.txt").read_text() == "built\n"
    # Without --write a folder outside the workspaces is read-only.
    done = run(home, "sh", "-c", f"echo x > {work}/denied")
    assert done.returncode != 0 and not (work / "denied").exists()
    assert rt.UNTRUSTED_WRITABLE == ("/work", "/workspaces", "/tmp", "/var/tmp", "/dev", "/proc")


@needs_landlock
def test_the_confinement_is_for_good_and_for_everything_the_command_starts(home):
    script = (f"import subprocess, sys\n"
              f"print(subprocess.run(['sh', '-c', 'cat {home}/.codex/auth.json'], capture_output=True, text=True).returncode)\n"
              f"print(subprocess.run([sys.executable, '-I', {str(RUNTIME)!r}, '--untrusted', '--', 'cat', "
              f"'{home}/.codex/auth.json'], capture_output=True, text=True).returncode)\n")
    done = run(home, sys.executable, "-c", script)
    # (inside the confinement a second --untrusted cannot make its rules - it lists no folder on the way: not run)
    assert done.stdout.split() == ["1", "126"] and FIXTURE_TOKEN not in done.stdout + done.stderr


@needs_landlock
def test_an_untrusted_command_cannot_read_another_process_of_the_sandbox(home):
    """/proc/PID/environ, its memory and its descriptors of a process outside the confinement (the agent: here, the
    test's own process, which holds a made-up secret in its environment)."""
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                              env={"PATH": os.environ["PATH"], "FIXTURE_SECRET": FIXTURE_TOKEN})
    try:
        script = ("import os, sys\npid = sys.argv[1]\n"
                  "for name in ('environ', 'mem', 'fd', 'cwd', 'maps'):\n"
                  "    try:\n"
                  "        path = f'/proc/{pid}/{name}'\n"
                  "        os.readlink(path + '/0') if name == 'fd' else os.readlink(path) if name == 'cwd' else "
                  "open(path, 'rb').read(4096)\n"
                  "        print(name, 'READ')\n"
                  "    except OSError:\n        print(name, 'REFUSED')\n")
        done = run(home, sys.executable, "-c", script, str(holder.pid))
        assert done.stdout.split() == [word for name in ("environ", "mem", "fd", "cwd", "maps")
                                       for word in (name, "REFUSED")], done.stdout + done.stderr
        if SUPPORT["scope"]:  # Linux 6.12: nor signal it
            done = run(home, sys.executable, "-c", "import os, sys\ntry:\n    os.kill(int(sys.argv[1]), 15)\n"
                                                   "    print('SENT')\nexcept OSError:\n    print('REFUSED')\n",
                       str(holder.pid))
            assert done.stdout.strip() == "REFUSED" and holder.poll() is None
    finally:
        holder.kill()
        holder.wait()


@needs_landlock
@pytest.mark.skipif(SUPPORT is not None and not SUPPORT["unix"],
                    reason="this kernel's Landlock has no access right for a Unix socket's path")
def test_an_untrusted_command_cannot_connect_to_the_agents_terminal_hooks_or_relays(home, untrusted_place,
                                                                                    monkeypatch):
    """SAI-21, SAI-22, SAI-23, SAI-14 for a confined command: the Unix sockets in the folders it has no access to -
    tmux's, the hooks', the MCP relays', the terminal relays' - cannot be connected, stream or datagram; one in the
    workspace can."""
    protected = untrusted_place / "protected"
    work = untrusted_place / "work"
    for folder in (protected / "tmux-0", work):
        folder.mkdir(parents=True)
    served = []

    def listener(path):
        server = socket.socket(socket.AF_UNIX)
        server.bind(str(path))
        server.listen(4)
        server.settimeout(0.2)

        def accept():
            while True:
                try:
                    connection, _ = server.accept()
                except socket.timeout:
                    continue
                except OSError:
                    return
                served.append(str(path))
                connection.close()
        threading.Thread(target=accept, daemon=True).start()
        return server
    servers = [listener(protected / "tmux-0" / "default"), listener(work / "own.sock")]
    monkeypatch.setattr(rt, "UNTRUSTED_SYSTEM", (*rt.UNTRUSTED_SYSTEM, str(protected)))
    script = ("import socket, sys\nfor path in sys.argv[1:]:\n"
              "    client = socket.socket(socket.AF_UNIX)\n"
              "    try:\n        client.connect(path)\n        print('CONNECTED')\n"
              "    except OSError as exc:\n        print('REFUSED', exc.errno)\n")
    read_end, write_end = os.pipe()
    pid = os.fork()
    if pid == 0:  # (in a child: the confinement is for good)
        os.close(read_end)
        status = 1
        try:
            os.dup2(write_end, 1)
            os.environ["HOME"] = str(home)
            status = rt.untrusted(["--write", str(work), "--", sys.executable, "-c", script,
                                   str(protected / "tmux-0" / "default"), str(work / "own.sock")])
        finally:
            os._exit(status or 1)
    os.close(write_end)
    with os.fdopen(read_end) as stream:
        said = stream.read()
    os.waitpid(pid, 0)
    for server in servers:
        server.close()
    assert said.split() == ["REFUSED", "13", "CONNECTED"], said
    assert served == [str(work / "own.sock")]


@needs_landlock
def test_an_untrusted_commands_environment_has_no_secrets(home):
    """The launcher's secret names (the run's configuration), every name that looks like a key or token, and tmux's
    are gone; --env NAME keeps one; the rest stays."""
    env = {"ANTHROPIC_API_KEY": FIXTURE_TOKEN, "GH_TOKEN": FIXTURE_TOKEN, "MY_PASSWORD": "p", "DATABASE_URL": "db",
           "TMUX": "/sandbox/tmux/tmux-0/default,5,0", "SSH_AUTH_SOCK": "/x", "PLAIN": "kept", "NPM_AUTH": "a"}
    done = run(home, "env", env=env)
    names = {line.split("=", 1)[0] for line in done.stdout.splitlines()}
    assert {"PLAIN", "DATABASE_URL", "HOME", "PATH"} <= names
    assert not names & {"ANTHROPIC_API_KEY", "GH_TOKEN", "MY_PASSWORD", "TMUX", "SSH_AUTH_SOCK", "NPM_AUTH"}
    assert FIXTURE_TOKEN not in done.stdout
    done = run(home, "env", env=env, options=("--env", "GH_TOKEN"))
    assert f"GH_TOKEN={FIXTURE_TOKEN}" in done.stdout and "ANTHROPIC_API_KEY" not in done.stdout
    # The names the launcher declared secret, whatever they are called.
    kept = rt.untrusted_environment({"DATABASE_URL": "db", "PLAIN": "x"}, secret_names=("DATABASE_URL",))
    assert kept == {"PLAIN": "x"}


def test_a_command_that_cannot_be_confined_is_not_run(home, monkeypatch, capsys):
    """Fail closed: a kernel without Landlock, a rule the kernel refuses, a --write that would open what is protected."""
    executed = []
    monkeypatch.setattr(rt, "landlock_supported", lambda libc=None: (_ for _ in ()).throw(
        OSError(38, "this kernel has no Landlock (Function not implemented)")))
    assert rt.untrusted(["--", "true"], environ={"HOME": str(home)}, execute=lambda *a: executed.append(a)) == 126
    assert "not run - the command cannot be confined" in capsys.readouterr().err and executed == []
    monkeypatch.undo()
    if SUPPORT is not None:
        monkeypatch.setattr(rt, "landlock_restrict", lambda *a, **k: (_ for _ in ()).throw(OSError(1, "refused")))
        assert rt.untrusted(["--", "true"], environ={"HOME": str(home)}, execute=lambda *a: executed.append(a)) == 126
        monkeypatch.undo()
        for path in (str(home / ".codex"), str(home), "/", str(home / ".codex" / "sessions"), "/sandbox/tmux"):
            assert rt.untrusted(["--write", path, "--", "true"], environ={"HOME": str(home)},
                                execute=lambda *a: executed.append(a)) == 126, path
        assert "what an untrusted command gets no access to" in capsys.readouterr().err
    assert executed == []
    assert rt.untrusted([], environ={"HOME": str(home)}) == 2
    assert rt.untrusted(["--no-such-option", "true"], environ={"HOME": str(home)}) == 2


def test_a_home_in_a_folder_the_command_may_change_is_refused(untrusted_place, capsys):
    """HOME below /tmp or /var/tmp (both the command's to change): the agents' folders below it would be changeable
    and readable through that rule - Landlock has no "all but" - so the command is not run. (GitHub's runner puts
    pytest's tmp under /tmp: the tests' made-up HOME lies outside, as /root and /home/USER do.)"""
    for folder in ("/tmp", "/var/tmp"):
        home = f"{folder}/sandbox-ai-test-no-such-place/home"
        with pytest.raises(ValueError, match=f"--write {folder}: it holds, or lies in, what an untrusted command gets "
                                             f"no access to \\({home}/"):
            rt.untrusted_plan(home)
        executed = []
        assert rt.untrusted(["--", "true"], environ={"HOME": home}, execute=lambda *a: executed.append(a)) == 126
        assert executed == [] and "not run - the command cannot be confined" in capsys.readouterr().err
    # The tests' own HOME is where a real one is: outside every folder the command may change.
    real = os.path.realpath(untrusted_place)
    assert not any(real == item or real.startswith(item + "/") for item in rt.UNTRUSTED_WRITABLE), real


def test_the_paths_named_are_everything_but_the_protected(tmp_path):
    """Landlock has no "all but": the rules name every entry beside a protected one, on the way down to it. A link is
    never named (one planted where an entry is allowed would open its target)."""
    root = tmp_path / "root"
    for folder in ("home/.codex", "home/.config/gh", "home/.config/tool", "home/.cache", "usr/bin", "run/hooks",
                   "run/ca"):
        (root / folder).mkdir(parents=True)
    (root / "home" / ".bashrc").write_text("")
    (root / "home" / "sneaky").symlink_to(root / "home" / ".codex")
    (root / "home" / ".config" / "link").symlink_to(root / "home" / ".config" / "gh")
    protected = {str(root / "home" / ".codex"), str(root / "home" / ".config" / "gh"), str(root / "run" / "hooks")}
    named, cut = rt.untrusted_paths(protected, str(root))
    assert named == sorted(str(root / path) for path in ("home/.bashrc", "home/.cache", "home/.config/tool", "run/ca",
                                                         "usr")) and cut == {}
    # The protected: the agents' and the credentials' below HOME, the run's own folders.
    guarded = rt.untrusted_protected("/root")
    for path in ("/root/.codex", "/root/.claude", "/root/.claude.json", "/root/.config/gh", "/root/.ssh",
                 "/root/.git-credentials", "/root/.local/share/opencode", "/sandbox", "/run/sandbox-hooks",
                 "/run/sandbox-mcp"):
        assert path in guarded, path


def test_the_check_says_what_holds_and_the_note_what_the_kernel_lacks(home):
    full = {"abi": 10, "fs": 0, "unix": True, "scope": 3}
    assert rt.untrusted_note(full) is None
    old = rt.untrusted_note({"abi": 3, "fs": 0, "unix": False, "scope": 0})
    assert "ABI 3" in old and "tmux" in old and "signals" in old
    if SUPPORT is None:
        return
    done = subprocess.run([sys.executable, "-I", str(RUNTIME), "--untrusted", "--check"], capture_output=True,
                          text=True, timeout=60, env={"PATH": os.environ["PATH"], "HOME": str(home),
                                                      "FIXTURE_TOKEN": "x"})
    said = json.loads(done.stdout)
    assert said["landlock"] == SUPPORT["abi"] and said["unix_sockets"] == SUPPORT["unix"]
    assert str(home / ".codex") in said["protected"] and "/sandbox" in said["protected"]
    assert said["environment_dropped"] == ["FIXTURE_TOKEN"] and "/tmp" in said["write"]
    assert isinstance(said["unreadable"], dict)  # (crowded folders on the way: what of them is left out)


def test_crowded_folders_on_the_way_are_bounded_never_opened(tmp_path):
    """A folder on the way to a protected one with more entries than LIMIT: the way down is taken all the same (it is
    known, not searched for), the protected stay out, and of the rest only LIMIT entries are named - dot entries
    first, then folders, then the others, by name; what is left out has no access (fail closed) and is counted.
    LISTED bounds what is read of one folder at all."""
    root = tmp_path / "root"
    for folder in ("home/.codex", "home/.config/gh", "home/.config/tool", "home/.cache", "home/zdir", "usr"):
        (root / folder).mkdir(parents=True)
    for name in ("a", "b", "c", ".profile"):
        (root / "home" / name).write_text("")
    protected = {str(root / "home" / ".codex"), str(root / "home" / ".config" / "gh")}
    named, cut = rt.untrusted_paths(protected, str(root), limit=3)
    home = root / "home"
    assert named == sorted([str(home / ".cache"), str(home / ".profile"), str(home / "zdir"),
                            str(home / ".config" / "tool"), str(root / "usr")])
    assert cut == {str(home): 3}  # a, b, c: no rule, no access
    assert not set(named) & protected
    named, cut = rt.untrusted_paths(protected, str(root), limit=3, listed=4)
    assert cut[str(home)] == -1 and not set(named) & protected  # more than LISTED there: not even counted
    assert str(home / ".config" / "tool") in named  # the way down is taken all the same
    for index in range(6):  # links count as read too: a folder of links is not read to its end
        (root / "usr" / f"link{index}").symlink_to(root / "usr")
    _, cut = rt.untrusted_paths(protected | {str(root / "usr" / "x")}, str(root), listed=5)
    assert cut[str(root / "usr")] == -1


def test_a_symlinked_way_to_home_does_not_open_the_protected(tmp_path):
    """HOME reached through a link (/home -> /var/home): the protected are the real folders, not only the link's
    names - else the real ones would be named as part of a sibling and readable."""
    real = tmp_path / "real" / "home"
    (real / ".codex").mkdir(parents=True)
    (real / ".cache").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    guarded = rt.untrusted_protected(str(tmp_path / "link" / "home"))
    assert str(real / ".codex") in guarded and str(tmp_path / "link" / "home" / ".codex") in guarded
    named, _ = rt.untrusted_paths(guarded, str(tmp_path))
    assert str(tmp_path / "real") not in named and str(real / ".cache") in named
    assert not any(path == str(real / ".codex") or path.startswith(str(real / ".codex") + "/")
                   or str(real / ".codex").startswith(path + "/") for path in named)


@needs_landlock
def test_a_symlinked_way_to_home_keeps_the_login_unreadable(home, untrusted_place):
    (untrusted_place / "via").symlink_to(untrusted_place)
    linked = untrusted_place / "via" / "home"
    done = run(linked, sys.executable, "-c", READ, str(home / ".codex" / "auth.json"))
    assert done.returncode == 0 and "REFUSED 13" in done.stdout and FIXTURE_TOKEN not in done.stdout


@needs_landlock
def test_a_home_with_many_entries_is_confined_correctly_and_quickly(home, untrusted_place):
    """Regression: a HOME (or a folder above it) with more than 20000 entries was refused ("more than 20000 entries
    on the way to the protected folders"), so a full test run's shared temp folder failed every test here. The
    folders beside the way are not walked whatever they hold; a crowded folder on the way is bounded."""
    import time
    # Many files in folders not on the way: never looked into.
    for folder in (home / ".cache" / "deep", untrusted_place / "project" / "node_modules"):
        folder.mkdir(parents=True)
        for index in range(15000):
            (folder / f"f{index:05}").touch()
    # Many entries in folders on the way: HOME itself and the folder above it.
    for index in range(rt.UNTRUSTED_SIBLINGS + 1500):
        (home / f"junk{index:05}").touch()
    for index in range(12000):
        (untrusted_place / f"beside{index:05}").touch()
    script = ("import sys\nfor path in sys.argv[1:]:\n    try:\n        open(path).read(); print('READ', path)\n"
              "    except OSError as exc:\n        print('REFUSED', exc.errno, path)\n")
    targets = [home / ".codex" / "auth.json", home / ".git-credentials", home / ".ssh" / "id_ed25519",
               home / ".bashrc", home / ".config" / "tool" / "settings", home / ".cache" / "deep" / "f14999",
               home / "junk00000", home / f"junk{rt.UNTRUSTED_SIBLINGS + 1499:05}", untrusted_place / "beside00000"]
    started = time.monotonic()
    done = run(home, sys.executable, "-c", script, *map(str, targets))
    took = time.monotonic() - started
    assert done.returncode == 0, done.stderr
    said = dict((line.split()[-1], line.split()[0]) for line in done.stdout.splitlines())
    for secret in targets[:3]:
        assert said[str(secret)] == "REFUSED", secret
    for readable in targets[3:7]:
        assert said[str(readable)] == "READ", readable
    # Beyond the bound: no access (never more), and said so.
    assert said[str(targets[7])] == "REFUSED"
    assert f"not readable for the command" in done.stderr and str(home) in done.stderr
    assert FIXTURE_TOKEN not in done.stdout
    assert took < 30, took
