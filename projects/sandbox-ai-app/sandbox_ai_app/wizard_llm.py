"""An own model server for the wizard (sandbox-ai llm, the "raw API" of LM Studio, Ollama, llama.cpp, vLLM or any
OpenAI-compatible server): what the wizard's "Model server" step and the presets' <llm:...> placeholders need, without
Qt (tested without a display).

- the registered servers (`sandbox-ai llm list --json`, an unusable entry reported in a line before the JSON),
- a new one (`sandbox-ai llm add NAME --kind KIND --url URL [--api-key-secret NAME] [--vpn PROFILE] [--anthropic]`),
  then checked for the chosen agent (`sandbox-ai llm check NAME --agent AGENT`),
- its models (`sandbox-ai llm models NAME --json`: [{name, size, loaded, details}]),
- what goes on the board: --llm NAME[:MODEL] for whichever agent (Claude Code needs a server with the Anthropic
  Messages API; Codex and OpenCode take any OpenAI-compatible one), --no-provider-defaults for "only this server",
  and no --model (the launcher refuses --model with --llm: the model is the server's).

The functions that run a command (list_endpoints, add_and_check, list_models) block: the pages hand them to
widgets.background (tests/test_references.py knows them). Values that go back to sandbox-ai as arguments are checked
as sandbox-ai llm checks them, and none may read as an option.
"""
from __future__ import annotations

import json
import re

from .files import printable
from .i18n import N_, tr

# As sandbox-ai llm checks them (llm.py: NAME, SECRET, MODEL_NAME, VPN_PROFILE, URL).
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}")
SECRET = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
MODEL = re.compile(r"[\w.:/@+-]{1,200}", re.ASCII)
VPN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}")
URL = re.compile(r"https?://(\[[0-9A-Fa-f:.]{2,45}\]|[A-Za-z0-9.-]{1,253})(?::[0-9]{1,5})?(/[A-Za-z0-9._~/-]{0,200})?")
AGENTS = ("claude", "codex", "opencode")
# (kind, label, the port it listens on by default): llm.py's KINDS, in the order the wizard offers them.
KINDS = (("lmstudio", N_("LM Studio"), 1234), ("ollama", N_("Ollama"), 11434), ("llamacpp", N_("llama.cpp"), 8080),
         ("vllm", N_("vLLM"), 8000), ("openai-compatible", N_("OpenAI-compatible API"), None))
LIST_SECONDS, ADD_SECONDS, CHECK_SECONDS, MODELS_SECONDS = 60, 90, 150, 120


def kind_label(kind):
    return tr(next((label for key, label, _ in KINDS if key == kind), kind)) if kind else ""


def default_port(kind, sai=None):
    """The port KIND listens on by default (sandbox-ai's own table when it loads, else the one above), or None."""
    try:
        port = sai.module("llm").KINDS[kind]["port"] if sai is not None else None
        if isinstance(port, int) and not isinstance(port, bool) and 0 < port < 65536:
            return port
    except Exception:  # noqa: BLE001 - an older or broken sandbox-ai: the table above
        pass
    return next((port for key, _, port in KINDS if key == kind), None)


def default_url(kind, sai=None):
    """http://127.0.0.1:PORT for KIND (this machine, its usual port); "" for a kind without one."""
    port = default_port(kind, sai)
    return f"http://127.0.0.1:{port}" if port else ""


def free_name(base, taken):
    """BASE, or BASE-2, BASE-3 ... when an endpoint of that name exists already."""
    taken = set(taken or ())
    base = base if NAME.fullmatch(base or "") else "llm"
    if base not in taken:
        return base
    return next(f"{base}-{n}" for n in range(2, 1000) if f"{base}-{n}" not in taken)


def valid_name(name):
    """NAME if it may go back to sandbox-ai as an endpoint's name (never one that reads as an option), else None."""
    return name if isinstance(name, str) and not name.startswith("-") and NAME.fullmatch(name) else None


def valid_model(model):
    return model if isinstance(model, str) and not model.startswith("-") and MODEL.fullmatch(model) else None


# -- the commands --

def add_argv(fields):
    """The arguments of `sandbox-ai llm add` for FIELDS ({name, kind, url, secret, vpn, anthropic}), each value
    checked as sandbox-ai llm checks it; ValueError says what is wrong. Remote hosts, Tailscale and LAN addresses
    are allowed as the CLI allows them (it refuses plain http to a public address itself)."""
    name = valid_name(fields.get("name"))
    if name is None:
        raise ValueError(tr("A server's name is 1-32 letters, digits, '_' or '-', not starting with '-'."))
    kind = fields.get("kind")
    if kind not in [key for key, _, _ in KINDS]:
        raise ValueError(tr("Choose the kind of server."))
    url = (fields.get("url") or "").strip()
    if not URL.fullmatch(url):
        raise ValueError(tr("The URL is http(s)://HOST[:PORT][/PREFIX], without user, query or fragment."))
    args = ["llm", "add", name, "--kind", kind, "--url", url]
    secret = (fields.get("secret") or "").strip()
    if secret:
        if not SECRET.fullmatch(secret):
            raise ValueError(tr("A secret's name is letters, digits and '_', not starting with a digit."))
        args += ["--api-key-secret", secret]
    vpn = (fields.get("vpn") or "").strip()
    if vpn:
        if vpn.startswith("-") or not VPN.fullmatch(vpn):
            raise ValueError(tr("Not a VPN profile's name: {name}", name=printable(vpn)))
        args += ["--vpn", vpn]
    if fields.get("anthropic") and kind == "openai-compatible":
        args.append("--anthropic")
    return args


def check_argv(name, agent=None, model=None):
    """`sandbox-ai llm check NAME [--agent AGENT] [--model MODEL]`: reachability, TLS, the key, the models and the
    API AGENT needs."""
    if valid_name(name) is None:
        raise ValueError(tr("Not a server's name: {name}", name=printable(name)))
    args = ["llm", "check", name]
    if agent in AGENTS:
        args += ["--agent", agent]
    if model:
        if valid_model(model) is None:
            raise ValueError(tr("Not a model name: {model}", model=printable(model)))
        args += ["--model", model]
    return args


def models_argv(name):
    if valid_name(name) is None:
        raise ValueError(tr("Not a server's name: {name}", name=printable(name)))
    return ["llm", "models", name, "--json"]


def message(status, out, err):
    """What a failed command said (its error output, else its output), printable, its end kept."""
    text = (err or "").strip() or (out or "").strip()
    return "\n".join(printable(line) for line in text[-1500:].splitlines()) or \
        tr("exit status {status}", status=status)


def parse_endpoints(out):
    """([endpoint], [note]) of `sandbox-ai llm list --json`; an endpoint the CLI found unusable is said in a line of
    its own before the JSON (those lines are the notes). ValueError: no list in the answer."""
    text, notes = out or "", []
    try:
        data = json.loads(text)
    except ValueError:
        lines = text.splitlines()
        start = next((i for i, line in enumerate(lines) if line.startswith("[")), None)
        if start is None:
            raise ValueError(printable(text.strip()[-500:]) or tr("no answer")) from None
        notes = [printable(line) for line in lines[:start] if line.strip()]
        try:
            data = json.loads("\n".join(lines[start:]))
        except ValueError:
            raise ValueError(printable(text.strip()[-500:])) from None
    if not isinstance(data, list):
        raise ValueError(tr("not a list of servers"))
    return [e for e in data if isinstance(e, dict) and valid_name(e.get("name"))], notes


def list_endpoints(sai):
    """([endpoint], [note]) of the servers registered on this machine (blocking); ValueError with the CLI's message."""
    status, out, err = sai.call("llm", "list", "--json", timeout=LIST_SECONDS)
    if status != 0:
        raise ValueError(message(status, out, err))
    return parse_endpoints(out)


def add_and_check(sai, fields, agent=None):
    """`sandbox-ai llm add` with FIELDS, then (when it was added) `sandbox-ai llm check NAME --agent AGENT`
    (blocking). {"name", "added": (status, out, err), "checked": (status, out, err) or None}."""
    args = add_argv(fields)
    added = sai.call(*args, timeout=ADD_SECONDS)
    checked = sai.call(*check_argv(args[2], agent), timeout=CHECK_SECONDS) if added[0] == 0 else None
    return {"name": args[2], "added": added, "checked": checked}


def parse_models(out):
    """[{name, size, loaded, details}] of `sandbox-ai llm models --json`, each checked (a server's answer is
    untrusted); ValueError: not a list."""
    try:
        data = json.loads(out or "")
    except ValueError:
        raise ValueError(printable((out or "").strip()[-500:]) or tr("no answer")) from None
    if not isinstance(data, list):
        raise ValueError(tr("not a list of models"))
    found = []
    for entry in data:
        name = entry.get("name") if isinstance(entry, dict) else None
        if valid_model(name) is None:
            continue
        size, loaded, details = entry.get("size"), entry.get("loaded"), entry.get("details")
        found.append({"name": name,
                      "size": size if isinstance(size, int) and not isinstance(size, bool) and size >= 0 else None,
                      "loaded": loaded if isinstance(loaded, bool) else None,
                      "details": printable(details)[:80] if isinstance(details, str) else ""})
    return found


def list_models(sai, name):
    """The models server NAME offers now (blocking); ValueError with the CLI's message (e.g. the server is down)."""
    status, out, err = sai.call(*models_argv(name), timeout=MODELS_SECONDS)
    if status != 0:
        raise ValueError(message(status, out, err))
    return parse_models(out)


# -- which agent can use which server --

def usable_by(endpoint, agent):
    """Whether AGENT can use ENDPOINT (as `llm list --json` says in "agents"; Codex and OpenCode any server)."""
    agents = endpoint.get("agents")
    if isinstance(agents, list):
        return agent in agents
    return agent != "claude" or endpoint.get("kind") in ("ollama", "lmstudio", "llamacpp", "vllm") or \
        bool(endpoint.get("anthropic"))


def agent_hint(endpoint, agent, sai=None):
    """Why AGENT cannot use ENDPOINT (sandbox-ai's own words when its llm module loads), or ""."""
    if usable_by(endpoint, agent):
        return ""
    try:
        problem = sai.module("llm").agent_problem(endpoint, agent) if sai is not None else None
        if problem:
            return printable(problem)
    except Exception:  # noqa: BLE001 - an entry the module cannot read: the short form
        pass
    if agent == "claude":
        return tr("Claude Code needs a server with the Anthropic Messages API (Ollama, LM Studio, llama.cpp, vLLM, or "
                  "an OpenAI-compatible one added with --anthropic); Codex and OpenCode can use this one.")
    return tr("{agent} cannot use this server.", agent=agent)


def endpoint_row(endpoint):
    """[name, kind, URL, agents] as the server list shows them."""
    agents = endpoint.get("agents")
    return [printable(endpoint.get("name", ""))[:64], kind_label(endpoint.get("kind")) or "?",
            printable(str(endpoint.get("url") or ""))[:300],
            ", ".join(a for a in agents if isinstance(a, str)) if isinstance(agents, list) else "?"]


def endpoint_detail(endpoint):
    """The kind, URL and VPN of ENDPOINT in one line (a tooltip)."""
    return " · ".join(printable(str(endpoint.get(k))) for k in ("kind", "url", "vpn") if endpoint.get(k))


# -- what goes on the board --

def llm_value(name, model=""):
    """--llm's value NAME[:MODEL] (ValueError for a name or model that cannot be passed on)."""
    if valid_name(name) is None:
        raise ValueError(tr("Not a server's name: {name}", name=printable(name)))
    if model and valid_model(model) is None:
        raise ValueError(tr("Not a model name: {model}", model=printable(model)))
    return f"{name}:{model}" if model else name


def parse_value(value):
    """(name, model) of --llm NAME[:MODEL] (a model may hold ':' itself: qwen2.5-coder:7b); ("", "") if invalid."""
    name, _, model = str(value or "").partition(":")
    if valid_name(name) is None or (model and valid_model(model) is None):
        return "", ""
    return name, model


def chosen_model(endpoint, model, listed=()):
    """The model for --llm: the one chosen or typed; "" when the server's default (llm add --model) will do; the
    only one listed; ValueError when a start would not know which (the launcher asks for NAME:MODEL then)."""
    model = (model or "").strip()
    if model:
        if valid_model(model) is None:
            raise ValueError(tr("Not a model name: {model}", model=printable(model)))
        return model
    if (endpoint or {}).get("model"):
        return ""
    names = [m["name"] if isinstance(m, dict) else m for m in listed or (endpoint or {}).get("models") or []]
    names = [n for n in names if isinstance(n, str) and valid_model(n)]
    if len(names) == 1:
        return names[0]
    raise ValueError(tr("Choose or type a model: {name} has no default model, so a start needs one.",
                        name=printable((endpoint or {}).get("name", ""))))


def options(choice):
    """The launcher options of CHOICE ({name, model, only}, or None for the agent's own provider)."""
    if not choice:
        return []
    return ["--llm", llm_value(choice["name"], choice.get("model") or "")] + \
        (["--no-provider-defaults"] if choice.get("only") else [])


def apply(board, choice):
    """Put CHOICE on BOARD (a board.Board): the LLM server node with --llm NAME[:MODEL] (for whichever agent the
    board has), --no-provider-defaults on the network node as "only this server" says, and the agent node's own
    model removed (the model is the server's: the launcher refuses --model with --llm). None leaves the board."""
    if not choice:
        return board
    value = llm_value(choice["name"], choice.get("model") or "")
    node = board.first("llm") or board.add("llm")
    node.props["--llm"] = value
    agent = board.first("agent")
    if agent is not None:
        if agent.id not in [a for a, b in board.edges if b == node.id]:
            board.connect(agent.id, node.id)
        agent.props.pop("model", None)
    network = board.first("network") or board.add("network")
    if choice.get("only"):
        network.props["--no-provider-defaults"] = True
    else:
        network.props.pop("--no-provider-defaults", None)
    return board


def from_board(board):
    """The choice ({name, model, only}) a board holds already (an existing project's --llm), or None."""
    node = board.first("llm")
    if node is None or not board.connected(node):
        return None
    name, model = parse_value(node.props.get("--llm"))
    if not name:
        return None
    network = board.first("network")
    return {"name": name, "model": model, "only": bool(network and network.props.get("--no-provider-defaults"))}


def prefilled(preset, choice):
    """{marker: NAME[:MODEL]} for the <llm:...> placeholders of PRESET from the wizard's CHOICE ({} without one)."""
    if not choice or preset is None:
        return {}
    value = llm_value(choice["name"], choice.get("model") or "")
    return {p.marker: value for p in preset.placeholders if p.kind == "llm"}


def needs_form(preset, values):
    """Whether PRESET leaves something to ask for once VALUES ({marker: value}) are filled in."""
    return any(p.marker not in values for p in getattr(preset, "placeholders", ()) or ())
