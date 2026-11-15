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
from pathlib import Path
import re

HERE = Path(__file__).resolve().parent

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
# sandbox-ai dsh without arguments: sandbox-ai's chat in the terminal (CHAT_SCRIPT, sandbox/dsh_chat.py). DSH has no
# chat in the terminal of its own: its shipped profiles (dsh-app-boot's PROFILE_TEMPLATES) are acp, web, headless (one
# task), sdk and sdk-minimal. headless keeps a conversation going from run to run (--session-id adopts a persisted
# Session, --json names the run's in its first event), so the chat runs it once per message. CHAT ("chat",
# sandbox-ai's own word, no profile of DSH's) names it explicitly: `sandbox-ai dsh -- chat --session-id ID` continues
# a conversation of an earlier start.
CHAT = "chat"
CHAT_SCRIPT = MANAGED_DIR + "/chat.py"  # sandbox/dsh_chat.py, in the session directory's dsh/ (read-only)
# The headless profile turns DSH's managed credential store off (keys from the launch environment only); the chat is
# the interactive DSH, so its runs read the store as the web UI does (the login sandbox-ai syncs, an account signed in
# in the web UI): DSH's own rows for that (dsh-headless' README, "Credentials"), in a patch before sandbox-ai's own,
# which stays the last word.
CHAT_PATCH = MANAGED_DIR + "/sandbox-ai-chat.patch.yml"
CHAT_ROWS = [{"id": "credentials", "disabled": False}, {"id": "authorization", "disabled": False},
             {"id": "deepseek-account", "disabled": False}]
# -- web: the web UI on the sandbox's loopback, for the sandbox's own browser (--browser: the same pod, the same
# loopback). --no-open: there is no browser in the agent's container to open; with --browser the launcher opens its
# URL in the session browser (WEB_SCRIPT, session_opener). The chat with --browser starts it beside itself.
WEB_PORT = 3080
WEB_ARGS = ("web", "--no-open", "--host", "127.0.0.1", "--port", str(WEB_PORT))
WEB_URL = f"http://127.0.0.1:{WEB_PORT}/"
# The web UI's launch URL (`dsh web: URL`): the sandbox's loopback, a 32-byte token in base64url
# (dsh-client-connection).
WEB_TOKEN_URL = re.compile(r"http://127\.0\.0\.1:[0-9]{1,5}/\?token=[A-Za-z0-9_-]{16,128}")
TOKEN = re.compile(r"(\?token=)[A-Za-z0-9_-]+")
WEB_SCRIPT = MANAGED_DIR + "/web.py"  # sandbox/dsh_web.py, in the session directory's dsh/ (read-only)
BROWSER_OPENS = 5  # times a run hands the web UI to the session browser (DSH started again in its window)
CHAT_WHAT = ("DeepSeek Harness' chat in this terminal (sandbox-ai's: each message runs DSH's headless mode, one "
             "conversation kept by its session id; /help lists the commands, Ctrl+C cancels an answer)")
APPROVALS = ("Approvals cannot be answered in the chat: an action DSH would ask about is refused (a start with -D "
             "asks nothing)")
CHAT_NOTE = (CHAT_WHAT + ". " + APPROVALS + ". Its web UI as well, where DSH asks you: start with --browser (the "
             "app's Browser tab)")
CHAT_AND_BROWSER = (CHAT_WHAT + ", and its web UI in the session browser (the app's Browser tab) once it is up - a "
                    "conversation of its own there, and where DSH asks you. " + APPROVALS)
WEB_ONLY = ("DeepSeek Harness' web UI (-- web) runs on the sandbox's own loopback, which no browser outside the "
            "sandbox reaches: start with --browser to use it in the app's Browser tab (in the app: the Board's "
            "Browser part). The chat in the terminal: start without -- web; one task: sandbox-ai dsh -- headless "
            "\"TASK\"")
IN_BROWSER = ("DeepSeek Harness' web UI opens in the session browser (the app's Browser tab) once it is up; this "
              "window shows its log (the chat in the terminal: start without -- web)")
OPENED = "sandbox-ai: DeepSeek Harness' web UI opened in the session browser (the app's Browser tab)"
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
    if args[:1] == [CHAT]:
        chat_session(args)  # refuses what the chat does not take
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


def model_rows(a, llm=None, note=None):
    """The patch's rows for the model: --llm's route (LLM: llm.dsh_config) as the default model, else --model with
    DSH's own DeepSeek route; the system prompt's persona with the loop guard's instructions and the run's network
    note (NOTE: agent_sandbox.network_note) after it, one row (a row replaces its whole configuration)."""
    rows = []
    if llm:
        rows = [{"id": "llm-pi-ai", "config": {"providers": {LLM_ROUTE: llm["route"]}}},
                {"id": "agent-default-model", "config": {"provider": LLM_ROUTE, "model": llm["model"]}}]
    elif getattr(a, "model", None):
        rows = [{"id": "agent-default-model", "config": {"provider": "deepseek-official", "model": a.model}}]
    added = [text for text in ((llm or {}).get("instructions"), note) if text]
    if added:
        parts = [PERSONA, *(text.rstrip("\n") for text in added[:-1]), added[-1]]
        rows.append({"id": "system-prompt", "config": {"personaPrefix": "\n\n".join(parts)}})
    return rows


def patch(a, mcp=None, llm=None, note=None):
    """DSH's /etc/dsh/sandbox-ai.patch.yml for this start: the model rows (with NOTE, the run's network note, in the
    system prompt), the --mcp servers (MCP: mcp_rows), and, last, the telemetry pins. A patch row replaces its row's
    whole configuration (DSH's rule)."""
    rows = model_rows(a, llm, note)
    if mcp:
        rows.append({"insert": list(mcp)})
    return [*rows, *TELEMETRY_PINNED]


def files(a, rows):
    """{path in the session directory: content}: the patch; for the chat its script (CHAT_SCRIPT) and patch
    (CHAT_PATCH), and the web UI's window script (WEB_SCRIPT), which the chat runs beside it with --browser."""
    found = {f"{SESSION_DIR}/sandbox-ai.patch.yml": json.dumps(rows, indent=2) + "\n"}
    args = getattr(a, "agent_args", None) or []
    if chat_profile(args):
        found[f"{SESSION_DIR}/{CHAT_SCRIPT.rsplit('/', 1)[1]}"] = (HERE / "dsh_chat.py").read_text()
        found[f"{SESSION_DIR}/{CHAT_PATCH.rsplit('/', 1)[1]}"] = json.dumps(CHAT_ROWS, indent=2) + "\n"
    if interactive(args):
        found[f"{SESSION_DIR}/{WEB_SCRIPT.rsplit('/', 1)[1]}"] = (HERE / "dsh_web.py").read_text()
    return found


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


def web_port(taken=()):
    """The web UI's port on the sandbox's loopback: WEB_PORT, or - when that one is TAKEN (a port of the host relayed
    there, --allow-host-port or a git login's, or a service's: a connection to it would not reach DSH) - the next
    free one up to WEB_PORT + 19."""
    taken = {int(port) for port in taken}
    return next((port for port in range(WEB_PORT, WEB_PORT + 20) if port not in taken), WEB_PORT)


def web_args(port=WEB_PORT):
    """The web UI on the sandbox's loopback at PORT (web_port), sandbox-ai's patch right after the profile."""
    return [WEB_ARGS[0], "--patch", PATCH, *WEB_ARGS[1:-1], str(port)]


def chat_args():
    """The chat's DSH for each message (the chat adds --json, --session-id and "-"): headless with the chat's patch
    (CHAT_PATCH) and sandbox-ai's own last."""
    return ["headless", "--patch", CHAT_PATCH, "--patch", PATCH]


def command_args(args, port=WEB_PORT):
    """DSH's arguments with sandbox-ai's patch: right after the profile's name (DSH's launcher reads its own flags
    there); none given or chat: the chat's own (CHAT and its options: command builds its command line); web alone:
    the web UI on the sandbox's loopback (web_args, at PORT: web_port)."""
    args = profile_words(args)
    if chat_profile(args):
        return [CHAT, *args[1:]]
    if args in (["web"], ["--profile", "web"]):
        return web_args(port)
    if args[0] == "--profile" and len(args) > 1:
        return [args[0], args[1], "--patch", PATCH, *args[2:]]
    if args[0].startswith("-"):
        return [*args]  # the launcher's own flags (--help, --version, --dump-config ...)
    return [args[0], "--patch", PATCH, *args[1:]]


def chat_profile(args):
    """DSH's arguments ARGS (as given) start sandbox-ai's chat in the terminal: none, or chat."""
    args = profile_words(args)
    return not args or args[0] == CHAT


def chat_options(args):
    """(--session-id, --prompt) of the chat's arguments ARGS (chat [--session-id ID] [--prompt TEXT]), each None when
    not given; anything else: ValueError. --prompt TEXT is the chat's first message, sent as it starts (a team
    member's role, team.py), then the chat waits for yours."""
    rest, found = list(args[1:]), {"--session-id": None, "--prompt": None}
    while rest:
        option = rest.pop(0)
        name, equals, value = option.partition("=")
        if name in found and not equals and rest and rest[0].strip():
            found[name] = rest.pop(0)
        elif name in found and equals and value.strip():
            found[name] = value
        else:
            raise ValueError(f"dsh chat: {option!r} is not an option of the chat (only --session-id ID, the "
                             "conversation to continue, and --prompt TEXT, its first message); DSH's own profiles: "
                             "web, headless, acp, sdk, sdk-minimal")
    return found["--session-id"], found["--prompt"]


def chat_session(args):
    """The --session-id of the chat's arguments ARGS (chat_options), else None."""
    return chat_options(args)[0]


def web_profile(args):
    """DSH's arguments ARGS (as given) start its web UI: web, or --profile web."""
    args = profile_words(args)
    return args[:1] == ["web"] or args[:2] == ["--profile", "web"]


def interactive(args):
    """ARGS start the chat or the web UI: what the session browser can show (with --browser)."""
    return chat_profile(args) or web_profile(args)


def note(a, opens):
    """What an interactive start says first (and its window): the chat's (with OPENS the web UI beside it), the web
    UI's (OPENS: it goes to the session browser, else how to reach it); None for a start of another profile."""
    args = getattr(a, "agent_args", None) or []
    if chat_profile(args):
        return CHAT_AND_BROWSER if opens else CHAT_NOTE
    if not web_profile(args):
        return None
    return IN_BROWSER if opens else WEB_ONLY


def command(a, binary, args, opens=False, port=WEB_PORT):
    """DSH's command line (ARGS: command_args'): the chat through CHAT_SCRIPT, running BINARY's headless profile for
    each message (chat_args) and, OPENS, the web UI beside it (web_args at PORT, handed to the session browser); the
    web UI through WEB_SCRIPT, which says what DSH offers, masks its launch URL and, OPENS, hands it to the session
    browser; another profile as it is."""
    given = getattr(a, "agent_args", None) or []
    if chat_profile(given):
        session, first = chat_options(profile_words(given))
        shown = getattr(a, "show_thinking", None)  # --show-thinking: the chat's own switch (default: shown)
        return ["python3", "-I", CHAT_SCRIPT, "--notice", note(a, opens),
                *(["--session-id", session] if session else []), *(["--first", first] if first else []),
                *(["--thinking", "show" if shown == "on" else "hide"] if shown else []),
                *(["--web", json.dumps([binary, *web_args(port)])] if opens else []), "--", binary, *chat_args()]
    if not web_profile(given):
        return [binary, *args]
    return ["python3", "-I", WEB_SCRIPT, "--notice", note(a, opens), *(["--open"] if opens else []), "--",
            binary, *args]


def is_web_url(url):
    """URL is the web UI's launch URL on the sandbox's loopback (what the session browser is given), nothing else."""
    return isinstance(url, str) and WEB_TOKEN_URL.fullmatch(url) is not None


def mask(text):
    """TEXT with the value of every ?token= masked (the web UI's launch URL in logs)."""
    return TOKEN.sub(r"\1***", str(text))


def session_opener(request_open, opens=BROWSER_OPENS, log=None):
    """The launcher's answer to the sandbox's requests to open a URL (approvals.browser_reply's SESSION): the web UI's
    launch URL (is_web_url) goes to the session browser - REQUEST_OPEN(url) - at most OPENS times; any other URL:
    None (not this one's). LOG(text): a line without the URL."""
    count = [0]

    def answer(url):
        if not is_web_url(url):
            return None
        count[0] += 1
        if count[0] > opens:
            return {"ok": False, "message": "sandbox-ai: DeepSeek Harness' web UI was handed to the session browser "
                                            f"{opens} times in this run already"}
        try:
            request_open(url)
        except (OSError, ValueError) as exc:
            return {"ok": False, "message": f"sandbox-ai: the session browser did not take it ({exc})"}
        if log:
            log("DeepSeek Harness' web UI handed to the session browser")
        return {"ok": True, "message": OPENED}
    return answer


def plan(a, opens=False, port=WEB_PORT):
    """The dry run's "dsh": the reviewed release, the patch's place, the telemetry, and that it is experimental."""
    args = profile_words(getattr(a, "agent_args", None) or [])
    chat = chat_profile(args)
    loopback = f"http://127.0.0.1:{port}/" if args in ([], ["web"], ["--profile", "web"]) else WEB_URL
    return {"version": VERSION, "commit": COMMIT, "package": f"{PACKAGE}@{VERSION}", "repository": REPOSITORY,
            "experimental": True, "patch": PATCH, "home": HOME,
            "profile": CHAT if chat else args[1] if args[:1] == ["--profile"] and len(args) > 1 else args[0],
            "web_ui": loopback if web_profile(args) or chat and opens else None,
            "terminal_ui": chat, "chat": {"script": CHAT_SCRIPT, "patch": CHAT_PATCH, "runs": chat_args(),
                                          "session_id": chat_session(args)} if chat else None,
            "note": note(a, opens), "opens_in_session_browser": bool(opens and interactive(args)),
            "telemetry": {"switchable": False, "blocked_hosts": list(TELEMETRY_HOSTS),
                          "environment": sorted(TELEMETRY_ENV), "pinned": [row["id"] for row in TELEMETRY_PINNED]}}


def login_document_ok(document):
    """A DSH credential store (~/.dsh/.credentials.yaml, parsed): any entry."""
    return isinstance(document, dict) and bool(document)
