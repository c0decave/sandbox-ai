"""What trusting a project file (.sandbox-ai.json) means, shown before anything is trusted. No Qt.

A trusted file gives every later start of its project its settings, so the question names every key that will be
trusted, marks what weakens the sandbox (a laxer security mode, canaries off, a kept HOME, debug logs, widening
options, the project's own ruleset and MCP servers), and shows the changes against the version trusted LAST. Never
against the file in the folder: whoever can write the folder (the agent too) wrote that one, so a diff against it
shows nothing of what that writer put there. The app keeps a copy of every version it saw trusted (backend:
keep_trusted_copy); without one (trusted elsewhere, or nothing trusted yet) the whole file is shown as new.

A project file on another host is reviewed the same way through that host's sandbox-ai (`wizard trust --show --json`,
then `wizard trust --digest` of exactly what was shown): its document as read there, measured against that host's
defaults, diffed against the version the app saw trusted last (backend.shown_baseline): host_question.
"""
from __future__ import annotations

import difflib
import json
import os
from pathlib import Path

from . import backend
from .files import printable
from .i18n import tr

WARN = "⚠"  # marks a line that weakens the sandbox (the question is plain text: no colour there)
ORDER = ("agent", "options", "workspaces", "agent_args", "mcp_servers", "rules", "mode", "canaries", "keep",
         "log_debug", "name", "description")
CANARY_DEFAULT = ("credentials",)


def user_defaults(sai):
    """The human's defaults.json ({} when there is none or it cannot be read): the mode and canaries a project
    file is measured against."""
    try:
        path = sai.launcher.defaults_file()
        document = json.loads(backend.read_bounded(path, 1 << 16).decode())
    except (OSError, ValueError, AttributeError, RecursionError, UnicodeDecodeError):
        return {}
    return document if isinstance(document, dict) else {}


def default_mode(sai, defaults=None):
    names = list(getattr(getattr(sai.launcher, "MODES", None), "NAMES", ())) or ["lax", "low", "medium", "high",
                                                                                 "paranoid"]
    chosen = (user_defaults(sai) if defaults is None else defaults).get("mode")
    return (chosen if chosen in names else "medium"), names


def weakening(sai, document, defaults=None):
    """{key: why} of the top-level keys of DOCUMENT that weaken the sandbox against the defaults (DEFAULTS: those of
    the host the file is on, {"mode", "canaries"}; None: this machine's); options are in option_warnings()."""
    defaults = user_defaults(sai) if defaults is None else defaults
    found = {}
    mode = document.get("mode")
    default, names = default_mode(sai, defaults)
    if isinstance(mode, str) and mode in names and names.index(mode) < names.index(default):
        found["mode"] = tr("a laxer security mode than your default ({mode})", mode=default)
    canaries = document.get("canaries")
    if "canaries" in document:
        wanted = defaults.get("canaries", list(CANARY_DEFAULT))
        wanted = set(wanted) if isinstance(wanted, list) else set()
        chosen = set(canaries) if isinstance(canaries, list) else set()
        if canaries is False or not chosen or not wanted <= chosen:
            found["canaries"] = tr("fewer canary tokens: a secret leaving is noticed less often")
    if document.get("keep") is True:
        found["keep"] = tr("the sandbox HOME is kept after the start: what the agent wrote there stays and runs again")
    if document.get("log_debug") is True:
        found["log_debug"] = tr("debug logs: the logs keep much more of what passes")
    if document.get("rules") is not None:
        found["rules"] = tr("the project's own ruleset decides requests (it may allow what is asked otherwise)")
    if document.get("mcp_servers"):
        found["mcp_servers"] = tr("the project defines its own MCP servers (programs with their own folders and "
                                  "network)")
    return found


def option_warnings(sai, options):
    """The options of OPTIONS that widen the sandbox (backend.WIDENING), as written."""
    found, index = [], 0
    options = list(options)
    while index < len(options):
        word = str(options[index])
        name = word.split("=", 1)[0]
        takes = "=" not in word and index + 1 < len(options) and not str(options[index + 1]).startswith("-")
        value = str(options[index + 1]) if takes else word.partition("=")[2]
        if name in backend.WIDENING:
            found.append(f"{name} {value}".strip())
        index += 2 if takes else 1
    return found


def short(value, limit=160):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = printable(text)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def summary(sai, document, defaults=None):
    """Lines naming every key of DOCUMENT (the known ones in a fixed order, any other after them), the weakening
    ones marked: what the question says will be trusted."""
    weak = weakening(sai, document, defaults)
    lines = []
    for key in [k for k in ORDER if k in document] + sorted(k for k in document if k not in ORDER and k != "version"):
        value = document[key]
        if key == "options":
            text = " ".join(printable(str(v)) for v in value) if isinstance(value, list) else short(value)
            lines.append(f"  options: {text or tr('(none)')}")
            for widening in option_warnings(sai, value if isinstance(value, list) else []):
                lines.append(f"    {WARN} {short(widening)}: " + tr("widens what the sandbox may do or see"))
            continue
        if key == "mcp_servers" and isinstance(value, dict):
            text = ", ".join(printable(str(name)) for name in sorted(value))
        else:
            text = short(value)
        line = f"  {printable(key)}: {text}"
        if key in weak:
            line = f"  {WARN} {printable(key)}: {text} - {weak[key]}"
        lines.append(line)
    return lines


def diff_details(sai, directory, new_text):
    """The details of the question: the changes against the version trusted last (or the note that there is none
    the app knows), then the whole file that will be trusted."""
    state, old = sai.trusted_baseline(directory)
    return baseline_details(state, old.decode("utf-8", "replace") if state == "kept" else None, new_text)


def baseline_details(state, old_text, new_text):
    """The details for a baseline (STATE "kept" with OLD_TEXT, "unknown" or "none"; see backend.trusted_baseline)
    and the NEW_TEXT that will be trusted."""
    parts = []
    if state == "kept":
        changes = "\n".join(difflib.unified_diff(old_text.splitlines(), new_text.splitlines(), tr("trusted before"),
                                                 tr("to be trusted"), lineterm=""))
        parts.append(tr("Changes against the version you trusted last:"))
        parts.append(printable(changes) if changes else tr("No change."))
    elif state == "unknown":
        parts.append(tr("A different version was trusted before, but this app has no copy of it (trusted elsewhere): "
                        "read the whole file below."))
    else:
        parts.append(tr("Nothing was trusted for this file before: all of it is new."))
    parts += ["", tr("The whole file that will be trusted:"), printable(new_text)]
    return "\n".join(parts)


def question(sai, directory, document, intro, new_text=None):
    """(text, details) of the question before DOCUMENT is written into DIRECTORY/.sandbox-ai.json and trusted (or,
    with NEW_TEXT, before those exact bytes are trusted): INTRO, every key, what weakens, and the details."""
    new_text = new_text if new_text is not None else json.dumps(document, indent=2) + "\n"
    return question_text(sai, document, intro), diff_details(sai, directory, new_text)


def question_text(sai, document, intro, defaults=None):
    lines = [intro, "", tr("What will be trusted:"), *summary(sai, document, defaults)]
    weak = weakening(sai, document, defaults)
    if weak or option_warnings(sai, document.get("options", []) if isinstance(document.get("options"), list) else []):
        lines += ["", tr("Lines marked {mark} weaken the sandbox: trust them only if you chose them.", mark=WARN)]
    return "\n".join(lines)


def host_question(sai, shown, intro, baseline, defaults):
    """(text, details) of the question before the project file SHOWN (backend.host_project_show) is trusted on its
    host: INTRO, every key of its document as that host's sandbox-ai read it, what weakens against that host's
    DEFAULTS, and the changes against BASELINE (backend.shown_baseline) with the whole document."""
    document = shown["document"]
    state, old_text = baseline
    return question_text(sai, document, intro, defaults), baseline_details(state, old_text,
                                                                          backend.shown_text(document))


def config_path(text):
    """The project file of TEXT (a folder or its .sandbox-ai.json, absolute): its path, or None."""
    text = (text or "").strip()
    if not backend.valid_directory(text):
        return None
    return text if text.endswith("/.sandbox-ai.json") else text.rstrip("/") + "/.sandbox-ai.json"

def unshown(document):
    """The keys of a project file that no part of the board shows ({key: value}): named above the board and in the
    question, never carried along silently."""
    return {k: v for k, v in document.items() if k in ("mode", "canaries", "keep", "log_debug", "name", "description")}


def path_text(directory):
    return str(Path(os.fspath(directory)) / ".sandbox-ai.json")
