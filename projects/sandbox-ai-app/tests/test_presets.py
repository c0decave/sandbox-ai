"""The preset library (sandbox_ai_app/presets.py): every preset builds a board without errors but its
placeholders, lies out without overlaps, survives the project file, and - its placeholders filled in with VPN
profiles and LLM servers registered for real in a private sandbox-ai configuration - passes the launcher's own
dry run. The user's own presets: saved, exported, imported; secrets refused."""
import base64
import json
import os

import pytest

from sandbox_ai_app import backend, board as B, presets as PR

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")
NODE_W, NODE_H = 210.0, 66.0  # board_view's node size (no Qt here)


def key():
    return base64.b64encode(os.urandom(32)).decode()


@pytest.fixture(scope="module")
def registered(tmp_path_factory):
    """A sandbox-ai configuration of its own (XDG_CONFIG_HOME, XDG_DATA_HOME) with VPN profiles (WireGuard,
    Tailscale) and LLM servers (a local Ollama, a vLLM behind the Tailscale profile), added with sandbox-ai's own
    commands. Nothing is contacted: profiles and endpoints are only written."""
    root = tmp_path_factory.mktemp("registered")
    patch = pytest.MonkeyPatch()
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"), ("XDG_STATE_HOME", "state")):
        patch.setenv(name, str(root / sub))
    sai = backend.SandboxAI()
    conf = root / "wg.conf"
    conf.write_text(f"[Interface]\nPrivateKey = {key()}\nAddress = 10.66.0.2/32\nDNS = 10.66.0.1\n\n[Peer]\n"
                    f"PublicKey = {key()}\nEndpoint = 198.51.100.7:51820\nAllowedIPs = 0.0.0.0/0\n")
    for command in (("vpn", "add", "home", "--wireguard", str(conf)), ("vpn", "add", "ts", "--tailscale"),
                    ("llm", "add", "ollama", "--kind", "ollama", "--url", "http://127.0.0.1:11434", "--model", "qwen3"),
                    ("llm", "add", "gpu", "--kind", "vllm", "--url", "http://100.100.1.5:8000", "--vpn", "ts",
                     "--model", "Qwen/Qwen3-Coder")):
        status, out, err = sai.call(*command)
        assert status == 0, out + err
    (root / "samples").mkdir()
    (root / "logs").mkdir()
    yield sai, root
    patch.undo()


def values_for(root):
    return {"<vpn:wireguard>": "home", "<vpn:search>": "home", "<vpn:fetch>": "ts", "<vpn:tailnet>": "ts",
            "<llm:local>": "ollama", "<llm:remote>": "gpu", "<folder:samples>": str(root / "samples"),
            "<folder:logs>": str(root / "logs"), "<gpu:kind>": "dri", "<mcp:browser>": "demo",
            "<mcp:unpack>": "demo", "<mcp:scan>": "scan", "<text:variant>": ""}


def test_the_library():
    assert len(PR.PRESETS) >= 12 and len(PR.BUILTIN) == len(PR.PRESETS), "ids are unique"
    # (sandbox-ai's own presets have a category of their own: test_sai_presets.py)
    assert {p.category for p in PR.PRESETS} == {key for key, _ in PR.CATEGORIES} - {PR.SAI_CATEGORY}
    assert {p.difficulty for p in PR.PRESETS} == set(PR.DIFFICULTIES)
    for preset in PR.PRESETS:
        assert PR.ID.fullmatch(preset.id) and preset.title and preset.description
        for placeholder in preset.placeholders:
            assert B.PLACEHOLDER.fullmatch(placeholder.marker) and placeholder.label
    assert sum(1 for p in PR.PRESETS if p.servers or p.mcp) >= 4, "several with MCP servers"
    assert sum(1 for p in PR.PRESETS if "--vpn" in p.options or "<vpn:" in json.dumps(p.servers)) >= 4, "and VPNs"


@pytest.mark.parametrize("preset", PR.PRESETS, ids=lambda p: p.id)
def test_every_preset_builds_and_says_what_is_left_to_fill_in(preset, tmp_path):
    sai = backend.SandboxAI()
    board = PR.build(sai, preset, tmp_path)
    markers = [m for m, _ in B.placeholders(board)]
    assert sorted(markers) == sorted(p.marker for p in preset.placeholders), "declared and on the board alike"
    errors = [p for p in B.problems(sai, board) if p.level == "error"]
    assert [e.text for e in errors if not e.text.startswith("Fill in <")] == []
    assert len(errors) == len(markers) and all(e.node is not None for e in errors)
    assert B.values(board)[0] == (preset.agent or "claude")
    # laid out: on the grid, no two parts on top of each other
    assert all(n.x % 20 == 0 and n.y % 20 == 0 for n in board.nodes)
    for a in board.nodes:
        for b in board.nodes:
            if a.id < b.id:
                apart = abs(a.x - b.x) >= NODE_W + 10 or abs(a.y - b.y) >= NODE_H + 10
                assert apart, f"{a.kind} and {b.kind} overlap at ({a.x}, {a.y}) and ({b.x}, {b.y})"
    # the agent's inputs on its left, what it talks to on its right
    agent = board.first("agent")
    for source, target in board.edges:
        a, b = board.node(source), board.node(target)
        assert a.x < b.x, f"{a.kind} -> {b.kind} goes backwards"
    assert agent.x == 300.0


@pytest.mark.parametrize("preset", PR.PRESETS, ids=lambda p: p.id)
def test_every_preset_passes_the_launchers_checks_once_filled_in(registered, preset):
    sai, root = registered
    project = root / ("project-" + preset.id)
    project.mkdir()
    board = PR.build(sai, preset, project, values=values_for(root))
    assert B.placeholders(board) == []
    problems = [p.text for p in B.problems(sai, board) if p.level == "error"]
    assert problems == []
    agent, _, _ = B.values(board)
    plan, message = sai.dry_run(agent, B.launcher_argv(sai, board))
    if preset.id == "gpu" and plan is None:  # this machine has no GPU: only that may be refused
        assert message.splitlines()[-1].startswith("agent-sandbox: --gpu dri"), message
        without = B.Board.from_state(board.state())
        without.first("devices").props.pop("--gpu")
        plan, message = sai.dry_run(agent, B.launcher_argv(sai, without))
    assert plan is not None, message
    # the project file (and the layout next to it) bring back the same board
    document, main = B.project_document(sai, board)
    assert main == str(project)
    again = B.from_project(sai, json.loads(json.dumps(document)), project)
    assert B.to_argv(sai, again) == B.to_argv(sai, board)
    assert (again.first("approvals") or B.Node(0, "approvals")).props.get("rules") == preset.rules
    assert {n.props["name"]: n.props["definition"] for n in again.of_kind("mcp") if n.props.get("project")} == \
        {name: json.loads(json.dumps(d).replace("<vpn:search>", "home").replace("<vpn:fetch>", "ts"))
         for name, d in preset.servers}
    B.write_layout(project, board)
    again.place(B.read_layout(project))
    assert sorted((n.key(), n.x, n.y) for n in again.nodes) == sorted((n.key(), n.x, n.y) for n in board.nodes)
    assert B.Board.from_state(board.state()).state() == board.state()


def test_placeholders_fill_and_optional_parts(tmp_path):
    sai = backend.SandboxAI()
    browser = PR.find("browser")
    assert any(n.props.get("name") == "<mcp:browser>" for n in PR.build(sai, browser, tmp_path).of_kind("mcp"))
    left_out = PR.build(sai, browser, tmp_path, values={})
    assert left_out.of_kind("mcp") == [] and B.problems(sai, left_out) == []
    session = PR.build(sai, PR.find("kept-session"), tmp_path, values={})
    assert "--log-dir" not in B.to_argv(sai, session) and "--log-debug" in B.to_argv(sai, session)
    vpns = PR.build(sai, PR.find("vpn-mcp"), tmp_path, values={"<vpn:search>": "mv"})
    servers = {n.props["name"]: n.props["definition"] for n in vpns.of_kind("mcp")}
    assert servers["search"]["options"][:2] == ["--vpn", "mv"] and servers["fetch"]["options"][1] == "<vpn:fetch>"
    assert [m for m, _ in B.placeholders(vpns)] == ["<vpn:fetch>"], "a required one stays"
    texts = [b.text for b in B.badges(vpns)[1]]
    assert "MCP server search reaches the network through the VPN mv: pypi, duckduckgo" in texts
    gpu = PR.build(sai, PR.find("gpu"), tmp_path, values={"<gpu:kind>": "nvidia"})
    assert "--image-variant" not in B.to_argv(sai, gpu) and B.to_argv(sai, gpu)[:4] == ["--workspace", str(tmp_path),
                                                                                       "--gpu", "nvidia"]


def test_rules_and_llm_on_the_board(tmp_path):
    sai = backend.SandboxAI()
    board = PR.build(sai, PR.find("strict-rules"), tmp_path)
    approvals = board.first("approvals")
    assert approvals.props["rules"]["http"][0]["id"] == "no-gists"
    assert any("Rulesets allow, ask or deny" in b.text for b in B.badges(board)[1])
    document, _ = B.project_document(sai, board)
    assert document["rules"] == PR.GITHUB_RULES and "rules" not in board.document_extra
    approvals.props["rules"] = {"version": 1, "http": [{"id": "x", "decision": "maybe", "host": "a.example"}]}
    assert any(p.level == "error" and p.node == approvals.id for p in B.problems(sai, board)), "sandbox-ai's own check"
    board.first("proxy").props["enabled"] = False
    assert any("HTTP rules need the connection proxy" in p.text for p in B.problems(sai, board))
    llm = PR.build(sai, PR.find("local-llm"), tmp_path, values={"<llm:local>": "ollama"})
    node = llm.first("llm")
    assert node.title() == "LLM server: ollama" and (llm.first("agent").id, node.id) in llm.edges
    assert any(b.text == "LLM server ollama: its address and port only" for b in B.badges(llm)[1])
    assert B.from_argv(sai, "claude", ["--llm", "x:m", "--rules", "/tmp/r.json", "--log-debug"]).first("logs") \
        .props == {"--log-debug": True}


def test_own_presets_save_export_import(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    sai = backend.SandboxAI()
    board = PR.build(sai, PR.find("vpn-mcp"), tmp_path / "project", values={"<vpn:search>": "mv"})
    board.first("agent").props["model"] = "sonnet"
    preset = PR.from_board(sai, board, "Search behind Mullvad", "mine", "privacy")
    assert preset.id == "my-search-behind-mullvad" and preset.user and preset.category == "privacy"
    assert "--workspace" not in preset.options and [p.marker for p in preset.placeholders] == ["<vpn:fetch>"]
    PR.add_mine(sai, preset)
    assert oct(os.stat(PR.store_path()).st_mode & 0o777) == "0o600"
    mine, problems = PR.load_mine(sai)
    assert problems == [] and [p.id for p in mine] == [preset.id]
    other = tmp_path / "other"
    again = PR.build(sai, mine[0], other)
    assert B.to_argv(sai, again, workspaces=False) == B.to_argv(sai, board, workspaces=False)
    assert B.values(again)[2] == ["--model", "sonnet"] and B.values(again)[1]["--workspace"] == [str(other)]
    assert sorted((n.kind, n.x, n.y) for n in again.nodes) == sorted((n.kind, n.x, n.y) for n in board.nodes), \
        "positions kept"
    path = tmp_path / "preset.json"
    PR.export(sai, preset, path)
    document = json.loads(path.read_text())
    assert document["format"] == PR.FORMAT and document["mcp_servers"]["search"]["options"][1] == "mv"
    imported = PR.import_file(sai, path)
    assert PR.to_document(imported) == document
    shadow = dict(document, id="locked")
    path.write_text(json.dumps(shadow))
    assert PR.import_file(sai, path).id == "my-locked", "a built-in preset is never replaced"
    PR.remove_mine(sai, preset.id)
    assert PR.load_mine(sai)[0] == []


@pytest.mark.parametrize("change, reason", [
    ({"agent_args": ["--api-key", "x"]}, "agent arguments"),
    ({"options": ["--secret-env", "ghp_" + "x1" * 18]}, "looks like a secret"),  # a made-up token
    ({"notes": ["token sk-live-abcdef"]}, "looks like a secret"),
    ({"options": ["--copy-auth"]}, "copies a login in"),
    ({"options": ["--workspace", "/etc"]}, "--workspace"),
    ({"options": ["--no-such-option"]}, "does not know"),
    ({"rules": {"version": 1, "http": [{"id": "x", "host": "a.example", "decision": "perhaps"}]}}, "decision"),
    ({"mcp_servers": {"bad": {"command": "not a list"}}}, "command"),
    ({"id": "../../x"}, "lower-case"),
    ({"positions": {"agent#0": ["x", 1]}}, "Positions"),
    ({"surprise": 1}, "Unknown keys"),
    ({"format": "something else"}, "Not a preset"),
])
def test_imports_are_checked_and_secrets_refused(tmp_path, change, reason):
    sai = backend.SandboxAI()
    document = {**PR.to_document(PR.find("strict-rules")), "id": "my-strict"}
    assert PR.from_document(sai, document).rules == PR.GITHUB_RULES
    path = tmp_path / "preset.json"
    path.write_text(json.dumps({**document, **change}))
    with pytest.raises(ValueError, match=reason):
        PR.import_file(sai, path)


def test_a_board_with_a_secret_is_not_saved_and_large_files_are_refused(tmp_path):
    sai = backend.SandboxAI()
    board = B.from_argv(sai, "claude", ["--allow-pypi"], ["--api-key", "sk-ant-0123456789"])
    with pytest.raises(ValueError, match="secret"):
        PR.from_board(sai, board, "Leaky")
    board = B.from_argv(sai, "claude", ["--copy-auth"])
    with pytest.raises(ValueError, match="--copy-auth"):
        PR.from_board(sai, board, "Login")
    path = tmp_path / "big.json"
    path.write_text(" " * (PR.LIMIT + 10))
    with pytest.raises(ValueError, match="larger"):
        PR.import_file(sai, path)


def test_search():
    assert PR.matches(PR.find("vpn-mcp"), "vpn mcp") and not PR.matches(PR.find("locked"), "vpn")
    assert {p.id for p in PR.PRESETS if PR.matches(p, "tailscale")} == {"remote-llm", "tailnet"}
