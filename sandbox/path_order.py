"""Which sandbox-ai the PATH finds first, when there are several (install.sh's notes at the end of an installation;
`sandbox-ai doctor`). The user's case (2026-11-18): a system installation's /usr/local/bin/sandbox-ai-app (0.1.97) came
before ~/.local/bin in PATH, so `install.sh --from .` as the user updated an installation the shell never ran.

A command is followed to the tree it runs (its link, resolved: DATA/versions/X/COMMAND, or a checkout's), whose
sandbox/agent_sandbox.py says the version; DATA/versions/X below PREFIX/lib/sandbox-ai is a system installation, any
other DATA a user's own. A pip installation's command is a script of its own (no tree beside it). Standard library only:
install.sh runs this file with python3 -I."""
from __future__ import annotations

import os
from pathlib import Path
import re
import shlex
import sys

COMMANDS = ("sandbox-ai", "sandbox-ai-update", "sandbox-ai-app")
VERSION_LINE = re.compile(r'^__version__ = "([^"\n]{1,40})"', re.M)
DEFAULT_PREFIX = "/usr/local"


class Found:
    """COMMAND at EXECUTABLE (as PATH names it), running TREE (None: not a sandbox-ai tree) of VERSION; KIND: "system"
    (PLACE: its prefix), "user" (PLACE: its data folder), "checkout" (PLACE: the tree), "pip" or "other". (No
    dataclass: doctor.py loads this file without registering it as a module.)"""

    def __init__(self, command, executable, tree, version, kind, place):
        self.command, self.executable, self.tree = command, executable, tree
        self.version, self.kind, self.place = version, kind, place

    def __repr__(self):
        return f"Found({self.command!r}, {self.executable!r}, {self.version!r}, {self.kind!r}, {self.place!r})"


def printable(text):
    text = str(text)
    return text if text.isprintable() else "".join(c if c.isprintable() else f"\\x{ord(c):02x}" for c in text)


def version_of(tree):
    """The __version__ line of TREE's sandbox/agent_sandbox.py (some 600 lines down), or None."""
    try:
        with open(Path(tree) / "sandbox" / "agent_sandbox.py", encoding="utf-8", errors="replace") as stream:
            for number, line in enumerate(stream):
                found = VERSION_LINE.match(line)
                if found or number > 5000:
                    return found[1] if found else None
    except OSError:
        return None
    return None


def tree_of(executable):
    """The sandbox-ai tree EXECUTABLE runs (the folder its resolved file lies in, with sandbox/agent_sandbox.py), or
    None."""
    try:
        folder = Path(executable).resolve().parent
    except (OSError, RuntimeError):
        return None
    return folder if (folder / "sandbox" / "agent_sandbox.py").is_file() else None


def describe(command, executable):
    """A Found for EXECUTABLE (a file named COMMAND in a folder of PATH)."""
    tree = tree_of(executable)
    if tree is None:
        try:
            head = Path(executable).read_bytes()[:4096].decode(errors="replace")
        except OSError:
            head = ""
        pip = head.startswith("#!") and "python" in head.split("\n", 1)[0] and "sandbox" in head
        return Found(command, str(executable), None, None, "pip" if pip else "other", None)
    if tree.parent.name == "versions":
        data = tree.parent.parent
        if data.name == "sandbox-ai" and data.parent.name == "lib":  # (a user's: XDG_DATA_HOME/sandbox-ai)
            return Found(command, str(executable), str(tree), version_of(tree), "system", str(data.parent.parent))
        return Found(command, str(executable), str(tree), version_of(tree), "user", str(data))
    return Found(command, str(executable), str(tree), version_of(tree), "checkout", str(tree))


def candidates(command, path):
    """Every executable file named COMMAND in the folders of PATH, in PATH's order (a folder named twice: once)."""
    seen, found = set(), []
    for folder in (path or "").split(os.pathsep):
        if not folder or folder in seen:
            continue
        seen.add(folder)
        candidate = os.path.join(folder, command)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            found.append(describe(command, candidate))
    return found


def same(first, second):
    """FIRST and SECOND (Found) are the same installation."""
    if first.tree and second.tree:
        try:
            return Path(first.tree).resolve() == Path(second.tree).resolve()
        except OSError:
            return first.tree == second.tree
    return os.path.realpath(first.executable) == os.path.realpath(second.executable)


def version_key(text):
    found = re.match(r"(\d+)\.(\d+)\.(\d+)", text or "")
    return tuple(int(n) for n in found.groups()) if found else None


def named(found):
    """'sandbox-ai 0.1.97 (a system installation in /usr/local)' and the like."""
    version = f"sandbox-ai {found.version}" if found.version else "sandbox-ai (version unknown)"
    where = {"system": f"a system installation in {found.place}", "user": f"an installation in {found.place}",
             "checkout": f"a checkout in {found.place}", "pip": "a pip installation",
             "other": "not sandbox-ai's"}[found.kind]
    return f"{version} ({printable(where)})" if found.kind != "other" else f"a file {printable(where)}"


def ways_out(found, source=None):
    """How to remove FOUND's installation or bring it up to date (SOURCE: install.sh's --from, when given)."""
    if found.kind == "system":
        prefix = "" if found.place == DEFAULT_PREFIX else f" --prefix {shlex.quote(found.place)}"
        installer = shlex.quote(f"{found.place}/lib/sandbox-ai/current/install.sh")
        update = (f"sudo {shlex.quote(source.rstrip('/') + '/install.sh') if source != '.' else './install.sh'} "
                  f"--from {shlex.quote(source)} --system{prefix}") if source else \
            f"sudo {installer} --system{prefix} (or from a checkout: sudo ./install.sh --from . --system{prefix})"
        return f"remove it (sudo {installer} --uninstall --system{prefix}) or update it ({update})"
    if found.kind == "user":
        data = Path(found.place)
        environment = "" if data.parent == Path.home() / ".local" / "share" else \
            f"XDG_DATA_HOME={shlex.quote(str(data.parent))} "
        installer = shlex.quote(str(data / "current" / "install.sh"))
        return (f"remove it ({environment}{installer} --uninstall) or update it ({environment}{installer}, or from a "
                f"checkout: {environment}./install.sh --from .)")
    if found.kind == "checkout":
        return f"update the checkout (git pull in {shlex.quote(found.place)}) or take it out of PATH"
    if found.kind == "pip":
        return ("remove it (pip uninstall sandbox-ai-app, or pipx uninstall sandbox-ai-app) or update it "
                "(pip install -U sandbox-ai-app)")
    return f"rename or remove {shlex.quote(found.executable)}"


def shadowed(mine, commands, path):
    """{the other installation's Found: [commands]} for each of COMMANDS whose first in PATH is not of tree MINE."""
    mine = Found("", "", str(mine), None, "user", None)
    others = []
    for command in commands:
        found = candidates(command, path)
        if not found or same(found[0], mine):
            continue
        for other, names in others:
            if same(other, found[0]):
                names.append(command)
                break
        else:
            others.append((found[0], [command]))
    return others


def install_notes(mine, commands, path, folder, source=None):
    """install.sh's lines (a user installation into tree MINE, its commands linked into FOLDER): each other
    installation PATH finds first for some of COMMANDS, with both versions and the ways out."""
    version = version_of(mine) or "?"
    lines = []
    for other, names in shadowed(mine, commands, path):
        where = os.path.dirname(other.executable)
        lines.append(f"warning: your PATH finds {', '.join(names)} in {printable(where)} first - {named(other)} - "
                     f"before the {version} installed now in {printable(folder)}: {ways_out(other, source)}; or put "
                     f"{printable(folder)} before {printable(where)} in PATH")
    return lines


def doctor_check(here, path=None):
    """sandbox-ai doctor: (status, title, detail, hint) - a warning when sandbox-ai or sandbox-ai-app is installed more
    than once in different versions and PATH finds one first that is not the newest (or not this one, HERE)."""
    path = os.environ.get("PATH", "") if path is None else path
    here = Path(here)
    problems, hints, seen = [], [], []
    for command in ("sandbox-ai", "sandbox-ai-app"):
        found = candidates(command, path)
        if here.parent.name == "versions" and (here / command).is_file() and \
                not any(f.tree and Path(f.tree).resolve() == here.resolve() for f in found):
            found.append(describe(command, here / command))  # (this one, started by its full path)
        installs = []
        for item in found:
            if item.kind != "other" and not any(same(item, known) for known in installs):
                installs.append(item)
        if len(installs) < 2:
            continue
        first, rest = installs[0], installs[1:]
        differing = [item for item in rest if item.version != first.version]
        if not differing:
            continue
        other = differing[0]
        problems.append(f"{command} first in PATH is {first.version or '?'} from {printable(first.executable)}, but "
                        f"{other.version or '?'} is installed in {printable(other.place or other.executable)}")
        stale = first if (version_key(first.version) or ()) < (version_key(other.version) or ()) else other
        hint = f"the older one, {stale.version or '?'} in {printable(stale.place or stale.executable)}: " \
               f"{ways_out(stale)}"
        if stale is first:
            hint += f"; or put {printable(os.path.dirname(other.executable))} first in PATH"
        if hint not in hints and not any(same(stale, known) for known in seen):
            hints.append(hint)
            seen.append(stale)
    if not problems:
        return "ok", "Commands in PATH", "one sandbox-ai installation in PATH (or the same version each time)", ""
    return "warn", "Commands in PATH", "; ".join(problems), "; ".join(hints)


def main(argv=None):
    """python3 -I path_order.py --mine TREE --folder DIR [--from SOURCE] [--path PATH] COMMAND...: install_notes."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    options = {"--mine": None, "--folder": None, "--from": None, "--path": os.environ.get("PATH", "")}
    commands = []
    while arguments:
        word = arguments.pop(0)
        if word in options and arguments:
            options[word] = arguments.pop(0)
        else:
            commands.append(word)
    if not options["--mine"] or not options["--folder"]:
        print("usage: path_order.py --mine TREE --folder DIR [--from SOURCE] [--path PATH] COMMAND...",
              file=sys.stderr)
        return 2
    for line in install_notes(options["--mine"], commands or COMMANDS, options["--path"], options["--folder"],
                              options["--from"] or None):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
