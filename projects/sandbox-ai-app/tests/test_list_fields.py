"""List fields as "kind drop-down -> Add -> values" (user 2026-11-19: "Board-Eigenschaften: 'erlaubte Hostnamen' bis
'eigene Nameserver' sind immer unnötig große Formboxen offen ... Dropdown mit Klassen -> Add -> Werte eintragen"):
list_fields.ListFields - a drop-down of the kinds, the value, Add; the values as chips grouped by kind, a kind without
values takes no room. On the Board the network's list options (Allowed host names ... Own name servers) are one such
field, as are the agent's variables and keys, Git's push tokens, the proxy's TLS pass-through, the VPN's Tor bypass and
the devices; the setup wizard's network page too. Every value keeps its board key and launcher option."""
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import QEvent, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QPushButton, QVBoxLayout  # noqa: E402

from sandbox_ai_app import backend, board as B, i18n, theme, widgets  # noqa: E402
from sandbox_ai_app import list_fields as LF  # noqa: E402

from test_projects import close, env, make_window, window  # noqa: E402,F401 - shared fixtures

NEEDS_SAI = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                               reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")
NETWORK_LISTS = ("--allow-host", "--allow-domain", "--allow-shared-wildcard", "--allow", "--allow-tcp",
                 "--allow-host-port", "--nameserver")


def kinds():
    return [LF.option_kind("--allow-host", "Allowed host names (HTTPS)", tip="Pin hostname and allow TCP/443"),
            LF.option_kind("--allow-tcp", "Allowed TCP ports (IP:PORT)", tip="Allow one TCP port"),
            LF.option_kind("--nameserver", "Own name servers", tip="Use this resolver")]


def pump(rounds=5):
    for _ in range(rounds):
        QApplication.processEvents()


@pytest.fixture
def shown(qapp):
    """A ListFields in a dialog of its own (an OK button as its default), shown and active."""
    made = []

    def make(kinds_=None, values=None):
        dialog = QDialog()
        layout = QVBoxLayout(dialog)
        field = LF.ListFields(kinds_ or kinds(), values or {}, name="Lists")
        layout.addWidget(field)
        ok = QPushButton("OK")
        ok.setDefault(True)
        dialog.ok_clicked = []
        ok.clicked.connect(lambda: dialog.ok_clicked.append(True))
        layout.addWidget(ok)
        dialog.ok = ok
        dialog.show()
        dialog.activateWindow()
        pump()
        made.append(dialog)
        return dialog, field
    yield make
    for dialog in made:
        dialog.close()
        dialog.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def changes(field):
    seen = []
    field.changed.connect(lambda key, values: seen.append((key, values)))
    return seen


# -- the widget --

def test_enter_adds_the_items_to_the_chosen_kind_as_chips(shown):
    dialog, field = shown()
    seen = changes(field)
    assert field.current_kind() == "--allow-host"
    field.value_box.setFocus()
    QTest.keyClicks(field.value_box, "pypi.org, files.pythonhosted.org pypi.org")
    QTest.keyClick(field.value_box, Qt.Key.Key_Return)
    assert seen == [("--allow-host", ["pypi.org", "files.pythonhosted.org"])], "split, each once, one change"
    assert dialog.ok_clicked == [], "Enter adds; never the dialog's default button as well"
    assert field.value_box.currentText() == "" and field.value_box.hasFocus(), "the next value can be typed"
    assert [chip.value for chip in field.chips["--allow-host"]] == ["pypi.org", "files.pythonhosted.org"]
    field.choose_kind("--nameserver")
    QTest.keyClicks(field.value_box, "192.0.2.53")
    field.add_button.click()
    assert seen[-1] == ("--nameserver", ["192.0.2.53"])
    assert field.values() == {"--allow-host": ["pypi.org", "files.pythonhosted.org"], "--allow-tcp": [],
                              "--nameserver": ["192.0.2.53"]}
    # grouped by kind, under the kind's name; a kind without values: nothing of it at all
    groups = [field.groups_layout.itemAt(i).widget().objectName() for i in range(field.groups_layout.count())]
    assert groups == ["chips:--allow-host", "chips:--nameserver"]
    titles = [label.text() for label in field.groups.findChildren(QtWidgets.QLabel)
              if label.property("role") == "secondary"]
    assert titles == ["Allowed host names (HTTPS):", "Own name servers:"]
    # Enter in an empty field: the dialog's own (OK)
    QTest.keyClick(field.value_box, Qt.Key.Key_Return)
    assert seen[-1] == ("--nameserver", ["192.0.2.53"]) and len(seen) == 2


@pytest.mark.parametrize("key, bad, good", [
    ("--nameserver", "dns.example", "2001:db8::53"), ("--allow-tcp", "10.0.0.5", "[2001:db8::1]:443"),
    ("--allow-tcp", "10.0.0.5:70000", "10.0.0.5:22"), ("--allow", "10.0.0.0/33", "10.0.0.0/8"),
    ("--allow-host-port", "http", "11434"), ("--allow-host", "https://pypi.org/simple", "*.example.com"),
])
def test_a_value_the_kind_does_not_take_is_refused_and_stays_in_the_field(key, bad, good):
    kind = LF.option_kind(key, key)
    field = LF.ListFields([kind], {})
    seen = changes(field)
    assert not field.add(bad)
    assert field.problem.isVisibleTo(field) and bad in field.problem.text() and seen == []
    field.value_box.setEditText(bad)
    assert not field.add() and field.value_box.currentText() == bad, "kept to be corrected"
    field.value_box.lineEdit().textEdited.emit("x")
    assert not field.problem.isVisibleTo(field), "typing again: the refusal goes"
    assert field.add(good) and seen == [(key, [good])]
    field.deleteLater()


def test_delete_removes_a_chip_and_the_keyboard_goes_to_the_next(shown):
    dialog, field = shown(values={"--allow-host": ["a.example", "b.example"], "--nameserver": ["192.0.2.53"]})
    seen = changes(field)
    first = field.chips["--allow-host"][0]
    assert first.focusPolicy() == Qt.FocusPolicy.StrongFocus
    assert first.accessibleName() == "a.example (Allowed host names (HTTPS))"
    first.setFocus()
    pump()
    QTest.keyClick(first, Qt.Key.Key_Delete)
    assert seen == [("--allow-host", ["b.example"])]
    focused = QApplication.focusWidget()
    assert isinstance(focused, LF.Chip) and focused.value == "b.example"
    QTest.keyClick(focused, Qt.Key.Key_Backspace)
    assert seen[-1] == ("--allow-host", []) and "--allow-host" not in field.chips
    # the remove button (x) of a chip
    chip = field.chips["--nameserver"][0]
    assert chip.close_button.accessibleName() == "Remove 192.0.2.53"
    chip.close_button.click()
    assert seen[-1] == ("--nameserver", []) and not field.groups.isVisibleTo(field)


def test_a_chips_right_click_menu_has_remove_edit_and_copy(shown):
    dialog, field = shown(values={"--allow-tcp": ["10.0.0.5:22"]})
    seen = changes(field)
    chip = field.chips["--allow-tcp"][0]
    menu = chip.menu()
    texts = [action.text() for action in menu.actions() if not action.isSeparator()]
    assert texts == ["Remove", "Edit", "Copy"]
    next(a for a in menu.actions() if a.text() == "Copy").trigger()
    assert QApplication.clipboard().text() == "10.0.0.5:22"
    next(a for a in menu.actions() if a.text() == "Edit").trigger()
    assert field.current_kind() == "--allow-tcp" and field.value_box.currentText() == "10.0.0.5:22"
    assert seen == [("--allow-tcp", [])], "taken out to be changed"
    field.value_box.setEditText("10.0.0.5:2222")
    QTest.keyClick(field.value_box, Qt.Key.Key_Return)
    assert seen[-1] == ("--allow-tcp", ["10.0.0.5:2222"])


def test_empty_kinds_take_no_room(shown):
    dialog, empty = shown()
    line = empty.entry_row.sizeHint().height()
    assert not empty.groups.isVisibleTo(empty) and not empty.problem.isVisibleTo(empty)
    assert empty.sizeHint().height() <= line + 2, "only the line to add one: no box per kind"
    _dialog, full = shown(values={"--allow-host": ["a.example"]})
    assert full.groups.isVisibleTo(full) and full.sizeHint().height() > line


def test_the_kinds_help_is_their_tooltip_and_the_fields_have_names(shown):
    dialog, field = shown()
    index = field.kind_box.findData("--allow-tcp")
    assert "Allow one TCP port" in field.kind_box.itemData(index, Qt.ItemDataRole.ToolTipRole)
    field.choose_kind("--allow-tcp")
    assert "Allow one TCP port" in field.value_box.toolTip() and "Enter adds it" in field.value_box.toolTip()
    assert field.value_box.lineEdit().placeholderText() == "192.0.2.10:5432"
    assert field.kind_box.accessibleName() == "Kind of value" and field.value_box.accessibleName() == "Value"
    assert field.add_button.accessibleName() == "Add Allowed TCP ports (IP:PORT)"
    assert field.accessibleName() == "Lists"


def test_one_kind_needs_no_drop_down():
    field = LF.ListFields([LF.option_kind("--device", "Host devices")], {"--device": ["/dev/kvm"]})
    assert field.kind_box.isHidden() and not field.entry_row.isHidden()
    titles = [label for label in field.groups.findChildren(QtWidgets.QLabel) if label.property("role") == "secondary"]
    assert titles == [], "the row's label names the kind"
    field.deleteLater()


def test_leaving_the_field_takes_what_was_typed(shown):
    dialog, field = shown()
    seen = changes(field)
    field.value_box.setFocus()
    QTest.keyClicks(field.value_box, "example.org")
    field.kind_box.setFocus()  # its own drop-down: not left yet
    pump()
    assert seen == []
    dialog.ok.setFocus()
    pump()
    assert seen == [("--allow-host", ["example.org"])] and field.value_box.currentText() == ""
    field.release()  # (made anew by its page: its going takes nothing)
    field.value_box.setFocus()
    QTest.keyClicks(field.value_box, "other.example")
    dialog.ok.setFocus()
    pump()
    assert len(seen) == 1


def test_a_kind_the_policy_forbids_is_greyed_out_its_values_stay_removable():
    field = LF.ListFields(kinds(), {"--allow-host": ["a.example"]})
    field.set_kind_enabled("--allow-host", False, "The administrator's policy forbids --allow-host")
    index = field.kind_box.findData("--allow-host")
    assert not field.kind_box.model().item(index).isEnabled()
    assert field.current_kind() == "--allow-tcp", "another kind chosen"
    assert not field.add("b.example", key="--allow-host")
    seen = changes(field)
    field.chips["--allow-host"][0].removed.emit()
    assert seen == [("--allow-host", [])]
    field.deleteLater()


def test_a_kind_not_offered_shows_its_values_to_be_removed():
    hidden = LF.option_kind("--hermes-enable", "Hermes: features turned on again", offered=False)
    field = LF.ListFields([LF.option_kind("--env", "Host variables"), hidden], {"--hermes-enable": ["gateway:telegram"]})
    assert field.kind_box.findData("--hermes-enable") < 0
    chip = field.chips["--hermes-enable"][0]
    assert not next(a for a in chip.menu().actions() if a.text() == "Edit").isEnabled()
    field.deleteLater()


# -- suggestions per kind: the hook a device picker (or the host's secrets) plugs into --

def test_a_provider_offers_values_in_the_editable_field(shown):
    dialog, field = shown()
    asked, later = [], []
    field.set_provider("--allow-tcp", lambda fill: asked.append("tcp") or later.append(fill))
    assert asked == [], "asked when its kind is chosen"
    field.choose_kind("--allow-tcp")
    assert asked == ["tcp"] and field.value_box.count() == 0
    later[-1]([("LLM server on lab (10.0.0.5:11434)", "10.0.0.5:11434"), "10.0.0.6:22"], note="2 found")
    assert [field.value_box.itemText(i) for i in range(field.value_box.count())] == [
        "LLM server on lab (10.0.0.5:11434)", "10.0.0.6:22"]
    assert field.note.text() == "2 found" and field.note.isVisibleTo(field)
    field.value_box.activated.emit(0)
    assert field.value_box.currentText() == "10.0.0.5:11434", "the value, not its label"
    QTest.keyClick(field.value_box, Qt.Key.Key_Return)
    assert field.values("--allow-tcp") == ["10.0.0.5:11434"]
    field.value_box.setEditText("LLM server on lab (10.0.0.5:11434)")  # a label completed as typed: its value
    assert field.typed() == "10.0.0.5:11434"
    field.value_box.setEditText("10.9.9.9:80")  # any other value may still be typed
    assert field.add() and field.values("--allow-tcp")[-1] == "10.9.9.9:80"
    field.choose_kind("--allow-host")
    assert field.value_box.count() == 0 and not field.note.isVisibleTo(field), "the other kind's: none"
    field.value_box.showPopup()  # (none to show: no empty list opens)
    assert not field.value_box.view().isVisible()


def test_a_suggestion_with_host_to_type_over_selects_it(shown):
    dialog, field = shown([LF.option_kind("--api-key", "API keys")])
    field.set_provider("--api-key", lambda fill: fill([("EXAMPLE_KEY", "HOST=EXAMPLE_KEY")]))
    field.value_box.activated.emit(0)
    assert field.value_box.lineEdit().selectedText() == "HOST"


def test_making_the_field_opens_no_window_of_its_own(qapp):
    """Regression (found while building it): its drop-down shown before it was in the field was a window of its own
    for a moment - it took the keyboard from the Board's window, and what was typed lost its field."""
    before = {id(w) for w in QApplication.topLevelWidgets() if w.isVisible()}
    for values in ({}, {"--allow-host": ["a.example"]}):
        made = [LF.ListFields(kinds(), values), LF.ListFields([LF.option_kind("--device", "Host devices")], values)]
        pump()
        assert {id(w) for w in QApplication.topLevelWidgets() if w.isVisible()} == before
        for field in made:
            field.deleteLater()


def test_a_field_gone_leaves_no_connection_behind(qapp):
    """The inspector makes its fields anew after every change: each one's watch of the keyboard goes with it."""
    from PySide6.QtCore import SIGNAL
    before = qapp.receivers(SIGNAL("focusChanged(QWidget*,QWidget*)"))
    for _ in range(20):
        LF.ListFields(kinds(), {"--allow-host": ["a.example"]}).deleteLater()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert qapp.receivers(SIGNAL("focusChanged(QWidget*,QWidget*)")) == before


def test_a_late_answer_for_a_field_gone_is_dropped(qapp):
    kept = []
    field = LF.ListFields(kinds(), {})
    field.set_provider("--allow-host", kept.append)
    field.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    kept[-1](["a.example"])  # no exception


@pytest.mark.parametrize("name", theme.THEMES)
def test_every_theme_draws_the_chips_and_keeps_the_empty_field_one_line(qapp, name):
    before = theme.current()
    theme.apply(name, qapp)
    try:
        empty = LF.ListFields(kinds(), {})
        full = LF.ListFields(kinds(), {"--allow-host": ["a.example", "b.example"]})
        for field in (empty, full):
            field.resize(360, field.sizeHint().height())
            field.show()
        pump()
        assert empty.height() <= empty.entry_row.sizeHint().height() + 2
        chip = full.chips["--allow-host"][0]
        assert chip.isVisible() and chip.width() > chip.fontMetrics().horizontalAdvance("a.example")
        assert full.groups.height() > 0
        for field in (empty, full):
            field.close()
            field.deleteLater()
    finally:
        theme.apply(before or "system", qapp)


def test_the_texts_are_german_too():
    for text in ("Value", "Kind of value", "Add {kind}", "Add what the field holds as {kind}", "Enter adds it",
                 "Remove {value}", "Delete removes it",
                 "{kind}: {value}\nDelete removes it; right-click: Edit, Copy", "Not IP:PORT or [IPv6]:PORT: {value}",
                 "Not a host name: {value}", "No more of this kind can be added here",
                 "Hosts, addresses, name servers", "Variables, keys, features", "Push tokens",
                 "Choose the kind, type the value, Enter or Add: {kinds}", "One or more, separated by commas or spaces"):
        assert text in i18n.TEXTS, text
        assert i18n.TEXTS[text]["de"] and i18n.TEXTS[text]["de"] != text, text


# -- the Board's properties --

@pytest.fixture
def page(qapp, tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    (tmp_path / "cwd").mkdir()
    monkeypatch.chdir(tmp_path / "cwd")
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    from sandbox_ai_app.board_view import BoardPage
    view = BoardPage(backend.SandboxAI(), lambda *a, **k: None, lambda: {"board_grid": 20})
    view.resize(1200, 800)
    view.show()
    view.activateWindow()
    qapp.processEvents()
    yield view
    view.check_timer.stop()
    view.side.close()
    view.close()
    widgets.ThreadCall.wait_all(30)
    view.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def inspect(page, kind):
    node = page.board.first(kind)
    page.scene.clearSelection()
    page.items[node.id].setSelected(True)
    page.inspect_timer.stop()
    page.inspected = node.id
    page.inspect(page.board.node(node.id))
    pump()
    return page.board.node(node.id)


def settled(page, wait):
    assert wait(lambda: not page.inspect_timer.isActive(), 10)
    widgets.ThreadCall.wait_all(30)
    assert wait(lambda: not page.inspect_timer.isActive(), 10)
    pump()


def active(page, wait):
    """The keyboard in the window of PAGE's properties (kept across the inspector's rebuilds only there): in the app
    a panel of the window; here the properties' own window (as test_inspector_focus)."""
    side = page.side
    if side.parentWidget() is None or not side.isVisible():
        side.setParent(None)
        side.resize(480, 700)
        side.show()
    side.window().activateWindow()
    assert wait(lambda: QApplication.activeWindow() is side.window(), 15)


def field_of(page, option="--allow-host"):
    field = page.option_fields[option]
    assert isinstance(field, LF.ListFields)
    return field


def row_labels(page):
    from PySide6.QtWidgets import QFormLayout
    found = []
    for row in range(page.inspector.rowCount()):
        item = page.inspector.itemAt(row, QFormLayout.ItemRole.LabelRole)
        if item is not None and item.widget() is not None:
            found.append(item.widget().text())
    return found


@NEEDS_SAI
def test_the_networks_lists_are_one_field_with_the_same_keys(page, qapp, wait):
    active(page, wait)
    from sandbox_ai_app.board_view import LinesEdit
    network = inspect(page, "network")
    field = field_of(page)
    assert all(page.option_fields[option] is field for option in NETWORK_LISTS), "one field for all seven"
    assert [field.kind_box.itemData(i) for i in range(field.kind_box.count())] == list(NETWORK_LISTS)
    assert [field.kind_box.itemText(i) for i in range(field.kind_box.count())] == [
        "Allowed host names (HTTPS)", "Allowed domains with every name below (HTTPS)",
        "Wildcards below a shared service (HTTPS, asked first)", "Allowed addresses and networks",
        "Allowed TCP ports (IP:PORT)", "Ports on the host's loopback", "Own name servers"]
    assert not page.inspector_holder.findChildren(LinesEdit), "no box per list any more"
    labels = row_labels(page)
    assert "Hosts, addresses, name servers" in labels and "Own name servers" not in labels
    assert labels.index("Hosts, addresses, name servers") < labels.index("Real DNS (1.1.1.1, 8.8.8.8)")
    assert not field.groups.isVisibleTo(field), "nothing set: one line"
    steps = page.undo.count()
    field.value_box.setFocus()
    QTest.keyClicks(field.value_box, "pypi.org, files.pythonhosted.org")
    QTest.keyClick(field.value_box, Qt.Key.Key_Return)
    field.choose_kind("--nameserver")
    field.value_box.setEditText("192.0.2.53")
    field.add_button.click()
    settled(page, wait)
    props = page.board.node(network.id).props
    assert props["--allow-host"] == ["pypi.org", "files.pythonhosted.org"] and props["--nameserver"] == ["192.0.2.53"]
    assert page.undo.count() == steps + 2, "one step of Undo per Add"
    argv = B.to_argv(page.sai, page.board)
    assert argv.count("--allow-host") == 2 and "--nameserver" in argv and "192.0.2.53" in argv
    page.undo.undo()
    settled(page, wait)
    assert "--nameserver" not in page.board.node(network.id).props
    assert field_of(page).values("--nameserver") == [], "the field shows the value undone to"
    page.undo.redo()
    settled(page, wait)
    assert field_of(page).values("--nameserver") == ["192.0.2.53"]
    chip = field_of(page).chips["--allow-host"][0]
    chip.setFocus()
    QTest.keyClick(chip, Qt.Key.Key_Delete)
    settled(page, wait)
    assert page.board.node(network.id).props["--allow-host"] == ["files.pythonhosted.org"]


@NEEDS_SAI
def test_an_old_board_loads_unchanged_and_shows_its_values_as_chips(page, qapp, wait):
    argv = ["-A", "pypi.org", "--allow-domain", "example.org", "--allow", "10.0.0.0/8", "-t", "10.0.0.5:22",
            "--allow-host-port", "11434", "--nameserver", "192.0.2.53", "--no-inspect", "*.corp.example",
            "--env", "TERM", "--api-key", "api.example.com=EX_KEY", "--git-token-env", "https://git.example.org=TOK",
            "--device", "/dev/kvm"]
    board = B.from_argv(page.sai, "claude", argv)
    before = B.to_argv(page.sai, board)
    page.set_board(board, None)
    for kind in ("network", "proxy", "agent", "git", "devices"):
        if page.board.first(kind) is None:
            continue
        inspect(page, kind)
    settled(page, wait)
    assert B.to_argv(page.sai, page.board) == before, "showing the parts changes nothing"
    inspect(page, "network")
    field = field_of(page)
    assert field.values() == {"--allow-host": ["pypi.org"], "--allow-domain": ["example.org"],
                              "--allow-shared-wildcard": [], "--allow": ["10.0.0.0/8"], "--allow-tcp": ["10.0.0.5:22"],
                              "--allow-host-port": ["11434"], "--nameserver": ["192.0.2.53"]}
    assert [chip.value for chips in field.chips.values() for chip in chips] == [
        "pypi.org", "example.org", "10.0.0.0/8", "10.0.0.5:22", "11434", "192.0.2.53"]
    inspect(page, "proxy")
    assert field_of(page, "--no-inspect").values("--no-inspect") == ["*.corp.example"]
    if page.board.first("git") is not None:
        inspect(page, "git")
        assert field_of(page, "--git-token-env").values("--git-token-env") == ["https://git.example.org=TOK"]
    if page.board.first("devices") is not None:
        inspect(page, "devices")
        assert field_of(page, "--device").values("--device") == ["/dev/kvm"]
        assert field_of(page, "--device").kind_box.isHidden()


@NEEDS_SAI
def test_the_kind_chosen_and_what_is_typed_survive_the_inspector_made_anew(page, qapp, wait):
    network = inspect(page, "network")
    field = field_of(page)
    field.choose_kind("--allow-tcp")
    active(page, wait)
    field.value_box.setFocus()
    pump()
    QTest.keyClicks(field.value_box, "10.0.0.5:2")
    steps = page.undo.count()
    page.inspect_timer.start()  # a late answer (the host's model servers, images ...)
    settled(page, wait)
    again = field_of(page)
    assert again is not field, "made anew"
    assert again.current_kind() == "--allow-tcp", "the kind chosen is kept"
    assert again.value_box.currentText() == "10.0.0.5:2" and again.value_box.hasFocus()
    assert page.undo.count() == steps and "--allow-tcp" not in page.board.node(network.id).props, \
        "not taken before it is left"
    QTest.keyClicks(again.value_box, "2")
    QTest.keyClick(again.value_box, Qt.Key.Key_Return)
    settled(page, wait)
    assert page.board.node(network.id).props["--allow-tcp"] == ["10.0.0.5:22"]
    third = field_of(page)
    assert third.current_kind() == "--allow-tcp" and third.value_box.hasFocus() and third.value_box.currentText() == ""


@NEEDS_SAI
def test_a_value_typed_and_not_added_is_taken_when_another_part_is_selected(page, qapp, wait):
    network = inspect(page, "network")
    active(page, wait)
    field = field_of(page)
    field.value_box.setFocus()
    pump()
    assert field.value_box.hasFocus()
    QTest.keyClicks(field.value_box, "example.net")
    page.scene.clearSelection()
    page.items[page.board.first("agent").id].setSelected(True)  # the keyboard stays where it is
    settled(page, wait)
    assert page.board.node(network.id).props.get("--allow-host") == ["example.net"]


@NEEDS_SAI
def test_the_agents_variables_keys_and_features_are_one_field_under_more_options(page, qapp, wait, monkeypatch):
    from sandbox_ai_app import name_fields
    monkeypatch.setattr(name_fields, "names_of", lambda sai, kind, host, done, keep=0: done(["EX_KEY"], None))
    agent = page.board.first("agent")
    page.set_prop(agent.id, "--hermes-enable", ["gateway:telegram"])  # (another agent's: shown, to be removed)
    page.more_open["agent"] = True
    agent = inspect(page, "agent")
    field = field_of(page, "--env")
    assert field is page.option_fields["--api-key"] is page.option_fields["--hermes-enable"]
    offered = [field.kind_box.itemData(i) for i in range(field.kind_box.count())]
    assert offered == ["--env", "--secret-env", "--api-key"], "only the board's agent's features"
    assert field.values("--hermes-enable") == ["gateway:telegram"]
    assert field in page.more_widgets(), "folded under More options with the rest"
    field.choose_kind("--api-key")
    assert [field.value_box.itemData(i) for i in range(field.value_box.count())] == ["HOST=EX_KEY"]
    field.choose_kind("--secret-env")
    assert [field.value_box.itemData(i) for i in range(field.value_box.count())] == ["EX_KEY"]
    field.value_box.activated.emit(0)
    field.add()
    settled(page, wait)
    assert page.board.node(agent.id).props["--secret-env"] == ["EX_KEY"]
    page.set_prop(agent.id, "agent", "hermes")
    settled(page, wait)
    inspect(page, "agent")
    assert field_of(page, "--env").kind_box.findData("--hermes-enable") >= 0


@NEEDS_SAI
def test_a_device_picker_plugs_in_as_the_device_kinds_provider(page, qapp, wait, monkeypatch):
    """The hook for the device picker (board-devices): BoardPage.value_provider(--device) offers the board's host's
    devices in the editable value field; a path typed by hand still goes."""
    from sandbox_ai_app.board_view import BoardPage
    original = BoardPage.value_provider
    monkeypatch.setattr(BoardPage, "value_provider", lambda self, option: (
        (lambda fill: fill([("FTDI FT232R serial (usb:0403:6001) - /dev/ttyUSB0", "usb:0403:6001")], note="1 found"))
        if option == "--device" else original(self, option)))
    page.add_node("devices")
    devices = inspect(page, "devices")
    field = field_of(page, "--device")
    assert field.value_box.itemText(0).startswith("FTDI FT232R") and field.note.text() == "1 found"
    field.value_box.activated.emit(0)
    QTest.keyClick(field.value_box, Qt.Key.Key_Return)
    settled(page, wait)
    assert page.board.node(devices.id).props["--device"] == ["usb:0403:6001"]
    field_of(page, "--device").add("/dev/ttyACM0")
    settled(page, wait)
    assert page.board.node(devices.id).props["--device"] == ["usb:0403:6001", "/dev/ttyACM0"]


@NEEDS_SAI
def test_a_list_option_the_policy_forbids_is_greyed_out_in_the_drop_down(page, qapp, monkeypatch):
    page.forbidden = {"--allow-tcp": {None}}
    inspect(page, "network")
    field = field_of(page)
    index = field.kind_box.findData("--allow-tcp")
    assert not field.kind_box.model().item(index).isEnabled()
    assert "forbids --allow-tcp" in field.kind_box.itemData(index, Qt.ItemDataRole.ToolTipRole)


@NEEDS_SAI
def test_e2e_the_main_windows_board_takes_a_host_by_keyboard(window, qapp, wait):
    window.show_page("board")
    page = window.board
    active(page, wait)
    network = inspect(page, "network")
    field = field_of(page)
    field.value_box.setFocus()
    pump()
    QTest.keyClicks(field.value_box, "registry.npmjs.org")
    QTest.keyClick(field.value_box, Qt.Key.Key_Return)
    settled(page, wait)
    assert page.board.node(network.id).props["--allow-host"] == ["registry.npmjs.org"]
    assert "registry.npmjs.org" in " ".join(B.to_argv(page.sai, page.board))
    assert [chip.value for chip in field_of(page).chips["--allow-host"]] == ["registry.npmjs.org"]


@NEEDS_SAI
def test_delete_on_a_chip_keeps_the_keyboard_on_the_one_in_its_place_across_the_rebuild(page, qapp, wait):
    network = page.board.first("network")
    page.set_prop(network.id, "--allow-host", ["a.example", "b.example", "c.example"])
    network = inspect(page, "network")
    active(page, wait)
    chip = field_of(page).chips["--allow-host"][1]
    chip.setFocus()
    pump()
    assert chip.hasFocus()
    QTest.keyClick(chip, Qt.Key.Key_Delete)
    settled(page, wait)
    assert page.board.node(network.id).props["--allow-host"] == ["a.example", "c.example"]
    assert wait(lambda: isinstance(QApplication.focusWidget(), LF.Chip), 5)
    focused = QApplication.focusWidget()
    assert focused.value == "c.example" and field_of(page).isAncestorOf(focused), "the chip in its place, made anew"


def test_a_kind_with_a_list_says_so_and_a_click_into_the_empty_field_opens_it(shown):
    """The user 2026-11-23: "warum gibt es beim Board und den supporteten Toolchains kein Dropdown, damit man die direkt
    wählen kann. Add -> Dropdown, Add -> Dropdown": the editable list looked like a line to type into. A kind that
    offers values says "Choose from the list, or type: ...", a click into the empty field shows the list, and each
    chosen value Add takes leaves the field empty for the next."""
    kind = LF.ListKind("--toolchain", "Toolchains", tip="Build tools", placeholder="NAME[@VERSION]")
    dialog, field = shown([kind])
    assert field.value_box.lineEdit().placeholderText() == "NAME[@VERSION]", "no list yet: what a value looks like"
    seen = changes(field)
    field.set_provider("--toolchain", lambda fill: fill([("rust 1.99.1 (in the store)", "rust"),
                                                         ("cc 0.17.1-1 (in the store)", "cc")]))
    pump()
    assert field.value_box.lineEdit().placeholderText() == "Choose from the list, or type: NAME[@VERSION]"
    QTest.mouseClick(field.value_box.lineEdit(), Qt.MouseButton.LeftButton)
    pump()
    assert field.value_box.view().isVisible(), "a click into the empty field shows the list"
    field.value_box.hidePopup()
    for row in (0, 1):  # Add -> choose, Add -> choose
        field.value_box.setCurrentIndex(row)
        field.add()
        pump()
        assert field.value_box.currentText() == ""
    assert seen[-1] == ("--toolchain", ["rust", "cc"])
