"""The Board's properties fold what is seldom set, and a Board panel undocked becomes a window centred and sized to
its content (2026-11-18): after the agent's questions option ("Agent ohne Rückfragen", --dangerously-skip-permissions)
the rest of its options go under "More options", folded at first, with how many of them are set; it opens on its own
where a finding points into it, and stays as chosen for that kind of part while the app runs (the inspector is made
anew after every change). Undocked by its button or a double click on its title, a panel is centred over the window
and sized to what it shows; a drag keeps where it was dropped unless that leaves it partly off the screen; where the
user put a floating panel is kept."""
import os
import time

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import QEvent, QRect, QSize, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QFormLayout, QListWidgetItem  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from sandbox_ai_app import backend, board as B, dock_fold, i18n, widgets  # noqa: E402

from test_projects import close, env, make_window, window  # noqa: E402,F401 - shared fixtures

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")

AGENT_TAIL = ("--local-config", "--env", "--secret-env", "--api-key")


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
    qapp.processEvents()
    yield view
    view.check_timer.stop()
    view.close()
    widgets.ThreadCall.wait_all(30)
    view.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def shown(page, what):
    """Whether WHAT (an option's field, by its name; or a function that finds the widget) is shown in the
    inspector. Found anew once the event loop ran: the inspector is made anew after every change."""
    import time
    end = time.monotonic() + 15  # (under a full suite's load the rebuild and the options read take longer)
    for _ in range(3):  # (a row just added is shown once the event loop ran - also after a rebuild it ran)
        QApplication.processEvents()
    def find():
        try:
            return page.option_fields[what] if isinstance(what, str) else what()
        except (KeyError, AttributeError, TypeError):
            return None
    while time.monotonic() < end:
        QApplication.processEvents()
        if find() is not None and not page.inspect_timer.isActive():
            # A late answer (the model servers, read once per process) may have made the inspector anew in this very
            # round of events: its rows are shown with the next one (a field found now read as hidden, and the test
            # failed in a full run). Looked at only once nothing makes it anew any more.
            for _ in range(3):
                QApplication.processEvents()
            widget = find()
            if widget is not None and not page.inspect_timer.isActive():
                return widget.isVisibleTo(page.inspector_holder)
        time.sleep(0.02)
    widget = page.option_fields[what] if isinstance(what, str) else what()
    return widget.isVisibleTo(page.inspector_holder)


def header_row(page):
    row, _role = page.inspector.getWidgetPosition(page.more_header)
    return row


def row_of(page, option):
    return page.inspector.getWidgetPosition(page.option_fields[option])[0]


def inspect(page, qapp, node):
    page.scene.clearSelection()
    page.items[node.id].setSelected(True)
    qapp.processEvents()
    page.inspect(page.board.node(node.id))
    return page.board.node(node.id)


def test_the_agents_options_after_its_questions_option_are_folded_at_first(page, qapp):
    agent = inspect(page, qapp, page.board.first("agent"))
    assert shown(page, "--dangerously-skip-permissions"), "the questions option stays in view"
    header = page.more_header
    assert header is not None and not header.isChecked()
    assert header.text() == "More options" and header.accessibleName().startswith("More options")
    assert header.focusPolicy() & Qt.FocusPolicy.TabFocus, "reached with Tab"
    # Right above the header; every option after it is folded away.
    assert row_of(page, "--dangerously-skip-permissions") == header_row(page) - 1
    for option in AGENT_TAIL:
        assert not shown(page, option), option
        assert row_of(page, option) > header_row(page)
    assert not shown(page, lambda: page.inspector.labelForField(page.option_fields["--env"])), \
        "the label folds with its field"
    assert shown(page, lambda: page.edit_box), "OK and Cancel stay below"
    assert agent.kind == "agent"


def test_the_header_says_how_many_folded_options_are_set_in_both_languages(page, qapp):
    agent = page.board.first("agent")
    page.set_prop(agent.id, "--env", ["A=1"])
    page.set_prop(agent.id, "--local-config", True)
    inspect(page, qapp, agent)
    assert page.more_header.text() == "More options (2 set)"
    assert "2 set" in page.more_header.accessibleName()
    i18n.set_language("de")
    page.inspect(page.board.node(agent.id))
    assert page.more_header.text() == "Weitere Optionen (2 gesetzt)"


def test_space_and_enter_open_and_fold_it_and_the_rows_are_then_in_the_tab_order(page, qapp):
    inspect(page, qapp, page.board.first("agent"))
    QApplication.processEvents()
    QTest.keyClick(page.more_header, Qt.Key.Key_Space)
    assert page.more_header.isChecked() and all(shown(page, o) for o in AGENT_TAIL)
    QTest.keyClick(page.more_header, Qt.Key.Key_Return)
    assert not page.more_header.isChecked() and not any(shown(page, o) for o in AGENT_TAIL)
    QTest.keyClick(page.more_header, Qt.Key.Key_Enter)
    assert page.more_header.isChecked() and shown(page, "--local-config")
    # Tab from the header reaches the first folded row once it is open.
    header = page.more_header
    header.setFocus()
    assert header.nextInFocusChain() is not None
    following = header.nextInFocusChain()
    while following.focusPolicy() == Qt.FocusPolicy.NoFocus or not following.isVisibleTo(page.inspector_holder):
        following = following.nextInFocusChain()
    first = page.option_fields["--local-config"]
    assert following is first or first.isAncestorOf(following), following


def test_open_or_folded_is_kept_for_the_kind_across_rebuilds_and_other_parts(page, qapp):
    agent = inspect(page, qapp, page.board.first("agent"))
    page.more_header.click()
    assert page.more_header.isChecked()
    page.set_prop(agent.id, "--local-config", True)  # a change: the inspector is made anew
    qapp.processEvents()  # (the inspector made anew: its timer)
    assert page.more_header.isChecked() and shown(page, "--env")
    inspect(page, qapp, page.board.first("network"))
    assert not page.more_header.isChecked(), "another kind of part: its own state (folded at first)"
    inspect(page, qapp, page.board.first("agent"))
    assert page.more_header.isChecked() and shown(page, "--api-key")
    page.more_header.click()
    page.inspect(page.board.node(agent.id))
    assert not page.more_header.isChecked()


def test_a_finding_about_a_folded_option_opens_the_section_and_puts_the_keyboard_there(page, qapp, wait):
    page.activateWindow()  # (the keyboard is kept across the inspector's rebuilds in the active window)
    assert wait(lambda: QApplication.activeWindow() is page, 15)  # (slow under a full suite's load)
    agent = inspect(page, qapp, page.board.first("agent"))
    assert not page.more_header.isChecked()
    item = QListWidgetItem("✘ The administrator's policy forbids --api-key.")
    item.setData(Qt.ItemDataRole.UserRole, agent.id)
    page.findings.addItem(item)
    page.finding_clicked(item)
    # (the inspector made anew: its timer - under load it may take more than one round of events)
    assert wait(lambda: page.more_header is not None and page.more_header.isChecked() and shown(page, "--api-key"), 10)

    def focused():
        widget, field = QApplication.focusWidget(), page.option_fields["--api-key"]
        return widget is not None and (widget is field or field.isAncestorOf(widget))
    assert wait(focused, 10)


def test_an_error_about_a_folded_option_opens_it_once(page, qapp):
    agent = page.board.first("agent")
    page.board_findings = ([B.Problem("error", "--secret-env: no secret named NOPE", agent.id)], [])
    inspect(page, qapp, agent)
    assert page.more_header.isChecked(), "the user's input is needed there"
    page.more_header.click()  # folded again by the user: the same finding does not open it again
    page.inspect(page.board.node(agent.id))
    assert not page.more_header.isChecked()


def test_other_kinds_fold_their_seldom_needed_tail(page, qapp):
    network = inspect(page, qapp, page.board.first("network"))
    assert page.more_header is not None
    for option in ("--network", "--ip", "--subnet"):
        assert not shown(page, option), option
    assert shown(page, "--egress-all-allow")
    page.set_prop(network.id, "--subnet", "10.9.0.0/24")
    page.inspect(page.board.node(network.id))
    assert page.more_header.text() == "More options (1 set)"
    proxy = inspect(page, qapp, page.board.first("proxy"))
    assert page.more_header is None, "a part without a seldom-needed tail has no section"
    assert proxy.kind == "proxy"


def test_the_bridges_limits_fold_after_its_ask_row(page, qapp):
    widgets.set_explanations_shown(True)  # (its closing note is an explanation: shown with the setting on)
    page.add_node("bridge", peer="web", limits={"rate": "30"})
    bridge = inspect(page, qapp, page.board.first("bridge"))
    QApplication.processEvents()
    header = page.more_header
    assert header is not None and header.text() == "More options (1 set)"

    def ask():
        return next(w for w in page.inspector_holder.findChildren(QtWidgets.QComboBox)
                    if w.accessibleName() == i18n.tr("Ask you about"))

    def rate():
        return next(w for w in page.inspector_holder.findChildren(QtWidgets.QLineEdit)
                    if w.placeholderText() == i18n.tr("default: {value}", value="60"))

    def note():
        return next(w for w in page.inspector_holder.findChildren(QtWidgets.QLabel)
                    if "not written into the project file" in w.text())
    assert page.inspector.getWidgetPosition(ask())[0] == header_row(page) - 1
    assert not shown(page, rate)
    assert shown(page, note), "the closing note stays outside the fold"
    assert isinstance(note(), widgets.Explanation)
    widgets.set_explanations_shown(False)
    assert not shown(page, note), "an explanation: hidden with the setting off"
    assert bridge.kind == "bridge"


# -- a panel floated out of the window --

def board_window(window, qapp, size=(640, 520)):
    window.resize(*size)
    window.show()
    window.show_page("board")
    qapp.processEvents()
    agent = window.board.board.first("agent")
    window.board.scene.clearSelection()
    window.board.items[agent.id].setSelected(True)
    window.board.inspect(window.board.board.node(agent.id))
    qapp.processEvents()
    return window.board, window.docks["inspector"], window.inspector_fold


def centre_over(window, geometry, available):
    """Where GEOMETRY's centre belongs: over the window's, moved only as far as keeps it inside AVAILABLE (a panel
    wider than the window near the screen's edge: the window shrinks to 640 px since R2-19, the panel's content is
    wider)."""
    placed = QRect(geometry)
    placed.moveCenter(window.frameGeometry().center())
    placed.moveLeft(max(available.left(), min(placed.left(), available.left() + available.width() - placed.width())))
    placed.moveTop(max(available.top(), min(placed.top(), available.top() + available.height() - placed.height())))
    return placed.center()


def settle(qapp, seconds=0.4):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)


def test_the_float_geometry_is_centred_fitted_and_inside_the_screen():
    available = QRect(0, 0, 1000, 800)
    over = QRect(100, 100, 600, 400)
    placed = dock_fold.float_geometry(QSize(300, 200), QSize(0, 0), over, available)
    assert placed.center() == over.center() and placed.size().width() >= dock_fold.MIN_FLOAT.width()
    big = dock_fold.float_geometry(QSize(5000, 5000), QSize(0, 0), over, available)
    assert big.width() <= 900 and big.height() <= 720 and available.contains(big)
    edge = dock_fold.float_geometry(QSize(500, 400), QSize(0, 0), QRect(900, 700, 300, 300), available)
    assert available.contains(edge), "kept inside the screen's available area"


def test_the_float_button_centres_the_properties_over_the_window_sized_to_their_content(window, qapp):
    board, dock, fold = board_window(window, qapp)
    fold.bar.float_button.click()
    settle(qapp)
    assert dock.isFloating()
    available = window.screen().availableGeometry()
    geometry = dock.geometry()
    assert available.contains(dock.frameGeometry()), (geometry, available)
    centre = centre_over(window, geometry, available)
    assert abs(geometry.center().x() - centre.x()) <= 2 and abs(geometry.center().y() - centre.y()) <= 2, \
        (geometry, window.frameGeometry())
    assert geometry.width() <= available.width() * 0.9 + 1 and geometry.height() <= available.height() * 0.9 + 1
    wanted = dock_fold.content_size(dock)
    assert geometry.width() >= min(wanted.width(), int(available.width() * 0.9))
    assert geometry.height() >= min(wanted.height(), int(available.height() * 0.9))
    assert geometry.width() >= dock_fold.MIN_FLOAT.width() and geometry.height() >= dock_fold.MIN_FLOAT.height()
    assert board.inspector_holder.isVisible()


def test_a_double_click_on_the_title_centres_it_too(window, qapp):
    _board, dock, fold = board_window(window, qapp)
    fold.unfold()  # (its title along the bar)
    qapp.processEvents()
    assert fold.bar.label.isVisible()
    QTest.mouseDClick(fold.bar.label, Qt.MouseButton.LeftButton)
    settle(qapp)
    assert dock.isFloating()
    centre = centre_over(window, dock.geometry(), window.screen().availableGeometry())
    assert abs(dock.geometry().center().x() - centre.x()) <= 2


def test_where_the_user_put_a_floating_panel_is_kept_when_it_is_undocked_again(window, qapp, monkeypatch):
    monkeypatch.setattr(dock_fold.FloatPlacement, "QUIET", 0.0)
    _board, dock, fold = board_window(window, qapp)
    fold.bar.float_button.click()
    settle(qapp)
    theirs = QRect(dock.x() + 13, dock.y() + 7, 401, 333)
    dock.setGeometry(theirs)
    settle(qapp)
    assert dock.geometry() == theirs
    fold.bar.float_button.click()  # docked again
    settle(qapp)
    assert not dock.isFloating()
    fold.bar.float_button.click()  # and out again: where the user had it
    settle(qapp)
    assert dock.isFloating() and dock.geometry() == theirs, (dock.geometry(), theirs)


class Held:
    """QApplication as the panel's placement sees it while a drag takes the panel out: the left button held."""

    @staticmethod
    def mouseButtons():  # noqa: N802
        return Qt.MouseButton.LeftButton


def dragged_out(dock, monkeypatch, where):
    """DOCK floated as a drag does it (a button held while it comes out), dropped at WHERE; then the button let go."""
    monkeypatch.setattr(dock_fold, "QApplication", Held)
    dock.setFloating(True)
    monkeypatch.setattr(dock_fold, "QApplication", QApplication)
    dock.setGeometry(where)


def test_a_drag_keeps_its_drop_place_unless_it_leaves_the_panel_off_the_screen(window, qapp, monkeypatch):
    board_window(window, qapp)
    parts = window.docks["parts"]
    available = window.screen().availableGeometry()
    dropped = QRect(available.left() + 20, available.top() + 30, 220, 300)
    dragged_out(parts, monkeypatch, dropped)
    window.settle_floating(parts)
    settle(qapp, 0.6)
    assert parts.geometry() == dropped, "a drop inside the screen stays where it was dropped"
    assert parts.placement.user_placed, "where the user dropped it is theirs"
    parts.setFloating(False)
    qapp.processEvents()
    parts.placement.user_placed = False
    off = QRect(available.right() - 60, available.bottom() - 40, 220, 300)
    dragged_out(parts, monkeypatch, off)
    window.settle_floating(parts)
    settle(qapp, 0.6)
    assert available.contains(parts.frameGeometry()), "partly off the screen: brought back"
    assert abs(parts.geometry().center().x() - window.frameGeometry().center().x()) <= 2, "centred"
    assert parts.size() == off.size(), "its size kept"
    assert not parts.windowFlags() & Qt.WindowType.X11BypassWindowManagerHint


def test_a_panel_restored_partly_off_the_screen_is_only_moved_inside(window, qapp):
    board_window(window, qapp)
    parts = window.docks["parts"]
    parts.setFloating(True)  # (as restoreState floats it: no drag, no button)
    available = window.screen().availableGeometry()
    off = QRect(available.right() - 60, available.top() + 50, 220, 300)
    parts.setGeometry(off)
    window.settle_floating(parts)
    settle(qapp, 0.6)
    assert available.contains(parts.frameGeometry()) and parts.size() == off.size()
    assert parts.geometry().top() == off.top(), "moved that far only"
    assert available.right() - 4 <= parts.frameGeometry().right() <= available.right(), "(with its frame)"


def test_the_settle_fix_still_gives_a_centred_panel_back_to_the_window_manager(window, qapp):
    _board, dock, fold = board_window(window, qapp)
    fold.bar.float_button.click()
    dock.setWindowFlag(Qt.WindowType.X11BypassWindowManagerHint, True)  # as a lost drag's end left it (spectrwm)
    dock.show()
    window.settle_floating(dock)
    settle(qapp, 0.8)
    assert dock.isFloating() and dock.isVisible()
    assert not dock.windowFlags() & Qt.WindowType.X11BypassWindowManagerHint
    assert isinstance(page_layout := _board.inspector, QFormLayout) and page_layout.rowCount() > 0
