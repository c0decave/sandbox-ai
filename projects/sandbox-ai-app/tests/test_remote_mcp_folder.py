"""The folder of an MCP server added for a start on another host is that host's (user report 2026-11-19: "Fehler beim
Remote-MCP-Server erstellen: PROJECT-FOLDER ist kein Ordner. <- die App versucht den Ordner
lokal zu erstellen und nicht auf dem Remote-Host").

What happened: the Board on another host, on a folder of that host (Open project folder… / Change folder…), then New
from preset… with an MCP server that needs a folder (binwalk's /work): the preset's board was loaded as the board of
THIS machine's project at that host's path - the window opened it as a project here and said "... is not a folder" -
and the server's folder was offered from it. On a board of this machine's project with another host chosen, the
preset's workspace and the folder offered for the server were this machine's project folder; the remote chooser
started there. Now: a host's folder stays that host's (never opened or looked at here), this machine's project folder
is never a default there, and the server's folder is chosen from that host's folders (New folder made there).

The Board end to end against the fake SFTP server as the host "lab" (test_remote_chooser's fixture); what the Board
asks of the host answered by a stand-in for `sandbox-ai` there (sai.host_run)."""
import json
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")

from sandbox_ai_app import backend, prerequisites, presets, widgets  # noqa: E402
from test_remote_chooser import driven, host, rows, select  # noqa: E402,F401
from test_remote_paths import board, open_button, sai, shown_project, workspaces  # noqa: E402,F401

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")

BINWALK = {"prefix": "hub", "name": "binwalk", "mounts": [{"target": "/work", "required": True}]}


def at_once(function, *args, done, **kw):
    try:
        result = function(*args)
    except Exception as exc:  # noqa: BLE001
        return done(None, exc)
    return done(result, None)


@pytest.fixture
def lab(sai, monkeypatch):
    """The host lab's sandbox-ai as the Board asks it: no MCP server yet (binwalk once added), its catalog has binwalk
    (which needs a folder for /work); `mcp add` recorded and answered as done. The questions asked: lab.asked."""
    hosts = sai.module("hosts")
    hosts.save({"lab": hosts.check("lab", {"ssh": "alice@lab.example"})})  # (its terminal: ssh to it)
    servers = []
    monkeypatch.setattr(sai, "host_mcp_servers", lambda name: [(s, "stopped", [], "", "hub") for s in servers]
                        if name == "lab" else pytest.fail(f"MCP servers read on {name}"))

    def run(name, *args, **kw):
        sai.ran.append((name, *args))
        if name != "lab":
            pytest.fail(f"sandbox-ai {' '.join(args)} run on {name}, not on lab")
        if args[:3] == ("mcp", "catalog", "--json"):
            return 0, json.dumps({"entries": sai.catalog, "problems": []}), ""
        if args[:2] == ("mcp", "add"):
            prefix, _, name = args[2].rpartition(":")
            matches = [e for e in sai.catalog if e["name"] == name and (not prefix or e["prefix"] == prefix)]
            if len(matches) != 1:  # (as sandbox-ai's mcp add refuses it)
                return 1, "", f"sandbox-ai mcp: {name} is in several catalogs" if matches else \
                    f"sandbox-ai mcp: no catalog entry {name}; see sandbox-ai mcp catalog"
            servers.append(name)
            return 0, f"sandbox-ai mcp: {name} added", ""
        return 1, "", "not reached in the tests"
    monkeypatch.setattr(sai, "host_run", run)
    monkeypatch.setattr(prerequisites, "background", at_once)
    asked = []
    real_choose = widgets.choose

    def choose(parent, title, text, choices, *a, **k):
        asked.append((title, text, list(choices)))
        return real_choose(parent, title, text, choices, *a, **k)
    monkeypatch.setattr(widgets, "choose", choose)
    sai.asked, sai.catalog, sai.lab_servers = asked, [BINWALK], servers
    return sai


def binwalk_preset(sai):
    """A preset that uses binwalk (its catalog entry needs a folder for /work), as the user's did."""
    return presets.from_document(sai, {"format": presets.FORMAT, "version": 1, "id": "firmware-check",
                                       "title": "Firmware check", "agent": "claude",
                                       "options": ["--mcp", "binwalk"]})


def added(sai):
    return [args for args in sai.ran if args[1:3] == ("mcp", "add")]


def folder_question(sai):
    return next((text, choices) for title, text, choices in sai.asked if title == "MCP server binwalk")


def test_a_preset_on_a_hosts_folder_stays_that_hosts_and_its_mcp_server_gets_that_folder(board, lab, host,
                                                                                         monkeypatch, wait):
    """The user's report: on lab's folder, New from preset… with binwalk. The board stays the board of lab's folder
    (no project of this machine opened at that path: "... is not a folder"), its workspace is that folder, and binwalk
    is added on lab with it."""
    shown_project(lab, monkeypatch, error="no such file")
    driven(monkeypatch, wait, lambda dialog: select(dialog, "proj/") and dialog.chosen())
    open_button(board).click()
    project = str(host / "proj")
    assert wait(lambda: board.remote_project == ("lab", project), 20)
    monkeypatch.setattr(widgets, "AUTO_CHOICE", "workspace")
    board.new_from_preset(binwalk_preset(lab))
    assert board.opened_seen == [], "lab's folder opened as a project of this machine"
    assert not [text for title, text, _ in widgets.SHOWN if "is not a folder" in text], widgets.SHOWN
    assert board.directory is None and board.remote_project == ("lab", project)
    assert workspaces(board) == [project]
    assert project in board.folder.toolTip() and "lab" in board.folder.text()
    text, choices = folder_question(lab)
    assert ("workspace", f"The start's workspace: {project}") in choices and "folder of lab" in text
    assert added(lab) == [("lab", "mcp", "add", "binwalk", "--mount", f"/work={project}")]


def test_a_board_of_this_machines_project_never_gives_its_folder_to_a_hosts_mcp_server(board, lab, host, tmp_path,
                                                                                         monkeypatch, wait):
    """The Board of a project of this machine with lab chosen: a preset's board gets no workspace of this machine,
    and binwalk's folder is chosen from lab's folders - starting in lab's HOME, never at this machine's path - and
    made there when wanted (New folder…)."""
    local = tmp_path / "here-project"
    local.mkdir()

    def no_file(directory):
        raise FileNotFoundError(directory)
    monkeypatch.setattr(lab, "review_project", no_file)
    board.activate(str(local))
    assert board.host() == "lab" and board.directory == str(local)
    seen = []

    def answer(dialog):
        seen.append(dialog.folder)
        dialog.make_folder("firmware")  # New folder…: made on lab, over SFTP
        assert wait(lambda: dialog.current() is not None and dialog.current().text() == "firmware/", 20), \
            dialog.status.text()
        return dialog.chosen()
    driven(monkeypatch, wait, answer)
    monkeypatch.setattr(widgets, "AUTO_CHOICE", "other")
    board.new_from_preset(binwalk_preset(lab))
    assert str(local) not in workspaces(board), "this machine's project folder as a workspace of lab"
    text, choices = folder_question(lab)
    assert [key for key, _ in choices] == ["other"], "this machine's folder offered for lab"
    assert str(local) not in text
    assert seen == [str(host)], "the chooser starts in lab's HOME"
    assert (host / "firmware").is_dir(), "the new folder is made on lab"
    assert added(lab) == [("lab", "mcp", "add", "binwalk", "--mount", f"/work={host / 'firmware'}")]
    assert set(board.opened_seen) == {str(local)}, "this machine's project stays the tab's project"


def test_change_folder_on_a_host_does_not_start_at_this_machines_project_folder(board, lab, host, tmp_path,
                                                                                 monkeypatch, wait):
    """Change folder… with lab chosen on the board of this machine's project: lab's folders, from lab's HOME (this
    machine's path is no folder of lab); the same path chosen there is taken as lab's folder."""
    local = tmp_path / "here-project"
    local.mkdir()
    monkeypatch.setattr(lab, "review_project", lambda directory: (_ for _ in ()).throw(FileNotFoundError(directory)))
    board.activate(str(local))
    assert board.host_folder("lab") == "", "this machine's project folder is no folder of lab"
    shown_project(lab, monkeypatch, error="no such file")
    seen = []
    driven(monkeypatch, wait, lambda dialog: seen.append(dialog.folder) or select(dialog, "proj/") and dialog.chosen())
    board.change_folder_on_host("lab")
    assert wait(lambda: board.remote_project == ("lab", str(host / "proj")), 20)
    assert seen == [str(host)]


def test_after_a_hosts_preparation_the_start_goes_on_and_the_tab_says_where(board, lab, host, monkeypatch, wait):
    """The second part of the report: the tab Preparing the start said "the start goes on" and ended with exit status
    0. A Start whose preparation on lab added binwalk goes on once the tab ends (its start opens a tab of its own,
    which the tab's last line says); a preparation no start waits for (a preset loaded) says that nothing starts by
    itself."""
    shown_project(lab, monkeypatch, error="no such file")
    driven(monkeypatch, wait, lambda dialog: select(dialog, "proj/") and dialog.chosen())
    open_button(board).click()
    project = str(host / "proj")
    assert wait(lambda: board.remote_project == ("lab", project), 20)
    tabs = []
    board.prerequisite_terminal = lambda: (lambda argv, title, ended: tabs.append((argv, title, ended)) or True)
    monkeypatch.setattr(widgets, "AUTO_CHOICE", "workspace")
    board.new_from_preset(binwalk_preset(lab))  # (preparing for a preset: no start waits)
    (argv, title, ended), = tabs
    assert title == "Preparing the start"
    end = next(word for word in argv if word.startswith("--end="))
    assert "the start goes on" not in end and "Start starts it" in end, end
    steps = json.loads(argv[-1])
    assert steps[0][1][:2] == ["ssh", "-tt"] and "LogLevel=ERROR" in steps[0][1], \
        "no 'Connection to HOST closed.' in the tab"
    ended(0)
    # A Start: binwalk is missing again on lab (removed there meanwhile), made, and the start goes on at the tab's end.
    started = []
    lab.host_mcp_servers = lambda name: []
    board.prerequisites_done = None
    board.check_prerequisites(then=lambda: started.append(True))
    (argv, title, ended) = tabs[-1]
    end = next(word for word in argv if word.startswith("--end="))
    assert "the start goes on in a tab of its own" in end, end
    lab.host_mcp_servers = lambda name: [("binwalk", "stopped", [], "", "hub")]
    ended(0)
    assert started == [True], "the start goes on once the tab says it is prepared"


# -- a start whose MCP servers its host has not: added there first, then it goes on (user report 2026-11-19: "Unknown
# MCP server: playwright, radare2; defined: binwalk, demo ..." - and that was all a Start did) --

PLAYWRIGHT = {"prefix": "mcp", "name": "playwright", "network": "full"}  # (needs the internet: its entry says so)
RADARE2 = [{"prefix": "mcp", "name": "radare2", "mounts": [{"target": "/work", "required": True}]},
           {"prefix": "hub", "name": "radare2", "mounts": [{"target": "/work", "required": True}]}]


def launcher_of(board, lab):
    """The launcher's check on lab, as its sandbox-ai answers it: refused while a server the board uses is not
    defined there ("Unknown MCP server: ..."), else accepted. The checks: launcher_of.count."""
    checks = []

    def check():
        checks.append(True)
        wanted = board.board_needs().get("--mcp", [])
        missing = [n for n in wanted if n not in lab.lab_servers]
        board.checking_state = board.board.state()
        board.check_token += 1
        result = ({"agent": "claude"}, "") if not missing else \
            (None, f"agent-sandbox: Unknown MCP server: {', '.join(missing)}; defined: "
                   f"{', '.join(['demo', *lab.lab_servers])}; add a server with sandbox-ai mcp add (sandbox-ai mcp "
                   "catalog lists the catalogs' servers), or start without it")
        board.checked(board.check_token, result, None, "lab")
    board.check = check
    return checks


@pytest.fixture
def on_lab(board, lab, host, monkeypatch, wait):
    """The Board on lab's folder proj, a board of binwalk (hub:binwalk, as a preset names it), playwright and
    radare2; the launcher's check has refused it; what starts is recorded (board.started)."""
    from sandbox_ai_app import board as B
    shown_project(lab, monkeypatch, error="no such file")
    driven(monkeypatch, wait, lambda dialog: select(dialog, "proj/") and dialog.chosen())
    open_button(board).click()
    project = str(host / "proj")
    assert wait(lambda: board.remote_project == ("lab", project), 20)

    def make(names, extra_catalog=()):
        lab.catalog = [BINWALK, PLAYWRIGHT, *RADARE2, *extra_catalog]
        options = [word for name in names for word in ("--mcp", name)] + ["--workspace", project]
        shown = B.from_argv(lab, "claude", options, [])
        for node in shown.of_kind("mcp"):
            if node.props.get("name") == "binwalk":
                node.props["entry"] = "hub:binwalk"
        board.set_board(shown, None)
        board.remote_project = ("lab", project)
        board.started = []
        board.terminal = lambda argv, title, **kw: board.started.append((argv, title))
        board.checks = launcher_of(board, lab)
        board.check()
        assert board.errors(), "the launcher's check refused the board"
        return board
    make.project = project
    return make


def answer(lab, monkeypatch, catalog="hub"):
    """The questions answered: Add (yes), each folder the start's workspace (lab's proj), radare2 from CATALOG."""
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    real = widgets.choose

    def choose(parent, title, text, choices, *a, **k):
        lab.asked.append((title, text, list(choices)))
        keys = [key for key, _ in choices]
        if "workspace" in keys:
            return "workspace"
        if catalog in keys:
            return catalog
        return real(parent, title, text, choices, *a, **k)
    monkeypatch.setattr(widgets, "choose", choose)


def test_a_start_adds_the_mcp_servers_its_host_lacks_and_goes_on(on_lab, lab, monkeypatch):
    """Start with binwalk (a folder for /work), playwright (the internet) and radare2 (in two catalogs) not defined
    on lab: each added there from its catalog - binwalk as the preset names it, radare2 from the catalog chosen,
    their folders lab's proj - then the launcher checks again and the start runs on lab."""
    board = on_lab(["binwalk", "playwright", "radare2"])
    answer(lab, monkeypatch)
    assert board.start() is False, "not at once: the servers are added first"
    project = on_lab.project
    assert added(lab) == [("lab", "mcp", "add", "hub:binwalk", "--mount", f"/work={project}"),
                          ("lab", "mcp", "add", "playwright"),
                          ("lab", "mcp", "add", "hub:radare2", "--mount", f"/work={project}")], lab.ran
    question = next(text for title, text, _ in lab.asked if title == "MCP server radare2" and "several" in text)
    assert "mcp:radare2, hub:radare2" in question
    assert len(board.started) == 1, (board.started, widgets.SHOWN)
    argv, title = board.started[0]
    assert title == "lab: claude" and argv[:2] == ["ssh", "-tt"]
    assert not [text for title, text, _ in widgets.SHOWN if title == "Not started"], widgets.SHOWN


def test_a_server_no_catalog_has_stops_the_start_before_it_runs_and_says_why(on_lab, lab, monkeypatch):
    """A server no catalog of lab has (a name typed on the board): not tried, the start does not run - said before
    anything starts, with the MCP servers page offered; the others are added all the same."""
    board = on_lab(["playwright", "nosuch"])
    answer(lab, monkeypatch)
    board.start()
    assert board.started == [], "nothing runs on lab: the launcher would only refuse it"
    assert added(lab) == [("lab", "mcp", "add", "playwright")], "nosuch is not tried"
    said = "\n".join(text for title, text, _ in widgets.SHOWN if title == "Not started")
    assert "MCP server nosuch: no catalog of lab has it" in said and "Unknown MCP server" not in said, widgets.SHOWN
    panel = board.preparing_panel
    assert any("nosuch" in label.text() for label in panel.labels)
    assert "Open the MCP servers page" in [button.text() for button in panel.buttons]


def test_a_start_whose_servers_were_added_elsewhere_meanwhile_is_checked_again_and_runs(on_lab, lab, monkeypatch):
    """The launcher's refusal is from before: playwright was added on lab meanwhile (a terminal, the MCP page).
    Start reads lab, finds it there, has the launcher check again and starts - nothing asked, nothing added."""
    board = on_lab(["playwright"])
    lab.lab_servers.append("playwright")
    answer(lab, monkeypatch)
    board.start()
    assert added(lab) == [] and len(board.started) == 1, (lab.ran, widgets.SHOWN)


def test_a_host_that_does_not_say_its_servers_is_not_asked_without_end(on_lab, lab, monkeypatch):
    """lab does not answer what it has (`mcp list` fails): the start reads once, the launcher checks once more, and
    the start is refused with the launcher's words - never read and checked again and again."""
    board = on_lab(["playwright"])
    reads = []

    def unread(name):
        reads.append(name)
        raise ValueError("lab did not answer")
    monkeypatch.setattr(lab, "host_mcp_servers", unread)
    answer(lab, monkeypatch)
    notes, real_notice = [], board.show_notice
    monkeypatch.setattr(board, "show_notice", lambda text, role="secondary": notes.append(text) or
                        real_notice(text, role))
    board.start()
    assert board.started == [] and len(reads) <= 2 and len(board.checks) <= 3, (reads, board.checks)
    said = [text for title, text, _ in widgets.SHOWN if title == "Not started"]
    assert said and "lab did not say which MCP servers it has (lab did not answer), so playwright could not be " \
        "added there" in said[-1], widgets.SHOWN
    panel = board.preparing_panel
    assert [button.text() for button in panel.buttons] == ["Start again", "Open the MCP servers page"]
    assert "lab did not say what it has of MCP server playwright (lab did not answer): the launcher checks it at " \
        "the start." in notes, notes


# -- the MCP servers page's Add… on another host: its folders are that host's, looked at there (user report
# 2026-11-19: "sandbox-ai mcp: --mount /work=/work:ro: the folder /work is not there on this machine; ...") --

RADARE2_ENTRY = {"name": "radare2", "mounts": [{"target": "/work", "required": True,
                                                "description": "The folder with the binaries"}],
                 "_signature": {"state": "good"}}


@pytest.fixture
def mcp_page(qapp, sai, host, monkeypatch):
    """The MCP servers page with lab chosen; Add… answered by FILL(dialog) (the dialog's fields set, then OK)."""
    from sandbox_ai_app import ui
    page = ui.McpPage(sai)
    assert page.host_choice.choose("lab")
    page.catalog_of = None

    def add(fill):
        def run(dialog):
            fill(dialog)
            dialog.accept()
            return dialog.result()
        monkeypatch.setattr(widgets, "exec_dropped", run)
        page.add_entry("hub:radare2", dict(RADARE2_ENTRY))
    page.add_with = add
    yield page
    page.close()
    page.deleteLater()


def mcp_adds(sai):
    return [args for args in sai.ran if args[1:3] == ("mcp", "add")]


def test_add_on_a_host_asks_for_a_folder_there_and_never_fills_one_in(mcp_page, sai, host):
    """A folder the entry requires: the field empty (never this machine's folder), OK waits for it, the reason says
    it is a folder of lab, and … chooses one there."""
    from sandbox_ai_app import ui
    dialog = ui.AddServerDialog(mcp_page, dict(RADARE2_ENTRY), "lab")
    ok = dialog.buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok)
    field, _writable = dialog.mounts["/work"]
    assert field.value() == "" and field.host == "lab" and not ok.isEnabled()
    assert "/work needs a folder of lab" in dialog.mount_reasons["/work"].text()
    field.edit.setText("work")
    assert not ok.isEnabled() and "absolute path" in dialog.mount_reasons["/work"].text()
    field.edit.setText("/srv/bins")
    assert ok.isEnabled(), "a folder of lab is looked at there when it is added"
    dialog.deleteLater()


def test_add_on_a_host_makes_a_missing_read_only_folder_there_after_a_yes(mcp_page, sai, host, wait):
    """The user's case: a read-only folder that is not there on lab - looked at on lab (not here), offered to be made
    there (asked first), made over SFTP, then `mcp add` runs on lab with it."""
    bins = host / "samples" / "bins"
    mcp_page.add_with(lambda dialog: dialog.mounts["/work"][0].edit.setText(str(bins)))
    assert wait(lambda: mcp_adds(sai), 20), (sai.ran, widgets.SHOWN)
    assert bins.is_dir() and bins.stat().st_mode & 0o777 == 0o700, "made on lab, only for you"
    question = next(text for title, text, _ in widgets.SHOWN if title == "Add hub:radare2 on lab")
    assert "Not there on lab" in question and str(bins) in question
    assert mcp_adds(sai) == [("lab", "mcp", "add", "--as=radare2", f"--mount=/work={bins}:ro", "--network=none",
                              "hub:radare2")], mcp_adds(sai)


def test_add_on_a_host_declined_or_not_a_folder_adds_nothing(mcp_page, sai, host, wait, monkeypatch):
    """No to making the folder: nothing added, said; a file of lab (or a link) as the folder: said, nothing added."""
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)
    mcp_page.add_with(lambda dialog: dialog.mounts["/work"][0].edit.setText(str(host / "nothere")))
    assert wait(lambda: "its folders are not there on lab" in mcp_page.catalog_status.text(), 20), \
        mcp_page.catalog_status.text()
    assert not (host / "nothere").exists() and mcp_adds(sai) == []
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    for path in (host / "notes.txt", host / "escape"):
        mcp_page.add_with(lambda dialog, p=path: dialog.mounts["/work"][0].edit.setText(str(p)))
        assert wait(lambda: "was not added" in mcp_page.catalog_status.text() and str(path) in
                    mcp_page.catalog_status.text(), 20), mcp_page.catalog_status.text()
    assert mcp_adds(sai) == []
    said = "\n".join(text for _, text, _ in widgets.SHOWN)
    assert "notes.txt is not a folder" in said and "escape is a symbolic link (not followed)" in said



# -- the in-depth review (docs/reviews/remote-mcp-board.md): what the Board knows of a host's MCP servers is read
# again - at every Start, after a start's preparation, when it is older than half a minute - and it is that host's --

def test_every_start_reads_its_host_again(on_lab, lab, monkeypatch):
    """Lead 1: once a start had found its servers on lab, the check remembered that for good - playwright removed on
    lab later (the MCP page, a terminal, the host made anew), every Start was refused by the launcher ("Unknown MCP
    server: playwright"). Every Start reads lab again: removed, it is added again, and the start runs."""
    board = on_lab(["playwright"])
    answer(lab, monkeypatch)
    board.start()
    assert len(board.started) == 1 and added(lab) == [("lab", "mcp", "add", "playwright")]
    lab.lab_servers.remove("playwright")  # removed on lab
    board.check()
    assert board.errors()
    board.start()
    assert added(lab) == [("lab", "mcp", "add", "playwright")] * 2, lab.ran
    assert len(board.started) == 2, widgets.SHOWN


def test_the_boards_list_of_a_hosts_servers_and_catalog_is_that_hosts_and_read_again(on_lab, lab, monkeypatch,
                                                                                       wait):
    """Lead 3: the MCP part's choices on lab were read once per host and never again (a server added there by a
    start's preparation or the MCP page not offered while the app ran), and its catalog entries were this machine's.
    Now: read again after a start made something there and when older than MCP_KEEP; the catalog is lab's."""
    from sandbox_ai_app import board_view
    monkeypatch.setattr(lab, "mcp_catalog", lambda: pytest.fail("this machine's catalog for a board on lab"))
    board = on_lab(["playwright"])
    assert wait(lambda: board.mcp_names() == [] and board.mcp_known.get("lab") == [], 20), board.mcp_known
    answer(lab, monkeypatch)
    board.start()  # (adds playwright on lab)
    assert "lab" not in board.mcp_known, "read again after the start's preparation"
    assert wait(lambda: board.mcp_names() == ["playwright"], 20), board.mcp_known
    lab.lab_servers.append("other")  # added on lab elsewhere (the MCP page)
    assert board.mcp_names() == ["playwright"], "a list read just now is offered"
    monkeypatch.setattr(board_view, "MCP_KEEP", 0)
    board.mcp_names()
    assert wait(lambda: board.mcp_names() == ["other", "playwright"], 20), board.mcp_known
    node = board.board.first("mcp")
    combo = board.mcp_chooser(node)
    assert wait(lambda: getattr(board, "mcp_catalog_host", None) == "lab", 20)
    combo = board.mcp_chooser(node)
    texts = [combo.itemText(i) for i in range(combo.count())]
    assert "radare2 (catalog: added at the start, asked first)" in texts, texts
    assert ("lab", "mcp", "catalog", "--json") in lab.ran


def test_a_part_named_with_its_catalog_is_said_on_the_board_not_added_without_end(on_lab, lab, monkeypatch):
    """Lead 4: an MCP part named hub:radare2 (typed, or a preset's option): the launcher takes a server's name, so it
    was refused as unknown - and the start's preparation added radare2, still missed "hub:radare2" and added it again.
    The board says what to write instead; nothing is added for it."""
    board = on_lab(["playwright"])
    board.board.first("mcp").props["name"] = "hub:radare2"
    answer(lab, monkeypatch)
    assert any("give the name alone" in p.text for p in board.errors())
    board.start()
    assert added(lab) == [] and board.started == []
    said = "\n".join(text for title, text, _ in widgets.SHOWN if title == "Not started")
    assert "--mcp takes the name of a server (\u201cradare2\u201d)" in said, widgets.SHOWN
