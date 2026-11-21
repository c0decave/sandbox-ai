"""Credentials are edited in place (user 2026-11-19: "wieso kann man bei 'Zugänge' eingetragene Creds nicht
editieren??"): `git-logins edit`, `publish-logins edit`, `secrets rename`, `vpn set` - through the real entry point,
./sandbox-ai as a subprocess of a user's host (tests/live_fakes.Host; the start that runs is the fake Podman's).

A secret's value is never shown by any of them; a rename is carried through everything sandbox-ai keeps (the lists,
the identities, VPN profiles, trusted project files, kept starts) and what still names the old one is said."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from live_fakes import Host  # noqa: E402

VALUE = "ghp_" + "Ed1tV4lu3" * 3 + "secretXY"
pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the fakes are Linux's")


def load(name):
    spec = importlib.util.spec_from_file_location(f"sandbox_ai_{name}_edit_test", ROOT / "sandbox" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def host(tmp_path):
    made = Host(tmp_path / "h")
    made.env.update(SANDBOX_AI_SECRETS="file", SANDBOX_AI_SECRETS_PLAIN="1")
    return made


def keep(host, name, value=VALUE):
    done = subprocess.run(host.command("secrets", "set", name), input=value + "\n", text=True, capture_output=True,
                          env=host.env, cwd=host.workspace, timeout=60)
    assert done.returncode == 0, done.stderr
    assert value not in done.stdout + done.stderr


def ok(done):
    assert done.returncode == 0, done.stderr + done.stdout
    assert VALUE not in done.stdout + done.stderr, "a value was printed"
    return done.stdout


def logins(host, command="git-logins"):
    return json.loads(ok(host.run(command, "list", "--json")))


def trusted_project(host, options):
    """HOST's project file with OPTIONS, trusted as written (as `sandbox-ai wizard trust` records it)."""
    path = host.workspace / ".sandbox-ai.json"
    path.write_text(json.dumps({"version": 1, "agent": "claude", "options": options}, indent=2) + "\n")
    done = subprocess.run(host.command("wizard", "trust", str(path)), input="y\n", text=True, capture_output=True,
                          env=host.env, cwd=host.workspace, timeout=60)
    assert done.returncode == 0, done.stderr + done.stdout
    return path


# --- git-logins edit --------------------------------------------------------------------------------------------------

def test_git_logins_edit_changes_the_user_the_secret_and_the_server_in_place(host):
    keep(host, "GT")
    ok(host.run("git-logins", "add", "https://git.example.org", "--secret", "GT", "--user", "alice"))
    ok(host.run("git-logins", "add", "https://other.example.org", "--secret", "GT"))
    said = ok(host.run("git-logins", "edit", "https://git.example.org", "--user", "bob"))
    assert "https://git.example.org: login bob with the secret GT" in said
    assert logins(host)[0] == {"url": "https://git.example.org", "user": "bob", "secret": "GT"}, "in place: its row"
    said = ok(host.run("git-logins", "edit", "https://git.example.org", "--secret", "NEW_ONE"))
    assert "note: the secret NEW_ONE is not kept yet: sandbox-ai secrets set NEW_ONE" in said
    said = ok(host.run("git-logins", "edit", "https://git.example.org", "--url", "https://code.example.org"))
    assert "(was https://git.example.org)" in said
    assert logins(host) == [{"url": "https://code.example.org", "user": "bob", "secret": "NEW_ONE"},
                            {"url": "https://other.example.org", "user": "x-access-token", "secret": "GT"}]
    # the same values again: said, nothing written
    assert "unchanged" in ok(host.run("git-logins", "edit", "https://code.example.org", "--user", "bob"))


@pytest.mark.parametrize("arguments, reason", [
    (["https://nope.example.org", "--user", "bob"], "no login for https://nope.example.org"),
    (["https://git.example.org"], "nothing to change: give --url NEW, --user USER or --secret NAME"),
    (["https://git.example.org", "--user", "not a user"], "--user is letters, digits, ._-"),
    (["https://git.example.org", "--secret", "9bad"], "--secret is a secret's name"),
    (["https://git.example.org", "--url", "ftp://x"], "http"),
    (["https://git.example.org", "--url", "https://other.example.org"],
     "a login for https://other.example.org is there already"),
])
def test_git_logins_edit_refuses_what_it_cannot_do_and_changes_nothing(host, arguments, reason):
    ok(host.run("git-logins", "add", "https://git.example.org", "--secret", "GT"))
    ok(host.run("git-logins", "add", "https://other.example.org", "--secret", "GT"))
    before = logins(host)
    done = host.run("git-logins", "edit", *arguments)
    assert done.returncode == 1 and reason in done.stderr, done.stderr
    assert done.stderr.startswith("sandbox-ai git-logins: ")
    assert logins(host) == before


def test_git_logins_edit_says_what_still_names_the_old_server(host):
    """A server's address changed: an identity's login, a trusted project file's and a kept start's --git-token-*
    option for the old address are named (they are other logins for that server: said, not changed)."""
    keep(host, "GT")
    ok(host.run("git-logins", "add", "https://git.example.org", "--secret", "GT"))
    ok(host.run("identities", "add", "work", "--name", "W", "--email", "w@example.org"))
    ok(host.run("identities", "login", "work", "https://git.example.org", "--secret", "GT"))
    project = trusted_project(host, ["--git-token-secret", "https://alice@git.example.org=GT", "--allow-host",
                                     "git.example.org"])
    said = ok(host.run("git-logins", "edit", "https://git.example.org", "--url", "https://code.example.org"))
    notes = [line for line in said.splitlines() if line.startswith("note: still names https://git.example.org: ")]
    assert any("the identity work" in line for line in notes), said
    assert any(str(project) in line and "--git-token-secret https://alice@git.example.org=GT" in line
               for line in notes), said


# --- publish-logins edit ----------------------------------------------------------------------------------------------

def test_publish_logins_edit_changes_the_secret_and_the_server(host):
    keep(host, "PYPI_TOKEN")
    ok(host.run("publish-logins", "add", "gitlab", "--secret", "PYPI_TOKEN"))
    said = ok(host.run("publish-logins", "edit", "gitlab", "--url", "https://gitlab.example.org", "--secret", "GL"))
    assert "gitlab: gitlab.example.org with the secret GL" in said
    assert "note: the secret GL is not kept yet" in said
    assert "restarted" in said, "a running sandbox keeps the login it was started with: said"
    assert logins(host, "publish-logins") == [{"kind": "gitlab", "host": "gitlab.example.org", "secret": "GL"}]
    ok(host.run("publish-logins", "edit", "gitlab", "--url", "default"))
    assert logins(host, "publish-logins")[0]["host"] == "gitlab.com"
    for arguments, reason in ((["npm", "--secret", "X"], "no login for npm"),
                              (["gitlab"], "nothing to change"),
                              (["gitlab", "--url", "http://plain.example.org"], "--url is https://HOST")):
        done = host.run("publish-logins", "edit", *arguments)
        assert done.returncode == 1 and reason in done.stderr, done.stderr


# --- secrets rename ---------------------------------------------------------------------------------------------------

def test_secrets_rename_moves_the_value_and_every_reference_sandbox_ai_keeps(host, tmp_path):
    keep(host, "OLD_TOKEN")
    ok(host.run("git-logins", "add", "https://git.example.org", "--secret", "OLD_TOKEN"))
    ok(host.run("publish-logins", "add", "pypi", "--secret", "OLD_TOKEN"))
    ok(host.run("identities", "add", "work", "--name", "W", "--email", "w@example.org"))
    ok(host.run("identities", "login", "work", "https://git.example.org", "--secret", "OLD_TOKEN"))
    ok(host.run("identities", "publish", "work", "npm", "--secret", "OLD_TOKEN"))
    options = ["--git-token-secret", "https://git.example.org=OLD_TOKEN", "--secret-env", "OLD_TOKEN", "--secret-env",
               "API=OLD_TOKEN", "--api-key=api.example.org=KEY=OLD_TOKEN", "-ky", "x.example.org=OLD_TOKEN",
               "--secret-env", "OTHER", "--mcp-env", "srv:TOKEN@OLD_TOKEN", "--env", "OLD_TOKEN"]
    project = trusted_project(host, options)
    # a kept start whose options name the secret (as a session keeps them for its resume)
    session = host.home / ".local/state/sandbox-ai/sessions/kept1"
    session.mkdir(parents=True)
    (session / "session.json").write_text(json.dumps({"agent": "claude", "name": "kept1", "options": [
        "--secret-env", "OLD_TOKEN", "--allow-host", "example.org"], "agent_args": []}))
    # an LLM endpoint names it too: said, not changed
    llm = host.home / ".config/sandbox-ai/llm.json"
    llm.write_text(json.dumps({"version": 1, "endpoints": {"local": {"kind": "openai", "url": "https://llm.example.org",
                                                                      "api_key_secret": "OLD_TOKEN"}}}))
    llm.chmod(0o600)
    said = ok(host.run("secrets", "rename", "OLD_TOKEN", "NEW_TOKEN"))
    assert "OLD_TOKEN is now NEW_TOKEN (file)" in said
    store = host.home / ".local/share/sandbox-ai/secrets"
    assert not (store / "OLD_TOKEN").exists() and (store / "NEW_TOKEN").read_text().strip() == VALUE
    assert logins(host)[0]["secret"] == "NEW_TOKEN" and logins(host, "publish-logins")[0]["secret"] == "NEW_TOKEN"
    identity = json.loads(ok(host.run("identities", "show", "work", "--json")))
    assert [e["secret"] for e in identity["git"] + identity["publish"]] == ["NEW_TOKEN", "NEW_TOKEN"]
    written = json.loads(project.read_text())["options"]
    assert written == ["--git-token-secret", "https://git.example.org=NEW_TOKEN", "--secret-env",
                       "OLD_TOKEN=NEW_TOKEN", "--secret-env", "API=NEW_TOKEN",
                       "--api-key=api.example.org=KEY=NEW_TOKEN", "-ky", "x.example.org=OLD_TOKEN=NEW_TOKEN",
                       "--secret-env", "OTHER", "--mcp-env", "srv:TOKEN@NEW_TOKEN", "--env", "OLD_TOKEN"], \
        "the variable the agent sees keeps its name; what is no reference stays"
    # the project file is trusted as written: its next start reads it
    trusted = json.loads((host.home / ".config/sandbox-ai/trusted.json").read_text())
    launcher = load("agent_sandbox")
    assert trusted[str(launcher.config_key(project))] == hashlib.sha256(project.read_bytes()).hexdigest()
    assert json.loads((session / "session.json").read_text())["options"] == [
        "--secret-env", "OLD_TOKEN=NEW_TOKEN", "--allow-host", "example.org"]
    for what in ("the Git login for https://git.example.org", "the publish login pypi", "the identity work",
                 str(project), "the start kept1"):
        assert any(line.startswith("now names NEW_TOKEN: ") and what in line for line in said.splitlines()), (what, said)
    assert "note: still names OLD_TOKEN: the LLM endpoint local" in said
    assert json.loads(llm.read_text())["endpoints"]["local"]["api_key_secret"] == "OLD_TOKEN"


def test_secrets_rename_leaves_a_project_file_changed_since_it_was_trusted(host):
    """A project file an agent may have changed is never rewritten and never trusted along: it is named."""
    keep(host, "OLD_TOKEN")
    project = trusted_project(host, ["--secret-env", "OLD_TOKEN"])
    changed = json.dumps({"version": 1, "agent": "claude", "options": ["--secret-env", "OLD_TOKEN", "--allow",
                                                                        "0.0.0.0/0"]})
    project.write_text(changed)
    said = ok(host.run("secrets", "rename", "OLD_TOKEN", "NEW_TOKEN"))
    assert project.read_text() == changed
    assert f"note: still names OLD_TOKEN: {project}: changed since you trusted it, not touched" in said


@pytest.mark.parametrize("arguments, reason", [
    (["MISSING", "NEW_TOKEN"], "no secret MISSING"),
    (["OLD_TOKEN", "TAKEN"], "a secret TAKEN is kept already"),
    (["OLD_TOKEN", "OLD_TOKEN"], "OLD and NEW are the same name"),
    (["OLD_TOKEN", "9bad"], "name"),
])
def test_secrets_rename_refuses_and_keeps_both_secrets(host, arguments, reason):
    keep(host, "OLD_TOKEN")
    keep(host, "TAKEN", "another-value-0123456789")
    done = host.run("secrets", "rename", *arguments)
    assert done.returncode == 1 and reason in done.stderr, done.stderr
    assert VALUE not in done.stdout + done.stderr
    store = host.home / ".local/share/sandbox-ai/secrets"
    assert (store / "OLD_TOKEN").read_text().strip() == VALUE
    assert (store / "TAKEN").read_text().strip() == "another-value-0123456789"


def test_secrets_rename_keeps_an_encrypted_secret_encrypted(tmp_path):
    """A store with a primary password: the value is moved sealed under its new name (a sealed file is bound to its
    name), and read back with the new one."""
    host = Host(tmp_path / "h")
    host.env.update(SANDBOX_AI_SECRETS="file")
    password = "correct horse battery\n"
    done = subprocess.run(host.command("secrets", "password", "set", "--password-stdin"), input=password, text=True,
                          capture_output=True, env=host.env, cwd=host.workspace, timeout=120)
    assert done.returncode == 0, done.stderr
    try:
        keep(host, "OLD_TOKEN")
        ok(host.run("git-logins", "add", "https://git.example.org", "--secret", "OLD_TOKEN"))
        ok(host.run("secrets", "rename", "OLD_TOKEN", "NEW_TOKEN"))
        store = host.home / ".local/share/sandbox-ai/secrets"
        assert not (store / "OLD_TOKEN").exists()
        assert VALUE.encode() not in (store / "NEW_TOKEN").read_bytes(), "kept encrypted"
        assert "https://git.example.org: ok" in ok(host.run("git-logins", "check"))
    finally:
        host.run("secrets", "lock")


# --- vpn set ----------------------------------------------------------------------------------------------------------

def test_vpn_set_changes_a_profile_in_place_and_keeps_its_keys(host):
    keep(host, "TS_KEY", "tskey-auth-0123456789abcdef")
    ok(host.run("vpn", "add", "tail", "--tailscale", "--auth-key-secret", "TS_KEY"))
    said = ok(host.run("vpn", "set", "tail", "--exit-node", "exit1", "--hostname", "box-7", "--provider", "office"))
    assert "exit node exit1" in said and "node name box-7" in said and "provider office" in said
    shown = json.loads(ok(host.run("vpn", "show", "tail", "--json")))
    assert shown["settings"] == {"provider": "office", "auth-key-secret": "TS_KEY", "exit-node": "exit1",
                                 "login-server": None, "hostname": "box-7"}
    assert shown["tunnel"] == "exit node exit1"
    ok(host.run("vpn", "set", "tail", "--exit-node", "none", "--auth-key-secret", "none"))
    shown = json.loads(ok(host.run("vpn", "show", "tail", "--json")))["settings"]
    assert shown["exit-node"] is None and shown["auth-key-secret"] is None
    for arguments, reason in ((["tail"], "nothing to set"),
                              (["tail", "--auth-secret", "X"], "--auth-secret is an OpenVPN profile's"),
                              (["tail", "--exit-country", "de"], "--exit-country is a Tor profile's"),
                              (["tail", "--account-secret", "X"], "no Mullvad profile"),
                              (["tail", "--login-server", "http://plain"], "--login-server expects an https URL"),
                              (["tail", "--bootstrap-attempts", "2"], "the bootstrap settings are a Tor profile's"),
                              (["nope", "--provider", "x"], "no VPN profile nope")):
        done = host.run("vpn", "set", *arguments)
        assert done.returncode == 1 and reason in done.stderr, (arguments, done.stderr)
    assert json.loads(ok(host.run("vpn", "show", "tail", "--json")))["settings"] == shown, "a refusal changes nothing"


def test_secrets_rename_is_carried_into_a_vpn_profile(host):
    keep(host, "TS_KEY", "tskey-auth-0123456789abcdef")
    ok(host.run("vpn", "add", "tail", "--tailscale", "--auth-key-secret", "TS_KEY"))
    said = ok(host.run("secrets", "rename", "TS_KEY", "TAILSCALE_KEY"))
    assert "now names TAILSCALE_KEY: the VPN profile tail" in said
    assert json.loads(ok(host.run("vpn", "show", "tail", "--json")))["settings"]["auth-key-secret"] == "TAILSCALE_KEY"


# --- the options a rename rewrites (credrefs) ---------------------------------------------------------------------

def test_the_short_options_a_rename_knows_are_the_launchers():
    credrefs, launcher = load("credrefs"), load("agent_sandbox")
    for long, short in credrefs.SECRET_OPTIONS.items():
        assert launcher.SHORT_OPTIONS.get(long) == short, long
    assert set(credrefs.GIT_OPTIONS) == {f"--git-token-{kind}" for kind in ("secret", "env", "file")}


def test_only_references_to_the_secret_are_renamed():
    credrefs = load("credrefs")
    options = ["--secret-env", "A", "--secret-env", "B=A", "--secret-env", "A=B", "--api-key", "h.example=A",
               "--api-key", "h.example=V=A", "--git-token-secret", "https://h.example=A", "--git-token-env",
               "https://h.example=A", "--mcp-env", "s:V=A", "--mcp-env", "s:V@A", "--allow-host", "A"]
    renamed, count = credrefs.renamed_options(options, "A", "Z")
    assert count == 6
    assert renamed == ["--secret-env", "A=Z", "--secret-env", "B=Z", "--secret-env", "A=B", "--api-key",
                       "h.example=A=Z", "--api-key", "h.example=V=Z", "--git-token-secret", "https://h.example=Z",
                       "--git-token-env", "https://h.example=A", "--mcp-env", "s:V=A", "--mcp-env", "s:V@Z",
                       "--allow-host", "A"]
    assert credrefs.options_naming(options, "B") == ["--secret-env A=B"]
