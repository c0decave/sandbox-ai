"""The fix plan (sandbox/fixes.py): what a start lacks and what makes each - its text, its question on a terminal
(one for everything, or one per fix), the security modes (an offer never widens one), --fix and --fix-only, the
refusal with the commands to copy, a fix that fails, the host's package command."""
import importlib.util
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sandbox_ai_fixes_tests", ROOT / "sandbox/fixes.py")
f = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(f)


def image(agent="claude"):
    return f.fix("image", f"localhost/{agent}-sandbox:latest", f"the image of {agent} is not built", "build it",
                 [["sandbox-ai", "build", agent]], duration="~5 min")


def unsigned_server(name="smali"):
    return f.fix("mcp", name, f"the MCP server {name} is not added", "build it here and add it",
                 [["sandbox-ai", "mcp", "add", name, "--build", "--yes"]], duration="minutes", unsigned=True,
                 security="nobody signed the image that is built here")


def toolchain(name="go"):
    return f.fix("toolchain", name, f"the toolchain {name} is not in the store", "add it from the signed catalog",
                 [["sandbox-ai", "toolchain", "add", name]], duration="a download")


def login(agent="claude"):
    return f.fix("login", agent, f"the host has no {agent} login", "log in now", [["sandbox-ai", "login", agent]],
                 terminal=True, needed=False)


class Runner:
    """run_one stood in for: every command recorded; FAIL: the commands (their text) that end with status 3."""

    def __init__(self, fail=()):
        self.ran, self.fail = [], set(fail)

    def __call__(self, command, terminal=False):
        self.ran.append(f.command_text(command))
        return (3, ["STEP 2/3: RUN make", "error: it broke"]) if f.command_text(command) in self.fail else (0, [])


def reader(*answers):
    """input() stood in for: the ANSWERS in turn (None: the end of input), every question recorded in .asked."""
    left, asked = list(answers), []

    def read(question):
        asked.append(question)
        reply = left.pop(0)
        if reply is None:
            raise EOFError
        return reply
    read.asked = asked
    return read


def resolve(fixes, **more):
    said = []
    more.setdefault("run_command", Runner())
    more.setdefault("terminal", True)
    done = f.resolve("Refused: something is missing", fixes, out=said.append, **more)
    return done, said, more["run_command"]


# --- a fix, and the plan's words --------------------------------------------------------------------------------

def test_a_fix_says_what_is_missing_what_is_done_and_whether_it_is_offered():
    item = image()
    assert item["id"] == "image:localhost/claude-sandbox:latest" and item["verdict"] == "offered" and item["needed"]
    assert f.what(item) == "build it: sandbox-ai build claude (~5 min)"
    tool = f.tool_fix(["podman", "pasta"], manager=("apt", ["sudo", "apt", "install", "-y"]))
    assert tool["verdict"] == "manual" and tool["commands"] == [["sudo", "apt", "install", "-y", "podman", "passt"]]
    assert "never runs sudo" in tool["why"]
    nothing = f.fix("mcp", "x", "the MCP server x is not added", "define it yourself", manual="no catalog lists it")
    assert nothing["verdict"] == "manual" and nothing["commands"] == []


def test_unsigned_and_security_relevant_fixes_are_always_named_as_such():
    text = f.what(unsigned_server())
    assert ", UNSIGNED" in text and "SECURITY: nobody signed the image that is built here" in text
    lines = "\n".join(f.listing([image(), unsigned_server()]))
    assert "1. the image of claude is not built" in lines and "UNSIGNED" in lines
    assert "UNSIGNED" not in f.what(image())


def test_the_refusal_ends_with_the_complete_block_of_commands_to_copy():
    text = f.refusal("Refused: two things", [image(), toolchain(), login(),
                                              f.tool_fix(["tmux"], manager=("dnf", ["sudo", "dnf", "install", "-y"]))])
    lines = text.splitlines()
    assert lines[0] == "Refused: two things"
    at = lines.index("What fixes it (copy these lines), then start again:")
    # every command on a line of its own, as it is typed; the login the start goes on without is no part of it
    assert lines[at + 1:at + 4] == ["  sandbox-ai build claude", "  sandbox-ai toolchain add go",
                                    "  sudo dnf install -y tmux"]
    assert "Or start again with --fix: sandbox-ai does it first, without asking." in lines
    assert "sandbox-ai login" not in text
    assert any("not done for you - it needs root" in line for line in lines)
    assert "--fix" not in f.refusal("Refused", [f.tool_fix(["tmux"], manager=("apt", ["sudo", "apt", "install"]))])


# --- the security modes: an offer never widens one --------------------------------------------------------------

@pytest.mark.parametrize("mode", ["high", "paranoid"])
def test_a_strict_mode_is_offered_nothing_unsigned_and_names_the_laxer_mode(mode):
    judged = {item["kind"]: item for item in f.judge([image(), toolchain(), unsigned_server()], mode)}
    assert judged["image"]["verdict"] == "offered" and judged["toolchain"]["verdict"] == "offered"
    assert judged["mcp"]["verdict"] == "refused"
    assert f"not offered in the security mode {mode}" in judged["mcp"]["why"]
    assert "the mode medium takes it (--mode medium)" in judged["mcp"]["why"]
    opens = f.fix("mcp", "web", "the MCP server web is not added", "add it", [["sandbox-ai", "mcp", "add", "web"]],
                  network=True)
    assert f.judge([opens], mode)[0]["verdict"] == "refused" and "open network" in f.judge([opens], mode)[0]["why"]
    # ... and it is never run, asked or not, with --fix or not
    with pytest.raises(ValueError) as refused:
        resolve([unsigned_server()], mode=mode, fix_all=True)
    assert "the mode medium takes it" in str(refused.value) and "sandbox-ai mcp add smali" not in str(refused.value)
    runner = Runner()
    with pytest.raises(ValueError):
        resolve([image(), unsigned_server()], mode=mode, fix_all=True, run_command=runner)
    assert runner.ran == ["sandbox-ai build claude"]  # (what the mode takes is done; the start still lacks the rest)


@pytest.mark.parametrize("mode", ["lax", "low", "medium"])
def test_the_other_modes_offer_it_named_unsigned(mode):
    assert [item["verdict"] for item in f.judge([unsigned_server()], mode)] == ["offered"]


# --- the question: one for everything, or one per fix -----------------------------------------------------------

def test_one_question_for_everything_and_enter_does_it_all():
    read = reader("")
    done, said, runner = resolve([image(), toolchain(), unsigned_server()], read=read)
    assert len(read.asked) == 1 and "Do these 3 now and go on? [Y]es, all / [c]hoose / [n]o:" in read.asked[0]
    assert runner.ran == ["sandbox-ai build claude", "sandbox-ai toolchain add go",
                          "sandbox-ai mcp add smali --build --yes"]
    assert done == ["image:localhost/claude-sandbox:latest", "toolchain:go", "mcp:smali"]
    assert said[0] == "this start needs what is not there yet:"
    assert any(line.startswith("fix 1/3: the image of claude is not built - build it") for line in said)
    assert any(line.startswith("fix 3/3 done in ") for line in said)


def test_choose_asks_each_and_a_declined_needed_one_stops_the_start_with_its_command():
    read, runner = reader("c", "y", "n"), Runner()
    with pytest.raises(ValueError) as refused:
        resolve([image(), toolchain()], read=read, run_command=runner)
    assert len(read.asked) == 3 and read.asked[1].endswith("build it: sandbox-ai build claude (~5 min)? [Y/n] ")
    assert runner.ran == ["sandbox-ai build claude"]  # (the one chosen is done)
    text = str(refused.value)
    assert text.startswith("Not started: the toolchain go is not in the store")
    assert "  sandbox-ai toolchain add go" in text and "sandbox-ai build claude" not in text


def test_no_and_the_end_of_input_do_nothing_and_refuse_with_the_commands():
    for answers in (("n",), (None,), ("what?",)):
        runner = Runner()
        with pytest.raises(ValueError) as refused:
            resolve([image(), toolchain()], read=reader(*answers), run_command=runner)
        assert runner.ran == []
        assert str(refused.value).startswith("Refused: something is missing")
        assert "  sandbox-ai build claude\n  sandbox-ai toolchain add go" in str(refused.value)


def test_the_setting_each_asks_one_question_per_fix():
    read, runner = reader("y", "y"), Runner()
    done, _said, _ = resolve([image(), toolchain()], questions="each", read=read, run_command=runner)
    assert len(read.asked) == 2 and all(question.endswith("? [Y/n] ") for question in read.asked)
    assert done == ["image:localhost/claude-sandbox:latest", "toolchain:go"]
    read, runner = reader("y", "n"), Runner()
    with pytest.raises(ValueError, match="Not started: the toolchain go is not in the store"):
        resolve([image(), toolchain()], questions="each", read=read, run_command=runner)
    assert runner.ran == ["sandbox-ai build claude"]


def test_a_login_the_start_goes_on_without_is_offered_but_never_a_reason_to_stop():
    # "all" is what the start needs: the login is done only when it is chosen
    read = reader("")
    done, _said, runner = resolve([image(), login()], read=read)
    assert done == ["image:localhost/claude-sandbox:latest"] and runner.ran == ["sandbox-ai build claude"]
    assert "Do it now and go on?" in read.asked[0]
    # chosen (No is its default: Enter leaves it) ...
    read = reader("c", "", "")
    done, _said, runner = resolve([image(), login()], read=read)
    assert runner.ran == ["sandbox-ai build claude"] and read.asked[2].endswith("? [y/N] ")
    done, _said, runner = resolve([image(), login()], read=reader("c", "y", "y"))
    assert runner.ran == ["sandbox-ai build claude", "sandbox-ai login claude"]
    # ... and when it failed (the human closed the sign-in): said, and the start goes on
    runner = Runner(fail=["sandbox-ai login claude"])
    done, said, _ = resolve([image(), login()], read=reader("c", "y", "y"), run_command=runner)
    assert done == ["image:localhost/claude-sandbox:latest"]
    assert any("not needed: going on without it" in line for line in said)
    # alone it is no question at all: nothing the start needs is missing that sandbox-ai could do
    with pytest.raises(ValueError):
        resolve([login(), f.tool_fix(["tmux"], manager=("apt", ["sudo", "apt", "install"]))],
                read=lambda question: pytest.fail("asked"))


# --- no terminal, --fix, --fix-only -----------------------------------------------------------------------------

def test_without_a_terminal_it_is_refused_with_the_commands_and_nothing_runs():
    runner = Runner()
    with pytest.raises(ValueError) as refused:
        resolve([image(), toolchain()], terminal=False, run_command=runner,
                read=lambda question: pytest.fail("asked without a terminal"))
    assert runner.ran == [] and "  sandbox-ai build claude\n  sandbox-ai toolchain add go" in str(refused.value)
    assert "Or start again with --fix" in str(refused.value)


def test_fix_does_everything_offered_without_asking_also_without_a_terminal():
    done, said, runner = resolve([image(), toolchain(), login()], terminal=False, fix_all=True,
                                 read=lambda question: pytest.fail("asked"))
    # (what the start needs; the login it goes on without only when named: --fix-only, and then on a terminal)
    assert runner.ran == ["sandbox-ai build claude", "sandbox-ai toolchain add go"]
    assert done == ["image:localhost/claude-sandbox:latest", "toolchain:go"]
    assert said[0] == "this start needs what is not there yet (--fix: done now, without asking):"
    _done, _said, runner = resolve([image(), login()], terminal=True, fix_all=True)
    assert runner.ran == ["sandbox-ai build claude"]
    _done, _said, runner = resolve([image(), login()], terminal=True,
                                   only=["image:localhost/claude-sandbox:latest", "login:claude"])
    assert runner.ran == ["sandbox-ai build claude", "sandbox-ai login claude"]


def test_fix_never_runs_what_needs_root():
    runner = Runner()
    with pytest.raises(ValueError) as refused:
        resolve([image(), f.tool_fix(["tmux"], manager=("apt", ["sudo", "apt", "install", "-y"]))], fix_all=True,
                terminal=False, run_command=runner)
    assert runner.ran == ["sandbox-ai build claude"]
    assert "sudo apt install -y tmux" in str(refused.value) and "tmux is not installed" in str(refused.value)


def test_fix_only_does_exactly_what_it_names():
    runner = Runner()
    with pytest.raises(ValueError, match="Not started: the toolchain go is not in the store"):
        resolve([image(), toolchain()], only=["image:localhost/claude-sandbox:latest"], terminal=False,
                run_command=runner)
    assert runner.ran == ["sandbox-ai build claude"]
    done, _said, runner = resolve([image(), toolchain()], terminal=False,
                                  only=["toolchain:go", "image:localhost/claude-sandbox:latest"])
    assert sorted(done) == ["image:localhost/claude-sandbox:latest", "toolchain:go"]
    with pytest.raises(ValueError, match="--fix-only mcp:nope: no fix of that id is offered here"):
        resolve([image()], only=["mcp:nope"], terminal=False)


# --- a fix that fails, and one that did not help ----------------------------------------------------------------

def test_a_failing_fix_stops_the_start_with_the_end_of_its_output_and_the_command_to_retry():
    runner = Runner(fail=["sandbox-ai toolchain add go"])
    with pytest.raises(ValueError) as failed:
        resolve([image(), toolchain(), unsigned_server()], fix_all=True, run_command=runner)
    text = str(failed.value)
    assert runner.ran == ["sandbox-ai build claude", "sandbox-ai toolchain add go"]  # (nothing after it is tried)
    assert "fix 2/3 failed: sandbox-ai toolchain add go ended with exit status 3" in text
    assert "  | STEP 2/3: RUN make\n  | error: it broke" in text
    assert "Try it again with: sandbox-ai toolchain add go" in text and "nothing was started" in text


def test_a_fix_that_ran_and_did_not_help_is_not_tried_twice():
    runner = Runner()
    with pytest.raises(ValueError) as refused:
        resolve([image()], fix_all=True, attempted=["image:localhost/claude-sandbox:latest"], run_command=runner)
    assert runner.ran == [] and "still so after its fix ran" in str(refused.value)


def test_run_one_gives_the_status_and_the_last_lines_of_a_real_command(capfd):
    status, tail = f.run_one([sys.executable, "-c", "import sys; [print('line', n) for n in range(40)]; sys.exit(4)"])
    assert status == 4 and tail[-1] == "line 39" and len(tail) == f.TAIL_LINES
    assert "line 0" in capfd.readouterr().err  # (all of it was shown as it came, on standard error)
    assert f.run_one(["/nonexistent/program"])[0] == 127
    assert f.runnable(["sandbox-ai", "build", "claude"])[-3:] == [str(ROOT / "sandbox/main.py"), "build", "claude"]


# --- the plan as JSON, the options a session does not keep, the host's packages ---------------------------------

def test_the_plan_as_json_carries_the_command_the_verdict_and_the_setting():
    plan = f.plan_json([image(), unsigned_server(), image()], "high", "each")
    assert plan["questions"] == "each" and plan["mode"] == "high" and len(plan["fixes"]) == 2
    first, second = plan["fixes"]
    assert first["command"] == "sandbox-ai build claude" and first["verdict"] == "offered"
    assert second["verdict"] == "refused" and second["unsigned"] and "--mode medium" in second["text"]
    assert f.plan_json([], "medium", "nonsense")["questions"] == "all"


def test_a_session_keeps_none_of_the_fix_options():
    argv = ["claude", "--fix", "--mcp", "x", "--fix-only", "image:a", "--fix-only=mcp:b", "--fix-plan", "--", "--fix"]
    assert f.without_options(argv) == ["claude", "--mcp", "x", "--", "--fix"]


def test_a_session_keeps_none_of_their_short_forms_either():
    """Security review 2026-11-22: the launcher hands a session its options as they were typed, and -fx, -fo ID and
    -fp (the short forms of --fix, --fix-only and --fix-plan) were kept - a later run of the session's options would
    fix without asking. As their long forms, none of them is kept."""
    argv = ["claude", "-fx", "--mcp", "x", "-fo", "image:a", "-fo=mcp:b", "-fp", "--", "-fx"]
    assert f.without_options(argv) == ["claude", "--mcp", "x", "--", "-fx"]


@pytest.mark.parametrize("release, platform, expected", [
    ('ID=debian\n', "linux", ["sudo", "apt", "install", "-y", "podman", "passt", "uidmap"]),
    ('ID=linuxmint\nID_LIKE="ubuntu debian"\n', "linux", ["sudo", "apt", "install", "-y", "podman", "passt", "uidmap"]),
    ('ID=fedora\n', "linux", ["sudo", "dnf", "install", "-y", "podman", "passt", "shadow-utils"]),
    ('ID=arch\n', "linux", ["sudo", "pacman", "-S", "--needed", "podman", "passt", "shadow"]),
    ('ID="opensuse-tumbleweed"\nID_LIKE="opensuse suse"\n', "linux",
     ["sudo", "zypper", "install", "podman", "passt", "shadow"]),
    ('ID=alpine\n', "linux", ["sudo", "apk", "add", "podman", "passt", "shadow-uidmap"]),
    ("", "darwin", ["brew", "install", "podman", "passt"])])
def test_a_missing_tool_is_the_distributions_package_command_never_run(tmp_path, release, platform, expected):
    path = tmp_path / "os-release"
    path.write_text(release)
    manager = f.package_manager(path, platform, which=lambda name: None)
    item = f.tool_fix(["podman", "pasta", "newuidmap"], manager=manager)
    assert item["commands"] == [expected] and item["verdict"] == "manual"


def test_an_unknown_distribution_gets_words_not_a_wrong_command(tmp_path):
    manager = f.package_manager(tmp_path / "none", "linux", which=lambda name: None)
    item = f.tool_fix(["podman"], manager=manager)
    assert manager == (None, []) and item["commands"] == [] and item["verdict"] == "manual"
    assert f.package_manager(tmp_path / "none", "linux",
                             which=lambda name: "/usr/bin/dnf" if name == "dnf" else None)[0] == "dnf"


# --- a choice between ways to start: the start's own options --------------------------------------------------------

def workers(lead="codex"):
    """The two answers of "--cairn-workers claude: claude is no agent of this session": add it, or take the lead."""
    lacking = f"--cairn-workers claude: claude is no agent of this session (the lead {lead})"
    return [f.fix("option", "with-claude", lacking, "add claude beside the lead", options={"add": ["--with", "claude"]},
                  choice="cairn-workers-claude", security="claude runs in the same sandbox"),
            f.fix("option", "cairn-workers-codex", lacking, "let the session's own codex be Cairn's worker",
                  options={"remove": ["--cairn-workers"], "add": ["--cairn-workers", "codex"]},
                  choice="cairn-workers-claude")]


ARGV = ["codex", "--workspace", "/w", "--cairn", "--cairn-workers", "claude", "--", "do it"]


def test_a_fix_can_change_the_starts_own_options():
    add, replace = workers()
    assert add["verdict"] == "offered" and add["commands"] == [] and add["choice"] == "cairn-workers-claude"
    assert f.start_options(ARGV, [add]) == ["codex", "--workspace", "/w", "--cairn", "--cairn-workers", "claude",
                                            "--with", "claude", "--", "do it"]
    assert f.start_options(ARGV, [replace]) == ["codex", "--workspace", "/w", "--cairn", "--cairn-workers", "codex",
                                                "--", "do it"]
    equals = ["codex", "--cairn-workers=claude", "--cairn"]
    assert f.start_options(equals, [replace]) == ["codex", "--cairn", "--cairn-workers", "codex"]
    assert f.start_options(ARGV, [image()]) == ARGV  # (a fix of the host changes no option)
    started = f.with_starts([add, image()], ARGV)
    assert started[0]["start"] == "sandbox-ai codex --workspace /w --cairn --cairn-workers claude --with claude -- 'do it'"
    assert started[1]["start"] == ""


def test_the_answers_of_one_question_are_listed_together_and_one_is_chosen_by_its_letter():
    lines = "\n".join(f.listing([image(), *workers()]))
    assert "2. --cairn-workers claude: claude is no agent of this session (the lead codex)\n     -> one of:" in lines
    assert "a) add claude beside the lead; SECURITY: claude runs in the same sandbox: --with claude" in lines
    assert "b) let the session's own codex be Cairn's worker: --cairn-workers codex" in lines
    read = reader("", "b")
    done, _said, runner = resolve([image(), *workers()], read=read)
    assert done == ["image:localhost/claude-sandbox:latest", "option:cairn-workers-codex"]
    assert runner.ran == ["sandbox-ai build claude"]  # (an option's change runs nothing on the host)
    assert "Do it (1) now and go on?" in read.asked[0]  # (2 is asked next)
    assert read.asked[1].endswith("2: which one? [a/b], or [n]one: ")
    # alone it is the only question; no letter, no start
    read = reader("a")
    done, _said, _ = resolve(workers(), read=read)
    assert done == ["option:with-claude"] and len(read.asked) == 1
    for reply in ("", "n", "c", "ab", None):
        with pytest.raises(ValueError):
            resolve(workers(), read=reader(reply))
    # with the setting each: the same question, after the fixes asked one by one
    done, _said, _ = resolve([image(), *workers()], questions="each", read=reader("y", "a"))
    assert done == ["image:localhost/claude-sandbox:latest", "option:with-claude"]


def test_a_choice_is_never_taken_by_fix_and_the_refusal_names_both_whole_command_lines():
    fixes = f.with_starts(workers(), ARGV)
    runner = Runner()
    for more in ({"fix_all": True}, {"terminal": False}):
        with pytest.raises(ValueError) as refused:
            resolve(fixes, run_command=runner, read=lambda question: pytest.fail("asked"), **{"terminal": False, **more})
        lines = str(refused.value).splitlines()
        at = lines.index("Start it one of these ways instead:")
        assert lines[at + 1:at + 3] == [
            "  sandbox-ai codex --workspace /w --cairn --cairn-workers claude --with claude -- 'do it'",
            "  sandbox-ai codex --workspace /w --cairn --cairn-workers codex -- 'do it'"]
        assert not any(line.startswith("Or start again with --fix") for line in lines)
    # named, it is taken: alone, or with --fix for what else the start needs
    done, _said, _ = resolve(fixes, only=["option:cairn-workers-codex"], terminal=False)
    assert done == ["option:cairn-workers-codex"]
    done, _said, runner = resolve([image(), *fixes], only=["option:with-claude"], fix_all=True, terminal=False)
    assert done == ["image:localhost/claude-sandbox:latest", "option:with-claude"]
    with pytest.raises(ValueError, match="two answers of one choice"):
        resolve(fixes, only=["option:with-claude", "option:cairn-workers-codex"], terminal=False)
    # what an earlier round of the same start did is not asked of the next one
    done, _said, runner = resolve([image()], only=["option:with-claude"], fix_all=True, terminal=False,
                                  attempted=["option:with-claude"])
    assert done == ["image:localhost/claude-sandbox:latest"]
    plan = f.plan_json(fixes, "medium", "all")
    assert plan["fixes"][0]["options"] == {"add": ["--with", "claude"], "remove": []}
    assert plan["fixes"][0]["command"].startswith("sandbox-ai codex ") and plan["fixes"][1]["choice"] == "cairn-workers-claude"
