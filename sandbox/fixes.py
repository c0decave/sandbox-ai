#!/usr/bin/env python3
"""The fix plan: what a start, a live action or the host lacks, and what makes each - offered, not only refused.

A refusal because something is missing (an image not built, an MCP server not added, a toolchain not in the store,
an identity, a tool of the host) is a Missing: a ValueError that says what today's refusal says and carries its
fixes. One fix is a dict:

  id        KIND:NAME (image:claude, mcp:jadx, toolchain:go@1.24, login:codex, tool:podman)
  kind      image, variant, mcp, mcp-update, toolchain, login, identity, kit, plugin, secret, vpn, llm, service, tool, host,
            option (a change of the start's own options)
  name      the thing's name
  missing   what is not there, in words ("the image of claude (localhost/claude-sandbox:latest) is not built")
  title     what the fix does ("build it")
  commands  [[argv]]: the commands that do it, in order, as they are typed (sandbox-ai ..., or the host's package
            manager for a tool - those are never run for you)
  duration  a hint ("~5 min", "minutes", "a download", "seconds"), or ""
  unsigned  True: the fix adds something nobody signed (an MCP server built here, an unsigned catalog's entry)
  security  what is security-relevant about it, in words ("" for nothing): always said in the question
  network   True: the fix opens network for the sandbox or a server beside it
  terminal  True: its command talks to the human (a login): run on a terminal only, its output not captured
  needed    False: the start goes on without it too (a login: the agent asks for it itself)
  options   {"add": [words], "remove": [option names]}: a fix that changes the start's own options instead of the
            host (add --with claude); the start goes on with them. None for every other fix
  choice    the name of the question this fix is one answer of: fixes with the same choice are alternatives (add
            the agent, or take another worker) - one is chosen by the human (its letter on a terminal, its id with
            --fix-only), never by "all" or --fix
  terms     the id of a license the fix accepts for you ("" for none; toolchain_recipes.TERMS): such a fix is one
            answer of a question of its own (choice: asked alone, No first, never done by "all" or --fix), its
            command carries the option that accepts, and the modes high and paranoid refuse it
  start     for a fix with options: the whole command line of the start as it would be then
  verdict   offered (sandbox-ai does it after a yes, or with --fix), manual (its commands are shown, never run: they
            need root, or a value only you have), refused (the security mode does not take it: not offered, the laxer
            mode that would is named in "why")
  why       for manual and refused: why

The command line asks on a terminal (questions "all", the default: one question for everything - Fix all, Choose,
Cancel; "each": one per fix; your default fix_questions), runs the fixes with their output, and the start goes on;
--fix does them without asking; without a terminal and without --fix the refusal ends with the commands, one per
line, to copy. A fix the start goes on without (needed False) is done only when it is chosen (Choose, each, or
--fix-only), never by "all" or --fix. The security mode is never widened by an offer (judge). `AGENT ... --fix-plan` prints the plan as
JSON for the app and the web interface, which ask in their own windows and run the same commands.

Standard library only.
"""
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
QUESTIONS = ("all", "each")  # the default fix_questions: one question for everything, or one per fix
VERDICTS = ("offered", "manual", "refused")
STRICT = ("high", "paranoid")  # the modes that take nothing unsigned and open no network by an offer
LAXER = "medium"  # the strictest mode that takes what STRICT refuses of a fix
TAIL_LINES = 15  # of a failed fix's output, said with the refusal
YES = ("", "y", "yes", "j", "ja", "a", "all", "alle")
CHOOSE = ("c", "choose", "w", "waehlen", "wählen")
# The sandbox-ai commands a fix may be (their first word): what the app and the web interface run of a plan a host
# answered - never anything else that host's answer names.
COMMANDS = ("build", "images", "mcp", "toolchain", "identities", "login", "vpn", "plugins", "kits", "hosts", "llm",
            "secrets", "publish-logins")


class Missing(ValueError):
    """A refusal because something is missing that can be provided: its message is the refusal as it was said before
    there were offers (scripts and the app's checks read it), FIXES what makes each missing thing."""

    def __init__(self, message, fixes=()):
        super().__init__(message)
        self.fixes = [dict(item) for item in fixes]


def fix(kind, name, missing, title, commands=(), duration="", unsigned=False, security="", network=False,
        terminal=False, needed=True, manual="", refused="", options=None, choice="", ident=None, terms=""):
    """One fix (the module's docstring). MANUAL: why its commands are only shown; REFUSED: why it is not offered.
    OPTIONS ({"add": [words], "remove": [option names]}): a fix that changes the start's own options instead of the
    host (start_options); CHOICE: the name of the question it is one answer of (alternatives) - a manual fix with the
    same CHOICE is the way that is yours to take (shown; the start needs no more once one way was chosen). IDENT: its
    id where KIND:NAME would be another fix's (two ways for one thing)."""
    commands = [[str(word) for word in command] for command in commands]
    change = None
    if options:
        change = {"add": [str(word) for word in options.get("add") or []],
                  "remove": [str(word) for word in options.get("remove") or []]}
    verdict = "refused" if refused else "manual" if manual or not (commands or change) else "offered"
    return {"id": str(ident or f"{kind}:{name}"), "kind": kind, "name": str(name), "missing": str(missing),
            "title": str(title),
            "commands": commands, "duration": str(duration or ""), "unsigned": bool(unsigned),
            "security": str(security or ""), "network": bool(network), "terminal": bool(terminal),
            "needed": bool(needed), "verdict": verdict, "options": change, "choice": str(choice or ""), "start": "",
            "terms": str(terms or ""),
            "why": str(refused or manual or ("" if commands or change else "nothing sandbox-ai can do for you"))}


def start_options(argv, items):
    """ARGV (a start's: the agent, its options, "--", the agent's own arguments) as the fixes ITEMS change it: each
    fix's options removed (--name VALUE and --name=VALUE, wherever they stand before the "--") and its words added
    at the end of the options. A fix without "options" changes nothing."""
    at = argv.index("--") if "--" in argv else len(argv)
    options, rest = list(argv[:at]), list(argv[at:])
    for item in items:
        change = item.get("options") or {}
        kept, skip = [], False
        for word in options:
            if skip:
                skip = False
            elif word in change.get("remove", ()):
                skip = True
            elif not any(word.startswith(name + "=") for name in change.get("remove", ())):
                kept.append(word)
        options = [*kept, *change.get("add", ())]
    return [*options, *rest]


def with_starts(fixes, argv):
    """FIXES, each one that changes the start's options with "start": the whole command line of the start as it
    would be then (for the refusal's lines and the plan), from the start's ARGV."""
    return [{**item, "start": command_text(["sandbox-ai", *start_options(argv, [item])])} if item.get("options")
            else item for item in fixes]


def groups(fixes):
    """[(choice or None, [fixes])] of FIXES in their order: the answers of one question (the same "choice", all
    offered) together, every other fix on its own."""
    found = []
    for item in fixes:
        choice = item.get("choice") if item["verdict"] == "offered" else ""
        if choice and found and found[-1][0] == choice:
            found[-1][1].append(item)
        else:
            found.append((choice or None, [item]))
    return found


def unique(fixes):
    """FIXES, each id once (the first said wins: the refusal's own before what the look ahead found)."""
    found = {}
    for item in fixes:
        found.setdefault(item["id"], item)
    return list(found.values())


def judge(fixes, mode):
    """FIXES as the security mode MODE takes them: an offer never widens the mode. In high and paranoid a fix that
    adds something unsigned or opens network is refused (not offered, never run by --fix), naming the laxer mode
    that would take it; what was refused or manual stays so. Building a local image and adding a signed catalog's
    entry are offered in every mode that takes the thing itself."""
    judged = []
    for item in fixes:
        item = dict(item)
        if item["verdict"] == "offered" and mode in STRICT and (item["unsigned"] or item["network"]):
            what = "add something nobody signed" if item["unsigned"] else "open network"
            item["verdict"] = "refused"
            item["why"] = (f"not offered in the security mode {mode}: it would {what}; the mode {LAXER} takes it "
                           f"(--mode {LAXER})")
        elif item["verdict"] == "offered" and mode in STRICT and item.get("terms"):
            # A license is yours to accept: in the strict modes never by an offer's yes, only by your own command.
            item["verdict"] = "refused"
            item["why"] = (f"not offered in the security mode {mode}: it accepts a license, which is yours to do - "
                           + " && ".join(command_text(command) for command in item["commands"]))
        judged.append(item)
    return judged


def command_text(command):
    return shlex.join(command)


def marks(item):
    """What is always said of a fix that is unsigned or security-relevant: ", UNSIGNED", "; SECURITY: ..."."""
    return (", UNSIGNED" if item["unsigned"] else "") + (f"; SECURITY: {item['security']}" if item["security"] else "")


def what(item):
    """One fix in a line: what it does, with its command, how long, and whether it is unsigned or security-relevant:
    "build it: sandbox-ai build claude (~5 min)"."""
    commands = " && ".join(command_text(command) for command in item["commands"])
    if item.get("options") and not commands:  # (a change of the start's own options: the words it adds)
        commands = " ".join(item["options"]["add"]) or "without " + " ".join(item["options"]["remove"])
    return (item["title"] + marks(item) + (f": {commands}" if commands else "")
            + (f" ({item['duration']})" if item["duration"] else ""))


def numbered(fixes):
    """[(number, choice or None, [fixes])]: groups(FIXES) numbered as the plan shows them - the groups of one missing
    thing (a VPN profile: your own not done for you, a Tor profile only you choose) under one number, your own way
    first."""
    found, numbers = [], {}
    for choice, members in groups(fixes):
        number = numbers.setdefault(members[0]["missing"], len(numbers) + 1)
        found.append((number, choice, members))
    return sorted(found, key=lambda group: (group[0], bool(group[1])))


def listing(fixes):
    """The plan's lines for a terminal: each missing thing numbered, below it what is done (or why it is not) - one
    missing thing with several ways once, each way below it."""
    lines, said = [], set()
    for number, choice, members in numbered(fixes):
        item = members[0]
        more = number in said
        if not more:
            said.add(number)
            lines.append(f"  {number}. {item['missing']}" + ("" if item["needed"] else
                                                           " (optional: the start goes on without it)"))
        if choice and len(members) == 1:  # (a way only you choose: asked on its own, No its default)
            lines.append(f"     -> {'or, ' if more else ''}if you want it so: {what(item)}")
        elif choice:  # the answers of one question, lettered: one of them is chosen
            lines.append(f"     -> {'or ' if more else ''}one of:")
            lines += [f"        {LETTERS[index]}) {what(member)}" for index, member in enumerate(members)]
        elif item["verdict"] == "offered":
            lines.append(f"     -> {'or: ' if more else ''}{what(item)}")
        elif item["verdict"] == "manual":
            lines.append(f"     -> {'or ' if more else ''}not done for you ({item['why']}): "
                         + (" && ".join(command_text(c) for c in item["commands"]) or item["title"]))
        else:
            lines.append(f"     -> {item['why']}")
    return lines


LETTERS = "abcdefgh"


def block(fixes):
    """The commands that fix FIXES, one per line, to copy: those sandbox-ai would run and those only you can, of
    what the start needs (a refused one is no command here: its mode does not take it)."""
    return [command_text(command) for item in fixes if item["verdict"] != "refused" and item["needed"]
            and not (item.get("choice") and item["verdict"] == "offered") for command in item["commands"]]


def refusal(message, fixes, flag="--fix", again="start again"):
    """What a refusal with FIXES says: MESSAGE (the refusal as before), then the complete block of commands that fix
    it, one per line, and the option that does them without asking; what the mode refuses with its laxer mode."""
    lines = [str(message)]
    commands = block(fixes)
    if commands:
        lines.append("What fixes it (copy these lines), then " + again + ":")
        lines += [f"  {command}" for command in commands]
    doable = [item for item in fixes if item["verdict"] == "offered" and item["needed"] and not item.get("options")
              and not item.get("choice")]
    if flag and any(not item["terminal"] for item in doable):
        lines.append(f"Or {again} with {flag}: sandbox-ai does it first, without asking.")
    elif flag and doable:  # (what asks for a value or a login: only on a terminal, where the start asks first)
        lines.append(f"Or {again} on a terminal: sandbox-ai asks there, then does it.")
    changes = [item for item in fixes if item["verdict"] == "offered" and item.get("options")]
    if changes:  # (a choice only you make: never taken by --fix; --fix-only ID names one)
        lines.append("Start it one of these ways instead:" if len(changes) > 1 else "Start it this way instead:")
        lines += [f"  {item['start'] or what(item)}" for item in changes]
    chosen = [item for item in fixes if item["verdict"] == "offered" and item.get("choice") and not item.get("options")]
    for item in chosen:  # (a fix only you choose - a Tor profile for a VPN profile you named: never taken by --fix)
        lines.append(f"{item['missing']}: or, if you want it so, {what(item)}"
                     + (f" (--fix-only {item['id']} does it)" if flag else ""))
    for item in fixes:
        if item["verdict"] == "refused":
            lines.append(f"{item['missing']}: {item['why']}")
        elif item["verdict"] == "manual" and item["needed"]:
            lines.append(f"{item['missing']}: not done for you - {item['why']}")
    return "\n".join(lines)


SECRET_SET = re.compile(r"sandbox-ai secrets set (?P<name>[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})\b")


def secret_fix(name, what=""):
    """The fix for the secret NAME that is not kept (WHAT: who wants it, in words): kept now, its value typed in this
    terminal (never shown, never in the scrollback: `sandbox-ai secrets set` asks for it)."""
    return fix("secret", name, f"there is no secret {name}" + (f" ({what})" if what else ""),
               "keep it now: its value is asked in this terminal, never shown",
               [["sandbox-ai", "secrets", "set", name]], duration="seconds", terminal=True)


def recognised(message):
    """The fixes a refusal MESSAGE names itself, for the refusals of modules that carry none: a secret that is not
    kept ("no secret NAME; keep one with: sandbox-ai secrets set NAME", "the secret NAME (...) is missing; ... keep
    it with: sandbox-ai secrets set NAME") - the --service, --vpn, --secret-env, --api-key, --git-token-secret,
    --publish and --llm refusals alike. [] for any other refusal."""
    found = []
    text = str(message)
    for match in SECRET_SET.finditer(text):
        name = match["name"]
        said = re.search(r"(?:no secret %s\b|the secret %s \([^)]*\) is missing)" % (re.escape(name), re.escape(name)),
                         text[:match.start()])
        if said and name not in {item["name"] for item in found}:
            found.append(secret_fix(name))
    return found


def on_terminal():
    """A question can be asked here: standard input and standard error are a terminal."""
    try:
        return sys.stdin is not None and sys.stdin.isatty() and sys.stderr.isatty()
    except (AttributeError, ValueError):
        return False


def say(text):
    print(f"agent-sandbox: {text}", file=sys.stderr, flush=True)


def answer(question, read=None):
    """The human's answer to QUESTION on the terminal, lower-case; None at the end of input (Ctrl-D)."""
    prompt = f"agent-sandbox: {question} "
    try:
        if read is not None:
            return read(prompt).strip().lower()
        # (the question on standard error, where the list is: standard output may be a file or a pipe)
        print(prompt, end="", file=sys.stderr, flush=True)
        return input().strip().lower()
    except EOFError:
        print(file=sys.stderr)
        return None


def ask(fixes, questions="all", read=None, out=say, about="this start"):
    """The fixes of FIXES the human wants done (a list, [] for none), asked on the terminal. QUESTIONS "all": the
    whole list, then one question - Enter or y: all that the start needs and are offered; c: each one asked; n:
    none. "each": one question per fix. What the start goes on without (a login) is done only when chosen: asked
    with c or "each", No its default. What is manual or refused is listed, never asked. READ, OUT: for tests."""
    offered = [item for item in fixes if item["verdict"] == "offered"]
    plain = [item for item in offered if not item.get("choice")]
    needed = [item for item in plain if item["needed"]]
    out(f"{about} needs what is not there yet:")
    for line in listing(fixes):
        print(line, file=sys.stderr, flush=True)
    if not offered:
        return []
    each, chosen = questions == "each", []
    if needed and not each:
        groups_numbered = numbered(fixes)
        which = sorted({number for number, _, members in groups_numbered if any(m in needed for m in members)})
        # (the numbers of what it does when the start needs more than it does: what else it needs is not done
        # by this answer - your own VPN profile, a choice asked next)
        named = "" if all(number in which for number, _, members in groups_numbered
                          if any(m["needed"] for m in members)) else " (" + ", ".join(map(str, which)) + ")"
        reply = answer(f"Do {'it' if len(needed) == 1 else 'these ' + str(len(needed))}{named} now and go on? "
                       "[Y]es, all / [c]hoose / [n]o:", read)
        if reply in YES:
            chosen, plain = list(needed), []
        elif reply not in CHOOSE:
            return []
    elif not each:
        plain = []  # (only a choice to make, or what the start goes on without: no question for "all")
    for item in plain:
        reply = answer(f"{what(item)}? " + ("[Y/n]" if item["needed"] else "[y/N]"), read)
        if reply is None:
            return []
        if reply in YES if item["needed"] else reply in YES[1:]:
            chosen.append(item)
    # A choice between ways to go on is the human's alone: asked by its letters, none of them the default.
    for number, choice, members in numbered(fixes):
        if not choice:
            continue
        if len(members) == 1:  # (one way only you choose - a Tor profile for the VPN profile you named: No first)
            if answer(f"{number}: {what(members[0])}? [y/N]", read) in YES[1:]:
                chosen.append(members[0])
            continue
        letters = LETTERS[:len(members)]
        reply = answer(f"{number}: which one? [" + "/".join(letters) + "], or [n]one:", read)
        if reply and len(reply) == 1 and reply in letters:
            chosen.append(members[letters.index(reply)])
    return chosen


def runnable(command):
    """COMMAND as it is run here: `sandbox-ai ...` is this installation's own dispatcher."""
    if command and command[0] == "sandbox-ai":
        return [sys.executable, "-I", str(HERE / "main.py"), *command[1:]]
    return list(command)


def run_one(command, terminal=False, popen=subprocess.Popen):
    """(exit status, the last lines of its output) of COMMAND run now, its output on standard error as it comes. A
    command that talks to the human (TERMINAL) gets this terminal as it is; its output is then not kept."""
    argv = runnable(command)
    if terminal:
        try:
            return popen(argv, stdout=2).wait(), []
        except OSError as exc:
            return 127, [str(exc)]
    try:
        process = popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        env={**os.environ, "PYTHONUNBUFFERED": "1"})
    except OSError as exc:
        return 127, [str(exc)]
    tail, rest = [], b""
    while True:
        data = os.read(process.stdout.fileno(), 65536)
        if not data:
            break
        try:
            sys.stderr.buffer.write(data)
            sys.stderr.flush()
        except (AttributeError, OSError, ValueError):
            sys.stderr.write(data.decode("utf-8", "replace"))
        *lines, rest = (rest + data).replace(b"\r", b"\n").split(b"\n")
        tail = (tail + [line.decode("utf-8", "replace").rstrip() for line in lines if line.strip()])[-TAIL_LINES:]
    if rest.strip():
        tail = (tail + [rest.decode("utf-8", "replace").rstrip()])[-TAIL_LINES:]
    process.stdout.close()
    return process.wait(), tail


def run(fixes, out=say, run_command=run_one, printable=str, again="start again"):
    """FIXES done one after the other, each said with its number, its own output, and how long it took. One the
    start needs that fails stops it: a ValueError with the end of its output and the command that tries it again;
    one it goes on without (a login) is said and left. Returns the ids done."""
    done = []
    for number, item in enumerate(fixes, 1):
        out(f"fix {number}/{len(fixes)}: {item['missing']} - {what(item)}…")
        since = time.monotonic()
        failed = None
        for command in item["commands"]:
            status, tail = run_command(command, item["terminal"])
            if status != 0:
                took = f"{time.monotonic() - since:.0f} s"
                ended = "".join(f"\n  | {printable(line)}" for line in tail) if tail else \
                    " (what it said is above)"
                failed = (f"fix {number}/{len(fixes)} failed: {command_text(command)} ended with exit status "
                          f"{status} after {took}. Its last lines:{ended}\nTry it again with: "
                          f"{command_text(command)}")
                break
        if failed and item["needed"]:
            raise ValueError(f"{failed}\n(then {again}; nothing was started)")
        if failed:  # (the start goes on without it: said, never a reason to stop)
            for line in f"{failed} - not needed: going on without it".split("\n"):
                out(line)
            continue
        out(f"fix {number}/{len(fixes)} done in {time.monotonic() - since:.0f} s: {item['title']}"
            + (f" ({item['name']})" if item["name"] else ""))
        done.append(item["id"])
    return done


def chosen_by_ids(fixes, ids):
    """(the offered fixes --fix-only IDS names, the ids that name none): an id is KIND:NAME as the plan says it."""
    offered = {item["id"]: item for item in fixes if item["verdict"] == "offered"}
    return [offered[i] for i in dict.fromkeys(ids) if i in offered], [i for i in dict.fromkeys(ids) if i not in offered]


def resolve(message, fixes, mode="medium", questions="all", fix_all=False, only=(), attempted=(), terminal=None,
            read=None, out=say, run_command=run_one, printable=str, about="this start", again="start again",
            flag="--fix"):
    """What happens to a refusal MESSAGE with FIXES (a Missing's): the ids done, when everything the start needs was
    done and it can be tried again - else a ValueError: the refusal with the commands that fix it (declined, no
    terminal and no --fix, only manual or refused fixes left, or a fix that failed). FIX_ALL (--fix): the offered
    ones the start needs without asking, never a choice between ways to start; ONLY (--fix-only ID...): those,
    without asking (with --fix: and what else the start needs). ATTEMPTED: the ids done before in
    this start - one that is missing again after its fix is not tried twice."""
    fixes = judge(unique(fixes), mode)
    again_missing = [item for item in fixes if item["id"] in attempted]
    todo = [item for item in fixes if item["verdict"] == "offered" and item["id"] not in attempted]
    if not any(item["needed"] for item in todo):
        todo = []  # (only what the start goes on without is offered: what it needs is not sandbox-ai's to do)
    if again_missing or not todo:
        note = "".join(f"\n{item['missing']}: still so after its fix ran ({what(item)})" for item in again_missing)
        raise ValueError(refusal(message, fixes, flag=None if again_missing else flag, again=again) + note)
    terminal = on_terminal() if terminal is None else terminal
    only = [ident for ident in only if ident not in attempted]  # (what an earlier round of this start did)
    if only or fix_all:
        chosen, unknown = chosen_by_ids(todo, only)
        twice = [item["choice"] for item in chosen if item.get("choice")]
        if len(twice) != len(set(twice)):
            raise ValueError(refusal(message, fixes, flag=flag, again=again) + "\n--fix-only names two answers of "
                             "one choice: one of them")
        if unknown:
            raise ValueError(refusal(message, fixes, flag=flag, again=again) + f"\n--fix-only {', '.join(unknown)}: "
                             f"no fix of that id is offered here (offered: "
                             f"{', '.join(item['id'] for item in todo) or 'none'})")
        if fix_all:  # (what the start needs; what it goes on without only when named; never a choice unnamed)
            chosen += [item for item in todo if item not in chosen and item["needed"] and not item.get("choice")
                       and (not item["terminal"] or terminal)]
            chosen = [item for item in todo if item in chosen]  # (in the order they are done)
    elif terminal:
        chosen = ask(fixes, questions, read=read, out=out, about=about)
    else:
        chosen = []
    if not chosen:
        raise ValueError(refusal(message, fixes, flag=flag, again=again))
    if fix_all or only:
        out(f"{about} needs what is not there yet ({flag}{'' if fix_all else '-only'}: done now, without asking):")
        for line in listing(fixes):
            print(line, file=sys.stderr, flush=True)
    done = run(chosen, out=out, run_command=run_command, printable=printable, again=again)
    answered = {item["choice"] for item in fixes if item.get("choice") and item["id"] in done}
    left = [item for item in fixes if item["needed"] and item["id"] not in done
            and item.get("choice") not in answered]
    if left:  # (declined, manual or refused: what was chosen is done, the start needs the rest too)
        raise ValueError(refusal("Not started: " + "; ".join(dict.fromkeys(item["missing"] for item in left)), left,
                                 flag=flag, again=again))
    return done


def plan_json(fixes, mode, questions):
    """The plan as `AGENT ... --fix-plan` prints it: {"fixes": [...], "questions": all|each, "mode"}; each fix with
    "command", its commands in one line as they are typed."""
    judged = judge(unique(fixes), mode)
    return {"fixes": [{**item, "command": " && ".join(command_text(command) for command in item["commands"])
                       or item.get("start") or "",
                       "text": what(item) if item["verdict"] == "offered" else item["why"]} for item in judged],
            "questions": questions if questions in QUESTIONS else QUESTIONS[0], "mode": mode}


def without_options(argv):
    """ARGV without --fix, --fix-only ID and --fix-plan, and their short forms -fx, -fo ID and -fp (before a "--"):
    what a session keeps for its later runs, which fix nothing unasked (the launcher hands it the options as they
    were typed)."""
    kept, skip = [], False
    for index, token in enumerate(argv):
        if token == "--":
            kept += argv[index:]
            break
        if skip:
            skip = False
        elif token in ("--fix-only", "-fo"):
            skip = True
        elif token not in ("--fix", "--fix-plan", "-fx", "-fp") and not token.startswith(("--fix-only=", "-fo=")):
            kept.append(token)
    return kept


# --- the host's own tools: the distribution's package command (hostinit.py) -----------------------------------

_hostinit = None


def hostinit():
    """hostinit.py: the host's package manager and which package brings a tool; Podman's init binary."""
    global _hostinit
    if _hostinit is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("sandbox_ai_hostinit_for_fixes", HERE / "hostinit.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        _hostinit = loaded
    return _hostinit


def package_manager(*args, **kwargs):
    return hostinit().package_manager(*args, **kwargs)


def tool_fix(tools, missing=None, manager=None):
    """The fix for host tools that are not installed (TOOLS: their commands): the distribution's package command -
    shown, never run (it needs root, and sudo is never typed for you)."""
    tools = [str(tool) for tool in tools]
    command = hostinit().install_command(tools, manager)
    said = missing or (f"{', '.join(tools)} {'is' if len(tools) == 1 else 'are'} not installed on this host")
    if not command:
        return fix("tool", ",".join(tools), said, "install " + ", ".join(tools) + " with the host's package manager",
                   manual="sandbox-ai installs no system packages")
    return fix("tool", ",".join(tools), said, "install it" if len(tools) == 1 else "install them", [command],
               manual="it needs root: sandbox-ai never runs sudo for you")


def init_fix(what=""):
    """The fix for Podman's init binary (catatonit), which a container of an image without an init of its own needs
    from the host (hostinit.py): the distribution's package command, shown."""
    return tool_fix([hostinit().INIT_NAME], missing=f"{hostinit().INIT_NAME}, the init binary Podman's --init takes "
                    "from this host, is not installed" + (f" ({what} needs it)" if what else ""))
