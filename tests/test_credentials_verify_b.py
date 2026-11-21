"""Independent verification B (2026-11-19): do credentials really reach a session - at its start and while it runs,
here and on another host - and nothing else?

Everything goes through the real entry point: `./sandbox-ai ...` as a subprocess of a user's host (live_fakes.Host:
only Podman, tmux, npm and ssh are faked on PATH). What a live container would do is stood in for as follows, and
only so:

  podman      the fake of live_fakes: the start goes as far as `podman run --pod ...` and then "runs" until the test
              lets it end (RELEASE below). The launcher's own work is real: run.json, the session directory (what the
              sandbox sees as /sandbox/session), the proxy's directory (/sandbox/proxy), the cleanup at the end.
  supervisor  runtime.control_server (the real one) as a thread of the test on the run's control.sock, the firewall
              a list (what nft would be told). The proxy's run directory (/run/sandbox-proxy) is a folder of the test.
  proxy       runtime.ProxyServer (the real one) started with the proxy.json the launcher wrote for the run, real TLS
              on both sides; the firewall's redirect is a fixed destination, so the port of each credential is the
              test server's (in the sandbox it is 443).
  git         the real git with the run's own environment (config.json "env": GIT_CONFIG_*). A login given during
              the run is the proxy's since 2026-11-19 (it was a file in the sandbox, readable by the agent - this
              review's finding 6): Git in the sandbox holds none, the run's proxy adds it on the wire (given_logins).
  DNS         names below .creds.test resolve to 192.0.2.77 for sandbox-ai (a python3 on PATH that answers
              socket.getaddrinfo for them): `net allow` looks a name up on the host.
  ssh         `sandbox-ai hosts run HOST -- ...`: an ssh on PATH that logs its command line and runs the remote
              command as the other host's user (another HOME) - no network, the remote shell command is the real one.

Not covered here (no live container): Firejail hiding /sandbox/proxy from the agent, nftables, the agents themselves
reading their login files."""
import base64
import http.server
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import threading
import time

import pytest

from live_fakes import Host
from test_proxy import EchoAuthorization, EchoHeaders, GitHandler, https_request, pki, rt, serve  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux") or not shutil.which("git")
                                or not shutil.which("openssl"), reason="needs Linux, git and openssl")

AGENT_SECRET = "agent-SECRET-0123456789-value"
API_SECRET = "sk-api-SECRET-abcdef0123456789"
GIT_TOKEN = "ghp_" + "G1t2T3k4" * 4 + "wxyz"
REGISTRY_TOKEN = "glpat-" + "REGISTRY0123456789abcd"  # (in two parts: no literal that looks like a real token)
PYPI_TOKEN = "pypi-AgEIcHlwaS5vcmcSECRET0123456789"
ENV_VALUE = "plain-ENV-VALUE-0123456789"
GIVEN_TOKEN = "gho_" + "R9u8N7t6" * 4 + "give"
SECOND_KEY = "sk-second-SECRET-0123456789ab"
EVERY_VALUE = (AGENT_SECRET, API_SECRET, GIT_TOKEN, REGISTRY_TOKEN, PYPI_TOKEN, ENV_VALUE, GIVEN_TOKEN, SECOND_KEY)

SHIM = '''"""python3 for sandbox-ai in these tests: names below .creds.test resolve to 192.0.2.77 (no DNS), and - when the
tests run as uid 0 - os.geteuid() says 1000 (live_fakes' uidshim). Nothing else changes."""
import os
import runpy
import socket
import sys

if os.environ.get("CVB_AS_USER"):
    os.geteuid = lambda: 1000
_real = socket.getaddrinfo


def _lookup(host, port, *args, **kwargs):
    if isinstance(host, str) and host.lower().endswith(".creds.test"):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.77", port or 0))]
    return _real(host, port, *args, **kwargs)


socket.getaddrinfo = _lookup
sys.executable = os.environ["FAKE_PYTHON"]
args = sys.argv[1:]
while args and args[0].startswith("-") and args[0] not in ("-c", "-m", "-"):
    args = args[1:]
if args[:1] == ["-c"]:
    sys.argv = ["-c", *args[2:]]
    exec(compile(args[1], "<string>", "exec"), {"__name__": "__main__"})
elif args[:1] == ["-m"]:
    sys.argv = [args[1], *args[2:]]
    runpy.run_module(args[1], run_name="__main__", alter_sys=True)
elif args[:1] == ["-"]:
    sys.argv = args
    exec(compile(sys.stdin.read(), "<stdin>", "exec"), {"__name__": "__main__"})
else:
    sys.argv = args
    runpy.run_path(args[0], run_name="__main__")
'''

# ssh for `sandbox-ai hosts run`: logs its command line (JSON lines, FAKE_SSH_LOG) and runs the remote command - the
# last argument, one string for the remote shell - as the user of the other host: the environment in the JSON file
# CVB_REMOTE_ENV. `ssh -G`, `-O check|exit|stop` (the shared connection) answer as the fake of live_fakes does.
SSH = r'''#!PYTHON
import json, os, subprocess, sys
args = sys.argv[1:]
with open(os.environ["FAKE_SSH_LOG"], "a") as stream:
    stream.write(json.dumps(args) + "\n")
if "-G" in args:
    print("hostname fake"); sys.exit(0)
if "-O" in args or "-M" in args or "-N" in args:
    sys.exit(255)  # no shared connection: each command connects on its own
remote = json.load(open(os.environ["CVB_REMOTE_ENV"]))
done = subprocess.run(["/bin/sh", "-c", args[-1]], env=remote, cwd=remote["HOME"],
                      stdin=sys.stdin if not sys.stdin.isatty() else subprocess.DEVNULL)
sys.exit(done.returncode)
'''


def user_host(directory, name="user"):
    """A user's host (live_fakes.Host) whose secrets are kept in sandbox-ai's private files (plain: no password to
    type in a test), with the python3 of SHIM on PATH and a `podman run` that ends when the test says so."""
    host = Host(directory, user=name)
    (host.bin / "cvb_shim.py").write_text(SHIM)
    (host.bin / "python3").write_text(f'#!/bin/sh\nexec {sys.executable} -I "{host.bin}/cvb_shim.py" "$@"\n')
    (host.bin / "python3").chmod(0o755)
    podman = (host.bin / "podman").read_text()
    wait = "    time.sleep(float(seconds or 0))\n"
    assert wait in podman, "live_fakes' podman changed: the sandbox's run is ended by a file here"
    (host.bin / "podman").write_text(podman.replace(wait, (
        "    _until = time.monotonic() + float(seconds or 0)\n"
        "    while time.monotonic() < _until and not os.path.exists(os.environ.get('CVB_RELEASE', '/nonexistent')):\n"
        "        time.sleep(0.05)\n")))
    host.env.update(SANDBOX_AI_SECRETS="file", SANDBOX_AI_SECRETS_PLAIN="1", FAKE_PYTHON=str(host.bin / "python3"),
                    CVB_RELEASE=str(host.directory / "release"), TMPDIR=str(host.directory / "tmp"))
    (host.directory / "tmp").mkdir()
    if os.geteuid() == 0:
        host.env["CVB_AS_USER"] = "1"
    host.add_image("localhost/claude-sandbox:latest")
    (host.workspace / "main.py").write_text("print('hello')\n")
    return host


def keep_secret(host, name, value):
    """`sandbox-ai secrets set NAME`, the value on standard input (never an argument)."""
    done = subprocess.run(host.command("secrets", "set", name), input=value + "\n", env=host.env, cwd=host.workspace,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert value not in done.stdout + done.stderr
    return done


class Started:
    """`sandbox-ai claude OPTIONS` of HOST while its sandbox "runs": the launcher process, the run's record
    (run.json), what the sandbox got mounted, and - supervise() - a supervisor answering on its control socket."""

    def __init__(self, host, *options, env=None, agent="claude"):
        self.host = host
        release = Path(host.env["CVB_RELEASE"])
        release.unlink(missing_ok=True)
        before = len(host.calls())
        self.process = host.popen(agent, "--no-tmux", "--no-checkpoint", *options,
                                  env={"FAKE_PODMAN_RUN": "0:120", **(env or {})})
        deadline = time.monotonic() + 90
        self.run_call = None
        while time.monotonic() < deadline and self.run_call is None:
            if self.process.poll() is not None:
                out, err = self.process.communicate()
                raise AssertionError(f"the start ended with {self.process.returncode}:\n{out}\n{err}")
            self.run_call = next((call for call in host.calls()[before:] if call[:1] == ["run"] and "--pod" in call),
                                 None)
            time.sleep(0.05)
        assert self.run_call is not None, "the start never reached podman run"
        self.mounts = {}
        for index, value in enumerate(self.run_call):
            if value == "--mount" and self.run_call[index + 1].startswith("type=bind,src="):
                source, rest = self.run_call[index + 1][len("type=bind,src="):].split(",dst=", 1)
                self.mounts[rest.split(",", 1)[0]] = Path(source)
        self.session, self.proxy_dir = self.mounts["/sandbox/session"], self.mounts.get("/sandbox/proxy")
        self.run_dir = self.mounts["/sandbox/control"]
        self.record = json.loads((self.run_dir / "run.json").read_text())
        self.run = self.record["run"]
        self.config = json.loads((self.session / "config.json").read_text())
        self.stop, self.thread, self.applied = threading.Event(), None, []

    def supervise(self, proxy_run, monkeypatch):
        """The run's supervisor: the real control server on its control socket; the proxy's run directory is
        PROXY_RUN (where the supervisor writes the keys and Git logins given during the run for the proxy)."""
        proxy_run.mkdir(mode=0o700, exist_ok=True)
        monkeypatch.setattr(rt, "PROXY_RUN", proxy_run)
        monkeypatch.setattr(rt, "_added_git_logins", {"stamp": None, "entries": []})
        monkeypatch.setattr(rt, "WILDCARDS", set())
        network = rt.RuntimeNetwork({"loopback": []}, proxy_run / "allow.json", self.applied.append)
        self.thread = threading.Thread(target=rt.control_server, args=(self.run_dir / "control.sock", network,
                                                                       self.stop), daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 10
        while not (self.run_dir / "control.sock").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert (self.run_dir / "control.sock").exists()
        return proxy_run

    def end(self):
        """The sandbox ends (its agent quit): the launcher's exit status and what it said."""
        self.stop.set()
        if self.thread is not None:
            self.thread.join(5)
        Path(self.host.env["CVB_RELEASE"]).touch()
        try:
            out, err = self.process.communicate(timeout=120)
        except subprocess.TimeoutExpired:
            self.process.kill()
            out, err = self.process.communicate()
            raise AssertionError(f"the launcher did not end:\n{out}\n{err}") from None
        return self.process.returncode, out, err

    def abandon(self):
        if self.process.poll() is None:
            self.end()


@pytest.fixture
def host(tmp_path):
    made = user_host(tmp_path / "host")
    started = []
    made.started = started
    yield made
    for run in started:
        run.abandon()


def start(host, *options, **kwargs):
    run = Started(host, *options, **kwargs)
    host.started.append(run)
    return run


def files_with(roots, values, skip=()):
    """{path: [which values]} of every regular file below ROOTS that holds one of VALUES (bytes compared: also in a
    binary file), without the paths below SKIP."""
    found = {}
    for root in roots:
        root = Path(root)
        if not root.exists():
            continue
        for path in ([root] if root.is_file() else root.rglob("*")):
            if path.is_symlink() or not path.is_file() or any(str(path).startswith(str(s)) for s in skip):
                continue
            try:
                data = path.read_bytes()
            except OSError:
                continue
            hit = [value for value in values if value.encode() in data]
            if hit:
                found[str(path)] = hit
    return found


def argv_text(host):
    """Every command line the fakes saw (podman, tmux, ssh), as one text."""
    return "\n".join(path.read_text() for path in (host.log_path, host.directory / "tmux.log",
                                                    host.directory / "ssh.log") if path.exists())


class RunProxy:
    """The run's connection proxy: runtime.ProxyServer with the proxy.json the launcher wrote for STARTED, unchanged
    but for the push checker (a process of the sandbox). A program of the sandbox that connects here is one whose
    connection the firewall redirected on its way to ADDRESS:PORT; the proxy reaches the test server on UPSTREAM_PORT
    instead of that address. PROXY_RUN: the proxy's run directory with the supervisor's allow-list (allow.json)."""

    def __init__(self, directory, pki, started, address, upstream_port, port=443, proxy_run=None):
        directory.mkdir(exist_ok=True)
        self.log = directory / "connections.jsonl"
        self.fd = os.open(self.log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        config = {**json.loads((started.proxy_dir / "proxy.json").read_text()), "push_check": False}
        if proxy_run is not None:
            config["allow_file"] = str(proxy_run / "allow.json")
        self.server = rt.ProxyServer(
            config, self.fd, None, run_dir=pki["run"], destination=lambda sock: (address, port),
            upstream=pki["upstream"],
            connect=lambda target, timeout=None: socket.create_connection(("127.0.0.1", upstream_port), timeout))
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.port = self.listener.getsockname()[1]
        self.server.serve([self.listener])

    def close(self):
        self.listener.close()
        os.close(self.fd)


def given_logins(proxy_run):
    """[(host, port, user, token)] of the Git logins the run's proxy was given during the run: the file its
    supervisor wrote for it in the proxy's run directory (never in what the sandbox sees)."""
    path = Path(proxy_run) / rt.ADDED_GIT_LOGINS
    if not path.exists():
        return []
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    return [(e["host"], e["port"], e["user"], e["token"]) for e in json.loads(path.read_text())["git_logins"]]


def pinned(started, name):
    """The address the launcher pinned NAME to for the run (its /etc/hosts): where the sandbox connects for it."""
    for line in (started.session / "hosts").read_text().splitlines():
        parts = line.split()
        if len(parts) >= 2 and name in parts[1:]:
            return parts[0]
    raise AssertionError(f"{name} is not in the run's /etc/hosts")


def upstream_for(tmp_path, name, handler):
    """An HTTPS server whose certificate is for NAME, signed by a CA the proxy trusts; (server, trust)."""
    run = tmp_path / f"upstream-{name}"
    rt.create_ca(run, tmp_path / f"upstream-ca-{name}", bundles=())
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(rt.server_certificate(run, name)), str(run / "leaf.key"))
    return serve(handler, context), ssl.create_default_context(cafile=str(run / "ca.pem"))


def wire(proxy, pki, name, request, values=EVERY_VALUE):
    """REQUEST (bytes) as a program of the sandbox sends it to https://NAME: what the server got (its credential
    headers; EchoHeaders keeps them). The answer the sandbox gets holds none of VALUES: a key the proxy put in is
    masked on the way back, even when the server echoes it."""
    EchoHeaders.got = None
    answer = https_request(proxy.port, pki["ca"], request, name=name)
    assert not any(value.encode() in answer for value in values), "the answer gave the sandbox a credential"
    return EchoHeaders.got if EchoHeaders.got is not None else json.loads(answer.split(b"\r\n\r\n", 1)[1])


# --- 1. the start: each credential where it is used, and nowhere else ---------------------------------------------

def prepare_credentials(host):
    keep_secret(host, "AGENT_SECRET", AGENT_SECRET)
    keep_secret(host, "API_SECRET", API_SECRET)
    keep_secret(host, "GT", GIT_TOKEN)
    keep_secret(host, "REG_TOKEN", REGISTRY_TOKEN)
    keep_secret(host, "PYPI_TOKEN", PYPI_TOKEN)
    added = host.run("git-logins", "add", "https://reg.creds.test", "--secret", "REG_TOKEN", "--user", "bob")
    assert added.returncode == 0, added.stderr
    added = host.run("publish-logins", "add", "pypi", "--secret", "PYPI_TOKEN")
    assert added.returncode == 0, added.stderr


START_OPTIONS = ("--secret-env", "AGENT_SECRET", "--secret-env", "RENAMED=AGENT_SECRET", "--env", "PLAIN_ENV",
                 "--api-key", "api.creds.test=API_KEY=API_SECRET", "--git-token-secret", "https://git.creds.test=GT",
                 "--publish", "pypi")


def test_a_start_puts_each_credential_where_it_is_used_and_nowhere_else(host, tmp_path, pki):
    prepare_credentials(host)
    plan = host.run("claude", "--dry-run", *START_OPTIONS, env={"PLAIN_ENV": ENV_VALUE})
    assert plan.returncode == 0, plan.stderr
    assert not any(value in plan.stdout + plan.stderr for value in EVERY_VALUE), "a dry run prints no value"
    planned = json.loads(plan.stdout)
    assert planned["secret_env"] == ["AGENT_SECRET", "RENAMED=AGENT_SECRET"] and planned["env_names"] == ["PLAIN_ENV"]
    assert [c["url"] for c in planned["git_credentials"]] == ["https://git.creds.test"]
    assert planned["git_logins"] == [{"url": "https://reg.creds.test", "user": "bob"}]

    run = start(host, *START_OPTIONS, env={"PLAIN_ENV": ENV_VALUE})
    env = run.config["env"]
    # The agent's variables: the secret and the host variable as values (that is what --secret-env and --env are),
    # the API key and the publish token as placeholders only, the Git token for Git's helper of that server.
    assert env["AGENT_SECRET"] == AGENT_SECRET and env["RENAMED"] == AGENT_SECRET and env["PLAIN_ENV"] == ENV_VALUE
    assert env["API_KEY"] == "sandbox-ai-api-key-placeholder-API_KEY"
    assert env["TWINE_PASSWORD"].startswith("sandbox-ai-publish-placeholder-") and env["TWINE_USERNAME"] == "__token__"
    in_sandbox = json.dumps(run.config)
    assert API_SECRET not in in_sandbox and PYPI_TOKEN not in in_sandbox and REGISTRY_TOKEN not in in_sandbox
    assert sorted(run.config["secret_names"]) == ["AGENT_SECRET", "PLAIN_ENV", "RENAMED"]  # redacted in the logs
    # What the sandbox sees of the session directory holds the values of its own variables in config.json only.
    assert set(files_with([run.session], EVERY_VALUE)) == {str(run.session / "config.json")}
    assert (run.session / "config.json").stat().st_mode & 0o077 == 0
    # The proxy's directory (never the agent's): the key, the publish token, the registry's Git login - and not the
    # --git-token-secret one, which without --token-proxy is Git's own in the sandbox.
    proxy = json.loads((run.proxy_dir / "proxy.json").read_text())
    assert (run.proxy_dir / "proxy.json").stat().st_mode & 0o077 == 0 and run.proxy_dir.stat().st_mode & 0o077 == 0
    assert [(k["host"], k["name"], k["key"]) for k in proxy["api_keys"]] == [
        ("api.creds.test", "API_KEY", API_SECRET), ("upload.pypi.org", "publish:pypi", PYPI_TOKEN)]
    assert proxy["git_tokens"] == [{"host": "reg.creds.test", "port": 443, "user": "bob", "loopback": False,
                                    "token": REGISTRY_TOKEN}]
    # No command line (podman's, tmux's) and nothing the start said holds a value.
    assert not any(value in argv_text(host) for value in EVERY_VALUE)
    # Git in the sandbox, with the run's own environment: the helper answers for the token's server only.
    git_env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "GIT_TERMINAL_PROMPT": "0",
               "GIT_CONFIG_NOSYSTEM": "1",
               **{k: v for k, v in env.items() if k.startswith(("GIT_CONFIG_", "SANDBOX_GIT_"))}}
    # (the helper for logins given during the run lives at /sandbox/runtime.py: not there on this host, Git goes on)

    def fill(url):
        done = subprocess.run(["git", "credential", "fill"], input=f"url={url}\n\n", text=True, capture_output=True,
                              env=git_env, timeout=30)
        return dict(line.split("=", 1) for line in done.stdout.splitlines() if "=" in line)
    assert fill("https://git.creds.test/o/r.git").get("password") == GIT_TOKEN
    assert fill("https://git.creds.test/o/r.git").get("username") == "x-access-token"
    assert "password" not in fill("https://other.creds.test/o/r.git")
    assert "password" not in fill("https://reg.creds.test/o/r.git")  # the registry's: the proxy's, never Git's here

    # On the wire, through the run's own proxy.json: the key in place of the placeholder for its host only.
    api, _ = upstream_for(tmp_path, "api.creds.test", EchoHeaders)
    other, _ = upstream_for(tmp_path, "evil.creds.test", EchoHeaders)
    api_port, other_port = api.server_address[1], other.server_address[1]
    placeholder = env["API_KEY"].encode()
    trust = ssl.create_default_context()
    for name in ("api.creds.test", "evil.creds.test"):
        trust.load_verify_locations(cafile=str(tmp_path / f"upstream-{name}" / "ca.pem"))
    pki = {**pki, "upstream": trust}
    assert pinned(run, "api.creds.test") == pinned(run, "upload.pypi.org") == "192.0.2.1"  # (the fake's lookup)
    through = RunProxy(tmp_path / "to-api", pki, run, pinned(run, "api.creds.test"), api_port)
    try:
        got = wire(through, pki, "api.creds.test", b"GET /v1/models HTTP/1.1\r\nHost: api.creds.test\r\n"
                   b"Authorization: Bearer " + placeholder + b"\r\nConnection: close\r\n\r\n")
        assert got["authorization"] == f"Bearer {API_SECRET}"
        got = wire(through, pki, "api.creds.test", b"GET /v1/models HTTP/1.1\r\nHost: api.creds.test\r\n"
                   b"X-API-Key: " + placeholder + b"\r\nConnection: close\r\n\r\n")
        assert got["x-api-key"] == API_SECRET
        log = through.log.read_text()
        assert API_SECRET not in log and '"api_keys": ["API_KEY"]' in log
    finally:
        through.close()
    through = RunProxy(tmp_path / "to-other", pki, run, pinned(run, "api.creds.test"), other_port)
    try:  # the same placeholder sent to another server: it stays the placeholder
        got = wire(through, pki, "evil.creds.test", b"GET /v1/models HTTP/1.1\r\nHost: evil.creds.test\r\n"
                   b"Authorization: Bearer " + placeholder + b"\r\nConnection: close\r\n\r\n")
        assert got["authorization"] == "Bearer " + placeholder.decode()
        # ... and a request there whose Host header names the key's host never reaches the server at all
        EchoHeaders.got = None
        refused = https_request(through.port, pki["ca"], b"GET /v1/models HTTP/1.1\r\nHost: api.creds.test\r\n"
                                b"Authorization: Bearer " + placeholder + b"\r\nConnection: close\r\n\r\n",
                                name="evil.creds.test")
        assert EchoHeaders.got is None and API_SECRET.encode() not in refused
        assert b"the Host header api.creds.test differs from the server name evil.creds.test" in refused
    finally:
        through.close()
        api.shutdown()
        other.shutdown()

    # The publish login: twine's Basic login (__token__ and the placeholder as its password) gets the token for
    # PyPI's upload host; the registry's Git login goes to Git's requests of its server (allowed by --allow-host).
    pypi, _ = upstream_for(tmp_path, "upload.pypi.org", EchoHeaders)
    both = ssl.create_default_context()
    both.load_verify_locations(cafile=str(tmp_path / "upstream-upload.pypi.org" / "ca.pem"))
    to_pypi = RunProxy(tmp_path / "to-pypi", {**pki, "upstream": both}, run, pinned(run, "upload.pypi.org"),
                       pypi.server_address[1])
    try:
        basic = base64.b64encode(f"__token__:{env['TWINE_PASSWORD']}".encode())
        got = wire(to_pypi, pki, "upload.pypi.org", b"GET /simple/ HTTP/1.1\r\nHost: upload.pypi.org\r\n"
                   b"Authorization: Basic " + basic + b"\r\nConnection: close\r\n\r\n")
        assert base64.b64decode(got["authorization"].split()[1]) == f"__token__:{PYPI_TOKEN}".encode()
        assert PYPI_TOKEN not in to_pypi.log.read_text()
    finally:
        to_pypi.close()
        pypi.shutdown()
    status, out, err = run.end()
    assert status == 0, err
    assert not any(value in out + err for value in EVERY_VALUE)
    # The run is over: its session directory, its proxy directory and its registration are gone, and with them
    # every value; what stays on the host holds them only in the secret store.
    assert not run.session.exists() and not run.proxy_dir.exists() and not run.run_dir.exists()
    store = host.home / ".local/share/sandbox-ai/secrets"
    left = files_with([host.directory], EVERY_VALUE, skip=[store])
    assert left == {}, left


def test_token_proxy_keeps_the_git_token_out_of_the_sandbox_and_adds_it_on_the_wire(host, tmp_path, pki, monkeypatch):
    keep_secret(host, "GT", GIT_TOKEN)
    run = start(host, "--git-token-secret", "https://alice@git.creds.test=GT", "--token-proxy")
    env = run.config["env"]
    assert GIT_TOKEN not in json.dumps(run.config) and files_with([run.session], [GIT_TOKEN]) == {}
    assert not any(key.startswith("SANDBOX_GIT_") for key in env) and "GH_TOKEN" not in env
    assert "credential.helper" not in [v for k, v in env.items() if k.startswith("GIT_CONFIG_KEY_")]
    proxy = json.loads((run.proxy_dir / "proxy.json").read_text())
    assert proxy["git_tokens"] == [{"host": "git.creds.test", "port": 443, "user": "alice", "loopback": False,
                                    "token": GIT_TOKEN}]
    assert run.record["runtime_git_tokens"] is False
    upstream, trust = upstream_for(tmp_path, "git.creds.test", EchoAuthorization)
    port = upstream.server_address[1]
    through = RunProxy(tmp_path / "to-git", {**pki, "upstream": trust}, run, pinned(run, "git.creds.test"), port)
    expected = "Basic " + base64.b64encode(f"alice:{GIT_TOKEN}".encode()).decode()

    def get(target, extra=b""):
        return wire(through, pki, "git.creds.test", b"GET " + target + b" HTTP/1.1\r\nHost: git.creds.test\r\n"
                    + extra + b"Connection: close\r\n\r\n")["authorization"]
    try:
        assert get(b"/o/r.git/info/refs?service=git-receive-pack") == expected
        assert get(b"/api/v1/user") is None, "the token is for Git's requests, not the agent's API calls"
        assert get(b"/o/r.git/info/refs?service=git-upload-pack", b"Authorization: Basic YTpi\r\n") == "Basic YTpi"
        assert GIT_TOKEN not in through.log.read_text() and expected not in through.log.read_text()
    finally:
        through.close()
        upstream.shutdown()
    # A login during the run goes to this run's proxy too (it was refused until 2026-11-19: such a run has no Git
    # helper - and needs none now); nothing enters the sandbox.
    run.supervise(tmp_path / "proxy-run", monkeypatch)
    given = host.run("git-logins", "give", run.run, "https://more.creds.test", "--secret", "GT")
    assert given.returncode == 0, given.stderr
    assert given_logins(tmp_path / "proxy-run") == [("more.creds.test", 443, "x-access-token", GIT_TOKEN)]
    assert not (run.session / "git-tokens.json").exists() and files_with([run.session], [GIT_TOKEN]) == {}
    status, _, err = run.end()
    assert status == 0, err
    assert files_with([host.directory], [GIT_TOKEN], skip=[host.home / ".local/share/sandbox-ai/secrets"]) == {}


# --- 2. while it runs: git-logins give / revoke, secrets give / revoke (the CLI) ----------------------------------

class LoginGitHandler(GitHandler):
    """Git's smart HTTP that wants a Basic login (EXPECTED: "user:token"); what was sent is kept in SEEN."""
    expected, seen = None, []

    def handle_git(self):
        sent = self.headers.get("Authorization")
        type(self).seen.append(sent)
        if sent != "Basic " + base64.b64encode(type(self).expected.encode()).decode():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="git"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        super().handle_git()

    do_GET = do_POST = handle_git


def git(*args, cwd=None, env=None):
    return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=60)


@pytest.fixture
def forge(tmp_path):
    """A Git server on this host's loopback (plain http, as a local Gitea) that wants the login alice:GIVEN_TOKEN,
    with one empty repository, and a clone of it with one commit to push."""
    root = tmp_path / "forge"
    root.mkdir()
    identity = {"PATH": os.environ["PATH"], "HOME": str(root), "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.org", "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@example.org"}
    assert git("init", "--bare", "-b", "main", str(root / "repo.git"), env=identity).returncode == 0
    git("config", "http.receivepack", "true", cwd=root / "repo.git", env=identity)
    handler = type("Forge", (LoginGitHandler,), {"root": root, "expected": f"alice:{GIVEN_TOKEN}", "seen": []})
    server = serve(handler)
    work = tmp_path / "clone"
    work.mkdir()
    assert git("init", "-b", "main", cwd=work, env=identity).returncode == 0
    (work / "a.txt").write_text("a\n")
    git("add", "a.txt", cwd=work, env=identity)
    assert git("commit", "-m", "first", cwd=work, env=identity).returncode == 0
    yield {"port": server.server_address[1], "handler": handler, "work": work, "identity": identity, "root": root}
    server.shutdown()


def sandbox_git_env(started, identity):
    """Git's environment in the sandbox of STARTED: the run's own GIT_CONFIG_* (config.json "env"). Its helper for
    logins given INTO the sandbox (/sandbox/runtime.py --internal-git-credential, reading /sandbox/session/
    git-tokens.json) runs as runtime.git_credential on the run's own file: it has nothing to answer with unless a
    login was given so (--in-sandbox)."""
    env = {**identity, "GIT_TERMINAL_PROMPT": "0"}
    for key, value in started.config["env"].items():
        if key.startswith(("GIT_CONFIG_", "SANDBOX_GIT_")):
            env[key] = value
    count = int(env["GIT_CONFIG_COUNT"])
    helpers = [i for i in range(count) if env[f"GIT_CONFIG_KEY_{i}"] == "credential.helper"]
    assert helpers and env[f"GIT_CONFIG_VALUE_{helpers[0]}"] == "!python3 -I /sandbox/runtime.py --internal-git-credential"
    tokens = started.session / "git-tokens.json"
    env[f"GIT_CONFIG_VALUE_{helpers[0]}"] = (
        f"!{sys.executable} -I -c 'import importlib.util, sys; spec = importlib.util.spec_from_file_location(\"rt\", "
        f"\"{ROOT / 'sandbox/runtime.py'}\"); rt = importlib.util.module_from_spec(spec); "
        f"spec.loader.exec_module(rt); sys.exit(rt.git_credential(sys.argv[1], path=\"{tokens}\"))'")
    hooks = [i for i in range(count) if env[f"GIT_CONFIG_KEY_{i}"] == "core.hooksPath"]
    for index in hooks:  # the privacy hooks are the sandbox's (/sandbox/session/privacy/hooks): not this test's
        env[f"GIT_CONFIG_VALUE_{index}"] = "/dev/null"
    return env


def test_a_git_login_given_during_the_run_makes_git_push_work_until_it_is_revoked(host, tmp_path, forge, pki,
                                                                                    monkeypatch):
    """A real `git push` of the sandbox's Git (the run's own environment, no credentials of its own) to an HTTPS Git
    server that wants alice's login, through the run's connection proxy (its proxy.json, the supervisor's allow-list
    and the logins the supervisor wrote for it): it works exactly while the login is given - and no token is ever in
    what the sandbox sees. (Until 2026-11-19 the login was a file in the sandbox's session directory.)"""
    keep_secret(host, "GH_PUSH", GIVEN_TOKEN)
    run = start(host)
    proxy_run = tmp_path / "proxy-run"
    proxy_run.mkdir(mode=0o700)
    (proxy_run / "allow.json").write_text(json.dumps(json.loads((run.proxy_dir / "proxy.json").read_text())["allow"]))
    run.supervise(proxy_run, monkeypatch)
    url = "https://git.creds.test"
    tokens = run.session / "git-tokens.json"
    given = host.run("git-logins", "give", run.run, url, "--secret", "GH_PUSH", "--user", "alice", "--keep")
    assert given.returncode == 0, given.stderr
    said = given.stdout + given.stderr
    assert GIVEN_TOKEN not in said and f"Git login for {url} (user alice)" in said
    assert f"({run.record['name']})" in said, "the message names the start, not only the run id"
    assert "the token does not enter the sandbox" in said
    # Where it is: with the run's proxy (the supervisor's file for it, 0600), named in the run's own list - and in
    # nothing the sandbox sees.
    assert given_logins(proxy_run) == [("git.creds.test", 443, "alice", GIVEN_TOKEN)]
    assert not tokens.exists() and files_with([run.session], [GIVEN_TOKEN]) == {}
    assert stat.S_IMODE((run.run_dir / "git-logins.json").stat().st_mode) == 0o600
    # The Git server behind TLS (the forge wants alice's login), reached through the run's proxy: in the sandbox the
    # firewall redirects the connection to git.creds.test:443 to the proxy; here Git is pointed at the proxy's port
    # and names the server as it would there (Host: git.creds.test).
    run_ca = tmp_path / "upstream-git.creds.test"
    rt.create_ca(run_ca, tmp_path / "upstream-ca-git.creds.test", bundles=())
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(rt.server_certificate(run_ca, "git.creds.test")), str(run_ca / "leaf.key"))
    upstream = serve(forge["handler"], context)
    trust = ssl.create_default_context(cafile=str(run_ca / "ca.pem"))
    through = RunProxy(tmp_path / "to-git", {**pki, "upstream": trust}, run, pinned(run, "git.creds.test"),
                       upstream.server_address[1], proxy_run=proxy_run)
    env = {**sandbox_git_env(run, forge["identity"]), "GIT_SSL_CAINFO": str(pki["ca"])}
    remote = f"https://git.creds.test:{through.port}/repo.git"

    def push(target=remote):
        return git("-c", f"http.curloptResolve=git.creds.test:{through.port}:127.0.0.1", "-c",
                   "http.extraHeader=Host: git.creds.test", "push", target, "main", cwd=forge["work"], env=env)
    try:
        pushed = push()
        assert pushed.returncode == 0, pushed.stderr
        assert GIVEN_TOKEN not in pushed.stdout + pushed.stderr
        assert git("rev-parse", "main", cwd=forge["root"] / "repo.git", env=forge["identity"]).stdout.strip() == \
            git("rev-parse", "main", cwd=forge["work"], env=forge["identity"]).stdout.strip()
        assert forge["handler"].seen[0] == "Basic " + base64.b64encode(f"alice:{GIVEN_TOKEN}".encode()).decode(), \
            "Git's very first request carries the login: the proxy put it in"
        assert GIVEN_TOKEN not in through.log.read_text()
        # Listed by its own command (by the run id and by the start's name) and by net show - never the token.
        for name in (run.run, run.record["name"]):
            listed = host.run("git-logins", "list", "--run", name, "--json")
            assert listed.returncode == 0, listed.stderr
            assert json.loads(listed.stdout)["git"] == [{"url": url, "user": "alice"}]
        shown = host.run("net", "show", run.run)
        assert shown.returncode == 0 and url in shown.stdout and GIVEN_TOKEN not in shown.stdout + shown.stderr
        # --keep: the project file names the login by its secret (never the token) and is trusted for the next start.
        project = json.loads((host.workspace / ".sandbox-ai.json").read_text())
        assert project["options"] == ["--allow-host", "git.creds.test", "--git-token-secret",
                                      "https://alice@git.creds.test=GH_PUSH"]
        assert GIVEN_TOKEN not in (host.workspace / ".sandbox-ai.json").read_text()
        # Taken back: the proxy has no login, the next push fails again (the server's 401; nothing to ask with).
        revoked = host.run("git-logins", "revoke", run.run, url)
        assert revoked.returncode == 0 and f"Git login for {url} taken back" in revoked.stdout, revoked.stderr
        assert given_logins(proxy_run) == []
        (forge["work"] / "b.txt").write_text("b\n")
        git("add", "b.txt", cwd=forge["work"], env=forge["identity"])
        git("commit", "-m", "second", cwd=forge["work"], env=forge["identity"])
        again = push()
        assert again.returncode != 0
        assert host.run("git-logins", "revoke", run.run, url).returncode == 1
    finally:
        through.close()
        upstream.shutdown()
    # Given again, then the run ends: the login ends with it (the run's list is gone with its directory).
    assert host.run("git-logins", "give", run.run, url, "--secret", "GH_PUSH", "--user", "alice").returncode == 0
    assert (run.run_dir / "git-logins.json").exists()
    status, out, err = run.end()
    assert status == 0, err
    assert not run.session.exists() and not run.run_dir.exists()
    after = host.run("git-logins", "give", run.run, url, "--secret", "GH_PUSH")
    assert after.returncode == 1 and GIVEN_TOKEN not in after.stdout + after.stderr
    store = host.home / ".local/share/sandbox-ai/secrets"
    assert files_with([host.directory], EVERY_VALUE, skip=[store]) == {}
    assert not any(value in argv_text(host) for value in EVERY_VALUE)
    # The next start of this project: the kept login is one of its own options again (a dry run names it).
    plan = host.run("claude", "--dry-run")
    assert plan.returncode == 0, plan.stderr
    assert [(c["url"], c["user"], c.get("secret")) for c in json.loads(plan.stdout)["git_credentials"]] == [
        (url, "alice", "GH_PUSH")]
    assert GIVEN_TOKEN not in plan.stdout + plan.stderr


def test_a_git_login_into_the_sandbox_is_given_only_on_the_humans_word(host, tmp_path, forge, monkeypatch):
    """A run without the connection proxy (--no-proxy): its proxy cannot log in, so `git-logins give` refuses -
    naming the run and saying that the only other way puts the token where the agent can read it. With --in-sandbox
    (the human's word) the sandbox's Git helper gets it: a real push to a Git server on this host's loopback works
    until it is revoked."""
    keep_secret(host, "GH_PUSH", GIVEN_TOKEN)
    run = start(host, "--no-proxy")
    run.supervise(tmp_path / "proxy-run", monkeypatch)
    url = f"http://localhost:{forge['port']}"
    env = sandbox_git_env(run, forge["identity"])
    tokens = run.session / "git-tokens.json"
    refused = host.run("git-logins", "give", run.run, url, "--secret", "GH_PUSH", "--user", "alice")
    assert refused.returncode == 1, refused.stdout
    assert f"({run.record['name']}) cannot be given the Git login for {url} through its connection proxy" in \
        refused.stderr and "it runs without the connection proxy" in refused.stderr
    assert "where the agent can read the token" in refused.stderr and "--in-sandbox" in refused.stderr
    assert not tokens.exists() and git("push", f"{url}/repo.git", "main", cwd=forge["work"], env=env).returncode != 0
    given = host.run("git-logins", "give", run.run, url, "--secret", "GH_PUSH", "--user", "alice", "--in-sandbox")
    assert given.returncode == 0, given.stderr
    assert "IN THE SANDBOX" in given.stdout and "the agent can read it" in given.stdout
    assert GIVEN_TOKEN not in given.stdout + given.stderr
    assert stat.S_IMODE(tokens.stat().st_mode) == 0o600
    assert json.loads(tokens.read_text())["logins"] == [{"url": url, "user": "alice", "token": GIVEN_TOKEN}]
    assert given_logins(tmp_path / "proxy-run") == []
    pushed = git("push", f"{url}/repo.git", "main", cwd=forge["work"], env=env)
    assert pushed.returncode == 0, pushed.stderr
    # A remote that names another user does not get this login (the helper answers for its user only).
    (forge["work"] / "b.txt").write_text("b\n")
    git("add", "b.txt", cwd=forge["work"], env=forge["identity"])
    git("commit", "-m", "second", cwd=forge["work"], env=forge["identity"])
    assert git("push", f"http://mallory@localhost:{forge['port']}/repo.git", "main", cwd=forge["work"],
               env=env).returncode != 0
    listed = host.run("git-logins", "list", "--run", run.run, "--json")
    assert json.loads(listed.stdout)["git"] == [{"url": url, "user": "alice", "in_sandbox": True}]
    assert host.run("git-logins", "revoke", run.run, url).returncode == 0
    assert json.loads(tokens.read_text())["logins"] == []
    assert git("push", f"{url}/repo.git", "main", cwd=forge["work"], env=env).returncode != 0
    assert run.end()[0] == 0
    assert files_with([host.directory], [GIVEN_TOKEN], skip=[host.home / ".local/share/sandbox-ai/secrets"]) == {}


def test_a_secret_given_during_the_run_is_put_in_on_the_wire_for_its_host_until_revoked(host, tmp_path, pki,
                                                                                         monkeypatch):
    keep_secret(host, "OPENROUTER", SECOND_KEY)
    run = start(host)
    proxy_run = tmp_path / "proxy-run"
    proxy_run.mkdir(mode=0o700)
    (proxy_run / "allow.json").write_text(json.dumps(json.loads((run.proxy_dir / "proxy.json").read_text())["allow"]))
    run.supervise(proxy_run, monkeypatch)
    given = host.run("secrets", "give", run.run, "OPENROUTER", "--api-key", "api.creds.test", "--as", "OR_KEY",
                     "--keep")
    assert given.returncode == 0, given.stderr
    said = given.stdout + given.stderr
    placeholder = "sandbox-ai-api-key-placeholder-OR_KEY"
    assert SECOND_KEY not in said and f"export OR_KEY={placeholder}" in said
    assert f"({run.record['name']}): API key OR_KEY for api.creds.test" in said
    # The proxy's run directory got the key (0600), the run's own list keeps the secret's name beside it (host only),
    # the sandbox's /etc/hosts names the host (the same file: it is bound), the firewall was told its address.
    added = proxy_run / "api-keys-added.json"
    assert stat.S_IMODE(added.stat().st_mode) == 0o600
    assert json.loads(added.read_text())["api_keys"] == [{"host": "api.creds.test", "port": 443, "name": "OR_KEY",
                                                          "placeholder": placeholder, "key": SECOND_KEY}]
    assert json.loads((run.run_dir / "api-keys.json").read_text())["api_keys"][0]["secret"] == "OPENROUTER"
    assert stat.S_IMODE((run.run_dir / "api-keys.json").stat().st_mode) == 0o600
    assert "192.0.2.77 api.creds.test" in (run.session / "hosts").read_text()
    assert any("192.0.2.77" in script for script in run.applied)
    assert files_with([run.session], [SECOND_KEY]) == {}, "the key never enters what the sandbox sees"
    # On the wire: the run's proxy (its proxy.json, the supervisor's allow-list) puts the key in for that host only.
    api, trust = upstream_for(tmp_path, "api.creds.test", EchoHeaders)
    other, other_trust = upstream_for(tmp_path, "evil.creds.test", EchoHeaders)
    both = ssl.create_default_context()
    for name in ("api.creds.test", "evil.creds.test"):
        both.load_verify_locations(cafile=str(tmp_path / f"upstream-{name}" / "ca.pem"))
    pki = {**pki, "upstream": both}
    request = (b"POST /api/v1/chat HTTP/1.1\r\nHost: %s\r\nAuthorization: Bearer " + placeholder.encode() +
               b"\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}")
    through = RunProxy(tmp_path / "to-api", pki, run, "192.0.2.77", api.server_address[1], proxy_run=proxy_run)
    elsewhere = RunProxy(tmp_path / "to-other", pki, run, "192.0.2.77", other.server_address[1], proxy_run=proxy_run)
    try:
        assert wire(through, pki, "api.creds.test", request % b"api.creds.test")["authorization"] == \
            f"Bearer {SECOND_KEY}"
        assert SECOND_KEY not in through.log.read_text() and '"api_keys": ["OR_KEY"]' in through.log.read_text()
        assert wire(elsewhere, pki, "evil.creds.test", request % b"evil.creds.test")["authorization"] == \
            f"Bearer {placeholder}"
        # As the password of git's Basic login too (git push over HTTPS with the placeholder as its password).
        basic = base64.b64encode(f"x-access-token:{placeholder}".encode())
        got = wire(through, pki, "api.creds.test", b"GET /o/r.git/info/refs?service=git-receive-pack HTTP/1.1\r\n"
                   b"Host: api.creds.test\r\nAuthorization: Basic " + basic + b"\r\nConnection: close\r\n\r\n")
        assert base64.b64decode(got["authorization"].split()[1]) == f"x-access-token:{SECOND_KEY}".encode()
        # Listed, never with the key.
        listed = host.run("secrets", "list", "--run", run.run, "--json")
        assert listed.returncode == 0 and SECOND_KEY not in listed.stdout
        assert json.loads(listed.stdout)["api_keys"] == [{"name": "OR_KEY", "host": "api.creds.test", "port": 443,
                                                          "placeholder": placeholder, "secret": "OPENROUTER"}]
        # Taken back (by the secret's name): the very next request keeps its placeholder.
        revoked = host.run("secrets", "revoke", run.run, "OPENROUTER")
        assert revoked.returncode == 0 and "API key OR_KEY taken back" in revoked.stdout, revoked.stderr
        assert json.loads(added.read_text())["api_keys"] == []
        assert wire(through, pki, "api.creds.test", request % b"api.creds.test")["authorization"] == \
            f"Bearer {placeholder}"
    finally:
        through.close()
        elsewhere.close()
        api.shutdown()
        other.shutdown()
    # --keep: the project file names the key by its secret; the next start of the project has it from the start.
    project = json.loads((host.workspace / ".sandbox-ai.json").read_text())
    assert project["options"] == ["--allow-host", "api.creds.test", "--api-key", "api.creds.test=OR_KEY=OPENROUTER"]
    status, out, err = run.end()
    assert status == 0, err
    assert not run.run_dir.exists(), "the run's own list of its keys goes with the run"
    store = host.home / ".local/share/sandbox-ai/secrets"
    assert files_with([host.directory, proxy_run], [SECOND_KEY], skip=[store]) == {}
    assert SECOND_KEY not in argv_text(host)
    again = start(host)
    assert again.config["env"]["OR_KEY"] == placeholder
    keys = json.loads((again.proxy_dir / "proxy.json").read_text())["api_keys"]
    assert [(k["host"], k["name"], k["key"]) for k in keys] == [("api.creds.test", "OR_KEY", SECOND_KEY)]
    assert again.end()[0] == 0


def test_what_keep_wrote_is_there_again_when_the_session_is_resumed(host, tmp_path, monkeypatch):
    """--keep writes references into the project file; `sandbox-ai resume NAME` reads that file again, so the resumed
    session has the login and the key from its start (what was given without --keep ended with the run)."""
    keep_secret(host, "GH_PUSH", GIVEN_TOKEN)
    keep_secret(host, "OPENROUTER", SECOND_KEY)
    run = start(host)
    proxy_run = tmp_path / "proxy-run"
    proxy_run.mkdir(mode=0o700)
    (proxy_run / "allow.json").write_text("{}")
    run.supervise(proxy_run, monkeypatch)
    name = run.record["name"]
    assert host.run("git-logins", "give", name, "https://kept.creds.test", "--secret", "GH_PUSH",
                    "--keep").returncode == 0
    assert host.run("git-logins", "give", name, "https://once.creds.test", "--secret", "GH_PUSH").returncode == 0
    assert host.run("secrets", "give", name, "OPENROUTER", "--api-key", "api.creds.test", "--keep").returncode == 0
    assert run.end()[0] == 0
    Path(host.env["CVB_RELEASE"]).unlink()
    before = len(host.calls())
    resumed = host.popen("resume", name, env={"FAKE_PODMAN_RUN": "0:120"})
    try:
        call, deadline = None, time.monotonic() + 90
        while call is None and time.monotonic() < deadline and resumed.poll() is None:
            call = next((c for c in host.calls()[before:] if c[:1] == ["run"] and "--pod" in c), None)
            time.sleep(0.05)
        assert call is not None, resumed.communicate()
        mounts = {value.split(",dst=", 1)[1].split(",", 1)[0]: Path(value[len("type=bind,src="):].split(",dst=", 1)[0])
                  for value in call if value.startswith("type=bind,src=")}
        env = json.loads((mounts["/sandbox/session"] / "config.json").read_text())["env"]
        keys = [env[key] for key in env if key.startswith("GIT_CONFIG_KEY_")]
        assert "credential.https://kept.creds.test.helper" in keys
        assert "credential.https://once.creds.test.helper" not in keys, "given without --keep: ended with the run"
        assert GIVEN_TOKEN in env.values()
        assert env["OPENROUTER"] == "sandbox-ai-api-key-placeholder-OPENROUTER"
        proxy = json.loads((mounts["/sandbox/proxy"] / "proxy.json").read_text())
        assert [(k["host"], k["name"], k["key"]) for k in proxy["api_keys"]] == [
            ("api.creds.test", "OPENROUTER", SECOND_KEY)]
        assert not (mounts["/sandbox/session"] / "git-tokens.json").exists()
    finally:
        Path(host.env["CVB_RELEASE"]).touch()
        try:
            resumed.communicate(timeout=120)  # (a resumed session wants a terminal: the fake has none, it ends)
        except subprocess.TimeoutExpired:
            resumed.kill()
            resumed.communicate()


# --- 3. the other sources of a start: an identity, a credential file, an LLM endpoint's key -----------------------

def test_an_identitys_logins_a_credential_file_and_an_llm_key_at_a_start(host, tmp_path, pki):
    keep_secret(host, "LLM_KEY", API_SECRET)
    keep_secret(host, "ID_TOKEN", REGISTRY_TOKEN)
    keep_secret(host, "REG_TOKEN", GIVEN_TOKEN)
    for command in (("llm", "add", "gw", "--kind", "openai-compatible", "--url", "https://llm.creds.test/v1",
                     "--api-key-secret", "LLM_KEY", "--model", "m1", "--anthropic"),
                    ("identities", "add", "work", "--name", "Work Me", "--email", "me@work.example"),
                    ("identities", "login", "work", "https://idgit.creds.test", "--secret", "ID_TOKEN", "--user",
                     "carol"),
                    ("git-logins", "add", "https://reg.creds.test", "--secret", "REG_TOKEN")):
        done = host.run(*command)
        assert done.returncode == 0, done.stderr
        assert not any(value in done.stdout + done.stderr for value in EVERY_VALUE)
    stored = tmp_path / "git-credentials"
    stored.write_text("https://dave" + ":" + GIT_TOKEN + "@store.creds.test\n")  # git-credential-store's format
    stored.chmod(0o600)
    # (a login of the list or of an identity opens nothing by itself: its server is allowed as usual, --allow-host)
    run = start(host, "--llm", "gw", "--identity", "work", "--git-credentials", str(stored), "--allow-host",
                "idgit.creds.test")
    env = run.config["env"]
    # The identity: its commit name and address, and its Git login in the proxy - not the default list's.
    settings = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(int(env["GIT_CONFIG_COUNT"]))}
    assert settings["user.name"] == "Work Me" and settings["user.email"] == "me@work.example"
    proxy = json.loads((run.proxy_dir / "proxy.json").read_text())
    assert proxy["git_tokens"] == [{"host": "idgit.creds.test", "port": 443, "user": "carol", "loopback": False,
                                    "token": REGISTRY_TOKEN}]
    # The credential file: Git's helper for that server, the token in the agent's variables (as --git-token-*).
    assert env["SANDBOX_GIT_TOKEN_0"] == GIT_TOKEN and env["SANDBOX_GIT_USER_0"] == "dave"
    assert "credential.https://store.creds.test.helper" in settings
    # The LLM endpoint's key: a placeholder for the agent, the key in the proxy's directory.
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sandbox-ai-llm-key-placeholder" and env["ANTHROPIC_BASE_URL"] == \
        "https://llm.creds.test"
    assert API_SECRET not in json.dumps(run.config) and REGISTRY_TOKEN not in json.dumps(run.config)
    assert [(e["host"], e["key"]) for e in proxy["llm"]] == [("llm.creds.test", API_SECRET)]
    assert set(files_with([run.session], EVERY_VALUE)) == {str(run.session / "config.json")}
    # On the wire: the key in place of the placeholder for the endpoint; the identity's login for Git's requests.
    llm, _ = upstream_for(tmp_path, "llm.creds.test", EchoHeaders)
    forge_, _ = upstream_for(tmp_path, "idgit.creds.test", EchoAuthorization)
    trust = ssl.create_default_context()
    for name in ("llm.creds.test", "idgit.creds.test"):
        trust.load_verify_locations(cafile=str(tmp_path / f"upstream-{name}" / "ca.pem"))
    pki = {**pki, "upstream": trust}
    to_llm = RunProxy(tmp_path / "to-llm", pki, run, pinned(run, "llm.creds.test"), llm.server_address[1])
    to_git = RunProxy(tmp_path / "to-git", pki, run, pinned(run, "idgit.creds.test"), forge_.server_address[1])
    try:
        got = wire(to_llm, pki, "llm.creds.test", b"POST /v1/messages HTTP/1.1\r\nHost: llm.creds.test\r\n"
                   b"Authorization: Bearer sandbox-ai-llm-key-placeholder\r\nContent-Type: application/json\r\n"
                   b"Content-Length: 2\r\nConnection: close\r\n\r\n{}")
        assert got["authorization"] == f"Bearer {API_SECRET}"
        assert API_SECRET not in to_llm.log.read_text()
        answer = https_request(to_git.port, pki["ca"], b"GET /o/r.git/info/refs?service=git-upload-pack HTTP/1.1\r\n"
                               b"Host: idgit.creds.test\r\nConnection: close\r\n\r\n", name="idgit.creds.test")
        assert json.loads(answer.split(b"\r\n\r\n", 1)[1])["authorization"] == \
            "Basic " + base64.b64encode(f"carol:{REGISTRY_TOKEN}".encode()).decode()
        assert REGISTRY_TOKEN not in to_git.log.read_text()
    finally:
        to_llm.close()
        to_git.close()
        llm.shutdown()
        forge_.shutdown()
    assert not any(value in argv_text(host) for value in EVERY_VALUE)
    status, out, err = run.end()
    assert status == 0 and not any(value in out + err for value in EVERY_VALUE), err
    store = host.home / ".local/share/sandbox-ai/secrets"
    assert files_with([host.directory], EVERY_VALUE, skip=[store]) == {}


# --- 4. a locked secret store ---------------------------------------------------------------------------------------

def test_a_locked_secret_store_refuses_cleanly_and_works_again_once_unlocked(host, tmp_path, monkeypatch):
    """The private files encrypted with a primary password: while they are locked, a start and a give that need a
    secret say so (and how to unlock) and change nothing; unlocked, both work; the file never holds the value."""
    host.env.pop("SANDBOX_AI_SECRETS_PLAIN")
    password = "correct horse battery staple"

    def with_input(*args, text):
        return subprocess.run(host.command(*args), input=text, env=host.env, cwd=host.workspace, capture_output=True,
                              text=True, timeout=180)
    try:
        done = with_input("secrets", "password", "set", "--password-stdin", text=password + "\n")
        assert done.returncode == 0, done.stderr
        done = with_input("secrets", "set", "SEALED", text=API_SECRET + "\n")
        assert done.returncode == 0 and "encrypted" in done.stdout, done.stderr
        store = host.home / ".local/share/sandbox-ai/secrets"
        assert files_with([store], [API_SECRET]) == {}, "the private file is encrypted"
        run = start(host)
        proxy_run = tmp_path / "proxy-run"
        proxy_run.mkdir(mode=0o700)
        (proxy_run / "allow.json").write_text("{}")
        run.supervise(proxy_run, monkeypatch)
        hosts_before = (run.session / "hosts").read_text()
        assert host.run("secrets", "lock").returncode == 0
        for command in (("secrets", "give", run.run, "SEALED", "--api-key", "api.creds.test"),
                        ("git-logins", "give", run.run, "https://git.creds.test", "--secret", "SEALED")):
            refused = host.run(*command)
            assert refused.returncode == 1 and "the secrets are locked" in refused.stderr
            assert "sandbox-ai secrets unlock" in refused.stderr and "Traceback" not in refused.stderr
        assert (run.session / "hosts").read_text() == hosts_before and run.applied == []
        assert not (run.session / "git-tokens.json").exists() and not (proxy_run / "api-keys-added.json").exists()
        locked = host.run("claude", "--no-tmux", "--no-checkpoint", "--secret-env", "SEALED",
                          env={"FAKE_PODMAN_RUN": "0:0"})
        assert locked.returncode == 1 and "the secrets are locked" in locked.stderr
        assert not any(call[:1] == ["run"] and "--pod" in call and "SEALED" in json.dumps(call)
                       for call in host.calls())
        done = with_input("secrets", "unlock", "--password-stdin", text=password + "\n")
        assert done.returncode == 0, done.stderr
        given = host.run("secrets", "give", run.run, "SEALED", "--api-key", "api.creds.test")
        assert given.returncode == 0, given.stderr
        assert json.loads((proxy_run / "api-keys-added.json").read_text())["api_keys"][0]["key"] == API_SECRET
        assert run.end()[0] == 0
        assert password not in argv_text(host) and files_with([host.directory], [password, API_SECRET]) == {}
    finally:
        subprocess.run(host.command("secrets", "lock"), env=host.env, capture_output=True, timeout=60)


# --- 5. another host: sandbox-ai hosts run HOST -- ... ------------------------------------------------------------

@pytest.fixture
def far(tmp_path, host):
    """Another machine of `sandbox-ai hosts`: a user's host of its own (another HOME, its own secrets and runs) with
    sandbox-ai on its PATH, reached from HOST through the ssh of SSH (above) as lab = dev@lab.example.org:2222."""
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


def ssh_calls(host):
    path = host.directory / "ssh.log"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_credentials_for_a_session_on_another_host_go_to_that_host_and_stay_there(host, far, tmp_path, monkeypatch):
    run = start(far)
    proxy_run = tmp_path / "proxy-run"
    proxy_run.mkdir(mode=0o700)
    (proxy_run / "allow.json").write_text("{}")
    run.supervise(proxy_run, monkeypatch)
    name = run.record["name"]
    # The secret is kept on that host: its value goes over ssh's standard input, never in a command line.
    kept = subprocess.run(host.command("hosts", "run", "lab", "--", "secrets", "set", "FAR_TOKEN"),
                          input=GIVEN_TOKEN + "\n", env=host.env, cwd=host.workspace, capture_output=True, text=True,
                          timeout=120)
    assert kept.returncode == 0 and "FAR_TOKEN kept" in kept.stdout, kept.stderr
    kept = subprocess.run(host.command("hosts", "run", "lab", "--", "secrets", "set", "FAR_KEY"),
                          input=SECOND_KEY + "\n", env=host.env, cwd=host.workspace, capture_output=True, text=True,
                          timeout=120)
    assert kept.returncode == 0, kept.stderr
    assert (far.home / ".local/share/sandbox-ai/secrets/FAR_TOKEN").exists()
    assert not (host.home / ".local/share/sandbox-ai/secrets").exists(), "nothing is kept on this machine"
    # A Git login and an API key for the session there (the app's form: options, "--", then the start's name).
    given = host.run("hosts", "run", "lab", "--", "git-logins", "give", "--secret", "FAR_TOKEN", "--user", "alice",
                     "--", name, "https://git.creds.test")
    assert given.returncode == 0, given.stderr
    assert f"({name}): Git login for https://git.creds.test (user alice)" in given.stdout
    assert given_logins(proxy_run) == [("git.creds.test", 443, "alice", GIVEN_TOKEN)], "that host's run's proxy"
    assert not (run.session / "git-tokens.json").exists()
    given = host.run("hosts", "run", "lab", "--", "secrets", "give", name, "FAR_KEY", "--api-key", "api.creds.test",
                     "--as", "OR_KEY")
    assert given.returncode == 0, given.stderr
    assert json.loads((proxy_run / "api-keys-added.json").read_text())["api_keys"][0]["key"] == SECOND_KEY
    listed = host.run("hosts", "run", "lab", "--", "git-logins", "list", "--run", name, "--json")
    assert json.loads(listed.stdout)["git"] == [{"url": "https://git.creds.test", "user": "alice"}]
    # The right command went to the right host: every ssh names lab's target and port, the remote command is
    # sandbox-ai with each argument quoted - and no value in any command line.
    calls = [call for call in ssh_calls(host) if "-N" not in call and "-O" not in call]
    assert calls and all(call[call.index("--") + 1] == "dev@lab.example.org" for call in calls)
    assert all(call[call.index("-p") + 1] == "2222" and "BatchMode=yes" in call for call in calls)
    remote = [call[-1] for call in calls]
    assert any(f"exec sandbox-ai git-logins give --secret FAR_TOKEN --user alice -- {name} https://git.creds.test;"
               in command for command in remote)
    assert any(f"exec sandbox-ai secrets give {name} FAR_KEY --api-key api.creds.test --as OR_KEY;" in command
               for command in remote)
    assert not any(value in argv_text(host) + argv_text(far) for value in EVERY_VALUE)
    # Nothing of it is on this machine: not the token, not the key, in no file.
    assert files_with([host.directory], EVERY_VALUE) == {}
    # Taken back from here, and the run's end there ends the rest.
    revoked = host.run("hosts", "run", "lab", "--", "git-logins", "revoke", "--", name, "https://git.creds.test")
    assert revoked.returncode == 0, revoked.stderr
    assert given_logins(proxy_run) == []
    assert run.end()[0] == 0
    assert files_with([far.directory], EVERY_VALUE, skip=[far.home / ".local/share/sandbox-ai/secrets"]) == {}
    # A host that is not in the list gets nothing sent (no ssh at all).
    before = len(ssh_calls(host))
    unknown = host.run("hosts", "run", "nowhere", "--", "secrets", "give", name, "FAR_KEY", "--api-key",
                       "api.creds.test")
    assert unknown.returncode != 0 and len(ssh_calls(host)) == before


# --- 6. agent logins: --auth-file, the central store, --auth-from-session, login --from-session, --no-auth -------

LOGIN_SECRET = "LOGIN-TOKEN-VALUE"  # every token of the login files below holds it: it may appear in no output


def codex_login(refresh="1", account="acct-codex-1"):
    """A Codex login file (auth.json) as `codex login` writes one: ChatGPT tokens of ACCOUNT."""
    def part(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    def jwt(claims):
        return f"{part({'alg': 'none'})}.{part(claims)}.{LOGIN_SECRET}-signature"
    return json.dumps({"tokens": {"id_token": jwt({"email": "dev@example.com"}),
                                  "access_token": jwt({"exp": int(time.time()) + 3600, "x": LOGIN_SECRET}),
                                  "refresh_token": f"{LOGIN_SECRET}-refresh-{refresh}", "account_id": account},
                       "last_refresh": "2026-11-17T10:00:00Z"})


def test_agent_logins_reach_a_session_from_a_file_the_store_and_another_session(host, tmp_path):
    host.add_image("localhost/codex-sandbox:latest")
    login = tmp_path / "auth.json"
    login.write_text(codex_login())
    login.chmod(0o600)
    said = []

    def codex(*options):
        run = start(host, *options, agent="codex")
        return run, run.mounts["/root"] / ".codex/auth.json"

    def ended(run):
        status, out, err = run.end()
        assert status == 0, err
        said.append(out + err)
    # --auth-file: the file is the session's login, private in its HOME.
    first, file = codex("--auth-file", str(login))
    assert json.loads(file.read_text()) == json.loads(login.read_text()) and stat.S_IMODE(file.stat().st_mode) == 0o600
    listed = host.run("login", "--status", "--sessions", "--json")
    assert listed.returncode == 0, listed.stderr
    row = next(r for r in json.loads(listed.stdout)["sessions"] if r["name"] == first.record["name"])
    assert row["source"] == "file" and row["login"] and row["state"] == "running"
    fingerprint = row["info"]["fingerprint"]
    assert LOGIN_SECRET not in listed.stdout + listed.stderr
    ended(first)
    # A session's login as the central one: only the login that was shown (--expect), then every start has it.
    wrong = host.run("login", "codex", "--from-session", first.record["name"], "--yes", "--expect", "00000000")
    assert wrong.returncode != 0 and LOGIN_SECRET not in wrong.stdout + wrong.stderr
    store = host.home / ".local/share/sandbox-ai/auth/codex/.codex/auth.json"
    assert not store.exists()
    imported = host.run("login", "codex", "--from-session", first.record["name"], "--yes", "--expect", fingerprint)
    assert imported.returncode == 0, imported.stderr
    assert LOGIN_SECRET not in imported.stdout + imported.stderr
    assert json.loads(store.read_text())["tokens"]["refresh_token"] == f"{LOGIN_SECRET}-refresh-1"
    assert stat.S_IMODE(store.stat().st_mode) == 0o600
    second, file = codex()
    assert json.loads(file.read_text())["tokens"]["refresh_token"] == f"{LOGIN_SECRET}-refresh-1"
    listed = json.loads(host.run("login", "--status", "--sessions", "--json").stdout)
    row = next(r for r in listed["sessions"] if r["name"] == second.record["name"])
    assert row["source"] == "store" and row["shares_central"] is True
    # The agent refreshes its tokens during the run: they go back into the store when the run ends.
    file.write_text(codex_login(refresh="2"))
    ended(second)
    assert "refreshed codex login written back" in said[-1]
    assert json.loads(store.read_text())["tokens"]["refresh_token"] == f"{LOGIN_SECRET}-refresh-2"
    # --auth-from-session: the first session's login (with the warning about rotating tokens), not the store's.
    third, file = codex("--auth-from-session", first.record["name"])
    assert json.loads(file.read_text())["tokens"]["refresh_token"] == f"{LOGIN_SECRET}-refresh-1"
    ended(third)
    assert "--auth-from-session" in said[-1] and "rotate" in said[-1]
    # --no-auth: no login at all, although the store has one.
    fourth, file = codex("--no-auth")
    assert not file.exists()
    ended(fourth)
    # --copy-auth: the login must be there - the store's while it has one, this computer's own (~/.codex/auth.json)
    # once the store's is logged out; with neither the start is refused.
    own = host.home / ".codex/auth.json"
    own.parent.mkdir(mode=0o700)
    own.write_text(codex_login(refresh="host", account="acct-host"))
    own.chmod(0o600)
    fifth, file = codex("--copy-auth")
    assert json.loads(file.read_text())["tokens"]["account_id"] == "acct-codex-1"
    ended(fifth)
    logout = host.run("login", "codex", "--logout")
    assert logout.returncode == 0 and not store.exists(), logout.stderr
    sixth, file = codex("--copy-auth")
    assert json.loads(file.read_text())["tokens"]["account_id"] == "acct-host"
    ended(sixth)
    own.unlink()
    missing = host.run("codex", "--no-tmux", "--copy-auth", env={"FAKE_PODMAN_RUN": "0:0"})
    assert missing.returncode != 0 and "sandbox-ai login codex" in missing.stderr
    own.write_text(codex_login(refresh="host", account="acct-host"))
    own.chmod(0o600)
    # A session that does not exist, a session of another agent: refused before anything starts.
    before = len(host.calls())
    refused = host.run("codex", "--no-tmux", "--auth-from-session", "no-such-session", env={"FAKE_PODMAN_RUN": "0:0"})
    assert refused.returncode != 0 and not any(c[:1] == ["run"] and "--pod" in c for c in host.calls()[before:])
    # No token in anything said, in any command line or log; the logins are in the HOMEs and the store only.
    assert not any(LOGIN_SECRET in text for text in said) and LOGIN_SECRET not in argv_text(host)
    state = host.home / ".local/state/sandbox-ai"
    holding = files_with([host.directory], [LOGIN_SECRET])
    allowed = (str(state / "sessions"), str(host.home / ".local/share/sandbox-ai/auth"), str(own), str(login))
    assert all(path.startswith(allowed) for path in holding), holding
    assert all(path.endswith("/home/.codex/auth.json") for path in holding if path.startswith(str(state))), holding


def test_a_sessions_login_on_another_host_becomes_that_hosts_central_login(host, far, tmp_path):
    """`sandbox-ai login --status --sessions --host HOST` and `login AGENT --from-session NAME --host HOST`: listed
    and imported there (what the app and the web send after their confirmation: --yes --expect FINGERPRINT); the
    login never comes to this machine."""
    far.add_image("localhost/codex-sandbox:latest")
    login = tmp_path / "auth.json"
    login.write_text(codex_login())
    login.chmod(0o600)
    run = start(far, "--auth-file", str(login), agent="codex")
    name = run.record["name"]
    assert run.end()[0] == 0
    listed = host.run("login", "--status", "--sessions", "--json", "--host", "lab")
    assert listed.returncode == 0, listed.stderr
    row = next(r for r in json.loads(listed.stdout)["sessions"] if r["name"] == name)
    assert row["login"] and row["source"] == "file" and LOGIN_SECRET not in listed.stdout
    imported = host.run("login", "codex", "--from-session", name, "--yes", "--expect", row["info"]["fingerprint"],
                        "--json", "--host", "lab")
    assert imported.returncode == 0, imported.stderr
    assert json.loads(imported.stdout)["imported"] is True and LOGIN_SECRET not in imported.stdout + imported.stderr
    stored = far.home / ".local/share/sandbox-ai/auth/codex/.codex/auth.json"
    assert json.loads(stored.read_text()) == json.loads(login.read_text())
    assert not (host.home / ".local/share/sandbox-ai/auth").exists()
    assert files_with([host.directory], [LOGIN_SECRET]) == {} and LOGIN_SECRET not in argv_text(host)
    calls = [call for call in ssh_calls(host) if "-N" not in call and "-O" not in call]
    assert all(call[call.index("--") + 1] == "dev@lab.example.org" for call in calls)
    assert any(f"login codex --from-session {name} --yes --expect={row['info']['fingerprint']} --json;" in call[-1]
               for call in calls)


# --- 7. an MCP server's secrets (env_secrets) -------------------------------------------------------------------------

def test_an_mcp_servers_secret_reaches_its_container_by_name_only(host, tmp_path):
    """A server whose definition takes TELEGRAM_BOT_TOKEN from the secret TG: its bridge starts each connection's
    container with --env=TELEGRAM_BOT_TOKEN (the name), the value in the environment podman is started with - never
    in a command line, the definition, the server's state or a log."""
    keep_secret(host, "TG", API_SECRET)
    image = "ghcr.io/example/mcp-telegram@sha256:" + "a" * 64
    host.add_image(image)
    config = host.home / ".config/sandbox-ai"
    config.mkdir(parents=True, exist_ok=True)
    (config / "mcp.json").write_text(json.dumps({"telegram": {
        "image": image, "network": "none", "env_secrets": {"TELEGRAM_BOT_TOKEN": "TG"}, "env_values": {"FLAG": "1"}}}))
    (config / "mcp.json").chmod(0o600)
    # What podman is started with for a container: the variables it is told to pass on by name, from its environment.
    podman = (host.bin / "podman").read_text()
    mark = 'while args and args[0].startswith("--"):'
    assert mark in podman
    (host.bin / "podman").write_text(podman.replace(mark, (
        'if os.environ.get("CVB_ENV_LOG"):\n'
        '    names = [a.split("=", 1)[1] for a in args if a.startswith("--env=") and "=" not in a.split("=", 1)[1]]\n'
        '    if names:\n'
        '        with open(os.environ["CVB_ENV_LOG"], "a") as stream:\n'
        '            stream.write(json.dumps({n: os.environ.get(n) for n in names}) + "\\n")\n' + mark), 1))
    passed = tmp_path / "passed.jsonl"  # (outside the host's folders: what the container got, not what is kept)
    host.env.update(SANDBOX_AI_MCP_NO_IMAGE_CHECK="1", CVB_ENV_LOG=str(passed))
    try:
        run = start(host, "--mcp", "telegram")
        state = host.home / ".local/state/sandbox-ai/mcp/telegram"
        bridge = json.loads((state / "mcp.json").read_text())["pid"]
        command = Path(f"/proc/{bridge}/cmdline").read_bytes()
        assert b"--env=TELEGRAM_BOT_TOKEN" in command.split(b"\0") and API_SECRET.encode() not in command
        with pytest.raises(PermissionError):  # (the bridge hides its environment from other processes of yours)
            Path(f"/proc/{bridge}/environ").read_bytes()
        # A connection (the agent's relay): the server's container is started, with the value in its environment.
        before = len(host.calls())
        fd = os.open(state / "sock", os.O_RDONLY | os.O_DIRECTORY)
        with socket.socket(socket.AF_UNIX) as client:
            try:
                client.connect(f"/proc/self/fd/{fd}/server.sock")
            finally:
                os.close(fd)
            client.sendall(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}})
                .encode() + b"\n")
            deadline = time.monotonic() + 30
            while not passed.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
        assert json.loads(passed.read_text().splitlines()[0]) == {"FLAG": "1", "TELEGRAM_BOT_TOKEN": API_SECRET}
        container = next(c for c in host.calls()[before:] if c[:1] == ["run"] and image in c)
        assert "--env=TELEGRAM_BOT_TOKEN" in container and "--network=none" in container
        shown = host.run("mcp", "list", "--json")
        assert shown.returncode == 0 and API_SECRET not in shown.stdout + shown.stderr
        assert run.end()[0] == 0
    finally:
        stopped = host.run("mcp", "stop", "telegram")
    assert stopped.returncode == 0, stopped.stderr
    assert API_SECRET not in argv_text(host)
    assert files_with([host.directory], [API_SECRET], skip=[host.home / ".local/share/sandbox-ai/secrets"]) == {}


# --- 8. defects found by this verification (fixed here, test first) -----------------------------------------------

def test_a_token_file_given_during_the_run_is_held_as_a_start_holds_it(host, tmp_path, monkeypatch):
    """Defect (fixed): `git-logins give RUN URL --file PATH` (net allow --git-token-file) read any private file of
    yours that PATH reached - also through a link inside the run's own workspace, which its agent can plant - and
    wrote its content into the sandbox's git-tokens.json. A start refuses such a path ("goes through the link ...,
    sandbox-ai does not follow it") and warns about a token file inside a workspace; a give does the same now."""
    run = start(host)
    run.supervise(tmp_path / "proxy-run", monkeypatch)
    outside = tmp_path / "private"
    outside.mkdir(mode=0o700)
    (outside / "token").write_text(REGISTRY_TOKEN + "\n")  # a file of yours that looks like a token
    (outside / "token").chmod(0o600)
    (host.workspace / "d").symlink_to(outside)  # what an agent can make in /work
    through = host.workspace / "d" / "token"
    at_start = host.run("claude", "--dry-run", "--git-token-file", f"http://localhost:3998={through}")
    assert at_start.returncode == 1 and "goes through the link" in at_start.stderr
    given = host.run("git-logins", "give", run.run, "http://localhost:3998", "--file", str(through))
    assert given.returncode == 1, given.stdout
    assert "goes through the link" in given.stderr and "sandbox-ai git-logins:" in given.stderr
    assert not (run.session / "git-tokens.json").exists()
    assert files_with([run.session], [REGISTRY_TOKEN]) == {}
    # A token file of yours inside the workspace: your choice, given - with the warning a start gives.
    inside = host.workspace / "token"
    inside.write_text(GIT_TOKEN + "\n")
    inside.chmod(0o600)
    given = host.run("git-logins", "give", run.run, "http://localhost:3999", "--file", str(inside))
    assert given.returncode == 0, given.stderr
    assert "which the agent can change" in given.stderr and GIT_TOKEN not in given.stdout + given.stderr
    # A file outside every workspace: as before, no warning.
    apart = tmp_path / "token"
    apart.write_text(GIVEN_TOKEN + "\n")
    apart.chmod(0o600)
    given = host.run("git-logins", "give", run.run, "http://localhost:3997", "--file", str(apart))
    assert given.returncode == 0 and "warning" not in given.stderr, given.stderr
    assert [(host_, port) for host_, port, _user, _token in given_logins(tmp_path / "proxy-run")] == [
        ("localhost", 3999), ("localhost", 3997)]
    assert not (run.session / "git-tokens.json").exists()
    assert run.end()[0] == 0


def test_a_relative_token_file_is_kept_as_the_file_it_named(host, tmp_path, monkeypatch):
    """Defect (fixed): `git-logins give RUN URL --file tokens/gitea --keep` wrote the path as typed into the project
    file ("--git-token-file URL=tokens/gitea"), and the hint without --keep named it so too: the next start, from
    another directory, read another file or none. It is kept as the file it named."""
    run = start(host)
    run.supervise(tmp_path / "proxy-run", monkeypatch)
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "tokens").mkdir(parents=True)
    token = elsewhere / "tokens" / "gitea"
    token.write_text(GIVEN_TOKEN + "\n")
    token.chmod(0o600)
    hinted = host.run("git-logins", "give", run.run, "http://localhost:3080", "--file", "tokens/gitea", cwd=elsewhere)
    assert hinted.returncode == 0, hinted.stderr
    assert f"--git-token-file http://localhost:3080={token}" in hinted.stdout
    kept = host.run("git-logins", "give", run.run, "http://localhost:3080", "--file", "tokens/gitea", "--keep",
                    cwd=elsewhere)
    assert kept.returncode == 0, kept.stderr
    project = json.loads((host.workspace / ".sandbox-ai.json").read_text())
    assert project["options"] == ["--git-token-file", f"http://localhost:3080={token}"]
    assert run.end()[0] == 0
    # The next start of the project (from its own folder) reads that very file.
    plan = host.run("claude", "--dry-run", "--allow-host-port", "3080")
    assert plan.returncode == 0, plan.stderr
    assert [(c["url"], c.get("file")) for c in json.loads(plan.stdout)["git_credentials"]] == [
        ("http://localhost:3080", str(token))]


# --- 9. the web interface -----------------------------------------------------------------------------------------

@pytest.fixture
def web(host, monkeypatch):
    """The web interface's service (webui.Service) of HOST's user, logged in, running the real sandbox-ai for this
    machine (hosts.run, as it does) - with the python3 of these tests in place of its own interpreter."""
    import test_web_matrix as matrix
    for key, value in host.env.items():
        monkeypatch.setenv(key, value)
    w = matrix.w
    monkeypatch.setattr(w, "SCRYPT", matrix.FAST)
    service = matrix.Web()
    hosts = w.module("hosts")
    ran = []

    def runner(command, **kwargs):
        assert command[:3] == [sys.executable, "-I", str(ROOT / "sandbox" / "main.py")], command
        ran.append(command[3:])
        return subprocess.run([str(host.bin / "python3"), *command[1:]], env=host.env, cwd=host.workspace, **kwargs)
    api = w.Api(lambda name, args, timeout=120, input=None:
                hosts.run(name, args, input=input, timeout=timeout, runner=runner))
    service.svc = w.Service({matrix.HOST}, api=api, sessions=w.Sessions(clock=lambda: service.clock[0]))
    service.sent = service.headers("full")
    service.ran = ran

    def call(method, path, body=None):
        status, _, data = service.request(method, f"/api/hosts/local{path}", body, service.sent)
        return status, json.loads(data)
    service.call = call
    return service


NOTHING = {"allow_host": [], "allow_tcp": [], "host_ports": [], "dns": False}


def test_the_web_interface_keeps_a_secret_and_gives_it_to_a_running_session(host, web, tmp_path, monkeypatch):
    run = start(host)
    proxy_run = tmp_path / "proxy-run"
    proxy_run.mkdir(mode=0o700)
    (proxy_run / "allow.json").write_text("{}")
    run.supervise(proxy_run, monkeypatch)
    name = run.record["name"]
    # A secret kept through the page: its value on the command's standard input, never an argument.
    status, answer = web.call("POST", "/secrets/GH_PUSH", {"secret": GIVEN_TOKEN, "confirm": True})
    assert status == 200, answer
    assert (host.home / ".local/share/sandbox-ai/secrets/GH_PUSH").read_text().strip() == GIVEN_TOKEN
    assert web.ran[-1] == ["secrets", "set", "GH_PUSH"]
    # A Git login and an API key for the running start, as the page's form sends them.
    status, answer = web.call("POST", f"/net/{name}", {"op": "allow", **NOTHING, "confirm": True, "git_login": {
        "url": "https://alice@git.creds.test", "kind": "secret", "ref": "GH_PUSH"}})
    assert status == 200, answer
    assert given_logins(proxy_run) == [("git.creds.test", 443, "alice", GIVEN_TOKEN)]
    assert files_with([run.session], [GIVEN_TOKEN]) == {}, "nothing in what the sandbox sees"
    status, answer = web.call("POST", f"/net/{name}", {"op": "allow", **NOTHING, "confirm": True, "api_key": {
        "host": "api.creds.test", "name": "GH_TOKEN", "secret": "GH_PUSH"}})
    assert status == 200, answer
    assert json.loads((proxy_run / "api-keys-added.json").read_text())["api_keys"][0]["key"] == GIVEN_TOKEN
    # What the page lists of the run: both, never a value.
    status, shown = web.call("GET", f"/net/{name}")
    assert status == 200 and shown["git"] == [{"url": "https://git.creds.test", "user": "alice"}]
    assert [(k["name"], k["host"]) for k in shown["api_keys"]] == [("GH_TOKEN", "api.creds.test")]
    # Not confirmed: nothing runs. A widening is asked first.
    count = len(web.ran)
    status, answer = web.call("POST", f"/net/{name}", {"op": "allow", **NOTHING, "git_login": {
        "url": "https://more.creds.test", "kind": "secret", "ref": "GH_PUSH"}})
    assert status != 200 and len(web.ran) == count
    # The Git login taken back with the row's Revoke button (what the page sends for it).
    status, answer = web.call("POST", f"/net/{name}", {"op": "revoke", **NOTHING, "git_login": {
        "url": "https://git.creds.test", "kind": "env", "ref": "REVOKE"}})
    assert status == 200, answer
    assert given_logins(proxy_run) == []
    # No value in any response, any command line or the audit log.
    for _method, _path, _status, _headers, data in web.responses:
        text = data.decode("utf-8", "replace") if isinstance(data, bytes) else str(data)
        assert GIVEN_TOKEN not in text
    assert GIVEN_TOKEN not in json.dumps(web.ran) and GIVEN_TOKEN not in argv_text(host)
    assert run.end()[0] == 0
    assert files_with([host.directory], [GIVEN_TOKEN], skip=[host.home / ".local/share/sandbox-ai/secrets"]) == {}


def test_the_web_interface_takes_back_an_api_key_given_during_the_run(host, web, tmp_path, monkeypatch):
    """Finding 4 (fixed 2026-11-19; a strict xfail until then): the web interface listed an API key given during a
    run but could not take it back - /net/RUN refused api_key for op revoke."""
    keep_secret(host, "OPENROUTER", SECOND_KEY)
    run = start(host)
    proxy_run = tmp_path / "proxy-run"
    proxy_run.mkdir(mode=0o700)
    (proxy_run / "allow.json").write_text("{}")
    run.supervise(proxy_run, monkeypatch)
    key = {"host": "api.creds.test", "name": "OR_KEY", "secret": "OPENROUTER"}
    status, answer = web.call("POST", f"/net/{run.run}", {"op": "allow", **NOTHING, "confirm": True, "api_key": key})
    assert status == 200, answer
    assert json.loads((proxy_run / "api-keys-added.json").read_text())["api_keys"]
    status, answer = web.call("POST", f"/net/{run.run}", {"op": "revoke", **NOTHING, "api_key": key})
    assert status == 200, answer
    assert json.loads((proxy_run / "api-keys-added.json").read_text())["api_keys"] == []
    # (what the page's row sends: the variable alone)
    status, answer = web.call("POST", f"/net/{run.run}", {"op": "allow", **NOTHING, "confirm": True, "api_key": key})
    assert status == 200, answer
    status, answer = web.call("POST", f"/net/{run.run}", {"op": "revoke", **NOTHING, "api_key": {"name": "OR_KEY"}})
    assert status == 200, answer
    assert json.loads((proxy_run / "api-keys-added.json").read_text())["api_keys"] == []
    status, shown = web.call("GET", f"/net/{run.run}")
    assert status == 200 and shown["api_keys"] == []


# --- 10. the ends that are not the run's own end ----------------------------------------------------------------------

def test_a_killed_launcher_leaves_its_credentials_only_until_the_next_command(host, tmp_path, monkeypatch):
    """The launcher cleans up when its sandbox ends. Killed (SIGKILL: no cleanup), the run's session directory
    (config.json with the agent's variables), its run directory (git-logins.json: the logins its proxy was
    given during the run) and the proxy's directory (proxy.json with the keys)
    stay - private files of yours - until the next sandbox-ai command looks at the runs: it removes them."""
    keep_secret(host, "K", API_SECRET)
    keep_secret(host, "GT", GIVEN_TOKEN)
    run = start(host, "--api-key", "api.creds.test=API_KEY=K", "--secret-env", "K")
    run.supervise(tmp_path / "proxy-run", monkeypatch)
    assert host.run("git-logins", "give", run.run, "http://localhost:3080", "--secret", "GT").returncode == 0
    children = [int(pid) for pid in subprocess.run(["pgrep", "-P", str(run.process.pid)], capture_output=True,
                                                   text=True).stdout.split()]
    run.stop.set()
    run.thread.join(5)
    run.process.kill()  # (this test's own processes: the launcher, then the fake sandbox it started)
    for pid in children:
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass
    run.process.communicate()
    store = host.home / ".local/share/sandbox-ai/secrets"
    left = files_with([host.directory], EVERY_VALUE, skip=[store])
    assert sorted(Path(path).name for path in left) == ["config.json", "git-logins.json", "proxy.json"]
    for path in left:
        assert stat.S_IMODE(os.stat(path).st_mode) & 0o077 == 0 and stat.S_IMODE(
            os.stat(Path(path).parent).st_mode) & 0o077 == 0, f"{path}: private, in a private folder"
    listed = host.run("list")
    assert listed.returncode == 0, listed.stderr
    assert not run.session.exists() and not run.proxy_dir.exists() and not run.run_dir.exists()
    assert files_with([host.directory], EVERY_VALUE, skip=[store]) == {}


def test_net_show_keep_says_what_it_does_not_keep_of_the_credentials(host, tmp_path, monkeypatch):
    """Defect (fixed): `net show RUN --keep` kept the host of an API key given during the run (--allow-host) but not
    the key, and said nothing - the next start reached the host without its key. It names each such key now, with
    the option that brings it back (as it does for the Git logins, whose tokens it cannot keep)."""
    keep_secret(host, "K", API_SECRET)
    keep_secret(host, "GT", GIVEN_TOKEN)
    run = start(host)
    proxy_run = tmp_path / "proxy-run"
    proxy_run.mkdir(mode=0o700)
    (proxy_run / "allow.json").write_text("{}")
    run.supervise(proxy_run, monkeypatch)
    assert host.run("git-logins", "give", run.run, "https://git.creds.test", "--secret", "GT").returncode == 0
    assert host.run("secrets", "give", run.run, "K", "--api-key", "api.creds.test", "--as", "API_KEY").returncode == 0
    kept = host.run("net", "show", run.run, "--keep")
    assert kept.returncode == 0, kept.stderr
    project = json.loads((host.workspace / ".sandbox-ai.json").read_text())
    assert project["options"] == ["--allow-host", "api.creds.test", "--allow-host", "git.creds.test"]
    said = kept.stdout + kept.stderr
    assert "note: Git logins added during the run are not kept" in said
    assert "note: API keys given during the run are not kept" in said
    assert "--api-key api.creds.test=API_KEY=K" in said, "the option that gives the next start the key"
    assert not any(value in said for value in EVERY_VALUE)
    assert run.end()[0] == 0
