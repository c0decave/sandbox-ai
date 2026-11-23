#!/usr/bin/env python3
"""How each harness fares as a worker of Cairn's board (--cairn).

Every harness the launcher knows can be one of Cairn's workers (--cairn-workers AGENT[=MODEL],...): Claude Code and
Codex through Cairn's own drivers, every other one through Cairn's claudecode driver, which sandbox-ai's relay maps
onto the harness's own one-shot run with a command line of its own (cairn_relay.harness_argv) - Cairn itself is
unchanged. How far each is supported is shown as a traffic light, the model lights' (model_profiles: green
verified, yellow partly working, red does not work with its reason, grey no data yet), from cairn_workers.json: a
light other than grey names the run it comes from (a CI job, a live check's report); what sandbox-ai's own tests
prove with stand-ins, and what Cairn expects that a harness cannot give, are said beside it.

Check them on your machine: scripts/live_check.sh --only cairn-workers-live --llm NAME runs one small task of
real Cairn's with each harness whose image is there (docs/LIVE_CHECK.md).

    sandbox-ai cairn workers [AGENT ...] [--json]

Standard library only.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
TABLE = HERE / "cairn_workers.json"
# Cairn's driver of each harness: codex its own, claude its own, every other one the claudecode driver, mapped.
NATIVE = {"claude": "claudecode", "codex": "codex"}
# The harnesses that take a model as an option on their command line (the launcher's model_effort_args); the others
# take it from their settings, so --cairn-workers AGENT=MODEL is refused for them.
MODEL_OPTION = ("claude", "codex", "opencode", "hermes", "qwen", "letta", "pi")
LIMITS = {
    "conclusion": "Cairn's conclusion (its second ask, when a first answer did not parse) is not run: a one-shot run "
                  "cannot be named that task's own conversation, and never goes on in one that may be another's; "
                  "the task fails and Cairn hands it out again",
    "model": "no model option on its command line: --cairn-workers AGENT=MODEL is refused; its model is its "
             "settings' (as the lead: the start's --model or --llm)",
    "openhands-lead": "a worker beside another lead (--with openhands), with its own login and model: as the lead "
                      "--cairn is refused (its Agent Canvas holds port 8000)",
    "nanobot-workspace": "it works in its own workspace (~/.nanobot/workspace); each task is told Cairn's project "
                         "folder",
}
STATUSES = ("verified", "smoke-only", "partial", "failing", "known-bad", "untested")
LIVE_CHECK = "scripts/live_check.sh --only cairn-workers-live --llm NAME"
EPILOG = ("Every harness can be one of Cairn's workers (--cairn-workers AGENT[=MODEL],...): Claude Code and Codex "
          "through Cairn's own drivers, every other one through Cairn's claudecode driver, which sandbox-ai's relay "
          "turns into the harness's own one-shot run - Cairn itself is unchanged. Each one's support is a traffic "
          "light, the same as the models': verified, partly working, does not work (with the reason; such a worker "
          "is refused), no data yet. A light other than grey names the run it comes from: a CI job or a report "
          "of the live check.\n\n"
          f"Check them on your machine: {LIVE_CHECK} runs one small task of real Cairn with each harness whose "
          "image is there (docs/LIVE_CHECK.md).")

_loaded = {}


def _module(name):
    if name not in _loaded:
        spec = importlib.util.spec_from_file_location(f"sandbox_ai_{name}_for_cairn_workers", HERE / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _loaded[name] = module
    return _loaded[name]


def lights():
    """model_profiles: the traffic light's colours, symbols and words (one vocabulary for models and workers)."""
    return _module("model_profiles")


def relay():
    return _module("cairn_relay")


def load(path=None):
    """The table ({agent: {status, evidence, tests, limits, reason}}), checked: every harness of the relay's once, a
    status of the traffic light's, evidence for every status but untested, a reason for a red one. ValueError
    otherwise."""
    document = json.loads(Path(path or TABLE).read_text(encoding="utf-8"))
    table = document.get("harnesses") if isinstance(document, dict) else None
    if not isinstance(table, dict) or set(table) != set(relay().HARNESSES):
        raise ValueError(f"{path or TABLE}: one entry for each harness ({', '.join(relay().HARNESSES)})")
    for agent, entry in table.items():
        status = entry.get("status")
        if status not in STATUSES:
            raise ValueError(f"{agent}: status {status!r} is none of {', '.join(STATUSES)}")
        if status != "untested" and not entry.get("evidence"):
            raise ValueError(f"{agent}: {status} names no evidence (a CI job, a live check's report)")
        if lights().light(status) == "red" and not entry.get("reason"):
            raise ValueError(f"{agent}: {status} says no reason")
        unknown = [limit for limit in entry.get("limits") or [] if limit not in LIMITS]
        if unknown:
            raise ValueError(f"{agent}: unknown limit {unknown[0]!r}")
    return table


def driver(agent):
    return NATIVE.get(agent, "claudecode")


def run_text(agent):
    """The command line a task of Cairn's runs for AGENT, as it is shown (PROMPT, UUID, MODEL in its places)."""
    if agent == "claude":
        return "claude --session-id UUID --dangerously-skip-permissions -p [--model MODEL] -- PROMPT"
    if agent == "codex":
        return "codex exec [--model MODEL] --dangerously-bypass-approvals-and-sandbox -- PROMPT"
    own = ["[--model MODEL]"] if agent in MODEL_OPTION else []
    argv, stdin = relay().harness_argv(agent, "PROMPT", own, session="UUID")
    return " ".join(argv) + (" < PROMPT" if stdin is not None else "")


def row(agent, table=None):
    """What is known of AGENT as Cairn's worker: its light (colour, symbol, words), its status, the driver Cairn
    calls it through, its command line, the evidence, what the tests prove, its limits in words, a red one's reason."""
    table = table or load()
    entry, profiles = table[agent], lights()
    colour = profiles.light(entry["status"])
    return {"agent": agent, "name": profiles.HARNESS_NAMES.get(agent, agent), "status": entry["status"],
            "light": colour, "symbol": profiles.SYMBOLS[colour], "words": profiles.LIGHT_WORDS[colour],
            "driver": driver(agent), "mapped": agent not in NATIVE, "run": run_text(agent),
            "model_option": agent in MODEL_OPTION, "evidence": entry.get("evidence"), "tests": entry.get("tests"),
            "limits": [LIMITS[limit] for limit in entry.get("limits") or []],
            "limit_ids": list(entry.get("limits") or []), "reason": entry.get("reason")}


def rows(agents=None):
    table = load()
    return [row(agent, table) for agent in (agents or relay().HARNESSES)]


def rank(agent, table=None):
    """AGENT's place among the workers by its light: green, yellow, grey, red (Cairn's first worker takes every kind
    of task)."""
    order = ("green", "yellow", "grey", "red")
    return order.index(lights().light((table or load())[agent]["status"]))


def refusal(agent, table=None):
    """Why AGENT does not work as Cairn's worker (a red light), or None."""
    found = row(agent, table)
    return found["reason"] if found["light"] == "red" else None


def summary(found):
    """One line of a worker: "~ claude (Claude Code): partly working - CI's job agents-live (cairn) ..."."""
    said = found["evidence"] or "; ".join(filter(None, (lights().NO_DATA, found["tests"])))
    text = f"{found['symbol']} {found['agent']} ({found['name']}): {found['words']} - {said}"
    if found["reason"]:
        text += f"; {found['reason']}"
    return text


def lines(found, width=100):
    """A worker's lines in the listing: its light, then what it runs, the evidence, what is tested, its limits."""
    import textwrap

    def item(label, text):
        return textwrap.wrap(f"{label}: {text}", width=width, initial_indent="    ", subsequent_indent=" " * 6,
                             break_on_hyphens=False)
    via = "Cairn's own driver, its output as it comes" if not found["mapped"] else \
        "through Cairn's claudecode driver, made by sandbox-ai's relay; Cairn reads its final message"
    out = [f"{found['symbol']} {found['agent']} ({found['name']}): {found['words']}",
           *item("run", f"{found['run']} ({via})")]
    if found["reason"]:
        out += item("does not work", found["reason"])
    out += item("evidence", found["evidence"] or lights().NO_DATA)
    if found["tests"]:
        out += item("tests", found["tests"])
    for limit in found["limits"]:
        out += item("limit", limit)
    return out


# --- command line ---------------------------------------------------------------------------------------------


def help_style(prog):
    """The layout of this command's help, the one of every command with subcommands (helpstyle.py)."""
    loaded = sys.modules.get("sandbox_ai_helpstyle")
    if loaded is None:
        spec_ = importlib.util.spec_from_file_location("sandbox_ai_helpstyle", HERE / "helpstyle.py")
        loaded = importlib.util.module_from_spec(spec_)
        spec_.loader.exec_module(loaded)
        sys.modules["sandbox_ai_helpstyle"] = loaded
    return loaded.CommandsFormatter(prog)


def parser():
    parser = argparse.ArgumentParser(prog="sandbox-ai cairn", description="Cairn's board (--cairn): its workers.",
                                     formatter_class=help_style,
                                     epilog=EPILOG)
    commands = parser.add_subparsers(dest="command", required=True)
    workers = commands.add_parser("workers", help="how each harness fares as Cairn's worker (the traffic light)",
                                  description="How each harness fares as Cairn's worker: its light, one line each; "
                                              "for the harnesses named the command line a task runs, the evidence, "
                                              "its limits.",
                                  epilog="Examples:\n  sandbox-ai cairn workers\n  sandbox-ai cairn workers opencode "
                                         "--json")
    workers.add_argument("agents", nargs="*", metavar="AGENT", help="only these harnesses")
    workers.add_argument("--json", action="store_true", help="as JSON")
    return parser


def main(argv, out=print):
    args = parser().parse_args(argv)
    try:
        known = relay().HARNESSES
        wrong = [agent for agent in args.agents if agent not in known]
        if wrong:
            raise ValueError(f"{wrong[0]!r} is no harness of the launcher ({', '.join(known)})")
        found = rows(list(dict.fromkeys(args.agents)) or None)
    except (ValueError, OSError) as exc:
        print(f"sandbox-ai cairn: {exc}", file=sys.stderr)
        return 1
    if args.json:
        out(json.dumps(found, indent=2, ensure_ascii=False))
        return 0
    profiles = lights()
    legend = ", ".join(f"{profiles.SYMBOLS[c]} {profiles.LIGHT_WORDS[c]}" for c in profiles.LIGHT_ORDER)
    text = [f"Cairn's workers: how each harness fares ({legend})", ""]
    if args.agents:  # (the harnesses named: what each runs, the evidence, what is tested, its limits)
        for entry in found:
            text += [*lines(entry), ""]
    else:  # (all of them: one line each - journeys round 1, ~120 lines were no overview)
        text += [f"{entry['symbol']} {entry['agent']} ({entry['name']}): {entry['words']}"
                 + (f" - {entry['reason']}" if entry["reason"] else "") for entry in found]
        text += ["", "What one runs, its evidence and its limits: sandbox-ai cairn workers AGENT"]
    text.append(f"Check them on your machine: {LIVE_CHECK}")
    out("\n".join(text))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
