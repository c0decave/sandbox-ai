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

import argparse
from dataclasses import dataclass, field
import ipaddress
import itertools
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import shlex
import weakref

from . import applog
from . import backend
from . import bridges as BR
from . import llm_params as LP
from . import remote_source
from .files import printable, printable_text
from .i18n import N_, tr

AGENTS = ("claude", "codex", "opencode", "opencode2", "hermes", "dsh", "qwen", "goose", "openhands", "nanobot", "letta", "pi")
# The agents a model server gives only the start's model (sandbox-ai's llm.ONE_MODEL; a test keeps both the same):
# --llm-models offers no other in them.
ONE_MODEL_AGENTS = ("openhands", "nanobot", "letta", "pi")
LAYOUT_NAME = ".sandbox-ai.board.json"  # node positions only; the options live in .sandbox-ai.json
LAYOUT_LIMIT = 256 << 10
# Requests per host, as the launcher checks them (agent_sandbox.HTTP_RULE).
HTTP_RULE = re.compile(r"(\*\.)?([a-z0-9-]{1,63}(?:\.[a-z0-9-]{1,63})+) +(\*|[A-Z]{3,10}(?:,[A-Z]{3,10})*) +"
                       r"(/[!-~]{0,500})")
PRESET_OPTIONS = ("--allow-pypi", "--allow-github", "--allow-gitlab", "--allow-kernel-org", "--allow-duckduckgo",
                  "--allow-npm")

WORKSPACE_FOLDER = "@workspace"  # an MCP server's folder bound to the start's workspace (mcp.WORKSPACE_HOST)


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
    # Project folder (--workspace) and extra folder (--mount): both give the sandbox a folder of this machine; a
    # project folder is where the agent works (git, the push check, /work), an extra folder is data at a path you
    # choose.
    Kind("workspace", N_("Project folder"), single=False, column=0,
         help=N_("Your project: the agent works in it (the first at /work, more at /workspaces/NAME); git and the "
                 "push check see it")),
    Kind("mount", N_("Extra folder"), single=False, column=0,
         help=N_("Another folder or file of this machine at a path you choose (e.g. /mnt/data), read-only or "
                 "writable: data, models, caches - not where the agent works")),
    Kind("home", N_("HOME and login"), ("--state-dir", "--copy", "--copy-auth", "--no-auth", "--auth-file",
                                        "--auth-from-session", "--import-codex-auth"), column=0,
         help=N_("The agent's HOME in the sandbox - the start's own, or a folder of yours that stays - and where "
                 "its login comes from (the stored one, a file, another session, none)")),
    Kind("kits", N_("Kits, memory, plugins"), ("--kit", "--kit-dir", "--no-default-kit", "--no-memory",
                                               "--no-plugins"), column=0,
         help=N_("Kits give the agent skills, commands, subagents and hooks (read-only in its HOME); project memory "
                 "keeps its notes for this project between runs; plugins come from sandbox-ai's plugin store")),
    Kind("agent", N_("Agent"), ("--dangerously-skip-permissions", "--local-config", "--env", "--secret-env",
                                "--api-key", "--hermes-enable", "--qwen-enable",
                                "--goose-enable", "--openhands-enable", "--nanobot-enable", "--letta-enable",
                                "--opencode2-enable",
                                "--pi-enable",
                                # The agent's intro (/sandbox/session/INTRO.md: what its sandbox has): on unless the
                                # part says no (--no-intro), or yes over a default of no (--intro).
                                "--no-intro", "--intro"),
         required=True, column=1, help=N_("Which coding agent runs in the sandbox, its model and effort, and "
                                          "whether it asks before it runs a tool")),
    Kind("approvals", N_("Approvals and rules"), ("--ask", "--rules", "--hide"), column=1,
         help=N_("Ask before what the options do not allow; rulesets allow, ask or deny each request and MCP "
                 "tool call; files such as .env hidden from the agent")),
    Kind("proxy", N_("Connection proxy"), ("--no-inspect", "--allow-http", "--privacy-check", "--privacy-rules"),
         required=True, column=2,
         help=N_("Every connection passes it: TLS opened and logged, pushes checked, requests per host")),
    Kind("git", N_("Git"), ("--git-identity", "--git-token-env", "--git-token-file", "--git-token-secret",
                            "--token-proxy", "--git-credentials"), column=1,
         help=N_("Commit identity and push tokens (with the token proxy the agent never sees them)")),
    Kind("mcp", N_("MCP server"), single=False, column=2,
         help=N_("An MCP server gives the agent more tools (a search, a database, a browser ...). It runs in a "
                 "sandbox of its own with its own network and folders; the agent reaches only its tools")),
    Kind("service", N_("Service (container)"), single=False, column=2,
         help=N_("A container of one of your images (a database, a cache, a search engine, a test target) beside the "
                 "agent, in the sandbox's pod: reached at localhost:PORT, behind the same firewall and proxy. Its image "
                 "is unchecked: nobody reviewed it")),
    Kind("llm", N_("LLM server"), ("--llm", "--use-case", "--llm-models", "--llm-tools", "--no-llm-profile"), column=2,
         help=N_("A model on your own server (sandbox-ai llm): only its address is opened, its key stays with "
                 "the proxy")),
    Kind("network", N_("Network"), ("--profile", *PRESET_OPTIONS, "--frontier", "--no-provider-defaults",
                                    "--allow-host", "--allow-domain", "--allow-shared-wildcard", "--allow",
                                    "--allow-tcp", "--allow-host-port",
                                    "--dns",
                                    "--nameserver", "--offline", "--egress-all-allow", "--network", "--ip", "--ip6",
                                    "--subnet", "--subnet6"),
         required=True, column=3, help=N_("Where connections may go: hosts, presets, addresses, ports, DNS")),
    Kind("vpn", N_("VPN"), ("--vpn", "--vpn-check", "--vpn-bypass-host-ports", "--vpn-bypass-tcp"), column=2,
         help=N_("A VPN profile in front (sandbox-ai vpn): everything leaves through the tunnel, a kill switch "
                 "blocks the rest, addresses and DNS are checked before the start")),
    Kind("devices", N_("Devices and GPU"), ("--device", "--gpu"), column=0,
         help=N_("Devices of this computer the agent may open: a serial port, a USB device, a camera, a GPU. Each "
                 "one weakens the isolation")),
    # Build toolchains (--toolchain): from the signed catalog into the host's store (sandbox-ai toolchain add), and
    # from there read-only into the sandbox at /opt/toolchains/NAME. One part with the list of them, as Devices.
    Kind("toolchains", N_("Toolchains"), ("--toolchain", "--toolchain-offline"), column=0,
         help=N_("A toolchain is a compiler or SDK (Go, Rust, a JDK, Node, Python ...) from the signed catalog. It is "
                 "mounted read-only at /opt/toolchains/NAME, its commands on the agent's PATH - nothing is installed "
                 "in the image. The package registries it needs (crates.io for Rust) are opened at the start; the "
                 "security modes high and paranoid keep them closed")),
    Kind("limits", N_("Limits"), ("--memory", "--cpus", "--pids", "--tmp-size"), column=0,
         help=N_("The most the sandbox may use: memory, CPUs, processes and the size of /tmp")),
    Kind("display", N_("Display"), ("--gui", "--gui-screen", "--gui-vnc", "--gui-wayland"), column=0,
         help=N_("A virtual screen inside the sandbox, so that GUI programs and browsers can run there; a VNC "
                 "socket lets you watch it")),
    # The sandbox's own browser (--browser): a container beside the agent, on its network; the app's Browser button
    # shows it. For you (human), for the agent (agent: Chrome's DevTools in the pod) or both.
    Kind("browser", N_("Browser"), ("--browser", "--browser-control", "--browser-profile"), column=2,
         help=N_("A browser in its own container beside the agent (Podman), on the agent's network: firewall, proxy, "
                 "approvals, kill switch, VPN or Tor. For you (the Browser button shows it), for the agent (it drives "
                 "it through Chrome's DevTools) or both")),
    # Cairn (--cairn): its board in a container beside the agent, opened in the sandbox's browser; the session's agents
    # are its workers (cairn.py, cairn_relay.py).
    Kind("cairn", N_("Cairn"), ("--cairn", "--cairn-workers", "--cairn-origin", "--cairn-goal", "--cairn-hint",
                                "--cairn-board"), column=2,
         help=N_("Cairn's board of facts, intents and hints in its own container beside the agent (Podman), on the "
                 "agent's network, opened in the sandbox's browser; the session's agents (any harness, each with "
                 "its traffic light) are its workers, with their own logins and models")),
    # ARTEX (--artex): an AI pentest platform in its own container beside the agent, built from its reviewed source;
    # its console forwarded to this computer's loopback (artex.py).
    Kind("artex", N_("ARTEX"), ("--artex", "--artex-data", "--artex-port"), column=2,
         help=N_("ARTEX, an AI penetration-testing platform, in its own container beside the agent (Podman), on the "
                 "agent's network: its targets only what the allow-list opens; its console on this computer's "
                 "127.0.0.1 (sandbox-ai artex console)")),
    Kind("session", N_("Session and tmux"), ("--tmux", "--no-tmux"), column=0,
         help=N_("Whether the agent runs in tmux inside the sandbox: with tmux you can detach from it and attach "
                 "again while it works")),
    Kind("logs", N_("Logs"), ("--log-dir", "--log-debug"), column=1,
         help=N_("Where the logs go (hidden from the agent), and how much of what is refused they keep")),
    # A session bridge (sandbox-ai bridge) to another running session of this machine: no launcher option - the app
    # makes it once the board's start runs, after the human confirmed its warnings. Kept with the board's draft, never
    # written into the project file (whoever can write a workspace must not be able to bridge sessions).
    Kind("bridge", N_("Bridge"), single=False, column=2,
         help=N_("Another running session of this machine the agent reaches, or that reaches it, through a session "
                 "bridge (sandbox-ai bridge): message, ask, files, watch. Made once the board's start runs, after "
                 "you confirm its warnings; not written into the project file")),
    # A team (sandbox-ai team): the board's sandbox once per agent, on its project folder, with a task board between
    # them. No launcher option of its own: with it the board's start is `team start` (team_request), on the board's
    # host, as the Teams page and the command line start one. Kept with the board's draft, never written into the
    # project file (it names no option of a sandbox).
    Kind("team", N_("Team"), column=2,
         help=N_("Several agents, a sandbox each with this board's settings, on its project folder: the lead talks to "
                 "you and hands the others tasks on the team's board (sandbox-ai team). With it the board's Start "
                 "starts the team; its board is shown here")),
    Kind("engine", N_("Engine"), ("--backend", "--image", "--image-variant", "--nested-containers", "--podman-url",
                                  "--binary", "--runtime"),
         column=0,
         help=N_("What isolates the agent - Podman (a container of sandbox-ai's image) or Bubblewrap (this "
                 "computer's own programs) -, which image, and another executable for the agent")),
    # Other options: the launcher options no other part represents (the security mode, canaries, a session's name,
    # checkpoints, a preset of sandbox-ai, tokens of another version ...), exactly as the command line has them
    # (props "tokens": Board.extra). No option is its own: it holds what is left, so a command line and the board are
    # always the same start; the Expert view has a field for each of them.
    Kind("other", N_("Other options"), column=0,
         help=N_("The options of the start no other part shows (the security mode, canaries, a name, checkpoints, "
                 "sandbox-ai's own presets ...), exactly as on the command line; the Expert view has a field for "
                 "each")),
)}
# Which edges may exist: (from kind, to kind). Workspaces, mounts and MCP servers count only when connected.
EDGES = {("workspace", "agent"), ("mount", "agent"), ("home", "agent"), ("kits", "agent"), ("agent", "proxy"),
         ("proxy", "network"), ("agent", "mcp"), ("approvals", "proxy"), ("git", "proxy"), ("devices", "agent"),
         ("toolchains", "agent"),
         ("limits", "agent"), ("display", "agent"), ("session", "agent"), ("logs", "proxy"), ("engine", "agent"),
         ("vpn", "network"), ("agent", "llm"), ("agent", "service"), ("service", "proxy"), ("browser", "proxy"),
         ("agent", "bridge"), ("agent", "cairn"), ("cairn", "proxy"), ("agent", "artex"), ("artex", "proxy"),
         ("agent", "team"), ("other", "agent")}
CONNECTED_ONLY = {"workspace", "mount", "mcp", "service", "bridge", "team"}
OWNER = {option: kind.key for kind in KINDS.values() for option in kind.options}
# Options shown as a property of their own: proxy "enabled" (--no-proxy), workspace "readonly" (--workspace-ro),
# the workspace, mount and MCP nodes themselves.
OWNER.update({"--no-proxy": "proxy", "--workspace-ro": "workspace", "--workspace": "workspace", "--mount": "mount",
              "--mcp": "mcp", "--mcp-limits": "mcp", "--mcp-env": "mcp", "--service": "service",
              "--llm-param": "llm"})
# A part's own settings (no launcher options) that are texts wherever a part has them: its name, path or choice.
TEXT_PROPS = frozenset(("name", "path", "source", "dest", "mode", "agent", "model", "effort", "thinking", "arguments",
                        "peer", "direction", "lead", "image", "raw", "entry"))
# What a part's settings hold of the command line besides its own options (--...): a start put onto a board
# (with_start) replaces these and the options, and keeps everything else a part holds (a project's own MCP
# definition, its ruleset, what a preset noted).
LINE_PROPS = {"agent": ("agent", "model", "arguments", "effort", "thinking"), "workspace": ("path", "readonly"),
              "mount": ("source", "dest", "mode"), "mcp": ("name", "limits", "variables"), "proxy": ("enabled",),
              "llm": ("params",), "other": ("tokens",)}
LINE_KINDS = ("workspace", "mount", "mcp", "service")  # the parts a command line names one by one (CONNECTED_ONLY)
LIMIT_KEYS = ("memory", "cpus", "pids", "tmp")  # an MCP node's own limits (props "limits"), --mcp-limits NAME:KEY=VALUE,...


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
                  "mcp": self.props.get("name"), "service": self.props.get("name"), "agent": self.props.get("agent"),
                  "llm": self.props.get("--llm"), "bridge": self.props.get("peer"),
                  "team": self.props.get("name")}.get(self.kind)
        return f"{name}: {printable(detail)}" if detail else name

    def key(self):
        """A stable name for the layout file (positions survive a reload of the options)."""
        detail = {"workspace": "path", "mount": "source", "mcp": "name", "service": "name",
                  "bridge": "peer"}.get(self.kind)
        return f"{self.kind}:{self.props.get(detail, '')}" if detail else self.kind


def snap(value, grid):
    return float(round(value / grid) * grid) if grid and grid > 1 else float(value)


def prop_set(value):
    """Whether a part's property VALUE sets something (an option given), not left empty or off."""
    return value not in (None, "", [], {}, False)


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


BOARD_SERIALS = itertools.count(1)


class Board:
    """The nodes and edges of one sandbox, plus what no node owns (EXTRA, kept as launcher tokens) and the
    keys of a project file the board does not edit (DOCUMENT_EXTRA: description, mcp_servers)."""

    def __init__(self):
        self.nodes, self.edges, self.document_extra = [], set(), {}
        self.labels = {}  # {placeholder: [what it is, where to get it]} of the preset the board came from
        self._ids = itertools.count(1)
        # A number no other board of this process has had (id() is handed to the next board once one is gone): what
        # a start being prepared is about (BoardPage.prerequisite_context).
        self.serial = next(BOARD_SERIALS)

    # -- what no other part owns: the tokens of the part Other options --

    @property
    def extra(self):
        """The launcher tokens no other part owns: those of the part Other options (its list itself), none without
        the part."""
        node = self.first("other")
        if node is None:
            return []
        if not isinstance(node.props.get("tokens"), list):
            node.props["tokens"] = []
        return node.props["tokens"]

    @extra.setter
    def extra(self, tokens):
        """TOKENS on the part Other options, which is put on the board for them when it is not there (beside the
        parts that are: place_free); without tokens a part that is there stays, empty."""
        tokens = list(tokens)
        node = self.first("other")
        if node is None:
            if not tokens:
                return
            node = self.add("other")
            self.place_free(node)
        node.props["tokens"] = tokens

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
                "document_extra": json.loads(json.dumps(self.document_extra)),
                **({"labels": json.loads(json.dumps(self.labels))} if self.labels else {})}

    @classmethod
    def from_state(cls, state):
        board = cls()
        board.nodes = [Node(n["id"], n["kind"], json.loads(json.dumps(n["props"])), n["x"], n["y"])
                       for n in state["nodes"]]
        board.edges = {tuple(e) for e in state["edges"]}
        board._ids = itertools.count(max([n.id for n in board.nodes] or [0]) + 1)
        # (a state from before the part Other options has its tokens here only: the part is made for them)
        board.extra, board.document_extra = list(state["extra"]), json.loads(json.dumps(state["document_extra"]))
        labels = state.get("labels") if isinstance(state.get("labels"), dict) else {}
        board.labels = {key: [str(v)[:300] for v in value[:2]] for key, value in labels.items()
                        if isinstance(key, str) and PLACEHOLDER.fullmatch(key) and isinstance(value, list)
                        and all(isinstance(v, str) for v in value[:2])}
        return board

    @classmethod
    def checked(cls, state, sai=None, policy=None):
        """The board of STATE when the app can show and use it, else ValueError: for a state that did not come from
        this app's own Undo (a draft in the app's state file, which anyone with the user's rights may have edited, or
        one of another version). Known kinds, settings as objects, places as finite numbers, edges between its parts,
        and what the board computes from it (titles, command line, problems, badges) computed without an error."""
        try:
            board = cls.from_state(state)
            ids = [n.id for n in board.nodes]
            sound = all(type(i) is int for i in ids) and len(set(ids)) == len(ids) and \
                all(n.kind in KINDS and isinstance(n.props, dict) and
                    all(type(v) in (int, float) and abs(v) < 1e9 for v in (n.x, n.y)) for n in board.nodes) and \
                all(len(e) == 2 and e[0] in ids and e[1] in ids for e in board.edges) and \
                all(isinstance(t, str) for t in board.extra) and isinstance(board.document_extra, dict)
            if not sound:
                raise ValueError("not a board of this app")
            for node in board.nodes:
                node.title()
            command_line(sai, board)
            problems(sai, board, policy)
            badges(board, policy)
        except ValueError:
            raise
        except Exception as exc:  # noqa: BLE001 - whatever a foreign state makes fail: not shown
            raise ValueError(f"not a board of this app: {exc!r}") from None
        return board

    @classmethod
    def part_problem(cls, kind, props, sai=None, policy=None):
        """Why a part of KIND with the settings PROPS that came from outside the app cannot be shown and used, or None.
        The clipboard holds what any program put there: a part is pasted only when its settings have the shapes this
        app gives them (a launcher option's flag a yes or no, its list a list of texts, its value a text or number;
        a part's own name, path or choice a text) and a board with it computes what a board shows (its title, the
        command line, the problems, the badges) without an error."""
        try:
            if kind not in KINDS or not isinstance(props, dict) or not all(isinstance(key, str) for key in props):
                return "not a part of this app"
            if kind == "other" and not (isinstance(props.get("tokens", []), list)
                                        and all(isinstance(token, str) for token in props.get("tokens", []))):
                return "tokens: not what this app keeps there"
            options = _options(sai) if sai is not None else {}
            for key, value in props.items():
                option = options.get(key)
                if option is not None and option.kind == "flag":
                    sound = isinstance(value, bool)
                elif option is not None and option.kind in ("list", "multi"):
                    sound = isinstance(value, list) and all(isinstance(v, str) for v in value)
                elif option is not None or key in TEXT_PROPS:
                    sound = isinstance(value, str) or (option is not None and type(value) in (int, float))
                else:
                    sound = True
                if not sound:
                    return f"{key}: not what this app keeps there"
            board = from_argv(sai, "claude", []) if sai is not None else cls()
            node = board.first(kind) if KINDS[kind].single and board.first(kind) is not None else board.add(kind)
            node.props = json.loads(json.dumps(props))
            cls.checked(board.state(), sai, policy)
        except Exception as exc:  # noqa: BLE001 - whatever a foreign part makes fail: not pasted
            return f"{type(exc).__name__}: {exc}"
        return None

    # -- layout --

    # The columns of layout(), by role: what configures the agent, the agent and what configures the proxy,
    # the proxy and what the agent talks to, the network. In each column, top to bottom, in this order.
    FLOW = (("workspace", "mount", "home", "kits", "engine", "devices", "toolchains", "limits", "display", "session",
             "other"),
            ("agent", "approvals", "git", "logs"),
            ("mcp", "llm", "service", "cairn", "artex", "browser", "bridge", "team", "proxy", "vpn"),
            ("network",))

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

    def place_free(self, node, grid=0):
        """NODE (a part put on a board that is laid out already) at a free place of its column: below the parts that
        are there, on no other part."""
        column = next((index for index, kinds in enumerate(self.FLOW) if node.kind in kinds), 0)
        others = [n for n in self.nodes if n is not node]
        x = snap(40.0 + column * 260.0, grid)
        y = max([n.y + 110.0 for n in others if abs(n.x - x) < 130.0], default=40.0)
        while any(abs(n.x - x) < 220.0 and abs(n.y - y) < 90.0 for n in others):
            y += 110.0
        node.x, node.y = x, snap(y, grid)

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

_OPTIONS = weakref.WeakKeyDictionary()  # {sai: {long option: Option}} (a backend that went takes its entry along)


def _options(sai):
    """The launcher's options by long name (cached per installation: the parser is built once)."""
    try:
        cached = _OPTIONS.get(sai)
    except TypeError:  # (a stand-in of the tests that takes no weak reference: not kept)
        return {o.long: o for o in sai.options()}
    if cached is None:
        cached = _OPTIONS[sai] = {o.long: o for o in sai.options()}
    return cached


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
    thinking = str(values.pop("--show-thinking", "") or "")
    board.add("agent", agent=str(agent), model=model, arguments=arguments,
              **({"effort": effort} if effort else {}),
              **({"thinking": thinking} if thinking else {}))  # an unknown agent is a problem
    paths = [w.strip() for value in list(workspaces) + list(values.pop("--workspace", []) or [])
             for w in str(value).split(",") if w.strip()]
    readonly = bool(values.pop("--workspace-ro", False))
    for path in dict.fromkeys(paths):
        board.add("workspace", path=path, readonly=readonly)
    if readonly and not paths:
        board.extra += ["--workspace-ro"]  # (no folder part to say it on: kept, not dropped)
    for value in values.pop("--mount", []) or []:
        source, dest, mode = (str(value).rsplit(":", 2) + ["", ""])[:3] if str(value).count(":") >= 2 else \
            (str(value), "", "ro")
        board.add("mount", source=source, dest=dest, mode=mode)
    for name in values.pop("--mcp", []) or []:
        board.add("mcp", name=name)
    for value in values.pop("--mcp-limits", []) or []:
        name, limits = parse_mcp_limits(value)
        node = next((n for n in board.of_kind("mcp") if n.props.get("name") == name), None) if limits else None
        if node is None:
            board.extra += ["--mcp-limits", str(value)]  # (for a server the board does not use: kept as it was)
        else:
            node.props["limits"] = {**node.props.get("limits", {}), **limits}
    for value in values.pop("--mcp-env", []) or []:
        name, spec = parse_mcp_env(value)
        node = next((n for n in board.of_kind("mcp") if n.props.get("name") == name), None) if spec else None
        if node is None:
            board.extra += ["--mcp-env", str(value)]  # (for a server the board does not use: kept as it was)
        else:
            node.props["variables"] = with_variable(node.props.get("variables"), spec)
    for spec in values.pop("--service", []) or []:
        board.add("service", **service_props(sai, spec))
    # The LLM node's own parameters for this board's starts (--llm-param KEY=VALUE): props "params" {key: text}.
    llm_params, other_params = LP.parse_start(values.pop("--llm-param", []) or [])
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
        if kind.key in ("agent", "workspace", "mount", "mcp", "service"):
            continue
        props = grouped.get(kind.key)
        if props or kind.required:
            board.add(kind.key, **(props or {}))
    board.first("proxy").props["enabled"] = proxy_on
    board.first("agent").props.update(grouped.get("agent", {}))
    llm = board.first("llm")
    if llm is not None and llm_params:
        llm.props["params"] = llm_params
    for value in other_params + ([f"{k}={v}" for k, v in llm_params.items()] if llm is None else []):
        board.extra += ["--llm-param", value]  # (without an LLM server node: kept as it was)
    board.extra += unknown
    board.layout()
    return board


# Options of the agent part only one agent takes (the launcher refuses them for the others).
AGENT_ONLY = {"--hermes-enable": "hermes", "--qwen-enable": "qwen", "--goose-enable": "goose",
              "--local-config": "opencode"}


def task_args(agent, text):
    """AGENT's arguments that give it TEXT as its first task, as sandbox-ai's presets.prompt_args forms them (a start
    with --preset): Claude Code and Codex take it as an argument, OpenCode --prompt, Qwen Code --prompt-interactive=,
    Goose run --interactive --text=, Hermes chat --query=, DSH headless (a text that starts with "-" after a space)."""
    if agent == "opencode":
        return ["--prompt", text]
    if agent == "qwen":
        return ["--prompt-interactive=" + text]
    if agent == "goose":
        return ["run", "--interactive", "--text=" + text]
    if agent == "hermes":
        return ["chat", "--query=" + text]
    if agent == "dsh":
        return ["headless", " " + text if text.startswith("-") else text]
    return [text]


def task_of(agent, args):
    """The first task ARGS (AGENT's arguments, a list) hold in AGENT's form (task_args), else None: only arguments
    that are exactly such a task count, anything else is the human's own."""
    prefixes = {"qwen": ([], "--prompt-interactive="), "goose": (["run", "--interactive"], "--text="),
                "hermes": (["chat"], "--query=")}
    if agent in prefixes:
        lead, option = prefixes[agent]
        if len(args) == len(lead) + 1 and args[:len(lead)] == lead and args[-1].startswith(option):
            return args[-1][len(option):] or None
        return None
    if agent == "opencode":
        return args[1] if len(args) == 2 and args[0] == "--prompt" and args[1] else None
    if agent == "dsh":
        return (args[1][1:] if args[1].startswith(" -") else args[1]) or None \
            if len(args) == 2 and args[0] == "headless" else None
    return args[0] if len(args) == 1 and args[0] and not args[0].startswith("-") else None


def switch_agent(props, agent):
    """The agent part's PROPS changed in place to AGENT: what only the agent before took goes (its own options, an
    effort AGENT does not know, a model alias only the one before has, such as Claude's opus); the rest stays (a
    model typed in, e.g. a server's, and what every agent takes). A first task (a preset's) takes AGENT's form:
    OpenCode's --prompt TEXT is Qwen Code's run-and-end option, a bare text an unknown command of Hermes or Goose."""
    from .guide import AGENT_EFFORTS, AGENT_MODELS
    before = props.get("agent", "claude")
    props["agent"] = agent
    try:
        task = task_of(before, shlex.split(str(props.get("arguments") or ""))) if before != agent else None
    except ValueError:
        task = None
    if task is not None:
        props["arguments"] = shlex.join(task_args(agent, task))
    for option, owner in AGENT_ONLY.items():
        if owner != agent:
            props.pop(option, None)
    if props.get("effort") and props["effort"] not in AGENT_EFFORTS.get(agent, ()):
        props.pop("effort")
    from . import thinking
    thinking.keep(props, agent)  # (Qwen Code has no switch for showing thinking)
    model = str(props.get("model") or "")
    if model and model in AGENT_MODELS.get(before, ()) and model not in AGENT_MODELS.get(agent, ()):
        props["model"] = ""
    from .models import keep_effort
    keep_effort(props, agent)  # (an effort the model kept does not take in AGENT goes too)
    return props


def set_model(props, model, sai=None):
    """The agent part's PROPS changed in place to MODEL (the Board's Model choice): an effort the new model does not
    take goes with it (the table of cloud models: models.efforts). The level removed, or None."""
    from .models import keep_effort
    props["model"] = str(model or "").strip()
    return keep_effort(props, props.get("agent", "claude"), sai)


def from_project(sai, document, directory):
    """The board of a project file (.sandbox-ai.json in DIRECTORY): its main workspace is DIRECTORY."""
    board = from_argv(sai, document.get("agent", ""), document.get("options", []), document.get("agent_args", []),
                      [str(directory)] + [str(w) for w in document.get("workspaces", [])])
    # The file's other keys (mode, canaries, keep, log_debug, name, budget, description) are no part of the board: kept for
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


def parse_mcp_limits(value):
    """(server name, {memory, cpus, pids} as text) of one --mcp-limits NAME:KEY=VALUE[,KEY=VALUE...]; (name, {}) for
    one that is not of that form (the launcher's check says what is wrong with it)."""
    name, colon, rest = str(value).partition(":")
    limits = {}
    for part in rest.split(",") if colon else []:
        key, equals, number = part.strip().partition("=")
        if not equals or key not in LIMIT_KEYS or not number.strip():
            return name, {}
        limits[key] = number.strip()
    return name, limits


MCP_ENV_SPEC = re.compile(r"([A-Za-z_][A-Za-z0-9_]{0,63})(?:=[^\x00-\x1f\x7f]{0,1024}|@[A-Za-z_][A-Za-z0-9_]{0,63})")


def parse_mcp_env(value):
    """(server name, "VAR=VALUE" or "VAR@SECRET") of one --mcp-env NAME:VAR=VALUE|NAME:VAR@SECRET; (name, "") for one
    that is not of that form (the launcher's check says what is wrong with it)."""
    name, colon, spec = str(value).partition(":")
    return name, spec if colon and MCP_ENV_SPEC.fullmatch(spec) else ""


def variable_of(spec):
    """VAR of "VAR=VALUE" or "VAR@SECRET"."""
    return re.split(r"[=@]", str(spec), maxsplit=1)[0]


def with_variable(specs, spec):
    """The MCP node's own variables SPECS (props "variables": ["VAR=VALUE", "VAR@SECRET"]) with SPEC in place of what
    they said of its variable."""
    kept = [s for s in specs or [] if isinstance(s, str) and variable_of(s) != variable_of(spec)]
    return [*kept, spec]


def mcp_env_values(name, specs):
    """--mcp-env's values of server NAME with its own variables SPECS (["VAR=VALUE", "VAR@SECRET"])."""
    return [f"{name}:{spec}" for spec in specs or [] if isinstance(spec, str) and MCP_ENV_SPEC.fullmatch(spec)]


def mcp_limits_value(name, limits):
    """--mcp-limits' value of server NAME with LIMITS ({memory, cpus, pids}; those not set left out), or None."""
    given = [f"{key}={limits[key]}" for key in LIMIT_KEYS if str(limits.get(key, "")).strip()]
    return f"{name}:{','.join(given)}" if given else None

# -- services (--service NAME[:KEY=VALUE,...]): sandbox-ai's services.py reads them; the board keeps the same keys --

SERVICE_DEFAULTS = {"memory": "512m", "cpus": "1", "pids": 256, "ready_timeout": 60}
SERVICE_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,23}")


def default_service(name):
    """A service's properties with every default (as sandbox-ai's services.default_service)."""
    return {"name": name, "image": name, "ports": [], "ready": None, "ready_timeout": SERVICE_DEFAULTS["ready_timeout"],
            "env": [], "secrets": [], "data": [], "keep": False, "user": None, "read_only": True,
            "memory": SERVICE_DEFAULTS["memory"], "cpus": SERVICE_DEFAULTS["cpus"], "pids": SERVICE_DEFAULTS["pids"],
            "devices": []}


def services_module(sai):
    """sandbox-ai's services.py (its parser and checks), or None for a sandbox-ai without services."""
    try:
        module = sai.module("services") if sai is not None else None
    except Exception as _exc:  # noqa: BLE001 - an older or unreadable installation: the board keeps the text as it is
        applog.swallowed("board", "services_module: an older or unreadable installation: the board keeps the te…", _exc)
        return None
    return module if module is not None and hasattr(module, "parse_spec") and hasattr(module, "spec_text") else None


def service_props(sai, spec):
    """The node properties of the --service value SPEC: what sandbox-ai's parser reads, or (when it refuses it, or
    cannot be asked) the name and the text as given ("raw"), so that nothing of it is lost and it is shown as a
    problem."""
    module = services_module(sai)
    name = str(spec).partition(":")[0]
    if module is not None:
        try:
            return dict(module.parse_spec(str(spec)))
        except ValueError as exc:
            return {"name": name, "raw": str(spec), "error": printable_text(str(exc))}
    return {"name": name, "raw": str(spec)}


def service_spec(props):
    """The --service value of a service node: its image always (without image= the launcher takes the name as the
    image, which surprised a user who never named one), then only what differs from the defaults, in a fixed order
    (the same text as sandbox-ai's services.spec_text), or the text as it was given when it could not be read."""
    if props.get("raw"):
        return str(props["raw"])
    name = str(props.get("name", ""))
    items = [f"image={props.get('image') or name}"]
    ports = [int(p) for p in props.get("ports") or [] if str(p).isdigit()]
    items += [f"port={port}" for port in ports]
    ready = props.get("ready")
    if ready not in (None, "") and not (ports and str(ready) == str(ports[0])):
        items.append(f"ready={ready}")
    if str(props.get("ready_timeout", SERVICE_DEFAULTS["ready_timeout"])) != str(SERVICE_DEFAULTS["ready_timeout"]):
        items.append(f"ready-timeout={props['ready_timeout']}")
    for entry in props.get("env") or []:
        items.append(f"env={entry['name']}" + ("" if entry.get("from_host") else f"={entry.get('value', '')}"))
    for entry in props.get("secrets") or []:
        secret = entry.get("secret") or entry["name"]
        items.append(f"secret={entry['name']}" + ("" if secret == entry["name"] else f"={secret}"))
    items += [f"data={path}" for path in props.get("data") or []]
    if props.get("keep"):
        items.append("keep=yes")
    if props.get("user"):
        items.append(f"user={props['user']}")
    if props.get("read_only") is False:
        items.append("read-only=no")
    for key in ("memory", "cpus", "pids"):
        if key in props and str(props[key]) != str(SERVICE_DEFAULTS[key]):
            items.append(f"{key}={props[key]}")
    # device=: a device of the host for the service alone (the emulator's /dev/kvm, an adb server's USB device)
    items += [f"device={device}" for device in props.get("devices") or []]
    return name + (":" + ",".join(items) if items else "")


def template_props(sai, name, image, template):
    """The properties of the service NAME of the image IMAGE with what sandbox-ai's own Containerfile brings for its
    service (TEMPLATE: a row of `images templates` - its "spec": the ports, the device, the limits), or None when it
    brings nothing or this sandbox-ai cannot read it."""
    spec = str((template or {}).get("spec") or "")
    module = services_module(sai)
    if not spec or module is None or not SERVICE_NAME.fullmatch(str(name or "")):
        return None
    try:
        return dict(module.parse_spec(f"{name}:image={image},{spec}"))
    except ValueError:
        return None


def your_images_text(user_images):
    """USER_IMAGES (their names) as a short list, or "none yet"."""
    names = sorted(str(name) for name in user_images or ())
    return ", ".join(printable(name) for name in names[:12]) + (", …" if len(names) > 12 else "") if names \
        else tr("none yet")


def image_problem(name, image, user_images):
    """What is said of the service NAME whose image IMAGE is none of USER_IMAGES (their names): which service, which
    image, the images there are, and where to add one; an image that is its name (--service NAME without image=:
    its name is its image) said so."""
    if image == name:
        return tr("The service {name} has no image of yours: without an image chosen its name is its image, and you "
                  "have no image {image} (your images: {mine}). Choose one of yours in its properties, or add one "
                  "(Images page, Your images).", name=printable(name), image=printable(image or "?"),
                  mine=your_images_text(user_images))
    return tr("The service {name}: {image} is none of your images (your images: {mine}). Choose one of yours in its "
              "properties, or add it (Images page, Your images).", name=printable(name), image=printable(image or "?"),
              mine=your_images_text(user_images))


def service_image_problems(sai, specs, user_images, templates=()):
    """[text] of the --service values SPECS whose image is none of USER_IMAGES (their names; None: not known, nothing
    said) nor of sandbox-ai's own TEMPLATES (built when the start needs one): the Board page's check before a start
    (a value sandbox-ai's parser refuses is the launcher's to say)."""
    if user_images is None:
        return []
    module = services_module(sai)
    found = []
    for spec in specs or []:
        try:
            props = dict(module.parse_spec(str(spec))) if module is not None else None
        except ValueError:
            continue
        if props is None:
            name, _, rest = str(spec).partition(":")
            image = next((item.partition("=")[2] for item in rest.split(",") if item.startswith("image=")), name)
            props = {"name": name, "image": image}
        if props.get("image") not in user_images and props.get("image") not in (templates or ()):
            found.append(image_problem(props.get("name", "?"), props.get("image"), user_images))
    return found


def service_problems(sai, board, user_images=None, templates=()):
    """[Problem] of the service nodes: one that cannot be read, what sandbox-ai's parser refuses, an image that is
    none of yours (USER_IMAGES: their names, when known) nor sandbox-ai's own (TEMPLATES: built when a start needs
    one), two with one name or port, and the backends that have no pod for them. The launcher's dry run has the last
    word (it also refuses them behind Tor)."""
    found, names, ports = [], {}, {}
    nodes = [n for n in board.included() if n.kind == "service"]
    if not nodes:
        return found
    module = services_module(sai)
    _, values_, _ = values(board)

    def add(text, node, level="error"):
        found.append(Problem(level, text, node.id))
    for node in nodes:
        props = node.props
        if props.get("raw") and image_markers([str(props["raw"])]):
            continue  # its image is a preset's value still: "Fill in …" says it (placeholders), read again once filled
        if props.get("raw"):
            add(tr("The service {name} cannot be read: {error}", name=printable(props.get("name") or "?"),
                   error=props.get("error") or tr("this sandbox-ai has no services (sandbox-ai update)")), node)
            continue
        if not SERVICE_NAME.fullmatch(str(props.get("name", ""))):
            add(tr("A service's name is lower-case letters, digits and '-' (up to 24)."), node)
            continue
        if module is not None:
            try:
                module.parse_spec(service_spec(props))
            except ValueError as exc:
                add(printable_text(str(exc)), node)
                continue
        if not str(props.get("image") or "").strip():
            add(tr("Choose the image of the service {name}: one of your images (Images page, Your images).",
                   name=printable(props["name"])), node)
        elif user_images is not None and props.get("image") not in user_images and \
                props.get("image") not in (templates or ()):
            add(image_problem(props["name"], props.get("image"), user_images), node)
        if props["name"] in names:
            add(tr("Two services are named {name}.", name=printable(props["name"])), node)
        names[props["name"]] = node
        for port in props.get("ports") or []:
            if port in ports:
                add(tr("Port {port} is taken by the service {name} already.", port=port, name=printable(ports[port])),
                    node)
            ports.setdefault(port, props["name"])
    engine = board.first("engine") or nodes[0]
    if values_.get("--backend", "podman") != "podman":
        add(tr("Services need the Podman backend: they are containers in the sandbox's pod."), engine)
    if values_.get("--podman-url"):
        add(tr("Services run on this machine's Podman, where your images are pinned: not with --podman-url."), engine)
    return found


def launcher_knows(sai, option):
    """Whether this sandbox-ai's launcher has OPTION (an older one has no --model or --effort)."""
    try:
        return sai is not None and option in _options(sai)
    except Exception as _exc:  # noqa: BLE001 - a launcher that does not load knows nothing
        applog.swallowed("board", "launcher_knows: a launcher that does not load knows nothing", _exc)
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
            elif value not in (None, "") and not backend.retired(option, value):  # an old --telemetry on: off
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
    # An MCP node's own limits (this board's; the server's own are the default): --mcp-limits NAME:memory=...
    limits = [mcp_limits_value(n.props["name"], n.props["limits"]) for n in board.included()
              if n.kind == "mcp" and n.props.get("name") and isinstance(n.props.get("limits"), dict)]
    if any(limits):
        found["--mcp-limits"] = [value for value in limits if value]
    # An MCP node's own variables (this board's; the server's own are the default): --mcp-env NAME:VAR=VALUE|VAR@SECRET
    variables = [value for n in board.included() if n.kind == "mcp" and n.props.get("name")
                 for value in mcp_env_values(n.props["name"], n.props.get("variables"))]
    if variables:
        found["--mcp-env"] = variables
    services = [service_spec(n.props) for n in board.included() if n.kind == "service" and n.props.get("name")]
    if services:
        found["--service"] = services
    # The LLM node's own parameters (this board's; the server's are the default): --llm-param KEY=VALUE
    llm = next((n for n in board.included() if n.kind == "llm" and n.props.get("--llm")), None)
    params = LP.start_argv(llm.props.get("params")) if llm is not None and isinstance(llm.props.get("params"), dict) \
        else []
    if params:
        found["--llm-param"] = params
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
        from . import thinking
        # (kept for an agent without the switch too, as a command line or project file had it: the launcher refuses it
        # and problems() says why - nothing is dropped silently)
        if agent.props.get("thinking") in thinking.VALUES and launcher_knows(sai, thinking.OPTION):
            found[thinking.OPTION] = str(agent.props["thinking"])
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
    return backend.argv_from_values(list(options.values()), found) + backend.without_retired(board.extra)


def document_argv(board):
    """The options of the project file's own keys that no node holds (Board.document_extra): its security mode,
    canaries, kept HOME, debug logs, name and budget. A board start passes --no-config, so without these a paranoid
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
    if "budget" in extra and not isinstance(extra["budget"], (bool, dict, list)) and extra["budget"] is not None:
        argv += ["--budget", str(extra["budget"])]  # (as the launcher takes it from the file: merge_config)
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
        backend.write_private_text(target, data.decode())  # (the bytes it is named by: written once)
    return target


def launcher_argv(sai, board, local=True):
    """What the launcher checks in a dry run and what a board start runs: the board alone (--no-config: a trusted
    project file in the workspace would be merged in otherwise), with the project file's own keys as options
    (document_argv) and, on this machine (LOCAL), its ruleset as --rules (a copy outside every workspace). Nothing
    of the project file is left out silently: a ruleset on another host is refused at the start (board_view)."""
    rules = project_rules(board)
    copy = ["--rules", str(rules_copy(rules))] if rules is not None and local else []
    return document_argv(board) + to_argv(sai, board) + copy + ["--no-config"]


def toolchain_registries(value, found):
    """(hosts, presets) the toolchain VALUE (NAME or NAME@VERSION, as --toolchain takes it) needs opened, as the
    host's store and signed catalog say (FOUND: backend.host_toolchains), with those of the toolchains it requires;
    None when it is in neither (typed, not known there)."""
    choices = backend.toolchain_choices(found or {})
    by_value = {choice["value"]: choice["row"] for choice in choices}
    by_name = {}
    for choice in choices:  # (the store's newest first: what NAME alone starts)
        by_name.setdefault(choice["name"], choice["row"])
    row = by_value.get(str(value)) or by_name.get(str(value).partition("@")[0])
    if row is None:
        return None
    hosts, presets, seen = [], [], set()

    def take(entry):
        if entry["name"] in seen:
            return
        seen.add(entry["name"])
        for name in entry.get("requires") or []:
            if name in by_name:
                take(by_name[name])
        hosts.extend(host for host in entry.get("hosts") or [] if isinstance(host, str) and host not in hosts)
        presets.extend(p for p in entry.get("allow") or [] if isinstance(p, str) and p not in presets)
    take(row)
    return hosts, presets


def security_mode(board, plan=None):
    """The security mode of BOARD's start: as the launcher's PLAN of it says (its dry run: the option, the project
    file's, your default), else the board's own option (--mode among Other options), else None (not known yet)."""
    mode = (plan or {}).get("mode")
    if isinstance(mode, dict) and isinstance(mode.get("name"), str):
        return mode["name"]
    other = board.first("other")
    tokens = [str(token) for token in (other.props.get("tokens") or [])] if other is not None else []
    found = None
    for index, token in enumerate(tokens):
        if token in ("--mode", "-mo") and index + 1 < len(tokens):
            found = tokens[index + 1]
        elif token.startswith("--mode="):
            found = token.partition("=")[2]
    return found


def toolchain_needs(board, found, plan=None):
    """What the board's toolchains need of the network, and what the start does about it:
    [{"toolchain", "needs": [host or "--allow-PRESET"], "state", "why"}] - STATE "opened" (set by the start itself),
    "closed" (the part says so: --toolchain-offline, or the network is offline), "refused" (WHY: "mode high", the
    administrator's policy), "none" (it needs nothing), "unknown" (not in the store or catalog of FOUND). PLAN: the
    launcher's dry run of this very board (its "mode", "toolchain_hosts"): the truth where it is there; before it
    the board's own options decide (--mode high among Other options)."""
    _, values_, _ = values(board)
    offline = bool(values_.get("--toolchain-offline") or values_.get("--offline"))
    mode = security_mode(board, plan)
    closed = set(((plan or {}).get("toolchain_hosts") or {}).get("closed") or []) \
        if isinstance((plan or {}).get("toolchain_hosts"), dict) else None
    return needs_rows(values_.get("--toolchain", []), found, offline, mode, closed)


def needs_rows(toolchains, found, offline=False, mode=None, closed=None):
    """toolchain_needs' rows for the --toolchain values TOOLCHAINS: OFFLINE (their registries are kept closed), MODE
    (the security mode: high and paranoid refuse them), CLOSED (what the launcher's plan left closed, or None)."""
    rows = []
    for value in toolchains:
        needs = toolchain_registries(value, found)
        if needs is None:
            rows.append({"toolchain": str(value), "needs": [], "state": "unknown", "why": ""})
            continue
        wanted = [*needs[0], *(f"--allow-{preset}" for preset in needs[1])]
        if not wanted:
            state, why = "none", ""
        elif offline:
            state, why = "closed", "offline"
        elif mode in ("high", "paranoid"):
            state, why = "refused", f"mode {mode}"
        elif closed is not None and any(item in closed for item in wanted):
            state, why = "refused", "policy"
        else:
            state, why = "opened", ""
        rows.append({"toolchain": str(value), "needs": wanted, "state": state, "why": why})
    return rows


# The sources of what a start may reach (agent_sandbox.reach, in the launcher's dry-run plan as "reach") that are
# another part of the board - not the Network part's own fields, the agent's provider, a profile, the project file or
# an approval -, and the kind of part that opens each: what the Network part lists, read-only.
REACH_PARTS = {"toolchain": "toolchains", "git-login": "git", "api-key": "agent", "publish": "agent", "llm": "llm",
               "feature": "agent"}


def opened_by_parts(board, plan):
    """What the other parts of BOARD open, as the launcher's dry run of this very board says it (PLAN): {"opened":
    [the plan's "reach" entries whose source is another part (REACH_PARTS)], "closed": [what the toolchains need and
    the start keeps closed: the plan's "toolchain_hosts"."closed"], "why": why they stay closed - "mode high", "mode
    paranoid" (refused), "offline" (the board says so: the Toolchains part's check box, or no network at all), "policy"
    (the administrator's), "" (nothing closed)}. None without a plan that says what the start may reach (no check
    yet, a refused board, a sandbox-ai before 0.1.245). Nothing is worked out here a second time: what is open is
    reach()'s, what is closed the plan's own list; only the reason is put into words."""
    reach = plan.get("reach") if isinstance(plan, dict) else None
    if not isinstance(reach, list):
        return None
    opened = [entry for entry in reach if isinstance(entry, dict) and entry.get("source") in REACH_PARTS]
    hosts = plan.get("toolchain_hosts") if isinstance(plan.get("toolchain_hosts"), dict) else {}
    closed = [str(item) for item in hosts.get("closed") or [] if isinstance(item, str)]
    why = ""
    if closed:
        _, values_, _ = values(board)
        mode = security_mode(board, plan)
        why = f"mode {mode}" if mode in ("high", "paranoid") else \
            "offline" if values_.get("--toolchain-offline") or values_.get("--offline") or plan.get("offline") else \
            "policy"
    return {"opened": opened, "closed": closed, "why": why}


def registry_shown(value):
    """A registry as the Board names it: a host as it is, a preset of the launcher (--allow-github) by its name."""
    return {"--allow-pypi": "PyPI", "--allow-github": "GitHub", "--allow-gitlab": "GitLab", "--allow-npm": "npm",
            "--allow-kernel-org": "kernel.org", "--allow-duckduckgo": "DuckDuckGo"}.get(value, value)


def toolchain_needs_lines(rows):
    """The lines the Board shows for ROWS (toolchain_needs): one per toolchain, what it needs and what happens."""
    lines = []
    for row in rows:
        needs = ", ".join(registry_shown(item) for item in row["needs"])
        if row["state"] == "unknown":
            lines.append(tr("{toolchain}: not in the store or the signed catalog of this host - what it needs is "
                            "not known", toolchain=printable(row["toolchain"])))
        elif row["state"] == "none":
            lines.append(tr("{toolchain}: needs no registry", toolchain=printable(row["toolchain"])))
        elif row["state"] == "opened":
            lines.append(tr("{toolchain}: opened at the start: {needs}", toolchain=printable(row["toolchain"]),
                            needs=printable(needs)))
        elif row["state"] == "closed":
            lines.append(tr("{toolchain}: needs {needs} - kept closed (you chose so)",
                            toolchain=printable(row["toolchain"]), needs=printable(needs)))
        elif row["why"] == "policy":
            lines.append(tr("{toolchain}: needs {needs} - refused by the administrator's policy",
                            toolchain=printable(row["toolchain"]), needs=printable(needs)))
        else:
            lines.append(tr("{toolchain}: needs {needs} - refused by {why}", toolchain=printable(row["toolchain"]),
                            needs=printable(needs), why=printable(row["why"])))
    return lines


def check_argv(sai, board, local=True):
    """launcher_argv for a dry run: without the MCP servers the project itself defines (project_servers). The
    launcher knows them only from the trusted project file, which a board start with them uses (Apply writes it);
    with --no-config the dry run refused them as unknown although the start finds them. The board checks them."""
    own, argv, result, index = set(project_servers(board)), launcher_argv(sai, board, local), [], 0
    while index < len(argv):
        item = argv[index]
        if item in ("--mcp", "--mcp-limits", "--mcp-env") and index + 1 < len(argv) and \
                (argv[index + 1] if item == "--mcp" else argv[index + 1].partition(":")[0]) in own:
            index += 2
            continue
        if item.startswith(("--mcp=", "--mcp-limits=", "--mcp-env=")) and \
                item.partition("=")[2].partition(":")[0] in own:
            index += 1
            continue
        result.append(item)
        index += 1
    return result


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
    # (and the values a project file may not hold, which project_document leaves out: --llm-traffic all)
    kept = {name: value for name, value in found.items() if name in forbidden or backend.project_refused(name, value)}
    argv = backend.argv_from_values(list(_options(sai).values()), kept)
    refused, extra = _refused_extra(board.extra)
    argv += [t for group in extra_groups(sai, extra) if group[0].split("=", 1)[0] in forbidden for t in group]
    return argv + refused + (["--workspace", workspaces[0]] if workspaces else [])


# Options of Board.extra a project file may not hold that are one of its keys: Apply writes them as that key (the
# board's --mode high is the file's "mode": "high"), so the file means what the board means.
EXTRA_KEYS = {"--mode": "mode", "--project-name": "name"}


def extra_groups(sai, tokens):
    """TOKENS (Board.extra) as groups: an option the launcher knows with its value (--X=V, or --X V), a flag, a token
    of its own. What takes a project file's options apart or keeps some of them never cuts an option from its value."""
    try:  # (every option of the parser, the hidden ones too: --session)
        actions = sai.launcher.option_actions()
    except Exception as _exc:  # noqa: BLE001 - an older launcher: the options the app offers
        applog.swallowed("board", "extra_groups: an older launcher: the options the app offers", _exc)
        actions = {name: argparse.Namespace(nargs=0 if o.kind == "flag" else None, const=o.const)
                   for name, o in (_options(sai) if sai is not None else {}).items()}
    tokens, groups, index = [str(t) for t in tokens], [], 0
    while index < len(tokens):
        name, equals, _ = tokens[index].partition("=")
        action = actions.get(name) if tokens[index].startswith("--") else None
        takes = action is not None and action.nargs != 0 and not equals and index + 1 < len(tokens)
        if takes and action.nargs == "?" and tokens[index + 1].startswith("-"):
            takes = False  # (an optional value not given: --browser alone)
        groups.append(tokens[index:index + (2 if takes else 1)])
        index += 2 if takes else 1
    return groups


def _refused_extra(tokens):
    """([tokens of the values a project file may not hold], [the other tokens]) of a board's extra TOKENS: "--llm-traffic
    all" and "--llm-traffic=all" (backend.PROJECT_REFUSED_VALUES) apart from the rest; the gone ones (--telemetry on)
    in neither."""
    refused, rest, index = [], [], 0
    tokens = backend.without_retired(tokens)
    while index < len(tokens):
        token = tokens[index]
        name, equals, value = token.partition("=")
        if equals and backend.project_refused(name, value):
            refused.append(token)
        elif not equals and index + 1 < len(tokens) and backend.project_refused(token, tokens[index + 1]):
            refused += tokens[index:index + 2]
            index += 1
        else:
            rest.append(token)
        index += 1
    return refused, rest


def command_line(sai, board):
    team = team_values(sai, board) if team_node(board) is not None else None
    if team is not None and sai is not None:
        try:
            return shlex.join(["sandbox-ai", *sai.team_start_args("local", **team)])
        except (ValueError, AttributeError):
            pass  # (what is missing is a problem of the board: the start of one sandbox is shown meanwhile)
    agent, argv, agent_args = start_argv(sai, board)
    return shlex.join(["sandbox-ai", agent, *argv, *(["--", *agent_args] if agent_args else [])])


# -- one start, two views: the board and its command line are the same start --

def start_argv(sai, board):
    """(agent, launcher options, the agent's arguments) of the board's start, as its command line gives them: the
    project file's own keys as options first (document_argv), then the board's (to_argv); debug logs the file and
    the part Logs both ask for are named once. What parse_command reads and with_start puts back onto the board."""
    agent, _, agent_args = values(board, sai)
    own, options = document_argv(board), to_argv(sai, board)
    if "--log-debug" in own and "--log-debug" in options:
        own = [token for token in own if token != "--log-debug"]
    return agent, own + options, agent_args


def start_values(sai, board):
    """(agent, {long option: value}, the agent's arguments) of the board's start: what the Expert view's fields show -
    every option of start_argv, read as the launcher's parser reads it."""
    agent, argv, agent_args = start_argv(sai, board)
    found, _ = backend.values_from_argv(list(_options(sai).values()), argv, backend.canonical_of(sai))
    return agent, found, agent_args


def parse_command(sai, text, known=()):
    """(host or None, agent, launcher options, the agent's arguments) of the command line TEXT of one start, as a
    shell reads it: [sandbox-ai] [hosts run HOST [--]] AGENT [options] [-- the agent's arguments]. ValueError says why
    a text is none (translated: shown below the field): a quotation mark not closed, no agent, an option the launcher
    does not have, an option without its value, a word that belongs to no option. Nothing of such a text is used.
    KNOWN: tokens that may stay although the parser does not know them (what the board holds of them already, of a
    project file of another version)."""
    try:
        words = shlex.split(str(text).replace("\\\n", " "))
    except ValueError as exc:
        why = {"No closing quotation": tr("a quotation mark is not closed"),
               "No escaped character": tr("a backslash at the end escapes nothing")}.get(str(exc), str(exc))
        raise ValueError(tr("The command line cannot be read: {why}", why=why)) from None
    if words and os.path.basename(words[0]) == "sandbox-ai":
        words = words[1:]
    host = None
    if words[:2] == ["hosts", "run"]:
        if len(words) < 3 or words[2] == "--" or words[2].startswith("-"):
            raise ValueError(tr("hosts run needs the name of the host it starts on: sandbox-ai hosts run HOST -- "
                                "AGENT [options]"))
        host, words = words[2], words[3:]
        if words[:1] == ["--"]:
            words = words[1:]
    agents = list(getattr(sai, "agents", None) or AGENTS)
    form = tr("sandbox-ai AGENT [options] [-- the agent's arguments]")
    if not words:
        raise ValueError(tr("Name the agent first: {form} (agents: {agents})", form=form, agents=", ".join(agents)))
    agent, rest = words[0], words[1:]
    if agent not in agents:
        raise ValueError(tr("{word} is not an agent of sandbox-ai: the line of a start is {form} (agents: {agents})",
                            word=printable(agent), form=form, agents=", ".join(agents)))
    cut = rest.index("--") if "--" in rest else len(rest)
    argv, agent_args = rest[:cut], rest[cut + 1:]
    options = _options(sai)
    _, unknown = backend.values_from_argv(list(options.values()), argv, backend.canonical_of(sai))
    try:  # (every option of the parser, the hidden ones too: --session)
        actions = set(sai.launcher.option_actions())
    except Exception as _exc:  # noqa: BLE001 - an older launcher: the options the app offers
        applog.swallowed("board", "parse_command: an older launcher: the options the app offers", _exc)
        actions = set(options)
    allowed = [str(token) for token in known]
    for index, token in enumerate(unknown):
        name = token.partition("=")[0]
        if name in options:  # (values_from_argv leaves an option without its value among the unknown)
            raise ValueError(tr("{option} needs a value", option=name))
        if name in actions or (index and unknown[index - 1] in actions and unknown[index - 1] not in options):
            continue  # an option of the launcher the app has no field for, and its value: kept as it is
        if token in allowed:
            allowed.remove(token)
            continue
        if token.startswith("-"):
            raise ValueError(tr("sandbox-ai has no option {option}", option=printable(name)))
        raise ValueError(tr("{word} belongs to no option (the agent's own arguments come after --)",
                            word=printable(token)))
    return host, agent, argv, agent_args


DOCUMENT_OPTIONS = {"--mode": "mode", "--canaries": "canaries", "--no-canaries": "canaries", "--keep": "keep",
                    "--no-keep": "keep", "--project-name": "name", "--budget": "budget"}


def _document_of(sai, board, argv):
    """(the project file's own keys, the other options) of the options ARGV put onto BOARD: an option that is one
    of the keys the board's file has already (its mode, canaries, kept HOME, name, budget, debug logs: document_argv)
    is that key again, changed or - no longer in ARGV - gone; any other option stays an option (Other options)."""
    before = json.loads(json.dumps(board.document_extra))
    held = {key for key in ("mode", "canaries", "keep", "name", "budget") if key in before
            and document_argv(_only(board, key))}
    tokens = list(backend.canonical_of(sai)(list(argv))) if sai is not None else list(argv)
    found, rest = {}, []
    for group in extra_groups(sai, tokens):
        name, equals, attached = group[0].partition("=")
        key = DOCUMENT_OPTIONS.get(name)
        value = attached if equals else (group[1] if len(group) == 2 else None)
        if key not in held or (name in ("--mode", "--canaries", "--project-name", "--budget") and value is None):
            rest += group
        elif name == "--canaries":
            found[key] = [item for item in value.split(",") if item]
        elif name in ("--no-canaries", "--keep", "--no-keep"):
            found[key] = [] if name == "--no-canaries" else name == "--keep"
        else:
            found[key] = value
    logs = board.first("logs")
    if before.get("log_debug") is True:
        if "--log-debug" not in rest:
            before.pop("log_debug")
        elif not (logs is not None and logs.props.get("--log-debug")):
            rest.remove("--log-debug")  # (the file's own: the part Logs does not ask for them itself)
    after = {key: value for key, value in before.items() if key not in held}
    for key, value in found.items():  # (said the same: kept as the file has it - a budget 5, not "5")
        same = document_argv(_only(board, key)) == document_argv(_only(board, key, value))
        after[key] = before[key] if same else value
    return {key: after[key] for key in [*before, *after] if key in after}, rest


def _only(board, key, *value):
    """A board with nothing but the project file's key KEY (BOARD's, or VALUE): what document_argv makes of it."""
    probe = Board()
    probe.document_extra = {key: value[0] if value else board.document_extra[key]}
    return probe


def with_start(sai, board, agent, argv, agent_args=()):
    """A new board: BOARD with the start AGENT ARGV -- AGENT_ARGS put onto it (what parse_command read, or the Expert
    view's fields). The start is exactly that line afterwards (start_argv); the board stays the one it was wherever
    the line says nothing else - every part that is there keeps its number, its place, its connections and what only
    the board holds of it (a project's own MCP definition and ruleset, bridges and the team, placeholders' labels,
    parts that are not connected). A part the line needs and the board has not is put at a free place of its column;
    a folder, mount, MCP server or service the line no longer names goes (a server the project itself defines stays,
    not connected); any other part the line no longer sets stays, with nothing set. The agent's arguments are kept as
    they are given (a --model among them is not taken for the board's model: it may be typed letter by letter).
    The same line as the board's own changes nothing."""
    agent_args = [str(word) for word in agent_args]
    if (str(agent), list(argv), agent_args) == start_argv(sai, board):
        return Board.from_state(board.state())
    own, rest = _document_of(sai, board, argv)
    fresh = from_argv(sai, agent, rest)
    fresh.first("agent").props["arguments"] = shlex.join(agent_args)
    result = Board.from_state(board.state())
    result.document_extra = own
    lead = result.first("agent")

    def put(node, source):
        # What the line says replaces the part's options and its own settings of the line (LINE_PROPS); what only the
        # board holds stays - and an option the part has open without a value yet (a session to take the login from,
        # chosen and not named: no part of any line).
        derived = LINE_PROPS.get(node.kind, ())
        kept = node.props if node.kind != "service" else {}
        node.props = {**{key: value for key, value in kept.items()
                         if (not prop_set(value) if key.startswith("--") else key not in derived)},
                      **json.loads(json.dumps(source.props))}

    def join(node):
        if lead is not None and not result.connected(node):
            edge = (node.id, lead.id) if (node.kind, "agent") in EDGES else (lead.id, node.id)
            result.edges.add(edge)

    def own_server(node):
        return node.kind == "mcp" and node.props.get("project") and isinstance(node.props.get("definition"), dict)

    order = {}
    for kind in LINE_KINDS:
        wanted = fresh.of_kind(kind)
        there = sorted(result.of_kind(kind), key=lambda n: not result.connected(n))  # (a connected one first)
        was = {node.id for node in there if result.connected(node)}
        pairs, left = [], []
        for node in wanted:  # the same folder or server: the same part
            match = next((old for old in there if old.key() == node.key()), None)
            if match is None:
                left.append(node)
            else:
                there.remove(match)
                pairs.append((node, match))
        spare = [old for old in there if old.id in was and not own_server(old)]
        for node in left:  # another folder where one was (a path edited): its part, with what the line says alone
            match = spare.pop(0) if spare else None
            if match is not None:
                there.remove(match)
                match.props = {}
            pairs.append((node, match))
        made = {}
        for node, match in pairs:
            if match is None:
                match = result.add(kind)
                result.place_free(match)
            put(match, node)
            join(match)
            made[id(node)] = match
        for old in there:  # no longer named
            if old.id not in was:
                continue
            if own_server(old):
                result.edges = {edge for edge in result.edges if old.id not in edge}
            else:
                result.remove(old.id)
        order[kind] = [made[id(node)] for node in wanted]
    for kind in KINDS.values():
        if kind.key in LINE_KINDS or kind.key in BUNDLE_APP_KINDS:
            continue
        source, node = fresh.first(kind.key), result.first(kind.key)
        if source is None and node is None:
            continue
        if node is None:
            node = result.add(kind.key)
            result.place_free(node)
        put(node, source if source is not None else Node(0, kind.key, _unset(node)))
    # The parts a line names one by one, in the line's order (the first folder is /work): each in a place of the list
    # one of them had.
    for kind, nodes in order.items():
        slots = sorted(result.nodes.index(node) for node in nodes)
        for slot, node in zip(slots, nodes):
            result.nodes[slot] = node
    if start_argv(sai, result) == start_argv(sai, board):
        return Board.from_state(board.state())  # (the same start, said another way: nothing changes)
    return result


def _unset(node):
    """The settings of a part the line says nothing of: its switch as it is by default (the proxy on), no tokens."""
    return {"enabled": True} if node.kind == "proxy" else {"tokens": []} if node.kind == "other" else {}


def project_document(sai, board):
    """(.sandbox-ai.json document, main workspace) of the board: the first workspace is where the file lives,
    further ones are listed; options a project file may not set are left out (and kept in nothing: the
    window shows them as problems first) - those that are a key of the file (EXTRA_KEYS: --mode, --project-name) are
    written as that key -, and so are values it may not hold (--llm-traffic all: the
    launcher refuses the file with them; a start from the file gives them on its command line, file_start_argv)."""
    agent, found, agent_args = values(board, sai)
    workspaces = list(found.get("--workspace", []))
    forbidden = set(sai.launcher.CONFIG_FORBIDDEN)
    kept = {name: value for name, value in found.items()
            if name not in forbidden and not backend.project_refused(name, value)}
    options = _options(sai)
    argv = backend.argv_from_values(list(options.values()), kept)
    moved = {}
    for group in extra_groups(sai, _refused_extra(board.extra)[1]):
        name, equals, attached = group[0].partition("=")
        if name not in forbidden:
            argv += group
        elif name in EXTRA_KEYS and (equals or len(group) == 2):
            moved[EXTRA_KEYS[name]] = attached if equals else group[1]  # (the last one wins, as on the command line)
    known = set(getattr(sai.launcher, "CONFIG_KEYS", ()))
    document = {"version": 1, "agent": agent, "options": argv,
                **{k: v for k, v in {**board.document_extra, **moved}.items() if k in known}}
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


IMAGE_MARKER = re.compile(r"(?:^|[:,])image=(<(?:text|image):[a-z0-9][a-z0-9-]{0,31}>)")


def image_markers(texts):
    """The placeholders (<kind:name>) that stand as a service's image (image=<text:postgres> in a --service value) in
    TEXTS (strings, lists and objects walked): what is filled in there is the name of one of your images."""
    return {match for text in _strings(list(texts) if not isinstance(texts, (str, dict)) else texts)
            for match in IMAGE_MARKER.findall(text)}


def board_image_markers(board):
    """image_markers of BOARD's service parts (a part whose --service value holds a placeholder keeps it as given)."""
    return image_markers([str(node.props.get("raw") or "") for node in board.of_kind("service")])


def placeholder_label(board, marker):
    """The preset's label for MARKER, else its name (<text:question>: question)."""
    label = (board.labels.get(marker) or [""])[0]
    return label or PLACEHOLDER.fullmatch(marker)[2].replace("-", " ")


def placeholder_text(board, marker, node_id=None):
    """What is still to be filled in for MARKER, in words: the preset's own label for it ("What to find out ..."),
    the part it is in and where to get it - not only the bare <kind:name> and "a value"."""
    match = PLACEHOLDER.fullmatch(marker)
    kind, name = match[1], match[2]
    label, hint = placeholder_label(board, marker), (list(board.labels.get(marker) or []) + ["", ""])[1]
    node = board.node(node_id) if node_id is not None else None
    where = node.title() if node is not None else tr("the start's options")
    if node is not None and node.kind == "agent" and \
            marker in str(node.props.get("arguments", "")):
        where = tr("the agent's task (Agent arguments)")
    text = tr("Fill in \u201c{label}\u201d in {where}: {what}.", label=label, where=where,
              what=tr(PLACEHOLDER_KINDS[kind]))
    if hint:
        text += " " + hint
    return printable(text) + f" ({marker})"


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


def fill(board, values, sai=None):
    """Put VALUES ({placeholder: value}) in place of the placeholders (empty values leave them). SAI: a service read
    while it held a placeholder (refused then: "raw") is read again once none is left in it (its image a value of a
    preset, database-services)."""
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
        if node.kind == "service" and sai is not None and isinstance(node.props.get("raw"), str) and \
                not PLACEHOLDER.search(node.props["raw"]):
            node.props = service_props(sai, node.props["raw"])
    board.extra = [put(str(t)) for t in board.extra]
    return board


def drop(board, markers):
    """Leave out what holds one of MARKERS (optional placeholders not filled in): an MCP server, mount,
    workspace or service, an option value (of a list: that entry; a part left with no option goes too), a token of
    Board.extra with the option before it."""
    markers = set(markers)
    if not markers:
        return board

    def holds(value):
        return any(m in text for text in _strings(value) for m in markers)
    for node in list(board.nodes):
        # (a service: its --service value as given - one of its keys holds the placeholder: its image, or the device
        # the preset gives it, android-development's emulator and adb server - goes whole, as the launcher leaves the
        # option out; before, the part stayed with its name alone and started an image of that name)
        defining = {"mcp": "name", "mount": "source", "workspace": "path", "service": "raw"}.get(node.kind)
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

def _domain(value):
    """The domain --allow-domain VALUE names, as sandbox-ai reads it (its domain_of, without the checks the start
    makes): lower case, without one leading "*." or "." and one trailing "."."""
    name = str(value).strip().lower()
    name = name[2:] if name.startswith("*.") else name[1:] if name.startswith(".") else name
    return name[:-1] if name.endswith(".") else name


def _hosts_of(values_):
    """The --allow-host names, and what each --allow-domain DOMAIN stands for: DOMAIN and *.DOMAIN."""
    hosts = [str(h) for h in values_.get("--allow-host", [])]
    hosts += [str(h).lower() for h in values_.get("--allow-shared-wildcard", [])]
    for value in values_.get("--allow-domain", []):
        hosts += [_domain(value), "*." + _domain(value)]
    return list(dict.fromkeys(hosts))


def _host_matches(host, pattern):
    host, pattern = host.lower(), pattern.lower()
    if pattern.startswith("*."):
        return host == pattern[2:] or host.endswith(pattern[1:])
    return host == pattern


def workspace_overlaps(board):
    """[Problem] of the included workspaces that are one folder or one inside another, in the launcher's words
    (validate: "Workspaces must not overlap"): the same folder added twice, or a folder and one below it. By their
    paths as written (normalized, ~ kept as it is: the board may be another host's, whose HOME is not this one's; links
    are the launcher's dry run's): nothing is read from the disk."""
    found, seen = [], []
    for node in board.included():
        path = str(node.props.get("path", "")).strip() if node.kind == "workspace" else ""
        if not path or remote_source.is_remote(path) or PLACEHOLDER.search(path) or \
                not os.path.isabs(os.path.expanduser(path)):
            continue
        here = PurePosixPath(posixpath.normpath(path))
        for other in seen:
            if here == other or other in here.parents or here in other.parents:
                found.append(Problem("error", tr("Workspaces must not overlap: {first} and {second}",
                                                 first=printable(str(other)), second=printable(str(here))), node.id))
                break
        seen.append(here)
    return found


MOUNT_BASES = ("/work", "/mnt", "/opt")  # where a mount may go (agent_sandbox.safe_dest)


def mount_dest_problem(dest):
    """What the launcher refuses of the mount destination DEST (its safe_dest: an absolute path written plainly, at or
    below /work, /mnt or /opt - not /mntx, not /opt/../etc), in words, or None."""
    dest = str(dest or "")
    path = PurePosixPath(dest)
    if not dest.strip():
        return tr("A mount goes under /work, /mnt or /opt.")
    if not path.is_absolute() or ".." in path.parts or str(path) != dest:
        return tr("The mount destination {dest} is not a plain absolute path (no .., no // and no / at the end).",
                  dest=printable(dest) or "''")
    if not any(path == PurePosixPath(base) or PurePosixPath(base) in path.parents for base in MOUNT_BASES):
        return tr("A mount goes under /work, /mnt or /opt.")
    return None


def problems(sai, board, policy=None, user_images=None, session_logins=None, templates=()):
    """What the launcher or the administrator's POLICY would refuse ("error"), and what has no effect or
    contradicts itself ("warning"). The launcher's dry run has the last word; this is what can be said at
    once, node by node. USER_IMAGES: the names of your images (sandbox-ai images list), when known; TEMPLATES: the
    images of sandbox-ai's own Containerfiles there (images templates), built when a start needs one.
    SESSION_LOGINS: the logins of the board's host's sessions (sandbox-ai login --status --sessions), when known."""
    from . import thinking
    found = []
    agent_node = board.first("agent")
    agent, values_, _ = values(board)
    proxy, network, git = board.first("proxy"), board.first("network"), board.first("git")

    def add(level, text, node=None):
        found.append(Problem(level, text, node.id if node is not None else None))
    for marker, node_id in placeholders(board):
        found.append(Problem("error", placeholder_text(board, marker, node_id), node_id))
    known = tuple(getattr(sai, "agents", AGENTS))
    if agent not in known:
        add("error", tr("Unknown agent {agent}: this sandbox-ai has {list}.", agent=printable(agent) or "?",
                        list=", ".join(known)), agent_node)
    if agent_node is not None and agent in known and agent_node.props.get("thinking") in thinking.VALUES \
            and not thinking.available(agent):
        # (a command line or project file said it: kept, not dropped; the launcher refuses the start with it)
        add("warning", tr("--show-thinking {value} is set, but {agent} has no such switch and the launcher refuses a "
                          "start with it: {reason}. Set Show thinking back to the agent's default.",
                          value=agent_node.props["thinking"], agent=agent, reason=thinking.reason(agent)), agent_node)
    if agent_node is not None and agent in known and not values_.get("--llm"):
        # The model and its effort, by the table of cloud models (models.py): an effort a listed model does not take
        # is the launcher's own warning, in its words; a model the table does not list is passed on as it is.
        from . import models as M
        model = str(agent_node.props.get("model") or "").strip()
        refused = M.effort_problem(agent, model, agent_node.props.get("effort"), sai)
        if refused:
            add("warning", refused, agent_node)
        unknown = M.unknown_note(agent, model, sai)
        if unknown:
            add("warning", unknown, agent_node)
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
        elif remote_source.is_remote(path):
            # a folder of another machine (sshfs://...): its source checked as sandbox-ai checks it
            try:
                remote_source.parse(path)
            except ValueError as exc:
                add("error", tr("The workspace {path}: {problem}", path=printable(path), problem=str(exc)), node)
        elif not os.path.isabs(os.path.expanduser(path)) and not PLACEHOLDER.search(path):
            # (relative to what? the launcher refuses one in a project file; a start resolved it twice)
            add("error", tr("The workspace {path} is a relative path: give the folder in full (/… or ~/…).",
                            path=printable(path)), node)
    found += workspace_overlaps(board)
    workspaces = board.of_kind("workspace")
    if len({bool(n.props.get("readonly")) for n in workspaces}) > 1:
        add("warning", tr("Read-only applies to every workspace (--workspace-ro), not to one."), workspaces[0])
    for node in board.of_kind("mount"):
        if remote_source.is_remote(str(node.props.get("source", ""))):
            try:
                remote_source.parse(str(node.props["source"]))
            except ValueError as exc:
                add("error", tr("The mount {path}: {problem}", path=printable(node.props["source"]),
                                problem=str(exc)), node)
        if not str(node.props.get("source", "")).strip():
            # (values() leaves a mount without a source out: shown on the board, it would be missing from the start)
            add("error", tr("A mount needs a folder or file."), node)
        if node.props.get("mode") not in ("ro", "rw"):
            add("error", tr("A mount is read-only (ro) or writable (rw)."), node)
        dest_problem = mount_dest_problem(node.props.get("dest", ""))
        if dest_problem:
            add("error", dest_problem, node)
        elif any(c in str(node.props.get("source", "")) + str(node.props.get("dest", "")) for c in ",\n\r\0"):
            add("error", tr("Mount paths cannot contain commas or control characters."), node)
    offline = values_.get("--offline") or "offline" in values_.get("--profile", [])
    widening = [o for o in ("--allow", "--allow-host", "--allow-domain", "--allow-shared-wildcard", "--allow-tcp",
                            "--allow-host-port",
                            "--frontier", "--dns",
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
        # Plain http only to a Git server on this machine's loopback (http://localhost:3000, a local Gitea): the
        # launcher's own rule (token_proxy_target), asked of it; an older sandbox-ai without it has the last word.
        for option in tokens:
            for value in values_.get(option, []):
                if str(value).lower().startswith("http://"):
                    found += _checked(lambda value=str(value), kind=option.rsplit("-", 1)[1]:
                                      sai.launcher.token_proxy_target(value, kind), git)
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
    if values_.get("--hermes-enable") and agent != "hermes":
        add("error", tr("--hermes-enable is for Hermes only."), agent_node)
    if values_.get("--qwen-enable") and agent != "qwen":
        add("error", tr("--qwen-enable is for Qwen Code only."), agent_node)
    if values_.get("--goose-enable") and agent != "goose":
        add("error", tr("--goose-enable is for Goose only."), agent_node)
    if values_.get("--openhands-enable") and agent != "openhands":
        add("error", tr("--openhands-enable is for OpenHands only."), agent_node)
    if values_.get("--nanobot-enable") and agent != "nanobot":
        add("error", tr("--nanobot-enable is for nanobot only."), agent_node)
    if values_.get("--letta-enable") and agent != "letta":
        add("error", tr("--letta-enable is for Letta Code only."), agent_node)
    if values_.get("--opencode2-enable") and agent != "opencode2":
        add("error", tr("--opencode2-enable is for OpenCode 2 only."), agent_node)
    if values_.get("--pi-enable") and agent != "pi":
        add("error", tr("--pi-enable is for Pi only."), agent_node)
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
        prefix, colon, bare = str(node.props.get("name") or "").partition(":")
        if colon and not node.props.get("project") and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}", bare):
            # (the launcher takes a server's name, never its catalog: hub:radare2 was refused as unknown - and its
            # start's preparation added radare2, found "hub:radare2" still missing, and added it again)
            add("error", tr("The MCP server {given}: --mcp takes the name of a server (\u201c{name}\u201d); its "
                            "catalog ({prefix}) is chosen when it is added - give the name alone.",
                            given=printable(node.props["name"]), name=printable(bare), prefix=printable(prefix)),
                node)
        if node.props.get("project") and isinstance(definition, dict) and node.props.get("name"):
            found += _checked(lambda: sai.module("mcp").validated({node.props["name"]: definition},
                                                                   tr("MCP server")), node)
        found += mcp_limit_problems(sai, node)
        found += mcp_env_problems(sai, node)
    found += llm_param_problems(sai, board)
    found += service_problems(sai, board, user_images, templates)
    found += bridge_problems(board)
    found += team_problems(board)
    found += session_login_problems(board, agent, values_, session_logins)
    found += policy_problems(board, values_, policy or {})
    return found


# -- the login of the start: "Login from" of the part HOME and login --

# The choices of "Login from", each with the options it sets (the others of LOGIN_OPTIONS are taken away): the central
# login and this computer's are both the launcher's automatic choice (the central one when one is stored).
LOGIN_CHOICES = (("central", N_("Central login (sandbox-ai login)")), ("host", N_("This computer's login")),
                 ("session", N_("From a session…")), ("file", N_("Login file…")), ("none", N_("No login")))
LOGIN_OPTIONS = ("--no-auth", "--auth-file", "--auth-from-session", "--import-codex-auth")
ROTATING_WARNING = N_("Subscription logins rotate their refresh tokens: this start and the session {name} refreshing "
                      "one login outside the central store can log each other out. Safer: import it as the central "
                      "login (Import as central login instead).")


def login_choice(props, central=None):
    """Which "Login from" PROPS (the HOME and login part's) stand for: none, file, session, else central (CENTRAL: a
    central login is stored, or not known) or host."""
    if props.get("--no-auth"):
        return "none"
    if props.get("--auth-file") or props.get("login_from") == "file":
        return "file"
    if "--auth-from-session" in props or props.get("login_from") == "session":
        return "session"
    return "host" if central is False else "central"


def set_login_choice(props, choice, value=None):
    """PROPS made to say CHOICE (login_choice's words): its option set (VALUE: the session's name or the file), the
    other login options taken away."""
    for option in LOGIN_OPTIONS:
        props.pop(option, None)
    props.pop("login_from", None)
    if choice == "none":
        props["--no-auth"] = True
    elif choice == "file":
        if value:
            props["--auth-file"] = str(value)
        else:
            props["login_from"] = "file"
    elif choice == "session":
        if value:
            props["--auth-from-session"] = str(value)
        else:
            props["login_from"] = "session"


def session_login_problems(board, agent, values_, sessions):
    """The checks of "Login from: a session" (--auth-from-session NAME): the rotating refresh tokens (always), and with
    SESSIONS (sandbox-ai login --status --sessions of the board's host) a session that is no longer there (ended
    without its HOME), one of another agent, one without a usable login."""
    found = []
    home = board.first("home")
    node = home.id if home is not None else None
    if home is not None and home.props.get("login_from") == "session" and not values_.get("--auth-from-session"):
        found.append(Problem("error", tr("Login from a session: choose the session."), node))
    if home is not None and home.props.get("login_from") == "file" and not values_.get("--auth-file"):
        found.append(Problem("error", tr("Login from a login file: choose the file."), node))
    name = values_.get("--auth-from-session")
    if not name:
        return found
    found.append(Problem("warning", tr(ROTATING_WARNING, name=printable(name)), node))
    if not isinstance(sessions, dict):
        return found
    rows = [row for row in sessions.get("sessions") or [] if isinstance(row, dict)]
    row = next((r for r in rows if r.get("name") == name), None) or next((r for r in rows if r.get("id") == name), None)
    if row is None:
        found.append(Problem("error", tr("The session {name} is neither running nor kept with its HOME: it has no "
                                         "login to give.", name=printable(name)), node))
    elif row.get("agent") != agent:
        found.append(Problem("error", tr("The session {name} runs {other}: its login is no {agent} login.",
                                         name=printable(name), other=printable(str(row.get("agent"))),
                                         agent=printable(agent)), node))
    elif not row.get("login"):
        found.append(Problem("error", tr("The session {name} has no usable login: {why}", name=printable(name),
                                         why=printable(str(row.get("problem") or tr("no login")))), node))
    return found


# -- the Bridge part: a session bridge made once the board's start runs --

BRIDGE_DIRECTIONS = (("out", N_("this agent reaches the other session")),
                     ("in", N_("the other session reaches this agent")))


def bridge_nodes(board):
    """The Bridge parts connected to the agent (an unconnected one is not made)."""
    return [node for node in board.included() if node.kind == "bridge"]


def bridge_caps(props, strict=True):
    """The --allow list of a Bridge part's PROPS (paths relative to the other workspace). STRICT: ValueError for what
    the launcher would refuse; else what can be shown."""
    try:
        return BR.capabilities(bool(props.get("message")), bool(props.get("ask")), _texts(props.get("read")),
                               _texts(props.get("write")), bool(props.get("watch")))
    except ValueError:
        if strict:
            raise
        return [name for name in ("message", "ask", "watch") if props.get(name)]


def _texts(value):
    return [str(v) for v in value if isinstance(v, (str, int, float))] if isinstance(value, list) else []


def bridge_values(props, name, host=None):
    """{a, b, allow, ...} (bridges.add_values) of a Bridge part's PROPS for the start NAME of the board: A reaches B -
    the board's start reaches the other session ("out", the default) or the other one reaches it ("in"). HOST: where
    the board starts (None or "local": this machine) - its start is HOST:NAME then, and the other session is named as
    the board's host sees it (bridges.on_host: a bare name is a session of HOST, local:NAME this machine's).
    ValueError: what is missing or wrong."""
    peer = BR.on_host(str(props.get("peer") or "").strip(), host)
    start = name if host in (None, "local") else f"{host}:{name}"
    a, b = (start, peer) if props.get("direction", "out") == "out" else (peer, start)
    limits = props.get("limits") if isinstance(props.get("limits"), dict) else {}
    return BR.add_values(a, b, bridge_caps(props), both=bool(props.get("both")), until=props.get("until") or "",
                         ask=props.get("ask_mode") or "first", watch=bool(props.get("watch")),
                         actions=_texts(props.get("actions")) if "actions" in props else BR.DEFAULT_ACTIONS,
                         limits={key: str(value) for key, value in limits.items() if key in dict(
                             (k, None) for k, *_ in BR.LIMITS)})


def bridge_problems(board):
    """[Problem] of the Bridge parts: no other session chosen (none is made), what the launcher would refuse, and an
    agent that reads its MCP servers only when it starts (it would not see the bridge)."""
    found = []
    agent = (board.first("agent").props.get("agent", "claude") if board.first("agent") else "claude")
    for node in bridge_nodes(board):
        if not str(node.props.get("peer") or "").strip():
            found.append(Problem("warning", tr("Bridge: choose the running session it joins; without one no bridge "
                                               "is made."), node.id))
            continue
        try:
            bridge_values(node.props, "board-start")
        except ValueError as exc:
            found.append(Problem("error", tr("Bridge to {peer}: {problem}", peer=printable(node.props["peer"]),
                                             problem=printable_text(str(exc).replace("\n", "; "))), node.id))
        props = node.props
        # This agent gets a tool of the bridge: it reaches the other session, or it is reached with an inbox (a
        # message, question or hint) or both ways; only watched (or read), it needs none.
        sees = props.get("direction", "out") == "out" or BR.peer_sees_tools(
            props.get("message"), props.get("ask"), props.get("watch") and "hint" in _texts(props.get("actions")),
            props.get("both"))
        if BR.mcp_at_start(agent) and sees:
            found.append(Problem("warning", tr("Bridge: {agent} reads its MCP servers only when it starts, so it does "
                                               "not see a bridge made after its start (Claude Code does).",
                                               agent=printable(agent)), node.id))
    return found


# -- the Team part: the board's sandbox once per agent (sandbox-ai team), started with team start --

TEAM_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,19}")  # sandbox-ai's team.TEAM_NAME
# A member's own launcher options on the Team part: (its props key, the launcher option team start gives it)
TEAM_MEMBER_OPTIONS = (("models", "--model"), ("llms", "--llm"), ("modes", "--mode"))


def team_label(name, host):
    """A team's (or member's) NAME as this machine names it: NAME here, HOST:NAME of another host (as bridges and
    the Sessions page name a start of a host)."""
    return name if host in (None, "local") else f"{printable(host)}:{name}"


def team_node(board):
    """The board's Team part while it is connected to the agent, else None."""
    node = board.first("team")
    return node if node is not None and board.connected(node) else None


def team_members(props):
    """The agents of a Team part's PROPS, the lead first (its "lead" and "members"; team agents only, each once)."""
    members = [a for a in props.get("members") or [] if a in backend.TEAM_AGENTS] \
        if isinstance(props.get("members"), list) else []
    lead = props.get("lead")
    return list(dict.fromkeys(([lead] if lead in backend.TEAM_AGENTS else []) + members))


def _member_map(props, key, members):
    value = props.get(key)
    return {agent: str(text).strip() for agent, text in value.items() if agent in members and str(text or "").strip()} \
        if isinstance(value, dict) else {}


def team_options(sai, board, local=True):
    """The launcher options of every member of a team made from BOARD: the board's own (launcher_argv) without what
    team start chooses itself (the workspace, the project's name) and without the agent's --model (the board's agent
    is one member: its model is that member's, team_values); --no-config, as a board start has it."""
    rules = project_rules(board)
    copy = ["--rules", str(rules_copy(rules))] if rules is not None and local else []
    argv, kept, index = document_argv(board) + to_argv(sai, board, workspaces=False) + copy, [], 0
    while index < len(argv):
        token = argv[index]
        if token in ("--project-name", "--model") and index + 1 < len(argv):
            index += 2
            continue
        if not token.startswith(("--project-name=", "--model=")):
            kept.append(token)
        index += 1
    return kept + ["--no-config"]


def team_values(sai, board, local=True):
    """{name, agents, workspace, options, roles, member_options, shared_openvpn} of the board's Team part - the
    arguments of backend.team_start_args - or None without one. The board's agent, when it is a member, takes the
    Agent part's model unless the Team part gives it another."""
    node = team_node(board)
    if node is None:
        return None
    props, members = node.props, team_members(node.props)
    workspaces = [str(n.props.get("path") or "").strip() for n in board.included() if n.kind == "workspace"]
    own = {}
    for key, option in TEAM_MEMBER_OPTIONS:
        for agent, value in _member_map(props, key, members).items():
            own.setdefault(agent, {})[option] = value
    agent = board.first("agent")
    model = str(agent.props.get("model") or "").strip() if agent is not None else ""
    chosen = agent.props.get("agent", "claude") if agent is not None else None
    if model and chosen in members and "--model" not in own.get(chosen, {}):
        own.setdefault(chosen, {})["--model"] = model
    return {"name": str(props.get("name") or "").strip(), "agents": members,
            "workspace": next((w for w in workspaces if w), ""), "options": team_options(sai, board, local),
            "roles": _member_map(props, "roles", members), "member_options": own,
            "shared_openvpn": bool(props.get("shared_openvpn"))}


def team_problems(board):
    """[Problem] of the Team part (what the app needs to ask team start at all): its name, two or more members, one
    project folder, no Bridge part beside it, the agent's arguments (a member starts with its team prompt). The rest -
    the options a team chooses itself, VPN profiles in several members, a member's model or mode - is team start's
    own check (team start --dry-run on the board's host), shown as its verdict."""
    node = team_node(board)
    if node is None:
        return []
    found, props = [], node.props
    if not TEAM_NAME.fullmatch(str(props.get("name") or "")):
        found.append(Problem("error", tr("Team: give it a name of 1-20 of a-z, 0-9, '_' and '-', starting with a "
                                         "letter or digit."), node.id))
    if len(team_members(props)) < 2:
        found.append(Problem("error", tr("Team: choose a lead and at least one more member."), node.id))
    workspaces = [n for n in board.included() if n.kind == "workspace"]
    if len(workspaces) > 1:
        found.append(Problem("error", tr("Team: its members share one project folder; connect only one."), node.id))
    if any(str(n.props.get("peer") or "").strip() for n in bridge_nodes(board)):
        found.append(Problem("error", tr("Team: a board with a team makes no bridges (its members are the sessions "
                                         "NAME-AGENT); remove the Bridge part or the Team part."), node.id))
    agent = board.first("agent")
    if agent is not None and str(agent.props.get("arguments") or "").strip():
        found.append(Problem("warning", tr("Team: the agent's arguments are not passed on; each member starts with "
                                           "its team prompt (and its role)."), node.id))
    # The Agent part's effort and thinking go to every member (team start has no option of a member's own for them):
    # a member whose agent does not take them refuses its start (agent_sandbox.check_model_effort, check_show_thinking).
    from . import thinking
    from .guide import AGENT_EFFORTS
    effort = str(agent.props.get("effort") or "").strip() if agent is not None else ""
    refusing = [a for a in team_members(props) if effort and effort not in AGENT_EFFORTS.get(a, ())]
    if refusing:
        found.append(Problem("error", tr("Team: --effort {level} goes to every member, and {agents} take no such "
                                         "level (their start is refused). Set the Agent part's effort back to the "
                                         "agent's default, or choose members that take it.", level=printable(effort),
                                         agents=", ".join(refusing)), node.id))
    shown = agent.props.get("thinking") if agent is not None else None
    silent = [a for a in team_members(props) if shown in thinking.VALUES and not thinking.available(a)]
    if silent:
        found.append(Problem("error", tr("Team: --show-thinking {value} goes to every member, and {agents} have no "
                                         "switch for it (their start is refused). Set Show thinking back to the "
                                         "agent's default.", value=shown, agents=", ".join(silent)), node.id))
    return found


def mcp_limit_problems(sai, node):
    """[Problem] of an MCP node's own limits (props "limits"): each checked by sandbox-ai's own rules (mcp.py's
    checked_limits, the rules of `mcp set` and --mcp-limits); a sandbox-ai without --mcp-limits runs the server with
    its own limits (said)."""
    limits = node.props.get("limits")
    if not isinstance(limits, dict) or not any(str(v).strip() for v in limits.values()):
        return []
    if sai is not None and not launcher_knows(sai, "--mcp-limits"):
        return [Problem("warning", tr("This sandbox-ai has no --mcp-limits: the server runs with its own limits."),
                        node.id)]
    found = []
    for key in LIMIT_KEYS:
        if str(limits.get(key, "")).strip():
            found += _checked(lambda key=key: sai.module("mcp").checked_limits({key: limits[key]}, tr("MCP server")),
                              node) if sai is not None else []
    given = {key: limits[key] for key in ("memory", "tmp") if str(limits.get(key, "")).strip()}
    if not found and "tmp" in given and sai is not None:  # /tmp at most the memory (the server's own where not given)
        defaults = mcp_defaults(sai, (sai.mcp_definitions() if hasattr(sai, "mcp_definitions") else {}).get(
            node.props.get("name"))) if "memory" not in given else None
        memory = given.get("memory") or (defaults or {}).get("memory")
        if memory:
            found += _checked(lambda: sai.module("mcp").check_tmp_fits({"tmp": str(given["tmp"]).lower(),
                                                                       "memory": str(memory).lower()},
                                                                      tr("MCP server")), node)
    return found


MCP_PROBLEMS = re.compile(r"\b\d+ problems with (?:the MCP servers of )?this start\b")
# An image the start needs that is not built on its host (the launcher asks Podman before anything is made).
IMAGE_PROBLEM = re.compile(r"The image of (\S+(?: browser)?) \(\S+\) is not built on this host\b")
MCP_PROBLEM_NAME = re.compile(r"(?:Unknown MCP server: |MCP server |--mcp-(?:limits|mount|env) )([A-Za-z0-9_-]{1,32})")


def missing_workspaces(board):
    """[(node id, folder)] of the board's workspaces (the parts in use, with a folder) that are not there on this
    machine: made at Start or Apply after a yes, or at once with Create now (a start used to end in "[Errno 2] No such
    file or directory" for a project folder not made yet)."""
    found = []
    for node in board.of_kind("workspace"):
        path = str(node.props.get("path") or "").strip()
        if not path or node not in board.included():
            continue
        folder = os.path.expanduser(path)
        if not os.path.isabs(folder):  # (an error of its own: never made below the app's own folder)
            continue
        try:
            there = os.path.isdir(folder)
        except (OSError, ValueError):
            there = False
        if not there and not os.path.lexists(folder):
            found.append((node.id, folder))
    return found


def missing_problems(missing):
    """[Problem] of the folders MISSING (missing_workspaces): a warning on each part - it will be created."""
    return [Problem("warning", tr("The folder {path} does not exist: it will be created (Create now, or at Start or "
                                  "Apply).", path=printable(folder)), node_id) for node_id, folder in missing]


def tool_budget_problems(board, plan):
    """[Problem] of the launcher's dry run PLAN when the board's model would see more MCP tools than its class copes
    with ("mcp_tools": sandbox-ai's mcp_budget count, round 5 of the local-model research): one warning on the LLM part
    with the servers to leave out - never an error, the start goes on; [] otherwise (an older sandbox-ai has none)."""
    from . import model_profile as MP
    text = MP.tool_budget_finding((plan or {}).get("mcp_tools") if isinstance(plan, dict) else None)
    if not text:
        return []
    llm = board.first("llm")
    return [Problem("warning", printable_text(text)[:800], llm.id if llm is not None else None)]


def launcher_problems(board, message):
    """[Problem] of what the launcher's dry run refused about the board's MCP servers, each problem on its own (the
    launcher says all of them at once before anything is made: mcp.start_problems), on the MCP part it is about; []
    when MESSAGE is about something else."""
    lines = [line.strip() for line in str(message or "").splitlines() if line.strip()]
    header = next((index for index, line in enumerate(lines) if MCP_PROBLEMS.search(line)), None)
    if header is not None:
        items = [line[2:].strip() for line in lines[header + 1:] if line.startswith("- ")]
    else:
        last = re.sub(r"^(?:agent-sandbox|sandbox-ai): ", "", lines[-1]) if lines else ""
        items = [last] if MCP_PROBLEM_NAME.match(last) or IMAGE_PROBLEM.match(last) else []
    nodes = {n.props.get("name"): n.id for n in board.of_kind("mcp") if n.props.get("name")}
    agent = board.first("agent")
    found = []
    for item in items[:20]:
        named = MCP_PROBLEM_NAME.match(item)
        node = nodes.get(named.group(1)) if named else agent.id if IMAGE_PROBLEM.match(item) and agent else None
        found.append(Problem("error", printable(item)[:800], node))
    return found


# (The launcher names the image store it looked in - Podman's GraphRoot and the user - since a build by root or another
# user went where the start does not look; never offered for another machine's Podman, "(Podman at URL)": a build here
# would not reach it. A variant several agents have is NAME@AGENT.)
BUILD_OFFER = re.compile(r"is not built on this host(?: \(Podman's image store: [^()\n]{1,600}\))? \(nothing is pulled "
                         r"at a start\): sandbox-ai (build(?: --variant [A-Za-z0-9][A-Za-z0-9_.-]{0,63}"
                         r"(?:@[a-z]{2,16})?| [a-z][a-z0-9-]{0,31}))[ \t]*$", re.M)


# A secret a start names that is not kept yet, as the launcher says it (--api-key, --secret-env, --service, --publish:
# "no secret NAME; keep one with: sandbox-ai secrets set NAME"): only the very same name twice, so that nothing else of
# a message is ever read as a name.
SECRET_OFFER = re.compile(r"no secret ([A-Za-z_][A-Za-z0-9_]{0,63}); keep one with: sandbox-ai secrets set \1(?![A-Za-z0-9_])")


def secret_offers(message):
    """[NAME, ...] of the secrets the launcher's refusal MESSAGE says are not kept yet, each once: the Board offers to
    keep each (a password field, then `sandbox-ai secrets set NAME` with the value on its standard input)."""
    found = []
    for name in SECRET_OFFER.findall(str(message or "")):
        if name not in found:
            found.append(name)
    return found


def build_offers(message):
    """[[build, ...]] of the image builds the launcher's refusal MESSAGE names (`sandbox-ai build AGENT`, `build
    --variant NAME`, `build browser`...), each once: Build now runs them."""
    found = []
    for words in BUILD_OFFER.findall(str(message or "")):
        args = words.split()
        if args not in found:
            found.append(args)
    return found


def mcp_env_problems(sai, node):
    """[Problem] of an MCP node's own variables (props "variables": --mcp-env): each checked by sandbox-ai's own rules
    (mcp.parse_start_env: one line, nothing of a secret's shape or name as a plain value, a secret's name); a
    sandbox-ai without --mcp-env runs the server with its own variables (said)."""
    specs = [spec for spec in node.props.get("variables") or [] if isinstance(spec, str) and spec.strip()]
    if not specs or not node.props.get("name"):
        return []
    if sai is not None and not launcher_knows(sai, "--mcp-env"):
        return [Problem("warning", tr("This sandbox-ai has no --mcp-env: the server runs with its own variables."),
                        node.id)]
    found = []
    for spec in specs:
        found += _checked(lambda spec=spec: sai.module("mcp").parse_start_env(f"{node.props['name']}:{spec}"),
                          node) if sai is not None else []
    return found


def llm_param_problems(sai, board):
    """[Problem] of the LLM node's own parameters (props "params"): each checked against this sandbox-ai's catalogue
    (llm.params_catalogue: the names, types and ranges; whether the server's kind takes one is the launcher's check);
    a sandbox-ai without --llm-param starts with the server's own parameters (said)."""
    node = board.first("llm")
    params = node.props.get("params") if node is not None else None
    if not isinstance(params, dict) or not any(str(v).strip() for v in params.values()):
        return []
    if sai is not None and not launcher_knows(sai, "--llm-param"):
        return [Problem("warning", tr("This sandbox-ai has no --llm-param: the server's own parameters hold."),
                        node.id)]
    catalogue = LP.local_catalogue(sai) if sai is not None else None
    spec = LP.loop_spec(sai) if sai is not None else None
    return [Problem("error", tr("Model parameter {name}: {why}", name=printable(key), why=why), node.id)
            for key, why in LP.board_problems(params, catalogue, spec)]


def _checked(function, node):
    """[Problem] of what sandbox-ai's own check FUNCTION refuses (nothing when this sandbox-ai cannot check)."""
    try:
        function()
    except ValueError as exc:
        return [Problem("error", printable_text(str(exc)), node.id if node is not None else None)]
    except Exception as _exc:  # noqa: BLE001 - an older sandbox-ai without the check: the launcher has the last word
        applog.swallowed("board", "_checked: an older sandbox-ai without the check: the launcher has the last w…", _exc)
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


LAUNCHER_WARNING = re.compile(r"^(?:agent-sandbox|sandbox-ai(?: [a-z-]+)?): warning: (.+)$")


def launcher_warnings(text):
    """[warning] of the launcher's own in TEXT (what a sandbox-ai command said on standard error or output): each
    line "agent-sandbox: warning: ..." (or "sandbox-ai mcp: warning: ..."), without its prefix, each once. A host
    path you chose with a risk (HOME, sandbox-ai's own folders, the launcher's tree, a kernel tree...) is given, and
    such a line says what it means."""
    found = []
    for line in str(text or "").splitlines():
        match = LAUNCHER_WARNING.match(line.strip())
        if match and match.group(1) not in found:
            found.append(match.group(1))
    return found


def path_warnings(warnings):
    """The launcher's WARNINGS (launcher_warnings: English, as the launcher says them) under a line of the app's own
    in the chosen language; "" without any."""
    if not warnings:
        return ""
    return tr("You decide what a sandbox gets: it is given, and the launcher says what it means (read-only is the "
              "safer choice):") + "\n" + "\n".join(f"• {printable_text(text)}" for text in warnings)


SYSTEM_TOPS = ("etc", "var", "usr", "boot", "root", "proc", "sys", "dev")  # /NAME: the system's, not a project's


def sensitive(path, launcher=None):
    """Why the agent should not see host PATH ("bad"), or a note ("warn"), or None for an ordinary folder.
    The launcher's own private_locations() (its settings, keys and program directories) count when given. A folder
    of another machine (sshfs://...) is a note of its own: the start's host mounts it, the files are that machine's."""
    from . import remote_source
    if remote_source.is_remote(str(path)):
        return "warn", tr("a folder of another machine, mounted with sshfs: its files are that machine's, its traffic "
                          "does not pass the sandbox's proxy")
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
        except Exception as _exc:  # noqa: BLE001 - the parts below still say enough
            applog.swallowed("board", "sensitive: the parts below still say enough", _exc)
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
    # A system directory, as whole path components (not /rootfs), and never inside your HOME: root's projects live
    # under /root, a HOME may be under /var (/var/home/USER).
    if resolved.parts[1:2] and resolved.parts[1] in SYSTEM_TOPS and home not in resolved.parents:
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


def capabilities(board, launcher=None, definitions=None, bases=None):
    """What the agent of BOARD can READ and WRITE on the host, REACH on the network, which SECRETS it holds and
    which CODE from outside it runs - each with a level and the points it costs the score. DEFINITIONS:
    {name: MCP server definition} for servers defined outside the board (mcp.json). BASES: {variant: your image it is
    built on} of the image variants (when known)."""
    agent, values_, _ = values(board)
    found = []

    def add(area, level, text, node=None, cost=0):
        found.append(Capability(area, level, text, node.id if node is not None else None, cost))

    verdicts = {}  # {path: sensitive()} of this look: a writable folder is judged once for READ and WRITE (each
    # judgement resolves the path on disk, every step of the board: a few lstat calls per folder)

    def verdict_of(path):
        key = str(path)
        if key not in verdicts:
            verdicts[key] = sensitive(path, launcher)
        return verdicts[key]

    def folder(area, path, writable, node, how):
        """One entry per area; a writable folder costs its points once, in the WRITE area."""
        verdict = verdict_of(path)
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
        level = "bad" if verdict_of(source) else "warn"
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
        for value in values_.get("--allow-domain", []):
            add("reach", "bad", tr("{domain} and every host below it", domain=printable(_domain(value))), network, 15)
        for value in values_.get("--allow-shared-wildcard", []):
            add("reach", "bad", tr("Every host matching {host}, below a shared service: any server anyone rents "
                                   "there", host=printable(value)), network, 25)
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
    if values_.get("--auth-from-session"):
        add("secrets", "warn", tr("The login of the session {name} is copied in",
                                  name=printable(values_["--auth-from-session"])), home, 5)
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
        base = (bases or {}).get(values_["--image-variant"])
        if base:
            # The sandbox's own supervisor (firewall, proxy, approvals) runs from that image's programs.
            add("code", "bad", tr("Image variant {name} is built on your image {base}, UNCHECKED: the sandbox's own "
                                  "firewall, proxy and approvals run from that image's programs",
                                  name=printable(values_["--image-variant"]), base=printable(base)), engine, 20)
        else:
            add("code", "warn", tr("Image variant {name}: built from your extension of the image",
                                   name=printable(values_["--image-variant"])), engine, 5)
    if values_.get("--binary"):
        verdict = verdict_of(values_["--binary"])
        add("code", "bad", tr("Agent program {path}{why}", path=printable(values_["--binary"]),
                              why=f" - {verdict[1]}" if verdict else ""), engine, 15)
    for value in values_.get("--runtime", []):
        add("code", "warn", tr("Runtime folder {path} (read-only, runs inside)", path=printable(value)), engine, 8)
    if values_.get("--nested-containers"):
        # Its network stays the sandbox's (the containers' connections pass the firewall and the proxy); what weakens
        # is the kernel within reach: user namespaces of its own, mounts and more system calls.
        add("code", "warn", tr("Nested containers: the agent is root of a user namespace of its own and runs "
                               "containers (more of your host's kernel within reach)"), engine, 10)
    if values_.get("--podman-url"):
        add("code", "warn", tr("Another Podman service: {url}", url=printable(values_["--podman-url"])), engine, 10)
    display = board.first("display")
    if values_.get("--gui-vnc"):
        add("write", "warn", tr("VNC socket in {path}: whoever reaches it sees and controls the display",
                                path=printable(values_["--gui-vnc"])), display, 5)
    if values_.get("--gui-wayland"):
        add("read", "bad", tr("The host's Wayland socket weakens the isolation"), display, 15)
    browser = board.first("browser")
    if browser is not None and values_.get("--browser"):
        control = values_.get("--browser-control") or "human"
        if control in ("agent", "both"):
            add("code", "warn", tr("The agent drives the browser (Chrome's DevTools at 127.0.0.1:9222 in the pod)") +
                ("; " + tr("whatever you sign in to there, the agent can use") if control == "both" else ""),
                browser, 8 if control == "both" else 5)
        profile = str(values_.get("--browser-profile") or "fresh")
        if profile not in ("fresh", "session"):
            add("write", "warn", tr("The browser keeps its profile (logins, cookies) in {path}", path=printable(profile)),
                browser, 3)
    cairn = board.first("cairn")
    if cairn is not None and values_.get("--cairn"):
        # Cairn hands out tasks on its own, and its workers run without their own questions (Cairn's flags).
        add("code", "warn", tr("Cairn starts the session's agents as its workers on its own, without their approval "
                               "prompts (the sandbox stays in force); every task spends your models' usage"), cairn, 5)
    artex = board.first("artex")
    if artex is not None and values_.get("--artex"):
        # ARTEX's own agents run shell commands, scanners and a browser without asking (its design).
        add("code", "warn", tr("ARTEX's own agents run shell commands, scanners and a browser without asking; its "
                               "targets are only what the allow-list opens"), artex, 5)
    devices = board.first("devices")
    for value in values_.get("--device", []):
        add("read", "bad", tr("Host device {path}", path=printable(value)), devices, 15)
    if values_.get("--gpu"):
        add("read", "warn", tr("GPU devices ({kind})", kind=printable(values_["--gpu"])), devices, 10)
    toolchains = board.first("toolchains")
    for value in values_.get("--toolchain", []):
        # Signed code the agent runs in the sandbox, read-only; its registries are opened unless the part says not to.
        add("code", "info", tr("Toolchain {name}: mounted read-only, its registries stay closed", name=printable(value))
            if values_.get("--toolchain-offline") else
            tr("Toolchain {name}: mounted read-only, its package registries are opened", name=printable(value)),
            toolchains, 2)
    if values_.get("--dangerously-skip-permissions"):
        add("code", "info", tr("The agent runs without its own approval prompts (the sandbox stays in force)"),
            agent_node, 3)
    for value in values_.get("--hermes-enable", []):
        # Hermes' features each open their own hosts (hermes.py): a messaging platform reaches the agent from outside.
        level = "warn" if str(value).startswith("gateway:") else "info"
        add("reach", level, tr("Hermes: {features} turned on (their hosts open)", features=printable(value)),
            agent_node, 5 if level == "warn" else 2)
    for value in values_.get("--qwen-enable", []):
        # Qwen Code's features each open their own hosts (qwen.py); its telemetry is never one of them.
        add("reach", "info", tr("Qwen Code: {features} turned on (their hosts open)", features=printable(value)),
            agent_node, 2)
    for value in values_.get("--goose-enable", []):
        # Goose's features each open their own hosts (goose.py); its gateway reaches the agent from outside, its
        # scheduler runs recipes on its own. Its telemetry is never one of them.
        level = "warn" if str(value).startswith(("gateway:", "scheduler")) else "info"
        add("reach", level, tr("Goose: {features} turned on (their hosts open)", features=printable(value)),
            agent_node, 5 if level == "warn" else 2)
    for value in values_.get("--openhands-enable", []):
        # OpenHands' features each open their own hosts (openhands.py); its automation backend runs automations on its
        # own. Its telemetry is never one of them.
        level = "warn" if "automations" in str(value) else "info"
        add("reach", level, tr("OpenHands: {features} turned on (their hosts open)", features=printable(value)),
            agent_node, 5 if level == "warn" else 2)
    for value in values_.get("--nanobot-enable", []):
        # nanobot's features each open their own hosts (nanobot.py); its Telegram channel reaches the agent from
        # outside, its scheduler runs turns on its own. Its telemetry is never one of them.
        level = "warn" if str(value).startswith(("gateway:", "scheduler")) else "info"
        add("reach", level, tr("nanobot: {features} turned on (their hosts open)", features=printable(value)),
            agent_node, 5 if level == "warn" else 2)
    for value in values_.get("--letta-enable", []):
        # Letta Code's features each open their own hosts (letta.py); cloud puts its memory in Letta Cloud. Its
        # telemetry is never one of them.
        level = "warn" if "cloud" in str(value).split(",") else "info"
        add("reach", level, tr("Letta Code: {features} turned on (their hosts open)", features=printable(value)),
            agent_node, 5 if level == "warn" else 2)
    for value in values_.get("--opencode2-enable", []):
        # OpenCode 2's features each open their own hosts (opencode2.py); plugins install code from npm at run time.
        # Its telemetry is never one of them.
        level = "warn" if "plugins" in str(value).split(",") else "info"
        add("reach", level, tr("OpenCode 2: {features} turned on (their hosts open)", features=printable(value)),
            agent_node, 5 if level == "warn" else 2)
    for value in values_.get("--pi-enable", []):
        # Pi's features each open their own hosts (pi.py); packages installs code from npm and GitHub that runs in
        # the sandbox, share uploads the session. Its telemetry is never one of them.
        level = "warn" if {"packages", "share"} & set(str(value).split(",")) else "info"
        add("reach", level, tr("Pi: {features} turned on (their hosts open)", features=printable(value)),
            agent_node, 5 if level == "warn" else 2)
    # Services: containers of images nobody reviewed, beside the agent (on its network, behind its firewall)
    for node in board.included():
        if node.kind != "service" or not node.props.get("name"):
            continue
        add("code", "warn", tr("Service {name}: the image {image} is UNCHECKED (nobody reviewed it); it runs beside the "
                               "agent, behind the sandbox's firewall and proxy",
                               name=printable(node.props["name"]), image=printable(node.props.get("image") or "?")),
            node, 5)
        if node.props.get("read_only") is False:
            add("code", "warn", tr("Service {name}: a writable root file system", name=printable(node.props["name"])),
                node, 2)
        for entry in node.props.get("secrets") or []:
            add("secrets", "warn", tr("Secret {secret} given to the service {name}", secret=printable(
                entry.get("secret") or entry.get("name")), name=printable(node.props["name"])), node, 2)
    # Session bridges: another running session reaches this one or is reached (its data can flow either way)
    for node in bridge_nodes(board):
        peer = printable(node.props.get("peer") or "?")
        caps = printable(",".join(bridge_caps(node.props, strict=False)) or "-")
        text = tr("Bridge: it may reach the session {peer} ({caps})", peer=peer, caps=caps) \
            if node.props.get("direction", "out") == "out" else \
            tr("Bridge: the session {peer} may reach it ({caps})", peer=peer, caps=caps)
        add("reach", "warn", text + ("; " + tr("both ways") if node.props.get("both") else ""), node, 5)
    # A team: the board's sandbox once per member, all on one project folder
    team = team_node(board)
    if team is not None and len(team_members(team.props)) > 1:
        add("reach", "info", tr("Team {name}: {count} sandboxes of these settings ({members}) on one project folder; "
                                "nothing keeps two of them from changing the same file", name=printable(
                                    team.props.get("name") or "?"), count=len(team_members(team.props)),
                                members=", ".join(team_members(team.props))), team, 0)
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
            if isinstance(mount, dict) and mount.get("host") == WORKSPACE_FOLDER:
                # the start's workspace (bound at each start): the board's own workspace, said as such
                add("read", "info", tr("{how}: the start's workspace at {target} ({mode})", how=how,
                                       target=printable(str(mount.get("target"))),
                                       mode=tr("writable") if mount.get("mode") == "rw" else tr("read-only")), node, 0)
            elif isinstance(mount, dict) and mount.get("host"):
                writable = mount.get("mode") == "rw"
                for area in ("read", "write") if writable else ("read",):
                    folder(area, mount["host"], writable, node, how)
        if definition.get("command") and not definition.get("image"):
            add("code", "warn", tr("{how} runs {command} in a sandbox of its own", how=how,
                                   command=printable(" ".join(map(str, definition["command"])))), node, 5)
    return found


def badges(board, policy=None, launcher=None, definitions=None, bases=None):
    """(score 0..100, [Badge]) from capabilities() (what weakens the sandbox and what keeps it tight) and the
    administrator's policy. BASES: see capabilities()."""
    found = [Badge(c.level, c.text, c.node, c.cost) for c in capabilities(board, launcher, definitions, bases)]
    _, values_, _ = values(board)
    for problem in policy_problems(board, values_, policy or {}):
        found.append(Badge("bad", problem.text, problem.node, 0))
    score = max(0, min(100, 100 - sum(b.cost for b in found)))
    return score, found


def panels(board, launcher=None, definitions=None, bases=None):
    """{area: [Capability]} for the panels beside the graph: READ, WRITE, REACH, SECRETS, CODE."""
    found = {area: [] for area in CAPABILITIES}
    for item in capabilities(board, launcher, definitions, bases):
        found[item.area].append(item)
    return found


def server_network(definition):
    """(reaches the network, [what it reaches], VPN profile or None) of an MCP server's DEFINITION: an image's
    network and hosts, or the launcher options of a server with a command (no network unless they allow it)."""
    if "image" in definition:
        return (backend.mcp_network(definition.get("network", "none")) != "none",
                [str(h) for h in definition.get("hosts", [])],
                definition.get("vpn"))
    options, reached, vpn, everything = [str(o) for o in definition.get("options", [])], [], None, False
    for index, option in enumerate(options):
        value = options[index + 1] if index + 1 < len(options) else ""
        if option in PRESET_OPTIONS:
            reached.append(option[len("--allow-"):])
        elif option in ("--allow-host", "--allow-shared-wildcard", "--allow", "--allow-net", "--allow-tcp"):
            reached.append(value)
        elif option == "--allow-domain":
            reached.append(f"{_domain(value)}, *.{_domain(value)}")
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
    mounts = [f"{tr('the start’s workspace') if m.get('host') == WORKSPACE_FOLDER else m.get('host', '?')} → "
              f"{m.get('target', '?')} ({m.get('mode', 'ro')})" for m in definition.get("mounts", []) if isinstance(m, dict)]
    network = backend.mcp_network_word(definition.get("network", "none"))  # (offline, egress-all, allowlist, lan)
    if "image" not in definition:
        reaches, hosts, _ = server_network(definition)
        network = (", ".join(hosts) or tr("every destination")) if reaches else "offline"
    rows = [(tr("Runs"), printable(runs)), (tr("Network"), printable(network))]
    if backend.is_burp(definition):
        # Burp Suite: host mode (your Burp, reached from this host) or container mode (your jar), Community or Pro
        rows.insert(0, (tr("Burp"), backend.burp_text(definition)))
        if "endpoint" in definition:
            rows[1] = (tr("Runs"), tr("your Burp on this host, reached at {endpoint}",
                                      endpoint=printable(str(definition["endpoint"]))))
    if server_network(definition)[2]:
        rows.append((tr("VPN"), printable(server_network(definition)[2])))
    if definition.get("hosts"):
        rows.append((tr("Hosts"), printable(", ".join(map(str, definition["hosts"])))))
    rows.append((tr("Folders"), printable("; ".join(mounts)) if mounts else tr("none")))
    return rows


def mcp_defaults(sai, definition):
    """{memory, cpus, pids} server DEFINITION runs with unless a board asks for others (sandbox-ai's own
    mcp.default_limits), or None (not defined here, a helper, or a sandbox-ai that cannot say)."""
    if not isinstance(definition, dict) or sai is None:
        return None
    try:
        return sai.module("mcp").default_limits(definition)
    except Exception as _exc:  # noqa: BLE001 - an older sandbox-ai without limits per server
        applog.swallowed("board", "mcp_defaults: an older sandbox-ai without limits per server", _exc)
        return None


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


# -- a board as one self-sufficient file (sandbox-ai board export|import) --

BUNDLE_APP_KINDS = ("bridge", "team")  # parts of the app alone (no launcher option): carried by the board's state


def bundle_document(sai, board, name):
    """The board file `sandbox-ai board export --board -` reads ("sandbox-ai-board", version 1): the board's start as
    the launcher takes it (the project file's own keys as options, every workspace), the project's own MCP servers and
    ruleset, and the board itself (its parts, places, labels, bridges and team: Board.state())."""
    agent, _, agent_args = values(board, sai)
    options = [*document_argv(board), *to_argv(sai, board)]
    project = {}
    rules = project_rules(board)
    if rules is not None:
        project["rules"] = json.loads(json.dumps(rules))
    servers = {n.props["name"]: n.props["definition"] for n in board.included() if n.kind == "mcp"
               and n.props.get("project") and n.props.get("name") and isinstance(n.props.get("definition"), dict)}
    if servers:
        project["mcp_servers"] = json.loads(json.dumps(servers))
    if isinstance(board.document_extra.get("description"), str) and board.document_extra["description"]:
        project["description"] = board.document_extra["description"]
    return {"schema": "sandbox-ai-board", "version": 1, "name": str(name or "board")[:80], "agent": agent,
            "options": options, "agent_args": agent_args, "project": project, "state": board.state()}


def imported_board(sai, document):
    """The board of an imported board file (`sandbox-ai board import` wrote it): made from its start as the launcher
    takes it (from_argv: it means exactly that command line), with the places, labels, bridges and team of the board
    it came from (its state, data: read through Board.checked's rules, a part that does not fit left out). (board,
    the main workspace or None)."""
    options = [str(v) for v in document.get("options", []) if isinstance(v, str)]
    arguments = [str(v) for v in document.get("agent_args", []) if isinstance(v, str)]
    board = from_argv(sai, str(document.get("agent") or "claude"), options, arguments)
    state = document.get("state")
    old = None
    if isinstance(state, dict):
        try:
            old = Board.from_state(state)
        except (KeyError, TypeError, ValueError, AttributeError):
            old = None
    if old is not None:
        # The state is the bundle's - another machine's file, which anyone may have made or edited: a part of it is
        # used only when it is a part of this app at a place a board has (finite numbers), and a Bridge or Team part
        # only when a board can show and use its settings (Board.part_problem). Taken as they came, a place that was
        # a text or a setting named as one of Board.add's own arguments raised while the board was shown: the import
        # written, the Board left with half a scene.
        def number(value):
            return type(value) in (int, float) and value == value and abs(value) < 1e6
        sound = [node for node in old.nodes if node.kind in KINDS and isinstance(node.props, dict)
                 and number(node.x) and number(node.y)]
        board.place({"version": 1, "nodes": {node.key(): [node.x, node.y] for node in sound}})
        board.labels = {k: list(v) for k, v in old.labels.items()}
        agent, before = board.first("agent"), old.first("agent")
        for node in sound:
            if node.kind not in BUNDLE_APP_KINDS or (KINDS[node.kind].single and board.first(node.kind) is not None) \
                    or Board.part_problem(node.kind, node.props, sai) is not None:
                continue
            added = board.add(node.kind, connect=False)
            added.props = json.loads(json.dumps(node.props))  # (never as Board.add's own arguments)
            added.x, added.y = float(node.x), float(node.y)
            try:
                connected = before is not None and (before.id, node.id) in old.edges
            except TypeError:  # (an id that is no number: it names no connection)
                connected = False
            if connected and agent is not None:
                board.connect(agent.id, added.id)
    workspace = board.first("workspace")
    return board, (str(workspace.props.get("path")) if workspace is not None and workspace.props.get("path") else None)
