"""The pages with a host chooser of their own follow the host chosen on the Sandboxes page (the app, offscreen, with the
hosts ct1 and ct2 of `sandbox-ai hosts`; what goes to a host is answered by a stand-in that records who asked).

A page that is hidden takes the host when it is shown (no SSH for a page nobody looks at) and then reads it once; a
host chosen on the page itself holds until the Sandboxes page's host changes again; a late answer of the host before
is never shown; the line beside the chooser says whose host the page shows."""
import json
import os
import sys
import threading
import time

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, widgets  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")

# Every page with a host chooser of its own and what its chooser is (the Start page and the Board choose where a start
# runs, and keep it with the project: they do not follow).
FOLLOWERS = {
    "approvals": lambda page: page.host_filter,
    "terminals": lambda page: page.host,
    "kits": lambda page: page.hosts.box,
    "mcp": lambda page: page.host_choice,
    "llm": lambda page: page.host_box,
    "images": lambda page: page.hosts.box,
    "credentials": lambda page: page.hosts.box,
    "team": lambda page: page.hosts.box,
    "connections": lambda page: page.hosts.box,
    "activity": lambda page: page.hosts.box,
    "traffic": lambda page: page.hosts.box,
    "statistics": lambda page: page.hosts.box,
    "monitor": lambda page: page.hosts.box,
    "files": lambda page: page.host_box,  # (whose sandboxes the left pane offers)
}
# Who looks at the hosts whatever page is shown (the Sandboxes page's list, the questions, the Hosts page's fleet).
WATCHERS = ("SandboxesPage", "ApprovalsPage", "HostsPage", "MainWindow")


TRACE = threading.local()  # who handed the call running in this thread to widgets.background


def callers(depth=2):
    """The classes of the app's code that led to this call (its frames' qualified names), and of what handed it to a
    thread (widgets.background)."""
    found, frame = [], sys._getframe(depth)
    while frame is not None:
        if "sandbox_ai_app" in frame.f_code.co_filename and not frame.f_code.co_filename.endswith("backend.py"):
            found.append(frame.f_code.co_qualname.split(".")[0])
        frame = frame.f_back
    return found + list(getattr(TRACE, "who", []))


def traced_threads(monkeypatch):
    """widgets.background's threads know who started them (TRACE)."""
    plain = widgets.ThreadCall

    class Traced(plain):
        def __init__(self, function, *args, **kw):
            who = callers(1)

            def run(*given):
                TRACE.who = who
                return function(*given)
            super().__init__(run, *args, **kw)
    monkeypatch.setattr(widgets, "ThreadCall", Traced)


def wait_for(qapp, condition, seconds=10.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        qapp.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def settle(qapp, seconds=0.4):
    wait_for(qapp, lambda: False, seconds)


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache")):
        monkeypatch.setenv(name, str(tmp_path / sub))
        (tmp_path / sub).mkdir()
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    from sandbox_ai_app import terminal
    monkeypatch.setattr(terminal, "available", lambda web=None: (False, "not in this test"))
    traced_threads(monkeypatch)
    return tmp_path


@pytest.fixture
def sai(isolated, monkeypatch):
    """ct1 and ct2; host_run and host_command of a host are recorded (host, args, callers) and answered from
    sai.answers (else an empty list); sai.gates[(host, args[:2])]: an Event the answer waits for."""
    sai = backend.SandboxAI()
    if not sai.hosts_supported():
        pytest.skip("this sandbox-ai has no hosts")
    sai.host_add("ct1", "admin@192.0.2.10", 22, "", "")
    sai.host_add("ct2", "admin@192.0.2.11", 22, "", "")
    sai.calls, sai.answers, sai.gates = [], {}, {}
    lock = threading.Lock()
    local_run = sai.host_run

    def host_run(name, *args, timeout=120, input=None):
        if name == "local":
            return local_run(name, *args, timeout=timeout, input=input)
        with lock:
            sai.calls.append((name, args, callers()))
        gate = sai.gates.get((name, args[:2]))
        if gate is not None:
            gate.wait(20)
        return sai.answers.get((name, args[:2]), (0, "[]", ""))
    monkeypatch.setattr(sai, "host_run", host_run)
    local_command = sai.host_command

    def host_command(name, *args):
        if name == "local":
            return local_command(name, *args)
        with lock:
            sai.calls.append((name, args, callers()))
        return ["true"]
    monkeypatch.setattr(sai, "host_command", host_command)
    return sai


def page_calls(sai, host, page_class=None):
    """What went to HOST from the pages (not from those that watch every host), or from PAGE_CLASS's code only."""
    return [(args, who) for name, args, who in list(sai.calls) if name == host and
            (page_class in who if page_class else not any(w in WATCHERS for w in who))]


def make_window(qapp, sai, lazy=False):
    from sandbox_ai_app import ui
    window = ui.MainWindow(sai, check_updates=False, approvals_interval=0, lazy=lazy)
    window.resize(1200, 800)
    window.show()
    qapp.processEvents()
    sandboxes = window.sandboxes
    sandboxes.remote_timer.stop()  # (its own looks at the hosts are not what this is about)
    sandboxes.remote_names = ["ct1", "ct2"]
    sandboxes.fill_host_filter()
    window.approvals.fill_hosts(["ct1", "ct2"])
    window.show_page("sandboxes")
    qapp.processEvents()
    return window


@pytest.fixture
def window(qapp, sai):
    window = make_window(qapp, sai)
    yield window
    close(qapp, window)


def close(qapp, window):
    window.shutdown()
    window.close()
    window.deleteLater()
    qapp.processEvents()


def on_sandboxes(qapp, window, host):
    """The human chooses HOST (None: all hosts) on the Sandboxes page."""
    window.show_page("sandboxes")
    combo = window.sandboxes.host_filter
    assert combo.isVisible() and combo.findData(host) >= 0, [combo.itemData(i) for i in range(combo.count())]
    combo.setCurrentIndex(combo.findData(host))
    qapp.processEvents()


def chosen(window, key):
    return FOLLOWERS[key](getattr(window, key)).currentData()


def note(window, key):
    return window.host_follow.pages[key][2]


def test_every_page_with_a_host_chooser_follows_the_sandboxes_page(qapp, window, sai):
    assert sorted(window.host_follow.pages) == sorted(FOLLOWERS), "every page with a host chooser of its own"
    for key in ("launch", "board"):  # where a start runs: kept with the project, never changed from elsewhere
        assert key not in window.host_follow.pages
    sai.calls.clear()
    on_sandboxes(qapp, window, "ct1")
    settle(qapp)
    assert page_calls(sai, "ct1") == [], "a hidden page reads nothing of the host it is given"
    assert chosen(window, "credentials") == "local", "it takes the host when it is shown"
    readers = ("CredentialsPage", "VpnBox", "IdentitiesBox", "KitsPage", "TeamPage", "LlmPage", "McpPage",
               "ImagesPage", "TerminalsPage", "ActivityPage", "TrafficPage", "ConnectionsPage", "FilesPage")
    # The first time each page is shown, then again (each page was shown before: its own reads on a show and on a host
    # change meet) with ct2 chosen on the Sandboxes page.
    for host in ("ct1", "ct2"):
        if host == "ct2":
            on_sandboxes(qapp, window, "ct2")
            settle(qapp)
            assert page_calls(sai, "ct2") == [], "a hidden page reads nothing of the host it is given"
        for key in FOLLOWERS:
            window.show_page(key)
            qapp.processEvents()
            assert chosen(window, key) == host, key
            label = note(window, key)
            assert label.isVisible() and label.text() == f"{host}, as on the Sandboxes page", (key, label.text())
        # It read that host when shown, and each thing once (not once for the host change and again for the show).
        for page_class in readers:
            assert wait_for(qapp, lambda c=page_class: page_calls(sai, host, c)), (host, page_class)
        settle(qapp)
        for page_class in readers:
            asked = [(args, who[0]) for args, who in page_calls(sai, host, page_class)]  # (args, whose code asked)
            assert len(asked) == len(set(asked)), (host, page_class, asked)
    assert window.credentials.host() == "ct2" and window.statistics.host() == "ct2"


def test_a_host_chosen_on_the_page_holds_until_the_sandboxes_page_changes(qapp, window, sai):
    on_sandboxes(qapp, window, "ct1")
    window.show_page("credentials")
    qapp.processEvents()
    page = window.credentials
    page.hosts.set_host("ct2")  # the human's own choice on the page
    qapp.processEvents()
    assert note(window, "credentials").text() == "ct2, chosen here; the Sandboxes page shows ct1"
    window.show_page("activity")
    window.show_page("credentials")
    qapp.processEvents()
    assert page.host() == "ct2", "its own choice holds while the Sandboxes page's host stays"
    on_sandboxes(qapp, window, "local")
    window.show_page("credentials")
    qapp.processEvents()
    assert page.host() == "local"
    assert note(window, "credentials").text() == "local (this machine), as on the Sandboxes page"
    # All hosts: a page of one host keeps its host (and says nothing); the questions show every host's again.
    window.show_page("approvals")
    on_sandboxes(qapp, window, "ct2")
    window.show_page("approvals")
    assert chosen(window, "approvals") == "ct2"
    on_sandboxes(qapp, window, None)
    window.show_page("approvals")
    qapp.processEvents()
    assert chosen(window, "approvals") is None and not note(window, "approvals").isVisible()
    window.show_page("credentials")
    qapp.processEvents()
    assert page.host() == "local" and not note(window, "credentials").isVisible()


LOGINS = {"ct1": [("agent-of-ct1", "login", None, "/x")], "ct2": [("agent-of-ct2", "login", None, "/y")]}


def started(window):
    return [entry.get("name") for entry in window.terminals.entries]


def rows(table):
    return [table.item(r, 0).text() for r in range(table.rowCount()) if table.item(r, 0) is not None]


def test_a_late_answer_of_the_host_before_is_never_shown(qapp, window, sai, monkeypatch):
    gate = threading.Event()

    def logins(host):
        if host == "ct1":
            gate.wait(20)
        return list(LOGINS.get(host, []))
    monkeypatch.setattr(sai, "host_login_status", logins)
    for host in ("ct1", "ct2"):
        sai.answers[(host, ("llm", "list"))] = (0, json.dumps([{"name": f"lm-of-{host}", "kind": "vllm",
                                                                 "url": "http://203.0.113.5:8000"}]), "")
        sai.answers[(host, ("list", "--json"))] = (0, json.dumps([{"name": f"s-of-{host}", "kind": "session",
                                                                    "id": "s-2", "state": "running",
                                                                    "agent": "claude"}]), "")
        sai.gates[("ct1", ("llm", "list"))] = sai.gates[("ct1", ("list", "--json"))] = gate
    window.terminals.sessions_fold.setChecked(True)
    pages = ("credentials", "llm", "terminals")
    try:
        on_sandboxes(qapp, window, "ct1")
        for key in pages:  # each reads ct1, whose answers wait
            window.show_page(key)
            qapp.processEvents()
            assert chosen(window, key) == "ct1"
        # Changed on the Sandboxes page while those reads are on their way; the pages are shown again.
        on_sandboxes(qapp, window, "ct2")
        for key in pages:
            window.show_page(key)
            qapp.processEvents()
            assert chosen(window, key) == "ct2"
        assert wait_for(qapp, lambda: rows(window.credentials.logins) == ["agent-of-ct2"])
        assert wait_for(qapp, lambda: rows(window.llm.servers) == ["lm-of-ct2"])
    finally:
        gate.set()  # ct1 answers now, late
    # The Sessions list reads one host at a time: ct2's once ct1's (dropped) is back, not at a look minutes later.
    assert wait_for(qapp, lambda: started(window) == ["s-of-ct2"], 3)
    settle(qapp, 0.6)
    assert rows(window.credentials.logins) == ["agent-of-ct2"], "nothing of ct1's under ct2"
    assert rows(window.llm.servers) == ["lm-of-ct2"] and started(window) == ["s-of-ct2"]
    assert note(window, "llm").text() == "ct2, as on the Sandboxes page"

    # The same on a page that is shown when the host changes (it takes it at once, and the rows of the host before go).
    window.show_page("llm")
    qapp.processEvents()
    gate.clear()
    window.host_follow.choose("ct1")
    qapp.processEvents()
    assert chosen(window, "llm") == "ct1" and rows(window.llm.servers) == [], "ct2's rows go at once"
    window.host_follow.choose("ct2")
    gate.set()
    assert wait_for(qapp, lambda: rows(window.llm.servers) == ["lm-of-ct2"])
    settle(qapp, 0.4)
    assert rows(window.llm.servers) == ["lm-of-ct2"]


def test_a_page_made_later_shows_the_sandboxes_pages_host(qapp, sai):
    """The app's lazy start: a page made after the choice takes it when it is first shown, and reads it once."""
    window = make_window(qapp, sai, lazy=True)
    try:
        assert window.built("credentials") is None
        on_sandboxes(qapp, window, "ct2")
        sai.calls.clear()
        window.show_page("credentials")
        qapp.processEvents()
        assert window.credentials.host() == "ct2"
        assert note(window, "credentials").text() == "ct2, as on the Sandboxes page"
        assert wait_for(qapp, lambda: page_calls(sai, "ct2", "CredentialsPage"))
        settle(qapp)
        asked = [(args, who[0]) for args, who in page_calls(sai, "ct2", "CredentialsPage")]
        assert len(asked) == len(set(asked)), asked
    finally:
        close(qapp, window)


def test_a_page_shown_another_way_takes_the_host_before_its_own_show(qapp):
    """Not through the window's navigation (HostFollow's watch of the page while a host waits for it)."""
    from PySide6.QtWidgets import QComboBox, QVBoxLayout, QWidget
    from sandbox_ai_app.host_choice import HostFollow, follow_note

    class Page(QWidget):
        def __init__(self):
            super().__init__()
            self.combo, self.note, self.seen = QComboBox(), follow_note(), []
            for host in ("local", "ct1"):
                self.combo.addItem(host, host)
            layout = QVBoxLayout(self)
            layout.addWidget(self.combo)
            layout.addWidget(self.note)

        def showEvent(self, event):  # noqa: N802
            super().showEvent(event)
            self.seen.append(self.combo.currentData())  # what its show reads

    follow, page = HostFollow(), Page()
    follow.attach("page", page, [page.combo], page.note)
    follow.choose("ct1")
    assert page.combo.currentData() == "local" and "page" in follow.pending
    page.show()
    qapp.processEvents()
    assert page.seen == ["ct1"] and not follow.pending
    assert page.note.isVisible() and page.note.text() == "ct1, as on the Sandboxes page"
    follow.choose("ct2")  # a host it does not list: its own stays, and the line says so
    assert page.combo.currentData() == "ct1"
    assert page.note.text() == "ct1, chosen here; the Sandboxes page shows ct2"
    page.close()
    page.deleteLater()


def test_a_page_opened_on_a_host_keeps_that_host(qapp, window):
    """Secrets and logins on HOST… (the Sandboxes page) and Show questions (the Hosts page) open a page on a host: that
    is newer than the Sandboxes page's host still waiting for the hidden page."""
    from sandbox_ai_app import ui
    on_sandboxes(qapp, window, "ct1")
    ui.MainWindow.show_credentials_of(window, "ct2")
    qapp.processEvents()
    assert window.current_page() == "credentials" and window.credentials.host() == "ct2"
    assert note(window, "credentials").text() == "ct2, chosen here; the Sandboxes page shows ct1"
    window.show_page("sandboxes")
    window.hosts.show_questions.emit("ct2")
    qapp.processEvents()
    assert window.current_page() == "approvals" and chosen(window, "approvals") == "ct2"
