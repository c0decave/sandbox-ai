"""The newer pages of the app: approvals, statistics and credentials (images: images_page.py, files: files_page.py)."""
from __future__ import annotations

import functools
import json
import os
from pathlib import Path
import time

from PySide6.QtCore import QEvent, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QPlainTextEdit, QPushButton, QScrollArea, QTabWidget, QToolButton, QVBoxLayout, QWidget,
)

from . import applog
from . import backend, theme, widgets
from .approvals_sections import AllowedSection, BlockedSection, ContextHeader, run_for_tab
from .files import printable, printable_text
from .state import choose_folder
from .i18n import N_, language as i18n_language, tr
from .widgets import (
    MONO, SORT_ROLE, AsyncCommand, ago, ask_first, background, bars, compact_combo, confirm, disclosure, explanation,
    fill, find_row, fit_columns, flow, outcome,
    plain_label, row_key, safe_tooltip, selected, selected_rows, set_enabled, table, warn,
)


ANSWER_WAIT = 90  # seconds an answer may take (sandbox-ai approvals: up to 60)
REMOTE_ANSWER_WAIT = 130  # an answer on another host: SSH on top (and "always" waits for the certificate check)
REMOTE_POLL = 5000  # ms between two looks at the other hosts' open questions (one look per host at a time)
REMOTE_BACKOFF = 30  # s at most between two looks at a host that does not answer (REMOTE_POLL, doubled each time)
REMOTE_GRACE = 60  # seconds the questions of a host that stopped answering stay listed after its last answer
WAKE_SECONDS = 120  # seconds a host is asked for questions after the app started something (it may not be listed yet)
LOCAL = "local"  # the host of this machine's own questions
BLOCKED_REFRESH = 10  # seconds between two reads of what was refused without a question, while it is shown
UNDO_MS = 8000  # how long "Undo" is offered after an answer given on the page
# The tabs of the one table: the questions waiting for an answer, those answered (allowed, refused - with what the
# sandboxes refused without a question), those nobody answered in time (still answerable), all of them.
STATE_TABS = (("open", N_("tab|Open")), ("allowed", N_("tab|Allowed")), ("refused", N_("tab|Refused")),
              ("timeout", N_("tab|Timed out")), ("all", N_("tab|All")))
SNAPSHOT_WAIT = 30  # seconds a scan of the questions may take before the next one is tried
POLL_WAIT = 0.2  # seconds a scan on the GUI thread waits for one running in a thread


class BlockedRow(dict):
    """What a sandbox refused without a question of its own (sandbox-ai approvals blocked: a rule, the network
    options, the mode, the policy, the kill switch), as a row of the Approvals table's Refused tab. Its "id" is its
    own key (no question's); "blocked" is the entry itself, which the Blocked section acts on."""

    def __init__(self, item, ident):
        super().__init__(id=ident, kind=item.get("question_kind") or "blocked", time=item.get("time") or "",
                         question=printable(item.get("what") or ""), state="refused", blocked=item,
                         question_id=item.get("question"))


def entry_state(question):
    """open, timeout, allowed or refused (backend.question_state); a refusal without a question is refused."""
    return "refused" if isinstance(question, BlockedRow) else backend.question_state(question)


def state_text(question):
    """What the State column says of QUESTION when it does not count down: answered how, timed out, refused why."""
    state = entry_state(question)
    if isinstance(question, BlockedRow):
        from .approvals_sections import kind_label
        return tr("refused: {why}", why=kind_label(question["blocked"].get("kind")))
    result = question.get("result") if isinstance(question.get("result"), dict) else {}
    scope = {"once": tr("once"), "run": tr("for this run"), "always": tr("always")}.get(result.get("scope"), "")
    if state == "timeout":
        return tr("timed out - still answerable") if backend.answerable(question) else tr("timed out")
    if state in ("allowed", "refused"):
        text = (tr("allowed") if state == "allowed" else tr("refused")) + (f" ({scope})" if scope else "")
        return text + (", " + tr("asked again") if question.get("asked_again") or result.get("forgotten") else "")
    return ""


def kind_name(kind):
    return {"connect": tr("connection"), "dns": tr("name lookup"), "push": tr("push"),
            "browser": tr("open a URL"), "mcp": tr("MCP tool call"), "http": tr("HTTP request"),
            "tool": tr("agent tool"), "device": tr("device"), "canary": tr("canary"),
            "bridge": tr("session bridge")}.get(kind, printable(kind))


def shown(value, limit=300):
    """A value the agent chose (a text, or the arguments of a tool call), escaped and shortened."""
    if not isinstance(value, str):
        try:
            value = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            value = str(value)
    value = printable(value)
    return value if len(value) <= limit else value[:limit - 1] + "…"


def package_of(question):
    """The package of a question the package guard asks (a request's, with "package": {"ecosystem", "name",
    "version", "age_days", "malicious", "new_name_days", "allow"}), or None."""
    if isinstance(question, (backend.RemoteQuestion, BlockedRow)) or question.get("kind") != "http":
        return None
    package = question.get("package")
    return package if isinstance(package, dict) and package.get("name") else None


def malicious_package(question):
    """QUESTION is about a download the package guard holds as a known malicious version (its advisory)."""
    package = package_of(question)
    return bool(package and package.get("malicious"))


def package_rich(package, url):
    """What a package question asks, as rich_notice.Rich: the package and its version monospaced, then its advisory
    (a known malicious version), its age and whether its name is brand new. Everything the agent's request chose is
    escaped (shown)."""
    from . import rich_notice as R
    number = lambda value: isinstance(value, int) and not isinstance(value, bool)  # noqa: E731
    name = shown(package.get("name", "?"), 214) + (f" {shown(package['version'], 128)}" if package.get("version")
                                                   else "")
    parts = list(R.fmt(tr("download the {ecosystem} package {package}"),
                       ecosystem=shown(package.get("ecosystem", "?"), 10), package=R.code(name)).lines[0])
    facts = []
    if package.get("malicious"):
        facts.append(tr("KNOWN MALICIOUS (advisory {advisory})", advisory=shown(package["malicious"], 64)))
    age = package.get("age_days")
    facts.append(tr("release age unknown") if not number(age) else tr("released 1 day ago") if age == 1
                 else tr("released {days} days ago", days=age))
    if number(package.get("new_name_days")):
        facts.append(tr("first release {days} days ago: possibly a hallucinated or slopsquatted name",
                        days=package["new_name_days"]))
    parts.append(("text", ": " + ", ".join(facts) + " ("))
    parts += [*R.fmt("{url}", url=R.address(shown(url, 500))).lines[0], ("text", ")")]
    return R.Rich([parts])


SECRET_PLACES = {"url": N_("the URL"), "header": N_("a header"), "body": N_("the body")}


def secret_of(question):
    """What the secret guard found in a request it asks about (a request's, with "secret": {"host", "found": [{"id",
    "kind", "label", "where", "encoding"}], "allow"}), or None. Never a value: only what kind of one."""
    if isinstance(question, (backend.RemoteQuestion, BlockedRow)) or question.get("kind") != "http":
        return None
    secret = question.get("secret")
    found = secret.get("found") if isinstance(secret, dict) else None
    return secret if isinstance(found, list) and found and all(isinstance(item, dict) for item in found) else None


def secret_words(texts):
    """TEXTS as one list in a sentence: "a, b and c"."""
    texts = list(dict.fromkeys(texts))
    return texts[0] if len(texts) == 1 else tr("{list} and {last}", list=", ".join(texts[:-1]), last=texts[-1])


def secret_phrases(secret):
    """(what was found, where in the request, how it was encoded) of a secret guard question, each as words. A value
    the start handed out is named by its label (its variable and option), a token by its shape ("what looks like:
    GitHub token"); everything is escaped (shown)."""
    said, places, encodings = [], [], []
    for finding in secret["found"][:8]:
        label = shown(finding.get("label") or finding.get("id") or "?", 120)
        said.append(label if finding.get("kind") == "value" else tr("what looks like a credential ({shape})",
                                                                  shape=label))
        places.append(tr(SECRET_PLACES.get(finding.get("where"), SECRET_PLACES["body"])))
        encodings += [shown(part, 20) for part in str(finding.get("encoding") or "").split("+")
                      if part and part != "plain"]
    return secret_words(said), secret_words(places), secret_words(encodings) if encodings else ""


def secret_rich(secret, method, url):
    """What a secret guard question asks, as rich_notice.Rich: what kind of secret, the host monospaced, then the
    request and where in it the secret is. Everything the request chose is escaped (shown)."""
    from . import rich_notice as R
    what, places, encodings = secret_phrases(secret)
    parts = list(R.fmt(tr("send {what} to {host}"), what=what,
                       host=R.address(shown(secret.get("host", "?"), 253))).lines[0])
    parts.append(("text", " ("))
    parts += R.fmt("{method} {url}", method=R.code(shown(method, 20)), url=R.address(shown(url, 500))).lines[0]
    parts.append(("text", ", " + tr("in {places}", places=places) + (f", {encodings}" if encodings else "") + ")"))
    return R.Rich([parts])


def describe(question):
    """What a question asks, in the app's language; everything the agent chose is escaped. A question of another
    host is said as that host's sandbox-ai says it (escaped there and here). Plain text: describe_rich's."""
    return str(describe_rich(question))


def describe_rich(question):
    """describe() as rich_notice.Rich, for a notification: the address it asks for in the accent colour, the tool, the
    arguments and the device monospaced, the rest recognised."""
    from . import rich_notice as R
    if isinstance(question, (backend.RemoteQuestion, BlockedRow)):
        return R.recognise(printable(question.get("question") or question.get("kind") or "?"))
    kind = question.get("kind")
    if kind == "connect":
        parts = list(R.fmt(tr("connect to {destination}"), destination=R.address(question.get("destination", "?")))
                     .lines[0])
        protocol = question.get("protocol")
        if protocol and protocol != "tcp":
            parts.append(("text", f" ({printable(protocol)[:10]})"))
        if question.get("host"):
            parts += [("text", " "), *R.fmt(tr("(named {host} by the agent, not verified)"),
                                             host=R.address(question["host"])).lines[0]]
        return R.Rich([parts])
    if kind == "dns":
        return R.fmt(tr("look up the name {name}"), name=R.address(question.get("name", "?")))
    if kind == "push":
        refs = question.get("refs") if isinstance(question.get("refs"), list) else []
        return R.fmt(tr("push {refs} to {url} despite privacy warnings"),
                     refs=R.code(", ".join(printable(r) for r in refs[:10])), url=R.address(question.get("url", "?")))
    if kind == "browser":
        return R.fmt(tr("open {url} in your browser"), url=R.address(question.get("url", "?")))
    if kind == "bridge":  # sandbox-ai bridge: a call across it (a write: its diff in full below)
        text = tr("bridge {bridge}: {source} wants to {tool} on {target}: {arguments}",
                  bridge=shown(question.get("bridge", "?"), 8), source=shown(question.get("from", "?"), 80),
                  tool=shown(question.get("tool", "?"), 40), target=shown(question.get("to", "?"), 80),
                  arguments=shown(question.get("arguments", "")))
        return text + (" " + tr("(this answer covers this one write only)") if question.get("write") else "")
    # Rules of the project ask for these: which call it is must be seen before it is allowed.
    rule = tr("rule {rule} of {ruleset}", rule=shown(question.get("rule", "?"), 80),
              ruleset=shown(question.get("ruleset", "?"), 120))
    if kind == "mcp":
        # Its arguments in full are below the list (ArgumentsBox): the line says it shortened them, before them (a
        # table's cell is cut after 300 characters).
        values = {"tool": R.code(shown(question.get("tool", "?"), 128)),
                  "server": R.code(shown(question.get("server", "?"), 40)),
                  "arguments": R.code(shown(question.get("arguments", ""))), "rule": rule}
        if backend.arguments_whole(question):
            return R.fmt(tr("call the MCP tool {tool} of {server} with {arguments} ({rule})"), **values)
        return R.fmt(tr("call the MCP tool {tool} of {server} (arguments shortened here: in full below) with "
                        "{arguments} ({rule})"), **values)
    if kind == "tool":  # as an MCP call's: its arguments in full below the list where the line shortens them
        values = {"agent": shown(question.get("agent", "?"), 20), "tool": R.code(shown(question.get("tool", "?"), 128)),
                  "arguments": R.code(shown(question.get("arguments", ""))), "rule": rule}
        if backend.arguments_whole(question):
            return R.fmt(tr("let {agent} use its tool {tool} with {arguments} ({rule})"), **values)
        return R.fmt(tr("let {agent} use its tool {tool} (arguments shortened here: in full below) with {arguments} "
                        "({rule})"), **values)
    if kind == "http" and package_of(question):  # the package guard's: the package, its age, the advisory
        return package_rich(package_of(question), question.get("url", "?"))
    if kind == "http" and secret_of(question):  # the secret guard's: what kind of secret, to which host
        return secret_rich(secret_of(question), question.get("method", "?"), question.get("url", "?"))
    if kind == "http":
        return R.fmt("{method} {url} ({rule})", method=R.code(shown(question.get("method", "?"), 20)),
                     url=R.address(shown(question.get("url", "?"), 500)), rule=rule)
    if kind == "device":
        nodes = question.get("nodes") if isinstance(question.get("nodes"), list) else []
        device = ", ".join(shown(n, 200) for n in nodes[:10]) or shown(question.get("value", "?"))
        return R.fmt(tr("attach the device {device} ({rule})"), device=R.code(device), rule=rule)
    if kind == "canary":
        return R.fmt(tr("canary {name}: {summary}"), name=R.code(shown(question.get("canary", "?"), 40)),
                     summary=shown(question.get("summary", ""), 500))
    return R.Rich([[("text", printable(kind))]])


def why_asked(question):
    """Why QUESTION is asked, in plain words (what the app knows of it: the network settings, a ruleset's rule, a
    privacy warning, the agent's wish to open a page)."""
    if isinstance(question, backend.RemoteQuestion):
        return ""
    kind = question.get("kind")
    if kind in ("connect", "dns"):
        return tr("Asked because the sandbox's network settings do not allow it.")
    if malicious_package(question):
        return tr("Asked by the package guard: this version is on the list of known malicious packages (advisory "
                  "{advisory}, https://osv.dev/vulnerability/{advisory}). Deny it unless you know why you need it; "
                  "an allow covers this one download only.", advisory=shown(package_of(question)["malicious"], 64))
    if package_of(question):
        command = package_of(question).get("allow")
        return tr("Asked by the package guard: a version this young, or a package this new, may be malware nobody "
                  "has reported yet, or a made-up name somebody registered.") + \
            (" " + tr("To allow the package in every run of the project: {command}", command=shown(command, 300))
             if command else "")
    if secret_of(question):
        command = secret_of(question).get("allow")
        return tr("Asked by the secret guard: this request carries a secret to a host it was not given for. The "
                  "value is never shown, only what kind it is. Deny it unless you know that it belongs there (a test "
                  "fixture, a token of that host).") + \
            (" " + tr("To allow that kind of value for that host in every run of the project: {command}",
                      command=shown(command, 600)) if command else "")
    if kind in ("http", "mcp", "tool", "device"):
        return tr("Asked because rule {rule} of the ruleset {ruleset} asks before it. The network settings do not "
                  "change that.", rule=shown(question.get("rule", "?"), 80), ruleset=shown(question.get("ruleset", "?"),
                                                                                            120))
    if kind == "push":
        return tr("Asked because the privacy check found warnings in what is pushed.")
    if kind == "browser":
        return tr("The agent asks to open this page in your browser.")
    if kind == "canary":
        return tr("A decoy file was touched: nothing legitimate uses it. The sandbox waits for you.")
    if kind == "bridge":
        return tr("Asked because the session bridge asks before calls across it (its --ask), and before every write, "
                  "with the diff below.")
    return ""


def needs_details(question, arguments):
    """QUESTION cannot be answered safely from its line alone: its call's arguments are shortened there (ARGUMENTS,
    its ArgumentsBox, opened them by itself: a long post, a write's diff across a bridge), or a decoy file was
    touched (why_asked says what that means). Its Details open by themselves."""
    if getattr(arguments, "needed", False):
        return True
    return not isinstance(question, backend.RemoteQuestion) and question.get("kind") == "canary"


def openable_url(text):
    """QUrl of TEXT if it is an http or https URL with a host (what "Open here" opens), else None."""
    url = QUrl(str(text or ""), QUrl.ParsingMode.StrictMode)
    if url.isValid() and url.scheme().lower() in ("http", "https") and url.host():
        return url
    return None


# One button per scope of an allow (user 2026-11-19: "'immer erlauben', 'immer auch für Folgesessions' ebenfalls als
# eigene Buttons"): (action of the buttons and keys, scope sandbox-ai is sent, label).
ALLOW_SCOPES = (("allow", "once", N_("Allow once")), ("allow-for-run", "run", N_("Always allow")),
                ("allow-always", "always", N_("Always, also for later sessions")))
SCOPE_OF = {action: scope for action, scope, _ in ALLOW_SCOPES}
ONCE_ONLY = ("canary", "browser")  # a canary's alarm (Continue) and a page to open: answered this once, nothing kept


def allow_buttons(owner, keys):
    """The three Allow buttons of OWNER (the Approvals page, the Questions window), each answering with its scope at
    once: Allow once, Always allow (every same request of this run), Always, also for later sessions (remembered for
    the project: its next starts and resumed sessions too). KEYS: the keys each tooltip names."""
    tips = {"once": tr("Allowed this time only: the next same request is asked again ({keys})", keys=keys["once"]),
            "run": tr("Every same request of this run (this session) is allowed without a question ({keys})",
                      keys=keys["run"]),
            "always": tr("Allowed now and remembered for the project: also at its later starts and resumed "
                         "sessions, without a question (Forget… under Remembered lifts it; {keys})",
                         keys=keys["always"])}
    made = []
    for action, scope, label in ALLOW_SCOPES:
        button = QPushButton(tr(label))
        button.setAccessibleName(tr(label))
        button.setToolTip(safe_tooltip(tips[scope]))  # (the app's own words: escaped all the same)
        button.clicked.connect(lambda _=False, a=action: owner.act(a))
        made.append(button)
    owner.allow_button, owner.allow_run_button, owner.allow_always_button = made
    return made


# The kinds sandbox-ai approves at most for the run (approvals.widest_answer): a push, a request, a tool call, a device.
RUN_AT_MOST = ("push", "http", "mcp", "tool", "device")
# Keys of the Approvals page and of its Questions window: (key, modifiers) -> action. None of them is a menu
# mnemonic in English or German (Alt+letters: &Datei, &Ansicht) or a key of the window's menus (Ctrl+Shift+B: Only
# icons); they are taken while the focus is in the open questions, before any shortcut of the window.
SHORTCUTS = {(Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier): "allow",
             (Qt.Key.Key_Enter, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.KeypadModifier): "allow",
             # Always allow (this run) and Always, also for later sessions: Ctrl+Shift+Return, Ctrl+Shift+A.
             (Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier): "always",
             (Qt.Key.Key_Enter, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier
              | Qt.KeyboardModifier.KeypadModifier): "always",
             (Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier): "always-later",
             (Qt.Key.Key_D, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier): "deny",
             (Qt.Key.Key_K, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier): "block",
             # In the list of questions only (PLAIN_KEYS): Enter allows for this run, Delete denies.
             (Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier): "allow-run",
             (Qt.Key.Key_Enter, Qt.KeyboardModifier.KeypadModifier): "allow-run",
             (Qt.Key.Key_Delete, Qt.KeyboardModifier.NoModifier): "deny-key"}
PLAIN_KEYS = ("allow-run", "deny-key")  # taken only where the focus is in a list of questions
MODIFIERS = (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.AltModifier
             | Qt.KeyboardModifier.MetaModifier | Qt.KeyboardModifier.KeypadModifier)


def in_question_list(widget):
    """WIDGET is a list of questions (the page's, the Questions window's) or a part of one (its viewport)."""
    from PySide6.QtWidgets import QAbstractItemView
    while widget is not None:
        if isinstance(widget, QAbstractItemView):
            return widget.accessibleName() in (tr("Open questions"), tr("Questions"))
        widget = widget.parentWidget()
    return False


def shortcut_of(event):
    """The action of the Approvals page a key EVENT stands for, or None."""
    try:
        modifiers = event.modifiers() & MODIFIERS
        return next((action for (key, wanted), action in SHORTCUTS.items()
                     if event.key() == key and modifiers == wanted), None)
    except (AttributeError, TypeError):
        return None


def local_widest(policy, record, question):
    """The widest answer an allow of a question of this machine keeps, as sandbox-ai's approvals.widest_answer says
    it: the administrator's POLICY, the run's security mode (its run.json RECORD) and the kind's own cap. None: no
    allow at all - a name stays pinned for the run once allowed, so where only "once" is allowed sandbox-ai refuses
    to allow it (approvals.DNS_ONCE)."""
    caps = [policy if policy in backend.SCOPES else "always"]
    mode = record.get("mode") if isinstance(record, dict) and isinstance(record.get("mode"), dict) else {}
    if mode.get("approval_scope") in backend.SCOPES:
        caps.append(mode["approval_scope"])
    if isinstance(question, dict) and question.get("kind") in RUN_AT_MOST:
        caps.append("run")
    if isinstance(question, dict) and malicious_package(question):
        caps.append("once")  # a known malicious package version: this one download (approvals.widest_answer)
    widest = min(caps, key=backend.SCOPES.index)
    if isinstance(question, dict) and question.get("kind") == "dns" and widest == "once":
        return None
    return widest


def remote_browser(entry):
    """True for a question of another host to open a page: the page is opened here, where the human is."""
    return entry is not None and entry[2] != LOCAL and entry[1].get("kind") == "browser"



def asker(name, record):
    """Which sandbox asks: its run NAME, workspace (~ for HOME) and session, from its run.json RECORD."""
    record = record if isinstance(record, dict) else {}
    session = record.get("session")
    # Made once per run (every open question's row and label asks again at each poll, every second: HOME was looked up
    # for each); what it is made of is the key - HOME and the language too, so a change of either shows at once.
    return _asker(str(name), str(record.get("workspace") or ""), str(session) if session else "",
                  os.environ.get("HOME"), i18n_language())


@functools.lru_cache(maxsize=1024)
def _asker(name, workspace, session, _home_variable, _language):
    home = str(Path.home())
    if workspace == home or workspace.startswith(home + os.sep):
        workspace = "~" + workspace[len(home):]
    details = [printable(workspace)] if workspace else []
    if session:
        details.append(tr("session {name}", name=printable(session)))
    return f"{printable(name)} ({', '.join(details)})" if details else printable(name)

def context_text(context):
    """(explanation, log lines) of a question (SandboxAI.question_context): the explanation in plain words,
    the run's log entries apart - quoted, as they are in the log (escaped), never as text of the app."""
    lines = []
    if context.get("kind") == "dns":
        lines.append(tr("The agent wants to look up {name}. Nothing is looked up before you answer: the name itself "
                        "can carry data out. Allowing resolves it and opens only its public addresses.",
                        name=printable(context.get("name", ""))))
    elif context.get("kind") == "connect":
        lines.append(tr("Address {address}, port {port}, protocol {protocol}.", address=printable(context.get("address")),
                        port=printable(context.get("port")), protocol=printable(context.get("protocol") or "tcp")))
        if context.get("host"):
            lines.append(tr("The agent calls it {host}: not verified, the address is what is opened.",
                            host=printable(context["host"])))
    recent = context.get("recent") or []
    if not recent and context.get("log"):
        lines.append(tr("No earlier connections of this run to it in its log."))
    log = ["> " + "  ".join(printable(row[i]) for i in (0, 1, 4) if row[i]) for row in recent]
    return "\n".join(lines), log


class ArgumentsBox(QWidget):
    """A tool call's arguments in full, as the call will send them (backend.question_details): a button that opens and
    closes them, the text read-only, monospace and scrollable. It opens by itself where the question's line shows them
    shortened: what is allowed is seen whole before Allow."""

    def __init__(self, height=220):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.toggle = widgets.disclosure(QToolButton())  # (an arrow and text: never a button stuck pressed)
        self.toggle.setAccessibleName(tr("The call's arguments in full"))
        self.toggle.toggled.connect(self.opened)
        self.note = plain_label(role="secondary")
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setFont(MONO)
        self.text.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.text.setMaximumHeight(height)
        self.text.setAccessibleName(tr("The call's arguments in full"))
        layout.addWidget(self.toggle, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.note)
        layout.addWidget(self.text)
        self.shown_text = None
        self.needed = False  # the question's line shows them shortened (opened by itself: needs_details)
        self.what = "arguments"  # "arguments" (a tool call's JSON), "diff" (a bridge's write), "content" (a bridge's)
        self.opened(False)
        self.hide()

    def show_for(self, question):
        """The details of QUESTION (None: nothing chosen)."""
        text, whole = backend.question_details(question) if question is not None else ("", True)
        if not text:
            self.shown_text = None
            self.needed = False
            self.hide()
            return
        self.needed = not whole  # (shortened in the question's line: what is allowed must be seen whole)
        # A session bridge's question carries what the call moves (a write: its diff), as it is: neither JSON nor
        # redacted - the human sees the token a hostile agent would write or send.
        bridge = question.get("kind") == "bridge"
        self.what = ("diff" if question.get("write") else "content") if bridge else "arguments"
        if text != self.shown_text:  # another question (the same one again keeps what you opened or closed)
            self.shown_text = text
            self.text.setPlainText(text)
            self.toggle.setChecked(not whole)
        self.opened(self.toggle.isChecked())
        note = {"arguments": tr("As the call will send them (pretty-printed JSON; credentials redacted)."),
                "diff": tr("The write's diff in full, as it will be written (nothing redacted)."),
                "content": tr("What the call moves across the bridge, in full (nothing redacted).")}[self.what]
        self.note.setText(note + ("" if whole else " " + tr("The question's line shows them shortened.")))
        self.show()

    def opened(self, on):
        if self.what == "diff":
            self.toggle.setText(tr("Hide the diff") if on else tr("Show the diff in full"))
        elif self.what == "content":
            self.toggle.setText(tr("Hide it") if on else tr("Show it in full"))
        else:
            self.toggle.setText(tr("Hide the arguments") if on else tr("Show the arguments in full"))
        self.note.setVisible(on)
        self.text.setVisible(on)


class QuestionsWindow(QDialog):
    """The open questions while another dialog is open. A modal dialog (the wizard, Settings, the gallery) blocks the
    main window, and with it the Approvals page, while a question is refused without an answer after a while. This
    window is application-modal itself, so it is above that dialog and gets the input; it answers through the
    Approvals page (the same path, the same checks, the same buttons and keys: Ctrl+Return, Ctrl+Shift+Return,
    Ctrl+Shift+A, Ctrl+Shift+D, Ctrl+Shift+K)."""

    def __init__(self, page):
        super().__init__(page)
        self.page = page
        self.setWindowTitle(tr("Questions of the running sandboxes"))
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        widgets.emergency_shortcut(self)  # (it blocks the window's Ctrl+Shift+X)
        layout = QVBoxLayout(self)
        layout.addWidget(plain_label(tr("A sandbox asks while a dialog is open. Answer here, or close this window "
                                        "and answer on the Approvals page.")))
        self.table = table([tr("Sandbox"), tr("Question")], stretch=1)
        from .tables import keep
        keep(self.table, "dialog-questions")
        self.table.itemSelectionChanged.connect(self.update_buttons)
        layout.addWidget(self.table, 1)
        self.arguments = ArgumentsBox(height=160)  # a tool call's arguments in full, seen here before Allow too
        layout.addWidget(self.arguments)
        row = QHBoxLayout()
        self.table.setAccessibleName(tr("Open questions"))
        # Allow once (Enter in the list does what it says), Always allow, Always, also for later sessions.
        allow_buttons(self, {"once": tr("Ctrl+Return; Enter in the list"), "run": tr("Ctrl+Shift+Return"),
                             "always": tr("Ctrl+Shift+A")})
        self.deny_button = QPushButton(tr("Deny for this run"))
        self.deny_button.setAccessibleName(tr("Deny for this run"))
        self.deny_button.setToolTip(tr("Refused; not asked again in this run (Ctrl+Shift+D)"))
        self.deny_button.clicked.connect(lambda: self.answer("deny"))
        self.block_button = QPushButton(tr("Block always"))
        self.block_button.setAccessibleName(tr("Block always"))
        self.block_button.setProperty("role", "danger")
        self.block_button.setToolTip(tr("Refused now and at every later start of this project, without a question "
                                        "(Ctrl+Shift+K)"))
        self.block_button.clicked.connect(lambda: self.answer("block"))
        close = QPushButton(tr("Close"))
        close.clicked.connect(self.reject)
        # (a row that wraps when the window is narrow: six buttons, longer in German)
        row.addWidget(flow(self.allow_button, self.allow_run_button, self.allow_always_button, self.deny_button,
                           self.block_button), 1)
        row.addWidget(close, 0, Qt.AlignmentFlag.AlignBottom)
        layout.addLayout(row)
        order = (self.table, self.allow_button, self.allow_run_button, self.allow_always_button, self.deny_button,
                 self.block_button, close)
        for first, second in zip(order, order[1:]):
            QWidget.setTabOrder(first, second)
        widgets.list_menu(self.table, lambda: [self.allow_button, self.allow_run_button, self.allow_always_button,
                                               self.deny_button, None, self.block_button])
        self.resize(720, 300)

    def show_questions(self):
        page = self.page
        keys = page.open_keys()
        fill(self.table, [[page.labels.get(key, printable(key[1])), describe(page.questions[key][1])] for key in keys],
             keys=keys)
        if not selected_rows(self.table) and len(keys) == 1:
            self.table.selectRow(0)
        self.update_buttons()

    def current(self):
        rows = selected_rows(self.table)
        return self.page.questions.get(row_key(self.table, rows[0])) if rows else None

    def update_buttons(self):
        entry = self.current()
        rules = self.page.answer_rules(entry)
        self.arguments.show_for(entry[1] if entry else None)
        self.allow_button.setText(tr("Open here and allow") if remote_browser(entry) else tr("Allow once"))
        for button, scope in ((self.allow_button, "once"), (self.allow_run_button, "run"),
                              (self.allow_always_button, "always")):
            why = rules["allow"] or (self.page.scope_refused(entry, scope) if entry is not None else None)
            set_enabled(button, why is None, why)
        set_enabled(self.deny_button, rules["deny"] is None, rules["deny"])
        set_enabled(self.block_button, rules["block"] is None, rules["block"])

    def shortcut(self, action):
        # Enter in the list does what the window's Allow button says (Allow once, or Open here and allow): this window
        # has no scope box, and Enter allowed for the whole run here while the button beside it said "once" - with
        # the confirmation's default Allow, Enter twice allowed for the run.
        button = {"allow": self.allow_button, "allow-run": self.allow_button, "always": self.allow_run_button,
                  "always-later": self.allow_always_button, "deny": self.deny_button, "deny-key": self.deny_button,
                  "block": self.block_button}[action]
        if button.isEnabled():
            button.click()

    def act(self, action):
        """An Allow button: the question highlighted allowed with its scope (once, for the run, always)."""
        entry = self.current()
        if entry is None:
            return
        scope = SCOPE_OF[action]
        self.page.answer("allow", scope, entry=entry, parent=self,
                         open_here=scope == "once" and remote_browser(entry))

    def answer(self, decision):
        entry = self.current()
        if entry is None:
            return
        if decision == "block":
            self.page.answer("deny", "always", entry=entry, parent=self)
            return
        self.page.answer(decision, "once" if decision == "allow" else "run", entry=entry, parent=self,
                         open_here=decision == "allow" and remote_browser(entry))


class ApprovalsPage(QWidget):
    """Open questions of every running sandbox, on this machine and on every host of `sandbox-ai hosts`, answered
    through `sandbox-ai approvals` (there: over SSH); the history (of this machine).

    Rows are found by their (host, run, question id) key, never by their position: the table can be sorted, and a
    question that disappears leaves nothing selected, so Allow can only answer the question highlighted. The other
    hosts are read every REMOTE_POLL ms in threads, one look per host at a time, whether the page is shown or not
    (their questions are refused after a minute too)."""
    pending_changed = Signal(int, list)  # number of open questions (all hosts), the new ones [(sandbox, question)]

    def __init__(self, sai, interval=1000, remote_interval=None):
        super().__init__()
        self.sai = sai
        layout = QVBoxLayout(self)
        open_box = QGroupBox(tr("Questions of every host"))
        open_layout = QVBoxLayout(open_box)
        self.url = plain_label()
        font = QFont()
        font.setPointSize(font.pointSize() + 3)
        font.setBold(True)
        self.url.setFont(font)
        self.url.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.url.hide()
        open_layout.addWidget(self.url)
        # Whose questions are listed: every host's, this machine's or one host's (Hosts › Show questions).
        filter_row = QHBoxLayout()
        self.host_filter = QComboBox()
        self.host_filter.setAccessibleName(tr("Questions of"))
        self.host_filter.setToolTip(tr("Whose questions are listed: every host's, this machine's or one host's"))
        self.host_filter.addItem(tr("All hosts"), None)
        self.host_filter.addItem(tr("This machine"), LOCAL)
        self.host_filter.currentIndexChanged.connect(lambda _=0: self.show_all())
        questions_of = QLabel(tr("Questions of"))
        questions_of.setBuddy(self.host_filter)
        filter_row.addWidget(questions_of)
        filter_row.addWidget(self.host_filter)
        from .host_choice import follow_note
        self.follow_note = follow_note()  # whose questions, when the window's pages follow the Sandboxes page's host
        filter_row.addWidget(self.follow_note)
        filter_row.addStretch()
        open_layout.addLayout(filter_row)
        # One table for every state, its tabs above it with their counts: Open | Allowed | Refused | Timed out | All.
        self.state_tabs = widgets.ArrowTabBar()  # (the app's one kind of tab row)
        self.state_tabs.setAccessibleName(tr("Which questions"))
        self.state_tabs.setExpanding(False)
        self.state_tabs.setDrawBase(False)
        for key, label in STATE_TABS:
            self.state_tabs.addTab(tr(label))
            self.state_tabs.setTabData(self.state_tabs.count() - 1, key)
        self.state_tabs.currentChanged.connect(lambda _=0: self.state_tab_changed())
        open_layout.addWidget(self.state_tabs)
        self.open = table([tr("Sandbox"), tr("Kind"), tr("Question"), tr("Asked"), tr("Time left / answer"), tr("Host")],
                          stretch=2, compact=(1, 3, 4, 5), multi=True)  # several at once: their answers together
        self.open.setAccessibleName(tr("Questions"))
        self.open.itemSelectionChanged.connect(self.selection_changed)
        open_layout.addWidget(self.open)
        self.none_waiting = plain_label(tr("Nothing is waiting for an answer."), role="secondary")
        open_layout.addWidget(self.none_waiting)
        # What the selected question's line does not say - why it is asked, a call's arguments in full, what the
        # run's log knows of it - in one Details box below the answers, folded at first and as the human left it
        # (user 2026-11-18: the parts below the table read as the table again). A question that cannot be answered
        # safely from its line (its arguments shortened there, a write's diff, a decoy file touched) opens it by
        # itself. (Below the answers, not above: a box that opens by itself never moves the buttons away.)
        self.details_box = QWidget()
        details_layout = QVBoxLayout(self.details_box)
        details_layout.setContentsMargins(0, 0, 0, 0)
        # Why the selected question is asked, in plain words.
        self.why = plain_label(selectable=True)
        self.why.setAccessibleName(tr("Why it is asked"))
        self.why.hide()
        details_layout.addWidget(self.why)
        # A tool call's arguments in full, as the call will send them (its line in the table may be shortened).
        self.arguments = ArgumentsBox()
        details_layout.addWidget(self.arguments)
        # What the selected question means (resolved names, the run's earlier connections): in a thread.
        self.context = plain_label(role="secondary")
        self.context.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        details_layout.addWidget(self.context)
        # The run's log entries to the same address or name: quoted, monospace, read-only, under a label that
        # says whose words they are - the agent chose these names and URLs.
        self.context_log_label = plain_label(tr("Last connections of this run to it, from its log (text the agent "
                                                "chose, shown as it is):"), role="secondary")
        self.context_log = QPlainTextEdit()
        self.context_log.setReadOnly(True)
        self.context_log.setFont(MONO)
        self.context_log.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.context_log.setMaximumHeight(110)
        for widget in (self.context_log_label, self.context_log):
            widget.hide()
            details_layout.addWidget(widget)
        self.details = disclosure(QToolButton())  # (an arrow and text: never a button stuck pressed)
        self.details.setText(tr("Details"))
        self.details.setAccessibleName(tr("Details of the question"))
        self.details.setToolTip(tr("Why it is asked, a call's arguments in full and what the run's log knows of it; "
                                   "it opens by itself where a question needs it to be answered safely"))
        self.details_wanted = False  # the human's choice, opened or folded (state.json: keep_details)
        self.details_store = None
        self.details_key = None  # the question the box was opened or folded for (the same one again keeps it)
        # One button per scope of an allow (user 2026-11-19), each one click: Allow once, Always allow (every same
        # request of this run), Always, also for later sessions (remembered for the project); a scope the caps forbid
        # is a disabled button that says why. Deny is for the run (as the web interface's and the command line's),
        # Deny once only this time; Block always is a deny remembered for the project. Each acts at once, on every
        # row selected it fits (no question first: Undo beside the note takes it back for a few seconds).
        allow_buttons(self, {"once": tr("Ctrl+Return"), "run": tr("Ctrl+Shift+Return; Enter in the list"),
                             "always": tr("Ctrl+Shift+A")})
        self.deny_button = QPushButton(tr("Deny for this run"))
        self.deny_button.setAccessibleName(tr("Deny for this run"))
        self.deny_button.setToolTip(tr("Refused; not asked again in this run (Delete, Ctrl+Shift+D)"))
        self.deny_button.clicked.connect(lambda: self.act("deny"))
        self.deny_once_button = QPushButton(tr("Deny once"))
        self.deny_once_button.setAccessibleName(tr("Deny once"))
        self.deny_once_button.setToolTip(tr("Refused this time only: the sandbox may ask again"))
        self.deny_once_button.clicked.connect(lambda: self.act("deny-once"))
        self.block_button = QPushButton(tr("Block always"))
        self.block_button.setAccessibleName(tr("Block always"))
        self.block_button.setProperty("role", "danger")
        self.block_button.setToolTip(tr("Refused now and at every later start of this project, without a question "
                                        "(Forget… below lifts it; Ctrl+Shift+K)"))
        self.block_button.clicked.connect(lambda: self.act("block"))
        self.ask_again_button = QPushButton(tr("Ask again"))
        self.ask_again_button.setAccessibleName(tr("Ask again"))
        self.ask_again_button.setToolTip(tr("Forget what the run decided: its next try is asked again"))
        self.ask_again_button.clicked.connect(lambda: self.act("ask-again"))
        # The administrator's max_approval_scope caps the allows (sandbox-ai refuses wider ones anyway); read again
        # at every scan, so a policy changed meanwhile counts.
        self.max_scope = sai.max_scope()
        # A row that wraps when the window is narrow (in German it made the window 1100 px wide).
        open_layout.addWidget(flow(self.allow_button, self.allow_run_button, self.allow_always_button,
                                   self.deny_button, self.deny_once_button, self.block_button, self.ask_again_button))
        widgets.list_menu(self.open, lambda: [self.allow_button, self.allow_run_button, self.allow_always_button,
                                              self.deny_button, self.deny_once_button, None, self.block_button,
                                              self.ask_again_button])
        # Tab goes from whose questions to the questions and the answers, in the order they are read.
        order = (self.host_filter, self.state_tabs, self.open, self.allow_button, self.allow_run_button,
                 self.allow_always_button, self.deny_button, self.deny_once_button, self.block_button,
                 self.ask_again_button)
        for first, second in zip(order, order[1:]):
            QWidget.setTabOrder(first, second)
        # What was done (with Undo for a few seconds) and what failed (also a short notice that goes by itself).
        self.message = plain_label(selectable=True, log="approvals")
        self.undo_button = QPushButton(tr("Undo"))
        self.undo_button.setAccessibleName(tr("Undo"))
        self.undo_button.setToolTip(tr("Take back the answer just given"))
        self.undo_button.clicked.connect(lambda: self.undo())
        self.undo_button.hide()
        self.undo_timer = QTimer(self)
        self.undo_timer.setSingleShot(True)
        self.undo_timer.timeout.connect(lambda: self.undo_button.hide())
        self.undoable = []  # [(entry, action, scope)] of the last answers, as Undo takes them back
        message_row = QHBoxLayout()
        message_row.addWidget(self.message, 1)
        message_row.addWidget(self.undo_button)
        open_layout.addLayout(message_row)
        open_layout.addWidget(self.details, 0, Qt.AlignmentFlag.AlignLeft)
        open_layout.addWidget(self.details_box)
        self.details.hide()  # (in the box first: never a window of its own for a moment)
        self.details_box.hide()
        self.details.toggled.connect(lambda on: self.details_box.setVisible(on))
        self.details.clicked.connect(lambda on: self.details_chosen(on))  # (by hand only: kept)
        QWidget.setTabOrder(self.ask_again_button, self.details)  # (after the answers: in the box now)
        self.remote_note = plain_label(role="secondary")  # hosts that did not answer
        self.remote_note.hide()
        open_layout.addWidget(self.remote_note)
        self.seconds = sai.approval_seconds
        open_layout.addWidget(explanation(tr(
            "A request waits up to {seconds} seconds for your answer. Unanswered by then it fails, and its question "
            "stays open under Timed out: allowed later, the request's next try goes through. A page to open waits "
            "until it is answered. A click answers at once (Undo takes it back); every answer can be changed under "
            "Allowed and Refused. A deny lasts at most for the run; “always” is remembered for the project.",
            seconds=self.seconds)))
        history_box = QGroupBox(tr("History"))
        history_layout = QVBoxLayout(history_box)
        # Whose history: this machine's (read at every scan) or a host's (`sandbox-ai approvals history` there, as
        # its table says it, when chosen or refreshed).
        history_row = QHBoxLayout()
        self.history_host = QComboBox()
        self.history_host.setAccessibleName(tr("History of"))
        self.history_host.setToolTip(tr("Whose answers: this machine's or a host's"))
        self.history_host.addItem(tr("This machine"), LOCAL)
        self.history_host.activated.connect(lambda _=0: self.load_history())
        history_refresh = QPushButton(tr("Refresh"))
        history_refresh.setAccessibleName(tr("Refresh the history"))
        history_refresh.clicked.connect(lambda: self.load_history())
        history_of = QLabel(tr("History of"))
        history_of.setBuddy(self.history_host)
        for widget in (history_of, self.history_host, history_refresh):
            history_row.addWidget(widget)
        history_row.addStretch()
        history_layout.addLayout(history_row)
        # A ModelTable: a long-running sandbox's history (hundreds of answers) is filled in one step.
        self.history = table([tr("Time"), tr("Sandbox"), tr("Question"), tr("Answer"), tr("Scope"), tr("Reason")],
                             stretch=2, compact=(0, 3, 4), many=True, multi=True)  # several: to copy them
        self.history.setAccessibleName(tr("History of the answers"))
        history_layout.addWidget(self.history)
        self.history_text = QPlainTextEdit()  # a host's history: its own table, escaped
        self.history_text.setReadOnly(True)
        self.history_text.setFont(MONO)
        self.history_text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.history_text.setAccessibleName(tr("History of the answers on the host chosen"))
        self.history_text.hide()
        history_layout.addWidget(self.history_text)
        self.history_token = 0
        always_box = QGroupBox(tr("Allowed or blocked always, per project: the next starts keep to it"))
        always_layout = QVBoxLayout(always_box)
        self.always = table([tr("Project"), tr("Allowed"), tr("Kind")], stretch=0, compact=(2,))
        self.always.setAccessibleName(tr("Allowed or blocked always"))
        widgets.multi_select(self.always)  # several at once (Ctrl, Shift): Forget… (and the Delete key) takes them all
        widgets.delete_key(self.always, lambda: self.forget(), tr("Forget"))
        always_layout.addWidget(self.always)
        row = QHBoxLayout()
        # Whose list: this machine's (sandbox-ai's file) or a host's (`sandbox-ai approvals remembered` there).
        self.always_host = QComboBox()
        self.always_host.setAccessibleName(tr("Remembered on"))
        self.always_host.setToolTip(tr("Whose list: this machine's or a host's"))
        self.always_host.addItem(tr("This machine"), LOCAL)
        self.always_host.activated.connect(lambda _=0: self.load_always())
        refresh = QPushButton(tr("Refresh"))
        refresh.setAccessibleName(tr("Refresh the remembered answers"))
        refresh.clicked.connect(lambda: self.load_always())
        self.forget_button = QPushButton(tr("Forget…"))
        self.forget_button.setAccessibleName(tr("Forget"))
        self.forget_button.setToolTip(tr("The project's sandboxes ask again next time"))
        self.forget_button.clicked.connect(lambda: self.forget())
        for widget in (self.always_host, refresh, self.forget_button):
            row.addWidget(widget)
        widgets.list_menu(self.always, lambda: [self.forget_button, None, refresh])
        row.addStretch()
        always_layout.addLayout(row)
        self.always_message = plain_label(role="secondary", selectable=True)
        always_layout.addWidget(self.always_message)
        # Which sandbox the page is about (the active tab's, one chosen, all), then its sections: the questions (the
        # window moves them into a panel of their own, above the page: its tab goes then), what was refused, what
        # it may reach now, what is remembered for the projects and the answers given.
        self.active_entry, self.last_runs = None, {}
        self.header = ContextHeader(sai, sai.approval_seconds)
        layout.addWidget(self.header)
        self.blocked = BlockedSection(sai, self.header)
        self.allowed = AllowedSection(sai, self.header)
        self.sections = QTabWidget()
        widgets.arrow_tabs(self.sections)  # (the app's tab row: arrows at its end when the tabs do not fit)
        self.sections.setAccessibleName(tr("Approvals"))
        self.history_box, self.always_box = history_box, always_box
        self.section_titles = {}
        # (Blocked and History are tabs of the one table now: Refused, Timed out, All. The Blocked section reads what
        # the sandboxes refused without a question for it, unseen, and does what is allowed about those.)
        for widget, title in ((open_box, tr("Questions")), (self.allowed, tr("Allowed now")),
                              (always_box, tr("Remembered"))):
            self.section_titles[widget] = title
            self.sections.addTab(widget, title)
        for hidden in (self.blocked, history_box):  # (children of the page: they go with it)
            hidden.setParent(self)
            hidden.hide()
        self.section_counts = {}
        self.blocked.counted.connect(lambda count: self.show_all())
        self.allowed.counted.connect(lambda count: self.count_section(self.allowed, count))
        self.header.changed.connect(self.context_changed)
        self.sections.currentChanged.connect(lambda _=0: self.section_shown())
        layout.addWidget(self.sections, 1)
        self.entries, self.questions, self.labels, self.seen, self.history_key = [], {}, {}, set(), None
        self.entry_keys, self.local_pending = [], []
        # The other hosts: {host: [RemoteQuestion]} as last read, why a host did not answer, the reads running
        # ({host: ThreadCall}) and the hosts to read once more when their read is done (after an answer).
        self.remote, self.remote_problems, self.remote_reading, self.remote_again = {}, {}, {}, set()
        self.remote_failures, self.remote_next = {}, {}  # hosts that do not answer: how often, when to ask again
        # When each host answered last (time.monotonic() when that read began), and the questions answered there
        # ({(host, run, id): time.monotonic() of the answer}) that a read begun after the answer still listed.
        self.remote_seen, self.answered_there = {}, {}
        self.hosts_reading, self.hosts_again, self.known_hosts = None, False, None
        # Questions only come from sandboxes that run: a host whose starts were read (the Sandboxes page, set_fleet)
        # and showed none running is not asked for questions at all (quiet), until a read of its starts shows one or
        # the app starts something there (woken: {host or None for every host: time.monotonic() until then}). This
        # machine the same way: while its registry lists no running sandbox, no question folder is read (scan).
        self.quiet_hosts, self.woken = set(), {}
        # The last scan of this machine applied ((open, answered, widest)): the same again, while no question counts
        # down anywhere, changes nothing on the page - nothing is done for it.
        self.local_read = None
        self.project, self.marked, self.project_pending = None, None, 0
        self.open_box = open_box
        self.records, self.contexts, self.always_token, self.always_loaded = {}, {}, 0, False
        # The answers on their way, per question ({(host, run, id): ThreadCall}): a slow host's answer (over SSH, up
        # to REMOTE_ANSWER_WAIT) never holds up the others, whose time runs.
        self.answers = {}
        self.answered_recently = set()  # questions answered from here, until they are no longer listed (the log)
        # Questions asked while a modal dialog blocks this page are offered in a window above it; the questions
        # open when it was closed are not offered again.
        self.questions_window, self.dismissed = None, set()
        # The page's own questions before an answer (Allow…, Block always…) are open: no Questions window above them,
        # where a second answer could be started on top of the first.
        self.confirming = 0
        # The keys of the answers (eventFilter): taken from the open questions and every widget in them (watch_keys),
        # while they are shown (keys_on).
        self.keys_on = False
        self.watch_keys(open_box)
        # The timer scans in a thread, so the questions are read even while something else keeps the window
        # busy; a newer scan (or a direct poll) makes an older result void.
        self.polling, self.generation = None, 0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll_soon)
        if interval:
            self.timer.start(interval)
        self.remote_timer = QTimer(self)
        self.remote_timer.timeout.connect(lambda: self.poll_remote(polled=True))
        remote_interval = (REMOTE_POLL if interval else 0) if remote_interval is None else remote_interval
        if remote_interval:
            self.remote_timer.start(remote_interval)
            QTimer.singleShot(0, lambda: self.poll_remote())
        # The app's page: its first scan in a thread too, after the window's first paint (MainWindow.eventFilter) or
        # at the timer's first tick (in a thread meanwhile it would slow the window's making and first paint down);
        # a page without its timer (the tests): scanned now.
        self.first_scan_due = bool(interval)
        if not interval:
            self.poll()

    def first_scan(self):
        """The window has painted itself: the first scan (in a thread), unless the timer did it already."""
        if self.first_scan_due:
            self.poll_soon()

    def poll_soon(self):
        """Scan in a thread (the timer's way); the result is applied on the GUI thread."""
        self.first_scan_due = False
        if self.polling is not None:
            return
        self.generation += 1
        token = self.generation
        self.polling = background(self.scan, SNAPSHOT_WAIT, timeout=SNAPSHOT_WAIT + 5,
                                  done=lambda result, error: self.polled(token, result, error))

    def scan(self, wait):
        """(open, answered) of this machine's questions and the policy's widest scope, read together. While no sandbox
        runs here (the registry, files and /proc only), no question folder and no policy is read: there can be no
        question (the widest scope stays as last read; it is read again with the first question)."""
        runs = self.sai.live_runs()
        self.last_runs = runs  # (read by update_context on the GUI thread: one assignment)
        if not runs:
            return self.sai.approvals_snapshot(runs={}, wait=wait), getattr(self, "max_scope", "once")
        # A session bridge (sandbox-ai bridge) asks from its own folder: its questions are answered here as well.
        bridges = self.sai.bridge_runs() if hasattr(self.sai, "bridge_runs") else {}
        snapshot = self.sai.approvals_snapshot(runs={**runs, **bridges}, wait=wait)
        try:
            widest = self.sai.max_scope()
        except Exception as _exc:  # noqa: BLE001
            # (a policy that cannot be read: the narrowest, sandbox-ai decides anyway)
            applog.swallowed("pages", "ApprovalsPage.scan: a policy that cannot be read: the narrowest, sandbox…", _exc)
            widest = "once"
        return snapshot, widest

    def polled(self, token, result, error):
        self.polling = None
        if token == self.generation:
            if error is None:
                result, self.max_scope = result
            self.apply(result, error)

    def poll(self, now=None):
        """Scan now (cached: a question file is parsed once; a few small files, no command)."""
        try:
            (result, self.max_scope), error = self.scan(POLL_WAIT), None
        except TimeoutError:  # a scan in a thread holds the cache (maybe stuck): its result, or the next, comes
            self.poll_soon()
            return
        except Exception as exc:  # noqa: BLE001 - shown, polled again
            result, error = None, exc
        self.generation += 1
        self.apply(result, error, now)

    def apply(self, result, error, now=None):
        if error is not None:
            applog.changed(("approvals host", LOCAL), "WARNING", "approvals", "this machine's questions cannot be "
                           f"read: {applog.error_text(error)}", quiet="DEBUG")
            self.local_read = None
            self.message.setText(tr("Cannot read the questions: {error}", error=printable_text(error)))
            return
        self.update_context()
        # What the sandboxes refused without a question (the Refused tab): read again now and then while it is shown.
        if self.state_tab() in ("refused", "all") and self.shown_now() and \
                time.monotonic() - getattr(self, "blocked_read", 0) > BLOCKED_REFRESH:
            self.blocked_read = time.monotonic()
            self.blocked.refresh()
        pending, answered = result
        read, previous = (pending, answered, self.max_scope), self.local_read
        self.local_read = read
        if read == previous and not any(entry_state(q) == "open" for _, q, _ in self.questions.values()):
            return  # nothing new, and no question anywhere whose time runs: the page stays as it is
        self.local_pending, self.local_answered = pending, answered
        if previous is None or answered != previous[1]:
            self.show_history(answered)
        self.show_all(now)

    def show_all(self, now=None):
        """Every question of this machine and of the other hosts in one table (the tab chosen: open, allowed, refused,
        timed out, all), with what the sandboxes here refused without a question (Refused); new open ones are
        announced once, and only the open ones are counted as waiting."""
        rows = [(LOCAL, run, record, q) for run, record, q in self.local_pending] + \
            [(LOCAL, run, record, q) for run, record, q in getattr(self, "local_answered", [])] + [
            (host, q.get("run", ""), {}, q) for host in sorted(self.remote) for q in self.remote[host]]
        known = {(host, run, q.get("id")) for host, run, _, q in rows}
        rows += [entry for entry in self.blocked_rows() if (entry[0], entry[1], entry[3].get("question_id")) not in known]
        self.show_open(rows, now)
        pending = [row for row in rows if entry_state(row[3]) == "open"]
        keys = {(host, run, q.get("id")) for host, run, _, q in pending}
        new = [(self.label_of(host, run, record, q), q, (host, run, q.get("id"))) for host, run, record, q in pending
               if (host, run, q.get("id")) not in self.seen]
        self.log_questions(new, keys)
        self.seen = keys
        self.offer_questions()
        self.pending_changed.emit(len(pending), new)

    def blocked_rows(self):
        """What the sandboxes shown above (Allowed now's choice) refused without a question of their own - by a rule,
        the network options, the mode, the policy, the kill switch (sandbox-ai approvals blocked) - as rows of the
        Refused tab: [(host, run, record, entry)]; a refusal of a question is that question's own row."""
        found = []
        for item in getattr(self.blocked, "entries", []) or []:
            if item.get("kind") in ("unanswered", "denied") and item.get("question"):
                continue  # (its question's own row says it)
            host = item.get("host") or LOCAL
            run = str(item.get("run") or "")
            ident = "blocked:" + json.dumps([item.get("kind"), item.get("what"), item.get("source")], sort_keys=True)
            found.append((host, run, self.header.runs.get(run, {}), BlockedRow(item, ident)))
        return found

    def state_tab_changed(self):
        """Another tab of the table: its rows; what was refused without a question is read when it is shown."""
        if self.state_tab() in ("refused", "all"):
            self.blocked.refresh()
        self.show_all()

    def state_tab(self):
        return self.state_tabs.tabData(self.state_tabs.currentIndex()) or "open"

    def log_questions(self, new, keys):
        """The questions that came (NEW) and those no longer listed (SEEN before, not in KEYS) into the log at INFO,
        once each: who asks, on which host, what (redacted) and how long it may wait; a question gone without an answer
        from here was answered elsewhere, timed out (sandbox-ai refused it) or its start ended."""
        for label, question, key in new:
            try:
                left = self.timing((key[1], question, key[0]))[1]
            except Exception as exc:  # noqa: BLE001 - only the line's note
                applog.swallowed("pages", "ApprovalsPage.log_questions: the time left of a question", exc)
                left = None
            where = "this machine" if key[0] == LOCAL else printable(key[0])
            timed = isinstance(left, (int, float)) and left
            wait = f" ({left:.0f} s to answer it, else it is refused)" if timed else ""
            applog.log("INFO", "approvals", f"question {printable(key[2])[:60]} from {printable(label)} on {where}: "
                       f"{applog.redact(describe(question))[:300]}{wait}")
        for key in sorted(set(getattr(self, "seen", ()) or ()) - set(keys), key=str):
            mine = key in self.answered_recently or key in self.answered_there
            applog.log("INFO" if not mine else "DEBUG", "approvals", f"question {printable(key[2])[:60]} on "
                       f"{'this machine' if key[0] == LOCAL else printable(key[0])} is no longer asked"
                       + (" (answered here)" if mine else " (answered elsewhere, its time ran out - sandbox-ai refused "
                                                         "it - or its start ended)"))
            self.answered_recently.discard(key)

    def label_of(self, host, run, record, question):
        """Which sandbox asks, and on which host when it is not this machine."""
        if host == LOCAL:
            return asker(run, record)
        return tr("{sandbox} on {host}", sandbox=printable(question.get("sandbox") or run), host=printable(host))

    # -- the other hosts --

    def poll_remote(self, hosts=None, polled=False):
        """Read the open questions of the other hosts in threads, one read per host at a time: HOSTS (after an answer
        there; read once more when a read of it runs already), or every host of `sandbox-ai hosts`."""
        if hosts is not None:
            for host in hosts:
                if host in self.remote_reading:
                    self.remote_again.add(host)
                else:
                    self.read_host(host)
            return
        # A host whose read hangs (it gives up only after its timeout): its questions go after REMOTE_GRACE all the same.
        if [host for host in list(self.remote_problems) if host in self.remote and self.expire_remote(host)]:
            self.show_all()
        if self.hosts_reading is not None:
            self.hosts_again = True
            return
        self.hosts_polled = polled  # (the timer: hosts that did not answer wait; a refresh asks every host)
        self.hosts_reading = background(self.sai.remote_hosts, timeout=30, done=self.hosts_listed)

    def forget_host(self, name):
        """Host NAME was removed from the list (the Hosts page): its questions go at once, not at the next read."""
        if self.known_hosts is not None:
            self.known_hosts.discard(name)
        self.drop_host(name)
        self.remote_again.discard(name)
        self.fill_hosts(sorted(self.known_hosts or ()))
        self.show_remote_note()
        self.show_all()

    def drop_host(self, name):
        for store in (self.remote, self.remote_problems, self.remote_seen, self.remote_failures, self.remote_next):
            store.pop(name, None)
        for key in [key for key in self.answered_there if key[0] == name]:
            del self.answered_there[key]

    def fill_hosts(self, hosts):
        """The host choices of the question filter and of the remembered list: the choice stays while its host does
        (filled again only when the hosts changed: an open list must not close under the mouse every few seconds)."""
        if list(hosts) == getattr(self, "filled_hosts", None):
            return
        self.filled_hosts = list(hosts)
        remembered_on, history_of = self.always_host.currentData(), self.history_host.currentData()
        for combo, extra in ((self.host_filter, [(tr("All hosts"), None)]), (self.always_host, []),
                             (self.history_host, [])):
            chosen = combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            for text, data in extra + [(tr("This machine"), LOCAL)] + [(printable(host), host) for host in hosts]:
                combo.addItem(text, data)
            index = combo.findData(chosen)
            combo.setCurrentIndex(index if index >= 0 else 0)
            combo.blockSignals(False)
        if self.always_host.currentData() != remembered_on and self.always_loaded:
            self.load_always()  # its host is gone: this machine's list, not that host's rows under another name
        if self.history_host.currentData() != history_of:
            self.load_history()

    def show_host(self, host):
        """List only HOST's questions (None: every host's), e.g. for Hosts › Show questions."""
        index = self.host_filter.findData(host)
        if index < 0 and host is not None:  # not read yet (the page was not polled since the host came)
            self.fill_hosts(sorted({*(self.known_hosts or ()), host}))
            index = self.host_filter.findData(host)
        self.host_filter.setCurrentIndex(max(0, index))
        self.show_all()

    def select_question(self, key):
        """Select the question KEY (host, run, id), e.g. from its notification; False when it is gone."""
        if key not in self.questions:
            return False
        if self.host_filter.currentData() not in (None, key[0]):
            self.host_filter.setCurrentIndex(0)  # every host's: the question must be seen
        self.show_all()
        row = find_row(self.open, key)
        if row < 0:
            return False
        self.open.clearSelection()
        self.open.selectRow(row)
        self.open.setFocus()
        return True

    def hosts_listed(self, hosts, error):
        self.hosts_reading = None
        hosts = list(hosts or []) if error is None else list(self.remote)
        self.known_hosts = set(hosts)
        self.fill_hosts(sorted(hosts))
        for gone in (set(self.remote) | set(self.remote_problems)) - set(hosts):
            self.drop_host(gone)
        now = time.monotonic()
        for host in hosts:
            # A host that did not answer is asked again after a while (remote_next), not every REMOTE_POLL: each look
            # is a Python process and an ssh that waits for its timeout. One where nothing runs is not asked (quiet).
            if host not in self.remote_reading and (not getattr(self, "hosts_polled", False)
                                                    or now >= self.remote_next.get(host, 0)) \
                    and not self.quiet(host, now):
                self.read_host(host)
        if self.hosts_again:
            self.hosts_again = False
            self.poll_remote()
        self.show_remote_note()
        self.show_all()

    def quiet(self, host, now=None):
        """True while HOST is not asked for questions: its starts were read last with none running, it lists no
        question, and the app has not started anything there within WAKE_SECONDS."""
        if host not in self.quiet_hosts or self.remote.get(host):
            return False
        now = time.monotonic() if now is None else now
        return not any(now < self.woken.get(key, 0) for key in (host, None))

    def set_fleet(self, fleet):
        """What the last reads of the hosts' starts showed (SandboxesPage.fleet: {host: {"active": starts running,
        paused or starting, None when not known}}): a host with none is not asked for questions any more; one that
        has one again (or is not known any more) is asked again at once."""
        for host, seen in (fleet or {}).items():
            if host == LOCAL:
                continue
            active = seen.get("active") if isinstance(seen, dict) else None
            if active == 0:
                if host not in self.quiet_hosts:
                    self.quiet_hosts.add(host)
                    if self.quiet(host):  # not asked any more: no note about its last read either
                        for store in (self.remote_problems, self.remote_failures, self.remote_next):
                            store.pop(host, None)
                        self.show_remote_note()
            elif host in self.quiet_hosts:
                self.quiet_hosts.discard(host)
                self.wake(host)

    def wake_hosts(self, hosts=None, seconds=WAKE_SECONDS):
        """The app started something on HOSTS (None: wherever, e.g. in a terminal): they are asked for questions for
        at least SECONDS from now on, whatever the last read of their starts said, and at once."""
        until = time.monotonic() + seconds
        for key in ([None] if hosts is None else hosts):
            if key != LOCAL:
                self.woken[key] = max(until, self.woken.get(key, 0))
        for host in (sorted(self.quiet_hosts) if hosts is None else hosts):
            if host != LOCAL and host in self.quiet_hosts:
                self.wake(host)

    def wake(self, host):
        """HOST is asked for questions again: now (or once more after the read that runs), whatever its back-off."""
        self.remote_next.pop(host, None)
        if self.known_hosts is not None and host not in self.known_hosts:
            return
        if host in self.remote_reading:
            self.remote_again.add(host)
        else:
            self.read_host(host)

    def read_host(self, host):
        started = time.monotonic()
        self.remote_reading[host] = background(
            self.sai.host_questions, host, timeout=backend.REMOTE_QUESTION_WAIT + 15,
            done=lambda result, error, h=host: self.host_read(h, result, error, started))

    def host_read(self, host, result, error, started=None):
        self.remote_reading.pop(host, None)
        started = time.monotonic() if started is None else started
        if self.known_hosts is not None and host not in self.known_hosts:
            self.remote_again.discard(host)
            return  # removed from the list meanwhile: its questions are not answered from here any more
        if error is None:
            self.remote[host] = list(result or [])
            if self.remote_problems.get(host) is not None:
                applog.changed(("approvals host", host), "INFO", "approvals", f"the questions of {printable(host)} "
                               "are read again")
            self.remote_problems.pop(host, None)
            self.remote_next.pop(host, None)
            self.remote_failures.pop(host, None)
            self.remote_seen[host] = started
            # An answer there counts until a read that began after it no longer lists the question.
            listed = {(host, q.get("run", ""), q.get("id")) for q in self.remote[host]}
            for key, at in list(self.answered_there.items()):
                if key[0] == host and started >= at and key not in listed:
                    del self.answered_there[key]
        else:
            # Its last questions stay while their time runs, at most REMOTE_GRACE seconds after its last answer (a
            # host that answers again soon must not announce them anew; one that stopped answering must not keep
            # them, a page to open with no deadline included, for ever).
            self.remote_problems[host] = printable_text(error)[:200]
            applog.changed(("approvals host", host), "WARNING", "approvals", f"the questions of {printable(host)} "
                           f"cannot be read (asked again later): {applog.error_text(error)}", quiet="DEBUG")
            self.expire_remote(host)
            failures = self.remote_failures[host] = self.remote_failures.get(host, 0) + 1
            self.remote_next[host] = time.monotonic() + min(REMOTE_BACKOFF, REMOTE_POLL / 1000 * 2 ** failures)
        if host in self.remote_again:
            self.remote_again.discard(host)
            self.read_host(host)
        self.show_remote_note()
        self.show_all()

    def expire_remote(self, host, now=None):
        """The questions of HOST, a host that does not answer, that may still be listed: those whose time runs, while
        its last answer is at most REMOTE_GRACE seconds old."""
        now = time.monotonic() if now is None else now
        seen = self.remote_seen.get(host)
        kept = [q for q in self.remote.get(host, []) if q.timing()[1] != 0] \
            if seen is not None and now - seen <= REMOTE_GRACE else []
        if kept:
            self.remote[host] = kept
        elif self.remote.pop(host, None) is not None:
            for key in [key for key in self.answered_there if key[0] == host]:
                del self.answered_there[key]
            return True
        return False

    def show_remote_note(self):
        problems = sorted(self.remote_problems.items())
        self.remote_note.setVisible(bool(problems))
        self.remote_note.setText("\n".join(tr("{host}: no answer", host=printable(host)) + f" ({text})"
                                           for host, text in problems))

    def open_keys(self):
        """The questions that still wait for an answer from here, in their order: not those whose answer is on its
        way or taken by another host - one answer anywhere settles a question everywhere."""
        return [key for key in self.questions if not self.settled(key)]

    def offer_questions(self):
        """While a modal dialog blocks the window, open questions are shown in a window above it; one answered
        already (from its notification, say) is never offered again."""
        keys = set(self.open_keys())
        self.dismissed &= keys
        window = self.questions_window
        if window is not None and window.isVisible():
            if keys:
                window.show_questions()
            else:
                window.hide()
            return
        if not keys - self.dismissed or not self.blocked_by_dialog():
            return
        if window is None:
            window = self.questions_window = QuestionsWindow(self)
            window.finished.connect(lambda _=0: self.dismissed.update(self.questions))
            self.watch_keys(window)  # its Show and Hide turn the keys of the answers on and off
        window.show_questions()
        window.show()
        window.raise_()
        window.activateWindow()

    def blocked_by_dialog(self):
        """True while a modal dialog of another part of the app (a message, the wizard, Settings, the gallery) blocks
        the window: then the open questions are offered above it. Never over the answers' own confirmations (Allow…,
        Block always…, from this page or the Questions window): a second answer must not be stacked on the first."""
        modal = QApplication.activeModalWidget()
        if modal is None or self.confirming:
            return False
        window = self.questions_window
        return modal is not window and not (window is not None and window.isAncestorOf(modal))

    def set_project(self, path):
        """Questions of the current project's sandboxes are shown in bold, and counted in the box's title."""
        self.project = os.path.realpath(path) if path else None
        self.poll()

    # -- which sandbox the page is about --

    def set_active_tab(self, entry):
        """The window's active tab (a project's, a session's or a run's entry): Blocked and Allowed now follow it."""
        self.active_entry = dict(entry) if isinstance(entry, dict) else None
        self.update_context()

    def set_remote_starts(self, starts):
        """The running starts of the other hosts ({host: [(name, record)]}, SandboxesPage.remote_running): Blocked and
        Allowed now offer each of them (read and changed there)."""
        if self.header.set_remote(starts):
            self.context_changed()

    def update_context(self):
        runs = dict(getattr(self, "last_runs", {}) or {})
        if self.header.set_runs(runs, run_for_tab(self.active_entry, runs)):
            self.context_changed()
        else:
            self.header.show_state()

    def context_changed(self):
        self.header.show_state()
        # Hidden in the window (another page shown, the window in the tray): read when the page is shown, not at
        # every switch of the tabs - each was a Blocked read (the newest 4 MiB of every log) and a `sandbox-ai net
        # show` (a Python start) for a page nobody looked at.
        if not self.shown_now():
            self.context_stale = True
            return
        self.context_stale = False
        self.blocked.refresh(force=True)
        self.allowed.refresh()

    def shown_now(self):
        """False while the page sits hidden in its window; a page on its own (no window: the tests) always True."""
        return self.window() is self or self.isVisible()

    def section_shown(self):
        current = self.sections.currentWidget()
        if current is self.blocked:
            self.blocked.refresh()
        elif current is self.allowed:
            self.allowed.refresh()

    def count_section(self, widget, count):
        """A section's tab says how many entries it has."""
        index = self.sections.indexOf(widget)
        if index >= 0:
            title = self.section_titles.get(widget, "")
            self.sections.setTabText(index, f"{title} ({count})" if count else title)

    def of_project(self, record):
        workspace = record.get("workspace") if isinstance(record, dict) else None
        return bool(self.project and workspace and os.path.realpath(str(workspace)) == self.project)

    def show_open(self, pending, now=None):
        """PENDING: [(host, run, record, question)] of every state, LOCAL ones first; the table lists those of the
        host and the tab chosen."""
        keys = [(host, run, q.get("id")) for host, run, _, q in pending]
        waiting = [entry for entry in pending if entry_state(entry[3]) == "open"]
        self.project_pending = sum(1 for host, _, record, _ in waiting if host == LOCAL and self.of_project(record))
        elsewhere = sum(1 for host, *_ in waiting if host != LOCAL)
        title = tr("Questions of every host")
        if elsewhere:
            title += "  " + tr("({count} waiting on other hosts)", count=elsewhere)
        if self.project_pending:
            title += "  " + tr("({count} of the current project)", count=self.project_pending)
        self.open_box.setTitle(title)
        if hasattr(self, "none_waiting"):
            self.count_section(self.open_box, len(waiting))
        # The questions themselves are taken every time (a host's newer read counts down from its own time left).
        self.questions = {key: (run, q, host) for key, (host, run, _, q) in zip(keys, pending)}
        self.labels = {key: self.label_of(host, run, record, q) for key, (host, run, record, q) in zip(keys, pending)}
        self.records = {key: record for key, (_, _, record, _) in zip(keys, pending)}
        # The table lists the questions of the host and the tab chosen (all are counted, announced and offered).
        chosen = self.host_filter.currentData()
        pending = [entry for entry in pending if chosen is None or entry[0] == chosen]
        counts = {key: 0 for key, _ in STATE_TABS}
        for entry in pending:
            counts[entry_state(entry[3])] += 1
            counts["all"] += 1
        for index, (key, label) in enumerate(STATE_TABS):
            text = f"{tr(label)} ({counts[key]})"
            if self.state_tabs.tabText(index) != text:
                self.state_tabs.setTabText(index, text)
        tab = self.state_tab()
        pending = [entry for entry in pending if tab == "all" or entry_state(entry[3]) == tab]
        if tab != "open":  # the newest first (the open ones keep the order they were asked in)
            pending.sort(key=lambda entry: str(entry[3].get("time") or ""), reverse=True)
        if hasattr(self, "none_waiting"):
            self.none_waiting.setVisible(not pending)
            self.none_waiting.setText(tr("Nothing is waiting for an answer.") if tab == "open" else
                                      tr("Nothing here."))
        keys = [(host, run, q.get("id")) for host, run, _, q in pending]
        states = [state_text(q) for _, _, _, q in pending]
        if keys != self.entry_keys or self.marked != self.project or states != getattr(self, "entry_states", None):
            self.marked = self.project
            had_selection = selected_rows(self.open) != []
            self.entry_keys, self.entry_states = keys, states
            self.entries = [(run, q, host) for host, run, _, q in pending]
            self.contexts = {key: text for key, text in self.contexts.items() if key in self.questions}
            # fill() selects the highlighted questions again where they now are, or nothing when they are gone.
            fill(self.open, [[asker(run, record) if host == LOCAL else printable(q.get("sandbox") or run),
                              kind_name(q.get("kind")), describe(q), "", state_text(q),
                              tr("this machine") if host == LOCAL else printable(host)]
                             for host, run, record, q in pending],
                 colors=[{"allowed": "ok", "refused": "bad", "timeout": "warn"}.get(entry_state(q))
                         for _, _, _, q in pending], keys=keys)
            if not had_selection and self.current() is None and len(self.entries) == 1 and tab == "open":
                # The one open question (e.g. after its notification) is ready to answer; a selection that
                # just lost its question never moves to another one.
                self.open.selectRow(0)
            for row in range(self.open.rowCount()):
                key = row_key(self.open, row)
                entry = self.questions.get(key)
                mine = self.of_project(self.records.get(key))
                for column in range(self.open.columnCount()):
                    if mine or (column == 2 and entry and entry[1].get("kind") == "browser"):
                        item = self.open.item(row, column)
                        font = item.font()
                        font.setBold(True)
                        item.setFont(font)
                        if mine and column == 0:
                            item.setToolTip(safe_tooltip(tr("A sandbox of the current project")))
            fit_columns(self.open)  # bold text is wider
            self.selection_changed()
        # The cells themselves, collected first: setting them may move rows when the table is sorted by them.
        cells = [(self.questions.get(row_key(self.open, row)), self.open.item(row, 3), self.open.item(row, 4))
                 for row in range(self.open.rowCount())]
        before = [(asked.text(), cell.text()) for _, asked, cell in cells if asked is not None and cell is not None]
        for entry, asked, cell in cells:
            if entry is None or asked is None or cell is None:
                continue
            age, left = self.timing(entry, now)
            asked.setText(ago(age) if age is not None else "?")
            asked.setData(SORT_ROLE, age if age is not None else -1)
            if entry_state(entry[1]) != "open":
                continue  # (said once by state_text: answered, refused or timed out)
            if self.key_of(entry) in self.answered_there:
                cell.setText(tr("answered"))
                cell.setData(SORT_ROLE, 1 << 30)
                cell.setForeground(QColor(theme.color("muted")))
            elif left is None:
                cell.setText(tr("no time limit"))
                cell.setData(SORT_ROLE, 1 << 30)
                cell.setForeground(self.open.palette().text())
            else:
                cell.setText(tr("{seconds} s", seconds=left) if left else tr("timed out"))
                cell.setData(SORT_ROLE, left)
                color = "bad" if left <= 10 else "warn" if left <= 30 else None
                cell.setForeground(QColor(theme.color(color)) if color else self.open.palette().text())
        after = [(asked.text(), cell.text()) for _, asked, cell in cells if asked is not None and cell is not None]
        if any(len(new) > len(old) for pair_before, pair_after in zip(before, after)
               for old, new in zip(pair_before, pair_after)):
            # The columns were fitted before these cells had their text ("39…go"): wider where it grew.
            fit_columns(self.open)
        self.update_buttons()

    def timing(self, entry, now=None):
        """(age, seconds left) of ENTRY (run, question, host); another host's counts down from its own time left."""
        question = entry[1]
        if isinstance(question, backend.RemoteQuestion):
            return question.timing(now)
        return backend.question_timing(question, now, default=self.seconds)

    def show_history(self, answered):
        key = tuple((run, q.get("id"), json.dumps(q.get("result"), sort_keys=True)) for run, _, q in answered)
        if key == self.history_key:
            return
        self.history_key = key
        rows, colors, keys = [], [], []
        for run, record, q in sorted(answered, key=lambda e: str(e[2].get("time", "")), reverse=True):
            result = q.get("result") if isinstance(q.get("result"), dict) else {}
            decision = result.get("decision")
            rows.append([printable(str(q.get("time", "")).replace("T", " ").rstrip("Z")), asker(run, record), describe(q),
                         {"allow": tr("allowed"), "deny": tr("denied")}.get(decision, tr("open")),
                         printable(result.get("scope", "")), printable(result.get("reason", ""))])
            colors.append("ok" if decision == "allow" else "bad" if decision == "deny" else None)
            keys.append((run, q.get("id")))
        fill(self.history, rows, colors, keys=keys)

    def current(self):
        """(run, question, host) of the highlighted row, found by its key; None when nothing is selected."""
        rows = selected_rows(self.open)
        return self.questions.get(row_key(self.open, rows[0])) if rows else None

    def chosen(self):
        """(run, question, host) of every row selected, in the table's order."""
        keys = [row_key(self.open, row) for row in selected_rows(self.open)]
        return [self.questions[key] for key in keys if key in self.questions]

    def past_actions(self, entry):
        """What may be done about ENTRY, a question answered already (or an older sandbox's refusal of one nobody
        answered): allow, deny, ask-again - as its host's sandbox-ai says (approvals.entry_actions; another host
        lists them with the question)."""
        run, question, host = entry
        if isinstance(question, BlockedRow):
            return []
        if host != LOCAL:
            return list(question.get("actions") or [])
        try:
            module = self.sai.module("approvals")
            if not hasattr(module, "entry_actions"):
                return []
            return list(module.entry_actions({**question, "state": entry_state(question)},
                                             self.records.get(self.key_of(entry)) or {}, self.max_scope))
        except Exception as exc:  # noqa: BLE001 - nothing offered for it, said in the log
            applog.swallowed("pages", "ApprovalsPage.past_actions", exc)
            return []

    def fits(self, entry, action):
        """None when ACTION (allow - once -, allow-for-run, allow-always, deny, deny-once, block, ask-again) may be
        done on ENTRY, else why not."""
        run, question, host = entry
        scope = SCOPE_OF.get(action)
        if isinstance(question, BlockedRow):
            if scope == "once":
                return tr("Refused without a question: Always allow allows it for the rest of this run.")
            wanted = {"allow-for-run": "allow-run", "allow-always": "allow-always", "deny": "deny-run",
                      "ask-again": "ask-again"}.get(action)
            return None if wanted and wanted in (question["blocked"].get("actions") or []) else \
                tr("Not possible for this refusal (its reason, the security mode or the administrator's policy).")
        if backend.answerable(question):
            if action == "ask-again":
                return tr("Still open: its next try waits for your answer.")
            if scope is not None:
                return self.answer_rules(entry)["allow"] or self.scope_refused(entry, scope)
            return self.answer_rules(entry)["deny" if action == "deny-once" else action]
        if self.key_of(entry) in self.answers:
            return tr("The answer is on its way.")
        if scope == "once":
            return tr("Answered already: Always allow allows it for the rest of this run.")
        if scope is not None and "allow" in self.past_actions(entry):
            return self.scope_refused(entry, scope)
        if action in self.past_actions(entry):
            return None
        return tr("Answered already: change it with Allow, Deny or Ask again where they are offered.") \
            if action in ("deny-once", "block") else tr("Not possible for this answer.")

    @property
    def answering(self):
        """An answer on its way (any question's), or None when none is."""
        return next(iter(self.answers.values()), None)

    @staticmethod
    def key_of(entry):
        run, question, host = entry
        return host, run, question.get("id")

    def widest(self, entry):
        """The widest allow ENTRY may get: this machine's policy, the run's security mode and the kind (as sandbox-ai
        caps it), or what the other host said of it (the same, there). A deny is never capped."""
        if entry is not None and entry[2] != LOCAL:
            scope = entry[1].get("max_scope")
            return scope if scope in backend.SCOPES else "once"
        if entry is not None:
            return local_widest(self.max_scope, self.records.get(self.key_of(entry)), entry[1])
        return self.max_scope if self.max_scope in backend.SCOPES else "once"

    def expired(self, entry, now=None):
        """True when sandbox-ai refused ENTRY already (its time is up and its sandbox-ai, older than 0.1.98, keeps no
        question open); a question to open a page has no deadline, one that timed out stays answerable."""
        if entry[1].get("kind") == "browser" or entry_state(entry[1]) != "open" or entry[1].get("state") == "open" \
                or (entry[2] == LOCAL and self.records.get(self.key_of(entry), {}).get("ask_timeout") is not None):
            return False
        return self.timing(entry, now)[1] == 0

    def answer_rules(self, entry):
        """{"allow", "deny", "block"}: None where the button may answer ENTRY, else why not."""
        if entry is None:
            why = tr("Select a question first.")
        elif self.key_of(entry) in self.answers:
            why = tr("The answer is on its way.")
        elif self.key_of(entry) in self.answered_there:
            why = tr("Answered: {host} lists it until it has read the answer.", host=printable(entry[2]))
        elif self.expired(entry):
            why = tr("Its time is up: sandbox-ai refused it already.")
        else:
            why = None
        rules = {"allow": why, "deny": why, "block": why}
        if why is None and entry[2] != LOCAL:
            rules["allow"] = self.remote_allow_refused(entry)
        elif why is None and self.widest(entry) is None:
            rules["allow"] = tr("This run approves at most once, and an allowed name stays pinned for the run: deny "
                                "it, or allow the name at the next start (--allow-host).")
        if why is None and entry[1].get("kind") not in ("connect", "dns"):
            rules["block"] = tr("Only names and addresses can be blocked always.")
        return rules

    @staticmethod
    def remote_allow_refused(entry):
        """Why another host's question ENTRY cannot be allowed from here, or None: that host allows no allow (its
        policy, the run's mode), or its page cannot be opened here (refused there, not http/https, not whole)."""
        question = entry[1]
        if question.get("allow_refused") or question.get("max_scope") is None:
            return tr("On {host} this can only be denied: {reason}", host=printable(entry[2]),
                      reason=printable(question.get("allow_refused") or "?"))
        if question.get("kind") != "browser":
            return None
        if question.get("url_refused"):
            return tr("{host} does not let the page be opened elsewhere: {reason}", host=printable(entry[2]),
                      reason=printable(question["url_refused"]))
        if question.get("url_truncated"):
            return tr("The address came shortened from {host}: it is not opened here.", host=printable(entry[2]))
        if openable_url(question.get("url")) is None:
            return tr("Only http and https pages are opened here.")
        return None

    def update_buttons(self):
        chosen = self.chosen()
        entry = chosen[0] if chosen else None
        # Another host's page is opened here, where the human is: that host may have no screen at all.
        self.show_allow_text(entry)
        # Each button on the rows selected it fits; none fits: disabled, its tooltip says why (the first row's).
        for button, action in ((self.allow_button, "allow"), (self.allow_run_button, "allow-for-run"),
                               (self.allow_always_button, "allow-always"), (self.deny_button, "deny"),
                               (self.deny_once_button, "deny-once"), (self.block_button, "block"),
                               (self.ask_again_button, "ask-again")):
            why = [self.fits(one, action) for one in chosen]
            possible = any(reason is None for reason in why)
            set_enabled(button, possible, None if possible else (why[0] if why else tr("Select a question first.")))
        if self.questions_window is not None:
            self.questions_window.update_buttons()

    def show_allow_text(self, entry=None):
        """Allow once says what it does (another host's page: Open here and allow - opened here, allowed there)."""
        entry = self.current() if entry is None else entry
        self.allow_button.setText(tr("Open here and allow") if remote_browser(entry) else tr("Allow once"))
        self.allow_button.setAccessibleName(self.allow_button.text())

    def watch_keys(self, root):
        """The keys of the answers are taken from ROOT (the open questions, the Questions window) and every widget in
        it, also one added later: a filter on each of them. Never one on the whole application: a filter in Python
        sees every event of every object there, Chromium's of the terminal tabs too, and while it was on (the open
        questions shown), the next terminal tab ended the process - a segmentation fault (stability round 3, the
        soak)."""
        for widget in [root, *root.findChildren(QWidget)]:
            if not widget.property("answerKeys"):
                widget.setProperty("answerKeys", True)
                widget.installEventFilter(self)

    def eventFilter(self, watched, event):  # noqa: N802
        """Ctrl+Return (allow), Ctrl+Shift+D (deny for the run) and Ctrl+Shift+K (block always, asked first) while the
        focus is in the open questions (on this page, in their panel wherever it is docked, or in the Questions
        window): taken before any shortcut of the window, while one of them is shown (update_key_filter)."""
        kind = event.type()
        if kind in (QEvent.Type.Show, QEvent.Type.Hide) and (
                watched is self.open_box or watched is self.questions_window):
            if kind == QEvent.Type.Show:
                self.watch_keys(watched)
            QTimer.singleShot(0, self.update_key_filter)  # once the change of visibility is done
            return False
        if kind == QEvent.Type.ChildPolished and isinstance(event.child(), QWidget):
            self.watch_keys(event.child())  # (a widget put in later: ready by now, unlike at ChildAdded)
            return False
        if kind in (QEvent.Type.ShortcutOverride, QEvent.Type.KeyPress) and self.keys_on \
                and isinstance(watched, QWidget):
            action = shortcut_of(event)
            if action in PLAIN_KEYS and not in_question_list(watched):
                action = None  # Enter and Delete answer only in a list of questions (a box takes them otherwise)
            target = self.key_target(watched) if action else None
            if target is not None:
                event.accept()
                if kind == QEvent.Type.KeyPress:
                    target.shortcut(action)
                return True
        return False

    def key_target(self, widget):
        """Who answers a key pressed in WIDGET: the Questions window, this page (its open questions), or None."""
        window = self.questions_window
        if window is not None and window.isVisible() and (widget is window or window.isAncestorOf(widget)):
            return window
        box = self.open_box
        if box.isVisible() and (widget is box or box.isAncestorOf(widget)):
            return self
        return None

    def update_key_filter(self):
        window = self.questions_window
        try:
            self.keys_on = self.open_box.isVisible() or (window is not None and window.isVisible())
        except RuntimeError:  # the page is being deleted
            return

    def shortcut(self, action):
        if action == "allow-run":
            # Enter in the list: Always allow (for the rest of this run) where the question may get it, else Allow
            # once (a canary's alarm, a page to open, a policy or mode that approves only once).
            entry = self.current()
            if entry is not None and self.fits(entry, "allow-for-run") is None:
                self.act("allow-for-run")
            elif entry is not None and self.allow_button.isEnabled():
                self.act("allow")
            return
        button = {"allow": self.allow_button, "always": self.allow_run_button, "always-later": self.allow_always_button,
                  "deny": self.deny_button, "deny-key": self.deny_button, "block": self.block_button}[action]
        if button.isEnabled():
            button.click()

    def selection_changed(self):
        entry = self.current()
        why = why_asked(entry[1]) if entry else ""
        self.why.setText(why)
        self.why.setVisible(bool(why))
        self.arguments.show_for(entry[1] if entry else None)
        url = backend.question_url(entry[1]) if entry else None
        destination = entry[1].get("url_host") if entry and entry[2] != LOCAL and entry[1].get("kind") == "browser" \
            else None
        self.url.setVisible(bool(url or destination))
        if url and entry[2] != LOCAL:
            self.url.setText(tr("{host} asks to open: {url}", host=printable(entry[2]), url=url)
                             + (" " + tr("(shortened)") if entry[1].get("url_truncated") else ""))
        elif destination:  # the page itself stays on that host (url_refused): where it leads is said
            self.url.setText(tr("{host} asks to open a page on {destination}", host=printable(entry[2]),
                                destination=printable(destination)))
        else:
            self.url.setText(tr("Open in your browser: {url}", url=url) if url else "")
        self.update_buttons()
        self.show_context()

    def details_chosen(self, on):
        """Details opened or folded by hand: so for every question from now on (one that needs them still opens
        them by itself), and at the next start."""
        self.details_wanted = bool(on)
        if self.details_store is not None:
            self.details_store.section("checks")["approvals-details"] = bool(on)
            self.details_store.schedule()

    def keep_details(self, store):
        """Whether Details is open, kept in STORE (the window's State) as the human leaves it."""
        self.details_store = store
        self.details_wanted = store.section("checks").get("approvals-details") is True
        self.details_key = None
        self.show_details()

    def show_details(self):
        """Details for the selected question: there when it has any; open as the human wants it, or by itself for a
        question that cannot be answered safely from its line (needs_details). The same question again keeps what
        was done by hand."""
        entry = self.current()
        has = entry is not None and bool(self.why.text() or not self.arguments.isHidden() or self.context.text())
        self.details.setVisible(has)
        key = (entry[2], entry[0], entry[1].get("id")) if entry is not None else None
        if key != self.details_key:
            self.details_key = key
            self.details.setChecked(self.details_wanted or (has and needs_details(entry[1], self.arguments)))
        self.details_box.setVisible(has and self.details.isChecked())

    def show_context(self):
        """Details of the selected question, looked up once per question in a thread (a name lookup may
        take seconds); another host's log is not read from here."""
        entry = self.current()
        if entry and entry[2] != LOCAL:
            self.set_context(tr("Asked on {host}: the answer goes there, over SSH.", host=printable(entry[2])), [])
            return
        if not entry or entry[1].get("kind") not in ("dns", "connect"):
            self.set_context("", [])
            return
        run, question, host = entry
        key = (host, run, question.get("id"))
        if key in self.contexts:
            self.set_context(*self.contexts[key])
            return
        self.contexts[key] = (tr("Reading what the run's log knows about it…"), [])
        self.set_context(*self.contexts[key])
        background(self.sai.question_context, run, self.records.get(key, {}), question, timeout=30,
                   done=lambda result, error: self.context_ready(key, result, error))

    def context_ready(self, key, result, error):
        if key not in self.questions:
            return
        self.contexts[key] = context_text(result) if error is None else \
            (tr("No details: {error}", error=printable_text(error)), [])
        entry = self.current()
        if entry and (entry[2], entry[0], entry[1].get("id")) == key:
            self.set_context(*self.contexts[key])

    def set_context(self, text, log):
        self.context.setText(text)
        self.context_log.setPlainText("\n".join(log))
        self.context_log_label.setVisible(bool(log))
        self.context_log.setVisible(bool(log))
        self.show_details()

    # -- the history of another host --

    def load_history(self):
        """The history of the host chosen: this machine's in the table (from the scans); a host's as `sandbox-ai
        approvals history` there says it, read in the background."""
        self.history_token += 1
        token, host = self.history_token, self.history_host.currentData() or LOCAL
        self.history.setVisible(host == LOCAL)
        self.history_text.setVisible(host != LOCAL)
        if host == LOCAL:
            return
        self.history_text.setPlainText(tr("Asking {host}…", host=printable(host)))
        background(self.sai.host_approvals_history, host, timeout=90,
                   done=lambda text, error: self.history_ready(token, host, text, error))

    def history_ready(self, token, host, text, error):
        if token != self.history_token:
            return
        if error is not None:
            self.history_text.setPlainText(tr("{host}: no answer", host=printable(host)) + f" ({printable_text(error)})")
            return
        self.history_text.setPlainText(text or tr("No answers on {host} yet.", host=printable(host)))

    # -- allowed always --

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        self.update_key_filter()
        if not self.always_loaded:
            self.load_always()
        if getattr(self, "context_stale", False):  # the sandbox shown changed while the page was hidden
            self.context_changed()

    def hideEvent(self, event):  # noqa: N802
        super().hideEvent(event)
        QTimer.singleShot(0, self.update_key_filter)

    def load_always(self, note=None):
        """What was allowed "always", per project (read from sandbox-ai's file in a thread); NOTE is shown
        when it is there (e.g. "Forgotten.")."""
        self.always_loaded = True
        self.always_token += 1
        token = self.always_token
        host = self.always_host.currentData() or LOCAL
        if host == LOCAL:
            background(self.sai.remembered, timeout=30,
                       done=lambda rows, error: self.always_ready(token, rows, error, note, host))
        else:  # `sandbox-ai approvals remembered --json` there, over SSH
            self.always_message.setText(tr("Asking {host}…", host=printable(host)))
            background(lambda: self.sai.host_remembered(host), timeout=90,
                       done=lambda rows, error: self.always_ready(token, rows, error, note, host))

    def always_ready(self, token, rows, error, note=None, host=LOCAL):
        if token != self.always_token:
            return
        self.always_shown = host
        if isinstance(error, AttributeError):
            self.always_message.setText(tr("This sandbox-ai version cannot list them yet: update it."))
            rows = []
        elif error is not None:
            self.always_message.setText(tr("Cannot read them: {error}", error=printable_text(error)))
            rows = []
        else:
            self.always_message.setText(note or ("" if rows else tr("Nothing is allowed always.")))
        kinds = {"host": tr("allowed: host name"), "tcp": tr("allowed: address and port"),
                 "blocked-host": tr("blocked: host name"), "blocked-tcp": tr("blocked: address and port")}
        fill(self.always, [[printable(project), printable(entry), kinds.get(kind, printable(kind))]
                           for project, entry, kind in rows], [("bad" if kind.startswith("blocked") else None)
                                                             for _, _, kind in rows],
             keys=[(project, entry) for project, entry, _ in rows])

    def forget(self):
        rows = selected_rows(self.always)
        if len(rows) > 1:
            self.forget_several([row_key(self.always, row) for row in rows])
            return
        key = row_key(self.always, rows[0]) if rows else None
        if not key:
            self.always_message.setText(tr("Select an entry first."))
            return
        project, entry = key
        host = getattr(self, "always_shown", LOCAL)  # the host whose list is shown, never the one chosen meanwhile
        text = tr("Forget {entry} for {project}? The project's next sandboxes ask again. Sandboxes running now keep "
                  "what they already allowed until they end (take it back there with Sandboxes › Revoke…, sandbox-ai "
                  "net revoke).", entry=printable(entry), project=printable(project))
        if host != LOCAL:
            text = tr("On {host}: {text}", host=printable(host), text=text)
        if not ask_first(self, "approval-forget", tr("Forget"), text, tr("Forget")):
            return
        if host == LOCAL:
            background(self.sai.forget_remembered, project, entry, owner=self, source=self.forget_button, timeout=90,
                       work=True, done=self.forgotten)
        else:
            background(lambda: self.sai.forget_remembered(project, entry, host=host), owner=self, work=True,
                       source=self.forget_button, timeout=120, done=self.forgotten)

    def forget_several(self, keys):
        """Forget… with several entries selected: one question naming them, then each (approvals forget)."""
        keys = [key for key in keys if key]
        host = getattr(self, "always_shown", LOCAL)  # the host whose list is shown, never the one chosen meanwhile
        text = widgets.several(tr(
            "Forget these {count} entries? Their projects' next sandboxes ask again. Sandboxes running now keep what "
            "they already allowed until they end (take it back there with Sandboxes › Revoke…, sandbox-ai net revoke).",
            count=len(keys)), [f"{printable(entry)} ({printable(project)})" for project, entry in keys])
        if host != LOCAL:
            text = tr("On {host}: {text}", host=printable(host), text=text)
        if not keys or not ask_first(self, "approval-forget", tr("Forget"), text, tr("Forget")):
            return

        def forget(key):
            project, entry = key
            return self.sai.forget_remembered(project, entry) if host == LOCAL else \
                self.sai.forget_remembered(project, entry, host=host)

        def done(outcomes):
            failures = widgets.report_each(self, tr("Forget"), outcomes,
                                           lambda key: f"{printable(key[1])} ({printable(key[0])})")
            self.load_always(note=tr("Forgotten: {count} of {total}.", count=len(keys) - len(failures),
                                     total=len(keys)))
        widgets.run_each(forget, keys, owner=self, source=self.forget_button, timeout=120 * len(keys), done=done)

    def forgotten(self, result, error):
        status, out, err = outcome(result, error)
        self.load_always(note=printable_text((out + err).strip()) if status != 0 else tr("Forgotten."))

    def act(self, action):
        """A button (or its key): ACTION (allow - once -, allow-for-run, allow-always, deny, deny-once, block,
        ask-again) on every row selected it fits, at once - an open question (also one that timed out) is answered,
        an answered one changed (sandbox-ai approvals allow|deny|ask-again ID), what was refused without a question
        allowed as its entry offers (Allowed now's net allow, an exception rule). Undo takes the answers back for a
        few seconds."""
        chosen = [entry for entry in self.chosen() if self.fits(entry, action) is None]
        if not chosen:
            entry = self.current()
            self.message.setText(self.fits(entry, action) if entry else tr("Select a question first."))
            return
        self.undoable, self.undo_batch = [], getattr(self, "undo_batch", 0) + 1
        self.undo_button.hide()
        scope = SCOPE_OF.get(action)  # an Allow button's scope
        blocked = []
        for entry in chosen:
            question = entry[1]
            if isinstance(question, BlockedRow):
                blocked.append(question["blocked"])
            elif backend.answerable(question):
                if scope is not None:
                    self.answer("allow", scope, entry=entry)
                elif action == "block":
                    self.answer("deny", "always", entry=entry)
                else:
                    self.answer("deny", "once" if action == "deny-once" else "run", entry=entry)
            else:
                self.change(entry, "allow" if scope is not None else action, scope)
        if blocked:
            wanted = {"allow-for-run": "allow-run", "allow-always": "allow-always", "deny": "deny-run",
                      "ask-again": "ask-again"}[action]
            self.blocked.act(wanted, chosen=blocked)

    def change(self, entry, action, scope=None, undoing=False):
        """Change what the run decided about ENTRY, a question answered already: allow (for the run, or always where
        that is kept: SCOPE), deny (for the run) or ask-again - through `sandbox-ai approvals` there, in the
        background."""
        run, question, host = entry
        key, ident = self.key_of(entry), question.get("id", "")
        if key in self.answers or not self.sai.question_id.fullmatch(str(ident)):
            return
        scope = None if action != "allow" or scope in (None, "once") else scope
        applog.log("INFO", "approvals", f"{'undo: ' if undoing else ''}{action}"
                   f"{f' (scope {scope})' if scope else ''} on the answered question {printable(ident)[:60]} "
                   f"({question.get('kind', '?')}) of {printable(run)} on {printable(host)}")
        done = lambda result, error: self.answered(result, error, action, scope, host, None, key, entry=entry,  # noqa
                                                  undoing=undoing, batch=self.undo_batch)
        if host == LOCAL:
            self.answers[key] = background(self.sai.answer, ident, action, scope, timeout=ANSWER_WAIT, work=True,
                                           done=done)
        else:
            argv = ["approvals", action, ident, *(["--scope", scope] if scope else [])]
            self.answers[key] = background(lambda: self.sai.host_answer(host, argv, timeout=REMOTE_ANSWER_WAIT - 30),
                                           timeout=REMOTE_ANSWER_WAIT, done=done)
        self.message.setText(tr("Sending the answer…"))
        self.update_buttons()

    def undo(self):
        """Take back the answers just given (Undo): a question answered from open is asked again (the run forgets
        the answer: a connection allowed for the run is closed again), a changed answer is changed back. An "always"
        stays remembered for the project (Remembered: Forget lifts it); what was allowed as a refusal's entry is
        undone on Allowed now."""
        items, self.undoable = list(self.undoable), []
        self.undo_timer.stop()
        self.undo_button.hide()
        self.undo_batch = getattr(self, "undo_batch", 0) + 1
        for entry, action, scope, was_open in items:
            question = entry[1]
            if was_open:
                back, back_scope = "ask-again", None
            elif action == "ask-again":
                result = question.get("result") if isinstance(question.get("result"), dict) else {}
                back, back_scope = ("allow" if result.get("decision") == "allow" else "deny"), "run"
            else:
                back, back_scope = ("deny" if action == "allow" else "allow"), "run"
            self.answers.pop(self.key_of(entry), None)
            self.change(entry, back, back_scope, undoing=True)
        self.undo_note = tr("Undone for the run; “always” stays remembered for the project until you forget it "
                            "under Remembered.") if any(scope == "always" for _, _, scope, _ in items) else tr("Undone.")
        self.message.setText(tr("Undoing…"))

    def answer(self, decision, scope=None, entry=None, parent=None, open_here=False, asked=False):
        """Send the answer in the background (`sandbox-ai approvals` may take up to a minute; on another host through
        `sandbox-ai hosts run HOST --`), one per question: a slow host's answer holds up no other question. It goes
        at once, as clicked - no question first, also not for "always" (Undo beside the note takes it back): Allow
        takes the SCOPE of its button (default once; one the caps forbid is not sent: said why); Deny lasts for the
        run (as the web interface's and the command line's), SCOPE "once" (Deny once) this time only; Block always
        is a deny with the scope "always".
        A question that timed out is still answered: its answer counts for the request's next try. ENTRY: the (run,
        question, host) to answer (default: the one highlighted); PARENT: the window asking; OPEN_HERE (a browser
        question of another host, always so for Allow): allowed there with --no-open, the page opened here once that
        host took the answer. ASKED: given from the question's notification."""
        entry = entry or self.current()
        if not entry or self.key_of(entry) in self.answers or self.key_of(entry) in self.answered_there:
            return
        run, question, host = entry
        key = self.key_of(entry)
        if decision == "deny":
            refused = self.answer_rules(entry)["deny"]  # (its time is up where its sandbox-ai keeps nothing open)
            if refused:
                self.message.setText(refused)
                return
            # A deny is for the run unless "once" was chosen (the Deny once button); "always" is Block always. The
            # policy caps allows only: a deny is never narrowed.
            scope = scope if scope in backend.SCOPES else "run"
        else:
            refused = self.answer_rules(entry)["allow"] if self.key_of(entry) not in self.answers else None
            if refused:
                self.message.setText(refused)
                return
            scope = scope if scope in backend.SCOPES else "once"
            refused = self.scope_refused(entry, scope)
            if refused:
                self.message.setText(refused)
                return
            # Another host's page is opened here (that host may have no screen): allowed there with --no-open.
            open_here = open_here or remote_browser(entry)
        url = openable_url(question.get("url")) if open_here and question.get("kind") == "browser" else None
        if open_here and url is None:
            self.message.setText(tr("Only http and https pages are opened here."))
            return
        # Answered meanwhile (in the other window): not sent.
        if key in self.answers or key in self.answered_there or key not in self.questions:
            return
        callback = question.get("callback") if isinstance(question.get("callback"), dict) else None
        if decision == "allow" and callback and (url is not None or host == LOCAL):
            # No question first (the answer goes as clicked): the note says what happens to the login's callback.
            self.message.setText(tr("Its login sends the browser back to {url} in the sandbox: sandbox-ai relays that "
                                    "one request into the sandbox (port {port} on this computer, 10 minutes at most). "
                                    "If that port is busy here, use the agent's device-code login instead.",
                                    url=printable(str(callback.get("url", "")))[:200],
                                    port=printable(str(callback.get("port", "")))[:8]))
        else:
            self.message.setText(tr("Sending the answer…"))
        ident = question.get("id", "")
        applog.log("INFO", "approvals", f"answer {decision} (scope {scope}) to question {printable(ident)[:60]} "
                                        f"({question.get('kind', '?')}) of {printable(run)} on {printable(host)}"
                                        + (" - the page is opened here" if url is not None else ""))
        batch = getattr(self, "undo_batch", 0)
        done = lambda result, error: self.answered(result, error, decision, scope, host, url, key,  # noqa: E731
                                                  entry=entry, batch=batch, was_open=True)
        if host == LOCAL:
            # sandbox-ai answers within 60 s; a call that hangs past that (e.g. on a stuck pipe) frees the buttons.
            self.answers[key] = background(self.sai.answer, ident, decision, scope, timeout=ANSWER_WAIT, work=True,
                                           done=done)
        elif not self.sai.question_id.fullmatch(str(ident)):
            self.message.setText(tr("Not a question of sandbox-ai: {ident}", ident=printable(ident)[:60]))
            return
        else:
            argv = ["approvals", decision, ident, "--scope", scope] + (["--no-open"] if url is not None else [])
            if url is not None and decision == "allow" and isinstance(question.get("callback"), dict):
                # A login's callback to the sandbox's loopback: this machine's catcher relays it (oauth_relay.py).
                self.answers[key] = background(
                    lambda: self.sai.host_answer_relaying(host, argv, ident, question.get("url"),
                                                          timeout=REMOTE_ANSWER_WAIT - 30),
                    timeout=REMOTE_ANSWER_WAIT, done=done)
            else:
                self.answers[key] = background(lambda: self.sai.host_answer(host, argv,
                                                                            timeout=REMOTE_ANSWER_WAIT - 30),
                                               timeout=REMOTE_ANSWER_WAIT, done=done)
        self.update_buttons()
        self.offer_questions()  # the Questions window lists it no more

    def scope_refused(self, entry, scope):
        """Why ENTRY cannot be allowed with SCOPE (once, run, always), or None - its button's tooltip while disabled,
        the note when it is asked for all the same (a key, a notification). As sandbox-ai caps it
        (approvals.widest_answer, past_cap): a canary's alarm and a page to open once; pushes, requests, tool calls
        and devices at most for the run; the administrator's policy and the run's security mode; another host's
        question as that host said (its max_scope). Whether it may be allowed at all is answer_rules'."""
        if scope == "once" or scope not in backend.SCOPES:
            return None
        run, question, host = entry
        kind = question.get("kind")
        if kind == "canary":
            return tr("A canary's alarm is answered this once: Allow once lets the sandbox go on, Deny stops it.")
        if kind in ONCE_ONLY or backend.question_url(question):
            return tr("A page is opened once: nothing is kept for it.")
        if malicious_package(question):
            return tr("A known malicious package version is allowed for this one download at most: never for the run "
                      "or for later sessions.")
        widest = self.widest(entry) if backend.answerable(question) else self.past_widest(entry)
        if widest is not None and backend.SCOPES.index(scope) <= backend.SCOPES.index(widest):
            return None
        words = {"once": tr("once"), "run": tr("for this run")}
        if host != LOCAL:
            return tr("{host} allows it at most {scope} (its policy, the run's security mode or the kind of "
                      "question).", host=printable(host), scope=words.get(widest, tr("once")))
        record = self.records.get(self.key_of(entry)) or {}
        mode = record.get("mode") if isinstance(record.get("mode"), dict) else {}
        policy = self.max_scope if self.max_scope in backend.SCOPES else "always"
        if kind in RUN_AT_MOST and widest == "run" and policy == "always" and \
                mode.get("approval_scope") in (None, "always"):
            if package_of(question) and package_of(question).get("allow"):
                return tr("A download is allowed at most for this run. To allow the package in every run of the "
                          "project: {command}", command=shown(package_of(question)["allow"], 300))
            if secret_of(question) and secret_of(question).get("allow"):
                return tr("A request with a secret is allowed at most for this run (the same kind of value to the "
                          "same host is then not asked about again). For every run of the project: {command}",
                          command=shown(secret_of(question)["allow"], 600))
            return tr("Pushes, requests, tool calls and devices are allowed at most for this run: none of them is "
                      "kept for later sessions.")
        if mode.get("approval_scope") in backend.SCOPES and \
                backend.SCOPES.index(mode["approval_scope"]) <= backend.SCOPES.index(policy):
            return tr("Allowed at most {scope} here: the run's security mode.",
                      scope=words.get(mode["approval_scope"], tr("once")))
        return tr("Allowed at most {scope} here: the administrator's policy (max_approval_scope).",
                  scope=words.get(widest, tr("once")))

    def past_widest(self, entry):
        """The widest scope an answered ENTRY may be allowed again with (sandbox-ai's approvals.past_cap: another
        host's as it said, max_scope), "run" where that is not known (an older sandbox-ai's own default)."""
        run, question, host = entry
        if host != LOCAL:
            scope = question.get("max_scope")
            return scope if scope in backend.SCOPES else "run"
        try:
            module = self.sai.module("approvals")
            if hasattr(module, "past_cap"):
                return module.past_cap({**question, "state": entry_state(question)},
                                       self.records.get(self.key_of(entry)) or {}, self.max_scope)
        except Exception as exc:  # noqa: BLE001 - the run's own default, said in the log
            applog.swallowed("pages", "ApprovalsPage.past_widest", exc)
        return "run"

    def settled(self, key):
        """True when the question KEY needs no answer from here any more: it is not open (answered elsewhere, ended,
        refused), or its answer is on its way, or another host took it and lists it until it has read it."""
        if key not in self.questions or key in self.answers or key in self.answered_there:
            return True
        return entry_state(self.questions[key][1]) != "open"

    def open_here(self):
        """A browser question of another host: allowed there without opening it (--no-open), opened here."""
        self.answer("allow", "once", open_here=True)

    def answered(self, result, error, decision="deny", scope="once", host=LOCAL, url=None, key=None, entry=None,
                 undoing=False, batch=None, was_open=False):
        self.answers.pop(key, None)
        if key is not None and len(self.answered_recently) < 1000:
            self.answered_recently.add(key)
        status, out, err = outcome(result, error)
        text = printable_text((out + err).strip())
        applog.log("INFO" if status == 0 else "WARNING", "approvals", f"answer {decision} (scope {scope}) on "
                   f"{printable(host)}: exit status {status}" + (f"\n{applog.tail(out + err)}" if status != 0 else ""))
        if status != 0:
            # No box with OK to click: a short notice in the error colour that goes by itself, and the same in the
            # note line below the table (to read it again; the log has it too).
            why = text or tr("sandbox-ai refused the answer.")
            self.message.setText(tr("Not answered: {why}", why=why))
            from .short_notices import short_notice
            short_notice(self, tr("Not answered"), why, error=True)
        elif decision == "allow" and not undoing and closed_note(out):
            # Allowed, but its name does not exist: sandbox-ai closed the question (denied for the run, nothing
            # opened) - said in one line and a notice that goes by itself, no error and nothing to undo.
            said = tr("Closed, not allowed: {why}", why=printable_text(closed_note(out))[:600])
            self.message.setText(said)
            from .short_notices import short_notice
            short_notice(self, tr("Question closed"), said)
        else:
            # (only what was done, in a few words: the table shows the question and its answer - the whole question
            # once more below it, a login's long address and all, said nothing new; the log keeps sandbox-ai's text)
            self.message.setText(getattr(self, "undo_note", tr("Undone.")) if undoing else answered_note(decision, scope))
            if url is not None and decision == "allow":
                QDesktopServices.openUrl(url)
            if host != LOCAL and key is not None:
                self.answered_there[key] = time.monotonic()  # until a read begun from now on no longer lists it
            if entry is not None and not undoing and batch == getattr(self, "undo_batch", 0) and \
                    entry[1].get("kind") != "browser":
                self.undoable.append((entry, decision, scope, was_open))
                self.undo_button.show()
                self.undo_timer.start(UNDO_MS)
        if host == LOCAL:
            self.poll()
            if status == 0 and scope == "always" and self.always_host.currentData() == LOCAL:
                self.load_always()  # it is listed there now
        else:
            self.poll_remote([host])  # read again now, also when a read of it runs already
            self.update_buttons()
            if status == 0 and scope == "always" and self.always_host.currentData() == host:
                self.load_always()


# How sandbox-ai's answer begins the line that says it closed a question instead of allowing it (the name does not
# exist): approvals.CLOSED_NOTE, the same on every host's sandbox-ai (a test keeps them equal).
CLOSED_NOTE = "denied for the run: "


def closed_note(output):
    """What sandbox-ai's OUTPUT of an allow says after CLOSED_NOTE (approvals.closed_note), or None."""
    for line in str(output or "").splitlines():
        if line.strip().startswith(CLOSED_NOTE):
            return line.strip()[len(CLOSED_NOTE):]
    return None


def answered_note(decision, scope):
    """The note line after an answer, naming its scope: "Allowed once." / "Always allowed in this run (this session)." /
    "Always allowed, also for later sessions." / "Blocked always." - the question itself is in the table."""
    if decision == "allow":
        return {"once": tr("Allowed once."), "run": tr("Always allowed in this run (this session)."),
                "always": tr("Always allowed, also for later sessions.")}.get(
            scope, tr("Allowed ({scope}).", scope=printable(str(scope or ""))))
    if decision == "deny":
        return {"once": tr("Refused once."), "run": tr("Refused for this run."), "always": tr("Blocked always.")}.get(
            scope, tr("Refused ({scope}).", scope=printable(str(scope or ""))))
    return tr("Done.")


class StatisticsPage(QWidget):
    """What the logs of a project say: model calls and tokens, hosts, credentials, pushes, refusals,
    processes, MCP tool calls. On a host of `sandbox-ai hosts`: `sandbox-ai logs DIR --json` (or --project PATH)
    there, over SSH, for a folder of that host typed in."""

    def __init__(self, sai):
        super().__init__()
        self.sai = sai
        layout = QVBoxLayout(self)
        from .host_choice import HostChooser
        self.hosts = HostChooser(sai, tr("The logs of this machine, or of a host (over SSH)"))
        self.hosts.changed.connect(lambda _host: self.host_changed())
        layout.addWidget(self.hosts)
        top = QHBoxLayout()
        self.directory = QComboBox()
        self.directory.setAccessibleName(tr("Logs"))
        compact_combo(self.directory, 30)  # long log folders: elided here, whole in the list
        self.directory.currentIndexChanged.connect(self.load)
        browse = QPushButton(tr("Other folder…"))
        browse.clicked.connect(self.browse)
        refresh = QPushButton(tr("Refresh"))
        refresh.clicked.connect(lambda: self.refresh_directories())
        # A report of every run of the log folder shown: statistics, network and the security report (sandbox-ai
        # report --log-dir DIR), as PDF, HTML or Markdown.
        self.report_button = QPushButton(tr("Report…"))
        self.report_button.setAccessibleName(tr("Report"))
        self.report_button.setToolTip(tr("One report of every run of this log folder: statistics, every destination "
                                         "and why it was allowed or refused, and a security report of what was not "
                                         "allowed - as PDF, HTML or Markdown (sandbox-ai report --log-dir)"))
        self.report_button.clicked.connect(lambda _=False: self.report())
        from PySide6.QtWidgets import QCheckBox
        self.project_folder = QCheckBox(tr("a project folder (its default log folder)"))
        self.project_folder.setToolTip(tr("sandbox-ai logs --project PATH: the log folder the wizard gives that "
                                          "project"))
        self.project_folder.setVisible(False)
        self.project_folder.toggled.connect(lambda _=False: self.load())
        for widget in (QLabel(tr("Logs")), self.directory, browse, refresh, self.report_button, self.project_folder):
            top.addWidget(widget)
        top.addStretch()
        layout.addLayout(top)
        self.summary = plain_label()
        layout.addWidget(self.summary)
        grid = QGridLayout()
        holder = QWidget()
        holder.setLayout(grid)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(holder)
        layout.addWidget(scroll, 1)
        self.tables = {}
        sections = (
            ("models", tr("Model calls"), [tr("Model"), tr("Calls"), ""]),
            ("tokens", tr("Tokens"), [tr("Kind"), tr("Tokens"), ""]),
            ("hosts", tr("HTTP requests per host"), [tr("Host"), tr("Requests"), ""]),
            ("credentials", tr("Credentials sent (header names only)"), [tr("Header"), tr("Host"), tr("Times")]),
            ("refused", tr("Refused connections"), [tr("What"), tr("Times")]),
            ("programs", tr("Programs started"), [tr("Program"), tr("Times"), ""]),
            ("tools", tr("MCP tool calls (all servers)"), [tr("Server"), tr("Tool"), tr("Calls"), tr("Refused")]),
            ("spend", tr("Model spend (sandbox-ai spend)"), [tr("Model"), tr("Cost"), tr("Requests"), ""]),
        )
        for index, (key, title, headers) in enumerate(sections):
            box = QGroupBox(title)
            box_layout = QVBoxLayout(box)
            # The bars take the rest of the width; a table without bars: its first text column.
            stretch = len(headers) - 1 if not headers[-1] else 1 if len(headers) > 2 else 0
            widget = table(headers, stretch=stretch, compact=[c for c in range(1, len(headers)) if c != stretch],
                           multi=True)  # several rows (Ctrl, Shift): to copy them
            widget.setAccessibleName(title)
            widget.setMinimumHeight(160)
            box_layout.addWidget(widget)
            grid.addWidget(box, index // 2, index % 2)
            self.tables[key] = widget
        self.data, self.generation = None, 0
        self.refresh_directories()

    def host(self):
        return self.hosts.host() or LOCAL

    def host_changed(self):
        """This machine: its log folders listed; another host: a folder of that host typed in (Enter reads it)."""
        remote = self.host() != LOCAL
        self.directory.blockSignals(True)
        self.directory.clear()
        self.directory.setEditable(remote)
        if remote:
            self.directory.lineEdit().setPlaceholderText(tr("a log folder or project folder on {host}",
                                                            host=self.hosts.label()))
            edit = self.directory.lineEdit()
            if edit is not getattr(self, "typed_folder", None):  # a new line edit each time it becomes editable
                edit.returnPressed.connect(self.load)
                self.typed_folder = edit
        self.directory.blockSignals(False)
        self.project_folder.setVisible(remote)
        if remote:
            self.generation += 1
            self.data = None
            self.summary.setText(tr("Type a folder on {host} and press Enter.", host=self.hosts.label()))
            for widget in self.tables.values():
                fill(widget, [])
            self.update_report_button()
        else:
            self.refresh_directories()

    def refresh_directories(self, select=None):
        if self.host() != LOCAL:
            if select:
                self.directory.setEditText(str(select))
            self.load()
            return
        current = select or self.directory.currentData()
        self.directory.blockSignals(True)
        self.directory.clear()
        for path in self.sai.log_directories():
            self.directory.addItem(path.name, str(path))
        if current and self.directory.findData(str(current)) < 0:
            self.directory.addItem(Path(current).name, str(current))
        if current:
            self.directory.setCurrentIndex(self.directory.findData(str(current)))
        self.directory.blockSignals(False)
        self.load()

    def browse(self):
        if self.host() != LOCAL:  # a folder of that host, chosen from its folders (remote_chooser)
            from . import remote_chooser as RC
            path = RC.choose_folder(self, self.host(), tr("Log folder"), "logs", sai=self.sai)
            if path:
                self.refresh_directories(select=path)
            return
        path = choose_folder(self, tr("Log folder"), "logs")
        if path:
            self.refresh_directories(select=path)

    def set_project(self, path):
        """The logs of the project PATH (its default log folder, as the launcher names it)."""
        try:
            folder = self.sai.launcher.default_log_dir(Path(path))
        except Exception as _exc:  # noqa: BLE001 - an older launcher: the choice stays
            applog.swallowed("pages", "StatisticsPage.set_project: an older launcher: the choice stays", _exc)
            return
        self.refresh_directories(select=str(folder))

    def report_folder(self):
        """(log folder, why none): the folder of this machine whose runs Report… covers."""
        if self.host() != LOCAL:
            return None, tr("Only for this machine's logs: on {host}, sandbox-ai report there writes it.",
                            host=self.hosts.label())
        directory = self.directory.currentData()
        if not directory or not os.path.isdir(directory):
            return None, tr("No logs yet: they appear once a sandbox has run with its connection proxy.")
        return directory, None

    def update_report_button(self):
        directory, why = self.report_folder()
        set_enabled(self.report_button, directory is not None, why)

    def report(self):
        directory, why = self.report_folder()
        if directory is None:
            warn(self, tr("Report"), why, modal=False)
            return None
        from .reports import make_report
        return make_report(self, self.sai, Path(directory).name,
                           tr("A report of every run that logged into {path}.", path=printable(directory)),
                           log_dir=directory, parts=False, owner=self, source=[self.report_button])

    def load(self):
        self.update_report_button()
        if self.host() != LOCAL:
            self.load_remote()
            return
        directory = self.directory.currentData()
        if not directory or not os.path.isdir(directory):
            self.generation += 1
            self.data = None
            self.summary.setText(tr("No logs yet: they appear once a sandbox has run with its connection proxy."))
            for widget in self.tables.values():
                fill(widget, [])
            return
        # Reading big logs takes a while: in a thread; only the newest request's result is shown.
        self.generation += 1
        token = self.generation
        self.summary.setText(tr("Reading the logs…"))
        background(self.sai.statistics, directory, owner=self, timeout=300,
                   done=lambda data, error: self.loaded(token, data, error))

    def load_remote(self):
        """`sandbox-ai logs DIR --json` (or --project DIR) on the host chosen, in the background."""
        host, folder = self.host(), self.directory.currentText().strip()
        self.generation += 1
        token = self.generation
        if not backend.valid_directory(folder):
            if folder:
                self.summary.setText(tr("Not an absolute path: {path}", path=printable(folder)))
            return
        project = self.project_folder.isChecked()
        self.summary.setText(tr("Asking {host}…", host=self.hosts.label(host)))
        background(lambda: self.sai.host_statistics(host, None if project else folder, folder if project else None),
                   owner=self, timeout=330, done=lambda data, error: self.loaded(token, data, error))

    def loaded(self, token, data, error):
        if token != self.generation:
            return
        if error is not None:
            self.summary.setText(tr("Cannot read the logs: {error}", error=printable_text(error)))
            return
        self.show_data(data)

    def show_data(self, data):
        self.data = data
        pushes = data.get("pushes", {})
        text = tr("{requests} HTTP requests, {processes} processes; pushes: {ok} checked, {refused} refused",
                  requests=data.get("requests", 0), processes=data.get("processes", 0), ok=pushes.get("ok", 0),
                  refused=pushes.get("refused", 0))
        if data.get("older_lines_skipped"):
            text += " " + tr("(only the newest entries: about {count} older lines not read)", count=data["older_lines_skipped"])
        self.summary.setText(text)

        def counted(key, pairs):
            rows = [[printable(name), count, ""] for name, count in pairs]
            fill(self.tables[key], rows)
            bars(self.tables[key], 2, [count for _, count in pairs])
        counted("models", sorted(data.get("model_calls", {}).items(), key=lambda p: -p[1]))
        counted("tokens", sorted(data.get("tokens", {}).items()))
        counted("hosts", data.get("hosts", []))
        counted("programs", data.get("programs", []))
        fill(self.tables["credentials"], [[*backend.split_credential(entry), count]
                                          for entry, count in data.get("credentials", [])])
        fill(self.tables["refused"], [[printable(entry), count] for entry, count in data.get("refused", [])],
             ["bad"] * len(data.get("refused", [])))
        tools, colours = [], []
        for server, calls in sorted((data.get("tool_calls") or {}).items()):
            counts, refused = {}, {}
            for call in calls:  # (time, tool) from older sandbox-ai, (time, tool, decision) since the MCP gateway
                tool = str(call[1]) if len(call) > 1 else "?"
                counts[tool] = counts.get(tool, 0) + 1
                if len(call) > 2 and call[2] not in (None, "allow"):
                    refused[tool] = refused.get(tool, 0) + 1
            for tool, count in sorted(counts.items(), key=lambda p: (-p[1], p[0])):
                tools.append([printable(server), printable(tool), count, refused.get(tool, 0)])
                colours.append("bad" if refused.get(tool) else None)
        fill(self.tables["tools"], tools, colours)
        spend = data.get("spend") if isinstance(data.get("spend"), dict) else {}
        models = [row for row in spend.get("models") or [] if isinstance(row, dict)]
        fill(self.tables["spend"], [[printable(row.get("key") or "?"), printable(row.get("cost") or "-"),
                                     row.get("requests") or 0, ""] for row in models])
        bars(self.tables["spend"], 3, [float(row.get("usd") or 0) for row in models])
        if spend.get("total"):
            self.summary.setText(self.summary.text() + " " + tr("Model spend: {cost}.", cost=printable(
                spend["total"].get("cost") or "-")))


from .credentials_page import CredentialsPage  # noqa: E402,F401 - the Credentials page lives in its own module
from .images_page import ImagesPage  # noqa: E402,F401 - the Images page lives in its own module
