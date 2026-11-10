"""The Terminals page's list of running starts (offscreen): every start of `sandbox-ai list --json` that runs, however
it was started; one click attaches a start in tmux (one tab per start), the others say why there is nothing to
attach (list --json's attach_why, guessed for an older sandbox-ai). Here sandbox-ai is a stand-in that records what
it was asked."""
import json
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, i18n, terminal, widgets  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")

STARTS = [
    {"name": "web", "kind": "session", "agent": "claude", "state": "running", "attachable": True, "attach_why": None,
     "workspace": "/srv/app"},
    {"name": "task", "kind": "run", "agent": "codex", "state": "running", "attachable": False,
     "attach_why": "foreground", "workspace": "/srv/task"},
    {"name": "held", "kind": "session", "agent": "claude", "state": "paused", "attachable": False,
     "attach_why": "paused", "workspace": "/srv/held"},
    {"name": "mcp-demo", "kind": "mcp", "agent": None, "state": "running", "attachable": False, "attach_why": "mcp"},
    {"name": "old-cli", "kind": "run", "agent": "claude", "state": "running", "attachable": False},  # no attach_why
    {"name": "kept", "kind": "session", "agent": "claude", "state": "stopped", "attachable": False,
     "attach_why": "stopped"},
]


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)

    def at_once(function, *args, done, **kw):  # background(), on this thread
        try:
            result = function(*args)
        except Exception as exc:  # noqa: BLE001 - as background() hands it on
            return done(None, exc)
        return done(result, None)
    monkeypatch.setattr(terminal, "background", at_once)
    return tmp_path


@pytest.fixture
def sai(env, monkeypatch):
    sai = backend.SandboxAI()
    sai.asked = []

    def host_run(name, *args, timeout=120):
        sai.asked.append((name, args))
        return 0, json.dumps(STARTS if args == ("list", "--json") else {}), ""
    monkeypatch.setattr(sai, "host_run", host_run)
    return sai


def cells(page):
    return {page.list.item(r, 0).text(): (page.list.item(r, 2).text(), page.list.item(r, 4).text(),
                                          button(page, r).text() if button(page, r) else None)
            for r in range(page.list.rowCount())}


def button(page, row):
    cell = page.list.cellWidget(row, 4)
    return cell.findChild(QtWidgets.QPushButton) if cell is not None else None


def row_of(page, name):
    return next(r for r in range(page.list.rowCount()) if page.list.item(r, 0).text() == name)


def test_every_running_start_is_listed_and_one_click_attaches(qapp, sai):
    opened = []
    page = terminal.TerminalsPage(sai=sai, in_terminal=lambda argv, title, embedded=None: opened.append(
        (argv, title, embedded)))
    assert sai.asked == [], "a page nobody opened asks nothing"
    page.show()
    assert sai.asked == [("local", ("list", "--json"))], "shown: the list is read"
    found = cells(page)
    assert set(found) == {"web", "task", "held", "mcp-demo", "old-cli"}, "what runs or is paused; not what stopped"
    assert found["web"] == ("running", "", "Open terminal")
    assert "--no-tmux" in found["task"][1] and found["task"][2] is None
    assert found["held"][1] == "Paused: resume it to attach." and found["held"][0] == "paused"
    assert "MCP server" in found["mcp-demo"][1]
    assert found["old-cli"][1] == found["task"][1], "an older sandbox-ai without attach_why: guessed from the kind"
    assert "5 running on local" in page.summary.text()
    button(page, row_of(page, "web")).click()
    assert opened == [(sai.command("attach", "web"), "web", True)]
    page.attach_row(row_of(page, "task"))  # a double click on one that cannot be attached: nothing
    assert len(opened) == 1
    # The timer reads again while the page is shown, not while it is hidden.
    page.tick()
    assert len(sai.asked) == 2
    page.hide()
    page.tick()
    assert len(sai.asked) == 2
    page.deleteLater()


def test_a_start_with_a_tab_shows_that_tab(qapp, sai, monkeypatch):
    opened = []
    page = terminal.TerminalsPage(sai=sai, in_terminal=lambda argv, title, embedded=None: opened.append(argv))
    tab = QtWidgets.QWidget()
    tab.argv, tab.view = sai.command("session", "attach", "web"), QtWidgets.QWidget(tab)  # from Sandboxes & sessions
    page.tabs.addTab(tab, "session web")
    page.tabs.addTab(QtWidgets.QWidget(), "build")
    monkeypatch.setattr(page, "running", lambda: [tab])
    page.show()
    assert cells(page)["web"][2] == "Show its tab"
    page.tabs.setCurrentIndex(1)
    button(page, row_of(page, "web")).click()
    assert opened == [] and page.tabs.currentWidget() is tab, "no second tab for the same start"
    page.deleteLater()


def test_attach_key_names_the_start_of_an_attach_command(qapp, sai):
    key = terminal.attach_key
    assert key(sai.command("attach", "web")) == key(sai.command("session", "attach", "web")) == ("here", "web")
    assert key(sai.command("attach", "other")) != key(sai.command("attach", "web"))
    assert key(["sandbox-ai", "attach", "web"]) == ("here", "web")
    assert key(sai.command("build", "all")) is None and key(sai.launch_argv("claude", [])) is None
    ssh = ["ssh", "-tt", "admin@192.0.2.10", "if command -v sandbox-ai >/dev/null 2>&1; then exec sandbox-ai attach "
           "web; else exec \"$HOME/.local/bin/sandbox-ai\" attach web; fi"]
    assert key(ssh) == ("ssh", tuple(ssh)) and key(ssh[:-1] + ["exec sandbox-ai list --json"]) is None


def test_the_starts_of_another_host(qapp, sai):
    if not sai.hosts_supported():
        pytest.skip("this sandbox-ai has no hosts.py")
    opened = []
    page = terminal.TerminalsPage(sai=sai, in_terminal=lambda argv, title, embedded=None: opened.append((argv, title)))
    assert page.host.count() == 1 and page.host.isHidden(), "no other host: no choice"
    sai.host_add("ct1", "admin@192.0.2.10", 2222, "", "Proxmox 101")  # meanwhile, on the Hosts page
    page.show()
    assert [page.host.itemData(i) for i in range(page.host.count())] == ["local", "ct1"] and not page.host.isHidden()
    page.host.setCurrentIndex(1)
    assert sai.asked[-1] == ("ct1", ("list", "--json"))
    button(page, row_of(page, "web")).click()
    argv, title = opened[-1]
    assert argv[0] == "ssh" and argv[-1].endswith("attach web; fi") and title == "ct1: web"
    # Another host is asked every fifth tick only (SSH).
    count = len(sai.asked)
    for _ in range(5):
        page.tick()
    assert len(sai.asked) == count + 1
    page.deleteLater()


def test_a_list_that_cannot_be_read_says_why(qapp, sai, monkeypatch):
    monkeypatch.setattr(sai, "host_run", lambda name, *args, timeout=120: (1, "", "sandbox-ai: no such command"))
    page = terminal.TerminalsPage(sai=sai, in_terminal=lambda *a, **k: None)
    page.show()
    assert "could not be read" in page.summary.text() and "no such command" in page.summary.text()
    assert page.list.rowCount() == 0
    page.deleteLater()


def test_the_reasons_in_german(qapp, sai):
    i18n.set_language("de")
    try:
        assert terminal.attach_why(STARTS[1]).startswith("Läuft im Vordergrund")
        assert terminal.attach_why(STARTS[2]).startswith("Pausiert")
        assert terminal.attach_why(STARTS[0]) == ""
    finally:
        i18n.set_language("en")


def test_sessions_lie_below_the_tabs_folded_until_opened(qapp, sai, monkeypatch):
    """Sessions (the running starts) below the terminal tabs, folded to one line at first: its count still shows;
    opened, it takes about 30% of the height. The line above the tabs says nothing while nothing is wrong."""
    monkeypatch.setattr(terminal, "available", lambda web=None: (True, ""))
    page = terminal.TerminalsPage(sai=sai, in_terminal=lambda *a, **k: None)
    page.resize(900, 1000)
    page.show()
    assert page.hint.text() == "" and page.hint.isHidden(), "no explanation that stays on the page"
    assert page.split.indexOf(page.tabs) == 0 and page.split.indexOf(page.starts) == 1, "the terminals first"
    assert page.sessions_fold.text() == "Sessions" and page.list.accessibleName() == "Sessions"
    assert page.sessions_fold.property("disclosure") and not page.sessions_fold.isChecked()
    assert page.list.isHidden() and "5 running on local" in page.summary.text(), "folded: its count is still there"
    tabs, sessions = page.split.sizes()
    assert sessions < 100 < tabs
    page.sessions_fold.setChecked(True)
    assert not page.list.isHidden()
    tabs, sessions = page.split.sizes()
    assert 0.2 < sessions / (tabs + sessions) < 0.4, (tabs, sessions)
    page.sessions_fold.setChecked(False)
    assert page.list.isHidden() and page.split.sizes()[1] < 100
    page.reveal("starts")  # a tour points at it: it opens
    assert page.sessions_fold.isChecked() and not page.list.isHidden()
    page.deleteLater()


def test_sessions_in_german(qapp, sai):
    i18n.set_language("de")
    try:
        page = terminal.TerminalsPage(sai=sai, in_terminal=lambda *a, **k: None)
        assert page.sessions_fold.text() == "Sitzungen" and page.list.accessibleName() == "Sitzungen"
        page.deleteLater()
    finally:
        i18n.set_language("en")
