"""What is added to a sandbox really reaches its agent - one scenario per kind of resource, for claude, codex and
opencode on Podman and Bubblewrap (the feature matrix's stand-in host: a whole start through main() with the backend
recorded, and the dry-run plan), and through the real ./sandbox-ai with a fake Podman.

User (todos.txt, 2026-11-24, board): "sicherstellen das hinzugefügte toolchains, ordner, container, tools auch
wirklich vom agenten erreichbar und nutzbar sind, falls dafuer z.b. bestimmte netzwerk-regeln notwendig sind, sollten
diese automatisch gesetzt werden".

Per kind: the plan names it, the backend's command line mounts or passes it with the chosen mode, the agent's own
configuration has it (PATH, MCP configuration, SANDBOX_AI_SERVICES), the network rule it needs is set by the start
itself (a toolchain's registries: allowed hosts in the firewall and the proxy) - not in the security modes high and
paranoid, which mount the toolchain and keep its registries closed (said: needs X, refused by the mode) -, and the
agent's intro (/sandbox/session/INTRO.md) lists it with where it is and how to use it. (The suite's starts go
without the intro - conftest.py's SANDBOX_AI_INTRO=0 -: each scenario here asks for it with --intro, the end-to-end
test takes the variable out of its host's environment.)
"""
import json
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_fakes import Host  # noqa: E402
import test_feature_matrix as fm  # noqa: E402
from test_feature_matrix import fake_dev, m  # noqa: E402,F401  (fixtures)
import toolchain_fakes as TF  # noqa: E402

s = fm.s
GO_HOSTS = ["proxy.golang.org", "sum.golang.org"]


def intro(run):
    """INTRO.md as the start wrote it into the session directory (/sandbox/session/INTRO.md in the sandbox)."""
    return run.files["INTRO.md"]


def path_of_the_agent(run):
    """The PATH the agent's processes get: the start's (config.json) as the sandbox's supervisor hands it on - with
    the toolchains' wrappers first where /opt/toolchains is there (runtime.toolchain_env)."""
    return s.RUNTIME.toolchain_env(dict(run.config["env"]), there=lambda path: True)["PATH"].split(":")


def mounted(m, run, source, dest, mode):
    """SOURCE is mounted at DEST with MODE (ro, rw) on the backend's command line."""
    if m.backend == "podman":
        bind = next((arg for arg in map(str, run.command) if f"src={source},dst={dest}" in arg), None)
        return bind is not None and ((",ro" in bind.split(f"dst={dest}", 1)[1]) == (mode == "ro"))
    return f"--{'ro-' if mode == 'ro' else ''}bind {source} {dest}" in run.joined


# --- folders: workspaces and mounts, with the chosen mode -------------------------------------------------------------

def test_folders_are_mounted_with_the_chosen_mode_and_listed(m):
    lib, data, scratch = (m.tmp / name for name in ("lib", "data", "scratch"))
    for folder in (lib, data, scratch):
        folder.mkdir()
    options = ("--workspace", str(lib), "--mount", f"{data}:/mnt/data:ro", "--mount",
               f"{scratch}:/mnt/scratch:rw", "--intro")  # (a second project folder beside the matrix's /work)
    plan = m.plan(*options)
    assert plan["workspaces"] == [{"host": str(m.work), "sandbox": "/work"},
                                  {"host": str(lib), "sandbox": "/workspaces/lib"}]
    assert plan["workspace_mode"] == "rw"
    assert plan["mounts"] == [f"{data}:/mnt/data:ro", f"{scratch}:/mnt/scratch:rw"]
    run = m.run(*options)
    assert mounted(m, run, m.work, "/work", "rw") and mounted(m, run, lib, "/workspaces/lib", "rw")
    assert mounted(m, run, data, "/mnt/data", "ro") and mounted(m, run, scratch, "/mnt/scratch", "rw")
    text = intro(run)
    assert "- `/work` - the project (you start here), read-write" in text
    assert "- `/workspaces/lib` - a further project folder, read-write" in text
    assert "- `/mnt/data` - an extra folder or file, read-only" in text
    assert "- `/mnt/scratch` - an extra folder or file, read-write" in text
    assert str(m.tmp) not in text, "sandbox paths only"
    readonly = m.run(*options, "--workspace-ro")
    assert mounted(m, readonly, m.work, "/work", "ro") and mounted(m, readonly, lib, "/workspaces/lib", "ro")
    assert "- `/work` - the project (you start here), read-only" in intro(readonly)


# --- a toolchain: mounted read-only, on PATH, its registries opened by the start ----------------------------------------

@pytest.fixture
def go(m):
    TF.install(m.tmp / "toolchain-catalog", ["go"])
    yield
    TF.writable(m.tmp)


def test_a_toolchain_is_on_path_and_its_registries_are_opened_by_the_start(m, go):
    options = ("--toolchain", "go", "--intro")
    plan = m.plan(*options)
    tool = plan["toolchains"][0]
    assert (tool["name"], tool["sandbox"], tool["mode"]) == ("go", "/opt/toolchains/go", "ro")
    assert tool["commands"] == ["go", "gofmt"]
    assert plan["toolchain_hosts"] == {"opened": [*GO_HOSTS, "--allow-github"], "closed": []}
    assert set(GO_HOSTS) <= set(plan["https_hosts"]) and "github.com" in plan["https_hosts"], \
        "the network rule it needs: set by the start itself"
    run = m.run(*options)
    store = Path(os.environ["HOME"]) / ".local/share/sandbox-ai/toolchains"
    tree = next(path for path in store.iterdir() if (path / "bin" / "go").exists())
    assert mounted(m, run, tree, "/opt/toolchains/go", "ro")
    # /opt/toolchains itself: the session's folder with one wrapper per command, read-only; first on the agent's PATH
    assert ("dst=/opt/toolchains,ro" if m.backend == "podman" else " /opt/toolchains ") in run.joined
    assert path_of_the_agent(run)[0] == "/opt/toolchains/bin", "its commands first on the agent's PATH"
    for host in GO_HOSTS:  # reachable: resolved for the sandbox and let through its firewall
        assert f"{fm.address(host)} {host}" in run.files["hosts"]
        assert f"ip daddr {fm.address(host)} tcp dport 443 accept" in run.firewall
    assert ("- go 1.27.3 at `/opt/toolchains/go`: go, gofmt. Registries open: proxy.golang.org, sum.golang.org, the "
            "preset github.") in intro(run)


@pytest.mark.parametrize("mode", ["high", "paranoid"])
def test_a_strict_mode_mounts_the_toolchain_and_refuses_its_registries(m, go, mode):
    """needs X, refused by mode high: mounted and on PATH all the same, nothing opened, and the agent is told."""
    options = ("--toolchain", "go", "--mode", mode, "--intro")
    plan = m.plan(*options)
    assert plan["mode"]["name"] == mode and plan["toolchains"][0]["sandbox"] == "/opt/toolchains/go"
    assert plan["toolchain_hosts"] == {"opened": [], "closed": [*GO_HOSTS, "--allow-github"]}
    assert not set(GO_HOSTS) & set(plan["https_hosts"]) and "github.com" not in plan["https_hosts"]
    run = m.run(*options)
    assert path_of_the_agent(run)[0] == "/opt/toolchains/bin"
    assert not any(f"ip daddr {fm.address(host)} " in run.firewall for host in GO_HOSTS)
    assert f"Registries closed (the security mode {mode}): proxy.golang.org, sum.golang.org, the preset github" \
        in intro(run)
    assert f"security mode {mode}: the registries of go stay closed" in run.err


def test_the_part_can_keep_the_registries_closed(m, go):
    plan = m.plan("--toolchain", "go", "--toolchain-offline")
    assert plan["toolchain_hosts"]["opened"] == [] and plan["toolchain_hosts"]["closed"][:2] == GO_HOSTS
    assert not set(GO_HOSTS) & set(plan["https_hosts"])
    run = m.run("--toolchain", "go", "--toolchain-offline", "--intro")
    assert "Registries closed (started with --toolchain-offline)" in intro(run)


# --- an MCP server: in the agent's own MCP configuration ----------------------------------------------------------------

def test_an_mcp_server_is_in_the_agents_mcp_configuration_and_listed(m):
    plan = m.plan("--mcp", "demo", "--intro")
    assert [entry["name"] for entry in plan["mcp"]] == ["demo"]
    run = m.run("--mcp", "demo", "--intro")
    assert m.mcp_started == ["demo"], "started for the run"
    assert (f"src={s.mcp_socket_dir('demo')},dst={s.MCP_UPSTREAM_DIR}/demo" if m.backend == "podman"
            else f"--ro-bind {s.mcp_socket_dir('demo')} {s.MCP_UPSTREAM_DIR}/demo") in run.joined
    command = run.config["command"]
    if m.agent == "claude":
        assert "demo" in json.loads(command[command.index("--mcp-config") + 1])["mcpServers"]
    elif m.agent == "codex":
        assert 'mcp_servers.demo.command="python3"' in command
    else:
        assert "demo" in json.loads(run.env["OPENCODE_CONFIG_CONTENT"])["mcp"]
    assert run.run_record["mcp_servers"] == ["demo"] and run.run_record["intro"] is True
    assert "## MCP servers" in intro(run) and "\n- demo\n" in intro(run)


# --- a service: a container beside the agent, reached at localhost:PORT -------------------------------------------------

def test_a_service_is_reachable_at_its_port_and_listed(m):
    path = Path(os.environ["HOME"]) / ".config" / "sandbox-ai" / "user-images.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "images": {"redis": {
        "kind": "pulled", "image": "docker.io/library/redis@sha256:" + "2" * 64, "id": "sha256:" + "2" * 64,
        "user": "999:1000"}}}))
    options = ("--service", "cache:image=redis,port=6379", "--intro")
    if m.backend != "podman":
        with pytest.raises(ValueError, match="--service"):  # a container beside the agent: Podman's pod
            m.plan(*options)
        return
    plan = m.plan(*options)  # (the dry run: the stand-in host has no Podman to ask for the image)
    assert plan["services"][0]["name"] == "cache" and plan["services"][0]["reachable_at"] == ["localhost:6379"]
    assert "the sandbox's own" in plan["services"][0]["network"], "no rule of its own to set: the agent's network"
    assert "## Services" in plan["intro"]["text"] and "- cache: localhost:6379" in plan["intro"]["text"]
    a = s.parse_args(m.argv(*options))
    a.services = [{"name": "cache", "ports": [6379]}]
    assert s.agent_environment(a)["SANDBOX_AI_SERVICES"] == "cache=localhost:6379"


# --- a device: passed to the sandbox -------------------------------------------------------------------------------------

def test_a_device_is_present_in_the_sandbox_and_listed(m, fake_dev):
    node = str(fake_dev / "ttyACM0")
    plan = m.plan("--device", node, "--intro")
    assert plan["devices"] == [{"path": node, "kind": "node"}]
    run = m.run("--device", node, "--intro")
    assert (f"--device {node}" if m.backend == "podman" else f"--dev-bind {node}") in run.joined
    assert "## Devices" in intro(run) and f"- `{node}`" in intro(run)


# --- everything at once, and nothing of it without the intro's own tests failing -----------------------------------------

def test_the_plan_names_every_kind_at_once(m, go, fake_dev):
    data = m.tmp / "data"
    data.mkdir()
    node = str(fake_dev / "ttyACM0")
    plan = m.plan("--mount", f"{data}:/mnt/data:ro", "--toolchain", "go", "--mcp", "demo", "--device", node,
                  "--intro")
    text = plan["intro"]["text"]
    for heading in ("## Folders", "## Toolchains", "## MCP servers", "## Devices", "## Network"):
        assert heading in text, heading
    assert plan["intro"]["agent_reads_it"] and str(m.tmp / "data") not in text


# --- end to end: the real ./sandbox-ai ------------------------------------------------------------------------------------

@pytest.fixture
def host(tmp_path):
    made = Host(tmp_path / "host")
    made.env.pop(s.INTRO_VARIABLE, None)  # (a user's environment: the suite's own starts go without the intro)
    yield made
    TF.writable(tmp_path)


def plan_of(done):
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout[done.stdout.index("{"):])


def test_e2e_a_start_with_a_toolchain_a_mount_and_an_mcp_server(host, tmp_path):
    """Through ./sandbox-ai: `toolchain add` from a catalog, then a start (a fake Podman) - what its pod is given."""
    catalog = TF.catalog(tmp_path / "catalog", ["go"])
    added = host.run("toolchain", "add", "go", "--catalog", catalog, "--unsigned")
    assert added.returncode == 0, added.stderr
    data = tmp_path / "data"
    data.mkdir()
    options = ("--toolchain", "go", "--mount", f"{data}:/mnt/data:ro", "--mcp", "demo")
    host.add_image("localhost/claude-sandbox:latest", "localhost/codex-sandbox:latest")  # (demo runs in codex's)
    plan = plan_of(host.run("claude", *options, "--dry-run"))
    assert plan["toolchains"][0]["sandbox"] == "/opt/toolchains/go" and plan["mounts"] == [f"{data}:/mnt/data:ro"]
    assert plan["toolchain_hosts"]["opened"] == [*GO_HOSTS, "--allow-github"] and plan["mcp"][0]["name"] == "demo"
    assert set(GO_HOSTS) <= set(plan["https_hosts"])
    assert "- go 1.27.3 at `/opt/toolchains/go`" in plan["intro"]["text"] and "\n- demo\n" in plan["intro"]["text"]
    strict = plan_of(host.run("claude", *options, "--mode", "high", "--dry-run"))
    assert strict["toolchain_hosts"]["opened"] == [] and not set(GO_HOSTS) & set(strict["https_hosts"])
    assert "Registries closed (the security mode high)" in strict["intro"]["text"]
    # the start itself: its pod gets the toolchain's tree and the folder, read-only; the session directory the intro
    kept = tmp_path / "kept"
    done = host.run("claude", "--no-tmux", "--no-auth", "--toolchain", "go", "--mount", f"{data}:/mnt/data:ro",
                    env={"FAKE_PODMAN_RUN": "0:0", "FAKE_PODMAN_KEEP": str(kept)})
    assert done.returncode == 0, done.stderr
    runs = [call for call in host.calls() if call[:1] == ["run"] and "--pod" in call]
    binds = [arg for call in runs for arg in call if "dst=" in arg]
    assert any("dst=/opt/toolchains/go" in arg and ",ro" in arg for arg in binds), binds
    assert any(f"src={data},dst=/mnt/data,ro" in arg for arg in binds), binds
    assert sorted(p.name for p in (kept / "session" / "toolchains" / "bin").iterdir()) == ["go", "gofmt"], \
        "one wrapper per command in /opt/toolchains/bin (first on the agent's PATH: runtime.toolchain_env)"
    text = (kept / "session" / "INTRO.md").read_text()
    assert "- `/mnt/data` - an extra folder or file, read-only" in text and "Registries open: proxy.golang.org" in text
