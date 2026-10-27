"""Opt-in real tests: no fake runtimes and no passing on CLI exit code alone."""
import hashlib
import json
import ipaddress
import os
from pathlib import Path
import signal
import shutil
import subprocess
import uuid

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.agent_live


@pytest.fixture(autouse=True)
def clean_test_credentials(tmp_path):
    yield
    # Retain test evidence, but never retain copied login tokens in pytest's
    # otherwise persistent temporary directories.
    shutil.rmtree(tmp_path / "agent-home", ignore_errors=True)


@pytest.fixture(params=["podman", "bwrap"])
def backend(request):
    selected = request.config.getoption("--agent-backend")
    if selected not in ("both", request.param):
        pytest.skip("backend not selected")
    return request.param


def launch(config, backend, agent, workspace, options=(), args=(), timeout=60):
    command = [str(ROOT / (agent + "-sandbox")), "--backend", backend, "--workspace", str(workspace)]
    if backend == "podman":
        command += ["--image", config.getoption("--agent-image")]
        if config.getoption("--agent-podman-url"):
            command += ["--podman-url", config.getoption("--agent-podman-url")]
    command += list(options) + ["--", *args]
    process = subprocess.Popen(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
        pytest.fail(f"Real {backend}/{agent} test timed out; stderr: {stderr[-5000:]}")
    assert process.returncode == 0, f"{backend}/{agent} exit {process.returncode}\n{stdout[-3000:]}\n{stderr[-5000:]}"
    return stdout, stderr


def test_real_probe(request, backend, tmp_path):
    stdout, stderr = launch(request.config, backend, "codex", tmp_path, ["--offline", "--probe", "--no-auth"])
    assert "Workspace existiert bereits" in stderr
    evidence = json.loads(stdout)
    assert evidence["verified"] is True
    for field in ["mnt", "pid"]:
        assert evidence["outer"][field] != evidence["firejail"][field]


def test_image_node_supports_current_agents(request):
    cmd = ["podman"]
    url = request.config.getoption("--agent-podman-url")
    if url:
        cmd += ["--remote", "--url", url]
    result = subprocess.run(cmd + ["run", "--rm", "--pull=never", "--network=none", "--entrypoint=node",
                                  request.config.getoption("--agent-image"), "-e",
                                  "if(Number(process.versions.node.split('.')[0])<22)throw Error('Node >=22 required; got '+process.version)"],
                            text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("agent", ["codex", "claude", "opencode"])
def test_real_cli_version(request, backend, agent, tmp_path):
    stdout, _ = launch(request.config, backend, agent, tmp_path, ["--offline"], ["--version"])
    assert any(char.isdigit() for char in stdout), stdout


@pytest.mark.parametrize("agent,relative", [("codex", ".codex/auth.json"), ("claude", ".claude/.credentials.json"), ("opencode", ".local/share/opencode/auth.json")])
def test_real_auth_copy_and_mount_permissions(request, backend, agent, relative, tmp_path):
    work = tmp_path / "new parent" / "work"
    readonly = tmp_path / "readonly"
    readonly.mkdir()
    (readonly / "reference").write_text("mounted reference")
    writable = tmp_path / "writable"
    writable.mkdir()
    source = Path.home() / relative
    assert source.is_file(), "A real auth file is required for this test"
    before = hashlib.sha256(source.read_bytes()).digest()
    script = '''
import json,os,pathlib,stat,sys
assert pathlib.Path('/mnt/reference/reference').read_text() == 'mounted reference'
for p in ['/work/forbidden', '/mnt/reference/forbidden']:
    try: pathlib.Path(p).write_text('must fail')
    except OSError: pass
    else: raise AssertionError('read-only mount is writable: '+p)
pathlib.Path('/mnt/output/writable').write_text('ok')
auth=pathlib.Path.home()/sys.argv[1]
assert stat.S_IMODE(auth.stat().st_mode) == 0o600
json.loads(auth.read_text())
auth.write_bytes(auth.read_bytes())
print('AUTH_COPY_AND_MOUNTS_OK')
'''
    stdout, _ = launch(request.config, backend, agent, work,
                       ["--offline", "--workspace-ro", "--binary", "/usr/bin/python3",
                        "--mount", f"{readonly}:/mnt/reference:ro", "--mount", f"{writable}:/mnt/output:rw"],
                       ["-I", "-c", script, relative])
    assert "AUTH_COPY_AND_MOUNTS_OK" in stdout
    assert work.is_dir()
    assert (writable / "writable").read_text() == "ok"
    assert hashlib.sha256(source.read_bytes()).digest() == before
    assert not (work / "forbidden").exists()
    assert not (readonly / "forbidden").exists()


def test_real_allowed_https_and_blocked_egress(request, backend, tmp_path):
    script = '''
import socket,ssl
host='api.openai.com'
with socket.create_connection((host,443),timeout=15) as tcp:
    with ssl.create_default_context().wrap_socket(tcp,server_hostname=host) as tls:
        assert tls.version()
for host,port in [('1.1.1.1',443),('api.openai.com',80)]:
    try: connection=socket.create_connection((host,port),timeout=2)
    except OSError: pass
    else:
        connection.close()
        raise AssertionError('forbidden destination reachable')
try: socket.getaddrinfo('example.org',443)
except OSError: pass
else: raise AssertionError('unpinned DNS succeeded')
print('HTTPS_AND_DENY_OK')
'''
    stdout, _ = launch(request.config, backend, "codex", tmp_path,
                       ["--no-provider-defaults", "--allow-host", "api.openai.com", "--binary", "/usr/bin/python3"],
                       ["-I", "-c", script])
    assert "HTTPS_AND_DENY_OK" in stdout


@pytest.mark.agent_provider
@pytest.mark.parametrize("agent", ["codex", "opencode"])
def test_real_authenticated_model_response(request, backend, agent, tmp_path, monkeypatch):
    expected = "SANDBOX_OK_" + uuid.uuid4().hex
    prompt = f"Reply with exactly {expected}. Do not call tools or access files."
    work = tmp_path / "work"
    work.mkdir()
    options = ["--state-dir", str(tmp_path / "agent-home")]
    if agent == "opencode" and request.config.getoption("--opencode-codex-auth"):
        options += ["--import-codex-auth", str(Path.home() / ".codex/auth.json")]
    else:
        options += ["--copy-auth"]
    if agent == "codex":
        args = ["exec", "--skip-git-repo-check", "--ephemeral", "--ignore-user-config", "--json",
                "-c", 'cli_auth_credentials_store="file"', "--output-last-message", "/work/answer.txt"]
        if request.config.getoption("--codex-model"):
            args += ["--model", request.config.getoption("--codex-model")]
        args += [prompt]
    else:
        for name in ["OPENCODE_DISABLE_MODELS_FETCH", "OPENCODE_DISABLE_AUTOUPDATE", "OPENCODE_DISABLE_SHARE",
                     "OPENCODE_DISABLE_PROJECT_CONFIG", "OPENCODE_DISABLE_CLAUDE_CODE"]:
            monkeypatch.setenv(name, "1")
            options += ["--env", name]
        args = ["run", "--pure", "--format", "json", "--model", request.config.getoption("--opencode-model"), prompt]
    stdout, _ = launch(request.config, backend, agent, work, options, args, timeout=150)
    events = [json.loads(line) for line in stdout.splitlines() if line.startswith("{")]
    assert not any(event.get("type") in ("error", "turn.failed") for event in events), events
    if agent == "codex":
        assert (work / "answer.txt").read_text().strip() == expected
        assert any(event.get("type") == "turn.completed" for event in events)
    else:
        texts = [event.get("part", {}).get("text", "") for event in events if event.get("type") == "text"]
        assert "".join(texts).strip() == expected, events
        assert any(event.get("type") == "step_finish" for event in events)


def test_real_named_network_static_ips_and_cidr_permissions(request, tmp_path):
    if request.config.getoption("--agent-backend") == "bwrap":
        pytest.skip("Podman network option")
    engine = ["podman"]
    if request.config.getoption("--agent-podman-url"):
        engine += ["--remote", "--url", request.config.getoption("--agent-podman-url")]
    name = "agent-network-test-" + uuid.uuid4().hex[:12]
    created = False
    server = None
    def run(*args):
        return subprocess.run([*engine, *args], check=True, capture_output=True, text=True, timeout=30).stdout.strip()
    try:
        run("network", "create", "--ipv6", name)
        created = True
        network = json.loads(run("network", "inspect", name))[0]
        subnets = [ipaddress.ip_network(entry["subnet"]) for entry in network["subnets"]]
        ipv4 = next(net for net in subnets if net.version == 4)
        ipv6 = next(net for net in subnets if net.version == 6)
        server = run("run", "--detach", "--rm", "--pull=never", "--read-only", "--cap-drop=ALL",
                     "--network", name, "--ip", str(ipv4[11]), "--ip6", str(ipv6[11]),
                     "--entrypoint=/usr/bin/python3", request.config.getoption("--agent-image"),
                     "-u", "-m", "http.server", "18080", "--bind", "::", "--directory", "/work")
        script = '''
import pathlib,sys,urllib.request
v4,v6,peer4,peer6=sys.argv[1:]
assert v4 in pathlib.Path('/proc/net/fib_trie').read_text()
assert v6 in pathlib.Path('/proc/net/if_inet6').read_text()
for peer in [peer4,'['+peer6+']']:
    with urllib.request.urlopen('http://'+peer+':18080/',timeout=10) as response:
        assert response.status == 200
print('STATIC_IPV4_IPV6_AND_CIDR_OK')
'''
        stdout, _ = launch(request.config, "podman", "codex", tmp_path,
                           ["--network", name, "--ip", str(ipv4[10]), "--ip6", str(ipv6[10]),
                            "--no-provider-defaults", "--allow", str(ipv4), "--allow", str(ipv6),
                            "--binary", "/usr/bin/python3"],
                           ["-I", "-c", script, str(ipv4[10]), ipv6[10].packed.hex(), str(ipv4[11]), str(ipv6[11])])
        assert "STATIC_IPV4_IPV6_AND_CIDR_OK" in stdout
    finally:
        if server:
            run("rm", "--force", "--time=1", server)
        if created:
            run("network", "rm", name)


@pytest.mark.agent_provider
@pytest.mark.parametrize("agent", ["codex", "opencode"])
def test_practical_agent_tools(request, backend, agent, tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    nonce = uuid.uuid4().hex
    left, right = int(nonce[:4], 16), int(nonce[4:8], 16)
    expected = {"sum": left + right, "nonce": nonce}
    (work / "input.json").write_text(json.dumps({"left": left, "right": right, "nonce": nonce}))
    verification = "import json,pathlib; p=pathlib.Path('.'); i=json.loads((p/'input.json').read_text()); r=json.loads((p/'result.json').read_text()); assert r=={'sum':i['left']+i['right'],'nonce':i['nonce']}; (p/'verified.txt').write_text('verified:'+i['nonce']); print('SHELL_OK_'+i['nonce'])"
    import shlex
    prompt = ("Practical sandbox check in the current workspace. Read input.json. Create result.json with exactly "
              "the keys sum (left+right as an integer) and nonce (copied unchanged from input.json). "
              "Then execute this command using your shell tool: python3 -c " + shlex.quote(verification) +
              ". Do not read credentials or access anything outside the workspace. Finish with PRACTICAL_OK.")
    options = ["--state-dir", str(tmp_path / "agent-home")]
    if agent == "codex":
        model = request.config.getoption("--codex-model") or "gpt-6.1-sol"
        args = ["exec", "--skip-git-repo-check", "--ephemeral", "--ignore-user-config", "--json",
                "--model", model, prompt]
    else:
        model = request.config.getoption("--free-model")
        provider, model_id = model.split("/", 1)
        catalog = json.loads((Path.home() / ".cache/opencode/models.json").read_text())
        definition = catalog[provider]["models"][model_id]
        assert definition["cost"]["input"] == definition["cost"]["output"] == 0
        assert definition.get("status") != "deprecated"
        config = tmp_path / "config.json"
        config.write_text(json.dumps({"enabled_providers": [provider], "model": model, "small_model": model,
                                      "share": "disabled", "autoupdate": False, "permission": {"*": "allow"}}))
        options += ["--no-auth", "--copy", f"{config}:.config/opencode/opencode.json"]
        for name in ["OPENCODE_DISABLE_MODELS_FETCH", "OPENCODE_DISABLE_AUTOUPDATE", "OPENCODE_DISABLE_SHARE",
                     "OPENCODE_DISABLE_PROJECT_CONFIG", "OPENCODE_DISABLE_CLAUDE_CODE"]:
            monkeypatch.setenv(name, "1")
            options += ["--env", name]
        args = ["run", "--pure", "--auto", "--format", "json", "--model", model, prompt]
    stdout, _ = launch(request.config, backend, agent, work, options, args, timeout=180)
    events = [json.loads(line) for line in stdout.splitlines() if line.startswith("{")]
    assert not any(event.get("type") in ("error", "turn.failed") for event in events), events
    assert (work / "result.json").exists(), stdout[-14000:]
    assert json.loads((work / "result.json").read_text()) == expected
    assert (work / "verified.txt").read_text() == "verified:" + nonce
    shell_output = "SHELL_OK_" + nonce
    if agent == "codex":
        tools = [event["item"] for event in events if event.get("type") == "item.completed" and event.get("item", {}).get("type") == "command_execution"]
        assert any(item.get("exit_code") == 0 and shell_output in item.get("aggregated_output", "") for item in tools), events
        assert any(event.get("type") == "turn.completed" for event in events)
        cost = None
    else:
        tools = [event["part"] for event in events if event.get("type") == "tool_use" and event.get("part", {}).get("tool") == "bash"]
        assert any(part.get("state", {}).get("status") == "completed" and shell_output in part.get("state", {}).get("output", "") for part in tools), events
        finishes = [event["part"] for event in events if event.get("type") == "step_finish"]
        assert finishes
        cost = sum(part.get("cost", 0) for part in finishes)
        assert cost == 0, "Free-model test reported a nonzero cost"
    evidence = {"agent": agent, "backend": backend, "model": model, "status": "passed",
                "checks": {"read_unprompted_nonce": True, "wrote_correct_result": True, "shell_execution_verified": True},
                "completed_shell_tools": len(tools), "reported_cost": cost,
                "input": {"left": left, "right": right, "nonce": nonce}, "result": expected,
                "shell_marker": shell_output}
    if agent == "codex":
        evidence["codex_native_sandbox"] = "danger-full-access"
        evidence["codex_sandbox_selection"] = "launcher default, not a test-only override"
        evidence["external_isolation"] = "outer namespace + verified inner Firejail"
    target = ROOT / "evidence" / f"practical-{backend}-{agent}.json"
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(evidence, indent=2) + "\n")
