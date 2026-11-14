"""Cairn (the image localhost/cairn-sandbox: docs/CAIRN_REVIEW.md) in a real sandbox: its server answers on
the sandbox's loopback only (127.0.0.1:8000), a project is created through its API, and its dispatcher runs a task in
local execution mode - it starts this sandbox's own claude CLI as its worker, which asks the scripted model on this host
(--llm with its Anthropic Messages API: no provider, no key). Cairn itself reaches nothing: the run's logs show no host
but the model's, and no telemetry host.

Needs Podman and the image localhost/cairn-sandbox (sandbox-ai build team, then sandbox-ai build cairn). Run:
python -m pytest -v --agent-live tests/test_cairn_live.py (CI: the job "agents-live", cairn; on your machine:
scripts/live_check_agents.sh cairn). tests/live_agents.py has what the checks share."""
import json

import pytest

import live_agents as L

IMAGE = "localhost/cairn-sandbox:latest"
SKIP = L.skip_reason([IMAGE], mcp=False)
TOKEN = "LIVEORIGIN7731"

pytestmark = [pytest.mark.agent_live, pytest.mark.skipif(SKIP is not None, reason=SKIP or "")]

# Only Claude Code as the worker (no Codex login here), one task at a time, short rounds; local execution only.
DISPATCH = """server: "http://127.0.0.1:8000"
runtime:
  execution: local
  worker_healthcheck: disabled
  max_workers: 1
  max_running_projects: 1
  max_project_workers: 1
  interval: 2
  healthcheck_timeout: 15
  prompt_group: "default"
tasks:
  bootstrap:
    timeout: 120
    conclude_timeout: 30
  reason:
    timeout: 60
  explore:
    timeout: 120
    conclude_timeout: 30
local:
  workspace_root: "/work/cairn/runs"
  completed_action: keep
workers:
  - name: "sandbox-claude"
    type: "claudecode"
    task_types: [bootstrap, reason, explore]
    max_running: 1
    priority: 0
"""

# (No -D: with --binary the launcher would hand bash Claude Code's --dangerously-skip-permissions. Cairn gives its
# worker that flag itself; IS_SANDBOX=1 is what -D sets for Claude Code in a sandbox.)
SCRIPT = r"""
set -u
export IS_SANDBOX=1
mkdir -p /work/cairn && cd /work/cairn
cairn serve > /work/cairn/serve.log 2>&1 &
SERVE=$!
python3 -I - <<'EOF'
import json, time, urllib.request
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
token = "__TOKEN__"
body = json.dumps({"title": "live check " + token, "origin": "The machine " + token,
                   "goal": "Describe " + token, "hints": [{"content": "Look at " + token, "creator": "human"}]})
request = urllib.request.Request(base + "/projects", data=body.encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
try:
    with urllib.request.urlopen(request, timeout=30) as answer:
        print("RESULT project", answer.status, answer.read()[:300].decode(errors="replace").replace("\n", " "),
              flush=True)
except OSError as exc:
    print("RESULT project failed", exc, flush=True)
EOF
cat > /work/cairn/dispatch.yaml <<'EOF'
__DISPATCH__EOF
cairn dispatch --config /work/cairn/dispatch.yaml > /work/cairn/dispatch.log 2>&1 &
DISPATCH=$!
python3 -I - <<'EOF'
import json, time, urllib.request
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
EOF
kill "$DISPATCH" "$SERVE" 2>/dev/null
sleep 2
echo "RESULT dispatcher-ran $(grep -c . /work/cairn/dispatch.log)"
echo "--- dispatch.log"; tail -n 80 /work/cairn/dispatch.log
echo "--- serve.log"; tail -n 20 /work/cairn/serve.log
exit 0
"""


def results(stdout):
    found = {}
    for line in stdout.splitlines():
        if line.startswith("RESULT "):
            _, name, value = (line.split(" ", 2) + [""])[:3]
            found[name] = value
    return found


def test_cairns_server_on_loopback_and_one_task_in_local_mode(tmp_path):
    live = L.Live(tmp_path, anthropic=True, token=TOKEN)
    try:
        script = SCRIPT.replace("__TOKEN__", TOKEN).replace("__PORT__", str(live.port)).replace("__DISPATCH__",
                                                                                                DISPATCH)
        status, stdout, stderr, logs = live.start(
            "claude", ["--image", IMAGE, "--llm", L.LLM, "--no-provider-defaults", "--binary", "/bin/bash"],
            ["-c", script], "live-cairn", timeout=900)
        text = L.run_report(status, stdout, stderr)
        found = results(stdout)
        requests = live.model_requests()
        reached, refused = L.reached_and_refused(logs)
        seen = json.loads(found.get("seen") or "{}") if found.get("seen", "").startswith("{") else {}
        L.report("cairn", {"exit": status, "serve": found.get("serve"), "listen": found.get("listen"),
                           "project": found.get("project"), "seen": seen, "model_requests": len(requests),
                           "token_in_a_prompt": any(r.get("token") for r in requests), "reached": sorted(reached),
                           "refused": sorted(refused)})
        assert status == 0, text
        assert found.get("serve") == "200", f"Cairn's server did not answer on 127.0.0.1:8000\n{text}"
        # 0100007F: 127.0.0.1 in /proc/net/tcp (and its IPv6 forms): never 0.0.0.0 (00000000) or ::.
        addresses = [a for a in found.get("listen", "").split(",") if a]
        assert addresses and all(a in ("0100007F", "00000000000000000000000001000000") for a in addresses), \
            f"Cairn's server listens beyond the loopback: {addresses}\n{text}"
        assert found.get("project", "").startswith("200") or found.get("project", "").startswith("201"), \
            f"no project through Cairn's API\n{text}"
        assert seen.get("messages"), f"the dispatcher's worker never asked the model: no task ran\n{text}"
        assert any(r.get("path", "").split("?")[0].endswith("/messages") for r in requests), text
        # (a name, not the address the model's relay is logged with)
        outside = sorted(host for host in reached if host not in L.LOOPBACK and any(c.isalpha() for c in host)
                         and ":" not in host)
        assert not outside, f"the run reached hosts besides the model: {outside}\n{text}"
    finally:
        live.close()
