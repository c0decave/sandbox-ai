"""Review round 5 of the app: trusting a project file shows everything trusted, against the version trusted last
(security H2); a Board start keeps the project file's security settings (A1); USB devices by identity (A2); MCP
servers out of a stopped start (A3); 0.1.78-only options only to hosts that know them (A4); an LLM server's CA dropped
(A5); remote starts name their workspace as given where the host knows it (L10)."""
import hashlib
import json
import os
import shlex

import pytest

from sandbox_ai_app import backend, board as B, project_trust as PT, widgets

needs_sai = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                               reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")

# What an agent that can write the workspace plants: lax mode (every destination, no approvals), no canaries, a kept
# HOME - none of it a part of the board.
PLANTED = {"version": 1, "agent": "claude", "options": [], "mode": "lax", "canaries": False, "keep": True,
           "log_debug": True}


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    return tmp_path


@pytest.fixture
def sai(isolated):
    return backend.SandboxAI()


def plant(directory, document, sai):
    """DOCUMENT written as the app would format it (an agent can read the app's code and do the same)."""
    board = B.from_project(sai, document, directory)
    written, _ = B.project_document(sai, board)
    (directory / ".sandbox-ai.json").write_text(json.dumps(written, indent=2) + "\n")
    return written


# -- security H2: what is trusted is shown, every key, against the version trusted last --


@needs_sai
def test_trusting_an_agent_written_file_names_every_key_and_marks_what_weakens(sai, isolated):
    project = isolated / "project"
    project.mkdir()
    written = plant(project, PLANTED, sai)
    assert written["mode"] == "lax" and written["canaries"] is False, "the board carries the keys it does not show"
    question, details = PT.question(sai, project, written, "Write and trust?")
    for key in ("mode: lax", "canaries: false", "keep: true", "log_debug: true"):
        assert f"{PT.WARN} {key}" in question, (key, question)
    assert "agent: claude" in question
    # The diff is never against the file in the folder (whoever wrote it would see "no change"): nothing was trusted.
    assert details.startswith("Nothing was trusted for this file before") and '"mode": "lax"' in details
    assert "No change." not in details


@needs_sai
def test_a_trusted_file_changed_by_the_agent_is_diffed_against_the_trusted_version(sai, isolated):
    project = isolated / "project"
    project.mkdir()
    trusted = {"version": 1, "agent": "claude", "options": ["--offline"]}
    sai.write_project(project, trusted)  # written and trusted by the app: its copy is kept
    planted = dict(PLANTED, options=["--offline"])
    written = plant(project, planted, sai)
    review = sai.review_project(project)
    assert review.changed and not review.trusted
    question, details = PT.question(sai, project, written, "Trust?", new_text=review.data.decode())
    assert "Changes against the version you trusted last:" in details
    assert '+  "mode": "lax",' in details and '+  "canaries": false,' in details, details
    state, data = sai.trusted_baseline(project)
    assert state == "kept" and json.loads(data) == trusted


@needs_sai
def test_a_version_trusted_elsewhere_is_shown_whole_and_a_tampered_copy_is_no_copy(sai, isolated):
    project = isolated / "project"
    project.mkdir()
    data = (json.dumps({"version": 1, "agent": "claude", "options": []}, indent=2) + "\n").encode()
    sai.launcher.trust_config(project / ".sandbox-ai.json", digest=hashlib.sha256(data).hexdigest())
    plant(project, PLANTED, sai)  # another version in the folder than the one trusted
    assert sai.trusted_baseline(project) == ("unknown", None)
    _, details = PT.question(sai, project, PLANTED, "Trust?")
    assert details.startswith("A different version was trusted before")
    # A copy whose bytes do not match its name is no copy.
    folder = backend.trusted_copies_dir()
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{hashlib.sha256(data).hexdigest()}.json").write_bytes(b'{"version": 1, "agent": "claude"}\n')
    assert sai.trusted_baseline(project) == ("unknown", None)


@needs_sai
def test_unknown_keys_are_never_carried_and_the_extra_keys_are_named_above_the_board(sai, isolated):
    project = isolated / "project"
    project.mkdir()
    board = B.from_project(sai, dict(PLANTED, surprise={"x": 1}), project)
    document, _ = B.project_document(sai, board)
    assert "surprise" not in document and "surprise" not in board.document_extra
    assert PT.unshown(document) == {"mode": "lax", "canaries": False, "keep": True, "log_debug": True}
    assert set(PT.weakening(sai, document)) == {"mode", "canaries", "keep", "log_debug"}
    assert PT.weakening(sai, {"mode": "high", "canaries": ["credentials", "home"], "keep": False}) == {}


@needs_sai
def test_the_board_apply_question_shows_the_hidden_keys_and_the_whole_file(qapp, sai, isolated, monkeypatch, wait):
    pytest.importorskip("PySide6.QtWidgets")
    from sandbox_ai_app.board_view import BoardPage
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    (isolated / "cwd").mkdir()
    monkeypatch.chdir(isolated / "cwd")
    project = isolated / "project"
    project.mkdir()
    plant(project, PLANTED, sai)
    page = BoardPage(sai, lambda *a, **k: None, lambda: {"board_grid": 20})
    try:
        assert page.open_project(str(project), quiet=True)
        notice = page.notice.text()
        assert "not trusted yet" in notice and f"{PT.WARN} mode: lax" in notice, notice
        assert page.apply()
        title, text, details = widgets.SHOWN[-1]
        assert title == "Write and trust"
        assert f"{PT.WARN} mode: lax" in text and f"{PT.WARN} canaries: false" in text and f"{PT.WARN} keep: true" in text
        assert "Nothing was trusted for this file before" in details and '"mode": "lax"' in details
        assert wait(lambda: not widgets.busy(page), 60)
        assert sai.review_project(project).trusted
        assert sai.trusted_baseline(project)[0] == "kept", "what the app trusted is kept to diff the next change"
    finally:
        page.deleteLater()


@needs_sai
def test_the_start_page_asks_the_same_way(qapp, sai, isolated, monkeypatch):
    pytest.importorskip("PySide6.QtWidgets")
    from sandbox_ai_app import ui
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)  # declined: nothing trusted
    monkeypatch.setattr(widgets, "SHOWN", [])
    project = isolated / "project"
    project.mkdir()
    plant(project, PLANTED, sai)
    form = ui.LaunchPage(sai, lambda *a, **k: None)
    try:
        assert form.load_project(str(project)) is False
        title, text, details = widgets.SHOWN[-1]
        assert f"{PT.WARN} mode: lax" in text and "Nothing was trusted for this file before" in details
        assert not sai.review_project(project).trusted
    finally:
        form.deleteLater()


# -- A1: a Board start keeps the project file's security settings --


@needs_sai
def test_a_board_start_keeps_every_key_of_the_project_file(sai, isolated):
    project = isolated / "project"
    project.mkdir()
    document = {"version": 1, "agent": "claude", "options": [], "mode": "paranoid",
                "canaries": ["home", "workspace"], "keep": False, "log_debug": True, "name": "proj-x",
                "rules": {"version": 1, "http": [{"id": "no-posts", "decision": "deny", "method": "POST"}]}}
    board = B.from_project(sai, document, project)
    argv = B.launcher_argv(sai, board)
    assert argv[:9] == ["--mode", "paranoid", "--canaries", "home,workspace", "--no-keep", "--log-debug",
                        "--project-name", "proj-x", "--workspace"] or argv[:8] == [
        "--mode", "paranoid", "--canaries", "home,workspace", "--no-keep", "--log-debug", "--project-name", "proj-x"]
    rules = argv[argv.index("--rules") + 1]
    assert json.loads(open(rules).read()) == document["rules"] and not rules.startswith(str(project))
    assert argv[-1] == "--no-config"
    parsed = sai.launcher.parse_args(["claude", *argv])
    assert (parsed.mode, parsed.canaries, parsed.keep, parsed.log_debug, parsed.project_name) == \
        ("paranoid", "home,workspace", False, True, "proj-x")
    assert "--rules" not in B.launcher_argv(sai, board, local=False), "a local file means nothing on another host"
    assert "--mode paranoid" in B.command_line(sai, board)
    assert B.launcher_argv(sai, B.from_project(sai, {"version": 1, "agent": "claude", "canaries": False}, project))[
        :1] == ["--no-canaries"]


@needs_sai
def test_a_board_dry_run_reports_the_project_mode(sai, isolated, monkeypatch):
    project = isolated / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    board = B.from_project(sai, {"version": 1, "agent": "claude", "options": [], "mode": "high"}, project)
    plan, message = sai.dry_run("claude", B.launcher_argv(sai, board))
    assert plan is not None, message
    assert plan["mode"]["name"] == "high", plan["mode"]


# -- A2, A3, A4, A5, L10 --


def test_usb_devices_by_identity_are_taken_as_the_cli_takes_them():
    assert backend.valid_device("usb:1d6b:0002") and backend.valid_device("usb:1D6B:0002:SER-1")
    assert backend.valid_device("/dev/video0")
    for bad in ("usb:1d6b", "usb:xyz1:0002", "usb:1d6b:0002:", "--usb", "usb:1d6b:0002:a b", "/dev/../etc"):
        assert not backend.valid_device(bad), bad


def test_mcp_detach_without_live_for_a_stopped_start():
    assert backend.mcp_live_args("api", "fetch", detach=True, live=False) == ["mcp", "detach", "fetch", "api", "--json"]
    assert backend.mcp_live_args("api", None, "codex", detach=True, live=False) == [
        "mcp", "detach", "--helper", "codex", "api", "--json"]
    assert backend.mcp_live_args("api", "fetch", detach=True) == ["mcp", "detach", "fetch", "api", "--live", "--json"]
    with pytest.raises(ValueError):
        backend.mcp_live_args("api", "fetch", live=False)  # an attach without --live is mcp attach


@pytest.fixture
def remote(isolated, monkeypatch):
    """A SandboxAI whose host ct1 runs an older sandbox-ai (0.1.77): its --help texts lack the newer options."""
    sai = backend.SandboxAI()
    sai.asked = []
    helps = {("net", "show"): "  --keep  keep\n", ("wizard", "trust"): "  --show  show\n  --json  json\n",
             ("workspace", "add"): "  --ro  read-only\n", ("mcp", "attach"): "  --restart  r\n  --json  j\n",
             ("mcp", "detach"): "  --json  j\n", ("approvals", "allow"): "  --scope S\n"}

    def host_run(name, *args, timeout=120, input=None):
        sai.asked.append((name, *args))
        if args and args[-1] == "--help":
            return 0, helps.get(tuple(args[:-1]), ""), ""
        return 0, "[]", ""
    monkeypatch.setattr(sai, "host_run", host_run)
    monkeypatch.setattr(sai, "host_statuses", lambda refresh=False, cached=True: {"ct1": {"version": "0.1.77"}})
    return sai


@needs_sai
def test_options_an_older_host_lacks_are_said_not_sent(remote):
    sai = remote
    status, _, err = sai.net_keep("ct1", "api", every=True)
    assert status == 2 and "too old" in err and "--keep-all" in err and "Hosts › Update" in err
    assert not any(a[1:3] == ("net", "show") and "api" in a for a in sai.asked), "nothing sent"
    assert sai.net_keep("ct1", "api")[0] == 0, "--keep it knows"
    with pytest.raises(backend.HostTooOld):
        sai.trusted_projects("ct1")
    status, _, err = sai.add_workspace("api", "/srv/x", name="data", host="ct1")
    assert status == 2 and "--name" in err
    assert sai.add_workspace("api", "/srv/x", host="ct1")[0] == 0
    with pytest.raises(backend.HostTooOld):
        sai.host_mcp_attach_live("ct1", "api", "fetch")
    status, _, err = sai.host_answer("ct1", ["approvals", "allow", "q1", "--scope", "once", "--no-open"])
    assert status == 2 and "--no-open" in err
    assert sai.host_answer("ct1", ["approvals", "allow", "q1", "--scope", "once"])[0] == 0
    assert sai.net_keep("local", "api")[0] in (0, 1, 2)  # this machine: never asked for its --help


def test_an_llm_servers_ca_can_be_dropped():
    from sandbox_ai_app import wizard_llm as WL
    fields = {"name": "gw", "kind": "ollama", "url": "https://gw.example.org", "replace": True, "no_ca": True}
    assert WL.server_argv(fields)[-2:] == ["--no-ca", "--replace"]
    assert "--no-ca" not in WL.server_argv(dict(fields, ca="/etc/ca.pem")), "a new CA replaces it instead"
    assert "--no-ca" not in WL.server_argv(dict(fields, replace=False))
    old = {"name": "gw", "kind": "ollama", "url": "https://gw.example.org", "ca": "", "has_ca": True}
    assert any("none" in change for change in WL.changes(old, dict(old, no_ca=True)))


@needs_sai
def test_a_remote_start_names_its_workspace_as_given_where_the_host_knows_it(remote):
    sai = remote
    monkey_terminal = lambda name, *args: ["ssh", "-tt", name, shlex.join(args)]  # noqa: E731
    sai.host_terminal = monkey_terminal
    argv = sai.host_launch_argv("ct1", "claude", ["--workspace", "/srv/x"])
    assert "--workspaces-as-given" not in argv[-1], "options not read yet: as it is"
    sai.__dict__.setdefault("_launch_options", {})[("ct1", "claude", "0.1.78")] = (1.0, {"--workspace",
                                                                                      "--workspaces-as-given"})
    assert sai.host_launch_argv("ct1", "claude", ["--workspace", "/srv/x"])[-1].endswith("--workspaces-as-given")
    assert "--workspaces-as-given" not in sai.host_launch_argv("ct1", "claude", ["--mode", "high"])[-1]


# -- UX H2: every host chooser follows the hosts file (the fake fleet: hosts added and removed with the real CLI) --


def choices(widget):
    """The hosts a chooser offers (a HostChooser, a HostCombo or a plain combo)."""
    combo = getattr(widget, "box", widget)
    return [combo.itemData(i) for i in range(combo.count())]


def chosen_host(widget):
    combo = getattr(widget, "box", widget)
    return combo.currentData()


@needs_sai
def test_every_host_chooser_follows_hosts_added_and_removed(qapp, isolated, monkeypatch):
    pytest.importorskip("PySide6.QtWidgets")
    from sandbox_ai_app import hosts_page, terminal, ui
    monkeypatch.setattr(terminal, "available", lambda web=None: (False, "not in this test"))
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    monkeypatch.setattr(hosts_page, "background", lambda function, *args, done, **kw: done(function(*args), None))
    sai = backend.SandboxAI()
    if not sai.hosts_supported():
        pytest.skip("this sandbox-ai has no hosts")
    sai.host_add("ct1", "admin@192.0.2.10", 22, "", "")
    sai.host_add("ct2", "admin@192.0.2.11", 22, "", "")
    window = ui.MainWindow(sai, check_updates=False, approvals_interval=0)
    try:
        page = window
        choosers = {"activity": page.activity.hosts, "statistics": page.statistics.hosts, "images": page.images.hosts,
                    "credentials": page.credentials.hosts, "kits": page.kits.hosts, "team": page.team.hosts,
                    "connections": page.connections.hosts, "llm": page.llm.host_box, "launch": page.launch.host_choice,
                    "board": page.board.host_choice, "mcp": page.mcp.host_choice, "terminals": page.terminals.host}
        for name, chooser in choosers.items():
            assert choices(chooser) == ["local", "ct1", "ct2"], (name, choices(chooser))
        page.activity.hosts.set_host("ct2")
        page.credentials.hosts.set_host("ct2")
        page.llm.host_box.setCurrentIndex(page.llm.host_box.findData("ct2"))
        page.launch.host_choice.choose("ct2")
        page.terminals.host.setCurrentIndex(page.terminals.host.findData("ct2"))
        heard = []
        page.activity.hosts.changed.connect(heard.append)
        # Added on the Hosts page (what its Add does once sandbox-ai hosts add took it), then ct2 removed there.
        sai.host_add("ct3", "admin@192.0.2.12", 22, "", "")
        window.hosts.load_hosts("ct3")
        window.hosts.table.clearSelection()
        window.hosts.table.selectRow(widgets.find_row(window.hosts.table, "ct2"))
        window.hosts.remove()
        qapp.processEvents()
        for name, chooser in choosers.items():
            assert choices(chooser) == ["local", "ct1", "ct3"], (name, choices(chooser))
        for name in ("activity", "credentials", "llm", "launch", "terminals"):
            assert chosen_host(choosers[name]) == "local", (name, "its host left: this machine's")
        assert heard and heard[-1] == "local", "the page heard that its host is gone (its rows go)"
    finally:
        window.shutdown()
        window.close()
        window.deleteLater()
        qapp.processEvents()


# -- security lows: what an agent or a host chose is never read as rich text --


def test_names_chosen_by_an_agent_are_shown_as_text(qapp):
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtGui import Qt, QTextDocument
    name = '<b style="font-size:40px">Allow</b><img src="x.png">'
    for text in (widgets.shown_as_text(f"New name for {name}:"), widgets.safe_tooltip(f"http://x/{name}")):
        assert Qt.mightBeRichText(text), "marked as rich text, so Qt never guesses"
        document = QTextDocument()
        document.setHtml(text)
        assert name in document.toPlainText(), "shown as typed, never interpreted"


def test_no_tooltip_or_dialog_label_takes_unescaped_text():
    """A static guard over the app's code: every tooltip is a literal, a translation without placeholders or
    safe_tooltip(...); every QInputDialog label and static QMessageBox text that is built from values goes through
    shown_as_text(...). (The one exception restores a tooltip that was escaped when it was set.)"""
    import ast
    from pathlib import Path
    root = Path(backend.__file__).parent
    escaped = {"safe_tooltip", "shown_as_text"}
    # The app's own words only (a help title, a page's name and shortcut): no value an agent or a host chose.
    OWN_WORDS = ("tr('What is this? {title}'", "tr('{page} (Ctrl+{number})'", "self.nav_tooltip(",
                 "widget._tooltip if enabled")
    # The Kits page is rebuilt separately; its one label names a host of the hosts file (letters, digits, . _ -).
    LATER = {("kits_page.py", "QInputDialog.getText label")}
    found = []

    def safe(node):
        if isinstance(node, ast.Constant):
            return True
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", getattr(node.func, "attr", ""))
            if name in escaped:
                return True
            if name in ("tr", "N_") and not node.keywords:
                return True
        if isinstance(node, ast.IfExp):
            return safe(node.body) and safe(node.orelse)
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "tr":
            return all(safe(k.value) for k in node.keywords)  # placeholders filled with the app's words
        return False
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            attr, owner = node.func.attr, ast.unparse(node.func.value)
            if attr == "setToolTip" and node.args:
                text = ast.unparse(node.args[-1])
                if not safe(node.args[-1]) and text not in ("tip", "widget._tooltip") and not text.startswith(OWN_WORDS):
                    found.append(f"{path.name}:{node.lineno} setToolTip({ast.unparse(node.args[-1])[:60]})")
            elif owner == "QInputDialog" and attr in ("getText", "getItem") and len(node.args) >= 3:
                if not safe(node.args[2]) and (path.name, f"QInputDialog.{attr} label") not in LATER:
                    found.append(f"{path.name}:{node.lineno} QInputDialog.{attr} label")
            elif owner == "QMessageBox" and attr in ("information", "warning", "critical", "question") \
                    and len(node.args) >= 3 and not safe(node.args[2]):
                found.append(f"{path.name}:{node.lineno} QMessageBox.{attr} text")
    assert found == [], "\n".join(found)


# -- M1, M2, M3, M4 and the lows on the pages --


@pytest.fixture
def window(qapp, isolated, monkeypatch):
    pytest.importorskip("PySide6.QtWidgets")
    from sandbox_ai_app import terminal, ui
    monkeypatch.setattr(terminal, "available", lambda web=None: (False, "not in this test"))
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    win = ui.MainWindow(backend.SandboxAI(), check_updates=False, approvals_interval=0)
    if win.tray is not None:
        win.tray.hide()
        win.tray = None
    yield win
    win.shutdown()
    win.close()
    win.deleteLater()
    qapp.processEvents()


@needs_sai
def test_the_start_form_speaks_german_and_marks_what_weakens(qapp, isolated, monkeypatch):
    from sandbox_ai_app import i18n, labels, ui
    i18n.set_language("de")
    try:
        form = ui.OptionsForm(backend.SandboxAI().options())
        assert form.labels["--workspace-ro"].text() == "Alle Arbeitsordner schreibgeschützt einbinden"
        assert form.labels["--workspace-ro"].flag.text().startswith("--workspace-ro")
        assert form.widgets["--workspace-ro"].text() == "Alle Arbeitsordner schreibgeschützt einbinden"
        assert "--workspace-ro" in form.widgets["--workspace-ro"].toolTip(), "the option itself in the tooltip"
        assert all(o.long in labels.OPTION_LABELS for o in form.options), "every option has its words"
        danger = form.labels["--dangerously-skip-permissions"]
        assert danger.title.property("role") is None
        form.widgets["--dangerously-skip-permissions"].setChecked(True)
        assert danger.title.property("role") == "notice-warn", "a weakening option set: the warning colour"
        form.widgets["--dangerously-skip-permissions"].setChecked(False)
        assert danger.title.property("role") is None
        form.deleteLater()
    finally:
        i18n.set_language("en")


@needs_sai
def test_clean_up_acts_on_the_chosen_host_and_names_it(window, monkeypatch):
    page = window.sandboxes
    collected = []
    monkeypatch.setattr(window.sai, "collect", lambda host="local", dry_run=True, days=None: collected.append(host) or {
        "removed": [{"name": "old", "agent": "codex", "last_used": "2026-08-01T10:00:00Z", "workspace": "/w"}],
        "leftovers": [], "histories": [], "skipped": [], "damaged": [], "dry_run": dry_run, "days": 30})
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)
    page.host_filter.setCurrentIndex(page.host_filter.findData(None))  # All hosts
    remote = backend.Instance("session", backend.remote_key("ct1", "web"), "web", state="stopped", host="ct1")
    monkeypatch.setattr(page, "current", lambda: remote)
    monkeypatch.setattr(widgets, "background", lambda function, *a, done, **k: done(function(*a), None))
    import sandbox_ai_app.ui as ui_module
    monkeypatch.setattr(ui_module, "background", lambda function, *a, done, **k: done(function(*a), None))
    page.clean_up()
    assert collected == ["ct1"], "All hosts and a row of ct1 selected: ct1's leftovers, not this machine's"
    title, text, _ = widgets.SHOWN[-1]
    assert "ct1" in title and text.startswith("On ct1: ") and "deleted with their HOME" in text
    monkeypatch.setattr(page, "current", lambda: None)
    collected.clear()
    page.clean_up()
    assert collected == [] and "Choose a host above" in widgets.SHOWN[-1][1]
    page.host_filter.setCurrentIndex(page.host_filter.findData("local"))
    page.clean_up()
    assert collected == ["local"] and widgets.SHOWN[-1][1].startswith("On this computer: ")


@needs_sai
def test_a_start_never_takes_the_apps_own_folder_as_its_workspace(window, isolated, monkeypatch):
    launch = window.launch
    started = []
    monkeypatch.setattr(launch, "terminal", lambda argv, title, embedded=None, cwd=None: started.append((argv, cwd)))
    launch.form.widgets["--workspace"].set_value([])
    launch.project = None
    assert launch.start() is False and not started
    assert "Choose the workspace first" in widgets.SHOWN[-1][1]
    project = isolated / "proj"
    project.mkdir()
    launch.project = str(project)
    assert launch.start() is True
    argv, cwd = started[-1]
    assert argv[argv.index("--workspace") + 1] == str(project) and "--workspaces-as-given" in argv
    assert cwd == str(project) and "project folder" in launch.project_note.text()


@needs_sai
def test_a_login_the_cli_would_refuse_keeps_the_dialog_open_and_stores_no_token(qapp, isolated, monkeypatch):
    from PySide6.QtWidgets import QDialog
    from sandbox_ai_app import credentials_page as C
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    sai = backend.SandboxAI()
    box = C.GitLoginsBox(sai, lambda: None)
    dialog = C.LoginDialog(box, "New Git login", [], (("Git server", "url", ""), ("User", "user", "")), host="local")
    dialog.edits["url"].setText("http://git.example.org")
    dialog.value.setText("glpat-" + "x" * 30)
    dialog.check_and_accept()
    assert dialog.result() != QDialog.DialogCode.Accepted and "unencrypted" in dialog.problem.text()
    assert dialog.value.text().startswith("glpat-"), "what was typed stays"
    dialog.edits["url"].setText("git.example.org")
    dialog.check_and_accept()
    assert dialog.result() != QDialog.DialogCode.Accepted and "http(s)://" in dialog.problem.text()
    dialog.edits["url"].setText("https://git.example.org")
    dialog.check_and_accept()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert sai.login_url_problem("publish-logins", {"kind": "gitlab", "url": "https://gitlab.example.org/x"})
    assert sai.login_url_problem("publish-logins", {"kind": "gitlab", "url": ""}) is None
    # A login refused anyway (e.g. by an older sandbox-ai there): a secret made for it is removed again.
    calls = []
    monkeypatch.setattr(sai, "set_secret", lambda name, value, host="local": calls.append(("set", name)) or (0, "", ""))
    monkeypatch.setattr(sai, "delete_secret", lambda name, host="local": calls.append(("delete", name)) or (0, "", ""))
    monkeypatch.setattr(sai, "host_run", lambda host, *args, timeout=90: (1, "", "refused"))
    status, _, err = sai.add_login("ct1", "git-logins", ["add", "--secret", "G", "--", "https://g"], "G", "tok",
                                   existed=False)
    assert status == 1 and calls == [("set", "G"), ("delete", "G")] and "removed again" in err
    calls.clear()
    sai.add_login("ct1", "git-logins", ["add", "--secret", "G", "--", "https://g"], "G", "tok", existed=True)
    assert calls == [("set", "G")], "a secret that was there before stays"
    dialog.deleteLater()
    box.deleteLater()


@needs_sai
def test_host_rows_count_paused_starts_apart_and_files_names_the_apps_folder(window):
    page = window.sandboxes
    starts = [backend.Instance("session", f"s{i}", f"s{i}", state=state)
              for i, state in enumerate(("running", "running", "paused", "stopped"))]
    assert page.host_row("local", starts)["cells"][1] == "Starts: 4, running: 2, paused: 1"
    window.project = None
    assert window.projects()[0][0] == "started-in"
    from sandbox_ai_app import files_page
    assert files_page.root_title("started-in", "", "/x").startswith("Folder the app was started in")


@needs_sai
def test_a_plain_run_here_pauses_and_resumes(window, monkeypatch):
    page = window.sandboxes
    ran = []
    monkeypatch.setattr(window.sai, "call", lambda *args, timeout=60, input=None: ran.append(args) or (0, "", ""))
    import sandbox_ai_app.ui as ui_module
    monkeypatch.setattr(ui_module, "background", lambda function, *a, done, **k: done(function(*a), None))
    run = backend.Instance("run", "run:claude-abc", "claude-abc", run="claude-abc", state="running")
    monkeypatch.setattr(page, "current", lambda: run)
    page.update_buttons()
    assert page.session_buttons["pause"].isEnabled() and not page.session_buttons["resume"].isEnabled()
    page.session("pause")
    assert ran[-1] == ("pause", "claude-abc")
