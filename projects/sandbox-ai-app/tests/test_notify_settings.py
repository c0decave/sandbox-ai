"""Settings > Notifications (notify_view.py) end to end, offscreen: the MainWindow's Settings dialog, its phone
channels from the real `sandbox-ai notify` of this checkout (SANDBOX_AI_HOME) - added through the channel dialog
(asked first), listed, tested against a fake ntfy server on 127.0.0.1, removed (asked first) - and the right-click
menu carrying the buttons. No token is ever typed here: channels name secrets."""
import http.server
import json
import os
import threading

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
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    return tmp_path


@pytest.fixture
def ntfy():
    got = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            got.append(json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0))))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", got
    server.shutdown()
    server.server_close()


def test_channels_added_tested_and_removed_from_the_settings(qapp, env, monkeypatch, ntfy):
    from sandbox_ai_app import notify_view, terminal
    from sandbox_ai_app.ui import MainWindow
    url, got = ntfy
    monkeypatch.setattr(terminal, "available", lambda web=None: (False, "x"))
    w = MainWindow(backend.SandboxAI(), check_updates=False, approvals_interval=1000)
    try:
        dialog = w.settings_dialog()
        titles = [dialog.tabs.tabText(i) for i in range(dialog.tabs.count())]
        assert "Notifications" in titles
        view = dialog.phone_channels
        assert wait_until(qapp, lambda: "No phone channels" in view.status.text(), 60)

        # Add…: the dialog's fields, checked as the command checks them (a bad one stays in the dialog)
        def fill_in(channel_dialog):
            channel_dialog.name.setText("phone")
            channel_dialog.kind.setCurrentIndex(channel_dialog.kind.findData("ntfy"))
            channel_dialog.inputs[("ntfy", "url")].setText("http://ntfy.example.org")
            channel_dialog.inputs[("ntfy", "topic")].setText("e2e-topic")
            channel_dialog.save()
            assert not channel_dialog.problem.isHidden() and "http only to this machine" in channel_dialog.problem.text()
            channel_dialog.inputs[("ntfy", "url")].setText(url)
            channel_dialog.events["run-failed"].setChecked(True)
            channel_dialog.rate.setValue(0)
            channel_dialog.save()
            assert channel_dialog.result() == QtWidgets.QDialog.DialogCode.Accepted
            return QtWidgets.QDialog.DialogCode.Accepted
        monkeypatch.setattr(notify_view.ChannelDialog, "exec", fill_in)
        view.add()
        assert wait_until(qapp, lambda: view.list.rowCount() == 1, 60)
        assert view.list.item(0, 0).text() == "phone" and view.list.item(0, 2).text() == f"{url}/e2e-topic"
        assert "run-failed" in view.list.item(0, 3).text()
        settings = (env / "config" / "sandbox-ai" / "notify.json")
        assert oct(settings.stat().st_mode & 0o777) == "0o600"
        assert json.loads(settings.read_text())["channels"][0]["rate"] == 0

        # the buttons are on the right-click menu too
        view.list.selectRow(0)
        assert [b for b in view.list.menu_entries() if b is not None][:3] == \
            [view.test_button, view.edit_button, view.remove_button]

        said = []
        monkeypatch.setattr(view, "tested", lambda name, result, error, real=view.tested: (
            said.append(result), real(name, result, error)))
        view.test()
        assert wait_until(qapp, lambda: said, 60) and said[0][0] == 0
        assert len(got) == 1 and got[0]["topic"] == "e2e-topic" and "test message" in got[0]["message"]
        assert wait_until(qapp, lambda: view.list.item(0, 5) is not None and view.list.item(0, 5).text() == "sent", 60)

        view.list.selectRow(0)
        view.remove()
        assert wait_until(qapp, lambda: view.list.rowCount() == 0 and "No phone channels" in view.status.text(), 60)
        assert json.loads(settings.read_text())["channels"] == []
    finally:
        w.shutdown()
