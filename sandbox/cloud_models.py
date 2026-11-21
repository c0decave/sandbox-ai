#!/usr/bin/env python3
"""sandbox-ai models: the cloud models each agent can be started with (--model), and their effort levels (--effort).

  sandbox-ai models                       every agent's models, grouped by provider
  sandbox-ai models --agent AGENT         one agent's
  sandbox-ai models [--agent AGENT] --json  the same as JSON (for programs: the app, the web interface)

The table is cloud_models.json beside this file: for each agent the models it can use with its own login or an API
key - the id exactly as that agent's --model takes it, a display name, the provider, the effort levels the model takes
in the agent's own words with its default, the context size where known, and where it was checked and when. A model's
"efforts" is a list ([] when it has none) or null: not known per model, the agent's own levels hold. Models of a
server of yours (--llm NAME:MODEL) are not here: `sandbox-ai llm models`.

The launcher, the app's Board and wizard and the web interface use this one table: an --effort a listed model does
not take is refused with the same words everywhere (effort_problem); a model that is not listed is passed on as it is
(the Board says so: unknown_note).

Standard library only.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import re
import sys

HERE = Path(__file__).resolve().parent
TABLE = HERE / "cloud_models.json"
VERSION = 1
LIMIT = 4 << 20
MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}")  # the launcher's MODEL_NAME
LEVEL = re.compile(r"[a-z]{1,16}")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
MODEL_KEYS = {"id", "name", "provider", "efforts", "default_effort", "context", "source", "checked", "alias_of"}
AGENT_KEYS = {"name", "effort_option", "efforts", "effort_note", "note", "models"}
_LOADED = {}


def problems(document):
    """What is wrong with DOCUMENT as a table of cloud models ([] when nothing): its version, every agent's levels and
    each model's id, name, provider, levels (a subset of the agent's), default, context, source and date."""
    found = []
    if not isinstance(document, dict) or document.get("version") != VERSION:
        return [f"not a table of cloud models of version {VERSION}"]
    if not isinstance(document.get("checked"), str) or not DATE.fullmatch(document["checked"]):
        found.append("checked: a date YYYY-MM-DD")
    agents = document.get("agents")
    if not isinstance(agents, dict) or not agents:
        return found + ["agents: an object of agents"]
    for agent, entry in agents.items():
        where = f"agents.{agent}"
        if not isinstance(entry, dict):
            found.append(f"{where}: an object")
            continue
        found += [f"{where}.{key}: unknown key" for key in sorted(set(entry) - AGENT_KEYS)]
        if not isinstance(entry.get("name"), str) or not entry["name"]:
            found.append(f"{where}.name: the agent's name")
        levels = entry.get("efforts")
        if not isinstance(levels, list) or not all(isinstance(v, str) and LEVEL.fullmatch(v) for v in levels) or \
                len(set(levels)) != len(levels):
            found.append(f"{where}.efforts: the agent's levels (a list of words, each once)")
            levels = []
        if levels and not isinstance(entry.get("effort_option"), str):
            found.append(f"{where}.effort_option: how the agent takes its effort")
        if not levels and not entry.get("effort_note"):
            found.append(f"{where}.effort_note: where the agent sets its effort instead")
        models = entry.get("models")
        if not isinstance(models, list):
            found.append(f"{where}.models: a list (empty with a note saying why)")
            continue
        if not models and not entry.get("note"):
            found.append(f"{where}.note: why the agent lists no model")
        seen = set()
        for index, model in enumerate(models):
            found += model_problems(f"{where}.models[{index}]", model, levels, seen)
    return found


def model_problems(where, model, levels, seen):
    if not isinstance(model, dict):
        return [f"{where}: an object"]
    found = [f"{where}.{key}: unknown key" for key in sorted(set(model) - MODEL_KEYS)]
    ident = model.get("id")
    if not isinstance(ident, str) or not MODEL_NAME.fullmatch(ident):
        found.append(f"{where}.id: a model name as --model takes it")
    elif ident in seen:
        found.append(f"{where}.id: {ident} is listed twice")
    else:
        seen.add(ident)
    for key in ("name", "provider", "source"):
        if not isinstance(model.get(key), str) or not model[key].strip():
            found.append(f"{where}.{key}: a text")
    if not str(model.get("source", "")).startswith("https://"):
        found.append(f"{where}.source: an https URL")
    if not isinstance(model.get("checked"), str) or not DATE.fullmatch(model["checked"]):
        found.append(f"{where}.checked: a date YYYY-MM-DD")
    efforts = model.get("efforts")
    if efforts is not None and (not isinstance(efforts, list) or not set(efforts) <= set(levels)
                                or len(set(efforts)) != len(efforts)):
        found.append(f"{where}.efforts: null or some of the agent's levels ({', '.join(levels) or 'none'})")
    default = model.get("default_effort")
    if default is not None and (not isinstance(efforts, list) or default not in efforts):
        found.append(f"{where}.default_effort: null or one of the model's levels")
    context = model.get("context")
    if context is not None and (not isinstance(context, int) or isinstance(context, bool) or context <= 0):
        found.append(f"{where}.context: null or a number of tokens")
    alias = model.get("alias_of")
    if alias is not None and (not isinstance(alias, str) or not MODEL_NAME.fullmatch(alias)):
        found.append(f"{where}.alias_of: the model the alias stands for")
    return found


def load(path=None):
    """The table (cloud_models.json beside this file, or PATH), read once per file; ValueError when it is damaged
    (an installation's own file: never silently half a table)."""
    path = Path(path) if path else TABLE
    key = str(path)
    if key not in _LOADED:
        try:
            if path.stat().st_size > LIMIT:
                raise ValueError("larger than 4 MiB")
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError, RecursionError) as exc:  # (deep nesting: damaged too)
            raise ValueError(f"{path}: cannot be read ({type(exc).__name__ if isinstance(exc, RecursionError) else exc})") from None
        found = problems(document)
        if found:
            raise ValueError(f"{path}: {found[0]}" + (f" (and {len(found) - 1} more)" if len(found) > 1 else ""))
        _LOADED[key] = document
    return _LOADED[key]


def table(document=None):
    return load() if document is None else document


def agent_entry(agent, document=None):
    """AGENT's part of the table ({} for an agent it does not have)."""
    entry = table(document)["agents"].get(agent)
    return entry if isinstance(entry, dict) else {}


def models(agent, document=None):
    """AGENT's models ([{id, name, provider, efforts, default_effort, context, source, checked}]), in the table's
    order (the provider's newest first)."""
    return list(agent_entry(agent, document).get("models") or [])


def find(agent, model, document=None):
    """AGENT's entry for the model id MODEL, or None (not listed: it may still be one the agent knows)."""
    model = str(model or "")
    return next((m for m in models(agent, document) if m["id"] == model), None) if model else None


def known_ids(document=None):
    """{agent: (model id, ...)}: the names each agent knows for sure (the launcher's AGENT_MODELS)."""
    return {agent: tuple(m["id"] for m in entry.get("models") or [])
            for agent, entry in table(document)["agents"].items()}


def agent_efforts(agent, document=None):
    return tuple(agent_entry(agent, document).get("efforts") or ())


def efforts(agent, model=None, document=None):
    """The effort levels to offer for AGENT with MODEL: the model's own when the table knows them, else the agent's
    (a model not listed, or one whose levels are not known per model)."""
    entry = find(agent, model, document)
    if entry is not None and isinstance(entry.get("efforts"), list):
        return tuple(entry["efforts"])
    return agent_efforts(agent, document)


def default_effort(agent, model=None, document=None):
    """The level MODEL works at in AGENT when none is chosen, where the table knows it; else None."""
    entry = find(agent, model, document)
    return entry.get("default_effort") if entry is not None else None


def effort_problem(agent, model, effort, document=None):
    """Why EFFORT does not go with the listed MODEL of AGENT (the launcher and the Board warn of it),
    or None: no model, no effort, a model not listed, levels not known per model, or a level the model takes."""
    entry = find(agent, model, document)
    if not effort or entry is None or not isinstance(entry.get("efforts"), list) or effort in entry["efforts"]:
        return None
    name = agent_entry(agent, document).get("name") or agent
    if not entry["efforts"]:
        return f"--effort: {model} has no effort levels in {name}; start without --effort"
    return f"--effort for {model} in {name} is one of {', '.join(entry['efforts'])}: {effort!r}"


def unknown_note(agent, model, document=None):
    """A note for a MODEL AGENT's list does not have (passed on as it is: it may be one the agent knows), or None."""
    model = str(model or "").strip()
    if not model or find(agent, model, document) is not None or agent not in table(document)["agents"]:
        return None
    return (f"{model} is not in the known list of {agent_entry(agent, document).get('name') or agent}'s models "
            f"(sandbox-ai models --agent {agent}): it is passed on as it is")


def context_text(tokens):
    if not tokens:
        return ""
    if tokens >= 1000000 and tokens % 1000000 < 100000:
        return f"{tokens // 1000000}M"
    return f"{round(tokens / 1000)}k"


def levels_text(entry):
    efforts = entry.get("efforts")
    if efforts is None:
        return "the agent's levels"
    if not efforts:
        return "none"
    text = ", ".join(efforts)
    return text + (f" (default {entry['default_effort']})" if entry.get("default_effort") else "")


def lines(agents, document=None):
    """The listing of AGENTS' models as text: per agent its effort levels, then its models grouped by provider."""
    out = []
    for agent in agents:
        entry = agent_entry(agent, document)
        levels = entry.get("efforts") or []
        how = f"{', '.join(levels)} ({entry.get('effort_option')})" if levels else \
            f"--effort is refused; {entry.get('effort_note') or 'set per model in its own configuration'}"
        out.append(f"{entry.get('name') or agent} ({agent}) - effort: {how}")
        if entry.get("note"):
            out.append(f"  {entry['note']}")
        groups = {}
        for model in entry.get("models") or []:
            groups.setdefault(model["provider"], []).append(model)
        width = max([len(m["id"]) for m in entry.get("models") or []] + [10])
        for provider, rows in groups.items():
            out.append(f"  {provider}")
            for model in rows:
                extra = [model["name"]]
                if levels:
                    extra.append(f"effort: {levels_text(model)}")
                if model.get("context"):
                    extra.append(context_text(model["context"]))
                out.append(f"    {model['id']:{width}}  " + " · ".join(extra))
        out.append("")
    out.append(f"Checked {table(document).get('checked')}; a model not listed can always be typed (--model).")
    return out


def help_style(prog):
    loaded = sys.modules.get("sandbox_ai_helpstyle")
    if loaded is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_helpstyle", HERE / "helpstyle.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        sys.modules["sandbox_ai_helpstyle"] = loaded
    return loaded.CommandsFormatter(prog)


def parser():
    p = argparse.ArgumentParser(prog="sandbox-ai models", description=__doc__.split("\n\n")[0],
                                formatter_class=help_style, allow_abbrev=False,
                                epilog="\n\n".join(__doc__.split("\n\n")[2:4]))
    p.add_argument("--agent", metavar="AGENT", help="Only this agent's models (claude, codex, opencode, ...)")
    p.add_argument("--json", action="store_true", help="Print the result as JSON (for programs)")
    return p


def main(argv=None, out=print):
    a = parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        document = load()
    except ValueError as exc:
        print(f"sandbox-ai models: {exc}", file=sys.stderr)
        return 1
    agents = list(document["agents"])
    if a.agent is not None:
        if a.agent not in agents:
            print(f"sandbox-ai models: no agent {a.agent!r}; the agents are {', '.join(agents)}", file=sys.stderr)
            return 2
        agents = [a.agent]
    if a.json:
        out(json.dumps({"version": document["version"], "checked": document["checked"],
                        "agents": {agent: document["agents"][agent] for agent in agents}}, indent=1,
                       ensure_ascii=False))
        return 0
    for line in lines(agents, document):
        out(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
