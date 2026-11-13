#!/usr/bin/env python3
"""sandbox-ai secrets: API keys and tokens for sandboxes, kept in your system keyring.

  sandbox-ai secrets list                  names (never values) and where they are kept
  sandbox-ai secrets set NAME              read the value from stdin (or --from-env VARIABLE)
  sandbox-ai secrets delete NAME
  sandbox-ai secrets backend               keyring, keychain (macOS) or file
  sandbox-ai secrets give RUN NAME --api-key HOST [--as VARIABLE] [--keep]
                                           a running sandbox gets the secret NAME as an API key for HOST, now
  sandbox-ai secrets revoke RUN NAME       take back a key given during the run (NAME: the secret or its variable)
  sandbox-ai secrets list --run RUN [--json]   the API keys a running sandbox was given during the run

Secrets go to the Secret Service (GNOME Keyring, KWallet, KeePassXC; through secret-tool) when it is there, on macOS
to the login Keychain (through security), else to private files (0600) in ~/.local/share/sandbox-ai/secrets.
Sandboxes get them only when named: --secret-env NAME (an environment variable for the agent), --git-token-secret
URL=NAME, or an MCP server's variable (sandbox-ai mcp add ... --env NAME). Values are never printed.

A sandbox that runs already gets a secret as an API key with `give` (RUN: its run id or its start's name, as
`sandbox-ai list` shows it): its connection proxy puts the key in on the wire for HTTPS requests to HOST - in a
credential header, also as the password of a Basic login (git over HTTPS) - where the agent sends the placeholder
that `export VARIABLE=...` (said by give) stands for; the agent never sees the key, and HOST is allowed too. It is
the same step as the older form `sandbox-ai net allow RUN --api-key HOST=VARIABLE=NAME`, which keeps working. For
`git push` to a Git server, a Git login is meant: sandbox-ai git-logins give RUN URL --secret NAME.

Standard library only.
"""
import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
LIMIT = 8192


def check_name(name):
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ValueError(f"a secret's name is letters, digits and _ (up to 64), not starting with a digit: {name!r}")
    return name


def files_dir():
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "sandbox-ai" / "secrets"


class FileStore:
    """Private files: one per secret, mode 0600, in a 0700 directory."""
    name = "file"

    def __init__(self, directory=None):
        self.directory = Path(directory) if directory else files_dir()

    def names(self):
        try:
            return sorted(p.name for p in self.directory.iterdir() if NAME.fullmatch(p.name) and p.is_file()
                          and not p.is_symlink())
        except OSError:
            return []

    def get(self, name):
        path = self.directory / check_name(name)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            return None
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError(f"{path} must be yours and private (chmod 600)")
            return stream.read(LIMIT + 1).decode("utf-8", "replace").rstrip("\n")

    def set(self, name, value):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        path = self.directory / check_name(name)
        temporary = self.directory / f".{name}.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(value)
        os.replace(temporary, path)

    def delete(self, name):
        try:
            (self.directory / check_name(name)).unlink()
            return True
        except FileNotFoundError:
            return False


class KeyringStore:
    """The Secret Service through secret-tool; entries carry service=sandbox-ai and name=NAME."""
    name = "keyring"

    def __init__(self, run=subprocess.run):
        self.run = run

    def tool(self, *args, data=None):
        return self.run(["secret-tool", *args], input=data, capture_output=True, text=True, timeout=30,
                        stdin=None if data is not None else subprocess.DEVNULL)

    def names(self):
        result = self.tool("search", "--all", "service", "sandbox-ai")
        found = set()
        for line in (result.stdout + result.stderr).splitlines():
            match = re.match(r"\s*attribute\.name\s*=\s*(\S+)", line)
            if match and NAME.fullmatch(match[1]):
                found.add(match[1])
        return sorted(found)

    def get(self, name):
        result = self.tool("lookup", "service", "sandbox-ai", "name", check_name(name))
        return result.stdout.rstrip("\n") if result.returncode == 0 and result.stdout else None

    def set(self, name, value):
        result = self.tool("store", f"--label=sandbox-ai {check_name(name)}", "service", "sandbox-ai", "name", name,
                           data=value)
        if result.returncode != 0:
            raise ValueError(f"the keyring refused the secret: {result.stderr.strip()[:200]} (keep it in sandbox-ai's "
                             f"private files instead: SANDBOX_AI_SECRETS=file sandbox-ai secrets set {name})")

    def delete(self, name):
        return self.tool("clear", "service", "sandbox-ai", "name", check_name(name)).returncode == 0


class KeychainStore:
    """macOS: the login Keychain through security(1); generic passwords of service sandbox-ai, account NAME. The
    value never goes on a command line (other users' `ps` would show it): `security -i` reads the command that adds
    it from stdin, the value in it as hex (-X)."""
    name = "keychain"
    SERVICE = "sandbox-ai"

    def __init__(self, run=subprocess.run):
        self.run = run

    def tool(self, *args, data=None):
        return self.run(["/usr/bin/security", *args], input=data, capture_output=True, text=True, timeout=30,
                        stdin=None if data is not None else subprocess.DEVNULL)

    def names(self):
        """The accounts of this service, from `security dump-keychain` (attributes only, never the values)."""
        result = self.tool("dump-keychain")
        found = set()
        for item in result.stdout.split("keychain: ")[1:]:
            account = re.search(r'"acct"<blob>="([^"]*)"', item)
            service = re.search(r'"svce"<blob>="([^"]*)"', item)
            if account and service and service[1] == self.SERVICE and NAME.fullmatch(account[1]):
                found.add(account[1])
        return sorted(found)

    def get(self, name):
        result = self.tool("find-generic-password", "-s", self.SERVICE, "-a", check_name(name), "-w")
        return result.stdout.rstrip("\n") if result.returncode == 0 and result.stdout else None

    def set(self, name, value):
        command = f"add-generic-password -U -s {self.SERVICE} -a {check_name(name)} -X {value.encode().hex()}\n"
        result = self.tool("-i", data=command)
        if result.returncode != 0 or "error" in result.stderr.lower() or self.get(name) != value:
            raise ValueError(f"the Keychain refused the secret: {result.stderr.strip()[:200]} (keep it in "
                             f"sandbox-ai's private files instead: SANDBOX_AI_SECRETS=file sandbox-ai secrets set "
                             f"{name})")

    def delete(self, name):
        return self.tool("delete-generic-password", "-s", self.SERVICE, "-a", check_name(name)).returncode == 0


def keychain_usable():
    """macOS with its security tool."""
    return sys.platform == "darwin" and os.path.exists("/usr/bin/security")


def keyring_usable(run=subprocess.run):
    """True if secret-tool is there and a Secret Service answers on the session bus."""
    if not shutil.which("secret-tool") or not os.environ.get("DBUS_SESSION_BUS_ADDRESS") and not \
            Path(f"/run/user/{os.getuid()}/bus").exists():
        return False
    try:
        result = run(["secret-tool", "search", "service", "sandbox-ai-probe"], capture_output=True, text=True,
                     timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return False
    # A search that finds nothing ends with 1 and says nothing; any message on standard error is the Secret
    # Service failing ("not provided by any .service files", "The name is not activatable", "Cannot autolaunch
    # D-Bus", a locked or missing collection): then the private files are used.
    return result.returncode == 0 or (result.returncode == 1 and not result.stderr.strip())


def store():
    """The keyring (macOS: the Keychain) if it works here, else the private files (SANDBOX_AI_SECRETS=file forces
    the files)."""
    if os.environ.get("SANDBOX_AI_SECRETS") != "file" and keychain_usable():
        return KeychainStore()
    if os.environ.get("SANDBOX_AI_SECRETS") != "file" and keyring_usable():
        return KeyringStore()
    return FileStore()


def get(name):
    """A secret's value from the keyring or the files (both are looked at), or None."""
    check_name(name)
    for backend in ([store()] if isinstance(store(), FileStore) else [store(), FileStore()]):
        value = backend.get(name)
        if value is not None:
            return value
    return None


_net = None


def net_module():
    """net.py, which gives a running sandbox a key (`give` and `revoke` are its `net allow/revoke --api-key`: one
    code path). Loaded only for those: the other commands stay standard library alone."""
    global _net
    if _net is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("sandbox_ai_net_for_secrets", Path(__file__).resolve().parent
                                                      / "net.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        _net = loaded
    return _net


def given_main(args, out):
    """give, revoke and list --run: net.py's allow, revoke and given credentials of the run."""
    net = net_module()
    if args.command == "list":
        return net.list_given(args.run, "api_keys", out, as_json=args.json, prog="secrets")
    try:
        check_name(args.name)
        if args.command == "give":
            variable = args.variable or args.name
            check_name(variable)
            argv = ["allow", args.run, "--api-key", f"{args.host}={variable}={args.name}",
                    *(["--keep"] if args.keep else [])]
        else:
            # NAME: the variable a key was given as, or the secret it came from (each key given of that secret)
            record = net.choose(net.running(), args.run, command="secrets revoke")
            given = net.given_credentials(record)["api_keys"]
            names = [e["name"] for e in given if e["name"] == args.name] or \
                [e["name"] for e in given if e.get("secret") == args.name]
            if not names:
                raise ValueError(f"no API key {args.name} was given during this run (sandbox-ai secrets list --run "
                                 f"{args.run})")
            argv = ["revoke", args.run, *[token for name in names for token in ("--api-key", name)]]
    except ValueError as exc:
        print(f"sandbox-ai secrets: {exc}", file=sys.stderr)
        return 1
    return net.main(argv, out=out, brief=True, prog="secrets", command=f"secrets {args.command}")


def main(argv, stdin=sys.stdin, out=print):
    parser = argparse.ArgumentParser(prog="sandbox-ai secrets", description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog="\n\n".join(__doc__.split("\n\n")[1:4]))
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list")
    commands.add_parser("backend")
    setting = commands.add_parser("set")
    setting.add_argument("name")
    setting.add_argument("-e", "--from-env", metavar="VARIABLE", help="take the value from this variable")
    commands.add_parser("delete", aliases=["remove"]).add_argument("name")  # remove: as vpn, llm and hosts say
    listing.add_argument("--run", metavar="RUN",
                         help="the API keys the running sandbox RUN was given during the run (secrets give)")
    listing.add_argument("--json", action="store_true", help="with --run: as JSON, for scripts")
    given = commands.add_parser("give", help="give a running sandbox the secret NAME as an API key, now",
                                description="Give the running sandbox RUN the secret NAME as an API key for HTTPS "
                                            "requests to HOST: its connection proxy puts the key in on the wire where "
                                            "the agent sends the placeholder (export VARIABLE=..., said here); the "
                                            "agent never sees it. The older form is sandbox-ai net allow RUN "
                                            "--api-key HOST=VARIABLE=NAME.")
    given.add_argument("run")
    given.add_argument("name", help="the secret")
    given.add_argument("--api-key", required=True, metavar="HOST", dest="host",
                       help="the HTTPS server the key is for (a host name); the sandbox may reach it from now on")
    given.add_argument("--as", metavar="VARIABLE", dest="variable",
                       help="the variable whose placeholder the agent sends (default: NAME)")
    given.add_argument("--keep", action="store_true",
                       help="also for the next starts: into the project's trusted .sandbox-ai.json, as a reference")
    taken = commands.add_parser("revoke", help="take back an API key given during the run")
    taken.add_argument("run")
    taken.add_argument("name", help="the secret, or the variable it was given as")
    args = parser.parse_args(argv)
    if args.command in ("give", "revoke") or (args.command == "list" and args.run):
        return given_main(args, out)
    try:
        backend = store()
        if args.command == "backend":
            out(backend.name)
        elif args.command == "list":
            names = sorted(set(backend.names()) | (set(FileStore().names()) if backend.name != "file" else set()))
            in_backend = set(backend.names())
            rows = [[name, backend.name if name in in_backend else "file"] for name in names]
            if rows:  # values are never shown, only where each is kept
                widths = [max(len(r[i]) for r in rows + [["NAME", "STORE"]]) for i in range(2)]
                for row in [["NAME", "STORE"]] + rows:
                    out(f"{row[0].ljust(widths[0])}  {row[1]}")
            else:
                out("no secrets")
        elif args.command == "set":
            if args.from_env:
                if args.from_env not in os.environ:
                    raise ValueError(f"the variable {args.from_env} is not set")
                value = os.environ[args.from_env]
            elif stdin is sys.stdin and sys.stdin.isatty():
                import getpass  # typed at a terminal: not shown, not in the scrollback
                value = getpass.getpass(f"value of {args.name} (not shown): ")
            else:
                text = stdin.read(LIMIT + 2)
                value = text[:-1] if text.endswith("\n") else text
                if "\n" in value:  # a PEM key, JSON over lines: refused, never cut to its first line
                    raise ValueError("the value must be one line (it had several; a multi-line value such as a key "
                                     "file: keep it as a file of mode 0600 instead)")
            if not value or len(value) > LIMIT or any(ord(c) < 32 for c in value):
                raise ValueError("the value must be one line of 1-8192 characters")
            backend.set(check_name(args.name), value)
            out(f"{args.name} kept ({backend.name})")
        else:
            found = backend.delete(args.name) | (FileStore().delete(args.name) if backend.name != "file" else False)
            out(f"{args.name} deleted" if found else f"{args.name} was not there")
        return 0
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f"sandbox-ai secrets: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
