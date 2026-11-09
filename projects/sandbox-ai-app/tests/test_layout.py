"""No clipped text: every page and the main dialogs, in both languages and three themes (light, dark and the
largest font), at the window's minimum size. A button or a one-line label narrower than its text, or a wrapping
label shorter than its text needs, is a finding. The Settings dialog (and its tab of the administrator's policy) is
also checked on a small laptop's screen: no text beyond the room of its tab, every tab title whole."""
import json
import time
from pathlib import Path

import pytest

QtCore = pytest.importorskip("PySide6.QtCore", reason="PySide6 is not installed")
QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, i18n, settings, theme, ui, vpn_page, widgets, wizard  # noqa: E402

from test_ui import env  # noqa: E402,F401 - the isolated home of the UI tests

KINDS = (QtWidgets.QLabel, QtWidgets.QPushButton, QtWidgets.QCheckBox, QtWidgets.QRadioButton, QtWidgets.QToolButton)


def clipped(top):
    found = []
    for kind in KINDS:
        for w in top.findChildren(kind):
            if not w.isVisibleTo(top) or not w.text().strip() or getattr(w, "elides", False):
                continue  # (an ElidedLabel cuts a long line on purpose, whole in its tooltip)
            if (isinstance(w, QtWidgets.QLabel) and w.wordWrap()) or getattr(w, "wraps", False):
                if w.heightForWidth(w.width()) > w.height() + 2:
                    found.append(f"{type(w).__name__} {w.text()[:60]!r}: needs {w.heightForWidth(w.width())} px high, has {w.height()}")
            elif w.width() > 0 and w.sizeHint().width() > w.width() + 2:
                found.append(f"{type(w).__name__} {w.text()[:60]!r}: needs {w.sizeHint().width()} px wide, has {w.width()}")
    return found


@pytest.mark.parametrize("language", ["en", "de"])
@pytest.mark.parametrize("mode", ["light", "dark", "win311"])
def test_no_text_is_clipped_at_the_smallest_window(qapp, env, monkeypatch, language, mode):  # noqa: F811
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    i18n.set_language(language)
    try:
        theme.apply(mode, qapp)
        sai = backend.SandboxAI()
        window = ui.MainWindow(sai)
        window.resize(window.minimumSizeHint())
        window.show()
        qapp.processEvents()
        found = []
        for key, _ in ui.PAGES:
            window.show_page(key, raise_window=False)
            qapp.processEvents()
            found += [f"page {key}: {item}" for item in clipped(getattr(window, key))]
        dialog = settings.SettingsDialog(sai, window)
        dialog.show()
        for index in range(dialog.tabs.count()):
            dialog.tabs.setCurrentIndex(index)
            qapp.processEvents()
            found += [f"settings {dialog.tabs.tabText(index)}: {item}" for item in clipped(dialog)]
        dialog.close()
        flow = wizard.SandboxWizard(sai, lambda: {}, window)
        flow.show()
        for page in flow.pageIds():
            flow.setStartId(page)
            flow.restart()
            qapp.processEvents()
            found += [f"wizard page {page}: {item}" for item in clipped(flow)]
        flow.close()
        vpn = vpn_page.VpnDialog(window)
        vpn.show()
        qapp.processEvents()
        found += [f"vpn dialog: {item}" for item in clipped(vpn)]
        vpn.close()
        window.close()
        assert found == [], "\n".join(found)
    finally:
        i18n.set_language("en")
        theme.apply("system", qapp)


# The widest the window's minimum may be after every page was shown (review round 5, H1: the Hosts page alone made it
# 1726 px wide in English and 1915 px in German, and Qt never shrinks a window back).
WIDEST, WIDEST_COMPACT = 1024, 860
WIDEST_PAGE = 900  # what a page itself needs at most before it scrolls sideways


@pytest.mark.parametrize("language", ["en", "de"])
@pytest.mark.parametrize("mode", ["light", "dark"])
@pytest.mark.parametrize("compact", [False, True])
def test_every_page_lets_the_window_shrink_back(qapp, env, monkeypatch, language, mode, compact):  # noqa: F811
    """Every page shown once, the window still shrinks to a size that fits a small laptop screen, and no text is cut
    off there: a page wider than the room scrolls sideways (PageScroll) instead of widening the window."""
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    i18n.set_language(language)
    try:
        theme.apply(mode, qapp)
        window = ui.MainWindow(backend.SandboxAI())
        window.show()
        window.set_compact_nav(compact)
        found = []
        for key, _ in ui.PAGES:
            window.show_page(key, raise_window=False)
            qapp.processEvents()
            page = getattr(window, key)
            if page.minimumSizeHint().width() > WIDEST_PAGE:
                found.append(f"page {key} needs {page.minimumSizeHint().width()} px")
            if window.pages.minimumSizeHint().width() > ui.SCROLL_MIN_WIDTH and key not in ui.UNSCROLLED_PAGES:
                found.append(f"page {key} asks the window for {window.pages.minimumSizeHint().width()} px")
        for key, _ in ui.PAGES:  # back on each page after all were shown: the window shrinks to its minimum
            window.show_page(key, raise_window=False)
            for _ in range(2):  # the page's own minimum is known once it is laid out
                qapp.processEvents()
                window.resize(window.minimumSizeHint())
            qapp.processEvents()
            limit = WIDEST_COMPACT if compact else WIDEST
            if window.width() > limit:
                found.append(f"page {key}: the window stays {window.width()} px wide (at most {limit}; its minimum "
                             f"{window.minimumSizeHint().width()}, the page asks {window.pages.minimumSizeHint().width()})")
            found += [f"page {key} at {window.width()} px: {item}" for item in clipped(getattr(window, key))]
        window.shutdown()  # timers, commands and terminals stop: many windows in one run
        window.close()
        window.deleteLater()
        qapp.processEvents()
        assert found == [], "\n".join(found)
    finally:
        i18n.set_language("en")
        theme.apply("system", qapp)


def beyond_view(top):
    """Texts below TOP that reach past the right edge of the scroll area they are in (the tab scrolls sideways)."""
    found = []
    for kind in KINDS:
        for w in top.findChildren(kind):
            if not w.isVisibleTo(top) or not w.text().strip():
                continue
            area = w.parentWidget()
            while area is not None and not isinstance(area, QtWidgets.QScrollArea):
                area = area.parentWidget()
            if area is None or not area.isVisibleTo(top):
                continue
            right = w.mapTo(area.viewport(), QtCore.QPoint(min(w.width(), w.sizeHint().width()), 0)).x()
            if right > area.viewport().width() + 2:
                beyond = right - area.viewport().width()
                found.append(f"{type(w).__name__} {w.text()[:60]!r}: {beyond} px beyond the view")
    return found


LAPTOP = (1366, 768)  # a small laptop's screen; the room for a window is its height less a panel
POLICY = {"forbid": ["--egress-all", "--no-proxy", "--device=/dev/kvm", "--gpu", "--share-ssh-agent", "--mount-home"],
          "backends": ["podman"], "max_approval_scope": "session",
          "allow_within": ["10.0.0.0/8", "192.168.0.0/16", "api.example.org"],
          "allow_hosts": ["api.anthropic.com", "api.openai.com", "github.com", "registry.npmjs.org", "pypi.org"]}


@pytest.mark.parametrize("language", ["en", "de"])
@pytest.mark.parametrize("mode", ["light", "dark"])
def test_the_settings_and_the_policy_fit_a_small_laptop(qapp, env, monkeypatch, language, mode):  # noqa: F811
    """Settings and the administrator's policy (Settings > Administrator policy, opened by its menu item), at their
    size at first and at their smallest: every tab title whole, no text cut or beyond the tab's room (a long check box
    text wraps, a long label goes above its field), and the dialog fits a 1366x768 screen."""
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(settings, "screen_room", lambda _widget: QtCore.QSize(LAPTOP[0], LAPTOP[1] - 40))
    i18n.set_language(language)
    try:
        theme.apply(mode, qapp)
        sai = backend.SandboxAI()
        problem = "the file /etc/sandbox-ai/policy.json must belong to root and be writable by root only (owner 1000)"
        monkeypatch.setattr(sai, "policy", lambda: (POLICY, problem, json.dumps(POLICY, indent=2),
                                                    Path("/etc/sandbox-ai/policy.json")))
        dialog = settings.SettingsDialog(sai)
        dialog.show_policy()  # as the menu item opens it
        dialog.show()
        deadline = time.monotonic() + 30  # the preset directories and trusted projects are read in the background
        while time.monotonic() < deadline and any(widgets.busy(w) for w in (dialog.preset_dirs, dialog.trusted)):
            qapp.processEvents()
            time.sleep(0.02)
        qapp.processEvents()
        assert dialog.tabs.currentIndex() == dialog.tabs.count() - 1
        found = []
        for size in ("at first", "smallest"):
            if size == "smallest":
                dialog.resize(dialog.minimumSize())
            assert dialog.width() <= LAPTOP[0] and dialog.height() <= LAPTOP[1] - 40, (size, dialog.size())
            for index in range(dialog.tabs.count()):
                dialog.tabs.setCurrentIndex(index)
                for _ in range(3):  # (a text that came in the background is laid out at the next pass)
                    qapp.processEvents()
                where = f"{size}, tab {dialog.tabs.tabText(index)!r}"
                found += [f"{where}: {item}" for item in clipped(dialog) + beyond_view(dialog)]
                if dialog.tabs.widget(index).horizontalScrollBar().isVisible():
                    found.append(f"{where}: scrolls sideways")
                bar = dialog.tabs.tabBar()
                for tab in range(bar.count()):
                    rect = bar.tabRect(tab)
                    whole = bar.tabSizeHint(tab).width()
                    if rect.left() < 0 or rect.right() >= bar.width() + 1 or rect.width() < whole:
                        found.append(f"{where}: the title {bar.tabText(tab)!r} is cut ({rect} in {bar.width()} px)")
        dialog.close()
        assert found == [], "\n".join(found)
    finally:
        i18n.set_language("en")
        theme.apply("system", qapp)
