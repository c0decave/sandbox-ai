"""Every dialog and message box of the app, shown offscreen in English and German, with the usual font and a large
one: its window title can be read whole in a window manager's title bar (the box is at least as wide as its title
needs, within the screen), no text is cut (a one-line label or a button narrower than its text, a wrapping label
lower than its text needs), every button lies inside the dialog, and it fits the screen. The message boxes and the
questions (widgets.inform, warn, confirm, choose, ask_first, Qt's input dialogs) also on a phone-sized screen (about
400 px wide). User (2026-11-14): "box msg in Sitzung & Session umbenennen ist zu klein. Bitte auch alle anderen Boxen
prüfen ob sie korrekt dargestellt werden." """
import os
import re
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


def problems(dialog, screen, rule=True):
    """What is wrong with DIALOG (shown) on SCREEN: [text]; RULE: what breaks the one rule too (rule_problems)."""
    app = QtWidgets.QApplication.instance()
    for _ in range(3):
        app.processEvents()
    found = rule_problems(dialog, screen) if rule else []
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
    monkeypatch.setattr(QtWidgets.QDialog, "hideEvent", QtWidgets.QDialog.hideEvent)
    monkeypatch.setattr(widgets, "PLACES", {})  # (where a dialog was moved: none yet)
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


# -- the one rule (widgets.dialog_rule, widgets.place) --

SCREENS = {"1280x800": QtCore.QRect(0, 0, 1280, 800), "1920x1080": QtCore.QRect(0, 0, 1920, 1080)}
# The app's looks: light and dark, the most contrast (hacker: green on black) and neon, and those that draw bigger:
# unicorn (round, wide paddings), win311 (bevels), fallout (a larger font of its own).
THEMES = ["light", "dark", "hacker", "hacker_neon", "unicorn", "win311", "fallout"]
LEAST = 420  # px: the least width of a dialog (more with a larger font)
NAME_CHARS = 40  # a name field alone in its row shows about this many characters
CENTRED, LARGE, INSET = 2, 0.7, 24


def outer(widget):
    """WIDGET's place and size (QWidget's own: a dialog may have a .size of its own - the Properties' label)."""
    return QtCore.QRect(QtWidgets.QWidget.pos(widget), QtWidgets.QWidget.size(widget))


def expected_place(dialog, screen):
    """Where the rule puts DIALOG on SCREEN: centred over the window it was opened from (its parent's window, else
    the screen), at that window's top left (24 px in) when wider or higher than 70% of the screen; inside the
    screen."""
    size = QtWidgets.QWidget.size(dialog)
    parent = dialog.parentWidget()
    anchor = parent.window() if parent is not None else QtWidgets.QApplication.activeWindow()
    over = anchor.frameGeometry() if anchor is not None and anchor is not dialog and anchor.isVisible() else screen
    if size.width() > screen.width() * LARGE or size.height() > screen.height() * LARGE:
        x, y = over.left() + INSET, over.top() + INSET
    else:
        x, y = over.center().x() - size.width() // 2, over.center().y() - size.height() // 2
    x = max(screen.left(), min(x, screen.right() - size.width() + 1))
    y = max(screen.top(), min(y, screen.bottom() - size.height() + 1))
    return QtCore.QPoint(x, y)


def rule_problems(dialog, screen):
    """What breaks the one rule for DIALOG (shown) on SCREEN: [text]. Inside the screen and at most 90% of it (all
    of a phone's); at least LEAST wide (its font's 52 average characters with a larger one); a text field alone in its
    row shows NAME_CHARS characters; placed by the rule (expected_place)."""
    if not widgets.own_rule(dialog):
        return []
    found = []
    name = f"{type(dialog).__name__} {dialog.windowTitle()!r}"
    placed = outer(dialog)
    if not screen.contains(placed):
        found.append(f"{name}: at {placed} - not inside the screen {screen}")
    share = 1.0 if screen.width() < 800 else 0.9
    if placed.width() > screen.width() * share + 1 or placed.height() > screen.height() * share + 1:
        found.append(f"{name}: {placed.width()}x{placed.height()} - more than {share:.0%} of the screen "
                     f"{screen.width()}x{screen.height()}")
    least = min(max(LEAST, 52 * dialog.fontMetrics().averageCharWidth()), screen.width() - 8)
    if placed.width() + 1 < least and dialog.maximumWidth() >= least:
        found.append(f"{name}: {placed.width()} px wide, less than the least {least}")
    limit = int(screen.width() * share) - 160
    for field in dialog.findChildren(QtWidgets.QLineEdit):
        if not field.isVisibleTo(dialog) or widgets.field_least(field) == 0 or widgets.shares_row(field):
            continue
        need = min(NAME_CHARS * field.fontMetrics().averageCharWidth(), limit)
        if field.width() + 1 < need:
            found.append(f"{name}: its field {field.accessibleName() or field.placeholderText()!r} is {field.width()} "
                         f"px wide - less than {NAME_CHARS} characters ({need} px)")
    want = expected_place(dialog, screen)
    if (placed.topLeft() - want).manhattanLength() > CENTRED:
        found.append(f"{name}: at {placed.topLeft()}, the rule puts it at {want}")
    return found


def showing(dialog, screens):
    """DIALOG shown as the app shows a dialog of its own, measured and closed (also a plain dialog shown with
    show_veiled: measured once it is shown)."""
    dialog.show()
    screens.found.extend(problems(dialog, QtCore.QRect(widgets.screen_area(dialog))))
    dialog.close()


LANGUAGES = ["en", "de"]
FONTS = ["usual", "large"]


def ask_everything(window):
    """The shared message boxes and questions with the app's own texts (each shown, measured and cancelled by the
    screens fixture)."""
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
    QtWidgets.QInputDialog.getText(window, tr("New folder"), widgets.shown_as_text(tr(
        "Name of the new folder in {folder}:", folder=LONG_FOLDER)))


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
    ask_everything(window)
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
    ] + more_dialogs(window)


LONG_FOLDER = "/srv/projekte/kunden/" + "/".join(["ein-sehr-langer-ordnername-mit-vielen-teilen"] * 3)


def files_root():
    """A folder of the test's own for the file manager's dialogs: two files in it."""
    from pathlib import Path
    from sandbox_ai_app import files
    folder = Path(os.environ["XDG_DATA_HOME"]).parent / "dialog-files"
    folder.mkdir(exist_ok=True)
    for name in ("notes.txt", "a-file-with-a-rather-long-name-for-its-properties.txt"):
        (folder / name).write_text("x")
    return files.Root(folder), folder


def more_dialogs(window):
    """[(what, make)]: the rest of the app's dialog classes (test_every_dialog_class_is_measured: none left out)."""
    from sandbox_ai_app import (agent_parts_view, bridges_view, bundle_dialogs, checkpoints_dialog, file_editor,
                                storage_view,
                                file_properties, file_search_view, guide_view, help_view, llm_loaded_view, llm_params, llm_params_view,
                                kits_live_view, mcp_copy_view, mcp_fields, notify_view, package_search, pages, preset_gallery,
                                presets, registry_browser, remote_chooser, remote_source, schedules_view,
                                session_inspect_dialog, session_logins_view, session_state_dialogs, session_transfer,
                                traffic_page, ui, wizard)
    from sandbox_ai_app import board as B
    from sandbox_ai_app import credentials_page, device_window, images_page
    name = "auburn-kakapo-bamboo"
    root, folder = files_root()
    long_names = [f"projekt-{n}-mit-einem-langen-namen" for n in range(3)]
    return [
        ("import parts", lambda: agent_parts_view.ImportDialog(
            window, tr("Import into a kit"), tr("Import into a kit") + ": " + LONG_FOLDER, kits=["mine"])),
        ("checkpoints", lambda: checkpoints_dialog.CheckpointsDialog(window, window.sai, name)),
        ("storage", lambda: storage_view.StorageDialog(window, window.sai, "local", name)),
        ("export bundle", lambda: bundle_dialogs.ExportDialog(window, window.sai, name, session=name)),
        ("import bundle", lambda: bundle_dialogs.ImportDialog(window, window.sai, path=LONG_FOLDER + "/board.tar")),
        ("properties", lambda: file_properties.PropertiesDialog(
            window, root, [(n, n) for n in sorted(os.listdir(folder))], str(folder), False)),
        ("search", lambda: file_search_view.SearchDialog(window, SimpleNamespace(
            root=root, relative="", side="sandbox", path_at=lambda _relative: LONG_FOLDER))),
        ("bridge", lambda: bridges_view.BridgeDialog(window, [
            {"name": n, "run": f"claude-{n}", "agent": "claude", "workspace": LONG_FOLDER, "host": "local"}
            for n in (name, name + "-2")])),
        ("editor", lambda: file_editor.EditorWindow(window, root, "notes.txt", str(folder / "notes.txt"))),
        ("device sign-in", lambda: device_window.DeviceCodeWindow(
            "https://auth.openai.com/codex/device?" + "x" * 300, "ABCD-12345", "codex", where="build-box-with-a-long-name",
            opened=True, parent=window, opener=lambda url: True)),
        ("new variant", lambda: images_page.NewVariantDialog(window, ["claude", "codex"], "claude", lambda _n: True)),
        ("folder name", lambda: remote_chooser.FolderNameDialog(window, LONG_FOLDER, "build-box", long_names)),
        ("remote chooser", lambda: remote_chooser.RemoteChooser(window, window.sai, "build-box",
                                                                tr("Open project folder…"))),
        ("load model", lambda: llm_loaded_view.LoadDialog("lmstudio", "lmstudio", [{"name": "qwen2.5-coder:32b"}],
                                                          lambda _m: 8192, "build-box", window)),
        ("loaded models", lambda: llm_loaded_view.LoadedDialog(lambda *a, **k: (1, "", "not here"), "lmstudio",
                                                               "lmstudio", "build-box", "build-box", window)),
        ("help", lambda: help_view.HelpDialog(None, window)),
        ("login", lambda: credentials_page.LoginDialog(window, tr("Add login…"), ["GH_PUSH"],
                                                       ((tr("Git server"), "url", ""), (tr("User"), "user", "")))),
        # the Credentials page's Edit… and Give… dialogs (2026-11-19)
        ("edit login", lambda: credentials_page.LoginDialog(
            window, tr("Edit Git login"), ["GH_PUSH"], ((tr("Git server"), "url", ""), (tr("User"), "user", "")),
            edit={"url": "https://git.example.org:8443", "user": "ein-recht-langer-benutzername", "secret": "GH_PUSH"})),
        ("edit secret", lambda: credentials_page.SecretDialog(
            window, "GIT_TOKEN_EIN_RECHT_LANGER_NAME", [f"Git login https://git-{n}.example.org" for n in range(4)],
            "build-box")),
        ("host for a secret", lambda: credentials_page.HostDialog(window, ["build-box", "ein-host-mit-langem-namen"],
                                                                  "GIT_TOKEN_EIN_RECHT_LANGER_NAME")),
        ("give a login", lambda: credentials_page.LoginGiveDialog(
            window, [__import__("types").SimpleNamespace(run=f"codex-{n}a2b3c4d", name=f"{name}-{n}", agent="codex")
                     for n in range(3)], "codex", tr("of session {name}", name=name), host="build-box")),
        ("edit vpn", lambda: __import__("sandbox_ai_app.vpn_page", fromlist=["x"]).VpnEditDialog(
            window, "ein-profil-mit-langem-namen", "tailscale",
            {"provider": "headscale", "auth-key-secret": "TS_KEY", "exit-node": "exit-node-1.example.org",
             "login-server": "https://headscale.example.org", "hostname": "sandbox-ai-box"}, "build-box")),
        ("preset gallery", lambda: preset_gallery.PresetGallery(window.sai, None, "claude", {}, parent=window)),
        ("placeholder form", lambda: preset_gallery.PlaceholderForm(window.sai, presets.find("remote-llm"), window)),
        ("node guide", lambda: guide_view.NodeGuide(B.Board().add("vpn"), [], window)),
        ("llm params", lambda: llm_params_view.ParamsDialog(
            {"name": "lmstudio", "kind": "lmstudio"}, llm_params.local_catalogue(window.sai, "lmstudio") or [],
            host_label="build-box", parent=window)),
        ("schedule", lambda: schedules_view.ScheduleDialog(lambda _fields: None, parent=window)),
        ("schedule history", lambda: schedules_view.HistoryDialog(window.sai, "local", "nightly", [
            {"run": "1", "reason": "timer", "started": 1700000000, "status": "ok", "seconds": 61,
             "changes": {"files": 3, "commits": 1}}], window)),
        ("server properties", lambda: mcp_fields.ServerPropertiesDialog(window, window.sai, {"name": "fetch"})),
        ("mcp copy", lambda: mcp_copy_view.McpCopyDialog(window, window.sai, "build-box")),
        ("registry", lambda: registry_browser.RegistryBrowser(window, lambda *a, **k: [])),
        ("derive", lambda: package_search.DeriveDialog(window, window.images, "mine", "Alpine Linux 3.20 (apk)")),
        ("phone channel", lambda: notify_view.ChannelDialog(lambda _fields: None, parent=window)),
        ("session memory", lambda: session_inspect_dialog.SessionInspectDialog(window, window.sai, name)),
        ("session transfer", lambda: session_transfer.SessionTransferDialog(window, window.sai, name, move=True)),
        ("session duplicate", lambda: session_state_dialogs.DuplicateDialog(window, window.sai, name)),
        ("session export with state", lambda: session_state_dialogs.StateExportDialog(window, window.sai, name)),
        ("session import", lambda: session_state_dialogs.ImportDialog(window, window.sai, path=LONG_FOLDER + "/s.tar.gz")),
        ("remote source", lambda: remote_source.RemoteSourceDialog(window, "ssh://build-box" + LONG_FOLDER)),
        ("questions", lambda: pages.QuestionsWindow(window.approvals)),
        ("secret value", lambda: widgets.SecretValueDialog(window, tr("Keep the key"),
                                                           "ANTHROPIC_API_KEY: " + tr("Value"), tr("Keep"))),
        ("chat", lambda: traffic_page.ChatWindow(window, tr("Request #{n} · {model}", n=3, model="qwen"),
                                                 "<p>" + "Hallo " * 200 + "</p>")),
        ("add server", lambda: ui.AddServerDialog(window, {"name": "files", "mounts": [{"target": "/data"}]})),
        ("privacy", lambda: ui.PrivacyDialog(window, window.sai)),
        ("wizard", lambda: wizard.SandboxWizard(window.sai, lambda: {}, window)),
        ("session login", lambda: session_logins_view.SessionLoginDialog(window, {
            "name": name, "agent": "claude", "account": "someone@example.org", "file": LONG_FOLDER + "/.claude.json",
            "fingerprint": "ab" * 32})),
        ("give to a running sandbox", lambda: kits_live_view.GiveDialog(window, window.sai, start=name,
                                                                        value=LONG_FOLDER + "/skills/review")),
        ("text", lambda: text_dialog(window)),
    ]


def text_dialog(window):
    """widgets.show_text's dialog (a plain QDialog), shown by it."""
    shown = []
    real = widgets.show_veiled
    try:
        widgets.show_veiled = lambda dialog: shown.append(dialog) or dialog
        saved, widgets.AUTO_ANSWER = widgets.AUTO_ANSWER, None
        widgets.show_text(window, tr("Logins"), "\n".join(f"line {n}: " + "text " * 30 for n in range(80)))
    finally:
        widgets.show_veiled, widgets.AUTO_ANSWER = real, saved
    return shown[0]


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


# -- the one rule in every theme; where a dialog goes; what scrolls (user 2026-11-18: "z. B. im Dateimanager ist die
# Box bei Neuer Ordner zu klein. Bitte alle Fenster/Boxen, die geöffnet werden, in allen Themes prüfen, ob sie
# richtig groß sind, sodass man sie benutzen kann. Idealerweise je nach Größe der Box zentriert oder oben links.") --

@pytest.fixture
def looks(qapp):
    """theme.apply as the settings apply it, and the large font on top of the theme's own; the system's look after
    the test."""
    from sandbox_ai_app import theme

    def use(mode, large=False):
        theme.apply(mode, qapp)
        if large:
            bigger = QtGui.QFont(qapp.font())
            bigger.setPointSizeF((bigger.pointSizeF() if bigger.pointSizeF() > 0 else 10) * 1.6)
            qapp.setFont(bigger)
    yield use
    theme.apply("system", qapp)
    i18n.set_language("en")


def window_on(window, screen):
    """WINDOW shown on SCREEN, a little inside its top left corner, the size of a usual window there."""
    window.setGeometry(QtCore.QRect(screen.left() + 60, screen.top() + 40, int(screen.width() * 0.75),
                                    int(screen.height() * 0.8)))
    window.show()
    QtWidgets.QApplication.processEvents()


@pytest.mark.parametrize("font", FONTS)
@pytest.mark.parametrize("size", list(SCREENS))
@pytest.mark.parametrize("mode", THEMES)
def test_every_dialog_follows_the_rule_in_every_theme(window, screens, looks, monkeypatch, mode, size,  # noqa: F811
                                                       font):
    """Every dialog, message box and question in German (the longer texts) in each theme, with the usual font and a
    large one, on a 1280x800 and a 1920x1080 screen, opened over the main window: nothing cut, every button inside,
    every name field about 40 characters wide, at least 420 px and at most 90% of the screen, and centred over the
    window - a large one at its top left."""
    i18n.set_language("de")
    looks(mode, font == "large")
    screen = SCREENS[size]
    screens.use(screen)
    window_on(window, screen)
    monkeypatch.setattr(widgets, "AUTO_ANSWER", None)
    monkeypatch.setattr(widgets, "show_veiled", lambda dialog: showing(dialog, screens))
    monkeypatch.setattr(widgets, "show_modeless", lambda dialog: showing(dialog, screens))
    ask_everything(window)
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    for what, make in dialogs(window):
        dialog = make()
        showing(dialog, screens)
        dialog.deleteLater()
    assert screens.found == [], "\n".join(screens.found)


def own_dialog_classes():
    """{module.class: class} of every dialog class of the app's own (a QDialog, a QWizard...)."""
    import importlib
    import pkgutil
    import sandbox_ai_app
    for info in pkgutil.iter_modules(sandbox_ai_app.__path__):
        if info.name != "__main__":
            importlib.import_module(f"sandbox_ai_app.{info.name}")
    found, todo = {}, [QtWidgets.QDialog]
    while todo:
        for cls in todo.pop().__subclasses__():
            todo.append(cls)
            if cls.__module__.startswith("sandbox_ai_app."):
                found[f"{cls.__module__.rpartition('.')[2]}.{cls.__qualname__}"] = cls
    return found


# A dialog class not measured on its own, and why.
NOT_MADE = {"kits_page.FormDialog": "the base of the kits page's dialogs, each of which is measured",
            "session_state_dialogs.StateDialog": "the base of Duplicate…, Export with state… and Import…, each measured"}


def test_every_dialog_class_is_measured(window, screens, monkeypatch):  # noqa: F811
    """A dialog class added to the app fails here until more_dialogs makes it (with realistic, long content): every
    dialog is measured by the rule in every theme."""
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    made = set()
    for what, make in dialogs(window):
        dialog = make()
        made.add(f"{type(dialog).__module__.rpartition('.')[2]}.{type(dialog).__qualname__}")
        dialog.deleteLater()
    missing = sorted(set(own_dialog_classes()) - made - set(NOT_MADE))
    assert missing == [], f"dialogs no test measures (add them to more_dialogs): {missing}"


def test_every_dialog_takes_the_rule_when_it_is_shown():
    """The rule is applied as a dialog is shown (fitting_dialogs wraps QDialog's and QMessageBox's showEvent): a
    dialog class with a showEvent of its own hands the event on, and none places itself."""
    import inspect
    found = []
    for name, cls in own_dialog_classes().items():
        source = inspect.getsource(cls)
        if "def showEvent" in source and "super().showEvent(" not in source:
            found.append(f"{name}: its showEvent never calls super().showEvent - the rule never runs for it")
        if re.search(r"\bself\.(move|setGeometry)\(", source):
            found.append(f"{name}: places itself (self.move / self.setGeometry) - widgets.place does that")
    assert found == [], "\n".join(found)


def test_every_extra_window_takes_the_rule():
    """An extra window that is no dialog (the log window, a video, a browser) gets the rule from close_controls (its
    WindowCloseKeys places it as it is first shown): each class made as a window of its own calls it."""
    import importlib
    import inspect
    import pkgutil
    import sandbox_ai_app
    found, windows = [], []
    for info in pkgutil.iter_modules(sandbox_ai_app.__path__):
        if info.name == "__main__":
            continue
        module = importlib.import_module(f"sandbox_ai_app.{info.name}")
        for name, cls in vars(module).items():
            if not isinstance(cls, type) or cls.__module__ != module.__name__ or \
                    not issubclass(cls, QtWidgets.QWidget) or issubclass(cls, QtWidgets.QDialog):
                continue
            source = inspect.getsource(cls)
            if re.search(r"Qt\.WindowType\.Window\)", source):
                windows.append(name)
                if "close_controls(self" not in source:
                    found.append(f"{info.name}.{name}: a window of its own without close_controls (no [x], no rule)")
    assert {"LogWindow", "VideoPlayer", "LoginBrowser"} <= set(windows), windows
    assert found == [], "\n".join(found)


def test_a_large_dialog_opens_at_the_windows_top_left_a_small_one_centred(window, screens):  # noqa: F811
    """User: "je nach Größe der Box zentriert oder oben links". A dialog wider or higher than 70% of the screen opens
    24 px inside the top left corner of the window it was opened from, a smaller one centred over it; both inside the
    screen, also when the window lies mostly beyond the screen's edge."""
    screen = SCREENS["1280x800"]
    screens.use(screen)
    window_on(window, screen)
    big = QtWidgets.QDialog(window)
    big.resize(1000, 640)  # (80% of the screen's height)
    big.show()
    QtWidgets.QApplication.processEvents()
    frame = window.frameGeometry()
    assert big.pos() == frame.topLeft() + QtCore.QPoint(INSET, INSET), (big.pos(), frame)
    big.close()
    small = QtWidgets.QDialog(window)
    small.resize(480, 200)
    small.show()
    QtWidgets.QApplication.processEvents()
    assert abs(outer(small).center().x() - frame.center().x()) <= CENTRED
    assert abs(outer(small).center().y() - frame.center().y()) <= CENTRED
    small.close()
    window.move(screen.right() - 200, screen.bottom() - 150)  # mostly beyond the screen's edge
    QtWidgets.QApplication.processEvents()
    for dialog in (big, small):
        dialog.show()
        QtWidgets.QApplication.processEvents()
        assert screen.contains(outer(dialog)), (outer(dialog), screen)
        dialog.close()


def test_a_dialog_the_human_moved_or_sized_opens_there_again(window, screens):  # noqa: F811
    """A dialog moved or sized by the human keeps that place and size for the app session: the next one of its kind
    opens there (its size never below what it needs) - another kind still opens centred."""
    from sandbox_ai_app import kits_page
    screen = SCREENS["1920x1080"]
    screens.use(screen)
    window_on(window, screen)
    first = kits_page.NewKitDialog(window)
    first.show()
    QtWidgets.QApplication.processEvents()
    first.move(first.pos() + QtCore.QPoint(300, -120))
    first.resize(first.width() + 150, first.height() + 80)
    left = outer(first)
    first.close()
    again = kits_page.NewKitDialog(window)
    again.show()
    QtWidgets.QApplication.processEvents()
    assert outer(again) == left
    again.close()
    other = kits_page.NewSkillDialog(window, ["claude"])
    other.show()
    QtWidgets.QApplication.processEvents()
    assert (outer(other).topLeft() - expected_place(other, screen)).manhattanLength() <= CENTRED
    other.close()
    # A dialog only shown and closed, never moved: nothing remembered.
    assert list(widgets.PLACES) == [widgets.place_key(first)]


def test_a_dialog_higher_than_the_screen_scrolls_its_buttons_stay(window, screens):  # noqa: F811
    """A dialog whose content needs more height than 90% of the screen: as high as that, its content scrolls and its
    buttons stay below, whole and inside it."""
    screen = SCREENS["1280x800"]
    screens.use(screen)
    window_on(window, screen)
    dialog = QtWidgets.QDialog(window)
    dialog.setWindowTitle(tr("Settings"))
    form = QtWidgets.QFormLayout(dialog)
    for n in range(60):
        form.addRow(f"Feld {n}", QtWidgets.QLineEdit())
    buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok |
                                         QtWidgets.QDialogButtonBox.StandardButton.Cancel)
    form.addRow(buttons)
    dialog.show()
    found = problems(dialog, screen)
    assert found == [], "\n".join(found)
    assert dialog.height() <= screen.height() * 0.9 + 1
    area = dialog.findChild(QtWidgets.QScrollArea, "dialogScroll")
    assert area is not None and area.verticalScrollBar().maximum() > 0, "its content scrolls"
    for button in buttons.buttons():
        where = QtCore.QRect(button.mapTo(dialog, QtCore.QPoint(0, 0)), button.size())
        assert dialog.rect().contains(where) and not area.isAncestorOf(button), button.text()
    dialog.close()


def test_a_long_text_wraps_at_a_readable_width_a_path_anywhere(window, screens, monkeypatch):  # noqa: F811
    """A question with a long sentence and a long path wraps at about 80 characters - the path broken after its
    slashes, not one line as wide as the screen; its field shows 40 characters."""
    screen = SCREENS["1920x1080"]
    screens.use(screen)
    window_on(window, screen)
    path = "/srv/" + "/".join(f"ordner-{n}-mit-einem-langen-namen" for n in range(8))
    label = ("Der Name des neuen Ordners, der in dem unten genannten Ordner angelegt wird, wenn Sie auf OK klicken: "
             + path)
    seen = []

    def run(dialog, *args):
        dialog.show()
        seen.append((outer(dialog), dialog.findChild(QtWidgets.QLabel), dialog.findChild(QtWidgets.QLineEdit)))
        screens.found.extend(problems(dialog, screen))
        dialog.reject()
        return QtWidgets.QDialog.DialogCode.Rejected
    monkeypatch.setattr(QtWidgets.QDialog, "exec", run)
    QtWidgets.QInputDialog.getText(window, tr("New folder"), label)
    (placed, shown, field), = seen
    chars = shown.fontMetrics().averageCharWidth()
    assert shown.wordWrap() and shown.width() <= 90 * chars, (shown.width(), 90 * chars)
    assert placed.width() <= 110 * chars, "not as wide as the path"
    assert field.width() >= NAME_CHARS * field.fontMetrics().averageCharWidth()
    assert screens.found == [], "\n".join(screens.found)


def test_the_new_folder_dialog_of_the_file_manager_is_wide_enough(window, screens, monkeypatch, tmp_path):  # noqa: F811
    """The user's example, end to end: Files, F7 - the New folder dialog is centred over the window, its name field
    shows 40 characters whatever the folder (in "/" the box was as narrow as its short question: 17 characters), a
    long folder's question wraps, a long name fits whole; OK makes the folder."""
    from PySide6.QtTest import QTest
    from conftest import wait_until
    from test_files_page import settle
    screen = SCREENS["1280x800"]
    screens.use(screen)
    deep = tmp_path / "kunden" / "ein-sehr-langer-ordnername-mit-vielen-teilen" / "noch-ein-langer-ordnername-hier"
    deep.mkdir(parents=True)
    window_on(window, screen)
    window.show_page("files")
    page = window.files
    name = "Ein Ordner mit einem recht langen Namen für 2026"
    seen, wanted = [], {"make": False}

    def run(dialog, *args):
        dialog.show()
        found = problems(dialog, screen)
        field = dialog.findChild(QtWidgets.QLineEdit)
        field.setText(name)
        seen.append((found, field.width(), field.fontMetrics(), dialog.windowTitle()))
        if not wanted["make"]:  # (not in "/")
            dialog.reject()
            return QtWidgets.QDialog.DialogCode.Rejected
        dialog.accept()
        return QtWidgets.QDialog.DialogCode.Accepted
    monkeypatch.setattr(QtWidgets.QDialog, "exec", run)
    for folder, make in (("/", False), (str(deep), True)):
        page.right.go_path(folder)
        settle(page.left, page.right)
        wanted["make"] = make
        page.right.table.setFocus()
        QTest.keyClick(page.right.table, QtCore.Qt.Key.Key_F7)
        found, width, metrics, title = seen[-1]
        assert title == tr("New folder") and found == [], "\n".join(found)
        assert width >= NAME_CHARS * metrics.averageCharWidth(), f"{folder}: the name field is {width} px wide"
        assert width >= metrics.horizontalAdvance(name), f"{folder}: the name ({metrics.horizontalAdvance(name)} px)"
    assert len(seen) == 2
    assert wait_until(QtWidgets.QApplication.instance(), lambda: (deep / name).is_dir(), 15), page.message.text()


def test_the_log_window_follows_the_rule_too(window, screens, qapp):  # noqa: F811
    """An extra window that is no dialog (here the log window) is at most 90% of the screen and placed by the rule
    when it is first shown; afterwards it stays where the human leaves it."""
    from sandbox_ai_app import logview
    screen = SCREENS["1280x800"]
    screens.use(screen)
    window_on(window, screen)
    window.activateWindow()
    qapp.processEvents()
    opener = qapp.activeWindow()  # (offscreen: the main window once it is active, else none - the screen then)
    log = logview.LogWindow()
    log.resize(1500, 900)
    log.show()
    qapp.processEvents()
    placed = outer(log)
    assert placed.width() <= screen.width() * 0.9 + 1 and placed.height() <= screen.height() * 0.9 + 1
    assert screen.contains(placed), (placed, screen)
    corner = (opener.frameGeometry() if opener is not None else screen).topLeft() + QtCore.QPoint(INSET, INSET)
    assert placed.topLeft() == corner, (placed, corner)  # (larger than 70% of the screen: at the top left)
    log.move(log.pos() + QtCore.QPoint(-20, 10))
    moved = log.pos()
    log.hide()
    log.show()
    qapp.processEvents()
    assert log.pos() == moved
    log.close()
    log.deleteLater()
