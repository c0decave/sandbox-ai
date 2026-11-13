"""A secret given to a running session from the Credentials page, end to end (user report 2026-11-12: GH_PUSH given to
the session wild-dromouse, whose run claude-e7d6edae runs, several times - nothing arrived: no
/sandbox/session/git-tokens.json, the placeholder sandbox-ai-api-key-placeholder-GH_PUSH not put in for
api.github.com, git push 401 / "could not read Username").

What runs here is real: the run registry and the session (run.json, session.json, a supervisor process of the
session), the app's backend.instances() that the dialog lists, sandbox-ai's own git-logins, secrets and net modules
(in this process, with a fake resolver), the supervisor's control_server and the secret store's private files. Only the
dialog's answers are given.

Before 0.1.94 the Secrets tab gave a secret only as an API key (`net allow RUN --api-key HOST=NAME=SECRET`): never a
Git login, so no git-tokens.json; the proxy puts a key in for requests to that very host only (a key for github.com
is not put into requests to api.github.com, GitHub's API) and - in a run started before 0.1.94 - never as the
password of git's Basic login. These tests pin what the app does now: the right run, the login where git reads it, a
key given for GitHub's API host, and messages that say which run got what and how to check it."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, credentials_page as C, widgets  # noqa: E402

TOKEN = "gho_" + "A1b2C3d4" * 4 + "abcd"  # a token of the gh CLI's form (gho_ and 36 characters)
RUN, SESSION = "claude-e7d6edae", "wild-dromouse"


@pytest.fixture
def registry(tmp_path, monkeypatch):
    """XDG folders of this test (the runtime folder short: the supervisor binds its socket there), the secret GH_PUSH
    in sandbox-ai's private files, the session wild-dromouse with a supervisor process and its run claude-e7d6edae
    registered (run.json) with a real control_server thread, and an MCP server's run beside it."""
    short = Path(tempfile.mkdtemp(prefix="give-"))
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(short))
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    sai = backend.SandboxAI()
    net = sai.module("net")
    store = tmp_path / "data" / "sandbox-ai" / "secrets"
    store.mkdir(parents=True, mode=0o700)

    def secret(value):
        path = store / "GH_PUSH"
        path.write_text(value + "\n")
        path.chmod(0o600)
    secret(TOKEN)
    workspace = tmp_path / "project"
    workspace.mkdir()
    folder = tmp_path / "state" / "sandbox-ai" / "sessions" / SESSION
    (folder / "home").mkdir(parents=True)
    supervisor = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", "/x/agent_sandbox.py",
                                   "claude", "--tmux-socket-dir", str(folder / "tmux")])
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and b"time.sleep" not in Path(f"/proc/{supervisor.pid}/cmdline").read_bytes():
        time.sleep(0.01)
    (folder / "session.json").write_text(json.dumps({
        "agent": "claude", "options": [], "backend": "podman", "workspace": str(workspace), "cwd": str(workspace),
        "created": "2026-11-12T09:00:00Z", "pid": supervisor.pid,
        "pid_start": sai.launcher.process_start(supervisor.pid), "run": RUN, "pod": f"agent-{RUN}", "mode": "tmux"}))
    session_dir = tmp_path / "run-session"  # the run's /sandbox/session
    session_dir.mkdir()
    (session_dir / "hosts").write_text("127.0.0.1 localhost\n")
    runs = short / "sandbox-ai" / "runs"
    run = runs / RUN
    run.mkdir(parents=True)
    me = {"pid": os.getpid(), "pid_start": sai.launcher.process_start(os.getpid())}
    (run / "run.json").write_text(json.dumps({
        "run": RUN, "agent": "claude", "backend": "podman", "workspace": str(workspace), **me,
        # as a session's launcher registers (agent_sandbox.register_run): its pod as "session", its start's id
        "session": f"agent-{RUN}", "session_dir": str(session_dir), "name": SESSION, "start": SESSION,
        "kind": "agent", "proxy": True, "version": "0.1.94", "runtime_git_tokens": True, "host_relay": True,
        "started": "2026-11-12T09:00:01Z"}))
    server = runs / "mcp-github"
    server.mkdir()
    (server / "run.json").write_text(json.dumps({
        "run": "mcp-github", "agent": "claude", "backend": "podman", "workspace": str(workspace), **me,
        "session": None, "session_dir": str(tmp_path / "mcp-session"), "name": None, "start": None, "kind": "mcp",
        "proxy": True, "version": "0.1.94", "started": "2026-11-12T09:00:02Z"}))
    rt = net.launcher().RUNTIME
    proxy = tmp_path / "proxy-run"
    proxy.mkdir(mode=0o700)
    monkeypatch.setattr(rt, "PROXY_RUN", proxy)
    stop = threading.Event()
    network = rt.RuntimeNetwork({"loopback": []}, tmp_path / "allow.json", lambda *a: None)
    thread = threading.Thread(target=rt.control_server, args=(run / "control.sock", network, stop), daemon=True)
    thread.start()
    while not (run / "control.sock").exists() and time.monotonic() < deadline + 5:
        time.sleep(0.02)
    # sandbox-ai's own commands, in this process (a name is resolved by the test, never by DNS).
    real = net.main
    monkeypatch.setattr(net, "main", lambda argv, **kw: real(argv, **{"resolver": lambda hosts: {
        h: ["192.0.2.50"] for h in hosts}, **kw}))
    commands = {"git-logins": sai.module("git_logins"), "secrets": sai.module("secret_store")}
    for module in commands.values():
        monkeypatch.setattr(module, "_net", net)
    sent = []

    def host_run(host, *args, timeout=None, input=None):
        sent.append((host, *args))
        out, err = [], io.StringIO()
        with contextlib.redirect_stderr(err):
            status = commands[args[0]].main(list(args[1:]), out=out.append)
        return status, "\n".join(out) + "\n", err.getvalue()
    monkeypatch.setattr(sai, "host_run", host_run)
    yield {"sai": sai, "tokens": session_dir / "git-tokens.json", "keys": proxy / rt.ADDED_API_KEYS, "sent": sent,
           "secret": secret, "net": net, "run": run}
    stop.set()
    thread.join(5)
    supervisor.kill()
    supervisor.wait()
    shutil.rmtree(short, ignore_errors=True)


@pytest.fixture
def page(qapp, registry, monkeypatch):
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)  # ask_first's question: yes
    monkeypatch.setattr(widgets, "SHOWN", [])
    view = C.CredentialsPage(registry["sai"], lambda *a, **k: None)
    widgets.fill(view.secrets, [["GH_PUSH", "file", ""]])
    view.secrets.selectRow(0)
    yield view
    view.deleteLater()


def give(page, wait, monkeypatch, answer):
    """The Secrets tab's Give… with the dialog answered by ANSWER(dialog); the dialog's runs are what the app's
    backend found. Returns the runs the dialog offered ([(label, run)]) once the message is final."""
    offered = []

    def run_dialog(dialog):
        offered.extend((dialog.choice.itemText(i), dialog.choice.itemData(i)) for i in range(dialog.choice.count()))
        answer(dialog)
        assert dialog.problem() is None, dialog.problem()
        return QtWidgets.QDialog.DialogCode.Accepted
    monkeypatch.setattr(C.GiveDialog, "exec", run_dialog)
    page.message.setText("")
    page.give_secret()
    assert wait(lambda: page.message.text() and not page.message.text().endswith("…"), 30), page.message.text()
    return offered


def test_a_secret_given_to_a_running_session_as_a_git_login_reaches_its_git(page, registry, wait, monkeypatch):
    def as_login(dialog):
        dialog.host.setText("github.com")
        assert dialog.as_git_login(), "github.com: offered as a Git login first"
    offered = give(page, wait, monkeypatch, as_login)
    # Offered: the session by its name, with its run id; never the MCP server's run.
    assert [run for _, run in offered] == [RUN]
    assert SESSION in offered[0][0] and RUN in offered[0][0]
    assert registry["sent"][-1] == ("local", "git-logins", "give", "--secret", "GH_PUSH", "--user", "x-access-token",
                                    "--", RUN, "https://github.com")
    # Where the session's git reads it (its /sandbox/session/git-tokens.json): the token, for github.com.
    assert json.loads(registry["tokens"].read_text())["logins"] == [
        {"url": "https://github.com", "user": "x-access-token", "token": TOKEN}]
    text = page.message.text()
    assert f"{RUN} ({SESSION})" in text and "https://github.com" in text and "x-access-token" in text
    assert f"sandbox-ai net show {RUN}" in text, "how to check it"
    assert TOKEN not in text
    # sandbox-ai net show lists it for that run.
    out = []
    assert registry["net"].main(["show", RUN, "--json"], out=out.append) == 0
    assert json.loads("\n".join(out))["git"] == [{"url": "https://github.com", "user": "x-access-token"}]


def test_a_secret_holding_user_and_token_is_refused_with_the_run_named(page, registry, wait, monkeypatch):
    registry["secret"]("id:" + TOKEN)  # USER:TOKEN, as the user kept it first
    give(page, wait, monkeypatch, lambda dialog: dialog.host.setText("github.com"))
    text = page.message.text()
    assert not registry["tokens"].exists(), "nothing written"
    assert f"{RUN} ({SESSION})" in text and "USER:TOKEN" in text, text
    assert "id:" + TOKEN not in text and TOKEN not in text


def test_a_secret_given_to_github_as_an_api_key_is_for_its_api_host(page, registry, wait, monkeypatch):
    """GitHub's API is api.github.com: a key for github.com was never put into requests there (the proxy puts a key in
    for its very host only) - the dialog offers the key for api.github.com and says so."""
    def as_key(dialog):
        dialog.host.setText("github.com")
        dialog.as_key.setChecked(True)
        assert "api.github.com" in dialog.as_key.text()
        dialog.variable.setText("GH_TOKEN")
    give(page, wait, monkeypatch, as_key)
    assert registry["sent"][-1] == ("local", "secrets", "give", RUN, "GH_PUSH", "--api-key", "api.github.com", "--as",
                                    "GH_TOKEN")
    entries = json.loads(registry["keys"].read_text())["api_keys"]
    assert [(e["host"], e["name"], e["key"]) for e in entries] == [("api.github.com", "GH_TOKEN", TOKEN)]
    text = page.message.text()
    assert f"{RUN} ({SESSION})" in text and "api.github.com" in text and "only" in text
    assert "export GH_TOKEN=sandbox-ai-api-key-placeholder-GH_TOKEN" in text
    assert f"sandbox-ai net show {RUN}" in text and TOKEN not in text
    # The proxy puts it in for api.github.com, and only there.
    rt = registry["net"].launcher().RUNTIME
    head = (b"GET /user HTTP/1.1\r\nHost: api.github.com\r\n"
            b"Authorization: Bearer sandbox-ai-api-key-placeholder-GH_TOKEN\r\n\r\n")
    assert TOKEN.encode() in rt.api_key_head(rt.added_api_keys(registry["keys"]), "https", "api.github.com", 443,
                                             head)[0]
    assert rt.api_key_head(rt.added_api_keys(registry["keys"]), "https", "github.com", 443, head)[1] == []


def test_a_run_that_takes_no_git_login_is_said_with_its_name(page, registry, wait, monkeypatch):
    record = json.loads((registry["run"] / "run.json").read_text())
    (registry["run"] / "run.json").write_text(json.dumps({**record, "runtime_git_tokens": False}))
    give(page, wait, monkeypatch, lambda dialog: dialog.host.setText("github.com"))
    text = page.message.text()
    assert not registry["tokens"].exists()
    assert "Not given" in text and RUN in text and "takes no Git login during the run" in text, text
