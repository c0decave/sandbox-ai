"""Settings › Updates (updates_view.py): this machine's update mode, the last check and what it found, Set… (asked
first), Check now, Install now… and Roll back… (asked first; what runs is named and asked about once more), "Restart the
app" offered afterwards; the modes a copy cannot have are greyed. The window at its start follows the mode (off: no
check; a newer release the last check found: offered at once; a day since the last check: checked). The Hosts page
shows each host's mode and sets it (Update mode…, also on the right-click menu)."""
import os
from types import SimpleNamespace
import time

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, hosts_page, updates_view  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")
BUSY = ("sandbox-ai update: not now, this installation is in use: brisk-otter runs; the MCP server mcp-files runs "
        "(stop them first, or --while-running: they go on with the version they started with)\n")


def status(**changes):
    base = {"mode": "notify", "chosen": False, "window": "03:00-05:00", "installed": "1.0.0", "kind": "user",
            "self_updating": True, "manual": None, "refused": {}, "last_check": time.time() - 60,
            "result": {"installed": "1.0.0", "available": "9.9.9", "signature": "good", "newer": True},
            "next_check": time.time() + 86000, "downloaded": None, "waiting": None, "last_install": None,
            "rollback": "0.9.0", "timer": {"installed": True, "active": True}}
    return {**base, **changes}


class FakeSai:
    def __init__(self, data, answers=()):
        self.data, self.answers, self.calls = data, list(answers), []

    def has_update_mode(self):
        return True

    def update_status(self, host="local"):
        return self.data

    def set_update_mode(self, mode, window=None, host="local"):
        self.calls.append(("mode", mode, window, host))
        self.data = {**self.data, "mode": mode, "chosen": True}
        return self.data

    def update_check_now(self):
        self.calls.append(("check",))
        return 0, "installed: 1.0.0\navailable: 9.9.9\n", ""

    def update_apply(self, while_running=False):
        self.calls.append(("apply", while_running))
        return self.answers.pop(0)

    def update_rollback(self, while_running=False):
        self.calls.append(("rollback", while_running))
        return self.answers.pop(0)


class InstallingSai(FakeSai):
    """A sandbox-ai with `update --install` (the copies that replace themselves)."""
    def has_update_install(self):
        return True

    def update_install(self, while_running=False):
        self.calls.append(("install", while_running))
        return self.answers.pop(0)


def test_the_state_and_the_modes_a_copy_cannot_have(qapp, wait):
    sai = FakeSai(status(downloaded="9.9.9", refused={}))
    panel = updates_view.UpdatesPanel(sai)
    assert wait(lambda: "Mode notify (the default)" in panel.state.text())
    text = panel.state.text()
    assert "9.9.9 is available (installed: 1.0.0), signed by a trusted key" in text
    assert "Downloaded and checked: 9.9.9" in text
    assert panel.install_button.isEnabled() and panel.rollback_button.isEnabled()
    pip = FakeSai(status(kind="pip", self_updating=False, manual="pipx upgrade sandbox-ai-app", rollback=None,
                         refused={"download": "a pip installation ...", "auto": "a pip installation ..."}))
    panel = updates_view.UpdatesPanel(pip)
    assert wait(lambda: "pipx upgrade sandbox-ai-app" in panel.state.text())
    model = panel.mode.model()
    assert [model.item(i).isEnabled() for i in range(4)] == [True, True, False, False]
    assert not panel.rollback_button.isEnabled() and not panel.install_button.isEnabled()


def test_set_asks_first_and_sets_the_mode_with_its_window(qapp, wait, monkeypatch):
    asked = []
    monkeypatch.setattr(updates_view, "ask_first", lambda parent, action, title, text, accept, **k:
                        asked.append((action, text)) or True)
    sai = FakeSai(status())
    panel = updates_view.UpdatesPanel(sai)
    assert wait(lambda: panel.data is not None)
    panel.mode.setCurrentIndex(panel.mode.findData("auto"))
    panel.window_field.setText("02:00-04:00")
    panel.set_mode()
    assert wait(lambda: sai.calls)
    assert sai.calls == [("mode", "auto", "02:00-04:00", "local")]
    assert asked[0][0] == "update-mode" and "02:00-04:00" in asked[0][1]
    assert wait(lambda: panel.status.text() == "Saved.")


def test_install_now_names_what_runs_and_offers_the_restart(qapp, wait, monkeypatch):
    asked = []
    monkeypatch.setattr(updates_view, "ask_first", lambda parent, action, title, text, accept, **k:
                        asked.append((action, text)) or True)
    ended = []
    window = SimpleNamespace(update_ended=lambda version, status: ended.append((version, status)), update_offer=None)
    sai = FakeSai(status(downloaded="9.9.9"), answers=[(3, "", BUSY), (0, "sandbox-ai update: installed 9.9.9", "")])
    panel = updates_view.UpdatesPanel(sai, window=window)
    assert wait(lambda: panel.data is not None)
    panel.install_now()
    assert wait(lambda: ended)
    assert sai.calls == [("apply", False), ("apply", True)], "asked again once it named what runs"
    assert [a for a, _ in asked] == ["update-apply", "update-apply"]
    assert "brisk-otter runs; the MCP server mcp-files runs" in asked[1][1]
    assert ended == [("9.9.9", 0)], "Restart the app offered (the window's update_ended); never restarted by itself"


@pytest.mark.parametrize("kind", ["user", "package", "pip"])
def test_install_now_without_a_download_fetches_checks_and_installs_in_the_background(kind, qapp, wait, monkeypatch):
    """Every copy that replaces itself - install.sh's, the AppImage, the macOS app, the .pyz, a pip installation -
    installs from the panel: `sandbox-ai update --install`, no terminal."""
    asked = []
    monkeypatch.setattr(updates_view, "ask_first", lambda parent, action, title, text, accept, **k:
                        asked.append((action, text)) or True)
    ended = []
    window = SimpleNamespace(update_ended=lambda version, status: ended.append((version, status)), update_offer=None,
                             update_now=lambda: pytest.fail("no terminal for a copy that replaces itself"))
    sai = InstallingSai(status(kind=kind, install_now=True, rollback=None),
                        answers=[(3, "", BUSY), (0, "sandbox-ai update: installed 9.9.9 (was 1.0.0)", "")])
    panel = updates_view.UpdatesPanel(sai, window=window)
    assert wait(lambda: panel.data is not None)
    assert panel.install_button.isEnabled()
    panel.install_now()
    assert wait(lambda: ended)
    assert sai.calls == [("install", False), ("install", True)]
    assert asked[0][0] == "update-apply" and "Download sandbox-ai 9.9.9 now, check it" in asked[0][1]
    assert "brisk-otter runs" in asked[1][1]
    assert ended == [("9.9.9", 0)] and "installed 9.9.9" in panel.status.text()


def test_install_now_that_finds_nothing_newer_offers_no_restart(qapp, wait, monkeypatch):
    monkeypatch.setattr(updates_view, "ask_first", lambda *a, **k: True)
    ended = []
    window = SimpleNamespace(update_ended=lambda version, status: ended.append(version), update_offer=None)
    sai = InstallingSai(status(install_now=True),
                        answers=[(0, "sandbox-ai 9.9.9 is already installed and up to date", "")])
    panel = updates_view.UpdatesPanel(sai, window=window)
    assert wait(lambda: panel.data is not None)
    panel.install_now()
    assert wait(lambda: "already installed" in panel.status.text())
    assert ended == []


def test_a_copy_that_cannot_replace_itself_says_the_command_and_uses_the_windows_sudo_update(qapp, wait, monkeypatch):
    """A system installation this user cannot write: the exact command is shown; Install now… is the window's
    Update now… (sudo in a terminal) when the window offers it, and never a background install."""
    shown = []
    monkeypatch.setattr(updates_view, "warn", lambda parent, title, text: shown.append(text))
    manual = "sudo sandbox-ai update (a system installation is root's to update)"
    data = status(kind="system", self_updating=False, install_now=False, manual=manual, rollback=None,
                  refused={"download": "a system installation ...", "auto": "a system installation ..."})
    updates = []
    window = SimpleNamespace(update_offer=("1.0.0", "9.9.9", "root", "/usr/local/lib/sandbox-ai"),
                             update_now=lambda: updates.append(1), update_ended=lambda *a: None)
    sai = InstallingSai(data)
    panel = updates_view.UpdatesPanel(sai, window=window)
    assert wait(lambda: manual in panel.state.text())
    assert panel.install_button.isEnabled()
    panel.install_now()
    assert updates == [1] and sai.calls == []
    panel = updates_view.UpdatesPanel(InstallingSai(data), window=None)  # (no window: said, nothing run)
    assert wait(lambda: panel.data is not None)
    assert not panel.install_button.isEnabled()
    panel.install_now()
    assert shown == [f"This copy does not replace itself: {manual}"]
    # A package or a pip installation in a folder this user cannot write: the same, with sandbox-ai's own words.
    cannot = "/opt/apps is not writable for you; run the update as someone who may write it: sudo -H " \
             "/opt/apps/sandbox-ai-x86_64.AppImage cli update"
    panel = updates_view.UpdatesPanel(InstallingSai(status(kind="package", self_updating=False, install_now=False,
                                                           manual=cannot, rollback=None)))
    assert wait(lambda: cannot in panel.state.text())
    assert not panel.install_button.isEnabled()


def test_roll_back_asks_and_a_no_does_nothing(qapp, wait, monkeypatch):
    answers = iter([True, False])
    monkeypatch.setattr(updates_view, "ask_first", lambda *a, **k: next(answers))
    sai = FakeSai(status(), answers=[(3, "", BUSY)])
    panel = updates_view.UpdatesPanel(sai)
    assert wait(lambda: panel.data is not None)
    panel.roll_back()
    assert wait(lambda: panel.status.text() == "Not done.")
    assert sai.calls == [("rollback", False)]


def test_the_refusal_names_what_runs():
    assert backend.update_busy_reasons(3, BUSY) == ["brisk-otter runs", "the MCP server mcp-files runs"]
    assert backend.update_busy_reasons(1, BUSY) == [] and backend.update_busy_reasons(3, "other") == []


def test_check_now_shows_what_it_found_here_and_in_the_windows_status_line(qapp, wait):
    """Check now in Settings › Updates: its state says it, and the window's status line follows at once (Update
    now… there) - not only at the app's next start."""
    found = []
    window = SimpleNamespace(update_checked=lambda status, out, err: found.append((status, out, err)))
    sai = FakeSai(status(last_check=None, result=None))
    panel = updates_view.UpdatesPanel(sai, window=window)
    assert wait(lambda: panel.data is not None)
    panel.check_now()
    assert wait(lambda: found == [(0, "installed: 1.0.0\navailable: 9.9.9\n", "")])
    assert sai.calls == [("check",)] and panel.status.text() == "Checked."


@pytest.mark.parametrize("result, said", [
    ({"available": "9.9.9", "installed": "1.0.0", "signature": "good", "newer": True},
     "9.9.9 is available (installed: 1.0.0), signed by a trusted key"),
    ({"available": "9.9.9", "installed": "1.0.0", "signature": "bad", "newer": False},
     "9.9.9 is there, but its signature is BAD: not offered"),
    ({"available": "9.9.9", "installed": "1.0.0", "signature": "none", "newer": False},
     "9.9.9 is there, but it is not signed (no SHA256SUMS.sig): not offered"),
    ({"available": "9.9.9", "installed": "1.0.0", "signature": "unchecked", "newer": False},
     "9.9.9 is there, but not signed by a trusted key: not offered"),
    ({"available": "0.1.188", "installed": "0.1.198", "signature": "good", "newer": False},
     "up to date (0.1.198; the newest release is 0.1.188)"),
    ({"error": "api.github.com limits the requests to its API (HTTP 403); try again after 10:15"},
     "failed: api.github.com limits the requests to its API (HTTP 403); try again after 10:15"),
])
def test_the_last_check_in_words(result, said):
    assert updates_view.result_text(result) == said


def test_the_settings_have_an_updates_tab(qapp, monkeypatch):
    from sandbox_ai_app import help_texts, settings
    monkeypatch.setattr(updates_view.UpdatesPanel, "refresh", lambda self: None)
    dialog = settings.SettingsDialog(backend.SandboxAI())
    titles = [dialog.tabs.tabText(i) for i in range(dialog.tabs.count())]
    assert "Updates" in titles and isinstance(dialog.updates, updates_view.UpdatesPanel)
    assert help_texts.SETTINGS_TABS[titles.index("Updates")] == "settings.updates"
    dialog.deleteLater()


def test_the_window_at_its_start_follows_the_update_mode(qapp, tmp_path, monkeypatch):
    from sandbox_ai_app.ui import MainWindow
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    window = MainWindow(backend.SandboxAI(), check_updates=False, approvals_interval=0)
    try:
        checks = []
        monkeypatch.setattr(window, "check_updates", lambda: checks.append(1))
        window.update_mode_read(status(mode="off"), None)
        assert checks == [] and "off" in window.update_said
        window.update_mode_read(status(last_check=time.time() - 3600), None)
        assert checks == [] and "sandbox-ai 9.9.9 is available" in window.update_label.text(), \
            "the last check's newer release shown at once (the status line), no new check within the day"
        window.update_mode_read(status(last_check=time.time() - 2 * 86400, result=None), None)
        assert checks == [1]
    finally:
        window.close()
        window.deleteLater()


def test_the_hosts_list_shows_each_hosts_mode():
    assert hosts_page.version_text({"version": "0.1.116", "update": False, "update_mode": "auto"}) == \
        "0.1.116, up to date; updates: auto"
    assert hosts_page.version_text({"version": "0.1.115", "update": False}) == "0.1.115, up to date"
    assert hosts_page.version_text({"version": "0.1.116", "kind": "pip", "update_mode": "nonsense"}) == \
        "0.1.116 (pip)"
