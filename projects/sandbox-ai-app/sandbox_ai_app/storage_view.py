"""Optimize… and Storage… (user, 2026-11-19): what sandbox-ai keeps on a host, and what could be freed - the storage
check's plan (`sandbox-ai gc --check --dry-run --json` on that host), each item with how much it frees, whether it is
safe while starts run, and its exact command; a checkbox per item and one Apply selected, asked first
(storage-apply), each item's own command run then (`gc --only ...`, `gc --pack --home NAME` ...: that and nothing
else). Storage… is the same for one start (`gc --check --home NAME`): its HOME by kind, the logs and checkpoints of
its project, its growth since the last check, and every action for it. Everything runs in the background; the
window never waits. Of this machine or a host of `sandbox-ai hosts` (over SSH, as the other remote actions)."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from . import applog, tables, widgets
from .files import printable, printable_text
from .i18n import N_, tr
from .name_fields import alive
from .widgets import ask_first, background, explanation, plain_label, safe_tooltip, set_enabled

KIND_TITLES = {"caches": N_("Empty the download caches (filled again when needed)"),
               "build": N_("Remove the build output and temp folders (made again by a build)"),
               "toolchains": N_("Remove the toolchains (installed again by their tools)"),
               "transcripts": N_("Remove old transcripts for good (never the newest)"),
               "pack": N_("Pack the HOME into one archive (unpacked again when resumed)"),
               "unpack": N_("Unpack the HOME now"),
               "compact": N_("Compact old transcripts in place (read as before)"),
               "logs": N_("Remove the old log files of its project"),
               "gc": N_("What Clean up… does (leftovers, logs compressed, expired starts)"),
               "images": N_("Remove sandbox-ai's Podman images without a name"),
               "log_folders": N_("Remove the log folders not written for half a year")}
# What is in a kept HOME, by kind (statedirs.HOME_KINDS; the same words as the Disk usage… dialog's)
HOME_KINDS = {"caches": N_("download caches"), "toolchains": N_("toolchains"), "agent": N_("agent state"),
              "build": N_("build output and temp"), "other": N_("other (the agent's work)")}
RULE_TITLES = {"caches": N_("emptied the caches of {name} ({size})"),
               "compact": N_("compacted transcripts of {name} ({size})"),
               "pack": N_("packed the HOME of {name} ({size})"),
               "logs": N_("removed the log folder {name} ({size})")}
COLUMNS = (N_("What"), N_("Frees"), N_("Name"), N_("While it runs"), N_("Command"))


def size_text(value):
    """VALUE in bytes as the app says sizes (files.size_text: 1.5 GiB, the language's decimal mark)."""
    from .files import size_text as text
    return text(value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0)


def plan_items(result):
    """The plan of a gc --check answer, each item checked as far as the app reads it (another host answers: only
    well-formed items, every text through printable)."""
    found = []
    for item in (result or {}).get("plan") or [] if isinstance(result, dict) else []:
        if not isinstance(item, dict) or not isinstance(item.get("argv"), list) or \
                not all(isinstance(part, str) for part in item["argv"]):
            continue
        size = item.get("bytes")
        found.append({"key": printable(str(item.get("key") or "")), "kind": str(item.get("kind") or ""),
                      "what": printable(str(item.get("what") or "")), "start": printable(str(item.get("start") or "")),
                      "bytes": size if isinstance(size, int) and not isinstance(size, bool) else 0,
                      "estimate": item.get("estimate") is True, "safe": item.get("safe_running") is True,
                      "checked": item.get("checked") is True, "argv": list(item["argv"]),
                      "command": printable(str(item.get("command") or ""))})
    return found


def summary_lines(result, name=None):
    """What the check says above its plan: what could be freed and the threshold, the growth, what the storage rules
    did; for one start (NAME) its HOME by kind, its project's logs and checkpoints."""
    result = result if isinstance(result, dict) else {}
    lines = []
    if name:
        starts = ((result.get("detail") or {}).get("starts") or [{}]) if isinstance(result.get("detail"), dict) \
            else [{}]
        item = starts[0] if starts and isinstance(starts[0], dict) else {}
        kinds = item.get("kinds") if isinstance(item.get("kinds"), dict) else {}
        lines.append(tr("Its HOME: {size} ({state})", size=size_text(item.get("bytes")),
                        state=printable(str(item.get("state") or "?"))))
        shown = [f"{tr(HOME_KINDS[kind])} {size_text(value)}" for kind, value in
                 sorted(kinds.items(), key=lambda pair: -(pair[1] if isinstance(pair[1], int) else 0))
                 if kind in HOME_KINDS and isinstance(value, int) and not isinstance(value, bool) and value]
        if shown:
            lines.append(tr("by kind: {kinds}", kinds=", ".join(shown)))
        packed = result.get("packed")
        if isinstance(packed, dict):
            lines.append(tr("packed into {size}: unpacked again when it is resumed",
                            size=size_text(packed.get("archive_bytes"))))
        logs = result.get("logs") if isinstance(result.get("logs"), dict) else {}
        lines.append(tr("The logs of its project: {size}; {count} file(s) not written for {days} days ({old})",
                        size=size_text(logs.get("bytes")), count=int(logs.get("old_files") or 0),
                        days=int(logs.get("days") or 0), old=size_text(logs.get("old_bytes"))))
        checks = result.get("checkpoints") if isinstance(result.get("checkpoints"), dict) else {}
        lines.append(tr("The workspace checkpoints of its project: {count}, {size}",
                        count=int(checks.get("count") or 0), size=size_text(checks.get("bytes"))))
        compacted = result.get("compacted") if isinstance(result.get("compacted"), dict) else {}
        if compacted.get("files"):
            lines.append(tr("Compacted transcripts: {count} ({size} saved)", count=int(compacted["files"]),
                            size=size_text(compacted.get("saved"))))
        for entry in result.get("starts") or []:
            if isinstance(entry, dict) and isinstance(entry.get("growth"), int):
                lines.append(tr("Since the check of {time}: {growth}", time=printable(str(entry.get("since") or ""))
                                .replace("T", " ").replace("Z", ""), growth=growth_text(entry["growth"])))
    else:
        lines.append(tr("{size} could be freed (the notice comes above {threshold}).",
                        size=size_text(result.get("total")), threshold=size_text(result.get("threshold"))))
        for entry in result.get("grew") or []:
            if isinstance(entry, dict):
                lines.append(tr("{name} grew by {size} since the last check.", name=printable(str(entry.get("name"))),
                                size=size_text(entry.get("bytes"))))
    said = result.get("said") if isinstance(result.get("said"), list) else []
    if said:
        lines.append(tr("The storage rules did: {what}", what="; ".join(
            tr(RULE_TITLES[action["rule"]], name=printable(str(action.get("name"))), size=size_text(action.get("bytes")))
            if action.get("rule") in RULE_TITLES else printable(str(action.get("name")))
            for action in said[:8] if isinstance(action, dict))))
    return lines


def growth_text(value):
    value = int(value or 0)
    return ("+" if value >= 0 else "-") + size_text(abs(value))


class StorageDialog(QDialog):
    """The storage check's plan on HOST (NAME: the storage of that start alone), a checkbox per item and Apply
    selected."""

    def __init__(self, parent, sai, host="local", name=None, owner=None, applied=None):
        super().__init__(parent)
        self.sai, self.host, self.name, self.owner, self.applied = sai, host or "local", name, owner, applied
        self.busy, self.items, self.result = False, [], None
        where = tr("this machine") if self.host == "local" else printable(self.host)
        self.setWindowTitle(tr("Storage of {name} on {host}", name=printable(name), host=where) if name else
                            tr("Optimize on {host}", host=where))
        layout = QVBoxLayout(self)
        layout.addWidget(explanation(tr(
            "What could be freed, each with how much, whether it is safe while starts run, and the exact command "
            "(hover a row). Tick what to do and Apply selected: it is asked first, then each command runs on its "
            "own. Nothing is removed before.") if not name else tr(
            "The storage of this start: its HOME by kind, the logs and checkpoints of its project, its growth since "
            "the last check, and what can be done with it - each with the expected saving and the exact command.")))
        self.summary = plain_label("", role="secondary", selectable=True)
        layout.addWidget(self.summary)
        self.tree = QTreeWidget()
        self.tree.setAccessibleName(tr("What could be freed"))
        self.tree.setHeaderLabels([tr(title) for title in COLUMNS])
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        tables.setup(self.tree, 0, (1,))
        self.tree.itemChanged.connect(lambda *_: self.update_buttons())
        layout.addWidget(self.tree, 1)
        self.status = plain_label("", role="secondary", selectable=True)
        layout.addWidget(self.status)
        row = QHBoxLayout()
        self.apply_button = QPushButton(tr("Apply selected…"))
        self.apply_button.setAccessibleName(tr("Apply selected"))
        self.apply_button.setToolTip(safe_tooltip(tr(
            "Run the command of each ticked item, one after another, on {host} - asked first", host=where)))
        self.apply_button.clicked.connect(lambda _=False: self.apply())
        self.all_button = QPushButton(tr("Tick all"))
        self.all_button.setToolTip(safe_tooltip(tr("Tick every item of the list")))
        self.all_button.clicked.connect(lambda _=False: self.tick_all(True))
        self.none_button = QPushButton(tr("Tick none"))
        self.none_button.setToolTip(safe_tooltip(tr("Untick every item of the list")))
        self.none_button.clicked.connect(lambda _=False: self.tick_all(False))
        self.refresh_button = QPushButton(tr("Check again"))
        self.refresh_button.setToolTip(safe_tooltip(tr("Measure again ({command})", command="sandbox-ai gc --check "
                                                                                          "--dry-run")))
        self.refresh_button.clicked.connect(lambda _=False: self.load())
        for button in (self.apply_button, self.all_button, self.none_button):
            row.addWidget(button)
        row.addStretch(1)
        row.addWidget(self.refresh_button)
        layout.addLayout(row)
        widgets.list_menu(self.tree, lambda: [self.apply_button, self.all_button, self.none_button, None,
                                              self.refresh_button])
        buttons = QDialogButtonBox()
        close = QPushButton(tr("Close"))
        close.clicked.connect(lambda _=False: self.reject())
        buttons.addButton(close, QDialogButtonBox.ButtonRole.RejectRole)
        layout.addWidget(buttons)
        self.resize(900, 520)
        self.update_buttons()
        self.load()

    # --- what is ticked -------------------------------------------------------------------------------------------

    def ticked(self):
        found = []
        for index in range(self.tree.topLevelItemCount()):
            row = self.tree.topLevelItem(index)
            if row.checkState(0) == Qt.CheckState.Checked:
                found.append(self.items[index])
        return found

    def tick_all(self, on):
        for index in range(self.tree.topLevelItemCount()):
            self.tree.topLevelItem(index).setCheckState(0, Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
        self.update_buttons()

    def update_buttons(self):
        if not alive(self):
            return
        why = tr("A command runs.") if self.busy else None
        none = why or (tr("Tick what to do first.") if not self.ticked() else None)
        set_enabled(self.apply_button, none is None, none)
        for button in (self.all_button, self.none_button):
            set_enabled(button, why is None and bool(self.items), why or tr("Nothing to choose."))
        set_enabled(self.refresh_button, why is None, why)

    # --- reading ------------------------------------------------------------------------------------------------

    def load(self):
        self.busy = True
        self.status.setText(tr("Measuring… (sandbox-ai gc --check)"))
        self.update_buttons()
        background(lambda: self.sai.storage_check(self.host, self.name, dry_run=True), owner=self.owner,
                   what="sandbox-ai gc --check --dry-run", timeout=900, done=self.loaded)

    def loaded(self, result, error):
        if not alive(self):
            return
        self.busy = False
        if error is not None:
            self.status.setText(tr("Not measured: {error}", error=printable_text(error)))
            self.update_buttons()
            return
        self.result = result
        self.items = plan_items(result)
        self.summary.setText("\n".join(summary_lines(result, self.name)))
        self.tree.blockSignals(True)
        self.tree.clear()
        for item in self.items:
            row = QTreeWidgetItem([tr(KIND_TITLES[item["kind"]]) if item["kind"] in KIND_TITLES else item["what"],
                                   (tr("about {size}", size=size_text(item["bytes"])) if item["estimate"]
                                    else size_text(item["bytes"])), item["start"] or "-",
                                   tr("safe") if item["safe"] else tr("only while it is stopped"), item["command"]])
            row.setFlags(row.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            row.setCheckState(0, Qt.CheckState.Checked if item["checked"] else Qt.CheckState.Unchecked)
            tip = safe_tooltip(f"{item['what']}\n{item['command']}")
            for column in range(len(COLUMNS)):
                row.setToolTip(column, tip)
            row.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.tree.addTopLevelItem(row)
        self.tree.blockSignals(False)
        tables.fit_columns(self.tree, force=True)
        self.status.setText(tr("Nothing could be freed.") if not self.items else
                            tr("{count} item(s); the biggest first. Hover one for its command.",
                               count=len(self.items)))
        self.update_buttons()

    # --- applying -----------------------------------------------------------------------------------------------

    def apply(self):
        chosen = self.ticked()
        if not chosen or self.busy:
            return
        lines = [f"• {tr(KIND_TITLES[item['kind']]) if item['kind'] in KIND_TITLES else item['what']} "
                 f"({item['start'] or '-'}): "
                 f"{size_text(item['bytes'])}\n    {item['command']}" for item in chosen]
        where = tr("this machine") if self.host == "local" else printable(self.host)
        text = tr("Run these {count} command(s) on {host}? What they remove is gone for good (caches and build output "
                  "are made again when needed; compacted transcripts and packed HOMEs are read as before).",
                  count=len(chosen), host=where) + "\n\n" + "\n".join(lines)
        if not ask_first(self, "storage-apply", tr("Apply selected"), text, tr("Apply")):
            self.status.setText(tr("Nothing changed."))
            return
        applog.log("INFO", "storage", f"apply on {printable(self.host)}: "
                   + "; ".join(item["command"] for item in chosen))
        self.busy = True
        self.status.setText(tr("Applying {count} item(s)…", count=len(chosen)))
        self.update_buttons()
        host, argvs = self.host, [item["argv"] for item in chosen]

        def run():
            done = []
            for argv in argvs:
                try:
                    self.sai.storage_apply(host, argv)
                    done.append((argv, None))
                except (OSError, ValueError, TimeoutError) as exc:
                    done.append((argv, str(exc)))
            return done
        background(run, owner=self.owner, work=True, what="sandbox-ai gc (the storage check's plan)", timeout=None,
                   done=self.applied_done)

    def applied_done(self, result, error):
        if not alive(self):
            return
        self.busy = False
        if error is not None:
            self.status.setText(tr("Not applied: {error}", error=printable_text(error)))
            self.update_buttons()
            return
        failed = [(argv, why) for argv, why in result if why]
        self.status.setText(tr("Applied {count} item(s).", count=len(result) - len(failed)) + "".join(
            "\n" + tr("Not done: {command}: {error}", command=printable(" ".join(argv)), error=printable_text(why))
            for argv, why in failed))
        if callable(self.applied):
            self.applied(self.host)
        self.load()


def open_dialog(parent, sai, host="local", name=None, owner=None, applied=None):
    dialog = StorageDialog(parent, sai, host, name, owner=owner, applied=applied)
    dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    dialog.show()
    return dialog
