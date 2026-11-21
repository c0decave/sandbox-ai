"""User 2026-11-19: "Bei Menü Freigaben: 'immer erlauben', 'immer auch für Folgesessions' ebenfalls als eigene Buttons
hinzufügen." The Approvals page answers with one button per scope - Allow once, Always allow (every same request of
this run), Always, also for later sessions (remembered for the project) - instead of a scope box beside one Allow;
the Questions window, the table's right-click menu and a question's notification offer the same; a scope the caps
forbid (the policy, the run's security mode, the kind: a canary's alarm, a page to open, a push or a tool call) is a
disabled button that says why; keys for each; the note after an answer names the scope."""
import json
import os
import time

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtCore import QEvent, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402

from sandbox_ai_app import backend, i18n, pages, short_notices, widgets  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    monkeypatch.setattr(short_notices, "SHOWN", [])
    return tmp_path


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def fake_run(env, name="opencode-1", mode=None):
    directory = env / "runtime" / "sandbox-ai" / "runs" / name
    (directory / "approvals").mkdir(parents=True)
    (env / "project").mkdir(exist_ok=True)
    (directory / "run.json").write_text(json.dumps({
        "run": name, "agent": "opencode", "backend": "podman", "workspace": str(env / "project"), "pid": os.getpid(),
        "session": None, "session_dir": str(env / "s" / name), "started": "2026-11-19T04:00:00Z", "proxy": True,
        "ask": "on", "ask_timeout": 60, **({"mode": mode} if mode else {})}))
    return directory / "approvals"


def question(directory, ident, done=None, **fields):
    (directory / f"{ident}.json").write_text(json.dumps({"id": ident, "time": now(), "timeout": 60, **fields}))
    if done:
        (directory / f"{ident}.done").write_text(json.dumps(done))


CONNECT = {"kind": "connect", "destination": "192.0.2.7:8443", "address": "192.0.2.7", "port": 8443}
HTTP = {"kind": "http", "method": "POST", "url": "https://registry.example.org/x", "rule": "writes",
        "ruleset": "rules.json"}
CANARY = {"kind": "canary", "canary": "aws-keys", "summary": "a decoy file was read: ~/.aws/credentials"}


@pytest.fixture
def page(qapp, env):
    made = pages.ApprovalsPage(backend.SandboxAI(), interval=0, remote_interval=0)
    yield made
    made.timer.stop()
    made.blocked.timer.stop()
    made.deleteLater()


def answers_to(page, monkeypatch):
    calls = []

    def answer(ident, decision, scope="once"):
        calls.append((ident, decision, scope))
        return 0, "done", ""
    monkeypatch.setattr(page.sai, "answer", answer)
    return calls


def select(page, ident):
    row = next(r for r in range(page.open.rowCount()) if pages.row_key(page.open, r)[2] == ident)
    page.open.clearSelection()
    page.open.selectRow(row)


def allow_buttons(owner):
    return [owner.allow_button, owner.allow_run_button, owner.allow_always_button]


def test_one_button_per_scope_and_no_scope_box(env, qapp, page, wait, monkeypatch):
    directory = fake_run(env)
    for n in (1, 2, 3):
        question(directory, f"1763500000-1-{n}", **{**CONNECT, "address": f"192.0.2.{n}",
                                                    "destination": f"192.0.2.{n}:8443"})
    page.poll()
    calls = answers_to(page, monkeypatch)
    assert [b.text() for b in allow_buttons(page)] == ["Allow once", "Always allow", "Always, also for later sessions"]
    assert not hasattr(page, "scope"), "one button per scope: no box beside Allow"
    assert not [box for box in page.open_box.findChildren(QtWidgets.QComboBox)
                if box.accessibleName() == "Scope of an allow"]
    select(page, "1763500000-1-1")
    assert "this time only" in page.allow_button.toolTip()
    assert "this run" in page.allow_run_button.toolTip() and "Ctrl+Shift+Return" in page.allow_run_button.toolTip()
    assert "later" in page.allow_always_button.toolTip() and "Ctrl+Shift+A" in page.allow_always_button.toolTip()
    for n, button, scope in ((1, page.allow_button, "once"), (2, page.allow_run_button, "run"),
                             (3, page.allow_always_button, "always")):
        select(page, f"1763500000-1-{n}")
        assert button.isEnabled(), button.toolTip()
        button.click()
        assert wait(lambda: len(calls) == n and not page.answers)
        assert calls[-1] == (f"1763500000-1-{n}", "allow", scope)
        assert page.message.text() == pages.answered_note("allow", scope)
    assert widgets.SHOWN == [], "each one click, no question first"


def test_the_note_after_an_answer_names_the_scope(qapp):
    assert pages.answered_note("allow", "once") == "Allowed once."
    assert pages.answered_note("allow", "run") == "Always allowed in this run (this session)."
    assert pages.answered_note("allow", "always") == "Always allowed, also for later sessions."
    assert pages.answered_note("deny", "once") == "Refused once."
    assert pages.answered_note("deny", "run") == "Refused for this run."
    assert pages.answered_note("deny", "always") == "Blocked always."


def test_caps_disable_a_scope_with_the_reason(env, qapp, page, monkeypatch):
    directory = fake_run(env)
    question(directory, "1763500000-2-1", **HTTP)
    question(directory, "1763500000-2-2", **CANARY)
    question(directory, "1763500000-2-3", kind="browser", url="https://login.example.org/device")
    page.poll()
    select(page, "1763500000-2-1")  # a request: at most for the run
    assert page.allow_button.isEnabled() and page.allow_run_button.isEnabled()
    assert not page.allow_always_button.isEnabled()
    assert "at most for this run" in page.allow_always_button.toolTip()
    select(page, "1763500000-2-2")  # a canary's alarm: this once
    assert page.allow_button.isEnabled()
    assert not page.allow_run_button.isEnabled() and not page.allow_always_button.isEnabled()
    assert "canary" in page.allow_run_button.toolTip() and "canary" in page.allow_always_button.toolTip()
    select(page, "1763500000-2-3")  # a page to open: once
    assert not page.allow_run_button.isEnabled() and "opened once" in page.allow_always_button.toolTip()
    # The administrator's policy: at most once - a connection gets Allow once only, said why.
    monkeypatch.setattr(page.sai, "max_scope", lambda: "once")
    page.poll()
    select(page, "1763500000-2-1")
    assert page.allow_button.isEnabled() and not page.allow_run_button.isEnabled()
    assert "at most" in page.allow_run_button.toolTip()
    # Even when asked by its key or the API: a scope the caps forbid is not sent.
    calls = answers_to(page, monkeypatch)
    page.answer("allow", "run")
    assert calls == [] and "at most" in page.message.text()


def test_the_run_s_security_mode_caps_the_scope(env, qapp, page):
    directory = fake_run(env, mode={"approval_scope": "run"})
    question(directory, "1763500000-3-1", **CONNECT)
    page.poll()
    select(page, "1763500000-3-1")
    assert page.allow_run_button.isEnabled() and not page.allow_always_button.isEnabled()
    assert "security mode" in page.allow_always_button.toolTip()


def test_an_answered_question_is_allowed_again_for_the_run_or_later_sessions(env, qapp, page, wait, monkeypatch):
    directory = fake_run(env)
    question(directory, "1763500000-4-1", done={"decision": "deny", "scope": "run", "reason": "answered"}, **CONNECT)
    page.poll()
    calls = answers_to(page, monkeypatch)
    page.state_tabs.setCurrentIndex([page.state_tabs.tabData(i)
                                     for i in range(page.state_tabs.count())].index("refused"))
    select(page, "1763500000-4-1")
    assert not page.allow_button.isEnabled() and "Answered already" in page.allow_button.toolTip()
    assert page.allow_run_button.isEnabled() and page.allow_always_button.isEnabled()
    page.allow_run_button.click()
    assert wait(lambda: calls and not page.answers)
    assert calls == [("1763500000-4-1", "allow", "run")]
    page.undo_button.click()
    assert wait(lambda: len(calls) == 2 and not page.answers)
    select(page, "1763500000-4-1")
    page.allow_always_button.click()
    assert wait(lambda: len(calls) == 3 and not page.answers)
    assert calls[-1] == ("1763500000-4-1", "allow", "always")


def key(key, modifiers):
    return QKeyEvent(QEvent.Type.KeyPress, key, modifiers)


def test_keys_for_each_scope(env, qapp, page, monkeypatch):
    control_shift = Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier
    assert pages.shortcut_of(key(Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)) == "allow"
    assert pages.shortcut_of(key(Qt.Key.Key_Return, control_shift)) == "always"
    assert pages.shortcut_of(key(Qt.Key.Key_Enter, control_shift | Qt.KeyboardModifier.KeypadModifier)) == "always"
    assert pages.shortcut_of(key(Qt.Key.Key_A, control_shift)) == "always-later"
    assert pages.shortcut_of(key(Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier)) == "allow-run"
    directory = fake_run(env)
    question(directory, "1763500000-5-1", **CONNECT)
    question(directory, "1763500000-5-2", **CANARY)
    page.poll()
    answers = []
    monkeypatch.setattr(page, "answer", lambda decision, scope=None, **kwargs: answers.append((decision, scope)))
    select(page, "1763500000-5-1")
    for action in ("allow", "always", "always-later", "allow-run"):
        page.shortcut(action)
    assert answers == [("allow", "once"), ("allow", "run"), ("allow", "always"), ("allow", "run")]
    # A canary's alarm: Enter continues it this once - never "for the run" (regression: Enter allowed it for the run).
    answers.clear()
    select(page, "1763500000-5-2")
    page.shortcut("allow-run")
    page.shortcut("always")
    page.shortcut("always-later")
    assert answers == [("allow", "once")]


def test_the_table_s_menu_mirrors_the_buttons(env, qapp, page):
    entries = page.open.menu_entries()
    assert entries[:5] == [page.allow_button, page.allow_run_button, page.allow_always_button, page.deny_button,
                           page.deny_once_button]


def test_the_questions_window_has_the_same_three_buttons(env, qapp, page, monkeypatch):
    directory = fake_run(env)
    question(directory, "1763500000-6-1", **CONNECT)
    page.poll()
    answers = []
    monkeypatch.setattr(page, "answer", lambda decision, scope=None, **kwargs: answers.append((decision, scope)))
    window = pages.QuestionsWindow(page)
    try:
        window.show_questions()
        assert [b.text() for b in allow_buttons(window)] == ["Allow once", "Always allow",
                                                             "Always, also for later sessions"]
        assert all(b.isEnabled() for b in allow_buttons(window))
        assert window.table.menu_entries()[:3] == allow_buttons(window)
        for button in allow_buttons(window):
            button.click()
        assert answers == [("allow", "once"), ("allow", "run"), ("allow", "always")]
        answers.clear()
        window.shortcut("always")
        window.shortcut("always-later")
        window.shortcut("allow-run")  # Enter: what its first Allow says (once)
        assert answers == [("allow", "run"), ("allow", "always"), ("allow", "once")]
    finally:
        window.deleteLater()


def test_the_questions_window_disables_what_the_caps_forbid(env, qapp, page):
    directory = fake_run(env)
    question(directory, "1763500000-7-1", **HTTP)
    page.poll()
    window = pages.QuestionsWindow(page)
    try:
        window.show_questions()
        assert window.allow_button.isEnabled() and window.allow_run_button.isEnabled()
        assert not window.allow_always_button.isEnabled()
        assert "at most for this run" in window.allow_always_button.toolTip()
    finally:
        window.deleteLater()


def test_another_host_s_cap_is_that_host_s(env, qapp, page, wait, monkeypatch):
    remote = backend.RemoteQuestion("ct1", {"id": "1763500000-8-1", "run": "claude-r", "kind": "connect",
                                     "question": "connect to 192.0.2.7:443", "max_scope": "run", "seconds_left": 40})
    entry = ("claude-r", remote, "ct1")
    assert page.scope_refused(entry, "run") is None
    assert "ct1" in page.scope_refused(entry, "always") and "at most" in page.scope_refused(entry, "always")
    assert page.scope_refused(entry, "once") is None


def test_german_texts(qapp, env, page):
    i18n.set_language("de")
    try:
        assert i18n.tr("Allow once") == "Einmal erlauben"
        assert i18n.tr("Always allow") == "Immer erlauben"
        assert i18n.tr("Always, also for later sessions") == "Immer, auch für Folgesitzungen"
        assert pages.answered_note("allow", "run") == "Immer erlaubt in diesem Lauf (dieser Sitzung)."
        assert pages.answered_note("allow", "always") == "Immer erlaubt, auch für Folgesitzungen."
        made = pages.ApprovalsPage(backend.SandboxAI(), interval=0, remote_interval=0)
        try:
            assert [b.text() for b in allow_buttons(made)] == ["Einmal erlauben", "Immer erlauben",
                                                               "Immer, auch für Folgesitzungen"]
        finally:
            made.timer.stop()
            made.blocked.timer.stop()
            made.deleteLater()
    finally:
        i18n.set_language("en")


# -- a question's notification (a themed toast) --

@pytest.fixture
def window(qapp, env, monkeypatch):
    from PySide6 import QtCore
    from sandbox_ai_app import terminal
    from sandbox_ai_app.ui import MainWindow
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    monkeypatch.setattr(terminal, "available", lambda web=None: (False, "not in this test"))
    monkeypatch.setattr(backend, "setting", lambda key, *a: {"notify": True, "notifications": True,
                                                             "notify_style": "themed"}.get(key, backend.SETTINGS.get(key)))
    w = MainWindow(backend.SandboxAI(), check_updates=False, approvals_interval=0)
    if w.tray is not None:
        w.tray.hide()
        w.tray = None
    yield w
    w.shutdown()
    w.close()
    w.deleteLater()
    QtWidgets.QApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)


def toast_of(window, key):
    toast = window.question_toasts.get(key)
    assert toast is not None and toast.isVisible(), "the question came as a toast with its answers"
    return toast, {button.text(): button for button in toast.actions}


def test_a_toast_offers_each_scope_and_each_is_the_answer(window, env, wait, qapp):
    directory = fake_run(env)
    page = window.approvals
    for n, label, scope in ((1, "Allow once", "once"), (2, "Always allow", "run"),
                            (3, "Always, also for later sessions", "always")):
        ident = f"1763500000-9-{n}"
        question(directory, ident, **{**CONNECT, "address": f"192.0.2.{n}", "destination": f"192.0.2.{n}:443"})
        page.poll()
        toast, buttons = toast_of(window, ("local", "opencode-1", ident))
        assert list(buttons) == ["Allow once", "Always allow", "Always, also for later sessions", "Block",
                                 "Block always", "Open approvals"]
        toast.layout().activate()
        for button in toast.actions:  # (the long one has a row of its own: no text cut)
            assert button.width() >= button.sizeHint().width(), (button.text(), button.width())
        buttons[label].click()
        qapp.processEvents()
        assert wait(lambda: page.answering is None, 90)
        assert json.loads((directory / f"{ident}.answer").read_text()) == {"decision": "allow", "scope": scope}
        (directory / f"{ident}.done").write_text(json.dumps({"decision": "allow", "scope": scope,
                                                              "reason": "answered"}))
    assert widgets.SHOWN == [], "no second question after a click on the toast"


def test_a_toast_leaves_out_a_scope_the_caps_forbid(window, env, qapp):
    directory = fake_run(env)
    question(directory, "1763500000-9-5", **HTTP)
    window.approvals.poll()
    _, buttons = toast_of(window, ("local", "opencode-1", "1763500000-9-5"))
    assert list(buttons) == ["Allow once", "Always allow", "Block", "Block always", "Open approvals"]


@pytest.mark.parametrize("language", ["en", "de"])
def test_a_toast_s_answers_are_never_cut(qapp, language):
    """Six answers on a 380 px toast, two to a row: the long one (Always, also for later sessions / Immer, auch für
    Folgesitzungen) takes a row of its own; no button is narrower than its text, in English or German."""
    from sandbox_ai_app import notifications
    i18n.set_language(language)
    try:
        labels = ["Allow once", "Always allow", "Always, also for later sessions", "Block", "Block always",
                  "Open approvals"]
        toast = notifications.Toast("warning", "sandbox-ai asks: claude-abc (~/project)", "connect to 192.0.2.7:443",
                                    15, action=[(i18n.tr(label), lambda: None) for label in labels])
        try:
            toast.setFixedWidth(notifications.WIDTH)
            toast.show()
            toast.layout().activate()
            qapp.processEvents()
            for button in toast.actions:
                assert button.width() >= button.sizeHint().width(), (button.text(), button.width())
            long = toast.actions[2]
            assert long.width() > toast.actions[0].width() * 1.5, "a row of its own"
        finally:
            toast.close()
            toast.deleteLater()
    finally:
        i18n.set_language("en")
