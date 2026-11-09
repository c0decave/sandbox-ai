"""Whether a sandbox still runs when its launcher is gone, and what is left of one that does not.

A run is registered by its launcher (agent_sandbox.py, "the supervisor" on the host): run.json in the run directory
($XDG_RUNTIME_DIR/sandbox-ai/runs/RUN) names its PID. With Podman the sandbox itself is a pod (agent-RUN) that
Podman's conmon keeps; it lives on when the launcher is killed without its cleanup (SIGKILL, the OOM killer, a cgroup
that was stopped). Such a run is "orphaned": its container runs, its control socket has nobody behind it on the
host. Its run directory must stay (it is bound into the sandbox), and it is ended with `sandbox-ai stop NAME`, which
stops its pod, records the end and removes what is left, so that a kept start can be resumed.

A run counts as ended only on positive evidence: its launcher's PID is gone (or another process has it now) where
this process can see the launcher's processes, and, with Podman, no container of its pod runs any more (Podman
answered). Inside a sandbox or container the host's processes are not visible: nothing is decided there.

Standard library only.
"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

PODMAN_TIMEOUT = 20
CACHE_SECONDS = 2.0
POD_NAME = re.compile(r"agent-[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,80}")
# sandbox-ai's own pods: agent-AGENT-HEX (a run, a session's run, a VPN's pod) and agent-mcp-NAME (an MCP server);
# newer ones also carry RUN_LABEL. Nothing else is ever removed as a leftover.
OWN_POD = re.compile(r"agent-(?:(?:claude|codex|opencode)-[0-9a-f]{8,12}|mcp-[A-Za-z0-9_-]{1,32})")
RUN_LABEL = "sandbox-ai.run"
SESSION_LABEL = "sandbox-ai.session"
# Container states in which a sandbox still lives (a paused pod too: `sandbox-ai pause`).
LIVE_STATES = ("running", "paused", "stopping")
# Pod states a leftover may be removed in: nothing of it runs. Never "Created" (a pod a starting launcher made a
# moment ago), "Running", "Degraded" or "Paused".
ENDED_POD_STATES = ("Exited", "Stopped", "Error", "Dead")
RUNNER = subprocess.run
_cache = {}


def podman(url, *args):
    return ["podman", *(["--remote", "--url", url] if url else []), *args]


def inside_sandbox(environ=None, root=Path("/")):
    """This process runs inside a sandbox or a container (sandbox-ai's own: $SANDBOX_AI_RUN; Podman's
    /run/.containerenv, Docker's /.dockerenv): the host's processes are not visible from there."""
    environ = os.environ if environ is None else environ
    return bool(environ.get("SANDBOX_AI_RUN")) or (Path(root) / "run/.containerenv").exists() or \
        (Path(root) / ".dockerenv").exists()


def pid_namespace(proc=Path("/proc")):
    """The PID namespace of this process ("pid:[4026531836]"), or None (no /proc: macOS)."""
    try:
        return os.readlink(Path(proc) / "self/ns/pid")
    except OSError:
        return None


def same_namespace(recorded):
    """RECORDED (a "pid_ns" a launcher wrote) is this process's PID namespace, or was not recorded (older versions)."""
    return not (isinstance(recorded, str) and recorded and recorded != pid_namespace())


def pids_visible(record):
    """Whether a missing launcher PID of RECORD (a run.json) means anything here: the launcher recorded its PID
    namespace and this process is in the same one; for records without one (versions before 0.1.78), this process
    runs outside any sandbox or container."""
    recorded = record.get("pid_ns") if isinstance(record, dict) else None
    if isinstance(recorded, str) and recorded:
        return recorded == pid_namespace()
    return not inside_sandbox()


def pod_of(record):
    """The Podman pod of the run RECORD: its run.json's "pod", else agent-RUN (what every launcher names it);
    None when there is none to name."""
    if not isinstance(record, dict):
        return None
    pod = record.get("pod")
    if isinstance(pod, str) and POD_NAME.fullmatch(pod):
        return pod
    run = record.get("run")
    return f"agent-{run}" if isinstance(run, str) and RUN_ID.fullmatch(run) else None


def pods(url=None, fresh=False, clock=time.monotonic):
    """{pod name: {"status", "live": containers of it (not the infra one) that run or are paused, "labels"}} as
    Podman (URL: a Podman service, else the local one) lists them ({} without a podman program), or None when
    Podman does not answer (no service, an error, a timeout). Kept for CACHE_SECONDS unless FRESH: a listing looks
    once."""
    key = url or ""
    hit = _cache.get(key)
    if hit is not None and not fresh and clock() - hit[0] < CACHE_SECONDS:
        return hit[1]
    try:
        done = RUNNER(podman(url, "pod", "ps", "--format", "json"), capture_output=True, text=True,
                      stdin=subprocess.DEVNULL, timeout=PODMAN_TIMEOUT, check=False)
        listed = json.loads(done.stdout or "null") if done.returncode == 0 else None
    except FileNotFoundError:
        listed = []  # no podman here: no pod of it can run (a launcher used this same program)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        listed = None
    found = None
    if isinstance(listed, list):
        found = {}
        for pod in listed:
            if not isinstance(pod, dict) or not isinstance(pod.get("Name"), str):
                continue
            infra = pod.get("InfraId")
            containers = pod.get("Containers") if isinstance(pod.get("Containers"), list) else []
            live = [c for c in containers if isinstance(c, dict) and c.get("Id") != infra
                    and str(c.get("Status", "")).lower() in LIVE_STATES]
            labels = pod.get("Labels") if isinstance(pod.get("Labels"), dict) else {}
            found[pod["Name"]] = {"status": str(pod.get("Status") or ""), "live": len(live), "labels": labels,
                                  "containers": len(containers)}
    _cache[key] = (clock(), found)
    return found


def pod_live(pod, url=None, fresh=False):
    """True when a container of POD (not the infra one) runs or is paused, False when none does or there is no
    such pod, None when Podman does not answer."""
    found = pods(url, fresh)
    if found is None:
        return None
    return bool(found.get(pod, {}).get("live"))


def verdict(record):
    """What became of the run RECORD (a run.json) whose launcher PID is gone: "dead" (ended: its directory may
    go), "orphaned" (its Podman pod still runs a container) or "unknown" (this process cannot tell: inside a
    sandbox, another PID namespace, Podman not answering)."""
    if not pids_visible(record):
        return "unknown"
    if record.get("backend") != "podman":
        return "dead"  # Bubblewrap ends with its launcher (--die-with-parent)
    pod = pod_of(record)
    if pod is None:
        return "unknown"
    url = record.get("podman_url") if isinstance(record.get("podman_url"), str) else None
    live = pod_live(pod, url)
    if live is None:
        return "unknown"
    return "orphaned" if live else "dead"


def remove_ended_pod(record):
    """Remove the pod of the ended run RECORD if Podman still lists it with nothing of it running (a launcher that
    was killed left it); without --force: a pod with a running container is never removed. True if removed."""
    pod = pod_of(record)
    if record.get("backend") != "podman" or pod is None:
        return False
    url = record.get("podman_url") if isinstance(record.get("podman_url"), str) else None
    found = pods(url)
    info = (found or {}).get(pod)
    if info is None or info["live"] or info["status"] not in ENDED_POD_STATES:
        return False
    try:
        done = RUNNER(podman(url, "pod", "rm", pod), capture_output=True, text=True, stdin=subprocess.DEVNULL,
                      timeout=PODMAN_TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    _cache.pop(url or "", None)
    return done.returncode == 0


def discard(directory):
    """Remove the run directory DIRECTORY of an ended run, and its browser directory (RUN.browser) beside it."""
    directory = Path(directory)
    shutil.rmtree(directory, ignore_errors=True)
    shutil.rmtree(directory.with_name(directory.name + ".browser"), ignore_errors=True)


def stop_pod(pod, url=None, wait=30):
    """End the pod POD of an orphaned run: `podman pod stop --time WAIT` (its supervisor gets SIGTERM and ends the
    agent as at any stop), then `podman pod rm --force`. (True, "") once no container of it runs, else (False,
    what Podman said)."""
    said = []
    for args, limit in ((("pod", "stop", "--time", str(int(wait)), pod), wait + PODMAN_TIMEOUT),
                        (("pod", "rm", "--force", pod), PODMAN_TIMEOUT * 3)):
        try:
            done = RUNNER(podman(url, *args), capture_output=True, text=True, stdin=subprocess.DEVNULL,
                          timeout=limit, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            said.append(str(exc))
            continue
        if done.returncode != 0:
            lines = (done.stderr or "").strip().splitlines()
            said.append(lines[-1] if lines else f"podman {' '.join(args[:2])} failed ({done.returncode})")
    _cache.pop(url or "", None)
    live = pod_live(pod, url, fresh=True)
    return live is False, "; ".join(said)


def leftover_pods(in_use=(), url=None):
    """sandbox-ai's own pods (OWN_POD names, or RUN_LABEL/SESSION_LABEL) that nothing runs in any more and that no
    start known here uses (IN_USE: pod names): [(name, status)]; None when Podman does not answer. Only pods in an
    ended state (ENDED_POD_STATES): one being created or running is never a leftover."""
    found = pods(url, fresh=True)
    if found is None:
        return None
    return sorted((name, info["status"]) for name, info in found.items()
                  if (OWN_POD.fullmatch(name) or RUN_LABEL in info["labels"] or SESSION_LABEL in info["labels"])
                  and name not in in_use and not info["live"] and info["status"] in ENDED_POD_STATES)


def unknown_live_pods(in_use=(), url=None):
    """sandbox-ai's own pods that still run a container although no start known here uses them (a run whose launcher
    died and whose directory an older version removed): [name]; None when Podman does not answer."""
    found = pods(url)
    if found is None:
        return None
    return sorted(name for name, info in found.items()
                  if (OWN_POD.fullmatch(name) or RUN_LABEL in info["labels"] or SESSION_LABEL in info["labels"])
                  and name not in in_use and info["live"])


def remove_pods(names, url=None):
    """`podman pod rm NAME` (without --force) for each of NAMES; the names removed."""
    removed = []
    for name in names:
        try:
            done = RUNNER(podman(url, "pod", "rm", name), capture_output=True, text=True, stdin=subprocess.DEVNULL,
                          timeout=PODMAN_TIMEOUT * 3, check=False)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if done.returncode == 0:
            removed.append(name)
    _cache.pop(url or "", None)
    return removed
