"""0.1.95, from what the user found (offscreen): an MCP server's variables in its Properties (the MCP page) and on the
board's MCP part (--mcp-env) - a table with each variable's description, whether it is required or a secret, a value
field for one that is no secret, a secret chooser for a secret; the MCP part's two buttons, of their own size, saying
what each does; the launcher's MCP problems on the board, each on its own, before Start; the image of a service chosen
from your images (the inspector and the guide's question); the guide's introduction of a kind of part once."""
import json
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6 import QtCore  # noqa: E402

from sandbox_ai_app import backend, board as B, mcp_fields as MF, widgets  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")
DIGEST = "sha256:" + "a" * 64
IMAGE = f"ghcr.io/c0decave/sandbox-ai-mcp-gowitness@{DIGEST}"
GOWITNESS = {"id": "mcp:gowitness", "name": "gowitness", "kind": "oci", "env": [
    {"name": "ALLOW_PRIVATE_TARGETS", "description": "1: also private addresses", "secret": False,
     "required": False}]}
TELEGRAM = {"id": "mcp:telegram", "name": "telegram", "kind": "oci", "env": [
    {"name": "TELEGRAM_BOT_TOKEN", "description": "bot token", "secret": True, "required": True},
    {"name": "TELEGRAM_ALLOWED_CHATS", "description": "chat ids", "secret": False, "required": False}]}


@pytest.fixture(params=["happy", "sad", "edge"])
def scenario(request):
    return request.param


@pytest.fixture
def homes(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
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


@pytest.fixture
def board_page(qapp, homes, monkeypatch):
    (homes / "cwd").mkdir()
    monkeypatch.chdir(homes / "cwd")
    from sandbox_ai_app.board_view import BoardPage
    view = BoardPage(backend.SandboxAI(), lambda *a, **kw: None, lambda: {"board_grid": 20})
    yield view
    view.deleteLater()


# -- the rows of the table (no Qt) --

def test_variable_rows(scenario):
    if scenario == "happy":
        rows = MF.variable_rows(TELEGRAM, {"env_secrets": {"TELEGRAM_BOT_TOKEN": "BOT"},
                                           "env_values": {"TELEGRAM_ALLOWED_CHATS": "1,2"}})
        assert [(r["name"], r["secret"], r["required"], r["source"], r["detail"]) for r in rows] == [
            ("TELEGRAM_BOT_TOKEN", True, True, "secret", "BOT"), ("TELEGRAM_ALLOWED_CHATS", False, False, "value", "1,2")]
        assert rows[0]["description"] == "bot token"
        assert MF.definition_specs({"env": ["A"], "env_values": {"B": "1"}, "env_secrets": {"C": "S"}}) == [
            "A", "B=1", "C@S"]
    elif scenario == "sad":
        # No catalog entry read: what the definition names, no description; an invalid name left out.
        rows = MF.variable_rows(None, {"env": ["KEY", "1bad"], "env_optional": ["OPT"]})
        assert [(r["name"], r["required"], r["source"]) for r in rows] == [("KEY", True, "pass"), ("OPT", False, "pass")]
        assert MF.variable_rows({"env": "junk"}, None) == []
    else:
        own = MF.variable_rows(GOWITNESS, {"env_values": {"ALLOW_PRIVATE_TARGETS": "1"}})
        rows = MF.board_rows(own, ["ALLOW_PRIVATE_TARGETS=0", "EXTRA@SEC"])
        assert [(r["name"], r["source"], r["detail"], r["own"]) for r in rows] == [
            ("ALLOW_PRIVATE_TARGETS", "value", "0", ("value", "1")), ("EXTRA", "secret", "SEC", ("", ""))]
        assert [r["source"] for r in MF.board_rows(own, [])] == ["own"]


# -- the table --

def test_the_variables_table(qapp, sai, scenario):
    mcp = sai.module("mcp")
    if scenario == "happy":
        table = MF.VariablesFields(MF.variable_rows(TELEGRAM, {}), mcp, ["BOT", "OTHER"])
        token, chats = table.widgets["TELEGRAM_BOT_TOKEN"], table.widgets["TELEGRAM_ALLOWED_CHATS"]
        # A secret one: never "this value"; a required one: never "not passed".
        assert [token["source"].itemData(i) for i in range(token["source"].count())] == ["pass", "secret"]
        assert [chats["source"].itemData(i) for i in range(chats["source"].count())] == ["", "pass", "value", "secret"]
        token["source"].setCurrentIndex(token["source"].findData("secret"))
        token["secret"].setEditText("BOT")
        chats["source"].setCurrentIndex(chats["source"].findData("value"))
        chats["value"].setText("123,-100")
        assert table.valid() and table.specs() == ["TELEGRAM_BOT_TOKEN@BOT", "TELEGRAM_ALLOWED_CHATS=123,-100"]
        assert not chats["value"].isHidden() and chats["secret"].isHidden()
        assert "bot token" in token["note"].text() and "required" in token["note"].text()
    elif scenario == "sad":
        table = MF.VariablesFields(MF.variable_rows(GOWITNESS, {}), mcp)
        row = table.widgets["ALLOW_PRIVATE_TARGETS"]
        row["source"].setCurrentIndex(row["source"].findData("value"))
        row["value"].setText("gh" + "p_" + "A1b2C3d4" * 5)  # the shape of a token: refused by sandbox-ai's own rule
        assert not table.valid() and "credential" in row["reason"].text() and not row["reason"].isHidden()
        assert row["value"].property("invalid") is True and table.specs() == []
        row["value"].setText("a\nb")
        assert "one line" in table.problems()["ALLOW_PRIVATE_TARGETS"]
        row["source"].setCurrentIndex(row["source"].findData("secret"))
        row["secret"].setEditText("not a name")
        assert "Choose a secret" in table.problems()["ALLOW_PRIVATE_TARGETS"]
    else:
        table = MF.VariablesFields([], mcp)
        assert table.specs() == [] and table.valid()
        board = MF.VariablesFields(MF.board_rows(MF.variable_rows(GOWITNESS, {}), []), mcp, board=True)
        row = board.widgets["ALLOW_PRIVATE_TARGETS"]
        assert row["source"].currentData() == "own" and board.specs() == []
        row["source"].setCurrentIndex(row["source"].findData("value"))
        row["value"].setText("1")
        assert board.specs() == ["ALLOW_PRIVATE_TARGETS=1"]
        board.set_secrets(["A", "B"])
        assert [row["secret"].itemText(i) for i in range(row["secret"].count())] == ["A", "B"]


def test_the_properties_dialog_sets_the_variables(qapp, sai, homes, scenario):
    define(homes, {"gowitness": {"image": IMAGE, "network": "full"}})
    info = {"name": "gowitness", "kind": "image", "own": True, "definition": {"image": IMAGE, "network": "full",
                                                                              "env": []}}
    if scenario == "happy":
        dialog = MF.ServerPropertiesDialog(None, sai, info, GOWITNESS)
        row = dialog.env.widgets["ALLOW_PRIVATE_TARGETS"]
        row["source"].setCurrentIndex(row["source"].findData("value"))
        row["value"].setText("1")
        assert dialog.arguments() == ["mcp", "set", "gowitness", "--env=ALLOW_PRIVATE_TARGETS=1"]
        dialog.deleteLater()
    elif scenario == "sad":
        dialog = MF.ServerPropertiesDialog(None, sai, info, TELEGRAM)
        row = dialog.env.widgets["TELEGRAM_ALLOWED_CHATS"]
        row["source"].setCurrentIndex(row["source"].findData("value"))
        row["value"].setText("gh" + "p_" + "A1b2C3d4" * 5)
        ok = dialog.buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok)
        assert not ok.isEnabled(), "nothing that is not valid is taken"
        dialog.deleteLater()
    else:
        # A board's values filled in ("Save as the server's defaults…"): OK makes them the server's own.
        dialog = MF.ServerPropertiesDialog(None, sai, info, GOWITNESS, values={
            "limits": {"memory": "1g"}, "variables": ["ALLOW_PRIVATE_TARGETS=1"]})
        assert dialog.limits.fields["memory"].text() == "1g"
        assert dialog.arguments() == ["mcp", "set", "gowitness", "--memory=1g", "--env=ALLOW_PRIVATE_TARGETS=1"]
        dialog.deleteLater()


def test_the_add_dialog_asks_each_variable(qapp, sai, scenario):
    from sandbox_ai_app import ui

    class Parent(QtWidgets.QWidget):
        pass
    parent = Parent()
    parent.sai = sai
    entry = {**(GOWITNESS if scenario != "sad" else TELEGRAM), "title": "x", "network": "full"}
    dialog = ui.AddServerDialog(parent, entry, "local")
    if scenario == "happy":
        # Not required, no secret: left out unless given (it was passed - and required - with every add).
        assert dialog.values("gowitness")["env"] == []
        row = dialog.env.widgets["ALLOW_PRIVATE_TARGETS"]
        row["source"].setCurrentIndex(row["source"].findData("value"))
        row["value"].setText("1")
        values = dialog.values("gowitness")
        assert values["env"] == ["ALLOW_PRIVATE_TARGETS=1"]
        assert "--env=ALLOW_PRIVATE_TARGETS=1" in backend.mcp_add_args(**values)[0]
    elif scenario == "sad":
        # A required secret: from your environment or its secret by default, never a value.
        assert dialog.values("telegram")["env"] == ["TELEGRAM_BOT_TOKEN"]
        row = dialog.env.widgets["TELEGRAM_BOT_TOKEN"]
        assert row["source"].findData("value") < 0
        row["source"].setCurrentIndex(row["source"].findData("secret"))
        row["secret"].setEditText("bad name")
        assert any("Choose a secret" in problem for problem in dialog.problems())
    else:
        listed = ui.AddServerDialog(parent, {"name": "x", "_listed": True}, "local")
        assert listed.env is None and listed.values("x")["env"] == []
        listed.deleteLater()
    dialog.deleteLater()
    parent.deleteLater()


def test_mcp_set_args_take_values_and_secrets(scenario):
    if scenario == "happy":
        assert backend.mcp_set_args("g", env=["A", "B=1", "C@S"]) == ["mcp", "set", "g", "--env=A", "--env=B=1",
                                                                     "--env=C@S"]
    elif scenario == "sad":
        for bad in ("1A", "A@1x", "A=a\nb", "A=" + "x" * 1025, "A B"):
            with pytest.raises(ValueError):
                backend.mcp_set_args("g", env=[bad])
    else:
        assert backend.mcp_set_args("g", env=[]) == ["mcp", "set", "g", "--no-env"]
        assert backend.env_spec("URL=https://x.example/?a=b") == "URL=https://x.example/?a=b"


# -- the board: its own variables (--mcp-env) --

def test_a_boards_mcp_variables_round_trip(sai, scenario, homes):
    if scenario == "happy":
        board = B.from_argv(sai, "claude", ["--mcp", "gowitness", "--mcp-env", "gowitness:ALLOW_PRIVATE_TARGETS=1",
                                            "--mcp-env", "gowitness:BOT@MY_BOT", "--mcp-env", "other:X=1"])
        node = board.of_kind("mcp")[0]
        assert node.props["variables"] == ["ALLOW_PRIVATE_TARGETS=1", "BOT@MY_BOT"]
        assert board.extra == ["--mcp-env", "other:X=1"], "a server the board does not use: kept as it was"
        argv = B.to_argv(sai, board)
        assert [argv[i + 1] for i, a in enumerate(argv) if a == "--mcp-env"][:2] == [
            "gowitness:ALLOW_PRIVATE_TARGETS=1", "gowitness:BOT@MY_BOT"]
    elif scenario == "sad":
        board = B.from_argv(sai, "claude", ["--mcp", "gowitness"])
        node = board.of_kind("mcp")[0]
        node.props["variables"] = ["GITHUB_TOKEN=abc"]
        problems = [p for p in B.problems(sai, board) if p.node == node.id and p.level == "error"]
        assert problems and "name of a secret" in problems[0].text
    else:
        board = B.from_argv(sai, "claude", ["--mcp", "gowitness", "--mcp-env", "gowitness:X=1", "--mcp-env",
                                            "gowitness:X=2"])
        assert board.of_kind("mcp")[0].props["variables"] == ["X=2"], "the later one"
        node = board.of_kind("mcp")[0]
        node.props.update(project=True, definition={"command": ["x"]})
        assert "--mcp-env" not in B.check_argv(sai, board), "the project's own server: checked by the board"


def test_the_mcp_part_shows_variables_and_two_buttons_of_their_own_size(board_page, homes):
    define(homes, {"gowitness": {"image": IMAGE, "network": "full"}})
    board_page.mcp_catalog_entries = {"mcp:gowitness": GOWITNESS, "gowitness": GOWITNESS}
    node = board_page.add_node("mcp", name="gowitness") or board_page.board.of_kind("mcp")[0]
    board_page.inspect(node)
    table = board_page.inspector_holder.findChildren(MF.VariablesFields)[0]
    row = table.widgets["ALLOW_PRIVATE_TARGETS"]
    row["source"].setCurrentIndex(row["source"].findData("value"))
    row["value"].setText("1")
    table.commit()
    assert board_page.board.node(node.id).props["variables"] == ["ALLOW_PRIVATE_TARGETS=1"]
    assert "--mcp-env gowitness:ALLOW_PRIVATE_TARGETS=1" in board_page.preview.toPlainText()
    qapp = QtWidgets.QApplication.instance()
    qapp.processEvents()  # (a rebuild of the inspector that was asked for: done before its buttons are looked at)
    board_page.inspect(board_page.board.node(node.id))
    board_page.inspect_timer.stop()
    buttons = {b.text(): b for b in board_page.inspector_holder.findChildren(QtWidgets.QPushButton)}
    reset, save = buttons["Use the server's limits"], buttons["Save as the server's defaults…"]
    assert "Discards this board's limits" in reset.toolTip() and "OK stores them" in save.toolTip()
    board_page.resize(1400, 900)
    board_page.show()
    board_page.inspector_holder.show()
    qapp.processEvents()
    # (an answer that came in meanwhile - the host's secrets, the catalog: slow on a loaded machine - may have
    # rebuilt the inspector, its buttons with it: the ones shown now are measured)
    buttons = {b.text(): b for b in board_page.inspector_holder.findChildren(QtWidgets.QPushButton)
               if b.isVisible()}
    reset, save = buttons["Use the server's limits"], buttons["Save as the server's defaults…"]
    for button in (reset, save):  # their own size: not as wide as the fields (they looked like the main action)
        assert button.width() <= button.sizeHint().width() + 4, (button.text(), button.width())
    assert any("These values hold for this board's starts only" in label.text()
               for label in board_page.inspector_holder.findChildren(QtWidgets.QLabel))
    asked = []
    board_page.server_defaults.connect(lambda name, host, values: asked.append((name, host, values)))
    board_page.inspect(board_page.board.node(node.id))  # (the events above rebuilt the inspector)
    board_page.inspector_holder.findChildren(MF.LimitsFields)[0].fields["memory"].setText("1g")
    next(b for b in board_page.inspector_holder.findChildren(QtWidgets.QPushButton)
         if b.text() == "Save as the server's defaults…").click()
    assert asked == [("gowitness", "local", {"limits": {"memory": "1g"}, "variables": ["ALLOW_PRIVATE_TARGETS=1"]})]
    board_page.hide()


# -- the launcher's problems of the MCP servers, each on its own, on the board before Start --

def test_launcher_problems(sai, scenario):
    board = B.from_argv(sai, "claude", ["--mcp", "gowitness", "--mcp", "files"])
    nodes = {n.props["name"]: n.id for n in board.of_kind("mcp")}
    if scenario == "happy":
        message = ("agent-sandbox: 3 problems with the MCP servers of this start (nothing was started or made; fix "
                   "them, then start again):\n  - MCP server gowitness needs the variable ALLOW_PRIVATE_TARGETS: set "
                   "it\n  - MCP server files: --mount /data=/gone:ro: the folder /gone is not there\n"
                   "  - Unknown MCP server: other; defined: demo")
        found = B.launcher_problems(board, message)
        assert [(p.level, p.node) for p in found] == [("error", nodes["gowitness"]), ("error", nodes["files"]),
                                                      ("error", None)]
        assert found[0].text.startswith("MCP server gowitness needs the variable")
    elif scenario == "sad":
        assert B.launcher_problems(board, "agent-sandbox: --memory must be like 512m") == []
        assert B.launcher_problems(board, None) == []
    else:
        found = B.launcher_problems(board, "agent-sandbox: MCP server files takes BOT from the secret BOT, which is "
                                           "not there: sandbox-ai secrets set BOT")
        assert len(found) == 1 and found[0].node == nodes["files"]


def test_the_board_lists_the_launchers_mcp_problems_before_start(board_page, homes):
    node = board_page.add_node("mcp", name="gowitness") or board_page.board.of_kind("mcp")[0]
    board_page.refresh()
    state = board_page.board.state()
    board_page.checking_state = state
    board_page.check_token += 1
    message = ("agent-sandbox: 2 problems with the MCP servers of this start (nothing was started or made; fix them, "
               "then start again):\n  - MCP server gowitness needs the variable A: set it\n"
               "  - MCP server gowitness needs the variable B: set it")
    board_page.checked(board_page.check_token, (None, message), None, "local")
    texts = [board_page.findings.item(i).text() for i in range(board_page.findings.count())]
    assert "✘ MCP server gowitness needs the variable A: set it" in texts
    assert "✘ MCP server gowitness needs the variable B: set it" in texts
    assert [p.text for p in board_page.errors() if "needs the variable" in p.text] == [
        "MCP server gowitness needs the variable A: set it", "MCP server gowitness needs the variable B: set it"]
    marked = board_page.findings.item(next(i for i in range(board_page.findings.count())
                                           if "variable A" in board_page.findings.item(i).text()))
    assert marked.data(QtCore.Qt.ItemDataRole.UserRole) == node.id, "on its part"
    board_page.set_prop(node.id, "name", "other")  # another board: those problems are not its own
    assert not [p for p in board_page.errors() if "needs the variable" in p.text]


# -- a service's image: your images in a drop-down, Other… for a name typed in --

def test_the_image_of_a_service_is_chosen_from_your_images(board_page, scenario):
    images = [{"name": "redis", "kind": "pulled", "ref": "docker.io/library/redis:7", "image": "x@sha256:1"},
              {"name": "mytool", "kind": "built", "image": "localhost/mytool:latest"}]
    board_page.set_user_images(images if scenario != "edge" else [], {}, board_page.host())
    node = board_page.add_node("service", name="db") or board_page.board.of_kind("service")[0]
    board_page.inspect(board_page.board.node(node.id))
    combo = next(c for c in board_page.inspector_holder.findChildren(QtWidgets.QComboBox)
                 if c.accessibleName() == "Image")
    data = [combo.itemData(i) for i in range(combo.count())]
    if scenario == "happy":
        assert data[:2] == ["redis", "mytool"] and combo.itemText(0).startswith("redis · pulled · docker.io")
        combo.setCurrentIndex(0)
        combo.activated.emit(0)
        assert board_page.board.node(node.id).props["image"] == "redis"
    elif scenario == "sad":
        # Other…: a name typed in.
        other = combo.count() - 1
        assert combo.itemText(other) == "Other…"
        combo.setCurrentIndex(other)
        combo.activated.emit(other)
        assert combo.currentText() == ""
        combo.setEditText("ghcr.io/me/thing")
        combo.lineEdit().editingFinished.emit()
        assert board_page.board.node(node.id).props["image"] == "ghcr.io/me/thing"
    else:
        assert combo.itemText(0).startswith("No images of yours yet") and not combo.model().item(0).isEnabled()
        # (2026-11-19: why agent images are none of them, and the Images page, before Other…)
        assert combo.itemText(1).startswith("Agent images and their variants are not service images")
        assert combo.itemText(2) == "Add a service image (Images page)…" and combo.itemText(3) == "Other…"


def test_the_guides_image_question_offers_your_images(board_page, qapp):
    from sandbox_ai_app import guide_view
    board_page.set_user_images([{"name": "redis", "kind": "pulled", "ref": "redis:7"}], {}, board_page.host())
    node = B.Node(99, "service", {"name": "db"})
    dialog = guide_view.NodeGuide(node, [], board_page)
    question, combo = dialog.inputs["image"]
    assert isinstance(combo, QtWidgets.QComboBox) and combo.itemData(0) == "redis"
    combo.setCurrentIndex(0)
    assert dialog.answers()["image"] == "redis"
    combo.setCurrentIndex(combo.count() - 1)  # Other…: never taken as a name
    combo.setEditText("own/image")
    assert dialog.answers()["image"] == "own/image"
    dialog.deleteLater()


# -- the guide: its introduction of a kind of part once --

class FakeState:
    def __init__(self):
        self.data = {}

    def get(self, key, default=None, kind=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value


def test_the_guides_introduction_is_shown_once_per_kind(board_page, monkeypatch, scenario):
    from sandbox_ai_app import guide_view
    board_page.state = FakeState()
    opened = []

    def exec_dropped(dialog):
        opened.append((dialog.node.kind, dialog.pages.currentIndex(), dialog.intro.isVisible() or
                       not dialog.intro.isHidden()))
        if scenario == "edge":
            dialog.intro.click()
            opened.append(("intro", dialog.pages.currentIndex(), not dialog.intro.isHidden()))
        return QtWidgets.QDialog.DialogCode.Rejected
    monkeypatch.setattr(widgets, "exec_dropped", exec_dropped)
    first = board_page.board.add("mount", source="/tmp", dest="/mnt/a")
    board_page.guide_node(first.id)
    if scenario == "sad":
        board_page.state = None  # no state to keep it in: shown every time, as before
        board_page.guide_node(first.id)
        assert [o[1] for o in opened] == [0, 0]
        return
    second = board_page.board.add("mount", source="/tmp", dest="/mnt/b")
    board_page.guide_node(second.id)
    assert opened[0][:2] == ("mount", 0), "the first time: its introduction"
    assert board_page.state.data["guide_intro_seen"] == ["mount"]
    if scenario == "happy":
        assert opened[1][:3] == ("mount", 1, True), "then its first question, with Show the introduction"
        other = board_page.board.add("devices")
        board_page.guide_node(other.id)
        assert opened[2][:2] == ("devices", 0), "another kind: its own introduction once"
    else:
        assert opened[1] == ("intro", 0, False) and opened[3] == ("intro", 0, False)
        assert guide_view.NodeGuide.__init__.__code__.co_varnames.count("skip_intro") == 1
