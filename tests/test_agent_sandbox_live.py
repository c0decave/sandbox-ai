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
    command = [str(ROOT / "sandbox-ai"), agent, "--backend", backend, "--workspace", str(workspace)]
    if backend == "podman":
        if config.getoption("--agent-image"):
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


@pytest.mark.parametrize("agent", ["codex", "claude", "opencode"])
def test_real_probe(request, backend, agent, tmp_path):
    stdout, stderr = launch(request.config, backend, agent, tmp_path, ["--offline", "--probe", "--no-auth"])
    assert "workspace exists already" in stderr
    evidence = json.loads(stdout)
    assert evidence["verified"] is True
    for field in ["mnt", "pid"]:
        assert evidence["outer"][field] != evidence["firejail"][field]


@pytest.mark.parametrize("agent", ["codex", "claude", "opencode"])
def test_image_node_supports_current_agents(request, agent):
    if request.config.getoption("--agent-backend") == "bwrap":
        pytest.skip("Podman images")
    cmd = ["podman"]
    url = request.config.getoption("--agent-podman-url")
    if url:
        cmd += ["--remote", "--url", url]
    result = subprocess.run(cmd + ["run", "--rm", "--pull=never", "--network=none", "--entrypoint=node",
                                  request.config.getoption("--agent-image") or f"localhost/{agent}-sandbox:latest", "-e",
                                  "if(Number(process.versions.node.split('.')[0])<22)throw Error('Node >=22 required; got '+process.version);"
                                  "if(!process.release.lts)throw Error('Node LTS release required; got '+process.version)"],
                            text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("agent", ["codex", "claude", "opencode"])
def test_image_contains_only_selected_cli(request, agent):
    if request.config.getoption("--agent-backend") == "bwrap":
        pytest.skip("Podman images")
    engine = ["podman"]
    if request.config.getoption("--agent-podman-url"):
        engine += ["--remote", "--url", request.config.getoption("--agent-podman-url")]
    script = """
import shutil,sys
agent=sys.argv[1]
for name in ['codex','claude','opencode']:
    assert bool(shutil.which(name)) == (name == agent), name
for name in ['python3','pip3','uv','uvx','firejail','nft','setpriv','git','rg',
             'vim','nc','host','dig','ping','curl','wget']:
    assert shutil.which(name), name
for name in ['gcc','g++','make','less']:  # ps arrives with the tools added in 0.1.20
    assert not shutil.which(name), name
"""
    result = subprocess.run([*engine, "run", "--rm", "--pull=never", "--network=none",
                             "--entrypoint=/usr/bin/python3",
                             request.config.getoption("--agent-image") or f"localhost/{agent}-sandbox:latest",
                             "-I", "-c", script, agent], text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("agent", ["codex", "claude", "opencode"])
def test_real_cli_version(request, backend, agent, tmp_path):
    stdout, _ = launch(request.config, backend, agent, tmp_path, ["--offline", "--no-auth"], ["--version"])
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
    if not source.is_file():
        pytest.skip(f"needs a real login of {agent} on this host ({source})")
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


@pytest.mark.parametrize("agent,host,foreign", [
    ("codex", "api.openai.com", "api.anthropic.com"),
    ("claude", "api.anthropic.com", "api.openai.com"),
    ("opencode", "opencode.ai", "api.openai.com"),
])
def test_real_allowed_https_and_blocked_egress(request, backend, agent, host, foreign, tmp_path):
    script = '''
import pathlib,socket,ssl,sys
host,foreign=sys.argv[1:]
assert foreign not in pathlib.Path('/etc/hosts').read_text()
with socket.create_connection((host,443),timeout=15) as tcp:
    with ssl.create_default_context().wrap_socket(tcp,server_hostname=host) as tls:
        assert tls.version()
# The connection proxy accepts every connection and refuses a forbidden one right away: no
# TLS session (no data at all) may come about.
for host,port in [('1.1.1.1',443),(host,80)]:
    try:
        with socket.create_connection((host,port),timeout=5) as connection:
            ssl.create_default_context().wrap_socket(connection,server_hostname=host).close()
    except (OSError, ssl.SSLError): pass
    else: raise AssertionError('forbidden destination reachable')
for name in ['example.org',foreign]:
    try: socket.getaddrinfo(name,443)
    except OSError: pass
    else: raise AssertionError('unpinned DNS succeeded: '+name)
print('HTTPS_AND_DENY_OK')
'''
    stdout, _ = launch(request.config, backend, agent, tmp_path,
                       ["--no-auth", "--binary", "/usr/bin/python3"],
                       ["-I", "-c", script, host, foreign])
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
        # This test explicitly selects OpenAI through OpenCode.
        options += ["--allow-host", "api.openai.com", "--allow-host", "chatgpt.com", "--allow-host", "auth.openai.com"]
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
                     "--entrypoint=/usr/bin/python3", request.config.getoption("--agent-image") or "localhost/codex-sandbox:latest",
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
@pytest.mark.parametrize("agent", ["codex", "claude", "opencode"])
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
    elif agent == "claude":
        model = request.config.getoption("--claude-model")
        options += ["--copy-auth"]
        args = ["--print", "--verbose", "--output-format", "stream-json",
                "--no-session-persistence", "--setting-sources", "",
                "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                "--permission-mode", "dontAsk", "--allowedTools", "Bash,Read,Write,Edit",
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
    elif agent == "claude":
        tools = [block for event in events if event.get("type") == "assistant"
                 for block in event.get("message", {}).get("content", [])
                 if block.get("type") == "tool_use" and block.get("name") == "Bash"]
        shell_ids = {tool["id"] for tool in tools}
        results = [block for event in events if event.get("type") == "user"
                   for block in event.get("message", {}).get("content", [])
                   if block.get("type") == "tool_result" and block.get("tool_use_id") in shell_ids]
        assert any(not block.get("is_error") and shell_output in str(block.get("content", "")) for block in results), events
        finishes = [event for event in events if event.get("type") == "result"]
        assert finishes and not finishes[-1].get("is_error"), events
        cost = finishes[-1].get("total_cost_usd")
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


@pytest.mark.parametrize("agent", ["codex", "claude", "opencode"])
def test_real_dangerously_skip_permissions_flags_are_accepted(request, backend, agent, tmp_path):
    # Claude refuses the flag as root without IS_SANDBOX; this proves the
    # injected flag and environment are accepted by the real CLI.
    stdout, _ = launch(request.config, backend, agent, tmp_path,
                       ["--offline", "--no-auth", "--dangerously-skip-permissions"], ["--version"])
    assert any(char.isdigit() for char in stdout), stdout


# In scripts that run inside a sandbox: can data flow to HOST:PORT? The connection proxy accepts every
# connection and resets a forbidden one at once.
EXCHANGES = '''
import socket, ssl
def exchanges(host, port, timeout=5):
    try:
        with socket.create_connection((host, port), timeout=timeout) as connection:
            ssl.create_default_context().wrap_socket(connection, server_hostname=host).close()
        return True
    except (OSError, ssl.SSLError):
        return False
'''


def test_real_dns_resolves_but_does_not_open_egress(request, backend, tmp_path):
    script = EXCHANGES + '''
import socket
assert socket.getaddrinfo('example.org', 443, type=socket.SOCK_STREAM)
for host, port in [('example.org', 443), ('1.1.1.1', 443)]:
    if exchanges(host, port):
        raise AssertionError('egress open with --dns only: %s:%d' % (host, port))
query = b'\\x12\\x34\\x01\\x00\\x00\\x01\\x00\\x00\\x00\\x00\\x00\\x00\\x07example\\x03org\\x00\\x00\\x01\\x00\\x01'
udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
udp.settimeout(2)
try:
    udp.sendto(query, ('9.9.9.9', 53))
    udp.recv(512)
except OSError: pass
else: raise AssertionError('unconfigured resolver reachable')
print('DNS_ONLY_OK')
'''
    stdout, _ = launch(request.config, backend, "codex", tmp_path,
                       ["--no-auth", "--no-provider-defaults", "--dns", "--binary", "/usr/bin/python3"], ["-I", "-c", script])
    assert "DNS_ONLY_OK" in stdout


def test_real_egress_all_allow(request, backend, tmp_path):
    script = '''
import socket, ssl
for host in ['example.org', 'pypi.org']:
    with socket.create_connection((host, 443), timeout=15) as tcp:
        with ssl.create_default_context().wrap_socket(tcp, server_hostname=host) as tls:
            assert tls.version()
socket.create_connection(('1.1.1.1', 443), timeout=10).close()
print('EGRESS_ALL_OK')
'''
    stdout, _ = launch(request.config, backend, "codex", tmp_path,
                       ["--no-auth", "--egress-all-allow", "--binary", "/usr/bin/python3"], ["-I", "-c", script])
    assert "EGRESS_ALL_OK" in stdout


def test_real_frontier_allows_openai_and_anthropic_only(request, backend, tmp_path):
    script = '''
import socket, ssl
for host in ['api.openai.com', 'api.anthropic.com', 'opencode.ai']:
    with socket.create_connection((host, 443), timeout=15) as tcp:
        with ssl.create_default_context().wrap_socket(tcp, server_hostname=host) as tls:
            assert tls.version()
try: socket.getaddrinfo('example.org', 443)
except OSError: pass
else: raise AssertionError('unpinned DNS succeeded')
print('FRONTIER_OK')
'''
    stdout, _ = launch(request.config, backend, "opencode", tmp_path,
                       ["--no-auth", "--frontier", "--binary", "/usr/bin/python3"], ["-I", "-c", script])
    assert "FRONTIER_OK" in stdout


def test_real_logs_denied_egress_and_file_access(request, backend, tmp_path):
    work, logs = tmp_path / "work", tmp_path / "logs"
    work.mkdir()
    script = EXCHANGES + '''
import pathlib, socket
for target in [('192.0.2.1', 8443), ('blocked.example', 443)]:
    if exchanges(*target):
        raise AssertionError('denied target reachable: %r' % (target,))
pathlib.Path('/work/logged.txt').write_text('logged')
assert pathlib.Path('/work/logged.txt').read_text() == 'logged'
try: open('/sandbox/log/files.jsonl', 'a').write('{"forged": true}\\n')
except OSError: pass
else: raise AssertionError('agent can write the log directory')
print('LOG_SCENARIO_OK')
'''
    stdout, _ = launch(request.config, backend, "codex", work,
                       # --ask off: refused at once and logged (with questions it would wait for an answer)
                       ["--no-auth", "--ask", "off", "--log-dir", str(logs), "--binary", "/usr/bin/python3"],
                       ["-I", "-c", script])
    assert "LOG_SCENARIO_OK" in stdout
    denied = [json.loads(line) for line in (logs / "egress-denied.jsonl").read_text().splitlines()]
    assert any(r.get("host") == "blocked.example" for r in denied), denied  # the DNS stub
    # A forbidden address is refused by the connection proxy, which logs it with the destination.
    connections = [json.loads(line) for path in logs.glob("connections-*.jsonl") for line in path.read_text().splitlines()]
    assert any(r.get("event") == "denied" and r.get("destination") == "192.0.2.1:8443" for r in connections), connections
    files = [json.loads(line) for line in (logs / "files.jsonl").read_text().splitlines()]
    actions = {(r.get("action"), r.get("path")) for r in files}
    assert ("write", "/work/logged.txt") in actions and ("read", "/work/logged.txt") in actions, files
    assert not any(r.get("forged") for r in files)
    assert (logs.stat().st_mode & 0o777) == 0o700


@pytest.mark.agent_provider
@pytest.mark.parametrize("agent", ["codex", "claude"])
def test_practical_dangerously_skip_permissions_runs_tools_without_prompt(request, backend, agent, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    nonce = "DANGER_OK_" + uuid.uuid4().hex
    prompt = f"Use your shell tool to run: echo {nonce} > danger.txt . Do not ask for confirmation. Finish with DONE."
    options = ["--state-dir", str(tmp_path / "agent-home"), "--dangerously-skip-permissions"]
    if agent == "codex":
        args = ["exec", "--skip-git-repo-check", "--ephemeral", "--ignore-user-config",
                "--model", request.config.getoption("--codex-model") or "gpt-6.1-sol", prompt]
    else:
        options += ["--copy-auth"]
        args = ["--print", "--no-session-persistence", "--setting-sources", "",
                "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                "--model", request.config.getoption("--claude-model"), prompt]
    launch(request.config, backend, agent, work, options, args, timeout=180)
    assert (work / "danger.txt").read_text().strip() == nonce


def test_real_default_tools_work_through_open_egress(request, backend, tmp_path):
    if backend != "podman":
        pytest.skip("bwrap uses the host's /usr; the image tool set applies to Podman")
    script = (
        "set -e; "
        "curl -fsS -o /dev/null https://example.org; "
        "wget -q -O /dev/null https://example.org; "
        "dig +short example.org | grep -q .; "
        "host example.org >/dev/null; "
        "nc -z -w 5 example.org 443; "
        "ping -c 1 -W 5 1.1.1.1 >/dev/null || { echo \"ping_group_range=$(cat /proc/sys/net/ipv4/ping_group_range) "
        "id=$(id)\" >&2; exit 3; }; "
        "vim --version >/dev/null; git --version >/dev/null; "
        "echo TOOLS_OK"
    )
    stdout, _ = launch(request.config, backend, "codex", tmp_path,
                       ["--no-auth", "--egress-all-allow", "--binary", "/bin/sh"], ["-c", script], timeout=90)
    assert "TOOLS_OK" in stdout


def test_real_multiple_workspaces(request, backend, tmp_path):
    main, extra = tmp_path / "main", tmp_path / "extra lib"
    script = '''
import os, pathlib
assert os.getcwd() == '/work'
pathlib.Path('/work/main.txt').write_text('main')
pathlib.Path('/workspaces/extra lib/extra.txt').write_text('extra')
assert sorted(os.listdir('/workspaces')) == ['extra lib']
print('MULTI_WORKSPACE_OK')
'''
    stdout, _ = launch(request.config, backend, "codex", main,
                       ["--offline", "--no-auth", "--workspace", str(extra), "--binary", "/usr/bin/python3"],
                       ["-I", "-c", script])
    assert "MULTI_WORKSPACE_OK" in stdout
    assert (main / "main.txt").read_text() == "main"
    assert (extra / "extra.txt").read_text() == "extra"


def gui_host_tools_or_skip(backend):
    # bwrap runs the host's /usr; the image provides these for Podman.
    if backend == "bwrap" and not all(shutil.which(tool) for tool in ("Xvfb", "xdpyinfo")):
        pytest.skip("bwrap GUI test needs Xvfb and xdpyinfo on the host")


def test_real_gui_virtual_display(request, backend, tmp_path):
    gui_host_tools_or_skip(backend)
    script = '''
import os, subprocess
assert os.environ['DISPLAY'] == ':99'
info = subprocess.run(['xdpyinfo'], capture_output=True, text=True, check=True).stdout
assert '1280x720 pixels' in info, info
with open('/dev/shm/probe', 'wb') as shm:
    shm.write(b'x' * 128 * 1024 * 1024)
os.unlink('/dev/shm/probe')
print('GUI_DISPLAY_OK')
'''
    stdout, _ = launch(request.config, backend, "codex", tmp_path,
                       ["--offline", "--no-auth", "--gui", "--gui-screen", "1280x720x24", "--binary", "/usr/bin/python3"],
                       ["-I", "-c", script])
    assert "GUI_DISPLAY_OK" in stdout


def test_real_gui_vnc_unix_socket_serves_rfb(request, backend, tmp_path):
    gui_host_tools_or_skip(backend)
    if backend == "bwrap" and not shutil.which("x11vnc"):
        pytest.skip("bwrap VNC test needs x11vnc on the host")
    import socket, time
    vnc = tmp_path / "vnc"
    command = [str(ROOT / "sandbox-ai"), "codex", "--backend", backend, "--workspace", str(tmp_path / "work"),
               "--offline", "--no-auth", "--gui-vnc", str(vnc), "--binary", "/usr/bin/python3",
               "--", "-I", "-c", "import os,time; assert not os.access('/sandbox/vnc', os.R_OK); time.sleep(20)"]
    if backend == "podman" and request.config.getoption("--agent-image"):
        command[1:1] = ["--image", request.config.getoption("--agent-image")]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        deadline = time.time() + 30
        while not (vnc / "vnc.sock").exists() and time.time() < deadline:
            time.sleep(0.2)
        client = socket.socket(socket.AF_UNIX)
        client.settimeout(10)
        for attempt in range(50):  # the socket file appears before x11vnc listens on it
            try:
                client.connect(str(vnc / "vnc.sock"))
                break
            except ConnectionRefusedError:
                if attempt == 49:
                    raise
                time.sleep(0.2)
        assert client.recv(12).startswith(b"RFB 003.")
        client.close()
        assert (vnc.stat().st_mode & 0o777) == 0o700
    finally:
        _, stderr = process.communicate(timeout=60)
    assert process.returncode == 0, stderr


def test_real_gui_wayland_socket(request, backend, tmp_path):
    if not os.environ.get("WAYLAND_DISPLAY"):
        pytest.skip("host has no Wayland session")
    script = '''
import os, socket
path = os.path.join(os.environ['XDG_RUNTIME_DIR'], os.environ['WAYLAND_DISPLAY'])
client = socket.socket(socket.AF_UNIX)
client.connect(path)
client.close()
print('WAYLAND_OK')
'''
    stdout, stderr = launch(request.config, backend, "codex", tmp_path,
                            ["--offline", "--no-auth", "--gui-wayland", "--binary", "/usr/bin/python3"], ["-I", "-c", script])
    assert "WAYLAND_OK" in stdout
    assert "--gui-wayland gives the agent access" in stderr


@pytest.mark.agent_network
def test_real_playwright_headed_chromium_screenshot(request, backend, tmp_path):
    if backend != "podman":
        pytest.skip("Playwright runtime libraries are provided by the Podman image")
    script = (
        "set -e; uvx --from playwright playwright install chromium >/dev/null; "
        "uv run --quiet --with playwright python - <<'PY'\n"
        "from playwright.sync_api import sync_playwright\n"
        "with sync_playwright() as p:\n"
        "    browser = p.chromium.launch(headless=False)\n"
        "    page = browser.new_page()\n"
        "    page.set_content('<h1 id=t>SANDBOX_GUI</h1>')\n"
        "    assert page.inner_text('#t') == 'SANDBOX_GUI'\n"
        "    page.screenshot(path='/work/shot.png')\n"
        "    browser.close()\n"
        "PY\n"
        "echo PLAYWRIGHT_OK"
    )
    stdout, _ = launch(request.config, backend, "codex", tmp_path,
                       ["--no-auth", "--gui", "--egress-all-allow", "--binary", "/bin/sh"], ["-c", script], timeout=600)
    assert "PLAYWRIGHT_OK" in stdout
    assert (tmp_path / "shot.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def session_cli(*args, env):
    return subprocess.run([str(ROOT / "sandbox-ai"), "session", *args], capture_output=True, text=True, env=env, timeout=120)


def test_real_session_pause_resume_stop_restart(request, backend, tmp_path):
    import time
    env = {**os.environ, "XDG_STATE_HOME": str(tmp_path / "state")}
    name = "live-" + uuid.uuid4().hex[:8]
    work = tmp_path / "work"
    # The "agent" counts in the workspace; a frozen session must not count.
    counter = ("import pathlib,time\np=pathlib.Path('/work/count')\n"
               "n=int(p.read_text()) if p.exists() else 0\n"
               "while True:\n n+=1; p.write_text(str(n)); time.sleep(0.2)\n")
    command = [str(ROOT / "sandbox-ai"), "codex", "--backend", backend, "--workspace", str(work), "--offline", "--no-auth",
               "--session", name, "--binary", "/usr/bin/python3", "--", "-I", "-c", counter]
    if backend == "podman" and request.config.getoption("--agent-image"):
        command[1:1] = ["--image", request.config.getoption("--agent-image")]
    def count():
        return int((work / "count").read_text())
    def state():
        line = next(l for l in session_cli("list", env=env).stdout.splitlines() if l.startswith(name))
        return line.split()[3]
    started = subprocess.run(command, capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL, timeout=180)
    try:
        assert started.returncode == 0, started.stderr
        time.sleep(2)
        assert state() == "running" and count() > 0
        assert session_cli("pause", name, env=env).returncode == 0
        assert state() == "paused"
        frozen = count()
        time.sleep(2)
        assert count() == frozen, "paused session kept running"
        assert session_cli("resume", name, env=env).returncode == 0
        time.sleep(2)
        assert state() == "running" and count() > frozen
        assert session_cli("stop", name, env=env).returncode == 0
        assert state() == "stopped"
        assert (tmp_path / "state/sandbox-ai/sessions" / name / "home").is_dir()
        stopped_at = count()
        restarted = session_cli("resume", name, env=env)
        assert restarted.returncode == 0, restarted.stderr
        time.sleep(2)
        assert state() == "running" and count() > stopped_at
    finally:
        session_cli("stop", name, env=env)
    assert session_cli("delete", name, "--yes", env=env).returncode == 0


def test_real_session_restarts_after_supervisor_was_killed(request, tmp_path):
    """A supervisor killed without cleanup (crash, reboot) leaves its pod; resume removes it and restarts."""
    import signal, time
    if request.config.getoption("--agent-backend") == "bwrap":
        pytest.skip("Podman only: bwrap leaves no pod")
    env = {**os.environ, "XDG_STATE_HOME": str(tmp_path / "state")}
    name = "live-" + uuid.uuid4().hex[:8]
    work = tmp_path / "work"
    counter = ("import pathlib,time\np=pathlib.Path('/work/count')\n"
               "n=int(p.read_text()) if p.exists() else 0\n"
               "while True:\n n+=1; p.write_text(str(n)); time.sleep(0.2)\n")
    command = [str(ROOT / "sandbox-ai"), "codex", "--workspace", str(work), "--offline", "--no-auth",
               "--session", name, "--binary", "/usr/bin/python3", "--", "-I", "-c", counter]
    if request.config.getoption("--agent-image"):
        command[1:1] = ["--image", request.config.getoption("--agent-image")]
    started = subprocess.run(command, capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL, timeout=180)
    try:
        assert started.returncode == 0, started.stderr
        data = json.loads((tmp_path / "state/sandbox-ai/sessions" / name / "session.json").read_text())
        os.kill(data["pid"], signal.SIGKILL)
        time.sleep(1)
        pod = subprocess.run(["podman", "pod", "exists", data["pod"]], timeout=60)
        assert pod.returncode == 0, "the killed supervisor should have left its pod behind"
        restarted = session_cli("resume", name, env=env)
        assert restarted.returncode == 0, restarted.stderr
        assert "left behind" in restarted.stderr
        before = int((work / "count").read_text())
        time.sleep(2)
        assert int((work / "count").read_text()) > before
    finally:
        session_cli("stop", name, env=env)
    assert session_cli("delete", name, "--yes", env=env).returncode == 0


@pytest.mark.parametrize("flag", [["--tmux"], []], ids=["explicit", "default-in-a-terminal"])
def test_real_tmux_mode_has_agent_and_shell_windows(request, backend, tmp_path, flag):
    if backend == "bwrap" and not shutil.which("tmux"):
        pytest.skip("bwrap tmux mode needs tmux on the host")
    import fcntl, pty, struct, termios, threading, time
    # The agent drives the shell window, then exits; ending the shell ends the sandbox. By default an
    # other binary (--binary) runs without tmux even in a terminal: then there is no tmux to drive.
    script = ("import os,pathlib,subprocess,time\n"
              "pathlib.Path('/work/agent.txt').write_text('agent')\n"
              "if os.environ.get('TMUX'):\n"
              "    subprocess.run(['tmux','send-keys','-t','sandbox:shell','echo shell > /work/shell.txt; exit','Enter'],check=True)\n"
              "    time.sleep(1)\n"
              "else:\n"
              "    pathlib.Path('/work/no-tmux.txt').write_text('direct')\n")
    command = [str(ROOT / "sandbox-ai"), "codex", "--backend", backend, "--workspace", str(tmp_path), "--offline", "--no-auth",
               *flag, "--binary", "/usr/bin/python3", "--", "-I", "-c", script]
    if backend == "podman" and request.config.getoption("--agent-image"):
        command[1:1] = ["--image", request.config.getoption("--agent-image")]
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
    process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
    os.close(slave)
    seen = []
    def drain():
        try:
            while True:
                data = os.read(master, 4096)
                if not data:
                    break
                seen.append(data)
        except OSError:
            pass
    threading.Thread(target=drain, daemon=True).start()
    try:
        status = process.wait(timeout=120)
        time.sleep(0.5)
        assert status == 0, b"".join(seen)[-4000:].decode("utf-8", "replace")
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
        os.close(master)
    assert (tmp_path / "agent.txt").read_text() == "agent"
    if flag:  # --tmux: the agent in window 0 drove the shell in window 1
        assert (tmp_path / "shell.txt").read_text().strip() == "shell"
    else:     # default with --binary: no tmux, the program runs directly in the terminal
        assert (tmp_path / "no-tmux.txt").read_text() == "direct" and not (tmp_path / "shell.txt").exists()


def test_real_opencode_uses_local_llm_only(request, backend, tmp_path, monkeypatch):
    target = request.config.getoption("--local-llm")
    if not target:
        pytest.skip("requires --local-llm KIND:IP:PORT")
    import importlib.util
    spec = importlib.util.spec_from_file_location("wizard_live", ROOT / "sandbox/wizard.py")
    wizard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wizard)
    kind, rest = target.split(":", 1)
    host, port = rest.rsplit(":", 1)
    models = wizard.sort_models(wizard.discover(kind, host.strip("[]"), int(port)), "loaded")
    assert models, "the local server offers no models"
    config = tmp_path / "opencode.json"
    config.write_text(json.dumps(wizard.opencode_config(kind, host.strip("[]"), int(port), models[0]["name"])))
    work = tmp_path / "work"
    work.mkdir()
    expected = "LOCAL_OK_" + uuid.uuid4().hex[:8]
    stdout, _ = launch(request.config, backend, "opencode", work,
                       ["--no-auth", "--local-config", str(config), "--allow-tcp", f"{host}:{port}"],
                       ["run", "--format", "json", f"Reply with exactly {expected} and nothing else."], timeout=300)
    events = [json.loads(line) for line in stdout.splitlines() if line.startswith("{")]
    texts = "".join(e.get("part", {}).get("text", "") for e in events if e.get("type") == "text")
    assert expected in texts, events


MCP_CLIENT = '''
import json, os, subprocess
bridge = subprocess.Popen(["python3", "-I", "/sandbox/runtime.py", "--internal-mcp-bridge",
                           "/run/sandbox-mcp/demo/server.sock"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
def call(message):
    bridge.stdin.write(json.dumps(message) + "\\n"); bridge.stdin.flush()
    return json.loads(bridge.stdout.readline()) if "id" in message else None
call({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
call({"jsonrpc": "2.0", "method": "notifications/initialized"})
listing = call({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "list_directory", "arguments": {}}})
version = call({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "version", "arguments": {}}})
bridge.stdin.close(); bridge.wait(timeout=10)
print("MCP_RESULT " + json.dumps({"listing": json.loads(listing["result"]["content"][0]["text"]),
                                  "version": json.loads(version["result"]["content"][0]["text"]),
                                  "own_pid_namespace": os.readlink("/proc/self/ns/pid")}))
'''


def test_real_mcp_demo_server_in_its_own_sandbox(request, backend, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    marker = tmp_path / "state/sandbox-ai/mcp/demo/work"
    marker.mkdir(parents=True)
    (marker / "from-mcp-pod.txt").write_text("hello")
    env = {**os.environ}
    try:
        results = []
        for _ in range(2):  # the second run must reuse the running server
            stdout, stderr = launch(request.config, backend, "codex", tmp_path / "work",
                                    ["--offline", "--no-auth", "--mcp", "demo", "--binary", "/usr/bin/python3"],
                                    ["-I", "-c", MCP_CLIENT], timeout=180)
            results.append((json.loads(stdout.split("MCP_RESULT ", 1)[1]), stderr))
        result = results[0][0]
        assert {"name": "from-mcp-pod.txt", "type": "file", "size": 5} in result["listing"]["entries"]
        assert result["version"]["version"] == "1.0.0" and result["version"]["seccomp"] == "2"
        assert result["version"]["pid_namespace"] != result["own_pid_namespace"]
        assert "MCP server demo started" in results[0][1] and "MCP server demo started" not in results[1][1]
        listed = subprocess.run([str(ROOT / "sandbox-ai"), "mcp", "list"], capture_output=True, text=True, env=env, timeout=30)
        assert any(line.split()[:2] == ["demo", "running"] for line in listed.stdout.splitlines())
    finally:
        subprocess.run([str(ROOT / "sandbox-ai"), "mcp", "stop", "demo"], capture_output=True, env=env, timeout=60)


@pytest.mark.agent_provider
@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_practical_agent_calls_demo_mcp_tool(request, backend, agent, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    prompt = ("Call the version tool of the MCP server named demo and reply with only the value of its "
              "version field.")
    options = ["--mcp", "demo", "--dangerously-skip-permissions", "--state-dir", str(tmp_path / "agent-home")]
    if agent == "claude":
        options += ["--copy-auth"]
        args = ["--print", "--model", request.config.getoption("--claude-model"), prompt]
    else:
        args = ["exec", "--skip-git-repo-check", "--ephemeral", prompt]
    try:
        stdout, _ = launch(request.config, backend, agent, tmp_path / "work", options, args, timeout=240)
        assert "1.0.0" in stdout
    finally:
        subprocess.run([str(ROOT / "sandbox-ai"), "mcp", "stop", "demo"], capture_output=True,
                       env={**os.environ}, timeout=60)
