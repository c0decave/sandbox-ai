"""An MCP server's limits per server and per board, and its properties (offscreen): the board's MCP node keeps its own
limits (--mcp-limits) with the server's own shown as the default; the MCP page's Properties dialog (sandbox-ai mcp
set) and its Add dialog check every value by sandbox-ai's own rules while it is typed and take nothing that is not
valid - the dialog stays open with the reason; the servers and the catalog have a context menu, a double click and
Enter / Alt+Enter."""
import json
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from sandbox_ai_app import backend, board as B, mcp_fields as MF, widgets  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")
DIGEST = "sha256:" + "a" * 64


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
def sai(homes):
    return backend.SandboxAI()


def define(homes, servers):
    path = homes / "config" / "sandbox-ai" / "mcp.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(servers))


# -- the board: an MCP node's own limits --


def test_a_boards_mcp_limits_round_trip_through_the_command_line(sai):
    board = B.from_argv(sai, "claude", ["--mcp", "pw", "--mcp", "demo", "--mcp-limits", "pw:memory=2g,cpus=2",
                                        "--mcp-limits", "pw:pids=512", "--mcp-limits", "other:memory=1g"])
    pw = next(n for n in board.of_kind("mcp") if n.props["name"] == "pw")
    assert pw.props["limits"] == {"memory": "2g", "cpus": "2", "pids": "512"}
    assert "limits" not in next(n for n in board.of_kind("mcp") if n.props["name"] == "demo").props
    assert board.extra == ["--mcp-limits", "other:memory=1g"], "a server the board does not use: kept as it was"
    argv = B.to_argv(sai, board)
    assert "--mcp-limits" in argv and argv[argv.index("--mcp-limits") + 1] == "pw:memory=2g,cpus=2,pids=512"
    pw.props["limits"] = {}
    assert argv.count("--mcp-limits") == 2 and B.to_argv(sai, board).count("--mcp-limits") == 1
    agent = board.first("agent")
    pw.props["limits"] = {"memory": "4g"}
    board.disconnect(agent.id, pw.id)  # a server not connected: neither it nor its limits
    assert "pw:memory=4g" not in B.to_argv(sai, board)


def test_the_project_file_keeps_the_limits_and_a_dry_run_leaves_out_the_projects_own(sai, tmp_path):
    board = B.from_argv(sai, "claude", ["--workspace", str(tmp_path), "--mcp", "search", "--mcp-limits",
                                        "search:memory=2g"])
    document, _main = B.project_document(sai, board)
    assert "--mcp-limits" in document["options"] and "search:memory=2g" in document["options"]
    node = board.of_kind("mcp")[0]
    node.props.update(project=True, definition={"command": ["uvx", "search"]})
    checked = B.check_argv(sai, board)
    assert "--mcp" not in checked and "--mcp-limits" not in checked, "the project's own server: checked by the board"


@pytest.mark.parametrize("limits,bad", [({"memory": "1024"}, "memory must be"), ({"cpus": "0"}, "cpus must be"),
                                        ({"cpus": "abc"}, "cpus must be"), ({"pids": "8"}, "pids must be 16-4096")])
def test_a_limit_of_the_board_that_is_not_valid_is_an_error_of_its_node(sai, limits, bad):
    board = B.from_argv(sai, "claude", ["--mcp", "demo"])
    node = board.of_kind("mcp")[0]
    node.props["limits"] = limits
    problems = [p for p in B.problems(sai, board) if p.node == node.id]
    assert any(p.level == "error" and bad in p.text for p in problems), problems
    node.props["limits"] = {"memory": "2g", "cpus": "1.5", "pids": "512"}
    assert not [p for p in B.problems(sai, board) if p.node == node.id and p.level == "error"]


def test_the_defaults_a_node_shows_are_the_servers_own(sai, homes):
    define(homes, {"pw": {"image": f"ghcr.io/a/b@{DIGEST}", "network": "full", "memory": "2g", "pids": 512}})
    defaults = B.mcp_defaults(sai, sai.mcp_definitions()["pw"])
    assert defaults["memory"] == "2g" and defaults["pids"] == 512 and float(defaults["cpus"]) >= 1
    assert B.mcp_defaults(sai, None) is None


@pytest.fixture
def board_page(qapp, homes, monkeypatch):
    (homes / "cwd").mkdir()
    monkeypatch.chdir(homes / "cwd")
    from sandbox_ai_app.board_view import BoardPage
    view = BoardPage(backend.SandboxAI(), lambda *a, **kw: None, lambda: {"board_grid": 20})
    yield view
    view.deleteLater()


def test_the_inspector_of_an_mcp_node_edits_its_own_limits_and_resets_them(board_page, homes):
    define(homes, {"pw": {"image": f"ghcr.io/a/b@{DIGEST}", "network": "none", "memory": "1g"}})
    node = board_page.add_node("mcp", name="pw") or board_page.board.of_kind("mcp")[0]
    board_page.inspect(node)
    fields = board_page.inspector_holder.findChildren(MF.LimitsFields)[0]
    assert fields.fields["memory"].placeholderText() == "default: 1g", "the server's own, shown as the default"
    assert fields.fields["pids"].placeholderText() == "default: 128"
    fields.fields["memory"].setText("1024")  # no unit
    assert not fields.valid() and fields.fields["memory"].property("invalid") is True
    assert not fields.reasons["memory"].isHidden() and "512m or 2g" in fields.reasons["memory"].text()
    fields.commit()
    assert "limits" not in board_page.board.node(node.id).props, "nothing that is not valid is kept"
    fields.fields["memory"].setText("2g")
    fields.fields["cpus"].setText("2")
    assert fields.valid() and fields.fields["memory"].property("invalid") is False
    fields.commit()
    assert board_page.board.node(node.id).props["limits"] == {"memory": "2g", "cpus": "2"}
    assert "--mcp-limits pw:memory=2g,cpus=2" in board_page.preview.toPlainText()
    board_page.inspect(board_page.board.node(node.id))
    fields = board_page.inspector_holder.findChildren(MF.LimitsFields)[0]
    assert fields.fields["memory"].text() == "2g"
    fields.reset_button.click()
    assert not board_page.board.node(node.id).props.get("limits")
    assert "--mcp-limits" not in board_page.preview.toPlainText()
    buttons = [b.text() for b in board_page.inspector_holder.findChildren(QtWidgets.QPushButton)]
    assert "Defaults for every start…" in buttons


def test_the_context_menu_of_an_mcp_node_leads_to_the_servers_defaults(board_page, homes):
    node = board_page.add_node("mcp", name="demo") or board_page.board.of_kind("mcp")[0]
    asked = []
    board_page.server_properties.connect(lambda name, host: asked.append((name, host)))
    item = board_page.items[node.id]
    menu = board_page.canvas_menu(item.sceneBoundingRect().center())
    texts = [a.text() for a in menu.actions()]
    assert "Properties" in texts and "Defaults for every start…" in texts
    next(a for a in menu.actions() if a.text() == "Defaults for every start…").trigger()
    assert asked == [("demo", "local")]


# -- the limit fields themselves --


def test_the_limit_fields_say_why_at_once_and_give_the_usual_form(qapp, sai):
    fields = MF.LimitsFields(MF.limit_checks(sai.module("mcp")), {"memory": "512m", "cpus": "4", "pids": 128})
    assert fields.fields["cpus"].placeholderText() == "default: 4" and fields.values() == {} and fields.valid()
    for key, value in (("memory", "2"), ("cpus", "0"), ("cpus", "abc"), ("pids", "5000")):
        fields.fields[key].setText(value)
        assert key in fields.problems() and not fields.reasons[key].isHidden(), (key, value)
        fields.fields[key].setText("")
        assert key not in fields.problems() and fields.reasons[key].isHidden()
    fields.set_values({"memory": "2G", "cpus": "1.50", "pids": "512"})
    assert fields.values() == {"memory": "2g", "cpus": "1.5", "pids": 512}
    committed = []
    fields.committed.connect(lambda: committed.append(True))
    fields.reset()
    assert fields.values() == {} and committed == [True]


def test_without_the_rules_of_an_older_sandbox_ai_nothing_is_refused_here(qapp):
    fields = MF.LimitsFields(MF.limit_checks(None), None)
    fields.fields["memory"].setText("anything")
    assert fields.valid() and fields.values() == {"memory": "anything"}
    assert fields.fields["memory"].placeholderText() == "default"


# -- the MCP page: Properties…, the menus, the Add dialog --


@pytest.fixture
def page(qapp, homes, sai, monkeypatch):
    from sandbox_ai_app import ui
    monkeypatch.setattr(ui, "background", lambda function, *args, done, **kw: done(function(*args), None))
    monkeypatch.setattr(MF, "background", lambda function, *args, done, **kw: done(function(*args), None))
    sai.ran, sai.answers = [], []

    def host_run(host, *args, timeout=120, input=None):
        sai.ran.append(args)
        return sai.answers.pop(0) if sai.answers else (0, "", "")
    monkeypatch.setattr(sai, "host_run", host_run)
    shown = ui.McpPage(sai)
    shown.ui = ui
    yield shown
    shown.close()


def select(table, name):
    for row in range(table.rowCount()):
        if table.item(row, 0).text() == name:
            table.selectRow(row)
            return
    raise AssertionError(f"{name} is not listed")


def test_the_servers_menu_repeats_the_buttons_and_properties_opens_by_double_click_and_alt_enter(page, homes,
                                                                                                 monkeypatch):
    define(homes, {"pw": {"image": f"ghcr.io/a/b@{DIGEST}", "network": "none"}})
    page.refresh()
    select(page.servers, "pw")
    from sandbox_ai_app import kits_page
    menu = kits_page.context_menu(page.servers, page.server_buttons)
    assert [a.text() for a in menu.actions()] == ["Start…", "Stop", "Attach to…", "Properties…"]
    opened = []
    monkeypatch.setattr(page, "properties", lambda *args: opened.append(args))
    page.servers.setFocus()
    index = page.servers.model().index(page.servers.currentRow(), 0)
    page.servers.doubleClicked.emit(index)
    assert len(opened) == 1
    shortcuts = [s for s in page.servers.findChildren(page.ui.QShortcut) if s.key().toString() in ("Alt+Return",
                                                                                                   "Alt+Enter")]
    assert len(shortcuts) == 2
    shortcuts[0].activated.emit()
    assert len(opened) == 2
    assert page.servers.contextMenuPolicy() == Qt.ContextMenuPolicy.CustomContextMenu


def test_properties_change_the_limits_and_keep_the_dialog_open_until_they_are_valid(page, homes, monkeypatch):
    define(homes, {"pw": {"image": f"ghcr.io/a/b@{DIGEST}", "network": "full", "memory": "1g"}})
    page.refresh()
    seen = []

    def exec_(dialog):
        seen.append(dialog)
        ok = dialog.buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok)
        assert dialog.limits.fields["memory"].text() == "1g" and dialog.network.currentText() == "full"
        dialog.limits.fields["memory"].setText("1024")
        assert not ok.isEnabled() and "512m or 2g" in dialog.limits.reasons["memory"].text()
        dialog.try_accept()
        assert dialog.result() != QtWidgets.QDialog.DialogCode.Accepted and not dialog.error.isHidden()
        assert page.sai.ran == [], "nothing runs with a value that is not valid"
        dialog.limits.fields["memory"].setText("2g")
        dialog.limits.fields["cpus"].setText("2")
        assert ok.isEnabled()
        page.sai.answers = [(1, "", "sandbox-ai mcp: MCP server pw: a VPN needs network full\n")]
        dialog.vpn.setText("anon")
        dialog.network.setCurrentText("none")
        dialog.try_accept()
        assert dialog.result() != QtWidgets.QDialog.DialogCode.Accepted, "a refusal of sandbox-ai: still open"
        assert "a VPN needs network full" in dialog.error.text()
        dialog.vpn.setText("")
        dialog.network.setCurrentText("full")
        page.sai.answers = [(0, "MCP server pw: cpus, memory set\n", "")]
        dialog.try_accept()
        return dialog.result()
    monkeypatch.setattr(MF.ServerPropertiesDialog, "exec", exec_)
    page.properties("pw")
    assert seen and seen[0].result() == QtWidgets.QDialog.DialogCode.Accepted
    assert page.sai.ran[-1] == ("mcp", "set", "pw", "--memory=2g", "--cpus=2")
    assert page.sai.ran[0][:3] == ("mcp", "set", "pw") and "--no-vpn" not in page.sai.ran[0]


def test_properties_of_a_server_that_is_not_yours_change_nothing(page, monkeypatch):
    seen = []
    monkeypatch.setattr(MF.ServerPropertiesDialog, "exec", lambda dialog: seen.append(dialog) or 0)
    page.properties("demo")
    dialog, = seen
    assert not dialog.buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).isEnabled()
    assert "not a server of yours" in dialog.error.text() and not dialog.limits.isEnabled()


def test_the_set_arguments_are_checked_for_their_form():
    assert backend.mcp_set_args("pw", memory="2g", cpus="1.5", pids=512, network="full", vpn=None, env=[],
                                mounts=[("/data", "/srv/x", "rw")], stop=True) == [
        "mcp", "set", "pw", "--memory=2g", "--cpus=1.5", "--pids=512", "--network=full", "--mount=/data=/srv/x:rw",
        "--no-env", "--no-vpn", "--stop"]
    assert backend.mcp_set_args("pw", memory="default") == ["mcp", "set", "pw", "--memory=default"]
    for bad in ({"memory": "2g; rm"}, {"cpus": "two"}, {"network": "some"}, {"vpn": "A B"}, {"swap": "1g"},
                {"mounts": [("/data", "/x:y", "ro")]}):
        with pytest.raises(ValueError):
            backend.mcp_set_args("pw", **bad)
    assert "--cpus=2" in backend.mcp_add_args("hub:binwalk", cpus="2")[0]


ENTRY = {"id": "hub:binwalk", "name": "binwalk", "title": "binwalk", "kind": "oci", "image": "ghcr.io/x/binwalk",
         "digest": DIGEST, "network": "none", "description": "Firmware", "notes": "Give it the firmware read-only.",
         "mounts": [{"target": "/work", "required": True}, {"target": "/out"}], "limits": {"memory": "1g"},
         "_signature": {"state": "good"}}


def test_the_add_dialog_checks_the_limits_and_folders_before_it_closes(page, homes):
    dialog = page.ui.AddServerDialog(page, dict(ENTRY), "local")
    ok = dialog.buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok)
    assert dialog.limits.fields["memory"].placeholderText() == "default: 1g", "what the catalog recommends"
    work_field, _ = dialog.mounts["/work"]
    work_field.edit.setText(str(homes / "missing"))
    assert not ok.isEnabled() and "a read-only folder must be there" in dialog.mount_reasons["/work"].text()
    firmware = homes / "firmware"
    firmware.mkdir()
    work_field.edit.setText(str(firmware))
    out_field, out_writable = dialog.mounts["/out"]
    out_field.edit.setText(str(homes / "new" / "out"))
    assert not ok.isEnabled(), "a read-only folder that is not there"
    out_writable.setChecked(True)
    assert ok.isEnabled(), "a writable one is created at add"
    dialog.limits.fields["pids"].setText("8")
    assert not ok.isEnabled()
    dialog.accept()
    assert dialog.result() != QtWidgets.QDialog.DialogCode.Accepted and "16 to 4096" in dialog.error.text()
    dialog.limits.fields["pids"].setText("512")
    values = dialog.values("hub:binwalk")
    assert (values["memory"], values["cpus"], values["pids"]) == (None, None, 512), "empty: the entry's or the default"
    dialog.accept()
    assert dialog.result() == QtWidgets.QDialog.DialogCode.Accepted
    assert not (homes / "new").exists(), "the dialog creates nothing: mcp add does"


def test_the_catalog_opens_add_by_double_click_and_enter_and_has_a_menu(page, monkeypatch):
    page.catalog_loaded(([("hub", dict(ENTRY))], []), None)
    page.catalog.selectRow(0)
    added = []
    monkeypatch.setattr(page, "add", lambda: added.append(True))
    page.catalog.doubleClicked.emit(page.catalog.model().index(0, 0))
    shortcuts = [s for s in page.catalog.findChildren(page.ui.QShortcut)]
    assert {s.key().toString() for s in shortcuts} == {"Return", "Enter"}
    shortcuts[0].activated.emit()
    assert added == [True, True]
    from sandbox_ai_app import kits_page
    menu = kits_page.context_menu(page.catalog, [page.add_button, page.details_button])
    assert [a.text() for a in menu.actions()] == ["Add…", "Details…"]
    shown = []
    monkeypatch.setattr(widgets, "show_text_window", lambda parent, title, text: shown.append((title, text)))
    page.details()
    (title, text), = shown
    assert title == "hub:binwalk" and "Give it the firmware read-only." in text and "/work (required)" in text
    assert "memory 1g" in text


def test_the_german_texts_are_there(qapp, sai):
    from sandbox_ai_app import i18n
    i18n.set_language("de")
    fields = MF.LimitsFields(MF.limit_checks(sai.module("mcp")), {"memory": "512m"})
    fields.fields["memory"].setText("1024")
    assert fields.reasons["memory"].text().startswith("Nicht gültig: eine ganze Zahl mit m oder g")
    assert fields.reset_button.text() == "Auf Standard zurücksetzen"
    assert fields.fields["memory"].placeholderText() == "Standard: 512m"


# -- /tmp per server, what ended a server's process, the catalog's recommended limits --


def test_the_tmp_field_is_a_size_and_never_more_than_the_memory(qapp, sai):
    fields = MF.LimitsFields(MF.limit_checks(sai.module("mcp")), {"memory": "512m", "cpus": "2", "pids": 128,
                                                                   "tmp": "64m"})
    assert fields.fields["tmp"].placeholderText() == "default: 64m"
    fields.fields["tmp"].setText("1024")
    assert "64m or 1g" in fields.problems()["tmp"]
    fields.fields["tmp"].setText("1g")  # more than the default memory 512m
    assert "may not be more than the memory limit 512m" in fields.problems()["tmp"]
    fields.fields["memory"].setText("2g")  # the memory decides: checked again at once
    assert fields.valid() and fields.reasons["tmp"].isHidden()
    assert fields.values() == {"memory": "2g", "tmp": "1g"}
    fields.fields["memory"].setText("768m")
    assert not fields.valid() and not fields.reasons["tmp"].isHidden()


def test_a_board_tmp_above_the_servers_memory_is_an_error(sai, homes):
    define(homes, {"pw": {"image": f"ghcr.io/a/b@{DIGEST}", "network": "none", "memory": "1g"}})
    board = B.from_argv(sai, "claude", ["--mcp", "pw", "--mcp-limits", "pw:tmp=768m"])
    node = board.of_kind("mcp")[0]
    assert node.props["limits"] == {"tmp": "768m"} and "pw:tmp=768m" in B.to_argv(sai, board)
    assert not [p for p in B.problems(sai, board) if p.node == node.id and p.level == "error"]
    node.props["limits"] = {"tmp": "2g"}
    assert any("more than the memory 1g" in p.text for p in B.problems(sai, board) if p.node == node.id)
    node.props["limits"] = {"tmp": "2g", "memory": "4g"}
    assert not [p for p in B.problems(sai, board) if p.node == node.id and p.level == "error"]


def test_properties_offer_the_recommended_limits_and_show_the_last_problem(qapp, sai):
    info = {"name": "playwright", "status": "running", "kind": "image", "own": True,
            "definition": {"image": f"ghcr.io/a/b@{DIGEST}", "network": "full"},
            "builtin_defaults": {"memory": "512m", "cpus": "2", "pids": 128, "tmp": "64m"},
            "below_recommended": {"memory": "2g", "pids": 512, "tmp": "768m"},
            "last_problem": {"time": "2026-11-12T10:00:00Z", "what": "out of memory: the kernel ended 1 process",
                             "why": "its memory limit 512m is used up", "fix": "sandbox-ai mcp set playwright --memory 1g"}}
    dialog = MF.ServerPropertiesDialog(None, sai, info)
    labels = " ".join(label.text() for label in dialog.findChildren(QtWidgets.QLabel))
    assert "Below the limits its catalog recommends: memory 2g, pids 512, tmp 768m." in labels
    assert "its memory limit 512m is used up" in labels and "Fix: sandbox-ai mcp set playwright --memory 1g" in labels
    dialog.recommended_button.click()
    assert dialog.limits.values() == {"memory": "2g", "pids": 512, "tmp": "768m"}
    assert dialog.arguments() == ["mcp", "set", "playwright", "--memory=2g", "--pids=512", "--tmp=768m"]
    dialog.deleteLater()


def test_properties_stay_open_while_sandbox_ai_takes_the_change(qapp, sai):
    """OK sends `mcp set` with the dialog open (its buttons greyed out): Esc or the window's [x] then rejected it - the
    page listed the servers again before the change was made, and sandbox-ai's answer (its refusal too) went
    nowhere. The dialog closes once the answer is there."""
    info = {"name": "pw", "status": "stopped", "kind": "image", "own": True,
            "definition": {"image": f"ghcr.io/a/b@{DIGEST}", "network": "none"}}
    pending = []
    dialog = MF.ServerPropertiesDialog(None, sai, info, submit=lambda arguments, done: pending.append(done))
    dialog.show()
    dialog.limits.fields["memory"].setText("2g")
    dialog.try_accept()
    assert pending and not dialog.buttons.isEnabled()
    dialog.reject()  # Esc, the [x]
    assert dialog.isVisible(), "still open while the change is made"
    pending[0](1, "", "sandbox-ai mcp: refused\n")
    assert "refused" in dialog.error.text() and dialog.buttons.isEnabled()
    dialog.reject()
    assert not dialog.isVisible() and dialog.result() == QtWidgets.QDialog.DialogCode.Rejected
    dialog.deleteLater()


def test_the_servers_status_says_what_ended_a_process_and_that_limits_are_below_the_catalogs(page, homes, monkeypatch):
    define(homes, {"playwright": {"image": f"ghcr.io/a/b@{DIGEST}", "network": "full"}})
    monkeypatch.setattr(page.sai, "host_mcp_notes", lambda host: {"playwright": {
        "problem": {"time": "10:00", "what": "out of memory", "why": "its memory limit 512m is used up",
                    "fix": "sandbox-ai mcp set playwright --memory 1g"}, "below": {"memory": "2g"}}})
    page.refresh()
    row = next(r for r in range(page.servers.rowCount()) if page.servers.item(r, 0).text() == "playwright")
    status = page.servers.item(row, 1)
    assert status.text() == "stopped, a problem at 10:00 · below the catalog's limits"
    tip = page.servers.item(row, 0).toolTip()  # (a row's tooltip: on its first cell)
    assert "its memory limit 512m is used up" in tip and "Fix: sandbox-ai mcp set playwright --memory 1g" in tip
    assert "mcp set playwright --recommended" in tip


def test_the_real_notes_of_this_machine(sai, homes):
    define(homes, {"playwright": {"image": f"ghcr.io/a/b@{DIGEST}", "network": "full"}})
    assert sai.host_mcp_notes("local") == {}
    mcp = sai.module("mcp")
    directory = mcp.state_dir("playwright")
    directory.mkdir(parents=True)
    watch = mcp.ContainerWatch(directory, "playwright", "64m", {"memory": "512m", "pids": "128"})
    watch.ended(137, False)
    notes = sai.host_mcp_notes("local")
    assert "SIGKILL" in notes["playwright"]["problem"]["why"] and notes["playwright"]["below"] == {}
