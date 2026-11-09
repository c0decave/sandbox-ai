"""The board: one sandbox as a graph of nodes and edges. No Qt.

A node stands for a part of the sandbox - the agent, a workspace, a mount, the network, the connection
proxy, approvals and rulesets, Git, an MCP server, an LLM server, a VPN, devices, limits, the display, the
session, logs, kits, HOME and login, the engine - and owns launcher options. The board turns into launcher options (and a project file) and back,
so it means exactly what the command line means: nothing is lost on the way (options no node owns, and
tokens the parser does not know, are kept in Board.extra), and the launcher itself has the last word
(launcher_argv() is checked with a dry run, which the window runs in the background).

Edges show the flow: a workspace or mount gives the agent its files, the agent's connections pass the proxy
on their way to the network, the agent talks to its MCP servers and its LLM server, and the settings nodes
configure the agent. Workspaces, mounts and MCP servers count only while they are connected to the agent.

A board made from a preset may hold placeholders, values written <KIND:NAME> (a VPN profile, an LLM server,
an MCP server, a folder ...): problems() lists each as an error until fill() replaces it.

The board also explains itself: problems() lists what the launcher or the administrator's policy would
refuse (and what makes no sense), badges() what weakens the sandbox, with a score from 0 to 100.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
import itertools
import json
import os
from pathlib import Path
import re
import shlex

from . import backend
from .files import printable
from .i18n import N_, tr

AGENTS = ("claude", "codex", "opencode")
LAYOUT_NAME = ".sandbox-ai.board.json"  # node positions only; the options live in .sandbox-ai.json
LAYOUT_LIMIT = 256 << 10
# Requests per host, as the launcher checks them (agent_sandbox.HTTP_RULE).
HTTP_RULE = re.compile(r"(\*\.)?([a-z0-9-]{1,63}(?:\.[a-z0-9-]{1,63})+) +(\*|[A-Z]{3,10}(?:,[A-Z]{3,10})*) +"
                       r"(/[!-~]{0,500})")
PRESET_OPTIONS = ("--allow-pypi", "--allow-github", "--allow-gitlab", "--allow-kernel-org", "--allow-duckduckgo")


@dataclass(frozen=True)
class Kind:
    key: str
    title: str  # translated when shown (N_)
    options: tuple = ()  # the launcher options the node owns
    single: bool = True  # at most one per board
    required: bool = False  # always on the board
    column: int = 2  # where auto-layout puts it
    help: str = ""


KINDS = {kind.key: kind for kind in (
    Kind("workspace", N_("Workspace"), single=False, column=0,
         help=N_("A folder the agent works in: the first at /work, further ones at /workspaces/NAME")),
    Kind("mount", N_("Mount"), single=False, column=0,
         help=N_("A further host folder or file at /work, /mnt or /opt, read-only or writable")),
    Kind("home", N_("HOME and login"), ("--state-dir", "--copy", "--copy-auth", "--no-auth", "--auth-file",
                                        "--import-codex-auth"), column=0,
         help=N_("The sandbox HOME (kept between runs or not) and the agent's login")),
    Kind("kits", N_("Kits, memory, plugins"), ("--kit", "--kit-dir", "--no-default-kit", "--no-memory",
                                               "--no-plugins"), column=0,
         help=N_("Kits copied into HOME, project memory and plugins")),
    Kind("agent", N_("Agent"), ("--dangerously-skip-permissions", "--local-config", "--env", "--secret-env"),
         required=True, column=1, help=N_("The coding agent and how it runs")),
    Kind("approvals", N_("Approvals and rules"), ("--ask", "--rules"), column=1,
         help=N_("Ask before what the options do not allow; rulesets allow, ask or deny each request and MCP "
                 "tool call")),
    Kind("proxy", N_("Connection proxy"), ("--no-inspect", "--allow-http", "--privacy-check", "--privacy-rules"),
         required=True, column=2,
         help=N_("Every connection passes it: TLS opened and logged, pushes checked, requests per host")),
    Kind("git", N_("Git"), ("--git-identity", "--git-token-env", "--git-token-file", "--git-token-secret",
                            "--token-proxy", "--git-credentials"), column=1,
         help=N_("Commit identity and push tokens (with the token proxy the agent never sees them)")),
    Kind("mcp", N_("MCP server"), single=False, column=2,
         help=N_("A local MCP server in its own sandbox, with its own network and folders")),
    Kind("llm", N_("LLM server"), ("--llm",), column=2,
         help=N_("A model on your own server (sandbox-ai llm): only its address is opened, its key stays with "
                 "the proxy")),
    Kind("network", N_("Network"), ("--profile", *PRESET_OPTIONS, "--frontier", "--no-provider-defaults",
                                    "--allow-host", "--allow", "--allow-tcp", "--allow-host-port", "--dns",
                                    "--nameserver", "--offline", "--egress-all-allow", "--network", "--ip", "--ip6",
                                    "--subnet", "--subnet6"),
         required=True, column=3, help=N_("Where connections may go: hosts, presets, addresses, ports, DNS")),
    Kind("vpn", N_("VPN"), ("--vpn", "--vpn-check"), column=2,
         help=N_("A VPN profile in front (sandbox-ai vpn): everything leaves through the tunnel, a kill switch "
                 "blocks the rest, addresses and DNS are checked before the start")),
    Kind("devices", N_("Devices and GPU"), ("--device", "--gpu"), column=0,
         help=N_("Host devices handed to the agent (weakens the isolation)")),
    Kind("limits", N_("Limits"), ("--memory", "--cpus", "--pids"), column=0,
         help=N_("Memory, CPUs and processes")),
    Kind("display", N_("Display"), ("--gui", "--gui-screen", "--gui-vnc", "--gui-wayland"), column=0,
         help=N_("A virtual display for GUI programs and browsers")),
    Kind("session", N_("Session and tmux"), ("--tmux", "--no-tmux"), column=0,
         help=N_("tmux inside the sandbox (sessions are started from the Start page)")),
    Kind("logs", N_("Logs"), ("--log-dir", "--log-debug"), column=1,
         help=N_("Where the logs go (hidden from the agent), and how much of what is refused they keep")),
    Kind("engine", N_("Engine"), ("--backend", "--image", "--image-variant", "--podman-url", "--binary", "--runtime"),
         column=0,
         help=N_("Podman or Bubblewrap, the image and the agent's executable")),
)}
# Which edges may exist: (from kind, to kind). Workspaces, mounts and MCP servers count only when connected.
EDGES = {("workspace", "agent"), ("mount", "agent"), ("home", "agent"), ("kits", "agent"), ("agent", "proxy"),
         ("proxy", "network"), ("agent", "mcp"), ("approvals", "proxy"), ("git", "proxy"), ("devices", "agent"),
         ("limits", "agent"), ("display", "agent"), ("session", "agent"), ("logs", "proxy"), ("engine", "agent"),
         ("vpn", "network"), ("agent", "llm")}
CONNECTED_ONLY = {"workspace", "mount", "mcp"}
OWNER = {option: kind.key for kind in KINDS.values() for option in kind.options}
# Options shown as a property of their own: proxy "enabled" (--no-proxy), workspace "readonly" (--workspace-ro),
# the workspace, mount and MCP nodes themselves.
OWNER.update({"--no-proxy": "proxy", "--workspace-ro": "workspace", "--workspace": "workspace", "--mount": "mount",
              "--mcp": "mcp"})


@dataclass
class Node:
    id: int
    kind: str
    props: dict = field(default_factory=dict)
    x: float = 0.0
    y: float = 0.0

    def title(self):
        """What the node shows: its kind and what tells it apart (a path, a server name)."""
        name = tr(KINDS[self.kind].title)
        detail = {"workspace": self.props.get("path"), "mount": self.props.get("source"),
                  "mcp": self.props.get("name"), "agent": self.props.get("agent"),
                  "llm": self.props.get("--llm")}.get(self.kind)
        return f"{name}: {printable(detail)}" if detail else name

    def key(self):
        """A stable name for the layout file (positions survive a reload of the options)."""
        detail = {"workspace": "path", "mount": "source", "mcp": "name"}.get(self.kind)
        return f"{self.kind}:{self.props.get(detail, '')}" if detail else self.kind


def snap(value, grid):
    return float(round(value / grid) * grid) if grid and grid > 1 else float(value)


def ports(kind):
    """(has an input, has an output) of a node kind: where edges may arrive and leave."""
    return any(b == kind for _, b in EDGES), any(a == kind for a, _ in EDGES)


@dataclass
class Problem:
    level: str  # "error" (the launcher or the policy refuses it) or "warning"
    text: str
    node: int | None = None


@dataclass
class Badge:
    level: str  # "bad", "warn", "ok" or "info"
    text: str
    node: int | None = None
    cost: int = 0  # points off the score of 100


class Board:
    """The nodes and edges of one sandbox, plus what no node owns (EXTRA, kept as launcher tokens) and the
    keys of a project file the board does not edit (DOCUMENT_EXTRA: description, mcp_servers)."""

    def __init__(self):
        self.nodes, self.edges, self.extra, self.document_extra = [], set(), [], {}
        self._ids = itertools.count(1)

    # -- nodes and edges --

    def add(self, kind, connect=True, **props):
        if kind not in KINDS:
            raise ValueError(f"unknown node kind {kind!r}")
        if KINDS[kind].single and self.first(kind) is not None:
            raise ValueError(f"a board has one {kind} node")
        node = Node(next(self._ids), kind, dict(props))
        self.nodes.append(node)
        if connect:
            for other in self.nodes:
                if (kind, other.kind) in EDGES:
                    self.edges.add((node.id, other.id))
                elif (other.kind, kind) in EDGES:
                    self.edges.add((other.id, node.id))
        return node

    def remove(self, node_id):
        node = self.node(node_id)
        if node is None:
            return None
        if KINDS[node.kind].required:
            raise ValueError(f"the {node.kind} node cannot be removed")
        self.nodes.remove(node)
        self.edges = {(a, b) for a, b in self.edges if node_id not in (a, b)}
        return node

    def node(self, node_id):
        return next((n for n in self.nodes if n.id == node_id), None)

    def first(self, kind):
        return next((n for n in self.nodes if n.kind == kind), None)

    def of_kind(self, kind):
        return [n for n in self.nodes if n.kind == kind]

    def can_connect(self, source, target):
        a, b = self.node(source), self.node(target)
        return a is not None and b is not None and (a.kind, b.kind) in EDGES

    def connect(self, source, target):
        if not self.can_connect(source, target):
            raise ValueError("these two cannot be connected")
        self.edges.add((source, target))

    def disconnect(self, source, target):
        self.edges.discard((source, target))

    def connected(self, node):
        """True if NODE takes part: always, except a workspace, mount or MCP server without its edge."""
        if node.kind not in CONNECTED_ONLY:
            return True
        agent = self.first("agent")
        return agent is not None and ((node.id, agent.id) in self.edges or (agent.id, node.id) in self.edges)

    def included(self):
        return [n for n in self.nodes if self.connected(n)]

    # -- snapshots (undo and redo keep whole states: simple, and never out of step with the model) --

    def state(self):
        return {"nodes": [{"id": n.id, "kind": n.kind, "props": json.loads(json.dumps(n.props)), "x": n.x, "y": n.y}
                          for n in self.nodes],
                "edges": sorted(map(list, self.edges)), "extra": list(self.extra),
                "document_extra": json.loads(json.dumps(self.document_extra))}

    @classmethod
    def from_state(cls, state):
        board = cls()
        board.nodes = [Node(n["id"], n["kind"], json.loads(json.dumps(n["props"])), n["x"], n["y"])
                       for n in state["nodes"]]
        board.edges = {tuple(e) for e in state["edges"]}
        board.extra, board.document_extra = list(state["extra"]), json.loads(json.dumps(state["document_extra"]))
        board._ids = itertools.count(max([n.id for n in board.nodes] or [0]) + 1)
        return board

    # -- layout --

    # The columns of layout(), by role: what configures the agent, the agent and what configures the proxy,
    # the proxy and what the agent talks to, the network. In each column, top to bottom, in this order.
    FLOW = (("workspace", "mount", "home", "kits", "engine", "devices", "limits", "display", "session"),
            ("agent", "approvals", "git", "logs"), ("mcp", "llm", "proxy", "vpn"), ("network",))

    def layout(self, grid=0):
        """Left to right as the connections flow: the agent's files and settings, the agent, the proxy with the
        MCP and LLM servers above it and the VPN below, the network; each column centred on what it connects
        to, so the edges stay short and do not cross (on a GRID, if given)."""
        columns = [[n for kind in kinds for n in self.nodes if n.kind == kind] for kinds in self.FLOW]
        middle, rows = columns[2], {}
        for index, node in enumerate(middle):
            rows[node.id] = index
        proxy = next((rows[n.id] for n in middle if n.kind == "proxy"), 0)
        agent_row = proxy / 2  # level with the middle of the MCP and LLM servers above the proxy, and the proxy
        settings = 0
        for node in columns[1]:
            if node.kind == "agent":
                rows[node.id] = agent_row
            else:  # below the agent, feeding the proxy
                rows[node.id] = max(agent_row, proxy - 0.5) + 1 + settings
                settings += 1
        for index, node in enumerate(columns[0]):
            rows[node.id] = agent_row - (len(columns[0]) - 1) / 2 + index
        vpn = any(n.kind == "vpn" for n in middle)
        for node in columns[3]:
            rows[node.id] = proxy + (0.5 if vpn else 0)
        lowest = min(rows.values(), default=0)
        for column, nodes in enumerate(columns):
            for node in nodes:
                node.x = snap(40.0 + column * 260.0, grid)
                node.y = snap(40.0 + (rows[node.id] - lowest) * 110.0, grid)

    def positions(self):
        return {"version": 1, "nodes": {node.key(): [round(node.x, 1), round(node.y, 1)] for node in self.nodes}}

    def place(self, layout):
        """Positions from a layout file (data, not instructions: numbers only, bounded); True if any."""
        nodes = layout.get("nodes") if isinstance(layout, dict) and layout.get("version") == 1 else None
        if not isinstance(nodes, dict):
            return False
        placed = False
        for node in self.nodes:
            value = nodes.get(node.key())
            if isinstance(value, list) and len(value) == 2 and all(isinstance(v, (int, float)) and
                                                                    -1e6 < v < 1e6 for v in value):
                node.x, node.y = float(value[0]), float(value[1])
                placed = True
        return placed


# -- options --

_OPTIONS = {}  # id(sai): (sai, {long option: Option})


def _options(sai):
    """The launcher's options by long name (cached per installation: the parser is built once)."""
    cached = _OPTIONS.get(id(sai))
    if cached is None or cached[0] is not sai:
        cached = _OPTIONS[id(sai)] = (sai, {o.long: o for o in sai.options()})
    return cached[1]


def _split_agent_args(agent_args):
    """(model, the other arguments as one line) of agent arguments."""
    args, model, rest = list(agent_args or []), "", []
    index = 0
    while index < len(args):
        if args[index] == "--model" and index + 1 < len(args) and not model:
            model, index = args[index + 1], index + 2
            continue
        if args[index].startswith("--model=") and not model:
            model = args[index].partition("=")[2]
        else:
            rest.append(args[index])
        index += 1
    return model, shlex.join(rest)


def from_argv(sai, agent, argv, agent_args=(), workspaces=()):
    """The board of a launcher command line (short forms included): every option goes to the node that owns
    it; what no node owns stays in Board.extra."""
    options = _options(sai)
    values, unknown = backend.values_from_argv(list(options.values()), list(argv), backend.canonical_of(sai))
    board = Board()
    model, arguments = _split_agent_args(agent_args)
    model = str(values.pop("--model", "") or "") or model  # the launcher's option, else the agent's own
    effort = str(values.pop("--effort", "") or "")
    board.add("agent", agent=str(agent), model=model, arguments=arguments,
              **({"effort": effort} if effort else {}))  # an unknown agent is a problem
    paths = [w.strip() for value in list(workspaces) + list(values.pop("--workspace", []) or [])
             for w in str(value).split(",") if w.strip()]
    readonly = bool(values.pop("--workspace-ro", False))
    for path in dict.fromkeys(paths):
        board.add("workspace", path=path, readonly=readonly)
    for value in values.pop("--mount", []) or []:
        source, dest, mode = (str(value).rsplit(":", 2) + ["", ""])[:3] if str(value).count(":") >= 2 else \
            (str(value), "", "ro")
        board.add("mount", source=source, dest=dest, mode=mode)
    for name in values.pop("--mcp", []) or []:
        board.add("mcp", name=name)
    proxy_on = not values.pop("--no-proxy", False)
    grouped = {}
    for option, value in values.items():
        kind = OWNER.get(option)
        if kind is None or option not in options:
            board.extra += options[option].tokens(value) if option in options else []
            continue
        if options[option].tokens(value):  # only what differs from the default
            grouped.setdefault(kind, {})[option] = value
    for kind in KINDS.values():
        if kind.key in ("agent", "workspace", "mount", "mcp"):
            continue
        props = grouped.get(kind.key)
        if props or kind.required:
            board.add(kind.key, **(props or {}))
    board.first("proxy").props["enabled"] = proxy_on
    board.first("agent").props.update(grouped.get("agent", {}))
    board.extra += unknown
    board.layout()
    return board


def from_project(sai, document, directory):
    """The board of a project file (.sandbox-ai.json in DIRECTORY): its main workspace is DIRECTORY."""
    board = from_argv(sai, document.get("agent", ""), document.get("options", []), document.get("agent_args", []),
                      [str(directory)] + [str(w) for w in document.get("workspaces", [])])
    # The file's other keys (mode, canaries, keep, log_debug, name, description) are no part of the board: kept for
    # the file Apply writes, named above the board and in Apply's question (project_trust), never carried silently.
    # A key the launcher does not know is dropped (it would refuse the file).
    known = set(getattr(sai.launcher, "CONFIG_KEYS", ())) if sai is not None else set()
    board.document_extra = {k: v for k, v in document.items()
                            if k not in ("version", "agent", "options", "workspaces", "agent_args", "mcp_servers",
                                         "rules") and k in known}
    if isinstance(document.get("rules"), dict):  # the project's ruleset belongs to the approvals node
        (board.first("approvals") or board.add("approvals")).props["rules"] = json.loads(json.dumps(document["rules"]))
    # The project's own MCP servers are nodes: inspectable and scored (their folders and network), connected
    # to the agent only when --mcp uses them.
    servers = document.get("mcp_servers")
    agent = board.first("agent")
    for name, definition in sorted(servers.items()) if isinstance(servers, dict) else []:
        node = next((n for n in board.of_kind("mcp") if n.props.get("name") == name), None)
        if node is None:
            node = board.add("mcp", name=name)
            board.disconnect(agent.id, node.id)
        node.props.update(definition=definition, project=True)
    board.layout()
    return board


def launcher_knows(sai, option):
    """Whether this sandbox-ai's launcher has OPTION (an older one has no --model or --effort)."""
    try:
        return sai is not None and option in _options(sai)
    except Exception:  # noqa: BLE001 - a launcher that does not load knows nothing
        return False


def values(board, sai=None):
    """({long option: value} of the included nodes, the agent, the agent arguments) - the form's format. The agent
    node's model and effort become --model and --effort where SAI's launcher has them (it turns them into the
    agent's own words); without SAI or with an older launcher the model goes to the agent's arguments."""
    found, agent_args = {}, []
    agent = board.first("agent")
    for node in board.included():
        for option in KINDS[node.kind].options:
            if option not in node.props:
                continue
            value = node.props[option]
            if isinstance(value, list):
                found.setdefault(option, [])
                found[option] = found[option] + [v for v in value if str(v).strip()]
            elif isinstance(value, bool):
                found[option] = found.get(option, False) or value
            elif value not in (None, ""):
                found[option] = value
    workspaces = [n for n in board.included() if n.kind == "workspace" and str(n.props.get("path", "")).strip()]
    if workspaces:
        found["--workspace"] = [str(n.props["path"]).strip() for n in workspaces]
    if any(n.props.get("readonly") for n in workspaces):
        found["--workspace-ro"] = True
    mounts = [n for n in board.included() if n.kind == "mount" and n.props.get("source")]
    if mounts:
        found["--mount"] = [f"{n.props['source']}:{n.props.get('dest', '')}:{n.props.get('mode') or 'ro'}" for n in mounts]
    servers = [n.props["name"] for n in board.included() if n.kind == "mcp" and n.props.get("name")]
    if servers:
        found["--mcp"] = servers
    proxy = board.first("proxy")
    if proxy is not None and proxy.props.get("enabled", True) is False:
        found["--no-proxy"] = True
    if agent is not None:
        if agent.props.get("model"):
            if launcher_knows(sai, "--model"):
                found["--model"] = str(agent.props["model"])
            else:
                agent_args += ["--model", str(agent.props["model"])]
        if agent.props.get("effort") and launcher_knows(sai, "--effort"):
            found["--effort"] = str(agent.props["effort"])
        try:
            agent_args += shlex.split(agent.props.get("arguments") or "")
        except ValueError:
            pass
    return (agent.props.get("agent", "claude") if agent else "claude"), found, agent_args


def to_argv(sai, board, workspaces=True):
    """The launcher options of the board, in the parser's order, then Board.extra. Without WORKSPACES, the
    --workspace options are left out (a project file names its own)."""
    options = _options(sai)
    agent, found, _ = values(board, sai)
    if not workspaces:
        found.pop("--workspace", None)
    return backend.argv_from_values(list(options.values()), found) + list(board.extra)


def document_argv(board):
    """The options of the project file's own keys that no node holds (Board.document_extra): its security mode,
    canaries, kept HOME, debug logs and name. A board start passes --no-config, so without these a paranoid
    project would start as medium; given first, so an option the board sets itself wins (as on the command line
    over a project file)."""
    extra, argv = board.document_extra, []
    if isinstance(extra.get("mode"), str) and extra["mode"]:
        argv += ["--mode", extra["mode"]]
    if "canaries" in extra:
        chosen = extra["canaries"]
        argv += ["--canaries", ",".join(map(str, chosen))] if isinstance(chosen, list) and chosen else ["--no-canaries"]
    if extra.get("keep") in (True, False):
        argv.append("--keep" if extra["keep"] else "--no-keep")
    if extra.get("log_debug") is True:
        argv.append("--log-debug")
    if isinstance(extra.get("name"), str) and extra["name"]:
        argv += ["--project-name", extra["name"]]
    return argv


def project_rules(board):
    """The project's own ruleset (.sandbox-ai.json "rules", on the approvals node), or None."""
    approvals = board.first("approvals")
    rules = approvals.props.get("rules") if approvals is not None else None
    return rules if isinstance(rules, dict) else None


def rules_copy(rules):
    """A private copy of the project's ruleset RULES outside every workspace (the app's settings, named by its
    sha256), for --rules: a board start passes --no-config, and the ruleset must hold all the same."""
    import hashlib
    data = (json.dumps(rules, indent=2, sort_keys=True) + "\n").encode()
    folder = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "sandbox-ai" / "app" / "rules"
    target = folder / f"{hashlib.sha256(data).hexdigest()}.json"
    if not (target.is_file() and not target.is_symlink() and target.read_bytes() == data):
        backend.write_private_json(target, json.loads(data))
    return target


def launcher_argv(sai, board, local=True):
    """What the launcher checks in a dry run and what a board start runs: the board alone (--no-config: a trusted
    project file in the workspace would be merged in otherwise), with the project file's own keys as options
    (document_argv) and, on this machine (LOCAL), its ruleset as --rules (a copy outside every workspace). Nothing
    of the project file is left out silently: a ruleset on another host is refused at the start (board_view)."""
    rules = project_rules(board)
    copy = ["--rules", str(rules_copy(rules))] if rules is not None and local else []
    return document_argv(board) + to_argv(sai, board) + copy + ["--no-config"]


def project_servers(board):
    """The names of the MCP servers the board uses that the project itself defines (.sandbox-ai.json
    "mcp_servers", e.g. a preset's search server). The launcher knows them only from the trusted project file:
    a start with --no-config would refuse them ("Unknown MCP server")."""
    return [n.props["name"] for n in board.included() if n.kind == "mcp" and n.props.get("name")
            and n.props.get("project") and isinstance(n.props.get("definition"), dict)]


def file_start_argv(sai, board):
    """The launcher options of a start that takes the board from its written and trusted project file: the
    main workspace (where the file is; the launcher applies it, its MCP servers included) and the options a
    project file may not carry (--mode, --name, --rules ...), nothing else (the file's options would be given
    twice otherwise)."""
    _, found, _ = values(board, sai)
    workspaces = list(found.get("--workspace", []))
    forbidden = set(sai.launcher.CONFIG_FORBIDDEN) - {"--workspace", "--config", "--no-config", "--"}
    kept = {name: value for name, value in found.items() if name in forbidden}
    argv = backend.argv_from_values(list(_options(sai).values()), kept)
    argv += [t for t in board.extra if t.split("=", 1)[0] in forbidden]
    return argv + (["--workspace", workspaces[0]] if workspaces else [])


def command_line(sai, board):
    agent, _, agent_args = values(board, sai)
    return shlex.join(["sandbox-ai", agent, *document_argv(board), *to_argv(sai, board),
                       *(["--", *agent_args] if agent_args else [])])


def project_document(sai, board):
    """(.sandbox-ai.json document, main workspace) of the board: the first workspace is where the file lives,
    further ones are listed; options a project file may not set are left out (and kept in nothing: the
    window shows them as problems first)."""
    agent, found, agent_args = values(board, sai)
    workspaces = list(found.get("--workspace", []))
    forbidden = set(sai.launcher.CONFIG_FORBIDDEN)
    kept = {name: value for name, value in found.items() if name not in forbidden}
    options = _options(sai)
    argv = backend.argv_from_values(list(options.values()), kept)
    argv += [t for t in board.extra if t.split("=", 1)[0] not in forbidden]
    known = set(getattr(sai.launcher, "CONFIG_KEYS", ()))
    document = {"version": 1, "agent": agent, "options": argv,
                **{k: v for k, v in board.document_extra.items() if k in known}}
    servers = {n.props["name"]: n.props["definition"] for n in board.of_kind("mcp")
               if n.props.get("project") and n.props.get("name") and isinstance(n.props.get("definition"), dict)}
    if servers:
        document["mcp_servers"] = servers
    approvals = board.first("approvals")
    if approvals is not None and isinstance(approvals.props.get("rules"), dict):
        document["rules"] = approvals.props["rules"]
    if workspaces[1:]:
        document["workspaces"] = workspaces[1:]
    if agent_args:
        document["agent_args"] = agent_args
    return document, (workspaces[0] if workspaces else None)


# -- placeholders: what a preset leaves to be filled in --

PLACEHOLDER = re.compile(r"<(vpn|llm|mcp|folder|gpu|text):([a-z0-9][a-z0-9-]{0,31})>")
PLACEHOLDER_KINDS = {
    "vpn": N_("a VPN profile (sandbox-ai vpn list)"),
    "llm": N_("an LLM server (sandbox-ai llm list)"),
    "mcp": N_("an MCP server (sandbox-ai mcp list)"),
    "folder": N_("a folder"),
    "gpu": N_("a GPU: nvidia, dri or rocm"),
    "text": N_("a value"),
}


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)


def _replace(value, function):
    """VALUE with FUNCTION applied to every string in it (lists and objects walked; keys stay)."""
    if isinstance(value, str):
        return function(value)
    if isinstance(value, list):
        return [_replace(item, function) for item in value]
    if isinstance(value, dict):
        return {key: _replace(item, function) for key, item in value.items()}
    return value


def placeholders(board):
    """[(placeholder, node id or None)] of the parts that take part (and Board.extra): each once."""
    found = {}
    for node in board.included():
        for text in _strings(node.props):
            for match in PLACEHOLDER.finditer(text):
                found.setdefault(match[0], node.id)
    for text in board.extra:
        for match in PLACEHOLDER.finditer(str(text)):
            found.setdefault(match[0], None)
    return list(found.items())


def fill(board, values):
    """Put VALUES ({placeholder: value}) in place of the placeholders (empty values leave them)."""
    values = {k: str(v).strip() for k, v in (values or {}).items() if PLACEHOLDER.fullmatch(k) and str(v).strip()}
    if not values:
        return board
    pattern = re.compile("|".join(map(re.escape, values)))

    def put(text):
        return pattern.sub(lambda m: values[m[0]], text)
    for node in board.nodes:
        arguments = node.props.get("arguments") if node.kind == "agent" else None
        if isinstance(arguments, str) and pattern.search(arguments):
            # The agent's arguments are one shell-quoted string: filled token by token and quoted again, so a value
            # with a quote or a space stays inside its argument (a task "don't stop" is not split, nor an option).
            try:
                filled = shlex.join(put(token) for token in shlex.split(arguments))
            except ValueError:
                filled = None  # not shell-quoted: filled as text below, as before
            if filled is not None:
                # The rest of the props only: the filled arguments are not scanned again (a value holding another
                # placeholder's marker would get that value in raw).
                rest = _replace({k: v for k, v in node.props.items() if k != "arguments"}, put)
                node.props = {**rest, "arguments": filled}
                continue
        node.props = _replace(node.props, put)
    board.extra = [put(str(t)) for t in board.extra]
    return board


def drop(board, markers):
    """Leave out what holds one of MARKERS (optional placeholders not filled in): an MCP server, mount or
    workspace, an option value (of a list: that entry; a part left with no option goes too), a token of
    Board.extra with the option before it."""
    markers = set(markers)
    if not markers:
        return board

    def holds(value):
        return any(m in text for text in _strings(value) for m in markers)
    for node in list(board.nodes):
        defining = {"mcp": "name", "mount": "source", "workspace": "path"}.get(node.kind)
        if defining and holds(node.props.get(defining, "")):
            board.remove(node.id)
            continue
        dropped = False
        for key, value in list(node.props.items()):
            if isinstance(value, list) and key.startswith("--"):
                kept = [v for v in value if not holds(v)]
                dropped = dropped or len(kept) < len(value)
                if kept:
                    node.props[key] = kept
                else:
                    node.props.pop(key)
            elif holds(value):
                node.props.pop(key)
                dropped = True
        if dropped and not KINDS[node.kind].required and node.kind not in CONNECTED_ONLY and \
                not any(k.startswith("--") for k in node.props):
            board.remove(node.id)  # a part that set nothing else says nothing
    extra, index = [], 0
    while index < len(board.extra):
        token = str(board.extra[index])
        following = str(board.extra[index + 1]) if index + 1 < len(board.extra) else ""
        if holds(token):
            index += 1
        elif token.startswith("-") and holds(following):
            index += 2
        else:
            extra.append(board.extra[index])
            index += 1
    board.extra = extra
    return board


# -- what the board means --

def _hosts_of(values_):
    return [str(h) for h in values_.get("--allow-host", [])]


def _host_matches(host, pattern):
    host, pattern = host.lower(), pattern.lower()
    if pattern.startswith("*."):
        return host == pattern[2:] or host.endswith(pattern[1:])
    return host == pattern


def problems(sai, board, policy=None):
    """What the launcher or the administrator's POLICY would refuse ("error"), and what has no effect or
    contradicts itself ("warning"). The launcher's dry run has the last word; this is what can be said at
    once, node by node."""
    found = []
    agent_node = board.first("agent")
    agent, values_, _ = values(board)
    proxy, network, git = board.first("proxy"), board.first("network"), board.first("git")

    def add(level, text, node=None):
        found.append(Problem(level, text, node.id if node is not None else None))
    for marker, node_id in placeholders(board):
        kind = PLACEHOLDER.fullmatch(marker)[1]
        found.append(Problem("error", tr("Fill in {placeholder}: {what}.", placeholder=marker,
                                         what=tr(PLACEHOLDER_KINDS[kind])), node_id))
    known = tuple(getattr(sai, "agents", AGENTS))
    if agent not in known:
        add("error", tr("Unknown agent {agent}: this sandbox-ai has {list}.", agent=printable(agent) or "?",
                        list=", ".join(known)), agent_node)
    if not board.of_kind("workspace"):
        add("warning", tr("No workspace: the folder the sandbox is started in becomes /work."))
    for node in board.nodes:
        if not board.connected(node):
            add("warning", tr("{name} is not connected to the agent, so it is not used.", name=node.title()), node)
    for node in board.of_kind("workspace"):
        path = str(node.props.get("path", "")).strip()
        if not path:
            add("error", tr("A workspace needs a folder."), node)
        elif "," in path or "\n" in path:
            add("error", tr("A workspace path cannot contain commas or new lines."), node)
    workspaces = board.of_kind("workspace")
    if len({bool(n.props.get("readonly")) for n in workspaces}) > 1:
        add("warning", tr("Read-only applies to every workspace (--workspace-ro), not to one."), workspaces[0])
    for node in board.of_kind("mount"):
        if node.props.get("mode") not in ("ro", "rw"):
            add("error", tr("A mount is read-only (ro) or writable (rw)."), node)
        if not str(node.props.get("dest", "")).startswith(("/work", "/mnt", "/opt")):
            add("error", tr("A mount goes under /work, /mnt or /opt."), node)
    offline = values_.get("--offline") or "offline" in values_.get("--profile", [])
    widening = [o for o in ("--allow", "--allow-host", "--allow-tcp", "--allow-host-port", "--frontier", "--dns",
                            "--nameserver", "--egress-all-allow", "--network", *PRESET_OPTIONS) if values_.get(o)]
    if offline and (widening or set(values_.get("--profile", [])) - {"offline"}):
        add("error", tr("Offline contradicts the network permissions: {options}.",
                        options=", ".join(widening) or "--profile"), network)
    tokens = [o for o in ("--git-token-env", "--git-token-file", "--git-token-secret") if values_.get(o)]
    if offline and (tokens or values_.get("--git-credentials")):
        add("error", tr("Git tokens need network access to their server; offline has none."), git)
    proxy_on = not values_.get("--no-proxy") and not offline
    if values_.get("--allow-http") and not proxy_on:
        add("error", tr("Requests per host (--allow-http) need the connection proxy."), proxy)
    if values_.get("--token-proxy"):
        if not proxy_on:
            add("error", tr("The token proxy needs the connection proxy."), git)
        if values_.get("--git-credentials"):
            add("error", tr("The token proxy takes --git-token-env, --git-token-file or --git-token-secret, not "
                            "--git-credentials."), git)
        for option in tokens:
            for value in values_.get(option, []):
                if str(value).startswith("http://"):
                    add("error", tr("The token proxy adds tokens only to HTTPS servers: {url}",
                                    url=printable(str(value).partition("=")[0])), git)
    if values_.get("--no-inspect") and not proxy_on:
        add("warning", tr("--no-inspect has no effect without the connection proxy."), proxy)
    for rule in values_.get("--allow-http", []):
        match = HTTP_RULE.fullmatch(str(rule).strip())
        if not match:
            add("error", tr("A request rule is 'HOST METHODS PATH', e.g. 'api.github.com GET,HEAD /repos/*': {rule}",
                            rule=printable(rule)), proxy)
            continue
        host = (match[1] or "") + match[2]
        if any(_host_matches(host.lstrip("*."), p) or _host_matches(p.lstrip("*."), host)
               for p in values_.get("--no-inspect", [])):
            add("error", tr("Requests to {host} are not seen under --no-inspect, so rules for it cannot hold.",
                            host=printable(host)), proxy)
    engine = values_.get("--backend", "podman")
    if values_.get("--allow-host-port") and (engine != "podman" or values_.get("--network")):
        add("error", tr("Host ports need the Podman backend with its default network."), network)
    vpn = board.first("vpn")
    if values_.get("--vpn"):
        if engine != "podman":
            add("error", tr("A VPN needs the Podman backend."), vpn)
        if offline:
            add("error", tr("A VPN needs network; offline has none."), vpn)
        if values_.get("--nameserver"):
            add("error", tr("With a VPN the lookups go to its resolvers: no --nameserver."), vpn)
    elif values_.get("--vpn-check", "stop") != "stop":
        add("warning", tr("The check mode has no effect without a VPN profile."), vpn)
    if values_.get("--local-config") and agent != "opencode":
        add("error", tr("--local-config is for OpenCode only."), agent_node)
    if values_.get("--tmux") and values_.get("--no-tmux"):
        add("error", tr("tmux cannot be both required and switched off."), board.first("session"))
    if values_.get("--copy-auth") and values_.get("--no-auth"):
        add("error", tr("Copy the login or copy none: not both."), board.first("home"))
    for value in values_.get("--nameserver", []):
        try:
            ipaddress.ip_address(str(value))
        except ValueError:
            add("error", tr("A resolver is one IP address: {value}", value=printable(value)), network)
    for value in values_.get("--allow", []):
        try:
            ipaddress.ip_network(str(value), strict=False)
        except ValueError:
            add("error", tr("Not an address or network: {value}", value=printable(value)), network)
    for value in values_.get("--allow-host-port", []):
        if not str(value).isdigit() or not 1 <= int(value) <= 65535:
            add("error", tr("Not a port: {value}", value=printable(value)), network)
    approvals = board.first("approvals")
    rules = approvals.props.get("rules") if approvals is not None else None
    if rules is not None:
        if rules.get("http") if isinstance(rules, dict) else False:
            if not proxy_on:
                add("error", tr("The project's HTTP rules need the connection proxy."), approvals)
        found += _checked(lambda: sai.launcher.RUNTIME.Ruleset.validated(rules, "rules"), approvals)
    for node in board.of_kind("mcp"):
        definition = node.props.get("definition")
        if node.props.get("project") and isinstance(definition, dict) and node.props.get("name"):
            found += _checked(lambda: sai.module("mcp").validated({node.props["name"]: definition},
                                                                   tr("MCP server")), node)
    found += policy_problems(board, values_, policy or {})
    return found


def _checked(function, node):
    """[Problem] of what sandbox-ai's own check FUNCTION refuses (nothing when this sandbox-ai cannot check)."""
    try:
        function()
    except ValueError as exc:
        return [Problem("error", printable(str(exc)), node.id if node is not None else None)]
    except Exception:  # noqa: BLE001 - an older sandbox-ai without the check: the launcher has the last word
        pass
    return []


def policy_problems(board, values_, policy):
    """What the administrator's policy refuses, as the launcher would (forbid, backends, allow_within,
    allow_hosts, mcp_network)."""
    found = []
    if not policy:
        return found
    for entry in policy.get("forbid", []):
        option, equals, wanted = str(entry).partition("=")
        value = values_.get(option)
        used = (wanted in [str(v) for v in value] if isinstance(value, list) else str(value) == wanted) if equals \
            else bool(value)
        if option == "--egress-all-allow" and "egress-all" in values_.get("--profile", []):
            used = True
        if used:
            node = board.first(OWNER.get(option, "agent"))
            found.append(Problem("error", tr("The administrator's policy forbids {option}.", option=entry),
                                 node.id if node else None))
    backend_ = values_.get("--backend", "podman")
    if "backends" in policy and backend_ not in policy["backends"]:
        found.append(Problem("error", tr("The administrator's policy allows only these backends: {list}.",
                                         list=", ".join(policy["backends"])),
                             (board.first("engine") or board.first("agent")).id))
    network = board.first("network")
    offline = values_.get("--offline") or "offline" in values_.get("--profile", [])
    required = policy.get("require_vpn")
    if required and not offline:
        node = board.first("vpn") or network
        if not values_.get("--vpn"):
            found.append(Problem("error", tr("The administrator's policy requires a VPN."), node.id if node else None))
        elif isinstance(required, list) and values_.get("--vpn") not in required:
            found.append(Problem("error", tr("The administrator's policy allows only these VPN profiles: {list}.",
                                             list=", ".join(map(str, required))), node.id if node else None))
        elif values_.get("--vpn-check", "stop") != "stop":
            found.append(Problem("error", tr("The administrator's policy requires a VPN: failed checks stop the start."),
                                 node.id if node else None))
    if policy.get("allow_within") is not None:
        ranges = []
        for value in policy["allow_within"]:
            try:
                ranges.append(ipaddress.ip_network(value, strict=False))
            except ValueError:
                pass
        if values_.get("--egress-all-allow") or "egress-all" in values_.get("--profile", []):
            found.append(Problem("error", tr("The administrator's policy has a network ceiling: no egress-all."),
                                 network.id if network else None))
        for option in ("--allow", "--allow-tcp", "--nameserver"):
            for value in values_.get(option, []):
                address = str(value).rsplit(":", 1)[0].strip("[]") if option == "--allow-tcp" else str(value)
                try:
                    net = ipaddress.ip_network(address, strict=False)
                except ValueError:
                    continue
                if not any(net.version == r.version and net.subnet_of(r) for r in ranges):
                    found.append(Problem("error", tr("{value} lies outside the administrator's network ceiling.",
                                                     value=printable(value)), network.id if network else None))
    if "allow_hosts" in policy or policy.get("allow_within") is not None:
        allowed = policy.get("allow_hosts", [])
        for host in _hosts_of(values_):
            if not any(_host_matches(host, p) for p in allowed):
                found.append(Problem("error", tr("The administrator's policy does not list {host} in allow_hosts.",
                                                 host=printable(host)), network.id if network else None))
    return found


SECRET_NAME = re.compile(r"(?i)(secret|token|passw|pwd|key|credential|cookie|session|auth|private|^aws_|^gh_|"
                         r"^github_|^gitlab_|^openai|^anthropic|^azure|^gcp|^google)")
SENSITIVE_PARTS = {".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", ".netrc", ".git-credentials", ".password-store",
                   ".config", ".local", ".mozilla", ".pki", "id_rsa", "id_ed25519", ".vault-token", ".npmrc", ".pypirc"}
CAPABILITIES = ("read", "write", "reach", "secrets", "code")


def sensitive(path, launcher=None):
    """Why the agent should not see host PATH ("bad"), or a note ("warn"), or None for an ordinary folder.
    The launcher's own private_locations() (its settings, keys and program directories) count when given."""
    try:
        resolved = Path(os.path.expanduser(str(path))).absolute().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return "warn", tr("a path that cannot be resolved")
    home = Path.home().resolve()
    if resolved == Path("/"):
        return "bad", tr("the whole file system")
    if resolved == home or resolved in home.parents:
        return "bad", tr("your HOME: keys, shell start files, every other project")
    private = []
    if launcher is not None and hasattr(launcher, "private_locations"):
        try:
            fixed, program_dirs = launcher.private_locations()
            private = list(fixed) + list(program_dirs)
        except Exception:  # noqa: BLE001 - the parts below still say enough
            private = []
    for location in private:
        location = Path(location)
        if resolved == location or location in resolved.parents:
            return "bad", tr("settings, keys or programs of the host ({path})", path=printable(location))
        if resolved in location.parents:
            return "bad", tr("contains {path} (settings, keys or programs of the host)", path=printable(location))
    hit = next((part for part in resolved.parts if part in SENSITIVE_PARTS), None)
    if hit:
        return "bad", tr("holds {name}: keys or settings", name=printable(hit))
    if str(resolved).startswith(("/etc", "/var", "/usr", "/boot", "/root", "/proc", "/sys", "/dev")):
        return "warn", tr("a system directory")
    try:
        world = resolved.is_dir() and resolved.stat().st_mode & 0o002
    except OSError:
        world = False
    if world or str(resolved) in ("/tmp", "/var/tmp", "/dev/shm"):
        return "warn", tr("a directory every user can write")
    return None


def _network_level(value):
    """("bad"|"warn", why) of an address or network the agent may reach."""
    try:
        net = ipaddress.ip_network(str(value).strip("[]"), strict=False)
    except ValueError:
        return "warn", tr("not an address")
    if net.is_loopback:
        return "bad", tr("this host's loopback")
    if net.is_private or net.is_link_local or net.is_reserved:
        return "bad", tr("an internal network (LAN, VPN, containers)")
    if net.prefixlen < (16 if net.version == 4 else 32):
        return "bad", tr("a large part of the internet")
    return "warn", tr("a public address")


@dataclass
class Capability:
    area: str  # read, write, reach, secrets, code
    level: str  # bad, warn, ok, info
    text: str
    node: int | None = None
    cost: int = 0


def capabilities(board, launcher=None, definitions=None):
    """What the agent of BOARD can READ and WRITE on the host, REACH on the network, which SECRETS it holds and
    which CODE from outside it runs - each with a level and the points it costs the score. DEFINITIONS:
    {name: MCP server definition} for servers defined outside the board (mcp.json)."""
    agent, values_, _ = values(board)
    found = []

    def add(area, level, text, node=None, cost=0):
        found.append(Capability(area, level, text, node.id if node is not None else None, cost))

    def folder(area, path, writable, node, how):
        """One entry per area; a writable folder costs its points once, in the WRITE area."""
        verdict = sensitive(path, launcher)
        mode = tr("writable") if writable else tr("read-only")
        label = tr("{how}: {path} ({mode})", how=how, path=printable(path), mode=mode)
        counted = area == "write" or not writable
        if verdict and verdict[0] == "bad":
            add(area, "bad", f"{label} - {verdict[1]}", node, (30 if writable else 20) if counted else 0)
        elif verdict:
            add(area, "warn", f"{label} - {verdict[1]}", node, (8 if writable else 4) if counted else 0)
        else:
            extra = writable and how != tr("Workspace")
            add(area, "warn" if extra else "info", label, node, 5 if extra and counted else 0)
    readonly = bool(values_.get("--workspace-ro"))
    for node in board.included():
        if node.kind == "workspace" and str(node.props.get("path", "")).strip():
            for area in ("read", "write") if not readonly else ("read",):
                folder(area, node.props["path"], not readonly, node, tr("Workspace"))
        elif node.kind == "mount" and node.props.get("source"):
            writable = node.props.get("mode") == "rw"
            for area in ("read", "write") if writable else ("read",):
                folder(area, node.props["source"], writable, node, tr("Mount"))
    home = board.first("home")
    if values_.get("--state-dir"):
        folder("write", values_["--state-dir"], True, home, tr("HOME kept in"))
    for value in values_.get("--copy", []):
        source = str(value).rsplit(":", 1)[0]
        level = "bad" if sensitive(source, launcher) else "warn"
        add("secrets", level, tr("File copied into HOME: {path}", path=printable(source)), home, 8)
    kits = board.first("kits")
    for value in values_.get("--kit-dir", []):
        add("code", "warn", tr("Kit copied into HOME from {path} (its files run inside)", path=printable(value)), kits, 5)
    # where it can go
    network = board.first("network")
    profiles = values_.get("--profile", [])
    offline = values_.get("--offline") or "offline" in profiles
    if offline:
        add("reach", "ok", tr("Offline: no network at all"), network)
    else:
        if values_.get("--egress-all-allow") or "egress-all" in profiles:
            add("reach", "bad", tr("Egress-all: every outbound destination"), network, 40)
        if not values_.get("--no-provider-defaults"):
            add("reach", "info", tr("The agent's own provider"), network)
        if values_.get("--frontier") or "frontier" in profiles or "development" in profiles:
            add("reach", "warn", tr("The OpenAI and Anthropic APIs (any account: a way out for data)"), network, 3)
        presets = [o[len("--allow-"):] for o in PRESET_OPTIONS if values_.get(o)]
        presets += ["pypi", "github"] if "development" in profiles else []
        for name in dict.fromkeys(presets):
            add("reach", "info", tr("Preset {name} (HTTPS)", name=name), network, 1)
        for host in values_.get("--allow-host", []):
            if "*" in str(host):
                add("reach", "bad", tr("Every host matching {host}", host=printable(host)), network, 15)
            else:
                add("reach", "info", tr("{host} (HTTPS)", host=printable(host)), network, 1)
        for port in values_.get("--allow-host-port", []):
            ssh = str(port) == "22"
            add("reach", "bad" if ssh else "warn",
                tr("Port {port} on this host's loopback{ssh}", port=printable(port),
                   ssh=tr(" (SSH into the host)") if ssh else ""), network, 15 if ssh else 8)
        for value in values_.get("--allow-tcp", []):
            address = str(value).rsplit(":", 1)[0]
            level, why = _network_level(address)
            add("reach", level, tr("TCP {target}: {why}", target=printable(value), why=why), network,
                10 if level == "bad" else 4)
        for value in values_.get("--allow", []):
            level, why = _network_level(value)
            add("reach", level, tr("Every port and protocol to {value}: {why}", value=printable(value), why=why),
                network, 15 if level == "bad" else 6)
        if values_.get("--dns") or values_.get("--nameserver"):
            add("reach", "warn", tr("Real DNS lookups (a way out for small amounts of data)"), network, 3)
        if values_.get("--network") or values_.get("--ip") or values_.get("--ip6"):
            add("reach", "warn", tr("A Podman network of its own: {name}", name=printable(values_.get("--network", ""))),
                network, 5)
    proxy = board.first("proxy")
    proxy_on = not values_.get("--no-proxy") and not offline
    if values_.get("--no-proxy") and not offline:
        add("reach", "bad", tr("No connection proxy: no connection log, pushes checked only by the hooks"), proxy, 25)
    elif proxy_on:
        add("reach", "ok", tr("Connection proxy: TLS inspected, every request logged"), proxy)
    for host in values_.get("--no-inspect", []):
        add("reach", "warn", tr("TLS not inspected for {host}", host=printable(host)), proxy, 5)
    if values_.get("--allow-http"):
        add("reach", "ok", tr("Requests narrowed per host ({count} rules)", count=len(values_["--allow-http"])), proxy)
    if values_.get("--privacy-check") == "off":
        add("reach", "bad", tr("Privacy check off: pushes and SSH are not checked"), proxy, 15)
    if values_.get("--llm"):
        add("reach", "info", tr("LLM server {name}: its address and port only", name=printable(values_["--llm"])),
            board.first("llm"), 1)
    approvals = board.first("approvals")
    rulesets = len(values_.get("--rules", [])) + (1 if approvals is not None and
                                                    isinstance(approvals.props.get("rules"), dict) else 0)
    if rulesets:
        add("reach", "ok", tr("Rulesets allow, ask or deny each request and tool call ({count})", count=rulesets),
            approvals)
    if values_.get("--ask") == "off" and not offline:
        add("reach", "info", tr("Nothing is asked: what the options do not allow is refused"), board.first("approvals"))
    if values_.get("--ask") == "allow" and proxy_on:
        # Every question answered yes at once (sandbox-ai 0.1.80): any destination is reached, logged all the same.
        add("reach", "bad", tr("Nothing is asked: every question is allowed at once, so any destination can be "
                               "reached (each one logged)"), board.first("approvals"), 30)
    # secrets
    agent_node, git = board.first("agent"), board.first("git")
    for value in values_.get("--secret-env", []):
        add("secrets", "warn", tr("Secret {name} as a variable", name=printable(value)), agent_node, 8)
    for name in values_.get("--env", []):
        if SECRET_NAME.search(str(name)):
            add("secrets", "bad", tr("Host variable {name} (looks like a secret)", name=printable(name)), agent_node, 10)
        else:
            add("secrets", "info", tr("Host variable {name}", name=printable(name)), agent_node, 1)
    if values_.get("--copy-auth"):
        add("secrets", "warn", tr("The agent's login is copied in"), home, 5)
    if values_.get("--auth-file"):
        add("secrets", "warn", tr("Login file {path}", path=printable(values_["--auth-file"])), home, 8)
    if values_.get("--import-codex-auth"):
        add("secrets", "warn", tr("Codex login imported from {path}", path=printable(values_["--import-codex-auth"])),
            home, 5)
    tokens = [v for o in ("--git-token-env", "--git-token-file", "--git-token-secret") for v in values_.get(o, [])]
    if tokens and not values_.get("--token-proxy"):
        for value in tokens:
            add("secrets", "warn", tr("Git token visible to the agent: {server} (the token proxy keeps it out)",
                                      server=printable(str(value).partition("=")[0])), git, 15)
    elif tokens:
        add("secrets", "ok", tr("Git tokens only in the proxy: the agent never sees them"), git)
    elif values_.get("--token-proxy"):
        add("secrets", "info", tr("Token proxy ready: tokens you add stay out of the sandbox"), git)
    if values_.get("--git-credentials"):
        add("secrets", "bad", tr("Every login in {path} is visible to the agent",
                                 path=printable(values_["--git-credentials"])), git, 15)
    # code from outside, display, devices
    engine = board.first("engine")
    image = values_.get("--image")
    if image and not re.fullmatch(rf"localhost/{re.escape(agent)}-sandbox(:[A-Za-z0-9._-]+)?", str(image)):
        add("code", "bad", tr("Image {image}: everything in the sandbox comes from it", image=printable(image)),
            engine, 15)
    if values_.get("--image-variant"):
        add("code", "warn", tr("Image variant {name}: built from your extension of the image",
                               name=printable(values_["--image-variant"])), engine, 5)
    if values_.get("--binary"):
        verdict = sensitive(values_["--binary"], launcher)
        add("code", "bad", tr("Agent program {path}{why}", path=printable(values_["--binary"]),
                              why=f" - {verdict[1]}" if verdict else ""), engine, 15)
    for value in values_.get("--runtime", []):
        add("code", "warn", tr("Runtime folder {path} (read-only, runs inside)", path=printable(value)), engine, 8)
    if values_.get("--podman-url"):
        add("code", "warn", tr("Another Podman service: {url}", url=printable(values_["--podman-url"])), engine, 10)
    display = board.first("display")
    if values_.get("--gui-vnc"):
        add("write", "warn", tr("VNC socket in {path}: whoever reaches it sees and controls the display",
                                path=printable(values_["--gui-vnc"])), display, 5)
    if values_.get("--gui-wayland"):
        add("read", "bad", tr("The host's Wayland socket weakens the isolation"), display, 15)
    devices = board.first("devices")
    for value in values_.get("--device", []):
        add("read", "bad", tr("Host device {path}", path=printable(value)), devices, 15)
    if values_.get("--gpu"):
        add("read", "warn", tr("GPU devices ({kind})", kind=printable(values_["--gpu"])), devices, 10)
    if values_.get("--dangerously-skip-permissions"):
        add("code", "info", tr("The agent runs without its own approval prompts (the sandbox stays in force)"),
            agent_node, 3)
    # MCP servers: their folders and network count for the agent that uses them
    for node in board.included():
        if node.kind != "mcp":
            continue
        name = node.props.get("name", "")
        definition = node.props.get("definition") or (definitions or {}).get(name)
        if not isinstance(definition, dict):
            continue
        how = tr("MCP server {name}", name=printable(name))
        reaches, hosts, vpn = server_network(definition)
        if reaches:
            hosts = ", ".join(hosts) or tr("every destination")
            if vpn:
                add("reach", "warn", tr("{how} reaches the network through the VPN {vpn}: {hosts}", how=how,
                                        vpn=printable(vpn), hosts=printable(hosts)), node, 5)
            else:
                add("reach", "warn", tr("{how} reaches the network: {hosts}", how=how, hosts=printable(hosts)), node, 5)
        for mount in definition.get("mounts", []):
            if isinstance(mount, dict) and mount.get("host"):
                writable = mount.get("mode") == "rw"
                for area in ("read", "write") if writable else ("read",):
                    folder(area, mount["host"], writable, node, how)
        if definition.get("command") and not definition.get("image"):
            add("code", "warn", tr("{how} runs {command} in a sandbox of its own", how=how,
                                   command=printable(" ".join(map(str, definition["command"])))), node, 5)
    return found


def badges(board, policy=None, launcher=None, definitions=None):
    """(score 0..100, [Badge]) from capabilities() (what weakens the sandbox and what keeps it tight) and the
    administrator's policy."""
    found = [Badge(c.level, c.text, c.node, c.cost) for c in capabilities(board, launcher, definitions)]
    _, values_, _ = values(board)
    for problem in policy_problems(board, values_, policy or {}):
        found.append(Badge("bad", problem.text, problem.node, 0))
    score = max(0, min(100, 100 - sum(b.cost for b in found)))
    return score, found


def panels(board, launcher=None, definitions=None):
    """{area: [Capability]} for the panels beside the graph: READ, WRITE, REACH, SECRETS, CODE."""
    found = {area: [] for area in CAPABILITIES}
    for item in capabilities(board, launcher, definitions):
        found[item.area].append(item)
    return found


def server_network(definition):
    """(reaches the network, [what it reaches], VPN profile or None) of an MCP server's DEFINITION: an image's
    network and hosts, or the launcher options of a server with a command (no network unless they allow it)."""
    if "image" in definition:
        return (definition.get("network", "none") != "none", [str(h) for h in definition.get("hosts", [])],
                definition.get("vpn"))
    options, reached, vpn, everything = [str(o) for o in definition.get("options", [])], [], None, False
    for index, option in enumerate(options):
        value = options[index + 1] if index + 1 < len(options) else ""
        if option in PRESET_OPTIONS:
            reached.append(option[len("--allow-"):])
        elif option in ("--allow-host", "--allow", "--allow-tcp"):
            reached.append(value)
        elif option == "--allow-host-port":
            reached.append(f"localhost:{value}")
        elif option == "--egress-all-allow" or (option == "--profile" and value == "egress-all"):
            everything = True
        elif option == "--profile" and value in ("development", "frontier"):
            reached.append(value)
        elif option == "--vpn":
            vpn = value
    if everything:
        reached = []
    return everything or bool(reached), reached, vpn


def mcp_summary(definition):
    """[(label, value)] of an MCP server's definition, for its node: what runs, its network and folders."""
    if not isinstance(definition, dict):
        return [(tr("Definition"), tr("not defined: add it on the MCP servers page"))]
    runs = definition.get("image") or " ".join(map(str, definition.get("command", []))) or "?"
    mounts = [f"{m.get('host', '?')} → {m.get('target', '?')} ({m.get('mode', 'ro')})"
              for m in definition.get("mounts", []) if isinstance(m, dict)]
    network = definition.get("network", "none")
    if "image" not in definition:
        reaches, hosts, _ = server_network(definition)
        network = (", ".join(hosts) or tr("every destination")) if reaches else "none"
    rows = [(tr("Runs"), printable(runs)), (tr("Network"), printable(network))]
    if server_network(definition)[2]:
        rows.append((tr("VPN"), printable(server_network(definition)[2])))
    if definition.get("hosts"):
        rows.append((tr("Hosts"), printable(", ".join(map(str, definition["hosts"])))))
    rows.append((tr("Folders"), printable("; ".join(mounts)) if mounts else tr("none")))
    return rows


def with_backend(board, name):
    """BOARD started with the default backend NAME of the settings (podman is the launcher's own default)."""
    if name and name != "podman":
        engine = board.first("engine") or board.add("engine")
        engine.props.setdefault("--backend", name)
    return board


# -- templates: the presets of sandbox_ai_app/presets.py --

def template(sai, name, directory=None, agent=None, values=None):
    """The board of preset NAME for the project DIRECTORY (see presets.build)."""
    from . import presets
    return presets.build(sai, presets.find(name), directory, agent=agent, values=values)


# -- the layout file next to the project file --

def layout_path(directory):
    return os.path.join(str(directory), LAYOUT_NAME)


def read_layout(directory):
    """The positions saved for the project in DIRECTORY ({} without, or when the file is not a small regular
    JSON file: the folder is the agent's to write, so it is read as untrusted data)."""
    path = layout_path(directory)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return {}
    try:
        import stat as stat_
        if not stat_.S_ISREG(os.fstat(fd).st_mode):
            return {}
        data = os.read(fd, LAYOUT_LIMIT + 1)
    finally:
        os.close(fd)
    if len(data) > LAYOUT_LIMIT:
        return {}
    try:
        layout = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        return {}
    return layout if isinstance(layout, dict) else {}


def write_layout(directory, board):
    """Save the positions next to the project file (never through a link: a new file renamed over it)."""
    import secrets as secrets_
    path = layout_path(directory)
    temporary = f"{path}.{os.getpid()}-{secrets_.token_hex(4)}.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), 0o644)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(board.positions(), stream, indent=1)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return path
