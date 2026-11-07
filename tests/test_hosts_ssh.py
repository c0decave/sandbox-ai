"""sandbox-ai hosts end to end: the real OpenSSH client (ssh) against a real SSH server on 127.0.0.1. The server is
asyncssh's (no sshd needed): public-key login only, each command run by sh in a remote HOME whose PATH has no
sandbox-ai, with or without a terminal, its exit status passed back. The remote sandbox-ai is a stand-in that
says what it got. Skipped without asyncssh (pip install asyncssh) or ssh."""
import asyncio
import contextlib
import json
import os
from pathlib import Path
import pty
import shutil
import subprocess
import sys
import threading
import time

import pytest

asyncssh = pytest.importorskip("asyncssh", reason="pip install asyncssh for the SSH end-to-end tests")
if not shutil.which("ssh") or not shutil.which("ssh-keygen"):
    pytest.skip("needs the OpenSSH client (ssh, ssh-keygen)", allow_module_level=True)

import test_hosts as th  # noqa: E402

# asyncssh's redirect to subprocess pipes leaves an unawaited Queue.join behind when a pipe closes; harmless here.
pytestmark = pytest.mark.filterwarnings("ignore:coroutine 'Queue.join' was never awaited:RuntimeWarning")

h = th.h
ROOT = Path(__file__).resolve().parents[1]
REMOTE_SANDBOX_AI = f"""#!{sys.executable}
import json, os, sys
args = sys.argv[1:]
if args == ["--version"]:
    print("sandbox-ai 0.1.31")
elif args == ["doctor", "--json"]:
    print(json.dumps([{{"status": "ok", "check": "Podman", "detail": "rootless 5.4", "hint": ""}}]))
elif args[:1] == ["exit"]:
    print("leaving", file=sys.stderr)
    sys.exit(int(args[1]))
elif args[:1] == ["stdin"]:
    data = sys.stdin.read()
    print(json.dumps({{"length": len(data), "head": data[:20]}}))
elif args[:1] == ["attach"]:
    print(json.dumps({{"tty": os.isatty(0) and os.isatty(1), "term": os.environ.get("TERM")}}))
elif args[:1] == ["big"]:
    sys.stdout.write("x" * int(args[1]))
else:
    print(json.dumps({{"args": args, "home": os.environ.get("HOME"), "cwd": os.getcwd()}}))
"""


class Server:
    """An SSH server in a thread of its own: public keys only, commands run by sh in HOME."""

    def __init__(self, home, authorized, host_key, port=0):
        self.home, self.authorized, self.host_key, self.wanted = home, authorized, host_key, port
        self.loop = asyncio.new_event_loop()
        self.logins, self.port, self.server = [], None, None
        started = threading.Event()
        self.thread = threading.Thread(target=self._run, args=(started,), daemon=True)
        self.thread.start()
        assert started.wait(10), "the SSH server did not start"

    def _run(self, started):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._start())
        started.set()
        self.loop.run_forever()

    async def _start(self):
        server = self

        class Auth(asyncssh.SSHServer):
            def begin_auth(self, username):
                server.logins.append(username)
                return True

            def password_auth_supported(self):
                return False

            def kbdint_auth_supported(self):
                return False

            def public_key_auth_supported(self):
                return True

            def validate_public_key(self, username, key):
                return username == "admin" and any(
                    key.public_data == k.public_data for k in server.authorized)

        self.server = await asyncssh.create_server(
            Auth, "127.0.0.1", self.wanted, reuse_address=True, server_host_keys=[self.host_key], process_factory=self._process,
            encoding=None, allow_scp=False)
        self.port = self.server.sockets[0].getsockname()[1]

    async def _process(self, process):
        env = {"HOME": str(self.home), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
        command = process.command or "exec sh -l"
        if process.get_terminal_type():
            env["TERM"] = process.get_terminal_type()
            master, slave = pty.openpty()
            child = await asyncio.create_subprocess_exec("sh", "-c", command, stdin=slave, stdout=slave,
                                                         stderr=slave, env=env, cwd=self.home)
            os.close(slave)
            reader = asyncio.get_running_loop().run_in_executor(None, self._copy_pty, master, process)

            async def keys():  # what is typed on the client, into the terminal
                with contextlib.suppress(Exception):
                    while True:
                        data = await process.stdin.read(4096)
                        if not data:
                            break
                        os.write(master, data)
            typing = asyncio.ensure_future(keys())
            status = await child.wait()
            typing.cancel()
            await reader
            process.exit(status)
            return
        child = await asyncio.create_subprocess_exec("sh", "-c", command, stdin=asyncio.subprocess.PIPE,
                                                     stdout=asyncio.subprocess.PIPE,
                                                     stderr=asyncio.subprocess.PIPE, env=env, cwd=self.home)
        await process.redirect(stdin=child.stdin, stdout=child.stdout, stderr=child.stderr)
        status = await child.wait()
        await process.stdout.drain()
        process.exit(status)

    def _copy_pty(self, master, process):
        while True:
            try:
                data = os.read(master, 65536)
            except OSError:
                break
            if not data:
                break
            self.loop.call_soon_threadsafe(process.stdout.write, data)
        os.close(master)

    def close(self):
        async def stop():
            self.server.close()
            try:  # a connection that is still being refused does not keep the test waiting
                await asyncio.wait_for(self.server.wait_closed(), 3)
            except asyncio.TimeoutError:
                pass
        asyncio.run_coroutine_threadsafe(stop(), self.loop).result(10)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(10)


def keygen(path):
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "test", "-f", str(path)], check=True)
    return asyncssh.read_public_key(str(path) + ".pub")


@pytest.fixture
def ssh(tmp_path, monkeypatch):
    """A running SSH server, a host entry ct1 for it with its own key, the remote sandbox-ai in ~/.local/bin."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    home = tmp_path / "remote-home"
    (home / ".local/bin").mkdir(parents=True)
    (home / ".local/bin/sandbox-ai").write_text(REMOTE_SANDBOX_AI)
    (home / ".local/bin/sandbox-ai").chmod(0o755)
    key = tmp_path / "id_ct1"
    server = Server(home, [keygen(key)], asyncssh.generate_private_key("ssh-ed25519"))
    h.save({"ct1": h.check("ct1", {"ssh": "admin@127.0.0.1", "port": server.port, "identity": str(key)})})
    state = {"server": server, "home": home, "key": key, "tmp": tmp_path}
    yield state
    state["server"].close()  # a test may have replaced it


def test_a_real_login_runs_the_remote_sandbox_ai(ssh):
    result = h.test("ct1")
    assert result["ssh"] and result["error"] is None, result
    assert result["version"] == "sandbox-ai 0.1.31" and result["doctor"][0]["check"] == "Podman"
    assert ssh["server"].logins[-1] == "admin"
    status, out, err = h.run("ct1", ["list", "--json"])
    assert status == 0, err
    assert json.loads(out) == {"args": ["list", "--json"], "home": str(ssh["home"]), "cwd": str(ssh["home"])}


def test_arguments_survive_a_real_remote_shell(ssh):
    marker = ssh["tmp"] / "pwned"
    args = ["start", "a b", "it's", f"$(touch {marker})", f"`touch {marker}`", "; touch " + str(marker), "\\n",
            "--", "-x", "ü€", "*", "~", "$HOME"]
    status, out, err = h.run("ct1", args)
    assert status == 0, err
    assert json.loads(out)["args"] == args and not marker.exists()


def test_exit_status_stderr_and_standard_input_pass(ssh):
    status, out, err = h.run("ct1", ["exit", "3"])
    assert status == 3 and "leaving" in err
    secret = "s3cr3t-value\n" * 1000  # how secrets reach the remote sandbox-ai: never as an argument
    status, out, err = h.run("ct1", ["stdin"], input=secret)
    assert status == 0 and json.loads(out) == {"length": len(secret), "head": secret[:20]}
    status, out, _ = h.run("ct1", ["big", str(3 << 20)])
    assert status == 0 and len(out) == 3 << 20


def test_an_attach_gets_a_terminal(ssh):
    """argv(tty=True) is what the app's and the web's terminals run: ssh -tt gives the remote side a terminal even
    without one here."""
    done = subprocess.run(h.argv("ct1", ["attach", "web"], tty=True), capture_output=True, text=True, timeout=30,
                          stdin=subprocess.DEVNULL)
    assert done.returncode == 0, done.stderr
    line = next(line for line in done.stdout.splitlines() if line.startswith("{"))
    assert json.loads(line) == {"tty": True, "term": os.environ.get("TERM") or "dumb"} or json.loads(line)["tty"]
    plain = subprocess.run(h.argv("ct1", ["attach", "web"]), capture_output=True, text=True, timeout=30,
                           stdin=subprocess.DEVNULL)
    assert json.loads(plain.stdout)["tty"] is False


def known(port):
    """Whether sandbox-ai's known_hosts has a key for 127.0.0.1:PORT (the entries may be hashed)."""
    return subprocess.run(["ssh-keygen", "-F", f"[127.0.0.1]:{port}", "-f", str(h.known_hosts_file())],
                          capture_output=True).returncode == 0


def test_the_host_key_is_learnt_once_then_held(ssh):
    port = ssh["server"].port
    assert not known(port)
    assert h.run("ct1", ["--version"])[0] == 0
    assert known(port), "the first key is learnt into sandbox-ai's own file"
    assert h.run("ct1", ["--version"])[0] == 0, "and accepted again"
    learnt = h.known_hosts_file().read_text()
    # Another server (another key) answering at the same address: refused, never learnt over the old key.
    ssh["server"].close()
    ssh["server"] = Server(ssh["home"], [asyncssh.read_public_key(str(ssh["key"]) + ".pub")],
                           asyncssh.generate_private_key("ssh-ed25519"), port=port)
    status, out, err = h.run("ct1", ["--version"])
    assert status == 255 and "sandbox-ai 0.1.31" not in out
    assert "Host key verification failed" in err or "IDENTIFICATION HAS CHANGED" in err, err
    assert h.known_hosts_file().read_text() == learnt, "the changed key was not written over the old one"
    failed = h.test("ct1")
    assert not failed["ssh"] and "Host key verification failed" in failed["error"]
    assert ssh["server"].logins == [], "no login was tried at a host whose key changed"


def test_a_key_the_host_does_not_know_fails_at_once(ssh):
    """BatchMode: no password or passphrase prompt that would hang the app or the web interface."""
    stranger = ssh["tmp"] / "id_stranger"
    keygen(stranger)
    h.save({"ct1": h.check("ct1", {"ssh": "admin@127.0.0.1", "port": ssh["server"].port, "identity": str(stranger)})})
    started = time.monotonic()
    status, _, err = h.run("ct1", ["--version"], timeout=30)
    assert status == 255 and "Permission denied" in err and time.monotonic() - started < 20
    h.save({"ct1": h.check("ct1", {"ssh": "root@127.0.0.1", "port": ssh["server"].port, "identity": str(ssh["key"])})})
    assert h.run("ct1", ["--version"], timeout=30)[0] == 255, "another user, the same key: refused"


def test_nobody_listening(ssh, tmp_path):
    import socket
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    h.save({"gone": h.check("gone", {"ssh": "admin@127.0.0.1", "port": port, "identity": str(ssh["key"])})})
    result = h.test("gone")
    assert not result["ssh"] and "Connection refused" in result["error"]


def test_the_host_s_own_command(ssh):
    other = ssh["home"] / "opt" / "sandbox-ai"
    other.parent.mkdir()
    other.write_text("#!/bin/sh\necho \"custom:$#:$1\"\n")
    other.chmod(0o755)
    h.save({"ct1": {**h.load()["ct1"], "command": str(other)}})
    assert h.run("ct1", ["a b", "c"])[1].strip() == "custom:2:a b"


def test_the_command_line_over_real_ssh(ssh):
    env = {**os.environ, "SANDBOX_AI_NO_DEPRECATION": "1"}
    main = ROOT / "sandbox" / "main.py"
    done = subprocess.run([sys.executable, "-I", str(main), "hosts", "run", "ct1", "--", "list", "--json"],
                          capture_output=True, text=True, env=env, timeout=60)
    assert done.returncode == 0 and json.loads(done.stdout)["args"] == ["list", "--json"], done.stderr
    done = subprocess.run([sys.executable, "-I", str(main), "hosts", "test", "ct1", "--json"],
                          capture_output=True, text=True, env=env, timeout=60)
    assert done.returncode == 0 and json.loads(done.stdout)["version"] == "sandbox-ai 0.1.31", done.stderr


def test_the_web_interface_reaches_a_remote_sandbox_over_ssh(ssh, monkeypatch):
    """The whole way, as the browser goes: HTTPS login with password and TOTP, the host's starts as JSON, an
    action, and a terminal: WebSocket -> pty -> ssh -tt -> the host's sandbox-ai attach, typing included."""
    import ssl
    import threading
    import test_web as tw
    monkeypatch.setenv("XDG_STATE_HOME", str(ssh["tmp"] / "state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(ssh["tmp"] / "data"))
    monkeypatch.setattr(tw.w, "SCRYPT", tw.FAST)
    attach = ssh["home"] / ".local/bin/sandbox-ai"
    attach.write_text(REMOTE_SANDBOX_AI.replace(
        '''    print(json.dumps({"tty": os.isatty(0) and os.isatty(1), "term": os.environ.get("TERM")}))''',
        '''    print(json.dumps({"tty": os.isatty(0) and os.isatty(1), "attach": args[1:]}), flush=True)
    print("typed:", sys.stdin.readline().strip(), flush=True)'''))
    secret = tw.add_user()
    started, holder = threading.Event(), {}
    thread = threading.Thread(target=tw.w.serve, kwargs={"listen": "127.0.0.1:0", "out": lambda line: None,
                                                         "ready": lambda srv: (holder.update(server=srv),
                                                                               started.set())}, daemon=True)
    thread.start()
    assert started.wait(30)
    server = {"port": holder["server"].server_address[1],
              "context": ssl.create_default_context(cafile=str(tw.w.cert_files()[0]))}
    try:
        status, headers, body = tw.https(server, "POST", "/api/login", {
            "user": "admin", "password": "correct horse battery", "code": tw.code(secret)})
        assert status == 200, body
        cookie, csrf = headers["Set-Cookie"].split(";")[0], json.loads(body)["csrf"]
        status, _, body = tw.https(server, "GET", "/api/hosts/ct1/starts", headers={"Cookie": cookie})
        assert status == 200 and json.loads(body)["args"] == ["list", "--json"], body
        status, _, body = tw.https(server, "POST", "/api/hosts/ct1/starts/web/pause", {},
                                   headers={"Cookie": cookie, "X-CSRF-Token": csrf})
        assert status == 200 and '"pause", "web"' in json.loads(body)["output"], body
        sock, head, rest, _ = tw.websocket(server, f"host=ct1&kind=attach&name=web&csrf={csrf}", cookie)
        assert head.startswith("HTTP/1.1 101"), head
        text = tw.read_messages(sock, rest, b'"attach"')
        assert b'"tty": true' in text and b'"attach": ["web"]' in text, text
        sock.sendall(tw.frame(b"over ssh\r", 2))
        assert b"typed: over ssh" in tw.read_messages(sock, b"", b"typed: over ssh")
        sock.close()
    finally:
        holder["server"].shutdown()
