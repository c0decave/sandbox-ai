"""What a start needs on its host before it can run - an identity, an image variant (made from its recipe and built),
a VPN profile, MCP servers - found and provided instead of left for the launcher to refuse: for the Board and the
Start page alike (every preset and profile). A page mixes Prerequisites in and gives prerequisite_argv() (its
launcher options), prerequisite_agent(), host() and, optionally, show_notice(text, role)."""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import time
import weakref

from . import applog, board as B, preparing as P
from .files import printable, printable_text
from .i18n import tr
from .widgets import ask_first, background, confirm, warn

LOCAL = "local"  # this machine, as `sandbox-ai hosts` names it

# The slow part of preparing a start (an image built, MCP servers added: their images pulled), run in a terminal tab
# of the app so that it is seen as it goes: each step said with its number, its own output (podman's build and pull
# output bounded by sandbox-ai's progress.py), how long it took, and the end. Arguments: [--end=TEXT] [STATUS]
# [[title, argv], ...] (the steps always last; TEXT: what its last line says comes next - which tab the start goes on
# in, or that nothing starts by itself). With STATUS (a file) it also writes there what it sees of each step - its
# state, when it began, its last line and the last that named its step ("(stage 2/2, step 9/14)") - for the app's
# progress (preparing.Preparation.read); each step then runs on a pseudo-terminal of its own when the tab is a
# terminal (its questions and its way of printing as before), else through a pipe.
CHAIN = r"""
import json, os, re, subprocess, sys, time
*rest, steps = sys.argv[1:]
end = next((word[len("--end="):] for word in rest if word.startswith("--end=")), "the start goes on")
rest = [word for word in rest if not word.startswith("--end=")]
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
print(f"agent-sandbox: prepared in {took(begun)}; {end}", flush=True)
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


# Where a sandbox sees its folders (mcp.SANDBOX_PLACES): a start's --mcp-mount whose folder is one of these and that this
# machine does not have is the agent's view of a folder, never handed to `mcp add` as this machine's (user report
# 2026-11-20: "--mount /work=/work:rw: /work is not there and cannot be created"); the folder is asked for instead.
SANDBOX_PLACES = ("/work", "/workspaces")


def sandbox_path(folder):
    """True for FOLDER (an --mcp-mount's HOST, MODE stripped) that is where a sandbox sees a folder and is not a folder
    of this machine."""
    folder = str(folder)[:-3] if str(folder).endswith((":ro", ":rw")) else str(folder)
    return any(folder == place or folder.startswith(place + "/") for place in SANDBOX_PLACES) and \
        not os.path.isdir(folder)


def start_mounts(argv, name, host=None):
    """The folders the start ARGV gives its MCP server NAME (--mcp-mount NAME:TARGET=HOST[:MODE]): [TARGET=HOST...].
    HOST LOCAL: without one whose folder is a sandbox's view of a folder this machine does not have (sandbox_path)."""
    mounts = []
    for index, token in enumerate(argv):
        option, equals, value = token.partition("=")
        if option != "--mcp-mount":
            continue
        value = value if equals else (argv[index + 1] if index + 1 < len(argv) else "")
        server, colon, rest = value.partition(":")
        if colon and server == name and "=" in rest and not B.PLACEHOLDER.search(rest) and rest.partition("=")[2]:
            if host == LOCAL and sandbox_path(rest.partition("=")[2]):
                applog.log("WARNING", "board", f"MCP server {name}: --mcp-mount {printable(rest)} names a folder as "
                                               "a sandbox sees it, not one of this machine: asked for instead")
                continue
            mounts.append(rest)
    return mounts


def start_named_targets(argv, name):
    """The folders (targets) of MCP server NAME that the start ARGV names with --mcp-mount NAME:TARGET=..., whatever
    it gives them - also none (cleared: NAME:TARGET=), a preset's folder not filled in, a sandbox's view."""
    named = set()
    for index, token in enumerate(argv):
        option, equals, value = token.partition("=")
        if option != "--mcp-mount":
            continue
        value = value if equals else (argv[index + 1] if index + 1 < len(argv) else "")
        server, colon, rest = value.partition(":")
        if colon and server == name and "=" in rest:
            named.add(rest.partition("=")[0])
    return named


def mcp_add_args(argv, name, folders=(), entry=None, network=False, host=None, build=()):
    """`sandbox-ai mcp add NAME` for a server the start ARGV uses, with the folders the start gives it (--mcp-mount
    NAME:TARGET=HOST[:MODE]) and FOLDERS (TARGET=HOST, asked for: ask_mcp_folders) as its --mount: a catalog server
    that must have a folder (engram, memory, exiftool) is not added without one, and the start's is the one it
    asked for. ENTRY: the catalog entry a preset names for it (hub:binwalk), added as NAME. NETWORK: with the outside
    network (--network full; a server whose entry lists the hosts it needs gets only those without it). BUILD: its
    build from source, asked for (build_args: an entry without a signed image yet)."""
    mounts = []
    for value in [*start_mounts(argv, name, host), *folders]:
        mounts += ["--mount", value]
    return ("mcp", "add", entry or name, *mounts, *(["--network", "full"] if network else []), *build)


def catalog_entry(entries, name):
    """The catalog entry NAME ("binwalk", "hub:binwalk") of ENTRIES (catalog_entries' [(prefix, entry)], or the
    entries of `mcp catalog --json`), or None when no catalog read has it."""
    prefix, _, bare = str(name).rpartition(":")
    for item in entries or []:
        found_prefix, entry = item if isinstance(item, tuple) and len(item) == 2 else \
            ((item or {}).get("prefix"), item)
        if isinstance(entry, dict) and entry.get("name") == bare and (not prefix or found_prefix == prefix):
            return entry
    return None


def catalog_prefixes(entries, name):
    """The catalogs (their prefixes, in order, each once) of ENTRIES that have NAME ("radare2", or "hub:radare2": that
    catalog's only)."""
    prefix, _, bare = str(name).rpartition(":")
    found = []
    for item in entries or []:
        found_prefix, entry = item if isinstance(item, tuple) and len(item) == 2 else \
            ((item or {}).get("prefix"), item)
        if isinstance(entry, dict) and entry.get("name") == bare and (not prefix or found_prefix == prefix):
            found_prefix = found_prefix if isinstance(found_prefix, str) else ""  # ("": a catalog not named)
            if found_prefix not in found:
                found.append(found_prefix)
    return found


def required_folders(entries, name):
    """The folders (targets in the server's container) the catalog entry NAME ("binwalk", "hub:binwalk") requires,
    from ENTRIES (catalog_entry); None when no catalog read has it (nothing known: `mcp add` says what it misses)."""
    entry = catalog_entry(entries, name)
    if entry is None:
        return None
    return [m["target"] for m in entry.get("mounts", []) if isinstance(m, dict) and m.get("required")
            and isinstance(m.get("target"), str)]


def needed_hosts(entries, name):
    """The hosts ("html.duckduckgo.com:443") the catalog entry NAME needs when it lists them (its network
    "allowlist", which sandbox-ai cannot limit a server to yet: `mcp add` wants --network full), else []."""
    entry = catalog_entry(entries, name)
    if entry is None or entry.get("network") != "allowlist":
        return []
    return [str(h) for h in entry.get("hosts") or [] if isinstance(h, str)][:20] or ["?"]


def unpublished_entry(entries, name):
    """The catalog entry NAME ("jadx", "mcp:jadx") of ENTRIES when its catalog has no image of it yet (pending:
    reviewed, not published - `mcp add` refuses it, `mcp add --build` builds it from its source), else None. An entry
    of a catalog whose signature does not verify is never one: nothing of that catalog is used."""
    entry = catalog_entry(entries, name)
    if not isinstance(entry, dict):
        return None
    if "published" in entry:  # (`mcp catalog --json`'s; catalog_entries' own entries: their digest)
        published = entry["published"]
    elif "_signature" in entry:
        published = bool(entry.get("digest"))
    else:
        return None  # (a host that does not say: not offered - `mcp add` there says what it misses)
    return None if published or entry_signature(entry) == "bad" else entry


def entry_signature(entry):
    """good, bad or none: the signature of ENTRY's catalog (`mcp catalog --json`'s, or catalog_entries' _signature)."""
    if "signature" in entry and isinstance(entry.get("signature"), str):
        return entry["signature"] if entry["signature"] in ("good", "bad") else "none"
    state = (entry.get("_signature") or {}).get("state") if isinstance(entry.get("_signature"), dict) else None
    return {"good": "good", "bad": "bad"}.get(state, "none")


def build_plan_args(add_args, entry):
    """`mcp add ... --build-plan` for the server `mcp add` ADD_ARGS (mcp_add_args) would add: what would be built,
    nothing built (--unsigned where ENTRY's catalog is not signed: said in the question)."""
    return (*add_args, "--build-plan", *(["--unsigned"] if entry_signature(entry) != "good" else []))


def build_args(plan, entry):
    """What `mcp add` gets after the human's yes to PLAN (`mcp add --build-plan`'s): that commit and source archive,
    nothing else (--commit, --source-sha256), and --yes - the question was asked here."""
    return ("--build", "--commit", str(plan["commit"]), "--source-sha256", str(plan["archive_sha256"]), "--yes",
            *(["--unsigned"] if entry_signature(entry) != "good" else []))


def checked_plan(text):
    """`mcp add --build-plan`'s JSON: {entry, repository, commit, path, archive_sha256, catalog_label, signature}
    checked (a host's answer is its text: every value one printable line), else ValueError."""
    try:
        plan = json.loads(text)
    except (ValueError, RecursionError):
        raise ValueError("not a build plan") from None
    if not isinstance(plan, dict) or not re.fullmatch(r"[0-9a-f]{40}", str(plan.get("commit") or "")) or \
            not re.fullmatch(r"[0-9a-f]{64}", str(plan.get("archive_sha256") or "")):
        raise ValueError("not a build plan (no commit, no source archive's SHA-256)")
    return {key: printable(str(plan.get(key) or ""))[:500] for key in
            ("entry", "title", "repository", "commit", "path", "archive_sha256", "catalog_label", "signature")}


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


# The login of the start's agent (and of each agent --with): asked for on the start's host as an option's value is,
# under this name in the needs ({LOGIN: [agents]}). An agent `sandbox-ai login` takes; not when the start configures
# its login otherwise (LOGIN_ELSEWHERE: these options).
LOGIN = "login"
LOGIN_AGENTS = ("claude", "codex", "opencode", "hermes", "qwen", "pi")
LOGIN_ELSEWHERE = frozenset({"--no-auth", "--copy-auth", "--auth-file", "--import-codex-auth", "--auth-from-session",
                             "--llm", "--api-key", "--local-config", "--offline", "--state-dir", "--binary",
                             "--probe", "--dry-run"})


KEY_VARIABLE = re.compile(r"API_KEY|AUTH_TOKEN|OAUTH_TOKEN|ACCESS_TOKEN")  # a variable's name that holds a login

# The image of the start's agent (localhost/AGENT-sandbox:latest), when it is not built on the start's host: asked
# about in the question with the rest under this name in the needs ({IMAGE: [agent]}) instead of by the start in its
# terminal tab - by a check that costs no launcher run (this machine: `podman image exists`; another host: the list
# the Images page read of it, when it shows that host).
IMAGE = "agent-image"
IMAGE_SEEN_SECONDS = 600  # an agent's image found built is not looked at again for so long (Podman is asked once)
IMAGE_UNBUILT_SECONDS = 30  # one found not built: looked at again sooner (built in a terminal meanwhile)
# Options with which the start runs another image than the agent's own (or none of this host's Podman).
OTHER_IMAGE = frozenset({"--image", "-i", "--image-variant", "-iv", "--podman-url", "--probe", "--dry-run"})

# What the start lacks as its host's sandbox-ai says it (`AGENT ... --fix-plan`: sandbox/fixes.py), read with the
# rest under this name in the needs ({PLAN: ["start"]}): the fixes of what this check does not look for itself - the
# agent's image not built, a toolchain not in the store, a kit - join what is asked about and made.
PLAN = "plan"
# The kinds of the launcher's plan this check provides itself (asked with their folders, catalogs, a terminal).
PLAN_OWN = frozenset({"identity", "variant", "mcp", "vpn", "service", "login", "plugin", "secret"})
# The start's options that name what this check does not look for itself: with one of them (or anything else to
# check) the launcher's plan is read first.
# (--cairn: its workers may name an agent the session does not have - a choice between ways to start. An image
# that is not built - the agent's, the browser's, a team's - is asked about by the start itself, in its terminal.)
PLAN_OPTIONS = frozenset({"--toolchain", "--kit", "--llm", "--cairn"})


def plan_extras(plan, needs, attempted=()):
    """(the fixes of PLAN (backend.checked_fix_plan) that this check does not provide itself and the start needs,
    those of them that ran before in this preparation and are missing still). An image that is a variant's (built
    with `build --variant`) is the check's own where the start names a variant; the agent's own image where the
    check looked at it (IMAGE)."""
    extras, again = [], []
    own_builds = [["sandbox-ai", "build", agent] for agent in needs.get(IMAGE, [])] if isinstance(needs, dict) else []
    for item in (plan or {}).get("fixes") or []:
        if item.get("kind") in PLAN_OWN or not item.get("needed"):
            continue
        commands = item.get("commands") or []
        if "--image-variant" in needs and any(command[1:3] == ["build", "--variant"] for command in commands):
            continue
        if own_builds and commands and all(command in own_builds for command in commands):
            continue
        (again if item.get("id") in attempted else extras).append(item)
    return extras, again


def mcp_row(name, host, page, entry_name):
    """The MCP server NAME's line of the one question (provide's rows: (key, text, marked)), with what PAGE (the
    catalogs and build plans read before it) says of it: one without a signed image yet with what would be built on
    HOST - repository, commit, source archive - UNSIGNED, a question of its own whose default is No; else added from
    its catalog."""
    plan = (page.get("plans") or {}).get(name)
    entry = unpublished_entry(page.get("entries"), entry_name) if page.get("entries") is not None else None
    if isinstance(plan, dict):
        signed = tr("signed: the entry was reviewed, its image is not published yet") \
            if entry_signature(entry or {}) == "good" else tr("NOT signed: nobody vouched for the entry")
        return (("mcp", name), tr("the MCP server {name} has no signed image in its catalog yet → built on {host} "
                                  "from its reviewed source and added UNSIGNED (minutes): {repository}, commit "
                                  "{commit}, {path}; source archive SHA-256 {sha256}; catalog {catalog} ({signed})",
                                  name=name, host=host, repository=plan["repository"], commit=plan["commit"],
                                  path=plan["path"], sha256=plan["archive_sha256"], catalog=plan["catalog_label"],
                                  signed=signed), True)
    if plan is not None:  # (what would be built could not be read: said, its own question follows)
        return (("mcp", name), tr("the MCP server {name} has no signed image in its catalog yet → built on {host} "
                                  "from its source after its own question (what would be built could not be read "
                                  "yet: {error})", name=name, host=host, error=printable_text(str(plan))[:300]), True)
    return (("mcp", name), tr("the MCP server {name} is not added on {host} → added from its catalog (its image is "
                              "pulled; one without a signed image is asked about on its own, named unsigned)",
                              name=name, host=host), False)


def host_update_fix(host, error):
    """A fix of the plan for HOST, whose sandbox-ai is too old for the start (ERROR: backend.HostTooOld of its
    `--fix-plan`): its signed update, `sandbox-ai hosts update HOST` - run on THIS machine ("here"), offered in the
    prerequisites' question; declined, the start goes on as before (the launcher there says what it does not know)."""
    missing = getattr(error, "missing", None) or []
    return {"id": f"host:{host}", "kind": "host", "name": str(host),
            "missing": tr("sandbox-ai on {host} is too old for this start (it does not know {options})",
                          host=printable(host), options=printable(" ".join(missing)) or "--fix-plan"),
            "title": tr("update it with its signed release, from this machine"),
            "commands": [["sandbox-ai", "hosts", "update", str(host)]],
            "command": f"sandbox-ai hosts update {printable(host)}", "duration": tr("a minute or two"),
            "unsigned": False, "security": "", "terminal": False, "needed": True, "verdict": "offered",
            "options": None, "choice": "", "start": "", "why": "", "here": True}


def fix_text(item):
    """One fix of the launcher's plan in a line: what is missing, and what is done (its command, how long), named
    UNSIGNED and with what is security-relevant about it."""
    done = item.get("title") or ""
    if item.get("unsigned"):
        done += tr(", UNSIGNED")
    if item.get("security"):
        done += tr("; security: {text}", text=item["security"])
    if item.get("command"):
        done += f": {item['command']}"
    if item.get("duration"):
        done += f" ({item['duration']})"
    return tr("{missing} → {done}", missing=item.get("missing") or item.get("id") or "?", done=done)



def changed_options(argv, add, remove):
    """The start's options ARGV as a chosen way to start changes them (sandbox/fixes.py start_options): each option
    of REMOVE taken out with its value (--name VALUE and --name=VALUE), the words ADD put at the end."""
    kept, skip = [], False
    for word in [str(word) for word in argv]:
        if skip:
            skip = False
        elif word in remove:
            skip = True
        elif not any(word.startswith(name + "=") for name in remove):
            kept.append(word)
    return [*kept, *[str(word) for word in add]]


def start_agents(agent, argv):
    """AGENT and the agents the start ARGV runs beside it (--with A[,B...], repeatable), each once."""
    found = [agent]
    for index, token in enumerate(argv):
        option, equals, value = str(token).partition("=")
        if option != "--with":
            continue
        value = value if equals else (argv[index + 1] if index + 1 < len(argv) else "")
        found += [name.strip() for name in str(value).split(",") if name.strip()]
    return list(dict.fromkeys(found))


def login_needs(agent, argv, skipped=()):
    """The agents of the start (AGENT, ARGV: its launcher options) whose login the start's host must have: those
    `sandbox-ai login` takes, unless the start configures its login otherwise (LOGIN_ELSEWHERE) - then none - or the
    human said to start without it (SKIPPED)."""
    if any(str(token).partition("=")[0] in LOGIN_ELSEWHERE for token in argv):
        return []
    for index, token in enumerate(argv):
        # A key or token handed in as a variable (--env ANTHROPIC_API_KEY=..., --secret-env OPENAI_API_KEY=NAME): the
        # agent signs in with that.
        option, equals, value = str(token).partition("=")
        if option in ("--env", "--secret-env"):
            value = value if equals else (str(argv[index + 1]) if index + 1 < len(argv) else "")
            if KEY_VARIABLE.search(value.partition("=")[0]):
                return []
    return [name for name in start_agents(agent, argv) if name in LOGIN_AGENTS and name not in skipped]


def service_image(value):
    """The image the --service VALUE names (NAME[:image=IMAGE,...]; without image= its name), else None."""
    name, _, rest = str(value).partition(":")
    image = next((item.partition("=")[2].strip() for item in rest.split(",") if item.strip().startswith("image=")),
                 name.strip())
    return image or None


def need_word(option, name, agent):
    """What the start needs (OPTION NAME, for AGENT) in words: "image variant tor of claude"."""
    return {"--identity": tr("identity {name}", name=name),
            "--service": tr("service image {name}", name=name),
            "--image-variant": tr("image variant {name} of {agent}", name=name, agent=agent),
            "--vpn": tr("VPN profile {name}", name=name),
            "--mcp": tr("MCP server {name}", name=name),
            LOGIN: tr("login of {name}", name=name),
            PLAN: tr("what sandbox-ai there says the start lacks")}.get(option, f"{option} {name}")


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

    PREREQUISITES = ("--identity", "--image-variant", "--vpn", "--mcp", "--service")

    def board_needs(self):
        """{option: [names]} of what the start uses that must exist on its host first: --identity, --image-variant,
        --vpn, --mcp, and the images of its services (--service) that are sandbox-ai's own Containerfiles - built
        first when they are not (placeholders left out: they are asked for first)."""
        argv = self.prerequisite_argv()
        own = set(self.prerequisite_own_servers())
        needs = {}
        templates = None
        for index, token in enumerate(argv):
            option, has_value, value = token.partition("=")
            if option in self.PREREQUISITES:
                value = value if has_value else (argv[index + 1] if index + 1 < len(argv) else "")
                if value and not B.PLACEHOLDER.search(value):
                    if option == "--mcp" and value in own:
                        continue  # the project's own server: defined by its project file, nothing to add
                    if option == "--mcp" and ":" in value:
                        continue  # (no server's name: hub:radare2 - the board says so; added, it was never found)
                    if option == "--service":
                        templates = self.prerequisite_templates() if templates is None else templates
                        value = service_image(value)
                        if value not in templates or value in needs.get(option, []):
                            continue  # (an image of yours: the board's check and the launcher's say what is missing)
                    needs.setdefault(option, []).append(value)
        return needs

    def image_look_agent(self, host):
        """The start's agent when its own image is to be looked at before the start (IMAGE): the start runs it (no
        --image, --image-variant, remote Podman, Bubblewrap or Team part), else None."""
        if os.environ.get("SANDBOX_AI_MCP_NO_IMAGE_CHECK") == "1":
            return None  # (as the launcher's: no image is asked of Podman before a start - the tests' made-up images)
        argv = [str(token) for token in self.prerequisite_argv()]
        agent = self.prerequisite_agent()
        if not agent or any(token.partition("=")[0] in OTHER_IMAGE for token in argv) or \
                (hasattr(self, "board") and B.team_node(self.board) is not None):
            return None
        for index, token in enumerate(argv):
            option, equals, value = token.partition("=")
            if option == "--backend" and (value if equals else argv[index + 1] if index + 1 < len(argv) else "") \
                    in ("bwrap", "both"):
                return None
        return agent

    def agent_image_need(self, host):
        """[the start's agent] when its own image is known not to be built on HOST (IMAGE) - what Start reads, never
        asking Podman itself: this machine's answer of the look in the background (look_at_agent_image: while the
        page shows the agent), another host's from the list the Images page read (kept_image_list). Not known (yet):
        the start is not held up, its own image is asked about by the start itself, in its terminal."""
        agent = self.image_look_agent(host)
        if agent is None:
            return []
        if host == LOCAL:
            self.look_at_agent_image()  # (none yet, or an old one: looked at again in the background, for later)
            return [agent] if self.known_agent_image(host, agent) is False else []
        kept = self.kept_image_list(host)
        return [agent] if kept is not None and kept.get(agent, True) is False else []

    def known_agent_image(self, host, agent):
        """Whether AGENT's own image is built on HOST as the last look said (True, False), None when never looked at:
        an old answer too (the look is made again by look_at_agent_image - a start that has its image is not held
        up for it; one found not built is read again with the rest of the start's needs, in the background)."""
        entry = self.__dict__.get("images_known", {}).get((host, agent))
        return None if entry is None else entry[1]

    def agent_images_answered(self, host, found):
        """FOUND ({agent: built}, a look or the start's read of its needs) kept as what is known of HOST's images."""
        known = self.__dict__.setdefault("images_known", {})  # {(host, agent): (when it was looked at, built)}
        for agent, built in (found or {}).items():
            if isinstance(built, bool):
                known[(host, str(agent))] = (time.monotonic(), built)

    def look_at_agent_image(self):
        """The start's agent's own image looked at on this machine in the background (`podman image exists`, IMAGE)
        as soon as the page shows that agent and when the agent changes (the pages call this when shown and on every
        change: cheap while the answer is fresh), the answer kept (known_agent_image): pressing Start only reads it
        and never waits for Podman. Looked at again after IMAGE_SEEN_SECONDS (built) or IMAGE_UNBUILT_SECONDS (not
        built: a build in a terminal meanwhile)."""
        try:
            host = self.host()
            agent = self.image_look_agent(host) if host == LOCAL else None
        except Exception as _exc:  # noqa: BLE001 - a page not filled yet: looked at with its next change
            applog.swallowed("prerequisites", "Prerequisites.look_at_agent_image: no agent yet", _exc)
            return
        if agent is None:
            return
        key = (host, agent)
        reading = self.__dict__.setdefault("images_reading", set())
        entry = self.__dict__.get("images_known", {}).get(key)
        if key in reading or (entry is not None and time.monotonic() - entry[0] < (
                IMAGE_SEEN_SECONDS if entry[1] else IMAGE_UNBUILT_SECONDS)):
            return
        if not shutil.which("podman"):
            return
        reading.add(key)
        page = weakref.ref(self)

        def looked(found, error):
            from shiboken6 import isValid
            this = page()
            if this is None or not isValid(this):
                return
            reading.discard(key)
            if error is None and isinstance(found, dict):
                this.agent_images_answered(host, found)
            else:
                applog.log("DEBUG", "prerequisites", f"the image of {agent} could not be looked at: "
                                                     f"{applog.error_text(error)} (the start asks itself)")
        background(self.agent_images_built, host, [agent], timeout=30, done=looked,
                   what=f"looking whether the image of {agent} is built on this machine (podman image exists)")

    def kept_image_list(self, host):
        """{target: built} of the Images page's list when that page shows HOST (read there already: no launcher run
        for it now), else None."""
        window = self.window() if hasattr(self, "window") else None
        page = getattr(window, "images", None) if window is not None and window is not self else None
        try:
            if page is None or page.host() != host or not getattr(page, "rows", None):
                return None
            return {str(target): bool(built) for target, built in page.rows}
        except Exception as _exc:  # noqa: BLE001 - not known then: the start asks itself
            applog.swallowed("prerequisites", "Prerequisites.kept_image_list: not read", _exc)
            return None

    def agent_images_built(self, host, agents):
        """{agent: whether its own image is built on HOST} (IMAGE), in the background only (look_at_agent_image, the
        start's read of its needs): this machine asks its Podman (`podman image exists`, no launcher run, bounded by
        5 s); another host's from the list the Images page read; an agent not known: True (the start asks itself, in
        its terminal)."""
        import subprocess
        kept = None if host == LOCAL else self.kept_image_list(host)
        found = {}
        for agent in agents:
            if host != LOCAL:
                found[agent] = (kept or {}).get(agent, True)
                continue
            try:
                done = subprocess.run(
                    ["podman", "image", "exists", f"localhost/{agent}-sandbox:latest"], stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
                found[agent] = done.returncode != 1  # (1: not there; any other answer: the start says it)
            except (OSError, subprocess.SubprocessError):
                found[agent] = True
        return found

    def prerequisite_templates(self):
        """The names of sandbox-ai's own Containerfiles for services (images build-image NAME --builtin NAME) a start
        may need built: this installation's, and those of the page's host where it read them (image_templates)."""
        found = set(self.__dict__.get("image_templates") or ())
        try:
            found |= set(self.sai.module("services").builtin_images())
        except Exception as _exc:  # noqa: BLE001 - an older sandbox-ai: none of its own
            applog.swallowed("prerequisites", "Prerequisites.prerequisite_templates: none known here", _exc)
        return found

    def prerequisite_own_servers(self):
        """The MCP servers the start's project defines itself (none known: a page may say)."""
        return set()

    def prerequisite_login_agents(self):
        """The agents whose login the start's host must have (login_needs: the start's agent and those --with; a
        page that starts several - the Board's Team part, each member - says which)."""
        return login_needs(self.prerequisite_agent(), self.prerequisite_argv())

    def prerequisite_context(self):
        """What the start is about (a board, a project, a host): a continuation made for another is dropped."""
        return self.host()

    def prerequisites_made(self, host):
        """Something was made on HOST for the start (a page that keeps lists of that host reads them again)."""

    def mcp_entry_of(self, server):
        """The catalog entry SERVER is added from: the one the start names (hub:binwalk), the one chosen of several
        (resolve_mcp_entries), else its name."""
        return self.__dict__.get("mcp_entries_chosen", {}).get(server) or \
            self.prerequisite_mcp_entries().get(server) or server

    def prerequisite_workspace(self, host, argv):
        """The start's workspace (of its options ARGV) when it is a folder of HOST, the start's host - offered as the
        folder of an MCP server there; None for none (a page whose workspace may be this machine's folder on another
        host says so: the Board)."""
        return start_workspace(argv)

    def prerequisite_mcp_entries(self):
        """{server: catalog entry} for the MCP servers the start's preset names by their catalog (hub:binwalk): added
        from that entry when missing; the rest by their name."""
        return {}

    def check_prerequisites(self, then=None, why="start"):
        """Everything the board needs that its host does not have yet - an identity, an image variant (made, or only
        not built), a VPN profile, an MCP server: a preset's "investigation" identity, "media" variant and Tor profile,
        say - read in the background, then provided (provide()). THEN runs after it all is there (the start goes on);
        nothing is left for the launcher to refuse. True while that is under way."""
        needs = self.board_needs()
        host = self.host()
        if why == "start" and then is not None and hasattr(self.sai, "host_logged_in") and \
                self.login_terminal() is not None:  # (no terminal for a login: the launcher says what is missing)
            # The login of the start's agent (and of those --with) on its host: asked for before the start, not left
            # for the launcher's "the host has no codex login" in the terminal - with a login there first, on request.
            logins = [name for name in self.prerequisite_login_agents() if name not in self.logins_skipped(host)]
            if logins and host == LOCAL and hasattr(self.sai, "logged_in_here"):
                # This machine's own: read here and now from the login files (no process). With every login there the
                # start is held up by nothing; one that is missing is asked about below.
                try:
                    there = self.sai.logged_in_here(logins, self.prerequisite_session())
                except Exception as exc:  # noqa: BLE001 - not read: the launcher says what is missing
                    applog.log("INFO", "prerequisites", f"{why}: this machine's logins could not be read "
                                                        f"({applog.error_text(exc)}): the launcher checks them")
                    there = set(logins)
                logins = [name for name in logins if name not in there]
            if logins:
                needs = {**needs, LOGIN: logins}
        if why == "start" and then is not None and self.fix_questions_mode() is not None:
            own = self.agent_image_need(host)
            if own:
                needs = {**needs, IMAGE: own}
        if why == "start" and then is not None and self.fix_questions_mode() is not None and \
                callable(getattr(self.sai, "host_fix_plan", None)) and \
                not any(B.PLACEHOLDER.search(str(token)) for token in self.prerequisite_argv()) and \
                (needs or any(str(token).partition("=")[0] in PLAN_OPTIONS for token in self.prerequisite_argv())):
            # What else the start lacks, as its host's sandbox-ai says it (its image, a toolchain, a kit ...): read
            # with the rest, asked about with it, made before the start - not left for the start's terminal. Where
            # the start names nothing that can be missing, its host is not asked first (a start is not held up for
            # it): its own image, when that is not built, is asked about by the start itself, in its terminal.
            needs = {**needs, PLAN: ["start"]}
        if not needs or not hasattr(self.sai, "host_image_variants"):
            applog.log("DEBUG", "prerequisites", f"{why}: nothing to check first (needs {needs or 'none'})")
            return False
        context = self.prerequisite_context()
        busy = self.__dict__.setdefault("preparing_contexts", set())
        stages = self.__dict__.setdefault("preparing_stages", {})  # {context: (since, what is under way)}
        if context in busy:  # (this start's needs are read or made already; another board or host is not held up)
            since, stage = stages.get(context, (None, "unknown"))
            applog.log("INFO", "prerequisites", f"{why} on {printable(host)} waits: what it needs is still being "
                       f"prepared for {printable(context)} ({stage}"
                       + (f", for {applog.seconds(time.monotonic() - since)}" if since else "") + ")")
            if then is not None:  # (a Start pressed while a preset's needs are made: it goes on once they are)
                self.__dict__.setdefault("preparing_waiters", {})[context] = then
            # What runs now and how far, as information (a Start pressed again is no warning), seen on the Board.
            self.prerequisite_notice(self.still_preparing(), "secondary", status=True, link=True)
            self.preparing_refresh(shown=True)
            return True
        key = (host, json.dumps(needs, sort_keys=True))
        if getattr(self, "prerequisites_done", None) == key:
            # (only while the start that found them there goes on: every Start reads its host again - a server
            # removed there, a host made anew, was taken as there for good, and the launcher refused the start)
            applog.log("DEBUG", "prerequisites", f"{why} on {printable(host)}: its needs were found there just now: "
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
        session = self.prerequisite_session()  # (its session's name: one that has a login of its own needs no other)
        plan_argv = list(self.prerequisite_argv())  # (the start's options, as they are now)

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
            def plan():
                # The launcher's plan there; a sandbox-ai there too old for it (or for an option of the start): its
                # update offered in the question (host_update_fix) - once per run of the app, declined.
                try:
                    return self.sai.host_fix_plan(host, agent, plan_argv)
                except Exception as exc:  # noqa: BLE001 - only an older host is turned into an offer
                    if type(exc).__name__ != "HostTooOld" or host == LOCAL or \
                            host in self.__dict__.get("updates_declined", set()):
                        raise
                    return {"fixes": [host_update_fix(host, exc)], "questions": "all", "mode": ""}

            def variants():
                # {(name, agent): built}: each agent has variants of its own (the recipe media for claude and dsh);
                # a target is NAME, or NAME@AGENT where several agents have a variant of that name.
                data = self.sai.host_json(host, "images", "--json", timeout=90)
                return {(str(e["target"]).partition("@")[0], str(e.get("agent") or "")): bool(e.get("present"))
                        for e in data if isinstance(e, dict) and e.get("variant") and isinstance(e.get("target"), str)}
            def service_images():
                # {image: built}: your images there (built), sandbox-ai's own templates there not built (False)
                mine = {str(row.get("name")): True for row in self.sai.user_images(host) if isinstance(row, dict)}
                return {**{str(row["image"]): False for row in self.sai.service_templates(host)}, **mine}
            readers = {"--identity": lambda: self.sai.identities() if host == LOCAL else self.sai.host_identities(host),
                       "--service": service_images,
                       "--image-variant": variants,
                       "--vpn": lambda: [e["profile"] for e in self.sai.host_vpn_profiles(host)],
                       "--mcp": lambda: [row[0] for row in self.sai.host_mcp_servers(host)],
                       LOGIN: lambda: self.sai.host_logged_in(host, needs[LOGIN], session),
                       IMAGE: lambda: self.agent_images_built(host, needs[IMAGE]),
                       PLAN: plan}
            have = {}
            for option in needs:
                began = time.monotonic()
                try:
                    found = readers[option]()
                    have[option] = found if isinstance(found, dict) and option in (
                        "--image-variant", "--service", PLAN, IMAGE) else set(found)
                    applog.log("DEBUG", "prerequisites", f"{option} on {host}: {len(have[option])} there, read in "
                                                         f"{applog.seconds(time.monotonic() - began)}")
                except Exception as exc:  # noqa: BLE001 - not answered: the launcher says what is missing at the start
                    applog.log("INFO", "prerequisites", f"{option} on {printable(host)} could not be read (after "
                               f"{applog.seconds(time.monotonic() - began)}; the launcher checks it at the start): "
                               f"{applog.error_text(exc)}")
                    have[option] = None
                    if option != PLAN:  # (no plan - an older sandbox-ai there, a refusal: its start says it itself)
                        unread[option] = printable_text(str(exc)).strip()[-300:] or type(exc).__name__
            return have
        unread = self.prerequisite_unread = {}  # {option: why it could not be read on the host} (said, not silent)

        def done(have, error):
            self.prerequisite_reading = None
            if not same():  # the page shows another start now: nothing is asked or made for this one
                applog.log("INFO", "prerequisites", f"{why}: the page shows another start now - nothing is made or "
                                                    f"started for {printable(context)}")
                busy.discard(context)
                self.drop_waiter(context, why)
                stages.pop(context, None)
                return
            if error is not None:
                applog.log("WARNING", "prerequisites", f"{why}: what {printable(host)} has could not be read: "
                           f"{applog.error_text(error)} - the start goes on, the launcher checks it")
            complete = error is None and isinstance(have, dict) and all(have.get(o) is not None for o in needs)
            if error is not None or not isinstance(have, dict):
                have = {}
            missing, unbuilt = {}, []
            if isinstance(have.get(IMAGE), dict):  # (what Start reads next time: looked at just now)
                self.agent_images_answered(host, have[IMAGE])
            if isinstance(have.get("--image-variant"), dict):  # whose variant each name is (variant_target)
                self.variant_owners = {}
                for name, owner in have["--image-variant"]:
                    self.variant_owners.setdefault(name, set()).add(owner)
            for option, names in needs.items():
                there = have.get(option)
                if there is None:
                    continue
                if option == PLAN:  # the launcher's own plan: what this check does not look for itself
                    extras, again = plan_extras(there, needs, self.__dict__.get("plan_attempted", {}).get(context, ()))
                    if extras or again:
                        missing.setdefault(PLAN, []).extend(extras + [{**item, "verdict": "manual", "commands": [],
                                                                       "why": tr("still so after its fix ran: see "
                                                                                 "what it said")} for item in again])
                    continue
                for name in names:
                    if option == IMAGE:  # the agent's own image: not built there - built first, asked
                        if there.get(name) is False and \
                                name in self.__dict__.get("images_attempted", {}).get(context, ()):
                            # (built in this preparation and still not there: said, not built a second time)
                            missing.setdefault(PLAN, []).append({
                                "id": f"image:{name}", "kind": "image", "name": name, "verdict": "manual",
                                "missing": tr("the image of {name} is still not built after its build ran",
                                              name=name), "why": tr("see the tab Preparing the start"),
                                "commands": [], "command": f"sandbox-ai build {name}", "needed": True})
                        elif there.get(name) is False:
                            missing.setdefault(option, []).append(name)
                        continue
                    if option == "--service":  # sandbox-ai's own there, not built: built first
                        if name in there and not there[name]:
                            missing.setdefault(option, []).append(name)
                        continue  # (yours, or none there: the board's check and the launcher say it)
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
            if unread and why == "start":  # (said, never only in the log: the launcher has the last word now)
                self.prerequisite_notice(tr("{host} did not say what it has of {list} ({error}): the launcher checks "
                                            "it at the start.", host=printable(host), list=printable(", ".join(
                                                need_word(o, n, agent) for o in unread for n in needs.get(o, []))),
                                            error=printable(next(iter(unread.values())))), "notice-warn")
            if not missing and not unbuilt:
                # Found there (or not answered: the launcher checks it): the start goes on now, and its own check
                # within that is not read again (it read, went on, read again ... without end while a host did not
                # answer); the next Start reads the host again (a server removed there meanwhile was taken as there
                # for good, and the launcher refused every start).
                self.prerequisites_done = key
                self.__dict__.get("plan_attempted", {}).pop(context, None)
                self.__dict__.get("images_attempted", {}).pop(context, None)
                applog.log("INFO", "prerequisites", f"{why} on {printable(host)}: everything it needs is there"
                           + ("" if complete else " (as far as it could be read)")
                           + ("; the start goes on" if then is not None and same() else ""))
                busy.discard(context)
                stages.pop(context, None)
                waiter = self.__dict__.get("preparing_waiters", {}).pop(context, None)
                try:
                    go_on()
                    if then is None and waiter is not None and same():  # (a Start pressed while this was read)
                        applog.log("INFO", "prerequisites", f"{why}: the start that waited for this check goes on")
                        waiter()
                finally:
                    if getattr(self, "prerequisites_done", None) == key:
                        self.prerequisites_done = None
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

    def drop_waiter(self, context, why):
        """A Start that waited for the preparation of CONTEXT (check_prerequisites kept it) is not started: nothing
        was made, or another start is shown - said in the log; the Board's notes say why already."""
        if self.__dict__.get("preparing_waiters", {}).pop(context, None) is not None:
            applog.log("INFO", "prerequisites", f"{why}: the start that waited for this preparation is not started "
                                                "(nothing was made for it)")

    def provide(self, missing, unbuilt, host, key, then, why, context=None, agent=None, yes=()):
        """Make what MISSING ({option: [names]}) lacks on HOST: identities at once (empty: no logins, none of yours);
        image variants of a recipe made, and built with those only defined (UNBUILT) - after one question, in the
        background, the output in the log window; a VPN profile as a Tor profile (nothing to fill in) or on the VPN
        page; MCP servers added from their catalog. Then THEN, once everything is there. YES: what is made without
        its question ("build", "mcp", "vpn": a button of the Board's note said so already)."""
        agent = agent or self.prerequisite_agent()
        argv = self.prerequisite_argv()  # (the folders the start gives its MCP servers: --mcp-mount)
        self.mcp_entries_chosen = {}  # (the catalog chosen for a server in several: for this preparation only)
        self.mcp_builds = {}  # (the servers built from their source, asked: ask_mcp_builds)
        context = context if context is not None else self.prerequisite_context()
        busy = self.__dict__.setdefault("preparing_contexts", set())
        logins = list(missing.get(LOGIN, []))
        if logins:
            # The login first (asked): in a terminal on the start's host, the start going on when it ends with one;
            # or the start without it (said); or not at all. What else is missing is read again afterwards.
            self.provide_login(logins, {k: v for k, v in missing.items() if k != LOGIN}, unbuilt, host, key, then,
                               why, context, agent, yes)
            return
        recipes = set()
        try:
            recipes = set(self.sai.module("images").RECIPES)
        except Exception as _exc:  # noqa: BLE001 - an older sandbox-ai: no recipes known here
            applog.swallowed("prerequisites", "Prerequisites.provide: an older sandbox-ai: no recipes known here", _exc)
        identities = list(missing.get("--identity", []))
        variants = [v for v in missing.get("--image-variant", []) if v in recipes]
        no_recipe = [v for v in missing.get("--image-variant", []) if v not in recipes]
        vpns, servers = list(missing.get("--vpn", [])), list(missing.get("--mcp", []))
        images = list(dict.fromkeys(missing.get("--service", [])))
        # The agent's own image, not built (IMAGE): built first, in the tab Preparing the start (provide_rest).
        agent_builds = self.__dict__.setdefault("agent_builds", {})
        agent_builds[context] = list(dict.fromkeys(missing.get(IMAGE, [])))
        questions = self.fix_questions_mode()
        # The MCP servers' catalogs, and what would be built of one without a signed image yet (its repository,
        # commit and source archive), read BEFORE the question: they are in it - one page of the plan - and not
        # asked about on their own afterwards (mcp_page). Read once per preparation, in the background.
        pages = self.__dict__.setdefault("mcp_page", {})
        if servers and questions is not None and "mcp" not in yes and context not in pages:
            pages[context] = None  # (being read)

            def read_first():
                entries = self.read_mcp_catalog(host)
                pending = [n for n in servers if unpublished_entry(entries, self.mcp_entry_of(n)) is not None]
                return entries, (self.read_build_plans(host, pending, argv, entries, {}, set(), why)
                                 if pending else {})

            def first_read(result, error):
                if self.prerequisite_context() != context:
                    pages.pop(context, None)
                    busy.discard(context)
                    self.drop_waiter(context, why)
                    return
                if error is not None:
                    applog.log("WARNING", "prerequisites", f"{why}: the MCP catalogs of {printable(host)} could not "
                                                           f"be read before the question: {applog.error_text(error)}")
                pages[context] = {"entries": result[0], "plans": result[1], "decided": {}} if error is None else \
                    {"entries": None, "plans": {}, "decided": {}}
                self.provide(missing, unbuilt, host, key, then, why, context, agent, yes)
            busy.add(context)
            self.__dict__.setdefault("preparing_stages", {})[context] = (
                time.monotonic(), f"reading the MCP catalogs of {host} for {', '.join(servers)}")
            background(read_first, owner=self, timeout=900, done=first_read,
                       what=f"reading the MCP catalogs of {host} (and what would be built) for {', '.join(servers)}")
            return
        # (taken: the next preparation reads them anew; what the question decided goes on with this one)
        page = pages.pop(context, None) or {"entries": None, "plans": {}, "decided": {}}
        self.__dict__.setdefault("mcp_page_now", {})[context] = page
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
        # A VPN profile that is not there: a Tor profile of that name offered in the one question with the rest (it
        # needs nothing filled in; named security-relevant); without the question (an older backend), asked here.
        tor_asked = [] if "vpn" in yes or questions is None else list(vpns)
        for name in vpns:
            if name in tor_asked:
                continue
            choice = "tor" if "vpn" in yes else self.ask_vpn_kind(name, host)
            (tor_profiles if choice == "tor" else vpn_page).append(name)
        if vpn_page:
            problems.append((tr("VPN profile {names}: make it on the VPN page (it needs its keys or login), then "
                                "start again.", names=", ".join(vpn_page)),
                             [(tr("Make a Tor profile"), again("--vpn", vpn_page)),
                              (tr("Open the VPN page"), lambda: self.open_page("credentials", "vpn"))]))
        to_build = list(dict.fromkeys(unbuilt + variants))
        # What the launcher's own plan lists beyond this check (PLAN): offered with the rest; what sandbox-ai cannot
        # do itself (it needs root, or the security mode does not take it) is said with its command, never run.
        extras = [item for item in missing.get(PLAN, [])]
        for item in extras:
            if item["verdict"] == "offered" and item.get("terminal"):
                # (it asks for a value or a login: the start's own terminal asks it - the start goes on to it)
                self.prerequisite_notice(tr("{missing}: the start asks for it in its terminal ({command}).",
                                            missing=item["missing"], command=item.get("command") or ""), "secondary")
            elif item["verdict"] != "offered":
                problems.append((tr("{missing}: {why}", missing=item["missing"], why=item.get("why") or tr(
                    "it runs in a terminal")) + (f" ({item['command']})" if item.get("command") and
                                                 item["verdict"] != "refused" else ""), []))
        fixes = [item for item in extras if item["verdict"] == "offered" and not item.get("terminal")
                 and not item.get("options")]
        self.plan_fixes = []
        # A choice between ways to start (the launcher's plan: add an agent beside the lead, or take another worker):
        # asked, and the page that starts changes its options (prerequisite_apply_options) and starts again; where
        # the page cannot, the ways are said.
        changes = [item for item in extras if item["verdict"] == "offered" and item.get("options")]
        if changes:
            applied = self.ask_start_choice(host, changes)
            if applied is None:
                problems.append((tr("{missing}: change the start - {ways}.", missing=changes[0]["missing"], ways=tr(
                    " or ").join(f"{item['title']} ({' '.join(item['options']['add']) or item.get('start', '')})"
                                 for item in changes)), []))
            elif applied and why == "start" and then is not None:
                applog.log("INFO", "prerequisites", f"{why}: the start's options were changed as chosen "
                                                    f"({applied}); it is checked and started again")
                busy.discard(context)
                self.__dict__.setdefault("preparing_stages", {}).pop(context, None)
                self.prerequisites_done = None
                then()
                return
        if questions is not None:
            # One question for everything that is missing (Fix all / Choose… / Cancel), or one per item - as the
            # setting says (Settings > Polling: What a start lacks; sandbox-ai's default fix_questions).
            where = tr("this machine") if host == LOCAL else printable(host)
            rows = [(("agent-image", name), tr("the image of {name} is not built on {host} → built (about 5 minutes, "
                                               "room on the disk)", name=name, host=where), False)
                    for name in agent_builds[context] if "build" not in yes]
            rows += [(("tor", name), tr("the VPN profile {name} is not on {host} → made a Tor profile (nothing to "
                                        "fill in; security: the sandbox's connections go through Tor, whose exits "
                                        "see them) - or one of yours, made on the VPN page", name=name, host=where),
                      True)
                     for name in tor_asked]
            rows += [(("build", name), tr("the image variant {name} of {agent} is not built → built{made} (some "
                                         "minutes, room on the disk)", name=name, agent=agent,
                                         made=tr(", made from its recipe first") if name in variants else ""), False)
                    for name in to_build if "build" not in yes]
            rows += [(("service", name), tr("the service image {name} is not built on {host} → built from "
                                            "sandbox-ai's own Containerfile (a minute or two; like your images it "
                                            "is UNCHECKED)", name=name, host=host), False)
                     for name in images if "build" not in yes]
            rows += [mcp_row(name, host, page, self.mcp_entry_of(name)) for name in servers if "mcp" not in yes]
            rows += [(("fix", item["id"]), fix_text(item), bool(item.get("unsigned") or item.get("security")))
                     for item in fixes]
            chosen = self.ask_fix_plan(host, rows, questions) if rows else set()
            declined = [key for key, _text, _marked in rows if key not in chosen]
            tor_profiles += [name for name in tor_asked if ("tor", name) in chosen]
            no_tor = [name for kind, name in declined if kind == "tor"]
            if no_tor:
                problems.append((tr("VPN profile {names}: make it on the VPN page (it needs its keys or login), then "
                                    "start again.", names=", ".join(no_tor)),
                                 [(tr("Make a Tor profile"), again("--vpn", no_tor)),
                                  (tr("Open the VPN page"), lambda: self.open_page("credentials", "vpn"))]))
            no_agent_image = [name for kind, name in declined if kind == "agent-image"]
            if no_agent_image:
                problems.append((tr("The image of {names}: not built (sandbox-ai build NAME).",
                                    names=", ".join(no_agent_image)),
                                 [(tr("Build now"), again(IMAGE, no_agent_image))]))
                agent_builds[context] = [n for n in agent_builds[context] if n not in no_agent_image]
            not_built = [name for kind, name in declined if kind == "build"]
            if not_built:
                problems.append((tr("Image variant {names}: not built (sandbox-ai build --variant NAME).",
                                    names=", ".join(not_built)),
                                 [(tr("Build now"), again("--image-variant", [n for n in not_built if n in variants],
                                                          built=[n for n in not_built if n not in variants]))]))
                to_build = [name for name in to_build if name not in not_built]
            no_image = [name for kind, name in declined if kind == "service"]
            if no_image:
                problems.append((tr("Service image {names}: not built (sandbox-ai images build-image NAME --builtin "
                                    "NAME).", names=", ".join(no_image)),
                                 [(tr("Build now"), again("--service", no_image))]))
                images = [name for name in images if name not in no_image]
            # (what the page said of a server to build from its source: decided there - not asked again)
            page["decided"] = {name: ("mcp", name) in chosen for name in servers
                               if isinstance(page["plans"].get(name), dict)}
            not_added = [name for kind, name in declined if kind == "mcp"]
            for name in [n for n in not_added if n in page["decided"]]:  # (to build from its source: declined)
                problems.append((tr("MCP server {name}: not built - its catalog has no signed image of it yet "
                                    "(sandbox-ai mcp add {name} --build builds it from its reviewed source and adds it "
                                    "unsigned, asked first).", name=printable(name)),
                                 [(tr("Add MCP server"), again("--mcp", [name]))]))
            servers = [name for name in servers if name not in page["decided"] or name not in not_added]
            not_added = [name for name in not_added if name not in page["decided"]]
            if not_added:
                problems.append((tr("MCP servers {names}: not added (the MCP page adds them from a catalog).",
                                    names=", ".join(not_added)), [(tr("Add MCP server"), again("--mcp", not_added))]))
                servers = [name for name in servers if name not in not_added]
            for item in fixes:
                if ("fix", item["id"]) not in chosen and item.get("here"):
                    # (the update of an older host: declined, the start goes on as before - not asked again until
                    # the app ends; the launcher there says what it does not know)
                    self.__dict__.setdefault("updates_declined", set()).add(item["name"])
                    self.prerequisite_notice(tr("{host} keeps its sandbox-ai for now: the start goes on, and the "
                                                "launcher there says what it does not know (Hosts › Update).",
                                                host=printable(item["name"])), "secondary")
                elif ("fix", item["id"]) not in chosen:
                    problems.append((tr("{missing}: not done ({command}).", missing=item["missing"],
                                        command=item.get("command") or item.get("title") or ""), []))
            self.plan_fixes = [item for item in fixes if ("fix", item["id"]) in chosen]
            yes = {*yes, "build", "mcp"}  # (asked just now: the questions below are not asked again)
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
        if images and "build" not in yes and not confirm(self, tr("Service image"), tr(
                "This start needs the service image {names}: sandbox-ai's own Containerfile, not built on {host} yet. "
                "Build it now? Its FROM is pulled once and pinned by digest, its RUN steps get no network; the output "
                "goes to the tab Preparing the start, and the start goes on when it is built. Like your images it is "
                "UNCHECKED.", names=", ".join(images), host=host), tr("Build")):
            problems.append((tr("Service image {names}: not built (sandbox-ai images build-image NAME --builtin "
                                "NAME).", names=", ".join(images)),
                             [(tr("Build now"), again("--service", images))]))
            images = []
        if servers and "mcp" not in yes and not confirm(self, tr("MCP servers"), tr(
                "This start uses the MCP servers {names}, which are not added on {host}. Add them from their catalogs "
                "now (their images are pulled)?", names=", ".join(servers), host=host), tr("Add")):
            problems.append((tr("MCP servers {names}: not added (the MCP page adds them from a catalog).",
                                names=", ".join(servers)), [(tr("Add MCP server"), again("--mcp", servers))]))
            servers = []
        def read_catalog():  # (in the background: the catalogs are read over the network, or on the host)
            return self.read_mcp_catalog(host)
        if servers:
            # A catalog server that must have a folder (exiftool's /work, binwalk's): the start's own (--mcp-mount),
            # else asked for (the start's workspace offered) - read from the catalogs in the background first.
            busy.add(context)

            def catalog_read(entries, error):
                if self.prerequisite_context() != context:
                    applog.log("INFO", "prerequisites", f"{why}: the page shows another start now - the MCP servers "
                                                        f"{', '.join(servers)} are not added for {printable(context)}")
                    busy.discard(context)
                    self.drop_waiter(context, why)
                    self.__dict__.setdefault("preparing_stages", {}).pop(context, None)
                    return
                if error is not None:
                    applog.log("WARN", "board", f"the MCP catalogs could not be read for the folders of "
                                                f"{', '.join(servers)}: {error}")
                read = None if error is not None else entries
                # Each server by its catalog entry first: one no catalog there has is not tried (`mcp add` would
                # only refuse it), one in several catalogs (radare2 of mcp and hub) is asked for - `mcp add NAME`
                # refused it as ambiguous (user report 2026-11-19).
                chosen, unknown = self.resolve_mcp_entries(host, servers, read)
                if chosen:
                    self.mcp_entries_chosen = {**self.__dict__.get("mcp_entries_chosen", {}), **chosen}
                left = [n for n in servers if n not in unknown]
                folders, dropped = self.ask_mcp_folders(host, left, argv, read)
                networks, declined = self.ask_mcp_network(host, [n for n in left if n not in dropped], read)
                dropped.update(declined)

                def go_on(builds, refused):
                    """On, with the servers to build from source (BUILDS: {server: build_args}) and those not
                    (REFUSED: {server: why})."""
                    dropped.update(refused)
                    self.mcp_builds = builds
                    self.provide_rest(identities, variants, to_build, tor_profiles,
                                      [n for n in left if n not in dropped], folders,
                                      problems + [(text, [(tr("Add MCP server"), again("--mcp", [name]))])
                                                  for name, text in dropped.items()]
                                      + [(text, [(tr("Open the MCP servers page"), lambda: self.open_page("mcp"))])
                                         for name, text in unknown.items()],
                                      host, key, then, why, context, agent, argv, networks, images=images)
                # One its catalog lists without a signed image yet (pending): built from its source on that host,
                # after a question naming the commit and the archive's hash - else said, not tried (user report
                # 2026-11-21: "warum werden die nicht automatisch gebaut oder gefragt ob sie direkt gebaut werden
                # sollen???").
                pending = [n for n in left if n not in dropped and read is not None
                           and unpublished_entry(read, self.mcp_entry_of(n)) is not None]
                if pending:
                    self.ask_mcp_builds(host, pending, argv, read, folders, networks, context, why, go_on)
                    return
                go_on({}, {})
            self.__dict__.setdefault("preparing_stages", {})[context] = (
                time.monotonic(), f"reading the MCP catalogs for the folders of {', '.join(servers)}")
            if page.get("entries") is not None:  # (read for the question already: not again)
                catalog_read(page["entries"], None)
                return
            background(read_catalog, owner=self, timeout=200, done=catalog_read,
                       what=f"reading the MCP catalogs of {host} for the servers {', '.join(servers)}")
            return
        self.provide_rest(identities, variants, to_build, tor_profiles, servers, {}, problems, host, key, then, why,
                          context, agent, argv, images=images)

    # -- what is missing, asked about: once for everything, or each (the setting) --

    def fix_questions_mode(self):
        """"all" (one question for everything that is missing) or "each" (one per item), as sandbox-ai's default
        fix_questions says (Settings > Polling: What a start lacks); None with a backend that has no such default:
        the questions as they were, one per kind."""
        read = getattr(self.sai, "fix_questions", None)
        if not callable(read):
            return None
        try:
            value = read()
        except Exception as _exc:  # noqa: BLE001 - not read: the default
            applog.swallowed("prerequisites", "Prerequisites.fix_questions_mode: not read (all)", _exc)
            value = "all"
        return value if value in ("all", "each") else "all"

    def prerequisite_apply_options(self, add, remove):
        """The start's options changed as a chosen way to start says: the options REMOVE taken out (with their
        values), the words ADD added. True when the page did it (its next start has them); False where a page cannot
        change its start from here (the ways are then said, for the human to make the change)."""
        return False

    def ask_start_choice(self, host, changes):
        """A choice between ways to start (CHANGES: the launcher's fixes with "options", the answers of one question
        or several): asked, and the chosen way applied to the page's start (prerequisite_apply_options). What was
        applied (its words), "" when nothing had to be asked, None when nothing was chosen or the page could not
        apply it."""
        from .widgets import choose
        applied = []
        for choice in dict.fromkeys(item.get("choice") or item["id"] for item in changes):
            answers = [item for item in changes if (item.get("choice") or item["id"]) == choice]
            text = printable(answers[0]["missing"]) + "\n\n" + "\n".join(
                f"{letter}) {printable(item['title'])}: {printable(' '.join(item['options']['add']) or item.get('start') or '')}"
                + (" - " + tr("security: {text}", text=printable(item["security"])) if item.get("security") else "")
                for letter, item in zip("abcdefgh", answers))
            answer = choose(self, tr("What the start lacks"), text,
                            [(item["id"], printable(item["title"])[:80]) for item in answers])
            chosen = next((item for item in answers if item["id"] == answer), None)
            applog.log("INFO", "prerequisites", f"a way to start on {printable(host)} ({printable(choice)}): "
                                                f"{chosen['id'] if chosen else 'none chosen'}")
            if chosen is None or not self.prerequisite_apply_options(list(chosen["options"]["add"]),
                                                                     list(chosen["options"]["remove"])):
                return None
            applied.append(" ".join(chosen["options"]["add"]) or "-" + " ".join(chosen["options"]["remove"]))
        return ", ".join(applied)

    def ask_fix_plan(self, host, rows, questions):
        """The keys of ROWS ([(key, what is missing and what is done, unsigned or security-relevant)]) the human
        wants done. QUESTIONS "all": everything listed, one question - Fix all, Choose… (each one asked then) or
        Cancel (nothing); "each": one question per row. What is unsigned or security-relevant is asked as a
        question whose default is No when asked on its own."""
        from .widgets import choose
        place = tr("on this machine") if host == LOCAL else tr("on {host}", host=printable(host))
        each = questions == "each"
        if not each:
            listed = "\n".join(f"{number}. {printable(text)}" for number, (_key, text, _marked) in enumerate(rows, 1))
            answer = choose(self, tr("What the start lacks"), tr(
                "This start needs what is not there yet {place}:\n\n{list}\n\nDo it now? Its progress shows in the "
                "notes here and in the tab Preparing the start; the start goes on when everything is there.",
                place=place, list=listed), [("all", tr("Fix all")), ("choose", tr("Choose…"))])
            applog.log("INFO", "prerequisites", f"what the start lacks on {printable(host)} ({len(rows)} items): "
                                                f"{answer or 'cancelled'}")
            if answer == "all":
                return {key for key, _text, _marked in rows}
            if answer != "choose":
                return set()
        chosen = set()
        for number, (key, text, marked) in enumerate(rows, 1):
            title = tr("What the start lacks ({number} of {total})", number=number, total=len(rows))
            asked = printable(text) + "\n\n" + tr("Do it now {place}?", place=place)
            # What is unsigned or security-relevant: a question whose default is No (choose: Cancel), never Enter.
            if (choose(self, title, asked, [("do", tr("Do it"))], cancel=tr("Not this one")) == "do") if marked else \
                    confirm(self, title, asked, tr("Do it"), reject=tr("Not this one")):
                chosen.add(key)
        applog.log("INFO", "prerequisites", f"what the start lacks on {printable(host)}: chosen "
                                            f"{len(chosen)} of {len(rows)}")
        return chosen

    # -- the agent's login on the start's host --

    def prerequisite_session(self):
        """The session's name the start is made under (None: none, or not known): the page's start() notes it."""
        return self.__dict__.get("starting_session") or None

    def logins_skipped(self, host):
        """The agents the human chose to start without a login of on HOST (until the app ends): not asked again."""
        return {agent for where, agent in self.__dict__.get("login_skipped", set()) if where == host}

    def login_terminal(self):
        """run(argv, title, ended) -> bool: a command in a terminal (a tab of the app or a window, as the setting
        says) whose end is told - the window's in_terminal, else the page's own terminal function; None: none."""
        window = self.window() if hasattr(self, "window") else None
        in_terminal = getattr(window, "in_terminal", None) if window is not self else None
        if callable(in_terminal):
            return lambda argv, title, ended: in_terminal(argv, title, ended=ended)
        return None

    def ask_login(self, host, agents):
        """"login" (log in now, in a terminal on HOST), "without" (start without it) or None (Cancel) for the agents
        AGENTS whose login HOST does not have."""
        from .widgets import choose
        names = printable(", ".join(agents))
        place = tr("on this machine") if host == LOCAL else tr("on {host}", host=printable(host))
        text = tr("There is no login for {agents} {place}: the sandbox would start without one, and the agent asks "
                  "for it in its terminal. Log in to {agents} {place} now? `sandbox-ai login` runs in a terminal "
                  "there - a sign-in with a one-time code opens its page in your browser - and the start goes on "
                  "when it ends with a login, kept for every later sandbox of that agent there.", place=place,
                  agents=names)
        return choose(self, tr("Log in to {agents}", agents=names), text,
                      [("login", tr("Log in now")), ("without", tr("Start without a login"))])

    def provide_login(self, agents, missing, unbuilt, host, key, then, why, context, agent, yes=()):
        """The logins of AGENTS, which HOST lacks, before the start: asked (ask_login) - "Log in now" runs `sandbox-ai
        login AGENT` on HOST in a terminal, one agent after the other, and the start's needs are read again when the
        last ended well (the start goes on with a login there); "Start without a login" is remembered for this run
        of the app and said, and the start goes on with what else it needs; Cancel: not started."""
        busy = self.__dict__.setdefault("preparing_contexts", set())
        stages = self.__dict__.setdefault("preparing_stages", {})
        place = tr("on this machine") if host == LOCAL else tr("on {host}", host=printable(host))

        def over():
            busy.discard(context)
            stages.pop(context, None)

        def again():
            """The needs read once more (without the logins that are there now, or left out), then THEN."""
            over()
            self.prerequisites_done = None
            if self.prerequisite_context() != context:
                self.drop_waiter(context, why)
                return
            if then is not None and not self.check_prerequisites(then=then, why=why):
                then()
        run = self.login_terminal()
        answer = self.ask_login(host, agents) if why == "start" else None
        if answer == "login" and run is None:
            warn(self, tr("Not started"), tr("No terminal could be opened for the login. Run it yourself, then start "
                                             "again: {command}", command="sandbox-ai login " + printable(agents[0])))
            answer = None
        if answer is None:
            applog.log("INFO", "prerequisites", f"{why}: not started - no login of {', '.join(agents)} on "
                                                f"{printable(host)}, and none was asked for")
            self.prerequisite_notice(tr("Not started: there is no login for {agents} {place}.", place=place,
                                        agents=printable(", ".join(agents))), "notice-warn")
            self.show_missing(tr("Not started: the start needs what is not there yet."), [(
                tr("There is no login for {agents} {place}.", place=place, agents=printable(", ".join(agents))),
                [(tr("Start again"), lambda: then() if then is not None and self.prerequisite_context() == context
                  else None)])])
            over()
            self.drop_waiter(context, why)
            return
        if answer == "without":
            self.__dict__.setdefault("login_skipped", set()).update((host, name) for name in agents)
            applog.log("INFO", "prerequisites", f"{why}: started without a login of {', '.join(agents)} on "
                                                f"{printable(host)} (chosen; not asked again until the app ends)")
            self.prerequisite_notice(tr("Started without a login of {agents} {place}: the agent asks for it in its "
                                        "terminal (Codex: its device sign-in runs there first).",
                                        agents=printable(", ".join(agents)), place=place), "notice-warn", status=True)
            again()
            return
        left = list(agents)

        def next_login():
            name = left.pop(0)
            stages[context] = (time.monotonic(), f"the login of {name} in a terminal on {host}")
            try:
                argv = self.sai.login_argv(host, name)
            except Exception as exc:  # noqa: BLE001 - said; the start waits
                warn(self, tr("Not started"), printable_text(exc))
                over()
                self.drop_waiter(context, why)
                return
            title = (f"{printable(host)}: " if host != LOCAL else "") + tr("Log in to {agents}", agents=name)
            applog.log("INFO", "prerequisites", f"{why}: the login of {name} on {printable(host)} runs in a terminal; "
                                                "the start goes on when it ends with a login")
            self.prerequisite_notice(tr("Log in to {agent} in its terminal ({place}): the start goes on when it "
                                        "ends with a login.", agent=name, place=place), "secondary", status=True)

            def ended(status):
                applog.log("INFO" if status == 0 else "WARNING", "prerequisites",
                           f"{why}: the login of {name} on {printable(host)} ended with exit status {status}")
                if status != 0:
                    text = tr("The login of {agent} {place} did not end with a login (exit status {status}): not "
                              "started.", agent=name, place=place, status=status)
                    self.prerequisite_notice(text, "notice-warn", toast=True)
                    self.show_missing(tr("Not started: the start needs what is not there yet."), [(text, [
                        (tr("Start again"), lambda: then() if then is not None
                         and self.prerequisite_context() == context else None)])])
                    over()
                    self.drop_waiter(context, why)
                    return
                if left:
                    next_login()
                    return
                self.prerequisites_made(host)
                again()
            if not run(argv, title, ended):
                ended(-1)
        busy.add(context)
        next_login()

    def resolve_mcp_entries(self, host, servers, entries):
        """({server: "prefix:name"} chosen, {server: why it is not added}) for SERVERS, by the catalogs of HOST
        ENTRIES (None: not read - nothing is said, `mcp add` has the last word): a server whose catalog entry the
        start names (hub:binwalk) or that one catalog has is added as before; one in no catalog is not tried, and
        said (the MCP servers page defines one of your own); one in several is asked for - Cancel: not added."""
        from .widgets import choose
        chosen, unknown = {}, {}
        if entries is None:
            return chosen, unknown
        named = {**self.prerequisite_mcp_entries(), **self.__dict__.get("mcp_entries_chosen", {})}
        for server in servers:
            if named.get(server) and catalog_entry(entries, named[server]) is not None:
                continue
            prefixes = catalog_prefixes(entries, named.get(server) or server)
            if len(prefixes) == 1 or "" in prefixes:  # (one catalog has it, or one that names none: as before)
                continue
            if not prefixes:
                unknown[server] = tr("MCP server {name}: no catalog of {host} has it (sandbox-ai mcp catalog there), so "
                                     "it is not added and the start does not go on - define it on the MCP servers "
                                     "page, or take it off the start.", name=printable(server),
                                     host=printable(host))
                applog.log("INFO", "board", f"MCP server {server}: in no catalog of {host}: not added")
                continue
            title = tr("MCP server {name}", name=printable(server))
            answer = choose(self, title, tr(
                "{name} is in several catalogs of {host}: {list}. Which one is added?", name=printable(server),
                host=printable(host), list=", ".join(f"{p}:{server}" for p in prefixes)),
                [(prefix, f"{prefix}:{server}") for prefix in prefixes])
            if answer not in prefixes:
                unknown[server] = tr("MCP server {name}: not added - it is in several catalogs and none was chosen "
                                     "(the MCP servers page adds the one you want).", name=printable(server))
                continue
            chosen[server] = f"{answer}:{server}"
            applog.log("INFO", "board", f"MCP server {server}: added from {answer}:{server} (chosen)")
        return chosen, unknown

    def ask_mcp_folders(self, host, servers, argv, entries):
        """({server: [TARGET=HOST]}, {server: why it is not added}) for the folders the catalog entries of SERVERS
        require (ENTRIES: the catalogs read, None: not read) that the start ARGV does not give them (--mcp-mount):
        each asked for - the start's workspace (when it is a folder of HOST: prerequisite_workspace), another folder
        (chosen; on another host from that host's folders, remote_chooser, starting in the workspace there or its
        HOME - never at a path of this machine; New folder… makes one there), or Cancel (that server is not added,
        and said)."""
        from . import remote_chooser as RC
        from .state import file_dialog, run_dialog
        from .widgets import choose
        folders, dropped = {}, {}
        workspace = self.prerequisite_workspace(host, argv)
        for server in servers:
            entry = self.mcp_entry_of(server)
            required = required_folders(entries, entry) if entries is not None else None
            given = {value.partition("=")[0] for value in start_mounts(argv, server, host)}
            # (0.1.214) A folder with a default where the server is added - /work the start's workspace, any other
            # one of the server's own - is not asked for: `mcp add` without --mount gives it. Asked only where that
            # sandbox-ai has no defaults (an older one: its add would refuse it), or where the start names the folder
            # but gives it none (cleared, a preset's folder not filled in, a sandbox's view of a folder).
            cleared = start_named_targets(argv, server) - given
            defaults = self.mcp_folder_defaults(host, entries, entry) if required else {}
            for target in [t for t in required or [] if t not in given and t in defaults and t not in cleared]:
                applog.log("INFO", "board", f"MCP server {server}: {target} gets its default folder on "
                                            f"{printable(host)} (" + ("the start's workspace" if defaults[target].get(
                                                "default") == "workspace" else "a folder of its own") + ")")
            for target in [t for t in required or [] if t not in given and (t not in defaults or t in cleared)]:
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
                    path = RC.choose_folder(self, host, title, "mcp-folder", start=workspace or "") or None
                if not path:
                    dropped[server] = tr("MCP server {name}: not added - it needs a folder for {target} (the MCP page "
                                         "adds it with one).", name=printable(server), target=printable(target))
                    folders.pop(server, None)
                    break
                folders.setdefault(server, []).append(f"{target}={path}")
                applog.log("INFO", "board", f"MCP server {server}: {target} gets the folder {path}")
        return folders, dropped

    def mcp_folder_defaults(self, host, entries, name):
        """{target: {"default": "workspace"|"own", "mode"}} of the folders the catalog entry NAME (of ENTRIES) gets
        by default where HOST's sandbox-ai adds it (mcp_fields.folder_defaults: this machine's MCP module, another
        host's `mcp catalog --json`); {} where it has none (older than 0.1.212) or the entry is not known."""
        from . import mcp_fields as MF
        entry = catalog_entry(entries, name)
        if entry is None:
            return {}
        return MF.folder_defaults(MF.mcp_module(self.sai) if host == LOCAL else None, entry,
                                  "local" if host == LOCAL else host)

    def ask_mcp_builds(self, host, servers, argv, entries, folders, networks, context, why, then):
        """SERVERS (each listed by its catalog of HOST, ENTRIES, without a signed image yet) offered to be built there
        from their source: what would be built read first (`mcp add ... --build-plan` on HOST, in the background:
        the catalog's repository at its exact commit, the source archive's SHA-256), then asked, each on its own - it
        is built and added UNSIGNED. THEN({server: build_args}, {server: why it is not built}). Nothing is built
        without a yes, and a yes builds that commit and archive only (--commit, --source-sha256)."""
        page = (self.__dict__.get("mcp_page_now") or {}).get(context) or {"plans": {}, "decided": {}}

        def read_plans():
            if all(isinstance(page["plans"].get(server), dict) for server in servers):
                return {server: page["plans"][server] for server in servers}  # (read for the question)
            return self.read_build_plans(host, servers, argv, entries, folders, networks, why)

        def asked(plans, error):
            builds, refused = {}, {}
            if self.prerequisite_context() != context:
                applog.log("INFO", "prerequisites", f"{why}: the page shows another start now - not built")
                then({}, {server: tr("MCP server {name}: not built (another start is shown now).",
                                     name=printable(server)) for server in servers})
                return
            for server in servers:
                plan = (plans or {}).get(server, error)
                entry = unpublished_entry(entries, self.mcp_entry_of(server)) or {}
                if not isinstance(plan, dict):
                    refused[server] = tr("MCP server {name}: its catalog has no signed image of it yet, and what "
                                         "would be built from its source on {host} could not be read: {error}",
                                         name=printable(server), host=printable(host),
                                         error=printable_text(plan if plan is not None else "?")[:400])
                    continue
                signed = tr("signed: the entry was reviewed, its image is not published yet") \
                    if entry_signature(entry) == "good" else tr("NOT signed: nobody vouched for the entry")
                yes = page["decided"][server] if server in page["decided"] else ask_first(
                    self, "mcp-build", tr("Build MCP server {name}?", name=printable(server)), tr(
                    "{entry} has no signed image in its catalog yet. Build it {place} from its reviewed source and "
                    "add it UNSIGNED?\n\nSource: {repository}, commit {commit}, {path}\nSource archive SHA-256: "
                    "{sha256}\nCatalog: {catalog} ({signed})\n\nPodman builds it as the catalog's CI does (base images "
                    "pinned by digest) and its smoke test checks it with sandbox-ai's own run line; that takes "
                    "minutes, its output goes to the tab Preparing the start. Built here, not signed: nobody signed "
                    "this image, and it is shown so wherever it appears.", entry=plan["entry"],
                    place=tr("on this machine") if host == LOCAL else tr("on {host}", host=printable(host)),
                    repository=plan["repository"], commit=plan["commit"], path=plan["path"],
                    sha256=plan["archive_sha256"], catalog=plan["catalog_label"], signed=signed), tr("Build"))
                applog.log("INFO", "prerequisites", f"{why}: build {server} on {printable(host)} from "
                           f"{plan['repository']} at {plan['commit']} (archive {plan['archive_sha256']}): "
                           + ("yes" if yes else "no"))
                if yes:
                    builds[server] = build_args(plan, entry)
                else:
                    refused[server] = tr("MCP server {name}: not built - its catalog has no signed image of it yet "
                                         "(sandbox-ai mcp add {name} --build builds it from its reviewed source and "
                                         "adds it unsigned, asked first).", name=printable(server))
            then(builds, refused)
        self.__dict__.setdefault("preparing_stages", {})[context] = (
            time.monotonic(), f"reading what building {', '.join(servers)} on {host} would build")
        background(read_plans, owner=self, timeout=900, done=asked,
                   what=f"reading what building the MCP servers {', '.join(servers)} on {host} would build")

    def read_mcp_catalog(self, host):
        """The MCP catalogs' entries as HOST reads them (this machine's, or `mcp catalog --json` there)."""
        if host == LOCAL:
            return self.sai.mcp_catalog()[0]
        data = self.sai.host_json(host, "mcp", "catalog", "--json", timeout=150)
        return data.get("entries", []) if isinstance(data, dict) else []

    def read_build_plans(self, host, servers, argv, entries, folders, networks, why):
        """{server: build plan (checked_plan) or the ValueError why none} of SERVERS, which their catalogs of HOST
        list without a signed image yet: `mcp add ... --build-plan` there - what would be built (the catalog's
        repository at its exact commit, the source archive's SHA-256), nothing built."""
        named = {**self.prerequisite_mcp_entries(), **self.__dict__.get("mcp_entries_chosen", {})}
        plans = {}
        for server in servers:
            entry = unpublished_entry(entries, self.mcp_entry_of(server)) or {}
            args = build_plan_args(mcp_add_args(argv, server, folders.get(server, ()), named.get(server),
                                                server in networks, host), entry)
            status, out, err = self.sai.host_run(host, *args, timeout=600)
            applog.log("INFO" if status == 0 else "WARNING", "prerequisites",
                       f"{why}: what building {server} on {printable(host)} would build: exit {status}"
                       + ("" if status == 0 else ": " + applog.redact((err or out or "").strip()[-2000:])))
            if status != 0:
                plans[server] = ValueError((err or out or "").strip().splitlines()[-1:][0]
                                           if (err or out or "").strip() else f"exit status {status}")
                continue
            try:
                plans[server] = checked_plan(out)
            except ValueError as exc:
                plans[server] = exc
        return plans

    def ask_mcp_network(self, host, servers, entries):
        """({servers added with the outside network}, {server: why it is not added}) for SERVERS: nothing is asked
        any more. A server whose catalog entry (of ENTRIES, None: not read) lists the hosts it needs is added with
        exactly those (`mcp add`'s default for such an entry: network allowlist, held in its namespace) - before,
        sandbox-ai could not limit it to them and the Board asked whether to give it the whole outside network."""
        for server in servers:
            hosts = needed_hosts(entries, self.mcp_entry_of(server)) if entries is not None else []
            if hosts:
                applog.log("INFO", "board", f"MCP server {server}: added with only the hosts its catalog entry "
                                            f"needs ({', '.join(hosts)})")
        return set(), {}

    def provide_rest(self, identities, variants, to_build, tor_profiles, servers, folders, problems, host, key, then,
                     why, context, agent, argv, networks=(), images=()):
        """provide(), once the MCP servers' folders are known (FOLDERS: {server: [TARGET=HOST]}). PROBLEMS: [(what
        is not made, [(button, function)])]. NETWORKS: the servers added with the outside network (a yes to it).
        IMAGES: the services' images of sandbox-ai's own Containerfiles to build (images build-image --builtin)."""
        images = list(images)
        # The agent's own image, not built and asked for (provide: agent_builds): built first.
        agent_images = list(self.__dict__.get("agent_builds", {}).pop(context, None) or [])
        # The fixes of the launcher's own plan that were asked for (provide: plan_fixes): each its commands on HOST.
        fixes = list(self.__dict__.get("plan_fixes") or [])
        self.plan_fixes = []
        busy = self.__dict__.setdefault("preparing_contexts", set())
        problems = [p if isinstance(p, tuple) else (p, []) for p in problems]
        stages = self.__dict__.setdefault("preparing_stages", {})
        if problems and why == "start":
            warn(self, tr("Not started"), "\n".join(text for text, _ in problems))
            self.show_missing(tr("Not started: the start needs what is not there yet."), problems)
            if not (identities or variants or to_build or tor_profiles or servers or images or fixes or agent_images):
                applog.log("INFO", "prerequisites", f"{why}: not started, nothing can be made for it here")
                busy.discard(context)
                self.drop_waiter(context, why)
                stages.pop(context, None)
                return
            then = None  # what can be made is made; the start waits for the rest
            applog.log("INFO", "prerequisites", f"{why}: what can be made is made; the start itself waits for the rest")
        elif problems:
            warn(self, tr("Preparing the start"), "\n".join(text for text, _ in problems))
            self.show_missing(tr("Not made for the start:"), problems)
        busy.add(context)
        fix_steps = [(f"{item['title']}: {command_text}", item, tuple(command[1:]))
                     for item in fixes for command in item["commands"]
                     for command_text in [" ".join(command)]]
        # (what is done on THIS machine - the update of an older host, sandbox-ai hosts update HOST: here, first)
        local_steps = [step for step in fix_steps if step[1].get("here")]
        fix_steps = [step for step in fix_steps if not step[1].get("here")]
        steps = [*(label for label, _item, _args in local_steps),
                 *(tr("the image of {name} (built: about 5 minutes)", name=n) for n in agent_images),
                 *(label for label, _item, _args in fix_steps),
                 *(tr("identity {name}", name=n) for n in identities),
                 *(tr("image variant {name} (made and built: some minutes)", name=n) for n in to_build),
                 *(tr("image variant {name}", name=n) for n in variants if n not in to_build),
                 *(tr("Tor profile {name}", name=n) for n in tor_profiles),
                 *(tr("MCP server {name}", name=n) + (tr(" (built from its source: minutes)")
                                                       if n in (self.__dict__.get("mcp_builds") or {}) else "")
                   for n in servers),
                 *(tr("service image {name} (built: a minute or two)", name=n) for n in images)]
        if not steps:  # nothing to make here (each part said above why): no "Preparing: ." whose end never came
            busy.discard(context)
            stages.pop(context, None)
            if not problems and then is not None and why == "start" and self.prerequisite_context() == context:
                # (only what the start goes on without was declined - an older host's update: it goes on as before)
                applog.log("INFO", "prerequisites", f"{why}: nothing is made (declined); the start goes on")
                self.prerequisites_done = key
                try:
                    then()
                finally:
                    if getattr(self, "prerequisites_done", None) == key:
                        self.prerequisites_done = None
                return
            applog.log("INFO", "prerequisites", f"{why}: nothing is made (each part declined or not possible)")
            self.drop_waiter(context, why)
            return
        # (a preset's hub:binwalk: never `mcp add binwalk`, in two catalogs; one chosen of several: resolve_mcp_entries)
        entries = {**self.prerequisite_mcp_entries(), **self.__dict__.get("mcp_entries_chosen", {})}
        builds = dict(self.__dict__.get("mcp_builds") or {})  # (asked: ask_mcp_builds)
        self.mcp_builds = {}
        stages[context] = (time.monotonic(), "making " + ", ".join(steps))
        applog.log("INFO", "prerequisites", f"{why} on {printable(host)}: making " + printable(", ".join(steps)))
        self.prerequisite_notice(tr("Preparing what the start needs: {list}.", list=printable(", ".join(steps))) + " "
                                 + done_said(), "secondary", toast=True)
        # The builds and MCP servers (minutes) in a terminal tab, seen as they go; the rest (seconds) here.
        # (the launcher's fixes first: the agent's image before what is added or built for it)
        slow = [("image", name, tr("building the image of {name}", name=name), ("build", name))
                for name in agent_images] + \
            [("fix", label, label, args) for label, _item, args in fix_steps] + \
            [("build", name, tr("building the image variant {name}", name=name),
              ("build", "--variant", self.variant_target(name, agent))) for name in to_build] + \
            [("mcp", name, tr("building the MCP server {name} from its source (unsigned; minutes)", name=name)
              if name in builds else tr("adding the MCP server {name} (its image is pulled)", name=name),
              mcp_add_args(argv, name, folders.get(name, ()), entries.get(name), name in networks,
                           host, builds.get(name, ())))
             for name in servers] + \
            [("service", name, tr("building the service image {name} (sandbox-ai's own Containerfile)", name=name),
              ("images", "build-image", name, "--builtin", name)) for name in images]
        in_tab = self.prerequisite_terminal() if slow else None
        here = fix_steps  # (made here, in the background, where no terminal tab runs them)
        if in_tab is not None:
            to_build, servers, images, here, agent_images = [], [], [], [], []
        if agent_images or any(kind == "image" for kind, *_rest in slow):  # (built once per preparation)
            self.__dict__.setdefault("images_attempted", {}).setdefault(context, set()).update(
                name for kind, name, *_rest in slow if kind == "image")
        if fixes:  # (a fix that ran and is missing still is not offered again in this preparation)
            self.__dict__.setdefault("plan_attempted", {}).setdefault(context, set()).update(
                item["id"] for item in fixes)
        # What is made, in the order it is made (work() here, then the tab): the Board's progress and its line.
        order = [*(("fix", label) for label, _item, _args in local_steps), *(("image", n) for n in agent_images),
                 *(("fix", label) for label, _item, _args in here),
                 *(("identity", n) for n in identities), *(("recipe", n) for n in variants),
                 *(("build", n) for n in to_build), *(("vpn", n) for n in tor_profiles),
                 *(("mcp", n) for n in servers), *(("service", n) for n in images)]
        tab = [(kind, name) for kind, name, _, _ in slow] if in_tab is not None else []
        preparation = P.Preparation([P.Item(kind, name) for kind, name in order] +
                                    [P.Item(kind, name, in_tab=True) for kind, name in tab])
        index_of = {(item.kind, item.name): i for i, item in enumerate(preparation.items) if not item.in_tab}
        self.track(preparation, problems)

        def work():
            def run(*args, timeout=600, where=None):
                began = time.monotonic()
                status, out, err = self.sai.host_run(where or host, *args, timeout=timeout)
                text = (out + err).strip()
                applog.log("INFO" if status == 0 else "ERROR", "board", f"sandbox-ai {applog.argv_text(args)} on "
                           f"{printable(host)}: exit {status} after {applog.seconds(time.monotonic() - began)}\n"
                           + applog.redact(text[-20000:]))
                return status, text

            def step(kind, name, *args, timeout, where=None):
                """One item made here (from this thread: the page's timer shows how far)."""
                index = index_of.get((kind, name))
                if index is not None and preparation.items[index].state == "waiting":
                    preparation.start(index)
                status, text = run(*args, timeout=timeout, where=where)
                if index is not None:
                    if text:
                        preparation.line(index, text.splitlines()[-1])
                    preparation.finish(index, status == 0, status)
                return status, text
            said = []
            for label, _item, args in local_steps:  # (on this machine: an older host's update)
                said.append(("fix", label, *step("fix", label, *args, timeout=1800, where=LOCAL)))
            if any(status != 0 for _kind, _name, status, _text in said):
                return said  # (an update that failed: nothing else is tried on that host)
            for name in agent_images:  # (no terminal tab: built here, its output in the log)
                said.append(("image", name, *step("image", name, "build", name, timeout=3600)))
            for label, _item, args in here:
                if any(kind == "fix" and status != 0 for kind, _n, status, _t in said):
                    said.append(("fix", label, -1, tr("not tried: an earlier step failed")))
                    continue
                said.append(("fix", label, *step("fix", label, *args, timeout=7200)))
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
                said.append(("mcp", name, *step("mcp", name, *mcp_add_args(argv, name, folders.get(name, ()),
                                                                           entries.get(name), name in networks, host,
                                                                           builds.get(name, ())),
                                                timeout=7200 if name in builds else 900)))
            for name in images:
                said.append(("service", name, *step("service", name, "images", "build-image", name, "--builtin", name,
                                                    timeout=3600)))
            return said

        def quick_done(said, error):
            """The quick part made: the slow one in its terminal tab (made() at its end), or the end now."""
            if error is not None or in_tab is None or any(status != 0 for _, _, status, _ in said):
                made(said, error)
                return
            steps_ = [[title, self.sai.command(*args) if host == LOCAL else self.sai.host_terminal(host, *args)]
                      for _, _, title, args in slow]
            # Its last line says what comes next: the start in a tab of its own, or - prepared for a preset or a
            # button, no start waiting - that nothing starts by itself (it said "the start goes on" either way).
            argv = self.chain_argv(preparation, steps_, end=tr("the start goes on in a tab of its own")
                                   if then is not None else
                                   tr("nothing starts by itself: Start starts it (a Start pressed meanwhile goes on "
                                      "now, in a tab of its own)"))

            def ended(status):
                applog.log("INFO" if status == 0 else "ERROR", "prerequisites", f"{why}: the terminal tab Preparing "
                           f"the start ended with exit status {status}: " + ", ".join(t for _, _, t, _ in slow))
                text = "" if status == 0 else tr("see the terminal tab Preparing the start (exit status {status})",
                                                 status=status)
                # The tab stops at its first failure: by its status file, a step done is made, one never begun is
                # said as not tried - not each as failed.
                preparation.read()
                items = {(item.kind, item.name): item for item in preparation.items if item.in_tab}
                stopped = status != 0 and any(item.state == "failed" for item in items.values())
                results = []
                for kind, name, _, _ in slow:
                    item = items.get((kind, name))
                    if status == 0 or (item is not None and item.state == "done"):
                        results.append((kind, name, 0, ""))
                    elif stopped and item is not None and item.state == "waiting":
                        results.append((kind, name, -1, tr("not tried: an earlier step failed")))
                    else:
                        results.append((kind, name, status, text))
                made(said + results, None)
            stages[context] = (time.monotonic(), "in the terminal tab Preparing the start: "
                               + ", ".join(t for _, _, t, _ in slow))
            applog.log("INFO", "prerequisites", f"{why}: the slow part runs in the terminal tab Preparing the start: "
                       + ", ".join(t for _, _, t, _ in slow))
            if not in_tab(argv, tr("Preparing the start"), ended=ended):
                applog.log("WARNING", "prerequisites", f"{why}: the terminal tab Preparing the start did not open")
                ended(-1)

        def made(said, error):
            busy.discard(context)
            # A Start pressed meanwhile (check_prerequisites kept it): it goes on with this preparation's end.
            waiter = self.__dict__.get("preparing_waiters", {}).pop(context, None)
            nonlocal then
            if then is None and waiter is not None:
                then = waiter
                applog.log("INFO", "prerequisites", f"{why}: a start waits for this preparation: it goes on when "
                                                    "everything is made, not when something failed")
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
                     "vpn": tr("Tor profile"), "mcp": tr("MCP server"), "service": tr("service image"),
                     "fix": tr("done"), "image": tr("image of")}
            ok = [f"{words[kind]} {name}" for kind, name, status, _ in said if status == 0]
            if ok:
                self.prerequisites_made(host)  # (the page's lists of that host read again: the Board's MCP servers)
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
        # (the launcher's own plan: what it says is missing beyond this check's own needs, in a sentence of its own)
        also = [item.get("missing") or item.get("id") or "?" for item in missing.get(PLAN, [])]
        for option, names in needs.items():
            if option == PLAN:
                continue
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
        text = tr("The start needs on {host}: {list}.", host=host, list=", ".join(wanted)) if wanted else \
            tr("The start was checked on {host}.", host=host)
        if there:
            text += " " + tr("There: {list}.", list=", ".join(there))
        if lacking:
            text += " " + tr("Missing: {list}.", list=", ".join(lacking))
        if unknown:
            text += " " + tr("Not answered (read again at the next start): {list}.", list=", ".join(unknown))
        if also:
            text += " " + tr("sandbox-ai there also says: {list}.", list="; ".join(also))
        self.__dict__.setdefault("prerequisite_lacking", {})[key] = lacking + also or unknown
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

    def chain_argv(self, preparation, steps, end=None):
        """The terminal tab's command for STEPS ([[title, argv]]): CHAIN, writing its status to a file of a private
        folder that PREPARATION reads (none when no folder can be made: it runs all the same). END: what its last line
        says comes next (default: the start goes on)."""
        argv = [sys.executable, "-I", "-c", CHAIN, *([f"--end={printable(end)}"] if end else []), json.dumps(steps)]
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
