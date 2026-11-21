"""Every place of the app that asks for a folder or file used on another host chooses it from that host's folders
(remote_chooser), never from this machine's, and passes the path on as it is there (never resolved or looked at here).
The user's report: on the Board switched to another host, "Open project folder…" showed this machine's folders.

The Board end to end against the fake SFTP server as the host "lab" (test_remote_chooser's fixture); the other places
with RC.choose recorded: which host, file or folder, and what the field gets."""
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")

from sandbox_ai_app import backend, remote_chooser as RC, widgets  # noqa: E402
from test_remote_chooser import HOSTS, driven, host, rows, select  # noqa: E402,F401

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


@pytest.fixture
def sai(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    (tmp_path / "cwd").mkdir()
    monkeypatch.chdir(tmp_path / "cwd")
    sai = backend.SandboxAI()
    sai.ran = []
    monkeypatch.setattr(sai, "hosts_supported", lambda: True)
    monkeypatch.setattr(sai, "host_entries", lambda: [dict(h) for h in HOSTS])
    # Nothing reaches a host: what the board asks there fails at once.
    monkeypatch.setattr(sai, "host_run", lambda name, *args, **kw: sai.ran.append((name, *args)) or
                        (1, "", "not reached in the tests"))

    def local_review(directory):
        raise AssertionError(f"a folder of another host read on this machine: {directory}")
    monkeypatch.setattr(sai, "review_project", local_review)
    return sai


@pytest.fixture
def board(qapp, sai, host):
    from sandbox_ai_app.board_view import BoardPage
    view = BoardPage(sai, lambda *a, **k: None, lambda: {"board_grid": 20})
    view.opened_seen = []
    view.opened.connect(view.opened_seen.append)
    # As in the window: this machine's folder opens in a project tab of its own (open_request).
    view.open_request = lambda: pytest.fail("this machine's project folder chosen for a board on another host")
    assert view.host_choice.choose("lab")
    yield view
    view.close()
    view.deleteLater()


def shown_project(sai, monkeypatch, document=None, trusted=False, error=None):
    asked = []

    def show(name, path):
        asked.append((name, path))
        if error:
            return {"path": path, "digest": None, "error": error, "document": None, "trusted": False}
        return {"path": path, "digest": "0" * 64, "document": document, "trusted": trusted,
                "trusted_digest": None, "problem": None}
    monkeypatch.setattr(sai, "host_project_show", show)
    return asked


def open_button(board):
    return next(b for b in board.findChildren(QtWidgets.QPushButton) if b.text() == "Open project folder…")


def workspaces(board):
    return [n.props.get("path") for n in board.board.of_kind("workspace")]


def test_open_project_folder_on_a_host_lists_that_hosts_folders_and_keeps_its_path(board, sai, host, monkeypatch,
                                                                                    wait):
    """The user's report: the Board on another host, Open project folder… - that host's folders, not this machine's;
    its project file read there (sandbox-ai wizard trust --show on that host), the path passed on unchanged."""
    asked = shown_project(sai, monkeypatch, {"agent": "codex", "options": ["--cpus", "2"]}, trusted=True)

    def answer(dialog):
        assert dialog.host == "lab" and "proj/" in rows(dialog) and "other/" in rows(dialog)
        select(dialog, "proj/")
        return dialog.chosen()
    seen = driven(monkeypatch, wait, answer)
    open_button(board).click()
    assert seen, "the chooser of the host"
    assert wait(lambda: workspaces(board) == [str(host / "proj")], 20), workspaces(board)
    assert asked == [("lab", str(host / "proj" / ".sandbox-ai.json"))]
    assert board.board.first("agent").props.get("agent") == "codex"
    assert board.directory is None and board.opened_seen == [], "no project tab of this machine for it"
    assert board.remote_project == ("lab", str(host / "proj"))
    assert "lab" in board.folder.text() and str(host / "proj") in board.folder.toolTip()


def test_a_hosts_folder_without_a_project_file_becomes_the_boards_workspace(board, sai, host, monkeypatch, wait):
    shown_project(sai, monkeypatch, error="no such file")
    driven(monkeypatch, wait, lambda dialog: select(dialog, "other/") and dialog.chosen())
    assert board.open_project()
    assert wait(lambda: workspaces(board) == [str(host / "other")], 20)
    assert board.opened_seen == [] and board.remote_project == ("lab", str(host / "other"))


def test_an_untrusted_project_file_of_a_host_is_asked_about_before_its_start(board, sai, host, monkeypatch, wait):
    shown_project(sai, monkeypatch, {"agent": "claude", "options": []}, trusted=False)
    driven(monkeypatch, wait, lambda dialog: select(dialog, "proj/") and dialog.chosen())
    board.open_project()
    assert wait(lambda: workspaces(board) == [str(host / "proj")], 20)
    assert board.untrusted.get(board.key_of(board.directory)) == str(host / "proj" / ".sandbox-ai.json")


def test_a_typed_path_of_an_unreachable_host_is_taken_as_it_is(board, sai, host, tmp_path, monkeypatch, wait):
    from test_remote_chooser import failing
    monkeypatch.setattr(RC, "SSH", failing("ssh: connect to host lab.example port 22: Connection refused", "",
                                           tmp_path))
    shown_project(sai, monkeypatch, error="not reached")

    def answer(dialog):
        assert wait(lambda: "type the path" in dialog.status.text(), 20)
        dialog.path.setText("/srv/../srv/app")
        return dialog.chosen()
    driven(monkeypatch, wait, answer, listed=False)
    board.open_project()
    assert wait(lambda: workspaces(board) == ["/srv/../srv/app"], 20), "as typed: never resolved here"


def test_the_inspectors_folder_fields_choose_from_the_host(board, sai, host, monkeypatch, wait):
    from sandbox_ai_app.board_view import PathEdit
    node = board.board.add("workspace", path="")
    board.rebuild()
    board.inspect(node)
    field = next(w for w in board.inspector_holder.findChildren(PathEdit))
    assert field.browse_button.isEnabled(), "a chooser on that host, no longer greyed out"
    driven(monkeypatch, wait, lambda dialog: select(dialog, "proj/") and dialog.chosen())
    field.browse_button.click()
    assert wait(lambda: board.board.node(node.id).props.get("path") == str(host / "proj"), 10)
    mount = board.board.add("mount", source="", dest="/mnt/x", mode="ro")
    board.rebuild()
    board.inspect(mount)
    field = next(w for w in board.inspector_holder.findChildren(PathEdit))
    driven(monkeypatch, wait, lambda dialog: select(dialog, "other/") and dialog.chosen())
    field.browse_button.click()
    assert wait(lambda: board.board.node(mount.id).props.get("source") == str(host / "other"), 10)


def test_clone_as_a_new_project_on_a_host_takes_that_hosts_folder_unresolved(board, sai, host, monkeypatch, wait):
    # A project file there already: Apply writes on this machine only, so nothing of the host is overwritten.
    (host / "other" / ".sandbox-ai.json").write_text("{}")
    board.board.add("workspace", path="/srv/first")
    board.rebuild()
    driven(monkeypatch, wait, lambda dialog: select(dialog, "other/") and dialog.chosen())
    assert board.clone_to_project()
    assert workspaces(board) == [str(host / "other")]
    assert board.directory is None and board.opened_seen == [] and board.remote_project == ("lab", str(host / "other"))


def test_on_this_machine_the_board_keeps_this_machines_dialog(board, monkeypatch):
    assert board.host_choice.choose("local")
    opened = []
    board.open_request = lambda: opened.append(True)
    monkeypatch.setattr(RC, "run_dialog", lambda dialog: pytest.fail("the remote chooser for this machine"))
    open_button(board).click()
    assert opened == [True]


# -- every other place: RC.choose asked for that host (recorded), the path passed on as it is --

@pytest.fixture
def chooser(qapp, monkeypatch):
    """RC.choose recorded: {host, mode, start, title} of each question; it answers /remote/chosen (a folder) or
    /remote/chosen/file.txt (a file). This machine's file dialog must not open for a host's path."""
    from sandbox_ai_app import state
    asked = []

    def choose(parent, host, title, mode="folder", key="folder", start="", sai=None, name_filter=""):
        asked.append({"host": host, "mode": mode, "start": start, "title": title})
        return "/remote/chosen" + ("/file.txt" if mode == "file" else "")
    monkeypatch.setattr(RC, "choose", choose)
    monkeypatch.setattr(state, "run_dialog", lambda dialog: pytest.fail("this machine's file dialog for a host"))
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    return asked


def test_the_start_pages_path_fields_choose_from_the_host(qapp, sai, chooser):
    from sandbox_ai_app import ui
    page = ui.LaunchPage(sai, lambda *a, **k: None)
    try:
        assert page.host_choice.choose("lab")
        workspace = page.form.widgets["--workspace"]
        assert workspace.add_button.isEnabled()
        workspace.add_button.click()
        assert workspace.value()[-1] == "/remote/chosen" and chooser[-1]["host"] == "lab"
        field = next(w for w in page.form.widgets.values() if isinstance(w, ui.PathField))
        assert field.browse_button.isEnabled()
        field.browse_button.click()
        assert chooser[-1]["host"] == "lab" and field.value().startswith("/remote/chosen")
        assert page.host_choice.choose("local")
        assert workspace.host == "local" and field.host == "local", "this machine's dialog again"
    finally:
        page.close()
        page.deleteLater()


def test_the_start_pages_project_on_a_host_is_chosen_there_and_reviewed_there(qapp, sai, chooser, monkeypatch):
    from sandbox_ai_app import host_trust, ui
    reviewed = []
    monkeypatch.setattr(host_trust, "review_and_trust", lambda parent, s, host, path, **kw: reviewed.append(
        (host, path)))
    page = ui.LaunchPage(sai, lambda *a, **k: None)
    try:
        assert page.host_choice.choose("lab")
        page.form.widgets["--workspace"].set_value(["/srv/app"])
        page.load_project()
        assert chooser[-1]["host"] == "lab" and chooser[-1]["start"] == "/srv/app"
        assert reviewed == [("lab", "/remote/chosen/.sandbox-ai.json")]
    finally:
        page.close()
        page.deleteLater()


def test_resume_with_add_workspace_privacy_and_mcp_folders_on_a_host(qapp, sai, chooser):
    from sandbox_ai_app import ui
    resume = ui.ResumeDialog(None, "api", host="lab")
    resume.workspaces.add_button.click()
    assert resume.workspaces.value() == ["/remote/chosen"] and chooser[-1]["host"] == "lab"
    resume.deleteLater()
    dialog = ui.WorkspaceDialog(None, "api", host="lab")
    assert sorted(b.text() for b in dialog.browse_buttons) == ["File…", "Folder…"]
    next(b for b in dialog.browse_buttons if b.text() == "File…").click()
    assert dialog.directory() == "/remote/chosen/file.txt" and chooser[-1] == {
        "host": "lab", "mode": "file", "start": "", "title": "File for the sandbox"}
    next(b for b in dialog.browse_buttons if b.text() == "Folder…").click()
    assert dialog.directory() == "/remote/chosen" and chooser[-1]["start"] == "/remote/chosen/file.txt"
    dialog.deleteLater()
    privacy = ui.PrivacyDialog(None, sai)
    assert privacy.host_choice.choose("lab")
    privacy.rules.browse_button.click()
    privacy.paths.add_button.click()
    assert [a["host"] for a in chooser[-2:]] == ["lab", "lab"]
    assert privacy.values()[1:3] == (["/remote/chosen"], "/remote/chosen")
    privacy.deleteLater()
    server = ui.AddServerDialog(None, {"name": "files", "mounts": [{"target": "/data"}]}, host="lab")
    field = next(w for w in server.findChildren(ui.PathField))
    field.browse_button.click()
    assert chooser[-1]["host"] == "lab" and field.value() == "/remote/chosen"
    server.deleteLater()


def test_kits_images_vpn_and_team_files_of_a_host(qapp, sai, chooser):
    from sandbox_ai_app import images_page, kits_page, team_page, vpn_page
    hook = kits_page.HookDialog(None, ["claude"], remote=True, host="lab")
    assert not hook.browse.isHidden()
    hook.choose()
    assert hook.file.text() == "/remote/chosen/file.txt" and chooser[-1]["host"] == "lab"
    hook.deleteLater()
    build = images_page.BuildImageDialog(None, [], local=False, host="lab")
    build.choose_file()
    build.choose_context()
    assert (build.file.text(), build.context.text()) == ("/remote/chosen/file.txt", "/remote/chosen")
    assert [(a["host"], a["mode"]) for a in chooser[-2:]] == [("lab", "file"), ("lab", "folder")]
    build.deleteLater()
    vpn = vpn_page.VpnDialog(None, "lab", remote=True, sai=sai, host="lab")
    assert not vpn.browse.isHidden() and not vpn.bridges_browse.isHidden()
    vpn.choose(vpn.file)
    assert vpn.file.text() == "/remote/chosen/file.txt" and chooser[-1]["host"] == "lab"
    vpn.deleteLater()
    team = team_page.StartDialog(None, "lab", True, folder="/srv/team", host="lab")
    team.choose()
    assert team.workspace.text() == "/remote/chosen" and chooser[-1] == {
        "host": "lab", "mode": "folder", "start": "/srv/team", "title": "Workspace"}
    team.deleteLater()


def test_a_bridges_folder_of_a_session_on_a_host_is_chosen_there(qapp, chooser):
    from sandbox_ai_app.bridges_view import PathList
    paths = PathList(lambda: "/remote", "Read files", lambda: "lab")
    paths.pick()
    assert chooser[-1]["host"] == "lab" and chooser[-1]["start"] == "/remote"
    assert paths.paths() == ["chosen"], "relative to the workspace there"
    paths.deleteLater()


def test_trusted_projects_and_preset_folders_of_a_host(qapp, sai, chooser, monkeypatch):
    from sandbox_ai_app import host_trust, settings
    reviewed = []
    monkeypatch.setattr(host_trust, "review_and_trust", lambda parent, s, host, path, **kw: reviewed.append(
        (host, path)))
    trusted = settings.TrustedProjects(sai)
    monkeypatch.setattr(trusted, "host", lambda: "lab")
    trusted.review()
    assert chooser[-1]["host"] == "lab" and reviewed == [("lab", "/remote/chosen/.sandbox-ai.json")]
    trusted.deleteLater()
    dirs = settings.PresetDirs(sai)
    monkeypatch.setattr(dirs, "host", lambda: "lab")
    added = []
    monkeypatch.setattr(dirs, "add_path", added.append)
    dirs.add()
    assert added == ["/remote/chosen"] and chooser[-1]["host"] == "lab"
    dirs.deleteLater()


def test_preset_placeholders_of_a_host_keep_its_paths_as_they_are(qapp, sai, chooser):
    from sandbox_ai_app import presets as PR
    from sandbox_ai_app.board_view import PathEdit
    from sandbox_ai_app.preset_gallery import PlaceholderForm
    preset = next((p for p in PR.catalogue(sai, "local").presets if any(
        getattr(h, "kind", "") == "folder" for h in getattr(p, "placeholders", ()))), None)
    if preset is None:
        pytest.skip("no preset with a folder placeholder")
    form = PlaceholderForm(sai, preset, None, host="lab")
    marker, field = next((m, w) for m, w in form.fields.items() if isinstance(w, PathEdit))
    assert field.host == "lab"
    field.browse_button.click()
    assert chooser[-1]["host"] == "lab"
    field.edit.setText("~/projects/app")
    assert form.values()[marker] == "~/projects/app", "another host's ~ is never this machine's HOME"
    form.deleteLater()


def test_log_folders_of_a_host_are_chosen_there(qapp, sai, chooser, monkeypatch):
    from sandbox_ai_app import activity_page, pages, ui
    for page in (activity_page.ActivityPage(sai), pages.StatisticsPage(sai), ui.ConnectionsPage(sai)):
        monkeypatch.setattr(page, "host", lambda: "lab")
        if hasattr(page, "remote"):
            monkeypatch.setattr(page, "remote", lambda: True)
        monkeypatch.setattr(page.hosts, "host", lambda: "lab")
        selected = []
        for name in ("refresh_sources", "refresh_directories", "fill_remote_sources"):
            if hasattr(page, name):
                monkeypatch.setattr(page, name, lambda *a, **k: selected.append((a, k)))
        page.browse()
        assert chooser[-1]["host"] == "lab" and chooser[-1]["title"] == "Log folder", type(page).__name__
        assert selected, type(page).__name__
        page.deleteLater()


def test_a_sessions_folder_map_to_a_host_is_chosen_there(qapp, sai, chooser, monkeypatch, tmp_path):
    from sandbox_ai_app import session_transfer
    monkeypatch.setattr(sai, "session_transfer_plan", lambda name: ([str(tmp_path)], []), raising=False)
    dialog = session_transfer.SessionTransferDialog(None, sai, "api")
    dialog.plan_read(([str(tmp_path)], []), None)
    monkeypatch.setattr(dialog.host, "host", lambda: "lab")
    dialog.browse(str(tmp_path), dialog.edits[str(tmp_path)])
    assert chooser[-1]["host"] == "lab" and dialog.maps() == [(str(tmp_path), "/remote/chosen")]
    dialog.deleteLater()


def test_the_mcp_servers_folder_on_a_host_is_chosen_there(qapp, sai, chooser, monkeypatch):
    from sandbox_ai_app import board_view, prerequisites
    page = board_view.BoardPage(sai, lambda *a, **k: None, lambda: {"board_grid": 20})
    try:
        monkeypatch.setattr(prerequisites, "required_folders", lambda entries, entry: ["/data"])
        monkeypatch.setattr(page, "prerequisite_mcp_entries", lambda: {})
        from sandbox_ai_app import widgets as W
        monkeypatch.setattr(W, "choose", lambda *a, **k: "other")
        folders, dropped = page.ask_mcp_folders("lab", ["files"], ["--workspace", "/srv/ws"], {})
        assert folders == {"files": ["/data=/remote/chosen"]} and dropped == {}
        assert chooser[-1]["host"] == "lab" and chooser[-1]["start"] == "/srv/ws"
    finally:
        page.close()
        page.deleteLater()


def test_a_folder_of_this_computer_dropped_on_a_board_of_a_host_is_refused(board, tmp_path):
    folder = tmp_path / "here"
    folder.mkdir()
    from PySide6.QtCore import QPointF
    before = len(board.board.of_kind("workspace"))
    board.choose_drop = lambda choices, point=None: "workspace"
    assert board.drop_paths([str(folder)], QPointF(10, 10)) is None
    assert len(board.board.of_kind("workspace")) == before
    assert "this computer's, but the board starts on lab" in widgets.SHOWN[-1][1]


def test_switching_between_a_local_project_and_a_hosts_folder_shows_each_ones_folder(board, sai, host, monkeypatch,
                                                                                     wait, tmp_path):
    local = tmp_path / "here-project"
    local.mkdir()

    def no_file(directory):
        raise FileNotFoundError(directory)
    monkeypatch.setattr(sai, "review_project", no_file)  # (this machine's project: read here, as before)
    board.activate(str(local))
    assert board.directory == str(local) and board.remote_project is None
    shown_project(sai, monkeypatch, error="none")
    driven(monkeypatch, wait, lambda dialog: select(dialog, "proj/") and dialog.chosen())
    board.open_project()
    assert wait(lambda: board.remote_project == ("lab", str(host / "proj")), 20)
    board.activate(str(local))  # its kept board again
    assert board.remote_project is None and str(local) in board.folder.toolTip() and "lab" not in board.folder.text()
    assert workspaces(board) == [str(local)]
