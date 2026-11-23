"""What users ran into in live use, end to end: the real ./sandbox-ai entry point as a subprocess, from the start
command to the point where the sandbox's pod would be made, with only Podman, tmux and npm faked (live_fakes.py:
a per-user image store, as rootless Podman keeps one). One test per report, named after it, its error line quoted.
"""
import contextlib
import json
import os
from pathlib import Path
import pty
import pwd
import select
import shutil
import socket
import subprocess
import tempfile
import threading
import time

import pytest

from live_fakes import OLD_TOR_FRAGMENT, Host, ROOT, old_tor_variant

# A private preset folder with the Tor presets the user ran (tor-researcher, tor-research-v2): only where it is given.
PRIVATE_PRESETS = Path(os.environ.get("SANDBOX_AI_PRIVATE_PRESETS") or "/nonexistent")
REVIEWED = ("qwen", "dsh", "hermes", "goose")  # built at their reviewed release: no version lookup
USER = pwd.getpwuid(os.getuid()).pw_name

@pytest.fixture
def host(tmp_path):
    return Host(tmp_path)


def infra_image(host):
    stopped = host.stopped_at()
    assert stopped is not None and stopped[:2] == ["pod", "create"], "the start never got to its pod"
    return stopped[stopped.index("--infra-image") + 1]


# --- A: "Podman failed (exit status 125) before the sandbox started: Error: localhost/qwen-sandbox:latest: image not
# known", later "The image of qwen (...) is not built on this host ... sandbox-ai build qwen" ------------------------

@pytest.mark.parametrize("agent", REVIEWED)
def test_qwen_image_not_known_the_start_builds_it(host, agent):
    """User: 'Podman failed (exit status 125) before the sandbox started: Error: localhost/qwen-sandbox:latest: image
    not known'. A start whose agent image is not in this user's store builds it (sandbox-ai build AGENT, into the
    user's own store) and goes on to its pod - once."""
    image = f"localhost/{agent}-sandbox:latest"
    result = host.run(agent)
    assert f"the image {image} is not built for this user yet: building it now (sandbox-ai build {agent}" \
        in result.stderr, result.stderr
    assert "image not known" not in result.stderr and "is not built on this host" not in result.stderr
    assert image in host.images() and len(host.builds()) == 1
    assert infra_image(host) == image
    # The next start finds it: nothing is built again.
    host.stop_path.unlink()
    again = host.run(agent)
    assert "building it now" not in again.stderr and len(host.builds()) == 1 and infra_image(host) == image


def test_qwen_session_start_builds_before_its_session(host):
    """User: the app's session start of qwen ended in 'image not known'. A session start (--session NAME, as the app
    starts one) builds the image in the terminal it was started in, before its supervisor runs; the supervisor then
    gets to the pod with the image it built."""
    result = host.run("qwen", "--session", "q1")
    assert "building it now (sandbox-ai build qwen" in result.stderr
    assert len(host.builds()) == 1 and infra_image(host) == "localhost/qwen-sandbox:latest"


def test_qwen_dry_run_says_the_start_builds_it(host):
    """User: 'The image of qwen (localhost/qwen-sandbox:latest) is not built on this host … sandbox-ai build qwen'
    from the app's check before a start. That check (a dry run: `sandbox-ai AGENT ... --dry-run`, the Start page's
    and the Board's) accepts the start and says it builds the image first; it builds nothing itself."""
    for agent in REVIEWED:
        result = host.run(agent, "--dry-run")
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["image"] == f"localhost/{agent}-sandbox:latest"
        assert f"the start builds it first (sandbox-ai build {agent})" in result.stderr
    assert host.builds() == [] and host.images() == {} and host.stopped_at() is None


def test_qwen_image_other_store(host, tmp_path):
    """User: `podman images` as root listed localhost/qwen-sandbox, the user's start said 'not built': root built it
    into /var/lib/containers/storage, the user's start looks in ~/.local/share/containers/storage. The user's start
    builds it into the user's own store (root's untouched); when that build fails, the message names the store it
    looked in and the user."""
    root = Host(tmp_path / "root-side", user="root")
    root_store = root.home / "var-lib-containers-storage"
    root.add_image("localhost/qwen-sandbox:latest", store=root_store)
    before = root.images(root_store)
    failed = host.run("qwen", env={"FAKE_PODMAN_BUILD_FAIL": "1"})
    assert failed.returncode != 0 and host.stopped_at() is None
    assert (f"The image of qwen (localhost/qwen-sandbox:latest) is not built on this host (Podman's image store: "
            f"{host.store}, user {USER}) (nothing is pulled at a start): sandbox-ai build qwen") in failed.stderr
    built = host.run("qwen")
    assert "building it now" in built.stderr and "localhost/qwen-sandbox:latest" in host.images()
    assert root.images(root_store) == before and infra_image(host) == "localhost/qwen-sandbox:latest"


@pytest.mark.skipif(os.geteuid() != 0, reason="root's own build: only as root")
def test_qwen_build_as_root_says_whose_store(host):
    """User: root ran `sandbox-ai build qwen` (and `./install.sh --from .`); the image went into root's store. A build
    as root says so, and that starts by another user do not see it."""
    env = {key: value for key, value in host.env.items() if key != "FAKE_PYTHON"}
    env["PATH"] = f"{host.bin}:/usr/bin:/bin"
    (host.bin / "python3").unlink(missing_ok=True)  # the real python3: euid 0
    result = subprocess.run(host.command("build", "qwen"), env=env, capture_output=True, text=True, timeout=300,
                            stdin=subprocess.DEVNULL, cwd=host.workspace)
    assert result.returncode == 0, result.stderr
    assert "built as root, the image goes into root's Podman store (/var/lib/containers/storage); starts by another " \
           "user do not see it" in result.stderr


def test_podman_exit_125_is_said_as_podman(host):
    """User: 'Podman failed (exit status 125) ...'. Podman failing to answer `image exists` (exit status 125: its
    storage locked, no user session) is said as Podman's failure with its own words - never as 'not built', and
    nothing is built over it."""
    for dry in ([], ["--dry-run"]):
        result = host.run("qwen", *dry, env={"FAKE_PODMAN_EXISTS_STATUS": "125"})
        assert result.returncode != 0
        assert ("Podman could not say whether the image of qwen (localhost/qwen-sandbox:latest) is there (podman image "
                "exists: exit status 125: Error: creating lock") in result.stderr, result.stderr
        assert "not built" not in result.stderr and host.builds() == [] and host.stopped_at() is None


# --- B: "Der Launcher lehnt ab: agent-sandbox: The image of dsh (localhost/dsh-sandbox-tor:latest) is not built on
# this host (nothing is pulled at a start): sandbox-ai build --variant tor" -------------------------------------------

def test_dsh_tor_variant_refused(host):
    """User: 'The image of dsh (localhost/dsh-sandbox-tor:latest) is not built on this host (nothing is pulled at a
    start): sandbox-ai build --variant tor', the variant missing from the app's image list. A variant tor of dsh made
    with the old recipe in the old layout (images/tor) moves to images/variants/dsh/tor, is listed with its agent, the
    dry run accepts it, and the start builds it (base first) and gets to its pod with it."""
    old_tor_variant(host)
    dry = host.run("dsh", "--image-variant", "tor", "--dry-run")
    assert dry.returncode == 0, dry.stderr
    assert "localhost/dsh-sandbox-tor:latest is not built for this user yet: the start builds it first (sandbox-ai " \
           "build --variant tor)" in dry.stderr
    moved = host.home / ".config/sandbox-ai/images/variants/dsh/tor"
    assert (moved / "image.json").is_file() and (moved / "Containerfile.fragment").read_text() == OLD_TOR_FRAGMENT
    assert not (host.home / ".config/sandbox-ai/images/tor").exists()
    listed = json.loads(host.run("images", "--json").stdout)
    assert [(e["target"], e["agent"], e["present"]) for e in listed if e["variant"]] == [("tor", "dsh", False)]
    start = host.run("dsh", "--image-variant", "tor")
    assert "is not built on this host" not in start.stderr, start.stderr
    assert {"localhost/dsh-sandbox:latest", "localhost/dsh-sandbox-tor:latest"} <= set(host.images())
    assert infra_image(host) == "localhost/dsh-sandbox-tor:latest"
    listed = json.loads(host.run("images", "--json").stdout)
    assert [(e["target"], e["agent"], e["present"], e["status"]) for e in listed if e["variant"]] == \
        [("tor", "dsh", True, "current")]


def test_dsh_tor_variant_of_two_agents_is_name_at_agent(host):
    """The same name a variant of two agents (tor of dsh from before, tor of claude made now): each its own image,
    taken as NAME@AGENT by build, listed with its agent; a start of either builds its own."""
    old_tor_variant(host)
    made = host.run("images", "new", "tor", "--from", "claude")
    assert made.returncode == 0, made.stderr
    listed = json.loads(host.run("images", "--json").stdout)
    assert sorted((e["target"], e["agent"]) for e in listed if e["variant"]) == [("tor@claude", "claude"),
                                                                                 ("tor@dsh", "dsh")]
    dry = host.run("dsh", "--image-variant", "tor", "--dry-run")
    assert dry.returncode == 0 and "(sandbox-ai build --variant tor@dsh)" in dry.stderr, dry.stderr
    host.run("dsh", "--image-variant", "tor")
    assert infra_image(host) == "localhost/dsh-sandbox-tor:latest"
    assert "localhost/claude-sandbox-tor:latest" not in host.images()


# --- the user's private presets tor-researcher and tor-research-v2 ---------------------------------------------------

def preset(name):
    path = PRIVATE_PRESETS / f"{name}.json"
    if not path.is_file():
        pytest.skip("SANDBOX_AI_PRIVATE_PRESETS names no folder with the Tor presets")
    return json.loads(path.read_text())


def board_options(document, values):
    """The preset's options as the app's Board makes them for any agent (its {name}s filled in with VALUES, the
    defaults, an optional value left empty taking its option out)."""
    defaults = {name: spec.get("default", "") for name, spec in document["values"].items()}
    filled = {**defaults, **values}
    options, index = [], 0
    raw = document["options"]
    while index < len(raw):
        token = raw[index]
        value = raw[index + 1] if index + 1 < len(raw) and not raw[index + 1].startswith("--") else None
        if value is not None:
            text = value
            for name, given in filled.items():
                text = text.replace("{" + name + "}", str(given))
            if "{" not in text and not text.startswith(":") and text.split(":")[0] != "":
                options += [token, text]
            index += 2
            continue
        options.append(token)
        index += 1
    return options


@pytest.fixture
def tor_host(host):
    """The user's host for the Tor presets: the private preset folder added as the app adds it (presets dirs add),
    a tor VPN profile, the MCP server duckduckgo added (its image present)."""
    preset("tor-researcher")
    assert host.run("presets", "dirs", "add", PRIVATE_PRESETS).returncode == 0
    assert host.run("vpn", "add", "tor-net", "--tor").returncode == 0
    added = host.run("mcp", "add", "mcp:duckduckgo", "--network", "full", "--no-pull")
    assert added.returncode == 0, added.stderr
    image = json.loads((host.home / ".config/sandbox-ai/mcp.json").read_text())["duckduckgo"]["image"]
    host.add_image(image, "localhost/tor-vpn-sandbox:latest", "localhost/tor-sandbox:latest")
    return host


@pytest.mark.parametrize("agent", ["claude", "opencode", "dsh", "qwen"])
def test_tor_researcher_needs_no_variant(tor_host, agent):
    """User (B): the preset tor-researcher set --image-variant {variant} (default tor), and dsh refused to start. It
    needs no variant now: the agent's own image, whatever agent the Board has (claude, opencode, dsh, qwen); the
    check before the start accepts it and says the start builds that image."""
    document = preset("tor-researcher")
    assert "--image-variant" not in document["options"] and "variant" not in document["values"]
    options = board_options(document, {"vpn": "tor-net", "question": "x"})
    if agent in document["agents"]:
        result = tor_host.run(agent, "--preset", "tor-researcher", "--preset-value", "question=x",
                              "--preset-value", "vpn=tor-net", "--dry-run")
    else:
        result = tor_host.run(agent, *options, "--dry-run")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["image"] == f"localhost/{agent}-sandbox:latest"
    assert "sandbox-tor" not in result.stderr + result.stdout


@pytest.mark.parametrize("agent", ["opencode", "claude", "dsh", "qwen"])
def test_tor_research_v2_variant_per_agent(tor_host, agent):
    """tor-research-v2 starts its crawl-tools variant (--image-variant tor-research): for an agent without one the
    check says how to make it (images new tor-research --from AGENT); made for that agent (and opencode, the preset's
    own), the dry run accepts it as tor-research@AGENT and the start builds that variant and gets to its pod."""
    document = preset("tor-research-v2")
    options = board_options(document, {"vpn": "tor-net", "question": "x"})
    # Only what this test is about: the variant (the MCP servers and folders the preset needs are its own concern).
    keep, index = [], 0
    while index < len(options):
        if options[index] in ("--mcp", "--mount", "--llm", "--vpn-bypass-tcp"):
            index += 2
            continue
        keep.append(options[index])
        index += 1
    refused = tor_host.run(agent, *keep, "--dry-run")
    assert refused.returncode != 0
    assert f"sandbox-ai images new tor-research --from {agent}" in refused.stderr, refused.stderr
    for owner in dict.fromkeys(["opencode", agent]):
        assert tor_host.run("images", "new", "tor-research", "--from", owner).returncode == 0
    target = "tor-research@" + agent if agent != "opencode" else "tor-research"
    dry = tor_host.run(agent, *keep, "--dry-run")
    assert dry.returncode == 0, dry.stderr
    if agent == "opencode":
        assert "(sandbox-ai build --variant tor-research" in dry.stderr
    else:
        assert f"(sandbox-ai build --variant {target})" in dry.stderr
    start = tor_host.run(agent, *keep)
    assert f"localhost/{agent}-sandbox-tor-research:latest" in tor_host.images(), start.stderr
    assert f"localhost/{agent}-sandbox-tor-research:latest" in json.dumps(tor_host.calls())


# --- build all -------------------------------------------------------------------------------------------------------

def test_build_all_builds_everything(host):
    """User: `sandbox-ai build all` was to build every image. It builds the seven agents, team, Cairn, browser, VPN,
    Tor and every variant (a name of two agents as NAME@AGENT), each into this user's store."""
    old_tor_variant(host)
    assert host.run("images", "new", "tor", "--from", "claude").returncode == 0
    result = host.run("build", "all", timeout=900)
    built = set(host.images())
    wanted = {f"localhost/{agent}-sandbox:latest" for agent in ("claude", "codex", "opencode", *REVIEWED)} | \
        {"localhost/dsh-sandbox-tor:latest", "localhost/claude-sandbox-tor:latest", "localhost/team-sandbox:latest"}
    assert wanted <= built, (sorted(wanted - built), result.stderr[-3000:])


# --- C: "Session terminal relay not reachable: …/tmux/attach.sock: No such file or directory"; "is running; attach
# with…" (exit 1) -----------------------------------------------------------------------------------------------------

class Relay:
    """A session's terminal relay as the sandbox makes it (tmux/attach.sock): each attach gets GREETING, then EOF."""

    GREETING = b"\x1b[Hhello from the session's terminal\r\n"

    def __init__(self, path):
        self.path = path
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        # (made through a short link to its folder: a test's path is longer than a socket's name may be)
        path.parent.mkdir(exist_ok=True)  # (tmux/relay: the supervisor makes it)
        self.short = Path(tempfile.mkdtemp(prefix="relay-"))
        (self.short / "d").symlink_to(path.parent)
        self.server.bind(str(self.short / "d" / path.name))
        self.server.listen(8)
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        while True:
            try:
                client, _ = self.server.accept()
            except OSError:
                return
            with client, contextlib.suppress(OSError):  # (a look whether it is there: closed at once)
                client.sendall(self.GREETING)

    def close(self):
        self.server.close()
        shutil.rmtree(self.short, ignore_errors=True)


def wait_for(check, timeout=60):
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, "waited in vain"
        time.sleep(0.05)


class Said:
    """What a process says on STREAM, read as it comes (in a thread of its own): text() so far, all of it once the
    process ended."""

    def __init__(self, stream):
        self.lines = []
        self.thread = threading.Thread(target=self.read, args=(stream,), daemon=True)
        self.thread.start()

    def read(self, stream):
        for line in iter(stream.readline, ""):
            self.lines.append(line)

    def text(self, ended=False):
        if ended:  # (the process ended: up to the stream's end)
            self.thread.join(timeout=30)
        return "".join(self.lines)


@pytest.fixture
def starting(host):
    """The session q1 of qwen started (in the background, as the app's terminal tab starts it) and held at its pod
    (still starting: no terminal yet); its relay comes once the test says so; at the end the start goes on (fails at
    its pod) and is waited for."""
    host.add_image("localhost/qwen-sandbox:latest")
    hold = host.directory / "release-pod"
    env = {"FAKE_PODMAN_POD_HOLD": str(hold)}
    first = host.popen("qwen", "--session", "q1", env=env)
    wait_for(lambda: host.stopped_at() is not None)
    # (a start of 0.1.117: its relays lie in tmux/relay, where the supervisor binds them - sessions.relay_socket)
    socket_path = host.home / ".local/state/sandbox-ai/sessions/q1/tmux/relay/attach.sock"
    relay = []
    yield host, socket_path, env, (lambda: relay.append(Relay(socket_path)))
    hold.touch()
    for item in relay:
        item.close()
    try:
        first.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        first.kill()
        first.communicate()


def test_attach_while_starting(starting):
    """User: 'Session terminal relay not reachable: …/tmux/attach.sock: No such file or directory' - the app said the
    session runs, and attaching failed. Attaching to a session still starting waits for its terminal (said once) and
    attaches when it is there."""
    host, socket_path, env, relay_up = starting
    assert not socket_path.exists()
    listed = json.loads(host.run("list", "--json").stdout)
    session = next(entry for entry in listed if entry.get("name") == "q1")
    assert session.get("terminal") == "starting" or session.get("state") == "starting" or \
        "starting" in json.dumps(session), session
    attach = host.popen("attach", "q1", env=env)
    out, err = Said(attach.stdout), Said(attach.stderr)
    # The relay comes once attach said it waits: on a loaded machine its start took longer than a fixed second, the
    # relay was there before attach looked, and it attached without waiting (~1 in 10 runs on two busy CPUs).
    wait_for(lambda: "still starting; waiting for its terminal" in err.text() or attach.poll() is not None)
    time.sleep(1.0)
    assert attach.poll() is None, (out.text(), err.text())  # waiting, not 'relay not reachable'
    relay_up()
    attach.wait(timeout=60)
    out, err = out.text(ended=True), err.text(ended=True)
    assert "still starting; waiting for its terminal" in err
    assert "relay not reachable" not in err
    assert "hello from the session's terminal" in out, (out, err)
    assert attach.returncode == 0, err


def test_second_start_attaches(starting):
    """User: the app started a running session a second time: 'is running; attach with…', exit status 1. Started
    again on a terminal (the app's tab is one), it attaches to the session instead; a script without a terminal still
    gets the message."""
    host, socket_path, env, relay_up = starting
    relay_up()
    script = host.run("qwen", "--session", "q1", env=env)
    assert script.returncode == 1 and "is running; attach with: sandbox-ai attach q1" in script.stderr
    controller, terminal = pty.openpty()
    second = subprocess.Popen(host.command("qwen", "--session", "q1"), cwd=host.workspace, env={**host.env, **env},
                              stdin=terminal, stdout=terminal, stderr=terminal, close_fds=True)
    os.close(terminal)
    said = b""
    deadline = time.monotonic() + 60
    while second.poll() is None and time.monotonic() < deadline:
        ready, _, _ = select.select([controller], [], [], 0.2)
        if ready:
            try:
                said += os.read(controller, 65536)
            except OSError:
                break
    second.wait(timeout=30)
    with_output = said.decode(errors="replace")
    os.close(controller)
    assert "runs already: attaching to it" in with_output, with_output
    assert "hello from the session's terminal" in with_output and second.returncode == 0


# --- D: "exiftool: needs --mount /work=HOST" when the Board added the MCP server ------------------------------------

def test_exiftool_needs_folder(host):
    """User: the Board's adding exiftool ended in 'needs --mount /work=HOST'. The catalog says which folders each
    server must have (mcp catalog --json: mounts, required); since 0.1.214 (the user: "alle mcp server sollten bzgl.
    /work pfad defaults haben") one added without it gets the default - /work the start's workspace (@workspace) -,
    and a folder given wins."""
    catalog = json.loads(host.run("mcp", "catalog", "--json").stdout)
    entry = next(e for e in catalog["entries"] if e["name"] == "exiftool")
    assert [m["target"] for m in entry["mounts"] if m.get("required")] == ["/work"]
    defaulted = host.run("mcp", "add", "hub:exiftool", "--no-pull")
    assert defaulted.returncode == 0, defaulted.stderr
    mounts = json.loads((host.home / ".config/sandbox-ai/mcp.json").read_text())["exiftool"]["mounts"]
    assert any(m.get("target") == "/work" and m.get("host") == "@workspace" for m in mounts), mounts
    host.run("mcp", "remove", "exiftool")
    folder = host.directory / "case-downloads"
    folder.mkdir()
    added = host.run("mcp", "add", "hub:exiftool", "--no-pull", "--mount", f"/work={folder}:ro")
    assert added.returncode == 0, added.stderr
    mounts = json.loads((host.home / ".config/sandbox-ai/mcp.json").read_text())["exiftool"]["mounts"]
    assert any(m.get("target") == "/work" and m.get("host") == str(folder) for m in mounts), mounts


def test_mcp_add_pull_failure_is_a_message(host):
    """The Board's 'add the MCP servers' (sandbox-ai mcp add in its Preparing tab): a pull that fails (no network, a
    registry that refuses) ends with what failed and why, not with a Python traceback."""
    result = host.run("mcp", "add", "mcp:duckduckgo", "--network", "full")
    assert result.returncode != 0
    assert "Traceback" not in result.stderr, result.stderr
    assert "duckduckgo is not added" in result.stderr and "exit status 125" in result.stderr
    assert not (host.home / ".config/sandbox-ai/mcp.json").exists() or \
        "duckduckgo" not in json.loads((host.home / ".config/sandbox-ai/mcp.json").read_text())


# --- C, a start that takes minutes ------------------------------------------------------------------------------

def load_sessions():
    import importlib.util
    spec = importlib.util.spec_from_file_location("sandbox_sessions_live_regressions", ROOT / "sandbox/sessions.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SlowStart:
    """A session's start that takes minutes (a Tor VPN bootstrapping, nine MCP servers behind Tor): every look a
    minute later, its supervisor saying a new step at each (until QUIET_AFTER), its terminal relay up after LAST s."""

    def __init__(self, directory, last=600, quiet_after=None):
        self.directory, self.now, self.last, self.quiet_after = directory, 0.0, last, quiet_after
        (directory / "tmux").mkdir(parents=True, exist_ok=True)
        (directory / "supervisor.log").write_text("agent-sandbox: starting the VPN tor-net…\n")

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += 60
        if self.quiet_after is None or self.now < self.quiet_after:
            with open(self.directory / "supervisor.log", "a") as log:
                log.write(f"agent-sandbox: starting the MCP server s{int(self.now)} behind Tor…\n")
        if self.now >= self.last:
            (self.directory / "tmux" / "attach.sock").touch()


def test_attach_while_starting_slowly(tmp_path, monkeypatch):
    """User: 'Session terminal relay not reachable: …/tmux/attach.sock: No such file or directory'. A start through
    Tor (the user's tor presets: the VPN's bootstrap and check, MCP servers each behind a Tor of its own) takes
    longer than two minutes: attaching waits as long as the start goes on (its supervisor says new steps), and gives
    up only once it has said nothing for a while."""
    sessions = load_sessions()
    monkeypatch.setattr(sessions, "session_dir", lambda name: tmp_path)
    monkeypatch.setattr(sessions, "alive", lambda name, data: True)
    slow = SlowStart(tmp_path, last=600)
    sessions.wait_for_terminal("q1", {}, tmp_path / "tmux" / "attach.sock", "q1", clock=slow.clock, sleep=slow.sleep)
    assert slow.now >= 600
    stuck = SlowStart(tmp_path / "stuck", last=10 ** 6, quiet_after=180)
    monkeypatch.setattr(sessions, "session_dir", lambda name: tmp_path / "stuck")
    with pytest.raises(RuntimeError, match=r"has no terminal after \d+ s"):
        sessions.wait_for_terminal("q1", {}, tmp_path / "stuck" / "tmux" / "attach.sock", "q1", clock=stuck.clock,
                                   sleep=stuck.sleep)
    assert 180 <= stuck.now <= 180 + sessions.TERMINAL_PATIENCE + 120


def test_start_while_starting_slowly(tmp_path, monkeypatch):
    """User: the app's session start said it failed while the session went on starting. The terminal a session was
    started in waits as long as its start goes on (its supervisor says new steps) - not two minutes at most - and
    gives up once it has said nothing for a while."""
    sessions = load_sessions()
    slow = SlowStart(tmp_path, last=600)
    # wait_ready's own SLEEP: time.sleep patched for the whole process moved the test's clock with other threads'
    # pauses too (CI: 2,343,420 s)
    process = type("Supervisor", (), {"poll": lambda self: None, "returncode": None})()
    sessions.wait_ready(tmp_path, process, follow=0, out=lambda text: None, clock=slow.clock, bar=False,
                        sleep=slow.sleep)
    assert slow.now >= 600
    stuck = SlowStart(tmp_path / "stuck", last=10 ** 6, quiet_after=180)
    with pytest.raises(RuntimeError, match="did not become ready"):
        sessions.wait_ready(tmp_path / "stuck", process, follow=0, out=lambda text: None, clock=stuck.clock,
                            bar=False, sleep=stuck.sleep)
    assert 180 <= stuck.now <= 180 + sessions.TERMINAL_PATIENCE + 120


# --- installed by root (./install.sh --from . as root), used by a user ------------------------------------------------

def test_install_as_root_app_as_user(tmp_path):
    """User: installed system-wide with `./install.sh --from .` as root, the app started as the user. Whatever root's
    umask (077 on hardened systems, a root shell's own), every folder of the installation is open to every user and
    every file readable: the user's sandbox-ai, its builds (the Containerfiles are the build's context) and the app
    run from it."""
    home, prefix = tmp_path / "root-home", tmp_path / "prefix"
    home.mkdir()
    prefix.mkdir(mode=0o755)  # (as /usr/local is)
    prefix.chmod(0o755)
    env = {key: value for key, value in os.environ.items() if not key.startswith(("XDG_", "SANDBOX_AI_"))}
    env.update(HOME=str(home), PATH="/usr/bin:/bin")
    result = subprocess.run(["bash", "-c", 'umask 077 && exec bash "$0" "$@"', str(ROOT / "install.sh"), "--from",
                             str(ROOT), "--system", "--prefix", str(prefix), "--no-app-deps", "--no-app-terminal",
                             "--no-modify-rc"], capture_output=True, text=True, env=env, timeout=300)
    assert result.returncode == 0, result.stderr
    closed = [str(path.relative_to(prefix)) for path in [prefix, *prefix.rglob("*")] if not path.is_symlink() and
              (path.stat().st_mode & 0o005 != 0o005 if path.is_dir() else path.stat().st_mode & 0o004 == 0)]
    assert closed == [], closed[:20]
    data = prefix / "lib/sandbox-ai/current"
    assert (data / "sandbox/Containerfile.qwen").stat().st_mode & 0o004
    assert all(path.stat().st_mode & 0o002 == 0 for path in data.rglob("*") if not path.is_symlink()), \
        "nothing of it writable by others"


# --- "--image-variant tor is a variant of opencode, not of qwen; make one of qwen too" ------------------------------

def old_tor_variant_of(host, agent):
    folder = old_tor_variant(host)
    document = json.loads((folder / "image.json").read_text())
    document["agent"] = agent
    (folder / "image.json").write_text(json.dumps(document) + "\n")
    return folder


def test_qwen_tor_variant_of_opencode_is_not_needed(host):
    """User (2026-11-14): "Der Launcher lehnt ab: agent-sandbox: the image localhost/qwen-sandbox-tor:latest is not
    built for this user yet: the start builds it first (sandbox-ai build --variant tor@qwen) / agent-sandbox:
    --image-variant tor is a variant of opencode, not of qwen; make one of qwen too: sandbox-ai images new tor --from
    qwen" - a preset made with opencode, then switched to qwen. Tor is the VPN: an agent without a variant tor of its
    own starts its own image, with a note, and nothing says the start builds a tor variant."""
    old_tor_variant_of(host, "opencode")
    dry = host.run("qwen", "--image-variant", "tor", "--dry-run")
    assert dry.returncode == 0, dry.stderr
    assert "is a variant of opencode, not of qwen" not in dry.stderr and "build --variant tor" not in dry.stderr
    assert "--image-variant tor: no variant tor of qwen" in dry.stderr and "Tor" in dry.stderr
    start = host.run("qwen", "--image-variant", "tor")
    assert infra_image(host) == "localhost/qwen-sandbox:latest", start.stderr


def test_a_variant_of_another_agent_is_made_for_this_one(host):
    """The same report for a variant that is more than Tor (a preset's crawl tools, made for opencode): a start of
    qwen with it makes qwen's own copy of that variant (same packages and files) and builds it - no refusal."""
    folder = host.home / ".config/sandbox-ai/images/variants/opencode/crawl"
    folder.mkdir(mode=0o700, parents=True)
    (folder / "image.json").write_text(json.dumps({"version": 1, "agent": "opencode", "apt": ["httrack"], "npm": [],
                                                   "pip": [], "files": []}) + "\n")
    dry = host.run("qwen", "--image-variant", "crawl", "--dry-run")
    assert dry.returncode == 0, dry.stderr
    assert "made the variant crawl of qwen from opencode's" in dry.stderr
    copy = json.loads((host.home / ".config/sandbox-ai/images/variants/qwen/crawl/image.json").read_text())
    assert copy["agent"] == "qwen" and copy["apt"] == ["httrack"]
    host.run("qwen", "--image-variant", "crawl")
    assert infra_image(host) == "localhost/qwen-sandbox-crawl:latest"


def test_tests_reach_no_real_host_over_ssh():
    """User (2026-11-14), seeing this sandbox's own test runs: "claude-e7d6edae: 7 Fragen zu 192.0.2.11:22 in der
    letzten Minute ohne Antwort abgelehnt" - a test that did not fake ssh tried a made-up host for real. Every test
    run's PATH starts with stubs that fail at once."""
    result = subprocess.run(["ssh", "-o", "BatchMode=yes", "root@192.0.2.11", "true"], capture_output=True,
                            text=True, timeout=10)
    assert result.returncode == 255 and "tests reach no real host" in result.stderr


# --- "dieser 60s timeout fuer freigaben nervt total, weil man es auch nicht rueckgaengig machen kann" (2026-11-17):
# a question nobody answered in time was refused, the same request then refused at once for 2 minutes ------------

def test_a_question_that_timed_out_is_still_answered_through_sandbox_ai(tmp_path):
    """The supervisor marks the question timed out (its .state) and keeps it open: ./sandbox-ai lists it under
    --state timeout and answers it (the supervisor applies that answer to the request's next try; the broker side:
    tests/test_approvals_rework.py, end to end with a real broker and control socket there too)."""
    short = Path(tempfile.mkdtemp(prefix="reg-", dir="/tmp"))
    try:
        host = Host(short / "h")
        runs = host.runtime / "sandbox-ai" / "runs" / "claude-1"
        (runs / "approvals").mkdir(parents=True)
        (runs / "run.json").write_text(json.dumps({"run": "claude-1", "agent": "claude", "pid": os.getpid(),
                                                   "workspace": str(host.workspace), "session_dir": str(short),
                                                   "started": "2026-11-17T04:00:00Z", "ask_timeout": 60}))
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 120))
        (runs / "approvals" / "1763352000-1-1.json").write_text(json.dumps({
            "id": "1763352000-1-1", "kind": "connect", "time": stamp, "timeout": 60, "destination": "192.0.2.7:443",
            "address": "192.0.2.7", "port": 443}))
        (runs / "approvals" / "1763352000-1-1.state").write_text(json.dumps({"state": "timeout", "time": stamp}))
        listed = host.run("approvals", "list", "--state", "timeout", "--json", timeout=60)
        assert listed.returncode == 0, listed.stderr
        [entry] = json.loads(listed.stdout)
        assert entry["state"] == "timeout" and entry["answerable"] and "allow" in entry["actions"]
        plain = host.run("approvals", "list", timeout=60)
        assert "1 more nobody answered in time, still answerable" in plain.stdout
        allowed = host.run("approvals", "allow", "1763352000-1-1", "--scope", "run", timeout=60)
        assert allowed.returncode == 0, allowed.stderr
        assert json.loads((runs / "approvals" / "1763352000-1-1.answer").read_text()) == {"decision": "allow",
                                                                                         "scope": "run"}
    finally:
        shutil.rmtree(short, ignore_errors=True)


# --- "agent-sandbox: Session vital-wallaby-azalea is running; attach with: sandbox-ai attach vital-wallaby-azalea /
# [Exit-Status 1]" - "the session/terminal is not opened", "the session is not started at all it seems" (2026-11-19) ---

def on_terminal(host, *args, env=None, timeout=90, seen=None):
    """`sandbox-ai ARGS` on a pseudo-terminal, as the app's tab or `ssh -tt` runs it: (exit status, what it said).
    SEEN(text): called with all it said so far whenever more came (to let a held start go on)."""
    controller, terminal = pty.openpty()
    process = subprocess.Popen(host.command(*args), cwd=host.workspace, env={**host.env, **(env or {})},
                               stdin=terminal, stdout=terminal, stderr=terminal, close_fds=True)
    os.close(terminal)
    said, deadline = b"", time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready, _, _ = select.select([controller], [], [], 0.2)
        if ready:
            try:
                more = os.read(controller, 65536)
            except OSError:
                break
            if not more:
                break
            said += more
            if seen is not None:
                seen(said.decode(errors="replace"))
        elif process.poll() is not None:
            break
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    os.close(controller)
    return process.returncode, said.decode(errors="replace").replace("\r\n", "\n")


def one_line(text):
    """TEXT with its line ends as blanks (a terminal 80 columns wide wraps nothing here, the launcher's lines do)."""
    return " ".join(text.split())


def test_start_names_the_session_of_another_agent(starting):
    """User: 'agent-sandbox: Session vital-wallaby-azalea is running; attach with: sandbox-ai attach
    vital-wallaby-azalea / [Exit-Status 1]' - 'the session is not started at all it seems'. The Board had kept its
    session name, and the second Start (another agent) met the session of the first. The launcher's refusal names
    what it saw - whose session it is, that its supervisor runs, how far its terminal is - and what helps: attach,
    stop it, or another name."""
    host, socket_path, env, relay_up = starting
    host.add_image("localhost/claude-sandbox:latest")
    # While its first start still waits for its terminal (it holds the session's lock): the same, and that so.
    status, said = on_terminal(host, "claude", "--session", "q1", env=env)
    said = one_line(said)
    assert status == 1, said
    assert "Session q1 is running with qwen, not claude" in said, said
    assert "its supervisor (PID " in said and "still starting" in said, said
    assert "Another command is starting or stopping it right now" in said, said
    relay_up()
    deadline = time.monotonic() + 60
    while True:
        status, said = on_terminal(host, "claude", "--session", "q1", env=env)
        said = one_line(said)
        if "Another command is starting" not in said or time.monotonic() > deadline:
            break
        time.sleep(0.2)
    assert status == 1, said
    assert "Session q1 is running with qwen, not claude" in said, said
    assert "attach with: sandbox-ai attach q1" in said and "sandbox-ai stop q1" in said, said
    assert "another name (--session NAME)" in said, said
    assert "its supervisor (PID " in said and "its terminal is there" in said, said
    assert "runs already: attaching" not in said
    # (a script too: the same words)
    script = host.run("claude", "--session", "q1", env=env)
    assert script.returncode == 1 and "is running with qwen, not claude" in one_line(script.stderr)


def test_session_status_says_what_it_sees(starting):
    """`sandbox-ai session status NAME --json`: what a start of that name would meet - its agent, state, whether its
    supervisor runs, how far its terminal is, whether its relay answers - for the app's check before a start."""
    host, socket_path, env, relay_up = starting
    found = json.loads(host.run("session", "status", "q1", "--json").stdout)
    assert (found["name"], found["agent"], found["state"], found["foreground"]) == ("q1", "qwen", "running", False)
    assert found["terminal"] == "starting" and found["relay"] is False and found["attachable"] is True
    assert found["supervisor"]["alive"] is True and isinstance(found["supervisor"]["pid"], int)
    assert found["login"] is False and "still starting" in found["seen"]
    relay_up()
    found = json.loads(host.run("session", "status", "q1", "--json").stdout)
    assert found["terminal"] == "ready" and found["relay"] is True and found["attach_why"] is None
    words = host.run("session", "status", "q1")
    assert words.returncode == 0 and "q1: running" in words.stdout and "qwen" in words.stdout
    missing = host.run("session", "status", "nothing-here", "--json")
    assert missing.returncode == 1 and "No session named nothing-here" in missing.stderr


def test_start_names_a_session_in_the_foreground(host):
    """A start of that name runs in the foreground of another terminal (a task, --no-tmux): there is no terminal to
    attach to, and the refusal says so instead of 'attach with: sandbox-ai attach NAME' (which then refused too)."""
    host.add_image("localhost/qwen-sandbox:latest")
    hold = host.directory / "release-pod"
    env = {"FAKE_PODMAN_POD_HOLD": str(hold)}
    first = host.popen("qwen", "--name", "t1", "--no-tmux", env=env)
    try:
        wait_for(lambda: host.stopped_at() is not None)
        status, said = on_terminal(host, "qwen", "--session", "t1", env=env)
        said = one_line(said)
        assert status == 1, said
        assert "Session t1 is running in the foreground of another terminal or script" in said, said
        assert "there is no terminal to attach to" in said and "sandbox-ai stop t1" in said, said
        assert "attach with: sandbox-ai attach t1" not in said
        found = json.loads(host.run("session", "status", "t1", "--json").stdout)
        assert found["foreground"] is True and found["attachable"] is False and found["attach_why"] == "foreground"
    finally:
        hold.touch()
        try:
            first.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            first.kill()
            first.communicate()


def test_start_of_a_session_without_an_agent_starts_it_again(starting):
    """A session that the list calls running while it has no agent any more - its tmux session ended, its supervisor
    still runs - was 'attached' by a second start and ended in '... has no agent any more'. A start of that name
    (the same agent) says what it saw, stops it and starts it again."""
    host, socket_path, env, relay_up = starting
    relay_up()
    ended = socket_path.parent.parent / "ended"
    ended.write_text(json.dumps({"status": 0, "seconds": 5}))
    hold = host.directory / "release-pod"

    def seen(text):
        if "resuming stopped session q1" in text:
            hold.touch()  # (its new supervisor gets as far as its pod, where the fake ends every start)
    status, said = on_terminal(host, "qwen", "--session", "q1", env=env, seen=seen, timeout=120)
    said = one_line(said)
    assert "session q1 runs without an agent" in said and "its agent exited with status 0 after 5 s" in said, said
    assert "it is stopped and started again" in said, said
    assert "resuming stopped session q1 with its saved options" in said, said
    assert "runs already: attaching" not in said and "has no agent any more" not in said, said


def test_second_start_while_the_first_still_starts_attaches(starting):
    """A second Start of the same session while the first still waits for its terminal (its image, a VPN): 'Session
    NAME is being started or stopped by another command; try again in a moment', exit status 1. On a terminal it
    attaches - waiting for the session's terminal, as attach does - instead."""
    host, socket_path, env, relay_up = starting

    def seen(text):
        if "still starting; waiting for its terminal" in text and not socket_path.exists():
            relay_up()
    status, said = on_terminal(host, "qwen", "--session", "q1", env=env, seen=seen)
    said = one_line(said)
    assert "session q1 is being started by another command: attaching to it" in said, said
    assert "try again in a moment" not in said, said
    assert "hello from the session's terminal" in said and status == 0, said
