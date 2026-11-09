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
        self.processes.answers[("list", "--history")] = (0, "[]", "")  # a list (the JSON echo is an object)
        api = w.Api(lambda host, args, timeout=120, input=None:
                    hosts.run(host, args, input=input, timeout=timeout, runner=self.processes))
        self.svc = w.Service({HOST}, api=api, sessions=w.Sessions(clock=lambda: self.clock[0]))
        self.responses, self.token, self.csrf, self.codes, self.extra_secrets = [], None, None, [], []

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
        return [value for value in (PASSWORD, self.secret, self.token, *self.codes, *self.extra_secrets) if value]

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
    ("POST", "/starts/web/stop", {"confirm": True}, [["stop", "web"]]),
    ("POST", "/starts/web/delete", {"confirm": True}, [["delete", "web", "--yes"]]),
    ("GET", "/starts/web/review", None, [["resume", "web", "--review-home", "--json"]]),
    ("GET", "/approvals", None, [["approvals", "list", "--json"]]),
    ("POST", "/approvals/1700000000-1-2", {"decision": "allow", "scope": "always", "confirm": True},
     [["approvals", "allow", "1700000000-1-2", "--scope", "always"]]),
    ("POST", "/approvals/1700000000-1-2", {"decision": "deny", "scope": "once"},
     [["approvals", "deny", "1700000000-1-2", "--scope", "once"]]),
    ("POST", "/approvals/1700000000-1-2", {"decision": "deny", "scope": "always", "confirm": True},
     [["approvals", "deny", "1700000000-1-2", "--scope", "always"]]),
    ("POST", "/approvals/1700000000-1-2", {"decision": "allow", "scope": "run", "no_open": True},
     [["approvals", "allow", "1700000000-1-2", "--scope", "run", "--no-open"]]),
    ("GET", "/doctor", None, [["doctor", "--json"]]),
    ("GET", "/logs", None, [["logs", "--json"]]),
    ("GET", "/llm", None, [["llm", "list", "--json"]]),
    ("POST", "/llm", {"name": "gpu", "kind": "vllm", "url": "http://100.100.1.5:8000", "model": "qwen3"},
     [["llm", "add", "gpu", "--kind", "vllm", "--url", "http://100.100.1.5:8000", "--model", "qwen3"]]),
    ("GET", "/llm/gpu/models", None, [["llm", "models", "gpu", "--json"]]),
    ("GET", "/llm-status", None, [["llm", "status", "--json"]]),
    ("GET", "/llm/gpu/status", None, [["llm", "status", "gpu", "--json"]]),
    ("POST", "/llm-models", {"kind": "ollama", "url": "http://100.100.1.5:11434"},
     [["wizard", "models", "--kind=ollama", "--url=http://100.100.1.5:11434", "--json"]]),
    ("POST", "/llm-models", {"kind": "openai-compatible", "url": "https://gw.example.org/v1",
                              "ca": "/home/you/ca.pem", "api_key_secret": "GW_KEY"},
     [["wizard", "models", "--kind=openai-compatible", "--url=https://gw.example.org/v1", "--ca=/home/you/ca.pem",
       "--api-key-secret=GW_KEY", "--json"]]),
    ("POST", "/llm/gpu/check", {"agent": "codex"}, [["llm", "check", "gpu", "--agent", "codex"]]),
    ("DELETE", "/llm/gpu", {"confirm": True}, [["llm", "remove", "gpu"]]),
    ("GET", "/defaults", None, [["defaults", "show", "--json"]]),
    # not confirmed: compared with what the host has first (no step down: then set)
    ("POST", "/defaults", {"key": "telemetry", "value": "strict"}, [["defaults", "show", "--json"],
                                                                   ["defaults", "set", "telemetry", '"strict"']]),
    ("POST", "/defaults", {"key": "telemetry", "value": None}, [["defaults", "show", "--json"],
                                                               ["defaults", "unset", "telemetry"]]),
    ("POST", "/defaults", {"key": "mode", "value": "lax", "confirm": True}, [["defaults", "set", "mode", '"lax"']]),
    ("GET", "/net/web", None, [["net", "show", "web", "--json"]]),
    ("POST", "/net/web", {"op": "allow", "allow_host": ["pypi.org"], "allow_tcp": [], "host_ports": [3000],
                          "dns": False, "git_login": {"url": "https://github.com", "kind": "secret", "ref": "GH_PUSH"},
                          "confirm": True},
     [["net", "allow", "web", "--allow-host=pypi.org", "--allow-host-port=3000",
       "--git-token-secret=https://github.com=GH_PUSH"]]),
    ("POST", "/net/web", {"op": "revoke", "allow_host": ["pypi.org"], "allow_tcp": [], "host_ports": [], "dns": False},
     [["net", "revoke", "web", "--allow-host=pypi.org"]]),
    ("GET", "/workspaces/web", None, [["workspace", "list", "web", "--json"]]),
    ("POST", "/workspaces/web", {"directory": "/home/u/src/lib", "read_only": True},
     [["workspace", "add", "web", "/home/u/src/lib", "--ro"]]),
    ("POST", "/workspaces/web", {"directory": "/home/u/src/lib", "read_only": False, "confirm": True},
     [["workspace", "add", "web", "/home/u/src/lib"]]),
    ("POST", "/workspaces/web/remove", {"workspace": "/workspaces/lib", "force": True, "confirm": True},
     [["workspace", "remove", "web", "/workspaces/lib", "--force"]]),
    ("POST", "/workspaces/web", {"path": "/home/u/notes.md", "name": "notes", "read_only": True},
     [["workspace", "add", "web", "/home/u/notes.md", "--name=notes", "--ro"]]),
    ("POST", "/starts/web/mcp-live", {"server": "files", "confirm": True},
     [["mcp", "attach", "files", "web", "--live", "--json"]]),
    ("POST", "/starts/web/mcp-live", {"helper": "claude", "confirm": True},
     [["mcp", "attach", "--helper=claude", "web", "--live", "--json"]]),
    ("POST", "/starts/web/mcp-live/remove", {"server": "files", "confirm": True},
     [["mcp", "detach", "files", "web", "--live", "--json"]]),
    ("POST", "/starts/web/mcp-live/remove", {"helper": "codex", "confirm": True},
     [["mcp", "detach", "--helper=codex", "web", "--live", "--json"]]),
    ("GET", "/devices/web", None, [["devices", "list", "web", "--json"]]),
    ("POST", "/devices/web", {"device": "usb:2e8a:000a", "confirm": True}, [["devices", "add", "web", "usb:2e8a:000a"]]),
    ("POST", "/devices/web/remove", {"device": "2"}, [["devices", "remove", "web", "2"]]),
    ("POST", "/limits/web", {"memory": "4g", "cpus": "2"}, [["limits", "web", "--show", "--json"],
                                                           ["limits", "web", "--memory=4g", "--cpus=2"]]),
    ("POST", "/limits/web", {"memory": "64g", "confirm": True}, [["limits", "web", "--memory=64g"]]),
    ("POST", "/limits/web", {"pids": "none", "confirm": True}, [["limits", "web", "--pids=none"]]),
    ("GET", "/limits/web", None, [["limits", "web", "--show", "--json"]]),
    ("GET", "/presets", None, [["presets", "list", "--json"]]),
    ("GET", "/vpn", None, [["vpn", "list", "--json"]]),
    ("POST", "/vpn", {"name": "mv", "kind": "mullvad", "secret": "MULLVAD", "location": "se-sto"},
     [["vpn", "add", "mv", "--mullvad", "--account-secret=MULLVAD", "--location=se-sto"]]),
    ("POST", "/vpn/mv/check", {}, [["vpn", "check", "mv"]]),
    ("DELETE", "/vpn/mv", {"confirm": True}, [["vpn", "remove", "mv"]]),
    ("GET", "/canaries", None, [["canaries", "log", "--json"]]),
    ("GET", "/canaries/web", None, [["canaries", "log", "web", "--json"]]),
    ("POST", "/test", {}, [["--version"], ["doctor", "--json"]]),
    ("GET", "/kits", None, [["kits", "list", "--json"]]),
    ("POST", "/kits", {"name": "mv", "agents": ["claude"]}, [["kits", "new", "--agent=claude", "--", "mv"]]),
    ("POST", "/kits/mv/add-skill", {"path": "/home/u/skills/review", "as": "", "agents": ["claude"], "create": False,
                                    "confirm": True},
     [["kits", "add-skill", "--agent=claude", "--", "mv", "/home/u/skills/review"]]),
    ("POST", "/kits/mv/new-skill", {"skill": "notes", "description": "when noting", "agents": [], "create": False},
     [["kits", "new-skill", "--description=when noting", "--", "mv", "notes"]]),
    ("POST", "/kits/mv/add-plugin", {"plugin": "egm@sandbox-ai", "agent": "claude", "source": "", "create": False,
                                     "confirm": True},
     [["kits", "add-plugin", "--agent=claude", "--", "mv", "egm@sandbox-ai"]]),
    ("POST", "/kits/mv/remove-skill", {"skill": "notes", "agents": ["claude"], "confirm": True},
     [["kits", "remove-skill", "--agent=claude", "--", "mv", "notes"]]),
    ("POST", "/kits/mv/remove-plugin", {"plugin": "egm@sandbox-ai", "agent": "claude", "confirm": True},
     [["kits", "remove-plugin", "--agent=claude", "--", "mv", "egm@sandbox-ai"]]),
    ("DELETE", "/kits/mv", {"confirm": True}, [["kits", "delete", "--yes", "--", "mv"]]),
    # the marketplace browser of the Kits page
    ("GET", "/marketplaces", None, [["plugins", "marketplaces", "--json"]]),
    ("POST", "/kits/mv/enable-plugin", {"plugin": "egm@sandbox-ai", "agent": "claude", "confirm": True},
     [["kits", "enable-plugin", "--agent=claude", "--", "mv", "egm@sandbox-ai"]]),
    ("POST", "/kits/mv/disable-plugin", {"plugin": "egm@sandbox-ai", "agent": "claude", "confirm": True},
     [["kits", "disable-plugin", "--agent=claude", "--", "mv", "egm@sandbox-ai"]]),
    ("POST", "/kits/mv/remove-marketplace", {"marketplace": "acme", "with_plugins": True, "confirm": True},
     [["kits", "remove-marketplace", "--with-plugins", "--", "mv", "acme"]]),
    ("POST", "/kits/mv/import-marketplace", {"marketplace": "cx", "from": "codex", "confirm": True},
     [["kits", "import-marketplace", "--from=codex", "--", "mv", "cx"]]),
    ("POST", "/kits/default/add-plugin", {"plugin": "egm@sandbox-ai", "agent": "claude", "confirm": True},
     [["kits", "add-plugin", "--agent=claude", "--", "default", "egm@sandbox-ai"]]),
    # management: rename, history, gc, untaint, answers given before, secrets
    ("POST", "/starts/web/rename", {"new": "web-2"}, [["rename", "web", "web-2"]]),
    ("GET", "/starts/web/history", None, [["list", "--history", "web", "--json"]]),
    ("GET", "/gc", None, [["gc", "--dry-run", "--json"]]),
    ("GET", "/gc/14", None, [["gc", "--dry-run", "--json", "--older-than=14"]]),
    ("POST", "/gc", {"older_than": 14, "confirm": True}, [["gc", "--json", "--older-than=14"]]),
    ("POST", "/net/web/untaint", {"confirm": True}, [["net", "untaint", "web"]]),
    ("GET", "/approvals-history", None, [["approvals", "history"]]),
    ("GET", "/remembered", None, [["approvals", "remembered", "--json"]]),
    ("POST", "/remembered/forget", {"project": "/srv/project", "entry": "pypi.org", "confirm": True},
     [["approvals", "forget", "/srv/project", "pypi.org"]]),
    ("GET", "/secrets", None, [["secrets", "list"]]),
    ("POST", "/secrets/GH_TOKEN", {"secret": "ghp-matrix-value", "confirm": True}, [["secrets", "set", "GH_TOKEN"]]),
    ("DELETE", "/secrets/GH_TOKEN", {"confirm": True}, [["secrets", "delete", "GH_TOKEN"]]),
    # the parity actions (Projects, MCP servers, Logins, Images, Activity and the rest)
    ("GET", "/projects", None, [["wizard", "trust", "--list", "--json"]]),
    ("POST", "/projects/review", {"path": "/srv/project/.sandbox-ai.json"},
     [["wizard", "trust", "/srv/project/.sandbox-ai.json", "--show", "--json"]]),
    ("POST", "/projects/trust", {"path": "/srv/project/.sandbox-ai.json", "digest": "b" * 64, "confirm": True},
     [["wizard", "trust", "/srv/project/.sandbox-ai.json", "--digest=" + "b" * 64]]),
    ("POST", "/projects/forget", {"path": "/srv/project/.sandbox-ai.json", "confirm": True},
     [["wizard", "forget", "/srv/project/.sandbox-ai.json"]]),
    ("GET", "/mcp", None, [["mcp", "list", "--json"]]),
    ("POST", "/mcp", {"entry": "hub:capa", "unsigned": True, "confirm": True}, [["mcp", "add", "--unsigned", "hub:capa"]]),
    ("POST", "/mcp-catalog", {"catalogs": []}, [["mcp", "catalog"]]),
    ("POST", "/mcp/files/start", {"vpn": "mv"}, [["mcp", "start", "--vpn=mv", "files"]]),
    ("POST", "/mcp/files/start", {"for": "web", "confirm": True}, [["mcp", "start", "--for=web", "--json", "files"]]),
    ("POST", "/mcp/files/stop", {"confirm": True}, [["mcp", "stop", "files"]]),
    ("GET", "/git-logins", None, [["git-logins", "list", "--json"]]),
    ("POST", "/git-logins", {"url": "https://github.com", "secret": "GH_TOKEN", "confirm": True},
     [["git-logins", "add", "--secret=GH_TOKEN", "https://github.com"]]),
    ("POST", "/git-logins/remove", {"url": "https://github.com", "confirm": True},
     [["git-logins", "remove", "https://github.com"]]),
    ("POST", "/git-logins/check", {}, [["git-logins", "check"]]),
    ("GET", "/publish-logins", None, [["publish-logins", "list", "--json"]]),
    ("GET", "/publish-logins/kinds", None, [["publish-logins", "kinds"]]),
    ("GET", "/login-status", None, [["login", "--status"]]),
    # (confirmed: one that replaces a login is asked first, review 5 L9; the check reads the list)
    ("POST", "/publish-logins", {"kind": "pypi", "secret": "PYPI", "confirm": True},
     [["publish-logins", "add", "--secret=PYPI", "pypi"]]),
    ("POST", "/publish-logins/check", {}, [["publish-logins", "check"]]),
    ("POST", "/publish-logins/pypi/remove", {"confirm": True}, [["publish-logins", "remove", "pypi"]]),
    ("GET", "/identities", None, [["identities", "list", "--json"]]),
    ("GET", "/identities/work", None, [["identities", "show", "--json", "work"]]),
    ("POST", "/identities", {"identity": "work", "email": "a@example.org"},
     [["identities", "add", "--email=a@example.org", "work"]]),
    ("POST", "/identities/work", {"name": "A B"}, [["identities", "set", "--name=A B", "work"]]),
    ("POST", "/identities/work/login", {"url": "https://github.com", "secret": "GH_WORK", "confirm": True},
     [["identities", "login", "--secret=GH_WORK", "work", "https://github.com"]]),
    ("POST", "/identities/work/publish", {"kind": "npm", "secret": "NPM_WORK", "confirm": True},
     [["identities", "publish", "--secret=NPM_WORK", "work", "npm"]]),
    ("POST", "/identities/work/logout", {"url": "https://github.com", "confirm": True},
     [["identities", "logout", "work", "https://github.com"]]),
    ("POST", "/identities/work/unpublish", {"kind": "npm", "confirm": True}, [["identities", "unpublish", "work", "npm"]]),
    ("DELETE", "/identities/work", {"confirm": True}, [["identities", "remove", "work"]]),
    ("POST", "/identities-check", {}, [["identities", "check"]]),
    ("POST", "/net/web/keep", {"all": False, "confirm": True}, [["net", "show", "web", "--keep"]]),
    ("GET", "/starts/all", None, [["list", "--all", "--json"]]),
    ("GET", "/presets/development", None, [["presets", "show", "--json", "development"]]),
    ("POST", "/presets/development/check", {"values": {}, "agent": "claude"},
     [["presets", "check", "--agent=claude", "development"]]),
    ("GET", "/preset-dirs", None, [["presets", "dirs", "list", "--json"]]),
    ("POST", "/preset-dirs", {"directory": "/srv/presets", "confirm": True}, [["presets", "dirs", "add", "/srv/presets"]]),
    ("POST", "/preset-dirs/remove", {"directory": "/srv/presets"}, [["presets", "dirs", "remove", "/srv/presets"]]),
    ("GET", "/canaries/web/list", None, [["canaries", "list", "web", "--json"]]),
    ("POST", "/canaries/web/tokens", {"confirm": True}, [["canaries", "list", "web", "--tokens", "--json"]]),
    ("POST", "/canaries/web/resume", {"confirm": True}, [["canaries", "resume", "web"]]),
    ("GET", "/kits/mv", None, [["kits", "show", "--json", "--", "mv"]]),
    ("POST", "/kits/mv/add-hook", {"file": "/srv/hooks/guard.sh", "agents": [], "confirm": True},
     [["kits", "add-hook", "--", "mv", "/srv/hooks/guard.sh"]]),
    ("POST", "/kits/mv/remove-hook", {"file": "guard.sh", "agents": [], "confirm": True},
     [["kits", "remove-hook", "--", "mv", "guard.sh"]]),
    ("GET", "/llm/gpu", None, [["llm", "show", "gpu", "--json"]]),
    ("GET", "/vpn/mv", None, [["vpn", "show", "mv", "--json"]]),
    ("POST", "/logs", {"directory": "/srv/logs"}, [["logs", "--json", "/srv/logs"]]),
    ("POST", "/privacy-check", {"paths": ["/srv/project"]}, [["privacy-check", "--json", "/srv/project"]]),
    ("POST", "/activity", {"tail": 20, "kinds": ["tool"]}, [["activity", "--json", "--tail=20", "--kind=tool"]]),
    ("GET", "/doctor/podman", None, [["doctor", "--json", "--backend=podman"]]),
    ("GET", "/version", None, [["version", "--verbose"]]),
    ("GET", "/images", None, [["images", "--json"]]),
    ("GET", "/demo-device", None, [["demo-device", "status"]]),
    ("POST", "/demo-device/start", {}, [["demo-device", "start"]]),
    ("POST", "/demo-device/stop", {}, [["demo-device", "stop"]]),
    # round 4: teams, the images' extensions and variants, replacements asked first, a kept allow
    ("GET", "/teams", None, [["team", "list"]]),
    ("GET", "/teams/mv", None, [["team", "show", "mv", "--json"]]),
    ("POST", "/teams/mv/post", {"agent": "codex", "task": "-rf the tests"}, [["team", "post", "--", "mv", "codex",
                                                                              "-rf the tests"]]),
    ("POST", "/teams/mv/stop", {"confirm": True}, [["team", "stop", "mv"]]),
    ("DELETE", "/teams/mv", {"confirm": True}, [["team", "delete", "mv", "--yes"]]),
    ("GET", "/images-history", None, [["images", "history", "--json"]]),
    ("POST", "/images-new", {"name": "mv", "from": "codex", "empty": True}, [["images", "new", "mv", "--from=codex",
                                                                              "--empty", "--json"]]),
    ("POST", "/images-recipe", {"recipe": "tor", "from": "claude", "as": "mv"},
     [["images", "recipe", "tor", "--from=claude", "--as=mv"]]),
    ("GET", "/images/mv", None, [["images", "extend", "mv", "--json"]]),
    ("GET", "/images/mv/packages", None, [["images", "packages", "mv", "--json"]]),
    ("GET", "/images/mv/history", None, [["images", "history", "mv", "--json"]]),
    ("POST", "/images/mv/extend", {"add_apt": ["jq"], "remove_npm": ["typescript"], "remove_files": ["/usr/bin/t"]},
     [["images", "extend", "mv", "--add-apt=jq", "--remove-npm=typescript", "--remove-file=/usr/bin/t", "--json"]]),
    ("POST", "/images/mv/extend", {"add_files": [{"source": "/srv/t.sh", "target": "/usr/local/bin/t"}],
                                   "confirm": True},
     [["images", "extend", "mv", "--add-file=/srv/t.sh:/usr/local/bin/t", "--json"]]),
    ("POST", "/images/mv/check", {}, [["images", "check", "mv", "--json"]]),
    ("POST", "/images/mv/remove", {"keep_image": True, "confirm": True}, [["images", "remove", "mv", "--keep-image"]]),
    ("POST", "/llm", {"name": "gpu", "kind": "vllm", "url": "https://gpu.example.org", "replace": True, "confirm": True},
     [["llm", "add", "gpu", "--kind", "vllm", "--url", "https://gpu.example.org", "--replace"]]),
    ("POST", "/vpn", {"name": "mv", "kind": "mullvad", "force": True, "confirm": True},
     [["vpn", "add", "mv", "--mullvad", "--force"]]),
    ("POST", "/net/web", {"op": "allow", "dns": True, "keep": True, "confirm": True},
     [["net", "allow", "web", "--dns", "--keep"]]),
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
                         ids=[f"{m} {p}" + (f" {b.get('decision') or b.get('op') or b.get('key') or ''}".rstrip()
                                            + (f" {b.get('scope') or b.get('value') or ''}".rstrip() if b else "")
                                            if b else "") for m, p, b, _ in ACTIONS])
def test_every_action_in_every_login_state_on_every_host(web, state, host, method, path, body, commands):
    headers = web.headers(state)
    web.processes.answers[("--version",)] = (0, "sandbox-ai 0.1.52\n", "")
    # privacy-check names each repository it was asked for, checked or not (an echo would be no answer)
    web.processes.answers[("privacy-check",)] = (0, json.dumps([{"repository": "/srv/project", "checked": True,
                                                                 "refused": None, "scanned": 2, "findings": []}]), "")
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
        if path.startswith("/secrets/") and method == "POST":
            # a secret's value: on standard input only, as the command reads it; never in argv, a response or the log
            assert [entry["input"] for entry in web.processes.log] == [body["secret"] + "\n"]
            assert all(body["secret"] not in arg for entry in web.processes.log for arg in entry["args"])
            web.extra_secrets.append(body["secret"])
        else:
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


# -- the questions of every host at once --

@pytest.mark.parametrize("state", STATES)
def test_the_questions_of_every_host_in_every_login_state(web, state):
    """GET /api/approvals: with a full login, `approvals list --json` on this machine and on ct1 (over SSH), each
    question with its host; nothing runs without one."""
    headers = web.headers(state)
    web.processes.answers[("approvals", "list")] = (0, json.dumps([{"id": "1700000000-1-2", "kind": "dns",
                                                                    "question": "look up pypi.org",
                                                                    "seconds_left": 30}]), "")
    status, out_headers, data = web.request("GET", "/api/approvals", None, headers)
    assert status == expected_status(state, "GET"), data
    assert out_headers["Cache-Control"] == "no-store"
    if status != 200:
        assert web.processes.log == []
    else:
        found = json.loads(data)
        assert [q["host"] for q in found["questions"]] == list(HOSTS) and found["errors"] == {}, "in the hosts' order"
        assert sorted(web.processes.args()) == sorted((host, ["approvals", "list", "--json"]) for host in HOSTS)
        assert {e["target"] for e in web.processes.log if e["host"] == "ct1"} == {REMOTE["ssh"]}
        # hosts.run gives the process what is left of its deadline (a shared SSH connection's start is spent out of it)
        assert all(w.APPROVALS_HOST_SECONDS - 1 < e["timeout"] <= w.APPROVALS_HOST_SECONDS for e in web.processes.log)
    web.assert_nothing_leaked()


@pytest.mark.parametrize("state", STATES)
def test_the_starts_of_every_host_in_every_login_state(web, state):
    """GET /api/starts: with a full login, `list --json` on this machine and on ct1 (over SSH), each start with its
    host and when its host's list was read; nothing runs without one."""
    headers = web.headers(state)
    web.processes.answers[("list", "--json")] = (0, json.dumps([{"name": "web", "agent": "claude", "state": "running",
                                                                 "host": "elsewhere"}]), "")
    status, out_headers, data = web.request("GET", "/api/starts", None, headers)
    assert status == expected_status(state, "GET"), data
    if status != 200:
        assert web.processes.log == []
    else:
        found = json.loads(data)
        assert [(s["host"], s["name"]) for s in found["starts"]] == [("local", "web"), ("ct1", "web")], \
            "each start with its own host (whatever it says)"
        assert found["errors"] == {} and set(found["seen"]) == {"local", "ct1"} and found["asking"] == []
        assert sorted(web.processes.args()) == sorted((host, ["list", "--json"]) for host in HOSTS)
        assert all(0 < e["timeout"] <= w.STARTS_HOST_SECONDS for e in web.processes.log), "each host its own limit"
    web.assert_nothing_leaked()


def test_a_damaged_hosts_file_still_asks_this_machine(web):
    """A hosts.json that cannot be read is named in errors ("hosts.json"); this machine's questions and starts come
    all the same."""
    headers = web.headers("full")
    web.processes.answers[("approvals", "list")] = (0, json.dumps([{"id": "1-1", "question": "q"}]), "")
    web.processes.answers[("list", "--json")] = (0, json.dumps([{"name": "web", "state": "running"}]), "")
    (w.module("hosts").config_file()).write_text("{not json")
    for path, field in (("/api/approvals", "questions"), ("/api/starts", "starts")):
        status, _, data = web.request("GET", path, None, headers)
        found = json.loads(data)
        assert status == 200 and [q["host"] for q in found[field]] == ["local"], (path, found)
        assert list(found["errors"]) == ["hosts.json"] and "not valid JSON" in found["errors"]["hosts.json"]
    assert {host for host, _ in web.processes.args()} == {"local"}


def test_a_host_that_fails_is_named_and_the_others_are_listed(web):
    headers = web.headers("full")
    web.processes.answers[("approvals", "list")] = (0, json.dumps([{"id": "1-1", "question": "q"}]), "")
    real = web.processes.__call__

    class Failing(Processes):
        def __call__(self, command, **kwargs):
            if command[0] == "ssh":
                self.log.append({"host": "ct1", "target": None, "args": [], "input": None, "timeout": None})
                return subprocess.CompletedProcess(command, 255, "", "ssh: connect to host 192.0.2.10 port 2222: "
                                                                       "No route to host")
            return real(command, **kwargs)
    failing = Failing()
    hosts = w.module("hosts")
    web.svc.api.runner = lambda host, args, timeout=120, input=None: hosts.run(host, args, input=input,
                                                                               timeout=timeout, runner=failing)
    status, _, data = web.request("GET", "/api/approvals", None, headers)
    found = json.loads(data)
    assert status == 200 and [q["host"] for q in found["questions"]] == ["local"]
    assert list(found["errors"]) == ["ct1"] and "No route to host" in found["errors"]["ct1"]
    web.assert_nothing_leaked()


# -- the service's own endpoints: the session, the hosts list --

SESSION_CALLS = [
    ("GET", "/api/me", None),
    ("POST", "/api/logout", {}),
    ("GET", "/api/hosts", None),
    ("GET", "/api/policy", None),
    ("POST", "/api/hosts", {"name": "ct2", "ssh": "root@192.0.2.20", "port": 22}),
    ("POST", "/api/hosts/ct1", {"description": "edited", "port": 2200, "confirm": True}),
    ("GET", "/api/hosts-activity", None),
    ("DELETE", "/api/hosts/ct1", {"confirm": True}),
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
    elif path == "/api/policy":
        assert json.loads(data)["confirm"] == w.NEEDS_CONFIRM
    elif path == "/api/hosts-activity":
        assert set(json.loads(data)) == {"local", "ct1"}, "from what was asked before: nothing is asked for it"
    elif method == "GET":
        assert [h["name"] for h in json.loads(data)] == ["local", "ct1"]
    elif path == "/api/hosts/ct1" and method == "POST":
        assert after["ct1"] == {**before["ct1"], "description": "edited", "port": 2200}
    elif method == "POST":
        assert set(after) == {"ct1", "ct2"} and after["ct2"]["ssh"] == "root@192.0.2.20"
    else:
        assert after == {}
    assert web.processes.log == [], "none of these runs sandbox-ai"
    web.assert_nothing_leaked()


# -- the pages: served in every state, holding nothing of a session --

PAGES = ["/", "/index.html", "/static/app.js", "/static/i18n.js", "/static/app.css", "/static/themes.json",
         "/static/icon.svg"]


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
        given = ["--workspaces-as-given"] if host == "local" else []  # a host's older sandbox-ai would not know it
        assert args == [agent, "--name", f"{agent}-1", "--workspace", "/srv/project", *given, "--preset",
                        "development", "--allow-pypi", "--mode", "high"]
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
    base["workspace"] = "/srv/app"
    for change in ({"agent": "bash"}, {"name": "--all"}, {"workspace": "relative/path"}, {"workspace": ""},
                   {"options": json.dumps({"--mode": "high"})}, {"options": json.dumps([1])}, {"options": "[not json"},
                   {"host": "nowhere"}, {"host": "Bad;id"}, {"kind": "shell"}, {"kind": "attach", "name": "-x"}):
        with pytest.raises(w.Refused) as refused:
            web.svc.websocket("/api/terminal?" + urllib.parse.urlencode({**base, **change}), sent, "192.0.2.1")
        assert refused.value.status == 400, change
    assert web.processes.log == []


# -- the interactive tools in a terminal: logins, the plugin store, image builds, a kit's hooks, the demo device --

TOOL_TERMINALS = [
    ({"kind": "login", "agent": "codex"}, ["login", "codex"]),
    ({"kind": "login", "agent": "claude", "logout": "1", "confirm": "1"}, ["login", "--logout", "claude"]),
    ({"kind": "plugins", "agent": "claude", "update": "1", "kits": "egm,review"},
     ["plugins", "--update", "--kit=egm", "--kit=review", "claude"]),
    ({"kind": "plugins", "agent": "claude", "marketplaces_only": "1"}, ["plugins", "--marketplaces-only", "claude"]),
    ({"kind": "build", "target": "opencode", "extension_only": "1"}, ["build", "--extension-only", "opencode"]),
    ({"kind": "build", "target": "vpn"}, ["build", "vpn"]),
    # review 5 W6: the project wizard in a terminal; build's --pin, --uv and --node
    ({"kind": "wizard"}, ["wizard", "new"]),
    ({"kind": "wizard", "path": "/srv/p/.sandbox-ai.json"}, ["wizard", "edit", "/srv/p/.sandbox-ai.json"]),
    ({"kind": "build", "target": "claude", "pin": "2.0.1", "uv": "0.5.1", "node": "node:22@sha256:abc"},
     ["build", "--pin", "2.0.1", "--uv", "0.5.1", "--node", "node:22@sha256:abc", "claude"]),
    ({"kind": "trust-hooks", "kit": "team", "agents": "claude", "confirm": "1"},
     ["kits", "trust-hooks", "--agent=claude", "--", "team"]),
    ({"kind": "trust-hooks", "kit": "team", "revoke": "1"}, ["kits", "trust-hooks", "--revoke", "--", "team"]),
    ({"kind": "demo-device", "port": "5555"}, ["demo-device", "start", "--port=5555"]),
    ({"kind": "team-start", "team": "review", "agents": "claude,codex", "workspace": "/srv/project",
      "options": json.dumps(["--mode", "high"])},
     ["team", "start", "review", "--agents=claude,codex", "--workspace=/srv/project", "--", "--mode", "high"]),
    ({"kind": "team-start", "team": "duo", "agents": "opencode,claude,codex", "workspace": "~/src/p"},
     ["team", "start", "duo", "--agents=opencode,claude,codex", "--workspace=~/src/p", "--"]),
]


@pytest.mark.parametrize("host", HOSTS)
@pytest.mark.parametrize("state", STATES)
@pytest.mark.parametrize("sent,args", TOOL_TERMINALS, ids=[" ".join(a) for _, a in TOOL_TERMINALS])
def test_the_tool_terminals_in_every_login_state_on_every_host(web, state, host, sent, args):
    import urllib.parse
    headers = web.headers(state)
    target = "/api/terminal?" + urllib.parse.urlencode({"host": host, "csrf": headers.get("X-CSRF-Token", "none"),
                                                        **sent})
    if state != "full":
        with pytest.raises(w.Refused) as refused:
            web.svc.websocket(target, {"Host": HOST, "Origin": ORIGIN, **headers}, "192.0.2.1")
        assert refused.value.status == {"wrong-host": 421, "wrong-origin": 403, "wrong-csrf": 403}.get(state, 401)
        return
    argv, _ = web.svc.websocket(target, {"Host": HOST, "Origin": ORIGIN, **headers}, "192.0.2.1")
    found_host, _, found = sandbox_args(argv)
    assert (found_host, found) == (host, args)
    assert argv[1] == "-tt" if host == "ct1" else True, "a terminal: a tty over SSH"
    web.assert_nothing_leaked()


@pytest.mark.parametrize("host", HOSTS)
def test_a_tool_terminal_that_is_not_one_or_not_confirmed_is_refused(web, host):
    import urllib.parse
    headers = web.headers("full")
    sent = {"Host": HOST, "Origin": ORIGIN, **headers}
    for query, confirm in (({"kind": "login", "agent": "bash"}, None), ({"kind": "login", "agent": "--status"}, None),
                           ({"kind": "login", "agent": "claude", "logout": "1"}, "login-logout"),
                           ({"kind": "login", "agent": "claude", "logout": "yes", "confirm": "1"}, None),
                           ({"kind": "plugins", "kits": "--x"}, None), ({"kind": "plugins", "agent": "-s"}, None),
                           ({"kind": "plugins", "marketplaces_only": "yes"}, None),
                           ({"kind": "build", "target": "--pin"}, None), ({"kind": "build", "target": "Claude"}, None),
                           ({"kind": "build", "target": "team", "extension_only": "1"}, None),
                           ({"kind": "build", "target": "all", "pin": "1.2.3"}, None),
                           ({"kind": "wizard", "path": "relative"}, None), ({"kind": "wizard", "path": "--x"}, None),
                           ({"kind": "build", "target": "claude", "pin": "--x"}, None),
                           ({"kind": "build", "target": "vpn", "uv": "0.5"}, None),
                           ({"kind": "build", "target": "claude", "node": "-x"}, None),
                           ({"kind": "trust-hooks", "kit": "team"}, "kit-hooks-trust"),
                           ({"kind": "trust-hooks", "kit": "--yes", "confirm": "1"}, None),
                           ({"kind": "trust-hooks", "kit": "team", "agents": "bash", "confirm": "1"}, None),
                           ({"kind": "demo-device", "port": "80"}, None), ({"kind": "demo-device", "port": "-1"}, None),
                           ({"kind": "images"}, None),
                           ({"kind": "team-start", "team": "--all", "agents": "claude,codex"}, None),
                           ({"kind": "team-start", "team": "Review", "agents": "claude,codex"}, None),
                           ({"kind": "team-start", "team": "r", "agents": "claude"}, None),
                           ({"kind": "team-start", "team": "r", "agents": "claude,claude"}, None),
                           ({"kind": "team-start", "team": "r", "agents": "claude,bash"}, None),
                           ({"kind": "team-start", "team": "r", "agents": "claude,codex", "workspace": "rel"}, None),
                           ({"kind": "team-start", "team": "r", "agents": "claude,codex"}, None),  # review 5 M3
                           ({"kind": "team-start", "team": "r", "agents": "claude,codex", "options": "[1]"}, None),
                           ({"kind": "team-start", "team": "r", "agents": "claude,codex", "options": "{"}, None)):
        with pytest.raises(w.Refused) as refused:
            web.svc.websocket("/api/terminal?" + urllib.parse.urlencode({"host": host, "csrf": web.csrf, **query}),
                              sent, "192.0.2.1")
        assert refused.value.status == 400 and refused.value.confirm == confirm, query
    assert web.processes.log == []


# -- a host's install: this machine's sandbox-ai, which reaches the host over SSH --

INSTALL_CASES = [
    ({"mode": "user"}, ["--user", "--from-here"]),
    ({"mode": "user", "source": "download", "version": "0.1.80"}, ["--user", "--download", "--version=0.1.80"]),
    ({"mode": "system", "sudo": True}, ["--system", "--sudo", "--from-here"]),
    ({"mode": "system", "sudo": False, "prefix": "/opt/sai"}, ["--system", "--prefix=/opt/sai", "--no-sudo",
                                                               "--from-here"]),
    ({"mode": "system", "source": "download"}, ["--system", "--download"]),
]


@pytest.mark.parametrize("state", STATES)
@pytest.mark.parametrize("sent,args", INSTALL_CASES, ids=[" ".join(a) for _, a in INSTALL_CASES])
def test_a_host_install_in_every_login_state(web, state, sent, args):
    """POST /api/hosts/ct1/install: with a full login and its confirmation, `sandbox-ai hosts install ct1 ...
    --json` on this machine (never on ct1 itself, which may have no sandbox-ai), with room for its steps; GET
    /api/hosts/ct1/install says how it went. Nothing runs without a full login."""
    headers = web.headers(state)
    status, _, data = web.request("POST", "/api/hosts/ct1/install", {**sent, "confirm": True}, headers)
    assert status == expected_status(state, "POST"), data
    if status != 200:
        assert web.processes.log == []
    else:
        assert web.processes.args() == [("local", ["hosts", "install", "ct1", *args, "--json"])]
        assert web.processes.log[0]["timeout"] >= 1800 and web.processes.log[0]["input"] is None
        assert json.loads(data) == {"ran": ["hosts", "install", "ct1", *args, "--json"]}
        status, _, data = web.request("GET", "/api/hosts/ct1/install", None, headers)
        assert status == 200 and json.loads(data)["last"]["result"] == {"ran": ["hosts", "install", "ct1", *args,
                                                                                 "--json"]}
    status, _, data = web.request("GET", "/api/hosts/ct1/install", None, headers)
    assert status == expected_status(state, "GET"), data
    web.assert_nothing_leaked()


def test_a_host_install_needs_its_confirmation_and_never_runs_on_this_machine(web):
    headers = web.headers("full")
    for body, status in (({"mode": "user"}, 400), ({"mode": "user", "confirm": "true"}, 400),
                         ({"mode": "system", "prefix": "/a/../b", "confirm": True}, 400),
                         ({"mode": "user", "version": "0.1.80", "confirm": True}, 400),
                         ({"mode": "admin", "confirm": True}, 400)):
        assert web.request("POST", "/api/hosts/ct1/install", body, headers)[0] == status, body
    assert web.request("POST", "/api/hosts/local/install", {"mode": "user", "confirm": True}, headers)[0] == 400
    assert web.processes.log == []


@pytest.mark.parametrize("state", STATES)
def test_the_install_terminal_in_every_login_state(web, state):
    """The terminal where sudo asks for its password: this machine's hosts install (no --json), in a tty; its query
    carries the page's confirmation."""
    import urllib.parse
    headers = web.headers(state)
    sent = {"Host": HOST, "Origin": ORIGIN, **headers}
    query = {"host": "ct1", "csrf": headers.get("X-CSRF-Token", "none"), "kind": "install", "mode": "system",
             "prefix": "/opt/sai", "sudo": "1", "source": "here", "confirm": "1"}
    if state != "full":
        with pytest.raises(w.Refused):
            web.svc.websocket("/api/terminal?" + urllib.parse.urlencode(query), sent, "192.0.2.1")
        return
    argv, _ = web.svc.websocket("/api/terminal?" + urllib.parse.urlencode(query), sent, "192.0.2.1")
    assert sandbox_args(argv) == ("local", None, ["hosts", "install", "ct1", "--system", "--prefix=/opt/sai", "--sudo",
                                                  "--from-here"])
    for change in ({"confirm": ""}, {"prefix": "/opt/../x"}, {"host": "local"}):
        with pytest.raises(w.Refused) as refused:
            web.svc.websocket("/api/terminal?" + urllib.parse.urlencode({**query, **change}), sent, "192.0.2.1")
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
             "rows": "30", "workspace": "/srv/app", "options": json.dumps(["--allow-pypi"])}
    argv, _ = web.svc.websocket("/api/terminal?" + urllib.parse.urlencode(query), {"Host": HOST, "Origin": ORIGIN,
                                                                                    **headers}, "192.0.2.1")
    # (on this machine the start is as given: review 5 M5)
    given = ["--workspaces-as-given"] if host == "local" else []
    assert sandbox_args(argv)[2] == [agent, "--name", name, "--workspace", "/srv/app", *given, "--allow-pypi"]
    web.processes.answers[("list",)] = (0, json.dumps([{"name": name, "agent": agent, "state": "running"}]), "")
    question = {"id": "1700000000-7-1", "run": name, "kind": "dns", "question": "look up pypi.org"}
    web.processes.answers[("approvals", "list")] = (0, json.dumps([question]), "")
    assert json.loads(web.request("GET", f"/api/hosts/{host}/starts", None, headers)[2])[0]["agent"] == agent
    questions = json.loads(web.request("GET", f"/api/hosts/{host}/approvals", None, headers)[2])
    for scope in ("once", "run", "always"):
        status, _, data = web.request("POST", f"/api/hosts/{host}/approvals/{questions[0]['id']}",
                                      {"decision": "allow", "scope": scope, **({"confirm": True} if scope == "always"
                                                                               else {})}, headers)
        assert status == 200, data
    status, _, _ = web.request("POST", f"/api/hosts/{host}/net/{name}",
                               {"op": "allow", "allow_host": [], "allow_tcp": ["192.0.2.9:5432"], "host_ports": [],
                                "dns": True, "confirm": True}, headers)
    assert status == 200
    for action in ("stop", "delete"):
        status, _, data = web.request("POST", f"/api/hosts/{host}/starts/{name}/{action}",
                                      {"confirm": True}, headers)
        assert status == 200, data
    assert web.processes.args() == [(host, args) for args in (
        ["list", "--json"], ["approvals", "list", "--json"],
        ["approvals", "allow", "1700000000-7-1", "--scope", "once"],
        ["approvals", "allow", "1700000000-7-1", "--scope", "run"],
        ["approvals", "allow", "1700000000-7-1", "--scope", "always"],
        ["net", "allow", name, "--allow-tcp=192.0.2.9:5432", "--dns"],
        ["stop", name], ["delete", name, "--yes"])]
    timeouts = [entry["timeout"] for entry in web.processes.log]
    within = lambda found, limit: limit - 1 < found <= limit  # what is left of each call's deadline (hosts.run)
    assert all(within(t, w.module("approvals").VERIFY_WAIT + 60) for t in timeouts[2:5]) and within(timeouts[6], 180)
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
    for method, path in re.findall(rf'api\("(GET|POST|DELETE)", (hostPath\((?:{literal})(?:, [A-Za-z_.]+)?\)|{literal})',
                                   script):
        prefix = "/api/hosts/X" if path.startswith("hostPath(") else ""
        path = re.sub(r", [A-Za-z_.]+\)$", ")", path)[len("hostPath("):-1] if prefix else path
        calls.add((method, prefix + re.sub(r"\$\{[^}]*\}", "X", path.strip("\"`"))))
    covered = {(m, "/api/hosts/X" + re.sub(r"/(web|gpu|mv|1700000000-1-2|GH_TOKEN|14|work|development|files)(?=/|$)", "/X", p))
               for m, p, _, _ in ACTIONS}
    covered |= {(m, re.sub(r"/ct1$", "/X", p)) for m, p, _ in SESSION_CALLS} | {("POST", "/api/login")}
    covered |= {("GET", "/api/approvals")}  # test_the_questions_of_every_host_in_every_login_state
    covered |= {("POST", "/api/hosts/X/starts/X/X")}  # ${action}: pause, resume, stop, delete above
    covered |= {("POST", "/api/hosts/X/kits/X/X"),  # ${action}: add-skill, new-skill, add-plugin above
                ("POST", "/api/hosts/X/kits/X/remove-X")}  # remove-${kind}: remove-skill, remove-plugin above
    # Tested on their own in test_web.py: a host's update runs through the updater (not one command), and an MCP
    # server attached to a start (test_an_mcp_server_is_attached_to_a_start).
    covered |= {("POST", "/api/hosts/X/update"), ("POST", "/api/hosts/X/mcp/X/attach")}
    # test_a_host_install_in_every_login_state (run by this machine's sandbox-ai, about the host)
    covered |= {("POST", "/api/hosts/X/install"), ("GET", "/api/hosts/X/install")}
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
    sure = {"confirm": True}
    assert https("POST", stop, sure)[0] == 401
    assert https("POST", "/api/login", {"user": "admin", "password": PASSWORD})[0] == 400
    code = w.totp(server["secret"], int(time.time() // 30))
    status, headers, data = https("POST", "/api/login", {"user": "admin", "password": PASSWORD, "code": code})
    assert status == 200, data
    cookie, csrf = headers["Set-Cookie"].split(";")[0], json.loads(data)["csrf"]
    for changed, wanted in (({"X-CSRF-Token": "x"}, 403), ({"Origin": "https://evil.example"}, 403),
                            ({"Host": "evil.example"}, 421), ({"Cookie": None}, 401)):
        assert https("POST", stop, sure, {"Cookie": cookie, "X-CSRF-Token": csrf, **changed})[0] == wanted, changed
    assert https("POST", stop, {}, {"Cookie": cookie, "X-CSRF-Token": csrf})[0] == 400, "a stop is confirmed"
    assert server["processes"].log == []
    status, headers, data = https("POST", stop, sure, {"Cookie": cookie, "X-CSRF-Token": csrf})
    assert status == 200 and server["processes"].args() == [("local", ["stop", "web"])]
    assert headers["Content-Security-Policy"].startswith("default-src 'self'") and headers["X-Frame-Options"] == "DENY"
    audit = (w.state_dir() / "audit.jsonl").read_text()
    assert PASSWORD not in audit and code not in audit and cookie.partition("=")[2] not in audit and csrf not in audit
