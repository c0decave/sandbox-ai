"""Several rows chosen at once (Ctrl+click, Shift+click) and what acts on all of them: one question naming them, then
each in turn in the background, what failed said together at the end; buttons for one row wait meanwhile."""
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import QItemSelectionModel  # noqa: E402
from sandbox_ai_app import backend, widgets  # noqa: E402
from test_ui import env, sandboxes_tree, window  # noqa: E402,F401 - the fixtures

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")
EXTENDED = QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection


def select_items(tree, items):
    flags = QItemSelectionModel.SelectionFlag
    tree.clearSelection()
    for item in items:
        tree.selectionModel().select(tree.indexFromItem(item), flags.Select | flags.Rows)


def test_the_helpers_run_each_and_report_the_failures_together(qapp, wait, monkeypatch):
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    assert widgets.listing(["a", "b"]) == "- a\n- b"
    assert widgets.listing([str(n) for n in range(20)], limit=3).endswith("… and 17 more")
    got = []

    def work(item):
        if item == "bad":
            raise RuntimeError("it broke")
        return (1, "", "refused\n") if item == "no" else (0, "ok", "")
    widgets.run_each(work, ["a", "bad", "no", "b"], done=got.append)
    assert wait(lambda: got)
    assert [item for item, _ in widgets.failed(got[0])] == ["bad", "no"]
    assert widgets.report_each(None, "Remove", got[0]) == ["bad", "no"]
    title, text, _ = widgets.SHOWN[-1]
    assert title == "Remove" and text.startswith("2 of 4 failed:") and "- bad: it broke" in text and "- no: refused" in text
    widgets.SHOWN.clear()
    assert widgets.report_each(None, "Remove", [("a", (0, "", ""), None)]) == [] and widgets.SHOWN == []


def test_several_starts_stop_pause_and_delete_at_once(window, monkeypatch, wait):
    page = sandboxes_tree(window, monkeypatch)
    assert wait(lambda: page.table.rowCount() == 4 and page.remote_reading is None, 15)
    assert page.table.selectionMode() == EXTENDED
    from sandbox_ai_app import start_tree
    keys = {item.data(0, start_tree.KEY_ROLE): item for item in page.table.start_items()}
    work, fetch, web = keys["session:work"], keys["mcp:fetch"], keys[backend.remote_key("ct1", "web")]
    select_items(page.table, [work, fetch, web])
    assert [i.name for i in page.selected_starts()] == ["work", "fetch", "web"]
    assert page.current() is None
    # what acts on several: on those it can; what acts on one waits
    assert page.stop_button.isEnabled()
    assert page.session_buttons["pause"].isEnabled()  # work and web run
    assert not page.session_buttons["delete"].isEnabled() and "None of the selected" in \
        page.session_buttons["delete"].toolTip()
    assert not page.session_buttons["attach"].isEnabled() and not page.rename_button.isEnabled()
    assert not page.resume_with_button.isEnabled() and not page.run_named["allow"].isEnabled()
    calls = []
    monkeypatch.setattr(page.sai, "call", lambda *args, **kw: calls.append(("call", *args)) or (0, "", ""))
    monkeypatch.setattr(page.sai, "stop_mcp", lambda name: calls.append(("mcp", name)) or (0, "", ""))
    monkeypatch.setattr(page.sai, "host_run", lambda host, *args, **kw: calls.append((host, *args)) or (1, "", "ssh: gone"))
    widgets.SHOWN.clear()
    page.stop()
    question = widgets.SHOWN[0][1]
    assert question.startswith("Stop these 3 starts?") and "- web on ct1" in question
    assert wait(lambda: len(calls) == 3 and not widgets.busy(page))
    assert calls == [("call", "session", "stop", "work"), ("mcp", "fetch"), ("ct1", "stop", "web")]
    title, text, _ = widgets.SHOWN[-1]
    assert title == "Stop" and text.startswith("1 of 3 failed:") and "web on ct1: ssh: gone" in text
    # Pause: the MCP server is left out, and the question says why
    calls.clear()
    widgets.SHOWN.clear()
    select_items(page.table, [work, fetch, web])
    page.session("pause")
    assert "Left out:" in widgets.SHOWN[0][1] and "- fetch: An MCP server only stops." in widgets.SHOWN[0][1]
    assert wait(lambda: len(calls) == 2 and not widgets.busy(page))
    assert calls == [("call", "session", "pause", "work"), ("ct1", "pause", "web")]


def test_several_selected_starts_stay_selected_when_the_list_changes(window, monkeypatch, wait):
    from conftest import read_now
    from sandbox_ai_app import start_tree
    page = sandboxes_tree(window, monkeypatch)
    assert wait(lambda: page.table.rowCount() == 4 and page.remote_reading is None, 15)
    keys = {item.data(0, start_tree.KEY_ROLE): item for item in page.table.start_items()}
    select_items(page.table, [keys["session:work"], keys["mcp:fetch"]])
    work, fetch = page.selected_starts()
    other = backend.Instance("session", "session:other", "other", session="other", state="running", run="claude-x")
    monkeypatch.setattr(window.sai, "instances", lambda: ([work, fetch, other], []))
    read_now(page)  # another row: the tree is built again
    assert wait(lambda: page.table.rowCount() == 5)
    assert [i.name for i in page.selected_starts()] == ["work", "fetch"]
    assert "2 rows are selected" in page.detail.text()


def select_all(view):
    assert view.selectionMode() == EXTENDED
    view.selectAll()


def press_delete(view):
    """The Delete key's action of VIEW (a key press needs a window with the focus, which an offscreen test may not
    get)."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QKeySequence
    action = next(a for a in view.actions() if a.objectName() == "deleteAction")
    assert QKeySequence(QKeySequence.StandardKey.Delete) in action.shortcuts()
    assert action.shortcutContext() == Qt.ShortcutContext.WidgetWithChildrenShortcut
    action.trigger()


@pytest.fixture
def credentials(qapp, monkeypatch):
    from test_credentials_page import FakeSai
    from sandbox_ai_app.credentials_page import CredentialsPage
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    view = CredentialsPage(FakeSai(), lambda *a, **k: None)
    yield view
    view.deleteLater()


def test_several_secrets_and_logins_are_removed_at_once(credentials, wait):
    page = credentials
    page.refresh()
    assert wait(lambda: not widgets.busy(page) and page.secrets.rowCount() == 2 and page.git.list.rowCount() == 2)
    select_all(page.secrets)
    assert not page.give_key_button.isEnabled() and page.remove_button.isEnabled()
    page.sai.calls.clear()
    press_delete(page.secrets)  # the Delete key: as Remove…
    assert widgets.SHOWN[0][1].startswith("Remove these 2 secrets?") and "- GH_PUSH\n- PYPI_TOKEN" in widgets.SHOWN[0][1]
    assert wait(lambda: not widgets.busy(page))
    assert [c for c in page.sai.calls if c[0] == "delete"] == [("delete", "GH_PUSH", "local"), ("delete", "PYPI_TOKEN", "local")]
    assert page.message.text() == "Removed 2 of 2."
    # the Git logins: one question, each removed with its own command
    assert wait(lambda: not widgets.busy(page) and page.git.list.rowCount() == 2)
    select_all(page.git.list)
    assert not page.git.give_button.isEnabled()
    widgets.SHOWN.clear()
    page.sai.calls.clear()
    page.git.remove()
    assert "Remove these 2 logins?" in widgets.SHOWN[0][1]
    assert wait(lambda: not widgets.busy(page.git))
    assert sorted(c for c in page.sai.calls if c[:2] == ("git-logins", "remove")) == [
        ("git-logins", "remove", "--", "http://localhost:3080"), ("git-logins", "remove", "--", "https://github.com")]
    # one selected again: Give… acts on it
    page.git.list.selectRow(0)
    assert page.git.give_button.isEnabled()


def test_several_hosts_are_removed_at_once_and_this_machine_stays(qapp, monkeypatch, tmp_path):
    from sandbox_ai_app.hosts_page import HostsPage
    import sandbox_ai_app.hosts_page as hp
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir(exist_ok=True)
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    monkeypatch.setattr(widgets, "background", lambda function, *args, done, **kw: done(function(*args), None))
    monkeypatch.setattr(hp, "background", lambda function, *args, done, **kw: done(function(*args), None))
    sai = backend.SandboxAI()
    if not sai.hosts_supported():
        pytest.skip("this sandbox-ai has no hosts.py")
    for name in ("ct1", "ct2", "ct3"):
        sai.host_add(name, f"admin@{name}.example.org", 22, "", "")
    page = HostsPage(sai, lambda argv, title: None)
    removed = []
    page.host_removed.connect(removed.append)
    select_all(page.table)
    assert page.remove_button.isEnabled()
    assert not page.edit_button.isEnabled() and not page.test_button.isEnabled() and "4 are selected" in \
        page.edit_button.toolTip()
    real = sai.host_remove
    monkeypatch.setattr(sai, "host_remove", lambda name: (_ for _ in ()).throw(ValueError("in use")) if name == "ct2"
                        else real(name))
    page.remove()
    question = widgets.SHOWN[0][1]
    assert question.startswith("Remove these 3 hosts") and "- ct1" in question and "local" not in question
    assert [e["name"] for e in sai.host_entries()] == ["local", "ct2"] and removed == ["ct1", "ct3"]
    title, text, _ = widgets.SHOWN[-1]
    assert text.startswith("1 of 3 failed:") and "- ct2  admin@ct2.example.org: in use" in text
    page.close()


def synchronous(function, *args, done, **kw):
    try:
        result = function(*args)
    except Exception as exc:  # noqa: BLE001
        done(None, exc)
        return
    done(result, None)


def test_several_vpn_profiles_and_identities_are_removed_at_once(qapp, monkeypatch):
    from sandbox_ai_app import identities_page, vpn_page
    for module in (widgets, identities_page, vpn_page):
        monkeypatch.setattr(module, "background", synchronous)
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    sai = backend.SandboxAI()
    calls = []
    monkeypatch.setattr(sai, "vpn_profiles", lambda: [{"profile": name, "type": "wireguard", "provider": ""}
                                                      for name in ("home", "work", "tor")])
    monkeypatch.setattr(sai, "vpn_remove", lambda name: calls.append(("vpn", name)) or (0, "", ""))
    monkeypatch.setattr(sai, "call", lambda *args, **kw: calls.append(args) or (0, "", ""))
    box = vpn_page.VpnBox(sai)
    box.refresh()
    assert box.profiles.rowCount() == 3
    select_all(box.profiles)
    assert not box.show_button.isEnabled() and not box.check_button.isEnabled() and box.remove_button.isEnabled()
    press_delete(box.profiles)
    assert widgets.SHOWN[0][1].startswith("Remove these 3 VPN profiles?")
    assert sorted(calls) == [("vpn", "home"), ("vpn", "tor"), ("vpn", "work")]
    assert box.message.text() == "Removed 3 of 3."
    box.deleteLater()
    calls.clear()
    widgets.SHOWN.clear()
    ids = identities_page.IdentitiesBox(sai)
    ids.shown(({"work": {"name": "Ada"}, "home": {"name": "Ada"}}, []), None)
    select_all(ids.list)
    assert not ids.edit_button.isEnabled() and ids.remove_button.isEnabled()
    ids.remove()
    assert "Remove these 2 identities?" in widgets.SHOWN[0][1] and "- home\n- work" in widgets.SHOWN[0][1]
    assert [c for c in calls if c[:2] == ("identities", "remove")] == [
        ("identities", "remove", "--", "home"), ("identities", "remove", "--", "work")]
    ids.deleteLater()


def test_several_image_variants_are_deleted_at_once(window, monkeypatch, wait):
    page = window.images
    page.show_command.start = lambda argv: True
    page.show_images([{"agent": "claude", "target": name, "variant": name != "claude", "present": False,
                       "image": f"localhost/claude-sandbox-{name}:latest", "extended": True, "status": "not-built"}
                      for name in ("claude", "web", "gpu")])
    ran = []
    monkeypatch.setattr(page, "run_argv", lambda *args, **kw: ran.append(args) or
                        ["sh", "-c", "echo gpu is in use >&2; exit 1" if args[2] == "gpu" else "exit 0"])
    monkeypatch.setattr(page, "refresh", lambda *a, **k: None)
    select_all(page.images)
    assert page.delete_button.isEnabled() and not page.build_button.isEnabled()
    widgets.SHOWN.clear()
    press_delete(page.images)  # the Delete key: as Delete variant…
    question = widgets.SHOWN[0][1]
    assert "Delete these 2 variants and their images?" in question and "- web\n- gpu" in question
    assert "claude\n" not in question.split("\n\n", 1)[1]  # the agent's own image is no variant: left alone
    removed = lambda: [args for args in ran if args[:2] == ("images", "remove")]  # noqa: E731
    assert wait(lambda: len(removed()) == 2 and not page.task.running, 30)
    assert removed() == [("images", "remove", "web"), ("images", "remove", "gpu")]
    assert widgets.SHOWN[-1][1].startswith("1 of 2 failed:") and "gpu: gpu is in use" in widgets.SHOWN[-1][1]


def test_several_mcp_servers_stop_at_once(window, monkeypatch, wait):
    page = window.mcp
    asked = []
    monkeypatch.setattr(window.sai, "host_run", lambda host, *args, **kw: asked.append((host, args)) or (0, "", ""))
    monkeypatch.setattr(page, "refresh", lambda *a, **k: None)
    page.servers_read([["fetch", "running", "", "", ""], ["docs", "running", "", "", ""]], None)
    select_all(page.servers)
    assert not page.server_buttons[0].isEnabled() and not page.attach_button.isEnabled()  # Start…, Attach to…: one
    widgets.SHOWN.clear()
    page.server("stop")
    assert widgets.SHOWN[0][1].startswith("Stop these 2 MCP servers?")
    assert wait(lambda: not widgets.busy(page) and len(asked) == 2)
    assert asked == [("local", ("mcp", "stop", "fetch")), ("local", ("mcp", "stop", "docs"))]


def test_several_allowed_always_entries_are_forgotten_at_once(window, env, wait):
    approvals = window.sai.module("approvals")
    if not hasattr(approvals, "remembered"):
        pytest.skip("this sandbox-ai has no approvals.remembered")
    project = env / "project"
    project.mkdir(exist_ok=True)
    for host in ("pypi.org", "files.pythonhosted.org", "example.org"):
        approvals.remember(project, host=host)
    page = window.approvals
    page.load_always()
    assert wait(lambda: page.always.rowCount() == 3)
    widgets.select_keys(page.always, [key for key in page.always.row_keys if key[1] != "example.org"])
    widgets.SHOWN.clear()
    press_delete(page.always)  # the Delete key: as Forget…
    assert widgets.SHOWN[0][1].startswith("Forget these 2 entries?") and "pypi.org (" in widgets.SHOWN[0][1]
    assert wait(lambda: not widgets.busy(page) and page.always.rowCount() == 1, 60)
    assert page.always.item(0, 1).text() == "example.org" and page.always_message.text() == "Forgotten: 2 of 2."

