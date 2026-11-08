"""The wizard offscreen, page by page as the buttons would go: it builds the board model, keeps typed tokens
out of the project file, honours the policy, writes and trusts the file and hands the board on."""
import json
import os
import stat

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, board as B, widgets  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


@pytest.fixture
def wizard(qapp, tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    from sandbox_ai_app.wizard import SandboxWizard
    w = SandboxWizard(backend.SandboxAI(), lambda: {"default_agent": "codex", "default_template": "development"})
    yield w
    w.deleteLater()


def walk(wizard, *pages):
    for page in pages:
        page.initializePage()
        assert page.validatePage(), page.title()


def test_a_new_project_from_a_template_to_the_board(wizard, tmp_path, wait):
    project = tmp_path / "project"
    project.mkdir()
    wizard.folder_page.path.edit.setText(str(project))
    walk(wizard, wizard.folder_page, wizard.agent_page)
    assert wizard.agent_page.agent.currentText() == "codex", "the settings' default agent"
    wizard.start_page.initializePage()
    assert wizard.start_page.chosen_preset().name == "development" and not wizard.start_page.buttons["existing"].isVisible()
    assert wizard.start_page.validatePage()
    # sandbox-ai's development preset where it has one (it replaces the app's of that id), else the app's own
    expected = "--token-proxy" if wizard.start_page.chosen_preset().source == "app" else "--allow-github"
    assert B.values(wizard.board)[0] == "codex" and expected in B.to_argv(wizard.sai, wizard.board)
    wizard.network_page.initializePage()
    assert wizard.network_page.presets["--allow-pypi"].isChecked() and wizard.network_page.presets["--allow-github"].isChecked()
    wizard.network_page.hosts.setPlainText("docs.example.org")
    assert wizard.network_page.validatePage()
    wizard.proxy_page.initializePage()
    wizard.proxy_page.rules.add_row("api.github.com", "GET,HEAD", "/**")
    assert wizard.proxy_page.validatePage()
    wizard.git_page.initializePage()
    wizard.git_page.add_row("https://github.com", "typed", "ghp_secret_value")
    assert wizard.git_page.validatePage()
    walk(wizard, wizard.mcp_page, wizard.hardware_page)
    argv = B.to_argv(wizard.sai, wizard.board)
    assert "docs.example.org" in argv and "api.github.com GET,HEAD /**" in argv
    token_file = next(v for v in argv if v.startswith("https://github.com=")).partition("=")[2]
    assert stat.S_IMODE(os.stat(token_file).st_mode) == 0o600 and open(token_file).read().strip() == "ghp_secret_value"
    assert "ghp_secret_value" not in json.dumps(wizard.document()[0]), "a typed token never goes into the project file"
    assert not str(token_file).startswith(str(project)), "kept outside the workspace"
    wizard.review_page.initializePage()
    assert "sandbox-ai codex" in wizard.review_page.command.toPlainText()
    assert wait(lambda: wizard.review_page.verdict_ok is not None, 60)
    received = []
    wizard.written.connect(lambda board, directory: received.append((board, directory)))
    assert not wizard.review_page.validatePage(), "written in the background; the wizard closes when it is done"
    assert not wizard.review_page.validatePage(), "a second Finish while it is written waits, it does not ask again"
    assert wait(lambda: received, 60)
    assert wizard.sai.review_project(project).trusted and received[0][1] == str(project)
    # Regression: QWizard.accept() validates the last page again, which asked and wrote again, in a loop.
    wait(lambda: False, 1)
    questions = [shown for shown in widgets.SHOWN if shown[0] == "Write and trust"]
    assert len(questions) == 1 and len(received) == 1 and wizard.result() == 1  # asked once, written once, closed
    assert (project / B.LAYOUT_NAME).is_file()



def test_the_write_finishing_closes_without_validating_a_page_again(wizard, tmp_path, monkeypatch, wait):
    """Regression: wrote() called accept(), and QWizard.accept() validates the current page again - any page,
    when Back was pressed while the file was written: its validatePage changed the board already handed on
    (and saved a token again). While writing, Back and Cancel are off, and the wizard closes as a QDialog."""
    import threading
    from PySide6.QtWidgets import QWizard
    project = tmp_path / "project"
    project.mkdir()
    wizard.folder_page.path.edit.setText(str(project))
    wizard.show()
    for _ in range(9):  # page by page, as the Next button goes (the Model page: the agent's provider)
        wizard.next()
    assert wizard.currentPage() is wizard.review_page
    assert wait(lambda: wizard.review_page.verdict_ok is not None, 60)
    release = threading.Event()
    real = wizard.sai.write_project
    monkeypatch.setattr(wizard.sai, "write_project", lambda *a: release.wait(30) and real(*a))
    received = []
    wizard.written.connect(lambda board, directory: received.append(directory))
    wizard.next()  # Finish on the last page: "Write and trust" (answered yes), written in a thread
    assert wizard.write_state == "writing"
    back, cancel = wizard.button(QWizard.WizardButton.BackButton), wizard.button(QWizard.WizardButton.CancelButton)
    assert not back.isEnabled() and not cancel.isEnabled(), "no Back or Cancel while the file is written"
    wizard.reject()
    assert wizard.isVisible(), "Escape or the window's close button do not cancel it either"
    wizard.back()  # as a click on Back could before: the hardware page is the current one now
    assert wizard.currentPage() is wizard.hardware_page
    wizard.hardware_page.memory.setText("3g")  # typed there meanwhile: its validatePage would put it on the board
    release.set()
    assert wait(lambda: received, 30)
    wait(lambda: False, 0.3)
    limits = wizard.board.first("limits")
    assert limits is None or limits.props.get("--memory") != "3g", "the page shown was not validated again"
    assert received == [str(project)] and not wizard.isVisible() and wizard.result() == 1


def test_an_existing_project_is_offered_and_kept(wizard, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".sandbox-ai.json").write_text(json.dumps({"version": 1, "agent": "claude", "options": ["--offline"],
                                                          "description": "keep me"}))
    wizard.folder_page.path.edit.setText(str(project))
    walk(wizard, wizard.folder_page, wizard.agent_page)
    wizard.start_page.initializePage()
    assert wizard.start_page.choice() == "existing"
    assert wizard.start_page.validatePage()
    walk(wizard, wizard.network_page, wizard.proxy_page, wizard.git_page, wizard.mcp_page, wizard.hardware_page)
    document, main = wizard.document()
    assert document["options"] == ["--offline"] and document["description"] == "keep me" and main == str(project)
    assert "No change." in wizard.file_diff() or '"--offline"' in wizard.file_diff()


def test_the_policy_greys_out_what_it_forbids(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    from sandbox_ai_app.wizard import SandboxWizard
    sai = backend.SandboxAI()
    monkeypatch.setattr(sai, "forbidden", lambda: {"--no-proxy": {None}, "--egress-all-allow": {None},
                                                  "--privacy-check": {"off"}})
    w = SandboxWizard(sai)
    assert not w.network_page.profiles["egress-all"].isEnabled() and not w.proxy_page.enabled.isEnabled()
    combo = w.proxy_page.privacy
    assert not combo.model().item(combo.findData("off")).isEnabled()


def test_a_new_folder_is_created_after_the_path_is_confirmed(wizard, tmp_path):
    target = tmp_path / "projects" / "new-test"
    wizard.folder_page.path.edit.setText(str(target))
    assert wizard.folder_page.validatePage() and target.is_dir()
    title, text, _ = widgets.SHOWN[-1]
    assert title == "Create the folder" and str(target) in text
    assert wizard.directory == os.path.realpath(target)


def test_a_new_folder_is_not_created_when_declined_or_in_a_private_place(wizard, tmp_path, monkeypatch):
    page = wizard.folder_page
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)
    page.path.edit.setText(str(tmp_path / "declined"))
    assert not page.validatePage() and not (tmp_path / "declined").exists() and "create" in page.note.text()
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    shown = len(widgets.SHOWN)
    for refused in (tmp_path / "config" / "evil",  # XDG_CONFIG_HOME: the host's settings
                    tmp_path / "work" / ".ssh" / "p",
                    os.path.join(os.environ["SANDBOX_AI_HOME"], "sandbox", "new-project"),
                    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "x")):
        page.path.edit.setText(str(refused))
        assert not page.validatePage(), refused
        assert "Not created" in page.note.text() and not os.path.exists(refused)
    assert len(widgets.SHOWN) == shown, "refused before the question"
    page.path.edit.setText("relative/folder")
    assert not page.validatePage() and "full path" in page.note.text()
    (tmp_path / "file").write_text("x")
    page.path.edit.setText(str(tmp_path / "file"))
    assert not page.validatePage() and "not a folder" in page.note.text()


def test_the_git_page_shows_how_tokens_get_there(wizard, tmp_path, monkeypatch, wait):
    """Quick rows for GitHub, GitLab and the local Gitea from a stored secret; the kept secrets' names are offered
    as you type (names only); each way of giving a token says what to type; the page explains git-logins,
    publish-logins and identities."""
    monkeypatch.setattr(wizard.sai, "secrets", lambda: ("file", [("GH_PUSH", "file"), ("PYPI_TOKEN", "file")]))
    project = tmp_path / "project"
    project.mkdir()
    wizard.folder_page.path.edit.setText(str(project))
    walk(wizard, wizard.folder_page, wizard.agent_page, wizard.start_page)
    page = wizard.git_page
    page.initializePage()
    assert wait(lambda: page.stored == ["GH_PUSH", "PYPI_TOKEN"], 10)
    github = next(b for b in page.findChildren(QtWidgets.QPushButton) if b.text() == "GitHub")
    github.click()
    row = page.table.rowCount() - 1
    assert page.table.item(row, 0).text() == "https://github.com"
    kind, edit = page.table.cellWidget(row, 1), page.token_edit(row)
    assert kind.currentData() == "secret" and edit.text() == "GH_PUSH"
    assert edit.completer() is not None and "GH_PUSH, PYPI_TOKEN" in edit.toolTip()
    kind.setCurrentIndex(kind.findData("env"))
    assert "variable of the shell" in edit.placeholderText() and edit.completer() is None
    assert "git-logins add" in page.help.text() and "publish-logins add pypi" in page.help.text()
    assert page.validatePage()


def test_the_folders_used_last_are_offered_and_taken(wizard, tmp_path, monkeypatch, wait):
    """User request: the project folder step offers the ten folders used last (recent starts' workspaces and the
    folders opened in the app); a click takes one, a double-click or Enter takes it and goes on; typing and
    Browse… stay."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    monkeypatch.setattr(wizard.sai, "recent_folders", lambda limit=10: [(str(one), 1762596000), (str(two), None)])
    wizard.show()
    page = wizard.folder_page
    page.path.edit.setText("/typed/before")
    page.initializePage()
    assert wait(lambda: page.recent.count() == 2, 10)
    assert page.path.edit.text() == "/typed/before", "filling the list chooses nothing"
    assert page.recent.item(0).text().startswith(str(one)) and "(used " in page.recent.item(0).text()
    assert page.recent.item(1).text() == str(two) and page.recent.accessibleName()
    rect = page.recent.visualItemRect(page.recent.item(1))
    QTest.mouseClick(page.recent.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, rect.center())
    assert page.path.edit.text() == str(two)
    page.recent.setFocus()
    page.recent.setCurrentRow(0)
    assert page.path.edit.text() == str(one), "the keys move through the list too"
    went = []
    monkeypatch.setattr(wizard, "next", lambda: went.append(True))
    page.recent.itemActivated.emit(page.recent.item(0))  # what a double-click or Enter sends
    assert went == [True] and page.path.edit.text() == str(one)
    wizard.hide()
