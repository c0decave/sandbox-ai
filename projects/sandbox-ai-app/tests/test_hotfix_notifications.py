"""Hotfix 0.1.96, from live use: the app's popups say at a glance what they are about, and their text can be copied.

User: "Diese weißen Notifications, die oben rechts erscheinen, bitte den Text darin markierbar und kopierbar machen.
Außerdem die wichtigen Punkte farblich hervorheben. Beispiel: IP farblich, allow/deny farblich, state farblich,
solution bold, session bold — man soll direkt erkennen können, worum es geht."

And on the Images page: what its three tabs are for (a line at the top of each), and "In Zukunft soll das oberste Item
in Images und Varianten aktiviert (ausgewählt) sein, sodass die untere Tabelle Inhalte hat und direkt erschließbar
ist, dass die beiden zusammenhängen."
"""
import time

import pytest

pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")

from PySide6.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QEnterEvent, QGuiApplication, QTextCursor  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QLineEdit, QMainWindow, QMenu  # noqa: E402

from sandbox_ai_app import backend, i18n, notifications as N, rich_notice as R, safeguards_view, theme  # noqa: E402
from sandbox_ai_app import short_notices as SN, widgets  # noqa: E402

# The user's notification: a connection the firewall dropped, why, and the command that allows it.
SAFEGUARD = {"run": "claude-e7d6edae", "kind": "firewall", "count": 1, "level": "warning",
             "entry": {"what": "tcp to 140.82.112.4:443 (git push to github.com)",
                       "why": "not allowed by the run's network options: the firewall drops it",
                       "fix": "sandbox-ai net allow claude-e7d6edae --allow-tcp 140.82.112.4:443",
                       "target": "140.82.112.4"}}


@pytest.fixture
def notifier(qapp):
    theme.apply("light")
    shown = N.Notifier(None)
    yield shown
    shown.close_all()
    qapp.processEvents()
    theme.apply("system")


def run_for(qapp, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)


def char_format(edit, text):
    """The format the text edit EDIT shows TEXT in (its first character)."""
    at = edit.toPlainText().index(text)
    cursor = QTextCursor(edit.document())
    cursor.setPosition(at + 1)
    return cursor.charFormat()


def monospaced(fmt):
    families = list(fmt.fontFamilies() or []) if hasattr(fmt, "fontFamilies") else [fmt.fontFamily()]
    return any(family in theme.MONO for family in families)


def test_the_users_notification_says_at_a_glance_what_it_is_about(qapp, notifier):
    level, title, text = safeguards_view.popup(SAFEGUARD)
    toast = notifier.show(level, title, text, seconds=60)
    colors = R.role_colors()
    # The structure the code passed: the session, the address, the decision, why, the solution.
    assert toast.title.rich.of("session") == ["claude-e7d6edae"]
    assert toast.title.rich.of("deny") == ["dropped"], "the decision in the title"
    assert "140.82.112.4:443" in toast.text.rich.of("address") and "github.com" in toast.text.rich.of("address")
    assert toast.text.rich.of("solution") == ["sandbox-ai net allow claude-e7d6edae --allow-tcp 140.82.112.4:443"]
    assert toast.text.rich.of("label") == ["Why:", "To allow it:"]
    # ... and what is seen: the IP in the accent colour and monospaced, the decision red and bold, the solution
    # bold and monospaced, the session bold.
    address = char_format(toast.text, "140.82.112.4:443")
    assert address.foreground().color().name() == colors["address"] and monospaced(address)
    dropped = char_format(toast.title, "dropped")
    assert dropped.foreground().color().name() == colors["deny"] and dropped.fontWeight() == 700
    fix = char_format(toast.text, "sandbox-ai net allow")
    assert fix.fontWeight() == 700 and monospaced(fix)
    assert char_format(toast.title, "claude-e7d6edae").fontWeight() == 700
    assert char_format(toast.text, "not allowed by").fontWeight() < 600, "the why reads as text"
    # The lines stay lines (they were joined with an escaped "\x0a" before).
    assert toast.text.plain().splitlines()[1].startswith("Why: ") and "\\x0a" not in toast.text.plain()


def test_a_question_names_the_start_the_address_and_why(qapp, notifier):
    from sandbox_ai_app.pages import describe, describe_rich, why_asked
    question = {"kind": "connect", "destination": "203.0.113.7:8443", "host": "api.example.org"}
    title = R.fmt(i18n.tr("sandbox-ai asks: {sandbox}"), sandbox=R.session("claude-e7d6edae"))
    text = R.lines(describe_rich(question), R.recognise(why_asked(question), "why"))
    toast = notifier.show("warning", title, text, seconds=60)
    assert toast.title.rich.of("session") == ["claude-e7d6edae"] and toast.title.rich.of("ask") == ["asks"]
    assert toast.text.rich.of("address") == ["203.0.113.7:8443", "api.example.org"]
    assert str(describe_rich(question)) == describe(question), "the Approvals page's text unchanged"
    assert toast.text.plain().splitlines()[1] == why_asked(question)
    assert not [r for r, _ in toast.text.rich.lines[1] if r in R.DECISIONS], "no colour in the why"


@pytest.mark.parametrize("text, wanted", [
    ("connection to 140.82.112.4:443 dropped", {"address": ["140.82.112.4:443"], "deny": ["dropped"]}),
    ("lookup of x.example.org refused", {"address": ["x.example.org"], "deny": ["refused"]}),
    ("[2001:db8::1]:443 and 2001:db8::5 and https://example.com/a?b=c.",
     {"address": ["[2001:db8::1]:443", "2001:db8::5", "https://example.com/a?b=c"]}),
    ("update with: sandbox-ai update", {"solution": ["sandbox-ai update"]}),
    ("allowed for the run; the agent asks", {"allow": ["allowed"], "ask": ["asks"]}),
    ("The start failed; it was running, now stopped; starting again", {
        "failed": ["failed"], "running": ["running"], "stopped": ["stopped"], "busy": ["starting"]}),
    ("Verbindung verworfen; erlaubt; läuft; fehlgeschlagen; beendet", {
        "deny": ["verworfen"], "allow": ["erlaubt"], "running": ["läuft"], "failed": ["fehlgeschlagen"],
        "stopped": ["beendet"]}),
    # Never the opposite colour, nothing that only looks like an address or a command:
    ("the settings do not allow it; nicht erlaubt; not running", {}),
    ("see app.log, config.yaml, README.md, v0.1.96, 10:00:00, Node.js and the allow-list", {}),
    ("sandbox-ai keeps answering in the system tray; sandbox-ai asks: claude-1", {"ask": ["asks"]}),
])
def test_free_text_is_recognised_carefully_and_never_changed(text, wanted):
    found = R.recognise(text)
    assert str(found) == text, "recognising never changes the text"
    marked = {role: found.of(role) for role in R.ROLES if role not in ("text", "why", "label", "heading")}
    assert {role: parts for role, parts in marked.items() if parts} == wanted


def test_the_labels_and_words_of_both_languages(qapp):
    i18n.set_language("de")
    try:
        _, title, text = safeguards_view.popup(SAFEGUARD)
        assert title.of("deny") == ["verworfen"] and title.of("session") == ["claude-e7d6edae"]
        assert text.of("label") == ["Warum:", "Zum Erlauben:"]
        assert text.of("solution") == [SAFEGUARD["entry"]["fix"]]
        found = R.recognise("Warum: nicht erlaubt\nZum Erlauben: sandbox-ai net allow x --allow-host a.example")
        assert found.of("label") == ["Warum:", "Zum Erlauben:"]
        assert found.of("solution") == ["sandbox-ai net allow x --allow-host a.example"]
        assert i18n.tr("Copy the message") == "Die Meldung kopieren" and i18n.tr("Copied") == "Kopiert"
    finally:
        i18n.set_language("en")


@pytest.mark.parametrize("name", list(theme.THEMES_DATA))
def test_every_highlight_is_readable_and_never_colour_alone(name):
    """WCAG AA: 4.5:1 on the popup's background in every theme; every coloured part also bold or monospaced."""
    data = theme.THEMES_DATA[name]
    colors = R.role_colors(data)
    for role, color in colors.items():
        assert theme.contrast(color, data["colors"]["base"]) >= R.MIN_CONTRAST, (name, role, color)
        assert role in R.BOLD or role in R.MONO_ROLES, (role, "colour is never the only signal")
    assert R.readable("#777777", "#ffffff", "#000000") != "#777777", "a colour too light is darkened"


def test_markup_is_never_read(qapp, notifier):
    toast = notifier.show("error", "<b>x</b> 1.2.3.4", "<img src=x> <a href='https://evil.example'>y</a> & `rm -rf`")
    for edit in (toast.title, toast.text):
        assert edit.toPlainText() == edit.plain() and "<img" not in edit.html() and "<a " not in edit.html()
    assert toast.text.plain() == "<img src=x> <a href='https://evil.example'>y</a> & `rm -rf`"
    assert R.desktop_markup(R.lines([R.session("<i>s</i>"), ("text", " & ")])) == "<b>&lt;i&gt;s&lt;/i&gt;</b> &amp; "


def test_the_text_is_marked_and_copied_with_the_keyboard_the_button_and_the_menu(qapp, notifier, monkeypatch):
    clipboard = QGuiApplication.clipboard()
    level, title, text = safeguards_view.popup(SAFEGUARD)
    toast = notifier.show(level, title, text, seconds=60)
    for edit in (toast.title, toast.text):
        flags = edit.textInteractionFlags()
        assert flags & Qt.TextInteractionFlag.TextSelectableByMouse
        assert flags & Qt.TextInteractionFlag.TextSelectableByKeyboard
        assert edit.isReadOnly() and edit.focusPolicy() == Qt.FocusPolicy.StrongFocus
    # Ctrl+C: what is marked ...
    at = toast.text.plain().index("140.82.112.4:443")
    toast.text.setSelection(at, len("140.82.112.4:443"))
    QTest.keyClick(toast.text, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    assert clipboard.text() == "140.82.112.4:443"
    # ... and with nothing marked, the whole message (plain text: no markup, no escaped line breaks).
    toast.text.setTextCursor(QTextCursor(toast.text.document()))
    QTest.keyClick(toast.text, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    assert clipboard.text() == f"{title}\n{text}" == toast.message_text()
    # The copy button: everything, and it says so for a moment.
    monkeypatch.setattr(N.Toast, "COPIED_MS", 100)
    clipboard.setText("")
    toast.copy_button.click()
    assert clipboard.text() == toast.message_text() and toast.copy_button.toolTip() == i18n.tr("Copied")
    assert toast.copy_button.accessibleName() == i18n.tr("Copy the message")
    run_for(qapp, 0.3)
    assert toast.copy_button.toolTip() == i18n.tr("Copy the message")
    # The right click's menu: Copy (what is marked), Copy all.
    toast.text.setSelection(at, len("140.82.112.4"))
    chosen = []

    def run_menu(menu, position):
        assert isinstance(menu, QMenu)
        chosen.append([action.text() for action in menu.actions()])
        next(a for a in menu.actions() if a.text() == i18n.tr("Copy")).trigger()
    monkeypatch.setattr(N.Toast, "run_menu", staticmethod(run_menu))
    clipboard.setText("")
    toast.contextMenuEvent(type("Event", (), {"globalPos": lambda self: toast.mapToGlobal(toast.rect().center())})())
    assert chosen == [[i18n.tr("Copy"), i18n.tr("Copy all")]] and clipboard.text() == "140.82.112.4"


def test_a_toast_stays_while_it_is_looked_at(qapp, monkeypatch):
    """No auto-hide while the pointer is over it, the focus is in it (marking, copying) or its menu is open."""
    monkeypatch.setattr(N.Toast, "HOLD_MS", 100)
    theme.apply("light")
    window = QMainWindow()
    field = QLineEdit()
    window.setCentralWidget(field)
    window.resize(800, 500)
    window.show()
    window.activateWindow()
    field.setFocus()
    qapp.processEvents()
    inside = N.Notifier(window)
    monkeypatch.setattr(N, "can_place_windows", lambda: False)  # (inside the window: the focus can be checked here)
    try:
        toast = inside.show("warning", "claude-1: refused", "tcp to 203.0.113.7:443", seconds=0.2)
        assert toast.overlay and qapp.focusWidget() is field, "it appears without taking the keyboard"
        QTest.mouseClick(toast.text.viewport(), Qt.MouseButton.LeftButton, pos=toast.text.viewport().rect().center())
        toast.text.setFocus()
        toast.text.setSelection(0, 3)
        run_for(qapp, 0.6)
        assert inside.toasts == [toast] and toast.isVisible(), "kept while marking"
        field.setFocus()
        run_for(qapp, 0.5)
        assert inside.toasts == [], "goes once the focus left it"
        menu = inside.show("info", "i", "x", seconds=0.2)
        menu.menu_open = True
        run_for(qapp, 0.5)
        assert inside.toasts == [menu], "kept while its menu is open"
        menu.menu_open = False
        run_for(qapp, 0.4)
        assert inside.toasts == []
        # Esc closes a toast that has the focus.
        esc = inside.show("info", "i", "x", seconds=60)
        esc.text.setFocus()
        QTest.keyClick(esc.text, Qt.Key.Key_Escape)
        qapp.processEvents()
        assert inside.toasts == []
    finally:
        inside.close_all()
        window.close()
        window.deleteLater()
        qapp.processEvents()
        theme.apply("system")


def test_a_click_gives_a_toast_window_the_keyboard_and_it_gives_it_back(qapp, notifier):
    toast = notifier.show("info", "t", "x", seconds=60)
    assert not toast.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus
    window = QMainWindow()
    window.show()
    window.activateWindow()
    qapp.processEvents()
    try:
        QTest.mousePress(toast.text.viewport(), Qt.MouseButton.LeftButton, pos=toast.text.viewport().rect().center())
        QTest.mouseRelease(toast.text.viewport(), Qt.MouseButton.LeftButton, pos=toast.text.viewport().rect().center())
        qapp.processEvents()
        assert toast.previous is window, "the window that had the keyboard is remembered"
        point = QPointF(5, 5)
        toast.enterEvent(QEnterEvent(point, point, point))
        assert toast.keeps()
        toast.leaveEvent(QEvent(QEvent.Type.Leave))
    finally:
        window.close()
        window.deleteLater()


def test_long_text_wraps_and_nothing_is_cut(qapp, notifier):
    url = "https://docs.example.org/" + "x" * 200 + "?token=abc"
    toast = notifier.show("warning", "claude-1: " + "a long title " * 12,
                          f"open {url} in your browser\n" + "word " * 60, seconds=60)
    qapp.processEvents()
    for edit in (toast.title, toast.text):
        assert edit.height() >= edit.heightForWidth(edit.width()), "every line shown"
        assert edit.document().size().width() <= edit.viewport().width() + 1, "the long URL wraps"
        assert edit.verticalScrollBar().maximum() == 0, "never scrolled"
    screen = QGuiApplication.primaryScreen().availableGeometry()
    assert toast.height() <= screen.height() and toast.width() <= N.WIDTH


def test_screen_readers_get_plain_text(qapp, notifier):
    level, title, text = safeguards_view.popup(SAFEGUARD)
    toast = notifier.show(level, title, text, seconds=60)
    assert toast.accessibleName() == f"{i18n.tr('Warning')}: {title}"
    assert toast.accessibleDescription() == str(text) == toast.text.accessibleName()
    assert "<" not in toast.accessibleDescription() and "span" not in toast.text.accessibleName()


def test_on_wayland_inside_the_active_window_else_the_desktops_with_bold_parts(qapp, monkeypatch):
    class Window(QMainWindow):
        active = True

        def isActiveWindow(self):  # noqa: N802
            return self.active

    class Tray:
        def __init__(self):
            self.messages = []

        def showMessage(self, *args):  # noqa: N802
            self.messages.append(args)
    monkeypatch.setattr(N, "can_place_windows", lambda: False)
    window = Window()
    window.resize(800, 500)
    window.show()
    tray = Tray()
    notifier = N.Notifier(window, tray)
    level, title, text = safeguards_view.popup(SAFEGUARD)
    try:
        toast = notifier.show(level, title, text, seconds=60)
        assert toast is not None and toast.overlay and not tray.messages, "the user works in the app: a toast in it"
        window.active = False
        assert notifier.show(level, title, text, seconds=60) is None
        body = tray.messages[-1][1]
        assert f"<b>{SAFEGUARD['entry']['fix']}</b>" in body and body.count("\n") == 2, "lines kept, the fix in bold"
    finally:
        notifier.close_all()
        window.close()
        window.deleteLater()


# -- short notices --

@pytest.fixture
def shown_window(qapp):
    theme.apply("light")
    window = QMainWindow()
    field = QLineEdit()
    window.setCentralWidget(field)
    window.resize(900, 600)
    window.show()
    window.activateWindow()
    field.setFocus()
    qapp.processEvents()
    window.field = field
    yield window
    window.close()
    window.deleteLater()
    qapp.processEvents()
    theme.apply("system")


def test_a_short_notice_is_marked_copied_and_stays_while_focused(qapp, shown_window, monkeypatch):
    monkeypatch.setattr(SN, "VISIBLE_MS", 150)
    monkeypatch.setattr(SN, "HOVER_MS", 100)
    notice = SN.short_notice(shown_window, "Hosts", "Trusted on 10.0.0.12: /srv/p (running)")
    label = notice.label
    assert qapp.focusWidget() is shown_window.field, "a notice never takes the keyboard when it appears"
    assert label.textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByMouse
    assert label.focusPolicy() == Qt.FocusPolicy.ClickFocus
    assert label.rich.of("address") == ["10.0.0.12"] and label.rich.of("running") == ["running"]
    label.setFocus(Qt.FocusReason.MouseFocusReason)
    label.setSelection(label.plain().index("10.0.0.12"), len("10.0.0.12"))
    QTest.keyClick(label, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    assert QGuiApplication.clipboard().text() == "10.0.0.12"
    label.setTextCursor(QTextCursor(label.document()))
    QTest.keyClick(label, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    assert QGuiApplication.clipboard().text() == "Trusted on 10.0.0.12: /srv/p (running)"
    run_for(qapp, 0.8)
    assert not notice.gone and notice.isVisible(), "kept while the focus is in it"
    QTest.keyClick(label, Qt.Key.Key_Escape)
    assert notice.gone and qapp.focusWidget() is shown_window.field, "Esc: gone, the keyboard back where it was"


def test_a_long_short_notice_is_never_cut(qapp, shown_window):
    text = "Written and trusted: /srv/data/" + "deep/" * 40 + "file.json on 10.0.0.12"
    notice = SN.short_notice(shown_window, "trust", text)
    qapp.processEvents()
    label = notice.label
    assert label.height() >= label.heightForWidth(label.width()) and label.verticalScrollBar().maximum() == 0
    assert notice.width() <= shown_window.width()


# -- the Images page --

@pytest.fixture
def images(qapp, tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache"), ("HOME", "home")):
        monkeypatch.setenv(name, str(tmp_path / sub))
        (tmp_path / sub).mkdir()
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    from sandbox_ai_app.images_page import ImagesPage
    page = ImagesPage(backend.SandboxAI(), lambda *a, **k: None)
    page.asked = []
    for command in (page.package_command, page.show_command, page.list_command, page.user_command):
        monkeypatch.setattr(command, "start", lambda argv, input=None: page.asked.append(argv) or True)
    yield page
    page.deleteLater()


IMAGES = [{"agent": "claude", "target": "claude", "present": True, "image": "localhost/claude-sandbox:latest"},
          {"agent": "claude", "target": "gpu", "variant": True, "present": True,
           "image": "localhost/claude-sandbox-gpu:latest"}]


def test_the_images_tabs_say_what_they_are_for(images):
    notes = [note.text() for note in images.tab_notes]
    assert "installed" in notes[0] and "Debian, npm and Python packages" in notes[0]
    assert "adds to the selected image when it builds it" in notes[1]
    assert images.tabs.widget(0).isAncestorOf(images.tab_notes[0])
    assert images.tabs.widget(1).isAncestorOf(images.tab_notes[1])
    i18n.set_language("de")
    try:
        assert i18n.tr(notes[0]).startswith("Was im ausgewählten Image installiert ist")
        assert i18n.tr(notes[1]).startswith("Was sandbox-ai dem ausgewählten Image")
    finally:
        i18n.set_language("en")


def test_the_top_image_is_selected_and_the_tabs_say_whose_they_are(images):
    images.show_images(IMAGES)
    assert widgets.selected_keys(images.images) == ["claude"], "the top row, at once"
    assert ["images", "packages", "claude", "--json"] == images.asked[-2][-4:], "its packages are read"
    assert images.shown_for.text() == \
        "Installed packages and extension of claude (image localhost/claude-sandbox:latest)"
    # The user's choice is kept across a refresh ...
    images.images.selectRow(1)
    images.show_images(IMAGES)
    assert widgets.selected_keys(images.images) == ["gpu"] and "of gpu" in images.shown_for.text()
    # ... and a refresh without it selects the top row again.
    images.show_images(IMAGES[:1])
    assert widgets.selected_keys(images.images) == ["claude"] and "of claude" in images.shown_for.text()
    # Your images are not about the row above: no header there.
    images.tabs.setCurrentIndex(2)
    assert images.shown_for.isHidden()
    images.tabs.setCurrentIndex(0)
    assert not images.shown_for.isHidden()
