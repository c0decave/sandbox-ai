"""sandbox-ai web (sandbox/webui.py): logins with password and TOTP, sessions, CSRF and same-origin checks, the
rate limit, the fixed set of actions (each a `sandbox-ai ARGS` with its arguments one by one), WebSocket framing,
and the whole service over real HTTPS: login, actions, and a terminal through a WebSocket into a real pty."""
import base64
import contextlib
import hashlib
import http.client
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("webui_for_tests", ROOT / "sandbox" / "webui.py")
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)
FAST = {"n": 1 << 10, "r": 8, "p": 1}  # quick scrypt for the tests (the real one: 2**15)


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # ~/.ssh: the keys adding and testing a host may change
    monkeypatch.setattr(w, "SCRYPT", FAST)
    return tmp_path


def add_user(name="admin", password="correct horse battery", now=None):
    secret = w.new_totp_secret()
    with w.users_locked() as users:
        users[name] = {"password": w.hash_password(password, **FAST), "totp": secret, "totp_counter": None}
    return secret


def code(secret, offset=0):
    return w.totp(secret, int(time.time() // 30) + offset)


HEADERS = {"Host": "localhost:8443", "Origin": "https://localhost:8443", "Content-Type": "application/json"}


class FakeRunner:
    """hosts.run as the Api calls it: records the arguments, answers from a table."""

    def __init__(self, answers=None):
        self.calls, self.answers = [], answers or {}

    def __call__(self, host, args, timeout=120, input=None):
        self.calls.append((host, args))
        for prefix, answer in self.answers.items():
            if tuple(args[:len(prefix)]) == prefix:
                return answer
        return 0, json.dumps({"host": host, "args": args}), ""


def service(runner=None, **kwargs):
    return w.Service({"localhost:8443"}, api=w.Api(runner or FakeRunner()), **kwargs)


def login(svc, secret, user="admin", password="correct horse battery", offset=0):
    status, headers, body = svc.handle("POST", "/api/login", HEADERS,
                                       json.dumps({"user": user, "password": password,
                                                   "code": code(secret, offset)}).encode(), "192.0.2.1")
    return status, dict(headers), json.loads(body)


def cookie_of(headers):
    value = headers["Set-Cookie"]
    assert "Secure" in value and "HttpOnly" in value and "SameSite=Strict" in value and value.startswith("__Host-")
    return value.split(";")[0]


# --- passwords and TOTP -------------------------------------------------------------------------------------

def test_rfc6238_vectors():
    """RFC 6238 appendix B, SHA1, 8 digits (the secret is ASCII 12345678901234567890)."""
    secret = base64.b32encode(b"12345678901234567890").decode()
    for when, expected in ((59, "94287082"), (1111111109, "07081804"), (1234567890, "89005924"),
                           (20000000000, "65353130")):
        assert w.totp(secret, when // 30, digits=8) == expected


def test_a_code_counts_once_and_only_near_now():
    secret = w.new_totp_secret()
    now = time.time()
    counter = w.check_totp(secret, w.totp(secret, int(now // 30)), None, now)
    assert counter == int(now // 30)
    assert w.check_totp(secret, w.totp(secret, counter), counter, now) is None, "the same code again"
    assert w.check_totp(secret, w.totp(secret, int(now // 30) - 2), None, now) is None, "too old"
    assert w.check_totp(secret, w.totp(secret, int(now // 30) + 1), None, now) == int(now // 30) + 1
    for bad in ("12345", "1234567", "abcdef", None, 123456):
        assert w.check_totp(secret, bad, None, now) is None


def test_passwords():
    record = w.hash_password("a long passphrase", **FAST)
    assert w.check_password("a long passphrase", record) and not w.check_password("a long passphrasE", record)
    assert not w.check_password("x" * 12, None), "an unknown user never logs in"
    assert not w.check_password("x", {"salt": "!!", "hash": "", "n": 2, "r": 1, "p": 1})
    assert "a long passphrase" not in json.dumps(record)


def test_otpauth_uri():
    assert w.otpauth_uri("ad min", "ABC") == ("otpauth://totp/sandbox-ai%3Aad%20min?secret=ABC&issuer=sandbox-ai"
                                             "&algorithm=SHA1&digits=6&period=30")


# --- the command line for logins ----------------------------------------------------------------------------

def run_cli(args, stdin):
    env = {**os.environ, "SANDBOX_AI_NO_DEPRECATION": "1"}
    return subprocess.run([sys.executable, "-I", str(ROOT / "sandbox/main.py"), "web", *args], input=stdin,
                          capture_output=True, text=True, env=env, timeout=60)


def test_user_add_needs_a_matching_code(home):
    # The code is computed from the secret the command prints: what a TOTP app does with it.
    process = subprocess.Popen([sys.executable, "-I", str(ROOT / "sandbox/main.py"), "web", "user", "add", "admin",
                                "--stdin"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, env={**os.environ, "SANDBOX_AI_NO_DEPRECATION": "1"})
    process.stdin.write("correct horse battery\ncorrect horse battery\n")
    process.stdin.flush()
    secret = None
    while secret is None:
        line = process.stdout.readline()
        assert line, process.stderr.read()
        if "or the secret" in line:
            secret = line.split("secret ")[1].split()[0]
    out, err = process.communicate(w.totp(secret, int(time.time() // 30)) + "\n", timeout=60)
    assert process.returncode == 0, err
    users = w.load_users()
    assert set(users) == {"admin"} and users["admin"]["totp"] == secret
    assert "correct horse battery" not in w.users_file().read_text()
    assert oct(w.users_file().stat().st_mode & 0o777) == "0o600"
    again = run_cli(["user", "add", "admin", "--stdin"], "x\n")
    assert again.returncode == 2 and "exists" in again.stderr
    short = run_cli(["user", "add", "other", "--stdin"], "short\nshort\n")
    assert short.returncode == 2 and "12 characters" in short.stderr
    wrong = run_cli(["user", "add", "other", "--stdin"], "correct horse battery\ncorrect horse battery\n000000\n")
    assert wrong.returncode == 2 and "does not match" in wrong.stderr and set(w.load_users()) == {"admin"}
    bad = run_cli(["user", "add", "Root;rm", "--stdin"], "")
    assert bad.returncode == 2 and "user name" in bad.stderr
    assert run_cli(["user", "remove", "admin"], "").returncode == 0 and w.load_users() == {}
    audit = [json.loads(line)["event"] for line in (w.state_dir() / "audit.jsonl").read_text().splitlines()]
    assert audit == ["user-added", "user-removed"]


def test_a_users_file_readable_by_others_is_refused(home):
    add_user()
    w.users_file().chmod(0o644)
    with pytest.raises(ValueError, match="readable by others"):
        w.load_users()


# --- logins, sessions, CSRF -------------------------------------------------------------------------------

def test_login_session_and_logout():
    secret = add_user()
    svc = service()
    status, headers, body = login(svc, secret)
    assert status == 200 and body["user"] == "admin" and len(body["csrf"]) > 30
    cookie = cookie_of(headers)
    me = svc.handle("GET", "/api/me", {**HEADERS, "Cookie": cookie}, b"", "192.0.2.1")
    assert me[0] == 200 and json.loads(me[2])["csrf"] == body["csrf"]
    assert svc.handle("GET", "/api/me", HEADERS, b"", "192.0.2.1")[0] == 401
    out = svc.handle("POST", "/api/logout", {**HEADERS, "Cookie": cookie, "X-CSRF-Token": body["csrf"]}, b"",
                     "192.0.2.1")
    assert out[0] == 200 and "Max-Age=0" in dict(out[1])["Set-Cookie"]
    assert svc.handle("GET", "/api/me", {**HEADERS, "Cookie": cookie}, b"", "192.0.2.1")[0] == 401
    events = [json.loads(line)["event"] for line in (w.state_dir() / "audit.jsonl").read_text().splitlines()]
    assert events == ["login", "logout"]


def test_every_part_of_a_login_is_needed():
    secret = add_user()
    svc = service()
    assert login(svc, secret, password="wrong password!!")[0] == 401
    assert login(svc, secret, user="nobody")[0] == 401
    status, _, body = svc.handle("POST", "/api/login", HEADERS, json.dumps(
        {"user": "admin", "password": "correct horse battery", "code": "000000"}).encode(), "192.0.2.9")
    assert status == 401 and body and json.loads(body)["error"] == "wrong user, password or code"
    assert login(svc, secret, offset=-3)[0] == 401, "an old code"
    status, _, body = login(svc, secret)
    assert status == 200
    assert login(service(), secret)[0] == 401, "a code is used once, also by another service"


def test_failed_logins_slow_down_per_address_and_user():
    secret = add_user()
    clock = [1000.0]
    svc = service(limiter=w.Limiter(free=3, first=30, maximum=900, clock=lambda: clock[0]))
    for _ in range(3):
        assert login(svc, secret, password="wrong password!!")[0] == 401
    status, headers, body = login(svc, secret)
    assert status == 429 and "Retry-After" in headers, "even the right password waits now"
    clock[0] += 31
    assert login(svc, secret, password="wrong password!!")[0] == 401
    assert login(svc, secret)[0] == 429, "the wait doubles"
    clock[0] += 61
    assert login(svc, secret)[0] == 200


def test_sessions_end_when_idle_and_after_their_lifetime():
    clock = [0.0]
    sessions = w.Sessions(idle=60, lifetime=300, clock=lambda: clock[0])
    token = sessions.new("admin", "192.0.2.1")
    for _ in range(5):
        clock[0] += 50
        assert sessions.get(token)
    clock[0] += 61
    assert sessions.get(token) is None, "idle"
    token = sessions.new("admin", "192.0.2.1")
    for _ in range(6):
        clock[0] += 50
    assert sessions.get(token) is None, "lifetime"
    assert sessions.get("") is None and sessions.get(None) is None


def test_changes_need_the_csrf_token_same_origin_json_and_a_known_host_name():
    secret = add_user()
    runner = FakeRunner()
    svc = service(runner)
    _, headers, body = login(svc, secret)
    cookie, csrf = cookie_of(headers), body["csrf"]
    ok = {**HEADERS, "Cookie": cookie, "X-CSRF-Token": csrf}
    path = "/api/hosts/local/starts/web/stop"
    assert svc.handle("POST", path, ok, b"{}", "192.0.2.1")[0] == 200
    assert runner.calls[-1] == ("local", ["stop", "web"])
    for changed, status in [({"X-CSRF-Token": "x"}, 403), ({"X-CSRF-Token": None}, 403),
                            ({"Origin": "https://evil.example"}, 403), ({"Origin": None}, 403),
                            ({"Content-Type": "text/plain"}, 415), ({"Host": "evil.example"}, 421),
                            ({"Cookie": None}, 401)]:
        sent = {k: v for k, v in {**ok, **changed}.items() if v is not None}
        assert svc.handle("POST", path, sent, b"{}", "192.0.2.1")[0] == status, changed
    assert len(runner.calls) == 1, "nothing refused reached a host"
    # A Referer of the same origin is enough when the browser sends no Origin.
    sent = {**{k: v for k, v in ok.items() if k != "Origin"}, "Referer": "https://localhost:8443/"}
    assert svc.handle("POST", path, sent, b"{}", "192.0.2.1")[0] == 200
    assert svc.handle("GET", "/", {"Host": "evil.example"}, b"", "192.0.2.1")[0] == 421, "DNS rebinding"


# --- the actions --------------------------------------------------------------------------------------------

@pytest.fixture
def logged_in():
    secret = add_user()
    runner = FakeRunner({("list",): (0, "[]", ""), ("approvals", "list"): (0, "[]", "")})
    svc = service(runner)
    _, headers, body = login(svc, secret)
    ok = {**HEADERS, "Cookie": cookie_of(headers), "X-CSRF-Token": body["csrf"]}

    def call(method, path, data=None):
        status, _, out = svc.handle(method, path, ok, json.dumps(data).encode() if data is not None else b"",
                                    "192.0.2.1")
        return status, json.loads(out)
    call.runner, call.svc = runner, svc
    return call


@pytest.mark.parametrize("method, path, data, args", [
    ("GET", "/api/hosts/local/starts", None, ["list", "--json"]),
    ("POST", "/api/hosts/ct1/starts/web/pause", {}, ["pause", "web"]),
    ("POST", "/api/hosts/ct1/starts/web/resume", {}, ["resume", "web"]),
    ("POST", "/api/hosts/ct1/starts/web/delete", {"confirm": True}, ["delete", "web", "--yes"]),
    ("GET", "/api/hosts/ct1/approvals", None, ["approvals", "list", "--json"]),
    ("POST", "/api/hosts/ct1/approvals/12-3-4", {"decision": "allow", "scope": "run"},
     ["approvals", "allow", "12-3-4", "--scope", "run"]),
    ("POST", "/api/hosts/ct1/approvals/12-3-4", {"decision": "deny"}, ["approvals", "deny", "12-3-4"]),
    ("GET", "/api/hosts/ct1/doctor", None, ["doctor", "--json"]),
    ("GET", "/api/hosts/ct1/logs", None, ["logs", "--json"]),
    ("GET", "/api/hosts/ct1/llm", None, ["llm", "list", "--json"]),
    ("POST", "/api/hosts/ct1/llm", {"name": "lmstudio", "kind": "lmstudio", "url": "http://192.0.2.20:1234",
                                    "model": "qwen3"},
     ["llm", "add", "lmstudio", "--kind", "lmstudio", "--url", "http://192.0.2.20:1234", "--model", "qwen3"]),
    ("POST", "/api/hosts/ct1/llm/lmstudio/check", {"agent": "opencode"}, ["llm", "check", "lmstudio", "--agent",
                                                                           "opencode"]),
    ("GET", "/api/hosts/ct1/llm/lmstudio/models", None, ["llm", "models", "lmstudio", "--json"]),
    ("DELETE", "/api/hosts/ct1/llm/lmstudio", None, ["llm", "remove", "lmstudio"]),
    ("GET", "/api/hosts/ct1/defaults", None, ["defaults", "show", "--json"]),
    ("POST", "/api/hosts/ct1/defaults", {"key": "telemetry", "value": "strict"},
     ["defaults", "set", "telemetry", '"strict"']),
    ("POST", "/api/hosts/ct1/defaults", {"key": "telemetry", "value": None}, ["defaults", "unset", "telemetry"]),
    # the network of a running sandbox: every value as --flag=VALUE, the token never (only where it is)
    ("GET", "/api/hosts/ct1/net/web", None, ["net", "show", "web", "--json"]),
    ("POST", "/api/hosts/ct1/net/web", {"op": "allow", "allow_host": ["pypi.org"], "allow_tcp": ["192.0.2.9:5432"],
                                        "host_ports": [3000], "dns": True,
                                        "git_login": {"url": "http://localhost:3080", "kind": "secret", "ref": "GITEA"}},
     ["net", "allow", "web", "--allow-host=pypi.org", "--allow-tcp=192.0.2.9:5432", "--allow-host-port=3000", "--dns",
      "--git-token-secret=http://localhost:3080=GITEA"]),
    ("POST", "/api/hosts/ct1/net/web", {"op": "revoke", "host_ports": [3000]},
     ["net", "revoke", "web", "--allow-host-port=3000"]),
    # host wildcards as `net allow` takes them (*.DOMAIN.TLD), unchanged: the command refuses public suffixes
    ("POST", "/api/hosts/ct1/net/web", {"op": "allow", "allow_host": ["*.example.org", "*.api.Example.co.uk"]},
     ["net", "allow", "web", "--allow-host=*.example.org", "--allow-host=*.api.Example.co.uk"]),
    ("POST", "/api/hosts/ct1/net/web", {"op": "revoke", "allow_host": ["*.example.org"]},
     ["net", "revoke", "web", "--allow-host=*.example.org"]),
    ("GET", "/api/hosts/ct1/presets", None, ["presets", "list", "--json"]),
    ("GET", "/api/hosts/ct1/vpn", None, ["vpn", "list", "--json"]),
    ("POST", "/api/hosts/ct1/vpn", {"name": "home", "kind": "wireguard", "file": "/etc/wireguard/home.conf"},
     ["vpn", "add", "home", "--wireguard=/etc/wireguard/home.conf"]),
    ("POST", "/api/hosts/ct1/vpn", {"name": "ts", "kind": "tailscale", "secret": "TS_KEY", "exit_node": "exit-1",
                                    "login_server": "https://hs.example.org"},
     ["vpn", "add", "ts", "--tailscale", "--auth-key-secret=TS_KEY", "--exit-node=exit-1",
      "--login-server=https://hs.example.org"]),
    ("POST", "/api/hosts/ct1/vpn", {"name": "mv", "kind": "mullvad", "secret": "MULLVAD", "location": "se-sto"},
     ["vpn", "add", "mv", "--mullvad", "--account-secret=MULLVAD", "--location=se-sto"]),
    ("POST", "/api/hosts/ct1/vpn/home/check", {}, ["vpn", "check", "home"]),
    ("DELETE", "/api/hosts/ct1/vpn/home", None, ["vpn", "remove", "home"]),
    # kits: the values after "--", options as --flag=VALUE
    ("GET", "/api/hosts/ct1/kits", None, ["kits", "list", "--json"]),
    ("POST", "/api/hosts/ct1/kits", {"name": "team", "agents": ["claude", "codex"]},
     ["kits", "new", "--agent=claude", "--agent=codex", "--", "team"]),
    ("POST", "/api/hosts/ct1/kits/team/add-skill", {"path": "/home/u/skills/review", "as": "review2",
                                                    "agents": ["claude"]},
     ["kits", "add-skill", "--as=review2", "--agent=claude", "--", "team", "/home/u/skills/review"]),
    ("POST", "/api/hosts/ct1/kits/team/new-skill", {"skill": "notes", "description": "-when writing notes"},
     ["kits", "new-skill", "--description=-when writing notes", "--", "team", "notes"]),
    ("POST", "/api/hosts/ct1/kits/team/add-plugin", {"plugin": "egm@sandbox-ai", "agent": "claude",
                                                     "source": "sandbox-ai"},
     ["kits", "add-plugin", "--agent=claude", "--source=sandbox-ai", "--", "team", "egm@sandbox-ai"]),
    ("POST", "/api/hosts/ct1/kits/team/add-plugin", {"plugin": "@scope/pkg@1.2", "agent": "opencode"},
     ["kits", "add-plugin", "--agent=opencode", "--", "team", "@scope/pkg@1.2"]),
    ("POST", "/api/hosts/ct1/kits/team/add-marketplace", {"marketplace": "tools", "source": "owner/repo"},
     ["kits", "add-marketplace", "--", "team", "tools", "owner/repo"]),
    ("POST", "/api/hosts/ct1/kits/team/remove-skill", {"skill": "notes", "agents": ["codex"]},
     ["kits", "remove-skill", "--agent=codex", "--", "team", "notes"]),
    ("POST", "/api/hosts/ct1/kits/team/remove-plugin", {"plugin": "egm@sandbox-ai"},
     ["kits", "remove-plugin", "--", "team", "egm@sandbox-ai"]),
    ("DELETE", "/api/hosts/ct1/kits/team", {"confirm": True}, ["kits", "delete", "--yes", "--", "team"]),
])
def test_each_action_is_one_fixed_command(logged_in, method, path, data, args):
    status, out = logged_in(method, path, data)
    assert status == 200, out
    assert logged_in.runner.calls[-1][1] == args and logged_in.runner.calls[-1][0] == path.split("/")[3]


@pytest.mark.parametrize("method, path, data, status", [
    ("GET", "/api/hosts/Bad;id/starts", None, 400),
    ("GET", "/api/hosts/ct1/nothing", None, 404),
    ("PUT", "/api/hosts/ct1/starts", None, 405),
    ("POST", "/api/hosts/ct1/starts/--all/stop", {}, 400),
    ("POST", "/api/hosts/ct1/starts/web/delete", {}, 400),
    ("POST", "/api/hosts/ct1/starts/web/exec", {}, 404),
    ("POST", "/api/hosts/ct1/approvals/a b", {"decision": "allow"}, 400),
    # Regression: an id with a leading "-" reached the host, read there as an option (-h answered with the help).
    ("POST", "/api/hosts/ct1/approvals/--scope", {"decision": "allow", "scope": "always"}, 400),
    ("POST", "/api/hosts/ct1/approvals/-h", {"decision": "deny", "scope": "once"}, 400),
    ("POST", "/api/hosts/ct1/approvals/1-2", {"decision": "maybe"}, 400),
    ("POST", "/api/hosts/ct1/approvals/1-2", {"decision": "allow", "scope": "forever"}, 400),
    ("POST", "/api/hosts/ct1/llm", {"name": "--replace", "kind": "lmstudio", "url": "http://x"}, 400),
    ("POST", "/api/hosts/ct1/llm", {"name": "a", "kind": "lmstudio", "url": "http://x", "model": "-x"}, 400),
    ("POST", "/api/hosts/ct1/llm/a/check", {"agent": "bash"}, 400),
    ("POST", "/api/hosts/ct1/defaults", {"key": "--x", "value": 1}, 400),
    ("POST", "/api/hosts/ct1/net/web", {"op": "allow"}, 400),  # nothing to change
    ("POST", "/api/hosts/ct1/net/web", {"op": "drop", "dns": True}, 400),
    ("POST", "/api/hosts/ct1/net/--all", {"op": "allow", "dns": True}, 400),
    ("POST", "/api/hosts/ct1/net/web", {"op": "allow", "allow_host": ["-x"]}, 400),
    ("POST", "/api/hosts/ct1/net/web", {"op": "allow", "allow_host": ["a b"]}, 400),
    *[("POST", "/api/hosts/ct1/net/web", {"op": "allow", "allow_host": [bad]}, 400)
      for bad in ("*.org", "*", "*.", "**.example.org", "a.*.example.org", "*.-x.org", "*.example.org.",
                  "*.example.123", "*.exa mple.org", "*example.org", "-*.example.org")],
    ("POST", "/api/hosts/ct1/net/web", {"op": "allow", "allow_tcp": ["192.0.2.9"]}, 400),
    ("POST", "/api/hosts/ct1/net/web", {"op": "allow", "host_ports": [0]}, 400),
    ("POST", "/api/hosts/ct1/net/web", {"op": "allow", "host_ports": ["3000"]}, 400),
    ("POST", "/api/hosts/ct1/net/web", {"op": "allow", "host_ports": [True]}, 400),
    ("POST", "/api/hosts/ct1/net/web", {"op": "allow", "git_login": {"url": "http://x", "kind": "file",
                                                                     "ref": "/etc/shadow"}}, 400),
    ("POST", "/api/hosts/ct1/net/web", {"op": "allow", "git_login": {"url": "http://x", "kind": "env",
                                                                     "ref": "glpat-the-token-itself"}}, 400),
    ("POST", "/api/hosts/ct1/net/web", {"op": "allow", "git_login": {"url": "-x", "kind": "env", "ref": "A"}}, 400),
    ("POST", "/api/hosts/ct1/vpn", {"name": "Home", "kind": "wireguard", "file": "/x"}, 400),
    ("POST", "/api/hosts/ct1/vpn", {"name": "home", "kind": "ipsec"}, 400),
    ("POST", "/api/hosts/ct1/vpn", {"name": "home", "kind": "wireguard", "file": "relative.conf"}, 400),
    ("POST", "/api/hosts/ct1/vpn", {"name": "home", "kind": "wireguard", "file": "/x", "secret": "K"}, 400),
    ("POST", "/api/hosts/ct1/vpn", {"name": "ts", "kind": "tailscale", "secret": "tskey-auth-the-key-itself"}, 400),
    ("POST", "/api/hosts/ct1/vpn", {"name": "ts", "kind": "tailscale", "location": "se"}, 400),
    ("POST", "/api/hosts/ct1/vpn", {"name": "ts", "kind": "tailscale", "login_server": "http://hs"}, 400),
    ("POST", "/api/hosts/ct1/vpn/--all/check", {}, 400),
    ("DELETE", "/api/hosts/ct1/vpn/a%20b", None, 400),
    ("POST", "/api/hosts/ct1/kits", {"name": "-rf"}, 400),
    ("POST", "/api/hosts/ct1/kits", {"name": "default"}, 400),
    ("POST", "/api/hosts/ct1/kits", {"name": "team", "agents": ["bash"]}, 400),
    ("POST", "/api/hosts/ct1/kits/team/add-skill", {"path": "relative/skill"}, 400),
    ("POST", "/api/hosts/ct1/kits/team/add-skill", {"path": "/x", "as": "../up"}, 400),
    ("POST", "/api/hosts/ct1/kits/--yes/add-skill", {"path": "/x"}, 400),
    ("POST", "/api/hosts/ct1/kits/team/new-skill", {"skill": "Bad Name"}, 400),
    ("POST", "/api/hosts/ct1/kits/team/new-skill", {"skill": "ok", "description": "two\nlines"}, 400),
    ("POST", "/api/hosts/ct1/kits/team/add-plugin", {"plugin": "a b"}, 400),
    ("POST", "/api/hosts/ct1/kits/team/add-plugin", {"plugin": "-x"}, 400),
    ("POST", "/api/hosts/ct1/kits/team/add-skill", {"path": "/x", "create": "yes"}, 400),
    ("POST", "/api/hosts/ct1/kits/team/trust-hooks", {}, 404),
    ("DELETE", "/api/hosts/ct1/kits/team", None, 400),
])
def test_what_is_refused_never_reaches_a_host(logged_in, method, path, data, status):
    assert logged_in(method, path, data)[0] == status
    assert logged_in.runner.calls == []


def test_adding_to_a_new_kit_creates_it_first(logged_in):
    """What is added while a built-in kit is chosen goes to a kit of yours: with create, a missing one is made
    first; an existing one is not made again."""
    logged_in.runner.answers[("kits", "list")] = (0, json.dumps({"kits": [{"name": "mine", "kind": "user"}]}), "")
    status, out = logged_in("POST", "/api/hosts/ct1/kits/team/new-skill", {"skill": "notes", "create": True})
    assert status == 200, out
    assert [args for _, args in logged_in.runner.calls[1:]] == [["kits", "new", "--", "team"],
                                                                ["kits", "new-skill", "--", "team", "notes"]]
    del logged_in.runner.calls[:]
    status, out = logged_in("POST", "/api/hosts/ct1/kits/mine/new-skill", {"skill": "notes", "create": True})
    assert status == 200 and [args[1] for _, args in logged_in.runner.calls] == ["list", "new-skill"]
    # When creating fails, nothing more runs.
    logged_in.runner.answers[("kits", "new")] = (1, "", "sandbox-ai kits: no space left on device")
    del logged_in.runner.calls[:]
    status, out = logged_in("POST", "/api/hosts/ct1/kits/other/add-skill", {"path": "/x", "create": True})
    assert status == 502 and "no space left" in out["error"]
    assert [args[1] for _, args in logged_in.runner.calls] == ["list", "new"]


def test_kits_end_to_end_through_the_real_command(tmp_path, monkeypatch):
    """The web's kit actions against the real `sandbox-ai kits` (a private HOME): a new kit, a skill copied from a
    folder (its symlink not followed), a new skill from the template, a refusal shown as the command says it."""
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "HOME": str(home)}
    for name in ("XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
        env.pop(name, None)

    def runner(host, args, timeout=120, input=None):
        done = subprocess.run([sys.executable, "-I", str(ROOT / "sandbox" / "main.py"), *args], env=env,
                              capture_output=True, text=True, timeout=timeout)
        return done.returncode, done.stdout, done.stderr
    api = w.Api(runner)
    skill = tmp_path / "review"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: review\ndescription: reviews\n---\n")
    (skill / "secret").symlink_to("/etc/hostname")
    out = api.dispatch("POST", "/api/hosts/local/kits/team/add-skill", {"path": str(skill), "create": True,
                                                                         "agents": ["claude"]})["output"]
    assert "kit team: created" in out and "skill review copied for claude" in out and "symlink" in out
    kit = home / ".local" / "share" / "sandbox-ai" / "kits" / "team"
    assert (kit / "claude" / "skills" / "review" / "SKILL.md").is_file()
    assert not os.path.lexists(kit / "claude" / "skills" / "review" / "secret")
    api.dispatch("POST", "/api/hosts/local/kits/team/new-skill", {"skill": "notes", "description": "when noting",
                                                                   "agents": ["claude"]})
    assert 'description: "when noting"' in (kit / "claude" / "skills" / "notes" / "SKILL.md").read_text()
    names = {k["name"]: k for k in api.dispatch("GET", "/api/hosts/local/kits", {})["kits"]}
    assert names["team"]["kind"] == "user" and names["team"]["agents"]["claude"]["skills"] == ["notes", "review"]
    with pytest.raises(w.Refused) as refused:
        api.dispatch("POST", "/api/hosts/local/kits/team/add-skill", {"path": str(tmp_path / "missing")})
    assert refused.value.status == 502 and "does not exist" in str(refused.value)
    api.dispatch("DELETE", "/api/hosts/local/kits/team", {"confirm": True})
    assert not kit.exists()


def test_the_page_has_a_kits_page_that_adds_to_a_kit_of_yours():
    script = (Path(w.__file__).parent / "web" / "app.js").read_text()
    page = script.split("pages.kits = ")[1].split("pages.settings")[0]
    assert '["kits", "Kits"]' in script and 'hostPath("/kits")' in script
    for action in ("add-skill", "new-skill", "add-plugin", "remove-${kind}"):
        assert action in page
    assert "create: fresh" in page and "a new kit…" in page and "innerHTML" not in page


def test_a_failing_host_says_why(logged_in):
    logged_in.runner.answers[("doctor",)] = (255, "", "ssh: connect to host ct1 port 22: Connection refused\x1b[2J")
    status, out = logged_in("GET", "/api/hosts/ct1/doctor")
    assert status == 502 and "Connection refused" in out["error"] and "\x1b" not in out["error"]


def test_hosts_are_added_tested_and_removed(logged_in):
    status, out = logged_in("POST", "/api/hosts", {"name": "ct1", "ssh": "root@192.0.2.10", "port": 2222})
    assert status == 200 and {k: out[k] for k in ("name", "ssh", "port")} == {"name": "ct1", "ssh": "root@192.0.2.10", "port": 2222}
    assert logged_in("POST", "/api/hosts", {"name": "ct1", "ssh": "root@x"})[0] == 400
    assert logged_in("POST", "/api/hosts", {"name": "ct2", "ssh": "-oProxyCommand=id"})[0] == 400
    status, hosts = logged_in("GET", "/api/hosts")
    assert [h["name"] for h in hosts] == ["local", "ct1"]
    logged_in.runner.answers[("--version",)] = (0, "sandbox-ai 0.1.31\n", "")
    logged_in.runner.answers[("doctor",)] = (0, "[]", "")
    status, result = logged_in("POST", "/api/hosts/ct1/test")
    assert result["ssh"] and result["version"] == "sandbox-ai 0.1.31" and result["doctor"] == []
    assert logged_in("DELETE", "/api/hosts/ct1")[0] == 200
    assert logged_in("DELETE", "/api/hosts/ct1")[0] == 404
    events = [json.loads(line) for line in (w.state_dir() / "audit.jsonl").read_text().splitlines()]
    assert [e["path"] for e in events if e["event"] == "action"][:1] == ["/api/hosts"]


def test_the_audit_log_keeps_no_secrets(logged_in):
    logged_in("POST", "/api/hosts/ct1/llm", {"name": "a", "kind": "lmstudio", "url": "http://x",
                                             "api_key": "sk-secret"})
    assert "sk-secret" not in (w.state_dir() / "audit.jsonl").read_text()


def test_static_files_only_the_listed_ones(logged_in):
    svc = logged_in.svc
    status, headers, body = svc.handle("GET", "/", {"Host": "localhost:8443"}, b"", "192.0.2.1")
    assert status == 200 and b"/static/app.js" in body and dict(headers)["Content-Type"].startswith("text/html")
    for path in ("/static/app.js", "/static/app.css", "/static/themes.json", "/static/icon.svg"):
        assert svc.handle("GET", path, {"Host": "localhost:8443"}, b"", "192.0.2.1")[0] == 200, path
    for path in ("/static/../webui.py", "/static/webui.py", "/static/vendor/../../hosts.py", "/etc/passwd"):
        assert svc.handle("GET", path, {"Host": "localhost:8443"}, b"", "192.0.2.1")[0] == 404, path
    themes = json.loads((ROOT / "sandbox/web/themes.json").read_text())["themes"]
    assert {"light", "dark", "hacker"} <= set(themes) and all("colors" in t for t in themes.values())


def test_terminal_commands():
    hosts = w.module("hosts")
    host, argv = w.terminal_command({"host": "local", "kind": "attach", "name": "web"})
    assert host == "local" and argv[-2:] == ["attach", "web"]
    host, argv = w.terminal_command({"host": "local", "kind": "start", "agent": "codex", "name": "n1",
                                     "workspace": "/srv/app", "options": json.dumps(["--mode", "high"])})
    assert argv[-7:] == ["codex", "--name", "n1", "--workspace", "/srv/app", "--mode", "high"]
    for query in ({"host": "local", "kind": "shell"}, {"host": "local", "kind": "start", "agent": "bash"},
                  {"host": "local", "kind": "attach", "name": "--all"},
                  {"host": "local", "kind": "start", "agent": "claude", "workspace": "relative"},
                  {"host": "x;y", "kind": "attach", "name": "web"},
                  {"host": "local", "kind": "start", "agent": "claude", "options": json.dumps([1])}):
        with pytest.raises(w.Refused):
            w.terminal_command(query)
    assert hosts.LOCAL == "local"


# --- WebSocket framing --------------------------------------------------------------------------------------

class Pipe:
    def __init__(self, data=b""):
        self.data, self.out = bytearray(data), bytearray()

    def read(self, count):
        chunk = bytes(self.data[:count])
        del self.data[:count]
        return chunk

    def write(self, data):
        self.out += data

    def flush(self):
        pass


def frame(payload, opcode=2, fin=True, mask=b"\x01\x02\x03\x04", masked=True):
    header = bytes([(0x80 if fin else 0) | opcode])
    length = len(payload)
    flag = 0x80 if masked else 0
    if length < 126:
        header += bytes([flag | length])
    elif length < 65536:
        header += bytes([flag | 126]) + struct.pack(">H", length)
    else:
        header += bytes([flag | 127]) + struct.pack(">Q", length)
    if not masked:
        return header + payload
    return header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))


def test_websocket_messages():
    big = os.urandom(70000)
    pipe = Pipe(frame(b"hello", 1) + frame(b"ab", 2, fin=False) + frame(b"cd", 0) + frame(b"!", 9) + frame(big)
                + frame(b"\x03\xe8", 8))
    ws = w.WebSocket(pipe)
    assert ws.receive() == (1, b"hello")
    assert ws.receive() == (2, b"abcd"), "fragments put together"
    assert ws.receive() == (2, big), "a ping in between was answered"
    assert pipe.out.startswith(b"\x8a\x01!"), "the pong"
    assert ws.receive()[0] == 8 and ws.closed


@pytest.mark.parametrize("data, code", [
    (frame(b"x", masked=False), 1002),                 # a browser always masks
    (frame(b"x", 0), 1002),                            # a continuation of nothing
    (frame(b"x" * 126, 9), 1002),                      # a control frame too long
    (bytes([0x82, 0xFF]) + struct.pack(">Q", 1 << 40) + b"\0" * 4, 1009),   # too big
    (bytes([0xC2, 0x81]) + b"\0" * 5, 1002),           # a reserved bit
])
def test_websocket_refuses_what_is_wrong(data, code):
    pipe = Pipe(data)
    ws = w.WebSocket(pipe)
    with pytest.raises(ConnectionError):
        ws.receive()
    assert pipe.out[0] == 0x88 and pipe.out[1] == len(pipe.out) - 2 <= 125
    assert struct.unpack(">H", pipe.out[2:4])[0] == code


def test_a_close_frame_carries_its_reason_within_a_control_frame():
    """The reason is what the page shows: UTF-8, cut to the 123 bytes a close frame has room for (never in the middle
    of a character), the code first."""
    pipe = Pipe()
    w.WebSocket(pipe).close(1008, "ä" * 100)
    assert pipe.out[0] == 0x88 and pipe.out[1] == len(pipe.out) - 2 <= 125
    assert struct.unpack(">H", pipe.out[2:4])[0] == 1008 and set(pipe.out[4:].decode()) == {"ä"}
    pipe = Pipe(bytes([0x82, 0xFF]) + struct.pack(">Q", 1 << 40) + b"\0" * 4)
    with pytest.raises(ConnectionError):
        w.WebSocket(pipe).receive()
    assert b"larger than 1 MiB" in pipe.out


# --- the whole service over HTTPS -------------------------------------------------------------------------

@pytest.fixture
def server(home, monkeypatch):
    """The real service on 127.0.0.1 (a free port) with its own certificate; the terminal runs a stand-in."""
    if not shutil.which("openssl"):
        pytest.skip("needs openssl")
    secret = add_user()
    script = home / "fake-terminal"
    script.write_text(f"#!{sys.executable}\nimport os, sys\nprint('tty' if os.isatty(0) else 'no tty', flush=True)\n"
                      "print('size', os.get_terminal_size(0), flush=True)\n"
                      "line = sys.stdin.readline()\nprint('got', line.strip(), flush=True)\n")
    script.chmod(0o755)
    monkeypatch.setattr(w, "terminal_command", lambda query: ("local", [str(script), query.get("name", "")]))
    started, holder = threading.Event(), {}

    def ready(srv):
        holder["server"] = srv
        started.set()
    thread = threading.Thread(target=w.serve, kwargs={"listen": "127.0.0.1:0", "out": lambda line: None,
                                                      "ready": ready}, daemon=True)
    thread.start()
    assert started.wait(30)
    port = holder["server"].server_address[1]
    context = ssl.create_default_context(cafile=str(w.cert_files()[0]))
    yield {"port": port, "secret": secret, "context": context, "server": holder["server"]}
    holder["server"].shutdown()


def https(server, method, path, body=None, headers=None):
    connection = http.client.HTTPSConnection("localhost", server["port"], context=server["context"], timeout=30)
    sent = {"Origin": f"https://localhost:{server['port']}", **(headers or {})}
    if body is not None:
        sent["Content-Type"] = "application/json"
    connection.request(method, path, body=json.dumps(body) if body is not None else None, headers=sent)
    response = connection.getresponse()
    data = response.read()
    connection.close()
    return response.status, dict(response.getheaders()), data


def test_over_https_login_and_an_action(server):
    status, headers, body = https(server, "GET", "/")
    assert status == 200 and headers["Content-Security-Policy"].startswith("default-src 'self'")
    assert headers["X-Frame-Options"] == "DENY" and "max-age" in headers["Strict-Transport-Security"]
    assert https(server, "GET", "/api/me")[0] == 401
    status, headers, body = https(server, "POST", "/api/login", {"user": "admin", "password": "correct horse battery",
                                                                 "code": code(server["secret"])})
    assert status == 200, body
    cookie, csrf = headers["Set-Cookie"].split(";")[0], json.loads(body)["csrf"]
    status, _, body = https(server, "GET", "/api/hosts", headers={"Cookie": cookie})
    assert status == 200 and json.loads(body)[0]["name"] == "local"
    assert json.loads(w.listen_file().read_text())["port"] == server["port"] and w.web_port() == server["port"]
    # Plain HTTP gets nothing.
    with socket.create_connection(("127.0.0.1", server["port"]), timeout=10) as plain:
        plain.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
        try:
            assert b"200 OK" not in plain.recv(100)
        except (ConnectionError, socket.timeout):
            pass
    # A body that is too big is refused before it is read.
    status, _, _ = https(server, "POST", "/api/logout", headers={"Cookie": cookie, "X-CSRF-Token": csrf,
                                                                 "Content-Length": str(w.BODY_LIMIT + 1)})
    assert status in (413, 400)


def websocket(server, query, cookie, origin=None):
    raw = socket.create_connection(("127.0.0.1", server["port"]), timeout=30)
    sock = server["context"].wrap_socket(raw, server_hostname="localhost")
    key = base64.b64encode(os.urandom(16)).decode()
    sock.sendall((f"GET /api/terminal?{query} HTTP/1.1\r\nHost: localhost:{server['port']}\r\n"
                  f"Origin: {origin or 'https://localhost:%d' % server['port']}\r\nCookie: {cookie}\r\n"
                  f"Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                  "Sec-WebSocket-Version: 13\r\n\r\n").encode())
    response = b""
    while b"\r\n\r\n" not in response:
        chunk = sock.recv(4096)
        if not chunk:
            break
        response += chunk
    head, _, rest = response.partition(b"\r\n\r\n")
    return sock, head.decode(), rest, key


def read_messages(sock, rest, until, timeout=20):
    data, text, deadline = bytearray(rest), b"", time.monotonic() + timeout
    while until not in text and time.monotonic() < deadline:
        while len(data) >= 2:
            length, start = data[1] & 0x7F, 2
            if length == 126:
                if len(data) < 4:
                    break
                length, start = struct.unpack(">H", data[2:4])[0], 4
            if len(data) < start + length:
                break
            opcode = data[0] & 0x0F
            if opcode == 2:
                text += bytes(data[start:start + length])
            del data[:start + length]
        if until in text:
            break
        chunk = sock.recv(65536)
        if not chunk:
            break
        data += chunk
    return text


def test_a_terminal_over_a_websocket(server):
    status, headers, body = https(server, "POST", "/api/login", {"user": "admin", "password": "correct horse battery",
                                                                 "code": code(server["secret"])})
    cookie, csrf = headers["Set-Cookie"].split(";")[0], json.loads(body)["csrf"]
    # Without the CSRF token, from another origin, without the cookie: no terminal.
    for query, sent_cookie, origin in [("host=local&kind=attach&name=web", cookie, None),
                                       (f"host=local&kind=attach&name=web&csrf={csrf}", cookie, "https://evil.example"),
                                       (f"host=local&kind=attach&name=web&csrf={csrf}", "x=y", None)]:
        sock, head, _, _ = websocket(server, query, sent_cookie, origin)
        assert not head.startswith("HTTP/1.1 101"), head
        sock.close()
    sock, head, rest, key = websocket(server, f"host=local&kind=attach&name=web&csrf={csrf}&cols=120&rows=40", cookie)
    assert head.startswith("HTTP/1.1 101"), head
    accept = base64.b64encode(hashlib.sha1((key + w.WS_GUID).encode()).digest()).decode()
    assert f"Sec-WebSocket-Accept: {accept}" in head
    text = read_messages(sock, rest, b"lines=40")
    assert b"tty" in text and b"no tty" not in text and b"columns=120, lines=40" in text, text
    sock.sendall(frame(b"hello from the browser\r", 2))
    text = read_messages(sock, b"", b"got hello from the browser")
    assert b"got hello from the browser" in text, text
    # The command ended: the service closes the WebSocket (a close frame, then the connection).
    rest = b""
    try:
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            rest += chunk
    except (ConnectionError, socket.timeout, ssl.SSLError):
        pass
    assert b"\x88\x02\x03\xe8" in rest, rest
    sock.close()


def test_the_terminals_page_lists_every_running_start():
    """Terminals: every running start of the host (however it began), attachable ones open with one click (one tab
    per start), the others say why (list --json's attach_why); refreshed with the page's poll."""
    script = (Path(w.__file__).parent / "web" / "app.js").read_text()
    page = script.split("// --- terminals")[1]
    assert "refreshTerminalStarts" in page and 'hostPath("/starts")' in page and "s.attachable" in page
    assert 'if (state.page === "terminals") refreshTerminalStarts(' in script
    for why in ("foreground", "paused", "mcp", "stopped", "damaged"):
        assert f"  {why}: " in page
    assert "--no-tmux" in page and "t.key === key && !t.closed" in page


def test_the_page_offers_the_network_of_a_start_and_the_presets():
    script = (Path(w.__file__).parent / "web" / "app.js").read_text()
    assert "/net/${encodeURIComponent(name)}" in script and 'hostPath("/presets")' in script
    assert "--preset ${chosen.id}" in script and "git_login" in script and "host_ports" in script


def test_the_page_has_a_vpn_page_that_sends_only_names_of_secrets():
    script = (Path(w.__file__).parent / "web" / "app.js").read_text()
    assert '["vpn", "VPN profiles"]' in script and "pages.vpn" in script and 'hostPath("/vpn")' in script
    assert "no key passes through this page" in script and 'type: "password"' not in script.split("pages.vpn")[1].split("pages.llm")[0]


# -- notifications: themed toasts instead of alert() --

WEB = ROOT / "sandbox" / "web"
THEMES = json.loads((WEB / "themes.json").read_text())["themes"]


def luminance(color):
    color = color.lstrip("#")
    if len(color) == 3:
        color = "".join(c * 2 for c in color)

    def channel(i):
        value = int(color[i:i + 2], 16) / 255
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4
    return 0.2126 * channel(0) + 0.7152 * channel(2) + 0.0722 * channel(4)


def contrast(a, b):
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def badge_sign(background, colors):
    """The colour app.js gives the sign on a toast's badge: the most readable of these on BACKGROUND."""
    candidates = [colors["accent_text"], colors["base"], colors["text"], "#000000", "#ffffff"]
    return max(candidates, key=lambda c: contrast(c, background))


def test_the_page_shows_results_and_errors_as_toasts_not_alerts():
    script = (WEB / "app.js").read_text()
    css = (WEB / "app.css").read_text()
    assert "alert(" not in script, "errors and results are toasts in the theme, not the browser's dialog"
    assert script.count("confirm(") == 6, "confirmations stay confirm()"
    assert "function toast(level, title, text, options)" in script and 'role: level === "error" ? "alert" : "status"' in script
    assert ".innerHTML" not in script and "insertAdjacentHTML" not in script and "outerHTML" not in script
    for level, colour in (("success", "--ok"), ("warning", "--warn"), ("error", "--bad")):
        assert f".toast.{level} {{ --level: var({colour});" in css, level
    assert ".notice.warn" in css


@pytest.mark.parametrize("name", sorted(THEMES))
def test_toasts_are_readable_in_every_theme(name):
    """The toast's background is the theme's base colour: title and text in its text colours, the level as bar,
    border and badge (3:1 for those against the base, as for controls), the badge's sign at 4.5:1 on the badge."""
    colors, status = THEMES[name]["colors"], THEMES[name]["status"]
    assert contrast(colors["text"], colors["base"]) >= 4.5 and contrast(colors["secondary"], colors["base"]) >= 4.5
    levels = {"info": colors["accent"], "success": status["ok"], "warning": status["warn"], "error": status["bad"]}
    for level, colour in levels.items():
        assert contrast(colour, colors["base"]) >= 3.0, (name, level, colour)
        assert contrast(badge_sign(colour, colors), colour) >= 4.5, (name, level, colour)
    assert len({levels["info"], levels["warning"], levels["error"]}) == 3, "the levels look different"
    for level in ("warning", "error"):  # the inline notices colour their text with the level's colour
        assert contrast(levels[level], colors["base"]) >= 4.5, (name, level)


TOAST_DRIVER = r"""
const fs = require("fs"), vm = require("vm");
class Node {
  constructor(tag) { this.tagName = tag; this.children = []; this.attributes = {}; this.listeners = {}; this.parent = null;
    this.className = ""; this.text = ""; }
  setAttribute(k, v) { this.attributes[k] = String(v); if (k === "id") this.id = String(v); }
  getAttribute(k) { return this.attributes[k]; }
  addEventListener(type, f) { (this.listeners[type] = this.listeners[type] || []).push(f); }
  fire(type) { for (const f of this.listeners[type] || []) f({ target: this }); }
  append(...nodes) { for (const n of nodes) { n.parent = this; this.children.push(n); } }
  prepend(n) { n.parent = this; this.children.unshift(n); }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(c => c !== this); this.parent = null; }
  get lastElementChild() { return this.children[this.children.length - 1]; }
  get textContent() { return this.tagName === "#text" ? this.text : this.children.map(c => c.textContent).join(""); }
  find(cls) { if ((this.className || "").split(" ").includes(cls)) return this;
    for (const c of this.children) { const f = c.find && c.find(cls); if (f) return f; } return null; }
  findTag(tag) { if (this.tagName === tag) return this;
    for (const c of this.children) { const f = c.findTag && c.findTag(tag); if (f) return f; } return null; }
}
const body = new Node("body");
const all = () => { const out = []; const walk = n => { out.push(n); n.children.forEach(walk); }; walk(body); return out; };
const document = {
  body, createElement: t => new Node(t),
  createTextNode: t => { const n = new Node("#text"); n.text = t; return n; },
  getElementById: id => all().find(n => n.id === id) || null,
  querySelector: () => null, querySelectorAll: () => [], documentElement: { style: { setProperty() {} } },
};
let now = 0, timers = [];
const setTimeout = (f, ms) => { const t = { f, at: now + ms, id: timers.length + 1 }; timers.push(t); return t.id; };
const clearTimeout = id => { timers = timers.filter(t => t.id !== id); };
const advance = ms => { now += ms; for (const t of timers.filter(t => t.at <= now)) { clearTimeout(t.id); t.f(); } };
const context = vm.createContext({ document, Node, setTimeout, clearTimeout, console, window: {}, location: { host: "x" },
  localStorage: { getItem: () => null, setItem() {} }, fetch: () => Promise.reject(new Error("offline")) });
const source = fs.readFileSync(process.argv[2], "utf8").replace(/loadThemes\(\)\.then\(render\);\s*$/, "");
vm.runInContext(source, context);
const box = () => document.getElementById("toasts");
const result = {};
const t1 = context.toast("warning", "<b>asks</b>", "<img src=x onerror=alert(1)>", { action: ["Open approvals", () => { result.acted = true; }] });
result.warning = { role: t1.getAttribute("role"), cls: t1.className, title: t1.find("title").textContent,
  text: t1.find("text").textContent, textNodes: t1.find("text").children.map(c => c.tagName),
  label: t1.find("sr-only").textContent, glyphHidden: t1.find("glyph").getAttribute("aria-hidden") };
const e1 = context.toast("error", "failed", "exit status 1");
result.error = { role: e1.getAttribute("role"), first: box().children[0] === e1 };
result.unknown = context.toast("nonsense", "x", "").className;
advance(4000);
t1.fire("mouseenter"); advance(8000); result.keptWhileHovered = box().children.includes(t1);
t1.fire("mouseleave"); advance(8001); result.closedAfter = !box().children.includes(t1);
result.errorStays = box().children.includes(e1);
e1.find("close").fire("click"); result.closedByButton = !box().children.includes(e1);
const t2 = context.toast("info", "i", "", { action: ["Go", () => { result.acted = true; }] });
t2.find("actions").findTag("button").fire("click"); result.actionClosed = !box().children.includes(t2);
for (let i = 0; i < 6; i++) context.toast("error", `e${i}`, "");
result.count = box().children.length; result.newest = box().children[0].find("title").textContent;
result.signs = {};
for (const [name, theme] of Object.entries(JSON.parse(fs.readFileSync(process.argv[3], "utf8")).themes)) {
  const c = theme.colors, s = theme.status;
  result.signs[name] = [c.accent, s.ok, s.warn, s.bad].map(bg => context.readableOn(bg, [c.accent_text, c.base, c.text, "#000000", "#ffffff"]));
}
console.log(JSON.stringify(result));
"""


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_toasts_in_the_page(tmp_path):
    """app.js's toast() in node with a small stand-in for the DOM: roles, plain text, closing on its own (not
    errors, not under the pointer), the close button, the action, at most four, and the badge's sign colour."""
    driver = tmp_path / "toasts.js"
    driver.write_text(TOAST_DRIVER)
    run = subprocess.run(["node", str(driver), str(WEB / "app.js"), str(WEB / "themes.json")], capture_output=True,
                         text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    assert result["warning"] == {"role": "status", "cls": "toast warning", "title": "<b>asks</b>",
                                 "text": "<img src=x onerror=alert(1)>", "textNodes": ["#text"],
                                 "label": "Warning: ", "glyphHidden": "true"}, "plain text, never markup"
    assert result["error"] == {"role": "alert", "first": True} and result["unknown"] == "toast info"
    assert result["keptWhileHovered"] and result["closedAfter"], "closes after 8 s, not while under the pointer"
    assert result["errorStays"] and result["closedByButton"], "an error stays until it is closed"
    assert result["acted"] and result["actionClosed"]
    assert result["count"] == 4 and result["newest"] == "e5"
    for name, signs in result["signs"].items():
        c, s = THEMES[name]["colors"], THEMES[name]["status"]
        assert signs == [badge_sign(bg, c) for bg in (c["accent"], s["ok"], s["warn"], s["bad"])], name


def test_a_removed_or_reset_user_loses_its_sessions_at_once():
    """sandbox-ai web user remove|reset end the running service's sessions of that user at their next request,
    also while they keep being used (they would otherwise live up to 12 hours)."""
    secret = add_user()
    svc = service(FakeRunner({("list",): (0, "[]", "")}))
    _, headers, body = login(svc, secret)
    ok = {**HEADERS, "Cookie": cookie_of(headers), "X-CSRF-Token": body["csrf"]}
    assert svc.handle("GET", "/api/me", ok, b"", "192.0.2.1")[0] == 200
    with w.users_locked() as users:  # what `user reset` does: a new password and TOTP secret
        users["admin"] = {"password": w.hash_password("another one entirely", **FAST), "totp": w.new_totp_secret(),
                          "totp_counter": None}
    assert svc.handle("GET", "/api/me", ok, b"", "192.0.2.1")[0] == 401
    assert svc.handle("GET", "/api/me", ok, b"", "192.0.2.1")[0] == 401, "gone, not only refused once"
    secret = add_user()
    _, headers, body = login(svc, secret)
    ok = {**HEADERS, "Cookie": cookie_of(headers), "X-CSRF-Token": body["csrf"]}
    assert svc.handle("GET", "/api/me", ok, b"", "192.0.2.1")[0] == 200
    with w.users_locked() as users:  # `user remove`
        del users["admin"]
    assert svc.handle("GET", "/api/me", ok, b"", "192.0.2.1")[0] == 401


# The close frame a terminal gets when its session ends: 1008, and why (the page shows it).
SESSION_CLOSE = bytes([0x88, 2 + len(w.SESSION_ENDED.encode())]) + b"\x03\xf0" + w.SESSION_ENDED.encode()


def test_an_open_terminal_ends_when_its_user_is_removed(server):
    """A terminal opened before `sandbox-ai web user remove` (or a logout, or the session's limits) does not run on:
    it is checked every few seconds and closed with 1008."""
    status, headers, body = https(server, "POST", "/api/login", {"user": "admin", "password": "correct horse battery",
                                                                 "code": code(server["secret"])})
    cookie, csrf = headers["Set-Cookie"].split(";")[0], json.loads(body)["csrf"]
    sock, head, rest, _ = websocket(server, f"host=local&kind=attach&name=web&csrf={csrf}", cookie)
    assert head.startswith("HTTP/1.1 101"), head
    read_messages(sock, rest, b"size")  # the stand-in waits for input now
    with w.users_locked() as users:
        del users["admin"]
    sock.settimeout(15)
    received = b""
    try:
        while SESSION_CLOSE not in received:  # a close frame with 1008 and why
            chunk = sock.recv(4096)
            if not chunk:
                break
            received += chunk
    except (ConnectionError, socket.timeout, ssl.SSLError):
        pass
    assert SESSION_CLOSE in received, received[-100:]
    sock.close()


def test_a_client_that_never_says_hello_holds_no_one_up(server):
    """The TLS handshake runs in the connection's thread: one silent TCP client does not stop the others."""
    silent = socket.create_connection(("127.0.0.1", server["port"]), timeout=30)
    try:
        started = time.monotonic()
        status, _, _ = https(server, "GET", "/")
        assert status == 200 and time.monotonic() - started < 10
    finally:
        silent.close()


def test_parallel_logins_are_checked_one_at_a_time(monkeypatch):
    """Tries sent at once are checked in turn, so the failures count before the next try: a known password gives a
    handful of TOTP guesses per lockout, not one per request."""
    import threading
    secret = add_user()
    svc = service(FakeRunner(), limiter=w.Limiter(free=3))
    checked, real = [], w.check_totp
    monkeypatch.setattr(w, "check_totp", lambda *a, **k: checked.append(1) or real(*a, **k))
    statuses = []

    def guess(index):
        body = json.dumps({"user": "admin", "password": "correct horse battery", "code": f"{index:06d}"}).encode()
        statuses.append(svc.handle("POST", "/api/login", HEADERS, body, "192.0.2.9")[0])
    threads = [threading.Thread(target=guess, args=(i,)) for i in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert len(checked) <= 3, f"{len(checked)} codes were checked"
    assert statuses.count(429) >= 9


# --- nothing waits for good ------------------------------------------------------------------------


def terminal_session(server):
    """A logged-in session with an open terminal: (socket, the session's record in the service)."""
    status, headers, body = https(server, "POST", "/api/login", {"user": "admin", "password": "correct horse battery",
                                                                 "code": code(server["secret"])})
    cookie, csrf = headers["Set-Cookie"].split(";")[0], json.loads(body)["csrf"]
    sock, head, rest, _ = websocket(server, f"host=local&kind=attach&name=web&csrf={csrf}", cookie)
    assert head.startswith("HTTP/1.1 101"), head
    (session,) = w.Handler.service.sessions.items.values()
    return sock, rest, session


def terminals_end(session, timeout):
    deadline = time.monotonic() + timeout
    while session["terminals"] and time.monotonic() < deadline:
        time.sleep(0.1)
    return session["terminals"] == 0


def test_an_open_terminal_ends_when_its_session_is_idle(server):
    """The terminal's check that its session still lives must not count as the user's activity: else a session with
    an open terminal never reaches its idle limit (the page's own requests are what keeps it)."""
    sock, rest, session = terminal_session(server)
    read_messages(sock, rest, b"size")  # the stand-in waits for input now
    w.Handler.service.sessions.idle = 3  # no request comes from the page from here on
    sock.settimeout(15)
    received = b""
    try:
        while SESSION_CLOSE not in received:  # a close frame with 1008 and why
            chunk = sock.recv(4096)
            if not chunk:
                break
            received += chunk
    except (ConnectionError, socket.timeout, ssl.SSLError):
        pass
    assert SESSION_CLOSE in received, "the terminal outlived its idle session"
    sock.close()


def test_a_terminal_whose_browser_vanished_ends(server, monkeypatch):
    """A browser that is gone without closing (sleep, a lost network) answers no ping: its terminal ends after the
    read deadline instead of running on, with its slot of TERMINALS_PER_SESSION, up to the session's 12 hours."""
    monkeypatch.setattr(w, "WS_PING", 0.3, raising=False)
    monkeypatch.setattr(w, "WS_DEADLINE", 1.5, raising=False)
    sock, _, session = terminal_session(server)
    try:
        assert terminals_end(session, 10), "the terminal of a browser that is gone runs on"
    finally:
        sock.close()


def test_a_browser_that_reads_nothing_does_not_hold_its_terminal(server, home, monkeypatch):
    """A client that sends but never reads: the output thread's write times out and the client is dropped, instead
    of blocking for good with the send lock held (which also holds the pong and the close)."""
    monkeypatch.setattr(w, "WS_PING", 0.3, raising=False)
    monkeypatch.setattr(w, "WS_DEADLINE", 1.5, raising=False)
    chatty = home / "chatty-terminal"
    chatty.write_text(f"#!{sys.executable}\nimport sys\nwhile True:\n    sys.stdout.write('x' * 65536)\n")
    chatty.chmod(0o755)
    monkeypatch.setattr(w, "terminal_command", lambda query: ("local", [str(chatty)]))
    sock, _, session = terminal_session(server)
    stop = threading.Event()

    def keys():
        while not stop.wait(0.2):
            try:
                sock.sendall(frame(b"k", 2))
            except (OSError, ValueError):
                return
    sender = threading.Thread(target=keys, daemon=True)
    sender.start()
    try:
        assert terminals_end(session, 15), "a client that reads nothing holds its terminal for good"
    finally:
        stop.set()
        sender.join(5)
        sock.close()


def test_a_quiet_terminal_whose_browser_answers_the_pings_stays_open(server, monkeypatch):
    """The read deadline is no idle limit: a browser that is there answers the service's pings (as browsers do on
    their own), and its quiet terminal stays open past WS_DEADLINE."""
    monkeypatch.setattr(w, "WS_PING", 0.3, raising=False)
    monkeypatch.setattr(w, "WS_DEADLINE", 1.5, raising=False)
    sock, rest, session = terminal_session(server)
    sock.settimeout(0.5)
    data, pings, deadline = bytearray(rest), 0, time.monotonic() + 4
    try:
        while time.monotonic() < deadline:
            with contextlib.suppress(socket.timeout):
                data += sock.recv(65536)
            while len(data) >= 2 and len(data) >= 2 + (data[1] & 0x7F):
                opcode, length = data[0] & 0x0F, data[1] & 0x7F
                if opcode == 9:
                    pings += 1
                    sock.sendall(frame(bytes(data[2:2 + length]), 10))  # the pong
                del data[:2 + length]
        assert pings >= 3 and session["terminals"] == 1, (pings, session["terminals"])
    finally:
        sock.close()


def test_an_answer_to_a_question_gets_more_time_than_the_approvals_wait(logged_in, monkeypatch):
    """`sandbox-ai approvals allow ID --scope always` waits up to approvals.VERIFY_WAIT for the sandbox before it
    remembers the answer: the web call must not be stopped before that (the "always" would be lost)."""
    timeouts = []
    real = logged_in.runner

    def runner(host, args, timeout=120, input=None):
        timeouts.append(timeout)
        return real(host, args, timeout, input)
    logged_in.svc.api.runner = runner
    status, out = logged_in("POST", "/api/hosts/ct1/approvals/12-3-4", {"decision": "allow", "scope": "always"})
    assert status == 200, out
    approvals = w.module("approvals")
    assert timeouts[-1] >= approvals.VERIFY_WAIT + 30, timeouts


def test_an_unchanged_static_file_is_answered_with_304(logged_in, tmp_path, monkeypatch):
    """Static files carry an ETag: a browser that has the file gets 304 and no body (xterm.js alone is about 300 KB
    per terminal view), a changed file comes again whole."""
    svc = logged_in.svc
    host = {"Host": "localhost:8443"}
    status, headers, body = svc.handle("GET", "/static/app.js", host, b"", "192.0.2.1")
    tag = dict(headers)["ETag"]
    assert status == 200 and body and tag.startswith('"') and dict(headers)["Cache-Control"] == "no-cache"
    status, headers, body = svc.handle("GET", "/static/app.js", {**host, "If-None-Match": tag}, b"", "192.0.2.1")
    assert status == 304 and body == b"" and dict(headers)["ETag"] == tag
    status, _, body = svc.handle("GET", "/static/app.js", {**host, "If-None-Match": '"other", ' + tag}, b"", "192.0.2.1")
    assert status == 304 and body == b""  # one of several tags
    static = tmp_path / "web"
    static.mkdir()
    for name in w.STATIC_FILES:
        (static / name).write_bytes((ROOT / "sandbox/web" / name).read_bytes())
    monkeypatch.setattr(w, "STATIC", static)
    (static / "app.js").write_text("// a new version\n")
    status, headers, body = svc.handle("GET", "/static/app.js", {**host, "If-None-Match": tag}, b"", "192.0.2.1")
    assert status == 200 and body == b"// a new version\n" and dict(headers)["ETag"] != tag


# -- the canary log --

CANARY_LOG = {"run": "claude-1", "agent": "claude", "status": "ended", "activity": True, "events": [
    {"time": "2026-11-07T10:00:05Z", "run": "claude-1", "canary": "aws", "path": "/root/.aws/credentials",
     "event": "read", "action": "report", "process": {"pid": 41, "comm": "cat", "argv": ["cat", "x"]},
     "tool_calls": [{"id": "t2", "match": "names the file", "summary": "cat ~/.aws/credentials"}],
     "turn": {"id": "p1", "summary": "tidy up"}, "requests": []}]}


def test_the_canary_log_is_one_fixed_command(logged_in):
    logged_in.runner.answers[("canaries", "log")] = (0, json.dumps(CANARY_LOG), "")
    status, out = logged_in("GET", "/api/hosts/ct1/canaries/claude-1")
    assert status == 200 and out == [CANARY_LOG]
    assert logged_in.runner.calls[-1] == ("ct1", ["canaries", "log", "claude-1", "--json"])
    logged_in.runner.answers[("canaries", "log")] = (0, json.dumps([CANARY_LOG]), "")
    status, out = logged_in("GET", "/api/hosts/ct1/canaries")
    assert status == 200 and out == [CANARY_LOG]
    assert logged_in.runner.calls[-1] == ("ct1", ["canaries", "log", "--json"])
    calls = len(logged_in.runner.calls)
    for bad in ("--json", "-h", "a%20b"):
        status, _ = logged_in("GET", f"/api/hosts/ct1/canaries/{bad}")
        assert status == 400
    assert len(logged_in.runner.calls) == calls, "nothing refused reaches the host"
    # An older sandbox-ai without the command: an error the page explains.
    logged_in.runner.answers[("canaries", "log")] = (2, "", "invalid choice: 'log'")
    status, out = logged_in("GET", "/api/hosts/ct1/canaries")
    assert status == 502 and "invalid choice" in out["error"]


def test_the_page_has_a_canaries_page_with_the_links_of_each_event():
    script = (Path(w.__file__).parent / "web" / "app.js").read_text()
    page = script.split("pages.canaries")[1].split("pages.settings")[0]
    assert '["canaries", "Canaries"]' in script and 'hostPath("/canaries")' in page
    assert "status-bad" in page and "canaryLinks(e)" in page and "aria-expanded" in page
    assert "innerHTML" not in script.split("function canaryProcess")[1].split("pages.settings")[0]
    css = (Path(w.__file__).parent / "web" / "app.css").read_text()
    assert ".canary-links.hidden { display: none; }" in css and "var(--bad)" in css.split(".canary-event")[1]


# --- hosts: key files made private, the hosts' versions, their update ----------------------------------------

def private_key(path, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    marker = "PRIVATE" + " KEY-----"  # in two pieces: no key-looking line in the source (privacy check)
    path.write_text(f"-----BEGIN OPENSSH {marker}\nnot a real key\n-----END OPENSSH {marker}\n")
    path.chmod(mode)
    return path


def test_adding_and_testing_a_host_makes_its_key_private(logged_in, home):
    key = private_key(home / "home/.ssh/id_ct1")
    logged_in.runner.answers[("--version",)] = (0, "sandbox-ai 0.1.66\n", "")
    logged_in.runner.answers[("doctor",)] = (0, "[]", "")
    status, out = logged_in("POST", "/api/hosts", {"name": "ct1", "ssh": "root@192.0.2.10", "identity": str(key)})
    assert status == 200 and out["fixed"] == [{"path": str(key), "was": "0644", "now": "0600"}], out
    assert out["connection"] == {"ok": True, "version": "sandbox-ai 0.1.66", "error": None}
    assert key.stat().st_mode & 0o777 == 0o600
    key.chmod(0o604)
    status, result = logged_in("POST", "/api/hosts/ct1/test")
    assert result["ssh"] and result["fixed"][0]["was"] == "0604" and key.stat().st_mode & 0o777 == 0o600
    status, out = logged_in("POST", "/api/hosts", {"name": "ct2", "ssh": "root@192.0.2.11", "identity": str(key),
                                                   "update_sudo": True})
    assert status == 200 and out["fixed"] == [] and "connection" not in out and out["update_sudo"] is True


def test_the_hosts_update_and_their_versions(home):
    secret = add_user()
    asked, statuses = [], []

    def updater(host, version=None, sudo=None):
        asked.append((host, version, sudo))
        if host == "gone":
            raise ValueError("no host gone; see: sandbox-ai hosts list")
        return {"name": host, "before": "0.1.60", "after": "0.1.66", "changed": True, "error": None}

    def status_of(names, refresh=False):
        statuses.append((names, refresh))
        return {name: {"version": "0.1.60", "available": "0.1.66", "update": True, "ssh": "x", "port": 22}
                for name in names}
    svc = w.Service({"localhost:8443"}, api=w.Api(FakeRunner(), updater=updater, statuses=status_of))
    _, headers, body = login(svc, secret)
    ok = {**HEADERS, "Cookie": cookie_of(headers), "X-CSRF-Token": body["csrf"]}

    def call(method, path, data=None):
        status, _, out = svc.handle(method, path, ok, json.dumps(data).encode() if data is not None else b"",
                                    "192.0.2.1")
        return status, json.loads(out)
    assert call("POST", "/api/hosts", {"name": "ct1", "ssh": "root@192.0.2.10"})[0] == 200
    status, out = call("POST", "/api/hosts/ct1/update", {})
    assert status == 200 and out["after"] == "0.1.66" and asked[-1] == ("ct1", None, None)
    assert call("POST", "/api/hosts/ct1/update", {"sudo": True, "version": "0.1.67"})[0] == 200
    assert asked[-1] == ("ct1", "0.1.67", True)
    for data in ({"sudo": "yes"}, {"version": "1.2; id"}, {"version": ["0.1.67"]}):
        assert call("POST", "/api/hosts/ct1/update", data)[0] == 400, data
    assert call("POST", "/api/hosts/gone/update", {})[0] == 404
    assert call("POST", "/api/hosts/x;y/update", {})[0] == 400
    status, found = call("GET", "/api/hosts-status")
    assert status == 200 and found == {"ct1": {"version": "0.1.60", "available": "0.1.66", "update": True}}
    assert statuses[-1] == (["ct1"], False)
    assert call("POST", "/api/hosts-status", {})[0] == 200 and statuses[-1] == (["ct1"], True)


def test_the_update_terminal(home, monkeypatch):
    hosts = w.module("hosts")
    hosts.save({"ct1": hosts.check("ct1", {"ssh": "root@192.0.2.10"})})
    seen = []
    monkeypatch.setattr(hosts, "update_terminal", lambda host, version=None: seen.append((host, version)) or ["ssh"])
    assert w.terminal_command({"host": "ct1", "kind": "update"}) == ("ct1", ["ssh"]) and seen == [("ct1", None)]
    assert w.terminal_command({"host": "ct1", "kind": "update", "version": "0.1.67"})[0] == "ct1"
    for query in ({"host": "local", "kind": "update"}, {"host": "ct1", "kind": "update", "version": "x"}):
        with pytest.raises(w.Refused):
            w.terminal_command(query)


def test_the_hosts_page_updates_and_shows_versions():
    script = (ROOT / "sandbox/web/app.js").read_text()
    for needed in ('"/api/hosts-status"', "/update`", '"Update all"', 'kind: "update"', "update_sudo", "keyNotices"):
        assert needed in script, needed


def test_an_mcp_server_is_attached_to_a_start(logged_in):
    status, out = logged_in("POST", "/api/hosts/ct1/mcp/demo/attach", {"start": "web", "restart": True})
    assert status == 200 and logged_in.runner.calls[-1] == ("ct1", ["mcp", "attach", "demo", "web", "--json",
                                                                    "--restart"])
    assert logged_in("POST", "/api/hosts/local/mcp/demo/attach", {"start": "web"})[0] == 200
    assert logged_in.runner.calls[-1] == ("local", ["mcp", "attach", "demo", "web", "--json"])
    count = len(logged_in.runner.calls)
    # A name with a leading "-" would be an option of mcp attach there.
    for path, data in (("/api/hosts/ct1/mcp/de mo/attach", {"start": "web"}), ("/api/hosts/ct1/mcp/demo/attach", {}),
                       ("/api/hosts/ct1/mcp/-h/attach", {"start": "web"}),
                       ("/api/hosts/ct1/mcp/--restart/attach", {"start": "web"}),
                       ("/api/hosts/ct1/mcp/_x/attach", {"start": "web"}),
                       ("/api/hosts/ct1/mcp/demo/attach", {"start": "--all"}),
                       ("/api/hosts/ct1/mcp/demo/attach", {"start": "web", "restart": "yes"})):
        assert logged_in("POST", path, data)[0] == 400, (path, data)
    assert len(logged_in.runner.calls) == count, "nothing refused reaches a host"
    script = (ROOT / "sandbox/web/app.js").read_text()
    assert "attachControls" in script and "/attach`" in script and "Attach to…" in script


# --- review findings: logins, sessions, the audit log, terminals, the service's connections -------------------

def test_a_known_user_name_cannot_lock_its_user_out():
    """Failed logins wait per address and per address with user; the wait of the user from every address keeps
    the right password with its code from no one: else whoever knows the name keeps the account locked. While that
    one waits, each failure locks its own address at once (one guess per address, not FREE)."""
    secret = add_user()
    clock = [1000.0]
    svc = service(limiter=w.Limiter(free=3, first=30, maximum=900, clock=lambda: clock[0]))

    def attempt(address, password="wrong password!!", offset=0):
        body = json.dumps({"user": "admin", "password": password, "code": code(secret, offset)}).encode()
        return svc.handle("POST", "/api/login", HEADERS, body, address)[0]
    for _ in range(3):
        assert attempt("198.51.100.66") == 401
    assert attempt("198.51.100.66", "correct horse battery") == 429, "the guessing address waits"
    assert svc.limiter.wait("user:admin") > 0, "and so does the user, from every address"
    assert attempt("192.0.2.1", "correct horse battery") == 200, "yet its owner gets in from elsewhere"
    for _ in range(3):  # the user waits again
        assert attempt("198.51.100.7") == 401
    assert attempt("198.51.100.8") == 401
    assert attempt("198.51.100.8", "correct horse battery", offset=1) == 429, "one failure locked that address"
    assert attempt("192.0.2.2", "correct horse battery", offset=1) == 200
    events = [json.loads(line) for line in (w.state_dir() / "audit.jsonl").read_text().splitlines()]
    assert [e["event"] for e in events].count("login") == 2
    assert any(e.get("user_throttled") for e in events if e["event"] == "login-failed")


def test_the_pages_polls_do_not_keep_its_session_alive():
    """A tab left open polls every 5 s with X-Background: 1: that is no use of the page, its session still reaches
    its idle limit. A request without it (the user's) counts."""
    secret = add_user()
    clock = [0.0]
    svc = service(FakeRunner({("approvals", "list"): (0, "[]", "")}),
                  sessions=w.Sessions(idle=60, lifetime=3600, clock=lambda: clock[0]))
    _, headers, _ = login(svc, secret)
    used = {**HEADERS, "Cookie": cookie_of(headers)}
    polled = {**used, "X-Background": "1"}
    path = "/api/hosts/local/approvals"
    for _ in range(3):
        clock[0] += 25
        assert svc.handle("GET", path, used, b"", "192.0.2.1")[0] == 200, "the user's requests keep it"
    for _ in range(2):
        clock[0] += 25
        assert svc.handle("GET", path, polled, b"", "192.0.2.1")[0] == 200
    clock[0] += 25  # 75 s since the user's last request
    assert svc.handle("GET", path, polled, b"", "192.0.2.1")[0] == 401
    assert svc.handle("GET", path, used, b"", "192.0.2.1")[0] == 401, "gone"


def test_the_audit_log_names_what_changed_but_no_secrets(logged_in):
    """Secrets are left out by their field's name, not by a search for "key": a defaults change says which key."""
    assert logged_in("POST", "/api/hosts/ct1/defaults", {"key": "telemetry", "value": "strict"})[0] == 200
    logged_in("POST", "/api/hosts/ct1/llm", {"name": "a", "kind": "lmstudio", "url": "http://x", "api_key": "sk-1",
                                             "token": "t-2", "password": "p-3", "code": "123456",
                                             "client-secret": "c-4", "github_token": "g-5"})
    text = (w.state_dir() / "audit.jsonl").read_text()
    for secret in ("sk-1", "t-2", "p-3", "123456", "c-4", "g-5"):
        assert secret not in text, secret
    bodies = [json.loads(line)["body"] for line in text.splitlines() if json.loads(line)["event"] == "action"]
    assert bodies[0] == {"key": "telemetry", "value": "strict"}
    assert bodies[1]["name"] == "a" and bodies[1]["api_key"] == "***" and bodies[1]["github_token"] == "***"
    assert w.redacted({"key": "mode", "monkey": 1, "keyboard": [{"Password": "x", "kind": "k"}]}) == {
        "key": "mode", "monkey": 1, "keyboard": [{"Password": "***", "kind": "k"}]}


def test_the_audit_log_says_what_a_terminal_starts():
    """A start in a terminal is logged with its agent, workspace and options, not only its name."""
    import urllib.parse
    secret = add_user()
    svc = service(terminal=lambda query: ("local", ["true"]))
    _, headers, body = login(svc, secret)
    query = {"host": "local", "csrf": body["csrf"], "kind": "start", "agent": "codex", "name": "n1",
             "workspace": "/srv/app", "options": json.dumps(["--mode", "high", "--allow-pypi"])}
    svc.websocket("/api/terminal?" + urllib.parse.urlencode(query), {**HEADERS, "Cookie": cookie_of(headers)},
                  "192.0.2.1")
    events = [json.loads(line) for line in (w.state_dir() / "audit.jsonl").read_text().splitlines()]
    (event,) = [e for e in events if e["event"] == "terminal"]
    assert {k: event.get(k) for k in ("user", "kind", "name", "agent", "workspace", "options")} == {
        "user": "admin", "kind": "start", "name": "n1", "agent": "codex", "workspace": "/srv/app",
        "options": ["--mode", "high", "--allow-pypi"]}


def test_a_users_file_readable_by_others_is_a_clear_error_not_a_dropped_connection():
    secret = add_user()
    svc = service()
    _, headers, body = login(svc, secret)
    ok = {**HEADERS, "Cookie": cookie_of(headers), "X-CSRF-Token": body["csrf"]}
    w.users_file().chmod(0o644)
    for method, path, data in (("GET", "/api/me", b""), ("POST", "/api/login", json.dumps(
            {"user": "admin", "password": "correct horse battery", "code": code(secret, 1)}).encode()),
            ("POST", "/api/hosts/local/starts/web/stop", b"{}")):
        status, _, out = svc.handle(method, path, ok, data, "192.0.2.1")
        error = json.loads(out)["error"]
        assert status == 503 and "cannot read its logins" in error and str(w.config_dir()) not in error, path
    with pytest.raises(w.Refused) as refused:
        svc.websocket("/api/terminal?host=local&kind=attach&name=web&csrf=" + body["csrf"], ok, "192.0.2.1")
    assert refused.value.status == 503
    events = [json.loads(line) for line in (w.state_dir() / "audit.jsonl").read_text().splitlines()]
    assert any(e["event"] == "users-unreadable" and "readable by others" in e["why"] for e in events)


def test_a_refused_terminal_says_why_in_its_close_frame(server, monkeypatch):
    """Logged in from the page, but no terminal (too many open, a query that is not one): the WebSocket opens only to
    close with 1008 and the reason, which the page shows (a refused handshake is 1006 and nothing more there). Not
    logged in: no WebSocket at all."""
    status, headers, body = https(server, "POST", "/api/login", {"user": "admin", "password": "correct horse battery",
                                                                 "code": code(server["secret"])})
    cookie, csrf = headers["Set-Cookie"].split(";")[0], json.loads(body)["csrf"]
    monkeypatch.setattr(w, "TERMINALS_PER_SESSION", 0)
    sock, head, rest, _ = websocket(server, f"host=local&kind=attach&name=web&csrf={csrf}", cookie)
    assert head.startswith("HTTP/1.1 101"), head
    reason = b"refused: at most 0 terminals at once"
    sock.settimeout(10)
    received = bytes(rest)
    with contextlib.suppress(ConnectionError, socket.timeout, ssl.SSLError):
        while reason not in received:
            chunk = sock.recv(4096)
            if not chunk:
                break
            received += chunk
    assert bytes([0x88, 2 + len(reason)]) + b"\x03\xf0" + reason in received, received
    sock.close()
    sock, head, _, _ = websocket(server, f"host=local&kind=attach&name=web&csrf={csrf}", "x=y")
    assert head.startswith("HTTP/1.1 401"), head
    sock.close()


def test_logins_are_throttled_by_the_socket_address_not_a_header(server):
    """No X-Forwarded-For is believed (no trusted proxy is configured): a client cannot choose the address its
    failures count for."""
    for index in range(5):
        status, _, _ = https(server, "POST", "/api/login", {"user": "admin", "password": "wrong password!!",
                                                            "code": "000000"},
                             headers={"X-Forwarded-For": f"203.0.113.{index}", "X-Real-IP": f"203.0.113.{index}"})
        assert status == 401
    status, _, _ = https(server, "POST", "/api/login", {"user": "admin", "password": "correct horse battery",
                                                        "code": code(server["secret"])},
                         headers={"X-Forwarded-For": "203.0.113.200"})
    assert status == 429


def test_the_service_serves_a_bounded_number_of_connections(server):
    """Each connection is a thread: past the limit one more is closed at once (the accept loop never waits), and a
    place that frees serves the next."""
    srv = server["server"]
    srv.limit = 2
    silent = [socket.create_connection(("127.0.0.1", server["port"]), timeout=30) for _ in range(2)]
    try:
        deadline = time.monotonic() + 10
        while srv.active < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert srv.active == 2
        with pytest.raises((ConnectionError, ssl.SSLError, http.client.HTTPException, OSError)):
            https(server, "GET", "/")
    finally:
        for sock in silent:
            sock.close()
    deadline = time.monotonic() + 10
    while srv.active and time.monotonic() < deadline:
        time.sleep(0.05)
    assert srv.active == 0
    assert https(server, "GET", "/")[0] == 200


def test_a_connection_without_a_session_is_kept_only_briefly(server, monkeypatch):
    """An idle connection that never showed a session is closed after GUEST_KEEPALIVE seconds; one whose request
    carried a session (or made one: a login) is kept KEEPALIVE seconds."""
    monkeypatch.setattr(w, "GUEST_KEEPALIVE", 1)
    origin = {"Origin": f"https://localhost:{server['port']}"}
    guest = http.client.HTTPSConnection("localhost", server["port"], context=server["context"], timeout=30)
    guest.request("GET", "/", headers=origin)
    assert guest.getresponse().read() and guest.sock is not None
    time.sleep(2.5)
    with pytest.raises((ConnectionError, ssl.SSLError, http.client.HTTPException, OSError)):
        guest.request("GET", "/", headers=origin)
        guest.getresponse().read()
    guest.close()
    user = http.client.HTTPSConnection("localhost", server["port"], context=server["context"], timeout=30)
    user.request("POST", "/api/login", body=json.dumps({"user": "admin", "password": "correct horse battery",
                                                        "code": code(server["secret"])}),
                 headers={**origin, "Content-Type": "application/json"})
    response = user.getresponse()
    response.read()
    assert response.status == 200
    cookie = response.getheader("Set-Cookie").split(";")[0]
    time.sleep(2.5)
    user.request("GET", "/api/me", headers={**origin, "Cookie": cookie})
    response = user.getresponse()
    assert response.status == 200 and json.loads(response.read())["user"] == "admin", "the same connection"
    user.close()


APPROVALS_DRIVER = r"""
const fs = require("fs"), vm = require("vm");
class Node {
  constructor(tag) { this.tagName = tag; this.children = []; this.attributes = {}; this.listeners = {}; this.parent = null;
    this.className = ""; this.text = ""; this.hidden = false; this.disabled = false; }
  setAttribute(k, v) { this.attributes[k] = String(v); if (k === "id") this.id = String(v); }
  getAttribute(k) { return this.attributes[k]; }
  addEventListener(type, f) { (this.listeners[type] = this.listeners[type] || []).push(f); }
  fire(type) { for (const f of this.listeners[type] || []) f({ target: this }); }
  append(...nodes) { for (const n of nodes) { if (n.parent) n.remove(); n.parent = this; this.children.push(n); } }
  prepend(n) { n.parent = this; this.children.unshift(n); }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(c => c !== this); this.parent = null; }
  removeChild(n) { n.remove(); return n; }
  get firstChild() { return this.children[0] || null; }
  get lastElementChild() { return this.children[this.children.length - 1]; }
  get isConnected() { let n = this; while (n.parent) n = n.parent; return n === body; }
  get textContent() { return this.tagName === "#text" ? this.text : this.children.map(c => c.textContent).join(""); }
  all() { const out = []; const walk = n => { out.push(n); n.children.forEach(walk); }; walk(this); return out; }
  querySelectorAll(sel) { return this.all().filter(n => n !== this && sel === ".badge" && (n.className || "").split(" ").includes("badge")); }
}
const body = new Node("body");
const nav = new Node("button");
const document = {
  body, createElement: t => new Node(t),
  createTextNode: t => { const n = new Node("#text"); n.text = t; return n; },
  getElementById: id => body.all().find(n => n.id === id) || null,
  querySelector: sel => (sel === 'nav button[data-page="approvals"]' ? nav : null), querySelectorAll: () => [],
  documentElement: { style: { setProperty() {} } },
};
const calls = [];
let answer = null;
const fetch = async (path, opts) => {
  calls.push({ method: opts.method, path, headers: opts.headers, body: opts.body });
  const data = answer(opts.method, path, opts.body);
  if (data instanceof Error) return { status: 502, ok: false, statusText: "Bad Gateway", json: async () => ({ error: data.message }) };
  return { status: 200, ok: true, json: async () => data };
};
const context = vm.createContext({ document, Node, setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0,
  clearInterval() {}, console, window: {}, location: { host: "x" }, localStorage: { getItem: () => null, setItem() {} }, fetch });
vm.runInContext(fs.readFileSync(process.argv[2], "utf8").replace(/loadThemes\(\)\.then\(render\);\s*$/, ""), context);
const settle = async () => { for (let i = 0; i < 20; i++) await new Promise(r => setImmediate(r)); };
const rows = () => body.all().filter(n => n.attributes["data-id"]);
const button = (row, text) => row.all().find(n => n.tagName === "button" && n.textContent === text);
const toasts = () => (document.getElementById("toasts") || new Node("x")).children.map(t => t.textContent);
(async () => {
  const result = {};
  const main = new Node("main"); main.setAttribute("id", "main"); body.append(main);
  vm.runInContext('state.me = { user: "admin", csrf: "c" }; state.page = "approvals"; state.host = "local";', context);
  const q = (id, question) => ({ id, run: "claude-web", sandbox: "claude-web", kind: "connect", question, always: "Allow always: pypi.org" });
  let open = [q("1-1", "connect to pypi.org:443"), q("1-2", "look up example.org")];
  answer = (method, path, sent) => {  // an answered question is no longer open
    if (method === "GET") return open;
    open = open.filter(x => !path.endsWith(`/${x.id}`));
    return { output: `sandbox-ai approvals: ${sent}` };
  };
  context.__main = main;
  await vm.runInContext("pages.approvals(__main)", context);
  result.first = rows().map(r => r.attributes["data-id"]);
  const kept = rows()[0], scope = kept.all().find(n => n.tagName === "select");
  scope.value = "always";
  open = [q("1-1", "connect to pypi.org:443"), q("1-3", "connect to 192.0.2.9:5432")];
  await vm.runInContext("poll()", context); await settle();
  result.polled = rows().map(r => r.attributes["data-id"]);
  result.sameRow = rows()[0] === kept; result.sameScope = kept.all().includes(scope) && scope.value === "always";
  result.background = calls.filter(c => c.method === "GET").map(c => c.headers["X-Background"] || null);
  button(kept, "Allow").fire("click");
  result.asked = kept.all().find(n => n.className === "confirm").textContent;
  result.postedBeforeYes = calls.filter(c => c.method === "POST").length;
  await vm.runInContext("poll()", context); await settle();
  result.stillAsking = rows()[0] === kept && Boolean(button(kept, "Yes, allow"));
  button(kept, "Yes, allow").fire("click"); await settle();
  const post = calls.find(c => c.method === "POST");
  result.post = [post.path, JSON.parse(post.body)];
  result.toast = toasts().find(t => t.includes("sandbox-ai approvals:")) || null;
  open = new Error("ssh: connect to host ct1 port 22: Connection refused");
  await vm.runInContext("poll()", context); await settle();
  const badge = nav.children.find(n => (n.className || "").includes("badge"));
  result.badge = badge ? [badge.textContent, badge.getAttribute("aria-label")] : null;
  result.count = vm.runInContext("state.approvals", context);
  result.notice = main.all().filter(n => (n.className || "").includes("notice")).map(n => n.textContent);
  result.rowsAfterError = rows().map(r => r.attributes["data-id"]);
  result.close = [vm.runInContext('closeText({ code: 1008, reason: "refused: at most 8 terminals at once\u001b[2J" }, true)', context),
    vm.runInContext('closeText({ code: 1006, reason: "" }, false)', context)];
  console.log(JSON.stringify(result));
})().catch(e => { console.error(e.stack); process.exit(1); });
"""


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_the_approvals_table_is_kept_between_polls(tmp_path):
    """app.js in node with a small stand-in for the DOM: the rows are keyed by the question's id (a poll adds and
    removes rows, the others stay with the scope chosen in them); Allow asks in the row first, naming the sandbox,
    what it asks and the scope; the answer's output is a toast; the polls are background requests, and a failed one
    shows "?" ("could not check"), never 0, and leaves the rows."""
    driver = tmp_path / "approvals.js"
    driver.write_text(APPROVALS_DRIVER)
    run = subprocess.run(["node", str(driver), str(WEB / "app.js")], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    assert result["first"] == ["1-1", "1-2"] and result["polled"] == ["1-1", "1-3"]
    assert result["sameRow"] and result["sameScope"], "an unchanged row stays, with its scope"
    assert result["background"] == [None, "1"], "the page's own load counts, the poll does not"
    assert result["postedBeforeYes"] == 0 and result["stillAsking"]
    asked = result["asked"]
    for part in ("claude-web", "connect to pypi.org:443", "always (remembered for the project)", "Allow always: pypi.org"):
        assert part in asked, (part, asked)
    assert result["post"] == ["/api/hosts/local/approvals/1-1", {"decision": "allow", "scope": "always"}]
    assert result["toast"] and '"scope":"always"' in result["toast"], "the command's output is shown"
    assert result["badge"] == ["?", "could not check"] and result["count"] is None
    assert any("Could not check" in text and "Connection refused" in text for text in result["notice"])
    assert result["rowsAfterError"] == ["1-3"], "a failed check takes no row away"
    assert "code 1008" in result["close"][0] and "at most 8 terminals" in result["close"][0]
    assert "\u001b" not in result["close"][0] and "could not connect" in result["close"][1]


def test_an_ended_terminal_keeps_its_tab_and_says_why():
    """The tab of a terminal that ended stays until it is closed, with the close code and the service's reason; its
    window listener goes with it. The page's polls are background requests."""
    script = (WEB / "app.js").read_text()
    handler = script.split("ws.onclose = (event) => {")[1].split("\n  };")[0]
    assert "setTimeout" not in handler, "no tab vanishes on its own"
    assert 'window.removeEventListener("resize", entry.fit)' in handler and "closeText(event, entry.opened)" in handler
    assert "event.code" in script.split("function closeText")[1] and "event.reason" in script.split("function closeText")[1]
    assert "function closeTerminal(entry)" in script and "onclick: () => closeTerminal(t)" in script
    assert 'hostPath("/approvals"), undefined, { background: true }' in script
    assert 'hostPath("/starts"), undefined, { background: Boolean(background) }' in script
    assert 'refreshTerminalStarts(document.getElementById("main"), true)' in script
    assert 'headers["X-Background"] = "1"' in script
    approvals = script.split("const SCOPES")[1].split("pages.doctor")[0]
    assert "confirm(" not in approvals and "innerHTML" not in approvals, "asked in the page, as text"


def test_a_broken_handshake_from_anyone_gets_no_websocket(server):
    """A wrong version or key is refused as plain HTTP before anything else is looked at: not logged in, nobody ever
    gets a 101 (the switch to a WebSocket), not even to read a close frame."""
    raw = socket.create_connection(("127.0.0.1", server["port"]), timeout=30)
    with server["context"].wrap_socket(raw, server_hostname="localhost") as sock:
        sock.sendall((f"GET /api/terminal?start=x HTTP/1.1\r\nHost: localhost:{server['port']}\r\n"
                      f"Origin: https://localhost:{server['port']}\r\n"
                      "Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: not-base64!\r\n"
                      "Sec-WebSocket-Version: 13\r\n\r\n").encode())
        head = sock.recv(4096).decode(errors="replace")
    assert head.startswith("HTTP/1.1 400") and "101" not in head.split("\r\n")[0]
