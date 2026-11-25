"""The menu Install (todos 2026-11-24: "ein eigenes install menu oben in der menueleiste ... alles oder nichts oder
nur teile zu bauen"): a checklist of what starts need on this machine or a host - the agents' images, the MCP servers of
the signed catalogs, the toolchains of the signed catalog, what each preset needs - with what is installed and what
each costs (its size where known), All and None, and Install…, which runs `sandbox-ai install ITEM...` there in a
terminal tab of the app: each item's own command (build, mcp add, toolchain add) one after the other, its progress
("[2/5] ...") and its questions there - a pending MCP server's build is asked by mcp add --build itself. Signed things
only: sandbox-ai offers nothing else (install.py)."""
from __future__ import annotations

import json

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QPushButton, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout)

from .files import printable, printable_text
from .host_choice import LOCAL, HostChooser, host_label
from .i18n import N_, tr
from .widgets import background, confirm, explanation, plain_label, safe_tooltip, set_notice

KINDS = (("images", N_("Agent images")), ("mcp", N_("MCP servers")), ("toolchains", N_("Toolchains")),
         ("presets", N_("What presets need")))
# The menu's entries: (key, text, kind or None for every kind, whether everything --all takes is ticked at once).
MENU = (("all", N_("Install…"), None, False),
        ("images", N_("Agent images…"), "images", False), ("mcp", N_("MCP servers…"), "mcp", False),
        ("toolchains", N_("Toolchains…"), "toolchains", False),
        ("presets", N_("What presets need…"), "presets", False),
        ("everything", N_("Everything signed…"), None, True))
MENU_TIPS = {"all": N_("A checklist of what starts need: tick all, nothing or a part, then Install… (sandbox-ai "
                       "install)"),
             "everything": N_("The checklist with everything ticked that is signed and not installed yet (sandbox-ai "
                              "install --all): look, untick, then Install…")}
LIST_SECONDS = 300  # the catalogs are read over the network (on a host: over SSH too)
ID_ROLE = Qt.ItemDataRole.UserRole


def size_text(size):
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1000 or unit == "GB":
            return f"{size} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1000
    return ""


def state_text(row):
    if row.get("pending") and not row.get("installed"):
        return tr("pending (built after a question)")
    return {True: tr("installed"), False: tr("not installed"), None: tr("not known")}.get(row.get("installed"), "")


def install_argv(sai, host, items):
    """argv of `sandbox-ai install ITEMS` on HOST in a terminal (ssh -tt for a host: its questions are asked there)."""
    return sai.command("install", *items) if host in (None, LOCAL) else sai.host_terminal(host, "install", *items)


class InstallDialog(QDialog):
    """The checklist: HOST chosen at the top, the items by kind (KIND: that kind unfolded, the others folded; EVERY:
    what --all takes ticked at once), All / None, and Install…, which hands the ticked ids to RUN(argv, title, ended)
    - the window's in_terminal - and reads the list again when it ends."""

    def __init__(self, parent, sai, run, kind=None, every=False, host=LOCAL):
        super().__init__(parent)
        self.sai, self.run, self.kind, self.every = sai, run, kind, every
        self.rows, self.reading, self.generation = [], None, 0
        self.setWindowTitle(tr("Install"))
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self.hosts = HostChooser(sai, tooltip=tr("Where to install: this machine, or a host of sandbox-ai hosts"))
        self.hosts.set_host(host)
        self.hosts.changed.connect(lambda _host: self.refresh())
        self.refresh_button = QPushButton(tr("Refresh"))
        self.refresh_button.setAccessibleName(tr("Read the list again"))
        self.refresh_button.clicked.connect(lambda _=False: self.refresh())
        top.addWidget(self.hosts, 1)
        top.addWidget(self.refresh_button)
        layout.addLayout(top)
        layout.addWidget(explanation(tr("Tick what to install. Each item runs its own command (build, mcp add, "
                                        "toolchain add) with its checks, in a terminal tab; only signed entries are "
                                        "offered.")))
        self.tree = QTreeWidget()
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels([tr("Name"), tr("State"), tr("Size"), tr("Note")])
        self.tree.setAccessibleName(tr("What can be installed"))
        self.tree.itemChanged.connect(lambda _item, column: self.update_state() if column == 0 else None)
        layout.addWidget(self.tree, 1)
        self.status = plain_label(role="secondary", selectable=True)
        layout.addWidget(self.status)
        row = QHBoxLayout()
        self.all_button = QPushButton(tr("All"))
        self.all_button.setToolTip(safe_tooltip(tr("Tick everything signed that is not installed yet (as sandbox-ai "
                                                   "install --all: not a pending MCP server, nor one that needs a "
                                                   "secret)")))
        self.all_button.clicked.connect(lambda _=False: self.tick(every=True))
        self.none_button = QPushButton(tr("None"))
        self.none_button.clicked.connect(lambda _=False: self.tick(every=False))
        self.install_button = QPushButton(tr("Install…"))
        self.install_button.clicked.connect(lambda _=False: self.install())
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        row.addWidget(self.all_button)
        row.addWidget(self.none_button)
        row.addStretch()
        row.addWidget(self.install_button)
        row.addWidget(buttons)
        layout.addLayout(row)
        self.resize(900, 620)
        self.update_state()
        self.refresh()

    def host(self):
        return self.hosts.host() or LOCAL

    def refresh(self):
        """`sandbox-ai install --list --json` on the host chosen, in the background."""
        self.generation += 1
        token, host = self.generation, self.host()
        set_notice(self.status, tr("Reading what {host} has and what the catalogs offer…", host=host_label(host)))
        self.reading = background(lambda: self.sai.host_run(host, "install", "--list", "--json",
                                                            timeout=LIST_SECONDS),
                                  source=self.refresh_button, owner=self, timeout=LIST_SECONDS + 30,
                                  what=f"sandbox-ai install --list on {host}",
                                  done=lambda result, error: self.listed(token, host, result, error))

    def listed(self, token, host, result, error):
        if token != self.generation:
            return
        self.reading = None
        status, out, err = result if error is None else (1, "", str(error))
        try:
            found = json.loads(out) if status == 0 else None
        except (ValueError, RecursionError):
            found = None
        if not isinstance(found, dict):
            said = printable_text((err or out or "").strip())[-600:]
            if "invalid choice" in said or "install" in said and "unknown" in said.lower():
                said = tr("sandbox-ai on {host} has no install command yet: update it (Hosts page)",
                          host=host_label(host))
            self.show_rows([])
            set_notice(self.status, tr("Not read: {error}", error=said), "error")
            return
        self.show_rows([r for r in found.get("items") or [] if isinstance(r, dict) and isinstance(r.get("id"), str)])
        problems = [printable_text(str(p)) for p in found.get("problems") or []]
        if self.every:
            self.tick(every=True)
            self.every = False  # (once: a refresh keeps what was ticked since)
        set_notice(self.status, "\n".join(problems), "warn" if problems else None)
        self.update_state()

    def show_rows(self, rows):
        ticked = set(self.chosen())
        self.rows = rows
        self.tree.blockSignals(True)
        self.tree.clear()
        for kind, title in KINDS:
            mine = [r for r in rows if r.get("kind") == kind]
            if not mine:
                continue
            known = all(r.get("installed") is not None for r in mine)
            group = QTreeWidgetItem([tr(title), tr("{count} installed of {total}", count=sum(
                1 for r in mine if r.get("installed")), total=len(mine)) if known else tr("{total}, state not known",
                                                                                        total=len(mine)), "", ""])
            group.setFlags(group.flags() | Qt.ItemFlag.ItemIsAutoTristate | Qt.ItemFlag.ItemIsUserCheckable)
            group.setCheckState(0, Qt.CheckState.Unchecked)
            self.tree.addTopLevelItem(group)
            for row in mine:
                note = row.get("note") or ""
                if kind == "presets":
                    note = tr("needs {list}", list=", ".join(row.get("needs") or []))
                item = QTreeWidgetItem([printable(row["id"]), state_text(row), size_text(row.get("size")),
                                        printable(note)])
                item.setData(0, ID_ROLE, row["id"])
                item.setToolTip(0, safe_tooltip(printable(str(row.get("title") or row["id"]))))
                item.setToolTip(3, safe_tooltip(printable(note)))
                if row.get("installed"):
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable & ~Qt.ItemFlag.ItemIsEnabled)
                else:
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    item.setCheckState(0, Qt.CheckState.Checked if row["id"] in ticked else Qt.CheckState.Unchecked)
                group.addChild(item)
            group.setExpanded(self.kind in (None, kind) and (self.kind is not None or kind != "presets"))
        self.tree.blockSignals(False)
        for column in range(3):
            self.tree.resizeColumnToContents(column)
        self.update_state()

    def items(self):
        """The rows' tree items: [(item, row)]."""
        rows = {r["id"]: r for r in self.rows}
        found = []
        for index in range(self.tree.topLevelItemCount()):
            group = self.tree.topLevelItem(index)
            for child in range(group.childCount()):
                item = group.child(child)
                if item.data(0, ID_ROLE) in rows:
                    found.append((item, rows[item.data(0, ID_ROLE)]))
        return found

    def chosen(self):
        """The ids ticked, in the list's order."""
        return [row["id"] for item, row in self.items() if not row.get("installed")
                and item.checkState(0) == Qt.CheckState.Checked]

    def tick(self, every):
        """EVERY: what --all takes ticked (everything signed not installed yet, of the kind shown if one was chosen);
        else nothing."""
        self.tree.blockSignals(True)
        for item, row in self.items():
            if not row.get("installed"):
                wanted = every and row.get("all") and row.get("kind") != "presets" and self.kind in (None, row["kind"])
                item.setCheckState(0, Qt.CheckState.Checked if wanted else Qt.CheckState.Unchecked)
        self.tree.blockSignals(False)
        for index in range(self.tree.topLevelItemCount()):  # (the groups' tristate follows their children)
            group = self.tree.topLevelItem(index)
            if group.childCount():
                group.child(0).emitDataChanged()
        self.update_state()

    def update_state(self):
        chosen = self.chosen()
        rows = {r["id"]: r for r in self.rows}
        sizes = [rows[i]["size"] for i in chosen if isinstance(rows[i].get("size"), int)]
        self.install_button.setText(tr("Install {count}…", count=len(chosen)) if chosen else tr("Install…"))
        self.install_button.setEnabled(bool(chosen))
        self.install_button.setToolTip(safe_tooltip(tr("Tick something first") if not chosen else tr(
            "{count} chosen; known sizes together: {size}", count=len(chosen), size=size_text(sum(sizes)) or "-")))

    def install(self):
        chosen = self.chosen()
        if not chosen:
            return
        host = self.host()
        rows = {r["id"]: r for r in self.rows}
        pending = [i for i in chosen if rows[i].get("pending")]
        text = tr("Install these on {host}, one after the other, in a terminal tab?\n\n{list}",
                  host=host_label(host), list="\n".join(f"• {printable(i)}" for i in chosen[:40])
                  + ("\n…" if len(chosen) > 40 else ""))
        if pending:
            text += "\n\n" + tr("Pending MCP servers ({list}) have no signed image yet: each is built there from its "
                                "reviewed source, unsigned, only after its own question in the terminal.",
                                list=", ".join(printable(p) for p in pending))
        if not confirm(self, tr("Install"), text, tr("Install")):
            return
        argv = install_argv(self.sai, host, chosen)
        self.run(argv, tr("Install on {host}", host=host_label(host)), lambda _status: self.refresh_if_open())

    def refresh_if_open(self):
        try:
            if self.isVisible():
                self.refresh()
        except RuntimeError:  # (closed and deleted meanwhile)
            pass


def open_dialog(window, kind=None, every=False):
    """The Install menu's entries: the checklist for KIND (None: every kind), EVERY: what --all takes ticked."""
    from . import widgets
    dialog = InstallDialog(window, window.sai, lambda argv, title, ended: window.in_terminal(argv, title,
                                                                                            ended=ended),
                           kind=kind, every=every)
    window.install_dialog = dialog
    widgets.show_modeless(dialog)
    return dialog
