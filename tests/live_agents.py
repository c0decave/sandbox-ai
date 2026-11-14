"""What the live checks of Hermes, Qwen Code, DSH, Goose and Cairn share (tests/test_hermes_live.py, test_qwen_live.py,
test_dsh_live.py, test_goose_live.py, test_cairn_live.py; scripts/live_check_agents.sh runs them on your machine, CI's
job "agents-live" on GitHub's).

Each check starts a real sandbox of the agent's image with Podman against a scripted model server on this host's
loopback (tests/devtools/agent_model.py, registered with `sandbox-ai llm add` and given with --llm: no provider, no key),
with sandbox-ai's demo MCP server (--mcp demo: it runs in the codex image) through sandbox-ai's bridge, and reads what
the run's logs say it reached. Every check keeps its own configuration and state (XDG_CONFIG_HOME, XDG_STATE_HOME: your
llm.json, mcp.json and logs stay untouched; Podman's own configuration is linked in, so its store stays yours).

A check writes what it saw - the answer, the tool call, the hosts the sandbox reached and refused, the telemetry hosts -
as JSON into $SANDBOX_AI_LIVE_REPORT_DIR when it is set (scripts/live_check.py --agents collects them in its report).
"""
import fnmatch
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import urllib.parse

ROOT = Path(__file__).resolve().parents[1]
SAI = ROOT / "sandbox-ai"
PODMAN = shutil.which("podman")
MCP_IMAGE = "localhost/codex-sandbox:latest"  # sandbox-ai's demo MCP server runs in it (mcp.py BUILTIN)
MODEL = "live-model"
LLM = "livecheck"  # the name of the scripted server in the check's own llm.json
PROMPT = ("Call the tool version of the MCP server demo, then answer in one line with what it reported. "
          "This is a check of the sandbox: do nothing else.")
# How each agent runs one task without asking (agent_mcp.command: its one-shot form) and the option of its
# switches (--AGENT-enable); -D gives each its own way of working without questions (the launcher's SKIP_ARGS/ENV).
AGENTS = {
    "hermes": {"args": ["--oneshot=" + PROMPT], "enable": "--hermes-enable", "name": "Hermes Agent"},
    "qwen": {"args": ["--prompt=" + PROMPT], "enable": "--qwen-enable", "name": "Qwen Code"},
    "dsh": {"args": ["headless", PROMPT], "enable": None, "name": "DeepSeek Harness"},
    "goose": {"args": ["run", "--quiet", "--text=" + PROMPT], "enable": "--goose-enable", "name": "Goose"},
}
# What a sandbox may connect to and what the proxy did: the events of connections-*.jsonl that let a connection through.
PASSED = {"tcp", "tls", "tls-passthrough", "http", "upgraded"}
LOOPBACK = {"127.0.0.1", "localhost", "::1", "host.containers.internal"}


def image_exists(image):
    return bool(PODMAN) and subprocess.run([PODMAN, "image", "exists", image], capture_output=True).returncode == 0


def image_of(agent):
    return f"localhost/{agent}-sandbox:latest"


def skip_reason(images, mcp=True):
    """Why the checks cannot run here (no Podman, an image not built), or None. IMAGES: the ones the check starts;
    MCP: it needs the demo MCP server's image too."""
    if not PODMAN:
        return "needs Podman (these agents run in its images; there is no Podman on this machine)"
    wanted = [*images, *([MCP_IMAGE] if mcp else [])]
    missing = [image for image in dict.fromkeys(wanted) if not image_exists(image)]
    if missing:
        builds = " && ".join(f"sandbox-ai build {image.split('/')[1].split('-sandbox')[0]}" for image in missing)
        return f"needs the image(s) {', '.join(missing)}: {builds} (or scripts/live_check.py --agents --build)"
    return None


def load_module(agent):
    spec = importlib.util.spec_from_file_location(f"live_{agent}", ROOT / "sandbox" / f"{agent}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_model_server():
    spec = importlib.util.spec_from_file_location("agent_model_live", ROOT / "tests" / "devtools" / "agent_model.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Live:
    """One check's world: its own configuration and state, the scripted model server (registered as LLM), and the
    sandboxes it starts."""

    def __init__(self, tmp_path, anthropic=False, token=None):
        self.tmp = tmp_path
        config, state = tmp_path / "config", tmp_path / "state"
        config.mkdir()
        state.mkdir()
        real = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "containers"
        if real.is_dir():  # Podman's own configuration (its storage among it) stays the real one
            (config / "containers").symlink_to(real)
        self.env = {**os.environ, "XDG_CONFIG_HOME": str(config), "XDG_STATE_HOME": str(state)}
        self.state = state
        self.model_log = tmp_path / "model.jsonl"
        self.model = load_model_server()
        self.server, self.port = self.model.start(model=MODEL, token=token, log=str(self.model_log))
        added = self.sandbox_ai("llm", "add", LLM, "--kind", "openai-compatible", "--url",
                                f"http://127.0.0.1:{self.port}", "--model", MODEL,
                                *(["--anthropic"] if anthropic else []), timeout=120)
        assert added.returncode == 0, f"llm add: {added.stdout}\n{added.stderr}"

    def sandbox_ai(self, *args, timeout=600, stdin=subprocess.DEVNULL):
        return subprocess.run([str(SAI), *args], env=self.env, capture_output=True, text=True, timeout=timeout,
                              stdin=stdin)

    def start(self, agent, options, args, name, timeout=900):
        """A real start of AGENT (foreground, no tmux, nothing kept, no questions) with OPTIONS and the agent's ARGS;
        (exit status, stdout, stderr, its log directory)."""
        work, logs = self.tmp / f"work-{name}", self.tmp / f"logs-{name}"
        work.mkdir(exist_ok=True)
        command = [str(SAI), agent, "--workspace", str(work), "--no-config", "--no-auth", "--no-tmux", "--no-keep",
                   "--ask", "off", "--log-dir", str(logs), "--name", f"{name}-{os.getpid()}", *options, "--", *args]
        process = subprocess.Popen(command, env=self.env, text=True, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=True)
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=120)
            stderr += f"\n(the check stopped the sandbox after {timeout} s)"
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=120)
        return process.returncode, stdout, stderr, logs

    def plan(self, agent, options):
        """The dry run's plan (JSON) of a start of AGENT with OPTIONS."""
        work = self.tmp / "work-plan"
        work.mkdir(exist_ok=True)
        done = self.sandbox_ai(agent, "--dry-run", "--workspace", str(work), "--no-config", "--no-auth", "--no-tmux",
                               "--no-keep", "--ask", "off", *options, timeout=300)
        assert done.returncode == 0, f"the dry run failed:\n{done.stderr[-3000:]}"
        return json.loads(done.stdout[done.stdout.index("{"):])

    def model_requests(self):
        if not self.model_log.exists():
            return []
        return [json.loads(line) for line in self.model_log.read_text().splitlines() if line.strip()]

    def mcp_calls(self):
        """The tool calls sandbox-ai's MCP gateway recorded for the demo server (its mcp-calls.jsonl)."""
        found = []
        for path in self.state.rglob("mcp-calls.jsonl"):
            for line in path.read_text(errors="replace").splitlines():
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict):
                    found.append(record)
        return found

    def close(self):
        self.sandbox_ai("mcp", "stop", "demo", timeout=300)
        self.server.shutdown()


def records(logs):
    """Every record of a run's logs that says where it went: the proxy's connections-*.jsonl, the firewall's and the
    DNS stub's egress-denied.jsonl, the forwarded lookups of dns.jsonl."""
    found = []
    for pattern in ("connections-*.jsonl", "egress-denied.jsonl", "dns.jsonl"):
        for path in Path(logs).rglob(pattern):
            for line in path.read_text(errors="replace").splitlines():
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict):
                    found.append({**record, "_file": path.name})
    return found


def name_of(record):
    """The host a record names (its host, server name, Host header, URL or destination), lowercase, or None."""
    for key in ("host", "sni", "http_host"):
        if isinstance(record.get(key), str) and record[key]:
            return record[key].lower().rstrip(".").rsplit(":", 1)[0] if record[key].count(":") == 1 else \
                record[key].lower().rstrip(".")
    if isinstance(record.get("url"), str):
        return (urllib.parse.urlsplit(record["url"]).hostname or "").lower() or None
    if isinstance(record.get("destination"), str):
        return record["destination"].rsplit(":", 1)[0].strip("[]")
    return None


def reached_and_refused(logs):
    """({hosts the run reached}, {hosts it was refused}) from its logs (records)."""
    reached, refused = set(), set()
    for record in records(logs):
        name = name_of(record)
        if not name:
            continue
        if record["_file"].startswith("connections-"):
            if record.get("event") == "denied":
                refused.add(name)
            elif record.get("event") in PASSED:
                reached.add(name)
        elif record["_file"] == "egress-denied.jsonl":
            refused.add(name)
        elif record["_file"] == "dns.jsonl" and record.get("forwarded"):
            reached.add(name)
    return reached, refused


def telemetry_in(module, hosts):
    """The hosts of HOSTS that are one of MODULE's telemetry hosts (its TELEMETRY_HOSTS, * as a wildcard)."""
    return sorted(host for host in hosts if any(fnmatch.fnmatch(host, pattern) or host == pattern.lstrip("*.")
                                                for pattern in module.TELEMETRY_HOSTS))


def telemetry_samples(module):
    """A name for each of MODULE's telemetry hosts to look up (a wildcard's: a name below it)."""
    return [pattern.replace("*", "live-check") for pattern in module.TELEMETRY_HOSTS]


def report(name, data):
    """DATA (what a check saw) as $SANDBOX_AI_LIVE_REPORT_DIR/NAME.json, for scripts/live_check.py --agents."""
    directory = os.environ.get("SANDBOX_AI_LIVE_REPORT_DIR")
    if directory:
        Path(directory).mkdir(parents=True, exist_ok=True)
        (Path(directory) / f"{name}.json").write_text(json.dumps(data, indent=2, sort_keys=True, default=str))
    print(f"LIVE-REPORT {name}: {json.dumps(data, sort_keys=True, default=str)[:4000]}")


def resolves(name):
    """Whether NAME has an address for this host (the sandbox's pinned names are looked up here at the start)."""
    try:
        socket.getaddrinfo(name, 443, proto=socket.IPPROTO_TCP)
        return True
    except OSError:
        return False


def lookups_script(names):
    """A shell script for --binary /bin/sh: Python looks each of NAMES up (TCP 443) and prints RESULT dns NAME
    ok|refused."""
    listed = json.dumps(list(names))
    return ("python3 -I - <<'EOF'\nimport socket\n"
            f"for name in {listed}:\n"
            "    try:\n"
            "        socket.getaddrinfo(name, 443, proto=socket.IPPROTO_TCP)\n"
            "        print('RESULT dns', name, 'ok', flush=True)\n"
            "    except OSError as exc:\n"
            "        print('RESULT dns', name, 'refused', type(exc).__name__, flush=True)\n"
            "EOF\n")


def lookups(stdout):
    """{name: "ok" | "refused"} of the RESULT dns lines of lookups_script."""
    found = {}
    for line in stdout.splitlines():
        parts = line.split()
        if parts[:2] == ["RESULT", "dns"] and len(parts) >= 4:
            found[parts[2]] = parts[3]
    return found


def run_report(status, stdout, stderr):
    return f"exit {status}\nSTDOUT:\n{stdout[-6000:]}\nSTDERR:\n{stderr[-6000:]}"


def features_to_check(module):
    """[(feature, its hosts)] of MODULE's switches that a start with no secret and no background program can show:
    those with hosts of their own (a gateway needs its bot token, a background feature starts the agent's own
    server)."""
    return [(name, list(spec["hosts"])) for name, spec in getattr(module, "FEATURES", {}).items()
            if spec.get("hosts") and not spec.get("secrets") and not spec.get("background")
            and not spec.get("gateway")]


def check_agent_answers_with_one_mcp_tool(tmp_path, agent):
    """The agent starts, answers one prompt from the scripted model, calls the demo MCP server's tool version through
    sandbox-ai's bridge (the gateway records it; the model gets the tool's result back), and reaches no telemetry
    host: the run's logs show none reached, the refused hosts are listed."""
    module, spec = load_module(agent), AGENTS[agent]
    live = Live(tmp_path)
    try:
        status, stdout, stderr, logs = live.start(
            agent, ["-D", "--llm", LLM, "--no-provider-defaults", "--mcp", "demo"], spec["args"], f"live-{agent}")
        text = run_report(status, stdout, stderr)
        requests = live.model_requests()
        calls = [call for call in live.mcp_calls() if call.get("event") == "call"]
        reached, refused = reached_and_refused(logs)
        results = [r for r in requests if r.get("tool_result")]
        report(f"{agent}-answer", {
            "agent": agent, "exit": status, "answered": "LIVE-CHECK-ANSWER" in stdout,
            "model_requests": len(requests), "tools_offered": sorted({t for r in requests for t in r.get("tools") or []}),
            "tool_called": [r.get("called") for r in requests if r.get("called")],
            "tool_result": (results[-1]["tool_result"] if results else None),
            "mcp_gateway_calls": [c.get("tool") for c in calls], "reached": sorted(reached),
            "refused": sorted(refused), "telemetry_reached": telemetry_in(module, reached),
            "telemetry_refused": telemetry_in(module, refused)})
        assert status == 0, f"{spec['name']} did not end cleanly\n{text}"
        assert requests, f"{spec['name']} never asked the model (--llm {LLM})\n{text}"
        assert any(r.get("called") for r in requests), \
            f"the model was never offered the demo server's tool version (tools: {requests[0].get('tools')})\n{text}"
        assert results and "sandbox-ai-demo" in results[-1]["tool_result"], \
            f"the tool's result never came back to the model\n{json.dumps(requests, indent=1)[-3000:]}\n{text}"
        assert any(c.get("tool") == "version" for c in calls), \
            f"sandbox-ai's MCP gateway recorded no call of version: {calls}\n{text}"
        assert "LIVE-CHECK-ANSWER" in stdout, f"{spec['name']} did not print the model's answer\n{text}"
        assert not telemetry_in(module, reached), f"telemetry hosts reached: {telemetry_in(module, reached)}"
    finally:
        live.close()


def check_no_switch_opens_nothing(tmp_path, agent):
    """Without a switch the agent's start (--llm only, no provider defaults) plans no host of its own, and in a real
    sandbox its telemetry hosts and an unrelated host do not resolve (DSH, which has no switches)."""
    module = load_module(agent)
    live = Live(tmp_path)
    try:
        options = ["--llm", LLM, "--no-provider-defaults"]
        plan = live.plan(agent, options)
        assert not plan["https_hosts"] and not plan.get("host_wildcards"), \
            f"a start without switches plans hosts: {plan['https_hosts']} {plan.get('host_wildcards')}"
        names = [*telemetry_samples(module), "example.com"]
        status, stdout, stderr, logs = live.start(agent, [*options, "--binary", "/bin/sh"],
                                                  ["-c", lookups_script(names)], f"live-{agent}-closed")
        found = lookups(stdout)
        _, refused = reached_and_refused(logs)
        report(f"{agent}-closed", {"agent": agent, "lookups": found, "refused": sorted(refused)})
        assert status == 0 and found, f"the probe did not run\n{run_report(status, stdout, stderr)}"
        assert all(found.get(name) == "refused" for name in names), f"opened: {found}"
    finally:
        live.close()


def check_switch_opens_only_its_hosts(tmp_path, agent, feature, hosts):
    """--AGENT-enable FEATURE opens exactly FEATURE's hosts: the plan (its dry run) names exactly them, and in a real
    sandbox they resolve while the other switches' hosts, the telemetry hosts and an unrelated host do not."""
    module, spec = load_module(agent), AGENTS[agent]
    live = Live(tmp_path)
    try:
        options = [spec["enable"], feature, "--llm", LLM, "--no-provider-defaults"]
        plan = live.plan(agent, options)
        assert set(plan["https_hosts"]) == set(hosts) and not plan.get("host_wildcards"), \
            f"--{agent}-enable {feature} plans {plan['https_hosts']} {plan.get('host_wildcards')}, not {hosts}"
        others = sorted({h for _, other in features_to_check(module) for h in other} - set(hosts))
        unrelated = ["example.com"]
        names = [*hosts, *others, *telemetry_samples(module), *unrelated]
        status, stdout, stderr, logs = live.start(agent, [*options, "--binary", "/bin/sh"],
                                                  ["-c", lookups_script(names)], f"live-{agent}-{feature}")
        text = run_report(status, stdout, stderr)
        found = lookups(stdout)
        _, refused = reached_and_refused(logs)
        # A host that has no address out there (on this host either) cannot be pinned: said, not held against it.
        gone = [host for host in hosts if not resolves(host)]
        report(f"{agent}-switch-{feature.replace(':', '-')}", {
            "agent": agent, "feature": feature, "hosts": hosts, "lookups": found, "refused": sorted(refused),
            "no_address_on_this_host": gone})
        assert status == 0 and found, f"the probe did not run\n{text}"
        closed = [host for host in hosts if host not in gone and found.get(host) != "ok"]
        assert not closed, f"{feature}'s own hosts not open: {closed} ({found})\n{text}"
        opened = [name for name in [*others, *telemetry_samples(module), *unrelated] if found.get(name) != "refused"]
        assert not opened, f"--{agent}-enable {feature} opened more than its hosts: {opened}\n{text}"
    finally:
        live.close()
