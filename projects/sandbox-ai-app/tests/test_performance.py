"""What the app costs while it only sits there (user report: "enormous losses in places"): no Python start for a
look at another host (ssh directly, as `sandbox-ai hosts run` would, through the host's shared connection), one Podman
listing kept a while for every look at what runs, a run's folder of answered questions listed again only when it
changed, the Sandboxes tree updated in place (not built again at every look), the navigation not repainted at every
scan of the questions; an idle window starts no `sandbox-ai` command, and every page shows within a bound generous
enough for CI. At the start (a lazy window, as the app makes it): only the first pages, the others when shown or in
idle time - and then the window is the same as one made at once."""
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
    monkeypatch.setenv("SANDBOX_AI_SSH_MULTIPLEX", "0")  # one ssh per read here; the shared connection: below
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


def shared_ssh(env, monkeypatch, refuse=False):
    """A fake ssh that notes each call; with REFUSE it says what ssh says when a shared connection refuses a session
    (exit status 255, nothing reached the host) to every call through one. The hosts module's shared connection is
    faked around it: (sai, notes, what ensure_master and stop_master were asked)."""
    bin_dir, notes = env / "bin", env / "notes"
    bin_dir.mkdir(exist_ok=True)
    notes.mkdir(exist_ok=True)
    refusal = "case \"$*\" in *ControlPath=*) echo 'mux_client_request_session: session request failed: Session " \
        "open refused by peer' >&2; exit 255;; esac\n" if refuse else ""
    (bin_dir / "ssh").write_text(f"#!/bin/sh\necho \"$*\" >> {notes}/ssh\n{refusal}echo '[]'\nexit 0\n")
    (bin_dir / "ssh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    add_host(env)
    sai = backend.SandboxAI()
    hosts = sai.module("hosts")
    socket = env / "control"
    socket.write_text("")  # (stands for the socket: it exists)
    asked = {"ensure": 0, "stop": []}

    def ensure(entry, path, runner=None, deadline=None):
        asked["ensure"] += 1
        return True
    monkeypatch.setattr(hosts, "control_path", lambda entry, platform=None: socket)
    monkeypatch.setattr(hosts, "ensure_master", ensure)
    monkeypatch.setattr(hosts, "stop_master", lambda entry, path=None, runner=None, timeout=5, how="exit":
                        asked["stop"].append(how))
    return sai, notes, socket, asked


def test_a_host_is_read_through_its_shared_connection(env, monkeypatch, started):
    """Every read of a host goes through its shared SSH connection (no new connection, key exchange and login on the
    host every few seconds); the connection is checked or started once, then trusted while it carries the reads."""
    sai, notes, socket, asked = shared_ssh(env, monkeypatch)
    for _ in range(5):
        assert sai.host_questions("h1") == []
        assert sai.host_instances("h1") == []
    assert asked["ensure"] == 1 and asked["stop"] == []
    lines = calls(notes, "ssh")
    assert len(lines) == 10 and all(f"ControlPath={socket}" in line and "ControlMaster=no" in line for line in lines)
    assert started[-1] == sai.module("hosts").argv("h1", ["list", "--json"], control=socket)
    assert not python_starts(started)
    # Older than MASTER_TRUSTED: asked again once.
    monkeypatch.setattr(backend, "MASTER_TRUSTED", 0)
    sai.host_questions("h1")
    assert asked["ensure"] == 2


def test_a_shared_connection_that_refuses_is_left_and_the_read_runs_on_its_own(env, monkeypatch):
    sai, notes, socket, asked = shared_ssh(env, monkeypatch, refuse=True)
    assert sai.host_questions("h1") == []  # answered all the same, on a connection of its own
    lines = calls(notes, "ssh")
    assert len(lines) == 2 and "ControlPath" in lines[0] and "ControlPath" not in lines[1]
    assert asked["stop"] == ["stop"]  # the master takes no new sessions; the next read starts a new one
    sai.host_questions("h1")
    assert asked["ensure"] == 2


def make_lazy(monkeypatch, **kw):
    from sandbox_ai_app import terminal
    from sandbox_ai_app.ui import MainWindow
    monkeypatch.setattr(terminal, "available", lambda web=None: (False, "not in this test"))
    w = MainWindow(backend.SandboxAI(), check_updates=False, approvals_interval=0, lazy=True, **kw)
    if w.tray is not None:
        w.tray.hide()
        w.tray = None
    return w


def window_facts(w):
    """What must be the same in a window made at once and in a lazy one once every page is made."""
    from sandbox_ai_app.ui import PAGES
    tips = {key: sorted(button.tip_id for button in buttons) for key, buttons in w.help_buttons.items()}
    kept = sorted(view.state_key for view in w.findChildren(QtWidgets.QAbstractItemView)
                  if getattr(view, "state_key", None))
    frames = [type(w.pages.widget(index)).__name__ for index in range(w.pages.count())]
    shown = [w.pages.frame_of(w.built(key)) is not None for key, _ in PAGES]
    return tips, kept, frames, shown, sorted(w.help_missing)


def test_a_lazy_window_makes_only_its_first_pages_and_the_others_when_shown(qapp, env, monkeypatch):
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    from sandbox_ai_app import ui
    w = make_lazy(monkeypatch)
    try:
        built = [key for key, _ in ui.PAGES if w.built(key) is not None]
        assert sorted(built) == sorted(ui.FIRST_PAGES), built
        assert w.pages.count() == len(ui.PAGES)  # every page has its place (an empty one until it is made)
        assert w.current_page() == "launch" and w.built("kits") is None
        w.show_page("kits")  # shown: made, in its place, linked
        assert w.built("kits") is not None and w.pages.currentWidget() is w.kits and w.current_page() == "kits"
        assert w.pages.frame_of(w.kits) is not None and "kits" in w.help_buttons
        assert getattr(w.kits.table, "state_key", None) == "kits"
        assert w.built("images") is None
        w.images.filter.setText("x")  # asked for by name: made at once
        assert w.built("images") is not None and w.state.section("texts").get("images-filter") == "x"
    finally:
        close(w)


def test_a_lazy_window_ends_as_one_made_at_once(qapp, env, monkeypatch):
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    from test_projects import make_window
    eager = make_window(monkeypatch)
    try:
        facts = window_facts(eager)
    finally:
        close(eager)
    w = make_lazy(monkeypatch)
    try:
        w.build_pages()
        assert window_facts(w) == facts
        assert w.help_missing == []
        # The links between pages made at different times: a host removed on the Hosts page leaves the others.
        w.sandboxes.remote_names = ["gone"]
        w.sandboxes.remote["gone"] = []
        w.hosts.host_removed.emit("gone")
        assert "gone" not in w.sandboxes.remote
    finally:
        close(w)


def test_a_page_made_later_shows_the_current_project(qapp, env, monkeypatch):
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    folder = project(env, "p")
    w = make_lazy(monkeypatch)
    try:
        assert w.set_project(str(folder))
        assert w.built("kits") is None
        assert w.kits.project == os.path.realpath(folder)
    finally:
        close(w)


def test_the_other_pages_are_made_in_idle_time_and_never_under_the_hand(qapp, env, monkeypatch):
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    from sandbox_ai_app import ui
    monkeypatch.setattr(ui, "LAZY_START", 0)
    monkeypatch.setattr(ui, "LAZY_STEP", 1)
    monkeypatch.setattr(ui, "LAZY_BUSY", 100.0)  # (the reads of this test's pages never hold the build up)
    w = make_lazy(monkeypatch)
    try:
        w.resize(1200, 800)
        w.show()
        deadline = time.monotonic() + 5
        while w.idle_build is None and time.monotonic() < deadline:  # the first paint starts it
            qapp.processEvents()
            time.sleep(0.01)
        assert w.idle_build is not None
        # Someone works with the window: nothing is made meanwhile.
        before = [key for key, _ in ui.PAGES if w.built(key) is not None]
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            if getattr(w, "input_watch", None) is not None:
                w.input_watch.last = time.monotonic()
            qapp.processEvents()
            time.sleep(0.005)
        assert [key for key, _ in ui.PAGES if w.built(key) is not None] == before
        deadline = time.monotonic() + 30
        while any(w.built(key) is None for key, _ in ui.PAGES) and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.005)
        assert all(w.built(key) is not None for key, _ in ui.PAGES)
        assert w.input_watch.app is None  # the watch of the mouse and keyboard ends with the last page
        assert w.help_missing == []
    finally:
        close(w)


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


def settle(qapp, seconds=0.3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)


def test_a_closed_window_goes_entirely(qapp, env, monkeypatch):
    """CI killed the app's tests (exit 137, out of memory): every window the tests made stayed - its pages, its backend
    and the sandbox-ai modules that backend had loaded, about 15 MB each. A QObject of Python's own (the project bar's
    list of projects) had its signal connected to a lambda that referred back to its owner: held in C++, a cycle that
    Python's collector cannot see; and caches kept every backend. Now a window shut down, closed and deleted leaves
    nothing behind once the collector has run (the tests' conftest sweeps after every test)."""
    import gc
    import weakref
    from conftest import sweep_widgets
    from test_projects import make_window
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    before = set(qapp.topLevelWidgets())
    backends, objects = [], []
    for _ in range(4):
        w = make_window(monkeypatch)
        w.show()
        for key in ("sandboxes", "kits", "hosts", "credentials"):
            w.show_page(key, raise_window=False)
            qapp.processEvents()
        backends.append(weakref.ref(w.sai))
        close(w)
        del w
        widgets.ThreadCall.wait_all(30)  # what still read when it closed reports (and is let go of) first
        settle(qapp)
        sweep_widgets(before)
        gc.collect()
        objects.append(len(gc.get_objects()))
    assert [ref() for ref in backends[:-1]] == [None] * 3, "a closed window's backend is still alive"
    assert objects[-1] - objects[1] < 3000, objects
    assert not [w for w in qapp.topLevelWidgets() if w not in before and w.parentWidget() is None]


def test_the_caches_keep_no_backend_alive(env):
    import gc
    import weakref
    from sandbox_ai_app import board
    sai = backend.SandboxAI()
    board._options(sai)
    backend.option_arity(sai.launcher)
    ref = weakref.ref(sai)
    del sai
    gc.collect()
    assert ref() is None

