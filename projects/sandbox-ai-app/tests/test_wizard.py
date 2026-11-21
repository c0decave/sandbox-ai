"""The wizard offscreen, page by page as the buttons would go: it builds the board model, keeps typed tokens
out of the project file, honours the policy, writes and trusts the file and hands the board on."""
import json
import os

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
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")  # (a typed token is kept as a secret: here, not in a keyring)
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
    wizard.network_page.lists.add("docs.example.org", key="--allow-host")
    assert wizard.network_page.validatePage()
    wizard.proxy_page.initializePage()
    wizard.proxy_page.rules.add_row("api.github.com", "GET,HEAD", "/**")
    assert wizard.proxy_page.validatePage()
    wizard.git_page.initializePage()
    wizard.git_page.add_row("https://github.com", "typed", "ghp_secret_value")
    # a typed token is kept in the background (the GUI never waits on sandbox-ai); valid once the row names it
    if not wizard.git_page.validatePage():
        assert wait(lambda: not getattr(wizard.git_page, "keeping", False), 60)
    assert wizard.git_page.validatePage(), wizard.git_page.note.text()
    walk(wizard, wizard.mcp_page, wizard.hardware_page)
    argv = B.to_argv(wizard.sai, wizard.board)
    assert "docs.example.org" in argv and "api.github.com GET,HEAD /**" in argv
    # a typed token is kept as a secret of sandbox-ai (since 0.1.98), the project file names the secret
    assert argv[argv.index("https://github.com=GIT_TOKEN_GITHUB_COM") - 1] == "--git-token-secret"
    assert ("GIT_TOKEN_GITHUB_COM", "file") in wizard.sai.secrets()[1]
    assert not (tmp_path / "config/sandbox-ai/tokens").exists(), "no token file"
    assert "ghp_secret_value" not in json.dumps(wizard.document()[0]), "a typed token never goes into the project file"
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


def test_going_back_and_on_again_keeps_what_was_set_on_every_page(wizard, tmp_path):
    """The bug class of 2026-11-16 (set, gone after the dialog was opened again): Back and Next again lost what was
    set. Next refilled a page from the board, which only Next had written: Back now writes it as Next would (the Git
    page: never a typed token on the way back, its row stays in the table). Start from rebuilt the board at every
    Next, dropping every later page's changes: it is kept while what it was built from is unchanged. Entered again,
    Start from and Agent kept their choice instead of going back to the default or the project's."""
    project = tmp_path / "project"
    project.mkdir()
    (project / ".sandbox-ai.json").write_text(json.dumps({"version": 1, "agent": "claude", "options": []}))
    wizard.folder_page.path.edit.setText(str(project))
    walk(wizard, wizard.folder_page, wizard.agent_page)
    assert wizard.agent_page.chosen() == "claude", "the project's agent"
    wizard.agent_page.agent.setCurrentIndex(wizard.agent_page.agent.findData("codex"))
    assert wizard.agent_page.chosen() == "codex"
    wizard.agent_page.initializePage()  # entered again (Back, Next)
    assert wizard.agent_page.chosen() == "codex", "the agent chosen stays"
    start = wizard.start_page
    start.initializePage()
    start.buttons["custom"].setChecked(True)
    start.initializePage()  # Back to the model, Next
    assert start.choice() == "custom", "the choice made stays"
    assert start.validatePage()
    built = wizard.board
    network, hardware, git = wizard.network_page, wizard.hardware_page, wizard.git_page
    network.initializePage()
    network.lists.value_box.setEditText("docs.example.org")  # typed, not added: Back takes it as Next would
    network.cleanupPage()  # Back to Start from
    assert start.validatePage() and wizard.board is built, "nothing changed there: the board stays"
    network.initializePage()
    assert network.lists.values("--allow-host") == ["docs.example.org"]
    assert network.validatePage()
    walk(wizard, wizard.proxy_page)
    git.initializePage()
    git.add_row("https://github.com", "secret", "GH_PUSH")
    git.add_row("https://gitlab.com", "typed", "glpat-secret-value")
    git.cleanupPage()  # Back
    props = wizard.board.first("git").props
    assert props.get("--git-token-secret") == ["https://github.com=GH_PUSH"]
    assert "glpat-secret-value" not in json.dumps(props, default=str), "a typed token is not kept on the way back"
    git.initializePage()
    assert git.table.rowCount() == 2 and git.token_edit(1).text() == "glpat-secret-value", "the rows as they were"
    git.table.removeRow(1)
    assert git.validatePage()
    walk(wizard, wizard.mcp_page)
    hardware.initializePage()
    hardware.memory.setText("16g")
    hardware.devices.setPlainText("/dev/ttyUSB0")
    hardware.cleanupPage()
    hardware.initializePage()
    assert hardware.memory.text() == "16g" and hardware.devices.values() == ["/dev/ttyUSB0"]
    argv = B.to_argv(wizard.sai, wizard.board)
    assert "docs.example.org" in argv and "16g" in argv
    # Another choice on Start from: built anew, as the subtitle says (a preset fills in the next pages).
    start.buttons["preset"].setChecked(True)
    start.select_preset("development")
    assert start.validatePage() and wizard.board is not built
    network.initializePage()
    assert "docs.example.org" not in network.lists.values("--allow-host")


def test_another_model_keeps_the_later_pages(wizard, tmp_path):
    """Backlog: going back to the Model page and choosing another model (the cloud's, its effort, Show thinking or
    another model server) rebuilt the board from scratch - every later page's changes were gone. Now only what
    depends on the model changes (the agent's model and effort, --show-thinking, --llm and its "only this server");
    another agent, folder or Start-from choice still builds anew."""
    project = tmp_path / "project"
    project.mkdir()
    wizard.folder_page.path.edit.setText(str(project))
    walk(wizard, wizard.folder_page, wizard.agent_page)
    model_page, start = wizard.model_page, wizard.start_page
    model_page.initializePage()
    model_page.cloud.setChecked(True)
    assert model_page.validatePage()
    start.initializePage()
    start.buttons["custom"].setChecked(True)
    assert start.validatePage()
    built = wizard.board
    network, hardware = wizard.network_page, wizard.hardware_page
    network.initializePage()
    network.lists.add("docs.example.org", key="--allow-host")
    assert network.validatePage()
    walk(wizard, wizard.proxy_page, wizard.git_page, wizard.mcp_page)
    hardware.initializePage()
    hardware.memory.setText("16g")
    assert hardware.validatePage()
    # Back to the Model page: another cloud model and effort
    model_page.initializePage()
    model_page.model.setEditText("gpt-5.5-codex")
    if model_page.effort.count() > 1:
        model_page.effort.setCurrentIndex(model_page.effort.count() - 1)
    assert model_page.validatePage()
    start.initializePage()
    assert start.validatePage()
    assert wizard.board is built, "the same board: the later pages' changes stay"
    argv = B.to_argv(wizard.sai, wizard.board)
    assert "docs.example.org" in argv and "16g" in argv
    agent = wizard.board.first("agent")
    assert agent.props.get("model") == "gpt-5.5-codex"
    if model_page.effort.count() > 1:
        assert agent.props.get("effort") == model_page.effort.currentData()
    # an own model server instead: --llm on the same board, the cloud model gone, the later pages kept
    wizard.llm_choice = {"name": "studio", "model": "qwen/qwen3-coder-next", "only": True}
    model_page.validatePage = lambda: True  # (the server's page is tested in test_wizard_llm_ui)
    model_page.own.setChecked(True)
    assert start.validatePage() and wizard.board is built
    argv = B.to_argv(wizard.sai, wizard.board)
    assert "--llm" in argv and argv[argv.index("--llm") + 1] == "studio:qwen/qwen3-coder-next"
    assert "--model" not in argv and "--no-provider-defaults" in argv and "docs.example.org" in argv
    # back to the cloud: the server goes, the rest stays
    wizard.llm_choice = None
    model_page.cloud.setChecked(True)
    assert start.validatePage() and wizard.board is built
    argv = B.to_argv(wizard.sai, wizard.board)
    assert "--llm" not in argv and "--no-provider-defaults" not in argv and "16g" in argv
    # another agent: built anew (the agent's own options and defaults)
    wizard.agent_page.agent.setCurrentIndex(wizard.agent_page.agent.findData("claude"))
    assert start.validatePage() and wizard.board is not built


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
    """Declined: nothing made. In a private place (the host's settings, keys, sandbox-ai's or the app's own folder):
    you decide what a sandbox gets - the question says what it means first, and a yes makes it."""
    page = wizard.folder_page
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)
    page.path.edit.setText(str(tmp_path / "declined"))
    assert not page.validatePage() and not (tmp_path / "declined").exists() and "create" in page.note.text()
    risky = [tmp_path / "config" / "evil",  # XDG_CONFIG_HOME: the host's settings
             tmp_path / "work" / ".ssh" / "p",
             os.path.join(os.environ["SANDBOX_AI_HOME"], "sandbox", "new-project"),
             os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "x")]
    for path in risky:  # a no: nothing made, the warning was in the question
        page.path.edit.setText(str(path))
        assert not page.validatePage() and not os.path.exists(path), path
        title, text, _ = widgets.SHOWN[-1]
        assert title == "Create the folder" and "Warning: it is" in text and "read-only" in text, text
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    for path in risky[:2]:  # a yes: made (the checkout's own folders are not made here: the tree is the tests')
        page.path.edit.setText(str(path))
        page.validatePage()
        assert os.path.isdir(path), path
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


def test_the_effort_offers_the_models_own_levels_and_resets_one_it_does_not_take(wizard, tmp_path):
    """The wizard asks as the Board does: the cloud models grouped by provider, the effort's levels those of the model
    chosen (sandbox-ai's table of cloud models); a level the next model does not take is reset, with a note."""
    from sandbox_ai_app import models as M
    project = tmp_path / "project"
    project.mkdir()
    wizard.folder_page.path.edit.setText(str(project))
    walk(wizard, wizard.folder_page, wizard.agent_page)
    page = wizard.model_page
    page.initializePage()
    page.cloud.setChecked(True)
    page.agent_changed("claude")  # (the agent page's choice: this computer's first agent with a login)
    assert page.model.findData(M.HEADER + "Anthropic") > 0, "grouped by provider"

    def levels():
        return [page.effort.itemData(i) for i in range(page.effort.count())]
    page.model.setCurrentIndex(page.model.findData("claude-opus-5-5"))
    assert levels() == ["", "low", "medium", "high", "xhigh", "max"]
    assert page.effort.itemText(0) == "the agent's default (medium for this model)"
    page.effort.setCurrentIndex(page.effort.findData("xhigh"))
    page.model.setCurrentIndex(page.model.findData("claude-opus-4-6"))
    assert levels() == ["", "low", "medium", "high", "max"] and page.effort.currentData() == ""
    assert not page.effort_note.isHidden()
    assert page.effort_note.text() == "The effort xhigh was reset: claude-opus-4-6 does not take it."
    page.effort.setCurrentIndex(page.effort.findData("max"))
    page.model.setEditText("claude-not-listed")  # typed: the agent's levels, the effort kept
    page.model.lineEdit().editingFinished.emit()
    assert levels() == ["", "low", "medium", "high", "xhigh", "max"] and page.effort.currentData() == "max"
