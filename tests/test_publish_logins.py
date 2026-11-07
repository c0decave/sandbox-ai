"""sandbox-ai publish-logins: registry and forge logins for --publish (kinds, hosts, secret names; never a token)."""
import base64
import importlib.util
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sandbox_ai_publish_logins_test", ROOT / "sandbox/publish_logins.py")
pl = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pl)
RT_SPEC = importlib.util.spec_from_file_location("sandbox_ai_runtime_publish_test", ROOT / "sandbox/runtime.py")
rt = importlib.util.module_from_spec(RT_SPEC)
RT_SPEC.loader.exec_module(rt)


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    return tmp_path


def run(*argv):
    lines = []
    return pl.main(list(argv), out=lines.append), "\n".join(lines)


def test_add_list_check_and_remove(capsys):
    status, text = run("add", "pypi", "--secret", "PYPI_TOKEN")
    assert status == 0 and "upload.pypi.org with the secret PYPI_TOKEN" in text and "is not kept yet" in text
    assert run("add", "gitlab", "--secret", "GL", "--url", "https://git.example.org/")[0] == 0
    assert oct(pl.path().stat().st_mode & 0o777) == "0o600"
    assert [(e["kind"], e["host"]) for e in pl.load()] == [("pypi", "upload.pypi.org"), ("gitlab", "git.example.org")]
    assert "TOKEN" not in pl.path().read_text().replace("PYPI_TOKEN", "")  # names only
    assert run("check")[0] == 1  # the secrets are not kept
    pl.launcher().load_secret_store_module().FileStore().set("PYPI_TOKEN", "pypi-REAL")
    assert run("check", "pypi") == (0, "pypi: ok")
    assert run("add", "pypi", "--secret", "OTHER")[0] == 0 and [e["secret"] for e in pl.load()] == ["GL", "OTHER"]
    assert run("remove", "gitlab")[0] == 0 and run("remove", "gitlab")[0] == 1
    assert "no login for gitlab" in capsys.readouterr().err
    status, text = run("list", "--json")
    assert status == 0 and json.loads(text) == [{"kind": "pypi", "host": "upload.pypi.org", "secret": "OTHER"}]
    assert run("kinds")[0] == 0 and "crates" in run("kinds")[1]


@pytest.mark.parametrize("url", ["http://git.example.org", "https://git.example.org:8443", "https://u:p@git.example.org",
                                 "https://git.example.org/group", "https://192.0.2.1", "https://localhost", "ftp://x.org"])
def test_a_server_url_is_an_https_host_name_and_nothing_else(url, capsys):
    assert run("add", "gitlab", "--secret", "GL", "--url", url)[0] == 1
    assert "--url is https://HOST" in capsys.readouterr().err and pl.load() == []


def test_a_damaged_or_open_list_is_refused(capsys):
    pl.path().parent.mkdir(parents=True)
    pl.path().write_text(json.dumps({"version": 1, "logins": [{"kind": "pypi", "host": "upload.pypi.org",
                                                               "secret": "X"}]}))
    pl.path().chmod(0o644)
    with pytest.raises(ValueError, match="mode 0600"):
        pl.load()
    pl.path().chmod(0o600)
    pl.path().write_text(json.dumps({"version": 1, "logins": [{"kind": "docker", "host": "x.org", "secret": "X"}]}))
    with pytest.raises(ValueError, match="each login is"):
        pl.load()


def logins():
    return [{"kind": kind, "host": pl.KINDS[kind]["hosts"][0], "secret": kind.upper()} for kind in pl.KINDS]


def test_every_kind_gives_placeholders_entries_and_rules_that_compile():
    kinds = list(pl.KINDS)
    store = {kind.upper(): f"{kind}-REAL-token" for kind in kinds}
    env = pl.environment(kinds, logins())
    assert not any("REAL" in value for value in env.values())
    entries = pl.proxy_entries(kinds, store.get, logins())
    assert {e["host"] for e in entries} == {h for spec in pl.KINDS.values() for h in spec["hosts"]}
    assert all(e["basic"] for e in entries if e["name"] in ("publish:pypi", "publish:testpypi"))
    ruleset = rt.Ruleset.validated(pl.rules(kinds, logins()), "--publish")  # compiles as any ruleset does
    assert ruleset["http"] and all(rule["decision"] == "ask" for rule in ruleset["http"])
    assert {host for rule in ruleset["http"] for host in rule["host"]} == {e["host"] for e in entries}
    assert pl.npmrc(["npm"], logins()) == "//registry.npmjs.org/:_authToken=${NPM_TOKEN}\n"
    assert pl.npmrc(["pypi"], logins()) is None


def test_a_server_of_your_own_has_its_host_only():
    own = [{"kind": "github", "host": "github.example.org", "secret": "GHE"}]
    assert pl.plan(["github"], own)[0]["hosts"] == ["github.example.org"]
    hosts = {rule["host"] for rule in pl.rules(["github"], own)["http"]}
    assert hosts == {"github.example.org"}  # no uploads.github.com for an Enterprise server


def pypi_upload(placeholder):
    return (b"POST /legacy/ HTTP/1.1\r\nHost: upload.pypi.org\r\nAuthorization: Basic "
            + base64.b64encode(b"__token__:" + placeholder.encode()) + b"\r\n\r\n")


def test_the_proxy_puts_the_pypi_token_inside_the_basic_login_for_its_host_only():
    entries = pl.proxy_entries(["pypi", "testpypi"], {"PYPI": "pypi-REAL", "TESTPYPI": "test-REAL"}.get, logins())
    head = pypi_upload(pl.placeholder("pypi"))
    changed, added = rt.api_key_head(entries, "https", "upload.pypi.org", 443, head)
    login = base64.b64decode(changed.split(b"Authorization: Basic ")[1].split(b"\r\n")[0])
    assert login == b"__token__:pypi-REAL" and added == ["publish:pypi"]
    changed, _ = rt.api_key_head(entries, "https", "test.pypi.org", 443, head)  # one placeholder, each its own token
    assert base64.b64decode(changed.split(b"Basic ")[1].split(b"\r\n")[0]) == b"__token__:test-REAL"
    assert rt.api_key_head(entries, "https", "evil.example", 443, head) == (head, [])
    assert rt.api_key_head(entries, "http", "upload.pypi.org", 443, head) == (head, [])


def test_the_other_kinds_go_in_their_tools_headers():
    store = {kind.upper(): f"{kind}-REAL" for kind in pl.KINDS}
    entries = pl.proxy_entries(list(pl.KINDS), store.get, logins())
    for kind, host, line in (("npm", "registry.npmjs.org", "Authorization: Bearer {p}"),
                             ("crates", "crates.io", "Authorization: {p}"),
                             ("gitlab", "gitlab.com", "PRIVATE-TOKEN: {p}"),
                             ("github", "api.github.com", "Authorization: token {p}"),
                             ("github", "uploads.github.com", "Authorization: Bearer {p}")):
        head = f"PUT /x HTTP/1.1\r\nHost: {host}\r\n{line.format(p=pl.placeholder(kind))}\r\n\r\n".encode()
        changed, added = rt.api_key_head(entries, "https", host, 443, head)
        assert f"{kind}-REAL".encode() in changed and added == [f"publish:{kind}"], kind
