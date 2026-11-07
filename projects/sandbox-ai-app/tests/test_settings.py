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
