"""Credentials for a running sandbox where one looks for them (user report 2026-11-12: "why are credentials in the
net section?"): `sandbox-ai git-logins give|revoke|list --run` and `sandbox-ai secrets give|revoke|list --run`, the
same code path as the older `net allow RUN --git-token-*|--api-key` (which keeps working); `net show` lists both;
messages name the run id and its start's name; a Git login or key that cannot be written, or a network change that
fails, leaves nothing half applied; and the proxy puts a key in as the password of a Basic login (git over HTTPS) for
the key's host only."""
import base64
import importlib.util
import json
from pathlib import Path

import pytest

from test_net import (KEY, TOKEN, git_ready, net, no_login_runtime, proxy_ready, rt, run_net,  # noqa: F401
                      sandbox)

ROOT = Path(__file__).resolve().parents[1]


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / "sandbox" / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gl = load("sandbox_ai_git_logins_given", "git_logins.py")
ss = load("sandbox_ai_secret_store_given", "secret_store.py")


@pytest.fixture(autouse=True)
def shared_net(monkeypatch):
    """git-logins and secrets hand their give/revoke/list to net.py: the test's (its runs, its fake resolver)."""
    monkeypatch.setattr(gl, "_net", net)
    monkeypatch.setattr(ss, "_net", net)
    real = net.main
    monkeypatch.setattr(net, "main", lambda argv, **kw: real(argv, **{"resolver": lambda hosts: {
        h: ["192.0.2.50"] for h in hosts}, **kw}))


def named(sandbox, name="my-start"):
    """The run as a start named NAME (run.json "name"): what messages name besides the run id."""
    record = json.loads((sandbox["run"] / "run.json").read_text())
    (sandbox["run"] / "run.json").write_text(json.dumps({**record, "name": name}))


def call(module, *argv):
    lines = []
    status = module.main(list(argv), out=lines.append)
    return status, "\n".join(lines)


def secrets_store(monkeypatch, values):
    monkeypatch.setattr(net.launcher(), "load_secret_store_module",
                        lambda: type("S", (), {"get": staticmethod(lambda name: values.get(name))}))


def test_git_logins_give_list_and_revoke(sandbox, monkeypatch, capsys):
    tokens = git_ready(sandbox)
    named(sandbox)
    monkeypatch.setenv("GITEA_TOKEN", TOKEN)
    status, text = call(gl, "give", "codex-1234abcd", "http://localhost:3080", "--env", "GITEA_TOKEN",
                        "--user", "alice")
    assert status == 0, capsys.readouterr().err
    assert "codex-1234abcd (my-start): Git login for http://localhost:3080 (user alice)" in text and TOKEN not in text
    assert "start with: --git-token-env http://alice@localhost:3080=GITEA_TOKEN" in text
    assert "allowed during this run" not in text  # brief: the table is net show's
    assert json.loads(tokens.read_text())["logins"] == [{"url": "http://localhost:3080", "user": "alice",
                                                         "token": TOKEN}]
    # Listed by git-logins list --run (by the start's name too) and by net show, never with the token.
    for run in ("codex-1234abcd", "my-start"):
        status, text = call(gl, "list", "--run", run)
        assert status == 0 and "http://localhost:3080" in text and "alice" in text and TOKEN not in text
        assert "codex-1234abcd (my-start)" in text
    status, text = call(gl, "list", "--run", "codex-1234abcd", "--json")
    assert json.loads(text) == {"run": "codex-1234abcd", "name": "my-start",
                                "git": [{"url": "http://localhost:3080", "user": "alice"}]}
    status, lines = run_net(["show"])
    assert "Git logins added during this run" in "\n".join(lines)
    status, text = call(gl, "revoke", "my-start", "http://localhost:3080")
    assert status == 0 and "codex-1234abcd (my-start): Git login for http://localhost:3080 taken back" in text
    assert json.loads(tokens.read_text())["logins"] == []
    assert call(gl, "revoke", "my-start", "http://localhost:3080")[0] == 1
    assert "sandbox-ai git-logins: no Git login for http://localhost:3080" in capsys.readouterr().err


def test_git_logins_give_from_a_secret_and_its_refusals(sandbox, monkeypatch, capsys):
    tokens = git_ready(sandbox)
    secrets_store(monkeypatch, {"GH_PUSH": "ghp_" + "a" * 36})
    status, text = call(gl, "give", "codex-1234abcd", "https://github.com", "--secret", "GH_PUSH")
    assert status == 0, capsys.readouterr().err
    assert json.loads(tokens.read_text())["logins"][0]["user"] == "x-access-token"
    assert "github.com" in sandbox["hosts"].read_text()  # the server allowed as at a start (its preset)
    # Exactly one source; a URL naming another user than --user; a run that does not run.
    with pytest.raises(SystemExit):
        gl.main(["give", "codex-1234abcd", "https://github.com", "--secret", "A", "--env", "B"])
    assert call(gl, "give", "codex-1234abcd", "https://bob@github.com", "--secret", "GH_PUSH", "--user", "eve")[0] == 1
    assert "names the user bob" in capsys.readouterr().err
    assert call(gl, "give", "no-such-run", "https://github.com", "--secret", "GH_PUSH")[0] == 1
    assert "sandbox-ai git-logins: no start named no-such-run" in capsys.readouterr().err


def test_secrets_give_list_and_revoke(sandbox, tmp_path, monkeypatch, capsys):
    keys = proxy_ready(sandbox, tmp_path, monkeypatch)
    named(sandbox)
    secrets_store(monkeypatch, {"OPENROUTER": KEY})
    status, text = call(ss, "give", "my-start", "OPENROUTER", "--api-key", "openrouter.ai", "--as",
                        "OPENROUTER_API_KEY")
    assert status == 0, capsys.readouterr().err
    assert "codex-1234abcd (my-start): API key OPENROUTER_API_KEY for openrouter.ai" in text and KEY not in text
    assert "export OPENROUTER_API_KEY=sandbox-ai-api-key-placeholder-OPENROUTER_API_KEY" in text
    # The proxy gets the key without the secret's name; the run's own list keeps the name, never shown with the key.
    assert json.loads(keys.read_text())["api_keys"] == [{"host": "openrouter.ai", "port": 443,
                                                         "name": "OPENROUTER_API_KEY", "key": KEY,
                                                         "placeholder": "sandbox-ai-api-key-placeholder-"
                                                                        "OPENROUTER_API_KEY"}]
    status, text = call(ss, "list", "--run", "my-start")
    assert status == 0 and "OPENROUTER_API_KEY" in text and "openrouter.ai" in text and "OPENROUTER" in text
    assert KEY not in text
    status, lines = run_net(["show", "--json"])
    shown = json.loads("\n".join(lines))
    assert shown["api_keys"] == [{"name": "OPENROUTER_API_KEY", "host": "openrouter.ai", "port": 443,
                                  "placeholder": "sandbox-ai-api-key-placeholder-OPENROUTER_API_KEY",
                                  "secret": "OPENROUTER"}]
    status, lines = run_net(["show"])
    assert "API keys given during this run" in "\n".join(lines) and KEY not in "\n".join(lines)
    # Taken back by the secret's name (or the variable's).
    status, text = call(ss, "revoke", "my-start", "OPENROUTER")
    assert status == 0 and "API key OPENROUTER_API_KEY taken back" in text
    assert json.loads(keys.read_text())["api_keys"] == []
    assert call(ss, "revoke", "my-start", "OPENROUTER")[0] == 1
    assert "no API key OPENROUTER was given" in capsys.readouterr().err
    # The default variable is the secret's name.
    assert call(ss, "give", "codex-1234abcd", "OPENROUTER", "--api-key", "openrouter.ai")[0] == 0
    assert json.loads(keys.read_text())["api_keys"][0]["name"] == "OPENROUTER"
    assert call(ss, "revoke", "codex-1234abcd", "OPENROUTER")[0] == 0


def test_the_older_net_allow_form_still_gives_both(sandbox, tmp_path, monkeypatch, capsys):
    tokens = git_ready(sandbox)
    keys = proxy_ready(sandbox, tmp_path, monkeypatch, runtime_git_tokens=True)
    secrets_store(monkeypatch, {"OPENROUTER": KEY})
    monkeypatch.setenv("GITEA_TOKEN", TOKEN)
    status, lines = run_net(["allow", "--git-token-env", "http://localhost:3080=GITEA_TOKEN", "--api-key",
                             "openrouter.ai=OR_KEY=OPENROUTER"])
    assert status == 0, capsys.readouterr().err
    assert json.loads(tokens.read_text())["logins"] and json.loads(keys.read_text())["api_keys"]
    text = "\n".join(lines)
    assert "Git logins added during this run" in text and "API keys given during this run" in text


def test_a_network_change_that_fails_puts_the_login_and_the_key_back(sandbox, tmp_path, monkeypatch, capsys):
    """Regression (half-applied state): the login was written after the network change, and a failure in between
    left one without the other. Now the credentials go first and are put back when the network change fails."""
    tokens = git_ready(sandbox)
    keys = proxy_ready(sandbox, tmp_path, monkeypatch, runtime_git_tokens=True)
    secrets_store(monkeypatch, {"OPENROUTER": KEY})
    monkeypatch.setenv("GITEA_TOKEN", TOKEN)
    monkeypatch.setenv("OTHER", "glpat-" + "b" * 20)
    assert run_net(["allow", "--git-token-env", "http://localhost:3080=GITEA_TOKEN"])[0] == 0
    before = json.loads(tokens.read_text())["logins"]
    real_ask = net.ask

    def refusing(directory, request, *args, **kwargs):
        if request.get("op") == "allow":
            raise ValueError("the sandbox refused: the firewall is gone")
        return real_ask(directory, request, *args, **kwargs)
    monkeypatch.setattr(net, "ask", refusing)
    applied = list(sandbox["applied"])
    assert run_net(["allow", "--git-token-env", "https://git.example.org=OTHER", "--api-key",
                    "openrouter.ai=OR_KEY=OPENROUTER"])[0] == 1
    assert "the firewall is gone" in capsys.readouterr().err
    assert json.loads(tokens.read_text())["logins"] == before, "the login given before stays, the new one is gone"
    assert json.loads(keys.read_text())["api_keys"] == [], "the key is taken back from the proxy"
    assert net.given_credentials(json.loads((sandbox["run"] / "run.json").read_text()) | {
        "directory": str(sandbox["run"])})["api_keys"] == []
    assert sandbox["applied"] == applied
    assert "git.example.org" not in sandbox["hosts"].read_text()


def test_a_key_the_run_got_but_its_list_could_not_keep_is_taken_back(sandbox, tmp_path, monkeypatch, capsys):
    """Regression (half-applied state): the supervisor got the key, then the run's own list (api-keys.json) could not
    be written - the command failed, the proxy kept putting the key in, and `secrets revoke` refused it as never
    given. Now the proxy gets the list as it was again."""
    keys = proxy_ready(sandbox, tmp_path, monkeypatch)
    secrets_store(monkeypatch, {"OPENROUTER": KEY})
    real_replace = net.os.replace

    def full_disk(source, target, *args, **kwargs):
        if Path(target).name == "api-keys.json" and Path(target).parent == sandbox["run"]:
            raise OSError(28, "No space left on device")
        return real_replace(source, target, *args, **kwargs)
    monkeypatch.setattr(net.os, "replace", full_disk)
    status, _ = call(ss, "give", "codex-1234abcd", "OPENROUTER", "--api-key", "openrouter.ai")
    assert status == 1 and "No space left on device" in capsys.readouterr().err
    assert json.loads(keys.read_text())["api_keys"] == [], "the proxy puts no key in that the run's list does not name"
    monkeypatch.setattr(net.os, "replace", real_replace)
    assert call(ss, "give", "codex-1234abcd", "OPENROUTER", "--api-key", "openrouter.ai")[0] == 0
    assert call(ss, "revoke", "codex-1234abcd", "OPENROUTER")[0] == 0
    assert json.loads(keys.read_text())["api_keys"] == []


def test_a_login_that_cannot_be_written_changes_no_network(sandbox, monkeypatch, capsys):
    git_ready(sandbox)
    monkeypatch.setenv("OTHER", "glpat-" + "b" * 20)

    def broken(record, logins):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(net, "write_git_logins", broken)
    assert run_net(["allow", "--git-token-env", "https://git.example.org=OTHER"])[0] == 1
    assert "No space left on device" in capsys.readouterr().err
    assert sandbox["applied"] == [] and "git.example.org" not in sandbox["hosts"].read_text()


def test_a_revoke_of_a_login_never_given_changes_nothing(sandbox, monkeypatch, capsys):
    """Regression: the network part was taken back before the missing login was noticed."""
    git_ready(sandbox)
    monkeypatch.setenv("OTHER", "glpat-" + "b" * 20)
    assert run_net(["allow", "--allow-host", "x.example"])[0] == 0
    applied = list(sandbox["applied"])
    assert run_net(["revoke", "--allow-host", "x.example", "--git-token-env", "https://git.example.org=X"])[0] == 1
    assert "no Git login for https://git.example.org" in capsys.readouterr().err
    assert sandbox["applied"] == applied and "x.example" in sandbox["hosts"].read_text()


PLACEHOLDER = "sandbox-ai-api-key-placeholder-GH_PUSH"
ENTRY = {"host": "github.com", "port": 443, "name": "GH_PUSH", "placeholder": PLACEHOLDER, "key": "ghp_REAL"}


def basic(user, password):
    return b"Authorization: Basic " + base64.b64encode(f"{user}:{password}".encode())


def head_with(line, host="github.com"):
    return b"GET /o/r.git/info/refs?service=git-receive-pack HTTP/1.1\r\nHost: " + host.encode() + b"\r\n" + \
        line + b"\r\n\r\n"


def sent_basic(head):
    line = next(l for l in head.split(b"\r\n") if l.lower().startswith(b"authorization:"))
    return base64.b64decode(line.split()[-1])


def test_the_proxy_puts_a_key_in_as_the_password_of_a_basic_login():
    """git over HTTPS sends the placeholder as its password: the key goes in there, for the key's host only."""
    head = head_with(basic("x-access-token", PLACEHOLDER))
    swapped, names = rt.api_key_head([ENTRY], "https", "github.com", 443, head)
    assert names == ["GH_PUSH"] and sent_basic(swapped) == b"x-access-token:ghp_REAL"
    # Another host, another port, plain http: the placeholder stays.
    for scheme, origin, port in (("https", "evil.example", 443), ("https", "github.com", 8443),
                                 ("http", "github.com", 443)):
        assert rt.api_key_head([ENTRY], scheme, origin, port, head) == (head, [])
    # Only the password part: a placeholder as the user name stays (it would land in the server's logs).
    as_user = head_with(basic(PLACEHOLDER, "x"))
    assert rt.api_key_head([ENTRY], "https", "github.com", 443, as_user) == (as_user, [])
    # A Basic value that is no user:password, or not base64: unchanged.
    for value in (b"Authorization: Basic " + base64.b64encode(PLACEHOLDER.encode()), b"Authorization: Basic !!"):
        assert rt.api_key_head([ENTRY], "https", "github.com", 443, head_with(value)) == (head_with(value), [])
    # A publish login's Basic (PyPI's __token__:TOKEN) as before.
    pypi = {"host": "upload.pypi.org", "port": 443, "name": "publish:pypi", "basic": True,
            "placeholder": "sandbox-ai-publish-placeholder-pypi", "key": "pypi-REAL"}
    head = head_with(basic("__token__", "sandbox-ai-publish-placeholder-pypi"), "upload.pypi.org")
    assert sent_basic(rt.api_key_head([pypi], "https", "upload.pypi.org", 443, head)[0]) == b"__token__:pypi-REAL"
