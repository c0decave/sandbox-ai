"""Two tabs of the Activity page for what a sandbox could leave for the host to run or trust (README "The workspace
review after each run", "MCP tool definitions pinned per project").

Workspace review: every change a run made to a file of its workspaces that the host runs or trusts (Git hooks and
configuration, editor tasks, agent settings, build scripts, CI workflows, instruction files), newest first, with its
diff and the hidden-text checker's findings below (`sandbox-ai workspace review --all --json`). Per file: Keep records
that the change is wanted; Restore… (asked first: workspace-restore) puts the file back as it was at the start, from
the before-state the launcher kept outside every sandbox (`workspace review RUN --log-dir DIR --restore PATH --yes`)
- a change without one (a binary or larger file, a run before 0.1.133) offers Keep only, and says why.

MCP tool pins: the tool definitions pinned for each project and server (`sandbox-ai mcp tools --all --json`), whether
the list the server gave last differs; a row unfolds its definitions and what differs below (`mcp tools NAME --project
DIR --json`). Re-pin… (asked first: mcp-pin-repin) makes the list seen last the pin; Forget… (mcp-pin-forget) drops
the pin. Every button is on the list's right-click menu too. Everything shown may quote the agent or a server: plain
text, control characters escaped. The commands run in the background (on the host the Activity page shows)."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush
from PySide6.QtWidgets import (
    QCheckBox, QHBoxLayout, QPlainTextEdit, QPushButton, QSplitter, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from . import tables, theme
from .files import printable, printable_text
from .i18n import N_, tr
from .widgets import MONO, ask_first, background, list_menu, plain_label, safe_tooltip, set_enabled

ROLE = Qt.ItemDataRole.UserRole + 41
CHANGES = {"added": N_("added"), "changed": N_("changed"), "removed": N_("removed")}
DECISIONS = {"kept": N_("decision|kept"), "restored": N_("restored")}
REVIEW_COLUMNS = (N_("Time"), N_("Run"), N_("Change"), N_("File"), N_("Why it matters"), N_("Decision"))
PIN_COLUMNS = (N_("Project"), N_("Server"), N_("Tools"), N_("Pinned"), N_("How"), N_("Last seen"))
PIN_CHANGES = {"new": N_("new"), "changed": N_("changed"), "gone": N_("gone")}


def short_time(value):
    text = str(value or "")
    return text[:19].replace("T", " ") if len(text) >= 19 else text


def change_key(change):
    """The file a change names, as `workspace review --keep|--restore` takes it: its path in the sandbox."""
    return f"{str(change.get('sandbox') or '').rstrip('/')}/{change.get('path')}"


def decision_text(change):
    decided = change.get("decision") if isinstance(change.get("decision"), dict) else {}
    if decided.get("decision") in DECISIONS:
        return f"{tr(DECISIONS[decided['decision']])} {short_time(decided.get('time'))}".strip()
    return tr("open") if change.get("restorable") else tr("open (Keep only)")


def review_rows(reviews, undecided_only=False):
    """[(review, change)] of REVIEWS (workspace review --all --json), newest first; UNDECIDED_ONLY: those without a
    decision."""
    rows = []
    for review in reversed([r for r in reviews or [] if isinstance(r, dict)]):
        for change in review.get("changes") or []:
            if not isinstance(change, dict):
                continue
            if undecided_only and isinstance(change.get("decision"), dict):
                continue
            rows.append((review, change))
    return rows


def review_detail(review, change):
    """Everything the review says of CHANGE, plain text (the agent wrote the file: escaped)."""
    lines = [f"{tr('File')}: {printable(change_key(change))}",
             f"{tr('On the host')}: {printable(str(change.get('workspace') or '').rstrip('/'))}/{printable(change.get('path'))}",
             f"{tr('Run')}: {printable(review.get('run'))} ({short_time(review.get('time'))} UTC)",
             f"{tr('Log folder')}: {printable(review.get('log_dir'))}",
             f"{tr('Why it matters')}: {printable(change.get('why'))}",
             f"{tr('Decision')}: {decision_text(change)}"]
    if not change.get("restorable"):
        lines.append(tr("Restore: not possible here - {why}; the start's checkpoint may have it (Sessions > "
                        "Checkpoints…)", why=printable(change.get("why_not") or tr("no before-state recorded"))))
    lines.append("")
    lines += [printable(line) for line in change.get("diff") or []]
    findings = change.get("findings") or []
    if findings:
        lines += ["", tr("Hidden-text findings:")] + [f"  {printable(line)}" for line in findings]
    return "\n".join(lines)


class WorkspaceReviewView(QWidget):
    """The workspace review of every run (newest first): a row unfolds its diff below; Keep and Restore… per file."""
    counted = Signal(int)  # changes without a decision, for the tab's title

    def __init__(self, sai):
        super().__init__()
        self.sai, self.host, self.loaded_once, self.token, self.reviews = sai, "local", False, 0, []
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self.refresh_button = QPushButton(tr("Refresh"))
        self.refresh_button.clicked.connect(lambda _=False: self.refresh())
        self.undecided = QCheckBox(tr("Only open ones"))
        self.undecided.setToolTip(tr("Only the changes nobody kept or restored yet"))
        self.undecided.toggled.connect(lambda _: self.show_reviews(self.reviews))
        self.status = plain_label(role="secondary", selectable=True, log="workspace-review")
        top.addWidget(self.refresh_button)
        top.addWidget(self.undecided)
        top.addWidget(self.status, 1)
        layout.addLayout(top)
        split = QSplitter(Qt.Orientation.Vertical)
        self.tree = QTreeWidget()
        self.tree.setAccessibleName(tr("Workspace review"))
        self.tree.setHeaderLabels([tr(title) for title in REVIEW_COLUMNS])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        tables.setup(self.tree, 4, (0, 1, 2, 5))
        self.tree.currentItemChanged.connect(lambda *_: self.show_details())
        lower = QWidget()
        lower_layout = QVBoxLayout(lower)
        lower_layout.setContentsMargins(0, 0, 0, 0)
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setFont(MONO)
        self.detail.setAccessibleName(tr("Details"))
        self.detail.setPlaceholderText(tr("Choose a change to see its diff and what the hidden-text checker found."))
        row = QHBoxLayout()
        self.keep_button = QPushButton(tr("Keep"))
        self.keep_button.setToolTip(tr("Record that this change is wanted (nothing is written)"))
        self.keep_button.clicked.connect(lambda _=False: self.keep())
        self.restore_button = QPushButton(tr("Restore…"))
        self.restore_button.setToolTip(tr("Put the file back as it was at the start of the run (asked first)"))
        self.restore_button.clicked.connect(lambda _=False: self.restore())
        row.addWidget(self.keep_button)
        row.addWidget(self.restore_button)
        row.addStretch(1)
        list_menu(self.tree, lambda: [self.keep_button, self.restore_button, None, self.refresh_button])
        lower_layout.addWidget(self.detail, 1)
        lower_layout.addLayout(row)
        split.addWidget(self.tree)
        split.addWidget(lower)
        split.setSizes([360, 240])
        layout.addWidget(split, 1)
        self.update_buttons()

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        if not self.loaded_once:
            self.refresh()

    def set_host(self, host):
        """The reviews of HOST from now on (the Activity page's host chooser): nothing of the host before stays."""
        host = host or "local"
        if host == self.host:
            return
        self.host, self.reviews = host, []
        self.token += 1
        self.tree.clear()
        self.detail.clear()
        self.status.setText("")
        self.update_buttons()
        if self.loaded_once:
            self.refresh()

    def refresh(self):
        self.loaded_once = True
        self.token += 1
        token = self.token
        self.status.setText(tr("Reading the workspace reviews…"))
        background(self.sai.workspace_reviews, self.host, owner=self, source=self.refresh_button, timeout=150,
                   done=lambda result, error: self.loaded(token, result, error))

    def loaded(self, token, result, error):
        if token != self.token:
            return
        if error is not None:
            self.status.setText(tr("Cannot read the workspace reviews: {error}", error=printable_text(error)))
            return
        self.show_reviews(result or [])

    def current(self):
        item = self.tree.currentItem()
        data = item.data(0, ROLE) if item is not None else None
        return data if isinstance(data, tuple) else (None, None)

    def show_reviews(self, reviews):
        self.reviews = list(reviews)
        _, chosen = self.current()
        chosen_key = (chosen.get("workspace"), chosen.get("path")) if chosen else None
        self.tree.clear()
        warn = QBrush(theme.color("warn"))
        rows = review_rows(self.reviews, self.undecided.isChecked())
        for review, change in rows:
            item = QTreeWidgetItem([short_time(review.get("time")), printable(review.get("run")),
                                    tr(CHANGES[change["change"]]) if change.get("change") in CHANGES
                                    else printable(change.get("change")),
                                    printable(change_key(change)), printable(change.get("why")),
                                    decision_text(change)])
            item.setData(0, ROLE, (review, change))
            for column in (3, 4):
                item.setToolTip(column, safe_tooltip(item.text(column)))
            if not isinstance(change.get("decision"), dict):
                item.setForeground(5, warn)
            self.tree.addTopLevelItem(item)
            if chosen_key and chosen_key == (change.get("workspace"), change.get("path")):
                self.tree.setCurrentItem(item)
        tables.fit_columns(self.tree)
        total = sum(len(r.get("changes") or []) for r in self.reviews)
        open_ = sum(1 for _, change in review_rows(self.reviews) if not isinstance(change.get("decision"), dict))
        self.status.setText(tr("{count} change(s) in {runs} run(s), {open} open", count=total, runs=len(self.reviews),
                               open=open_) if total else
                            tr("No run changed a file the host runs or trusts."))
        self.counted.emit(open_)
        self.show_details()

    def show_details(self):
        review, change = self.current()
        self.detail.setPlainText(review_detail(review, change) if change else "")
        self.update_buttons()

    def update_buttons(self):
        _, change = self.current()
        decided = (change or {}).get("decision") if isinstance((change or {}).get("decision"), dict) else None
        if change is None:
            for button in (self.keep_button, self.restore_button):
                set_enabled(button, False, tr("Choose a change"))
            return
        set_enabled(self.keep_button, decided is None, tr("Decided already: {decision}", decision=decision_text(change)))
        if decided is not None and decided.get("decision") == "restored":
            set_enabled(self.restore_button, False, tr("Restored already"))
        elif not change.get("restorable"):
            set_enabled(self.restore_button, False, tr("No before-state recorded: {why}. Keep it, or restore the "
                                                       "start's checkpoint", why=printable(change.get("why_not") or "")))
        else:
            set_enabled(self.restore_button, True)

    def decide(self, decision):
        review, change = self.current()
        if change is None:
            return
        self.status.setText(tr("Restoring {path}…", path=printable(change_key(change))) if decision == "restore" else
                            tr("Keeping {path}…", path=printable(change_key(change))))
        background(self.sai.workspace_review_decide, review["log_dir"], review["run"], change_key(change), decision,
                   self.host, owner=self, source=self.tree, timeout=150,
                   done=lambda result, error: self.decided(change, decision, error))

    def decided(self, change, decision, error):
        if error is not None:
            self.status.setText(tr("Not done: {error}", error=printable_text(error)))
            return
        self.status.setText(tr("{path}: restored as it was at the start", path=printable(change_key(change)))
                            if decision == "restore" else
                            tr("{path}: kept as the agent left it", path=printable(change_key(change))))
        self.refresh()

    def keep(self):
        self.decide("keep")

    def restore(self):
        review, change = self.current()
        if change is None or not change.get("restorable"):
            return
        what = (tr("{path} was added by the agent: restoring removes it.", path=printable(change_key(change)))
                if change.get("change") == "added" else
                tr("{path} is put back as it was at the start of run {run}; what the agent wrote there is replaced.",
                   path=printable(change_key(change)), run=printable(review.get("run"))))
        if not ask_first(self, "workspace-restore", tr("Restore {path}", path=printable(change_key(change))), what,
                         tr("Restore"), details="\n".join(printable(line) for line in change.get("diff") or [])):
            return
        self.decide("restore")


def seen_text(entry):
    if not entry.get("seen"):
        return tr("not seen since")
    if entry.get("differs"):
        return tr("{time}: {count} differ", time=short_time(entry["seen"]), count=entry["differs"])
    return tr("{time}: as pinned", time=short_time(entry["seen"]))


def pin_detail(entry, pin):
    """The definitions of a server's pin and what the list seen last changes, plain text (a server wrote them)."""
    lines = [f"{tr('Project')}: {printable(entry.get('project') or tr('(not named: pinned before 0.1.133)'))}",
             f"{tr('Server')}: {printable(entry.get('server'))}",
             f"{tr('Pinned')}: {short_time(entry.get('pinned'))} UTC ({printable(entry.get('how'))})",
             f"{tr('Last seen')}: {seen_text(entry)}"]
    if not isinstance(pin, dict):
        return "\n".join(lines)
    changes = [c for c in pin.get("changes") or [] if isinstance(c, dict)]
    if changes:
        lines += ["", tr("The list seen last differs from the pin:")]
        lines += [f"  {printable(c.get('tool'))}: {tr(PIN_CHANGES[c['change']]) if c.get('change') in PIN_CHANGES else printable(c.get('change'))}"
                  for c in changes]
    seen = ((pin.get("seen") or {}).get("tools") or {}) if isinstance(pin.get("seen"), dict) else {}
    for name, item in sorted((pin.get("tools") or {}).items()):
        lines += ["", f"{printable(name)}  sha256 {printable(str(item.get('digest'))[:16])}",
                  f"  {tr('pinned')}: {printable(item.get('definition'))}"]
        now = seen.get(name)
        if isinstance(now, dict) and now.get("digest") != item.get("digest"):
            lines.append(f"  {tr('seen last')}: {printable(now.get('definition'))}")
    for name, item in sorted(seen.items()):
        if name not in (pin.get("tools") or {}):
            lines += ["", f"{printable(name)}  ({tr('new')})", f"  {tr('seen last')}: {printable(item.get('definition'))}"]
    return "\n".join(lines)


class McpPinsView(QWidget):
    """The MCP tool definitions pinned per project and server; a row unfolds its definitions; Re-pin… and Forget…"""

    def __init__(self, sai):
        super().__init__()
        self.sai, self.host, self.loaded_once, self.token, self.detail_token = sai, "local", False, 0, 0
        self.entries = []
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self.refresh_button = QPushButton(tr("Refresh"))
        self.refresh_button.clicked.connect(lambda _=False: self.refresh())
        self.status = plain_label(role="secondary", selectable=True, log="mcp-pins")
        top.addWidget(self.refresh_button)
        top.addWidget(self.status, 1)
        layout.addLayout(top)
        split = QSplitter(Qt.Orientation.Vertical)
        self.tree = QTreeWidget()
        self.tree.setAccessibleName(tr("MCP tool pins"))
        self.tree.setHeaderLabels([tr(title) for title in PIN_COLUMNS])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        tables.setup(self.tree, 0, (1, 2, 3, 5))
        self.tree.currentItemChanged.connect(lambda *_: self.chosen())
        lower = QWidget()
        lower_layout = QVBoxLayout(lower)
        lower_layout.setContentsMargins(0, 0, 0, 0)
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setFont(MONO)
        self.detail.setAccessibleName(tr("Definitions"))
        self.detail.setPlaceholderText(tr("Choose a server to see its pinned tool definitions."))
        row = QHBoxLayout()
        self.show_button = QPushButton(tr("Definitions"))
        self.show_button.setToolTip(tr("Show the pinned definitions and the list seen last (sandbox-ai mcp tools NAME)"))
        self.show_button.clicked.connect(lambda _=False: self.load_detail())
        self.repin_button = QPushButton(tr("Re-pin…"))
        self.repin_button.setToolTip(tr("Make the list the server gave last its pin (asked first)"))
        self.repin_button.clicked.connect(lambda _=False: self.change("repin"))
        self.forget_button = QPushButton(tr("Forget…"))
        self.forget_button.setToolTip(tr("Drop the pin: the server's next list is pinned anew (asked first)"))
        self.forget_button.clicked.connect(lambda _=False: self.change("forget"))
        for button in (self.show_button, self.repin_button, self.forget_button):
            row.addWidget(button)
        row.addStretch(1)
        list_menu(self.tree, lambda: [self.show_button, self.repin_button, self.forget_button, None,
                                      self.refresh_button])
        lower_layout.addWidget(self.detail, 1)
        lower_layout.addLayout(row)
        split.addWidget(self.tree)
        split.addWidget(lower)
        split.setSizes([300, 300])
        layout.addWidget(split, 1)
        self.update_buttons()

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        if not self.loaded_once:
            self.refresh()

    def set_host(self, host):
        host = host or "local"
        if host == self.host:
            return
        self.host, self.entries = host, []
        self.token += 1
        self.detail_token += 1
        self.tree.clear()
        self.detail.clear()
        self.status.setText("")
        self.update_buttons()
        if self.loaded_once:
            self.refresh()

    def refresh(self):
        self.loaded_once = True
        self.token += 1
        token = self.token
        self.status.setText(tr("Reading the pinned MCP tool definitions…"))
        background(self.sai.mcp_tool_pins, self.host, owner=self, source=self.refresh_button, timeout=90,
                   done=lambda result, error: self.loaded(token, result, error))

    def loaded(self, token, result, error):
        if token != self.token:
            return
        if error is not None:
            self.status.setText(tr("Cannot read the MCP tool pins: {error}", error=printable_text(error)))
            return
        self.show_entries(result or [])

    def current(self):
        item = self.tree.currentItem()
        data = item.data(0, ROLE) if item is not None else None
        return data if isinstance(data, dict) else None

    def show_entries(self, entries):
        self.entries = list(entries)
        chosen = self.current()
        wanted = (chosen.get("project"), chosen.get("server")) if chosen else None
        self.tree.clear()
        warn = QBrush(theme.color("warn"))
        for entry in self.entries:
            item = QTreeWidgetItem([printable(entry.get("project") or tr("(not named)")), printable(entry.get("server")),
                                    str(entry.get("tools") if isinstance(entry.get("tools"), int) else ""),
                                    short_time(entry.get("pinned")), printable(entry.get("how")), seen_text(entry)])
            item.setData(0, ROLE, entry)
            for column in (0, 4):
                item.setToolTip(column, safe_tooltip(item.text(column)))
            if entry.get("differs"):
                item.setForeground(5, warn)
            self.tree.addTopLevelItem(item)
            if wanted == (entry.get("project"), entry.get("server")):
                self.tree.setCurrentItem(item)
        tables.fit_columns(self.tree)
        differ = sum(1 for entry in self.entries if entry.get("differs"))
        self.status.setText(tr("{count} pinned server(s), {differ} whose last list differs", count=len(self.entries),
                               differ=differ) if self.entries else
                            tr("No MCP tool definitions pinned yet (a server's first list is pinned after its run)."))
        self.chosen()

    def chosen(self):
        entry = self.current()
        self.detail.setPlainText(pin_detail(entry, None) if entry else "")
        self.update_buttons()
        if entry and entry.get("project"):
            self.load_detail()

    def load_detail(self):
        entry = self.current()
        if not entry or not entry.get("project"):
            return
        self.detail_token += 1
        token = self.detail_token
        background(self.sai.mcp_tool_pin, entry["project"], entry["server"], self.host, owner=self,
                   source=self.show_button, timeout=90,
                   done=lambda result, error: self.detail_loaded(token, entry, result, error))

    def detail_loaded(self, token, entry, result, error):
        if token != self.detail_token:
            return
        if error is not None:
            self.detail.setPlainText(pin_detail(entry, None) + "\n\n" + tr("Cannot read its definitions: {error}",
                                                                          error=printable_text(error)))
            return
        self.detail.setPlainText(pin_detail(entry, result))

    def update_buttons(self):
        entry = self.current()
        if entry is None:
            for button in (self.show_button, self.repin_button, self.forget_button):
                set_enabled(button, False, tr("Choose a server"))
            return
        named = bool(entry.get("project"))
        why = tr("Pinned before 0.1.133, without its project: use sandbox-ai mcp tools NAME --project DIR")
        set_enabled(self.show_button, named, why)
        set_enabled(self.forget_button, named, why)
        if not named:
            set_enabled(self.repin_button, False, why)
        elif not entry.get("seen"):
            set_enabled(self.repin_button, False, tr("No list was seen since it was pinned"))
        elif not entry.get("differs"):
            set_enabled(self.repin_button, False, tr("The list seen last is the pinned one"))
        else:
            set_enabled(self.repin_button, True)

    def change(self, action):
        entry = self.current()
        if not entry or not entry.get("project"):
            return
        names = {"server": printable(entry["server"]), "project": printable(entry["project"])}
        if action == "repin":
            asked = ask_first(self, "mcp-pin-repin", tr("Re-pin {server}", **names),
                              tr("Pin the list MCP server {server} gave last for {project}? Its later lists are "
                                 "compared with it: what changed in it is no longer asked about.", **names),
                              tr("Re-pin"), details=self.detail.toPlainText())
        else:
            asked = ask_first(self, "mcp-pin-forget", tr("Forget the pin of {server}", **names),
                              tr("Drop the pin of MCP server {server} for {project}? Its next list is pinned without "
                                 "a question.", **names), tr("Forget"))
        if not asked:
            return
        background(self.sai.mcp_pin_change, entry["project"], entry["server"], action, self.host, owner=self,
                   source=self.tree, timeout=90, done=lambda result, error: self.changed(entry, action, error))

    def changed(self, entry, action, error):
        if error is not None:
            self.status.setText(tr("Not done: {error}", error=printable_text(error)))
            return
        self.status.setText(tr("{server}: the list seen last is pinned now", server=printable(entry["server"]))
                            if action == "repin" else
                            tr("{server}: pin dropped", server=printable(entry["server"])))
        self.refresh()
