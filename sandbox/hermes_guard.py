"""sandbox-ai's telemetry guard inside Hermes' own Python (sandbox/hermes.py: guard_files, guard_mounts).

Hermes' telemetry is always off in sandbox-ai (the user's rule) - also from inside the sandbox. The managed
configuration (/etc/hermes) pins its switches, but Hermes v0.22.5 reads the shared-metrics switches - collection
(telemetry.shared_metrics.enabled) and sending (.send) - only from the profile's own ~/.hermes/config.yaml, which the
agent can write: `hermes setup telemetry`, the full setup's Shared Metrics menu, the TUI's and the dashboard's consent
calls, `hermes config edit` or any editor turned local counting on for the session (the proxy refused the sending).
And HERMES_MANAGED_DIR or PYTEST_CURRENT_TEST in the environment took the managed configuration away altogether.

The launcher mounts this file and a .pth that loads it read-only into Hermes' site-packages, so every process of
Hermes' Python runs install() at its start - the `hermes` command, `python -m hermes_cli.main`, the TUI's gateway, the
dashboard, the launcher's own answer script:

- a Hermes command that would turn telemetry on is refused with MESSAGE before Hermes runs: `setup telemetry`,
  `config set|unset` of a telemetry, monitoring or computer-use telemetry key, an opt-out variable of PINNED_ENV set
  to another value, HERMES_MANAGED_DIR or PYTEST_CURRENT_TEST (SCOPE_ENV);
- the opt-out variables are set again and SCOPE_ENV dropped in every process (and so in what Hermes starts);
- Hermes' modules are patched as they are imported (PATCHES): every read of the configuration, raw or merged, sees
  each of SWITCHES off whatever a file says; the consent answer is always "No thanks" and counts as given (Hermes
  never asks); the collection gate and the sending policy are off; the Shared Metrics setup says MESSAGE; the managed
  configuration is always /etc/hermes.

While rules for Hermes' own tools hold (HOOKS_MARKER, written by the launcher), it keeps their shell hooks: a Hermes
command with --safe-mode is refused, HERMES_SAFE_MODE dropped, and the arguments a plugin or shell hook changes asked
of the rules again before the call runs (patch_plugins).

Standalone, standard library only: it runs in the sandbox, under Hermes' CPython, before Hermes is imported.
"""
import functools
import json
import os
from pathlib import Path
import subprocess
import sys

MESSAGE = "sandbox-ai: Hermes' telemetry is always off in sandbox-ai and cannot be turned on"
MANAGED_DIR = "/etc/hermes"  # sandbox-ai's managed configuration (sandbox/hermes.py: MANAGED_DIR)
OFFER_VERSION = 2  # Hermes v0.22.5's consent offer version (shared_metrics_consent.OFFER_VERSION), if unreadable
# The opt-out variables (sandbox/hermes.py: TELEMETRY_ENV is this).
PINNED_ENV = {
    "CUA_DRIVER_RS_TELEMETRY_ENABLED": "0",  # the computer-use driver's PostHog events (tools/computer_use)
    "OTEL_SDK_DISABLED": "true",  # OpenTelemetry (opentelemetry-api is in the lock; no exporter is)
    "DO_NOT_TRACK": "1", "HF_HUB_DISABLE_TELEMETRY": "1",  # the common opt-outs of tools Hermes may run
}
# Variables that take the managed configuration away (hermes_cli/managed_scope.py: get_managed_dir honors an override
# of its directory, and ignores /etc/hermes under pytest).
SCOPE_ENV = ("HERMES_MANAGED_DIR", "PYTEST_CURRENT_TEST")
# Every telemetry switch of Hermes' configuration: off in every read of it.
SWITCHES = (("telemetry", "shared_metrics", "enabled"), ("telemetry", "shared_metrics", "send"),
            ("monitoring", "gateway_health_export", "enabled"), ("monitoring", "export", "otlp", "enabled"),
            ("computer_use", "cua_telemetry"))
# `hermes config set|unset KEY`: refused for these keys and everything below them.
CONFIG_KEYS = ("telemetry", "monitoring", "computer_use.cua_telemetry")
CLI_MODULES = ("hermes_cli.main", "hermes_cli")  # python -m MODULE: Hermes' command line
# Rules for Hermes' own tools (sandbox/hermes.py: HOOKS_MARKER_NAME, written when they hold): their shell hooks are in
# the managed configuration, and the guard keeps them in every Hermes of the sandbox - --safe-mode and
# HERMES_SAFE_MODE (no shell hook registered at all) are refused or dropped, and the arguments a plugin or shell hook
# changes ("modify") are asked of the rules again (patch_plugins).
HOOKS_MARKER = MANAGED_DIR + "/sandbox-ai-hooks.json"
SAFE_MODE_MESSAGE = ("Hermes then registers no shell hook, and the rules for its own tools (a ruleset's \"tool\" "
                     "rules) hold through them")
HOOK_COMMAND = ["/usr/bin/python3", "-I", "/sandbox/runtime.py", "--internal-hook", "hermes", "PreToolUse"]
HOOK_WAIT = 130  # seconds: the hook client's own wait (runtime.HOOK_TIMEOUT) and a margin


def refused(args):
    """(what, why) of Hermes command-line ARGS that would turn telemetry on, or None. Only before a "--"."""
    options = list(args[:args.index("--")]) if "--" in args else list(args)
    for index, word in enumerate(options):
        words = [value for value in options[index + 1:] if not value.startswith("-")]
        if word == "setup" and words[:1] == ["telemetry"]:
            return "hermes setup telemetry", "its shared-metrics answer is always No thanks"
        if word == "config" and words[:1] in (["set"], ["unset"]):
            key = words[1].split("=", 1)[0].strip().lower() if len(words) > 1 else ""
            if any(key == name or key.startswith(name + ".") for name in CONFIG_KEYS):
                return f"hermes config {words[0]} {key}", "the telemetry switches are pinned off"
    return None


def hooks_hold():
    """Rules for Hermes' own tools hold in this sandbox (HOOKS_MARKER)."""
    return os.path.exists(HOOKS_MARKER)


def safe_mode_arg(args):
    """--safe-mode among Hermes' ARGS before a "--" (it registers no shell hook), else None."""
    options = list(args[:args.index("--")]) if "--" in args else list(args)
    return "--safe-mode" if "--safe-mode" in options else None


def hooks_refused(args):
    """The refusal of a Hermes command line ARGS that would drop the rule hooks while they hold, else None."""
    found = safe_mode_arg(args) if hooks_hold() else None
    return f"sandbox-ai: {found} is refused: {SAFE_MODE_MESSAGE}" if found else None


def recheck(tool_name, args, run=None):
    """None if the rules allow Hermes' TOOL_NAME with ARGS (the hook client, as Hermes' own shell hook runs it), else
    the refusal; whatever goes wrong refuses."""
    run = run or subprocess.run
    try:
        cwd = os.getcwd()
    except OSError:
        cwd = ""
    try:
        done = run(HOOK_COMMAND, input=json.dumps({"tool_name": tool_name, "tool_input": args, "cwd": cwd},
                                                  default=repr), capture_output=True, timeout=HOOK_WAIT)
    except Exception as exc:  # noqa: BLE001 - refused, never let through
        return f"sandbox-ai: the tool call is refused: its rules could not be checked ({type(exc).__name__})"
    if done.returncode == 0:
        return None
    text = done.stderr.decode("utf-8", "replace") if isinstance(done.stderr, bytes) else str(done.stderr or "")
    return text.strip()[:2000] or "sandbox-ai: the tool call is refused"


def refused_environment(environ):
    """(what, why) of a variable in ENVIRON that would turn telemetry on or take the pins away, or None."""
    for name, value in PINNED_ENV.items():
        if name in environ and environ[name] != value:
            return f"{name}={environ[name]}", f"it stays {value}"
    for name in SCOPE_ENV:
        if name in environ and (environ[name].strip() or name == "PYTEST_CURRENT_TEST"):
            return f"{name}={environ[name]}", f"Hermes' configuration stays the one at {MANAGED_DIR}"
    return None


def cli_args(argv):
    """The arguments of a Hermes command line in ARGV (sys.orig_argv: the interpreter's own) - python -m
    hermes_cli.main ARGS, as the image's `hermes` runs it, or a path to hermes_cli/main.py - or None for anything
    else (python -c, another module or script, a bare interpreter). The interpreter's options as CPython reads them:
    flags combine (-Im), -m, -c, -X and -W take a value (the rest of the word or the next one)."""
    index = 1
    while index < len(argv):
        word = argv[index]
        if word == "--check-hash-based-pycs":
            index += 2
            continue
        if word.startswith("--"):
            index += 1
            continue
        if word.startswith("-") and len(word) > 1:
            letters, skip = word[1:], 1
            for position, letter in enumerate(letters):
                if letter in "mcXW":
                    value = letters[position + 1:]
                    if not value:
                        value = argv[index + 1] if index + 1 < len(argv) else ""
                        skip = 2
                    if letter == "m":
                        return list(argv[index + skip:]) if value in CLI_MODULES else None
                    if letter == "c":
                        return None
                    break
            index += skip
            continue
        if word == "-":
            return None
        script = word.replace("\\", "/").rstrip("/")
        return list(argv[index + 1:]) if script.endswith(("hermes_cli/main.py", "hermes_cli")) else None
    return None


def scrub(config):
    """CONFIG (a mapping of Hermes' configuration, changed in place) with each of SWITCHES that is on turned off;
    anything else as it was. Returns CONFIG."""
    if not isinstance(config, dict):
        return config
    for path in SWITCHES:
        section = config
        for key in path[:-1]:
            section = section.get(key) if isinstance(section, dict) else None
        if isinstance(section, dict) and section.get(path[-1]) not in (None, False):
            section[path[-1]] = False
    return config


def pin_environment(environ):
    """ENVIRON with the opt-out variables set and SCOPE_ENV dropped (in place); HERMES_SAFE_MODE too while rules for
    Hermes' own tools hold (hooks_hold)."""
    environ.update(PINNED_ENV)
    for name in SCOPE_ENV:
        environ.pop(name, None)
    if hooks_hold():
        environ.pop("HERMES_SAFE_MODE", None)


def say(stream=None):
    """MESSAGE where a person sees it."""
    stream = stream or sys.stderr
    try:
        stream.write(MESSAGE + "\n")
        stream.flush()
    except Exception:
        pass


def warn(what):
    try:
        sys.stderr.write(f"sandbox-ai: Hermes' telemetry guard could not patch {what} (telemetry stays off: "
                         "pinned in /etc/hermes, answered No thanks before every start, refused by the proxy)\n")
    except Exception:
        pass


def warn_hooks(what):
    try:
        sys.stderr.write(f"sandbox-ai: the guard could not patch {what}: arguments a Hermes plugin or shell hook "
                         "changes are not asked of the rules again (the rule hooks still check every call)\n")
    except Exception:
        pass


def wrap(module, name, make, warning=None):
    """Replace MODULE.NAME by make(original) once; warn (WARNING, else warn) when Hermes has no such function."""
    original = getattr(module, name, None)
    if not callable(original):
        (warning or warn)(f"{module.__name__}.{name}")
        return None
    if getattr(original, "_sandbox_ai_guard", False):
        return original
    replacement = functools.wraps(original)(make(original))
    replacement._sandbox_ai_guard = True
    setattr(module, name, replacement)
    return replacement


def patch_config(module):
    """hermes_cli.config: the raw and the merged configuration (every reader goes through these two) scrubbed."""
    for name in ("_read_raw_config_impl", "_load_config_impl"):
        wrap(module, name, lambda original: lambda *args, **kwargs: scrub(original(*args, **kwargs)))


def offer_version(module):
    version = getattr(module, "OFFER_VERSION", OFFER_VERSION)
    return version if isinstance(version, int) else OFFER_VERSION


def patch_consent(module):
    """hermes_cli.observability.shared_metrics_consent: the answer is always No thanks and always given."""
    def consent_state(original):
        def state(raw_cfg, *args, **kwargs):
            found = original(raw_cfg, *args, **kwargs)
            found = dict(found) if isinstance(found, dict) else {}
            return {**found, "enabled": False, "send": False, "decided": True, "reask": False}
        return state

    def set_answer(original):
        return lambda target, enabled=False, send=False, *args, **kwargs: original(target, False, False, *args,
                                                                                    **kwargs)

    def save_consent(original):
        def save(enabled=False, send=False, *args, **kwargs):
            if enabled or send:
                say()
            return original(False, False, *args, **kwargs)
        return save

    wrap(module, "consent_state", consent_state)
    wrap(module, "set_answer", set_answer)
    wrap(module, "save_consent", save_consent)


def patch_relay(module):
    """hermes_cli.observability.relay_shared_metrics: the collection gate is closed (its teardown still runs)."""
    def enabled(original):
        def gate(*args, **kwargs):
            try:
                original(*args, **kwargs)
            except Exception:
                pass
            return False
        return gate

    wrap(module, "enabled", enabled)


def patch_send_config(module):
    """hermes_cli.observability.shared_metrics_send_config: nothing is collected, nothing is sent."""
    def resolve(original):
        def resolved(config, *args, **kwargs):
            found = original(scrub(config) if isinstance(config, dict) else config, *args, **kwargs)
            kind = getattr(module, "SendConfig", None)
            try:
                return kind(enabled=False, send=False, endpoint=getattr(found, "endpoint", ""))
            except Exception:
                return found
        return resolved

    wrap(module, "resolve_send_config", resolve)


def patch_setup(module):
    """hermes_cli.setup: the Shared Metrics section (hermes setup telemetry, the tools menu) only says MESSAGE."""
    original = getattr(module, "setup_telemetry", None)

    def setup_telemetry(_original):
        def section(*args, **kwargs):
            say(sys.stdout)
        return section

    replacement = wrap(module, "setup_telemetry", setup_telemetry)
    sections = getattr(module, "SETUP_SECTIONS", None)
    if replacement is not None and isinstance(sections, list):
        for index, entry in enumerate(sections):
            if isinstance(entry, tuple) and len(entry) == 3 and (entry[2] is original or entry[0] == "telemetry"):
                sections[index] = (entry[0], entry[1], replacement)


def patch_scope(module):
    """hermes_cli.managed_scope: the managed configuration is sandbox-ai's, whatever the environment says."""
    def managed_dir(_original):
        def found(*args, **kwargs):
            path = Path(MANAGED_DIR)
            return path if path.is_dir() else None
        return found

    wrap(module, "get_managed_dir", managed_dir)


def patch_plugins(module):
    """hermes_cli.plugins, while rules hold: a call's arguments as a plugin's or shell hook's "modify" left them
    (Hermes runs every pre_tool_call hook on the same arguments and applies the changes after them, so the rule hook
    never saw these) are asked of the rules again; a refusal blocks the call with the rules' reason."""
    if not hooks_hold():
        return

    def dispatch(original):
        def checked(tool_name, args, *more, **kwargs):
            block, modified = original(tool_name, args, *more, **kwargs)
            if block is None and modified is not None and modified != args:
                why = recheck(tool_name, modified)
                if why:
                    return why, modified
            return block, modified
        return checked

    wrap(module, "_dispatch_pre_tool_call_hooks", dispatch, warn_hooks)


PATCHES = {
    "hermes_cli.plugins": patch_plugins,
    "hermes_cli.config": patch_config,
    "hermes_cli.managed_scope": patch_scope,
    "hermes_cli.setup": patch_setup,
    "hermes_cli.observability.shared_metrics_consent": patch_consent,
    "hermes_cli.observability.relay_shared_metrics": patch_relay,
    "hermes_cli.observability.shared_metrics_send_config": patch_send_config,
}


class Loader:
    """A module's own loader, with its patch applied right after the module ran (before anyone uses it)."""

    def __init__(self, inner, patch):
        self._inner, self._patch = inner, patch

    def create_module(self, spec):
        return self._inner.create_module(spec)

    def exec_module(self, module):
        self._inner.exec_module(module)
        try:
            self._patch(module)
        except Exception as exc:
            warn(f"{module.__name__} ({type(exc).__name__}: {exc})")

    def __getattr__(self, name):
        return getattr(self._inner, name)


class Finder:
    """Finds PATCHES' modules through the other finders and hands out their loaders wrapped (Loader)."""

    def find_spec(self, name, path=None, target=None):
        patch = PATCHES.get(name)
        if patch is None:
            return None
        for finder in sys.meta_path:
            find = getattr(finder, "find_spec", None)
            if isinstance(finder, Finder) or find is None:
                continue
            spec = find(name, path, target)
            if spec is not None:
                if spec.loader is not None and hasattr(spec.loader, "exec_module"):
                    spec.loader = Loader(spec.loader, patch)
                return spec
        return None

    def invalidate_caches(self):
        pass


def process_argv():
    """This process's whole command line (the interpreter, its options, -m MODULE, the arguments): sys.orig_argv
    (Python 3.10+); before, /proc/self/cmdline (in a .pth sys.argv is not set yet)."""
    if hasattr(sys, "orig_argv"):
        return sys.orig_argv
    try:
        with open("/proc/self/cmdline", "rb") as handle:
            return [part.decode("utf-8", "surrogateescape") for part in handle.read().split(b"\0") if part]
    except OSError:
        return []


def install(argv=None, environ=None):
    """At the start of every process of Hermes' Python (its .pth): refuse a Hermes command line that would turn
    telemetry on (MESSAGE, exit status 2), pin the variables, patch Hermes' modules as they are imported. Once."""
    environ = os.environ if environ is None else environ
    args = cli_args(list(argv if argv is not None else process_argv()))
    if args is not None:
        hooks = hooks_refused(args)
        if hooks:
            try:
                sys.stderr.write(hooks + "\n")
                sys.stderr.flush()
            finally:
                os._exit(2)
        found = refused(args) or refused_environment(environ)
        if found:
            try:
                sys.stderr.write(f"{MESSAGE} ({found[0]} is refused: {found[1]})\n")
                sys.stderr.flush()
            finally:
                os._exit(2)  # in a .pth: an exception would only be printed, and Hermes would start
    pin_environment(environ)
    if not any(isinstance(finder, Finder) for finder in sys.meta_path):
        sys.meta_path.insert(0, Finder())
    for name, patch in PATCHES.items():  # imported before the guard (never in the sandbox; in tests)
        if name in sys.modules:
            patch(sys.modules[name])
