"""The logins of the sessions in the app (user 2026-11-18): the Credentials page's Agent logins tab lists which login
each running or kept session uses (sandbox-ai login --status --sessions, read in the background), a double click shows
its details (no secret), Import as central login… asks naming the account and what it replaces, Use for a new start…
opens the Board with "Login from: a session"; the Board's part HOME and login has "Login from" with its checks (a
session gone, another agent's, the rotating refresh tokens with Import as central login instead). The end-to-end
tests run the real sandbox-ai (SANDBOX_AI_HOME) against fixture sessions; no token may show anywhere."""
import base64
import json
import os
from pathlib import Path
import time

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import QEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QComboBox, QPushButton  # noqa: E402
from sandbox_ai_app import backend, board as B, widgets  # noqa: E402
from sandbox_ai_app import session_logins_view as V  # noqa: E402

from test_projects import close, env, make_window, window  # noqa: E402,F401 - shared fixtures

SECRET = "SECRET-TOKEN-VALUE"
FUTURE_MS = int((time.time() + 6 * 3600) * 1000)
needs_checkout = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                    reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


def info(fingerprint="1a2b3c4d", email="me@example.com", kind="subscription"):
    return {"agent": "claude", "kind": kind, "account": {"email": email, "organization": "Example Org", "plan": "max",
                                                         "account_id": None, "providers": []},
            "expires": "2026-11-18T18:00:00Z", "refresh_expires": None, "last_refresh": "2026-11-18T12:00:00Z",
            "fingerprint": fingerprint}


SESSIONS = {
    "sessions": [
        {"name": "web", "id": "web", "agent": "claude", "state": "running", "source": "host", "source_session": None,
         "login": True, "problem": None, "info": info(), "shares_central": False},
        {"name": "api", "id": "api", "agent": "codex", "state": "stopped", "source": "session", "source_session": "x",
         "login": True, "problem": None,
         "info": {**info("9f8e7d6c", "dev@example.com"), "agent": "codex"}, "shares_central": False},
        {"name": "bare", "id": "bare", "agent": "claude", "state": "stopped", "source": "none", "source_session": None,
         "login": False, "problem": "no login in its HOME", "info": None, "shares_central": False},
    ],
    "central": {"claude": info("00ff00ff", "old@example.com"), "codex": None},
}


class FakeSai:
    agents = ("claude", "codex")

    def __init__(self):
        self.calls = []

    def session_logins(self, host="local"):
        self.calls.append(("session_logins", host))
        return json.loads(json.dumps(SESSIONS))

    def import_session_login(self, host, agent, name, fingerprint, replace=False):
        self.calls.append(("import", host, agent, name, fingerprint, replace))
        return {"imported": True, "agent": agent, "session": name, "notes": []}


@pytest.fixture
def box(qapp, monkeypatch):
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    used = []
    view = V.SessionLoginsBox(FakeSai(), use=lambda *a: used.append(a))
    view.used = used
    yield view
    widgets.ThreadCall.wait_all(30)
    view.deleteLater()


def cells(table):
    return [[table.item(r, c).text() if table.item(r, c) else "" for c in range(table.columnCount())]
            for r in range(table.rowCount())]


def select(box, name):
    for row in range(box.list.rowCount()):
        if box.list.item(row, 0).text() == name:
            box.list.selectRow(row)
            return
    raise AssertionError(name)


def test_the_list_shows_each_sessions_login_never_a_token(box, wait):
    box.set_host("local")
    wait(lambda: box.list.rowCount() == 3)
    rows = {row[0]: row for row in cells(box.list)}
    assert rows["web"][1:5] == ["claude", "running", "this computer's login", "me@example.com, Example Org"]
    assert rows["web"][7] == "1a2b3c4d" and rows["api"][3] == "from session x"
    assert rows["bare"][4] == "no login in its HOME"
    select(box, "web")
    dialog = box.details()
    fields = {label: field.text() for label, field in dialog.fields.items()}
    assert fields["Account (as the file says)"] == "me@example.com" and fields["Fingerprint"] == "1a2b3c4d"
    assert fields["Login from"] == "this computer's login" and fields["The same login as the central one"] == "no"
    dialog.close()
    assert SECRET not in json.dumps(cells(box.list)) + json.dumps(fields)
    # the buttons, also on the right-click menu (the app's rule)
    assert box.list.menu_entries() == [box.details_button, box.import_button, box.use_button, box.give_button,
                                       box.refresh_button]
    select(box, "bare")
    assert not box.import_button.isEnabled() and not box.use_button.isEnabled() and box.details_button.isEnabled()


def test_import_asks_naming_the_account_and_what_it_replaces(box, wait, monkeypatch):
    asked = []
    monkeypatch.setattr(V, "ask_first", lambda parent, action, title, text, accept: asked.append((action, text)) or True)
    box.set_host("ct1")
    wait(lambda: box.list.rowCount() == 3)
    select(box, "web")
    assert box.import_login()
    wait(lambda: any(call[0] == "import" for call in box.sai.calls))
    assert asked[0][0] == "login-import"
    assert "me@example.com" in asked[0][1] and "old@example.com" in asked[0][1] and "on ct1" in asked[0][1]
    assert ("import", "ct1", "claude", "web", "1a2b3c4d", True) in box.sai.calls, "replaces another login: --replace"
    select(box, "api")
    assert box.import_login()
    wait(lambda: ("import", "ct1", "codex", "api", "9f8e7d6c", False) in box.sai.calls)
    assert "replaces" not in asked[1][1]


def test_use_for_a_new_start_hands_the_session_on(box, wait):
    box.set_host("local")
    wait(lambda: box.list.rowCount() == 3)
    select(box, "api")
    box.use_button.click()
    assert box.used == [("local", "codex", "api")]


def test_a_failed_read_is_said_and_a_late_answer_for_another_host_is_dropped(box, wait):
    box.sai.session_logins = lambda host="local": (_ for _ in ()).throw(ValueError("ct1: older sandbox-ai"))
    box.set_host("ct1")
    wait(lambda: "Could not read" in box.message.text())
    box.read(box.token - 1, "ct1", SESSIONS, None)
    assert box.list.rowCount() == 0


# --- the Board: Login from ---------------------------------------------------------------------------------------------

def board_with(agent="claude", **home):
    board = B.Board()
    board.add("agent", agent=agent)
    if home:
        board.add("home", **home)
    return board


def test_login_from_choices_and_their_options():
    props = {}
    assert B.login_choice(props) == "central" and B.login_choice(props, central=False) == "host"
    B.set_login_choice(props, "session", "web")
    assert props == {"--auth-from-session": "web"} and B.login_choice(props) == "session"
    B.set_login_choice(props, "none")
    assert props == {"--no-auth": True}
    B.set_login_choice(props, "file")
    assert props == {"login_from": "file"} and B.login_choice(props) == "file"
    B.set_login_choice(props, "central")
    assert props == {}


def test_the_boards_checks_of_a_login_from_a_session():
    def texts(board, sessions):
        _agent, values, _ = B.values(board)
        return [(p.level, p.text) for p in B.session_login_problems(board, "claude", values, sessions)]
    board = board_with(**{"--auth-from-session": "web"})
    found = texts(board, None)
    assert [level for level, _ in found] == ["warning"] and "rotate their refresh tokens" in found[0][1]
    assert texts(board, SESSIONS) == found, "a session of the agent with a login: only the warning"
    gone = texts(board_with(**{"--auth-from-session": "ended"}), SESSIONS)
    assert ("error", "The session ended is neither running nor kept with its HOME: it has no login to give.") in gone
    other = texts(board_with(**{"--auth-from-session": "api"}), SESSIONS)
    assert ("error", "The session api runs codex: its login is no claude login.") in other
    bare = texts(board_with(**{"--auth-from-session": "bare"}), SESSIONS)
    assert ("error", "The session bare has no usable login: no login in its HOME") in bare
    unchosen = texts(board_with(login_from="session"), SESSIONS)
    assert ("error", "Login from a session: choose the session.") in unchosen
    assert texts(board_with(login_from="file"), SESSIONS) == [("error", "Login from a login file: choose the file.")]
    assert texts(board_with(), SESSIONS) == []


@pytest.fixture
def page(qapp, tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    (tmp_path / "cwd").mkdir()
    monkeypatch.chdir(tmp_path / "cwd")
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    from sandbox_ai_app.board_view import BoardPage
    sai = backend.SandboxAI()
    sai.session_logins = lambda host="local": json.loads(json.dumps(SESSIONS))
    imported = []
    sai.import_session_login = lambda *args: imported.append(args) or {"imported": True, "notes": []}
    view = BoardPage(sai, lambda *a, **k: None, lambda: {"board_grid": 20})
    view.imported = imported
    view.resize(1200, 800)
    view.show()
    qapp.processEvents()
    yield view
    view.check_timer.stop()
    view.close()
    widgets.ThreadCall.wait_all(30)
    view.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def inspect_home(page, qapp):
    home = page.board.first("home") or page.board.add("home")
    page.scene.clearSelection()
    page.items[home.id].setSelected(True) if home.id in page.items else None
    qapp.processEvents()
    page.inspect(page.board.node(home.id))
    return page.board.node(home.id)


@needs_checkout
def test_the_board_offers_the_hosts_sessions_and_checks_them(page, qapp, wait):
    page.session_login_data["local"] = json.loads(json.dumps(SESSIONS))
    page.session_login_when["local"] = time.monotonic()
    home = inspect_home(page, qapp)
    combo = page.login_combo
    keys = [combo.itemData(i) for i in range(combo.count())]
    assert keys == ["central", "host", "session", "file", "none"] and combo.currentData() == "central"
    assert not combo.model().item(combo.findData("host")).isEnabled(), "a central login is stored: it is the one"
    combo.setCurrentIndex(combo.findData("session"))
    qapp.processEvents()
    assert page.board.node(home.id).props == {"--auth-from-session": "web"}, "the agent's first session with a login"
    page.refresh()
    assert any("rotate their refresh tokens" in p.text for p in page.problems)
    page.inspect(page.board.node(home.id))
    sessions = [w for w in page.inspector_holder.findChildren(QComboBox) if w.accessibleName() == "Session"][-1]
    assert [sessions.itemData(i) for i in range(sessions.count())] == ["web"], "only the agent's sessions with a login"
    assert "me@example.com" in sessions.itemText(0)
    from sandbox_ai_app.board_view import summary
    assert summary(page.board.node(home.id)) == "login from session web"
    # Import as central login instead: asked (AUTO_ANSWER), imported, and the start takes the central login
    button = page.import_instead_button
    assert isinstance(button, QPushButton) and button.isEnabled()
    button.click()
    wait(lambda: page.imported)
    assert page.imported[0] == ("local", "claude", "web", "1a2b3c4d", True)
    wait(lambda: "--auth-from-session" not in page.board.node(home.id).props)
    # a session of another agent named on the board: an error
    page.set_prop(home.id, "--auth-from-session", "api")
    page.refresh()
    assert any(p.level == "error" and "runs codex" in p.text for p in page.problems)


@needs_checkout
def test_the_option_is_passed_with_the_start_and_never_written_into_the_project_file(page):
    home = page.board.first("home") or page.board.add("home")
    B.set_login_choice(home.props, "session", "web")
    assert "--auth-from-session" in B.launcher_argv(page.sai, page.board)
    document, _ = B.project_document(page.sai, page.board)
    assert "--auth-from-session" not in json.dumps(document)
    assert "--auth-from-session" in B.file_start_argv(page.sai, page.board)


# --- end to end: the real sandbox-ai, the real window ------------------------------------------------------------------

def put_session(state, key, agent="claude", files=None, options=()):
    directory = state / "sandbox-ai" / "sessions" / key
    (directory / "home").mkdir(parents=True)
    os.chmod(directory / "home", 0o700)
    for relative, data in (files or {}).items():
        path = directory / "home" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data)
    (directory / "session.json").write_text(json.dumps({
        "agent": agent, "options": ["--workspace", "/srv/project", *options], "agent_args": [], "backend": "podman",
        "workspace": "/srv/project", "cwd": "/", "created": "2026-11-18T00:00:00Z", "keep": True}))


def claude_login(refresh="1", email="me@example.com"):
    return {".claude/.credentials.json": json.dumps({"claudeAiOauth": {
                "accessToken": f"{SECRET}-a{refresh}", "refreshToken": f"{SECRET}-r{refresh}", "expiresAt": FUTURE_MS,
                "subscriptionType": "pro"}}),
            ".claude.json": json.dumps({"oauthAccount": {"emailAddress": email, "accountUuid": "acc-1",
                                                         "organizationUuid": "org-1"}})}


def codex_login():
    claims = base64.urlsafe_b64encode(json.dumps({"email": "dev@example.com"}).encode()).decode().rstrip("=")
    return {".codex/auth.json": json.dumps({"tokens": {"id_token": f"x.{claims}.{SECRET}", "access_token": f"{SECRET}-a",
                                                       "refresh_token": f"{SECRET}-r", "account_id": "acct-1"}})}


@needs_checkout
def test_end_to_end_through_the_real_sandbox_ai(env):
    put_session(env / "state", "web", files=claude_login())
    put_session(env / "state", "api", "codex", files=codex_login(), options=("--auth-from-session", "web"))
    sai = backend.SandboxAI()
    found = sai.session_logins("local")
    rows = {row["name"]: row for row in found["sessions"]}
    assert rows["web"]["info"]["account"]["email"] == "me@example.com" and rows["web"]["source"] == "host"
    assert rows["api"]["source"] == "session" and rows["api"]["source_session"] == "web"
    assert SECRET not in json.dumps(found)
    with pytest.raises(ValueError, match="changed since it was shown"):
        sai.import_session_login("local", "claude", "web", "00000000")
    done = sai.import_session_login("local", "claude", "web", rows["web"]["info"]["fingerprint"])
    assert done["imported"] and SECRET not in json.dumps(done)
    stored = env / "data" / "sandbox-ai" / "auth" / "claude" / ".claude/.credentials.json"
    assert json.loads(stored.read_text())["claudeAiOauth"]["refreshToken"] == f"{SECRET}-r1"
    assert sai.session_logins("local")["sessions"][0]["shares_central"] or \
        any(row["shares_central"] for row in sai.session_logins("local")["sessions"])


@needs_checkout
def test_the_window_lists_imports_and_starts_with_a_sessions_login(window, env, wait, qapp):
    put_session(env / "state", "web", files=claude_login())
    put_session(env / "state", "api", "codex", files=codex_login())
    page = window.credentials
    box = page.session_logins
    box.refresh()
    wait(lambda: box.list.rowCount() == 2, 60)
    assert SECRET not in json.dumps(cells(box.list))
    select(box, "web")
    assert box.import_login()  # AUTO_ANSWER: the confirmation says yes
    stored = env / "data" / "sandbox-ai" / "auth" / "claude" / ".claude/.credentials.json"
    wait(lambda: stored.exists(), 60)
    wait(lambda: "central login now" in box.message.text(), 60)
    select(box, "api")
    box.use_button.click()
    qapp.processEvents()
    board = window.board
    assert window.current_page() == "board"
    assert board.board.first("agent").props.get("agent") == "codex"
    assert board.board.first("home").props.get("--auth-from-session") == "api"
    assert "--auth-from-session" in B.launcher_argv(board.sai, board.board)
    from sandbox_ai_app import applog
    log = applog.log_path()
    assert SECRET not in (log.read_text(errors="replace") if log.exists() else ""), "no token in the app's log"
