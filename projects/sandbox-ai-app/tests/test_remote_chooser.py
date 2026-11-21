"""The one chooser of a path used on a host (remote_chooser.py): this machine's file dialog for this machine, and for a
host of `sandbox-ai hosts` a dialog that lists that host's folders over SSH and SFTP - against the fake SFTP server
(fake_sftp_server.py) as the host: its HOME, folders and files, links named and never followed, a path typed taken as
it is (also when the host cannot be reached), an unknown host key trusted only after a yes, a host that hangs never
holding up the window, and the folders chosen last kept per host."""
from pathlib import Path
import sys

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")

from sandbox_ai_app import remote, remote_chooser as RC, state, widgets  # noqa: E402

HERE = Path(__file__).resolve().parent
FAKE = [sys.executable, "-I", str(HERE / "fake_sftp_server.py")]
HOSTS = [{"name": "local", "kind": "local"}, {"name": "lab", "kind": "ssh", "ssh": "alice@lab.example", "port": 22}]


class Sai:
    """What the chooser asks of SandboxAI: the hosts of `sandbox-ai hosts`."""

    def host_entries(self):
        return [dict(h) for h in HOSTS]

    def hosts_supported(self):
        return True


def failing(text, script, tmp_path):
    """A fake ssh that writes TEXT to stderr and fails, until SCRIPT's condition holds (then it serves)."""
    path = tmp_path / "fake-ssh"
    path.write_text(f"#!/bin/sh\n{script}\necho '{text}' >&2\nexit 255\n")
    path.chmod(0o755)
    return [str(path)]


@pytest.fixture
def host(tmp_path, monkeypatch):
    """The fake host "lab": its HOME is tmp_path/remote (folders proj and other, a file, a link out of it); this
    machine's file dialog is never asked."""
    home = tmp_path / "remote"
    (home / "proj" / "src").mkdir(parents=True)
    (home / "other").mkdir()
    (home / "notes.txt").write_text("n")
    (home / "proj" / "Containerfile").write_text("FROM x")
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / "escape").symlink_to(outside)
    monkeypatch.setenv("FAKE_SFTP_HOME", str(home))
    monkeypatch.setattr(RC, "SSH", FAKE)
    monkeypatch.setattr(RC, "KNOWN_HOSTS", str(tmp_path / "known_hosts"))
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])

    def local_dialog(dialog):
        raise AssertionError("this machine's file dialog was opened for a path on another host")
    monkeypatch.setattr(state, "run_dialog", local_dialog)
    return home.resolve()


def driven(monkeypatch, wait, answer, listed=True):
    """RC.run_dialog as a human answers: once the dialog listed a folder (LISTED), ANSWER(dialog) - it selects or
    types - and the path chosen is what the dialog gives. The dialogs seen: driven.seen."""
    seen = []

    def run(dialog):
        seen.append(dialog)
        try:
            if listed:
                assert wait(lambda: dialog.folder and dialog.go_button.isEnabled(), 20), dialog.status.text()
            return answer(dialog)
        finally:
            dialog.disconnect_root()
    monkeypatch.setattr(RC, "run_dialog", run)
    return seen


def rows(dialog):
    return [dialog.list.item(r).text() for r in range(dialog.list.count())]


def select(dialog, text):
    item = next(dialog.list.item(r) for r in range(dialog.list.count()) if dialog.list.item(r).text() == text)
    dialog.list.setCurrentItem(item)
    return item


def settled(dialog, wait):
    assert wait(lambda: dialog.go_button.isEnabled(), 20), dialog.status.text()


def test_a_folder_on_a_host_is_chosen_from_its_folders_starting_in_its_home(host, monkeypatch, wait):
    def answer(dialog):
        assert dialog.folder == str(host) and dialog.path.text() == str(host), "the remote HOME first"
        assert sorted(rows(dialog)) == sorted(["escape → " + str(host.parent / "outside") + " (a link: not followed)",
                                               "other/", "proj/"]), "folders only (and links, named)"
        select(dialog, "proj/")
        return dialog.chosen()
    seen = driven(monkeypatch, wait, answer)
    assert RC.choose(None, "lab", "Project folder", sai=Sai()) == str(host / "proj")
    assert seen[0].windowTitle() == "Project folder on lab"


def test_links_are_named_and_never_followed(host, monkeypatch, wait):
    def answer(dialog):
        link = next(text for text in rows(dialog) if text.startswith("escape → "))
        assert "(a link: not followed)" in link
        dialog.open_item(select(dialog, link))
        assert "is a symbolic link to" in dialog.status.text() and dialog.folder == str(host), "not followed"
        assert dialog.chosen() == str(host), "a link is no folder to choose: the folder shown is"
        dialog.go(str(host / "escape"))  # typed: remote.py walks with LSTAT and refuses the link
        settled(dialog, wait)
        assert "not followed" in dialog.status.text() and dialog.folder == str(host)
        return ""
    driven(monkeypatch, wait, answer)
    assert RC.choose(None, "lab", "Folder", sai=Sai()) == ""


def test_a_file_on_a_host_starts_at_the_fields_value_and_lists_files(host, monkeypatch, wait):
    def answer(dialog):
        assert dialog.folder == str(host / "proj"), "the folder of the field's value"
        assert rows(dialog) == ["src/", "Containerfile"]
        dialog.open_item(select(dialog, "src/"))
        settled(dialog, wait)
        assert dialog.folder == str(host / "proj" / "src") and rows(dialog) == []
        assert dialog.chosen() == "", "a folder is no file"
        dialog.up()
        settled(dialog, wait)
        select(dialog, "Containerfile")
        return dialog.chosen()
    driven(monkeypatch, wait, answer)
    assert RC.choose(None, "lab", "Containerfile", "file", start=str(host / "proj" / "Containerfile"),
                     sai=Sai()) == str(host / "proj" / "Containerfile")


def test_an_unreachable_host_says_so_and_a_typed_path_is_taken_as_it_is(host, tmp_path, monkeypatch, wait):
    monkeypatch.setattr(RC, "SSH", failing("ssh: connect to host lab.example port 22: Connection refused", "",
                                           tmp_path))

    def answer(dialog):
        assert wait(lambda: "type the path" in dialog.status.text(), 20), dialog.status.text()
        assert "Connection refused" in dialog.status.text() or "lab" in dialog.status.text()
        dialog.path.setText("/srv/projects/app")
        return dialog.chosen()
    driven(monkeypatch, wait, answer, listed=False)
    assert RC.choose(None, "lab", "Project folder", sai=Sai()) == "/srv/projects/app"


def test_a_host_that_is_not_reached_over_ssh_is_said_and_typing_still_works(host, monkeypatch, wait):
    def answer(dialog):
        assert "no host of sandbox-ai reached over SSH" in dialog.status.text()
        dialog.path.setText("relative/path")
        assert dialog.chosen() == "", "an absolute path only"
        dialog.path.setText("/data/x")
        return dialog.chosen()
    driven(monkeypatch, wait, answer, listed=False)
    assert RC.choose(None, "gone", "Folder", sai=Sai()) == "/data/x"


def test_an_unknown_host_key_is_trusted_only_after_a_yes(host, tmp_path, monkeypatch, wait):
    known = tmp_path / "known_hosts"
    serve = " ".join(FAKE)
    monkeypatch.setattr(RC, "SSH", failing("Host key verification failed.",
                                           f"grep -q lab '{known}' 2>/dev/null && exec {serve}", tmp_path))
    monkeypatch.setattr(remote, "scan_host", lambda target: ["lab.example ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA"])
    monkeypatch.setattr(remote, "fingerprints", lambda lines: ["ED25519 SHA256:abcdef"])
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)

    def declined(dialog):
        assert wait(lambda: widgets.SHOWN, 20)
        title, text, _ = widgets.SHOWN[0]
        assert title == "Trust this host?" and "ED25519 SHA256:abcdef" in text
        assert wait(lambda: "not trusted" in dialog.status.text(), 5)
        return ""
    driven(monkeypatch, wait, declined, listed=False)
    assert RC.choose(None, "lab", "Folder", sai=Sai()) == "" and not known.exists()
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    driven(monkeypatch, wait, lambda dialog: dialog.chosen())
    assert RC.choose(None, "lab", "Folder", sai=Sai()) == str(host)
    assert known.read_text() == "lab.example ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA\n"


def test_a_host_that_hangs_never_holds_up_the_window(host, monkeypatch, wait):
    monkeypatch.setenv("FAKE_SFTP_HANG", "READDIR")
    monkeypatch.setattr(RC, "LIST_WAIT", 1)

    def answer(dialog):
        assert wait(lambda: "did not answer" in dialog.status.text(), 20), dialog.status.text()
        assert dialog.go_button.isEnabled() and dialog.list.count() == 0
        dialog.path.setText("/srv/typed")
        return dialog.chosen()
    driven(monkeypatch, wait, answer, listed=False)
    assert RC.choose(None, "lab", "Folder", sai=Sai()) == "/srv/typed"


def test_this_machine_gets_its_own_file_dialog(monkeypatch):
    asked = []
    monkeypatch.setattr(state, "run_dialog", lambda dialog: asked.append(dialog.fileMode()) or ["/here/x"])
    monkeypatch.setattr(RC, "run_dialog", lambda dialog: pytest.fail("the remote chooser for this machine"))
    assert RC.choose(None, "local", "Folder") == "/here/x" and RC.choose(None, None, "File", "file") == "/here/x"
    assert asked == [QtWidgets.QFileDialog.FileMode.Directory, QtWidgets.QFileDialog.FileMode.ExistingFile]


def test_the_folders_chosen_last_are_kept_per_host_and_offered(host, tmp_path, monkeypatch, wait):
    kept = state.State(tmp_path / "state.json")
    try:
        for name in ("proj/", "other/"):
            driven(monkeypatch, wait, lambda dialog, name=name: select(dialog, name) and dialog.chosen())
            RC.choose(None, "lab", "Folder", sai=Sai())
        assert RC.recent_folders("lab") == [str(host / "other"), str(host / "proj")]
        assert RC.recent_folders("local") == [] and RC.recent_folders("elsewhere") == []

        def answer(dialog):
            assert dialog.recent.isVisibleTo(dialog)
            assert [dialog.recent.itemData(i) for i in range(1, dialog.recent.count())] == \
                [str(host / "other"), str(host / "proj")]
            dialog.recent.activated.emit(2)
            assert wait(lambda: dialog.folder == str(host / "proj"), 20)
            return dialog.chosen()
        driven(monkeypatch, wait, answer)
        assert RC.choose(None, "lab", "Folder", sai=Sai()) == str(host / "proj")
        assert RC.recent_folders("lab")[0] == str(host / "proj")
    finally:
        kept.deleteLater()
        monkeypatch.setattr(state, "_current", None)


def test_the_lists_actions_are_on_its_context_menu(host, monkeypatch, wait):
    def answer(dialog):
        select(dialog, "proj/")
        entries = [text for text, _enabled in widgets.menu_entries(widgets.menu_of(dialog.list))]
        assert entries == ["Open", "Choose", "Up", "Home", "Refresh", "New folder…"]
        return ""
    driven(monkeypatch, wait, answer)
    RC.choose(None, "lab", "Folder", sai=Sai())


# -- New folder… (Ctrl+Shift+N, the button, the list's menu): made over SFTP in the folder listed, then selected --

def named(monkeypatch, name, seen=None):
    """RC.run_name_dialog as a human answers: NAME typed (None: cancelled); the dialogs asked: SEEN."""
    def run(dialog):
        if seen is not None:
            seen.append((dialog.name.text(), dialog.name.selectedText(), dialog.problem.text(),
                         dialog.ok_button.isEnabled()))
        if name is None:
            return ""
        dialog.name.setText(name)
        return dialog.name.text() if dialog.ok_button.isEnabled() else ""
    monkeypatch.setattr(RC, "run_name_dialog", run)


def shortcut(dialog, keys):
    from PySide6.QtGui import QKeySequence, QShortcut
    return next(s for s in dialog.findChildren(QShortcut) if s.key() == QKeySequence(keys))


def listing_of(home):
    return sorted(p.name for p in home.iterdir())


def test_a_new_folder_is_made_in_the_folder_listed_selected_and_taken_by_choose(host, monkeypatch, wait):
    asked = []
    named(monkeypatch, "build", asked)

    def answer(dialog):
        assert dialog.new_folder_button.isEnabled()
        shortcut(dialog, "Ctrl+Shift+N").activated.emit()
        assert asked == [("New folder", "New folder", "", True)], "prefilled, selected, nothing wrong with it"
        assert wait(lambda: dialog.current() is not None and dialog.current().text() == "build/", 20), \
            dialog.status.text()
        assert (host / "build").is_dir(), "made on the host, in the folder listed"
        assert "build" in dialog.status.text() and "created" in dialog.status.text()
        dialog.take()  # Choose (OK)
        return dialog.chosen()
    driven(monkeypatch, wait, answer)
    assert RC.choose(None, "lab", "Project folder", sai=Sai()) == str(host / "build")


def test_new_folder_from_the_lists_menu_and_cancelled_makes_nothing(host, monkeypatch, wait):
    def answer(dialog):
        named(monkeypatch, None)
        menu = widgets.menu_of(dialog.list)
        entry = next(a for a in menu.actions() if a.text() == "New folder…")
        assert entry.isEnabled()
        entry.trigger()
        assert listing_of(host) == ["escape", "notes.txt", "other", "proj"], "cancelled: nothing made"
        named(monkeypatch, "from-menu")
        dialog.open_item(select(dialog, "proj/"))
        settled(dialog, wait)
        next(a for a in widgets.menu_of(dialog.list).actions() if a.text() == "New folder…").trigger()
        assert wait(lambda: (host / "proj" / "from-menu").is_dir(), 20), dialog.status.text()
        assert wait(lambda: dialog.current() is not None and dialog.current().text() == "from-menu/", 20)
        return ""
    driven(monkeypatch, wait, answer)
    RC.choose(None, "lab", "Folder", sai=Sai())


@pytest.mark.parametrize("name, said", [
    ("", "Type a name"), ("a/b", "no '/'"), ("/etc/x", "no '/'"), (".", "not names of a folder"),
    ("..", "not names of a folder"), ("a\0b", "control characters"), ("a\nb", "control characters"),
    ("a\tb", "control characters"), ("a\x1bb", "control characters"), ("a\x7fb", "control characters"),
    ("a\x85b", "control characters"), ("x" * 256, "too long"), ("ä" * 128, "too long"),
    ("proj", "already exists"), ("notes.txt", "already exists")])
def test_a_folder_name_is_refused_with_what_is_wrong(name, said):
    assert said in RC.folder_name_problem(name, {"proj", "notes.txt"})


def test_a_good_folder_name_passes():
    for name in ("build", "New folder", "ä" * 127, "x" * 255, ".hidden", "a b", "<b>x<b>", "-n", "$(x)"):
        assert RC.folder_name_problem(name, {"proj"}) == "", name


def test_every_refusal_is_said_in_the_status_line_and_nothing_is_sent(host, tmp_path, monkeypatch, wait):
    log = tmp_path / "sftp.log"
    monkeypatch.setenv("FAKE_SFTP_LOG", str(log))

    def answer(dialog):
        for name, said in (("a/b", "no '/'"), ("..", "not names of a folder"), ("a\nb", "control characters"),
                           ("x" * 256, "too long"), ("proj", "already exists"), ("notes.txt", "already exists")):
            assert dialog.make_folder(name) is False
            assert said in dialog.status.text(), (name, dialog.status.text())
            assert dialog.status.property("role") == "notice-warn"
        assert "\\x0a" in (dialog.make_folder("a\nb") or dialog.status.text()), "the name shown escaped"
        assert "MKDIR" not in log.read_text(), "nothing asked of the host"
        assert listing_of(host) == ["escape", "notes.txt", "other", "proj"]
        return ""
    driven(monkeypatch, wait, answer)
    RC.choose(None, "lab", "Folder", sai=Sai())


def test_the_name_dialog_says_what_is_wrong_and_offers_a_free_name(host, monkeypatch, wait):
    (host / "New folder").mkdir()

    def answer(dialog):
        names = RC.FolderNameDialog(dialog, dialog.folder, "lab", dialog.names)
        try:
            assert names.name.text() == "New folder 2" and names.name.selectedText() == "New folder 2", \
                "a name not taken yet, selected"
            assert names.ok_button.isEnabled() and not names.problem.text()
            names.name.setText("proj")
            assert "already exists" in names.problem.text() and not names.ok_button.isEnabled()
            names.name.setText("a/b")
            assert "no '/'" in names.problem.text() and not names.ok_button.isEnabled()
            names.name.setText("fine")
            assert names.ok_button.isEnabled() and not names.problem.text()
            assert any(str(host) in label.text() for label in names.findChildren(QtWidgets.QLabel)), \
                "says where it is made"
        finally:
            names.deleteLater()
        return ""
    driven(monkeypatch, wait, answer)
    RC.choose(None, "lab", "Folder", sai=Sai())


def test_a_name_that_exists_by_now_is_said_by_the_host(host, monkeypatch, wait):
    def answer(dialog):
        (host / "late").mkdir()  # made on the host after the folder was listed
        assert dialog.make_folder("late") is True
        assert wait(lambda: "already exists" in dialog.status.text(), 20), dialog.status.text()
        assert dialog.go_button.isEnabled() and dialog.new_folder_button.isEnabled()
        return ""
    driven(monkeypatch, wait, answer)
    RC.choose(None, "lab", "Folder", sai=Sai())


def test_an_error_of_the_host_is_said_and_nothing_hangs(host, monkeypatch, wait):
    monkeypatch.setenv("FAKE_SFTP_NO_MKDIR", "1")

    def answer(dialog):
        assert dialog.make_folder("build") is True
        assert wait(lambda: "Permission denied" in dialog.status.text(), 20), dialog.status.text()
        assert dialog.status.property("role") == "notice-warn"
        assert dialog.go_button.isEnabled() and dialog.new_folder_button.isEnabled()
        assert not (host / "build").exists() and "proj/" in rows(dialog), "the listing as it was"
        return ""
    driven(monkeypatch, wait, answer)
    RC.choose(None, "lab", "Folder", sai=Sai())


def test_a_host_that_does_not_answer_a_new_folder_never_holds_up_the_window(host, monkeypatch, wait):
    import time
    monkeypatch.setenv("FAKE_SFTP_HANG", "MKDIR")
    monkeypatch.setattr(RC, "MKDIR_WAIT", 1)

    def answer(dialog):
        started = time.monotonic()
        assert dialog.make_folder("build") is True
        assert time.monotonic() - started < 0.5, "the GUI thread never waits for the host"
        assert not dialog.new_folder_button.isEnabled(), "one at a time"
        assert wait(lambda: "did not answer" in dialog.status.text(), 20), dialog.status.text()
        assert dialog.go_button.isEnabled() and dialog.new_folder_button.isEnabled()
        return ""
    driven(monkeypatch, wait, answer)
    RC.choose(None, "lab", "Folder", sai=Sai())


def test_new_folder_before_a_folder_is_listed_is_refused(host, tmp_path, monkeypatch, wait):
    monkeypatch.setattr(RC, "SSH", failing("ssh: connect to host lab.example port 22: Connection refused", "",
                                           tmp_path))

    def answer(dialog):
        assert wait(lambda: "type the path" in dialog.status.text(), 20), dialog.status.text()
        assert not dialog.new_folder_button.isEnabled()
        assert dialog.make_folder("build") is False and "not connected" in dialog.status.text().lower()
        return ""
    driven(monkeypatch, wait, answer, listed=False)
    RC.choose(None, "lab", "Folder", sai=Sai())


# -- the bar's buttons: icons, their function in the tooltip, their text the accessible name --

def test_the_bars_buttons_are_icons_with_tooltips_and_accessible_names(host, monkeypatch, wait):
    from PySide6.QtCore import Qt

    def answer(dialog):
        expected = {"go_button": ("Go", "go", "Enter"), "up_button": ("Up", "up", "Alt+Up"),
                    "home_button": ("Home", "home", "Alt+Home"), "refresh_button": ("Refresh", "refresh", "Ctrl+R"),
                    "new_folder_button": ("New folder…", "folder-new", "Ctrl+Shift+N")}
        for attribute, (name, glyph, keys) in expected.items():
            widget = getattr(dialog, attribute)
            assert widget.text() == "" and not widget.icon().isNull(), name
            assert widget.accessibleName() == name and widget.glyph == glyph
            assert widget.toolTip() and keys in widget.toolTip(), (name, widget.toolTip())
            assert widget.focusPolicy() & Qt.FocusPolicy.TabFocus, "Tab reaches it"
            assert widget.isVisibleTo(dialog)
        for keys in ("Alt+Up", "Alt+Home", "Ctrl+R", "Ctrl+Shift+N"):
            assert shortcut(dialog, keys).isEnabled(), keys
        assert dialog.choose_button.text() == "Choose", "OK and Cancel stay words"
        assert dialog.buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Cancel).text()
        return ""
    driven(monkeypatch, wait, answer)
    RC.choose(None, "lab", "Folder", sai=Sai())


def tint_of(widget):
    """The colour the icon of WIDGET is drawn in (its most opaque pixel)."""
    image = widget.icon().pixmap(16, 16).toImage()
    best = max((image.pixelColor(x, y) for x in range(image.width()) for y in range(image.height())),
               key=lambda c: c.alpha())
    return best.name()


def test_the_icons_follow_the_look(host, monkeypatch, wait, qapp):
    from PySide6.QtGui import QColor
    from sandbox_ai_app import theme

    def answer(dialog):
        try:
            for mode in ("light", "dark", "hacker", "hacker_neon", "win311"):
                theme.apply(mode, qapp)
                qapp.processEvents()
                text = QColor(theme.colors()["text"]).name()
                for widget in (dialog.go_button, dialog.up_button, dialog.home_button, dialog.refresh_button,
                               dialog.new_folder_button):
                    assert tint_of(widget) == text, (mode, widget.accessibleName(), tint_of(widget), text)
        finally:
            theme.apply("system", qapp)
        return ""
    driven(monkeypatch, wait, answer)
    RC.choose(None, "lab", "Folder", sai=Sai())


def test_the_files_page_and_the_chooser_share_one_icon_button():
    from sandbox_ai_app import files_page
    assert files_page.nav_button is widgets.nav_button and not hasattr(files_page, "paint_glyph")
    assert RC.nav_button is widgets.nav_button


def test_this_machines_folder_dialog_offers_new_folder(qapp):
    dialog = state.file_dialog(None, "Folder", str(HERE), "folder")
    try:
        assert not dialog.testOption(QtWidgets.QFileDialog.Option.ReadOnly)
        new = dialog.findChild(QtWidgets.QToolButton, "newFolderButton")
        assert new is not None and new.isEnabled() and new.toolTip()
    finally:
        dialog.deleteLater()
