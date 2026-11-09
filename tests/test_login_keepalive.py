"""The Claude login stays alive across runs, idle hours and sandboxes running at the same time.

Claude Code's OAuth login holds a short-lived access token and a refresh token that rotates: each refresh returns a
new pair and uses the old refresh token up. Whichever side refreshed last holds the only valid pair, so a refresh in
one sandbox must reach the source (host or `sandbox-ai login` store) and every other sandbox of that login before
they refresh with the used-up token. An OAuth server may also revoke the whole login when a used-up refresh token is
sent again (refresh token reuse detection, RFC 9700 section 4.14.2); then one stale sandbox logs out all of them.

Nothing here contacts a real endpoint: FakeOAuth is a local HTTP server with rotation and (optionally) reuse
detection and the account lookup the launcher makes before a sandbox's login goes back to its source (each login of
FakeOAuth is an account of its own), FakeClaude refreshes the way Claude Code 2.1.317 does (taken from its bundle: under its own lock it reads
its credentials file again, takes a pair another process wrote instead of refreshing, and after a failed refresh
reads the file once more before giving up). All tokens are random fixtures.
"""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
import urllib.error
import urllib.request

import pytest

import test_feature_matrix as fm
from test_feature_matrix import m  # noqa: F401  (fixture)
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import home_guard  # noqa: E402

home_guard.ensure()  # tests write logins: never into the real HOME, also when conftest.py was not run

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sandbox_login_keepalive", ROOT / "sandbox/agent_sandbox.py")
s = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(s)
LOGIN_SPEC = importlib.util.spec_from_file_location("sandbox_login_keepalive_login", ROOT / "sandbox/login.py")
login = importlib.util.module_from_spec(LOGIN_SPEC)
LOGIN_SPEC.loader.exec_module(login)
HOUR = 3600


class FakeOAuth:
    """A token endpoint with rotating refresh tokens. REUSE_DETECTION: a used-up refresh token sent again revokes
    every token of its login (its family). Each family is an account of its own (account())."""
    live = []  # every server not closed yet: the fake account lookup asks them

    def __init__(self, reuse_detection=True, strict=True):
        FakeOAuth.live.append(self)
        self.lookups = []  # the access tokens the account lookup was asked about
        self.reuse_detection, self.strict = reuse_detection, strict  # STRICT=False: a used token still works
        self.refresh, self.access = {}, {}  # token -> family; access also -> expiry (s)
        self.used, self.revoked = {}, set()  # used refresh token -> family; revoked families
        self.reuses = 0
        self.requests = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                server.requests.append(self.path)
                if self.path != "/v1/oauth/token" or body.get("grant_type") != "refresh_token":
                    return self.reply(400, {"error": "unsupported_grant_type"})
                token = body.get("refresh_token")
                family = server.refresh.pop(token, None) if server.strict else server.refresh.get(token)
                if family is None or family in server.revoked:
                    if token in server.used:
                        server.reuses += 1
                        if server.reuse_detection:
                            server.revoked.add(server.used[token])
                    return self.reply(400, {"error": "invalid_grant"})
                server.used[token] = family
                return self.reply(200, server.pair(family))

            def do_GET(self):
                token = self.headers.get("Authorization", "").removeprefix("Bearer ")
                family, expiry = server.access.get(token, (None, 0))
                ok = family is not None and family not in server.revoked and expiry > time.time()
                return self.reply(200 if ok else 401, {"ok": ok})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def pair(self, family, lifetime=HOUR):
        access, refresh = "fixture-access-" + secrets.token_hex(8), "fixture-refresh-" + secrets.token_hex(8)
        self.access[access] = (family, time.time() + lifetime)
        self.refresh[refresh] = family
        return {"access_token": access, "refresh_token": refresh, "expires_in": lifetime}

    def login(self, lifetime=HOUR):
        """A new login, as claudeAiOauth; LIFETIME of its access token in seconds (negative: expired)."""
        reply = self.pair(secrets.token_hex(4), lifetime)
        return {"accessToken": reply["access_token"], "refreshToken": reply["refresh_token"],
                "expiresAt": int((time.time() + lifetime) * 1000), "scopes": ["user:inference"],
                "subscriptionType": "max"}

    def tokens(self):
        return set(self.access) | set(self.refresh) | set(self.used)

    def account(self, access):
        """What Anthropic's profile endpoint names for the access token ACCESS: (account, organization), or None
        (401) for an unknown, expired or revoked token."""
        family, expiry = self.access.get(access, (None, 0))
        if family is None or family in self.revoked or expiry <= time.time():
            return None
        return "account-" + family, "organization-fixture"

    def close(self):
        FakeOAuth.live.remove(self)
        self.httpd.shutdown()
        self.httpd.server_close()


def fake_profile_account(access_token, timeout=None):
    """claude_profile_account answered by the FakeOAuth servers of the test."""
    for server in FakeOAuth.live:
        if access_token in server.access:
            server.lookups.append(access_token)
            return server.account(access_token)
    return None


@pytest.fixture(autouse=True)
def account_lookup(monkeypatch):
    monkeypatch.setattr(s, "claude_profile_account", fake_profile_account)


class FakeClaude:
    """Claude Code in one sandbox HOME, as far as its login goes."""

    def __init__(self, home, server):
        self.path, self.server = Path(home) / ".claude/.credentials.json", server
        self.lock = threading.Lock()  # Claude's own lock: it reaches only processes that share this HOME
        self.memory = self.read()

    def read(self):
        return json.loads(self.path.read_text())["claudeAiOauth"]

    def call(self):
        """One request to the model API with the access token in memory: True if it is accepted."""
        request = urllib.request.Request(self.server.url + "/v1/me",
                                         headers={"Authorization": "Bearer " + self.memory["accessToken"]})
        try:
            with urllib.request.urlopen(request, timeout=10):
                return True
        except urllib.error.HTTPError:
            return False

    def use(self):
        """What Claude does on each request: refresh first if the access token has (nearly) expired."""
        if self.memory["expiresAt"] / 1000 - 300 <= time.time():
            if self.refresh() == "known_dead_refresh_token":
                return False
        return self.call()

    def refresh(self):
        with self.lock:
            stored = self.read()
            if stored["accessToken"] != self.memory["accessToken"]:
                self.memory = stored
                return "race_resolved"  # another process refreshed: its pair is taken, nothing sent
            body = json.dumps({"grant_type": "refresh_token", "refresh_token": stored["refreshToken"],
                               "client_id": "fixture-client", "scope": "user:inference"}).encode()
            request = urllib.request.Request(self.server.url + "/v1/oauth/token", data=body,
                                             headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    reply = json.loads(response.read())
            except urllib.error.HTTPError:
                stored = self.read()
                if stored["accessToken"] != self.memory["accessToken"]:
                    self.memory = stored
                    return "race_recovered"
                return "known_dead_refresh_token"
            self.memory = {**stored, "accessToken": reply["access_token"], "refreshToken": reply["refresh_token"],
                           "expiresAt": int((time.time() + reply["expires_in"]) * 1000)}
            document = json.loads(self.path.read_text())
            document["claudeAiOauth"] = self.memory
            self.path.write_text(json.dumps(document))
            return "refreshed"


@pytest.fixture
def host(tmp_path, monkeypatch):
    home = tmp_path / "host"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in ("XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME"):
        monkeypatch.delenv(name, raising=False)
    return home


@pytest.fixture(params=[True, False], ids=["reuse-detection", "rotation-only"])
def server(request):
    server = FakeOAuth(reuse_detection=request.param)
    yield server
    server.close()


def store_login(oauth):
    """`sandbox-ai login claude` without the sandbox: the login goes into the store, with the account Claude wrote
    into its ~/.claude.json at the login (oauthAccount)."""
    files = {s.AUTH_TARGETS["claude"]: json.dumps({"claudeAiOauth": oauth}).encode()}
    family = next((server.access[oauth["accessToken"]][0] for server in FakeOAuth.live
                   if oauth["accessToken"] in server.access), None)
    if family:  # kept also once the access token has expired
        files[".claude.json"] = json.dumps({"oauthAccount": {"accountUuid": "account-" + family,
                                                             "organizationUuid": "organization-fixture",
                                                             "emailAddress": "user@example.org"}}).encode()
    login.write_store("claude", files)
    return s.auth_store("claude") / s.AUTH_TARGETS["claude"]


class Run:
    """A sandbox's start (prepare_home and the write-back state, as main() makes them), its Claude, its end."""

    def __init__(self, tmp_path, name, server, state_dir=False):
        self.home = tmp_path / name
        self.home.mkdir(mode=0o700)
        (tmp_path / "work").mkdir(exist_ok=True)
        options = ["claude", "--workspace", str(tmp_path / "work")] + (["--state-dir", str(self.home)] if state_dir else [])
        self.a = s.parse_args(options)
        with contextlib.redirect_stderr(io.StringIO()):
            s.prepare_home(self.a, self.home)
        self.pair = s.claude_credential_pair(self.a, self.home)
        self.state = s.claude_writeback_state(self.pair)
        self.claude = FakeClaude(self.home, server)

    def sync(self):
        """What watch_claude_login does when one of the two files changed."""
        return s.claude_login_sync(self.home, self.pair, self.state)

    def end(self):
        return s.claude_writeback(self.home, self.pair, self.state)


def expire(*runs):
    """Hours pass: the access tokens in memory have run out (as if their expiresAt lay behind)."""
    for run in runs:
        claude = getattr(run, "claude", run)
        claude.memory = {**claude.memory, "expiresAt": 0}


# --- question 1 and 4: the refresh of one sandbox must reach the store and the others during the run ------------


def test_write_back_only_at_the_end_lets_a_later_sandbox_use_up_the_login(tmp_path, host, server):
    """How it was before the live sync, and why it is not enough: the write-back at the end of a run alone."""
    stored = store_login(server.login())
    first = Run(tmp_path, "first", server)
    assert first.claude.use()
    expire(first)
    assert first.claude.use(), "the first sandbox refreshes on its own"
    # The store still holds the refresh token the first sandbox has just used up ...
    assert s.claude_oauth(stored)["refreshToken"] in server.used
    # ... so a sandbox started now gets it, and its refresh fails: "Please run /login".
    second = Run(tmp_path, "second", server)
    expire(second)
    assert second.claude.use() is False
    assert server.reuses == 1
    # With reuse detection the provider revokes the login: the first sandbox is logged out as well, and what its
    # end writes back to the store is dead too, so every later start needs a new login.
    assert first.claude.call() is (not server.reuse_detection)
    # A revoked login cannot be checked with Anthropic (it refuses the token) and is not written back; it is dead.
    assert first.end().startswith("refreshed Claude credentials written back") is (not server.reuse_detection)
    third = Run(tmp_path, "third", server)
    expire(third)
    assert third.claude.use() is (not server.reuse_detection)


def test_live_sync_keeps_sandboxes_started_one_after_another_logged_in(tmp_path, host, server):
    stored = store_login(server.login())
    first = Run(tmp_path, "first", server)
    expire(first)
    assert first.claude.use()
    assert first.sync() == "refreshed Claude credentials written back to sandbox-ai's login store"
    assert s.claude_oauth(stored)["refreshToken"] == first.claude.memory["refreshToken"]
    second = Run(tmp_path, "second", server)  # starts with the pair the first one refreshed
    assert second.claude.use()
    expire(second)
    assert second.claude.use() and "written back" in second.sync()
    # The first sandbox gets the second one's pair before its own access token runs out, and Claude takes it
    # instead of sending its used-up refresh token.
    assert "took over the newer Claude login" in first.sync()
    expire(first)
    assert first.claude.refresh() == "race_resolved" and first.claude.use()
    assert first.end() is None and second.end() is None
    third = Run(tmp_path, "third", server)
    expire(third)
    assert third.claude.use()
    assert server.reuses == 0


def test_live_sync_with_sandboxes_running_at_the_same_time(tmp_path, host, server):
    store_login(server.login())
    runs = [Run(tmp_path, f"run{i}", server) for i in range(3)]  # all started from one pair
    for turn in range(6):  # each refreshes in turn, hours apart; the others look at their files in between
        current = runs[turn % 3]
        expire(current)
        assert current.claude.use(), turn
        for run in [current] + [r for r in runs if r is not current]:
            run.sync()
    assert all(run.claude.use() for run in runs)
    assert server.reuses == 0
    # The sandboxes that did not refresh last took over the newest pair, so none of them sends a used one.
    assert len({json.loads((run.home / ".claude/.credentials.json").read_text())["claudeAiOauth"]["refreshToken"]
                for run in runs}) == 1


def test_a_stale_write_back_never_replaces_a_newer_login(tmp_path, host):
    """Compare-and-swap: a sandbox whose refresh the provider still accepted (a provider that lets a used refresh
    token work for a while) but which refreshed from a pair already replaced in the store never overwrites the
    newer one there."""
    server = FakeOAuth(reuse_detection=False, strict=False)
    try:
        stored = store_login(server.login())
        first, second = Run(tmp_path, "first", server), Run(tmp_path, "second", server)
        expire(first)
        assert first.claude.use() and "written back" in first.sync()
        newest = s.claude_oauth(stored)["refreshToken"]
        # The second sandbox refreshes before it looked at its file again (within one interval) ...
        expire(second)
        second.claude.use()
        # ... and the store keeps the first one's pair, which also goes into the second sandbox.
        assert second.sync() == "the Claude login in sandbox-ai's login store changed during the run; kept that one"
        assert s.claude_oauth(stored)["refreshToken"] == newest
        assert json.loads((second.home / ".claude/.credentials.json").read_text())["claudeAiOauth"][
            "refreshToken"] == newest
        assert second.end() is None and s.claude_oauth(stored)["refreshToken"] == newest
    finally:
        server.close()


def test_the_host_claude_and_a_sandbox_share_the_host_login(tmp_path, host, server):
    """Without a login store the sandbox shares the host's login: the host's own Claude refreshes too."""
    (host / ".claude").mkdir()
    (host / ".claude/.credentials.json").write_text(json.dumps({"claudeAiOauth": server.login(),
                                                                "mcpOAuth": {"srv": {"accessToken": "m"}}}))
    on_host = FakeClaude(host, server)
    sandbox = Run(tmp_path, "sandbox", server)
    expire(on_host)
    assert on_host.use()  # Claude on the host refreshed
    assert "took over the newer Claude login from the host" in sandbox.sync()
    expire(sandbox)
    assert sandbox.claude.refresh() == "race_resolved" and sandbox.claude.use()
    expire(sandbox)
    assert sandbox.claude.use() and sandbox.sync() == "refreshed Claude credentials written back to the host"
    expire(on_host)
    assert on_host.refresh() == "race_resolved" and on_host.use()
    assert json.loads((host / ".claude/.credentials.json").read_text())["mcpOAuth"] == {"srv": {"accessToken": "m"}}
    assert server.reuses == 0


def test_a_persistent_home_follows_the_store_and_is_not_mistaken_for_a_missed_write_back(tmp_path, host, server):
    stored = store_login(server.login())
    session = Run(tmp_path, "session", server, state_dir=True)
    other = Run(tmp_path, "other", server)
    expire(other)
    assert other.claude.use() and "written back" in other.sync()
    assert "took over" in session.sync()
    # The launcher of the session is killed now (no end). Its next start finds the HOME and the store in step.
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        s.prepare_home(session.a, session.home)
    assert "never written back" not in err.getvalue()
    assert s.claude_oauth(session.home / ".claude/.credentials.json")["refreshToken"] == \
        s.claude_oauth(stored)["refreshToken"]


def test_a_new_login_reaches_running_sandboxes(tmp_path, host, server):
    store_login(server.login())
    running = Run(tmp_path, "running", server)
    fresh = server.login()
    store_login(fresh)  # sandbox-ai login claude while the sandbox runs (another account, a lost login)
    assert "took over" in running.sync()
    assert s.claude_oauth(running.home / ".claude/.credentials.json")["refreshToken"] == fresh["refreshToken"]


# --- question 2: between runs nothing refreshes; an expired access token is no reason to log in again ------------


def test_an_expired_access_token_is_handed_in_and_refreshed_at_the_first_request(tmp_path, host, server, capsys):
    stored = store_login(server.login(lifetime=-10 * HOUR))  # the last run ended ten hours ago
    assert s.login_state("claude") == ("store", None)
    assert login.status() == 0
    assert "access token refreshes on use" in capsys.readouterr().out
    run = Run(tmp_path, "run", server)
    assert s.claude_token_status(run.home / ".claude/.credentials.json").startswith("Claude access token expired")
    # Onboarding is marked done, so Claude does not offer a new login instead of refreshing.
    assert json.loads((run.home / ".claude.json").read_text())["hasCompletedOnboarding"] is True
    assert run.claude.use()
    assert "written back" in run.sync()
    assert s.claude_oauth(stored)["expiresAt"] > time.time() * 1000


def test_a_refresh_token_past_its_expiry_needs_a_new_login(tmp_path, host, server, capsys):
    stored = store_login({**server.login(), "refreshTokenExpiresAt": int((time.time() - 60) * 1000)})
    assert s.login_state("claude") == ("store", "expired")
    login.status()
    assert "expired, log in again" in capsys.readouterr().out
    run = Run(tmp_path, "run", server)
    assert "hasCompletedOnboarding" not in json.loads((run.home / ".claude.json").read_text() or "{}")
    # A login with time left says how long it can still be renewed (the provider's own figure).
    store_login({**s.claude_oauth(stored), "refreshTokenExpiresAt": int((time.time() + 30 * 86400 + 600) * 1000)})
    login.status()
    assert "renewable for 30 more days" in capsys.readouterr().out


# --- question 3: a long idle run can refresh: the token endpoint is the provider's, open in every mode ----------


@pytest.mark.parametrize("mode", [None, "medium", "high", "paranoid"])
def test_the_token_endpoint_is_reachable_through_the_proxy(m, mode):  # noqa: F811
    if m.agent != "claude":
        pytest.skip("Claude's login")
    run = m.run(*(["--mode", mode] if mode else []))
    runtime = fm.s.RUNTIME
    rules = runtime.Ruleset(list(run.a.rulesets), providers=fm.s.provider_hosts(run.a))
    # Claude Code 2.1.317 refreshes at the first, older versions at the second.
    for name in ("platform.claude.com", "console.anthropic.com"):
        assert name in run.config["pinned"]
        decision = rules.http(runtime.Ruleset.request(name, "POST", "/v1/oauth/token", []))["decision"]
        assert decision == "allow", (name, mode)


# --- the whole start: the watcher writes back during the run; no token is ever printed ---------------------------


def test_main_writes_a_refresh_back_while_the_sandbox_still_runs(tmp_path, host, server, monkeypatch):
    stored = store_login(server.login(lifetime=-HOUR))
    monkeypatch.setattr(s, "CLAUDE_SYNC_INTERVAL", 0.02)
    monkeypatch.setattr(s.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(s.shutil, "which", lambda name: "/usr/bin/" + name)
    seen = {}

    def run_pod(a, session, home):
        claude = FakeClaude(home, server)
        assert claude.use()  # refreshes the expired access token
        deadline = time.monotonic() + 10
        while s.claude_oauth(stored)["refreshToken"] != claude.memory["refreshToken"] and time.monotonic() < deadline:
            time.sleep(0.02)
        seen["during"] = s.claude_oauth(stored)["refreshToken"] == claude.memory["refreshToken"]
        return 0

    monkeypatch.setattr(s, "run_pod", run_pod)
    err, out = io.StringIO(), io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
        assert s.main(["claude", "--workspace", str(tmp_path / "w"), "--offline", "--no-tmux"]) == 0
    assert seen["during"], "written back while the sandbox ran, not only after it"
    assert "refreshed Claude credentials written back to sandbox-ai's login store (during the run)" in err.getvalue()
    printed = err.getvalue() + out.getvalue()
    assert not any(token in printed for token in server.tokens())
    # The launcher's own files (the login-sync record, its lock, logs) hold no token; the sandbox HOME kept for
    # `sandbox-ai resume` holds its login, as it must.
    state = host / ".local/state/sandbox-ai"
    leaked = [str(path) for path in state.rglob("*") if path.is_file() and "home" not in path.relative_to(state).parts
              and any(token in path.read_text(errors="replace") for token in server.tokens())]
    assert leaked == []


# --- only a refresh of the same account goes back: a sandbox never replaces the source's login with another --------

ANOTHER = "the sandbox holds a Claude login of another account (or one that could not be checked): not written back"


def host_login(host, server, account=True, lifetime=HOUR):
    """The host's own Claude login (with an MCP server's OAuth token in the same file) and, if ACCOUNT, the account
    Claude keeps for it in ~/.claude.json."""
    oauth = server.login(lifetime=lifetime)
    path = host / ".claude/.credentials.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"claudeAiOauth": oauth, "mcpOAuth": {"srv": {"accessToken": "m"}}}))
    if account:
        family = server.access[oauth["accessToken"]][0]
        (host / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "account-" + family,
                                                                        "organizationUuid": "organization-fixture"}}))
    return path


def sandbox_relogin(run, oauth):
    """`/login` with another account inside the sandbox (or tokens the agent planted): Claude writes them."""
    path = run.home / ".claude/.credentials.json"
    document = json.loads(path.read_text())
    document["claudeAiOauth"] = {**document["claudeAiOauth"], **oauth}
    path.write_text(json.dumps(document))
    run.claude.memory = run.claude.read()


def test_a_refresh_of_the_same_account_is_written_back_after_asking_anthropic_once(tmp_path, host, server):
    credentials = host_login(host, server)
    run = Run(tmp_path, "run", server)
    expire(run)
    assert run.claude.use()
    assert run.sync() == "refreshed Claude credentials written back to the host"
    assert s.claude_oauth(credentials)["refreshToken"] == run.claude.memory["refreshToken"]
    # The host's account came from its ~/.claude.json; only the sandbox's new access token was asked about.
    assert server.lookups == [run.claude.memory["accessToken"]]
    assert server.reuses == 0


def test_a_source_without_account_state_is_asked_once_per_run(tmp_path, host, server):
    credentials = host_login(host, server, account=False)
    shared = s.claude_oauth(credentials)["accessToken"]
    run = Run(tmp_path, "run", server)
    for _ in range(2):
        expire(run)
        assert run.claude.use() and run.sync() == "refreshed Claude credentials written back to the host"
    assert server.lookups.count(shared) == 1 and len(server.lookups) == 3
    assert s.claude_oauth(credentials)["refreshToken"] == run.claude.memory["refreshToken"]


@pytest.mark.parametrize("where", ["host", "store"])
def test_a_login_of_another_account_in_the_sandbox_never_reaches_its_source(tmp_path, host, server, where):
    """What happened to the user: `/login` with another account in a sandbox replaced the host's login. And an
    agent could plant its own account's tokens in the host's login the same way."""
    source = host_login(host, server) if where == "host" else store_login(server.login())
    before = source.read_bytes()
    named = "the host" if where == "host" else "sandbox-ai's login store"
    run = Run(tmp_path, "run", server)
    other = server.login(lifetime=2 * HOUR)  # newer than the source's, as a fresh login is
    sandbox_relogin(run, other)
    note = run.sync()
    assert note == f"{ANOTHER} to {named}; {named} keeps its own login"
    assert source.read_bytes() == before
    # The sandbox keeps the login it chose; it is neither asked about again nor pushed back over.
    asked = len(server.lookups)
    assert run.sync() is None and run.end() is None and len(server.lookups) == asked
    assert run.claude.read()["refreshToken"] == other["refreshToken"]
    assert source.read_bytes() == before
    # Tokens Anthropic does not know (made up by the agent) cannot be checked: not written back either.
    sandbox_relogin(run, {"accessToken": "planted-access", "refreshToken": "planted-refresh",
                          "expiresAt": int((time.time() + 3 * HOUR) * 1000)})
    assert run.sync().startswith(ANOTHER)
    assert source.read_bytes() == before
    assert not any(token in note for token in server.tokens())


def test_a_planted_login_after_a_genuine_refresh_is_checked_again(tmp_path, host, server):
    credentials = host_login(host, server)
    run = Run(tmp_path, "run", server)
    expire(run)
    assert run.claude.use() and "written back" in run.sync()
    written = credentials.read_bytes()
    attacker = FakeOAuth()  # another provider account altogether
    try:
        sandbox_relogin(run, attacker.login(lifetime=2 * HOUR))
        assert run.sync().startswith(ANOTHER)
        assert run.end() is None
        assert credentials.read_bytes() == written
    finally:
        attacker.close()


def test_an_expired_source_without_account_state_cannot_be_checked(tmp_path, host, server):
    credentials = host_login(host, server, account=False, lifetime=-HOUR)
    before = credentials.read_bytes()
    run = Run(tmp_path, "run", server)
    assert run.claude.use()  # refreshes the expired token: the same account, but the host's cannot be asked
    note = run.sync()
    assert note == f"{ANOTHER} to the host; the host keeps its own login"
    assert credentials.read_bytes() == before and server.lookups == []


def test_when_anthropic_cannot_be_asked_nothing_is_written_back_and_it_is_asked_again_a_minute_later(
        tmp_path, host, server, monkeypatch):
    credentials = host_login(host, server)
    before = credentials.read_bytes()
    calls = []

    def unreachable(token, timeout=None):
        calls.append(1)
        raise OSError("network is unreachable")
    monkeypatch.setattr(s, "claude_profile_account", unreachable)
    run = Run(tmp_path, "run", server)
    expire(run)
    assert run.claude.use()
    note = run.sync()
    assert note.startswith("could not ask Anthropic which account the sandbox's Claude login belongs to: "
                           "not written back to the host")
    assert credentials.read_bytes() == before and len(calls) == 1
    # Within the minute nobody asks again, however often the files change.
    for _ in range(5):
        assert run.sync() is None
    assert len(calls) == 1 and credentials.read_bytes() == before
    # A minute later: asked again (still failing: one more request, not one per look).
    run.state["account_retry"] = time.monotonic() - 1
    assert run.sync().startswith("could not ask Anthropic") and len(calls) == 2
    # Reachable again, still within the minute: the end of the run asks once more and writes back.
    monkeypatch.setattr(s, "claude_profile_account", fake_profile_account)
    assert run.sync() is None and credentials.read_bytes() == before
    assert run.end() == "refreshed Claude credentials written back to the host"
    assert s.claude_oauth(credentials)["refreshToken"] == run.claude.memory["refreshToken"]
    assert json.loads(credentials.read_text())["mcpOAuth"] == {"srv": {"accessToken": "m"}}


def test_the_watcher_asks_again_when_the_retry_is_due_without_a_change(tmp_path, host, server, monkeypatch):
    credentials = host_login(host, server)
    run = Run(tmp_path, "run", server)
    expire(run)
    assert run.claude.use()
    calls = []

    def flaky(token, timeout=None):
        calls.append(token)
        if len(calls) == 1:
            raise OSError("timed out")
        return fake_profile_account(token)
    monkeypatch.setattr(s, "claude_profile_account", flaky)
    monkeypatch.setattr(s, "CLAUDE_ACCOUNT_RETRY", 0.3)
    stop = threading.Event()
    watcher = threading.Thread(target=s.watch_claude_login, args=(run.home, run.pair, run.state, stop, 0.01))
    watcher.start()
    try:
        deadline = time.monotonic() + 10
        while s.claude_oauth(credentials)["refreshToken"] != run.claude.memory["refreshToken"] \
                and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        stop.set()
        watcher.join(10)
    assert s.claude_oauth(credentials)["refreshToken"] == run.claude.memory["refreshToken"]
    assert len(calls) == 2  # the failed lookup and one retry, not one per look every 10 ms
    assert run.state["notes"][0].startswith("could not ask Anthropic")
    assert run.state["notes"][-1] == "refreshed Claude credentials written back to the host"


def test_a_persistent_home_left_with_another_account_is_not_written_back_at_the_next_start(tmp_path, host, server):
    credentials = host_login(host, server)
    before = credentials.read_bytes()
    session = Run(tmp_path, "session", server, state_dir=True)
    other = server.login(lifetime=2 * HOUR)
    sandbox_relogin(session, other)  # then the launcher is killed: no end, no write-back
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        s.prepare_home(session.a, session.home)
    assert "another account" in err.getvalue() and "earlier run of this HOME" in err.getvalue()
    assert credentials.read_bytes() == before
    # The source is the authority at a start: the HOME gets the host's login again.
    assert s.claude_oauth(session.home / ".claude/.credentials.json")["refreshToken"] == \
        s.claude_oauth(credentials)["refreshToken"]
    assert not any(token in err.getvalue() for token in server.tokens())
    # The same account refreshed in a killed run still goes back.
    session.claude = FakeClaude(session.home, server)
    expire(session)
    assert session.claude.use()
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        s.prepare_home(session.a, session.home)
    assert "wrote it back now" in err.getvalue()
    assert s.claude_oauth(credentials)["refreshToken"] == session.claude.memory["refreshToken"]


def test_main_keeps_the_stored_login_when_the_sandbox_logs_in_with_another_account(tmp_path, host, server,
                                                                                    monkeypatch):
    stored = store_login(server.login())
    before = stored.read_bytes()
    monkeypatch.setattr(s, "CLAUDE_SYNC_INTERVAL", 0.02)
    monkeypatch.setattr(s.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(s.shutil, "which", lambda name: "/usr/bin/" + name)
    other = server.login(lifetime=2 * HOUR)

    def run_pod(a, session, home):
        path = home / ".claude/.credentials.json"
        path.write_text(json.dumps({"claudeAiOauth": other}))
        time.sleep(0.3)  # the watcher looks meanwhile
        return 0

    monkeypatch.setattr(s, "run_pod", run_pod)
    err, out = io.StringIO(), io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
        assert s.main(["claude", "--workspace", str(tmp_path / "w"), "--offline", "--no-tmux"]) == 0
    assert stored.read_bytes() == before
    assert f"{ANOTHER} to sandbox-ai's login store; sandbox-ai's login store keeps its own login" in err.getvalue()
    printed = err.getvalue() + out.getvalue()
    assert not any(token in printed for token in server.tokens())
