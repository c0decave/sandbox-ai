"""The add form and the properties of an MCP server show its folder defaults (user 2026-11-22: "alle mcp server
sollten bzgl. /work pfad defaults haben, sodass sie simpel und fast hinzugefügt werden können"; "--mount /work=/app:rw
... es geht doch um container und nicht die maschine???"): each field is "Folder on HOST for the container's TARGET",
an empty one is its default - shown in the field ("the start's workspace", the server's own folder) - and OK needs
nothing typed. On another host the default is made there by sandbox-ai, never by the app over SSH."""
import json
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")

from sandbox_ai_app import backend, mcp_fields as MF, widgets  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")
DIGEST = "sha256:" + "c" * 64
BINWALK = {"id": "hub:binwalk", "name": "binwalk", "title": "binwalk", "kind": "oci", "image": "ghcr.io/x/binwalk",
           "digest": DIGEST, "network": "none", "_signature": {"state": "good"},
           "mounts": [{"target": "/work", "required": True, "description": "The firmware (read-only is enough)"},
                      {"target": "/out", "description": "A folder for extracted files (tick writable)"}]}


@pytest.fixture
def homes(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    return tmp_path


@pytest.fixture
def page(qapp, homes, monkeypatch):
    from sandbox_ai_app import ui
    sai = backend.SandboxAI()
    monkeypatch.setattr(ui, "background", lambda function, *args, done, **kw: done(function(*args), None))
    sai.ran = []

    def host_run(host, *args, timeout=120, input=None):
        sai.ran.append((host, *args))
        return 0, "sandbox-ai mcp: added", ""
    monkeypatch.setattr(sai, "host_run", host_run)
    shown = ui.McpPage(sai)
    shown.ui = ui
    yield shown
    shown.close()
    shown.deleteLater()


def labels(dialog):
    return [label.text() for label in dialog.findChildren(QtWidgets.QLabel)]


def test_the_add_form_shows_the_defaults_and_needs_nothing_typed(page, homes):
    dialog = page.ui.AddServerDialog(page, dict(BINWALK), "local")
    ok = dialog.buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok)
    work, work_writable = dialog.mounts["/work"]
    out, out_writable = dialog.mounts["/out"]
    shown = " ".join(labels(dialog))
    assert "Folder on this machine for the container's /work" in shown
    assert "Folder on this machine for the container's /out" in shown
    assert work.value() == "" and "the start's workspace" in work.edit.placeholderText()
    own = homes / "data" / "sandbox-ai" / "mcp-folders" / "binwalk" / "out"
    assert str(own) in out.edit.placeholderText() and "its own folder" in out.edit.placeholderText()
    assert not work_writable.isChecked() and out_writable.isChecked(), "the modes its entry declares"
    assert ok.isEnabled() and not dialog.mount_reasons["/work"].text(), "the required /work: its default"
    assert dialog.values("hub:binwalk")["mounts"] == [], "nothing given: sandbox-ai there makes the defaults"
    assert not [word for word in dialog.arguments("hub:binwalk") if word.startswith("--mount")]
    work_writable.setChecked(True)
    assert dialog.values("hub:binwalk")["mounts"] == [("/work", "default", "rw")]
    assert "--mount=/work=default:rw" in dialog.arguments("hub:binwalk")
    dialog.deleteLater()


def test_the_name_for_mcp_names_its_own_folder(page, homes):
    dialog = page.ui.AddServerDialog(page, dict(BINWALK), "local")
    dialog.local.setText("fw2")
    out, _ = dialog.mounts["/out"]
    assert str(homes / "data" / "sandbox-ai" / "mcp-folders" / "fw2" / "out") in out.edit.placeholderText()
    dialog.deleteLater()


def test_on_another_host_the_defaults_are_made_there_by_sandbox_ai(page, homes, monkeypatch):
    """An entry that host's `mcp catalog --json` lists with its defaults: nothing to type, `mcp add` runs there without
    --mount - no folder looked at or made over SSH."""
    entry = {"name": "binwalk", "_signature": {"state": "good"},
             "mounts": [{"target": "/work", "required": True, "default": "workspace", "mode": "ro"},
                        {"target": "/out", "default": "own", "mode": "rw"}]}
    dialog = page.ui.AddServerDialog(page, dict(entry), "lab")
    ok = dialog.buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok)
    shown = " ".join(labels(dialog))
    assert "Folder on lab for the container's /work" in shown and ok.isEnabled()
    out, out_writable = dialog.mounts["/out"]
    assert "its own folder on lab" in out.edit.placeholderText() and out_writable.isChecked()
    dialog.deleteLater()
    looked = []
    monkeypatch.setattr(page, "look_at_host_folders", lambda *a, **k: looked.append(a))

    def run(dialog):
        dialog.accept()
        return dialog.result()
    monkeypatch.setattr(widgets, "exec_dropped", run)
    page.catalog_of = ("lab", "")
    page.add_entry("hub:binwalk", dict(entry))
    assert looked == [], "no folder of lab looked at (or made) for its defaults"
    adds = [args for args in page.sai.ran if args[1:3] == ("mcp", "add")]
    assert adds and adds[0][0] == "lab" and not [w for w in adds[0] if str(w).startswith("--mount")], adds


def test_properties_show_the_defaults_and_go_back_to_them(qapp, homes):
    sai = backend.SandboxAI()
    own = str(homes / "data" / "sandbox-ai" / "mcp-folders" / "binwalk" / "out")
    definition = {"image": f"ghcr.io/x/binwalk@{DIGEST}", "network": "none",
                  "mounts": [{"target": "/work", "host": "@workspace", "mode": "ro"},
                             {"target": "/out", "host": own, "mode": "rw"}]}
    info = {"name": "binwalk", "status": "stopped", "kind": "image", "own": True, "definition": definition,
            "folders": [{"target": "/work", "host": "@workspace", "mode": "ro", "default": "workspace"},
                        {"target": "/out", "host": own, "mode": "rw", "default": "own"}]}
    dialog = MF.ServerPropertiesDialog(None, sai, info, BINWALK)
    work, _ = dialog.mounts["/work"]
    out, _ = dialog.mounts["/out"]
    assert work.text() == "" and "the start's workspace" in work.placeholderText()
    assert out.text() == "" and own in out.placeholderText()
    assert "Folder on this machine for the container's /work" in " ".join(labels(dialog))
    assert dialog.arguments() is None, "nothing changed: no mcp set"
    firmware = homes / "fw"
    firmware.mkdir()
    work.setText(str(firmware))
    assert "--mount=/work=" + str(firmware) + ":ro" in dialog.arguments()
    assert "--mount=/out=default:rw" in dialog.arguments(), "the others stay their defaults"
    dialog.deleteLater()
    typed = {**info, "definition": {**definition, "mounts": [{"target": "/work", "host": str(firmware), "mode": "ro"},
                                                              definition["mounts"][1]]},
             "folders": [{"target": "/work", "host": str(firmware), "mode": "ro", "default": None},
                         info["folders"][1]]}
    dialog = MF.ServerPropertiesDialog(None, sai, typed, BINWALK)
    work, _ = dialog.mounts["/work"]
    assert work.text() == str(firmware)
    work.setText("")
    assert "--mount=/work=default:ro" in dialog.arguments(), "an emptied field: back to its default"
    dialog.deleteLater()


def test_backend_takes_default_as_a_folder():
    args, writable = backend.mcp_add_args("hub:binwalk", mounts=[("/work", "default", "rw")])
    assert "--mount=/work=default:rw" in args and writable == []
    assert "--mount=/out=default:ro" in backend.mcp_set_args("binwalk", mounts=[("/out", "default", "ro")])
    with pytest.raises(ValueError):
        backend.mcp_add_args("hub:binwalk", mounts=[("/work", "defaults", "ro")])
    assert json.dumps(args)  # (plain words)


def test_the_board_says_the_start_s_workspace_never_a_folder_named_at_workspace(qapp, homes):
    """A server bound to the start's workspace on the Board: its node says "the start's workspace", and what the start
    can reach names it so - never a folder "@workspace" judged on disk."""
    from sandbox_ai_app import board as B
    definition = {"image": f"ghcr.io/x/r2@{DIGEST}", "network": "none",
                  "mounts": [{"target": "/work", "host": "@workspace", "mode": "ro"}]}
    rows = dict(B.mcp_summary(definition))
    assert "the start’s workspace → /work (ro)" in rows["Folders"]
    sai = backend.SandboxAI()
    board = B.from_argv(sai, "claude", ["--mcp", "r2"])
    texts = [c.text for c in B.capabilities(board, definitions={"r2": definition})]
    assert any("the start's workspace at /work (read-only)" in text for text in texts), texts
    assert not any("@workspace" in text for text in texts), texts
