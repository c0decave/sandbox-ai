"""What the app costs while it only sits there (user report: "enormous losses in places"): no Python start for a
look at another host (ssh directly, as `sandbox-ai hosts run` would), one Podman listing kept a while for every look
at what runs, a run's folder of answered questions listed again only when it changed, the Sandboxes tree updated in
place (not built again at every look), the navigation not repainted at every scan of the questions; an idle window
starts no `sandbox-ai` command, and every page shows within a bound generous enough for CI."""
import json
import os
import subprocess
import threading
import time

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6 import QtCore  # noqa: E402
from PySide6.QtGui import QColor  # noqa: E402
from sandbox_ai_app import backend, widgets  # noqa: E402

from test_projects import ask, close, env, fake_run, project  # noqa: E402,F401

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")

IDLE = 6.0  # seconds the window idles in the idle test
MAX_SHOW = 1.0  # seconds a page may take to show (generous: CI machines are slow; here it is ~10-60 ms)


def fake_programs(env, ssh_out="[]"):
    """A fake ssh that answers SSH_OUT (and notes each call) and a fake podman that lists no pod (and notes each
    call), first on PATH; returns the folder of their notes."""
    bin_dir, notes = env / "bin", env / "notes"
    bin_dir.mkdir(exist_ok=True)
    notes.mkdir(exist_ok=True)
    (bin_dir / "ssh").write_text(f"#!/bin/sh\necho \"$*\" >> {notes}/ssh\necho '{ssh_out}'\nexit 0\n")
    (bin_dir / "podman").write_text(f"#!/bin/sh\necho \"$*\" >> {notes}/podman\necho '[]'\nexit 0\n")
    for name in ("ssh", "podman"):
        (bin_dir / name).chmod(0o755)
    return bin_dir, notes


def calls(notes, name):
    path = notes / name
    return path.read_text().splitlines() if path.exists() else []


def add_host(env, name="h1"):
    config = env / "config" / "sandbox-ai"
    config.mkdir(parents=True, exist_ok=True)
    (config / "hosts.json").write_text(json.dumps({"version": 1, "hosts": {name: {"ssh": f"u@{name}.invalid"}}}))


@pytest.fixture
def started(monkeypatch):
    """Every command started (its argv), from any thread."""
    seen = []
    original = subprocess.Popen.__init__

    def note(self, args, *a, **k):
        seen.append([str(arg) for arg in (args if isinstance(args, (list, tuple)) else [args])])
        return original(self, args, *a, **k)
    monkeypatch.setattr(subprocess.Popen, "__init__", note)
    return seen


def python_starts(seen):
    """The sandbox-ai commands among SEEN (a Python start each: main.py, or a module of sandbox-ai run as a script)."""
    return [argv for argv in seen if any(arg.endswith("main.py") or arg.endswith("hosts.py") for arg in argv[:3])]


def test_another_host_is_read_through_ssh_without_a_python_start(env, monkeypatch, started):
    bin_dir, notes = fake_programs(env)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    add_host(env)
    sai = backend.SandboxAI()
    assert sai.host_questions("h1") == []
    assert not python_starts(started), started
    assert started[-1][0] == "ssh" and started[-1][-1].endswith("approvals list --json; fi")
    assert len(calls(notes, "ssh")) == 1
    # What the command line would be, as sandbox-ai's own `hosts run` builds it.
    assert started[-1] == sai.module("hosts").argv("h1", ["approvals", "list", "--json"])
    # A host it does not know: sandbox-ai's own command answers (in its own words).
    status, _, err = sai.host_run("nowhere", "approvals", "list", "--json", timeout=60)
    assert status != 0 and "nowhere" in err and python_starts(started)


def test_one_podman_listing_serves_runs_and_sessions_for_a_while(env, monkeypatch):
    bin_dir, notes = fake_programs(env)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    workspace = project(env, "p")
    # A stopped Podman session: whether its pod still runs is asked of Podman.
    folder = env / "state" / "sandbox-ai" / "sessions" / "kept"
    (folder / "home").mkdir(parents=True)
    (folder / "session.json").write_text(json.dumps({"agent": "codex", "options": [], "backend": "podman",
                                                    "pod": "agent-codex-0123abcd", "workspace": str(workspace),
                                                    "cwd": str(workspace), "created": "2026-11-02T09:00:00Z"}))
    sai = backend.SandboxAI()
    liveness = sai.module("liveness")
    assert sai.module("sessions").liveness() is liveness and sai.module("net").liveness() is liveness
    assert liveness.CACHE_SECONDS >= backend.PODS_CACHE_SECONDS
    for _ in range(3):  # three looks within the time a listing is kept
        sai.instances()
    assert len(calls(notes, "podman")) == 1
    sai.forget_pods()  # after an action: asked afresh
    sai.instances()
    assert len(calls(notes, "podman")) == 2


def test_a_quiet_folder_of_answered_questions_is_not_listed_again(env, monkeypatch):
    run = fake_run(env, "claude-abc", project(env, "p"))
    for index in range(50):
        ask(run, f"1-{index}-1", kind="net", host=f"h{index}.example.org", port=443)
        (run / "approvals" / f"1-{index}-1.done").write_text(json.dumps({"decision": "allow", "scope": "once"}))
    ask(run, "2-1-1", kind="net", host="example.org", port=443)
    past = time.time() - 60
    os.utime(run / "approvals", (past, past))
    listed = []
    original = os.scandir
    monkeypatch.setattr(backend.os, "scandir", lambda path: listed.append(str(path)) or original(path))
    cache = backend.QuestionCache()
    runs = {"claude-abc": {"directory": str(run)}}
    first = cache.scan(runs)
    assert len(first[0]) == 1 and len(first[1]) == 50 and len(listed) == 1
    assert cache.scan(runs) == first and len(listed) == 1, "a folder that did not change is not read again"
    (run / "approvals" / "2-1-1.done").write_text(json.dumps({"decision": "deny", "scope": "once"}))
    pending, answered = cache.scan(runs)
    assert len(listed) == 2 and pending == [] and len(answered) == 51
    assert {q["id"]: q["result"] for _, _, q in answered}["2-1-1"]["decision"] == "deny"


def test_the_sandboxes_tree_is_updated_in_place(qapp, env):
    from sandbox_ai_app import theme, ui
    page = ui.SandboxesPage(backend.SandboxAI(), lambda *a, **k: None)
    try:
        page.timer.stop()
        page.remote_timer.stop()

        def instance(name, state="running"):
            return backend.Instance("session", f"session:{name}", name, run=f"claude-{name}", session=name,
                                    state=state, agent="claude", project="/p")
        page.apply(([instance("a"), instance("b")], []), None)
        items = page.table.every_item()
        texts = [item.text(0) for item in items]
        page.apply(([instance("a"), instance("b")], []), None)  # the next look finds the same
        assert page.table.every_item() == items and [item.text(0) for item in items] == texts
        page.apply(([instance("a"), instance("b", "paused")], []), None)  # a cell changed: set in place
        assert page.table.every_item() == items
        b = next(item for item in items if item.text(1) == "b")
        assert "paused" in b.text(6).lower() and b.foreground(0).color().name() == QColor(theme.color("warn")).name()
        page.apply(([instance("a"), instance("b", "running")], []), None)
        assert b.foreground(0).color().name() == QColor(theme.color("ok")).name()
        page.apply(([instance("a")], []), None)  # a row went: built again
        assert [item.text(1) for item in page.table.start_items()] == ["a"]
    finally:
        page.close()


def test_an_idle_window_starts_no_sandbox_ai_command_and_repaints_nothing(qapp, env, monkeypatch):
    bin_dir, notes = fake_programs(env)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    add_host(env)
    run = fake_run(env, "claude-abc", project(env, "p"))
    ask(run, "1-2-3", kind="net", host="example.org", port=600)  # an open question: the count stays at 1
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    from sandbox_ai_app import terminal
    from sandbox_ai_app.ui import MainWindow
    monkeypatch.setattr(terminal, "available", lambda web=None: (False, "not in this test"))
    sai = backend.SandboxAI()
    # What this window starts (pages of other tests may still poll in this process): its commands - a Python start
    # each unless the command line was built here (argv: ssh) - and its Podman listings.
    commands, listings = [], []
    call = sai.call
    sai.call = lambda *args, **kw: commands.append((args, kw.get("argv"))) or call(*args, **kw)
    liveness = sai.module("liveness")
    runner = liveness.RUNNER
    monkeypatch.setattr(liveness, "RUNNER", lambda argv, **kw: listings.append(argv) or runner(argv, **kw))
    w = MainWindow(sai, check_updates=False)
    if w.tray is not None:
        w.tray.hide()
        w.tray = None
    try:
        w.show()
        w.show_page("sandboxes")
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:  # the first looks (and the start's own reads) come back
            qapp.processEvents()
            time.sleep(0.01)
        paints, before, listed = [], len(commands), len(listings)

        class Paints(QtCore.QObject):
            def eventFilter(self, obj, event):  # noqa: N802
                if event.type() == QtCore.QEvent.Type.Paint and obj is w.nav.viewport():
                    paints.append(obj)
                return False
        watcher = Paints()
        qapp.installEventFilter(watcher)
        items = w.sandboxes.table.every_item()
        try:
            deadline = time.monotonic() + IDLE
            while time.monotonic() < deadline:
                qapp.processEvents()
                time.sleep(0.01)
        finally:
            qapp.removeEventFilter(watcher)
        idle = commands[before:]
        assert not [args for args, argv in idle if argv is None], f"sandbox-ai started while idle: {idle}"
        # Per minute: the host's questions every 5 s, its starts every 10 s; Podman at most every 10 s.
        assert len(idle) <= (60 / 5 + 60 / 10) * IDLE / 60 + 3, idle
        assert len(listings) - listed <= 60 / backend.PODS_CACHE_SECONDS * IDLE / 60 + 2, listings[listed:]
        assert w.pending_count == 1 and paints == [], "the navigation is not repainted at every scan of the questions"
        assert w.sandboxes.table.every_item() == items, "the Sandboxes tree is not built again at every look"
    finally:
        close(w)


def test_every_page_shows_within_a_bound(qapp, env, monkeypatch):
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    from test_projects import make_window
    from sandbox_ai_app.ui import PAGES
    w = make_window(monkeypatch)
    try:
        w.resize(1200, 800)
        w.show()
        qapp.processEvents()
        slow = {}
        for _ in range(2):  # the first show of each page, and a later one
            for key, _title in PAGES:
                begin = time.monotonic()
                w.show_page(key)
                qapp.processEvents()
                took = time.monotonic() - begin
                slow[key] = max(slow.get(key, 0.0), took)
        assert max(slow.values()) < MAX_SHOW, {key: round(took, 3) for key, took in slow.items() if took >= MAX_SHOW}
        assert threading.current_thread() is threading.main_thread()
    finally:
        close(w)
