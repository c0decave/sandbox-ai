"""The board model (sandbox_ai_app/board.py, no Qt): options -> graph -> options round trips, problems,
badges, the administrator's policy, templates checked by the real launcher, the layout file."""
import json
import os

import pytest

from sandbox_ai_app import backend, board as B

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


@pytest.fixture(scope="module")
def sai():
    return backend.SandboxAI()


def normal(sai, argv):
    """{option: value} of ARGV with workspaces split at commas (both spellings mean the same)."""
    values, unknown = backend.values_from_argv(sai.options(), list(argv), sai.launcher.canonical_options)
    if "--workspace" in values:
        values["--workspace"] = [w for v in values["--workspace"] for w in v.split(",") if w]
    return values, unknown


ROUND_TRIPS = [
    [],
    ["--offline"],
    ["-o", "-nt", "-W", "-w", "/tmp/a"],
    ["--profile", "development", "--token-proxy", "--git-identity"],
    ["-p", "dns", "-p", "frontier", "-y", "-g", "-gl", "-ak", "-dd", "-A", "x.example", "-A", "y.example"],
    ["--allow", "10.0.0.0/8", "--allow-tcp", "192.0.2.5:5432", "-P", "3000", "--nameserver", "9.9.9.9"],
    ["--egress-all-allow", "--no-provider-defaults"],
    ["--no-proxy", "--privacy-check", "off"],
    ["--allow-http", "api.github.com GET,HEAD /**", "--allow-http", "api.github.com POST /repos/me/app/pulls",
     "--no-inspect", "*.corp.example", "--privacy-rules", "/tmp/rules"],
    ["--ask", "off", "--git-token-env", "https://github.com=GH_TOKEN", "--git-token-file",
     "http://localhost:3080=/tmp/token", "--git-token-secret", "https://gitlab.com=GL"],
    ["--git-credentials", "/tmp/creds"],
    ["-m", "/data:/mnt/data:rw", "-m", "/etc/ssl:/opt/ssl:ro", "--mcp", "files", "--mcp", "web"],
    ["--device", "/dev/video0", "--gpu", "dri", "--memory", "4g", "--cpus", "2", "--pids", "512"],
    ["--gui", "--gui-screen", "1280x720x24", "--gui-vnc", "/tmp/vnc", "--gui-wayland"],
    ["--tmux", "--log-dir", "/tmp/logs", "--kit", "a", "--kit-dir", "/tmp/kit", "--no-default-kit", "--no-memory",
     "--no-plugins"],
    ["--state-dir", "/tmp/home", "--copy", "/tmp/x.json:.config/x.json", "--no-auth"],
    ["--auth-file", "/tmp/auth.json", "--env", "FOO", "--secret-env", "API=OPENROUTER", "-D"],
    ["--backend", "bwrap", "--binary", "/usr/bin/claude", "--runtime", "/opt/node"],
    ["--image", "localhost/claude-sandbox:test", "--podman-url", "unix:///tmp/p.sock", "--network", "mynet",
     "--ip", "10.88.0.5"],
    ["--probe", "--no-config"],  # owned by no node: kept as they are
    ["--vpn", "mullvad-se", "--vpn-check", "warn"],
]


@pytest.mark.parametrize("argv", ROUND_TRIPS, ids=lambda a: " ".join(a)[:40] or "empty")
def test_options_survive_the_board(sai, argv):
    board = B.from_argv(sai, "claude", argv)
    back = B.to_argv(sai, board)
    assert normal(sai, back) == normal(sai, argv)
    again = B.from_argv(sai, "claude", back)
    assert B.to_argv(sai, again) == back, "a second trip changes nothing"


def test_nodes_own_their_options_and_unknown_tokens_are_kept(sai):
    board = B.from_argv(sai, "opencode", ["-w", "/tmp/a,/tmp/b", "-m", "/data:/mnt/d:rw", "--mcp", "files",
                                          "--allow-host", "x.example", "--memory", "4g", "--bogus-option"],
                        ["--model", "local/qwen", "--verbose"])
    kinds = sorted(n.kind for n in board.nodes)
    assert kinds == ["agent", "limits", "mcp", "mount", "network", "proxy", "workspace", "workspace"]
    assert board.first("agent").props["model"] == "local/qwen" and board.first("agent").props["arguments"] == "--verbose"
    assert board.first("network").props == {"--allow-host": ["x.example"]}
    assert "--bogus-option" in board.extra
    agent, _, agent_args = B.values(board)
    assert agent == "opencode" and agent_args == ["--model", "local/qwen", "--verbose"]
    assert B.command_line(sai, board).startswith("sandbox-ai opencode ")


def test_disconnected_parts_do_not_count(sai):
    board = B.from_argv(sai, "claude", ["-w", "/tmp/a", "--mcp", "files", "-m", "/data:/mnt/d:ro"])
    agent, server = board.first("agent"), board.first("mcp")
    board.disconnect(agent.id, server.id)
    board.disconnect(board.first("mount").id, agent.id)
    argv = B.to_argv(sai, board)
    assert "--mcp" not in argv and "--mount" not in argv and "--workspace" in argv
    assert sum("not connected" in p.text for p in B.problems(sai, board)) == 2
    board.connect(agent.id, server.id)
    assert "--mcp" in B.to_argv(sai, board)
    with pytest.raises(ValueError):
        board.connect(server.id, board.first("network").id)  # no such flow
    with pytest.raises(ValueError):
        board.remove(agent.id)  # the agent, proxy and network always exist
    with pytest.raises(ValueError):
        board.add("network")  # one of them


def test_project_files_round_trip(sai, tmp_path):
    document = {"version": 1, "agent": "codex", "options": ["--allow-pypi", "--token-proxy", "--git-token-secret",
                                                           "https://github.com=GH"],
                "workspaces": ["/tmp/shared"], "agent_args": ["--model", "gpt-5"], "description": "my project",
                "mcp_servers": {"x": {"command": ["x"]}}}
    board = B.from_project(sai, document, tmp_path)
    assert [n.props["path"] for n in board.of_kind("workspace")] == [str(tmp_path), "/tmp/shared"]
    written, main = B.project_document(sai, board)
    options, expected = written.pop("options"), document.pop("options")
    if "--model" in {o.long for o in sai.options()}:  # the launcher's own option now, no longer the agent's
        expected += ["--model", document.pop("agent_args")[1]]
    assert main == str(tmp_path) and normal(sai, options) == normal(sai, expected)
    assert written == document, "nothing lost, nothing added (the options in the parser's order)"
    assert "--workspace" not in options, "the file lives in the main workspace"


def test_problems_mirror_the_launcher(sai):
    def texts(argv, agent="claude", policy=None):
        return [p.text for p in B.problems(sai, B.from_argv(sai, agent, argv), policy) if p.level == "error"]
    assert texts(["--offline", "--allow-pypi"]) and texts(["-o", "--git-token-env", "https://github.com=T"])
    assert any("connection proxy" in t for t in texts(["--no-proxy", "--allow-http", "api.github.com GET /**"]))
    assert any("not seen under --no-inspect" in t
               for t in texts(["--allow-http", "api.github.com GET /**", "--no-inspect", "*.github.com"]))
    assert any("'HOST METHODS PATH'" in t for t in texts(["--allow-http", "api.github.com"]))
    assert any("HTTPS" in t for t in texts(["--token-proxy", "--git-token-env", "http://localhost:3080=T"]))
    assert any("not\n--git-credentials" in t or "--git-credentials" in t
               for t in texts(["--token-proxy", "--git-credentials", "/tmp/c"]))
    assert any("Podman" in t for t in texts(["--backend", "bwrap", "-P", "3000"]))
    assert any("OpenCode" in t for t in texts(["--local-config", "/tmp/c.json"], agent="claude"))
    assert any("tmux" in t for t in texts(["--tmux", "--no-tmux"]))
    assert any("IP address" in t for t in texts(["--nameserver", "dns.example"]))
    assert texts(["--profile", "development", "--token-proxy", "--git-identity"]) == []
    board = B.from_argv(sai, "claude", ["-m", "/data:/etc:rw"])
    assert any("/work, /mnt or /opt" in p.text for p in B.problems(sai, board))


def test_the_administrators_policy(sai):
    policy = {"version": 1, "forbid": ["--egress-all-allow", "--no-proxy", "--ask=off"], "backends": ["podman"],
              "allow_within": ["10.0.0.0/8"], "allow_hosts": ["pypi.org", "*.corp.example"]}

    def errors(argv):
        return [p.text for p in B.problems(sai, B.from_argv(sai, "claude", argv), policy) if p.level == "error"]
    assert any("forbids --egress-all-allow" in t for t in errors(["--profile", "egress-all"])), "a profile counts"
    assert any("forbids --ask=off" in t for t in errors(["--ask", "off"]))
    assert errors(["--ask", "on"]) == []
    assert any("backends" in t for t in errors(["--backend", "bwrap"]))
    assert any("outside" in t for t in errors(["--allow", "192.168.1.0/24"]))
    assert errors(["--allow", "10.1.0.0/16", "--allow-host", "git.corp.example"]) == []
    assert any("allow_hosts" in t for t in errors(["--allow-host", "evil.example"]))
    score, badges = B.badges(B.from_argv(sai, "claude", ["--no-proxy"]), policy)
    assert any(b.level == "bad" and "forbids --no-proxy" in b.text for b in badges)


def test_badges_and_score(sai):
    tight, _ = B.badges(B.from_argv(sai, "claude", ["--offline"]))
    loose, badges = B.badges(B.from_argv(sai, "claude", ["--egress-all-allow", "--no-proxy", "--device", "/dev/video0",
                                                          "--git-token-env", "https://github.com=T",
                                                          "--no-inspect", "internal.example", "--gui-wayland"]))
    assert tight == 100 and loose < 20
    texts = " | ".join(b.text for b in badges)
    for expected in ("Egress-all", "No connection proxy", "TLS not inspected for internal.example",
                     "Git token visible to the agent", "Host device /dev/video0", "Wayland"):
        assert expected in texts
    _, hidden = B.badges(B.from_argv(sai, "claude", ["--git-token-env", "https://github.com=T", "--token-proxy"]))
    assert any("never sees" in b.text for b in hidden) and not any("visible" in b.text for b in hidden)


@pytest.mark.parametrize("name", ["locked", "development", "review", "research", "strict-rules", "own-subnet"])
def test_templates_pass_the_launchers_own_checks(sai, name, tmp_path):
    """Presets without placeholders (tests/test_presets.py fills in the others)."""
    board = B.template(sai, name, tmp_path)
    assert not [p for p in B.problems(sai, board) if p.level == "error"]
    agent, _, _ = B.values(board)
    plan, message = sai.dry_run(agent, B.launcher_argv(sai, board))
    assert plan is not None, message


def test_the_launcher_refuses_what_the_board_refuses(sai, tmp_path):
    board = B.from_argv(sai, "claude", ["--offline", "--allow-pypi"], workspaces=[str(tmp_path)])
    plan, message = sai.dry_run("claude", B.launcher_argv(sai, board))
    assert plan is None and "--offline" in message


def test_layout_is_untrusted_data_next_to_the_project(sai, tmp_path):
    board = B.template(sai, "development", tmp_path)
    board.layout()
    board.first("agent").x = 333.0
    B.write_layout(tmp_path, board)
    again = B.template(sai, "development", tmp_path)
    assert again.place(B.read_layout(tmp_path)) and again.first("agent").x == 333.0
    (tmp_path / B.LAYOUT_NAME).write_text('{"version": 1, "nodes": {"agent": ["x", 1e99]}}')
    assert not again.place(B.read_layout(tmp_path))
    (tmp_path / B.LAYOUT_NAME).unlink()
    (tmp_path / B.LAYOUT_NAME).symlink_to("/etc/passwd")
    assert B.read_layout(tmp_path) == {}, "a link is not followed"
    victim = tmp_path / "victim"
    victim.write_text("keep")
    (tmp_path / B.LAYOUT_NAME).unlink()
    (tmp_path / B.LAYOUT_NAME).symlink_to(victim)
    B.write_layout(tmp_path, board)
    assert victim.read_text() == "keep" and not (tmp_path / B.LAYOUT_NAME).is_symlink()
    assert json.loads((tmp_path / B.LAYOUT_NAME).read_text())["version"] == 1


def test_mcp_nodes_describe_their_server(sai):
    rows = dict(B.mcp_summary({"image": "ghcr.io/x@sha256:1", "network": "full", "hosts": ["api.example"],
                               "mounts": [{"host": "/data", "target": "/work", "mode": "rw"}]}))
    assert rows["Runs"] == "ghcr.io/x@sha256:1" and rows["Network"] == "full" and "/data → /work (rw)" in rows["Folders"]
    assert "not defined" in dict(B.mcp_summary(None))["Definition"]
    assert isinstance(sai.mcp_definitions(), dict)


def test_snapshots_restore_the_board_exactly(sai):
    board = B.from_argv(sai, "claude", ["-w", "/tmp/a", "--mcp", "files", "--allow-host", "x.example"])
    board.layout(grid=20)
    assert all(n.x % 20 == 0 and n.y % 20 == 0 for n in board.nodes)
    state = board.state()
    board.first("network").props["--allow-host"].append("y.example")
    board.remove(board.first("mcp").id)
    again = B.Board.from_state(state)
    assert B.to_argv(sai, again) == B.to_argv(sai, B.from_argv(sai, "claude", ["-w", "/tmp/a", "--mcp", "files",
                                                                                 "--allow-host", "x.example"]))
    assert again.add("devices").id > max(n["id"] for n in state["nodes"]), "new ids never reuse old ones"
    assert B.ports("agent") == (True, True) and B.ports("workspace") == (False, True) and B.ports("network") == (True, False)


def test_the_default_backend_of_the_settings(sai):
    board = B.with_backend(B.from_argv(sai, "claude", []), "bwrap")
    assert B.to_argv(sai, board)[:2] == ["--backend", "bwrap"]
    assert B.to_argv(sai, B.with_backend(B.from_argv(sai, "claude", []), "podman")) == []


REVIEWERS_BOARD = ["--workspace", "~", "--mount", "~:/mnt/home:ro", "--allow-host", "*.com", "--allow-host-port", "22",
                   "--allow-tcp", "10.0.0.5:5432", "--secret-env", "AWS_SECRET_ACCESS_KEY", "--copy-auth", "--frontier",
                   "--image", "evil/image:latest", "--binary", "/tmp/x", "--gui-vnc", "/tmp/vnc", "--kit-dir",
                   "/tmp/kit", "--state-dir", "/tmp/state"]


def test_an_open_board_scores_low_with_the_reasons(sai):
    """The review's board once scored 100 with a green "TLS inspected": every opening now costs points."""
    board = B.from_argv(sai, "claude", REVIEWERS_BOARD)
    score, badges = B.badges(board, launcher=sai.launcher)
    assert score <= 10, score
    texts = [b.text for b in badges if b.level in ("bad", "warn")]

    def has(*words):
        return any(all(w in t for w in words) for t in texts)
    assert has("Workspace", "your HOME") and has("Mount", "your HOME")
    assert has("Every host matching *.com") and has("Port 22", "SSH")
    assert has("10.0.0.5:5432", "internal") and has("AWS_SECRET_ACCESS_KEY")
    assert has("login is copied") and has("OpenAI and Anthropic")
    assert has("Image evil/image:latest") and has("Agent program /tmp/x")
    assert has("VNC socket") and has("Kit copied")
    panels = B.panels(board, sai.launcher)
    assert {p.level for p in panels["read"]} >= {"bad"} and panels["secrets"] and panels["code"] and panels["reach"]
    assert any("HOME kept in" in p.text for p in panels["write"])


def test_a_project_board_scores_high_and_the_panels_say_why(sai, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    board = B.from_argv(sai, "claude", ["--allow-pypi"], workspaces=[str(project)])
    score, _ = B.badges(board, launcher=sai.launcher)
    assert score >= 95
    panels = B.panels(board, sai.launcher)
    assert [p.level for p in panels["read"]] == ["info"] and str(project) in panels["read"][0].text
    assert any("pypi" in p.text for p in panels["reach"]) and panels["secrets"] == []
    sensitive = B.panels(B.from_argv(sai, "claude", ["-m", f"{os.path.expanduser('~')}/.ssh:/mnt/ssh:ro"],
                                     workspaces=[str(project)]), sai.launcher)
    assert any(p.level == "bad" and ".ssh" in p.text for p in sensitive["read"])
    env = B.panels(B.from_argv(sai, "claude", ["--env", "GITHUB_TOKEN", "--env", "TERM"]))["secrets"]
    assert [p.level for p in env] == ["bad", "info"]


def test_unknown_agents_are_a_problem_not_claude(sai):
    board = B.from_argv(sai, "gemini", [])
    assert B.values(board)[0] == "gemini"
    assert any(p.level == "error" and "Unknown agent gemini" in p.text for p in B.problems(sai, board))


def test_the_projects_own_mcp_servers_are_nodes(sai, tmp_path):
    document = {"version": 1, "agent": "claude", "options": ["--mcp", "used"],
                "mcp_servers": {"used": {"image": "ghcr.io/x@sha256:1", "network": "full",
                                         "mounts": [{"host": os.path.expanduser("~"), "target": "/work", "mode": "rw"}]},
                                "idle": {"command": ["idle-server"]}}}
    board = B.from_project(sai, document, tmp_path)
    nodes = {n.props["name"]: n for n in board.of_kind("mcp")}
    assert set(nodes) == {"used", "idle"} and nodes["used"].props["project"]
    assert board.connected(nodes["used"]) and not board.connected(nodes["idle"])
    panels = B.panels(board, sai.launcher)
    assert any("MCP server used" in p.text and "your HOME" in p.text for p in panels["write"])
    assert any("MCP server used reaches the network" in p.text for p in panels["reach"])
    written, _ = B.project_document(sai, board)
    assert written["mcp_servers"] == document["mcp_servers"] and "mcp_servers" not in board.document_extra


def test_the_development_template_has_no_warnings(sai, tmp_path):
    board = B.template(sai, "development", tmp_path)
    assert B.problems(sai, board) == []
    assert any("Token proxy ready" in b.text for b in B.badges(board)[1])


def test_the_vpn_node(sai):
    board = B.from_argv(sai, "claude", ["-vp", "home", "-vc", "warn"])
    node = board.first("vpn")
    assert node is not None and node.props == {"--vpn": "home", "--vpn-check": "warn"}
    assert B.to_argv(sai, board, workspaces=False)[-4:] == ["--vpn", "home", "--vpn-check", "warn"]

    def errors(argv, policy=None):
        return [p.text for p in B.problems(sai, B.from_argv(sai, "claude", argv), policy) if p.level == "error"]
    assert errors(["--vpn", "home"]) == []
    assert any("Podman" in t for t in errors(["--vpn", "home", "--backend", "bwrap"]))
    assert any("no --nameserver" in t for t in errors(["--vpn", "home", "--nameserver", "9.9.9.9"]))
    policy = {"version": 1, "require_vpn": ["corp"]}
    assert any("requires a VPN" in t for t in errors([], policy))
    assert any("only these VPN profiles: corp" in t for t in errors(["--vpn", "home"], policy))
    assert errors(["--vpn", "corp"], policy) == [] and errors(["--offline"], policy) == []
