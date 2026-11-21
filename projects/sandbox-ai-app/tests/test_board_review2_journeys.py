"""Board review 2: the Board walked at random, in the app's main window (docs/reviews/board-review-2.md).

A seeded walk of what a human does with a board - parts added from the palette, dropped, pasted, cut, connected and
removed, their properties typed into and clicked, Undo and Redo, presets, the gallery, the wizard, Export, Import and
Save as preset (each dialog used at random, then accepted or cancelled), project tabs, Clone, Change folder, Apply and
Start, the panels floated, folded and closed, themes, the other pages in between, and a host that does not answer
chosen and left again. After every step: no slot raised, the scene shows exactly the model's parts and connections,
the part inspected exists, and nothing was run on the host from the GUI thread.

The walk found R2-10 (Change folder and the tabs: a kept board's Undo deleted) and R2-11; MONKEY_SEEDS=5,6,... walks
other seeds, MONKEY_STEPS their length."""
import json
import os
import random
import sys
import threading
import time
import traceback

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import QPointF, Qt, QTimer  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QCheckBox, QComboBox, QDialog, QLineEdit, QListWidget, QMenu, QPlainTextEdit, QRadioButton, QWizard,
)

from sandbox_ai_app import backend, board as B, board_view as BV, i18n, theme, widgets  # noqa: E402

from test_projects import close, env, make_window, window  # noqa: E402,F401 - shared fixtures

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")

SEEDS = [int(seed) for seed in os.environ.get("MONKEY_SEEDS", "3,8").split(",")]
STEPS = int(os.environ.get("MONKEY_STEPS", "220"))
UNREACHABLE = "ssh: connect to host lab.example port 22: Connection timed out"


def pump(qapp, rounds=2):
    for _ in range(rounds):
        qapp.processEvents()


def sound(page):
    """What must hold after every step, or why not."""
    ids = {node.id for node in page.board.nodes}
    if set(page.items) != ids:
        return f"the scene shows {sorted(page.items)}, the model has {sorted(ids)}"
    if sorted(edge.ids for edge in page.edges) != sorted(page.board.edges):
        return "the connections shown are not the model's"
    if not all(a in ids and b in ids for a, b in page.board.edges):
        return "a connection to a part that is gone"
    if page.inspected is not None and page.inspected not in ids:
        return f"the part inspected ({page.inspected}) is gone"
    for kind, known in B.KINDS.items():
        count = len(page.board.of_kind(kind))
        if (known.required and count != 1) or (known.single and count > 1):
            return f"{count} parts of the kind {kind}"
    stacks = [session["stack"] for session in page.sessions.values()]
    if not all(BV.alive(stack) for stack in stacks) or not BV.alive(page.undo):
        return "a board whose Undo was deleted"
    return None


class Walk:
    """The steps of the walk (each returns what it did, or None when it did not apply)."""

    def __init__(self, window, env, rng):  # noqa: F811
        self.window, self.page, self.rng, self.qapp = window, window.board, rng, QApplication.instance()
        self.folders = []
        for name in ("a", "b", "c"):
            (env / name).mkdir()
            self.folders.append(str(env / name))
        (env / "b" / ".sandbox-ai.json").write_text(json.dumps(
            {"version": 1, "agent": "codex", "options": ["--allow-pypi", "--memory", "2g"]}))
        from sandbox_ai_app import presets
        self.presets = [preset.id for preset in presets.catalogue(window.sai, "local").presets][:40]
        self.dialogs = {}

    # -- what answers a dialog or a menu that opens (exec waits for it) --

    def answer(self):
        rng = self.rng
        for widget in QApplication.topLevelWidgets():
            try:
                if not widget.isVisible():
                    continue
                if isinstance(widget, QMenu):
                    actions = [a for a in widget.actions() if a.isEnabled() and not a.isSeparator() and not a.menu()]
                    widget.close()
                    if actions and rng.random() < 0.3:
                        rng.choice(actions).trigger()
                elif isinstance(widget, QDialog):
                    self.dialogs[type(widget).__name__] = self.dialogs.get(type(widget).__name__, 0) + 1
                    for _ in range(rng.randint(0, 4)):
                        fields = [w for w in widget.findChildren(QtWidgets.QWidget) if w.isEnabled()
                                  and w.isVisibleTo(widget) and isinstance(
                                      w, (QCheckBox, QComboBox, QLineEdit, QRadioButton, QListWidget))]
                        if not fields:
                            break
                        field = rng.choice(fields)
                        if isinstance(field, (QCheckBox, QRadioButton)):
                            field.click()
                        elif isinstance(field, QComboBox) and field.count():
                            field.setCurrentIndex(rng.randrange(field.count()))
                        elif isinstance(field, QLineEdit) and not field.isReadOnly():
                            field.setText(rng.choice(["", "x", "/tmp/zz", "name-1"]))
                        elif isinstance(field, QListWidget) and field.count():
                            field.setCurrentRow(rng.randrange(field.count()))
                    if isinstance(widget, QWizard) and rng.random() < 0.7:
                        widget.next() if rng.random() < 0.8 else widget.back()
                        if rng.random() < 0.8:
                            continue
                    widget.accept() if rng.random() < 0.5 else widget.reject()
            except RuntimeError:  # gone meanwhile
                pass

    # -- the steps --

    def add(self):
        kind = self.rng.choice([k for k in B.KINDS if not B.KINDS[k].required])
        self.page.add_node(kind, guided=self.rng.random() < 0.3)
        return f"add {kind}"

    def remove(self):
        nodes = [n for n in self.page.board.nodes if not B.KINDS[n.kind].required]
        if nodes:
            node = self.rng.choice(nodes)
            self.page.remove_node(node.id)
            return f"remove {node.kind}"

    def select(self):
        node = self.rng.choice(self.page.board.nodes)
        self.page.scene.clearSelection()
        self.page.items[node.id].setSelected(True)
        if self.rng.random() < 0.6:
            self.page.show_properties(node.id)
        return f"select {node.kind}"

    def select_many(self):
        self.page.scene.clearSelection()
        nodes = self.page.board.nodes
        for node in self.rng.sample(nodes, min(len(nodes), self.rng.randint(0, 4))):
            self.page.items[node.id].setSelected(True)
        return "select several"

    def delete(self):
        self.page.delete_selected()
        return "delete the selection"

    def undo(self):
        self.page.undo_group.undo()
        return "undo"

    def redo(self):
        self.page.undo_group.redo()
        return "redo"

    def copy_paste(self):
        self.page.copy_selected()
        self.page.paste(QPointF(self.rng.randint(-200, 900), self.rng.randint(-200, 700))
                        if self.rng.random() < 0.5 else None)
        return "copy and paste"

    def cut(self):
        self.page.cut_selected()
        return "cut"

    def arrange(self):
        self.page.arrange()
        return "arrange"

    def edge(self):
        page, rng = self.page, self.rng
        if page.edges and rng.random() < 0.5:
            page.scene.clearSelection()
            rng.choice(page.edges).setSelected(True)
            page.delete_selected()
            return "delete a connection"
        a, b = rng.choice(page.board.nodes), rng.choice(page.board.nodes)
        page.connect_nodes(a.id, b.id)
        return f"connect {a.kind} and {b.kind}"

    def field(self):
        page, rng = self.page, self.rng
        pump(self.qapp)
        fields = [w for w in page.inspector_holder.findChildren(QtWidgets.QWidget)
                  if isinstance(w, (QLineEdit, QCheckBox, QComboBox, QPlainTextEdit)) and w.isEnabled()
                  and w.isVisibleTo(page.inspector_holder)]
        if not fields:
            return None
        field = rng.choice(fields)
        if isinstance(field, QCheckBox):
            field.click()
        elif isinstance(field, QComboBox):
            if field.count():
                field.setCurrentIndex(rng.randrange(field.count()))
                field.activated.emit(field.currentIndex())
        elif isinstance(field, QLineEdit):
            if field.isReadOnly():
                return None
            field.setFocus()
            field.setText(rng.choice(["", "x", "/tmp/a b", "4g", "näme", "-x", "a:b:c", "{{thing}}", "1" * 300,
                                      "a\tb"]))
            if rng.random() < 0.7:
                field.editingFinished.emit()
        else:
            if field.isReadOnly():
                return None
            field.setPlainText(rng.choice(["", "a\nb", "pypi.org, *.x.org", "-dash", "x" * 500]))
            if rng.random() < 0.5:
                page.commit_lists()
        return f"a field ({type(field).__name__})"

    def button(self):
        page = self.page
        pump(self.qapp)
        buttons = [w for w in page.inspector_holder.findChildren(QtWidgets.QAbstractButton)
                   if not isinstance(w, QCheckBox) and w.isEnabled() and w.isVisibleTo(page.inspector_holder)
                   and w.text() != "…" and "sshfs" not in w.text()]  # (not the file dialogs: no human answers them)
        if not buttons:
            return None
        button = self.rng.choice(buttons)
        text = button.text() or button.accessibleName()
        button.click()
        return f"the button {text!r}"

    def project(self):
        folder = self.rng.choice(self.folders)
        self.window.set_project(folder)
        return f"the project {os.path.basename(folder)}"

    def preset(self):
        if self.page.host() != "local":
            return None
        name = self.rng.choice(self.presets)
        self.page.new_from_preset(name)
        return f"the preset {name}"

    def theme(self):
        name = self.rng.choice(["light", "dark", "system", "hacker", "win311"])
        theme.apply(name, self.qapp)
        return f"the theme {name}"

    def other_page(self):
        key = self.rng.choice(["launch", "sandboxes", "files"])
        if key in self.window.keys:
            self.window.show_page(key, raise_window=False)
        if self.rng.random() < 0.8:
            self.window.show_page("board", raise_window=False)
        return f"the page {key}"

    def dock(self):
        window = self.window
        dock = window.docks[self.rng.choice(["inspector", "parts", "findings"])]
        what = self.rng.choice(["float", "dock", "fold", "unfold", "close", "show"])
        if what == "float":
            dock.setFloating(True)
        elif what == "dock":
            dock.setFloating(False)
        elif what == "fold":
            window.inspector_fold.fold()
        elif what == "unfold":
            window.inspector_fold.unfold()
        elif what == "close":
            dock.close()
        else:
            self.page.show_inspector()
        return f"a panel: {what}"

    def clone(self):
        if self.rng.random() < 0.5:
            self.page.clone_to_session()
            return "clone as a session"
        self.page.clone_to_project(self.rng.choice(self.folders) if self.page.host() == "local" else "/srv/clone")
        return "clone as a project"

    def change_folder(self):
        self.page.change_folder(self.rng.choice(self.folders) if self.page.host() == "local" else "/srv/other")
        return "change folder"

    def apply(self):
        self.page.apply()
        return "apply"

    def start(self):
        self.page.start(embedded=self.rng.random() < 0.5, session=self.rng.choice([None, "s1", ""]))
        return "start"

    def key(self):
        self.page.view.setFocus()
        key = self.rng.choice([Qt.Key.Key_Delete, Qt.Key.Key_Escape, Qt.Key.Key_A, Qt.Key.Key_Z, Qt.Key.Key_C,
                               Qt.Key.Key_V, Qt.Key.Key_X, Qt.Key.Key_Tab, Qt.Key.Key_Return, Qt.Key.Key_Left,
                               Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down])
        modifier = self.rng.choice([Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.ControlModifier,
                                    Qt.KeyboardModifier.AltModifier])
        QTest.keyClick(QApplication.focusWidget() or self.page.view, key, modifier)
        return f"the key {key.name} with {modifier.name}"

    def drop(self):
        what = self.rng.choice([("part", self.rng.choice(list(B.KINDS))), ("paths", [self.rng.choice(self.folders)]),
                                ("paths", [os.path.join(self.folders[1], ".sandbox-ai.json")])])
        if what[0] == "part" and B.KINDS[what[1]].required:
            return None
        self.page.drop(what, QPointF(self.rng.randint(0, 800), self.rng.randint(0, 600)))
        return f"a drop of {what[0]}"

    def discard(self):
        self.page.discard_changes()
        return "discard the changes"

    def guide(self):
        node = self.rng.choice(self.page.board.nodes)
        self.page.guide_node(node.id)
        return f"the guide of {node.kind}"

    def finding(self):
        if self.page.findings.count():
            self.page.finding_clicked(self.page.findings.item(self.rng.randrange(self.page.findings.count())))
            return "a finding"

    def wait(self):
        end = time.monotonic() + self.rng.choice([0.01, 0.05, 0.3])
        while time.monotonic() < end:
            self.qapp.processEvents()
        return "a moment"

    def host(self):
        host = self.rng.choice(["lab", "local", "local"])
        self.page.host_choice.choose(host)
        return f"the host {host}"

    def dialog(self):
        page = self.page
        what = self.rng.choice(["gallery", "wizard", "export", "import", "save as preset", "fill in", "remote folder",
                                "dry run", "unnamed"])
        if what == "gallery":
            page.open_gallery()
        elif what == "wizard":
            page.open_wizard()
        elif what == "export":
            page.export_board()
        elif what == "import":
            page.import_board()
        elif what == "save as preset":
            page.save_as_preset()
        elif what == "fill in":
            page.fill_in()
        elif what == "remote folder":
            if page.host() != "local":
                page.open_remote_project(self.rng.choice(["/srv/a", "/srv/b"]))
        elif what == "unnamed":
            page.show_unnamed()
        else:
            page.dry_run()
        return f"the dialog {what}"

    STEPS = (["add"] * 6 + ["remove"] * 2 + ["select"] * 6 + ["field"] * 10 + ["button"] * 3 + ["undo"] * 3
             + ["redo"] * 2 + ["edge"] * 2 + ["key"] * 2 + ["dock"] * 2 + ["wait"] * 2 + ["host"] * 2 + ["dialog"] * 3
             + ["select_many", "delete", "copy_paste", "cut", "arrange", "project", "preset", "theme", "other_page",
                "clone", "change_folder", "apply", "start", "drop", "discard", "guide", "finding"])


@pytest.mark.parametrize("seed", SEEDS)
def test_a_random_walk_over_the_board_raises_nothing_and_keeps_it_sound(window, qapp, env, monkeypatch, seed):  # noqa: F811
    rng = random.Random(seed)
    raised, on_gui, log = [], [], []
    monkeypatch.setattr(sys, "excepthook", lambda kind, error, trace: raised.append(
        "".join(traceback.format_exception(kind, error, trace))))
    sai = window.sai
    if seed % 2 == 0:
        i18n.set_language("de")
    monkeypatch.setattr(sai, "hosts_supported", lambda: True)
    monkeypatch.setattr(sai, "host_entries", lambda: [{"name": "lab", "kind": "ssh", "ssh": "me@lab.example"}])

    def host_run(name, *args, **kw):  # the host does not answer: as ssh fails, after a moment, never on the GUI thread
        if threading.current_thread() is threading.main_thread():
            on_gui.append((args, "".join(traceback.format_stack(limit=12))))
        else:
            time.sleep(rng.choice([0.0, 0.05, 0.3]))
        return 255, "", UNREACHABLE
    monkeypatch.setattr(sai, "host_run", host_run)
    window.resize(1280, 800)
    window.show()
    window.show_page("board")
    page = window.board
    page.host_choice.reload()
    page.settings = lambda: {"board_grid": 20, "guide_nodes": False, "board_check_delay": 30}
    walk = Walk(window, env, rng)
    answers = QTimer()
    answers.timeout.connect(walk.answer)
    answers.start(15)

    def trail():
        return f"seed {seed}:\n" + "\n".join(log[-30:])
    try:
        for step in range(STEPS):
            name = rng.choice(Walk.STEPS)
            log.append(f"{step}: {name}")
            done = getattr(walk, name)()
            log[-1] = f"{step}: {done or name + ' (nothing to do)'}"
            pump(qapp)
            assert not raised, raised[0][-4000:] + "\n" + trail()
            problem = sound(page)
            assert problem is None, problem + "\n" + trail()
    finally:
        answers.stop()
        theme.apply("system", qapp)
        i18n.set_language("en")
    widgets.ThreadCall.wait_all(60)
    pump(qapp, 5)
    assert not raised, raised[0][-4000:] + "\n" + trail()
    assert not on_gui, f"run on the host from the GUI thread: {on_gui[0][0]}\n{on_gui[0][1]}"


# -- a host that answers slowly: the Board is never held up, and a late answer is never taken for the board shown --

@pytest.fixture
def slow(qapp, tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    (tmp_path / "cwd").mkdir()
    monkeypatch.chdir(tmp_path / "cwd")
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    sai = backend.SandboxAI()
    sai.on_gui, sai.dry_runs, sai.gate = [], [], threading.Event()
    monkeypatch.setattr(sai, "hosts_supported", lambda: True)
    monkeypatch.setattr(sai, "host_entries", lambda: [{"name": "slow", "kind": "ssh", "ssh": "me@slow.example"},
                                                      {"name": "fast", "kind": "ssh", "ssh": "me@fast.example"}])

    def host_run(name, *args, **kw):
        if threading.current_thread() is threading.main_thread():
            sai.on_gui.append(args)
            return 255, "", "run on the GUI thread"
        if "--dry-run" in args:
            sai.dry_runs.append(name)
            if name == "slow":
                sai.gate.wait(20)  # (it answers when the test lets it)
                return 2, "", "agent-sandbox: slow refuses this"
            return 0, json.dumps({"agent": args[0], "host": name}), ""
        return 1, "", "not asked in this test"
    monkeypatch.setattr(sai, "host_run", host_run)
    monkeypatch.setattr(BV, "REMOTE_CHECK_DELAY", 20)
    view = BV.BoardPage(sai, lambda *a, **k: None, lambda: {"board_grid": 20, "guide_nodes": False,
                                                            "board_check_delay": 20})
    view.resize(1200, 800)
    view.show()
    qapp.processEvents()
    yield view
    sai.gate.set()
    view.check_timer.stop()
    view.close()
    widgets.ThreadCall.wait_all(30)
    view.deleteLater()


def test_a_slow_hosts_late_verdict_is_never_shown_for_another_host(slow, qapp, wait):
    page, sai = slow, slow.sai
    page.add_node("workspace", path="/srv/project")
    assert page.host_choice.choose("slow")
    assert wait(lambda: "slow" in sai.dry_runs, 20), "its launcher is asked (in a worker)"
    took = time.monotonic()
    page.add_node("limits")  # the Board is used meanwhile: nothing waits for the host
    page.show_properties(page.board.first("limits").id)
    page.arrange()
    pump(qapp, 5)
    assert time.monotonic() - took < 2.0 and page.check_running
    assert page.verdict.text() == "The launcher checks the options…"
    assert page.host_choice.choose("fast")  # another host while the slow one is still asked
    pump(qapp, 5)
    sai.gate.set()  # ... now the slow one answers: its refusal is not this host's verdict
    assert wait(lambda: page.launcher_ok is True and "accepts" in page.verdict.text(), 20), page.verdict.text()
    assert "slow refuses" not in page.verdict.text() and sai.dry_runs[-1] == "fast"
    assert getattr(page, "launcher_refusal", (None,))[0] is None
    assert sai.on_gui == []
