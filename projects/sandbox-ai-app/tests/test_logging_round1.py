"""Logging, round 1 (user, live use: "I press Start and nothing happens, and no information" - the app re-checked in
the background without one useful line, "Still preparing what the start needs…" again and again, debug lines naming
only `Prerequisites.check_prerequisites.<locals>.read`): what was asked, what was decided and why, which command ran,
how long it took and how it ended can be followed in the log - and a secret never reaches it."""
import time

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import applog, backend, prerequisites, terminal, widgets  # noqa: E402
from test_prepare_progress import FakeSai, Page  # noqa: E402

TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
PASSWORD = "hunter" + "2pass"  # (made here: a URL with a password in it is not committed as one)


@pytest.fixture
def logged(monkeypatch):
    """Every log entry (DEBUG too) as (level, where, message)."""
    entries = []
    monkeypatch.setattr(applog, "_listeners", [])
    monkeypatch.setattr(applog, "_debug_wanted", True)
    monkeypatch.setattr(applog, "_repeat", {"key": None, "count": 0, "since": 0.0})
    monkeypatch.setattr(applog, "_changes", {})
    applog.add_listener(lambda entry: entries.append((entry["level"], entry["where"], entry["message"])))
    return entries


def text_of(entries, level=None):
    return "\n".join(f"{lv} {where}: {message}" for lv, where, message in entries if level in (None, lv))


# -- applog: secrets, repeats, changes --

def test_a_secret_never_reaches_a_log_line():
    shown = applog.argv_text(["sandbox-ai", "secrets", "add", "--token", TOKEN, "--api-key=sk-abcdefghijklmnop1234",
                              "--env", "GITHUB_TOKEN=" + TOKEN, "https://user:" + PASSWORD + "@example.org/x", TOKEN])
    assert TOKEN not in shown and "sk-abcdefghijklmnop1234" not in shown and PASSWORD not in shown, shown
    assert "--token ***" in shown and "--api-key=***" in shown and "sandbox-ai secrets add" in shown
    said = applog.redact(f"Authorization: Bearer {TOKEN}\nexport API_KEY=abc123secret\nok")
    assert TOKEN not in said and "abc123secret" not in said and said.endswith("ok")
    assert applog.tail("a\nb\n" + f"token={TOKEN}\n", lines=2) == "b\ntoken=***"
    assert "\\x1b" in applog.redact("\x1b[31mred")  # (a control character is shown escaped)


def test_the_same_line_again_is_counted_not_written(logged):
    for _ in range(5):
        applog.log("WARNING", "board", "Still preparing what the start needs…")
    applog.log("INFO", "board", "something else")
    lines = [m for _, _, m in logged]
    assert lines.count("Still preparing what the start needs…") == 1
    assert any("came 4 more times" in m for m in lines), lines
    assert lines[-1] == "something else"
    applog.log("WARN", "board", "a level named WARN is a warning")
    assert logged[-1][0] == "WARNING"


def test_a_state_is_logged_once_per_change(logged):
    for state in ("running", "running", "running", "stopped"):
        applog.changed(("session", "s1"), "INFO", "sessions", f"s1: {state}", quiet="DEBUG")
    assert [(lv, m) for lv, _, m in logged if m.startswith("s1")] == [
        ("INFO", "s1: running"), ("DEBUG", "s1: running"), ("INFO", "s1: stopped")]


# -- background(): what it does, in words --

def test_a_background_call_says_what_it_does_and_how_it_ended(qapp, logged, wait):
    class Owner(QtWidgets.QWidget):
        pass
    owner = Owner()
    results = []

    def check_prerequisites():
        def read():
            return sai.host_json("box", "images", "--json")
        return read

    class Sai:
        def host_json(self, *args):
            return []
    sai = Sai()
    widgets.background(check_prerequisites(), owner=owner, done=lambda r, e: results.append((r, e)))
    widgets.background(lambda: 1, owner=owner, what="reading the images of box", work=True, timeout=30,
                       done=lambda r, e: results.append((r, e)))
    assert wait(lambda: len(results) == 2)
    text = text_of(logged)
    assert "started: test_a_background_call_says_what_it_does_and_how_it_ended.check_prerequisites.read calling " \
           "host_json" in text, text
    assert "<locals>" not in text
    assert "INFO background: started: reading the images of box for Owner (timeout 30 s)" in text, text
    assert "INFO background: done in" in text and ": reading the images of box" in text
    owner.deleteLater()


def test_a_secret_handed_to_a_background_call_is_not_logged(qapp, logged, wait):
    """A secret's value goes to sandbox-ai on standard input - and to background() as an argument (set_secret(name,
    value)): the argument of a parameter named value, secret, token, password, key... is masked."""
    results = []

    def set_secret(name, value, host="local"):
        return 0

    def host_run(name, *args, timeout=120, input=None):
        return 0
    widgets.background(set_secret, "DEMO_TOKEN", "very-secret-value", "box", work=True,
                       done=lambda r, e: results.append(e))
    widgets.background(host_run, "box", "secrets", "add", "--token", "plain-but-secret", work=True,
                       done=lambda r, e: results.append(e))
    assert wait(lambda: len(results) == 2)
    text = text_of(logged)
    assert "very-secret-value" not in text and "plain-but-secret" not in text, text
    assert "with DEMO_TOKEN *** box" in text and "with box secrets add --token ***" in text


def test_a_failed_read_is_said_once_and_a_timeout_names_the_call(qapp, logged, wait):
    results = []

    def fails():
        raise OSError("podman: connection refused")
    for _ in range(3):
        widgets.background(fails, what="listing the sandboxes", done=lambda r, e: results.append(e))
    assert wait(lambda: len(results) == 3)
    failures = [(lv, m) for lv, _, m in logged if m.startswith("failed after")]
    assert [lv for lv, _ in failures][:2] == ["INFO", "DEBUG"] and len(failures) <= 3, failures
    assert "listing the sandboxes: OSError: podman: connection refused" in failures[0][1]
    import threading
    release = threading.Event()
    widgets.background(lambda: release.wait(5), what="asking a host that hangs", timeout=0.05,
                       done=lambda r, e: results.append(e))
    assert wait(lambda: len(results) == 4)
    release.set()
    assert isinstance(results[-1], TimeoutError)
    assert "WARNING background: no answer within 0 s: asking a host that hangs" in text_of(logged)


def test_a_task_logs_its_job_its_end_and_its_error_output(qapp, logged, wait):
    task = widgets.Task()
    ended = []
    task.call_watched(type("S", (), {"watching": lambda self, **k: __import__("contextlib").nullcontext()})(),
                      lambda: (1, "", "Error: image not known\n"), lambda r, e: ended.append(r),
                      "Building the image claude")
    assert wait(lambda: ended)
    text = text_of(logged)
    assert "task: started: Building the image claude" in text
    assert "WARNING task: ended after" in text and "(exit status 1): Building the image claude" in text
    assert "Error: image not known" in text
    task.deleteLater()


# -- what the human was told and asked --

def test_messages_and_answers_are_logged(qapp, logged, monkeypatch):
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)
    monkeypatch.setattr(widgets, "SHOWN", [])
    widgets.warn(None, "Not started", f"The image of qwen is not built (token {TOKEN})")
    assert not widgets.confirm(None, "Delete", "Delete the session s1?", "Delete")
    text = text_of(logged)
    assert "WARNING app: warning shown: Not started: The image of qwen is not built" in text, text
    assert "asked: Delete: Delete the session s1? -> answer: Cancel" in text
    assert TOKEN not in text


def test_a_status_line_is_logged_once_per_change(qapp, logged):
    label = widgets.plain_label()
    for _ in range(3):
        widgets.set_notice(label, "Not reachable: box", "error")
    widgets.set_notice(label, "Copied.", "ok")
    said = [(lv, m) for lv, _, m in logged]
    assert said == [("WARNING", "error: Not reachable: box"), ("INFO", "ok: Copied.")], said
    label.deleteLater()


# -- the commands the app runs --

def test_a_command_is_logged_with_its_status_and_error_tail_never_its_secret(logged, tmp_path):
    class Result:
        returncode, stdout, stderr = 3, "", f"line 1\nError: no such host box (token {TOKEN})\n"

    sai = backend.SandboxAI(tmp_path, runner=lambda argv, **kw: Result())
    status, _, _ = sai.call("hosts", "test", "box", f"--token={TOKEN}")
    assert status == 3
    sai.call("hosts", "test", "box", f"--token={TOKEN}")
    text = text_of(logged)
    assert TOKEN not in text, text
    assert "INFO command: exit status 3 after" in text and "sandbox-ai hosts test box --token=***" in text
    assert "Error: no such host box" in text
    assert text_of(logged, "INFO").count("exit status 3") == 1, "the same failure again: at DEBUG"
    Result.returncode = 0
    sai.call("hosts", "test", "box")
    assert "exit status 0 again" in text_of(logged, "INFO")


def test_an_error_swallowed_by_the_backend_is_logged(logged, tmp_path):
    sai = backend.SandboxAI(tmp_path, runner=lambda argv, **kw: None)
    assert sai.net_options() == set()  # (no launcher in tmp_path: an older or damaged installation)
    assert any(lv == "DEBUG" and m.startswith("SandboxAI.net_options: an old or damaged installation:")
               for lv, _, m in logged), logged


# -- the Start's prerequisites --

class Variants(FakeSai):
    def __init__(self, script, fail=False):
        super().__init__(script, terminal_setting="window")
        self.fail = fail

    def host_image_variants(self, host):
        return []

    def module(self, name):
        return type("Images", (), {"RECIPES": {"media": {}}})

    def host_json(self, host, *args, timeout=None):
        if self.fail:
            raise ValueError("ssh: connect to host box port 22: Connection refused")
        return []


class MediaPage(Page):
    def prerequisite_argv(self):
        return ["--image-variant", "media"]


@pytest.fixture
def at_once(qapp, monkeypatch):
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)
    monkeypatch.setattr(widgets, "SHOWN", [])
    monkeypatch.setattr(terminal, "available", lambda web=None: (True, ""))

    def run(function, *args, done, **kw):
        try:
            result = function(*args)
        except Exception as exc:  # noqa: BLE001
            return done(None, exc)
        return done(result, None)
    monkeypatch.setattr(prerequisites, "background", run)


def test_a_start_names_what_it_needs_and_why_it_waits(at_once, logged, tmp_path):
    page = MediaPage(Variants(tmp_path / "sandbox-ai"))
    page.check_prerequisites(then=lambda: None)
    text = text_of(logged)
    assert "start on local needs --image-variant media: read there first" in text, text
    assert "missing --image-variant media - provided first" in text
    assert "asked: Image variant: This start needs the image variant media" in text and "-> answer: Cancel" in text
    # A second Start while the first is still being prepared says what it waits for, and since when.
    page.preparing_contexts = {"local"}
    page.preparing_stages = {"local": (time.monotonic() - 42, "making image variant media")}
    assert page.check_prerequisites(then=lambda: None) is True
    assert "start on local waits: what it needs is still being prepared for local (making image variant media, " \
           "for 42 s)" in text_of(logged)
    page.deleteLater()


def test_a_prerequisite_that_cannot_be_read_is_said_with_its_reason(at_once, logged, tmp_path):
    page = MediaPage(Variants(tmp_path / "sandbox-ai", fail=True))
    started = []
    page.check_prerequisites(then=lambda: started.append(True))
    assert started == [True], "not answered: the launcher says it at the start"
    text = text_of(logged)
    assert "--image-variant on local could not be read" in text and "Connection refused" in text, text
    assert "everything it needs is there (as far as it could be read); the start goes on" in text
    page.deleteLater()


# -- the Board's Start, end to end (the real sandbox-ai, Podman faked) --

from test_live_regressions import board_page, user  # noqa: E402,F401


def test_a_start_refused_for_a_missing_image_logs_which_image_and_why(board_page, user, logged, monkeypatch):
    monkeypatch.setenv("SANDBOX_AI_NO_AUTO_BUILD", "1")
    page = board_page.show_board("qwen")
    assert page.errors(), "the launcher refuses: the image is not built"
    assert page.start() is False and board_page.started == []
    text = text_of(logged)
    assert "INFO board: Start: the board of" in text and "with qwen on local" in text, text
    assert "the start waits for the launcher's check again" in text
    assert "the launcher refuses the board's options:" in text and "localhost/qwen-sandbox:latest" in text
    refusal = [m for lv, _, m in logged if m.startswith("warning shown: Not started")]
    assert refusal and "qwen-sandbox" in refusal[-1] and "sandbox-ai build qwen" in refusal[-1], text


# -- state changes, terminals, connections --

class Row:
    def __init__(self, key, name, state, host="local", kind="session"):
        self.key, self.name, self.state, self.host, self.kind = key, name, state, host, kind


def test_sandboxes_are_logged_once_per_change_of_their_state(logged):
    from sandbox_ai_app import ui
    page = type("P", (), {})()
    ui.log_instance_changes(page, "local", [Row("a", "s1", "starting")])
    ui.log_instance_changes(page, "local", [Row("a", "s1", "starting")])
    ui.log_instance_changes(page, "local", [Row("a", "s1", "running"), Row("b", "box-run", "running", host="box")])
    ui.log_instance_changes(page, "local", [])
    said = [m for _, where, m in logged if where == "sandboxes"]
    assert said == ["session s1 on local: starting (listed)", "session s1 on local: starting -> running",
                    "session s1 on local: no longer listed (was running)"], said


def test_a_terminal_command_is_logged_from_its_start_to_its_end(qapp, logged, wait):
    from sandbox_ai_app import pty_bridge
    bridge = pty_bridge.PtyBridge(["sh", "-c", "exit 3"])
    statuses = []
    bridge.exited.connect(statuses.append)
    assert wait(lambda: statuses == [3])
    text = text_of(logged, "INFO")
    assert "command started in a terminal tab (pid" in text and "sh -c 'exit 3'" in text, text
    assert "the terminal's command ended (pid" in text and "exit status 3 after" in text
    bridge.close()


def test_the_bar_of_a_sign_in_never_logs_its_code(qapp, logged):
    from sandbox_ai_app import terminal_tools
    bar = terminal_tools.TerminalBar()
    bar.ask("Open https://auth.example.org/cb?code=s3cr3tc0de&state=xyz ?", [("Open", lambda: None)])
    assert "s3cr3tc0de" not in text_of(logged) and "https://auth.example.org/cb?…" in text_of(logged)
    bar.logged = False
    bar.ask("Signed in. The code for the terminal: s3cr3tc0de", [("Copy", lambda: None)])
    assert "s3cr3tc0de" not in text_of(logged)
    bar.deleteLater()


def test_a_status_line_of_a_page_is_logged(qapp, logged):
    label = widgets.plain_label(log="files")
    label.setText("Reading…")
    label.setText("Cannot open /x: Permission denied")
    label.setText("Cannot open /x: Permission denied")
    assert [(lv, where, m) for lv, where, m in logged] == [
        ("DEBUG", "files", "status: Reading…"), ("INFO", "files", "status: Cannot open /x: Permission denied")]
    label.deleteLater()


def test_a_changed_setting_is_logged_without_a_secret(logged, tmp_path, monkeypatch):
    sai = backend.SandboxAI(tmp_path, runner=lambda argv, **kw: None)
    monkeypatch.setattr(sai, "settings_file", lambda: tmp_path / "settings.json")
    sai.save_settings({"terminal": "window", "api_token": TOKEN})
    sai.save_settings({"terminal": "window"})
    said = [m for _, where, m in logged if where == "settings"]
    assert said == ["changed: terminal='window', api_token=***"], said


def test_an_sftp_connection_that_fails_says_why(logged, tmp_path):
    from sandbox_ai_app import remote
    script = tmp_path / "ssh"
    script.write_text("#!/bin/sh\necho 'ssh: connect to host box port 22: Connection refused' >&2\nexit 255\n")
    script.chmod(0o755)
    with pytest.raises(remote.RemoteError):
        remote.Sftp([str(script)], remote.Target("box"), timeout=5)
    text = text_of(logged)
    assert "INFO remote: SFTP to box: connecting:" in text, text
    assert "WARNING remote: SFTP to box: not connected after" in text and "Connection refused" in text
