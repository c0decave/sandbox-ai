"""Independent verification A (2026-11-19, docs/reviews/credentials-verify-A.md): the Credentials page's Git logins
tab, "Give to a running sandbox…", end to end - the app's real backend runs the real ./sandbox-ai (a subprocess) for
a start the real launcher made (tests/live_fakes.Host: Podman faked; the sandbox's supervisor is the real
runtime.control_server in a thread here). Only the dialog's answers are given."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, credentials_page as C, widgets  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tests"))
from live_fakes import Host  # noqa: E402

TOKEN = "gitea_appgiven_0123456789abcdef"
NAME = "appcred"


def runtime():
    spec = importlib.util.spec_from_file_location("sandbox_ai_runtime_verify_a_app", ROOT / "sandbox" / "runtime.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def running(monkeypatch):
    """A real start NAME (its launcher waits on the fake sandbox) with the secret GITEA, and the app's process in
    that user's environment (HOME, XDG folders, PATH with the fakes): what the app runs reaches that start."""
    short = Path(tempfile.mkdtemp(prefix="cva-app-", dir="/tmp"))
    host = Host(short / "h")
    host.add_image("localhost/claude-sandbox:latest")
    host.env.update(SANDBOX_AI_SECRETS="file", SANDBOX_AI_SECRETS_PLAIN="1")
    for key in ("HOME", "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME",
                "PATH", "SANDBOX_AI_SECRETS", "SANDBOX_AI_SECRETS_PLAIN", *(["FAKE_PYTHON"] if host.shimmed else [])):
        monkeypatch.setenv(key, host.env[key])
    kept = subprocess.run(host.command("secrets", "set", "GITEA"), input=TOKEN, text=True, capture_output=True,
                          env=host.env, cwd=host.workspace, timeout=60)
    assert kept.returncode == 0, kept.stderr
    process = host.popen("claude", "--no-tmux", "--name", NAME, "--no-canaries", "--no-checkpoint",
                         env={"FAKE_PODMAN_RUN": "0:600"})
    deadline, run_dir = time.monotonic() + 60, None
    while run_dir is None and time.monotonic() < deadline:
        for record in (host.runtime / "sandbox-ai" / "runs").glob("*/run.json"):
            if json.loads(record.read_text()).get("name") == NAME:
                run_dir = record.parent
        time.sleep(0.1)
    assert run_dir is not None, "the start registered"
    rt = runtime()
    # (the proxy's run directory, which the supervisor writes: where a login given during the run goes)
    proxy = short / "proxy-run"
    proxy.mkdir(mode=0o700)
    monkeypatch.setattr(rt, "PROXY_RUN", proxy)
    monkeypatch.setattr(rt, "_added_git_logins", {"stamp": None, "entries": []})
    stop = threading.Event()
    network = rt.RuntimeNetwork({"loopback": []}, short / "allow.json", lambda *a: None)
    thread = threading.Thread(target=rt.control_server, args=(run_dir / "control.sock", network, stop), daemon=True)
    thread.start()
    while not (run_dir / "control.sock").exists() and time.monotonic() < deadline + 10:
        time.sleep(0.02)
    record = json.loads((run_dir / "run.json").read_text())
    yield {"host": host, "rt": rt, "run": record["run"], "session": Path(record["session_dir"])}
    if process.poll() is None:
        host.run("stop", NAME, timeout=120)
        try:
            process.communicate(timeout=120)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
    stop.set()
    thread.join(5)
    shutil.rmtree(short, ignore_errors=True)


def test_the_git_logins_tabs_give_reaches_the_runs_git_through_the_real_sandbox_ai(qapp, running, wait, monkeypatch):
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)  # ask_first's question: yes
    monkeypatch.setattr(widgets, "SHOWN", [])
    sai = backend.SandboxAI()
    page = C.CredentialsPage(sai, lambda *a, **k: None)
    try:
        page.git.show_entries([{"url": "http://localhost:3080", "user": "alice", "secret": "GITEA"}], ["GITEA"])
        page.git.list.selectRow(0)
        offered = []

        def run_dialog(dialog):
            offered.extend((dialog.choice.itemText(i), dialog.choice.itemData(i)) for i in range(dialog.choice.count()))
            return QtWidgets.QDialog.DialogCode.Accepted
        monkeypatch.setattr(C.GiveDialog, "exec", run_dialog)
        page.git.message.setText("")
        page.git.give()
        assert wait(lambda: page.git.message.text() and not page.git.message.text().endswith("…"), 90), \
            page.git.message.text()
        text = page.git.message.text()
        assert [run for _, run in offered] == [running["run"]] and NAME in offered[0][0]
        assert f"{running['run']} ({NAME}) has the Git login for http://localhost:3080 (user alice)" in text, text
        assert TOKEN not in text
        assert "connection proxy logs in with it" in text and "the token is not in the sandbox" in text
        # Where it is: with the run's connection proxy (the supervisor's file for it; since 2026-11-19 - it was a
        # file in the sandbox's session directory, finding 2), and what that proxy adds for its server on the wire.
        rt = running["rt"]
        given = json.loads((Path(rt.PROXY_RUN) / rt.ADDED_GIT_LOGINS).read_text())["git_logins"]
        assert [(e["host"], e["port"], e["user"], e["token"]) for e in given] == [("localhost", 3080, "alice", TOKEN)]
        assert not (running["session"] / "git-tokens.json").exists(), "nothing in what the sandbox sees"
        server = rt.ProxyServer.__new__(rt.ProxyServer)
        server.config = {}
        import base64
        added = server.git_token("http", "localhost", {"destination": "127.0.0.1:3080"}, "GET", "/o/r.git/info/refs",
                                 "/o/r.git/info/refs?service=git-receive-pack", [])
        assert base64.b64decode(added) == f"alice:{TOKEN}".encode()
        # The app's log of the command: no token.
        logs = [p for p in Path(os.environ["XDG_STATE_HOME"]).rglob("*.log") if p.is_file()]
        assert not [p for p in logs if TOKEN in p.read_text(errors="replace")]
    finally:
        page.deleteLater()
