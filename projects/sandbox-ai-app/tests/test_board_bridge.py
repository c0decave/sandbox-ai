"""The Board's Bridge part: a session bridge to another running session of this machine, made once the board's start
runs (named for it), after the launcher's dry run and the human's confirmation; never written into the project file,
kept with the board's draft; its problems and its badge."""
import json
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, board as B, i18n, widgets  # noqa: E402
from test_board_view import page  # noqa: E402,F401 - the fixture

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


def bridged_board(tmp_path, **props):
    board = B.Board()
    for kind in ("agent", "proxy", "network"):
        board.add(kind)
    board.add("workspace", path=str(tmp_path))
    node = board.add("bridge", **{"peer": "docs", "message": True, "read": ["src"], **props})
    return board, node


def test_the_bridge_part_is_no_launcher_option_and_never_reaches_the_project_file(tmp_path):
    sai = backend.SandboxAI()
    board, node = bridged_board(tmp_path)
    assert (board.first("agent").id, node.id) in board.edges, "added beside the agent, it is connected"
    assert node.title() == "Bridge: docs" and node.key() == "bridge:docs"
    plain, _ = bridged_board(tmp_path)
    plain.remove(plain.of_kind("bridge")[0].id)
    assert B.to_argv(sai, board) == B.to_argv(sai, plain)
    document, _ = B.project_document(sai, board)
    assert "bridge" not in json.dumps(document) and "docs" not in json.dumps(document)
    # Kept with the board's draft (the app's own state), checked as any draft.
    again = B.Board.checked(board.state(), sai)
    assert again.of_kind("bridge")[0].props["peer"] == "docs"
    board.layout()
    assert node.x == board.first("proxy").x, "in the column of what the agent talks to"


def test_its_values_follow_the_direction(tmp_path):
    _, node = bridged_board(tmp_path, watch=True, actions=["alert", "hint"], until="2h", ask_mode="each", both=True,
                            limits={"rate": "30", "bogus": "1"})
    values = B.bridge_values(node.props, "claude-board-1")
    assert values == {"a": "claude-board-1", "b": "docs", "allow": ["message", "files:ro:src", "watch"], "both": True,
                      "until": "2h", "ask": "each", "observer_actions": ["alert", "hint"], "limits": [("rate", "30")]}
    node.props["direction"] = "in"
    assert (B.bridge_values(node.props, "me")["a"], B.bridge_values(node.props, "me")["b"]) == ("docs", "me")
    del node.props["actions"]
    assert B.bridge_values(node.props, "me")["observer_actions"] == ["alert"], "alert by default"


def test_its_problems_and_its_badge(tmp_path):
    sai = backend.SandboxAI()
    board, node = bridged_board(tmp_path)
    assert [p for p in B.problems(sai, board) if p.node == node.id] == []
    score, badges = B.badges(board)
    assert any(b.node == node.id and b.level == "warn" and "may reach the session docs (message,files:ro:src)" in b.text
               for b in badges)
    node.props["write"] = ["../etc"]
    found = [p for p in B.problems(sai, board) if p.node == node.id]
    assert [p.level for p in found] == ["error"] and "'..' is never followed" in found[0].text
    node.props.update(write=[], message=False, read=[])
    assert "at least one capability" in [p for p in B.problems(sai, board) if p.node == node.id][0].text
    node.props.update(peer="", message=True)
    found = [p for p in B.problems(sai, board) if p.node == node.id]
    assert [p.level for p in found] == ["warning"] and "no bridge is made" in found[0].text
    node.props["peer"] = "docs"
    board.first("agent").props["agent"] = "codex"
    found = [p for p in B.problems(sai, board) if p.node == node.id]
    assert [p.level for p in found] == ["warning"] and "reads its MCP servers only when it starts" in found[0].text
    board.disconnect(board.first("agent").id, node.id)
    assert not [p for p in B.problems(sai, board) if p.node == node.id and p.level == "error"]
    assert B.bridge_nodes(board) == [], "not connected: not made"


def test_the_inspector_edits_the_bridge_part(page, tmp_path, wait):
    from sandbox_ai_app.ui import backend as _  # noqa: F401 - (the window's modules loaded as the app loads them)
    instance = backend.Instance("session", "session:docs", "docs", session="docs", state="running", run="session-docs",
                                agent="claude", record={"kind": "agent", "workspace": str(tmp_path)})
    page.set_running({"session:docs": instance})
    assert [e["name"] for e in page.running_sessions] == ["docs"]
    page.add_node("bridge")
    node_id = page.inspected
    assert page.board.node(node_id).props == {"peer": "docs", "direction": "out", "message": True}, \
        "the first session that runs, messages"

    def field(cls, test):
        page.inspect(page.board.node(node_id))
        return [w for w in page.findChildren(cls) if test(w)][-1]
    combo = field(QtWidgets.QComboBox, lambda w: w.accessibleName() == "Other session")
    assert combo.findData("docs") >= 0 and combo.currentText() == "docs"
    combo.setEditText("web")
    combo.lineEdit().editingFinished.emit()
    assert page.board.node(node_id).props["peer"] == "web", "a name typed for a session to come"
    direction = field(QtWidgets.QComboBox, lambda w: w.accessibleName() == "Direction")
    direction.setCurrentIndex(direction.findData("in"))
    assert page.board.node(node_id).props["direction"] == "in"
    observer = field(QtWidgets.QCheckBox, lambda w: w.text() == i18n.tr("Pause B"))
    assert not observer.isEnabled(), "the observer's actions only with watch"
    field(QtWidgets.QCheckBox, lambda w: w.text() == i18n.tr("Observe B (watch)")).setChecked(True)
    assert page.board.node(node_id).props["watch"] is True
    field(QtWidgets.QCheckBox, lambda w: w.text() == i18n.tr("Pause B")).setChecked(True)
    assert page.board.node(node_id).props["actions"] == ["alert", "pause"]
    from sandbox_ai_app.list_fields import ListFields  # (read and write: one field of kinds since 2026-11-19)
    files = field(ListFields, lambda w: w.accessibleName() == i18n.tr("Files"))
    assert files.choose_kind("read") and files.add("src") and files.add("docs")
    assert page.board.node(node_id).props["read"] == ["src", "docs"]
    rate = field(QtWidgets.QLineEdit, lambda w: w.placeholderText() == i18n.tr("default: {value}", value="60"))
    rate.setText("20")
    rate.editingFinished.emit()
    assert page.board.node(node_id).props["limits"] == {"rate": "20"}
    page.undo.undo()
    assert "limits" not in page.board.node(node_id).props, "each change is a step of Undo"
    assert "--allow" not in page.preview.toPlainText() and "web" not in page.preview.toPlainText()


def test_the_start_is_named_and_the_bridge_made_once_both_sessions_run(page, tmp_path, monkeypatch, wait):
    from sandbox_ai_app import bridges_view
    page.directory = str(tmp_path)
    page.add_node("workspace", path=str(tmp_path))
    page.add_node("bridge", peer="docs", watch=True, actions=["alert", "pause"])
    looks, added = [], []

    def plan(args):
        looks.append(args)
        if len(looks) < 3:
            raise ValueError(f"nothing named {args[2]} runs (see sandbox-ai list)")
        return {"id": None, "state": "planned", "grants": {"a": {"actions": ["alert", "pause"]}},
                "warnings": ["data can flow from docs (more restricted) to " + args[2] + ", which reaches what it "
                                                                                       "cannot: host x.org"]}
    monkeypatch.setattr(page.sai, "bridge_plan", plan)
    monkeypatch.setattr(page.sai, "bridge_add", lambda args: added.append(args) or {"id": "abcdef01", "a": args[2],
                                                                                   "b": args[3], "notes": []})
    real = bridges_view.BridgeWaiter
    monkeypatch.setattr(bridges_view, "BridgeWaiter",
                        lambda parent, sai, entries: real(parent, sai, entries, every=20, seconds=30))
    assert page.start(embedded=True)
    argv = page.started[-1][0]
    name = argv[argv.index("--name") + 1]
    assert name.startswith("claude-board-") and "--name" not in argv[argv.index("--name") + 1:]
    assert wait(lambda: added and not widgets.busy(page), 15)
    assert len(looks) == 3, "asked again while the start did not run yet"
    assert added == [["bridge", "add", name, "docs", "--allow", "watch", "--ask=first",
                      "--observer-actions=alert,pause"]]
    question = next(text for title, text, _ in widgets.SHOWN if title == "New bridge" and text.startswith("Create"))
    assert "⚠ data can flow from docs" in question and "The observer may: alert, pause" in question
    assert wait(lambda: page.bridge_waiters == [], 5)


def test_a_session_start_keeps_its_name_and_a_refusal_is_said(page, tmp_path, monkeypatch, wait):
    from sandbox_ai_app import bridges_view
    page.directory = str(tmp_path)
    page.add_node("workspace", path=str(tmp_path))
    page.add_node("bridge", peer="docs", message=True, direction="in")
    added = []
    monkeypatch.setattr(page.sai, "bridge_plan", lambda args: (_ for _ in ()).throw(
        ValueError("docs was started by a version of sandbox-ai that cannot take an MCP server while it runs")))
    monkeypatch.setattr(page.sai, "bridge_add", lambda args: added.append(args))
    real = bridges_view.BridgeWaiter
    monkeypatch.setattr(bridges_view, "BridgeWaiter",
                        lambda parent, sai, entries: real(parent, sai, entries, every=20, seconds=30))
    assert page.start(embedded=True, session="work")
    argv = page.started[-1][0]
    assert "--name" not in argv and argv[argv.index("--session") + 1] == "work"
    assert wait(lambda: any(title == "Not bridged" for title, *_ in widgets.SHOWN), 10)
    text = next(text for title, text, _ in widgets.SHOWN if title == "Not bridged")
    assert "docs → work" in text and "cannot take an MCP server" in text and added == []


def test_a_bridge_part_on_another_host_is_made_there_once_both_run_there(page, tmp_path, monkeypatch, wait):
    """A board that starts on ct1: its start is ct1:NAME, a bare peer is a session of ct1 (the board's host), and the
    bridge is made once ct1 lists both as running (the launcher's dry run does not ask the other host)."""
    from sandbox_ai_app import bridges_view
    page.directory = str(tmp_path)
    page.add_node("workspace", path=str(tmp_path))
    page.add_node("bridge", peer="docs", message=True)
    monkeypatch.setattr(page, "host", lambda: "ct1")
    monkeypatch.setattr(page.sai, "host_launch_argv", lambda host, agent, argv, agent_args=(), session=None:
                        ["ssh", host, "sandbox-ai", agent, *argv])
    page.host_known[("ct1", "claude")] = None
    listed, plans, added = [], [], []

    def host_instances(host, timeout=60):
        listed.append(host)
        names = ["docs"] + ([name] if len(listed) > 2 else [])  # (the board's start runs from the third look)
        return [backend.listed_instance(host, {"name": n, "id": n, "kind": "session", "agent": "claude",
                                               "state": "running"}) for n in names]
    monkeypatch.setattr(page.sai, "host_instances", host_instances)
    monkeypatch.setattr(page.sai, "bridge_plan", lambda args: plans.append(args) or {"id": None, "state": "planned",
                                                                                     "grants": {}, "warnings": []})
    monkeypatch.setattr(page.sai, "bridge_add", lambda args: added.append(args) or {"id": "abcdef03", "host": "ct1",
                                                                                   "a": args[2], "b": args[3]})
    real = bridges_view.BridgeWaiter
    monkeypatch.setattr(bridges_view, "BridgeWaiter",
                        lambda parent, sai, entries: real(parent, sai, entries, every=20, seconds=30))
    assert page.run_start(["--workspace", str(tmp_path)], True, None)
    argv = page.started[-1][0]
    name = argv[argv.index("--name") + 1]
    assert argv[:2] == ["ssh", "ct1"] and name.startswith("claude-board-")
    assert wait(lambda: added and not widgets.busy(page), 15)
    assert len(listed) >= 3 and set(listed) == {"ct1"}, "asked on ct1 until its start ran there"
    assert added == plans == [["bridge", "add", f"ct1:{name}", "ct1:docs", "--allow", "message", "--ask=first"]]
    assert not any(title == "Bridge" for title, *_ in widgets.SHOWN)


def test_a_bridge_part_in_the_boards_host_context(tmp_path):
    """The other session as the board's host sees it: bare on that host, local:NAME of this machine, HOST:NAME of a
    third host; on this machine a bare name is this machine's."""
    _, node = bridged_board(tmp_path)
    values = lambda peer, host: B.bridge_values({**node.props, "peer": peer}, "me", host)  # noqa: E731
    assert (values("docs", "ct1")["a"], values("docs", "ct1")["b"]) == ("ct1:me", "ct1:docs")
    assert values("local:docs", "ct1")["b"] == "local:docs" and values("ct2:docs", "ct1")["b"] == "ct2:docs"
    assert (values("docs", None)["a"], values("docs", None)["b"]) == ("me", "docs")
    assert values("local:docs", "local")["b"] == "docs" and values("ct2:docs", "local")["b"] == "ct2:docs"
    node.props["direction"] = "in"
    assert (values("docs", "ct1")["a"], values("docs", "ct1")["b"]) == ("ct1:docs", "ct1:me")


def test_the_inspector_offers_every_hosts_sessions_grouped_by_host(page, tmp_path, monkeypatch, wait):
    instance = backend.Instance("session", "session:docs", "docs", session="docs", state="running", run="session-docs",
                                agent="claude", record={"kind": "agent", "workspace": str(tmp_path)})
    page.set_running({"session:docs": instance})
    asked = []

    def host_instances(host, timeout=60):
        asked.append(host)
        if host == "ct2":
            raise ValueError("ssh: connect to host ct2 port 22: Connection refused")
        return [backend.listed_instance(host, {"name": "web", "id": "web", "kind": "session", "agent": "codex",
                                               "state": "running"})]
    monkeypatch.setattr(page.sai, "remote_hosts", lambda: ["ct1", "ct2"])
    monkeypatch.setattr(page.sai, "host_instances", host_instances)
    page.add_node("bridge")
    node_id = page.inspected

    def combo():
        page.inspect(page.board.node(node_id))
        return [w for w in page.findChildren(QtWidgets.QComboBox) if w.accessibleName() == "Other session"][-1]
    assert wait(lambda: page.host_sessions.get("ct1") and "ct2" in page.host_session_problems, 10)
    field = combo()
    items = [(field.itemText(i), field.itemData(i), field.model().item(i).isEnabled()) for i in range(field.count())]
    assert items == [("This machine (the board's host)", None, False), ("  docs (claude)", "docs", True),
                     ("ct1", None, False), ("  ct1:web (codex)", "ct1:web", True)]
    assert sorted(set(asked)) == ["ct1", "ct2"]
    notes = [w.text() for w in page.findChildren(QtWidgets.QLabel) if "ct2: no answer" in w.text()]
    assert notes and "Connection refused" in notes[-1]
    field.activated.emit(field.findData("ct1:web"))
    assert page.board.node(node_id).props["peer"] == "ct1:web"
    # The board on ct1: ct1's sessions are its own (bare), this machine's are local:NAME.
    monkeypatch.setattr(page, "host", lambda: "ct1")
    field = combo()
    items = [(field.itemText(i), field.itemData(i)) for i in range(field.count())]
    assert items == [("ct1 (the board's host)", None), ("  web (codex)", "web"), ("This machine", None),
                     ("  local:docs (claude)", "local:docs")]
    page.board.node(node_id).props["peer"] = "docs"
    combo()
    assert any("Both sessions run on ct1: the bridge is made there" in w.text()
               for w in page.findChildren(QtWidgets.QLabel)), "two sessions of ct1: made there"
