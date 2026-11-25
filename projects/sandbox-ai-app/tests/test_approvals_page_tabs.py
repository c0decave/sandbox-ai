"""The Approvals page in two tabs (user 2026-11-24: "In Freigaben zwei große Bereiche: das, was aktuell aktiv angefragt
wird, und die Sandboxes selber. Bitte mit Tabs arbeiten: 1. Tab bleibt das obere Menü, 2. Tab ist Inspektion mit dem
Inhalt des unteren Menüs und Tabellen."): Questions - the questions of every host as they were (their panel above the
page is gone) - and Inspection - which sandbox, Allowed now, Remembered, each with the whole height. The page opens
on Questions; its tab counts the open questions; a new question never switches the tab; every way to a lower section
opens Inspection at it; the answer keys act only while the questions are shown."""
import json
import os
import time

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtCore import QEvent, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402

from sandbox_ai_app import backend, pages, short_notices, widgets  # noqa: E402


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


def fake_run(env, name="opencode-1"):
    directory = env / "runtime" / "sandbox-ai" / "runs" / name
    (directory / "approvals").mkdir(parents=True)
    (env / "project").mkdir(exist_ok=True)
    (directory / "run.json").write_text(json.dumps({
        "run": name, "agent": "opencode", "backend": "podman", "workspace": str(env / "project"), "pid": os.getpid(),
        "session": None, "session_dir": str(env / "s" / name), "started": "2026-11-24T04:00:00Z", "proxy": True,
        "ask": "on", "ask_timeout": 60}))
    return directory / "approvals"


def ask(directory, ident, address="192.0.2.7"):
    (directory / f"{ident}.json").write_text(json.dumps({
        "id": ident, "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "timeout": 60, "kind": "connect",
        "destination": f"{address}:8443", "address": address, "port": 8443}))


@pytest.fixture
def page(qapp, env):
    made = pages.ApprovalsPage(backend.SandboxAI(), interval=0, remote_interval=0)
    yield made
    made.timer.stop()
    made.blocked.timer.stop()
    made.deleteLater()


def texts(tabs):
    return [tabs.tabText(i) for i in range(tabs.count())]


def test_two_tabs_questions_first_and_inspection_with_the_sections(page):
    assert texts(page.page_tabs) == ["Questions", "Inspection"]
    assert page.page_tabs.currentWidget() is page.open_box, "the page opens on Questions"
    assert page.page_tabs.widget(1) is page.inspection
    # Inspection: which sandbox (the header), then its sections, each a tab of the whole height.
    assert page.inspection.isAncestorOf(page.header) and page.inspection.isAncestorOf(page.sections)
    assert texts(page.sections) == ["Allowed now", "Remembered"]
    assert page.sections.indexOf(page.open_box) == -1, "the questions are not a section of Inspection"
    assert not page.open_box.isAncestorOf(page.header)


def test_the_questions_tab_counts_the_open_questions_and_a_new_one_never_switches(env, page):
    directory = fake_run(env)
    ask(directory, "1764000000-7-1")
    page.poll()
    assert page.page_tabs.tabText(0) == "Questions (1)"
    page.show_inspection("always")
    assert page.page_tabs.currentWidget() is page.inspection and page.sections.currentWidget() is page.always_box
    ask(directory, "1764000000-7-2", "192.0.2.8")
    page.poll()
    assert page.page_tabs.tabText(0) == "Questions (2)"
    assert page.page_tabs.currentWidget() is page.inspection, "a new question does not switch the tab"
    for ident in ("1764000000-7-1", "1764000000-7-2"):
        (directory / f"{ident}.json").unlink()
    page.poll()
    assert page.page_tabs.tabText(0) == "Questions"


def test_the_ways_to_a_section_open_inspection_and_the_ways_to_a_question_open_questions(env, page):
    directory = fake_run(env)
    ask(directory, "1764000000-7-1")
    page.poll()
    page.show_inspection("allowed")
    assert page.page_tabs.currentWidget() is page.inspection and page.sections.currentWidget() is page.allowed
    page.show_inspection("always")
    assert page.sections.currentWidget() is page.always_box
    assert page.select_question(("local", "opencode-1", "1764000000-7-1"))
    assert page.page_tabs.currentWidget() is page.open_box
    page.show_inspection()
    assert page.page_tabs.currentWidget() is page.inspection and page.sections.currentWidget() is page.always_box, \
        "Inspection alone keeps the section last shown"
    page.show_host(None)
    assert page.page_tabs.currentWidget() is page.open_box
    page.show_inspection("allowed")
    page.show_questions()
    assert page.page_tabs.currentWidget() is page.open_box


def test_the_answer_keys_act_only_while_the_questions_are_shown(env, qapp, page, monkeypatch):
    directory = fake_run(env)
    ask(directory, "1764000000-7-1")
    page.resize(900, 700)
    page.show()
    page.poll()
    qapp.processEvents()
    page.open.selectRow(0)
    calls = []
    monkeypatch.setattr(page.sai, "answer", lambda ident, decision, scope="once": calls.append((ident, decision))
                        or (0, "done", ""))
    page.update_key_filter()
    assert page.keys_on

    def press():
        event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
        return page.eventFilter(page.open, event)
    page.show_inspection("allowed")
    qapp.processEvents()
    page.update_key_filter()
    assert not page.keys_on and not press(), "Inspection shown: Ctrl+Return answers nothing"
    page.show_questions()
    qapp.processEvents()
    page.update_key_filter()
    assert page.keys_on and press()
    page.hide()


def test_the_german_labels():
    from sandbox_ai_app import i18n
    assert i18n.TEXTS["tab|Questions"]["de"] == "Fragen"
    assert i18n.TEXTS["Inspection"]["de"] == "Inspektion"


# -- E (user 2026-11-24): one app view of what a sandbox may reach - Inspection › Allowed now, with Untaint…, Keep for
# later starts… and Copy (the plain text of `sandbox-ai net show`); Sessions › What it may reach… leads there. --

SHOWN = {"run": "opencode-1", "https": [{"address": "192.0.2.10", "names": ["pypi.org"]}],
         "taint": {"tainted": True, "since": "2026-11-24T05:00:00Z", "source": {"what": "a web page"}},
         "started": [{"kind": "preset", "what": "--allow-github", "source": "option", "hosts": ["github.com"]}]}
TEXT = "opencode-1\n  what it may reach from its start:\n    host set  option  this start  --allow-github\n"


def fake_net(page, monkeypatch, shown=SHOWN):
    called = []

    def call(*argv, **kwargs):
        called.append(argv)
        if argv[:2] == ("net", "show"):
            return (0, json.dumps(shown), "") if "--json" in argv else (0, TEXT, "")
        return 0, "done", ""
    monkeypatch.setattr(page.sai, "call", call)
    return called


def test_allowed_now_untaints_keeps_for_later_starts_and_copies_the_plain_text(env, qapp, page, wait, monkeypatch):
    fake_run(env)
    called = fake_net(page, monkeypatch)
    kept = []
    monkeypatch.setattr(page.sai, "net_keep", lambda host, run, every=False: kept.append((host, run, every))
                        or (0, "kept", ""))
    page.poll()
    page.show_reach("local", "opencode-1")
    assert page.page_tabs.currentWidget() is page.inspection and page.sections.currentWidget() is page.allowed
    allowed = page.allowed
    assert wait(lambda: allowed.reading is None and allowed.table.rowCount() == 2, 20)
    assert "tainted since 2026-11-24T05:00:00Z" in allowed.summary.text()
    assert allowed.untaint.isEnabled() and allowed.keep.isEnabled() and allowed.keep_all.isEnabled()
    allowed.untaint.click()
    assert wait(lambda: ("net", "untaint", "opencode-1") in called, 20)
    allowed.keep.click()
    allowed.keep_all.click()
    assert wait(lambda: len(kept) == 2, 20) and kept == [("local", "opencode-1", False), ("local", "opencode-1", True)]
    QtWidgets.QApplication.clipboard().clear()
    allowed.copy.click()
    assert wait(lambda: QtWidgets.QApplication.clipboard().text() == TEXT, 20)
    assert ("net", "show", "opencode-1") in called, "the plain text of sandbox-ai net show"


def test_untaint_is_off_for_a_run_that_is_not_tainted_and_nothing_acts_on_all(env, qapp, page, wait, monkeypatch):
    fake_run(env)
    fake_net(page, monkeypatch, {**SHOWN, "taint": {"tainted": False}})
    page.poll()
    page.show_reach("local", "opencode-1")
    allowed = page.allowed
    assert wait(lambda: allowed.reading is None and allowed.table.rowCount() == 2, 20)
    assert not allowed.untaint.isEnabled() and "not tainted" in allowed.untaint.toolTip()
    assert allowed.copy.isEnabled()
    page.header.choice.setCurrentIndex(page.header.choice.findData("all"))
    page.header.changed.emit()
    assert wait(lambda: allowed.reading is None and allowed.table.rowCount() == 0, 20)
    assert not any(button.isEnabled() for button in (allowed.untaint, allowed.keep, allowed.keep_all, allowed.copy))


def test_what_it_may_reach_of_a_start_on_a_host_is_read_and_changed_there(env, qapp, page, wait, monkeypatch):
    ran = []

    def host_run(host, *argv, **kwargs):
        ran.append((host, argv))
        return (0, json.dumps(SHOWN), "") if argv[:2] == ("net", "show") and "--json" in argv else (0, TEXT, "")
    monkeypatch.setattr(page.sai, "host_run", host_run)
    page.show_reach("ct1", "web")
    assert page.header.remote_target() == ("ct1", "web")
    allowed = page.allowed
    assert wait(lambda: allowed.reading is None and allowed.table.rowCount() == 2, 20)
    assert ("ct1", ("net", "show", "web", "--json")) in ran
    allowed.untaint.click()
    assert wait(lambda: ("ct1", ("net", "untaint", "web")) in ran, 20)
    allowed.copy.click()
    assert wait(lambda: ("ct1", ("net", "show", "web")) in ran, 20)
