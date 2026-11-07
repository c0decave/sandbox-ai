#!/usr/bin/env python3
"""sandbox-ai publish-logins: your package registries' and forges' logins, used by the connection proxy, never
inside the sandbox.

  sandbox-ai publish-logins add KIND --secret NAME [--url URL]   a login: the token is the secret NAME
  sandbox-ai publish-logins list [--json]                         the logins (kinds, hosts, secret names)
  sandbox-ai publish-logins remove KIND
  sandbox-ai publish-logins check [KIND]                          whether each login's secret is there
  sandbox-ai publish-logins kinds                                 what each kind opens, sets and asks about

KIND is npm, pypi, testpypi, crates, gitlab or github. --url names another server of that kind (a private npm
registry, your GitLab, GitHub Enterprise). Keep the token first: sandbox-ai secrets set NAME.

A start uses them only when it says so: sandbox-ai AGENT --publish pypi,npm. Then the sandbox can reach that
kind's upload and API hosts, the agent's tools get their usual variables (TWINE_PASSWORD, UV_PUBLISH_TOKEN,
CARGO_REGISTRY_TOKEN, NPM_TOKEN with an npmrc, GITLAB_TOKEN, GH_TOKEN) holding only a placeholder, and the proxy
puts the token in its place on the wire, only in a credential header of HTTPS requests to that kind's hosts
(PyPI's Basic login too). Every request that publishes, changes a release or takes something back (an upload, an
npm publish or unpublish, a crate yank, a GitHub or GitLab release, a file written through the API, which a push's
privacy check would not see) is asked first; with --ask off it is refused. Answers of those hosts reach the agent
with the token masked.

The list is ~/.config/sandbox-ai/publish-logins.json (mode 0600): kinds, hosts and secret names, no tokens.
Git pushes are sandbox-ai git-logins. Standard library only.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import urllib.parse

HERE = Path(__file__).resolve().parent
SECRET = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
HOST = re.compile(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,62}")
WRITES = ["POST", "PUT", "PATCH", "DELETE"]


def placeholder(kind):
    # pypi and testpypi share one: TWINE_PASSWORD then works for either, each host gets its own token.
    return f"sandbox-ai-publish-placeholder-{'pypi' if kind in ('pypi', 'testpypi') else kind}"


# What each kind is: its hosts (the first is the one --url replaces), how the token goes on the wire
# ("header": in place of the placeholder in a credential header; "basic": inside a Basic login), the agent's
# variables (VALUE "{placeholder}" or a fixed text) and the requests that are asked.
KINDS = {
    "npm": {"title": "npm registry (npm publish, unpublish, dist-tag)", "hosts": ["registry.npmjs.org"],
            "wire": "header", "env": {"NPM_TOKEN": "{placeholder}"}, "npmrc": True,
            "ask": [{"methods": ["PUT", "DELETE", "PATCH"], "path": "/**", "reason": "publishes, changes or "
                     "unpublishes a package on {host}"}]},
    "pypi": {"title": "PyPI uploads (twine, uv publish, poetry, hatch, flit)", "hosts": ["upload.pypi.org"],
             "wire": "basic",
             "env": {"TWINE_USERNAME": "__token__", "TWINE_PASSWORD": "{placeholder}", "UV_PUBLISH_TOKEN": "{placeholder}",
                     "POETRY_PYPI_TOKEN_PYPI": "{placeholder}", "HATCH_INDEX_USER": "__token__",
                     "HATCH_INDEX_AUTH": "{placeholder}", "FLIT_USERNAME": "__token__", "FLIT_PASSWORD": "{placeholder}"},
             "ask": [{"methods": ["POST"], "path": "/legacy/**", "reason": "uploads a release to {host}"}]},
    "testpypi": {"title": "TestPyPI uploads", "hosts": ["test.pypi.org"], "wire": "basic",
                 "env": {"TWINE_USERNAME": "__token__", "TWINE_PASSWORD": "{placeholder}",
                         "UV_PUBLISH_TOKEN": "{placeholder}", "POETRY_PYPI_TOKEN_TESTPYPI": "{placeholder}"},
                 "ask": [{"methods": ["POST"], "path": "/legacy/**", "reason": "uploads a release to {host}"}]},
    "crates": {"title": "crates.io (cargo publish, yank, owner)", "hosts": ["crates.io"], "wire": "header",
               "env": {"CARGO_REGISTRY_TOKEN": "{placeholder}"},
               "ask": [{"methods": WRITES, "path": "/api/v1/crates/**", "reason": "publishes, yanks or changes "
                        "the owners of a crate on {host}"}]},
    "gitlab": {"title": "GitLab API (glab, packages, releases)", "hosts": ["gitlab.com"], "wire": "header",
               "env": {"GITLAB_TOKEN": "{placeholder}"},
               "ask": [{"methods": WRITES, "path": ["/api/v4/projects/*/packages/**", "/api/v4/projects/*/releases/**",
                                                    "/api/v4/projects/*/repository/files/**",
                                                    "/api/v4/projects/*/repository/commits/**",
                                                    "/api/v4/projects/*/uploads/**", "/api/v4/snippets/**",
                                                    "/api/v4/projects/*/snippets/**"],
                        "reason": "publishes on {host} (a package, a release, a file or commit the push's privacy "
                                  "check does not see, a snippet)"}]},
    "github": {"title": "GitHub API (gh, releases, packages)", "hosts": ["api.github.com", "uploads.github.com"],
               "wire": "header", "env": {"GH_TOKEN": "{placeholder}"},
               "ask": [{"methods": WRITES, "path": ["/repos/*/*/releases/**", "/repos/*/*/contents/**",
                                                    "/repos/*/*/git/**", "/gists/**", "/user/packages/**",
                                                    "/orgs/*/packages/**"],
                        "reason": "publishes on {host} (a release, a file or commit the push's privacy check does not "
                                  "see, a gist, a package)"},
                       {"host": 1, "methods": WRITES, "path": "/**", "reason": "uploads a release asset to {host}"}]},
}
_launcher = None


def launcher():
    global _launcher
    if _launcher is None:
        spec = importlib.util.spec_from_file_location("agent_sandbox_for_publish_logins", HERE / "agent_sandbox.py")
        _launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_launcher)
    return _launcher


def path():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "sandbox-ai" / "publish-logins.json"


def server_host(kind, url):
    """The host of KIND's server URL (https only, no path beyond /, no port): ValueError otherwise."""
    if kind not in KINDS:
        raise ValueError(f"no kind {kind!r}; kinds: {', '.join(KINDS)}")
    if url is None:
        return KINDS[kind]["hosts"][0]
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").rstrip(".").lower()
    try:
        port = parts.port
    except ValueError:
        port = -1
    if parts.scheme != "https" or not HOST.fullmatch(host) or port not in (None, 443) or parts.username \
            or parts.password or parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError(f"--url is https://HOST (a host name, port 443, no path): {url!r}")
    return host


def load():
    """[{"kind", "host", "secret"}] of the list ([] without one); ValueError for a damaged or unsafe file."""
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
        if not (isinstance(entry, dict) and entry.get("kind") in KINDS and isinstance(entry.get("host"), str)
                and HOST.fullmatch(entry["host"]) and isinstance(entry.get("secret"), str)
                and SECRET.fullmatch(entry["secret"])):
            raise ValueError(f"{file}: each login is {{kind, host, secret}}")
    return logins


def save(logins):
    file = path()
    file.parent.mkdir(parents=True, exist_ok=True)
    temporary = file.with_name(f".{file.name}.{os.getpid()}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump({"version": 1, "logins": logins}, stream, indent=2)
    os.replace(temporary, file)


def hosts_of(kind, host):
    """The hosts a KIND login with its first host HOST covers (GitHub Enterprise has no uploads host of its own)."""
    defaults = KINDS[kind]["hosts"]
    if host == defaults[0]:
        return list(defaults)
    return [host]


def kinds_value(values):
    """The kinds of --publish KIND[,KIND] (repeatable), each once, in order; ValueError for an unknown one."""
    kinds = []
    for value in values or []:
        for part in value.split(","):
            part = part.strip().lower()
            if not part:
                continue
            if part not in KINDS:
                raise ValueError(f"--publish: no kind {part!r}; kinds: {', '.join(KINDS)}")
            if part not in kinds:
                kinds.append(part)
    return kinds


def selected(kinds, logins=None):
    """[(login, KINDS entry)] for KINDS (from --publish); ValueError naming a kind without a login."""
    logins = load() if logins is None else logins
    by_kind = {entry["kind"]: entry for entry in logins}
    found = []
    for kind in kinds:
        if kind not in by_kind:
            raise ValueError(f"--publish {kind}: no login; add one with: sandbox-ai publish-logins add {kind} "
                             f"--secret NAME (after sandbox-ai secrets set NAME)")
        found.append((by_kind[kind], KINDS[kind]))
    return found


def environment(kinds, logins=None):
    """{variable: value} the agent gets for KINDS: placeholders and fixed user names, never a token."""
    env = {}
    for login, spec in selected(kinds, logins):
        for name, value in spec["env"].items():
            env[name] = value.format(placeholder=placeholder(login["kind"]))
    return env


def npmrc(kinds, logins=None):
    """The npm configuration for --publish npm (its registry's token from $NPM_TOKEN), or None."""
    for login, spec in selected(kinds, logins):
        if spec.get("npmrc"):
            return f"//{login['host']}/:_authToken=${{NPM_TOKEN}}\n"
    return None


def proxy_entries(kinds, get, logins=None):
    """proxy.json's "api_keys" entries for KINDS: per host the placeholder and the token (GET reads the secret
    store); ValueError for a secret that is not kept or not usable in a header."""
    entries = []
    for login, spec in selected(kinds, logins):
        token = get(login["secret"])
        if token is None:
            raise ValueError(f"--publish {login['kind']}: no secret {login['secret']}; keep one with: "
                             f"sandbox-ai secrets set {login['secret']}")
        if not token or any(ord(c) < 33 or ord(c) == 127 for c in token):
            raise ValueError(f"--publish {login['kind']}: the secret {login['secret']} is not usable in a header")
        for host in hosts_of(login["kind"], login["host"]):
            entries.append({"host": host, "port": 443, "name": f"publish:{login['kind']}",
                            "placeholder": placeholder(login["kind"]), "key": token,
                            **({"basic": True} if spec["wire"] == "basic" else {})})
    return entries


def rules(kinds, logins=None):
    """The ruleset "publish" for KINDS: every request that publishes is asked (None without kinds)."""
    http = []
    for login, spec in selected(kinds, logins):
        hosts = hosts_of(login["kind"], login["host"])
        for index, rule in enumerate(spec["ask"], 1):
            if isinstance(rule.get("host"), int):
                if rule["host"] >= len(hosts):
                    continue  # GitHub Enterprise: no uploads host of its own
                host = hosts[rule["host"]]
            else:
                host = hosts[0]
            http.append({"id": f"publish-{login['kind']}-{index}", "host": host, "methods": rule["methods"],
                         "path": rule["path"], "decision": "ask", "reason": rule["reason"].format(host=host)})
    return {"version": 1, "description": "--publish: every request that publishes is asked", "http": http} if http \
        else None


def plan(kinds, logins=None):
    """What --publish does, for the dry run: kinds, hosts, variables and secret names, never a token."""
    return [{"kind": login["kind"], "hosts": hosts_of(login["kind"], login["host"]), "secret": login["secret"],
             "variables": sorted(spec["env"])} for login, spec in selected(kinds, logins)]


def main(argv, out=print):
    p = argparse.ArgumentParser(prog="sandbox-ai publish-logins", description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter, allow_abbrev=False,
                                epilog="\n\n".join(__doc__.split("\n\n")[1:4]))
    commands = p.add_subparsers(dest="command", required=True)
    added = commands.add_parser("add")
    added.add_argument("kind", choices=sorted(KINDS))
    added.add_argument("--secret", required=True, metavar="NAME")
    added.add_argument("--url", metavar="URL")
    listing = commands.add_parser("list")
    listing.add_argument("--json", action="store_true")
    removed = commands.add_parser("remove")
    removed.add_argument("kind", choices=sorted(KINDS))
    checked = commands.add_parser("check")
    checked.add_argument("kind", nargs="?", choices=sorted(KINDS))
    commands.add_parser("kinds")
    args = p.parse_args(argv)
    try:
        if args.command == "kinds":
            for kind, spec in KINDS.items():
                out(f"{kind:<9} {spec['title']}: {', '.join(spec['hosts'])}; variables {', '.join(sorted(spec['env']))}")
            return 0
        logins = load()
        if args.command == "add":
            host = server_host(args.kind, args.url)
            if not SECRET.fullmatch(args.secret):
                raise ValueError("--secret is a secret's name (letters, digits, _)")
            logins = [e for e in logins if e["kind"] != args.kind] + [
                {"kind": args.kind, "host": host, "secret": args.secret}]
            save(logins)
            out(f"{args.kind}: {', '.join(hosts_of(args.kind, host))} with the secret {args.secret}; "
                f"a start uses it with --publish {args.kind}")
            if launcher().load_secret_store_module().get(args.secret) is None:
                out(f"note: the secret {args.secret} is not kept yet: sandbox-ai secrets set {args.secret}")
            return 0
        if args.command == "list":
            if args.json:
                out(json.dumps(logins, indent=2))
            else:
                for entry in logins:
                    out(f"{entry['kind']:<9} {entry['host']:<32} secret {entry['secret']}")
                if not logins:
                    out("no publish logins; add one with: sandbox-ai publish-logins add KIND --secret NAME")
            return 0
        if args.command == "remove":
            kept = [e for e in logins if e["kind"] != args.kind]
            if len(kept) == len(logins):
                raise ValueError(f"no login for {args.kind}")
            save(kept)
            out(f"{args.kind}: removed")
            return 0
        store, missing = launcher().load_secret_store_module(), 0
        for entry in logins:
            if args.kind and entry["kind"] != args.kind:
                continue
            ok = store.get(entry["secret"]) is not None
            missing += not ok
            out(f"{entry['kind']}: {'ok' if ok else 'the secret ' + entry['secret'] + ' is missing'}")
        return 1 if missing else 0
    except ValueError as exc:
        print(f"sandbox-ai publish-logins: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
