"""home_guard: tests never write into the real HOME (an agent running them would log itself out)."""
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import home_guard  # noqa: E402

home_guard.ensure()


def test_the_session_runs_in_a_temporary_home():
    # conftest.py (or the import above) moved HOME before any test module ran.
    assert os.environ.get(home_guard.MARKER)
    if os.environ[home_guard.MARKER] != "live":
        import pwd
        real = pwd.getpwuid(os.getuid()).pw_dir
        assert Path(os.environ["HOME"]).resolve() != Path(real).resolve()
        assert not any(name in os.environ for name in home_guard.DROPPED)


def test_ensure_moves_home_and_the_agents_variables_once(tmp_path):
    environ = {"HOME": "/root", "CLAUDE_CONFIG_DIR": "/root/.claude", "CODEX_HOME": "/root/.codex",
               "XDG_RUNTIME_DIR": "/run/user/1000"}
    root = home_guard.ensure(environ)
    try:
        assert environ["HOME"] == str(root / "home") and Path(environ["HOME"]).is_dir()
        assert all(environ[name].startswith(str(root)) for name in home_guard.MOVED)
        assert "CLAUDE_CONFIG_DIR" not in environ and "CODEX_HOME" not in environ
        assert environ["XDG_RUNTIME_DIR"] == "/run/user/1000"  # Podman finds the user's session through it
        assert home_guard.ensure(environ) is None and environ["HOME"] == str(root / "home")  # once
    finally:
        import shutil
        shutil.rmtree(root, ignore_errors=True)


def test_a_changed_login_is_noticed(tmp_path):
    home = tmp_path / "real"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude/.credentials.json").write_text('{"claudeAiOauth": {"accessToken": "real"}}')
    (home / ".local/share/sandbox-ai/auth/claude").mkdir(parents=True)
    paths = home_guard.login_files({"HOME": str(home), "CODEX_HOME": str(tmp_path / "codex")})
    assert home / ".claude/.credentials.json" in paths and tmp_path / "codex/auth.json" in paths
    before = home_guard.fingerprint(paths)
    assert home_guard.changed(before) == []
    (home / ".local/share/sandbox-ai/auth/claude/x.json").write_text("{}")  # a file in the login store
    (home / ".claude/.credentials.json").write_text('{"claudeAiOauth": {"accessToken": "fixture-access"}}')
    assert home_guard.changed(before) == sorted([str(home / ".claude/.credentials.json"),
                                                 str(home / ".local/share/sandbox-ai/auth")])


def test_the_apps_tests_move_home_as_well():
    """The desktop app's tests (projects/sandbox-ai-app/tests) call sandbox-ai's modules and commands too: their
    conftest.py moves HOME with the same home_guard before anything is imported, and fails a session that changed a
    real login file (2026-11-08: an app test run in the real HOME was the next way to log the agent out)."""
    app_tests = Path(__file__).resolve().parents[1] / "projects" / "sandbox-ai-app" / "tests"
    assert (app_tests / "home_guard.py").read_text() == (Path(__file__).resolve().parent / "home_guard.py").read_text()
    conftest = (app_tests / "conftest.py").read_text()
    guard = conftest.index("home_guard.ensure()")
    assert guard < conftest.index("import PySide6"), "HOME moves before Qt (or anything else) is loaded"
    assert "home_guard.changed(_REAL_LOGINS)" in conftest


# The ssh stubs (no_ssh): refuse every real host, hand over to the real ssh for this machine (a test's own sshd,
# CI's files-remote job, 2026-11-14: "ssh: tests reach no real host" for runner@127.0.0.1) and for a test that opts in.

import shutil as _shutil  # noqa: E402
import subprocess as _subprocess  # noqa: E402

import pytest  # noqa: E402


def test_ssh_arguments_are_read_as_ssh_reads_them():
    assert home_guard.ssh_arguments(["-o", "BatchMode=yes", "-p22", "-l", "me", "--", "127.0.0.1", "sftp"]) == (
        [("o", "BatchMode=yes"), ("p", "22"), ("l", "me")], "127.0.0.1")
    assert home_guard.ssh_arguments(["host", "-p", "2", "ls", "-W"]) == ([("p", "2")], "host")  # -W of the command
    assert home_guard.ssh_arguments(["-NL", "1:x:2", "localhost"]) == ([("N", None), ("L", "1:x:2")], "localhost")
    assert home_guard.ssh_arguments(["-Z", "localhost"]) is None  # unknown: refused, never guessed
    assert home_guard.ssh_arguments(["-p"]) is None


def fake_g(output, returncode=0):
    return lambda argv, **kwargs: _subprocess.CompletedProcess(argv, returncode, output, "")


@pytest.mark.parametrize("args, output, local", [
    (["runner@127.0.0.1"], "hostname 127.0.0.1\nport 22\nproxyjump none\n", True),
    (["-p", "2222", "--", "::1", "sftp"], "hostname ::1\n", True),
    (["localhost"], "hostname localhost\ntunnel false\n", True),
    (["root@192.0.2.11", "true"], "hostname 192.0.2.11\n", False),
    (["127.0.0.1"], "hostname 192.0.2.11\n", False),  # a HostName of the configuration
    (["127.0.0.1"], "hostname 127.0.0.1\nproxyjump jump.example\n", False),
    (["127.0.0.1"], "hostname 127.0.0.1\nproxycommand nc %h %p\n", False),
    (["127.0.0.1"], "hostname 127.0.0.1\nlocalforward 1 [192.0.2.1]:22\n", False),
    (["127.0.0.1"], "hostname 127.0.0.1\ntunnel point-to-point\n", False),
    (["-J", "127.0.0.1", "127.0.0.1"], "hostname 127.0.0.1\n", False),
    (["-W", "192.0.2.1:22", "127.0.0.1"], "hostname 127.0.0.1\n", False),
    (["-D", "1080", "localhost"], "hostname localhost\n", False),
    ([], "hostname 127.0.0.1\n", False),
])
def test_only_this_machine_is_a_local_destination(args, output, local):
    assert home_guard.local_destination(args, "/usr/bin/ssh", run=fake_g(output)) is local


def test_a_failing_ssh_g_is_no_local_destination():
    assert home_guard.local_destination(["127.0.0.1"], "/usr/bin/ssh", run=fake_g("", 255)) is False


@pytest.fixture
def stubs(tmp_path):
    """The stubs first on PATH, behind them a 'real' ssh, scp and sftp that say so (ssh -G is OpenSSH's)."""
    real_ssh = _shutil.which("ssh", path=os.pathsep.join(
        p for p in os.environ.get("PATH", "").split(os.pathsep) if not p.endswith("no-ssh-bin")))
    if real_ssh is None:
        pytest.skip("no OpenSSH client here")
    real = tmp_path / "real-bin"
    real.mkdir()
    for name in home_guard.SSH_COMMANDS:
        (real / name).write_text(f'#!/bin/sh\n[ "$1" = -G ] && exec {real_ssh} "$@"\necho REAL {name} "$@"\n')
        (real / name).chmod(0o755)
    environ = {"PATH": f"{real}{os.pathsep}/usr/bin{os.pathsep}/bin", "HOME": str(tmp_path)}
    home_guard.no_ssh(tmp_path, environ)

    def run(*argv, **extra):
        return _subprocess.run(list(argv), capture_output=True, text=True, timeout=30, env={**environ, **extra})
    run.calls = tmp_path / "ssh-calls"
    run.folder = tmp_path
    return run


@pytest.mark.parametrize("argv", [
    ["ssh", "-o", "BatchMode=yes", "root@192.0.2.11", "true"],
    ["ssh", "-J", "127.0.0.1", "root@127.0.0.1"],
    ["ssh", "-o", "ProxyJump=192.0.2.11", "127.0.0.1"],
    ["ssh", "-o", "ProxyCommand=nc 192.0.2.11 22", "localhost"],
    ["ssh", "-L", "2222:192.0.2.11:22", "localhost"],
    ["ssh", "-W", "192.0.2.11:22", "127.0.0.1"],
    ["scp", "a", "127.0.0.1:b"],
    ["sftp", "localhost"],
])
def test_the_stubs_refuse_real_hosts_and_onward_connections(stubs, argv):
    result = stubs(*argv)
    assert result.returncode == 255 and "tests reach no real host" in result.stderr and "REAL" not in result.stdout
    assert argv[0] in stubs.calls.read_text()


def test_the_stubs_refuse_a_local_alias_of_a_real_host(stubs):
    config = stubs.folder / "config"
    config.write_text("Host 127.0.0.1\n  HostName 192.0.2.11\nHost localhost\n  ProxyJump 192.0.2.11\n")
    for host in ("127.0.0.1", "localhost"):
        result = stubs("ssh", "-F", str(config), host)
        assert result.returncode == 255 and "REAL" not in result.stdout


@pytest.mark.parametrize("argv", [
    ["ssh", "-o", "BatchMode=yes", "-p", "2222", "-l", "runner", "-s", "--", "127.0.0.1", "sftp"],
    ["ssh", "runner@127.0.0.1", "true"],
    ["ssh", "-p", "22", "::1", "true"],
    ["ssh", "runner@[::1]", "true"],
    ["ssh", "localhost", "true"],
    ["ssh", "-G", "192.0.2.11"],  # prints the configuration, connects nowhere
])
def test_the_stubs_pass_ssh_to_this_machine_to_the_real_ssh(stubs, argv):
    result = stubs(*argv)
    assert result.returncode == 0, result.stderr
    assert ("hostname 192.0.2.11" in result.stdout) if "-G" in argv else result.stdout.startswith("REAL ssh ")
    assert not stubs.calls.exists()


def test_a_test_with_its_own_ssh_server_opts_in(stubs):
    for argv in (["ssh", "runner@192.0.2.11", "true"], ["scp", "a", "192.0.2.11:b"], ["sftp", "192.0.2.11"]):
        assert stubs(*argv, SANDBOX_AI_TEST_REAL_SSH="1").stdout.startswith(f"REAL {argv[0]} ")
        assert stubs(*argv, SANDBOX_AI_TEST_REAL_SSH="0").returncode == 255


def test_remove_survives_a_folder_it_may_not_open_and_removes_the_rest(tmp_path, monkeypatch):
    """GitHub's runner (0.1.188's CI): rootless Podman's storage below the test HOME had a folder 000
    (overlay/.../work/work); rmtree's os.open failed, the handler called os.open(path) without flags (TypeError), and
    a part whose 1298 tests had all passed exited 1. Now the folder is made writable and removed, and nothing raises."""
    import home_guard
    root = tmp_path / "home"
    (root / "storage" / "work" / "work").mkdir(parents=True)
    (root / "storage" / "file").write_text("x")
    real, failed = os.open, []

    def refusing(name, flags, *args, **kwargs):
        if str(name).endswith("work") and len(failed) < 2:
            failed.append(name)
            raise PermissionError(13, "Permission denied", name)
        return real(name, flags, *args, **kwargs)
    monkeypatch.setattr(os, "open", refusing)
    home_guard.remove(root)
    monkeypatch.undo()
    assert failed, "the stand-in refused an open"
    assert not root.exists()


def test_remove_never_raises_where_something_stays(tmp_path, monkeypatch):
    import home_guard
    root = tmp_path / "home"
    (root / "a").mkdir(parents=True)
    monkeypatch.setattr(home_guard.shutil, "rmtree", lambda *a, **k: (_ for _ in ()).throw(TypeError("not this")))
    home_guard.remove(root)  # a clean-up of a temporary HOME never fails the session
