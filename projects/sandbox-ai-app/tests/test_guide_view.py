"""The guide on screen: the tour panel walks the window's pages, and a part added from the board's list is set up
question by question (guide_view.py, with guide.py behind it)."""
import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import QEvent, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402

from sandbox_ai_app import guide, guide_view  # noqa: E402

from test_projects import env, project, window  # noqa: E402,F401 - shared fixtures


def test_the_tour_walks_the_pages_and_remembers_where_it_was(window):  # noqa: F811
    window.start_tour(0)
    assert window.tour_dock.isVisible() and window.current_page() == "launch"
    assert "1 of" in window.tour.where.text() and window.tour.title.text() == "Start"
    window.tour.next.click()
    assert window.current_page() == "board" and window.state.get("tour_step") == 1
    window.tour.back.click()
    assert window.current_page() == "launch" and not window.tour.back.isEnabled()
    window.tour.go(len(guide.TOUR) - 1)
    assert window.current_page() == guide.TOUR[-1].page and window.tour.next.text() == "Finish"
    window.tour.next.click()
    assert not window.tour_dock.isVisible(), "Finish closes it"
    window.start_tour()  # Help > Guided tour: where it was left
    assert window.tour.index == len(guide.TOUR) - 1 and window.tour_dock.isVisible()
    QtWidgets.QApplication.sendEvent(window.tour, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape,
                                                            Qt.KeyboardModifier.NoModifier))
    assert not window.tour_dock.isVisible() and window.state.get("tour_offered") is True


def test_a_new_part_is_set_up_question_by_question(window, monkeypatch, tmp_path):  # noqa: F811
    board = window.board
    folder = tmp_path / "mount-source"  # not "data": XDG_DATA_HOME of the window lives there
    folder.mkdir()

    def answer(dialog):
        assert dialog.pages.count() == 1 + len(guide.questions("mount")) + 1, "intro, questions, what is left"
        question, widget = dialog.inputs["source"]
        widget.edit.setText(str(folder))
        dialog.inputs["mode"][1].setCurrentIndex(dialog.inputs["mode"][1].findData("rw"))
        for _ in range(dialog.pages.count() - 1):
            dialog.next.click()
        assert dialog.next.text() == "Done"
        texts = [dialog.summary.item(i).text() for i in range(dialog.summary.count())]
        assert not any("not set yet" in text for text in texts), texts
        return QtWidgets.QDialog.DialogCode.Accepted
    monkeypatch.setattr(guide_view.NodeGuide, "exec", lambda self: answer(self))
    item = next(board.palette.item(i) for i in range(board.palette.count())
                if board.palette.item(i).data(Qt.ItemDataRole.UserRole) == "mount")
    board.palette.itemDoubleClicked.emit(item)
    mount = board.board.of_kind("mount")[-1]
    assert mount.props["source"] == str(folder) and mount.props["mode"] == "rw"
    board.undo_group.undo()
    assert board.board.of_kind("mount")[-1].props["source"] == "", "the answers are one undo step"


def test_cancel_changes_nothing_and_the_setting_turns_it_off(window, monkeypatch):  # noqa: F811
    board = window.board
    opened = []
    monkeypatch.setattr(guide_view.NodeGuide, "exec",
                        lambda self: opened.append(self.node.kind) or QtWidgets.QDialog.DialogCode.Rejected)
    board.add_node("limits", guided=True)
    assert opened == ["limits"] and "--memory" not in board.board.first("limits").props
    board.settings = lambda: {"guide_nodes": False}
    board.add_node("display", guided=True)
    assert opened == ["limits"], "no guide when the setting says no"
    board.add_node("logs")  # added by code (presets, the wizard): never guided
    assert opened == ["limits"]


def test_the_inspector_offers_the_guide_for_a_part(window, monkeypatch):  # noqa: F811
    board = window.board
    opened = []
    monkeypatch.setattr(guide_view.NodeGuide, "exec",
                        lambda self: opened.append(self.node.kind) or QtWidgets.QDialog.DialogCode.Rejected)
    node = board.board.first("network")
    board.inspect(node)
    button = next(b for b in board.inspector_holder.findChildren(QtWidgets.QPushButton)
                  if b.text() == "Wizard")
    button.click()
    assert opened == ["network"]


def test_a_part_that_is_not_ready_says_what_is_missing(window):  # noqa: F811
    node = window.board.board.add("mcp", name="")
    dialog = guide_view.NodeGuide(node, [], window)
    dialog.show_page(dialog.pages.count() - 1)
    texts = [dialog.summary.item(i).text() for i in range(dialog.summary.count())]
    assert texts == ["✘ Server is not set yet"]
    dialog.inputs["name"][1].setText("demo")
    dialog.show_page(dialog.pages.count() - 1)
    assert dialog.summary.item(0).text() == "✔ Nothing: the node is ready."


def test_with_a_model_server_the_guide_offers_the_servers_models(window, monkeypatch):  # noqa: F811
    """A board with a model server (--llm): the agent's Model question lists that server's models (NAME:MODEL), and
    the answer goes to --llm, never --model (the launcher refuses --model with --llm)."""
    from sandbox_ai_app import wizard_llm as WL
    board = window.board
    agent = board.board.first("agent")
    agent.props["agent"] = "opencode"
    board.change("server", lambda: WL.apply(board.board, {"name": "macbook", "model": "", "only": False}))
    board.llm_servers = [{"name": "macbook", "kind": "openai-compatible", "url": "http://192.168.1.20:1234",
                          "models": ["qwen3-coder-30b", "gpt-oss-20b"], "agents": ["opencode"]}]
    assert WL.uses_server(board.board)

    def answer(dialog):
        _, widget = dialog.inputs["model"]
        items = [widget.itemData(i) for i in range(widget.count())]
        assert "macbook:qwen3-coder-30b" in items and "macbook:gpt-oss-20b" in items
        assert not any(str(i).startswith(("anthropic/", "openai/")) for i in items), items
        widget.setCurrentIndex(widget.findData("macbook:gpt-oss-20b"))
        return QtWidgets.QDialog.DialogCode.Accepted
    monkeypatch.setattr(guide_view.NodeGuide, "exec", lambda self: answer(self))
    board.guide_node(agent.id)
    assert board.board.first("llm").props["--llm"] == "macbook:gpt-oss-20b"
    assert not agent.props.get("model")
    # A model typed bare is the server's (NAME:MODEL), never read as a server of that name; another server's name
    # typed chooses that server.
    typed = []

    def type_in(dialog):
        _, widget = dialog.inputs["model"]
        widget.setEditText(typed[-1])
        return QtWidgets.QDialog.DialogCode.Accepted
    monkeypatch.setattr(guide_view.NodeGuide, "exec", lambda self: type_in(self))
    typed.append("llama3")
    board.guide_node(agent.id)
    assert board.board.first("llm").props["--llm"] == "macbook:llama3"
    board.llm_servers.append({"name": "studio", "kind": "openai-compatible", "url": "http://192.168.1.21:1234",
                              "models": ["qwen3"], "agents": ["opencode"]})
    typed.append("studio")
    board.guide_node(agent.id)
    assert board.board.first("llm").props["--llm"] == "studio"


def test_the_inspector_says_which_model_the_server_gives(window):  # noqa: F811
    from sandbox_ai_app import wizard_llm as WL
    from sandbox_ai_app.board_view import summary
    board = window.board
    agent = board.board.first("agent")
    agent.props["agent"] = "opencode"
    board.change("server", lambda: WL.apply(board.board, {"name": "macbook", "model": "", "only": False}))
    servers = [{"name": "macbook", "kind": "openai-compatible", "model": "qwen3-coder-30b",
                "models": ["qwen3-coder-30b", "gpt-oss-20b"]}]
    assert WL.used_model(board.board, servers) == ("qwen3-coder-30b", "default")
    assert WL.used_model(board.board, [{"name": "macbook", "models": ["gpt-oss-20b"]}]) == ("gpt-oss-20b", "only")
    assert WL.used_model(board.board, [{"name": "macbook", "models": ["a", "b"]}]) == ("", "unknown")
    board.llm_servers = servers
    board.inspect(agent)
    assert "qwen3-coder-30b (the default model of macbook)" in board.model_combo.currentText()
    assert not board.model_combo.isEnabled()
    assert "macbook" in summary(board.board.first("llm"))
    board.change("model", lambda: WL.apply(board.board, {"name": "macbook", "model": "gpt-oss-20b", "only": False}))
    assert WL.used_model(board.board, servers) == ("gpt-oss-20b", "named")


def test_ok_and_cancel_close_the_properties_docked_or_floating(window, qapp):  # noqa: F811
    board = window.board
    window.show()
    window.show_page("board")
    agent = board.board.first("agent")
    dock = window.docks["inspector"]
    window.reopen_panel("inspector")
    board.inspect(agent)
    qapp.processEvents()
    assert dock.isVisible() and not window.inspector_fold.is_folded()
    board.edit_done(True)  # OK, docked: folded to its strip
    qapp.processEvents()
    assert window.inspector_fold.is_folded()
    dock.setFloating(True)
    window.reopen_panel("inspector")
    qapp.processEvents()
    assert dock.isVisible()
    board.edit_done(False)  # Cancel, floating: closed
    qapp.processEvents()
    assert not dock.isVisible() and "inspector" in window.closed_docks
    window.reopen_panel("inspector")  # the next part selected: where it was
    qapp.processEvents()
    assert dock.isVisible() and dock.isFloating()
    dock.setFloating(False)


def test_what_the_board_needs_is_made_before_the_start(window, monkeypatch):  # noqa: F811
    """Whatever a board or preset needs and the host lacks is made before the start, which then goes on: an identity
    (no logins), an image variant from its recipe and built (in the background), a Tor profile, MCP servers from
    their catalogs. Nothing is left to the launcher's refusal - for every preset, not only Tor."""
    import time
    board = window.board
    monkeypatch.setattr(board, "board_needs", lambda: {"--identity": ["investigation"], "--image-variant": ["tor"],
                                                         "--vpn": ["anon"], "--mcp": ["yara"]})
    state = {"made": False}
    monkeypatch.setattr(board.sai, "identities", lambda: {"investigation": {}} if state["made"] else {})
    monkeypatch.setattr(board.sai, "host_json", lambda host, *a, **k: [{"target": "tor", "variant": True,
                                                                       "present": True}] if state["made"] else [])
    monkeypatch.setattr(board.sai, "host_vpn_profiles", lambda host: [{"profile": "anon"}] if state["made"] else [])
    monkeypatch.setattr(board.sai, "host_mcp_servers", lambda host: [["yara"]] if state["made"] else [])
    ran, started, warned = [], [], []

    def host_run(host, *args, **kw):
        ran.append(args)
        if args[:1] == ("mcp",):
            state["made"] = True  # (the last step)
        return 0, "", ""
    monkeypatch.setattr(board.sai, "host_run", host_run)
    monkeypatch.setattr("sandbox_ai_app.board_view.warn", lambda parent, title, text: warned.append(text))
    assert board.check_prerequisites(then=lambda: started.append(True)) is True
    deadline = time.monotonic() + 15
    while not started and not warned and time.monotonic() < deadline:
        QtWidgets.QApplication.processEvents()
        time.sleep(0.01)
    assert not warned, warned
    assert ("identities", "add", "--description=made for a start (no logins)", "--", "investigation") in ran
    assert ("images", "recipe", "tor", "--from", "claude") in ran and ("build", "--variant", "tor") in ran
    assert ("vpn", "add", "anon", "--tor", "--socks") in ran and ("mcp", "add", "yara") in ran
    assert started == [True], "the start went on once everything was there"


def test_preparing_ignores_the_projects_own_servers_and_starts_once(window, monkeypatch):  # noqa: F811
    """Review: the research preset's own servers (search, fetch: its project file) counted as missing, so it never
    started; a second Start while the needs were read started twice; a continuation ran on another board."""
    import time
    board = window.board
    monkeypatch.setattr(board, "prerequisite_argv", lambda: ["--mcp", "search", "--identity", "work"])
    monkeypatch.setattr(board, "prerequisite_own_servers", lambda: {"search"})
    assert board.board_needs() == {"--identity": ["work"]}
    monkeypatch.setattr(board.sai, "identities", lambda: {"work": {}})
    started = []
    assert board.check_prerequisites(then=lambda: started.append(1)) is True
    assert board.check_prerequisites(then=lambda: started.append(2)) is True  # (still reading: no second start)
    deadline = time.monotonic() + 10
    while not started and time.monotonic() < deadline:
        QtWidgets.QApplication.processEvents()
        time.sleep(0.01)
    for _ in range(20):
        QtWidgets.QApplication.processEvents()
    assert started == [1]
    # Another board meanwhile: the continuation is dropped.
    board.prerequisites_done = None
    monkeypatch.setattr(board, "prerequisite_argv", lambda: ["--identity", "work2"])
    monkeypatch.setattr(board.sai, "identities", lambda: {"work2": {}})
    contexts = iter([("local", 1, ""), ("local", 2, "")])
    monkeypatch.setattr(board, "prerequisite_context", lambda: next(contexts, ("local", 2, "")))
    later = []
    assert board.check_prerequisites(then=lambda: later.append(1)) is True
    deadline = time.monotonic() + 5
    while board.__dict__.get("preparing_contexts") and time.monotonic() < deadline:
        QtWidgets.QApplication.processEvents()
        time.sleep(0.01)
    assert later == []


def test_a_read_that_failed_is_read_again_and_a_build_for_another_board_starts_nothing(window, monkeypatch):  # noqa: F811
    """Review 2: an unanswered read counted as "everything there" for the rest of the session; questions and
    makings went on after the page showed another board; another board was held up while one was prepared."""
    import time
    board = window.board
    monkeypatch.setattr(board, "prerequisite_argv", lambda: ["--identity", "work"])
    calls = []

    def identities():
        calls.append(1)
        raise OSError("ssh: connection refused")
    monkeypatch.setattr(board.sai, "identities", identities)
    started = []
    for _ in range(2):
        assert board.check_prerequisites(then=lambda: started.append(1)) is True
        deadline = time.monotonic() + 10
        while board.__dict__.get("preparing_contexts") and time.monotonic() < deadline:
            QtWidgets.QApplication.processEvents()
            time.sleep(0.01)
    assert len(calls) == 2, "read again: one that failed is not remembered as complete"
    # The page shows another board by the time the read answers: nothing is asked, made or started.
    monkeypatch.setattr(board.sai, "identities", lambda: {})
    ran = []
    monkeypatch.setattr(board.sai, "host_run", lambda host, *a, **k: ran.append(a) or (0, "", ""))
    contexts = iter([("local", 1, ""), ("local", 2, "")])
    monkeypatch.setattr(board, "prerequisite_context", lambda: next(contexts, ("local", 2, "")))
    started.clear()
    board.prerequisites_done = None
    assert board.check_prerequisites(then=lambda: started.append(1)) is True
    deadline = time.monotonic() + 5
    while board.__dict__.get("preparing_contexts") and time.monotonic() < deadline:
        QtWidgets.QApplication.processEvents()
        time.sleep(0.01)
    assert ran == [] and started == []


def test_a_preset_that_needs_only_what_is_made_elsewhere_prepares_nothing(window, monkeypatch):  # noqa: F811
    """Review: a preset needing only what the app cannot make here (an image variant without a recipe) said
    "Preparing what the start needs: ." and promised a notification that never came."""
    board = window.board
    notices, warned, ran = [], [], []
    monkeypatch.setattr(board, "prerequisite_notice", lambda text, role="", toast=False: notices.append(text))
    monkeypatch.setattr("sandbox_ai_app.prerequisites.warn", lambda parent, title, text: warned.append(text))
    monkeypatch.setattr(board.sai, "host_run", lambda host, *a, **k: ran.append(a) or (0, "", ""))
    board.provide({"--image-variant": ["no-such-recipe"]}, [], "local", None, None, "preset",
                  context=("local", 1, ""), agent="claude")
    assert warned and "no-such-recipe" in warned[-1]
    assert not any("Preparing what the start needs" in text for text in notices), notices
    assert not board.__dict__.get("preparing_contexts") and ran == []


def test_the_guide_offers_the_mcp_servers_to_choose_from(window, monkeypatch):  # noqa: F811
    """Adding an MCP server through the guide: a drop-down of the servers there are (defined here, the project's
    own), the one chosen taken by its name; a name typed that it does not list is taken as typed."""
    board = window.board
    monkeypatch.setattr(board, "mcp_names", lambda: ["demo", "playwright"])
    board.mcp_catalog_names = ["searxng"]
    node = board.board.add("mcp", name="")
    dialog = guide_view.NodeGuide(node, [], board)
    _question, widget = dialog.inputs["name"]
    assert isinstance(widget, QtWidgets.QComboBox) and widget.isEditable()
    names = [widget.itemData(i) for i in range(widget.count())]
    assert names[:2] == ["demo", "playwright"] and "searxng" in names
    widget.setCurrentIndex(1)
    assert dialog.answers()["name"] == "playwright"
    widget.setEditText("other-host-server")
    assert dialog.answers()["name"] == "other-host-server"
    dialog.reject()
