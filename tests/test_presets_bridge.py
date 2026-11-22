"""Presets with a bridge (user 2026-11-22: "example presets with a bridge"): a preset's optional "bridge" section
PROPOSES a session bridge between its start and another running session (a value of kind session the human picks);
it is made only once the preset's own start runs, after the human confirmed it on a terminal with what `bridge add
--dry-run` warns about - by the very code of `sandbox-ai bridge add`. Without a terminal the exact command is said.
The sessions here are the bridge tests' fakes (test_bridge.world: runs with real supervisor control sockets and MCP
relays, brokers in threads)."""
import importlib.util
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import home_guard  # noqa: E402

home_guard.ensure()

import test_bridge as tb  # noqa: E402
from test_bridge import world  # noqa: E402,F401 - the fixture

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sandbox_ai_presets_bridge_test", ROOT / "sandbox/presets.py")
p = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(p)
b = tb.b


def minimal(**fields):
    return {"format": "sandbox-ai preset", "version": 1, "id": "mine", "title": "Mine", "description": "d", **fields}


PEER = {"peer": {"label": "The session to watch", "kind": "session", "optional": True}}


def bridged(bridge=None, values=None, **fields):
    return minimal(values={**PEER, **(values or {})}, bridge={"peer": "{peer}", "allow": ["watch"], **(bridge or {})},
                   **fields)


# --- the format -------------------------------------------------------------------------------------------------

def test_a_bridge_section_is_taken_with_every_part_of_bridge_add():
    assert p.problems(bridged()) == []
    full = bridged({"direction": "in", "allow": ["message", "ask", "files:ro:src", "files:rw:out/docs", "watch"],
                    "both": True, "ask": "each", "observer_actions": ["alert", "pause"],
                    "limits": {"rate": 30, "size": "8k", "ask-timeout": 120, "daily": "1m"}, "until": "2h"})
    assert p.problems(full) == []
    assert p.problems(bridged({"until": "18:30"})) == []
    # A path may name a text value of the preset (the folder of the peer's workspace, chosen at the start).
    assert p.problems(bridged({"allow": ["files:ro:{code}"]}, values={"code": {"label": "Folder", "default": "."}})) \
        == []


@pytest.mark.parametrize("bridge, problem", [
    ({"peer": "coder"}, "peer is a value of kind session"),            # fixed in the file: never
    ({"peer": "{other}"}, "peer is a value of kind session"),
    ({"bogus": 1}, "unknown keys"),
    ({"allow": []}, "allow is a non-empty list"),
    ({"allow": "watch"}, "allow is a non-empty list"),
    ({"allow": ["exec"]}, "no capability 'exec'"),
    ({"allow": ["files:ro:/etc"]}, "relative to the peer's workspace"),
    ({"allow": ["files:ro:../up"]}, "'..'"),
    ({"allow": ["files:ro:src/.git"]}, ".git"),
    ({"allow": ["files:ro:a,b"]}, "no capability"),
    ({"allow": ["files:rx:src"]}, "no capability"),
    ({"allow": ["files:ro:{nothing}"]}, "{nothing} is used but not declared"),
    ({"allow": ["files:ro:{peer}"]}, "kind session is the bridge's peer only"),
    ({"direction": "sideways"}, "direction is out"),
    ({"both": "yes"}, "both is true or false"),
    ({"ask": "always"}, "ask is each, first or never"),
    ({"observer_actions": ["alert", "nuke"]}, "observer_actions is a list of"),
    ({"allow": ["message"], "observer_actions": ["alert"]}, "observer_actions go with watch"),
    ({"limits": {"rate": 0}}, "rate: from 1 to 600"),
    ({"limits": {"size": "1g"}}, "size: from 256 to"),
    ({"limits": {"speed": 1}}, "limits are"),
    ({"limits": {"rate": True}}, "rate: from 1 to 600"),
    ({"until": "2026-12-01T10:00"}, "until is a duration"),
    ({"until": "30s"}, "until is a duration"),
])
def test_a_bridge_section_is_refused_with_the_reason(bridge, problem):
    found = p.problems(bridged(bridge))
    assert any(problem in line for line in found), found


def test_a_session_value_is_the_bridges_peer_only_and_never_fixed():
    assert any("not an object" in line or "bridge is an object" in line for line in p.problems(minimal(bridge=[])))
    # A session value without a bridge, with a default (the peer fixed in the file), or used as an option or in the
    # task: refused.
    assert any("kind session is the bridge's peer only" in line for line in p.problems(minimal(values=PEER)))
    with_default = {"peer": {"label": "x", "kind": "session", "default": "coder"}}
    assert any("never has a default" in line for line in p.problems(bridged(values=with_default)))
    assert any("kind session is the bridge's peer only" in line
               for line in p.problems(bridged(options=["--name", "{peer}"])))
    assert any("kind session is the bridge's peer only" in line for line in p.problems(bridged(prompt="see {peer}")))
    # The limits and actions are bridge.py's own.
    assert p.BRIDGE_LIMITS == b.LIMIT_RANGES and p.BRIDGE_ACTIONS == b.OBSERVER_ACTIONS and \
        p.BRIDGE_ASK == b.ASK_POLICIES


def test_ask_never_only_in_a_preset_of_yours():
    """--ask never (no question about the calls that move content across) is the human's own choice: a preset passed on
    (built-in, an extra directory's) may not set it; one of yours may."""
    preset = bridged({"ask": "never"})
    assert any("ask never" in line for line in p.problems(preset, origin="extra"))
    assert p.problems(preset, origin="yours") == []


def test_a_session_value_is_never_required_and_checked_as_bridge_add_names_a_session():
    preset = {**bridged(), "id": "mine"}
    values, found = p.checked_values(preset, {})
    assert found == [] and values == {}, "the start asks for it on a terminal"
    assert p.checked_values(preset, {"peer": "coder"})[0] == {"peer": "coder"}
    assert p.checked_values(preset, {"peer": "lab1:coder"})[0] == {"peer": "lab1:coder"}
    for bad in ("two words", "-x", "a;b", "a/b"):
        with pytest.raises(ValueError, match="peer must be a session"):
            p.checked_values(preset, {"peer": bad})


# --- the proposal and the command -------------------------------------------------------------------------------

def test_the_proposal_and_its_bridge_add_command():
    preset = {**bridged({"allow": ["ask", "files:ro:{code}"], "limits": {"rate": 30}, "until": "8h"},
                        values={"code": {"label": "Folder", "default": "."}}), "id": "pair"}
    proposal = p.bridge_proposal(preset, {"peer": "coder", "code": "src"})
    assert proposal == {"preset": "pair", "peer": "coder", "direction": "out", "allow": ["ask", "files:ro:src"],
                        "both": False, "ask": "first", "observer_actions": None, "limits": [["rate", "30"]],
                        "until": "8h", "agent_sees": True}
    assert p.bridge_argv(proposal, "rev") == ["add", "rev", "coder", "--allow", "ask,files:ro:src", "--ask", "first",
                                              "--limit", "rate=30", "--until", "8h"]
    assert p.bridge_command(proposal, "rev") == \
        "sandbox-ai bridge add rev coder --allow ask,files:ro:src --ask first --limit rate=30 --until 8h"
    # Without a peer the command names its place; the other direction swaps the two.
    assert p.bridge_command({**proposal, "peer": None}, "rev").startswith("sandbox-ai bridge add rev PEER ")
    watch = p.bridge_proposal({**bridged({"direction": "in", "observer_actions": ["alert", "hint"], "both": True}),
                               "id": "w"}, {})
    assert p.bridge_argv(watch, "me", "lab1:guard")[:3] == ["add", "lab1:guard", "me"]
    assert "--both" in p.bridge_argv(watch, "me", "x") and "--observer-actions" in p.bridge_argv(watch, "me", "x")
    # A path filled in that bridge add would refuse is refused before anything is asked.
    with pytest.raises(ValueError, match="'..'"):
        p.bridge_proposal(preset, {"code": "../x"})
    assert p.bridge_proposal(minimal(), {}) is None


def test_which_agents_see_the_bridge():
    """Only Claude Code takes an MCP server while it runs: the start of another agent says so when its side gets a tool
    of the bridge (as the Board's bridge_problems and bridge add's warning)."""
    out = p.bridge_proposal({**bridged(), "id": "o"}, {})
    assert out["agent_sees"] is True
    assert p.bridge_agent_note(out, "claude") is None
    assert "codex reads its MCP servers only when it starts" in p.bridge_agent_note(out, "codex")
    watched = p.bridge_proposal({**bridged({"direction": "in"}), "id": "o"}, {})
    assert watched["agent_sees"] is False and p.bridge_agent_note(watched, "codex") is None
    asked = p.bridge_proposal({**bridged({"direction": "in", "allow": ["ask"]}), "id": "o"}, {})
    assert asked["agent_sees"] is True


def test_presets_show_lists_the_bridge_part(capsys):
    out = []
    preset = {**bridged({"allow": ["watch"], "observer_actions": ["alert", "pause"], "until": "2h"}), "id": "o",
              "_origin": "built-in", "_path": "/x/o.json"}
    p.describe(preset, out.append)
    text = "\n".join(out)
    assert "value peer: The session to watch [session] (optional)" in text
    assert "bridge: proposed once the start runs, made only when you confirm it: this start -> {peer} " \
           "(watch; observer actions alert,pause; --ask first; until 2h)" in text


# --- the CLI: asked on a terminal, said without one -------------------------------------------------------------

def spawn_in(world_):
    def spawn(ident):
        broker = b.make_broker(ident)
        broker.record_broker()
        broker.start()
        thread = threading.Thread(target=broker.run, kwargs={"interval": 0.05}, daemon=True)
        thread.start()
        world_.brokers.append((broker, thread))
    return spawn


@pytest.fixture
def two(world, monkeypatch):  # noqa: F811
    """obs (the preset's start, just up) and coder run; bridge add's own code makes the bridge (its brokers in
    threads)."""
    world.make("obs"), world.make("coder")
    monkeypatch.setattr(b, "spawn_broker", spawn_in(world))
    return world


def observer_proposal(**values):
    return p.bridge_proposal(p.catalog()["observer"], values)


def propose(two, proposal, answers, tty=True):
    said, asked = [], []

    def answer(question):
        asked.append(question)
        if not answers:
            raise EOFError
        return answers.pop(0)
    made = p.propose_bridge(proposal, "obs", out=said.append, tty=tty, answer=answer, bridge=b,
                            starts=two.starts)
    return made, said, asked


def test_asked_on_a_terminal_and_made_by_bridge_adds_own_code(two):
    made, said, asked = propose(two, observer_proposal(peer="coder"), ["y"])
    assert made is True
    assert asked == ["agent-sandbox: bridge this start (obs) to coder with watch (observer actions: alert)? [y/N] "]
    text = "\n".join(said)
    assert "the preset observer proposes a bridge" in text
    assert "bridge (dry run, nothing made): obs -> coder (watch)" in text, "the warnings are shown before the question"
    rows = b.listing()
    assert [(row["a"], row["b"], row["a_to_b"], row["state"]) for row in rows] == [("obs", "coder", "watch", "active")]
    assert sorted(two.runs["obs"].relays.names()) == ["bridge-coder"]


def test_declined_on_a_terminal_nothing_is_made_and_the_command_is_said(two):
    for reply in (["n"], [""], []):  # no, Enter, the end of input (Ctrl-D)
        made, said, _ = propose(two, observer_proposal(peer="coder"), list(reply))
        assert made is False and b.all_ids() == []
        assert said[-1] == ("no bridge made; to make it later: sandbox-ai bridge add obs coder --allow watch --ask "
                            "first --observer-actions alert")


def test_the_peer_is_chosen_from_the_running_sessions(two):
    two.make("docs")
    made, said, asked = propose(two, observer_proposal(), ["2", "yes"])
    assert "running sessions to bridge with: 1) coder (claude)  2) docs (claude)" in said
    assert asked[0].startswith("agent-sandbox: bridge this start to which session?")
    assert made is True and b.listing()[0]["b"] == "docs"
    # A name is taken too, an empty answer is no bridge, a wrong one is said.
    b.revoke(b.listing()[0]["id"], out=lambda line: None)
    made, said, _ = propose(two, observer_proposal(), [""])
    assert made is False and said[-1].startswith("no bridge made; to make it later: sandbox-ai bridge add obs PEER")
    made, said, _ = propose(two, observer_proposal(), ["no such;name"])
    assert made is False and any("not a session" in line for line in said)


def test_without_a_terminal_only_the_command_is_said(two):
    made, said, asked = propose(two, observer_proposal(peer="coder"), ["y"], tty=False)
    assert made is False and asked == [] and b.all_ids() == []
    assert said == ["note: the preset observer proposes a bridge, made only once you confirm it: sandbox-ai bridge add "
                    "obs coder --allow watch --ask first --observer-actions alert"]


def test_a_bridge_add_that_refuses_makes_nothing_and_asks_nothing(two):
    made, said, asked = propose(two, observer_proposal(peer="gone"), ["y"])
    assert made is False and asked == [] and b.all_ids() == []
    assert said[-1].startswith("not bridged; once it can be: sandbox-ai bridge add obs gone")


# --- the launcher: the proposal reaches the start; a session asks once it is up ---------------------------------

@pytest.fixture(scope="module")
def launcher():
    spec = importlib.util.spec_from_file_location("agent_sandbox_presets_bridge", ROOT / "sandbox/agent_sandbox.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_dry_run_plan_carries_the_proposal(launcher, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(launcher, "load_policy", lambda path=None: {})
    monkeypatch.setattr(launcher, "resolve_hosts", lambda hosts: {h: ["192.0.2.80"] for h in hosts})
    work = tmp_path / "w"
    work.mkdir()
    assert launcher.launch(["claude", "--backend", "podman", "--workspace", str(work), "--no-tmux", "--preset",
                            "observer", "--preset-value", "peer=coder", "--dry-run"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["preset_bridge"]["peer"] == "coder" and plan["preset_bridge"]["allow"] == ["watch"]
    assert launcher.launch(["claude", "--backend", "podman", "--workspace", str(work), "--no-tmux",
                            "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["preset_bridge"] is None, "only a start of that preset"


def test_a_new_session_proposes_it_once_it_is_up_then_attaches(launcher, monkeypatch, tmp_path):
    """sessions.start: after the new session is up (its terminal ready) and before this terminal attaches to it; never
    for a session that was there already (started again or attached)."""
    sessions = launcher.load_sessions_module()
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    order, proposals = [], []

    def start_locked(a, argv, key=None):
        sessions.save(key, {"agent": "claude", "options": [], "agent_args": [], "name": "obs", "created": "x"})
        order.append("up")
        return 0
    monkeypatch.setattr(sessions, "start_locked", start_locked)
    monkeypatch.setattr(sessions, "attach_if_terminal", lambda key: order.append("attach") or 0)
    monkeypatch.setattr(launcher, "propose_preset_bridge", lambda a, name: proposals.append(name) or
                        order.append("proposed"))
    monkeypatch.setattr(sessions, "launcher", lambda: launcher)
    a = SimpleNamespace(session=None, start_name="obs", agent="claude", preset_bridge={"preset": "observer"})
    assert sessions.start(a, ["claude"]) == 0
    assert order == ["up", "proposed", "attach"] and proposals == ["obs"]
    order.clear()
    a.preset_bridge = None
    a.start_name = "obs2"
    assert sessions.start(a, ["claude"]) == 0
    assert order == ["up", "attach"], "no preset bridge: nothing proposed"


def test_propose_preset_bridge_never_fails_the_start(launcher, monkeypatch, capsys):
    monkeypatch.setattr(launcher.load_presets_module(), "propose_bridge",
                        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("broken")))
    launcher.propose_preset_bridge(SimpleNamespace(preset_bridge={"preset": "observer"}), "obs")
    assert "the preset's bridge was not proposed: broken" in capsys.readouterr().err


def test_through_sandbox_ai_a_foreground_start_says_the_command(tmp_path):
    """./sandbox-ai claude --preset pair-reviewer ... in a script (live_fakes' Podman stops the start at its pod): the
    proposal is said with the exact bridge add command before the sandbox runs (no session to ask once it is up)."""
    from live_fakes import Host
    host = Host(tmp_path)
    result = host.run("claude", "--no-tmux", "--backend", "podman", "--workspace", str(host.workspace), "--preset",
                      "pair-reviewer", "--preset-value", "coder=web", "--preset-value", "code=src", "--name", "rev")
    assert "proposes a bridge" in result.stderr, result.stderr
    assert "sandbox-ai bridge add rev web --allow ask,files:ro:src --ask first" in result.stderr
    shown = host.run("presets", "show", "pair-reviewer")
    assert shown.returncode == 0 and "bridge: proposed once the start runs" in shown.stdout


# --- the built-in presets with a bridge -------------------------------------------------------------------------

def test_the_built_in_presets_with_a_bridge():
    presets = p.catalog()
    with_bridge = {ident for ident, preset in presets.items() if preset.get("bridge")}
    assert {"observer", "pair-reviewer", "docs-from-code"} <= with_bridge
    for ident in with_bridge:
        preset = presets[ident]
        assert preset["notes"] and preset["translations"]["de"]["title"], ident
        assert preset["agents"] == ["claude"], "only Claude Code sees a bridge made while it runs"
        assert preset["bridge"]["peer"] == "{" + next(n for n, s in preset["values"].items()
                                                      if s.get("kind") == "session") + "}"
        assert p.bridge_proposal(preset, {})["agent_sees"], ident
    observer = presets["observer"]["bridge"]
    assert observer["allow"] == ["watch"] and observer["observer_actions"] == ["alert"]
    reviewer = presets["pair-reviewer"]
    assert reviewer["bridge"]["allow"] == ["ask", "files:ro:{code}"], "asks the coder, reads its code, writes nothing"
    assert reviewer["category"] == "development"
    docs = presets["docs-from-code"]
    assert docs["bridge"]["allow"] == ["message", "files:ro:{code}"] and docs["category"] == "writing"
    for preset in (reviewer, docs):
        assert not any(cap.startswith("files:rw") for cap in preset["bridge"]["allow"])
