#!/usr/bin/env python3
"""sandbox-ai hosts: other machines (or Proxmox LXC containers) whose sandbox-ai this one manages over SSH.

  sandbox-ai hosts list [--json]                       the hosts, "local" first
  sandbox-ai hosts add NAME USER@HOST [--port N] [--identity FILE] [--description TEXT] [--command PATH]
  sandbox-ai hosts remove NAME
  sandbox-ai hosts test NAME [--json]                  SSH, the host's sandbox-ai version, its doctor
  sandbox-ai hosts run NAME -- ARGS...                 `sandbox-ai ARGS` on that host (e.g. -- list --json)

The desktop app and the web interface (sandbox-ai web) use the same list (~/.config/sandbox-ai/hosts.json) and
reach each host the same way: SSH with your keys, never a password prompt (BatchMode), the host key learnt at the
first contact and checked strictly from then on, each argument quoted on its own. Nothing listens on the network
on the hosts: they only need sshd and sandbox-ai. A non-interactive SSH login reads no ~/.bashrc on Debian, so the
remote command falls back to ~/.local/bin/sandbox-ai (install.sh's place) when sandbox-ai is not on its PATH.

Standard library only.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

LOCAL = "local"
NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
TARGET = re.compile(r"(?:[A-Za-z0-9_][A-Za-z0-9_.-]{0,31}@)?(?:[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?|\[[0-9A-Fa-f:.]+\])")
COMMAND = re.compile(r"[A-Za-z0-9_./~-]{1,200}")
CONNECT_TIMEOUT = 10
LIMIT = 1 << 20


def config_file():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "sandbox-ai" / "hosts.json"


def known_hosts_file():
    """sandbox-ai's own known_hosts: the keys of the hosts it manages, apart from yours (~/.ssh/known_hosts is
    read too, for keys you accepted already)."""
    return config_file().with_name("known_hosts")


_launcher = None


def printable(text):
    """TEXT safe for a terminal: the launcher's printable (one implementation for every command)."""
    global _launcher
    if _launcher is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("agent_sandbox_for_hosts",
                                                      Path(__file__).resolve().parent / "agent_sandbox.py")
        _launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_launcher)
    return _launcher.printable(str(text))


def check(name, entry):
    """ENTRY (a host of the file) checked: {"ssh", "port", "identity", "description", "command"}."""
    if not isinstance(name, str) or not NAME.fullmatch(name) or name == LOCAL:
        raise ValueError(f"{printable(name)!r}: a host's name is 1-32 of a-z, 0-9, '_', '-' (not {LOCAL})")
    if not isinstance(entry, dict) or set(entry) - {"ssh", "port", "identity", "description", "command"}:
        raise ValueError(f"host {name}: allowed keys are ssh, port, identity, description, command")
    target = entry.get("ssh")
    if not isinstance(target, str) or not TARGET.fullmatch(target) or target.startswith("-"):
        raise ValueError(f"host {name}: ssh is USER@HOST or HOST: {printable(target)!r}")
    port = entry.get("port", 22)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError(f"host {name}: port is 1-65535")
    identity = entry.get("identity")
    if identity is not None and (not isinstance(identity, str) or not os.path.isabs(os.path.expanduser(identity))
                                 or "\n" in identity):
        raise ValueError(f"host {name}: identity is an absolute path (or ~/...)")
    description = entry.get("description", "")
    if not isinstance(description, str) or len(description) > 200 or not description.isprintable():
        raise ValueError(f"host {name}: description is one line of at most 200 characters")
    command = entry.get("command")
    if command is not None and (not isinstance(command, str) or not COMMAND.fullmatch(command)):
        raise ValueError(f"host {name}: command is the path of sandbox-ai there")
    return {"ssh": target, "port": port, **({"identity": identity} if identity else {}),
            "description": description, **({"command": command} if command else {})}


def load(path=None):
    """{name: host} of the file; an empty list when there is none. A damaged file is an error, never half read."""
    path = Path(path or config_file())
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return {}
    with os.fdopen(fd, "rb") as stream:
        data = stream.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise ValueError(f"{path}: too large")
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"{path}: not valid JSON ({exc})") from None
    hosts = document.get("hosts") if isinstance(document, dict) else None
    if not isinstance(hosts, dict):
        raise ValueError(f"{path}: {{\"version\": 1, \"hosts\": {{NAME: {{\"ssh\": \"USER@HOST\"}}}}}}")
    return {name: check(name, entry) for name, entry in hosts.items()}


def save(hosts, path=None):
    path = Path(path or config_file())
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".hosts-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump({"version": 1, "hosts": hosts}, stream, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def entries(path=None):
    """[{"name", "kind", ...}]: this machine, then each host of the file."""
    return [{"name": LOCAL, "kind": "local", "description": "this machine"},
            *({"name": name, "kind": "ssh", **entry} for name, entry in sorted(load(path).items()))]


def ssh_base(entry, tty=False):
    """The ssh command line for ENTRY up to the remote command: no prompt, the host key strictly checked once
    known (the first key is learnt: accept-new), into sandbox-ai's own known_hosts as well."""
    known = known_hosts_file()
    if '"' in str(known):
        raise ValueError(f"{known}: a path with a double quote cannot be given to ssh")
    known.parent.mkdir(mode=0o700, parents=True, exist_ok=True)  # accept-new writes the first key there
    options = ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
               # Quoted for ssh's own word splitting: a path with a space would otherwise be two files, neither of
               # them sandbox-ai's, and accept-new would accept any key at every connection.
               "-o", f'UserKnownHostsFile="{known}" "~/.ssh/known_hosts"', "-o", f"ConnectTimeout={CONNECT_TIMEOUT}",
               "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4", "-p", str(entry["port"])]
    if entry.get("identity"):
        options += ["-o", "IdentitiesOnly=yes", "-i", os.path.expanduser(entry["identity"])]
    return ["ssh", "-tt" if tty else "-T", *options, "--", entry["ssh"]]


def remote_command(entry, args):
    """The one string ssh hands the remote shell: sandbox-ai with ARGS, each quoted; sandbox-ai from PATH, else
    ~/.local/bin/sandbox-ai (a non-interactive login reads no ~/.bashrc), else the host's own command."""
    quoted = " ".join(shlex.quote(str(arg)) for arg in args)
    if entry.get("command"):
        return f"exec {shlex.quote(entry['command'])} {quoted}".rstrip()
    return (f'if command -v sandbox-ai >/dev/null 2>&1; then exec sandbox-ai {quoted}; '
            f'else exec "$HOME/.local/bin/sandbox-ai" {quoted}; fi')


def argv(name, args, tty=False, path=None):
    """The command line that runs `sandbox-ai ARGS` on host NAME (LOCAL: this machine's sandbox-ai)."""
    if any(not isinstance(arg, str) or "\0" in arg for arg in args):
        raise ValueError("an argument is not a string without NUL")
    if name == LOCAL:
        here = Path(__file__).resolve().parent
        return [sys.executable, "-I", str(here / "main.py"), *args]
    hosts = load(path)
    if name not in hosts:
        raise ValueError(f"no host {printable(name)}; see: sandbox-ai hosts list")
    entry = hosts[name]
    return [*ssh_base(entry, tty), remote_command(entry, args)]


def run(name, args, input=None, timeout=120, path=None, runner=subprocess.run):
    """(exit status, stdout, stderr) of `sandbox-ai ARGS` on host NAME; 255 when SSH itself failed."""
    command = argv(name, args, path=path)
    try:
        done = runner(command, input=input, capture_output=True, text=True, timeout=timeout,
                      **({} if input is not None else {"stdin": subprocess.DEVNULL}))
    except subprocess.TimeoutExpired:
        return 124, "", f"sandbox-ai on {name}: no answer within {timeout} s"
    except OSError as exc:
        return 127, "", f"{command[0]}: {exc.strerror}"
    return done.returncode, done.stdout, done.stderr


def run_json(name, args, timeout=120, path=None, runner=subprocess.run):
    """The JSON a `sandbox-ai ARGS --json` prints on host NAME, or a ValueError with what it said."""
    status, out, err = run(name, args, timeout=timeout, path=path, runner=runner)
    try:
        return json.loads(out)
    except ValueError:
        raise ValueError(f"{name}: sandbox-ai {' '.join(args[:2])}: exit status {status}: "
                         f"{printable((err or out).strip()[-500:])}") from None


def test(name, path=None, runner=subprocess.run):
    """{"name", "ssh", "version", "doctor": [...], "error"}: whether NAME answers and how its sandbox-ai is."""
    result = {"name": name, "ssh": False, "version": None, "doctor": None, "error": None}
    status, out, err = run(name, ["--version"], timeout=30, path=path, runner=runner)
    if status != 0:
        result["error"] = printable((err or out).strip()[-500:]) or f"exit status {status}"
        if status == 255:
            result["error"] += " (SSH: is the key in ~/.ssh/authorized_keys there, and the host reachable?)"
        return result
    result["ssh"], result["version"] = True, out.strip()
    try:
        result["doctor"] = run_json(name, ["doctor", "--json"], timeout=90, path=path, runner=runner)
    except ValueError as exc:
        result["error"] = str(exc)
    return result


def cli(argv_, out=print):
    parser = argparse.ArgumentParser(prog="sandbox-ai hosts", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="the hosts")
    listing.add_argument("--json", action="store_true")
    adding = commands.add_parser("add", help="add a host reached over SSH")
    adding.add_argument("name")
    adding.add_argument("ssh", metavar="USER@HOST")
    adding.add_argument("--port", type=int, default=22)
    adding.add_argument("--identity", metavar="FILE", help="the SSH key to use (default: your SSH agent's)")
    adding.add_argument("--description", default="")
    adding.add_argument("--command", dest="remote_command", metavar="PATH",
                        help="sandbox-ai's path there, if not the usual")
    removing = commands.add_parser("remove", help="forget a host")
    removing.add_argument("name")
    testing = commands.add_parser("test", help="SSH, the host's sandbox-ai and its doctor")
    testing.add_argument("name")
    testing.add_argument("--json", action="store_true")
    running = commands.add_parser("run", help="sandbox-ai ARGS on a host")
    running.add_argument("name")
    running.add_argument("args", nargs=argparse.REMAINDER)
    a = parser.parse_args(argv_)
    if a.command == "list":
        found = entries()
        if a.json:
            out(json.dumps(found, indent=2))
            return 0
        for entry in found:
            where = "this machine" if entry["kind"] == "local" else f"ssh {entry['ssh']}:{entry['port']}"
            out(f"{entry['name']:<16} {where:<36} {printable(entry.get('description', ''))}")
        return 0
    hosts = load()
    if a.command == "add":
        if a.name in hosts:
            raise ValueError(f"host {a.name} exists; remove it first")
        entry = check(a.name, {"ssh": a.ssh, "port": a.port, **({"identity": a.identity} if a.identity else {}),
                               "description": a.description,
                               **({"command": a.remote_command} if a.remote_command else {})})
        hosts[a.name] = entry
        save(hosts)
        out(f"host {a.name} added; check it with: sandbox-ai hosts test {a.name}")
        return 0
    if a.command == "remove":
        if a.name not in hosts:
            raise ValueError(f"no host {printable(a.name)}")
        del hosts[a.name]
        save(hosts)
        out(f"host {a.name} removed")
        return 0
    if a.command == "test":
        result = test(a.name)
        if a.json:
            out(json.dumps(result, indent=2))
        else:
            out(f"{a.name}: " + (f"ssh ok, {printable(result['version'])}" if result["ssh"] else "ssh failed"))
            if result["error"]:
                out(f"  {printable(result['error'])}")
            for check_ in result["doctor"] or []:
                if check_.get("status") in ("warn", "fail"):
                    out(f"  {check_['status'].upper():<4}  {printable(check_.get('check'))}: "
                        f"{printable(check_.get('detail'))}")
        return 0 if result["ssh"] and not result["error"] else 1
    args = a.args[1:] if a.args[:1] == ["--"] else a.args
    if not args:
        raise ValueError("sandbox-ai hosts run NAME -- ARGS...")
    command = argv(a.name, args, tty=sys.stdin.isatty())
    return subprocess.run(command).returncode


if __name__ == "__main__":
    try:
        sys.exit(cli(sys.argv[1:]))
    except (OSError, ValueError) as exc:
        print(f"sandbox-ai hosts: {printable(exc)}", file=sys.stderr)
        sys.exit(1)
