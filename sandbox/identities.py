#!/usr/bin/env python3
"""sandbox-ai identities: who a sandbox works as - a commit name and e-mail address, Git logins and publish logins,
under one name, chosen per start with --identity NAME (also in a preset or a project's .sandbox-ai.json).

  sandbox-ai identities add NAME [--name NAME] [--email ADDRESS] [--description TEXT]
  sandbox-ai identities set NAME [--name NAME] [--email ADDRESS] [--description TEXT]
  sandbox-ai identities login NAME URL --secret SECRET [--user USER]     a Git server's login for this identity
  sandbox-ai identities logout NAME URL
  sandbox-ai identities publish NAME KIND --secret SECRET [--url URL]    npm, pypi, testpypi, crates, gitlab, github
  sandbox-ai identities unpublish NAME KIND
  sandbox-ai identities list [--json] | show NAME [--json] | check [NAME] | remove NAME

Without --identity a start works as "default": the logins of sandbox-ai git-logins and sandbox-ai publish-logins,
and with --git-identity your host's git user.name and user.email. With --identity NAME it works as NAME only:
NAME's commit name and address, NAME's Git logins (none of default's) and, with --publish, NAME's publish logins.
Several accounts on one server (two GitHub users) are two identities. The tokens stay in sandbox-ai secrets and
reach only the connection proxy, never the sandbox; the list holds names, addresses, servers and secret names.

The list is ~/.config/sandbox-ai/identities.json (mode 0600). Standard library only.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import sys

HERE = Path(__file__).resolve().parent
NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
SECRET = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
USER = re.compile(r"[A-Za-z0-9._-]{1,64}")
EMAIL = re.compile(r"[^\s<>@,;\"']{1,64}@[A-Za-z0-9.-]{1,253}")
PERSON = re.compile(r"[^\x00-\x1f\x7f<>\n]{1,100}")
DEFAULT = "default"
_modules = {}


def module(name):
    """A sibling module of sandbox-ai's (agent_sandbox, git_logins, publish_logins), loaded once."""
    if name not in _modules:
        spec = importlib.util.spec_from_file_location(f"sandbox_ai_identities_{name}", HERE / f"{name}.py")
        _modules[name] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_modules[name])
    return _modules[name]


def path():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "sandbox-ai" / "identities.json"


def problems(name, identity):
    """Why IDENTITY (of NAME) is not usable ([] when it is)."""
    found = []
    if not isinstance(identity, dict) or set(identity) - {"name", "email", "description", "git", "publish"}:
        return [f"identity {name}: name, email, description, git and publish only"]
    if identity.get("name") is not None and not (isinstance(identity["name"], str) and PERSON.fullmatch(identity["name"])):
        found.append(f"identity {name}: name is a person's name, one line")
    if identity.get("email") is not None and not (isinstance(identity["email"], str) and EMAIL.fullmatch(identity["email"])):
        found.append(f"identity {name}: email is an address")
    if not isinstance(identity.get("description", ""), str):
        found.append(f"identity {name}: description is a text")
    for login in identity.get("git", []) if isinstance(identity.get("git", []), list) else [None]:
        if not (isinstance(login, dict) and set(login) == {"url", "user", "secret"} and isinstance(login["url"], str)
                and isinstance(login["user"], str) and USER.fullmatch(login["user"])
                and isinstance(login["secret"], str) and SECRET.fullmatch(login["secret"])):
            found.append(f"identity {name}: each Git login is {{url, user, secret}}")
    kinds = module("publish_logins").KINDS
    for login in identity.get("publish", []) if isinstance(identity.get("publish", []), list) else [None]:
        if not (isinstance(login, dict) and set(login) == {"kind", "host", "secret"} and login["kind"] in kinds
                and isinstance(login["host"], str) and module("publish_logins").HOST.fullmatch(login["host"])
                and isinstance(login["secret"], str) and SECRET.fullmatch(login["secret"])):
            found.append(f"identity {name}: each publish login is {{kind, host, secret}}")
    return found


def load():
    """{NAME: identity} of the list ({} without one); ValueError for a damaged or unsafe file."""
    file = path()
    try:
        info = os.lstat(file)
    except FileNotFoundError:
        return {}
    if not os.path.isfile(file) or os.path.islink(file) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError(f"{file} must be a regular file of yours with mode 0600")
    try:
        data = json.loads(file.read_text())
    except (OSError, ValueError) as exc:
        raise ValueError(f"{file}: {exc}") from None
    identities = data.get("identities") if isinstance(data, dict) and data.get("version") == 1 else None
    if not isinstance(identities, dict):
        raise ValueError(f"{file}: version 1 with an object of identities")
    for name, identity in identities.items():
        if not NAME.fullmatch(name) or name == DEFAULT:
            raise ValueError(f"{file}: {name!r} is not an identity's name")
        found = problems(name, identity)
        if found:
            raise ValueError(f"{file}: {'; '.join(found)}")
    return identities


def save(identities):
    file = path()
    file.parent.mkdir(parents=True, exist_ok=True)
    temporary = file.with_name(f".{file.name}.{os.getpid()}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump({"version": 1, "identities": identities}, stream, indent=2)
    os.replace(temporary, file)


def get(name):
    """The identity NAME ({"name", "email", "git", "publish", ...}); ValueError naming the ones there are."""
    identities = load()
    if name not in identities:
        known = ", ".join(sorted(identities)) or "none yet"
        raise ValueError(f"--identity {name}: no such identity (yours: {known}); add one with: sandbox-ai "
                         f"identities add {name}")
    return identities[name]


def git_url(url):
    """The canonical origin of a Git server URL, checked as a --git-token-* server is."""
    return module("agent_sandbox").git_server(url, "identities")["url"]


def describe(name, identity, out):
    who = " ".join(part for part in (identity.get("name"), f"<{identity['email']}>" if identity.get("email") else None)
                   if part) or "(no commit name)"
    out(f"{name}: {who}" + (f" - {identity['description']}" if identity.get("description") else ""))
    for login in identity.get("git", []):
        out(f"  git      {login['url']:<36} user {login['user']:<16} secret {login['secret']}")
    for login in identity.get("publish", []):
        out(f"  publish  {login['kind']:<9} {login['host']:<26} secret {login['secret']}")


def main(argv, out=print):
    p = argparse.ArgumentParser(prog="sandbox-ai identities", description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter, allow_abbrev=False,
                                epilog="\n\n".join(__doc__.split("\n\n")[1:3]))
    commands = p.add_subparsers(dest="command", required=True)
    for command in ("add", "set"):
        sub = commands.add_parser(command)
        sub.add_argument("identity")
        sub.add_argument("--name")
        sub.add_argument("--email")
        sub.add_argument("--description")
    login = commands.add_parser("login")
    login.add_argument("identity")
    login.add_argument("url")
    login.add_argument("--secret", required=True)
    login.add_argument("--user", default="x-access-token")
    logout = commands.add_parser("logout")
    logout.add_argument("identity")
    logout.add_argument("url")
    publish = commands.add_parser("publish")
    publish.add_argument("identity")
    publish.add_argument("kind", choices=sorted(module("publish_logins").KINDS))
    publish.add_argument("--secret", required=True)
    publish.add_argument("--url")
    unpublish = commands.add_parser("unpublish")
    unpublish.add_argument("identity")
    unpublish.add_argument("kind", choices=sorted(module("publish_logins").KINDS))
    listing = commands.add_parser("list")
    listing.add_argument("--json", action="store_true")
    shown = commands.add_parser("show")
    shown.add_argument("identity")
    shown.add_argument("--json", action="store_true")
    checked = commands.add_parser("check")
    checked.add_argument("identity", nargs="?")
    removed = commands.add_parser("remove")
    removed.add_argument("identity")
    args = p.parse_args(argv)
    try:
        identities = load()
        name = getattr(args, "identity", None)
        if name is not None and (not NAME.fullmatch(name) or name == DEFAULT):
            raise ValueError(f"{name!r}: an identity's name is lower-case letters, digits, - and _ (not "
                             f"{DEFAULT!r}, which is sandbox-ai git-logins and publish-logins)")
        if args.command in ("add", "set"):
            if args.command == "add" and name in identities:
                raise ValueError(f"identity {name} exists; change it with: sandbox-ai identities set {name}")
            identity = dict(identities.get(name) or {"git": [], "publish": []}) if args.command == "set" \
                else {"git": [], "publish": []}
            if args.command == "set" and name not in identities:
                get(name)
            for key in ("name", "email", "description"):
                value = getattr(args, key)
                if value is not None:
                    identity[key] = value or None
                    if not value:
                        identity.pop(key)
            found = problems(name, identity)
            if found:
                raise ValueError("; ".join(found))
            identities[name] = identity
            save(identities)
            describe(name, identity, out)
            return 0
        if args.command in ("login", "logout", "publish", "unpublish", "show", "remove") and name not in identities:
            get(name)
        if args.command == "login":
            url = git_url(args.url)
            if not SECRET.fullmatch(args.secret) or not USER.fullmatch(args.user):
                raise ValueError("--secret is a secret's name (letters, digits, _), --user letters, digits, ._-")
            identity = identities[name]
            identity["git"] = [e for e in identity.get("git", []) if e["url"] != url] + [
                {"url": url, "user": args.user, "secret": args.secret}]
        elif args.command == "logout":
            url = git_url(args.url)
            before = identities[name].get("git", [])
            identities[name]["git"] = [e for e in before if e["url"] != url]
            if len(before) == len(identities[name]["git"]):
                raise ValueError(f"identity {name} has no login for {url}")
        elif args.command == "publish":
            host = module("publish_logins").server_host(args.kind, args.url)
            if not SECRET.fullmatch(args.secret):
                raise ValueError("--secret is a secret's name (letters, digits, _)")
            identity = identities[name]
            identity["publish"] = [e for e in identity.get("publish", []) if e["kind"] != args.kind] + [
                {"kind": args.kind, "host": host, "secret": args.secret}]
        elif args.command == "unpublish":
            before = identities[name].get("publish", [])
            identities[name]["publish"] = [e for e in before if e["kind"] != args.kind]
            if len(before) == len(identities[name]["publish"]):
                raise ValueError(f"identity {name} has no publish login for {args.kind}")
        elif args.command == "remove":
            identities.pop(name)
            save(identities)
            out(f"{name}: removed (its secrets stay in sandbox-ai secrets)")
            return 0
        elif args.command == "list":
            if args.json:
                out(json.dumps(identities, indent=2))
            else:
                for each, identity in sorted(identities.items()):
                    describe(each, identity, out)
                if not identities:
                    out("no identities; add one with: sandbox-ai identities add NAME --name 'Your Name' --email ADDRESS")
            return 0
        elif args.command == "show":
            if args.json:
                out(json.dumps(identities[name], indent=2))
            else:
                describe(name, identities[name], out)
            return 0
        elif args.command == "check":
            store, missing = module("agent_sandbox").load_secret_store_module(), 0
            for each, identity in sorted(identities.items()):
                if name and each != name:
                    continue
                for login in identity.get("git", []) + identity.get("publish", []):
                    ok = store.get(login["secret"]) is not None
                    missing += not ok
                    target = login.get("url") or login.get("kind")
                    out(f"{each}: {target}: {'ok' if ok else 'the secret ' + login['secret'] + ' is missing'}")
            return 1 if missing else 0
        save(identities)
        describe(name, identities[name], out)
        return 0
    except ValueError as exc:
        print(f"sandbox-ai identities: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
