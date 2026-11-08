"""The Sandboxes page's "While it runs" box offscreen: Add MCP server… and Remove MCP server… on a running kept start
of this machine or a host (asked first: mcp-attach, mcp-detach; the agent's restart offered where it needs one), Add
workspace… with a file or a name; the Connections page on a host; the Model choice of the Start page and the Board
from the host it starts on. Against stand-ins that record what they were asked: nothing reaches a host, nothing
starts."""
import json
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, widgets  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")
HOSTS = [{"name": "local", "kind": "local"}, {"name": "ct1", "kind": "ssh", "ssh": "admin@192.0.2.10"}]


def at_once(function, *args, done, **kw):
    """widgets.background, but on this thread: DONE gets the result or the error at once."""
    try:
        result = function(*args)
    except Exception as exc:  # noqa: BLE001 - as background hands it on
        done(None, exc)
        return
    done(result, None)


@pytest.fixture
def sai(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    sai = backend.SandboxAI()
    sai.asked, sai.answers = [], {}

    def host_run(name, *args, timeout=120, input=None):
        sai.asked.append((name, *args))
        for length in range(len(args), 0, -1):
            if args[:length] in sai.answers:
                answer = sai.answers[args[:length]]
                return answer(name, args) if callable(answer) else answer
        return 0, "", ""
    monkeypatch.setattr(sai, "host_run", host_run)
    monkeypatch.setattr(sai, "hosts_supported", lambda: True)
    monkeypatch.setattr(sai, "host_entries", lambda: [dict(h) for h in HOSTS])
    monkeypatch.setattr(sai, "host_mcp_servers", lambda host: [("fetch", "running", "", "python3 fetch"),
                                                               ("web", "stopped", "api", "x")])
    return sai


@pytest.fixture
def ui(qapp, sai, monkeypatch):
    from sandbox_ai_app import board_view, ui as module
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    asked = []

    def ask_first(parent, action, title, text, accept, detail=None, details=None):
        if backend.needs_confirm(action, detail) is None:  # as widgets.ask_first: nothing to confirm
            return True
        asked.append((action, text, accept))
        return module.answer_yes
    for target in (module, board_view):
        monkeypatch.setattr(target, "ask_first", ask_first)
        monkeypatch.setattr(target, "background", at_once)
    module.asked, module.answer_yes = asked, True
    return module


def ran(sai, *words):
    """The last command asked of a host that begins with WORDS (host first)."""
    return next((asked for asked in reversed(sai.asked) if asked[:len(words)] == words), None)


def page_on(ui, sai, monkeypatch, instance):
    page = ui.SandboxesPage(sai, lambda *a, **k: None)
    monkeypatch.setattr(page, "current", lambda: instance)
    return page


def attached(restart):
    return lambda host, args: (0, json.dumps({"attached": "live", "agent_restart": restart, "message":
                                              f"{args[-3]} reaches the MCP server now. As said there."}), "")


def test_add_mcp_server_to_a_running_start_here_and_restart_the_agent(ui, sai, monkeypatch):
    instance = backend.Instance("session", "session:api", "api", run="claude-1", session="api", state="running",
                                agent="claude", record={"mcp_servers": []})
    page = page_on(ui, sai, monkeypatch, instance)
    page.update_buttons()
    assert page.run_named["add-mcp"].isEnabled() and page.run_named["remove-mcp"].isEnabled()
    seen = []

    def choose(dialog):
        seen.append([dialog.choice.itemText(i) for i in range(dialog.choice.count())])
        dialog.choice.setCurrentIndex(dialog.choice.findData("server:fetch"))
        return QtWidgets.QDialog.DialogCode.Accepted
    monkeypatch.setattr(ui.LiveMcpDialog, "exec", choose)
    sai.answers[("mcp", "attach")] = attached("maybe")
    page.add_mcp()
    assert seen[0] == ["fetch (running)", "web (stopped)", "Claude Code as a helper", "Codex as a helper",
                       "OpenCode as a helper"]
    assert ran(sai, "local", "mcp", "attach") == ("local", "mcp", "attach", "fetch", "api", "--live", "--json")
    assert [action for action, *_ in ui.asked] == ["mcp-attach", "start-restart"]
    assert "reaches the MCP server now. As said there." in ui.asked[1][1], "sandbox-ai's message, as it is"
    assert ui.asked[1][2] == "Restart the agent now (the conversation goes on)"
    assert ran(sai, "local", "resume") == ("local", "resume", "--restart", "api")
    # Declined: nothing restarts, and nothing attaches when the attach itself is declined.
    sai.asked.clear()
    ui.answer_yes = False
    page.add_mcp()
    assert ran(sai, "local", "mcp") is None and ran(sai, "local", "resume") is None


def test_a_helper_on_a_host_and_the_restart_codex_needs(ui, sai, monkeypatch):
    instance = backend.Instance("session", "remote:ct1:api", "api", host="ct1", state="running", agent="codex")
    page = page_on(ui, sai, monkeypatch, instance)
    page.update_buttons()
    assert page.run_named["add-mcp"].isEnabled()
    monkeypatch.setattr(ui.LiveMcpDialog, "exec", lambda dialog: (dialog.choice.setCurrentIndex(
        dialog.choice.findData("helper:opencode")), QtWidgets.QDialog.DialogCode.Accepted)[-1])
    sai.answers[("mcp", "attach")] = attached("required")
    page.add_mcp()
    assert ran(sai, "ct1", "mcp") == ("ct1", "mcp", "attach", "--helper", "opencode", "api", "--live", "--json")
    assert ui.asked[0][0] == "mcp-attach" and ui.asked[0][1].startswith("On ct1: ")
    assert "only after a restart" in ui.asked[1][1]
    assert ran(sai, "ct1", "resume") == ("ct1", "resume", "--restart", "api")
    # Refused there: its reason is said, nothing is offered.
    ui.asked.clear()
    sai.answers[("mcp", "attach")] = (1, "", "sandbox-mcp: api was started by a version of sandbox-ai that cannot "
                                             "take an MCP server while it runs\n")
    page.add_mcp()
    assert [action for action, *_ in ui.asked] == ["mcp-attach"]
    assert any("Not attached: api was started by a version" in text for _, text, _ in widgets.SHOWN)


def test_the_buttons_say_why_not(ui, sai, monkeypatch):
    for kind, state, why in (("run", "running", "Only for kept starts"), ("session", "paused", "resume it first"),
                             ("mcp", "running", "An MCP server's sandbox")):
        instance = backend.Instance(kind, f"{kind}:x", "x", run="claude-1", session="x" if kind == "session" else None,
                                    state=state, agent="claude")
        page = page_on(ui, sai, monkeypatch, instance)
        page.update_buttons()
        button = page.run_named["add-mcp"]
        assert not button.isEnabled() and why in button.toolTip(), (kind, button.toolTip())
        page.close()


def test_remove_mcp_server_from_what_the_start_reaches_now(ui, sai, monkeypatch):
    instance = backend.Instance("session", "remote:ct1:api", "api", host="ct1", state="running", agent="claude",
                                record={"mcp_running": ["fetch", "helper-claude-0123abcd"], "mcp": ["later"]})
    page = page_on(ui, sai, monkeypatch, instance)
    offered = []

    def pick(parent, title, label, items, current, editable):
        offered.append(list(items))
        return items[1], True
    monkeypatch.setattr(ui.QInputDialog, "getItem", pick)
    sai.answers[("mcp", "detach")] = (0, json.dumps({"detached": "live", "message": "api no longer reaches it"}), "")
    page.remove_mcp()
    assert offered[0] == ["fetch", "Claude Code as a helper (helper-claude-0123abcd)"], "only what it reaches now"
    assert ran(sai, "ct1", "mcp") == ("ct1", "mcp", "detach", "--helper", "claude", "api", "--live", "--json")
    assert ui.asked[-1][0] == "mcp-detach" and ui.asked[-1][1].startswith("On ct1: ")
    assert any("api no longer reaches it" in text for _, text, _ in widgets.SHOWN)
    # Nothing in its rows: whom `mcp list` there says each server serves.
    sai.asked.clear()
    instance.record = {}
    monkeypatch.setattr(ui.QInputDialog, "getItem", lambda parent, title, label, items, current, editable: (
        offered.append(list(items)), (items[0], True))[-1])
    page.remove_mcp()
    assert offered[-1] == ["web"] and ran(sai, "ct1", "mcp") == ("ct1", "mcp", "detach", "web", "api", "--live",
                                                                 "--json")
    ui.answer_yes = False
    sai.asked.clear()
    page.remove_mcp()
    assert ran(sai, "ct1", "mcp") is None, "declined: nothing detached"


def test_the_workspace_dialog_takes_a_file_or_a_folder_and_a_name(ui, sai, monkeypatch):
    dialog = ui.WorkspaceDialog(None, "claude-1")
    ok = dialog.buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok)
    assert sorted(b.text() for b in dialog.browse_buttons) == ["File…", "Folder…"]
    for bad in ("-x", ".hidden", "a/b", "x" * 65):
        dialog.name_edit.setText(bad)
        assert not ok.isEnabled() and dialog.problem.isVisibleTo(dialog), bad
    dialog.name_edit.setText("notes.txt")
    assert ok.isEnabled() and not dialog.problem.isVisibleTo(dialog)
    monkeypatch.setattr(ui, "choose_file", lambda parent, title, key="file": "/home/me/notes.txt")
    next(b for b in dialog.browse_buttons if b.text() == "File…").click()
    assert dialog.directory() == "/home/me/notes.txt"
    dialog.deleteLater()
    remote = ui.WorkspaceDialog(None, "api", host="ct1")
    assert remote.browse_buttons == [] and "on ct1" in remote.windowTitle()
    remote.deleteLater()
    # Added with its name, read-only: the list says file or folder and that it is named.
    instance = backend.Instance("session", "remote:ct1:api", "api", host="ct1", state="running", agent="claude")
    page = page_on(ui, sai, monkeypatch, instance)

    def fill(dialog):
        dialog.edit.setText("/srv/notes.txt")
        dialog.name_edit.setText("notes")
        dialog.read_only.setChecked(True)
        return QtWidgets.QDialog.DialogCode.Accepted
    monkeypatch.setattr(ui.WorkspaceDialog, "exec", fill)
    page.add_workspace()
    assert ran(sai, "ct1", "workspace") == ("ct1", "workspace", "add", "api", "/srv/notes.txt", "--ro", "--name=notes")
    assert ui.asked == [], "read-only: nothing to confirm"
    assert ui.workspace_label({"sandbox": "/workspaces/notes", "host": "/srv/notes.txt", "kind": "file", "named": True,
                               "mode": "ro"}) == "/workspaces/notes ← /srv/notes.txt (file, named, read-only)"
    assert ui.workspace_label({"sandbox": "/workspaces/x", "host": "/srv/x", "mode": "rw"}) == \
        "/workspaces/x ← /srv/x (folder)"


def test_the_connections_page_reads_a_hosts_log(ui, sai, monkeypatch):
    sai.answers[("list", "--json")] = (0, json.dumps([
        {"name": "api", "kind": "session", "state": "running", "agent": "claude", "workspace": "/srv/project"}]), "")
    record = {"kind": "network", "time": "2026-11-08T10:00:00Z", "summary": "denied: evil.example",
              "details": {"verdict": "denied", "event": "denied", "destination": "evil.example:443",
                          "reason": "not allowed"}}
    sai.answers[("activity",)] = (0, json.dumps(record), "")
    sai.answers[("logs",)] = (0, json.dumps({"directory": "/srv/logs", "denied": 1, "targets": [["DNS evil", 1]],
                                             "telemetry": {"requests": 3, "hosts": [["pypi.org", 3]]},
                                             "refusals": [{"time": "2026-11-08T10:00:00", "what": "GET evil",
                                                           "reason": "not allowed"}]}), "")
    page = ui.ConnectionsPage(sai)
    page.hosts.set_host("ct1")
    assert page.remote() and not page.file.isVisibleTo(page)
    assert page.directory.currentData() == ["--session", "api"]
    assert ran(sai, "ct1", "activity") == ("ct1", "activity", "--session", "api", "--json", "--tail", "5000",
                                           "--kind=network")
    assert page.table.rowCount() == 1 and page.table.item(0, 1).text() == "denied"
    assert "evil.example:443" in page.table.item(0, 4).text()
    page.show_summary()
    assert ran(sai, "ct1", "logs") == ("ct1", "logs", "--project=/srv/project", "--json")
    title, text, _ = widgets.SHOWN[-1]
    assert title == "Logs on ct1" and "Requests: 3" in text and "not allowed" in text
    monkeypatch.setattr(ui.QInputDialog, "getText", lambda *a, **k: ("/srv/sandbox-ai/logs/p", True))
    page.browse()
    assert page.directory.currentData() == ["--log-dir", "/srv/sandbox-ai/logs/p"]
    assert ran(sai, "ct1", "activity")[2:4] == ("--log-dir", "/srv/sandbox-ai/logs/p")
    page.open_remote("ct1", ["--session", "api"])
    assert page.directory.currentData() == ["--session", "api"]
    page.hosts.set_host("local")
    assert not page.remote() and page.file.isVisibleTo(page)
    page.close()


def test_the_model_choice_lists_the_servers_of_the_host_it_starts_on(ui, sai, monkeypatch):
    monkeypatch.setattr(sai, "llm_endpoints", lambda: [{"name": "here", "kind": "ollama", "models": []}])
    sai.answers[("llm", "list")] = (0, json.dumps([{"name": "gpu", "kind": "ollama", "url": "http://127.0.0.1:11434",
                                                    "models": []}]), "")
    sai.answers[("llm", "models")] = (0, json.dumps([{"name": "llama3:8b"}]), "")
    page = ui.LaunchPage(sai, lambda *a, **k: None)
    if "--llm" not in page.form.widgets:
        pytest.skip("this sandbox-ai has no --llm")

    def values(combo):
        return [combo.itemData(i) for i in range(combo.count())]
    assert "here" in values(page.model_choice)
    assert page.host_choice.choose("ct1")
    assert values(page.model_choice) == ["", "gpu", "gpu:llama3:8b"], "that host's servers, not this machine's"
    assert ("ct1", "llm", "models", "gpu", "--json") in sai.asked
    assert page.host_choice.choose("local")
    assert "here" in values(page.model_choice) and "gpu" not in values(page.model_choice)
    page.close()
    board = ui.BoardPage(sai, lambda *a, **k: None)
    assert board.host_choice.choose("ct1")
    board.load_llm_servers()
    assert [s["name"] for s in board.llm_servers] == ["gpu"] and board.llm_servers[0]["models"] == ["llama3:8b"]
    assert board.host_choice.choose("local")
    assert board.llm_servers is None, "another host: read again"
    board.load_llm_servers()
    assert [s["name"] for s in board.llm_servers] == ["here"]
    board.close()
