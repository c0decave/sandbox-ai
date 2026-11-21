"""Board review 1 (interplay and correctness, docs/reviews/board-review-1.md): each finding's regression test.

The generated corpus (every part kind, random combinations, edge values, read by the launcher's own parser) is
test_board_review1_corpus.py; these are the findings it and the review turned up, one by one, through the real entry
points (the board model with the real launcher, and the Board page offscreen where it is user-visible)."""
import argparse
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path

import pytest

from sandbox_ai_app import backend, board as B, project_trust as PT

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


@pytest.fixture(scope="module")
def sai():
    return backend.SandboxAI()


def parsed(sai, agent, argv):
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        try:
            return sai.launcher.build_parser(agent).parse_args([agent, *argv]), None
        except SystemExit:
            return None, err.getvalue().strip().splitlines()[-1]


# -- R1-1: the project file's budget was dropped by a board start (--no-config) and named nowhere --

def test_a_board_start_keeps_the_project_files_budget(sai, tmp_path):
    board = B.from_project(sai, {"version": 1, "agent": "claude", "budget": 5, "mode": "high"}, str(tmp_path))
    argv = B.launcher_argv(sai, board)
    assert "--no-config" in argv
    namespace, error = parsed(sai, "claude", argv)
    assert error is None
    assert namespace.budget == 5.0, "a board start passes --no-config: the file's budget must come as --budget"
    assert "--budget 5" in B.command_line(sai, board)
    # named above the board and in Apply's question, as the file's other keys no part shows
    assert PT.unshown(board.document_extra) == {"mode": "high", "budget": 5}
    assert any(line.strip().startswith("budget:") for line in PT.summary(sai, {"version": 1, "agent": "claude",
                                                                               "budget": 5}))
    assert PT.ORDER.index("budget") < PT.ORDER.index("description")


def test_the_budget_goes_back_into_the_project_file_not_into_its_options(sai, tmp_path):
    board = B.from_project(sai, {"version": 1, "agent": "claude", "budget": "2.5"}, str(tmp_path))
    document, _ = B.project_document(sai, board)
    assert document["budget"] == "2.5" and "--budget" not in document["options"]


# -- R1-2: a value that starts with "-" was given apart from its option: the launcher refused the start --

@pytest.mark.parametrize("kind,props,dest,want", [
    ("cairn", {"--cairn": True, "--cairn-goal": "- fix the failing tests"}, "cairn_goal", "- fix the failing tests"),
    ("home", {"--state-dir": "/tmp/x", "--copy": ["-notes.txt:.notes"]}, "copy", ["-notes.txt:.notes"]),
    ("agent", {"--env": ["-X"]}, "env", ["-X"]),
    ("network", {"--allow-host": ["-bad.example"]}, "allow_host", ["-bad.example"]),
])
def test_a_value_with_a_leading_dash_reaches_its_option(sai, kind, props, dest, want):
    board = B.from_argv(sai, "claude", ["--workspace", "/tmp/w"])
    node = board.first(kind) or board.add(kind)
    node.props.update(props)
    argv = B.launcher_argv(sai, board)
    namespace, error = parsed(sai, "claude", argv)
    assert error is None, f"{argv}: {error}"
    assert getattr(namespace, dest) == want
    # and the board reads it back as it was
    again = B.from_argv(sai, "claude", B.to_argv(sai, board))
    assert (again.first(kind) or again.first("agent")).props.get(next(iter(props.keys() - {"--cairn", "--state-dir"}))) \
        == next(v for k, v in props.items() if k not in ("--cairn", "--state-dir"))


def test_the_start_page_form_gives_a_dashed_value_attached_too(sai):
    option = B._options(sai)["--cairn-goal"]
    assert option.tokens("-x") == ["--cairn-goal=-x"] and option.tokens("x") == ["--cairn-goal", "x"]
    values, unknown = backend.values_from_argv(sai.options(), ["--cairn-goal=-x"], backend.canonical_of(sai))
    assert values == {"--cairn-goal": "-x"} and unknown == []


# -- R1-3: two workspaces of one folder (or one inside the other): only the launcher's dry run refused them --

@pytest.mark.parametrize("paths,overlap", [
    (["/tmp/p", "/tmp/p"], True),
    (["/tmp/p", "/tmp/p/sub"], True),
    (["/tmp/p/sub", "/tmp/p/"], True),
    (["~/p", "~/p/./"], True),
    (["/tmp/p", "/tmp/p2"], False),
    (["/tmp/p", "/tmp/q"], False),
    (["~/p", os.path.expanduser("~/p")], False),  # (~ is the board's host's HOME: not this machine's, not compared)
])
def test_overlapping_workspaces_are_said_as_the_launcher_says_them(sai, paths, overlap):
    board = B.from_argv(sai, "claude", [])
    for path in paths:
        board.add("workspace", path=path)
    texts = [p.text for p in B.problems(sai, board) if p.level == "error"]
    assert any(text.startswith("Workspaces must not overlap:") for text in texts) == overlap, texts


def test_a_workspace_not_connected_overlaps_nothing(sai):
    board = B.from_argv(sai, "claude", [], workspaces=["/tmp/p"])
    other = board.add("workspace", path="/tmp/p")
    board.disconnect(other.id, board.first("agent").id)
    assert not B.workspace_overlaps(board)


# -- R1-4: the board's mount checks were looser than the launcher's (/mntx, /opt/../etc, /mnt/x/ passed the board) and a
# mount without a source was left out of the start without a word --

@pytest.mark.parametrize("dest", ["/mnt", "/mnt/data", "/opt/x/y", "/work/sub", "/mntx", "/optical", "/workshop",
                                  "/opt/../etc", "/mnt/x/", "//mnt/x", "mnt/x", "/mnt/./x", "", "/etc", "/work"])
def test_the_mount_destination_is_judged_as_the_launcher_judges_it(sai, dest):
    try:
        sai.launcher.safe_dest(dest)
        refused = False
    except ValueError:
        refused = True
    board = B.from_argv(sai, "claude", [], workspaces=["/tmp/w"])
    board.add("mount", source="/tmp", dest=dest, mode="ro")
    said = [p.text for p in B.problems(sai, board) if p.level == "error" and "mount" in p.text.lower()]
    assert bool(said) == refused, (dest, said)


def test_a_mount_without_a_source_is_said_not_dropped(sai):
    board = B.from_argv(sai, "claude", [], workspaces=["/tmp/w"])
    board.add("mount", source="", dest="/mnt/x", mode="ro")
    assert "--mount" not in B.to_argv(sai, board)
    assert "A mount needs a folder or file." in [p.text for p in B.problems(sai, board) if p.level == "error"]


def test_a_mount_path_with_a_comma_is_said(sai):
    board = B.from_argv(sai, "claude", [], workspaces=["/tmp/w"])
    board.add("mount", source="/tmp/a,b", dest="/mnt/x", mode="ro")
    assert "Mount paths cannot contain commas or control characters." in [p.text for p in B.problems(sai, board)]


# -- R1-5: an option of Board.extra a project file may not hold (--mode high: a preset saved from a project with a mode)
# was cut from its value: Apply wrote a bare "high" into the file's options, and a start from the file passed --mode
# without its value; the board's mode was lost from the file --

def preset_board(sai, tmp_path):
    from sandbox_ai_app import presets as P
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    source = B.from_project(sai, {"version": 1, "agent": "claude", "mode": "high", "name": "proj",
                                  "options": ["--allow-host", "x.example"]}, str(tmp_path / "a"))
    preset = P.from_board(sai, source, "Mine")
    assert "--mode" in preset.options
    return P.build(sai, preset, str(tmp_path / "b"))


def trusted_read(sai, main, document):
    """(the namespace of a start in MAIN, the document) as the launcher reads DOCUMENT from MAIN/.sandbox-ai.json
    once trusted."""
    path = Path(main) / ".sandbox-ai.json"
    path.write_text(json.dumps(document))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    original = sai.launcher.trusted_configs
    sai.launcher.trusted_configs = lambda: {str(path.resolve()): digest}
    try:
        a = sai.launcher.parse_args(["claude", "--workspace", main, "--workspaces-as-given"])
        return a, sai.launcher.load_project_config(a)[1]
    finally:
        sai.launcher.trusted_configs = original


def test_apply_moves_a_mode_of_the_options_into_the_project_files_mode(sai, tmp_path):
    board = preset_board(sai, tmp_path)
    assert "--mode" in board.extra
    document, main = B.project_document(sai, board)
    assert document["options"] == ["--allow-host", "x.example"], "no option cut from its value, nothing forbidden"
    assert document["mode"] == "high" and document["name"] == "proj"
    a, read = trusted_read(sai, main, document)
    merged = sai.launcher.merge_config(["claude", "--workspace", main], a, read)
    namespace, error = parsed(sai, "claude", merged[1:merged.index("--")])
    assert error is None, (merged, error)
    assert namespace.project_name == "proj"
    # the file read back is the board again: the mode a key of the file, as Apply wrote it
    again = B.from_project(sai, read, main)
    assert again.document_extra["mode"] == "high" and "--mode" not in again.extra


def test_a_start_from_the_project_file_passes_a_forbidden_option_with_its_value(sai, tmp_path):
    board = preset_board(sai, tmp_path)
    board.extra += ["--session", "s1"]
    board.add("mcp", name="own", definition={"command": ["/bin/true"]}, project=True)
    argv = B.file_start_argv(sai, board)
    namespace, error = parsed(sai, "claude", argv)
    assert error is None, (argv, error)
    assert namespace.mode == "high" and namespace.session == "s1"


# -- R1-6: a team board passes its Agent part's --effort and --show-thinking to every member (team start has no
# per-member effort): a member whose agent takes no such level, or has no switch for thinking, refused its start, and
# the board said nothing before team start's own check --

def team_board(sai, tmp_path, members, **agent):
    board = B.from_argv(sai, "claude", [], workspaces=[str(tmp_path)])
    board.first("agent").props.update(agent)
    board.add("team", name="rev", lead=members[0], members=list(members))
    return board


@pytest.mark.parametrize("members,agent,refused", [
    (["claude", "opencode"], {"effort": "high"}, ["opencode"]),
    (["claude", "codex"], {"effort": "max"}, []),
    (["claude", "goose"], {"effort": "xhigh"}, ["goose"]),
    (["claude", "codex"], {}, []),
])
def test_a_team_says_which_members_refuse_the_agents_effort(sai, tmp_path, members, agent, refused):
    board = team_board(sai, tmp_path, members, **agent)
    assert ("--effort" in B.team_values(sai, board)["options"]) == bool(agent)
    said = [p for p in B.problems(sai, board) if p.level == "error" and "--effort" in p.text]
    assert bool(said) == bool(refused), said
    for name in refused:
        assert name in said[0].text
        # as the launcher refuses that member's start
        a = argparse.Namespace(model=None, effort=agent["effort"], llm=None, agent=name, agent_args=[])
        with pytest.raises(ValueError):
            sai.launcher.check_model_effort(a)


def test_a_team_says_which_members_have_no_switch_for_thinking(sai, tmp_path):
    from sandbox_ai_app import thinking
    without = next(a for a in backend.TEAM_AGENTS if not thinking.available(a))
    board = team_board(sai, tmp_path, ["claude", without], thinking="on")
    said = [p.text for p in B.problems(sai, board) if p.level == "error" and "--show-thinking" in p.text]
    assert said and without in said[0]
    assert not [p for p in B.problems(sai, team_board(sai, tmp_path, ["claude", "codex"], thinking="on"))
                if "--show-thinking" in p.text and p.level == "error"]
