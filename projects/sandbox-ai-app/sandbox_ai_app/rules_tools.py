"""Help beside a field of ruleset files (--rules FILE, on the Board and the Start page): what to enter (a
placeholder and an example in the tooltip), New from example… (a starter ruleset written to a file outside every
workspace, then added) and Check (the launcher's own dry run reads the files: their rules, or why it refuses them).

A ruleset allows, asks or denies each HTTP request and MCP tool call (README, "Rulesets"). The launcher refuses a
ruleset inside a workspace (the agent could change its own rules), so the starter is written outside them."""
from __future__ import annotations

import json
import os
import shutil
import tempfile

from PySide6.QtWidgets import QHBoxLayout, QPushButton, QVBoxLayout, QWidget

from .files import printable
from .i18n import tr
from .state import choose_save, current
from .widgets import background, plain_label, safe_tooltip, warn

# The README's example: --allow-github opens every request to api.github.com, these keep out gists and issue writes
# and ask before a release; the MCP rules show the format for a server named "files" (they hold only when it runs).
EXAMPLE = {
    "version": 1,
    "description": "Starter ruleset of the sandbox-ai app: GitHub without gists and issue writes, releases asked; "
                   "a server named files may read, writes go to /work/src, deletes are asked. Change it to your needs.",
    "http": [
        {"id": "no-gists", "host": "api.github.com", "path": "/gists/**", "decision": "deny",
         "reason": "gists can publish workspace data"},
        {"id": "no-issue-writes", "host": "api.github.com", "methods": ["POST", "PATCH", "PUT", "DELETE"],
         "path": "/repos/*/*/issues/**", "decision": "deny", "reason": "no issues or comments in your name"},
        {"id": "releases", "host": "api.github.com", "methods": "POST", "path": "/repos/*/*/releases",
         "decision": "ask", "reason": "a release is asked first"},
    ],
    "mcp": [
        {"id": "files-reads", "server": "files", "tool": ["read_file", "list_directory"], "decision": "allow",
         "reason": "an example for an MCP server named files: reading is fine"},
        {"id": "files-writes", "server": "files", "tool": "write_file",
         "args": {"path": {"path_glob": "/work/src/**"}}, "decision": "allow", "reason": "writes only below /work/src"},
        {"id": "files-deletes", "server": "files", "tool": "delete_file", "decision": "ask"},
    ],
}
EXAMPLE_TIP = """{"version": 1, "http": [
  {"id": "no-gists", "host": "api.github.com", "path": "/gists/**", "decision": "deny"},
  {"id": "no-issue-writes", "host": "api.github.com", "methods": ["POST", "PATCH", "PUT", "DELETE"],
   "path": "/repos/*/*/issues/**", "decision": "deny"},
  {"id": "releases", "host": "api.github.com", "methods": "POST", "path": "/repos/*/*/releases",
   "decision": "ask"}]}"""
CONFIG_DIR = "~/.config/sandbox-ai"


def placeholder():
    return tr("~/.config/sandbox-ai/github.json — JSON: allow, ask or deny per HTTP request and MCP tool call")


def tooltip():
    return safe_tooltip(tr("One ruleset file per line, outside every workspace. A ruleset allows, asks or denies each "
                           "HTTP request and MCP tool call; the strictest decision of all rulesets wins. For example, "
                           "GitHub without gists and issue writes, a release asked first:") + "\n\n" + EXAMPLE_TIP)


def inside(path, folders):
    """The folder of FOLDERS that PATH lies in (links resolved), or None."""
    real = os.path.realpath(os.path.expanduser(str(path)))
    for folder in folders or []:
        if not folder:
            continue
        top = os.path.realpath(os.path.expanduser(str(folder)))
        try:
            if os.path.commonpath([real, top]) == top:
                return folder
        except ValueError:
            continue
    return None


def write_example(path):
    """The starter ruleset written to PATH (its folder made, private); an existing file is replaced (the save
    dialog asked first); a link is not followed."""
    path = os.path.expanduser(str(path))
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(EXAMPLE, indent=2) + "\n")
    return path


class RulesetTools(QWidget):
    """New from example… and Check below a field of ruleset files. PATHS() gives the files listed, ADD(path) adds one,
    WORKSPACES() the start's workspaces (a ruleset must lie outside them); LOCAL() whether the start is on this
    machine (another host's files are its own: neither button works there)."""

    def __init__(self, sai, paths, add, workspaces, local=lambda: True, agent=lambda: "claude"):
        super().__init__()
        self.sai, self.paths, self.add, self.workspaces, self.local, self.agent = sai, paths, add, workspaces, local, agent
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.new_button = QPushButton(tr("New from example…"))
        self.new_button.setToolTip(tr("Write a starter ruleset (GitHub without gists and issue writes) to a file "
                                      "outside every workspace and add it"))
        self.new_button.clicked.connect(lambda _=False: self.new_from_example())
        self.check_button = QPushButton(tr("Check"))
        self.check_button.setToolTip(tr("Ask the launcher (a dry run) whether it reads these ruleset files"))
        self.check_button.clicked.connect(lambda _=False: self.check())
        row.addWidget(self.new_button)
        row.addWidget(self.check_button)
        row.addStretch(1)
        layout.addLayout(row)
        self.result = plain_label(role="secondary")
        self.result.setWordWrap(True)
        self.result.hide()
        layout.addWidget(self.result)

    def show_result(self, text, role="secondary"):
        self.result.setText(text)
        self.result.setProperty("role", role)
        self.result.style().unpolish(self.result)
        self.result.style().polish(self.result)
        self.result.show()

    def new_from_example(self, path=None):
        """Ask where (in ~/.config/sandbox-ai/ at first), refuse a place inside a workspace, write the starter there
        and add it. PATH: the place without asking (tests). The file written, or None."""
        if not self.local():
            self.show_result(tr("The start is on another host: write the ruleset there, then name its file here."),
                             "notice-warn")
            return None
        if path is None:
            state = current()
            if state is not None and not state.folder("rules"):
                folder = os.path.expanduser(CONFIG_DIR)
                os.makedirs(folder, mode=0o700, exist_ok=True)
                state.remember_folder("rules", os.path.join(folder, "rules.json"))
            path = choose_save(self, tr("New ruleset from the example"), "github.json", "JSON (*.json)", key="rules")
            if not path:
                return None
        workspace = inside(path, self.workspaces())
        if workspace is not None:
            warn(self, tr("New from example"),
                 tr("{path} lies inside the workspace {workspace}: the agent could change its own rules there, so the "
                    "launcher refuses it. Choose a place outside every workspace, such as ~/.config/sandbox-ai/.",
                    path=printable(path), workspace=printable(workspace)))
            return None
        try:
            written = write_example(path)
        except OSError as exc:
            warn(self, tr("New from example"), tr("Cannot write {path}: {error}", path=printable(path),
                                                  error=printable(exc.strerror or exc)))
            return None
        self.add(written)
        self.show_result(tr("Written: {path}. Change it to your needs; Check reads it.", path=printable(written)))
        return written

    def check(self):
        paths = list(self.paths())
        if not paths:
            self.show_result(tr("Add a ruleset file first (or New from example…)."), "notice-warn")
            return None
        if not self.local():
            self.show_result(tr("The start is on another host: its launcher checks the files at the start."),
                             "notice-warn")
            return None
        self.show_result(tr("The launcher reads the rulesets…"))
        return background(self.run_check, paths, list(self.workspaces() or []), self.agent() or "claude",
                          source=self.check_button, owner=self, timeout=120, done=self.checked)

    def run_check(self, paths, workspaces, agent="claude"):
        """(ok, text) of the launcher's dry run with the ruleset files PATHS (and WORKSPACES, so that one inside a
        workspace is refused as at the start; without, an empty folder of its own stands in)."""
        scratch = None
        if not workspaces:
            scratch = tempfile.mkdtemp(prefix="sandbox-ai-rules-")
            workspaces = [scratch]
        try:
            argv = [token for path in paths for token in ("--rules", str(path))] + ["--no-config"]
            argv += [token for folder in workspaces for token in ("--workspace", str(folder))]
            plan, message = self.sai.dry_run(agent, argv)
        finally:
            if scratch:
                shutil.rmtree(scratch, ignore_errors=True)
        if plan is None:
            return False, message or tr("The launcher refused them, without saying why.")
        lines = []
        for source in plan.get("rulesets") or []:
            name = str(source.get("name", ""))
            if not name.startswith("--rules "):  # the files given here ("--rules NAME"), not the policy's or mode's
                continue
            lines.append(tr("{name}: {http} HTTP and {mcp} MCP rules", name=printable(name),
                            http=len(source.get("http") or []), mcp=len(source.get("mcp") or [])))
        return True, "\n".join([tr("The launcher reads them:")] + (lines or [tr("(no rules of their own)")]))

    def checked(self, result, error):
        if error is not None:
            self.show_result(tr("Not checked: {error}", error=printable(error)), "notice-error")
            return
        ok, text = result
        self.show_result("✔ " + text if ok else "✘ " + printable(text), "notice-ok" if ok else "notice-error")
