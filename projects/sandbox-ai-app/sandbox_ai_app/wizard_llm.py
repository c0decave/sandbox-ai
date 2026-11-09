"""An own model server for the wizard (sandbox-ai llm, the "raw API" of LM Studio, Ollama, llama.cpp, vLLM or any
OpenAI-compatible server): what the wizard's "Model server" step and the presets' <llm:...> placeholders need, without
Qt (tested without a display).

- the registered servers (`sandbox-ai llm list --json`, an unusable entry reported in a line before the JSON),
- a new one (`sandbox-ai llm add NAME --kind KIND --url URL [--api-key-secret NAME] [--vpn PROFILE] [--anthropic]`),
  then checked for the chosen agent (`sandbox-ai llm check NAME --agent AGENT`),
- its models (`sandbox-ai llm models NAME --json`: [{name, size, loaded, details}] and, when the server gives them,
  type, arch, parameters, quantization and context), as the columns of a table (model_columns, model_cells),
- the LLM servers page's dialog: a name checked as it is typed (name_problem, suggest_name), every option of `llm add`
  (server_argv), an existing server's settings from `llm show NAME --json` kept by a --replace (fields_of, changes;
  the CA it has stays without --ca), and the models of a server not registered yet (`sandbox-ai wizard models --kind
  --url --json`: probe_argv),
- the Model choice beside the agent on the launch page and the board's agent node (model_choices, form_values,
  apply): the agent's provider, or a registered server as NAME or NAME:MODEL,
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
import unicodedata

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
        raise ValueError(name_problem(fields.get("name")) or tr("Not a server's name: {name}",
                                                                 name=printable(fields.get("name"))))
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
            data = json.JSONDecoder().raw_decode("\n".join(lines[start:]))[0]  # notes after it: left out
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


def add_and_check(sai, fields, agent=None, keep_key=None):
    """`sandbox-ai llm add` with FIELDS, then (when it was added) KEEP_KEY() - the page's `sandbox-ai secrets set`
    of a typed key, its value on standard input, asked for first when that secret exists - and `sandbox-ai llm
    check NAME --agent AGENT` (blocking). A server refused keeps no key. {"name", "added": (status, out, err),
    "kept": (status, out, err) or None, "checked": (status, out, err) or None}."""
    args = add_argv(fields)
    added = sai.call(*args, timeout=ADD_SECONDS)
    kept = checked = None
    if added[0] == 0:
        kept = keep_key() if keep_key is not None else None
        checked = sai.call(*check_argv(args[2], agent), timeout=CHECK_SECONDS)
    return {"name": args[2], "added": added, "kept": kept, "checked": checked}


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
        model = {"name": name,
                 "size": size if isinstance(size, int) and not isinstance(size, bool) and size >= 0 else None,
                 "loaded": loaded if isinstance(loaded, bool) else None,
                 "details": printable(details)[:80] if isinstance(details, str) else ""}
        # The further columns (LM Studio, Ollama give them; llm.py's clean_models): only when known.
        for key, limit in (("type", 20), ("arch", 40), ("parameters", 20), ("quantization", 20)):
            value = entry.get(key)
            if isinstance(value, str) and value.strip():
                model[key] = printable(value.strip())[:limit]
        context = entry.get("context")
        if isinstance(context, int) and not isinstance(context, bool) and 0 < context < 1 << 31:
            model["context"] = context
        found.append(model)
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


def loopback(endpoint):
    """Whether ENDPOINT is on this machine's loopback (127.0.0.1, localhost, [::1]): only the Podman backend reaches
    it (the launcher opens it with --allow-host-port), Bubblewrap does not."""
    match = URL.fullmatch(str((endpoint or {}).get("url") or ""))
    host = match[1].strip("[]").lower() if match else ""
    return host in ("localhost", "::1") or host.startswith("127.")


def bare_name_works(endpoint):
    """Whether --llm NAME alone (no :MODEL) starts with ENDPOINT: it has a default model (llm add --model), or it
    listed at most one model when it was last checked (none listed: not known, the launcher tells). With several
    models and no default the launcher refuses NAME and asks for NAME:MODEL."""
    models = [m for m in endpoint.get("models") or [] if valid_model(m)] if isinstance(endpoint.get("models"), list) \
        else []
    return bool(endpoint.get("model")) or len(models) <= 1


def placeholder_choices(endpoint):
    """What an <llm:...> placeholder may take for ENDPOINT: its name when that alone starts (bare_name_works: the
    server's default or only model), then NAME:MODEL for each model it listed when it was last checked."""
    name = valid_name(endpoint.get("name"))
    if name is None:
        return []
    models = [m for m in endpoint.get("models") or [] if valid_model(m)] if isinstance(endpoint.get("models"), list) \
        else []
    return ([name] if bare_name_works(endpoint) else []) + [f"{name}:{model}" for model in models[:30]]


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


def clear(board):
    """Take an own server off BOARD (the agent's provider again): the LLM server node and --no-provider-defaults."""
    node = board.first("llm")
    if node is not None:
        board.remove(node.id)
    network = board.first("network")
    if network is not None:
        network.props.pop("--no-provider-defaults", None)
    return board


def uses_server(board):
    """Whether BOARD starts with --llm (a value or a placeholder still to fill in): no --model then."""
    node = board.first("llm")
    return node is not None and board.connected(node) and bool(str(node.props.get("--llm") or "").strip())


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


# -- the "Model" choice beside the agent (the launch page, the board's agent node) --

def model_choices(endpoints, agent, sai=None):
    """[(label, value, hint)] for the Model choice beside AGENT: the agent's own provider first (value ""), then each
    registered server as NAME (when that alone starts: its default or only model) and as NAME:MODEL for each model
    it listed. HINT says why AGENT cannot use that server ("" when it can): the choice is shown, not offered."""
    found = [(tr("the agent's provider (cloud)"), "", "")]
    for endpoint in endpoints or []:
        values = placeholder_choices(endpoint)
        if not values:
            continue
        hint = agent_hint(endpoint, agent, sai)
        kind = kind_label(endpoint.get("kind"))
        for value in values:
            found.append((f"{value} ({kind})" if value == values[0] and kind else value, value, hint))
    return found


def choice_of(value, only=False):
    """The choice ({name, model, only}) of a Model value NAME[:MODEL] ("" or an invalid one: None, the agent's
    provider)."""
    name, model = parse_value(value)
    return {"name": name, "model": model, "only": bool(only)} if name else None


def form_values(values, choice):
    """The launch page's form VALUES ({option: value}) with CHOICE: --llm NAME[:MODEL], --no-provider-defaults as
    "only this server" says, --model emptied (the launcher refuses it with --llm: the model is the server's); None
    (the agent's provider) empties --llm and --no-provider-defaults."""
    values = dict(values)
    if choice:
        values["--llm"] = llm_value(choice["name"], choice.get("model") or "")
        values["--no-provider-defaults"] = bool(choice.get("only"))
        if "--model" in values:
            values["--model"] = ""
    else:
        values["--llm"] = ""
        values["--no-provider-defaults"] = False
    return values


def from_values(values):
    """The choice the form VALUES hold (--llm and --no-provider-defaults), or None."""
    return choice_of(str((values or {}).get("--llm") or "").strip(), (values or {}).get("--no-provider-defaults"))


def prefilled(preset, choice, endpoint=None):
    """{marker: value} for the <llm:...> placeholders of PRESET from the wizard's CHOICE (NAME[:MODEL]), and for a
    VPN placeholder that follows one of them the VPN profile ENDPOINT (the chosen server) is reached through
    ({} without a choice)."""
    if not choice or preset is None:
        return {}
    value = llm_value(choice["name"], choice.get("model") or "")
    found = {p.marker: value for p in preset.placeholders if p.kind == "llm"}
    vpn = (endpoint or {}).get("vpn")
    if isinstance(vpn, str) and VPN.fullmatch(vpn):
        found.update({p.marker: vpn for p in preset.placeholders if p.follows in found})
    return found


def needs_form(preset, values):
    """Whether PRESET leaves something to ask for once VALUES ({marker: value}) are filled in."""
    return any(p.marker not in values for p in getattr(preset, "placeholders", ()) or ())


# -- the LLM servers page's dialog: a name as it is typed, every option of llm add, an edit kept by --replace --

NAME_LIMIT = 32
NAME_CHAR = re.compile(r"[A-Za-z0-9_-]")
HEADER = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,63}")  # llm.py's HEADER
RESERVED_HEADERS = {"host", "content-length", "transfer-encoding", "connection", "upgrade", "te", "trailer"}
NAME_EXAMPLE = "lmstudio"  # the same in every language


def char_name(char):
    """CHAR as a message names it: '.', a space, or U+0009 for one that does not print."""
    if char == " ":
        return tr("a space")
    return f"'{char}'" if char.isprintable() else f"U+{ord(char):04X}"


def name_problem(name):
    """Why NAME cannot be a server's name, naming what is wrong (the first character that cannot be in it, a first
    character that may not come first, the length), or None. As sandbox-ai llm checks it: 1-32 of A-Z, a-z, 0-9,
    '_' and '-', the first a letter or a digit (so a name may start with digits: 4090-box)."""
    name = name if isinstance(name, str) else ""
    if not name:
        return tr("Give the server a name: 1-32 letters, digits, '_' or '-', e.g. {example}.", example=NAME_EXAMPLE)
    bad = next((char for char in name if not NAME_CHAR.fullmatch(char)), None)
    if bad is not None:
        return tr("Not allowed in a server's name: {char}. A name holds only the letters A-Z and a-z, digits, '_' "
                  "and '-' (a digit may come first).", char=char_name(bad))
    if name[0] in "-_":
        return tr("A server's name starts with a letter or a digit, not '{char}'.", char=name[0])
    if len(name) > NAME_LIMIT:
        return tr("A server's name is at most 32 characters; this one has {count}.", count=len(name))
    return None


def suggest_name(name):
    """A valid name close to NAME (an accented letter without its accent, each other character that cannot be in a
    name as '-', no '-' or '_' first, at most 32 characters), or None when there is none or NAME is valid already."""
    if not isinstance(name, str) or valid_name(name):
        return None
    out = []
    for char in name:
        plain = unicodedata.normalize("NFKD", char).encode("ascii", "ignore").decode()
        out.append(plain if plain and all(NAME_CHAR.fullmatch(c) for c in plain) else "-")
    text = re.sub(r"-{2,}", "-", "".join(out)).lstrip("-_")[:NAME_LIMIT].rstrip("-")
    return text if valid_name(text) and text != name else None


def server_argv(fields):
    """The arguments of `sandbox-ai llm add` for FIELDS ({name, kind, url, model, vpn, secret, anthropic, headers,
    ca, insecure, replace}: the LLM servers page's dialog), each value checked as sandbox-ai llm checks it and none
    that reads as an option; ValueError says what is wrong. Keys are never among them."""
    name = valid_name(fields.get("name"))
    if name is None:
        raise ValueError(name_problem(fields.get("name")) or tr("Not a server's name: {name}",
                                                                 name=printable(fields.get("name"))))
    kind = fields.get("kind")
    if kind not in [key for key, _, _ in KINDS]:
        raise ValueError(tr("Choose the kind of server."))
    url = fields.get("url") or ""
    if not URL.fullmatch(url):
        raise ValueError(tr("The URL is http(s)://HOST[:PORT][/PREFIX], without user, query or fragment."))
    args = ["llm", "add", name, "--kind", kind, "--url", url]
    model = fields.get("model")
    if model:
        if valid_model(model) is None:
            raise ValueError(tr("Not a model name: {model}", model=printable(model)))
        args += ["--model", model]
    vpn = fields.get("vpn")
    if vpn:
        if vpn.startswith("-") or not VPN.fullmatch(vpn):
            raise ValueError(tr("Not a VPN profile's name: {name}", name=printable(vpn)))
        args += ["--vpn", vpn]
    secret = fields.get("secret")
    if secret:
        if not SECRET.fullmatch(secret):
            raise ValueError(tr("A secret's name is letters, digits and '_', not starting with a digit."))
        args += ["--api-key-secret", secret]
    if fields.get("anthropic") and kind == "openai-compatible":
        args.append("--anthropic")
    for pair in (fields.get("headers") or "").replace(",", " ").split():
        header, sep, secret = pair.partition("=")
        if not sep or not HEADER.fullmatch(header) or header.lower() in RESERVED_HEADERS or not SECRET.fullmatch(secret):
            raise ValueError(tr("A header from a secret is HEADER=SECRET (a header name, a secret's name): {value}",
                                value=printable(pair)))
        args.append(f"--header-secret={header}={secret}")
    ca = fields.get("ca")
    if ca:
        if not ca.startswith("/") or "\0" in ca or not ca.isprintable():
            raise ValueError(tr("The CA is a file with its absolute path."))
        args.append(f"--ca={ca}")
    elif fields.get("no_ca") and fields.get("replace"):
        args.append("--no-ca")  # the CA an edited server has is dropped (--replace keeps it otherwise)
    if fields.get("insecure"):
        args.append("--insecure-http")
    if fields.get("replace"):
        args.append("--replace")
    return args


def parse_show(out):
    """The endpoint of `sandbox-ai llm show NAME --json` (a dict with a valid name); ValueError otherwise."""
    try:
        data = json.loads(out or "")
    except ValueError:
        raise ValueError(printable((out or "").strip()[-500:]) or tr("no answer")) from None
    if not isinstance(data, dict) or valid_name(data.get("name")) is None:
        raise ValueError(tr("not an LLM server"))
    return data


def fields_of(view):
    """The dialog's FIELDS (see server_argv) of an endpoint as `llm show NAME --json` gives it, with replace on: what
    `llm add NAME ... --replace` needs to keep every setting it has. The CA is not among them: the store holds only a
    copy of it, which `llm add --replace` keeps by itself (without --ca; --no-ca drops it), so "has_ca" only says
    there is one, and a path typed into the dialog replaces it."""
    def text(key):
        value = view.get(key)
        return value if isinstance(value, str) else ""
    headers = view.get("headers") if isinstance(view.get("headers"), dict) else {}
    return {"name": text("name"), "kind": text("kind"), "url": text("url"), "model": text("model"),
            "vpn": text("vpn"), "secret": text("api_key_secret"),
            "anthropic": view.get("anthropic") is True and view.get("kind") == "openai-compatible",
            "headers": " ".join(f"{h}={s}" for h, s in sorted(headers.items()) if isinstance(h, str) and
                                isinstance(s, str)),
            "ca": "", "has_ca": bool(view.get("ca")), "insecure": view.get("insecure_http") is True,
            "replace": True}


FIELD_LABELS = (("kind", N_("Kind")), ("url", N_("URL")), ("model", N_("Default model")), ("vpn", N_("VPN profile")),
                ("anthropic", N_("Anthropic Messages API")), ("secret", N_("Key secret")),
                ("headers", N_("Headers from secrets")), ("ca", N_("CA certificate")),
                ("insecure", N_("Plain http outside the local networks")))


def changes(old, new):
    """["Label: old → new"] for each field of the dialog that NEW changes from OLD (both as fields_of gives them)."""
    def shown(value):
        if isinstance(value, bool):
            return tr("yes") if value else tr("no")
        return printable(str(value)) if value else tr("none")
    found = []
    for key, label in FIELD_LABELS:
        before, after = old.get(key), new.get(key)
        if key == "ca" and old.get("has_ca"):
            # The endpoint has a CA and `llm add --replace` keeps it: only a file named in the dialog replaces it.
            if (after or "").strip():
                found.append(f"{tr(label)}: {tr('the CA kept now')} \u2192 {printable(str(after))}")
            elif new.get("no_ca"):
                found.append(f"{tr(label)}: {tr('the CA kept now')} \u2192 {tr('none')}")
            continue
        if isinstance(before, bool) or isinstance(after, bool):
            before, after = bool(before), bool(after)
        else:
            before, after = (before or "").strip(), (after or "").strip()
        if before != after:
            found.append(f"{tr(label)}: {shown(before)} → {shown(after)}")
    return found


# -- a server's models as a table --

MODEL_COLUMNS = (("name", N_("Name")), ("loaded", N_("Loaded")), ("type", N_("Type")), ("parameters", N_("Params")),
                 ("quantization", N_("Quant")), ("context", N_("Context")), ("size", N_("Size")))


def size_text(value):
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return ""
    for unit, factor in (("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if value >= factor:
            return f"{value / factor:.1f} {unit}"
    return f"{value} B"


def model_cell(model, key):
    """(text, sort value) of column KEY for MODEL (parse_models' entry)."""
    value = model.get(key)
    if key == "loaded":
        text = {True: tr("yes"), False: tr("no")}.get(value, "") if isinstance(value, bool) else ""
        return text, {True: 0, False: 1}.get(value, 2) if isinstance(value, bool) else 2
    if key == "context":
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            return "", -1
        return (f"{value // 1024}k" if value >= 1024 else str(value)), value
    if key == "size":
        return size_text(value), value if isinstance(value, int) and not isinstance(value, bool) else -1
    text = printable(value) if isinstance(value, str) else ""
    return text, text.lower()


def model_columns(models):
    """The columns (key, title) a table of MODELS shows: the name, and each other column some model has a value in."""
    return [(key, title) for key, title in MODEL_COLUMNS
            if key == "name" or any(model_cell(m, key)[0] for m in models)]


def model_order(models):
    """MODELS loaded first, then by name (as `llm models --sort loaded` lists them)."""
    return sorted(models, key=lambda m: ({True: 0, False: 1}.get(m.get("loaded"), 2), str(m.get("name", ""))))


# -- the models of a server not registered yet: sandbox-ai wizard models --

# The kinds `sandbox-ai wizard models --kind` knows (wizard.py's SERVERS): an OpenAI-compatible server is not among
# them, so that one is reached with `llm check` once it is saved.
PROBE_KINDS = ("ollama", "lmstudio", "llamacpp", "vllm")
PROBE_SECONDS = 60


def probe_argv(kind, url):
    """`sandbox-ai wizard models --kind KIND --url URL --sort loaded --json` for a server at URL that is not
    registered anywhere: it lists the models, stores nothing and sends no key (https and a path prefix included).
    ValueError says why that command cannot ask this one (an OpenAI-compatible server is tested with `llm check`
    once it is saved)."""
    if kind not in PROBE_KINDS:
        raise ValueError(tr("An OpenAI-compatible server cannot be tested before it is saved."))
    url = (url or "").strip()
    if not URL.fullmatch(url):
        raise ValueError(tr("The URL is http(s)://HOST[:PORT][/PREFIX], without user, query or fragment."))
    return ["wizard", "models", "--kind", kind, "--url", url, "--sort", "loaded", "--json"]
