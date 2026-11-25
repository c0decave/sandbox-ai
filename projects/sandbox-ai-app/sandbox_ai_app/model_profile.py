"""The model x harness profile of a board's (or the Board page's) model in its agent, shown where the model is chosen.

sandbox-ai applies a profile at every start with --llm NAME:MODEL (its model_profiles.py and model_profiles.json: the
local-model research's sampling, context window, turn limit and the harness's own switches) UNDER the user's own
values - the server's parameters (llm set), the board's --llm-param, --env - and --no-llm-profile switches it off.
This module looks the profile up in this machine's sandbox-ai (sai.module("model_profiles")) and says it in the app's
words: a badge ("Model profile: qwen3-coder-next × opencode (verified)"), its values with "your value wins" where the
board or the server sets one itself, and its warnings for a pair known not to work (a warning, never a refusal).

No Qt: the Board page (board_view.py) and the Board page (ui.py) show it.
"""
from __future__ import annotations

from .files import printable
from .i18n import N_, tr
from . import applog

OPTION = "--no-llm-profile"
STATUS = {"verified": N_("verified"), "smoke-only": N_("smoke test only"), "partial": N_("partly working"),
          "failing": N_("failing"), "known-bad": N_("known not to work"), "untested": N_("no data yet in this agent")}
WARNING_STATUSES = ("failing", "known-bad")
# What each kind of setting is, in the app's words (the values themselves are sandbox-ai's names).
WORDS = {"temperature": N_("temperature"), "top_p": N_("top P"), "top_k": N_("top K"),
         "repeat_penalty": N_("repeat penalty"), "min_p": N_("min P"), "max_tokens": N_("max. output tokens"),
         "num_ctx": N_("context length"), "loop_guard.steps": N_("turn limit (loop guard steps)"),
         "tools": N_("Claude Code's tools (--llm-tools)")}


def table(sai):
    """The profiles of this machine's sandbox-ai, or None (an older one without them, or a damaged file)."""
    try:
        module = sai.module("model_profiles") if sai is not None else None
        if module is None or not hasattr(module, "match"):
            return None
        return module, module.load()
    except Exception as _exc:  # noqa: BLE001 - an older sandbox-ai has no profiles: nothing is shown
        applog.swallowed("model_profile", "table: an older or damaged sandbox-ai shows no model profile", _exc)
        return None


def lookup(sai, model, agent):
    """The profile of MODEL in AGENT ({id, name, status, params, loop_guard, env, warnings, ...}), or None."""
    found = table(sai) if model and agent else None
    if found is None:
        return None
    module, document = found
    try:
        return module.match(str(model), str(agent), document)
    except Exception as _exc:  # noqa: BLE001
        applog.swallowed("model_profile", "lookup failed (no profile shown)", _exc)
        return None


def settings(profile):
    """{key: value} of what PROFILE sets (parameters, loop_guard.LAYER, env.NAME), as sandbox-ai's settings()."""
    found = dict(profile.get("params") or {})
    found.update({f"loop_guard.{k}": v for k, v in (profile.get("loop_guard") or {}).items()})
    found.update({f"env.{k}": v for k, v in (profile.get("env") or {}).items()})
    if profile.get("tools") is not None and profile.get("agent") in ("claude", "*"):
        found["tools"] = profile["tools"]  # Claude Code's own tools the proxy keeps (all, core or a list)
    return found


def own_keys(params=None, server=None, model=None):
    """The keys the user set themselves, whose value wins over the profile's: the board's --llm-param values
    (PARAMS {key: text}, loop_guard.LAYER included) and the server's own parameters (SERVER: `llm list --json`'s
    endpoint, its "params" {"*" or MODEL: {key: value}}; its "loop_guard_own" layers)."""
    keys = {str(key) for key, value in (params or {}).items() if str(value).strip()}
    stored = (server or {}).get("params") if isinstance(server, dict) else None
    for scope in ("*", model):
        values = stored.get(scope) if isinstance(stored, dict) else None
        keys.update(str(key) for key in (values or {}) if isinstance(values, dict))
    layers = (server or {}).get("loop_guard_own") if isinstance(server, dict) else None
    keys.update(f"loop_guard.{key}" for key in (layers or {}) if isinstance(layers, dict))
    return keys


def status_text(status):
    return tr(STATUS[status]) if status in STATUS else printable(str(status))


def badge(profile, enabled=True):
    """One line: "Model profile: qwen3-coder-next × opencode (verified)" (off: "Model profile off: ...")."""
    name = printable(str(profile.get("name") or profile.get("id") or ""))
    case = profile.get("use_case") if isinstance(profile.get("use_case"), dict) else None
    if case:  # the use case's settings over it (or alone, for a model of no family)
        name = tr("{name} with use case {use_case}", name=name, use_case=printable(str(case.get("id")))) \
            if profile.get("family") else tr("use case {use_case} for {model}", use_case=printable(str(case.get("id"))),
                                             model=printable(str(profile.get("label") or "")))
    if not enabled:
        return tr("Model profile off: {name}", name=name)
    if case and not profile.get("family"):
        return tr("Model profile: {name} (the use case's settings: no profile of its own)", name=name)
    return tr("Model profile: {name} ({status})", name=name, status=status_text(profile.get("status")))


def value_lines(profile, mine=(), enabled=True):
    """Its values, one per line, "your value wins" beside those the user set (MINE); off: what it would set."""
    lines = []
    for key, value in settings(profile).items():
        shown = "true" if value is True else "false" if value is False else \
            printable(",".join(map(str, value))) if isinstance(value, list) else printable(str(value))
        label = tr(WORDS[key]) if key in WORDS else printable(key[4:] if key.startswith("env.") else key)
        line = f"{label} ({printable(key)}): {shown}" if key in WORDS else f"{label}: {shown}"
        if key in mine:
            line += " — " + tr("your value wins")
        lines.append(line)
    return lines


def details(profile, mine=(), enabled=True):
    """The whole of it for a tooltip or the inspector: the badge, what the status means, the evidence, its values
    (yours marked), the reasoning, its notes and what sandbox-ai sets for every local model anyway."""
    lines = [badge(profile, enabled)]
    if profile.get("evidence"):
        lines.append(tr("Measured: {evidence}", evidence=printable(str(profile["evidence"]))))
    if profile.get("status") == "untested":
        lines.append(tr("No data yet for this model in this agent: its settings for every agent apply."))
    lines.append(tr("It sets (under your own values):") if enabled else
                 tr("It would set (switched off: --no-llm-profile):"))
    lines += ["  " + line for line in value_lines(profile, mine, enabled)] or ["  " + tr("nothing")]
    if profile.get("reasoning"):
        lines.append(tr("Thinking: {text}", text=printable(str(profile["reasoning"]))))
    lines += [printable(str(note)) for note in profile.get("notes") or []]
    if profile.get("builtin"):
        lines.append(tr("sandbox-ai sets for every model of your own anyway:"))
        lines += ["  " + printable(str(text)) for text in profile["builtin"]]
    return lines


def warnings(profile):
    """Its warnings (a pair or model known not to work, few MCP servers for this model): shown, never refused."""
    return [printable(str(text)) for text in profile.get("warnings") or []]


def board_choice(board, servers=None):
    """(model, agent, LLM part) of BOARD's start: the LLM part's NAME:MODEL (or the server's default model, as the
    model row shows it), the agent part's agent; (None, None, None) without an LLM part with a server."""
    from . import wizard_llm as WL
    llm = board.first("llm")
    agent = board.first("agent")
    if llm is None or agent is None or not board.connected(llm) or not str(llm.props.get("--llm") or "").strip():
        return None, None, None
    used = WL.used_model(board, servers)
    model = used[0] if used else None
    return model, agent.props.get("agent", "claude"), llm


def board_profile(sai, board, servers=None):
    """(profile, enabled, mine) of BOARD's model in its agent, or None: MINE the keys the board's LLM part or its
    server sets itself (their values win)."""
    model, agent, llm = board_choice(board, servers)
    profile = lookup(sai, model, agent) if model else None
    if model and llm is not None and llm.props.get("--use-case"):
        # the use case's settings for this model in this agent over it, as the start puts them (use_case.py)
        from .use_case import with_use_case
        profile = with_use_case(sai, profile, model, agent, llm.props.get("--use-case"))
    if profile is None:
        return None
    from . import wizard_llm as WL
    name, _ = WL.parse_value(llm.props.get("--llm"))
    server = next((e for e in servers or [] if isinstance(e, dict) and e.get("name") == name), None)
    params = llm.props.get("params") if isinstance(llm.props.get("params"), dict) else {}
    mine = own_keys(params, server, model)
    if str(llm.props.get("--llm-tools") or "").strip():
        mine.add("tools")  # the board's --llm-tools wins over the profile's
    return profile, not llm.props.get(OPTION), mine


# --- MCP tools against the model's class (sandbox-ai's mcp_budget.py) -----------------------------------------------
# User 2026-11-19: Gemma-4-12B/26B did worse with all four memory aids at once (round 5 of the local-model research).
# The launcher counts the MCP tools a start's model would see against its class's budget (model_profiles.json
# "tool_budgets"); the Board shows its dry run's count ("mcp_tools") as a finding, the Start page this machine's.
# A warning only: nothing is refused.

TOOL_SIZES = {"small": N_("a small model"), "mid": N_("a mid-sized model"), "large": N_("a large model")}
TOOL_TEXTS = (N_("{model} would see {count} MCP tools, more than {size} copes with (about {budget})."),
              N_("{model} would see about {count} MCP tools, more than {size} copes with (about {budget})."),
              N_("Servers to leave out: {servers} - then {left}."), N_("{server} ({tools} tools)"),
              N_("The tools of {servers} are not known yet (counted after its first start in this project)."),
              N_("A warning only: the start goes on, you decide."),
              N_("The budget (sandbox-ai's local-model research): {evidence}"))


def tool_budget_lines(found):
    """The app's words for FOUND (sandbox-ai's mcp_budget report: the launcher's own count) when the model would see
    more MCP tools than its class copes with: what it sees and copes with, the servers to leave out (the largest
    first) and the tools then, the servers whose tools are not known; [] when it is within (or nothing is known)."""
    if not isinstance(found, dict) or not found.get("over"):
        return []
    size = tr(TOOL_SIZES[found["class"]]) if found.get("class") in TOOL_SIZES else printable(str(found.get("class")))
    tools = {str(s.get("server")): s.get("tools") for s in found.get("servers") or [] if isinstance(s, dict)}
    lines = [tr(TOOL_TEXTS[1] if found.get("about") else TOOL_TEXTS[0], model=printable(str(found.get("model"))),
                count=int(found.get("tools") or 0), size=size, budget=int(found.get("budget") or 0))]
    dropped = [tr(TOOL_TEXTS[3], server=printable(str(name)), tools=tools.get(str(name)))
               for name in found.get("drop") or []]
    if dropped:
        lines.append(tr(TOOL_TEXTS[2], servers=", ".join(dropped), left=int(found.get("left") or 0)))
    if found.get("unknown"):
        lines.append(tr(TOOL_TEXTS[4], servers=", ".join(printable(str(n)) for n in found["unknown"])))
    return lines


def tool_budget_finding(found):
    """One line of tool_budget_lines and that it is a warning only (the Board's finding), or ""."""
    lines = tool_budget_lines(found)
    return " ".join([*lines, tr(TOOL_TEXTS[5])]) if lines else ""


def tool_budget_details(found):
    """The tooltip's text: the lines, the warning only, the research's evidence for the budget."""
    lines = tool_budget_lines(found)
    if not lines:
        return ""
    if found.get("evidence"):
        lines.append(tr(TOOL_TEXTS[6], evidence=printable(str(found["evidence"]))))
    return "\n".join([*lines, tr(TOOL_TEXTS[5])])


# --- what we know: the "What we know" box beside the profile --------------------------------------------------------
# User: "a box ... with agent + harness what we know and what we dont with short bullet points and what is working and
# what not". sandbox-ai's table lists per family and pair what works, what fails and what is not known yet
# (model_profiles.KNOWLEDGE); this says it with the pair's status in words. Information only: nothing is refused.

STATUS_WORDS = {
    # derived from the research's graded runs (sandbox-ai's model_evidence.RULES); a pair without measured runs has
    # none (untested: no data yet)
    "verified": N_("Verified: at least 10 graded runs over 5 use cases, the sandbox's own among them, a mean score of "
                   "0.85 or more, no use case below 0.50 and no injection that fired."),
    "smoke-only": N_("Smoke test only: it starts, calls tools and edits; nothing else was run."),
    "partial": N_("Partly working: something passed, short of verified (the evidence says what is missing)."),
    "failing": N_("Failing: nothing passed."),
    "known-bad": N_("Known not to work: {reason}"),
    "untested": N_("No data yet for this pair: the model's settings for every agent apply."),
}
GROUPS = (("works", "✓", N_("Works")), ("fails", "✗", N_("Does not work")), ("unknown", "?", N_("Not known yet")))


def _status_line(status, reason):
    if status == "known-bad":
        return tr(STATUS_WORDS[status], reason=printable(str(reason or "")))
    return tr(STATUS_WORDS[status]) if status in STATUS_WORDS else printable(str(status))


def knowledge(profile, model=None, agent=None):
    """What we know of a model in an agent ({title, lines, groups: [(sign, heading, [bullet])], bad: the line that
    says it is known not to work, or None}) from PROFILE
    (lookup's; None: a model of no family, MODEL and AGENT name it): the pair's status in words (with what was
    measured), or that there is no data yet for the pair, with the family's general notes; then ✓ works, ✗ does not
    work, ? not known yet (the pair's own bullets first, then the family's)."""
    if profile is None:
        name = f"{printable(str(model or ''))} × {printable(str(agent or ''))}"
        return {"title": tr("What we know: {name}", name=name), "groups": [], "bad": None,
                "lines": [tr("Nothing is known about this model: no model family of sandbox-ai's research matches its "
                             "name. The agent's and the server's own settings apply.")]}
    name = printable(str(profile.get("name") or profile.get("id") or ""))
    status = profile.get("status")
    if status != "untested":  # a measured pair (only its own runs give a status)
        lines = [_status_line(status, profile.get("reason"))]
        if profile.get("evidence"):
            lines.append(tr("Measured: {evidence}", evidence=printable(str(profile["evidence"]))))
    else:
        lines = [_status_line("untested", None)]
        lines += [printable(str(note)) for note in profile.get("notes") or []]
    groups = [(sign, tr(heading), [printable(str(text)) for text in profile.get(key) or []])
              for key, sign, heading in GROUPS]
    bad = next((line for line in lines if status in WARNING_STATUSES and line == _status_line(status,
                                                                                          profile.get("reason"))), None)
    return {"title": tr("What we know: {name}", name=name), "lines": lines,
            "groups": [group for group in groups if group[2]], "bad": bad}


def knowledge_text(found):
    """FOUND (knowledge()) as plain text: its title, its lines, each group's heading ("✓ Works") and bullets."""
    lines = [found["title"], *found["lines"]]
    for sign, heading, bullets in found["groups"]:
        lines.append(f"{sign} {heading}")
        lines += [f"  • {text}" for text in bullets]
    return "\n".join(lines)


def knowledge_of(sai, model, agent):
    """knowledge() of MODEL in AGENT, or None: nothing chosen, or a sandbox-ai without profiles (nothing to say)."""
    if not model or not agent or table(sai) is None:
        return None
    return knowledge(lookup(sai, model, agent), model, agent)


def board_knowledge(sai, board, servers=None):
    """knowledge() of BOARD's model in its agent, or None (no model server of yours chosen)."""
    model, agent, _ = board_choice(board, servers)
    return knowledge_of(sai, model, agent)


# --- the traffic light: how far the research supports a model, per harness ------------------------------------------
# User: "farbliche Ampel zu den Modellen, die auf einem lokalen LLM-Server sind: wie weit wir sie supporten und
# erfolgreich in welchem Harness hatten. Detail-Infos, was funktioniert hat und was nicht, pro Modell". sandbox-ai's
# model_profiles.support() reads the table at run time (round 4 adds models: nothing here names one); this says it in
# the app's words. Colour, a shape of its own per colour (model_profile_view.light_icon) and words: never colour alone.

LIGHT_WORDS = {"green": N_("verified"), "yellow": N_("partly working"), "red": N_("does not work"),
               "grey": N_("no data yet")}
LIGHT_THEME = {"green": "ok", "yellow": "warn", "red": "bad", "grey": "muted"}  # theme.color's names
SYMBOLS = {"green": "✓", "yellow": "~", "red": "✗", "grey": "?"}


def support_of(sai, model):
    """How far MODEL is supported (sandbox-ai's model_profiles.support(): light, harnesses ...), or None: no model, or
    a sandbox-ai without the traffic light (nothing is shown then)."""
    found = table(sai) if model else None
    if found is None or not hasattr(found[0], "support"):
        return None
    module, document = found
    try:
        return module.support(str(model), document)
    except Exception as _exc:  # noqa: BLE001
        applog.swallowed("model_profile", "support failed (no traffic light shown)", _exc)
        return None


AGENT_NAMES = {"claude": "Claude Code", "codex": "Codex", "opencode": "OpenCode", "opencode2": "OpenCode 2",
               "hermes": "Hermes", "dsh": "DeepSeek Harness", "qwen": "Qwen Code", "goose": "Goose",
               "openhands": "OpenHands", "nanobot": "nanobot", "letta": "Letta Code", "pi": "Pi"}


def agent_name(agent):
    return AGENT_NAMES.get(agent) or printable(str(agent))


def harness_names(agents):
    return ", ".join(agent_name(agent) for agent in agents)


def light_label(found):
    """A cell's short text: "✓ verified: OpenCode, Codex +5", "? no data yet"."""
    colour = found.get("light", "grey")
    agents = (found.get("by_light") or {}).get(colour) or []
    words = tr(LIGHT_WORDS.get(colour, LIGHT_WORDS["grey"]))
    if not agents:
        return f"{SYMBOLS.get(colour, '?')} {words}" + (": " + tr("every agent") if colour != "grey" else "")
    more = f" +{len(agents) - 3}" if len(agents) > 3 else ""
    return f"{SYMBOLS.get(colour, '?')} {words}: {harness_names(agents[:3])}{more}"


def light_summary(found):
    """In words (a tooltip, a screen reader): "verified in OpenCode, Claude Code; does not work in Codex", "no data
    yet" - only the agents the model was measured in."""
    parts = [tr("{words} in {agents}", words=tr(LIGHT_WORDS[colour]), agents=harness_names(agents))
             for colour, agents in (found.get("by_light") or {}).items() if agents and colour in LIGHT_WORDS]
    return "; ".join(parts) or tr("no data yet")


def light_accessible(found, model=""):
    """What a screen reader says of a model's light: "Support of MODEL: verified in OpenCode, ...". """
    return tr("Support of {model}: {summary}", model=printable(str(model or found.get("model") or "")),
              summary=light_summary(found))


def measured_text(counts):
    """A derived pair's evidence in a few words, where its status is shown: "49 runs, 13 use cases, rounds 4-6"
    (sandbox-ai's evidence_counts(); "" for a hand-written evidence)."""
    if not isinstance(counts, dict) or not isinstance(counts.get("runs"), int):
        return ""
    runs, cases = counts["runs"], counts.get("use_cases")
    parts = [tr("1 run") if runs == 1 else tr("{runs} runs", runs=runs)]
    if isinstance(cases, int) and cases > 0:
        parts.append(tr("1 use case") if cases == 1 else tr("{use_cases} use cases", use_cases=cases))
    if counts.get("rounds"):
        parts.append(printable(str(counts["rounds"])))
    return ", ".join(parts)


def support_details(found):
    """The detail panel of a model ({title, light, summary, lines, harnesses: [{symbol, light, title, lines, works,
    fails}], untested}): per measured agent its status, the round and date of the evidence, the evidence, what worked
    (✓) and what failed (✗), the recommended settings (the profile) and the measured speed; "no data yet" when
    none."""
    model = printable(str(found.get("model") or ""))
    title = tr("Support of {model}", model=model)
    if found.get("family") is None:
        return {"title": title, "light": "grey", "summary": tr("no data yet"),
                "lines": [tr("No data yet: no model family of sandbox-ai's research matches this name.")],
                "harnesses": [], "untested": ""}
    lines = [tr("Model family: {label}", label=printable(str(found.get("label") or found["family"])))]
    if found.get("speed"):
        lines.append(tr("Measured speed: {speed}", speed=printable(str(found["speed"]))))
    if found.get("settings_text"):
        lines.append(tr("Recommended for every agent: {settings}", settings=printable(str(found["settings_text"]))))
    harnesses = []
    for pair in found.get("harnesses") or []:
        status = status_text(pair.get("status"))
        when = ", ".join(filter(None, [", ".join(map(str, pair.get("rounds") or [])), pair.get("date") or ""]))
        rows = []
        if when:
            rows.append(tr("Evidence of {when}", when=printable(when)))
        if pair.get("reason"):
            rows.append(tr("Why: {reason}", reason=printable(str(pair["reason"]))))
        if pair.get("evidence"):
            rows.append(tr("Measured: {evidence}", evidence=printable(str(pair["evidence"]))))
        if pair.get("runs") is not None and pair.get("solved") is not None:
            rows.append(tr("{solved} of {runs} runs solved", solved=pair["solved"], runs=pair["runs"]))
        if pair.get("settings_text"):
            rows.append(tr("Recommended settings: {settings}", settings=printable(str(pair["settings_text"]))))
        if pair.get("speed"):
            rows.append(tr("Measured speed: {speed}", speed=printable(str(pair["speed"]))))
        measured = measured_text(pair.get("measured_counts"))  # the derived evidence beside the status
        heading = f"{agent_name(pair.get('agent'))}: {status}" + (f" ({measured})" if measured else "")
        harnesses.append({"light": pair.get("light", "grey"), "symbol": SYMBOLS.get(pair.get("light"), "?"),
                          "title": heading,
                          "lines": rows,
                          "works": [printable(str(text)) for text in pair.get("works") or []],
                          "fails": [printable(str(text)) for text in pair.get("fails") or []]})
    untested = found.get("untested") or []
    return {"title": title, "light": found.get("light", "grey"), "summary": light_summary(found), "lines": lines,
            "harnesses": harnesses,
            "untested": tr("No data yet in: {agents}", agents=harness_names(untested)) if untested else ""}


def support_text(details):
    """DETAILS (support_details()) as plain text: for a tooltip, a copy."""
    lines = [details["title"], details["summary"], *details["lines"]]
    for pair in details["harnesses"]:
        lines.append(f"{pair['symbol']} {pair['title']}")
        lines += [f"    {line}" for line in pair["lines"]]
        lines += [f"    ✓ {text}" for text in pair["works"]]
        lines += [f"    ✗ {text}" for text in pair["fails"]]
    if details["untested"]:
        lines.append(details["untested"])
    return "\n".join(lines)
