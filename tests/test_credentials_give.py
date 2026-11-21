"""Every kind of credential given to a sandbox that runs - here and on another host (user 2026-11-19: "check in-depth if
Zugangsdaten/logins/creds can really be added to sessions, also remote sessions").

Through the real entry point, ./sandbox-ai as a subprocess of a user's host, with the stand-ins of the independent
verification B (tests/test_credentials_verify_b.py: Podman faked, the run's supervisor the real control server, an
ssh that runs the remote command as the other host's user). What this file adds to the two verifications:

  agent logins    `sandbox-ai login give RUN [AGENT] [--from-session NAME]`: the central login, or another session's,
                  into the HOME of a sandbox that runs - here and with --host; its refusals name the run
  another host    `sandbox-ai secrets send NAME HOST`: this machine's secret into that host's store over ssh's
                  standard input, then given there
  publish logins  `sandbox-ai publish-logins give RUN KIND`: not possible while it runs - said, with the ways
  an edit         `git-logins edit` names the sandboxes that run with the login as it was, and how to give it

The Git login and API key paths themselves are the verifications' (test_credentials_verify_a.py, _b.py) and
test_credentials_given.py; the app's window: projects/sandbox-ai-app/tests/test_credentials_give_all_app.py."""
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import time

import pytest

from test_credentials_verify_b import (EVERY_VALUE, GIVEN_TOKEN, LOGIN_SECRET, SECOND_KEY, SSH, argv_text,  # noqa: F401
                                       codex_login, files_with, given_logins, host, keep_secret, ssh_calls, start,
                                       user_host)

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux") or not shutil.which("git")
                                or not shutil.which("openssl"), reason="needs Linux, git and openssl")


@pytest.fixture
def far(tmp_path, host):
    """Another machine of `sandbox-ai hosts` (lab = dev@lab.example.org:2222), as the verification's."""
    other = user_host(tmp_path / "far", name="far")
    other.started = host.started
    (other.bin / "sandbox-ai").symlink_to(ROOT / "sandbox-ai")
    (tmp_path / "far-env.json").write_text(json.dumps(other.env))
    (host.bin / "ssh").write_text(SSH.replace("PYTHON", sys.executable, 1))
    (host.bin / "ssh").chmod(0o755)
    host.env["CVB_REMOTE_ENV"] = str(tmp_path / "far-env.json")
    added = host.run("hosts", "add", "lab", "dev@lab.example.org", "--port", "2222")
    assert added.returncode == 0, added.stderr
    return other


def central_login(on, document):
    """DOCUMENT as the central codex login of the user of ON (what `sandbox-ai login codex` keeps)."""
    store = on.home / ".local/share/sandbox-ai/auth/codex/.codex"
    store.mkdir(parents=True, mode=0o700)
    (store / "auth.json").write_text(document)
    (store / "auth.json").chmod(0o600)
    return store / "auth.json"


def codex(on, *options):
    on.add_image("localhost/codex-sandbox:latest")
    run = start(on, *options, agent="codex")
    return run, run.mounts["/root"] / ".codex/auth.json"


def said(done):
    assert LOGIN_SECRET not in done.stdout + done.stderr, "a token was printed"
    return done.stdout + done.stderr


def refresh_of(path):
    return json.loads(path.read_text())["tokens"]["refresh_token"]


# --- agent logins -----------------------------------------------------------------------------------------------------

def test_the_central_login_is_given_to_a_sandbox_that_runs(host, tmp_path):
    """A codex session started without a login (--no-auth; or before `sandbox-ai login codex`): `login give RUN` puts
    the central login into its HOME as a start does - the same document, 0600 - shows whose it is first, and says that
    codex must be started again to read it (with the command)."""
    central = central_login(host, codex_login())
    run, file = codex(host, "--no-auth")
    name = run.record["name"]
    assert not file.exists()
    # What would be given: shown, nothing written; no terminal and no --yes: nothing given.
    shown = host.run("login", "give", name, "--dry-run")
    assert shown.returncode == 0, shown.stderr
    text = said(shown)
    assert "The central login (codex):" in text and "fingerprint:" in text
    assert f"{name} (running) has no codex login now." in text and not file.exists()
    preview = json.loads(host.run("login", "give", name, "--dry-run", "--json").stdout)
    assert preview["run"]["name"] == name and preview["reads_again"] is False and preview["same"] is False
    fingerprint = preview["login"]["fingerprint"]
    unasked = host.run("login", "give", name)
    assert unasked.returncode == 1 and "add --yes" in unasked.stderr and not file.exists()
    stale = host.run("login", "give", name, "--yes", "--expect", "00000000")
    assert stale.returncode == 1 and "changed since it was shown" in stale.stderr and not file.exists()
    given = host.run("login", "give", name, "--yes", "--expect", fingerprint)
    assert given.returncode == 0, given.stderr
    text = said(given)
    assert f"Gave the central login (codex, fingerprint {fingerprint}) to {name}" in text
    assert f"sandbox-ai kits restart-agent {name}" in text, "codex reads its login when it starts: said, with the way"
    assert "rotate their refresh tokens" in text
    assert json.loads(file.read_text()) == json.loads(central.read_text())
    assert stat.S_IMODE(file.stat().st_mode) == 0o600 and stat.S_IMODE(file.parent.stat().st_mode) == 0o700
    # Listed as the session's login from now on; again: nothing to do.
    listed = json.loads(host.run("login", "--status", "--sessions", "--json").stdout)
    row = next(r for r in listed["sessions"] if r["name"] == name)
    assert row["login"] and row["info"]["fingerprint"] == fingerprint
    again = host.run("login", "give", name, "--yes")
    assert again.returncode == 0 and "it has this login already" in again.stdout
    # --restart: the agent cannot be started again inside this sandbox (--no-tmux: no window of its own) - the login
    # is given all the same, and the note says the way that works.
    central.write_text(codex_login(refresh="2"))
    restarted = host.run("login", "give", name, "--yes", "--restart", "--json")
    result = json.loads(restarted.stdout)
    assert result["given"] is True and result["restart_needed"] is True and result["restarted"] is False
    assert any("was not started again" in note for note in result["notes"])
    assert refresh_of(file) == f"{LOGIN_SECRET}-refresh-2"
    status, out, err = run.end()
    assert status == 0, err
    assert LOGIN_SECRET not in out + err and LOGIN_SECRET not in argv_text(host)
    holding = files_with([host.directory], [LOGIN_SECRET])
    assert all("/home/.codex/auth.json" in path or path == str(central) for path in holding), holding


def test_another_sessions_login_is_given_and_the_central_store_stays_as_it_is(host, tmp_path):
    """Session A's login (its own account, from a file) copied into the running session B, which has the central
    login of another account: said first as a login of ANOTHER account; B's HOME has A's afterwards, the central
    store keeps its own - also when B ends (what a sandbox writes back is only ever the store's own account)."""
    central = central_login(host, codex_login(account="acct-central"))
    login = tmp_path / "a.json"
    login.write_text(codex_login(refresh="A", account="acct-a"))
    login.chmod(0o600)
    first, _ = codex(host, "--auth-file", str(login))
    a = first.record["name"]
    assert first.end()[0] == 0
    second, file = codex(host)
    b = second.record["name"]
    assert json.loads(file.read_text())["tokens"]["account_id"] == "acct-central"
    shown = host.run("login", "give", b, "--from-session", a, "--dry-run")
    assert shown.returncode == 0, shown.stderr
    text = said(shown)
    assert f"The login of session {a} (codex):" in text
    assert f"It replaces the login {b} (running) has now of ANOTHER account" in text
    given = host.run("login", "give", b, "codex", "--from-session", a, "--yes")
    assert given.returncode == 0, given.stderr
    assert f"Gave the login of session {a}" in said(given)
    assert json.loads(file.read_text())["tokens"]["account_id"] == "acct-a"
    assert stat.S_IMODE(file.stat().st_mode) == 0o600
    before = central.read_text()
    status, out, err = second.end()
    assert status == 0, err
    assert central.read_text() == before, "the central login is not replaced by what a session was given"
    assert LOGIN_SECRET not in out + err


def test_a_claude_login_is_merged_into_its_credentials_file_and_read_again_by_itself(host):
    """Claude's ~/.claude/.credentials.json also holds the OAuth tokens of the sandbox's MCP servers, and
    ~/.claude.json is Claude's own state while it runs: the login alone is replaced, the rest stays; nothing needs a
    restart."""
    now = int(time.time() * 1000)
    store = host.home / ".local/share/sandbox-ai/auth/claude"
    (store / ".claude").mkdir(parents=True, mode=0o700)
    central = {"claudeAiOauth": {"accessToken": f"{LOGIN_SECRET}-access-new", "refreshToken": f"{LOGIN_SECRET}-refresh",
                                 "expiresAt": now + 3_600_000, "subscriptionType": "max"}}
    (store / ".claude/.credentials.json").write_text(json.dumps(central))
    (store / ".claude/.credentials.json").chmod(0o600)
    (store / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "new@example.org"}}))
    (store / ".claude.json").chmod(0o600)
    run = start(host, "--no-auth")
    home = run.mounts["/root"]
    (home / ".claude").mkdir(mode=0o700, exist_ok=True)
    (home / ".claude/.credentials.json").write_text(json.dumps({
        "claudeAiOauth": {"accessToken": f"{LOGIN_SECRET}-old", "refreshToken": f"{LOGIN_SECRET}-oldr",
                          "expiresAt": now - 1000}, "mcpOAuth": {"server": {"accessToken": "kept-as-it-is"}}}))
    (home / ".claude.json").write_text(json.dumps({"projects": {"/work": {"x": 1}}, "theme": "dark"}))
    given = host.run("login", "give", run.record["name"], "--yes")
    assert given.returncode == 0, given.stderr
    text = said(given)
    assert "claude reads its login file again before its next request: nothing to restart" in text
    assert "restart-agent" not in text
    document = json.loads((home / ".claude/.credentials.json").read_text())
    assert document["claudeAiOauth"]["accessToken"] == f"{LOGIN_SECRET}-access-new"
    assert document["mcpOAuth"] == {"server": {"accessToken": "kept-as-it-is"}}, "the MCP servers' tokens stay"
    assert json.loads((home / ".claude.json").read_text()) == {"projects": {"/work": {"x": 1}}, "theme": "dark"}, \
        "Claude's own state is left to Claude"
    assert stat.S_IMODE((home / ".claude/.credentials.json").stat().st_mode) == 0o600
    assert run.end()[0] == 0


def test_login_give_refuses_naming_the_run_and_changes_nothing(host, tmp_path):
    central = central_login(host, codex_login())
    run, file = codex(host, "--no-auth")
    name = run.record["name"]
    plain = start(host, "--no-auth")  # a claude session
    for arguments, reason in (
            (["no-such-run", "--yes"], "no-such-run"),
            ([name, "claude", "--yes"], f"{name} is a codex session and runs no claude beside it"),
            ([plain.record["name"], "codex", "--yes"], f"{plain.record['name']} is a claude session"),
            ([name, "--from-session", name, "--yes"], f"{name} is the session the login is taken from"),
            ([name, "--from-session", "no-such-session", "--yes"], "no-such-session"),
            ([name, "--expect", "nothex", "--yes"], "--expect is the 8 hex characters"),
            ([name, "--host", "nowhere", "--yes"], "nowhere")):
        refused = host.run("login", "give", *arguments)
        assert refused.returncode != 0 and reason in refused.stderr, (arguments, refused.stderr)
        assert "Traceback" not in refused.stderr and LOGIN_SECRET not in refused.stdout + refused.stderr
    assert not file.exists()
    # No central login: said, with the two ways.
    central.unlink()
    none = host.run("login", "give", name, "--yes")
    assert none.returncode != 0 and "no central codex login is stored (sandbox-ai login codex)" in none.stderr
    assert "--from-session NAME" in none.stderr
    central.write_text(codex_login())
    central.chmod(0o600)
    # A folder of its HOME the agent replaced by a link: never written through (nothing lands outside).
    outside = tmp_path / "outside"
    outside.mkdir()
    aside = file.parent.with_name(".codex-aside")
    if file.parent.exists():
        file.parent.rename(aside)
    file.parent.symlink_to(outside)
    linked = host.run("login", "give", name, "--yes")
    assert linked.returncode != 0 and "link" in linked.stderr, linked.stderr
    assert list(outside.iterdir()) == []
    file.parent.unlink()
    if aside.exists():
        aside.rename(file.parent)
    # A start that does not run: said by its name, with what brings it the login.
    assert run.end()[0] == 0
    stopped = host.run("login", "give", name, "--yes")
    assert stopped.returncode != 0 and f"{name} does not run" in stopped.stderr
    assert plain.end()[0] == 0


def test_a_login_is_given_to_a_sandbox_on_another_host(host, far, tmp_path):
    """`sandbox-ai login give RUN --host lab`: that host's central login into that host's running session - shown
    here, given there; nothing of it comes to this machine, no token in any command line."""
    central = central_login(far, codex_login(account="acct-far"))
    run, file = codex(far, "--no-auth")
    name = run.record["name"]
    shown = host.run("login", "give", name, "--host", "lab", "--dry-run")
    assert shown.returncode == 0, shown.stderr
    assert "The central login (codex):" in said(shown) and not file.exists()
    given = host.run("login", "give", name, "--host", "lab", "--yes")
    assert given.returncode == 0, given.stderr
    assert f"to {name} on lab" in said(given)
    assert json.loads(file.read_text()) == json.loads(central.read_text())
    calls = [call for call in ssh_calls(host) if "-N" not in call and "-O" not in call]
    assert calls and all(call[call.index("--") + 1] == "dev@lab.example.org" for call in calls)
    assert any(f"exec sandbox-ai login give {name} --yes --expect=" in call[-1] for call in calls)
    assert files_with([host.directory], [LOGIN_SECRET]) == {} and LOGIN_SECRET not in argv_text(host)
    assert run.end()[0] == 0


# --- a secret of this machine for a sandbox on another host -----------------------------------------------------------

def test_a_secret_of_this_machine_is_sent_to_another_host_and_given_there(host, far, tmp_path, monkeypatch):
    """This machine's secret, a session on lab: `secrets give` there knows only lab's secrets. `secrets send NAME
    lab` copies it - asked first, the value on ssh's standard input into `secrets set` there - then it is given to
    lab's run as any secret of lab."""
    keep_secret(host, "OPENROUTER", SECOND_KEY)
    run = start(far)
    proxy_run = tmp_path / "proxy-run"
    proxy_run.mkdir(mode=0o700)
    (proxy_run / "allow.json").write_text("{}")
    run.supervise(proxy_run, monkeypatch)
    name = run.record["name"]
    # Before: lab has no such secret, the give there says so.
    missing = host.run("hosts", "run", "lab", "--", "secrets", "give", name, "OPENROUTER", "--api-key",
                       "api.creds.test")
    assert missing.returncode != 0 and "OPENROUTER" in missing.stderr
    unasked = host.run("secrets", "send", "OPENROUTER", "lab")
    assert unasked.returncode == 1 and "there is no terminal to ask: add --yes" in unasked.stderr
    assert not (far.home / ".local/share/sandbox-ai/secrets/OPENROUTER").exists()
    sent = host.run("secrets", "send", "OPENROUTER", "lab", "--yes")
    assert sent.returncode == 0, sent.stderr
    assert SECOND_KEY not in sent.stdout + sent.stderr
    assert "OPENROUTER sent to lab" in sent.stdout and f"sandbox-ai hosts run lab -- secrets give RUN OPENROUTER" in \
        sent.stdout
    assert (far.home / ".local/share/sandbox-ai/secrets/OPENROUTER").read_text().strip() == SECOND_KEY
    # The value went over standard input: no command line of ssh (or anything else) holds it, here or there.
    calls = [call for call in ssh_calls(host) if "-N" not in call and "-O" not in call]
    assert any("exec sandbox-ai secrets set OPENROUTER;" in call[-1] for call in calls)
    assert not any(SECOND_KEY in json.dumps(call) for call in ssh_calls(host))
    assert SECOND_KEY not in argv_text(host) + argv_text(far)
    assert files_with([host.directory, far.directory], [SECOND_KEY],
                      skip=[host.home / ".local/share/sandbox-ai/secrets",
                            far.home / ".local/share/sandbox-ai/secrets"]) == {}, "no temporary file, no log"
    # Given there as any secret of that host.
    given = host.run("hosts", "run", "lab", "--", "secrets", "give", name, "OPENROUTER", "--api-key", "api.creds.test",
                     "--as", "OR_KEY")
    assert given.returncode == 0, given.stderr
    assert json.loads((proxy_run / "api-keys-added.json").read_text())["api_keys"][0]["key"] == SECOND_KEY
    # Kept there already: replaced only when said; another name with --as.
    again = host.run("secrets", "send", "OPENROUTER", "lab", "--yes")
    assert again.returncode == 1 and "lab keeps a secret OPENROUTER already: add --replace" in again.stderr
    assert host.run("secrets", "send", "OPENROUTER", "lab", "--yes", "--replace").returncode == 0
    renamed = host.run("secrets", "send", "OPENROUTER", "lab", "--yes", "--as", "OR_COPY")
    assert renamed.returncode == 0, renamed.stderr
    assert (far.home / ".local/share/sandbox-ai/secrets/OR_COPY").read_text().strip() == SECOND_KEY
    for arguments, reason in ((["NOPE", "lab", "--yes"], "no secret NOPE on this machine"),
                              (["OPENROUTER", "nowhere", "--yes"], "no host nowhere"),
                              (["OPENROUTER", "local", "--yes"], "HOST is another host")):
        refused = host.run("secrets", "send", *arguments)
        assert refused.returncode == 1 and reason in refused.stderr, (arguments, refused.stderr)
        assert SECOND_KEY not in refused.stdout + refused.stderr
    assert run.end()[0] == 0


def test_a_git_login_of_this_machine_reaches_a_session_on_another_host(host, far, tmp_path, monkeypatch):
    """The whole way for a Git login: its secret sent to lab, then `git-logins give` there - lab's run's proxy has
    the login, this machine keeps no trace beyond its own secret store."""
    keep_secret(host, "GH_PUSH", GIVEN_TOKEN)
    run = start(far)
    proxy_run = tmp_path / "proxy-run"
    proxy_run.mkdir(mode=0o700)
    (proxy_run / "allow.json").write_text("{}")
    run.supervise(proxy_run, monkeypatch)
    name = run.record["name"]
    assert host.run("secrets", "send", "GH_PUSH", "lab", "--yes").returncode == 0
    given = host.run("hosts", "run", "lab", "--", "git-logins", "give", "--secret", "GH_PUSH", "--user", "alice",
                     "--", name, "https://git.creds.test")
    assert given.returncode == 0, given.stderr
    assert given_logins(proxy_run) == [("git.creds.test", 443, "alice", GIVEN_TOKEN)]
    assert files_with([run.session], [GIVEN_TOKEN]) == {}
    assert files_with([host.directory], [GIVEN_TOKEN], skip=[host.home / ".local/share/sandbox-ai/secrets"]) == {}
    assert GIVEN_TOKEN not in argv_text(host) + argv_text(far)
    assert run.end()[0] == 0


# --- what cannot be given while it runs: said, with the ways ----------------------------------------------------------

def test_a_publish_login_is_not_given_to_a_running_sandbox_and_the_refusal_names_it(host):
    keep_secret(host, "PYPI_TOKEN", "pypi-AgEIcHlwaS5vcmcSECRET0123456789")
    assert host.run("publish-logins", "add", "pypi", "--secret", "PYPI_TOKEN").returncode == 0
    run = start(host)
    name = run.record["name"]
    refused = host.run("publish-logins", "give", name, "pypi")
    assert refused.returncode == 1 and refused.stdout == ""
    text = refused.stderr
    assert f"{run.run} ({name}) cannot be given the publish login pypi while it runs" in text
    assert f"sandbox-ai resume {name} --restart --publish pypi" in text
    assert f"sandbox-ai secrets give {name} PYPI_TOKEN --api-key upload.pypi.org --as TWINE_PASSWORD" in text
    assert "pypi-AgEI" not in text
    unknown = host.run("publish-logins", "give", "no-such-run", "pypi")
    assert unknown.returncode == 1 and "no-such-run" in unknown.stderr
    assert run.end()[0] == 0


# --- a login edited while sandboxes run -------------------------------------------------------------------------------

def test_an_edited_git_login_names_the_sandboxes_that_run_and_how_to_give_it(host, tmp_path, monkeypatch):
    """`git-logins edit` is for the starts from then on: it says which sandboxes run with what they were started
    with, and the command that gives one the login as it is now - which then works."""
    keep_secret(host, "OLD", "ghp_" + "o" * 36)
    keep_secret(host, "GH_PUSH", GIVEN_TOKEN)
    assert host.run("git-logins", "add", "https://git.creds.test", "--secret", "OLD").returncode == 0
    quiet = host.run("git-logins", "edit", "https://git.creds.test", "--user", "bob")
    assert quiet.returncode == 0 and "note: this is for the starts from now on" not in quiet.stdout, "nothing runs"
    run = start(host)
    proxy_run = tmp_path / "proxy-run"
    proxy_run.mkdir(mode=0o700)
    (proxy_run / "allow.json").write_text("{}")
    run.supervise(proxy_run, monkeypatch)
    name = run.record["name"]
    edited = host.run("git-logins", "edit", "https://git.creds.test", "--secret", "GH_PUSH", "--user", "alice")
    assert edited.returncode == 0, edited.stderr
    note = next(line for line in edited.stdout.splitlines() if line.startswith("note: this is for the starts"))
    assert f"1 sandbox runs with what it was started with ({name})" in note
    assert "sandbox-ai git-logins give RUN https://git.creds.test --secret GH_PUSH --user alice" in note
    given = host.run("git-logins", "give", name, "https://git.creds.test", "--secret", "GH_PUSH", "--user", "alice")
    assert given.returncode == 0, given.stderr
    assert given_logins(proxy_run) == [("git.creds.test", 443, "alice", GIVEN_TOKEN)]
    assert run.end()[0] == 0


def test_a_login_of_an_agent_that_runs_beside_the_leading_one_is_taken(tmp_path):
    """`login give RUN AGENT`: AGENT is the run's own agent, or one it runs beside it (--with A,B of its kept
    options, long or short, joined or not) - never another one."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("sandbox_ai_session_login_give_test", ROOT / "sandbox/session_login.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for options, expected in ((["--with", "codex,pi", "--allow-host", "x.example"], ["codex", "pi"]),
                              (["--with=codex", "-wa", "qwen", "--with", "codex"], ["codex", "qwen"]),
                              (["--allow-host", "--with"], []), ([], [])):
        (tmp_path / "session.json").write_text(json.dumps({"agent": "claude", "options": options}))
        assert module.companions({"directory": str(tmp_path)}) == expected, options
    assert module.companions({"directory": None}) == [] and module.companions({"directory": str(tmp_path / "no")}) == []
    (tmp_path / "session.json").write_text("{not json")
    assert module.companions({"directory": str(tmp_path)}) == []
