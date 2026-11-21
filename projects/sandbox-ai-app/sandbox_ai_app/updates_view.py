"""Settings › Updates: this machine's update mode (`sandbox-ai update mode`) and what it found.

The mode (off, notify - the default -, download, auto) with auto's window, set with Set… (asked first: update-mode);
the last check and its result, the next check, a release downloaded and checked, an install waiting for its window;
Check now (`sandbox-ai update --check`), Install now… (a downloaded release: `sandbox-ai update --apply`; else
`sandbox-ai update --install`: the newest signed release downloaded, checked and installed in the background, for
every copy that replaces itself - install.sh's installation, the AppImage, the macOS app, the .pyz, a pip
installation; a system installation this user cannot write: the window's Update now…, sudo in a terminal) and Roll
back… (`sandbox-ai update --rollback`): both asked first, and when something of this installation runs, its names are
shown and asked about once more (it goes on with the version it started with: --while-running). After an install or a
rollback the window offers "Restart the app" and never restarts by itself. A copy that cannot replace itself (a
checkout, a package or a pip installation in a folder this user cannot write, a system installation) has notify only:
the modes it cannot have are greyed, the command that updates it is shown. Every command runs in the background; the
GUI thread never waits for the network.
"""
from __future__ import annotations

import time

from PySide6.QtWidgets import QComboBox, QFormLayout, QHBoxLayout, QLineEdit, QPushButton, QVBoxLayout, QWidget

from . import backend
from .files import printable, printable_text
from .i18n import N_, tr
from .widgets import ask_first, background, plain_label, safe_tooltip, warn

MODES = (("off", N_("off - nothing is checked unless asked")),
         ("notify", N_("notify - a newer signed release is announced (the default)")),
         ("download", N_("download - also downloaded and checked; installed when you say")),
         ("auto", N_("auto - also installed in its window while nothing runs")))


def when_of(seconds):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(seconds)) if isinstance(seconds, (int, float)) else \
        tr("never")


def result_text(result):
    """What the last check found, in words."""
    if not isinstance(result, dict):
        return tr("not checked yet")
    if result.get("error"):
        return tr("failed: {error}", error=printable(result["error"]))
    if result.get("newer"):
        return tr("{available} is available (installed: {installed}), signed by a trusted key",
                  available=printable(result.get("available")), installed=printable(result.get("installed")))
    if backend.newer(result.get("available"), result.get("installed")):
        return tr("{available} is there, but not signed by a trusted key: not offered",
                  available=printable(result.get("available")))
    return tr("up to date ({installed})", installed=printable(result.get("installed")))


class UpdatesPanel(QWidget):
    """The Updates tab of the settings. WINDOW: the main window (its update_ended offers "Restart the app", its
    update_now runs `sandbox-ai update` in a terminal), or None."""

    def __init__(self, sai, window=None, parent=None):
        super().__init__(parent)
        # (main_window, never "window": that name would hide QWidget.window(), which every question and message asks
        # of its parent - Set… raised TypeError once its question was answered)
        self.sai, self.main_window, self.data = sai, window, None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        form = QFormLayout()
        self.mode = QComboBox()
        self.mode.setAccessibleName(tr("Update mode"))
        for value, text in MODES:
            self.mode.addItem(tr(text), value)
        self.window_field = QLineEdit()
        self.window_field.setPlaceholderText("03:00-05:00")
        self.window_field.setAccessibleName(tr("Window of mode auto"))
        self.window_field.setToolTip(safe_tooltip(tr("auto installs only in this window, local time (HH:MM-HH:MM; "
                                                     "HH:MM alone: that hour)")))
        self.set_button = QPushButton(tr("Set…"))
        self.set_button.clicked.connect(lambda _=False: self.set_mode())
        row = QHBoxLayout()
        row.addWidget(self.mode, 1)
        row.addWidget(self.set_button)
        form.addRow(tr("Update mode"), row)
        form.addRow(tr("Window of mode auto"), self.window_field)
        self.state = plain_label(tr("Reading…"), selectable=True)
        fit_in_form(self.state)
        form.addRow(tr("State"), self.state)
        layout.addLayout(form)
        buttons = QHBoxLayout()
        self.check_button = QPushButton(tr("Check now"))
        self.check_button.clicked.connect(lambda _=False: self.check_now())
        self.install_button = QPushButton(tr("Install now…"))
        self.install_button.clicked.connect(lambda _=False: self.install_now())
        self.rollback_button = QPushButton(tr("Roll back…"))
        self.rollback_button.clicked.connect(lambda _=False: self.roll_back())
        for button in (self.check_button, self.install_button, self.rollback_button):
            buttons.addWidget(button)
        buttons.addStretch()
        layout.addLayout(buttons)
        self.status = plain_label(role="secondary", selectable=True)
        layout.addWidget(self.status)
        layout.addStretch()
        self.show_data(None)
        self.refresh()

    def buttons(self):
        return [self.set_button, self.check_button, self.install_button, self.rollback_button]

    # -- reading --

    def refresh(self):
        if not self.sai.has_update_mode():
            self.state.setText(tr("This sandbox-ai has no update mode yet (0.1.122 or later): update it."))
            for button in self.buttons():
                button.setEnabled(False)
            return
        background(self.sai.update_status, owner=self, timeout=90, what="update mode --json",
                   done=lambda result, error: self.read(result, error))

    def read(self, result, error):
        if error is not None:
            self.state.setText(tr("Could not read the update mode: {error}", error=printable_text(error)))
            return
        self.show_data(result)

    def show_data(self, data):
        self.data = data if isinstance(data, dict) else None
        data = self.data or {}
        refused = data.get("refused") or {}
        model = self.mode.model()
        for index, (value, _) in enumerate(MODES):
            item = model.item(index)
            if item is not None:
                item.setEnabled(value not in refused)
                item.setToolTip(safe_tooltip(refused.get(value) or ""))
        if data.get("mode"):
            self.mode.setCurrentIndex(max(0, self.mode.findData(data["mode"])))
            self.window_field.setText(str(data.get("window") or ""))
        lines = []
        if data:
            lines.append(tr("Mode {mode}", mode=data["mode"]) + ("" if data.get("chosen") else
                                                                   " " + tr("(the default)")))
            lines.append(tr("Last check: {when}: {result}", when=when_of(data.get("last_check")),
                            result=result_text(data.get("result"))))
            lines.append(tr("Next check: none (mode off)") if data["mode"] == "off" else
                         tr("Next check: after {when}", when=when_of(data.get("next_check"))))
            if data.get("downloaded"):
                lines.append(tr("Downloaded and checked: {version} (Install now installs it)",
                                version=printable(data["downloaded"])))
            if isinstance(data.get("waiting"), dict) and data["waiting"].get("text"):
                lines.append(printable(data["waiting"]["text"]))
            if isinstance(data.get("last_install"), dict):
                last = data["last_install"]
                lines.append(tr("Last change: {old} → {new} at {when}", old=printable(last.get("from")),
                                new=printable(last.get("to")), when=when_of(last.get("time"))))
            if data.get("manual"):
                lines.append(tr("This copy updates with: {command}", command=printable(data["manual"])))
        self.state.setText("\n".join(lines) or tr("Reading…"))
        newer = bool((data.get("result") or {}).get("newer"))
        self.install_button.setEnabled(bool(data.get("downloaded")) or (newer and (self.installs_here(data)
                                                                                   or self.window_offers())))
        self.rollback_button.setEnabled(bool(data.get("rollback")))
        self.rollback_button.setToolTip(safe_tooltip(tr("Back to {version}, the version kept before",
                                                        version=printable(data["rollback"]))
                                                     if data.get("rollback") else tr("No other version is kept")))

    def installs_here(self, data):
        """Whether Install now… can install a release into this copy in the background (`update --install`): a copy
        that replaces itself, or a system installation this user may write (an older sandbox-ai: what updates
        itself)."""
        return bool(data.get("install_now", data.get("self_updating"))) and self.has_install()

    def has_install(self):
        return bool(getattr(self.sai, "has_update_install", lambda: False)())

    def window_offers(self):
        """The window's Update now… (a terminal; a system installation: sudo there) is there for this copy."""
        return self.main_window is not None and getattr(self.main_window, "update_offer", None) is not None

    # -- changing --

    def set_mode(self):
        mode = self.mode.currentData()
        window = self.window_field.text().strip() if mode == "auto" else ""
        text = {"off": tr("Nothing is checked unless asked; the update timer is removed."),
                "notify": tr("A check once a day; a newer signed release is announced."),
                "download": tr("A check once a day; a newer signed release is also downloaded and checked, and "
                               "installed when you say."),
                "auto": tr("A check once a day; a newer signed release is also installed in the window {window} "
                           "while nothing of this installation runs.", window=window or "03:00-05:00")}[mode]
        if not ask_first(self, "update-mode", tr("Update mode"), tr("Set the update mode to {mode}?", mode=mode) +
                         " " + text, tr("Set")):
            return
        self.status.setText(tr("Working…"))
        background(lambda: self.sai.set_update_mode(mode, window or None), owner=self, work=True,
                   source=self.buttons(), timeout=240, what=f"update mode {mode}",
                   done=lambda result, error: self.mode_set(result, error))

    def mode_set(self, result, error):
        if error is not None:
            warn(self, tr("Update mode"), printable_text(error))
            self.status.setText(tr("Not done."))
            return
        self.status.setText(tr("Saved."))
        self.show_data(result)

    def check_now(self):
        self.status.setText(tr("Checking for a newer sandbox-ai…"))
        background(self.sai.update_check_now, owner=self, source=self.buttons(), timeout=240,
                   what="update --check", done=lambda result, error: self.checked(result, error))

    def checked(self, result, error):
        status, out, err = result if error is None else (1, "", printable_text(error))
        self.status.setText(tr("Checked.") if status == 0 else
                            printable_text(((err or out).strip().splitlines() or [tr("Failed.")])[-1])[-300:])
        self.refresh()

    def install_now(self):
        data = self.data or {}
        if not data.get("downloaded") and self.installs_here(data):
            # Nothing fetched yet: downloaded, checked and installed in one go, in the background.
            version = printable((data.get("result") or {}).get("available") or "")
            if not ask_first(self, "update-apply", tr("Install now"),
                             tr("Download sandbox-ai {version} now, check it (SHA256SUMS and its signature by a "
                                "trusted key) and install it? The version before is kept for Roll back…; a running "
                                "app or sandbox goes on with the version it started with.", version=version),
                             tr("Install")):
                return
            self.run_change("install", False, version)
            return
        if not data.get("downloaded"):  # a system installation of root's: the window's update in a terminal (sudo)
            if self.window_offers():
                self.main_window.update_now()
            else:
                warn(self, tr("Install now"), tr("This copy does not replace itself: {command}",
                                                 command=printable(data.get("manual") or "sandbox-ai update")))
            return
        version = printable(data["downloaded"])
        if not ask_first(self, "update-apply", tr("Install now"),
                         tr("Install sandbox-ai {version} now (downloaded and checked; it is checked once more)? A "
                            "running app or sandbox goes on with the version it started with.", version=version),
                         tr("Install")):
            return
        self.run_change("apply", False, version)

    def roll_back(self):
        target = printable((self.data or {}).get("rollback") or "")
        if not target or not ask_first(self, "update-rollback", tr("Roll back"),
                                       tr("Go back to sandbox-ai {version} (the version kept before)? download "
                                          "and auto leave the version you leave out from then on.", version=target),
                                       tr("Roll back")):
            return
        self.run_change("rollback", False, target)

    def run_change(self, what, while_running, version):
        call = {"apply": self.sai.update_apply, "rollback": self.sai.update_rollback,
                "install": getattr(self.sai, "update_install", None)}[what]
        self.status.setText(tr("Downloading, checking and installing…") if what == "install" else tr("Working…"))
        background(lambda: call(while_running), owner=self, work=True, source=self.buttons(), timeout=1900,
                   what=f"update --{what}", done=lambda result, error: self.changed(what, version, result, error))

    def changed(self, what, version, result, error):
        status, out, err = result if error is None else (1, "", printable_text(error))
        running = backend.update_busy_reasons(status, err)
        if running:  # what of this installation runs: named, and asked once more
            text = tr("These run now: {list}. They go on with the version they started with; the change applies to "
                      "what starts afterwards. Go ahead?", list="; ".join(printable(r) for r in running[:12]))
            if what in ("apply", "install"):
                yes = ask_first(self, "update-apply", tr("Install now"), text, tr("Go ahead"))
            else:
                yes = ask_first(self, "update-rollback", tr("Roll back"), text, tr("Go ahead"))
            if yes:
                self.run_change(what, True, version)
            else:
                self.status.setText(tr("Not done."))
            return
        if status != 0:
            warn(self, tr("Updates"), printable_text((err or out).strip())[-1500:] or
                 tr("Failed (exit status {status}).", status=status))
            self.status.setText(tr("Not done."))
            self.refresh()
            return
        self.status.setText(printable_text(out.strip())[-500:])
        if what == "install" and "is already installed and up to date" in out:
            self.refresh()  # (nothing changed: nothing to restart for)
            return
        if self.main_window is not None and hasattr(self.main_window, "update_ended"):
            self.main_window.update_ended(version, 0)  # "Restart the app" offered; never restarted by itself
        self.refresh()


def fit_in_form(label):
    """LABEL (a wrapped label in a QFormLayout's row) at least as high as its text at its width: the form gave it the
    height of its size hint - its lines at another width - and the state of several lines was cut to one (the German
    "Modus notify (die Voreinstellung)" + "Letzte Prüfung: ..." at the smallest window: 14 px of 56)."""
    label.setWordWrap(True)
    resize = label.resizeEvent

    def resized(event):
        resize(event)
        need = label.heightForWidth(label.width())
        if need > 0 and need != label.minimumHeight():
            label.setMinimumHeight(need)
    label.resizeEvent = resized

