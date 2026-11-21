"""A folder of another machine (sshfs://[USER@]HOST[:PORT]/PATH) as an option of the existing workspace and mount
fields: the Add workspace dialog of a running sandbox, the Board's project folder and extra folder, the backend's
commands (sandbox-ai mounts it on the start's host with sshfs, sandbox/remotefs.py). User's decision 2026-11-16:
remote folders via sshfs for the Board and the mount/attach functions. Offscreen, against stand-ins: nothing
mounts, nothing starts."""
import os
import subprocess

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")

from sandbox_ai_app import backend, help_view, remote_source, widgets  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


def test_the_source_is_checked_as_sandbox_ai_checks_it():
    assert remote_source.parse("sshfs://dev@build1:2222/srv/project") == {
        "scheme": "sshfs", "user": "dev", "domain": None, "host": "build1", "port": 2222, "path": "/srv/project",
        "query": {}}
    assert remote_source.parse("sshfs://build1/~/proj")["path"] == "~/proj"
    assert remote_source.parse("sshfs://build1")["path"] == "~"
    for bad in ("sshfs://", "sshfs://h:0/x", "sshfs://h/a,b", "sshfs://u@@h/x", "sshfs://h/~/../x", "/srv/x",
                "sshfs://-h/x"):
        assert not remote_source.valid_source(bad), bad
    assert remote_source.build("build1", "/srv/p", "dev", 2222) == "sshfs://dev@build1:2222/srv/p"
    assert remote_source.build("build1", "~/p") == "sshfs://build1/~/p"
    assert remote_source.build("build1", "p") == "sshfs://build1/~/p"
    assert remote_source.build("build1", "") == "sshfs://build1/~"
    assert remote_source.label("sshfs://h/srv/my project/") == "my_project"


def test_the_core_and_the_app_agree_on_what_a_source_is():
    """The app's quick check (remote_source.parse) and the launcher's (sandbox/remotefs.py) take the same sources."""
    import importlib.util
    from pathlib import Path
    home = Path(os.environ.get("SANDBOX_AI_HOME") or Path(backend.shutil.which("sandbox-ai")).resolve().parent)
    spec = importlib.util.spec_from_file_location("remotefs_for_app_tests", home / "sandbox" / "remotefs.py")
    core = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(core)
    for value in ("sshfs://dev@build1:2222/srv/project", "sshfs://build1/~/proj", "sshfs://build1", "sshfs://",
                  "sshfs://h:0/x", "sshfs://h/a,b", "sshfs://u@@h/x", "sshfs://h/~/../x", "sshfs://-h/x",
                  "sshfs://u@[::1]:22/x", "SSHFS://h/x", "nfs://nas/export/data", "nfs://nas:2049/e?mount=sudo&vers=4.2",
                  "nfs://u@nas/x", "nfs://nas", "nfs://nas/x?mount=bad", "smb://CORP;alice@nas/share/docs?secret=PW",
                  "smb://nas/public", "smb://nas/", "smb://nas/s?secret=1x", "smb://nas/s?mount=fuse",
                  "smb://bad domain;u@nas/s", "nfs://nas/../x"):
        try:
            core.parse(value)
            taken = True
        except ValueError:
            taken = False
        assert remote_source.valid_source(value) is taken, value


def test_the_dialog_builds_a_source_and_takes_only_a_good_one(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    (tmp_path / "sandbox-ai").mkdir()
    (tmp_path / "sandbox-ai" / "hosts.json").write_text('{"version": 1, "hosts": {"build1": {"ssh": "dev@ct1"}}}')
    dialog = remote_source.RemoteSourceDialog(None)
    ok = dialog.buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok)
    assert not ok.isEnabled(), "no machine yet"
    assert dialog.host.findText("build1") >= 0, "the hosts of sandbox-ai hosts are offered"
    dialog.host.setCurrentText("build1")
    dialog.path.setText("/srv/project")
    assert ok.isEnabled() and dialog.source() == "sshfs://build1/srv/project"
    dialog.user.setText("-x")
    assert not ok.isEnabled() and dialog.problem.text()
    dialog.user.setText("dev")
    dialog.port.setValue(2222)
    assert dialog.source() == "sshfs://dev@build1:2222/srv/project" and ok.isEnabled()
    again = remote_source.RemoteSourceDialog(None, "sshfs://ops@db1:2200/~/dumps")
    assert (again.host.currentText(), again.user.text(), again.port.value(), again.path.text()) == \
        ("db1", "ops", 2200, "~/dumps")
    assert again.source() == "sshfs://ops@db1:2200/~/dumps"
    dialog.deleteLater()
    again.deleteLater()


def test_the_backend_passes_a_remote_folder_to_workspace_add():
    calls = []
    s = backend.SandboxAI(runner=lambda argv, **k: calls.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""))
    assert s.add_workspace("claude-abc", "sshfs://dev@build1/srv/data")[0] == 0
    assert calls[-1][-4:] == ["workspace", "add", "claude-abc", "sshfs://dev@build1/srv/data"]
    assert s.remove_workspace("claude-abc", "sshfs://dev@build1/srv/data")[0] == 0
    count = len(calls)
    for bad in (s.add_workspace("run", "sshfs://h:0/x"), s.add_workspace("run", "sshfs://-o/x")):
        assert bad[0] == 2
    assert len(calls) == count, "nothing invalid reaches sandbox-ai"


@pytest.fixture
def ui(qapp, monkeypatch):
    from sandbox_ai_app import ui as module
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    return module


def test_the_add_workspace_dialog_offers_another_machine_and_says_what_it_means(ui, monkeypatch):
    dialog = ui.WorkspaceDialog(None, "claude-1")
    ok = dialog.buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok)
    assert dialog.remote_button.text() == "Other machine…"

    def fill(made):
        made.host.setCurrentText("build1")
        made.path.setText("/srv/data")
        return QtWidgets.QDialog.DialogCode.Accepted
    monkeypatch.setattr(remote_source.RemoteSourceDialog, "exec", fill)
    dialog.remote_button.click()
    assert dialog.directory() == "sshfs://build1/srv/data"
    assert dialog.remote_note.isVisibleTo(dialog) and "changes and deletes the files on that machine" in \
        dialog.remote_note.text()
    dialog.read_only.setChecked(True)
    assert "reads its files" in dialog.remote_note.text() and ok.isEnabled()
    dialog.edit.setText("sshfs://h:0/x")
    assert not ok.isEnabled() and "port" in dialog.problem.text()
    dialog.edit.setText("/srv/local")
    assert ok.isEnabled() and not dialog.remote_note.isVisibleTo(dialog)
    assert ui.workspace_label({"sandbox": "/workspaces/data", "host": "/home/u/.local/state/sandbox-ai/remote/r/1/data",
                               "remote": "sshfs://build1/srv/data", "mode": "rw"}) == \
        "/workspaces/data ← sshfs://build1/srv/data (folder, sshfs)"
    dialog.deleteLater()


@pytest.fixture
def page(qapp, tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    monkeypatch.setattr(help_view, "OPENED", [])
    from sandbox_ai_app.board_view import BoardPage
    view = BoardPage(backend.SandboxAI(), lambda *a, **k: None, lambda: {"board_grid": 20, "guide_nodes": False})
    view.resize(1200, 800)
    view.show()
    qapp.processEvents()
    yield view
    view.close()
    view.deleteLater()


def inspector_texts(page):
    return [w.text() for w in page.inspector_holder.findChildren(QtWidgets.QLabel)]


def test_the_board_takes_a_remote_folder_as_project_folder_and_as_extra_folder(page, monkeypatch):
    from sandbox_ai_app import board as B
    from sandbox_ai_app.board_view import PathEdit
    node = page.board.first("workspace") or page.board.add("workspace", path="")
    page.inspect(node)
    edits = page.inspector_holder.findChildren(PathEdit)
    assert edits and edits[0].remote_button is not None
    monkeypatch.setattr(remote_source.RemoteSourceDialog, "exec", lambda made: (
        made.host.setCurrentText("build1"), made.path.setText("/srv/project"),
        QtWidgets.QDialog.DialogCode.Accepted)[-1])
    edits[0].remote_button.click()
    assert page.board.node(node.id).props["path"] == "sshfs://build1/srv/project"
    page.inspect(page.board.node(node.id))
    assert any("changes and deletes the files on that machine" in text for text in inspector_texts(page))
    mount = page.board.add("mount", source="sshfs://dev@build1:2222/data", dest="/mnt/data", mode="ro")
    page.board.connect(mount.id, page.board.first("agent").id)
    argv = B.to_argv(page.sai, page.board)
    assert "sshfs://build1/srv/project" in argv
    assert argv[argv.index("--workspace") + 1] == "sshfs://build1/srv/project"
    assert argv[argv.index("--mount") + 1] == "sshfs://dev@build1:2222/data:/mnt/data:ro"
    # and back: the board of these options has the same folders
    back = B.from_argv(page.sai, "claude", ["--workspace", "sshfs://build1/srv/project", "--mount",
                                            "sshfs://dev@build1:2222/data:/mnt/data:ro"])
    assert back.first("workspace").props["path"] == "sshfs://build1/srv/project"
    assert {k: back.first("mount").props[k] for k in ("source", "dest", "mode")} == \
        {"source": "sshfs://dev@build1:2222/data", "dest": "/mnt/data", "mode": "ro"}


def test_the_board_checks_a_remote_source_and_says_what_it_means():
    from sandbox_ai_app import board as B
    sai = backend.SandboxAI()
    board = B.from_argv(sai, "claude", ["--workspace", "sshfs://build1/srv/project", "--mount",
                                        "sshfs://h:0/x:/mnt/x:ro"])
    found = [(p.level if hasattr(p, "level") else p[0], p.text if hasattr(p, "text") else p[1])
             for p in B.problems(sai, board)]
    texts = " ".join(text for _, text in found)
    assert "relative path" not in texts, "a remote source is no relative path"
    assert "The mount sshfs://h:0/x: The port is 1-65535" in texts
    level, why = B.sensitive("sshfs://build1/srv/project")
    assert level == "warn" and "another machine" in why


def test_nfs_and_smb_sources_are_folders_of_another_machine_too():
    assert remote_source.valid_source("smb://CORP;alice@nas/share/docs?secret=NAS_PW")
    assert "SMB folder of nas, writable" in remote_source.warning("smb://nas/share", False)
    assert "NFS folder of nas" in remote_source.warning("nfs://nas/export", True)
    assert backend.valid_remote("nfs://nas/export/data")
