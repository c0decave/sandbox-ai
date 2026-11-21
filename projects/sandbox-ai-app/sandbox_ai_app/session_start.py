"""A start under a session's name that is taken: found before anything runs, and when the launcher refused it.

User report (2026-11-19): a Board start on another host ended in a terminal tab with "agent-sandbox: Session
vital-wallaby-azalea is running; attach with: sandbox-ai attach vital-wallaby-azalea / [Exit-Status 1]" - "the
session/terminal is not opened", "the session is not started at all it seems". The Board had kept the name of its
last start (a codename is replaced only once its start ran), and the next Start - with another agent - met the
session of the first. Only a session of this machine, of the same agent, was turned into an attach.

The Board and the Start page mix SessionStart in (beside prerequisites.Prerequisites, whose host(), sai and
prerequisite_agent() it uses). Before a start with a session name goes to its terminal, session_in_the_way() looks
at that name on the start's host - this machine: the session's own files, read at once; another host: the Sandboxes
page's look when it is a few seconds old, else `sandbox-ai session status NAME --json` there, in the background:

  the same agent's session runs, with a terminal   its terminal is opened (attach), as for this machine before
  it is paused, in the foreground, another         said before anything runs, with: Attach to it / Continue it,
  agent's, or runs without a terminal              Stop it and start, Start under another name, Cancel
  a stopped session of another agent               said too (the launcher refuses it): another name, or Cancel

And when a start's tab ends with an error although all that was checked (a session made meanwhile, a host that did
not answer the look), start_ended() says in the tab's line and on the page why - the launcher's own last words -
with the same buttons where a session of that name is in the way.
"""
from __future__ import annotations

import inspect
import time

from . import applog, terminal_text
from .files import printable, printable_text
from .i18n import tr
from .widgets import ask_first, background, choose, warn

LOCAL = "local"  # this machine, as `sandbox-ai hosts` names it
SESSIONS_FRESH = 5  # seconds the Sandboxes page's look at a host may be old to answer for a start there
SEEN_WAIT = 75  # seconds a host may take to say what is there under a name
IN_THE_WAY = ("running", "paused", "orphaned")


class StartTab:
    """What a page hands the window's in_terminal as ENDED for a start: called with the command's exit status; the
    window notes the tab it opened (view), so that the page can say in that tab why the start did not run."""

    def __init__(self, function):
        self.function, self.view = function, None

    def __call__(self, status):
        return self.function(status, self.view)


def on_host(host, text):
    """TEXT, with the host's name before it when it is not this machine."""
    return text if host == LOCAL else f"{printable(host)}: {text}"


def place(host):
    """"on this machine" or "on HOST": where a session is, as a part of a sentence (in German too)."""
    return tr("on this machine") if host == LOCAL else tr("on {host}", host=printable(host))


def seen_of(instance):
    """The facts of a start the Sandboxes page lists (INSTANCE, of another host: its record is `list --json`'s) as
    backend.seen_facts gives them, or None when its record says too little."""
    from . import backend
    record = getattr(instance, "record", None)
    if not isinstance(record, dict) or "state" not in record or "attachable" not in record:
        return None
    return backend.seen_facts({**record, "name": instance.name, "agent": record.get("agent") or instance.agent,
                               "foreground": record.get("kind") == "run"})


def judged(seen, agent):
    """What a start of AGENT does with what is there under its session's name (SEEN: backend.seen_facts, or None):
    "start" (nothing in the way: none, a stopped session of that agent, one the launcher handles itself), "attach"
    (the same agent's session runs with a terminal: its terminal), or "ask" (said first: another agent's, a paused
    one, one in the foreground or without a terminal)."""
    if not seen:
        return "start"
    state, same = seen["state"], seen["agent"] == agent
    if state == "stopped":
        return "start" if same or not seen["agent"] else "ask"  # (another agent's: the launcher refuses the name)
    if state not in IN_THE_WAY:
        return "start"  # (damaged: the launcher says what it reads there)
    if state == "orphaned":
        return "start" if same else "ask"  # (the launcher stops its pod and starts it again, said)
    if state == "paused" or seen["foreground"] or not same:
        return "ask"
    if seen["terminal"] == "ended":
        return "start"  # (no agent any more: the launcher stops it and starts it again, said)
    if seen["terminal"] == "missing":
        return "ask"
    return "attach"


def what_is_there(host, name, seen, agent):
    """The sentence that says what is there under NAME on HOST (SEEN) for a start of AGENT."""
    theirs = printable(seen["agent"]) or "?"
    name = printable(name)
    if seen["state"] == "stopped":
        return tr("A stopped session named {name} {place} belongs to {theirs}, not {agent}: the launcher does not "
                  "start {agent} under that name.", name=name, place=place(host), theirs=theirs, agent=agent)
    if seen["state"] == "paused":
        text = tr("A session named {name} is paused {place} ({theirs}).", name=name, place=place(host), theirs=theirs)
    elif seen["foreground"]:
        text = tr("A start named {name} runs {place} in the foreground of another terminal or script ({theirs}; a "
                  "task or --no-tmux): it has no terminal to attach to.", name=name, place=place(host), theirs=theirs)
    elif seen["agent"] != agent:
        text = tr("A session named {name} runs already {place} - with {theirs}, not {agent}.", name=name,
                  place=place(host), theirs=theirs, agent=agent)
    elif seen["terminal"] == "missing":
        text = tr("A session named {name} runs {place} without a terminal to attach to (its start made none).",
                  name=name, place=place(host))
    else:
        text = tr("A session named {name} runs already {place} ({theirs}).", name=name, place=place(host),
                  theirs=theirs)
    if seen["terminal"] == "starting":
        text += " " + tr("It is still starting.")
    elif seen["terminal"] == "ended":
        text += " " + tr("Its agent has ended (its tmux session is gone), its sandbox has not.")
    if seen.get("seen"):
        text += "\n\n" + tr("What sandbox-ai sees there: {seen}", seen=printable(seen["seen"]))
    return text


def choices_for(seen, agent, other):
    """[(key, label)] of what can be done about SEEN for a start of AGENT (OTHER: the fresh name offered): attach
    (it has a terminal), continue (paused), stop (it runs or is paused), other, in that order."""
    found = []
    same = seen["agent"] == agent
    if seen["state"] == "running" and seen["attachable"]:
        found.append(("attach", tr("Attach to it")))
    if seen["state"] == "paused":
        found.append(("continue", tr("Continue it")))
    if seen["state"] in IN_THE_WAY:
        # (another agent's session keeps its name when it is stopped: the new start takes the fresh one)
        found.append(("stop", tr("Stop it and start") if same else
                      tr("Stop it and start {agent} as {name}", agent=agent, name=printable(other))))
    found.append(("other", tr("Start as {name}", name=printable(other))))
    return found


class SessionStart:
    """The mixin (see the module's docstring). The page gives host(), sai, prerequisite_agent(), terminal (the
    window's in_terminal) and, optionally, show_notice(text, role), show_missing(text, rows) and start_controls."""

    def session_in_the_way(self, session, embedded, go_on, same=None):
        """False when nothing is known in the way of a start under SESSION right now (the caller goes on). True
        while that is looked up on the start's host, or when something else was done: its terminal opened, or the
        human asked - GO_ON(name) then starts under NAME (SESSION, or another), unless SAME() says the page shows
        another start meanwhile."""
        if not session or not hasattr(self.sai, "session_seen"):
            return False
        host, agent = self.host(), self.prerequisite_agent()
        same = same or (lambda: True)
        if host == LOCAL and hasattr(self.sai, "local_session_seen"):
            # This machine's own: read here and now from the session's files (no process, no wait).
            try:
                seen = self.sai.local_session_seen(session)
            except Exception as exc:  # noqa: BLE001 - not read: the launcher has the last word
                applog.log("INFO", "start", f"what is there under {printable(session)} on this machine could not be "
                                            f"read ({applog.error_text(exc)}): the launcher checks it")
                return False
            return self.session_decided(host, agent, session, embedded, seen, go_on)
        known = self.session_known(host, session)
        if known is not None:
            applog.log("DEBUG", "start", f"a session named {printable(session)} on {printable(host)}: known from the "
                                         "Sandboxes page's look just now")
            return self.session_decided(host, agent, session, embedded, known, go_on)

        def done(seen, error):
            if not same() or self.host() != host:
                applog.log("INFO", "start", "the page shows another start now: nothing is started for the one that "
                                            f"waited for the look at {printable(host)}")
                return
            if error is not None:
                # Not answered: the start goes on, the launcher has the last word (and start_ended says its refusal).
                applog.log("INFO", "start", f"what is there under {printable(session)} on {printable(host)} could not "
                                            f"be read ({applog.error_text(error)}): the launcher checks it")
                seen = None
            if not self.session_decided(host, agent, session, embedded, seen, go_on):
                go_on(session)
        self.session_notice(on_host(host, tr("Looking whether a session named {name} runs there already…",
                                             name=printable(session))) if host != LOCAL else
                            tr("Looking whether a session named {name} runs already…", name=printable(session)))
        background(lambda: self.sai.session_seen(host, session), owner=self, timeout=SEEN_WAIT, done=done,
                   what=f"what is there under the session name {session} on {host}")
        return True

    def session_known(self, host, session):
        """What the Sandboxes page saw under SESSION on HOST when its look is at most SESSIONS_FRESH seconds old and
        found a start of that name that runs or is paused (another host's: its listing says all a start needs to
        know); None otherwise - then the host is asked."""
        window = self.window() if hasattr(self, "window") else None
        fresh = getattr(window, "fresh_starts", None) if window is not self else None
        if not callable(fresh):
            return None
        try:
            found = fresh(host, SESSIONS_FRESH)
        except Exception as exc:  # noqa: BLE001 - the host is asked instead
            applog.swallowed("session_start", "SessionStart.session_known: the Sandboxes page's look", exc)
            return None
        for instance in found or []:
            if session in (instance.name, getattr(instance, "ident", None)) and instance.state in IN_THE_WAY:
                return seen_of(instance)
        return None

    def session_decided(self, host, agent, session, embedded, seen, go_on):
        """What judged() says is done about SEEN: False for "start" (the caller goes on); True after its terminal was
        opened or the human was asked (GO_ON(name) runs when the answer starts something)."""
        verdict = judged(seen, agent)
        applog.log("INFO", "start", f"a start of {agent} as session {printable(session)} on {printable(host)}: "
                   + (f"there is {seen['state']} {seen['agent'] or '?'}"
                      + (f", terminal {seen['terminal']}" if seen.get("terminal") else "")
                      + (", in the foreground" if seen.get("foreground") else "") if seen else "nothing of that name")
                   + f" - {verdict}")
        if verdict == "start":
            return False
        if verdict == "attach":
            self.session_attach(host, session, embedded, seen)
            return True
        self.session_ask(host, agent, session, embedded, seen, go_on)
        return True

    def session_attach(self, host, session, embedded, seen=None, resume=False):
        """The terminal of the session SESSION on HOST, as Attach of the Sandboxes page opens it (RESUME: continued
        first, `sandbox-ai resume`): instead of a second start that the launcher refuses."""
        try:
            argv = self.sai.resume_argv(host, session) if resume else self.sai.attach_argv(host, session)
        except Exception as exc:  # noqa: BLE001 - shown, the window stays
            warn(self, tr("Not attached"), printable_text(exc))
            return False
        title = on_host(host, tr("session {name}", name=printable(session)))
        text = on_host(host, tr("{name} is continued: its terminal is opened", name=printable(session)) if resume else
                       tr("{name} runs already: its terminal is opened", name=printable(session)))
        if seen and seen.get("terminal") == "starting":
            text += " " + tr("(it is still starting: the tab waits for its terminal)")
        applog.log("INFO", "start", f"{printable(session)} on {printable(host)}: its terminal is opened instead of a "
                                    "second start")
        self.session_notice(text, status=True)
        self.start_terminal(argv, title, embedded, None)
        return True

    def session_ask(self, host, agent, session, embedded, seen, go_on, lead=None):
        """Say what is there under SESSION (SEEN) and do what the human chooses: attach, continue, stop it and start,
        another name, or nothing (Cancel, the default). LEAD: a sentence before it (the launcher's refusal)."""
        other = self.other_session_name(session)
        text = what_is_there(host, session, seen, agent)
        if lead:
            text = f"{lead}\n\n{text}"
        offered = choices_for(seen, agent, other)
        answer = choose(self, tr("Session {name}", name=printable(session)), text, offered, destructive=("stop",))
        self.session_answer(answer, host, agent, session, embedded, seen, go_on, other)
        return answer

    def session_answer(self, answer, host, agent, session, embedded, seen, go_on, other):
        """ANSWER (a key of choices_for, None: Cancel) carried out."""
        if answer == "attach":
            self.session_attach(host, session, embedded, seen)
        elif answer == "continue":
            self.session_attach(host, session, embedded, seen, resume=True)
        elif answer == "other":
            self.take_session_name(other)
            go_on(other)
        elif answer == "stop":
            self.session_stop_and_start(host, agent, session, seen, go_on, other)
        else:
            self.session_notice(tr("Not started: a session named {name} is there already.", name=printable(session)),
                                role="notice-warn")

    def session_stop_and_start(self, host, agent, session, seen, go_on, other):
        """Stop the session SESSION on HOST (asked first: start-stop), then start: under its name when it is this
        agent's (the launcher continues it with the options it was created with), else under OTHER."""
        name = session if seen["agent"] == agent else other
        if not ask_first(self, "start-stop", tr("Stop"), on_host(host, tr(
                "Stop {name}? Its agent ends; what it did not save is lost (a session's HOME is kept).",
                name=printable(session))), tr("Stop")):
            return

        def stopped(_result, error):
            if error is not None:
                warn(self, tr("Not stopped"), on_host(host, printable_text(error)))
                return
            refresh = getattr(self.window(), "refresh_starts", None) if self.window() is not self else None
            if callable(refresh):
                refresh(host)
            if name != session:
                self.take_session_name(name)
            go_on(name)
        self.session_notice(on_host(host, tr("Stopping {name}…", name=printable(session))))
        background(lambda: self.sai.session_stop(host, session), owner=self, work=True, timeout=180, done=stopped,
                   what=f"stopping the session {session} on {host}")

    # -- the name --

    def other_session_name(self, taken):
        """A fresh session name (a codename, as the launcher names starts; else TAKEN with a number)."""
        try:
            name = self.sai.module("codenames").codename()
        except Exception as exc:  # noqa: BLE001 - an older sandbox-ai without codenames
            applog.swallowed("session_start", "SessionStart.other_session_name: no codenames", exc)
            name = ""
        if not name or name == taken:
            name = f"{str(taken)[:36]}-{int(time.time()) % 1000}"
        return name

    def take_session_name(self, name):
        """The Start controls show NAME as the session's name (typed: in the field; a codename: as its suggestion)."""
        controls = getattr(self, "start_controls", None)
        if controls is not None and hasattr(controls, "take_name"):
            controls.take_name(name)

    def session_started(self, session):
        """A start under SESSION went to its terminal: the next one gets another codename (a typed name stays). The
        Board kept the codename of a start that had waited for a check or a preparation, and its next Start - with
        another agent - met the session of the first."""
        controls = getattr(self, "start_controls", None)
        if controls is not None and session and hasattr(controls, "used"):
            controls.used(session)

    # -- its terminal, and its end --

    def start_terminal(self, argv, title, embedded, cwd, ended=None):
        """ARGV in a terminal through the page's terminal function (the window's in_terminal); ENDED only where that
        function takes one."""
        extra = {}
        if ended is not None:
            try:
                parameters = inspect.signature(self.terminal).parameters
            except (TypeError, ValueError):
                parameters = {}
            if "ended" in parameters or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
                extra["ended"] = ended
        return self.terminal(argv, title, embedded=embedded, cwd=cwd, **extra)

    def start_watch(self, host, agent, session, embedded, go_on):
        """ENDED for a start's terminal (a StartTab): when the start ends with an error, why is said (start_ended)."""
        return StartTab(lambda status, view: self.start_ended(status, view, host, agent, session, embedded, go_on))

    def start_ended(self, status, view, host, agent, session, embedded, go_on):
        """A start's terminal ended with STATUS. An error: the launcher's last words (the tab VIEW's end) are said in
        the tab's line and on the page - never only "exit status 1" - and when a session of that name is in the way
        (the launcher refused the start for it, or the host says so now), with the buttons of session_ask."""
        if not status:
            return
        said = ""
        if view is not None:
            try:
                said = terminal_text.refusal(view.start_tail())
            except Exception as exc:  # noqa: BLE001 - the tab is gone: the status alone
                applog.swallowed("session_start", "SessionStart.start_ended: the tab's end", exc)
        applog.log("WARNING", "start", f"the start of {agent}" + (f" as session {printable(session)}" if session else "")
                   + f" on {printable(host)} ended with exit status {status}: "
                   + (applog.redact(said)[:600] or "its terminal says why"))
        plain = tr("Not started: {why}", why=printable_text(said)) if said else \
            tr("The start ended with an error (exit status {status}): its terminal shows why.", status=status)
        refused = terminal_text.session_refusal(said) if said else None
        if not session or not hasattr(self.sai, "session_seen") or (said and not refused):
            self.say_refusal(view, on_host(host, plain), [])
            return

        def done(seen, error):
            # The buttons when something of that name is in the way: what judged() would have asked about, or what
            # the launcher itself refused the start for (a session that runs: started meanwhile, or held by a lock).
            in_the_way = error is None and bool(seen) and (judged(seen, agent) == "ask" or bool(refused))
            if not in_the_way:
                self.say_refusal(view, on_host(host, plain), [])
                return
            other = self.other_session_name(session)
            offered = choices_for(seen, agent, other)

            def act(key):
                return lambda: self.session_answer(key, host, agent, session, embedded, seen, go_on, other)
            text = on_host(host, plain) + "\n" + what_is_there(host, session, seen, agent).split("\n\n")[0]
            self.say_refusal(view, text, [(label, act(key)) for key, label in offered])
        background(lambda: self.sai.session_seen(host, session), owner=self, timeout=SEEN_WAIT, done=done,
                   what=f"what is there under the session name {session} on {host} (its start was refused)")

    def say_refusal(self, view, text, actions):
        """TEXT - why a start did not run - in the line above its terminal (VIEW's) and on the page (the Board's
        panel, its notes), each with ACTIONS [(label, function)]."""
        bar = getattr(view, "bar", None) if view is not None else None
        if bar is not None:
            try:
                if actions:
                    bar.ask(text, [*actions, (tr("Cancel"), lambda: None)], level="warn")
                else:
                    bar.say(text, seconds=0, level="warn")
            except RuntimeError:  # the tab was closed meanwhile
                pass
        self.session_notice(text.split("\n")[0], role="notice-warn")
        first, _, rest = text.partition("\n")
        if getattr(self, "preparing_panel", None) is not None or "show_missing" in self.__dict__:
            # (the Board's panel: why a start did not go on, with what helps)
            self.show_missing(first, [(rest or first, list(actions))] if actions or rest else [], role="notice-warn")
            return
        notify = getattr(self.window(), "notify", None) if hasattr(self, "window") and self.window() is not self \
            else None
        if callable(notify):  # (the Start page has no such panel: a notification beside the tab's line)
            try:
                notify(tr("Not started"), text, level="warning")
            except Exception as exc:  # noqa: BLE001 - the tab's line and the log have it
                applog.swallowed("session_start", "SessionStart.say_refusal: no notification", exc)

    def session_notice(self, text, role="secondary", status=False):
        """TEXT in the page's notes (show_notice) - and, with STATUS, in the window's status line."""
        shown = getattr(self, "show_notice", None)
        if callable(shown):
            shown(text, role)
        else:
            applog.log("WARNING" if role == "notice-warn" else "INFO", "start", text)
        window = self.window() if hasattr(self, "window") else None
        bar = getattr(window, "statusBar", None) if status and window is not self else None
        if callable(bar):
            try:
                bar().showMessage(text, 8000)
            except Exception as exc:  # noqa: BLE001 - no status line: the page's note has it
                applog.swallowed("session_start", "SessionStart.session_notice: no status line", exc)


# -- a team start: the same for a team of that name (team start --dry-run --json: "exists", "taken") --

TEAM_NAME_LIMIT = 20  # team.TEAM_NAME: 1-20 of a-z, 0-9, _ and -


def other_team_name(taken, suffix=None):
    """A fresh team name for TAKEN: its first letters and two to four of a-z0-9 (team names are short: the members
    are the sessions NAME-AGENT)."""
    import secrets
    suffix = suffix or secrets.token_hex(2)
    return f"{str(taken)[:TEAM_NAME_LIMIT - len(suffix) - 1].rstrip('-_')}-{suffix}"


def team_what(host, name, verdict):
    """What team start's verdict VERDICT (its refusal: code "exists" or "taken") says is there under the team NAME on
    HOST, in a sentence and the launcher's own words."""
    team = verdict.get("team") if isinstance(verdict.get("team"), dict) else {}
    if verdict.get("ok") and verdict.get("continues"):
        text = tr("Team {name} is paused {place}: Continue the team continues its members ({members}) with the options they "
                  "were made with.", name=printable(name), place=place(host), members=printable(", ".join(
                      f"{name}-{agent}" for agent in verdict.get("continues") or [])))
    elif verdict.get("code") == "taken":
        text = tr("A member's session name of team {name} is used by another start {place}.", name=printable(name),
                  place=place(host))
    elif team.get("verdict") == "attach":
        text = tr("Team {name} runs already {place}.", name=printable(name), place=place(host))
    elif team and not team.get("same"):
        text = tr("A team named {name} is there already {place}, with other members: {members}.",
                  name=printable(name), place=place(host),
                  members=printable(", ".join(str(m) for m in team.get("members") or [])))
    else:
        text = tr("A team named {name} is there already {place}.", name=printable(name), place=place(host))
    said = printable_text(verdict.get("error") or "")
    return text + ("\n\n" + tr("What sandbox-ai sees there: {seen}", seen=said) if said else "")


def team_choices(verdict, other):
    """[(key, label)] of what can be done about team start's verdict VERDICT (its refusal, or a paused team it
    continues; OTHER: the fresh team name): continue it (paused), attach to its lead (it runs with a terminal), stop it
    and start (a team of that name: continued when it has the same members, else started as OTHER), another name."""
    team = verdict.get("team") if isinstance(verdict.get("team"), dict) else {}
    found = []
    if verdict.get("ok") and verdict.get("continues"):
        found.append(("continue", tr("Continue the team")))
    if team.get("verdict") == "attach":
        found.append(("attach", tr("Attach to its lead")))
    states = team.get("states") if isinstance(team.get("states"), dict) else {}
    if verdict.get("code") == "exists" and any(state in IN_THE_WAY for state in states.values()):
        found.append(("stop", tr("Stop the team and start") if team.get("same") else
                      tr("Stop the team and start as {name}", name=printable(other))))
    found.append(("other", tr("Start as {name}", name=printable(other))))
    return found


class TeamStart:
    """A team start's session check (mixed in beside SessionStart): team start's dry run on the start's host said a
    team of that name is there (code "exists": it runs, or has other members, or a member is gone) or a member's
    session name is another start's ("taken"). Said before anything runs, with what can be done - Attach to its
    lead, Stop it and start, Start as another name, Cancel - as a single start's session_ask. A team of that name
    with the same members, stopped or paused, is no question: team start continues it (its verdict says ok)."""

    def team_ask(self, host, name, verdict, embedded, go_on, rename, go_on_continued=None):
        """Ask about VERDICT (team start's refusal for team NAME on HOST, or a paused team it continues) and do what the
        human chooses: GO_ON() starts again (team start's check first), RENAME(name) gives the team the fresh name
        first, GO_ON_CONTINUED() starts it as it is (Continue it). The answer's key, or None (Cancel)."""
        other = other_team_name(name)
        offered = team_choices(verdict, other)
        applog.log("INFO", "start", f"a team start as {printable(name)} on {printable(host)}: "
                   f"{verdict.get('code')} ({(verdict.get('team') or {}).get('verdict') or '-'}) - asked")
        answer = choose(self, tr("Team {name}", name=printable(name)), team_what(host, name, verdict), offered,
                        destructive=("stop",))
        team = verdict.get("team") if isinstance(verdict.get("team"), dict) else {}
        if answer == "continue" and go_on_continued is not None:
            go_on_continued()
        elif answer == "attach":
            lead = team.get("lead")
            seen = (team.get("seen") or {}).get(lead) if isinstance(team.get("seen"), dict) else None
            self.session_attach(host, f"{name}-{lead}", embedded, seen if isinstance(seen, dict) else None)
        elif answer == "other":
            rename(other)
            go_on()
        elif answer == "stop":
            self.team_stop_and_start(host, name, bool(team.get("same")), other, go_on, rename)
        else:
            self.session_notice(tr("Not started: a team named {name} is there already.", name=printable(name)),
                                role="notice-warn")
        return answer

    def team_stop_and_start(self, host, name, same, other, go_on, rename):
        """Stop team NAME on HOST (asked first: team-stop, as the Teams page's Stop…), then start: under its name when
        it has the same members (team start continues them with the options they were made with), else as OTHER."""
        if not ask_first(self, "team-stop", tr("Stop"), on_host(host, tr(
                "Stop team {name}? Every member's agent ends; what they did not save is lost (their HOMEs are "
                "kept).", name=printable(name))), tr("Stop")):
            return

        def stopped(result, error):
            status, _out, err = result if error is None and isinstance(result, tuple) else (1, "", str(error))
            if status != 0:
                warn(self, tr("Not stopped"), on_host(host, printable_text(err) or f"exit status {status}"))
                return
            if not same:
                rename(other)
            go_on()
        self.session_notice(on_host(host, tr("Stopping team {name}…", name=printable(name))))
        background(lambda: self.sai.team_stop(host, name), owner=self, work=True, timeout=360, done=stopped,
                   what=f"stopping the team {name} on {host}")
