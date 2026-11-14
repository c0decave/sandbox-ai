#!/usr/bin/env python3
"""DeepSeek Harness (dsh; deepseek-ai/deepseek-harness, MIT) as a sandbox-ai agent: its telemetry off for good, its
command line in a sandbox, and the read-only patch the launcher lays over its configuration.

DSH boots a profile: plugin bundles, then the profile's and the home's cordis.patch.yml, then the --patch overlays
given on its command line - the last word. The launcher writes its patch (JSON, which YAML reads) into the session
directory, mounts it read-only at /etc/dsh and passes it with --patch right after the profile's name. DSH v0.2.1-rc.5
reaches out with nothing but its model provider and its session telemetry, so there is no --dsh-enable (Hermes'
--hermes-enable exists for the features Hermes has; DSH has none to switch).

The review behind every default here: docs/DSH_REVIEW.md (DeepSeek Harness 0.2.1-rc.5).

Standard library only.
"""
import json

# The release reviewed and built into localhost/dsh-sandbox (Containerfile.dsh): the newest one at least 14 days old
# when it was taken in (DSH publishes pre-releases only: a developer preview). Dependencies locked with versions
# published before LOCK_BEFORE (scripts/agent_npm_lock.py, Containerfile.dsh.lock).
PACKAGE = "@deepseek-ai/dsh"
VERSION = "0.2.1-rc.5"
COMMIT = "b830ec950faba0a46dda564be3e2d854afed94f1"
INTEGRITY = "sha512-WkyJZv4zl9sCAxSzRctGm58m0rilGqwR6pniBiLOz5CPDfHfDuAUicg0AAazXI7oj/Cph6/cjNTncA/rpLTXpw=="
LOCK_BEFORE = "2026-10-30T00:00:00Z"
REPOSITORY = "https://github.com/deepseek-ai/deepseek-harness"
MANAGED_DIR = "/etc/dsh"
SESSION_DIR = "dsh"  # in the session directory, mounted read-only at MANAGED_DIR
PATCH = MANAGED_DIR + "/sandbox-ai.patch.yml"
EXPERIMENTAL = "dsh"  # not live-tested yet: the start says so (a note, never a refusal)
NAME = "DeepSeek Harness"
HOME = "/root/.dsh"  # DSH_HOME: sessions, profiles, its credentials, AGENTS.md, skills
# The profiles DSH ships (dsh --profile NAME); desktop is the Electron app's, refused by the npm CLI.
PROFILES = ("web", "headless", "acp", "sdk", "sdk-minimal")
# sandbox-ai dsh without arguments: the web UI on the sandbox's loopback, for the sandbox's own browser (--browser:
# the same pod, the same loopback). --no-open: there is no browser in the agent's container to open.
WEB_PORT = 3080
DEFAULT_ARGS = ("web", "--no-open", "--host", "127.0.0.1", "--port", str(WEB_PORT))
WEB_URL = f"http://127.0.0.1:{WEB_PORT}/"
LLM_ROUTE = "sandbox-ai"  # --llm's provider route (dsh-llm-pi-ai), its key in KEY_VARIABLE
KEY_VARIABLE = "SANDBOX_AI_LLM_KEY"
PERSONA = "You are a coding agent powered by the {{model}} model."  # web's and headless' personaPrefix, upstream
DANGER_MODE = "danger-full-access"  # DSH_PERMISSION_MODE for --dangerously-skip-permissions: no approvals
MCP_NAME = r"[A-Za-z0-9_-]{1,32}"  # dsh-mcp-client's serverName

# Telemetry is never switchable for DSH (the user's rule): its session telemetry (a session-log prefix with an
# anonymous user id to DeepSeek's collector, after feedback) and the OTel service under it are disabled in the patch,
# the opt-out variable is set for every dsh in the sandbox and the collector's URL points at a closed port; the
# collector is never opened and the connection proxy refuses it in every DSH run.
TELEMETRY_HOSTS = ("dsh-otel-collector.deepseeksvc.com",)
TELEMETRY_ENV = {
    "DSH_TELEMETRY_DISABLED": "1",  # any non-empty value: the launcher disables the telemetry row itself
    "DSH_TELEMETRY_OTLP_URL": "http://127.0.0.1:9/v1/logs",  # discard: should a row ever be enabled again
    "OTEL_SDK_DISABLED": "true", "DO_NOT_TRACK": "1",
}
TELEMETRY_PINNED = [{"id": "session-telemetry-otel", "disabled": True}, {"id": "otel", "disabled": True}]


def check(a, hooks_active=False, companions=()):
    """Refuse what DSH cannot do in a sandbox (yet), before anything starts."""
    if a.agent != "dsh" and "dsh" not in companions:
        return
    if a.agent != "dsh" or companions:
        raise ValueError("--with: DeepSeek Harness is not in the team image; give it another agent as a helper "
                         "(sandbox-ai dsh --helper claude) or it to another agent (--helper dsh)")
    if hooks_active:
        raise ValueError("rules for the agent's own tools (a ruleset's \"tool\" rules, the security mode "
                         "paranoid's) are not enforced for DeepSeek Harness yet; start without them, in the mode high "
                         "or laxer (the network, MCP and device rules hold as for every agent)")
    args = profile_words(getattr(a, "agent_args", None) or [])
    if args and args[0] in ("desktop", "plugin") or args[:2] in (["--profile", "desktop"], ["--profile", "plugin"]):
        raise ValueError(f"dsh {args[0] if args[0] != '--profile' else args[1]}: not in a sandbox (the desktop "
                         "profile is the Electron app's; plugins need pnpm and a registry, and are code in the agent's "
                         "process: a new one is a review)")
    for arg in args:
        if arg.split("=", 1)[0] == "--patch" and "--" not in args[:args.index(arg)]:
            raise ValueError("--patch in DSH's arguments: sandbox-ai gives DSH its own patch (read-only, the last "
                             "layer); put your settings in the profile's or the home's cordis.patch.yml")
    check_telemetry(a)


def host_matches_any(host, pattern):
    """HOST and PATTERN (each a name or *.DOMAIN) can name the same server (the launcher's rule)."""
    host, pattern = host.lower().rstrip("."), pattern.lower().rstrip(".")
    bare = lambda value: value[2:] if value.startswith("*.") else value  # noqa: E731
    return host == pattern or bare(host).endswith("." + bare(pattern)) or bare(pattern).endswith("." + bare(host)) \
        or bare(host) == bare(pattern)


def telemetry_hosts_in(values):
    """The TELEMETRY_HOSTS that host names or *.DOMAIN patterns VALUES would reach."""
    return sorted({telemetry for value in values for telemetry in TELEMETRY_HOSTS
                   if host_matches_any(str(value), telemetry)})


def drop_fix(a):
    """The fix of a start that opens a telemetry host: the --allow-domain that opened one (it became --allow-host
    DOMAIN '*.DOMAIN' right after the options were read), else that --allow-host."""
    for value in getattr(a, "allow_domain", None) or []:
        name = str(value).lower()
        name = (name[2:] if name.startswith("*.") else name.lstrip(".")).rstrip(".")
        if name and telemetry_hosts_in([name, "*." + name]):
            return f"drop --allow-domain {name}"
    return "drop that --allow-host"


def check_telemetry(a):
    """DSH's telemetry is never reachable: refused, a start that would open its collector (--allow-host, a wildcard
    over it), let it pass the proxy unseen (--no-inspect), or open everything without the proxy (--egress-all-allow
    with --no-proxy)."""
    opened = telemetry_hosts_in(getattr(a, "allow_host", []) or [])
    if opened:
        raise ValueError(f"DeepSeek Harness' telemetry is always off: {', '.join(opened)} is never opened for DSH; "
                         + drop_fix(a))
    unseen = telemetry_hosts_in(getattr(a, "no_inspect", []) or [])
    if unseen:
        raise ValueError(f"DeepSeek Harness' telemetry is always off: the proxy must see {', '.join(unseen)} to "
                         "refuse it; drop that --no-inspect")
    if getattr(a, "egress_all_allow", False) and getattr(a, "no_proxy", False):
        raise ValueError("DeepSeek Harness' telemetry is always off: --egress-all-allow without the connection "
                         "proxy would let it out; keep the proxy (drop --no-proxy)")


def rule_hosts():
    """The hosts the proxy's rules refuse: each of TELEMETRY_HOSTS, and for a *.DOMAIN DOMAIN itself as well (a rule
    for *.DOMAIN covers only the names below it; telemetry_hosts_in counts DOMAIN as one of them)."""
    return list(dict.fromkeys(name for host in TELEMETRY_HOSTS
                              for name in ((host, host[2:]) if host.startswith("*.") else (host,))))


def telemetry_rules():
    """The proxy's rules for every DSH run (the ruleset "telemetry"): its collector refused (rule_hosts())."""
    return [{"id": f"telemetry-dsh-{number}", "host": host, "path": "/**", "decision": "deny",
             "reason": "DeepSeek Harness' telemetry is always off"} for number, host in enumerate(rule_hosts(), 1)]


def experimental_note():
    return ("DeepSeek Harness: not live-tested yet (its image and a real start have not passed their "
            "live test)")


def mcp_rows(names, bridge_args, timeouts=None):
    """dsh-mcp-client rows for the --mcp servers NAMES: each the launcher's stdio bridge (BRIDGE_ARGS(name)), with
    its wait for one call from TIMEOUTS ({name: seconds}, the launcher's mcp_tool_timeouts; DSH counts
    milliseconds); a name not in it keeps DSH's own."""
    timeouts = timeouts or {}
    rows = []
    for name in dict.fromkeys(names):
        server = name if len(name) <= 32 else name[:32]
        rows.append({"id": f"sandbox-ai-mcp-{name}", "name": "@deepseek-ai/dsh-mcp-client",
                     "config": {"serverName": server, "transport": "stdio", "command": "python3",
                                "args": list(bridge_args(name)),
                                **({"toolCallTimeoutMs": timeouts[name] * 1000} if name in timeouts else {})}})
    return rows


def model_rows(a, llm=None):
    """The patch's rows for the model: --llm's route (LLM: llm.dsh_config) as the default model, else --model with
    DSH's own DeepSeek route."""
    if llm:
        rows = [{"id": "llm-pi-ai", "config": {"providers": {LLM_ROUTE: llm["route"]}}},
                {"id": "agent-default-model", "config": {"provider": LLM_ROUTE, "model": llm["model"]}}]
        if llm.get("instructions"):
            rows.append({"id": "system-prompt", "config": {"personaPrefix": PERSONA + "\n\n" + llm["instructions"]}})
        return rows
    if getattr(a, "model", None):
        return [{"id": "agent-default-model", "config": {"provider": "deepseek-official", "model": a.model}}]
    return []


def patch(a, mcp=None, llm=None):
    """DSH's /etc/dsh/sandbox-ai.patch.yml for this start: the model rows, the --mcp servers (MCP: mcp_rows), and,
    last, the telemetry pins. A patch row replaces its row's whole configuration (DSH's rule)."""
    rows = model_rows(a, llm)
    if mcp:
        rows.append({"insert": list(mcp)})
    return [*rows, *TELEMETRY_PINNED]


def files(a, rows):
    """{path in the session directory: content}: the patch."""
    return {f"{SESSION_DIR}/sandbox-ai.patch.yml": json.dumps(rows, indent=2) + "\n"}


def environment(a):
    """DSH's variables: its home and the telemetry opt-outs (for every dsh in the sandbox, also one the agent starts
    without the patch)."""
    return {"DSH_HOME": HOME, **TELEMETRY_ENV}


def profile_words(args):
    """ARGS with a first --profile=NAME as --profile NAME: the profile is read (and sandbox-ai's patch passed after
    it, a desktop or plugin profile refused) whichever way it is written."""
    args = list(args)
    if args and isinstance(args[0], str) and args[0].startswith("--profile="):
        return ["--profile", args[0].split("=", 1)[1], *args[1:]]
    return args


def command_args(args):
    """DSH's arguments with sandbox-ai's patch: right after the profile's name (DSH's launcher reads its own flags
    there); none given: the web UI on the sandbox's loopback (DEFAULT_ARGS)."""
    args = profile_words(args) or list(DEFAULT_ARGS)
    if args[0] == "--profile" and len(args) > 1:
        return [args[0], args[1], "--patch", PATCH, *args[2:]]
    if args[0].startswith("-"):
        return [*args]  # the launcher's own flags (--help, --version, --dump-config ...)
    return [args[0], "--patch", PATCH, *args[1:]]


def plan(a):
    """The dry run's "dsh": the reviewed release, the patch's place, the telemetry, and that it is experimental."""
    args = profile_words(getattr(a, "agent_args", None) or [])
    return {"version": VERSION, "commit": COMMIT, "package": f"{PACKAGE}@{VERSION}", "repository": REPOSITORY,
            "experimental": True, "patch": PATCH, "home": HOME,
            "profile": (args[1] if args[:1] == ["--profile"] and len(args) > 1 else args[0]) if args else "web",
            "web_ui": WEB_URL if not args or args[0] == "web" else None,
            "telemetry": {"switchable": False, "blocked_hosts": list(TELEMETRY_HOSTS),
                          "environment": sorted(TELEMETRY_ENV), "pinned": [row["id"] for row in TELEMETRY_PINNED]}}


def login_document_ok(document):
    """A DSH credential store (~/.dsh/.credentials.yaml, parsed): any entry."""
    return isinstance(document, dict) and bool(document)
