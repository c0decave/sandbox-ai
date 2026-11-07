"""The web interface as a matrix: every endpoint (the pages, every action of the API the page calls, the terminal
WebSocket) x every login state (none, a password without its code, password and code, a session that expired, a
wrong CSRF token, another origin, an unknown Host) x every host (this machine, a host of `sandbox-ai hosts` over SSH)
x the agent where a start is involved.

Everything runs through the real code - Service.handle and Service.websocket, the Api, hosts.argv and hosts.run (the
SSH command line, the remote command's quoting) - down to the process that would be started: only that is faked
(Processes records the command line and answers like sandbox-ai). Asserted: the status codes, that nothing runs
without a full login, that a CSRF token or origin that does not match is refused, the exact sandbox-ai arguments a
start, an answer or a stop produces on each host, and that no password, code, TOTP secret, session token or CSRF
token appears where it does not belong (responses, the audit log)."""
import importlib.util
import json
from pathlib import Path
import shlex
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("webui_for_matrix", ROOT / "sandbox" / "webui.py")
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)
FAST = {"n": 1 << 10, "r": 8, "p": 1}  # quick scrypt for the tests (the real one: 2**15)
PASSWORD = "correct horse battery staple"
HOST, ORIGIN = "localhost:8443", "https://localhost:8443"
REMOTE = {"ssh": "admin@192.0.2.10", "port": 2222}
HOSTS = ("local", "ct1")
AGENTS = ("claude", "codex", "opencode")
STATES = ("none", "password-only", "full", "expired", "wrong-csrf", "wrong-origin", "wrong-host")


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    monkeypatch.setattr(w, "SCRYPT", FAST)
    return tmp_path


# -- the processes the service would start --

REMOTE_HEAD = "if command -v sandbox-ai >/dev/null 2>&1; then exec sandbox-ai "


def sandbox_args(command):
    """(host, target, sandbox-ai arguments) of a command line hosts.argv made: this machine's sandbox-ai, or ssh
    to a host of `sandbox-ai hosts` with its remote command (each argument quoted; both branches the same)."""
    if command[0] != "ssh":
        assert command[:3] == [sys.executable, "-I", str(ROOT / "sandbox" / "main.py")], command
        return "local", None, list(command[3:])
    assert command[1] in ("-T", "-tt") and "BatchMode=yes" in command and "StrictHostKeyChecking=accept-new" in command
    index = command.index("--")
    assert command[command.index("-p") + 1] == str(REMOTE["port"]) and len(command) == index + 3, command
    target, remote = command[index + 1], command[index + 2]
    assert remote.startswith(REMOTE_HEAD), remote
    quoted = remote[len(REMOTE_HEAD):remote.index("; else exec ")]
    assert remote.endswith(f'; else exec "$HOME/.local/bin/sandbox-ai" {quoted}; fi'), remote
    return "ct1", target, shlex.split(quoted)


class Processes:
    """The runner hosts.run gets (subprocess.run's place): what would be started, answered from ANSWERS
    ({first sandbox-ai arguments: (status, out, err)}); else a JSON echo of the arguments."""

    def __init__(self):
        self.log, self.answers = [], {}

    def __call__(self, command, input=None, capture_output=True, text=True, timeout=None, stdin=None, **_):
        host, target, args = sandbox_args(command)
        self.log.append({"host": host, "target": target, "args": args, "input": input, "timeout": timeout})
        for length in range(len(args), 0, -1):
            if tuple(args[:length]) in self.answers:
                return subprocess.CompletedProcess(command, *self.answers[tuple(args[:length])])
        return subprocess.CompletedProcess(command, 0, json.dumps({"ran": args}), "")

    def args(self):
        return [(entry["host"], entry["args"]) for entry in self.log]


class Web:
    """One service with a user, the host ct1 and a clock for its sessions; login() gets a session the way the page
    does, request() sends what the page sends."""

    def __init__(self):
        self.secret = w.new_totp_secret()
        with w.users_locked() as users:
            users["admin"] = {"password": w.hash_password(PASSWORD, **FAST), "totp": self.secret, "totp_counter": None}
        hosts = w.module("hosts")
        hosts.save({"ct1": hosts.check("ct1", REMOTE)})
        self.processes, self.clock = Processes(), [1000.0]
        api = w.Api(lambda host, args, timeout=120, input=None:
                    hosts.run(host, args, input=input, timeout=timeout, runner=self.processes))
        self.svc = w.Service({HOST}, api=api, sessions=w.Sessions(clock=lambda: self.clock[0]))
        self.responses, self.token, self.csrf, self.codes = [], None, None, []

    def code(self, offset=0):
        code = w.totp(self.secret, int(time.time() // 30) + offset)
        self.codes.append(code)
        return code

    def request(self, method, path, body=None, headers=None):
        sent = {"Host": HOST, "Origin": ORIGIN, "Content-Type": "application/json", **(headers or {})}
        sent = {k: v for k, v in sent.items() if v is not None}
        status, out_headers, data = self.svc.handle(method, path, sent, json.dumps(body).encode() if body is not None
                                                    else b"", "192.0.2.1")
        self.responses.append((method, path, status, dict(out_headers), data))
        return status, dict(out_headers), data

    def login(self, code=True):
        body = {"user": "admin", "password": PASSWORD, **({"code": self.code()} if code else {})}
        status, headers, data = self.request("POST", "/api/login", body)
        if status == 200:
            self.token = headers["Set-Cookie"].split(";")[0].partition("=")[2]
            self.csrf = json.loads(data)["csrf"]
        return status, headers, data

    def headers(self, state):
        """The headers a browser in STATE sends (logging in first where it has a session)."""
        if state == "none":
            return {}
        if state == "password-only":
            status, headers, data = self.login(code=False)
            assert status == 400 and "Set-Cookie" not in headers
            status, headers, _ = self.request("POST", "/api/login", {"user": "admin", "password": PASSWORD,
                                                                     "code": "000000"})
            assert status == 401 and "Set-Cookie" not in headers
            return {}
        status, headers, data = self.login()
        assert status == 200, data
        cookie = headers["Set-Cookie"]
        assert cookie.startswith(f"{w.COOKIE}=") and all(f in cookie for f in ("Secure", "HttpOnly", "SameSite=Strict",
                                                                               "Path=/"))
        sent = {"Cookie": cookie.split(";")[0], "X-CSRF-Token": self.csrf}
        if state == "expired":
            self.clock[0] += w.IDLE + 1
        elif state == "wrong-csrf":
            sent["X-CSRF-Token"] = "A" * len(self.csrf)
        elif state == "wrong-origin":
            sent["Origin"] = "https://evil.example"
        elif state == "wrong-host":
            sent.update({"Host": "evil.example", "Origin": "https://evil.example"})
        return sent

    def secrets(self):
        return [value for value in (PASSWORD, self.secret, self.token, *self.codes) if value]

    def assert_nothing_leaked(self):
        """No password, code or TOTP secret anywhere; the session token only in the Set-Cookie of a login; the CSRF
        token only in the bodies of a login and of /api/me; the audit log holds none of them."""
        for method, path, status, headers, data in self.responses:
            text = data.decode("utf-8", "replace")
            for value in self.secrets():
                assert value not in text, (path, value)
                assert all(value not in v for k, v in headers.items() if k != "Set-Cookie"), (path, value)
            if self.csrf and path not in ("/api/login", "/api/me"):
                assert self.csrf not in text, path
            assert "Access-Control-Allow-Origin" not in headers, "no other origin may read a response"
        audit = w.state_dir() / "audit.jsonl"
        if audit.exists():
            text = audit.read_text()
            for value in [*self.secrets(), self.csrf]:
                assert not value or value not in text, value


@pytest.fixture
def web():
    return Web()


# -- every action of the API, on each host, in each login state --

# (method, path below /api/hosts/HOST, body, the sandbox-ai commands it runs on that host); the page's own calls
# (app.js) are among them: test_every_call_of_the_page_is_in_the_matrix keeps it so.
ACTIONS = [
    ("GET", "/starts", None, [["list", "--json"]]),
    ("POST", "/starts/web/pause", {}, [["pause", "web"]]),
    ("POST", "/starts/web/resume", {}, [["resume", "web"]]),
    ("POST", "/starts/web/stop", {}, [["stop", "web"]]),
    ("POST", "/starts/web/delete", {"confirm": True}, [["delete", "web", "--yes"]]),
    ("GET", "/approvals", None, [["approvals", "list", "--json"]]),
    ("POST", "/approvals/1700000000-1-2", {"decision": "allow", "scope": "always"},
     [["approvals", "allow", "1700000000-1-2", "--scope", "always"]]),
    ("POST", "/approvals/1700000000-1-2", {"decision": "deny", "scope": "once"},
     [["approvals", "deny", "1700000000-1-2", "--scope", "once"]]),
    ("GET", "/doctor", None, [["doctor", "--json"]]),
    ("GET", "/logs", None, [["logs", "--json"]]),
    ("GET", "/llm", None, [["llm", "list", "--json"]]),
    ("POST", "/llm", {"name": "gpu", "kind": "vllm", "url": "http://100.100.1.5:8000", "model": "qwen3"},
     [["llm", "add", "gpu", "--kind", "vllm", "--url", "http://100.100.1.5:8000", "--model", "qwen3"]]),
    ("GET", "/llm/gpu/models", None, [["llm", "models", "gpu", "--json"]]),
    ("POST", "/llm/gpu/check", {"agent": "codex"}, [["llm", "check", "gpu", "--agent", "codex"]]),
    ("DELETE", "/llm/gpu", None, [["llm", "remove", "gpu"]]),
    ("GET", "/defaults", None, [["defaults", "show", "--json"]]),
    ("POST", "/defaults", {"key": "telemetry", "value": "strict"}, [["defaults", "set", "telemetry", '"strict"']]),
    ("POST", "/defaults", {"key": "telemetry", "value": None}, [["defaults", "unset", "telemetry"]]),
    ("GET", "/net/web", None, [["net", "show", "web", "--json"]]),
    ("POST", "/net/web", {"op": "allow", "allow_host": ["pypi.org"], "allow_tcp": [], "host_ports": [3000],
                          "dns": False, "git_login": {"url": "https://github.com", "kind": "secret", "ref": "GH_PUSH"}},
     [["net", "allow", "web", "--allow-host=pypi.org", "--allow-host-port=3000",
       "--git-token-secret=https://github.com=GH_PUSH"]]),
    ("POST", "/net/web", {"op": "revoke", "allow_host": ["pypi.org"], "allow_tcp": [], "host_ports": [], "dns": False},
     [["net", "revoke", "web", "--allow-host=pypi.org"]]),
    ("GET", "/presets", None, [["presets", "list", "--json"]]),
    ("GET", "/vpn", None, [["vpn", "list", "--json"]]),
    ("POST", "/vpn", {"name": "mv", "kind": "mullvad", "secret": "MULLVAD", "location": "se-sto"},
     [["vpn", "add", "mv", "--mullvad", "--account-secret=MULLVAD", "--location=se-sto"]]),
    ("POST", "/vpn/mv/check", {}, [["vpn", "check", "mv"]]),
    ("DELETE", "/vpn/mv", None, [["vpn", "remove", "mv"]]),
    ("GET", "/canaries", None, [["canaries", "log", "--json"]]),
    ("GET", "/canaries/web", None, [["canaries", "log", "web", "--json"]]),
    ("POST", "/test", {}, [["--version"], ["doctor", "--json"]]),
    ("GET", "/kits", None, [["kits", "list", "--json"]]),
    ("POST", "/kits", {"name": "mv", "agents": ["claude"]}, [["kits", "new", "--agent=claude", "--", "mv"]]),
    ("POST", "/kits/mv/add-skill", {"path": "/home/u/skills/review", "as": "", "agents": ["claude"], "create": False},
     [["kits", "add-skill", "--agent=claude", "--", "mv", "/home/u/skills/review"]]),
    ("POST", "/kits/mv/new-skill", {"skill": "notes", "description": "when noting", "agents": [], "create": False},
     [["kits", "new-skill", "--description=when noting", "--", "mv", "notes"]]),
    ("POST", "/kits/mv/add-plugin", {"plugin": "egm@sandbox-ai", "agent": "claude", "source": "", "create": False},
     [["kits", "add-plugin", "--agent=claude", "--", "mv", "egm@sandbox-ai"]]),
    ("POST", "/kits/mv/remove-skill", {"skill": "notes", "agents": ["claude"]},
     [["kits", "remove-skill", "--agent=claude", "--", "mv", "notes"]]),
    ("POST", "/kits/mv/remove-plugin", {"plugin": "egm@sandbox-ai", "agent": "claude"},
     [["kits", "remove-plugin", "--agent=claude", "--", "mv", "egm@sandbox-ai"]]),
    ("DELETE", "/kits/mv", {"confirm": True}, [["kits", "delete", "--yes", "--", "mv"]]),
]


def expected_status(state, method):
    if state == "wrong-host":
        return 421
    if method != "GET" and state == "wrong-origin":
        return 403  # before anything else, logged in or not
    if state in ("none", "password-only", "expired"):
        return 401
    if method != "GET" and state == "wrong-csrf":
        return 403
    return 200  # a GET needs no CSRF token (it changes nothing; another origin cannot read it)


@pytest.mark.parametrize("host", HOSTS)
@pytest.mark.parametrize("state", STATES)
@pytest.mark.parametrize("method,path,body,commands", ACTIONS,
                         ids=[f"{m} {p}" + (f" {b.get('decision') or b.get('op') or ''}".rstrip() if b else "")
                              for m, p, b, _ in ACTIONS])
def test_every_action_in_every_login_state_on_every_host(web, state, host, method, path, body, commands):
    headers = web.headers(state)
    web.processes.answers[("--version",)] = (0, "sandbox-ai 0.1.52\n", "")
    status, out_headers, data = web.request(method, f"/api/hosts/{host}{path}", body, headers)
    assert status == expected_status(state, method), data
    assert out_headers["Content-Type"] == "application/json" and out_headers["Cache-Control"] == "no-store"
    if status != 200:
        assert web.processes.log == [], "nothing runs without a full login, the right token and origin"
        assert "error" in json.loads(data)
    else:
        assert web.processes.args() == [(host, command) for command in commands]
        if host == "ct1":
            assert {entry["target"] for entry in web.processes.log} == {REMOTE["ssh"]}
        assert all(entry["input"] is None for entry in web.processes.log), "nothing on standard input"
    events = [json.loads(line) for line in (w.state_dir() / "audit.jsonl").read_text().splitlines()] \
        if (w.state_dir() / "audit.jsonl").exists() else []
    actions = [e for e in events if e["event"] == "action"]
    if method != "GET" and status == 200:
        assert [(e["method"], e["path"], e["user"]) for e in actions] == [(method, f"/api/hosts/{host}{path}", "admin")]
    else:
        assert actions == []
    if status in (403,) and state != "wrong-host":
        assert [e["event"] for e in events].count("refused") == 1, "a refused change of a session is audited"
    web.assert_nothing_leaked()


# -- the service's own endpoints: the session, the hosts list --

SESSION_CALLS = [
    ("GET", "/api/me", None),
    ("POST", "/api/logout", {}),
    ("GET", "/api/hosts", None),
    ("POST", "/api/hosts", {"name": "ct2", "ssh": "root@192.0.2.20", "port": 22}),
    ("DELETE", "/api/hosts/ct1", None),
]


@pytest.mark.parametrize("state", STATES)
@pytest.mark.parametrize("method,path,body", SESSION_CALLS, ids=[f"{m} {p}" for m, p, _ in SESSION_CALLS])
def test_the_session_and_hosts_endpoints_in_every_login_state(web, state, method, path, body):
    hosts = w.module("hosts")
    headers = web.headers(state)
    before = hosts.load()
    status, out_headers, data = web.request(method, path, body, headers)
    assert status == expected_status(state, method), data
    after = hosts.load()
    if status != 200:
        assert after == before, "the hosts file is changed by a full login only"
    elif path == "/api/me":
        me = json.loads(data)
        assert me["user"] == "admin" and me["csrf"] == web.csrf and "version" in me
    elif path == "/api/logout":
        assert "Max-Age=0" in out_headers["Set-Cookie"]
        assert web.request("GET", "/api/me", None, headers)[0] == 401, "the session is gone"
    elif method == "GET":
        assert [h["name"] for h in json.loads(data)] == ["local", "ct1"]
    elif method == "POST":
        assert set(after) == {"ct1", "ct2"} and after["ct2"]["ssh"] == "root@192.0.2.20"
    else:
        assert after == {}
    assert web.processes.log == [], "none of these runs sandbox-ai"
    web.assert_nothing_leaked()


# -- the pages: served in every state, holding nothing of a session --

PAGES = ["/", "/index.html", "/static/app.js", "/static/app.css", "/static/themes.json", "/static/icon.svg"]


@pytest.mark.parametrize("state", STATES)
def test_the_pages_in_every_login_state(web, state):
    headers = web.headers(state)
    for page in PAGES:
        status, out_headers, data = web.request("GET", page, None, headers)
        if state == "wrong-host":
            assert status == 421, page  # DNS rebinding: not even the page
            continue
        assert status == 200 and out_headers["Cache-Control"] == "no-cache", page
    for page in ("/static/../webui.py", "/static/vendor/../../hosts.py", "/static/users.json", "/api"):
        assert web.request("GET", page, None, headers)[0] in ((421,) if state == "wrong-host" else (404, 401)), page
    assert web.processes.log == []
    web.assert_nothing_leaked()


# -- the terminal WebSocket: attach, or a new start of each agent --

TERMINALS = [("attach", None)] + [("start", agent) for agent in AGENTS]


def terminal_query(web, kind, agent, host, csrf):
    query = {"host": host, "csrf": csrf, "cols": "120", "rows": "40", "kind": kind}
    if kind == "attach":
        query["name"] = "web"
    else:  # what the page's New start sends: the options one per line, as a JSON list
        query.update(agent=agent, name=f"{agent}-1", workspace="/srv/project",
                     options=json.dumps(["--preset", "development", "--allow-pypi", "--mode", "high"]))
    import urllib.parse
    return "/api/terminal?" + urllib.parse.urlencode(query)


@pytest.mark.parametrize("host", HOSTS)
@pytest.mark.parametrize("state", STATES)
@pytest.mark.parametrize("kind,agent", TERMINALS, ids=[k if not a else f"{k}-{a}" for k, a in TERMINALS])
def test_the_terminal_in_every_login_state_on_every_host(web, state, host, kind, agent):
    headers = web.headers(state)
    sent = {"Host": HOST, "Origin": ORIGIN, **headers}
    target = terminal_query(web, kind, agent, host, headers.get("X-CSRF-Token", "none"))
    if state in ("none", "password-only", "expired", "wrong-csrf", "wrong-origin", "wrong-host"):
        with pytest.raises(w.Refused) as refused:
            web.svc.websocket(target, sent, "192.0.2.1")
        assert refused.value.status == {"wrong-host": 421, "wrong-origin": 403, "wrong-csrf": 403}.get(state, 401)
        return
    argv, session = web.svc.websocket(target, sent, "192.0.2.1")
    assert session["user"] == "admin" and session["terminals"] == 0
    found_host, found_target, args = sandbox_args(argv)
    assert found_host == host and (argv[1] == "-tt" if host == "ct1" else True), "a terminal: a tty over SSH"
    if kind == "attach":
        assert args == ["attach", "web"]
    else:
        assert args == [agent, "--name", f"{agent}-1", "--workspace", "/srv/project", "--preset", "development",
                        "--allow-pypi", "--mode", "high"]
    events = [json.loads(line) for line in (w.state_dir() / "audit.jsonl").read_text().splitlines()]
    assert [(e["event"], e["host"], e["kind"], e["name"]) for e in events if e["event"] == "terminal"] == [
        ("terminal", host, kind, "web" if kind == "attach" else f"{agent}-1")]
    web.assert_nothing_leaked()


@pytest.mark.parametrize("agent", AGENTS)
@pytest.mark.parametrize("host", HOSTS)
def test_a_terminal_query_that_is_not_one_is_refused_before_anything_runs(web, host, agent):
    """Logged in: an agent the launcher does not have, a name that would be read as an option, a relative workspace,
    options that are not a list of strings, a host nobody configured."""
    headers = web.headers("full")
    sent = {"Host": HOST, "Origin": ORIGIN, **headers}
    import urllib.parse
    base = {"host": host, "csrf": web.csrf, "kind": "start", "agent": agent}
    for change in ({"agent": "bash"}, {"name": "--all"}, {"workspace": "relative/path"},
                   {"options": json.dumps({"--mode": "high"})}, {"options": json.dumps([1])}, {"options": "[not json"},
                   {"host": "nowhere"}, {"host": "Bad;id"}, {"kind": "shell"}, {"kind": "attach", "name": "-x"}):
        with pytest.raises(w.Refused) as refused:
            web.svc.websocket("/api/terminal?" + urllib.parse.urlencode({**base, **change}), sent, "192.0.2.1")
        assert refused.value.status == 400, change
    assert web.processes.log == []


# -- answers, starts and stops: the exact command on each host, for each agent's sandbox --

@pytest.mark.parametrize("agent", AGENTS)
@pytest.mark.parametrize("host", HOSTS)
def test_a_start_its_question_answered_and_its_stop(web, host, agent):
    """The workflow of the page: a new start in a terminal, the start in the list, its question answered once, for
    the run and always, its network widened, then stop and delete."""
    import urllib.parse
    headers = web.headers("full")
    name = f"{agent}-web"
    query = {"host": host, "csrf": web.csrf, "kind": "start", "agent": agent, "name": name, "cols": "100",
             "rows": "30", "options": json.dumps(["--allow-pypi"])}
    argv, _ = web.svc.websocket("/api/terminal?" + urllib.parse.urlencode(query), {"Host": HOST, "Origin": ORIGIN,
                                                                                    **headers}, "192.0.2.1")
    assert sandbox_args(argv)[2] == [agent, "--name", name, "--allow-pypi"]
    web.processes.answers[("list",)] = (0, json.dumps([{"name": name, "agent": agent, "state": "running"}]), "")
    question = {"id": "1700000000-7-1", "run": name, "kind": "dns", "question": "look up pypi.org"}
    web.processes.answers[("approvals", "list")] = (0, json.dumps([question]), "")
    assert json.loads(web.request("GET", f"/api/hosts/{host}/starts", None, headers)[2])[0]["agent"] == agent
    questions = json.loads(web.request("GET", f"/api/hosts/{host}/approvals", None, headers)[2])
    for scope in ("once", "run", "always"):
        status, _, data = web.request("POST", f"/api/hosts/{host}/approvals/{questions[0]['id']}",
                                      {"decision": "allow", "scope": scope}, headers)
        assert status == 200, data
    status, _, _ = web.request("POST", f"/api/hosts/{host}/net/{name}",
                               {"op": "allow", "allow_host": [], "allow_tcp": ["192.0.2.9:5432"], "host_ports": [],
                                "dns": True}, headers)
    assert status == 200
    for action in ("stop", "delete"):
        status, _, data = web.request("POST", f"/api/hosts/{host}/starts/{name}/{action}",
                                      {"confirm": True} if action == "delete" else {}, headers)
        assert status == 200, data
    assert web.processes.args() == [(host, args) for args in (
        ["list", "--json"], ["approvals", "list", "--json"],
        ["approvals", "allow", "1700000000-7-1", "--scope", "once"],
        ["approvals", "allow", "1700000000-7-1", "--scope", "run"],
        ["approvals", "allow", "1700000000-7-1", "--scope", "always"],
        ["net", "allow", name, "--allow-tcp=192.0.2.9:5432", "--dns"],
        ["stop", name], ["delete", name, "--yes"])]
    timeouts = [entry["timeout"] for entry in web.processes.log]
    assert timeouts[2:5] == [w.module("approvals").VERIFY_WAIT + 60] * 3 and timeouts[6] == 180
    web.assert_nothing_leaked()


@pytest.mark.parametrize("host", HOSTS)
@pytest.mark.parametrize("ident", ["--scope=always", "--scope", "-h", "--help", "1-2-3;id", "a b", "../x",
                                   "x" * 129])
def test_a_question_id_that_is_not_one_never_reaches_a_host(web, host, ident):
    import urllib.parse
    headers = web.headers("full")
    status, _, data = web.request("POST", f"/api/hosts/{host}/approvals/{urllib.parse.quote(ident, safe='')}",
                                  {"decision": "allow", "scope": "once"}, headers)
    assert status in (400, 404), data
    assert web.processes.log == []


@pytest.mark.parametrize("host", HOSTS)
def test_a_failing_host_says_why_without_its_secrets_or_escapes(web, host):
    headers = web.headers("full")
    web.processes.answers[("doctor",)] = (255, "", "ssh: connect to host 192.0.2.10 port 2222: Connection refused"
                                                   "\x1b]0;evil\x07")
    status, _, data = web.request("GET", f"/api/hosts/{host}/doctor", None, headers)
    error = json.loads(data)["error"]
    assert status == 502 and "Connection refused" in error and "\x1b" not in error and "\x07" not in error
    web.assert_nothing_leaked()


# -- the page itself: every call it makes is one of the matrix --

def test_every_call_of_the_page_is_in_the_matrix():
    """app.js calls the API with fixed paths (hostPath(...) below /api/hosts/HOST); each is an action above or a
    session endpoint, so no call of the page goes untested."""
    import re
    script = (ROOT / "sandbox" / "web" / "app.js").read_text()
    calls = set()
    literal = r'`[^`]*`|"[^"]*"'
    for method, path in re.findall(rf'api\("(GET|POST|DELETE)", (hostPath\((?:{literal})\)|{literal})', script):
        prefix = "/api/hosts/X" if path.startswith("hostPath(") else ""
        path = path[len("hostPath("):-1] if prefix else path
        calls.add((method, prefix + re.sub(r"\$\{[^}]*\}", "X", path.strip("\"`"))))
    covered = {(m, "/api/hosts/X" + re.sub(r"/(web|gpu|mv|1700000000-1-2)(?=/|$)", "/X", p)) for m, p, _, _ in ACTIONS}
    covered |= {(m, re.sub(r"/ct1$", "/X", p)) for m, p, _ in SESSION_CALLS} | {("POST", "/api/login")}
    covered |= {("POST", "/api/hosts/X/starts/X/X")}  # ${action}: pause, resume, stop, delete above
    covered |= {("POST", "/api/hosts/X/kits/X/X"),  # ${action}: add-skill, new-skill, add-plugin above
                ("POST", "/api/hosts/X/kits/X/remove-X")}  # remove-${kind}: remove-skill, remove-plugin above
    # Tested on their own in test_web.py: a host's update runs through the updater (not one command), and an MCP
    # server attached to a start (test_an_mcp_server_is_attached_to_a_start).
    covered |= {("POST", "/api/hosts/X/update"), ("POST", "/api/hosts/X/mcp/X/attach")}
    assert calls and calls <= covered, sorted(calls - covered)
    assert "new WebSocket(`wss://${location.host}/api/terminal?" in script


# -- over real HTTPS: the handler in front of the service, each login state once --

@pytest.fixture
def server(home, monkeypatch):
    import shutil
    import ssl
    import threading
    if not shutil.which("openssl"):
        pytest.skip("needs openssl")
    secret = w.new_totp_secret()
    with w.users_locked() as users:
        users["admin"] = {"password": w.hash_password(PASSWORD, **FAST), "totp": secret, "totp_counter": None}
    processes = Processes()
    hosts = w.module("hosts")
    real_api = w.Api

    def api(runner=None):
        return real_api(lambda host, args, timeout=120, input=None:
                        hosts.run(host, args, input=input, timeout=timeout, runner=processes))
    monkeypatch.setattr(w, "Api", api)
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
    yield {"port": port, "secret": secret, "context": context, "processes": processes}
    holder["server"].shutdown()


def test_over_https_each_login_state_once(server):
    import http.client
    port = server["port"]

    def https(method, path, body=None, headers=None):
        connection = http.client.HTTPSConnection("localhost", port, context=server["context"], timeout=30)
        sent = {"Origin": f"https://localhost:{port}", "Content-Type": "application/json", **(headers or {})}
        connection.request(method, path, body=json.dumps(body) if body is not None else None,
                           headers={k: v for k, v in sent.items() if v is not None})
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, dict(response.getheaders()), data
    stop = "/api/hosts/local/starts/web/stop"
    assert https("POST", stop, {})[0] == 401
    assert https("POST", "/api/login", {"user": "admin", "password": PASSWORD})[0] == 400
    code = w.totp(server["secret"], int(time.time() // 30))
    status, headers, data = https("POST", "/api/login", {"user": "admin", "password": PASSWORD, "code": code})
    assert status == 200, data
    cookie, csrf = headers["Set-Cookie"].split(";")[0], json.loads(data)["csrf"]
    for changed, wanted in (({"X-CSRF-Token": "x"}, 403), ({"Origin": "https://evil.example"}, 403),
                            ({"Host": "evil.example"}, 421), ({"Cookie": None}, 401)):
        assert https("POST", stop, {}, {"Cookie": cookie, "X-CSRF-Token": csrf, **changed})[0] == wanted, changed
    assert server["processes"].log == []
    status, headers, data = https("POST", stop, {}, {"Cookie": cookie, "X-CSRF-Token": csrf})
    assert status == 200 and server["processes"].args() == [("local", ["stop", "web"])]
    assert headers["Content-Security-Policy"].startswith("default-src 'self'") and headers["X-Frame-Options"] == "DENY"
    audit = (w.state_dir() / "audit.jsonl").read_text()
    assert PASSWORD not in audit and code not in audit and cookie.partition("=")[2] not in audit and csrf not in audit
