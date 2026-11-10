"""sandbox-ai secrets: keyring (secret-tool) or private files; the launcher's --secret-env and
--git-token-secret."""
import importlib.util
import io
import os
from pathlib import Path
import stat
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("secret_store_tests", ROOT / "sandbox/secret_store.py")
ss = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ss)
LSPEC = importlib.util.spec_from_file_location("launcher_for_secrets", ROOT / "sandbox/agent_sandbox.py")
s = importlib.util.module_from_spec(LSPEC)
LSPEC.loader.exec_module(s)


@pytest.fixture(autouse=True)
def private(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")


def test_file_store_keeps_values_private(tmp_path):
    store = ss.FileStore()
    store.set("BRAVE_API_KEY", "value-1")
    path = tmp_path / "data/sandbox-ai/secrets/BRAVE_API_KEY"
    assert store.get("BRAVE_API_KEY") == "value-1" and store.names() == ["BRAVE_API_KEY"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    path.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        store.get("BRAVE_API_KEY")
    path.unlink()
    path.symlink_to(tmp_path / "elsewhere")
    assert store.get("BRAVE_API_KEY") is None  # never followed
    for bad in ("../x", "1ABC", "a b", ""):
        with pytest.raises(ValueError):
            store.set(bad, "v")
    assert store.delete("NOPE") is False


def test_keyring_store_through_secret_tool():
    calls = []

    def run(argv, input=None, **kwargs):
        calls.append((argv, input))
        if argv[1] == "lookup":
            return subprocess.CompletedProcess(argv, 0, "token-value\n", "")
        if argv[1] == "search":
            return subprocess.CompletedProcess(argv, 0, "", "[/1]\nlabel = x\nattribute.name = GH_PAT\nattribute.service = sandbox-ai\n")
        return subprocess.CompletedProcess(argv, 0, "", "")
    store = ss.KeyringStore(run)
    store.set("GH_PAT", "token-value")
    assert calls[-1] == (["secret-tool", "store", "--label=sandbox-ai GH_PAT", "service", "sandbox-ai", "name", "GH_PAT"],
                         "token-value")  # the value goes through stdin, never the command line
    assert store.get("GH_PAT") == "token-value" and store.names() == ["GH_PAT"]


def test_cli(capsys):
    assert ss.main(["set", "OPENAI_KEY"], stdin=io.StringIO("sk-test-value\n")) == 0
    assert ss.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "NAME        STORE\nOPENAI_KEY  file\n" in out and "sk-test-value" not in out
    assert ss.main(["set", "BAD"], stdin=io.StringIO("\n")) == 1
    assert ss.main(["delete", "OPENAI_KEY"]) == 0 and ss.get("OPENAI_KEY") is None
    assert ss.main(["backend"]) == 0 and capsys.readouterr().out.strip().endswith("file")


def test_launcher_secret_env_and_git_token_secret(tmp_path):
    ss.FileStore().set("BRAVE_API_KEY", "brave-value")
    ss.FileStore().set("GH_PAT", "ghp_" + "Z" * 36)
    a = s.parse_args(["codex", "--secret-env", "BRAVE_API_KEY", "-se", "SEARCH_KEY=BRAVE_API_KEY", "--no-proxy",
                      "--privacy-check", "off", "--git-token-secret", "https://github.com=GH_PAT"])
    env = s.agent_environment(a)
    assert env["BRAVE_API_KEY"] == "brave-value" and env["SEARCH_KEY"] == "brave-value"
    assert env["GH_TOKEN"] == "ghp_" + "Z" * 36
    with pytest.raises(ValueError, match="no secret MISSING"):
        s.secret_environment(s.parse_args(["codex", "--secret-env", "MISSING"]))
    with pytest.raises(ValueError, match="--secret-env expects"):
        s.validate(s.parse_args(["codex", "--workspace", str(tmp_path / "w"), "--secret-env", "A=b c"]))
    with pytest.raises(ValueError, match="Sandbox-owned"):
        s.secret_environment(s.parse_args(["codex", "--secret-env", "PATH=BRAVE_API_KEY"]))


@pytest.mark.parametrize("returncode, stderr, usable", [
    (0, "", True),                       # found something
    (1, "", True),                       # found nothing: a working keyring
    (1, "secret-tool: The name is not activatable\n", False),   # regression: no Secret Service on the bus
    (1, "secret-tool: The name org.freedesktop.secrets was not provided by any .service files\n", False),
    (1, "secret-tool: Cannot autolaunch D-Bus without X11 $DISPLAY\n", False),
    (1, "secret-tool: Cannot create an item in a locked collection\n", False),
    (2, "", False),
])
def test_the_keyring_counts_as_usable_only_when_secret_tool_says_nothing_wrong(returncode, stderr, usable, monkeypatch):
    """Regression: "The name is not activatable" passed the probe, so sandbox-ai secrets set failed instead of
    keeping the secret in the private files."""
    monkeypatch.setattr(ss.shutil, "which", lambda name: "/usr/bin/secret-tool")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")
    run = lambda *args, **kwargs: subprocess.CompletedProcess(args, returncode, "", stderr)  # noqa: E731
    assert ss.keyring_usable(run) is usable


def test_without_a_usable_keyring_set_keeps_the_secret_in_the_files(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.delenv("SANDBOX_AI_SECRETS", raising=False)
    monkeypatch.setattr(ss, "keyring_usable", lambda run=None: False)
    lines = []
    assert ss.main(["set", "GH_PUSH"], stdin=io.StringIO("ghp_value\n"), out=lines.append) == 0
    assert lines == ["GH_PUSH kept (file)"] and ss.get("GH_PUSH") == "ghp_value"


def test_a_keyring_that_refuses_says_how_to_keep_it_in_the_files(monkeypatch, capsys):
    refusing = ss.KeyringStore(run=lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "secret-tool: locked\n"))
    monkeypatch.setattr(ss, "store", lambda: refusing)
    assert ss.main(["set", "GH_PUSH"], stdin=io.StringIO("ghp_value\n")) == 1
    assert "SANDBOX_AI_SECRETS=file sandbox-ai secrets set GH_PUSH" in capsys.readouterr().err


def test_set_refuses_several_lines_and_an_unset_variable(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.delenv("NO_SUCH_VARIABLE", raising=False)
    out = []
    assert ss.main(["set", "MULTI"], stdin=io.StringIO("a\nb\n"), out=out.append) == 1
    assert "it had several" in capsys.readouterr().err and ss.FileStore().get("MULTI") is None, "never cut"
    assert ss.main(["set", "ONE"], stdin=io.StringIO("value\n"), out=out.append) == 0
    assert ss.FileStore().get("ONE") == "value"
    assert ss.main(["set", "E", "--from-env", "NO_SUCH_VARIABLE"], out=out.append) == 1
    assert "NO_SUCH_VARIABLE is not set" in capsys.readouterr().err
