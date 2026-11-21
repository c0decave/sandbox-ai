"""Stop all… (user wish from live use, 2026-11-14): Sandboxes & sessions stops every start that runs, is paused or
orphaned - not the MCP servers - on this machine and the hosts (or the host chosen), after a question that lists
them; `sandbox-ai stop --all --yes` here, `hosts run HOST -- stop --all --yes` on a host, in the background. The
Hosts page's menu does it for one host. FAKES ONLY: sandbox-ai's call and host_run are replaced before anything is
clicked - this test never stops a real start, never touches Podman or tmux."""
import json

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, widgets  # noqa: E402
from test_ui import env, window  # noqa: E402,F401 - the isolated home and window of the UI tests


def start(name, state="running", kind="session", host="local"):
    return backend.Instance(kind, f"{kind}:{host}:{name}", name, run=f"r-{name}", session=name if kind == "session"
                            else None, agent="claude", state=state, host=host)


@pytest.fixture
def fake(window, monkeypatch, wait):
    """The window's sandbox-ai with every command replaced: what was asked is recorded, nothing runs."""
    calls = []

    def call(*args, timeout=None, **kw):
        calls.append(("local", args))
        return 0, "stopped 2 of 2", ""

    def host_run(host, *args, timeout=None, **kw):
        calls.append((host, args))
        return 0, "stopped 1 of 1", ""

    def host_json(host, *args, **kw):
        calls.append((host, args))
        return [{"name": "far", "kind": "session", "state": "running"}, {"name": "mcp-x", "kind": "mcp",
                                                                           "state": "running"}]
    for name, value in (("call", call), ("host_run", host_run), ("host_json", host_json)):
        monkeypatch.setattr(window.sai, name, value)
    monkeypatch.setattr(window.sandboxes, "refresh_soon", lambda again=False: None)
    monkeypatch.setattr(window.sandboxes, "refresh_remote", lambda hosts=None, polled=False: calls.append(
        ("refresh", tuple(hosts or ()))))
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    return calls


def test_stop_all_asks_with_the_list_then_stops_here_and_on_the_hosts(window, fake, wait, monkeypatch):
    page = window.sandboxes
    page.instances = {i.key: i for i in (start("web"), start("held", "paused"), start("old", "stopped"),
                                         start("fetch", kind="mcp"))}
    page.remote = {"box": [start("far", host="box"), start("gone", "stopped", host="box")]}
    # (the real re-read after Stop all, due a moment later, emptied page.remote below on a loaded machine)
    monkeypatch.setattr(page, "refresh_soon", lambda *a, **k: fake.append(("refresh_soon",)))
    targets = page.stop_all_targets()
    assert {host: [i.name for i in found] for host, found in targets.items()} == {"local": ["web", "held"],
                                                                                 "box": ["far"]}
    menu = [text for text, _ in widgets.menu_entries(widgets.menu_of(page.table))]
    assert "Stop all…" in menu, "in the list's context menu too"
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)  # Cancel: nothing runs
    assert page.stop_all() is False and fake == []
    title, text, _ = widgets.SHOWN[-1]
    assert title == "Stop all" and "Stop these 3 starts?" in text and "this machine: web, held" in text \
        and "box: far" in text
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    assert page.stop_all() is True
    assert wait(lambda: ("refresh", ("box",)) in fake, 10)
    assert ("local", ("stop", "--all", "--yes")) in fake and ("box", ("stop", "--all", "--yes")) in fake
    # The host chosen above: only its starts.
    fake.clear()
    page.host_filter.addItem("box", "box")
    page.host_filter.setCurrentIndex(page.host_filter.findData("box"))
    assert list(page.stop_all_targets()) == ["box"]
    # Nothing that runs: said, nothing asked.
    page.instances, page.remote = {}, {}
    assert page.stop_all() is False and fake == []


def test_the_hosts_page_stops_all_on_one_host(window, fake, wait, monkeypatch):
    page = window.hosts
    monkeypatch.setattr(page, "host", lambda: "box")
    assert page.stop_all() is True
    assert wait(lambda: ("box", ("stop", "--all", "--yes")) in fake, 10)
    assert fake[0] == ("box", ("list", "--json"))
    question = next(text for title, text, _ in widgets.SHOWN if title == "Stop all")
    assert "far" in question and "mcp-x" not in question
    monkeypatch.setattr(page, "host", lambda: "local")
    assert page.stop_all() is False
    assert json.dumps(fake)
