"""Review round 4 (host switches and confirmations): what one host's page showed never stays under another host,
late answers are dropped, a secret of unknown state is asked about before it is replaced, confirmations name the host,
values go after "--", the VPN key is kept after the profile, the Files page lists in a thread and greys out its keys
during a copy, renamed sessions are offered, JSON after notes is read, and every destructive confirmation goes through
widgets.ask_first. Offscreen Qt; everything below tmp_path (HOME is moved by conftest/home_guard)."""
import ast
import json
import re
import threading
import time
from pathlib import Path

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import QEvent, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402

from sandbox_ai_app import backend, widgets  # noqa: E402

PACKAGE = Path(__file__).resolve().parents[1] / "sandbox_ai_app"
HOSTS = [{"name": "local", "kind": "local"}, {"name": "ct1", "kind": "ssh", "ssh": "u@192.0.2.10"}]


@pytest.fixture
def shown(monkeypatch):
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    return widgets.SHOWN


# -- 1. Credentials: a secret whose state is not known is asked about --

class CredentialsSai:
    agents = ("claude", "codex", "opencode")

    def __init__(self, secrets=None, remote=None):
        self.calls, self.gate = [], threading.Event()
        self.local_secrets, self.remote = secrets, remote
        self.backends = {"local": "keyring", "ct1": "file"}

    def hosts_supported(self): return True
    def host_entries(self): return HOSTS
    def remote_hosts(self): return ["ct1"]
    def login_status(self): return []
    def git_logins(self): return []
    def publish_logins(self): return []
    def has_identities(self): return False
    def vpn_profiles(self): return []
    def host_vpn_profiles(self, host): return []
    def host_login_status(self, host): return []
    def host_git_logins(self, host): return []
    def host_publish_logins(self, host): return []
    def command(self, *args): return list(args)

    def secrets(self):
        self.gate.wait(10)
        if self.local_secrets is None:
            raise RuntimeError("keyring locked")
        return "file", self.local_secrets

    def host_secrets(self, host):
        if self.remote is None:
            raise ValueError("ssh: secrets list failed")
        return "file", self.remote

    def secrets_backend(self, host="local"):
        return self.backends[host]

    def valid_secret(self, name, value=None):
        return bool(re.fullmatch(r"[A-Za-z_]\w*", name or ""))

    def set_secret(self, name, value, host="local"):
        self.calls.append(("set", name, host))
        return 0, "kept", ""


def keep(page, wait, name="API_TOKEN"):
    page.name.setText(name)
    page.value.setText("new value")
    page.set_secret()
    wait(lambda: False, 0.2)


def test_a_secret_is_asked_about_before_the_first_read_and_after_an_unreadable_keyring(qapp, wait, shown,
                                                                                        monkeypatch):
    from sandbox_ai_app.credentials_page import CredentialsPage
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)  # every question answered Cancel
    sai = CredentialsSai(secrets=[("API_TOKEN", "file")])
    page = CredentialsPage(sai, lambda *a: None)
    page.show()
    keep(page, wait)  # the keyring has not answered yet: not known
    assert len(shown) == 1 and "could not be told" in shown[0][1] and sai.calls == []
    sai.gate.set()
    assert wait(lambda: page.remote_secrets.get("local") == [("API_TOKEN", "file")])
    shown.clear()
    keep(page, wait)  # known and there: asked
    assert len(shown) == 1 and "kept already" in shown[0][1] and sai.calls == []
    shown.clear()
    keep(page, wait, "OTHER")  # known and not there: kept at once
    assert shown == [] and wait(lambda: sai.calls == [("set", "OTHER", "local")])
    page.close()

    sai = CredentialsSai(secrets=None)  # secrets() raises: the names are not known
    sai.gate.set()
    page = CredentialsPage(sai, lambda *a: None)
    page.show()
    assert wait(lambda: "keyring locked" in page.message.text())
    shown.clear()
    keep(page, wait)
    assert len(shown) == 1 and sai.calls == []
    page.close()


def test_a_hosts_unreadable_secrets_are_asked_about_and_its_backend_is_shown(qapp, wait, shown, monkeypatch):
    from sandbox_ai_app.credentials_page import CredentialsPage
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)
    sai = CredentialsSai(secrets=[], remote=None)
    sai.gate.set()
    page = CredentialsPage(sai, lambda *a: None)
    page.show()
    assert wait(lambda: "your system keyring" in page.store_label.text())
    assert "sandbox-ai secrets backend" in page.store_label.text()
    page.hosts.set_host("ct1")
    assert wait(lambda: "On ct1" in page.store_label.text() and "private files" in page.store_label.text())
    assert wait(lambda: "no answer" in page.backend_label.text() or "failed" in page.message.text())
    shown.clear()
    keep(page, wait)
    assert len(shown) == 1 and shown[0][1].startswith("On ct1") and sai.calls == []
    page.close()


# -- 2. Kits: the rows of one host never stay under another --

def test_kits_rows_go_with_a_host_switch_and_the_confirmation_names_the_host(qapp, wait, shown, monkeypatch):
    import copy
    from test_kits_page import INVENTORY
    from sandbox_ai_app import kits_page
    sai = backend.SandboxAI()
    monkeypatch.setattr(sai, "hosts_supported", lambda: True)
    monkeypatch.setattr(sai, "host_entries", lambda: HOSTS)
    monkeypatch.setattr(sai, "has_kits_command", lambda: True)
    monkeypatch.setattr(sai, "kits_inventory", lambda: copy.deepcopy(INVENTORY))
    calls = []

    def host_run(host, *args, timeout=120, input=None):
        calls.append((host, *args))
        if args[:2] == ("kits", "list"):
            return (0, json.dumps(INVENTORY), "") if ok_remote else (255, "", "ssh: Connection timed out")
        return 0, "deleted", ""
    monkeypatch.setattr(sai, "host_run", host_run)
    ok_remote = False
    page = kits_page.KitsPage(sai, lambda *a, **k: None)
    page.refresh()
    assert wait(lambda: page.table.rowCount() == 4)
    page.hosts.set_host("ct1")
    assert page.table.rowCount() == 0 and page.current() is None  # at once, before ct1 answers
    assert wait(lambda: "Cannot list" in page.summary.text())
    page.delete()
    assert not any(c[1:3] == ("kits", "delete") for c in calls) and shown == []
    ok_remote = True
    page.refresh()
    assert wait(lambda: page.table.rowCount() == 4 and page.kits_host == "ct1")
    for row in range(page.table.rowCount()):
        if page.table.item(row, 0).text() == "mine":
            page.table.selectRow(row)
    page.delete()
    assert wait(lambda: any(c[0] == "ct1" and c[1:3] == ("kits", "delete") for c in calls))
    assert any(text.startswith("On ct1:") and "Delete the kit mine" in text for _, text, _ in shown)
    # A late answer of the host before is dropped.
    token = page.token
    page.hosts.set_host("local")
    page.loaded(token, copy.deepcopy(INVENTORY), None, "ct1")
    assert page.kits_host in (None, "local")


# -- 3. Connections: a slow local read never lands under a remote host --

def test_a_slow_local_connection_log_is_dropped_after_a_host_switch(qapp, wait, shown, monkeypatch, tmp_path):
    from sandbox_ai_app import ui
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "connections-20261108-local.jsonl").write_text(json.dumps({
        "time": "2026-11-08T09:00:00Z", "event": "allowed", "method": "GET", "status": 200,
        "url": "https://LOCAL-ONLY.example/", "host": "LOCAL-ONLY.example"}) + "\n")
    sai = backend.SandboxAI()
    monkeypatch.setattr(sai, "hosts_supported", lambda: True)
    monkeypatch.setattr(sai, "host_entries", lambda: HOSTS)
    monkeypatch.setattr(sai, "log_directories", lambda: [logs])
    gate, real = threading.Event(), backend.read_connections

    def slow(*args, **kwargs):
        gate.wait(10)
        return real(*args, **kwargs)
    monkeypatch.setattr(backend, "read_connections", slow)

    def host_run(name, *args, timeout=120, input=None):
        if args[:2] == ("list", "--json"):
            return 0, json.dumps([{"name": "api", "kind": "session", "state": "running", "agent": "claude",
                                   "workspace": "/srv/p"}]), ""
        if args[:1] == ("activity",):
            return 0, json.dumps({"kind": "network", "time": "2026-11-08T10:00:00Z", "summary": "denied: REMOTE",
                                  "details": {"event": "denied", "destination": "REMOTE.example:443"}}), ""
        return 0, "", ""
    monkeypatch.setattr(sai, "host_run", host_run)
    page = ui.ConnectionsPage(sai)
    page.show()
    assert wait(lambda: page.file.count() > 0 and page.reading is not None)
    page.hosts.set_host("ct1")
    assert page.file.count() == 0  # this machine's logs are no source of ct1
    assert wait(lambda: page.table.rowCount() == 1 and "REMOTE" in page.table.item(0, 4).text())
    gate.set()
    wait(lambda: False, 0.5)
    rows = [page.table.item(r, 4).text() for r in range(page.table.rowCount())]
    assert rows == ["REMOTE.example:443"], rows
    page.close()


# -- 4. Teams, Images, Activity --

def test_teams_rows_go_with_a_host_switch(qapp, shown, monkeypatch):
    from sandbox_ai_app import team_page
    sai = backend.SandboxAI()
    monkeypatch.setattr(sai, "hosts_supported", lambda: True)
    monkeypatch.setattr(sai, "host_entries", lambda: HOSTS)
    acted = []
    monkeypatch.setattr(sai, "team_stop", lambda host, name: acted.append((host, name)) or (0, "", ""))
    monkeypatch.setattr(team_page, "background", lambda function, *a, done, **k: None)  # nothing comes back
    page = team_page.TeamPage(sai, lambda *a, **k: None) if hasattr(team_page, "TeamPage") else \
        team_page.TeamsPage(sai, lambda *a, **k: None)
    page.loaded_once = True
    page.listed("local", page.token, [{"name": "review", "members": ["claude"], "workspace": "/w"}], None)
    page.table.selectRow(0)
    assert page.chosen() == "review"
    token = page.token
    page.hosts.set_host("ct1")
    assert page.table.rowCount() == 0 and page.chosen() is None
    page.listed("local", token, [{"name": "review", "members": ["claude"], "workspace": "/w"}], None)  # late
    assert page.table.rowCount() == 0
    page.stop()
    assert acted == [] and shown == []
    # Rows that are another host's are never acted on (e.g. a refresh that did not run yet).
    page.listed("ct1", page.token, [{"name": "review", "members": ["claude"], "workspace": "/w"}], None)
    page.teams_host = "local"
    page.table.selectRow(0)
    page.stop()
    assert acted == [] and "Refresh first" in page.status.text()


def test_an_image_save_that_finishes_after_a_host_switch_is_not_shown(qapp, shown, monkeypatch):
    from sandbox_ai_app import images_page
    sai = backend.SandboxAI()
    monkeypatch.setattr(sai, "hosts_supported", lambda: True)
    monkeypatch.setattr(sai, "host_entries", lambda: HOSTS)
    page = images_page.ImagesPage(sai, lambda *a, **k: None)
    page.hosts.set_host("ct1")
    document = {"agent": "claude", "target": "claude", "apt": ["jq"], "npm": [], "pip": [], "files": []}
    page.saved(0, json.dumps(document), "", "local")
    assert page.description is None and page.target is None
    page.variant_done(0, json.dumps(document), "", "v1", "local")
    assert page.description is None


def test_image_confirmations_go_through_ask_first_and_name_the_host(qapp, shown, monkeypatch):
    from sandbox_ai_app import images_page
    sai = backend.SandboxAI()
    monkeypatch.setattr(sai, "hosts_supported", lambda: True)
    monkeypatch.setattr(sai, "host_entries", lambda: HOSTS)
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)
    page = images_page.ImagesPage(sai, lambda *a, **k: None)
    page.hosts.set_host("ct1")
    ran = []
    monkeypatch.setattr(page.task, "run", lambda *a, **k: ran.append(a))
    entry = {"target": "v1", "agent": "claude", "variant": True, "image": "img", "extended": True}
    monkeypatch.setattr(page, "entry", lambda: entry)
    page.delete_variant()
    entry.update(target="claude", variant=False)
    page.drop_extension()
    assert ran == [] and [text.split(":")[0] for _, text, _ in shown] == ["On ct1", "On ct1"]


def test_activity_host_switch_stops_the_local_stream_and_forgets_its_sources(qapp, monkeypatch, tmp_path):
    from sandbox_ai_app import activity_page
    sai = backend.SandboxAI()
    monkeypatch.setattr(sai, "hosts_supported", lambda: True)
    monkeypatch.setattr(sai, "host_entries", lambda: HOSTS)
    listed = []
    monkeypatch.setattr(activity_page.widgets, "background",
                        lambda function, *a, done, **k: listed.append(done))
    page = activity_page.ActivityPage(sai)
    page.started = True
    page.source.addItem("Project: logs", ["--log-dir", str(tmp_path)])
    page.source.setCurrentIndex(0)
    page.add_lines([json.dumps({"kind": "prompt", "summary": "LOCAL", "time": "2026-11-08T10:00:00Z"}).encode()])
    assert page.model.rowCount() == 1
    page.hosts.set_host("ct1")
    assert page.model.rowCount() == 0 and page.process is None
    assert page.source.count() == 0 or page.source.currentData() != ["--log-dir", str(tmp_path)]
    # The listing of ct1's sources is not dropped by a restart of the polls (separate counters).
    assert listed, "ct1's sources are asked for"
    page.restart()
    listed[0]([], None)
    assert "no answer" not in page.status.text()


# -- 5. Hosts named, values after "--", the VPN key after the profile --

def test_identity_steps_and_git_logins_put_values_after_a_double_dash():
    steps = backend.SandboxAI.identity_steps("-x", None, {"name": "Ada", "git": [
        {"url": "https://git.example.org", "secret": "GIT"}], "publish": [{"kind": "pypi", "secret": "PYPI"}]})
    for step in steps:
        assert "--" in step and step.index("--") < step.index("-x"), step
        assert all(not part.startswith("-") or part == "--" or part.startswith("--") and "=" in part
                   for part in step[2:step.index("--")])
    gone = backend.SandboxAI.identity_steps("w", {"name": "", "git": [{"url": "https://g", "user": "x-access-token",
                                                                       "secret": "S"}], "publish": []},
                                            {"name": "", "git": [], "publish": []})
    assert gone == [["identities", "logout", "--", "w", "https://g"]]


def test_git_login_add_and_remove_name_the_host_and_put_the_url_after_a_double_dash(qapp, shown, monkeypatch):
    from sandbox_ai_app import credentials_page
    sai = backend.SandboxAI()
    box = credentials_page.GitLoginsBox(sai, lambda: None)
    box.host = "ct1"
    ran = []
    monkeypatch.setattr(box, "run", lambda *args, what, host=None: ran.append(args))

    class Dialog:
        def __init__(self, *a, **k): pass
        def exec(self): return QtWidgets.QDialog.DialogCode.Accepted
        def values(self): return {"url": "https://git.example.org", "user": "", "secret": "GIT", "value": None}
        def forget_value(self): pass
    monkeypatch.setattr(credentials_page, "LoginDialog", Dialog)
    box.add()
    assert ran == [("add", "--secret", "GIT", "--", "https://git.example.org")]
    assert shown[-1][1].startswith("On ct1:")
    monkeypatch.setattr(credentials_page, "selected", lambda widget: "https://git.example.org")
    box.remove()
    assert ran[-1] == ("remove", "--", "https://git.example.org") and shown[-1][1].startswith("On ct1:")


def test_vpn_key_is_kept_after_the_profile_on_the_host_and_socks_names_it(qapp, shown, monkeypatch):
    from sandbox_ai_app import vpn_page
    calls = []

    class Sai:
        def set_secret(self, name, value, host="local"):
            calls.append(("secret", name, host))
            return 0, "", ""

        def host_run(self, host, *args, timeout=120, input=None):
            calls.append(("add", host, args[:3]))
            return 0, "added", ""

        def secret_exists(self, name, host="local"):
            return False

    class Dialog:
        value = QtWidgets.QLineEdit()
        def __init__(self, *a, **k): pass
        def exec(self): return QtWidgets.QDialog.DialogCode.Accepted
        def deleteLater(self): pass
        def request(self): return "ts", "tailscale", "tskey-x", {"secret": "TS", "socks": True}
    monkeypatch.setattr(vpn_page, "VpnDialog", Dialog)
    monkeypatch.setattr(vpn_page, "add_args", lambda name, kind, fields: ["vpn", "add", name])
    monkeypatch.setattr(vpn_page, "background", lambda function, *a, done, **k: done(function(), None))
    names = []
    vpn_page.add_profile(None, Sai(), names.append, host="ct1")
    assert calls == [("add", "ct1", ("vpn", "add", "ts")), ("secret", "TS", "ct1")] and names == ["ts"]
    assert shown[0][1].startswith("On ct1:") and "SOCKS" in shown[0][1]


# -- 6. Files: keys during a copy, listing in a thread, renamed sessions --

def test_files_keys_are_refused_during_a_copy_and_folders_are_listed_in_a_thread(qapp, shown, tmp_path, monkeypatch):
    import os
    from sandbox_ai_app import files, files_page
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_text("a")

    class Sai:
        def file_roots(self, extra=()):
            return [("run", "claude-abc", str(ws.resolve()))]

        def host_statuses(self, refresh=False, cached=True):
            return {}
    threads = []
    real = files.Root.list

    def listing(root, relative=""):
        threads.append(threading.current_thread() is threading.main_thread())
        return real(root, relative)
    monkeypatch.setattr(files.Root, "list", listing)
    page = files_page.FilesPage(Sai())
    from conftest import wait_until
    assert wait_until(qapp, lambda: page.left.table.rowCount() and page.left.listing is None
                      and page.right.listing is None, 30)
    assert threads and not any(threads), "every folder is listed off the GUI thread"
    assert sorted(page.left.table.item(r, 0).text() for r in range(page.left.table.rowCount())) == ["a.txt"]
    monkeypatch.setattr(type(page.task), "running", property(lambda self: True))
    asked = []
    monkeypatch.setattr(QtWidgets.QInputDialog, "getText", lambda *a, **k: asked.append(a) or ("x", True))
    for key in (Qt.Key.Key_F2, Qt.Key.Key_F7):
        event = QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier)
        QtWidgets.QApplication.sendEvent(page.left.table, event)
    assert asked == [] and not (ws / "x").exists() and "Wait until" in page.message.text()

    # A slow listing: list() returns at once (no local event loop), the pane says it lists after LIST_WAIT; when it
    # comes back after another listing began, it is dropped.
    pending = []
    monkeypatch.setattr(files_page, "background", lambda function, *a, done, **k: pending.append((function, done)))
    monkeypatch.setattr(files_page, "LIST_WAIT", 50)
    assert page.left.list() is True and page.left.listing is not None
    assert wait_until(qapp, lambda: "Listing" in page.message.text(), 5)
    (ws / "b.txt").write_text("b")
    assert page.left.list() is True
    (late_work, late_done), (work, done) = pending
    done(work(), None)
    assert sorted(page.left.table.item(r, 0).text() for r in range(page.left.table.rowCount())) == ["a.txt", "b.txt"]
    (ws / "c.txt").write_text("c")
    late_done(late_work(), None)  # began first, came back last: dropped
    assert sorted(page.left.table.item(r, 0).text() for r in range(page.left.table.rowCount())) == ["a.txt", "b.txt"]
    assert os.path.isdir(ws)


def test_a_renamed_session_is_offered_by_its_name(monkeypatch, tmp_path):
    sai = backend.SandboxAI()
    workspace = tmp_path / "work"
    workspace.mkdir()
    loaded = []

    class Sessions:
        @staticmethod
        def rows():
            return [["renamed", "claude", "podman", "stopped", str(workspace), ""]]

        @staticmethod
        def load(key):
            loaded.append(key)
            if key != "id-1":
                raise ValueError("no session")
            return {"workspace": str(workspace), "options": []}

        @staticmethod
        def session_dir(key):
            return tmp_path / "sessions" / key
    real_module = sai.module
    monkeypatch.setattr(sai, "module", lambda name: Sessions if name == "sessions" else real_module(name))
    monkeypatch.setattr(sai, "runs", lambda: {})
    monkeypatch.setattr(sai, "session_ids", lambda: {"renamed": "id-1"})
    roots = sai.file_roots()
    assert ("session", "renamed", str(workspace.resolve())) in roots and loaded == ["id-1"]


# -- 7. JSON after notes --

def test_json_after_notes_is_read(monkeypatch):
    out = ("note: the server old is unusable (no URL)\n"
           '[{"name": "ollama", "kind": "ollama", "url": "http://127.0.0.1:11434"}]\n')
    assert backend.json_part(out) == [{"name": "ollama", "kind": "ollama", "url": "http://127.0.0.1:11434"}]
    assert backend.json_part('{"a": 1}\nnote after\n') == {"a": 1}
    with pytest.raises(ValueError):
        backend.json_part("no json here\n")
    sai = backend.SandboxAI()
    monkeypatch.setattr(sai, "call", lambda *args, timeout=60, input=None: (0, out, ""))
    assert [e["name"] for e in sai.llm_endpoints()] == ["ollama"]
    from sandbox_ai_app import wizard_llm
    endpoints, notes = wizard_llm.parse_endpoints(out + "trailing note\n")
    assert [e["name"] for e in endpoints] == ["ollama"] and notes


# -- 8. Every destructive confirmation is one of widgets.ask_first --

def bare_destructive_confirms(source):
    """[line] of every confirm(..., destructive=True) in SOURCE that is not inside a function called ask_first."""
    tree, found = ast.parse(source), []

    def walk(node, inside):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            inside = inside or node.name == "ask_first"
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
            destructive = any(k.arg == "destructive" and not (isinstance(k.value, ast.Constant) and not k.value.value)
                              for k in node.keywords)
            if name == "confirm" and destructive and not inside:
                found.append(node.lineno)
        for child in ast.iter_child_nodes(node):
            walk(child, inside)
    walk(tree, False)
    return found


def test_the_walk_finds_a_bare_destructive_confirm():
    source = ("def ask_first(p):\n    return confirm(p, 't', 'x', 'y', destructive=True)\n"
              "def delete(p):\n    if confirm(p, 't', 'x', 'y', destructive=True):\n        pass\n"
              "def ask(p):\n    return widgets.confirm(p, 't', 'x', 'y')\n")
    assert bare_destructive_confirms(source) == [4]


def test_no_destructive_confirmation_outside_ask_first():
    found = [f"{path.name}:{line}" for path in sorted(PACKAGE.glob("*.py"))
             for line in bare_destructive_confirms(path.read_text())]
    assert not found, "confirm(..., destructive=True) outside widgets.ask_first (route it through ask_first with " \
                      "an action of backend.NEEDS_CONFIRM or APP_NEEDS_CONFIRM):\n" + "\n".join(found)
