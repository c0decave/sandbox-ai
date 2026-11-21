"""The sandboxes of sandbox-ai's hosts on the Files page (user 2026-11-17: "why are the remote systems not available in
the file manager of the current version, nor the remote processes/dirs?"): the left chooser lists, per host of
`sandbox-ai hosts`, the folders of that host's running sandboxes and kept sessions (read with one call per host in a
thread: backend.host_file_roots, `sandbox-ai list --folders --json` there), opened over the host's SFTP connection
in that folder and counted as an agent's; a host that does not answer is marked and holds nothing up; an older
sandbox-ai there leaves its host's own entry alone. The chooser has a heading per section once there is more than this
machine; the page follows the hosts file (follow_hosts) and the Sandboxes page's host (HostFollow) with its host filter.
Against the fake SFTP server (fake_sftp_server.py); everything changed is below tmp_path."""
import json
import os
import threading
import time

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")

from sandbox_ai_app import backend, files_page, host_choice, widgets  # noqa: E402
from test_files_page import FakeSai, settle, setup  # noqa: E402,F401
from test_files_remote import FAKE  # noqa: E402

HOSTS = {"lab": {"version": "0.1.99"}, "farm": {"version": "0.1.99"}}
ENTRIES = [{"name": "local", "kind": "local"}, {"name": "lab", "kind": "ssh", "ssh": "alice@fake"},
           {"name": "farm", "kind": "ssh", "ssh": "bob@farm"}]


class Hosts:
    """What each host answers to host_file_roots (a list, None for an older sandbox-ai, an exception, or "hang": no
    answer until RELEASE is set), and who was asked (the thread: never the GUI thread)."""

    def __init__(self):
        self.answers, self.asked, self.release = {}, [], threading.Event()

    def __call__(self, name, timeout=20):
        self.asked.append((name, threading.current_thread() is threading.main_thread()))
        answer = self.answers.get(name, [])
        if answer == "hang":
            self.release.wait(30)
            raise ValueError(f"no answer within {timeout:g} s")
        if isinstance(answer, Exception):
            raise answer
        return answer


def texts(pane):
    return [pane.roots.itemText(i) for i in range(pane.roots.count())]


def headings(pane):
    return [pane.roots.itemText(i) for i in range(pane.roots.count()) if pane.roots.itemData(i, files_page.HEADING_ROLE)]


def enabled(pane, index):
    return pane.roots.model().item(index).isEnabled()


@pytest.fixture
def remote(setup, tmp_path, monkeypatch, wait):
    """The page shown, with sandbox-ai's hosts lab (its sandbox claude-abc's workspace and HOME) and farm (no answer
    yet); both are the fake server, whose HOME is tmp_path/remote."""
    page, ws, host, outside = setup
    home = tmp_path / "remote"
    for folder in ("lab/proj", "lab/state/home", "lab/kept"):
        (home / folder).mkdir(parents=True)
    (home / "lab" / "proj" / "notes.txt").write_text("from lab's sandbox")
    monkeypatch.setenv("FAKE_SFTP_HOME", str(home))
    monkeypatch.setattr(files_page, "HOST_FOLDERS_WAIT", 1)
    reader = Hosts()
    home = home.resolve()
    reader.answers = {"lab": [("run", "claude-abc", str(home / "lab" / "proj")),
                              ("run-home", "claude-abc", str(home / "lab" / "state" / "home")),
                              ("session", "kept-one", str(home / "lab" / "kept"))],
                      "farm": "hang"}
    page.sai.host_entries = lambda: ENTRIES
    page.sai.hosts = dict(HOSTS)
    page.sai.host_file_roots = reader
    page.ssh_program = FAKE
    page.hosts_changed()
    page.resize(1100, 700)
    page.show()
    yield page, home, ws, reader
    reader.release.set()
    for root in page.remotes.values():
        root.close()
    page.hide()


def test_each_hosts_sandboxes_are_listed_and_a_host_that_does_not_answer_holds_nothing_up(remote, wait):
    page, home, ws, reader = remote
    left = page.left
    assert wait(lambda: "lab: Running sandbox claude-abc: workspace" in " ".join(texts(left)), 10), texts(left)
    # This machine's sandboxes are there at once, farm's are on their way (not holding up the list).
    assert not page.roots_reading and left.roots.findData(str(ws)) >= 0
    assert "farm: reading its sandboxes…" in texts(left)
    assert not enabled(left, texts(left).index("farm: reading its sandboxes…"))
    assert headings(left) == ["Sandboxes on this machine", "Hosts", "Sandboxes on farm", "Sandboxes on lab"]
    assert texts(left).index("Hosts") < texts(left).index("Host lab (alice@fake)") < \
        texts(left).index("Sandboxes on farm")
    lab = files_page.host_folder("lab", str(home / "lab" / "proj"))
    index = left.roots.findData(lab)
    assert index >= 0 and enabled(left, index)
    assert left.roots.itemText(index) == f"lab: Running sandbox claude-abc: workspace  ({home / 'lab' / 'proj'})"
    assert left.roots.findData(files_page.host_folder("lab", str(home / "lab" / "kept"))) >= 0
    # farm does not answer in time: its section says so (and why), the rest stays as it is.
    assert wait(lambda: any(t.startswith("farm: its sandboxes are not available") for t in texts(left)), 10), texts(left)
    failed = next(t for t in texts(left) if t.startswith("farm: its sandboxes are not available"))
    assert "no answer within" in failed and not enabled(left, texts(left).index(failed))
    assert left.roots.currentData() == str(ws), "what was shown stays"
    assert reader.asked and not any(on_gui for _name, on_gui in reader.asked), "never on the GUI thread"
    assert sorted(name for name, _ in reader.asked) == ["farm", "lab"], "one call per host"
    # A sandbox's folder of a remote host is no path of this machine: never one of this machine's agent folders.
    assert all(not path.startswith(files_page.HOST_FOLDER) for path in page.folder_sources()[0])


def test_a_hosts_sandbox_folder_opens_over_its_connection_in_that_folder_as_an_agents(remote, wait):
    page, home, ws, reader = remote
    left = page.left
    lab = files_page.host_folder("lab", str(home / "lab" / "proj"))
    assert wait(lambda: left.roots.findData(lab) >= 0, 10)
    left.roots.setCurrentIndex(left.roots.findData(lab))
    assert wait(lambda: left.root is not None and left.remote() and left.listing is None, 15), page.message.text()
    settle(left)
    assert left.roots.currentData() == lab and left.path_text() == str(home / "lab" / "proj")
    assert left.root is page.host_root("lab"), "the host's own connection (as Host lab)"
    assert left.root.agents and left.untrusted(), "its files count as an agent's"
    assert [left.table.item(r, 0).text() for r in range(2, left.table.rowCount())] == ["notes.txt"]
    # The host's own entry shares the connection: no second one.
    right = page.right
    right.roots.setCurrentIndex(right.roots.findData("host:lab"))
    assert wait(lambda: right.remote() and right.listing is None, 10)
    assert right.root is left.root and len(page.remotes) == 1
    # Read again (Refresh): the folder chosen stays chosen while lab is read anew.
    left.refresh_roots()
    assert wait(lambda: not page.roots_reading and page.host_folders["lab"][0] == "listed", 10)
    assert left.roots.currentData() == lab
    # Recent keeps it as a folder of lab (Host lab: NAME:/path), not as the chooser's data.
    left.go(str(home / "lab").lstrip("/"))
    settle(left)
    assert ("host:lab", str(home / "lab" / "proj").lstrip("/")) in left.recent


def test_an_older_sandbox_ai_on_a_host_leaves_its_own_entry_and_a_host_gone_leaves_the_list(remote, wait):
    page, home, ws, reader = remote
    reader.answers["lab"] = None  # sandbox-ai there knows no list --folders
    reader.answers["farm"] = []
    page.refresh_roots()
    left = page.left
    assert wait(lambda: page.host_folders.get("farm", ("",))[0] == "listed" and "lab" not in page.host_folders, 10)
    assert wait(lambda: "farm: no sandbox runs or is kept there" in texts(left), 5), texts(left)
    assert "Sandboxes on lab" not in headings(left) and left.roots.findData("host:lab") >= 0
    assert not any(t.startswith("lab: ") for t in texts(left))
    # farm removed from sandbox-ai hosts (the Hosts page says so: follow_hosts): gone from both choosers and the filter.
    del page.sai.hosts["farm"]
    host_choice.hosts_changed()
    for pane in (page.left, page.right):
        assert pane.roots.findData("host:farm") < 0 and not any("farm" in t for t in texts(pane)), texts(pane)
    assert page.host_box.findData("farm") < 0 and page.host_box.findData("lab") >= 0


def test_a_host_added_shows_in_both_panes_at_once(setup, wait):
    """Regression: the Files page did not follow the hosts file - a host added on the Hosts page after the page was
    made never showed in the right pane (and in the left one only after Refresh), so its files looked unavailable."""
    page, ws, host, _ = setup
    page.sai.hosts = {}
    page.sai.host_file_roots = Hosts()
    host_choice.hosts_changed()
    assert page.left.roots.findData("host:lab") < 0 and page.right.roots.findData("host:lab") < 0
    assert page.host_row.isHidden() and not headings(page.left), "one machine: the chooser as before"
    page.sai.hosts = {"lab": {"version": "0.1.99"}}
    page.sai.host_entries = lambda: ENTRIES[:2]
    host_choice.hosts_changed()  # (as the Hosts page and the hosts file's watcher say it)
    for pane in (page.left, page.right):
        assert pane.roots.findData("host:lab") >= 0, texts(pane)
        assert "Hosts" in headings(pane)
    assert not page.host_row.isHidden() and page.host_box.findData("lab") >= 0
    # Hidden, the page asks no host (no SSH for a page nobody looks at): lab's sandboxes once it is shown.
    assert page.sai.host_file_roots.asked == [] and page.hosts_stale
    page.show()
    assert wait(lambda: page.sai.host_file_roots.asked == [("lab", False)] and
                page.host_folders.get("lab", ("",))[0] == "listed", 10)
    page.hide()


def test_the_context_menu_offers_every_place_under_its_heading(remote, wait):
    page, home, ws, reader = remote
    lab = files_page.host_folder("lab", str(home / "lab" / "proj"))
    assert wait(lambda: page.left.roots.findData(lab) >= 0, 10)
    menu = page.menu(page.left, at_row=False)
    where = next(action.menu() for action in menu.actions() if action.text() == "Show in this pane")
    sections = [action.text() for action in where.actions() if action.isSeparator() and action.text()]
    assert sections[:4] == ["Sandboxes on this machine", "Hosts", "Sandboxes on farm", "Sandboxes on lab"]
    entries = {text: on for text, on in widgets.menu_entries(where)}
    assert entries["Host lab (alice@fake)"] and entries["Host farm (bob@farm)"]
    assert entries[f"lab: Running sandbox claude-abc: workspace  ({home / 'lab' / 'proj'})"]
    assert not entries["farm: reading its sandboxes…"]
    show = next(a for a in where.actions() if a.text().startswith("lab: Running sandbox claude-abc: workspace"))
    show.trigger()
    assert wait(lambda: page.left.roots.currentData() == lab and page.left.remote(), 15), page.message.text()


def test_the_host_filter_offers_one_hosts_sandboxes_and_shows_its_first(remote, wait):
    page, home, ws, reader = remote
    left = page.left
    lab = files_page.host_folder("lab", str(home / "lab" / "proj"))
    assert wait(lambda: left.roots.findData(lab) >= 0, 10)
    assert not page.host_row.isHidden()
    assert [page.host_box.itemData(i) for i in range(page.host_box.count())] == [None, "local", "farm", "lab"]
    page.host_box.setCurrentIndex(page.host_box.findData("lab"))
    assert wait(lambda: left.roots.currentData() == lab and left.remote() and left.listing is None, 15), \
        page.message.text()
    assert headings(left) == ["Hosts", "Sandboxes on lab"], "this machine's and farm's sandboxes are not offered"
    # This machine's sandbox folder, not listed now, is still an agent's (the right pane treats it so).
    assert str(ws) in page.folder_sources()[0]
    page.right.go_path(str(ws))
    settle(page.right)
    assert page.right.untrusted()
    # This machine: its first sandbox's folder.
    page.host_box.setCurrentIndex(page.host_box.findData("local"))
    assert wait(lambda: left.roots.currentData() == str(ws) and left.mode() == "sandbox", 10)
    assert headings(left) == ["Sandboxes on this machine", "Hosts"]
    page.host_box.setCurrentIndex(0)  # all hosts: what is shown stays
    assert left.roots.currentData() == str(ws) and "Sandboxes on lab" in headings(left)


def test_the_files_page_follows_the_sandboxes_pages_host(remote, wait):
    """As the other pages (HostFollow): the Sandboxes page's host is the page's host filter - its first sandbox's folder
    shown -, until the human chooses another one here."""
    page, home, ws, reader = remote
    follow = host_choice.HostFollow()
    follow.attach("files", page, [page.host_box], page.follow_note)
    lab = files_page.host_folder("lab", str(home / "lab" / "proj"))
    reader.answers["lab"] = "hang"  # (its folders still on their way when it is followed)
    page.refresh_roots()
    follow.choose("lab")
    assert page.host_filter() == "lab" and page.follow_note.text() == "lab, as on the Sandboxes page"
    assert page.left.roots.currentData() == str(ws), "nothing of lab's known yet: what is shown stays"
    reader.answers["lab"] = [("run", "claude-abc", str(home / "lab" / "proj"))]
    page.refresh_roots()  # (lab answers this time; the read that hangs is dropped)
    assert wait(lambda: page.left.roots.currentData() == lab and page.left.remote(), 15), page.message.text()
    # Chosen here: it holds, and the note says so.
    page.host_box.setCurrentIndex(page.host_box.findData("local"))
    assert page.follow_note.text() == "local (this machine), chosen here; the Sandboxes page shows lab"
    assert wait(lambda: page.left.roots.currentData() == str(ws), 10)
    follow.choose(None)  # all hosts there: the page can show all - it does, and what it shows stays
    assert page.host_filter() is None and page.left.roots.currentData() == str(ws)
    follow.deleteLater()


def test_host_file_roots_reads_list_folders_and_degrades(monkeypatch):
    """The CLI contract (backend.host_file_roots <- sandbox-ai list --folders --json): what it takes, what it drops,
    an older sandbox-ai (its argparse refuses --folders) and a host that does not answer."""
    sai = backend.SandboxAI.__new__(backend.SandboxAI)
    answers = {}

    def host_run(name, *args, timeout=120, input=None):
        answers["asked"] = (name, args, timeout)
        return answers["reply"]
    monkeypatch.setattr(sai, "host_run", host_run, raising=False)
    good = [{"kind": "run", "name": "claude-abc", "id": "r1", "path": "/srv/proj"},
            {"kind": "session-home", "name": "web", "id": "web", "path": "/home/u/.local/state/sandbox-ai/x/home"},
            {"kind": "run", "name": "dup", "id": "r2", "path": "/srv/proj/"},
            {"kind": "mcp", "name": "demo", "id": "m", "path": "/srv/mcp"},
            {"kind": "run", "name": "rel", "id": "r3", "path": "relative/path"},
            {"kind": "run", "name": "nl", "id": "r4", "path": "/srv/a\nb"},
            {"kind": "run", "name": "\x1b[31mred", "id": "r5", "path": "//srv/../srv/x"}, "junk"]
    answers["reply"] = (0, json.dumps(good), "")
    assert sai.host_file_roots("lab") == [("run", "claude-abc", "/srv/proj"),
                                          ("session-home", "web", "/home/u/.local/state/sandbox-ai/x/home"),
                                          ("run", "\\x1b[31mred", "/srv/x")]
    assert answers["asked"] == ("lab", ("list", "--folders", "--json"), backend.HOST_FOLDERS_WAIT)
    answers["reply"] = (2, "", "usage: sandbox-ai list ...\nsandbox-ai: error: unrecognized arguments: --folders\n")
    assert sai.host_file_roots("lab") is None, "an older sandbox-ai: the host's own entry only"
    answers["reply"] = (124, "", "")
    with pytest.raises(ValueError, match="no answer within 5 s"):
        sai.host_file_roots("lab", timeout=5)
    answers["reply"] = (255, "", "ssh: connect to host lab port 22: Connection refused")
    with pytest.raises(ValueError, match="Connection refused"):
        sai.host_file_roots("lab")
    answers["reply"] = (0, json.dumps({"not": "a list"}), "")
    with pytest.raises(ValueError):
        sai.host_file_roots("lab")
    for name in ("local", "-oProxyCommand=x", "a b"):
        with pytest.raises(ValueError):
            sai.host_file_roots(name)
    assert backend.HOST_FOLDER_KINDS == tuple(files_page.ROOT_KINDS)[:4]


@pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME"), reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")
def test_host_file_roots_reads_what_the_real_cli_lists(tmp_path, monkeypatch):
    """End to end through sandbox-ai's own entry point: a kept session's workspace and HOME, listed by `sandbox-ai list
    --folders --json` (run here as the host's sandbox-ai would), read by host_file_roots as the Files page takes it."""
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache")):
        monkeypatch.setenv(name, str(tmp_path / sub))
        (tmp_path / sub).mkdir()
    workspace = tmp_path / "project"
    workspace.mkdir()
    session = tmp_path / "state" / "sandbox-ai" / "sessions" / "kept-one"
    (session / "home").mkdir(parents=True)
    (session / "session.json").write_text(json.dumps({
        "agent": "claude", "options": [], "agent_args": [], "backend": "podman", "workspace": str(workspace),
        "cwd": str(tmp_path), "created": "2026-11-01T10:00:00Z", "keep": True}))
    sai = backend.SandboxAI()
    monkeypatch.setattr(sai, "host_run", lambda name, *args, timeout=120, input=None: sai.call(*args, timeout=timeout))
    started = time.monotonic()
    found = sai.host_file_roots("lab")
    assert ("session", "kept-one", str(workspace.resolve())) in found, found
    assert ("session-home", "kept-one", str((session / "home").resolve())) in found
    assert time.monotonic() - started < backend.HOST_FOLDERS_WAIT
