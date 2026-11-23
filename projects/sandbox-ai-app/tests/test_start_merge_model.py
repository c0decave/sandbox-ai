"""One start, two views (the model, no Qt): a command line and the board are the same start.

Every option of the launcher's parser lands on a part or a property of the board - what no part represents on the part
"Other options" -, and comes back as the same command line (the order the parser's); a command line typed in is taken
whole or not at all; and a start put onto a board that has its parts already keeps their places."""
import os
import shlex

import pytest

from sandbox_ai_app import backend, board as B, presets as PR

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


@pytest.fixture(scope="module")
def sai():
    return backend.SandboxAI()


# A value each option's parser takes (what kind of text an option wants: the launcher's dry run decides whether it is
# right - here only that it survives the board).
SAMPLES = {"--workspace": "/tmp/proj", "--mount": "/tmp/data:/mnt/data:rw", "--mcp": "files",
           "--mcp-limits": "files:memory=1g", "--mcp-env": "files:DEBUG=1", "--mcp-mount": "files:/tmp/a:/mnt/a:ro",
           "--service": "db:image=localhost/sandbox-ai-service-db:latest,port=5432",
           "--llm": "local:qwen", "--llm-param": "temperature=0.2", "--allow-http": "api.example.org GET /**",
           "--memory": "4g", "--cpus": "2", "--pids": "512", "--tmp-size": "2g", "--env": "FOO",
           "--secret-env": "API=NAME", "--api-key": "api.example.org=NAME", "--publish": "8080:80",
           "--canaries": "aws,github", "--budget": "5", "--preset-value": "folder=/tmp/x", "--allow": "10.0.0.0/8",
           "--allow-tcp": "192.0.2.5:5432", "--allow-host-port": "3000", "--ip": "10.88.0.5", "--ip6": "fd00::5",
           "--subnet": "10.88.0.0/24", "--subnet6": "fd00::/64", "--checkpoint-every": "10m",
           "--ask-timeout": "30", "--llm-traffic-limit": "100m", "--gui-screen": "1280x720x24",
           "--copy": "/tmp/x.json:.config/x.json", "--device": "/dev/video0", "--vpn-bypass-tcp": "192.0.2.9:443",
           "--vpn-bypass-host-ports": "8080", "--git-token-env": "https://github.com=GH_TOKEN",
           "--git-token-file": "https://gitlab.com=/tmp/token", "--git-token-secret": "https://example.org=GL",
           "--with": "codex", "--cairn-workers": "2", "--model": "sonnet", "--effort": "high"}


def sample(option):
    """The tokens of OPTION with a value of its kind."""
    if option.kind == "flag":
        return [option.long]
    if option.long in SAMPLES:
        return [option.long, SAMPLES[option.long]]
    if option.choices:
        choice = next((c for c in option.choices if str(c) != str(option.default)), option.choices[0])
        return [option.long, str(choice)]
    return [option.long, "x-" + option.long.strip("-")]


def normal(sai, argv):
    """ARGV as the parser's order gives it (short forms spelt out, a list's items in the order given)."""
    options = sai.options()
    values, unknown = backend.values_from_argv(options, list(argv), backend.canonical_of(sai))
    return backend.argv_from_values(options, values) + unknown


def every_option(sai):
    options = sai.options()
    assert len(options) > 100
    return options


def test_every_option_of_the_parser_survives_the_board_alone(sai):
    """argv -> board -> argv for every option of `sandbox-ai AGENT --help`, each alone: the same command line."""
    lost = []
    for option in every_option(sai):
        argv = sample(option)
        board = B.from_argv(sai, "claude", argv)
        agent, back, agent_args = B.start_argv(sai, board)
        if agent != "claude" or agent_args or sorted(normal(sai, back)) != sorted(normal(sai, argv)):
            lost.append((argv, back, agent_args))
    assert not lost, lost


# Options that say something about another part: on that part when it is there, else kept on Other options.
ABOUT = {"--workspace-ro": ["--workspace", "/tmp/proj"], "--mcp-limits": ["--mcp", "files"],
         "--mcp-env": ["--mcp", "files"], "--llm-param": ["--llm", "local:qwen"]}


def test_every_option_lands_on_a_part_or_on_other_options(sai):
    """Nothing is kept anywhere but on a part: an option a part owns on that part, any other on "Other options"."""
    for option in every_option(sai):
        argv = sample(option)
        board = B.from_argv(sai, "claude", ABOUT.get(option.long, []) + argv)
        other = board.first("other")
        if option.long in B.OWNER or option.long in ("--model", "--effort", "--show-thinking"):
            assert other is None or not other.props.get("tokens"), (argv, other.props)
        else:
            assert other is not None and other.props["tokens"] == normal(sai, argv), argv
            assert board.extra == other.props["tokens"]
    for name in ABOUT:  # ... and without the part they are about: not dropped
        argv = sample(next(option for option in sai.options() if option.long == name))
        board = B.from_argv(sai, "claude", argv)
        assert board.first("other").props["tokens"] == argv and B.start_argv(sai, board)[1] == argv


def test_all_options_together_survive_and_settle(sai):
    """Every option at once: board -> argv -> board -> argv does not change any more (the order normalised)."""
    argv = [token for option in every_option(sai) for token in sample(option)
            if option.long not in ("--offline", "--no-proxy")]
    first = B.from_argv(sai, "claude", argv, ["--verbose", "fix it"])
    agent, once, args = B.start_argv(sai, first)
    second = B.from_argv(sai, agent, once, args)
    assert B.start_argv(sai, second) == (agent, once, args)
    assert sorted(normal(sai, once)) == sorted(normal(sai, argv))
    assert args == ["--verbose", "fix it"]


def builtin_presets(sai):
    return [preset for preset in PR.catalogue(sai, "local", mine=[]).presets]


def test_every_builtin_preset_survives_the_command_line(sai):
    """board -> argv -> board -> argv for every preset of the app and of sandbox-ai: the same start, and put back
    onto the preset's own board nothing moves and no part goes."""
    presets = builtin_presets(sai)
    assert len(presets) > 10
    for preset in presets:
        board = PR.build(sai, preset, "/tmp/proj", agent=(preset.agents or [preset.agent or "claude"])[0])
        agent, argv, args = B.start_argv(sai, board)
        again = B.from_argv(sai, agent, argv, args)
        assert B.start_argv(sai, again) == (agent, argv, args), preset.key
        merged = B.with_start(sai, board, agent, argv, args)
        assert B.start_argv(sai, merged) == (agent, argv, args), preset.key
        assert {n.id: (n.kind, n.x, n.y) for n in merged.nodes} == {n.id: (n.kind, n.x, n.y) for n in board.nodes}, \
            preset.key
        assert merged.edges == board.edges, preset.key
        assert merged.state() == board.state(), preset.key


# -- the part Other options --

def test_other_options_is_a_part_that_holds_board_extra(sai):
    board = B.from_argv(sai, "claude", ["--mode", "high", "--offline", "--name", "night"])
    other = board.first("other")
    assert other is not None and other.props["tokens"] == ["--mode", "high", "--name", "night"]
    assert board.extra == ["--mode", "high", "--name", "night"]
    assert (other.id, board.first("agent").id) in board.edges and board.connected(other)
    assert B.to_argv(sai, board)[-4:] == ["--mode", "high", "--name", "night"]
    # set: the part follows; emptied: the part stays (it is on the board), removed: the options go
    board.extra = ["--probe"]
    assert board.first("other").props["tokens"] == ["--probe"]
    board.extra += ["--no-config"]
    assert board.extra == ["--probe", "--no-config"]
    board.remove(other.id)
    assert board.extra == [] and "--probe" not in B.to_argv(sai, board)
    assert B.from_argv(sai, "claude", ["--offline"]).first("other") is None


def test_an_older_state_without_the_part_gets_it(sai):
    """A draft kept by a version before the part (its options in "extra" only): the part is there, beside the others."""
    board = B.from_argv(sai, "claude", ["--offline"])
    state = board.state()
    state["extra"] = ["--mode", "high"]
    back = B.Board.from_state(state)
    other = back.first("other")
    assert other is not None and back.extra == ["--mode", "high"]
    assert all((other.x, other.y) != (n.x, n.y) for n in back.nodes if n is not other)
    assert B.Board.checked(back.state(), sai).extra == ["--mode", "high"]
    assert B.Board.from_state(back.state()).state() == back.state()


# -- a command line typed in --

def test_a_command_line_is_parsed_whole(sai):
    host, agent, argv, args = B.parse_command(sai, "sandbox-ai codex -o --memory 4g -w '/tmp/my proj' -- exec 'fix it'")
    assert (host, agent, args) == (None, "codex", ["exec", "fix it"])
    assert argv == ["-o", "--memory", "4g", "-w", "/tmp/my proj"]
    assert B.parse_command(sai, "claude")[1:] == ("claude", [], [])
    assert B.parse_command(sai, "./sandbox-ai claude --offline \\\n  --cpus 2")[2] == ["--offline", "--cpus", "2"]
    assert B.parse_command(sai, "sandbox-ai hosts run build1 -- claude --offline") == ("build1", "claude",
                                                                                         ["--offline"], [])


@pytest.mark.parametrize("text, said", [
    ("sandbox-ai claude --memory '4g", "quot"),
    ("", "agent"),
    ("sandbox-ai", "agent"),
    ("sandbox-ai nobody --offline", "nobody"),
    ("sandbox-ai claude --no-such-option", "--no-such-option"),
    ("sandbox-ai claude --memory", "--memory"),
    ("sandbox-ai claude stray --offline", "stray"),
    ("sandbox-ai team start x", "team"),
    ("sandbox-ai hosts run", "host"),
])
def test_a_command_line_that_does_not_parse_says_why(sai, text, said):
    with pytest.raises(ValueError) as error:
        B.parse_command(sai, text)
    assert said in str(error.value)


def test_tokens_the_board_holds_already_may_stay_in_a_typed_line(sai):
    """What a project file of another version brought (a token the parser does not know, kept in Other options) does
    not make every later edit of the line an error."""
    with pytest.raises(ValueError):
        B.parse_command(sai, "sandbox-ai claude --from-the-future 1 --offline")
    assert B.parse_command(sai, "sandbox-ai claude --from-the-future 1 --offline",
                           known=["--from-the-future", "1"])[2] == ["--from-the-future", "1", "--offline"]


# -- a start put onto the board that is there --

def placed(board):
    return {node.key(): (node.id, node.x, node.y) for node in board.nodes}


def test_with_start_keeps_ids_places_and_what_only_the_board_holds(sai):
    board = B.from_argv(sai, "claude", ["--offline", "--memory", "4g", "--mcp", "files", "-w", "/tmp/a", "-m",
                                        "/data:/mnt/data:ro"])
    for index, node in enumerate(board.nodes):
        node.x, node.y = 1000.0 + index * 10, 2000.0 + index * 7
    files = next(n for n in board.of_kind("mcp"))
    files.props.update(definition={"command": ["x"]}, project=True)
    board.add("bridge", peer="other", direction="out")
    board.labels = {"<vpn:work>": ["a", "b"]}
    before = placed(board)
    agent, argv, args = B.start_argv(sai, board)
    argv = [token for token in argv if token not in ("--memory", "4g")] + ["--cpus", "2", "--mode", "high", "--gui"]
    merged = B.with_start(sai, board, "codex", argv, ["exec"])
    assert merged is not board and placed(board) == before  # (a new board: the one given is not changed)
    after = placed(merged)
    for key, where in before.items():
        assert after[key] == where, key
    assert merged.first("agent").props["agent"] == "codex" and merged.first("agent").props["arguments"] == "exec"
    limits = merged.first("limits")
    assert limits.props.get("--cpus") == "2" and "--memory" not in limits.props
    assert merged.first("display").props.get("--gui") is True
    assert merged.extra == ["--mode", "high"] and merged.first("other") is not None
    mcp = merged.of_kind("mcp")[0]
    assert mcp.props["definition"] == {"command": ["x"]} and mcp.props["project"] is True
    assert [n.props["peer"] for n in merged.of_kind("bridge")] == ["other"] and merged.labels == board.labels
    new = [node for key, node in ((n.key(), n) for n in merged.nodes) if key not in before]
    assert {n.kind for n in new} == {"display", "other"}
    spots = [(n.x, n.y) for n in merged.nodes]
    assert len(set(spots)) == len(spots)  # the new parts lie on no other
    assert B.start_argv(sai, merged) == ("codex", B.start_argv(sai, B.from_argv(sai, "codex", argv, ["exec"]))[1],
                                         ["exec"])


def test_with_start_removes_what_the_line_no_longer_names(sai):
    board = B.from_argv(sai, "claude", ["-w", "/tmp/a", "-w", "/tmp/b", "--mcp", "files", "--mcp", "web", "-m",
                                        "/data:/mnt/data:ro"])
    own = next(n for n in board.of_kind("mcp") if n.props["name"] == "web")
    own.props.update(definition={"command": ["x"]}, project=True)
    spare = board.add("workspace", connect=False, path="/tmp/spare")  # on the board, not connected: no part of a start
    merged = B.with_start(sai, board, "claude", ["-w", "/tmp/b", "-w", "/tmp/c"], [])
    assert [n.props["path"] for n in merged.of_kind("workspace") if merged.connected(n)] == ["/tmp/b", "/tmp/c"]
    assert merged.node(spare.id) is not None and not merged.connected(merged.node(spare.id))
    assert not merged.of_kind("mount")
    # a server the project itself defines stays on the board, not connected (as a project file without --mcp has it)
    assert [(n.props["name"], merged.connected(n)) for n in merged.of_kind("mcp")] == [("web", False)]
    again = B.with_start(sai, merged, "claude", ["-w", "/tmp/b", "--mcp", "web", "-w", "/tmp/spare"], [])
    assert [(n.props["name"], again.connected(n)) for n in again.of_kind("mcp")] == [("web", True)]
    assert again.of_kind("mcp")[0].props["project"] is True
    assert again.node(spare.id) is not None and again.connected(again.node(spare.id))


def test_with_start_follows_the_order_of_the_line(sai):
    """The first workspace is /work: turned round in the line, it is turned round on the board (the parts stay)."""
    board = B.from_argv(sai, "claude", ["-w", "/tmp/a", "-w", "/tmp/b"])
    ids = {n.props["path"]: n.id for n in board.of_kind("workspace")}
    merged = B.with_start(sai, board, "claude", ["-w", "/tmp/b", "-w", "/tmp/a"], [])
    assert B.start_argv(sai, merged)[1] == ["--workspace", "/tmp/b", "--workspace", "/tmp/a"]
    assert {n.props["path"]: n.id for n in merged.of_kind("workspace")} == ids


def test_with_start_keeps_agent_arguments_as_typed(sai):
    """--model among the agent's own arguments stays there (typed letter by letter, it is no model of the board
    that empties the field it is typed into)."""
    board = B.from_argv(sai, "claude", ["--offline"])
    merged = B.with_start(sai, board, "claude", ["--offline"], ["--model", "son", "fix it"])
    assert B.start_argv(sai, merged) == ("claude", ["--offline"], ["--model", "son", "fix it"])
    assert not merged.first("agent").props.get("model")


def test_the_project_files_own_keys_are_part_of_the_line_and_come_back(sai, tmp_path):
    """mode, canaries, keep, name and budget of .sandbox-ai.json are no part of the board, but of its start: the line
    shows them, and changed in the line they change where they are kept (never twice in the line)."""
    document = {"version": 1, "agent": "claude", "options": ["--offline", "--log-debug"], "mode": "paranoid",
                "keep": True, "name": "night", "log_debug": True, "canaries": ["aws"]}
    board = B.from_project(sai, document, tmp_path)
    agent, argv, args = B.start_argv(sai, board)
    assert argv[:argv.index("--offline")].count("--mode") == 1 and argv.count("--log-debug") == 1
    assert ["--mode", "paranoid"] == argv[:2] and "--keep" in argv and ["--project-name", "night"] == \
        argv[argv.index("--project-name"):][:2]
    assert B.with_start(sai, board, agent, argv, args).state() == board.state()  # the same line: nothing changes
    changed = [("high" if token == "paranoid" else token) for token in argv if token not in ("--keep",)]
    merged = B.with_start(sai, board, agent, changed, args)
    assert merged.document_extra["mode"] == "high" and "keep" not in merged.document_extra
    assert merged.document_extra["name"] == "night" and merged.document_extra["canaries"] == ["aws"]
    assert not merged.extra
    assert B.start_argv(sai, merged) == (agent, changed, args)
    without = B.with_start(sai, board, agent, [t for t in argv if t != "--log-debug"], args)
    assert "log_debug" not in without.document_extra and "--log-debug" not in B.start_argv(sai, without)[1]


def test_the_command_line_is_start_argv_and_parses_back(sai):
    board = B.from_argv(sai, "codex", ["--offline", "--mode", "high", "-w", "/tmp/my proj"], ["exec", "fix it"])
    line = B.command_line(sai, board)
    assert line == shlex.join(["sandbox-ai", "codex", "--workspace", "/tmp/my proj", "--offline", "--mode", "high",
                               "--", "exec", "fix it"])
    host, agent, argv, args = B.parse_command(sai, line)
    assert B.with_start(sai, board, agent, argv, args).state() == board.state()


def test_start_values_are_the_forms(sai):
    """What the Expert view's fields show: the agent, {option: value} of every option of the line, the arguments."""
    board = B.from_argv(sai, "codex", ["--offline", "--mode", "high", "--memory", "4g", "-w", "/tmp/a"], ["exec"])
    agent, values, args = B.start_values(sai, board)
    assert (agent, args) == ("codex", ["exec"])
    assert values["--offline"] is True and values["--mode"] == "high" and values["--memory"] == "4g"
    assert values["--workspace"] == ["/tmp/a"]
