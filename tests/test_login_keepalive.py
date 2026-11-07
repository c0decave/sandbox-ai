"""The Claude login stays alive across runs, idle hours and sandboxes running at the same time.

Claude Code's OAuth login holds a short-lived access token and a refresh token that rotates: each refresh returns a
new pair and uses the old refresh token up. Whichever side refreshed last holds the only valid pair, so a refresh in
one sandbox must reach the source (host or `sandbox-ai login` store) and every other sandbox of that login before
they refresh with the used-up token. An OAuth server may also revoke the whole login when a used-up refresh token is
sent again (refresh token reuse detection, RFC 9700 section 4.14.2); then one stale sandbox logs out all of them.

Nothing here contacts a real endpoint: FakeOAuth is a local HTTP server with rotation and (optionally) reuse
detection, FakeClaude refreshes the way Claude Code 2.1.317 does (taken from its bundle: under its own lock it reads
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
    every token of its login (its family)."""

    def __init__(self, reuse_detection=True, strict=True):
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

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


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
    """`sandbox-ai login claude` without the sandbox: the login goes into the store."""
    login.write_store("claude", {s.AUTH_TARGETS["claude"]: json.dumps({"claudeAiOauth": oauth}).encode()})
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
    assert "written back" in first.end()
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
