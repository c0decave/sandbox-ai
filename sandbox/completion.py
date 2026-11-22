#!/usr/bin/env python3
"""sandbox-ai completion: completion for bash, zsh and fish.

  sandbox-ai completion bash|zsh|fish      the script for that shell (install.sh puts it where the shell finds it)
  sandbox-ai completion install [SHELL]    write the scripts for your shells now, and a marked block into
                                           ~/.bashrc and ~/.zshrc that loads them (--no-rc: not)

A program cannot change a shell that already runs: install says the line that loads it into a terminal open now
(source FILE); new terminals have it.

The scripts ask `sandbox-ai __complete WORDS...` for the candidates, so they always know the commands and options
of the sandbox-ai that runs: every command and subcommand, each option and its choices (--mode, --telemetry,
--backend ...), and the names you have: starts (attach, stop, resume ...), running sandboxes (net, devices, workspace,
canaries), hosts, MCP servers, LLM servers, VPN profiles, kits and secrets. Paths are left to the shell.

Standard library only.
"""
import argparse
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import runpy
import sys

HERE = Path(__file__).resolve().parent
FILES, DIRS = "__files__", "__dirs__"  # tell the script to complete paths
AGENTS = ("claude", "codex", "opencode", "opencode2", "hermes", "dsh", "qwen", "goose", "openhands", "nanobot", "letta", "pi")
OTHER_COMMANDS = ("build", "update", "version", "help")
PATH_HINTS = ("FILE", "DIR", "PATH", "KEY", "CERT")


class Captured(Exception):
    def __init__(self, parser):
        super().__init__("parser")
        self.parser = parser


def module(name):
    spec = importlib.util.spec_from_file_location(f"sandbox_{name}_for_completion", HERE / f"{name}.py")
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def commands():
    """{command: (script, prefix...)} of `sandbox-ai`, as main.py runs them."""
    return dict(module("main").PYTHON_COMMANDS)


# Every way a script asks its parser to parse. All of them are caught: since Python 3.12.7/3.13 the intermixed
# ones no longer go through parse_known_args (they call argparse's internal _parse_known_args2), so catching
# parse_args and parse_known_args alone let `completion` (parse_intermixed_args) print its --help and exit.
PARSE_METHODS = ("parse_args", "parse_known_args", "parse_intermixed_args", "parse_known_intermixed_args")

# Why parser_of found no parser, per command (for the report of a test, or a look by hand): its contract stays
# None, so a script that cannot be read gets no completion and never fails the shell.
NOT_FOUND = {}


def parser_of(command):
    """The argparse parser COMMAND's script builds, caught when it parses: the script runs as it would, up to
    its parse_args (nothing after that), with its output thrown away."""
    table = commands()
    if command not in table:
        NOT_FOUND[command] = "not a command of main.py's PYTHON_COMMANDS"
        return None
    script, *prefix = table[command]
    if script == "agent_sandbox.py":
        return module("agent_sandbox").build_parser()
    original = {name: getattr(argparse.ArgumentParser, name) for name in PARSE_METHODS
                if hasattr(argparse.ArgumentParser, name)}

    def capture(self, *args, **kwargs):
        raise Captured(self)
    argv = sys.argv
    for name in original:
        setattr(argparse.ArgumentParser, name, capture)
    sys.argv = [str(HERE / script), *prefix, "--help"]
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            runpy.run_path(str(HERE / script), run_name="__main__")
    except Captured as found:
        parser = found.parser
        # A prefix (starts.py list, attach ...) is the script's own first word: complete below it.
        for word in prefix:
            sub = subparsers(parser).get(word)
            if sub is None:
                break
            parser = sub
        return parser
    except BaseException as exc:  # noqa: BLE001 - a script that cannot be read gets no completion
        NOT_FOUND[command] = (f"{script} ended with {type(exc).__name__}: {exc} before any of "
                              f"{', '.join(original)} was called")
        return None
    finally:
        for name, method in original.items():
            setattr(argparse.ArgumentParser, name, method)
        sys.argv = argv
    NOT_FOUND[command] = f"{script} ran to its end without calling any of {', '.join(original)}"
    return None


def subparsers(parser):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def options(parser):
    return {option: action for action in parser._actions for option in action.option_strings}


def positionals(parser):
    return [a for a in parser._actions if not a.option_strings and not isinstance(a, argparse._SubParsersAction)]


# --- the names you have ---------------------------------------------------------------------------------

def quietly(function):
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return list(function())
    except Exception:  # noqa: BLE001 - completion never fails the shell
        return []


def start_names():
    return quietly(lambda: [e["name"] for e in module("starts").entries(prune=False)])


def other_users_starts():
    """For root: USER:NAME of every other user's start (allusers.py: their records, read as data; Podman is not asked
    on a Tab); nothing for anybody else."""
    def names():
        allusers = module("allusers")
        if allusers.effective_uid() != 0:
            return []
        return [f"{entry['user']}:{entry['name']}" for entry in
                allusers.all_entries(True, own=lambda include_all: [], pods_of=lambda user, runtime: {})]
    return quietly(names)


def start_names_all():
    """The starts here, and for root also the other users' as USER:NAME."""
    return start_names() + other_users_starts()


def run_names():
    def names():
        for run, record in module("net").running().items():
            yield run
            if record.get("name"):
                yield record["name"]
    return quietly(names)


def review_runs():
    """The runs whose workspace review sandbox-ai keeps (workspace review RUN --keep|--restore), newest first."""
    def names():
        root = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "sandbox-ai" / "workspace-review"
        found = sorted(root.glob("*/*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
        return list(dict.fromkeys(path.stem for path in found if not path.name.startswith(".")))
    return quietly(names)


def host_names():
    return quietly(lambda: [e["name"] for e in module("hosts").entries()])


def mcp_names():
    return quietly(lambda: sorted(module("mcp").definitions()))


def toolchain_names():
    """The toolchains of your store (sandbox-ai toolchain list): NAME, and NAME@VERSION where it has several."""
    def names():
        for name, versions in sorted(module("toolchains").installed().items()):
            yield name
            if len(versions) > 1:
                yield from (f"{name}@{version}" for version in sorted(versions))
    return quietly(names)


def attached_toolchains():
    """The toolchains attached to running sandboxes while they ran (toolchain detach takes one of them)."""
    def names():
        toolchains = module("toolchains")
        for record in module("net").running().values():
            yield from (row["name"] for row in toolchains.run_listing(record) if row["source"] == "added")
    return sorted(set(quietly(names)))


def llm_names():
    return quietly(lambda: sorted(module("llm").load_store().get("endpoints", {})))


def preset_names():
    return quietly(lambda: sorted(module("presets").catalog()))


def schedule_names():
    """The schedules of `sandbox-ai schedule` (their files' names: no file is read)."""
    return quietly(lambda: module("schedule").names())


def board_names():
    """The boards kept by `sandbox-ai board import` (their files' names: no file is read)."""
    def read():
        bundle = module("board_bundle")
        return sorted(path.stem for path in bundle.boards_dir().glob("*.json") if bundle.NAME.fullmatch(path.stem))
    return quietly(read)


def hermes_features():
    return quietly(lambda: [*module("hermes").FEATURES, "gateway:all"])


def qwen_features():
    return quietly(lambda: list(module("qwen").FEATURES))


def goose_features():
    return quietly(lambda: [*module("goose").FEATURES, "gateway:all"])


def openhands_features():
    return quietly(lambda: list(module("openhands").FEATURES))


def nanobot_features():
    return quietly(lambda: [*module("nanobot").FEATURES, "gateway:all"])


def letta_features():
    return quietly(lambda: list(module("letta").FEATURES))


def opencode2_features():
    return quietly(lambda: list(module("opencode2").FEATURES))


def pi_features():
    return quietly(lambda: list(module("pi").FEATURES))


def vpn_names():
    return quietly(lambda: module("vpn").list_profiles())


def kit_names():
    return quietly(lambda: sorted(set(module("kits").available_kits())))


def plugin_kit_names():
    """The kits whose plugins and marketplaces sandbox-ai kits changes: yours and the default kit."""
    return quietly(lambda: sorted(set(module("kits").available_kits(builtin=False)) | {"default"}))


def marketplace_names():
    return quietly(lambda: module("plugins").marketplace_names())


def host_part_ids():
    """The ids of what the agents on this machine have of their own (sandbox-ai kits host-list)."""
    return quietly(lambda: sorted(p["id"] for p in module("hostkits").inventory(measured=False)["parts"]))


def plugin_ids():
    def ids():
        data = module("plugins").marketplaces()
        for market in data["marketplaces"]:
            if market["agent"] == "claude":
                yield from (p["id"] for p in market["plugins"])
        yield from (p["id"] for p in data["agents"].get("opencode", {}).get("plugins", []))
    return quietly(lambda: sorted(set(ids())))


def question_ids():
    return quietly(lambda: [q.get("id") for _, _, q in module("approvals").questions() if q.get("id")])


def answered_ids():
    """The answered questions of the running sandboxes (approvals ask-again ID)."""
    return quietly(lambda: [q.get("id") for _, _, q in module("approvals").questions(answered=True)
                            if q.get("id") and "result" in q])


def team_names():
    return quietly(lambda: sorted(p.name for p in module("team").teams_dir().iterdir() if p.is_dir()))


def bridge_ids():
    return quietly(lambda: module("bridge").all_ids())


def bridge_sides():
    """A side of a bridge: a start of this machine, or HOST: to go on with a start of another host."""
    return [*start_names(), *(f"{host}:" for host in host_names())]


def identity_names():
    return quietly(lambda: sorted(module("identities").load()))


def git_login_urls():
    return quietly(lambda: sorted({entry["url"] for entry in module("git_logins").load() if entry.get("url")}))


def web_users():
    return quietly(lambda: sorted(module("webui").load_users()))


def notify_channels():
    return quietly(lambda: [channel["name"] for channel in module("notify").load()["channels"]])


def own_price_models():
    """The models you priced yourself (sandbox-ai spend prices set): what spend prices remove takes."""
    return quietly(lambda: sorted({model for models in module("spend").load_settings()["prices"].values()
                                   for model in models}))


def default_keys():
    return quietly(lambda: module("defaults_cmd").keys())


def remembered_projects():
    return quietly(lambda: sorted(module("approvals").remembered()))


def remembered_entries(project):
    def entries():
        found = module("approvals").remembered().get(project, {})
        for field in ("hosts", "tcp", "blocked_hosts", "blocked_tcp"):
            yield from found.get(field, [])
    return quietly(entries)


def model_examples():
    """The models the profiles know by name (model_profiles.json beside sandbox-ai: each family's examples)."""
    def names():
        import json
        for family in json.loads((HERE / "model_profiles.json").read_text()).get("families", []):
            yield from family.get("examples", [])
    return quietly(lambda: sorted(set(names())))


def use_case_names():
    """The use cases of the local-model research (model_usecases.json beside sandbox-ai: their ids)."""
    def names():
        import json
        for entry in json.loads((HERE / "model_usecases.json").read_text()).get("use_cases", []):
            yield entry.get("id")
    return quietly(lambda: [name for name in names() if isinstance(name, str)])


def agent_targets():
    """An agent's image or one of your variants (images extend, check, packages ...)."""
    return [*AGENTS, *variant_names()]


def env_names():
    return sorted(os.environ)


def checkpoint_ids():
    """The workspace checkpoints' ids, newest first (their records only: no manifest is read)."""
    return quietly(lambda: [meta["id"] for meta in module("checkpoints").listing()][:500])


CUT_MODES = ("remote", "local", "api", "all", "off")  # net cut MODE (net.CUT_MODES and off)


# (command, subcommand or None, positional dest) -> what it names
NAMED = {
    ("attach", None, "name"): start_names, ("shell", None, "name"): start_names,
    ("pause", None, "name"): start_names,
    ("resume", None, "name"): start_names, ("stop", None, "name"): start_names_all,
    ("delete", None, "name"): start_names, ("rename", None, "old"): start_names,
    ("list", None, "history"): start_names, ("ps", None, "history"): start_names,
    ("net", "*", "run"): run_names, ("devices", "*", "run"): run_names, ("workspace", "list", "run"): run_names,
    ("workspace", "review", "run_id"): review_runs,
    ("limits", None, "run"): run_names, ("usage", None, "run"): run_names,
    ("canaries", "*", "run"): run_names, ("activity", None, "run"): run_names,
    ("safeguards", None, "run"): run_names, ("report", None, "name"): start_names_all,
    ("hosts", "*", "name"): host_names, ("mcp", "*", "name"): mcp_names, ("llm", "*", "name"): llm_names,
    ("llm", "traffic", "run"): run_names,
    ("spend", "run", "name"): run_names, ("spend", "raise", "name"): run_names,
    ("spend", "remove", "model"): own_price_models,
    ("vpn", "*", "name"): vpn_names, ("kits", "*", "name"): kit_names,
    # a running start's kits (kits_live.py): the start, and for attach the kit given to it
    ("kits", "attach", "start"): start_names, ("kits", "attach", "kit"): kit_names,
    ("kits", "detach", "start"): start_names, ("kits", "restart-agent", "start"): start_names,
    **{("kits", sub, "name"): plugin_kit_names for sub in ("add-plugin", "remove-plugin", "enable-plugin",
                                                            "disable-plugin", "add-marketplace", "remove-marketplace",
                                                            "import-marketplace")},
    ("kits", "remove-plugin", "plugin"): plugin_ids, ("kits", "enable-plugin", "plugin"): plugin_ids,
    ("kits", "disable-plugin", "plugin"): plugin_ids, ("kits", "add-plugin", "plugin"): plugin_ids,
    ("kits", "remove-marketplace", "marketplace"): marketplace_names,
    ("kits", "import-marketplace", "marketplace"): marketplace_names,
    ("kits", "host-show", "part"): host_part_ids, ("kits", "host-export", "parts"): host_part_ids,
    ("kits", "host-import", "parts"): host_part_ids,
    ("plugins", "marketplaces", "name"): marketplace_names,
    ("approvals", "allow", "id"): question_ids, ("approvals", "deny", "id"): question_ids,
    ("approvals", "show", "id"): question_ids,
    ("team", "*", "name"): team_names, ("approvals", "history", "run"): run_names,
    ("approvals", "ask-again", "id"): answered_ids, ("approvals", "blocked", "run"): run_names,
    ("approvals", "relay", "id"): answered_ids, ("approvals", "callback", "id"): answered_ids,
    ("approvals", "decided", "run"): run_names,
    ("hosts", "update", "names"): host_names, ("mcp", "attach", "start"): start_names,
    ("mcp", "detach", "start"): start_names, ("rules", "show", "name"): start_names,
    ("rules", "reload", "name"): start_names, ("presets", "show", "name"): preset_names,
    ("presets", "check", "name"): preset_names,
    ("bridge", "add", "a"): lambda: bridge_sides(), ("bridge", "add", "b"): lambda: bridge_sides(),
    **{("bridge", sub, "id"): lambda: bridge_ids() for sub in ("show", "revoke", "resume")},
    ("identities", "*", "identity"): lambda: identity_names(),
    **{("git-logins", sub, "url"): lambda: git_login_urls() for sub in ("remove", "give", "revoke", "check", "edit")},
    ("git-logins", "give", "run"): lambda: run_names(), ("git-logins", "revoke", "run"): lambda: run_names(),
    ("publish-logins", "give", "run"): lambda: run_names(),
    ("secrets", "give", "run"): lambda: run_names(), ("secrets", "revoke", "run"): lambda: run_names(),
    ("secrets", "send", "host"): lambda: host_names(),
    ("login", "give", "run"): lambda: start_names(),
    ("session", "*", "name"): lambda: start_names(), ("session", "copy", "host"): lambda: host_names(),
    ("session", "move", "host"): lambda: host_names(),
    ("schedule", "*", "name"): lambda: schedule_names(),
    ("web", "remove", "name"): lambda: web_users(), ("web", "reset", "name"): lambda: web_users(),
    ("notify", "remove", "name"): lambda: notify_channels(), ("notify", "test", "name"): lambda: notify_channels(),
    ("defaults", "set", "key"): lambda: default_keys(), ("defaults", "unset", "key"): lambda: default_keys(),
    ("llm", "profiles", "model"): lambda: model_examples(),
    ("llm", "recommend", "use_case_word"): use_case_names,  # llm recommend USE_CASE
    **{("images", sub, field): lambda: agent_targets() for sub, field in (
        ("packages", "agent"), ("extend", "target"), ("check", "target"), ("remove", "target"), ("history", "target"),
        ("os", "target"), ("search", "target"))},
    ("completion", None, "only"): lambda: ["bash", "zsh", "fish"],
    ("board", "show", "name"): lambda: board_names(), ("board", "delete", "name"): lambda: board_names(),
    ("board", "export", "out"): lambda: [FILES], ("session", "export", "out"): lambda: [FILES],
    ("session", "import", "file"): lambda: [FILES],
    ("checkpoint", "create", "run"): lambda: start_names(), ("checkpoint", "list", "run"): lambda: start_names(),
    **{("checkpoint", sub, field): lambda: checkpoint_ids() for sub, field in (
        ("show", "id"), ("diff", "id"), ("diff", "other"), ("delete", "ids"))},
    ("checkpoint", "restore", "target"): lambda: [*start_names(), *checkpoint_ids()],
    ("rules", "builtin", "name"): lambda: builtin_rulesets(),
    ("toolchain", "show", "name"): toolchain_names, ("toolchain", "remove", "name"): toolchain_names,
}

def builtin_rulesets():
    """sandbox-ai's own rulesets (sandbox/rulesets/NAME.json): --rules builtin:NAME, rules builtin NAME."""
    try:
        return sorted(path.stem for path in (HERE / "rulesets").glob("*.json"))
    except OSError:
        return []


def hide_globs():
    """--hide: the globs the wizard and the Board suggest (hide.SUGGESTED, kept the same by tests/test_hide.py)."""
    return [".env", ".env.*", "*.pem", "*.key", "id_rsa", "id_ed25519", "id_ecdsa", "credentials*.json", ".npmrc",
            ".pypirc", ".netrc", ".git-credentials"]


# A positional whose words depend on the ones before it: (command, subcommand) -> function(index of the word in
# it, the words before) -> candidates. "[RUN] X": a run's name first (it may be left out), then X.
POSITIONS = {
    ("net", "cut"): lambda index, given: [*(run_names() if index == 0 else []), *(CUT_MODES if index < 2 else ())],
    ("workspace", "add"): lambda index, given: [*(run_names() if index == 0 else []), FILES],
    ("workspace", "remove"): lambda index, given: [*(run_names() if index == 0 else []), FILES],
    ("devices", "add"): lambda index, given: [*(run_names() if index == 0 else []), FILES],
    ("devices", "remove"): lambda index, given: [*(run_names() if index == 0 else []), FILES],
    ("toolchain", "attach"): lambda index, given: [*(run_names() if index == 0 else []),
                                                    *(toolchain_names() if index < 2 else [])],
    ("toolchain", "detach"): lambda index, given: [*(run_names() if index == 0 else []),
                                                    *(attached_toolchains() if index < 2 else [])],
    ("approvals", "forget"): lambda index, given: remembered_projects() if index == 0 else
    remembered_entries(given[0]) if index == 1 else [],
    ("completion", "install"): lambda index, given: [shell for shell in ("bash", "zsh", "fish") if shell not in given],
}

# What nothing can be offered for, and why: a value only you know. The test that walks every parser
# (tests/test_completion.py) wants a provider above or an entry here for each position - a new command cannot miss
# its completion unnoticed. Options by name (whatever command has them), positionals by (command, subcommand, name).
FREE_TEXT = {
    # a toolchain of the signed catalog: `sandbox-ai toolchain catalog` lists them (nothing is downloaded on a Tab)
    ("toolchain", "add", "name"): "a toolchain of the catalog (sandbox-ai toolchain catalog)",
    # a search in a start's session files: a pattern and a text you type (session files --search, --text)
    **dict.fromkeys(("--search", "--text"), "a pattern or a text of yours"),
    # sizes, counts, times and numbers you choose
    **dict.fromkeys(("--memory", "--cpus", "--pids", "--tmp-size", "--tmp", "--llm-traffic-limit", "--tail", "--limit",
                     "--jobs", "--top", "--interval", "--wait", "--timeout", "--tool-timeout", "--context", "--ttl",
                     "--older-than", "--logs-older-than", "--transcripts", "--compact-transcripts", "--since",
                     "--until", "--watch", "--request",
                     "--bootstrap-attempts", "--bootstrap-stall", "--bootstrap-timeout", "--port",
                     "--for", "--idle", "--password-fd", "--ask-timeout", "--rate", "--quiet", "--every", "--at",
                     "--cron", "--keep-starts", "--budget", "--expect",
                     "--checkpoint-every"),
                    # (--for: a DURATION, secrets unlock; --rate, --quiet: notify add's seconds and hours; --every,
                    # --at, --cron: schedule add's times; --budget: USD; --expect: login --from-session's
                    # fingerprint, as shown)
                    "a value you choose"),
    # the launcher's list options (LIST_OPTIONS): one or more in one word, separated by commas or spaces (quoted)
    **dict.fromkeys(("--allow", "--allow-host", "--allow-domain", "--allow-shared-wildcard", "--allow-tcp",
                     "--allow-host-port", "--nameserver", "--no-inspect", "--vpn-bypass-tcp"),
                    "addresses you name: one or more, separated by commas or spaces"),
    # addresses, hosts, domains, URLs and networks: anything on the network
    **dict.fromkeys(("--subnet", "--subnet6", "--network", "--ip", "--ip6", "--allow-private",
                     "--podman-url", "--url", "--address", "--listen", "--hostname", "--check-url", "--login-server",
                     "--exit-node", "--dns", "--tailnet-allow", "--source", "--ntfy", "--webhook", "--api"),
                    "an address you name"),
    # texts and names of your own: a description, a new name, a task, a search
    **dict.fromkeys(("--description", "--email", "--user", "--cairn-goal", "--cairn-hint", "--cairn-origin", "--grep",
                     "--project-name", "--as", "--matcher", "--event", "--location", "--exit-country", "--provider",
                     "--digest", "--version", "--model", "--effort", "--image", "--base", "--from", "--builtin",
                     "--project", "--kind", "--map", "--drop-option", "--observer-actions", "--agents",
                     "--llm-models", "--llm-tools", "--canaries", "--publish", "--cairn-workers", "--gui-screen",
                     "--loop-guard", "--events", "--template", "--language", "--chat", "--phone-id", "--recipient",
                     "--prompt", "--commit", "--push",
                     "--label",
                     "--ca-of"), "a text or name of your own"),
    ("session", "duplicate", "new"): "the new session's name, of your own",
    # the package guard: a package of a registry, named by you (never looked up anywhere: not on a Tab either)
    "--allow-package": "a package you name: ECOSYSTEM:NAME[@VERSION]",
    "--sha256": "the SHA-256 you got for the file (64 hex digits)",
    "--source-sha256": "the source archive's SHA-256 mcp add --build-plan said (64 hex digits)",
    ("packages", "*", "spec"): "a package you name: ECOSYSTEM:NAME[@VERSION]",
    # the secret guard: a kind of value and a host, named by you (sandbox-ai secret-guard status lists the kinds)
    "--allow-send": "a kind of value and a host you name: WHAT@HOST",
    ("secret-guard", "*", "spec"): "a kind of value and a host you name: WHAT@HOST",
    # mcp copy and import: folders of another host, a start running there
    **dict.fromkeys(("--path", "--drop-path"), "a folder the export names (and one of the target host)"),
    "--attach": "a running start of the target host (asked over SSH: not on a Tab)",
    # session memory|files --file: a name of that start's listing (its HOME is read only when asked, never on a Tab)
    "--file": "a file of the start's listing (sandbox-ai session memory|files NAME)",
    # NAME=VALUE pairs, mounts and copies: their parts are yours
    **dict.fromkeys(("--env", "--secret-env", "--api-key", "--mcp-env", "--mcp-limits", "--mcp-mount", "--mount",
                     "--copy", "--git-token-env", "--git-token-secret", "--add-os", "--add-npm", "--add-pip",
                     "--remove-os", "--remove-npm", "--remove-pip", "--add-file", "--remove-file", "--role",
                     "--member-model", "--member-llm", "--member-mode"),
                    "NAME=VALUE or a mount of your own"),
    # secrets: never listed - reading their names may ask the keyring to unlock, which a Tab must never do
    **dict.fromkeys(("--secret", "--api-key-secret", "--header-secret", "--account-secret", "--auth-secret",
                     "--auth-key-secret", "--bridge-secret", "--token-secret", "--bot-token-secret",
                     "--webhook-secret"), "a secret's name (not listed: the keyring)"),
    ("secrets", "*", "name"): "a secret's name (not listed: the keyring)",
    ("secrets", "rename", "old"): "a secret's name (not listed: the keyring)",
    ("secrets", "rename", "new"): "the secret's new name",
    # new names, texts and what runs elsewhere
    ("rename", None, "new"): "the new name", ("images", "new", "name"): "the new variant's name",
    ("images", "build-image", "name"): "the new image's name", ("images", "derive", "name"): "the new image's name",
    ("images", "pull", "reference"): "an image in a registry", ("images", "search", "query"): "a package to look for",
    ("kits", "new-skill", "skill"): "the new skill's name", ("kits", "remove-skill", "skill"): "a skill of that kit",
    ("kits", "add-marketplace", "marketplace"): "the new marketplace's name",
    ("kits", "add-marketplace", "source"): "OWNER/REPO or a URL",
    ("defaults", "set", "value"): "the key's value", ("git-logins", "add", "url"): "a new Git host's URL",
    ("identities", "login", "url"): "a Git host's URL", ("identities", "logout", "url"): "a Git host's URL",
    ("mcp", "copy", "names"): "servers of the source host (asked over SSH: not on a Tab)",
    ("mcp", "image-load", "ref"): "the server's pinned image (mcp copy gives it)",
    ("hosts", "add", "ssh"): "USER@HOST of a new host", ("hosts", "run", "args"): "a sandbox-ai command on that host",
    "--value": "NAME=VALUE: a value the bundle withholds", "--rename": "KIND:OLD=NEW: a name of your own",
    "--replace": "KIND:NAME of a part the bundle brings",
    ("team", "post", "task"): "the task, in your words", ("web", "add", "name"): "the new user's name",
    ("notify", "add", "name"): "the channel's name (a new one, or one to replace)",
    ("spend", "raise", "usd"): "how many USD more", ("spend", "set", "usd"): "the limit in USD",
    ("spend", "set", "model"): "a model's name (or the start of its names)",
    ("spend", "set", "values"): "in=USD out=USD [cached=USD write=USD] per million tokens",
    ("notify", "link", "url"): "your web interface's public https URL",
    ("schedule", "add", "start"): "the start: AGENT [OPTIONS] [-- AGENT ARGS], as sandbox-ai AGENT takes it",
    ("schedule", "report", "run"): "a run of the schedule's history (sandbox-ai schedule history NAME)",
    ("llm", "load", "model"): "a model of that server (asked over the network: not on a Tab)",
    ("llm", "unload", "model"): "a model of that server (asked over the network: not on a Tab)",
}

# Secrets are not listed: reading their names may ask the keyring to unlock, which a Tab must never do.
VALUES = {"--home": start_names, "--unpack": start_names, "--detail": start_names,  # gc: a kept start
          "--hide": lambda: hide_globs(),  # the suggested globs (any other is yours)
          "--kit": kit_names, "--mcp": mcp_names, "--llm": llm_names, "--vpn": vpn_names, "--preset": preset_names,
          "--board": lambda: board_names(),
          "--server": llm_names, "--use-case": use_case_names,  # llm recommend, a start's --use-case
          "--to": lambda: ["local", *(host_names() or [])],  # mcp copy: the target host
          "--session": start_names, "--auth-from-session": start_names, "--from-session": start_names,
          "--name": start_names, "--for": start_names, "--host": host_names, "--helper": lambda: list(AGENTS),
          "--agent": lambda: list(AGENTS),  # models --agent, presets check --agent
          # The agents of the team image: the launcher's own list says which are not in it.
          "--with": lambda: sorted(agent for agent in AGENTS if agent not in module("agent_sandbox").NOT_IN_TEAM),
          "--identity": lambda: identity_names(), "--image-variant": lambda: variant_names(),
          "--run": lambda: run_names(), "--history": lambda: start_names(), "--from-env": env_names,
          "--toolchain": lambda: toolchain_names(),

          "--hermes-enable": lambda: hermes_features(), "--qwen-enable": lambda: qwen_features(),
          "--goose-enable": lambda: goose_features(), "--openhands-enable": lambda: openhands_features(), "--nanobot-enable": lambda: nanobot_features(), "--letta-enable": lambda: letta_features(),
          "--opencode2-enable": lambda: opencode2_features(),
          "--pi-enable": lambda: pi_features()}


# A word of a command whose script reads the rest with a parser of its own: (command, word) -> that parser
OWN_PARSERS = {("plugins", "marketplaces"): lambda: module("plugins").marketplaces_parser(),
               ("login", "give"): lambda: module("session_login").give_parser()}


def named(command, sub, dest):
    function = NAMED.get((command, sub, dest)) or NAMED.get((command, "*", dest))
    return function() if function else []


FLAGS = (argparse._StoreTrueAction, argparse._StoreFalseAction, argparse._CountAction, argparse._StoreConstAction,
         argparse._AppendConstAction, argparse._HelpAction, argparse._VersionAction)


def provider(command, sub, action):
    """What completes ACTION of COMMAND's parser (SUB: its subcommand): "flag", "commands", "choices", "names",
    "path", "free" (FREE_TEXT: nothing to offer, and why) - or None: a position without completion (a gap)."""
    if isinstance(action, argparse._SubParsersAction):
        return "commands"
    if action.help == argparse.SUPPRESS:
        return "hidden"
    if action.option_strings and (action.nargs == 0 or isinstance(action, FLAGS)):
        return "flag"
    if action.choices:
        return "choices"
    metavar = str(action.metavar or action.dest or "").upper()
    if action.option_strings:
        if any(option in VALUES for option in action.option_strings) and "DURATION" not in metavar:
            return "names"
        if action.type is Path or any(hint in metavar for hint in PATH_HINTS) or "--workspace" in action.option_strings:
            return "path"
        return "free" if any(option in FREE_TEXT for option in action.option_strings) else None
    if (command, sub) in POSITIONS or NAMED.get((command, sub, action.dest)) or NAMED.get((command, "*", action.dest)):
        return "names"
    if action.type is Path or any(hint in metavar for hint in PATH_HINTS):
        return "path"
    if (command, sub, action.dest) in FREE_TEXT or (command, "*", action.dest) in FREE_TEXT:
        return "free"
    return None


def place_of(places, used):
    """(the positional the word after USED words fills, its index in it) - one that takes several words takes
    every word after it - or (None, 0)."""
    for place in places:
        if place.nargs in ("*", "+", argparse.REMAINDER):
            return place, used
        if used == 0:
            return place, 0
        used -= 1
    return None, 0


def value_candidates(action, option):
    if action.choices:
        return [str(c) for c in action.choices]
    metavar = str(action.metavar or action.dest or "").upper()
    # (activity --home DIR is a folder; secrets unlock --for DURATION is a time, not a start)
    if option in VALUES and not any(hint in metavar for hint in PATH_HINTS) and "DURATION" not in metavar:
        return VALUES[option]()
    if action.type is Path or any(hint in metavar for hint in PATH_HINTS) or option in ("--workspace", "-w"):
        return [DIRS] if "DIR" in metavar or option in ("--workspace", "-w", "--state-dir", "--kit-dir") else [FILES]
    return []


COMMA_LISTS = ("--with",)  # options whose value is NAME,NAME... (the launcher's --with: companion_list)
BUILD_OPTIONS = ("-pu", "--podman-url", "-p", "--pin", "-u", "--uv", "-n", "--node", "-e", "--extension-only", "--variant")
# update: main.py's UPDATE_HELP (install.sh's options and the update mode's), and `update mode` (updates.py).
UPDATE_OPTIONS = ("--check", "--apply", "--install", "--rollback", "--while-running", "--scheduled", "--version",
                  "--source", "--force", "--allow-downgrade", "--user", "--system", "--prefix", "--uninstall", "--help")
UPDATE_MODE_OPTIONS = ("--at", "--source", "--json", "--help")
UPDATE_MODES = ("off", "notify", "download", "auto")


def variant_names():
    """The variants of your images (sandbox-ai images), as `sandbox-ai build` takes them (NAME or NAME@AGENT)."""
    def listed():
        images = module("images")
        found = images.variants()
        return sorted({images.shown_target(name, agent, found) for name, agent in found})
    return quietly(listed)


INTERNAL_COMMANDS = {"bridge-stub"}  # run by sandbox-ai itself (over SSH), not typed: never offered
# sandbox-ai's own options, before the command; --no-color and --color after any command too (main.colour_options)
GLOBAL_OPTIONS = ("-h", "--help", "-V", "--version", "--color", "--no-color", "-nc")
COLOUR_OPTIONS = ("--color", "--no-color")
COLOUR_CHOICES = ("auto", "always", "never")


def without_colour_options(done):
    """DONE (the words before the one completed) without sandbox-ai's own colour options before "--"."""
    kept, index = [], 0
    stop = done.index("--") if "--" in done else len(done)
    while index < len(done):
        word = done[index]
        if index < stop and (word in ("--no-color", "-nc") and not kept or word == "--no-color"
                             or word.startswith("--color=")):
            index += 1
            continue
        if index < stop and word == "--color" and index + 1 < len(done):
            index += 2
            continue
        kept.append(word)
        index += 1
    return kept


def complete(words):
    """Candidates for the last of WORDS (the words after `sandbox-ai`; the last may be empty)."""
    *done, current = words or [""]
    if done[-1:] == ["--color"] and ("--" not in done):
        return [choice for choice in COLOUR_CHOICES if choice.startswith(current)]
    if current.startswith("--color="):
        return [f"--color={choice}" for choice in COLOUR_CHOICES if choice.startswith(current[len("--color="):])]
    done = without_colour_options(done)
    if not done:
        if current.startswith("-"):
            return sorted(option for option in GLOBAL_OPTIONS if option.startswith(current))
        return sorted(c for c in {*commands(), *OTHER_COMMANDS} if c.startswith(current) and not c.startswith("_")
                      and c not in INTERNAL_COMMANDS)
    command = done[0]
    if command == "build":  # a script of its own (build_image.sh), no argparse parser: its options and targets
        if current.startswith("-"):
            return sorted(o for o in BUILD_OPTIONS if o.startswith(current))
        if len(done) > 1 and done[-1] in ("--pin", "-p", "--uv", "-u", "--node", "-n", "--podman-url", "-pu"):
            return []  # the value of an option: nothing to offer
        if "--variant" in done[1:] and done[-1] == "--variant":
            return [c for c in variant_names() if c.startswith(current)]
        return [c for c in (*AGENTS, "all", "team", "cairn", "vpn", "tor", "browser", *variant_names())
                if c.startswith(current)]
    if command == "update":  # (no argparse parser: install.sh takes most of its options)
        if done[1:2] == ["mode"]:
            if current.startswith("-"):
                return [o for o in UPDATE_MODE_OPTIONS if o.startswith(current)]
            return [m for m in UPDATE_MODES if m.startswith(current)] if len(done) == 2 else []
        if current.startswith("-"):
            return [o for o in UPDATE_OPTIONS if o.startswith(current)]
        return ["mode"] if len(done) == 1 and "mode".startswith(current) else []
    if command == "completion":
        if current.startswith("-"):
            return [o for o in ("--no-rc", "--help", *COLOUR_OPTIONS) if o.startswith(current)]
        if len(done) == 1:
            return [c for c in ("bash", "zsh", "fish", "install") if c.startswith(current)]
        given = [word for word in done[2:] if not word.startswith("-")]
        return [c for c in POSITIONS[("completion", "install")](len(given), given) if c.startswith(current)] \
            if done[1] == "install" else []
    parser = parser_of(command)
    if parser is None:
        return [FILES]
    sub, used, expecting, after_dashes, given = None, 0, None, False, []
    rest = done[1:]
    own = quietly(lambda: [OWN_PARSERS[(command, rest[0])]()]) if rest and (command, rest[0]) in OWN_PARSERS else []
    if own:
        parser, sub, rest = own[0], rest[0], rest[1:]
    for word in rest:
        if expecting is not None:
            expecting = None
            continue
        if after_dashes:
            continue
        if word == "--":
            after_dashes = True
            continue
        if word.startswith("-"):
            action = options(parser).get(word.split("=", 1)[0])
            if action is not None and action.nargs != 0 and "=" not in word and not isinstance(
                    action, (argparse._StoreTrueAction, argparse._StoreFalseAction, argparse._CountAction,
                             argparse._StoreConstAction, argparse._AppendConstAction, argparse._HelpAction)):
                expecting = action
            continue
        children = subparsers(parser)
        if children and word in children:
            parser, sub, used, given = children[word], word, 0, []
            continue
        used += 1
        given.append(word)
    if after_dashes:
        return [FILES]  # the agent's own arguments
    if expecting is not None:
        option = next(o for o in expecting.option_strings if o.startswith("--")) if any(
            o.startswith("--") for o in expecting.option_strings) else expecting.option_strings[0]
        if option in COMMA_LISTS and "," in current:  # AGENT,AGENT...: the next name after the last comma
            head, _, tail = current.rpartition(",")
            return [f"{head},{c}" for c in value_candidates(expecting, option)
                    if c not in (FILES, DIRS) and c.startswith(tail) and c not in head.split(",")]
        return [c for c in value_candidates(expecting, option) if c in (FILES, DIRS) or c.startswith(current)]
    if current.startswith("-"):
        if "=" in current:
            option, _, partial = current.partition("=")
            action = options(parser).get(option)
            return [f"{option}={c}" for c in (value_candidates(action, option) if action else [])
                    if c not in (FILES, DIRS) and c.startswith(partial)]
        return sorted(o for o in {*options(parser), *COLOUR_OPTIONS} if o.startswith(current))
    children = subparsers(parser)
    if children:
        return sorted(c for c in children if c.startswith(current))
    candidates = []
    place, index = place_of(positionals(parser), used)
    if (command, sub) in POSITIONS:
        candidates = quietly(lambda: POSITIONS[(command, sub)](index, given))
    elif place is not None and place.choices:
        candidates = [str(choice) for choice in place.choices]
    elif place is not None:
        candidates = named(command, sub, place.dest) or (value_candidates(place, place.dest) if index == 0 or
                                                         place.nargs in ("*", "+") else [])
    return [c for c in dict.fromkeys(candidates) if c in (FILES, DIRS) or c.startswith(current)]


# --- the shell scripts ------------------------------------------------------------------------------------

BASH = r'''# sandbox-ai completion for bash (sandbox-ai completion bash)
_sandbox_ai() {
    local current="${COMP_WORDS[COMP_CWORD]}" line
    local -a words=("${COMP_WORDS[@]:1:COMP_CWORD-1}" "$current")
    COMPREPLY=()
    while IFS= read -r line; do
        case "$line" in
            __files__) COMPREPLY+=($(compgen -f -- "$current")); compopt -o filenames 2>/dev/null ;;
            __dirs__) COMPREPLY+=($(compgen -d -- "$current")); compopt -o filenames 2>/dev/null ;;
            *) COMPREPLY+=("$line") ;;
        esac
    done < <(SANDBOX_AI_NO_DEPRECATION=1 command sandbox-ai __complete -- "${words[@]}" 2>/dev/null)
}
complete -o default -F _sandbox_ai sandbox-ai
'''

ZSH = r'''#compdef sandbox-ai
# sandbox-ai completion for zsh (sandbox-ai completion zsh)
_sandbox_ai() {
    local -a found words_before
    local line
    words_before=("${(@)words[2,CURRENT-1]}" "${words[CURRENT]}")
    found=("${(@f)$(SANDBOX_AI_NO_DEPRECATION=1 command sandbox-ai __complete -- "${words_before[@]}" 2>/dev/null)}")
    if (( ${found[(I)__dirs__]} )); then
        _files -/
    elif (( ${found[(I)__files__]} )); then
        _files
    elif (( ${#found} )) && [[ -n "${found[1]}" ]]; then
        compadd -Q -- "${found[@]}"
    fi
}
if [[ "${zsh_eval_context[-1]}" == loadautofunc ]]; then
    _sandbox_ai "$@"
else
    compdef _sandbox_ai sandbox-ai
fi
'''

FISH = r'''# sandbox-ai completion for fish (sandbox-ai completion fish)
function __sandbox_ai_complete
    set -l words (commandline -opc)[2..-1] (commandline -ct)
    for line in (env SANDBOX_AI_NO_DEPRECATION=1 sandbox-ai __complete -- $words 2>/dev/null)
        switch $line
            case __files__
                __fish_complete_path (commandline -ct)
            case __dirs__
                __fish_complete_directories (commandline -ct)
            case '*'
                echo $line
        end
    end
end
complete -c sandbox-ai -f -a '(__sandbox_ai_complete)'
'''

SCRIPTS = {"bash": BASH, "zsh": ZSH, "fish": FISH}


def data_home():
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")


def config_home():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def targets():
    """Where each shell finds them without a line in its rc file (zsh needs its fpath: install.sh adds it)."""
    return {"bash": data_home() / "bash-completion/completions/sandbox-ai",
            "zsh": data_home() / "zsh/site-functions/_sandbox-ai",
            "fish": config_home() / "fish/completions/sandbox-ai.fish"}


def install(shells=None, out=print):
    written = []
    for shell, path in targets().items():
        if shells and shell not in shells:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            raise ValueError(f"{path} is a symlink; remove it first")
        path.write_text(SCRIPTS[shell])
        written.append(path)
        out(f"{shell}: {path}")
    return written


# The rc files' block (bash, zsh): it loads the completion in every new shell - for a bash without the
# bash-completion package and a zsh whose fpath lacks the folder. install.sh's own block ("# >>> sandbox-ai >>>")
# does the same with the PATH: a file that has it gets no second one.
RC_BEGIN, RC_END = "# >>> sandbox-ai completion >>>", "# <<< sandbox-ai completion <<<"
INSTALL_BLOCK = "# >>> sandbox-ai >>>"


def rc_files(shells=None):
    """{shell: rc file} the block goes into: ~/.bashrc (on macOS only when there is one; bash reads ~/.bash_profile
    at login there), ~/.zshrc when there is one or zsh is your shell."""
    home = Path.home()
    found = {}
    if not shells or "bash" in shells:
        if sys.platform == "darwin":
            for name in (".bashrc", ".bash_profile"):
                if (home / name).is_file():
                    found["bash"] = home / name
                    break
        else:
            found["bash"] = home / ".bashrc"
    if (not shells or "zsh" in shells) and ((home / ".zshrc").is_file() or
                                            os.environ.get("SHELL", "").endswith("/zsh")):
        found["zsh"] = home / ".zshrc"
    return found


def rc_block():
    import shlex
    bash, zsh = targets()["bash"], targets()["zsh"]
    return (f"{RC_BEGIN}\n"
            "# added by sandbox-ai completion install (remove these lines to stop it)\n"
            f'if [ -n "${{BASH_VERSION:-}}" ] && [ -r {shlex.quote(str(bash))} ]; then\n'
            f"  complete -p sandbox-ai >/dev/null 2>&1 || . {shlex.quote(str(bash))}\n"
            f'elif [ -n "${{ZSH_VERSION:-}}" ] && [ -d {shlex.quote(str(zsh.parent))} ]; then\n'
            f"  fpath=({shlex.quote(str(zsh.parent))} $fpath)\n"
            f"  if typeset -f compdef >/dev/null 2>&1; then . {shlex.quote(str(zsh))}; fi\n"
            "fi\n"
            f"{RC_END}\n")


def add_rc_block(path):
    """The block at the end of the rc file PATH, written in place (a link into a dotfiles repository stays one); True
    when it was added - False when PATH has it, or install.sh's block, already."""
    try:
        text = path.read_text(encoding="utf-8", errors="surrogateescape")
    except FileNotFoundError:
        text = ""
    lines = {line.rstrip() for line in text.splitlines()}
    if RC_BEGIN in lines or INSTALL_BLOCK in lines:
        return False
    with open(path, "a", encoding="utf-8", errors="surrogateescape") as stream:
        stream.write(("\n" if text and not text.endswith("\n") else "") + rc_block())
    return True


def short(path):
    home = str(Path.home())
    return "~" + str(path)[len(home):] if str(path).startswith(home + "/") else str(path)


def running_shell_line(written):
    """What a terminal open now runs to have it: a program cannot change a shell that already runs."""
    shell = os.environ.get("SHELL", "").rsplit("/", 1)[-1]
    chosen = targets().get(shell) or targets()["bash"]
    if chosen not in written:
        chosen = written[0]
    return (f"In a terminal open now run: source {short(chosen)} (new terminals load it themselves; a program "
            "cannot change a shell that already runs)")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["__complete"]:
        words = argv[2:] if argv[1:2] == ["--"] else argv[1:]
        for candidate in complete(words):
            print(candidate)
        return 0
    parser = argparse.ArgumentParser(prog="sandbox-ai completion", description=__doc__.split("\n\n")[0])
    parser.add_argument("shell", choices=["bash", "zsh", "fish", "install"])
    parser.add_argument("only", nargs="*", default=[], help="with install: these shells only")
    parser.add_argument("--no-rc", action="store_true",
                        help="with install: leave ~/.bashrc and ~/.zshrc alone (install.sh keeps them itself)")
    a = parser.parse_intermixed_args(argv)  # (install --no-rc bash: the shells after the option too)
    if any(shell not in SCRIPTS for shell in a.only):
        parser.error("the shells are bash, zsh and fish")
    if a.shell == "install":
        try:
            written = install(a.only or None)
            added = [] if a.no_rc else [path for path in rc_files(a.only or None).values() if add_rc_block(path)]
        except (ValueError, OSError) as exc:
            print(f"sandbox-ai completion: {exc}", file=sys.stderr)
            return 1
        for path in added:
            print(f"{short(path)}: a marked block loads it in every new shell")
        if written:
            print(running_shell_line(written))
        return 0
    sys.stdout.write(SCRIPTS[a.shell])
    return 0


if __name__ == "__main__":
    sys.exit(main())
