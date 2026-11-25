"""Memory… and Session files… offscreen (`sandbox-ai session memory|files|export`): the Sessions page's buttons and
right-click menu (a start of this machine or a host, running or kept; never an MCP server or one that kept no HOME),
the dialog against a stand-in that records what it was asked (the list, a file's text, a conversation, Find, In full,
Which copy, Export… of a kept start of this machine), what the agent wrote shown as plain text, and once end to end
through the real sandbox-ai of this checkout. The GUI thread never runs the commands (BLOCKING_SAI:
tests/test_references)."""
import json
import os
from pathlib import Path
import tarfile

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, widgets  # noqa: E402

from test_accessibility import unnamed  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


def at_once(function, *args, done, **kw):
    """widgets.background, but on this thread: DONE gets the result or the error at once."""
    try:
        result = function(*args)
    except Exception as exc:  # noqa: BLE001 - as background hands it on
        done(None, exc)
        return
    done(result, None)


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    (tmp_path / "cwd").mkdir()
    monkeypatch.chdir(tmp_path / "cwd")
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    from sandbox_ai_app import session_inspect_dialog
    monkeypatch.setattr(session_inspect_dialog, "background", at_once)
    ASKED[:] = []

    def ask_first(parent, action, title, text, accept, detail=None, details=None, reject=None):
        assert backend.needs_confirm(action, detail) is not None, action
        ASKED.append((action, text))
        return True
    monkeypatch.setattr(session_inspect_dialog, "ask_first", ask_first)
    return tmp_path


ASKED = []  # (action, question) of every confirmation the dialog asked for (env answers yes)


@pytest.fixture
def recorded(env, monkeypatch):
    """A SandboxAI whose commands are recorded and answered from ANSWERS (a list, first first)."""
    sai = backend.SandboxAI()
    sai.calls, sai.answers = [], []

    def call(*args, timeout=120, input=None, argv=None):
        sai.calls.append(args)
        return sai.answers.pop(0) if sai.answers else (0, "{}", "")
    monkeypatch.setattr(sai, "call", call)
    return sai


def start(name="web", state="stopped", host="local", kind="session"):
    if host != "local":
        return backend.Instance(kind, f"remote:{host}:{name}", name, host=host, state=state, agent="claude")
    return backend.Instance(kind, f"{kind}:{name}", name, session=name if kind == "session" else None, state=state,
                            agent="claude", mcp=name if kind == "mcp" else None)


MEMORY = {"name": "web", "id": "web", "agent": "claude", "state": "running", "source": "home", "path": "/h/home",
          "memory_file": ".claude/CLAUDE.md", "notes": [], "complete": True,
          "files": [{"name": ".claude/CLAUDE.md", "size": 30, "modified": "2026-11-18T10:00:00Z"},
                    {"name": ".claude/projects/-work/memory/MEMORY.md", "size": 12, "modified": "2026-11-18T10:01:00Z"}]}
HOSTILE = "<b>bold</b> \x1b]0;title\x07 ‮evil\nsecond line"
FILES = {"name": "web", "id": "web", "agent": "claude", "state": "running", "home": "/h/home", "unverified": [".hermes"],
         "files": [{"name": ".claude/projects/-work/s1.jsonl", "agent": "claude", "size": 2048,
                    "modified": "2026-11-18T10:00:00Z", "format": "claude", "verified": True},
                   {"name": ".hermes/sessions/a.jsonl", "agent": "hermes", "size": 10,
                    "modified": "2026-11-18T09:00:00Z", "format": "jsonl", "verified": False}]}
TALK = {**FILES, "files": FILES["files"][:1], "file": FILES["files"][0], "notes": ["1 line(s) are no JSON object"],
        "messages": [{"role": "user", "time": "2026-11-18T10:00:00Z", "text": "list the files", "tool": None,
                      "error": False, "shortened": False},
                     {"role": "tool-call", "time": "2026-11-18T10:00:02Z", "text": '{"command": "ls"}', "tool": "Bash",
                      "error": False, "shortened": False},
                     {"role": "assistant", "time": "2026-11-18T10:00:03Z", "text": HOSTILE, "tool": None,
                      "error": False, "shortened": False}]}


def test_the_file_names_are_checked():
    assert backend.listed_file(".claude/CLAUDE.md") == ".claude/CLAUDE.md"
    for bad in ("", "/etc/passwd", "--all", "a\nb", None, "x" * 5000):
        with pytest.raises(ValueError):
            backend.listed_file(bad)


def test_the_buttons_and_the_right_click_for_each_kind_of_start(qapp, recorded, env, monkeypatch):
    from sandbox_ai_app import ui
    monkeypatch.setattr(recorded, "hosts_supported", lambda: True)
    monkeypatch.setattr(recorded, "host_entries", lambda: [{"name": "local", "kind": "local"},
                                                             {"name": "ct1", "kind": "ssh", "ssh": "a@192.0.2.1"}])
    ended = backend.Instance("run", "ended:local:x", "x", run="claude-1", state="ended")
    for instance, why in ((start(), None), (start(state="running"), None), (start(host="ct1"), None),
                          (backend.Instance("run", "run:x", "x", run="claude-1", state="running"), None),
                          (ended, "without keeping its HOME"),
                          (backend.Instance("run", "run:y", "y", run="claude-2", state="stopped"),
                           "without keeping its HOME"),
                          (start("files", "running", kind="mcp"), "an MCP server"), (None, "Select a row")):
        page = ui.SandboxesPage(recorded, lambda *a, **k: None)
        monkeypatch.setattr(page, "current", lambda instance=instance: instance)
        page.update_buttons()
        for key, text in (("memory", "Memory…"), ("files", "Session files…")):
            button = page.transfer_buttons[key]
            if why is None:
                assert button.isEnabled(), (instance.key, key, button.toolTip())
                entries = widgets.menu_entries(widgets.menu_of(page.table))
                assert any(text in str(entry) for entry in entries), entries
            else:
                assert not button.isEnabled() and why in button.toolTip(), (why, button.toolTip())
        page.close()
    opened = []
    from sandbox_ai_app import session_inspect_dialog
    monkeypatch.setattr(session_inspect_dialog.SessionInspectDialog, "exec",
                        lambda dialog: opened.append((dialog.name, dialog.kind, dialog.host, dialog.exportable)) or 0)
    for instance, key in ((start(state="running"), "memory"), (start(host="ct1"), "files")):
        page = ui.SandboxesPage(recorded, lambda *a, **k: None)
        monkeypatch.setattr(page, "current", lambda instance=instance: instance)
        page.update_buttons()
        recorded.answers.append((0, json.dumps(MEMORY if key == "memory" else FILES), ""))
        page.transfer_buttons[key].click()
        page.close()
    assert opened == [("web", "memory", "local", True), ("web", "files", "ct1", False)]
    assert ("session", "memory", "web", "--from=auto", "--json") in recorded.calls
    assert any(call[:3] == ("hosts", "run", "ct1") and "session" in call and "files" in call
               for call in recorded.calls)


def test_the_memory_dialog_lists_shows_finds_and_switches_the_copy(qapp, recorded, env):
    from sandbox_ai_app.session_inspect_dialog import SessionInspectDialog
    recorded.answers.append((0, json.dumps(MEMORY), ""))
    dialog = SessionInspectDialog(None, recorded, "web", kind="memory", exportable=True)
    assert unnamed(dialog) == []
    assert dialog.table.rowCount() == 2 and "in its HOME: /h/home" in dialog.where.text()
    recorded.answers.append((0, json.dumps({**MEMORY, "files": [{**MEMORY["files"][0], "text": HOSTILE,
                                                                  "binary": False}]}), ""))
    dialog.table.selectRow(0)
    assert recorded.calls[-1] == ("session", "memory", "web", "--from=auto", "--json", "--file=.claude/CLAUDE.md")
    shown = dialog.text.toPlainText()
    assert "<b>bold</b>" in shown and "\\x1b]0;title\\x07" in shown and "\\u202e" in shown
    assert "\x1b" not in shown and "‮" not in shown
    assert isinstance(dialog.text, QtWidgets.QPlainTextEdit), "plain text: never read as markup"
    dialog.search.setText("second")
    dialog.find_button.click()
    assert dialog.text.textCursor().selectedText() == "second"
    recorded.answers.append((0, json.dumps({**MEMORY, "source": "store", "path": "/state/memory/p/claude"}), ""))
    dialog.which.setCurrentIndex(2)
    assert recorded.calls[-1][:4] == ("session", "memory", "web", "--from=store")
    assert "memory store: /state/memory/p/claude" in dialog.where.text()
    recorded.answers.append((1, "", "sandbox-ai session: no memory store of web's project yet"))
    dialog.load()
    assert "no memory store" in dialog.status.text()
    dialog.deleteLater()


def test_the_session_files_dialog_shows_a_conversation_in_full_and_exports(qapp, recorded, env, monkeypatch):
    from sandbox_ai_app import state
    from sandbox_ai_app.session_inspect_dialog import SessionInspectDialog
    recorded.answers.append((0, json.dumps(FILES), ""))
    dialog = SessionInspectDialog(None, recorded, "web", kind="files", exportable=True)
    assert dialog.table.rowCount() == 2 and dialog.table.item(1, 1).text() == "hermes *"
    assert "not verified" in dialog.status.text()
    recorded.answers.append((0, json.dumps(TALK), ""))
    dialog.table.selectRow(0)
    assert recorded.calls[-1] == ("session", "files", "web", "--json", "--file=.claude/projects/-work/s1.jsonl")
    shown = dialog.text.toPlainText()
    assert "== 2026-11-18 10:00:00  user\nlist the files" in shown and "tool call: Bash" in shown
    assert "<b>bold</b>" in shown and "\x1b" not in shown and "note: 1 line(s) are no JSON object" in shown
    recorded.answers.append((0, json.dumps(TALK), ""))
    dialog.full.setChecked(True)
    assert recorded.calls[-1][-1] == "--full"
    texts = [str(entry) for entry in widgets.menu_entries(widgets.menu_of(dialog.table))]
    assert any("Export…" in text for text in texts) and any("Refresh" in text for text in texts)
    target = env / "web.tar.gz"
    monkeypatch.setattr(state, "choose_save", lambda *a, **k: str(target))
    recorded.answers.append((0, json.dumps({"name": "web", "path": str(target), "files": 12, "bytes": 4096,
                                            "left_out": [{"path": ".claude/.credentials.json",
                                                          "why": "an agent's login"}]}), ""))
    dialog.export_button.click()
    assert recorded.calls[-1] == ("session", "export", "web", "--json", "--", str(target))
    [(action, question)] = ASKED
    assert action == "session-export" and str(target) in question and "Logins and keys are left out" in question
    assert "12 files" in dialog.status.text() and ".claude/.credentials.json: an agent's login" in dialog.status.text()
    target.write_text("there")
    calls = len(recorded.calls)
    dialog.export_button.click()
    assert len(recorded.calls) == calls and "is there already" in dialog.status.text()
    dialog.deleteLater()
    other = SessionInspectDialog(None, recorded, "web", kind="files", host="ct1", exportable=False,
                                 export_why="A start of ct1: its archive is written there")
    assert not other.export_button.isEnabled() and "written there" in other.export_button.toolTip()
    other.deleteLater()


SECRET_FILES = {**FILES, "hidden": 0, "files": [
    {**FILES["files"][0], "secret": None},
    {"name": ".claude/projects/-work/token-notes.jsonl", "agent": "claude", "size": 20,
     "modified": "2026-11-18T08:00:00Z", "format": "claude", "verified": True,
     "secret": "a login, token or key (by its name)"}]}


def test_a_login_or_key_is_listed_marked_and_shown_after_a_question(qapp, recorded, env, monkeypatch):
    """User 2026-11-19: such files are ALWAYS visible by default. The list marks one with why; selecting it asks first
    (show-secret-file) and only then reads it with --show-secret, with a warning above its text; declined, nothing is
    read. Asked once per file while the dialog is open (In full does not ask again)."""
    from sandbox_ai_app import session_inspect_dialog
    from sandbox_ai_app.session_inspect_dialog import SessionInspectDialog
    recorded.answers.append((0, json.dumps(SECRET_FILES), ""))
    dialog = SessionInspectDialog(None, recorded, "web", kind="files", exportable=True)
    assert dialog.table.rowCount() == 2
    assert dialog.table.item(1, 4).text() == "a login, token or key (by its name)" and dialog.table.item(0, 4).text() == ""
    answers = [False]

    def ask_first(parent, action, title, text, accept, detail=None, details=None, reject=None):
        assert backend.needs_confirm(action, detail) is not None, action
        ASKED.append((action, text))
        return answers.pop(0) if answers else True
    monkeypatch.setattr(session_inspect_dialog, "ask_first", ask_first)
    calls = len(recorded.calls)
    dialog.table.selectRow(1)
    assert ASKED[-1][0] == "show-secret-file" and "token-notes.jsonl" in ASKED[-1][1]
    assert len(recorded.calls) == calls, "declined: nothing is read"
    assert "only when you ask" in dialog.text.toPlainText()
    dialog.table.selectRow(0)
    recorded.answers.append((0, json.dumps({**TALK, "file": SECRET_FILES["files"][1]}), ""))
    dialog.table.selectRow(1)
    assert recorded.calls[-1] == ("session", "files", "web", "--json", "--file=.claude/projects/-work/token-notes.jsonl",
                                  "--show-secret")
    assert dialog.text.toPlainText().startswith("Warning: it holds a login or key")
    asked = len(ASKED)
    recorded.answers.append((0, json.dumps({**TALK, "file": SECRET_FILES["files"][1]}), ""))
    dialog.full.setChecked(True)
    assert len(ASKED) == asked and recorded.calls[-1][-2:] == ("--full", "--show-secret")
    dialog.deleteLater()


def test_the_list_says_how_many_the_setting_hides(qapp, recorded, env):
    from sandbox_ai_app.session_inspect_dialog import SessionInspectDialog
    recorded.answers.append((0, json.dumps({**SECRET_FILES, "hidden": 3, "files": SECRET_FILES["files"][:1]}), ""))
    dialog = SessionInspectDialog(None, recorded, "web", kind="files", exportable=True)
    assert "3 files that hold a login or key are not listed" in dialog.status.text()
    dialog.deleteLater()


def test_an_export_names_the_secrets_left_out_and_takes_them_in_only_when_asked(qapp, recorded, env, monkeypatch):
    from sandbox_ai_app import state
    from sandbox_ai_app.session_inspect_dialog import SessionInspectDialog
    recorded.answers.append((0, json.dumps(SECRET_FILES), ""))
    dialog = SessionInspectDialog(None, recorded, "web", kind="files", exportable=True)
    target = env / "web.tar.gz"
    monkeypatch.setattr(state, "choose_save", lambda *a, **k: str(target))
    recorded.answers.append((0, json.dumps({"name": "web", "path": str(target), "files": 3, "bytes": 10,
                                            "include_secrets": False, "secrets_left_out": [".git-credentials"],
                                            "left_out": [{"path": ".git-credentials", "secret": True,
                                                          "why": "a login, token or key (by its name)"},
                                                         {"path": "out", "why": "a link: not followed"}]}), ""))
    dialog.export_button.click()
    assert recorded.calls[-1] == ("session", "export", "web", "--json", "--", str(target))
    status = dialog.status.text()
    assert "Logins, keys and secrets left out (1)" in status and ".git-credentials" in status and "out: a link" in status
    assert not dialog.with_secrets.isChecked() and dialog.with_secrets.accessibleName()
    target2 = env / "with.tar.gz"
    monkeypatch.setattr(state, "choose_save", lambda *a, **k: str(target2))
    dialog.with_secrets.setChecked(True)
    recorded.answers.append((0, json.dumps({"name": "web", "path": str(target2), "files": 4, "bytes": 12,
                                            "include_secrets": True, "secrets_left_out": [], "left_out": []}), ""))
    dialog.export_button.click()
    action, question = ASKED[-1]
    assert action == "session-export-secrets" and "log in as you" in question
    assert recorded.calls[-1] == ("session", "export", "web", "--json", "--include-secrets", "--yes", "--", str(target2))
    assert "whoever has it can log in as you" in dialog.status.text()
    assert not dialog.with_secrets.isChecked(), "each export with logins and keys is chosen anew"
    dialog.deleteLater()


def test_the_setting_in_settings_and_for_other_hosts(qapp, recorded, env, monkeypatch):
    """Settings > Privacy: Hide logins and keys in file views (hide_secrets of this machine's defaults); on, the views
    of another host are asked with --hide-secrets too (an older sandbox-ai there, which never lists them, is asked
    again without it)."""
    from sandbox_ai_app.settings import SettingsDialog
    dialog = SettingsDialog(recorded)
    box = dialog.fields["hide_secrets"]
    assert not box.isChecked()
    box.setChecked(True)
    assert json.loads(recorded.sandbox_defaults_file().read_text())["hide_secrets"] is True
    dialog.deleteLater()
    recorded.answers += [(2, "", "sandbox-ai: error: unrecognized arguments: --hide-secrets"), (0, json.dumps(FILES), "")]
    recorded.session_files("web", "ct1")
    assert "--hide-secrets" in recorded.calls[-2] and "--hide-secrets" not in recorded.calls[-1]
    assert recorded.calls[-1][:3] == ("hosts", "run", "ct1")
    dialog = SettingsDialog(recorded)
    assert dialog.fields["hide_secrets"].isChecked()
    dialog.fields["hide_secrets"].setChecked(False)
    assert "hide_secrets" not in json.loads(recorded.sandbox_defaults_file().read_text())
    dialog.deleteLater()


def test_e2e_through_the_real_sandbox_ai(qapp, env):
    """The real `sandbox-ai session memory|files|export` of this checkout, for a kept start of this machine."""
    directory = env / "state" / "sandbox-ai" / "sessions" / "web"
    home = directory / "home"
    (home / ".claude/projects/-work").mkdir(parents=True)
    os.chmod(home, 0o700)
    (directory / "session.json").write_text(json.dumps({
        "agent": "claude", "options": ["--workspace", "/srv/project", "--no-memory"], "agent_args": [],
        "backend": "podman", "workspace": "/srv/project", "cwd": "/", "created": "2026-11-18T00:00:00Z",
        "keep": True}))
    (home / ".claude/CLAUDE.md").write_text("remember <i>this</i>\n")
    (home / ".claude/projects/-work/s1.jsonl").write_text(json.dumps(
        {"type": "user", "timestamp": "2026-11-18T10:00:00Z", "message": {"content": "hello there"}}) + "\n")
    (home / ".git-credentials").write_text("https://u:tok@example.org\n")
    from sandbox_ai_app.session_inspect_dialog import SessionInspectDialog
    sai = backend.SandboxAI()
    dialog = SessionInspectDialog(None, sai, "web", kind="memory", exportable=True)
    assert dialog.table.rowCount() == 1, dialog.status.text()
    dialog.table.selectRow(0)
    assert dialog.text.toPlainText() == "remember <i>this</i>\n"
    dialog.deleteLater()
    dialog = SessionInspectDialog(None, sai, "web", kind="files", exportable=True)
    assert dialog.table.rowCount() == 1, dialog.status.text()
    dialog.table.selectRow(0)
    assert "user\nhello there" in dialog.text.toPlainText()
    target = env / "out.tar.gz"
    from sandbox_ai_app import state
    state_choose = state.choose_save
    try:
        state.choose_save = lambda *a, **k: str(target)
        dialog.export_button.click()
    finally:
        state.choose_save = state_choose
    assert tarfile.is_tarfile(target), dialog.status.text()
    with tarfile.open(target) as tar:
        assert "web/home/.claude/projects/-work/s1.jsonl" in tar.getnames()
        assert "web/home/.git-credentials" not in tar.getnames()
    assert "Logins, keys and secrets left out (1)" in dialog.status.text() and ".git-credentials" in dialog.status.text()
    dialog.deleteLater()
    assert Path(target).stat().st_size > 0
