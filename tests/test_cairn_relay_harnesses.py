"""Every harness of the launcher as Cairn's worker (sandbox/cairn_relay.py), without a change to Cairn: Cairn calls
it through its claudecode driver, the relay maps that driver's command line onto the harness's own one-shot run with
a command line of its own, and translates the harness's output into what the driver reads (the final message on
standard output, the exit status). Here: the exact command line of each harness, the variables it gets, what is
refused, and the call end to end over a real socket with stand-ins of the harnesses that print as the real ones do.

The user (2026-11-22): "why workers only for claude and not all harnesses?" and "shouldnt all harnesses generally be
support? and if not the harnesses supported shown by ampel or desc?" """
import importlib.util
import io
import json
from pathlib import Path
import sys
import threading

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


relay = load("cairn_relay_harness_tests", "sandbox/cairn_relay.py")
helper = load("agent_mcp_cairn_tests", "sandbox/agent_mcp.py")
UUID = "3f2b8c1e-9a4d-4e2f-8b7a-1c2d3e4f5a6b"
SKIP = "--dangerously-skip-permissions"
WORKSPACE = "/work/cairn/runs"
CWD = "/work/cairn/runs/p1"
OWN = ["--model", "m1"]
PROMPT = "Explore the target and answer with a JSON object"
MAPPED = [agent for agent in relay.HARNESSES if agent not in ("claude", "codex")]
# Each harness's command line for Cairn's execute call, with the launcher's arguments OWN (its model) - exactly.
EXPECTED = {
    "opencode": ["opencode", "run", "--model", "m1", "--", PROMPT],
    "opencode2": ["opencode2", "run", "--auto", "--model", "m1", "--", PROMPT],
    "hermes": ["hermes", "--model", "m1", "--oneshot=" + PROMPT],
    "dsh": ["dsh", "headless", "--patch", "/etc/dsh/sandbox-ai.patch.yml", "--model", "m1", "-"],
    "qwen": ["qwen", "--yolo", "--model", "m1", "--prompt=" + PROMPT],
    "goose": ["goose", "run", "--quiet", "--model", "m1", "--text=" + PROMPT],
    "openhands": ["openhands", "task", "--model", "m1", "--text=" + PROMPT],
    "nanobot": ["nanobot", "agent", "--no-markdown", "--session", f"cairn-{UUID}", "--model", "m1",
                f"--message=(sandbox-ai: Cairn's project folder is {CWD}; work there - your tools' relative paths "
                f"start in your own workspace.)\n\n{PROMPT}"],
    "letta": ["letta", "-p", "--new", "--yolo", "--model", "m1", "--", PROMPT],
    "pi": ["pi", "-p", "--model", "m1", "--", PROMPT],
}
ENVIRONMENT = {"opencode": {"OPENCODE_PERMISSION": '{"*":"allow"}'}, "hermes": {"HERMES_YOLO_MODE": "1"},
               "dsh": {"DSH_PERMISSION_MODE": "danger-full-access"}, "goose": {"GOOSE_MODE": "auto"}}


def execute(agent, prompt=PROMPT, driver="claude", session="--session-id"):
    return {"agent": agent, "driver": driver, "argv": [session, UUID, SKIP, "-p", "--", prompt], "cwd": CWD}


def test_every_harness_of_the_launcher_can_be_a_worker():
    launcher = load("agent_sandbox_cairn_harness_tests", "sandbox/agent_sandbox.py")
    assert set(relay.HARNESSES) == launcher.AGENT_PROGRAMS == set(helper.AGENTS)
    assert set(EXPECTED) == set(MAPPED)


@pytest.mark.parametrize("agent", MAPPED)
def test_each_harness_gets_exactly_its_own_one_shot_command_line(agent):
    found = relay.call(execute(agent), {agent: OWN}, WORKSPACE)
    assert found["argv"] == EXPECTED[agent]
    assert found["cwd"] == CWD and found["answer"] == agent, "its output is translated"
    assert found["env"] == ENVIRONMENT.get(agent, {}), "its own switch to work without questions, nothing else"
    assert found["stdin"] == (PROMPT if agent == "dsh" else None), "DeepSeek Harness reads the task on stdin"


@pytest.mark.parametrize("agent", MAPPED)
def test_a_prompt_that_starts_with_a_dash_stays_the_prompt(agent):
    found = relay.call(execute(agent, "--version; rm -rf /"), {agent: []}, WORKSPACE)
    argv = found["argv"]
    if agent == "dsh":
        assert argv[-1] == "-" and found["stdin"] == "--version; rm -rf /"
    elif argv[-2] == "--":
        assert argv[-1] == "--version; rm -rf /"
    else:  # an option's own value: --oneshot=..., --prompt=..., --text=..., --message=...
        assert argv[-1].split("=", 1)[1].endswith("--version; rm -rf /") and argv[-1].startswith("--")
    assert "--version" not in argv and "-c" not in argv


@pytest.mark.parametrize("agent", MAPPED)
def test_the_same_forms_as_the_helpers_one_shot_runs(agent):
    """A harness's one-shot form is known to sandbox-ai once (agent_mcp.command, the helpers'): the relay's is the
    same, with the launcher's arguments where the helper has its switch."""
    mine, _ = relay.harness_argv(agent, PROMPT, [], session=UUID)
    theirs = helper.command(agent, PROMPT, skip=True)
    if agent == "nanobot":  # (its session and the note about the folder are the relay's own)
        assert mine[:3] == theirs[:3] and mine[-1] == "--message=" + PROMPT
        return
    assert mine == theirs


def test_the_switches_are_the_launchers_and_the_helpers():
    launcher = load("agent_sandbox_cairn_switch_tests", "sandbox/agent_sandbox.py")
    for agent in MAPPED:
        assert relay.SKIP_ARGS.get(agent, []) == helper.SKIP[agent], agent
        assert relay.SKIP_ENV.get(agent, {}) == {**launcher.DANGER_ENV[agent], **helper.SKIP_ENV.get(agent, {})}, agent
    assert relay.DSH_PATCH == helper.DSH_PATCH and relay.NANOBOT_HEADER == helper.NANOBOT_HEADER


@pytest.mark.parametrize("agent", [a for a in MAPPED if a != "nanobot"])
def test_cairn_s_conclusion_is_not_run_where_no_session_can_be_named(agent):
    """Cairn's second ask (-r UUID) goes on in the task's own conversation; a harness whose one-shot run cannot be
    named exactly that conversation is never sent into one that may be another's (the lead's own)."""
    with pytest.raises(relay.NoSession, match="conclusion"):
        relay.call(execute(agent, session="-r"), {agent: []}, WORKSPACE)


def test_nanobot_goes_on_in_the_task_s_own_session():
    first = relay.call(execute("nanobot"), {"nanobot": []}, WORKSPACE)
    second = relay.call(execute("nanobot", "Conclude", session="-r"), {"nanobot": []}, WORKSPACE)
    assert first["argv"][3:5] == second["argv"][3:5] == ["--session", f"cairn-{UUID}"]


@pytest.mark.parametrize("request_, said", [
    (execute("opencode", driver="codex"), "claudecode driver, not its codex"),
    ({"agent": "codex", "driver": "claude", "argv": ["--session-id", UUID, SKIP, "-p", "--", "x"], "cwd": CWD},
     "codex driver, not its claude"),
    ({"agent": "opencode", "driver": "claude", "argv": ["exec", relay.CODEX_SKIP, "--", "x"], "cwd": CWD},
     "claudecode driver"),
    ({"agent": "opencode", "driver": "claude", "argv": ["--session-id", UUID, SKIP, "-p", "--model", "x"],
      "cwd": CWD}, "claudecode driver"),
    ({"agent": "opencode", "driver": "claude", "argv": ["run", "--", "x"], "cwd": CWD}, "claudecode driver"),
    ({"agent": "opencode", "driver": "bash", "argv": ["--session-id", UUID, SKIP, "-p", "--", "x"], "cwd": CWD},
     "none of Cairn's drivers"),
    (execute("hermes"), "no worker of this session"),
    ({**execute("opencode"), "cwd": "/root"}, "folder"),
    ({**execute("goose"), "files": {"/root/.config/goose/config.yaml": "x"}}, "graph snapshot"),
])
def test_anything_else_is_refused(request_, said):
    with pytest.raises(ValueError, match=said):
        relay.call(request_, {"opencode": [], "codex": [], "goose": []}, WORKSPACE)


def test_cairn_s_look_at_its_start_is_answered_by_a_worker_its_shim_calls():
    """`claude --help` comes with the dispatcher's own environment (no worker named): the first worker called through
    the claudecode driver answers it; `codex --help` only Codex."""
    assert relay.call({"agent": "claude", "driver": "claude", "argv": ["--help"]}, {"codex": [], "goose": []},
                      WORKSPACE)["argv"] == ["goose", "--help"]
    assert relay.call({"agent": "pi", "driver": "claude", "argv": ["--help"]}, {"pi": []}, WORKSPACE)["argv"] == \
        ["pi", "--help"]
    with pytest.raises(ValueError, match="no worker"):
        relay.call({"agent": "codex", "driver": "codex", "argv": ["--help"]}, {"goose": []}, WORKSPACE)


def test_the_answer_is_the_final_message_alone():
    assert relay.answer_text("opencode", "\x1b[1m\x1b[32mdone\x1b[0m\r\n{\"kind\": \"fact\"}\n") == \
        'done\n{"kind": "fact"}\n'
    nanobot = "nanobot: first start, config written\n{\"not\": \"this\"}\n\N{CAT} nanobot\n{\"kind\": \"fact\"}\n"
    assert relay.answer_text("nanobot", nanobot) == '{"kind": "fact"}\n'
    assert relay.answer_text("goose", "   \n") == ""


# --- end to end: the shim in Cairn's container, the relay, a stand-in of each harness ------------------------------

STAND_IN = r'''#!PYTHON
import json, os, sys
args = sys.argv[1:]
given = sys.stdin.read() if args[-1:] == ["-"] else None
with open(os.environ["FAKE_LOG"], "a") as stream:
    stream.write(json.dumps({"name": os.path.basename(sys.argv[0]), "args": args, "cwd": os.getcwd(), "stdin": given,
                             "env": {k: os.environ.get(k) for k in ("GOOSE_MODE", "DSH_PERMISSION_MODE",
                                                                    "OPENCODE_PERMISSION", "HERMES_YOLO_MODE")}})
                 + "\n")
if args == ["--help"]:
    print("usage")
    sys.exit(0)
# What a harness prints: progress, a tool call with JSON of its own, colours - then the final message.
print("\x1b[2m> tool: bash {\"cmd\": \"ls\"}\x1b[0m", file=sys.stderr)
if os.path.basename(sys.argv[0]) == "nanobot":
    print('first start {"config": "written"}')
    print("\N{CAT} nanobot")
size = int(os.environ.get("FAKE_SIZE", "0"))
print(json.dumps({"kind": "fact", "description": "port 80 is open", "pad": "x" * size}))
sys.exit(4 if "FAIL" in " ".join(args) + (given or "") else 0)
'''


@pytest.fixture
def world(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in relay.HARNESSES:
        (bin_dir / name).write_text(STAND_IN.replace("PYTHON", sys.executable, 1), encoding="utf-8")
        (bin_dir / name).chmod(0o755)
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("PYTHONIOENCODING", "utf-8")
    for name in ("GOOSE_MODE", "DSH_PERMISSION_MODE", "OPENCODE_PERMISSION", "HERMES_YOLO_MODE",
                 relay.WORKER_VARIABLE):
        monkeypatch.delenv(name, raising=False)
    workspace = tmp_path / "work" / "cairn" / "runs"
    socket_path = tmp_path / "r" / "relay.sock"
    cfg = {"socket": str(socket_path), "workspace": str(workspace),
           "workers": {agent: (["--model", "m1"] if agent == "pi" else []) for agent in MAPPED}}
    server = relay.listen(cfg["socket"])
    stop = threading.Event()
    threading.Thread(target=relay.serve, args=(cfg,), kwargs={"server": server, "stop": stop}, daemon=True).start()

    def call(shim, *argv, harness=None):
        out, err = io.BytesIO(), io.BytesIO()
        status = relay.worker(shim, list(argv), path=cfg["socket"], out=out, err=err, cwd=str(workspace / "p1"),
                              harness=harness)
        return status, out.getvalue().decode(), err.getvalue().decode()

    def calls():
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    yield {"call": call, "calls": calls, "workspace": workspace}
    stop.set()
    server.close()


@pytest.mark.parametrize("agent", MAPPED)
def test_cairn_s_claudecode_call_runs_the_harness_and_gets_its_answer_back(world, agent):
    status, out, err = world["call"]("claude", "--session-id", UUID, SKIP, "-p", "--", PROMPT, harness=agent)
    assert status == 0, err
    assert json.loads(out) == {"kind": "fact", "description": "port 80 is open", "pad": ""}, \
        "the final message alone on standard output: the JSON object Cairn looks for"
    assert "tool: bash" in err, "the harness's error output is passed on (Cairn's log shows it)"
    [ran] = world["calls"]()
    assert ran["name"] == agent and ran["cwd"] == str(world["workspace"] / "p1")
    assert ran["args"][-1] == ("-" if agent == "dsh" else EXPECTED[agent][-1].replace(CWD, str(
        world["workspace"] / "p1")) if agent == "nanobot" else EXPECTED[agent][-1])
    assert ran["stdin"] == (PROMPT if agent == "dsh" else None)
    assert {k: v for k, v in ran["env"].items() if v} == ENVIRONMENT.get(agent, {})


def test_a_failing_harness_s_status_reaches_cairn(world):
    status, out, _ = world["call"]("claude", "--session-id", UUID, SKIP, "-p", "--", "FAIL", harness="qwen")
    assert status == 4 and "fact" in out


def test_a_large_answer_comes_back_whole(world, monkeypatch):
    monkeypatch.setenv("FAKE_SIZE", str(600 << 10))  # more than one frame the worker takes
    status, out, err = world["call"]("claude", "--session-id", UUID, SKIP, "-p", "--", PROMPT, harness="goose")
    assert status == 0, err
    assert len(json.loads(out)["pad"]) == 600 << 10


def test_the_conclusion_is_answered_as_not_run_and_runs_nothing(world):
    status, out, err = world["call"]("claude", "-r", UUID, SKIP, "-p", "--", "Conclude", harness="opencode")
    assert status == 1 and out == "" and "conclusion" in err and "refused" not in err
    assert world["calls"]() == []
    status, out, _ = world["call"]("claude", "-r", UUID, SKIP, "-p", "--", "Conclude", harness="nanobot")
    assert status == 0 and world["calls"]()[-1]["args"][3] == f"cairn-{UUID}"


def test_the_worker_names_its_harness_by_the_dispatcher_s_variable(world, monkeypatch):
    monkeypatch.setenv(relay.WORKER_VARIABLE, "letta")
    status, _, err = world["call"]("claude", "--session-id", UUID, SKIP, "-p", "--", PROMPT)
    assert status == 0, err
    assert world["calls"]()[-1]["name"] == "letta"


def test_a_harness_not_of_the_session_and_a_wrong_shim_are_refused(world):
    status, _, err = world["call"]("claude", "--session-id", UUID, SKIP, "-p", "--", PROMPT, harness="claude")
    assert status == relay.REFUSED and "no worker" in err
    status, _, err = world["call"]("codex", "exec", relay.CODEX_SKIP, "--", PROMPT, harness="opencode")
    assert status == relay.REFUSED and "not its codex" in err
    assert world["calls"]() == []


def test_the_look_at_cairn_s_start_is_answered(world):
    status, out, _ = world["call"]("claude", "--help")
    assert status == 0 and out == "usage\n" and world["calls"]()[-1]["name"] == MAPPED[0]
