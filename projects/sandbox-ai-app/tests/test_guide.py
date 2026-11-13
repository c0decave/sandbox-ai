"""The guide (guide.py, no Qt): the tour covers every page of the window, and the node helper asks only what the
node owns, in the launcher's own terms; an answer survives the way to the command line and back."""
import ast
import os
from pathlib import Path

import pytest

from sandbox_ai_app import backend, board as B, guide

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def sai():
    if not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"):
        pytest.skip("set SANDBOX_AI_HOME to a sandbox-ai checkout")
    return backend.SandboxAI()


def window_pages():
    """The page keys of the navigation (ui.SECTIONS), read without importing Qt."""
    tree = ast.parse((ROOT / "sandbox_ai_app/ui.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "SECTIONS" for t in node.targets):
            keys = []
            for section in node.value.elts:
                for page in section.elts[1].elts:
                    keys.append(page.elts[0].value)
            return keys
    raise AssertionError("no SECTIONS in ui.py")


def test_the_tour_walks_every_page_in_the_windows_order():
    assert [step.page for step in guide.TOUR] == window_pages()
    assert all(step.title and step.text and step.try_ for step in guide.TOUR)
    assert guide.tour_step(-3) is guide.TOUR[0] and guide.tour_step(99) is guide.TOUR[-1]


OWN = {"workspace": {"path", "readonly"}, "mount": {"source", "dest", "mode"}, "mcp": {"name"}, "service": {"name", "image"},
       "agent": {"agent", "model", "effort"}, "proxy": {"enabled"}, "home": {"login"}, "session": {"tmux"}}


def test_every_question_is_about_what_its_node_owns(sai):
    options = {o.long: o for o in sai.options()}
    assert set(guide.NODE_QUESTIONS) == set(B.KINDS), "every kind of node has its questions"
    for kind, asked in guide.NODE_QUESTIONS.items():
        assert asked, kind
        for question in asked:
            assert question.form in guide.FORMS, question
            keys = [v for v, _ in question.choices if v] if question.form == "switches" else [question.key]
            for key in keys:
                if key.startswith("--"):
                    assert B.OWNER.get(key) == kind, f"{kind} does not own {key}"
                    assert key in options, f"the launcher has no {key}"
                else:
                    assert key in OWN.get(kind, ()), f"{kind}.{key} is no property of its own"
            if question.form == "choice" and question.key.startswith("--"):
                known = getattr(options[question.key], "choices", None)
                if known:
                    assert {v for v, _ in question.choices if v} <= set(known), question.key


SAMPLES = {"path": "/home/u/src/app", "source": "/home/u/data", "dest": "/mnt/data", "--state-dir": "/home/u/sb",
           "--kit": ["egm", "mine"], "model": "opus", "effort": "high", "--rules": ["/home/u/rules.json"],
           "--no-inspect": ["pinned.example.org"], "--git-token-env": ["https://github.com=GH_TOKEN"],
           "name": "demo", "--llm": "local", "--allow-host": ["api.example.org"], "--vpn": "work",
           "--device": ["/dev/kvm"], "--memory": "4g", "--cpus": "2", "--pids": "1024", "--gui-vnc": "/home/u/vnc",
           "--log-dir": "/home/u/logs", "--image-variant": "gpu", "image": "redis"}


def sample(question):
    if question.form == "flag":
        return question.key != "enabled" or False  # the proxy: turned off, the rest: turned on
    if question.form in ("choice", "switches"):
        return [v for v, _ in question.choices][-1]
    return SAMPLES[question.key]


def test_answers_survive_the_way_to_the_command_line_and_back(sai):
    for kind, asked in guide.NODE_QUESTIONS.items():
        if kind == "network":  # --offline with hosts makes no sense together: asked one by one below
            continue
        board = B.Board()
        board.add("agent", agent="claude")
        board.add("proxy")
        board.add("network")
        node = board.first(kind) if B.KINDS[kind].single and board.first(kind) else board.add(kind)
        knows_effort = "--effort" in {o.long for o in sai.options()}
        asked = [q for q in asked if q.key != "effort" or knows_effort]  # an older launcher has no --effort
        answers = {q.key: sample(q) for q in asked}
        for key, value in guide.props_of(kind, answers).items():
            if value is None:
                node.props.pop(key, None)
            else:
                node.props[key] = value
        argv = B.to_argv(sai, board)
        agent, _, agent_args = B.values(board, sai)
        again = B.from_argv(sai, agent, argv, agent_args)
        found = next(n for n in again.nodes if n.kind == kind)
        for question in asked:
            assert guide.answer_of(found, question) == guide.answer_of(node, question), (kind, question.key, argv)
            assert guide.answer_of(found, question) == answers[question.key], (kind, question.key, argv)
    for question in guide.questions("network"):
        board = B.Board()
        board.add("agent", agent="claude")
        node = board.add("network")
        node.props.update({k: v for k, v in guide.props_of("network", {question.key: sample(question)}).items()
                           if v is not None})
        again = B.from_argv(sai, "claude", B.to_argv(sai, board))
        assert guide.answer_of(again.first("network"), question) == sample(question), question.key


def test_an_empty_answer_takes_the_option_away():
    props = guide.props_of("network", {"--allow-host": "", "--allow-pypi": False, "--offline": False})
    assert props == {"--allow-host": None, "--allow-pypi": None, "--offline": None}
    props = guide.props_of("home", {"login": ""})
    assert props == {"--copy-auth": None, "--no-auth": None}, "automatic: neither switch"
    assert guide.props_of("home", {"login": "--no-auth"}) == {"--copy-auth": None, "--no-auth": True}
    assert guide.props_of("kits", {"--kit": "egm\n\n  mine \n"}) == {"--kit": ["egm", "mine"]}
    assert guide.props_of("workspace", {"path": " /x "}) == {"path": "/x"}
    assert guide.props_of("proxy", {"enabled": False}) == {"enabled": False}


def test_what_a_new_node_still_lacks():
    board = B.Board()
    board.add("agent", agent="claude")
    workspace = board.add("workspace", path="", readonly=False)
    mcp = board.add("mcp", name="")
    assert guide.missing(workspace) == ["Folder"] and guide.missing(mcp) == ["Server"]
    problems = [B.Problem("warning", "the folder is your HOME", workspace.id), B.Problem("error", "other", 999)]
    assert guide.checklist(problems, workspace) == [("error", "Folder is not set yet"),
                                                    ("warning", "the folder is your HOME")]
    workspace.props["path"] = "/src/app"
    assert guide.checklist([], workspace) == []
    assert guide.answer_of(board.add("proxy"), guide.questions("proxy")[0]) is True, "the proxy is on by default"


def test_the_agents_models_and_efforts_are_the_launchers():
    """The app's lists for the agent node and the wizard are the launcher's (a launcher without them: skipped)."""
    from sandbox_ai_app import backend
    launcher = backend.SandboxAI().launcher
    if not hasattr(launcher, "AGENT_EFFORTS"):
        pytest.skip("this sandbox-ai has no --effort yet")
    assert guide.AGENT_MODELS == launcher.AGENT_MODELS and guide.AGENT_EFFORTS == launcher.AGENT_EFFORTS


def test_model_and_effort_reach_the_launcher_as_its_options():
    from sandbox_ai_app import backend
    sai = backend.SandboxAI()
    if "--effort" not in {o.long for o in sai.options()}:
        pytest.skip("this sandbox-ai has no --model/--effort yet")
    board = B.Board()
    board.add("agent", agent="codex", model="gpt-5-codex", effort="high")
    argv = B.to_argv(sai, board)
    assert argv[argv.index("--model") + 1] == "gpt-5-codex" and argv[argv.index("--effort") + 1] == "high"
    assert B.values(board, sai)[2] == []  # not as the agent's own arguments too
    back = B.from_argv(sai, "codex", argv).first("agent").props
    assert back["model"] == "gpt-5-codex" and back["effort"] == "high"
    assert B.values(board)[2] == ["--model", "gpt-5-codex"]  # without the launcher: as before
