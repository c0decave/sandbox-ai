"""No QStandardItem wrapper in the app (the user's segfault in a right-click menu, 0.1.229).

A QComboBox keeps its entries as QStandardItems that C++ made. Python's wrapper of one (model().item(i), takeRow,
insertRow) is never told when C++ deletes the item (combo.clear(), removeRow, the combo deleted): once the wrapper is
kept (insertRow hands the item to the model, and its wrapper is kept with the model's), it stays in Shiboken's map,
and the next object C++ allocates at that address - a QAction of the same size - is handed out as that
QStandardItem: menu.addAction(text).setEnabled(...) ran QStandardItem::setEnabled on a QAction (widgets.py
context_menu, "Fatal Python error: Segmentation fault"). The app reaches its combo entries through the combo's roles
only (widgets.set_row_enabled, row_enabled, set_row_tip, set_row_heading), sorts by light by rewriting the rows, and
context_menu makes its own actions."""
import io
import os
from pathlib import Path
import re
import subprocess
import sys
import tokenize

import pytest

QtCore = pytest.importorskip("PySide6.QtCore", reason="PySide6 is not installed")
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QFont  # noqa: E402
from PySide6.QtWidgets import QComboBox  # noqa: E402

from sandbox_ai_app import widgets  # noqa: E402

APP = Path(__file__).resolve().parents[1]
SOURCES = APP / "sandbox_ai_app"

# The app's own light sort, combo.clear(), then a right-click menu of buttons, a text entry and a submenu - the
# user's crash, in the first rounds before the fix -; then combo entries greyed and read back the way the pages do.
# Every menu entry is a QAction, and no QStandardItem wrapper is left once the combo is cleared.
SCRIPT = r"""
import sys
sys.path.insert(0, %r)
import faulthandler
faulthandler.enable()
import shiboken6
from PySide6.QtGui import QAction, QStandardItem
from PySide6.QtWidgets import QApplication, QComboBox, QMenu, QPushButton, QWidget
app = QApplication([])
from sandbox_ai_app import model_profile_view as MPV, widgets
parent = QWidget()
combo = QComboBox(parent)
buttons = [QPushButton("button %%d" %% i, parent) for i in range(12)]
for number, button in enumerate(buttons):
    button.setEnabled(number %% 2 == 0)
types = set()


def right_click():
    menu = widgets.context_menu(parent, buttons + [None, ("text", False, "why", None), QMenu("sub", parent)])
    for action in menu.actions():
        types.add(type(action).__name__)
        action.setEnabled(action.isEnabled())
    menu.deleteLater()
    app.processEvents()


for round_ in range(150):
    for i in range(24):
        combo.addItem("model %%d" %% i, "model-%%d" %% i)
    MPV.order_by_light(combo, {i: ("green", "yellow", "red", "grey")[(i * 7) %% 4] for i in range(24)},
                       lambda index: combo.itemData(index))
    combo.clear()
    right_click()
for round_ in range(50):
    for i in range(24):
        combo.addItem("model %%d" %% i, "model-%%d" %% i)
    for i in range(0, 24, 3):
        widgets.set_row_enabled(combo, i, False)
    usable = [i for i in range(combo.count()) if widgets.row_enabled(combo, i)]
    assert len(usable) == 16, usable
    combo.clear()
    right_click()
stale = [w for w in shiboken6.getAllValidWrappers() if isinstance(w, QStandardItem)]
assert not stale, "QStandardItem wrappers left after combo.clear(): %%d" %% len(stale)
assert types == {"QAction"}, types
print("all QActions")
"""


def test_a_light_sort_then_a_cleared_combo_then_a_right_click_menu_runs_without_a_crash():
    """The reproduction (a process of its own: it segfaulted in context_menu, widgets.py, before the fix)."""
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "SANDBOX_AI_APP_LANGUAGE": "en"}
    done = subprocess.run([sys.executable, "-c", SCRIPT % str(APP)], env=env, capture_output=True, text=True,
                          timeout=240)
    assert done.returncode == 0 and "all QActions" in done.stdout, (done.returncode, done.stdout, done.stderr[-3000:])


def test_rows_are_greyed_and_read_through_the_roles(qapp):
    combo = QComboBox()
    combo.addItems(["a", "b", "c"])
    flags = lambda row: combo.model().flags(combo.model().index(row, 0))  # noqa: E731
    widgets.set_row_enabled(combo, 1, False)
    assert [widgets.row_enabled(combo, i) for i in range(3)] == [True, False, True]
    assert not flags(1) & Qt.ItemFlag.ItemIsEnabled and flags(1) & Qt.ItemFlag.ItemIsSelectable, "only enabled went"
    widgets.set_row_enabled(combo, 1, True)
    assert widgets.row_enabled(combo, 1)
    widgets.set_row_enabled(combo, 2, False, selectable=False)
    assert not flags(2) & Qt.ItemFlag.ItemIsSelectable
    assert not widgets.row_enabled(combo, 7) and not widgets.row_enabled(combo, -1), "no such row: not enabled"
    widgets.set_row_enabled(combo, 7, False)  # (no such row: nothing happens)
    widgets.set_row_tip(combo, 0, "<b>x</b>")
    assert combo.itemData(0, Qt.ItemDataRole.ToolTipRole) == widgets.safe_tooltip("<b>x</b>")
    widgets.set_row_heading(combo, 0)
    assert flags(0) == Qt.ItemFlag.NoItemFlags and not widgets.row_enabled(combo, 0)
    font = combo.itemData(0, Qt.ItemDataRole.FontRole)
    assert isinstance(font, QFont) and font.bold()


def test_the_light_sort_keeps_each_entry_whole(qapp):
    """order_by_light rewrites the rows (no takeRow/insertRow): text, data, tooltip, icon, flags and the chosen entry
    go with each one, and nothing is said about it."""
    from PySide6.QtGui import QColor, QIcon, QPixmap
    from sandbox_ai_app import model_profile_view as MPV
    combo = QComboBox()
    combo.addItem("default", None)
    for name in ("red one", "green one", "grey one"):
        combo.addItem(name, name.split()[0])
        combo.setItemData(combo.count() - 1, "tip " + name, Qt.ItemDataRole.ToolTipRole)
    pixmap = QPixmap(8, 8)
    pixmap.fill(QColor("red"))
    combo.setItemIcon(1, QIcon(pixmap))
    widgets.set_row_enabled(combo, 3, False)
    combo.setCurrentIndex(1)
    seen = []
    combo.currentIndexChanged.connect(seen.append)
    MPV.order_by_light(combo, {1: "red", 2: "green", 3: "grey"}, lambda index: combo.itemData(index))
    assert [combo.itemText(i) for i in range(4)] == ["default", "green one", "red one", "grey one"]
    assert [combo.itemData(i) for i in range(4)] == [None, "green", "red", "grey"]
    assert [combo.itemData(i, Qt.ItemDataRole.ToolTipRole) for i in range(1, 4)] == [
        "tip green one", "tip red one", "tip grey one"]
    assert [widgets.row_enabled(combo, i) for i in range(4)] == [True, True, True, False]
    assert [combo.itemIcon(i).isNull() for i in range(4)] == [True, True, False, True]
    assert combo.currentIndex() == 2 and combo.currentText() == "red one" and seen == [], "chosen stays, unsaid"


def test_the_right_click_menu_still_shows_each_entry(qapp):
    from PySide6.QtWidgets import QMenu, QPushButton, QWidget
    parent = QWidget()
    on, off = QPushButton("On", parent), QPushButton("Off", parent)
    off.setEnabled(False)
    off.setToolTip("why not")
    clicked = []
    on.clicked.connect(lambda: clicked.append("on"))
    sub = QMenu("More", parent)
    sub.addAction("Inner")
    menu = widgets.context_menu(parent, [None, on, None, None, off, ("Text", False, "<i>no</i>", None), sub, None])
    assert widgets.menu_entries(menu) == [("On", True), ("Off", False), ("Text", False), ("More", True),
                                          ("  Inner", True)]
    actions = menu.actions()
    assert [a.isSeparator() for a in actions] == [False, True, False, False, False]
    assert actions[2].toolTip() == "why not" and actions[3].toolTip() == widgets.safe_tooltip("<i>no</i>")
    actions[0].trigger()
    assert clicked == ["on"]
    assert all(a.parent() is menu for a in actions if not a.menu()), "the menu's own actions go with it"
    menu.deleteLater()
    parent.deleteLater()


# Calls that wrap a QStandardItem (or a combo's model's item): none in the app. ALLOWED: (file, line text) pairs
# proven safe, each with its reason - none needed so far.
WRAPPING = re.compile(r"\.model\(\)\.item\(|\bmodel\.item\(|takeRow\(|\bmodel\.insertRow\(|QStandardItem\b|"
                      r"itemFromIndex\(|invisibleRootItem\(")
ALLOWED = {}
STRINGS = {tokenize.STRING, tokenize.COMMENT} | {getattr(tokenize, name) for name in (
    "FSTRING_START", "FSTRING_MIDDLE", "FSTRING_END") if hasattr(tokenize, name)}


def code_lines(text):
    """{line number: its code} of the Python source TEXT, strings and comments left out (they may say why)."""
    lines = {}
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type not in STRINGS:
            lines[token.start[0]] = lines.get(token.start[0], "") + token.string
    return lines


def test_no_qstandarditem_wrapper_in_the_app_sources():
    found = []
    for path in sorted(SOURCES.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        source = text.splitlines()
        for number, code in sorted(code_lines(text).items()):
            line = source[number - 1].strip() if number <= len(source) else ""
            if WRAPPING.search(code) and (path.name, line) not in ALLOWED:
                found.append(f"{path.relative_to(APP)}:{number}: {line}")
    assert not found, "QStandardItem wrappers (use widgets.set_row_enabled, row_enabled, set_row_tip):\n" + \
        "\n".join(found)


def test_the_guard_finds_what_it_is_for():
    def caught(line):
        return any(WRAPPING.search(code) for code in code_lines(line + "\n").values())
    for line in ("combo.model().item(3).setEnabled(False)", "item = model.item(index)",
                 "rows = [model.takeRow(0) for _ in x]", "model.insertRow(first, row)",
                 "from PySide6.QtGui import QStandardItem", "x = QStandardItem('a')"):
        assert caught(line), line
    for line in ("self.table.item(row, 0)", "self.list.item(i).data(1)", "self.table.insertRow(row)",
                 "widgets.row_enabled(combo, i)", "from PySide6.QtGui import QStandardItemModel",
                 "f(1)  # model.item(i) went", '"""never takeRow, a QStandardItem"""'):
        assert not caught(line), line
