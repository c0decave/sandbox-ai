#!/usr/bin/env python3
"""sandbox-ai defaults: your defaults for every start (~/.config/sandbox-ai/defaults.json), without editing it.

  sandbox-ai defaults [show [--json]]     what the file says, and which keys and values there are
  sandbox-ai defaults set KEY VALUE       VALUE as JSON (strict, ["credentials","home"], false, 90) or plain text
  sandbox-ai defaults unset KEY           back to sandbox-ai's own default

The keys are the launcher's: telemetry (off, strict: telemetry is always off; the former on is refused here and
ignored where an old file still has it), mode (lax ... paranoid), canaries (false or a list of
categories), keep_days, subnet, subnet6, preset_dirs (extra preset directories; `sandbox-ai presets dirs add DIR`
checks where each lies), safeguard_mute (kinds of safeguard warnings never announced: `sandbox-ai safeguards kinds`),
name_style (codename: new starts are named like brisk-otter-lantern, the default; project: AGENT-PROJECT-xxxx),
desktop_notifications (all, the default: the launchers' desktop notifications - questions, safeguards, limits,
alarms - while neither the app nor the web interface watches; alarms: only a canary's alarm; none: nothing),
loop_guard (the loop guard's layers for starts with --llm, an object such as {"warn": 4, "steps": 60,
"instructions": false}; `sandbox-ai llm set NAME --loop-guard` changes one server's), llm_traffic (what the LLM
traffic log records: llm, the default - the --llm server's requests -, all - the cloud provider's too -, off;
`sandbox-ai llm traffic`), llm_traffic_limit (MiB of one request's record, 1 to 64, default 4), log_days (the logs
older than that many days are removed by sandbox-ai gc and its daily pass; default 30, 0: never), file_reads (how
files.jsonl keeps reads: folders, the default - summed per folder and 30 s -, files - each file -, off),
connection_bodies (text, the default: a binary answer is logged by its size, digest and first bytes; all: whole),
hide_secrets (true: files that look like logins or keys - ~/.git-credentials, an agent's login, a private key - are
left out of the file views: sandbox-ai session files and memory, the app's and the web interface's; false, the
default: they are listed and marked, their text shown only when asked),
ask_timeout (seconds a request waits for the answer to its question, 5 to 3600, default 60; unanswered by then, the
question stays open and its answer counts for the next try; --ask-timeout wins),
package_min_age_days (the package guard asks before a download of a package version younger than that many days, 0 to
3650, default 14; 0: no age check; rulesets and the modes high and paranoid can only lengthen it; `sandbox-ai
packages`),
fix_questions (what a start lacks and sandbox-ai can provide - its image not built, an MCP server not added, a
toolchain not in the store: all, the default - the start lists everything and asks once, Fix all / Choose / Cancel -
or each - one question per fix; --fix does it without asking, and without a terminal nothing is asked),
monitor_interval (the system monitor's seconds between two samples of a run, 1 to 3600, default 10; 0: no
recording; `sandbox-ai top`, `sandbox-ai stats usage`), budget (USD a start's model requests may cost, as --budget;
`sandbox-ai spend`),
the workspace checkpoints (`sandbox-ai checkpoint`): checkpoint_start (true: one when a start begins),
checkpoint_every (minutes between two while it runs, 0 to 1440, default 0: none), checkpoint_mounts (false: the rw
--mount folders too), checkpoint_exclude (names or relative paths never checkpointed nor restored; default
node_modules, .venv, target and build caches), and their limits per project: checkpoint_days (30), checkpoint_count
(50), checkpoint_gb (10; 0: no limit for each). The storage check (`sandbox-ai gc --check`, the app's Optimize…):
storage_check (off, weekly - the default -, daily: how often the app runs it by itself), storage_threshold_gb (5: what
could be freed above that is said), storage_growth_gb (2: a start that grew more since the last check is named), and
its automatic rules, each off unless set, applied by the check and gc: storage_caches_days (empty the download caches
of the stopped kept starts unused for that many days), storage_compact_days (compact the transcripts older than that,
also of a running start), storage_pack_days (pack the stopped kept starts unused for that long), storage_logs_days
(remove the log folders not written for that long). The update mode's keys (update_mode, update_window,
update_source) are shown here and set by `sandbox-ai update mode`, which makes or removes its timer too.
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


_filelock = None


def locked():
    """The lock of a read-modify-write of defaults.json (filelock.py): `defaults set` and `unset` run by the CLI, the
    app and the web interface at the same time never lose each other's change (a security mode set in one, dropped by
    the other's write of what it had read before)."""
    global _filelock
    if _filelock is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_filelock", HERE / "filelock.py")
        with LOADING:
            if _filelock is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _filelock = loaded
    return _filelock.locked(launcher().defaults_file())


def read():
    """(document, path) of the defaults file ({"version": 1} without one); a broken file is a ValueError."""
    path = launcher().defaults_file()
    try:
        document = json.loads(path.read_text())
    except FileNotFoundError:
        return {"version": 1}, path
    except RecursionError:  # (nested deeper than Python reads: damaged)
        raise ValueError(f"{path}: not valid JSON (nested too deeply); fix or remove it first") from None
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
            "desktop_notifications": list(getattr(L, "DESKTOP_NOTIFICATIONS", ())),
            **loop_layers(L),
            **({"safeguard_kinds": [{"kind": k, "level": v[0], "title": v[1]}
                                    for k, v in L.load_safeguards_module().KINDS.items()]}
               if hasattr(L, "load_safeguards_module") else {})}


def loop_layers(L):
    """The loop guard's layers with sandbox-ai's own values and the ranges of the counts (for the front ends)."""
    try:
        llm = L.load_llm_module()
        return {"loop_guard_layers": dict(llm.LOOP_DEFAULTS),
                "loop_guard_ranges": {key: list(value) for key, value in llm.LOOP_RANGES.items()}}
    except (AttributeError, OSError, ImportError):
        return {}


def help_style(prog):
    """The layout of this command's help, the one of every command with subcommands (helpstyle.py)."""
    loaded = sys.modules.get("sandbox_ai_helpstyle")
    if loaded is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_helpstyle", HERE / "helpstyle.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        sys.modules["sandbox_ai_helpstyle"] = loaded
    return loaded.CommandsFormatter(prog)


def cli(argv, out=print):
    parser = argparse.ArgumentParser(prog="sandbox-ai defaults", formatter_class=help_style,
                                     description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command")
    showing = commands.add_parser("show", help="the file and its keys")
    showing.add_argument("-j", "--json", action="store_true", help="print the result as JSON (for programs)")
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
            retired = getattr(launcher(), "TELEMETRY_RETIRED", None)
            width = max([10, *map(len, keys())])
            for key in keys():
                note = "  (ignored: telemetry is always off since 0.1.95; sandbox-ai defaults unset telemetry)" \
                    if key == "telemetry" and retired is not None and values.get(key) == retired else ""
                out(f"  {key:{width}} {json.dumps(values[key]) if key in values else '-'}{note}")
        return 0
    if a.key not in keys():
        raise ValueError(f"no key {a.key!r}; the keys are {', '.join(keys())}")
    if a.key in getattr(launcher(), "UPDATE_KEYS", ()):
        raise ValueError(f"{a.key} is set by sandbox-ai update mode (it makes or removes the update timer too): "
                         "sandbox-ai update mode MODE [--at HH:MM-HH:MM] [--source URL]")
    with locked():  # from reading the file to writing it back
        document, path = read()
        if a.command == "set":
            document[a.key] = parse_value(a.value)
            if a.key == "telemetry" and document[a.key] == getattr(launcher(), "TELEMETRY_RETIRED", None):
                raise ValueError("telemetry is off or strict: the agents' telemetry is always off since 0.1.95 "
                                 "(on is gone)")
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
