"""The devices of a running sandbox, changed while it runs (user 2026-11-19: "add feature for add device to board during
runtime"): what it has now (`sandbox-ai devices list RUN --json` on its host) with each one's state, "Remove from the
run" for those added during the run (devices given at the start stay until it ends: `devices remove` takes only
those), and "Add device now…" from the devices its host has (device_picker: `devices scan` there) minus those it has.
A GPU kind is listed and not offered: --gpu cannot change while a sandbox runs (a restart takes it). Read again on
Refresh, when a USB device is plugged in or out (this machine: its USB devices looked at every LOCAL_POLL ms; another
host: read again every REMOTE_POLL ms while shown), and after each change."""
from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from . import backend, widgets
from . import device_picker as DP
from .files import printable, printable_text
from .host_choice import on_host
from .i18n import tr
from .widgets import plain_label, safe_tooltip

LOCAL = "local"
LOCAL_POLL = 3000  # ms: this machine's USB devices looked at (a directory listing of sysfs; nothing is run)
REMOTE_POLL = 20000  # ms: another host's devices and the run's read again (one command over its shared connection)
USB_DIR = Path("/sys/bus/usb/devices")


def usb_signature():
    """What this machine's USB devices are now (their sysfs names): another value after a plug or unplug."""
    try:
        return tuple(sorted(os.listdir(USB_DIR)))
    except OSError:
        return None


class LiveDevices(QWidget):
    """RUN's devices on HOST now, with Add and Remove (see the module's text). added(value): VALUE was given to the
    run (the Board asks whether its later starts get it too)."""
    added = Signal(str)

    def __init__(self, sai, run, host=LOCAL, parent=None, poll=True):
        super().__init__(parent)
        self.sai, self.run, self.host = sai, run, host or LOCAL
        self.listing, self.entries, self.token, self.reading = [], [], 0, False
        self.signature = usb_signature() if self.host == LOCAL else None
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(3)
        self.title = plain_label(tr("Devices of {run} now:", run=printable(run)) if self.host == LOCAL else
                                 tr("Devices of {run} on {host} now:", run=printable(run), host=printable(self.host)))
        column.addWidget(self.title)
        self.rows = QWidget()
        self.rows_layout = QVBoxLayout(self.rows)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(2)
        column.addWidget(self.rows)
        self.picker = QComboBox()
        self.picker.setEditable(True)
        self.picker.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.picker.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.picker.setMinimumContentsLength(16)
        self.picker.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.picker.setAccessibleName(tr("Device to add now"))
        self.picker.lineEdit().setPlaceholderText(tr("{path} or {usb}", path="/dev/ttyUSB0", usb="usb:2e8a:000a"))
        self.picker.setToolTip(safe_tooltip(tr("The devices {host} has and {run} has not; a path under /dev or "
                                               "usb:VENDOR:PRODUCT[:SERIAL] may be typed", host=DP.host_name(self.host),
                                               run=printable(run))))
        self.add_button = QPushButton(tr("Add device now…"))
        self.add_button.setAutoDefault(False)
        self.add_button.clicked.connect(lambda _=False: self.add_now())
        self.refresh_button = QPushButton(tr("Refresh"))
        self.refresh_button.setAutoDefault(False)
        self.refresh_button.setToolTip(safe_tooltip(tr("Read the devices of {host} and of {run} again",
                                                       host=DP.host_name(self.host), run=printable(run))))
        self.refresh_button.clicked.connect(lambda _=False: self.reload(refresh=True))
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.picker, 1)
        row.addWidget(self.add_button)
        row.addWidget(self.refresh_button)
        column.addLayout(row)
        self.note = plain_label("", role="secondary", selectable=True)
        self.note.hide()
        column.addWidget(self.note)
        self.gpu_note = plain_label(tr("A GPU (--gpu) cannot be given while it runs: set it on the part and start "
                                       "again (a restart takes it)."), role="notice-warn")
        self.gpu_note.hide()
        column.addWidget(self.gpu_note)
        self.timer = QTimer(self)
        self.timer.setInterval(LOCAL_POLL if self.host == LOCAL else REMOTE_POLL)
        self.timer.timeout.connect(self.poll)
        if poll:
            self.timer.start()
        self.reload()

    # -- reading --

    def reload(self, refresh=False):
        """The run's devices and its host's read (again: REFRESH the host's, not from the cache)."""
        self.token += 1
        token, run, host, sai = self.token, self.run, self.host, self.sai
        self.reading = True

        def read():
            try:
                listing, listing_error = sai.devices_listed(run, host), None
            except Exception as exc:  # noqa: BLE001 - said in the part
                listing, listing_error = [], exc
            try:
                entries, scan_error = DP.scan(sai, host, refresh), None
            except Exception as exc:  # noqa: BLE001 - said in the part; a path may still be typed
                entries, scan_error = [], exc
            return listing, listing_error, entries, scan_error
        widgets.background(read, owner=self, timeout=150, work=False,
                           done=lambda found, error: self.loaded(token, found, error))

    def loaded(self, token, found, error):
        if token != self.token or not widgets_alive(self):
            return
        self.reading = False
        listing, listing_error, entries, scan_error = found if error is None else ([], error, [], error)
        self.listing, self.entries = list(listing or []), list(entries or [])
        self.show_listing(listing_error)
        self.show_picker(scan_error)

    def poll(self):
        """A USB device plugged in or out here (another host: every REMOTE_POLL ms): read again."""
        if self.reading or not self.isVisible():
            return
        if self.host == LOCAL:
            now = usb_signature()
            if now == self.signature:
                return
            self.signature = now
        DP.forget(self.host)
        self.reload(refresh=True)

    # -- showing --

    def show_listing(self, error):
        while self.rows_layout.count():
            widget = self.rows_layout.takeAt(0).widget()
            if widget is not None:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        if error is not None:
            self.rows_layout.addWidget(plain_label(tr("What {run} has could not be read: {error}", run=printable(
                self.run), error=printable_text(str(error))[:300]), role="notice-warn", selectable=True))
            return
        shown = [entry for entry in self.listing if entry.get("state") != "removed"]
        if not shown:
            self.rows_layout.addWidget(plain_label(tr("No devices given to it."), role="secondary"))
            return
        for entry in shown:
            line = QWidget()
            line.setObjectName(f"device:{entry.get('id') or entry.get('value')}")
            row = QHBoxLayout(line)
            row.setContentsMargins(0, 0, 0, 0)
            text = QLabel(f"{DP.listing_label(entry)}  ({DP.state_label(entry)})")
            text.setTextFormat(Qt.TextFormat.PlainText)
            text.setWordWrap(True)
            text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            row.addWidget(text, 1)
            if entry.get("source") == "added" and entry.get("id") and entry.get("state") in DP.LIVE_STATES:
                remove = QPushButton(tr("Remove from the run"))
                remove.setAutoDefault(False)
                remove.setAccessibleName(tr("Remove {what} from {run}", what=entry.get("value") or "?",
                                            run=printable(self.run)))
                remove.clicked.connect(lambda _=False, e=entry, b=remove: self.remove(e, b))
                row.addWidget(remove)
            else:
                text.setToolTip(safe_tooltip(tr("Given at the start: it stays until the sandbox ends (only a device "
                                                "added during the run can be taken away)")))
            self.rows_layout.addWidget(line)

    def show_picker(self, error):
        typed = self.picker.currentText()
        self.picker.blockSignals(True)
        self.picker.clear()
        items = DP.live_items(self.entries, self.listing) if error is None else []
        group = ""
        for entry, in_group, why in items:
            if in_group != group:
                group = in_group
                self.picker.addItem(group)
                header = self.picker.model().item(self.picker.count() - 1)
                header.setFlags(Qt.ItemFlag.NoItemFlags)
                font = header.font()
                font.setBold(True)
                header.setFont(font)
            label = entry["label"] + (f"  ({tr('restart needed')})" if why else "")
            self.picker.addItem(label, None if why else entry["value"])
            index = self.picker.count() - 1
            self.picker.setItemData(index, safe_tooltip(why or entry.get("note") or entry["value"]),
                                    Qt.ItemDataRole.ToolTipRole)
            if why:
                self.picker.model().item(index).setFlags(Qt.ItemFlag.NoItemFlags)
        self.picker.setCurrentIndex(-1)
        self.picker.setEditText(typed)
        self.picker.blockSignals(False)
        text = DP.note(self.host, self.entries, error, held_count=DP.held_count(self.entries, self.listing))
        self.note.setText(text)
        self.note.setVisible(bool(text))
        self.gpu_note.setVisible(any(entry["option"] == "--gpu" for entry in self.entries))

    def items(self):
        """[(label, value or None)] the picker offers (None: a group's name, or not to be chosen)."""
        return [(self.picker.itemText(i), self.picker.itemData(i)) for i in range(self.picker.count())]

    def chosen(self):
        """What the picker holds: the value of the device chosen (or whose label was typed), else the text typed."""
        text = self.picker.currentText().strip()
        index = self.picker.findText(text, Qt.MatchFlag.MatchExactly)
        if text and index >= 0:
            return self.picker.itemData(index) or ""
        return text

    def choose(self, value):
        """The device VALUE chosen in the picker (a test, a finding): True when it is offered."""
        index = self.picker.findData(value)
        if index < 0:
            return False
        self.picker.setCurrentIndex(index)
        return True

    # -- changing --

    def add_now(self):
        value = self.chosen()
        title = tr("Add device") if self.host == LOCAL else tr("Add device on {host}", host=printable(self.host))
        if DP.gpu_of(value) or value in backend.GPU_KINDS:
            widgets.inform(self, title, tr("A GPU (--gpu) cannot be given while it runs: set it on the part and start "
                                           "again (a restart takes it)."))
            return
        if not backend.valid_device(value):
            widgets.warn(self, title, tr("A device is a path under /dev/, or usb:VENDOR:PRODUCT[:SERIAL]."))
            return
        text = tr("Give {list} to {run} until it ends? Devices weaken the isolation.", list=printable(value),
                  run=printable(self.run))
        if not widgets.ask_first(self, "device-add", title, text if self.host == LOCAL else on_host(self.host, text),
                                 tr("Add")):
            return
        run, host = self.run, self.host
        widgets.background(lambda: self.sai.add_device(run, value) if host == LOCAL else
                           self.sai.add_device(run, value, host=host), owner=self,
                           source=[self.add_button, self.picker], timeout=300, work=True,
                           done=lambda result, error: self.add_done(title, value, result, error))

    def add_done(self, title, value, result, error):
        if not widgets_alive(self):
            return
        failed = error is not None or not isinstance(result, tuple) or result[0] != 0
        text = printable_text(error) if error is not None else printable_text(
            "\n".join(filter(None, [result[1].strip(), result[2].strip()])) if isinstance(result, tuple) else result)
        if failed:
            widgets.warn(self, title, text or tr("Not added"))
        else:
            self.picker.setEditText("")
            widgets.inform(self, title, text or tr("Added"), modal=False)
        self.reload(refresh=True)
        if not failed:
            self.added.emit(value)

    def remove(self, entry, button=None):
        what = entry.get("value") or entry.get("id") or "?"
        title = tr("Remove device") if self.host == LOCAL else tr("Remove device from {run} on {host}",
                                                                  run=printable(self.run), host=printable(self.host))
        if not widgets.confirm(self, title, tr("Take {what} away from {run}? What uses it there loses it.",
                                               what=printable(what), run=printable(self.run)), tr("Remove")):
            return
        run, host, ident = self.run, self.host, entry["id"]
        widgets.background(lambda: self.sai.remove_device(run, ident) if host == LOCAL else
                           self.sai.remove_device(run, ident, host=host), owner=self, source=button, timeout=150,
                           work=True, done=lambda result, error: self.remove_done(title, result, error))

    def remove_done(self, title, result, error):
        if not widgets_alive(self):
            return
        if error is not None or not isinstance(result, tuple) or result[0] != 0:
            widgets.warn(self, title, printable_text(error) if error is not None else
                         printable_text((result[1] + result[2]).strip()) if isinstance(result, tuple) else "?")
        self.reload(refresh=True)


def widgets_alive(widget):
    try:
        from shiboken6 import isValid
        return isValid(widget)
    except ImportError:  # pragma: no cover - PySide6 brings shiboken6
        return True
