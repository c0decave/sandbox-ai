"""What a start needs on its host before it can run - an identity, an image variant (made from its recipe and built),
a VPN profile, MCP servers - found and provided instead of left for the launcher to refuse: for the Board and the
Start page alike (every preset and profile). A page mixes Prerequisites in and gives prerequisite_argv() (its
launcher options), prerequisite_agent(), host() and, optionally, show_notice(text, role)."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import weakref

from . import applog, board as B, preparing as P
from .files import printable, printable_text
from .i18n import tr
from .widgets import background, confirm, warn

LOCAL = "local"  # this machine, as `sandbox-ai hosts` names it

# The slow part of preparing a start (an image built, MCP servers added: their images pulled), run in a terminal tab
# of the app so that it is seen as it goes: each step said with its number, its own output (podman's build and pull
# output bounded by sandbox-ai's progress.py), how long it took, and the end. Arguments: [STATUS] [[title, argv], ...]
# (the steps always last). With STATUS (a file) it also writes there what it sees of each step - its state, when it
# began, its last line and the last that named its step ("(stage 2/2, step 9/14)") - for the app's progress
# (preparing.Preparation.read); each step then runs on a pseudo-terminal of its own when the tab is a terminal (its
# questions and its way of printing as before), else through a pipe.
CHAIN = r"""
import json, os, re, subprocess, sys, time
*rest, steps = sys.argv[1:]
steps, begun, path = json.loads(steps), time.monotonic(), rest[0] if rest else None
state = [{"state": "waiting", "begun": None, "took": None, "status": None, "mark": "", "last": ""} for _ in steps]
MARK = re.compile(r"\bstep \d+/\d+\)")
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
written = [0.0]
def took(since):
    return f"{time.monotonic() - since:.0f} s"
def write(now=False):
    if not path or (not now and time.monotonic() - written[0] < 0.25):
        return
    written[0] = time.monotonic()
    try:
        with open(path + ".part", "w", encoding="utf-8") as out:
            json.dump({"steps": state}, out)
        os.replace(path + ".part", path)
    except OSError:
        pass
def watch(entry):
    rest = [""]
    def seen(data):
        *lines, rest[0] = re.split(r"[\r\n]", rest[0] + data.decode("utf-8", "replace"))
        for line in lines:
            line = ANSI.sub("", line).strip()
            if line:
                entry["last"] = line[-300:]
                if MARK.search(line):
                    entry["mark"] = line[-300:]
        write()
    return seen
def run(argv, entry):
    if not path:
        return subprocess.call(argv)
    seen = watch(entry)
    if os.name == "posix" and sys.stdin.isatty():
        import pty, shutil, signal
        if shutil.which(argv[0]) is None:
            raise OSError(f"{argv[0]}: no such command")
        master = []
        def size(*_):
            try:
                import fcntl, termios
                fcntl.ioctl(master[0], termios.TIOCSWINSZ, fcntl.ioctl(0, termios.TIOCGWINSZ, b"\0" * 8))
            except (OSError, IndexError, ImportError):
                pass
        def read(fd):
            if not master:
                master.append(fd)
                size()
            data = os.read(fd, 4096)
            seen(data)
            return data
        signal.signal(signal.SIGWINCH, size)
        return os.waitstatus_to_exitcode(pty.spawn(argv, read))
    process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               env={**os.environ, "PYTHONUNBUFFERED": "1"})
    while True:
        data = os.read(process.stdout.fileno(), 4096)
        if not data:
            break
        sys.stdout.buffer.write(data)
        sys.stdout.flush()
        seen(data)
    process.stdout.close()
    return process.wait()
for number, (title, argv) in enumerate(steps, 1):
    entry = state[number - 1]
    print(f"agent-sandbox: preparing the start ({number}/{len(steps)}): {title}…", flush=True)
    since = time.monotonic()
    entry["state"], entry["begun"] = "running", time.time()
    write(True)
    try:
        status = run(argv, entry)
    except OSError as exc:
        status = 127
        print(f"agent-sandbox: {exc}", flush=True)
    entry["took"], entry["status"] = round(time.monotonic() - since, 1), status
    if status != 0:
        entry["state"] = "failed"
        write(True)
        print(f"agent-sandbox: {title} failed (exit status {status}) after {took(since)}; the start waits", flush=True)
        sys.exit(status)
    entry["state"] = "done"
    write(True)
    print(f"agent-sandbox: {title}: done in {took(since)}", flush=True)
print(f"agent-sandbox: prepared in {took(begun)}; the start goes on", flush=True)
"""
# The same check of what a start needs, more often than this within LOOP_SECONDS, is a loop (a bug: in 0.1.95 the
# check remembered the wrong thing as done and ran again every 0.15 s, and Start did nothing): it is stopped, said.
LOOP_LIMIT, LOOP_SECONDS = 8, 5.0


def done_said():
    """How the end of preparing a start is told: by a notification only while notifications are on (Settings; off by
    default, when the page's notes alone say it)."""
    from .backend import setting
    return tr("A notification says when it is done.") if setting("notifications") else \
        tr("The notes here say when it is done.")


def start_mounts(argv, name):
    """The folders the start ARGV gives its MCP server NAME (--mcp-mount NAME:TARGET=HOST[:MODE]): [TARGET=HOST...]."""
    mounts = []
    for index, token in enumerate(argv):
        option, equals, value = token.partition("=")
        if option != "--mcp-mount":
            continue
        value = value if equals else (argv[index + 1] if index + 1 < len(argv) else "")
        server, colon, rest = value.partition(":")
        if colon and server == name and "=" in rest and not B.PLACEHOLDER.search(rest):
            mounts.append(rest)
    return mounts


def mcp_add_args(argv, name, folders=()):
    """`sandbox-ai mcp add NAME` for a server the start ARGV uses, with the folders the start gives it (--mcp-mount
    NAME:TARGET=HOST[:MODE]) and FOLDERS (TARGET=HOST, asked for: ask_mcp_folders) as its --mount: a catalog server
    that must have a folder (engram, memory, exiftool) is not added without one, and the start's is the one it
    asked for."""
    mounts = []
    for value in [*start_mounts(argv, name), *folders]:
        mounts += ["--mount", value]
    return ("mcp", "add", name, *mounts)


def required_folders(entries, name):
    """The folders (targets in the server's container) the catalog entry NAME ("binwalk", "hub:binwalk") requires,
    from ENTRIES (catalog_entries' [(prefix, entry)], or the entries of `mcp catalog --json`); None when no catalog
    read has it (nothing known: `mcp add` says what it misses)."""
    prefix, _, bare = str(name).rpartition(":")
    for item in entries or []:
        found_prefix, entry = item if isinstance(item, tuple) and len(item) == 2 else \
            ((item or {}).get("prefix"), item)
        if not isinstance(entry, dict) or entry.get("name") != bare or (prefix and found_prefix != prefix):
            continue
        return [m["target"] for m in entry.get("mounts", []) if isinstance(m, dict) and m.get("required")
                and isinstance(m.get("target"), str)]
    return None


def start_workspace(argv):
    """The start's (first) workspace of its options ARGV (--workspace PATH[:ro|rw]), or None."""
    for index, token in enumerate(argv):
        option, equals, value = str(token).partition("=")
        if option not in ("--workspace", "-w"):
            continue
        value = value if equals else (argv[index + 1] if index + 1 < len(argv) else "")
        value = value[:-3] if value.endswith((":ro", ":rw")) else value
        if value and not B.PLACEHOLDER.search(value):
            return value
    return None


def need_word(option, name, agent):
    """What the start needs (OPTION NAME, for AGENT) in words: "image variant tor of claude"."""
    return {"--identity": tr("identity {name}", name=name),
            "--image-variant": tr("image variant {name} of {agent}", name=name, agent=agent),
            "--vpn": tr("VPN profile {name}", name=name),
            "--mcp": tr("MCP server {name}", name=name)}.get(option, f"{option} {name}")


def busy_now(page):
    """True while the start PAGE shows is being prepared (its needs read, asked for or made)."""
    return page.prerequisite_context() in page.__dict__.get("preparing_contexts", set())


def needs_text(needs):
    """{option: [names]} for a log line: "--identity investigation, --image-variant media"."""
    return ", ".join(f"{option} {', '.join(printable(n) for n in names)}" for option, names in (needs or {}).items())


def agent_name(page):
    try:
        return page.prerequisite_agent()
    except Exception as exc:  # noqa: BLE001 - only for the log line
        return f"? ({type(exc).__name__})"


class Prerequisites:
    """The mixin (see the module's docstring)."""

    PREREQUISITES = ("--identity", "--image-variant", "--vpn", "--mcp")

    def board_needs(self):
        """{option: [names]} of what the start uses that must exist on its host first: --identity, --image-variant,
        --vpn, --mcp (placeholders left out: they are asked for first)."""
        argv = self.prerequisite_argv()
        own = set(self.prerequisite_own_servers())
        needs = {}
        for index, token in enumerate(argv):
            option, _, value = token.partition("=")
            if option in self.PREREQUISITES:
                value = value if "=" in token else (argv[index + 1] if index + 1 < len(argv) else "")
                if value and not B.PLACEHOLDER.search(value):
                    if option == "--mcp" and value in own:
                        continue  # the project's own server: defined by its project file, nothing to add
                    needs.setdefault(option, []).append(value)
        return needs

    def prerequisite_own_servers(self):
        """The MCP servers the start's project defines itself (none known: a page may say)."""
        return set()

    def prerequisite_context(self):
        """What the start is about (a board, a project, a host): a continuation made for another is dropped."""
        return self.host()

    def check_prerequisites(self, then=None, why="start"):
        """Everything the board needs that its host does not have yet - an identity, an image variant (made, or only
        not built), a VPN profile, an MCP server: a preset's "investigation" identity, "media" variant and Tor profile,
        say - read in the background, then provided (provide()). THEN runs after it all is there (the start goes on);
        nothing is left for the launcher to refuse. True while that is under way."""
        needs = self.board_needs()
        if not needs or not hasattr(self.sai, "host_image_variants"):
            applog.log("DEBUG", "prerequisites", f"{why}: nothing to check first (needs {needs or 'none'})")
            return False
        host = self.host()
        context = self.prerequisite_context()
        busy = self.__dict__.setdefault("preparing_contexts", set())
        stages = self.__dict__.setdefault("preparing_stages", {})  # {context: (since, what is under way)}
        if context in busy:  # (this start's needs are read or made already; another board or host is not held up)
            since, stage = stages.get(context, (None, "unknown"))
            applog.log("INFO", "prerequisites", f"{why} on {printable(host)} waits: what it needs is still being "
                       f"prepared for {printable(context)} ({stage}"
                       + (f", for {applog.seconds(time.monotonic() - since)}" if since else "") + ")")
            # What runs now and how far, as information (a Start pressed again is no warning), seen on the Board.
            self.prerequisite_notice(self.still_preparing(), "secondary", status=True, link=True)
            self.preparing_refresh(shown=True)
            return True
        key = (host, json.dumps(needs, sort_keys=True))
        if getattr(self, "prerequisites_done", None) == key:
            applog.log("DEBUG", "prerequisites", f"{why} on {printable(host)}: its needs were found there before: "
                       f"{needs_text(needs)}")
            return False
        if self.prerequisite_loop(key, needs):
            return True
        busy.add(context)  # from the read on: a second Start meanwhile starts nothing twice
        self.prerequisite_reading = (host, needs)
        stages[context] = (time.monotonic(), f"reading on {host} whether it has {needs_text(needs)}")
        applog.log("INFO", "prerequisites", f"{why} on {printable(host)} needs {needs_text(needs)}: read there first "
                                            f"(agent {agent_name(self)})")
        agent = self.prerequisite_agent()  # (the start's, also when the page shows another one by the answer)

        def same():
            return self.prerequisite_context() == context

        def go_on():
            """THEN, unless the page now shows another start (another board, project or host): never one the human
            did not start."""
            if then is not None and same():
                then()

        def read():
            """{option: {names there}} on HOST for the options of NEEDS ("--image-variant": {name: built}; None: not
            answered - the launcher says what is missing at the start)."""
            def variants():
                # {(name, agent): built}: each agent has variants of its own (the recipe media for claude and dsh);
                # a target is NAME, or NAME@AGENT where several agents have a variant of that name.
                data = self.sai.host_json(host, "images", "--json", timeout=90)
                return {(str(e["target"]).partition("@")[0], str(e.get("agent") or "")): bool(e.get("present"))
                        for e in data if isinstance(e, dict) and e.get("variant") and isinstance(e.get("target"), str)}
            readers = {"--identity": lambda: self.sai.identities() if host == LOCAL else self.sai.host_identities(host),
                       "--image-variant": variants,
                       "--vpn": lambda: [e["profile"] for e in self.sai.host_vpn_profiles(host)],
                       "--mcp": lambda: [row[0] for row in self.sai.host_mcp_servers(host)]}
            have = {}
            for option in needs:
                began = time.monotonic()
                try:
                    found = readers[option]()
                    have[option] = found if isinstance(found, dict) and option == "--image-variant" else set(found)
                    applog.log("DEBUG", "prerequisites", f"{option} on {host}: {len(have[option])} there, read in "
                                                         f"{applog.seconds(time.monotonic() - began)}")
                except Exception as exc:  # noqa: BLE001 - not answered: the launcher says what is missing at the start
                    applog.log("INFO", "prerequisites", f"{option} on {printable(host)} could not be read (after "
                               f"{applog.seconds(time.monotonic() - began)}; the launcher checks it at the start): "
                               f"{applog.error_text(exc)}")
                    have[option] = None
            return have

        def done(have, error):
            self.prerequisite_reading = None
            if not same():  # the page shows another start now: nothing is asked or made for this one
                applog.log("INFO", "prerequisites", f"{why}: the page shows another start now - nothing is made or "
                                                    f"started for {printable(context)}")
                busy.discard(context)
                stages.pop(context, None)
                return
            if error is not None:
                applog.log("WARNING", "prerequisites", f"{why}: what {printable(host)} has could not be read: "
                           f"{applog.error_text(error)} - the start goes on, the launcher checks it")
            complete = error is None and isinstance(have, dict) and all(have.get(o) is not None for o in needs)
            if error is not None or not isinstance(have, dict):
                have = {}
            missing, unbuilt = {}, []
            if isinstance(have.get("--image-variant"), dict):  # whose variant each name is (variant_target)
                self.variant_owners = {}
                for name, owner in have["--image-variant"]:
                    self.variant_owners.setdefault(name, set()).add(owner)
            for option, names in needs.items():
                there = have.get(option)
                if there is None:
                    continue
                for name in names:
                    # (not `key`: that is what check_prerequisites remembers as done - overwritten here, every start
                    # with a prerequisite read its needs again and started itself again, without end)
                    wanted = (name, agent) if option == "--image-variant" else name
                    if option == "--image-variant" and wanted not in there and (name, "") in there:
                        wanted = (name, "")  # (a host whose listing names no agent: its variant of that name)
                    if wanted not in there:
                        missing.setdefault(option, []).append(name)
                    elif option == "--image-variant" and not there[wanted]:
                        unbuilt.append(name)
            self.prerequisite_report(host, agent, key, needs, have, missing, unbuilt)
            if not missing and not unbuilt:
                if complete:  # (one that could not be read is read again at the next start)
                    self.prerequisites_done = key
                applog.log("INFO", "prerequisites", f"{why} on {printable(host)}: everything it needs is there"
                           + ("" if complete else " (as far as it could be read)")
                           + ("; the start goes on" if then is not None and same() else ""))
                busy.discard(context)
                stages.pop(context, None)
                go_on()
                return
            applog.log("INFO", "prerequisites", f"{why} on {printable(host)}: missing "
                       f"{needs_text(missing) or 'nothing'}"
                       + (f"; image variants not built: {', '.join(unbuilt)}" if unbuilt else "")
                       + " - provided first")
            stages[context] = (time.monotonic(), "asking and providing what is missing")
            self.provide(missing, unbuilt, host, key, go_on if then is not None else None, why, context, agent)
        background(read, owner=self, timeout=180, done=done,
                   what=f"the prerequisites check: whether {host} has {needs_text(needs)}")
        return True

    def provide(self, missing, unbuilt, host, key, then, why, context=None, agent=None, yes=()):
        """Make what MISSING ({option: [names]}) lacks on HOST: identities at once (empty: no logins, none of yours);
        image variants of a recipe made, and built with those only defined (UNBUILT) - after one question, in the
        background, the output in the log window; a VPN profile as a Tor profile (nothing to fill in) or on the VPN
        page; MCP servers added from their catalog. Then THEN, once everything is there. YES: what is made without
        its question ("build", "mcp", "vpn": a button of the Board's note said so already)."""
        agent = agent or self.prerequisite_agent()
        argv = self.prerequisite_argv()  # (the folders the start gives its MCP servers: --mcp-mount)
        context = context if context is not None else self.prerequisite_context()
        busy = self.__dict__.setdefault("preparing_contexts", set())
        recipes = set()
        try:
            recipes = set(self.sai.module("images").RECIPES)
        except Exception as _exc:  # noqa: BLE001 - an older sandbox-ai: no recipes known here
            applog.swallowed("prerequisites", "Prerequisites.provide: an older sandbox-ai: no recipes known here", _exc)
        identities = list(missing.get("--identity", []))
        variants = [v for v in missing.get("--image-variant", []) if v in recipes]
        no_recipe = [v for v in missing.get("--image-variant", []) if v not in recipes]
        vpns, servers = list(missing.get("--vpn", [])), list(missing.get("--mcp", []))
        # [(what is not made, [(button, function)])]: said, and on the Board each with the button that makes it.
        problems = []

        def again(what, names, *, built=()):
            """The button's function: NAMES of WHAT made now, without asking again (YES)."""
            page = weakref.ref(self)

            def make():
                me = page()
                if me is None:
                    return
                if busy_now(me):  # (a click while it is made already: what runs is said)
                    me.prerequisite_notice(me.still_preparing(), "secondary", status=True, link=True)
                    return
                me.provide({what: list(names)} if what else {}, list(built), host, None, None, "action",
                           me.prerequisite_context(), agent, yes={"build", "mcp", "vpn"})
            return make
        if no_recipe:
            problems.append((tr("Image variant {names}: no recipe of that name - make it on the Images page "
                                "(sandbox-ai images new NAME --from {agent}).", names=", ".join(no_recipe),
                                agent=agent), [(tr("Open the Images page"), lambda: self.open_page("images"))]))
        tor_profiles, vpn_page = [], []
        for name in vpns:
            choice = "tor" if "vpn" in yes else self.ask_vpn_kind(name, host)
            (tor_profiles if choice == "tor" else vpn_page).append(name)
        if vpn_page:
            problems.append((tr("VPN profile {names}: make it on the VPN page (it needs its keys or login), then "
                                "start again.", names=", ".join(vpn_page)),
                             [(tr("Make a Tor profile"), again("--vpn", vpn_page)),
                              (tr("Open the VPN page"), lambda: self.open_page("credentials", "vpn"))]))
        to_build = list(dict.fromkeys(unbuilt + variants))
        if to_build and "build" not in yes and not confirm(self, tr("Image variant"), tr(
                "This start needs the image variant {names}{made}. Build it now? It takes some minutes and room on "
                "the disk; the output goes to the log window, and the start goes on when it is built.",
                names=", ".join(to_build), made=tr(" (made from its recipe first)") if variants else ""),
                tr("Build")):
            problems.append((tr("Image variant {names}: not built (sandbox-ai build --variant NAME).",
                                names=", ".join(to_build)),
                             [(tr("Build now"), again("--image-variant", variants,
                                                      built=[n for n in to_build if n not in variants]))]))
            to_build = []
        if servers and "mcp" not in yes and not confirm(self, tr("MCP servers"), tr(
                "This start uses the MCP servers {names}, which are not added on {host}. Add them from their catalogs "
                "now (their images are pulled)?", names=", ".join(servers), host=host), tr("Add")):
            problems.append((tr("MCP servers {names}: not added (the MCP page adds them from a catalog).",
                                names=", ".join(servers)), [(tr("Add MCP server"), again("--mcp", servers))]))
            servers = []
        def read_catalog():  # (in the background: the catalogs are read over the network, or on the host)
            if host == LOCAL:
                return self.sai.mcp_catalog()[0]
            data = self.sai.host_json(host, "mcp", "catalog", "--json", timeout=150)
            return data.get("entries", []) if isinstance(data, dict) else []
        if servers:
            # A catalog server that must have a folder (exiftool's /work, binwalk's): the start's own (--mcp-mount),
            # else asked for (the start's workspace offered) - read from the catalogs in the background first.
            busy.add(context)

            def catalog_read(entries, error):
                if self.prerequisite_context() != context:
                    applog.log("INFO", "prerequisites", f"{why}: the page shows another start now - the MCP servers "
                                                        f"{', '.join(servers)} are not added for {printable(context)}")
                    busy.discard(context)
                    self.__dict__.setdefault("preparing_stages", {}).pop(context, None)
                    return
                if error is not None:
                    applog.log("WARN", "board", f"the MCP catalogs could not be read for the folders of "
                                                f"{', '.join(servers)}: {error}")
                folders, dropped = self.ask_mcp_folders(host, servers, argv, None if error is not None else entries)
                self.provide_rest(identities, variants, to_build, tor_profiles,
                                  [n for n in servers if n not in dropped], folders,
                                  problems + [(text, [(tr("Add MCP server"), again("--mcp", [name]))])
                                              for name, text in dropped.items()],
                                  host, key, then, why, context, agent, argv)
            self.__dict__.setdefault("preparing_stages", {})[context] = (
                time.monotonic(), f"reading the MCP catalogs for the folders of {', '.join(servers)}")
            background(read_catalog, owner=self, timeout=200, done=catalog_read,
                       what=f"reading the MCP catalogs of {host} for the servers {', '.join(servers)}")
            return
        self.provide_rest(identities, variants, to_build, tor_profiles, servers, {}, problems, host, key, then, why,
                          context, agent, argv)

    def ask_mcp_folders(self, host, servers, argv, entries):
        """({server: [TARGET=HOST]}, {server: why it is not added}) for the folders the catalog entries of SERVERS
        require (ENTRIES: the catalogs read, None: not read) that the start ARGV does not give them (--mcp-mount):
        each asked for - the start's workspace, another folder (chosen; on another host typed), or Cancel (that server
        is not added, and said)."""
        from PySide6.QtWidgets import QInputDialog
        from .state import file_dialog, run_dialog
        from .widgets import choose, shown_as_text
        folders, dropped = {}, {}
        workspace = start_workspace(argv)
        for server in servers:
            required = required_folders(entries, server) if entries is not None else None
            given = {value.partition("=")[0] for value in start_mounts(argv, server)}
            for target in [t for t in required or [] if t not in given]:
                title = tr("MCP server {name}", name=printable(server))
                choices = ([("workspace", tr("The start's workspace: {path}", path=printable(workspace)))]
                           if workspace else []) + [("other", tr("Another folder…"))]
                answer = choose(self, title, tr(
                    "The MCP server {name} needs a folder of {host} for {target} in its container (what it works on). "
                    "Which folder does it get?", name=printable(server), host=printable(host),
                    target=printable(target)), choices)
                path = workspace if answer == "workspace" else None
                if answer == "other" and host == LOCAL:
                    chosen = run_dialog(file_dialog(self, title, workspace or "", "folder"))
                    path = chosen[0] if chosen else None
                elif answer == "other":
                    text, ok = QInputDialog.getText(self, title, shown_as_text(tr("A folder on {host}",
                                                                                  host=printable(host))),
                                                    text=workspace or "")
                    path = text.strip() if ok and text.strip() else None
                if not path:
                    dropped[server] = tr("MCP server {name}: not added - it needs a folder for {target} (the MCP page "
                                         "adds it with one).", name=printable(server), target=printable(target))
                    folders.pop(server, None)
                    break
                folders.setdefault(server, []).append(f"{target}={path}")
                applog.log("INFO", "board", f"MCP server {server}: {target} gets the folder {path}")
        return folders, dropped

    def provide_rest(self, identities, variants, to_build, tor_profiles, servers, folders, problems, host, key, then,
                     why, context, agent, argv):
        """provide(), once the MCP servers' folders are known (FOLDERS: {server: [TARGET=HOST]}). PROBLEMS: [(what
        is not made, [(button, function)])]."""
        busy = self.__dict__.setdefault("preparing_contexts", set())
        problems = [p if isinstance(p, tuple) else (p, []) for p in problems]
        stages = self.__dict__.setdefault("preparing_stages", {})
        if problems and why == "start":
            warn(self, tr("Not started"), "\n".join(text for text, _ in problems))
            self.show_missing(tr("Not started: the start needs what is not there yet."), problems)
            if not (identities or variants or to_build or tor_profiles or servers):
                applog.log("INFO", "prerequisites", f"{why}: not started, nothing can be made for it here")
                busy.discard(context)
                stages.pop(context, None)
                return
            then = None  # what can be made is made; the start waits for the rest
            applog.log("INFO", "prerequisites", f"{why}: what can be made is made; the start itself waits for the rest")
        elif problems:
            warn(self, tr("Preparing the start"), "\n".join(text for text, _ in problems))
            self.show_missing(tr("Not made for the start:"), problems)
        busy.add(context)
        steps = [*(tr("identity {name}", name=n) for n in identities),
                 *(tr("image variant {name} (made and built: some minutes)", name=n) for n in to_build),
                 *(tr("image variant {name}", name=n) for n in variants if n not in to_build),
                 *(tr("Tor profile {name}", name=n) for n in tor_profiles),
                 *(tr("MCP server {name}", name=n) for n in servers)]
        if not steps:  # nothing to make here (each part said above why): no "Preparing: ." whose end never came
            applog.log("INFO", "prerequisites", f"{why}: nothing is made (each part declined or not possible)")
            busy.discard(context)
            stages.pop(context, None)
            return
        stages[context] = (time.monotonic(), "making " + ", ".join(steps))
        applog.log("INFO", "prerequisites", f"{why} on {printable(host)}: making " + printable(", ".join(steps)))
        self.prerequisite_notice(tr("Preparing what the start needs: {list}.", list=printable(", ".join(steps))) + " "
                                 + done_said(), "secondary", toast=True)
        # The builds and MCP servers (minutes) in a terminal tab, seen as they go; the rest (seconds) here.
        slow = [("build", name, tr("building the image variant {name}", name=name),
                 ("build", "--variant", self.variant_target(name, agent))) for name in to_build] + \
            [("mcp", name, tr("adding the MCP server {name} (its image is pulled)", name=name),
              mcp_add_args(argv, name, folders.get(name, ()))) for name in servers]
        in_tab = self.prerequisite_terminal() if slow else None
        if in_tab is not None:
            to_build, servers = [], []
        # What is made, in the order it is made (work() here, then the tab): the Board's progress and its line.
        order = [*(("identity", n) for n in identities), *(("recipe", n) for n in variants),
                 *(("build", n) for n in to_build), *(("vpn", n) for n in tor_profiles),
                 *(("mcp", n) for n in servers)]
        tab = [(kind, name) for kind, name, _, _ in slow] if in_tab is not None else []
        preparation = P.Preparation([P.Item(kind, name) for kind, name in order] +
                                    [P.Item(kind, name, in_tab=True) for kind, name in tab])
        index_of = {(item.kind, item.name): i for i, item in enumerate(preparation.items) if not item.in_tab}
        self.track(preparation, problems)

        def work():
            def run(*args, timeout=600):
                began = time.monotonic()
                status, out, err = self.sai.host_run(host, *args, timeout=timeout)
                text = (out + err).strip()
                applog.log("INFO" if status == 0 else "ERROR", "board", f"sandbox-ai {applog.argv_text(args)} on "
                           f"{printable(host)}: exit {status} after {applog.seconds(time.monotonic() - began)}\n"
                           + applog.redact(text[-20000:]))
                return status, text

            def step(kind, name, *args, timeout):
                """One item made here (from this thread: the page's timer shows how far)."""
                index = index_of.get((kind, name))
                if index is not None and preparation.items[index].state == "waiting":
                    preparation.start(index)
                status, text = run(*args, timeout=timeout)
                if index is not None:
                    if text:
                        preparation.line(index, text.splitlines()[-1])
                    preparation.finish(index, status == 0, status)
                return status, text
            said = []
            for name in identities:
                said.append(("identity", name, *step("identity", name, "identities", "add",
                                                     "--description=made for a start (no logins)", "--",
                                                     name, timeout=60)))
            for name in variants:
                said.append(("variant", name, *step("recipe", name, "images", "recipe", name, "--from", agent,
                                                    timeout=120)))
            for name in to_build:
                if any(kind == "variant" and n == name and status != 0 for kind, n, status, _ in said):
                    continue
                said.append(("build", name, *step("build", name, "build", "--variant",
                                                  self.variant_target(name, agent), timeout=3600)))
            for name in tor_profiles:
                index = index_of.get(("vpn", name))
                if index is not None:
                    preparation.start(index)
                status, text = run("vpn", "add", name, "--tor", "--socks", timeout=120)
                if status != 0 and "build tor" in text:  # the Tor VPN's image first, then the profile again
                    if index is not None:
                        preparation.items[index].detail = tr("building the Tor VPN's image first")
                    said.append(("build", "tor (VPN)", *run("build", "tor", timeout=3600)))
                    status, text = run("vpn", "add", name, "--tor", "--socks", timeout=120)
                if index is not None:
                    preparation.finish(index, status == 0, status)
                said.append(("vpn", name, status, text))
            for name in servers:
                said.append(("mcp", name, *step("mcp", name, *mcp_add_args(argv, name, folders.get(name, ())),
                                                timeout=900)))
            return said

        def quick_done(said, error):
            """The quick part made: the slow one in its terminal tab (made() at its end), or the end now."""
            if error is not None or in_tab is None or any(status != 0 for _, _, status, _ in said):
                made(said, error)
                return
            steps_ = [[title, self.sai.command(*args) if host == LOCAL else self.sai.host_terminal(host, *args)]
                      for _, _, title, args in slow]
            argv = self.chain_argv(preparation, steps_)

            def ended(status):
                applog.log("INFO" if status == 0 else "ERROR", "prerequisites", f"{why}: the terminal tab Preparing "
                           f"the start ended with exit status {status}: " + ", ".join(t for _, _, t, _ in slow))
                text = "" if status == 0 else tr("see the terminal tab Preparing the start (exit status {status})",
                                                 status=status)
                made(said + [(kind, name, 0 if status == 0 else status, text) for kind, name, _, _ in slow], None)
            stages[context] = (time.monotonic(), "in the terminal tab Preparing the start: "
                               + ", ".join(t for _, _, t, _ in slow))
            applog.log("INFO", "prerequisites", f"{why}: the slow part runs in the terminal tab Preparing the start: "
                       + ", ".join(t for _, _, t, _ in slow))
            if not in_tab(argv, tr("Preparing the start"), ended=ended):
                applog.log("WARNING", "prerequisites", f"{why}: the terminal tab Preparing the start did not open")
                ended(-1)

        def made(said, error):
            busy.discard(context)
            self.untrack(preparation, error is None and all(status == 0 for _, _, status, _ in said))
            stages.pop(context, None)
            applog.log("INFO" if error is None and all(s == 0 for _, _, s, _ in said) else "WARNING", "prerequisites",
                       f"{why}: preparing ended" + (f" with an error: {applog.error_text(error)}" if error is not None
                                                    else ": " + "; ".join(f"{kind} {name}: exit {status}"
                                                                          for kind, name, status, _ in said)))
            if error is not None:  # (the end told as promised, as for a part that failed)
                self.prerequisite_notice(tr("Not everything could be made for the start."), "notice-error", toast=True)
                warn(self, tr("Preparing the start"), printable_text(error))
                return
            words = {"identity": tr("identity"), "variant": tr("image variant"), "build": tr("built"),
                     "vpn": tr("Tor profile"), "mcp": tr("MCP server")}
            ok = [f"{words[kind]} {name}" for kind, name, status, _ in said if status == 0]
            failed = [f"{words[kind]} {name}: {printable(text.splitlines()[-1] if text else '')[-300:]}"
                      for kind, name, status, text in said if status != 0]
            if ok:
                self.prerequisite_notice(tr("Made for the start: {list}.", list=printable(", ".join(ok))), "notice-ok",
                                         toast=not failed)
            if failed:
                self.prerequisite_notice(tr("Not everything could be made for the start."), "notice-error", toast=True)
                warn(self, tr("Preparing the start"), tr("Not everything could be made (the log window has the output):") +
                     "\n" + "\n".join(failed))
                return
            self.prerequisites_done = None  # read again: what was made is there now
            if self.prerequisite_context() != context:
                applog.log("INFO", "prerequisites", f"{why}: made, but the page shows another start now - not started")
                return  # another start is shown now: nothing more is made or started for this one
            # (a board edited meanwhile to need nothing any more starts at once)
            if then is not None and not self.check_prerequisites(then=then, why=why):
                then()
        background(work, owner=self, work=True, timeout=4 * 3600, done=quick_done,
                   what=f"preparing the start on {host}: " + ", ".join(steps))

    # -- what the check found, and what is prepared now (user report 2026-11-14) --

    PREREQUISITE_LOG = "start"  # where the check's results are logged (the Board's: "board")

    def prerequisite_report(self, host, agent, key, needs, have, missing, unbuilt):
        """Each check's result in plain words at INFO, once per change: what the start NEEDS on HOST, what is there,
        what is missing (MISSING, UNBUILT), what could not be read (asked again at the next start)."""
        wanted, there, lacking, unknown = [], [], [], []
        for option, names in needs.items():
            for name in names:
                word = need_word(option, name, agent)
                wanted.append(word)
                if have.get(option) is None:
                    unknown.append(word)
                elif name in missing.get(option, []):
                    lacking.append(word)
                elif option == "--image-variant" and name in unbuilt:
                    lacking.append(tr("{thing} (not built)", thing=word))
                else:
                    there.append(word)
        text = tr("The start needs on {host}: {list}.", host=host, list=", ".join(wanted))
        if there:
            text += " " + tr("There: {list}.", list=", ".join(there))
        if lacking:
            text += " " + tr("Missing: {list}.", list=", ".join(lacking))
        if unknown:
            text += " " + tr("Not answered (read again at the next start): {list}.", list=", ".join(unknown))
        self.__dict__.setdefault("prerequisite_lacking", {})[key] = lacking or unknown
        text = printable(text)
        if text != self.__dict__.get("prerequisite_said"):
            self.prerequisite_said = text
            applog.log("INFO", self.PREREQUISITE_LOG, text)

    def prerequisite_loop(self, key, needs):
        """True (and said, as an error the Board shows) when the check of KEY came back LOOP_LIMIT times within
        LOOP_SECONDS: a loop, whatever its cause - nothing is read or started then, until the checks are older."""
        now = time.monotonic()
        checks = self.__dict__.setdefault("prerequisite_checks", {})
        recent = [at for at in checks.get(key, []) if now - at < LOOP_SECONDS]
        if len(recent) < LOOP_LIMIT:
            checks[key] = recent + [now]
            return False
        checks[key] = recent
        agent = self.prerequisite_agent()
        items = self.__dict__.get("prerequisite_lacking", {}).get(key) or \
            [need_word(option, name, agent) for option, names in needs.items() for name in names]
        text = tr("The check of what the start needs came back {count} times within {seconds} s and is stopped (a "
                  "loop, a fault of the app): {list}. Nothing is started; press Start again in a moment - the log "
                  "has each check.", count=len(recent), seconds=int(LOOP_SECONDS), list=printable(", ".join(items)))
        self.prerequisite_notice(text, "notice-error")
        self.show_missing(text, [], role="notice-error")
        return True

    def still_preparing(self):
        """What a Start pressed while the start is prepared is told: what runs now, how far, what comes next."""
        preparation = self.__dict__.get("preparation")
        if preparation and not preparation.finished():
            preparation.read()
            return preparation.summary(still=True) + tr(". The start goes on when it is done.")
        reading = self.__dict__.get("prerequisite_reading")
        if reading:
            host, needs = reading
            agent = self.prerequisite_agent()
            return tr("Still checking on {host} what the start needs: {list}.", host=printable(host), list=printable(
                ", ".join(need_word(option, name, agent) for option, names in needs.items() for name in names)))
        return tr("Still preparing what the start needs: a question about it is open, or the MCP catalogs are "
                  "read for the folders of its MCP servers.")

    def chain_argv(self, preparation, steps):
        """The terminal tab's command for STEPS ([[title, argv]]): CHAIN, writing its status to a file of a private
        folder that PREPARATION reads (none when no folder can be made: it runs all the same)."""
        argv = [sys.executable, "-I", "-c", CHAIN, json.dumps(steps)]
        try:
            folder = tempfile.mkdtemp(prefix="sandbox-ai-app-preparing.")
        except OSError:
            return argv
        preparation.status = os.path.join(folder, "status.json")
        preparation.tab_argv = argv = P.chain_status_argv(argv, preparation.status)
        return argv

    def track(self, preparation, problems=()):
        """PREPARATION is what the start's preparation does from now on: shown (the Board's panel) and logged once per
        item, read again twice a second. PROBLEMS: what is not made (shown again once it is done)."""
        self.preparation, self.last_preparation = preparation, preparation
        self.preparing_logged, self.preparing_problems = None, list(problems)
        timer = self.__dict__.get("preparing_timer")
        if timer is None:
            from PySide6.QtCore import QTimer
            timer = self.preparing_timer = QTimer(self)
            timer.setInterval(500)
            timer.timeout.connect(self.preparing_tick)
        timer.start()
        self.preparing_refresh(shown=True)

    def untrack(self, preparation, ok):
        """PREPARATION is over (OK: everything made): its last state shown (a failure stays, its line opens where it
        ran), its status file removed."""
        preparation.read()
        for index, item in enumerate(preparation.items):
            if ok and item.state != "done":
                preparation.finish(index, True, 0)
            elif not ok and item.state == "running":
                preparation.finish(index, False)
        if not ok and preparation.failed() is None:  # (the tab ended before its first step said anything)
            waiting = next((i for i, item in enumerate(preparation.items) if item.state == "waiting"), None)
            if waiting is not None:
                preparation.finish(waiting, False)
        if preparation.status:
            shutil.rmtree(os.path.dirname(preparation.status), ignore_errors=True)
            preparation.status = None
        if self.__dict__.get("preparation") is preparation:
            self.preparation = None
            timer = self.__dict__.get("preparing_timer")
            if timer is not None:
                timer.stop()
        panel = getattr(self, "preparing_panel", None)
        if not ok:
            self.prerequisite_notice(preparation.summary(timed=False), "notice-error", link=True)
            if panel is not None:
                panel.show_preparation(preparation)
        elif panel is not None and self.__dict__.get("preparation") is None:
            problems = self.__dict__.get("preparing_problems") or []
            if problems:
                self.show_missing(tr("Not started: the start needs what is not there yet."), problems)
            else:
                panel.done()

    def preparing_tick(self):
        """Twice a second while a start is prepared: the tab's status read, the panel and the log brought up to it."""
        preparation = self.__dict__.get("preparation")
        if preparation is None:
            timer = self.__dict__.get("preparing_timer")
            if timer is not None:
                timer.stop()
            return
        preparation.read()
        self.preparing_refresh()

    def preparing_refresh(self, shown=False):
        """The panel (SHOWN: also when it was closed) and, once per item that begins, the log: what is prepared."""
        preparation = self.__dict__.get("preparation")
        if preparation is None:
            return
        panel = getattr(self, "preparing_panel", None)
        if panel is not None and (shown or not getattr(panel, "dismissed", False)):
            panel.dismissed = False
            panel.show_preparation(preparation)
        key = preparation.changed_key()
        if key != self.__dict__.get("preparing_logged") and not preparation.finished():
            self.preparing_logged = key
            self.prerequisite_notice(preparation.summary(timed=False), "secondary", link=True)

    def show_missing(self, text, rows, role="notice-warn"):
        """TEXT and ROWS [(what is not there, [(button, function)])] on the page's panel (the Board's), not only in
        the log: why a start did not go on, and what makes it."""
        panel = getattr(self, "preparing_panel", None)
        if panel is not None:
            panel.dismissed = False
            panel.show_missing(printable(text), [(printable(what), list(actions)) for what, actions in rows], role)

    def show_preparing_where(self):
        """Where the start's preparation runs or ran: its terminal tab (the Terminals page), else the log window."""
        window = self.window()
        preparation = self.__dict__.get("preparation") or self.__dict__.get("last_preparation")
        terminals = getattr(window, "terminals", None) if window is not self else None
        if preparation is not None and preparation.tab_argv and terminals is not None:
            for view in terminals.views():
                if list(getattr(view, "argv", []) or []) == list(preparation.tab_argv):
                    show = getattr(window, "show_page", None)
                    if callable(show):
                        show("terminals")
                    terminals.tabs.setCurrentWidget(view)
                    view.setFocus()
                    return "tab"
        opener = getattr(window, "open_log_window", None)
        if callable(opener):
            opener()
            return "log"
        return None

    def open_page(self, key, tab=None):
        """The window's page KEY (and its tab TAB: the Credentials page's "vpn")."""
        window = self.window()
        show = getattr(window, "show_page", None)
        if not callable(show):
            return False
        show(key)
        if tab:
            page = getattr(window, key, None)
            index = page.tab_of(tab) if page is not None and hasattr(page, "tab_of") else -1
            if index >= 0:
                page.tabs.setCurrentIndex(index)
        return True

    def variant_target(self, name, agent):
        """The variant NAME of AGENT as `sandbox-ai build --variant` takes it: NAME, or NAME@AGENT when another agent
        has a variant of that name too (as the host's images listing said: variant_owners)."""
        owners = getattr(self, "variant_owners", None) or {}
        return f"{name}@{agent}" if owners.get(name, set()) - {agent} else name

    def prerequisite_terminal(self):
        """in_terminal(argv, title, ended=...) of the window when its commands run in a terminal tab of the app (the
        setting terminal "app", QtWebEngine and xterm.js there), else None: the slow part runs in the background then,
        its output in the log window."""
        window = self.window()
        in_terminal = getattr(window, "in_terminal", None)
        if not callable(in_terminal):
            return None
        try:
            from . import terminal
            if self.sai.settings().get("terminal", "app") != "app" or not terminal.available()[0]:
                return None
        except Exception as _exc:  # noqa: BLE001 - no terminal tabs: in the background
            applog.swallowed("prerequisites", "Prerequisites.prerequisite_terminal: no terminal tabs: in the ba…", _exc)
            return None
        return lambda argv, title, ended: in_terminal(argv, title, embedded=True, ended=ended)

    def ask_vpn_kind(self, name, host):
        """"tor" when the missing VPN profile NAME is to be made as a Tor profile now (nothing to fill in), else None
        (made on the VPN page: a WireGuard, OpenVPN, Mullvad or Tailscale profile needs its keys or login)."""
        from . import widgets as W
        if W.AUTO_ANSWER is not None:
            return "tor" if W.AUTO_ANSWER else None
        from PySide6.QtWidgets import QMessageBox
        box = QMessageBox(QMessageBox.Icon.Question, tr("VPN profile {name}", name=printable(name)), tr(
            "This start goes through the VPN profile {name}, which is not on {host}. A Tor profile needs nothing "
            "filled in and is made now; another kind (WireGuard, OpenVPN, Mullvad, Tailscale) needs its keys or login "
            "on the VPN page.", name=printable(name), host=printable(host)), parent=self)
        # The name comes from the board, a preset or a project's .sandbox-ai.json: never drawn as markup.
        from PySide6.QtCore import Qt
        box.setTextFormat(Qt.TextFormat.PlainText)
        tor = box.addButton(tr("Make a Tor profile"), QMessageBox.ButtonRole.AcceptRole)
        box.addButton(tr("I make it on the VPN page"), QMessageBox.ButtonRole.RejectRole)
        W.exec_dropped(box)
        chosen = "tor" if box.clickedButton() is tor else None
        applog.log("INFO", "prerequisites", f"VPN profile {printable(name)} missing on {printable(host)}: "
                                            f"{'a Tor profile is made' if chosen else 'left for the VPN page'}")
        return chosen


    def prerequisite_notice(self, text, role="notice-warn", toast=False, status=False, link=False):
        """TEXT into the page's notes (and so the log window); with TOAST also as a notification: preparing a start
        takes minutes and goes on in the background, its beginning and its end are told where they are seen. STATUS:
        also in the window's status line; LINK: a click on it in the log window opens where the preparation runs."""
        shown = getattr(self, "show_notice", None)
        if callable(shown):
            shown(text, role)
        else:
            applog.log({"notice-warn": "WARNING", "notice-error": "ERROR"}.get(role, "INFO"), "start", text)
        if link:
            try:
                from . import logview
                where = weakref.WeakMethod(self.show_preparing_where)
                logview.link(text, lambda: (where() or (lambda: None))())
            except Exception:  # noqa: BLE001 - no log window (no Qt): the text alone
                pass
        bar = getattr(self.window(), "statusBar", None) if status and self.window() is not self else None
        if callable(bar):
            try:
                bar().showMessage(text, 15000)
            except Exception:  # noqa: BLE001
                pass
        notify = getattr(self.window(), "notify", None) if toast else None
        if callable(notify):
            level = {"notice-ok": "success", "notice-error": "error", "notice-warn": "warning"}.get(role, "info")
            try:
                notify(tr("Preparing the start"), text, level=level)
            except Exception as _exc:  # noqa: BLE001 - a notification that cannot be shown: the log has it
                applog.swallowed("prerequisites", "Prerequisites.prerequisite_notice: a notification that canno…", _exc)
