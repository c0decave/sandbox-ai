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
    assert "OPENAI_KEY  (file)" in out and "sk-test-value" not in out
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
