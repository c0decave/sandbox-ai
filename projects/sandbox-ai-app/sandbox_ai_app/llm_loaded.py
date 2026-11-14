"""The models an LLM server has loaded, and loading and unloading them, without Qt: the arguments of `sandbox-ai llm
loaded NAME --json`, `llm load NAME MODEL [--context N] [--ttl S] [--force]` and `llm unload NAME MODEL|INSTANCE
[--force]` (each value checked as sandbox-ai checks it, none that reads as an option), their answer checked (it
holds what a server said: untrusted), the table's rows and what each instance's way of staying loaded means
(llm.py's KEPT: JIT with an idle TTL, explicit, Ollama's keep_alive, the llama.cpp router's, the server's own).
llm_loaded_view.py is the dialog of the LLM servers page that uses them."""
from __future__ import annotations

import json
import re

from .files import printable, printable_text
from .i18n import N_, tr
from . import wizard_llm as WL

LOADED_SECONDS = 60     # llm loaded: one listing (through a VPN: its tunnel first)
LOAD_SECONDS = 330      # llm load: LM Studio may take minutes for a large model (sandbox-ai waits 300 s)
UNLOAD_SECONDS = 90
CONTEXT_RANGE = (512, 1 << 24)  # llm.py's CONTEXT_RANGE
TTL_MAX = 10 ** 8
KINDS_LOADING = ("lmstudio", "ollama", "llamacpp")  # the servers whose API loads and unloads (llama.cpp: router)
TTL_KINDS = ("ollama",)  # --ttl: only Ollama's API takes one at a load (LM Studio's refuses it)
CONTEXT_KINDS = ("lmstudio", "ollama")  # --context: llama.cpp's router loads with its preset's arguments
KEPT = {"explicit": N_("explicit"), "jit": N_("JIT"), "timed": N_("until idle"), "router": N_("router (LRU)"),
        "server": N_("for the server's run")}
KEPT_HELP = {
    "explicit": N_("Loaded explicitly: it stays until it is unloaded."),
    "jit": N_("Loaded JIT (at its first request), with an idle TTL: LM Studio may unload it when another sandbox's "
              "model is loaded: Auto-Evict; see Help > Keeping models loaded."),
    "timed": N_("Ollama unloads it when it was idle for its keep_alive (Expires), or when memory runs short; each "
                "request sets the time again."),
    "router": N_("llama.cpp's router unloads the least recently used model beyond --models-max or its memory."),
    "server": N_("The server serves it for as long as it runs; nothing loads or unloads it."),
}
STATES = ("loaded", "loading", "sleeping", "unloaded")
TIME = re.compile(r"[0-9T:.+Z -]{1,40}")


def whole(value, high=1 << 62):
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < high else None


def context_value(value):
    value = whole(value)
    return value if value is not None and CONTEXT_RANGE[0] <= value <= CONTEXT_RANGE[1] else None


def loaded_argv(name):
    if WL.valid_name(name) is None:
        raise ValueError(tr("Not a server's name: {name}", name=printable(name)))
    return ["llm", "loaded", name, "--json"]


def check_context(text):
    """The context typed (TEXT), as tokens; None for an empty field (the model's num_ctx, else the server's own);
    ValueError says the range."""
    text = str(text or "").strip()
    if not text:
        return None
    if not text.isdigit() or context_value(int(text)) is None:
        raise ValueError(tr("The context is a number of tokens from {low} to {high}.", low=CONTEXT_RANGE[0],
                            high=CONTEXT_RANGE[1]))
    return int(text)


def check_ttl(text):
    """The seconds typed (TEXT) after which Ollama unloads the idle model; None for an empty field (until it is
    unloaded)."""
    text = str(text or "").strip()
    if not text:
        return None
    if not text.isdigit() or not 1 <= int(text) <= TTL_MAX:
        raise ValueError(tr("The time is a number of seconds from 1 on; empty: it stays until it is unloaded."))
    return int(text)


def load_argv(name, model, context=None, ttl=None, force=False):
    """`llm load NAME MODEL [--context N] [--ttl S] [--force]`; ValueError for a value sandbox-ai would refuse."""
    if WL.valid_name(name) is None:
        raise ValueError(tr("Not a server's name: {name}", name=printable(name)))
    if WL.valid_model(model) is None:
        raise ValueError(tr("Choose a model first."))
    argv = ["llm", "load", name, model]
    if context is not None:
        if context_value(context) is None:
            raise ValueError(tr("The context is a number of tokens from {low} to {high}.", low=CONTEXT_RANGE[0],
                                high=CONTEXT_RANGE[1]))
        argv += ["--context", str(context)]
    if ttl is not None:
        if whole(ttl) is None or not 1 <= ttl <= TTL_MAX:
            raise ValueError(tr("The time is a number of seconds from 1 on; empty: it stays until it is unloaded."))
        argv += ["--ttl", str(ttl)]
    return argv + (["--force"] if force else [])


def unload_args(name, target, force=False):
    """The arguments of `llm unload` after its words (NAME MODEL|INSTANCE [--force]): the caller asks first."""
    if WL.valid_name(name) is None:
        raise ValueError(tr("Not a server's name: {name}", name=printable(name)))
    if WL.valid_model(target) is None:
        raise ValueError(tr("Choose a loaded model first."))
    return [name, target] + (["--force"] if force else [])


def parse_loaded(out):
    """`sandbox-ai llm loaded NAME --json`, checked: {name, kind, instances (None: not reported), used_by: {model:
    [names]}, load, unload, read_at}; ValueError: not such an answer."""
    try:
        data = json.loads(out or "")
    except (ValueError, RecursionError):
        raise ValueError(printable_text((out or "").strip()[-500:]) or tr("no answer")) from None
    if not isinstance(data, dict):
        raise ValueError(tr("not a list of loaded models"))
    instances = None
    if isinstance(data.get("instances"), list):
        instances = []
        for raw in data["instances"][:200]:
            model = raw.get("model") if isinstance(raw, dict) else None
            if WL.valid_model(model) is None:
                continue
            item = {"model": model, "instance": raw["instance"] if WL.valid_model(raw.get("instance")) else model}
            for key in ("size", "memory_bytes", "vram_bytes", "ram_bytes", "ttl_seconds"):
                if whole(raw.get(key)) is not None:
                    item[key] = raw[key]
            if context_value(raw.get("context")):
                item["context"] = raw["context"]
            if raw.get("kept") in KEPT:
                item["kept"] = raw["kept"]
            if raw.get("state") in STATES:
                item["state"] = raw["state"]
            if isinstance(raw.get("expires_at"), str) and TIME.fullmatch(raw["expires_at"].strip()):
                item["expires_at"] = raw["expires_at"].strip()
            instances.append(item)
    used = data.get("used_by") if isinstance(data.get("used_by"), dict) else {}
    used_by = {model: [printable(n)[:64] for n in names[:50] if isinstance(n, str)]
               for model, names in used.items() if WL.valid_model(model) and isinstance(names, list)}
    return {"name": data.get("name") if WL.valid_name(data.get("name")) else None,
            "kind": data.get("kind") if data.get("kind") in {k for k, *_ in WL.KINDS} else None,
            "instances": instances, "used_by": {m: names for m, names in used_by.items() if names},
            "load": data.get("load") is True, "unload": data.get("unload") is True,
            "read_at": data["read_at"] if isinstance(data.get("read_at"), str) and WL.TIME.fullmatch(data["read_at"])
            else ""}


def memory_text(item):
    if "memory_bytes" in item:
        split = [tr("VRAM {size}", size=WL.size_text(item["vram_bytes"]))] if "vram_bytes" in item else []
        split += [tr("RAM {size}", size=WL.size_text(item["ram_bytes"]))] if "ram_bytes" in item else []
        return WL.size_text(item["memory_bytes"]) + (f" ({', '.join(split)})" if split else "")
    if "size" in item:
        return tr("size {size}", size=WL.size_text(item["size"]))
    return ""


def kept_text(item):
    kept = item.get("kept")
    if kept == "jit" and "ttl_seconds" in item:
        return tr("JIT (unloaded after {seconds} s idle)", seconds=item["ttl_seconds"])
    return tr(KEPT[kept]) if kept in KEPT else tr("not reported")


def expires_text(item):
    if item.get("kept") == "explicit":
        return tr("never (until unloaded)")
    return item.get("expires_at") or ""


COLUMNS = (N_("Model"), N_("Instance"), N_("Context"), N_("Memory"), N_("Loaded"), N_("Expires"), N_("Used by"))


def rows(entry):
    """The table's rows of ENTRY (parse_loaded), with their sort values and tooltips: [(cells, sorts, tip)]."""
    found = []
    for item in entry["instances"] or []:
        users = entry["used_by"].get(item["model"]) or []
        cells = [item["model"], item["instance"], str(item.get("context") or ""), memory_text(item), kept_text(item),
                 expires_text(item), ", ".join(users)]
        if item.get("state") and item["state"] != "loaded":
            cells[4] += f" ({item['state']})"
        sorts = [item["model"], item["instance"], item.get("context") or 0, item.get("memory_bytes") or
                 item.get("size") or 0, cells[4], cells[5], len(users)]
        tip = "\n".join(part for part in (item["instance"], tr(KEPT_HELP[item["kept"]]) if item.get("kept") in
                                          KEPT_HELP else "", tr("Used by the running sandboxes: {names}",
                                                                names=", ".join(users)) if users else "") if part)
        found.append((cells, sorts, tip))
    return found


def default_context(view, model):
    """The context a load of MODEL defaults to: num_ctx of its parameters (llm set), else every model's; VIEW is
    `llm show NAME --json` (wizard_llm.parse_show). None: none set (the server's own)."""
    params = view.get("params") if isinstance(view, dict) and isinstance(view.get("params"), dict) else {}
    for scope in (model, "*"):
        values = params.get(scope) if isinstance(params.get(scope), dict) else {}
        if context_value(values.get("num_ctx")):
            return values["num_ctx"]
    return None


def too_old(status, err):
    """Whether the answer says the host's sandbox-ai has no `llm loaded` yet."""
    return status == 2 and "invalid choice" in (err or "")


def jit_instances(entry):
    return [item for item in entry["instances"] or [] if item.get("kept") == "jit"]
