"""sandbox-ai identities: commit name and address, Git and publish logins under one name (never a token)."""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sandbox_ai_identities_test", ROOT / "sandbox/identities.py")
ids = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ids)


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    return tmp_path


def run(*argv):
    lines = []
    return ids.main(list(argv), out=lines.append), "\n".join(lines)


def test_an_identity_with_git_and_publish_logins(capsys):
    assert run("add", "c0decave", "--name", "dash", "--email", "c0decave@users.noreply.github.com")[0] == 0
    assert run("login", "c0decave", "https://github.com/", "--secret", "GH_C0DECAVE", "--user", "c0decave")[0] == 0
    assert run("login", "c0decave", "http://localhost:3080", "--secret", "GITEA", "--user", "you")[0] == 0
    assert run("publish", "c0decave", "pypi", "--secret", "PYPI_C0DECAVE")[0] == 0
    status, text = run("show", "c0decave")
    assert status == 0 and text.splitlines()[0] == "c0decave: dash <c0decave@users.noreply.github.com>"
    assert "https://github.com" in text and "http://localhost:3080" in text and "upload.pypi.org" in text
    stored = json.loads(ids.path().read_text())["identities"]["c0decave"]
    assert stored["git"][0] == {"url": "https://github.com", "user": "c0decave", "secret": "GH_C0DECAVE"}
    assert oct(ids.path().stat().st_mode & 0o777) == "0o600"
    # The same server again replaces its login; logout and unpublish take one back.
    assert run("login", "c0decave", "https://github.com", "--secret", "GH_OTHER")[0] == 0
    assert [e["secret"] for e in ids.get("c0decave")["git"]] == ["GITEA", "GH_OTHER"]
    assert run("logout", "c0decave", "http://localhost:3080")[0] == 0 and run("unpublish", "c0decave", "pypi")[0] == 0
    assert run("logout", "c0decave", "http://localhost:3080")[0] == 1
    assert "has no login for http://localhost:3080" in capsys.readouterr().err
    assert run("check", "c0decave")[0] == 1  # GH_OTHER is not kept
    ids.module("agent_sandbox").load_secret_store_module().FileStore().set("GH_OTHER", "ghp_x")
    assert run("check") == (0, "c0decave: https://github.com: ok")
    assert run("set", "c0decave", "--email", "")[0] == 0 and "email" not in ids.get("c0decave")
    assert run("remove", "c0decave")[0] == 0 and ids.load() == {}


@pytest.mark.parametrize("argv, text", [
    (["add", "default"], "not 'default'"), (["add", "Bad Name"], "lower-case"),
    (["add", "x", "--email", "not an address"], "email is an address"),
    (["add", "x", "--name", "two\nlines"], "one line"),
    (["login", "nobody", "https://github.com", "--secret", "S"], "no such identity"),
    (["publish", "nobody", "pypi", "--secret", "S"], "no such identity"),
])
def test_refusals(argv, text, capsys):
    assert run(*argv)[0] == 1 and text in capsys.readouterr().err


def test_logins_are_checked_as_everywhere(capsys):
    run("add", "x")
    for url in ("http://git.example.org", "ftp://x", "https://u:p@github.com"):
        assert run("login", "x", url, "--secret", "S")[0] == 1
    assert run("publish", "x", "gitlab", "--secret", "S", "--url", "https://git.example.org/group")[0] == 1
    assert ids.get("x") == {"git": [], "publish": []}


def test_a_damaged_or_open_file_is_refused():
    run("add", "x")
    ids.path().chmod(0o644)
    with pytest.raises(ValueError, match="mode 0600"):
        ids.load()
    ids.path().chmod(0o600)
    ids.path().write_text(json.dumps({"version": 1, "identities": {"default": {}}}))
    with pytest.raises(ValueError, match="not an identity's name"):
        ids.load()
    ids.path().write_text(json.dumps({"version": 1, "identities": {"x": {"git": [{"url": "u"}]}}}))
    with pytest.raises(ValueError, match="each Git login"):
        ids.load()
