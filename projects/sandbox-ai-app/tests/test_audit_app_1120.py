"""The in-depth audit of the app before the 2026-11-21 release: every page driven through the real MainWindow
offscreen as a user does (its buttons, right-click menus and dialogs). One test per defect found, named after what
was wrong; each stays as a regression test."""
import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import identities_page, widgets  # noqa: E402
from test_ui import env, window  # noqa: E402,F401 - the fixtures


def opened_dialogs(monkeypatch, module):
    """The dialogs MODULE runs (exec_dropped), recorded and cancelled."""
    opened = []

    def run(dialog, *args):
        opened.append(dialog.windowTitle())
        dialog.deleteLater()
        return QtWidgets.QDialog.DialogCode.Rejected
    monkeypatch.setattr(module.widgets, "exec_dropped", run)
    return opened


def test_identities_edit_with_nothing_selected_opened_new_identity(window, monkeypatch, wait):  # noqa: F811
    """Credentials, Identities: Edit… with no identity selected (an empty list) opened the dialog "New identity" -
    Add… under another name - and Remove… did nothing at all, without a word. Both say to choose one first, as Edit…
    of the Secrets, VPN and Git logins tabs do."""
    window.show_page("credentials")
    box = window.credentials.identities
    assert box is not None, "this sandbox-ai has identities"
    wait(lambda: box.list.rowCount() == 0 and not widgets.busy(box), 20)
    opened = opened_dialogs(monkeypatch, identities_page)
    box.list.clearSelection()
    box.edit_button.click()
    assert opened == [], "Edit… with nothing selected opened a dialog"
    assert box.message.text() == "Choose an identity in the list first."
    box.message.setText("")
    box.remove_button.click()
    assert box.message.text() == "Choose an identity in the list first."
    box.add_button.click()
    assert opened == ["New identity"], "Add… still opens the dialog"


def test_add_schedule_without_a_name_said_none(window, monkeypatch):  # noqa: F811
    """Sandboxes & sessions, Schedules, Add…, Save with the name left empty: the dialog said "a schedule's name is
    1-24 lower-case letters, digits, '_' and '-': 'None'" - a name nobody typed. It says the name is missing, and
    the dialog stays open."""
    from sandbox_ai_app import schedules_view
    box = window.sandboxes.schedules_box
    seen = []

    def run(dialog):
        dialog.name.setText("")
        dialog.save()
        seen.append((dialog.problem.isVisibleTo(dialog), dialog.problem.text(), dialog.result()))
        return QtWidgets.QDialog.DialogCode.Rejected
    monkeypatch.setattr(schedules_view.ScheduleDialog, "exec", run)
    box.buttons_by["add"].click()
    assert len(seen) == 1, "Add… opens the dialog"
    visible, text, result = seen[0]
    assert visible and "None" not in text and "needs a name" in text, text
    assert result != QtWidgets.QDialog.DialogCode.Accepted


def test_new_kit_without_a_name_said_is_not_a_valid_name(window, monkeypatch):  # noqa: F811
    """Kits & plugins, New kit…, OK with the name left empty: the message was " is not a valid name." - naming
    nothing, starting with a blank. It says to type a name; a name that is wrong is still named."""
    from sandbox_ai_app import kits_page
    window.show_page("kits")
    page = window.kits
    typed = [""]

    def run(dialog, *args):
        dialog.name.setText(typed[0])
        return QtWidgets.QDialog.DialogCode.Accepted
    monkeypatch.setattr(kits_page.widgets, "exec_dropped", run)
    page.new_button.click()
    assert widgets.SHOWN[-1][:2] == ("Kits & plugins", "Type a name first."), widgets.SHOWN[-1]
    typed[0] = "no/kit"
    page.new_kit()
    assert widgets.SHOWN[-1][:2] == ("Kits & plugins", "no/kit is not a valid name."), widgets.SHOWN[-1]


def test_kit_dialogs_with_their_source_or_agents_left_empty_did_nothing(window, monkeypatch):  # noqa: F811
    """Kits & plugins: New kit… with every agent unticked, and Add marketplace with its name but no source, closed
    and did nothing - no command, no word. Each says what is missing."""
    from sandbox_ai_app import kits_page
    window.show_page("kits")
    page = window.kits
    ran = []
    monkeypatch.setattr(page, "run", lambda *a, **kw: ran.append(a))

    def no_agent(dialog, *args):
        dialog.name.setText("mykit")
        for box in dialog.agents.findChildren(QtWidgets.QCheckBox):
            box.setChecked(False)
        return QtWidgets.QDialog.DialogCode.Accepted
    monkeypatch.setattr(kits_page.widgets, "exec_dropped", no_agent)
    page.new_kit()
    assert ran == [] and widgets.SHOWN[-1][:2] == ("Kits & plugins", "Tick at least one agent."), widgets.SHOWN[-1:]
    monkeypatch.setattr(page, "target", lambda default_ok=False: ("mykit", False, None))

    def no_source(dialog, *args):
        dialog.name.setText("tools")
        dialog.source.setText("")
        return QtWidgets.QDialog.DialogCode.Accepted
    monkeypatch.setattr(kits_page.widgets, "exec_dropped", no_source)
    page.add_marketplace()
    assert ran == [] and widgets.SHOWN[-1][1].startswith("Type the marketplace's source first"), widgets.SHOWN[-1:]


def test_add_a_host_whose_description_starts_with_a_dash(window, monkeypatch, wait):  # noqa: F811
    """Hosts, Add…: a description such as "-prod" (or a key path typed with a leading "-") reached the command line
    as an option of its own ("argument --description: expected one argument") and the host was not added. The
    values go as --description=VALUE, the name and the SSH target after "--"."""
    from sandbox_ai_app import hosts_page
    window.show_page("hosts")
    page = window.hosts

    def run(dialog, *args):
        dialog.name.setText("box1")
        dialog.ssh.setText("admin@192.0.2.10")
        dialog.description.setText("-prod")
        return QtWidgets.QDialog.DialogCode.Accepted
    monkeypatch.setattr(hosts_page.widgets, "exec_dropped", run)
    page.add_button.click()
    assert wait(lambda: any(e.get("name") == "box1" for e in window.sai.host_entries()) or widgets.SHOWN, 30)
    entries = {e.get("name"): e for e in window.sai.host_entries()}
    assert "box1" in entries, widgets.SHOWN
    assert entries["box1"].get("description") == "-prod", entries["box1"]


def menu_texts(view):
    return [text.strip() for text, _ in widgets.menu_entries(widgets.menu_of(view))]


def test_refresh_beside_a_list_was_missing_from_its_right_click_menu(window, wait):  # noqa: F811
    """The rule (user, 2026-11-14): a list's buttons' commands are also on its right-click menu. Sandboxes & sessions:
    Refresh above the list of starts was not on its menu; MCP servers: Reload catalogs beside the catalog had a
    separator on the menu and nothing after it. Both are there now, and do what their buttons do."""
    page = window.sandboxes
    assert "Refresh" in menu_texts(page.table), menu_texts(page.table)
    window.show_page("mcp")
    mcp = window.mcp
    assert wait(lambda: mcp.load_button.isEnabled(), 30), "the catalogs are read"
    assert menu_texts(mcp.catalog)[-1] == "Reload catalogs", menu_texts(mcp.catalog)
    loaded = []
    mcp.load_catalog = lambda *a, **kw: loaded.append(True)
    mcp.load_button.clicked.disconnect()
    mcp.load_button.clicked.connect(lambda _=False: mcp.load_catalog())
    next(a for a in widgets.menu_of(mcp.catalog).actions() if a.text() == "Reload catalogs").trigger()
    assert loaded == [True]


def test_check_now_showed_a_python_list(window, wait):  # noqa: F811
    """System › All settings… › Updates, Check now, on a copy that cannot check (a git checkout): the line below said
    "['sandbox-ai update: this copy was not installed by install.sh: …']" - a Python list, brackets and quotes. It
    says the last line of what sandbox-ai said, as it is."""
    from sandbox_ai_app import settings
    dialog = settings.SettingsDialog(window.sai)
    panel = dialog.updates
    assert wait(lambda: panel.check_button.isEnabled(), 60)
    panel.status.setText("")
    panel.check_button.click()
    assert wait(lambda: panel.status.text() not in ("", "Checking for a newer sandbox-ai…"), 120), panel.status.text()
    text = panel.status.text()
    assert not text.startswith("[") and "']" not in text, text
    dialog.deleteLater()


def test_a_base_images_problem_tooltip_was_read_as_markup(window, monkeypatch):  # noqa: F811
    """Images, New variant…, Base image: an image of yours that cannot be a base had its reason as the entry's
    tooltip unescaped - and the reason names the image's own /etc/os-release PRETTY_NAME, which whoever built the
    image chose: "<b>…</b>", a link or an image was rendered. It goes through safe_tooltip as every tooltip does."""
    from sandbox_ai_app import images_page
    window.show_page("images")
    page = window.images
    page.user_entries = [{"name": "evil", "base_problem": "it is <b>Totally Debian</b> <a href='x'>click</a>",
                          "unchecked": True, "present": True}]
    seen = []

    def run(dialog, *args):
        index = dialog.base.findText("evil (cannot be a base)")
        seen.append(dialog.base.itemData(index, images_page.Qt.ItemDataRole.ToolTipRole))
        return QtWidgets.QDialog.DialogCode.Rejected
    monkeypatch.setattr(images_page.widgets, "exec_dropped", run)
    page.new_variant()
    assert seen and seen[0] == widgets.safe_tooltip("it is <b>Totally Debian</b> <a href='x'>click</a>"), seen


def test_add_mcp_server_question_in_german_said_von_den(window, monkeypatch):  # noqa: F811
    """Sandboxes & sessions › Add MCP server… in German: the question read "web die Werkzeuge von den MCP-Server demo
    jetzt geben, …" - "von" with the accusative the inserted words have. It is said in correct German, for a server and
    for a helper agent."""
    from sandbox_ai_app import i18n, ui
    page = window.sandboxes
    chosen = [("server", "demo")]
    monkeypatch.setattr(ui.LiveMcpDialog, "exec", lambda self: QtWidgets.QDialog.DialogCode.Accepted)
    monkeypatch.setattr(ui.LiveMcpDialog, "value", lambda self: chosen[0])
    monkeypatch.setattr(page.sai, "host_mcp_attach_live", lambda *a, **kw: {"attached": True})
    i18n.set_language("de")
    try:
        page.choose_live_mcp("local", "web", "claude", [("demo", "stopped")], None)
        chosen[0] = ("helper", "codex")
        page.choose_live_mcp("local", "web", "claude", [("demo", "stopped")], None)
    finally:
        i18n.set_language("en")
    asked = [text for title, text, _ in widgets.SHOWN if title == "MCP-Server hinzufügen" and "Werkzeuge" in text]
    assert len(asked) == 2, widgets.SHOWN
    assert "von den" not in asked[0] and "den MCP-Server demo" in asked[0], asked[0]
    assert "Codex als Helfer" in asked[1] or "codex als Helfer" in asked[1], asked[1]


def test_delete_variant_with_no_variant_selected_did_nothing_without_a_word(window, monkeypatch):  # noqa: F811
    """Images: Delete variant… was enabled before any image was listed or selected, and a click on it (or the Delete
    key on an agent's own image) did nothing at all. It is greyed with the reason until a variant is selected, and
    when it is reached anyway (the Delete key) it says to select a variant."""
    from sandbox_ai_app import images_page
    window.show_page("images")
    page = window.images
    page.images.clearSelection()
    page.update_buttons()
    assert not page.delete_button.isEnabled()
    assert "Select a variant" in page.delete_button.toolTip(), page.delete_button.toolTip()
    page.entries = [{"target": "claude", "variant": False, "image": "localhost/claude-sandbox:latest",
                     "agent": "claude", "present": True, "status": "ok"}]
    widgets.fill(page.images, [["claude"]], keys=["claude"])
    page.images.selectRow(0)
    page.update_buttons()
    assert not page.delete_button.isEnabled(), "an agent's own image is no variant"
    asked = []
    monkeypatch.setattr(images_page, "ask_first", lambda *a, **kw: asked.append(a) or False)
    page.delete_variant()
    assert asked == [] and widgets.SHOWN[-1][:2] == ("Images", "Select a variant first."), widgets.SHOWN[-1:]


def test_allow_dialog_ok_with_nothing_or_half_a_git_login(window, monkeypatch):  # noqa: F811
    """Sandboxes & sessions › Allow… (also on a terminal tab's While it runs): OK with nothing typed closed the dialog
    and nothing happened, without a word; a Git login with its URL but not the place of its token (or the other way
    round) was dropped from what was sent - the rest was allowed as if it had been all. Both are said in the dialog,
    which stays open; a complete answer is sent as before."""
    from sandbox_ai_app import ui
    supported = {"--allow-host", "--git-token-env", "--keep"}
    dialog = ui.NetworkDialog(window, {}, "Allow for web", supported, op="allow")
    dialog.show()
    ok = dialog.findChild(QtWidgets.QDialogButtonBox).button(QtWidgets.QDialogButtonBox.StandardButton.Ok)
    ok.click()
    assert dialog.isVisible() and dialog.result() != QtWidgets.QDialog.DialogCode.Accepted
    assert dialog.problem.isVisibleTo(dialog) and dialog.problem.text() == "Type or tick what to allow first."
    dialog.hosts.setText("pypi.org")
    ok.click()
    assert dialog.result() == QtWidgets.QDialog.DialogCode.Accepted
    assert dialog.arguments() == ["--allow-host", "pypi.org"]
    # (half a Git login: OtherLoginDialog of Credentials says it since 2026-11-24, test_net_proposals_app.py)
    revoke = ui.NetworkDialog(window, {}, "Revoke for web", supported, op="revoke")
    revoke.show()
    revoke.findChild(QtWidgets.QDialogButtonBox).button(QtWidgets.QDialogButtonBox.StandardButton.Ok).click()
    assert revoke.problem.text() == "Type or tick what to take back first."
    for each in (dialog, revoke):
        each.deleteLater()


def test_add_workspace_and_add_device_ok_with_nothing_typed(window):  # noqa: F811
    """Sandboxes & sessions › Add workspace… and Add device… (also a terminal tab's Add folder… and While it runs):
    OK with the field left empty closed the dialog and nothing happened, without a word. Each says what to choose,
    and stays open; with a path OK closes as before."""
    from sandbox_ai_app import ui
    dialog = ui.WorkspaceDialog(window, "claude-abc")
    dialog.show()
    ok = dialog.buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok)
    ok.click()
    assert dialog.isVisible() and dialog.result() != QtWidgets.QDialog.DialogCode.Accepted
    assert dialog.problem.isVisibleTo(dialog) and dialog.problem.text() == \
        "Choose or type a folder or file first (Folder…, File… or Other machine…)."
    dialog.edit.setText("/srv/data")
    ok.click()
    assert dialog.result() == QtWidgets.QDialog.DialogCode.Accepted
    device = ui.DeviceDialog(window, [], added=[])
    device.show()
    device.findChild(QtWidgets.QDialogButtonBox).button(QtWidgets.QDialogButtonBox.StandardButton.Ok).click()
    assert device.isVisible() and device.missing.isVisibleTo(device)
    assert device.missing.text().startswith("Choose a device in the list or type its path first"), device.missing.text()
    device.edit.setText("/dev/ttyUSB0")
    device.findChild(QtWidgets.QDialogButtonBox).button(QtWidgets.QDialogButtonBox.StandardButton.Ok).click()
    assert device.result() == QtWidgets.QDialog.DialogCode.Accepted
    for each in (dialog, device):
        each.deleteLater()


def test_preset_directories_said_what_sandbox_ai_wrote_unescaped(window, monkeypatch):  # noqa: F811
    """Settings › sandbox-ai › Preset directories: what `sandbox-ai presets dirs add|remove` wrote (on this machine or a
    host, whose sandbox-ai may be older) was put into the message as it came - control characters (a folder named
    with an escape sequence) and all, and without a bound. It goes through printable_text, as every message does."""
    from sandbox_ai_app import settings
    monkeypatch.setattr(settings.PresetDirs, "refresh", lambda self: None)
    dirs = settings.PresetDirs(window.sai)
    dirs.done((1, "", "x" * 5000 + "\nsandbox-ai presets: /srv/evil\x1b[31mred: not a directory\n"), None)
    title, text, _ = widgets.SHOWN[-1]
    assert title == "Preset directories" and "\x1b" not in text and "\\x1b[31m" in text and len(text) <= 1500, \
        (title, text[:200], len(text))
    dirs.deleteLater()


def test_a_failed_commands_message_showed_its_control_characters(window, monkeypatch):  # noqa: F811
    """What sandbox-ai wrote when a command failed went into the message box as it came in several places (the
    outcome of a page's command, ui.report; MCP servers' Start/Stop; others): control characters - an escape
    sequence the agent or a host's file name put into it - and all, though every message goes through printable_text.
    The message box escapes them itself now, for every message."""
    from sandbox_ai_app import ui
    ui.report(window, "mcp stop", (1, "", "sandbox-ai mcp: no server \x1b[31mred\x07 here\n"), None)
    title, text, _ = widgets.SHOWN[-1]
    assert title == "mcp stop" and "\x1b" not in text and "\x07" not in text and "\\x1b[31m" in text, repr(text)
    widgets.warn(window, "T", "line one\n\tline two")
    assert widgets.SHOWN[-1][1] == "line one\n\tline two", "line breaks and tabs stay"
    widgets.warn(window, "T", "from ssh\r\nand a progress\rline")
    assert widgets.SHOWN[-1][1] == "from ssh\nand a progress\nline", "a carriage return is a line break"


def test_new_bridge_dialog_did_not_open_in_the_window(window):  # noqa: F811
    """Sandboxes & sessions › New bridge… in the app's window did nothing: the dialog's own `actions` (its observer's
    check boxes, a dict) hid QWidget.actions(), and the window's emergency stop (Ctrl+Shift+X in every dialog) read
    dialog.actions() before showing it - TypeError: 'dict' object is not callable, no dialog. It opens, and Ctrl+Shift+X
    works in it as in every dialog."""
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QWidget
    from sandbox_ai_app import bridges_view
    assert widgets.EMERGENCY_ACTION is not None, "the window's emergency stop"
    sessions = [{"name": "alpha", "run": "claude-1", "agent": "claude"},
                {"name": "beta", "run": "codex-1", "agent": "codex"}]
    dialog = bridges_view.BridgeDialog(window, sessions, first="alpha")
    QTimer.singleShot(200, dialog.reject)
    assert dialog.exec() == QtWidgets.QDialog.DialogCode.Rejected
    assert widgets.EMERGENCY_ACTION in QWidget.actions(dialog)
    dialog.deleteLater()


def answer_the_next_question(qapp, accept=True):
    """The question box that opens next (a real one, not AUTO_ANSWER): its first accepting button clicked, or Escape."""
    from PySide6.QtCore import QTimer

    def answer():
        boxes = [w for w in QtWidgets.QApplication.topLevelWidgets()
                 if isinstance(w, QtWidgets.QMessageBox) and w.isVisible()]
        if not boxes:
            QTimer.singleShot(50, answer)
            return
        box = boxes[-1]
        button = next((b for b in box.buttons() if box.buttonRole(b) in (
            QtWidgets.QMessageBox.ButtonRole.AcceptRole, QtWidgets.QMessageBox.ButtonRole.YesRole)), None)
        (button.click() if accept and button is not None else box.reject())
    QTimer.singleShot(50, answer)


def test_settings_updates_set_failed_on_its_question(window, monkeypatch, qapp):  # noqa: F811
    """Settings › Updates › Set… in the real app: the question was asked, and once answered the app raised
    TypeError ('MainWindow' object is not callable) - the Updates tab keeps the main window as self.window, which hid
    QWidget.window(), and the question's end asks its parent's window whether the app is ending. Nothing was set.
    (The tests answered questions at once - AUTO_ANSWER - and never got there.) Answered for real, the mode is set."""
    from sandbox_ai_app import settings, updates_view
    monkeypatch.setattr(widgets, "AUTO_ANSWER", None)
    monkeypatch.setattr(updates_view.UpdatesPanel, "refresh", lambda self: None)
    dialog = settings.SettingsDialog(window.sai, window)
    panel = dialog.updates
    called = []
    monkeypatch.setattr(panel.sai, "set_update_mode", lambda mode, window=None, host="local": called.append(mode)
                        or {"mode": mode})
    panel.mode.setCurrentIndex(panel.mode.findData("notify"))
    answer_the_next_question(qapp)
    panel.set_mode()
    end = __import__("time").monotonic() + 10
    while not called and __import__("time").monotonic() < end:
        qapp.processEvents()
    assert called == ["notify"], "the mode is set once the question is answered"
    dialog.deleteLater()


def test_schedules_buttons_were_greyed_without_a_reason(window):  # noqa: F811
    """The rule: a list's buttons are on its right-click menu, a disabled one with the reason. Sandboxes & sessions ›
    Schedules greyed Edit…, Run now, Pause, Resume, History… and Remove… with no reason at all (Resume of a schedule
    that is not paused, everything with none selected). Each says why."""
    box = window.sandboxes.schedules_box
    if not box.available:
        pytest.skip("this sandbox-ai has no schedules")
    box.schedules = [{"name": "nightly", "every": "1d", "paused": False, "running": False}]
    widgets.fill(box.list, [["nightly"]], keys=["nightly"])
    box.list.clearSelection()
    box.update_buttons()
    for key in ("edit", "run", "pause", "resume", "history", "remove"):
        button = box.buttons_by[key]
        assert not button.isEnabled() and "Select a schedule first." in button.toolTip(), (key, button.toolTip())
    box.list.selectRow(0)
    box.update_buttons()
    resume = box.buttons_by["resume"]
    assert not resume.isEnabled() and "It is not paused." in resume.toolTip(), resume.toolTip()
    assert box.buttons_by["pause"].isEnabled() and box.buttons_by["edit"].isEnabled()
    menu = dict((text.strip(), enabled) for text, enabled in widgets.menu_entries(widgets.menu_of(box.list)))
    assert menu["Resume"] is False and menu["Pause"] is True
    box.schedules[0]["paused"] = True
    box.update_buttons()
    assert box.buttons_by["resume"].isEnabled() and "It is paused already." in box.buttons_by["pause"].toolTip()
