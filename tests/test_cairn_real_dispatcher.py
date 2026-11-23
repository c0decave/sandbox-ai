"""Every harness as a worker of REAL Cairn (its server and its dispatcher in local execution mode, at the reviewed
commit, unchanged), with sandbox-ai's own runtime (cairn_runtime.main), shims and relay, and stand-ins of the
harnesses that print as harnesses do (progress, colours, a tool call's JSON, nanobot's header) and answer Cairn's task
with the JSON object its prompts ask for: Cairn's claudecode driver calls the shim, the relay runs the harness's own
one-shot command line, and the project must be completed with the worker's fact on the board.

Opt-in: SANDBOX_AI_CAIRN_PYTHON names a Python 3.12 in which Cairn imports (`cairn.cli`) - Cairn's locked
dependencies (sandbox/Containerfile.cairn.lock, `--require-hashes`) and its source at cairn.COMMIT; skipped otherwise
(CI has no Cairn outside its image). Run 2026-11-22 for every mapped harness: each project completed.

The user (2026-11-22): "why workers only for claude and not all harnesses?" """
import importlib.util
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
from types import SimpleNamespace
import urllib.request

import pytest

ROOT = Path(__file__).resolve().parents[1]
PYTHON = os.environ.get("SANDBOX_AI_CAIRN_PYTHON")
pytestmark = pytest.mark.skipif(not PYTHON, reason="set SANDBOX_AI_CAIRN_PYTHON to a Python in which Cairn imports")


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cairn = load("cairn_for_real_dispatcher", "sandbox/cairn.py")
STAND_IN = r'''#!PYTHON -I
import json, os, re, sys
name, args = os.path.basename(sys.argv[0]), sys.argv[1:]
if args == ["--help"]:
    print("usage")
    sys.exit(0)
prompt = sys.stdin.read() if args[-1:] == ["-"] else \
    (args[-1].split("=", 1)[-1] if args[-1].startswith("--") else args[-1])
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps({"name": name, "args": args[:-1]}) + "\n")
words = re.findall(r"LIVE[0-9A-F]{8}", prompt)
for path in re.findall(r"\S+/graph\.yaml", prompt):
    try:
        words += re.findall(r"LIVE[0-9A-F]{8}", open(path).read())
    except OSError:
        pass
word = words[0] if words else "unknown"
print("\x1b[2m> tool: bash {\"cmd\": \"ls\"}\x1b[0m", file=sys.stderr)
print("\x1b[1mthinking...\x1b[0m")
if name == "nanobot":
    print('first start {"config": "written"}')
    print("\N{CAT} nanobot")
print(json.dumps({"accepted": True, "data": {"fact": {"description": f"The code word in lower case is {word.lower()}."},
                                             "complete": {"description": "The fact states the code word."}}}))
'''
RUNTIME = '''import importlib.util, sys
from pathlib import Path
base = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("cairn_runtime_real", sys.argv[2])
rt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rt)
rt.BIN, rt.HOME, rt.DISPATCH = base / "bin-cairn", base / "home", base / "dispatch.yaml"
sys.exit(rt.main(mount=base / "mount"))
'''


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def get(url):
    with urllib.request.urlopen(url, timeout=5) as answer:
        return json.loads(answer.read())


@pytest.mark.parametrize("harness", [a for a in cairn.WORKERS if a not in ("claude", "codex")])
def test_real_cairn_completes_a_project_with_each_mapped_harness(tmp_path, harness):
    standins = tmp_path / "standins"
    standins.mkdir()
    (standins / harness).write_text(STAND_IN.replace("PYTHON", sys.executable, 1), encoding="utf-8")
    (standins / harness).chmod(0o755)
    base, runs, port = tmp_path / "b", tmp_path / "b" / "runs", free_port()
    (base / "mount" / "relay").mkdir(parents=True)
    (base / "data").mkdir()
    nonce = "LIVE" + secrets.token_hex(4).upper()
    a = SimpleNamespace(cairn_workers=[{"agent": harness, "model": None}],
                        cairn_project={"title": f"sandbox-ai: The code word is {nonce}.",
                                       "origin": f"The code word is {nonce}.",
                                       "goal": f"A fact on the board states the code word {nonce} in lower case.",
                                       "hints": []})
    dispatch = cairn.dispatch(a)
    dispatch["server"] = f"http://127.0.0.1:{port}"
    dispatch["local"]["workspace_root"] = str(runs)
    socket_path = base / "mount" / "relay" / "relay.sock"
    (base / "mount" / "config.json").write_text(json.dumps({
        "port": port, "data": str(base / "data"), "workers": [harness], "socket": str(socket_path),
        "dispatch": dispatch, "project": a.cairn_project}))
    shutil.copy(ROOT / "sandbox" / "cairn_relay.py", base / "mount" / "cairn_relay.py")
    (tmp_path / "runtime.py").write_text(RUNTIME)
    log = tmp_path / "calls.jsonl"
    relay = subprocess.Popen(
        [sys.executable, "-I", str(ROOT / "sandbox" / "cairn_relay.py"), "serve",
         json.dumps({"socket": str(socket_path), "workspace": str(runs), "workers": {harness: []}})],
        env={"PATH": f"{standins}:/usr/bin:/bin", "FAKE_LOG": str(log), "HOME": str(tmp_path / "agent-home"),
             "LANG": "C.UTF-8", "PYTHONIOENCODING": "utf-8"},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    runtime = subprocess.Popen([PYTHON, "-I", str(tmp_path / "runtime.py"), str(base),
                                str(ROOT / "sandbox" / "cairn_runtime.py")], cwd="/",
                               stdout=open(tmp_path / "runtime.out", "w"), stderr=subprocess.STDOUT,
                               start_new_session=True)
    detail = {}
    try:
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            time.sleep(1)
            try:
                found = [p for p in get(f"http://127.0.0.1:{port}/projects") if nonce in p.get("title", "")]
                if found:
                    detail = get(f"http://127.0.0.1:{port}/projects/{found[0]['id']}")
                    if detail["project"].get("status") == "completed":
                        break
            except OSError:
                continue
    finally:
        for child in (runtime, relay):
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except OSError:
                pass
        for child in (runtime, relay):
            try:
                child.wait(15)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
    said = (tmp_path / "runtime.out").read_text(errors="replace")[-3000:]
    assert detail.get("project", {}).get("status") == "completed", said
    assert any(f"live{nonce[4:].lower()}" in fact["description"] for fact in detail["facts"]), detail["facts"]
    assert any(i.get("worker") == f"sandbox-{harness}" for i in detail["intents"]), detail["intents"]
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert [c["name"] for c in calls] == [harness], "one task, the harness's own command line"
