"""Every preset of the gallery - the app's own (presets.PRESETS) and sandbox-ai's (sandbox-ai presets, as the gallery
converts them) - with every agent it is for: built into a board with its placeholders filled in (VPN profiles of every
kind, Tor included, and LLM servers of every kind registered for real with sandbox-ai's own commands in a private
configuration), its argv dry-run through the launcher. Claude Code with a server without the Anthropic Messages API is
refused by the launcher, and the Model choice of the launch page and the board (wizard_llm) gives --llm and drops
--model. Nothing is contacted: profiles and endpoints are only written."""
import base64
import os

import pytest

from sandbox_ai_app import backend, board as B, presets as PR, wizard_llm as WL

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")
AGENTS = ("claude", "codex", "opencode")
WG_PEER = "xTIBA5rboUvnH4htodjb6e697QjLERt1NAB4mZqp8Dg="
SERVERS = (("ollama", "ollama", "http://127.0.0.1:11434", "qwen3-coder:30b"),
           ("lmstudio", "lmstudio", "http://127.0.0.1:1234", "qwen/qwen3-coder-30b"),
           ("llamacpp", "llamacpp", "http://127.0.0.1:8080", "qwen3-coder"),
           ("vllm", "vllm", "http://192.168.1.20:8000", "Qwen/Qwen3-Coder-30B"),
           ("api", "openai-compatible", "http://192.168.1.21:4000", "qwen3"))
ANTHROPIC = {"ollama", "lmstudio", "llamacpp", "vllm", "gateway", "gpu"}


@pytest.fixture(scope="module")
def registered(tmp_path_factory):
    """A sandbox-ai configuration of its own with VPN profiles (WireGuard, Tailscale, Tor) and LLM servers of every
    kind (a raw OpenAI-compatible API, one with the Anthropic API, a vLLM behind the Tailscale profile)."""
    root = tmp_path_factory.mktemp("presets-all")
    patch = pytest.MonkeyPatch()
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"), ("XDG_STATE_HOME", "state")):
        patch.setenv(name, str(root / sub))
    patch.setenv("SANDBOX_AI_SECRETS", "file")
    sai = backend.SandboxAI()
    conf = root / "wg.conf"
    conf.write_text(f"[Interface]\nPrivateKey = {base64.b64encode(os.urandom(32)).decode()}\nAddress = 10.66.0.2/32\n"
                    f"DNS = 10.66.0.1\n\n[Peer]\nPublicKey = {WG_PEER}\nEndpoint = 198.51.100.7:51820\n"
                    "AllowedIPs = 0.0.0.0/0\n")
    conf.chmod(0o600)
    commands = [("vpn", "add", "home", "--wireguard", str(conf)), ("vpn", "add", "ts", "--tailscale"),
                ("vpn", "add", "anon", "--tor", "--socks"),
                ("llm", "add", "gateway", "--kind", "openai-compatible", "--url", "https://192.168.1.22:4000/litellm",
                 "--anthropic", "--model", "qwen3"),
                ("llm", "add", "gpu", "--kind", "vllm", "--url", "http://100.100.1.5:8000", "--vpn", "ts", "--model",
                 "Qwen/Qwen3-Coder")]
    commands += [("llm", "add", name, "--kind", kind, "--url", url, "--model", model) for name, kind, url, model in SERVERS]
    for command in commands:
        status, out, err = sai.call(*command)
        assert status == 0, out + err
    yield sai, root
    patch.undo()


def every_preset():
    sai = backend.SandboxAI()
    return list(PR.PRESETS) + PR.sandbox_ai_presets(sai)[0]


PRESETS = every_preset() if os.environ.get("SANDBOX_AI_HOME") or backend.shutil.which("sandbox-ai") else []


def value_for(preset, placeholder, root, agent):
    """What a user fills in for PLACEHOLDER of PRESET with AGENT ("" leaves an optional one out)."""
    kind, name = B.PLACEHOLDER.fullmatch(placeholder.marker).groups()
    if placeholder.default and kind != "llm":  # sandbox-ai's presets: what the form offers first (a port)
        return placeholder.default
    if kind == "folder":
        folder = root / f"value-{name}"
        folder.mkdir(exist_ok=True)
        return str(folder)
    if kind == "vpn":
        return {"tor": "anon", "tailnet": "ts", "fetch": "ts"}.get(name, "home")
    if kind == "llm":
        server = {"remote": "gpu", "api": "api"}.get(name, "ollama")
        if preset.id in ("sai-local-api",):
            server = "api"
        return "gateway" if agent == "claude" and server not in ANTHROPIC else server
    if kind == "mcp":
        return "" if placeholder.optional else "demo"
    if kind == "gpu":
        return "dri"
    return "" if placeholder.optional else "a task for the test"


def registered_servers(root, board):
    """The MCP servers BOARD names that a user registers (sandbox-ai mcp add, e.g. from the private catalog), not the
    project's own: defined in this configuration as small stand-ins, as `mcp add` would have (the launcher refuses
    a start whose --mcp servers are unknown, dry runs too)."""
    import json as _json
    path = root / "config" / "sandbox-ai" / "mcp.json"
    document = _json.loads(path.read_text()) if path.is_file() else {}
    own = set(B.project_servers(board))
    for node in board.included():
        name = node.props.get("name") if node.kind == "mcp" else None
        if name and name not in own and name not in document and name != "demo":
            document[name] = {"command": ["python3", "-I", "-c", "pass"], "options": ["--offline"],
                              "description": "stand-in of a registered server (tests)"}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json.dumps(document))


@pytest.mark.parametrize("agent", AGENTS)
@pytest.mark.parametrize("preset", PRESETS, ids=lambda p: p.id)
def test_every_preset_with_every_agent_passes_a_dry_run(registered, preset, agent):
    sai, root = registered
    project = root / f"project-{preset.id}-{agent}"
    project.mkdir()
    values = {p.marker: value_for(preset, p, root, agent) for p in preset.placeholders}
    board = PR.build(sai, preset, project, agent=agent, values=values)
    assert B.placeholders(board) == []
    used = B.values(board)[0]
    assert used == (preset.agent or (agent if not preset.agents or agent in preset.agents else preset.agents[0]))
    registered_servers(root, board)
    plan, message = sai.dry_run(used, B.check_argv(sai, board))
    if plan is None and "--gpu dri: no /dev/dri" in message:  # this machine has no GPU: only that may be refused
        without = B.Board.from_state(board.state())
        without.first("devices").props.pop("--gpu")
        plan, message = sai.dry_run(used, B.check_argv(sai, without))
    assert plan is not None, message
    assert plan["agent"] == used
    llm = next((v for m, v in values.items() if m.startswith("<llm:") and v), None)
    if llm:
        assert plan["llm"]["name"] == llm.split(":", 1)[0]
    tor = values.get("<vpn:tor>")
    if tor:
        assert plan["vpn"]["profile"] == tor and plan["vpn"]["type"] == "tor"


def test_the_tor_and_raw_api_presets_are_in_the_gallery():
    tor, api = PR.BUILTIN["tor"], PR.BUILTIN["local-api"]
    assert tor.category == "privacy" and "--vpn" in tor.options and [p.marker for p in tor.placeholders] == ["<vpn:tor>"]
    assert api.category == "llm" and api.options == ("--llm", "<llm:api>", "--no-provider-defaults")


@pytest.mark.parametrize("server", ["api", "api:qwen2.5-coder:7b"])
def test_claude_with_a_raw_api_is_refused_and_says_why(registered, server):
    sai, root = registered
    project = root / f"raw-{server.replace(':', '-')}"
    project.mkdir(exist_ok=True)
    board = PR.build(sai, PR.BUILTIN["local-api"], project, agent="claude", values={"<llm:api>": server})
    plan, message = sai.dry_run("claude", B.check_argv(sai, board))
    assert plan is None and "Anthropic Messages API" in message


# -- the Model choice beside the agent (launch page and board, without Qt) --

def test_the_model_choice_lists_the_provider_and_every_server_with_its_models(registered):
    sai, _ = registered
    endpoints = sai.llm_endpoints()
    choices = WL.model_choices(endpoints + [{"name": "listed", "kind": "ollama", "models": ["a:7b", "b"]}], "claude",
                               sai)
    assert choices[0][1] == "" and "provider" in choices[0][0]
    values = [value for _, value, _ in choices]
    assert {"api", "gateway", "gpu", "ollama", "listed:a:7b", "listed:b"} <= set(values)
    assert "listed" not in values, "several models and no default: the launcher needs NAME:MODEL"
    hints = {value: hint for _, value, hint in choices}
    assert "Anthropic Messages API" in hints["api"] and hints["gateway"] == "" and hints["ollama"] == ""
    assert all(hint == "" for _, _, hint in WL.model_choices(endpoints, "codex", sai))


@pytest.mark.parametrize("agent", AGENTS)
@pytest.mark.parametrize("value,only", [("ollama", True), ("vllm:Qwen/Qwen3-Coder-30B", False), ("gpu", True),
                                        ("api:qwen2.5-coder:7b", True), ("gateway", False)])
def test_a_model_server_chosen_on_the_launch_page_gives_llm_and_no_model(registered, agent, value, only):
    sai, root = registered
    options = sai.options()
    project = root / "launch-page"
    project.mkdir(exist_ok=True)
    values, _ = backend.values_from_argv(options, ["--model", "sonnet", "--workspace", str(project)],
                                         sai.launcher.canonical_options)
    chosen = WL.form_values(values, WL.choice_of(value, only))
    argv = backend.argv_from_values(options, chosen)
    assert "--model" not in argv and argv[argv.index("--llm") + 1] == value
    assert ("--no-provider-defaults" in argv) == only
    assert WL.from_values(chosen) == WL.choice_of(value, only)
    plan, message = sai.dry_run(agent, argv)
    if agent == "claude" and value.split(":")[0] not in ANTHROPIC:
        assert plan is None and "Anthropic Messages API" in message
        return
    assert plan is not None, message
    assert plan["llm"]["name"] == value.split(":")[0]
    # back to the agent's provider: neither --llm nor --no-provider-defaults
    again = backend.argv_from_values(options, WL.form_values(chosen, None))
    assert "--llm" not in again and "--no-provider-defaults" not in again and WL.from_values(
        WL.form_values(chosen, None)) is None


@pytest.mark.parametrize("agent", AGENTS)
def test_a_model_server_chosen_on_the_board_gives_llm_and_no_model(registered, agent):
    sai, root = registered
    project = root / "board"
    project.mkdir(exist_ok=True)
    board = B.from_argv(sai, agent, ["--model", "some-model"], [], [str(project)])
    WL.apply(board, WL.choice_of("ollama:qwen3-coder:30b", True))
    argv = B.launcher_argv(sai, board)
    assert "--model" not in argv and "--llm" in argv and "--no-provider-defaults" in argv
    plan, message = sai.dry_run(agent, argv)
    assert plan is not None, message
    WL.clear(board)
    assert "--llm" not in B.launcher_argv(sai, board)


@pytest.mark.parametrize("agent", AGENTS)
@pytest.mark.parametrize("server", [entry[0] for entry in SERVERS] + ["gateway"])
@pytest.mark.parametrize("ident", ["local-llm", "mixed-llm", "remote-llm", "local-api"])
def test_the_llm_presets_with_every_kind_of_server_and_model(registered, ident, server, agent):
    """The gallery's LLM presets with a server of each kind (Ollama, LM Studio, llama.cpp, vLLM, a raw
    OpenAI-compatible API, one with the Anthropic API), as NAME and as NAME:MODEL: Codex and OpenCode with any, Claude
    Code only with one that has the Anthropic Messages API."""
    sai, root = registered
    preset = PR.BUILTIN[ident]
    marker = next(p.marker for p in preset.placeholders if p.kind == "llm")
    for value in (server, f"{server}:qwen2.5-coder:7b", f"{server}:org/Model-GGUF"):
        project = root / f"llm-{ident}-{agent}-{value.replace(':', '-').replace('/', '-')}"
        project.mkdir()
        values = {marker: value, **({"<vpn:tailnet>": "ts"} if ident == "remote-llm" else {})}
        board = PR.build(sai, preset, project, agent=agent, values=values)
        used = B.values(board)[0]
        argv = B.launcher_argv(sai, board)
        assert argv[argv.index("--llm") + 1] == value and "--model" not in argv
        plan, message = sai.dry_run(used, argv)
        if used == "claude" and server not in ANTHROPIC:
            assert plan is None and "Anthropic Messages API" in message
            continue
        assert plan is not None, message
        assert plan["llm"]["name"] == server
