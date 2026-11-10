#!/usr/bin/env python3
"""sandbox-ai defaults: your defaults for every start (~/.config/sandbox-ai/defaults.json), without editing it.

  sandbox-ai defaults [show] [--json]     what the file says, and which keys and values there are
  sandbox-ai defaults set KEY VALUE       VALUE as JSON (strict, ["credentials","home"], false, 90) or plain text
  sandbox-ai defaults unset KEY           back to sandbox-ai's own default

The keys are the launcher's: telemetry (off, strict, on), mode (lax ... paranoid), canaries (false or a list of
categories), keep_days, subnet, subnet6, preset_dirs (extra preset directories; `sandbox-ai presets dirs add DIR`
checks where each lies), safeguard_mute (kinds of safeguard warnings never announced: `sandbox-ai safeguards kinds`),
name_style (codename: new starts are named like brisk-otter-lantern, the default; project: AGENT-PROJECT-xxxx).
A value is checked by the launcher itself before the file is replaced
(atomically, mode 0600); the other keys stay as they are. An option given at a start wins over the file.
The web interface and the desktop app change other hosts' defaults through this command.

Standard library only.
"""
import argparse
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import threading

LOADING = threading.RLock()  # the lazily loaded helper modules below

HERE = Path(__file__).resolve().parent
_launcher = None


def launcher():
    global _launcher
    if _launcher is None:
        spec = importlib.util.spec_from_file_location("agent_sandbox_for_defaults", HERE / "agent_sandbox.py")
        with LOADING:  # published only once loaded: another thread never sees a half-made module
            if _launcher is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _launcher = loaded
    return _launcher


def keys():
    return sorted(launcher().DEFAULTS_KEYS - {"version"})


def read():
    """(document, path) of the defaults file ({"version": 1} without one); a broken file is a ValueError."""
    path = launcher().defaults_file()
    try:
        document = json.loads(path.read_text())
    except FileNotFoundError:
        return {"version": 1}, path
    except (OSError, ValueError) as exc:
        raise ValueError(f"{path}: {exc}; fix or remove it first") from None
    if not isinstance(document, dict) or document.get("version") != 1:
        raise ValueError(f"{path}: expected an object with \"version\": 1; fix or remove it first")
    return document, path


def check(document, path):
    """DOCUMENT as the launcher reads it at a start: its own checks, on a copy next to PATH."""
    L = launcher()
    candidate = path.with_name(f".{path.name}.{os.getpid()}-{secrets.token_hex(4)}.check")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        write(candidate, document)
        original = L.defaults_file
        L.defaults_file = lambda: candidate
        empty = tempfile.mkdtemp(prefix="sandbox-ai-defaults-")  # a workspace that holds nothing of yours
        try:
            L.load_user_defaults(L.parse_args(["claude", "--no-config", "--workspace", empty]))
        except ValueError as exc:  # named as the file it is about, not the copy it was checked on
            raise ValueError(str(exc).replace(str(candidate), str(path))) from None
        finally:
            L.defaults_file = original
            os.rmdir(empty)
    finally:
        candidate.unlink(missing_ok=True)


def write(path, document):
    temporary = path.with_name(f".{path.name}.{os.getpid()}-{secrets.token_hex(4)}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(document, stream, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def parse_value(text):
    with contextlib.suppress(ValueError):
        return json.loads(text)
    return text


def check_preset_dirs(value):
    """Extra preset directories: never inside a folder sandboxes can or could write (presets.py's check)."""
    spec = importlib.util.spec_from_file_location("sandbox_ai_presets_for_defaults", HERE / "presets.py")
    presets = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(presets)
    trees = presets.writable_trees()
    for path in presets.check_dirs(value):
        why = presets.unsafe_dir(path, trees)
        if why:
            raise ValueError(f"preset_dirs: {path} cannot hold presets: {why}")


def describe():
    """What `show --json` adds to the values: the keys and the choices of each."""
    L = launcher()
    canary = L.load_canary_module()
    return {"keys": keys(), "telemetry_levels": list(getattr(L, "TELEMETRY_LEVELS", ())),
            "modes": list(L.MODES.NAMES), "canary_categories": list(canary.CATEGORIES),
            "canary_default": list(canary.DEFAULT_CATEGORIES), "name_styles": list(L.NAME_STYLES),
            **({"safeguard_kinds": [{"kind": k, "level": v[0], "title": v[1]}
                                    for k, v in L.load_safeguards_module().KINDS.items()]}
               if hasattr(L, "load_safeguards_module") else {})}


def cli(argv, out=print):
    parser = argparse.ArgumentParser(prog="sandbox-ai defaults", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command")
    showing = commands.add_parser("show", help="the file and its keys")
    showing.add_argument("-j", "--json", action="store_true")
    setting = commands.add_parser("set", help="set a key")
    setting.add_argument("key")
    setting.add_argument("value")
    unsetting = commands.add_parser("unset", help="remove a key")
    unsetting.add_argument("key")
    a = parser.parse_args(argv)
    if a.command in (None, "show"):
        document, path = read()
        values = {k: v for k, v in document.items() if k != "version"}
        if getattr(a, "json", False):
            out(json.dumps({"path": str(path), "values": values, **describe()}, indent=2))
        else:
            out(f"{path}{'' if path.exists() else ' (none yet)'}")
            for key in keys():
                out(f"  {key:10} {json.dumps(values[key]) if key in values else '-'}")
        return 0
    if a.key not in keys():
        raise ValueError(f"no key {a.key!r}; the keys are {', '.join(keys())}")
    document, path = read()
    if a.command == "set":
        document[a.key] = parse_value(a.value)
        if a.key == "preset_dirs":
            check_preset_dirs(document[a.key])
    elif document.pop(a.key, None) is None:
        out(f"{a.key} was not set")
        return 0
    check(document, path)
    write(path, document)
    out(f"{a.key} = {json.dumps(document[a.key])}" if a.command == "set" else f"{a.key} removed")
    return 0


def main(argv=None):
    try:
        return cli(sys.argv[1:] if argv is None else argv)
    except ValueError as exc:
        print(f"sandbox-ai defaults: {launcher().printable(str(exc))}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
