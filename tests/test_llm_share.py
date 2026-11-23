"""LLM servers for all hosts (sandbox-ai llm share; user 2026-11-22: "a flag for registered servers in the LLM servers
area that makes them reachable for all hosts"): the entry copied to every host of `sandbox-ai hosts` and kept in sync,
the key only to the hosts ticked, each host's check of the server, removal, a host offline tried again.

End to end through the real entry point: ./sandbox-ai as this machine's user, and as the user of each other host - an
ssh on PATH runs the remote command (the real one hosts.py builds) with sh as that host's user (another HOME, its own
llm.json and secret store), the standard input passed on: what `secrets send` writes reaches `secrets set` there. The
LLM server is a stand-in on 127.0.0.1, registered at a LAN address (plain http to a local address, as `llm add` allows
it): the other hosts' python3 connects that address to the stand-in (SHIM below; no network needed). It wants the
key as a Bearer token."""
import http.server
import importlib.util
import json
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import sys
import threading

import pytest

ROOT = Path(__file__).resolve().parents[1]
KEY = "sk-share-SECRET-0123456789abcdef"  # shaped like a key, valid nowhere
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX hosts")

SSH = r'''#!PYTHON
import json, os, subprocess, sys
args = sys.argv[1:]
root = os.environ["FAKE_REMOTES"]
with open(os.path.join(root, "ssh.log"), "a") as stream:
    stream.write(json.dumps(args) + "\n")
if "-O" in args or "-M" in args or "-N" in args or "-G" in args:
    sys.exit(255)
host = args[args.index("--") + 1].rpartition("@")[2]
if os.path.exists(os.path.join(root, host + ".offline")):
    print(f"ssh: connect to host {host} port 22: Connection timed out", file=sys.stderr)
    sys.exit(255)
env = json.load(open(os.path.join(root, host + ".json")))
sys.exit(subprocess.run(["/bin/sh", "-c", args[-1]], env=env, cwd=env["HOME"]).returncode)
'''


LAN = "10.254.254.254"  # the server's address as registered: a private one, reached on the hosts through SHIM
# python3 of the other hosts: a connection to LAN goes to the stand-in on 127.0.0.1 (the same port); nothing else changes.
SHIM = """import runpy, socket, sys
_real = socket.create_connection
def _connect(address, *args, **kwargs):
    if address[0] == %r:
        address = ("127.0.0.1", address[1])
    return _real(address, *args, **kwargs)
socket.create_connection = _connect
args = sys.argv[1:]
while args and args[0].startswith("-") and args[0] not in ("-c", "-m", "-"):
    args = args[1:]
sys.argv = args
runpy.run_path(args[0], run_name="__main__")
""" % LAN


class Server:
    """An OpenAI-compatible stand-in (vLLM's /v1/models) on this machine's LAN address: the key as a Bearer token,
    HTTP 401 without it; the Authorization headers it saw are kept."""

    def __init__(self, context=None):
        seen = self.seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - http.server's name
                seen.append(self.headers.get("Authorization"))
                if self.headers.get("Authorization") != f"Bearer {KEY}":
                    body, status = b'{"error": "no key"}', 401
                else:
                    body, status = json.dumps({"data": [{"id": "qwen-coder", "max_model_len": 32768}]}).encode(), 200
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        if context is not None:
            self.httpd.socket = context.wrap_socket(self.httpd.socket, server_side=True)
        self.port = self.httpd.server_address[1]
        self.url = f"http://{LAN}:{self.port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class World:
    """This machine's user and the other hosts' users, each with a HOME of its own; ssh reaches the hosts."""

    def __init__(self, tmp_path):
        self.root = tmp_path
        self.remotes = tmp_path / "remotes"
        self.remotes.mkdir()
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        (self.bin / "ssh").write_text(SSH.replace("PYTHON", sys.executable, 1))
        (self.bin / "ssh").chmod(0o755)
        self.homes = {}
        self.env = self.user("local", self.bin)
        self.env["FAKE_REMOTES"] = str(self.remotes)

    def user(self, name, bin_dir):
        home = self.root / name
        runtime = self.root / f"run-{name}"
        for path in (home, self.root / f"tmp-{name}"):
            path.mkdir(parents=True, exist_ok=True)
        runtime.mkdir(mode=0o700, exist_ok=True)
        self.homes[name] = home
        return {"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"), "XDG_DATA_HOME": str(home / ".local/share"),
                "XDG_STATE_HOME": str(home / ".local/state"), "XDG_CACHE_HOME": str(home / ".cache"),
                "XDG_RUNTIME_DIR": str(runtime), "PATH": f"{bin_dir}:/usr/bin:/bin", "LC_ALL": "C.UTF-8",
                "TERM": "dumb", "SANDBOX_AI_SECRETS": "file", "SANDBOX_AI_SECRETS_PLAIN": "1",
                "SANDBOX_AI_SSH_MULTIPLEX": "0", "TMPDIR": str(self.root / f"tmp-{name}")}

    def host(self, name, add=True):
        """Another host NAME (admin@NAME), its sandbox-ai this checkout's, added to sandbox-ai hosts here."""
        bin_dir = self.root / f"bin-{name}"
        bin_dir.mkdir()
        (bin_dir / "sandbox-ai").symlink_to(ROOT / "sandbox-ai")
        (bin_dir / "shim.py").write_text(SHIM)
        (bin_dir / "python3").write_text(f'#!/bin/sh\nexec {sys.executable} -I "{bin_dir}/shim.py" "$@"\n')
        (bin_dir / "python3").chmod(0o755)
        (self.remotes / f"{name}.json").write_text(json.dumps(self.user(name, bin_dir)))
        if add:
            done = self.run("hosts", "add", name, f"admin@{name}")
            assert done.returncode == 0, done.stderr
            return done
        return None

    def host_again(self, name):
        """HOST (made before) added to sandbox-ai hosts again."""
        done = self.run("hosts", "add", name, f"admin@{name}")
        assert done.returncode == 0, done.stderr
        return done

    def run(self, *args, input=None, env=None):
        return subprocess.run([str(ROOT / "sandbox-ai"), *args], env=env or self.env, input=input,
                              capture_output=True, text=True, timeout=300)

    def on(self, host, *args, input=None):
        """`sandbox-ai ARGS` as the user of HOST, on that host."""
        return self.run(*args, input=input, env=json.loads((self.remotes / f"{host}.json").read_text()))

    def store(self, who):
        path = self.homes[who] / ".config/sandbox-ai/llm.json"
        return json.loads(path.read_text())["endpoints"] if path.exists() else {}

    def secret_file(self, who, name):
        return self.homes[who] / ".local/share/sandbox-ai/secrets" / name

    def offline(self, host, off=True):
        marker = self.remotes / f"{host}.offline"
        marker.touch() if off else marker.unlink()

    def ssh_lines(self):
        path = self.remotes / "ssh.log"
        return path.read_text() if path.exists() else ""

    def shares(self):
        return json.loads((self.homes["local"] / ".config/sandbox-ai/llm-shares.json").read_text())

    def listed(self, who="local"):
        done = self.on(who, "llm", "list", "--json") if who != "local" else self.run("llm", "list", "--json")
        assert done.returncode == 0, done.stderr
        return {entry["name"]: entry for entry in json.loads(done.stdout)}


def said(done):
    text = done.stdout + done.stderr
    assert KEY not in text, "the key was printed"
    return text


def files_with(root, value, skip=()):
    found = []
    for path in Path(root).rglob("*"):
        if path.is_file() and not path.is_symlink() and not any(str(path).startswith(str(s)) for s in skip):
            try:
                if value.encode() in path.read_bytes():
                    found.append(str(path))
            except OSError:
                pass
    return found


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


@pytest.fixture
def server():
    made = Server()
    yield made
    made.close()


def keep_key(world, who="local"):
    done = world.run("secrets", "set", "LMS_KEY", input=KEY + "\n") if who == "local" else \
        world.on(who, "secrets", "set", "LMS_KEY", input=KEY + "\n")
    assert done.returncode == 0, done.stderr


def add_server(world, server, *more):
    done = world.run("llm", "add", "lms", "--kind", "vllm", "--url", server.url, "--api-key-secret", "LMS_KEY",
                     "--model", "qwen-coder", *more)
    assert done.returncode == 0, done.stderr


def test_a_shared_server_reaches_every_host_its_key_only_the_ticked_ones(world, server):
    keep_key(world)
    add_server(world, server)
    world.host("ct1")
    world.host("ct2")
    shared = world.run("llm", "share", "lms", "--key-to", "ct1")
    text = said(shared)
    assert shared.returncode == 0, text
    assert "lms: copied to ct1" in text and "lms: copied to ct2" in text
    assert "for all hosts (llm share): key to ct1" in text
    # Each host has the entry as it is here, marked as managed from this machine; nothing secret in it.
    origin = world.shares()["origin"]
    for host in ("ct1", "ct2"):
        copy = world.store(host)["lms"]
        assert copy == {"kind": "vllm", "url": server.url, "api_key_secret": "LMS_KEY", "model": "qwen-coder",
                        "shared_from": origin}
    # The key only on ct1, kept in its own secret store; it never was on a command line or in a file here or there
    # but the secret stores.
    assert world.secret_file("ct1", "LMS_KEY").read_text().strip() == KEY
    assert not world.secret_file("ct2", "LMS_KEY").exists()
    assert KEY not in world.ssh_lines()
    stores = [world.homes[w] / ".local/share/sandbox-ai/secrets" for w in ("local", "ct1", "ct2")]
    assert files_with(world.root, KEY, skip=stores) == [], "the key in a file (a log, the state, a copy)"
    # Each host checked the server from there: ct1 with the key it has, ct2 without (an answer all the same).
    assert f"Bearer {KEY}" in server.seen and None in server.seen
    listed = world.listed()["lms"]
    hosts = {h["host"]: h for h in listed["shared"]["hosts"]}
    assert listed["shared"]["key_to"] == ["ct1"]
    assert (hosts["ct1"]["copied"], hosts["ct1"]["in_sync"], hosts["ct1"]["key"], hosts["ct1"]["reachable"],
            hosts["ct1"]["http"]) == (True, True, "sent", True, 200)
    assert (hosts["ct2"]["copied"], hosts["ct2"]["in_sync"], hosts["ct2"]["key"], hosts["ct2"]["reachable"],
            hosts["ct2"]["keys_missing"]) == (True, True, "missing", True, ["LMS_KEY"])
    text = said(world.run("llm", "list"))
    assert "ct1: copied, in sync, key LMS_KEY sent, reachable from there" in text
    assert "ct2: copied, in sync, key missing there (LMS_KEY: sandbox-ai hosts run ct2 -- secrets set LMS_KEY, or " \
           "sandbox-ai llm share lms --key-to ct1,ct2), reachable from there (HTTP 401 without a key)" in text
    # On the other host: listed as shared from here, and a change there says so.
    there = world.on("ct2", "llm", "list")
    assert f"shared from {origin}" in there.stdout
    assert f"managed from {origin}" in world.on("ct2", "llm", "show", "lms").stdout
    assert world.listed("ct2")["lms"]["shared_from"] == origin
    # ... and says that it does not keep the key, with how to give it one; ct1 has it.
    assert f"its key is not kept on this host: sandbox-ai secrets set LMS_KEY, or on {origin}: sandbox-ai llm share " \
           "lms --key-to HOST,..." in there.stdout
    assert (world.listed("ct2")["lms"]["key_here"], world.listed("ct1")["lms"]["key_here"]) == ("missing", "there")
    assert "its key is not kept" not in world.on("ct1", "llm", "show", "lms").stdout
    changed_there = world.on("ct2", "llm", "set", "lms", "--param", "temperature=0.9")
    assert changed_there.returncode == 0 and f"managed from {origin}" in changed_there.stdout
    # A change here is copied at once; the one made there is replaced by it.
    changed = world.run("llm", "set", "lms", "--param", "temperature=0.3")
    assert changed.returncode == 0, changed.stderr
    assert "lms: copied to ct1" in changed.stdout and "lms: copied to ct2" in changed.stdout
    for host in ("ct1", "ct2"):
        assert world.store(host)["lms"]["params"] == {"*": {"temperature": 0.3}}
    # Nothing to do: nothing copied again (idempotent), no SSH at all.
    before = world.ssh_lines()
    assert said(world.run("llm", "list")).count("copied to") == 0
    assert world.ssh_lines() == before
    # A host added later gets it as well.
    added = world.host("ct3")
    assert "lms: copied to ct3" in added.stdout
    assert world.store("ct3")["lms"]["shared_from"] == origin
    # The key taken off ct1: the copy sent there is deleted, the entry stays.
    unkeyed = world.run("llm", "share", "lms", "--no-key")
    assert unkeyed.returncode == 0, unkeyed.stderr
    assert not world.secret_file("ct1", "LMS_KEY").exists() and "lms" in world.store("ct1")
    # A host that leaves sandbox-ai hosts takes no copy with it: removed there first (and a key sent there).
    world.run("llm", "share", "lms", "--key-to", "ct3")
    assert world.secret_file("ct3", "LMS_KEY").exists()
    left = world.run("hosts", "remove", "ct3")
    assert left.returncode == 0 and "lms: copy removed on ct3" in left.stdout and "host ct3 removed" in left.stdout
    assert "lms" not in world.store("ct3") and not world.secret_file("ct3", "LMS_KEY").exists()
    assert "ct3" not in world.shares()["shares"]["lms"]["hosts"]
    world.host_again("ct3")
    # Unshared: every copy removed; the server stays here.
    keyed = world.run("llm", "share", "lms", "--key-to", "ct2")
    assert keyed.returncode == 0 and world.secret_file("ct2", "LMS_KEY").exists()
    gone = world.run("llm", "unshare", "lms")
    assert gone.returncode == 0, gone.stderr
    assert "lms is not shared any more" in gone.stdout
    for host in ("ct1", "ct2", "ct3"):
        assert "lms" not in world.store(host) and not world.secret_file(host, "LMS_KEY").exists()
    assert "lms" in world.store("local") and world.secret_file("local", "LMS_KEY").exists()
    assert world.listed()["lms"]["shared"] is None
    assert world.shares()["shares"] == {}
    # Every copy, key and removal is a line of this machine's launcher.log (names, never the key).
    logged = (world.homes["local"] / ".local/state/sandbox-ai/launcher.log").read_text()
    for line in ("share lms: copied to ct1", "share lms: key LMS_KEY sent to ct1", "share lms: key LMS_KEY deleted "
                 "on ct1", "share lms: copy removed on ct2", "share lms: not for all hosts any more"):
        assert line in logged, line
    assert KEY not in logged


def test_a_host_that_does_not_answer_is_tried_again_at_the_next_command(world, server):
    keep_key(world)
    add_server(world, server)
    world.host("ct1")
    world.host("ct2")
    world.offline("ct2")
    shared = world.run("llm", "share", "lms")
    assert shared.returncode == 1
    assert "lms: copied to ct1" in shared.stdout
    assert "note: lms is not on ct2 as it is here yet: ct2 did not answer over SSH" in shared.stdout
    hosts = {h["host"]: h for h in world.listed()["lms"]["shared"]["hosts"]}
    assert (hosts["ct2"]["copied"], hosts["ct2"]["offline"], hosts["ct2"]["pending"]) == (False, True, "copy")
    assert "ct2: not copied yet: ct2 did not answer over SSH" in world.run("llm", "list").stdout
    # Back: tried again by the next command once it is due (RETRY seconds after the last try), and said.
    world.offline("ct2", False)
    state = world.shares()
    state["shares"]["lms"]["hosts"]["ct2"]["tried_at"] = 0
    (world.homes["local"] / ".config/sandbox-ai/llm-shares.json").write_text(json.dumps(state))
    cached = world.run("hosts", "list", "--cached")
    assert cached.returncode == 0 and "copied" not in cached.stdout, "--cached asks no host"
    again = world.run("hosts", "set", "ct2", "--update-sudo", "off")  # (a command of hosts that asks no host itself)
    assert "lms: copied to ct2" in again.stdout, again.stdout + again.stderr
    assert "lms" in world.store("ct2")
    # Removed here while ct1 is offline: its copy is removed when it answers again.
    world.offline("ct1")
    removed = world.run("llm", "remove", "lms")
    assert "lms removed" in removed.stdout and "copy removed on ct2" in removed.stdout
    assert "its copy on ct1 is not removed yet" in removed.stdout
    world.offline("ct1", False)
    state = world.shares()
    state["shares"]["lms"]["hosts"]["ct1"]["tried_at"] = 0
    (world.homes["local"] / ".config/sandbox-ai/llm-shares.json").write_text(json.dumps(state))
    later = world.run("llm", "list", "--json")
    assert "lms: copy removed on ct1" in later.stderr and json.loads(later.stdout) == []
    assert "lms" not in world.store("ct1") and world.shares()["shares"] == {}


def test_an_entry_of_the_hosts_own_is_never_replaced(world, server):
    add_server(world, server)
    world.host("ct1", add=False)
    own = world.on("ct1", "llm", "add", "lms", "--kind", "ollama", "--url", "http://192.168.7.7:11434")
    assert own.returncode == 0, own.stderr
    world.run("hosts", "add", "ct1", "admin@ct1")
    shared = world.run("llm", "share", "lms")
    assert shared.returncode == 1
    assert "note: lms is not on ct1 as it is here yet: ct1: it has an LLM server lms of its own (not shared from" in \
        shared.stdout
    assert world.store("ct1")["lms"] == {"kind": "ollama", "url": "http://192.168.7.7:11434"}
    # ... nor removed by unshare, which is done all the same: nothing of this share is there
    gone = world.run("llm", "unshare", "lms")
    assert gone.returncode == 0 and "lms is not shared any more" in gone.stdout, gone.stdout + gone.stderr
    assert world.store("ct1")["lms"]["kind"] == "ollama" and world.shares()["shares"] == {}


def test_what_is_not_shared_and_what_a_copy_cannot_carry(world, server):
    world.host("ct1")
    loop = world.run("llm", "add", "here", "--kind", "ollama", "--url", "http://127.0.0.1:11434")
    assert loop.returncode == 0, loop.stderr
    refused = world.run("llm", "share", "here")
    assert refused.returncode == 1 and "is this machine's loopback" in refused.stderr
    assert "here" not in world.store("ct1")
    add_server(world, server)
    unknown = world.run("llm", "share", "lms", "--key-to", "nowhere")
    assert unknown.returncode == 1 and "--key-to: no host nowhere" in unknown.stderr
    # `llm receive` there takes the settings of an entry only: no other field (a key's value has no field)
    for record, why in (({"kind": "vllm", "url": server.url, "api_key": KEY}, "fields a copy does not take"),
                        ({"kind": "vllm", "url": "http://127.0.0.1:8000"}, "loopback"),
                        ({"kind": "vllm", "url": server.url, "shared_from": "x"}, "fields a copy does not take")):
        done = world.on("ct1", "llm", "receive", "--from", "box", input=json.dumps({"name": "x", "record": record}))
        assert done.returncode == 1 and why in done.stdout + done.stderr, (record, done.stdout, done.stderr)
        assert "x" not in world.store("ct1")
    # A copy is not shared on from the host that has it.
    world.run("llm", "share", "lms")
    onward = world.on("ct1", "llm", "share", "lms")
    assert onward.returncode == 1 and "is managed from" in onward.stderr


def test_a_server_the_host_cannot_reach_is_said(world, server):
    keep_key(world)
    with socket.socket() as probe:  # a port nobody listens on: refused at once
        probe.bind(("127.0.0.1", 0))
        closed = probe.getsockname()[1]
    url = f"http://{LAN}:{closed}"
    assert world.run("llm", "add", "far", "--kind", "vllm", "--url", url).returncode == 0
    world.host("ct1")
    shared = world.run("llm", "share", "far")
    assert shared.returncode == 0, shared.stderr
    assert "ct1: copied, in sync, no key needed, NOT reachable from there" in shared.stdout
    hosts = {h["host"]: h for h in world.listed()["far"]["shared"]["hosts"]}
    assert hosts["ct1"]["reachable"] is False and hosts["ct1"]["reach_error"]


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
def test_the_ca_goes_with_the_copy_header_secrets_are_keys_and_a_hosts_own_secret_stays(world, tmp_path):
    """https with the server's own CA: the CA (no secret) is copied with the entry, and the host reaches the server
    with it. Every secret the entry names is its key: the API key and a header secret. A host that keeps a secret of
    that name already uses its own - never replaced, never deleted by unshare; only what was sent is deleted."""
    spec = importlib.util.spec_from_file_location("sandbox_runtime_share_test", ROOT / "sandbox/runtime.py")
    rt = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rt)
    pki = tmp_path / "pki"
    rt.create_ca(pki, tmp_path / "ca-out", bundles=())
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(rt.server_certificate(pki, LAN)), str(pki / "leaf.key"))
    server = Server(context)
    try:
        keep_key(world)
        assert world.run("secrets", "set", "GW_KEY", input="gw-local-value\n").returncode == 0
        url = f"https://{LAN}:{server.port}"
        added = world.run("llm", "add", "gpu", "--kind", "vllm", "--url", url, "--api-key-secret", "LMS_KEY",
                          "--header-secret", "X-Gateway=GW_KEY", "--ca", str(pki / "ca.pem"))
        assert added.returncode == 0, added.stderr
        world.host("ct1", add=False)
        assert world.on("ct1", "secrets", "set", "GW_KEY", input="gw-own-value\n").returncode == 0
        world.run("hosts", "add", "ct1", "admin@ct1")
        shared = world.run("llm", "share", "gpu", "--key-to", "ct1")
        assert shared.returncode == 0, shared.stdout + shared.stderr
        copy = world.store("ct1")["gpu"]
        assert copy["ca"] == "gpu.pem" and copy["headers"] == {"X-Gateway": "GW_KEY"}
        assert (world.homes["ct1"] / ".config/sandbox-ai/llm/gpu.pem").read_text() == (pki / "ca.pem").read_text()
        assert world.secret_file("ct1", "LMS_KEY").read_text().strip() == KEY
        assert world.secret_file("ct1", "GW_KEY").read_text().strip() == "gw-own-value", "its own: never replaced"
        found = {h["host"]: h for h in world.listed()["gpu"]["shared"]["hosts"]}["ct1"]
        assert (found["reachable"], found["http"], found["key"], found["keys_sent"]) == (True, 200, "there",
                                                                                         ["LMS_KEY"])
        assert f"Bearer {KEY}" in server.seen, "reached over TLS with the copied CA, with the key"
        assert world.run("llm", "unshare", "gpu").returncode == 0
        assert not world.secret_file("ct1", "LMS_KEY").exists() and world.secret_file("ct1", "GW_KEY").exists()
        assert not (world.homes["ct1"] / ".config/sandbox-ai/llm/gpu.pem").exists() and "gpu" not in world.store("ct1")
    finally:
        server.close()


def test_a_host_whose_sandbox_ai_is_too_old_is_named_with_the_update(world, server):
    add_server(world, server)
    world.host("old")
    stand_in = world.root / "bin-old/sandbox-ai"
    stand_in.unlink()
    stand_in.write_text("#!/bin/sh\necho \"sandbox-ai llm: error: argument command: invalid choice: 'receive'\" >&2\n"
                        "exit 2\n")
    stand_in.chmod(0o755)
    shared = world.run("llm", "share", "lms")
    assert shared.returncode == 1
    assert "the sandbox-ai of old is too old for shared LLM servers: update it there (sandbox-ai hosts update old)" \
        in shared.stdout


def test_a_damaged_state_file_is_said_and_the_listing_still_works(world, server):
    add_server(world, server)
    path = world.homes["local"] / ".config/sandbox-ai/llm-shares.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    path.chmod(0o600)
    listed = world.run("llm", "list", "--json")
    assert listed.returncode == 0 and json.loads(listed.stdout)[0]["name"] == "lms"
    assert "shared LLM servers:" in listed.stderr and "is not valid JSON" in listed.stderr
    assert world.run("llm", "show", "lms").returncode == 0


def test_a_host_that_left_the_list_gets_no_key_when_its_name_comes_back(world, server):
    """Security review 2026-11-22: --key-to names hosts, and `hosts remove` took the copy and the key away but left
    the name in key_to - a host added later under that name (another machine, an address that changed hands) got the
    key at once, by `hosts add` itself, without anyone ticking it. The key goes again only when it is given again."""
    keep_key(world)
    add_server(world, server)
    world.host("ct1")
    shared = world.run("llm", "share", "lms", "--key-to", "ct1")
    assert shared.returncode == 0, shared.stderr
    assert world.secret_file("ct1", "LMS_KEY").exists()
    left = world.run("hosts", "remove", "ct1")
    assert left.returncode == 0, left.stderr
    assert not world.secret_file("ct1", "LMS_KEY").exists()
    assert world.shares()["shares"]["lms"]["key_to"] == []
    added = world.host_again("ct1")
    assert "lms: copied to ct1" in added.stdout, "the entry is for every host of the list"
    assert not world.secret_file("ct1", "LMS_KEY").exists(), "the key went to a host nobody ticked"
    assert world.listed()["lms"]["shared"]["key_to"] == []
    again = world.run("llm", "share", "lms", "--key-to", "ct1")
    assert again.returncode == 0 and world.secret_file("ct1", "LMS_KEY").exists()


def test_a_host_gone_from_the_list_by_hand_is_no_longer_one_its_key_goes_to(world, server):
    """The same for a host taken out of hosts.json another way (edited by hand, a list restored): the next round
    forgets it in key_to too, before a host of that name can come back."""
    keep_key(world)
    add_server(world, server)
    world.host("ct1")
    world.host("ct2")
    assert world.run("llm", "share", "lms", "--key-to", "ct1,ct2").returncode == 0
    hosts_file = world.homes["local"] / ".config/sandbox-ai/hosts.json"
    hosts = json.loads(hosts_file.read_text())
    listed = hosts["hosts"] if isinstance(hosts.get("hosts"), dict) else hosts
    listed.pop("ct1")
    hosts_file.write_text(json.dumps(hosts))
    assert world.run("llm", "sync").returncode == 0
    assert world.shares()["shares"]["lms"]["key_to"] == ["ct2"]


def test_a_name_that_stands_for_another_machine_now_gets_no_key(world, server):
    """The key was given to the machine a name stood for: hosts.json changed so that ct1 is another machine (edited,
    restored from elsewhere) - that machine gets the entry as a new host, never the key; the share says so."""
    keep_key(world)
    add_server(world, server)
    world.host("ct1")
    world.host("other", add=False)
    assert world.run("llm", "share", "lms", "--key-to", "ct1").returncode == 0
    assert world.shares()["shares"]["lms"]["key_for"] == {"ct1": "admin@ct1:22"}
    hosts_file = world.homes["local"] / ".config/sandbox-ai/hosts.json"
    hosts = json.loads(hosts_file.read_text())
    hosts["hosts"]["ct1"]["ssh"] = "admin@other"
    hosts_file.write_text(json.dumps(hosts))
    synced = world.run("llm", "sync")
    text = said(synced)
    assert "ct1 is another machine now (admin@other:22): its key goes there only when you name it again" in text
    assert world.store("other")["lms"]["shared_from"] == world.shares()["origin"]
    assert not world.secret_file("other", "LMS_KEY").exists(), "the key went to another machine"
    assert world.shares()["shares"]["lms"]["key_to"] == []
