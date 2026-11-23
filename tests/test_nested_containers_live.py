"""--nested-containers in a real sandbox: Podman inside the agent's sandbox runs a container, unshare -rn works, and the
containers' connections meet the sandbox's own firewall and connection proxy.

A destination that answers whenever it is reachable - tests/devtools/fake_internet.py on this host's own address, one
port per attempt - must stay unreached for the agent itself, a container on the agent's network, a container as user
1 (the proxy's ID outside), a container behind slirp4netns and one behind pasta; each attempt's port must appear in the
proxy's connection log (the connection went to the proxy, not past it) and no request may arrive at its server. Only
the client's own "no" counts as refused: one that crashed, hung or was not found is "broken" and fails the test. This
test reaches each server itself first: they do answer. slirp4netns must work (the dev image has it); pasta only where
the Podman inside supports it as root (4.4 and later: Debian bookworm's 4.3 has no pasta mode), else the test fails.
The agent's loopback is the containers' (they share its network namespace: a server of the agent's on 127.0.0.1,
which the agent itself reaches first, answers a container too, and both are in the same namespace), a container's
/dev/null can be written, and none of the reserved IDs (the proxy's, PulseAudio's, tor's) is mapped.

Needs Podman, /dev/net/tun and the dev image variant (sandbox-ai build claude; sandbox-ai images recipe dev --from
claude; sandbox-ai build dev), or SANDBOX_AI_NESTED_IMAGE naming an image with Podman and busybox-static; skipped
without. Run: python -m pytest -v --agent-live tests/test_nested_containers_live.py (CI: the job "nested")."""
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import urllib.request

import pytest

import ci_annotations

ROOT = Path(__file__).resolve().parents[1]
PODMAN = shutil.which("podman")
IMAGE = os.environ.get("SANDBOX_AI_NESTED_IMAGE", "localhost/claude-sandbox-dev:latest")
RESERVED = (1, 2, 950)  # the proxy's, PulseAudio's and tor's IDs
ATTEMPTS = ("agent", "shared", "user1", "slirp4netns", "pasta")


def image_ready():
    if not PODMAN or not os.path.exists("/dev/net/tun"):
        return False
    return subprocess.run([PODMAN, "image", "exists", IMAGE], capture_output=True).returncode == 0


pytestmark = [pytest.mark.agent_live,
              pytest.mark.skipif(not image_ready(), reason=f"needs Podman, /dev/net/tun and the image {IMAGE} "
                                                           "(sandbox-ai images recipe dev; sandbox-ai build dev)")]


def load_fake_internet():
    spec = importlib.util.spec_from_file_location("fake_internet_live", ROOT / "tests" / "devtools" / "fake_internet.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def host_address():
    """This host's own address on its default route: what the sandbox would reach it by, past the firewall."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect(("192.0.2.1", 9))  # no packet is sent: only the route is looked up
        return probe.getsockname()[0]


# Runs as the agent (sh): every step reports "RESULT name value"; nothing stops the script early. HOST and the PORT_*
# placeholders are filled in by the test.
SCRIPT = r"""
result() { echo "RESULT $1 $2"; }
attempt() {  # NAME, then the command that fetches the page: reached, refused (wget's own "no": exit status 1,
  # NAME_said its words), or broken:STATUS - no answer at all: a client that crashed (139) or was not found, one
  # that ran into its timeout (143), a Podman that failed (125). Only "refused" says anything of the firewall.
  name=$1; shift
  "$@" >/tmp/$name.out 2>/tmp/$name.err; status=$?
  if [ $status -eq 0 ] && grep -q '"method": "GET"' /tmp/$name.out; then
    result "$name" reached
  elif [ $status -le 1 ]; then
    result "$name" refused
  else
    result "$name" "broken:$status"
  fi
  result "${name}_said" "$(tail -c 200 /tmp/$name.err | tr '\n' ' ')"
}
python3 -I -c 'import sys; print("RESULT uid_map", sys.argv[1].replace("\n", ";"))' "$(cat /proc/self/uid_map)"
python3 -I -c 'import sys; print("RESULT gid_map", sys.argv[1].replace("\n", ";"))' "$(cat /proc/self/gid_map)"
result podman_version "$(podman version --format '{{.Client.Version}}' 2>/dev/null || echo unknown)"
# What the Podman inside thinks it is (rootful in the agent's namespace), and where it keeps what; an error in full.
result podman_info "$(podman info --format '{{.Host.Security.Rootless}} {{.Store.GraphDriverName}} {{.Store.GraphRoot}} {{.Host.NetworkBackend}}' 2>&1 | tr '\n' ' ' | head -c 600)"
result caps "$(grep -E '^Cap(Eff|Bnd)' /proc/self/status | tr '\n' ' ')"
result sys "$(ls /sys/fs 2>&1 | tr '\n' ' ') $(stat -f -c %T /sys/fs/cgroup 2>&1)"
# Podman's own account of its start, its last 30 lines (they name a missing file, a refused mount).
result podman_debug "$(podman --log-level=debug info 2>&1 | tail -n 30 | tr '\n' '|' | cut -c1-6000)"
unshare -rn true && result unshare ok || result unshare failed
unshare -ri true && result unshare_ipc ok || result unshare_ipc failed
# The agent's IPC namespace (the guard's own, else it said "no IPC namespace of its own") and its /dev/mqueue.
result ipc_ns "$(readlink /proc/self/ns/ipc)"
result mqueue "$(grep -E ' /dev/mqueue ' /proc/self/mountinfo | tail -n 1 | cut -c1-200)"
# A small local image: busybox-static alone, imported from a file (nothing is pulled, no pipe to break).
mkdir -p /tmp/rootfs/bin /tmp/rootfs/tmp && cp /bin/busybox /tmp/rootfs/bin/busybox \
  && tar -C /tmp/rootfs -cf /tmp/rootfs.tar . 2>/tmp/import.err \
  && podman import -q /tmp/rootfs.tar localhost/nested-probe:latest >/dev/null 2>>/tmp/import.err \
  && result import ok || result import "failed:$(tail -c 1500 /tmp/import.err | tr '\n' ' ')"
IMG=localhost/nested-probe:latest
podman run --rm $IMG /bin/busybox echo hello >/tmp/run.out 2>/tmp/run.err \
  && result run "$(cat /tmp/run.out)" || result run "failed:$(tail -c 400 /tmp/run.err | tr '\n' ' ')"
# The client of every request, the agent's and the containers': the image's static busybox. Not its wget's -T:
# Debian's busybox-static is built without wget's timeout, and its option parser writes -T's number through a null
# pointer - the client died of it before it connected, in the agent and in every container (CI: "Segmentation
# fault", and "refused" for all of them). busybox's own timeout ends a client that hangs.
WGET="/bin/busybox timeout 20 /bin/busybox wget -q -O -"
# The agent's loopback is the containers' (they share its network namespace): a server of the agent's own there,
# asked by the agent itself first (the control: until it answers, 20 s at most - what is not there, no container
# reaches), then from a container. What each says when it fails is kept (CI's first run this far said "failed",
# no more), and so is whose network namespace a container is in, what listens there and what the server saw.
python3 -I -m http.server 8765 --bind 127.0.0.1 --directory /tmp/rootfs >/tmp/http.log 2>&1 &
HTTP=$!
tries=0
until $WGET http://127.0.0.1:8765/bin/ >/dev/null 2>/tmp/loopback_agent.err; do
  status=$?
  [ $status -ne 1 ] && { tries=20; break; }  # not wget's "no": the client itself failed - waiting changes nothing
  tries=$((tries + 1)); [ $tries -ge 20 ] && break; sleep 1
done
[ $tries -lt 20 ] && result loopback_agent ok \
  || result loopback_agent "failed:exit $status: $(tail -c 300 /tmp/loopback_agent.err | tr '\n' ' ') server: $(tail -c 300 /tmp/http.log | tr '\n' ' ')"
podman run --rm $IMG $WGET http://127.0.0.1:8765/bin/ >/tmp/loopback.out 2>/tmp/loopback.err; status=$?
[ $status -eq 0 ] && result loopback ok \
  || result loopback "failed:exit $status: $(tail -c 400 /tmp/loopback.err | tr '\n' ' ')"
result netns "$(readlink /proc/self/ns/net) $(podman run --rm $IMG /bin/busybox readlink /proc/self/ns/net 2>&1 | tail -n 1 | cut -c1-200)"
result listening "$(podman run --rm $IMG /bin/busybox netstat -ltn 2>&1 | tr '\n' ';' | cut -c1-500)"
result addresses "$(podman run --rm $IMG /bin/busybox ip -o addr 2>&1 | cut -c1-90 | tr '\n' ';' | cut -c1-500)"
result served "$(grep -c 'GET /bin/' /tmp/http.log) $(tail -n 3 /tmp/http.log | tr '\n' '|' | cut -c1-300)"
result server "$(kill -0 $HTTP 2>/dev/null && echo alive || echo gone) after the containers"
# What Podman itself says of a container's network as it starts one (its debug lines that name it).
result run_debug "$(podman --log-level=debug run --rm $IMG /bin/busybox true 2>&1 | grep -i -E 'netns|network|slirp|cni' | tail -n 12 | tr '\n' '|' | cut -c1-2500)"
# A container's /dev/null is one it can write (crun binds the agent's own into it).
podman run --rm $IMG /bin/busybox sh -c 'echo written >/dev/null' >/dev/null 2>/tmp/devnull.err \
  && result devnull ok || result devnull "failed:$(tail -c 300 /tmp/devnull.err | tr '\n' ' ')"
# A destination that answers when it is reached, one port per attempt: refused for every one of them.
attempt agent $WGET http://HOST:PORT_agent/agent
attempt shared podman run --rm $IMG $WGET http://HOST:PORT_shared/shared
attempt user1 podman run --rm --user 1:1 $IMG $WGET http://HOST:PORT_user1/user1
for mode in slirp4netns pasta; do
  if podman run --rm --network $mode $IMG /bin/busybox true 2>/tmp/$mode-start.err; then
    case $mode in slirp4netns) port=PORT_slirp4netns ;; pasta) port=PORT_pasta ;; esac
    attempt $mode podman run --rm --network $mode $IMG $WGET http://HOST:$port/$mode
  else
    result $mode "unavailable:$(tail -c 300 /tmp/$mode-start.err | tr '\n' ' ')"
  fi
done
kill $HTTP 2>/dev/null
exit 0
"""


def results(stdout):
    found = {}
    for line in stdout.splitlines():
        if line.startswith("RESULT "):
            _, name, value = (line.split(" ", 2) + [""])[:3]
            found[name] = value
    return found


KNOWN_LIMITATION = "known limitation of this runner"


def known_limitation(found):
    """The skip reason when the container run failed only because crun may not mount mqueue - although the agent has
    an IPC namespace of its own (found["ipc"] "own": sandbox-ai did its part, runtime.nested_ipc) - else None. The
    kernel lets only a holder of CAP_SYS_ADMIN in the IPC namespace's user namespace mount it; where it still refuses
    (GitHub's runner: Ubuntu's kernel and AppArmor, Debian's Podman 4.3.1 inside), sandbox-ai cannot do more without
    weakening the sandbox. Every other failure stays a failure."""
    run = found.get("run", "")
    if found.get("ipc") != "own" or not run.startswith("failed:"):
        return None
    if "mqueue" in run and "Operation not permitted" in run:
        return (f"{KNOWN_LIMITATION}: crun may not mount mqueue in the sandbox here ({run[len('failed:'):][:200]}); "
                "this runner cannot run containers inside the sandbox")
    return None


def loopback_problem(found):
    """Why the containers do not have the agent's loopback, in words - or None when a container reached the agent's
    server there. Told apart, because each has another remedy: the test's own server never answered (nothing is
    known of the containers); the containers are in another network namespace than the agent (the drop-in's
    netns = "host" was not taken: they are not behind the sandbox's firewall and proxy as the agent is, whatever
    else passes); or they share it and the connection still failed (what the container's wget said)."""
    agent, container = found.get("loopback_agent"), found.get("loopback")
    if agent != "ok" and str(agent).startswith("failed:exit ") and not str(agent).startswith("failed:exit 1:"):
        return (f"the test's own client crashed or could not run (the agent's own request at its own server: "
                f"{agent!r}; a container's: {container!r}): nothing is known of the server or the containers")
    if agent != "ok":
        return (f"the test's own server on the agent's loopback did not answer the agent itself ({agent!r}): "
                f"nothing is known of the containers (a container's attempt: {container!r})")
    own, _, inside = found.get("netns", "").partition(" ")
    if own.startswith("net:[") and inside.startswith("net:[") and own != inside:
        return (f"the containers do not share the agent's network namespace (the agent {own}, a container {inside}) "
                f"although sandbox-ai's containers.conf says netns = \"host\": Podman "
                f"{found.get('podman_version', 'unknown')} inside did not take it; a container's attempt at the "
                f"agent's loopback: {container!r}")
    if container != "ok":
        shared = "in the agent's network namespace" if own and own == inside else \
            f"(its network namespace: {inside or 'not read'}; the agent's: {own or 'not read'})"
        return (f"a container {shared} did not reach the agent's server on 127.0.0.1: {container!r}; the server "
                f"saw: {found.get('served')!r} and is {found.get('server')!r}; listening for the container: "
                f"{found.get('listening')!r}")
    return None


def broken_attempts(found):
    """{attempt: its result} of the attempts at the outside that gave no answer at all ("broken:STATUS": the client
    crashed or was not found, ran into its timeout, or Podman failed) - they say nothing of the firewall."""
    return {name: found[name] for name in ATTEMPTS if str(found.get(name, "")).startswith("broken:")}


def account(code, found, stderr, lines=50):
    """What a failure says (it becomes CI's annotation: what matters first, in a few KB): the exit status, every
    result but Podman's long debug account, the last LINES lines of the start's standard error, then that account.
    Everything in full goes to the report (ci_annotations.report: "nested")."""
    brief = {name: value for name, value in found.items() if name not in ("podman_debug", "run_debug")}
    debug = "\n".join(found.get(name, "").replace("|", "\n").strip() for name in ("run_debug", "podman_debug"))
    return (f"exit {code}\nRESULTS:\n{json.dumps(brief, indent=1)}\n"
            f"STDERR (its last {lines} lines; all of it in the report):\n"
            + "\n".join(stderr.splitlines()[-lines:]) + f"\nPODMAN'S DEBUG ACCOUNT:\n{debug.strip()}")


def covered_outside(text):
    """IDs of the parent namespace a uid_map/gid_map (lines joined by ;) covers."""
    found = set()
    for line in text.split(";"):
        if line.split():
            inside, outside, count = (int(v) for v in line.split())
            found.update(range(outside, outside + count))
    return found


def pasta_supported(version):
    """Whether the Podman inside has a pasta network mode as root (4.4 and later)."""
    match = re.match(r"(\d+)\.(\d+)", version or "")
    return bool(match) and (int(match.group(1)), int(match.group(2))) >= (4, 4)


def test_podman_inside_the_sandbox_stays_behind_its_firewall_and_proxy(tmp_path):
    fi = load_fake_internet()
    address = host_address()
    assert not address.startswith("127."), "this host has no address of its own to test with"
    servers, logs_of = {}, {}
    for name in ATTEMPTS:
        logs_of[name] = tmp_path / f"server-{name}.jsonl"
        servers[name] = fi.start(bind="0.0.0.0", http=0, log=str(logs_of[name]))
    try:
        for name, started in servers.items():  # they answer whoever reaches them: this test does
            with urllib.request.urlopen(f"http://{address}:{started['http']}/control", timeout=10) as answer:
                assert answer.status == 200 and json.loads(answer.read())["path"] == "/control"
            logs_of[name].unlink()
        script = SCRIPT.replace("HOST", address)
        for name, started in servers.items():
            script = script.replace(f"PORT_{name}", str(started["http"]))
        work, logs = tmp_path / "work", tmp_path / "logs"
        work.mkdir()
        command = [str(ROOT / "sandbox-ai"), "claude", "--image", IMAGE, "--workspace", str(work), "--no-config",
                   "--no-auth", "--no-tmux", "--ask", "off", "--log-dir", str(logs), "--nested-containers",
                   "--binary", "/bin/sh", "--", "-c", script]
        environment = dict(os.environ)  # no opt-in: choosing --nested-containers starts it
        process = subprocess.Popen(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=True, env=environment)
        try:
            stdout, stderr = process.communicate(timeout=900)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=60)
            ci_annotations.report("nested", ci_annotations.sections({
                "results (the sandbox timed out)": json.dumps(results(stdout), indent=1), "standard error": stderr,
                "standard output": stdout}))
            pytest.fail("the nested sandbox timed out\n" + account("none (timed out)", results(stdout), stderr))
    finally:
        for started in servers.values():
            started["stop"]()
    found = results(stdout)
    found["ipc"] = "shared" if "no IPC namespace of its own" in stderr else "own"
    (tmp_path / "stdout.txt").write_text(stdout)
    (tmp_path / "stderr.txt").write_text(stderr)
    logged = "\n".join(p.read_text(errors="replace") for p in logs.rglob("connections-*.jsonl"))
    # A failure says what matters first (CI's annotation of it); everything in full is the report beside it.
    report = account(process.returncode, found, stderr)
    ci_annotations.report("nested", ci_annotations.sections({
        "exit": str(process.returncode), "results": json.dumps(found, indent=1), "standard error": stderr,
        "standard output": stdout, "the proxy's connection log (its end)": logged[-4000:]}))
    if process.returncode != 0:
        pytest.fail(f"the nested sandbox ended with {process.returncode}\n{report}")
    # The agent's own user namespace: root is the sandbox's root, the reserved IDs are mapped nowhere.
    for key in ("uid_map", "gid_map"):
        outside = covered_outside(found.get(key, ""))
        if not (0 in outside and not outside & set(RESERVED)):
            pytest.fail(f"{key}: root not mapped or a reserved ID mapped\n{report}")
    limitation = known_limitation(found)
    if limitation:
        # One line (CI's annotation takes the SKIPPED line), with what tells the next step apart.
        seen = {key: found.get(key) for key in ("podman_version", "podman_info", "caps", "unshare_ipc", "ipc_ns",
                                               "mqueue")}
        pytest.skip(" ".join(f"{limitation} {json.dumps(seen)}".split()))
    for name, wanted in (("unshare", "ok"), ("import", "ok"), ("run", "hello")):
        if found.get(name) != wanted:
            pytest.fail(f"{name}: {found.get(name)!r}, not {wanted!r}\n{report}")
    problem = loopback_problem(found)
    if problem:
        pytest.fail(f"loopback: {problem}\n{report}")
    if found.get("devnull") != "ok":
        pytest.fail(f"devnull: a container cannot write its /dev/null: {found.get('devnull')!r}\n{report}")
    required = ["agent", "shared", "user1", "slirp4netns"]
    if pasta_supported(found.get("podman_version")):
        required.append("pasta")
    elif not found.get("pasta", "").startswith("unavailable"):
        required.append("pasta")  # it ran after all: then it must be refused like the others
    for name in required:
        if found.get(name) != "refused":  # "unavailable" and "broken:STATUS" fail here too
            why = " - the client or Podman failed, the firewall was not asked" if name in broken_attempts(found) else ""
            pytest.fail(f"{name}: {found.get(name)!r}, not refused{why} (it said: {found.get(name + '_said')!r})\n"
                        f"{report}")
    for name in required:
        port = servers[name]["http"]
        if f"{address}:{port}" not in logged:
            pytest.fail(f"{name}: port {port} is not in the proxy's log\n{logged[-3000:]}\n{report}")
        if logs_of[name].exists() and logs_of[name].read_text().strip():
            pytest.fail(f"{name}: a request reached the server past the firewall: {logs_of[name].read_text()}")
