"""Board review 1 (interplay and correctness): a corpus of generated boards - every part kind, random combinations,
edge values - whose command line is read by the launcher's own parser and compared with what the board means.

Properties, for every board of the corpus:
- the launcher's parser accepts the board's start (launcher_argv, agent arguments after --) whenever every value is
  one its own types take: no value is eaten as an option, none is lost between two options;
- every option the board sets arrives as the board means it (values(): flags, choices, lists item by item, values);
- the agent and its arguments arrive after -- untouched;
- the project file's own keys the board keeps (document_extra) arrive as the launcher applies them from the file;
- Board -> argv -> Board means the same options again;
- Board -> .sandbox-ai.json -> the launcher's merge of that file: the same options as the board's own start.
"""
import argparse
import contextlib
import io
import json
import os
import random
import shlex

import pytest

from sandbox_ai_app import backend, board as B

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")

SEEDS = range(300)

# Values every option takes from the corpus, edge values among them: a leading "-" (a goal "- fix it", a folder
# "-old"), spaces, "=", commas (in options that are no list), non-ASCII text, padding the app strips.
TEXTS = ["plain", "with space", "-leading dash", "--double", "a=b", "x,y", "ünïcödé ✓", "  padded  ", "-1"]
PATHS = ["/tmp/a", "/tmp/with space", "/tmp/-dash", "-relative", "~/home", "/tmp/ä"]
LIST_POOLS = {
    "--allow": ["10.0.0.0/8", "192.0.2.1", "2001:db8::/32", "-1.2.3.4"],
    "--allow-host": ["example.com", "API.Example.org", "*.corp.example", "-bad.example"],
    "--allow-domain": ["example.net", "Sub.Example.io", "-x.example"],
    "--allow-tcp": ["192.0.2.5:5432", "198.51.100.1:22"],
    "--allow-host-port": ["3000", "8080", "-5"],
    "--nameserver": ["9.9.9.9", "1.1.1.1"],
    "--no-inspect": ["*.corp.example", "pinned.example"],
    "--vpn-bypass-tcp": ["192.0.2.9:443"],
    "--allow-http": ["api.github.com GET,HEAD /**", "x.example POST /a", "-y.example GET /"],
    "--env": ["FOO", "BAR_2", "-X"],
    "--secret-env": ["API=OPENROUTER", "TOKEN"],
    "--api-key": ["api.example.com=KEY", "-h=K"],
    "--kit": ["base", "-kit"],
    "--kit-dir": PATHS,
    "--copy": ["/tmp/x.json:.config/x.json", "-src:.y"],
    "--rules": PATHS,
    "--git-token-env": ["https://github.com=GH_TOKEN"],
    "--git-token-file": ["http://localhost:3080=/tmp/token"],
    "--git-token-secret": ["https://gitlab.com=GL"],
    "--device": ["/dev/video0", "usb:1234:abcd"],
    "--runtime": PATHS,
}
FEATURES = ["web", "browser", "-odd"]
# Board.extra: options no part owns, among them ones a project file may not hold (--mode, --session, --project-name)
EXTRAS = [["--mode", "high"], ["--mode=paranoid"], ["--project-name", "proj"], ["--session", "s1"], ["--probe"],
          ["--with", "codex"], ["--budget", "3"], ["--keep"], ["--mode", "low", "--session", "s2"]]


def parse(sai, agent, argv):
    """(the launcher's namespace of `sandbox-ai AGENT ARGV`, None) or (None, its error)."""
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            return _parse(sai, agent, argv), None
    except SystemExit:
        return None, (err.getvalue().strip().splitlines() or ["?"])[-1]


def _parse(sai, agent, argv):
    """argparse alone (parse_args would also read the network and the agents' modules): the options and the agent's
    arguments after -- as the launcher splits them."""
    tail = []
    if "--" in argv:
        position = argv.index("--")
        argv, tail = argv[:position], argv[position + 1:]
    namespace = sai.launcher.build_parser(agent).parse_args([agent, *argv])
    namespace.agent_args = tail
    return namespace


def actions(sai, agent):
    return {o: action for action in sai.launcher.build_parser(agent)._actions for o in action.option_strings}


def value_of(rng, option, sai):
    if option.long in LIST_POOLS:
        pool = LIST_POOLS[option.long]
        return rng.sample(pool, rng.randint(1, min(3, len(pool))))
    if option.long.endswith("-enable"):
        return rng.sample(FEATURES, rng.randint(1, 2))
    if option.kind == "flag":
        return True
    if option.kind == "choice":
        return rng.choice(option.choices)
    if option.kind == "multi":
        return rng.sample(option.choices, rng.randint(1, 2))
    if option.kind == "list":
        return [rng.choice(TEXTS)]
    if option.is_path or option.long in ("--state-dir", "--log-dir", "--privacy-rules", "--git-credentials",
                                         "--auth-file", "--import-codex-auth", "--local-config", "--gui-vnc",
                                         "--binary"):
        return rng.choice(PATHS)
    return rng.choice(TEXTS)


def generate(sai, seed):
    """A board of SEED: an agent with a model, effort and arguments; every other part kind with some of its options
    set (or none); workspaces, mounts, MCP servers, services, an LLM node's parameters; the project file's keys."""
    rng = random.Random(seed)
    options = B._options(sai)
    agent = rng.choice([a for a in B.AGENTS if a in sai.agents])
    board = B.from_argv(sai, agent, [])
    props = board.first("agent").props
    props["model"] = rng.choice(["", "", "sonnet", "local/qwen3", "-weird"])
    props["arguments"] = shlex.join(rng.sample(["--verbose", "fix it", "-p", "a b", "--", "ü"], rng.randint(0, 3)))
    for kind in B.KINDS.values():
        if kind.key in ("workspace", "mount", "mcp", "service", "bridge", "team"):
            continue
        if not kind.required and rng.random() < 0.4:
            continue
        node = board.first(kind.key) or board.add(kind.key)
        for name in kind.options:
            option = options.get(name)
            if option is None or rng.random() < 0.6:
                continue
            node.props[name] = value_of(rng, option, sai)
    for _ in range(rng.randint(0, 3)):
        board.add("workspace", path=rng.choice(PATHS), readonly=rng.random() < 0.2)
    for _ in range(rng.randint(0, 2)):
        board.add("mount", source=rng.choice(PATHS), dest=rng.choice(["/mnt/data", "/opt/x", "/work/y", "/mnt/a b"]),
                  mode=rng.choice(["ro", "rw"]))
    for index in range(rng.randint(0, 2)):
        node = board.add("mcp", name=f"srv{index}")
        if rng.random() < 0.5:
            node.props["limits"] = {"memory": "1g", "pids": "64"}
        if rng.random() < 0.5:
            node.props["variables"] = ["FOO=bar baz", "TOKEN@MY_SECRET"]
    for index in range(rng.randint(0, 2)):
        board.add("service", **B.default_service(f"db{index}"))
    if board.first("llm") is not None and rng.random() < 0.5:
        board.first("llm").props["--llm"] = "lab"
        board.first("llm").props["params"] = {"temperature": "0.2"}
    if rng.random() < 0.3:
        board.first("proxy").props["enabled"] = False
    for node in list(board.nodes):
        if node.kind in B.CONNECTED_ONLY and rng.random() < 0.15:
            agent_node = board.first("agent")
            board.disconnect(agent_node.id, node.id)
            board.disconnect(node.id, agent_node.id)
    extra = {}
    if rng.random() < 0.5:
        extra["mode"] = rng.choice(["lax", "low", "medium", "high", "paranoid"])
    if rng.random() < 0.3:
        extra["canaries"] = rng.choice([False, ["credentials"], ["home", "workspace"]])
    if rng.random() < 0.3:
        extra["keep"] = rng.choice([True, False])
    if rng.random() < 0.3:
        extra["log_debug"] = True
    if rng.random() < 0.3:
        extra["name"] = rng.choice(["proj", "my-proj.2"])
    if rng.random() < 0.3:
        extra["budget"] = rng.choice([5, 2.5, "10"])
    board.document_extra = extra
    if rng.random() < 0.4:  # options no part holds (a preset's, a command line's): kept in Board.extra
        board.extra += rng.choice(EXTRAS)
    return board


def expected(sai, board):
    """{dest: value} the launcher's namespace should hold for BOARD's start, by what the board means (values())."""
    agent, found, agent_args = B.values(board, sai)
    options, by_name = B._options(sai), actions(sai, agent)
    want = {}
    for name, value in found.items():
        option, action = options[name], by_name[name]
        if option.kind == "flag":
            if not value:
                continue
            if isinstance(action, argparse._AppendConstAction):
                want.setdefault(action.dest, set()).add(action.const)
            elif isinstance(action, argparse._StoreConstAction) and not isinstance(action, argparse._StoreTrueAction):
                want.setdefault(action.dest + "#const", []).append(action.const)
            else:
                want[action.dest] = True
        elif option.kind in ("list", "multi"):
            items = backend.option_items(name, value)
            if action.type is not None:
                items = [action.type(item) for item in items]
            want[action.dest] = (want.get(action.dest) or []) + items
        else:
            text = str(value).strip()
            if option.kind == "choice" and text == str(option.default):
                continue
            want[action.dest] = action.type(text) if action.type is not None else text
    return agent, want, agent_args


def compare(namespace, want):
    wrong = {}
    for dest, value in want.items():
        if dest.endswith("#const"):
            continue  # (two of one dest, --tmux and --no-tmux: the last wins; problems() says so)
        got = getattr(namespace, dest, None)
        if isinstance(value, set):
            ok = value <= set(got or [])
        elif isinstance(value, list):
            ok = [str(v) for v in got or []][:len(value)] == [str(v) for v in value] or \
                all(str(v) in [str(g) for g in got or []] for v in value)
        else:
            ok = str(got) == str(value)
        if not ok:
            wrong[dest] = (value, got)
    return wrong


@pytest.fixture(scope="module")
def sai():
    return backend.SandboxAI()


@pytest.mark.parametrize("seed", SEEDS)
def test_the_launcher_reads_every_board_as_the_board_means_it(sai, seed):
    board = generate(sai, seed)
    agent, want, agent_args = expected(sai, board)
    argv = B.launcher_argv(sai, board, local=False) + (["--", *agent_args] if agent_args else [])
    namespace, error = parse(sai, agent, argv)
    assert error is None, f"the launcher refuses the board's start {argv}: {error}"
    assert compare(namespace, want) == {}, argv
    assert namespace.agent_args == agent_args
    assert namespace.no_config


DOCUMENT_KEYS = ("mode", "canaries", "keep", "log_debug", "name", "budget")


@pytest.mark.parametrize("seed", SEEDS)
def test_the_project_file_keys_arrive_as_the_launcher_applies_them_from_the_file(sai, seed):
    """A board start passes --no-config: what the launcher would take from the project file's own keys (merge_config)
    the board gives as options (document_argv) - every key, none dropped (a budget dropped is a limit gone)."""
    board = generate(sai, seed)
    document = {"version": 1, "agent": "claude", **board.document_extra}
    merged = sai.launcher.merge_config(["claude", "--workspace", "/tmp/a"], argparse.Namespace(workspaces=["/tmp/a"]),
                                       document)
    from_file, error = parse(sai, "claude", merged[1:])
    assert error is None, error
    from_board, error = parse(sai, "claude", B.document_argv(board) + ["--workspace", "/tmp/a"])
    assert error is None, error
    for dest in ("canaries", "keep", "log_debug", "project_name", "budget"):
        assert getattr(from_board, dest) == getattr(from_file, dest), (dest, board.document_extra)
    # (the file's mode is no option of the merge: modes.resolve takes it, below a --mode of the command line)
    assert from_board.mode == document.get("mode")


def normal(sai, found):
    """values()' options in one spelling: list items one by one, values stripped, defaults of choices left out."""
    options, result = B._options(sai), {}
    for name, value in found.items():
        option = options.get(name)
        if option is None:
            result[name] = value
        elif option.kind in ("list", "multi"):
            result[name] = backend.option_items(name, value)
        elif option.kind == "flag":
            if value:
                result[name] = True
        else:
            text = str(value).strip()
            if text and not (option.kind == "choice" and text == str(option.default)):
                result[name] = text
    return result


@pytest.mark.parametrize("seed", SEEDS)
def test_board_to_command_line_and_back_means_the_same(sai, seed):
    board = generate(sai, seed)
    if B.workspace_overlaps(board):
        # (one folder twice, or one inside another: the board says it - problems() - as the launcher refuses it; read
        # back, the folder is there once)
        assert any(p.text.startswith("Workspaces must not overlap") for p in B.problems(sai, board))
        return
    paths = [n.props["path"] for n in board.included() if n.kind == "workspace"]
    if len(set(paths)) < len(paths):  # (a relative one twice: an error of its own, "a relative path")
        assert any("is a relative path" in p.text for p in B.problems(sai, board))
        return
    agent, found, agent_args = B.values(board, sai)
    again = B.from_argv(sai, agent, B.to_argv(sai, board), agent_args)
    agent2, found2, agent_args2 = B.values(again, sai)
    assert (agent2, agent_args2) == (agent, agent_args)
    assert normal(sai, found2) == normal(sai, found)


@pytest.mark.parametrize("seed", SEEDS)
def test_the_project_file_of_a_board_starts_what_the_board_starts(sai, seed, tmp_path):
    """Board -> .sandbox-ai.json (project_document) -> the launcher's merge of it (as a start from the trusted file):
    the options a project file may hold arrive as the board's own start gives them; the file is one the launcher
    takes (its keys, no forbidden option, no relative path - the board says those first); and the file read back
    into a board means the same."""
    board = generate(sai, seed)
    for node in board.of_kind("workspace") + board.of_kind("mount"):
        node.props.update(path=f"{tmp_path}/w{node.id}") if node.kind == "workspace" else None
    document, main = B.project_document(sai, board)
    agent = document["agent"]
    assert set(document) <= set(sai.launcher.CONFIG_KEYS)
    forbidden = set(sai.launcher.CONFIG_FORBIDDEN)
    assert not [o for o in sai.launcher.canonical_options(document["options"]) if o.split("=", 1)[0] in forbidden]
    if not main:
        return
    back = B.from_project(sai, json.loads(json.dumps(document)), main)
    _, found, args = B.values(board, sai)
    _, found2, args2 = B.values(back, sai)
    keep = {k: v for k, v in normal(sai, found).items() if k not in forbidden or k == "--workspace"}
    keep2 = {k: v for k, v in normal(sai, found2).items() if k not in forbidden or k == "--workspace"}
    assert keep2 == keep
    assert args2 == args
    # the file's own keys: the board's, and the --mode/--project-name of Board.extra as the keys they are (the last wins)
    moved = {}
    for group in B.extra_groups(sai, board.extra):
        name, equals, attached = group[0].partition("=")
        if name in B.EXTRA_KEYS:
            moved[B.EXTRA_KEYS[name]] = attached if equals else group[1]
    assert back.document_extra == {k: v for k, v in {**board.document_extra, **moved}.items()
                                   if k in sai.launcher.CONFIG_KEYS}
    assert not B.extra_groups(sai, back.extra) or all(g[0].split("=", 1)[0] not in forbidden
                                                       for g in B.extra_groups(sai, back.extra))
    merged = sai.launcher.merge_config([agent, "--workspace", main],
                                       argparse.Namespace(workspaces=[main]), document)
    from_file, error = parse(sai, agent, merged[1:])
    assert error is None, error
    assert from_file.agent_args == args
