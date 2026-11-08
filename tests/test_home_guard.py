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
