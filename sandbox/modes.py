"""Security modes: named presets of the launcher's options, from lax to paranoid. Standard library only.

A mode sets defaults; options (on the command line or in the project file) may only narrow it. In the strict
modes (high, paranoid) an option that would widen the sandbox beyond the mode is refused, naming the mode and
the laxer mode that would allow it. lax, low and medium only change defaults: each of them still allows
everything the launcher allowed before modes existed (medium is exactly that start).

--ask allow (every question answered "allow" by the launcher) is no widening of a strict mode the human chose
(--mode, the project file, defaults.json): it is their answer to the mode's questions, given beforehand, and the
start says so in a warning; what the mode refuses stays refused. It is refused where a floor chose the strict mode
(the administrator's min_mode, a preset's mode, --mode-at-least): those questions are not the starter's to answer
unseen (ask_allow_refusal).

Where the mode comes from, the first that is set: --mode, the project file's "mode", "mode" in
~/.config/sandbox-ai/defaults.json, else medium. The administrator's policy key min_mode is a floor: a mode
below it is refused, and without a choice the floor is the mode.

The launcher loads this module by path (launchers run with python -I) and passes itself as L; nothing here
reads the launcher at import time.
"""
import argparse
import os
import re

NAMES = ("lax", "low", "medium", "high", "paranoid")  # in rising strictness
DEFAULT = "medium"
STRICT = ("high", "paranoid")
SCOPES = ("once", "run", "always")  # approval scopes, narrowest first
PRIVACY = ("off", "public", "always")  # --privacy-check, laxest first

# Options that choose the shape of the network. Given explicitly, they replace the mode's network default
# (--mode lax --profile development: the development hosts, not everything); the other network options add
# to it.
NETWORK_SHAPE = ("profile", "offline", "egress_all_allow", "no_provider_defaults", "local_config")

# The modes. "network": options for the network unless one of NETWORK_SHAPE is given; "ask", "privacy_check",
# "limits": defaults unless the option is given; "flags": options always added (they only narrow);
# "approval_scope": the widest scope an approval may have (the policy's max_approval_scope can narrow it
# further); "other_workspaces_ro": every workspace but the first (/work) read-only; "rules": the mode's own
# ruleset (see ruleset()); "taint": what the mode does once untrusted content entered the run (see
# taint_settings()); "canaries": the decoy
# files (canary.py): "categories" planted at least, "on_read"/"on_send" weighed like a ruleset's "canary" section
# (the strictest wins). Without the key, the launcher's own: credentials and home, a read reported, a token leaving
# stops.
# "telemetry": the least --telemetry level (agent_sandbox.telemetry_level; off when the key is missing).
MODES = {
    "lax": {
        "summary": "every outbound destination (--egress-all-allow), logged with --log-debug; no approval questions",
        "network": ["--egress-all-allow"], "ask": "off", "flags": ["--log-debug"],
        # Everything is open on purpose: a canary token leaving is reported, not refused (rulesets may be stricter).
        "canaries": {"on_read": "report", "on_send": "report"}},
    "low": {
        "summary": "development hosts: the provider, PyPI, npm, GitHub and the OpenAI and Anthropic APIs; "
                   "approvals on",
        "network": ["--profile", "development", "--allow-npm"]},
    "medium": {
        "summary": "the launcher's defaults: the agent's provider only, approvals on (the default mode)"},
    "high": {
        "summary": "the provider only, nothing can be opened by options; approvals at most for the run; writes "
                   "to other hosts are asked; workspaces after the first read-only; no devices, nested containers, "
                   "Wayland or variants on your own base images; privacy check of every push; 4g memory, 2048 processes",
        "privacy_check": "always", "limits": {"memory": "4g", "pids": "2048"}, "approval_scope": "run",
        "other_workspaces_ro": True, "rules": "ask-writes", "taint": "ask-mcp",
        "canaries": {"categories": ["credentials", "home"], "on_read": "ask", "on_send": "stop"}},
    "paranoid": {
        "summary": "as high, and: every HTTP request but the provider's refused, approvals only once, all "
                   "workspaces read-only, no -D, no display, no MCP server with network, no services, --log-debug; 2g "
                   "memory, 2 CPUs, 1024 processes",
        "privacy_check": "always", "limits": {"memory": "2g", "cpus": "2", "pids": "1024"},
        "approval_scope": "once", "flags": ["--workspace-ro", "--log-debug"], "rules": "deny",
        "canaries": {"categories": ["credentials", "home"], "on_read": "stop", "on_send": "stop"},
        "taint": "escalate", "telemetry": "strict"},
}
WRITE_METHODS = ["POST", "PUT", "PATCH", "DELETE"]


def rank(name):
    return NAMES.index(name)


def check_name(value, where):
    if value not in NAMES:
        raise ValueError(f"{where}: the security mode is one of {', '.join(NAMES)}, not {value!r}")
    return value


def summaries():
    """["NAME: what it does"] for --help, sandbox-ai doctor and the wizard."""
    return [f"{name}: {MODES[name]['summary']}" for name in NAMES]


# The agent's own tools that act or fetch: in paranoid they ask once the run is tainted (through its hooks).
TAINTED_TOOLS = ["Bash", "Write", "Edit", "MultiEdit", "NotebookEdit", "apply_patch", "WebFetch"]


def taint_settings(name):
    """What the mode does once untrusted content entered the run (README, Rulesets: the run's taint), as sections
    of its ruleset: "untrusted" (the sources) and rules with "when": {"tainted": true}; {} without escalation.
    high: content from a host other than the provider taints, and then every MCP tool call asks (writes to such
    hosts ask anyway). paranoid: every MCP tool result and the agent's WebSearch and WebFetch taint as well
    (WebSearch runs at the provider, past the proxy), and then the agent's shell, file edits and fetches ask too."""
    kind = MODES[name].get("taint")
    if not kind:
        return {}
    settings = {"untrusted": [{"id": "mode-hosts", "host": "*"}],
                "mcp": [{"id": "mode-tainted-mcp", "server": "*", "tool": "*", "when": {"tainted": True},
                         "decision": "ask", "reason": f"security mode {name}: an MCP tool call in a tainted run"}]}
    if kind == "escalate":
        settings["untrusted"] += [{"id": "mode-mcp-results", "server": "*", "tool": "*"},
                                  {"id": "mode-agent-web", "agent": "*", "tool": ["WebSearch", "WebFetch"]}]
        settings["tool"] = [{"id": "mode-tainted-tools", "tool": TAINTED_TOOLS, "when": {"tainted": True},
                             "decision": "ask",
                             "reason": f"security mode {name}: the agent's shell, edits and fetches in a tainted run"}]
    return settings


# --- choosing the mode ----------------------------------------------------------------------------------------

def resolve(cli, project, project_path, defaults, defaults_path, policy, policy_path, server=False):
    """(mode, origin): --mode CLI, else the project file's mode, else your defaults' (not for an MCP server's own
    sandbox: SERVER), else medium. The policy's min_mode is a floor nothing may go below."""
    floor = policy.get("min_mode")
    if cli:
        name, origin = cli, "--mode"
    elif project and project.get("mode"):
        name, origin = project["mode"], f"the project file {project_path}"
    elif defaults.get("mode") and not server:
        name, origin = defaults["mode"], f"your defaults {defaults_path}"
    elif floor and rank(floor) > rank(DEFAULT):
        return floor, f"the administrator's policy {policy_path} (min_mode)"
    else:
        return DEFAULT, "default"
    if floor and rank(name) < rank(floor):
        raise ValueError(f"The security mode {name} ({origin}) is below the administrator's minimum, {floor} "
                         f"({policy_path}, min_mode); choose {' or '.join(NAMES[rank(floor):])}")
    return name, origin


# --- the mode's defaults ---------------------------------------------------------------------------------------

def explicit(l, argv):
    """The destinations (argparse dest) of the options ARGV (as on the command line, the agent first) gives."""
    parser = l.build_parser()
    for action in parser._actions:
        action.default = argparse.SUPPRESS
    options = list(argv)
    if "--" in options:
        options = options[:options.index("--")]
    return set(vars(parser.parse_args(options)))


def host_cpus():
    """The launcher's default --cpus: half of this host's CPUs."""
    return max(1, (os.cpu_count() or 2) // 2)


def limit_ceiling(name, key):
    """The mode's limit for KEY (memory, cpus, pids) as text, or None: no limit beyond the launcher's own."""
    limits = MODES[name].get("limits", {})
    if key == "cpus" and name in STRICT:
        # Never more than the launcher's default, half of the host's CPUs.
        return str(min(int(limits.get("cpus", host_cpus())), host_cpus()))
    return limits.get(key)


def defaults(name, given):
    """The options the mode adds for a start whose options set GIVEN (destinations)."""
    mode, tokens = MODES[name], []
    if not given & set(NETWORK_SHAPE):
        tokens += mode.get("network", [])
    if "ask" in mode and "ask" not in given:
        tokens.append(f"--ask={mode['ask']}")
    if "privacy_check" in mode and "privacy_check" not in given:
        tokens.append(f"--privacy-check={mode['privacy_check']}")
    for key in ("memory", "cpus", "pids"):
        value = limit_ceiling(name, key) if key in mode.get("limits", {}) else None
        if value and key not in given:
            tokens += [f"--{key}", value]
    return tokens + mode.get("flags", [])


def apply(l, argv, a, document, project_path, user_defaults, policy):
    """A (the parsed ARGV, project file applied) with the mode's defaults: parsed again with them in front of the
    options when the mode adds any. Records a.mode, a.mode_origin and a.mode_options (what the mode added)."""
    server = bool(getattr(a, "mcp_socket_dir", None))
    name, origin = resolve(a.mode, document, project_path, user_defaults, l.defaults_file(), policy,
                           l.POLICY_FILE, server)
    given = explicit(l, argv)
    tokens = defaults(name, given)
    if tokens:
        a = l.parse_args([argv[0], *tokens, *argv[1:]])
    # mode_given: the destinations the options set; the others may hold the mode's values (check_policy says so).
    a.mode, a.mode_origin, a.mode_options, a.mode_given = name, origin, tokens, sorted(given)
    return a


def refusal(l, agent, options, name):
    """Why the mode NAME refuses a start of AGENT with OPTIONS (as on the command line), or None: for the
    wizard, which asks only what the mode allows."""
    argv = [agent, *options]
    a = l.parse_args([agent, *defaults(name, explicit(l, argv)), *options])
    found = widening(l, a, name)
    return f"{found[0][0]} widens the security mode {name}: {found[0][1]}" if found else None


# --- what the mode refuses -------------------------------------------------------------------------------------

def mcp_has_network(definition, l):
    """A server definition (mcp.py) whose sandbox or container reaches the network."""
    if "image" in definition or "rootfs" in definition:
        return definition.get("network", "none") == "full"
    options = l.canonical_options(list(definition.get("options", [])))
    return not ("--offline" in options or "--profile=offline" in options
                or any(a == "--profile" and b == "offline" for a, b in zip(options, options[1:])))


def _given(values, option):
    return " ".join(f"{option} {value}" for value in values) or None


def _size(value):
    """Bytes of a --memory value such as 4g, 512m or 1048576, or None if it is no size."""
    match = re.fullmatch(r"([0-9]+)([kmg]?)", str(value).strip().lower())
    return int(match[1]) << {"": 0, "k": 10, "m": 20, "g": 30}[match[2]] if match else None


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# (modes that refuse it, what was given in A or None, why it widens). Checked on the parsed options, so a profile,
# a short form, a project file or a Git token (which opens its server) counts as what it sets.
RULES = [
    (STRICT, lambda a, l: _given([p for p in a.profile if p != "offline"], "--profile"),
     "a network profile opens more than the agent's provider"),
    (STRICT, lambda a, l: "--egress-all-allow" if a.egress_all_allow else None, "it opens every destination"),
    (STRICT, lambda a, l: " ".join(f"--allow-{p}" for p in a.presets) or None,
     "it opens hosts beyond the agent's provider (a Git token for github.com opens them too)"),
    (STRICT, lambda a, l: "--frontier" if a.frontier else None, "it opens the OpenAI and Anthropic APIs"),
    (STRICT, lambda a, l: _given(list(dict.fromkeys(getattr(a, "companions", None) or [])), "--with"),
     "another agent in the sandbox reaches its own provider"),
    (STRICT, lambda a, l: _given(list(dict.fromkeys(getattr(a, "helper", None) or [])), "--helper"),
     "a helper reaches its own provider and works on the same workspace"),
    (STRICT, lambda a, l: _given(a.allow_host, "--allow-host"),
     "it opens hosts beyond the agent's provider (so does a Git token for another server)"),
    (STRICT, lambda a, l: _given(a.allow, "--allow"), "it opens addresses"),
    (STRICT, lambda a, l: _given(a.allow_tcp, "--allow-tcp"), "it opens TCP ports"),
    (STRICT, lambda a, l: _given(getattr(a, "vpn_bypass_tcp", None) or [], "--vpn-bypass-tcp"),
     "it reaches an address of your network past Tor, from your own address"),
    (STRICT, lambda a, l: _given(a.allow_host_port, "--allow-host-port"),
     "it opens ports on the host (so does a Git token for a server on this host)"),
    (STRICT, lambda a, l: ("--nameserver " + " ".join(a.nameserver)) if a.nameserver else "--dns" if a.dns
     else None, "DNS lookups leave the sandbox"),
    (STRICT, lambda a, l: f"--llm {a.llm}" if a.llm else None, "it opens the LLM server's address"),
    (STRICT, lambda a, l: "--no-proxy" if a.no_proxy else None,
     "without the proxy, connections are neither logged nor asked about, and pushes are not checked"),
    (STRICT, lambda a, l: _given(a.no_inspect, "--no-inspect"), "TLS to these hosts passes unchecked"),
    (STRICT, lambda a, l: "--ask=off" if a.ask == "off" else None, "nothing would be asked before a connection"),
    (STRICT, lambda a, l: f"--privacy-check={a.privacy_check}" if a.privacy_check != "always" else None,
     "pushes would not all be checked"),
    (STRICT, lambda a, l: "--no-canaries" if getattr(a, "canaries", None) == "none" else None,
     "without canary files a read of credentials or a leaking token goes unnoticed"),
    (STRICT, lambda a, l: "--telemetry on" if getattr(a, "telemetry", None) == "on" else None,
     "the agents would report usage, errors and feedback to their makers"),
    (("paranoid",), lambda a, l: "--telemetry off" if getattr(a, "telemetry", None) == "off" else None,
     "the agents would make calls beyond the model's (plugin downloads, model lists, language servers)"),
    (STRICT, lambda a, l: _given(a.device, "--device"), "the device's driver runs in your host's kernel"),
    (STRICT, lambda a, l: f"--gpu {a.gpu}" if a.gpu else None, "the GPU's driver runs in your host's kernel"),
    (STRICT, lambda a, l: "--nested-containers" if getattr(a, "nested_containers", False) else None,
     "the agent gets user namespaces of its own and more system calls, which reach more of your host's kernel"),
    (STRICT, lambda a, l: "--gui-wayland" if a.gui_wayland else None, "it reaches your desktop's compositor"),
    (STRICT, lambda a, l: f"--image-variant {a.image_variant}" if getattr(l, "variant_base", None) and
     l.variant_base(a) else None,
     "the variant is built on an image of yours that nobody reviewed, and the sandbox's own supervisor (firewall, "
     "proxy, approvals) would run from its programs"),
    (("paranoid",), lambda a, l: _given(list(getattr(a, "service", None) or []), "--service"),
     "an image nobody reviewed would run beside the agent, on its network"),
    (STRICT, lambda a, l: _given([m for m in a.mount if m.rsplit(":", 1)[-1] == "rw"], "--mount"),
     "the agent could write a host folder"),
    # A start's own folder for an MCP server (a memory per project): what the agent has it keep there outlives the run.
    (("paranoid",), lambda a, l: _given([m for m in getattr(a, "mcp_mount", None) or [] if not m.endswith(":ro")],
                                        "--mcp-mount"),
     "an MCP server would keep what the agent gives it in a host folder, beyond the run"),
    (("paranoid",), lambda a, l: "--dangerously-skip-permissions" if a.dangerously_skip_permissions else None,
     "the agent would act without asking"),
    (("paranoid",), lambda a, l: ("--gui-vnc" if a.gui_vnc else "--gui") if a.gui else None,
     "a display serves programs the mode keeps out"),
    (("paranoid",), lambda a, l: _given([n for n in dict.fromkeys(a.mcp) if mcp_has_network(
        l.load_mcp_module().definitions(getattr(a, "project_document", None)).get(n, {}), l)], "--mcp"),
     "the MCP server reaches the network past the agent's firewall and proxy"),
    # paranoid keeps nothing beyond the run: every start discards its HOME (as --no-keep; resolve_keep), so asking
    # to keep it, or for a session (kept by definition), widens the mode. high keeps HOMEs like the laxer modes.
    (("paranoid",), lambda a, l: "--keep" if getattr(a, "keep", None) is True else None,
     "a kept HOME outlives the run, and whatever the agent left in it reaches the next start"),
    (("paranoid",), lambda a, l: f"--state-dir {a.state_dir}" if getattr(a, "state_dir", None)
     and not (getattr(a, "mcp_socket_dir", None) or getattr(a, "tmux_socket_dir", None)) else None,
     "a HOME of your own outlives the run, and whatever the agent left in it reaches the next start"),
    (("paranoid",), lambda a, l: f"--session {a.session}" if getattr(a, "session", None) is not None else None,
     "a session keeps its HOME beyond the run"),
]


def limit_widening(a, name):
    """[(given, why)] of the limits above the mode's (a limit of none is above every one)."""
    found = []
    for key in ("memory", "cpus", "pids"):
        ceiling = limit_ceiling(name, key)
        value = getattr(a, key)
        if ceiling is None or value is None:
            continue
        measure = _size if key == "memory" else _number
        if str(value).strip().lower() == "none":
            found.append((f"--{key} none", f"the mode limits it to {ceiling}"))
        elif measure(value) is not None and measure(value) > measure(ceiling):
            found.append((f"--{key} {value}", f"the mode limits it to {ceiling}"))
    return found


def widening(l, a, name):
    """[(given, why)]: what in A widens the mode NAME."""
    found = [(given, why) for modes, test, why in RULES if name in modes for given in [test(a, l)] if given]
    return found + limit_widening(a, name)


def laxer(l, a, given, name, floor):
    """The strictest mode laxer than NAME (not below FLOOR) in which GIVEN is no widening, or None."""
    for candidate in reversed(NAMES[rank(floor or NAMES[0]):rank(name)]):
        if not any(item == given for item, _ in widening(l, a, candidate)):
            return candidate
    return None


def ask_allow_refusal(l, a, policy):
    """Why --ask allow is refused for the start A, or None: only where a floor chose a strict mode - the
    administrator's min_mode (high or paranoid), or a.mode_floor (the launcher's: a preset's mode, --mode-at-least).
    A strict mode the human chose is theirs, and so is answering its questions (the start warns)."""
    if getattr(a, "ask", None) != "allow" or a.mode not in STRICT:
        return None
    floor = policy.get("min_mode")
    if floor in STRICT:
        return (f"--ask allow would answer every question of the security mode {a.mode} unseen, and the "
                f"administrator's policy ({l.POLICY_FILE}) requires at least the mode {floor} (min_mode): its "
                "questions are put to you; start with --ask on")
    if getattr(a, "mode_floor", None):
        return (f"--ask allow would answer every question of the security mode {a.mode} unseen, and that mode is a "
                f"floor ({a.mode_floor}), not your choice: leave --ask allow out (its questions are put to you), or "
                f"choose the mode yourself with --mode (with --ask allow its questions are answered too)")
    return None


def check(l, a, policy):
    """Refuse the first option that widens the mode, naming the mode and the laxer one that would allow it; then
    record what the mode enforces beyond options: the approval scope, read-only workspaces and its ruleset."""
    name = a.mode
    refused = ask_allow_refusal(l, a, policy)
    if refused:
        raise ValueError(refused)
    for given, why in widening(l, a, name):
        floor = policy.get("min_mode")
        other = laxer(l, a, given, name, floor)
        hint = (f"leave it out, or choose a laxer mode: --mode {other} (or set \"mode\" in the project file or in "
                f"{l.defaults_file()})" if other else
                f"the administrator's policy ({l.POLICY_FILE}) requires at least the mode {floor}, so it cannot be "
                "used")
        raise ValueError(f"{given} widens the security mode {name} ({a.mode_origin}): {why}; {hint}")
    mode = MODES[name]
    scope = mode.get("approval_scope", "always")
    if SCOPES.index(scope) < SCOPES.index(getattr(a, "max_approval_scope", "always")):
        a.max_approval_scope = scope
    a.mode_other_workspaces_ro = bool(mode.get("other_workspaces_ro"))
    a.mode_canaries = mode.get("canaries")  # canary.categories() and canary.decisions() weigh it
    a.mode_rules = ruleset(l, a, name)


def ruleset(l, a, name):
    """The mode's own ruleset (version 1, see the README's Rulesets), or None. high asks before a write
    (POST, PUT, PATCH, DELETE) to a host other than the agent's provider; paranoid refuses every HTTP request but
    the provider's. Both escalate once the run is tainted (taint_settings)."""
    kind = MODES[name].get("rules")
    if not kind:
        return None
    provider = list(l.PROVIDERS.get(a.agent, []))
    rules = [{"id": "mode-provider", "host": provider, "decision": "allow", "priority": 10,
              "reason": f"security mode {name}: the agent's provider"}] if provider else []
    if kind == "ask-writes":
        rules.append({"id": "mode-writes", "host": "*", "methods": WRITE_METHODS, "decision": "ask",
                      "reason": f"security mode {name}: a write to a host other than the agent's provider"})
        # No devices in high and paranoid: also refused by the mode's options; the ruleset says so for devices
        # handed over during the run (sandbox-ai devices add).
        return {"version": 1, "description": f"security mode {name}", "defaults": {"devices": "deny"}, "http": rules,
                **taint_settings(name)}
    return {"version": 1, "description": f"security mode {name}", "defaults": {"http": "deny", "devices": "deny"},
            "http": rules, **taint_settings(name)}


# What `sandbox-ai net allow` can open during a run, as the start option it stands for, with a sample value.
RUNTIME_OPTIONS = (("--allow", "192.0.2.1"), ("--allow-host", "example.org"), ("--allow-tcp", "192.0.2.1:443"),
                   ("--dns", None), ("--allow-host-port", "3000"))


def runtime_record(l, a):
    """The mode of a run for run.json and the supervisor (config.json): {"name", "origin", "refuses",
    "approval_scope"}. REFUSES: the start options whose run-time counterparts (sandbox-ai net allow, presets
    included) the mode refuses, found by the same checks as at the start."""
    name = getattr(a, "mode", None) or DEFAULT
    options = [[option, value] if value else [option] for option, value in RUNTIME_OPTIONS] + \
        [[f"--allow-{p}"] for p in l.PRESETS]
    refuses = [option[0] for option in options
               if refusal(l, a.agent, ["--backend", a.backend, *option], name)] if name in STRICT else []
    return {"name": name, "origin": getattr(a, "mode_origin", "default"), "refuses": refuses,
            "approval_scope": MODES[name].get("approval_scope", "always")}


def runtime_refusal(record, options, run):
    """Why the mode RECORD (runtime_record, from run.json) refuses the run-time OPTIONS (start options) of the
    run RUN, or None."""
    mode = record if isinstance(record, dict) else {}
    refused = [option for option in options if option in (mode.get("refuses") or [])]
    if not refused:
        return None
    name = mode.get("name", "?")
    laxer = next((n for n in reversed(NAMES[:rank(name)]) if n not in STRICT), DEFAULT) if name in NAMES else DEFAULT
    return (f"{' '.join(refused)} widens the security mode {name} of {run} ({mode.get('origin', '?')}): a running "
            f"sandbox opens nothing its mode refuses; answer its questions instead (sandbox-ai approvals), or "
            f"start it again with a laxer mode (--mode {laxer})")


def plan(a):
    """The mode in the dry-run plan: where it came from and what it set."""
    return {"name": a.mode, "origin": a.mode_origin, "options": a.mode_options,
            "approval_scope": getattr(a, "max_approval_scope", "always"),
            "other_workspaces_read_only": getattr(a, "mode_other_workspaces_ro", False),
            "rules": getattr(a, "mode_rules", None) is not None}


# Options that weaken the sandbox or widen what it may do or see, by long name: a trust question (the app's, the
# web interface's) marks each of them in a project file before it is trusted. The short forms (-A, -x, -kp ...)
# are found through the launcher's own canonical_options (weakening_options), never by a copy of the aliases.
WEAKENING_OPTIONS = frozenset({
    # the network and the proxy
    "--egress-all-allow", "--allow", "--allow-tcp", "--allow-host", "--allow-http", "--allow-host-port", "--allow-pypi",
    "--allow-github", "--allow-gitlab", "--allow-kernel-org", "--allow-duckduckgo", "--allow-npm", "--frontier",
    "--profile", "--dns", "--nameserver", "--network", "--ip", "--ip6", "--no-proxy", "--no-inspect", "--ask",
    "--local-config", "--llm", "--preset", "--preset-value", "--vpn-check",  # --vpn-check: a failing VPN used anyway
    "--vpn-bypass-host-ports", "--vpn-bypass-tcp",  # the host's ports, an address of your network past Tor
    # files, devices, displays and sound
    "--mount", "--copy", "--device", "--gpu", "--gui", "--gui-screen", "--gui-wayland", "--gui-vnc", "--audio",
    "--state-dir", "--kit", "--kit-dir", "--mcp", "--mcp-mount",  # --mcp-mount: a host folder an MCP server writes
    # logins, secrets and identities
    "--dangerously-skip-permissions", "--env", "--secret-env", "--api-key", "--publish", "--git-token-env",
    "--git-token-file", "--git-token-secret", "--git-credentials", "--git-identity", "--git-logins", "--identity",
    "--auth-file", "--import-codex-auth", "--copy-auth", "--with", "--helper",
    # what is noticed, checked and kept
    "--privacy-check", "--privacy-rules", "--canaries", "--no-canaries", "--keep", "--log-debug", "--log-dir",
    "--telemetry", "--llm-param",  # --llm-param loop_guard=off: the loop guard off
    # what runs the sandbox, and its limits
    "--runtime", "--binary", "--image", "--image-variant", "--podman-url", "--backend", "--memory", "--cpus", "--pids",
    "--tmp-size", "--nested-containers", "--service",  # --service: an image nobody reviewed, beside the agent
})


def option_items(l, tokens):
    """[(long name, value or None)] of the start options TOKENS (as a project file or a command line has them, up to
    an optional --): every short form, bundle and -x=VALUE as the launcher reads it (l.canonical_options), a value
    taken where the launcher's parser takes one. A word that is no option is (None, word)."""
    tokens = [str(token) for token in tokens]
    try:
        tokens = l.canonical_options(tokens)
    except (ValueError, SystemExit):
        pass
    actions = l.option_actions()
    items, index = [], 0
    while index < len(tokens):
        word = tokens[index]
        index += 1
        if word == "--":
            break
        if not word.startswith("-") or word == "-":
            items.append((None, word))
            continue
        name, equals, value = word.partition("=")
        action = actions.get(name)
        if equals:
            items.append((name, value))
        elif action is not None and action.nargs != 0 and index < len(tokens) and \
                (action.nargs in (None, 1) or not tokens[index].startswith("-")):
            values = [tokens[index]]
            index += 1
            while action.nargs in ("*", "+") and index < len(tokens) and not tokens[index].startswith("-"):
                values.append(tokens[index])
                index += 1
            items.append((name, " ".join(values)))
        else:
            items.append((name, None))
    return items


def weakening_options(l, tokens):
    """[(long name, value or None)] of the options in TOKENS that weaken the sandbox (WEAKENING_OPTIONS), however
    they are written (option_items)."""
    return [(name, value) for name, value in option_items(l, tokens) if name in WEAKENING_OPTIONS]


CANARY_DEFAULT = ("credentials",)  # the canaries a project file is measured against without the human's own choice


def project_marks(l, document, defaults=None):
    """What in the project file DOCUMENT (.sandbox-ai.json) weakens the sandbox, for a trust question to mark: a list
    of {"key", "why"} (WHY: mode, canaries, keep, log_debug, rules, mcp_servers, workspaces, agent_args, option -
    with "option" and "value" -, workspace - with "value" and "read_only" -). DEFAULTS: {"mode", "canaries"} of the
    host the file is on (defaults.json there; {} or None: sandbox-ai's own); a laxer mode or fewer canaries than
    those weaken. Options are read as the launcher reads them (weakening_options), every workspace is named."""
    defaults = defaults if isinstance(defaults, dict) else {}
    marks = []
    default = defaults.get("mode") if defaults.get("mode") in NAMES else DEFAULT
    mode = document.get("mode")
    if isinstance(mode, str) and mode in NAMES and rank(mode) < rank(default):
        marks.append({"key": "mode", "why": "mode", "default": default})
    if "canaries" in document:
        canaries, wanted = document.get("canaries"), defaults.get("canaries", list(CANARY_DEFAULT))
        wanted = set(wanted) if isinstance(wanted, list) else set()
        chosen = set(canaries) if isinstance(canaries, list) else set()
        if canaries is False or not chosen or not wanted <= chosen:
            marks.append({"key": "canaries", "why": "canaries"})
    for key in ("keep", "log_debug"):
        if document.get(key) is True:
            marks.append({"key": key, "why": key})
    if document.get("rules") is not None:
        marks.append({"key": "rules", "why": "rules"})
    if document.get("mcp_servers"):
        marks.append({"key": "mcp_servers", "why": "mcp_servers"})
    options = document.get("options") if isinstance(document.get("options"), list) else []
    marks += [{"key": "options", "why": "option", "option": name, "value": value}
              for name, value in weakening_options(l, options)]
    workspaces = document.get("workspaces")
    if isinstance(workspaces, list) and workspaces:
        read_only = any(name == "--workspace-ro" for name, _ in option_items(l, options))
        marks.append({"key": "workspaces", "why": "workspaces"})
        marks += [{"key": "workspaces", "why": "workspace", "value": str(item), "read_only": read_only}
                  for item in workspaces]
    if isinstance(document.get("agent_args"), list) and document["agent_args"]:
        marks.append({"key": "agent_args", "why": "agent_args"})
    return marks
