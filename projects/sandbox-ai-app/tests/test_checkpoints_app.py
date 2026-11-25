"""Checkpoints… offscreen (`sandbox-ai checkpoint`): the Sessions page's button and right-click menu (a start of this
machine or a host, one that ended too; never an MCP server), the dialog against a stand-in that records what it was
asked (listed, Changes, Restore… with its preview first and then asked, Delete asked, Take one now with its label),
and once end to end through the real sandbox-ai of this checkout: a workspace whose files an agent deleted put back.
The GUI thread never runs the commands (BLOCKING_SAI: tests/test_references)."""
import importlib.util
import json
import os
from pathlib import Path

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, widgets  # noqa: E402

from test_accessibility import unnamed  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")
ID = "20261117-120000-abcdef"


def at_once(function, *args, done, **kw):
    """widgets.background, but on this thread: DONE gets the result or the error at once."""
    try:
        result = function(*args)
    except Exception as exc:  # noqa: BLE001 - as background hands it on
        done(None, exc)
        return
    done(result, None)


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    (tmp_path / "cwd").mkdir()
    monkeypatch.chdir(tmp_path / "cwd")
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    from sandbox_ai_app import checkpoints_dialog
    monkeypatch.setattr(checkpoints_dialog, "background", at_once)
    asked = []

    def ask_first(parent, action, title, text, accept, detail=None, details=None, reject=None):
        assert backend.needs_confirm(action, detail) is not None, action
        asked.append((action, text))
        return True
    monkeypatch.setattr(checkpoints_dialog, "ask_first", ask_first)
    return tmp_path, asked


@pytest.fixture
def recorded(env, monkeypatch):
    """A SandboxAI whose commands are recorded and answered from ANSWERS (a list, first first)."""
    sai = backend.SandboxAI()
    sai.calls, sai.answers = [], []

    def call(*args, timeout=120, input=None, argv=None):
        sai.calls.append(args)
        return sai.answers.pop(0) if sai.answers else (0, "{}", "")
    monkeypatch.setattr(sai, "call", call)
    return sai


def session(name="web", state="stopped", host="local", kind="session"):
    if host != "local":
        return backend.Instance(kind, f"remote:{host}:{name}", name, host=host, state=state, agent="codex")
    return backend.Instance(kind, f"{kind}:{name}", name, session=name if kind == "session" else None, state=state,
                            agent="codex", mcp=name if kind == "mcp" else None)


LISTED = [{"id": ID, "created": "2026-11-17T12:00:00Z", "kind": "start", "files": 3, "bytes": 2048, "label": None,
           "roots": [{"host": "/srv/web", "sandbox": "/work"}]}]
CHANGES = {"counts": {"changed": 1, "added": 1, "deleted": 1},
           "roots": [{"host": "/srv/web", "sandbox": "/work", "changed": [["src/a.py", "content"]],
                      "added": ["new.txt"], "deleted": ["old.txt"]}]}


def test_the_words_of_the_checkpoint_commands_are_checked():
    assert backend.checkpoint_paths(["src/a.py", " /work/docs "]) == ["--path=src/a.py", "--path=/work/docs"]
    for bad in (["a\nb"], [""], ["x" * 5000]):
        with pytest.raises(ValueError):
            backend.checkpoint_paths(bad)
    for bad in ("../x", "2026", "", None):
        with pytest.raises(ValueError):
            backend.checkpoint_id(bad)
    assert set(backend.NEEDS_CONFIRM) >= {"checkpoint-restore", "checkpoint-delete"}


def test_the_button_and_the_right_click_for_each_kind_of_start(qapp, recorded, env, monkeypatch):
    from sandbox_ai_app import ui
    monkeypatch.setattr(recorded, "hosts_supported", lambda: True)
    monkeypatch.setattr(recorded, "host_entries", lambda: [{"name": "local", "kind": "local"},
                                                             {"name": "ct1", "kind": "ssh", "ssh": "a@192.0.2.1"}])
    ended = backend.Instance("run", "ended:local:x", "x", run="codex-1", state="ended")
    for instance, why in ((session(), None), (session(state="running"), None), (session(host="ct1"), None),
                          (backend.Instance("run", "run:x", "x", run="codex-1", state="running"), None),
                          (ended, None), (session("files", "running", kind="mcp"), "an MCP server"),
                          (None, "Select a row")):
        page = ui.SandboxesPage(recorded, lambda *a, **k: None)
        monkeypatch.setattr(page, "current", lambda instance=instance: instance)
        page.update_buttons()
        button = page.transfer_buttons["checkpoints"]
        if why is None:
            assert button.isEnabled(), (instance.key, button.toolTip())
            entries = widgets.menu_entries(widgets.menu_of(page.table))
            assert any("Checkpoints…" in str(entry) for entry in entries), entries
        else:
            assert not button.isEnabled() and why in button.toolTip(), (why, button.toolTip())
        page.close()
    opened = []
    from sandbox_ai_app import checkpoints_dialog
    monkeypatch.setattr(checkpoints_dialog.CheckpointsDialog, "exec",
                        lambda dialog: opened.append((dialog.name, dialog.host, dialog.running_start)) or 0)
    for instance in (session(state="running"), session(host="ct1")):
        page = ui.SandboxesPage(recorded, lambda *a, **k: None)
        monkeypatch.setattr(page, "current", lambda instance=instance: instance)
        page.update_buttons()
        recorded.answers.append((0, "[]", ""))
        page.transfer_buttons["checkpoints"].click()
        page.close()
    assert opened == [("web", "local", True), ("web", "ct1", False)]
    assert ("checkpoint", "list", "web", "--json") in recorded.calls
    assert any(call[:3] == ("hosts", "run", "ct1") and "checkpoint" in call for call in recorded.calls)


def test_the_dialog_lists_compares_previews_restores_deletes_and_takes(qapp, recorded, env):
    _, asked = env
    from sandbox_ai_app.checkpoints_dialog import CheckpointsDialog
    recorded.answers.append((0, json.dumps(LISTED), ""))
    dialog = CheckpointsDialog(None, recorded, "web", running=True)
    assert unnamed(dialog) == []
    assert dialog.table.rowCount() == 1 and dialog.table.item(0, 5).text() == ID
    assert dialog.table.item(0, 1).text() == "at the start"
    assert not dialog.restore_button.isEnabled(), "nothing selected yet"
    dialog.table.selectRow(0)
    texts = [str(entry) for entry in widgets.menu_entries(widgets.menu_of(dialog.table))]
    assert any("Restore…" in text for text in texts) and any("Take one now" in text for text in texts)
    recorded.answers.append((0, json.dumps(CHANGES), ""))
    dialog.changes_button.click()
    assert recorded.calls[-1] == ("checkpoint", "diff", ID, "--json")
    shown = dialog.detail.toPlainText()
    assert "M /work/src/a.py" in shown and "A /work/new.txt" in shown and "D /work/old.txt" in shown
    # Restore…: the preview first (a dry run), then asked, then the restore - with the paths chosen.
    dialog.paths.setText("src/a.py, /work/old.txt")
    recorded.answers += [(0, json.dumps(CHANGES), ""), (0, json.dumps({**CHANGES, "before": "20261117-130000-fedcba",
                                                                        "restored": True}), ""),
                         (0, json.dumps(LISTED), "")]
    dialog.restore_button.click()
    assert recorded.calls[-3:] == [
        ("checkpoint", "restore", ID, "--dry-run", "--json", "--path=src/a.py", "--path=/work/old.txt"),
        ("checkpoint", "restore", ID, "--yes", "--json", "--path=src/a.py", "--path=/work/old.txt"),
        ("checkpoint", "list", "web", "--json")], "the preview, then the restore, then the list read again"
    [(action, text)] = asked
    # (each count says what happens to its files: "1 changed, 1 added and 1 deleted files are replaced or removed"
    # said deleted files were removed - they are put back; audit 2026-11-20)
    assert action == "checkpoint-restore" and "pause it first" in text
    assert "Files changed since then: 1 (their content of then comes back); added since: 1 (removed); deleted " \
        "since: 1 (put back)" in text, text
    assert "20261117-130000-fedcba" in dialog.status.text()
    # Delete: asked first.
    asked.clear()
    dialog.table.selectRow(0)
    recorded.answers += [(0, "", ""), (0, "[]", "")]
    dialog.delete_button.click()
    assert asked[0][0] == "checkpoint-delete" and ("checkpoint", "delete", ID, "--yes") in recorded.calls
    assert dialog.table.rowCount() == 0 and "No checkpoints" in dialog.status.text()
    # Take one now: with its label.
    dialog.label.setText("before the refactoring")
    recorded.answers += [(0, json.dumps({"id": ID, "files": 3}), ""), (0, json.dumps(LISTED), "")]
    dialog.take_button.click()
    assert ("checkpoint", "create", "web", "--json", "--label=before the refactoring") in recorded.calls
    assert dialog.table.rowCount() == 1
    # A failure is said in the dialog.
    recorded.answers.append((1, "", "sandbox-ai checkpoint list: no start named web has checkpoints"))
    dialog.load()
    assert dialog.table.rowCount() == 0
    recorded.answers.append((1, "", "sandbox-ai checkpoint list: the store is damaged"))
    dialog.load()
    assert "the store is damaged" in dialog.status.text()
    dialog.deleteLater()


def test_e2e_a_deleted_workspace_is_put_back_through_the_real_sandbox_ai(qapp, env):
    """The real `sandbox-ai checkpoint` of this checkout: a start's checkpoint (as its launcher takes it), the agent
    deletes its files, the dialog shows what changed and restores it."""
    tmp_path, asked = env
    home = Path(os.environ.get("SANDBOX_AI_HOME") or Path(backend.shutil.which("sandbox-ai")).resolve().parent)
    spec = importlib.util.spec_from_file_location("checkpoints_for_app_test", home / "sandbox/checkpoints.py")
    C = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(C)
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "src/app.py").write_text("print('mine')\n")
    (ws / ".git").mkdir()
    (ws / ".git/HEAD").write_text("ref: refs/heads/main\n")
    roots = [{"host": str(ws), "sandbox": "/work", "kind": "workspace"}]
    meta = C.take(str(ws), roots, kind="start", run="codex-1", start="web", name="web", agent="codex")
    C.record_run("web", {"run": "codex-1", "start": "web", "name": "web", "agent": "codex", "project": str(ws),
                         "roots": roots, "checkpoint": meta["id"]})
    import shutil
    shutil.rmtree(ws / "src")
    shutil.rmtree(ws / ".git")
    from sandbox_ai_app.checkpoints_dialog import CheckpointsDialog
    dialog = CheckpointsDialog(None, backend.SandboxAI(), "web")
    assert dialog.table.rowCount() == 1, dialog.status.text()
    dialog.table.selectRow(0)
    dialog.changes_button.click()
    assert "D /work/src/app.py" in dialog.detail.toPlainText() and "D /work/.git/HEAD" in dialog.detail.toPlainText()
    dialog.restore_button.click()
    assert asked and asked[0][0] == "checkpoint-restore", dialog.status.text()
    assert (ws / "src/app.py").read_text() == "print('mine')\n" and (ws / ".git/HEAD").exists()
    assert dialog.table.rowCount() == 2, "the restore's own checkpoint is listed (it undoes the restore)"
    dialog.deleteLater()
