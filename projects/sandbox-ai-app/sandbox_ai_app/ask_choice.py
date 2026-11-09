"""--ask in words: the Start page's chooser, the Board's approvals part, the beginner guide, the project trust question
and the Sandboxes page. on asks you (the default), off refuses without asking, allow asks nothing and allows every
question at once (sandbox-ai 0.1.80 and later), marked with a warning. The value stays the launcher's own (on, off,
allow); only what is shown is worded. The web page shows the same words (sandbox/web/app.js, ASK_LABELS)."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox

from .i18n import N_, tr

OPTION = "--ask"
WARN = "⚠"
LABELS = {"on": N_("Ask me (default)"), "off": N_("Refuse without asking"), "allow": N_("Always allow everything")}
WARNED = ("allow",)  # weakens the sandbox: marked, its label in the warning colour
ALLOW_HELP = N_("Nothing is asked: every question is allowed at once, for the run, and recorded in the approvals "
                "history and the connection log. What rules deny, the privacy check's blocking findings and a "
                "canary's alarm stay as they are.")
BADGE = N_("asks nothing, allows every question")  # a start with --ask allow, after its state
TRUST = N_("asks nothing: every question is allowed at once")  # --ask allow in a project file
SINCE = (0, 1, 80)  # the sandbox-ai that knows --ask allow


def label(value):
    """VALUE of --ask as shown: its words in the current language, a warning sign before allow."""
    text = tr(LABELS[value]) if value in LABELS else str(value)
    return f"{WARN} {text}" if value in WARNED else text


def warned(option, value):
    """True when OPTION has the VALUE that asks nothing (--ask allow)."""
    return option == OPTION and value in WARNED


def allows_all(entry):
    """True for a start (sandbox-ai list --json entry) that runs with --ask allow."""
    return isinstance(entry, dict) and entry.get("ask") == "allow"


def in_options(options):
    """True when the start options OPTIONS (as on a command line, up to --) ask for --ask allow, however written."""
    words = [str(word) for word in options]
    if "--" in words:
        words = words[:words.index("--")]
    return any(word in ("--ask=allow", "-Q=allow") or (word in ("--ask", "-Q") and following == "allow")
               for word, following in zip(words, words[1:] + [""]))


class AskChoice(QComboBox):
    """--ask's values, each in words (label). Like every choice of the forms it speaks in values: currentText(),
    setCurrentText(), itemText() and findText() take the value itself (on, off, allow), whatever is shown. Allow is
    marked, and explained in its entry's tooltip (the box's own tooltip stays the option's help)."""

    def __init__(self, choices, default=None, parent=None):
        super().__init__(parent)
        for value in [str(choice) for choice in choices]:
            self.addItem(label(value), value)
            if value in WARNED:
                self.setItemData(self.count() - 1, tr(ALLOW_HELP), Qt.ItemDataRole.ToolTipRole)
        if default is not None:
            self.setCurrentText(str(default))

    def currentText(self):  # noqa: N802 - Qt's name
        value = self.currentData()
        return super().currentText() if value is None else str(value)

    def setCurrentText(self, text):  # noqa: N802
        index = self.findData(str(text))
        if index >= 0:
            self.setCurrentIndex(index)

    def itemText(self, index):  # noqa: N802
        value = self.itemData(index)
        return super().itemText(index) if value is None else str(value)

    def findText(self, text, flags=Qt.MatchFlag.MatchExactly):  # noqa: N802
        return self.findData(str(text))
