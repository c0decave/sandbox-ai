"""The sampling parameters of an LLM server (sandbox-ai llm set / params) and of a board's start (--llm-param).

sandbox-ai keeps them per server and model (`llm show NAME --json`: "params" {"*" or MODEL: {KEY: VALUE}}, "forced"
{scope: [KEY]}, "evict" never|allow) and its connection proxy applies them to the
requests that go to that server only: a value is set where the agent's request has none, a forced one replaces the
agent's own, max_tokens is a ceiling, num_ctx goes into the agents' configurations. What the parameters are, their
types and ranges come from sandbox-ai itself (`llm params --kind KIND --json`); every value is checked here against
them before anything is saved, and sandbox-ai checks it again.

The loop guard is a set of layers (sandbox-ai's llm.LOOP_DEFAULTS: switches such as "steps" or "sampling", counts such
as "warn", "block" and "text", 0 for off): sandbox-ai's own defaults, yours (defaults.json "loop_guard", the Settings),
a server's own (`llm show --json` "loop_guard_own"; `llm set --loop-guard LAYER=VALUE|LAYER=default`) and a start's
(--llm-param loop_guard=on|off, loop_guard.LAYER=VALUE) over each other; "loop_guard" in `llm show --json` is what
applies.

The checks and argument lists are here (no Qt: the board uses them); the form and the dialog are llm_params_view.py's.
"""
from __future__ import annotations

import json
import math
import re

from .files import printable
from .i18n import N_, tr

SCOPE_ALL = "*"
MODEL = re.compile(r"[\w.:/@+-]{1,200}", re.ASCII)  # as sandbox-ai's llm.MODEL_NAME
NAME = re.compile(r"[A-Za-z0-9_]{1,40}")  # a parameter's name
SERVER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}")  # as sandbox-ai's llm.NAME
# The loop guard's layers as this app knows them (sandbox-ai's own come from its llm.LOOP_DEFAULTS and LOOP_RANGES
# where it has them; a host's from its `llm show --json`): what each does, in words of the app.
LAYER_HELP = {
    "enabled": N_("The whole loop guard: off switches every layer off"),
    "steps": N_("OpenCode: the most steps of its agents before it asks for a text answer (0: OpenCode's own)"),
    "compaction": N_("OpenCode: automatic compaction, old tool output pruned, long tool output cut"),
    "instructions": N_("OpenCode: rules against loops as an instruction file of its own (your AGENTS.md stays)"),
    "sampling": N_("The model family's recommended sampling as the lowest default (Qwen3, QwQ, DeepSeek-R1, gpt-oss)"),
    "context": N_("Warn when the agents are told a larger context window than the server loaded"),
    "warn": N_("The same tool call with the same result this many times: a note tells the model so (0: off)"),
    "block": N_("The same tool call this many times: tool use is paused for that answer (0: off)"),
    "text": N_("One block of text repeated this many times in a row (4 KiB at least): the answer ends (0: off)"),
    "watchdog_requests": N_("Unattended runs: tool use paused after this many model requests in one turn (0: off)"),
    "watchdog_minutes": N_("Unattended runs: tool use paused after this many minutes in one turn (0: off)"),
}
EVICT = (("never", N_("kept loaded (never unloaded for another model)")),
         ("allow", N_("the server may unload them (its own loading)")))
# What each kind of server does about unloading models (sandbox-ai's llm.evict_notes, in words of the app).
EVICT_NOTES = {
    "lmstudio": N_("LM Studio unloads a JIT-loaded model when it JIT-loads another (Auto-Evict, on by default: "
                   "Developer > Server Settings). Kept loaded: a start loads its model explicitly, which Auto-Evict "
                   "leaves alone."),
    "ollama": N_("Ollama unloads a model idle for OLLAMA_KEEP_ALIVE (5 minutes) or when more than "
                 "OLLAMA_MAX_LOADED_MODELS are asked for; its API does not report them: set them on the server "
                 "(OLLAMA_MAX_LOADED_MODELS=2 or more, OLLAMA_KEEP_ALIVE=-1)."),
    "llamacpp": N_("llama.cpp serves one model; in router mode up to --models-max at once (the least recently used "
                   "is unloaded)."),
    "vllm": N_("vLLM serves one model: nothing is unloaded."),
}


def parse_catalogue(out):
    """The parameters of `sandbox-ai llm params [--kind KIND] --json`: [{name, type, min, max, help, ...}], each
    checked (a name, a known type, numbers as bounds); ValueError when the answer is none of that."""
    try:
        data = json.loads(out or "")
    except ValueError:
        raise ValueError(printable((out or "").strip()[-300:]) or tr("no answer")) from None
    if not isinstance(data, list):
        raise ValueError(tr("not a list of parameters"))
    found = []
    for entry in data:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str) or \
                not NAME.fullmatch(entry["name"]) or entry.get("type") not in ("float", "int", "bool"):
            continue
        if entry["type"] != "bool" and not all(isinstance(entry.get(k), (int, float)) and
                                               not isinstance(entry.get(k), bool) for k in ("min", "max")):
            continue
        found.append({"name": entry["name"], "type": entry["type"], "min": entry.get("min"), "max": entry.get("max"),
                      "help": printable(entry.get("help") or "")[:300], "ceiling": entry.get("ceiling") is True,
                      "never_sent": entry.get("never_sent") is True,
                      "sent": entry.get("sent") if isinstance(entry.get("sent"), dict) else {}})
    return found


def local_catalogue(sai, kind=None):
    """The catalogue of this machine's sandbox-ai (its llm.params_catalogue), or None for one without parameters."""
    try:
        module = sai.module("llm")
        if not hasattr(module, "params_catalogue"):
            return None
        return parse_catalogue(json.dumps(module.params_catalogue(kind)))
    except Exception:  # noqa: BLE001 - a sandbox-ai that does not load knows no parameters
        return None


def check_value(entry, text):
    """The value of TEXT for the parameter ENTRY (int, float or bool), or ValueError with the rule it breaks."""
    text = str(text).strip()
    if entry["type"] == "bool":
        lowered = text.lower()
        if lowered in ("true", "on", "yes", "1"):
            return True
        if lowered in ("false", "off", "no", "0"):
            return False
        raise ValueError(tr("true or false"))
    try:
        value = int(text, 10) if entry["type"] == "int" else float(text)
    except ValueError:
        raise ValueError(tr("a whole number") if entry["type"] == "int" else tr("a number")) from None
    if isinstance(value, float) and not math.isfinite(value) or not entry["min"] <= value <= entry["max"]:
        raise ValueError(tr("from {low} to {high}", low=entry["min"], high=entry["max"]))
    return value


def value_text(value):
    """VALUE as sandbox-ai's KEY=VALUE takes it (and as a field shows it)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        return repr(value)  # 1.0, not 1: the type stays visible
    return str(value)


def loop_spec(sai=None, view=None):
    """(defaults {layer: on/off or a count}, ranges {layer: (low, high)}) of the loop guard: this machine's
    sandbox-ai's (llm.LOOP_DEFAULTS, LOOP_RANGES), else the layers VIEW (`llm show --json`) has, without ranges;
    None for a sandbox-ai without layers."""
    try:
        module = sai.module("llm") if sai is not None else None
        defaults, ranges = getattr(module, "LOOP_DEFAULTS", None), getattr(module, "LOOP_RANGES", None)
    except Exception:  # noqa: BLE001 - a sandbox-ai that does not load: the view's layers
        defaults, ranges = None, None
    if isinstance(defaults, dict) and defaults:
        return dict(defaults), dict(ranges or {})
    applied = (view or {}).get("loop_guard")
    if isinstance(applied, dict) and applied and all(isinstance(k, str) and NAME.fullmatch(k) and
                                                     isinstance(v, (bool, int)) for k, v in applied.items()):
        return {key: (value if isinstance(value, bool) else int(value)) for key, value in applied.items()}, {}
    return None


def check_layer(key, text, spec):
    """The value of TEXT for the loop guard's layer KEY (on/off for a switch, 0 or a count in its range), or
    ValueError with the rule."""
    defaults, ranges = spec
    if key not in defaults:
        raise ValueError(tr("not a layer of the loop guard"))
    value = str(text).strip().lower()
    if isinstance(defaults[key], bool):
        if value in ("on", "true", "yes", "1"):
            return True
        if value in ("off", "false", "no", "0"):
            return False
        raise ValueError(tr("on or off"))
    if value == "off":
        return 0
    low, high = ranges.get(key, (0, 10 ** 6))
    if re.fullmatch(r"[0-9]{1,7}", value) and (int(value) == 0 or low <= int(value) <= high):
        return int(value)
    raise ValueError(tr("0 (off) or {low}-{high}", low=low, high=high))


def layer_text(value):
    """A layer's value as `--loop-guard LAYER=VALUE` takes it."""
    if isinstance(value, bool):
        return "on" if value else "off"
    return str(value)


def loop_changes(before, after):
    """The `--loop-guard` values that turn the server's own layers BEFORE into AFTER ({layer: value}): LAYER=VALUE for
    each one set or changed, LAYER=default for each one dropped."""
    found = [f"{key}={layer_text(value)}" for key, value in (after or {}).items()
             if NAME.fullmatch(key) and (key not in (before or {}) or (before or {})[key] != value
                                         or isinstance((before or {})[key], bool) != isinstance(value, bool))]
    found += [f"{key}=default" for key in (before or {}) if NAME.fullmatch(key) and key not in (after or {})]
    return found


def scope_of(view, scope):
    """({key: value}, {forced keys}) of SCOPE in an endpoint as `llm show NAME --json` gives it."""
    params = view.get("params") if isinstance(view.get("params"), dict) else {}
    forced = view.get("forced") if isinstance(view.get("forced"), dict) else {}
    values = params.get(scope) if isinstance(params.get(scope), dict) else {}
    keys = forced.get(scope) if isinstance(forced.get(scope), list) else []
    return dict(values), {k for k in keys if k in values}


def set_argv(name, scope, values, forced, guard=None, evict=None, catalogue=None):
    """`sandbox-ai llm set NAME [--model MODEL] --reset --param|--force-param KEY=VALUE... [--loop-guard G]...
    [--evict E]`: SCOPE's parameters replaced by VALUES at once (FORCED: those in place of the agent's own); GUARD:
    the --loop-guard values (loop_changes)."""
    if not SERVER.fullmatch(str(name)):
        raise ValueError(tr("not a server's name"))
    argv = ["llm", "set", str(name)]
    if scope != SCOPE_ALL:
        if not MODEL.fullmatch(str(scope)) or str(scope).startswith("-"):
            raise ValueError(tr("not a model name"))
        argv += ["--model", str(scope)]
    argv.append("--reset")
    order = [entry["name"] for entry in catalogue or []]
    for key in sorted(values, key=lambda k: (order.index(k) if k in order else len(order), k)):
        if not NAME.fullmatch(key):
            raise ValueError(tr("not a parameter's name"))
        argv += ["--force-param" if key in forced else "--param", f"{key}={value_text(values[key])}"]
    for value in guard or []:
        if not re.fullmatch(r"(?:on|off|[A-Za-z0-9_]{1,40}=[A-Za-z0-9]{1,10})", str(value)):
            raise ValueError(tr("not a layer of the loop guard"))
        argv += ["--loop-guard", str(value)]
    if evict is not None:
        if evict not in dict(EVICT):
            raise ValueError(tr("never or allow"))
        argv += ["--evict", evict]
    return argv


def start_argv(params, catalogue=None):
    """--llm-param KEY=VALUE values of a board's PARAMS ({key: text}; the loop guard's layers as loop_guard.LAYER),
    in the catalogue's order (without one: as given); those not set left out."""
    order = [entry["name"] for entry in catalogue or []]
    keys = list(params or {})
    if catalogue:
        keys.sort(key=lambda k: (order.index(k) if k in order else len(order), k))
    found = []
    for key in keys:
        text = str((params or {}).get(key, "")).strip()
        if text and (NAME.fullmatch(key) or key.startswith("loop_guard.") and NAME.fullmatch(key[11:])):
            found.append(f"{key}={text}")
    return found


def parse_start(values):
    """({key: text}, [values not of that form]) of --llm-param values (KEY=VALUE)."""
    found, other = {}, []
    for value in values or []:
        key, sep, text = str(value).partition("=")
        key = key.strip()
        if key == "loop_guard":
            key = "loop_guard.enabled"  # the whole guard on or off: its "enabled" layer
        if sep and (NAME.fullmatch(key) or key.startswith("loop_guard.") and NAME.fullmatch(key[11:])) \
                and text.strip():
            found[key] = text.strip()
        else:
            other.append(str(value))
    return found, other


def board_problems(params, catalogue, spec=None):
    """[(key, why)] of a board's PARAMS ({key: text}) that are not valid (unknown or out of range); SPEC (loop_spec)
    checks the loop guard's layers (loop_guard.LAYER)."""
    entries = {entry["name"]: entry for entry in catalogue or []}
    found = []
    for key, text in (params or {}).items():
        if not str(text).strip():
            continue
        try:
            if key.startswith("loop_guard."):
                if spec is not None:
                    check_layer(key[11:], text, spec)
            elif key in entries:
                check_value(entries[key], text)
            elif entries:
                raise ValueError(tr("not a parameter sandbox-ai knows"))
        except ValueError as exc:
            found.append((key, str(exc)))
    return found
