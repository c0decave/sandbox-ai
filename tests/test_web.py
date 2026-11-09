"""sandbox-ai web (sandbox/webui.py): logins with password and TOTP, sessions, CSRF and same-origin checks, the
rate limit, the fixed set of actions (each a `sandbox-ai ARGS` with its arguments one by one), WebSocket framing,
and the whole service over real HTTPS: login, actions, and a terminal through a WebSocket into a real pty."""
import base64
import contextlib
import hashlib
import http.client
import importlib.util
import json
import re
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
    path, sure = "/api/hosts/local/starts/web/stop", b'{"confirm": true}'
    assert svc.handle("POST", path, ok, sure, "192.0.2.1")[0] == 200
    assert runner.calls[-1] == ("local", ["stop", "web"])
    for changed, status in [({"X-CSRF-Token": "x"}, 403), ({"X-CSRF-Token": None}, 403),
                            ({"Origin": "https://evil.example"}, 403), ({"Origin": None}, 403),
                            ({"Content-Type": "text/plain"}, 415), ({"Host": "evil.example"}, 421),
                            ({"Cookie": None}, 401)]:
        sent = {k: v for k, v in {**ok, **changed}.items() if v is not None}
        assert svc.handle("POST", path, sent, sure, "192.0.2.1")[0] == status, changed
    assert len(runner.calls) == 1, "nothing refused reached a host"
    # A Referer of the same origin is enough when the browser sends no Origin.
    sent = {**{k: v for k, v in ok.items() if k != "Origin"}, "Referer": "https://localhost:8443/"}
    assert svc.handle("POST", path, sent, sure, "192.0.2.1")[0] == 200
    assert svc.handle("GET", "/", {"Host": "evil.example"}, b"", "192.0.2.1")[0] == 421, "DNS rebinding"


# --- the actions --------------------------------------------------------------------------------------------

@pytest.fixture
def logged_in():
    secret = add_user()
    runner = FakeRunner({("list",): (0, "[]", ""), ("approvals", "list"): (0, "[]", ""),
                         ("privacy-check",): (0, json.dumps([{"repository": "/home/u/src/web", "checked": True,
                                                              "refused": None, "scanned": 3, "findings": []}]), "")})
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
    ("POST", "/api/hosts/ct1/starts/web/stop", {"confirm": True}, ["stop", "web"]),
    ("GET", "/api/hosts/ct1/starts/web/review", None, ["resume", "web", "--review-home", "--json"]),
    ("GET", "/api/hosts/ct1/approvals", None, ["approvals", "list", "--json"]),
    ("POST", "/api/hosts/ct1/approvals/12-3-4", {"decision": "allow", "scope": "run"},
     ["approvals", "allow", "12-3-4", "--scope", "run"]),
    ("POST", "/api/hosts/ct1/approvals/12-3-4", {"decision": "deny"}, ["approvals", "deny", "12-3-4"]),
    ("POST", "/api/hosts/ct1/approvals/12-3-4", {"decision": "deny", "scope": "run"},
     ["approvals", "deny", "12-3-4", "--scope", "run"]),
    ("POST", "/api/hosts/ct1/approvals/12-3-4", {"decision": "deny", "scope": "always", "confirm": True},
     ["approvals", "deny", "12-3-4", "--scope", "always"]),
    # a browser question whose link the user opened: allowed without opening a browser on that host
    ("POST", "/api/hosts/ct1/approvals/12-3-4", {"decision": "allow", "scope": "once", "no_open": True},
     ["approvals", "allow", "12-3-4", "--scope", "once", "--no-open"]),
    ("GET", "/api/hosts/ct1/doctor", None, ["doctor", "--json"]),
    ("GET", "/api/hosts/ct1/logs", None, ["logs", "--json"]),
    ("GET", "/api/hosts/ct1/llm", None, ["llm", "list", "--json"]),
    ("POST", "/api/hosts/ct1/llm", {"name": "lmstudio", "kind": "lmstudio", "url": "http://192.0.2.20:1234",
                                    "model": "qwen3"},
     ["llm", "add", "lmstudio", "--kind", "lmstudio", "--url", "http://192.0.2.20:1234", "--model", "qwen3"]),
    ("POST", "/api/hosts/ct1/llm/lmstudio/check", {"agent": "opencode"}, ["llm", "check", "lmstudio", "--agent",
                                                                           "opencode"]),
    ("GET", "/api/hosts/ct1/llm/lmstudio/models", None, ["llm", "models", "lmstudio", "--json"]),
    # "Test connection": the models of a server that is not registered, by its whole URL; nothing is stored
    ("POST", "/api/hosts/ct1/llm-models", {"kind": "ollama", "url": "http://192.168.1.9:11434"},
     ["wizard", "models", "--kind=ollama", "--url=http://192.168.1.9:11434", "--json"]),
    ("POST", "/api/hosts/ct1/llm-models", {"kind": "openai-compatible", "url": "https://gw.example.org:8443/v1/openai"},
     ["wizard", "models", "--kind=openai-compatible", "--url=https://gw.example.org:8443/v1/openai", "--json"]),
    ("POST", "/api/hosts/ct1/llm-models", {"kind": "vllm", "url": "http://[2001:db8::5]:8000"},
     ["wizard", "models", "--kind=vllm", "--url=http://[2001:db8::5]:8000", "--json"]),
    # an https server with its own CA, and one that wants a key: the key is NAMED (read on that host), never sent
    ("POST", "/api/hosts/ct1/llm-models", {"kind": "openai-compatible", "url": "https://gw.example.org",
                                            "ca": "/home/you/ca.pem", "api_key_secret": "GW_KEY"},
     ["wizard", "models", "--kind=openai-compatible", "--url=https://gw.example.org", "--ca=/home/you/ca.pem",
      "--api-key-secret=GW_KEY", "--json"]),
    # an edited server's own CA (the form cannot send it again): named, read on that host (review 5 M7)
    ("POST", "/api/hosts/ct1/llm-models", {"kind": "openai-compatible", "url": "https://gw.example.org", "ca_of": "lan"},
     ["wizard", "models", "--kind=openai-compatible", "--url=https://gw.example.org", "--ca-of=lan", "--json"]),
    # review 5 M3: the key over plain http outside the local networks only when asked for and confirmed
    ("POST", "/api/hosts/ct1/llm-models", {"kind": "ollama", "url": "http://93.184.216.34:11434",
                                            "api_key_secret": "GW_KEY", "insecure_http": True, "confirm": True},
     ["wizard", "models", "--kind=ollama", "--url=http://93.184.216.34:11434", "--api-key-secret=GW_KEY",
      "--insecure-http", "--json"]),
    ("POST", "/api/hosts/ct1/llm", {"name": "gw", "kind": "openai-compatible", "url": "https://gw.example.org",
                                    "api_key_secret": "GW_KEY"},
     ["llm", "add", "gw", "--kind", "openai-compatible", "--url", "https://gw.example.org", "--api-key-secret=GW_KEY"]),
    # "_" in DNS names as they have it in practice (SRV-like and service names)
    ("POST", "/api/hosts/ct1/net/web", {"op": "allow", "allow_host": ["_sip._tcp.example.org", "my_host.lan"],
                                        "confirm": True},
     ["net", "allow", "web", "--allow-host=_sip._tcp.example.org", "--allow-host=my_host.lan"]),
    ("POST", "/api/hosts/ct1/remembered/forget", {"project": "/home/u/src/web", "entry": "_svc.my_host.lan:8443",
                                                  "confirm": True},
     ["approvals", "forget", "/home/u/src/web", "_svc.my_host.lan:8443"]),
    ("DELETE", "/api/hosts/ct1/llm/lmstudio", {"confirm": True}, ["llm", "remove", "lmstudio"]),
    ("GET", "/api/hosts/ct1/defaults", None, ["defaults", "show", "--json"]),
    ("POST", "/api/hosts/ct1/defaults", {"key": "telemetry", "value": "strict"},
     ["defaults", "set", "telemetry", '"strict"']),
    ("POST", "/api/hosts/ct1/defaults", {"key": "telemetry", "value": None}, ["defaults", "unset", "telemetry"]),
    ("POST", "/api/hosts/ct1/defaults", {"key": "mode", "value": "high"}, ["defaults", "set", "mode", '"high"']),
    ("POST", "/api/hosts/ct1/defaults", {"key": "mode", "value": "lax", "confirm": True},
     ["defaults", "set", "mode", '"lax"']),
    ("POST", "/api/hosts/ct1/defaults", {"key": "telemetry", "value": "on", "confirm": True},
     ["defaults", "set", "telemetry", '"on"']),
    ("POST", "/api/hosts/ct1/defaults", {"key": "canaries", "value": False, "confirm": True},
     ["defaults", "set", "canaries", "false"]),
    # fewer canary categories than the host has now (its default: credentials and home) is lowering: confirmed;
    # more of them is not
    ("POST", "/api/hosts/ct1/defaults", {"key": "canaries", "value": ["home"], "confirm": True},
     ["defaults", "set", "canaries", '["home"]']),
    ("POST", "/api/hosts/ct1/defaults", {"key": "canaries", "value": ["credentials", "home", "workspace"]},
     ["defaults", "set", "canaries", '["credentials", "home", "workspace"]']),
    ("POST", "/api/hosts/ct1/defaults", {"key": "telemetry", "value": "strict"}, ["defaults", "set", "telemetry", '"strict"']),
    # the network of a running sandbox: every value as --flag=VALUE, the token never (only where it is)
    ("GET", "/api/hosts/ct1/net/web", None, ["net", "show", "web", "--json"]),
    ("POST", "/api/hosts/ct1/net/web", {"op": "allow", "allow_host": ["pypi.org"], "allow_tcp": ["192.0.2.9:5432"],
                                        "host_ports": [3000], "dns": True, "confirm": True,
                                        "git_login": {"url": "http://localhost:3080", "kind": "secret", "ref": "GITEA"}},
     ["net", "allow", "web", "--allow-host=pypi.org", "--allow-tcp=192.0.2.9:5432", "--allow-host-port=3000", "--dns",
      "--git-token-secret=http://localhost:3080=GITEA"]),
    ("POST", "/api/hosts/ct1/net/web", {"op": "revoke", "host_ports": [3000]},
     ["net", "revoke", "web", "--allow-host-port=3000"]),
    # host wildcards as `net allow` takes them (*.DOMAIN.TLD), unchanged: the command refuses public suffixes
    ("POST", "/api/hosts/ct1/net/web", {"op": "allow", "allow_host": ["*.example.org", "*.api.Example.co.uk"],
                                        "confirm": True},
     ["net", "allow", "web", "--allow-host=*.example.org", "--allow-host=*.api.Example.co.uk"]),
    ("POST", "/api/hosts/ct1/net/web", {"op": "revoke", "allow_host": ["*.example.org"]},
     ["net", "revoke", "web", "--allow-host=*.example.org"]),
    ("GET", "/api/hosts/ct1/presets", None, ["presets", "list", "--json"]),
    # what a running sandbox is given while it runs: workspaces, devices, limits (a widening one confirmed)
    ("GET", "/api/hosts/ct1/workspaces/web", None, ["workspace", "list", "web", "--json"]),
    ("POST", "/api/hosts/ct1/workspaces/web", {"directory": "/home/u/src/other", "read_only": True},
     ["workspace", "add", "web", "/home/u/src/other", "--ro"]),
    ("POST", "/api/hosts/ct1/workspaces/web", {"directory": "/home/u/src/my lib", "confirm": True},
     ["workspace", "add", "web", "/home/u/src/my lib"]),
    # a file or a folder ("path"), with a name of its own at /workspaces/NAME
    ("POST", "/api/hosts/ct1/workspaces/web", {"path": "/home/u/notes.md", "name": "notes.v2", "read_only": True},
     ["workspace", "add", "web", "/home/u/notes.md", "--name=notes.v2", "--ro"]),
    ("POST", "/api/hosts/ct1/workspaces/web", {"path": "/home/u/src/lib", "name": "", "confirm": True},
     ["workspace", "add", "web", "/home/u/src/lib"]),
    ("POST", "/api/hosts/ct1/workspaces/web", {"path": "/home/u/src/lib", "name": "Lib_2", "read_only": False,
                                               "confirm": True},
     ["workspace", "add", "web", "/home/u/src/lib", "--name=Lib_2"]),
    ("POST", "/api/hosts/ct1/workspaces/web/remove", {"workspace": "/workspaces/other"},
     ["workspace", "remove", "web", "/workspaces/other"]),
    # an MCP server (or a helper agent) into a running start now, and out of it again (--live)
    ("POST", "/api/hosts/ct1/starts/web/mcp-live", {"server": "files", "confirm": True},
     ["mcp", "attach", "files", "web", "--live", "--json"]),
    ("POST", "/api/hosts/ct1/starts/web/mcp-live", {"helper": "codex", "confirm": True},
     ["mcp", "attach", "--helper=codex", "web", "--live", "--json"]),
    ("POST", "/api/hosts/ct1/starts/web/mcp-live/remove", {"server": "helper-claude-0123abcd", "confirm": True},
     ["mcp", "detach", "helper-claude-0123abcd", "web", "--live", "--json"]),
    ("POST", "/api/hosts/ct1/starts/web/mcp-live/remove", {"helper": "opencode", "confirm": True},
     ["mcp", "detach", "--helper=opencode", "web", "--live", "--json"]),
    ("POST", "/api/hosts/ct1/workspaces/web/remove", {"workspace": "other-2", "force": True, "confirm": True},
     ["workspace", "remove", "web", "other-2", "--force"]),
    ("GET", "/api/hosts/ct1/devices/web", None, ["devices", "list", "web", "--json"]),
    ("POST", "/api/hosts/ct1/devices/web", {"device": "/dev/ttyUSB0", "confirm": True},
     ["devices", "add", "web", "/dev/ttyUSB0"]),
    ("POST", "/api/hosts/ct1/devices/web", {"device": "usb:2e8a:000A:E6609CB2D3A7", "confirm": True},
     ["devices", "add", "web", "usb:2e8a:000A:E6609CB2D3A7"]),
    ("POST", "/api/hosts/ct1/devices/web/remove", {"device": "3"}, ["devices", "remove", "web", "3"]),
    ("POST", "/api/hosts/ct1/devices/web/remove", {"device": "/dev/bus/usb/001/004"},
     ["devices", "remove", "web", "/dev/bus/usb/001/004"]),
    ("POST", "/api/hosts/ct1/limits/web", {"memory": "8g", "cpus": "1.5", "pids": "none", "confirm": True},
     ["limits", "web", "--memory=8g", "--cpus=1.5", "--pids=none"]),
    ("POST", "/api/hosts/ct1/limits/web", {"memory": "8g", "cpus": "1.5"}, ["limits", "web", "--memory=8g", "--cpus=1.5"]),
    ("GET", "/api/hosts/ct1/limits/web", None, ["limits", "web", "--show", "--json"]),
    ("POST", "/api/hosts/local/limits/web", {"memory": "", "pids": "4096"}, ["limits", "web", "--pids=4096"]),
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
    ("DELETE", "/api/hosts/ct1/vpn/home", {"confirm": True}, ["vpn", "remove", "home"]),
    # kits: the values after "--", options as --flag=VALUE
    ("GET", "/api/hosts/ct1/kits", None, ["kits", "list", "--json"]),
    ("POST", "/api/hosts/ct1/kits", {"name": "team", "agents": ["claude", "codex"]},
     ["kits", "new", "--agent=claude", "--agent=codex", "--", "team"]),
    ("POST", "/api/hosts/ct1/kits/team/add-skill", {"path": "/home/u/skills/review", "as": "review2",
                                                    "agents": ["claude"], "confirm": True},
     ["kits", "add-skill", "--as=review2", "--agent=claude", "--", "team", "/home/u/skills/review"]),
    ("POST", "/api/hosts/ct1/kits/team/new-skill", {"skill": "notes", "description": "-when writing notes"},
     ["kits", "new-skill", "--description=-when writing notes", "--", "team", "notes"]),
    ("POST", "/api/hosts/ct1/kits/team/add-plugin", {"plugin": "egm@sandbox-ai", "agent": "claude",
                                                     "source": "sandbox-ai", "confirm": True},
     ["kits", "add-plugin", "--agent=claude", "--source=sandbox-ai", "--", "team", "egm@sandbox-ai"]),
    ("POST", "/api/hosts/ct1/kits/team/add-plugin", {"plugin": "@scope/pkg@1.2", "agent": "opencode", "confirm": True},
     ["kits", "add-plugin", "--agent=opencode", "--", "team", "@scope/pkg@1.2"]),
    ("POST", "/api/hosts/ct1/kits/team/add-marketplace", {"marketplace": "tools", "source": "owner/repo",
                                                          "confirm": True},
     ["kits", "add-marketplace", "--", "team", "tools", "owner/repo"]),
    ("POST", "/api/hosts/ct1/kits/team/remove-skill", {"skill": "notes", "agents": ["codex"], "confirm": True},
     ["kits", "remove-skill", "--agent=codex", "--", "team", "notes"]),
    ("POST", "/api/hosts/ct1/kits/team/remove-plugin", {"plugin": "egm@sandbox-ai", "confirm": True},
     ["kits", "remove-plugin", "--", "team", "egm@sandbox-ai"]),
    ("DELETE", "/api/hosts/ct1/kits/team", {"confirm": True}, ["kits", "delete", "--yes", "--", "team"]),
    # management: rename, a start's history, gc (preview, then confirmed), untaint, answers given before
    ("POST", "/api/hosts/ct1/starts/web/rename", {"new": "web-2"}, ["rename", "web", "web-2"]),
    ("GET", "/api/hosts/ct1/starts/web/history", None, ["list", "--history", "web", "--json"]),
    ("GET", "/api/hosts/ct1/gc", None, ["gc", "--dry-run", "--json"]),
    ("GET", "/api/hosts/ct1/gc/30", None, ["gc", "--dry-run", "--json", "--older-than=30"]),
    ("POST", "/api/hosts/ct1/gc", {"older_than": 30, "confirm": True}, ["gc", "--json", "--older-than=30"]),
    ("POST", "/api/hosts/ct1/gc", {"older_than": None, "confirm": True}, ["gc", "--json"]),
    ("POST", "/api/hosts/ct1/net/web/untaint", {"confirm": True}, ["net", "untaint", "web"]),
    ("POST", "/api/hosts/ct1/approvals/12-3-4", {"decision": "allow", "scope": "always", "confirm": True},
     ["approvals", "allow", "12-3-4", "--scope", "always"]),
    ("GET", "/api/hosts/ct1/approvals-history", None, ["approvals", "history"]),
    ("GET", "/api/hosts/ct1/approvals-history/claude-1", None, ["approvals", "history", "claude-1"]),
    ("POST", "/api/hosts/ct1/remembered/forget", {"project": "/home/u/src/web", "entry": "pypi.org", "confirm": True},
     ["approvals", "forget", "/home/u/src/web", "pypi.org"]),
    ("POST", "/api/hosts/ct1/remembered/forget", {"project": "/home/u/src/web", "entry": "[2001:db8::1]:443",
                                                  "confirm": True},
     ["approvals", "forget", "/home/u/src/web", "[2001:db8::1]:443"]),
    ("POST", "/api/hosts/ct1/secrets/GITEA_TOKEN", {"secret": "glpat-x", "confirm": True},
     ["secrets", "set", "GITEA_TOKEN"]),
    # the Logins page's "Enter the token": stored under the name it suggests (or another), the token on standard
    # input only, never in the arguments; the login then names it
    ("POST", "/api/hosts/ct1/secrets/GIT_TOKEN_GIT_EXAMPLE_ORG", {"secret": "glpat-the-token-itself"},
     ["secrets", "set", "GIT_TOKEN_GIT_EXAMPLE_ORG"]),
    ("POST", "/api/hosts/ct1/secrets/TESTPYPI_TOKEN", {"secret": "pypi-" + "A" * 200},
     ["secrets", "set", "TESTPYPI_TOKEN"]),
    ("DELETE", "/api/hosts/ct1/secrets/GITEA_TOKEN", {"confirm": True}, ["secrets", "delete", "GITEA_TOKEN"]),
    ("GET", "/api/hosts/ct1/secrets", None, ["secrets", "list"]),
    # a limit lower than the current one (here: none now) needs no confirmation
    ("POST", "/api/hosts/ct1/limits/web", {"memory": "999999t"}, ["limits", "web", "--memory=999999t"]),
    # the parity actions: each the CLI command of the same name, every value as --flag=VALUE or after the options
    ("GET", "/api/hosts/ct1/projects", None, ["wizard", "trust", "--list", "--json"]),
    ("POST", "/api/hosts/ct1/projects/review", {"path": "/home/u/my web/.sandbox-ai.json"},
     ["wizard", "trust", "/home/u/my web/.sandbox-ai.json", "--show", "--json"]),
    ("POST", "/api/hosts/ct1/projects/forget", {"path": "/home/u/web/.sandbox-ai.json", "confirm": True},
     ["wizard", "forget", "/home/u/web/.sandbox-ai.json"]),
    ("GET", "/api/hosts/ct1/mcp", None, ["mcp", "list", "--json"]),
    ("POST", "/api/hosts/ct1/mcp/files/start", {}, ["mcp", "start", "files"]),
    ("POST", "/api/hosts/ct1/mcp/files/start", {"backend": "bwrap", "vpn": "home"},
     ["mcp", "start", "--backend=bwrap", "--vpn=home", "files"]),
    ("POST", "/api/hosts/ct1/mcp/files/start", {"for": "web", "restart": True, "confirm": True},
     ["mcp", "start", "--for=web", "--json", "--restart", "files"]),
    ("POST", "/api/hosts/ct1/mcp/files/stop", {"confirm": True}, ["mcp", "stop", "files"]),
    ("POST", "/api/hosts/ct1/mcp-catalog", {}, ["mcp", "catalog"]),
    ("POST", "/api/hosts/ct1/mcp-catalog", {"catalogs": ["/home/u/catalog.json", "https://example.org/c.json"]},
     ["mcp", "catalog", "--catalog=/home/u/catalog.json", "--catalog=https://example.org/c.json"]),
    ("POST", "/api/hosts/ct1/mcp", {"entry": "hub:binwalk", "as": "bw", "mounts": [
        {"target": "/mnt/samples", "host": "/home/u/samples", "mode": "ro"}], "env": ["VT_KEY"], "network": "full",
        "memory": "2g", "pids": 256, "no_pull": True, "backend": "both", "vpn": "home"},
     ["mcp", "add", "--as=bw", "--mount=/mnt/samples=/home/u/samples:ro", "--env=VT_KEY", "--network=full",
      "--memory=2g", "--pids=256", "--no-pull", "--backend=both", "--vpn=home", "hub:binwalk"]),
    ("GET", "/api/hosts/ct1/git-logins", None, ["git-logins", "list", "--json"]),
    ("POST", "/api/hosts/ct1/git-logins", {"url": "http://localhost:3080", "secret": "GITEA", "user": "u.1",
                                            "confirm": True},
     ["git-logins", "add", "--secret=GITEA", "--user=u.1", "http://localhost:3080"]),
    ("POST", "/api/hosts/ct1/git-logins/remove", {"url": "https://git.example.org", "confirm": True},
     ["git-logins", "remove", "https://git.example.org"]),
    ("POST", "/api/hosts/ct1/git-logins/check", {}, ["git-logins", "check"]),
    ("POST", "/api/hosts/ct1/git-logins/check", {"url": "https://git.example.org"},
     ["git-logins", "check", "https://git.example.org"]),
    ("GET", "/api/hosts/ct1/publish-logins", None, ["publish-logins", "list", "--json"]),
    ("GET", "/api/hosts/ct1/publish-logins/kinds", None, ["publish-logins", "kinds"]),
    ("POST", "/api/hosts/ct1/publish-logins", {"kind": "npm", "secret": "NPM_TOKEN", "url": "https://npm.example.org"},
     ["publish-logins", "add", "--secret=NPM_TOKEN", "--url=https://npm.example.org", "npm"]),
    ("POST", "/api/hosts/ct1/publish-logins/pypi/remove", {"confirm": True}, ["publish-logins", "remove", "pypi"]),
    ("POST", "/api/hosts/ct1/publish-logins/check", {"kind": "crates"}, ["publish-logins", "check", "crates"]),
    ("GET", "/api/hosts/ct1/identities", None, ["identities", "list", "--json"]),
    ("GET", "/api/hosts/ct1/identities/work", None, ["identities", "show", "--json", "work"]),
    ("POST", "/api/hosts/ct1/identities", {"identity": "work", "name": "Ann Example", "email": "ann@example.org",
                                            "description": "-the work account"},
     ["identities", "add", "--name=Ann Example", "--email=ann@example.org", "--description=-the work account", "work"]),
    ("POST", "/api/hosts/ct1/identities/work", {"email": "ann@example.com"},
     ["identities", "set", "--email=ann@example.com", "work"]),
    ("POST", "/api/hosts/ct1/identities/work/login", {"url": "https://github.com", "secret": "GH_WORK", "user": "ann"},
     ["identities", "login", "--secret=GH_WORK", "--user=ann", "work", "https://github.com"]),
    ("POST", "/api/hosts/ct1/identities/work/publish", {"kind": "pypi", "secret": "PYPI_WORK"},
     ["identities", "publish", "--secret=PYPI_WORK", "work", "pypi"]),
    ("POST", "/api/hosts/ct1/identities/work/logout", {"url": "https://github.com", "confirm": True},
     ["identities", "logout", "work", "https://github.com"]),
    ("POST", "/api/hosts/ct1/identities/work/unpublish", {"kind": "pypi", "confirm": True},
     ["identities", "unpublish", "work", "pypi"]),
    ("POST", "/api/hosts/ct1/identities-check", {}, ["identities", "check"]),
    ("POST", "/api/hosts/ct1/identities-check", {"identity": "work"}, ["identities", "check", "work"]),
    ("POST", "/api/hosts/ct1/net/web", {"op": "allow", "allow": ["192.0.2.0/24", "2001:db8::1"], "presets": ["pypi", "kernel-org"],
                                        "nameservers": ["192.0.2.53"], "keep": True, "confirm": True,
                                        "git_login": {"url": "https://git.example.org", "kind": "file",
                                                      "ref": "/home/u/.config/git-token"}},
     ["net", "allow", "web", "--allow=192.0.2.0/24", "--allow=2001:db8::1", "--allow-pypi", "--allow-kernel-org",
      "--nameserver=192.0.2.53", "--git-token-file=https://git.example.org=/home/u/.config/git-token", "--keep"]),
    ("POST", "/api/hosts/ct1/net/web", {"op": "revoke", "presets": ["npm"]}, ["net", "revoke", "web", "--allow-npm"]),
    ("POST", "/api/hosts/ct1/net/web/keep", {"all": True, "confirm": True}, ["net", "show", "web", "--keep-all"]),
    ("GET", "/api/hosts/ct1/starts/all", None, ["list", "--all", "--json"]),
    ("POST", "/api/hosts/ct1/starts/web/stop", {"wait": 90, "confirm": True}, ["stop", "web", "--wait=90"]),
    ("POST", "/api/hosts/ct1/starts/web/resume", {"workspaces": ["/home/u/lib"], "mounts": [
        {"source": "/home/u/data", "destination": "/mnt/data", "mode": "ro"}], "mcp": ["files"], "confirm": True},
     ["resume", "--workspace=/home/u/lib", "--mount=/home/u/data:/mnt/data:ro", "--mcp=files", "web"]),
    ("GET", "/api/hosts/ct1/presets/development", None, ["presets", "show", "--json", "development"]),
    ("POST", "/api/hosts/ct1/presets/hardware-analysis/check", {"values": {"device": "/dev/ttyUSB0", "port": ""},
                                                                 "agent": "codex"},
     ["presets", "check", "--value=device=/dev/ttyUSB0", "--agent=codex", "hardware-analysis"]),
    ("GET", "/api/hosts/ct1/preset-dirs", None, ["presets", "dirs", "list", "--json"]),
    ("POST", "/api/hosts/ct1/preset-dirs", {"directory": "/home/u/presets", "confirm": True},
     ["presets", "dirs", "add", "/home/u/presets"]),
    ("POST", "/api/hosts/ct1/preset-dirs/remove", {"directory": "/home/u/presets"},
     ["presets", "dirs", "remove", "/home/u/presets"]),
    ("GET", "/api/hosts/ct1/canaries/claude-1/list", None, ["canaries", "list", "claude-1", "--json"]),
    ("GET", "/api/hosts/ct1/kits/team", None, ["kits", "show", "--json", "--", "team"]),
    ("POST", "/api/hosts/ct1/kits/team/add-hook", {"file": "/home/u/hooks/guard.sh", "as": "guard.sh",
                                                   "event": "PreToolUse", "matcher": "Bash|Write", "timeout": 30,
                                                   "agents": ["claude"], "confirm": True},
     ["kits", "add-hook", "--agent=claude", "--as=guard.sh", "--event=PreToolUse", "--matcher=Bash|Write",
      "--timeout=30", "--", "team", "/home/u/hooks/guard.sh"]),
    ("POST", "/api/hosts/ct1/kits/team/remove-hook", {"file": "guard.sh", "confirm": True},
     ["kits", "remove-hook", "--", "team", "guard.sh"]),
    ("GET", "/api/hosts/ct1/llm/gpu", None, ["llm", "show", "gpu", "--json"]),
    ("POST", "/api/hosts/ct1/llm", {"name": "gw", "kind": "openai-compatible", "url": "https://gw.example.org",
                                    "header_secrets": [{"header": "X-Gateway-Key", "secret": "GW_KEY"}],
                                    "ca": "/home/u/ca.pem", "vpn": "home", "anthropic": True, "replace": True,
                                    "confirm": True},
     ["llm", "add", "gw", "--kind", "openai-compatible", "--url", "https://gw.example.org",
      "--header-secret=X-Gateway-Key=GW_KEY", "--ca=/home/u/ca.pem", "--vpn=home", "--anthropic", "--replace"]),
    ("GET", "/api/hosts/ct1/vpn/home", None, ["vpn", "show", "home", "--json"]),
    ("POST", "/api/hosts/ct1/vpn", {"name": "tor", "kind": "tor", "bridge_secret": "TOR_BRIDGES", "exit_country": "de,nl"},
     ["vpn", "add", "tor", "--tor", "--bridge-secret=TOR_BRIDGES", "--exit-country=de,nl"]),
    ("POST", "/api/hosts/ct1/vpn", {"name": "ts", "kind": "tailscale", "hostname": "box-1", "ephemeral": True,
                                    "tailnet_allow": ["100.64.0.0/10"], "dns": ["100.100.100.100"],
                                    "check_urls": ["https://ip.example.org"], "provider": "mine", "force": True,
                                    "no_dns_probe": True, "confirm": True},
     ["vpn", "add", "ts", "--tailscale", "--dns=100.100.100.100", "--check-url=https://ip.example.org",
      "--provider=mine", "--no-dns-probe", "--force", "--ephemeral", "--hostname=box-1",
      "--tailnet-allow=100.64.0.0/10"]),
    ("POST", "/api/hosts/ct1/vpn", {"name": "mv", "kind": "mullvad", "no_ipv6": True},
     ["vpn", "add", "mv", "--mullvad", "--no-ipv6"]),
    ("POST", "/api/hosts/ct1/logs", {"project": "/home/u/src/web"}, ["logs", "--json", "--project=/home/u/src/web"]),
    ("POST", "/api/hosts/ct1/logs", {"directory": "/home/u/.local/state/sandbox-ai/logs/web"},
     ["logs", "--json", "/home/u/.local/state/sandbox-ai/logs/web"]),
    ("POST", "/api/hosts/ct1/privacy-check", {"paths": ["/home/u/src/web"], "rules_dir": "/home/u/rules",
                                              "project": "web", "all": True},
     ["privacy-check", "--json", "--rules-dir=/home/u/rules", "--project=web", "--all", "/home/u/src/web"]),
    ("POST", "/api/hosts/ct1/activity", {"tail": 50, "kinds": ["tool", "network"], "grep": "-rf", "since": "2h",
                                         "run": "claude-1"},
     ["activity", "--json", "--tail=50", "--kind=tool", "--kind=network", "--grep=-rf", "--since=2h", "claude-1"]),
    ("POST", "/api/hosts/ct1/activity", {}, ["activity", "--json", "--tail=200"]),
    ("GET", "/api/hosts/ct1/doctor/bwrap", None, ["doctor", "--json", "--backend=bwrap"]),
    ("GET", "/api/hosts/ct1/version", None, ["version", "--verbose"]),
    ("GET", "/api/hosts/ct1/images", None, ["images", "--json"]),
    ("GET", "/api/hosts/ct1/demo-device", None, ["demo-device", "status"]),
    ("POST", "/api/hosts/ct1/demo-device/start", {"port": 5555}, ["demo-device", "start", "--port=5555"]),
    ("POST", "/api/hosts/ct1/demo-device/stop", {}, ["demo-device", "stop"]),
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
    # what ends or weakens something needs its confirmation (the page asks first)
    ("POST", "/api/hosts/ct1/starts/web/stop", {}, 400),
    ("POST", "/api/hosts/ct1/starts/web/stop", {"confirm": "yes"}, 400),
    ("POST", "/api/hosts/ct1/starts/web/delete", {"confirm": 1}, 400),
    ("GET", "/api/hosts/ct1/starts/--all/review", None, 400),
    ("GET", "/api/hosts/ct1/starts/-h/review", None, 400),
    ("POST", "/api/hosts/ct1/starts/web/review", {}, 405),
    ("POST", "/api/hosts/ct1/approvals/1-2", {"decision": "deny", "scope": "always"}, 400),
    ("POST", "/api/hosts/ct1/approvals/1-2", {"decision": "deny", "scope": "always", "confirm": "true"}, 400),
    ("POST", "/api/hosts/ct1/approvals/1-2", {"decision": "deny", "no_open": True}, 400),
    ("POST", "/api/hosts/ct1/approvals/1-2", {"decision": "allow", "no_open": "yes"}, 400),
    ("POST", "/api/hosts/ct1/approvals/-x", {"decision": "allow", "no_open": True}, 400),
    ("POST", "/api/hosts/ct1/defaults", {"key": "mode", "value": "lax"}, 400),
    ("POST", "/api/hosts/ct1/defaults", {"key": "telemetry", "value": "on"}, 400),
    ("POST", "/api/hosts/ct1/defaults", {"key": "canaries", "value": []}, 400),
    ("POST", "/api/hosts/ct1/defaults", {"key": "canaries", "value": ["none"]}, 400),
    ("POST", "/api/hosts/ct1/defaults", {"key": "canaries", "value": "none"}, 400),
    ("POST", "/api/hosts/ct1/defaults", {"key": "canaries", "value": 3}, 400),
    ("POST", "/api/hosts/ct1/defaults", {"key": "canaries", "value": False}, 400),
    ("POST", "/api/hosts/ct1/defaults", {"key": "canaries", "value": False, "confirm": 1}, 400),
    ("POST", "/api/hosts/ct1/limits/web", {"memory": "none"}, 400),
    ("POST", "/api/hosts/ct1/limits/web", {"memory": "8g", "pids": "none"}, 400),
    ("POST", "/api/hosts/ct1/limits/web", {"cpus": "none", "confirm": "yes"}, 400),
    ("GET", "/api/hosts/ct1/limits/--all", None, 400),
    ("GET", "/api/hosts/ct1/limits/-h", None, 400),
    ("POST", "/api/hosts/ct1/starts/web/exec", {}, 404),
    ("POST", "/api/hosts/ct1/approvals/a b", {"decision": "allow"}, 400),
    # Regression: an id with a leading "-" reached the host, read there as an option (-h answered with the help).
    ("POST", "/api/hosts/ct1/approvals/--scope", {"decision": "allow", "scope": "always"}, 400),
    ("POST", "/api/hosts/ct1/approvals/-h", {"decision": "deny", "scope": "once"}, 400),
    ("POST", "/api/hosts/ct1/approvals/1-2", {"decision": "maybe"}, 400),
    ("POST", "/api/hosts/ct1/approvals/1-2", {"decision": "allow", "scope": "forever"}, 400),
    ("POST", "/api/hosts/ct1/llm", {"name": "--replace", "kind": "lmstudio", "url": "http://x"}, 400),
    ("POST", "/api/hosts/ct1/llm-models", {"kind": "anthropic", "url": "https://x.example.org"}, 400),
    ("POST", "/api/hosts/ct1/llm-models", {"kind": "ollama"}, 400),
    ("POST", "/api/hosts/ct1/llm-models", {"kind": "vllm", "url": "https://x.example.org", "ca_of": "--ca=/etc/x"}, 400),
    ("POST", "/api/hosts/ct1/llm-models", {"kind": "ollama", "url": "http://93.184.216.34:11434",
                                            "api_key_secret": "K", "insecure_http": True}, 400),  # not confirmed
    ("POST", "/api/hosts/ct1/llm-models", {"url": "https://x.example.org"}, 400),
    ("POST", "/api/hosts/ct1/llm-models", {"kind": "ollama", "url": "--json"}, 400),
    ("POST", "/api/hosts/ct1/llm-models", {"kind": "ollama", "url": "ftp://x.example.org"}, 400),
    ("POST", "/api/hosts/ct1/llm-models", {"kind": "ollama", "url": "http://x.example.org/v1?a=b"}, 400),
    ("POST", "/api/hosts/ct1/llm-models", {"kind": "ollama", "url": "http://u:p@x.example.org"}, 400),
    ("POST", "/api/hosts/ct1/llm-models", {"kind": "ollama", "url": "http://x.example.org;id"}, 400),
    ("POST", "/api/hosts/ct1/llm-models", {"kind": "ollama", "url": f"http://x.example.org/{'a' * 300}"}, 400),
    ("POST", "/api/hosts/ct1/llm-models", {"kind": "ollama", "url": 11434}, 400),
    ("POST", "/api/hosts/ct1/llm-models", {"kind": "ollama", "url": "http://x.example.org", "ca": "ca.pem"}, 400),
    ("POST", "/api/hosts/ct1/llm-models", {"kind": "ollama", "url": "http://x.example.org", "ca": "/x/../ca.pem"}, 400),
    ("POST", "/api/hosts/ct1/llm-models", {"kind": "ollama", "url": "http://x.example.org",
                                            "api_key_secret": "sk-the-key-itself"}, 400),
    ("GET", "/api/hosts/ct1/llm-models", None, 405),
    ("POST", "/api/hosts/ct1/llm", {"name": "a", "kind": "lmstudio", "url": "http://x", "model": "-x"}, 400),
    ("POST", "/api/hosts/ct1/llm/a/check", {"agent": "bash"}, 400),
    ("POST", "/api/hosts/ct1/defaults", {"key": "--x", "value": 1}, 400),
    # only the keys of defaults.json (agent_sandbox.DEFAULTS_KEYS), not even with a confirmation
    ("POST", "/api/hosts/ct1/defaults", {"key": "version", "value": 2, "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/defaults", {"key": "made_up", "value": 1, "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/net/web", {"op": "allow", "allow_host": ["-_x.org"], "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/remembered/forget", {"project": "/a", "entry": "-_x.org", "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/llm", {"name": "a", "kind": "lmstudio", "url": "http://x", "api_key_secret": "sk-x"}, 400),
    ("POST", "/api/hosts/ct1/llm", {"name": "a", "kind": "lmstudio", "url": "http://x",
                                    "api_key_secret": "--header-secret"}, 400),
    ("POST", "/api/hosts/ct1/mcp/files/attach", {"start": "web"}, 400),
    ("POST", "/api/hosts/ct1/mcp/files/attach", {"start": "web", "restart": True}, 400),
    ("POST", "/api/hosts/ct1/kits/team/add-plugin", {"plugin": "egm@sandbox-ai"}, 400),
    ("POST", "/api/hosts/ct1/kits/team/add-skill", {"path": "/x", "create": True}, 400),
    ("POST", "/api/hosts/ct1/kits/team/add-marketplace", {"marketplace": "tools", "source": "o/r"}, 400),
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
    # workspaces, devices, limits of a running sandbox: nothing that could be read as an option, every widening
    # one confirmed
    ("GET", "/api/hosts/ct1/workspaces/--all", None, 400),
    ("POST", "/api/hosts/ct1/workspaces/-h", {"directory": "/x", "read_only": True}, 400),
    ("POST", "/api/hosts/ct1/workspaces/web", {"directory": "relative/dir", "read_only": True}, 400),
    ("POST", "/api/hosts/ct1/workspaces/web", {"directory": "--ro", "read_only": True}, 400),
    ("POST", "/api/hosts/ct1/workspaces/web", {"directory": "/home/u/../../etc", "read_only": True}, 400),
    ("POST", "/api/hosts/ct1/workspaces/web", {"directory": "/home/u/a\nb", "read_only": True}, 400),
    ("POST", "/api/hosts/ct1/workspaces/web", {"directory": "/home/u/src"}, 400),  # writable, not confirmed
    ("POST", "/api/hosts/ct1/workspaces/web", {"directory": "/home/u/src", "read_only": False,
                                               "confirm": "yes"}, 400),
    ("POST", "/api/hosts/ct1/workspaces/web", {"directory": "/home/u/src", "read_only": "true"}, 400),
    ("POST", "/api/hosts/ct1/workspaces/web", {"directory": ["/a", "/b"], "read_only": True}, 400),
    ("POST", "/api/hosts/ct1/workspaces/web/remove", {"workspace": "--force"}, 400),
    ("POST", "/api/hosts/ct1/workspaces/web/remove", {"workspace": "-h"}, 400),
    ("POST", "/api/hosts/ct1/workspaces/web/remove", {"workspace": ""}, 400),
    ("POST", "/api/hosts/ct1/workspaces/web/remove", {"workspace": "a/b"}, 400),
    ("POST", "/api/hosts/ct1/workspaces/web/remove", {"workspace": "other", "force": True}, 400),  # not confirmed
    ("POST", "/api/hosts/ct1/workspaces/web/remove", {"workspace": "other", "force": "yes", "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/workspaces/web/drop", {"workspace": "other"}, 404),
    ("GET", "/api/hosts/ct1/devices/-x", None, 400),
    ("POST", "/api/hosts/ct1/devices/web", {"device": "/dev/ttyUSB0"}, 400),  # not confirmed
    ("POST", "/api/hosts/ct1/devices/web", {"device": "/dev/ttyUSB0", "confirm": 1}, 400),
    ("POST", "/api/hosts/ct1/devices/web", {"device": "/etc/shadow", "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/devices/web", {"device": "/dev/../etc/shadow", "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/devices/web", {"device": "/dev/", "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/devices/web", {"device": "--gpu", "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/devices/web", {"device": "usb:2e8a", "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/devices/web", {"device": "usb:2e8a:000a:with space", "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/devices/web/remove", {"device": "-3"}, 400),
    ("POST", "/api/hosts/ct1/devices/web/remove", {"device": 3}, 400),
    ("POST", "/api/hosts/ct1/devices/web/remove", {"device": "all"}, 400),
    ("POST", "/api/hosts/ct1/limits/web", {}, 400),  # nothing to change
    ("POST", "/api/hosts/ct1/limits/web", {"memory": "-1"}, 400),
    ("POST", "/api/hosts/ct1/limits/web", {"memory": "8 g"}, 400),
    ("POST", "/api/hosts/ct1/limits/web", {"cpus": 2}, 400),
    ("POST", "/api/hosts/ct1/limits/web", {"pids": "--memory=none"}, 400),
    ("POST", "/api/hosts/ct1/limits/web", {"memory": "unlimited"}, 400),
    ("POST", "/api/hosts/ct1/limits/--all", {"memory": "8g"}, 400),
    ("DELETE", "/api/hosts/ct1/limits/web", None, 405),
    ("POST", "/api/approvals", {}, 405),
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
    # what widens or removes, without its confirmation (W4, W7)
    ("POST", "/api/hosts/ct1/net/web", {"op": "allow", "dns": True}, 400),
    ("POST", "/api/hosts/ct1/net/web", {"op": "allow", "allow_host": ["*.example.org"], "confirm": "yes"}, 400),
    ("POST", "/api/hosts/ct1/approvals/1-2", {"decision": "allow", "scope": "always"}, 400),
    ("POST", "/api/hosts/ct1/approvals/1-2", {"decision": "allow", "scope": "always", "confirm": 1}, 400),
    ("DELETE", "/api/hosts/ct1/vpn/home", None, 400),
    ("DELETE", "/api/hosts/ct1/llm/lmstudio", {}, 400),
    ("POST", "/api/hosts/ct1/kits/team/remove-skill", {"skill": "notes"}, 400),
    ("POST", "/api/hosts/ct1/kits/team/remove-plugin", {"plugin": "egm@sandbox-ai", "confirm": "true"}, 400),
    ("POST", "/api/hosts/ct1/net/web/untaint", {}, 400),
    ("POST", "/api/hosts/ct1/net/--all/untaint", {"confirm": True}, 400),
    ("POST", "/api/hosts/ct1/gc", {}, 400),
    ("POST", "/api/hosts/ct1/gc", {"older_than": -1, "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/gc", {"older_than": "--dry-run", "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/gc", {"older_than": True, "confirm": True}, 400),
    ("GET", "/api/hosts/ct1/gc/-1", None, 400),
    ("GET", "/api/hosts/ct1/gc/123456", None, 400),
    ("GET", "/api/hosts/ct1/gc/1.5", None, 400),
    ("GET", "/api/hosts/ct1/gc/%D9%A3", None, 400),  # an Arabic-Indic 3 is a digit to str.isdigit, not a number here
    ("POST", "/api/hosts/ct1/gc", {"older_than": "1.5", "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/gc", {"older_than": 1, "confirm": True, "expected": ["web"]}, 400),
    ("POST", "/api/hosts/ct1/gc", {"older_than": 1, "confirm": True, "expected": {"removed": [3]}}, 400),
    ("DELETE", "/api/hosts/ct1/secrets/GITEA_TOKEN", {}, 400),
    ("POST", "/api/hosts/ct1/remembered/forget", {"project": "/home/u/src/web", "entry": "pypi.org"}, 400),
    ("POST", "/api/hosts/ct1/remembered/forget", {"project": "relative", "entry": "pypi.org", "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/remembered/forget", {"project": "-x", "entry": "pypi.org", "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/remembered/forget", {"project": "/a/../b", "entry": "pypi.org", "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/remembered/forget", {"project": "/a", "entry": "--help", "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/remembered/forget", {"project": "/a", "entry": "a b", "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/remembered/forget", {"project": "/a", "entry": ["pypi.org"], "confirm": True}, 400),
    # management: every value checked, nothing read as an option
    ("POST", "/api/hosts/ct1/starts/web/rename", {"new": "--all"}, 400),
    ("POST", "/api/hosts/ct1/starts/web/rename", {"new": "a b"}, 400),
    ("POST", "/api/hosts/ct1/starts/web/rename", {}, 400),
    ("POST", "/api/hosts/ct1/starts/-h/rename", {"new": "x"}, 400),
    ("GET", "/api/hosts/ct1/starts/--all/history", None, 400),
    ("GET", "/api/hosts/ct1/approvals-history/--all", None, 400),
    ("GET", "/api/hosts/ct1/secrets/X", None, 405),
    ("POST", "/api/hosts/ct1/secrets/-x", {"secret": "v", "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/secrets/1X", {"secret": "v", "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/secrets/X", {"secret": "", "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/secrets/X", {"secret": "two\nlines", "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/secrets/X", {"secret": "x" * 8193, "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/secrets/X", {"secret": 42, "confirm": True}, 400),
    ("DELETE", "/api/hosts/ct1/secrets/a%20b", {"confirm": True}, 400),
    # a host's install: every choice checked before the command line is made
    ("POST", "/api/hosts/ct1/install", {"mode": "system", "prefix": "/opt/../etc", "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/install", {"mode": "system", "prefix": "/opt/./x", "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/install", {"mode": "system", "prefix": "opt/sai", "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/install", {"mode": "system", "prefix": "/opt/x;id", "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/install", {"mode": "system", "prefix": "/opt//x", "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/install", {"mode": "system", "prefix": "--help", "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/install", {"mode": "system", "prefix": ["/opt"], "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/install", {"mode": "user", "prefix": "/opt/sai", "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/install", {"mode": "user", "version": "0.1.80", "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/install", {"mode": "user", "source": "here", "version": "0.1.80", "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/install", {"mode": "user", "source": "download", "version": "1.2; id", "confirm": True},
     400),
    ("POST", "/api/hosts/ct1/install", {"mode": "root", "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/install", {"mode": "--system", "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/install", {"mode": ["system"], "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/install", {"mode": "system", "sudo": "yes", "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/install", {"mode": "user", "sudo": True, "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/install", {"mode": "user", "source": "ftp", "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/install", {"mode": "user", "as": "root", "confirm": True}, 400),
    ("POST", "/api/hosts/local/install", {"mode": "user", "confirm": True}, 400),
    ("POST", "/api/hosts/x;y/install", {"mode": "user", "confirm": True}, 400),
    ("POST", "/api/hosts/nowhere/install", {"mode": "user", "confirm": True}, 404),
    ("DELETE", "/api/hosts/ct1/install", {"confirm": True}, 405),
    # the parity actions: nothing that would be read as an option, nothing unchecked
    ("POST", "/api/hosts/ct1/projects/review", {"path": "relative/.sandbox-ai.json"}, 400),
    ("POST", "/api/hosts/ct1/projects/review", {"path": "-x"}, 400),
    ("POST", "/api/hosts/ct1/projects/review", {"path": "/a/../b/.sandbox-ai.json"}, 400),
    ("POST", "/api/hosts/ct1/projects/trust", {"path": "/a/.sandbox-ai.json", "digest": "A" * 64, "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/projects/trust", {"path": "/a/.sandbox-ai.json", "digest": "--yes", "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/projects/trust", {"path": "/a/.sandbox-ai.json", "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/projects/trust", {"path": "/a/.sandbox-ai.json", "digest": "a" * 64}, 400),
    ("POST", "/api/hosts/ct1/projects/forget", {"path": "/a/.sandbox-ai.json"}, 400),
    ("POST", "/api/hosts/ct1/mcp/-x/start", {}, 400),
    ("POST", "/api/hosts/ct1/mcp/files/start", {"backend": "docker"}, 400),
    ("POST", "/api/hosts/ct1/mcp/files/start", {"vpn": "--x"}, 400),
    ("POST", "/api/hosts/ct1/mcp/files/start", {"for": "web"}, 400),  # attaches: asked first
    ("POST", "/api/hosts/ct1/mcp/files/start", {"for": "-x", "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/mcp/files/start", {"restart": True}, 400),
    ("POST", "/api/hosts/ct1/mcp/files/stop", {}, 400),
    ("POST", "/api/hosts/ct1/mcp", {"entry": "--unsigned"}, 400),
    ("POST", "/api/hosts/ct1/mcp", {"entry": "x", "unsigned": True}, 400),
    ("POST", "/api/hosts/ct1/mcp", {"entry": "x", "unsigned": "yes", "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/mcp", {"entry": "x", "mounts": [{"target": "/d", "host": "/h", "mode": "rw"}]}, 400),
    ("POST", "/api/hosts/ct1/mcp", {"entry": "x", "mounts": [{"target": "/d", "host": "/h=/etc"}]}, 400),
    ("POST", "/api/hosts/ct1/mcp", {"entry": "x", "mounts": [{"target": "d", "host": "/h"}]}, 400),
    ("POST", "/api/hosts/ct1/mcp", {"entry": "x", "env": ["sk-the-value"]}, 400),
    ("POST", "/api/hosts/ct1/mcp", {"entry": "x", "network": "allowlist"}, 400),
    ("POST", "/api/hosts/ct1/mcp", {"entry": "x", "memory": "-1"}, 400),
    ("POST", "/api/hosts/ct1/mcp", {"entry": "x", "pids": "many"}, 400),
    ("POST", "/api/hosts/ct1/mcp", {"entry": "x", "catalogs": ["--catalog"]}, 400),
    ("POST", "/api/hosts/ct1/mcp", {"entry": "x", "catalogs": ["http://plain.example.org/c.json"]}, 400),
    ("POST", "/api/hosts/ct1/mcp-catalog", {"catalogs": ["relative.json"]}, 400),
    ("POST", "/api/hosts/ct1/git-logins", {"url": "https://git.example.org", "secret": "glpat-value-itself!"}, 400),
    ("POST", "/api/hosts/ct1/git-logins", {"url": "https://git.example.org", "secret": "T"}, 400),  # asked first
    ("POST", "/api/hosts/ct1/git-logins", {"url": "-x", "secret": "T", "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/git-logins", {"url": "https://g.org", "secret": "T", "user": "-u", "confirm": True}, 400),
    # review 5 M4: a server sandbox-ai would refuse is refused here, before any token is stored for it
    ("POST", "/api/hosts/ct1/git-logins", {"url": "http://git.example.org", "secret": "T", "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/git-logins", {"url": "http://git.example.org", "secret": "T", "check_only": True}, 400),
    ("POST", "/api/hosts/ct1/git-logins", {"url": "https://bad_host.example.org", "secret": "T", "check_only": True}, 400),
    ("POST", "/api/hosts/ct1/git-logins", {"url": "https://git.example.org", "secret": "T", "check_only": "yes"}, 400),
    ("POST", "/api/hosts/ct1/publish-logins", {"kind": "npm", "secret": "T", "url": "https://npm.example.org:8443", "check_only": True}, 400),
    ("POST", "/api/hosts/ct1/git-logins/remove", {"url": "https://git.example.org"}, 400),
    ("POST", "/api/hosts/ct1/git-logins/check", {"url": "--all"}, 400),
    ("POST", "/api/hosts/ct1/publish-logins", {"kind": "pypi", "secret": "pypi-AgEIcHlwaS5vcmc-the-token-itself"}, 400),
    ("POST", "/api/hosts/ct1/publish-logins", {"kind": "docker", "secret": "T"}, 400),
    ("POST", "/api/hosts/ct1/publish-logins", {"kind": "npm", "secret": "T", "url": "https://npm.example.org/path"}, 400),
    ("POST", "/api/hosts/ct1/publish-logins", {"kind": "npm", "secret": "T", "url": "http://npm.example.org"}, 400),
    ("POST", "/api/hosts/ct1/publish-logins/pypi/remove", {}, 400),
    ("POST", "/api/hosts/ct1/publish-logins/-x/remove", {"confirm": True}, 400),
    ("POST", "/api/hosts/ct1/identities", {"identity": "Work"}, 400),
    ("POST", "/api/hosts/ct1/identities", {"identity": "-w"}, 400),
    ("POST", "/api/hosts/ct1/identities", {"identity": "w", "email": "not an address"}, 400),
    ("POST", "/api/hosts/ct1/identities", {"identity": "w", "name": "two\nlines"}, 400),
    ("POST", "/api/hosts/ct1/identities/work", {}, 400),  # nothing to change
    ("POST", "/api/hosts/ct1/identities/work/login", {"url": "https://github.com", "secret": "ghp_x y"}, 400),
    ("POST", "/api/hosts/ct1/identities/work/publish", {"kind": "maven", "secret": "T"}, 400),
    ("POST", "/api/hosts/ct1/identities/work/logout", {"url": "https://github.com"}, 400),
    ("POST", "/api/hosts/ct1/identities/work/unpublish", {"kind": "pypi"}, 400),
    ("DELETE", "/api/hosts/ct1/identities/work", {}, 400),
    ("DELETE", "/api/hosts/ct1/identities/-x", {"confirm": True}, 400),
    ("POST", "/api/hosts/ct1/net/web", {"op": "allow", "allow": ["0.0.0.0/99"], "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/net/web", {"op": "allow", "allow": ["--dns"], "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/net/web", {"op": "allow", "presets": ["everything"], "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/net/web", {"op": "allow", "nameservers": ["dns.example.org"], "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/net/web", {"op": "allow", "presets": ["pypi"]}, 400),  # widens: asked first
    ("POST", "/api/hosts/ct1/net/web", {"op": "revoke", "presets": ["pypi"], "keep": True}, 400),
    ("POST", "/api/hosts/ct1/net/web", {"op": "allow", "presets": ["pypi"], "keep": "yes", "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/net/web", {"op": "allow", "git_login": {"url": "https://g.org", "kind": "file",
                                                                     "ref": "relative"}, "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/net/web", {"op": "allow", "git_login": {"url": "https://g.org", "kind": "file",
                                                                     "ref": "/a/../etc/shadow"}, "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/net/web/keep", {}, 400),
    ("POST", "/api/hosts/ct1/net/-x/keep", {"confirm": True}, 400),
    ("POST", "/api/hosts/ct1/starts/web/stop", {"wait": 0, "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/starts/web/stop", {"wait": "--x", "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/starts/web/resume", {"restart": True}, 400),
    ("POST", "/api/hosts/ct1/starts/web/resume", {"workspaces": ["/home/u/lib"]}, 400),
    ("POST", "/api/hosts/ct1/starts/web/resume", {"mcp": ["files"]}, 400),
    ("POST", "/api/hosts/ct1/starts/web/resume", {"mounts": [{"source": "/a", "destination": "/b", "mode": "rw"}]}, 400),
    ("POST", "/api/hosts/ct1/starts/web/resume", {"workspaces": ["relative"], "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/starts/web/resume", {"mcp": ["-x"], "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/starts/web/resume", {"mounts": [{"source": "/a:b", "destination": "/b"}], "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/starts/web/resume", {"restart": "yes", "confirm": True}, 400),
    # an MCP server while it runs: asked first; one server or one helper, never an option
    ("POST", "/api/hosts/ct1/starts/web/mcp-live", {"server": "files"}, 400),
    ("POST", "/api/hosts/ct1/starts/web/mcp-live", {"server": "files", "confirm": "yes"}, 400),
    ("POST", "/api/hosts/ct1/starts/web/mcp-live", {"helper": "claude"}, 400),
    ("POST", "/api/hosts/ct1/starts/web/mcp-live", {"confirm": True}, 400),
    ("POST", "/api/hosts/ct1/starts/web/mcp-live", {"server": "files", "helper": "claude", "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/starts/web/mcp-live", {"server": "--restart", "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/starts/web/mcp-live", {"server": "-h", "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/starts/web/mcp-live", {"server": "a" * 33, "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/starts/web/mcp-live", {"server": ["files"], "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/starts/web/mcp-live", {"helper": "bash", "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/starts/web/mcp-live", {"helper": "--live", "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/starts/web/mcp-live", {"helper": ["claude"], "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/starts/--all/mcp-live", {"server": "files", "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/starts/-x/mcp-live/remove", {"server": "files", "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/starts/web/mcp-live/remove", {"server": "files"}, 400),  # cuts its connections: asked
    ("POST", "/api/hosts/ct1/starts/web/mcp-live/remove", {"helper": "codex"}, 400),
    ("POST", "/api/hosts/ct1/starts/web/mcp-live/remove", {"server": "--live", "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/starts/web/mcp-live/remove", {"helper": "gpt", "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/starts/web/mcp-live/remove", {"confirm": True}, 400),
    ("GET", "/api/hosts/ct1/starts/web/mcp-live", None, 405),
    # a workspace: a file or folder, a name as --name takes it
    ("POST", "/api/hosts/ct1/workspaces/web", {"path": "relative/notes.md", "read_only": True}, 400),
    ("POST", "/api/hosts/ct1/workspaces/web", {"path": "/home/u/../etc", "read_only": True}, 400),
    ("POST", "/api/hosts/ct1/workspaces/web", {"path": "/home/u/a", "directory": "/home/u/a", "read_only": True}, 400),
    ("POST", "/api/hosts/ct1/workspaces/web", {"read_only": True}, 400),
    ("POST", "/api/hosts/ct1/workspaces/web", {"path": "/home/u/a"}, 400),  # writable: asked first
    ("POST", "/api/hosts/ct1/workspaces/web", {"path": "/home/u/a", "name": "x", "read_only": False}, 400),
    ("POST", "/api/hosts/ct1/workspaces/web", {"path": "/home/u/a", "name": "-x", "read_only": True}, 400),
    ("POST", "/api/hosts/ct1/workspaces/web", {"path": "/home/u/a", "name": ".hidden", "read_only": True}, 400),
    ("POST", "/api/hosts/ct1/workspaces/web", {"path": "/home/u/a", "name": "a/b", "read_only": True}, 400),
    ("POST", "/api/hosts/ct1/workspaces/web", {"path": "/home/u/a", "name": "a b", "read_only": True}, 400),
    ("POST", "/api/hosts/ct1/workspaces/web", {"path": "/home/u/a", "name": "a" * 65, "read_only": True}, 400),
    ("POST", "/api/hosts/ct1/workspaces/web", {"path": "/home/u/a", "name": "a\n", "read_only": True}, 400),
    ("POST", "/api/hosts/ct1/workspaces/web", {"path": "/home/u/a", "name": 7, "read_only": True}, 400),
    ("GET", "/api/hosts/ct1/presets/-x", None, 400),
    ("POST", "/api/hosts/ct1/presets/dev/check", {"values": {"--x": "1"}}, 400),
    ("POST", "/api/hosts/ct1/presets/dev/check", {"values": {"a": "two\nlines"}}, 400),
    ("POST", "/api/hosts/ct1/presets/dev/check", {"agent": "bash"}, 400),
    ("POST", "/api/hosts/ct1/preset-dirs", {"directory": "/home/u/presets"}, 400),  # asked first
    ("POST", "/api/hosts/ct1/preset-dirs", {"directory": "relative", "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/preset-dirs/remove", {"directory": "-x"}, 400),
    ("GET", "/api/hosts/ct1/canaries/-x/list", None, 400),
    ("POST", "/api/hosts/ct1/canaries/claude-1/tokens", {}, 400),
    ("POST", "/api/hosts/ct1/canaries/claude-1/resume", {}, 400),
    ("POST", "/api/hosts/ct1/canaries/-x/resume", {"confirm": True}, 400),
    ("GET", "/api/hosts/ct1/kits/-x", None, 400),
    ("POST", "/api/hosts/ct1/kits/team/add-hook", {"file": "/home/u/hook.sh"}, 400),  # asked first
    ("POST", "/api/hosts/ct1/kits/team/add-hook", {"file": "hook.sh", "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/kits/team/add-hook", {"file": "/h.sh", "event": "--x", "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/kits/team/add-hook", {"file": "/h.sh", "timeout": 0, "confirm": True}, 400),
    ("POST", "/api/hosts/ct1/kits/team/remove-hook", {"file": "guard.sh"}, 400),
    ("POST", "/api/hosts/ct1/kits/team/remove-hook", {"file": "../guard.sh", "confirm": True}, 400),
    ("GET", "/api/hosts/ct1/llm/-x", None, 400),
    ("POST", "/api/hosts/ct1/llm", {"name": "a", "kind": "vllm", "url": "http://x", "insecure_http": True}, 400),
    ("POST", "/api/hosts/ct1/llm", {"name": "a", "kind": "vllm", "url": "http://x", "header_secrets": [
        {"header": "X-Key", "secret": "the value"}]}, 400),
    ("POST", "/api/hosts/ct1/llm", {"name": "a", "kind": "vllm", "url": "http://x", "header_secrets": [
        {"header": "-H", "secret": "K"}]}, 400),
    ("POST", "/api/hosts/ct1/llm", {"name": "a", "kind": "vllm", "url": "http://x", "ca": "ca.pem"}, 400),
    ("POST", "/api/hosts/ct1/llm", {"name": "a", "kind": "vllm", "url": "http://x", "anthropic": "yes"}, 400),
    ("GET", "/api/hosts/ct1/vpn/-x", None, 400),
    ("POST", "/api/hosts/ct1/vpn", {"name": "tor", "kind": "tor", "socks": True}, 400),  # asked first
    ("POST", "/api/hosts/ct1/vpn", {"name": "tor", "kind": "tor", "bridges": "/b", "bridge_secret": "B"}, 400),
    ("POST", "/api/hosts/ct1/vpn", {"name": "tor", "kind": "tor", "exit_country": "germany"}, 400),
    ("POST", "/api/hosts/ct1/vpn", {"name": "home", "kind": "wireguard", "file": "/x", "split": True}, 400),
    ("POST", "/api/hosts/ct1/vpn", {"name": "home", "kind": "wireguard", "file": "/x", "exit_country": "de"}, 400),
    ("POST", "/api/hosts/ct1/vpn", {"name": "home", "kind": "wireguard", "file": "/x", "dns": ["-x"]}, 400),
    ("POST", "/api/hosts/ct1/vpn", {"name": "ts", "kind": "tailscale", "check_urls": ["http://ip.example.org"]}, 400),
    ("POST", "/api/hosts/ct1/logs", {"directory": "--json"}, 400),
    ("POST", "/api/hosts/ct1/logs", {"project": "relative"}, 400),
    ("POST", "/api/hosts/ct1/privacy-check", {}, 400),
    ("POST", "/api/hosts/ct1/privacy-check", {"paths": ["--all"]}, 400),
    ("POST", "/api/hosts/ct1/privacy-check", {"paths": ["/r"], "project": "-x"}, 400),
    ("POST", "/api/hosts/ct1/activity", {"tail": 0}, 400),
    ("POST", "/api/hosts/ct1/activity", {"kinds": ["everything"]}, 400),
    ("POST", "/api/hosts/ct1/activity", {"run": "--follow"}, 400),
    ("POST", "/api/hosts/ct1/activity", {"since": "yesterday"}, 400),
    ("POST", "/api/hosts/ct1/activity", {"grep": "a\nb"}, 400),
    ("GET", "/api/hosts/ct1/doctor/docker", None, 400),
    ("POST", "/api/hosts/ct1/demo-device/start", {"port": 80}, 400),
    ("POST", "/api/hosts/ct1/demo-device/start", {"port": "--x"}, 400),
])
def test_what_is_refused_never_reaches_a_host(logged_in, method, path, data, status):
    assert logged_in(method, path, data)[0] == status
    assert logged_in.runner.calls == []


def test_a_login_is_checked_before_its_token_is_stored(logged_in):
    """Review 5 M4: check_only checks a Git or publish login as sandbox-ai checks its server, and runs nothing on
    the host (a publish login only lists the logins there, to ask before one is replaced); the page sends it before
    it stores the token."""
    assert logged_in("POST", "/api/hosts/ct1/git-logins", {"url": "https://git.example.org:3000", "secret": "T",
                                                            "check_only": True}) == (200, {"ok": True})
    assert logged_in("POST", "/api/hosts/ct1/git-logins", {"url": "http://localhost:3080", "secret": "T",
                                                            "check_only": True}) == (200, {"ok": True})
    status, said = logged_in("POST", "/api/hosts/ct1/git-logins", {"url": "http://git.lan", "secret": "T", "check_only": True})
    assert status == 400 and "plain http would send the token unencrypted" in said["error"]
    assert logged_in.runner.calls == [], "nothing asked of the host, nothing confirmed"
    logged_in.runner.answers[("publish-logins", "list")] = (0, json.dumps([{"kind": "npm", "secret": "OLD"}]), "")
    assert logged_in("POST", "/api/hosts/ct1/publish-logins", {"kind": "pypi", "secret": "T", "check_only": True}) == \
        (200, {"ok": True})
    status, said = logged_in("POST", "/api/hosts/ct1/publish-logins", {"kind": "npm", "secret": "T", "check_only": True})
    assert status == 400 and said.get("confirm") == "login-replace", "a replacement is asked before the token is stored"
    assert logged_in("POST", "/api/hosts/ct1/publish-logins", {"kind": "npm", "secret": "T", "check_only": True,
                                                                "confirm": True}) == (200, {"ok": True})
    assert {tuple(args[:2]) for _, args in logged_in.runner.calls} == {("publish-logins", "list")}, "nothing added"


def test_a_running_sandboxs_workspaces_and_devices_come_as_the_command_lists_them(logged_in):
    """The lists are the commands' own JSON, passed on; a failure is the command's message (its stderr), as a 502."""
    listed = [{"host": "/home/u/src/web", "sandbox": "/work", "mode": "rw", "source": "start"},
              {"host": "/home/u/src/lib", "sandbox": "/workspaces/lib", "mode": "ro", "source": "added", "kept": False,
               "since": "2026-11-08T10:00:00Z"}]
    logged_in.runner.answers[("workspace", "list")] = (0, json.dumps(listed), "")
    assert logged_in("GET", "/api/hosts/ct1/workspaces/web") == (200, listed)
    grants = [{"id": "1", "value": "/dev/ttyUSB0", "source": "added", "state": "attached", "nodes": ["/dev/ttyUSB0"],
               "device": {}, "rule": None, "ruleset": None, "replugs": 0, "since": "2026-11-08T10:00:00Z"}]
    logged_in.runner.answers[("devices", "list")] = (0, json.dumps(grants), "")
    assert logged_in("GET", "/api/hosts/local/devices/web") == (200, grants)
    logged_in.runner.answers[("workspace", "remove")] = (
        1, "", "sandbox-ai workspace: /workspaces/lib is in use in the sandbox: 42 bash (cwd). Let the agent leave it")
    status, out = logged_in("POST", "/api/hosts/ct1/workspaces/web/remove", {"workspace": "/workspaces/lib"})
    assert status == 502 and "is in use in the sandbox: 42 bash (cwd)" in out["error"] and "on ct1" in out["error"]
    logged_in.runner.answers[("limits",)] = (1, "", "sandbox-ai limits: --memory none: no limit (security mode high)")
    status, out = logged_in("POST", "/api/hosts/ct1/limits/web", {"memory": "none", "confirm": True})
    assert status == 502 and "security mode high" in out["error"]
    logged_in.runner.answers[("devices", "list")] = (1, "", "sandbox-ai devices: no sandbox named web runs")
    status, out = logged_in("GET", "/api/hosts/ct1/devices/web")
    assert status == 502 and "no sandbox named web runs" in out["error"]
    assert logged_in.runner.calls[-1] == ("ct1", ["devices", "list", "web", "--json"])


def test_a_device_add_outlasts_the_question_a_device_rule_may_ask(logged_in):
    timeouts = []
    real = logged_in.svc.api.runner

    def runner(host, args, timeout=120, input=None):
        timeouts.append((args[:2], timeout))
        return real(host, args, timeout=timeout, input=input)
    logged_in.svc.api.runner = runner
    assert logged_in("POST", "/api/hosts/ct1/devices/web", {"device": "/dev/video0", "confirm": True})[0] == 200
    assert timeouts == [(["devices", "add"], w.DEVICE_ADD_TIMEOUT)]
    assert w.DEVICE_ADD_TIMEOUT >= 60 + 60, "devices.APPROVAL_SECONDS and the way there"
    assert "\nAPPROVAL_SECONDS = 60\n" in (ROOT / "sandbox" / "devices.py").read_text()


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
    status, out = logged_in("POST", "/api/hosts/ct1/kits/other/add-skill", {"path": "/x", "create": True, "confirm": True})
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
                                                                         "agents": ["claude"], "confirm": True})["output"]
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
        api.dispatch("POST", "/api/hosts/local/kits/team/add-skill", {"path": str(tmp_path / "missing"),
                                                                       "confirm": True})
    assert refused.value.status == 502 and "does not exist" in str(refused.value)
    api.dispatch("DELETE", "/api/hosts/local/kits/team", {"confirm": True})
    assert not kit.exists()


def test_the_page_has_a_kits_page_that_adds_to_a_kit_of_yours():
    script = (Path(w.__file__).parent / "web" / "app.js").read_text()
    page = script.split("pages.kits = ")[1].split("pages.settings")[0]
    assert '["kits", N_("Kits")]' in script and 'hostPath("/kits")' in script
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
    status, out = logged_in("DELETE", "/api/hosts/ct1")
    assert status == 400 and '{"confirm": true}' in out["error"] and "root@192.0.2.10" in out["error"]
    assert [h["name"] for h in logged_in("GET", "/api/hosts")[1]] == ["local", "ct1"], "not removed unconfirmed"
    assert logged_in("DELETE", "/api/hosts/ct1", {"confirm": True})[0] == 200
    assert logged_in("DELETE", "/api/hosts/ct1", {"confirm": True})[0] == 404
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
    for path in ("/static/app.js", "/static/i18n.js", "/static/app.css", "/static/themes.json", "/static/icon.svg"):
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
    # On this machine the chosen workspace is given as is (a project file in the service's directory never takes over).
    assert argv[-8:] == ["codex", "--name", "n1", "--workspace", "/srv/app", "--workspaces-as-given", "--mode", "high"]
    # Review 5 M5: also with the workspace field empty (a --workspace may come in the options); on another host
    # where its last status found 0.1.78 or later; never on an older one (which has no such rule either).
    host, argv = w.terminal_command({"host": "local", "kind": "start", "agent": "codex",
                                     "options": json.dumps(["--workspace", "/srv/x"])})
    assert argv[-4:] == ["codex", "--workspaces-as-given", "--workspace", "/srv/x"]
    hosts.save({"ct1": hosts.check("ct1", {"ssh": "root@192.0.2.10"}),
                "old": hosts.check("old", {"ssh": "root@192.0.2.11"}),
                "new": hosts.check("new", {"ssh": "root@192.0.2.12"})})
    hosts.save_statuses({"old": {"version": "0.1.77", "ssh": "root@192.0.2.11", "port": 22, "checked": 1},
                         "new": {"version": "0.1.78", "ssh": "root@192.0.2.12", "port": 22, "checked": 1}})
    for name, given in (("ct1", False), ("old", False), ("new", True)):
        _, argv = w.terminal_command({"host": name, "kind": "start", "agent": "codex", "workspace": "/srv/app"})
        assert ("--workspaces-as-given" in argv[-1]) == given, name
        team = w.tool_command("team-start", {"host": name, "team": "t", "agents": "claude,codex",
                                             "workspace": "/srv/app"})
        assert team[-1:] == (["--workspaces-as-given"] if given else ["--"]), (name, team)
    for query in ({"host": "local", "kind": "shell"}, {"host": "local", "kind": "start", "agent": "bash"},
                  {"host": "local", "kind": "attach", "name": "--all"},
                  {"host": "local", "kind": "start", "agent": "claude", "workspace": "relative"},
                  {"host": "x;y", "kind": "attach", "name": "web"},
                  {"host": "local", "kind": "start", "agent": "claude", "options": json.dumps([1])},
                  # review 5 UX M3 / W5: no workspace at all (the service's own directory would be /work)
                  {"host": "local", "kind": "start", "agent": "claude"},
                  {"host": "local", "kind": "start", "agent": "claude", "options": json.dumps(["--mode", "high", "--", "-w", "/x"])},
                  {"host": "local", "kind": "start", "agent": "claude", "options": json.dumps(["--workspace-ro", "--with", "codex"])}):
        with pytest.raises(w.Refused):
            w.terminal_command(query)
    for options in (["-w", "/srv/x"], ["-w/srv/x"], ["--workspace=/srv/x"]):
        _, argv = w.terminal_command({"host": "local", "kind": "start", "agent": "claude", "options": json.dumps(options)})
        assert argv[-len(options):] == options
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
    assert "/net/${encodeURIComponent(name)}" in script and 'hostPath("/presets", host)' in script
    assert '["--preset", id, ' in script and "git_login" in script and "host_ports" in script


def test_the_page_has_a_vpn_page_that_sends_only_names_of_secrets():
    script = (Path(w.__file__).parent / "web" / "app.js").read_text()
    assert '["vpn", N_("VPN profiles")]' in script and "pages.vpn" in script and 'hostPath("/vpn")' in script
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
    assert "window.confirm" not in script and " confirm(`" not in script, "confirmations are asked in the page"
    assert "function toast(level, title, text, options)" in script
    toast = script.split("function toast(")[1].split("\n}")[0]
    assert "role:" not in toast and "say(" in toast, "told through the page's one live region, not a role of its own"
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
  set textContent(v) { this.children = []; if (String(v) !== "") { const t = new Node("#text"); t.text = String(v); this.append(t); } }
  find(cls) { if ((this.className || "").split(" ").includes(cls)) return this;
    for (const c of this.children) { const f = c.find && c.find(cls); if (f) return f; } return null; }
  findTag(tag) { if (this.tagName === tag) return this;
    for (const c of this.children) { const f = c.findTag && c.findTag(tag); if (f) return f; } return null; }
}
const body = new Node("body");
const live = new Node("div"); live.setAttribute("id", "live"); body.append(live);
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
vm.runInContext(fs.readFileSync(require("path").join(require("path").dirname(process.argv[2]), "i18n.js"), "utf8"), context);
vm.runInContext(source, context);
const box = () => document.getElementById("toasts");
const result = {};
const t1 = context.toast("warning", "<b>asks</b>", "<img src=x onerror=alert(1)>", { action: ["Open approvals", () => { result.acted = true; }] });
result.said = live.textContent;
result.warning = { role: t1.getAttribute("role") || null, cls: t1.className, title: t1.find("title").textContent,
  text: t1.find("text").textContent, textNodes: t1.find("text").children.map(c => c.tagName),
  label: t1.find("sr-only").textContent, glyphHidden: t1.find("glyph").getAttribute("aria-hidden") };
const e1 = context.toast("error", "failed", "exit status 1");
result.error = { role: e1.getAttribute("role") || null, first: box().children[0] === e1, said: live.textContent };
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
    """app.js's toast() in node with a small stand-in for the DOM: told through the one live region, plain text,
    closing on its own (not
    errors, not under the pointer), the close button, the action, at most four, and the badge's sign colour."""
    driver = tmp_path / "toasts.js"
    driver.write_text(TOAST_DRIVER)
    run = subprocess.run(["node", str(driver), str(WEB / "app.js"), str(WEB / "themes.json")], capture_output=True,
                         text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    assert result["said"] == "Warning: <b>asks</b>. <img src=x onerror=alert(1)>", "said once, as text"
    assert result["warning"] == {"role": None, "cls": "toast warning", "title": "<b>asks</b>",
                                 "text": "<img src=x onerror=alert(1)>", "textNodes": ["#text"],
                                 "label": "Warning: ", "glyphHidden": "true"}, "plain text, never markup"
    assert result["error"] == {"role": None, "first": True, "said": "Error: failed. exit status 1"}
    assert result["unknown"] == "toast info"
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
    status, out = logged_in("POST", "/api/hosts/ct1/approvals/12-3-4", {"decision": "allow", "scope": "always",
                                                                       "confirm": True})
    assert status == 200, out
    approvals = w.module("approvals")
    assert timeouts[-1] >= approvals.VERIFY_WAIT + 30, timeouts


def test_the_questions_of_every_host_come_together_and_a_failing_host_is_named(logged_in, monkeypatch):
    """GET /api/approvals asks every host (this machine and each of `sandbox-ai hosts`) at once, each with its own
    time limit: the questions with their host (a question cannot name another host for itself), and why a host
    could not be asked, without holding up the others."""
    hosts = w.module("hosts")
    hosts.save({name: hosts.check(name, {"ssh": f"admin@192.0.2.{i}"}) for i, name in enumerate(("ct1", "ct2", "ct3",
                                                                                                    "slow"), 10)})
    monkeypatch.setattr(w, "APPROVALS_HOST_SECONDS", 1)
    asked, release = [], threading.Event()

    def runner(host, args, timeout=120, input=None):
        asked.append((host, args, timeout))
        if host == "local":
            return 0, json.dumps([{"id": "1-1", "run": "web", "kind": "dns", "question": "look up pypi.org",
                                   "timeout": 60, "seconds_left": 42, "max_scope": "always"}]), ""
        if host == "ct1":
            return 0, json.dumps([{"id": "2-1", "host": "local", "kind": "browser", "url": "https://example.org/x",
                                   "seconds_left": 9, "max_scope": "once"}, "not a question"]), ""
        if host == "ct2":
            return 255, "", "ssh: connect to host 192.0.2.11 port 22: Connection refused\x1b[2J"
        if host == "slow":
            release.wait(10)
            return 0, "[]", ""
        return 0, json.dumps({"not": "a list"}), ""
    logged_in.svc.api.runner = runner
    started = time.monotonic()
    status, out = logged_in("GET", "/api/approvals")
    release.set()
    assert status == 200, out
    assert time.monotonic() - started < 9, "a host that does not answer holds nobody up"
    assert out["questions"] == [
        {"id": "1-1", "run": "web", "kind": "dns", "question": "look up pypi.org", "timeout": 60, "seconds_left": 42,
         "max_scope": "always", "host": "local"},
        {"id": "2-1", "host": "ct1", "kind": "browser", "url": "https://example.org/x", "seconds_left": 9,
         "max_scope": "once"}]
    assert set(out["errors"]) == {"ct2", "ct3", "slow"}
    assert "Connection refused" in out["errors"]["ct2"] and "\x1b" not in out["errors"]["ct2"]
    assert "not a list of questions" in out["errors"]["ct3"] and "1 seconds" in out["errors"]["slow"]
    assert sorted(host for host, _, _ in asked) == ["ct1", "ct2", "ct3", "local", "slow"]
    assert all(args == ["approvals", "list", "--json"] and timeout == 1 for _, args, timeout in asked)
    # Only this machine: one host, nothing else asked.
    hosts.save({})
    del asked[:]
    assert logged_in("GET", "/api/approvals")[1]["errors"] == {} and [h for h, _, _ in asked] == ["local"]


# Every action of NEEDS_CONFIRM with a request that needs it, and the one command it runs once confirmed (None: the
# hosts list on the service's machine). Without {"confirm": true} it is refused, and nothing but the read its check
# needs (READS) reaches the host.
CONFIRM_CASES = {
    "start-stop": ("POST", "/api/hosts/ct1/starts/web/stop", {}, ["stop", "web"]),
    "start-delete": ("POST", "/api/hosts/ct1/starts/web/delete", {}, ["delete", "web", "--yes"]),
    "gc": ("POST", "/api/hosts/ct1/gc", {"older_than": 7}, ["gc", "--json", "--older-than=7"]),
    "approval-allow-always": ("POST", "/api/hosts/ct1/approvals/1-2", {"decision": "allow", "scope": "always"},
                              ["approvals", "allow", "1-2", "--scope", "always"]),
    "approval-deny-always": ("POST", "/api/hosts/ct1/approvals/1-2", {"decision": "deny", "scope": "always"},
                             ["approvals", "deny", "1-2", "--scope", "always"]),
    "approval-forget": ("POST", "/api/hosts/ct1/remembered/forget", {"project": "/home/u/web", "entry": "pypi.org"},
                        ["approvals", "forget", "/home/u/web", "pypi.org"]),
    "defaults-lower": ("POST", "/api/hosts/ct1/defaults", {"key": "mode", "value": "low"},
                       ["defaults", "set", "mode", '"low"']),
    "net-allow": ("POST", "/api/hosts/ct1/net/web", {"op": "allow", "dns": True}, ["net", "allow", "web", "--dns"]),
    "net-untaint": ("POST", "/api/hosts/ct1/net/web/untaint", {}, ["net", "untaint", "web"]),
    "workspace-writable": ("POST", "/api/hosts/ct1/workspaces/web", {"directory": "/home/u/src", "read_only": False},
                           ["workspace", "add", "web", "/home/u/src"]),
    "workspace-force": ("POST", "/api/hosts/ct1/workspaces/web/remove", {"workspace": "other", "force": True},
                        ["workspace", "remove", "web", "other", "--force"]),
    "device-add": ("POST", "/api/hosts/ct1/devices/web", {"device": "/dev/ttyUSB0"}, ["devices", "add", "web",
                                                                                     "/dev/ttyUSB0"]),
    # 8g now: a huge value is no limit either
    "limits-raise": ("POST", "/api/hosts/ct1/limits/web", {"memory": "999999t"}, ["limits", "web", "--memory=999999t"]),
    "host-remove": ("DELETE", "/api/hosts/ct1", {}, None),
    "vpn-remove": ("DELETE", "/api/hosts/ct1/vpn/home", {}, ["vpn", "remove", "home"]),
    "llm-remove": ("DELETE", "/api/hosts/ct1/llm/gpu", {}, ["llm", "remove", "gpu"]),
    "kit-delete": ("DELETE", "/api/hosts/ct1/kits/team", {}, ["kits", "delete", "--yes", "--", "team"]),
    "kit-remove": ("POST", "/api/hosts/ct1/kits/team/remove-plugin", {"plugin": "egm@sandbox-ai"},
                   ["kits", "remove-plugin", "--", "team", "egm@sandbox-ai"]),
    "secret-delete": ("DELETE", "/api/hosts/ct1/secrets/GITEA_TOKEN", {}, ["secrets", "delete", "GITEA_TOKEN"]),
    "secret-replace": ("POST", "/api/hosts/ct1/secrets/GITEA_TOKEN", {"secret": "glpat-new"},
                       ["secrets", "set", "GITEA_TOKEN"]),
    "kit-add": ("POST", "/api/hosts/ct1/kits/team/add-plugin", {"plugin": "egm@sandbox-ai"},
                ["kits", "add-plugin", "--", "team", "egm@sandbox-ai"]),
    "mcp-attach": ("POST", "/api/hosts/ct1/mcp/files/attach", {"start": "web"},
                   ["mcp", "attach", "files", "web", "--json"]),
    "mcp-restart": ("POST", "/api/hosts/ct1/mcp/files/attach", {"start": "web", "restart": True},
                    ["mcp", "attach", "files", "web", "--json", "--restart"]),
    "host-edit": ("POST", "/api/hosts/ct1", {"ssh": "admin@192.0.2.99"}, None),
    # run by this machine's sandbox-ai (it reaches the host over SSH)
    "host-install": ("POST", "/api/hosts/ct1/install", {"mode": "system", "prefix": "/opt/sai"},
                     ["hosts", "install", "ct1", "--system", "--prefix=/opt/sai", "--from-here", "--json"]),
    # the parity actions
    "project-trust": ("POST", "/api/hosts/ct1/projects/trust", {"path": "/home/u/web/.sandbox-ai.json", "digest": "a" * 64},
                      ["wizard", "trust", "/home/u/web/.sandbox-ai.json", "--digest=" + "a" * 64]),
    "project-forget": ("POST", "/api/hosts/ct1/projects/forget", {"path": "/home/u/web/.sandbox-ai.json"},
                       ["wizard", "forget", "/home/u/web/.sandbox-ai.json"]),
    "mcp-stop": ("POST", "/api/hosts/ct1/mcp/files/stop", {}, ["mcp", "stop", "files"]),
    "mcp-detach": ("POST", "/api/hosts/ct1/starts/web/mcp-live/remove", {"server": "files"},
                   ["mcp", "detach", "files", "web", "--live", "--json"]),
    "mcp-unsigned": ("POST", "/api/hosts/ct1/mcp", {"entry": "hub:binwalk", "unsigned": True},
                     ["mcp", "add", "--unsigned", "hub:binwalk"]),
    "mcp-writable-mount": ("POST", "/api/hosts/ct1/mcp", {"entry": "duckduckgo", "mounts": [
        {"target": "/data", "host": "/home/u/data", "mode": "rw"}]}, ["mcp", "add", "--mount=/data=/home/u/data:rw",
                                                                     "duckduckgo"]),
    "git-login-add": ("POST", "/api/hosts/ct1/git-logins", {"url": "https://git.example.org", "secret": "GITEA_TOKEN"},
                      ["git-logins", "add", "--secret=GITEA_TOKEN", "https://git.example.org"]),
    "login-remove": ("DELETE", "/api/hosts/ct1/identities/work", {}, ["identities", "remove", "work"]),
    "net-keep": ("POST", "/api/hosts/ct1/net/web/keep", {}, ["net", "show", "web", "--keep"]),
    "start-restart": ("POST", "/api/hosts/ct1/starts/web/resume", {"restart": True}, ["resume", "--restart", "web"]),
    "canary-tokens": ("POST", "/api/hosts/ct1/canaries/claude-1/tokens", {},
                      ["canaries", "list", "claude-1", "--tokens", "--json"]),
    "canary-resume": ("POST", "/api/hosts/ct1/canaries/claude-1/resume", {}, ["canaries", "resume", "claude-1"]),
    "llm-insecure": ("POST", "/api/hosts/ct1/llm", {"name": "gw", "kind": "vllm", "url": "http://203.0.113.5:8000",
                                                    "insecure_http": True},
                     ["llm", "add", "gw", "--kind", "vllm", "--url", "http://203.0.113.5:8000", "--insecure-http"]),
    "vpn-socks": ("POST", "/api/hosts/ct1/vpn", {"name": "tor", "kind": "tor", "socks": True},
                  ["vpn", "add", "tor", "--tor", "--socks"]),
    # round 4: a replacement of an LLM server or a VPN profile, teams, images
    "llm-replace": ("POST", "/api/hosts/ct1/llm", {"name": "gw", "kind": "vllm", "url": "https://gw.example.org",
                                                   "replace": True},
                    ["llm", "add", "gw", "--kind", "vllm", "--url", "https://gw.example.org", "--replace"]),
    "vpn-replace": ("POST", "/api/hosts/ct1/vpn", {"name": "mv", "kind": "mullvad", "force": True},
                    ["vpn", "add", "mv", "--mullvad", "--force"]),
    "team-stop": ("POST", "/api/hosts/ct1/teams/review/stop", {}, ["team", "stop", "review"]),
    "team-delete": ("DELETE", "/api/hosts/ct1/teams/review", {}, ["team", "delete", "review", "--yes"]),
    "image-file": ("POST", "/api/hosts/ct1/images/data/extend", {"add_files": [
        {"source": "/home/u/tool.sh", "target": "/usr/local/bin/tool", "mode": "0755"}]},
                   ["images", "extend", "data", "--add-file=/home/u/tool.sh:/usr/local/bin/tool:0755", "--json"]),
    "image-remove": ("POST", "/api/hosts/ct1/images/data/remove", {}, ["images", "remove", "data"]),
    # review 5 L9: replacing a login or an MCP server's definition
    "login-replace": ("POST", "/api/hosts/ct1/publish-logins", {"kind": "npm", "secret": "NPM_TOKEN"},
                      ["publish-logins", "add", "--secret=NPM_TOKEN", "npm"]),
    "mcp-replace": ("POST", "/api/hosts/ct1/mcp", {"entry": "duckduckgo", "as": "files"},
                    ["mcp", "add", "--as=files", "duckduckgo"]),
}
# The actions whose command runs on this machine, not on the host they are about.
RUN_HERE = {"host-install"}
READS = (["defaults", "show", "--json"], ["limits", "web", "--show", "--json"], ["secrets", "list"],
         ["publish-logins", "list", "--json"], ["identities", "show", "--json", "work"], ["mcp", "list", "--json"])
# The POST routes that end, remove or weaken nothing (each of the others names its NEEDS_CONFIRM actions): a new route
# has to be put here or in the table, deliberately.
SAFE_POSTS = {r"/api/hosts", r"/api/hosts-status", r"/api/hosts/(?P<host>[^/]+)/test",
              r"/api/hosts/(?P<host>[^/]+)/update", r"/api/hosts/(?P<host>[^/]+)/starts/(?P<name>[^/]+)/rename",
              r"/api/hosts/(?P<host>[^/]+)/llm/(?P<name>[^/]+)/check",
              r"/api/hosts/(?P<host>[^/]+)/vpn/(?P<name>[^/]+)/check", r"/api/hosts/(?P<host>[^/]+)/kits",
              r"/api/hosts/(?P<host>[^/]+)/devices/(?P<name>[^/]+)/remove"} | {
    r"/api/hosts/(?P<host>[^/]+)" + tail for tail in (
        "/projects/review", "/mcp-catalog", "/git-logins/check", "/publish-logins/check",
        "/identities", "/identities-check", "/identities/(?P<name>[^/]+)", "/presets/(?P<name>[^/]+)/check",
        "/preset-dirs/remove", "/logs", "/privacy-check", "/activity", "/demo-device/start", "/demo-device/stop",
        "/teams/(?P<name>[^/]+)/post", "/images-new", "/images-recipe", "/images/(?P<target>[^/]+)/check")}


@pytest.fixture
def confirming(logged_in):
    """A service whose host ct1 has a start with 8g of memory, a secret GITEA_TOKEN and the default defaults."""
    hosts = w.module("hosts")
    hosts.save({"ct1": hosts.check("ct1", {"ssh": "root@192.0.2.10"})})
    logged_in.runner.answers[("limits", "web", "--show")] = (0, json.dumps({"memory": 8 << 30, "memory_high": None,
                                                                            "cpus": 2.0, "pids": 4096}), "")
    logged_in.runner.answers[("secrets", "list")] = (0, "NAME         STORE\nGITEA_TOKEN  file\n", "")
    logged_in.runner.answers[("defaults", "show")] = (0, json.dumps({"values": {}, "canary_default": ["credentials",
                                                                                                     "home"]}), "")
    logged_in.runner.answers[("publish-logins", "list")] = (0, json.dumps([{"kind": "npm", "host": "registry.npmjs.org",
                                                                            "secret": "OLD_TOKEN"}]), "")
    logged_in.runner.answers[("identities", "show")] = (0, json.dumps({"git": [{"url": "https://git.example.org",
                                                                                "user": "u", "secret": "S"}],
                                                                       "publish": []}), "")
    logged_in.runner.answers[("mcp", "list")] = (0, json.dumps([{"name": "files"}]), "")
    return logged_in


def test_the_confirmation_table_covers_every_route_that_ends_removes_or_weakens():
    """One table (NEEDS_CONFIRM): every DELETE route names actions of it, every POST route does or is one that
    weakens nothing (SAFE_POSTS), every action of it is named by a route and has its case here."""
    api = w.Api(FakeRunner())
    named = set()
    for method, pattern, _, actions in api.routes:
        assert set(actions) <= set(w.NEEDS_CONFIRM), (pattern, actions)
        named |= set(actions)
        if method == "DELETE":
            assert actions, f"DELETE {pattern} removes something: it names its confirmation"
        if method == "POST":
            assert bool(actions) != (pattern in SAFE_POSTS), f"POST {pattern}: in the table or in SAFE_POSTS"
        for action in actions:
            case_method, path, _, _ = CONFIRM_CASES[action]
            assert any(m == case_method and re.fullmatch(p, path) and action in a for m, p, _, a in api.routes), action
    assert named | set(w.TERMINAL_CONFIRM) == set(w.NEEDS_CONFIRM) == set(CONFIRM_CASES) | set(w.TERMINAL_CONFIRM)
    assert not named & set(w.TERMINAL_CONFIRM), "a terminal's confirmation is asked by its query"


@pytest.mark.parametrize("action", sorted(CONFIRM_CASES))
def test_what_ends_removes_or_weakens_needs_its_confirmation_on_the_server(confirming, action):
    """The page asks first; the service refuses the same request without {"confirm": true} (another page, a script
    with the session's token), naming why (the table's words); with it, the one command runs."""
    method, path, body, args = CONFIRM_CASES[action]
    for refused in (body, {**body, "confirm": "true"}, {**body, "confirm": 1}):
        status, out = confirming(method, path, refused)
        assert status == 400 and '{"confirm": true}' in out["error"] and w.NEEDS_CONFIRM[action] in out["error"], out
        assert out["confirm"] == action, "the refusal names what it needs: a page out of date asks for exactly that"
    assert [args for _, args in confirming.runner.calls if args not in READS] == [], "nothing changed on a host"
    assert {name for name in w.module("hosts").load()} == {"ct1"}
    confirming.runner.calls.clear()
    status, out = confirming(method, path, {**body, "confirm": True})
    assert status == 200, out
    if action == "host-edit":
        assert w.module("hosts").load()["ct1"]["ssh"] == "admin@192.0.2.99" and confirming.runner.calls == []
    elif args is None:
        assert w.module("hosts").load() == {} and confirming.runner.calls == []
    else:
        where = "local" if action in RUN_HERE else "ct1"
        assert confirming.runner.calls == [(where, args)], "confirmed: no read first, the one command"


def test_what_weakens_nothing_needs_no_confirmation(confirming):
    """Compared with what the host has now: a stricter mode or telemetry, more canary categories, a lower limit, a
    narrower answer, a new secret, a revoke."""
    for path, body in (("/api/hosts/ct1/starts/web/pause", {}), ("/api/hosts/ct1/starts/web/resume", {}),
                       ("/api/hosts/ct1/limits/web", {"memory": "4g", "cpus": "1.5", "pids": "1024"}),
                       ("/api/hosts/ct1/limits/web", {"memory": "8g"}),
                       ("/api/hosts/ct1/approvals/1-2", {"decision": "deny", "scope": "run"}),
                       ("/api/hosts/ct1/approvals/1-2", {"decision": "allow", "scope": "run"}),
                       ("/api/hosts/ct1/defaults", {"key": "mode", "value": "paranoid"}),
                       ("/api/hosts/ct1/defaults", {"key": "mode", "value": None}),
                       ("/api/hosts/ct1/defaults", {"key": "telemetry", "value": "strict"}),
                       ("/api/hosts/ct1/defaults", {"key": "canaries", "value": "all"}),
                       ("/api/hosts/ct1/defaults", {"key": "keep_days", "value": None}),
                       ("/api/hosts/ct1/net/web", {"op": "revoke", "dns": True}),
                       ("/api/hosts/ct1/secrets/NEW_TOKEN", {"secret": "v1"})):
        status, out = confirming("POST", path, body)
        assert status == 200, (path, body, out)
    assert ("ct1", ["secrets", "set", "NEW_TOKEN"]) in confirming.runner.calls


@pytest.mark.parametrize("now, key, value, lowers", [
    ({"mode": "paranoid"}, "mode", "high", True), ({"mode": "paranoid"}, "mode", None, True),
    ({"mode": "lax"}, "mode", "low", False), ({"mode": "high"}, "mode", "high", False),
    ({}, "mode", "low", True), ({}, "mode", "medium", False),
    ({"telemetry": "strict"}, "telemetry", "off", True), ({"telemetry": "strict"}, "telemetry", None, True),
    ({"telemetry": "on"}, "telemetry", "off", False), ({}, "telemetry", "strict", False),
    ({"canaries": ["credentials", "home", "workspace"]}, "canaries", ["credentials", "home"], True),
    ({"canaries": ["credentials", "home", "workspace"]}, "canaries", None, True),
    ({"canaries": False}, "canaries", ["home"], False), ({"canaries": "all"}, "canaries", "credentials,home", True),
    ({}, "canaries", ["credentials", "home"], False), ({}, "canaries", ["workspace", "home", "credentials"], False),
    # keep_days: kept starts removed sooner (or at all, where none was set); unset keeps them
    ({}, "keep_days", 30, True), ({"keep_days": 30}, "keep_days", 7, True), ({"keep_days": 7}, "keep_days", 30, False),
    ({"keep_days": 7}, "keep_days", 7, False), ({"keep_days": 7}, "keep_days", None, False),
    # preset_dirs: another place a start reads presets from; fewer of them is not
    ({}, "preset_dirs", ["/home/u/presets"], True), ({"preset_dirs": ["/a"]}, "preset_dirs", ["/a", "/b"], True),
    ({"preset_dirs": ["/a", "/b"]}, "preset_dirs", ["/a"], False), ({"preset_dirs": ["/a"]}, "preset_dirs", None, False),
    # the subnet: any change of the network of every later start
    ({}, "subnet", "10.9.0.0/24", True), ({"subnet": "10.9.0.0/24"}, "subnet", None, True),
    ({"subnet": "10.9.0.0/24"}, "subnet", "10.9.0.0/24", False),
])
def test_a_default_is_lowered_by_any_step_down_from_what_the_host_has(logged_in, now, key, value, lowers):
    """W3: compared with `defaults show` on that host: mode lax < low < medium < high < paranoid, telemetry on < off <
    strict, canaries as a set (fewer categories, [] or none: no canary files), keep_days lower or new, more
    preset_dirs, another subnet; unset is the default."""
    logged_in.runner.answers[("defaults", "show")] = (0, json.dumps({"values": now, "canary_categories": [
        "credentials", "home", "workspace"], "canary_default": ["credentials", "home"]}), "")
    status, out = logged_in("POST", "/api/hosts/ct1/defaults", {"key": key, "value": value})
    assert (status == 400 and "lowers the protection" in out["error"]) if lowers else status == 200, out
    assert logged_in.runner.calls[0] == ("ct1", ["defaults", "show", "--json"])
    assert len(logged_in.runner.calls) == (1 if lowers else 2)


def test_the_policy_is_the_services_own_table(logged_in):
    status, out = logged_in("GET", "/api/policy")
    assert status == 200 and out["confirm"] == w.NEEDS_CONFIRM
    assert out["order"] == {"mode": ["lax", "low", "medium", "high", "paranoid"], "telemetry": ["on", "off", "strict"]}
    script = (WEB / "app.js").read_text()
    assert 'api("GET", "/api/policy")' in script and "function needsConfirm(action)" in script
    for action in ("net-allow", "defaults-lower"):
        assert f'needsConfirm("{action}")' in script


def test_each_answer_says_how_long_the_session_lives_without_use():
    """W5: X-Session-Idle-Left (the page's polls are no use: it goes down under them) and X-Session-Lifetime-Left;
    a request of the user's renews it; none for a request without a session, or once it is gone."""
    secret = add_user()
    clock = [5000.0]
    svc = service(sessions=w.Sessions(clock=lambda: clock[0]))
    _, headers, body = login(svc, secret)
    ok = {**HEADERS, "Cookie": cookie_of(headers), "X-CSRF-Token": body["csrf"]}
    left = lambda answer: (dict(answer[1]).get("X-Session-Idle-Left"), dict(answer[1]).get("X-Session-Lifetime-Left"))
    assert left(svc.handle("GET", "/api/me", ok, b"", "192.0.2.1")) == (str(w.IDLE), str(w.LIFETIME))
    clock[0] += 600
    assert left(svc.handle("GET", "/api/hosts", {**ok, "X-Background": "1"}, b"", "192.0.2.1")) == (
        str(w.IDLE - 600), str(w.LIFETIME - 600)), "a poll is no use"
    clock[0] += 60
    assert left(svc.handle("GET", "/api/hosts", ok, b"", "192.0.2.1"))[0] == str(w.IDLE), "a request of the user's"
    assert left(svc.handle("GET", "/api/me", HEADERS, b"", "192.0.2.1")) == (None, None)
    assert left(svc.handle("POST", "/api/logout", ok, b"{}", "192.0.2.1")) == (None, None)
    script = (WEB / "app.js").read_text()
    assert 'response.headers.get(name)' in script and '"X-Session-Idle-Left"' in script


def test_a_host_still_being_asked_is_not_asked_again_and_its_last_list_stands_in(logged_in, monkeypatch):
    """At most one request in flight per host (an unreachable one held 15 s each time and the threads piled up): a
    poll while it runs gets its last list with its age, or why there is none yet; once it answered, it is asked
    again."""
    hosts = w.module("hosts")
    hosts.save({"slow": hosts.check("slow", {"ssh": "admin@192.0.2.9"})})
    monkeypatch.setattr(w, "APPROVALS_HOST_SECONDS", 0.2)
    asked, release, answers = [], threading.Event(), {"slow": [{"id": "1-1", "question": "q"}]}

    def runner(host, args, timeout=120, input=None):
        asked.append(host)
        if host == "slow" and len([h for h in asked if h == "slow"]) > 1:
            release.wait(10)
        return 0, json.dumps(answers.get(host, [])), ""
    logged_in.svc.api.runner = runner
    first = logged_in("GET", "/api/approvals")[1]
    assert [q["host"] for q in first["questions"]] == ["slow"] and set(first["age"]) == {"local", "slow"}
    second = logged_in("GET", "/api/approvals")[1]   # slow is asked again, does not answer within the time
    assert "slow" not in second["errors"] and [q["host"] for q in second["questions"]] == ["slow"], \
        "its last list stands in"
    third = logged_in("GET", "/api/approvals")[1]
    assert asked.count("slow") == 2, "not asked again while its request runs"
    assert third["age"]["slow"] >= second["age"]["slow"] and [q["host"] for q in third["questions"]] == ["slow"]
    release.set()
    for _ in range(100):
        if not logged_in.svc.api.approvals_fanout.flight:
            break
        time.sleep(0.05)
    logged_in("GET", "/api/approvals")
    assert asked.count("slow") == 3, "asked again once it answered"


def test_a_host_is_edited_as_hosts_add_checks_it(logged_in, home):
    """Where and how the commands run (ssh, port, key, command, sudo) changes only confirmed; the key is a regular
    file below the service user's HOME, a command never an option; what the fan-outs kept of the host goes."""
    hosts = w.module("hosts")
    hosts.save({"ct1": hosts.check("ct1", {"ssh": "root@192.0.2.10", "port": 2222, "description": "old"})})
    api = logged_in.svc.api
    api.approvals_fanout.last["ct1"] = {"result": [{"id": "1"}], "at": time.monotonic(), "seen": time.time()}
    api.starts_fanout.last["ct1"] = {"result": [], "at": time.monotonic(), "seen": time.time()}
    change = {"ssh": "admin@192.0.2.11", "port": 22, "description": "", "update_sudo": True}
    status, out = logged_in("POST", "/api/hosts/ct1", change)
    assert status == 400 and out["confirm"] == "host-edit" and 'port 2222 -> 22' in out["error"], out
    assert hosts.load()["ct1"]["ssh"] == "root@192.0.2.10" and "ct1" in api.approvals_fanout.last
    status, out = logged_in("POST", "/api/hosts/ct1", {**change, "confirm": True})
    assert status == 200 and hosts.load()["ct1"] == {"ssh": "admin@192.0.2.11", "port": 22, "description": "",
                                                     "update_sudo": True}, out
    assert "ct1" not in api.approvals_fanout.last and "ct1" not in api.starts_fanout.last, "the old host's lists go"
    assert logged_in("POST", "/api/hosts/ct1", {"description": "the rack"})[0] == 200, "a description: no question"
    (home / "home/.ssh").mkdir(parents=True, exist_ok=True)
    os.symlink("/etc/hostname", home / "home/.ssh/link")
    for bad in ({"ssh": "-oProxyCommand=id"}, {"port": 0}, {"identity": "relative/key"}, {"update_sudo": "yes"},
                {"description": "two\nlines"}, {"shell": "/bin/sh"}, {"command": "sandbox-ai; id"},
                {"command": "-oProxyCommand=id"}, {"identity": "/etc/ssh/ssh_host_ed25519_key"},
                {"identity": str(home / "home/.ssh")}, {"identity": str(home / "home/.ssh/link")},
                {"identity": str(home / "home/.ssh/missing")}, {"identity": "~/../../etc/shadow"}):
        assert logged_in("POST", "/api/hosts/ct1", {**bad, "confirm": True})[0] == 400, bad
    key = private_key(home / "home/.ssh/id_ct1", 0o600)
    assert logged_in("POST", "/api/hosts/ct1", {"identity": str(key), "confirm": True})[0] == 200
    assert logged_in("POST", "/api/hosts/nowhere", {"port": 22, "confirm": True})[0] == 404
    assert hosts.load()["ct1"]["ssh"] == "admin@192.0.2.11" and logged_in.runner.calls == []


def test_a_host_edit_waits_for_the_lock_of_hosts_json(logged_in):
    """The read-modify-write of hosts.json holds its lock (the one `sandbox-ai hosts` takes): a change made under it
    by another writer is not lost."""
    hosts = w.module("hosts")
    hosts.save({"ct1": hosts.check("ct1", {"ssh": "root@192.0.2.10"})})
    held, release = threading.Event(), threading.Event()

    def other_writer():
        with hosts.hosts_file_lock():
            held.set()
            release.wait(5)
            hosts.save({**hosts.load(), "ct2": hosts.check("ct2", {"ssh": "root@192.0.2.12"})})
    writer = threading.Thread(target=other_writer)
    writer.start()
    held.wait(5)
    threading.Timer(0.5, release.set).start()
    started = time.monotonic()
    assert logged_in("POST", "/api/hosts/ct1", {"port": 2200, "confirm": True})[0] == 200
    writer.join(5)
    assert time.monotonic() - started >= 0.4, "the edit waited for the other writer"
    assert set(hosts.load()) == {"ct1", "ct2"} and hosts.load()["ct1"]["port"] == 2200, "neither change is lost"


def test_a_damaged_hosts_file_is_a_clear_error_where_it_is_needed(home):
    """A hosts.json that cannot be read is a JSON answer with why (500), never a dropped connection: the host list,
    the versions, a test, an edit, a removal, an add, and every remote host's action (hosts.run reads it)."""
    secret = add_user()
    svc = w.Service({"localhost:8443"}, api=w.Api())  # the real hosts.run: it reads hosts.json first
    _, headers, body = login(svc, secret)
    ok = {**HEADERS, "Cookie": cookie_of(headers), "X-CSRF-Token": body["csrf"]}
    path = w.module("hosts").config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    for method, route, data in (("GET", "/api/hosts", None), ("GET", "/api/hosts-status", None),
                                ("POST", "/api/hosts-status", {}), ("POST", "/api/hosts/ct1/test", {}),
                                ("GET", "/api/hosts/ct1/doctor", None), ("GET", "/api/hosts/ct1/starts", None),
                                ("POST", "/api/hosts/ct1/starts/web/pause", {}),
                                ("POST", "/api/hosts/ct1/update", {}),
                                ("POST", "/api/hosts/ct1", {"port": 22, "confirm": True}),
                                ("DELETE", "/api/hosts/ct1", {"confirm": True}),
                                ("POST", "/api/hosts", {"name": "ct2", "ssh": "root@192.0.2.12"})):
        status, _, out = svc.handle(method, route, ok, json.dumps(data).encode() if data is not None else b"",
                                    "192.0.2.1")
        error = json.loads(out)["error"]
        assert status == 500 and "hosts.json" in error and "not valid JSON" in error, (method, route, error)
    assert path.read_text() == "{not json", "nothing wrote over it"


def test_a_second_question_while_a_host_is_asked_waits_for_it():
    """Two questions at once (the page drawn and its poll, two tabs): the second one waits for the request in flight
    (up to the host's limit) instead of naming an error for a host that answers; one host asked once."""
    asked, release = [], threading.Event()

    def ask(name):
        asked.append(name)
        release.wait(5)
        return [{"id": f"{name}-1"}]
    fan = w.FanOut(ask)
    results = {}
    first = threading.Thread(target=lambda: results.__setitem__("first", fan.gather(["a", "b"], 3)))
    first.start()
    for _ in range(100):
        if len(fan.flight) == 2:
            break
        time.sleep(0.01)
    second = threading.Thread(target=lambda: results.__setitem__("second", fan.gather(["a", "b"], 3)))
    second.start()
    time.sleep(0.3)
    assert second.is_alive(), "it waits for the answer in flight"
    release.set()
    first.join(5)
    second.join(5)
    for which in ("first", "second"):
        assert {name: info.get("result") for name, info in results[which].items()} == {
            "a": [{"id": "a-1"}], "b": [{"id": "b-1"}]}, results[which]
    assert sorted(asked) == ["a", "b"], "not asked twice"
    # forgotten (the host's entry changed): what the old request answers later is not kept
    fan.forget("a")
    assert fan.cached("a") is None and fan.cached("b")["result"] == [{"id": "b-1"}]


def test_gc_removes_only_what_its_preview_showed(logged_in):
    """The page sends what the preview listed: a dry run comes first, and when it would now remove more (a start
    that became old enough meanwhile), nothing is removed (409); fewer is fine."""
    dry = {"removed": [{"name": "old", "id": "old-1"}], "leftovers": ["gone"], "histories": ["older"]}
    logged_in.runner.answers[("gc", "--dry-run")] = (0, json.dumps(dry), "")
    expected = {"removed": ["old-1"], "leftovers": ["gone"], "histories": ["older"]}
    status, out = logged_in("POST", "/api/hosts/ct1/gc", {"older_than": 7, "confirm": True, "expected": expected})
    assert status == 200, out
    assert [args for _, args in logged_in.runner.calls] == [["gc", "--dry-run", "--json", "--older-than=7"],
                                                            ["gc", "--json", "--older-than=7"]]
    logged_in.runner.calls.clear()
    dry["removed"].append({"name": "kept", "id": "kept-2"})
    logged_in.runner.answers[("gc", "--dry-run")] = (0, json.dumps(dry), "")
    status, out = logged_in("POST", "/api/hosts/ct1/gc", {"older_than": 7, "confirm": True, "expected": expected})
    assert status == 409 and "removed kept-2" in out["error"] and "preview it again" in out["error"], out
    assert [args for _, args in logged_in.runner.calls] == [["gc", "--dry-run", "--json", "--older-than=7"]]
    logged_in.runner.calls.clear()
    dry.update(removed=[], histories=[])
    logged_in.runner.answers[("gc", "--dry-run")] = (0, json.dumps(dry), "")
    status, out = logged_in("POST", "/api/hosts/ct1/gc", {"older_than": None, "confirm": True, "expected": expected})
    assert status == 200 and logged_in.runner.calls[-1][1] == ["gc", "--json"]
    status, out = logged_in("GET", "/api/hosts/ct1/gc/1.5")
    assert status == 400 and "whole number of days" in out["error"]


def test_typing_in_a_terminal_is_use_of_the_session_its_output_is_not():
    """bridge() calls USED when the browser sends input (at most once a second), never for a resize or the output;
    the service hands it the session's touch."""
    class Socket:
        def __init__(self, frames):
            self.frames, self.closed, self.hang_up = list(frames), False, None

        def receive(self):
            if not self.frames:
                raise ConnectionError("closed")
            return self.frames.pop(0)

        def send(self, payload, opcode=2):
            pass

        def close(self, code=1000, reason=""):
            self.closed = True
    clock, used = [100.0], []
    frames = [(1, b'{"resize": [80, 24]}'), (2, b"a"), (2, b"b"), (1, b'{"resize": [90, 24]}'), (8, b"")]
    w.bridge(Socket(frames), ["/bin/cat"], alive=lambda: True, clock=lambda: clock[0], used=lambda: used.append(1))
    assert used == [1], "the first key counts, the next within the second and the resizes do not"
    clock[0], used[:] = 100.0, []
    w.bridge(Socket([(1, b'{"resize": [80, 24]}'), (8, b"")]), ["/bin/cat"], alive=lambda: True,
             clock=lambda: clock[0], used=lambda: used.append(1))
    assert used == []
    source = (ROOT / "sandbox" / "webui.py").read_text()
    assert "used=lambda: sessions.get(token, touch=True)" in source


def test_the_hosts_page_gets_each_hosts_starts_and_questions_from_what_was_asked(logged_in):
    """GET /api/hosts-activity: the counts the fan-outs keep (nothing asked for it); null before anything was;
    GET /api/starts gives each host's age by the service's clock."""
    hosts = w.module("hosts")
    hosts.save({"ct1": hosts.check("ct1", {"ssh": "root@192.0.2.10"})})
    logged_in.runner.answers[("list",)] = (0, json.dumps([{"name": "a", "state": "running"},
                                                          {"name": "b", "state": "stopped"}]), "")
    logged_in.runner.answers[("approvals", "list")] = (0, json.dumps([{"id": "1-1"}]), "")
    status, out = logged_in("GET", "/api/hosts-activity")
    assert status == 200 and out == {name: {"starts": None, "running": None, "questions": None, "age": None}
                                     for name in ("local", "ct1")} and logged_in.runner.calls == []
    status, starts = logged_in("GET", "/api/starts")
    assert set(starts["age"]) == {"local", "ct1"} and all(0 <= age < 5 for age in starts["age"].values())
    logged_in("GET", "/api/approvals")
    count = len(logged_in.runner.calls)
    status, out = logged_in("GET", "/api/hosts-activity")
    assert out["ct1"]["starts"] == 2 and out["ct1"]["running"] == 1 and out["ct1"]["questions"] == 1
    assert out["local"]["questions"] == 1 and out["ct1"]["age"] is not None and len(logged_in.runner.calls) == count


def test_the_defaults_keys_are_the_launchers():
    assert set(w.DEFAULT_KEYS) == w.module("agent_sandbox").DEFAULTS_KEYS - {"version"}


def test_the_canary_categories_are_the_hosts_own(logged_in):
    """A host whose sandbox-ai knows another category: compared with its own list (defaults show), not a fixed one."""
    logged_in.runner.answers[("defaults", "show")] = (0, json.dumps({"values": {}, "canary_categories": [
        "credentials", "home", "workspace", "browser"], "canary_default": ["credentials", "home"]}), "")
    status, out = logged_in("POST", "/api/hosts/ct1/defaults", {"key": "canaries", "value": ["credentials", "home",
                                                                                            "browser"]})
    assert status == 200, out
    status, out = logged_in("POST", "/api/hosts/ct1/defaults", {"key": "canaries", "value": ["passwords"]})
    assert status == 400 and "browser" in out["error"], out
    del logged_in.runner.answers[("defaults", "show")]
    logged_in.runner.answers[("defaults", "show")] = (0, json.dumps({"values": {}}), "")  # an older host: the CLI's own
    status, out = logged_in("POST", "/api/hosts/ct1/defaults", {"key": "canaries", "value": ["credentials"]})
    assert status == 400 and out["confirm"] == "defaults-lower" and "no home canary files" in out["error"], out


def test_the_secrets_of_a_host_are_their_names_and_stores_only(logged_in):
    logged_in.runner.answers[("secrets", "list")] = (0, "no secrets\n", "")
    assert logged_in("GET", "/api/hosts/ct1/secrets") == (200, [])
    logged_in.runner.answers[("secrets", "list")] = (0, "NAME         STORE\nGITEA_TOKEN  secret-service\n"
                                                        "MULLVAD      file\n\x1b[2J bad line here\n", "")
    assert logged_in("GET", "/api/hosts/ct1/secrets") == (200, [{"name": "GITEA_TOKEN", "store": "secret-service"},
                                                                {"name": "MULLVAD", "store": "file"}])
    # a new name is kept without a question, its value on standard input only, and the audit log never has it
    calls = []
    real = logged_in.runner

    def runner(host, args, timeout=120, input=None):
        calls.append((args, input))
        return real(host, args, timeout, input)
    logged_in.svc.api.runner = runner
    assert logged_in("POST", "/api/hosts/ct1/secrets/NEW_ONE", {"secret": "s3cr3t-value"})[0] == 200
    assert calls == [(["secrets", "list"], None), (["secrets", "set", "NEW_ONE"], "s3cr3t-value\n")]
    assert "s3cr3t-value" not in (w.state_dir() / "audit.jsonl").read_text()


def test_the_page_tells_a_copy_from_an_open_file_when_a_removal_is_refused():
    """The force-remove confirmation names the right consequence from the CLI's own refusal phrases: a copy the agent
    took stays usable until a restart (--force only takes it out of the sandbox's own view); what is open stays usable
    until it is closed. The refusal text itself is shown."""
    spec = importlib.util.spec_from_file_location("workspaces_for_web_test", ROOT / "sandbox" / "workspaces.py")
    workspaces = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(workspaces)
    page = (WEB / "app.js").read_text().split("function workspaceRefusal")[1].split("\n}")[0]
    for phrase in (workspaces.REFUSED_COPY, workspaces.REFUSED_UNKNOWN):
        assert phrase in page.split('return "copy"')[0], phrase
    for phrase in (workspaces.REFUSED_IN_USE, workspaces.REFUSED_BUSY):
        assert phrase in page.split('return "copy"')[1].split('return "open"')[0], phrase
    script = (WEB / "app.js").read_text()
    assert "The agent's copy stays usable, also for writing, until the sandbox restarts" in script
    assert 't("It was refused: {why}", { why: refusal })' in script


def test_the_current_limits_and_the_review_of_a_kept_home_come_as_the_commands_say(logged_in):
    limits = {"memory": 8 << 30, "memory_high": 7 << 30, "cpus": 4.0, "pids": None}
    logged_in.runner.answers[("limits",)] = (0, json.dumps(limits), "")
    assert logged_in("GET", "/api/hosts/ct1/limits/web") == (200, limits)
    review = {"name": "web", "id": "web", "state": "stopped", "pending": True,
              "review": {"since": "2026-11-07T10:00:00Z", "run": "r1", "complete": True,
                         "changes": [{"path": ".bashrc", "change": "changed", "kind": "file", "diff": "+curl x | sh"}]}}
    logged_in.runner.answers[("resume",)] = (0, json.dumps(review), "")
    assert logged_in("GET", "/api/hosts/local/starts/web/review") == (200, review)
    logged_in.runner.answers[("resume",)] = (2, "", "sandbox-ai: web keeps no HOME: there is nothing to review")
    status, out = logged_in("GET", "/api/hosts/local/starts/web/review")
    assert status == 502 and "keeps no HOME" in out["error"]


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
    assert '["canaries", N_("Canaries")]' in script and 'hostPath("/canaries")' in page
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

    def updater(host, version=None, sudo=None, from_here=False):
        asked.append((host, version, sudo, True) if from_here else (host, version, sudo))
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
    # from this machine's copy (hosts update --from-here): asked first like an install, never with a version
    status, out = call("POST", "/api/hosts/ct1/update", {"from_here": True})
    assert status == 400 and out.get("confirm") == "host-install" and "root@192.0.2.10" in out["error"]
    assert call("POST", "/api/hosts/ct1/update", {"from_here": True, "confirm": True, "version": "0.1.67"})[0] == 400
    assert call("POST", "/api/hosts/ct1/update", {"from_here": "yes", "confirm": True})[0] == 400
    assert call("POST", "/api/hosts/local/update", {"from_here": True, "confirm": True})[0] == 400
    assert call("POST", "/api/hosts/ct1/update", {"from_here": True, "confirm": True, "sudo": True})[0] == 200
    assert asked[-1] == ("ct1", None, True, True)
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


INSTALLS = [
    # (what the page sends, the arguments of this machine's sandbox-ai)
    ({"mode": "user"}, ["--user", "--from-here"]),
    ({"mode": "user", "source": "download"}, ["--user", "--download"]),
    ({"mode": "user", "source": "download", "version": "0.1.80"}, ["--user", "--download", "--version=0.1.80"]),
    ({"mode": "user", "sudo": False}, ["--user", "--no-sudo", "--from-here"]),
    ({"mode": "system"}, ["--system", "--from-here"]),
    ({"mode": "system", "sudo": True}, ["--system", "--sudo", "--from-here"]),
    ({"mode": "system", "sudo": False}, ["--system", "--no-sudo", "--from-here"]),
    ({"mode": "system", "sudo": None, "prefix": "/opt/sandbox-ai"}, ["--system", "--prefix=/opt/sandbox-ai", "--from-here"]),
    ({"mode": "system", "prefix": "/usr/local", "sudo": True, "source": "download", "version": "0.1.79"},
     ["--system", "--prefix=/usr/local", "--sudo", "--download", "--version=0.1.79"]),
    ({"mode": "system", "prefix": "", "version": "", "source": ""}, ["--system", "--from-here"]),
    ({}, ["--from-here"]),  # the host decides: for every user when the SSH user is root, else for that user
]


@pytest.mark.parametrize("sent, args", INSTALLS, ids=[" ".join(a) for _, a in INSTALLS])
def test_a_host_install_is_one_fixed_command_on_this_machine(home, sent, args):
    """sandbox-ai hosts install NAME, run by this machine's sandbox-ai (it copies itself, or install.sh, there over
    SSH): each choice as its option, with room for its steps (up to 15 minutes each); its answer as it came."""
    hosts = w.module("hosts")
    hosts.save({"ct1": hosts.check("ct1", {"ssh": "admin@192.0.2.10"})})
    secret = add_user()
    seen = []
    answer = {"name": "ct1", "before": None, "after": "0.1.80", "changed": True, "mode": sent.get("mode") or "user",
              "prefix": None, "sudo": False, "root": False, "source": sent.get("source") or "here", "error": None,
              "needs_password": False, "output": "", "notes": []}

    def runner(host, argv, timeout=120, input=None):
        seen.append((host, argv, timeout))
        return 0, json.dumps(answer), ""
    svc = w.Service({"localhost:8443"}, api=w.Api(runner))
    _, headers, body = login(svc, secret)
    ok = {**HEADERS, "Cookie": cookie_of(headers), "X-CSRF-Token": body["csrf"]}
    status, _, out = svc.handle("POST", "/api/hosts/ct1/install", ok, json.dumps({**sent, "confirm": True}).encode(),
                                "192.0.2.1")
    assert status == 200 and json.loads(out) == answer, out
    assert seen == [("local", ["hosts", "install", "ct1", *args, "--json"], w.install_route_timeout())]
    assert w.install_route_timeout() >= 2 * 900, "the copy and install.sh may each take 15 minutes"


def test_a_host_install_runs_once_at_a_time_and_says_how_it_ended(home):
    """A second install while one runs is refused (409); GET …/install says whether one runs and how the last one
    ended (a page whose own answer was cut off asks it); sudo that wants a password comes back as needs_password,
    and the terminal that asks for it runs hosts install there, asked first as the route is."""
    hosts = w.module("hosts")
    hosts.save({"ct1": hosts.check("ct1", {"ssh": "admin@192.0.2.10"})})
    secret = add_user()
    wanted = {"name": "ct1", "before": None, "after": None, "changed": False, "mode": "system", "prefix": "/usr/local",
              "sudo": True, "root": False, "source": "here", "needs_password": True, "output": "", "notes": [],
              "error": "sudo on ct1 wants a password: install it in a terminal (sandbox-ai hosts install ct1 --system "
                       "there), or allow sudo without one"}
    runner = FakeRunner({("hosts", "install"): (1, json.dumps(wanted), "")})
    api = w.Api(runner)
    svc = w.Service({"localhost:8443"}, api=api)
    _, headers, body = login(svc, secret)
    ok = {**HEADERS, "Cookie": cookie_of(headers), "X-CSRF-Token": body["csrf"]}

    def call(method, path, data=None):
        status, _, out = svc.handle(method, path, ok, json.dumps(data).encode() if data is not None else b"",
                                    "192.0.2.1")
        return status, json.loads(out)
    assert call("GET", "/api/hosts/ct1/install") == (200, {"running": False, "seconds": None, "last": None})
    api.installing["ct1"] = time.monotonic() - 5
    status, out = call("POST", "/api/hosts/ct1/install", {"mode": "system", "confirm": True})
    assert status == 409 and "being installed on ct1" in out["error"] and runner.calls == []
    status, out = call("GET", "/api/hosts/ct1/install")
    assert status == 200 and out["running"] and out["seconds"] >= 5
    del api.installing["ct1"]
    # unconfirmed: refused, naming why and how
    status, out = call("POST", "/api/hosts/ct1/install", {"mode": "system"})
    assert status == 400 and out["confirm"] == "host-install" and "ct1 (admin@192.0.2.10): for every user in " \
        "/usr/local, with sudo unless the SSH user is root, from this machine's copy" in out["error"] \
        and runner.calls == [], "whether the SSH user is root is the host's to say, never guessed from the entry"
    status, out = call("POST", "/api/hosts/ct1/install", {"mode": "system", "confirm": True})
    assert status == 200 and out == wanted and out["needs_password"]
    assert runner.calls == [("local", ["hosts", "install", "ct1", "--system", "--from-here", "--json"])]
    status, out = call("GET", "/api/hosts/ct1/install")
    assert status == 200 and not out["running"] and out["last"]["result"] == wanted and out["last"]["error"] is None
    assert out["last"]["choice"] == {"mode": "system", "prefix": None, "sudo": None, "source": "here", "version": None}
    # an answer that is no install's: a 502 with what it said, kept for the page that asks afterwards
    runner.answers[("hosts", "install")] = (2, "", "usage: sandbox-ai hosts [-h] {list,add,remove,test,update}")
    status, out = call("POST", "/api/hosts/ct1/install", {"mode": "user", "confirm": True})
    assert status == 502 and "usage:" in out["error"]
    assert "usage:" in call("GET", "/api/hosts/ct1/install")[1]["last"]["error"]
    # the terminal where sudo asks: hosts install without --json, in a tty; confirmed like the route
    query = {"host": "ct1", "kind": "install", "mode": "system", "prefix": "/opt/sai", "sudo": "1", "source": "here",
             "confirm": "1"}
    host, argv = w.terminal_command(query)
    assert host == "ct1" and argv[:3] == [sys.executable, "-I", str(ROOT / "sandbox" / "main.py")]
    assert argv[3:] == ["hosts", "install", "ct1", "--system", "--prefix=/opt/sai", "--sudo", "--from-here"]
    assert w.terminal_command({**query, "source": "download", "version": "0.1.80", "sudo": "0"})[1][3:] == [
        "hosts", "install", "ct1", "--system", "--prefix=/opt/sai", "--no-sudo", "--download", "--version=0.1.80"]
    for change in ({"confirm": None}, {"confirm": "yes"}, {"host": "local"}, {"host": "nowhere"}, {"sudo": "yes"},
                   {"prefix": "/opt/../etc"}, {"mode": "root"}, {"version": "0.1.80"}, {"mode": "user"}):
        with pytest.raises(w.Refused):
            w.terminal_command({k: v for k, v in {**query, **change}.items() if v is not None})


def test_an_older_doctor_answers_with_text(logged_in):
    """A host whose sandbox-ai has no doctor --json yet: its plain doctor, as text (the page shows it as it is); a
    doctor that answers nothing at all is a 502 with why."""
    logged_in.runner.answers[("doctor", "--json")] = (2, "", "sandbox-ai: error: unrecognized arguments: --json")
    logged_in.runner.answers[("doctor",)] = (1, "FAIL  Podman  not found\n      -> install podman\n", "")
    status, out = logged_in("GET", "/api/hosts/ct1/doctor")
    assert status == 200 and out == "FAIL  Podman  not found\n      -> install podman"
    assert [args for _, args in logged_in.runner.calls] == [["doctor", "--json"], ["doctor"]]
    logged_in.runner.answers[("doctor",)] = (2, "", "no doctor here")
    assert logged_in("GET", "/api/hosts/ct1/doctor")[0] == 502


def test_the_hosts_page_updates_and_shows_versions():
    script = (ROOT / "sandbox/web/app.js").read_text()
    for needed in ('"/api/hosts-status"', "/update`", '"Update all"', 'kind: "update"', "update_sudo", "keyNotices"):
        assert needed in script, needed


def test_an_mcp_server_is_attached_to_a_start(logged_in):
    status, out = logged_in("POST", "/api/hosts/ct1/mcp/demo/attach", {"start": "web", "restart": True,
                                                                        "confirm": True})
    assert status == 200 and logged_in.runner.calls[-1] == ("ct1", ["mcp", "attach", "demo", "web", "--json",
                                                                    "--restart"])
    # attaching (and restarting) is asked first: the reason names the restart when there is one
    status, out = logged_in("POST", "/api/hosts/ct1/mcp/demo/attach", {"start": "web", "restart": True})
    assert status == 400 and out["confirm"] == "mcp-restart" and "demo to web" in out["error"]
    status, out = logged_in("POST", "/api/hosts/ct1/mcp/demo/attach", {"start": "web"})
    assert status == 400 and out["confirm"] == "mcp-attach"
    assert logged_in("POST", "/api/hosts/local/mcp/demo/attach", {"start": "web", "confirm": True})[0] == 200
    assert logged_in.runner.calls[-1] == ("local", ["mcp", "attach", "demo", "web", "--json"])
    count = len(logged_in.runner.calls)
    # A name with a leading "-" would be an option of mcp attach there.
    for path, data in (("/api/hosts/ct1/mcp/de mo/attach", {"start": "web"}), ("/api/hosts/ct1/mcp/demo/attach", {}),
                       ("/api/hosts/ct1/mcp/-h/attach", {"start": "web"}),
                       ("/api/hosts/ct1/mcp/--restart/attach", {"start": "web"}),
                       ("/api/hosts/ct1/mcp/_x/attach", {"start": "web"}),
                       ("/api/hosts/ct1/mcp/demo/attach", {"start": "--all"}),
                       ("/api/hosts/ct1/mcp/demo/attach", {"start": "web", "restart": "yes", "confirm": True})):
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


def test_a_request_that_trickles_in_is_cut_off_and_one_address_cannot_take_every_place(server, monkeypatch):
    """Review 5 L10 (PoC poc_slowloris.py): the timeout was per read, so clients sending a header byte every few
    seconds held all 64 places for minutes. The headers must now arrive within HEAD_DEADLINE of the request line,
    and an address other than this machine's holds at most MAX_PER_ADDRESS places."""
    monkeypatch.setattr(w, "HEAD_DEADLINE", 1.0)
    context = ssl.create_default_context()
    context.check_hostname, context.verify_mode = False, ssl.CERT_NONE
    slow = context.wrap_socket(socket.create_connection(("127.0.0.1", server["port"]), timeout=30),
                               server_hostname="localhost")
    started = time.monotonic()
    slow.sendall(b"GET / HTTP/1.1\r\n")
    closed = False
    for _ in range(20):
        time.sleep(0.4)
        try:
            slow.sendall(b"X: y\r\n")
            slow.settimeout(0.05)
            if slow.recv(1024) == b"":
                closed = True
                break
        except (ssl.SSLError, socket.timeout):
            continue
        except OSError:
            closed = True
            break
    slow.close()
    assert closed and time.monotonic() - started < 5
    # the count per address: a remote one is capped, this machine's own is not
    srv = w.Server.__new__(w.Server)
    srv.limit, srv.per_address, srv.active, srv.active_lock, srv.by_address = 64, 2, 0, threading.Lock(), {}
    refused = []
    monkeypatch.setattr(w.http.server.ThreadingHTTPServer, "process_request", lambda self, request, address: None)
    monkeypatch.setattr(w.Server, "shutdown_request", lambda self, request: refused.append(request))
    for index in range(3):
        srv.process_request(f"r{index}", ("192.0.2.9", 1000 + index))
    for index in range(3):
        srv.process_request(f"l{index}", ("127.0.0.1", 2000 + index))
    assert refused == ["r2"] and srv.by_address == {"192.0.2.9": 2, "127.0.0.1": 3}
    srv.finished("192.0.2.9")
    srv.process_request("r3", ("192.0.2.9", 1003))
    assert refused == ["r2"]


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


# A small stand-in for the DOM, enough for app.js's pages (no innerHTML: it has none, so a use would fail).
FAKE_DOM = r"""
const fs = require("fs"), vm = require("vm");
class Node {
  constructor(tag) { this.tagName = tag; this.children = []; this.attributes = {}; this.listeners = {}; this.parent = null;
    this.className = ""; this.text = ""; this.hidden = false; this.disabled = false; this.dataset = {};
    const node = this; this.classList = { toggle(c, on) { const set = new Set(node.className.split(" ").filter(Boolean));
      if (on === undefined ? !set.has(c) : on) set.add(c); else set.delete(c); node.className = [...set].join(" "); },
      add(c) { this.toggle(c, true); }, remove(c) { this.toggle(c, false); } }; }
  setAttribute(k, v) { this.attributes[k] = String(v); if (k === "id") this.id = String(v); }
  getAttribute(k) { return this.attributes[k]; }
  removeAttribute(k) { delete this.attributes[k]; }
  addEventListener(type, f) { (this.listeners[type] = this.listeners[type] || []).push(f); }
  fire(type, extra) { for (const f of this.listeners[type] || []) f(Object.assign({ target: this, preventDefault() {} }, extra || {})); }
  append(...nodes) { for (let n of nodes) { if (typeof n === "string") { const t = new Node("#text"); t.text = n; n = t; }
    if (n.parent) n.remove(); n.parent = this; this.children.push(n); } }
  prepend(n) { n.parent = this; this.children.unshift(n); }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(c => c !== this); this.parent = null; }
  removeChild(n) { n.remove(); return n; }
  replaceWith(n) { const i = this.parent.children.indexOf(this); if (n.parent) n.remove(); n.parent = this.parent; this.parent.children[i] = n; this.parent = null; }
  focus() { globalThis.focused = this; }
  get parentNode() { return this.parent; }
  get firstChild() { return this.children[0] || null; }
  get lastElementChild() { return this.children[this.children.length - 1]; }
  get childNodes() { return this.children; }
  get isConnected() { let n = this; while (n.parent) n = n.parent; return n === body; }
  get textContent() { return this.tagName === "#text" ? this.text : this.children.map(c => c.textContent).join(""); }
  set textContent(v) { this.children = []; if (String(v) !== "") this.append(String(v)); }
  all() { const out = []; const walk = n => { out.push(n); n.children.forEach(walk); }; walk(this); return out; }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  // ".class", or tag names ("button, input")
  querySelectorAll(sel) {
    if (sel.startsWith(".")) return this.all().filter(n => n !== this && (n.className || "").split(" ").includes(sel.slice(1)));
    const tags = sel.split(",").map(x => x.trim());
    return this.all().filter(n => n !== this && tags.includes(n.tagName));
  }
}
const body = new Node("body");
const app = new Node("div"); app.setAttribute("id", "app"); body.append(app);
const live = new Node("div"); live.setAttribute("id", "live"); body.append(live);
const nav = new Node("button");
const document = {
  body, title: "", createElement: t => new Node(t),
  createTextNode: t => { const n = new Node("#text"); n.text = t; return n; },
  getElementById: id => body.all().find(n => n.id === id) || null,
  querySelector: sel => (sel === 'nav button[data-page="approvals"]' ? nav : null), querySelectorAll: () => [],
  documentElement: { style: { setProperty() {} } },
};
const calls = [], opened = [];
let answer = null, sessionLeft = null;
// ANSWER's result: data, an Error (502), { __status: N } (that status), or a Promise of one of them (a slow host)
const fetch = async (path, opts) => {
  calls.push({ method: opts.method, path, headers: opts.headers, body: opts.body });
  const data = await answer(opts.method, path, opts.body);
  const headers = { get: (name) => (name === "X-Session-Idle-Left" && sessionLeft !== null ? String(sessionLeft) : name === "X-Session-Lifetime-Left" && sessionLeft !== null ? "40000" : null) };
  if (data instanceof Error) return { status: 502, ok: false, statusText: "Bad Gateway", headers, json: async () => ({ error: data.message }) };
  if (data && data.__status) return { status: data.__status, ok: false, statusText: "", headers, json: async () => Object.assign({ error: data.__error || "log in" }, data.__confirm ? { confirm: data.__confirm } : {}) };
  return { status: 200, ok: true, headers, json: async () => data };
};
const context = vm.createContext({ document, Node, setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0,
  clearInterval() {}, console, window: { open: (...args) => { opened.push(args); return null; } }, location: { host: "x", hash: "" }, localStorage: { getItem: () => null, setItem() {} }, fetch });
vm.runInContext("let __now = 1000000; Date.now = () => __now;", context);
// the page's texts first, as index.html loads them (i18n.js before app.js)
vm.runInContext(fs.readFileSync(require("path").join(require("path").dirname(process.argv[2]), "i18n.js"), "utf8"), context);
vm.runInContext(fs.readFileSync(process.argv[2], "utf8").replace(/loadThemes\(\)\.then\(render\);\s*$/, ""), context);
const settle = async () => { for (let i = 0; i < 20; i++) await new Promise(r => setImmediate(r)); };
const button = (row, text) => row.all().find(n => n.tagName === "button" && n.textContent === text);
const toasts = () => (document.getElementById("toasts") || new Node("x")).children.map(t => t.textContent);
const main = new Node("main"); main.setAttribute("id", "main"); body.append(main);
context.__main = main;
"""

APPROVALS_DRIVER = FAKE_DOM + r"""
const rows = () => body.all().filter(n => n.attributes["data-id"]);
const key = (r) => `${r.attributes["data-host"]} ${r.attributes["data-id"]}`;
const row = (k) => rows().find(r => key(r) === k);
const cell = (r, cls) => r.all().find(n => (n.className || "").split(" ").includes(cls));
(async () => {
  const result = {};
  vm.runInContext('state.me = { user: "admin", csrf: "c" }; state.page = "approvals"; state.host = "local";', context);
  const q = (host, id, question, extra) => Object.assign({ host, id, run: "claude-web", sandbox: "claude-web", kind: "connect", question,
    always: "Allow always: pypi.org", seconds_left: 45, max_scope: "always" }, extra || {});
  let open = [q("local", "1-1", "connect to pypi.org:443"), q("local", "1-2", "look up example.org"),
    q("ct1", "1-1", "open the login page", { kind: "browser", url: "https://login.example.org/device?code=1", seconds_left: 15, max_scope: "run" }),
    q("ct1", "1-4", "open this", { kind: "browser", url: "javascript:alert(1)", seconds_left: 8, max_scope: "once" })];
  let errors = {};
  answer = (method, path, sent) => {  // an answered question is no longer open
    if (method === "GET") return open instanceof Error ? open : { questions: open, errors };
    open = open.filter(x => path !== `/api/hosts/${x.host}/approvals/${x.id}`);
    return { output: `sandbox-ai approvals: ${sent}` };
  };
  await vm.runInContext("pages.approvals(__main)", context);
  result.first = rows().map(key);
  result.title = document.title;
  result.countdown = rows().map(r => [cell(r, "countdown").textContent, cell(r, "countdown").className]);
  const browser = row("ct1 1-1"), bad = row("ct1 1-4");
  const link = browser.all().find(n => n.tagName === "a");
  result.link = link && [link.getAttribute("href"), link.getAttribute("target"), link.getAttribute("rel"), link.textContent];
  result.badLink = bad.all().some(n => n.tagName === "a") || Boolean(button(bad, "Open here and allow")) || Boolean(button(bad, "Allow"));
  result.badText = bad.textContent;
  const options = (r) => r.all().filter(n => n.tagName === "option").map(o => [o.value, o.textContent, "disabled" in o.attributes]);
  result.options = [options(row("local 1-1")), options(browser), options(bad)];
  result.block = ["local 1-1", "ct1 1-1", "ct1 1-4"].map(k => Boolean(button(row(k), "Block always…")));
  // one second at a time, here: no request for it
  const before = calls.length;
  vm.runInContext("__now += 10000; tickApprovals();", context);
  result.later = rows().map(r => cell(r, "countdown").textContent);
  result.expired = [cell(bad, "countdown").className, bad.textContent.includes("refused: no answer in time"), Boolean(button(bad, "Allow"))];
  result.tickRequests = calls.length - before;
  // kept between polls, with the scope chosen
  const kept = row("local 1-1"), scope = kept.all().find(n => n.tagName === "select");
  scope.value = "always";
  open = open.filter(x => x.id !== "1-2").concat([q("local", "1-3", "connect to 192.0.2.9:5432")]);
  await vm.runInContext("poll()", context); await settle();
  result.polled = rows().map(key);
  result.sameRow = row("local 1-1") === kept; result.sameScope = kept.all().includes(scope) && scope.value === "always";
  result.background = calls.filter(c => c.method === "GET").map(c => [c.path, c.headers["X-Background"] || null]);
  // Allow asks in the row first, naming the host
  button(kept, "Allow").fire("click");
  result.asked = kept.all().find(n => n.className === "confirm").textContent;
  result.postedBeforeYes = calls.filter(c => c.method === "POST").length;
  await vm.runInContext("poll()", context); await settle();
  result.stillAsking = row("local 1-1") === kept && Boolean(button(kept, "Yes, allow"));
  button(kept, "Yes, allow").fire("click"); await settle();
  // the browser question of ct1: opened here, allowed there without opening a browser (no plain Allow for it)
  result.browserButtons = browser.all().filter(n => n.tagName === "button").map(b => b.textContent);
  browser.all().find(n => n.tagName === "select").value = "run";
  button(browser, "Open here and allow").fire("click");
  result.askedNoOpen = browser.all().find(n => n.className === "confirm").textContent;
  result.openedBeforeYes = opened.length;
  button(browser, "Yes, open and allow").fire("click"); await settle();
  result.opened = opened;
  // Deny: for the run; Block always: asked first
  const other = row("local 1-3");
  button(other, "Block always…").fire("click");
  result.askedBlock = other.all().find(n => n.className === "confirm").textContent;
  result.postsBeforeBlock = calls.filter(c => c.method === "POST").length;
  button(other, "Yes, block always").fire("click"); await settle();
  open.push(q("ct1", "2-1", "look up x.org", { max_scope: "always" }));
  await vm.runInContext("poll()", context); await settle();
  result.denyLabels = [button(row("ct1 2-1"), "Deny for this run") ? "run" : null];
  const scope21 = row("ct1 2-1").all().find(n => n.tagName === "select");
  scope21.value = "once"; scope21.fire("change");
  result.denyLabels.push(button(row("ct1 2-1"), "Deny once") ? "once" : null);
  scope21.value = "run"; scope21.fire("change");
  button(row("ct1 2-1"), "Deny for this run").fire("click"); await settle();
  result.posts = calls.filter(c => c.method === "POST").map(c => [c.path, JSON.parse(c.body)]);
  result.toast = toasts().filter(t => t.includes("sandbox-ai approvals:"));
  // a host that cannot be asked keeps its rows and is named; the others are shown
  open = [q("ct1", "1-4", "open this", { seconds_left: 30 }), q("local", "5-1", "connect to a.org:443")];
  errors = {};
  await vm.runInContext("poll()", context); await settle();
  result.revived = [Boolean(button(row("ct1 1-4"), "Deny once")), cell(row("ct1 1-4"), "countdown").textContent];
  open = [q("local", "5-1", "connect to a.org:443")];
  errors = { ct1: "ssh: connect to host ct1 port 22: No route to host" };
  await vm.runInContext("poll()", context); await settle();
  result.partly = [rows().map(key), main.all().filter(n => (n.className || "").includes("notice")).map(n => n.textContent), document.title];
  // the whole check failed: "?", never 0, and the rows stay
  open = new Error("the service could not be reached");
  await vm.runInContext("poll()", context); await settle();
  const badge = nav.children.find(n => (n.className || "").includes("badge"));
  result.badge = badge ? [badge.textContent, badge.getAttribute("aria-label")] : null;
  result.count = vm.runInContext("state.approvals", context);
  result.notice = main.all().filter(n => (n.className || "").includes("notice")).map(n => n.textContent);
  result.rowsAfterError = rows().map(key);
  // elsewhere in the page: a new question is a toast that stays until it is answered or gone
  vm.runInContext('state.page = "logs";', context);
  open = [q("local", "5-1", "connect to a.org:443"), q("ct1", "6-1", "connect to b.org:443")];
  errors = {};
  await vm.runInContext("poll()", context); await settle();
  result.newToasts = toasts().filter(t => t.includes("sandbox-ai asks"));
  open = [q("local", "5-1", "connect to a.org:443")];
  await vm.runInContext("poll()", context); await settle();
  result.toastsAfter = toasts().filter(t => t.includes("sandbox-ai asks"));
  result.close = [vm.runInContext('closeText({ code: 1008, reason: "refused: at most 8 terminals at once\u001b[2J" }, true)', context),
    vm.runInContext('closeText({ code: 1006, reason: "" }, false)', context)];
  console.log(JSON.stringify(result));
})().catch(e => { console.error(e.stack); process.exit(1); });
"""


def run_driver(tmp_path, source, name):
    driver = tmp_path / f"{name}.js"
    driver.write_text(source)
    run = subprocess.run(["node", str(driver), str(WEB / "app.js")], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_the_approvals_table_is_kept_between_polls(tmp_path):
    """app.js in node with a small stand-in for the DOM: the questions of every host in one table, keyed by host and
    id (a poll adds and removes rows, the others stay with the scope chosen in them), each counting down its time
    left (amber below 20 s, red below 10 s) without a request; Allow asks in the row first, naming the host, the
    sandbox, what it asks and the scope, and the answer goes to the question's own host; a browser question's link
    (http(s) only) and "Opened it — allow" (no_open); Deny is for the run, Block always is asked first; scopes wider
    than the question allows are disabled; the tab's title has the count; a host that cannot be asked keeps its rows
    and is named; a failed check shows "?", never 0; a new question elsewhere is a toast until it is gone."""
    result = run_driver(tmp_path, APPROVALS_DRIVER, "approvals")
    assert result["first"] == ["local 1-1", "local 1-2", "ct1 1-1", "ct1 1-4"] and result["title"] == "(4) sandbox-ai"
    assert result["countdown"] == [["45 s", "countdown"], ["45 s", "countdown"], ["15 s", "countdown warn"],
                                   ["8 s", "countdown bad"]]
    assert result["link"] == ["https://login.example.org/device?code=1", "_blank", "noopener noreferrer",
                              "https://login.example.org/device?code=1"]
    assert result["badLink"] is False, "a javascript: URL is no link, and offers no Allow of any kind"
    assert "not opened anywhere" in result["badText"]
    labels = [["once", "this once"], ["run", "for the rest of this run"], ["always", "always (remembered for the project)"]]
    assert result["options"] == [[[*l, False] for l in labels], [[*labels[0], False], [*labels[1], False], [*labels[2], True]],
                                 []], "scopes wider than the question allows are off; one that cannot be allowed has none"
    assert result["block"] == [True, False, False], "Block always only where an always is possible"
    assert result["later"] == ["35 s", "35 s", "5 s", "expired"] and result["tickRequests"] == 0
    assert result["expired"] == ["countdown bad", True, False]
    assert result["polled"] == ["local 1-1", "ct1 1-1", "ct1 1-4", "local 1-3"]
    assert result["sameRow"] and result["sameScope"], "an unchanged row stays, with its scope"
    assert result["background"] == [["/api/approvals", None], ["/api/approvals", "1"]], "the page's own load counts"
    assert result["postedBeforeYes"] == 0 and result["stillAsking"]
    for part in ("claude-web", "on local", "connect to pypi.org:443", "always (remembered for the project)",
                 "Allow always: pypi.org"):
        assert part in result["asked"], (part, result["asked"])
    assert "on ct1" in result["askedNoOpen"] and "nothing opens on ct1" in result["askedNoOpen"]
    assert result["browserButtons"] == ["Open here and allow", "Deny for this run"], "never a plain Allow (it opens there)"
    assert result["openedBeforeYes"] == 0
    assert result["opened"] == [["https://login.example.org/device?code=1", "_blank", "noopener,noreferrer"]]
    assert result["denyLabels"] == ["run", "once"], "the Deny button says what it does"
    assert "remembered for the project" in result["askedBlock"] and "on local" in result["askedBlock"]
    assert result["postsBeforeBlock"] == 2
    assert result["posts"] == [
        ["/api/hosts/local/approvals/1-1", {"decision": "allow", "scope": "always", "confirm": True}],
        ["/api/hosts/ct1/approvals/1-1", {"decision": "allow", "scope": "run", "no_open": True}],
        ["/api/hosts/local/approvals/1-3", {"decision": "deny", "scope": "always", "confirm": True}],
        ["/api/hosts/ct1/approvals/2-1", {"decision": "deny", "scope": "run"}]]
    assert any('"scope":"always"' in t and "Blocked always: claude-web on local" in t for t in result["toast"]), \
        "the command's output is shown"
    assert result["revived"] == [True, "30 s"], "a question the host still has open is answerable again"
    rows, notices, title = result["partly"]
    assert rows == ["ct1 1-4", "local 5-1"], "the rows of a host that could not be asked stay"
    assert any("Could not check ct1" in n and "No route to host" in n for n in notices) and title == "(1) sandbox-ai"
    assert result["badge"] == ["?", "could not check"] and result["count"] is None
    assert any("Could not check" in text and "could not be reached" in text for text in result["notice"])
    assert result["rowsAfterError"] == ["ct1 1-4", "local 5-1"], "a failed check takes no row away"
    assert len(result["newToasts"]) == 1 and "on ct1" in result["newToasts"][0]
    assert result["toastsAfter"] == [], "the toast of a question goes with it"
    assert "code 1008" in result["close"][0] and "at most 8 terminals" in result["close"][0]
    assert "\u001b" not in result["close"][0] and "could not connect" in result["close"][1]


QUESTIONS_DRIVER = FAKE_DOM + r"""
const rows = () => body.all().filter(n => n.attributes["data-id"]);
const key = (r) => `${r.attributes["data-host"]} ${r.attributes["data-id"]}`;
const row = (k) => rows().find(r => key(r) === k);
const cell = (r, cls) => r.all().find(n => (n.className || "").split(" ").includes(cls));
const asks = () => toasts().filter(t => t.includes("sandbox-ai asks"));
(async () => {
  const result = {};
  vm.runInContext('state.me = { user: "admin", csrf: "c" }; state.page = "logs"; state.host = "local";', context);
  const q = (host, id, question, extra) => Object.assign({ host, id, run: "r1", sandbox: "web", kind: "connect", question,
    seconds_left: 45, max_scope: "always" }, extra || {});
  let open = [q("local", "1-1", "connect to a.org:443"), q("ct1", "2-1", "connect to b.org:443")], errors = {}, age = {};
  let held = null;  // a poll fetched before an answer, arriving after it
  answer = (method, path, sent) => {
    if (method === "GET" && path === "/api/approvals") return held ? held : { questions: open, errors, age };
    if (method === "GET") return {};
    open = open.filter(x => path !== `/api/hosts/${x.host}/approvals/${x.id}`);
    return { output: "done" };
  };
  // W1: two questions in one poll elsewhere: one toast for both; one answered or gone leaves it for the other
  await vm.runInContext("poll()", context); await settle();
  result.together = asks();
  open = open.filter(x => x.id !== "2-1");
  await vm.runInContext("poll()", context); await settle();
  result.oneLeft = asks();
  open = [];
  await vm.runInContext("poll()", context); await settle();
  result.noneLeft = asks();
  // a closed toast stays closed for its questions
  open = [q("local", "3-1", "connect to c.org:443")];
  await vm.runInContext("poll()", context); await settle();
  document.getElementById("toasts").children[0].all().find(n => n.tagName === "button" && n.getAttribute("aria-label") === "Close").fire("click");
  await vm.runInContext("poll()", context); await settle();
  result.closedStays = asks();
  // the approvals page: countdowns from when each host's list was read; no deadline; what cannot be allowed
  vm.runInContext('state.page = "approvals";', context);
  open = [q("local", "4-1", "connect to d.org:443", { seconds_left: 30 }), q("ct1", "4-2", "connect to e.org:443", { seconds_left: 30 }),
    q("ct1", "4-3", "open the page", { kind: "browser", url: "https://bank.example@evil.example/x", url_host: "evil.example", seconds_left: null, timeout: null, max_scope: "once" }),
    q("ct1", "4-4", "look up f.org", { kind: "dns", max_scope: null, allow_refused: "a lookup is allowed for the run or not at all" }),
    q("ct1", "4-5", "open the docs", { kind: "browser", url: "https://docs.example.org/a@b", url_host: "docs.example.org", url_truncated: true, seconds_left: null, max_scope: "run" })];
  age = { local: 0.5, ct1: 12 };
  await vm.runInContext("pages.approvals(__main)", context); await settle();
  result.countdowns = rows().map(r => [key(r), cell(r, "countdown").textContent]);
  const userinfo = row("ct1 4-3"), refused = row("ct1 4-4"), docs = row("ct1 4-5");
  result.userinfo = [userinfo.all().some(n => n.tagName === "a"), userinfo.all().filter(n => n.tagName === "button").map(b => b.textContent), userinfo.textContent.includes("opens evil.example")];
  result.refused = [refused.all().filter(n => n.tagName === "button").map(b => b.textContent), refused.all().some(n => n.tagName === "select"), refused.textContent.includes("cannot be allowed: a lookup is allowed for the run or not at all")];
  const link = docs.all().find(n => n.tagName === "a");
  result.docs = [link && link.getAttribute("href"), docs.textContent.includes("the URL was cut"), docs.textContent.includes("opens docs.example.org")];
  // spoken at 20 and 10 seconds, once each
  const spoken = [];
  for (let i = 0; i < 25; i++) { vm.runInContext("__now += 1000; tickApprovals();", context); if (live.textContent && !spoken.includes(live.textContent)) spoken.push(live.textContent); }
  result.spoken = spoken;
  // keys in a focused row: d denies (for the run), a asks to allow
  const local = row("local 4-1");
  result.focusable = local.getAttribute("tabindex");
  local.fire("keydown", { key: "a" });
  result.keyAsk = Boolean(local.all().find(n => n.className === "confirm"));
  button(local, "Cancel").fire("click");
  result.cancelFocus = globalThis.focused && globalThis.focused.textContent;
  // W10: the answer is given; a poll fetched before it arrives after: its row does not come back
  const before = { questions: open.slice(), errors: {}, age: {} };
  local.fire("keydown", { key: "d" }); await settle();
  result.denied = calls.filter(c => c.method === "POST").map(c => [c.path, JSON.parse(c.body)]);
  held = before;
  await vm.runInContext("poll()", context); await settle();
  result.afterStalePoll = [rows().map(key).includes("local 4-1"), vm.runInContext("state.approvals", context)];
  held = null;
  await vm.runInContext("poll()", context); await settle();
  result.answeredKept = vm.runInContext('state.answered.has("local 4-1")', context);  // that list is older than the answer
  age = {};
  await vm.runInContext("poll()", context); await settle();
  result.answeredForgotten = !vm.runInContext('state.answered.has("local 4-1")', context);
  // W5: the session's end, two minutes before; Stay logged in is a request of the user's
  sessionLeft = 100;
  await vm.runInContext('api("GET", "/api/hosts/local/doctor", undefined, { background: true })', context);
  const banner = document.getElementById("session-banner");
  result.banner = banner && banner.textContent;
  calls.length = 0;
  sessionLeft = 1800;
  button(banner, "Stay logged in").fire("click"); await settle();
  result.stay = [calls.map(c => [c.method, c.path, c.headers["X-Background"] || null]), Boolean(document.getElementById("session-banner"))];
  // a 401: the login says the session ended, and what was open where
  vm.runInContext('state.questions = [{ host: "local", id: "9-1" }, { host: "ct1", id: "9-2" }, { host: "ct1", id: "9-3" }];', context);
  answer = () => ({ __status: 401 });
  await vm.runInContext('api("GET", "/api/hosts/local/doctor").catch(() => {})', context); await settle();
  result.login = [vm.runInContext("state.me", context), app.textContent];
  console.log(JSON.stringify(result));
})().catch(e => { console.error(e.stack); process.exit(1); });
"""


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_questions_toasts_countdowns_keys_and_the_sessions_end(tmp_path):
    """W1: one toast for the questions that came, drawn anew from what is still open (an answer to one leaves it for
    the other), closed for good by the user; countdowns from when each host's list was read (age), none without a
    deadline; a URL with a user part is no link and gets no Allow; max_scope null: no Allow, no scope, why; spoken at
    20 and 10 s; a and d in a focused row; W10: an answered question never comes back from an older poll; W5: the
    banner before the session ends, Stay logged in, and the login that says what was open."""
    result = run_driver(tmp_path, QUESTIONS_DRIVER, "questions")
    assert len(result["together"]) == 1 and "and 1 more" in result["together"][0]
    assert len(result["oneLeft"]) == 1 and "on local" in result["oneLeft"][0] and "more" not in result["oneLeft"][0]
    assert result["noneLeft"] == [] and result["closedStays"] == []
    assert result["countdowns"] == [["local 4-1", "30 s"], ["ct1 4-2", "18 s"], ["ct1 4-3", "no time limit"],
                                    ["ct1 4-4", "33 s"], ["ct1 4-5", "no time limit"]], "a slow host's age is taken off"
    assert result["userinfo"] == [False, ["Deny once"], True], "a user part makes no link, the real host is named"
    assert result["refused"][0] == ["Deny for this run"] and result["refused"][1] is False and result["refused"][2]
    assert result["docs"] == ["https://docs.example.org/a@b", True, True], "an @ in the path is fine"
    assert any("20 seconds left: connect to d.org:443 (web on local)" in s for s in result["spoken"])
    assert any("10 seconds left" in s for s in result["spoken"]) and len([s for s in result["spoken"] if "seconds left" in s]) <= 8
    assert result["focusable"] == "0" and result["keyAsk"] and result["cancelFocus"]
    assert result["denied"] == [["/api/hosts/local/approvals/4-1", {"decision": "deny", "scope": "run"}]]
    assert result["afterStalePoll"] == [False, 4], "an older poll does not bring the answered question back"
    assert result["answeredKept"] is True and result["answeredForgotten"] is True, "forgotten once a newer list lacks it"
    assert result["banner"] and "Session ends in 1:40" in result["banner"] and "Stay logged in" in result["banner"]
    assert result["stay"] == [[["GET", "/api/me", None]], False], "a real request: the session is used, the banner goes"
    me, login = result["login"]
    assert me is None and "The session ended" in login
    assert "When it ended, 3 questions were open: 1 on local, 2 on ct1." in login


PAGE_DRIVER = FAKE_DOM + r"""
(async () => {
  const result = {};
  // the free-form options of a new start, split as a shell does
  const split = (text) => { try { return vm.runInContext(`shellWords(${JSON.stringify(text)})`, context); } catch (e) { return `error: ${e.message}`; } };
  result.words = [split("--mode high\n-m sonnet\n--allow-pypi"), split("--preset-value repo='my repo' -m \"opus 4\""),
    split("a\\ b c\\\nd 'it''s' \"x\\\"y\""), split("--name 'open"), split("  \n ")];
  result.sizes = vm.runInContext("[exactSize(8 * 2 ** 30), exactSize(512 * 2 ** 20), exactSize(1000), readableSize(7.5 * 2 ** 30)]", context);
  // one rule for confirmations: asked in place, Cancel puts back what was there, nothing sent before Yes
  answer = (method, path) => (method === "GET" ? [] : { output: "done" });
  const cellNode = new Node("td"); body.append(cellNode);
  const original = new Node("button"); original.append("Delete"); cellNode.append(original);
  context.__cell = cellNode;
  vm.runInContext('confirmIn(__cell, "Delete web on ct1?", "Yes, delete it", () => api("POST", "/api/x", { confirm: true }))', context);
  result.asking = [cellNode.textContent, globalThis.focused && globalThis.focused.textContent];
  cellNode.all().find(n => n.tagName === "button" && n.textContent === "Cancel").fire("click");
  result.cancelled = [cellNode.children[0] === original, calls.length];
  vm.runInContext('confirmIn(__cell, "Delete web on ct1?", "Yes, delete it", () => api("POST", "/api/x", { confirm: true }))', context);
  cellNode.all().find(n => n.tagName === "button" && n.textContent === "Yes, delete it").fire("click"); await settle();
  result.yes = [calls.map(c => [c.method, c.path, c.body]), cellNode.children[0] === original];
  // the starts page: rows by name; an unchanged one stays (with a Stop being asked), MCP rows have no Pause,
  // Network or Resources, Stop names the host and the HOME
  vm.runInContext('state.me = { user: "admin", csrf: "c" }; state.page = "starts"; state.host = "ct1";', context);
  let starts = [{ name: "web", agent: "claude", kind: "session", state: "running", keep: true, home: "kept", directory: "/d", attachable: true },
    { name: "mcp-files", kind: "mcp", state: "running", serves: [] },
    { name: "old", agent: "codex", kind: "session", state: "stopped", keep: true, home: "kept", directory: "/e" }];
  answer = (method, path) => (path.endsWith("/starts") ? starts : path.endsWith("/presets") ? [] : { output: "ok" });
  await vm.runInContext("pages.starts(__main)", context);
  const rowOf = (name) => body.all().find(n => n.attributes["data-name"] === name);
  const buttons = (r) => r.all().filter(n => n.tagName === "button").map(b => b.textContent);
  result.buttons = ["web", "mcp-files", "old"].map(n => buttons(rowOf(n)));
  const web = rowOf("web");
  web.all().find(n => n.tagName === "button" && n.textContent === "Stop").fire("click");
  result.stopAsk = web.all().find(n => n.className === "confirm").textContent;
  calls.length = 0;
  starts = starts.map(s => (s.name === "old" ? Object.assign({}, s, { state: "running" }) : s)).concat([{ name: "new", agent: "opencode", kind: "session", state: "running", keep: false, home: "discarded", directory: "/f" }]);
  await vm.runInContext("refreshStarts(__main)", context); await settle();
  result.refreshed = [rowOf("web") === web, Boolean(web.all().find(n => n.textContent === "Yes, stop it")), body.all().filter(n => n.attributes["data-name"]).map(n => n.attributes["data-name"]),
    calls.map(c => [c.path, c.headers["X-Background"] || null])];
  rowOf("new").all().find(n => n.tagName === "button" && n.textContent === "Stop").fire("click");
  result.stopAskDiscarded = rowOf("new").all().find(n => n.className === "confirm").textContent;
  web.all().find(n => n.textContent === "Yes, stop it").fire("click"); await settle();
  result.stopPost = calls.filter(c => c.method === "POST").map(c => [c.path, JSON.parse(c.body)]);
  console.log(JSON.stringify(result));
})().catch(e => { console.error(e.stack); process.exit(1); });
"""


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_the_page_splits_options_confirms_in_place_and_keeps_the_starts_table(tmp_path):
    result = run_driver(tmp_path, PAGE_DRIVER, "page")
    assert result["words"] == [["--mode", "high", "-m", "sonnet", "--allow-pypi"],
                               ["--preset-value", "repo=my repo", "-m", "opus 4"],
                               ["a b", "cd", "its", 'x"y'], "error: a single quote is not closed", []]
    assert result["sizes"] == ["8g", "512m", "1000", "7.5g"]
    assert result["asking"] == ["Delete web on ct1?Yes, delete itCancel", "Cancel"], "Cancel has the focus"
    assert result["cancelled"] == [True, 0], "Cancel puts back what was there, nothing is sent"
    assert result["yes"] == [[["POST", "/api/x", '{"confirm":true}']], True]
    assert result["buttons"] == [["Attach", "Pause", "Stop", "Network…", "Resources…", "Rename…", "More…", "History"],
                                 ["Stop", "Attach to…", "History"], ["Resume", "Delete", "Rename…", "More…", "History"]], \
        "an MCP server has no Pause, Network, Resources or Rename"
    assert "Stop web on ct1?" in result["stopAsk"] and "its HOME is kept" in result["stopAsk"]
    same, asking, names, polled = result["refreshed"]
    assert same and asking, "an unchanged row stays, its Stop still asked"
    assert names == ["web", "mcp-files", "old", "new"] and polled == [["/api/hosts/ct1/starts", "1"]]
    assert "Stop new on ct1?" in result["stopAskDiscarded"] and "not kept" in result["stopAskDiscarded"]
    assert result["stopPost"] == [["/api/hosts/ct1/starts/web/stop", {"confirm": True}]]


STARTS_DRIVER = FAKE_DOM + r"""
const rowOf = (name) => body.all().find(n => n.attributes["data-name"] === name);
const buttonsOf = (r) => r.all().filter(n => n.tagName === "button");
(async () => {
  const result = {};
  vm.runInContext('state.me = { user: "admin", csrf: "c", terminals: true }; state.page = "settings"; state.host = "ct1"; state.hosts = [{ name: "local", kind: "local" }, { name: "ct1", kind: "ssh", ssh: "root@ct1" }];', context);
  // W2/W3: the defaults page: one prompt at a time, compared with what the host has now
  let defaults = { values: { mode: "high" }, canary_categories: ["credentials", "home", "workspace"], canary_default: ["credentials", "home"] };
  answer = (method, path, sent) => (method === "GET" && path.endsWith("/defaults") ? defaults : { output: "ok" });
  await vm.runInContext("pages.settings(__main)", context); await settle();
  const mode = document.getElementById("mode"), modeAsk = mode.parent.children[mode.parent.children.indexOf(mode) + 2];
  mode.value = "lax"; mode.fire("change");
  result.first = modeAsk.textContent;
  mode.value = "low"; mode.fire("change");
  result.second = [modeAsk.all().filter(n => n.className === "confirm").length, modeAsk.textContent];
  button(modeAsk, "Cancel").fire("click");
  result.cancelled = [modeAsk.textContent, mode.value, calls.filter(c => c.method === "POST").length, globalThis.focused === mode];
  mode.value = "paranoid"; mode.fire("change"); await settle();
  result.stricter = [modeAsk.textContent, calls.filter(c => c.method === "POST").map(c => JSON.parse(c.body))];
  mode.value = "high"; mode.fire("change"); await settle();
  result.stepDown = modeAsk.textContent.includes("Make high instead of paranoid");
  button(modeAsk, "Yes, lower it").fire("click"); await settle();
  result.lowered = calls.filter(c => c.method === "POST").map(c => JSON.parse(c.body)).pop();
  const telemetry = document.getElementById("telemetry");
  telemetry.value = "strict"; telemetry.fire("change"); await settle();
  telemetry.value = "off"; telemetry.fire("change"); await settle();
  result.telemetryDown = telemetry.parent.textContent.includes("instead of strict");

  // the starts of ct1; a listing that fails leaves the rows as last seen, greyed, their buttons off
  calls.length = 0;
  vm.runInContext('state.page = "starts"; state.startsHost = "ct1";', context);
  let starts = [{ name: "web", agent: "claude", kind: "session", state: "running", keep: true, home: "kept", directory: "/d", attachable: true, run: "claude-1" }];
  let pending = null;
  answer = (method, path) => {
    if (path === "/api/hosts/ct1/starts") return pending ? pending.promise : starts;
    if (path.endsWith("/net/web")) return { https: [], taint: { tainted: true, since: "2026-11-08T10:00:00Z" } };
    if (path.endsWith("/presets")) return [];
    return { output: "ok" };
  };
  await vm.runInContext("pages.starts(__main)", context); await settle();
  result.address = vm.runInContext("addressFor('starts')", context);
  button(rowOf("web"), "Network…").fire("click"); await settle();
  const card = document.getElementById("network-card");
  result.cardOpen = Boolean(card) && !button(card, "Allow…").disabled;
  // W4: Allow widens at once: asked first, naming what
  for (const id of ["net-tcp", "net-ports", "net-git-url", "net-git-ref"]) document.getElementById(id).value = "";
  document.getElementById("net-dns").checked = true;
  document.getElementById("net-hosts").value = "*.example.org";
  button(card, "Allow…").fire("click");
  result.netAsk = card.all().find(n => n.className === "confirm").textContent;
  result.netPostsBefore = calls.filter(c => c.method === "POST").length;
  button(card, "Yes, allow it").fire("click"); await settle();
  result.netPost = calls.filter(c => c.method === "POST").map(c => JSON.parse(c.body)).pop();
  result.untaint = Boolean(button(card, "Untaint…"));
  starts = new Error("ssh: connect to host ct1 port 22: No route to host");
  vm.runInContext("__now += 7000;", context);
  await vm.runInContext("refreshStarts(__main)", context); await settle();
  const web = rowOf("web");
  result.stale = [web.className, buttonsOf(web).every(b => b.disabled), web.textContent.includes("(last seen 7 s ago)"), main.textContent.includes("No route to host")];
  result.cardWhileStale = !button(card, "Allow…").disabled;
  // W10: a refresh asked for while one runs is queued, not dropped
  starts = [{ name: "web", agent: "claude", kind: "session", state: "running", keep: true, home: "kept", directory: "/d", attachable: true, run: "claude-1" }];
  let release; pending = { promise: new Promise(r => { release = r; }) };
  calls.length = 0;
  const first = vm.runInContext("refreshStarts(__main)", context); await settle();
  vm.runInContext("refreshStarts(__main, true)", context); await settle();
  result.whileLoading = calls.filter(c => c.path === "/api/hosts/ct1/starts").length;
  pending = null; release(starts); await first; await settle();
  result.queued = calls.filter(c => c.path === "/api/hosts/ct1/starts").map(c => c.headers["X-Background"] || null);
  result.fresh = [rowOf("web").className, buttonsOf(rowOf("web")).some(b => b.disabled)];
  // W11: the start stopped: its card is switched off, with why
  starts = [Object.assign({}, starts[0], { state: "stopped", attachable: false })];
  await vm.runInContext("refreshStarts(__main, true)", context); await settle();
  result.cardRetired = [button(card, "Allow…").disabled, card.textContent.includes("web on ct1 is stopped now"), !button(card, "Close").disabled];
  starts = [];
  await vm.runInContext("refreshStarts(__main, true)", context); await settle();
  result.gone = rowOf("web") === undefined;

  // every host at once: a host column, the asking badge, a failing host's rows greyed
  vm.runInContext('state.startsHost = "all"; state.questions = [{ host: "ct1", id: "1-1", run: "r9" }];', context);
  let fleet = { starts: [{ host: "local", name: "a", agent: "claude", kind: "session", state: "running" },
    { host: "ct1", name: "b", agent: "codex", kind: "session", state: "running", run: "r9" }], errors: {}, seen: { local: 5, ct1: 5 }, age: { local: 7, ct1: 7 } };
  answer = (method, path) => (path === "/api/starts" ? fleet : path.endsWith("/presets") ? [] : { output: "ok" });
  await vm.runInContext("pages.starts(__main)", context); await settle();
  result.fleet = ["a", "b"].map(n => [rowOf(n).attributes["data-host"], rowOf(n).children[0].textContent]);
  result.badge = [Boolean(rowOf("b").all().find(n => (n.className || "").includes("asking"))), Boolean(rowOf("a").all().find(n => (n.className || "").includes("asking")))];
  result.fleetAddress = vm.runInContext("addressFor('starts')", context);
  // the service's own clock is far off (seen): only the ages it sends count, from when the answer came
  fleet = { starts: [fleet.starts[0]], errors: { ct1: "no answer within 20 seconds" }, seen: { local: 99999999 }, age: { local: 0 } };
  vm.runInContext("__now = 1012000;", context);
  await vm.runInContext("refreshStarts(__main)", context); await settle();
  result.fleetStale = [rowOf("a").className, rowOf("b").className, rowOf("b").textContent.includes("(last seen 12 s ago)")];
  // the first visit: no start, no other host: the checklist
  vm.runInContext('state.hosts = [{ name: "local", kind: "local" }]; state.startsHost = "local"; state.host = "local";', context);
  answer = (method, path) => (path.endsWith("/starts") ? [] : path.endsWith("/doctor") ? [{ status: "fail" }, { status: "warn" }] : path.endsWith("/presets") ? [] : {});
  await vm.runInContext("pages.starts(__main)", context); await settle();
  const checklist = main.all().find(n => (n.className || "").includes("first-run"));
  result.firstRun = checklist ? checklist.textContent : null;
  console.log(JSON.stringify(result));
})().catch(e => { console.error(e.stack); process.exit(1); });
"""


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_prompts_stale_rows_queued_refreshes_retired_cards_and_the_fleet(tmp_path):
    result = run_driver(tmp_path, STARTS_DRIVER, "starts")
    # W2: never a prompt on top of a prompt; Cancel puts back what was there, the focus back on the control
    assert "Make lax instead of high" in result["first"]
    count, second = result["second"]
    assert count == 1 and "Make low instead of high" in second and "lax" not in second.split("instead")[0]
    assert result["cancelled"] == ["", "high", 0, True]
    # W3: a step up is saved at once; a step down from what the host has now is asked
    assert result["stricter"] == ["", [{"key": "mode", "value": "paranoid"}]]
    assert result["stepDown"] and result["lowered"] == {"key": "mode", "value": "high", "confirm": True}
    assert result["telemetryDown"], "strict to off is a step down"
    assert result["address"] == "#starts@ct1"
    # W4: the network's Allow asks first, naming what it widens
    assert result["cardOpen"] and result["netPostsBefore"] == 0
    for part in ("*.example.org", "real DNS", "web on ct1", "at once"):
        assert part in result["netAsk"], (part, result["netAsk"])
    assert result["netPost"] == {"op": "allow", "allow_host": ["*.example.org"], "allow_tcp": [], "host_ports": [],
                                 "dns": True, "confirm": True}
    assert result["untaint"], "a tainted run offers Untaint…"
    # W9: the rows of a listing that failed: as last seen, greyed, buttons off
    assert result["stale"] == ["stale", True, True, True]
    assert result["cardWhileStale"], "a host that did not answer is no news about its start"
    # W10: queued, not dropped
    assert result["whileLoading"] == 1 and result["queued"] == ["1", None]
    assert result["fresh"] == ["", False]
    # W11: the card of a start that stopped is switched off (its Close stays)
    assert result["cardRetired"] == [True, True, True] and result["gone"]
    # the fleet: the host in a column of its own, the asking badge, a failing host's rows greyed
    assert result["fleet"] == [["local", "local"], ["ct1", "ct1 · remote"]], "the host on every row, a remote one says so"
    assert result["badge"] == [True, False] and result["fleetAddress"] == "#starts@all"
    assert result["fleetStale"] == ["", "stale", True]
    for part in ("First steps", "1 problem(s), 1 warning(s)", "Add a host", "Start one below"):
        assert part in result["firstRun"], (part, result["firstRun"])


ROUND3_DRIVER = FAKE_DOM + r"""
const rowOf = (name) => body.all().find(n => n.attributes["data-name"] === name);
const confirmText = (node) => { const c = node.all().find(n => n.className === "confirm"); return c ? c.textContent : null; };
const posts = () => calls.filter(c => c.method !== "GET").map(c => [c.method, c.path, JSON.parse(c.body)]);
const byId = (id) => document.getElementById(id);
(async () => {
  const result = {};
  vm.runInContext('state.me = { user: "admin", csrf: "c", terminals: true }; state.host = "ct1"; state.hosts = [{ name: "local", kind: "local" }, { name: "ct1", kind: "ssh", ssh: "root@ct1", port: 22 }];', context);
  // a malformed address is the default page, not an exception
  result.address = vm.runInContext('location.hash = "#starts@%"; const a = addressOf(); location.hash = ""; [a.page, a.host]', context);

  // secrets: Yes sends the name that was asked about, or nothing
  let secretAnswer = null;
  answer = (method, path, sent) => (method === "GET" ? [{ name: "GITEA_TOKEN", store: "file" }] : secretAnswer ? secretAnswer(path, sent) : { output: "ok" });
  vm.runInContext('state.page = "secrets";', context);
  await vm.runInContext("pages.secrets(__main)", context); await settle();
  byId("secret-name").value = "GITEA_TOKEN"; byId("secret-value").value = "v1";
  button(main, "Add").fire("click");
  result.secretAsk = confirmText(main);
  byId("secret-name").value = "OTHER";
  button(main, "Yes, replace it").fire("click"); await settle();
  result.secretChanged = [posts().length, main.textContent.includes("The name is OTHER now, not GITEA_TOKEN as asked: nothing was sent.")];
  // a secret that came meanwhile: the service's refusal names its confirmation, and the page asks for it
  calls.length = 0;
  let first = true;
  secretAnswer = (path, sent) => { if (first) { first = false; return { __status: 400, __error: 'NEW_ONE: a secret of that name exists: its value is replaced: send {"confirm": true}', __confirm: "secret-replace" }; } return { output: "kept" }; };
  byId("secret-name").value = "NEW_ONE"; byId("secret-value").value = "v2";
  button(main, "Add").fire("click"); await settle();
  result.reasked = confirmText(main);
  button(main, "Yes, replace it").fire("click"); await settle();
  result.secretPosts = posts();

  // gc: the previewed days and list, or nothing
  calls.length = 0;
  let gcAnswer = { output: "ok" };
  answer = (method, path) => (method === "GET" ? { removed: [{ name: "old", id: "old-1", last_used: "2026-01-01T00:00:00Z" }], leftovers: ["gone"], histories: [] } : gcAnswer);
  vm.runInContext('gcCard(__main, "ct1")', context);
  byId("gc-days").value = "7";
  button(main, "Preview").fire("click"); await settle();
  button(main, "Remove these…").fire("click");
  byId("gc-days").value = "8";
  button(main, "Yes, remove them").fire("click"); await settle();
  result.gcChanged = [posts().length, main.textContent.includes("The days were changed after the preview: nothing was removed.")];
  byId("gc-days").value = "7";
  button(main, "Preview").fire("click"); await settle();
  button(main, "Remove these…").fire("click");
  button(main, "Yes, remove them").fire("click"); await settle();
  result.gcPost = posts();
  gcAnswer = { __status: 409, __error: "the clean-up would now remove more than the preview showed (removed new-2): nothing was removed; preview it again" };
  button(main, "Preview").fire("click"); await settle();
  button(main, "Remove these…").fire("click");
  button(main, "Yes, remove them").fire("click"); await settle();
  result.gc409 = byId("gc-card").textContent.includes("would now remove more than the preview showed");
  byId("gc-card").remove();

  // the starts: buttons named with start and host, states in the page's language, cards that follow a rename
  calls.length = 0;
  let starts = [{ name: "web", id: "web-1", agent: "claude", kind: "session", state: "running", keep: true, home: "kept", directory: "/d", attachable: true },
    { name: "mcp-files", id: "mcp-files", kind: "mcp", state: "running", serves: [] }];
  answer = (method, path, sent) => (path === "/api/hosts/ct1/starts" ? starts : path.endsWith("/presets") ? [] : path.includes("/net/") && method === "GET" ? { https: [] } : { output: "ok", start: "web", attached: "at its next start" });
  vm.runInContext('state.page = "starts"; state.startsHost = "ct1";', context);
  await vm.runInContext("pages.starts(__main)", context); await settle();
  result.labels = rowOf("web").all().filter(n => n.tagName === "button").map(b => b.getAttribute("aria-label"));
  result.state = rowOf("web").children[3].textContent;
  vm.runInContext('setLanguage("de")', context);
  await vm.runInContext("pages.starts(__main)", context); await settle();
  result.german = [rowOf("web").children[3].textContent, rowOf("web").all().find(n => n.tagName === "button" && n.textContent === "Stoppen").getAttribute("aria-label")];
  vm.runInContext('setLanguage("en")', context);
  await vm.runInContext("pages.starts(__main)", context); await settle();
  // attaching an MCP server is asked first, with what was chosen then
  rowOf("mcp-files").all().find(n => n.tagName === "select").value = "web";  // a browser's select: its first option
  rowOf("mcp-files").all().find(n => n.tagName === "input").checked = false;
  button(rowOf("mcp-files"), "Attach to…").fire("click");
  result.attachAsk = confirmText(rowOf("mcp-files"));
  result.attachBefore = posts().length;
  button(rowOf("mcp-files"), "Yes, attach it").fire("click"); await settle();
  result.attachPost = posts();
  await vm.runInContext("pages.starts(__main)", context); await settle();
  button(rowOf("web"), "Network…").fire("click"); await settle();
  const card = byId("network-card");
  starts = [Object.assign({}, starts[0], { name: "web-2" }), starts[1]];
  await vm.runInContext("refreshStarts(__main, true)", context); await settle();
  calls.length = 0;
  button(card, "Refresh").fire("click"); await settle();
  result.renamed = [Boolean(rowOf("web-2")), rowOf("web") === undefined, card.isConnected && !button(card, "Allow…").disabled,
    card.children[0].textContent, calls.map(c => c.path)];

  // another page: its heading has the focus
  answer = (method, path) => (path.endsWith("/doctor") ? [{ status: "warn", check: "x", detail: "y" }] : {});
  vm.runInContext('show("doctor")', context); await settle();
  result.focus = [globalThis.focused && globalThis.focused.tagName, globalThis.focused && globalThis.focused.getAttribute("tabindex"), globalThis.focused && globalThis.focused.textContent];
  result.doctorState = main.all().find(n => (n.className || "").includes("status-warn")).textContent;

  // the hosts page: each host's starts and open questions, from what was asked before
  answer = (method, path) => (path === "/api/hosts-activity" ? { local: { starts: 2, running: 1, questions: 0, age: 3 }, ct1: { starts: null, running: null, questions: 3, age: 1 } } : path === "/api/hosts-status" ? { ct1: null } : {});
  vm.runInContext('state.page = "hosts"; clear(__main);', context);
  await vm.runInContext("pages.hosts(__main)", context); await settle();
  const hostRows = main.all().filter(n => n.tagName === "tr");
  result.hosts = hostRows.slice(1).map(r => [r.children[0].textContent, r.children[3].textContent, r.children[4].textContent]);
  result.activityBackground = calls.filter(c => c.path === "/api/hosts-activity").map(c => c.headers["X-Background"] || null);

  // LLM: the API key's secret by name
  calls.length = 0;
  answer = (method, path) => (method === "GET" ? [] : { output: "ok" });
  vm.runInContext('state.page = "llm"; clear(__main);', context);
  await vm.runInContext("pages.llm(__main)", context); await settle();
  byId("llm-name").value = "gw"; byId("llm-url").value = "https://gw.example.org"; byId("llm-key-secret").value = "sk-the-key";
  byId("llm-model").value = ""; byId("llm-kind").value = "lmstudio";
  button(main, "Add").fire("click"); await settle();
  result.llmBad = [posts().length, main.textContent.includes("A secret's name")];
  byId("llm-key-secret").value = "GW_KEY";
  button(main, "Add").fire("click"); await settle();
  result.llmPost = posts();
  result.llmSecrets = Boolean(button(main, "Open Secrets"));

  // kits: what widens every later start of a kit is asked first
  calls.length = 0;
  answer = (method, path) => (method === "GET" ? { kits: [{ name: "mine", kind: "user", agents: {} }] } : { output: "ok" });
  vm.runInContext('state.page = "kits"; clear(__main);', context);
  await vm.runInContext("pages.kits(__main)", context); await settle();
  byId("kit-plugin").value = "egm@sandbox-ai"; byId("kit-plugin-source").value = ""; byId("kit-plugin-agent").value = "claude";
  byId("kit-target").value = "mine";
  button(main, "Add plugin").fire("click"); await settle();
  result.kitAsk = [confirmText(main), posts().length];
  button(main, "Yes, add it").fire("click"); await settle();
  result.kitPost = posts();

  // defaults: a stricter value the host refuses as lowering (it changed meanwhile): asked there
  calls.length = 0;
  let refuse = true;
  answer = (method, path, sent) => (method === "GET" ? { values: { mode: "high" }, canary_categories: ["credentials", "home", "workspace"] }
    : refuse ? (refuse = false, { __status: 400, __error: 'mode paranoid instead of x: this lowers the protection of every later start on that host: send {"confirm": true}', __confirm: "defaults-lower" }) : { output: "ok" });
  vm.runInContext('state.page = "settings"; clear(__main);', context);
  await vm.runInContext("pages.settings(__main)", context); await settle();
  const mode = byId("mode");
  mode.value = "paranoid"; mode.fire("change"); await settle();
  result.defaultsAsk = confirmText(main);
  button(main, "Yes, do it").fire("click"); await settle();
  result.defaultsPosts = posts();

  // the approvals page drawn while the poll asks: no second request
  calls.length = 0;
  vm.runInContext('state.page = "approvals"; state.polling = true;', context);
  await vm.runInContext("pages.approvals(__main)", context); await settle();
  result.approvalsWhilePolling = calls.length;
  vm.runInContext("state.polling = false;", context);

  // the host edit: where the commands go is asked first
  calls.length = 0;
  answer = (method, path) => (path === "/api/me" ? { user: "admin", csrf: "c" } : path === "/api/hosts" ? [{ name: "local", kind: "local" }, { name: "ct1", kind: "ssh", ssh: "root@ct1", port: 22 }]
    : path === "/api/approvals" ? { questions: [], errors: {} } : path === "/api/policy" ? null : method === "GET" ? [] : { name: "ct1", warnings: [] });
  vm.runInContext('state.page = "logs"; hostEditCard(__main, state.hosts[1]);', context);
  byId("edit-port").value = "2222"; byId("edit-description").value = "rack";
  button(byId("host-edit-card"), "Save").fire("click");
  result.editAsk = [confirmText(byId("host-edit-card")), posts().length];
  button(byId("host-edit-card"), "Yes, change it").fire("click"); await settle();
  result.editPost = posts()[0];

  // the session ended: the tab's title and a desktop notification (asked for before)
  const notes = [];
  context.__notes = notes;
  vm.runInContext('localStorage.getItem = (k) => (k === "sandbox-ai-notify" ? "1" : null); window.Notification = function (title, opts) { __notes.push([title, opts.tag]); }; window.Notification.permission = "granted"; state.questions = []; sessionEnded();', context);
  await settle();
  result.ended = [document.title, notes];
  console.log(JSON.stringify(result));
})().catch(e => { console.error(e.stack); process.exit(1); });
"""


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_confirmations_are_bound_to_what_was_asked_and_the_page_reads_well(tmp_path):
    """Round 3: Yes sends what the question named (a secret's name, the previewed days and list) or nothing; a refusal
    that names its confirmation is asked in place; attach and kit additions are asked first; a malformed address
    falls back; row buttons say what and where; states in the page's language; cards follow a rename; the heading
    gets the focus on another page; the hosts' counts; the API key's secret; the session's end in the title."""
    result = run_driver(tmp_path, ROUND3_DRIVER, "round3")
    assert result["address"] == [None, None]
    assert "Replace the value of GITEA_TOKEN on ct1?" in result["secretAsk"]
    assert result["secretChanged"] == [0, True], "a name changed after the question: nothing sent"
    assert result["reasked"].startswith("A secret NEW_ONE exists on ct1 now: replace its value?"), result["reasked"]
    assert result["secretPosts"] == [["POST", "/api/hosts/ct1/secrets/NEW_ONE", {"secret": "v2"}],
                                     ["POST", "/api/hosts/ct1/secrets/NEW_ONE", {"secret": "v2", "confirm": True}]]
    assert result["gcChanged"] == [0, True]
    assert result["gcPost"] == [["POST", "/api/hosts/ct1/gc", {"older_than": 7, "confirm": True, "expected": {
        "removed": ["old-1"], "leftovers": ["gone"], "histories": []}}]]
    assert result["gc409"]
    assert result["labels"][:3] == ["Attach web on ct1", "Pause web on ct1", "Stop web on ct1"]
    assert "History web on ct1" in result["labels"] and result["state"] == "running"
    assert result["german"] == ["läuft", "Stoppen: web auf ct1"]
    assert "Attach the MCP server files to web on ct1?" in result["attachAsk"] and result["attachBefore"] == 0
    assert result["attachPost"] == [["POST", "/api/hosts/ct1/mcp/files/attach", {"start": "web", "restart": False,
                                                                                "confirm": True}]]
    has_new, old_gone, card_live, heading, paths = result["renamed"]
    assert has_new and old_gone and card_live and heading == "Network of web-2 on ct1", result["renamed"]
    assert paths == ["/api/hosts/ct1/net/web-2"], "the card's requests name the start as it is called now"
    assert result["focus"] == ["h1", "-1", "Doctor on ct1"]
    assert result["doctorState"] == "! Warning", "its status as an icon and a word"
    assert result["hosts"] == [["local", "2 (1 running)", "0"], ["ct1", "?", "3"]]
    assert result["activityBackground"] == ["1"]
    assert result["llmBad"] == [0, True] and result["llmSecrets"]
    assert result["llmPost"] == [["POST", "/api/hosts/ct1/llm", {"name": "gw", "kind": "lmstudio",
                                                                 "url": "https://gw.example.org", "model": "",
                                                                 "api_key_secret": "GW_KEY"}]]
    assert "Add egm@sandbox-ai to the kit mine on ct1?" in result["kitAsk"][0] and result["kitAsk"][1] == 0
    assert result["kitPost"] == [["POST", "/api/hosts/ct1/kits/mine/add-plugin", {
        "plugin": "egm@sandbox-ai", "agent": "claude", "source": "", "create": False, "confirm": True}]]
    assert result["defaultsAsk"].startswith("The service asks first: mode paranoid")
    assert result["defaultsPosts"] == [["POST", "/api/hosts/ct1/defaults", {"key": "mode", "value": "paranoid"}],
                                       ["POST", "/api/hosts/ct1/defaults", {"key": "mode", "value": "paranoid",
                                                                            "confirm": True}]]
    assert result["approvalsWhilePolling"] == 0, "the poll's answer is drawn, not asked again"
    ask, before = result["editAsk"]
    assert "Change how this service reaches ct1 (port: 22 → 2222)" in ask and before == 0
    assert result["editPost"] == ["POST", "/api/hosts/ct1", {"ssh": "root@ct1", "port": 2222, "identity": "",
                                                             "command": "", "description": "rack",
                                                             "update_sudo": False, "confirm": True}]
    assert result["ended"] == ["(session ended) sandbox-ai", [["sandbox-ai: the session ended", "sandbox-ai-session"]]]
    script = (WEB / "app.js").read_text()
    render = script.split("async function render()")[1].split("\n}")[0]
    assert render.rstrip().endswith("poll();\n  show(state.page);"), "the poll first, the page takes its answer"


def test_the_page_never_uses_the_browsers_dialogs_or_markup_from_data():
    """Every confirmation is asked in the page (ask/confirmIn), never confirm(); nothing is set as markup."""
    import re
    script = (WEB / "app.js").read_text()
    code = re.sub(r"//[^\n]*", "", script)
    assert not re.search(r"(?<![\w.])confirm\(", code), "no window.confirm()"
    assert not re.search(r"\b(alert|prompt)\(", code)
    assert not re.search(r"\.(innerHTML|outerHTML)\s*=|insertAdjacentHTML|document\.write", code)
    assert "function confirmIn(container, text, yes, onYes, options)" in script
    for asked in ("Yes, stop it", "Yes, delete it", "Yes, remove it", "Yes, forget it", "Yes, no limit",
                  "Yes, lower it", "Yes, block always"):
        assert asked in script, asked
    assert 'rel: "noopener noreferrer"' in script and "^https?:" in script


def test_the_page_is_reachable_and_announced_sensibly():
    """#app is no live region (each new drawing would be read out); a toast is told through the one live region
    (say()), so neither it nor their box is one (a toast would be announced twice); the questions' count has its own; the nav says which page is current and the
    address keeps it and its host (#page@host); terminal tabs are tabs, their close button is outside the tablist;
    the boxes of open terminals outlive a new drawing of the frame; the tables have captions; a confirmation gives
    the focus back to what opened it."""
    html = (WEB / "index.html").read_text()
    script = (WEB / "app.js").read_text()
    assert '<div id="app"></div>' in html and 'id="live"' in html and '<div id="toasts" class="toasts"></div>' in html
    assert 'id: "toasts", class: "toasts" }' in script and '"aria-live"' not in script.split("function toast(")[1].split("\n}")[0]
    assert '"aria-current": state.page === key ? "page" : null' in script and 'setAttribute("aria-current", "page")' in script
    assert 'window.addEventListener("hashchange"' in script and "location.hash = addressFor(page)" in script
    assert "const where = addressOf();" in script and "useAddressHost(where)" in script
    assert script.count('el("caption", { class: "sr-only" }') >= 5 and "function returnFocus(trigger, container)" in script
    page = script.split("pages.terminals = ")[1].split("\n};")[0]
    assert '"aria-selected": current ? "true" : "false"' in page and 'role: "tablist"' in page
    assert 'el("div", { class: "terminal-close" })' in page and "terminalBoxes().append(t.box)" in page
    assert 'role: "tabpanel"' in script


def test_an_ended_terminal_keeps_its_tab_and_says_why():
    """The tab of a terminal that ended stays until it is closed, with the close code and the service's reason; its
    window listener goes with it. The page's polls are background requests."""
    script = (WEB / "app.js").read_text()
    handler = script.split("ws.onclose = (event) => {")[1].split("\n  };")[0]
    assert "setTimeout" not in handler, "no tab vanishes on its own"
    assert 'window.removeEventListener("resize", entry.fit)' in handler and "closeText(event, entry.opened)" in handler
    assert "event.code" in script.split("function closeText")[1] and "event.reason" in script.split("function closeText")[1]
    assert "function closeTerminal(entry)" in script and "onclick: () => closeTerminal(entry)" in script
    assert 'api("GET", "/api/approvals", undefined, { background: true })' in script
    assert 'hostPath("/starts"), undefined, { background: Boolean(background) }' in script
    assert 'refreshTerminalStarts(main, true)' in script and 'const main = document.getElementById("main")' in script
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


# --- the doctor for a human, a host's install, the starts grouped (node, the stand-in DOM above) ----------------

DOCTOR_DRIVER = FAKE_DOM + r"""
const byClass = (node, cls) => node.all().filter(n => (n.className || "").split(" ").includes(cls));
(async () => {
  const result = {};
  vm.runInContext('state.me = { user: "admin", csrf: "c" }; state.host = "ct1";', context);
  result.groupOf = vm.runInContext('["Login claude", "Podman", "Image codex", "systemd user session", "Workspace /x", "Kits"].map(doctorGroupOf)', context);
  result.commands = vm.runInContext(`[
    "Run: \x60podman system migrate\x60, then try again.", "sandbox-ai build claude", "sudo modprobe tun (or reboot)",
    "Log in again with the agent itself.", "run: loginctl enable-linger you; then log in again", "try: sysctl -w x=1, or ask an admin",
    "apt install podman.", ""].map(doctorCommand)`, context);
  const checks = [
    { status: "ok", check: "Python", detail: "3.12" },
    { status: "warn", check: "Login claude", detail: "expired 2 days ago", hint: "Log in again with the agent itself." },
    { status: "fail", check: "Podman", detail: "not found on PATH", hint: "Run: \x60sudo apt install podman\x60 (Debian, Ubuntu)." },
    { status: "ok", check: "Bubblewrap", detail: "0.9", group: "Engine" },
    { status: "skip", check: "Project", detail: "no project here", group: "Project" },
    { status: "fail", check: "a check of a newer doctor", detail: "a very long detail ".repeat(20), hint: "sandbox-ai build claude", group: "Agents" },
    { status: "bogus", check: 42 }, "not a check", null];
  context.__checks = checks;
  const view = vm.runInContext("doctorView(__checks, { level: 2, host: 'ct1' })", context);
  result.summary = byClass(view, "doctor-summary")[0].textContent;
  // the areas with problems and warnings, open, in the doctor's order; in each, problems first
  const openGroups = view.children.filter(n => n.tagName === "section");
  result.open = openGroups.map(g => [g.attributes["data-group"], byClass(g, "doctor-check").map(c => [c.attributes["data-status"], byClass(c, "doctor-name")[0].textContent])]);
  result.headings = openGroups.map(g => g.children[0].tagName);
  const podman = openGroups.flatMap(g => byClass(g, "doctor-check")).find(c => c.textContent.includes("Podman"));
  result.podman = [byClass(podman, "doctor-state")[0].textContent, byClass(podman, "doctor-detail")[0].textContent,
    byClass(podman, "doctor-hint").map(n => n.textContent), byClass(podman, "command-block").map(b => b.all().find(n => n.tagName === "code").textContent)];
  const newer = openGroups.flatMap(g => byClass(g, "doctor-check")).find(c => c.textContent.includes("a newer doctor"));
  result.bareCommand = [byClass(newer, "doctor-hint").length, byClass(newer, "command-block").length];
  const login = openGroups.flatMap(g => byClass(g, "doctor-check")).find(c => c.textContent.includes("Login claude"));
  result.advice = [byClass(login, "doctor-hint")[0].textContent, byClass(login, "command-block").length];
  // the fine ones and those not checked: folded away (closed details), by area
  result.folded = view.children.filter(n => n.tagName === "details").map(d => [d.attributes["data-status"], "open" in d.attributes,
    d.children[0].textContent, d.children.slice(1).map(g => g.attributes["data-group"]), byClass(d, "doctor-check").length]);
  // Copy: the clipboard, else the command selected
  const copied = [];
  vm.runInContext("globalThis.navigator = { clipboard: { writeText: async (text) => { __copied.push(text); } } };", Object.assign(context, { __copied: copied }));
  const copy = byClass(podman, "command-block")[0].all().find(n => n.tagName === "button");
  result.copyLabel = copy.getAttribute("aria-label");
  copy.fire("click"); await settle();
  result.copied = [copied, copy.textContent, live.textContent];
  vm.runInContext("globalThis.navigator = {};", context);
  copy.fire("click"); await settle();
  result.selected = copy.textContent;
  // all fine: the summary says so, nothing open
  context.__fine = [{ status: "ok", check: "Python" }, { status: "ok", check: "Podman" }];
  const fine = vm.runInContext("doctorView(__fine, { level: 3 })", context);
  result.fine = [byClass(fine, "doctor-summary")[0].textContent, fine.children.filter(n => n.tagName === "section").length,
    fine.children.filter(n => n.tagName === "details").map(d => d.children[0].textContent)];
  // an older sandbox-ai's text: as it is, with a note
  const text = vm.runInContext('doctorView("ok    Python   3.12\\nFAIL  Podman   not found", { host: "old1" })', context);
  result.text = [text.all().find(n => n.tagName === "pre").textContent, byClass(text, "notice")[0].textContent];
  // the doctor page and a host's test: the same view
  answer = (method, path) => (path.endsWith("/doctor") ? checks.slice(0, 3) : path.endsWith("/test") ? { version: "sandbox-ai 0.1.80", doctor: "FAIL  Podman" } : path === "/api/hosts-status" ? {} : path === "/api/hosts-activity" ? {} : {});
  vm.runInContext('state.page = "doctor";', context);
  await vm.runInContext("pages.doctor(__main)", context); await settle();
  result.page = [byClass(main, "doctor").length, main.all().filter(n => n.tagName === "table").length];
  vm.runInContext('state.page = "hosts"; state.hosts = [{ name: "local", kind: "local" }, { name: "ct1", kind: "ssh", ssh: "admin@ct1" }]; clear(__main);', context);
  await vm.runInContext("pages.hosts(__main)", context); await settle();
  const test = main.all().find(n => n.tagName === "button" && n.textContent === "Test" && n.parent.parent.children[0].textContent === "ct1");
  test.fire("click"); await settle();
  result.hostTest = [byClass(main, "doctor").length, main.all().some(n => n.tagName === "pre" && n.textContent === "FAIL  Podman")];
  console.log(JSON.stringify(result));
})().catch(e => { console.error(e.stack); process.exit(1); });
"""


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_the_doctor_is_readable_by_area_with_its_commands_to_copy(tmp_path):
    """One view for the doctor page and a host's test: by area, problems and warnings first and open, each with its
    status as an icon and a word, what was found, the advice, the fix command in a block of its own with Copy; the
    fine ones folded away; an older sandbox-ai's text as it is, with a note."""
    result = run_driver(tmp_path, DOCTOR_DRIVER, "doctor")
    assert result["groupOf"] == ["Agents", "Engine", "Engine", "System", "Project", "Agents"]
    assert result["commands"] == ["podman system migrate", "sandbox-ai build claude", "sudo modprobe tun", "",
                                  "loginctl enable-linger you", "sysctl -w x=1", "apt install podman", ""]
    assert result["summary"] == "2 problem(s), 1 warning(s), 2 fine: each says how to fix it. 2 not checked."
    assert result["open"] == [["Engine", [["fail", "Podman"]]],
                              ["Agents", [["fail", "a check of a newer doctor"], ["warn", "Login claude"]]]]
    assert result["headings"] == ["h2", "h2"]
    state, detail, hint, command = result["podman"]
    assert state == "✘ Problem" and detail == "not found on PATH"
    assert hint == ["What to do: Run: `sudo apt install podman` (Debian, Ubuntu)."] and command == ["sudo apt install podman"]
    assert result["bareCommand"] == [0, 1], "a hint that is only its command: the command block alone"
    assert result["advice"] == ["What to do: Log in again with the agent itself.", 0], "advice in words: no command"
    assert result["folded"] == [["ok", False, "2 checks are fine", ["System", "Engine"], 2],
                                ["skip", False, "2 checks were not made here", ["Project"], 2]]
    assert result["copyLabel"] == "Copy the command sudo apt install podman"
    assert result["copied"] == [["sudo apt install podman"], "Copied", "Copied: sudo apt install podman"]
    assert result["selected"] == "Selected: copy it with Ctrl+C", "no clipboard: the command is selected to copy"
    assert result["fine"] == ["Everything needed is in place: 2 checks are fine.", 0, ["2 checks are fine"]]
    pre, note = result["text"]
    assert pre == "ok    Python   3.12\nFAIL  Podman   not found" and "old1 answered with text" in note
    assert result["page"] == [1, 0], "the doctor page: the view, no table"
    assert result["hostTest"] == [1, True], "a host's test: the same view, an older one's text as it is"
    css = (WEB / "app.css").read_text()
    for rule in (".doctor-check {", "overflow-wrap: anywhere", ".command-block code", "white-space: pre-wrap",
                 "@media (max-width: 760px)"):
        assert rule in css, rule


INSTALL_DRIVER = FAKE_DOM + r"""
const byId = (id) => document.getElementById(id);
const confirmText = (node) => { const c = node.all().find(n => n.className === "confirm"); return c ? c.textContent : null; };
const posts = () => calls.filter(c => c.method === "POST").map(c => [c.path, JSON.parse(c.body)]);
(async () => {
  const result = {};
  vm.runInContext('state.me = { user: "admin", csrf: "c", version: "0.1.80" }; state.page = "hosts"; state.hosts = [{ name: "local", kind: "local" }, { name: "ct1", kind: "ssh", ssh: "admin@ct1" }, { name: "ct2", kind: "ssh", ssh: "root@ct2", update_sudo: true }];', context);
  let installed = { name: "ct1", before: null, after: "0.1.80", changed: true, mode: "system", prefix: "/opt/sai", sudo: true, root: false, source: "here", error: null, needs_password: false, output: "install.sh: done", notes: ["a note"] };
  answer = (method, path) => (method === "GET" && path.endsWith("/install") ? { running: false, seconds: null, last: null } : path.endsWith("/install") ? installed : path.endsWith("/update") ? updated : {});
  let updated = {};
  // for every user, with sudo, from this machine's copy: the question names the host, for whom and how
  const card = vm.runInContext("installCard(__main, state.hosts[1])", context); await settle();
  byId("install-source").value = "here";  // a browser's select: its selected option
  byId("install-mode").value = "system"; byId("install-mode").fire("change");
  result.shown = [byId("install-prefix").hidden, byId("install-version").hidden, byId("install-sudo").disabled];
  byId("install-prefix").value = "/opt/../etc";
  const install = card.all().find(n => n.tagName === "button" && n.textContent === "Install");
  install.fire("click");
  result.badPrefix = [card.textContent.includes("no . or .. parts"), posts().length, confirmText(card)];
  byId("install-prefix").value = "/opt/sai"; byId("install-sudo").value = "sudo";
  install.fire("click");
  result.ask = [confirmText(card), posts().length];
  card.all().find(n => n.tagName === "button" && n.textContent === "Yes, install it").fire("click"); await settle();
  result.post = posts();
  result.done = [card.textContent.includes("ct1: sandbox-ai none → 0.1.80, installed for every user in /opt/sai, with sudo."),
    card.textContent.includes("ct1: a note"), card.all().some(n => n.tagName === "details" && n.textContent.includes("install.sh: done")),
    toasts().some(t => t.includes("sandbox-ai installed on ct1"))];
  // sudo there wants a password: no terminals here, the exact command to run
  calls.length = 0;
  installed = Object.assign({}, installed, { after: null, changed: false, needs_password: true, error: "sudo on ct1 wants a password" });
  byId("install-sudo").value = "none";
  install.fire("click");
  result.askNoSudo = confirmText(card);
  card.all().find(n => n.tagName === "button" && n.textContent === "Yes, install it").fire("click"); await settle();
  result.postNoSudo = posts()[0][1];
  installed.needs_password = true;
  byId("install-sudo").value = "sudo"; byId("install-source").value = "download"; byId("install-source").fire("change");
  byId("install-version").value = "0.1.79";
  result.versionShown = !byId("install-version").hidden;
  calls.length = 0;
  install.fire("click");
  card.all().find(n => n.tagName === "button" && n.textContent === "Yes, install it").fire("click"); await settle();
  result.postDownload = posts()[0][1];
  const help = card.all().find(n => (n.className || "").includes("password-help"));
  result.password = [help.all().find(n => n.tagName === "code").textContent, card.all().some(n => n.tagName === "button" && n.textContent === "Install in a terminal (sudo asks there)")];
  result.query = vm.runInContext('installQuery({ mode: "system", prefix: "/opt/sai", sudo: true, source: "download", version: "0.1.79" })', context);
  // the SSH user is root: for every user as root, no sudo to choose
  calls.length = 0;
  installed = Object.assign({}, installed, { name: "ct2", needs_password: false, error: null, root: true, sudo: false, after: "0.1.80" });
  const rootCard = vm.runInContext("installCard(__main, state.hosts[2])", context); await settle();
  result.rootForm = [byId("install-mode").value === undefined ? null : byId("install-mode").value, byId("install-sudo").disabled, rootCard.textContent.includes("is root: an installation for every user needs no sudo")];
  byId("install-mode").value = "system"; byId("install-source").value = "here"; byId("install-prefix").value = "/usr/local";
  const rootInstall = rootCard.all().find(n => n.tagName === "button" && n.textContent === "Install");
  rootInstall.fire("click");
  result.rootAsk = confirmText(rootCard);
  rootCard.all().find(n => n.tagName === "button" && n.textContent === "Yes, install it").fire("click"); await settle();
  result.rootPost = posts()[0];
  // the update: as the host's setting, with sudo or without; a password: the exact command
  calls.length = 0;
  updated = { name: "ct1", before: "0.1.70", after: null, error: "sudo on ct1 wants a password", needs_password: true, notes: [] };
  const output = new Node("div");
  context.__output = output;
  await vm.runInContext('updateHost("ct1", __output, sudoChoice({ value: "sudo" }))', context);
  await vm.runInContext('updateHost("ct1", __output, sudoChoice({ value: "none" }))', context);
  await vm.runInContext('updateHost("ct1", __output, sudoChoice({ value: "" }))', context);
  result.updates = posts().map(p => p[1]);
  result.updatePassword = output.all().find(n => (n.className || "").includes("password-help")).all().find(n => n.tagName === "code").textContent;
  // the hosts page: a host without sandbox-ai offers the install in its version cell
  answer = (method, path) => (path === "/api/hosts-status" ? { ct1: { error: "bash: sandbox-ai: command not found (exit status 127)" }, ct2: { version: "0.1.80", update: false } } : path === "/api/hosts-activity" ? {} : {});
  vm.runInContext("clear(__main)", context);
  await vm.runInContext("pages.hosts(__main)", context); await settle();
  const rows = main.all().filter(n => n.tagName === "tr");
  const version = (name) => rows.find(r => r.children[0] && r.children[0].textContent === name).children[2];
  result.missing = [version("ct1").textContent, version("ct2").textContent];
  result.sudoSelects = main.all().filter(n => n.tagName === "select" && (n.getAttribute("aria-label") || "").startsWith("sudo for the update")).map(s => s.children[0].textContent);
  console.log(JSON.stringify(result));
})().catch(e => { console.error(e.stack); process.exit(1); });
"""


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_a_hosts_install_is_chosen_asked_and_sent_as_chosen(tmp_path):
    """The Install… form: for the SSH user or every user (a prefix), as root when the SSH user is root, with sudo or
    without, from this machine's copy or the signed release (a version); checked before it asks, asked naming the
    host and how, sent as asked; its answer (before → after, notes, the output folded); sudo that wants a password:
    the exact command. The update's sudo choice, and a host without sandbox-ai offering the install."""
    result = run_driver(tmp_path, INSTALL_DRIVER, "install")
    assert result["shown"] == [False, True, False], "a prefix for every user; a version only for a download"
    assert result["badPrefix"] == [True, 0, None], "checked before anything is asked or sent"
    ask, before = result["ask"]
    assert ask == "Install sandbox-ai on ct1 (admin@ct1) for every user in /opt/sai, with sudo; from this machine's copy?"
    assert before == 0
    assert result["post"] == [["/api/hosts/ct1/install", {"mode": "system", "source": "here", "prefix": "/opt/sai",
                                                          "sudo": True, "expected": {"ssh": "admin@ct1", "port": 22},
                                                          "confirm": True}]], "the SSH target the question named"
    assert result["done"] == [True, True, True, True]
    assert "for every user in /opt/sai, without sudo" in result["askNoSudo"]
    assert result["postNoSudo"] == {"mode": "system", "source": "here", "prefix": "/opt/sai", "sudo": False,
                                    "expected": {"ssh": "admin@ct1", "port": 22}, "confirm": True}
    assert result["versionShown"]
    assert result["postDownload"] == {"mode": "system", "source": "download", "prefix": "/opt/sai", "sudo": True,
                                      "version": "0.1.79", "expected": {"ssh": "admin@ct1", "port": 22},
                                      "confirm": True}
    assert result["password"] == ["sandbox-ai hosts install ct1 --system --prefix=/opt/sai --download --version=0.1.79",
                                  False], "no terminals in this page: the command to run, no terminal button"
    assert result["query"] == {"mode": "system", "source": "download", "prefix": "/opt/sai", "version": "0.1.79",
                               "sudo": "1", "confirm": "1"}
    assert result["rootForm"][1:] == [True, True]
    assert "as root (the SSH user is root)" in result["rootAsk"] and "ct2 (root@ct2)" in result["rootAsk"]
    assert result["rootPost"] == ["/api/hosts/ct2/install", {"mode": "system", "source": "here", "prefix": "/usr/local",
                                                            "expected": {"ssh": "root@ct2", "port": 22},
                                                            "confirm": True}], "as root: no sudo sent"
    assert result["updates"] == [{"sudo": True}, {"sudo": False}, {}]
    assert result["updatePassword"] == "sandbox-ai hosts update ct1 --sudo"
    missing, current = result["missing"]
    assert missing == "not installed Install…" and current == "0.1.80, up to date"
    assert result["sudoSelects"] == ["as set: without sudo", "as set: with sudo"]


GROUPED_DRIVER = FAKE_DOM + r"""
const rowOf = (name) => body.all().find(n => n.attributes["data-name"] === name);
const tbody = () => main.all().find(n => n.tagName === "tbody");
const rowsShown = () => tbody().children.map(r => [r.attributes["data-group"] ? `[${r.attributes["data-group"]}]` : r.attributes["data-name"] || `note ${r.attributes["data-for"]}`, r.hidden ? "hidden" : "shown", (r.className || "").includes("related") ? "related" : ""]);
const foldOf = (r) => r.all().find(n => (n.className || "").split(" ").includes("fold"));
(async () => {
  const result = {};
  const stored = {};
  vm.runInContext('localStorage.getItem = (k) => (__stored[k] === undefined ? null : __stored[k]); localStorage.setItem = (k, v) => { __stored[k] = String(v); };', Object.assign(context, { __stored: stored }));
  vm.runInContext('state.me = { user: "admin", csrf: "c" }; state.page = "starts"; state.host = "ct1"; state.startsHost = "ct1"; state.hosts = [{ name: "local", kind: "local" }, { name: "ct1", kind: "ssh", ssh: "admin@ct1", port: 2222 }];', context);
  let starts = [
    { id: "s-web", name: "web", agent: "claude", kind: "session", state: "running", keep: true, home: "kept", directory: "/d", attachable: true, run: "claude-1", backend: "podman", mcp: ["files", "gone"], mcp_running: ["files"] },
    { id: "mcp-files", name: "mcp-files", kind: "mcp", state: "running", serves: ["web", "api"] },
    { id: "s-help", name: "web-helper", agent: "codex", kind: "run", state: "running", helper_of: "claude-1" },
    { id: "s-api", name: "api", agent: "codex", kind: "session", state: "stopped", keep: true, home: "kept", directory: "/e", mcp: ["files"] },
    { id: "mcp-lone", name: "mcp-lone", kind: "mcp", state: "running", serves: [] }];
  answer = (method, path) => (path.endsWith("/starts") ? starts : path.endsWith("/presets") ? [] : { output: "ok" });
  await vm.runInContext("pages.starts(__main)", context); await settle();
  result.first = rowsShown();
  const header = tbody().children[0];
  result.header = [header.textContent, foldOf(header).getAttribute("aria-expanded")];
  result.markers = ["web", "mcp-files", "api"].map(n => rowOf(n).children[0].textContent);
  result.note = tbody().children.find(r => r.attributes["data-for"]).textContent;
  const web = rowOf("web"), webFold = foldOf(web);
  result.webFold = [webFold.getAttribute("aria-expanded"), webFold.getAttribute("aria-label"), webFold.textContent];
  // collapse web: what belongs to it is hidden, kept in this browser by host and start id
  webFold.fire("click");
  result.collapsed = [rowsShown(), webFold.getAttribute("aria-expanded"), foldOf(web) === webFold, JSON.parse(stored["sandbox-ai-starts-open"])];
  // a poll: the same rows, still collapsed
  starts = starts.map(s => (s.name === "api" ? Object.assign({}, s, { state: "running" }) : s));
  await vm.runInContext("refreshStarts(__main)", context); await settle();
  result.polled = [rowsShown(), rowOf("web") === web, foldOf(rowOf("web")).getAttribute("aria-expanded")];
  // the page drawn anew, in a new page load (what this browser kept): still collapsed
  vm.runInContext("openChoices.map = null; clear(__main);", context);
  await vm.runInContext("pages.starts(__main)", context); await settle();
  result.reloaded = rowsShown();
  // the host folded: only its header; Expand all / Collapse all
  foldOf(tbody().children[0]).fire("click");
  result.hostFolded = [rowsShown(), foldOf(tbody().children[0]).getAttribute("aria-expanded")];
  main.all().find(n => n.tagName === "button" && n.textContent === "Expand all").fire("click");
  result.expandAll = [rowsShown(), main.all().filter(n => (n.className || "").split(" ").includes("fold")).map(b => b.getAttribute("aria-expanded"))];
  main.all().find(n => n.tagName === "button" && n.textContent === "Collapse all").fire("click");
  result.collapseAll = rowsShown();
  main.all().find(n => n.tagName === "button" && n.textContent === "Expand all").fire("click");
  // the host stopped answering: its rows as last seen, its header says so
  starts = new Error("ssh: connect to host ct1 port 2222: No route to host");
  vm.runInContext("__now += 9000;", context);
  await vm.runInContext("refreshStarts(__main)", context); await settle();
  result.stale = [tbody().children[0].textContent, rowOf("web").className, foldOf(rowOf("web")).disabled];
  // every host: a header each, by the hosts list's order, the host on every row
  vm.runInContext('state.startsHost = "all"; clear(__main);', context);
  const fleet = { starts: [{ host: "ct1", id: "b", name: "b", agent: "codex", kind: "session", state: "paused" },
    { host: "local", id: "a", name: "a", agent: "claude", kind: "session", state: "running" },
    { host: "local", id: "mcp-x", name: "mcp-x", kind: "mcp", state: "running", serves: ["a"] }], errors: {}, age: { local: 3, ct1: 4 } };
  answer = (method, path) => (path === "/api/starts" ? fleet : path.endsWith("/presets") ? [] : { output: "ok" });
  await vm.runInContext("pages.starts(__main)", context); await settle();
  result.fleet = [rowsShown(), tbody().children.filter(r => r.attributes["data-group"]).map(r => r.textContent)];
  console.log(JSON.stringify(result));
})().catch(e => { console.error(e.stack); process.exit(1); });
"""


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_the_starts_are_grouped_by_host_and_by_what_belongs_together(tmp_path):
    """The starts by host (a header row: where, counts, whether it answers), each start with what belongs to it
    indented below it (the MCP servers it reaches, its helper, what it names but is not listed); folding is kept
    across the polls and in this browser (by host and start id), Expand all / Collapse all, aria-expanded."""
    result = run_driver(tmp_path, GROUPED_DRIVER, "grouped")
    assert result["first"] == [["[ct1]", "shown", ""], ["web", "shown", ""], ["mcp-files", "shown", "related"],
                               ["web-helper", "shown", "related"], ["note ct1 s-web", "shown", "related"],
                               ["api", "shown", ""], ["note ct1 s-api", "shown", "related"], ["mcp-lone", "shown", ""]]
    header, expanded = result["header"]
    assert expanded == "true"
    for part in ("ct1", "remote, over SSH: admin@ct1:2222", "5 start(s), 4 running, 1 stopped", "answering"):
        assert part in header, (part, header)
    assert result["markers"] == ["ct1 · remote"] * 3, "the host on every row"
    assert result["note"] == "↳ MCP server gone: not running now (it starts with web) · Podman: its run claude-1"
    assert result["webFold"] == ["true", "Collapse what belongs to web on ct1", "▾ 4"], "2 rows and 2 notes"
    rows, expanded, same, kept = result["collapsed"]
    assert [r[:2] for r in rows] == [["[ct1]", "shown"], ["web", "shown"], ["mcp-files", "hidden"],
                                     ["web-helper", "hidden"], ["note ct1 s-web", "hidden"], ["api", "shown"],
                                     ["note ct1 s-api", "shown"], ["mcp-lone", "shown"]]
    assert expanded == "false" and same, "the same button, the focus stays on it"
    assert kept == {"s ct1 s-web": False}
    rows, same, expanded = result["polled"]
    assert rows == result["collapsed"][0] and same and expanded == "false", "a poll keeps what was folded"
    assert result["reloaded"] == result["collapsed"][0], "and so does a new page load (this browser kept it)"
    rows, expanded = result["hostFolded"]
    assert [r[:2] for r in rows][0] == ["[ct1]", "shown"] and all(r[1] == "hidden" for r in rows[1:]) and expanded == "false"
    rows, folds = result["expandAll"]
    assert all(r[1] == "shown" for r in rows) and folds == ["true"] * len(folds) and len(folds) == 3
    assert [r[1] for r in result["collapseAll"]] == ["shown"] + ["hidden"] * 7, "Collapse all: one line per host"
    header, stale, fold_off = result["stale"]
    assert "not answering: last seen 9 s ago" in header and stale == "stale" and fold_off is False
    rows, headers = result["fleet"]
    assert rows == [["[local]", "shown", ""], ["a", "shown", ""], ["mcp-x", "shown", "related"],
                    ["[ct1]", "shown", ""], ["b", "shown", ""]]
    assert "this machine" in headers[0] and "2 start(s), 2 running" in headers[0]
    assert "1 start(s), 1 paused" in headers[1] and "answering" in headers[1]


PARITY_DRIVER = FAKE_DOM + r"""
const buttonIn = (node, text) => node.all().find(n => n.tagName === "button" && n.textContent === text);
const byId = (id) => body.all().find(n => n.id === id);
(async () => {
  const result = {};
  vm.runInContext('state.me = { user: "admin", csrf: "c", terminals: true }; state.host = "ct1"; state.hosts = [{ name: "local", kind: "local" }, { name: "ct1", kind: "ssh", ssh: "root@ct1" }]; var terminalsOpened = []; openTerminal = (params, title) => { terminalsOpened.push(params); };', context);
  // the model of a new start: an LLM server of that host as --llm NAME[:MODEL]; --model goes; the agent's own
  // arguments after -- stay; a server the agent cannot use is shown disabled
  const model = (list, choice, only) => vm.runInContext(`withModel(${JSON.stringify(list)}, ${JSON.stringify(choice)}, ${only})`, context);
  result.withModel = [model(["--mode", "high", "--model", "opus", "-mm", "x", "--model=y", "--llm", "old", "-nd"], "gpu:qwen3", true),
    model(["--mode", "high", "--model", "opus"], "", false), model(["--allow-pypi", "--", "--model", "kept"], "gpu", false)];
  vm.runInContext('state.page = "starts"; state.startsHost = "ct1";', context);
  answer = (method, path) => (path.endsWith("/starts") ? [] : path.endsWith("/presets") ? [] : path.endsWith("/llm") ?
    [{ name: "gpu", kind: "vllm", model: "qwen3", agents: ["codex", "opencode"] }, { name: "gw", kind: "openai-compatible", agents: ["claude", "codex", "opencode"] }]
    : path.endsWith("/llm/gpu/models") ? [{ name: "qwen3" }, { name: "llama3" }] : path.endsWith("/llm/gw/models") ? [] : { output: "ok" });
  await vm.runInContext("pages.starts(__main)", context); await settle();
  byId("agent").value = "claude"; byId("agent").fire("change"); byId("name").value = ""; byId("workspace").value = "";
  const select = byId("start-model");
  result.models = select.children.map(o => [o.value, Boolean(o.attributes.disabled !== undefined), o.textContent]);
  select.value = "gw"; byId("start-only-server").checked = true;
  byId("options").value = "--mode high\n--model opus";
  // no workspace (neither the field nor --workspace in the options): nothing starts, the form says why (review 5 M3)
  buttonIn(__main_of(), "Start in a terminal").fire("click"); await settle();
  result.noWorkspace = [vm.runInContext("terminalsOpened.length", context), byId("start-status").textContent, byId("workspace-hint").textContent];
  byId("workspace").value = "/srv/w";
  buttonIn(__main_of(), "Start in a terminal").fire("click"); await settle();
  result.started = vm.runInContext("terminalsOpened[terminalsOpened.length - 1]", context);
  result.namesWorkspace = vm.runInContext('[namesWorkspace(["-w", "/x"]), namesWorkspace(["-w/x"]), namesWorkspace(["--workspace=/x"]), namesWorkspace(["--workspace-ro"]), namesWorkspace(["-wa", "codex"]), namesWorkspace(["--", "-w", "/x"])]', context);
  // a start refused for an untrusted project file: the path from what the terminal printed
  result.untrusted = vm.runInContext(String.raw`[untrustedConfig("\u001b[31msandbox-ai: Project configuration /home/u/my web/.sandbox-ai.json is not trusted. Review it, then run: sandbox-ai wizard trust ..."), untrustedConfig("Project configuration /p/.sandbox-ai.json is changed since it was trusted. Review"), untrustedConfig("all fine"), untrustedConfig("agent-sandbox: /work is /srv/x; configuration: none\r\nProject configuration /evil/.sandbox-ai.json is not trusted")]`, context);
  // Projects: reviewed, then trusted with exactly the digest shown, asked first naming the host
  calls.length = 0;
  answer = (method, path, sent) => (path.endsWith("/projects") ? [{ path: "/p/.sandbox-ai.json", digest: "a".repeat(64), state: "changed" }]
    : path.endsWith("/projects/review") ? { path: "/p/.sandbox-ai.json", digest: "b".repeat(64), document: { version: 1, agent: "codex", options: ["--dns"] }, trusted: false, trusted_digest: "a".repeat(64), problem: null }
    : { output: "Trusted." });
  vm.runInContext('state.page = "projects"; state.review = { host: "ct1", path: "/p/.sandbox-ai.json" };', context);
  await vm.runInContext("pages.projects(__main)", context); await settle();
  const card = byId("project-review");
  result.review = card.textContent.includes('"--dns"') && card.textContent.includes("changed since it was trusted");
  buttonIn(card, "Trust exactly this…").fire("click");
  result.trustAsk = card.all().find(n => n.className === "confirm").textContent;
  result.beforeYes = calls.filter(c => c.method === "POST" && c.path.endsWith("/trust")).length;
  buttonIn(card, "Yes, trust it").fire("click"); await settle();
  result.trusted = calls.filter(c => c.path.endsWith("/projects/trust")).map(c => JSON.parse(c.body));
  // Logins: logging out is asked first; then the terminal, confirmed
  vm.runInContext('state.page = "logins"; terminalsOpened.length = 0;', context);
  answer = (method, path) => (path.endsWith("/identities") ? {} : []);
  clear_main();
  await vm.runInContext("pages.logins(__main)", context); await settle();
  buttonIn(__main_of(), "Log out…").fire("click");
  result.logoutBefore = vm.runInContext("terminalsOpened.length", context);
  buttonIn(__main_of(), "Yes, log out").fire("click"); await settle();
  result.logout = vm.runInContext("terminalsOpened", context);
  // MCP: an entry from an unsigned catalog is asked first
  vm.runInContext('state.page = "mcp";', context);
  calls.length = 0;
  answer = (method, path) => (path.endsWith("/mcp") && method === "GET" ? [{ name: "files", status: "stopped", serves: [], command: "img" }] : { output: "added" });
  clear_main();
  await vm.runInContext("pages.mcp(__main)", context); await settle();
  byId("mcp-entry").value = "hub:capa"; byId("mcp-unsigned").checked = true;
  byId("mcp-as").value = ""; byId("mcp-mounts").value = "/data=/home/u/data:rw"; byId("mcp-env").value = ""; byId("mcp-memory").value = ""; byId("mcp-pids").value = "";
  byId("mcp-catalogs").value = ""; byId("mcp-add-vpn").value = "";
  buttonIn(__main_of(), "Add the server").fire("click"); await settle();
  result.mcpAsk = (__main_of().all().find(n => n.className === "confirm") || { textContent: "" }).textContent;
  result.mcpBefore = calls.filter(c => c.method === "POST").length;
  buttonIn(__main_of(), "Yes, add it").fire("click"); await settle();
  result.mcpSent = calls.filter(c => c.method === "POST").map(c => [c.path, JSON.parse(c.body)]);
  console.log(JSON.stringify(result));
})().catch(e => { console.error(e.stack); process.exit(1); });
function __main_of() { return main; }
function clear_main() { main.children = []; }
"""


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_the_pages_of_the_clis_other_commands(tmp_path):
    """In node: the model of a new start (--llm, --model taken out, a server the agent cannot use disabled), a start
    refused for an untrusted project file offered for review, a project trusted with exactly the reviewed digest
    (asked first), a logout asked first and then confirmed in its terminal, an unsigned MCP entry asked first."""
    result = run_driver(tmp_path, PARITY_DRIVER, "parity")
    assert result["withModel"] == [["--mode", "high", "--llm", "gpu:qwen3", "--no-provider-defaults"],
                                   ["--mode", "high", "--model", "opus"],
                                   ["--allow-pypi", "--llm", "gpu", "--", "--model", "kept"]]
    values = {value: (disabled, text) for value, disabled, text in result["models"]}
    assert values[""][0] is False and "provider" in values[""][1]
    assert values["gpu"][0] is True and "Anthropic" in values["gpu"][1], "Claude Code cannot use a plain vLLM server"
    assert values["gpu:qwen3"][0] is True and values["gw"][0] is False
    opened, said, hint = result["noWorkspace"]
    assert opened == 0 and "Which workspace? Name the folder on ct1 the agent works on" in said
    assert "Required" in hint and "never taken instead" in hint
    assert result["namesWorkspace"] == [True, True, True, False, False, False]
    assert result["started"]["kind"] == "start" and result["started"]["agent"] == "claude"
    assert result["started"]["workspace"] == "/srv/w"
    assert json.loads(result["started"]["options"]) == ["--mode", "high", "--llm", "gw", "--no-provider-defaults"]
    # Review 5 L7: what the sandbox prints once it runs (after the launcher's "/work is" line) never raises it
    assert result["untrusted"] == ["/home/u/my web/.sandbox-ai.json", "/p/.sandbox-ai.json", None, None]
    assert result["review"] is True
    assert "/p/.sandbox-ai.json on ct1" in result["trustAsk"] and result["beforeYes"] == 0
    assert result["trusted"] == [{"path": "/p/.sandbox-ai.json", "digest": "b" * 64, "confirm": True}]
    assert result["logoutBefore"] == 0
    assert result["logout"] == [{"kind": "login", "host": "ct1", "agent": "claude", "logout": "1", "confirm": "1"}]
    assert "hub:capa on ct1" in result["mcpAsk"]
    assert "no trusted key signed" in result["mcpAsk"] and "/home/u/data writable" in result["mcpAsk"]
    assert result["mcpBefore"] == 0
    (path, sent), = result["mcpSent"]
    assert path == "/api/hosts/ct1/mcp" and sent["entry"] == "hub:capa" and sent["unsigned"] is True and sent["confirm"] is True
    assert sent["mounts"] == [{"target": "/data", "host": "/home/u/data", "mode": "rw"}]


def test_the_net_presets_are_the_launchers():
    spec_ = importlib.util.spec_from_file_location("launcher_for_web_tests", ROOT / "sandbox" / "agent_sandbox.py")
    launcher = importlib.util.module_from_spec(spec_)
    spec_.loader.exec_module(launcher)
    assert tuple(launcher.PRESETS) == w.NET_PRESETS
    assert "const NET_PRESETS = " + json.dumps(list(w.NET_PRESETS)) + ";" in (WEB / "app.js").read_text()


def test_the_mcp_catalog_and_the_activity_are_read_as_the_commands_print_them(logged_in):
    """mcp catalog has no --json: its lines are read (entry, network, title, published, signature); activity --json
    prints JSON lines."""
    logged_in.runner.answers[("mcp", "catalog")] = (0, "hub:binwalk  [none]  Binwalk firmware analysis  signed\n"
                                                       "c0:duck  [full duckduckgo.com]  DuckDuckGo search  (not published yet)  unsigned\n"
                                                       "garbage line\n", "sandbox-mcp: catalog not read: c1: 404\n")
    status, out = logged_in("POST", "/api/hosts/ct1/mcp-catalog", {})
    assert status == 200 and out["entries"] == [
        {"entry": "hub:binwalk", "prefix": "hub", "name": "binwalk", "network": "none", "title": "Binwalk firmware analysis",
         "published": True, "signature": "good"},
        {"entry": "c0:duck", "prefix": "c0", "name": "duck", "network": "full duckduckgo.com", "title": "DuckDuckGo search",
         "published": False, "signature": "none"}]
    assert out["problems"] == ["sandbox-mcp: catalog not read: c1: 404"]
    logged_in.runner.answers[("activity",)] = (0, '{"kind": "tool", "run": "r1"}\nnot json\n{"kind": "file"}\n', "")
    status, out = logged_in("POST", "/api/hosts/ct1/activity", {"tail": 10})
    assert status == 200 and out == {"events": [{"kind": "tool", "run": "r1"}, {"kind": "file"}]}


def test_a_project_is_trusted_through_the_real_wizard_only_as_reviewed(tmp_path, monkeypatch):
    """The web's review and trust against the real `sandbox-ai wizard trust` (a private HOME): the digest of the
    review trusts the file; a file changed after the review is refused, nothing trusted."""
    home = tmp_path / "home"
    project = home / "src" / "web"
    project.mkdir(parents=True)
    config = project / ".sandbox-ai.json"
    config.write_text(json.dumps({"version": 1, "agent": "codex", "options": ["--dns"]}))
    env = {"HOME": str(home), "XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state"),
           "XDG_DATA_HOME": str(tmp_path / "data"), "PATH": os.environ.get("PATH", "")}

    def runner(host, args, timeout=120, input=None):
        run = subprocess.run([sys.executable, "-I", str(ROOT / "sandbox" / "main.py"), *args], capture_output=True,
                             text=True, env=env, timeout=timeout)
        return run.returncode, run.stdout, run.stderr
    api = w.Api(runner)
    shown = api.dispatch("POST", "/api/hosts/local/projects/review", {"path": str(config)})
    assert shown["document"]["options"] == ["--dns"] and shown["trusted"] is False
    config.write_text(json.dumps({"version": 1, "agent": "codex", "options": ["--egress-all-allow"]}))
    with pytest.raises(w.Refused) as refused:
        api.dispatch("POST", "/api/hosts/local/projects/trust", {"path": str(config), "digest": shown["digest"],
                                                                 "confirm": True})
    assert refused.value.status == 502 and "not what was reviewed" in str(refused.value)
    assert api.dispatch("GET", "/api/hosts/local/projects", None) == [] or all(
        e["path"] != str(config) for e in api.dispatch("GET", "/api/hosts/local/projects", None))
    config.write_text(json.dumps({"version": 1, "agent": "codex", "options": ["--dns"]}))
    assert "Trusted." in api.dispatch("POST", "/api/hosts/local/projects/trust", {"path": str(config),
                                                                                  "digest": shown["digest"],
                                                                                  "confirm": True})["output"]
    assert api.dispatch("GET", "/api/hosts/local/projects", None) == [
        {"path": str(config), "digest": shown["digest"], "state": "trusted"}]


def test_the_starts_of_every_host_with_the_mcp_servers_that_do_not_run(logged_in):
    """GET /api/starts/all: list --all --json on every host (a fan-out of its own, beside the plain list)."""
    status, out = logged_in("GET", "/api/starts/all")
    assert status == 200 and out["starts"] == [] and out["errors"] == {}
    assert ("local", ["list", "--all", "--json"]) in logged_in.runner.calls
    assert ("local", ["list", "--json"]) not in logged_in.runner.calls


# --- a running start's MCP servers (mcp attach|detach --live) and its workspaces of a file or with a name --------

RESOURCES_DRIVER = FAKE_DOM + r"""
const confirmText = (node) => { const c = node.all().find(n => n.className === "confirm"); return c ? c.textContent : null; };
const posts = () => calls.filter(c => c.method !== "GET").map(c => [c.path, JSON.parse(c.body)]);
const byId = (id) => document.getElementById(id);
(async () => {
  const result = {};
  vm.runInContext('state.me = { user: "admin", csrf: "c", terminals: true }; state.host = "ct1"; state.page = "logs"; state.hosts = [{ name: "ct1", kind: "ssh", ssh: "root@ct1", port: 22 }];', context);
  let attached = { message: "web reaches the MCP server search now, kept for its later starts. Codex reads ...", agent_restart: "required", attached: "live" };
  answer = (method, path, sent) => {
    if (path === "/api/hosts/ct1/workspaces/web" && method === "GET") return [
      { host: "/srv/web", sandbox: "/work", mode: "rw", source: "start", kind: "dir" },
      { host: "/home/u/notes.md", sandbox: "/workspaces/notes", mode: "ro", source: "added", kind: "file", named: true, state: "attached" },
      { host: "/home/u/lib", sandbox: "/workspaces/lib", mode: "ro", source: "added", kind: "dir", named: false, state: "attached" }];
    if (path === "/api/hosts/ct1/starts") return [{ name: "other", id: "o-1", mcp_running: ["nope"] },
      { name: "web", id: "web-1", kind: "session", state: "running", directory: "/d", mcp: ["files", "later"], mcp_running: ["files", "helper-codex-0123abcd"] }];
    if (path === "/api/hosts/ct1/mcp") return [{ name: "files" }, { name: "search" }, { name: "-bad" }, { name: 7 }];
    if (path.endsWith("/mcp-live")) return attached;
    if (path.endsWith("/mcp-live/remove")) return { message: "web no longer reaches it", detached: "live" };
    if (method === "GET") return path.includes("/limits/") ? {} : [];
    return { output: "ok" };
  };
  vm.runInContext('resourcesCard(__main, { name: "web", id: "web-1", host: "ct1", state: "running", kind: "session", directory: "/d", keep: true })', context);
  await settle();
  const card = byId("resources-card");
  const tables = card.all().filter(n => n.tagName === "table");
  const rowsOf = (table) => table.all().filter(n => n.tagName === "tr").slice(1).map(r => r.children.slice(0, -1).map(c => c.textContent));
  result.workspaces = rowsOf(tables[0]);
  result.mcpRows = tables[1].all().filter(n => n.tagName === "tr").slice(1).map(r => r.children[0].textContent);
  result.choices = byId("mcp-live-server").children.map(o => [o.value, o.textContent]);

  // a workspace: a file with a name of its own, read-only at once; a bad name is refused here
  const add = button(card, "Add workspace");
  byId("ws-dir").value = "/home/u/todo.txt"; byId("ws-name").value = "bad name"; byId("ws-ro").checked = true;
  add.fire("click"); await settle();
  result.badName = [posts().length, toasts().some(t => t.includes("1-64 letters"))];
  byId("ws-name").value = "todo.v2";
  button(card, "Add workspace").fire("click"); await settle();
  result.readOnlyPost = posts();
  calls.length = 0;
  byId("ws-dir").value = "/home/u/src/app"; byId("ws-name").value = ""; byId("ws-ro").checked = false;
  button(card, "Add workspace").fire("click"); await settle();
  result.writableAsk = [confirmText(card), posts().length];
  button(card, "Yes, writable").fire("click"); await settle();
  result.writablePost = posts();

  // an MCP server now: asked first naming host and start; then its message and the restart of the agent offered
  calls.length = 0;
  byId("mcp-live-server").value = "search";
  button(card, "Add now…").fire("click");
  result.addAsk = [confirmText(card), posts().length];
  button(card, "Yes, add it now").fire("click"); await settle();
  result.addPost = posts();
  result.message = card.textContent.includes("web reaches the MCP server search now");
  const restart = button(card, "Restart the agent now (the conversation goes on)");
  result.restartOffered = Boolean(restart);
  calls.length = 0;
  restart.fire("click");
  result.restartAsk = [confirmText(card), posts().length];
  button(card, "Yes, restart it").fire("click"); await settle();
  result.restartPost = posts();
  result.restartGone = !card.all().some(n => n.tagName === "button" && n.textContent === "Restart the agent now (the conversation goes on)" && !n.parent.hidden);
  // a helper agent; an agent that sees it at once: no restart offered
  calls.length = 0;
  attached = { message: "web reaches it now", attached: "live" };
  byId("mcp-live-server").value = "helper:opencode";
  button(card, "Add now…").fire("click");
  result.helperAsk = confirmText(card);
  button(card, "Yes, add it now").fire("click"); await settle();
  result.helperPost = posts();
  result.noRestart = !button(card, "Restart the agent now (the conversation goes on)");

  // Remove: asked first (it cuts the agent's connections); a helper by its agent, a server by its name
  calls.length = 0;
  const mcpTable = card.all().filter(n => n.tagName === "table")[1];
  const rowOf = (text) => mcpTable.all().filter(n => n.tagName === "tr").find(r => r.children[0].textContent.startsWith(text));
  button(rowOf("helper-codex"), "Remove").fire("click");
  result.removeAsk = [confirmText(rowOf("helper-codex")), posts().length];
  button(rowOf("helper-codex"), "Yes, detach it").fire("click"); await settle();
  button(rowOf("files"), "Remove").fire("click");
  button(rowOf("files"), "Yes, detach it").fire("click"); await settle();
  result.removePosts = posts();
  vm.runInContext('setLanguage("de")', context);
  result.german = vm.runInContext('[t("Add an MCP server"), t("{agent} as a helper", { agent: "codex" }), t("file")]', context);
  console.log(JSON.stringify(result));
})().catch(e => { console.error(e.stack); process.exit(1); });
"""


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_a_running_starts_mcp_servers_and_workspaces_in_its_resources_card(tmp_path):
    """The Resources card: workspaces with their kind and name, a file or folder added with a name of its own (a bad
    name refused in the page, a writable one asked first); MCP servers it reaches now, one added (a server of that
    host or a helper agent) and removed, each asked first naming host and start, the command's message shown and a
    restart of the agent offered (asked first too) when the agent does not see it at once."""
    result = run_driver(tmp_path, RESOURCES_DRIVER, "resources")
    assert result["workspaces"] == [["/work", "folder", "-", "rw", "start", "/srv/web"],
                                    ["/workspaces/notes", "file", "notes (chosen)", "ro", "added", "/home/u/notes.md"],
                                    ["/workspaces/lib", "folder", "lib", "ro", "added", "/home/u/lib"]]
    assert result["mcpRows"] == ["files", "helper-codex-0123abcd (codex as a helper)"], "its own row, not another's"
    assert result["choices"] == [["search", "search"], ["helper:claude", "claude as a helper"],
                                 ["helper:codex", "codex as a helper"], ["helper:opencode", "opencode as a helper"]]
    assert result["badName"] == [0, True]
    assert result["readOnlyPost"] == [["/api/hosts/ct1/workspaces/web",
                                       {"path": "/home/u/todo.txt", "read_only": True, "name": "todo.v2"}]]
    ask, before = result["writableAsk"]
    assert before == 0 and all(part in ask for part in ("web", "/home/u/src/app", "ct1", "/workspaces/app")), ask
    assert result["writablePost"] == [["/api/hosts/ct1/workspaces/web",
                                       {"path": "/home/u/src/app", "read_only": False, "confirm": True}]]
    ask, before = result["addAsk"]
    assert before == 0 and all(part in ask for part in ("search", "web", "ct1", "now")), ask
    assert result["addPost"] == [["/api/hosts/ct1/starts/web/mcp-live", {"server": "search", "confirm": True}]]
    assert result["message"] and result["restartOffered"]
    ask, before = result["restartAsk"]
    assert before == 0 and "web on ct1" in ask and "continues its conversation" in ask, ask
    assert result["restartPost"] == [["/api/hosts/ct1/starts/web/resume", {"restart": True, "confirm": True}]]
    assert result["restartGone"]
    assert "opencode as a helper" in result["helperAsk"] or "opencode" in result["helperAsk"]
    assert "ct1" in result["helperAsk"] and "web" in result["helperAsk"]
    assert result["helperPost"] == [["/api/hosts/ct1/starts/web/mcp-live", {"helper": "opencode", "confirm": True}]]
    assert result["noRestart"]
    ask, before = result["removeAsk"]
    assert before == 0 and all(part in ask for part in ("codex", "web", "ct1", "connections")), ask
    assert result["removePosts"] == [["/api/hosts/ct1/starts/web/mcp-live/remove", {"helper": "codex", "confirm": True}],
                                     ["/api/hosts/ct1/starts/web/mcp-live/remove", {"server": "files", "confirm": True}]]
    assert result["german"] == ["Einen MCP-Server hinzufügen", "codex als Helfer", "Datei"]


# --- round 4: error codes, teams, images, the privacy check's answer, a bound install, a kept allow ---------------

def test_the_common_refusals_say_their_code_for_the_pages_own_words(logged_in, monkeypatch):
    """{"error", "code", "params"}: the page shows its own (German) text for a known code with the English message
    below it. A refusal naming a code the page cannot know is a bug, never a pass."""
    hosts = w.module("hosts")
    hosts.save({"ct1": hosts.check("ct1", {"ssh": "root@192.0.2.10"})})
    status, out = logged_in("GET", "/api/hosts/Bad;id/starts")
    assert status == 400 and out["code"] == "no-host" and out["params"] == {"host": "Bad;id"}
    status, out = logged_in("DELETE", "/api/hosts/ct9", {"confirm": True})
    assert status == 404 and out["code"] == "no-host" and out["params"] == {"host": "ct9"}
    status, out = logged_in("POST", "/api/hosts/ct1/starts/web/stop", {})
    assert status == 400 and out["code"] == "confirm-needed" and out["confirm"] == "start-stop"
    assert out["params"] == {"action": "start-stop", "detail": ""}
    status, out = logged_in("POST", "/api/hosts/ct1/workspaces/web", {"path": "relative", "read_only": True})
    assert status == 400 and out["code"] == "bad-path" and out["params"] == {"what": "path"}
    status, out = logged_in("POST", "/api/hosts/ct1/starts/-x/pause", {})
    assert status == 400 and out["code"] == "bad-name"
    status, out = logged_in("POST", "/api/hosts/ct1/presets/tor/check", {"values": {"Bad key": "x"}})
    assert status == 400 and out["code"] == "unknown-preset-value"
    assert out["params"] == {"preset": "tor", "value": "Bad key"}
    status, out = logged_in("GET", "/api/hosts/ct1/presets/-x")
    assert status == 400 and out["code"] == "no-preset"
    logged_in.runner.answers[("doctor",)] = (255, "", "ssh: connect to host 192.0.2.10 port 22: No route to host")
    status, out = logged_in("GET", "/api/hosts/ct1/doctor")
    assert status == 502 and out["code"] == "host-unreachable" and out["params"]["host"] == "ct1"
    logged_in.runner.answers[("logs",)] = (3, "", "no logs here")
    status, out = logged_in("GET", "/api/hosts/ct1/logs")
    assert status == 502 and out["code"] == "command-failed"
    assert out["params"] == {"host": "ct1", "command": "sandbox-ai logs", "status": "3"}
    assert "no logs here" in out["error"]
    logged_in.runner.answers[("vpn", "check")] = (124, "", "")
    status, out = logged_in("POST", "/api/hosts/ct1/vpn/home/check", {})
    assert status == 502 and out["code"] == "timeout"
    svc = logged_in.svc
    status, _, body = svc.handle("GET", "/api/hosts/ct1/starts", HEADERS, b"", "192.0.2.1")
    assert status == 401 and json.loads(body)["code"] == "login-needed"
    with pytest.raises(KeyError):
        w.Refused(400, "x", code="no-such-code")
    script = (WEB / "app.js").read_text()
    table = script.split("const ERROR_TEXTS = {", 1)[1].split("\n};", 1)[0]
    assert set(re.findall(r'^  "([a-z-]+)":', table, re.M)) == set(w.ERROR_CODES), "the page knows every code"


def test_an_allow_kept_for_later_starts_is_also_asked_as_keeping(logged_in, monkeypatch):
    """net allow --keep widens the run now (net-allow) and writes the project's file for every later start
    (net-keep): both are checked."""
    asked, real = [], w.confirmed
    monkeypatch.setattr(w, "confirmed", lambda body, action, detail=None: (asked.append(action),
                                                                          real(body, action, detail)))
    status, out = logged_in("POST", "/api/hosts/ct1/net/web", {"op": "allow", "dns": True, "keep": True,
                                                               "confirm": True})
    assert status == 200 and asked == ["net-allow", "net-keep"]
    assert logged_in.runner.calls[-1] == ("ct1", ["net", "allow", "web", "--dns", "--keep"])
    asked.clear()
    status, out = logged_in("POST", "/api/hosts/ct1/net/web", {"op": "allow", "dns": True, "confirm": True})
    assert status == 200 and asked == ["net-allow"]
    routes = {pattern: actions for _, pattern, _, actions in w.Api(FakeRunner()).routes}
    assert routes[r"/api/hosts/(?P<host>[^/]+)/net/(?P<name>[^/]+)"] == ("net-allow", "net-keep")


def test_teams_are_listed_shown_posted_to_stopped_and_deleted(logged_in):
    """team list has no --json: its lines are read; a task goes after "--" (never an option); a team's name as
    team.py takes it; stop and delete are confirmed (CONFIRM_CASES)."""
    logged_in.runner.answers[("team", "list")] = (0, "review               claude, codex            /srv/project\n"
                                                     "trio                 opencode, claude, codex  /home/u/my src\n"
                                                     "garbage\n", "")
    status, out = logged_in("GET", "/api/hosts/ct1/teams")
    assert status == 200 and out == [{"name": "review", "members": ["claude", "codex"], "workspace": "/srv/project"},
                                     {"name": "trio", "members": ["opencode", "claude", "codex"],
                                      "workspace": "/home/u/my src"}]
    status, out = logged_in("GET", "/api/hosts/ct1/teams/review")
    assert status == 200 and logged_in.runner.calls[-1] == ("ct1", ["team", "show", "review", "--json"])
    status, out = logged_in("POST", "/api/hosts/ct1/teams/review/post", {"agent": "codex",
                                                                         "task": "--help me\n  please"})
    assert status == 200 and logged_in.runner.calls[-1] == ("ct1", ["team", "post", "--", "review", "codex",
                                                                    "--help me\n  please"])
    for path, body in (("/api/hosts/ct1/teams/Review/post", {"agent": "codex", "task": "x"}),
                       ("/api/hosts/ct1/teams/-x/post", {"agent": "codex", "task": "x"}),
                       ("/api/hosts/ct1/teams/review/post", {"agent": "bash", "task": "x"}),
                       ("/api/hosts/ct1/teams/review/post", {"agent": "codex", "task": "  "}),
                       ("/api/hosts/ct1/teams/review/post", {"agent": "codex", "task": "a\x1b[2Jb"})):
        calls = len(logged_in.runner.calls)
        status, out = logged_in("POST", path, body)
        assert status == 400 and len(logged_in.runner.calls) == calls, (path, body)
    logged_in.runner.answers[("team", "show")] = (1, "", "sandbox-ai team: no team nope")
    status, out = logged_in("GET", "/api/hosts/ct1/teams/nope")
    assert status == 502 and "no team nope" in out["error"]


def test_an_images_extension_is_changed_checked_and_removed_as_the_command_takes_it(logged_in):
    """images extend: each package as --add-KIND=PKG, a host file as --add-file=SRC:TARGET[:MODE] (asked first:
    image-file), a fragment on standard input (--fragment=-); images check answers with its findings even when they
    are errors (exit status 1); remove keeps the image when asked."""
    status, out = logged_in("POST", "/api/hosts/ct1/images/data/extend", {
        "add_apt": ["ffmpeg", "jq=1.7"], "remove_pip": ["ruff"], "add_npm": ["@scope/tool@1.2"],
        "fragment": "RUN echo hi\n", "remove_files": ["/usr/local/bin/old"]})
    assert status == 200, out
    assert logged_in.runner.calls[-1] == ("ct1", ["images", "extend", "data", "--add-apt=ffmpeg", "--add-apt=jq=1.7",
                                                  "--add-npm=@scope/tool@1.2", "--remove-pip=ruff",
                                                  "--remove-file=/usr/local/bin/old", "--fragment=-", "--json"])
    inputs = []
    logged_in.svc.api.runner = lambda host, args, timeout=120, input=None: (inputs.append(input), (0, "{}", ""))[1]
    logged_in("POST", "/api/hosts/ct1/images/data/extend", {"fragment": "RUN echo hi\n"})
    logged_in("POST", "/api/hosts/ct1/images/data/check", {"fragment": "FROM x\n"})
    assert inputs == ["RUN echo hi\n", "FROM x\n"], "a fragment only ever on standard input"
    logged_in.svc.api.runner = logged_in.runner
    findings = {"findings": [{"severity": "error", "message": "FROM"}], "containerfile": "FROM x"}
    logged_in.runner.answers[("images", "check")] = (1, json.dumps(findings), "")
    status, out = logged_in("POST", "/api/hosts/ct1/images/data/check", {})
    assert status == 200 and out["findings"][0]["message"] == "FROM", "errors found are its answer"
    logged_in.runner.answers[("images", "extend")] = (1, "", "sandbox-ai images: error: line 1: FROM is not allowed")
    status, out = logged_in("POST", "/api/hosts/ct1/images/data/extend", {"add_apt": ["jq"]})
    assert status == 502 and "FROM is not allowed" in out["error"], "a refused change is no answer"
    for body in ({"add_apt": ["--privileged"]}, {"add_apt": ["a b"]},
                 {"add_files": [{"source": "rel", "target": "/t"}]},
                 {"add_files": [{"source": "/a:b", "target": "/t"}], "confirm": True},
                 {"add_files": [{"source": "/a", "target": "/t", "mode": "4755"}], "confirm": True},
                 {"add_files": [{"source": "/a/../etc/shadow", "target": "/t"}], "confirm": True},
                 {"fragment": "x", "clear_fragment": True}, {}, {"fragment": "x" * (65 << 10)}):
        calls = len(logged_in.runner.calls)
        status, out = logged_in("POST", "/api/hosts/ct1/images/data/extend", body)
        assert status == 400 and len(logged_in.runner.calls) == calls, body
    for path in ("/api/hosts/ct1/images/--all/extend", "/api/hosts/ct1/images/Data/remove"):
        assert logged_in("POST", path, {"add_apt": ["jq"], "confirm": True})[0] == 400
    assert logged_in("POST", "/api/hosts/ct1/images-new", {"name": "codex", "from": "codex"})[0] == 400
    assert logged_in("POST", "/api/hosts/ct1/images-recipe", {"recipe": "evil", "from": "codex"})[0] == 400
    status, out = logged_in("POST", "/api/hosts/ct1/images/data/remove", {"keep_image": True, "confirm": True})
    assert status == 200 and logged_in.runner.calls[-1] == ("ct1", ["images", "remove", "data", "--keep-image"])


def test_the_privacy_check_is_an_all_clear_only_for_what_it_checked(logged_in):
    """privacy-check --json names each repository, checked or refused ("checked": false, why in "refused"; exit
    status 2): passed on as it is; a list that names none, or a failure, is never an empty all-clear."""
    path = "/api/hosts/ct1/privacy-check"
    refused = [{"repository": "/r", "checked": False, "refused": "no rules", "findings": []}]
    logged_in.runner.answers[("privacy-check",)] = (2, json.dumps(refused),
                                                    "sandbox-ai privacy-check: REFUSED: no rules")
    assert logged_in("POST", path, {"paths": ["/r"]}) == (200, refused)
    logged_in.runner.answers[("privacy-check",)] = (2, "[]", "sandbox-ai privacy-check: REFUSED: no rules")
    status, out = logged_in("POST", path, {"paths": ["/r"]})
    assert status == 502 and out["code"] == "nothing-checked" and "REFUSED" in out["error"]
    logged_in.runner.answers[("privacy-check",)] = (0, "[]", "")
    assert logged_in("POST", path, {"paths": ["/r"]})[1]["code"] == "nothing-checked"
    logged_in.runner.answers[("privacy-check",)] = (3, json.dumps(refused), "Traceback ...")
    assert logged_in("POST", path, {"paths": ["/r"]})[1]["code"] == "command-failed"
    # an older sandbox-ai: exit status 2 with the checked ones only: none of them is an all-clear
    logged_in.runner.answers[("privacy-check",)] = (2, json.dumps([{"repository": "/a", "scanned": 3, "findings": []}]),
                                                    "sandbox-ai privacy-check: REFUSED: /b: no rules")
    status, out = logged_in("POST", path, {"paths": ["/a", "/b"]})
    assert status == 200 and out[0]["checked"] is False and "REFUSED" in out[0]["refused"]
    found = [{"repository": "/a", "checked": True, "refused": None, "scanned": 3,
              "findings": [{"path": "k", "id": "key", "severity": "block", "reason": "a key"}]}]
    logged_in.runner.answers[("privacy-check",)] = (1, json.dumps(found), "")
    assert logged_in("POST", path, {"paths": ["/a"]}) == (200, found)


def test_a_host_install_goes_where_the_question_said_and_one_at_a_time(home):
    """The confirmation names the SSH target with its port; "expected" binds it: a host whose entry changed since
    (another page, the command line) is not installed (409). The terminal install waits for the route's."""
    hosts = w.module("hosts")
    hosts.save({"ct1": hosts.check("ct1", {"ssh": "admin@192.0.2.10", "port": 2222})})
    runner = FakeRunner({("hosts", "install"): (0, json.dumps({"name": "ct1", "after": "0.1.80"}), "")})
    api = w.Api(runner)
    with pytest.raises(w.Refused) as refused:
        api.dispatch("POST", "/api/hosts/ct1/install", {"mode": "user"})
    assert "ct1 (admin@192.0.2.10:2222): for the SSH user, without sudo" in str(refused.value)
    with pytest.raises(w.Refused) as refused:
        api.dispatch("POST", "/api/hosts/ct1/install", {"mode": "user", "confirm": True,
                                                        "expected": {"ssh": "admin@192.0.2.10", "port": 22}})
    assert refused.value.status == 409 and refused.value.code == "changed-meanwhile" and runner.calls == []
    with pytest.raises(w.Refused):
        api.dispatch("POST", "/api/hosts/ct1/install", {"mode": "user", "confirm": True, "expected": "admin@x"})
    assert api.dispatch("POST", "/api/hosts/ct1/install", {"mode": "user", "confirm": True, "expected": {
        "ssh": "admin@192.0.2.10", "port": 2222}})["after"] == "0.1.80"
    assert runner.calls == [("local", ["hosts", "install", "ct1", "--user", "--from-here", "--json"])]
    # the terminal where sudo asks: refused while the route's install runs there
    secret = add_user()
    svc = w.Service({"localhost:8443"}, api=api)
    _, headers, body = login(svc, secret)
    api.installing["ct1"] = time.monotonic() - 3
    query = "kind=install&host=ct1&mode=system&confirm=1&csrf=" + body["csrf"]
    with pytest.raises(w.Refused) as refused:
        svc.websocket("/api/terminal?" + query, {**HEADERS, "Cookie": cookie_of(headers)}, "192.0.2.1")
    assert refused.value.status == 409 and refused.value.code == "busy"
    del api.installing["ct1"]
    argv, _ = svc.websocket("/api/terminal?" + query, {**HEADERS, "Cookie": cookie_of(headers)}, "192.0.2.1")
    assert "install" in argv


def test_the_help_is_the_apps_exported_and_current():
    """sandbox/web/help.json is what scripts/export_web_help.py makes of the app's help_content.py now (its
    sections about sandbox-ai itself, both languages); every section a page's (?) names is in it; it is served."""
    spec_ = importlib.util.spec_from_file_location("export_web_help", ROOT / "scripts" / "export_web_help.py")
    script = importlib.util.module_from_spec(spec_)
    spec_.loader.exec_module(script)
    shipped = (WEB / "help.json").read_text(encoding="utf-8")
    assert shipped == script.text(), "help.json is out of date: run python3 scripts/export_web_help.py"
    sections = json.loads(shipped)["sections"]
    ids = {s["id"] for s in sections}
    assert {"sessions", "approvals", "presets", "teams", "llm", "vpn-tor", "hosts"} <= ids
    for s in sections:
        assert s["title"]["en"] and s["title"]["de"] and s["body"]["en"] and s["body"]["de"], s["id"]
        assert set(s["see"]) <= ids
    app = (WEB / "app.js").read_text()
    named = set(re.findall(r'"([a-z-]+)"', app.split("const HELP_FOR = {", 1)[1].split("\n};", 1)[0]))
    assert named and named <= ids, sorted(named - ids)
    assert w.STATIC_FILES["help.json"] == "application/json"


def test_the_demo_devices_port_is_its_default():
    spec_ = importlib.util.spec_from_file_location("demo_for_web", ROOT / "sandbox" / "demo_device.py")
    demo = importlib.util.module_from_spec(spec_)
    spec_.loader.exec_module(demo)
    app = (WEB / "app.js").read_text()
    assert f'id: "demo-port", type: "number", min: 1024, max: 65535, placeholder: "{demo.DEFAULT_PORT}"' in app
    assert "-mm sonnet" in app and "-m sonnet" not in app, "the model's flag is -mm (-m is --mount)"


ROUND4_DRIVER = FAKE_DOM + r"""
const buttonIn = (node, text) => node.all().find(n => n.tagName === "button" && n.textContent === text);
const byId = (id) => body.all().find(n => n.id === id);
const confirmText = (node) => { const c = node.all().find(n => n.className === "confirm"); return c ? c.textContent : null; };
const posts = () => calls.filter(c => c.method !== "GET").map(c => [c.method, c.path, JSON.parse(c.body)]);
const reset = () => { vm.runInContext("clear(__main); terminalsOpened.length = 0", context); calls.length = 0; };
(async () => {
  const result = {};
  vm.runInContext('state.me = { user: "admin", csrf: "c", terminals: true }; state.host = "ct1"; state.hosts = [{ name: "local", kind: "local" }, { name: "ct1", kind: "ssh", ssh: "root@ct1" }]; var terminalsOpened = []; openTerminal = (params, title) => { terminalsOpened.push(params); };', context);
  // 1. the start form: its host, presets by category for the agent, one field per value, checked before it starts
  let checkAnswer = { ok: false, output: "missing: tor: no VPN profile anon\nmissing: preset tor is for claude" };
  const presets = [{ id: "tor", title: "Everything through Tor", category: "privacy", agents: ["claude", "codex", "opencode"] },
    { id: "ollama", title: "Ollama", category: "llm", agents: ["claude", "codex", "opencode"] },
    { id: "sand-chess", title: "Sand chess", category: "fun", agents: ["claude", "opencode"] },
    { id: "software-analysis", title: "Software analysis", category: "analysis", agents: ["claude", "codex", "opencode"] }];
  answer = (method, path, sent) => {
    if (path.endsWith("/starts")) return [{ name: "web", agent: "claude", kind: "session", state: "running", keep: true, home: "kept", directory: "/d", attachable: true }];
    if (path.endsWith("/presets")) return presets;
    if (path.endsWith("/presets/tor")) return { id: "tor", description: "Every connection through Tor", values: { tor: { label: "Tor profile (sandbox-ai vpn list)", kind: "vpn" } }, notes: ["a note"] };
    if (path.endsWith("/presets/ollama")) return { id: "ollama", values: { llm: { label: "Ollama server", kind: "llm", default: "ollama" } } };
    if (path.endsWith("/presets/software-analysis")) return { id: "software-analysis", values: { samples: { label: "Folder with the samples", kind: "folder" } }, mcp: ["hub:capa", "hub:yara"] };
    if (path.endsWith("/check")) return checkAnswer;
    if (path.endsWith("/vpn")) return [{ profile: "anon", type: "tor" }, { profile: "home", type: "wireguard" }];
    if (path.endsWith("/mcp")) return [{ name: "capa" }];
    if (path.endsWith("/llm")) return [{ name: "ollama", kind: "ollama", agents: ["claude", "codex", "opencode"] }, { name: "gpu", kind: "vllm", agents: ["codex", "opencode"] }];
    if (path.includes("/models")) return [];
    return { output: "ok" };
  };
  vm.runInContext('state.page = "starts"; state.startsHost = "ct1";', context);
  await vm.runInContext("pages.starts(__main)", context); await settle();
  result.formHosts = byId("start-host").children.map(o => o.value);
  const ticked = (box) => (box.checked === undefined ? "checked" in box.attributes : box.checked);  // as a browser has it
  result.only = [ticked(byId("start-only-server")), byId("start-only-server").disabled];
  result.example = byId("options").getAttribute("placeholder");
  const groups = () => byId("preset").children.filter(n => n.tagName === "optgroup").map(g => [g.getAttribute("label"), g.children.map(o => o.value)]);
  result.groupsClaude = groups();
  byId("agent").value = "codex"; byId("agent").fire("change");
  result.groupsCodex = groups();
  byId("preset").value = "tor"; byId("preset").fire("change"); await settle();
  result.torChoices = byId("preset-value-tor").children.map(o => o.value);
  byId("preset-value-tor").value = "anon";
  byId("options").value = "--mode high"; byId("workspace").value = "/srv/w";
  buttonIn(main, "Start in a terminal").fire("click"); await settle();
  result.notReady = [vm.runInContext("terminalsOpened.length", context), byId("start-status").textContent];
  checkAnswer = { ok: true, output: "tor: ready" };
  buttonIn(main, "Start in a terminal").fire("click"); await settle();
  result.checked = posts().filter(p => p[1].endsWith("/check")).map(p => [p[1], p[2]]);
  result.started = vm.runInContext("terminalsOpened.map(p => [p.host, p.agent, JSON.parse(p.options)])", context);
  byId("preset").value = "software-analysis"; byId("preset").fire("change"); await settle();
  result.mcp = main.all().filter(n => n.tagName === "li").map(n => [n.className, n.textContent]);
  result.samples = Boolean(byId("preset-value-samples"));
  byId("preset").value = "ollama"; byId("preset").fire("change"); await settle();
  result.ollama = [byId("preset-value-llm").value, byId("start-model").disabled];
  byId("preset").value = ""; byId("preset").fire("change"); await settle();
  byId("start-model").value = "gpu"; byId("start-model").fire("change");
  result.onlyWithServer = [ticked(byId("start-only-server")), byId("start-only-server").disabled];
  calls.length = 0;
  byId("start-host").value = "local"; byId("start-host").fire("change"); await settle();
  result.otherHost = [main.all().find(n => n.tagName === "h2" && n.textContent.startsWith("New start")).textContent,
    [...new Set(calls.map(c => c.path.split("/")[3]))]];
  // 2. a start's card opens right below its row, its heading focused
  const rowOf = (name) => body.all().find(n => n.attributes["data-name"] === name);
  buttonIn(rowOf("web"), "History").fire("click"); await settle();
  const tbody = rowOf("web").parent;
  const at = tbody.children.indexOf(rowOf("web"));
  result.card = [tbody.children[at + 1].className, tbody.children[at + 1].textContent.startsWith("History of web on ct1"), globalThis.focused && globalThis.focused.tagName];
  result.rowLabels = rowOf("web").all().filter(n => n.tagName === "button").map(b => b.getAttribute("aria-label"));
  buttonIn(tbody.children[at + 1], "Close").fire("click");
  result.closed = tbody.children.some(r => r.className === "card-row");
  // 3. LLM and VPN rows: the row's host, named for screen readers; Remove says remove
  reset();
  answer = (method, path) => (path.endsWith("/llm") && method === "GET" ? [{ name: "gw", kind: "vllm", url: "https://gw.example.org" }] : path.endsWith("/vpn") && method === "GET" ? [{ profile: "anon", type: "tor" }] : { ok: true, output: "fine" });
  vm.runInContext('state.page = "llm"; state.host = "ct1";', context);
  await vm.runInContext("pages.llm(__main)", context); await settle();
  vm.runInContext('state.host = "local";', context);  // the header's host changed, this page not drawn anew
  for (const label of ["Check", "Models", "Show"]) { buttonIn(main, label).fire("click"); await settle(); }
  result.llmPaths = calls.filter(c => c.path.includes("/llm/gw")).map(c => c.path);
  result.llmLabels = main.all().filter(n => n.tagName === "button" && (n.getAttribute("aria-label") || "").includes("gw")).map(b => b.getAttribute("aria-label"));
  buttonIn(main, "Remove").fire("click");
  result.llmRemove = confirmText(main) + "|" + main.all().filter(n => n.tagName === "button").map(b => b.textContent).join(",");
  reset();
  vm.runInContext('state.page = "vpn"; state.host = "ct1";', context);
  await vm.runInContext("pages.vpn(__main)", context); await settle();
  vm.runInContext('state.host = "local";', context);
  buttonIn(main, "Check").fire("click"); await settle();
  result.vpnPaths = calls.filter(c => c.method === "POST").map(c => c.path);
  // a replacement is asked first, naming it
  byId("vpn-name").value = "anon"; byId("vpn-kind").value = "tor"; byId("vpn-kind").fire("change"); byId("vpn-force").checked = true;
  calls.length = 0;
  buttonIn(main, "Add profile").fire("click");
  result.vpnForce = [confirmText(main), posts().length];
  // 4. the one live region: a notice has no role; an error is said there; a known code in the page's words
  result.notice = vm.runInContext('(() => { const n = notice("it failed", "error"); return [n.getAttribute("role") || null, document.getElementById("live").textContent]; })()', context);
  result.codes = vm.runInContext('(() => { const e = new ApiError(404, "no such host", null, "no-host", { host: "ct9" }); setLanguage("de"); const de = errorNotice(e).textContent; setLanguage("en"); return [errorNotice(e).textContent, de, errorText(new ApiError(500, "boom")), errorText(new ApiError(400, "x", null, "unknown-code"))]; })()', context);
  // 5. a slow page never draws over the page shown since
  reset();
  let release = null;
  answer = (method, path) => (path.endsWith("/doctor") ? new Promise(resolve => { release = () => resolve([]); }) : path.endsWith("/llm") ? [] : {});
  vm.runInContext('state.host = "ct1"; show("doctor"); show("llm");', context); await settle();
  release(); await settle();
  result.guard = main.children.filter(n => n.tagName === "h1").map(n => n.textContent);
  // 6. teams: listed, shown below their row, posted to, stopped after a question, started in a terminal
  reset();
  answer = (method, path, sent) => {
    if (path.endsWith("/teams")) return [{ name: "review", members: ["claude", "codex"], workspace: "/srv/p" }];
    if (path.endsWith("/teams/review") && method === "GET") return { name: "review", members: ["claude", "codex"], lead: "claude", workspace: "/srv/p",
      states: { claude: "running", codex: "stopped" }, board: { tasks: [{ id: 1, from: "claude", to: "codex", state: "open", task: "write the tests" }] } };
    return { output: "sandbox-ai team: done" };
  };
  vm.runInContext('state.page = "teams"; state.host = "ct1";', context);
  await vm.runInContext("pages.teams(__main)", context); await settle();
  const teamRow = main.all().find(n => n.attributes["data-team"] === "review");
  buttonIn(teamRow, "Show").fire("click"); await settle();
  const teamBody = teamRow.parent, teamAt = teamBody.children.indexOf(teamRow);
  const teamCard = byId("team-card");
  result.teamCard = [teamBody.children[teamAt + 1].children[0].children[0] === teamCard, teamCard.textContent.includes("write the tests"),
    teamCard.all().filter(n => n.tagName === "button").map(b => b.getAttribute("aria-label") || b.textContent).filter(x => x.startsWith("Attach"))];
  byId("team-post-agent").value = "codex"; byId("team-post-task").value = "  review the docs  ";
  buttonIn(teamCard, "Post").fire("click"); await settle();
  buttonIn(teamRow, "Stop…").fire("click");
  result.teamStop = [confirmText(teamRow), posts().filter(p => p[1].endsWith("/stop")).length];
  buttonIn(teamRow, "Yes, stop it").fire("click"); await settle();
  result.teamPosts = posts();
  byId("team-name").value = "duo"; byId("team-lead").value = "opencode"; byId("team-agent-codex").checked = true;
  byId("team-workspace").value = "/srv/x"; byId("team-options").value = "--mode high";
  buttonIn(main, "Start the team in a terminal").fire("click");
  result.teamStart = vm.runInContext("terminalsOpened.slice()", context);
  // 7. images: the variants in the build list (their "target"), a host file asked first, a removal asked first
  reset();
  answer = (method, path) => (path.endsWith("/images") ? [{ agent: "claude", target: "claude", variant: false, extended: true, status: "current" },
    { agent: "codex", target: "data", variant: true, extended: true, status: "not-built" }] : path.endsWith("/extend") ? { changes: ["added file /usr/local/bin/t"], findings: [] } : { output: "removed" });
  vm.runInContext('state.page = "images"; state.host = "ct1";', context);
  await vm.runInContext("pages.images(__main)", context); await settle();
  result.buildTargets = byId("build-target").children.map(o => o.value);
  byId("image-target").value = "data"; byId("image-add-files").value = "/home/u/t.sh:/usr/local/bin/t:0755"; byId("image-add-apt").value = "jq";
  buttonIn(main, "Save the changes…").fire("click");
  result.imageAsk = [confirmText(main), posts().length];
  byId("image-target").value = "claude";  // changed while asked: the question's image is the one changed
  buttonIn(main, "Yes, add them").fire("click"); await settle();
  const dataRow = main.all().find(n => n.tagName === "tr" && n.children[0] && n.children[0].textContent === "data");
  buttonIn(dataRow, "Remove…").fire("click");
  result.imageRemoveAsk = confirmText(dataRow);
  buttonIn(dataRow, "Yes, remove it").fire("click"); await settle();
  result.imagePosts = posts();
  // 8. the privacy check: a refused repository is "not checked", never clean; the all-clear only for all checked
  result.privacy = vm.runInContext(`[privacyResult([{ repository: "/a", checked: false, refused: "no rules", findings: [] }, { repository: "/b", checked: true, scanned: 3, findings: [] }], "ct1"),
    privacyResult([{ repository: "/b", checked: true, scanned: 3, findings: [] }], "ct1"), privacyResult([], "ct1")].map(nodes => nodes.filter(n => (n.className || "").includes("notice")).map(n => n.textContent))`, context);
  // 9. bound values: Stop and wait, a preset directory
  reset();
  answer = () => ({ output: "ok" });
  vm.runInContext('moreCard(__main, { name: "web", host: "ct1", state: "running", keep: true })', context);
  byId("more-wait").value = "45";
  buttonIn(main, "Stop and wait…").fire("click");
  byId("more-wait").value = "99";
  buttonIn(main, "Yes, stop it").fire("click"); await settle();
  const stopped = posts();
  reset();
  answer = (method, path) => (path.endsWith("/preset-dirs") ? { dirs: [] } : path.endsWith("/presets") ? [] : { output: "ok" });
  vm.runInContext('state.page = "presets";', context);
  await vm.runInContext("pages.presets(__main)", context); await settle();
  byId("preset-dir").value = "/srv/a";
  buttonIn(main, "Add…").fire("click");
  byId("preset-dir").value = "/srv/b";
  buttonIn(main, "Yes, add it").fire("click"); await settle();
  result.bound = [...stopped, ...posts()];
  // 10. kits: a marketplace added, asked first; an LLM server replaced, asked first
  reset();
  answer = (method, path) => (path.endsWith("/kits") && method === "GET" ? { kits: [{ name: "mine", kind: "user", agents: {} }] } : { output: "ok" });
  vm.runInContext('state.page = "kits";', context);
  await vm.runInContext("pages.kits(__main)", context); await settle();
  byId("kit-target").value = "mine";  // a browser's select: its first option
  byId("kit-market-name").value = "mp"; byId("kit-market-source").value = "owner/repo"; byId("kit-market-agent").value = "claude";
  buttonIn(main, "Add marketplace").fire("click");
  result.marketAsk = confirmText(main);
  buttonIn(main, "Yes, add it").fire("click"); await settle();
  result.market = posts();
  reset();
  answer = (method, path) => (path.endsWith("/llm") && method === "GET" ? [] : { output: "ok" });
  vm.runInContext('state.page = "llm";', context);
  await vm.runInContext("pages.llm(__main)", context); await settle();
  for (const id of ["llm-model", "llm-key-secret", "llm-headers", "llm-ca", "llm-vpn"]) byId(id).value = "";
  byId("llm-name").value = "gw"; byId("llm-kind").value = "vllm"; byId("llm-url").value = "https://gw.example.org"; byId("llm-replace").checked = true;
  buttonIn(main, "Add").fire("click");
  result.llmReplace = [confirmText(main), posts().length];
  buttonIn(main, "Yes, replace it").fire("click"); await settle();
  result.llmReplacePost = posts().map(p => [p[2].replace, p[2].confirm]);
  // 10b. a start's teams (list --json "teams"): named below it, with the other members listed
  result.teams = vm.runInContext(`[...startTree([{ name: "review-claude", host: "ct1", kind: "session", teams: ["review"] },
    { name: "review-codex", host: "ct1", kind: "session", teams: ["review"] }, { name: "solo", host: "ct1", kind: "session", teams: [] }]).notes.values()]`, context);
  // 11. an install whose answer was cut off: an older install's result is never shown as this one's
  answer = () => ({ running: false, last: { result: { name: "ct1", after: "0.1.70" }, ago: 3600, error: null } });
  vm.runInContext("setTimeout = (f) => { f(); return 0; }", context);
  const status = new Node("div"); context.__status = status;
  result.waited = [await vm.runInContext('waitForInstall("ct1", __status, new Error("cut off"), Date.now())', context), status.textContent.includes("knows of no install")];
  answer = () => ({ running: false, last: { result: { name: "ct1", after: "0.1.80" }, ago: 1, error: null } });
  result.waitedOurs = await vm.runInContext('waitForInstall("ct1", __status, new Error("cut off"), Date.now() - 60000)', context);
  console.log(JSON.stringify(result));
})().catch(e => { console.error(e.stack); process.exit(1); });
"""


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_round_four_of_the_page(tmp_path):
    """In node: the structured start form (its own host, presets by category for the agent, a field per value with
    that host's profiles, the MCP servers there, presets check before Start, -mm for the model, "only this server"
    off for the cloud and on by default); cards below their row with the focus on them; LLM and VPN rows asking
    their own host, named for screen readers; one live region; error codes in the page's words; a slow page never
    drawing over a newer one; teams; images; the privacy check's all-clear; values bound to their question."""
    result = run_driver(tmp_path, ROUND4_DRIVER, "round4")
    assert result["formHosts"] == ["local", "ct1"]
    assert result["only"] == [True, True], "on by default (as the app's wizard), off while the cloud is chosen"
    assert result["example"] == "--mode high\n--allow-pypi\n-mm sonnet"
    assert result["groupsClaude"] == [["Local models", ["ollama"]], ["Analysis", ["software-analysis"]], ["Privacy", ["tor"]],
                                      ["Fun", ["sand-chess"]]]
    assert result["groupsCodex"] == [["Local models", ["ollama"]], ["Analysis", ["software-analysis"]], ["Privacy", ["tor"]]]
    assert result["torChoices"][:2] == ["", "anon"] and "home" not in result["torChoices"], "a Tor value: Tor profiles only"
    opened, text = result["notReady"]
    assert opened == 0 and "not ready on ct1: nothing was started" in text and "no VPN profile anon" in text
    assert result["checked"] == [["/api/hosts/ct1/presets/tor/check", {"values": {"tor": "anon"}, "agent": "codex"}]] * 2
    assert result["started"] == [["ct1", "codex", ["--preset", "tor", "--preset-value", "tor=anon", "--mode", "high"]]]
    assert result["mcp"] == [["status-ok", "hub:capa added on ct1"],
                             ["status-warn", "hub:yara not added on ct1 yet: sandbox-ai mcp add hub:yaraOpen MCP servers"]]
    assert result["samples"] and result["ollama"] == ["ollama", True], "the preset names the LLM server: no model"
    assert result["onlyWithServer"] == [True, False]
    heading, asked = result["otherHost"]
    assert heading == "New start on local" and asked == ["local"], "the form asks the host it names"
    row, below, focused = result["card"]
    assert row == "card-row" and below and focused == "h2", "the card right below its start, its heading focused"
    assert "History web on ct1" in result["rowLabels"] and "Stop web on ct1" in result["rowLabels"]
    assert result["closed"] is False
    assert result["llmPaths"] == ["/api/hosts/ct1/llm/gw/check", "/api/hosts/ct1/llm/gw/models", "/api/hosts/ct1/llm/gw"]
    assert {"Check gw on ct1", "Models gw on ct1", "Show gw on ct1", "Remove gw on ct1"} <= set(result["llmLabels"])
    assert result["llmRemove"].startswith("Remove the LLM server gw on ct1?") and "Yes, remove it" in result["llmRemove"]
    assert "forget" not in result["llmRemove"]
    assert result["vpnPaths"] == ["/api/hosts/ct1/vpn/anon/check"]
    ask, before = result["vpnForce"]
    assert "Replace the VPN profile anon on ct1?" in ask and before == 0
    assert result["notice"] == [None, "it failed"]
    english, german, plain, unknown = result["codes"]
    assert english == "There is no host ct9 (any more): choose another one, or add it on the page Hosts.no such host"
    assert german.startswith("Es gibt (nicht mehr) den Host ct9") and german.endswith("no such host")
    assert plain == "boom" and unknown == "x"
    assert result["guard"] == ["LLM servers on ct1"], "the doctor's late answer drew nothing"
    in_slot, board, attach = result["teamCard"]
    assert in_slot and board and attach == ["Attach review-claude on ct1"]
    ask, before = result["teamStop"]
    assert "Stop the team review on ct1?" in ask and "review-claude, review-codex" in ask and before == 0
    assert result["teamPosts"] == [["POST", "/api/hosts/ct1/teams/review/post", {"agent": "codex", "task": "review the docs"}],
                                   ["POST", "/api/hosts/ct1/teams/review/stop", {"confirm": True}]]
    assert result["teamStart"] == [{"kind": "team-start", "host": "ct1", "team": "duo", "agents": "opencode,codex",
                                    "workspace": "/srv/x", "options": '["--mode","high"]'}]
    assert result["buildTargets"] == ["claude", "codex", "opencode", "all", "team", "vpn", "data"]
    ask, before = result["imageAsk"]
    assert ask.startswith("Build /home/u/t.sh of ct1 into data?") and before == 0
    assert "Remove the variant data on ct1" in result["imageRemoveAsk"]
    assert result["imagePosts"] == [
        ["POST", "/api/hosts/ct1/images/data/extend", {"add_apt": ["jq"], "add_files": [
            {"source": "/home/u/t.sh", "target": "/usr/local/bin/t", "mode": "0755"}], "confirm": True}],
        ["POST", "/api/hosts/ct1/images/data/remove", {"keep_image": False, "confirm": True}]]
    refused, clean, empty = result["privacy"]
    assert refused == ["/a: not checked: no rules"], "no all-clear while one was refused"
    assert clean == ["Nothing that should stay private."] and empty == ["Nothing was checked on ct1: there is no all-clear."]
    assert result["bound"] == [["POST", "/api/hosts/ct1/starts/web/stop", {"wait": 45, "confirm": True}],
                               ["POST", "/api/hosts/ct1/preset-dirs", {"directory": "/srv/a", "confirm": True}]]
    assert result["marketAsk"].startswith("Add mp to the kit mine on ct1?")
    assert result["market"] == [["POST", "/api/hosts/ct1/kits/mine/add-marketplace",
                                 {"marketplace": "mp", "source": "owner/repo", "agent": "claude", "create": False,
                                  "confirm": True}]]
    ask, before = result["llmReplace"]
    assert "Replace the LLM server gw on ct1?" in ask and before == 0 and result["llmReplacePost"] == [[True, True]]
    assert result["waited"] == [None, True] and result["waitedOurs"] == {"name": "ct1", "after": "0.1.80"}
    assert result["teams"] == [["Team review: review-codex"], ["Team review: review-claude"]], "list --json's teams"


SECRET_FIELDS_DRIVER = FAKE_DOM + r"""
// a working localStorage (the stand-in's keeps nothing), and one that throws (blocked site data)
const store = {};
context.localStorage.getItem = (k) => (k in store ? store[k] : null);
context.localStorage.setItem = (k, v) => { store[k] = String(v); };
const secretInputs = (root) => root.all().filter(n => n.tagName === "input" && (n.attributes.type === "password" || "data-secret-value" in n.attributes));
const look = (input) => {
  const toggle = input.parent && input.parent.children.find(n => n.tagName === "button" && (n.className || "").includes("secret-toggle"));
  return { id: input.id, type: input.attributes.type, autocomplete: input.attributes.autocomplete, spellcheck: input.attributes.spellcheck,
    value: input.value || "", toggle: toggle ? [toggle.textContent, toggle.attributes.type, toggle.attributes["aria-pressed"], toggle.attributes.title, toggle.attributes["aria-controls"]] : null };
};
const press = (input) => input.parent.children.find(n => (n.className || "").includes("secret-toggle")).fire("click");
(async () => {
  const result = {};
  vm.runInContext('state.me = { user: "admin", csrf: "c" }; state.host = "ct1"; state.hosts = [{ name: "local", kind: "local" }, { name: "ct1", kind: "ssh", ssh: "root@ct1", port: 22 }];', context);
  // every page of the page, each drawn from an empty answer: every secret field there has its toggle, shown at first
  answer = (method, path) => (path.endsWith("/secrets") ? [{ name: "GITEA_TOKEN", store: "file" }] : []);
  result.pages = {};
  for (const name of vm.runInContext("Object.keys(pages)", context)) {
    const page = new Node("main"); body.append(page); context.__page = page;
    vm.runInContext(`state.page = ${JSON.stringify(name)};`, context);
    try { await vm.runInContext(`pages[${JSON.stringify(name)}](__page)`, context); await settle(); } catch (e) { /* a page that wants more: its fields are not drawn */ }
    const found = secretInputs(page).map(look);
    if (found.length) result.pages[name] = found;
    page.remove();
  }
  result.drawnPages = vm.runInContext("Object.keys(pages).length", context);
  // the Secrets page: shown while typed; a press hides it (and is remembered for every such field); drawn again: hidden
  const secrets = new Node("main"); body.append(secrets); context.__page = secrets;
  vm.runInContext('state.page = "secrets";', context);
  calls.length = 0;
  await vm.runInContext("pages.secrets(__page)", context); await settle();
  let value = secretInputs(secrets)[0];
  value.value = "s3cr3t";
  press(value);
  result.pressed = [look(value), store["sandbox-ai-show-secret-values"]];
  secrets.remove();
  const again = new Node("main"); body.append(again); context.__page = again;
  await vm.runInContext("pages.secrets(__page)", context); await settle();
  value = secretInputs(again)[0];
  result.remembered = look(value);
  press(value);
  result.pressedAgain = [look(value), store["sandbox-ai-show-secret-values"]];
  result.requests = calls.map(c => [c.method, c.path]);
  again.remove();
  // the web login: hidden at first whatever the secret fields remember, its choice not remembered, no submit
  store["sandbox-ai-show-secret-values"] = "1";
  const login = vm.runInContext("loginView()", context); body.append(login);
  const password = secretInputs(login)[0];
  result.login = [look(password)];
  press(password);
  result.login.push(look(password), store["sandbox-ai-show-secret-values"]);
  press(password);
  result.login.push(look(password).type, store["sandbox-ai-show-secret-values"]);
  login.remove();
  // a browser that keeps nothing: shown, as the default
  context.localStorage.getItem = () => { throw new Error("blocked"); };
  context.localStorage.setItem = () => { throw new Error("blocked"); };
  const blocked = new Node("main"); body.append(blocked); context.__page = blocked;
  await vm.runInContext("pages.secrets(__page)", context); await settle();
  value = secretInputs(blocked)[0];
  result.blocked = [look(value).type];
  press(value);
  result.blocked.push(look(value).type);
  console.log(JSON.stringify(result));
})().catch(e => { console.error(e.stack); process.exit(1); });
"""


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_every_secret_value_field_has_show_value_shown_by_default(tmp_path):
    """In node: every field the page builds where a secret's value (or the login's password) is typed has "Show
    value" beside it (a toggle button with aria-pressed). A secret's value is shown while it is typed (type text,
    no autofill, no spell check) unless this browser chose otherwise for all such fields; the web login's password
    starts hidden and its choice is not remembered. A value already stored is never read back into the field."""
    source = (WEB / "app.js").read_text()
    # the only way the page makes a password field is secretInput (which sets the type itself)
    assert 'type: "password"' not in source and "type: 'password'" not in source
    assert source.count('setAttribute("type", shown ? "text" : "password")') == 1
    result = run_driver(tmp_path, SECRET_FIELDS_DRIVER, "secret_fields")
    assert result["drawnPages"] >= 15
    shown = ["Show value", "button", "true", "Hide value"]
    # every page drawn: the values typed in the page are a secret's (page Secrets) and a Git or publish login's
    # token (page Logins, stored as a secret first); the LLM and VPN pages name a stored secret, never its value
    field = lambda ident: {"id": ident, "type": "text", "autocomplete": "off", "spellcheck": "false", "value": "",
                           "toggle": shown + [ident]}
    assert result["pages"] == {"secrets": [field("secret-value")], "logins": [field("git-token"), field("publish-token")]}
    hidden = ["Show value", "button", "false", "Show value", "secret-value"]
    look, kept = result["pressed"]
    assert look["type"] == "password" and look["autocomplete"] == "new-password" and look["toggle"] == hidden
    assert look["value"] == "s3cr3t" and kept == "0", "hiding keeps what was typed; the choice is remembered"
    assert result["remembered"]["type"] == "password" and result["remembered"]["toggle"] == hidden
    assert result["remembered"]["value"] == ""
    look, kept = result["pressedAgain"]
    assert look["type"] == "text" and look["toggle"] == shown + ["secret-value"] and kept == "1"
    assert all(method == "GET" and path == "/api/hosts/ct1/secrets" for method, path in result["requests"]), \
        "only the names are asked for, never a value"
    first, pressed, kept, back, kept_after = result["login"]
    assert first["id"] == "password" and first["type"] == "password" and first["autocomplete"] == "current-password"
    assert first["toggle"] == ["Show value", "button", "false", "Show value", "password"]
    assert pressed["type"] == "text" and pressed["toggle"][2] == "true" and kept == "1" and back == "password"
    assert kept_after == "1", "the login's toggle is not remembered for the secret fields"
    assert result["blocked"] == ["text", "password"], "no storage: shown by default, the toggle still works"


MORE_DRIVER = FAKE_DOM + r"""
const confirmText = (node) => { const c = node.all().find(n => n.className === "confirm"); return c ? c.textContent : null; };
const posts = () => calls.filter(c => c.method !== "GET").map(c => [c.path, JSON.parse(c.body)]);
const gets = () => calls.filter(c => c.method === "GET").map(c => c.path);
const byId = (id) => document.getElementById(id);
const reset = () => { calls.length = 0; main.children = []; };
const fields = () => { for (const id of ["more-workspaces", "more-mounts", "more-mcp"]) if (byId(id)) byId(id).value = ""; };  // a browser's are ""
(async () => {
  const result = {};
  vm.runInContext('state.me = { user: "admin", csrf: "c" }; state.host = "ct1"; state.page = "logs"; state.hosts = [{ name: "ct1", kind: "ssh", ssh: "root@ct1", port: 22 }];', context);
  let review = { name: "kept", id: "kept-1", state: "stopped", pending: true, review: { since: "2026-11-01", complete: true, changes: [{ change: "changed", path: ".bashrc" }] } };
  answer = (method, path) => (path.endsWith("/review") ? review : path.endsWith("/history") ? [{ time: "t", event: "start", run: "r1" }] : { output: "ok" });
  // (1) paused, kept: the restart is offered and ticked; what is added goes with it
  vm.runInContext('moreCard(__main, { name: "web", id: "web-1", host: "ct1", state: "paused", keep: true })', context);
  fields();
  result.pausedForm = [Boolean(byId("more-restart")), "checked" in byId("more-restart").attributes, Boolean(button(main, "Restart with these…")), Boolean(button(main, "Stop and wait…"))];
  byId("more-workspaces").value = "/srv/lib";
  byId("more-restart").checked = false;
  button(main, "Restart with these…").fire("click");
  result.noRestart = [byId("more-card").textContent.includes("Tick “Restart it now”"), posts().length, confirmText(main)];
  byId("more-restart").checked = true;
  button(main, "Restart with these…").fire("click");
  button(main, "Yes, resume it").fire("click"); await settle();
  result.pausedPost = posts();
  // (2) stopped, kept: asked, then its HOME reviewed as Resume does; the resume only after "Resume anyway"
  reset();
  vm.runInContext('moreCard(__main, { name: "kept", id: "kept-1", host: "ct1", state: "stopped", keep: true })', context);
  fields();
  result.stoppedForm = [Boolean(byId("more-restart")), Boolean(button(main, "Resume with these…")), Boolean(button(main, "Stop and wait…"))];
  byId("more-mcp").value = "files";
  button(main, "Resume with these…").fire("click");
  button(main, "Yes, resume it").fire("click"); await settle();
  result.reviewed = [gets(), posts().length, Boolean(byId("review-card")) && byId("review-card").textContent.includes(".bashrc")];
  button(byId("review-card"), "Resume anyway").fire("click"); await settle();
  result.stoppedPost = posts();
  result.closed = [Boolean(byId("more-card")), Boolean(byId("review-card"))];
  // nothing changed in its HOME: resumed at once
  reset();
  review = { name: "kept", id: "kept-1", state: "stopped", pending: true, review: { since: "x", complete: true, changes: [] } };
  vm.runInContext('moreCard(__main, { name: "kept", id: "kept-1", host: "ct1", state: "stopped", keep: true })', context);
  fields(); byId("more-workspaces").value = "/srv/lib";
  button(main, "Resume with these…").fire("click");
  button(main, "Yes, resume it").fire("click"); await settle();
  result.cleanPost = posts();
  // (3) a run without a kept start: only the longer stop; no More for it once stopped
  reset();
  vm.runInContext('moreCard(__main, { name: "plain", id: "run-1", host: "ct1", state: "running", keep: false, kind: "run" })', context);
  result.plain = [Boolean(button(main, "Restart with these…")), Boolean(byId("more-workspaces")), Boolean(button(main, "Stop and wait…")), byId("more-card").textContent.includes("keeps nothing to start again with")];
  result.rows = vm.runInContext(`[{ state: "running", keep: false }, { state: "paused", keep: true }, { state: "stopped", keep: true }, { state: "stopped", keep: false }]
    .map(x => Boolean(startRow(Object.assign({ name: "x", host: "ct1", kind: "session" }, x), [], __main, false).all().find(n => n.tagName === "button" && n.textContent === "More…")))`, context);
  // (4) More follows a rename and is switched off once the state is another
  reset();
  vm.runInContext('moreCard(__main, { name: "web", id: "web-1", host: "ct1", state: "running", keep: true })', context);
  vm.runInContext('retireCards([{ name: "web2", id: "web-1", host: "ct1", state: "running" }], () => false)', context);
  result.renamed = byId("more-card").children.find(n => n.tagName === "h2").textContent;
  button(main, "Stop and wait…").fire("click");
  button(main, "Yes, stop it").fire("click"); await settle();
  result.renamedPost = posts().map(p => p[0]);
  reset();
  vm.runInContext('moreCard(__main, { name: "web", id: "web-1", host: "ct1", state: "running", keep: true })', context);
  vm.runInContext('retireCards([{ name: "web", id: "web-1", host: "ct1", state: "paused" }], () => false)', context);
  result.retired = [byId("more-card").textContent.includes("web on ct1 is paused now"), button(main, "Restart with these…").disabled];
  // History: read again under the new name; a state change keeps it, gone switches it off
  reset();
  await vm.runInContext('historyCard(__main, { name: "web", id: "web-1", host: "ct1", state: "running" })', context);
  vm.runInContext('retireCards([{ name: "web3", id: "web-1", host: "ct1", state: "stopped" }], () => false)', context); await settle();
  result.history = [gets(), byId("history-card").children.find(n => n.tagName === "h2").textContent, byId("history-card").textContent.includes("nothing here changes it")];
  vm.runInContext('retireCards([], () => false)', context);
  result.historyGone = byId("history-card").textContent.includes("web3 on ct1 is gone");
  console.log(JSON.stringify(result));
})().catch(e => { console.error(e.stack); process.exit(1); });
"""


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_more_resumes_a_paused_start_with_a_restart_and_reviews_a_stopped_ones_home(tmp_path):
    """Review 5 M5/W4, M6, M8: More… on a paused start offers and sends the restart (the CLI adds nothing to a
    paused start without one); on a stopped start the kept HOME is reviewed first, as Resume does; a run without a
    kept start gets only the longer stop; the More and History cards follow a rename, More is switched off when
    the state changes, History when the start is gone."""
    result = run_driver(tmp_path, MORE_DRIVER, "more")
    assert result["pausedForm"] == [True, True, True, True]
    shown, before, asked = result["noRestart"]
    assert shown and before == 0 and asked is None, "added without the restart: said in the card, nothing asked or sent"
    assert result["pausedPost"] == [["/api/hosts/ct1/starts/web/resume", {"workspaces": ["/srv/lib"], "mcp": [], "mounts": [],
                                                                          "restart": True, "confirm": True}]]
    assert result["stoppedForm"] == [False, True, False]
    reviewed, before, card = result["reviewed"]
    assert reviewed == ["/api/hosts/ct1/starts/kept/review"] and before == 0 and card, "the review first, nothing resumed"
    assert result["stoppedPost"] == [["/api/hosts/ct1/starts/kept/resume", {"workspaces": [], "mcp": ["files"], "mounts": [],
                                                                            "confirm": True}]]
    assert result["closed"] == [False, False]
    assert result["cleanPost"] == [["/api/hosts/ct1/starts/kept/resume", {"workspaces": ["/srv/lib"], "mcp": [], "mounts": [],
                                                                          "confirm": True}]]
    assert result["plain"] == [False, False, True, True]
    assert result["rows"] == [True, True, True, False]
    assert result["renamed"] == "More for web2 on ct1"
    assert result["renamedPost"] == ["/api/hosts/ct1/starts/web2/stop"]
    assert result["retired"] == [True, True]
    history, heading, retired = result["history"]
    assert history == ["/api/hosts/ct1/starts/web/history", "/api/hosts/ct1/starts/web3/history"]
    assert heading == "History of web3 on ct1" and not retired
    assert result["historyGone"]


LOGIN_TOKENS_DRIVER = FAKE_DOM + r"""
const byId = (id) => document.getElementById(id);
const confirmText = (node) => { const c = node.all().find(n => n.className === "confirm"); return c ? c.textContent : null; };
const posts = () => calls.filter(c => c.method !== "GET").map(c => [c.path, JSON.parse(c.body)]);
const formOf = (id) => { let n = byId(id); while (n && n.className !== "form") n = n.parent; return n; };
const fire = (id, type, value) => { const n = byId(id); if (value !== undefined) n.value = value; n.fire(type); };
const toggleOf = (id) => byId(id).parent.children.find(n => (n.className || "").includes("secret-toggle"));
context.URL = URL;  // the browser's, for the suggested name of a Git server's token
(async () => {
  const result = {};
  vm.runInContext('state.me = { user: "admin", csrf: "c" }; state.host = "ct1"; state.page = "logins"; state.hosts = [{ name: "local", kind: "local" }, { name: "ct1", kind: "ssh", ssh: "root@ct1", port: 22 }];', context);
  let secretAnswer = () => ({ output: "kept" }), checkAnswer = () => ({ ok: true }), addAnswer = () => ({ output: "added" });
  answer = (method, path, sent) => {
    if (method === "GET") return path.endsWith("/secrets") ? [{ name: "GITEA_TOKEN", store: "file" }, { name: "PYPI_TOKEN", store: "file" }] : path.endsWith("/identities") ? {} : [];
    if (path.includes("/secrets/")) return secretAnswer(path, sent, method);
    return JSON.parse(sent).check_only ? checkAnswer(path, sent) : addAnswer(path, sent);
  };
  vm.runInContext("show('logins')", context); await settle();
  const TOKEN = "glpat-" + "x".repeat(300);
  // Git: "Enter the token" at first, the name suggested from the server, the token shown while typed, no length limit
  result.gitStart = [byId("git-source").value, byId("git-secret").value, byId("git-token").attributes.type,
    toggleOf("git-token").attributes["aria-pressed"], "maxlength" in byId("git-token").attributes, byId("git-stored").hidden, byId("git-token").parent.hidden];
  fire("git-url", "input", "https://git.example.org:3000");
  result.gitSuggested = byId("git-secret").value;
  const typed = byId("git-token"); typed.value = TOKEN; fire("git-user", "input", "");
  const gitForm = () => formOf("git-url");  // drawn anew after each login added (show("logins"))
  button(gitForm(), "Add…").fire("click"); await settle();
  result.gitAsk = [confirmText(gitForm()), posts()];
  button(gitForm(), "Yes, add it").fire("click"); await settle();
  result.gitPosts = posts(); result.gitCleared = [typed.value, byId("git-token") !== typed && !byId("git-token").value];
  // an edited name stays when the server changes; a pasted token as the name is refused here
  calls.length = 0;
  byId("git-user").value = "";
  fire("git-secret", "input", "MY_GIT");
  fire("git-url", "input", "https://gitlab.example.com");
  result.gitEdited = byId("git-secret").value;
  fire("git-secret", "input", "glpat-the-token");
  byId("git-token").value = TOKEN;
  button(gitForm(), "Add…").fire("click"); await settle();
  result.badName = [posts().length, toasts().some(x => x.includes("Which name?"))];
  // a secret of that name came meanwhile: the service's refusal is asked about, Yes stores it confirmed
  let first = true;
  secretAnswer = () => { if (first) { first = false; return { __status: 400, __error: 'NEW_GIT: a secret of that name exists: its value is replaced: send {"confirm": true}', __confirm: "secret-replace" }; } return { output: "kept" }; };
  fire("git-secret", "input", "NEW_GIT");
  byId("git-token").value = "tok-2";
  button(gitForm(), "Add…").fire("click"); await settle();
  button(gitForm(), "Yes, add it").fire("click"); await settle();
  result.reasked = confirmText(gitForm());
  button(gitForm(), "Yes, do it").fire("click"); await settle();
  result.reaskedPosts = posts();
  // publish: the usual name of each kind; an existing one is asked about before it is replaced
  calls.length = 0; secretAnswer = () => ({ output: "kept" });
  result.kinds = {};
  for (const kind of ["npm", "pypi", "testpypi", "crates", "gitlab", "github"]) { fire("publish-kind", "change", kind); result.kinds[kind] = byId("publish-secret").value; }
  fire("publish-kind", "change", "pypi");
  byId("publish-token").value = "pypi-the-token"; byId("publish-url").value = "";
  const publishForm = () => formOf("publish-kind");
  button(publishForm(), "Add").fire("click");
  result.publishAsk = [confirmText(publishForm()), posts().length];
  button(publishForm(), "Yes, replace it").fire("click"); await settle();
  result.publishPosts = posts();
  // "Use a stored secret": the host's names, nothing stored, the login names the one chosen
  calls.length = 0;
  fire("publish-source", "change", "stored");
  result.storedShown = [byId("publish-token").parent.hidden, byId("publish-secret").hidden, byId("publish-stored").hidden,
    byId("publish-stored").children.map(o => o.value)];
  byId("publish-stored").value = "GITEA_TOKEN"; byId("publish-url").value = "";
  fire("publish-kind", "change", "gitlab");
  button(publishForm(), "Add").fire("click"); await settle();
  result.storedPosts = posts();
  result.hint = byId("git-secret-hint").textContent;
  // review 5 M4: a server sandbox-ai refuses is said before anything is stored, the form kept as typed
  calls.length = 0; secretAnswer = () => ({ output: "kept" });
  checkAnswer = () => ({ __status: 400, __error: "the Git server: plain http would send the token unencrypted; use https: 'http://git.lan'" });
  fire("git-url", "input", "http://git.lan"); fire("git-secret", "input", "LAN_TOKEN"); byId("git-token").value = "tok-3"; byId("git-user").value = "";
  button(gitForm(), "Add…").fire("click"); await settle();
  result.refusedUrl = [posts(), confirmText(gitForm()), gitForm().textContent.includes("plain http would send the token unencrypted"),
    byId("git-url").value, byId("git-secret").value, byId("git-token").value];
  // the add refused after all (the host's own check): the secret stored for it under a new name goes again
  calls.length = 0; checkAnswer = () => ({ ok: true });
  addAnswer = () => ({ __status: 502, __error: "sandbox-ai git-logins on ct1: exit status 1: refused" });
  fire("git-url", "input", "https://git.lan");
  button(gitForm(), "Add…").fire("click"); await settle();
  button(gitForm(), "Yes, add it").fire("click"); await settle();
  result.orphan = [calls.filter(c => c.method !== "GET").map(c => [c.method, c.path, JSON.parse(c.body)]), gitForm().textContent.includes("The secret LAN_TOKEN stored for it was removed again."),
    gitForm().textContent.includes("refused")];
  console.log(JSON.stringify(result));
})().catch(e => { console.error(e.stack); process.exit(1); });
"""


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_a_login_token_is_typed_and_stored_as_a_secret_first(tmp_path):
    """In node: a Git or publish login takes the token itself ("Enter the token", shown while typed, no length
    limit) under a suggested secret name (GIT_TOKEN_<HOST>, PYPI_TOKEN...) that can be changed and is checked; the
    page stores it with POST /secrets/NAME (secrets set NAME, the token on standard input) and only then adds the
    login naming it; a secret that exists is asked about first. Or a stored secret of that host is chosen. The token
    goes in no other request. Review 5 M4: the login is checked first (check_only: its server as sandbox-ai checks
    it), so nothing is stored for one that is refused; the form keeps what was typed; a secret stored for a login
    the host refuses after all is removed again."""
    result = run_driver(tmp_path, LOGIN_TOKENS_DRIVER, "login_tokens")
    assert result["gitStart"] == ["token", "GIT_TOKEN", "text", "true", False, True, False]
    assert result["gitSuggested"] == "GIT_TOKEN_GIT_EXAMPLE_ORG"
    ask, before = result["gitAsk"]
    assert "the Git login of the secret GIT_TOKEN_GIT_EXAMPLE_ORG?" in ask
    check = ["/api/hosts/ct1/git-logins", {"url": "https://git.example.org:3000", "secret": "GIT_TOKEN_GIT_EXAMPLE_ORG", "user": "",
                                           "check_only": True}]
    assert before == [check], "checked first, nothing stored before the question"
    assert "The token is stored first as the secret GIT_TOKEN_GIT_EXAMPLE_ORG on ct1, for good." in ask
    token = "glpat-" + "x" * 300
    assert result["gitPosts"] == [check,
        ["/api/hosts/ct1/secrets/GIT_TOKEN_GIT_EXAMPLE_ORG", {"secret": token}],
        ["/api/hosts/ct1/git-logins", {"url": "https://git.example.org:3000", "secret": "GIT_TOKEN_GIT_EXAMPLE_ORG",
                                       "user": "", "confirm": True}]]
    assert result["gitCleared"] == ["", True], "the token is gone from the page once stored, the page drawn anew"
    assert result["gitEdited"] == "MY_GIT", "a name the user chose is not overwritten"
    assert result["badName"] == [0, True], "a token pasted as the name is refused before anything is sent"
    assert result["reasked"].startswith("The service asks first: NEW_GIT: a secret of that name exists")
    assert result["reaskedPosts"] == [
        ["/api/hosts/ct1/git-logins", {"url": "https://gitlab.example.com", "secret": "NEW_GIT", "user": "", "check_only": True}],
        ["/api/hosts/ct1/secrets/NEW_GIT", {"secret": "tok-2"}], ["/api/hosts/ct1/secrets/NEW_GIT", {"secret": "tok-2", "confirm": True}],
        ["/api/hosts/ct1/git-logins", {"url": "https://gitlab.example.com", "secret": "NEW_GIT", "user": "", "confirm": True}]]
    assert result["kinds"] == {"npm": "NPM_TOKEN", "pypi": "PYPI_TOKEN", "testpypi": "TESTPYPI_TOKEN", "crates": "CARGO_TOKEN",
                               "gitlab": "GITLAB_TOKEN", "github": "GITHUB_TOKEN"}
    ask, before = result["publishAsk"]
    assert ask.startswith("Replace the value of PYPI_TOKEN on ct1?") and before == 0
    assert result["publishPosts"] == [["/api/hosts/ct1/publish-logins", {"kind": "pypi", "secret": "PYPI_TOKEN", "url": "", "check_only": True}],
                                      ["/api/hosts/ct1/secrets/PYPI_TOKEN", {"secret": "pypi-the-token", "confirm": True}],
                                      ["/api/hosts/ct1/publish-logins", {"kind": "pypi", "secret": "PYPI_TOKEN", "url": ""}]]
    assert result["storedShown"] == [True, True, False, ["GITEA_TOKEN", "PYPI_TOKEN"]]
    assert result["storedPosts"] == [["/api/hosts/ct1/publish-logins", {"kind": "gitlab", "secret": "GITEA_TOKEN", "url": "", "check_only": True}],
                                     ["/api/hosts/ct1/publish-logins", {"kind": "gitlab", "secret": "GITEA_TOKEN", "url": ""}]]
    assert "A secret's name is what the token is stored under" in result["hint"]
    sent, asked, said, *kept = result["refusedUrl"]
    assert sent == [["/api/hosts/ct1/git-logins", {"url": "http://git.lan", "secret": "LAN_TOKEN", "user": "", "check_only": True}]]
    assert asked is None and said and kept == ["http://git.lan", "LAN_TOKEN", "tok-3"], "nothing stored, nothing asked, all kept"
    sent, removed, said = result["orphan"]
    assert [c[:2] for c in sent] == [["POST", "/api/hosts/ct1/git-logins"], ["POST", "/api/hosts/ct1/secrets/LAN_TOKEN"],
                                     ["POST", "/api/hosts/ct1/git-logins"], ["DELETE", "/api/hosts/ct1/secrets/LAN_TOKEN"]]
    assert sent[-1][2] == {"confirm": True} and removed and said


LLM_PAGE_DRIVER = FAKE_DOM + r"""
const byId = (id) => document.getElementById(id);
const confirmText = (node) => { const c = node.all().find(n => n.className === "confirm"); return c ? c.textContent : null; };
const posts = () => calls.filter(c => c.method !== "GET").map(c => [c.path, JSON.parse(c.body)]);
const paths = () => calls.map(c => `${c.method} ${c.path}`);
const rowOf = (name) => main.all().find(n => n.tagName === "tr" && n.children[0] && n.children[0].textContent === name);
const rowButtonIn = (row, text) => row.all().find(n => n.tagName === "button" && n.textContent === text);
const LONG = "a-model-with-a-name-far-longer-than-the-table-shows-q8";
(async () => {
  const result = {};
  vm.runInContext('state.me = { user: "admin", csrf: "c" }; state.host = "ct1"; state.page = "llm"; state.hosts = [{ name: "local", kind: "local" }, { name: "ct1", kind: "ssh", ssh: "root@ct1", port: 22 }];', context);
  let addAnswer = () => ({ output: "ok" });
  let testAnswer = () => [{ name: "m2", loaded: false }, { name: "m1", loaded: true, size: 4294967296 }];
  const shows = {
    gw: { name: "gw", kind: "vllm", url: "https://gw.example.org", model: "qwen3", api_key_secret: "GW_KEY", headers: { "X-Org": "ORG_KEY" },
          ca: null, vpn: "home", anthropic: true, insecure_http: false, models: ["qwen3", "zeta"], checked: null },
    lan: { name: "lan", kind: "openai-compatible", url: "https://lan.example.org", model: null, api_key_secret: null, headers: {}, ca: "lan.pem",
           vpn: null, anthropic: false, insecure_http: false, models: [], checked: null },
  };
  answer = (method, path) => {
    if (method === "GET" && path.endsWith("/llm")) return [{ name: "gw", kind: "vllm", url: "https://gw.example.org", model: "qwen3" }, { name: "lan", kind: "openai-compatible", url: "https://lan.example.org" }];
    if (path.endsWith("/llm/lan/models")) return [{ name: "only", loaded: null }];
    if (path.endsWith("/llm/gw/models")) return [{ name: "zeta", loaded: false, size: 4294967296, type: "llm", arch: "qwen2", parameters: "7B", quantization: "Q4_K_M", context: 32768 },
      { name: LONG, loaded: false }, { name: "qwen3", loaded: true, size: 8589934592 }];
    if (path.endsWith("/llm-models")) return testAnswer();
    if (method === "GET") { const name = path.split("/").pop(); return shows[name] || { __status: 404, __error: "no such server" }; }
    if (path.endsWith("/check")) return { ok: true, output: "fine" };
    return addAnswer();
  };
  await vm.runInContext("pages.llm(__main)", context); await settle();
  // (1) the models: only the columns with values, loaded first then by name, a long name cut, a filter
  rowButtonIn(rowOf("gw"), "Models").fire("click"); await settle();
  const modelTable = main.all().find(n => n.tagName === "table" && n.textContent.includes("Models of gw"));
  result.columns = modelTable.all().filter(n => n.tagName === "th").map(n => n.textContent);
  const modelRows = modelTable.all().filter(n => n.tagName === "tr" && n.attributes["data-model"]);
  result.order = modelRows.map(r => r.attributes["data-model"]);
  result.cells = modelRows.map(r => r.children.slice(0, -1).map(c => c.textContent));
  result.sizes = vm.runInContext("[readableSize(8589934592), readableSize(4294967296)]", context);
  result.cut = [modelRows[1].children[0].textContent.length, modelRows[1].children[0].attributes.title];
  const filter = byId("llm-models-filter");
  filter.value = "ZE"; filter.fire("input");
  result.filtered = [modelRows.filter(r => !r.hidden).map(r => r.attributes["data-model"]), main.textContent.includes("1 of 3 models")];
  filter.value = ""; filter.fire("input");
  result.defaultMarked = modelRows[0].textContent.includes("the default") && !rowButtonIn(modelRows[0], "Use as default");
  // "Copy NAME:MODEL": the browser does not allow the clipboard here, so the reference is shown in its place
  rowButtonIn(modelRows[2], "Copy gw:zeta").fire("click"); await settle();
  result.copied = modelRows[2].all().some(n => n.tagName === "code" && n.textContent === "gw:zeta");
  // "Use as default": llm show's fields again, the model changed, --replace asked first
  calls.length = 0;
  rowButtonIn(modelRows[2], "Use as default").fire("click"); await settle();
  result.useAsk = [confirmText(modelRows[2]), posts().length];
  rowButtonIn(modelRows[2], "Yes, use it").fire("click"); await settle();
  result.usePosts = posts();
  // (2) Edit…: the form from llm show, saved with --replace (asked)
  calls.length = 0;
  rowButtonIn(rowOf("gw"), "Edit…").fire("click"); await settle();
  result.edit = [byId("llm-name").value, byId("llm-kind").value, byId("llm-url").value, byId("llm-model").value, byId("llm-key-secret").value,
    byId("llm-headers").value, byId("llm-vpn").value, byId("llm-anthropic").checked, byId("llm-replace").checked, byId("llm-form-title").textContent,
    byId("llm-model-pick").children.map(o => o.value), byId("llm-model-pick").hidden];
  byId("llm-model-pick").value = "zeta"; byId("llm-model-pick").fire("change");
  result.picked = byId("llm-model").value;
  rowButtonIn(main, "Save").fire("click");
  result.editAsk = [confirmText(main), posts().length];
  rowButtonIn(main, "Yes, replace it").fire("click"); await settle();
  result.editPosts = posts();
  // a server with a CA of its own: Edit… says it is kept (llm add --replace keeps it), and nothing asks about it
  calls.length = 0;
  rowButtonIn(rowOf("lan"), "Edit…").fire("click"); await settle();
  result.caNote = byId("llm-ca-note").textContent;
  rowButtonIn(main, "Test connection").fire("click"); await settle();  // its own CA goes with the test, by name
  result.caTest = posts().filter(p => p[0].endsWith("/llm-models"));
  calls.length = 0;
  rowButtonIn(main, "Save").fire("click");
  result.caAsk = confirmText(main);
  rowButtonIn(main, "Cancel").fire("click");
  // "Use as default" on it: nothing about a CA, and what is sent names no CA either (the host keeps the stored one)
  calls.length = 0;
  rowButtonIn(rowOf("lan"), "Models").fire("click"); await settle();
  const lanRow = main.all().find(n => n.attributes["data-model"] === "only");
  rowButtonIn(lanRow, "Use as default").fire("click"); await settle();
  result.caUseAsk = confirmText(lanRow);
  rowButtonIn(lanRow, "Yes, use it").fire("click"); await settle();
  result.caUsePosts = posts();
  rowButtonIn(main, "Stop editing").fire("click");
  result.stopped = [byId("llm-name").value, byId("llm-form-title").textContent, rowButtonIn(main, "Add") ? "Add" : null];
  // (4) the name checked while typed: the offending character named, a fixed name offered
  const typeName = (value) => { byId("llm-name").value = value; byId("llm-name").fire("input"); return byId("llm-name-problem").textContent; };
  result.names = [typeName("my server"), typeName("-x"), typeName("x".repeat(33)), typeName("lm.studio"), typeName("ok_name-1")];
  typeName("my server");
  rowButtonIn(byId("llm-name-problem"), "Use my-server").fire("click");
  result.fixed = [byId("llm-name").value, byId("llm-name-problem").textContent, byId("llm-name").attributes["aria-invalid"] || null];
  // a refused name or a refusal of the CLI: nothing sent / its message inline, every field kept
  calls.length = 0;
  typeName("bad.name"); byId("llm-url").value = "http://192.168.1.9:11434"; byId("llm-kind").value = "ollama";
  for (const id of ["llm-model", "llm-key-secret", "llm-headers", "llm-ca", "llm-vpn"]) byId(id).value = "";
  rowButtonIn(main, "Add").fire("click"); await settle();
  result.badName = [posts().length, byId("llm-url").value, byId("llm-name").value];
  typeName("new1");
  addAnswer = () => ({ __status: 400, __error: "sandbox-ai llm: plain http to 192.168.1.9 needs a local address" });
  rowButtonIn(main, "Add").fire("click"); await settle();
  result.refused = [main.textContent.includes("plain http to 192.168.1.9 needs a local address"), byId("llm-name").value, byId("llm-url").value, byId("llm-kind").value];
  // (3) Test connection: the models of that URL (wizard models --kind --url --json), nothing saved, the form kept
  addAnswer = () => ({ output: "ok" });
  calls.length = 0;
  rowButtonIn(main, "Test connection").fire("click"); await settle();
  result.testCalls = paths();
  result.testPost = posts()[0];
  result.tested = [byId("llm-test-result").textContent, byId("llm-model-pick").children.map(o => o.value), byId("llm-model-pick").hidden,
    byId("llm-form-title").textContent, byId("llm-url").value, byId("llm-name").value];
  // a key and a CA go with the test (named, read on that host); a header secret or a VPN profile cannot, and it says so
  calls.length = 0;
  byId("llm-key-secret").value = "GW_KEY"; byId("llm-ca").value = "/home/you/ca.pem";
  rowButtonIn(main, "Test connection").fire("click"); await settle();
  result.keyed = [posts()[0], byId("llm-test-result").textContent.includes("does not go through a VPN profile")];
  byId("llm-vpn").value = "home";
  rowButtonIn(main, "Test connection").fire("click"); await settle();
  result.partly = byId("llm-test-result").textContent.includes("The test sends no header secret and does not go through a VPN profile");
  byId("llm-key-secret").value = "sk-the-key-itself";
  calls.length = 0;
  rowButtonIn(main, "Test connection").fire("click"); await settle();
  result.keyPasted = [calls.length, main.textContent.includes("A secret's name")];
  byId("llm-key-secret").value = ""; byId("llm-ca").value = ""; byId("llm-vpn").value = "";
  // a test without a URL asks for one; a server that does not answer says so inline, the form kept
  calls.length = 0;
  byId("llm-url").value = "";
  rowButtonIn(main, "Test connection").fire("click"); await settle();
  result.noUrl = [calls.length, main.textContent.includes("Which URL?")];
  byId("llm-url").value = "http://192.168.1.9:11434";
  testAnswer = () => ({ __status: 502, __error: "sandbox-ai wizard on ct1: exit status 1: Cannot reach ollama at 192.168.1.9:11434" });
  rowButtonIn(main, "Test connection").fire("click"); await settle();
  result.testFailed = [byId("llm-test-result").textContent.includes("Cannot reach ollama at 192.168.1.9:11434"),
    byId("llm-url").value, byId("llm-model-pick").hidden, posts().filter(p => p[0].endsWith("/llm")).length];
  testAnswer = () => [];
  rowButtonIn(main, "Test connection").fire("click"); await settle();
  result.testEmpty = byId("llm-test-result").textContent.includes("lists no model");
  console.log(JSON.stringify(result));
})().catch(e => { console.error(e.stack); process.exit(1); });
"""


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_the_llm_page_models_edit_test_and_names(tmp_path):
    """In node: the models of a server as a table (only the columns with values, loaded first then by name, long
    names cut with the whole one in a title, a filter), "Use as default" (llm show's fields saved again with the
    model, --replace asked first) and "Copy NAME:MODEL"; Edit… fills the form from llm show and saves with --replace;
    Test connection saves first (asked) and lists the models and checks, filling the default-model select; the
    Test connection lists the models of that URL without saving anything (wizard models --kind --url --json), and
    fills the default-model select; the name is checked while typed (the offending character, a fixed name); on any
    error every field stays."""
    result = run_driver(tmp_path, LLM_PAGE_DRIVER, "llm_page")
    assert result["columns"] == ["Name", "Loaded", "Type", "Params", "Quant", "Context", "Size", ""]
    long = "a-model-with-a-name-far-longer-than-the-table-shows-q8"
    assert result["order"] == ["qwen3", long, "zeta"], "loaded first, then by name"
    eight, four = result["sizes"]
    assert result["cells"][0] == ["qwen3", "yes", "", "", "", "", eight]
    assert result["cells"][2] == ["zeta", "no", "llm · qwen2", "7B", "Q4_K_M", "32k", four]
    assert result["cut"] == [48, long]
    assert result["filtered"] == [["zeta"], True]
    assert result["defaultMarked"], "the server's default model is named, without Use as default"
    assert result["copied"]
    ask, before = result["useAsk"]
    assert ask.startswith("Make zeta the default model of gw on ct1?") and before == 0
    assert result["usePosts"] == [["/api/hosts/ct1/llm", {"name": "gw", "kind": "vllm", "url": "https://gw.example.org", "model": "zeta",
                                   "api_key_secret": "GW_KEY", "header_secrets": [{"header": "X-Org", "secret": "ORG_KEY"}],
                                   "vpn": "home", "anthropic": True, "replace": True, "confirm": True}]]
    assert result["edit"] == ["gw", "vllm", "https://gw.example.org", "qwen3", "GW_KEY", "X-Org=ORG_KEY", "home", True, True,
                              "Change the server gw", ["", "qwen3", "zeta"], False]
    assert result["picked"] == "zeta"
    ask, before = result["editAsk"]
    assert "Replace the LLM server gw on ct1?" in ask and before == 0
    assert result["editPosts"] == [["/api/hosts/ct1/llm", {"name": "gw", "kind": "vllm", "url": "https://gw.example.org", "model": "zeta",
                                    "api_key_secret": "GW_KEY", "header_secrets": [{"header": "X-Org", "secret": "ORG_KEY"}],
                                    "vpn": "home", "anthropic": True, "replace": True, "confirm": True}]]
    assert "It has a CA certificate of its own (lan.pem), which is kept" in result["caNote"]
    assert result["caTest"] == [["/api/hosts/ct1/llm-models", {"kind": "openai-compatible", "url": "https://lan.example.org",
                                 "ca_of": "lan"}]], "review 5 M7: the CA it keeps goes with the test"
    assert "CA" not in result["caAsk"] and "Replace the LLM server lan on ct1?" in result["caAsk"]
    assert "CA" not in result["caUseAsk"] and result["caUseAsk"].startswith("Make only the default model of lan on ct1?")
    assert result["caUsePosts"] == [["/api/hosts/ct1/llm", {"name": "lan", "kind": "openai-compatible",
                                     "url": "https://lan.example.org", "model": "only", "replace": True, "confirm": True}]], \
        "no ca in what is sent: llm add --replace keeps the one the host stored"
    assert result["stopped"] == ["", "Add a server", "Add"]
    space, dash, long_name, dot, fine = result["names"]
    assert "a space cannot be in an LLM server's name" in space and "Use my-server" in space
    assert "starts with a letter or a digit, not “-”" in dash and "Use x" in dash
    assert "at most 32 characters" in long_name and f"Use {'x' * 32}" in long_name
    assert "“.” cannot be in an LLM server's name" in dot and "Use lm-studio" in dot
    assert fine == ""
    assert result["fixed"] == ["my-server", "", None]
    assert result["badName"] == [0, "http://192.168.1.9:11434", "bad.name"], "nothing sent, nothing lost"
    assert result["refused"] == [True, "new1", "http://192.168.1.9:11434", "ollama"], "the CLI's message inline, the form kept"
    assert result["testCalls"] == ["POST /api/hosts/ct1/llm-models"], "nothing is saved to test"
    assert result["testPost"] == ["/api/hosts/ct1/llm-models", {"kind": "ollama", "url": "http://192.168.1.9:11434"}]
    text, picks, hidden, title, kept, named = result["tested"]
    assert "http://192.168.1.9:11434 answers: 2 model(s) listed." in text and "m1" in text and "m2" in text
    assert picks == ["", "m1", "m2"] and not hidden, "loaded first, as the default-model list"
    assert title == "Add a server" and kept == "http://192.168.1.9:11434" and named == "new1", "the form as it was"
    sent, no_note = result["keyed"]
    assert sent == ["/api/hosts/ct1/llm-models", {"kind": "ollama", "url": "http://192.168.1.9:11434",
                                                   "ca": "/home/you/ca.pem", "api_key_secret": "GW_KEY"}]
    assert not no_note, "a key and a CA do go with the test: nothing to warn about"
    assert result["partly"], "a VPN profile does not"
    assert result["keyPasted"] == [0, True], "a pasted key as the secret's name: nothing sent"
    assert result["noUrl"] == [0, True]
    assert result["testFailed"] == [True, "http://192.168.1.9:11434", True, 0], "what it said inline, nothing saved"
    assert result["testEmpty"]
