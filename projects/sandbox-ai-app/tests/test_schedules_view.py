"""Schedules on the Sessions page (schedules_view.py) end to end, offscreen: the MainWindow's Sessions page with the
real `sandbox-ai schedule` of this checkout (SANDBOX_AI_HOME) - a schedule added through its dialog (checked as the
command checks it, asked first), listed, paused and resumed, its history with a run's report, changed (Edit), removed
(asked first) - the right-click menu carrying the buttons, and the timer asked first (its command faked here: no
systemd unit is installed by a test)."""
import json
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, widgets  # noqa: E402

from conftest import wait_until  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    (tmp_path / "ws").mkdir()
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    widgets.SHOWN.clear()
    return tmp_path


def test_schedules_added_listed_paused_with_history_and_removed(qapp, env, monkeypatch):
    from sandbox_ai_app import schedules_view, terminal
    from sandbox_ai_app.ui import MainWindow
    monkeypatch.setattr(terminal, "available", lambda web=None: (False, "x"))
    w = MainWindow(backend.SandboxAI(), check_updates=False, approvals_interval=1000)
    try:
        from PySide6.QtGui import QShowEvent
        box = w.built("sandboxes").schedules_box
        assert not box.read_once and "Asking" not in box.status.text(), "nothing is read before the box is shown"
        box.showEvent(QShowEvent())  # (shown: it reads its host's schedules, once)
        assert box.read_once
        assert wait_until(qapp, lambda: "No schedules on" in box.status.text(), 60), box.status.text()
        assert box.timer_label.text() == "Timer: off - nothing starts by itself"

        def fill_in(dialog):
            dialog.name.setText("nightly")
            dialog.when.setCurrentIndex(dialog.when.findData("cron"))
            dialog.when_value.setText("61 * * * *")
            dialog.agent.setCurrentIndex(dialog.agent.findData("codex"))
            dialog.prompt.setPlainText("Review yesterday's commits")
            dialog.workdir.setText(str(env / "ws"))
            dialog.save()
            assert not dialog.problem.isHidden() and "not within 0-59" in dialog.problem.text()
            dialog.when_value.setText("0 3 * * 1-5")
            dialog.limit.setText("30m")
            dialog.notify.setChecked(False)
            dialog.save()
            assert dialog.result() == QtWidgets.QDialog.DialogCode.Accepted
            return QtWidgets.QDialog.DialogCode.Accepted
        monkeypatch.setattr(schedules_view.ScheduleDialog, "exec", fill_in)
        box.add()
        assert wait_until(qapp, lambda: box.list.rowCount() == 1, 60), box.status.text()
        assert [box.list.item(0, c).text() for c in (0, 1, 2, 3)] == ["nightly", "cron 0 3 * * 1-5", "codex", "off"]
        stored = json.loads((env / "config" / "sandbox-ai" / "schedules" / "nightly.json").read_text())
        assert stored["prompt"] == "Review yesterday's commits" and stored["limit"] == 1800 and stored["ask"] == "off"
        assert stored["notify"] is False and stored["workdir"] == str(env / "ws")

        # the buttons are on the right-click menu too
        box.list.selectRow(0)
        entries = [b for b in box.list.menu_entries() if b is not None]
        assert entries[:5] == [box.buttons_by[k] for k in ("run", "edit", "history", "pause", "resume")]
        assert box.buttons_by["remove"] in entries and box.timer_button in entries

        box.pause()
        assert wait_until(qapp, lambda: box.list.rowCount() == 1 and box.list.item(0, 6).text() == "paused", 60)
        box.list.selectRow(0)
        assert box.buttons_by["resume"].isEnabled() and not box.buttons_by["pause"].isEnabled()
        box.resume()
        assert wait_until(qapp, lambda: box.list.rowCount() == 1 and box.list.item(0, 6).text() == "active", 60)

        # a run in its history (as a run writes it) and its report
        sched = box.sai.module("schedule")
        run_dir = sched.private_dir(sched.state_dir("nightly") / "runs" / "20261117-030000-abcd")
        sched.write_private(run_dir / "summary.md", "# Scheduled run nightly 20261117-030000-abcd\n\nall well\n")
        sched.record("nightly", {"run": "20261117-030000-abcd", "reason": "due", "started": 1.79e9, "seconds": 42,
                                 "status": "ok", "summary": "runs/20261117-030000-abcd/summary.md",
                                 "changes": {"files": 2, "insertions": 3, "deletions": 1, "commits": 0}})
        box.list.selectRow(0)
        box.history()
        assert wait_until(qapp, lambda: getattr(box, "history_dialog", None) is not None, 60)
        dialog = box.history_dialog
        assert dialog.list.rowCount() == 1 and dialog.list.item(0, 5).text() == "2 file(s), 0 commit(s)"
        assert dialog.list.menu_entries() == [dialog.report_button]
        dialog.report()
        assert wait_until(qapp, lambda: any("all well" in str(text) for _title, text, _ in widgets.SHOWN), 60)
        assert any(title == "Report of 20261117-030000-abcd" for title, _text, _ in widgets.SHOWN)
        dialog.close()

        # Edit: the dialog filled in, saved under the same name (--replace)
        def change(dialog):
            assert dialog.name.isReadOnly() and dialog.name.text() == "nightly"
            assert dialog.prompt.toPlainText() == "Review yesterday's commits" and dialog.limit.text() == "30m"
            dialog.ask.setCurrentIndex(dialog.ask.findData("allow"))
            dialog.save()
            return QtWidgets.QDialog.DialogCode.Accepted
        monkeypatch.setattr(schedules_view.ScheduleDialog, "exec", change)
        box.list.selectRow(0)
        box.edit()
        assert wait_until(qapp, lambda: box.list.rowCount() == 1 and box.list.item(0, 3).text() == "allow", 60)

        # the timer: asked first, its command faked
        asked = []
        monkeypatch.setattr(box.sai, "schedule_timer", lambda host, on: asked.append((host, on)) or (0, "", ""))
        box.toggle_timer()
        assert wait_until(qapp, lambda: asked == [("local", True)], 60)

        box.list.selectRow(0)
        box.remove()
        assert wait_until(qapp, lambda: box.list.rowCount() == 0 and "No schedules on" in box.status.text(), 60)
        assert not (env / "config" / "sandbox-ai" / "schedules" / "nightly.json").exists()
    finally:
        w.shutdown()


def test_a_host_whose_sandbox_ai_has_no_schedules_or_channels_says_update_it(qapp, env):
    """Review 3x-2: the Schedules box and the phone channels on a host with an older sandbox-ai (0.1.98 says
    "unknown command 'schedule'") showed that error; they say what helps instead: update that host."""
    from sandbox_ai_app.notify_view import PhoneChannels
    from sandbox_ai_app.schedules_view import SchedulesBox
    old = ValueError("sandbox-ai: unknown command 'schedule'; see sandbox-ai --help")
    box = SchedulesBox(backend.SandboxAI())
    box.loaded(box.token, "lab", None, old)
    assert "has no schedules yet" in box.status.text() and "update" in box.status.text()
    assert "unknown command" not in box.status.text()
    box.loaded(box.token, "lab", None, ValueError("no answer within 60 s"))
    assert box.status.text() == "Cannot list the schedules: no answer within 60 s"
    channels = PhoneChannels(backend.SandboxAI())
    channels.loaded(channels.token, "lab", None, ValueError("sandbox-ai: unknown command 'notify'; see sandbox-ai "
                                                            "--help"))
    assert "has no phone notifications yet" in channels.status.text() and "update" in channels.status.text()
