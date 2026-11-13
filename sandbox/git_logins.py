#!/usr/bin/env python3
"""sandbox-ai git-logins: your Git servers' logins, used by the connection proxy of every sandbox, never inside it.

  sandbox-ai git-logins add URL --secret NAME [--user USER]   a login: the token is the secret NAME
  sandbox-ai git-logins list [--json]                         the logins (servers, users, secret names)
  sandbox-ai git-logins remove URL
  sandbox-ai git-logins check [URL]                           whether each login's secret is there
  sandbox-ai git-logins give RUN URL (--secret NAME | --env VARIABLE | --file PATH) [--user USER] [--keep]
                                                              a running sandbox gets a Git login now, until it ends
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
it): its Git logs in with it from now on, until the run ends; --keep also writes it into the project's trusted
.sandbox-ai.json, as a reference (never the token). It is the same step as the older form `sandbox-ai net allow RUN
--git-token-secret URL=NAME` (--git-token-env, --git-token-file), which keeps working; the server is allowed as at a
start (github.com: its preset). `sandbox-ai net show RUN` lists them too.

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
        argv = ["allow", args.run, f"--git-token-{kind}", f"{url}={ref}", *(["--keep"] if args.keep else [])]
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
    temporary = file.with_name(f".{file.name}.{os.getpid()}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump({"version": 1, "logins": logins}, stream, indent=2)
    os.replace(temporary, file)


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
        if token is None or not re.fullmatch(launcher().GIT_TOKEN, token):
            notes.append(f"{origin}: {credential['url']} left out: no usable secret {entry['secret']} "
                         f"(sandbox-ai secrets set {entry['secret']})")
            continue
        pairs.append(({**credential, "user": entry["user"]}, token))
    return pairs, notes


def main(argv, out=print):
    p = argparse.ArgumentParser(prog="sandbox-ai git-logins", description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter, allow_abbrev=False,
                                epilog="\n\n".join(__doc__.split("\n\n")[1:5]))
    commands = p.add_subparsers(dest="command", required=True)
    added = commands.add_parser("add")
    added.add_argument("url")
    added.add_argument("--secret", required=True, metavar="NAME")
    added.add_argument("--user", default="x-access-token")
    listing = commands.add_parser("list")
    listing.add_argument("--json", action="store_true")
    removed = commands.add_parser("remove")
    removed.add_argument("url")
    checked = commands.add_parser("check")
    checked.add_argument("url", nargs="?")
    listing.add_argument("--run", metavar="RUN",
                         help="the logins the running sandbox RUN was given during the run (git-logins give)")
    given = commands.add_parser("give", help="give a running sandbox a Git login now, until it ends",
                                description="Give the running sandbox RUN (its run id or its start's name, as "
                                            "sandbox-ai list shows it) a Git login for URL now: its Git logs in "
                                            "with it from now on, until the run ends; the server is allowed as at a "
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
    taken = commands.add_parser("revoke", help="take back a Git login given during the run")
    taken.add_argument("run")
    taken.add_argument("url")
    args = p.parse_args(argv)
    if args.command in ("give", "revoke") or (args.command == "list" and args.run):
        return given_main(args, out)
    try:
        with settings_lock(path()) if args.command in ("add", "remove") else contextlib.nullcontext():
            return run_command(args, out)
    except ValueError as exc:  # the lock not taken in time
        print(f"sandbox-ai git-logins: {exc}", file=sys.stderr)
        return 1


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
