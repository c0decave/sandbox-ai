"""The sandbox's own browser (--browser) in the app's window: a tab of the Terminals page, right beside the terminal tab
of its start - "Browser: NAME" with a browser icon -, its live view (vnc_view.BrowserPanel) inside.

One tab per start: asked for again (Sandboxes & sessions: the Browser button or its right click, a tab's menu), its
tab is shown. Its right click: Open in its own window (a window of the desktop, on the app's screen, raised; its
"Back into the tabs" puts it back beside its terminal), Focus in Sandboxes & sessions, Close tab, Close other tabs.
It follows its start as the terminal tabs do (the Sandboxes page's reads): when the start ends, the tab (or its window)
says so and closes AUTO_CLOSE_MS later unless the start runs again meanwhile; a start that runs again with another run
(a resumed session) is connected to its new display. The browser tabs open when the app ends come back at its next
start - only those whose start still runs with a browser that shows a display; the others are left out and logged.
"""
from __future__ import annotations

import time

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QVBoxLayout, QWidget

from . import applog, theme, vnc_view
from .files import printable
from .i18n import tr

SAVED = "browser-tabs"  # the app's state: [{"key", "name"}] of the browser tabs open when the app ended
SAVED_LIMIT = 20
AUTO_CLOSE_MS = 3000  # as a terminal tab of a session that ended (terminal.AUTO_CLOSE_MS)
ACTIVE = ("running", "paused", "starting")  # a start in these states keeps its browser's tab


def raise_window(window, near):
    """WINDOW shown, not minimised, on the screen of NEAR's window (centred there when it was not shown yet), inside
    it, raised and active - never left behind the app or on another screen."""
    from .widgets import fit_on_screen
    if window.isMinimized():
        window.setWindowState(window.windowState() & ~Qt.WindowState.WindowMinimized)
    if not window.isVisible():
        anchor = near.window() if near is not None else None
        screen = (anchor.screen() if anchor is not None else None) or window.screen()
        if screen is not None:
            area = screen.availableGeometry()
            window.move(area.center().x() - window.width() // 2, area.center().y() - window.height() // 2)
    window.show()
    fit_on_screen(window)
    window.raise_()
    window.activateWindow()


def title_of(name):
    return tr("Browser: {name}", name=printable(name))


class BrowserFrame(QWidget):
    """A browser tab opened in its own window: the panel is the whole window; closing the window closes the
    browser's view (Back into the tabs puts it back as a tab)."""

    def __init__(self, tabs, panel):
        super().__init__(None, Qt.WindowType.Window)
        from .widgets import close_controls
        self.tabs, self.panel, self.returning = tabs, panel, False
        self.setObjectName("browserWindow")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle(panel.title)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(panel)
        panel.set_in_window(True)
        panel.back.show()
        panel.show()
        self.resize(1080, 760)
        close_controls(self)  # an [x] at its top right, Esc closes it

    def closeEvent(self, event):  # noqa: N802
        if not self.returning:
            applog.log("INFO", "browser", f"the browser of {printable(self.panel.name)}: its own window closed")
            self.panel.shutdown()
        self.tabs.frame_closed(self)
        super().closeEvent(event)


class BrowserTabs:
    """The browser tabs (and the windows they were opened in) of the Terminals page PAGE (terminal.TerminalsPage:
    its tabs, its store, its sessions_page and focus_start)."""

    def __init__(self, page):
        self.page, self.frames, self.restored = page, [], False

    # -- what is open --

    def in_tabs(self):
        tabs = self.page.tabs
        return [widget for widget in (tabs.widget(i) for i in range(tabs.count()))
                if isinstance(widget, vnc_view.BrowserPanel)]

    def panels(self):
        return self.in_tabs() + [frame.panel for frame in self.frames]

    def find(self, key):
        return next((panel for panel in self.panels() if panel.instance_key == key), None)

    def frame_of(self, panel):
        return next((frame for frame in self.frames if frame.panel is panel), None)

    # -- opening --

    def terminal_index(self, key):
        """The index of the last terminal tab that shows the start KEY, or -1."""
        found = -1
        for index in range(self.page.tabs.count()):
            widget = self.page.tabs.widget(index)
            if isinstance(widget, vnc_view.BrowserPanel):
                continue
            try:
                instance = self.page.instance_of(widget)
            except (AttributeError, RuntimeError):
                instance = None
            if instance is not None and instance.key == key:
                found = index
        return found

    def open(self, instance, activate=True):
        """INSTANCE's browser (vnc_view.why_not said yes): its tab right after its terminal's tab (else at the end),
        shown (ACTIVATE); the tab or window it has already, shown. Returns its panel."""
        browser = vnc_view.browser_record(instance)
        name = str(getattr(instance, "name", "") or getattr(instance, "run", "") or "?")
        panel = self.find(instance.key)
        if panel is not None:
            panel.retarget(browser["vnc"])
            if activate:
                self.show(panel)
            applog.log("INFO", "browser", f"the browser of {printable(name)} is open already: its "
                                          f"{'window' if self.frame_of(panel) else 'tab'} is shown")
            return panel
        panel = vnc_view.BrowserPanel(title_of(name), browser["vnc"], browser.get("control") or "human",
                                      tor=vnc_view.tor_of(browser))
        panel.instance_key, panel.name = instance.key, name
        panel.back.clicked.connect(lambda _=False, p=panel: self.to_tabs(p))
        beside = self.terminal_index(instance.key)
        self.insert(panel, beside + 1 if beside >= 0 else self.page.tabs.count(), activate)
        applog.log("INFO", "browser", f"the browser of {printable(name)} opens in a tab of Terminals ("
                   + ("beside its terminal" if beside >= 0 else "no terminal tab of it: at the end")
                   + f"), its display {browser['vnc']} (control {browser.get('control')})")
        self.save()
        return panel

    def insert(self, panel, index, activate=True):
        tabs = self.page.tabs
        panel.set_in_window(False)
        panel.back.hide()
        index = tabs.insertTab(index, panel, theme.glyph("browser", theme.colors()["text"], 16), panel.title)
        tabs.setTabToolTip(index, tr("The browser of {name} (its own container in the sandbox's pod); right click: "
                                     "Open in its own window, Close tab", name=printable(panel.name)))
        if activate:
            tabs.setCurrentIndex(index)
            panel.view.setFocus()
        self.page.update_close_key()
        return index

    def show(self, panel):
        frame = self.frame_of(panel)
        if frame is not None:
            raise_window(frame, self.page)
            return
        self.page.tabs.setCurrentWidget(panel)
        panel.view.setFocus()

    # -- moving and closing --

    def to_window(self, panel):
        """The tab of PANEL in a window of its own (on the app's screen, raised)."""
        index = self.page.tabs.indexOf(panel)
        if index < 0:
            return None
        self.page.tabs.removeTab(index)
        frame = BrowserFrame(self, panel)
        self.frames.append(frame)
        raise_window(frame, self.page)
        applog.log("INFO", "browser", f"the browser of {printable(panel.name)}: opened in its own window")
        self.page.update_close_key()
        self.save()
        return frame

    def to_tabs(self, panel):
        """PANEL's window back into a tab beside its start's terminal."""
        frame = self.frame_of(panel)
        if frame is None:
            return None
        frame.returning = True
        frame.layout().removeWidget(panel)
        panel.setParent(None)
        frame.close()
        beside = self.terminal_index(panel.instance_key)
        index = self.insert(panel, beside + 1 if beside >= 0 else self.page.tabs.count())
        applog.log("INFO", "browser", f"the browser of {printable(panel.name)}: back in the tabs")
        self.save()
        return index

    def frame_closed(self, frame):
        if frame in self.frames:
            self.frames.remove(frame)
        if not frame.returning:
            self.save()

    def close(self, panel, why="closed"):
        """PANEL's tab (or window) closed: its connection ends."""
        frame = self.frame_of(panel)
        applog.log("INFO", "browser", f"the browser of {printable(panel.name)}: its "
                                      f"{'window' if frame else 'tab'} {why}")
        if frame is not None:
            frame.close()
            return
        index = self.page.tabs.indexOf(panel)
        panel.shutdown()
        if index >= 0:
            self.page.tabs.removeTab(index)
        panel.deleteLater()
        self.page.update_close_key()
        self.save()

    def actions(self, panel):
        """The context menu of a browser tab: [(text, enabled, why, callback) or None]."""
        known = self.focusable(panel)
        focus_why = None if known else tr("Not a session the app knows (yet): see Sandboxes & sessions")
        tabs = self.page.tabs
        return [(tr("Open in its own window"), True, None, lambda: self.to_window(panel)),
                (tr("Focus in Sandboxes & sessions"), known, focus_why, lambda: self.focus(panel)),
                None,
                (tr("Close tab"), True, None, lambda: self.close(panel)),
                (tr("Close other tabs"), tabs.count() > 1, None, lambda: self.page.close_others(panel))]

    def focusable(self, panel):
        page = self.page.sessions_page() if self.page.sessions_page is not None else None
        return page is not None and self.page.focus_start is not None and \
            panel.instance_key in (getattr(page, "instances", None) or {})

    def focus(self, panel):
        if self.focusable(panel):
            self.page.focus_start(panel.instance_key)

    # -- following the starts (the Sandboxes page's reads) --

    def sync(self, instances, trusted=True):
        """INSTANCES ({key: Instance} of this machine, as the Sandboxes page read them; TRUSTED: the read worked):
        a browser whose start ended says so and closes AUTO_CLOSE_MS later; one whose start runs again shows its
        new display. The first trusted read reopens the browser tabs kept from the app's last run."""
        if not trusted:
            return
        if not self.restored:
            self.restored = True
            self.restore(instances)
        for panel in self.panels():
            instance = instances.get(panel.instance_key)
            if instance is not None and instance.state in ACTIVE:
                renamed = bool(instance.name) and str(instance.name) != panel.name  # (sandbox-ai rename: its id stays)
                if panel.ended_at is not None or renamed:
                    panel.ended_at, panel.name = None, str(instance.name or panel.name)
                    panel.title = title_of(panel.name)
                    self.retitle(panel, panel.title)
                    self.save()
                browser = vnc_view.browser_record(instance)
                if browser is not None and browser.get("vnc"):
                    panel.retarget(browser["vnc"])
                continue
            if panel.ended_at is None:
                panel.ended_at = time.monotonic()
                self.retitle(panel, tr("{title} (ended)", title=title_of(panel.name)))
                applog.log("INFO", "browser", f"the browser of {printable(panel.name)}: its start ended - its "
                           f"{'window' if self.frame_of(panel) else 'tab'} closes by itself in "
                           f"{AUTO_CLOSE_MS / 1000:g} s")
                QTimer.singleShot(AUTO_CLOSE_MS, lambda p=panel: self.close_ended(p))

    def close_ended(self, panel):
        try:
            if panel.ended_at is None or panel not in self.panels():
                return  # runs again, or closed meanwhile
        except RuntimeError:  # (gone)
            return
        self.close(panel, why="closed by itself (its start ended)")

    def retitle(self, panel, text):
        frame = self.frame_of(panel)
        if frame is not None:
            frame.setWindowTitle(text)
        index = self.page.tabs.indexOf(panel)
        if index >= 0:
            self.page.tabs.setTabText(index, text)

    # -- kept for the app's next start --

    def save(self):
        store = getattr(self.page, "store", None)
        if store is None or not self.restored:  # (before the first read: what the last run kept is not lost)
            return
        store.set(SAVED, [{"key": panel.instance_key, "name": panel.name} for panel in self.panels()
                          if isinstance(panel.instance_key, str) and panel.ended_at is None][:SAVED_LIMIT])

    def restore(self, instances):
        """The browser tabs open when the app ended, again - each only if its start still runs here with a browser
        that shows a display (vnc_view.why_not); the others are left out and logged."""
        store = getattr(self.page, "store", None)
        saved = store.get(SAVED, []) if store is not None else []
        saved = [entry for entry in saved if isinstance(entry, dict) and isinstance(entry.get("key"), str)
                 ][:SAVED_LIMIT] if isinstance(saved, list) else []
        if not saved:
            self.save()  # (a browser opened before the first read is kept now)
            return
        opened, skipped = [], []
        for entry in saved:
            instance = instances.get(entry["key"])
            why = vnc_view.why_not(instance) if instance is not None else tr("It does not run.")
            if why is None and instance.state == "running":
                self.open(instance, activate=False)
                opened.append(printable(instance.name))
            else:
                skipped.append(f"{printable(str(entry.get('name') or entry['key']))} ({why or tr('It does not run.')})")
        applog.log("INFO", "browser", "browser tabs of the app's last run: reopened " + (", ".join(opened) or "none")
                   + (f"; not reopened: {'; '.join(skipped)}" if skipped else ""))
        self.save()
