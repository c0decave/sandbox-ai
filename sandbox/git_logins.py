#!/usr/bin/env python3
"""sandbox-ai git-logins: your Git servers' logins, used by the connection proxy of every sandbox, never inside it.

  sandbox-ai git-logins add URL --secret NAME [--user USER]   a login: the token is the secret NAME
  sandbox-ai git-logins list [--json]                         the logins (servers, users, secret names)
  sandbox-ai git-logins edit URL [--url NEW] [--user USER] [--secret NAME]
                                                              change a login: its server, its user, which secret
  sandbox-ai git-logins remove URL
  sandbox-ai git-logins check [URL]                           whether each login's secret is there
  sandbox-ai git-logins give RUN URL (--secret NAME | --env VARIABLE | --file PATH) [--user USER] [--keep]
                        [--in-sandbox]                        a running sandbox gets a Git login now, until it ends
  sandbox-ai git-logins revoke RUN URL                        take back a login given during the run
  sandbox-ai git-logins list --run RUN [--json]               the logins a running sandbox was given during the run

URL is http(s)://HOST[:PORT] (plain http only for this host's loopback, e.g. a local Gitea at
https://git.example.org, http://localhost:3080). Keep the token first: sandbox-ai secrets set NAME (it is read from standard input).

Every sandbox with the connection proxy gets these logins in the proxy's own directory, which the agent cannot
read: the proxy adds them on the wire to Git's smart HTTP requests to that server (clone, fetch, push), and only
there, never to the agent's other requests and never into a log. The agent's Git needs no credentials at all, and
no token ever enters its sandbox. The server must be reachable as usual: an HTTPS server by --allow-host or a
preset (github.com: --allow-github), a server on this host's loopback by --allow-host-port PORT or, during the run,
sandbox-ai net allow --allow-host-port PORT. A --git-token-* option for the same server wins for its start;
--git-logins off starts without them.

A sandbox that runs already gets a login with `give` (the run's id or its start's name as `sandbox-ai list` shows
it): the login goes to its connection proxy, as a login of this list does at a start - the proxy logs in on the wire
from now on, until the run ends, no token enters the sandbox, and where the run already has a login for that server
(this list's, an identity's, --token-proxy's) the one given wins. --keep also writes it into the project's trusted
.sandbox-ai.json, as a reference (never the token). It is the same step as the older form `sandbox-ai net allow RUN
--git-token-secret URL=NAME` (--git-token-env, --git-token-file), which keeps working; the server is allowed as at a
start (github.com: its preset). `sandbox-ai net show RUN` lists them too. Only where the proxy cannot log in - a run
without the proxy, a server the run does not inspect (--no-inspect), a run started by an older sandbox-ai
- a login can be put into the sandbox's session directory for its Git helper instead, where the agent can
read the token: refused unless you say so with --in-sandbox (net allow: --git-token-in-sandbox).

`edit` changes a login in place (the token itself is the secret's: sandbox-ai secrets set NAME replaces its value,
sandbox-ai secrets rename OLD NEW its name, everywhere). A change is for the starts from then on: a sandbox that
runs keeps the login its proxy was started with, until it is given the new one (git-logins give, said by edit).
When the server's address changes, edit says what else still names the old one (an identity, a project file's or
a kept start's --git-token-* option).

The list is ~/.config/sandbox-ai/git-logins.json (mode 0600): servers, users and secret names, no tokens.
Standard library only.
"""
import argparse
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import threading

LOADING = threading.RLock()  # the lazily loaded helper modules below

HERE = Path(__file__).resolve().parent


_filelock = None


def settings_lock(file):
    """The lock of a read-modify-write of the settings file FILE (filelock.py): commands that run at the same time
    never lose each other's change."""
    global _filelock
    if _filelock is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_filelock", HERE / "filelock.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        _filelock = loaded
    return _filelock.locked(file)
SECRET = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
USER = re.compile(r"[A-Za-z0-9._-]{1,64}")
_launcher = None


def launcher():
    global _launcher
    if _launcher is None:
        spec = importlib.util.spec_from_file_location("agent_sandbox_for_git_logins", HERE / "agent_sandbox.py")
        with LOADING:  # published only once loaded: another thread never sees a half-made module
            if _launcher is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _launcher = loaded
    return _launcher


_net = None


def net_module():
    """net.py, which gives a running sandbox a login (`give` and `revoke` are its `net allow/revoke --git-token-*`:
    one code path), with this module's launcher."""
    global _net
    if _net is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_net_for_git_logins", HERE / "net.py")
        with LOADING:
            if _net is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                loaded._launcher_source = launcher
                _net = loaded
    return _net


def with_user(url, user):
    """URL with USER in it (http(s)://USER@HOST[:PORT]), as net allow --git-token-* takes it; a URL that names
    another user already is refused."""
    scheme, sep, rest = url.partition("://")
    if not user:
        return url
    if not USER.fullmatch(user):
        raise ValueError("--user is letters, digits, ._-")
    if not sep:
        return url  # (refused by the URL check, with its own message)
    named, at, _host = rest.partition("@")
    if at:
        if named != user:
            raise ValueError(f"the URL names the user {named}, --user {user}: give one of them")
        return url
    return f"{scheme}://{user}@{rest}"


_credrefs = None


def credrefs():
    """credrefs.py: what else names a server or a secret, and the sandboxes that run now."""
    global _credrefs
    if _credrefs is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_credrefs_for_git_logins", HERE / "credrefs.py")
        with LOADING:
            if _credrefs is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                loaded._modules.setdefault("agent_sandbox", launcher())
                _credrefs = loaded
    return _credrefs


def running_note(url, secret, user, out):
    """Said after a login changed: the sandboxes that run keep what they were started with, and the command that
    gives each the login as it is now."""
    try:
        runs = credrefs().running_starts()
    except Exception:  # noqa: BLE001 - a note only: the change itself is done
        runs = []
    if not runs:
        return
    printable = launcher().printable
    names = ", ".join(printable(name) for _, name in runs[:8]) + (" ..." if len(runs) > 8 else "")
    out(f"note: this is for the starts from now on; {len(runs)} sandbox{'es' if len(runs) != 1 else ''} run"
        f"{'' if len(runs) != 1 else 's'} with what {'they were' if len(runs) != 1 else 'it was'} started with "
        f"({names}). To give one the login as it is now: sandbox-ai git-logins give RUN {printable(url)} --secret "
        f"{secret}" + (f" --user {user}" if user and user != "x-access-token" else ""))


def given_main(args, out):
    """give, revoke and list --run: net.py's allow, revoke and given credentials of the run."""
    net = net_module()
    if args.command == "list":
        return net.list_given(args.run, "git", out, as_json=args.json, prog="git-logins")
    try:
        url = with_user(args.url, getattr(args, "user", None))
    except ValueError as exc:
        print(f"sandbox-ai git-logins: {exc}", file=sys.stderr)
        return 1
    if args.command == "give":
        kind, ref = next((kind, value) for kind, value in (("secret", args.secret), ("env", args.env),
                                                           ("file", args.file)) if value)
        argv = ["allow", args.run, f"--git-token-{kind}", f"{url}={ref}", *(["--keep"] if args.keep else []),
                *(["--git-token-in-sandbox"] if args.in_sandbox else [])]
    else:
        argv = ["revoke", args.run, "--git-token-secret", f"{url}=X"]  # (the reference is not read by a revoke)
    return net.main(argv, out=out, brief=True, prog="git-logins", command=f"git-logins {args.command}")


def path():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "sandbox-ai" / "git-logins.json"


def load():
    """[{"url", "user", "secret"}] of the list ([] without one); ValueError for a damaged or unsafe file."""
    file = path()
    try:
        info = os.lstat(file)
    except FileNotFoundError:
        return []
    if not os.path.isfile(file) or os.path.islink(file) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError(f"{file} must be a regular file of yours with mode 0600")
    try:
        data = json.loads(file.read_text())
    except RecursionError:  # (nested deeper than Python reads: damaged)
        raise ValueError(f"{file}: not valid JSON (nested too deeply)") from None
    except (OSError, ValueError) as exc:
        raise ValueError(f"{file}: {exc}") from None
    logins = data.get("logins") if isinstance(data, dict) and data.get("version") == 1 else None
    if not isinstance(logins, list):
        raise ValueError(f"{file}: version 1 with a list of logins")
    for entry in logins:
        if not (isinstance(entry, dict) and isinstance(entry.get("url"), str)
                and isinstance(entry.get("secret"), str) and SECRET.fullmatch(entry["secret"])
                and isinstance(entry.get("user"), str) and USER.fullmatch(entry["user"])):
            raise ValueError(f"{file}: each login is {{url, user, secret}}")
    return logins


def save(logins):
    file = path()
    file.parent.mkdir(parents=True, exist_ok=True)
    # A name of its own each time, gone again when the write fails: one left behind (a full disk, a kill) by this
    # PID never refuses a later save.
    temporary = file.with_name(f".{file.name}.{os.getpid()}-{os.urandom(4).hex()}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump({"version": 1, "logins": logins}, stream, indent=2)
        os.replace(temporary, file)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def server(url):
    """{url, user, host, port, loopback} of URL as the launcher checks a --git-token-* server."""
    return launcher().git_server(url, "git-logins")


def credentials(skip=(), entries=None, origin="git-logins"):
    """[(credential, token)] for the proxy: each login (of the list, or ENTRIES: an identity's) whose server is not
    in SKIP (origins a --git-token-* option of the start names), its token read from the secret store. A login
    whose secret is missing is left out and named in the notes. Returns (pairs, notes)."""
    pairs, notes = [], []
    store = launcher().load_secret_store_module()
    for entry in load() if entries is None else entries:
        credential = server(entry["url"])
        if credential["url"] in skip:
            continue
        token = store.get(entry["secret"])
        if token is None:
            notes.append(f"{origin}: {credential['url']} left out: no secret {entry['secret']} "
                         f"(sandbox-ai secrets set {entry['secret']})")
            continue
        token = token.strip()  # (a line end or blanks around it: never part of a token)
        problem = launcher().token_problem(token)
        if problem is not None:  # (kept, but not a token: said what is wrong, never the value)
            # (the usual cause: a line break or a blank pasted with it - said, with the command that takes it again)
            notes.append(f"{origin}: {credential['url']} left out: the secret {entry['secret']} is no access token - "
                         f"{problem} (enter it again: sandbox-ai secrets set {entry['secret']} - a line break or a "
                         "blank pasted with it is the usual cause)")
            continue
        pairs.append(({**credential, "user": entry["user"]}, token))
    return pairs, notes


def help_style(prog):
    """The layout of this command's help, the one of every command with subcommands (helpstyle.py)."""
    loaded = sys.modules.get("sandbox_ai_helpstyle")
    if loaded is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_helpstyle", HERE / "helpstyle.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        sys.modules["sandbox_ai_helpstyle"] = loaded
    return loaded.CommandsFormatter(prog)


def main(argv, out=print):
    p = argparse.ArgumentParser(prog="sandbox-ai git-logins", description=__doc__.split("\n\n")[0],
                                formatter_class=help_style, allow_abbrev=False,
                                epilog="\n\n".join(__doc__.split("\n\n")[1:6]))
    commands = p.add_subparsers(dest="command", required=True)
    added = commands.add_parser("add", help="a login: the token is the secret NAME")
    added.add_argument("url")
    added.add_argument("--secret", required=True, metavar="NAME", help="the secret (sandbox-ai secrets) holding the access token")
    added.add_argument("--user", default="x-access-token", help="the user name sent with the token (default: x-access-token)")
    listing = commands.add_parser("list", help="the logins (servers, users, secret names)")
    listing.add_argument("--json", action="store_true", help="print the result as JSON (for programs)")
    edited = commands.add_parser("edit", help="change a login: its server, its user, which secret",
                                 description="Change the login for URL in place: --url its server's address, --user "
                                             "the user name, --secret which secret holds the token (the token's "
                                             "value: sandbox-ai secrets set NAME). For the starts from then on; a "
                                             "sandbox that runs is given the new login with git-logins give.")
    edited.add_argument("url", help="the login's server as the list shows it")
    edited.add_argument("--url", dest="new_url", metavar="NEW", help="the server's new address, http(s)://HOST[:PORT]")
    edited.add_argument("--user", metavar="USER", help="the user name sent with the token")
    edited.add_argument("--secret", metavar="NAME", help="the secret (sandbox-ai secrets) holding the access token")
    removed = commands.add_parser("remove", help="forget a login")
    removed.add_argument("url")
    checked = commands.add_parser("check", help="whether each login's secret is there")
    checked.add_argument("url", nargs="?")
    listing.add_argument("--run", metavar="RUN",
                         help="the logins the running sandbox RUN was given during the run (git-logins give)")
    given = commands.add_parser("give", help="give a running sandbox a Git login now, until it ends",
                                description="Give the running sandbox RUN (its run id or its start's name, as "
                                            "sandbox-ai list shows it) a Git login for URL now: its connection proxy "
                                            "logs in with it on the wire from now on (clone, fetch, push), until the "
                                            "run ends; no token enters the sandbox, and for a server the run already "
                                            "has a login for, the one given wins. The server is allowed as at a "
                                            "start. The older form is sandbox-ai net allow RUN --git-token-*.")
    given.add_argument("run")
    given.add_argument("url", help="http(s)://[USER@]HOST[:PORT]")
    source = given.add_mutually_exclusive_group(required=True)
    source.add_argument("--secret", metavar="NAME", help="the token is the secret NAME of sandbox-ai secrets")
    source.add_argument("--env", metavar="VARIABLE", help="the token is your variable VARIABLE")
    source.add_argument("--file", metavar="PATH", help="the token is in the file PATH (yours, mode 0600)")
    given.add_argument("--user", metavar="USER", help="the user Git logs in as (default: the URL's, else "
                                                      "x-access-token)")
    given.add_argument("--keep", action="store_true",
                       help="also for the next starts: into the project's trusted .sandbox-ai.json, as a reference")
    given.add_argument("--in-sandbox", action="store_true",
                       help="for a run whose connection proxy cannot log in (no proxy, --no-inspect for that server, "
                            "started by an older sandbox-ai): put the login into the sandbox's session "
                            "directory for its Git helper instead, where the agent can read the token. Without "
                            "this such a login is refused")
    taken = commands.add_parser("revoke", help="take back a Git login given during the run")
    taken.add_argument("run")
    taken.add_argument("url")
    args = p.parse_args(argv)
    if args.command in ("give", "revoke") or (args.command == "list" and args.run):
        return given_main(args, out)
    try:
        with settings_lock(path()) if args.command in ("add", "edit", "remove") else contextlib.nullcontext():
            return run_command(args, out)
    except ValueError as exc:  # the lock not taken in time
        print(f"sandbox-ai git-logins: {exc}", file=sys.stderr)
        return 1


def edit(args, logins, out):
    """git-logins edit URL [--url NEW] [--user USER] [--secret NAME] (under the list's lock): the login changed in
    place, then what the change does not reach - the sandboxes that run, and what else names an address that
    changed. ValueError: no such login, nothing to change, a value that is none, an address another login has."""
    wanted = server(args.url)["url"]
    entry = next((e for e in logins if server(e["url"])["url"] == wanted), None)
    if entry is None:
        raise ValueError(f"no login for {wanted} (sandbox-ai git-logins list)")
    if args.new_url is None and args.user is None and args.secret is None:
        raise ValueError("nothing to change: give --url NEW, --user USER or --secret NAME")
    if args.secret is not None and not SECRET.fullmatch(args.secret):
        raise ValueError("--secret is a secret's name (letters, digits, _)")
    if args.user is not None and not USER.fullmatch(args.user):
        raise ValueError("--user is letters, digits, ._-")
    before = dict(entry)
    if args.new_url is not None:
        fresh = server(args.new_url)["url"]
        if fresh != wanted and any(server(e["url"])["url"] == fresh for e in logins):
            raise ValueError(f"a login for {fresh} is there already: remove one of the two first (sandbox-ai "
                             f"git-logins remove URL)")
        entry["url"] = fresh
    if args.user is not None:
        entry["user"] = args.user
    if args.secret is not None:
        entry["secret"] = args.secret
    if entry == before:
        out(f"{wanted}: unchanged (it is that already)")
        return 0
    save(logins)
    out(f"{entry['url']}: login {entry['user']} with the secret {entry['secret']}"
        + (f" (was {before['url']})" if entry["url"] != before["url"] else "")
        + "; every sandbox's proxy started from now on uses it")
    if launcher().load_secret_store_module().get(entry["secret"]) is None:
        out(f"note: the secret {entry['secret']} is not kept yet: sandbox-ai secrets set {entry['secret']}")
    if entry["url"] != before["url"]:
        try:
            still = credrefs().url_references(before["url"])
        except Exception:  # noqa: BLE001 - a report after the change: never its failure
            still = []
        for text in still:
            out(f"note: still names {before['url']}: {launcher().printable(text)}")
    running_note(entry["url"], entry["secret"], entry["user"], out)
    return 0


def run_command(args, out):
    """One command of main (add and remove under the file's lock, from reading it to writing it back)."""
    try:
        logins = load()
        if args.command == "add":
            credential = server(args.url)
            if not SECRET.fullmatch(args.secret) or not USER.fullmatch(args.user):
                raise ValueError("--secret is a secret's name (letters, digits, _), --user letters, digits, ._-")
            logins = [e for e in logins if server(e["url"])["url"] != credential["url"]]
            logins.append({"url": credential["url"], "user": args.user, "secret": args.secret})
            save(logins)
            out(f"{credential['url']}: login {args.user} with the secret {args.secret}; every sandbox's proxy uses it")
            if launcher().load_secret_store_module().get(args.secret) is None:
                out(f"note: the secret {args.secret} is not kept yet: sandbox-ai secrets set {args.secret}")
            return 0
        if args.command == "edit":
            return edit(args, logins, out)
        if args.command == "list":
            if args.json:
                out(json.dumps(logins, indent=2))
            else:
                for entry in logins:
                    out(f"{entry['url']:<40} {entry['user']:<20} secret {entry['secret']}")
                if not logins:
                    out("no Git logins; add one with: sandbox-ai git-logins add URL --secret NAME")
            return 0
        if args.command == "remove":
            wanted = server(args.url)["url"]
            kept = [e for e in logins if server(e["url"])["url"] != wanted]
            if len(kept) == len(logins):
                raise ValueError(f"no login for {wanted}")
            save(kept)
            out(f"{wanted}: removed")
            return 0
        wanted = server(args.url)["url"] if args.url else None
        store, missing = launcher().load_secret_store_module(), 0
        for entry in logins:
            if wanted and server(entry["url"])["url"] != wanted:
                continue
            ok = store.get(entry["secret"]) is not None
            missing += not ok
            out(f"{entry['url']}: {'ok' if ok else 'the secret ' + entry['secret'] + ' is missing'}")
        return 1 if missing else 0
    except ValueError as exc:
        print(f"sandbox-ai git-logins: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
