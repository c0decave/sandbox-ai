"""Duplicate…, Export with state… and Import… of the Sandboxes page, and the state options of Copy/Move to host…,
offscreen against stand-ins that record what sandbox-ai was asked (nothing is copied, imported or started): the words
of each command, what is asked first (logins and keys along: session-export-secrets; network approvals granted again:
session-grant-approvals; an import's configuration that gives the sandbox secrets or folders: session-import), an
import checked before Import is offered, and what each says shown."""
import json
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, widgets  # noqa: E402

from test_accessibility import unnamed  # noqa: E402
from test_session_transfer import HOSTS, at_once, page_on, session  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


@pytest.fixture
def sai(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    sai = backend.SandboxAI()
    sai.calls, sai.answer = [], (0, "{}", "")

    def call(*args, timeout=120, input=None, argv=None):
        sai.calls.append(args)
        return sai.answer
    monkeypatch.setattr(sai, "call", call)
    monkeypatch.setattr(sai, "hosts_supported", lambda: True)
    monkeypatch.setattr(sai, "host_entries", lambda: [dict(h) for h in HOSTS])
    return sai


@pytest.fixture
def ui(qapp, sai, monkeypatch):
    from sandbox_ai_app import session_state_dialogs, session_transfer, ui as module
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    asked = []

    def ask_first(parent, action, title, text, accept, detail=None, details=None):
        if backend.needs_confirm(action, detail) is None:
            return True
        asked.append((action, text))
        return module.answer_yes
    for target in (module, session_transfer, session_state_dialogs):
        monkeypatch.setattr(target, "ask_first", ask_first)
        monkeypatch.setattr(target, "background", at_once)
    module.asked, module.answer_yes = asked, True
    return module


IMPORTED = {"name": "web-2", "agent": "codex", "options": ["--no-config", "--workspace", "/srv/w"], "cwd": "/srv/w",
            "workspace": "/srv/w", "checked": True, "imported": True, "asked": [], "warnings": [],
            "approvals": ["--allow-host pypi.org"], "granted": [], "memory": {"added": 2, "kept_here": []},
            "checkpoints": {"ids": ["20261121-000000-aaaaaa"]}, "from": {"name": "web", "sandbox_ai": "0.1.193"}}


def test_the_words_of_state_and_import():
    assert backend.state_words(None) == [] and backend.state_words(False) == ["--config-only"]
    assert backend.state_words(True) == []
    assert backend.state_words(True, with_workspace=True, include_secrets=True, grant=True) == [
        "--with-workspace", "--include-secrets", "--grant-approvals", "--yes"]
    assert backend.session_transfer_args("move", "web", "ct1", state=False) == [
        "session", "move", "web", "ct1", "--config-only"]
    assert backend.session_import_args("/tmp/a.tar.gz", "web", [("/home/me/w", "/srv/w"), ("/x", "/x")], check=True) == [
        "session", "import", "--json", "--as", "web", "--map=/home/me/w=/srv/w", "--check", "--", "/tmp/a.tar.gz"]
    for bad in (("relative.tar.gz",), ("/a.tar.gz", "../x"), ("/a.tar.gz", None, [("/a=b", "/c")]),
                ("/a.tar.gz", None, [("/a", "rel")])):
        with pytest.raises(ValueError):
            backend.session_import_args(*bad)


def test_the_page_offers_duplicate_export_and_import(ui, sai, monkeypatch):
    for instance, enabled in ((session(), True), (session(host="ct1"), False), (None, False)):
        page = page_on(ui, sai, monkeypatch, instance)
        page.update_buttons()
        for key in ("duplicate", "export-state"):
            assert page.transfer_buttons[key].isEnabled() == enabled, (key, page.transfer_buttons[key].toolTip())
        assert page.transfer_buttons["import"].isEnabled(), "an archive of anywhere: no start to select"
        page.close()


def test_duplicate_asks_for_logins_and_approvals_and_shows_what_it_made(ui, sai, monkeypatch):
    from sandbox_ai_app.session_state_dialogs import DuplicateDialog
    made = []
    dialog = DuplicateDialog(None, sai, "web", succeeded=lambda: made.append(1))
    assert unnamed(dialog) == [] and dialog.new_name.text() == "web-2"
    sai.answer = (0, json.dumps(IMPORTED), "")
    dialog.with_workspace.setChecked(True)
    dialog.go_button.click()
    assert sai.calls[-1] == ("session", "duplicate", "web", "web-2", "--json", "--with-workspace")
    assert ui.asked == [] and made == [1]
    text = dialog.output.toPlainText()
    assert "Made: web-2" in text and "--allow-host pypi.org" in text and "Checkpoints: 1" in text
    dialog = DuplicateDialog(None, sai, "web")
    dialog.with_secrets.setChecked(True)
    dialog.grant.setChecked(True)
    ui.answer_yes = False
    dialog.go_button.click()
    assert [a for a, _ in ui.asked] == ["session-export-secrets"] and len(sai.calls) == 1, "said no: nothing runs"
    ui.answer_yes = True
    dialog.go_button.click()
    assert [a for a, _ in ui.asked][-2:] == ["session-export-secrets", "session-grant-approvals"]
    assert sai.calls[-1][-3:] == ("--include-secrets", "--grant-approvals", "--yes")
    assert not dialog.go_button.isEnabled(), "made: not a second time"
    dialog = DuplicateDialog(None, sai, "web")
    sai.answer = (1, "", "sandbox-ai session: there is a session web-2 here already: choose another name\n")
    dialog.go_button.click()
    assert "Not duplicated" in dialog.status.text() and "already" in dialog.output.toPlainText()


def test_import_is_checked_first_and_asks_what_its_configuration_gives(ui, sai, monkeypatch, tmp_path):
    from sandbox_ai_app.session_state_dialogs import ImportDialog
    archive = tmp_path / "web.tar.gz"
    archive.write_bytes(b"x")
    dialog = ImportDialog(None, sai, path=str(archive))
    assert unnamed(dialog) == []
    assert not dialog.go_button.isEnabled(), "checked first"
    dialog.maps.setPlainText("/home/me/w=/srv/w\n")
    checked = {**IMPORTED, "imported": False, "asked": ["--api-key x=Y: gives the sandbox a secret or login"]}
    sai.answer = (0, json.dumps(checked), "")
    dialog.check_button.click()
    assert sai.calls[-1] == ("session", "import", "--json", "--map=/home/me/w=/srv/w", "--check", "--", str(archive))
    assert dialog.go_button.isEnabled() and "It can start here" in dialog.output.toPlainText()
    assert "Asked first: --api-key" in dialog.output.toPlainText()
    dialog.grant.setChecked(True)
    sai.answer = (0, json.dumps(IMPORTED), "")
    dialog.go_button.click()
    assert [a for a, _ in ui.asked] == ["session-import", "session-grant-approvals"]
    assert "--api-key" in ui.asked[0][1]
    assert sai.calls[-1][-5:] == ("--map=/home/me/w=/srv/w", "--grant-approvals", "--yes", "--", str(archive))
    assert "Done: web-2 is made." in dialog.status.text()
    dialog.new_name.setText("other")
    assert not dialog.go_button.isEnabled(), "another choice: checked again first"
    sai.answer = (1, "", "sandbox-ai session: evil/home/.bashrc is a link: an export holds only folders and regular "
                         "files\n")
    dialog.check_button.click()
    assert "is a link" in dialog.output.toPlainText() and not dialog.go_button.isEnabled()


def test_export_with_state_writes_a_new_file_only(ui, sai, monkeypatch, tmp_path):
    from sandbox_ai_app.session_state_dialogs import StateExportDialog
    dialog = StateExportDialog(None, sai, "web")
    assert unnamed(dialog) == []
    (tmp_path / "there.tar.gz").write_text("mine")
    dialog.path.setText(str(tmp_path / "there.tar.gz"))
    dialog.go_button.click()
    assert sai.calls == [] and "is there already" in dialog.status.text()
    dialog.path.setText(str(tmp_path / "new.tar.gz"))
    dialog.with_workspace.setChecked(True)
    sai.answer = (0, json.dumps({"path": str(tmp_path / "new.tar.gz"), "files": 7, "bytes": 2048,
                                 "left_out": [{"path": ".claude/.credentials.json", "why": "an agent's login",
                                               "secret": True}]}), "")
    dialog.go_button.click()
    assert sai.calls[-1] == ("session", "export", "web", "--json", "--with-workspace", "--", str(tmp_path / "new.tar.gz"))
    assert "7 files" in dialog.status.text() and ".claude/.credentials.json" in dialog.output.toPlainText()


def test_copy_with_state_by_default_or_only_its_configuration(ui, sai, monkeypatch):
    from sandbox_ai_app.session_transfer import SessionTransferDialog
    monkeypatch.setattr(sai, "session_transfer_plan", lambda name: (["/srv/w"], []))
    dialog = SessionTransferDialog(None, sai, "web")
    assert dialog.state.isChecked() and dialog.state.isVisibleTo(dialog)
    assert "with its state" in dialog.intro.text()
    dialog.host.set_host("ct1")
    dialog.with_workspace.setChecked(True)
    dialog.grant.setChecked(True)
    dialog.go_button.click()
    assert [a for a, _ in ui.asked] == ["session-grant-approvals"]
    assert sai.calls[-1] == ("session", "copy", "web", "ct1", "--with-workspace", "--grant-approvals", "--yes")
    dialog = SessionTransferDialog(None, sai, "web")
    dialog.host.set_host("ct1")
    dialog.state.setChecked(False)
    assert not dialog.with_workspace.isEnabled() and "configuration starts again" in dialog.intro.text()
    dialog.go_button.click()
    assert sai.calls[-1] == ("session", "copy", "web", "ct1", "--config-only")
    # An older sandbox-ai: no state options shown, called as before.
    monkeypatch.setattr(sai, "session_state_supported", lambda: False)
    dialog = SessionTransferDialog(None, sai, "web")
    assert not dialog.state.isVisibleTo(dialog)
    dialog.host.set_host("ct1")
    dialog.go_button.click()
    assert sai.calls[-1] == ("session", "copy", "web", "ct1")
