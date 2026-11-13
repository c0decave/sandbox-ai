"""The Settings dialog and the administrator's policy in the app (offscreen)."""
import json
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, widgets  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")

POLICY = {"version": 1, "forbid": ["--egress-all-allow", "--no-proxy", "--privacy-check=off"],
          "max_approval_scope": "run", "allow_within": ["10.0.0.0/8"]}


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    return tmp_path


def with_policy(monkeypatch, sai, problem=None):
    monkeypatch.setattr(sai, "policy", lambda: (POLICY if not problem else {}, problem, json.dumps(POLICY),
                                                "/etc/sandbox-ai/policy.json"))
    monkeypatch.setattr(sai, "forbidden", lambda: {"--egress-all-allow": {None}, "--no-proxy": {None},
                                                  "--privacy-check": {"off"}})


def test_every_setting_is_saved_and_applied(qapp, env, monkeypatch):
    from sandbox_ai_app import terminal
    from sandbox_ai_app.ui import MainWindow
    monkeypatch.setattr(terminal, "available", lambda web=None: (False, "x"))
    w = MainWindow(backend.SandboxAI(), check_updates=False, approvals_interval=1000)
    try:
        dialog = w.settings_dialog()
        dialog.fields["approvals_interval"].setValue(500)
        assert w.sai.settings()["approvals_interval"] == 500 and w.approvals.timer.interval() == 500
        dialog.fields["default_agent"].setCurrentIndex(dialog.fields["default_agent"].findData("codex"))
        dialog.fields["board_grid"].setValue(40)
        dialog.fields["notify"].setChecked(False)
        settings = w.sai.settings()
        assert settings["default_agent"] == "codex" and settings["board_grid"] == 40 and settings["notify"] is False
        notes = []
        w.notify = lambda title, text, **_: notes.append(title)
        w.questions_changed(1, [("run", {"kind": "dns", "name": "x.example"})])
        assert notes == [], "no notification when they are switched off"
        dialog.fields["theme"].setCurrentIndex(dialog.fields["theme"].findData("dark"))
        from PySide6.QtWidgets import QApplication
        QApplication.processEvents()  # applied after the combo box's own event (theme.apply_soon)
        from sandbox_ai_app import theme
        assert theme.is_dark()
        dialog.fields["theme"].setCurrentIndex(dialog.fields["theme"].findData("system"))
        assert stat_mode(backend.settings_file()) == 0o600
    finally:
        w.shutdown()
        w.deleteLater()


def stat_mode(path):
    return os.stat(path).st_mode & 0o777


def test_what_needs_notifications_follows_their_switch(qapp, env):
    """With notifications off (the default) "Desktop notification for each new question" stayed ticked and could be
    changed, and nothing it says happened: the switch above it holds every popup back. It, how long they stay and how
    they are shown can be changed only while notifications are on."""
    from sandbox_ai_app.settings import SettingsDialog
    sai = backend.SandboxAI()
    dialog = SettingsDialog(sai)
    try:
        switch = dialog.fields["notifications"]
        following = [dialog.fields[key] for key in ("notify", "notify_seconds", "notify_style")]
        assert not switch.isChecked() and not any(widget.isEnabled() for widget in following)
        switch.setChecked(True)
        assert all(widget.isEnabled() for widget in following) and sai.settings()["notifications"] is True
        switch.setChecked(False)
        assert not any(widget.isEnabled() for widget in following)
    finally:
        dialog.deleteLater()


def test_preparing_a_start_promises_a_notification_only_when_there_is_one(qapp, env, monkeypatch):
    """"A notification says when it is done": with notifications off (the default) none came - the page's notes say
    it then."""
    from sandbox_ai_app import prerequisites
    sai = backend.SandboxAI()
    assert prerequisites.done_said() == "The notes here say when it is done."
    sai.save_setting("notifications", True)
    assert prerequisites.done_said() == "A notification says when it is done."


def test_fonts_are_chosen_installed_or_marked_with_how_to_get_them(qapp, env, monkeypatch):
    """Appearance: the theme's font or one of the list; one that is not installed is marked so, says how to get it
    (its tooltip, and under the box once chosen) and the theme's font stays; an installed one applies at once."""
    from PySide6.QtCore import Qt
    from sandbox_ai_app import fonts, theme
    from sandbox_ai_app.settings import SettingsDialog
    monkeypatch.setattr(fonts, "_families", ["Hack", "DejaVu Sans"])
    sai = backend.SandboxAI()
    dialog = SettingsDialog(sai)
    try:
        mono, ui = dialog.fields["mono_font"], dialog.fields["ui_font"]
        assert mono.currentData() == "" and ui.currentData() == "", "the theme's by default"
        texts = {mono.itemData(i): mono.itemText(i) for i in range(mono.count())}
        assert texts["Hack"] == "Hack" and texts["JetBrains Mono"] == "JetBrains Mono (not installed)"
        tip = mono.itemData(mono.findData("JetBrains Mono"), Qt.ItemDataRole.ToolTipRole)
        installed_tip = mono.itemData(mono.findData("Hack"), Qt.ItemDataRole.ToolTipRole) or ""
        # (an installed one's tooltip, if any, is its name alone: entries explain themselves on hover since 0.1.80)
        assert "fonts-jetbrains-mono" in tip and "install" not in installed_tip.lower()
        assert ui.findData("Inter") > 0 and ui.itemText(ui.findData("Inter")) == "Inter (not installed)"
        mono.setCurrentIndex(mono.findData("JetBrains Mono"))
        qapp.processEvents()
        assert sai.settings()["mono_font"] == "JetBrains Mono", "kept for when it is installed"
        hint = dialog.font_hints["mono"]
        assert not hint.isHidden() and "not installed" in hint.text() and "fonts-jetbrains-mono" in hint.text()
        assert widgets.MONO.families() == ["monospace"], "the theme's font until then"
        mono.setCurrentIndex(mono.findData("Hack"))
        qapp.processEvents()
        assert sai.settings()["mono_font"] == "Hack" and hint.isHidden()
        assert widgets.MONO.families() == ["Hack", "monospace"]
        assert json.loads(theme.terminal_theme())["fontFamily"].startswith("Hack, ")
        mono.setCurrentIndex(0)
        qapp.processEvents()
        assert sai.settings()["mono_font"] == "" and widgets.MONO.families() == ["monospace"]
        before = qapp.font().families()
        ui.setCurrentIndex(ui.findData("DejaVu Sans"))
        assert sai.settings()["ui_font"] == "DejaVu Sans" and qapp.font().families()[0] == "DejaVu Sans"
        ui.setCurrentIndex(0)
        assert qapp.font().families() == before, "the theme's font again"
    finally:
        monkeypatch.setattr(fonts, "_families", None)
        fonts.use({})
        theme.apply("system")


def test_the_policy_is_shown_and_greyed_out_in_the_form(qapp, env, monkeypatch):
    from sandbox_ai_app.settings import SettingsDialog
    from sandbox_ai_app.ui import LaunchPage
    sai = backend.SandboxAI()
    with_policy(monkeypatch, sai)
    dialog = SettingsDialog(sai)
    dialog.show_policy()
    texts = [label.text() for label in dialog.policy_page.findChildren(QtWidgets.QLabel)]
    assert any("--egress-all-allow" in t for t in texts) and any("10.0.0.0/8" in t for t in texts)
    view = dialog.policy_page.findChildren(QtWidgets.QPlainTextEdit)[0]
    assert view.isReadOnly() and '"max_approval_scope": "run"' in view.toPlainText()
    page = LaunchPage(sai, lambda *a, **k: None)
    assert not page.form.widgets["--egress-all-allow"].isEnabled() and not page.form.widgets["--no-proxy"].isEnabled()
    privacy = page.form.widgets["--privacy-check"]
    assert not privacy.model().item(privacy.findText("off")).isEnabled()
    assert not page.form.widgets["--profile"].boxes["egress-all"].isEnabled()
    assert page.form.widgets["--allow-pypi"].isEnabled()


def test_a_broken_policy_is_said_plainly(qapp, env, monkeypatch):
    from sandbox_ai_app.settings import SettingsDialog
    sai = backend.SandboxAI()
    with_policy(monkeypatch, sai, problem="Policy /etc/sandbox-ai/policy.json must be owned by root")
    dialog = SettingsDialog(sai)
    texts = [label.text() for label in dialog.policy_page.findChildren(QtWidgets.QLabel)]
    assert any("stops every start" in t and "owned by root" in t for t in texts)


def test_the_agents_telemetry_is_chosen_for_every_start(qapp, env, monkeypatch):
    """Privacy: the level goes into sandbox-ai's own defaults file (other keys stay, mode 0600); off is the
    default and removes the key; a broken file is shown, never rewritten; an older sandbox-ai offers nothing."""
    sai = backend.SandboxAI()
    if not sai.telemetry_levels():
        pytest.skip("this sandbox-ai has no --telemetry")
    from sandbox_ai_app.settings import SettingsDialog
    path = sai.sandbox_defaults_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "mode": "high"}))
    dialog = SettingsDialog(sai)
    field = dialog.fields["telemetry"]
    assert field.isEnabled() and field.currentData() == "off"
    field.setCurrentIndex(field.findData("strict"))
    assert json.loads(path.read_text()) == {"version": 1, "mode": "high", "telemetry": "strict"}
    assert stat_mode(path) == 0o600
    sai.launcher.load_user_defaults(sai.launcher.parse_args(["claude"]))  # sandbox-ai accepts the file
    field.setCurrentIndex(field.findData("off"))
    assert json.loads(path.read_text()) == {"version": 1, "mode": "high"}
    with pytest.raises(ValueError, match="no default"):
        sai.save_sandbox_default("telemetry", "maybe")
    path.write_text("{broken")
    dialog = SettingsDialog(sai)
    assert not dialog.fields["telemetry"].isEnabled()
    with pytest.raises(ValueError):
        sai.save_sandbox_default("telemetry", "strict")
    assert path.read_text() == "{broken"
    monkeypatch.setattr(type(sai), "telemetry_levels", lambda self: ())
    path.unlink()
    assert not SettingsDialog(sai).fields["telemetry"].isEnabled()


def test_extra_preset_directories_are_managed_through_sandbox_ai(qapp, env, monkeypatch, wait):
    """Settings > sandbox-ai: the extra preset directories, listed with what each gives or why it is skipped;
    Add folder… and Remove run `sandbox-ai presets dirs`, whose refusal is shown as it is."""
    from sandbox_ai_app import settings
    calls, listing = [], {"dirs": [{"path": "/srv/presets", "state": "used", "why": None, "presets": 3},
                                   {"path": "/work/p", "state": "skipped", "why": "it lies in /work", "presets": 0}],
                          "yours": "/home/u/.config/sandbox-ai/presets"}

    class Sai:
        def has_preset_dirs(self):
            return True

        def preset_dirs(self):
            return listing

        def preset_dir_command(self, action, path):
            calls.append((action, path))
            return (1, "", "sandbox-ai presets: /tmp/x cannot hold presets: it lies in /tmp") if path == "/tmp/x" \
                else (0, f"preset directory {path} {action}ed", "")
    box = settings.PresetDirs(Sai())
    assert wait(lambda: box.list.count() == 2, 10)
    assert box.list.item(0).text() == "/srv/presets  (3 presets)" and "skipped: it lies in /work" in box.list.item(1).text()
    shown = box.status.text().replace("\u200b", "")  # (a long path may break after each "/")
    assert "/home/u/.config/sandbox-ai/presets" in shown and not box.remove_button.isEnabled()
    monkeypatch.setattr("sandbox_ai_app.state.run_dialog", lambda dialog: ["/srv/more"])
    box.add()
    assert wait(lambda: calls == [("add", "/srv/more")], 10)
    assert wait(lambda: box.add_button.isEnabled(), 10)
    box.list.setCurrentRow(0)
    assert box.remove_button.isEnabled()
    box.remove()
    assert wait(lambda: calls[-1] == ("remove", "/srv/presets"), 10)
    assert wait(lambda: box.add_button.isEnabled(), 10)
    monkeypatch.setattr("sandbox_ai_app.state.run_dialog", lambda dialog: ["/tmp/x"])
    box.add()
    assert wait(lambda: widgets.SHOWN and "cannot hold presets" in widgets.SHOWN[-1][1], 10)
    # A sandbox-ai without the command: said, nothing to press.
    older = type("Old", (), {"has_preset_dirs": lambda self: False})()
    box = settings.PresetDirs(older)
    assert "update it" in box.status.text() and not box.add_button.isEnabled()


def test_preset_dir_commands_are_checked_before_they_run(env):
    sai = backend.SandboxAI()
    assert sai.preset_dir_command("delete", "/x")[0] == 2 and sai.preset_dir_command("add", "relative")[0] == 2
    assert sai.has_preset_dirs()
