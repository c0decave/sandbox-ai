"""Independent verification A (2026-11-19, docs/reviews/credentials-verify-A.md): credentials reach a sandbox where
the agent, its Git and the connection proxy use them - at the start and while it runs, on this machine and on a host
of sandbox-ai hosts - and nowhere else.

End to end through the real entry point: ./sandbox-ai as a subprocess (tests/live_fakes.Host: only Podman, tmux and
ssh faked). A start really runs its launcher (run.json, the session directory, config.json, the proxy's proxy.json,
podman's arguments); what runs inside the sandbox does not exist here, so its supervisor's control socket is served by
the real runtime.control_server in a thread of this test (as tests/test_net.py does), and what the sandbox's Git and
proxy do with what they got is run with the real runtime.py functions (git_credential behind a real `git credential
fill`, api_key_head, host_header_problem, ProxyServer.git_token) on the very files the launcher and sandbox-ai wrote.
Proven only with fakes: that Podman mounts those files (the arguments are checked, not a container), the proxy's
TLS (tests/test_proxy.py has it against a real upstream), and SSH (a fake ssh runs the remote command in a second
user's environment; tests/test_hosts_ssh.py has a real sshd)."""
import base64
import importlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import http.server

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from live_fakes import Host  # noqa: E402

SPEC = importlib.util.spec_from_file_location("sandbox_ai_runtime_verify_a", ROOT / "sandbox" / "runtime.py")
rt = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rt)

NAME = "credrun"
SECRETS = {"GITEA": "gitea_tokenvalue_0123456789ab", "GH_PUSH": "ghp_" + "S3cr3tPushValue" * 2 + "abcdef",
           "GH_READ": "ghp_" + "R3adOnlyValue0" * 2 + "abcdefgh", "OR_KEY": "sk-or-v1-KEYVALUE9876543210fedcba",
           "MYSEC": "envsecretvalue-42-xyz", "PYPI_TOKEN": "pypi-AgEIcHlwaS5vcmcPUBLISHVALUE123"}


def keep_secret(host, name, value):
    """`sandbox-ai secrets set NAME` with the value on standard input (never an argument)."""
    done = subprocess.run(host.command("secrets", "set", name), input=value, text=True, capture_output=True,
                          env=host.env, cwd=host.workspace, timeout=60)
    assert done.returncode == 0, done.stderr


NAMES = ("openrouter.ai", "git.example.org", "github.com", "upload.pypi.org")
DNS_SHIM = '''"""python3 for these tests (as live_fakes' uidshim, which it includes): the names of FAKE_DNS resolve to
192.0.2.50 without asking any resolver (a host allowed during the run is looked up on this machine)."""
import json, os, socket
_names, _real = set(json.loads(os.environ.get("FAKE_DNS", "[]"))), socket.getaddrinfo
socket.getaddrinfo = lambda host, *a, **k: _real("192.0.2.50" if isinstance(host, str) and host.lower().rstrip(".")
                                                  in _names else host, *a, **k)
'''


def a_host(directory, secrets=SECRETS):
    host = Host(directory)
    host.add_image("localhost/claude-sandbox:latest")
    # Every python3 the start runs (sandbox-ai, its helpers) resolves NAMES itself: no lookup leaves this machine.
    shim = DNS_SHIM + importlib.import_module("live_fakes").UIDSHIM.strip().strip('"').split('"""', 1)[1]
    if not host.shimmed:
        shim = shim.replace("os.geteuid = lambda: 1000", "")
    (host.bin / "dnsshim.py").write_text(shim)
    (host.bin / "python3").write_text(f'#!/bin/sh\nexec {sys.executable} -I "{host.bin}/dnsshim.py" "$@"\n')
    (host.bin / "python3").chmod(0o755)
    host.env.update(FAKE_PYTHON=str(host.bin / "python3"), FAKE_DNS=json.dumps(NAMES))
    host.env.update(SANDBOX_AI_SECRETS="file", SANDBOX_AI_SECRETS_PLAIN="1", SANDBOX_AI_SSH_MULTIPLEX="0")
    for name, value in secrets.items():
        keep_secret(host, name, value)
    return host


def registered(host, name=NAME, timeout=60):
    """The run directory of the start NAME once its launcher registered it (run.json)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for record in (host.runtime / "sandbox-ai" / "runs").glob("*/run.json"):
            with_name = json.loads(record.read_text())
            if with_name.get("name") == name and with_name.get("session_dir"):
                return record.parent
        time.sleep(0.1)
    raise AssertionError(f"{name} never registered")


def everything_written(root, skip=()):
    """{path: bytes} of every regular file below ROOT (the secret store and SKIP left out)."""
    found = {}
    for path in Path(root).rglob("*"):
        if path.is_file() and not path.is_symlink() and "secrets" not in path.parts and \
                not any(str(path).startswith(str(s)) for s in skip):
            try:
                found[path] = path.read_bytes()
            except OSError:
                pass
    return found


def leaked(root, values, skip=()):
    """The files below ROOT that hold one of VALUES (in plain, base64'd as a Basic login, or JSON-escaped)."""
    forms = {v.encode() for v in values} | {base64.b64encode(f"x-access-token:{v}".encode()) for v in values}
    return sorted(str(path) for path, data in everything_written(root, skip).items() if any(f in data for f in forms))


class Running:
    """A real start of NAME (launcher, run.json, session directory) whose sandbox is the fake Podman's process, with
    the supervisor's control socket served by the real runtime.control_server (firewall changes recorded)."""

    def __init__(self, host, *options, keep_dir=None, name=NAME):
        self.host, self.name = host, name
        env = {"FAKE_PODMAN_RUN": "0:600", **({"FAKE_PODMAN_KEEP": str(keep_dir)} if keep_dir else {})}
        self.process = host.popen("claude", "--no-tmux", "--name", name, "--no-canaries", "--no-checkpoint",
                                  *options, env=env)
        self.run_dir = registered(host, name)
        self.record = json.loads((self.run_dir / "run.json").read_text())
        self.session = Path(self.record["session_dir"])
        self.applied, self.stop_event = [], threading.Event()
        network = rt.RuntimeNetwork({"loopback": []}, self.run_dir.parent / f"{name}-allow.json", self.applied.append)
        self.thread = threading.Thread(target=rt.control_server, args=(self.run_dir / "control.sock", network,
                                                                       self.stop_event), daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 10
        while not (self.run_dir / "control.sock").exists() and time.monotonic() < deadline:
            time.sleep(0.02)

    def end(self):
        """`sandbox-ai stop NAME`: the launcher cleans up and ends (the session directory goes)."""
        if self.process.poll() is None:
            stopped = self.host.run("stop", self.name, timeout=120)
            try:
                self.process.communicate(timeout=120)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.communicate()
            self.stopped = stopped
        self.stop_event.set()
        self.thread.join(5)


@pytest.fixture
def short_dir():
    """A short folder (a Unix socket's path is at most 107 bytes)."""
    directory = Path(tempfile.mkdtemp(prefix="cva-", dir="/tmp"))
    yield directory
    for path in [directory, *directory.rglob("*")]:
        if path.is_dir() and not path.is_symlink():
            path.chmod(0o700)
    shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def proxy_run(short_dir, monkeypatch):
    """The proxy's run directory as the supervisor gives keys to it (runtime.PROXY_RUN)."""
    folder = short_dir / "proxy-run"
    folder.mkdir(mode=0o700)
    monkeypatch.setattr(rt, "PROXY_RUN", folder)
    monkeypatch.setattr(rt, "_added_api_keys", {"stamp": None, "entries": []})
    monkeypatch.setattr(rt, "_added_git_logins", {"stamp": None, "entries": []})
    return folder / rt.ADDED_API_KEYS


def proxy_login(scheme, origin, destination, started=(), service="git-receive-pack"):
    """What the run's connection proxy adds to a Git request (the first of a push: info/refs) to ORIGIN at
    DESTINATION: (user, token) of the Basic login, or None - the real ProxyServer.git_token reading the logins the
    supervisor wrote for it (runtime.PROXY_RUN), STARTED being the logins of its proxy.json."""
    server = rt.ProxyServer.__new__(rt.ProxyServer)
    server.config = {"git_tokens": list(started)}
    added = server.git_token(scheme, origin, {"destination": destination}, "GET", "/o/r.git/info/refs",
                             f"/o/r.git/info/refs?service={service}", [])
    return tuple(base64.b64decode(added).decode().split(":", 1)) if added else None


def git_fill(tokens, url):
    """`git credential fill` for URL with the sandbox's own helper (runtime.git_credential, as RUNTIME_GIT_HELPER
    runs it) reading TOKENS: what the sandbox's Git sends."""
    helper = ("!python3 -I -c 'import importlib.util, sys; spec = importlib.util.spec_from_file_location(\"rt\", "
              f"\"{ROOT / 'sandbox/runtime.py'}\"); rt = importlib.util.module_from_spec(spec); "
              f"spec.loader.exec_module(rt); sys.exit(rt.git_credential(sys.argv[1], path=\"{tokens}\"))'")
    env = {"PATH": os.environ["PATH"], "HOME": str(Path(tokens).parent), "GIT_TERMINAL_PROMPT": "0",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "credential.helper",
           "GIT_CONFIG_VALUE_0": helper}
    done = subprocess.run(["git", "credential", "fill"], input=f"url={url}\n\n", text=True, capture_output=True,
                          env=env, timeout=30)
    return done.returncode, dict(line.split("=", 1) for line in done.stdout.splitlines() if "=" in line)


# --- at the start ------------------------------------------------------------------------------------------------------

def test_a_start_hands_each_credential_to_where_it_is_used_and_nowhere_else(short_dir):
    """--git-token-secret (Git's helper, the agent's environment), sandbox-ai git-logins (the proxy only), --api-key
    (the proxy, the agent a placeholder), --secret-env (the agent's environment), --publish (the proxy, a
    placeholder): every value in the one place it is used, none in podman's arguments, the launcher's log or a
    file that outlives the run."""
    host = a_host(short_dir / "h")
    assert host.run("git-logins", "add", "https://git.example.org", "--secret", "GH_READ").returncode == 0
    assert host.run("publish-logins", "add", "pypi", "--secret", "PYPI_TOKEN").returncode == 0
    kept = short_dir / "kept"
    started = host.run("claude", "--no-tmux", "--name", NAME, "--no-canaries", "--no-checkpoint",
                       "--git-token-secret", "https://github.com=GH_PUSH", "--allow-host", "git.example.org",
                       "--api-key", "openrouter.ai=OPENROUTER_API_KEY=OR_KEY", "--secret-env", "MYSEC",
                       "--publish", "pypi", env={"FAKE_PODMAN_RUN": "0:0", "FAKE_PODMAN_KEEP": str(kept)})
    assert started.returncode == 0, started.stderr
    values = list(SECRETS.values())
    assert not [v for v in values if v in started.stdout + started.stderr]
    config = json.loads((kept / "session" / "config.json").read_text())
    proxy = json.loads((kept / "proxy" / "proxy.json").read_text())
    env = config["env"]
    # --git-token-secret without --token-proxy: Git's helper for that server reads the agent's variables.
    assert env["SANDBOX_GIT_TOKEN_0"] == SECRETS["GH_PUSH"] and env["GH_TOKEN"] == SECRETS["GH_PUSH"]
    keys = {env[k]: env[k.replace("KEY", "VALUE")] for k in env if k.startswith("GIT_CONFIG_KEY_")}
    assert keys["credential.helper"] == "!python3 -I /sandbox/runtime.py --internal-git-credential"
    assert "SANDBOX_GIT_USER_0" in keys["credential.https://github.com.helper"]
    # sandbox-ai git-logins: the proxy only (no variable, no helper of its own for that server).
    assert [(t["host"], t["port"], t["token"]) for t in proxy["git_tokens"]] == \
        [("git.example.org", 443, SECRETS["GH_READ"])]
    assert SECRETS["GH_READ"] not in json.dumps(config)
    assert not any("git.example.org" in k for k in keys)
    # --api-key: the key in the proxy, the placeholder for the agent.
    assert env["OPENROUTER_API_KEY"] == "sandbox-ai-api-key-placeholder-OPENROUTER_API_KEY"
    api = {e["name"]: e for e in proxy["api_keys"]}
    assert api["OPENROUTER_API_KEY"]["key"] == SECRETS["OR_KEY"] and api["OPENROUTER_API_KEY"]["host"] == "openrouter.ai"
    assert SECRETS["OR_KEY"] not in json.dumps(config)
    # --publish pypi: the token in the proxy (a Basic login's password), never in the agent's environment.
    published = [e for e in proxy["api_keys"] if e["name"].startswith("publish:")]
    assert published and published[0]["key"] == SECRETS["PYPI_TOKEN"] and published[0]["host"] == "upload.pypi.org"
    assert SECRETS["PYPI_TOKEN"] not in json.dumps(config)
    # --secret-env: the agent's variable, named as a secret (the supervisor's untrusted commands lose it).
    assert env["MYSEC"] == SECRETS["MYSEC"] and "MYSEC" in config["secret_names"]
    # Never in podman's arguments (ps), the launcher's log, the run's logs, the HOME it keeps.
    argv = json.dumps(host.calls())
    assert not [v for v in values if v in argv]
    assert leaked(host.directory, values, skip=(kept,)) == []
    # The session's config.json and the proxy's directory are private, and gone when the run ended.
    run_call = next(c for c in host.calls() if c[:1] == ["run"] and "--pod" in c)
    sources = {a.split(",dst=", 1)[1].split(",", 1)[0]: Path(a.split("src=", 1)[1].split(",dst=", 1)[0])
               for a in run_call if a.startswith("type=bind,")}
    assert ",ro," in next(a + "," for a in run_call if ",dst=/sandbox/session," in a)
    assert not sources["/sandbox/session"].exists() and not sources["/sandbox/proxy"].exists()
    assert stat.S_IMODE((kept / "session" / "config.json").stat().st_mode) == 0o600


# --- while it runs -----------------------------------------------------------------------------------------------------

def test_a_git_login_given_while_it_runs_reaches_its_git_and_ends_with_the_run(short_dir, proxy_run):
    """(Since 2026-11-19 the login goes to the run's connection proxy, not into the sandbox: findings 1 and 2.)"""
    host = a_host(short_dir / "h")
    run = Running(host)
    try:
        given = host.run("git-logins", "give", NAME, "http://localhost:3080", "--secret", "GITEA", "--user", "alice")
        assert given.returncode == 0, given.stderr
        assert SECRETS["GITEA"] not in given.stdout + given.stderr
        assert f"({NAME}): Git login for http://localhost:3080 (user alice)" in given.stdout
        assert "the token does not enter the sandbox" in given.stdout
        logins = proxy_run.with_name(rt.ADDED_GIT_LOGINS)
        assert stat.S_IMODE(logins.stat().st_mode) == 0o600
        # The run's proxy: the login on the wire for Git's requests to that server on the host's loopback, and for
        # no other port, no other address, not over HTTPS.
        assert proxy_login("http", "localhost", "127.0.0.1:3080") == ("alice", SECRETS["GITEA"])
        for scheme, origin, destination in (("http", "localhost", "127.0.0.1:3081"),
                                            ("http", "localhost", "127.0.0.5:3080"),
                                            ("https", "localhost", "127.0.0.1:3080")):
            assert proxy_login(scheme, origin, destination) is None, (scheme, origin, destination)
        assert not (run.session / "git-tokens.json").exists(), "nothing for the sandbox's own Git: it needs none"
        listed = host.run("git-logins", "list", "--run", NAME, "--json")
        assert json.loads(listed.stdout)["git"] == [{"url": "http://localhost:3080", "user": "alice"}]
        shown = host.run("net", "show", NAME, "--json")
        assert SECRETS["GITEA"] not in shown.stdout and json.loads(shown.stdout)["git"]
        assert leaked(host.directory, [SECRETS["GITEA"]]) == [str(run.run_dir / "git-logins.json")], \
            "only in the run's own list (its run directory: yours, hidden from the agent), beside the proxy's copy"
        assert stat.S_IMODE((run.run_dir / "git-logins.json").stat().st_mode) == 0o600
        # Taken back: the proxy adds nothing any more.
        taken = host.run("git-logins", "revoke", NAME, "http://localhost:3080")
        assert taken.returncode == 0, taken.stderr
        assert proxy_login("http", "localhost", "127.0.0.1:3080") is None
        assert leaked(host.directory, [SECRETS["GITEA"]]) == []
        # Given again, then the run ends: the login ends with it (the run's directory is removed; the proxy's is the
        # sandbox's own, gone with it).
        assert host.run("git-logins", "give", NAME, "http://localhost:3080", "--secret", "GITEA").returncode == 0
        assert (run.run_dir / "git-logins.json").exists()
    finally:
        run.end()
    assert run.stopped.returncode == 0, run.stopped.stderr
    assert not run.session.exists() and not run.run_dir.exists()
    assert leaked(host.directory, [SECRETS["GITEA"]]) == []


def test_a_key_given_while_it_runs_is_put_in_on_the_wire_for_its_host_only(short_dir, proxy_run):
    host = a_host(short_dir / "h")
    run = Running(host)
    try:
        given = host.run("secrets", "give", NAME, "OR_KEY", "--api-key", "openrouter.ai", "--as", "OPENROUTER_API_KEY")
        assert given.returncode == 0, given.stderr
        assert SECRETS["OR_KEY"] not in given.stdout + given.stderr
        placeholder = "sandbox-ai-api-key-placeholder-OPENROUTER_API_KEY"
        assert f"export OPENROUTER_API_KEY={placeholder}" in given.stdout
        # The supervisor gave the proxy the key (0600); the host is allowed with it.
        assert stat.S_IMODE(proxy_run.stat().st_mode) == 0o600
        entries = rt.added_api_keys(proxy_run)
        assert [(e["host"], e["port"], e["name"], e["key"]) for e in entries] == \
            [("openrouter.ai", 443, "OPENROUTER_API_KEY", SECRETS["OR_KEY"])]
        assert "openrouter.ai" in (run.session / "hosts").read_text() and run.applied
        # On the wire: the proxy's very function on the very file - the key for openrouter.ai:443 over HTTPS only.
        head = (b"POST /api/v1/chat/completions HTTP/1.1\r\nHost: openrouter.ai\r\nAuthorization: Bearer "
                + placeholder.encode() + b"\r\nX-Note: " + placeholder.encode() + b"\r\n\r\n")
        sent, names = rt.api_key_head(entries, "https", "openrouter.ai", 443, head)
        assert names == ["OPENROUTER_API_KEY"]
        assert f"Authorization: Bearer {SECRETS['OR_KEY']}".encode() in sent
        assert b"X-Note: " + placeholder.encode() in sent, "only credential headers"
        for scheme, origin, port in (("https", "evil.example", 443), ("http", "openrouter.ai", 443),
                                     ("https", "openrouter.ai", 8443)):
            assert rt.api_key_head(entries, scheme, origin, port, head) == (head, [])
        # A request through openrouter.ai's TLS that names another server in its Host header is refused.
        assert rt.host_header_problem([["Host", "evil.example"]], "https", 443, {"openrouter.ai"}, "openrouter.ai")
        assert not rt.host_header_problem([["Host", "openrouter.ai"]], "https", 443, {"openrouter.ai"},
                                          "openrouter.ai")
        listed = host.run("secrets", "list", "--run", NAME, "--json")
        assert SECRETS["OR_KEY"] not in listed.stdout and "OPENROUTER_API_KEY" in listed.stdout
        assert leaked(host.directory, [SECRETS["OR_KEY"]]) == [run.run_dir.joinpath("api-keys.json").as_posix()], \
            "the run's own list (its run directory, the agent's cannot read it) and nothing else of the host"
        taken = host.run("secrets", "revoke", NAME, "OR_KEY")
        assert taken.returncode == 0, taken.stderr
        assert rt.added_api_keys(proxy_run) == []
        assert rt.api_key_head(rt.added_api_keys(proxy_run), "https", "openrouter.ai", 443, head) == (head, [])
    finally:
        run.end()
    assert not run.run_dir.exists() or not (run.run_dir / "api-keys.json").exists()
    assert leaked(host.directory, [SECRETS["OR_KEY"]]) == []


def test_kept_logins_and_keys_reach_the_next_start_and_a_resume(short_dir, proxy_run):
    """--keep: into the project's trusted .sandbox-ai.json as references (never the value); the next start of the
    project - and `sandbox-ai resume` of the kept start - hands them over as the options would."""
    host = a_host(short_dir / "h")
    run = Running(host)
    try:
        assert host.run("git-logins", "give", NAME, "https://git.example.org", "--secret", "GH_PUSH", "--user",
                        "alice", "--keep").returncode == 0
        kept_key = host.run("secrets", "give", NAME, "OR_KEY", "--api-key", "openrouter.ai", "--keep")
        assert kept_key.returncode == 0, kept_key.stderr
    finally:
        run.end()
    project = host.workspace / ".sandbox-ai.json"
    document = json.loads(project.read_text())
    assert "--git-token-secret" in document["options"] and "https://alice@git.example.org=GH_PUSH" in document["options"]
    assert "openrouter.ai=OR_KEY=OR_KEY" in document["options"]
    assert not [v for v in SECRETS.values() if v in project.read_text()]
    for args in (("claude", "--no-tmux", "--name", "next", "--no-canaries", "--no-checkpoint"), ("resume", NAME)):
        kept = short_dir / f"kept-{args[0]}"
        started = host.run(*args, env={"FAKE_PODMAN_RUN": "0:0", "FAKE_PODMAN_KEEP": str(kept)})
        # (a resume restarts the session in its terminal: the fake sandbox that ends at once is said as such)
        assert started.returncode == 0 or "Session ended at once" in started.stderr, started.stderr
        assert f"configuration: {project}" in started.stderr, started.stderr
        env = json.loads((kept / "session" / "config.json").read_text())["env"]
        keys = {env[k]: env[k.replace("KEY", "VALUE")] for k in env if k.startswith("GIT_CONFIG_KEY_")}
        assert env["SANDBOX_GIT_TOKEN_0"] == SECRETS["GH_PUSH"] and env["SANDBOX_GIT_USER_0"] == "alice", args
        assert "credential.https://git.example.org.helper" in keys, args
        proxy = json.loads((kept / "proxy" / "proxy.json").read_text())
        assert [(e["host"], e["name"], e["key"]) for e in proxy["api_keys"]] == \
            [("openrouter.ai", "OR_KEY", SECRETS["OR_KEY"])], args
        assert env["OR_KEY"] == "sandbox-ai-api-key-placeholder-OR_KEY", args


# --- on another host -------------------------------------------------------------------------------------------------

FAKE_REMOTE_SSH = r'''#!PYTHON
"""ssh to the host of sandbox-ai hosts: the remote command (its last argument, as sshd hands it to the login
shell) runs with `sh -c` as the remote user (REMOTE_ENV: their HOME, XDG folders, PATH with sandbox-ai on it).
Each call is logged (FAKE_REMOTE_SSH_LOG)."""
import json, os, subprocess, sys
args = sys.argv[1:]
with open(os.environ["FAKE_REMOTE_SSH_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")
if "-G" in args or "-O" in args:
    sys.exit(0 if "-G" in args else 255)
target = args[args.index("--") + 1]
command = args[args.index("--") + 2]
env = json.loads(open(os.environ["REMOTE_ENV"]).read())
sys.exit(subprocess.run(["sh", "-c", command], env=env, stdin=sys.stdin).returncode)
'''


def test_a_login_and_a_key_given_on_another_host_reach_that_hosts_run(short_dir, proxy_run):
    """The app's and the web's way to a start on another host (`sandbox-ai hosts run HOST -- git-logins give ...`,
    the arguments the app's Credentials page builds): the remote sandbox-ai gives its own secret to its own run;
    this machine's secrets and runs are not touched."""
    from_here = a_host(short_dir / "here", secrets={"GITEA": "gitea_THIS_MACHINES_0123456"})
    remote = a_host(short_dir / "box")
    bin_dir = remote.directory / "bin"
    bin_dir.mkdir()
    (bin_dir / "sandbox-ai").symlink_to(ROOT / "sandbox-ai")
    remote_env = {**remote.env, "PATH": f"{bin_dir}:{remote.env['PATH']}"}
    (short_dir / "remote-env.json").write_text(json.dumps(remote_env))
    (from_here.bin / "ssh").write_text(FAKE_REMOTE_SSH.replace("PYTHON", sys.executable, 1))
    (from_here.bin / "ssh").chmod(0o755)
    from_here.env.update(REMOTE_ENV=str(short_dir / "remote-env.json"),
                         FAKE_REMOTE_SSH_LOG=str(short_dir / "ssh.log"))
    hosts = from_here.home / ".config" / "sandbox-ai" / "hosts.json"
    hosts.parent.mkdir(parents=True, exist_ok=True)
    hosts.write_text(json.dumps({"version": 1, "hosts": {"box": {"ssh": "me@box.example", "port": 22}}}))
    hosts.chmod(0o600)
    run = Running(remote)
    try:
        sys.path.insert(0, str(ROOT / "projects" / "sandbox-ai-app"))
        try:
            from sandbox_ai_app.credentials_page import give_login_args
        except ImportError:  # (PySide6 missing: the app's arguments, as credentials_page.give_login_args makes them)
            def give_login_args(run_, entry, keep):
                return ["--secret", entry["secret"], "--user", entry["user"], "--", run_, entry["url"]]
        args = ["git-logins", "give", *give_login_args(NAME, {"url": "http://localhost:3080", "user": "alice",
                                                               "secret": "GITEA"}, False)]
        given = from_here.run("hosts", "run", "box", "--", *args)
        assert given.returncode == 0, given.stderr
        assert f"({NAME}): Git login for http://localhost:3080 (user alice)" in given.stdout
        assert proxy_login("http", "localhost", "127.0.0.1:3080") == ("alice", SECRETS["GITEA"]), \
            "the remote host's secret, never this machine's"
        key = from_here.run("hosts", "run", "box", "--", "secrets", "give", NAME, "OR_KEY", "--api-key",
                            "openrouter.ai", "--as", "OPENROUTER_API_KEY")
        assert key.returncode == 0, key.stderr
        assert [e["key"] for e in rt.added_api_keys(proxy_run)] == [SECRETS["OR_KEY"]]
        # The one command line ssh got: to box, each argument quoted; no value of a secret in it.
        calls = [json.loads(line) for line in (short_dir / "ssh.log").read_text().splitlines()]
        sent = [c for c in calls if "-G" not in c and "-O" not in c]
        assert [c[c.index("--") + 1] for c in sent] == ["me@box.example"] * 2
        assert "git-logins give --secret GITEA --user alice -- credrun http://localhost:3080" in sent[0][-1]
        assert not [v for v in [*SECRETS.values(), "gitea_THIS_MACHINES_0123456"] if v in json.dumps(calls)]
        assert leaked(from_here.directory, list(SECRETS.values())) == []
        # Taken back there the same way.
        assert from_here.run("hosts", "run", "box", "--", "git-logins", "revoke", NAME,
                             "http://localhost:3080").returncode == 0
        assert proxy_login("http", "localhost", "127.0.0.1:3080") is None
    finally:
        run.end()


# --- defects -----------------------------------------------------------------------------------------------------------

class Recorder(http.server.BaseHTTPRequestHandler):
    seen = []

    def do_GET(self):
        Recorder.seen.append(self.headers.get("Authorization"))
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="git"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


def test_git_asks_its_credential_helper_only_after_a_401(tmp_path):
    """The mechanism behind the defect below, with the real git: the first request of a clone or push carries no
    credentials of the sandbox's Git, whatever its helper knows - so a proxy that adds its own login to it decides."""
    tokens = tmp_path / "git-tokens.json"
    tokens.write_text(json.dumps({"logins": [{"url": "http://127.0.0.1:%d", "user": "a", "token": "given-token-1"}]}))
    Recorder.seen = []
    server = http.server.HTTPServer(("127.0.0.1", 0), Recorder)
    port = server.server_address[1]
    tokens.write_text(tokens.read_text() % port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        helper = ("!python3 -I -c 'import importlib.util, sys; spec = importlib.util.spec_from_file_location(\"rt\", "
                  f"\"{ROOT / 'sandbox/runtime.py'}\"); rt = importlib.util.module_from_spec(spec); "
                  f"spec.loader.exec_module(rt); sys.exit(rt.git_credential(sys.argv[1], path=\"{tokens}\"))'")
        env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "GIT_TERMINAL_PROMPT": "0",
               "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "credential.helper",
               "GIT_CONFIG_VALUE_0": helper}
        subprocess.run(["git", "ls-remote", f"http://127.0.0.1:{port}/o/r.git"], env=env, capture_output=True,
                       timeout=60)
    finally:
        server.shutdown()
    assert Recorder.seen[0] is None, "the first request goes without credentials"
    assert Recorder.seen[1] == "Basic " + base64.b64encode(b"a:given-token-1").decode(), "the helper after the 401"


def test_a_login_given_during_the_run_wins_over_the_proxys_login_for_that_server(short_dir, proxy_run):
    """Finding 1 of credentials-verify-A (fixed 2026-11-19; a strict xfail until then): a Git login given during the
    run (git-logins give / net allow --git-token-*) never took effect for a server the run's proxy already had a login
    for (sandbox-ai git-logins, an identity's, --token-proxy): the proxy added its own to Git's first request, which
    carries none, so Git never asked the helper that knew the given one. The given login goes to the proxy now and
    is used before the ones of its start - end to end: a start with a read-only login of sandbox-ai git-logins, then
    `git-logins give` of the push login through ./sandbox-ai and the run's supervisor."""
    host = a_host(short_dir / "h")
    assert host.run("git-logins", "add", "https://github.com", "--secret", "GH_READ").returncode == 0
    kept = short_dir / "kept"
    run = Running(host, "--allow-github", keep_dir=kept)
    try:
        deadline, started = time.monotonic() + 60, None
        while started is None and time.monotonic() < deadline:  # (the fake Podman copies the proxy's directory)
            try:
                started = json.loads((kept / "proxy" / "proxy.json").read_text())["git_tokens"]
            except (OSError, ValueError):
                time.sleep(0.1)
        assert [(t["host"], t["token"]) for t in started] == [("github.com", SECRETS["GH_READ"])]
        assert proxy_login("https", "github.com", "140.82.121.4:443", started) == \
            ("x-access-token", SECRETS["GH_READ"]), "before: the login the run was started with"
        given = host.run("git-logins", "give", NAME, "https://github.com", "--secret", "GH_PUSH")
        assert given.returncode == 0, given.stderr
        assert "it wins over a login the run was started with for that server" in given.stdout
        assert proxy_login("https", "github.com", "140.82.121.4:443", started) == \
            ("x-access-token", SECRETS["GH_PUSH"]), "the login given during the run wins"
        assert host.run("git-logins", "revoke", NAME, "https://github.com").returncode == 0
        assert proxy_login("https", "github.com", "140.82.121.4:443", started) == \
            ("x-access-token", SECRETS["GH_READ"]), "taken back: the start's again"
    finally:
        run.end()


# --- agent logins --------------------------------------------------------------------------------------------------

def test_agent_logins_reach_the_sandboxs_home_by_each_way_and_nowhere_else(short_dir):
    """--copy-auth (the host's login), --auth-file FILE, --auth-from-session NAME and `sandbox-ai login AGENT
    --from-session NAME` (the central login every later start gets): the login file in the sandbox's HOME, 0600,
    as it was; never in podman's arguments or a log."""
    host = a_host(short_dir / "h", secrets={})
    host.add_image("localhost/codex-sandbox:latest")
    logins = {name: json.dumps({"OPENAI_API_KEY": f"sk-proj-{name}-" + "Q" * 40}) for name in
              ("host", "file", "other")}
    (host.home / ".codex").mkdir()
    (host.home / ".codex" / "auth.json").write_text(logins["host"])
    (host.home / ".codex" / "auth.json").chmod(0o600)
    login_file = short_dir / "codex-auth.json"
    login_file.write_text(logins["file"])
    login_file.chmod(0o600)

    def start(name, *options):
        done = host.run("codex", "--no-tmux", "--name", name, "--no-canaries", "--no-checkpoint", *options,
                        env={"FAKE_PODMAN_RUN": "0:0"})
        assert done.returncode == 0, done.stderr
        assert not [v for v in logins.values() if v["OPENAI_API_KEY"] in done.stdout + done.stderr]
        target = host.home / ".local/state/sandbox-ai/sessions" / name / "home/.codex/auth.json"
        assert stat.S_IMODE(target.stat().st_mode) == 0o600, name
        return json.loads(target.read_text())  # (as JSON: a login taken from a session is written indented)

    logins = {k: json.loads(v) for k, v in logins.items()}
    assert start("copied", "--copy-auth") == logins["host"]
    assert start("from-file", "--auth-file", str(login_file)) == logins["file"]
    assert start("from-session", "--auth-from-session", "from-file") == logins["file"]
    imported = host.run("login", "codex", "--from-session", "from-file", "--yes")
    assert imported.returncode == 0, imported.stderr
    central = host.home / ".local/share/sandbox-ai/auth/codex/.codex/auth.json"
    assert json.loads(central.read_text()) == logins["file"] and stat.S_IMODE(central.stat().st_mode) == 0o600
    assert start("central") == logins["file"], "a start without an option: the central login, not the host's"
    keys = [v["OPENAI_API_KEY"] for v in logins.values()]
    assert not [k for k in keys if k in json.dumps(host.calls())]
    found = leaked(host.directory, keys)
    assert all("/home/.codex/auth.json" in path or path in (str(central), str(host.home / ".codex/auth.json"))
               or path == str(login_file) for path in found), found


def test_a_git_login_given_while_it_runs_stays_out_of_the_sandbox(short_dir, proxy_run):
    """Finding 2 of credentials-verify-A (fixed 2026-11-19; a strict xfail until then): `git-logins give` (the app's
    Git logins tab: Give to a running sandbox…, the web's Git login of Allow) wrote the token into the run's session
    directory, which the sandbox mounts at /sandbox/session for its Git helper: the agent (root in its sandbox)
    could read it - while sandbox-ai git-logins promises that no token enters a sandbox. The login goes to the run's
    proxy now (PROXY_RUN stands for the proxy's run directory, which the supervisor writes: the fixture)."""
    host = a_host(short_dir / "h")
    run = Running(host)
    try:
        assert host.run("git-logins", "give", NAME, "http://localhost:3080", "--secret", "GITEA").returncode == 0
        deadline = time.monotonic() + 60
        while not [c for c in host.calls() if c[:1] == ["run"] and "--pod" in c] and time.monotonic() < deadline:
            time.sleep(0.1)  # (the start registers its run before it starts the sandbox)
        run_call = next(c for c in host.calls() if c[:1] == ["run"] and "--pod" in c)
        mounted = [a.split("src=", 1)[1].split(",dst=", 1)[0] for a in run_call
                   if a.startswith("type=bind,") and ",dst=/sandbox/session," in a]
        assert mounted == [str(run.session)] and "USER=root" in run_call, "what the agent's sandbox sees"
        inside = [p.name for p, data in everything_written(run.session).items() if SECRETS["GITEA"].encode() in data]
        assert inside == [], f"the token is readable in the sandbox: /sandbox/session/{inside}"
        assert proxy_login("http", "localhost", "127.0.0.1:3080") == ("x-access-token", SECRETS["GITEA"]), \
            "and the run's proxy logs in with it"
    finally:
        run.end()


def test_a_token_file_kept_by_its_relative_path_is_kept_absolute(short_dir, proxy_run):
    """Regression (credentials-verify-A, finding 4, fixed): `git-logins give RUN URL --file ../tok --keep` (net allow
    --git-token-file URL=../tok --keep) wrote the relative path into the project's trusted .sandbox-ai.json - and
    every later start of the project was refused ("use absolute paths"). The path is kept as the file it named."""
    host = a_host(short_dir / "h", secrets={})
    token = host.directory / "tok"
    token.write_text("gitea_filetoken_0123456789\n")
    token.chmod(0o600)
    run = Running(host)
    try:
        given = host.run("git-logins", "give", NAME, "https://git.example.org", "--file", "../tok", "--keep")
        assert given.returncode == 0, given.stderr
    finally:
        run.end()
    options = json.loads((host.workspace / ".sandbox-ai.json").read_text())["options"]
    assert options[options.index("--git-token-file") + 1] == f"https://git.example.org={token}"
    kept = short_dir / "kept"
    started = host.run("claude", "--no-tmux", "--name", "next", "--no-canaries", "--no-checkpoint",
                       env={"FAKE_PODMAN_RUN": "0:0", "FAKE_PODMAN_KEEP": str(kept)})
    assert started.returncode == 0, started.stderr
    env = json.loads((kept / "session" / "config.json").read_text())["env"]
    assert env["SANDBOX_GIT_TOKEN_0"] == "gitea_filetoken_0123456789"
