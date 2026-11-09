"""Fields that take the NAME of a stored secret (sandbox-ai secrets) or of a VPN profile (sandbox-ai vpn): an editable
combo box that offers the names the chosen host has, a name typed that it lacks still taken (the human decides; a
secret named before it is kept can be added afterwards).

The names are read in the background, never on the GUI thread: a host's secrets with `sandbox-ai secrets list` there
(its table: names and where each is kept - a value is never read, asked for or shown), its VPN profiles with `vpn list
--json` (an older host without it: the field stays typed in, with why in its tooltip). One read per kind and host is
shared by every field that waits for it and kept for a short while; a page that listed them anyway (the Credentials
page's secrets, the VPN box) hands its list on (publish), and every field of that kind and host takes it at once. An
answer for a host a field no longer shows is dropped."""
from __future__ import annotations

import re
import time
import weakref

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QComboBox, QCompleter, QMenu, QToolButton

from .files import printable
from .i18n import tr
from .widgets import background, safe_tooltip

LOCAL = "local"
KINDS = ("secret", "vpn")
KEEP_SECONDS = 30.0  # a list read this recently is offered again without asking the host again
READ_SECONDS = 120  # a host that does not answer within this: the field stays typed in


# What a name can be (secret_store.NAME; vpn.py's profile names, which llm and mcp take too): nothing else is offered.
NAMES = {"secret": re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}"), "vpn": re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}")}


def valid(kind, names):
    return sorted({name for name in names if isinstance(name, str) and NAMES[kind].fullmatch(name)})


def read_names(sai, kind, host):
    """[name] of HOST's secrets (their names only: `secrets list` there) or VPN profiles (`vpn list --json`), sorted.
    ValueError (or another error): not answered."""
    if kind == "secret":
        return valid(kind, [name for name, _ in sai.host_secrets(host)[1]])
    if kind == "vpn":
        return valid(kind, [entry.get("profile") for entry in sai.host_vpn_profiles(host) if isinstance(entry, dict)])
    raise ValueError(f"unknown kind {kind}")


class Hub(QObject):
    """changed(kind, host): the names of that kind on that host changed (or were read again): the fields read them."""
    changed = Signal(str, str)


_hub = []
_kept = {}  # (id(sai), kind, host) -> (weak reference to sai, generation, time read, names or None, problem or None)
_waiting = {}  # (id(sai), kind, host, generation) -> [done(names, problem)]
_generation = {}  # (kind, host) -> a number that grows with each change: a read begun before it is not kept


def hub():
    if not _hub:
        _hub.append(Hub())
    return _hub[0]


def forget(kind, host=LOCAL):
    """A secret or VPN profile was added or removed on HOST (and nobody listed them since): every field of KIND there
    reads them again."""
    _generation[(kind, host)] = _generation.get((kind, host), 0) + 1
    for key in [key for key in _kept if key[1:] == (kind, host)]:
        del _kept[key]
    hub().changed.emit(kind, host)


def publish(sai, kind, host, names):
    """NAMES are what HOST has of KIND now (a page listed them): every field of that kind there offers them."""
    _generation[(kind, host)] = _generation.get((kind, host), 0) + 1
    _kept[(id(sai), kind, host)] = (_weak(sai), _generation[(kind, host)], time.monotonic(),
                                    valid(kind, names), None)
    hub().changed.emit(kind, host)


def names_of(sai, kind, host, done):
    """DONE(names, problem) on the GUI thread: HOST's names of KIND (kept ones if read just now), or None and why not."""
    key = (id(sai), kind, host)
    generation = _generation.get((kind, host), 0)
    kept = _kept.get(key)
    if kept is not None and kept[0]() is sai and kept[1] == generation and time.monotonic() - kept[2] < KEEP_SECONDS:
        done(kept[3], kept[4])
        return
    waiting = key + (generation,)
    if waiting in _waiting:  # read already: this one waits for the same answer
        _waiting[waiting].append(done)
        return
    _waiting[waiting] = [done]

    def finished(result, error):
        names, problem = (result, None) if error is None else (None, printable(str(error))[-200:] or "?")
        if error is None and _generation.get((kind, host), 0) == generation:
            _kept[key] = (_weak(sai), generation, time.monotonic(), names, None)
        for callback in _waiting.pop(waiting, []):
            callback(names, problem)
    background(read_names, sai, kind, host, timeout=READ_SECONDS, done=finished)


def _weak(sai):
    """A weak reference to SAI (the backend that went must not be kept alive by what it read; a stand-in of the tests
    that takes none: itself, as a callable that returns it)."""
    try:
        return weakref.ref(sai)
    except TypeError:
        return lambda: sai


def where(host):
    return tr("this computer") if host == LOCAL else printable(host)


def alive(widget):
    try:
        from shiboken6 import isValid
        return isValid(widget)
    except ImportError:  # pragma: no cover
        return True


class _Source:
    """What a field or button needs to follow HOST's names of KIND: set_source(sai, host) reads them (a token per
    read: a late answer for another host is dropped), and the hub's changed reads them again."""

    def init_source(self, kind):
        if kind not in KINDS:
            raise ValueError(f"unknown kind {kind}")
        self.kind, self.sai, self.host, self.token = kind, None, LOCAL, 0
        self.names, self.problem, self.reading, self.names_host = None, None, False, None
        hub().changed.connect(self._hub_changed)

    def set_source(self, sai, host=LOCAL, read=True):
        """Offer HOST's names (read in the background through SAI, the app's backend; READ False: the names given
        are that host's, read just now - read again only when they change)."""
        self.sai, self.host = sai, host or LOCAL
        if read or self.names_host != self.host:
            self.reload()

    def set_host(self, host):
        if self.sai is not None and (host or LOCAL) != self.host:
            self.set_source(self.sai, host)

    def reload(self):
        if self.sai is None:
            return
        self.token += 1
        token, host = self.token, self.host
        if self.names_host != host:  # another host's names are not offered for this one, not even meanwhile
            self.names_host = host
            self.show_names([], None)
            self.names = None
        self.reading = True
        self.show_names(None, None)
        names_of(self.sai, self.kind, host, lambda names, problem: alive(self) and self._read(token, host, names,
                                                                                              problem))

    def _read(self, token, host, names, problem):
        if token != self.token or host != self.host:
            return  # an answer for another host, or one overtaken by a newer read
        self.reading = False
        self.show_names(names, problem)

    def _hub_changed(self, kind, host):
        if alive(self) and kind == self.kind and host == self.host and self.sai is not None:
            self.reload()

    def note(self):
        """What the tooltip says about the names offered."""
        if self.reading:
            return tr("Reading what {host} has…", host=where(self.host))
        if self.problem:
            return tr("Could not be read: {error}", error=self.problem)
        if self.names is None:
            return ""
        return tr("{count} on {host}", count=len(self.names), host=where(self.host))


class NameField(_Source, QComboBox):
    """An editable combo box for the name of a secret (KIND "secret") or of a VPN profile ("vpn"): the chosen host's
    names in its list and as you type (a completer that finds a part anywhere in a name), any other name typed is
    kept. It reads like a QLineEdit (text, setText, textChanged, editingFinished, setPlaceholderText), so a form
    that had one keeps working. NAMES: a list known already (offered until set_source reads the host's)."""
    textChanged = Signal(str)
    editingFinished = Signal()

    def __init__(self, kind, sai=None, host=LOCAL, names=None, placeholder="", parent=None):
        super().__init__(parent)
        self.init_source(kind)
        self.tip = ""
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)  # a name typed is not added to the list
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(16)
        self.lineEdit().setMaxLength(64)  # a secret's name: up to 64 characters; a profile's: 32
        found = QCompleter(self.model(), self)
        found.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        found.setFilterMode(Qt.MatchFlag.MatchContains)
        found.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self.setCompleter(found)
        self.addItem("")
        self.currentTextChanged.connect(self.textChanged)
        self.lineEdit().editingFinished.connect(self.editingFinished)
        if placeholder:
            self.setPlaceholderText(placeholder)
        if names is not None:  # HOST's, known already: offered at once, and again when read
            self.names_host = host or LOCAL
            self.show_names(list(names), None)
        if sai is not None:
            self.set_source(sai, host)

    # -- what a QLineEdit has --

    def text(self):
        return self.currentText()

    def setText(self, text):  # noqa: N802
        self.setEditText(str(text or ""))

    def setPlaceholderText(self, text):  # noqa: N802
        self.lineEdit().setPlaceholderText(text)

    def placeholderText(self):  # noqa: N802
        return self.lineEdit().placeholderText()

    def setReadOnly(self, on):  # noqa: N802
        self.lineEdit().setReadOnly(on)
        self.setEnabled(not on)

    def setMaxLength(self, length):  # noqa: N802
        self.lineEdit().setMaxLength(length)

    def setToolTip(self, text):  # noqa: N802
        self.tip = text or ""
        self.show_tip()

    def setAccessibleName(self, name):  # noqa: N802
        super().setAccessibleName(name)
        self.lineEdit().setAccessibleName(name)

    # -- the names --

    def show_names(self, names, problem):
        """Offer NAMES (None: being read, or PROBLEM: not read); what is typed or chosen stays, a name the host lacks
        too."""
        if names is not None or problem is not None:
            self.names, self.problem = names, problem
        current = self.currentText()
        if names is not None:
            self.blockSignals(True)
            try:
                self.clear()
                self.addItem("")
                for name in names:
                    self.addItem(name)
                    self.setItemData(self.count() - 1, safe_tooltip(name), Qt.ItemDataRole.ToolTipRole)
                self.setEditText(current)
            finally:
                self.blockSignals(False)
        self.show_tip()

    def show_tip(self):
        note = self.note()
        super().setToolTip(safe_tooltip(f"{self.tip}\n{note}" if self.tip and note else self.tip or note))

    def offered(self):
        """The names in the list (what the host has, as read)."""
        return [self.itemText(index) for index in range(1, self.count())]


def add_line(text, name, option=None):
    """TEXT (one value per line) with NAME added: completing a last line that ends in "=" (HOST= of --api-key, URL= of
    --git-token-secret), else a line of its own - for those two HOST=NAME and https://HOST=NAME, HOST to be typed over."""
    lines = text.splitlines()
    if lines and lines[-1].rstrip().endswith("="):
        lines[-1] = lines[-1].rstrip() + name
    elif name in [line.strip() for line in lines]:
        return text
    else:
        lines.append({"--api-key": "HOST=", "--git-token-secret": "https://HOST="}.get(option, "") + name)
    return "\n".join(lines)


class NameButton(_Source, QToolButton):
    """A "+" button whose menu has the chosen host's names of KIND; picked(name) when one is chosen (for a field of one
    value per line, e.g. --secret-env)."""
    picked = Signal(str)

    def __init__(self, kind, sai=None, host=LOCAL, parent=None):
        super().__init__(parent)
        self.init_source(kind)
        self.setText("+")
        self.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.setMenu(QMenu(self))
        self.setAccessibleName(tr("Add one"))
        self.show_names(None, None)
        if sai is not None:
            self.set_source(sai, host)

    def show_names(self, names, problem):
        if names is not None or problem is not None:
            self.names, self.problem = names, problem
        menu = self.menu()
        menu.clear()
        for name in self.names or []:
            menu.addAction(name, lambda n=name: self.picked.emit(n))
        if not self.names:
            text = (tr("Could not be read: {error}", error=self.problem) if self.problem else
                    tr("Reading…") if self.names is None else tr("None on {host}", host=where(self.host)))
            menu.addAction(text).setEnabled(False)
        tip = tr("Add one that {host} has", host=where(self.host)) if self.sai is not None else tr("Add one")
        note = self.note()
        self.setToolTip(safe_tooltip(f"{tip}\n{note}" if note else tip))
