"""The models an agent can be started with (--model), each with its version and its effort levels: for the wizard,
the board's guide and the inspector, grouped by provider.

Where the lists come from:

- sandbox-ai's table of cloud models (cloud_models.json, its module cloud_models: `sandbox-ai models`): for every
  agent the models it can use with its own login or an API key - the id as its --model takes it, a name, the
  provider, the effort levels each model takes in the agent's own words. The same table gives the Board's checks and
  the launcher's refusals (an --effort a listed model does not take), so they agree.
- On top of it what this computer's agents fetched themselves: Codex's models_cache.json (~/.codex, $CODEX_HOME, the
  kept HOMEs of sessions) and OpenCode's copy of the models.dev catalogue (~/.cache/opencode/models.json), for the
  providers in OPENCODE_PROVIDERS, newest first - those the table does not list already (a server of sandbox-ai llm
  has its models chosen with --llm NAME:MODEL instead).

Everything read from a file is untrusted (a sandbox could write a kept HOME): size-limited, never through a link,
names checked like the launcher's --model, labels made printable. A model not listed can always be typed (the Board
says it is not in the known list).
"""
from __future__ import annotations

from collections.abc import Mapping
import json
import os
from pathlib import Path
import re
import stat
import threading

from .files import printable
from .i18n import tr
from . import applog

MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}")  # the launcher's MODEL_NAME
LIMIT = 8 << 20
OPENCODE_PROVIDERS = ("anthropic", "openai", "google", "mistral", "deepseek", "xai")
PER_PROVIDER = 12
CLAUDE_ID = re.compile(r"(?:[a-z0-9.-]+[./])?claude-([a-z]+)-(\d+)(?:-(\d{1,2}))?(?:-(\d{8}))?$")
HEADER = "\x00"  # the data of a provider's heading in a combo (never a model: guide_view leaves it out too)
_CLOUD = {"module": None, "tried": False}
_CLOUD_LOCK = threading.Lock()


# -- the table of cloud models (sandbox-ai's cloud_models module) --

def cloud(sai=None):
    """sandbox-ai's cloud_models module with its table loaded, or None (an older sandbox-ai without it, a damaged
    table, none found). SAI's when given; else the one loaded last, or the installation the app finds."""
    with _CLOUD_LOCK:
        if sai is not None and hasattr(sai, "module"):
            try:
                module = sai.module("cloud_models")
                module.load()
                _CLOUD["module"] = module
                return module
            except Exception as _exc:  # noqa: BLE001 - an older sandbox-ai, or a damaged table: no list, typed only
                applog.swallowed("models", "cloud: sandbox-ai's table of cloud models is not there", _exc)
                return None
        if _CLOUD["module"] is None and not _CLOUD["tried"]:
            _CLOUD["tried"] = True
            try:
                from . import backend
                module = backend.SandboxAI().module("cloud_models")
                module.load()
                _CLOUD["module"] = module
            except Exception as _exc:  # noqa: BLE001 - no sandbox-ai found (yet): no list, typed only
                applog.swallowed("models", "cloud: no sandbox-ai with a table of cloud models found", _exc)
        return _CLOUD["module"]


def table_models(agent, sai=None):
    """AGENT's models of the table ([] without one)."""
    module = cloud(sai)
    try:
        return module.models(agent) if module is not None else []
    except Exception as _exc:  # noqa: BLE001 - see cloud()
        applog.swallowed("models", "table_models: the table could not be read", _exc)
        return []


def entry(agent, model, sai=None):
    """The table's entry of MODEL for AGENT, or None (not listed)."""
    return next((m for m in table_models(agent, sai) if m.get("id") == model), None) if model else None


def known_ids(sai=None):
    """{agent: (model id, ...)} of the table (the launcher's AGENT_MODELS); {} without one."""
    module = cloud(sai)
    try:
        return module.known_ids() if module is not None else {}
    except Exception as _exc:  # noqa: BLE001 - see cloud()
        applog.swallowed("models", "known_ids: the table could not be read", _exc)
        return {}


class KnownModels(Mapping):
    """guide.AGENT_MODELS: the table's names per agent, read when first asked (the table is sandbox-ai's)."""

    def __getitem__(self, agent):
        found = known_ids()
        if agent in found:
            return found[agent]
        from .guide import AGENT_EFFORTS
        if agent in AGENT_EFFORTS:
            return ()
        raise KeyError(agent)

    def __iter__(self):
        from .guide import AGENT_EFFORTS
        return iter(dict.fromkeys([*AGENT_EFFORTS, *known_ids()]))

    def __len__(self):
        return len(list(iter(self)))


def efforts(agent, model="", sai=None):
    """The effort levels to offer for AGENT with MODEL: the model's own where the table knows them, else the agent's
    (guide.AGENT_EFFORTS: a model not listed, or one whose levels are not known per model)."""
    from .guide import AGENT_EFFORTS
    found = entry(agent, str(model or "").strip(), sai)
    if found is not None and isinstance(found.get("efforts"), list):
        return tuple(found["efforts"])
    return tuple(AGENT_EFFORTS.get(agent, ()))


def default_effort(agent, model="", sai=None):
    found = entry(agent, str(model or "").strip(), sai)
    return found.get("default_effort") if found is not None else None


def effort_problem(agent, model, effort, sai=None):
    """Why EFFORT does not go with the listed MODEL of AGENT - the launcher's own refusal (cloud_models.effort_problem),
    in the app's language - or None."""
    model = str(model or "").strip()
    found = entry(agent, model, sai)
    if not effort or found is None or not isinstance(found.get("efforts"), list) or effort in found["efforts"]:
        return None
    name = agent_name(agent, sai)
    if not found["efforts"]:
        return tr("--effort: {model} has no effort levels in {agent}; start without --effort", model=model,
                  agent=name)
    return tr("--effort for {model} in {agent} is one of {levels}: {effort}", model=model, agent=name,
              levels=", ".join(found["efforts"]), effort=repr(str(effort)))


def unknown_note(agent, model, sai=None):
    """A soft note for a MODEL the table does not list for AGENT (passed on as it is), or None - also without a
    table."""
    model = str(model or "").strip()
    if not model or not table_models(agent, sai) or entry(agent, model, sai) is not None:
        return None
    return tr("{model} is not in the known list of {agent}'s models (sandbox-ai models --agent {name}): it is passed "
              "on as it is", model=printable(model), agent=agent_name(agent, sai), name=agent)


def agent_name(agent, sai=None):
    module = cloud(sai)
    try:
        return (module.agent_entry(agent).get("name") if module is not None else None) or agent
    except Exception as _exc:  # noqa: BLE001 - see cloud()
        applog.swallowed("models", "agent_name: the table could not be read", _exc)
        return agent


def effort_note(agent, sai=None):
    """Where AGENT sets its effort when it takes no --effort (the table's words), or ""."""
    module = cloud(sai)
    try:
        return str(module.agent_entry(agent).get("effort_note") or "") if module is not None else ""
    except Exception as _exc:  # noqa: BLE001 - see cloud()
        applog.swallowed("models", "effort_note: the table could not be read", _exc)
        return ""


def keep_effort(props, agent, sai=None):
    """The Board's agent part PROPS after its model changed: an effort the new model does not take goes. The level
    removed, or None (nothing to do)."""
    effort = str(props.get("effort") or "")
    if effort and effort not in efforts(agent, props.get("model"), sai):
        props.pop("effort", None)
        return effort
    return None


def context_text(tokens):
    if not isinstance(tokens, int) or tokens <= 0:
        return ""
    if tokens >= 1000000 and tokens % 1000000 < 100000:
        return f"{tokens // 1000000}M"
    return f"{round(tokens / 1000)}k"


def tooltip(found):
    """What a model's row says on hover: its id, levels, context, where it was checked."""
    levels = found.get("efforts")
    parts = [str(found.get("id", ""))]
    parts.append(tr("Effort: the agent's own levels") if levels is None else
                 tr("Effort: none") if not levels else
                 tr("Effort: {levels}", levels=", ".join(levels)) +
                 (" " + tr("(default {level})", level=found["default_effort"]) if found.get("default_effort") else ""))
    if context_text(found.get("context")):
        parts.append(tr("Context: {tokens} tokens", tokens=context_text(found.get("context"))))
    parts.append(tr("Checked {date}: {source}", date=found.get("checked", ""), source=found.get("source", "")))
    return "\n".join(printable(part) for part in parts)


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


def table_label(agent, found, sai=None):
    """What the list shows for a model of the table: its name and its id (an alias of Claude Code: the model it stands
    for now)."""
    model = str(found.get("id", ""))
    alias = found.get("alias_of")
    if alias:
        target = entry(agent, alias, sai)
        words = tr("{version} now (alias; the installed Claude Code decides)",
                   version=printable((target or {}).get("name") or version_label(alias) or alias))
        return f"{words} — {model}"
    return label(model, str(found.get("name") or ""))


def grouped(agent, sai=None, environ=None):
    """[(provider, [(model id, label, tooltip)])] for AGENT: the table's models by provider in its order, then what
    this computer's Codex and OpenCode fetched themselves that the table does not list. Never raises (a list that
    cannot be read is left out)."""
    groups, seen = {}, set()
    for found in table_models(agent, sai):
        model = str(found.get("id", ""))
        if not MODEL_NAME.fullmatch(model) or model in seen:
            continue
        seen.add(model)
        groups.setdefault(printable(str(found.get("provider") or "?")), []).append(
            (model, table_label(agent, found, sai), tooltip(found)))
    try:
        fetched = codex_models(environ) if agent == "codex" else opencode_models(environ) if agent == "opencode" else []
    except Exception as _exc:  # noqa: BLE001 - a damaged cache: the model can still be typed
        applog.swallowed("models", "grouped: a damaged cache: the model can still be typed", _exc)
        fetched = []
    extra = [(model, text, model) for model, text in fetched if model not in seen]
    if extra:
        groups.setdefault(tr("Fetched by the agent on this computer"), []).extend(extra)
    return list(groups.items())


def choices(agent, sai=None, environ=None):
    """[(model id, label with its version)] for AGENT, the providers one after the other (grouped())."""
    return [(model, text) for _, rows in grouped(agent, sai, environ) for model, text, _ in rows]


def fill_combo(combo, agent, current="", sai=None):
    """An editable combo box of AGENT's models, grouped by provider under a heading each (not choosable), typing
    filters them (model_combo.searchable); data: the id, text: name and id, tooltip: its effort levels, context and
    source. The empty first choice is the agent's default; CURRENT stays chosen, or typed when not listed."""
    from . import widgets  # Qt: only where a combo is filled
    from .model_combo import searchable
    from PySide6.QtCore import Qt
    combo.blockSignals(True)
    combo.clear()
    combo.addItem(tr("the agent's default"), "")
    for provider, rows in grouped(agent, sai):
        combo.addItem(f"— {provider} —", HEADER + provider)
        item = combo.model().item(combo.count() - 1) if hasattr(combo.model(), "item") else None
        if item is not None:
            item.setFlags(item.flags() & ~(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled))
        for model, text, tip in rows:
            combo.addItem(text, model)
            combo.setItemData(combo.count() - 1, widgets.safe_tooltip(tip), Qt.ItemDataRole.ToolTipRole)
    searchable(combo)
    index = combo.findData(current) if current else 0
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
        if isinstance(data, str) and data.startswith(HEADER):
            return ""
        return data if data is not None else text
    return text.rsplit(" — ", 1)[-1].strip() if " — " in text else text
