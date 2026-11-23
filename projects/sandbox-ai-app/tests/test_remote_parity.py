"""Remote parity (user, 2026-11-16: "the functions that work here must work remote too: mounts, secrets, git ..."):
what the app does for a start of this machine it does for a start on a host of `sandbox-ai hosts` - lists read from
that host, actions sent there (SandboxAI.host_run, here a stand-in that records what it was asked and answers from a
table), the host named in every title and question. One test per place of /root/briefs/remote-parity-audit.md."""
import json
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, widgets  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")
MODULES = ("credentials_page", "activity_page", "safeguards_view", "approvals_sections", "pages", "traffic_page",
           "board_view", "ui", "reports")
SESSION = {"name": "wild-dromouse", "id": "s-1", "kind": "session", "state": "running", "agent": "claude",
           "workspace": "/srv/work/project"}


def synchronous(function, *args, done=None, **kw):
    try:
        result = function(*args)
    except Exception as exc:  # noqa: BLE001 - as ThreadCall hands it on
        if done is not None:
            done(None, exc)
        return None
    if done is not None:
        done(result, None)
    return None


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache"), ("HOME", "home")):
        monkeypatch.setenv(name, str(tmp_path / sub))
        (tmp_path / sub).mkdir()
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    import importlib
    import pkgutil
    import sandbox_ai_app
    # Every module of the app first: one imported later, while widgets.background is replaced, would keep the
    # stand-in for good (`from .widgets import background`) and run its reads on the GUI thread in later tests.
    for found in pkgutil.iter_modules(sandbox_ai_app.__path__):
        if found.name not in ("__main__", "main"):
            importlib.import_module(f"sandbox_ai_app.{found.name}")
    for name in MODULES:
        module = importlib.import_module(f"sandbox_ai_app.{name}")
        if hasattr(module, "background"):
            monkeypatch.setattr(module, "background", synchronous)
    monkeypatch.setattr(widgets, "background", synchronous)
    return tmp_path


@pytest.fixture
def sai(env, monkeypatch):
    """This machine and the host ct1; host_run records (host, args[, input]) and answers from sai.answers."""
    sai = backend.SandboxAI()
    sai.asked, sai.answers = [], {}
    monkeypatch.setattr(sai, "hosts_supported", lambda: True)
    monkeypatch.setattr(sai, "host_entries", lambda: [{"name": "local", "kind": "local"},
                                                      {"name": "ct1", "kind": "ssh", "ssh": "admin@192.0.2.10"}])

    def host_run(name, *args, timeout=120, input=None):
        sai.asked.append((name, args) if input is None else (name, args, input))
        return sai.answers.get((name, args), (0, "", ""))
    monkeypatch.setattr(sai, "host_run", host_run)
    monkeypatch.setattr(sai, "host_terminal", lambda name, *args: ["ssh", "-tt", name, "sandbox-ai", *args])
    return sai


MADE = []  # the pages a test made: deleted after it (none of their timers or signals may act in a later test)


@pytest.fixture(autouse=True)
def cleaned(qapp):
    yield
    from PySide6.QtCore import QCoreApplication, QEvent
    while MADE:
        widget = MADE.pop()
        try:
            widget.deleteLater()
        except RuntimeError:  # deleted already
            pass
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)


def answer(sai, args, data, host="ct1", status=0, err=""):
    sai.answers[(host, tuple(args))] = (status, data if isinstance(data, str) else json.dumps(data), err)


def asked(sai, host="ct1"):
    return [entry[1] for entry in sai.asked if entry[0] == host]


def choose(chooser, host="ct1"):
    chooser.box.setCurrentIndex(chooser.box.findData(host))


# -- 1-3: Credentials › Give to a running sandbox, on the host chosen --

def credentials(sai, monkeypatch, answer_dialog):
    from sandbox_ai_app import credentials_page as C
    answer(sai, ["list", "--json"], [SESSION, {**SESSION, "name": "old-one", "id": "s-2", "state": "stopped"},
                                     {"name": "mcp-github", "kind": "mcp", "state": "running"}])
    answer(sai, ["secrets", "list"], "NAME STORE\nGH_PUSH file\n")
    answer(sai, ["git-logins", "list", "--json"], [{"url": "https://github.com", "user": "x-access-token",
                                                    "secret": "GH_PUSH"}])
    offered, titles = [], []

    def run_dialog(dialog):
        titles.append(dialog.windowTitle())
        offered.extend((dialog.choice.itemText(i), dialog.choice.itemData(i)) for i in range(dialog.choice.count()))
        answer_dialog(dialog)
        assert dialog.problem() is None, dialog.problem()
        return QtWidgets.QDialog.DialogCode.Accepted
    monkeypatch.setattr(C.GiveDialog, "exec", run_dialog)
    page = C.CredentialsPage(sai, lambda *a, **k: None)
    MADE.append(page)
    choose(page.hosts)
    return page, offered, titles


def test_a_secret_is_given_to_a_running_start_of_the_host_chosen(qapp, sai, monkeypatch):
    def as_login(dialog):
        dialog.host.setText("github.com")
    page, offered, titles = credentials(sai, monkeypatch, as_login)
    page.secrets.selectRow(0)
    page.give_secret()
    assert offered == [(offered[0][0], "wild-dromouse")], "the running agent starts of ct1, never an MCP server's"
    assert titles == ["Give to a running sandbox on ct1"]
    assert ("git-logins", "give", "--secret", "GH_PUSH", "--user", "x-access-token", "--", "wild-dromouse",
            "https://github.com") in asked(sai)
    assert not [entry for entry in sai.asked if entry[0] == "local"], "nothing sent to this machine"
    assert widgets.SHOWN[-1][:2][1].startswith("On ct1: ") if widgets.SHOWN else True
    text = page.message.text()
    assert text.startswith("On ct1: ") and "wild-dromouse" in text and "https://github.com" in text, text


def test_a_secret_is_given_as_an_api_key_on_the_host_chosen(qapp, sai, monkeypatch):
    def as_key(dialog):
        dialog.host.setText("api.example.com")
        dialog.variable.setText("EX_KEY")
    page, _offered, _titles = credentials(sai, monkeypatch, as_key)
    page.secrets.selectRow(0)
    page.give_secret()
    assert ("secrets", "give", "wild-dromouse", "GH_PUSH", "--api-key", "api.example.com", "--as", "EX_KEY") in \
        asked(sai)
    assert page.message.text().startswith("On ct1: ")


def test_a_git_login_is_given_to_a_running_start_of_the_host_chosen(qapp, sai, monkeypatch):
    page, offered, titles = credentials(sai, monkeypatch, lambda dialog: None)
    page.git.list.selectRow(0)
    page.git.give()
    assert [run for _, run in offered] == ["wild-dromouse"] and titles == ["Give to a running sandbox on ct1"]
    assert ("git-logins", "give", "--secret", "GH_PUSH", "--user", "x-access-token", "--", "wild-dromouse",
            "https://github.com") in asked(sai)
    assert page.git.message.text().startswith("On ct1: ")


def test_an_older_host_is_told_to_update(qapp, sai, monkeypatch):
    def as_key(dialog):
        dialog.host.setText("api.example.com")
    page, _offered, _titles = credentials(sai, monkeypatch, as_key)
    answer(sai, ["secrets", "give", "wild-dromouse", "GH_PUSH", "--api-key", "api.example.com", "--as", "GH_PUSH"],
           "", status=2, err="usage: sandbox-ai secrets [-h] {list,set,delete}\nsandbox-ai secrets: error: argument "
                             "command: invalid choice: 'give'")
    page.secrets.selectRow(0)
    page.give_secret()
    text = page.message.text()
    assert "ct1" in text and "sandbox-ai hosts update ct1 --from-here" in text, text


def test_no_running_start_on_the_host_is_said_with_its_name(qapp, sai, monkeypatch):
    page, offered, _titles = credentials(sai, monkeypatch, lambda dialog: None)
    answer(sai, ["list", "--json"], [{**SESSION, "state": "stopped"}])
    page.secrets.selectRow(0)
    page.give_secret()
    assert offered == [] and page.message.text() == "No sandbox runs on ct1 now."


# -- 4, 10-13: the Sandboxes page with a start of ct1 selected --

@pytest.fixture
def sandboxes(qapp, sai, monkeypatch):
    from sandbox_ai_app import ui
    page = ui.SandboxesPage(sai, lambda *a, **k: None)
    MADE.append(page)
    instance = backend.Instance("session", "remote:ct1:s-1", "wild-dromouse", host="ct1", state="running",
                                agent="claude", ident="s-1", record=dict(SESSION))
    monkeypatch.setattr(page, "current", lambda: instance)
    monkeypatch.setattr(page, "several", lambda: False)
    yield page, instance
    page.timer.stop()
    page.remote_timer.stop()
    page.deleteLater()


def test_secrets_and_logins_of_the_selected_starts_host(sandboxes):
    page, _instance = sandboxes
    opened = []
    page.open_credentials.connect(opened.append)
    page.update_buttons()
    button = page.credentials_button
    assert button.isEnabled() and widgets.button_text(button) == "Secrets and logins on ct1…"  # (an icon: its name)
    assert "ct1" in button.toolTip()
    assert button in page.table.menu_entries(), "on the start's right click too"
    button.click()
    assert opened == ["ct1"]


def test_the_credentials_page_opens_with_the_starts_host(qapp, sai, monkeypatch):
    """The window: Secrets and logins on ct1… shows the Credentials page with ct1 chosen."""
    from sandbox_ai_app import ui
    from sandbox_ai_app.credentials_page import CredentialsPage
    page = CredentialsPage(sai, lambda *a, **k: None)
    MADE.append(page)
    shown = []
    window = type("W", (), {"credentials": page, "show_page": lambda self, key: shown.append(key)})()
    ui.MainWindow.show_credentials_of(window, "ct1")
    assert page.host() == "ct1" and shown == ["credentials"]
    page.deleteLater()


def test_resume_with_offers_the_mcp_servers_of_the_starts_host(sandboxes, sai, monkeypatch):
    from sandbox_ai_app import ui
    page, instance = sandboxes
    answer(sai, ["mcp", "list", "--json"], [{"name": "fetch", "status": "stopped"}, {"name": "git", "status": "x"}])
    seen = []
    # (start: the options a session of this machine was created with, for "On the Start page…" - none for a host's)
    monkeypatch.setattr(ui.ResumeDialog, "__init__", lambda self, parent, name, host, running, servers, draft=None,
                        start=None: (seen.append((name, host, servers, start)), QtWidgets.QDialog.__init__(self, parent))[-1])
    monkeypatch.setattr(ui.ResumeDialog, "exec", lambda self: QtWidgets.QDialog.DialogCode.Rejected)
    page.resume_with(instance)
    assert seen == [("wild-dromouse", "ct1", ["fetch", "git"], None)]
    assert ("mcp", "list", "--json") in asked(sai)


def test_what_was_added_during_the_run_is_read_on_its_host(sandboxes, sai):
    page, _instance = sandboxes
    answer(sai, ["devices", "list", "wild-dromouse", "--json"], [{"id": "d1", "path": "/dev/ttyUSB0", "added": True}])
    answer(sai, ["workspace", "list", "wild-dromouse", "--json"],
           [{"host": "/srv/more", "sandbox": "/workspaces/more", "mode": "rw", "source": "added", "note": ""}])
    page.load_devices()
    assert ("devices", "list", "wild-dromouse", "--json") in asked(sai)
    assert ("workspace", "list", "wild-dromouse", "--json") in asked(sai)
    assert "on ct1" in page.workspaces_label.text() and "/srv/more" in page.workspaces_label.text()
    assert page.added_workspaces == [], "Remove… of a host's start reads that host's list afresh"


def test_a_report_of_a_start_of_the_host_is_made_there(sandboxes, sai, monkeypatch, tmp_path):
    from sandbox_ai_app import reports
    page, _instance = sandboxes
    target = tmp_path / "report.html"
    seen = {}

    def run_dialog(dialog):
        seen["title"] = dialog.windowTitle()
        seen["formats"] = [dialog.format.itemData(i) for i in range(dialog.format.count())]
        dialog.format.setCurrentIndex(dialog.format.findData("html"))
        dialog.path.setText(str(target))
        dialog.open_after.setChecked(False)
        return QtWidgets.QDialog.DialogCode.Accepted
    monkeypatch.setattr(reports.ReportDialog, "exec", run_dialog)
    answer(sai, ["report", "--format", "html", "--", "wild-dromouse"], "<html>report of ct1</html>\n")
    page.update_buttons()
    assert page.manage_buttons["report"].isEnabled(), page.manage_buttons["report"].toolTip()
    page.report()
    assert seen["title"] == "Report of wild-dromouse on ct1" and "pdf" not in seen["formats"]
    assert ("report", "--format", "html", "--", "wild-dromouse") in asked(sai)
    assert target.read_text() == "<html>report of ct1</html>\n" and (target.stat().st_mode & 0o777) == 0o600


def test_restore_home_names_the_host(sandboxes, sai, monkeypatch):
    from sandbox_ai_app import ui
    page, _instance = sandboxes
    questions = []
    monkeypatch.setattr(ui, "ask_first", lambda parent, action, title, text, *a, **k: questions.append(
        (action, title, text)) or False)
    review = {"name": "api", "review": {"complete": True, "changes": [
        {"path": ".bashrc", "change": "changed", "kind": "file", "before": {"type": "file"}, "after": {"type": "file"},
         "diff": "-a\n+b"}]}}
    page.offer_restore("ct1", "api", review)
    assert questions[-1][0] == "home-restore" and questions[-1][1] == "Restore the HOME of api on ct1"
    assert questions[-1][2].startswith("On ct1: ")


# -- 8: Activity › Safeguards follows the page's host chooser --

def test_the_safeguards_tab_shows_the_records_of_the_host_chosen(qapp, sai, monkeypatch):
    from sandbox_ai_app.activity_page import ActivityPage
    record = {"id": "r1", "time": "2026-11-16T10:00:00Z", "run": "claude-1", "kind": "dns", "level": "warning",
              "what": "lookup of evil.example refused", "why": "not allowed", "fix": "sandbox-ai net allow ..."}
    answer(sai, ["safeguards", "--json", "--tail", "1000", "--all"], json.dumps(record) + "\n")
    page = ActivityPage(sai)
    MADE.append(page)
    view = page.safeguards
    view.refresh()
    choose(page.hosts)
    assert view.host == "ct1"
    assert ("safeguards", "--json", "--tail", "1000", "--all") in asked(sai)
    assert view.tree.topLevelItemCount() == 1 and "evil.example" in view.tree.topLevelItem(0).text(3)
    view.add_live([{**record, "id": "local-1"}])
    assert view.tree.topLevelItemCount() == 1, "this machine's live records never go into a host's list"
    sai.answers[("ct1", ("safeguards", "--json", "--tail", "1000", "--all"))] = (
        2, "", "sandbox-ai: error: argument command: invalid choice: 'safeguards'")
    view.refresh()
    assert "sandbox-ai hosts update ct1 --from-here" in view.status.text()
    page.deleteLater()


# -- 5: Approvals › Blocked and Allowed now of a start of ct1 --

BLOCKED = [{"time": "2026-11-16T10:00:00Z", "run": "claude-7", "kind": "network", "what": "pypi.org:443",
            "why": "not allowed", "process": "pip", "count": 2, "target": {"host": "pypi.org"},
            "actions": ["allow-run", "allow-always", "deny-run"]},
           {"time": "2026-11-16T10:01:00Z", "run": "claude-7", "kind": "rule", "what": "POST https://api.x/y",
            "why": "a rule asks", "count": 1, "question": "q-1", "question_kind": "http",
            "actions": ["allow-run", "allow-always", "ask-again"]}]


@pytest.fixture
def approvals(qapp, sai):
    from sandbox_ai_app import approvals_sections as A
    header = A.ContextHeader(sai, 120)
    assert header.set_remote({"ct1": [("wild-dromouse", {"agent": "claude", "workspace": "/srv/work/project"})]})
    index = header.choice.findData("ct1:wild-dromouse")
    assert index >= 0 and header.choice.itemText(index) == "wild-dromouse on ct1"
    header.choice.setCurrentIndex(index)
    assert header.remote_target() == ("ct1", "wild-dromouse") and "ct1" in (header.show_state() or
                                                                             header.state.text())
    answer(sai, ["approvals", "blocked", "wild-dromouse", "--json"], BLOCKED)
    answer(sai, ["net", "show", "wild-dromouse", "--json"], {"https": [{"names": ["pypi.org"],
                                                                        "address": "151.101.0.223"}]})
    blocked, allowed = A.BlockedSection(sai, header), A.AllowedSection(sai, header)
    MADE.extend([header, blocked, allowed])
    yield header, blocked, allowed
    for widget in (header, blocked, allowed):
        widget.deleteLater()


def test_blocked_of_a_start_of_the_host_is_read_and_answered_there(approvals, sai):
    _header, blocked, _allowed = approvals
    blocked.refresh(force=True)
    assert ("approvals", "blocked", "wild-dromouse", "--json") in asked(sai)
    assert blocked.table.rowCount() == 2
    request = next(e for e in blocked.entries if e["kind"] == "rule")
    assert "allow-always" not in request["actions"], "its exception rule would go into a ruleset of this machine"
    blocked.table.selectRow(next(r for r in range(2) if blocked.table.item(r, 3).text() == "pypi.org:443"))
    blocked.act("allow-run")
    assert ("net", "allow", "claude-7", "--allow-host", "pypi.org", "--public-only") in asked(sai)
    assert widgets.SHOWN and widgets.SHOWN[-1][1].startswith("On ct1: ") if widgets.SHOWN else True
    assert not [entry for entry in sai.asked if entry[0] == "local"], "nothing to this machine"
    before = len(sai.asked)
    blocked.refresh()
    assert len(sai.asked) == before, "not read over SSH again within a few seconds unless asked"


def test_allowed_now_of_a_start_of_the_host_is_read_and_revoked_there(approvals, sai):
    _header, _blocked, allowed = approvals
    allowed.refresh()
    assert ("net", "show", "wild-dromouse", "--json") in asked(sai)
    assert allowed.table.rowCount() >= 1
    allowed.table.selectRow(0)
    allowed.revoke_selected()
    revoke = [args for args in asked(sai) if args[:2] == ("net", "revoke")]
    assert revoke and revoke[-1][2] == "wild-dromouse"


def test_an_older_host_says_to_update_for_the_blocked_list(approvals, sai):
    _header, blocked, _allowed = approvals
    answer(sai, ["approvals", "blocked", "wild-dromouse", "--json"], "", status=2,
           err="sandbox-ai approvals: error: argument command: invalid choice: 'blocked'")
    blocked.refresh(force=True)
    assert "sandbox-ai hosts update ct1 --from-here" in blocked.message.text()


def test_the_approvals_page_is_given_the_running_starts_of_the_hosts(sandboxes, qapp, sai):
    page, instance = sandboxes
    stopped = backend.Instance("session", "remote:ct1:s-2", "old-one", host="ct1", state="stopped", agent="claude")
    mcp = backend.Instance("mcp", "remote:ct1:m", "mcp-github", host="ct1", state="running")
    page.instances = {i.key: i for i in (instance, stopped, mcp)}
    assert page.remote_running() == {"ct1": [("wild-dromouse", {"agent": "claude", "workspace": ""})]}
    page.remote_problems["ct1"] = "no answer"
    assert page.remote_running() == {}, "a host that does not answer: nothing read from its last look"


# -- 6, 7: the Board on ct1: its MCP servers, and the live devices and limits of its start there --

@pytest.fixture
def board(qapp, sai, monkeypatch, tmp_path):
    from sandbox_ai_app.board_view import BoardPage
    (tmp_path / "cwd").mkdir()
    monkeypatch.chdir(tmp_path / "cwd")
    page = BoardPage(sai, lambda *a, **k: None, lambda: {"board_grid": 20})
    MADE.append(page)
    assert page.host_choice.choose("ct1")
    yield page
    page.deleteLater()


def test_the_boards_mcp_part_offers_the_servers_of_its_host(board, sai):
    answer(sai, ["mcp", "list", "--json"], [{"name": "fetch", "status": "stopped"}, {"name": "there-only"}])
    names = board.mcp_names()
    assert names == ["fetch", "there-only"] and ("mcp", "list", "--json") in asked(sai)
    combo = board.mcp_combo("")
    assert [combo.itemData(i) for i in range(combo.count())][:2] == ["fetch", "there-only"]
    before = len(sai.asked)
    board.mcp_names()
    assert len(sai.asked) == before, "read once per host"


def test_the_boards_live_devices_and_limits_go_to_its_start_on_the_host(board, sai, monkeypatch):
    from sandbox_ai_app import board_view
    questions = []
    monkeypatch.setattr(board_view, "ask_first", lambda parent, action, title, text, *a, **k: questions.append(
        (action, title, text)) or True)
    answer(sai, ["list", "--json"], [SESSION])
    found = []
    monkeypatch.setattr(board, "live_rows_found", lambda node, run, host=board_view.LOCAL: found.append((run, host)))
    board.add_node("workspace", path="/srv/work/project")
    node = board.add_node("devices")
    board.live_rows(node)
    assert found == [("wild-dromouse", "ct1")], "the start of ct1 whose workspace it is, never a run of this machine"
    button = QtWidgets.QPushButton()
    board.live_devices("wild-dromouse", ["/dev/ttyUSB0"], button, "ct1")
    assert ("devices", "add", "wild-dromouse", "/dev/ttyUSB0") in asked(sai)
    assert questions[-1][0] == "device-add" and questions[-1][2].startswith("On ct1: ")
    answer(sai, ["limits", "wild-dromouse", "--show", "--json"], {"memory": 1 << 30, "cpus": 1, "pids": 100})
    board.live_limits("wild-dromouse", {"--memory": "8g"}, button, "ct1")
    assert ("limits", "wild-dromouse", "--show", "--json") in asked(sai)
    assert any(args[:2] == ("limits", "wild-dromouse") and "--memory" in " ".join(args) for args in asked(sai))
    assert questions[-1][0] == "limits-raise" and "ct1" in questions[-1][1]
    assert not [entry for entry in sai.asked if entry[0] == "local"]


# -- 9: LLM traffic of a start on ct1 --

def test_the_llm_traffic_of_a_session_on_the_host_is_read_there(qapp, sai, monkeypatch, wait):
    import sys
    from sandbox_ai_app.traffic_page import TrafficPage
    answer(sai, ["list", "--json"], [SESSION, {"name": "quick-run", "kind": "run", "state": "running"},
                                     {"name": "gone-run", "kind": "run", "state": "stopped"}])
    line = json.dumps({"n": 1, "time": "2026-11-16T10:00:00Z", "model": "qwen3", "run": "claude-7"})
    commands = []

    def host_command(name, *args):
        commands.append((name, args))
        return [sys.executable, "-c", f"print({line!r})"]
    monkeypatch.setattr(sai, "host_command", host_command)
    page = TrafficPage(sai)
    MADE.append(page)
    page.started = True
    choose(page.hosts)
    titles = [page.source.itemText(i) for i in range(page.source.count())]
    assert titles[0].startswith("Session: wild-dromouse") and any("quick-run" in t for t in titles)
    assert not any("gone-run" in t for t in titles)
    assert not page.other_button.isEnabled(), "Other folder… is this machine's"
    assert wait(lambda: page.model.rowCount() == 1, 15)
    assert commands[-1][0] == "ct1" and commands[-1][1][:4] == ("llm", "traffic", "--session", "wild-dromouse")
    document = {"record": {"n": 1}, "view": {}}
    answer(sai, ["llm", "traffic", "--session", "wild-dromouse", "claude-7", "--request", "1", "--json"],
           json.dumps(document))
    assert page.fetch(["llm", "traffic", "--session", "wild-dromouse", "claude-7", "--request", "1", "--json"],
                      "ct1")[0] == document
    page.stop()
    page.deleteLater()
