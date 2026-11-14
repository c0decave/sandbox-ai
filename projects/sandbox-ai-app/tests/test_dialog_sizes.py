"""Every dialog and message box of the app, shown offscreen in English and German, with the usual font and a large
one: its window title can be read whole in a window manager's title bar (the box is at least as wide as its title
needs, within the screen), no text is cut (a one-line label or a button narrower than its text, a wrapping label
lower than its text needs), every button lies inside the dialog, and it fits the screen. The message boxes and the
questions (widgets.inform, warn, confirm, choose, ask_first, Qt's input dialogs) also on a phone-sized screen (about
400 px wide). User (2026-11-14): "box msg in Sitzung & Session umbenennen ist zu klein. Bitte auch alle anderen Boxen
prüfen ob sie korrekt dargestellt werden." """
import os
from types import SimpleNamespace

import pytest

QtCore = pytest.importorskip("PySide6.QtCore", reason="PySide6 is not installed")
QtGui = pytest.importorskip("PySide6.QtGui", reason="PySide6 is not installed")
QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from sandbox_ai_app import backend, i18n, widgets  # noqa: E402
from sandbox_ai_app.i18n import tr  # noqa: E402

from test_layout import clipped  # noqa: E402
from test_ui import env, window  # noqa: E402,F401 - the isolated home and the window of the UI tests

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")

LAPTOP = QtCore.QRect(0, 0, 1366, 728)
PHONE = QtCore.QRect(0, 0, 412, 760)
# What a window manager's title bar takes beside the title, at the least (its close button, a margin): a box narrower
# than its title and this has its title cut ("Rename auburn-kak…").
TITLE_BAR_ROOM = 100


def title_need(dialog):
    font = QtGui.QFont(dialog.font())
    font.setBold(True)
    return QtGui.QFontMetrics(font).horizontalAdvance(dialog.windowTitle()) + TITLE_BAR_ROOM


def problems(dialog, screen):
    """What is wrong with DIALOG (shown) on SCREEN: [text]."""
    app = QtWidgets.QApplication.instance()
    for _ in range(3):
        app.processEvents()
    found = []
    name = f"{type(dialog).__name__} {dialog.windowTitle()!r}"
    width = dialog.width()
    if isinstance(dialog, QtWidgets.QMessageBox):
        # Qt makes a message box at least as wide as its title in a title bar's font plus 50 px, limited by its
        # screen's free width (QMessageBoxPrivate::updateSize). The screen of this test (widgets.screen_area) does not
        # reach Qt, which limits by the offscreen screen (800 px): a box that only Qt's title rule made wider than the
        # test's screen is as wide as that screen on a real one. (Measured otherwise, the large font's family decided:
        # the theme's Inter, left by an earlier test, made the German phone box 447 px, the plain one 409.)
        rule = QtGui.QFontMetrics(QtWidgets.QApplication.font("QMdiSubWindowTitleBar")).horizontalAdvance(
            dialog.windowTitle()) + 50
        if width <= rule:
            width = min(width, screen.width())
    if width > screen.width() or dialog.height() > screen.height():
        found.append(f"{name}: {dialog.width()}x{dialog.height()} beyond the screen {screen.width()}x{screen.height()}")
    need = min(title_need(dialog), screen.width() - 8)  # (on a phone: nearly the whole width)
    if dialog.width() + 1 < need:
        found.append(f"{name}: {dialog.width()} px wide, its title needs {need}")
    found += [f"{name}: {item}" for item in clipped(dialog)]
    inside = dialog.rect().adjusted(-1, -1, 1, 1)
    def scrolled(widget):  # (in a scroll area: it may lie beyond the dialog's edge, scrolled to)
        parent = widget.parentWidget()
        while parent is not None and parent is not dialog:
            if isinstance(parent, QtWidgets.QAbstractScrollArea):
                return True
            parent = parent.parentWidget()
        return False
    for button in dialog.findChildren(QtWidgets.QAbstractButton):
        if button.isVisibleTo(dialog) and button.width() > 0 and not scrolled(button):
            where = QtCore.QRect(button.mapTo(dialog, QtCore.QPoint(0, 0)), button.size())
            if not inside.contains(where):
                found.append(f"{name}: the button {button.text()!r} lies outside ({where} in {dialog.rect()})")
    for area in dialog.findChildren(QtWidgets.QAbstractScrollArea):
        if area.isVisibleTo(dialog) and area.horizontalScrollBar().isVisible() and \
                isinstance(area, (QtWidgets.QPlainTextEdit, QtWidgets.QTextEdit)):
            found.append(f"{name}: its text {area.objectName()!r} scrolls sideways")
    return found


@pytest.fixture
def screens(qapp, monkeypatch):
    """The dialogs as the app shows them (fitting_dialogs, as main.py installs it), on a screen of the test's size;
    every dialog that is run (exec) is shown, measured and cancelled. Yields (set the screen, the findings, the large
    font on or off)."""
    for cls in (QtWidgets.QDialog, QtWidgets.QMessageBox):  # (taken back after the test: the layout tests size
        monkeypatch.setattr(cls, "showEvent", cls.showEvent)  # their dialogs as screens of their own)
    widgets.fitting_dialogs()
    room = {"screen": LAPTOP}
    monkeypatch.setattr(widgets, "screen_area", lambda _widget: QtCore.QRect(room["screen"]), raising=False)
    found = []
    shown = []

    def measured(dialog, *args):
        dialog.show()
        shown.append(dialog)
        found.extend(problems(dialog, room["screen"]))
        dialog.reject()
        return QtWidgets.QDialog.DialogCode.Rejected
    monkeypatch.setattr(QtWidgets.QDialog, "exec", measured)
    font = QtGui.QFont(qapp.font())

    def large(on):
        bigger = QtGui.QFont(font)
        if on:
            bigger.setPointSizeF((font.pointSizeF() if font.pointSizeF() > 0 else 10) * 1.6)
        qapp.setFont(bigger)

    def use(screen):
        room["screen"] = screen
    yield SimpleNamespace(use=use, found=found, shown=shown, large=large)
    qapp.setFont(font)


def showing(dialog, screens):
    """DIALOG shown as the app shows a dialog of its own, measured and closed."""
    dialog.show()
    screens.found.extend(problems(dialog, QtCore.QRect(widgets.screen_area(dialog))))
    dialog.close()


LANGUAGES = ["en", "de"]
FONTS = ["usual", "large"]


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("font", FONTS)
@pytest.mark.parametrize("screen", ["laptop", "phone"])
def test_every_message_box_and_question_can_be_read(window, screens, monkeypatch, language, font, screen):  # noqa: F811
    """The shared message boxes and questions with the app's own texts: short and long messages, a confirmation, the
    three-way close of a terminal tab, Stop all, the Rename question and its outcome, a choice from a list."""
    i18n.set_language(language)
    screens.large(font == "large")
    screens.use(LAPTOP if screen == "laptop" else PHONE)
    monkeypatch.setattr(widgets, "AUTO_ANSWER", None)  # the boxes themselves, not the tests' answers
    monkeypatch.setattr(widgets, "show_veiled", lambda dialog: showing(dialog, screens))
    monkeypatch.setattr(widgets, "show_modeless", lambda dialog: showing(dialog, screens))
    name = "auburn-kakapo-bamboo"
    title = tr("Rename {name}", name=name)
    widgets.inform(window, title, f"{name} is now {name}-2")  # the outcome of the rename
    widgets.inform(window, tr("Stop all"), tr("Nothing runs that could be stopped."))
    widgets.warn(window, tr("Change folder"), "sandbox-ai: " + "the folder cannot be read: /srv/projects/" + "x" * 90)
    widgets.inform(window, tr("Stop all"), "\n\n".join(f"{name}-{n}: stopped" for n in range(40)))  # long
    widgets.confirm(window, tr("Stop all"), tr("Stop {what}? It ends; its HOME is kept and Resume starts it again.",
                                               what=tr("session {name}", name=name)), tr("Stop"), destructive=True)
    widgets.choose(window, tr("Close terminal"), tr(
        "{name} keeps running when only its tab is closed (attach it again from Sandboxes & sessions or the "
        "Sessions list). Stop the session too?", name=name),
        [("close", tr("Close tab")), ("stop", tr("Stop session"))], cancel=tr("Cancel"), destructive=("stop",))
    widgets.ask_first(window, "start-delete", tr("Delete {name}", name=name), tr("Delete {name}", name=name) + "?",
                      tr("Delete"))
    QtWidgets.QInputDialog.getText(window, title, tr("New name (1-40 letters, digits, _ . -; its run id stays)"),
                                   text=name)
    QtWidgets.QInputDialog.getItem(window, tr("Remove device"), tr("Device"), ["/dev/ttyUSB0", "/dev/video0"], 0,
                                   False)
    QtWidgets.QInputDialog.getInt(window, tr("Limits of {run}", run=name), tr("Processes"), 512)
    QtWidgets.QMessageBox.question(window, title, tr("Delete {name}", name=name) + "?")
    assert screens.shown, "the questions were shown"
    assert screens.found == [], "\n".join(screens.found)


def dialogs(window):
    """[(what, make)]: every dialog of the app's own, made with what its callers give it."""
    from sandbox_ai_app import (credentials_page, files_page, help_view, hosts_page, identities_page, images_page,
                                kits_page, llm_page, model_picker, preset_gallery, reports, settings, team_page, ui,
                                vpn_page, wizard)
    name = "auburn-kakapo-bamboo"
    targets = [{"name": name, "agent": "claude", "state": "running", "mcp": []}]
    instance = backend.Instance("session", f"session:{name}", name, run=f"claude-{name}", session=name,
                                agent="claude", state="running")
    host = {"name": "build-box", "address": "build.example.org", "user": "me", "port": 22}
    rows = [{"label": "qwen2.5-coder:32b", "value": "local:qwen2.5-coder:32b", "hint": "local", "server": "local",
             "model": "qwen2.5-coder:32b", "tags": ["code"], "params": 32, "size": 19 << 30}]
    return [
        ("network", lambda: ui.NetworkDialog(window, {}, tr("Allow during the run of {run}", run=name))),
        ("allowed", lambda: ui.AllowedDialog(window, tr("Allowed for {run}", run=name), "api.example.org\n",
                                             lambda: None)),
        ("device", lambda: ui.DeviceDialog(window, [("/dev/ttyUSB0", None, "serial"), ("/dev/mem", "raw memory",
                                                                                         None)])),
        ("workspace", lambda: ui.WorkspaceDialog(window, f"claude-{name}")),
        ("limits", lambda: ui.LimitsDialog(window, f"claude-{name}", current={"memory": 8 << 30, "cpus": 2, "pids": 512})),
        ("live mcp", lambda: ui.LiveMcpDialog(window, name, servers=[("fetch", "defined"), ("github", "")], agent="claude")),
        ("resume with", lambda: ui.ResumeDialog(window, name, running=True, servers=["fetch"])),
        ("mcp start", lambda: ui.McpStartDialog(window, "fetch", targets)),
        ("attach server", lambda: ui.AttachServerDialog(window, "fetch", targets)),
        ("privacy results", lambda: ui.PrivacyResults(window, tr("Privacy check"), [], ["nothing to check"])),
        ("model picker", lambda: model_picker.ModelPicker(window, rows, "local:qwen2.5-coder:32b")),
        ("new kit", lambda: kits_page.NewKitDialog(window)),
        ("new skill", lambda: kits_page.NewSkillDialog(window, ["claude", "codex"])),
        ("plugin", lambda: kits_page.PluginDialog(window, [])),
        ("marketplace", lambda: kits_page.MarketplaceDialog(window)),
        ("hook", lambda: kits_page.HookDialog(window, ["claude"])),
        ("remove from kit", lambda: kits_page.RemoveDialog(window, [("skill", "claude", "review")])),
        ("target kit", lambda: kits_page.TargetKitDialog(window, tr("Add to a kit of yours"), ["mine"])),
        ("file target", lambda: images_page.FileTargetDialog(window, "/srv/app/Containerfile")),
        ("build", lambda: images_page.BuildDialog(window, "claude")),
        ("recipe", lambda: images_page.RecipeDialog(window, ["claude", "codex"], "claude")),
        ("pull", lambda: images_page.PullDialog(window)),
        ("build image", lambda: images_page.BuildImageDialog(window)),
        ("add host", lambda: hosts_page.AddHostDialog(window)),
        ("edit host", lambda: hosts_page.EditHostDialog(host, window)),
        ("install on host", lambda: hosts_page.InstallDialog(host, "0.1.95", window)),
        ("team start", lambda: team_page.StartDialog(window, tr("this machine"), False)),
        ("team post", lambda: team_page.PostDialog(window, "alpha", ["claude", "codex"])),
        ("vpn", lambda: vpn_page.VpnDialog(window)),
        ("tour search", lambda: help_view.TourSearchDialog(window)),
        ("report", lambda: reports.ReportDialog(window, tr("Report"), tr("Report"), name)),
        ("llm", lambda: llm_page.LlmDialog(window)),
        ("save preset", lambda: preset_gallery.SavePresetDialog(window, window.sai)),
        ("owner", lambda: files_page.OwnerDialog(window, 3, False)),
        ("identity", lambda: identities_page.IdentityDialog(window, "", {}, [], [])),
        ("give", lambda: credentials_page.GiveDialog(window, [instance])),
        ("server", lambda: wizard.ServerDialog(window.sai, parent=window)),
        ("settings", lambda: settings.SettingsDialog(window.sai, window)),
        ("message", lambda: widgets.MessageDialog(window, QtWidgets.QMessageBox.Icon.Warning, tr("Stop all"),
                                                  "x " * 400)),
    ]


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("font", FONTS)
def test_every_dialog_can_be_read(window, screens, monkeypatch, language, font):  # noqa: F811
    """Each dialog of the app's own on a laptop's screen: its title whole, no text cut, its buttons inside."""
    i18n.set_language(language)
    screens.large(font == "large")
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    made = []
    for what, make in dialogs(window):
        try:
            dialog = make()
        except Exception as exc:  # noqa: BLE001 - the test's own arguments are wrong: said, never skipped
            raise AssertionError(f"{what}: {exc!r}") from exc
        made.append(what)
        showing(dialog, screens)
        dialog.deleteLater()
    assert len(made) == len(dialogs(window))
    assert screens.found == [], "\n".join(screens.found)
