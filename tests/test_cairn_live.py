"""Cairn as a part of its own (--cairn: sandbox/cairn.py, docs/CAIRN_REVIEW.md) in a real pod: its container's
server answers on the pod's loopback only (127.0.0.1:8000), the start's project is on its board, the relay's socket
is in the agent's container, and its dispatcher runs a task through the relay - the session's own claude CLI as its
worker, which asks the scripted model on this host (--llm with its Anthropic Messages API: no provider, no key).
Cairn itself reaches nothing: the run's logs show no host but the model's, and no telemetry host.

Needs Podman and the images localhost/sandbox-ai-cairn and localhost/sandbox-ai-browser (sandbox-ai build claude,
sandbox-ai build cairn, sandbox-ai build browser). Run: python -m pytest -v --agent-live tests/test_cairn_live.py (CI:
the job "agents-live", cairn; on your machine: scripts/live_check_agents.sh cairn). tests/live_agents.py has what the
checks share."""
import json

import pytest

import live_agents as L

IMAGES = ["localhost/claude-sandbox:latest", "localhost/sandbox-ai-cairn:latest", "localhost/sandbox-ai-browser:latest"]
SKIP = L.skip_reason(IMAGES, mcp=False)
TOKEN = "LIVEORIGIN7731"

pytestmark = [pytest.mark.agent_live, pytest.mark.skipif(SKIP is not None, reason=SKIP or "")]

SCRIPT = r"""
set -u
python3 -I - <<'EOF'
import json, os, time, urllib.request
base = "http://127.0.0.1:8000"
for _ in range(120):
    try:
        with urllib.request.urlopen(base + "/", timeout=5) as answer:
            print("RESULT serve", answer.status, flush=True)
            break
    except OSError:
        time.sleep(1)
else:
    print("RESULT serve none", flush=True)
listening = set()
for table in ("/proc/net/tcp", "/proc/net/tcp6"):
    try:
        lines = open(table).read().splitlines()[1:]
    except OSError:
        continue
    for line in lines:
        fields = line.split()
        if fields[3] == "0A" and int(fields[1].split(":")[1], 16) == 8000:
            listening.add(fields[1].split(":")[0])
print("RESULT listen", ",".join(sorted(listening)) or "none", flush=True)
print("RESULT relay", "socket" if os.path.exists("/sandbox/cairn/relay.sock") else "none", flush=True)
try:
    with urllib.request.urlopen(base + "/projects", timeout=30) as answer:
        print("RESULT projects", json.dumps([p.get("title") for p in json.loads(answer.read())]), flush=True)
except OSError as exc:
    print("RESULT projects failed", exc, flush=True)
seen = {}
for _ in range(240):
    try:
        with urllib.request.urlopen("http://127.0.0.1:__PORT__/live/seen", timeout=5) as answer:
            seen = json.loads(answer.read())
    except OSError as exc:
        seen = {"error": str(exc)}
    if seen.get("messages"):
        break
    time.sleep(1)
time.sleep(5)
print("RESULT seen", json.dumps(seen), flush=True)
print("RESULT runs", ",".join(sorted(os.listdir("/work/cairn/runs"))) if os.path.isdir("/work/cairn/runs") else "none",
      flush=True)
EOF
exit 0
"""


def results(stdout):
    found = {}
    for line in stdout.splitlines():
        if line.startswith("RESULT "):
            _, name, value = (line.split(" ", 2) + [""])[:3]
            found[name] = value
    return found


def test_cairns_part_serves_on_loopback_and_runs_one_task_through_the_relay(tmp_path):
    live = L.Live(tmp_path, anthropic=True, token=TOKEN)
    try:
        script = SCRIPT.replace("__PORT__", str(live.port))
        status, stdout, stderr, logs = live.start(
            "claude", ["--cairn", "--cairn-origin", f"The machine {TOKEN}", "--cairn-goal", f"Describe {TOKEN}",
                       "--cairn-hint", f"Look at {TOKEN}", "--llm", L.LLM, "--no-provider-defaults",
                       "--binary", "/bin/bash"],
            ["-c", script], "live-cairn", timeout=900)
        text = L.run_report(status, stdout, stderr)
        found = results(stdout)
        requests = live.model_requests()
        reached, refused = L.reached_and_refused(logs)
        seen = json.loads(found.get("seen") or "{}") if found.get("seen", "").startswith("{") else {}
        L.report("cairn", {"exit": status, "serve": found.get("serve"), "listen": found.get("listen"),
                           "relay": found.get("relay"), "projects": found.get("projects"), "seen": seen,
                           "runs": found.get("runs"), "model_requests": len(requests),
                           "token_in_a_prompt": any(r.get("token") for r in requests), "reached": sorted(reached),
                           "refused": sorted(refused)})
        assert status == 0, text
        assert found.get("serve") == "200", f"Cairn's server did not answer on 127.0.0.1:8000\n{text}"
        # 0100007F: 127.0.0.1 in /proc/net/tcp (and its IPv6 forms): never 0.0.0.0 (00000000) or ::.
        addresses = [a for a in found.get("listen", "").split(",") if a]
        assert addresses and all(a in ("0100007F", "00000000000000000000000001000000") for a in addresses), \
            f"Cairn's server listens beyond the loopback: {addresses}\n{text}"
        assert found.get("relay") == "socket", f"no relay in the agent's container\n{text}"
        assert f"sandbox-ai: The machine {TOKEN}" in found.get("projects", ""), f"the start's project\n{text}"
        assert seen.get("messages"), f"the dispatcher's worker never asked the model: no task ran\n{text}"
        assert any(r.get("path", "").split("?")[0].endswith("/messages") for r in requests), text
        assert found.get("runs", "none") != "none", f"the worker ran in no project folder of /work\n{text}"
        # (a name, not the address the model's relay is logged with)
        outside = sorted(host for host in reached if host not in L.LOOPBACK and any(c.isalpha() for c in host)
                         and ":" not in host)
        assert not outside, f"the run reached hosts besides the model: {outside}\n{text}"
    finally:
        live.close()
