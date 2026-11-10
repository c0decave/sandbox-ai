"""Soak: the real window (lazy, as the app makes it, polling at its normal intervals) used for a long time, and what
it costs measured every minute - memory, Python objects, widgets and Qt objects, timers, threads, open files,
child processes, the event loop's longest stall, the state file, the log window - so that what grows with time or
with use shows as a line in a table.

Not part of the suite (the name is not test_*.py); run it explicitly, e.g.
    SOAK_MINUTES=25 python -m pytest tests/soak_app.py -s
SOAK_MINUTES (default 25) is the wall time of the use; SOAK_SKIP (e.g. "terminal,theme") leaves those uses out,
SOAK_WEIGHTS (e.g. "terminal=3") draws them more or less often, SOAK_SEED chooses another sequence;
SOAK_TRACEMALLOC=1 also prints where the memory allocated
after the first minutes was allocated (slows the app down about twofold). What is used, against fake records (no
sandbox runs): page switches; the Board of several projects (parts added, changed, removed with their [x], undone
and redone, Apply declined and now and then accepted, the projects switched, their tabs closed and opened again);
questions of fake runs that appear, are answered (on the page and from their notification) or expire, runs and
sessions that start and end; the log window at Debug with thousands of entries; themes; the Terminals page (tabs
when QtWebEngine works here); the window hidden to the tray and shown again."""
from collections import Counter
import faulthandler
import gc
import json
import os
from pathlib import Path
import random
import shutil
import sys
import threading
import time
import traceback

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6 import QtCore  # noqa: E402
from sandbox_ai_app import applog, backend, widgets  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")

MINUTES = float(os.environ.get("SOAK_MINUTES", "25"))
SAMPLE = float(os.environ.get("SOAK_SAMPLE", "60"))  # seconds between two rows of the table
STALL = 0.2  # seconds without a turn of the event loop that count as a stall
BEAT = 0.025  # the heartbeat's interval
THEMES = ("dark", "light", "hacker", "amber", "system", "unicorn", "mac")
PAGE_KEYS = ("launch", "board", "sandboxes", "approvals", "terminals", "connections", "activity", "statistics",
             "files", "kits", "mcp", "llm", "team", "images", "credentials", "system", "hosts", "help")


def out(text=""):
    sys.__stdout__.write(text + "\n")
    sys.__stdout__.flush()


class FakeTray:
    """The tray icon (offscreen there is no system tray): the window hides to it and is shown again."""
    def __init__(self):
        self.messages = 0

    def showMessage(self, *args):  # noqa: N802
        self.messages += 1

    def setToolTip(self, text):  # noqa: N802
        pass

    def hide(self):
        pass


def rss_kb():
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1])
    return 0


def trimmed_rss_kb():
    """RSS once the C library has given back the memory it keeps free (what is really held, not what is cached)."""
    try:
        import ctypes
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):
        pass
    return rss_kb()


def children():
    """{pid: command line} of this process's children (and theirs)."""
    me, parents, commands = os.getpid(), {}, {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            stat = Path(f"/proc/{entry}/stat").read_text()
            parents[int(entry)] = int(stat.rsplit(")", 1)[1].split()[1])
            commands[int(entry)] = Path(f"/proc/{entry}/cmdline").read_bytes().replace(b"\0", b" ")[:100].decode(
                errors="replace")
        except (OSError, ValueError, IndexError):
            continue
    found, frontier = {}, {me}
    while frontier:
        level = {pid for pid, parent in parents.items() if parent in frontier and pid not in found}
        found.update({pid: commands.get(pid, "") for pid in level})
        frontier = level
    return found


def type_counts():
    return Counter(type(o).__qualname__ for o in gc.get_objects())


class Soak:
    def __init__(self, qapp, env, window):
        self.qapp, self.env, self.w = qapp, env, window
        self.random = random.Random(int(os.environ.get("SOAK_SEED", "7")))
        # What was done, line by line as it is done (unbuffered): after a crash of the process, its last lines say what
        # the window was doing.
        self.trail = open(env / "actions.log", "w", buffering=1)  # noqa: SIM115
        out(f"actions: {env / 'actions.log'}")
        self.loop = QtCore.QEventLoop()
        self.rows, self.errors, self.warnings, self.harness = [], [], Counter(), []
        self.actions, self.action_times = Counter(), {}
        self.beat_at = time.monotonic()
        self.in_action = None
        self.minute_stall, self.minute_action = (0.0, ""), (0.0, "")
        self.stalls = []  # (seconds, what ran, the GUI thread's stack while it stalled)
        self.stack_seen = None
        self.projects = []
        self.runs, self.sessions, self.questions = {}, [], {}
        self.serial = 0
        self.terminal_tabs = []
        self.hidden_since = None
        self.log_lines = 0
        self.started = time.monotonic()
        self.baseline_types = None
        self.baseline_widgets = None
        self.baseline_objects = None
        self.trace_start = None
        applog.add_listener(self.logged)

    # -- what is measured --

    def logged(self, entry):
        level = entry.get("level")
        if level in ("ERROR", "FATAL"):
            self.errors.append(f"{entry.get('where')}: {str(entry.get('message'))[:2000]}")
        elif level == "WARNING":
            self.warnings[f"{entry.get('where')}: {str(entry.get('message'))[:160]}"] += 1

    def beat(self):
        now = time.monotonic()
        gap = now - self.beat_at
        self.beat_at = now
        if gap > STALL:
            what = self.last_action
            self.stalls.append((gap, what, self.stack_seen))
            if gap > self.minute_stall[0]:
                self.minute_stall = (gap, what)
        self.stack_seen = None

    def watchdog(self):
        """In a thread: the GUI thread's stack while the event loop has not turned for STALL seconds (once a stall)."""
        main = threading.main_thread().ident
        while not self.done:
            time.sleep(0.05)
            if time.monotonic() - self.beat_at > STALL * 0.75 and self.stack_seen is None:
                frame = sys._current_frames().get(main)
                if frame is not None:
                    stack = traceback.extract_stack(frame)
                    mine = [f for f in stack if "sandbox_ai_app" in f.filename or "soak_app" in f.filename]
                    self.stack_seen = " <- ".join(f"{Path(f.filename).stem}:{f.lineno}:{f.name}"
                                                  for f in reversed((mine or stack)[-8:]))

    def qobjects(self):
        top = [w for w in QtWidgets.QApplication.topLevelWidgets()]
        objects = Counter()
        for widget in top:
            objects[type(widget).__name__] += 1
            for child in widget.findChildren(QtCore.QObject):
                objects[type(child).__name__] += 1
        for child in self.qapp.children():
            objects["app:" + type(child).__name__] += 1
        return objects

    def sample(self):
        gc.collect()
        objects = self.qobjects()
        state = self.w.state.path
        hub = self.w.log_panel.hub
        kids = children()
        terminals = sum(1 for c in kids.values() if "while :" in c)
        row = {
            "min": round((time.monotonic() - self.started) / 60, 1),
            "rss_mb": round(rss_kb() / 1024, 1),
            "trim_mb": round(trimmed_rss_kb() / 1024, 1),
            "py_mb": round(sys.getallocatedblocks() * 64 / 1048576, 1),  # (blocks of Python's allocator, ~64 B)
            "py_objs": len(gc.get_objects()),
            "widgets": len(QtWidgets.QApplication.allWidgets()),
            "qobjs": sum(objects.values()),
            "timers": objects.get("QTimer", 0),
            "toplevel": len(QtWidgets.QApplication.topLevelWidgets()),
            "threads": threading.active_count(),
            "tasks": len(os.listdir("/proc/self/task")),
            "fds": len(os.listdir("/proc/self/fd")),
            "kids": len(kids) - terminals,
            "term_kids": terminals,
            "stall_ms": round(self.minute_stall[0] * 1000),
            "stall_in": self.minute_stall[1],
            "act_ms": round(self.minute_action[0] * 1000),
            "act": self.minute_action[1],
            "state_kb": round(state.stat().st_size / 1024, 1) if state.exists() else 0,
            "log": len(hub.entries),
            "blocks": self.w.log_panel.text.document().blockCount(),
            "errors": len(self.errors),
            "toasts": len(self.w.notifier.toasts),
            "actions": sum(self.actions.values()),
        }
        self.rows.append(row)
        self.minute_stall, self.minute_action = (0.0, ""), (0.0, "")
        out(" ".join(f"{key}={value}" for key, value in row.items()))
        if self.baseline_types is None and row["min"] >= 2:
            self.baseline_types, self.baseline_widgets = type_counts(), Counter(
                type(w).__name__ for w in QtWidgets.QApplication.allWidgets())
            self.baseline_objects = objects
            if os.environ.get("SOAK_TRACEMALLOC"):
                import tracemalloc
                tracemalloc.start(12)
                self.trace_start = tracemalloc.take_snapshot()
        widgets.SHOWN.clear()  # (what the dialogs said: collected by the tests' auto answers, not by the app)

    # -- the use --

    def setup(self):
        for name in ("alpha", "beta", "gamma", "delta"):
            folder = self.env / name
            folder.mkdir(exist_ok=True)
            (folder / ".sandbox-ai.json").write_text(json.dumps({"version": 1, "agent": "claude", "options": []}))
            self.projects.append(folder)
        self.w.tray = self.w.tray or FakeTray()
        for project in self.projects[:2]:
            self.start_run(project)

    def start_run(self, workspace=None):
        workspace = workspace or self.random.choice(self.projects)
        self.serial += 1
        name = f"claude-soak{self.serial}"
        directory = self.env / "runtime" / "sandbox-ai" / "runs" / name
        (directory / "approvals").mkdir(parents=True)
        (directory / "run.json").write_text(json.dumps({
            "run": name, "agent": "claude", "backend": "bwrap", "workspace": str(workspace), "pid": os.getpid(),
            "session": None, "session_dir": str(self.env / "s"), "started": "2026-11-03T10:00:00Z", "proxy": True}))
        self.runs[name] = directory

    def end_run(self):
        if len(self.runs) <= 1:
            return
        name = self.random.choice(sorted(self.runs))
        shutil.rmtree(self.runs.pop(name), ignore_errors=True)
        self.questions = {k: v for k, v in self.questions.items() if v[0] != name}

    def session(self):
        root = self.env / "state" / "sandbox-ai" / "sessions"
        if len(self.sessions) < 4 and (not self.sessions or self.random.random() < 0.5):
            self.serial += 1
            folder = root / f"work{self.serial}"
            (folder / "home").mkdir(parents=True)
            workspace = self.random.choice(self.projects)
            (folder / "session.json").write_text(json.dumps({
                "agent": "codex", "options": [], "backend": "podman", "workspace": str(workspace),
                "cwd": str(workspace), "created": "2026-11-02T09:00:00Z"}))
            self.sessions.append(folder)
        else:
            shutil.rmtree(self.sessions.pop(0), ignore_errors=True)

    def ask(self):
        if not self.runs:
            return
        name = self.random.choice(sorted(self.runs))
        self.serial += 1
        ident = f"1700000000-{self.serial}-1"
        asked = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        kind = self.random.choice(("dns", "connect", "browser"))
        question = {"dns": {"kind": "dns", "name": f"host{self.serial}.example"},
                    "connect": {"kind": "connect", "destination": f"192.0.2.{self.serial % 250}:443",
                                "address": f"192.0.2.{self.serial % 250}", "port": 443},
                    "browser": {"kind": "browser", "url": f"https://login.example/device?code={self.serial}"}}[kind]
        (self.runs[name] / "approvals" / f"{ident}.json").write_text(json.dumps(
            {"id": ident, "time": asked, "timeout": 60, **question}))
        self.questions[ident] = (name, time.monotonic())

    def answer(self):
        page = self.w.approvals
        open_keys = list(page.questions)
        if not open_keys:
            return
        key = self.random.choice(open_keys)
        if self.random.random() < 0.15:
            return  # left to expire
        if self.random.random() < 0.3:  # from its notification
            self.w.answer_question(key, self.random.choice(("allow", "deny")), self.random.choice(("once", "run")))
        else:
            row = widgets.find_row(page.open, key)
            if row is None or row < 0:
                return
            page.open.selectRow(row)
            page.answer(self.random.choice(("allow", "deny")))

    def supervise(self):
        """What a sandbox's supervisor does with an answer: its .done."""
        for directory in list(self.runs.values()):
            for answer in (directory / "approvals").glob("*.answer"):
                done = answer.with_suffix(".done")
                if not done.exists():
                    try:
                        data = json.loads(answer.read_text())
                    except (OSError, ValueError):
                        continue
                    done.write_text(json.dumps({**data, "reason": "answered"}))

    def page(self):
        self.w.show_page(self.random.choice(PAGE_KEYS))

    def project(self):
        if self.random.random() < 0.2 and self.w.tabs.count() > 2:
            self.w.close_tab(self.random.randrange(self.w.tabs.count()))
        else:
            self.w.set_project(self.random.choice(self.projects))

    def board(self):
        w = self.w
        if w.current_page() != "board":
            w.show_page("board")
        board = w.board
        choice = self.random.random()
        limits = board.board.first("limits")
        if choice < 0.25:
            if limits is None:
                board.add_node("limits")
            else:
                board.set_prop(limits.id, "--memory", f"{self.random.randint(1, 16)}g")
        elif choice < 0.4:
            board.add_node("devices") if board.board.first("devices") is None else board.undo_group.undo()
        elif choice < 0.55:
            board.undo_group.undo()
        elif choice < 0.7:
            board.undo_group.redo()
        elif choice < 0.8:
            node = next((n for n in (board.board.first(k) for k in ("devices", "limits")) if n is not None), None)
            if node is not None:
                board.ask_remove(node.id)  # its [x]
        elif choice < 0.95:
            widgets.AUTO_ANSWER = False  # Apply, declined
            try:
                board.apply()
            finally:
                widgets.AUTO_ANSWER = True
        else:
            board.apply()  # written and trusted (in a thread)

    def log(self):
        panel = self.w.log_panel
        if panel.chosen() != "DEBUG":
            panel.level.setCurrentIndex(panel.level.findData("DEBUG"))
        for _ in range(self.random.randint(5, 40)):
            self.log_lines += 1
            level = "WARNING" if self.log_lines % 50 == 0 else "INFO"
            applog.log(level, "soak", f"entry {self.log_lines}: " + "x" * self.random.randint(10, 120))
        if self.random.random() < 0.05:
            panel.filter.setText(self.random.choice(("", "entry 1", "soak", "")))
        if self.random.random() < 0.1:
            if self.w.log_fold.is_folded():
                self.w.log_fold.unfold()
            else:
                self.w.log_fold.fold()

    def theme(self):
        system = self.w.system
        system.theme.setCurrentIndex(system.theme.findData(self.random.choice(THEMES)))

    def tray(self):
        if self.hidden_since is None:
            self.w.close()  # hidden to the tray
            self.hidden_since = time.monotonic()
        elif time.monotonic() - self.hidden_since > 20:
            self.w.show()
            self.hidden_since = None

    def terminal(self):
        from sandbox_ai_app import terminal
        page = self.w.terminals
        if not terminal.available(page.web)[0]:
            return
        if page.tabs.count() >= 2:
            page.close_tab(0, ask=False)
        else:
            self.serial += 1
            page.open(["sh", "-c", f"while :; do echo soak {self.serial} $(date); sleep 0.5; done"],
                      f"soak {self.serial}", done=lambda view: self.trail.write(
                          f"{time.monotonic() - self.started:9.3f} tab made: {view is not None}\n"))

    def schedule(self):
        """(action, weight) - one is drawn every ~350 ms."""
        weights = dict(item.split("=") for item in os.environ.get("SOAK_WEIGHTS", "").split(",") if "=" in item)
        return tuple((action, float(weights.get(action.__name__, weight))) for action, weight in (
            (self.page, 10), (self.board, 12), (self.project, 3), (self.ask, 4), (self.answer, 5), (self.supervise, 4),
            (self.log, 8), (self.theme, 0.4), (self.tray, 0.15), (self.session, 1.5), (self.start_run, 0.3),
            (self.end_run, 0.3), (self.terminal, 0.6)))

    def act(self):
        skipped = set(filter(None, os.environ.get("SOAK_SKIP", "").split(",")))  # e.g. terminal,theme: left out
        actions, weights = zip(*((a, w) for a, w in self.schedule() if a.__name__ not in skipped))
        action = self.random.choices(actions, weights)[0]
        name = action.__name__
        if self.hidden_since is not None and name in ("page", "board", "theme", "terminal", "project"):
            name, action = "tray", self.tray  # (hidden: only what goes on in the background, and showing it again)
        self.last_action = name
        begin = time.monotonic()
        w = self.w
        self.trail.write(f"{begin - self.started:9.3f} {name} page={w.current_page()} shown={w.isVisible()} "
                         f"focus={type(QtWidgets.QApplication.focusWidget()).__name__}\n")
        try:
            action()
        except Exception:  # noqa: BLE001 - the harness's own mistake or the app's: both reported
            self.harness.append(f"{name}: {traceback.format_exc()}")
        took = time.monotonic() - begin
        self.actions[name] += 1
        self.action_times[name] = max(self.action_times.get(name, 0.0), took)
        if took > self.minute_action[0]:
            self.minute_action = (took, name)
        self.beat_at = time.monotonic()  # (an action's own time is measured above, not as a stall)
        self.stack_seen = None
        self.last_action = f"after {name}"
        self.action_timer.start(self.random.randint(200, 500))

    def run(self):
        self.done = False
        self.last_action = "start"
        self.setup()
        watchdog = threading.Thread(target=self.watchdog, daemon=True)
        watchdog.start()
        heart = QtCore.QTimer()
        heart.setInterval(int(BEAT * 1000))
        heart.timeout.connect(self.beat)
        heart.start()
        sampler = QtCore.QTimer()
        sampler.setInterval(int(SAMPLE * 1000))
        sampler.timeout.connect(self.sample)
        sampler.start()
        self.action_timer = QtCore.QTimer()
        self.action_timer.setSingleShot(True)
        self.action_timer.timeout.connect(self.act)
        self.action_timer.start(1000)
        QtCore.QTimer.singleShot(int(MINUTES * 60 * 1000), self.loop.quit)
        self.sample()
        self.loop.exec()
        self.action_timer.stop()
        # The last minute: nothing done, only the polls (what is left over settles).
        self.last_action = "idle"
        QtCore.QTimer.singleShot(int(min(SAMPLE, 60) * 1000), self.loop.quit)
        self.loop.exec()
        heart.stop()
        sampler.stop()
        self.sample()
        self.done = True

    def report(self):
        out("\n== soak table ==")
        if not self.rows:
            return
        keys = [k for k in self.rows[0] if k not in ("stall_in", "act")]
        out(" ".join(f"{k:>8}" for k in keys) + "  stall_in / act")
        for row in self.rows:
            out(" ".join(f"{row[k]!s:>8}" for k in keys) + f"  {row['stall_in']} / {row['act']}")
        out("\n== actions (count, longest ms) ==")
        for name, count in self.actions.most_common():
            out(f"  {name:12} {count:6} {round(self.action_times[name] * 1000):6}")
        out(f"\n== stalls > {int(STALL * 1000)} ms outside the actions: {len(self.stalls)} ==")
        for gap, what, stack in sorted(self.stalls, key=lambda s: -s[0])[:25]:
            out(f"  {round(gap * 1000):6} ms {what}: {stack}")
        stacks = Counter(stack for _, _, stack in self.stalls)
        out("\n== stall stacks (count) ==")
        for stack, count in stacks.most_common(15):
            out(f"  {count:4} {stack}")
        if self.baseline_types is not None:
            gc.collect()
            now = type_counts()
            out("\n== Python objects by type: growth since minute 2 ==")
            for name, grown in sorted(((n, now[n] - self.baseline_types.get(n, 0)) for n in now),
                                      key=lambda x: -x[1])[:30]:
                if grown > 0:
                    out(f"  {grown:8} {name} (now {now[name]})")
            out("\n== widgets by class: growth since minute 2 ==")
            current = Counter(type(w).__name__ for w in QtWidgets.QApplication.allWidgets())
            for name, grown in sorted(((n, current[n] - self.baseline_widgets.get(n, 0)) for n in current),
                                      key=lambda x: -x[1])[:20]:
                if grown:
                    out(f"  {grown:8} {name} (now {current[name]})")
            out("\n== QObjects by class (window trees): growth since minute 2 ==")
            objects = self.qobjects()
            for name, grown in sorted(((n, objects[n] - self.baseline_objects.get(n, 0)) for n in objects),
                                      key=lambda x: -x[1])[:20]:
                if grown:
                    out(f"  {grown:8} {name} (now {objects[name]})")
        if self.trace_start is not None:
            import tracemalloc
            snapshot = tracemalloc.take_snapshot()
            out("\n== tracemalloc: allocated since minute 2, by line ==")
            for stat in snapshot.compare_to(self.trace_start, "traceback")[:20]:
                out(f"  {stat.size_diff / 1024:9.1f} KiB {stat.count_diff:7} blocks")
                for line in stat.traceback.format()[-12:]:
                    out(f"      {line}")
            tracemalloc.stop()
        out(f"\n== threads now: {sorted(Counter(t.name.rstrip('0123456789') for t in threading.enumerate()).items())}")
        out(f"== children now: {children()}")
        out(f"== errors in the log ({len(self.errors)}) ==")
        for text in Counter(self.errors).most_common(15):
            out(f"  {text[1]}x {text[0]}")
        out(f"== warnings in the log (other than the soak's own) ==")
        for text, count in self.warnings.most_common(20):
            if not text.startswith("soak:") and "This plugin does not support" not in text:  # (offscreen)
                out(f"  {count}x {text}")
        out(f"== harness exceptions ({len(self.harness)}) ==")
        for text in self.harness[:15]:
            out("  " + text.replace("\n", "\n  "))


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    return tmp_path


def web_folder(env):
    """The terminal's page with xterm.js: the checkout's own when it is vendored (or SOAK_XTERM, a vendor folder of
    another checkout), else the tests' stand-in for it."""
    from sandbox_ai_app import terminal
    vendor = Path(os.environ.get("SOAK_XTERM") or terminal.WEB / "vendor")
    if not (vendor / "xterm.js").exists():
        vendor = Path(__file__).resolve().parent / "fake_xterm"
    web = env / "web"
    (web / "vendor").mkdir(parents=True)
    for name in ("terminal.html", "terminal.js"):
        shutil.copy(terminal.WEB / name, web / name)
    for name in ("xterm.js", "addon-fit.js", "xterm.css"):
        shutil.copy(vendor / name, web / "vendor" / name)
    return web


# Programs that open something on the human's desktop (a page in the browser, a notification): never run by the
# soak. Its answers allow pages of fake browser questions, which sandbox-ai opens with xdg-open - and in a sandbox
# of sandbox-ai xdg-open and $BROWSER forward every page to the human's desktop as a question.
OPENERS = ("xdg-open", "sensible-browser", "x-www-browser", "www-browser", "gio", "open", "kde-open", "kde-open5",
           "gnome-open", "exo-open", "firefox", "chromium", "chromium-browser", "google-chrome", "notify-send")


@pytest.fixture
def opened(env, monkeypatch):
    """Nothing is opened for real: stand-ins for every opener, first on PATH (also for sandbox-ai's commands the app
    starts) and as $BROWSER, record what would have been opened; in this process QDesktopServices.openUrl, webbrowser
    and any command line of an opener (by any path) record it too. Returns the list of what was opened."""
    import subprocess
    import webbrowser
    from PySide6 import QtGui
    notes, fake = env / "opened.txt", env / "openers"
    fake.mkdir()
    for name in OPENERS:
        (fake / name).write_text(f"#!/bin/sh\necho \"$(basename \"$0\") $*\" >> '{notes}'\nexit 0\n")
        (fake / name).chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("BROWSER", str(fake / "xdg-open"))
    for name in OPENERS:
        assert shutil.which(name) in (None, str(fake / name)), f"{name} would be found elsewhere: {shutil.which(name)}"
    seen = []
    monkeypatch.setattr(QtGui.QDesktopServices, "openUrl", staticmethod(lambda url: seen.append(("qt", str(
        url.toString() if hasattr(url, "toString") else url))) or True))
    for name in ("open", "open_new", "open_new_tab"):
        monkeypatch.setattr(webbrowser, name, lambda url, *a, **k: seen.append(("webbrowser", url)) or True)
    original = subprocess.Popen.__init__

    def guarded(self, args, *a, **k):
        argv = [args] if isinstance(args, (str, bytes, os.PathLike)) else list(args)
        if argv and os.path.basename(str(argv[0])).split()[0] in OPENERS:
            seen.append(("process", " ".join(map(str, argv))))
            args = ["true"]
        return original(self, args, *a, **k)
    monkeypatch.setattr(subprocess.Popen, "__init__", guarded)
    yield seen
    if notes.exists():
        seen += [("program", line) for line in notes.read_text().splitlines()]


def test_soak(qapp, env, monkeypatch, opened):
    from sandbox_ai_app import terminal
    from sandbox_ai_app.ui import MainWindow
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    monkeypatch.setattr("sandbox_ai_app.ui.QProcess.startDetached", lambda program, args: True)
    for name in ("question", "information", "warning", "critical"):
        monkeypatch.setattr(QtWidgets.QMessageBox, name, lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Yes)
    # The app's own error output, as main() installs it (app.log under this test's XDG_STATE_HOME); what it writes
    # to standard error goes to a file (thousands of log lines).
    stderr = open(env / "stderr.log", "w")  # noqa: SIM115
    monkeypatch.setattr(sys, "stderr", stderr)
    hooks = (sys.excepthook, threading.excepthook, sys.unraisablehook)
    applog.install()
    faulthandler.enable(file=sys.__stderr__, all_threads=True)
    w = MainWindow(backend.SandboxAI(), check_updates=False, lazy=True)
    web = web_folder(env)
    w.page_makers["terminals"] = lambda: terminal.TerminalsPage(web, sai=w.sai, in_terminal=w.in_terminal)
    w.sai.in_terminal = lambda argv, title="": ["term", *argv]
    w.resize(1240, 840)
    w.show()
    soak = Soak(qapp, env, w)
    try:
        soak.run()
    finally:
        soak.done = True
        soak.report()
        applog.remove_listener(soak.logged)
        w.shutdown()
        w.hide()
        sys.excepthook, threading.excepthook, sys.unraisablehook = hooks
    first, last = soak.rows[1] if len(soak.rows) > 1 else soak.rows[0], soak.rows[-1]
    out(f"\nRSS {first['rss_mb']} -> {last['rss_mb']} MB, Python objects {first['py_objs']} -> {last['py_objs']}, "
        f"widgets {first['widgets']} -> {last['widgets']}, threads {first['threads']} -> {last['threads']}, "
        f"fds {first['fds']} -> {last['fds']}")
    out(f"opened (recorded, not opened): {len(opened)} in this process; the stand-ins' notes: "
        f"{len((env / 'opened.txt').read_text().splitlines()) if (env / 'opened.txt').exists() else 0}")
    assert not soak.harness, soak.harness[:3]
    assert not soak.errors, soak.errors[:3]
