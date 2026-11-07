"""Presets: ready boards to start from, simple and complex, and the user's own. No Qt.

A preset is a board in a few lines: launcher options, MCP servers (registered ones by name, or the project's
own, defined here), a ruleset for the project, and placeholders (<vpn:search>, <llm:local>, <folder:samples>
...) for what only the user knows: a VPN profile, an LLM server, an MCP server, a folder. build() turns a
preset into a board (laid out, placeholders filled in where values are given); problems() of the board lists
every placeholder left as an error until it is filled in, so nothing half-made is written or started.

The user's own presets (the current board saved under a name) are kept in ~/.config/sandbox-ai/app/presets.json
(mode 0600) and can be exported and imported as JSON files. A preset never holds a secret: secrets stay in
sandbox-ai secrets, and options only name them. An imported file is untrusted data: every value is checked
(the launcher's own option parser, MCP definitions and rulesets checked by sandbox-ai itself), and a file with
anything that looks like a secret, or with options that copy a login in, is refused.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re

from . import backend
from . import board as B
from .i18n import N_, tr

CATEGORIES = (("simple", N_("Simple")), ("development", N_("Development")), ("research", N_("Research")),
              ("llm", N_("Local & remote LLM")), ("privacy", N_("Privacy & VPN")), ("teams", N_("Teams & policy")),
              ("sandbox-ai", N_("sandbox-ai's presets (sandbox-ai presets)")))
MINE = N_("My presets")
DIFFICULTIES = {"easy": N_("easy"), "medium": N_("medium"), "advanced": N_("advanced")}
BROWSER_SERVERS = ("browser", "playwright", "chrome", "chromium", "puppeteer")
FORMAT = "sandbox-ai-app preset"
LIMIT = 256 << 10  # bytes of a preset file
ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
# Options a preset may not carry: they copy a login in (a preset is meant to be passed on), or select what
# only a start decides.
REFUSED = ("--copy-auth", "--auth-file", "--import-codex-auth", "--git-credentials", "--workspace", "--config",
           "--no-config", "--session", "--dry-run", "--tmux-socket-dir", "--pod-name", "--mcp-socket-dir",
           "--mcp-approvals-dir")


@dataclass(frozen=True)
class Placeholder:
    marker: str  # <kind:name>, as it stands in the options (board.PLACEHOLDER)
    label: str  # what to fill in (N_ for the built-in presets)
    hint: str = ""  # where to get it
    optional: bool = False  # left empty: what holds it is left out instead
    follows: str = ""  # the marker of an LLM server whose VPN this one is (filled in from its registration)

    @property
    def kind(self):
        return B.PLACEHOLDER.fullmatch(self.marker)[1]


@dataclass(frozen=True)
class Preset:
    id: str
    title: str
    description: str
    category: str
    difficulty: str = "easy"
    options: tuple = ()
    agent: str | None = None  # None: the agent of the settings
    mcp: tuple = ()  # registered MCP servers (or placeholders for them)
    servers: tuple = ()  # (name, definition) of MCP servers the project defines itself
    rules: dict | None = None  # the project's ruleset (the approvals node)
    placeholders: tuple = ()
    notes: tuple = ()
    agent_args: tuple = ()
    positions: dict = field(default_factory=dict)  # {role key: [x, y]} (own presets: as they were saved)
    user: bool = False  # the user's own (texts are shown as they are, not translated)
    agents: tuple = ()  # the agents it is for (sandbox-ai's presets); () for any
    prompt: str = ""  # the first task (sandbox-ai's presets): a positional argument, for OpenCode --prompt

    def text(self, value):
        return value if self.user else tr(value)


def _search(vpn=None):
    """The DuckDuckGo search server (duckduckgo-mcp-server from PyPI, started with uvx), optionally behind VPN."""
    return {"command": ["uvx", "duckduckgo-mcp-server"],
            "options": (["--vpn", vpn] if vpn else []) + ["--allow-pypi", "--allow-duckduckgo"],
            "description": "DuckDuckGo search (duckduckgo-mcp-server from PyPI)"}


def _fetch(vpn=None):
    """The reference fetch server (mcp-server-fetch from PyPI): web pages as Markdown, any host."""
    return {"command": ["uvx", "mcp-server-fetch"],
            "options": (["--vpn", vpn] if vpn else []) + ["--allow-pypi", "--egress-all-allow"],
            "description": "Web pages as Markdown (mcp-server-fetch from PyPI)"}


GITHUB_RULES = {
    "version": 1,
    "description": "GitHub without gists and issue writes; other writes asked",
    "http": [
        {"id": "no-gists", "host": "api.github.com", "path": "/gists/**", "decision": "deny",
         "reason": "gists can publish workspace data"},
        {"id": "no-issue-writes", "host": "api.github.com", "methods": ["POST", "PATCH", "PUT", "DELETE"],
         "path": "/repos/*/*/issues/**", "decision": "deny", "reason": "issues and comments are public"},
        {"id": "writes", "host": "api.github.com", "methods": ["POST", "PATCH", "PUT", "DELETE"], "path": "/**",
         "decision": "ask", "priority": 200, "reason": "a change on GitHub"},
    ],
}
READ_ONLY_WEB_RULES = {
    "version": 1,
    "description": "Read-only web: GET and HEAD everywhere, every other request refused",
    "http": [{"id": "read-only", "host": "*", "methods": ["GET", "HEAD"], "decision": "allow"},
             {"id": "no-writes", "host": "*", "decision": "deny", "priority": 1000,
              "reason": "this project reads the web, it does not write to it"}],
}
RESEARCH_RULES = {
    "version": 1,
    "description": "Each page fetched is asked for",
    "mcp": [{"id": "ask-fetch", "server": "fetch", "tool": "fetch", "decision": "ask"}],
}

PRESETS = (
    # -- simple --
    Preset("locked", N_("Locked down (offline)"),
           N_("No network at all: the agent works on the project's files and nothing leaves."), "simple", "easy",
           ("--offline",),
           notes=(N_("Offline also means no provider: use it with a local model on this host, or for tools "
                     "that need no model."),)),
    Preset("development", N_("Development"),
           N_("The agent's provider plus PyPI, npm and GitHub through the connection proxy; push tokens stay in "
              "the proxy."), "simple", "easy",
           ("--allow-pypi", "--allow-github", "--allow-host", "registry.npmjs.org", "--token-proxy",
            "--git-identity"),
           notes=(N_("Add tokens on the Git node: the token proxy adds them to Git's requests, the agent never "
                     "sees them."),)),
    Preset("review", N_("Read-only review"),
           N_("The project read-only, no network but the agent's provider: for reviews and questions about "
              "code."), "simple", "easy",
           ("--workspace-ro", "--ask", "off"),
           notes=(N_("Nothing is asked: what the options do not allow is refused at once."),)),
    # -- development --
    Preset("database", N_("Web app with a database on this host"),
           N_("PyPI, npm and GitHub, PostgreSQL on this host's port 5432, tmux for long builds."), "development",
           "medium",
           ("--allow-pypi", "--allow-github", "--allow-host", "registry.npmjs.org", "--allow-host-port", "5432",
            "--tmux", "--token-proxy"),
           notes=(N_("A host port reaches this host's loopback past the sandbox's network: open only the ports "
                     "of services you started for the project."),)),
    Preset("kernel", N_("Systems programming"),
           N_("kernel.org, GitHub and GitLab, more memory, CPUs and processes for builds, without the default "
              "kit."), "development", "medium",
           ("--allow-kernel-org", "--allow-github", "--allow-gitlab", "--memory", "16g", "--cpus", "8",
            "--pids", "8192", "--no-default-kit")),
    # -- research --
    Preset("research", N_("Web research"),
           N_("The agent searches with DuckDuckGo and reads pages through two MCP servers of the project; each "
              "page fetched is asked for."), "research", "medium",
           (), servers=(("search", _search()), ("fetch", _fetch())), rules=RESEARCH_RULES,
           notes=(N_("The servers run in sandboxes of their own (uvx installs them from PyPI at their first "
                     "start); the agent itself reaches only its provider."),
                  N_("The fetch server reaches every web site: it has no files of yours.")),),
    Preset("browser", N_("Browser automation"),
           N_("A virtual display (Xvfb) for a browser the agent drives, PyPI and npm for Playwright, and "
              "approvals for every other site."), "research", "medium",
           ("--gui", "--allow-pypi", "--allow-host", "registry.npmjs.org"), mcp=("<mcp:browser>",),
           placeholders=(Placeholder("<mcp:browser>", N_("Browser MCP server"),
                                     N_("e.g. Playwright's (npx @playwright/mcp), defined with sandbox-ai mcp"),
                                     optional=True),),
           notes=(N_("Playwright is a candidate: `npx playwright install chromium` inside fetches the browser; "
                     "approvals ask before each new site."),)),
    Preset("binary-analysis", N_("Binary analysis, offline"),
           N_("Samples read-only, no network, analysis tools as MCP servers from the catalog (binwalk, YARA, "
              "capa ...)."), "research", "advanced",
           ("--offline", "--mount", "<folder:samples>:/mnt/samples:ro"),
           mcp=("<mcp:unpack>", "<mcp:scan>"),
           placeholders=(Placeholder("<folder:samples>", N_("Folder with the samples (read-only)")),
                         Placeholder("<mcp:unpack>", N_("MCP server that unpacks"),
                                     N_("e.g. sandbox-ai mcp add hub:binwalk"), optional=True),
                         Placeholder("<mcp:scan>", N_("MCP server that scans"),
                                     N_("e.g. sandbox-ai mcp add hub:yara"), optional=True)),
           notes=(N_("Catalog servers without network run with no network at all; the samples are mounted "
                     "read-only."),)),
    # -- LLM --
    Preset("local-llm", N_("Local model only"),
           N_("A model on this machine (Ollama, LM Studio, llama.cpp or vLLM): no cloud at all, the agent reaches "
              "nothing else."), "llm", "easy",
           ("--llm", "<llm:local>", "--no-provider-defaults"),
           placeholders=(Placeholder("<llm:local>", N_("Local LLM server"),
                                     N_("sandbox-ai llm add ollama --kind ollama --url http://127.0.0.1:11434")),),
           notes=(N_("Claude Code needs a server with the Anthropic API (Ollama, LM Studio, llama.cpp, vLLM); "
                     "a generic OpenAI-compatible server works with Codex and OpenCode."),)),
    Preset("remote-llm", N_("Remote vLLM behind Tailscale"),
           N_("A vLLM server in your tailnet: the sandbox runs behind the Tailscale profile and reaches only "
              "the model."), "llm", "advanced",
           ("--llm", "<llm:remote>", "--vpn", "<vpn:tailnet>", "--no-provider-defaults"),
           placeholders=(Placeholder("<llm:remote>", N_("Remote LLM server"),
                                     N_("sandbox-ai llm add gpu --kind vllm --url http://100.x.y.z:8000 --vpn ts")),
                         Placeholder("<vpn:tailnet>", N_("Tailscale profile the server is reached through"),
                                     N_("sandbox-ai vpn add ts --tailscale"), follows="<llm:remote>")),
           notes=(N_("An LLM server registered with --vpn brings its VPN along: the profile here must be the "
                     "same."),)),
    Preset("mixed-llm", N_("Cloud and local models (OpenCode)"),
           N_("OpenCode with its cloud provider and a local model side by side, plus PyPI and GitHub."), "llm",
           "medium",
           ("--llm", "<llm:local>", "--allow-pypi", "--allow-github"), agent="opencode",
           placeholders=(Placeholder("<llm:local>", N_("Local LLM server"),
                                     N_("sandbox-ai llm add ollama --kind ollama --url http://127.0.0.1:11434")),),
           notes=(N_("OpenCode lists both providers; choose the model per task."),)),
    # -- privacy and VPN --
    Preset("vpn-all", N_("Everything through a WireGuard VPN"),
           N_("Every connection leaves through your WireGuard tunnel, lookups through its resolvers; a failed "
              "check stops the start."), "privacy", "medium",
           ("--vpn", "<vpn:wireguard>", "--vpn-check", "stop", "--dns", "--allow-pypi", "--allow-github"),
           placeholders=(Placeholder("<vpn:wireguard>", N_("WireGuard profile"),
                                     N_("sandbox-ai vpn add home --wireguard wg.conf")),),
           notes=(N_("The kill switch lets nothing out but through the tunnel; the checks compare the address "
                     "seen outside with this host's."),)),
    Preset("vpn-mcp", N_("MCP servers behind different VPNs"),
           N_("Search through one VPN (e.g. Mullvad), page fetching through another (your own WireGuard); the "
              "agent reaches only its provider."), "privacy", "advanced",
           (), servers=(("search", _search("<vpn:search>")), ("fetch", _fetch("<vpn:fetch>"))),
           placeholders=(Placeholder("<vpn:search>", N_("VPN profile for the search server"),
                                     N_("sandbox-ai vpn add mv --mullvad")),
                         Placeholder("<vpn:fetch>", N_("VPN profile for the fetch server"),
                                     N_("sandbox-ai vpn add home --wireguard wg.conf"))),
           notes=(N_("Each server's kill switch lets its connections out only through its own tunnel."),)),
    Preset("tailnet", N_("Tailnet-only internal services"),
           N_("Behind a Tailscale profile without exit node: the tailnet's services and names, the rest as "
              "usual."), "privacy", "medium",
           ("--vpn", "<vpn:tailnet>", "--dns"),
           placeholders=(Placeholder("<vpn:tailnet>", N_("Tailscale profile (no exit node)"),
                                     N_("sandbox-ai vpn add ts --tailscale")),),
           notes=(N_("Without an exit node only the tailnet (100.64.0.0/10) goes through Tailscale; its ACLs "
                     "apply too."),)),
    Preset("own-subnet", N_("Own subnet and read-only web"),
           N_("The sandbox in a network of its own, GitHub and PyPI readable, every write to the web "
              "refused by a ruleset."), "privacy", "advanced",
           ("--subnet", "10.77.0.0/24", "--subnet6", "fd77::/64", "--allow-github", "--allow-pypi",
            "--privacy-check", "always"),
           rules=READ_ONLY_WEB_RULES,
           notes=(N_("Choose a subnet that is free on your networks; the sandbox gets .100, the gateway .2."),)),
    # -- teams and policy --
    Preset("strict-rules", N_("Strict rules for GitHub"),
           N_("GitHub with a ruleset: no gists, no issue writes, every other write asked; tokens only in the "
              "proxy."), "teams", "medium",
           ("--allow-github", "--token-proxy", "--git-identity", "--privacy-check", "always"),
           rules=GITHUB_RULES,
           notes=(N_("The ruleset is written into the project file; a ruleset of yours or the administrator's "
                     "can only narrow it further."),)),
    Preset("kept-session", N_("Long-running session"),
           N_("tmux, firm limits and detailed logs for an agent that works for hours; start it as a session."),
           "teams", "medium",
           ("--tmux", "--memory", "8g", "--cpus", "4", "--pids", "2048", "--log-dir", "<folder:logs>",
            "--log-debug", "--allow-pypi", "--allow-github"),
           placeholders=(Placeholder("<folder:logs>", N_("Folder for the logs"), N_("outside the workspace"),
                                     optional=True),),
           notes=(N_("Start it with Start as session: it keeps running when the window closes."),)),
    Preset("gpu", N_("GPU workstation"),
           N_("The GPU handed to the agent, generous limits, PyPI and Hugging Face for models."), "teams",
           "advanced",
           ("--gpu", "<gpu:kind>", "--memory", "32g", "--cpus", "8", "--allow-pypi", "--allow-host",
            "huggingface.co", "--image-variant", "<text:variant>"),
           placeholders=(Placeholder("<gpu:kind>", N_("GPU"), N_("nvidia (CDI), dri (Intel/AMD) or rocm")),
                         Placeholder("<text:variant>", N_("Image variant with the GPU libraries"),
                                     N_("sandbox-ai images new NAME --from AGENT"), optional=True)),
           notes=(N_("A GPU weakens the isolation; CUDA or ROCm libraries come from an image variant of "
                     "yours."),)),
)
BUILTIN = {preset.id: preset for preset in PRESETS}


def find(name, mine=None):
    """The preset NAME (built-in, or one of MINE)."""
    for preset in list(PRESETS) + list(mine or []):
        if preset.id == name:
            return preset
    raise ValueError(f"unknown preset {name!r}")


def role_keys(board):
    """{node id: KIND#INDEX}: names of the parts that do not depend on paths (for a preset's positions)."""
    seen, keys = {}, {}
    for node in board.nodes:
        index = seen.get(node.kind, 0)
        seen[node.kind] = index + 1
        keys[node.id] = f"{node.kind}#{index}"
    return keys


def build(sai, preset, directory=None, agent=None, values=None, grid=20, backend=None):
    """The board of PRESET for the project DIRECTORY, laid out. VALUES ({placeholder: value}): what the user
    filled in; given (even empty), optional placeholders left empty are left out. Without VALUES every
    placeholder stays, to be filled in on the board. BACKEND: the default backend of the settings."""
    agent = preset.agent or agent or "claude"
    if preset.agents and agent not in preset.agents:
        agent = preset.agents[0]  # the preset is not for the agent of the settings: its first one
    task = (["--prompt", preset.prompt] if agent == "opencode" else [preset.prompt]) if preset.prompt else []
    board = B.from_argv(sai, agent, list(preset.options), list(preset.agent_args) + task,
                        [str(directory)] if directory else [])
    for name in preset.mcp:
        if not any(n.props.get("name") == name for n in board.of_kind("mcp")):
            board.add("mcp", name=name)
    for name, definition in preset.servers:
        node = next((n for n in board.of_kind("mcp") if n.props.get("name") == name), None) or \
            board.add("mcp", name=name)
        node.props.update(definition=json.loads(json.dumps(definition)), project=True)
    if preset.rules is not None:
        (board.first("approvals") or board.add("approvals")).props["rules"] = json.loads(json.dumps(preset.rules))
    if values is not None:
        B.fill(board, values)
        left = {marker for marker, _ in B.placeholders(board)}
        B.drop(board, [p.marker for p in preset.placeholders if p.optional and p.marker in left])
    B.with_backend(board, backend)
    board.layout(grid)
    keys = role_keys(board)
    for node in board.nodes:
        position = preset.positions.get(keys[node.id])
        if isinstance(position, list) and len(position) == 2:
            node.x, node.y = float(position[0]), float(position[1])
    return board


def score(sai, preset, policy=None):
    """The security score the board of PRESET gets (placeholders as they are)."""
    return B.badges(build(sai, preset), policy)[0]


def matches(preset, text):
    """True if every word of TEXT is in the preset's title, description, category or notes."""
    words = str(text or "").lower().split()
    category = dict(CATEGORIES).get(preset.category, preset.category)
    haystack = " ".join([preset.text(preset.title), preset.text(preset.description), tr(category), preset.id,
                         *[preset.text(n) for n in preset.notes], *preset.options]).lower()
    return all(word in haystack for word in words)


# -- the user's own presets --

def store_path():
    return backend.settings_file().with_name("presets.json")


def looks_secret(value):
    from .state import looks_secret as looks
    return looks(value)


def _check(condition, text, **values):
    """ValueError with TEXT (an N_ text, translated) unless CONDITION."""
    if not condition:
        raise ValueError(tr(text, **values))


def to_document(preset):
    """The JSON form of PRESET (what is kept and exported)."""
    return {"format": FORMAT, "version": 1, "id": preset.id, "title": preset.text(preset.title),
            "description": preset.text(preset.description), "category": preset.category,
            "difficulty": preset.difficulty, "agent": preset.agent, "options": list(preset.options),
            "agent_args": list(preset.agent_args), "mcp": list(preset.mcp),
            "mcp_servers": {name: definition for name, definition in preset.servers}, "rules": preset.rules,
            "placeholders": [{"marker": p.marker, "label": preset.text(p.label), "hint": preset.text(p.hint),
                              "optional": p.optional, "follows": p.follows} for p in preset.placeholders],
            "notes": [preset.text(n) for n in preset.notes], "positions": dict(preset.positions)}


def from_document(sai, document):
    """The Preset of a JSON DOCUMENT (from the store or a file: untrusted), every value checked; ValueError
    (translated) with the reason otherwise. Anything that looks like a secret is refused."""
    _check(isinstance(document, dict) and document.get("format") == FORMAT and document.get("version") == 1,
           N_("Not a preset of this app (format {format}, version 1)."), format=FORMAT)
    known = {"format", "version", "id", "title", "description", "category", "difficulty", "agent", "options",
             "agent_args", "mcp", "mcp_servers", "rules", "placeholders", "notes", "positions"}
    _check(not set(document) - known, N_("Unknown keys in the preset: {keys}."),
           keys=", ".join(sorted(set(document) - known)))
    secrets = [word for text in B._strings(document) for word in [text, *text.replace("=", " ").split()]
               if looks_secret(word)]
    _check(not secrets, N_("The preset holds what looks like a secret ({start}…): secrets stay in sandbox-ai "
           "secrets, a preset only names them."), start=str(secrets[0])[:4] if secrets else "")
    ident, title = document.get("id"), document.get("title")
    _check(isinstance(ident, str) and ID.fullmatch(ident), N_("A preset's id is lower-case letters, digits and -."))
    _check(isinstance(title, str) and 0 < len(title.strip()) <= 80,
           N_("A preset needs a title (at most 80 characters)."))
    description = document.get("description", "")
    _check(isinstance(description, str) and len(description) <= 400, N_("The description is too long."))
    category = document.get("category", "simple")
    _check(category in dict(CATEGORIES), N_("Unknown category {name}."), name=str(category)[:40])
    difficulty = document.get("difficulty", "medium")
    _check(difficulty in DIFFICULTIES, N_("Unknown difficulty {name}."), name=str(difficulty)[:40])
    agent = document.get("agent")
    _check(agent is None or agent in tuple(getattr(sai, "agents", B.AGENTS)), N_("Unknown agent {agent}."),
           agent=str(agent)[:40])
    lists = {}
    for key in ("options", "agent_args", "mcp", "notes"):
        value = document.get(key, [])
        _check(isinstance(value, list) and len(value) <= 400 and all(isinstance(v, str) and len(v) <= 1000
                                                                      for v in value),
               N_("{field} must be a list of texts."), field=key)
        lists[key] = value
    from .state import scrub_arguments
    _check(scrub_arguments(lists["agent_args"]) == lists["agent_args"],
           N_("The agent arguments hold what may be a secret."))
    options = B._options(sai)
    values, unknown = backend.values_from_argv(list(options.values()), list(lists["options"]),
                                               sai.launcher.canonical_options)
    _check(not unknown, N_("Options this sandbox-ai does not know: {options}."),
           options=" ".join(map(str, unknown))[:200])
    refused = [o for o in REFUSED if values.get(o)]
    _check(not refused, N_("A preset may not carry {options}: it copies a login in or belongs to one start."),
           options=", ".join(refused))
    for name in lists["mcp"]:
        _check(re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name) or B.PLACEHOLDER.fullmatch(name),
               N_("Not an MCP server name: {name}."), name=name[:40])
    servers = document.get("mcp_servers") or {}
    _check(isinstance(servers, dict) and len(servers) <= 16, N_("mcp_servers must be an object of at most 16 servers."))
    try:
        sai.module("mcp").validated(servers, "mcp_servers")
    except ValueError as exc:
        raise ValueError(str(exc)) from None
    rules = document.get("rules")
    if rules is not None:
        try:
            sai.launcher.RUNTIME.Ruleset.validated(rules, "rules")
        except ValueError as exc:
            raise ValueError(str(exc)) from None
    marks = []
    for entry in document.get("placeholders", []) or []:
        _check(isinstance(entry, dict) and isinstance(entry.get("marker"), str)
               and B.PLACEHOLDER.fullmatch(entry["marker"]) and isinstance(entry.get("label", ""), str)
               and isinstance(entry.get("hint", ""), str) and isinstance(entry.get("optional", False), bool)
               and isinstance(entry.get("follows", ""), str),
               N_("A placeholder has a marker, a label, a hint and whether it is optional."))
        marks.append(Placeholder(entry["marker"], entry.get("label", "")[:120] or entry["marker"],
                                 entry.get("hint", "")[:200], entry.get("optional", False),
                                 entry.get("follows", "")[:40]))
    positions = document.get("positions") or {}
    _check(isinstance(positions, dict) and len(positions) <= 200 and all(
        isinstance(k, str) and re.fullmatch(r"[a-z]+#[0-9]{1,3}", k) and isinstance(v, list) and len(v) == 2
        and all(isinstance(n, (int, float)) and not isinstance(n, bool) and -1e5 < n < 1e5 for n in v)
        for k, v in positions.items()), N_("Positions are KIND#N: [x, y]."))
    return Preset(ident, title.strip(), description.strip(), category, difficulty, tuple(lists["options"]), agent,
                  tuple(lists["mcp"]), tuple(sorted(servers.items())), rules, tuple(marks), tuple(lists["notes"]),
                  tuple(lists["agent_args"]), {k: list(v) for k, v in positions.items()}, user=True)


def from_board(sai, board, title, description="", category="simple", ident=None):
    """The current BOARD as a preset of the user's own: its options without the workspaces (a preset is
    for any project), its MCP servers and ruleset, the placeholders it still has, the positions. ValueError
    if it holds what looks like a secret or options a preset may not carry."""
    agent, _, agent_args = B.values(board)
    options = B.to_argv(sai, board, workspaces=False)
    # The project's own servers are kept with their definitions; --mcp names them in the options.
    servers = {n.props["name"]: n.props["definition"] for n in board.included() if n.kind == "mcp"
               and n.props.get("project") and n.props.get("name") and isinstance(n.props.get("definition"), dict)}
    approvals = board.first("approvals")
    rules = approvals.props.get("rules") if approvals is not None and isinstance(approvals.props.get("rules"),
                                                                                 dict) else None
    marks = [{"marker": marker, "label": tr(B.PLACEHOLDER_KINDS[B.PLACEHOLDER.fullmatch(marker)[1]]),
              "hint": "", "optional": False, "follows": ""} for marker, _ in B.placeholders(board)]
    keys = role_keys(board)
    ident = ident or "my-" + (re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:56] or "preset")
    document = {"format": FORMAT, "version": 1, "id": ident, "title": title, "description": description,
                "category": category, "difficulty": "medium", "agent": agent, "options": options,
                "agent_args": agent_args, "mcp": [], "mcp_servers": servers, "rules": rules, "placeholders": marks,
                "notes": [], "positions": {keys[n.id]: [round(n.x, 1), round(n.y, 1)] for n in board.nodes}}
    return from_document(sai, document)


SAI_KINDS = {"folder": "folder", "vpn": "vpn", "llm": "llm"}  # sandbox-ai's value kinds the board knows; the rest: text
SAI_CATEGORY = "sandbox-ai"


def floor_mode(options):
    """OPTIONS with a preset's --mode as a floor (--mode-at-least): sandbox-ai's launcher applies a preset's mode
    only when it is stricter than the one the user's defaults or policy give, and so does a start from the board."""
    out, i = [], 0
    while i < len(options):
        option = options[i]
        if option in ("--mode", "-m") and i + 1 < len(options):
            out += ["--mode-at-least", options[i + 1]]
            i += 2
            continue
        if option.startswith("--mode="):
            option = "--mode-at-least=" + option.split("=", 1)[1]
        out.append(option)
        i += 1
    return out


def from_sandbox_ai(sai, document):
    """sandbox-ai's own preset DOCUMENT (sandbox-ai presets list --json) as a Preset of the gallery: its {values}
    become the board's placeholders, its MCP servers registered ones by name, its ruleset file the approvals
    node's rules, its first task the agent's arguments. Checked as an imported preset is (from_document)."""
    values = document.get("values") if isinstance(document.get("values"), dict) else {}
    markers = {}
    for name, spec in values.items():
        kind = SAI_KINDS.get((spec or {}).get("kind"), "text")
        markers[name] = f"<{kind}:{re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')[:32] or 'value'}>"
    fill = lambda text: re.sub(r"\{([a-z][a-z0-9_]{0,31})\}", lambda m: markers.get(m.group(1), m.group(0)), text)  # noqa: E731
    agents = [a for a in document.get("agents") or [] if isinstance(a, str)]
    agent = agents[0] if len(agents) == 1 else None
    prompt = document.get("prompt") if isinstance(document.get("prompt"), str) else ""
    rules = None
    if isinstance(document.get("rules"), str) and isinstance(document.get("_path"), str):
        try:
            rules = json.loads((Path(document["_path"]).parent / document["rules"]).read_text())
        except (OSError, ValueError):
            rules = None
    converted = {
        "format": FORMAT, "version": 1, "id": "sai-" + str(document.get("id", ""))[:60],
        "title": str(document.get("title", ""))[:80], "description": str(document.get("description", ""))[:400],
        "category": SAI_CATEGORY, "difficulty": "medium", "agent": agent,
        "options": floor_mode([fill(str(option)) for option in document.get("options") or []]),
        "agent_args": [fill(prompt)] if prompt else [],
        "mcp": [str(server).split(":", 1)[-1] for server in document.get("mcp") or []],
        "rules": rules,
        "placeholders": [{"marker": markers[name], "label": str((spec or {}).get("label", name))[:200],
                          "hint": str((spec or {}).get("hint", ""))[:300], "optional": bool((spec or {}).get("optional")),
                          "follows": ""} for name, spec in values.items()],
        "notes": [str(note)[:1000] for note in (document.get("notes") or []) + (document.get("needs") or [])]}
    checked = from_document(sai, converted)  # the task is checked with the arguments, then kept apart
    import dataclasses
    return dataclasses.replace(checked, agent_args=(), prompt=checked.agent_args[0] if checked.agent_args else "",
                               agents=tuple(a for a in agents if a in tuple(getattr(sai, "agents", B.AGENTS))))


def sandbox_ai_presets(sai):
    """([Preset] of sandbox-ai's own presets (built-in and the user's), [problem]); none from a sandbox-ai without
    presets."""
    try:
        module = sai.module("presets")
    except Exception:  # noqa: BLE001 - an older sandbox-ai: no presets of its own
        return [], []
    found, problems = [], []
    for document in module.catalog().values():
        try:
            found.append(from_sandbox_ai(sai, {**document, "_path": document.get("_path")}))
        except ValueError as exc:
            problems.append(f"{document.get('id')}: {exc}")
    return found, problems


def load_mine(sai):
    """([Preset] of the user's own, [problem]): a stored preset that does not pass the checks is left out
    and said."""
    try:
        data = store_path().read_bytes()
    except OSError:
        return [], []
    try:
        stored = json.loads(data[:LIMIT * 64].decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        return [], [tr("{path} is not valid JSON.", path=str(store_path()))]
    found, problems = [], []
    for document in stored.get("presets", []) if isinstance(stored, dict) else []:
        try:
            found.append(from_document(sai, document))
        except ValueError as exc:
            problems.append(str(exc))
    return found, problems


def save_mine(presets):
    backend.write_private_json(store_path(), {"version": 1, "presets": [to_document(p) for p in presets]})


def add_mine(sai, preset):
    """Keep PRESET with the user's own (one of the same id is replaced)."""
    mine, _ = load_mine(sai)
    save_mine([p for p in mine if p.id != preset.id] + [preset])


def remove_mine(sai, ident):
    mine, _ = load_mine(sai)
    save_mine([p for p in mine if p.id != ident])


def export(sai, preset, path):
    """Write PRESET as a JSON file (checked once more: nothing secret leaves)."""
    document = to_document(preset)
    from_document(sai, document)
    text = json.dumps(document, indent=2) + "\n"
    with open(path, "w", encoding="utf-8") as stream:
        stream.write(text)
    return path


def import_file(sai, path):
    """The preset in the JSON file PATH (untrusted: size limited, every value checked), as one of the user's
    own (its id prefixed with my- when it would shadow a built-in one)."""
    with open(path, "rb") as stream:
        data = stream.read(LIMIT + 1)
    _check(len(data) <= LIMIT, N_("The file is larger than {size} KiB."), size=LIMIT >> 10)
    try:
        document = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise ValueError(tr("Not a JSON file.")) from None
    preset = from_document(sai, document)
    if preset.id in BUILTIN:
        preset = from_document(sai, {**document, "id": ("my-" + preset.id)[:64]})
    return preset
