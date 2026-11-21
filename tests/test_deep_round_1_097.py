"""Deep test round 1 of 0.1.96's new features: sandbox-ai shell, the session bridge, OpenHands, nanobot and Letta Code.

Each test is named after the bug it found (or the feature it drives end to end where none did). The launcher runs as
`./sandbox-ai` in a subprocess wherever that is possible here (tests/live_fakes.py: a user's host with only Podman,
tmux and npm faked); the bridge, whose add and revoke refuse to run inside a sandbox, runs with test_bridge.py's world
(real supervisor control sockets and MCP relays, the broker in a thread)."""
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from live_fakes import Host  # noqa: E402


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / "sandbox" / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


AGENT_IMAGES = ("localhost/openhands-sandbox:latest", "localhost/nanobot-sandbox:latest",
                "localhost/letta-sandbox:latest")


@pytest.fixture
def host():
    directory = tempfile.mkdtemp(prefix="deep1-", dir=os.environ.get("TMPDIR") or "/tmp")
    found = Host(directory)
    found.add_image(*AGENT_IMAGES)
    yield found
    subprocess.run(["rm", "-rf", directory], check=False)


def refused(host, *args):
    """./sandbox-ai ARGS (a dry run) refused before anything starts: its error text."""
    done = host.run(*args, timeout=180)
    assert done.returncode == 1, (args, done.stdout[-500:], done.stderr[-1500:])
    assert host.stopped_at() is None
    return done.stderr


def taken(host, *args):
    done = host.run(*args, timeout=180)
    assert done.returncode == 0, (args, done.stderr[-1500:])
    return json.loads(done.stdout)


# --- the agents' own arguments ------------------------------------------------------------------------------------

def test_openhands_canvas_host_given_as_an_attached_short_option_is_refused(host):
    """`-H0.0.0.0` (a short option with its value attached, as Node's parsers take it) passed the check of the
    Canvas' address: `--host 127.0.0.1` was put before it, and the later -H wins - the Canvas off the loopback."""
    err = refused(host, "openhands", "--dry-run", "--", "-H0.0.0.0")
    assert "loopback only" in err
    cli = load("deep1_openhands_cli", "openhands_cli.py")
    assert cli.refused(["-H0.0.0.0"]) and cli.refused(["--port", "9000", "-H::"])  # the image's command as well
    assert cli.refused(["-H127.0.0.1"]) is None
    assert taken(host, "openhands", "--dry-run", "--", "-H127.0.0.1")["agent_args"] == ["-H127.0.0.1"]


def test_nanobot_attached_short_option_values_are_refused(host):
    """`-H0.0.0.0` (nanobot gateway's address) and `-c/other.json` (another configuration file, without sandbox-ai's
    settings) passed the check: click takes a short option's value attached."""
    err = refused(host, "nanobot", "--dry-run", "--nanobot-enable", "scheduler", "--", "gateway", "-H0.0.0.0")
    assert "loopback only" in err
    err = refused(host, "nanobot", "--dry-run", "--", "-c/tmp/other.json")
    assert "nanobot keeps that file" in err
    nanobot = load("deep1_nanobot", "nanobot.py")
    assert nanobot.refused_arg(["gateway", "-H127.0.0.1"], ["scheduler"]) is None


def test_letta_server_every_listen_address_is_checked(host):
    """Only the first --listen of `letta server` was checked: a second one, off the loopback, passed."""
    err = refused(host, "letta", "--dry-run", "--", "server", "--listen", "ws://127.0.0.1:8283",
                  "--listen=ws://0.0.0.0:8283")
    assert "loopback only" in err


def test_the_new_agents_presets_and_the_observer_start_end_to_end(host):
    """The presets of 0.1.96 through ./sandbox-ai AGENT --preset NAME (a dry run): what each needs is asked for by
    name (the key's secret, an LLM server), and once it is there the start goes through with its agent's features
    off and its telemetry pinned; the observer preset is Claude Code with a read-only workspace in the mode high."""
    assert "sandbox-ai secrets set ANTHROPIC_API_KEY" in refused(host, "nanobot", "--preset", "coding-nanobot",
                                                                 "--dry-run")
    assert "sandbox-ai llm add ollama" in refused(host, "letta", "--preset", "letta-local-llm", "--dry-run")
    secret = subprocess.run(host.command("secrets", "set", "ANTHROPIC_API_KEY"), input="sk-ant-api03-" + "x" * 40,
                            capture_output=True, text=True, env=host.env, cwd=host.workspace, timeout=60)
    assert secret.returncode == 0, secret.stderr
    assert host.run("llm", "add", "ollama", "--kind", "ollama", "--url", "http://127.0.0.1:11434").returncode == 0
    for agent, preset in (("openhands", "coding-openhands"), ("nanobot", "coding-nanobot"), ("letta", "coding-letta"),
                          ("openhands", "openhands-local-llm"), ("nanobot", "nanobot-local-llm"),
                          ("letta", "letta-local-llm")):
        values = ["--preset-value", "llm=ollama:qwen3-coder"] if preset.endswith("local-llm") else []
        plan = taken(host, agent, "--preset", preset, *values, "--dry-run")
        assert plan["agent"] == agent and plan[agent]["enabled"] == [], preset
        assert plan[agent]["telemetry"]["environment"], preset
        if preset.startswith("coding-") and agent != "openhands":
            assert [key["secret"] for key in plan["api_keys"]] == ["ANTHROPIC_API_KEY"], preset
        if preset.endswith("local-llm"):
            assert plan["llm"], preset
    host.add_image("localhost/claude-sandbox:latest")
    plan = taken(host, "claude", "--preset", "observer", "--dry-run")
    assert plan["mode"]["name"] == "high" and plan["workspace_mode"] == "ro"


def test_letta_effort_refusal_names_letta_codes_own_setting_not_opencodes(host):
    """`sandbox-ai letta --effort high` was refused with OpenCode's words: "sets its effort per model in its own
    configuration (opencode.json, a model's options)" - a file Letta Code has not got. It sets reasoning per model with
    its /model command."""
    err = refused(host, "letta", "--dry-run", "--effort", "high")
    assert "opencode.json" not in err and "/model" in err
    assert "opencode.json" in refused(host, "opencode", "--dry-run", "--effort", "high")


# --- the session bridge ---------------------------------------------------------------------------------------------

from test_bridge import answer_questions, ap, b, call, text, tools, wait_for, world  # noqa: E402,F401


def test_bridge_paths_with_control_characters_are_refused(world):
    """A path with a line break (or another control character) reached the write's question: its diff's header lines
    `+++ b/PATH` then held lines the peer chose, in what the human reads before Allow. The app refused such paths
    already (bridges.relative_path); now the launcher does as well, for every tool and for --allow."""
    for bad in ("notes\n+++ b/README.md", "a\rb", "x\x1b[2Jy", "tab\there", "del\x7f", "c1\x9b"):
        with pytest.raises(ValueError, match="control character"):
            b.path_parts(bad)
        with pytest.raises(ValueError, match="control character"):
            b.parse_caps(f"files:ro:{bad}")
    assert b.path_parts("src/ü ñ.txt") == ["src", "ü ñ.txt"]
    world.make("web")
    docs = world.make("docs")
    (docs.folder / "README.md").write_text("old\n")
    world.add("web", "docs", "files:rw:.")
    result = call(world.runs["web"], "bridge-docs", "write_file",
                  {"path": "notes\n+++ b/README.md\n+harmless", "content": "x\n"})
    assert result["isError"] and "control character" in text(result)
    assert ap.questions(ap.bridge_runs()) == []  # nothing was asked
    assert sorted(p.name for p in docs.folder.iterdir()) == ["README.md"]


def dead_pid():
    child = subprocess.Popen(["true"])
    child.wait()
    return child.pid


def test_approvals_list_no_open_question_of_a_bridge_whose_broker_is_gone(tmp_path):
    """A question of a bridge whose broker ended without answering it (killed, the machine restarted) stayed open
    for ever: sandbox-ai approvals and the app's Approvals page listed it, Allow and all, although nothing waits
    for it any more. Only the questions of a bridge whose broker runs are open."""
    root = tmp_path / "sandbox-ai" / "bridges"
    bridges = (("0badc0de", "active", dead_pid()), ("0000dead", "ended", os.getpid()),
               ("1111aaaa", "active", os.getpid()), ("2222bbbb", "suspended", os.getpid()))
    for number, (ident, state, pid) in enumerate(bridges):
        folder = root / ident / "approvals"
        folder.mkdir(parents=True)
        (root / ident / "bridge.json").write_text(json.dumps({"id": ident, "state": state, "broker_pid": pid}))
        (folder / f"1700000000-7-{number}.json").write_text(json.dumps({
            "id": f"1700000000-7-{number}", "kind": "bridge", "bridge": ident, "tool": "read_file", "from": "web",
            "to": "docs", "arguments": "read x", "arguments_full": "{}"}))
    (root / "3333cccc" / "approvals").mkdir(parents=True)  # no record at all
    runs = ap.bridge_runs(tmp_path)
    assert sorted(runs) == ["bridge-1111aaaa", "bridge-2222bbbb"]
    assert sorted(q["bridge"] for _, _, q in ap.questions(runs)) == ["1111aaaa", "2222bbbb"]


def test_two_peers_whose_names_clean_to_the_same_mcp_name_can_both_be_bridged(world):
    """bridge-PEER is the peer's name with what an MCP name cannot hold made '-': my.app and my-app both became
    bridge-my-app, and the second bridge of a session to them was refused ("has an MCP server named bridge-my-app
    already") with nothing the human could do about it. The second one is named by its bridge's id instead."""
    web = world.make("web")
    world.make("my-app")
    world.make("my.app")
    first = world.add("web", "my-app", "message")
    second = world.add("web", "my.app", "message")
    assert first["a"]["server"] == "bridge-my-app"
    assert second["a"]["server"] == f"bridge-{second['id']}"
    assert "send_message" in tools(web, f"bridge-{second['id']}")
    result = call(web, f"bridge-{second['id']}", "send_message", {"text": "hello"})
    assert not result["isError"], text(result)
    assert b.load(second["id"])["a"]["server"] == f"bridge-{second['id']}"


def test_a_watched_agent_is_not_warned_that_it_does_not_see_the_bridge(world):
    """`bridge add OBSERVER PEER --allow watch` (the observer preset's use) warned that the watched peer, any agent but
    Claude Code, "does not see" the bridge's server - although it gets no tool of it but info and needs none: the
    observer does the watching. Only a side that gets tools of its own (or an inbox: messages, questions, hints) is
    warned."""
    world.make("guard")
    world.make("worker", agent="codex")
    out = []
    assert b.main(["add", "guard", "worker", "--allow", "watch", "--dry-run", "--json"], out=out.append) == 0
    assert not any("reads its MCP servers only when it starts" in w for w in json.loads(out[-1])["warnings"])
    for allow, actions in (("watch", "alert,hint"), ("message", None), ("files:ro:.", None)):
        out.clear()
        assert b.main(["add", "guard", "worker", "--allow", allow, "--dry-run", "--json",
                       *(["--observer-actions", actions] if actions else [])], out=out.append) == 0
        warnings = json.loads(out[-1])["warnings"]
        if allow == "files:ro:.":
            assert not any("worker runs codex" in w for w in warnings), "reading its files needs nothing of it"
        else:
            assert any("worker runs codex, which reads its MCP servers only when it starts" in w for w in warnings)
    out.clear()
    assert b.main(["add", "worker", "guard", "--allow", "watch", "--dry-run", "--json"], out=out.append) == 0
    assert any("worker runs codex" in w for w in json.loads(out[-1])["warnings"]), "the observer itself is warned"


# --- sandbox-ai shell end to end ------------------------------------------------------------------------------------

@pytest.fixture
def running_start(host):
    """A running session "web" of HOST's user as ./sandbox-ai finds it: its supervisor (a process whose command line
    names the session's tmux folder, as the launcher's does), its run in the registry with an activity log, and the
    sandbox's shell relay (runtime.py --internal-shell-relay, the real one) on its socket."""
    state = host.home / ".local/state" / "sandbox-ai" / "sessions" / "web"
    tmux = state / "tmux"
    for folder in (state / "home", tmux):
        folder.mkdir(parents=True)
    supervisor = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)", "agent_sandbox.py",
                                   "--tmux-socket-dir", str(tmux)])
    logs = host.directory / "logs"
    logs.mkdir()
    (state / "session.json").write_text(json.dumps({
        "agent": "codex", "options": ["--backend", "podman"], "agent_args": [], "backend": "podman",
        "workspace": str(host.workspace), "cwd": str(host.workspace), "created": "2026-11-14T10:00:00Z",
        "pod": "agent-session-web", "name": "web", "pid": supervisor.pid, "started": "2026-11-14T10:00:01Z"}))
    run = host.runtime / "sandbox-ai" / "runs" / "session-web"
    run.mkdir(parents=True)
    (run / "run.json").write_text(json.dumps({"run": "session-web", "agent": "codex", "kind": "agent",
                                              "pid": supervisor.pid, "start": "web", "log_dir": str(logs)}))
    work = host.directory / "sandbox-work"
    work.mkdir()
    relay = subprocess.Popen([sys.executable, "-I", "-S", str(ROOT / "sandbox" / "runtime.py"),
                              "--internal-shell-relay", str(tmux / "shell.sock")], cwd=work)
    (tmux / "attach.sock").touch()
    assert wait_for(lambda: (tmux / "shell.sock").exists(), 10)
    yield logs
    for process in (relay, supervisor):
        process.kill()
        process.wait()


def test_shell_end_to_end_through_the_launcher(host, running_start):
    """./sandbox-ai shell NAME -- COMMAND (no terminal: -n is the default without one) runs COMMAND through the
    sandbox's shell relay: its output and error output apart, its input to its end, its exit status passed on, and
    the human's shell in the start's activity timeline with a credential in its command redacted."""
    done = host.run("shell", "web", "--", "sh", "-c", "echo inside; echo oops >&2; exit 7")
    assert (done.returncode, done.stdout) == (7, "inside\n") and "oops" in done.stderr, done.stderr[-800:]
    piped = subprocess.run(host.command("shell", "web", "-n", "--", "sh", "-c", "tr a-z A-Z; exit 3"),
                           input="hello\nworld\n", capture_output=True, text=True, cwd=host.workspace,
                           env=host.env, timeout=60)
    assert (piped.returncode, piped.stdout) == (3, "HELLO\nWORLD\n"), piped.stderr[-800:]
    secret = "Authorization: Bearer abcdefgh12345678abcdefgh"
    assert host.run("shell", "web", "--", "true", secret).returncode == 0
    records = [json.loads(line) for line in (running_start / "activity.jsonl").read_text().splitlines()]
    summaries = [r["summary"] for r in records if r.get("kind") == "session"]
    assert "session web shell closed (exit status 7)" in summaries
    assert any("the human ran in its sandbox: sh -c" in s for s in summaries)
    assert "abcdefgh12345678abcdefgh" not in (running_start / "activity.jsonl").read_text()
    # Refused with the reason, nothing run: a word that is no command's, a start that does not run.
    wrong = host.run("shell", "web", "ls")
    assert wrong.returncode == 2 and "a command goes after --" in wrong.stderr
    missing = host.run("shell", "nothing", "--", "true")
    assert missing.returncode == 1 and "nothing named nothing" in missing.stderr
    # Two at once, each with its own exit status.
    first, second = (host.popen("shell", "web", "--", "sh", "-c", f"sleep 1; echo {n}; exit {n}") for n in (4, 5))
    assert [(p.wait(60), p.stdout.read()) for p in (first, second)] == [(4, "4\n"), (5, "5\n")]
    for process in (first, second):
        process.stdout.close()
        process.stderr.close()
    # Paused: refused with the way out.
    state = host.home / ".local/state" / "sandbox-ai" / "sessions" / "web"
    (state / "paused").touch()
    paused = host.run("shell", "web", "--", "true")
    assert paused.returncode == 1 and "sandbox-ai resume web" in paused.stderr
    (state / "paused").unlink()
    # A start of a sandbox-ai before 0.1.97 (no shell relay): pointed to its tmux window "shell".
    (state / "tmux" / "shell.sock").unlink()
    old = host.run("shell", "web", "--", "true")
    assert old.returncode == 1 and "sandbox-ai attach web" in old.stderr and "--restart" in old.stderr


# --- the bridge across hosts ----------------------------------------------------------------------------------------

from test_bridge_remote import hostile, two  # noqa: E402,F401


def test_an_alert_from_the_other_host_whose_side_may_not_alert_is_dropped(two, world):  # noqa: F811
    """The other host's half could send alert events for a side that is no observer there (no watch, no alert
    action): each became an alert of this host's bridge (a notification, a line in its record, an activity event, a
    finding of the report). Neither half takes the other's word for what was granted: only an observer's alerts
    count."""
    world.make("alpha")
    sent = {}

    def script(peer):
        for number in range(3):
            peer.send({"t": "ev", "op": "alert", "body": {"severity": "critical", "text": f"FAKE {number}"}})
        sent["done"] = True
    two.script["run"] = hostile(script)
    data = world.add("alpha", "host1:beta", "message")  # beta may do nothing to alpha
    assert wait_for(lambda: sent.get("done"), 30)
    time.sleep(0.5)
    assert b.load(data["id"]).get("alerts") == []
    assert not any("FAKE" in body for _, body, _ in world.notes)
    assert b.load(data["id"])["state"] == "active"

