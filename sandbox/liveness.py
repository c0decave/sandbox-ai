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
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent


def launch_log():
    """launchlog.py, the one copy of this process (launcher.log; DEBUG lines with SANDBOX_AI_DEBUG=1)."""
    loaded = sys.modules.get("sandbox_ai_launchlog")
    if loaded is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_launchlog", HERE / "launchlog.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        loaded = sys.modules.setdefault("sandbox_ai_launchlog", module)
    return loaded


LOG = launch_log()
PODMAN_TIMEOUT = 20
CACHE_SECONDS = 2.0
POD_NAME = re.compile(r"agent-[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,80}")
# sandbox-ai's own pods: agent-AGENT-HEX (a run, a session's run, a VPN's pod) and agent-mcp-NAME (an MCP server);
# newer ones also carry RUN_LABEL. Nothing else is ever removed as a leftover.
OWN_POD = re.compile(r"agent-(?:(?:claude|codex|opencode|hermes|dsh|qwen|goose)-[0-9a-f]{8,12}|mcp-[A-Za-z0-9_-]{1,32})")
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
    why = ""
    try:
        done = RUNNER(podman(url, "pod", "ps", "--format", "json"), capture_output=True, text=True,
                      stdin=subprocess.DEVNULL, timeout=PODMAN_TIMEOUT, check=False)
        listed = json.loads(done.stdout or "null") if done.returncode == 0 else None
        if done.returncode != 0:
            why = f"exit status {done.returncode}: {LOG.tail(done.stderr)}"
        elif not isinstance(listed, list):  # (it was logged with no reason at all)
            why = f"exit status 0, but no list of pods: {LOG.tail(done.stdout, 200) or 'no output'}"
            listed = None
    except FileNotFoundError:
        listed = []  # no podman here: no pod of it can run (a launcher used this same program)
    except (OSError, ValueError, subprocess.TimeoutExpired, RecursionError) as exc:
        why = LOG.exception_text(exc)
        listed = None
    # Polled (the app's listing): said once when Podman stops answering, once when it answers again.
    where = f"Podman at {url}" if url else "Podman"
    answered_before = LOG.state(("podman pod ps", key)) is not False
    LOG.changed(("podman pod ps", key), listed is not None, "liveness",
                (None if answered_before else f"{where} answers podman pod ps again") if listed is not None else
                f"{where} does not answer podman pod ps (whether a run's pod still runs stays unknown): {why}",
                "INFO" if listed is not None else "WARNING")
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
    def decided(state, why):
        run = record.get("run") if isinstance(record, dict) else None
        LOG.changed(("verdict", str(run)), state, "liveness", f"the run {run} (its launcher is gone): {state}: {why}",
                    "DEBUG" if state == "unknown" else "INFO")
        return state
    if not pids_visible(record):
        return decided("unknown", "its launcher's processes are not visible from here (another PID namespace, a "
                                  "sandbox)")
    if record.get("backend") != "podman":
        return decided("dead", "Bubblewrap ends with its launcher")
    pod = pod_of(record)
    if pod is None:
        return decided("unknown", "its record names no pod")
    url = record.get("podman_url") if isinstance(record.get("podman_url"), str) else None
    live = pod_live(pod, url)
    if live is None:
        return decided("unknown", "Podman does not answer")
    return decided("orphaned", f"its pod {pod} still runs a container") if live else \
        decided("dead", f"no container of its pod {pod} runs")


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
        done = LOG.run("liveness", f"remove the pod {pod} of an ended run", podman(url, "pod", "rm", pod),
                       runner=RUNNER, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                       timeout=PODMAN_TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    _cache.pop(url or "", None)
    if done.returncode == 0:
        LOG.info("liveness", f"removed the pod {pod} its killed launcher left behind")
    return done.returncode == 0


TEMPORARY_PREFIX = "agent-sandbox-"  # the launcher's temporary folders (its session's and its proxy's)


def discard(directory):
    """Remove the run directory DIRECTORY of an ended run, its browser directory (RUN.browser) and the folder of its
    sandbox browser's VNC socket (RUN.browser-vnc, browser.vnc_dir) beside it, and the temporary folders its run.json
    names (a launcher killed with SIGKILL left them for good: the proxy's proxy.json holds the --api-key keys and the
    Git and LLM tokens in plain text, the session's folder the tokens given at run time)."""
    directory = Path(directory)
    try:
        record = json.loads((directory / "run.json").read_text())
    except (OSError, ValueError, RecursionError):
        record = None
    if isinstance(record, dict):
        for key in ("session_dir", "proxy_dir"):
            remove_temporary(record.get(key))
    shutil.rmtree(directory, ignore_errors=True)
    for suffix in (".browser", ".browser-vnc"):
        shutil.rmtree(directory.with_name(directory.name + suffix), ignore_errors=True)


def remove_temporary(path):
    """Remove PATH when it is one of the launcher's temporary folders: an absolute path to a folder (never a link)
    named agent-sandbox-..., of this user. Anything else a damaged run.json names is left alone."""
    if not isinstance(path, str) or not os.path.isabs(path) or not Path(path).name.startswith(TEMPORARY_PREFIX):
        return
    try:
        info = os.lstat(path)
    except OSError:
        return
    if stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid():
        shutil.rmtree(path, ignore_errors=True)


def stop_pod(pod, url=None, wait=30):
    """End the pod POD of an orphaned run: `podman pod stop --time WAIT` (its supervisor gets SIGTERM and ends the
    agent as at any stop), then `podman pod rm --force`. (True, "") once no container of it runs, else (False,
    what Podman said)."""
    said = []
    for args, limit in ((("pod", "stop", "--time", str(int(wait)), pod), wait + PODMAN_TIMEOUT),
                        (("pod", "rm", "--force", pod), PODMAN_TIMEOUT * 3)):
        try:
            done = LOG.run("liveness", f"end the orphaned pod {pod}", podman(url, *args), runner=RUNNER,
                           capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=limit, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            said.append(str(exc))
            continue
        if done.returncode != 0:
            lines = (done.stderr or "").strip().splitlines()
            said.append(lines[-1] if lines else f"podman {' '.join(args[:2])} failed ({done.returncode})")
    _cache.pop(url or "", None)
    live = pod_live(pod, url, fresh=True)
    LOG.info("liveness", f"the orphaned pod {pod} is ended" if live is False else
             f"the orphaned pod {pod} is not ended ({'it runs' if live else 'Podman does not answer'})"
             + (f": {'; '.join(said)}" if said else ""))
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
            done = LOG.run("liveness", f"remove the leftover pod {name}", podman(url, "pod", "rm", name), runner=RUNNER,
                           capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=PODMAN_TIMEOUT * 3,
                           check=False)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if done.returncode == 0:
            removed.append(name)
    _cache.pop(url or "", None)
    return removed
