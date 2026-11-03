#!/usr/bin/env python3
"""sandbox-ai secrets: API keys and tokens for sandboxes, kept in your system keyring.

  sandbox-ai secrets list                  names (never values) and where they are kept
  sandbox-ai secrets set NAME              read the value from stdin (or --from-env VARIABLE)
  sandbox-ai secrets delete NAME
  sandbox-ai secrets backend               keyring or file

Secrets go to the Secret Service (GNOME Keyring, KWallet, KeePassXC; through secret-tool) when it is
there, else to private files (0600) in ~/.local/share/sandbox-ai/secrets. Sandboxes get them only when
named: --secret-env NAME (an environment variable for the agent), --git-token-secret URL=NAME, or an
MCP server's variable (sandbox-ai mcp add ... --env NAME). Values are never printed.

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
            raise ValueError(f"the keyring refused the secret: {result.stderr.strip()[:200]}")

    def delete(self, name):
        return self.tool("clear", "service", "sandbox-ai", "name", check_name(name)).returncode == 0


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
    return result.returncode in (0, 1) and "not provided by any .service" not in result.stderr


def store():
    """The keyring if it works here, else the private files (SANDBOX_AI_SECRETS=file forces the files)."""
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


def main(argv, stdin=sys.stdin, out=print):
    parser = argparse.ArgumentParser(prog="sandbox-ai secrets", description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog="\n\n".join(__doc__.split("\n\n")[1:3]))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    commands.add_parser("backend")
    setting = commands.add_parser("set")
    setting.add_argument("name")
    setting.add_argument("-e", "--from-env", metavar="VARIABLE", help="take the value from this variable")
    commands.add_parser("delete").add_argument("name")
    args = parser.parse_args(argv)
    try:
        backend = store()
        if args.command == "backend":
            out(backend.name)
        elif args.command == "list":
            names = sorted(set(backend.names()) | (set(FileStore().names()) if backend.name != "file" else set()))
            for name in names:
                out(f"{name}  ({backend.name if name in backend.names() else 'file'})")
            if not names:
                out("no secrets")
        elif args.command == "set":
            value = os.environ.get(args.from_env, "") if args.from_env else stdin.readline().rstrip("\n")
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
