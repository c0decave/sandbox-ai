"""Board review 2, the findings left open there (docs/reviews/board-review-2.md R2-18, R2-20, R2-21, R2-23), each
through the real entry points - the app's main window offscreen (closed and started again, or crashed), its Board and
Start pages, and the preset gallery as "New from preset…" opens it.

- R2-18: a draft lost its steps of Undo once a Change folder was among its last 20: after a restart Undo went back to
  the project's file in one step. Kept now with both folders' trust bookkeeping (a folder whose project file is not
  trusted, or changed meanwhile, is not trusted after the Undo either).
- R2-20: Dry run's answer was shown as the plan of the board although the board or the host had changed meanwhile.
- R2-21: Bridge parts still waiting when the app quit made no bridge, and nothing said so.
- R2-23: the preset gallery was made in one go on the GUI thread, a card per preset."""
import json
import os
import sys
import time
import traceback

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from sandbox_ai_app import backend, board_view as BV, widgets  # noqa: E402

from test_projects import close, env, make_window, project, window  # noqa: E402,F401 - shared fixtures

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


@pytest.fixture
def raised(monkeypatch):
    """What a slot raised (Qt hands it to sys.excepthook and goes on): a test of a journey fails for any."""
    seen = []
    monkeypatch.setattr(sys, "excepthook",
                        lambda kind, error, trace: seen.append("".join(traceback.format_exception(kind, error, trace))))
    return seen


@pytest.fixture
def answered(monkeypatch):
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    return widgets.SHOWN


def pump(qapp, rounds=5):
    for _ in range(rounds):
        qapp.processEvents()


def restart(window, monkeypatch):
    """Close WINDOW as the app does (its state is written) and start a new one."""
    window.shutdown()
    return make_window(monkeypatch)


def crashed(window, monkeypatch, wait):
    """WINDOW's process dies (nothing of its shutdown runs; what the state's timer wrote is all there is), and the app
    is started again."""
    assert wait(lambda: not window.state.timer.isActive(), 10)
    assert window.state.flush()
    window.state.flush = lambda *a, **k: True
    window.commit_edits = lambda: None
    return make_window(monkeypatch)


def change_folder(board, folder, wait):
    assert board.change_folder(folder)
    assert wait(lambda: board.key_of(board.directory) == board.key_of(folder), 10)


# -- R2-18: Undo across a Change folder after a restart --

@pytest.mark.parametrize("how", ["closed", "crashed"])
def test_undo_goes_back_across_change_folder_after_a_restart(qapp, env, monkeypatch, wait, raised, answered, how):  # noqa: F811
    one, two = project(env, "one"), project(env, "two")
    first = make_window(monkeypatch)
    first.show_page("board")
    board = first.board
    assert first.set_project(one)
    board.add_node("limits")
    board.set_prop(board.board.first("limits").id, "--memory", "4g")
    change_folder(board, two, wait)  # the board of one (limits) is on two now, as one step
    board.add_node("mount", source="/data", dest="/mnt/data", mode="ro")
    assert board.undo.count() == 4
    again = restart(first, monkeypatch) if how == "closed" else crashed(first, monkeypatch, wait)
    try:
        again.show_page("board")
        board = again.board
        assert wait(lambda: board.key_of(board.directory) == board.key_of(two), 10)
        assert board.dirty() and board.board.first("limits") is not None and board.board.of_kind("mount")
        assert board.undo.count() == 4, [board.undo.text(i) for i in range(board.undo.count())]
        board.undo.undo()  # the mount
        assert board.board.of_kind("mount") == [] and board.key_of(board.directory) == board.key_of(two)
        board.undo.undo()  # Change folder: back on one, with the window's project
        pump(qapp)
        assert board.key_of(board.directory) == board.key_of(one)
        assert again.project == os.path.realpath(one)
        assert board.board.first("limits").props == {"--memory": "4g"}
        board.undo.undo()
        assert board.board.first("limits") is not None and "--memory" not in board.board.first("limits").props
        board.undo.undo()
        assert board.board.first("limits") is None, "back at the project's file"
        board.undo.redo()
        board.undo.redo()
        board.undo.redo()  # Change folder again: on two
        pump(qapp)
        assert board.key_of(board.directory) == board.key_of(two)
        assert not raised, raised[0][-3000:]
    finally:
        close(again)
        if how == "crashed":
            close(first)


def trust(sai, folder):
    sai.trust_project(sai.review_project(folder))


@pytest.mark.parametrize("case", ["untrusted", "trusted", "changed meanwhile"])
def test_undo_of_change_folder_after_a_restart_keeps_the_old_folders_trust(qapp, env, monkeypatch, wait, raised,
                                                                           answered, case):  # noqa: F811
    """The board goes back onto the folder it came from with the trust it had there: a project file nobody trusted
    (or one changed while the app was closed) is not trusted after the Undo - Start waits for Apply."""
    one, two = project(env, "one"), project(env, "two")
    (one / ".sandbox-ai.json").write_text(json.dumps({"version": 1, "agent": "claude", "options": ["--memory", "2g"]}))
    first = make_window(monkeypatch)
    if case != "untrusted":
        trust(first.sai, one)
    first.show_page("board")
    board = first.board
    assert first.set_project(one)
    assert bool(board.untrusted.get(board.key_of(one))) == (case == "untrusted")
    change_folder(board, two, wait)
    board.add_node("mount", source="/data", dest="/mnt/data", mode="ro")
    first.shutdown()
    if case == "changed meanwhile":
        (one / ".sandbox-ai.json").write_text(json.dumps({"version": 1, "agent": "claude",
                                                          "options": ["--memory", "8g"]}))
    again = make_window(monkeypatch)
    try:
        again.show_page("board")
        board = again.board
        assert wait(lambda: board.key_of(board.directory) == board.key_of(two), 10)
        board.undo.undo()
        board.undo.undo()  # Change folder: back on one
        pump(qapp)
        assert board.key_of(board.directory) == board.key_of(one)
        expected = case != "trusted"
        assert bool(board.untrusted.get(board.key_of(one))) == expected, (case, board.untrusted)
        if expected:
            widgets.SHOWN.clear()
            assert board.start() is False
            assert any("is not trusted" in str(text) for _title, text, _d in widgets.SHOWN), widgets.SHOWN
        assert not raised, raised[0][-3000:]
    finally:
        close(again)


def test_change_folder_keeps_a_board_from_an_untrusted_file_untrusted(window, env, wait, answered):  # noqa: F811
    """A board read from a project file nobody trusted carries what that file set (also what no part shows): on
    another folder (Change folder) it is not trusted either - as a clone is not - until Apply shows it and asks."""
    one, two = project(env, "one"), project(env, "two")
    (one / ".sandbox-ai.json").write_text(json.dumps({"version": 1, "agent": "claude", "mode": "low"}))
    board = window.board
    assert window.set_project(one)
    assert board.untrusted.get(board.key_of(one))
    change_folder(board, two, wait)
    assert board.untrusted.get(board.key_of(two)), "not trusted on the new folder either"
    assert board.sources[board.key_of(two)]["trusted"] is False
    board.undo.undo()
    board.undo.redo()
    assert board.untrusted.get(board.key_of(two))


def test_a_folder_gone_meanwhile_keeps_the_draft_without_its_undo(qapp, env, monkeypatch, wait, raised, answered):  # noqa: F811
    """Undo onto a folder deleted while the app was closed cannot go there: the draft comes back as one step (as
    before), never a step that opens a folder that is gone."""
    one, two = project(env, "one"), project(env, "two")
    first = make_window(monkeypatch)
    first.show_page("board")
    board = first.board
    assert first.set_project(one)
    change_folder(board, two, wait)
    board.add_node("limits")
    first.shutdown()
    one.rmdir()
    again = make_window(monkeypatch)
    try:
        again.show_page("board")
        board = again.board
        assert wait(lambda: board.key_of(board.directory) == board.key_of(two), 10)
        assert board.board.first("limits") is not None and board.undo.count() == 1
        board.undo.undo()
        assert board.key_of(board.directory) == board.key_of(two)
        assert not raised, raised[0][-3000:]
    finally:
        close(again)


def test_history_of_keeps_change_folder_steps(qapp, tmp_path, monkeypatch, env):  # noqa: F811
    """The state file's steps: a Change folder is one with both folders and their trust, a Change folder on another
    host one with both of its folders there."""
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    view = BV.BoardPage(backend.SandboxAI(), lambda *a, **k: None, lambda: {"board_grid": 20, "guide_nodes": False})
    try:
        one, two = project(env, "one"), project(env, "two")
        view.open_project(str(one), quiet=True)
        view.add_node("limits")
        view.switch_folder(str(two))
        view.switch_host_folder("lab", "/srv/x")
        history = BV.history_of(view.undo)
        assert history is not None and [s.get("kind", "snapshot") for s in history["steps"]] == \
            ["snapshot", "folder", "host-folder"]
        step = history["steps"][1]
        assert step["before_dir"] == str(one) and step["after_dir"] == str(two)
        assert set(step["trust"]) == {view.key_of(one), view.key_of(two)}
        assert history["steps"][2]["before_remote"] is None and history["steps"][2]["after_remote"] == ["lab", "/srv/x"]
        json.dumps(history)
    finally:
        view.check_timer.stop()
        widgets.ThreadCall.wait_all(30)
        view.deleteLater()


# -- R2-20: a dry run's answer for a board (or form, or host) that changed while it ran --

HOSTS = [{"name": "lab", "kind": "ssh", "ssh": "me@lab.example"}]


def held_dry_run(monkeypatch, sai, refused=False):
    """sai.host_dry_run answers only once RELEASE is set: (release, the hosts asked)."""
    import threading
    release, asked = threading.Event(), []

    def dry(host, agent, argv):
        asked.append(host)
        release.wait(30)
        return (None, "the launcher refuses --nothing") if refused else ({"host": host, "agent": agent}, "")
    monkeypatch.setattr(sai, "host_dry_run", dry)
    return release, asked


def answer(wait, shown):
    count = len(widgets.SHOWN)
    shown()
    assert wait(lambda: len(widgets.SHOWN) > count, 20)
    return widgets.SHOWN[-1]


@pytest.mark.parametrize("refused", [False, True])
def test_the_boards_dry_run_says_when_the_board_changed_while_it_ran(window, qapp, env, wait, monkeypatch, refused):  # noqa: F811
    board = window.board
    window.show_page("board")
    assert window.set_project(project(env, "one"))
    release, _asked = held_dry_run(monkeypatch, window.sai, refused)

    def changed_meanwhile():
        board.dry_run()
        board.add_node("limits")
        release.set()
    title, text, _ = answer(wait, changed_meanwhile)
    assert "out of date" in title, title
    assert text.startswith("The board changed while the dry run ran"), text
    assert "local" in text.split("\n")[0]
    release.clear()

    def unchanged():
        board.dry_run()
        release.set()
    title, text, _ = answer(wait, unchanged)
    assert "out of date" not in title and "changed while" not in text
    assert title == ("The launcher refused these options" if refused else "Plan (dry run)")


def test_the_boards_dry_run_says_when_another_host_was_chosen_meanwhile(window, qapp, env, wait, monkeypatch):  # noqa: F811
    from sandbox_ai_app import i18n
    monkeypatch.setattr(window.sai, "hosts_supported", lambda: True)
    monkeypatch.setattr(window.sai, "host_entries", lambda: [dict(h) for h in HOSTS])
    monkeypatch.setattr(window.sai, "host_run", lambda name, *args, **kw: (255, "", "ssh: unreachable"))
    board = window.board
    window.show_page("board")
    board.host_choice.reload()
    release, asked = held_dry_run(monkeypatch, window.sai)
    i18n.set_language("de")
    try:
        def host_meanwhile():
            board.dry_run()
            assert board.host_choice.choose("lab")
            release.set()
        title, text, _ = answer(wait, host_meanwhile)
        assert asked[0] == "local"
        assert title == "Plan (Probelauf) – veraltet", title
        assert text.startswith("Während des Probelaufs wurde ein anderer Host gewählt"), text
        assert "local" in text.split("\n")[0] and "lab" in text.split("\n")[0]
    finally:
        i18n.set_language("en")


def test_the_start_pages_dry_run_says_when_the_form_changed_while_it_ran(window, qapp, env, wait, monkeypatch):  # noqa: F811
    """The Expert view's Dry run is the board's (one start, two views): a field changed while it ran is a change of
    that start, and the answer says it is for the start as it was."""
    launch = window.launch
    window.set_project(str(project(env, "one")))
    window.show_page("launch")
    release, _asked = held_dry_run(monkeypatch, window.sai)

    def changed_meanwhile():
        launch.dry_run()
        launch.form.widgets["--memory"].setText("8g")
        window.start_link.flush()
        release.set()
    title, text, _ = answer(wait, changed_meanwhile)
    assert "out of date" in title and text.startswith("The board changed while the dry run ran"), (title, text)
    release.clear()

    def unchanged():
        launch.dry_run()
        release.set()
    title, text, _ = answer(wait, unchanged)
    assert title == "Plan (dry run)" and json.loads(text) == {"host": "local", "agent": launch.agent.currentText()}
    assert launch.dry_button.isEnabled()


def test_the_start_form_on_its_own_says_when_it_changed_while_its_dry_run_ran(qapp, env, wait, monkeypatch):  # noqa: F811
    """The Start form without a Board beside it: its own dry run, and its own words when the form changed."""
    from sandbox_ai_app.ui import LaunchPage
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    sai = backend.SandboxAI()
    launch = LaunchPage(sai, lambda *a, **k: None)
    try:
        launch.form.widgets["--workspace"].set_value([str(project(env, "one"))])
        release, _asked = held_dry_run(monkeypatch, sai)

        def changed_meanwhile():
            launch.dry_run()
            launch.form.widgets["--memory"].setText("8g")
            release.set()
        title, text, _ = answer(wait, changed_meanwhile)
        assert "out of date" in title and text.startswith("The form changed while the dry run ran"), (title, text)
    finally:
        launch.deleteLater()


# -- R2-21: the Bridge parts of a start still waiting when the app quit --

def bridge_board(board, folder):
    board.add_node("workspace", path=str(folder))
    board.add_node("bridge", peer="docs", message=True)


def bridges_not_yet(monkeypatch, made):
    """Every SandboxAI made from now on: the bridge's sessions do not run (the launcher's dry run says so) until
    RUNNING[0] is set; bridge add records what it was asked."""
    running = [False]

    def plan(self, args):
        if not running[0]:
            raise ValueError(f"nothing named {args[2]} runs (see sandbox-ai list)")
        return {"id": None, "state": "planned", "grants": {}, "warnings": []}
    monkeypatch.setattr(backend.SandboxAI, "bridge_plan", plan)
    monkeypatch.setattr(backend.SandboxAI, "bridge_add", lambda self, args: made.append(args) or
                        {"id": "abcdef01", "a": args[2], "b": args[3], "notes": []})
    return running


@pytest.mark.parametrize("how", ["closed", "crashed"])
def test_a_bridge_still_waiting_when_the_app_quit_waits_on_after_the_restart(qapp, env, monkeypatch, wait, raised,
                                                                           answered, how):  # noqa: F811
    from sandbox_ai_app import bridges_view
    made = []
    running = bridges_not_yet(monkeypatch, made)
    monkeypatch.setattr(bridges_view, "WAIT_EVERY", 30)
    first = make_window(monkeypatch)
    first.show_page("board")
    board = first.board
    folder = project(env, "one")
    assert first.set_project(folder)
    bridge_board(board, folder)
    assert board.start_bridges("claude-board-1") is not None
    pump(qapp, 10)
    widgets.ThreadCall.wait_all(30)
    assert board.bridge_waiters and made == []
    if how == "closed":
        first.shutdown()
        assert board.bridge_waiters == [], "nothing waits in an app that has quit"
    else:
        assert wait(lambda: not first.state.timer.isActive(), 10)
        assert first.state.flush()
        first.state.flush = lambda *a, **k: True
        first.commit_edits = lambda: None
        for waiter in list(board.bridge_waiters):
            waiter.timer.stop()  # (the process is gone)
    widgets.SHOWN.clear()
    again = make_window(monkeypatch)
    try:
        board = again.board
        pump(qapp, 10)
        assert board.bridge_waiters, "the wait goes on"
        said = [text for title, text, _ in widgets.SHOWN if title == "Bridges wait"]
        assert said and "claude-board-1 → docs" in said[0] and "wait again" in said[0], widgets.SHOWN
        running[0] = True
        assert wait(lambda: made, 20)
        assert made == [["bridge", "add", "claude-board-1", "docs", "--allow", "message", "--ask=first"]]
        assert wait(lambda: board.bridge_waiters == [], 10)
        assert again.state.flush() and again.state.data.get("bridge_waits") == [], "made: no wait is kept"
        assert not raised, raised[0][-3000:]
    finally:
        close(again)
        if how == "crashed":
            close(first)


def test_a_bridge_whose_wait_ended_while_the_app_was_closed_is_said_at_the_next_start(qapp, env, monkeypatch, wait,
                                                                                     raised, answered):  # noqa: F811
    from sandbox_ai_app import bridges_view
    made = []
    bridges_not_yet(monkeypatch, made)
    monkeypatch.setattr(bridges_view, "WAIT_EVERY", 30)
    monkeypatch.setattr(bridges_view, "WAIT_SECONDS", 1)
    first = make_window(monkeypatch)
    board = first.board
    folder = project(env, "one")
    assert first.set_project(folder)
    bridge_board(board, folder)
    assert board.start_bridges("claude-board-1") is not None
    first.shutdown()
    time.sleep(1.2)  # the app stays closed until the wait is over
    widgets.SHOWN.clear()
    again = make_window(monkeypatch)
    try:
        board = again.board
        pump(qapp, 10)
        assert board.bridge_waiters == [] and made == []
        said = [text for title, text, _ in widgets.SHOWN if title == "Not bridged"]
        assert said and "claude-board-1 → docs" in said[0] and "Sandboxes page" in said[0], widgets.SHOWN
        assert again.state.flush() and again.state.data.get("bridge_waits") == [], "said once"
        assert not raised, raised[0][-3000:]
    finally:
        close(again)
    widgets.SHOWN.clear()
    third = make_window(monkeypatch)
    try:
        third.board
        pump(qapp, 10)
        assert not [t for t, *_ in widgets.SHOWN if t == "Not bridged"], "not said again"
    finally:
        close(third)


def test_a_kept_wait_that_cannot_be_used_is_dropped_with_a_word(qapp, env, monkeypatch, answered):  # noqa: F811
    """What the state file holds for a wait is checked as a bridge's values are: a name that would be read as an
    option, a capability missing - dropped and said, never asked for."""
    made = []
    bridges_not_yet(monkeypatch, made)
    first = make_window(monkeypatch)
    first.shutdown()
    kept = json.loads(first.state.path.read_text())
    kept["bridge_waits"] = [
        {"values": {"a": "--evil", "b": "docs", "allow": ["message"]}, "until": time.time() + 600},
        {"values": {"a": "one", "b": "docs", "allow": []}, "until": time.time() + 600},
        {"values": "nonsense", "until": "later"}, 7]
    first.state.path.write_text(json.dumps(kept))
    widgets.SHOWN.clear()
    again = make_window(monkeypatch)
    try:
        board = again.board
        pump(qapp, 5)
        assert board.bridge_waiters == [] and made == []
        said = [text for title, text, _ in widgets.SHOWN if title == "Not bridged"]
        assert said and "cannot be used were dropped" in said[0] and "--evil" in said[0], widgets.SHOWN
        assert not [t for t, *_ in widgets.SHOWN if t == "Bridges wait"]
    finally:
        close(again)


# -- R2-23: the preset gallery was made in one go on the GUI thread, a card per preset --

def counted_cards(monkeypatch):
    from sandbox_ai_app import preset_gallery
    made = []
    real = preset_gallery.PresetCard.__init__

    def init(self, *a, **k):
        made.append(self)
        real(self, *a, **k)
    monkeypatch.setattr(preset_gallery.PresetCard, "__init__", init)
    return made


def test_new_from_preset_shows_the_gallery_before_every_card_is_made(window, qapp, env, wait, monkeypatch):  # noqa: F811
    """"New from preset…": the click makes the gallery with the first cards only (what fills its view); the others
    are made in batches while it is shown - the window answers in between - and the search, the categories and the
    keys see every preset meanwhile."""
    from sandbox_ai_app import preset_gallery, presets as PR
    total = len(PR.catalogue(window.sai, "local").presets)
    assert total > 3 * preset_gallery.FIRST_CARDS, "enough presets to tell"
    made = counted_cards(monkeypatch)
    seen = {}

    def run(gallery):
        seen["at_exec"] = len(made)
        turns = 0
        while len(gallery.made) < total and turns < 1000:
            before = len(gallery.made)
            gallery.make_next()  # (what the event loop runs between two of the human's events)
            assert len(gallery.made) - before <= preset_gallery.BATCH_CARDS
            turns += 1
        seen["turns"] = turns
        far = gallery.made[-1].preset  # the last card made: found by the search too
        gallery.choose(gallery.made[-1])
        seen["chosen"] = far
        gallery.result_ = (far, None)
        return QtWidgets.QDialog.DialogCode.Accepted
    monkeypatch.setattr(preset_gallery.PresetGallery, "exec", run)
    board = window.board
    window.show_page("board")
    assert window.set_project(project(env, "one"))
    began = time.perf_counter()
    assert board.open_gallery() is not None
    assert seen["at_exec"] <= preset_gallery.FIRST_CARDS, f"{seen['at_exec']} cards made before the gallery showed"
    assert seen["turns"] >= (total - preset_gallery.FIRST_CARDS) // preset_gallery.BATCH_CARDS
    assert board.dirty() and time.perf_counter() - began < 30


def test_the_gallery_finds_and_shows_presets_whose_cards_are_not_made_yet(qapp, env, monkeypatch, wait):  # noqa: F811
    from sandbox_ai_app import preset_gallery, presets as PR
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    sai = backend.SandboxAI()
    catalogue = PR.catalogue(sai, "local")
    gallery = preset_gallery.PresetGallery(sai, None, "claude", {}, None)
    try:
        assert len(gallery.made) <= preset_gallery.FIRST_CARDS
        last = catalogue.groups()[-1][2][-1] if catalogue.groups()[-1][2] else catalogue.presets[-1]
        counts = {gallery.categories.item(r).data(Qt.ItemDataRole.UserRole): gallery.categories.item(r).text()
                  for r in range(gallery.categories.count())}
        assert all(f"({len(presets)})" in counts[key] for key, _title, presets in catalogue.groups() if key in counts), \
            "every category counts all its presets from the start"
        gallery.search.setText(last.title)
        assert last.key in {c.preset.key for c in gallery.visible_cards()}, "found though made last"
        gallery.search.setText("")
        assert wait(lambda: len(gallery.made) == len(catalogue.presets), 20), "the rest is made while it is shown"
        assert len(gallery.cards) == len(catalogue.presets)
        # made again (another host's presets read, a preset saved): the old batches stop, the new ones run
        gallery.populate(select=last.key)
        assert gallery.selected is not None and gallery.selected.preset.key == last.key
        assert wait(lambda: len(gallery.made) == len(catalogue.presets), 20)
        assert len({id(c) for c in gallery.made}) == len(gallery.made)
    finally:
        gallery.close()
        gallery.deleteLater()
