"""Model and harness per use case: which local model, in which agent, with which settings, for what kind of work.

The model profiles (model_profiles.json) say how a model family works in each harness. sandbox-ai's local-model research
also measures use cases - coding, sandbox behaviour, web research, code review, security, large codebases, building
from a spec, ops, small fast jobs, vision, teams and helpers, recovery - and model_usecases.json keeps that as a
reviewed table: per use case its research tasks and the RANKED recommendations, each a model pattern (a regular
expression on the model's name, case-insensitive) with examples, the harness, settings over the model's profile, a
one-line why, known failures, and the evidence with its round and date; "basis" says whether the evidence comes from
the use case's own tasks ("measured") or from other tasks ("indirect"). A use case without one is "not tested": its
"unknown" bullets say what is not known, its "fails" what went wrong where it was seen. Nothing here names a model.

recommend() takes the models the user's LLM servers actually have (`llm models`: the names, sizes, what is loaded,
what reads images) and the servers' memory (`llm set NAME --memory GB`, this host's own for a server on its loopback)
and picks the best tested choice that fits - loaded first, then what fits, then what may not load - else the nearest:
the choice of a related use case, else the model with the best traffic light (model_profiles.support), always with why.
`sandbox-ai llm recommend`, `sandbox-ai AGENT --llm SERVER --use-case NAME`, the app's Board and the web's start form
use it; at a start the use case's settings for the model in that agent go over the model's profile and under
everything you set yourself (overlay):

    the profile < the use case's settings < the endpoint's values (llm set) < --llm-param < explicit options

A recommendation is a PAIR: a model and the harness it was measured best in ("untrusted input: Ornith-35B x Claude
Code"). pairs() ranks every model a server has as such a pair - the table's (each with its first recommendation's
harness), then the models it does not have ("not measured", their harness by the traffic light), then those the use
case's "unsafe" list marks (a model that ran a planted command where the input is untrusted: shown with the reason,
never hidden, never the choice nor the nearest); agent_note() says, for a start's agent that is not the pair's harness,
what is known of the model there (the use case's rank in it, else the model profiles' status - model_profiles.support,
the profiles' own API -, else "not tested").

Round 5 fills the table in: add recommendations with basis "measured", "evidence": "round 5: ...", "round": "round 5"
(and the round's date under "rounds"), "runs"/"solved" or "score" (0..1, the task's grader), and drop the "unknown"
bullets that are known now. tests/test_model_usecases.py checks the schema.

Standard library only; Python 3.9.
"""
import importlib.util
import json
import os
from pathlib import Path
import re
import threading

HERE = Path(__file__).resolve().parent
TABLE = HERE / "model_usecases.json"
SCHEMA = 1
TABLE_LIMIT = 1 << 20
ID = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
TASK = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,39}")
BASES = ("measured", "indirect")
NEEDS = ("vision",)
STATUS_TEXT = {"measured": "measured on this use case's tasks",
               "indirect": "from other tasks than this use case's own",
               "not tested": "not tested yet"}
WHY_LIMIT = 120
TABLE_KEYS = {"schema", "source", "rounds", "use_cases"}
CASE_KEYS = {"id", "label", "about", "tasks", "needs", "related", "fails", "unknown", "recommendations", "unsafe"}
# A pair the research marks UNSAFE for a use case (a model that ran a planted command where the input is untrusted):
# the model in every harness, shown with the pair it was measured in and why - never hidden, never chosen on its own.
UNSAFE_KEYS = {"model", "examples", "label", "harness", "why", "evidence", "round", "date", "runs", "solved", "score"}
UNSAFE_NEEDS = {"model", "examples", "harness", "why", "evidence", "round"}
OWN_KEYS = {"model", "examples", "label", "harness", "basis", "why", "score"}
# What a recommendation shares with a model profile's pair entry (checked by model_profiles.check_settings).
SHARED_KEYS = {"evidence", "round", "date", "runs", "solved", "params", "loop_guard", "env", "tools", "notes", "fails"}
REC_KEYS = OWN_KEYS | SHARED_KEYS
# What fits: a model needs about its size and a quarter more (the KV cache of a 128k window and the runtime: round 4's
# load checks, e.g. 18.2 GB of weights wired 22.9 GB at 131072), and gets about three quarters of a machine's memory
# (macOS gives the GPU ~75% of unified memory; LM Studio's guardrail refuses more). Said as "about", never refused.
GB = 1000 ** 3
OVERHEAD = 1.25
USABLE = 0.75
FIT_ORDER = ("loaded", "fits", "unknown", "tight", "too big")
LIGHT_RANK = {"green": 0, "yellow": 1}
LOCK = threading.Lock()
_loaded = {}
_profiles = None


def profiles_module():
    """model_profiles.py beside this module (its checks and harness names), loaded once."""
    global _profiles
    with LOCK:
        if _profiles is None:
            spec = importlib.util.spec_from_file_location("sandbox_model_profiles_usecases", HERE / "model_profiles.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _profiles = module
    return _profiles


def harness_name(agent):
    return profiles_module().HARNESS_NAMES.get(agent, str(agent))


# --- the table: its schema ------------------------------------------------------------------------------------------

def _why(value, where):
    if not isinstance(value, str) or not value.strip() or len(value) > WHY_LIMIT or \
            any(not c.isprintable() for c in value):
        raise ValueError(f"{where}: why is one line of printable text (at most {WHY_LIMIT} characters)")
    return value


def check_recommendation(found, where, rounds, check_param=None, check_layer=None):
    """One recommendation FOUND, checked: model pattern and examples, harness, basis, why, evidence of a known round,
    the settings sandbox-ai takes (model_profiles' checks with CHECK_PARAM, CHECK_LAYER), runs/solved, score."""
    mp = profiles_module()
    if not isinstance(found, dict) or set(found) - REC_KEYS or not {"model", "examples", "harness", "basis", "why",
                                                                     "evidence", "round"} <= set(found):
        missing = sorted({"model", "examples", "harness", "basis", "why", "evidence", "round"} - set(found)) \
            if isinstance(found, dict) else []
        raise ValueError(f"{where}: an object of {', '.join(sorted(REC_KEYS))}"
                         + (f" (missing: {', '.join(missing)})" if missing else ""))
    try:
        pattern = re.compile(mp._text(found["model"], f"{where}: model"), re.I)
    except re.error as exc:
        raise ValueError(f"{where}: model is no regular expression ({exc})") from None
    examples = mp._texts(found["examples"], f"{where}: examples")
    if not examples:
        raise ValueError(f"{where}: examples names at least one model")
    for example in examples:
        if not pattern.search(example):
            raise ValueError(f"{where}: the example {example!r} does not match the model pattern")
    where = f"{where} ({found.get('label') or examples[0]})"
    mp._text(found.get("label", examples[0]), f"{where}: label")
    if found["harness"] not in mp.HARNESSES:
        raise ValueError(f"{where}: {found['harness']!r} is not a harness ({', '.join(mp.HARNESSES)})")
    if found["basis"] not in BASES:
        raise ValueError(f"{where}: basis is {' or '.join(BASES)}")
    _why(found["why"], where)
    shared = {key: value for key, value in found.items() if key in SHARED_KEYS}
    mp.check_settings(shared, where, SHARED_KEYS, found["harness"], check_param, check_layer)
    if found["round"] not in rounds:
        raise ValueError(f"{where}: round {found['round']!r} is not in the table's rounds")
    if "solved" in found and found["solved"] > found.get("runs", 0):
        raise ValueError(f"{where}: solved is at most runs")
    score = found.get("score")
    if score is not None and (isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 1):
        raise ValueError(f"{where}: score is a number from 0 to 1 (the task's grader)")
    return found


def check_unsafe(found, where, rounds):
    """One unsafe pair FOUND of a use case, checked: model pattern and examples, the harness it was measured in, why
    (the reason, one line), evidence of a known round, runs/solved, score."""
    mp = profiles_module()
    if not isinstance(found, dict) or set(found) - UNSAFE_KEYS or not UNSAFE_NEEDS <= set(found):
        raise ValueError(f"{where}: an object of {', '.join(sorted(UNSAFE_KEYS))}")
    try:
        pattern = re.compile(mp._text(found["model"], f"{where}: model"), re.I)
    except re.error as exc:
        raise ValueError(f"{where}: model is no regular expression ({exc})") from None
    examples = mp._texts(found["examples"], f"{where}: examples")
    if not examples or not all(pattern.search(example) for example in examples):
        raise ValueError(f"{where}: an example does not match the model pattern")
    where = f"{where} ({found.get('label') or examples[0]})"
    mp._text(found.get("label", examples[0]), f"{where}: label")
    mp._text(found["evidence"], f"{where}: evidence")
    if found["harness"] not in mp.HARNESSES:
        raise ValueError(f"{where}: {found['harness']!r} is not a harness ({', '.join(mp.HARNESSES)})")
    _why(found["why"], where)
    if found["round"] not in rounds:
        raise ValueError(f"{where}: round {found['round']!r} is not in the table's rounds")
    if "date" in found:
        mp._text(found["date"], f"{where}: date")
    for key in ("runs", "solved"):
        if key in found and (isinstance(found[key], bool) or not isinstance(found[key], int) or found[key] < 0):
            raise ValueError(f"{where}: {key} is a whole number")
    if "solved" in found and found["solved"] > found.get("runs", 0):
        raise ValueError(f"{where}: solved is at most runs")
    score = found.get("score")
    if score is not None and (isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 1):
        raise ValueError(f"{where}: score is a number from 0 to 1 (the task's grader)")
    return found


def check_table(document, check_param=None, check_layer=None):
    """The table DOCUMENT (model_usecases.json), checked whole; ValueError names the place of the first fault."""
    mp = profiles_module()
    if not isinstance(document, dict) or document.get("schema") != SCHEMA or set(document) - TABLE_KEYS or \
            not isinstance(document.get("use_cases"), list):
        raise ValueError(f"model use cases: schema {SCHEMA} with source, rounds and use_cases")
    if "source" in document:
        mp._text(document["source"], "model use cases: source")
    rounds = document.get("rounds", {})
    if not isinstance(rounds, dict) or len(rounds) > 100 or not all(
            isinstance(name, str) and 0 < len(name) <= 40 and name.isprintable() and name.strip() and
            isinstance(date, str) and 0 < len(date) <= 40 and date.isprintable() for name, date in rounds.items()):
        raise ValueError("model use cases: rounds maps a round to its date (short texts: \"round 5\": \"2026-11-18\")")
    seen = []
    for index, entry in enumerate(document["use_cases"]):
        where = f"model use cases: use case {index}"
        if not isinstance(entry, dict) or set(entry) - CASE_KEYS or \
                not {"id", "label", "about", "tasks", "recommendations"} <= set(entry):
            raise ValueError(f"{where}: an object of {', '.join(sorted(CASE_KEYS))}")
        if not isinstance(entry["id"], str) or not ID.fullmatch(entry["id"]) or entry["id"] in seen:
            raise ValueError(f"{where}: id is a new short name (a-z, 0-9, -)")
        seen.append(entry["id"])
        where = f"model use cases: {entry['id']}"
        mp._text(entry["label"], f"{where}: label")
        mp._text(entry["about"], f"{where}: about")
        tasks = entry["tasks"]
        if not isinstance(tasks, list) or not 0 < len(tasks) <= 40 or \
                not all(isinstance(task, str) and TASK.fullmatch(task) for task in tasks):
            raise ValueError(f"{where}: tasks names the research's tasks of this use case (short names)")
        needs = entry.get("needs", [])
        if not isinstance(needs, list) or not all(need in NEEDS for need in needs):
            raise ValueError(f"{where}: needs is a list of {', '.join(NEEDS)}")
        for key in ("fails", "unknown"):
            if key in entry:
                mp._bullets(entry[key], f"{where}: {key}")
        recommendations = entry["recommendations"]
        if not isinstance(recommendations, list) or len(recommendations) > 20:
            raise ValueError(f"{where}: recommendations is a list (at most 20), best first")
        pairs = set()
        for rank, found in enumerate(recommendations, 1):
            check_recommendation(found, f"{where}: recommendation {rank}", rounds, check_param, check_layer)
            pair = (found["model"].lower(), found["harness"])
            if pair in pairs:
                raise ValueError(f"{where}: recommendation {rank}: the same model and harness twice")
            pairs.add(pair)
        unsafe = entry.get("unsafe", [])
        if not isinstance(unsafe, list) or len(unsafe) > 20:
            raise ValueError(f"{where}: unsafe is a list (at most 20) of the pairs not to use for it")
        for number, found in enumerate(unsafe, 1):
            check_unsafe(found, f"{where}: unsafe {number}", rounds)
        for rank, found in enumerate(recommendations, 1):
            marked = next((u for u in unsafe for example in found["examples"]
                           if re.search(u["model"], example, re.I)), None)
            if marked is not None:
                raise ValueError(f"{where}: recommendation {rank} ({found.get('label') or found['examples'][0]}): "
                                 f"its model is marked unsafe for this use case ({marked['why']})")
    for entry in document["use_cases"]:
        related = entry.get("related", [])
        if not isinstance(related, list) or not all(name in seen and name != entry["id"] for name in related):
            raise ValueError(f"model use cases: {entry['id']}: related names other use cases of the table")
    return document


def load(path=None, check_param=None, check_layer=None):
    """The table at PATH (default: model_usecases.json beside this module, or SANDBOX_AI_MODEL_USECASES), checked;
    read again when the file changed. ValueError (or OSError) when it cannot be used."""
    path = Path(path or os.environ.get("SANDBOX_AI_MODEL_USECASES") or TABLE)
    stat = path.stat()
    key = (str(path), stat.st_mtime_ns, stat.st_size, check_param is None, check_layer is None)
    with LOCK:
        if key in _loaded:
            return _loaded[key]
    with open(path, "rb") as handle:
        data = handle.read(TABLE_LIMIT + 1)
    if len(data) > TABLE_LIMIT:
        raise ValueError(f"{path}: larger than {TABLE_LIMIT} bytes")
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ValueError(f"{path}: not JSON ({exc})") from None
    check_table(document, check_param, check_layer)
    with LOCK:
        _loaded.clear()
        _loaded[key] = document
    return document


def names(table):
    """The use cases' ids, in the table's order."""
    return [entry["id"] for entry in table["use_cases"]]


def use_case(table, name):
    """The use case NAME (its id or its label, case-insensitive); ValueError naming the known ones."""
    word = str(name or "").strip().lower()
    for entry in table["use_cases"]:
        if word in (entry["id"], entry["label"].lower()):
            return entry
    raise ValueError(f"no use case {str(name)[:60]!r} (known: {', '.join(names(table))}; sandbox-ai llm recommend "
                     "lists them)")


def unsafe_for(entry, name):
    """The unsafe pair of ENTRY whose model pattern matches the model NAME, or None."""
    return next((found for found in entry.get("unsafe") or [] if re.search(found["model"], str(name), re.I)), None)


def status(entry):
    """"measured" (a recommendation from the use case's own tasks), "indirect" (only from other tasks) or "not
    tested" (none)."""
    bases = {found["basis"] for found in entry["recommendations"]}
    return "measured" if "measured" in bases else "indirect" if bases else "not tested"


# --- what fits: a model's size against its server's memory --------------------------------------------------------

def gb(value):
    """VALUE bytes as whole gigabytes (decimal, as `llm models` shows sizes): "88 GB"."""
    return f"{int(value / GB + 0.5)} GB"


def fit(model, server):
    """Whether MODEL ({name, size, loaded}) fits SERVER ({name, memory_bytes, models}): {"state": loaded | fits |
    tight (may not load) | too big | unknown, "text": in words}. Its size and a quarter more against three quarters of
    the memory, less what the server's other loaded models take (said: "after unloading ...")."""
    if model.get("loaded"):
        return {"state": "loaded", "text": "loaded now"}
    size, memory = model.get("size"), server.get("memory_bytes")
    if not memory:
        return {"state": "unknown", "text": "fit not known: nobody said the server's memory (sandbox-ai llm set "
                                            f"{server.get('name') or 'NAME'} --memory GB)"}
    if not size:
        return {"state": "unknown", "text": "fit not known: the server does not report the model's size"}
    need, usable = size * OVERHEAD, memory * USABLE
    others = sum(m.get("size") or 0 for m in server.get("models") or []
                 if m.get("loaded") and m.get("name") != model.get("name"))
    if need <= usable:
        after = f" after unloading the {gb(others)} loaded now" if need > usable - others else ""
        return {"state": "fits", "text": f"fits: about {gb(need)} of {gb(usable)}{after}"}
    if need <= memory:
        return {"state": "tight", "text": f"may not load: about {gb(need)} of {gb(memory)}, more than the "
                                          f"{gb(usable)} a model usually gets (unload others, or raise the server's "
                                          "limit)"}
    return {"state": "too big", "text": f"too big: about {gb(need)}, the server has {gb(memory)}"}


def server_entry(found):
    """A server of the caller ({name, memory_bytes?, answered?, error?, models: [names or {name, size, loaded,
    capabilities}]}) in one shape."""
    models = []
    for model in found.get("models") or []:
        model = {"name": model} if isinstance(model, str) else model
        if isinstance(model, dict) and isinstance(model.get("name"), str) and model["name"]:
            models.append({"name": model["name"], "size": model.get("size") if isinstance(model.get("size"), int)
                           and not isinstance(model.get("size"), bool) and model["size"] > 0 else None,
                           "loaded": model.get("loaded") is True,
                           "capabilities": model.get("capabilities") if isinstance(model.get("capabilities"), list)
                           else None})
    memory = found.get("memory_bytes")
    return {"name": str(found.get("name") or ""), "models": models,
            "memory_bytes": memory if isinstance(memory, int) and not isinstance(memory, bool) and memory > 0 else None,
            "answered": found.get("answered", True) is not False, "error": found.get("error")}


def reads(model, needs):
    """Whether MODEL can do what the use case NEEDS: "yes", "no" (the server says it cannot) or "unknown"."""
    if not needs:
        return "yes"
    capabilities = model.get("capabilities")
    if capabilities is None:
        return "unknown"
    return "yes" if all(need in capabilities for need in needs) else "no"


def embedding(model):
    return "embed" in model["name"].lower() or "embedding" in (model.get("capabilities") or [])


# --- the recommendation ------------------------------------------------------------------------------------------

def _date(found, table):
    return found.get("date") or table.get("rounds", {}).get(found.get("round")) or None


def _settings(found):
    values = dict(found.get("params") or {})
    values.update({f"loop_guard.{key}": value for key, value in (found.get("loop_guard") or {}).items()})
    values.update({f"env.{key}": value for key, value in (found.get("env") or {}).items()})
    if found.get("tools") is not None:
        values["tools"] = found["tools"]
    return values


def _choice(table, entry, rank, found, server, model, agent):
    """One candidate: the server's MODEL for the recommendation FOUND (rank RANK) of ENTRY."""
    mp = profiles_module()
    place = fit(model, server)
    own = agent is None or found["harness"] == agent
    settings = _settings(found) if own else {}
    notes = []
    if not own:
        notes.append(f"measured in {harness_name(found['harness'])}, not in {harness_name(agent)}: the use case's "
                     f"settings are for {harness_name(found['harness'])}")
    if reads(model, entry.get("needs") or []) == "unknown":
        notes.append("the server does not say whether it reads images; the table's evidence does")
    harness = found["harness"]  # where it was measured (the start's own agent stays the start's: note)
    return {"server": server["name"], "model": model["name"], "harness": harness,
            "harness_name": harness_name(harness), "measured_in": found["harness"], "rank": rank, "source": "table",
            "related": None, "basis": found["basis"], "label": found.get("label") or found["examples"][0],
            "why": found["why"], "fails": list(found.get("fails") or []), "evidence": found.get("evidence"),
            "round": found.get("round"), "date": _date(found, table), "runs": found.get("runs"),
            "solved": found.get("solved"), "score": found.get("score"), "settings": settings,
            "settings_text": mp.values_text(settings), "fit": place["state"], "fit_text": place["text"],
            "loaded": model["loaded"], "size": model["size"], "note": "; ".join(notes),
            "command": f"sandbox-ai {harness} --llm {server['name']}:{model['name']} --use-case {entry['id']}"}


def _order(candidate, agent, index):
    """Best first: what may not load last, the start's own harness first, then the rank, loaded before fits."""
    return (candidate["fit"] == "tight", agent is not None and candidate["measured_in"] != agent, candidate["rank"],
            FIT_ORDER.index(candidate["fit"]), index)


def _nearest(table, entry, servers, agent, profiles):
    """No model of ENTRY's table on the servers: the choice of a related use case, else the model with the best
    traffic light (PROFILES: model_profiles' table; None: no such choice) that can do what the use case needs."""
    for name in entry.get("related") or []:
        other = use_case(table, name)
        found = _table_choice(table, other, servers, agent, guard=entry)[0]
        if found is not None:
            # Its settings are the related use case's: a start with --use-case of this one applies only this one's
            # (settings_for), so none are shown - where they are is said.
            moved = f"its settings for it: --use-case {other['id']}" if found["settings"] else ""
            found.update(source="related", related=other["id"], settings={}, settings_text="",
                         note="; ".join(filter(None, [
                             f"not tested for {entry['label']}: the choice for {other['label']}", found["note"], moved])),
                         command=found["command"].rsplit(" --use-case ", 1)[0] + f" --use-case {entry['id']}")
            return found
    if profiles is None:
        return None
    mp = profiles_module()
    needs = entry.get("needs") or []
    candidates = []
    for server in servers:
        for model in server["models"]:
            if embedding(model) or reads(model, needs) != "yes" or unsafe_for(entry, model["name"]):
                continue
            found = mp.support(model["name"], profiles)
            if found["light"] not in LIGHT_RANK:
                continue
            place = fit(model, server)
            if place["state"] == "too big":
                continue
            green = found["by_light"].get("green") or []
            yellow = found["by_light"].get("yellow") or []
            harness = agent or (green or yellow or [None])[0]
            if harness is None:
                continue
            own = 0 if harness in green else 1 if harness in yellow else 2
            candidates.append(((place["state"] == "tight", LIGHT_RANK[found["light"]], own,
                                FIT_ORDER.index(place["state"]), len(candidates)), server, model, found, place,
                               harness))
    if not candidates:
        return None
    _, server, model, found, place, harness = min(candidates, key=lambda c: c[0])
    why = "not in this use case's table" + ("; this use case is not tested yet" if status(entry) == "not tested"
                                            else "")
    return {"server": server["name"], "model": model["name"], "harness": harness,
            "harness_name": harness_name(harness), "measured_in": None, "rank": None, "source": "traffic light",
            "related": None, "basis": None, "label": found.get("label") or model["name"],
            "why": f"its traffic light: {found['text']}", "fails": [], "evidence": None, "round": None,
            "date": None, "runs": None, "solved": None, "score": None, "settings": {}, "settings_text": "",
            "fit": place["state"], "fit_text": place["text"], "loaded": model["loaded"], "size": model["size"],
            "note": f"{why}: chosen by its traffic light (the debugging and smoke tests), nothing measured for "
                    f"{entry['label']}",
            "command": f"sandbox-ai {harness} --llm {server['name']}:{model['name']} --use-case {entry['id']}"}


def _table_choice(table, entry, servers, agent, guard=None):
    """(the best candidate of ENTRY's recommendations or None, [the other usable candidates], [the recommendations
    not available with why]). A model ENTRY (or GUARD: the use case a related one's choice is for) marks unsafe is
    never a candidate."""
    candidates, missing = [], []
    needs = entry.get("needs") or []
    for rank, found in enumerate(entry["recommendations"], 1):
        usable, too_big, blind = [], [], []
        for server in servers:
            for model in server["models"]:
                if not re.search(found["model"], model["name"], re.I) or unsafe_for(entry, model["name"]) or \
                        (guard is not None and unsafe_for(guard, model["name"])):
                    continue
                if reads(model, needs) == "no":
                    blind.append(server["name"])
                    continue
                candidate = _choice(table, entry, rank, found, server, model, agent)
                (too_big if candidate["fit"] == "too big" else usable).append(candidate)
        candidates += usable
        if not usable:
            label = found.get("label") or found["examples"][0]
            reason = f"{too_big[0]['fit_text']} ({too_big[0]['server']}:{too_big[0]['model']})" if too_big else \
                f"the server says it does not read images ({', '.join(blind)})" if blind else "not on your servers"
            missing.append({"rank": rank, "label": label, "harness": found["harness"],
                            "harness_name": harness_name(found["harness"]), "reason": reason})
    ordered = [c for _, c in sorted(((_order(c, agent, i), c) for i, c in enumerate(candidates)),
                                    key=lambda pair: pair[0])]
    best = ordered[0] if ordered else None
    seen = {(best["server"], best["model"])} if best else set()
    others = []
    for candidate in ordered[1:]:
        if (candidate["server"], candidate["model"]) not in seen and len(others) < 5:
            seen.add((candidate["server"], candidate["model"]))
            others.append(candidate)
    return best, others, missing


def _status_in(entry, model, agent, profiles):
    """What is known of MODEL in AGENT: the use case's own word where its table has the pair ("rank 3, from other
    tasks"), else the model profiles' status for that harness (model_profiles.support: "partial", "verified" ...;
    the family's own for a harness without an entry), else "not tested"."""
    light = profiles_module().support(model, profiles) if profiles is not None else None
    return _known_in(entry, model, agent, light)["text"]


def _known_in(entry, model, agent, light):
    """_status_in() in parts: {rank (the table's, or None), measured (its basis, None without a rank), status (the
    profiles' for AGENT from LIGHT - support() of MODEL, None: no profiles -, or None), text (_status_in's words)}."""
    for rank, found in enumerate(entry["recommendations"], 1):
        if found["harness"] == agent and re.search(found["model"], model, re.I):
            measured = found["basis"] == "measured"
            return {"rank": rank, "measured": measured, "status": None,
                    "text": f"rank {rank}, " + ("measured" if measured else "from other tasks")}
    status = None
    if light is not None:
        own = next((h for h in light["harnesses"] if h["agent"] == agent), None)
        if own is not None:
            status = own["status"]
        elif light.get("status") and agent in light.get("untested", []):
            status = light["status"]
    return {"rank": None, "measured": None, "status": status, "text": status or "not tested"}


def in_agents(entry, pair, profiles):
    """{agent: _known_in()} for every harness but PAIR's own that something is known of (a rank in ENTRY's table,
    a profile status): what a page whose agent is not the pair's harness says of it, in its own language."""
    mp = profiles_module()
    light = mp.support(pair["model"], profiles) if profiles is not None else None
    found = {}
    for agent in mp.HARNESSES:
        if agent != pair.get("harness"):
            known = _known_in(entry, pair["model"], agent, light)
            if known["rank"] is not None or known["status"] is not None:
                found[agent] = known
    return found


def agent_note(entry, pair, agent, profiles):
    """PAIR (one of pairs()) when AGENT is not its harness: "MODEL is measured best in Claude Code; in OpenCode:
    partial" (not measured: "best by its traffic light in ..."); "" otherwise."""
    if not agent or not pair.get("harness") or pair["harness"] == agent:
        return ""
    known = _status_in(entry, pair["model"], agent, profiles)
    if pair["source"] == "not measured":
        return f"{pair['model']} is not measured for this use case, best by its traffic light in " \
               f"{harness_name(pair['harness'])}; in {harness_name(agent)}: {known}"
    return f"{pair['model']} is measured best in {harness_name(pair['harness'])}; in {harness_name(agent)}: {known}"


def _other_pair(table, entry, server, model, source, harness, unsafe=None, light=None):
    """A pair of PAIRS that is not one of the table's recommendations: SOURCE "unsafe" (UNSAFE: the use case's
    unsafe entry) or "not measured" (HARNESS: its best by the traffic light, LIGHT that support())."""
    place = fit(model, server)
    if unsafe is not None:
        why, label = unsafe["why"], unsafe.get("label") or unsafe["examples"][0]
        evidence, round_, date = unsafe["evidence"], unsafe["round"], _date(unsafe, table)
    else:
        why = "not measured for this use case" + (f"; its traffic light: {light['text']}" if light else "")
        label, evidence, round_, date = (light or {}).get("label") or model["name"], None, None, None
    return {"server": server["name"], "model": model["name"], "harness": harness,
            "harness_name": harness_name(harness) if harness else "", "measured_in": harness if unsafe else None,
            "rank": None, "source": source, "related": None, "basis": None, "label": label, "why": why,
            "fails": [], "evidence": evidence, "round": round_, "date": date,
            "runs": (unsafe or {}).get("runs"), "solved": (unsafe or {}).get("solved"),
            "score": (unsafe or {}).get("score"), "settings": {}, "settings_text": "", "fit": place["state"],
            "fit_text": place["text"], "loaded": model["loaded"], "size": model["size"], "note": "",
            "unsafe": unsafe["why"] if unsafe else None,
            "command": (f"sandbox-ai {harness} --llm {server['name']}:{model['name']} --use-case {entry['id']}"
                        if harness else f"sandbox-ai AGENT --llm {server['name']}:{model['name']} --use-case "
                                        f"{entry['id']}")}


def _penalty(state):
    return {"tight": 1, "too big": 2}.get(state, 0)


def pairs(table, entry, servers, agent=None, profiles=None):
    """Every chat model SERVERS have, as a pair for ENTRY, best first: the table's (each model with its best harness:
    its first recommendation; what may not load after what fits), then those the table does not have ("not
    measured", by their traffic light with PROFILES, their best harness by it), then those it marks unsafe for the
    use case (with the reason: never hidden, never the choice). Each says when AGENT is not its harness
    (agent_note). Models that cannot do what the use case needs (the server says so) and embedding models are left
    out."""
    mp = profiles_module()
    needs = entry.get("needs") or []
    measured, untested, unsafe = [], [], []
    for server in servers:
        for model in server["models"]:
            if embedding(model) or reads(model, needs) == "no":
                continue
            marked = unsafe_for(entry, model["name"])
            if marked is not None:
                unsafe.append(((_penalty(fit(model, server)["state"]), entry["unsafe"].index(marked), len(unsafe)),
                               _other_pair(table, entry, server, model, "unsafe", marked["harness"], unsafe=marked)))
                continue
            rank, found = next(((rank, found) for rank, found in enumerate(entry["recommendations"], 1)
                                if re.search(found["model"], model["name"], re.I)), (None, None))
            if found is not None:
                pair = _choice(table, entry, rank, found, server, model, None)
                pair["unsafe"] = None
                measured.append(((_penalty(pair["fit"]), rank, FIT_ORDER.index(pair["fit"]), len(measured)), pair))
                continue
            light = mp.support(model["name"], profiles) if profiles is not None else None
            harness = None
            if light is not None:
                harness = next(iter(light["by_light"].get("green") or light["by_light"].get("yellow") or []), None)
                if harness is None and light.get("status") in ("verified", "smoke-only", "partial"):
                    harness = agent or "claude"
            pair = _other_pair(table, entry, server, model, "not measured", harness or agent, light=light)
            order = mp.LIGHT_ORDER.index(light["light"]) if light is not None and light["light"] in mp.LIGHT_ORDER \
                else len(mp.LIGHT_ORDER)
            untested.append(((_penalty(pair["fit"]), order, len(untested)), pair))
    ranked = [pair for group in (measured, untested, unsafe) for _, pair in sorted(group, key=lambda item: item[0])]
    for pair in ranked:
        pair["agent_note"] = agent_note(entry, pair, agent, profiles)
        # (the same in parts, for a page that says it in its own language)
        pair["agent"] = agent if pair["agent_note"] else None
        pair["agent_name"] = harness_name(agent) if pair["agent_note"] else None
        pair["agent_status"] = _status_in(entry, pair["model"], agent, profiles) if pair["agent_note"] else None
        pair["in_agents"] = in_agents(entry, pair, profiles)  # (every other agent: a page that changes its agent)
    return ranked


def recommend(table, servers, name, agent=None, profiles=None, note_agent=None):
    """The recommendation for the use case NAME from the models SERVERS have (each {name, memory_bytes, answered,
    error, models: [{name, size, loaded, capabilities}] or names}): {use_case, label, about, status, status_text,
    tasks, needs, choice (the best tested choice that fits, else the nearest - a related use case's, else by the
    traffic light with PROFILES -, or None; {server, model, harness, rank, source: table | related | traffic light,
    basis, why, fails, evidence, round, date, settings, fit, fit_text, note, command ...}; never a model the use case
    marks unsafe), alternatives (other usable choices), missing (recommendations not available, with why), pairs
    (every model of the servers as a pair with its best harness, best first: pairs()), unsafe (the use case's unsafe
    pairs, as the table has them), fails, unknown, servers}. AGENT: the start's agent - its own harness first;
    NOTE_AGENT (default AGENT): the agent the pairs' notes compare with (a page that sets the agent from the pair
    chosen: the choice is not for its agent, the notes are)."""
    entry = use_case(table, name)
    servers = [server_entry(server) for server in servers or []]
    best, others, missing = _table_choice(table, entry, servers, agent)
    if best is None:
        best = _nearest(table, entry, servers, agent, profiles)
    if best is not None:
        best.setdefault("unsafe", None)
    found = status(entry)
    return {"use_case": entry["id"], "label": entry["label"], "about": entry["about"], "status": found,
            "status_text": STATUS_TEXT[found], "tasks": list(entry["tasks"]), "needs": list(entry.get("needs") or []),
            "choice": best, "alternatives": others, "missing": missing,
            "pairs": pairs(table, entry, servers, note_agent or agent, profiles),
            "unsafe": [{"label": u.get("label") or u["examples"][0], "harness": u["harness"],
                        "harness_name": harness_name(u["harness"]), "why": u["why"], "evidence": u["evidence"]}
                       for u in entry.get("unsafe") or []],
            "fails": list(entry.get("fails") or []),
            "unknown": list(entry.get("unknown") or []),
            "harness_names": dict(profiles_module().HARNESS_NAMES),  # (each agent's name: a pair's in_agents)
            "servers": [{"name": s["name"], "answered": s["answered"], "error": s["error"],
                         "memory_bytes": s["memory_bytes"], "models": len(s["models"])} for s in servers]}


def recommend_all(table, servers, agent=None, profiles=None, note_agent=None):
    """recommend() of every use case, in the table's order."""
    return [recommend(table, servers, entry["id"], agent, profiles, note_agent) for entry in table["use_cases"]]


def settings_for(table, name, model, agent):
    """The use case NAME's recommendation for MODEL in AGENT (the first that matches, with its "rank"), or None: its
    settings go over the model's profile at a start."""
    entry = use_case(table, name)
    for rank, found in enumerate(entry["recommendations"], 1):
        if found["harness"] == agent and model and re.search(found["model"], str(model), re.I):
            return {**found, "rank": rank}
    return None


def overlay(profile, found, case_id, case_label, agent=None, rank=None):
    """PROFILE (model_profiles.match's, or None: a model of no family) with the use case's recommendation FOUND over
    it: its params, loop_guard and env over the profile's, its tools in place of the profile's; "use_case" says which.
    A copy: the profile itself is not changed."""
    mp = profiles_module()
    marker = {"id": case_id, "label": case_label, "rank": found.get("rank", rank), "basis": found["basis"],
              "evidence": found.get("evidence"), "why": found["why"]}
    if profile is None:
        agent = agent or found["harness"]
        builtin = [text for _, text in mp.BUILTIN.get("*", ()) + mp.BUILTIN.get(agent, ())]
        return {"id": f"use case {case_id}/{agent}", "name": f"use case {case_id} × {agent}", "family": None,
                "label": found.get("label") or found["examples"][0], "pattern": found["model"], "agent": agent,
                "pair": False, "status": mp.UNTESTED, "status_text": mp.STATUS_TEXT[mp.UNTESTED],
                "light": "grey", "symbol": mp.SYMBOLS["grey"], "reason": None, "evidence": found.get("evidence"),
                "runs": found.get("runs"), "solved": found.get("solved"), "params": dict(found.get("params") or {}),
                "loop_guard": dict(found.get("loop_guard") or {}), "env": dict(found.get("env") or {}),
                "reasoning": None, "notes": list(found.get("notes") or []), "warnings": [], "mcp_max": None,
                "tools": found.get("tools"), "tool_class": None, "builtin": builtin, "works": [], "fails": list(found.get("fails") or []),
                "unknown": [], "use_case": marker}
    merged = dict(profile)
    merged["params"] = {**profile.get("params", {}), **(found.get("params") or {})}
    merged["loop_guard"] = {**profile.get("loop_guard", {}), **(found.get("loop_guard") or {})}
    merged["env"] = {**profile.get("env", {}), **(found.get("env") or {})}
    if found.get("tools") is not None:
        merged["tools"] = found["tools"]
    merged["use_case"] = marker
    return merged


# --- in words -------------------------------------------------------------------------------------------------------

def _size(value):
    return f"{value / GB:.1f} GB" if value else ""


def choice_line(choice):
    """"studio:mid-35b in Claude Code (rank 2, measured; fits: about 46 GB of 48 GB)"."""
    where = [f"rank {choice['rank']}" if choice["rank"] else choice["source"]]
    if choice["basis"]:
        where[0] += f", {choice['basis']}"
    if choice["related"]:
        where[0] += f" for {choice['related']}"
    return f"{choice['server']}:{choice['model']} in {choice['harness_name']} ({where[0]}; {choice['fit_text']})"


def pair_where(pair):
    """Where PAIR (one of pairs()) comes from in words: "rank 2, measured", "not measured", "UNSAFE"."""
    if pair["source"] == "table":
        return f"rank {pair['rank']}, {pair['basis']}"
    return "unsafe for this use case" if pair["source"] == "unsafe" else "not measured"


def pair_lines(pair, number=None):
    """PAIR in lines: "2. studio:mid-35b x OpenCode (rank 2, measured; fits: ...)", then why (an unsafe one: "⚠
    unsafe for this use case: REASON" with the evidence), the note on the start's agent, the command."""
    head = f"{number}. " if number else ""
    harness = pair["harness_name"] or "an agent of yours"
    out = [f"{head}{pair['server']}:{pair['model']} x {harness} ({pair_where(pair)}; {pair['fit_text']})"]
    if pair["unsafe"]:
        out.append(f"   ⚠ unsafe for this use case: {pair['unsafe']} ({pair['evidence']})")
    else:
        out.append(f"   why: {pair['why']}")
    if pair.get("agent_note"):
        out.append(f"   note: {pair['agent_note']}")
    return out


def lines(found):
    """FOUND (recommend()) in lines: the use case and its status, the choice with why, evidence, settings, known
    failures, a note and the command to start it; the other choices, what is not available and why, what fails and
    what is not known for the use case, servers that did not answer."""
    mp = profiles_module()
    out = [f"{found['use_case']}: {found['label']} ({found['status_text']})", f"  {found['about']}"]
    choice = found["choice"]
    if choice is None:
        out.append("  choice: none - " + ("nothing of this use case's table is on your servers" if
                                          found["status"] != "not tested" else "not tested yet, and no model of "
                                          "your servers has a traffic light to go by"))
    else:
        out.append(f"  choice: {choice_line(choice)}")
        out.append(f"    why: {choice['why']}")
        if choice["evidence"]:
            when = ", ".join(filter(None, [choice["round"], choice["date"]]))
            out.append(f"    evidence: {choice['evidence']}" + (f" ({when})" if when else ""))
        if choice["settings_text"]:
            out.append(f"    settings: {choice['settings_text']} (over the model's profile, under yours)")
        out += [f"    {mp.SIGNS['fails']} {text}" for text in choice["fails"]]
        if choice["note"]:
            out.append(f"    note: {choice['note']}")
        out.append(f"    start: {choice['command']}")
    for other in found["alternatives"]:
        out.append(f"  also: {choice_line(other)}")
    if found.get("pairs"):
        out.append("  pairs on your servers, best first (model x harness):")
        for number, pair in enumerate(found["pairs"], 1):
            out += [f"    {line}" for line in pair_lines(pair, number)]
    for entry in found["missing"]:
        out.append(f"  not available: {entry['rank']}. {entry['label']} in {entry['harness_name']}: {entry['reason']}")
    out += [f"  {mp.SIGNS['fails']} {text}" for text in found["fails"]]
    out += [f"  {mp.SIGNS['unknown']} {text}" for text in found["unknown"]]
    for server in found["servers"]:
        if not server["answered"]:
            out.append(f"  {server['name']} did not answer ({server['error'] or 'no reason given'}): its remembered "
                       "models were used")
    return out


def table_lines(found_list):
    """recommend_all()'s result as a table: USE CASE, STATUS, CHOICE (SERVER:MODEL), HARNESS, FIT."""
    rows = [("USE CASE", "STATUS", "CHOICE", "HARNESS", "FIT")]
    for found in found_list:
        choice = found["choice"]
        if choice is None:
            rows.append((found["use_case"], found["status"], "-", "-", "-"))
            continue
        shown = f"{choice['server']}:{choice['model']}"
        if choice["source"] != "table":
            shown += f" ({'nearest: ' + choice['related'] if choice['related'] else 'nearest'})"
        rows.append((found["use_case"], found["status"], shown, choice["harness_name"], choice["fit"]))
    widths = [max(len(row[i]) for row in rows) for i in range(4)]
    return ["  ".join([*(row[i].ljust(widths[i]) for i in range(4)), row[4]]).rstrip() for row in rows]
