"""The guide: a tour through every area of the app, and a helper that sets up a new node of the board step by
step. No Qt (guide_view.py shows it).

The tour has one step per page of the navigation: what the page is for and what to try there. The node helper
asks the few things that matter for a node of a kind, each with what it means, and turns the answers into the
node's properties: the launcher options it owns (a list for an option that can be given more than once, True for
a switch, a text otherwise) and the few properties of its own (a workspace's path, a mount's source, an MCP
server's name, the agent). What it asks is checked against the launcher's own options by the tests, so an answer
always means what the command line means. After the last question it shows what the board still finds wrong
with the node (board.problems for that node).
"""
from __future__ import annotations

from dataclasses import dataclass
import re

from .backend import LIST_OPTIONS
from .i18n import N_, tr
from .models import KnownModels


@dataclass(frozen=True)
class Step:
    page: str   # a page key of the navigation (ui.PAGES)
    title: str
    text: str   # what the page is for
    try_: str   # what to do there first


TOUR = (
    # The Start page: one start in two views, with the switch above the page (Board | Expert). Its two steps are its
    # two views (each a page key of the window): the Expert view first, as the pages' order has it; the page itself
    # opens as the Board.
    Step("launch", N_("Start"),
         N_("Start an agent in a sandbox. The page shows one start in two views, with a switch above it: Board (the "
            "start as parts and connections; the page opens with it) and Expert, shown here - the project folder, "
            "the agent, every option a field and the whole command line as text to edit or paste. Both are always "
            "the same start: what you change in one is in the other at once, Undo covers both, and Start runs the "
            "same command from either."),
         N_("Type --offline into the command line, then press Dry run to see the launcher's verdict without "
            "starting.")),
    Step("board", N_("Start: the Board"),
         N_("The Board view of the Start page: the same start as nodes - the agent, its folders, the proxy, the "
            "network and everything else; options no part shows are on the part Other options. The checks below "
            "follow every change; Apply writes .sandbox-ai.json after showing it. The Expert view shows this very "
            "start as fields and its command line."),
         N_("Double-click a part in the list on the left to add it: the guide helps you set it up. Then choose "
            "Expert above the page (or F6) and find it in the command line.")),
    Step("sandboxes", N_("Sandboxes & sessions"),
         N_("Everything that runs or is kept: one-off runs, sessions, MCP servers, with pause, resume, stop and "
            "attach; a running one gets folders, MCP servers and toolchains without a restart."),
         N_("Select a session to see its options and history.")),
    Step("terminals", N_("Terminals"),
         N_("The terminals of builds, logins and sandboxes started in the app, and every running start to attach to, "
            "however it was started. A tab's right-click menu has its session's actions."),
         N_("Ctrl-b d detaches from a session; it keeps running.")),
    Step("approvals", N_("Approvals"),
         N_("Questions of running sandboxes: a connection that is not allowed waits here for your answer, once, "
            "for the run, or always."),
         N_("Leave the page open while an agent works, or let the tray tell you.")),
    Step("files", N_("File manager"),
         N_("The files of a sandbox's HOME and workspaces, to look into and to copy in or out."),
         N_("Copy a file into a running sandbox without giving it a mount.")),
    Step("kits", N_("Kits & plugins"),
         N_("Skills, agents, commands, prompts, memory templates, hooks and plugins, in kits a project uses with "
            "--kit."),
         N_("Create a kit of your own, add a skill, and use it in the current project.")),
    Step("mcp", N_("MCP servers"),
         N_("MCP servers in sandboxes of their own, from your definitions and the catalogs, with their network and "
            "folders; From another host… copies one of another host here."),
         N_("Add a search server from the catalog and connect it on the board.")),
    Step("llm", N_("LLM servers"),
         N_("Model servers of your own, kept by name on this machine or a host: Ollama, LM Studio, llama.cpp, vLLM "
            "or another OpenAI-compatible one, local or behind a VPN. A start with --llm NAME reaches only that "
            "server; its key stays with the proxy. For all hosts… makes one reachable from every host."),
         N_("Add the server you run, then Check it for the agent you use.")),
    Step("images", N_("Images"),
         N_("The agents' images: build, update, and extend them with packages of your own."),
         N_("Build the image of the agent you use, once.")),
    Step("credentials", N_("Credentials"),
         N_("Logins, secrets and Git tokens: kept on this computer, handed to a sandbox only when a start names "
            "them."),
         N_("Log in to your agent once; every sandbox gets the login from here.")),
    Step("team", N_("Teams"),
         N_("Teams of agents on one workspace, on this machine or a host: one sandbox per agent, the lead hands the "
            "others tasks on the team's board."),
         N_("Start a team of Claude Code and Codex on the current project, then talk to the lead.")),
    Step("connections", N_("Connections"),
         N_("Every connection the proxy saw: allowed, asked, refused, with the request and the rule that "
            "decided."),
         N_("Filter by host to see what an agent reached.")),
    Step("activity", N_("Activity"),
         N_("What happened in a run, in order: starts, tool calls, questions, connections, the taint."),
         N_("Pick a run to follow it from start to end.")),
    Step("traffic", N_("LLM traffic"),
         N_("What an agent and its model exchanged, request by request: the messages, tool calls and results, the "
            "answer, and what the relay changed."),
         N_("Pick a run with --llm and read its last request as a chat.")),
    Step("statistics", N_("Statistics"),
         N_("Tokens, requests and tool calls per run and per agent."),
         N_("Compare two runs of the same project.")),
    Step("monitor", N_("System monitor"),
         N_("What the running sandboxes use now and over time: CPU, memory of the limit, disk IO, network and "
            "processes per run and per project, the host's totals, warnings with what helps."),
         N_("Sort by memory to see what holds the most; select a run for its history.")),
    Step("system", N_("System"),
         N_("What sandbox-ai needs on this computer, and whether it is there: Podman, Bubblewrap, the image, the "
            "network."),
         N_("Run the checks; each problem comes with what fixes it.")),
    Step("hosts", N_("Hosts"),
         N_("Other machines whose sandbox-ai this app manages over SSH, such as LXC containers; the web interface "
            "uses the same list."),
         N_("Add a host with its SSH address, then test it: the app runs sandbox-ai there as on this computer.")),
    Step("help", N_("Help"),
         N_("Everything explained again, in English and German: every page and feature of the app and the ideas "
            "behind sandbox-ai, searchable, with tours through whole tasks. Each (?) in the app leads here."),
         N_("Search for preset, then start the tour that builds and saves a preset of your own.")),
)


def tour_step(index):
    """The step at INDEX, clamped to the tour."""
    return TOUR[max(0, min(len(TOUR) - 1, int(index)))]


# -- the node helper --

@dataclass(frozen=True)
class Question:
    key: str              # the node property: a launcher option it owns (--...) or one of its own (path, name ...)
    label: str
    help: str
    form: str             # "text", "path", "flag", "list" (one per line), "choice", "switches" (one of some flags),
    #                       "model" (the agent's model names to pick, any other typed), "effort" (the agent's levels),
    #                       "thinking" (shown, hidden or the agent's default: thinking.py; greyed out without a switch)
    choices: tuple = ()   # (value, label): for "choice"; for "switches" the value is the flag ("" for none)
    placeholder: str = ""
    needed: bool = False  # the node means nothing without it


FORMS = ("text", "path", "flag", "list", "choice", "switches", "model", "effort", "thinking")
# The effort levels each agent takes, as the launcher (AGENT_EFFORTS) has them; a test keeps both the same. Per model
# the table of cloud models narrows them (models.efforts). AGENT_MODELS: the model names each agent knows for sure,
# the table's (sandbox-ai's cloud_models.json, read when first asked; the launcher's AGENT_MODELS). An empty choice is
# the agent's own default.
AGENT_MODELS = KnownModels()
AGENT_EFFORTS = {"claude": ("low", "medium", "high", "xhigh", "max"),
                 "codex": ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"),
                 "opencode": (), "opencode2": (),
                 "hermes": ("none", "minimal", "low", "medium", "high", "xhigh", "max"),
                 "dsh": (), "qwen": ("none", "low", "medium", "high", "xhigh", "max"),
                 "goose": ("off", "low", "medium", "high", "max"),
                 "openhands": ("none", "low", "medium", "high", "xhigh"),
                 "nanobot": ("none", "low", "medium", "high", "xhigh", "max"), "letta": (),
                 "pi": ("off", "minimal", "low", "medium", "high", "xhigh", "max")}

NODE_QUESTIONS = {
    "workspace": (
        Question("path", N_("Folder"), N_("The host folder the agent works in. The first workspace is /work in the "
                                          "sandbox, further ones /workspaces/NAME."), "path", needed=True),
        Question("readonly", N_("Read-only"), N_("The agent can read the workspaces but not change them (this holds "
                                                 "for every workspace)."), "flag"),
    ),
    "mount": (
        Question("source", N_("Host folder or file"), N_("What the sandbox gets in addition to its workspaces. "
                                                         "Never a folder with keys or logins."), "path", needed=True),
        Question("dest", N_("In the sandbox"), N_("Where it appears: below /mnt or /opt, or in /work."), "text",
                 placeholder="/mnt/data", needed=True),
        Question("mode", N_("Mode"), N_("Read-only unless the agent has to write there."), "choice",
                 (("ro", N_("read-only")), ("rw", N_("writable")))),
    ),
    "home": (
        Question("--state-dir", N_("HOME of your own"), N_("A folder that is the sandbox's HOME and stays. Empty: "
                                                           "the start keeps a HOME of its own anyway."), "path"),
        Question("login", N_("Login"), N_("The agent's login: found automatically (the login store's or this "
                                          "computer's), always copied, or none."), "switches",
                 (("", N_("automatic")), ("--copy-auth", N_("always copy it")), ("--no-auth", N_("none")))),
    ),
    "kits": (
        Question("--kit", N_("Kits"), N_("Kits by name, one per line: their skills, agents, commands and hooks "
                                         "join the sandbox (see the Kits & plugins page)."), "list", placeholder="egm"),
        Question("--no-default-kit", N_("Without the default kit"), N_("Leave out the kit every sandbox gets."),
                 "flag"),
        Question("--no-memory", N_("Without project memory"), N_("The agent starts without the notes it kept for "
                                                                 "this project."), "flag"),
    ),
    "agent": (
        Question("agent", N_("Agent"), N_("The coding agent that runs in the sandbox."), "choice",
                 (("claude", "Claude Code"), ("codex", "Codex"), ("opencode", "OpenCode"), ("opencode2", "OpenCode 2"),
                  ("hermes", "Hermes Agent"), ("dsh", "DeepSeek Harness"), ("qwen", "Qwen Code"),
                  ("goose", "Goose"), ("openhands", "OpenHands"), ("nanobot", "nanobot"), ("letta", "Letta Code"),
                  ("pi", "Pi")), needed=True),
        Question("model", N_("Model"), N_("The agent's model: pick one it knows (grouped by provider; typing "
                                            "filters them) or type another (Codex: its model name, OpenCode: "
                                            "provider/model); empty: its default."), "model"),
        Question("effort", N_("Effort"), N_("How hard the agent thinks (the choices list the levels the chosen "
                                            "model takes, or the agent's for a model it does not list); empty: its "
                                            "default. OpenCode, OpenCode 2, DSH and Letta Code set it per model in "
                                            "their own configuration."),
                 "effort"),
        Question("thinking", N_("Show thinking"), N_("Whether the agent shows its model's thinking (reasoning) in its "
                                                     "terminal: shown, hidden, or the agent's own default. Qwen Code, "
                                                     "OpenHands and Letta Code have no setting for it (Ctrl+O, a "
                                                     "click or Ctrl+T unfolds it in the session)."),
                 "thinking"),
        Question("--dangerously-skip-permissions", N_("Act without asking"),
                 N_("The agent no longer asks before it runs a tool. The sandbox still holds, but it acts on its "
                    "own."), "flag"),
    ),
    "approvals": (
        Question("--ask", N_("Questions"), N_("Ask you before a connection the options do not allow, refuse it at "
                                              "once, or allow every question at once without asking (each one "
                                              "logged)."), "choice",
                 (("on", N_("ask me")), ("off", N_("refuse")), ("allow", N_("⚠ always allow everything")))),
        Question("--rules", N_("Rulesets"), N_("Files of rules, one per line: they allow, ask or deny requests, "
                                               "MCP tool calls and the agent's own tools."), "list"),
    ),
    "proxy": (
        Question("enabled", N_("Connection proxy"), N_("Every connection passes it: logged, asked about, pushes "
                                                       "checked. Turn it off only if a tool cannot work with it."),
                 "flag"),
        Question("--no-inspect", N_("Hosts without TLS inspection"), N_("Hosts whose TLS the proxy passes through "
                                                                        "unopened, one per line (for tools that pin "
                                                                        "their certificates)."), "list"),
        Question("--privacy-check", N_("Privacy check of pushes"), N_("Which pushes are checked for private data "
                                                                      "before they leave."), "choice",
                 (("public", N_("to public hosts")), ("always", N_("every push")), ("off", N_("none")))),
    ),
    "git": (
        Question("--git-identity", N_("Your name and e-mail"), N_("Commits in the sandbox carry your Git identity."),
                 "flag"),
        Question("--git-token-env", N_("Push tokens"), N_("URL=VARIABLE, one per line: a token from your "
                                                          "environment for that Git server."), "list",
                 placeholder="https://github.com=GH_TOKEN"),
        Question("--token-proxy", N_("Token proxy"), N_("The proxy adds the token to each push: the agent never "
                                                        "sees it."), "flag"),
    ),
    "service": (
        Question("name", N_("Name"), N_("The service's name: the agent finds it in SANDBOX_AI_SERVICES, its logs are "
                                         "named after it."), "text", placeholder="db", needed=True),
        Question("image", N_("Image"), N_("One of your images (sandbox-ai images pull|build-image, the Images page): "
                                           "pinned by digest, unchecked."), "text", placeholder="redis", needed=True),
    ),
    "mcp": (
        Question("name", N_("Server"), N_("The MCP server by name (the MCP servers page lists them); it runs in a "
                                          "sandbox of its own."), "text", placeholder="demo", needed=True),
    ),
    "bridge": (
        Question("peer", N_("Other session"), N_("A running agent session of this machine (sandbox-ai list): the "
                                                 "bridge is made once it and this board's start run."), "text",
                 needed=True),
        Question("direction", N_("Direction"), N_("Who reaches whom: an observer of this agent reaches it, a helper "
                                                  "is reached by it."), "choice",
                 (("out", N_("this agent reaches the other session")),
                  ("in", N_("the other session reaches this agent")))),
        Question("message", N_("Send messages (message)"), N_("Notes into the other's inbox; more (ask, files, the "
                                                               "observer's actions) in the properties."), "flag"),
        Question("watch", N_("Observe B (watch)"), N_("The one that reaches observes the other and alerts you."),
                 "flag"),
    ),
    "team": (
        Question("name", N_("Name"), N_("The team's name: its members are the sessions NAME-AGENT, its board the MCP "
                                        "server team-NAME."), "text", placeholder="review", needed=True),
        Question("lead", N_("Lead"), N_("The agent that talks to you and hands the others tasks; the members, their "
                                        "roles and their own models in the properties."), "choice",
                 # (sandbox-ai's team agents, backend.TEAM_AGENTS: a test keeps both the same)
                 tuple((agent, agent) for agent in ("claude", "codex", "opencode", "hermes", "dsh", "qwen", "goose",
                                                    "openhands", "nanobot", "letta", "opencode2", "pi"))),
    ),
    "llm": (
        Question("--llm", N_("LLM server"), N_("A server of sandbox-ai llm, NAME or NAME:MODEL: only its address is "
                                               "opened."), "text", needed=True),
    ),
    "network": (
        Question("--allow-host", N_("Hosts"), N_("Further hosts the agent may reach over HTTPS, one or more per "
                                                 "line, separated by commas or spaces."),
                 "list", placeholder="api.example.org"),
        Question("--allow-domain", N_("Whole domains"), N_("Domains the agent may reach over HTTPS with every name "
                                                           "below them, one or more per line, separated by commas or "
                                                           "spaces (not github.io or co.uk)."),
                 "list", placeholder="example.org"),
        Question("--allow-pypi", N_("PyPI"), N_("Python packages."), "flag"),
        Question("--allow-github", N_("GitHub"), N_("GitHub and its downloads."), "flag"),
        Question("--offline", N_("Offline"), N_("No network at all, not even the agent's provider."), "flag"),
    ),
    "vpn": (
        Question("--vpn", N_("VPN profile"), N_("A profile of sandbox-ai vpn: everything leaves through it."), "text",
                 needed=True),
        Question("--vpn-check", N_("When the check fails"), N_("Stop the start, or only warn."), "choice",
                 (("stop", N_("stop")), ("warn", N_("warn")))),
    ),
    "devices": (
        Question("--device", N_("Devices"), N_("Host devices, one per line (/dev/ttyUSB0, /dev/kvm): each weakens "
                                               "the isolation."), "list"),
        Question("--gpu", N_("GPU"), N_("A GPU for the agent."), "choice",
                 (("", N_("none")), ("nvidia", "NVIDIA"), ("dri", "DRI (Intel, AMD)"), ("rocm", "ROCm"))),
    ),
    "toolchains": (
        Question("--toolchain", N_("Toolchains"), N_("Build tools from the signed catalog, one per line (go, rust, "
                                                     "jdk, node ...): mounted read-only at /opt/toolchains/NAME."),
                 "list"),
        Question("--toolchain-offline", N_("Without their registries"),
                 N_("Mount them, but keep the registries they name (the Go module proxy, crates.io ...) closed."),
                 "flag"),
    ),
    "limits": (
        Question("--memory", N_("Memory"), N_("At most this much, e.g. 8g."), "text", placeholder="8g"),
        Question("--cpus", N_("CPUs"), N_("At most this many CPUs, e.g. 4."), "text", placeholder="4"),
        Question("--pids", N_("Processes"), N_("At most this many processes."), "text", placeholder="4096"),
    ),
    "display": (
        Question("--gui", N_("Virtual display"), N_("A display for GUI programs and browsers in the sandbox."),
                 "flag"),
        Question("--gui-vnc", N_("Watch it"), N_("A folder for the display's VNC socket, to watch what happens."),
                 "path"),
    ),
    "browser": (
        Question("--browser", N_("Browser"), N_("Which browser runs beside the agent (its image: sandbox-ai build "
                                                "browser)."), "choice",
                 (("chromium", "Chromium"), ("firefox", "Firefox ESR")), needed=True),
        Question("--browser-control", N_("Who drives it"), N_("You (in its tab in the app), the agent (Chrome's "
                                                             "DevTools; Chromium only) or both."), "choice",
                 (("", N_("you")), ("agent", N_("the agent")), ("both", N_("you and the agent")))),
        Question("--browser-profile", N_("Profile"), N_("`fresh` (new at every start), `session` (kept with the session) "
                                                        "or a folder of yours."), "text", placeholder="fresh"),
    ),
    "cairn": (
        Question("--cairn", N_("Cairn's board"), N_("Cairn beside the agent (its image: sandbox-ai build cairn); its "
                                                    "board opens in the sandbox's browser."), "flag", needed=True),
        Question("--cairn-workers", N_("Workers"), N_("The session's agents that work for Cairn - any harness - each "
                                                      "with a model of its own if you like: claude=sonnet,codex. "
                                                      "Empty: every agent of the session, with its own model, the "
                                                      "best supported first."), "text",
                 placeholder="claude,codex"),
        Question("--cairn-origin", N_("Starting point"), N_("What is given: the target and the scope. With a goal, the "
                                                    "project is made at the start."), "text",
                 placeholder="The machine 10.10.10.5, its TCP ports 80 and 443"),
        Question("--cairn-goal", N_("Goal"), N_("What counts as done."), "text"),
        Question("--cairn-hint", N_("First hint"), N_("A first hint for the workers."), "text"),
        Question("--cairn-board", N_("Board data"), N_("Its database: new at every start, or kept with the session."),
                 "choice", (("", N_("new at every start")), ("session", N_("kept with the session")))),
    ),
    "artex": (
        Question("--artex", N_("ARTEX"), N_("ARTEX beside the agent (its image: sandbox-ai build artex); its console "
                                            "on this computer's 127.0.0.1, its password: sandbox-ai artex console."),
                 "flag", needed=True),
        Question("--artex-data", N_("ARTEX's data"), N_("Its database and findings: new at every start, or kept with "
                                                        "the session."),
                 "choice", (("", N_("new at every start")), ("session", N_("kept with the session")))),
        Question("--artex-port", N_("Console port"), N_("The port of this computer's 127.0.0.1 for its console. "
                                                        "Empty: 8787, or the next free one."), "text",
                 placeholder="8787"),
    ),
    "session": (
        Question("tmux", N_("tmux"), N_("Run the agent in tmux inside the sandbox (detach and attach again)."),
                 "switches", (("", N_("as the start decides")), ("--tmux", N_("always")), ("--no-tmux", N_("never")))),
    ),
    "logs": (
        Question("--log-dir", N_("Log folder"), N_("Where the connection logs go; the agent cannot read them."),
                 "path"),
        Question("--log-debug", N_("More detail"), N_("Keep what was refused in more detail."), "flag"),
    ),
    "engine": (
        Question("--backend", N_("Backend"), N_("Podman (containers) or Bubblewrap (the host's programs)."),
                 "choice", (("podman", "Podman"), ("bwrap", "Bubblewrap"))),
        Question("--image-variant", N_("Image variant"), N_("An image of your own with more packages (Images "
                                                            "page)."), "text"),
    ),
    # Other options: what no other part shows, an option per line as the command line has it ("tokens": each line
    # read as a shell reads it - option_lines, line_tokens).
    "other": (
        Question("tokens", N_("Options"),
                 N_("Options of the start that no other part shows, one per line as on the command line: the "
                    "security mode (--mode high), a name (--name night), canaries, checkpoints. The Expert view of "
                    "the Start page has a field for each."),
                 "list", placeholder="--mode high"),
    ),
}


def questions(kind):
    return NODE_QUESTIONS.get(kind, ())


def answer_of(node, question):
    """What NODE's properties say for QUESTION, in the question's form."""
    props = node.props
    if question.form == "switches":
        return next((value for value, _ in question.choices if value and props.get(value)), "")
    value = props.get(question.key)
    if question.form == "flag":
        if question.key == "enabled":  # the proxy is on unless turned off
            return value is not False
        return bool(value)
    if question.form == "list" and question.key == "tokens":
        return option_lines(value if isinstance(value, list) else [])
    if question.form == "list":
        return [str(v) for v in value] if isinstance(value, list) else []
    if question.form == "choice":
        known = [v for v, _ in question.choices]
        return value if value in known else ({"mode": "ro", "--ask": "on", "--privacy-check": "public",
                                              "--backend": "podman", "--vpn-check": "stop",
                                              "agent": "claude"}.get(question.key, known[0] if known else ""))
    return "" if value is None else str(value)


def option_lines(tokens):
    """Command-line TOKENS as lines, an option with its value on each (quoted as a shell needs it): what the part
    Other options shows of its tokens."""
    import shlex
    lines = []
    for token in (str(token) for token in tokens):
        if lines and not token.startswith("-") and lines[-1][0].startswith("-") and len(lines[-1]) == 1 \
                and "=" not in lines[-1][0]:
            lines[-1].append(token)
        else:
            lines.append([token])
    return [shlex.join(line) for line in lines]


def line_tokens(lines):
    """The tokens of LINES (option_lines' form: each line read as a shell reads it; a line whose quotes are not
    closed is one token, as it is)."""
    import shlex
    tokens = []
    for line in (str(line).strip() for line in lines):
        if not line:
            continue
        try:
            tokens += shlex.split(line)
        except ValueError:
            tokens.append(line)
    return tokens


def props_of(kind, answers):
    """{property: value or None} for the ANSWERS ({question key: answer}) of a node of KIND: None removes the
    property (an option at its default, as the inspector does)."""
    found = {}
    for question in questions(kind):
        if question.key not in answers:
            continue
        answer = answers[question.key]
        if question.form == "switches":
            for value, _ in question.choices:
                if value:
                    found[value] = True if answer == value else None
        elif question.form == "flag":
            if question.key == "enabled":
                found["enabled"] = bool(answer)
            elif question.key == "readonly":
                found["readonly"] = bool(answer)
            else:
                found[question.key] = True if answer else None
        elif question.form == "list" and question.key == "tokens":
            found[question.key] = line_tokens(answer if isinstance(answer, list) else str(answer).splitlines()) or None
        elif question.form == "list":
            lines = answer if isinstance(answer, list) else str(answer).splitlines()
            if question.key in LIST_OPTIONS:  # a line may list several (commas or spaces), each once
                items = list(dict.fromkeys(item for line in lines for item in re.split(r"[\s,]+", str(line)) if item))
            else:
                items = [str(line).strip() for line in lines if str(line).strip()]
            found[question.key] = items or None
        else:
            text = str(answer).strip()
            own = not question.key.startswith("--")
            found[question.key] = text if text or own else None
    return found


def missing(node):
    """The labels of the questions NODE needs and has no answer for."""
    return [question.label for question in questions(node.kind)
            if question.needed and not str(answer_of(node, question) or "").strip()]


def checklist(problems, node):
    """[(level, text)]: what is still wrong with NODE: unanswered questions it needs, then the board's problems
    that name it."""
    items = [("error", tr("{what} is not set yet", what=tr(label))) for label in missing(node)]
    items += [(problem.level, problem.text) for problem in problems if problem.node == node.id]
    return items
