"""Model x harness profiles: the settings a local model works best with in each agent, applied at a start.

sandbox-ai's local-model research (docs/LOCAL_MODELS.md) measured which sampling, context window, turn limit and
per-harness switches make a model work in each harness. model_profiles.json keeps the result as a reviewed table:
per model family (a regular expression on the model's name, case-insensitive; the first family that matches wins)
the settings for every harness ("all") and per harness the measured status, the evidence and that pair's own
settings over them. A start with --llm NAME:MODEL and agent A looks up the family of MODEL and the entry of A
(llm.selection marks the endpoint with the start's agent) and applies the profile UNDER everything you set yourself:

    the profile < the endpoint's own values (llm set) < the start's --llm-param < explicit options (--env ...)

It says so at the start ("profile qwen3-coder-next x opencode (verified): ..."), shows it in --dry-run's
llm.profile (applied keys, overridden keys), and --no-llm-profile (a project file's options, the Board's switch)
switches it off. A pair known not to work is said as a WARNING, never refused (sandbox-ai's rule: no experimental
gates).

Each setting maps to a mechanism sandbox-ai already has (MECHANISMS; tests/test_model_profiles.py checks every key):
  params      the endpoint's parameters (llm.PARAMS): the sampling ones as the connection proxy's lowest default for
              the model's requests (llm.proxy_params "presets"), max_tokens a ceiling, num_ctx the context window
              the agents are told (at most what the server has the model loaded with or reports) and LM Studio's
              load (llm.keep_loaded, at most the model's maximum)
  loop_guard  the loop guard's layers (llm.LOOP_DEFAULTS) over sandbox-ai's defaults, under yours (defaults.json),
              the endpoint's and the start's (llm.loop_config); a profile never switches the guard off
  env         a harness's variables (ENV): Claude Code's CLAUDE_CODE_DISABLE_THINKING (llm.claude_environment)
  mcp_max     a warning when the start attaches more MCP servers than the model copes with (Laguna)
  tools       Claude Code's own tools the connection proxy keeps in its requests (llm.start_tools): "all", "core"
              (sandbox-ai's default for every local model) or a list of tool names ("core" among them adds the core
              ones); the start's --llm-tools wins
What sandbox-ai sets for every local model anyway (BUILTIN: Claude Code's tool search off and window, the waits for
a slow server, LM Studio's parallel 1 ...) is listed with a profile, never set twice.

MCP tools against the model's class (TOOL_CLASSES, information only, nothing applied): the table's "tool_budgets"
gives each class - small, mid, large - the MCP tools a model of it copes with ("tools"), the parameter count below
which a model is of it ("params_below", in billions; the largest class has none) and the research's evidence; a
family's "all" may name its class ("tool_class"); a model of no class is classed by the parameter count in its name
(params_of: "gemma-4-12b-it" 12B), a model without either has no budget. tool_budget() says it; mcp_budget.py
counts a start's MCP tools against it (a warning when they are more, never a refusal).

A status comes only from measured runs: a pair's status is derived from the research's graded runs
(model_evidence.py's RULES, written by scripts/profile_status.py with the numbers and the run ids into the pair's
"evidence" block, the same numbers in model_evidence.json; a hand-written evidence text stays as the block's
"summary"). A family has no status of its own, and a pair without that block shows none: a harness without its own
measured pair is grey ("no data yet in OpenCode") whatever the model did in other harnesses, and a model's light is
the best of its measured harnesses only (the user, 2026-11-22: "it says opencode verified" for a model never run in
OpenCode). scripts/profile_status.py --check and tests/test_profile_status.py hold every status to the snapshot.
evidence_text() says the block in words, evidence_short() in a few ("49 runs, 13 use cases, rounds 4-6") where a
status is shown.

What we know (KNOWLEDGE, information only, nothing applied): a family's "all" and a pair's entry may list short bullets
of what was verified to work ("works"), what is known not to work ("fails") and what is not known yet ("unknown"); a
profile has the pair's first, then the family's. `llm profiles MODEL` and the app's "What we know" box show them.

Standard library only; Python 3.9.
"""
import json
import os
from pathlib import Path
import re
import threading

HERE = Path(__file__).resolve().parent
TABLE = HERE / "model_profiles.json"
SCHEMA = 1
TABLE_LIMIT = 1 << 20
# The research's statuses (profiles/index.json) and one of this table's own: known-bad, a pair found not to work, with
# the reason. "untested": no measured pair (no data yet), the family's settings for every harness apply.
STATUSES = ("verified", "smoke-only", "partial", "failing", "known-bad")
WARNING_STATUSES = ("failing", "known-bad")
UNTESTED = "untested"
# What a status means: derived from the research's graded runs (model_evidence.RULES, scripts/profile_status.py;
# docs/LOCAL_MODELS.md "How a status is derived"); a pair without measured runs has none (UNTESTED: no data yet).
STATUS_TEXT = {"verified": "verified: at least 10 graded runs over 5 use cases incl. the sandbox's own (1a-1c), mean "
                           "at least 0.85, no use case below 0.50, no canary",
               "smoke-only": "only the smoke test ran",
               "partial": "something passed, short of verified (the evidence says what is missing)",
               "failing": "nothing passed",
               "known-bad": "known not to work",
               UNTESTED: "no data yet for this harness: the model's settings for every harness apply"}
# The launcher's agents (llm.AGENTS; a test keeps both the same).
HARNESSES = ("claude", "codex", "opencode", "opencode2", "hermes", "dsh", "qwen", "goose", "openhands", "nanobot",
             "letta", "pi")
# A harness's variables a profile may set, each with what it does (applied by llm.agent_environment).
ENV = {"claude": {"CLAUDE_CODE_DISABLE_THINKING": "Claude Code sends no thinking request (the model still reasons; "
                                                  "the server does not return it)"}}
MECHANISMS = {
    "params": "the connection proxy's lowest default for the model's requests (llm.proxy_params presets), under the "
              "endpoint's and the start's values",
    "num_ctx": "the context window the agents are told, at most what the server has loaded or reports "
               "(llm.agent_context) and what Ollama serves (llm.start_context), and LM Studio's load "
               "(llm.keep_loaded)",
    "loop_guard": "the loop guard's layers under yours, the endpoint's and the start's (llm.loop_config)",
    "env": "the harness's variables, under your --env (llm.agent_environment)",
    "mcp_max": "a warning at the start when more MCP servers are attached",
    "tools": "Claude Code's own tools the connection proxy keeps in its requests (llm.start_tools), under the start's "
             "--llm-tools",
}
# What sandbox-ai sets itself for every --llm start of a harness (llm's name for it, in words): shown with a profile.
BUILTIN = {
    "*": (("lmstudio_load_body", "LM Studio loads the model with parallel 1 (its prompt cache stays usable)"),),
    "claude": (("CLAUDE_CORE_TOOLS", "the tools local models never used left out of its requests (--llm-tools core: "
                                     "Bash, Read, Edit, Write, Glob, Grep, TodoWrite and every MCP tool stay)"),
               ("CLAUDE_TOOL_SEARCH", "ENABLE_TOOL_SEARCH=false: the MCP servers' tools offered as they are"),
               ("CLAUDE_WINDOW", "CLAUDE_CODE_MAX_CONTEXT_TOKENS and CLAUDE_CODE_AUTO_COMPACT_WINDOW: the window"),
               ("CLAUDE_WAITS", "the waits for a slow server")),
    "codex": (("LOCAL_WAIT", "stream_idle_timeout_ms: an hour for a server of your own"),),
    "opencode": (("LOCAL_WAIT", "the provider's headerTimeout and chunkTimeout: an hour"),
                 ("OPENCODE_TOOL_OUTPUT", "tool output cut (tool_output)")),
    "opencode2": (("LOCAL_WAIT", "the provider's headerTimeout and chunkTimeout: an hour"),),
    "qwen": (("QWEN_MEMORY", "the managed auto-memory off (one request less after every turn)"),
             ("QWEN_WAITS", "the request and stream timeouts: an hour")),
    "goose": (("GOOSE_WAITS", "OPENAI_TIMEOUT and GOOSE_STREAM_TIMEOUT: an hour"),),
    "hermes": (("HERMES_MIN_CONTEXT", "a window of at least 64,000 tokens (Hermes refuses less)"),),
}
# What we know of a family or a pair, each a list of short bullets, with the sign it is shown with.
KNOWLEDGE = ("works", "fails", "unknown")
SIGNS = {"works": "✓", "fails": "✗", "unknown": "?"}
BULLET_LIMIT = 120
BULLETS_MAX = 12
FAMILY_KEYS = {"id", "label", "pattern", "examples", "all", "harnesses"}
ALL_KEYS = {"params", "loop_guard", "reasoning", "notes", "warnings", "mcp_max", "tools", "speed",
            "tool_class", *KNOWLEDGE}
PAIR_KEYS = {"status", "reason", "evidence", "runs", "solved", "params", "loop_guard", "env", "notes", "warnings",
             "mcp_max", "tools", "speed", "round", "date", *KNOWLEDGE}
TABLE_KEYS = {"schema", "source", "rounds", "families", "tool_budgets"}
# A pair's evidence: a text (by hand), or the block scripts/profile_status.py derives (model_evidence.block): what
# derived it, the rule that gave the status, the hand-written text it had (summary), the numbers, a warning.
EVIDENCE_TEXTS = ("derived", "rule", "summary", "warning", "run_ids", "last_round", "date")
EVIDENCE_COUNTS = ("runs", "passed", "smoke_runs", "use_case_runs", "canary", "injection_runs",
                   "contaminated_excluded", "experiments_excluded")
EVIDENCE_LISTS = ("use_cases", "legacy_tasks", "family_canary", "rounds")
EVIDENCE_KEYS = {*EVIDENCE_TEXTS, *EVIDENCE_COUNTS, *EVIDENCE_LISTS, "tasks", "mean", "min", "use_case_low"}
# The classes of models by size, from the smallest: the MCP tools each copes with (the table's "tool_budgets").
TOOL_CLASSES = ("small", "mid", "large")
BUDGET_KEYS = {"tools", "params_below", "evidence", "short"}
# A parameter count in a model's name: "12b", "1.5b", "350m", "8x7b" (experts x size); never the active parameters of
# a mixture of experts ("a4b", after a letter) nor a version ("v3") or a context length ("128k").
PARAMS = re.compile(r"(?<![a-z0-9.])(?:(\d{1,3})x)?(\d{1,4}(?:\.\d{1,2})?)([bm])(?![a-z0-9])")
# The traffic light: a status's colour, each colour's symbol (a shape of its own: readable without the colour) and
# words, the colours from best to worst (a model's overall colour is its best measured harness's).
LIGHTS = {"verified": "green", "smoke-only": "yellow", "partial": "yellow", "failing": "red", "known-bad": "red",
          UNTESTED: "grey"}
LIGHT_ORDER = ("green", "yellow", "red", "grey")
SYMBOLS = {"green": "✓", "yellow": "~", "red": "✗", "grey": "?"}
LIGHT_WORDS = {"green": "verified", "yellow": "partly working", "red": "does not work", "grey": "no data yet"}
NO_DATA = "no data yet"
HARNESS_NAMES = {"claude": "Claude Code", "codex": "Codex", "opencode": "OpenCode", "opencode2": "OpenCode 2",
                 "hermes": "Hermes", "dsh": "DeepSeek Harness", "qwen": "Qwen Code", "goose": "Goose",
                 "openhands": "OpenHands", "nanobot": "nanobot", "letta": "Letta Code", "pi": "Pi"}
ROUND = re.compile(r"\b(phase \d+|round \d+)\b", re.I)
ROUND_NUMBER = re.compile(r"round (\d+)")
SPEED = re.compile(r"tokens/s|\bt/s\b")
TOOL = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}")
ID = re.compile(r"[a-z0-9][a-z0-9.-]{0,39}")
TEXT_LIMIT = 600
LOCK = threading.Lock()
_loaded = {}


def _text(value, where, empty=False):
    if not isinstance(value, str) or (not value.strip() and not empty) or len(value) > TEXT_LIMIT or \
            any(not c.isprintable() for c in value):
        raise ValueError(f"{where}: a line of printable text (at most {TEXT_LIMIT} characters)")
    return value


def _texts(value, where):
    if not isinstance(value, list) or len(value) > 20:
        raise ValueError(f"{where}: a list of texts")
    return [_text(item, f"{where}[{index}]") for index, item in enumerate(value)]


def _bullets(value, where):
    """A list of at most BULLETS_MAX short texts (one printable line of at most BULLET_LIMIT characters each)."""
    if not isinstance(value, list) or len(value) > BULLETS_MAX:
        raise ValueError(f"{where}: a list of short texts (at most {BULLETS_MAX})")
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip() or len(item) > BULLET_LIMIT or \
                any(not c.isprintable() for c in item):
            raise ValueError(f"{where}[{index}]: one line of printable text (at most {BULLET_LIMIT} characters)")
    return value


def _tools(value, where):
    """Claude Code's tools a profile keeps: "all", "core" or a list of 1-64 tool names ("core" may be one of them)."""
    if value in ("all", "core"):
        return value
    if not isinstance(value, list) or not 0 < len(value) <= 64 or \
            not all(isinstance(name, str) and name != "all" and (name == "core" or TOOL.fullmatch(name))
                    for name in value):
        raise ValueError(f"{where}: all, core or a list of Claude Code's tool names (core may be one of them)")
    return value


def _count(value, where, low=0, high=1000):
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise ValueError(f"{where}: a whole number from {low} to {high}")
    return value


def check_settings(entry, where, keys, agent=None, check_param=None, check_layer=None):
    """One family's "all" or one harness's entry (ENTRY), checked: known keys, a known status (a known-bad or
    failing one with its reason), the parameters and layers sandbox-ai takes (CHECK_PARAM(key, value),
    CHECK_LAYER(key, value): llm's, ValueError for one it does not), only the variables ENV allows for AGENT."""
    if not isinstance(entry, dict) or set(entry) - keys:
        raise ValueError(f"{where}: an object of {', '.join(sorted(keys))}")
    status = entry.get("status")
    if status is not None and status not in STATUSES:
        raise ValueError(f"{where}: status is one of {', '.join(STATUSES)}")
    if status in WARNING_STATUSES and "reason" not in entry:
        raise ValueError(f"{where}: a {status} entry says why (reason)")
    if keys is PAIR_KEYS and status is None:
        raise ValueError(f"{where}: a harness's entry has a status")
    for key in ("reason", "reasoning", "speed", "round", "date"):
        if key in entry:
            _text(entry[key], f"{where}: {key}")
    if "evidence" in entry:
        check_evidence(entry["evidence"], f"{where}: evidence")
    for key in ("notes", "warnings"):
        if key in entry:
            _texts(entry[key], f"{where}: {key}")
    for key in KNOWLEDGE:
        if key in entry:
            _bullets(entry[key], f"{where}: {key}")
    for key in ("runs", "solved"):
        if key in entry:
            _count(entry[key], f"{where}: {key}")
    if "mcp_max" in entry:
        _count(entry["mcp_max"], f"{where}: mcp_max", 0, 100)
    if "tools" in entry:
        _tools(entry["tools"], f"{where}: tools")
    if "tool_class" in entry and entry["tool_class"] not in TOOL_CLASSES:
        raise ValueError(f"{where}: tool_class is {', '.join(TOOL_CLASSES[:-1])} or {TOOL_CLASSES[-1]}")
    params = entry.get("params", {})
    if not isinstance(params, dict):
        raise ValueError(f"{where}: params is an object of parameters")
    for key, value in params.items():
        if not isinstance(key, str) or not re.fullmatch(r"[a-z_]{1,40}", key) or isinstance(value, (dict, list)) \
                or value is None:
            raise ValueError(f"{where}: params {key!r} is not a parameter")
        try:
            if check_param is not None and check_param(key, value) != value:
                raise ValueError("not a value sandbox-ai stores")
        except ValueError as exc:
            raise ValueError(f"{where}: params {key}: {exc}") from None
    loop = entry.get("loop_guard", {})
    if not isinstance(loop, dict) or "enabled" in loop:
        raise ValueError(f"{where}: loop_guard is an object of layers (never enabled: a profile does not switch the "
                         "guard off)")
    for key, value in loop.items():
        try:
            if not isinstance(key, str) or not isinstance(value, (bool, int)) or \
                    (check_layer is not None and check_layer(key, value) != value):
                raise ValueError("not a layer's value")
        except ValueError as exc:
            raise ValueError(f"{where}: loop_guard {key!r}: {exc}") from None
    env = entry.get("env", {})
    allowed = ENV.get(agent, {})
    if not isinstance(env, dict) or any(name not in allowed or not isinstance(value, str) or
                                        not re.fullmatch(r"[A-Za-z0-9_.-]{0,100}", value)
                                        for name, value in env.items()):
        raise ValueError(f"{where}: env sets only {', '.join(allowed) or 'nothing'} for {agent or 'every harness'}")
    return entry


def _share(value, where):
    if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1):
        raise ValueError(f"{where}: a score from 0 to 1 (or null)")
    return value


def check_evidence(value, where):
    """A pair's evidence: a line of text, or a derived block of EVIDENCE_KEYS (texts, counts, lists of short texts,
    tasks {passed, total}, mean and min 0..1 or null, use_case_low {use_case, mean} or null)."""
    if isinstance(value, str):
        return _text(value, where)
    if not isinstance(value, dict) or set(value) - EVIDENCE_KEYS or "rule" not in value:
        raise ValueError(f"{where}: evidence is a text or an object of {', '.join(sorted(EVIDENCE_KEYS))} (with rule)")
    for key in EVIDENCE_TEXTS:
        if value.get(key) is not None:
            _text(value[key], f"{where} {key}")
    for key in EVIDENCE_COUNTS:
        if key in value:
            _count(value[key], f"{where} {key}", 0, 100000)
    for key in EVIDENCE_LISTS:
        if key in value and (not isinstance(value[key], list) or len(value[key]) > 64 or
                             not all(isinstance(item, str) and 0 < len(item) <= 40 and item.isprintable()
                                     for item in value[key])):
            raise ValueError(f"{where} {key}: a list of short texts")
    tasks = value.get("tasks")
    if tasks is not None and (not isinstance(tasks, dict) or set(tasks) != {"passed", "total"} or
                              _count(tasks["passed"], f"{where} tasks", 0, 100000) >
                              _count(tasks["total"], f"{where} tasks", 0, 100000)):
        raise ValueError(f"{where} tasks: {{passed, total}}, passed at most total")
    for key in ("mean", "min"):
        _share(value.get(key), f"{where} {key}")
    low = value.get("use_case_low")
    if low is not None:
        if not isinstance(low, dict) or set(low) != {"use_case", "mean"}:
            raise ValueError(f"{where} use_case_low: {{use_case, mean}} or null")
        _text(low["use_case"], f"{where} use_case_low")
        _share(low["mean"], f"{where} use_case_low")
    if "passed" in value and "runs" in value and value["passed"] > value["runs"]:
        raise ValueError(f"{where} passed: at most runs")
    return value


def _rounds_words(rounds):
    """"round 5", "rounds 4-6", "phase 2, rounds 1-6", "rounds 4, 6" (model_evidence.rounds_words)."""
    phases = [name for name in rounds if not ROUND_NUMBER.fullmatch(name)]
    numbers = sorted(int(ROUND_NUMBER.fullmatch(name).group(1)) for name in rounds if ROUND_NUMBER.fullmatch(name))
    parts = list(phases)
    if len(numbers) == 1:
        parts.append(f"round {numbers[0]}")
    elif numbers and numbers == list(range(numbers[0], numbers[-1] + 1)):
        parts.append(f"rounds {numbers[0]}-{numbers[-1]}")
    elif numbers:
        parts.append("rounds " + ", ".join(map(str, numbers)))
    return ", ".join(parts)


def _block(entry):
    evidence = (entry or {}).get("evidence")
    return evidence if isinstance(evidence, dict) else None


def measured(entry):
    """Whether the pair ENTRY's status comes from measured runs: a status with the derived evidence block
    (scripts/profile_status.py's numbers). A status without it is shown as none (grey: no data yet)."""
    return bool(entry) and entry.get("status") in STATUSES and _block(entry) is not None


def status_of(entry):
    """The status a pair ENTRY (None: none) is shown with: its own when it was measured, else UNTESTED."""
    return entry["status"] if measured(entry) else UNTESTED


def evidence_short(entry):
    """A derived pair's evidence in a few words, for where its status is shown: "13 runs, 5 use cases, rounds 4-6"
    (None: a hand-written evidence or none)."""
    found = _block(entry)
    if found is None:
        return None
    cases = len(found.get("use_cases") or [])
    runs = found.get("runs", 0)
    parts = [f"{runs} run{'s' if runs != 1 else ''}"]
    if cases:
        parts.append(f"{cases} use case{'s' if cases != 1 else ''}")
    return ", ".join(parts + [_rounds_words(found.get("rounds") or [])]).rstrip(", ")


def evidence_counts(entry):
    """A derived pair's {"runs", "use_cases" (how many), "rounds" (in words)} for an app to say in its own words
    (None: a hand-written evidence or none)."""
    found = _block(entry)
    if found is None:
        return None
    return {"runs": found.get("runs", 0), "use_cases": len(found.get("use_cases") or []),
            "rounds": _rounds_words(found.get("rounds") or [])}


def evidence_text(entry):
    """A pair's evidence in words: the hand-written text, or the derived block's numbers, the rule and the summary."""
    evidence = (entry or {}).get("evidence")
    if not isinstance(evidence, dict):
        return evidence
    cases = evidence.get("use_cases") or []
    text = f"{evidence.get('runs', 0)} graded runs ({evidence.get('passed', 0)} passed)"
    if cases:
        text += f", {len(cases)} use cases ({', '.join(cases)})"
    text += f", {_rounds_words(evidence.get('rounds') or [])}"
    if evidence.get("mean") is not None:
        text += f"; mean {evidence['mean']:.2f}"
        low = evidence.get("use_case_low")
        if low:
            text += f", lowest use case {low['use_case']} {low['mean']:.2f}"
    if evidence.get("canary"):
        text += f"; canary in {evidence['canary']} of {evidence.get('injection_runs', 0)} injection runs"
    if evidence.get("legacy_tasks"):
        text += f"; rounds 1-4's tasks: {', '.join(evidence['legacy_tasks'])}"
    left = [f"{evidence[key]} {word}" for key, word in (("contaminated_excluded", "contaminated"),
                                                         ("experiments_excluded", "experiments"))
            if evidence.get(key)]
    if left:
        text += f"; left out: {', '.join(left)}"
    text += f". {evidence['rule']}"
    if evidence.get("summary"):
        text += f". By hand: {evidence['summary']}"
    return text


def check_table(document, check_param=None, check_layer=None):
    """The table DOCUMENT (model_profiles.json), checked whole; ValueError names the place of the first fault."""
    if not isinstance(document, dict) or document.get("schema") != SCHEMA or \
            set(document) - TABLE_KEYS or not isinstance(document.get("families"), list):
        raise ValueError(f"model profiles: schema {SCHEMA} with source, rounds and families")
    if "source" in document:
        _text(document["source"], "model profiles: source")
    rounds = document.get("rounds", {})
    if not isinstance(rounds, dict) or len(rounds) > 100 or not all(
            isinstance(name, str) and 0 < len(name) <= 40 and name.isprintable() and name.strip() and
            isinstance(date, str) and 0 < len(date) <= 40 and date.isprintable() for name, date in rounds.items()):
        raise ValueError("model profiles: rounds maps a round to its date (short texts: \"round 1\": \"2026-11-16\")")
    check_budgets(document.get("tool_budgets"))
    seen = set()
    for index, family in enumerate(document["families"]):
        where = f"model profiles: family {index}"
        if not isinstance(family, dict) or set(family) - FAMILY_KEYS or \
                not {"id", "pattern", "examples", "all"} <= set(family):
            raise ValueError(f"{where}: an object of {', '.join(sorted(FAMILY_KEYS))}")
        if not isinstance(family["id"], str) or not ID.fullmatch(family["id"]) or family["id"] in seen:
            raise ValueError(f"{where}: id is a new short name (a-z, 0-9, ., -)")
        seen.add(family["id"])
        where = f"model profiles: {family['id']}"
        _text(family.get("label", family["id"]), f"{where}: label")
        try:
            pattern = re.compile(_text(family["pattern"], f"{where}: pattern"), re.I)
        except re.error as exc:
            raise ValueError(f"{where}: pattern is no regular expression ({exc})") from None
        examples = _texts(family.get("examples"), f"{where}: examples")
        if not examples:
            raise ValueError(f"{where}: examples names at least one model")
        for example in examples:
            earlier = family_of(example, {"families": document["families"][:index]})
            if not pattern.search(example) or earlier is not None:
                raise ValueError(f"{where}: the example {example!r} does not match the pattern" if earlier is None
                                 else f"{where}: the example {example!r} matches {earlier['id']} before it")
        if isinstance(family["all"], dict) and {"status", "reason"} & set(family["all"]):
            raise ValueError(f"{where}: all has no status (or its reason): a status is a measured pair's, from its "
                             "runs (scripts/profile_status.py); what is known of the model goes into works/fails")
        check_settings(family["all"], f"{where}: all", ALL_KEYS, None, check_param, check_layer)
        harnesses = family.get("harnesses", {})
        if not isinstance(harnesses, dict):
            raise ValueError(f"{where}: harnesses maps a harness to its entry")
        for agent, entry in harnesses.items():
            if agent not in HARNESSES:
                raise ValueError(f"{where}: {agent!r} is not a harness ({', '.join(HARNESSES)})")
            check_settings(entry, f"{where} x {agent}", PAIR_KEYS, agent, check_param, check_layer)
    return document


def check_budgets(budgets):
    """The table's "tool_budgets" (None: none), checked: some of TOOL_CLASSES, each {"tools", "params_below" (not
    the largest class), "evidence", "short" (the evidence in a few words, for a warning)}, the budgets and
    params_below growing from class to class."""
    if budgets is None:
        return None
    where = "model profiles: tool_budgets"
    if not isinstance(budgets, dict) or not budgets or set(budgets) - set(TOOL_CLASSES):
        raise ValueError(f"{where}: maps small, mid and large to their budgets")
    tools = below = None
    for name in TOOL_CLASSES:
        entry = budgets.get(name)
        if entry is None:
            continue
        if not isinstance(entry, dict) or set(entry) - BUDGET_KEYS or not {"tools", "evidence"} <= set(entry):
            raise ValueError(f"{where}: {name} is an object of tools, params_below, evidence and short")
        _count(entry["tools"], f"{where}: {name} tools", 0, 1000)
        _text(entry["evidence"], f"{where}: {name} evidence")
        if "short" in entry:
            _bullets([entry["short"]], f"{where}: {name} short")
        limit = entry.get("params_below")
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, (int, float)) or
                                  not 0 < limit <= 100000):
            raise ValueError(f"{where}: {name} params_below is a parameter count in billions")
        if (tools is not None and entry["tools"] < tools) or \
                (below is not None and limit is not None and limit <= below):
            raise ValueError(f"{where}: the budgets and params_below grow from class to class")
        tools, below = entry["tools"], limit if limit is not None else below
    return budgets


def params_of(model):
    """The parameter count (in billions) MODEL's name gives - "gemma-4-12b-it" 12, "mixtral-8x7b" 56, "350m" 0.35 -
    or None."""
    found = PARAMS.search(str(model or "").lower())
    if found is None:
        return None
    experts, size, unit = found.groups()
    value = float(size) * (int(experts) if experts else 1) / (1000 if unit == "m" else 1)
    return int(value) if value == int(value) else round(value, 3)


def tool_budget(model, table):
    """The MCP tools MODEL copes with by TABLE's "tool_budgets" ({"class", "tools", "evidence", "from": "family" or
    "name", "family": its id or None, "params": the count in its name or None}), or None: no budgets, or a model of
    neither a family with a tool_class nor a parameter count in its name (a cloud model: no warning). "short":
    the class's evidence in a few words."""
    budgets = (table or {}).get("tool_budgets") if isinstance(table, dict) else None
    if not budgets or not model:
        return None
    family = family_of(model, table)
    params = params_of(model)
    name = (family or {}).get("all", {}).get("tool_class")
    source = "family"
    if name is None and params is not None:
        source = "name"
        for candidate in TOOL_CLASSES:
            if candidate in budgets and ("params_below" not in budgets[candidate] or
                                         params < budgets[candidate]["params_below"]):
                name = candidate
                break
    if name not in budgets:
        return None
    return {"class": name, "tools": budgets[name]["tools"], "evidence": budgets[name]["evidence"],
            "short": budgets[name].get("short"), "from": source, "family": family["id"] if family else None,
            "params": params}


def load(path=None, check_param=None, check_layer=None):
    """The table at PATH (default: model_profiles.json beside this module), checked; read again when the file
    changed. ValueError (or OSError) when it cannot be used."""
    path = Path(path or os.environ.get("SANDBOX_AI_MODEL_PROFILES") or TABLE)
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


def family_of(model, table):
    """The first family of TABLE whose pattern MODEL's name matches (case-insensitive), or None."""
    for family in table["families"]:
        if model and re.search(family["pattern"], str(model), re.I):
            return family
    return None


def families_of(query, table):
    """The families a user's QUERY names: the one whose pattern matches it as a model name (as family_of), else the
    one whose id it is, else every family whose id, label or examples contain it (case-insensitive)."""
    family = family_of(query, table)
    if family is not None:
        return [family]
    word = str(query or "").strip().lower()
    if not word:
        return []
    exact = [f for f in table["families"] if f["id"].lower() == word]
    if exact:
        return exact
    return [f for f in table["families"]
            if any(word in str(text).lower() for text in (f["id"], f.get("label", ""), *f.get("examples", [])))]


def _topic(text):
    """A bullet's topic: its text before the first ':', lower-case (the whole text without one)."""
    return str(text).split(":", 1)[0].strip().lower()


def _known(*entries):
    """{works, fails, unknown} of ENTRIES (the pair's, then the family's "all"), each bullet once, in their order;
    a later entry's bullet is left out when an earlier entry has one on its topic (same text before the first ':')."""
    found = {key: [] for key in KNOWLEDGE}
    taken = set()  # topics an earlier entry has a bullet on (under any sign)
    for entry in entries:
        for key in KNOWLEDGE:
            for text in entry.get(key, []):
                if text not in found[key] and _topic(text) not in taken:
                    found[key].append(text)
        taken |= {_topic(text) for key in KNOWLEDGE for text in entry.get(key, [])}
    return found


def knowledge_lines(profile):
    """What we know of PROFILE, one line per bullet with its sign: "✓ smoke test passed", "✗ ...", "? ..."."""
    return [f"{SIGNS[key]} {text}" for key in KNOWLEDGE for text in profile.get(key) or []]


def match(model, agent, table):
    """The profile for MODEL in the harness AGENT ({id, name, family, label, agent, pair, status, reason, evidence,
    runs, solved, params, loop_guard, env, reasoning, notes, warnings, mcp_max, tools, tool_class, builtin, works,
    fails, unknown}), or None for a model of no family. The family's settings for every harness, the pair's own over them;
    its status is the pair's own measured one (measured_pair), else "untested" (no data yet in that harness)."""
    return _profile(family_of(model, table), agent, table)


def _profile(family, agent, table):
    """match() for a known FAMILY."""
    if family is None or agent not in HARNESSES:
        return None
    every, pair = family["all"], family.get("harnesses", {}).get(agent)
    own = pair or {}
    # Only this pair's own measured runs give it a status: never the family's, never another harness's.
    status = status_of(pair)
    reason = own.get("reason") if status != UNTESTED else None
    warnings = [own["reason"]] if status in WARNING_STATUSES and own.get("reason") else []
    warnings += list(every.get("warnings", [])) + list(own.get("warnings", []))
    if (_block(own) or {}).get("warning"):  # a derived pair of a model whose injection canary fired
        warnings.append(_block(own)["warning"])
    builtin = [text for name, text in BUILTIN.get("*", ()) + BUILTIN.get(agent, ())]
    return {"id": f"{family['id']}/{agent}", "name": f"{family['id']} × {agent}", "family": family["id"],
            "label": family.get("label", family["id"]), "pattern": family["pattern"], "agent": agent,
            "pair": pair is not None, "measured_pair": status != UNTESTED, "status": status,
            "status_text": STATUS_TEXT[status],
            "light": light(status), "symbol": SYMBOLS[light(status)], "reason": reason,
            "evidence": evidence_text(own), "measured": evidence_short(own),
            "measured_counts": evidence_counts(own), "runs": own.get("runs"),
            "solved": own.get("solved"), "params": {**every.get("params", {}), **own.get("params", {})},
            "loop_guard": {**every.get("loop_guard", {}), **own.get("loop_guard", {})},
            "env": dict(own.get("env", {})), "reasoning": own.get("reasoning") or every.get("reasoning"),
            "notes": list(every.get("notes", [])) + list(own.get("notes", [])), "warnings": warnings,
            "mcp_max": own.get("mcp_max", every.get("mcp_max")), "tools": own.get("tools", every.get("tools")),
            "tool_class": every.get("tool_class"), "builtin": builtin, **_known(own, every)}


def settings(profile):
    """{key: value} of everything PROFILE sets: its parameters (KEY), layers (loop_guard.LAYER) and variables
    (env.NAME)."""
    found = dict(profile["params"])
    found.update({f"loop_guard.{key}": value for key, value in profile["loop_guard"].items()})
    found.update({f"env.{key}": value for key, value in profile["env"].items()})
    if profile.get("tools") is not None and profile.get("agent") in ("claude", "*"):
        found["tools"] = profile["tools"]
    return found


def value_text(key, value):
    shown = "true" if value is True else "false" if value is False else \
        ",".join(value) if isinstance(value, list) else str(value)
    if key == "num_ctx":
        return f"context {shown}"
    return f"{key[4:] if key.startswith('env.') else key}={shown}" if key.startswith("env.") else f"{key} {shown}"


def values_text(values):
    """{key: value} of settings() in one line: temperature 0.7, top_p 0.8, context 131072, loop_guard.steps 150."""
    return ", ".join(value_text(key, value) for key, value in values.items())


def headline(profile):
    """"profile qwen3-coder-next x opencode (verified)"; "(no data yet in OpenCode)" for a pair without measured
    runs (the family's settings)."""
    if profile["status"] == UNTESTED:
        return f"profile {profile['name']} ({NO_DATA} in {HARNESS_NAMES.get(profile['agent'], profile['agent'])})"
    return f"profile {profile['name']} ({profile['status'].replace('-', ' ')})"


def rows(table, model=None, agent=None):
    """`llm profiles`: [profile] of every family's every harness entry (with each family's "all" as harness "*"),
    or MODEL's (in AGENT, else in every harness that has an entry, else every harness)."""
    found = []
    families = table["families"] if model is None else families_of(model, table)
    for family in families:
        agents = [agent] if agent else list(family.get("harnesses", {})) or \
            ([] if model is None else list(HARNESSES))
        if model is None and not agent:
            every = family["all"]
            found.append({"id": f"{family['id']}/*", "name": f"{family['id']} × *", "family": family["id"],
                          "label": family.get("label", family["id"]), "pattern": family["pattern"], "agent": "*",
                          "pair": False, "measured_pair": False, "status": UNTESTED,
                          "status_text": STATUS_TEXT[UNTESTED], "light": "grey", "symbol": SYMBOLS["grey"],
                          "reason": None,
                          "evidence": None, "measured": None, "measured_counts": None, "runs": None, "solved": None,
                          "params": dict(every.get("params", {})),
                          "loop_guard": dict(every.get("loop_guard", {})), "env": {},
                          "reasoning": every.get("reasoning"), "notes": list(every.get("notes", [])),
                          "warnings": list(every.get("warnings", [])), "mcp_max": every.get("mcp_max"),
                          "tools": every.get("tools"), "tool_class": every.get("tool_class"), "builtin": [],
                          **_known(every)})
        for name in agents:
            found.append(_profile(family, name, table))
    return [row for row in found if row is not None]


# --- the traffic light: how far a model is supported, per harness (support) ------------------------------------------

def light(status):
    """The colour of STATUS: green, yellow, red, or grey (no data yet, or a status this module does not know)."""
    return LIGHTS.get(status, "grey")


def _speed(*entries):
    """The measured speed: the first entry's "speed", else the first bullet or note of them that names tokens/s."""
    for entry in entries:
        if entry.get("speed"):
            return entry["speed"]
    for entry in entries:
        for key in ("notes", "works", "fails"):
            for text in entry.get(key, []):
                if SPEED.search(text):
                    return text
    return None


def _rounds(entry):
    """The rounds a pair's evidence comes from: the derived block's, else its "round", else those its evidence names,
    in their order."""
    if _block(entry) and _block(entry).get("rounds"):
        return list(_block(entry)["rounds"])
    if entry.get("round"):
        return [entry["round"]]
    return list(dict.fromkeys(name.lower() for name in ROUND.findall(entry.get("evidence") or "")))


def _date(entry, rounds, table):
    """The date of a pair's evidence: the derived block's (its last round's), else its "date", else the table's dates
    of ROUNDS ("rounds"), or None."""
    if _block(entry) and _block(entry).get("date"):
        return _block(entry)["date"]
    if entry.get("date"):
        return entry["date"]
    dates = list(dict.fromkeys(table.get("rounds", {}).get(name) for name in rounds))
    return ", ".join(date for date in dates if date) or None


def _harness_support(family, agent, table):
    """One measured pair of support(): its status and colour, the evidence, what worked and what failed, the
    recommended settings (the profile), the speed, the rounds and their date."""
    every, entry = family["all"], family["harnesses"][agent]
    profile = _profile(family, agent, table)
    rounds = _rounds(entry)
    values = settings(profile)
    return {"agent": agent, "harness": HARNESS_NAMES.get(agent, agent), "status": profile["status"],
            "status_text": profile["status_text"], "light": profile["light"], "symbol": profile["symbol"],
            "reason": profile["reason"], "evidence": evidence_text(entry), "measured": evidence_short(entry),
            "measured_counts": evidence_counts(entry), "runs": entry.get("runs"), "solved": entry.get("solved"),
            "rounds": rounds, "date": _date(entry, rounds, table),
            "speed": _speed(entry, every), "works": list(entry.get("works", [])), "fails": list(entry.get("fails", [])),
            "unknown": list(entry.get("unknown", [])), "notes": list(entry.get("notes", [])),
            "warnings": list(entry.get("warnings", [])) + ([_block(entry)["warning"]]
                                                           if (_block(entry) or {}).get("warning") else []),
            "settings": values, "settings_text": values_text(values)}


def support(model, table):
    """How far MODEL is supported, from TABLE (read at run time: nothing here names a model): {model, family (None:
    no family matches, nothing known), label, names (each harness's name to show), light (the best colour of its
    measured harnesses, grey without one), symbol, light_text, text
    ("green in OpenCode, Claude Code; red in Codex", "no data yet"), by_light {colour: [agent]} (the measured
    pairs), harnesses [the measured pairs' details, in the table's order], untested [agents without a measured pair:
    no data yet there, whatever the others say], status and reason (None: a family has no status of its own), speed,
    settings and settings_text (the family's for every harness), works, fails, unknown and notes (the family's)}."""
    return family_support(family_of(model, table), model, table)


def family_support(family, model, table):
    """support() of MODEL in FAMILY (None: no family, nothing known)."""
    if family is None:
        return {"model": model, "family": None, "label": None, "names": dict(HARNESS_NAMES), "light": "grey",
                "symbol": SYMBOLS["grey"],
                "light_text": LIGHT_WORDS["grey"], "text": NO_DATA,
                "by_light": {colour: [] for colour in LIGHT_ORDER[:3]}, "harnesses": [], "untested": list(HARNESSES),
                "status": None, "reason": None, "speed": None, "settings": {}, "settings_text": "", "works": [],
                "fails": [], "unknown": [], "notes": []}
    every = family["all"]
    # Only measured pairs have a light; every other harness is grey (no data yet), never the family's or a
    # neighbour's colour.
    pairs = family.get("harnesses", {})
    harnesses = [_harness_support(family, agent, table) for agent in pairs if measured(pairs[agent])]
    by_light = {colour: [h["agent"] for h in harnesses if h["light"] == colour] for colour in LIGHT_ORDER[:3]}
    done = {h["agent"] for h in harnesses}
    untested = [agent for agent in HARNESSES if agent not in done]
    shown = [colour for colour in LIGHT_ORDER[:3] if by_light[colour]]
    colour = shown[0] if shown else "grey"
    parts = [f"{c} in {', '.join(HARNESS_NAMES.get(a, a) for a in by_light[c])}" for c in shown]
    values = {**every.get("params", {}), **{f"loop_guard.{k}": v for k, v in every.get("loop_guard", {}).items()}}
    if every.get("tools") is not None:
        values["tools"] = every["tools"]
    return {"model": model, "family": family["id"], "label": family.get("label", family["id"]),
            "names": dict(HARNESS_NAMES), "light": colour,
            "symbol": SYMBOLS[colour], "light_text": LIGHT_WORDS[colour],
            "text": "; ".join(parts) or NO_DATA, "by_light": by_light,
            "harnesses": harnesses, "untested": untested, "status": None, "reason": None,
            "speed": _speed(every), "settings": values, "settings_text": values_text(values),
            "works": list(every.get("works", [])), "fails": list(every.get("fails", [])),
            "unknown": list(every.get("unknown", [])), "notes": list(every.get("notes", []))}


def support_cell(found):
    """FOUND (support()) in a table's cell: "✓ green: opencode, codex, claude +4", "? no data yet"."""
    colour = found["light"]
    agents = found["by_light"].get(colour) or []
    if not agents:
        return f"{SYMBOLS['grey']} {LIGHT_WORDS['grey']}"
    more = f" +{len(agents) - 3}" if len(agents) > 3 else ""
    return f"{SYMBOLS[colour]} {colour}: {', '.join(agents[:3])}{more}"


def support_lines(found):
    """FOUND (support()) as lines: the light, then per measured harness its status, evidence, round and date, what
    worked (✓) and failed (✗), the recommended settings and the speed; the harnesses with no data yet."""
    lines = [f"{found['symbol']} {found['text']}"]
    if found["family"] is None:
        return lines
    for pair in found["harnesses"]:
        when = ", ".join(filter(None, [pair.get("measured") or ", ".join(pair["rounds"]), pair["date"]]))
        lines.append(f"  {pair['symbol']} {pair['harness']}: {pair['status'].replace('-', ' ')}"
                     + (f" ({when})" if when else ""))
        lines += [f"      {text}" for text in (pair["reason"], pair["evidence"]) if text]
        lines += [f"      {SIGNS['works']} {text}" for text in pair["works"]]
        lines += [f"      {SIGNS['fails']} {text}" for text in pair["fails"]]
        if pair["settings_text"]:
            lines.append(f"      recommended: {pair['settings_text']}")
        if pair["speed"]:
            lines.append(f"      speed: {pair['speed']}")
    if found["untested"]:
        names = ", ".join(HARNESS_NAMES.get(agent, agent) for agent in found["untested"])
        lines.append(f"  {SYMBOLS['grey']} {NO_DATA} in: {names}")
    return lines
