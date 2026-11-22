"""sandbox-ai's presets with a bridge on the Board (user 2026-11-22: "example presets with a bridge"): PR.build puts a
Bridge part on the board from the preset's "bridge" - its other session left to choose there, the rest as the preset
gives it, a path's value as a placeholder of the board -, and the Board's own flow makes the bridge once the start runs,
after the launcher's dry run and the human's yes. A value of kind session is no placeholder (the part offers the
running sessions); a bridge this app cannot take refuses the preset."""
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, board as B, presets as PR, widgets  # noqa: E402
from test_board_view import page  # noqa: E402,F401 - the fixture

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


def documents(sai):
    return {document["id"]: document for document in PR.local_documents(sai)[0]}


def converted(sai, name, **changes):
    return PR.from_sandbox_ai(sai, {**documents(sai)[name], **changes})


def test_a_preset_with_a_bridge_puts_a_bridge_part_on_the_board(tmp_path):
    sai = backend.SandboxAI()
    preset = converted(sai, "pair-reviewer")
    assert preset.bridge == {"direction": "out", "allow": ["ask", "files:ro:<text:code>"], "both": False,
                             "ask": "first", "observer_actions": None, "limits": {}, "until": None}
    # The session is chosen on the Bridge part, not asked as a placeholder; the folder to read is one.
    assert [p.marker for p in preset.placeholders] == ["<text:code>"]
    assert [name for _, name in preset.value_names] == ["code"]
    board = PR.build(sai, preset, tmp_path, values={"<text:code>": "src"})
    nodes = B.bridge_nodes(board)
    assert len(nodes) == 1 and (board.first("agent").id, nodes[0].id) in board.edges
    assert nodes[0].props == {"peer": "", "direction": "out", "ask": True, "read": ["src"], "ask_mode": "first"}
    problems = [p for p in B.problems(sai, board) if p.node == nodes[0].id]
    assert [p.level for p in problems] == ["warning"] and "no bridge is made" in problems[0].text
    # Not filled in: the folder stays a placeholder of the board, to fill in there.
    left = PR.build(sai, preset, tmp_path)
    assert ("<text:code>", B.bridge_nodes(left)[0].id) in B.placeholders(left)
    # Never written into the project file.
    document, _ = B.project_document(sai, board)
    assert "files:ro:src" not in str(document) and "bridge" not in document and "--allow" not in str(document)


def test_the_observer_preset_and_every_part_of_a_bridge(tmp_path):
    sai = backend.SandboxAI()
    board = PR.build(sai, converted(sai, "observer"), tmp_path)
    assert B.bridge_nodes(board)[0].props == {"peer": "", "direction": "out", "watch": True, "ask_mode": "first",
                                              "actions": ["alert"]}
    full = {"peer": "{peer}", "direction": "in", "allow": ["message", "watch", "files:rw:out"], "both": True,
            "ask": "each", "observer_actions": ["alert", "hint"], "limits": {"rate": 30, "size": "8k"},
            "until": "2h"}
    props = B.bridge_nodes(PR.build(sai, converted(sai, "observer", bridge=full), tmp_path))[0].props
    assert props == {"peer": "", "direction": "in", "message": True, "watch": True, "write": ["out"], "both": True,
                     "until": "2h", "ask_mode": "each", "actions": ["alert", "hint"],
                     "limits": {"rate": "30", "size": "8k"}}
    props["peer"] = "coder"
    assert B.bridge_values(props, "obs")["a"] == "coder", "the other session reaches this start"
    for bad in ([], {"allow": ["exec"]}, {"allow": ["watch"], "direction": "up"}, {"allow": ["watch"], "until": "x"},
                {"allow": ["watch"], "limits": {"rate": "fast"}}, {"allow": ["files:ro:a\nb"]}):
        with pytest.raises(ValueError, match="bridge this app cannot take"):
            converted(sai, "observer", bridge=bad)


def test_preset_board_start_makes_the_bridge_once_both_run(page, tmp_path, monkeypatch, wait):
    """The preset's board started: once the start runs, the launcher's dry run is shown in the question and the human's
    yes makes the bridge with exactly the preset's grant (the other session chosen on the part)."""
    from sandbox_ai_app import bridges_view
    sai = page.sai
    page.directory = str(tmp_path)
    page.new_from_preset(converted(sai, "pair-reviewer"), {"<text:code>": "src"})
    node = B.bridge_nodes(page.board)[0]
    node.props["peer"] = "coder"
    looks, added = [], []

    def plan(args):
        looks.append(args)
        return {"id": None, "state": "planned", "grants": {"a": {}}, "warnings": []}
    monkeypatch.setattr(page.sai, "bridge_plan", plan)
    monkeypatch.setattr(page.sai, "bridge_add", lambda args: added.append(args) or {"id": "abcdef01", "a": args[2],
                                                                                   "b": args[3], "notes": []})
    real = bridges_view.BridgeWaiter
    monkeypatch.setattr(bridges_view, "BridgeWaiter",
                        lambda parent, sai_, entries: real(parent, sai_, entries, every=20, seconds=30))
    assert page.start(embedded=True)
    argv = page.started[-1][0]
    name = argv[argv.index("--name") + 1]
    assert "--preset" not in argv, "the board starts its options as typed"
    assert wait(lambda: added and not widgets.busy(page), 15)
    assert added == [["bridge", "add", name, "coder", "--allow", "ask,files:ro:src", "--ask=first"]]
    assert any(title == "New bridge" and text.startswith("Create") for title, text, _ in widgets.SHOWN)
    assert wait(lambda: page.bridge_waiters == [], 5)
