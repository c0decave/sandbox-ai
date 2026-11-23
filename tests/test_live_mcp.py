"""mcp-live (scripts/live_check.sh --only mcp-live [--mcp-servers A,B]) without a container engine: the user,
2026-11-23: "in-depth testing aller mcp server ob sie jetzt funktionieren sollten". The MCP repository's CI smoke only
borrows sandbox-ai's run line; nothing tested a start's real path (the supervisor, the relay, the @workspace binding,
the per-workspace own folders) - which is where binwalk and radare2 died before the handshake.

Here: each server's plan (what it is given, what it skips and why, with the command to run it yourself); the agent
side's client run for real against the real bridge and relay of runtime.py with a stand-in server; the live test
itself (tests/test_live_gaps.py::test_live_mcp_server) with a stand-in launcher - a server that works, one that dies
before its handshake (its supervisor.log in the record), one whose tools differ, one whose instance sees the other
workspace; the preparation (asked first, the signed catalog read, the MCP checkout found or cloned, its fixtures); and
the whole way through main() to the report's table."""
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


LG = load("live_gaps_for_mcp_live", ROOT / "scripts" / "live_gaps.py")
RT = load("runtime_for_mcp_live", ROOT / "sandbox" / "runtime.py")

RADARE2 = {"entry": "mcp:radare2", "prefix": "mcp", "name": "radare2", "published": True, "bubblewrap": True,
           "env": [], "mounts": [{"target": "/work", "required": True, "default": "workspace", "mode": "ro"}]}
BRAVE = {"entry": "mcp:brave-search", "prefix": "mcp", "name": "brave-search", "published": True, "bubblewrap": True,
         "env": [{"name": "BRAVE_API_KEY", "secret": True, "required": True}], "mounts": []}
EXEC = {"entry": "mcp:exec", "prefix": "mcp", "name": "exec", "published": True, "bubblewrap": False, "env": [],
        "mounts": [{"target": "/work", "required": True, "default": "workspace", "mode": "ro"}]}
YOUTUBE = {"entry": "mcp:youtube", "prefix": "mcp", "name": "youtube", "published": True, "bubblewrap": True,
           "env": [{"name": "YOUTUBE_CLIENT_SECRET", "secret": True, "required": False},
                   {"name": "YOUTUBE_REGION", "secret": False, "required": False}], "mounts": []}
R2_SPEC = {"name": "radare2", "mounts": [{"host": "${FIXTURES}", "target": "/work"}],
           "expect_tools": ["open_file"],
           "calls": [{"tool": "open_file", "arguments": {"file_path": "/work/hello"}, "expect": "opened"},
                     {"tool": "open_file", "arguments": {"file_path": "/etc/passwd"}, "expect": "only /work",
                      "expect_error": True},
                     {"tool": "list_functions", "arguments": {}, "expect": "main", "runners": ["sandbox-ai-podman"],
                      "runners_why": "needs Podman"}]}


# --- each server's plan -------------------------------------------------------------------------------------------

def test_a_file_server_gets_the_smoke_files_and_a_second_workspace():
    plan = LG.mcp_plan(RADARE2, R2_SPEC, "podman", ["open_file", "list_functions"])
    assert plan["skip"] is None and plan["work"] == "fixtures" and plan["isolation"] is True
    assert plan["add"] == ["--backend", "both"], "its Bubblewrap form too: one add serves both backends"
    assert [c["tool"] for c in plan["calls"]] == ["open_file", "open_file", "list_functions"]
    bwrap = LG.mcp_plan(RADARE2, R2_SPEC, "bwrap")
    assert [c["tool"] for c in bwrap["calls"]] == ["open_file", "open_file"]
    assert bwrap["skipped_calls"] == [("list_functions", "not for sandbox-ai-bwrap: needs Podman")]


def test_a_server_that_needs_a_secret_of_yours_is_skipped_with_the_command():
    spec = {"name": "brave-search", "env": {"BRAVE_API_KEY": "${BRAVE_API_KEY}"}, "calls": []}
    plan = LG.mcp_plan(BRAVE, spec, "podman")
    assert plan["skip"].startswith("needs a value of yours: BRAVE_API_KEY (a secret)")
    assert "sandbox-ai mcp add brave-search --env BRAVE_API_KEY@BRAVE_API_KEY; sandbox-ai claude --backend podman " \
           "--mcp brave-search" in plan["skip"]
    # a required one its spec does not even name: skipped too
    assert LG.mcp_plan(BRAVE, {"calls": []}, "podman")["skip"].startswith("needs a value of yours: BRAVE_API_KEY")


def test_made_up_values_of_the_spec_are_given_a_secret_one_as_a_secret_of_the_checks_own():
    spec = {"env": {"YOUTUBE_CLIENT_SECRET": "GOCSPX-made-up", "YOUTUBE_REGION": "DE", "EMPTY": ""}, "calls": []}
    plan = LG.mcp_plan(YOUTUBE, spec, "podman")
    assert plan["skip"] is None
    assert plan["env"] == [("YOUTUBE_CLIENT_SECRET", "GOCSPX-made-up", True), ("YOUTUBE_REGION", "DE", False)]


def test_no_bubblewrap_form_and_not_published_are_skipped():
    assert LG.mcp_plan(EXEC, {}, "bwrap")["skip"] == "its catalog entry has no Bubblewrap form (rootfs): Podman only"
    assert LG.mcp_plan(EXEC, {}, "podman")["add"] == []
    pending = LG.mcp_plan({**EXEC, "published": False}, {}, "podman")["skip"]
    assert pending.startswith("not published") and "sandbox-ai mcp add exec --build" in pending


def test_a_writable_work_of_its_spec_is_the_start_s_workspace_writable():
    spec = {"mounts": [{"target": "/work", "fresh": True, "writable": True,
                        "seed": [{"path": "a/b.txt", "text": "x"}]}], "calls": []}
    plan = LG.mcp_plan(EXEC, spec, "podman")
    assert plan["work"] == "seed" and plan["seed"] == [{"path": "a/b.txt", "text": "x"}]
    assert plan["add"] == ["--mount", "/work=@workspace:rw"] and plan["writable"]
    assert LG.mcp_plan({**EXEC, "mounts": [{"target": "/work", "mode": "rw"}]}, spec, "podman")["add"] == []


def test_a_call_is_judged_as_the_mcp_repository_s_smoke_test_judges_it():
    ok = {"result": {"content": [{"type": "text", "text": "opened /work/hello"}]}}
    assert LG.mcp_call_passed({"expect": "opened"}, ok)[0] is True
    refused = {"result": {"content": [{"type": "text", "text": "only /work"}], "isError": True}}
    assert LG.mcp_call_passed({"expect": "only /work", "expect_error": True}, refused)[0] is True
    assert LG.mcp_call_passed({"expect": "x"}, {"error": {"code": -1, "message": "x"}})[0] is False
    limited = {"result": {"content": [{"type": "text", "text": "DuckDuckGo answered HTTP 202"}]}}
    assert LG.mcp_call_passed({"expect": "results", "upstream_limited": ["answered HTTP 202"]}, limited)[2] == \
        "answered HTTP 202"
    assert LG.mcp_call_passed({"expect": "x"}, None) == (False, "no answer", None)


# --- the agent side's client, run for real against the real bridge and relay ----------------------------------------

STAND_IN_SERVER = r'''
import json, sys
for line in sys.stdin:
    m = json.loads(line)
    if "id" not in m:
        continue
    if m["method"] == "initialize":
        r = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "r2"}}
    elif m["method"] == "tools/list":
        r = {"tools": [{"name": "open_file", "inputSchema": {"type": "object"}}]}
    else:
        r = {"content": [{"type": "text", "text": "opened " + m["params"]["arguments"].get("file_path", "")}]}
    print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": r}), flush=True)
'''


def test_the_client_speaks_mcp_through_the_real_bridge_and_relay(tmp_path):
    live = load("live_gaps_tests_for_mcp_client", ROOT / "tests" / "test_live_gaps.py")
    directory = tmp_path / "s"
    (directory / "radare2").mkdir(parents=True)
    sock = directory / "radare2" / "server.sock"
    server = tmp_path / "server.py"
    server.write_text(STAND_IN_SERVER)
    threading.Thread(target=RT.mcp_serve, args=(str(sock), [sys.executable, str(server)]),
                     kwargs={"cwd": str(tmp_path), "out": open(os.devnull, "w")}, daemon=True).start()
    for _ in range(100):
        if sock.exists():
            break
        time.sleep(0.05)
    client = live.MCP_CLIENT.replace('"/sandbox/runtime.py"', repr(str(ROOT / "sandbox" / "runtime.py"))) \
        .replace('f"/run/sandbox-mcp/{server}/server.sock"', f'f"{directory}/{{server}}/server.sock"') \
        .replace('["python3", "-I"', f'[{sys.executable!r}, "-I"')
    payload = {"calls": [{"tool": "open_file", "arguments": {"file_path": "/work/hello"}}]}
    done = subprocess.run([sys.executable, "-I", "-c", client, "radare2", json.dumps(payload)], capture_output=True,
                          text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    found = live.answer(done.stdout, "MCP_LIVE_RESULT")
    assert found["initialize"]["result"]["serverInfo"]["name"] == "r2"
    assert [t["name"] for t in found["tools"]["result"]["tools"]] == ["open_file"]
    assert found["calls"][0]["answer"]["result"]["content"][0]["text"] == "opened /work/hello"


def test_the_client_says_a_server_that_ends_before_its_handshake(tmp_path):
    live = load("live_gaps_tests_for_mcp_client_dies", ROOT / "tests" / "test_live_gaps.py")
    directory = tmp_path / "s"
    (directory / "radare2").mkdir(parents=True)
    sock = directory / "radare2" / "server.sock"
    dies = [sys.executable, "-c", "import sys; sys.stderr.write('Error: crun: cannot set limits without cgroups\\n');"
                                  " sys.exit(126)"]
    threading.Thread(target=RT.mcp_serve, args=(str(sock), dies), kwargs={"cwd": str(tmp_path),
                                                                          "out": open(os.devnull, "w")},
                     daemon=True).start()
    for _ in range(100):
        if sock.exists():
            break
        time.sleep(0.05)
    client = live.MCP_CLIENT.replace('"/sandbox/runtime.py"', repr(str(ROOT / "sandbox" / "runtime.py"))) \
        .replace('f"/run/sandbox-mcp/{server}/server.sock"', f'f"{directory}/{{server}}/server.sock"') \
        .replace('["python3", "-I"', f'[{sys.executable!r}, "-I"')
    done = subprocess.run([sys.executable, "-I", "-c", client, "radare2", json.dumps({"calls": []})],
                          capture_output=True, text=True, timeout=120)
    found = live.answer(done.stdout, "MCP_LIVE_RESULT")
    message = found["initialize"]["error"]["message"]
    assert "ended before it answered" in message and "cannot set limits without cgroups" in message
    plan = LG.mcp_plan(RADARE2, R2_SPEC, "podman")
    handshake, _tools, _calls, problems = live.mcp_verdict(plan, found)
    assert handshake.startswith("no: sandbox-ai: the MCP server ended before it answered (exit status 126")


# --- the live test with a stand-in launcher --------------------------------------------------------------------------

FAKE_LAUNCHER = r'''#!/usr/bin/env python3
"""A stand-in for ./sandbox-ai in mcp-live's unit test (SAI_FAKE_MCP: ok, dies, tools, crossed, nostart)."""
import json, os, sys
from pathlib import Path
argv = sys.argv[1:]
log = Path(os.environ["SAI_FAKE_LOG"])
with log.open("a") as out:
    out.write(json.dumps(argv) + "\n")
mode = os.environ.get("SAI_FAKE_MCP", "ok")
starts = [json.loads(line) for line in log.read_text().splitlines() if json.loads(line)[:1] == ["codex"]]
if argv[:2] == ["mcp", "list"]:
    rows = []
    for start in starts:
        name = start[start.index("--mcp") + 1]
        work = start[start.index("--workspace") + 1]
        hosts = [s[s.index("--workspace") + 1] for s in starts] if mode == "crossed" else [work]
        rows.append({"instance": f"{name}.{abs(hash(work)) % 10**8:08d}", "status": "running",
                     "mounts": [{"target": "/work", "host": h, "mode": "ro"} for h in hosts]})
    print(json.dumps([{"name": "radare2", "instances": rows}]))
elif argv[:1] == ["codex"]:
    if mode == "nostart":
        print("agent-sandbox: MCP server radare2: its image is not there", file=sys.stderr)
        sys.exit(1)
    if mode == "dies":
        state = Path(os.environ["XDG_STATE_HOME"]) / "sandbox-ai" / "mcp" / "radare2.1234abcd"
        state.mkdir(parents=True, exist_ok=True)
        (state / "supervisor.log").write_text("bridge up\nmcp-server: Error: OCI runtime error: crun: the requested "
                                              "cgroup controller `pids` is not available\n")
elif argv[:1] == ["shell"]:
    server, payload = argv[-2], json.loads(argv[-1])
    session = argv[1]
    start = next(s for s in starts if s[s.index("--session") + 1] == session)
    work = Path(start[start.index("--workspace") + 1])
    if mode == "dies":
        found = {"initialize": {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "sandbox-ai: the MCP "
                 "server ended before it answered (exit status 126: Error: OCI runtime error: crun: the requested "
                 "cgroup controller `pids` is not available)"}}, "calls": []}
    else:
        tools = ["open_file"] if mode == "tools" else ["open_file", "list_functions"]
        calls = []
        for call in payload["calls"]:
            text = call.get("expect", "")
            if mode == "crossed" and work.name == "wa":
                text += " only-in-workspace-b.txt"
            calls.append({"answer": {"result": {"content": [{"type": "text", "text": text}],
                                                "isError": bool(call.get("expect_error"))}}, "seconds": 0.1})
        found = {"initialize": {"result": {"protocolVersion": "2025-06-18"}}, "handshake_seconds": 2.0,
                 "tools": {"result": {"tools": [{"name": t} for t in tools]}}, "calls": calls}
    print("MCP_LIVE_RESULT " + json.dumps(found))
'''


@pytest.fixture
def live(tmp_path, monkeypatch):
    """tests/test_live_gaps.py as a module, its launcher a stand-in (FAKE_LAUNCHER), its image check a stand-in, the
    catalog, the MCP checkout and the shared home folders of this test's."""
    module = load("live_gaps_tests_for_mcp_live", ROOT / "tests" / "test_live_gaps.py")
    fake = tmp_path / "sandbox-ai"
    fake.write_text(FAKE_LAUNCHER.replace("#!/usr/bin/env python3", f"#!{sys.executable}", 1))
    fake.chmod(0o755)
    monkeypatch.setattr(module, "SAI", fake)
    monkeypatch.setattr(module, "harness_image", lambda agent, env: f"localhost/{agent}-sandbox:latest")
    repo = tmp_path / "repo"
    (repo / "servers" / "radare2").mkdir(parents=True)
    (repo / "servers" / "radare2" / "smoke.json").write_text(json.dumps(R2_SPEC))
    (repo / "servers" / "radare2" / "tools.json").write_text(json.dumps({"tools": ["open_file", "list_functions"]}))
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "hello").write_bytes(b"\x7fELF")
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"entries": [RADARE2, BRAVE], "problems": []}))
    facts = tmp_path / "facts"
    facts.mkdir()
    for key, value in (("SAI_LIVE_MCP_CATALOG", catalog), ("SAI_LIVE_MCP_REPO", repo),
                       ("SAI_LIVE_MCP_FIXTURES", fixtures), ("SAI_LIVE_MCP_HOME", tmp_path / "home"),
                       ("SAI_LIVE_MCP_DIR", facts), ("SAI_FAKE_LOG", tmp_path / "launcher.log")):
        monkeypatch.setenv(key, str(value))
    (tmp_path / "home").mkdir()
    monkeypatch.delenv("SAI_FAKE_MCP", raising=False)
    return SimpleNamespace(module=module, facts=facts, log=tmp_path / "launcher.log")


def run(live, request, tmp_path, name="radare2", backend="podman"):
    sessions = []
    try:
        live.module.test_live_mcp_server(request, backend, tmp_path / f"{name}-{backend}", name, sessions)
    finally:
        for made in sessions:
            made.close()


def found(live, name="radare2", backend="podman"):
    return json.loads((live.facts / f"{name}.{backend}.json").read_text())


def calls(live):
    return [json.loads(line) for line in live.log.read_text().splitlines()]


def test_a_server_that_works_through_both_workspaces(live, request, tmp_path):
    run(live, request, tmp_path)
    record = found(live)
    assert record["outcome"] == "passed" and record["handshake"] == "yes (2 s)"
    assert record["tools"] == "2 = tools.json" and record["calls"] == {"passed": 3, "failed": 0, "skipped": 0}
    assert record["isolation"] == "ok (one instance per workspace)" and record["added"].startswith("yes")
    made = calls(live)
    assert made[0] == ["mcp", "add", "mcp:radare2", "--backend", "both"], "its defaults: no --mount; the signed catalog's"
    starts = [c for c in made if c[:1] == ["codex"]]
    assert len(starts) == 2 and all("--mcp" in c and "radare2" in c for c in starts)
    assert {Path(c[c.index("--workspace") + 1]).name for c in starts} == {"wa", "wb"}
    workspace = Path(starts[0][starts[0].index("--workspace") + 1])
    assert (workspace / "hello").read_bytes() == b"\x7fELF" and (workspace / LG.MCP_MARKERS[0]).is_file()
    # the second backend: added once for both, not again
    run(live, request, tmp_path, backend="bwrap")
    assert [c for c in calls(live) if c[:2] == ["mcp", "add"]] == [made[0]]
    assert found(live, backend="bwrap")["calls"] == {"passed": 2, "failed": 0, "skipped": 1}


def test_a_server_that_dies_before_its_handshake_shows_its_supervisor_log(live, request, tmp_path, monkeypatch):
    monkeypatch.setenv("SAI_FAKE_MCP", "dies")
    with pytest.raises(BaseException, match="no handshake"):
        run(live, request, tmp_path)
    record = found(live)
    assert record["outcome"] == "failed" and record["handshake"].startswith("no: sandbox-ai: the MCP server ended")
    assert "the requested cgroup controller `pids` is not available" in record["supervisor"]


def test_tools_other_than_its_tools_json_fail(live, request, tmp_path, monkeypatch):
    monkeypatch.setenv("SAI_FAKE_MCP", "tools")
    with pytest.raises(BaseException, match="tools/list"):
        run(live, request, tmp_path)
    assert "missing list_functions" in found(live)["tools"]


def test_an_instance_that_sees_the_other_workspace_fails(live, request, tmp_path, monkeypatch):
    monkeypatch.setenv("SAI_FAKE_MCP", "crossed")
    with pytest.raises(BaseException, match="isolation"):
        run(live, request, tmp_path)
    assert found(live)["isolation"].startswith("failed: an instance mounts both workspaces")


def test_a_session_that_does_not_start_is_said(live, request, tmp_path, monkeypatch):
    monkeypatch.setenv("SAI_FAKE_MCP", "nostart")
    with pytest.raises(BaseException, match="did not start"):
        run(live, request, tmp_path)
    assert found(live)["why"] == "agent-sandbox: MCP server radare2: its image is not there"


def test_a_server_that_needs_your_secret_is_skipped_and_recorded(live, request, tmp_path):
    with pytest.raises(BaseException) as caught:
        run(live, request, tmp_path, name="brave-search")
    assert caught.type.__name__ == "Skipped"
    assert found(live, "brave-search")["outcome"] == "skipped"
    assert not live.log.exists() or not calls(live), "nothing added, nothing started"


# --- the preparation, the check's entry, the whole way through main() ------------------------------------------------

def test_mcp_live_is_optional_and_never_joins_a_whole_run():
    check = LG.BY_NAME["mcp-live"]
    assert check.optional and check.tests == ("tests/test_live_gaps.py::test_live_mcp_server",)
    assert "mcp-live" not in [c.name for c in LG.selected(None)]
    assert [c.name for c in LG.selected(["mcp-live"])] == ["mcp-live"]
    assert "mcp-live" not in LG.LLM_CHECKS + LG.LLM_USERS
    args = LG.parser().parse_args(["--only", "mcp-live", "--mcp-servers", "binwalk,radare2"])
    assert args.mcp_servers == "binwalk,radare2"


def test_the_preparation_asks_first_reads_the_catalog_and_writes_the_fixtures(tmp_path):
    repo = tmp_path / "repo"
    (repo / "servers" / "radare2").mkdir(parents=True)
    (repo / "tests" / "fixtures").mkdir(parents=True)
    (repo / "tests" / "fixtures" / "a.json").write_text("{}")
    (repo / "scripts").mkdir()
    (repo / "scripts" / "fixtures.py").write_text("import sys, pathlib\n"
                                                  "pathlib.Path(sys.argv[sys.argv.index('--out') + 1], 'hello')"
                                                  ".write_bytes(b'ELF')\n")
    catalog = {"entries": [RADARE2, {**RADARE2, "prefix": "hub", "name": "nmap"}, BRAVE], "problems": []}
    ran = []

    def runner(command, timeout=60, env=None, cwd=None, input=None):
        ran.append(command)
        if command[1:3] == ["mcp", "catalog"]:
            return 0, json.dumps(catalog), ""
        return LG.run_command(command, timeout=timeout, env=env, cwd=cwd)
    env = {}
    args = LG.parser().parse_args(["--only", "mcp-live", "--mcp-repo", str(repo)])
    assert LG.prepare_mcp(args, tmp_path, env, io.StringIO("n\n"), runner) == \
        "not run: not confirmed (on a terminal it asks; else --yes)"
    assert ran == [], "nothing read or fetched before the yes"
    args = LG.parser().parse_args(["--only", "mcp-live", "--mcp-repo", str(repo), "--yes"])
    assert LG.prepare_mcp(args, tmp_path, env, None, runner) is None
    assert env["SAI_LIVE_MCP_SERVERS"] == "radare2,brave-search", "the signed catalog's (mcp:) servers only"
    assert (Path(env["SAI_LIVE_MCP_FIXTURES"]) / "hello").read_bytes() == b"ELF"
    assert (Path(env["SAI_LIVE_MCP_FIXTURES"]) / "a.json").is_file()
    assert env["SAI_LIVE_MCP_REPO"] == str(repo.resolve())
    args = LG.parser().parse_args(["--only", "mcp-live", "--mcp-repo", str(repo), "--yes", "--mcp-servers",
                                   "radare2,nothere"])
    assert LG.prepare_mcp(args, tmp_path, {}, None, runner) == "--mcp-servers: not in the signed catalog: nothere"
    args = LG.parser().parse_args(["--only", "mcp-live", "--mcp-repo", str(tmp_path / "nope"), "--yes"])
    assert "no servers/ there" in LG.prepare_mcp(args, tmp_path, {}, None, runner)


def test_without_a_checkout_it_is_cloned_after_the_question(tmp_path, monkeypatch):
    monkeypatch.setattr(LG, "mcp_repo", lambda given=None, root=None: None)
    ran = []

    def runner(command, timeout=60, env=None, cwd=None, input=None):
        ran.append(command)
        if command[1:3] == ["mcp", "catalog"]:
            return 0, json.dumps({"entries": [RADARE2]}), ""
        if command[:2] == ["git", "clone"]:
            (Path(command[-1]) / "servers").mkdir(parents=True)
            return 0, "", ""
        return 0, "", ""
    lines = []
    args = LG.parser().parse_args(["--only", "mcp-live"])
    question = io.StringIO("y\n")
    question.isatty = lambda: True
    monkeypatch.setattr(LG, "ask", lambda text, stream_in=None, stream_out=None: lines.append(text) or True)
    assert LG.prepare_mcp(args, tmp_path, {}, question, runner) is None
    assert "cloned from https://github.com/c0decave/sandbox-ai-mcp.git" in lines[0]
    assert ["git", "clone", "--quiet", "--depth", "1", LG.MCP_REPOSITORY, str(tmp_path / "sandbox-ai-mcp")] in ran


JUNIT = """<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest">{}</testsuite></testsuites>"""


def test_the_whole_way_from_the_live_check_to_its_table(tmp_path, monkeypatch):
    """scripts/live_check.sh --only mcp-live --mcp-servers binwalk,radare2 --yes (main, as the entry point runs it)
    on a host whose Podman is a stand-in and whose pytest run is faked to write what test_live_mcp_server writes."""
    home = tmp_path / "home" / "alice"
    home.mkdir(parents=True)
    fake = tmp_path / "fakebin"
    fake.mkdir()
    (fake / "podman").write_text(
        "#!/bin/sh\ncase \"$1 $2\" in\n"
        "  'info --format') echo '{\"version\": {\"Version\": \"5.6.1\"}, \"host\": {\"cgroupVersion\": \"v2\", "
        "\"security\": {\"rootless\": true}}}' ;;\n  'image exists') exit 0 ;;\nesac\n")
    (fake / "unshare").write_text("#!/bin/sh\nexit 0\n")
    for name in ("podman", "unshare"):
        (fake / name).chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}:/usr/bin:/bin")
    monkeypatch.setenv("HOME", str(home))
    for name in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(LG.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(LG, "subordinate_ids", lambda *a, **k: LG.Found(True, "ranges"))
    real = LG.load_live_check()
    monkeypatch.setattr(real, "inside_a_container", lambda: False)
    monkeypatch.setattr(real, "pytest_python", lambda directory: sys.executable)
    monkeypatch.setattr(LG, "load_live_check", lambda: real)
    prepared = {}

    def prepare(args, temporary, env, stdin=None, runner=None):
        prepared.update(servers=args.mcp_servers)
        env.update(SAI_LIVE_MCP_SERVERS="binwalk,radare2")
        args.mcp_said = "2 servers of the signed catalog"
        return None
    monkeypatch.setattr(LG, "prepare_mcp", prepare)
    seen = {}

    def fake_pytest(command, log, timeout, env, cwd):
        junit = Path(next(part for part in command if part.startswith("--junitxml="))[len("--junitxml="):])
        seen.update(command=command, env=env)
        facts = Path(env["SAI_LIVE_MCP_DIR"])
        for name, backend, record in (
                ("radare2", "podman", {"outcome": "passed", "added": "yes (12 s)", "started": "yes",
                                       "handshake": "yes (3 s)", "tools": "32 = tools.json",
                                       "calls": {"passed": 7, "failed": 0, "skipped": 0},
                                       "isolation": "ok (one instance per workspace)", "seconds": 61}),
                ("binwalk", "podman", {"outcome": "failed", "added": "yes (20 s)", "started": "yes",
                                       "handshake": "no: sandbox-ai: the MCP server ended before it answered",
                                       "why": "no handshake", "supervisor": "mcp-server: Error: crun: x",
                                       "seconds": 30})):
            (facts / f"{name}.{backend}.json").write_text(json.dumps({"server": name, "backend": backend, **record}))
        junit.write_text(JUNIT.format('<testcase classname="tests.test_live_gaps" '
                                      'name="test_live_mcp_server[podman-radare2]" time="61"/>'))
        Path(log).write_text("1 passed\n")
        return 0, 90.0
    monkeypatch.setattr(LG, "run_logged", fake_pytest)
    lines = []
    status = LG.main(["--only", "mcp-live", "--mcp-servers", "binwalk,radare2", "--yes", "--out",
                      str(tmp_path / "out"), "--work-dir", str(tmp_path)], out=lines.append, stdin=io.StringIO(""))
    assert status == 0, lines
    assert prepared["servers"] == "binwalk,radare2"
    assert "tests/test_live_gaps.py::test_live_mcp_server" in seen["command"]
    assert seen["env"]["SAI_LIVE_MCP_SERVERS"] == "binwalk,radare2"
    report = json.loads(next((tmp_path / "out").glob("REPORT-*.json")).read_text())
    rows = {row["server"]: row for row in next(c for c in report["checks"] if c["name"] == "mcp-live")["mcp"]}
    assert rows["radare2"]["calls"] == "7 passed, 0 failed" and rows["radare2"]["outcome"] == "passed"
    assert rows["binwalk"]["supervisor"] == "mcp-server: Error: crun: x"
    text = next((tmp_path / "out").glob("REPORT-*.md")).read_text()
    assert "| server | backend | outcome | added | started | handshake | tools | calls | isolation | seconds |" in text
    assert "| radare2 | podman | passed | yes (12 s) | yes | yes (3 s) | 32 = tools.json | 7 passed, 0 failed | " \
           "ok (one instance per workspace) | 61 |" in text
    assert "supervisor.log of binwalk (podman), its last lines:" in text and "mcp-server: Error: crun: x" in text


def test_the_server_is_added_from_the_signed_catalog_by_its_prefix(tmp_path, monkeypatch):
    """The user's first mcp-live run (2026-11-23): binwalk and radare2 are in two catalogs (sandbox-ai-mcp's and
    axross's hub), so `sandbox-ai mcp add binwalk` asked which one and the check failed at once. It adds mcp:NAME."""
    import test_live_gaps as gaps
    monkeypatch.setenv("SAI_LIVE_MCP_HOME", str(tmp_path))
    calls = []

    def sai(env, *argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(gaps, "sai", sai)
    said, problem = gaps.mcp_added({}, "binwalk", {"folders": {}, "add": []}, "podman")
    assert problem is None and said.startswith("yes")
    assert calls == [("mcp", "add", "mcp:binwalk")]
