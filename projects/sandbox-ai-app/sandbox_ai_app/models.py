"""The models an agent can be started with (--model), each with its version: for the wizard, the board's guide and
the inspector.

Where each list comes from:

- Claude Code: its aliases (fable, opus, sonnet, haiku: the launcher's AGENT_MODELS) with the model each stands for
  now, and the full model ids with their version (claude-opus-5-5: "Opus 5.5"), from the table below (Anthropic's
  model list; Claude Code itself has no command that lists them). An alias follows the installed Claude Code: a newer
  one may mean a newer model by it.
- Codex: the model list Codex fetched from OpenAI and keeps in models_cache.json in its home (~/.codex, $CODEX_HOME,
  and the kept HOMEs of sessions), with the display name Codex shows; nothing built in (OpenAI's list changes too often).
- OpenCode: its copy of the models.dev catalogue (~/.cache/opencode/models.json, also in kept HOMEs), with each
  model's name and release date, for the providers listed in OPENCODE_PROVIDERS, newest first (a server of
  sandbox-ai llm has its models chosen with --llm NAME:MODEL instead).

Everything read from a file is untrusted (a sandbox could write a kept HOME): size-limited, never through a link,
names checked like the launcher's --model, labels made printable. A model not listed can always be typed.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat

from .files import printable
from .i18n import tr

MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}")  # the launcher's MODEL_NAME
LIMIT = 8 << 20
# Claude Code's aliases and the model each stands for at this writing (Anthropic's current models).
CLAUDE_ALIASES = {"fable": "claude-fable-5-1", "opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5",
                  "haiku": "claude-haiku-5-5"}
# The Claude models (newest first in each family), as Anthropic lists them.
CLAUDE_MODELS = ("claude-fable-5-1", "claude-fable-5", "claude-opus-5-5", "claude-opus-5", "claude-opus-4-8",
                 "claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-5-5", "claude-sonnet-5", "claude-sonnet-4-6",
                 "claude-haiku-5-5", "claude-haiku-4-5")
OPENCODE_PROVIDERS = ("anthropic", "openai", "google", "mistral", "deepseek", "xai")
PER_PROVIDER = 12
CLAUDE_ID = re.compile(r"(?:[a-z0-9.-]+[./])?claude-([a-z]+)-(\d+)(?:-(\d{1,2}))?(?:-(\d{8}))?$")


def version_label(model):
    """A model id's family and version in words: claude-opus-5-5 -> "Opus 5.5", claude-haiku-4-5-20251001 ->
    "Haiku 4.5 (2025-10-01)", gpt-5.1-codex -> "GPT-5.1 Codex"; "" when the id says nothing more than itself."""
    model = str(model or "")
    match = CLAUDE_ID.fullmatch(model.split("/")[-1])
    if match:
        family, major, minor, date = match.groups()
        text = f"{family.capitalize()} {major}" + (f".{minor}" if minor else "")
        return text + (f" ({date[:4]}-{date[4:6]}-{date[6:]})" if date else "")
    name = model.split("/")[-1]
    gpt = re.fullmatch(r"gpt-(\d+(?:\.\d+)?)(?:-(.+))?", name)
    if gpt:
        rest = " ".join(part.capitalize() for part in (gpt.group(2) or "").split("-") if part)
        return f"GPT-{gpt.group(1)}" + (f" {rest}" if rest else "")
    o_series = re.fullmatch(r"(o\d+)(?:-(.+))?", name)
    if o_series:
        return o_series.group(1) + (f" {o_series.group(2)}" if o_series.group(2) else "")
    return ""


def label(model, name="", date=""):
    """What the list shows for MODEL: its version (or the NAME its source gives), the release DATE, and the id
    itself (what is passed as --model)."""
    words = printable(name).strip() if name and name != model else version_label(model)
    if date:
        words = f"{words} ({date})" if words and date not in words else words or date
    return f"{words} — {model}" if words else model


def read_json(path):
    """The JSON of a small regular file (no link, at most LIMIT bytes), or None."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > LIMIT:
            return None
        chunks, size = [], 0
        while size <= LIMIT:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        return json.loads(b"".join(chunks).decode("utf-8")) if size <= LIMIT else None
    except (OSError, ValueError, UnicodeDecodeError, RecursionError):
        return None
    finally:
        os.close(fd)


def homes(environ=None):
    """HOME and the kept HOMEs of sessions (where an agent run keeps its caches)."""
    environ = os.environ if environ is None else environ
    found = [Path.home()]
    state = Path(environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "sandbox-ai" / "sessions"
    try:
        found += sorted(p / "home" for p in state.iterdir() if (p / "home").is_dir())[:50]
    except OSError:
        pass
    return found


def codex_models(environ=None):
    """[(id, label)] of Codex's models_cache.json files (the list Codex fetched from OpenAI)."""
    environ = os.environ if environ is None else environ
    paths = ([Path(environ["CODEX_HOME"]) / "models_cache.json"] if environ.get("CODEX_HOME") else []) + \
        [home / ".codex" / "models_cache.json" for home in homes(environ)]
    found = {}
    for path in paths:
        document = read_json(path)
        entries = document.get("models") if isinstance(document, dict) else document
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            model = next((entry[k] for k in ("slug", "id", "model") if isinstance(entry.get(k), str)), None)
            if not model or not MODEL_NAME.fullmatch(model) or model in found:
                continue
            if entry.get("visibility") in ("hide", "hidden") or entry.get("hidden") is True:
                continue
            name = entry.get("display_name") or entry.get("name") or ""
            found[model] = label(model, name if isinstance(name, str) else "")
    return list(found.items())


def opencode_models(environ=None):
    """[(provider/model, label)] of OpenCode's models.dev catalogue, newest first per provider. (The models of a
    server of sandbox-ai llm are chosen with --llm NAME:MODEL, not --model: not listed here.)"""
    environ = os.environ if environ is None else environ
    found = {}
    caches = [Path(environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "opencode" / "models.json"] + \
        [home / ".cache" / "opencode" / "models.json" for home in homes(environ)[1:]]
    for path in caches:
        document = read_json(path)
        if not isinstance(document, dict):
            continue
        for provider in OPENCODE_PROVIDERS:
            entry = document.get(provider)
            models = entry.get("models") if isinstance(entry, dict) else None
            if not isinstance(models, dict):
                continue
            rows = []
            for model, info in models.items():
                info = info if isinstance(info, dict) else {}
                date = info.get("release_date") if isinstance(info.get("release_date"), str) else ""
                date = date if re.fullmatch(r"\d{4}-\d{2}(?:-\d{2})?", date) else ""
                rows.append((date, str(model), info.get("name") if isinstance(info.get("name"), str) else ""))
            for date, model, name in sorted(rows, reverse=True)[:PER_PROVIDER]:
                ident = f"{provider}/{model}"
                if MODEL_NAME.fullmatch(ident) and ident not in found:
                    found[ident] = label(ident, name, date)
        if found:
            break  # the host's own catalogue is enough; a kept HOME's only when there is none
    return list(found.items())


def claude_models():
    """[(id, label)]: the aliases (with what they stand for now), then the full ids with their version."""
    rows = [(alias, tr("{version} now (alias; the installed Claude Code decides)", version=version_label(model))
             + f" — {alias}") for alias, model in CLAUDE_ALIASES.items()]
    return rows + [(model, label(model)) for model in CLAUDE_MODELS]


def choices(agent, sai=None, environ=None):
    """[(model id, label with its version)] for AGENT; never raises (a list that cannot be read is left out)."""
    try:
        if agent == "claude":
            return claude_models()
        if agent == "codex":
            return codex_models(environ)
        if agent == "opencode":
            return opencode_models(environ)
    except Exception:  # noqa: BLE001 - a damaged cache: the model can still be typed
        return []
    return []


def fill_combo(combo, agent, current="", sai=None):
    """An editable combo box of AGENT's models (data: the id; text: version and id); the empty first choice is the
    agent's default; CURRENT stays chosen, or typed when not listed."""
    combo.blockSignals(True)
    combo.clear()
    combo.addItem(tr("the agent's default"), "")
    for model, text in choices(agent, sai):
        combo.addItem(text, model)
        combo.setItemData(combo.count() - 1, model, 3)  # Qt.ToolTipRole: the id passed as --model
    index = combo.findData(current)
    if index >= 0:
        combo.setCurrentIndex(index)
    elif current:
        combo.setEditText(current)
    else:
        combo.setCurrentIndex(0)
    combo.blockSignals(False)


def combo_value(combo):
    """The model id chosen or typed in a combo of fill_combo ("" for the agent's default)."""
    text = combo.currentText().strip()
    index = combo.findText(text)
    if index >= 0:
        data = combo.itemData(index)
        return data if data is not None else text
    return text.rsplit(" — ", 1)[-1].strip() if " — " in text else text
