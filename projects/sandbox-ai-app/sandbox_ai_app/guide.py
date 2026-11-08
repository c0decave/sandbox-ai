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

from .i18n import N_, tr


@dataclass(frozen=True)
class Step:
    page: str   # a page key of the navigation (ui.PAGES)
    title: str
    text: str   # what the page is for
    try_: str   # what to do there first


TOUR = (
    Step("launch", N_("Start"),
         N_("Start an agent in a sandbox: pick the project folder and the agent, see what the start will do, "
            "and start it in the app, in a terminal window or as a session that keeps running."),
         N_("Choose a project folder, then press Dry run to see the launcher's verdict without starting.")),
    Step("board", N_("Board"),
         N_("One sandbox as nodes: the agent, its folders, the proxy, the network and everything else. The checks "
            "below follow every change; Apply writes .sandbox-ai.json after showing it."),
         N_("Double-click a part in the list on the left to add it: the guide helps you set it up.")),
    Step("sandboxes", N_("Sandboxes & sessions"),
         N_("Everything that runs or is kept: one-off runs, sessions, MCP servers, with pause, resume, stop and "
            "attach."),
         N_("Select a session to see its options and history.")),
    Step("approvals", N_("Approvals"),
         N_("Questions of running sandboxes: a connection that is not allowed waits here for your answer, once, "
            "for the run, or always."),
         N_("Leave the page open while an agent works, or let the tray tell you.")),
    Step("terminals", N_("Terminals"),
         N_("The terminals of builds, logins and sandboxes started in the app, and every running start to attach "
            "to, however it was started."),
         N_("Ctrl-b d detaches from a session; it keeps running.")),
    Step("connections", N_("Connections"),
         N_("Every connection the proxy saw: allowed, asked, refused, with the request and the rule that "
            "decided."),
         N_("Filter by host to see what an agent reached.")),
    Step("activity", N_("Activity"),
         N_("What happened in a run, in order: starts, tool calls, questions, connections, the taint."),
         N_("Pick a run to follow it from start to end.")),
    Step("statistics", N_("Statistics"),
         N_("Tokens, requests and tool calls per run and per agent."),
         N_("Compare two runs of the same project.")),
    Step("files", N_("Files"),
         N_("The files of a sandbox's HOME and workspaces, to look into and to copy in or out."),
         N_("Copy a file into a running sandbox without giving it a mount.")),
    Step("kits", N_("Kits & plugins"),
         N_("Skills, agents, commands, prompts, memory templates, hooks and plugins, in kits a project uses with "
            "--kit."),
         N_("Create a kit of your own, add a skill, and use it in the current project.")),
    Step("mcp", N_("MCP servers"),
         N_("MCP servers in sandboxes of their own, from your definitions and the catalogs, with their network "
            "and folders."),
         N_("Add a search server from the catalog and connect it on the board.")),
    Step("llm", N_("LLM servers"),
         N_("Model servers of your own, kept by name on this machine or a host: Ollama, LM Studio, llama.cpp, vLLM "
            "or another OpenAI-compatible one, local or behind a VPN. A start with --llm NAME reaches only that "
            "server; its key stays with the proxy."),
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
    #                       "model" (the agent's model names to pick, any other typed), "effort" (the agent's levels)
    choices: tuple = ()   # (value, label): for "choice"; for "switches" the value is the flag ("" for none)
    placeholder: str = ""
    needed: bool = False  # the node means nothing without it


FORMS = ("text", "path", "flag", "list", "choice", "switches", "model", "effort")
# The model names each agent knows for sure and its effort levels, as the launcher (AGENT_MODELS, AGENT_EFFORTS)
# has them; a test keeps both the same. An empty choice is the agent's own default.
AGENT_MODELS = {"claude": ("fable", "opus", "sonnet", "haiku"), "codex": (), "opencode": ()}
AGENT_EFFORTS = {"claude": ("low", "medium", "high", "xhigh", "max"),
                 "codex": ("minimal", "low", "medium", "high", "xhigh"), "opencode": ()}

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
                 (("claude", "Claude Code"), ("codex", "Codex"), ("opencode", "OpenCode")), needed=True),
        Question("model", N_("Model"), N_("The agent's model: pick one it knows or type another (Codex: its "
                                            "model name, OpenCode: provider/model); empty: its default."), "model"),
        Question("effort", N_("Effort"), N_("How hard the agent thinks (Claude Code: low to max, Codex: minimal to "
                                            "xhigh); empty: its default. OpenCode sets it in its own configuration."),
                 "effort"),
        Question("--dangerously-skip-permissions", N_("Act without asking"),
                 N_("The agent no longer asks before it runs a tool. The sandbox still holds, but it acts on its "
                    "own."), "flag"),
    ),
    "approvals": (
        Question("--ask", N_("Questions"), N_("Ask you before a connection the options do not allow, or refuse it "
                                              "at once."), "choice", (("on", N_("ask me")), ("off", N_("refuse")))),
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
    "mcp": (
        Question("name", N_("Server"), N_("The MCP server by name (the MCP servers page lists them); it runs in a "
                                          "sandbox of its own."), "text", placeholder="demo", needed=True),
    ),
    "llm": (
        Question("--llm", N_("LLM server"), N_("A server of sandbox-ai llm, NAME or NAME:MODEL: only its address is "
                                               "opened."), "text", needed=True),
    ),
    "network": (
        Question("--allow-host", N_("Hosts"), N_("Further hosts the agent may reach over HTTPS, one per line."),
                 "list", placeholder="api.example.org"),
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
    if question.form == "list":
        return [str(v) for v in value] if isinstance(value, list) else []
    if question.form == "choice":
        known = [v for v, _ in question.choices]
        return value if value in known else ({"mode": "ro", "--ask": "on", "--privacy-check": "public",
                                              "--backend": "podman", "--vpn-check": "stop",
                                              "agent": "claude"}.get(question.key, known[0] if known else ""))
    return "" if value is None else str(value)


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
        elif question.form == "list":
            items = [line.strip() for line in (answer if isinstance(answer, list) else str(answer).splitlines())
                     if str(line).strip()]
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
