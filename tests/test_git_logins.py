"""sandbox-ai git-logins: the list of the proxy's Git logins (servers, users, secret names; never a token)."""
import importlib.util
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sandbox_ai_git_logins_test", ROOT / "sandbox/git_logins.py")
g = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(g)
TOKEN = "gitea_0123456789abcdef"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    return tmp_path


def run(*argv):
    lines = []
    return g.main(list(argv), out=lines.append), "\n".join(lines)


def test_add_list_check_and_remove(capsys):
    status, text = run("add", "http://localhost:3000/", "--secret", "GITEA", "--user", "alice")
    assert status == 0 and "http://localhost:3000: login alice with the secret GITEA" in text
    assert "sandbox-ai secrets set GITEA" in text  # not kept yet: said
    assert g.path().stat().st_mode & 0o777 == 0o600
    assert run("check")[0] == 1 and "the secret GITEA is missing" in run("check")[1]
    g.launcher().load_secret_store_module().store().set("GITEA", TOKEN)
    assert run("check") == (0, "http://localhost:3000: ok")
    pairs, notes = g.credentials()
    assert [(c["url"], c["user"], c["loopback"], t) for c, t in pairs] == [("http://localhost:3000", "alice", True, TOKEN)]
    assert notes == [] and TOKEN not in g.path().read_text()  # the list holds the secret's name only
    # The same server again replaces its login; list shows it once.
    run("add", "http://localhost:3000", "--secret", "GITEA2")
    status, text = run("list", "--json")
    assert [e["secret"] for e in json.loads(text)] == ["GITEA2"]
    assert run("remove", "http://localhost:3000")[0] == 0 and run("list")[1].startswith("no Git logins")
    assert run("remove", "http://localhost:3000")[0] == 1
    assert "no login for http://localhost:3000" in capsys.readouterr().err


def test_refusals(capsys, home):
    for argv, text in ((["add", "http://git.example.org", "--secret", "X"], "plain http would send the token"),
                       (["add", "https://git.example.org/path", "--secret", "X"], "without a path"),
                       (["add", "https://git.example.org", "--secret", "bad name"], "--secret is a secret's name"),
                       (["add", "https://git.example.org", "--secret", "X", "--user", "a b"], "--secret is a secret's name")):
        assert run(*argv)[0] == 1, argv
        assert text in capsys.readouterr().err, argv
    g.path().parent.mkdir(parents=True, exist_ok=True)
    g.path().write_text("{}")
    g.path().chmod(0o644)
    assert run("list")[0] == 1 and "mode 0600" in capsys.readouterr().err  # others could read or change it
    g.path().chmod(0o600)
    assert run("list")[0] == 1 and "version 1" in capsys.readouterr().err
    g.path().unlink()
    os.symlink(home / "elsewhere.json", g.path())
    assert run("list")[0] == 1 and "regular file" in capsys.readouterr().err


def test_a_start_option_skips_its_server_and_a_missing_secret_is_named():
    g.save([{"url": "https://github.com", "user": "x-access-token", "secret": "GH"},
            {"url": "http://localhost:3000", "user": "ch", "secret": "NOT_KEPT"}])
    g.launcher().load_secret_store_module().store().set("GH", "ghp_" + "a" * 36)
    pairs, notes = g.credentials(skip={"https://github.com"})
    assert pairs == [] and notes == ["git-logins: http://localhost:3000 left out: no usable secret NOT_KEPT "
                                     "(sandbox-ai secrets set NOT_KEPT)"]
    assert [c["url"] for c, _ in g.credentials()[0]] == ["https://github.com"]
