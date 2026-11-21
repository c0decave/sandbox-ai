#!/usr/bin/env python3
"""The runtime of sandbox-ai's Cairn container (Containerfile.cairn; --cairn, cairn.py): mounted read-only at
/sandbox-cairn at every start beside cairn_relay.py, run by the image's CPython 3.12 (Cairn's venv), so that a new
sandbox-ai needs no rebuild of the image.

It starts Cairn's server on the pod's loopback (cairn serve, its database in /data), makes the project the start
names (--cairn-origin, --cairn-goal, --cairn-hint) unless the board has one of that title already, waits for the relay
of the agent's container, and starts Cairn's dispatcher in its local execution mode with sandbox-ai's configuration
and the workers' shims first on its PATH: `claude` and `codex` are cairn_relay.py worker, nothing of an agent is in
this image. The dispatcher is started again when it ends (at most RESTARTS times in RESTART_WINDOW seconds); the
server ending ends the container. SIGTERM (the run ends) stops both. Once the relay's supervisor in the agent's
container gave up starting the relay again (its note beside the socket, RELAY_ENDED), the dispatcher is stopped with
that note and not started again: no task could run, and each would fail at once.

Cairn hands a failed task out again a moment later, without a limit (a worker whose answer does not parse fails
with exit status 0, which the relay cannot tell from a good one): its dispatcher's log is read here, and FAILURES
failed tasks within FAILURE_WINDOW seconds stop it for a pause (PAUSE seconds, doubled with each pause up to
PAUSE_LIMIT), after which it starts again. The board stays meanwhile.

Standard library only (Cairn's own code runs in its processes).
"""
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request

MOUNT = Path("/sandbox-cairn")
BIN = Path("/tmp/cairn-bin")
HOME = Path("/tmp/home")
DISPATCH = Path("/tmp/dispatch.yaml")  # sandbox-ai's configuration of the dispatcher, written at the start
RESTARTS = 10
RESTART_WINDOW = 600
FAILURES = 5
FAILURE_WINDOW = 600
PAUSE = 60
PAUSE_LIMIT = 900
FAILED = re.compile(r"\btask finished project=\S+ task=(\S+) worker=(\S+) outcome=(\S+)")
SERVE_WAIT = 90
CAIRN = ("-c", "from cairn.cli import main; main()")


def shims(folder=None, python=None, relay=None):
    """FOLDER/claude and FOLDER/codex: the relay's worker side, which Cairn's dispatcher takes for the CLIs."""
    folder, python = Path(folder or BIN), python or sys.executable
    relay = relay or MOUNT / "cairn_relay.py"
    folder.mkdir(parents=True, exist_ok=True)
    for agent in ("claude", "codex"):
        path = folder / agent
        path.write_text(f'#!/bin/sh\nexec {python} -I {relay} worker {agent} "$@"\n')
        path.chmod(0o755)
    return folder


def port_open(port, host="127.0.0.1", timeout=0.5):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def relay_ready(path):
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.settimeout(1)
        client.connect(str(path))
        return True
    except OSError:
        return False
    finally:
        client.close()


RELAY_ENDED = "relay.ended"  # beside the relay's socket: why it is not started again (cairn_relay.ENDED, supervise)


def relay_ended(path):
    """The note of the relay's supervisor beside its socket PATH once it gave up starting the relay again, or None."""
    try:
        with open(Path(path).with_name(RELAY_ENDED), "rb") as stream:
            return stream.read(2000).decode("utf-8", "replace").strip() or None
    except OSError:
        return None


def request(method, url, body=None, opener=urllib.request.urlopen):
    data = json.dumps(body).encode() if body is not None else None
    sent = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    with opener(sent, timeout=30) as answer:
        return json.loads(answer.read() or b"null")


def make_project(cfg, opener=urllib.request.urlopen):
    """The start's project on the board, unless one of its title is there (a kept board): its id, or None."""
    project = cfg.get("project")
    if not project:
        return None
    base = f"http://127.0.0.1:{int(cfg['port'])}"
    for summary in request("GET", f"{base}/projects", opener=opener) or []:
        if isinstance(summary, dict) and summary.get("title") == project["title"]:
            return summary.get("id")
    made = request("POST", f"{base}/projects", {"title": project["title"], "origin": project["origin"],
                                                "goal": project["goal"], "hints": project.get("hints") or []},
                   opener=opener)
    return ((made or {}).get("project") or {}).get("id")


def environment(base=None, mount=None):
    env = {key: value for key, value in (base or os.environ).items() if key in ("LANG", "TZ")}
    env.update({"HOME": str(HOME), "PATH": f"{BIN}:/usr/local/bin:/usr/bin:/bin", "PYTHONUNBUFFERED": "1",
                "SANDBOX_AI_CAIRN_RELAY": str(Path(mount or MOUNT) / "relay" / "relay.sock")})
    return env


class FailureWatch:
    """The dispatcher's log, line by line: FAILURES failed tasks within WINDOW seconds trip it; each trip asks for a
    pause twice as long as the one before (PAUSE up to LIMIT)."""

    def __init__(self, clock=time.monotonic, failures=None, window=None, pause=None, limit=None):
        self.clock = clock
        self.failures, self.window = failures or FAILURES, window or FAILURE_WINDOW
        self.pause, self.limit = pause or PAUSE, limit or PAUSE_LIMIT
        self.lock = threading.Lock()
        self.times, self.trips = [], 0

    def line(self, text):
        """One line of the dispatcher's log; True when it trips the watch."""
        if not FAILED.search(text):
            return False
        with self.lock:
            now = self.clock()
            self.times = [t for t in self.times if now - t < self.window] + [now]
            return len(self.times) >= self.failures

    def tripped(self):
        with self.lock:
            return len([t for t in self.times if self.clock() - t < self.window]) >= self.failures

    def next_pause(self):
        """The pause after a trip (and the watch cleared for the next start)."""
        with self.lock:
            pause = min(self.limit, self.pause * (2 ** self.trips))
            self.trips += 1
            self.times = []
            return pause


def forward(stream, watch, out=None):
    """The dispatcher's output to this container's (podman logs), each line shown to WATCH."""
    out = out or sys.stderr
    for line in iter(stream.readline, ""):
        out.write(line)
        out.flush()
        watch.line(line)


def say(text):
    print(f"sandbox-ai cairn: {text}", file=sys.stderr, flush=True)


def main(mount=MOUNT, popen=subprocess.Popen, sleep=time.sleep, clock=time.monotonic):
    cfg = json.loads((mount / "config.json").read_text())
    HOME.mkdir(parents=True, exist_ok=True)
    shims(relay=Path(mount) / "cairn_relay.py")
    Path(cfg["dispatch"]["local"]["workspace_root"]).mkdir(parents=True, exist_ok=True)
    dispatch = DISPATCH
    dispatch.write_text(json.dumps(cfg["dispatch"], indent=1))  # JSON is YAML: Cairn's loader reads it
    env = {**environment(mount=mount), "SANDBOX_AI_CAIRN_RELAY": str(cfg["socket"])}
    children = []

    def stop(*_):
        for child in children:
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(10)
            except subprocess.TimeoutExpired:
                child.kill()
        sys.exit(0)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    server = popen([sys.executable, "-I", *CAIRN, "serve", "--host", "127.0.0.1", "--port", str(int(cfg["port"])),
                    "--db-path", f"{cfg['data']}/cairn.db", "--no-access-log"], env=env, cwd=str(HOME))
    children.append(server)
    deadline = clock() + SERVE_WAIT
    while not port_open(int(cfg["port"])):
        if server.poll() is not None or clock() > deadline:
            say("Cairn's server did not start")
            return 1
        sleep(0.2)
    try:
        project = make_project(cfg)
        if project:
            say(f"the project {project} is on the board")
    except (OSError, ValueError) as exc:
        say(f"the start's project could not be made ({exc}); make it on the board")
    socket_path = Path(cfg["socket"])
    while not relay_ready(socket_path):  # the agent's container starts after this one
        if server.poll() is not None:
            return 1
        sleep(0.5)
    say(f"the relay to the workers ({', '.join(cfg['workers'])}) answers; Cairn's dispatcher starts")
    starts, watch = [], FailureWatch(clock=clock)
    while server.poll() is None:
        ended = relay_ended(socket_path)
        if ended:
            say(f"{ended}. Cairn's dispatcher is stopped (the board stays)")
            server.wait()
            break
        starts = [t for t in starts if clock() - t < RESTART_WINDOW]
        if len(starts) >= RESTARTS:
            say(f"the dispatcher ended {RESTARTS} times in {RESTART_WINDOW} s: it is not started again (the board "
                "stays)")
            server.wait()
            break
        starts.append(clock())
        dispatcher = popen([sys.executable, "-I", *CAIRN, "dispatch", "--config", str(dispatch)], env=env,
                           cwd=cfg["dispatch"]["local"]["workspace_root"], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True, errors="replace")
        children.append(dispatcher)
        threading.Thread(target=forward, args=(dispatcher.stdout, watch), daemon=True).start()
        while dispatcher.poll() is None and server.poll() is None and not watch.tripped() and \
                not relay_ended(socket_path):
            sleep(1)
        if relay_ended(socket_path) and server.poll() is None:
            dispatcher.terminate()  # (no task can run any more: the loop's start says why)
            try:
                dispatcher.wait(30)
            except subprocess.TimeoutExpired:
                dispatcher.kill()
        elif watch.tripped() and server.poll() is None:
            pause = watch.next_pause()
            dispatcher.terminate()
            try:
                dispatcher.wait(30)
            except subprocess.TimeoutExpired:
                dispatcher.kill()
            starts.pop()  # (a pause is no restart of its own)
            say(f"{watch.failures} tasks failed within {watch.window} s: Cairn's dispatcher is paused for {pause} s "
                "(every attempt spends the model's usage); stop the project on the board if it keeps failing")
            sleep(pause)
        elif server.poll() is None:
            say(f"the dispatcher ended ({dispatcher.returncode}); it starts again")
            sleep(3)
    say("Cairn's server ended")
    return 1


if __name__ == "__main__":
    sys.exit(main())
