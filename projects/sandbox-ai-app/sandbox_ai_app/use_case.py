"""The use case of a board's start: the model and harness sandbox-ai's local-model research found best for a kind of work.

sandbox-ai keeps per use case (coding, sandbox behaviour, web research, code review ...) the ranked model x harness
choices with their settings, why, known failures and evidence (its model_usecases.py and model_usecases.json; round 5
of the research fills in what has "no data yet"). This module asks this machine's sandbox-ai (sai.module) to pick
from the models the board's host's servers have - the Board read them already (backend.host_llm_servers: names, sizes,
what is loaded, what reads images) - and their memory (`llm set NAME --memory GB`; a server on this machine's loopback
has this machine's), and says it in the app's words: the choice and why, and the "What we know" box of the use case.

The Board puts the choice on the board (the LLM part's --llm SERVER:MODEL, the agent part's agent, --use-case NAME);
at the start sandbox-ai puts the use case's settings for that model in that agent over the model profile, under the
board's own values. Overridable: another model or agent chosen afterwards stays, the use case's settings then apply
where the table has that pair.

No Qt: board_view.py shows it.
"""
from __future__ import annotations

import os
import urllib.parse

from .files import printable
from .i18n import N_, tr
from . import applog

OPTION = "--use-case"
STATUS = {"measured": N_("measured on this use case's tasks"),
          "indirect": N_("from other tasks than this use case's own"),
          "not tested": N_("no data yet")}  # (the status id "not tested": no measured or indirect choice)
# The table's labels in the app's language (a label the table has that is not here is shown as it comes).
LABELS = (N_("Coding and debugging"), N_("Sandbox behaviour"), N_("Untrusted input"), N_("Web research"),
          N_("Code review"),
          N_("Security and reverse engineering"), N_("Large codebase"), N_("Building from a spec"),
          N_("Ops: containers, CI, shell"), N_("Small fast jobs"), N_("Vision"), N_("Teams and helpers"),
          N_("Recovery: compaction and resume"))
LOOPBACK = ("127.0.0.1", "localhost", "::1")  # llm.LOOPBACK_HOSTS: the names a server on the loopback has


def table(sai):
    """(module, the use-case table) of this machine's sandbox-ai, or None: an older one without use cases, or a
    damaged table (nothing is offered then)."""
    try:
        module = sai.module("model_usecases") if sai is not None else None
        if module is None or not hasattr(module, "recommend_all"):
            return None
        return module, module.load()
    except Exception as _exc:  # noqa: BLE001 - an older sandbox-ai has no use cases: nothing is offered
        applog.swallowed("use_case", "table: an older or damaged sandbox-ai offers no use case", _exc)
        return None


def label(text):
    return tr(str(text)) if str(text) in LABELS else printable(str(text))


def status_text(status):
    return tr(STATUS[status]) if status in STATUS else printable(str(status))


def machine_memory():
    """This machine's memory in bytes (a server on its loopback has it), or None."""
    try:
        pages, size = os.sysconf("SC_PHYS_PAGES"), os.sysconf("SC_PAGE_SIZE")
        return pages * size if pages > 0 and size > 0 else None
    except (ValueError, OSError, AttributeError):
        return None


def servers_of(endpoints, local=True):
    """The board's model servers (backend.host_llm_servers: `llm list --json`'s endpoints with "models", "model_info",
    "answered") as sandbox-ai's model_usecases.recommend takes them: their models with size, loaded and capabilities,
    their memory (memory_gb, else this machine's for a server on its loopback when LOCAL)."""
    found = []
    for endpoint in endpoints or []:
        if not isinstance(endpoint, dict) or not isinstance(endpoint.get("name"), str):
            continue
        info = endpoint.get("model_info") if isinstance(endpoint.get("model_info"), dict) else {}
        models = [info.get(name) if isinstance(info.get(name), dict) else {"name": name}
                  for name in endpoint.get("models") or [] if isinstance(name, str)]
        memory = endpoint.get("memory_gb")
        memory = int(memory * 1e9) if isinstance(memory, (int, float)) and not isinstance(memory, bool) and \
            memory > 0 else None
        try:  # (IPv6 without its brackets, with or without a port: what sandbox-ai's llm.parse_url says)
            host = urllib.parse.urlsplit(str(endpoint.get("url") or "")).hostname or ""
        except ValueError:
            host = ""
        if memory is None and local and host in LOOPBACK:
            memory = machine_memory()
        found.append({"name": endpoint["name"], "models": models, "memory_bytes": memory,
                      "answered": endpoint.get("answered") is not False, "error": endpoint.get("models_error")})
    return found


def recommendations(sai, endpoints, local=True, agent=None, note_agent=None):
    """[recommend()] of every use case from the board's servers ENDPOINTS (sandbox-ai's model_usecases with the
    profiles for the nearest choice and the pairs' statuses), or None: a sandbox-ai without use cases. NOTE_AGENT:
    the agent the pairs' notes compare with (the page's agent; the choice is the best whatever it is)."""
    found = table(sai)
    if found is None:
        return None
    module, document = found
    try:
        profiles_module = sai.module("model_profiles")
        profiles = profiles_module.load() if hasattr(profiles_module, "support") else None
    except Exception as _exc:  # noqa: BLE001 - no nearest by the traffic light then
        applog.swallowed("use_case", "recommendations: no profiles (no nearest choice by the traffic light)", _exc)
        profiles = None
    try:
        try:
            return module.recommend_all(document, servers_of(endpoints, local), agent, profiles, note_agent)
        except TypeError:  # an older sandbox-ai: no notes on the agent (its pairs, if any, without them)
            return module.recommend_all(document, servers_of(endpoints, local), agent, profiles)
    except Exception as _exc:  # noqa: BLE001 - nothing is offered rather than a broken page
        applog.swallowed("use_case", "recommendations failed (no use case offered)", _exc)
        return None


def choice_text(found):
    """FOUND (one use case's recommend()) in one or more lines: the choice and where it comes from, why, the
    evidence, the settings over the model profile, what fits, the note; or why there is none."""
    choice = found.get("choice")
    if not choice:
        return tr("No data yet, and no model of this host's servers to go by: choose the model yourself.") \
            if found.get("status") == "not tested" else \
            tr("Nothing of this use case's table is on this host's servers: choose the model yourself.")
    where = tr("rank {rank}", rank=choice["rank"]) if choice.get("rank") else \
        tr("nearest: the choice for {use_case}", use_case=printable(str(choice.get("related")))) \
        if choice.get("related") else tr("nearest: by the traffic light, nothing measured for it")
    lines = [tr("Choice: {model} in {harness} ({where})", model=printable(f"{choice['server']}:{choice['model']}"),
                harness=printable(str(choice.get("harness_name") or choice.get("harness"))), where=where),
             printable(str(choice.get("why") or ""))]
    if choice.get("evidence"):
        lines.append(tr("Measured: {evidence}", evidence=printable(", ".join(
            filter(None, [str(choice["evidence"]), str(choice.get("date") or "")])))))
    if choice.get("settings_text"):
        lines.append(tr("Settings over the model profile: {settings}", settings=printable(choice["settings_text"])))
    for key in ("fit_text", "note"):
        if choice.get(key):
            lines.append(printable(str(choice[key])))
    return "\n".join(line for line in lines if line)


def knowledge(found):
    """The "What we know" box of a use case ({title, lines, groups, bad}, as model_profile.knowledge()): its status and
    what it is, then ✓ why the choice works, ✗ what does not (the choice's known failures and the use case's), ? what
    is not known yet."""
    if not found:
        return None
    lines = [status_text(found.get("status")), printable(str(found.get("about") or ""))]
    choice = found.get("choice") or {}
    works = [printable(str(choice["why"]))] if choice.get("why") and choice.get("source") == "table" else []
    if choice.get("evidence"):
        works.append(printable(str(choice["evidence"])))
    fails = [printable(str(text)) for text in [*(choice.get("fails") or []), *(found.get("fails") or [])]]
    unknown = [printable(str(text)) for text in found.get("unknown") or []]
    groups = [("✓", tr("Works"), works), ("✗", tr("Does not work"), fails), ("?", tr("Not known yet"), unknown)]
    return {"title": tr("What we know: use case {name}", name=label(found.get("label") or found.get("use_case"))),
            "lines": [line for line in lines if line], "groups": [group for group in groups if group[2]],
            "bad": None}


def with_use_case(sai, profile, model, agent, name):
    """PROFILE (model_profile.lookup's, or None) with the use case NAME's settings for MODEL in AGENT over it, as
    sandbox-ai's start puts them (model_usecases.overlay); PROFILE as it is without a use case or such settings."""
    found = table(sai) if name and model and agent else None
    if found is None:
        return profile
    module, document = found
    try:
        entry = module.use_case(document, name)
        chosen = module.settings_for(document, entry["id"], str(model), str(agent))
        return module.overlay(profile, chosen, entry["id"], entry["label"], str(agent)) if chosen else profile
    except Exception as _exc:  # noqa: BLE001 - an unknown use case: the profile alone
        applog.swallowed("use_case", "with_use_case: the profile alone", _exc)
        return profile


# --- the pairs: model x harness, best first (model_usecases.pairs) -------------------------------------------------

# What a pair's status in another agent is called (model_profiles.STATUSES); "rank 3, measured" and the like stay.
AGENT_STATUS = {"verified": N_("verified"), "smoke-only": N_("smoke test only"), "partial": N_("partial"),
                "failing": N_("failing"), "known-bad": N_("known bad"), "untested": N_("no data yet"),
                "not tested": N_("no data yet"), "no data yet": N_("no data yet")}  # (an older sandbox-ai: "not tested")


def pairs_of(found, server=None):
    """FOUND's pairs (one use case's recommend(): every model of the servers with its best harness, best first),
    only SERVER's when given; [] for an older sandbox-ai without pairs."""
    return [pair for pair in (found or {}).get("pairs") or []
            if isinstance(pair, dict) and (server is None or pair.get("server") == server)]


def pair_where(pair):
    """Where PAIR comes from, in the app's words: "rank 2, measured", "rank 6, from other tasks", "not measured for
    this use case", "⚠ unsafe for this use case"."""
    if pair.get("unsafe"):
        return tr("⚠ unsafe for this use case")
    if pair.get("source") == "table":
        return tr("rank {rank}, measured", rank=pair.get("rank")) if pair.get("basis") == "measured" else \
            tr("rank {rank}, from other tasks", rank=pair.get("rank"))
    return tr("not measured for this use case")


def harness_of(pair):
    return printable(str(pair.get("harness_name") or pair.get("harness") or "")) or tr("any agent")


def agent_note(pair):
    """The note on PAIR when the page's agent is not its harness: "MODEL is measured best in Claude Code; in OpenCode:
    partial" ("" when it is)."""
    if not pair.get("agent"):
        return ""
    status = str(pair.get("agent_status") or "no data yet")
    status = tr(AGENT_STATUS[status]) if status in AGENT_STATUS else printable(status)
    agent = printable(str(pair.get("agent_name") or pair["agent"]))
    if pair.get("source") == "not measured":
        return tr("{model} is not measured for this use case, best by its traffic light in {harness}; in {agent}: "
                  "{status}", model=printable(pair["model"]), harness=harness_of(pair), agent=agent, status=status)
    return tr("{model} is measured best in {harness}; in {agent}: {status}", model=printable(pair["model"]),
              harness=harness_of(pair), agent=agent, status=status)


def pair_row(pair, one_server=True):
    """PAIR as a row of the pairs' table: model (SERVER:MODEL when the table shows several servers), agent, where it
    comes from, what fits, why (an unsafe one: its reason)."""
    model = printable(pair["model"] if one_server else f"{pair['server']}:{pair['model']}")
    why = printable(str(pair.get("unsafe") or pair.get("why") or ""))
    fit = {"loaded": tr("loaded"), "fits": tr("fits"), "tight": tr("may not load"), "too big": tr("too big"),
           "unknown": tr("not known")}.get(pair.get("fit"), printable(str(pair.get("fit") or "")))
    return [model, harness_of(pair), pair_where(pair), fit, why]


def pair_text(pair):
    """PAIR in lines (its tooltip and the note below the table): the pair and where it comes from, why or the
    reason it is unsafe, the evidence, its known failures, what fits, the note on the page's agent, the command."""
    lines = [tr("{model} in {harness} ({where})", model=printable(f"{pair['server']}:{pair['model']}"),
                harness=harness_of(pair), where=pair_where(pair))]
    if pair.get("unsafe"):
        lines.append(tr("Unsafe for this use case: {reason}", reason=printable(str(pair["unsafe"]))))
    elif pair.get("why"):
        lines.append(printable(str(pair["why"])))
    if pair.get("evidence"):
        lines.append(tr("Measured: {evidence}", evidence=printable(", ".join(
            filter(None, [str(pair["evidence"]), str(pair.get("date") or "")])))))
    lines += ["✗ " + printable(str(text)) for text in pair.get("fails") or []]
    if pair.get("settings_text"):
        lines.append(tr("Settings over the model profile: {settings}", settings=printable(pair["settings_text"])))
    if pair.get("fit_text"):
        lines.append(printable(str(pair["fit_text"])))
    note = agent_note(pair)
    if note:
        lines.append(note)
    if pair.get("command"):
        lines.append(tr("Start: {command}", command=printable(str(pair["command"]))))
    return "\n".join(lines)
