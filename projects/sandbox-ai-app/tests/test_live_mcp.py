"""A running start on this machine or a host, the words of the commands (no Qt): MCP servers attached and detached
while it runs (`sandbox-ai mcp attach|detach NAME START --live --json`, --helper AGENT), a workspace that is a file or
has a name (`workspace add RUN PATH --name=NAME`), the model servers of a host (`llm list --json`, `llm models NAME
--json` there) and its connection logs (`activity --kind=network --json`, `logs --json`). Each runs as `sandbox-ai
...` here or `sandbox-ai hosts run HOST -- ...` there, values checked first; the real command line takes every argv
(run against the checkout in SANDBOX_AI_HOME in a temporary HOME and state: nothing starts, nothing of the real HOME
is read or written)."""
import json
import os

import pytest

from sandbox_ai_app import backend

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")
REMOTE = ("hosts", "run", "ct1", "--")
ATTACHED = {"server": "fetch", "start": "api", "run": "claude-1", "state": "running", "attached": "live",
            "server_started": True, "kept": True, "agent": "codex", "agent_config": None, "agent_restart": "required",
            "relay": "/run/sandbox-mcp/fetch/server.sock",
            "message": "api reaches the MCP server fetch now, kept for its later starts. Codex reads ..."}


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache"), ("HOME", "home")):
        monkeypatch.setenv(name, str(tmp_path / sub))
        (tmp_path / sub).mkdir()
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    return tmp_path


@pytest.fixture
def sai(env, monkeypatch):
    """A SandboxAI whose call() records its arguments and answers from sai.answers (by the words after the host)."""
    sai = backend.SandboxAI()
    sai.calls, sai.answers = [], {}

    def call(*args, timeout=120, input=None):
        sai.calls.append(args)
        words = args[len(REMOTE):] if args[:2] == ("hosts", "run") else args
        for length in range(len(words), 0, -1):
            if words[:length] in sai.answers:
                return sai.answers[words[:length]]
        return 0, "", ""
    monkeypatch.setattr(sai, "call", call)
    return sai


def on(host, *args):
    return (*(() if host == "local" else REMOTE), *args)


@pytest.mark.parametrize("host", ["local", "ct1"])
def test_mcp_servers_attached_and_detached_while_it_runs(sai, host):
    sai.answers[("mcp", "attach")] = (0, json.dumps(ATTACHED), "api reaches the MCP server fetch now ...\n"
                                                                "  not kept for the next start: busy\n")
    result = sai.host_mcp_attach_live(host, "api", "fetch")
    assert sai.calls[-1] == on(host, "mcp", "attach", "fetch", "api", "--live", "--json")
    assert result["agent_restart"] == "required" and result["message"].startswith("api reaches")
    assert result["notes"] == ["api reaches the MCP server fetch now ...", "not kept for the next start: busy"]
    sai.host_mcp_attach_live(host, "api", helper="codex")
    assert sai.calls[-1] == on(host, "mcp", "attach", "--helper", "codex", "api", "--live", "--json")
    sai.answers[("mcp", "detach")] = (0, json.dumps({"server": "fetch", "detached": "live", "message": "closed"}), "")
    assert sai.host_mcp_detach_live(host, "api", "fetch")["detached"] == "live"
    assert sai.calls[-1] == on(host, "mcp", "detach", "fetch", "api", "--live", "--json")
    sai.host_mcp_detach_live(host, "api", helper="claude")
    assert sai.calls[-1] == on(host, "mcp", "detach", "--helper", "claude", "api", "--live", "--json")
    # Refused: sandbox-ai's own reason, without its program name.
    sai.answers[("mcp", "attach")] = (1, "", "sandbox-mcp: api is paused: resume it first (sandbox-ai resume api), "
                                             "then attach\n")
    with pytest.raises(ValueError, match="^api is paused: resume it first"):
        sai.host_mcp_attach_live(host, "api", "fetch")


def test_live_values_are_checked_before_anything_runs(sai):
    for start, server, helper in (("-x", "fetch", None), ("api", "--evil", None), ("api", None, "bash"),
                                  ("api", "fetch", "codex"), ("api", None, None), ("a b", "fetch", None)):
        with pytest.raises(ValueError):
            sai.host_mcp_attach_live("ct1", start, server, helper)
        with pytest.raises(ValueError):
            sai.host_mcp_detach_live("ct1", start, server, helper)
    assert sai.calls == []
    assert backend.helper_of("helper-codex-0123abcd") == "codex" and backend.helper_of("fetch") is None
    assert backend.helper_of("helper-bash-0123abcd") is None


@pytest.mark.parametrize("host", ["local", "ct1"])
def test_a_workspace_with_a_name_and_one_that_is_a_file(sai, host):
    assert sai.add_workspace("api", "/srv/notes.txt", True, "notes.txt", host=host)[0] == 0
    assert sai.calls[-1] == on(host, "workspace", "add", "api", "/srv/notes.txt", "--ro", "--name=notes.txt")
    sai.add_workspace("api", "/srv/data", host=host)
    assert sai.calls[-1] == on(host, "workspace", "add", "api", "/srv/data"), "no name: its own"
    for name in ("-x", ".hidden", "a/b", "x" * 65, "..", "a b"):
        assert sai.add_workspace("api", "/srv/data", False, name, host=host)[0] == 2
    assert len(sai.calls) == 2
    assert backend.valid_workspace_name("A1_.-z") and not backend.valid_workspace_name("")


def test_a_hosts_workspaces_say_file_or_folder_and_named(sai):
    sai.answers[("workspace", "list")] = (0, json.dumps([
        {"host": "/srv/x", "sandbox": "/work", "mode": None, "source": "start", "kind": "dir"},
        {"host": "/srv/n.txt", "sandbox": "/workspaces/notes", "mode": "ro", "source": "added", "kind": "file",
         "named": True, "kept": False},
        {"host": "/srv/y", "sandbox": "/workspaces/y", "mode": "rw", "source": "added", "kind": "socket",
         "named": "yes"}]), "")
    rows = sai.host_workspaces("ct1", "api")
    assert rows[1] == {"host": "/srv/n.txt", "sandbox": "/workspaces/notes", "mode": "ro", "source": "added",
                       "kind": "file", "named": True}
    assert "kind" not in rows[2] and "named" not in rows[2], "only what sandbox-ai says it may be"


def test_the_model_servers_of_a_host(sai, monkeypatch):
    listed = [{"name": "gpu", "kind": "ollama", "url": "http://127.0.0.1:11434", "models": []},
              {"name": "big", "kind": "vllm", "url": "http://10.0.0.2:8000", "models": ["qwen"]},
              {"name": "--x", "kind": "vllm"}]
    sai.answers[("llm", "list")] = (0, "llm: skipped a broken entry\n" + json.dumps(listed), "")
    sai.answers[("llm", "models", "gpu")] = (0, json.dumps([{"name": "llama3:8b"}, {"name": "-evil"}]), "")
    servers = sai.host_llm_servers("ct1")
    assert [s["name"] for s in servers] == ["gpu", "big"]
    assert servers[0]["models"] == ["llama3:8b"] and servers[1]["models"] == ["qwen"]
    assert on("ct1", "llm", "list", "--json") in sai.calls and on("ct1", "llm", "models", "gpu", "--json") in sai.calls
    assert on("ct1", "llm", "models", "big", "--json") not in sai.calls, "listed already: not asked"
    sai.answers[("llm", "models", "gpu")] = (1, "", "llm: gpu does not answer")
    assert sai.host_llm_servers("ct1")[0]["models"] == [], "a server that does not answer keeps what was listed"
    monkeypatch.setattr(sai, "llm_endpoints", lambda: [{"name": "here"}])
    sai.calls.clear()
    assert sai.host_llm_servers("local") == [{"name": "here"}] and sai.calls == [], "this machine's: its own module"


def test_the_connection_log_of_a_host(sai):
    records = [{"kind": "network", "time": "2026-11-08T10:00:00Z", "summary": "denied: evil.example (not allowed)",
                "details": {"verdict": "denied", "event": "denied", "target": "evil.example", "reason": "not allowed",
                            "destination": "evil.example:443"}},
               {"kind": "network", "time": "2026-11-08T10:00:01Z", "summary": "allowed: GET https://pypi.org/ → 200",
                "details": {"verdict": "allowed", "event": "http", "method": "GET", "url": "https://pypi.org/",
                            "status": 200, "bytes_in": 10, "bytes_out": 5}},
               {"kind": "network", "time": "2026-11-08T10:00:02Z", "summary": "allowed: 3 more requests to pypi.org",
                "details": {"verdict": "allowed", "target": "pypi.org", "count": 3, "repeats": True}}]
    sai.answers[("activity",)] = (0, "\n".join(json.dumps(r) for r in records), "")
    found = sai.host_connections("ct1", ["--session", "api"], tail=100)
    assert sai.calls[-1] == on("ct1", "activity", "--session", "api", "--json", "--tail", "100", "--kind=network")
    assert [backend.summary(r)[:5] for r in found] == [
        ("2026-11-08T10:00:00Z", "denied", "", "", "evil.example:443  (not allowed)"),
        ("2026-11-08T10:00:01Z", "http", "GET", "200", "https://pypi.org/"),
        ("2026-11-08T10:00:02Z", "allowed", "", "", "pypi.org")]
    assert backend.summary(found[1])[5] == "15"
    sai.answers[("logs",)] = (0, json.dumps({"directory": "/srv/logs", "denied": 2, "refusals": []}), "")
    assert sai.host_log_summary("ct1", ["--log-dir", "/srv/logs"])["denied"] == 2
    assert sai.calls[-1] == on("ct1", "logs", "/srv/logs", "--json")
    sai.host_log_summary("ct1", ["--project", "/srv/project"])
    assert sai.calls[-1] == on("ct1", "logs", "--project=/srv/project", "--json")
    with pytest.raises(ValueError):
        sai.host_log_summary("ct1", ["--log-dir", "relative"])


@pytest.mark.parametrize("args", [
    ("mcp", "attach", "fetch", "api", "--live", "--json"), ("mcp", "attach", "--helper", "codex", "api", "--live",
                                                            "--json"),
    ("mcp", "detach", "fetch", "api", "--live", "--json"), ("mcp", "detach", "--helper", "claude", "api", "--live",
                                                            "--json"),
    ("workspace", "add", "api", "/nonexistent/notes.txt", "--ro", "--name=notes.txt"),
    ("llm", "list", "--json"), ("llm", "models", "gpu", "--json"),
    ("activity", "--session", "api", "--json", "--tail", "5", "--kind=network"),
    ("activity", "--log-dir", "/nonexistent", "--json", "--tail", "5", "--kind=network"),
    ("logs", "/nonexistent", "--json"), ("logs", "--project=/nonexistent", "--json")])
def test_the_real_command_line_takes_each_argv(env, args):
    """Run for real (nothing to act on in the empty state): sandbox-ai may say no such start, but never that it does
    not understand the arguments (argparse: exit status 2 with its usage)."""
    sai = backend.SandboxAI()
    status, out, err = sai.call(*args, timeout=120)
    assert not (status == 2 and "usage:" in err), (args, err)
    assert "unrecognized arguments" not in err and "invalid choice" not in err, (args, err)
