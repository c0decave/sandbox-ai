"""What the app's windows looked like when they were left, kept for the next start.

One file, ~/.config/sandbox-ai/app/state.json (next to settings.json; mode 0600, written atomically through
backend.write_private_json), read once at the start. A damaged or foreign file is moved aside and the app starts
with defaults (State.problem says why; the window shows it); a value of the wrong type falls back to its default.
Writes are collected for a moment (debounced) and done at once when the app quits.

Pages register what they want kept - a table's sort order and column widths, a splitter, a text filter, a check
box, a combo box - or keep their own small values with get()/set(). Secrets never come here: nothing registers a
password field, and agent arguments pass scrub_arguments() first.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import time

from PySide6.QtCore import QByteArray, QDir, QObject, Qt, QTimer
from PySide6.QtWidgets import QApplication, QFileDialog

from . import backend

LIMIT = 4 << 20  # a state file larger than this is not read
DELAY = 400  # ms: changes within this time are written together
_current = None  # the State of the running window (file dialogs remember their folders there)


def state_file():
    return backend.settings_file().with_name("state.json")


def current():
    return _current


# Words in an argument that say its value is a secret: the value is not kept (nor a --name=value of that name).
SECRET = re.compile(r"(?i)(key|token|secret|passw|credential|auth|bearer|cookie)")


def scrub_arguments(arguments):
    """ARGUMENTS without what may be a secret: the value after an option whose name says key, token,
    secret, password, … (and --name=VALUE of such a name), and anything that looks like a credential itself."""
    kept, skip = [], False
    for argument in arguments:
        argument = str(argument)
        if skip:
            skip = False
            continue
        name, equals, _ = argument.partition("=")
        if argument.startswith("-") and SECRET.search(name):
            if not equals:
                skip = True
            continue
        if looks_secret(argument):
            continue
        kept.append(argument)
    return kept


def looks_secret(value):
    """A token by its look: well-known prefixes, or a long run of letters and digits with no spaces."""
    value = str(value)
    if re.match(r"(sk-|ghp_|gho_|github_pat_|glpat-|xox[abp]-|AKIA|ya29\.|eyJ)", value):
        return True
    return bool(re.fullmatch(r"[A-Za-z0-9_\-+/=.]{32,}", value)) and bool(re.search(r"[0-9]", value)) \
        and bool(re.search(r"[A-Za-z]", value)) and "/" not in value[:1]


def dragging():
    """True while a mouse button is down (a column or splitter being dragged by the human)."""
    return bool(QApplication.mouseButtons() & Qt.MouseButton.LeftButton)


class State(QObject):
    def __init__(self, path=None, delay=DELAY, parent=None):
        super().__init__(parent)
        global _current
        self.path = Path(path) if path else state_file()
        self.data, self.problem = self.read()
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(delay)
        self.timer.timeout.connect(self.flush)
        self.collectors = []  # functions that put their values in before each write
        self.writes = 0
        _current = self

    def read(self):
        """(data, problem): {} and why, when the file is missing (no problem), damaged or not ours."""
        try:
            with open(self.path, "rb") as stream:
                raw = stream.read(LIMIT + 1)
        except FileNotFoundError:
            return {}, None
        except OSError as exc:
            return {}, f"{self.path}: {exc.strerror or exc}"
        try:
            if len(raw) > LIMIT:
                raise ValueError("larger than 4 MiB")
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("not a JSON object")
        except (ValueError, RecursionError, UnicodeDecodeError) as exc:
            aside = self.path.with_name(f"{self.path.name}.damaged-{time.strftime('%Y%m%d-%H%M%S')}")
            try:
                os.replace(self.path, aside)
            except OSError:
                aside = None
            return {}, f"{self.path}: {exc}" + (f" (kept as {aside.name})" if aside else "")
        return data, None

    # -- values --

    def get(self, key, default=None, kind=None):
        """The value of KEY, or DEFAULT when it is missing or not of KIND (default: the type of DEFAULT)."""
        value = self.data.get(key, default)
        kind = kind or (type(default) if default is not None else None)
        if kind is not None and not isinstance(value, kind) or isinstance(value, bool) and kind in (int, float):
            return default
        return value

    def set(self, key, value):
        if self.data.get(key) != value:
            self.data[key] = value
            self.schedule()

    def section(self, key):
        """A dict kept under KEY (created when missing or damaged), for pages with several values."""
        value = self.data.get(key)
        if not isinstance(value, dict):
            value = self.data[key] = {}
        return value

    def schedule(self):
        self.timer.start()

    def collect(self, function):
        """FUNCTION() runs before every write: for values that change too often to be set one by one."""
        self.collectors.append(function)

    def flush(self):
        """Write now (0600, atomic). A failure to write never stops the app."""
        self.timer.stop()
        for function in self.collectors:
            try:
                function()
            except Exception:  # noqa: BLE001 - a page that is gone: the rest is still written
                pass
        try:
            backend.write_private_json(self.path, self.data)
            self.writes += 1
        except (OSError, TypeError, ValueError):
            return False
        return True

    # -- widgets --

    def table(self, key, widget):
        """Keep the sort column and order and the widths the human gave the columns of WIDGET."""
        header = widget.horizontalHeader()
        saved = self.section("tables").get(key)
        if isinstance(saved, dict) and isinstance(saved.get("header"), str):
            header.restoreState(QByteArray.fromBase64(saved["header"].encode()))
            widget.user_sized = bool(saved.get("sized"))
            column, order = saved.get("sort"), saved.get("order")
            if isinstance(column, int) and 0 <= column < widget.columnCount() and order in (0, 1):
                widget.setSortingEnabled(True)
                widget.sortByColumn(column, Qt.SortOrder(order))

        def remember(sized=None):
            entry = {"header": bytes(header.saveState().toBase64()).decode(),
                     "sized": bool(sized or getattr(widget, "user_sized", False)),
                     "sort": header.sortIndicatorSection(), "order": header.sortIndicatorOrder().value}
            self.section("tables")[key] = entry
            self.schedule()

        def resized(*_):
            if dragging():
                widget.user_sized = True
                remember(True)
        header.sortIndicatorChanged.connect(lambda *_: remember())
        header.sectionResized.connect(resized)
        widget.remember_state = remember
        return widget

    def splitter(self, key, splitter):
        saved = self.section("splitters").get(key)
        if isinstance(saved, list) and len(saved) == splitter.count() and all(isinstance(s, int) and s >= 0 for s in saved):
            splitter.setSizes(saved)
        splitter.splitterMoved.connect(lambda *_: self.section("splitters").__setitem__(key, splitter.sizes())
                                       or self.schedule())
        return splitter

    def text(self, key, edit):
        """Keep the text of a line edit (a filter: never a password field)."""
        value = self.section("texts").get(key)
        if isinstance(value, str) and len(value) < 1000:
            edit.setText(value)
        edit.textChanged.connect(lambda text: self.section("texts").__setitem__(key, text[:1000]) or self.schedule())
        return edit

    def check(self, key, box):
        value = self.section("checks").get(key)
        if isinstance(value, bool):
            box.setChecked(value)
        box.toggled.connect(lambda on: self.section("checks").__setitem__(key, bool(on)) or self.schedule())
        return box

    # -- folders of file dialogs --

    def folder(self, key):
        value = self.section("folders").get(key)
        return value if isinstance(value, str) and os.path.isabs(value) and os.path.isdir(value) else ""

    def remember_folder(self, key, path):
        if path:
            folder = path if os.path.isdir(path) else os.path.dirname(path)
            self.section("folders")[key] = folder
            self.schedule()


def file_dialog(parent, title, start="", mode="folder", name_filter="", name=""):
    """A file dialog that lists hidden entries too (.claude, .config: QDir.Filter.Hidden; the platform's own dialog
    would ignore that filter, so Qt's own is used, with its "Show hidden files" in the context menu). MODE: folder,
    file, files or save (NAME the file offered). A typed path with a leading dot is taken as it is."""
    dialog = QFileDialog(parent, title, start or "")
    dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
    dialog.setFilter(dialog.filter() | QDir.Filter.Hidden)
    if mode == "folder":
        dialog.setFileMode(QFileDialog.FileMode.Directory)
        dialog.setOption(QFileDialog.Option.ShowDirsOnly, True)
    elif mode == "files":
        dialog.setFileMode(QFileDialog.FileMode.ExistingFiles)
    elif mode == "save":
        dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
        dialog.setFileMode(QFileDialog.FileMode.AnyFile)
        if name:
            dialog.selectFile(name)
    else:
        dialog.setFileMode(QFileDialog.FileMode.ExistingFile)
    if name_filter:
        dialog.setNameFilter(name_filter)
    return dialog


def run_dialog(dialog):
    """[path] the human chose in DIALOG ([]: cancelled). Tests replace this; with widgets.AUTO_ANSWER set no dialog
    is shown (it would wait for a human)."""
    from . import widgets
    try:
        if widgets.AUTO_ANSWER is not None:
            return []
        return list(dialog.selectedFiles()) if dialog.exec() == QFileDialog.DialogCode.Accepted else []
    finally:
        dialog.deleteLater()


def choose_path(parent, title, key, mode="folder", name_filter="", name=""):
    """[path] chosen with file_dialog, starting where the last one of KEY ended; the folder chosen is kept."""
    state = current()
    start = state.folder(key) if state else ""
    if mode == "save" and name:
        name = os.path.join(start, name) if start else name
    paths = [p for p in run_dialog(file_dialog(parent, title, start, mode, name_filter, name)) if p]
    if paths and state:
        state.remember_folder(key, paths[0])
    return paths


def choose_folder(parent, title, key="folder"):
    """A folder (hidden ones listed too), starting where the last one of KEY ended; the folder chosen is kept."""
    found = choose_path(parent, title, key, "folder")
    return found[0] if found else ""


def choose_file(parent, title, key="file", name_filter=""):
    found = choose_path(parent, title, key, "file", name_filter)
    return found[0] if found else ""


def choose_files(parent, title, key="files"):
    return choose_path(parent, title, key, "files")


def choose_save(parent, title, name, name_filter="", key="save"):
    """Where to write a file (NAME offered), hidden folders listed too; "" when cancelled."""
    found = choose_path(parent, title, key, "save", name_filter, name)
    return found[0] if found else ""
