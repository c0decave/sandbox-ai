"""A quieter app (user 2026-11-18): "such info texts we don't need in the app - show them only when it is enabled in the
settings". The setting explanations ("Show explanations", off by default) shows or hides every purely explanatory
text - a page's intro line, a hint below a form or a table, a "how this works" paragraph - at once, on every page and
in every dialog (widgets.explanation). Warnings, errors, status and result lines, an empty list's line and what must be
read to act safely stay: the registry below says for each secondary text kept why it is kept, and a new one fails
until it is an explanation or listed. A hidden explanation takes no room: its layout closes the space."""
import ast
import json
from pathlib import Path
import time

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QBoxLayout, QDialog, QFormLayout, QLabel, QLayout, QTabWidget, QWizard,
)

from sandbox_ai_app import backend, i18n, pages, ui, widgets  # noqa: E402
from sandbox_ai_app.state import State  # noqa: E402

from test_help_hover import make_window  # noqa: E402
from test_ui import env, fake_run, ask, window  # noqa: E402,F401 - the window of the UI tests

APP = Path(__file__).resolve().parents[1] / "sandbox_ai_app"


@pytest.fixture
def dialogs(monkeypatch):
    """exec() shows the dialog without waiting: the walk looks into it."""
    opened = []

    def show(dialog):
        opened.append(dialog)
        dialog.show()
        return 0
    monkeypatch.setattr(QDialog, "exec", show)
    monkeypatch.setattr(QWizard, "exec", show)
    yield opened
    for dialog in opened:
        dialog.close()


def explanations_on_screen():
    """Every explanation in a visible window: (shown, its parent shown, the page asked to hide it)."""
    found = []
    for top in QApplication.topLevelWidgets():
        if not top.isVisible():
            continue
        found += [(label, label.isVisible(), label.parentWidget().isVisible(), label.wanted is False)
                  for label in top.findChildren(QLabel) if isinstance(label, widgets.Explanation)]
    return found


def wrong(shown):
    """The explanations on screen that do not follow the setting SHOWN."""
    return [label.text()[:60] for label, visible, parent, hidden_by_page in explanations_on_screen()
            if parent and visible != (shown and not hidden_by_page)]


def visit(window, qapp, check):
    """Every page, every tab of it: CHECK() there; what it found, by where."""
    found = {}
    for key, _ in ui.PAGES:
        window.show_page(key, raise_window=False)
        qapp.processEvents()
        tabs_found = getattr(window, key).findChildren(QTabWidget)
        for tabs in tabs_found:
            for index in range(tabs.count()):
                tabs.setCurrentIndex(index)
                qapp.processEvents()
                found.update({f"{key}, tab {index}: {text}": 1 for text in check()})
        found.update({f"{key}: {text}": 1 for text in check()})
    return sorted(found)


def seen(counted):
    return lambda: [label.text()[:60] for label, visible, *_ in explanations_on_screen()
                    if visible and counted.add(label.text()) is None]


def test_off_by_default_read_at_the_start(qapp, env, monkeypatch):  # noqa: F811
    assert backend.SETTINGS["explanations"] is False and backend.setting("explanations", {}) is False
    made = make_window(monkeypatch)
    try:
        assert not widgets.explanations_shown()
        intro = made.approvals.header.purpose
        assert isinstance(intro, widgets.Explanation) and intro.isHidden()
    finally:
        made.close()
    backend.SandboxAI().save_setting("explanations", True)
    made = make_window(monkeypatch)
    try:
        assert widgets.explanations_shown(), "read at the start, before the pages are made"
        assert not made.approvals.header.purpose.isHidden()
    finally:
        made.close()


def test_the_setting_shows_and_hides_them_on_every_page_and_dialog_at_once(window, qapp, dialogs):
    """Off: no explanation shows anywhere. Switched on in the Settings dialog: every one is there at once - on every
    page, in the dialog itself and in the dialogs opened later - without a restart; off again, gone."""
    window.resize(1360, 900)
    window.show()
    assert visit(window, qapp, lambda: wrong(False)) == []
    assert visit(window, qapp, seen(set())) == [], "none shows while the setting is off"
    window.open_settings()
    settings = dialogs[-1]
    qapp.processEvents()
    assert wrong(False) == [] and not any(visible for _, visible, *_ in explanations_on_screen())
    settings.fields["explanations"].setChecked(True)  # the human's click: saved, applied at once
    qapp.processEvents()
    assert window.sai.settings()["explanations"] is True and widgets.explanations_shown()
    assert wrong(True) == [], "in the Settings dialog itself, at once"
    settings.close()
    counted = set()
    assert visit(window, qapp, lambda: wrong(True)) == []
    visit(window, qapp, seen(counted))
    assert len(counted) >= 20, sorted(counted)  # the pages' intro lines and hints: shown, page after page
    assert any(text.startswith("A request waits up to") for text in counted)
    assert any(text.startswith("What your sandboxes ask") for text in counted)
    window.board.open_gallery()
    window.board.open_wizard(ask=False)
    qapp.processEvents()
    assert wrong(True) == [], "in dialogs made after the switch"
    wizard = next(d for d in dialogs if isinstance(d, QWizard))
    folder_page = wizard.page(wizard.pageIds()[0])
    assert folder_page.subTitle() == folder_page.subtitle, "a wizard page's line is an explanation too"

    window.open_settings()
    dialogs[-1].fields["explanations"].setChecked(False)
    qapp.processEvents()
    assert window.sai.settings()["explanations"] is False and wrong(False) == []
    assert folder_page.subTitle() == ""
    devices = next(wizard.page(i) for i in wizard.pageIds() if wizard.page(i).title() == "Devices and limits")
    assert devices.subTitle().startswith("Devices weaken the isolation"), "a risk is never hidden"
    for dialog in dialogs:
        dialog.close()
    assert visit(window, qapp, lambda: wrong(False)) == []
    assert visit(window, qapp, seen(set())) == []


def test_what_the_page_hides_stays_hidden_and_a_new_one_follows_the_setting(qapp):
    box = QtWidgets.QWidget()
    layout = QtWidgets.QVBoxLayout(box)
    hint = widgets.explanation("How this works.")
    layout.addWidget(hint)
    box.show()
    assert hint.isHidden()
    widgets.set_explanations_shown(True)
    assert hint.isVisible()
    hint.hide()  # the page hides it for a while (another state of the page)
    widgets.set_explanations_shown(False)
    widgets.set_explanations_shown(True)
    assert hint.isHidden(), "the page's own hide wins"
    hint.show()
    assert hint.isVisible()
    later = widgets.explanation("Made while the setting is on.")
    layout.addWidget(later)
    qapp.processEvents()
    assert later.isVisible()
    widgets.set_explanations_shown(False)
    assert later.isHidden() and hint.isHidden()
    lonely = widgets.explanation("No parent yet.")
    widgets.set_explanations_shown(True)
    assert not lonely.isVisible() and not lonely.isWindow() or not lonely.isVisible(), "never a window of its own"
    box.close()


# -- the layouts close the space --

def item_of(layout, widget):
    """(layout, index) of the layout that holds WIDGET, searched from LAYOUT down."""
    for index in range(layout.count()):
        item = layout.itemAt(index)
        if item.widget() is widget:
            return layout, index
        if item.layout() is not None:
            found = item_of(item.layout(), widget)
            if found:
                return found
    return None


def shown_item(item):
    if item.widget() is not None:
        return not item.widget().isHidden() and item.geometry().height() > 0
    if item.layout() is not None:
        return any(shown_item(item.layout().itemAt(i)) for i in range(item.layout().count()))
    return False


def neighbours(label):
    """The geometries of the shown items before and after LABEL in its layout (None where there is none)."""
    parent = label.parentWidget()
    found = item_of(parent.layout(), label) if parent is not None and parent.layout() is not None else None
    if not found:
        return None
    layout, index = found
    before = next((layout.itemAt(i) for i in range(index - 1, -1, -1) if shown_item(layout.itemAt(i))), None)
    after = next((layout.itemAt(i) for i in range(index + 1, layout.count()) if shown_item(layout.itemAt(i))), None)
    if before is None or after is None:
        return None
    spacing = layout.verticalSpacing() if isinstance(layout, QFormLayout) else layout.spacing()
    return before.geometry(), after.geometry(), max(spacing, 0)


def gaps_left(window, qapp):
    """Explanations whose space stays empty once they are hidden: the room between the shown items before and
    after one (in its layout) must shrink by its height."""
    widgets.set_explanations_shown(True)
    qapp.processEvents()
    measured = []
    for label, visible, parent, _ in explanations_on_screen():
        if not visible or not parent:
            continue
        around = neighbours(label)
        if around is not None and isinstance(label.parentWidget().layout(), (QBoxLayout, QFormLayout)):
            before, after, spacing = around
            measured.append((label, after.top() - before.bottom(), label.height(), spacing))
    widgets.set_explanations_shown(False)
    qapp.processEvents()
    for widget in QApplication.topLevelWidgets():
        if widget.isVisible() and widget.layout() is not None:
            widget.layout().activate()
    qapp.processEvents()
    left = []
    for label, gap_on, height, spacing in measured:
        around = neighbours(label)
        if around is None:
            continue
        before, after, _ = around
        gap_off = after.top() - before.bottom()
        if gap_off > gap_on - height + spacing + 2:
            left.append(f"{label.text()[:50]}: {gap_off} px left (shown: {gap_on}, its height {height})")
    return left, len(measured)


def test_a_hidden_explanation_leaves_no_gap(window, qapp, dialogs):
    window.resize(1360, 900)
    window.show()
    total, left = 0, []
    for key in ("approvals", "hosts", "llm", "kits", "images", "team", "credentials", "files"):
        window.show_page(key, raise_window=False)
        qapp.processEvents()
        tabs = getattr(window, key).findChildren(QTabWidget)
        for widget in tabs or [None]:
            for index in range(widget.count() if widget else 1):
                if widget:
                    widget.setCurrentIndex(index)
                    qapp.processEvents()
                found, count = gaps_left(window, qapp)
                left += found
                total += count
    window.open_settings()
    settings = dialogs[-1]
    for index in range(settings.tabs.count()):
        settings.tabs.setCurrentIndex(index)
        qapp.processEvents()
        found, count = gaps_left(window, qapp)
        left += found
        total += count
    assert total >= 15, total
    assert sorted(set(left)) == []


def test_an_empty_form_label_goes_with_its_explanation(qapp):
    page = QtWidgets.QWidget()
    form = QFormLayout(page)
    form.addRow("A", QtWidgets.QLineEdit())
    hint = widgets.explanation("What A means.")
    form.addRow("", hint)
    form.addRow("B", QtWidgets.QLineEdit())
    page.show()
    qapp.processEvents()
    empty = form.labelForField(hint)  # (Qt makes none for "", a QLabel("") of one's own is followed)
    assert empty is None or empty.isHidden(), "no empty row"
    own = QLabel("")
    other = widgets.explanation("What B means.")
    form.addRow(own, other)
    assert own.isHidden()
    widgets.set_explanations_shown(True)
    assert hint.isVisible() and other.isVisible() and not own.isHidden()
    page.close()


# -- the registry: each secondary text that is kept, and why --

# English text (its beginning) -> why it stays shown with the setting off. A secondary-role label with a fixed text,
# or a longer label without a role, is an explanation (widgets.explanation) or listed here.
KEPT = {
    # empty lists and placeholders: what is (not) there
    "Nothing is waiting for an answer.": "empty list",
    "No devices given to it.": "empty list",
    "No bridge between running sessions.": "empty list",
    "No kits yet: create one on the Kits": "empty list",
    "None yet: “Save the board as a preset": "empty list",
    "No host yet: add one on the Hosts page": "empty list (nothing to choose)",
    "This server takes no variables.": "empty list",
    "No host in sandbox-ai hosts yet: a host added later": "empty list (Share with hosts: no host to tick)",
    "Select an image or a variant to edit its extension.": "placeholder of an empty editor",
    "Choose a bundle, then Check.": "status line before the first result",
    "Not checked yet: press Check.": "status line before the first result",
    # status lines: what is going on now
    "Reading your images (sandbox-ai images list)…": "progress",
    "Reading the kits (sandbox-ai kits list)…": "progress",
    "Reading the model servers (sandbox-ai llm list)…": "progress",
    "Asking the model servers for their models": "progress",
    "Reading the session's folders…": "progress",
    "This sandbox-ai cannot list its kits": "status: an older sandbox-ai, what to do instead",
    "This sandbox-ai records no safeguards yet; update it.": "status: an older sandbox-ai",
    "This sandbox-ai has no loop guard layers yet: update it.": "status: an older sandbox-ai",
    "Load the catalogs to set the folders": "status: what to do to see more",
    "Load the catalogs to see the variables": "status: what to do to see more",
    "{count} question(s); each can be changed later": "result",
    "gets the copy when it is shared": "status of a host (Share with hosts)",
    "not copied yet": "status of a host (Share with hosts)",
    # facts of the dialog: where, on which host, which values
    "on {host}": "where the dialog acts",
    "It is imported on {host}.": "where the dialog acts",
    "Paths on {host}: chosen from {host}'s folders": "where the paths are (another machine's)",
    "Defined: {list}": "values",
    "Memory pressure from {size} on (memory.high)": "value",
    "The log: {path}": "where the log is (a path to find it)",
    "Tags": "the caption of a list",
    "{agent} (leads)": "the heading of a member's fields",
    "Set on the server only (shown, not set by sandbox-ai):": "the caption of a list of values",
    "Command line: {cli}": "the command a help text is about",
    "Folders and files of the session: where each one lies": "the caption of the form below it",
    "Last connections of this run to it, from its log": "whose words the log lines are (inside Details)",
    "Which topic? A tour shows it": "the question of the dialog",
    # consequences and risks: what must be read to act safely
    "Terminal tabs show what sandboxes and agents print": "a dangerous choice (Chromium's sandbox off)",
    "Devices weaken the isolation": "a risk",
    "The report holds no secret, but it names hosts": "pass on with care",
    "What a session's login file says about its account": "the agent wrote it: not checked",
    "Memory and instructions files may hold private notes.": "before an import: private notes",
    "Secrets are never kept: a board with one is not saved.": "what is (not) saved, before Save",
    "It is opened in the running sandbox now, without a restart": "what to do for the agent to use it (restart)",
    "A server that runs a command: its network, folders": "why the fields are greyed out",
    "{name} is {system}. The packages are installed": "the image and its result stay UNCHECKED",
    "A sandbox asks while a dialog is open.": "why the window came up, where to answer",
    "This is a Git server: give the secret": "the choice below depends on it",
    "The file holds this machine's folder paths": "pass the file on knowingly",
    "The app cannot play videos here": "the question of the dialog",
    "The videos about the app's pages are not on this computer": "status and where to put them",
}

# Labels a test builds from these calls: a function whose call makes a label of its first argument.
LABELS = ("plain_label", "NoteLabel", "ElidedLabel", "QLabel")


def constants(tree):
    """{NAME: text} of the module's N_("…") and plain string constants (tr(PRIVACY))."""
    found = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            text = literal(node.value, {})
            if text:
                found[node.targets[0].id] = text[0]
    return found


def literal(node, names):
    """The texts NODE may be: tr("…")'s, "…" itself, a constant's, both of a conditional; [] when not fixed."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.Name) and node.id in names:
        return [names[node.id]]
    if isinstance(node, ast.IfExp):
        return literal(node.body, names) + literal(node.orelse, names)
    if isinstance(node, ast.BinOp):  # tr("…") + (" " + more): the fixed beginning
        return literal(node.left, names)
    if isinstance(node, ast.Call) and node.args:
        name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
        if name in ("tr", "N_"):
            return literal(node.args[0], names)
    return []


def fixed_labels():
    """[(where, text, role)] of every label made with a fixed text: secondary ones, and longer ones without a role."""
    found = []
    for path in sorted(APP.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = constants(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
            if name not in LABELS:
                continue
            role = next((kw.value.value for kw in node.keywords if kw.arg == "role" and
                         isinstance(kw.value, ast.Constant)), None)
            for text in literal(node.args[0], names):
                if role == "secondary" and text.strip() or role is None and len(text) >= 60:
                    found.append((f"{path.name}:{node.lineno}", text, role))
    return found


def kept(text):
    return next((key for key in KEPT if text.startswith(key)), None)


def test_every_secondary_text_is_an_explanation_or_kept_for_a_reason():
    """A new page's hint must use widgets.explanation (shown only with the setting on) - or be listed in KEPT with
    the reason it must be read with the setting off."""
    found = fixed_labels()
    assert len(found) >= 40, "the scan finds the app's labels"
    unlisted = [f"{where}: {text[:70]}" for where, text, _ in found if kept(text) is None]
    assert unlisted == [], "make it widgets.explanation(...) or keep it in KEPT with a reason"
    used = {kept(text) for _, text, _ in found}
    assert sorted(set(KEPT) - used) == [], "KEPT names no label that is not there any more"


def test_the_converted_texts_are_explanations_and_never_warnings():
    """widgets.explanation(...) is never given a warning or an error: those are notice roles and always shown."""
    for path in sorted(APP.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and (getattr(node.func, "id", "") == "explanation" or
                                               getattr(node.func, "attr", "") == "explanation"):
                assert not any(kw.arg in ("role", "log", "fit") for kw in node.keywords), \
                    f"{path.name}:{node.lineno}: an explanation has no role, log or fit of its own"


def test_the_setting_has_its_words_in_both_languages():
    from sandbox_ai_app import help_texts as H
    assert "explanations" in H.SETTING_TIPS
    tip = H.SETTING_TIPS["explanations"]
    assert "Off (the default)" in tip.en and "Aus (die Voreinstellung)" in tip.de
    i18n.set_language("de")
    try:
        assert i18n.tr("Show explanations (the info texts below forms and tables, the pages' intro lines)") \
            .startswith("Erklärungen anzeigen")
    finally:
        i18n.set_language("en")


# -- the Approvals page: one Details box below the question table --

def long_call(run, ident, text):
    asked = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    arguments = {"text": text}
    (run / "approvals" / f"{ident}.json").write_text(json.dumps({
        "id": ident, "time": asked, "timeout": 60, "kind": "mcp", "server": "bsky", "tool": "post", "rule": "posts",
        "ruleset": "rules.json", "arguments": json.dumps({"text": text[:300]})[:300] + "...",
        "arguments_full": json.dumps(arguments, indent=2)}))


def choose(page, ident):
    for row in range(page.open.rowCount()):
        page.open.selectRow(row)
        if page.current() and page.current()[1].get("id") == ident:
            return
    raise AssertionError(f"no row for {ident}")


def shown_in_details(page):
    return [name for name, widget in (("why", page.why), ("arguments", page.arguments), ("context", page.context),
                                      ("log", page.context_log)) if widget.isVisible()]


def test_the_details_are_folded_below_the_question_and_open_where_they_are_needed(window, env, qapp):  # noqa: F811
    page = window.approvals
    window.resize(1360, 900)
    window.show()
    window.show_page("approvals", raise_window=False)
    run = fake_run(env)
    ask(run, "1700000000-1-1", kind="dns", name="example.org")
    long_call(run, "1700000000-1-2", "Anfang " + "lang " * 200 + "ENDE")
    ask(run, "1700000000-1-3", kind="canary", canary="aws-keys", summary="read ~/.aws/credentials")
    page.poll()
    qapp.processEvents()
    assert page.open.rowCount() == 3
    # Every part that read as the table again is inside the one box: the why, the arguments, the context, the log.
    for part in (page.why, page.arguments, page.context, page.context_log_label, page.context_log):
        assert page.details_box.isAncestorOf(part)
    choose(page, "1700000000-1-1")  # a name to look up: its line says it all - folded
    qapp.processEvents()
    assert page.details.isVisible() and not page.details.isChecked() and page.details_box.isHidden()
    assert shown_in_details(page) == [] and page.allow_button.isVisible(), "the row and the buttons are what shows"
    choose(page, "1700000000-1-2")  # its arguments shortened in the line: opened by itself, the call whole
    qapp.processEvents()
    assert page.details.isChecked() and page.details_box.isVisible() and page.arguments.isVisible()
    assert "ENDE" in page.arguments.text.toPlainText() and page.arguments.text.isVisible()
    choose(page, "1700000000-1-3")  # a decoy file touched: what it means is read before the answer
    qapp.processEvents()
    assert page.details.isChecked() and "decoy file" in page.why.text() and page.why.isVisible()
    choose(page, "1700000000-1-1")
    qapp.processEvents()
    assert not page.details.isChecked(), "back to the human's choice for one that needs nothing"
    page.details.click()  # opened by hand: kept for every question, and in the window's state
    qapp.processEvents()
    assert page.details_box.isVisible() and "why" in shown_in_details(page)
    assert window.state.section("checks").get("approvals-details") is True
    page.poll()  # the next look at the questions keeps it
    qapp.processEvents()
    assert page.details.isChecked()
    choose(page, "1700000000-1-2")
    choose(page, "1700000000-1-1")
    assert page.details.isChecked()
    page.details.click()  # folded by hand
    assert window.state.section("checks").get("approvals-details") is False
    page.open.clearSelection()
    qapp.processEvents()
    assert page.details.isHidden(), "nothing selected: no Details"


def test_the_details_choice_is_kept_for_the_next_start(qapp, env, tmp_path):  # noqa: F811
    store = State(path=tmp_path / "state.json")
    page = pages.ApprovalsPage(backend.SandboxAI(), interval=0, remote_interval=0)
    try:
        page.keep_details(store)
        assert page.details_wanted is False, "folded at first"
        page.details_chosen(True)
        store.flush()
        again = State(path=tmp_path / "state.json")
        page.keep_details(again)
        assert page.details_wanted is True
    finally:
        page.timer.stop()
        page.blocked.timer.stop()
        page.deleteLater()


def test_the_approvals_page_says_each_thing_once(window, qapp):
    """Folded and with the setting off, the page's question box holds the table, the answers and their result: the
    intro line, the hint about the wait and the Allowed now note are explanations."""
    page = window.approvals
    assert isinstance(page.header.purpose, widgets.Explanation)
    hints = [label for label in page.findChildren(QLabel) + page.open_box.findChildren(QLabel)
             if isinstance(label, widgets.Explanation)]
    texts = [label.text() for label in hints]
    assert any(text.startswith("A request waits up to") for text in texts)
    assert any(text.startswith("Listed: what was allowed while it runs") for text in texts)
    assert page.header.state.property("role") == "secondary" and not isinstance(page.header.state,
                                                                                 widgets.Explanation), \
        "how the sandbox runs is its state: shown"
    assert isinstance(page.details_box.layout(), QLayout)
