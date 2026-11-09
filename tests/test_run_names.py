"""Every command for a running sandbox (workspace, net, limits, devices, approvals history, canaries resume) finds
it by the name `sandbox-ai list` (ps) shows, also after a rename, or by its run id (net.choose); a name that does
not run says what it is and what to do instead; `workspace add|remove` on a stopped kept start changes what its next
start mounts."""
import importlib.util
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sandbox_workspaces_for_run_names", ROOT / "sandbox/workspaces.py")
w = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(w)
net = w.module("net")
sessions = w.module("sessions")
L = w.module("agent_sandbox")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    (tmp_path / "runtime").mkdir()
    monkeypatch.setattr(net, "USER_RUNTIME", tmp_path / "run-user")
    monkeypatch.setattr(L, "exposed_before", lambda: [])
    monkeypatch.setattr(L, "load_policy", lambda path=None: {})


def make_start(tmp_path, key, **fields):
    """A start directory as sessions.py writes it; no supervisor runs (a stopped start, its HOME kept)."""
    folder = tmp_path / "projects" / key
    folder.mkdir(parents=True, exist_ok=True)
    directory = sessions.session_dir(key)
    for path in (directory, directory / "home", directory / "tmux"):
        path.mkdir(parents=True, exist_ok=True)
    data = {"agent": "claude", "options": ["--backend", "podman"], "agent_args": [], "backend": "podman",
            "workspace": str(folder), "cwd": str(folder), "created": "2026-11-07T09:10:00Z", **fields}
    sessions.save(key, data)
    return folder


def register(run, **fields):
    """A run.json of a running launcher (this test process: alive for net.running())."""
    directory = L.runs_dir() / run
    directory.mkdir(parents=True)
    (directory / "run.json").write_text(json.dumps({"run": run, "agent": "claude", "pid": os.getpid(),
                                                    "workspace": "/srv/x", "started": "2026-11-09T08:00:00Z",
                                                    **fields}))
    return directory


def make_mcp(name, **fields):
    directory = Path(os.environ["XDG_STATE_HOME"]) / "sandbox-ai" / "mcp" / name
    (directory / "sock").mkdir(parents=True)
    (directory / "mcp.json").write_text(json.dumps({"backend": "podman", "command": ["x"], **fields}))


@pytest.fixture
def host(tmp_path):
    """The user's report: two sessions running under names other than their run ids (one renamed since its run.json
    was written), MCP servers, and a stopped kept start."""
    make_start(tmp_path, "claude-sandbox-ai-public", name="claude-sandbox-ai-public-20c7")
    register("claude-75d613a4", name="claude-sandbox-ai-public", start="claude-sandbox-ai-public")
    make_start(tmp_path, "opencode-tor-network-a3fb")
    register("opencode-7ba00d13", name="opencode-tor-network-a3fb", start="opencode-tor-network-a3fb")
    register("mcp-fetch", kind="mcp")
    register("mcp-search", kind="mcp")
    make_start(tmp_path, "claude-axross-de39", workspace="/srv/axross")
    return net.running()


def test_a_running_start_is_found_by_its_name_after_a_rename_its_id_and_its_run_id(host):
    assert net.choose(host, "claude-sandbox-ai-public-20c7")["run"] == "claude-75d613a4"  # session.json's name
    assert net.choose(host, "claude-75d613a4")["run"] == "claude-75d613a4"  # the run id
    assert net.choose(host, "claude-sandbox-ai-public")["run"] == "claude-75d613a4"  # its start's id
    assert net.choose(host, "opencode-tor-network-a3fb")["run"] == "opencode-7ba00d13"
    assert net.choose(host, "mcp-fetch")["run"] == "mcp-fetch"
    starts = net.starts_module()
    assert starts.rename("claude-sandbox-ai-public-20c7", "public", out=lambda line: None) == 0
    assert net.choose(net.running(), "public")["run"] == "claude-75d613a4"
    with pytest.raises(net.NotRunning, match="claude-sandbox-ai-public-20c7 was renamed: it is public now; running: public, "):
        net.choose(net.running(), "claude-sandbox-ai-public-20c7")  # the old name is no longer its name


def test_the_running_ones_are_named_as_sandbox_ai_list_names_them(host):
    with pytest.raises(net.NotRunning) as caught:
        net.choose(host, "nope", command="workspace add")
    assert str(caught.value) == ("no start named nope; running: claude-sandbox-ai-public-20c7, mcp-fetch, mcp-search, "
                                 "opencode-tor-network-a3fb (see sandbox-ai list)")
    assert caught.value.entry is None and "75d613a4" not in str(caught.value)
    with pytest.raises(ValueError, match="several sandboxes run; name one: claude-sandbox-ai-public-20c7, "):
        net.choose(host, None, cwd="/elsewhere")
    with pytest.raises(net.NotRunning, match=r"no start named x\\x1b; nothing runs"):
        net.choose({}, "x\x1b")  # escaped in the message


def test_a_stopped_start_says_so_and_what_each_command_can_do(host):
    expected = {
        "workspace add": "claude-axross-de39 is stopped: start it with the folder: sandbox-ai resume claude-axross-de39 "
                         "--workspace DIR",
        "net allow": "claude-axross-de39 is stopped: start it first (sandbox-ai resume claude-axross-de39), then: "
                     "sandbox-ai net allow claude-axross-de39 ...",
        "limits": "claude-axross-de39 is stopped: start it first (sandbox-ai resume claude-axross-de39), then: "
                  "sandbox-ai limits claude-axross-de39",
        "devices add": "then: sandbox-ai devices add claude-axross-de39 DEVICE",
        None: "claude-axross-de39 is stopped: start it again first: sandbox-ai resume claude-axross-de39",
    }
    for command, message in expected.items():
        with pytest.raises(net.NotRunning) as caught:
            net.choose(host, "claude-axross-de39", command=command)
        assert message in str(caught.value), command
        assert caught.value.entry["state"] == "stopped" and w.stopped_start(caught.value.entry)


def test_ambiguous_names_paused_mcp_and_ended_starts(host, tmp_path):
    runs = {**host, "codex-2": {"run": "codex-2", "name": "twin"}, "codex-3": {"run": "codex-3", "name": "twin"}}
    with pytest.raises(ValueError, match="several running sandboxes are named twin; name one by its run id: codex-2, "
                                         "codex-3"):
        net.choose(runs, "twin")
    assert "twin (run codex-2), twin (run codex-3)" in net.running_names(runs)
    # Paused: it still runs (frozen); a change asks it to be resumed first, by its name.
    paused = register("claude-p1", name="frozen")
    (paused / "paused").touch()
    assert net.choose(net.running(), "frozen")["run"] == "claude-p1"
    with pytest.raises(ValueError, match=r"paused: resume it first \(sandbox-ai resume frozen\)"):
        net.ask(str(paused), {"op": "show"})
    make_mcp("demo")
    with pytest.raises(net.NotRunning, match="mcp-demo is an MCP server sandbox that does not run now"):
        net.choose(host, "mcp-demo")
    net.starts_module().record("gone-1", "start", "gone-1", "claude-9")
    with pytest.raises(net.NotRunning, match="gone-1 has ended and its HOME was discarded"):
        net.choose(host, "gone-1")


def test_the_commands_take_the_name_and_print_the_message(host, capsys, monkeypatch):
    for module, argv in ((net, ["allow", "claude-axross-de39", "--allow-host", "example.org"]),
                         (w.module("devices"), ["claude-axross-de39", "--memory", "2g"]),
                         (w, ["list", "claude-axross-de39"])):
        main = module.main if module is not w.module("devices") else (lambda argv: module.main(argv, prog="limits"))
        assert main(argv) == 1
        err = capsys.readouterr().err
        assert "claude-axross-de39 is stopped: " in err and "resume claude-axross-de39" in err, err
    approvals_spec = importlib.util.spec_from_file_location("sandbox_approvals_for_run_names",
                                                            ROOT / "sandbox/approvals.py")
    approvals = importlib.util.module_from_spec(approvals_spec)
    approvals_spec.loader.exec_module(approvals)
    lines = []
    assert approvals.main(["history", "claude-sandbox-ai-public-20c7"], out=lines.append) == 0
    assert approvals.main(["history", "nope"], out=lines.append) == 1
    assert "no start named nope; running: claude-sandbox-ai-public-20c7" in capsys.readouterr().err


def test_workspace_add_and_remove_on_a_stopped_kept_start_change_its_next_start(host, tmp_path, capsys):
    folder = tmp_path / "config-axross"
    folder.mkdir()
    lines = []
    assert w.main(["add", "claude-axross-de39", str(folder), "--name", "axcfg"], out=lines.append) == 0
    options = sessions.load("claude-axross-de39")["options"]
    assert options[-4:] == ["--workspace", str(folder), "--workspace-name", f"axcfg={folder}"]
    assert lines[-1] == (f"claude-axross-de39 is stopped: {folder} is kept for its next start, at /workspaces/axcfg "
                         "(rw); it is mounted, with the checks of a start, when it starts again: sandbox-ai resume "
                         "claude-axross-de39")
    # Twice, or another one under that name: refused.
    assert w.main(["add", "claude-axross-de39", str(folder)]) == 1
    assert "one of claude-axross-de39's workspaces already" in capsys.readouterr().err
    other = tmp_path / "other"
    other.mkdir()
    assert w.main(["add", "claude-axross-de39", str(other), "--name", "axcfg"]) == 1
    assert "/workspaces/axcfg is where another workspace" in capsys.readouterr().err
    # --ro would come back writable; a file lasts for a run only.
    assert w.main(["add", "claude-axross-de39", str(other), "--ro"]) == 1
    assert "--workspace-ro" in capsys.readouterr().err
    (tmp_path / "notes.txt").write_text("x")
    assert w.main(["add", "claude-axross-de39", str(tmp_path / "notes.txt")]) == 1
    assert "a file lasts for a run only" in capsys.readouterr().err
    assert sessions.load("claude-axross-de39")["options"] == options
    # remove: by its name below /workspaces (or its host path); the start's own workspace stays.
    assert w.main(["remove", "claude-axross-de39", str(tmp_path / "projects" / "claude-axross-de39")]) == 1
    assert "no workspace kept for its next start is" in capsys.readouterr().err
    assert w.main(["remove", "claude-axross-de39", "axcfg"], out=lines.append) == 0
    left = sessions.load("claude-axross-de39")["options"]
    assert str(folder) not in " ".join(left) and "--workspace-name" not in left
    assert lines[-1] == (f"claude-axross-de39 is stopped: {folder} is no longer kept for its next start (sandbox-ai "
                         "resume claude-axross-de39 starts without it)")
    assert w.main(["add", "claude-axross-de39", str(folder)], out=lines.append) == 0
    assert w.main(["remove", "claude-axross-de39", str(folder)], out=lines.append) == 0
    assert str(folder) not in " ".join(sessions.load("claude-axross-de39")["options"])


def test_a_start_that_keeps_no_home_or_an_unknown_name_is_not_changed(host, tmp_path, capsys):
    make_start(tmp_path, "tmp-run", keep=False)
    folder = tmp_path / "f"
    folder.mkdir()
    assert w.main(["add", "nope", str(folder)]) == 1
    assert "sandbox-ai workspace: no start named nope; running: claude-sandbox-ai-public-20c7" in \
        capsys.readouterr().err
    entry = {"state": "stopped", "directory": "/d", "kind": "session", "keep": False}
    assert not w.stopped_start(entry) and not w.stopped_start(None) and w.stopped_start({**entry, "keep": True})
