#!/usr/bin/env python3
"""Interactive project setup: writes a trusted .sandbox-ai.json.

Every agent can use an LLM server registered with `sandbox-ai llm add` (local or
remote, with its key kept by the connection proxy): the wizard lists the
server's models and writes --llm NAME:MODEL. For OpenCode it can also configure
a local server (Ollama, LM Studio, llama.cpp or vLLM) directly: it lists the
server's models with size and load state, writes an OpenCode configuration that
uses only that server, and allows exactly its IP and port through the sandbox
firewall.
"""
import argparse
from collections import Counter
import getpass
import glob
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import socket
import sys
import tempfile
import urllib.request

HERE = Path(__file__).resolve().parent
SERVERS = {"ollama": ("Ollama", 11434), "lmstudio": ("LM Studio", 1234), "llamacpp": ("llama.cpp", 8080),
           "vllm": ("vLLM", 8000)}
def network_choices():
    """Network answers: "default" plus the launcher's own profiles."""
    return ["default", *launcher().PROFILES]


def network_help(mode="medium"):
    network = launcher().MODES.MODES[mode].get("network")
    default = (f"what the security mode {mode} opens ({shlex.join(network)})" if network
               else "the agent's own provider only")
    return f"default: {default}; " + "; ".join(
        f"{name}: {value['help']}" for name, value in launcher().PROFILES.items())


def network_options(choice):
    return [] if choice == "default" else ["--profile", choice]


def inherited_mode():
    """The security mode a project file without "mode" gets: yours from defaults.json, else medium, never below
    the administrator's min_mode."""
    l = launcher()
    try:
        defaults = l.load_user_defaults(l.parse_args(["codex", "--no-config"]))
        policy = l.load_policy()
        return l.MODES.resolve(None, None, None, defaults, l.defaults_file(), policy, l.POLICY_FILE)[0]
    except (OSError, ValueError, SystemExit):
        return l.MODES.DEFAULT


def mode_of(answers):
    return answers.get("mode") or inherited_mode()


def mode_refusal(answers, *options):
    """Why the security mode of ANSWERS refuses OPTIONS for its agent and backend, or None."""
    l = launcher()
    mode = mode_of(answers)
    if mode not in l.MODES.STRICT:
        return None  # lax, low and medium refuse nothing
    try:
        return l.MODES.refusal(l.launcher_namespace(), answers["agent"],
                               ["--backend", answers["backend"], *options], mode)
    except SystemExit:
        return "The launcher does not accept these options"


def ask_mode(p, answers):
    """The security mode, asked first: Enter keeps the inherited one (no "mode" is written then)."""
    l = launcher()
    inherited, floor = inherited_mode(), l.load_policy().get("min_mode")
    p.out("  Security modes (options can only narrow the chosen one):")
    for line in l.MODES.summaries():
        p.out(f"    {line}")
    choices = list(l.MODES.NAMES[l.MODES.rank(floor) if floor else 0:])
    chosen = p.choose("Security mode", choices, answers.get("mode") or inherited)
    answers["mode"] = None if chosen == inherited and not answers.get("mode") else chosen
# Model ids as servers report them, e.g. "qwen2.5-coder:7b-instruct-q4_K_M" or "org/Model-GGUF".
MODEL_NAME = re.compile(r"[\w.:/@+-]{1,200}")
_launcher = None
_llm = None


def llm():
    """sandbox/llm.py: registered LLM endpoints and the model listing of every server kind."""
    global _llm
    if _llm is None:
        spec = importlib.util.spec_from_file_location("sandbox_llm_for_wizard", HERE / "llm.py")
        _llm = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_llm)
    return _llm


def launcher():
    global _launcher
    if _launcher is None:
        spec = importlib.util.spec_from_file_location("agent_sandbox_for_wizard", HERE / "agent_sandbox.py")
        _launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_launcher)
    return _launcher


def fetch_json(url, timeout=5):
    # A local server must never be reached through an HTTP proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=timeout) as response:
        return json.load(response)


def url_host(host):
    return f"[{host}]" if ":" in host else host


def local_config_path(project):
    """Where the OpenCode config for PROJECT lives: outside every workspace, so the agent
    cannot swap it for a symlink to a host file that the launcher would then copy in."""
    config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    digest = hashlib.sha256(os.fsencode(str(project))).hexdigest()[:12]
    return config / "sandbox-ai" / "opencode" / f"{project.name or 'root'}-{digest}.json"


def clean_models(models):
    """Server answers are untrusted: keep well-typed entries, make text terminal-safe."""
    return llm().clean_models(models)


def discover(kind, host, port, fetch=fetch_json):
    """Models offered by a local server: [{name, size, loaded, details}]; unknown values are None."""
    return clean_models(discover_raw(kind, host, port, fetch))


def discover_raw(kind, host, port, fetch=fetch_json):
    if kind not in SERVERS:
        raise ValueError(f"Unknown server type: {kind}")
    base = f"http://{url_host(host)}:{port}"
    try:
        return llm().discover_raw(kind, lambda path: fetch(base + path))
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f"Cannot reach {kind} at {host}:{port}: {launcher().printable(exc)}") from None


def sort_models(models, key):
    return llm().sort_models(models, key)


def format_size(value):
    return llm().format_size(value)


def model_table(models):
    return llm().model_table(models)


def opencode_config(kind, host, port, model):
    """OpenCode configuration that uses only the local server."""
    return {"$schema": "https://opencode.ai/config.json",
            "provider": {kind: {"npm": "@ai-sdk/openai-compatible",
                                "name": f"{SERVERS[kind][0]} ({host}:{port})",
                                "options": {"baseURL": f"http://{url_host(host)}:{port}/v1"},
                                "models": {model: {"name": model}}}},
            "model": f"{kind}/{model}", "small_model": f"{kind}/{model}",
            "enabled_providers": [kind], "share": "disabled", "autoupdate": False}


def build_document(answers):
    options = [] if answers["backend"] == "podman" else ["--backend", answers["backend"]]
    if answers.get("llm"):
        options += ["--llm", answers["llm"]] + (["--no-provider-defaults"] if answers.get("llm_only") else [])
    elif answers["local"]:
        local = answers["local"]
        target = local.get("target") or f"{url_host(local['host'])}:{local['port']}"
        options += ["--local-config", local["config"], "--allow-tcp", target]
    else:
        options += network_options(answers["network"])
    options += answers.get("subnet") or []
    options += ["--dangerously-skip-permissions"] if answers["danger"] else []
    vnc = any(name == "--gui-vnc" for name, *_ in option_groups(answers.get("extra") or []))
    options += ["--gui"] if answers["gui"] and not vnc else []  # --gui-vnc implies it
    # tmux is the launcher's default for interactive starts; only switching it off is written down.
    # An explicit --tmux of an edited file (insist, fail without a terminal) is kept as it was.
    options += (["--tmux"] if answers.get("tmux_forced") else []) if answers["tmux"] else ["--no-tmux"]
    options += ["--log-dir", answers["log_dir"]] if answers["log_dir"] else []
    # Optional blocks; every entry is already a list of launcher arguments.
    for key in ("network_extra", "git", "kits", "extra"):
        options += answers.get(key) or []
    document = {"version": 1, "agent": answers["agent"], "options": options, "workspaces": answers["workspaces"]}
    if answers.get("mode"):
        document["mode"] = answers["mode"]
    if "keep" in answers:
        # An edited configuration keeps its description, agent arguments and MCP definitions.
        document.update(answers["keep"])
    else:
        document["description"] = "Created by sandbox-wizard"
    return document


def split_list(text):
    return [value.strip() for value in text.split(",") if value.strip()]


def ask_list(p, question, check, default=None):
    """Comma-separated answer; CHECK raises ValueError for an invalid entry, which is asked again.
    With a DEFAULT (the current values when editing), Enter keeps it and "-" empties the list."""
    if default:
        question += " ('-' for none)"
    while True:
        answer = p.ask(question, default)
        if answer.strip() == "-":
            return []
        values = split_list(answer)
        try:
            for value in values:
                check(value)
        except ValueError as exc:
            p.out(f"  {launcher().printable(exc)}")
            continue
        return values


def check_name(value):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError(f"Not a variable name: {value!r}")


def check_launcher(agent, *options):
    """Let the launcher's own parser and checks judge OPTIONS."""
    launcher().validate(parse_options(agent, list(options)))


def parse_options(agent, options):
    try:
        return launcher().parse_args([agent, *options])
    except SystemExit:  # argparse has printed the reason
        raise ValueError("The launcher does not accept these options") from None


def option_groups(options):
    """OPTIONS (a list as on the command line) as [(name, value or None, tokens)] by the launcher's own
    parser: "--x=v" counts as ("--x", "v"); tokens it does not know stay single entries."""
    actions = launcher().build_parser()._option_string_actions
    options = launcher().canonical_options(list(options))  # short forms as their long names
    groups, index = [], 0
    while index < len(options):
        token = options[index]
        name, equals, value = token.partition("=")
        action = actions.get(name if token.startswith("--") else token)
        if action is None or equals and action.nargs == 0:
            groups.append((token, None, [token]))
        elif equals:
            groups.append((name, value, [token]))
        elif action.nargs == 0 or index + 1 >= len(options):
            groups.append((token, None, [token]))
        else:
            groups.append((token, options[index + 1], [token, options[index + 1]]))
            index += 1
        index += 1
    return groups


def flat(name, value):
    return [name] if value is None else [name, value]


NETWORK_VALUES = ("--allow-host", "--allow-tcp", "--allow", "--allow-host-port")
GIT_OPTIONS = ("--git-token-file", "--git-token-env", "--git-token-secret", "--git-credentials", "--token-proxy")
EXTRA_VALUES = ("--state-dir", "--mount", "--env", "--mcp", "--gui-screen", "--gui-vnc")
KIT_VALUES = ("--kit", "--kit-dir")
KIT_FLAGS = ("--no-default-kit", "--no-memory", "--no-plugins")
SUBNET_OPTIONS = ("--subnet", "--subnet6")


def answers_from_document(document):
    """Wizard answers for an existing configuration; options the wizard does not ask about are kept
    under "extra" as they are. Returns (answers, notes about anything dropped)."""
    l = launcher()
    answers = {"agent": document["agent"], "backend": "podman", "workspaces": list(document.get("workspaces", [])),
               "network": "default", "local": None, "llm": None, "llm_only": False, "danger": False, "gui": False, "tmux": True, "log_dir": None,
               "network_extra": [], "git": [], "kits": [], "extra": [], "subnet": [],
               "mode": document.get("mode") if document.get("mode") in l.MODES.NAMES else None,
               "keep": {key: document[key] for key in ("description", "agent_args", "mcp_servers") if key in document}}
    presets = {f"--allow-{name}" for name in l.PRESETS}
    groups, notes, index = option_groups(document.get("options", [])), [], 0
    while index < len(groups):
        name, value, tokens = groups[index]
        following = groups[index + 1] if index + 1 < len(groups) else (None, None, [])
        if any(name == flag or name.startswith(flag + "=") for flag in l.CONFIG_FORBIDDEN):
            notes.append(f"dropped {shlex.join(tokens)}: not allowed in a project configuration")
        elif name == "--backend" and value in ("podman", "bwrap"):
            answers["backend"] = value
        elif name == "--profile" and value in l.PROFILES and answers["network"] == "default":
            answers["network"] = value
        elif (name == "--local-config" and value and answers["local"] is None and following[0] == "--allow-tcp"
              and following[1]):
            answers["local"] = {"config": value, "target": following[1]}
            index += 1
        elif name == "--llm" and value and answers["llm"] is None:
            answers["llm"] = value
        elif name == "--no-provider-defaults" and not answers["llm_only"]:
            answers["llm_only"] = True  # kept with --llm; otherwise moved to the further options below
        elif name == "--dangerously-skip-permissions":
            answers["danger"] = True
        elif name in ("--gui", "--tmux", "--no-tmux"):
            answers[name.lstrip("-").replace("no-", "")] = name != "--no-tmux"
            if name != "--gui":
                answers["tmux_forced"] = name == "--tmux"
        elif name == "--log-dir" and value:
            answers["log_dir"] = value
        elif name in SUBNET_OPTIONS and value is not None and name not in answers["subnet"]:
            answers["subnet"] += [name, value]
        elif name in presets and value is None or name in NETWORK_VALUES and value is not None:
            answers["network_extra"] += flat(name, value)
        elif name == "--git-identity" or name in GIT_OPTIONS and value is not None:
            answers["git"] += flat(name, value)
        elif name in KIT_VALUES and value is not None or name in KIT_FLAGS and value is None:
            answers["kits"] += flat(name, value)
        elif name in EXTRA_VALUES and value is not None or name == "--workspace-ro":
            answers["extra"] += flat(name, value)
        else:
            answers["extra"] += tokens
        index += 1
    if any(name == "--gui-vnc" for name, *_ in option_groups(answers["extra"])):
        answers["gui"] = True  # --gui-vnc implies --gui
    if answers["llm_only"] and not answers["llm"]:
        answers["extra"], answers["llm_only"] = ["--no-provider-defaults", *answers["extra"]], False
    if (answers["local"] or answers["llm"]) and answers["network"] != "default":
        # With an LLM server the wizard asks no network question: keep the profile as a further option.
        answers["extra"] = ["--profile", answers["network"], *answers["extra"]]
        answers["network"] = "default"
    return answers, notes


def git_remote_origin(project):
    """http(s)://[USER@]HOST[:PORT] of the project's origin remote, or None.

    The repository is agent-writable, so its config is parsed as text, never run
    through git, and the result is only offered as a default."""
    path = Path(project) / ".git" / "config"
    try:
        text = launcher().read_config_bytes(path, 1 << 16).decode("utf-8", "replace")
    except (OSError, ValueError):
        return None
    section = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            section = line
        elif section == '[remote "origin"]' and re.match(r"url\s*=", line):
            match = re.match(r"url\s*=\s*(https?://(?:[A-Za-z0-9_.-]+@)?[A-Za-z0-9.-]+(?::[0-9]{1,5})?)(?:/|$)",
                             line)
            return match[1] if match else None
    return None


def ask_network_extra(p, a, current=()):
    """Presets, host names, TCP targets and host loopback ports; CURRENT (options) gives the defaults."""
    presets = list(launcher().PRESETS)
    now = {"presets": [], **{name: [] for name in NETWORK_VALUES}}
    for name, value, _ in option_groups(list(current)):
        if value is None and name[len("--allow-"):] in presets:
            now["presets"].append(name[len("--allow-"):])
        elif name in now and value is not None:
            now[name].append(value)

    def default(key):
        return ", ".join(now[key]) or None
    options = []

    def check_preset(value):
        if value not in presets:
            raise ValueError(f"Unknown preset {value!r}; choose from {', '.join(presets)}")
    for value in ask_list(p, f"Host presets, comma-separated ({', '.join(presets)})", check_preset,
                          default("presets")):
        options.append(f"--allow-{value}")
    for value in ask_list(p, "Further HTTPS host names, comma-separated (--allow-host)",
                          lambda v: check_launcher(a["agent"], "--allow-host", v), default("--allow-host")):
        options += ["--allow-host", value]
    for value in ask_list(p, "TCP targets IP:PORT on other machines, e.g. a LAN server, comma-separated (--allow-tcp)",
                          lambda v: check_launcher(a["agent"], "--allow-tcp", v), default("--allow-tcp")):
        options += ["--allow-tcp", value]
    for value in ask_list(p, "Whole addresses or networks IP or CIDR, all ports and protocols, comma-separated "
                             "(--allow)", lambda v: check_launcher(a["agent"], "--allow", v), default("--allow")):
        options += ["--allow", value]
    if a["backend"] == "podman":
        for value in ask_list(p, "Ports on this host's loopback, comma-separated, e.g. a local Git server "
                                 "(--allow-host-port)", lambda v: check_launcher(a["agent"], "--allow-host-port", v),
                              default("--allow-host-port")):
            options += ["--allow-host-port", value]
    elif now["--allow-host-port"]:
        p.out("  Dropped --allow-host-port: ports on this host's loopback need the Podman backend")
    return options


def token_store():
    """Private directory for tokens entered in the wizard; outside every workspace."""
    config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return config / "sandbox-ai" / "tokens"


def token_path(label):
    """File for the token of LABEL (github.com or a Git server URL)."""
    return token_store() / re.sub(r"[^A-Za-z0-9.-]+", "-", label).strip("-")


def write_token(path, token):
    """Write TOKEN to PATH with mode 0600, atomically and never through a symlink."""
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    refuse_symlink(path)
    fd, temporary = tempfile.mkstemp(prefix=".token-", dir=path.parent)
    with os.fdopen(fd, "w") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(token + "\n")
    os.replace(temporary, path)


def ask_token(p, label, pattern):
    """A token typed (or pasted) without echo; empty skips. Never shown, not even in errors."""
    while True:
        token = p.secret(f"Token for {label} (input hidden, empty to skip): ").strip()
        if not token or re.fullmatch(pattern, token):
            return token or None
        p.out("  That does not look like an access token (one line of letters, digits and _.~+/=-); try again")


def ask_variable(p, question, default):
    while True:
        name = p.ask(question, default)
        try:
            check_name(name)
        except ValueError as exc:
            p.out(f"  {exc}")
            continue
        if name not in os.environ:
            p.out(f"  Note: {name} is not set now; export it before starting the agent.")
        return name


def ask_secret_name(p, label, default):
    """The name of a secret kept with `sandbox-ai secrets set NAME` that holds the token for LABEL."""
    while True:
        name = p.ask(f"Name of the secret with the token for {label} (keep it with: sandbox-ai secrets set NAME)",
                     default)
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", name or ""):
            return name
        p.out("  A secret name has letters, digits and _, and does not start with a digit")


def git_credentials_path():
    """git's own credential store, offered for --git-credentials when it exists. The launcher reads
    no symlink, so a link (as dotfile managers make) is offered as the file it points to."""
    path = Path.home() / ".git-credentials"
    return Path(os.path.realpath(path)) if path.is_symlink() else path


def token_entry(name, value):
    """(URL, option, description) of an existing --git-token-* option."""
    kind = name.rsplit("-", 1)[1]
    try:
        url = launcher().git_token_credential(value, kind)["url"]
    except ValueError:
        url = value.partition("=")[0]
    reference = value.partition("=")[2]
    return url, [name, value], {"file": f"file {reference}", "secret": f"secret {reference}"}.get(
        kind, f"host variable {reference}")


def ask_git(p, a, project, pending=None, current=None):
    """Commit identity and push tokens. A token typed here goes to a private file (mode 0600) under
    ~/.config/sandbox-ai/tokens, never into the project configuration, which only names that file.
    PENDING collects (path, token) to write once the configuration is confirmed.
    CURRENT (options) when editing: its identity is the default and each of its tokens can be kept."""
    pending = [] if pending is None else pending
    existing = option_groups(list(current or []))
    identity = any(name == "--git-identity" for name, *_ in existing) if current is not None else True
    options = ["--git-identity"] if p.yes("Commit as your host's git user.name and user.email (--git-identity)",
                                          default=identity) else []
    tokens = [token_entry(name, value) for name, value, _ in existing
              if name in GIT_OPTIONS and name not in ("--git-credentials", "--token-proxy") and value is not None]
    proxied = any(name == "--token-proxy" for name, *_ in existing)
    store = next((value for name, value, _ in existing if name == "--git-credentials" and value), None)
    if a["network"] == "offline":
        if tokens or store:
            p.out("  Offline: the push tokens and the Git credentials file are dropped")
        return options
    if mode_refusal(a, "--allow-github"):
        p.out(f"  The security mode {mode_of(a)} opens no Git server: no push tokens"
              + (" (the tokens and the Git credentials file are dropped)" if tokens or store else ""))
        return options
    # Every login in the file reaches the agent, so a new configuration asks with "no" as default.
    if store or git_credentials_path().is_file():
        found = git_credentials_path()
        shown = store or ("~/" + str(found.relative_to(Path.home())) if Path.home() in found.parents else str(found))
        if p.yes(f"Use the Git logins in {launcher().printable(shown)} for Git over HTTP(S) "
                 "(--git-credentials; the agent can read every login in it, a token below wins)",
                 default=store is not None):
            options += ["--git-credentials", shown]
    done = set()  # URLs that have a token already: one token per URL
    for url, option, description in tokens:
        if url not in done and p.yes(f"Keep the token for {launcher().printable(url)} "
                                     f"({launcher().printable(description)})", default=True):
            options += option
            done.add(url)
    if "https://github.com" in done:
        how = "none"
    else:
        how = p.choose("Token for github.com (for your own Git server, e.g. a local Gitea, see further below): "
                       "enter it now, use a host variable, a secret of `sandbox-ai secrets`, or none",
                       ["none", "enter", "variable", "secret"], "none")
    if how == "enter":
        token = ask_token(p, "github.com", launcher().GIT_TOKEN)
        if token:
            pending.append((str(token_path("github.com")), token))
            options += ["--git-token-file", f"https://github.com={token_path('github.com')}"]
            done.add("https://github.com")
    elif how == "variable":
        default = next((n for n in ("SANDBOX_GH_TOKEN", "GH_TOKEN", "GITHUB_TOKEN") if n in os.environ),
                       "SANDBOX_GH_TOKEN")
        name = ask_variable(p, "Host variable with the github.com token (the agent sees it as GH_TOKEN)", default)
        options += ["--git-token-env", f"https://github.com={name}"]
        done.add("https://github.com")
    elif how == "secret":
        options += ["--git-token-secret", f"https://github.com={ask_secret_name(p, 'github.com', 'GH_PUSH')}"]
        done.add("https://github.com")
    # The origin comes from the agent-writable .git/config: offer it only behind an explicit yes
    # (default no), never as a default value that an empty answer would accept.
    origin = git_remote_origin(project)
    try:
        origin_url = launcher().git_token_credential(f"{origin}=CHECK")["url"] if origin else None
    except ValueError:
        origin_url = None
    offered = bool(origin) and origin_url not in done and p.yes(f"Add a push token for this project's origin {origin}")
    while True:
        if offered:
            url, offered = origin, False
        else:
            url = p.ask("Your own Git server (Gitea, Forgejo, GitLab ...) for a token, e.g. https://git.example.org, "
                        "or a Git credentials file (/path or ~/path, git's store format), empty to finish")
        if not url:
            break
        if url.startswith(("/", "~")):
            # git's store format (https://USER:TOKEN@HOST per line, mode 0600): every login in it reaches the agent.
            found = Path(url).expanduser()
            if "--git-credentials" in options:
                p.out("  A Git credentials file is set already")
            elif not found.is_file() or found.is_symlink():
                p.out(f"  {launcher().printable(url)} is not a regular file")
            else:
                options += ["--git-credentials", url]
                p.out("  The agent can read every login in that file; a token given here wins for its server")
            continue
        try:
            credential = launcher().git_token_credential(f"{url}=CHECK")
            if credential["loopback"] and a["backend"] != "podman":
                raise ValueError("A server on this host's loopback is reachable only with the Podman backend")
        except ValueError as exc:
            p.out(f"  {launcher().printable(exc)}")
            continue
        if credential["url"] in done:
            p.out(f"  {credential['url']} has a token already")
            continue
        done.add(credential["url"])
        how = p.choose(f"Token for {url}: enter it now, use a host variable or a secret",
                       ["enter", "variable", "secret"], "enter")
        if how == "enter":
            token = ask_token(p, url, launcher().GIT_TOKEN)
            if not token:
                continue
            path = str(token_path(credential["url"]))
            pending.append((path, token))
            options += ["--git-token-file", f"{url}={path}"]
        elif how == "secret":
            options += ["--git-token-secret", f"{url}={ask_secret_name(p, url, 'GIT_PUSH')}"]
        else:
            name = ask_variable(p, f"Host variable with the token for {url}", "SANDBOX_GIT_TOKEN")
            options += ["--git-token-env", f"{url}={name}"]
    # Tokens of HTTPS servers can stay with the connection proxy, out of the agent's reach.
    chosen = [value for name, value, _ in option_groups(options)
              if name in ("--git-token-file", "--git-token-env", "--git-token-secret") and value]
    if chosen and all(value.startswith("https://") for value in chosen) and "--git-credentials" not in options:
        if p.yes("Keep these tokens out of the sandbox: the connection proxy adds them to Git's requests, the agent "
                 "never reads them (--token-proxy; no GH_TOKEN for gh then)", default=proxied or current is None):
            options.append("--token-proxy")
    return options


def load_kits():
    spec = importlib.util.spec_from_file_location("sandbox_kits_for_wizard", HERE / "kits.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def kit_list(k, agent, names):
    """NAMES for a question; the kits that come with sandbox-ai are marked."""
    shipped = k.builtin_kits(agent)
    return ", ".join(f"{name} (built-in)" if name in shipped else name for name in names) or "none"


def ask_kits(p, agent, current=None):
    """Kits and project memory. CURRENT (options) when editing; its choices are the defaults."""
    k, l = load_kits(), launcher()
    groups = option_groups(list(current or []))
    chosen = [value for name, value, _ in groups if name == "--kit" and value]
    dirs = [value for name, value, _ in groups if name == "--kit-dir" and value]
    flags = {name for name, value, _ in groups if name in KIT_FLAGS and value is None}
    home = k.default_home(agent)
    try:
        content = k.describe(agent, "home", home) if home.is_dir() else "created with examples on the first start"
    except (OSError, ValueError) as exc:
        content = f"cannot be used: {exc}"
    options = []
    if not p.yes(f"Use the default kit {l.printable(str(home))} ({l.printable(content)})",
                 "--no-default-kit" not in flags):
        options.append("--no-default-kit")
    available = k.available_kits(agent)
    if available or chosen:
        def check(value):
            if value not in available:
                raise ValueError(f"No kit {value!r} for {agent}; available: {', '.join(available) or 'none'}")
        for name in ask_list(p, f"Further kits, comma-separated (available for {agent}: "
                                f"{kit_list(k, agent, available)})", check, ", ".join(chosen) or None):
            options += ["--kit", name]
    for value in dirs:
        if p.yes(f"Keep the kit directory {l.printable(value)} (--kit-dir)", True):
            options += ["--kit-dir", value]
    if not p.yes("Keep the agent's memory for this project between runs (project memory)",
                 "--no-memory" not in flags):
        options.append("--no-memory")
    if agent != "codex" and "--no-plugins" in flags and p.yes("Keep plugins switched off (--no-plugins)", True):
        options.append("--no-plugins")
    return options


def ask_new_kits(p, agent):
    """A new configuration gets the default kit and project memory without asking (the launcher's
    default); further kits are offered when the user has some (the built-in ones are then offered too,
    else only named). Everything else: edit menu, part k."""
    k, l = load_kits(), launcher()
    home = k.default_home(agent)
    try:
        content = k.describe(agent, "home", home) if home.is_dir() else "created with examples on the first start"
    except (OSError, ValueError) as exc:
        content = f"cannot be used: {exc}"
    p.out(f"  Kit: {l.printable(str(home))} ({l.printable(content)}); project memory on. "
          "Change both later with: sandbox-ai wizard edit, part k")
    available = k.available_kits(agent)
    if not k.available_kits(agent, builtin=False):
        if available:
            p.out(f"  Built-in kits for {agent}: {', '.join(available)}; choose them later with: sandbox-ai wizard "
                  "edit, part k (or --kit NAME)")
        return []
    def check(value):
        if value not in available:
            raise ValueError(f"No kit {value!r} for {agent}; available: {', '.join(available)}")
    names = ask_list(p, f"Further kits, comma-separated (available for {agent}: {kit_list(k, agent, available)})",
                     check)
    return [token for name in names for token in ("--kit", name)]


def ask_extra(p, a, project, state, current=None):
    """Everything else: persistent HOME, mounts, variables, MCP, display details, free options.
    CURRENT (options) when editing gives the defaults; what the wizard does not ask about is offered
    as the free options line."""
    now = {"ro": False, "--state-dir": [], "--mount": [], "--env": [], "--mcp": [], "--gui-screen": [],
           "--gui-vnc": [], "rest": []}
    for name, value, tokens in option_groups(list(current or [])):
        if name == "--workspace-ro":
            now["ro"] = True
        elif name in now and value is not None:
            now[name].append(value)
        else:
            now["rest"] += tokens

    def default(key):
        return ", ".join(now[key]) or None
    if "--workspace-ro" in launcher().MODES.MODES[mode_of(a)].get("flags", []):
        p.out(f"  The security mode {mode_of(a)} mounts the workspaces read-only")
        options = []
    else:
        options = ["--workspace-ro"] if p.yes("Mount the workspaces read-only (--workspace-ro)", now["ro"]) else []
    digest = hashlib.sha256(os.fsencode(str(project))).hexdigest()[:12]
    if p.yes("Keep the sandbox HOME between runs (--state-dir; cannot be combined with --session)",
             bool(now["--state-dir"])):
        options += ["--state-dir", now["--state-dir"][-1] if now["--state-dir"] else
                    str(state / "sandbox-ai" / "home" / f"{project.name or 'root'}-{digest}")]
    def check_mount(value):
        launcher().parse_mount(value)
        refused = mode_refusal(a, "--mount", value)
        if refused:
            raise ValueError(refused)
    for value in ask_list(p, "Extra mounts SRC:DEST:ro|rw, comma-separated, DEST under /mnt or /opt (--mount)",
                          check_mount, default("--mount")):
        options += ["--mount", value]
    for value in ask_list(p, "Host variables passed to the agent, comma-separated (--env)", check_name,
                          default("--env")):
        options += ["--env", value]
    servers = sorted(load_mcp().definitions())

    def check_server(value):
        if value not in servers:
            raise ValueError(f"Unknown MCP server {value!r}; defined: {', '.join(servers)}")
        refused = mode_refusal(a, "--mcp", value)
        if refused:
            raise ValueError(refused)
    for value in ask_list(p, f"MCP servers, comma-separated ({', '.join(servers)}; --mcp)", check_server,
                          default("--mcp")):
        options += ["--mcp", value]
    if a["gui"]:
        current_screen = now["--gui-screen"][-1] if now["--gui-screen"] else "1920x1080x24"
        screen = p.ask("Virtual screen size (--gui-screen)", current_screen)
        while not re.fullmatch(r"[0-9]{2,5}x[0-9]{2,5}x(8|16|24|32)", screen):
            p.out("  Please enter WIDTHxHEIGHTxDEPTH, e.g. 1920x1080x24")
            screen = p.ask("Virtual screen size (--gui-screen)", current_screen)
        options += [] if screen == "1920x1080x24" else ["--gui-screen", screen]
        if p.yes("Serve the display over VNC (--gui-vnc)", bool(now["--gui-vnc"])):
            options += ["--gui-vnc", now["--gui-vnc"][-1] if now["--gui-vnc"] else
                        str(state / "sandbox-ai" / "vnc" / f"{project.name or 'root'}-{digest}")]
    elif now["--gui-screen"] or now["--gui-vnc"]:
        p.out("  Dropped the display options: the virtual display is off")
    while True:
        text = p.edit("Any other launcher options, as on the command line (see --help), empty for none",
                      shlex.join(now["rest"]))
        try:
            values = check_free_options(a["agent"], text)
            refused = mode_refusal(a, *values) if values else None
            if refused:
                raise ValueError(refused)
        except ValueError as exc:
            p.out(f"  {launcher().printable(exc)}")
            continue
        return options + values


def check_free_options(agent, text):
    """Launcher options typed as one command line: allowed in a project file, accepted by the launcher,
    with absolute paths only."""
    values = launcher().canonical_options(shlex.split(text))  # short forms are checked (and saved) as long ones
    forbidden = launcher().CONFIG_FORBIDDEN
    bad = [v for v in values if any(v == flag or v.startswith(flag + "=") for flag in forbidden)]
    if bad:
        raise ValueError(f"Not allowed in a project configuration: {' '.join(bad)}")
    if values:
        parse_options(agent, values)
    relative = launcher().relative_paths(values)
    if relative:
        raise ValueError(f"Use absolute paths (or ~/...), not: {', '.join(relative)}")
    return values


_mcp = None


def load_mcp():
    global _mcp
    if _mcp is None:
        spec = importlib.util.spec_from_file_location("sandbox_mcp_for_wizard", HERE / "mcp.py")
        _mcp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_mcp)
    return _mcp


def refuse_symlink(path):
    # Project directories are agent-writable: a planted link must not redirect our writes.
    if Path(path).is_symlink():
        raise ValueError(f"{path} is a symlink; refusing to write through it")


def write_file(path, text):
    """Replace PATH atomically; never follows a symlink at PATH."""
    refuse_symlink(path)
    fd, temporary = tempfile.mkstemp(prefix=".sandbox-ai-", dir=Path(path).parent)
    with os.fdopen(fd, "w") as stream:
        os.fchmod(stream.fileno(), 0o644)
        stream.write(text)
    os.replace(temporary, path)


def lan_address():
    """This host's outward-facing address; no packet is sent."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))
            return probe.getsockname()[0]
    except OSError:
        return None


def complete_path(text, state):
    """readline completer: file names for TEXT, keeping a leading ~ as typed; directories end in /."""
    expanded = os.path.expanduser(text)
    matches = sorted(glob.glob(glob.escape(expanded) + "*"))
    if text.startswith("~"):
        home = os.path.expanduser(text.split("/", 1)[0])
        matches = [text.split("/", 1)[0] + match[len(home):] for match in matches]
    matches = [match + "/" if os.path.isdir(os.path.expanduser(match)) else match for match in matches]
    return matches[state] if state < len(matches) else None


def enable_line_editing():
    """GNU readline for every prompt: Backspace (both ^H and ^?), arrow keys, Home/End, Ctrl-A/E/W/U,
    history with Up/Down, and Tab completion of paths. Only on a terminal; True if enabled."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False
    try:
        import readline
    except ImportError:
        return False
    # Paths are separated by commas in some answers; complete each one.
    readline.set_completer_delims(" \t\n,")
    readline.set_completer(complete_path)
    if "libedit" in (readline.__doc__ or ""):
        readline.parse_and_bind("bind ^I rl_complete")
    else:
        readline.parse_and_bind("tab: complete")
        # Both erase keys, whatever the terminal sends and stty says.
        readline.parse_and_bind(r'"\C-h": backward-delete-char')
        readline.parse_and_bind(r'"\C-?": backward-delete-char')
    global LINE_EDITING
    LINE_EDITING = True
    return True


LINE_EDITING = False


def prefilled_input(prompt, text):
    """input() with TEXT already on the line, to be edited in place (GNU readline)."""
    import readline
    readline.set_startup_hook(lambda: readline.insert_text(text))
    try:
        return input(prompt)
    finally:
        readline.set_startup_hook()


class Prompter:
    def __init__(self, prompt, out, secret=None, prefill=None):
        self.prompt, self.out = prompt, out
        self.secret = secret or getpass.getpass
        self.prefill = prefill

    def edit(self, question, text):
        """A line that starts as TEXT: in a terminal it is shown ready to edit, otherwise TEXT is the
        default; "-" empties it."""
        if self.prefill and text:
            answer = self.prefill(f"{question}: ", text).strip()
        else:
            answer = self.ask(f"{question}{' (- for none)' if text else ''}", text or None)
        return "" if answer == "-" else answer

    def ask(self, question, default=None):
        answer = self.prompt(f"{question}{f' [{default}]' if default else ''}: ").strip()
        return answer or default or ""

    def choose(self, question, choices, default=None):
        while True:
            answer = self.ask(f"{question} ({'/'.join(choices)})", default)
            if answer in choices:
                return answer
            self.out(f"  Please choose one of: {', '.join(choices)}")

    def yes(self, question, default=False):
        while True:
            answer = self.ask(f"{question} [{'Y/n' if default else 'y/N'}]").lower()
            if not answer:
                return default
            if answer in ("y", "yes", "j", "ja", "n", "no", "nein"):
                return answer in ("y", "yes", "j", "ja")
            self.out("  Please answer y or n")


def ask_local_llm(p, fetch):
    kind = p.choose("Server type", list(SERVERS))
    while True:
        host = p.ask("Server IP address (the sandbox cannot reach this host's localhost)", lan_address())
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            p.out("  Please enter an IP address")
            continue
        if address.is_loopback:
            p.out("  Warning: a loopback address is unreachable from inside the sandbox. Bind the server to the "
                  "LAN (e.g. OLLAMA_HOST=0.0.0.0, LM Studio 'Serve on Local Network', vllm --host 0.0.0.0) "
                  "and use this host's LAN IP.")
            if not p.yes("Use it anyway"):
                continue
        port = p.ask("Port", str(SERVERS[kind][1]))
        if not port.isdigit() or not 1 <= int(port) <= 65535:
            p.out("  Please enter a port between 1 and 65535")
            continue
        try:
            models = discover(kind, str(address), int(port), fetch=fetch)
        except ValueError as exc:
            p.out(f"  {exc}")
            continue
        break
    models = sort_models(models, p.choose("Sort models by", ["name", "size", "loaded"], "loaded"))
    for line in model_table(models):
        p.out("  " + line)
    if not models:
        raise ValueError("The server offers no models; load or pull one first")
    while True:
        number = p.ask("Model number", "1")
        if number.isdigit() and 1 <= int(number) <= len(models):
            return kind, str(address), int(port), models[int(number) - 1]["name"]
        p.out(f"  Please enter a number between 1 and {len(models)}")


def make_prompter(prompt, out, secret):
    prefill = None
    if prompt is None and LINE_EDITING:
        import readline
        if hasattr(readline, "insert_text") and "libedit" not in (readline.__doc__ or ""):
            prefill = prefilled_input
    return Prompter(prompt or input, out, secret, prefill)


def state_home():
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")


def registered_endpoints(agent):
    """The endpoints of sandbox-ai llm that AGENT can use (Claude: those with the Anthropic API); unusable entries
    are left out."""
    try:
        store = llm().load_store()
    except (OSError, ValueError):
        return []
    found = []
    for name in sorted(store["endpoints"]):
        try:
            endpoint = llm().check_record(name, store["endpoints"][name])
        except ValueError:
            continue
        if not llm().agent_problem(endpoint, agent):
            found.append(endpoint)
    return found


def ask_llm_endpoint(p, endpoints, current=None, models_of=None):
    """NAME:MODEL for --llm: the server lists its models (with its key, from the host) when it answers, else the
    ones it listed last time are offered; a model name can be typed as well."""
    printable = launcher().printable
    for index, endpoint in enumerate(endpoints, 1):
        p.out(f"  {index}  {printable(endpoint['name'])}  {endpoint['kind']}  {printable(endpoint['url'])}")
    name = (current or "").partition(":")[0]
    default = next((str(i) for i, e in enumerate(endpoints, 1) if e["name"] == name), "1")
    while True:
        number = p.ask("Endpoint number", default)
        if number.isdigit() and 1 <= int(number) <= len(endpoints):
            break
        p.out(f"  Please enter a number between 1 and {len(endpoints)}")
    endpoint = endpoints[int(number) - 1]
    try:
        models = sort_models((models_of or llm().endpoint_models)(endpoint), "loaded")
    except (OSError, ValueError) as exc:
        p.out(f"  {printable(exc)}")
        models = [{"name": m, "size": None, "loaded": None, "details": "listed earlier"} for m in endpoint["models"]]
    for line in model_table(models) if models else []:
        p.out("  " + line)
    names = [m["name"] for m in models]
    chosen = (current or "").partition(":")[2] if name == endpoint["name"] else ""
    default = str(names.index(chosen) + 1) if chosen in names else chosen or (
        str(names.index(endpoint["model"]) + 1) if endpoint["model"] in names else endpoint["model"] or
        ("1" if names else None))
    while True:
        answer = p.ask("Model number or name" if names else "Model name", default)
        if answer.isdigit() and names and 1 <= int(answer) <= len(names):
            return f"{endpoint['name']}:{names[int(answer) - 1]}"
        if answer and not answer.isdigit() and llm().MODEL_NAME.fullmatch(answer):
            return f"{endpoint['name']}:{answer}"
        p.out("  Please enter a listed number or a model name")


def ask_network(p, answers, project, fetch, editing=False):
    """Local LLM server (OpenCode) or a network profile, then (advanced) the sandbox's own addresses; when
    editing, the current choices are the defaults."""
    choose_network(p, answers, project, fetch, editing)
    if answers["network"] == "offline" and not answers["local"]:
        if answers.get("subnet"):
            p.out("  Dropped --subnet/--subnet6: an offline sandbox has no network")
        answers["subnet"] = []
        return
    ask_subnet(p, answers, editing)


def ask_subnet(p, answers, editing=False):
    """Advanced, default no: --subnet (and --subnet6 with Podman) instead of the backend's own addresses."""
    current = dict(zip(answers.get("subnet", [])[::2], answers.get("subnet", [])[1::2]))
    if not p.yes("Advanced: choose the sandbox's own network addresses (--subnet; default: the backend's own)",
                 bool(current) if editing else False):
        answers["subnet"] = []
        return
    answers["subnet"] = []
    asked = [("--subnet", "IPv4 network for the sandbox, a private /8 to /25 such as 10.20.0.0/24 (the sandbox "
                             "gets .100, the gateway .2)")]
    if answers["backend"] == "podman":
        asked.append(("--subnet6", "IPv6 /64 for the sandbox in fc00::/7, such as fd20::/64"))
    elif current.get("--subnet6"):
        p.out("  Dropped --subnet6: slirp4netns (bwrap) always uses fd00::/64")
    for option, question in asked:
        while True:
            value = p.ask(question + (" ('-' for none)" if current.get(option) else " (Enter for none)"),
                          current.get(option)).strip()
            if value in ("", "-"):
                break
            try:
                # The launcher's own checks, the host's routes included.
                check_launcher(answers["agent"], "--backend", answers["backend"], option, value)
            except ValueError as exc:
                p.out(f"  {launcher().printable(exc)}")
                continue
            answers["subnet"] += [option, value]
            break


def choose_network(p, answers, project, fetch, editing=False):
    """An LLM server registered with sandbox-ai llm, a local LLM server (OpenCode) or a network profile; when
    editing, the current choice is the default."""
    strict = mode_of(answers) in launcher().MODES.STRICT  # the provider only: no LLM server, no profile
    endpoints = [] if strict else registered_endpoints(answers["agent"])
    current = answers.get("llm")
    if endpoints and p.yes(f"Use an LLM server registered with sandbox-ai llm "
                           f"({', '.join(launcher().printable(e['name']) for e in endpoints)})",
                           bool(current) and editing):
        answers["llm"] = ask_llm_endpoint(p, endpoints, current)
        answers["llm_only"] = p.yes("Reach nothing but this server and what you allow below "
                                    "(--no-provider-defaults)", answers.get("llm_only", True) if editing and current
                                    else True)
        answers["local"], answers["network"] = None, "default"
        return
    answers["llm"], answers["llm_only"] = None, False
    local = answers["local"]
    if answers["agent"] == "opencode" and not strict and p.yes("Use a local LLM server (Ollama, LM Studio, vLLM) instead of the cloud",
                                                bool(local) and editing):
        if editing and local and local.get("target") and p.yes(
                f"Keep the local server {launcher().printable(local['target'])} "
                f"({launcher().printable(local['config'])})", default=True):
            return
        kind, host, port, model = ask_local_llm(p, fetch)
        answers["local"] = {"kind": kind, "host": host, "port": port, "model": model,
                            "config": str(local_config_path(project))}
        answers["network"] = "default"
        return
    answers["local"] = None
    p.out("  " + network_help(mode_of(answers)))
    choices = [choice for choice in network_choices() if not mode_refusal(answers, *network_options(choice))]
    current = answers["network"] if editing and answers["network"] in choices else "default"
    answers["network"] = p.choose("Network", choices, current)


def ask_behaviour(p, answers, project, editing=False):
    if mode_refusal(answers, "--dangerously-skip-permissions"):
        p.out(f"  The security mode {mode_of(answers)} keeps the agent's permission prompts")
        answers["danger"] = False
    else:
        answers["danger"] = p.yes("Start the agent without permission prompts (--dangerously-skip-permissions)",
                                  answers["danger"] if editing else False)
    if mode_refusal(answers, "--gui"):
        p.out(f"  The security mode {mode_of(answers)} has no display")
        gui = False
    else:
        gui = p.yes("Virtual display for GUI apps and browsers (--gui)", answers["gui"] if editing else False)
    if editing and answers["gui"] and not gui:
        names = ("--gui-screen", "--gui-vnc")
        if any(name in names for name, *_ in option_groups(answers["extra"])):
            p.out("  Dropped the display options (--gui-screen, --gui-vnc) as well")
            answers["extra"] = [t for name, _, tokens in option_groups(answers["extra"]) if name not in names
                                for t in tokens]
    answers["gui"] = gui
    answers["tmux"] = p.yes("Run in tmux with an extra shell window (default of the launcher in a terminal; "
                            "no adds --no-tmux)", answers["tmux"] if editing else True)
    answers["tmux_forced"] = answers["tmux"] and answers.get("tmux_forced", False)
    # Projects with the same directory name must not share one log directory.
    if p.yes("Log denied egress and file access (--log-dir)", bool(answers["log_dir"]) if editing else False):
        answers["log_dir"] = answers["log_dir"] or str(launcher().default_log_dir(project))
    else:
        answers["log_dir"] = None


def ask_new(p, project, fetch, tokens):
    chosen = {}
    ask_mode(p, chosen)  # first: the questions below offer only what the mode allows
    extra = p.ask("Additional workspaces, comma-separated (mounted at /workspaces/<name>)")
    workspaces = [str(Path(value.strip()).expanduser().absolute()) for value in extra.split(",") if value.strip()]
    agent = p.choose("Agent", ["codex", "claude", "opencode"])
    answers = {"agent": agent, "backend": p.choose("Backend", ["podman", "bwrap"], "podman"),
               "workspaces": workspaces, "network": "default", "local": None, "llm": None, "llm_only": False,
               "danger": False, "gui": False,
               "tmux": True, "log_dir": None, "kits": [], "subnet": [], "mode": chosen["mode"]}
    ask_network(p, answers, project, fetch)
    ask_behaviour(p, answers, project)
    if mode_refusal(answers, "--allow-pypi"):
        p.out(f"  The security mode {mode_of(answers)} opens nothing beyond the agent's provider")
    elif answers["network"] != "offline" and p.yes(
            "Allow further network destinations (presets such as github, hosts, TCP ports, ports on this host)"):
        answers["network_extra"] = ask_network_extra(p, answers)
    if p.yes("Set up Git (commit identity, push tokens for GitHub or your own Git server)"):
        answers["git"] = ask_git(p, answers, project, tokens)
    answers["kits"] = ask_new_kits(p, agent)
    if p.yes("Further options (read-only workspaces, persistent HOME, mounts, variables, MCP, display, any other)"):
        answers["extra"] = ask_extra(p, answers, project, state_home())
    return answers


def load_existing(p, target):
    """(answers, document) of the configuration at TARGET for editing, or None. A file that is not trusted
    (new, or changed since: the agent can write it) is shown in full and used only after a yes."""
    l = launcher()
    data = l.read_config_bytes(target)
    try:
        text = data.decode("utf-8")
        document = json.loads(text)
    except (ValueError, RecursionError):
        p.out(f"{l.printable(target)} is not valid UTF-8 JSON; create a new one instead (sandbox-ai wizard new)")
        return None
    if not all(c in "\n\t" or c.isprintable() for c in text):
        p.out(f"{l.printable(target)} contains control or invisible characters; create a new one instead")
        return None
    problem = config_problem(document)
    if problem:
        p.out(f"{l.printable(target)}: {problem}; create a new one instead (sandbox-ai wizard new)")
        return None
    trusted = trusted_store().get(str(l.config_key(target)))
    if trusted != hashlib.sha256(data).hexdigest():
        state = "changed since it was trusted" if trusted else "not trusted"
        p.out(f"Warning: {l.printable(target)} is {state}. The agent can write this file; review every option:")
        for line in json.dumps(document, indent=2, ensure_ascii=True).splitlines():
            p.out("  " + line)
        if not p.yes("Start from this content anyway"):
            return None
    answers, notes = answers_from_document(document)
    for note in notes:
        p.out(f"  Note: {l.printable(note)}")
    return answers, document


def config_problem(document):
    """Why DOCUMENT cannot be edited as a project configuration, or None."""
    l = launcher()
    if not isinstance(document, dict) or document.get("version") != 1 or set(document) - l.CONFIG_KEYS:
        return f"expected version 1 with keys {sorted(l.CONFIG_KEYS)}"
    if document.get("agent") not in ("codex", "claude", "opencode"):
        return "agent must be codex, claude or opencode"
    for key in ("options", "agent_args", "workspaces"):
        values = document.get(key, [])
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            return f"{key} must be a list of strings"
    if "description" in document and not isinstance(document["description"], str):
        return "description must be a string"
    return None


def summary(answers):
    """[(key, label, text)] of the parts the edit menu offers."""
    def options(values):
        return launcher().printable(shlex.join(values)) if values else "(none)"
    local = answers["local"]
    if answers.get("llm"):
        network = f"LLM server {answers['llm']}" + (", nothing else" if answers.get("llm_only") else "")
    elif local:
        network = (f"local LLM {local.get('target') or url_host(local['host']) + ':' + str(local['port'])} "
                   f"({local['config']})")
    else:
        network = answers["network"]
    if answers.get("subnet"):
        network += ", " + shlex.join(answers["subnet"])
    on = {True: "on", False: "off"}
    behaviour = (f"permission prompts {on[not answers['danger']]}, display {on[answers['gui']]}, "
                 f"tmux {on[answers['tmux']]}, logging {on[bool(answers['log_dir'])]}")
    args = answers["keep"].get("agent_args", [])
    mode = answers.get("mode") or f"{inherited_mode()} (inherited)"
    return [("m", "Security mode", mode), ("1", "Agent", answers["agent"]), ("2", "Backend", answers["backend"]),
            ("3", "Workspaces", launcher().printable(", ".join(answers["workspaces"])) or "(none)"),
            ("4", "Network", launcher().printable(network)), ("5", "Behaviour", behaviour),
            ("6", "Network extras", options(answers["network_extra"])), ("7", "Git", options(answers["git"])),
            ("8", "Further options", options(answers["extra"])), ("9", "Agent arguments", options(args)),
            ("k", "Kits and project memory", options(answers.get("kits") or [])),
            ("0", "All launcher options as one line", "(expert)")]


def edit_menu(p, answers, project, fetch, tokens):
    """Change parts of ANSWERS until saved (True) or abandoned (False)."""
    while True:
        p.out("")
        p.out("Current configuration:")
        parts = summary(answers)
        for key, label, text in parts:
            p.out(f"  {key}  {label.ljust(33)} {text}")
        servers = answers["keep"].get("mcp_servers")
        if servers:
            names = ", ".join(sorted(servers)) if isinstance(servers, dict) else "(invalid)"
            p.out(f"     {'MCP server definitions (kept)'.ljust(33)} {launcher().printable(names)}")
        choice = p.ask("Change which part (number), s to save, q to quit without saving", "s").lower()
        if choice in ("s", "save"):
            return True
        if choice in ("q", "quit"):
            return False
        if choice not in {key for key, *_ in parts}:
            p.out("  Please enter one of the numbers, s or q")
            continue
        edit_part(p, choice, answers, project, fetch, tokens)


def edit_part(p, choice, answers, project, fetch, tokens):
    if choice == "m":
        ask_mode(p, answers)
        p.out("  The options are checked against the mode before saving.")
    elif choice == "1":
        agent = p.choose("Agent", ["codex", "claude", "opencode"], answers["agent"])
        if agent != "opencode" and answers["local"]:
            p.out("  A local LLM server is for OpenCode only; dropped it (network: default)")
            answers["local"], answers["network"] = None, "default"
        name = (answers.get("llm") or "").partition(":")[0]
        if name and name not in [e["name"] for e in registered_endpoints(agent)]:
            p.out(f"  The LLM server {launcher().printable(name)} cannot serve {agent}; dropped it (network: default)")
            answers["llm"], answers["llm_only"] = None, False
        if agent != answers["agent"]:
            p.out("  The options are checked for the new agent before saving.")
        answers["agent"] = agent
    elif choice == "2":
        answers["backend"] = p.choose("Backend", ["podman", "bwrap"], answers["backend"])
        if answers["backend"] == "bwrap":
            ports = [v for name, value, _ in option_groups(answers["network_extra"])
                     if name == "--allow-host-port" for v in [value]]
            if ports:
                p.out("  Dropped --allow-host-port: ports on this host's loopback need the Podman backend")
                answers["network_extra"] = [t for name, _, tokens_ in option_groups(answers["network_extra"])
                                            if name != "--allow-host-port" for t in tokens_]
            if "--subnet6" in answers.get("subnet", []):
                p.out("  Dropped --subnet6: slirp4netns (bwrap) always uses fd00::/64")
                index = answers["subnet"].index("--subnet6")
                del answers["subnet"][index:index + 2]
    elif choice == "3":
        values = ask_list(p, "Additional workspaces, comma-separated (mounted at /workspaces/<name>)",
                          lambda value: None, ", ".join(answers["workspaces"]) or None)
        answers["workspaces"] = [str(Path(value).expanduser().absolute()) for value in values]
    elif choice == "4":
        ask_network(p, answers, project, fetch, editing=True)
    elif choice == "5":
        ask_behaviour(p, answers, project, editing=True)
    elif choice == "6":
        if answers["network"] == "offline" and not answers["local"] and not answers.get("llm"):
            p.out("  The network is offline; choose another network first (4)")
        else:
            answers["network_extra"] = ask_network_extra(p, answers, answers["network_extra"])
    elif choice == "7":
        answers["git"] = ask_git(p, answers, project, tokens, answers["git"])
    elif choice == "8":
        answers["extra"] = ask_extra(p, answers, project, state_home(), answers["extra"])
    elif choice == "k":
        answers["kits"] = ask_kits(p, answers["agent"], answers.get("kits") or [])
    elif choice == "9":
        text = p.edit("Agent arguments (after --), as on the command line",
                      shlex.join(answers["keep"].get("agent_args", [])))
        try:
            values = shlex.split(text)
        except ValueError as exc:
            p.out(f"  {exc}")
            return
        if values:
            answers["keep"]["agent_args"] = values
        else:
            answers["keep"].pop("agent_args", None)
    elif choice == "0":
        current = build_document(answers)["options"]
        while True:
            text = p.edit("All launcher options", shlex.join(current))
            try:
                values = check_free_options(answers["agent"], text)
            except ValueError as exc:
                p.out(f"  {launcher().printable(exc)}")
                continue
            break
        document = {"agent": answers["agent"], "workspaces": answers["workspaces"], "options": values,
                    "mode": answers.get("mode")}
        rebuilt, notes = answers_from_document(document)
        for note in notes:
            p.out(f"  Note: {note}")
        rebuilt["keep"] = answers["keep"]
        local = answers["local"]
        if local and "kind" in local and rebuilt["local"] and rebuilt["local"]["config"] == local["config"]:
            rebuilt["local"] = local  # chosen in this edit and not written yet
        answers.clear()
        answers.update(rebuilt)


def changes(old, new):
    """Lines describing what changes from document OLD to NEW."""
    lines = []
    for key in ("agent", "workspaces", "agent_args", "description"):
        if (old.get(key) or None) != (new.get(key) or None):  # missing and empty are the same
            lines.append(f"  {key}: {json.dumps(old.get(key), ensure_ascii=True)} -> "
                         f"{json.dumps(new.get(key), ensure_ascii=True)}")

    def counted(options):
        return Counter(tuple(flat(name, value)) for name, value, _ in option_groups(options))
    before, after = counted(old.get("options", [])), counted(new.get("options", []))
    lines += [f"  - {launcher().printable(shlex.join(option))}" for option in (before - after).elements()]
    lines += [f"  + {launcher().printable(shlex.join(option))}" for option in (after - before).elements()]
    return lines


def run(prompt=None, out=print, fetch=None, secret=None, project=None, mode=None):
    """Interactive wizard; returns the written configuration path, or None if cancelled.
    MODE "edit" changes the existing configuration of PROJECT; otherwise an existing one can be edited,
    replaced or kept."""
    p = make_prompter(prompt, out, secret)
    fetch = fetch or fetch_json
    out("sandbox-ai project setup. Press Enter to accept [defaults].")
    if project is None:
        project = Path(p.ask("Project directory (mounted at /work)", os.getcwd()))
    project = Path(project).expanduser().absolute()
    target = project / launcher().CONFIG_NAME
    refuse_symlink(target)
    if mode == "edit" and not target.exists():
        out(f"No configuration at {target}; create one with: sandbox-ai wizard new")
        return None
    if target.exists() and mode != "edit":
        mode = p.choose(f"{target} exists: edit it, replace it with a new one, or cancel",
                        ["edit", "new", "cancel"], "edit")
        if mode == "cancel":
            out("Cancelled; nothing written.")
            return None
    tokens, old = [], None
    if mode == "edit":
        loaded = load_existing(p, target)
        if loaded is None:
            out("Cancelled; nothing written.")
            return None
        answers, old = loaded
        if not edit_menu(p, answers, project, fetch, tokens):
            out("Cancelled; nothing written.")
            return None
    else:
        answers = ask_new(p, project, fetch, tokens)
    return finish(p, project, target, answers, tokens, old)


def finish(p, project, target, answers, tokens, old=None):
    """Show, confirm and write the configuration (and the local OpenCode file and typed tokens)."""
    out = p.out
    agent, workspaces = answers["agent"], answers["workspaces"]
    document = build_document(answers)
    # A token typed for an entry that was dropped again is not written.
    named = {value.partition("=")[2] for name, value, _ in option_groups(document["options"])
             if name == "--git-token-file" and value}
    tokens = [(path, token) for path, token in tokens if path in named]
    new_local = answers["local"] if answers["local"] and "kind" in answers["local"] else None
    checked = list(document["options"])
    # The local OpenCode configuration and typed tokens are written only after confirmation.
    unwritten = {new_local["config"]} if new_local else set()
    unwritten |= {path for path, _ in tokens}
    index = 0
    while index < len(checked) - 1:
        option, value = checked[index], checked[index + 1]
        if (option == "--local-config" and value in unwritten
                or option == "--git-token-file" and value.partition("=")[2] in unwritten):
            del checked[index:index + 2]
        else:
            index += 1
    try:
        check_launcher(agent, "--workspace", ",".join([str(project), *workspaces]), *checked)
        refused = mode_refusal(answers, *checked)
        if refused:
            raise ValueError(refused)
    except ValueError as exc:
        out(f"Note: the launcher would refuse to start with this right now: {launcher().printable(exc)}")
    out("")
    out(f"Configuration for {target}:")
    for line in json.dumps(document, indent=2, ensure_ascii=True).splitlines():
        out("  " + line)
    if old is not None:
        lines = changes(old, document)
        out("Changes:" if lines else "No changes.")
        for line in lines:
            out(line)
    if not p.yes("Write configuration", default=True):
        out("Cancelled; nothing written.")
        return None
    project.mkdir(parents=True, exist_ok=True)
    for path in [target] + ([new_local["config"]] if new_local else []):
        refuse_symlink(path)
    if new_local:
        Path(new_local["config"]).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        write_file(new_local["config"], json.dumps(opencode_config(new_local["kind"], new_local["host"],
                                                                   new_local["port"], new_local["model"]),
                                                   indent=2) + "\n")
        out(f"Wrote {new_local['config']} (OpenCode uses only {new_local['host']}:{new_local['port']}, "
            f"model {new_local['model']})")
    for path, token in tokens:
        write_token(path, token)
        out(f"Wrote the token to {path} (mode 0600; the project configuration names only this file)")
    text = json.dumps(document, indent=2) + "\n"
    write_file(target, text)
    launcher().trust_config(target, hashlib.sha256(text.encode()).hexdigest())
    out(f"Wrote and trusted {target}")
    out(f"Start with: sandbox-ai {agent} --workspace {shlex.quote(str(project))}")
    out(f"Change it later with: sandbox-ai wizard edit {shlex.quote(str(project))}")
    return target


def review(path):
    """(key, data, document, problem) of the project configuration at PATH, read once: problem is why it cannot be
    trusted (not UTF-8 JSON, control or invisible characters), else None."""
    path = launcher().config_key(path)
    data = launcher().read_config_bytes(path)
    try:
        text = data.decode("utf-8")
        document = json.loads(text)
    except ValueError as exc:
        return path, data, None, f"{launcher().printable(path)} is not valid UTF-8 JSON: {launcher().printable(exc)}"
    # Raw bytes could hide options from view (carriage returns, escapes,
    # bidi controls); refuse those and show what will actually take effect.
    if not all(c in "\n\t" or c.isprintable() for c in text):
        return path, data, document, (f"{launcher().printable(path)} contains control or invisible characters; "
                                      "refusing to trust it. Remove them (e.g. Windows line endings) and try again.")
    return path, data, document, None


def trust(path, yes=False, prompt=None, out=print, digest=None):
    """Show the configuration at PATH and trust exactly the bytes shown (asked first unless YES). With DIGEST (the
    SHA-256 of what was reviewed elsewhere, e.g. `trust --show --json` in the web page) it is trusted only when the
    file is exactly that now, without asking."""
    if digest is not None and not re.fullmatch(r"[0-9a-f]{64}", digest):
        out("--digest is a SHA-256 in hex (64 characters 0-9, a-f)")
        return 2
    # Trust exactly the bytes shown, even if the file changes during the prompt.
    path, data, document, problem = review(path)
    if problem:
        out(problem)
        return 1
    found = hashlib.sha256(data).hexdigest()
    if digest is not None and found != digest:
        out(f"{launcher().printable(path)} is not what was reviewed (its SHA-256 is {found}, not {digest}): not "
            "trusted. Review it again.")
        return 1
    out(f"{launcher().printable(path)} takes effect as:")
    out(json.dumps(document, indent=2, ensure_ascii=True))
    if digest is None and not yes and not Prompter(prompt or input, out).yes("Trust this configuration"):
        out("Not trusted.")
        return 1
    launcher().trust_config(path, found)
    out("Trusted.")
    return 0


def trusted_store(note=None):
    """{path: digest} of the trusted list, damaged entries left out (the file may have been edited by hand or
    damaged: no object, a key or digest that is no string): NOTE(text) is told of each one skipped."""
    store = launcher().trusted_configs()
    note = note or (lambda line: print(line, file=sys.stderr))
    if not isinstance(store, dict):
        note("sandbox-wizard: the trusted list is damaged (no JSON object); nothing in it is trusted")
        return {}
    found = {}
    for key, digest in store.items():
        if isinstance(key, str) and isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest):
            found[key] = digest
        else:
            note(f"sandbox-wizard: skipped a damaged entry of the trusted list: {launcher().printable(key)}")
    return found


def show(path, as_json=False, out=print):
    """What trusting PATH would trust, nothing trusted: {"path", "digest", "document", "trusted", "trusted_digest",
    "problem"} with --json (document null when it is no JSON; trusted: whether the trusted list has exactly this
    digest). When PATH cannot be read (missing, a symlink, not a regular file, too big) the same object comes with
    "error" (and "problem") set to why, digest and document null, and the exit status is 1."""
    try:
        path, data, document, problem = review(path)
    except (OSError, ValueError) as exc:
        if not as_json:
            raise
        key = str(Path(path).expanduser().absolute())
        try:
            key = str(launcher().config_key(path))
        except (OSError, ValueError, RuntimeError):
            pass
        why = launcher().printable(exc)
        out(json.dumps({"path": key, "digest": None, "document": None, "trusted": False,
                        "trusted_digest": trusted_store().get(key), "problem": why, "error": why}, indent=2))
        return 1
    digest = hashlib.sha256(data).hexdigest()
    known = trusted_store().get(str(path))
    found = {"path": str(path), "digest": digest, "document": document, "trusted": known == digest,
             "trusted_digest": known, "problem": problem}
    if as_json:
        out(json.dumps(found, indent=2))
        return 0
    out(problem or f"{launcher().printable(path)} takes effect as:")
    if not problem:
        out(json.dumps(document, indent=2, ensure_ascii=True))
    out(f"SHA-256 {digest} ({'trusted' if found['trusted'] else 'changed since it was trusted' if known else 'not trusted'})")
    return 0


def trusted_list(as_json=False, out=print):
    """The trusted project configurations: [{"path", "digest", "state"}], state trusted (the file is that now),
    changed, missing or unreadable."""
    found = []
    for key, digest in sorted(trusted_store().items()):
        try:
            now = hashlib.sha256(launcher().read_config_bytes(key)).hexdigest()
            state = "trusted" if now == digest else "changed"
        except FileNotFoundError:
            state = "missing"
        except (OSError, ValueError):
            state = "unreadable"
        found.append({"path": key, "digest": digest, "state": state})
    if as_json:
        out(json.dumps(found, indent=2))
        return 0
    if not found:
        out("No trusted project configurations.")
    for entry in found:
        out(f"{entry['state']:<10}  {launcher().printable(entry['path'])}")
    return 0


def forget(path, out=print):
    if launcher().forget_config(path):
        out(f"No longer trusted: {launcher().config_key(path)}")
        return 0
    out(f"Not in the trusted list: {launcher().config_key(path)}")
    return 1


def cli(argv):
    parser = argparse.ArgumentParser(prog="sandbox-wizard", description="Create and trust sandbox project configurations.")
    parser.add_argument("-V", "--version", action="version", version=f"sandbox-ai {launcher().__version__}")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("new", help="Interactive setup (default); offers to edit an existing configuration")
    edit_parser = commands.add_parser("edit", help="Change the existing configuration of a project")
    edit_parser.add_argument("path", nargs="?", type=Path, default=Path("."),
                             help=f"Project directory or its {launcher().CONFIG_NAME} (default: current directory)")
    trust_parser = commands.add_parser("trust", help="Review and trust a project configuration")
    trust_parser.add_argument("path", type=Path, nargs="?")
    trust_parser.add_argument("-y", "--yes", action="store_true", help="Trust without asking")
    trust_parser.add_argument("--show", action="store_true", help="Only show what would be trusted, and its SHA-256")
    trust_parser.add_argument("--digest", metavar="SHA256",
                              help="Trust only if the file's SHA-256 is exactly this (what was reviewed); no question")
    trust_parser.add_argument("--list", action="store_true", help="The trusted project configurations")
    trust_parser.add_argument("-j", "--json", action="store_true", help="with --show or --list: as JSON")
    forget_parser = commands.add_parser("forget", help="Remove a configuration from the trusted list")
    forget_parser.add_argument("path", type=Path)
    models = commands.add_parser("models", help="List the models of a local LLM server, or of one registered "
                                                "with sandbox-ai llm (--llm NAME)")
    models.add_argument("-k", "--kind", choices=list(SERVERS))
    models.add_argument("-H", "--host")
    models.add_argument("-l", "--llm", metavar="NAME", help="a server of sandbox-ai llm list (key, https, its CA)")
    models.add_argument("-p", "--port", type=int)
    models.add_argument("-u", "--url", metavar="URL", help="http(s)://HOST[:PORT][/PREFIX] of a server not "
                                                           "registered yet (with --kind)")
    models.add_argument("-c", "--ca", metavar="FILE", help="with --url https://...: the PEM CA that signed its "
                                                            "certificate")
    models.add_argument("-s", "--api-key-secret", metavar="NAME", dest="api_key_secret",
                        help="with --url: the secret (sandbox-ai secrets) holding its API key")
    models.add_argument("--sort", choices=["name", "size", "loaded"], default="loaded")
    models.add_argument("-j", "--json", action="store_true", help="as JSON")
    a = parser.parse_args(argv)
    if a.command in (None, "new", "edit") or a.command == "trust" and not (a.show or a.list or a.digest):
        enable_line_editing()
    if a.command == "trust":
        if a.list:
            if a.path is not None or a.show or a.digest or a.yes:
                parser.error("trust --list takes no PATH, --show, --digest or --yes")
            return trusted_list(a.json)
        if a.path is None:
            parser.error("trust needs PATH (or --list)")
        if a.show:
            if a.digest or a.yes:
                parser.error("--show trusts nothing: no --digest or --yes with it")
            return show(a.path, a.json)
        if a.json:
            parser.error("--json goes with --show or --list")
        return trust(a.path, a.yes, digest=a.digest)
    if a.command == "forget":
        return forget(a.path)
    if a.command == "models":
        if a.llm:
            found = llm().endpoint_models(llm().endpoint(a.llm))
        elif a.url:
            # A server that is not registered yet, by its whole URL (https and a path prefix included): what the
            # app's and the web's "Test connection" asks before anything is stored.
            if not a.kind:
                parser.error("--url needs --kind")
            found = llm().discover_url(a.kind, a.url, ca=a.ca, api_key_secret=a.api_key_secret)
        elif a.kind and a.host:
            found = discover(a.kind, a.host, a.port or SERVERS[a.kind][1], fetch=fetch_json)
        else:
            parser.error("models needs --kind with --host or --url, or --llm NAME")
        found = sort_models(found, a.sort)
        if a.json:
            print(json.dumps(found, indent=2))
            return 0
        for line in model_table(found):
            print(line)
        return 0
    if a.command == "edit":
        path = a.path.expanduser().absolute()
        project = path.parent if path.name == launcher().CONFIG_NAME else path
        return 0 if run(project=project, mode="edit") else 1
    return 0 if run() else 1


if __name__ == "__main__":
    try:
        sys.exit(cli(sys.argv[1:]))
    except (OSError, ValueError) as exc:
        print(f"sandbox-wizard: {exc}", file=sys.stderr)
        sys.exit(1)
    except EOFError:  # Ctrl-D
        print("\nCancelled; nothing written.", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nCancelled; nothing written.", file=sys.stderr)
        sys.exit(130)
