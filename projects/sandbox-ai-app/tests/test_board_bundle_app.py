"""Export… and Import… of a board or session offscreen (`sandbox-ai board export|import`): the Board page's buttons and
canvas menu, the Sandboxes page's button and right click, the dialogs - against stand-ins that record what they were
asked, and once end to end through the real sandbox-ai of this checkout (a board exported to a file, that file
checked and imported, the board shown). The GUI thread never runs the commands (BLOCKING_SAI: tests/test_references)."""
import json
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, board as B, widgets  # noqa: E402

from test_accessibility import unnamed  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


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
    monkeypatch.setenv("SANDBOX_AI_MCP_NO_IMAGE_CHECK", "1")
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    from sandbox_ai_app import bundle_dialogs
    monkeypatch.setattr(bundle_dialogs, "background", at_once)
    asked = []

    def ask_first(parent, action, title, text, accept, detail=None, details=None, reject=None):
        if backend.needs_confirm(action, detail) is None:
            return True
        asked.append((action, text))
        return True
    monkeypatch.setattr(bundle_dialogs, "ask_first", ask_first)
    return tmp_path, asked


@pytest.fixture
def recorded(env, monkeypatch):
    """A SandboxAI whose commands are recorded (args, input) and answered from ANSWERS (a list, first first)."""
    sai = backend.SandboxAI()
    sai.calls, sai.answers = [], []

    def call(*args, timeout=120, input=None, argv=None):
        sai.calls.append((args, input))
        return sai.answers.pop(0) if sai.answers else (0, "{}", "")
    monkeypatch.setattr(sai, "call", call)
    return sai


def board_page(sai):
    from sandbox_ai_app.board_view import BoardPage
    return BoardPage(sai, lambda argv, title, embedded=None, cwd=None: None, lambda: {"board_grid": 20})


# -- the command line --

def test_the_words_of_board_export_and_import_are_checked():
    assert backend.board_export_args("/tmp/x.json", session="web") == [
        "board", "export", "--session=web", "--json", "--", "/tmp/x.json"]
    assert backend.board_export_args("/tmp/x.tar", board=True, host="ct1", images=True) == [
        "board", "export", "--board=-", "--json", "--images", "--host=ct1", "--", "/tmp/x.tar"]
    assert backend.board_import_args("/tmp/x.json", "ct1", as_name="copy", maps=[("<folder:proj>", "/srv/p")],
                                     values=[("db-password", "dev")], replaces=[("mcp", "own")], images=True,
                                     check=False) == [
        "board", "import", "--json", "--yes", "--as=copy", "--map=<folder:proj>=/srv/p", "--value=db-password=dev",
        "--replace=mcp:own", "--images", "--host=ct1", "--", "/tmp/x.json"]
    for bad in ((("relative.json",), {"session": "web"}), (("/x",), {}), (("/x",), {"session": "-web"}),
                (("/x",), {"session": "web", "host": "-x"}), (("/x",), {"session": "web", "board": True})):
        with pytest.raises(ValueError):
            backend.board_export_args(*bad[0], **bad[1])
    for kwargs in ({"as_name": "Bad Name"}, {"maps": [("<folder:a>", "relative")]}, {"values": [("x", "a\nb")]},
                   {"replaces": [("secret", "x")]}, {"renames": [("mcp", "a", "-b")]}):
        with pytest.raises(ValueError):
            backend.board_import_args("/tmp/x.json", **kwargs)


# -- the Board page --

def test_the_board_page_exports_the_board_shown_with_its_state(qapp, recorded, env, monkeypatch):
    tmp_path, _ = env
    page = board_page(recorded)
    assert page.export_button.text() == "Export…" and page.import_button.text() == "Import…"
    from PySide6.QtCore import QPointF
    texts = [action.text() for action in page.canvas_menu(QPointF(-5000, -5000)).actions()]
    assert "Export…" in texts and "Import…" in texts
    project = tmp_path / "proj"
    project.mkdir()
    page.add_node("workspace", path=str(project))
    opened = []
    from sandbox_ai_app import bundle_dialogs

    def run(dialog):
        opened.append(dialog)
        assert unnamed(dialog) == []
        assert "never" in dialog.privacy.text().lower() and "pass it on knowingly" in dialog.privacy.text()
        dialog.path.setText(str(tmp_path / "out.json"))
        dialog.images.setChecked(True)
        assert dialog.path.text().endswith(".tar")
        recorded.answers.append((0, json.dumps({"written": "x", "secrets": ["GH"], "withheld": ["--copy-auth: never"],
                                                "folders": [str(project)]}), ""))
        dialog.export_button.click()
        return 0
    monkeypatch.setattr(bundle_dialogs.ExportDialog, "exec", run)
    page.export_button.click()
    args, given = recorded.calls[-1]
    assert args[:4] == ("board", "export", "--board=-", "--json") and "--images" in args
    assert args[-1] == str(tmp_path / "out.tar")
    document = json.loads(given)
    assert document["schema"] == "sandbox-ai-board" and document["agent"] == "claude"
    assert document["options"][document["options"].index("--workspace") + 1] == str(project)
    assert any(node["kind"] == "workspace" for node in document["state"]["nodes"])
    dialog = opened[0]
    assert "Secrets named (never their values): GH" in dialog.output.toPlainText()
    assert "Not in it: --copy-auth: never" in dialog.output.toPlainText()
    page.deleteLater()


PLAN = {"bundle": {"name": "demo", "source": {"kind": "session", "name": "web"}, "sandbox_ai": "0.1.99"},
        "board": "demo", "folders": [{"marker": "<folder:proj>", "default": "/old/proj", "label": "project folder",
                                      "to": "/old/proj", "chosen": False}],
        "values": [{"marker": "<text:db-password>", "label": "db POSTGRES_PASSWORD", "given": False}],
        "create": [{"kind": "mcp", "name": "own"}], "reuse": [], "replace": [],
        "missing": {"secrets": [{"name": "GH", "fix": "sandbox-ai secrets set GH"}]},
        "withheld": ["--copy-auth: never goes along"], "notes": [], "warnings": [],
        "problems": [{"kind": "value", "text": "<text:db-password> was withheld", "fix": "--value db-password=VALUE"},
                     {"kind": "mcp", "text": "this machine has an MCP server own already, defined otherwise",
                      "fix": "--rename mcp:own=NEWNAME, or --replace mcp:own"}],
        "command": None, "start": None}


def test_import_checks_first_then_imports_what_was_chosen_and_shows_the_board(qapp, recorded, env, monkeypatch):
    tmp_path, asked = env
    bundle = tmp_path / "demo.json"
    bundle.write_text("{}")
    page = board_page(recorded)
    from sandbox_ai_app import bundle_dialogs
    shown = []
    monkeypatch.setattr(page, "show_imported", lambda document, host: shown.append((document, host)))
    there = tmp_path / "there"
    there.mkdir()

    def run(dialog):
        assert unnamed(dialog) == []
        assert not dialog.import_button.isEnabled() and "Choose the bundle" in dialog.import_button.toolTip()
        dialog.path.setText(str(bundle))
        assert not dialog.import_button.isEnabled() and "Check first" in dialog.import_button.toolTip()
        recorded.answers.append((1, json.dumps(PLAN), ""))
        dialog.check_button.click()
        assert recorded.calls[-1][0] == ("board", "import", "--json", "--check", "--", str(bundle))
        text = dialog.output.toPlainText()
        for said in ("Missing here (secrets): GH - sandbox-ai secrets set GH", "Not in the bundle: --copy-auth",
                     "Problems:", "--value db-password=VALUE", "Created: mcp own"):
            assert said in text, (said, text)
        assert "Not importable as it is" in dialog.status.text()
        assert set(dialog.folder_edits) == {"<folder:proj>"} and set(dialog.value_edits) == {"db-password"}
        # A withheld value is a secret's field (widgets.secret_value): hidden unless its "Show value" toggle is on.
        secret = dialog.value_edits["db-password"]
        secret.show_toggle.setChecked(False)
        assert secret.echoMode() == QtWidgets.QLineEdit.EchoMode.Password
        secret.show_toggle.setChecked(True)
        assert secret.echoMode() == QtWidgets.QLineEdit.EchoMode.Normal
        secret.show_toggle.setChecked(False)
        dialog.folder_edits["<folder:proj>"][1].setText(str(there))
        dialog.value_edits["db-password"].setText("dev-only")
        dialog.replace.setChecked(True)
        assert not dialog.import_button.isEnabled(), "a change of a folder or value: checked again first"
        ok = {**PLAN, "problems": [], "command": "sandbox-ai claude --no-config", "replace": [
            {"kind": "mcp", "name": "own"}]}
        recorded.answers.append((0, json.dumps(ok), ""))
        dialog.check_button.click()
        args = recorded.calls[-1][0]
        assert f"--map=<folder:proj>={there}" in args and "--value=db-password=dev-only" in args
        assert "--replace=mcp:own" in args and "--check" in args
        assert dialog.import_button.isEnabled() and "It can be imported" in dialog.status.text()
        document = {"schema": "sandbox-ai-board", "version": 1, "name": "demo", "agent": "claude",
                    "options": ["--workspace", str(there)], "agent_args": []}
        recorded.answers.append((0, json.dumps({**ok, "written": True, "board_document": document}), ""))
        dialog.import_button.click()
        assert "--yes" in recorded.calls[-1][0] and "--check" not in recorded.calls[-1][0]
        assert asked and asked[-1][0] == "board-import" and "mcp own" in asked[-1][1]
        assert "Imported as demo" in dialog.status.text() and not dialog.import_button.isEnabled()
        return 0
    monkeypatch.setattr(bundle_dialogs.ImportDialog, "exec", run)
    page.import_button.click()
    assert shown and shown[0][0]["name"] == "demo" and shown[0][1] == "local"
    page.deleteLater()


def test_an_imported_board_keeps_its_places_bridges_and_team(qapp, env):
    tmp_path, _ = env
    sai = backend.SandboxAI()
    old = B.from_argv(sai, "claude", ["--workspace", str(tmp_path), "--allow-pypi"])
    bridge = old.add("bridge", peer="other", direction="out")
    old.first("network").x, old.first("network").y = 999.0, 444.0
    document = {"agent": "claude", "options": ["--workspace", str(tmp_path), "--allow-pypi"], "agent_args": [],
                "state": old.state()}
    board, folder = B.imported_board(sai, document)
    assert folder == str(tmp_path)
    assert (board.first("network").x, board.first("network").y) == (999.0, 444.0)
    assert board.first("bridge").props["peer"] == "other" and board.connected(board.first("bridge"))
    assert bridge.id in [n.id for n in old.nodes]
    assert "--allow-pypi" in B.to_argv(sai, board)
    page = board_page(sai)
    assert page.show_imported({**document, "name": "demo"}, "local") is True
    assert page.board.first("bridge") is not None and page.directory and str(tmp_path) in str(page.directory)
    page.deleteLater()


# -- the Sandboxes page --

def session(name="web", state="stopped", host="local"):
    if host != "local":
        return backend.Instance("session", f"remote:{host}:{name}", name, host=host, state=state, agent="codex")
    return backend.Instance("session", f"session:{name}", name, session=name, state=state, agent="codex")


def test_sandboxes_export_of_a_session_here_or_on_a_host_and_on_the_right_click(qapp, recorded, env, monkeypatch):
    tmp_path, _ = env
    from sandbox_ai_app import bundle_dialogs, ui
    monkeypatch.setattr(recorded, "hosts_supported", lambda: True)
    monkeypatch.setattr(recorded, "host_entries", lambda: [{"name": "local", "kind": "local"},
                                                             {"name": "ct1", "kind": "ssh", "ssh": "a@192.0.2.1"}])
    for instance, why in ((session(), None), (session(state="running"), None), (session(host="ct1"), None),
                          (backend.Instance("run", "run:x", "x", run="codex-1", state="running"), "Only for sessions"),
                          (None, "Select a row")):
        page = ui.SandboxesPage(recorded, lambda *a, **k: None)
        monkeypatch.setattr(page, "current", lambda instance=instance: instance)
        page.update_buttons()
        button = page.transfer_buttons["export"]
        if why is None:
            assert button.isEnabled(), button.toolTip()
            entries = widgets.menu_entries(widgets.menu_of(page.table))
            assert any("Export…" in str(entry) for entry in entries), entries
        else:
            assert not button.isEnabled() and why in button.toolTip(), (why, button.toolTip())
        page.close()
    opened = []

    def run(dialog):
        opened.append((dialog.session, dialog.host))
        dialog.path.setText(str(tmp_path / "web.json"))
        dialog.export_button.click()
        return 0
    monkeypatch.setattr(bundle_dialogs.ExportDialog, "exec", run)
    for instance, host in ((session(), "local"), (session(host="ct1"), "ct1")):
        page = ui.SandboxesPage(recorded, lambda *a, **k: None)
        monkeypatch.setattr(page, "current", lambda instance=instance: instance)
        page.update_buttons()
        page.transfer_buttons["export"].click()
        args = recorded.calls[-1][0]
        assert args[:4] == ("board", "export", "--session=web", "--json") and args[-1] == str(tmp_path / "web.json")
        assert ("--host=ct1" in args) == (host == "ct1")
        page.close()
    assert opened == [("web", "local"), ("web", "ct1")]


# -- end to end: the real sandbox-ai of this checkout ----------------------------------------------------------------

def test_e2e_export_then_import_through_the_real_sandbox_ai(qapp, env, monkeypatch):
    """The board shown exported by the real `sandbox-ai board export` (its file written), that file checked and
    imported by the real `sandbox-ai board import` into another folder of this machine, and shown on the board."""
    tmp_path, asked = env
    sai = backend.SandboxAI()
    page = board_page(sai)
    project, there = tmp_path / "proj", tmp_path / "there"
    project.mkdir()
    there.mkdir()
    page.add_node("workspace", path=str(project))
    page.set_prop(page.board.first("network").id, "--allow-host", ["pypi.org"])
    from sandbox_ai_app import bundle_dialogs
    out = tmp_path / "board.json"

    def export(dialog):
        dialog.path.setText(str(out))
        dialog.export_button.click()
        return 0
    monkeypatch.setattr(bundle_dialogs.ExportDialog, "exec", export)
    page.export_board()
    document = json.loads(out.read_text())
    assert document["schema"] == "sandbox-ai-bundle" and str(project) not in json.dumps(document["start"])
    assert document["folders"][0]["default"] == str(project)

    def importing(dialog):
        dialog.path.setText(str(out))
        dialog.name.setText("imported")
        dialog.check_button.click()
        assert dialog.plan and not dialog.plan["problems"], dialog.output.toPlainText()
        dialog.folder_edits[document["folders"][0]["marker"]][1].setText(str(there))
        dialog.check_button.click()
        dialog.import_button.click()
        assert dialog.done_with == "imported", dialog.output.toPlainText()
        return 0
    monkeypatch.setattr(bundle_dialogs.ImportDialog, "exec", importing)
    page.import_board()
    assert asked and asked[-1][0] == "board-import"
    assert page.board.first("workspace").props["path"] == str(there)
    assert "--allow-host pypi.org" in page.preview.toPlainText()
    kept = json.loads((tmp_path / "config/sandbox-ai/boards/imported.json").read_text())
    assert kept["options"][kept["options"].index("--workspace") + 1] == str(there)
    page.deleteLater()
