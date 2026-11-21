"""No clipped text: every page and the main dialogs, in both languages and three themes (light, dark and the
largest font), at the window's minimum size. A button or a one-line label narrower than its text, or a wrapping
label shorter than its text needs, is a finding. The Settings dialog (and its tab of the administrator's policy) is
also checked on a small laptop's screen: no text beyond the room of its tab, every tab title whole."""
import json
import time
from pathlib import Path

import pytest

QtCore = pytest.importorskip("PySide6.QtCore", reason="PySide6 is not installed")
QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, i18n, settings, theme, ui, vpn_page, widgets, wizard  # noqa: E402

from test_ui import env  # noqa: E402,F401 - the isolated home of the UI tests

KINDS = (QtWidgets.QLabel, QtWidgets.QPushButton, QtWidgets.QCheckBox, QtWidgets.QRadioButton, QtWidgets.QToolButton)


def settle(qapp):
    """The window as it is drawn: the events waiting handled, then every layout Qt was asked for (a text that a
    background read's answer set in this very pass - Settings > Updates' state - is laid out in the next one: checked
    in between, "needs 70 px high, has 14" on a loaded machine, where the answer comes late), and the ones those ask
    for in turn (a label fitted to its new size)."""
    qapp.processEvents()
    for _ in range(5):
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.LayoutRequest)


def clipped(top):
    found = []
    for kind in KINDS:
        for w in top.findChildren(kind):
            if not w.isVisibleTo(top) or not w.text().strip() or getattr(w, "elides", False):
                continue  # (an ElidedLabel cuts a long line on purpose, whole in its tooltip)
            if (isinstance(w, QtWidgets.QLabel) and w.wordWrap()) or getattr(w, "wraps", False):
                if w.heightForWidth(w.width()) > w.height() + 2:
                    found.append(f"{type(w).__name__} {w.text()[:60]!r}: needs {w.heightForWidth(w.width())} px high, has {w.height()}")
            elif w.width() > 0 and w.sizeHint().width() > w.width() + 2:
                found.append(f"{type(w).__name__} {w.text()[:60]!r}: needs {w.sizeHint().width()} px wide, has {w.width()}")
    return found


@pytest.mark.parametrize("language", ["en", "de"])
@pytest.mark.parametrize("mode", ["light", "dark", "win311"])
def test_no_text_is_clipped_at_the_smallest_window(qapp, env, monkeypatch, language, mode):  # noqa: F811
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    i18n.set_language(language)
    try:
        theme.apply(mode, qapp)
        sai = backend.SandboxAI()
        window = ui.MainWindow(sai)
        window.resize(window.minimumSizeHint())
        window.show()
        qapp.processEvents()
        found = []
        for key, _ in ui.PAGES:
            window.show_page(key, raise_window=False)
            settle(qapp)
            found += [f"page {key}: {item}" for item in clipped(getattr(window, key))]
        dialog = settings.SettingsDialog(sai, window)
        dialog.show()
        for index in range(dialog.tabs.count()):
            dialog.tabs.setCurrentIndex(index)
            settle(qapp)
            found += [f"settings {dialog.tabs.tabText(index)}: {item}" for item in clipped(dialog)]
        dialog.close()
        flow = wizard.SandboxWizard(sai, lambda: {}, window)
        flow.show()
        for page in flow.pageIds():
            flow.setStartId(page)
            flow.restart()
            settle(qapp)
            found += [f"wizard page {page}: {item}" for item in clipped(flow)]
        flow.close()
        vpn = vpn_page.VpnDialog(window)
        vpn.show()
        settle(qapp)
        found += [f"vpn dialog: {item}" for item in clipped(vpn)]
        vpn.close()
        window.close()
        assert found == [], "\n".join(found)
    finally:
        i18n.set_language("en")
        theme.apply("system", qapp)


def test_a_read_answered_in_the_last_pass_before_the_check_is_laid_out_first(qapp, env, monkeypatch):  # noqa: F811
    """The flaky case above (a loaded machine): Settings > Updates' state, read in the background (`update mode
    --json`), answered in the very pass of the event loop before the check - its text was set, its layout came in the
    next pass, and the check saw "Modus notify (die Voreinstellung)\nLetzte Prüfung: ...: needs 70 px high, has 14".
    Deterministic here (the read held until the tab was laid out with "Reading…"); settle() checks what is drawn."""
    import threading
    from sandbox_ai_app import updates_view
    from test_updates_view import status
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    gate, answered = threading.Event(), threading.Event()

    def held(self, host="local"):
        gate.wait(30)
        try:
            return status(last_install={"from": "0.9.0", "to": "1.0.0", "time": 1})
        finally:
            answered.set()
    monkeypatch.setattr(backend.SandboxAI, "update_status", held)
    monkeypatch.setattr(backend.SandboxAI, "has_update_mode", lambda self: True)
    i18n.set_language("de")
    try:
        dialog = settings.SettingsDialog(backend.SandboxAI(), None)
        dialog.show()
        panel = dialog.findChild(updates_view.UpdatesPanel)
        dialog.tabs.setCurrentIndex(next(i for i in range(dialog.tabs.count())
                                         if dialog.tabs.widget(i).isAncestorOf(panel)))
        settle(qapp)
        one_line = panel.state.height()
        assert panel.state.text() == i18n.tr("Reading…") and clipped(dialog) == []
        gate.set()
        assert answered.wait(10)
        time.sleep(0.2)  # (its answer waits for the event loop)
        settle(qapp)  # the answer handled in this pass
        assert panel.state.text().startswith("Modus notify") and panel.state.height() > one_line
        assert clipped(dialog) == []
        dialog.close()
    finally:
        i18n.set_language("en")


# The widest the window's minimum may be after every page was shown (review round 5, H1: the Hosts page alone made it
# 1726 px wide in English and 1915 px in German, and Qt never shrinks a window back).
WIDEST, WIDEST_COMPACT = 1024, 860
WIDEST_PAGE = 900  # what a page itself needs at most before it scrolls sideways


@pytest.mark.parametrize("language", ["en", "de"])
@pytest.mark.parametrize("mode", ["light", "dark"])
@pytest.mark.parametrize("compact", [False, True])
def test_every_page_lets_the_window_shrink_back(qapp, env, monkeypatch, language, mode, compact):  # noqa: F811
    """Every page shown once, the window still shrinks to a size that fits a small laptop screen, and no text is cut
    off there: a page wider than the room scrolls sideways (PageScroll) instead of widening the window."""
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    i18n.set_language(language)
    try:
        theme.apply(mode, qapp)
        window = ui.MainWindow(backend.SandboxAI())
        window.show()
        window.set_compact_nav(compact)
        found = []
        for key, _ in ui.PAGES:
            window.show_page(key, raise_window=False)
            qapp.processEvents()
            page = getattr(window, key)
            if page.minimumSizeHint().width() > WIDEST_PAGE:
                found.append(f"page {key} needs {page.minimumSizeHint().width()} px")
            if window.pages.minimumSizeHint().width() > ui.SCROLL_MIN_WIDTH and key not in ui.UNSCROLLED_PAGES:
                found.append(f"page {key} asks the window for {window.pages.minimumSizeHint().width()} px")
        for key, _ in ui.PAGES:  # back on each page after all were shown: the window shrinks to its minimum
            window.show_page(key, raise_window=False)
            for _ in range(2):  # the page's own minimum is known once it is laid out
                qapp.processEvents()
                window.resize(window.minimumSizeHint())
            settle(qapp)
            limit = WIDEST_COMPACT if compact else WIDEST
            if window.width() > limit:
                found.append(f"page {key}: the window stays {window.width()} px wide (at most {limit}; its minimum "
                             f"{window.minimumSizeHint().width()}, the page asks {window.pages.minimumSizeHint().width()})")
            found += [f"page {key} at {window.width()} px: {item}" for item in clipped(getattr(window, key))]
        window.shutdown()  # timers, commands and terminals stop: many windows in one run
        window.close()
        window.deleteLater()
        qapp.processEvents()
        assert found == [], "\n".join(found)
    finally:
        i18n.set_language("en")
        theme.apply("system", qapp)


@pytest.mark.parametrize("language", ["en", "de"])
def test_leaving_the_board_does_not_widen_the_window(qapp, env, monkeypatch, language):  # noqa: F811
    """Review 6 N1: the Board's panels were hidden only after the next page was shown, so for that moment they were
    laid out beside its minimum and Qt widened the window for good (1277 px in English, 1381 px in German, from a
    1000 px window): past a small laptop's screen."""
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    i18n.set_language(language)
    try:
        window = ui.MainWindow(backend.SandboxAI())
        window.show()
        qapp.processEvents()
        start = max(1000, window.minimumSizeHint().width())
        window.resize(start, 760)
        qapp.processEvents()
        window.show_page("board", raise_window=False)
        qapp.processEvents()
        window.resize(start, 760)
        qapp.processEvents()
        for key in ("sandboxes", "board", "hosts", "board", "system"):
            window.show_page(key, raise_window=False)
            qapp.processEvents()
            if key != "board":
                assert window.width() <= start + 2, f"{key} after the Board: {window.width()} px (was {start})"
        window.shutdown()
        window.close()
        window.deleteLater()
        qapp.processEvents()
    finally:
        i18n.set_language("en")


def beyond_view(top):
    """Texts below TOP that reach past the right edge of the scroll area they are in (the tab scrolls sideways)."""
    found = []
    for kind in KINDS:
        for w in top.findChildren(kind):
            if not w.isVisibleTo(top) or not w.text().strip():
                continue
            area = w.parentWidget()
            while area is not None and not isinstance(area, QtWidgets.QScrollArea):
                area = area.parentWidget()
            if area is None or not area.isVisibleTo(top):
                continue
            right = w.mapTo(area.viewport(), QtCore.QPoint(min(w.width(), w.sizeHint().width()), 0)).x()
            if right > area.viewport().width() + 2:
                beyond = right - area.viewport().width()
                found.append(f"{type(w).__name__} {w.text()[:60]!r}: {beyond} px beyond the view")
    return found


LAPTOP = (1366, 768)  # a small laptop's screen; the room for a window is its height less a panel
POLICY = {"forbid": ["--egress-all", "--no-proxy", "--device=/dev/kvm", "--gpu", "--share-ssh-agent", "--mount-home"],
          "backends": ["podman"], "max_approval_scope": "session",
          "allow_within": ["10.0.0.0/8", "192.168.0.0/16", "api.example.org"],
          "allow_hosts": ["api.anthropic.com", "api.openai.com", "github.com", "registry.npmjs.org", "pypi.org"]}


@pytest.mark.parametrize("language", ["en", "de"])
@pytest.mark.parametrize("mode", ["light", "dark"])
def test_the_settings_and_the_policy_fit_a_small_laptop(qapp, env, monkeypatch, language, mode):  # noqa: F811
    """Settings and the administrator's policy (Settings > Administrator policy, opened by its menu item), at their
    size at first and at their smallest: every tab title whole, no text cut or beyond the tab's room (a long check box
    text wraps, a long label goes above its field), and the dialog fits a 1366x768 screen."""
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(settings, "screen_room", lambda _widget: QtCore.QSize(LAPTOP[0], LAPTOP[1] - 40))
    i18n.set_language(language)
    try:
        theme.apply(mode, qapp)
        sai = backend.SandboxAI()
        problem = "the file /etc/sandbox-ai/policy.json must belong to root and be writable by root only (owner 1000)"
        monkeypatch.setattr(sai, "policy", lambda: (POLICY, problem, json.dumps(POLICY, indent=2),
                                                    Path("/etc/sandbox-ai/policy.json")))
        dialog = settings.SettingsDialog(sai)
        dialog.show_policy()  # as the menu item opens it
        dialog.show()
        deadline = time.monotonic() + 30  # the preset directories and trusted projects are read in the background
        while time.monotonic() < deadline and any(widgets.busy(w) for w in (dialog.preset_dirs, dialog.trusted)):
            qapp.processEvents()
            time.sleep(0.02)
        qapp.processEvents()
        assert dialog.tabs.currentIndex() == dialog.tabs.count() - 1
        found = []
        for size in ("at first", "smallest"):
            if size == "smallest":
                dialog.resize(dialog.minimumSize())
            assert dialog.width() <= LAPTOP[0] and dialog.height() <= LAPTOP[1] - 40, (size, dialog.size())
            for index in range(dialog.tabs.count()):
                dialog.tabs.setCurrentIndex(index)
                for _ in range(3):  # (a text that came in the background is laid out at the next pass)
                    qapp.processEvents()
                where = f"{size}, tab {dialog.tabs.tabText(index)!r}"
                found += [f"{where}: {item}" for item in clipped(dialog) + beyond_view(dialog)]
                if dialog.tabs.widget(index).horizontalScrollBar().isVisible():
                    found.append(f"{where}: scrolls sideways")
                bar = dialog.tabs.tabBar()
                for tab in range(bar.count()):
                    rect = bar.tabRect(tab)
                    whole = bar.tabSizeHint(tab).width()
                    if rect.left() < 0 or rect.right() >= bar.width() + 1 or rect.width() < whole:
                        found.append(f"{where}: the title {bar.tabText(tab)!r} is cut ({rect} in {bar.width()} px)")
        dialog.close()
        assert found == [], "\n".join(found)
    finally:
        i18n.set_language("en")
        theme.apply("system", qapp)


def test_a_row_of_labels_fields_and_buttons_shares_one_middle(qapp):
    """Design review 0.1.95: in a wrapping row (widgets.flow: the Start page's Agent/Model/Profile, the board's start
    row, the toolbars) every item stood at the row's top, so a label sat 5 px above the middle of its field and a
    field 3 px above its button. Now each is centred in its row's height, also in a row that wrapped."""
    label, combo, button = QtWidgets.QLabel("Agent"), QtWidgets.QComboBox(), QtWidgets.QPushButton("Save as profile…")
    combo.addItem("claude")
    wide = QtWidgets.QPushButton("A button that wraps to a second row")
    holder = widgets.flow(label, combo, button, wide)
    holder.resize(360, 200)
    holder.show()
    qapp.processEvents()
    try:
        middles = {name: w.geometry().center().y() for name, w in (("label", label), ("combo", combo),
                                                                     ("button", button))}
        assert max(middles.values()) - min(middles.values()) <= 1, middles
        assert wide.geometry().top() > button.geometry().bottom(), "the long one is in the second row"
    finally:
        holder.close()


@pytest.mark.parametrize("mode, language", [*((mode, "de") for mode in theme.THEMES), ("light", "en")])
def test_the_boards_list_fields_fit_the_properties_in_every_theme(qapp, env, monkeypatch, language, mode):  # noqa: F811
    """User 2026-11-19: the network's lists (Allowed host names ... Own name servers) are one field of kinds
    (list_fields): no text of it clipped in the docked properties at the window's smallest, the empty field one line
    high, its chips within the panel's width (they wrap). Every theme in German (the longer texts), English once."""
    from sandbox_ai_app import list_fields as LF
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    i18n.set_language(language)
    try:
        theme.apply(mode, qapp)
        window = ui.MainWindow(backend.SandboxAI())
        window.resize(window.minimumSizeHint())
        window.show()
        window.show_page("board", raise_window=False)
        page = window.board
        network = page.board.first("network")
        page.set_prop(network.id, "--allow-host", [f"host{index}.example.org" for index in range(8)])
        page.inspect_timer.stop()
        page.inspected = network.id
        page.inspect(page.board.node(network.id))
        for _ in range(5):
            qapp.processEvents()
        field = page.option_fields["--allow-host"]
        assert isinstance(field, LF.ListFields)
        found = [item for item in clipped(field) if "host" not in item]  # (a chip's own text: whole, checked below)
        assert found == [], found
        view = page.inspector_scroll.viewport()
        for chip in field.chips["--allow-host"]:
            right = chip.mapTo(view, QtCore.QPoint(chip.width(), 0)).x()
            assert right <= max(view.width(), field.mapTo(view, QtCore.QPoint(field.width(), 0)).x()) + 2, chip.value
            assert chip.width() >= chip.sizeHint().width() - 2, f"{chip.value} clipped"
        empty = LF.ListFields(field.kinds.values(), {})
        empty.show()
        qapp.processEvents()
        assert empty.sizeHint().height() <= empty.entry_row.sizeHint().height() + 2
        empty.close()
        empty.deleteLater()
        window.shutdown()
        window.close()
        window.deleteLater()
        qapp.processEvents()
    finally:
        theme.apply("system", qapp)
        i18n.set_language("en")


BOARD_DOCKED = (1024, 768)  # R2-19: a 1024x768 screen, the Board's properties docked open beside the canvas
BOARD_FOLDED = 800  # and an 800 px wide one with them folded to their strip
# Where 800 px folded needs the navigation as a strip of icons (its button at the navigation's top, the app's answer
# for a small screen): fallout's large font in German - its navigation is 283 px for "Sandboxen & Sitzungen" whole,
# and Undo with Redo, which stay together (user 2026-11-18), are 275 px; with the titles it is 815 px.
FOLDED_COMPACT = {("fallout", "de")}


def board_fits(qapp, window, width, height):
    """The window on the Board resized to WIDTH x HEIGHT (its minimum where that is larger): what does not fit."""
    for _ in range(3):  # (the page's own minimum is known once it is laid out)
        qapp.processEvents()
        window.resize(width, height)
    qapp.processEvents()
    found = []
    least = window.minimumSizeHint()
    if window.width() > width or least.width() > width:
        found.append(f"the window is {window.width()} px wide (its least {least.width()}, at most {width}; the Board "
                     f"asks {window.board.minimumSizeHint().width()})")
    if window.height() > height or least.height() > height:
        found.append(f"the window is {window.height()} px high (its least {least.height()}, at most {height})")
    found += clipped(window)
    return found


@pytest.mark.parametrize("language", ["en", "de"])
@pytest.mark.parametrize("mode", theme.THEMES)
def test_the_board_fits_1024_with_its_properties_docked_and_800_folded(qapp, env, monkeypatch, language, mode):  # noqa: F811
    """Board review 2, R2-19 (user 2026-11-19: "wrap when narrow"): the window on the Board needed 1049 px with the
    properties docked open (1077 in German), 25 px more than a 1024 px screen, and 835 px (863) with them folded, so
    never fit an 800 px one - the Start controls were one row that set the Board's least width. Now they wrap onto a
    second line when narrow: every theme, both languages, nothing clipped there; on a wide window they stay one row."""
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    i18n.set_language(language)
    try:
        theme.apply(mode, qapp)
        window = ui.MainWindow(backend.SandboxAI())
        window.resize(1600, 900)
        window.show()
        window.show_page("board", raise_window=False)
        qapp.processEvents()
        page = window.board
        controls = page.start_controls
        parts = (controls.button, controls.where, controls.as_session, controls.name)
        rows = {part.mapTo(page, QtCore.QPoint(0, part.height() // 2)).y() for part in parts}
        assert max(rows) - min(rows) <= 1, f"wide: the Start controls are one row ({rows})"
        network = page.board.first("network")  # (the part of the most properties)
        page.inspect_timer.stop()
        page.inspected = network.id
        page.inspect(page.board.node(network.id))
        window.inspector_fold.unfold()
        qapp.processEvents()
        dock = window.docks["inspector"]
        assert dock.isVisible() and not dock.isFloating() and not window.inspector_fold.is_folded()
        found = [f"docked open: {item}" for item in board_fits(qapp, window, *BOARD_DOCKED)]
        window.inspector_fold.fold()
        if (mode, language) in FOLDED_COMPACT:
            window.set_compact_nav(True)
        qapp.processEvents()
        found += [f"folded: {item}" for item in board_fits(qapp, window, BOARD_FOLDED, BOARD_DOCKED[1])]
        window.set_compact_nav(False)
        qapp.processEvents()
        board_fits(qapp, window, 1600, 900)  # wide again: as at first
        rows = {part.mapTo(page, QtCore.QPoint(0, part.height() // 2)).y() for part in parts}
        if max(rows) - min(rows) > 1:
            found.append(f"wide again: the Start controls are not one row ({rows})")
        if "\n" in widgets.QToolButton.text(page.score_button):
            found.append("wide again: the score is not on one line")
        window.shutdown()
        window.close()
        window.deleteLater()
        qapp.processEvents()
        assert found == [], "\n".join(found)
    finally:
        theme.apply("system", qapp)
        i18n.set_language("en")
