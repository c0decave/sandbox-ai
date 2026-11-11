"""Copy to host… and Move to host… of the Sandboxes page: a session of this machine started again on a host of
`sandbox-ai hosts` (`sandbox-ai session copy|move NAME HOST [--as NAME] [--map=LOCAL=REMOTE]... [--check]`).

The dialog offers the hosts (this machine left out), the session's name there and every folder or file of this machine
the session's options name, each where it lies there (the same path unless changed: only a changed one becomes
--map). Check only runs sandbox-ai's dry run on that host (the folders there, the MCP servers it knows, its login of the
agent, a session of that name there already); Copy (Move) checks and starts it there - Move then stops the session here,
asked first (session-move). A session of that name there is refused, or replaced when chosen (--replace, asked first:
session-replace). The command runs in the background; what it says (the problems, the dry run's complaints and
warnings) is shown in the dialog. Logins and the session's HOME are not copied."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QLineEdit, QPlainTextEdit, QPushButton, QScrollArea, QVBoxLayout,
    QWidget,
)

from . import backend
from .files import printable
from .host_choice import HostChooser, follow_hosts
from .i18n import tr
from .name_fields import alive
from .widgets import ask_first, background, output_text, outcome, plain_label, safe_tooltip, set_enabled

OUTPUT_LINES = 400  # lines of the command's output the dialog keeps (the last ones)


class SessionTransferDialog(QDialog):
    """Session NAME of this machine to a host: copied (MOVE False) or moved. OWNER (the page) counts the command as
    its work; SUCCEEDED(host) runs after a copy or move that succeeded (the page reads its lists again)."""

    def __init__(self, parent, sai, name, move=False, owner=None, succeeded=None):
        super().__init__(parent)
        # (moving and done_on: never move or done, which would hide QWidget.move and QDialog.done - reject() calls
        # done() through Python, and Close did nothing: "'bool' object is not callable")
        self.sai, self.name, self.moving = sai, name, move
        self.owner, self.succeeded = owner, succeeded
        self.paths, self.edits, self.problems, self.running, self.done_on = [], {}, [], False, None
        self.setWindowTitle(tr("Move session {name} to a host", name=printable(name)) if move else
                            tr("Copy session {name} to a host", name=printable(name)))
        layout = QVBoxLayout(self)
        layout.addWidget(plain_label(
            tr("Its configuration starts again there as a session of its own, checked there first; then it is "
               "stopped here (its HOME stays until it is deleted).") if move else
            tr("Its configuration starts again there as a session of its own, checked there first; this one stays "
               "as it is.")))
        layout.addWidget(plain_label(tr("Logins and the session's HOME are not copied: the host uses its own."),
                                     role="secondary"))
        self.host = HostChooser(sai, tr("The host it starts on (sandbox-ai hosts; this machine is not offered)"),
                                local=False)
        self.host.changed.connect(lambda _host: self.update_buttons())
        layout.addWidget(self.host)
        self.no_host = plain_label(tr("No host yet: add one on the Hosts page (sandbox-ai hosts add)."),
                                   role="secondary")
        layout.addWidget(self.no_host)
        form = QFormLayout()
        self.new_name = QLineEdit(name)
        self.new_name.setAccessibleName(tr("Name there"))
        self.new_name.setToolTip(safe_tooltip(tr("The session's name on the host (--as; the same unless changed)")))
        self.new_name.setMaxLength(40)
        form.addRow(tr("Name there"), self.new_name)
        layout.addLayout(form)
        self.replace = QCheckBox(tr("Replace a session of that name there"))
        self.replace.setToolTip(safe_tooltip(tr(
            "A session of that name on the host is deleted first, its HOME there too (--replace). Without, the copy "
            "is refused when there is one: started again, it would keep its own options, not these.")))
        layout.addWidget(self.replace)
        self.replace.setVisible(_replace_supported(sai))  # (in the dialog first: no window of its own for a moment)
        layout.addWidget(plain_label(tr("Folders and files of the session: where each one lies on the host. One that "
                                        "lies elsewhere there is changed here (--map); the others stay the same.")))
        self.folders = QWidget()
        self.folders_form = QFormLayout(self.folders)
        self.folders_form.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.folders)
        scroll.setMaximumHeight(220)
        scroll.setAccessibleName(tr("Folders of the session"))
        layout.addWidget(scroll)
        self.status = plain_label(tr("Reading the session's folders…"), role="secondary")
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.status)
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setAccessibleName(tr("What sandbox-ai said"))
        self.output.setMaximumBlockCount(OUTPUT_LINES)
        self.output.setMinimumHeight(110)
        self.output.hide()
        layout.addWidget(self.output, 1)
        buttons = QDialogButtonBox()
        self.check_button = QPushButton(tr("Check only"))
        self.check_button.setAccessibleName(tr("Check only"))
        self.check_button.setToolTip(safe_tooltip(tr("Only check that it can start on the host as it is: a dry run "
                                                     "there, nothing starts (--check)")))
        self.check_button.clicked.connect(lambda _=False: self.run(check=True))
        self.go_button = QPushButton(tr("Move") if move else tr("Copy"))
        self.go_button.setAccessibleName(tr("Move to the host") if move else tr("Copy to the host"))
        self.go_button.setToolTip(safe_tooltip(
            tr("Check it on the host, start it there, then stop it here ({command})", command="sandbox-ai session move")
            if move else tr("Check it on the host and start it there ({command})", command="sandbox-ai session copy")))
        if move:
            self.go_button.setProperty("role", "danger")
        self.go_button.clicked.connect(lambda _=False: self.run(check=False))
        self.close_button = QPushButton(tr("Close"))
        self.close_button.clicked.connect(lambda _=False: self.reject())
        buttons.addButton(self.check_button, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton(self.go_button, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton(self.close_button, QDialogButtonBox.ButtonRole.RejectRole)
        layout.addWidget(buttons)
        self.resize(640, 480)
        follow_hosts(self, self.update_buttons)  # a host added or removed meanwhile: the buttons follow
        self.update_buttons()
        background(self.sai.session_transfer_plan, name, timeout=60,
                   done=lambda result, error: alive(self) and self.plan_read(result, error))

    # -- the session's folders --

    def plan_read(self, result, error):
        if error is not None:
            self.status.setText(tr("The session's folders could not be read: {error}",
                                   error=printable(error)[:400]))
            return
        paths, self.problems = result
        self.paths = [path for path in paths if isinstance(path, str)]
        for path in self.paths:
            edit = QLineEdit(path)
            edit.setAccessibleName(tr("{path} on the host", path=printable(path)))
            edit.setToolTip(safe_tooltip(tr("Where {path} lies on the host (the same unless changed)",
                                            path=printable(path))))
            self.folders_form.addRow(printable(path), edit)
            self.edits[path] = edit
        lines = [tr("No folder or file of this machine is named.") if not self.paths else
                 tr("Folders and files: {count}", count=len(self.paths))]
        lines += [tr("Cannot go along: {problem}", problem=printable(problem)[:400]) for problem in self.problems]
        self.status.setText("\n".join(lines))

    def maps(self):
        """[(local, remote)] of the folders whose place on the host was changed."""
        found = []
        for local, edit in self.edits.items():
            remote = edit.text().strip()
            if remote and remote.rstrip("/") != local.rstrip("/"):
                found.append((local, remote))
        return found

    # -- the command --

    def update_buttons(self):
        if not alive(self):
            return
        host = self.host.host()
        self.no_host.setVisible(host is None)
        why = (tr("A command runs.") if self.running else
               tr("Choose a host first.") if host is None else None)
        set_enabled(self.check_button, why is None, why)
        # Done there (or moved: it no longer runs here): not a second time; a copy may go to another host still.
        go_why = why or (tr("Done: it runs on {host}.", host=printable(self.done_on))
                         if self.done_on and (self.moving or self.done_on == host) else None)
        set_enabled(self.go_button, go_why is None, go_why)

    def run(self, check=False):
        """Check only (CHECK) or Copy/Move: `sandbox-ai session copy|move ...` in the background; a move is asked
        first (session-move)."""
        host = self.host.host()
        if host is None or self.running:
            return
        new_name = self.new_name.text().strip() or self.name
        command = "move" if self.moving else "copy"
        maps = self.maps()
        replace = not self.replace.isHidden() and self.replace.isChecked()
        try:
            backend.session_transfer_args(command, self.name, host, new_name, maps, check, replace)
        except ValueError as exc:
            self.show_output(tr("Not started: {error}", error=printable(exc)))
            return
        if self.moving and not check and not ask_first(
                self, "session-move", tr("Move to host"),
                tr("Start session {name} on {host} as {new}, then stop it here? Its HOME stays here until it is "
                   "deleted; logins and HOME are not copied.", name=printable(self.name), host=printable(host),
                   new=printable(new_name)), tr("Move")):
            return
        if not check and not ask_first(
                self, "session-replace", tr("Replace on host"),
                tr("Delete the session {new} on {host} first, its HOME there too, if there is one?",
                   new=printable(new_name), host=printable(host)), tr("Replace"), detail={"replace": replace}):
            return
        function = self.sai.session_move if self.moving else self.sai.session_copy
        self.running = True
        self.status.setText(tr("Checking on {host}…", host=printable(host)) if check else
                            tr("Checking and starting on {host}… (this can take minutes)", host=printable(host)))
        self.update_buttons()
        # --replace only when chosen: a sandbox-ai without it is called as before
        args = (self.name, host, new_name, maps, check) + ((True,) if replace else ())
        background(function, *args, owner=self.owner, work=True,
                   source=[self.check_button, self.go_button], timeout=backend.TRANSFER_SECONDS + 60,
                   done=lambda result, error: self.ran(host, check, result, error))

    def ran(self, host, check, result, error):
        status, out, err = outcome(result, error)
        if status == 0 and not check and self.succeeded is not None:
            self.succeeded(host)  # the lists follow, also when the dialog was closed meanwhile
        if not alive(self):
            return
        self.running = False
        if status == 0 and not check:
            self.done_on = host
        said = (out + err).strip()
        self.show_output(said or (tr("Done.") if status == 0 else tr("It failed (exit {status}).", status=status)))
        warned = "warning:" in said
        self.status.setText(
            (tr("It can start on {host}, with warnings: see below.", host=printable(host)) if warned else
             tr("It can start on {host}.", host=printable(host))) if status == 0 and check else
            (tr("Done: it runs on {host}, with warnings: see below.", host=printable(host)) if warned else
             tr("Done: it runs on {host}.", host=printable(host))) if status == 0 else
            tr("It failed (exit {status}): see what sandbox-ai said below.", status=status))
        self.update_buttons()

    def show_output(self, text):
        self.output.show()
        self.output.setPlainText(output_text(text))


def _replace_supported(sai):
    """Whether this sandbox-ai's session copy|move takes --replace (an older one is called without it)."""
    try:
        return bool(sai.session_replace_supported())
    except Exception:  # noqa: BLE001 - not known: not offered
        return False
