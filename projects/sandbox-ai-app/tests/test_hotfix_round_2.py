"""Hotfix round 2 of 0.1.95: realistic user stories driven through the app (offscreen) as the user works - the
Board, its preset gallery and its starts running the real sandbox-ai of SANDBOX_AI_HOME as subprocesses, with only
Podman, tmux, npm and ssh faked (sandbox-ai's tests/live_fakes.py). One test per bug, named after what the user saw."""
import os
import shlex

import pytest

pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, board as B, presets as PR, widgets  # noqa: E402

from test_live_regressions import board_page, run_tab, user  # noqa: E402,F401 - the fixtures

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME"), reason="set SANDBOX_AI_HOME to a sandbox-ai "
                                                                             "checkout")


def as_user(argv, host):
    """ARGV (the app's: this Python, -I, sandbox-ai's main.py ...) as the user runs it: where the tests run as root,
    through the fakes' python3 that says euid 1000."""
    import sys
    return [host.env["FAKE_PYTHON"], *argv[1:]] if host.shimmed and argv and argv[0] == sys.executable else list(argv)


def gallery(page, key, workspace, values=None, agent=None):
    """The preset KEY of the gallery loaded onto the Board as the user does it: its form filled in (folders made,
    defaults kept, optional ones left empty, VALUES by the value's name)."""
    preset = PR.catalogue(page.sai).find(key)
    names = dict(preset.value_names)
    filled = {}
    for placeholder in preset.placeholders:
        kind, name = B.PLACEHOLDER.fullmatch(placeholder.marker).groups()
        given = (values or {}).get(names.get(placeholder.marker, name))
        if given is not None:
            filled[placeholder.marker] = given
        elif kind == "folder":
            folder = workspace.parent / f"value-{name}"
            folder.mkdir(exist_ok=True)
            filled[placeholder.marker] = str(folder)
        else:
            filled[placeholder.marker] = placeholder.default or ("" if placeholder.optional else "x")
    page.set_board(B.from_argv(page.sai, agent or "claude", [], [], workspaces=[str(workspace)]), workspace)
    if agent:
        page.settings = lambda: {"board_grid": 20, "default_agent": agent}
    page.new_from_preset(preset, filled)
    return preset


# --- a preset with a first task, its agent switched on the Board ----------------------------------------------------

TASK = "Read README* and docs/. Check every claim."
FORMS = {"claude": [TASK], "codex": [TASK], "opencode": ["--prompt", TASK], "qwen": ["--prompt-interactive=" + TASK],
         "goose": ["run", "--interactive", "--text=" + TASK], "hermes": ["chat", "--query=" + TASK],
         "dsh": ["headless", TASK]}


@pytest.mark.parametrize("before", sorted(FORMS))
def test_the_first_task_follows_the_agent_switched_on_the_board(before):
    """Story: the private preset tor-research-v2 loaded for opencode, then the agent switched to qwen on the Board.
    The first task stayed opencode's "--prompt TEXT" - for Qwen Code --prompt runs the task without a terminal and
    ends; a claude task switched to hermes, goose or dsh became an unknown command of theirs. The task takes each
    agent's own form (the launcher's presets.prompt_args) when the agent changes; other arguments stay."""
    for after in sorted(FORMS):
        props = {"agent": before, "arguments": shlex.join(FORMS[before])}
        B.switch_agent(props, after)
        assert shlex.split(props["arguments"]) == FORMS[after], (before, after)
    props = {"agent": before, "arguments": "--verbose"}
    B.switch_agent(props, "opencode" if before != "opencode" else "claude")
    assert props["arguments"] == "--verbose", "arguments that are no task stay as they are"


def test_the_task_forms_are_the_launchers(board_page):
    """The app's forms are what sandbox-ai's presets.prompt_args gives each agent (a start with --preset)."""
    module = board_page.sai.module("presets")
    for agent, form in FORMS.items():
        assert module.prompt_args({"prompt": TASK}, agent) == form, agent
        assert B.task_args(agent, TASK) == form


@pytest.mark.parametrize("agent", ["qwen", "goose", "hermes", "dsh"])
def test_a_preset_loaded_for_another_agent_gives_it_the_task_in_its_form(board_page, user, agent):
    """Story: the default agent of the settings is qwen (goose, hermes, dsh), a sandbox-ai preset with a first task
    is chosen in the gallery: its board gave the task as a bare argument (only OpenCode got --prompt)."""
    page = board_page
    page.set_board(B.from_argv(page.sai, agent, [], [], workspaces=[str(user.workspace)]), user.workspace)
    preset = PR.catalogue(page.sai).find("sandbox-ai:sai-docs-lie-detector")
    import dataclasses
    preset = dataclasses.replace(preset, agents=())  # (for any agent: the board keeps the one of the settings)
    board = PR.build(page.sai, preset, user.workspace, agent=agent, values={})
    _, _, args = B.values(board, page.sai)
    assert args == page.sai.module("presets").prompt_args({"prompt": preset.prompt}, agent), args


# --- Start pressed while what the preset needs is still being made --------------------------------------------------

def test_start_while_the_presets_mcp_server_is_added_waits_and_then_starts(board_page, user, tmp_path):
    """Story: coding-memory chosen in the gallery; the Board adds its MCP server engram in the terminal tab "Preparing
    the start" (asked when the preset was loaded). Start pressed meanwhile said "Not started: Unknown MCP server:
    engram ... add a server with sandbox-ai mcp add" - the server being added that very moment - and once it was
    added nothing started: the notes promised "The start goes on when it is done". Now Start waits for the
    preparation (said as information, not refused) and the start goes on once the server is there."""
    import json
    page = board_page
    # (the agent's image is there: one that is not built is asked about with the rest and built first - the
    # launcher's own plan, read on the start's host; here the story is the MCP server alone)
    user.add_image("localhost/claude-sandbox:latest")
    gallery(page, "sandbox-ai:sai-coding-memory", user.workspace)
    (argv, title, ended), = page.tabs  # the preset's preparation runs in its tab
    page.tabs.clear()
    assert title == "Preparing the start"
    page.check()
    widgets.SHOWN.clear()
    assert page.start() is False
    assert not [text for title_, text, _ in widgets.SHOWN if title_ == "Not started"], widgets.SHOWN
    assert "goes on when it is done" in page.notice.text(), page.notice.text()
    assert not page.started
    # The tab adds engram (here as the fakes can: its definition, its image in the user's store), and ends well.
    brain = [a.split("=", 1)[1] for a in json.loads(argv[-1])[0][1] if a.startswith("/data=")]
    added = user.run("mcp", "add", "mcp:engram", "--no-pull", *(["--mount", "/data=" + brain[0]] if brain else []))
    assert added.returncode == 0, added.stderr
    user.add_image(json.loads((user.home / ".config/sandbox-ai/mcp.json").read_text())["engram"]["image"])
    ended(0)
    assert len(page.started) == 1, "the start that waited goes on by itself"
    assert "--mcp" in page.started[0] and "engram" in page.started[0]


def test_a_start_that_waited_for_a_failed_preparation_is_not_started_and_says_why(board_page, user, tmp_path):
    """The other end of the story: the preparation fails (the image cannot be pulled) - the start that waited is not
    started, said in the log, and a later preparation of the same board does not start it by surprise."""
    page = board_page
    gallery(page, "sandbox-ai:sai-coding-memory", user.workspace)
    (argv, title, ended), = page.tabs
    page.tabs.clear()
    page.check()
    page.start()
    ended(1)
    assert not page.started
    assert not page.__dict__.get("preparing_waiters"), "nothing left over to start later"


# --- a preset's MCP server from a named catalog ----------------------------------------------------------------------

@pytest.mark.parametrize("key,entry", [("sandbox-ai:sai-firmware-iot", "mcp:binwalk"),
                                       ("sandbox-ai:sai-software-analysis", "mcp:capa")])
def test_a_presets_mcp_server_is_added_from_the_catalog_the_preset_names(board_page, user, key, entry):
    """Story: firmware-iot (software-analysis) chosen in the gallery, its MCP server added by the Board ("Add them
    from their catalogs now?" - Add). The tab "Preparing the start" ran `sandbox-ai mcp add binwalk` and failed:
    "binwalk is in several catalogs: mcp:binwalk, hub:binwalk" - the preset names hub:binwalk, the Board dropped the
    catalog. It adds the preset's entry now. (Since the MCP review of 2026-11-17 both presets name sandbox-ai-mcp's
    reviewed builds, mcp:binwalk and mcp:capa, and give them their folder at /work with --mcp-mount: the entry's
    required folder is that one, not asked for.)"""
    import json
    page = board_page
    gallery(page, key, user.workspace)
    (argv, title, ended), = page.tabs
    steps = json.loads(argv[-1])
    add = next(step[1] for step in steps if "add" in step[1])
    assert add[add.index("add") + 1] == entry, add
    # the folder the entry requires is the preset's own (its --mcp-mount ...:/work=FOLDER:ro): given, not asked for
    assert add[add.index("--mount") + 1].startswith("/work=") and add[-1].endswith(":ro"), add
    asked = [text for title_, text, _ in widgets.SHOWN if title_.startswith("MCP server ")]
    assert not asked, widgets.SHOWN
    # The command the tab runs knows the entry (the fakes pull nothing: added without its image here).
    result = user.run(*add[add.index("mcp"):], "--no-pull")
    assert result.returncode == 0, result.stderr
    assert entry.split(":")[1] in json.loads((user.home / ".config/sandbox-ai/mcp.json").read_text())


# --- a preset's MCP server whose catalog entry lists the hosts it needs ------------------------------------------

def test_a_server_that_needs_named_hosts_is_added_with_only_them_without_a_question(board_page, user):
    """Story: research-memory chosen in the gallery, its MCP servers added by the Board. duckduckgo's catalog entry
    names the hosts it needs (html.duckduckgo.com:443). sandbox-ai could not limit a server to them, so the Board asked
    whether to give it the whole outside network (--network full) - and a yes let it reach the LAN and the metadata
    address too (MCP review, group B). Now `mcp add` holds such an entry to its hosts (network allowlist, the rules
    in its namespace): nothing is asked, nothing of --network is added, and it is defined with only its hosts."""
    import json
    page = board_page
    gallery(page, "sandbox-ai:sai-research-memory", user.workspace)
    assert not [text for _, text, _ in widgets.SHOWN if "html.duckduckgo.com:443" in text or "--network full" in text]
    (argv, title, ended), = page.tabs
    adds = {step[1][step[1].index("add") + 1]: step[1][step[1].index("add"):] for step in json.loads(argv[-1])}
    assert "--network" not in adds["mcp:duckduckgo"] and "--network" not in adds["mcp:web-fetch"], adds
    result = user.run("mcp", *adds["mcp:duckduckgo"], "--no-pull")
    assert result.returncode == 0, result.stderr
    defined = json.loads((user.home / ".config/sandbox-ai/mcp.json").read_text())["duckduckgo"]
    assert defined["network"] == "allowlist" and defined["hosts"] == ["html.duckduckgo.com:443"]
    assert "reaches only html.duckduckgo.com:443" in result.stdout

def test_after_a_failed_step_the_steps_not_tried_are_said_as_not_tried(board_page, user):
    """Story: tor-research-v2's nine MCP servers added in the tab Preparing the start; the first pull failed and the
    tab stopped there ("the start waits"). The Board then said each of the nine "see the terminal tab Preparing the
    start (exit status 1)" - eight of them were never tried. Now only the one that failed points to the tab; the rest
    are said as not tried."""
    page = board_page
    gallery(page, "sandbox-ai:sai-research-memory", user.workspace)
    (argv, title, ended), = page.tabs
    widgets.SHOWN.clear()
    result = run_tab(as_user(argv, user), env=user.env)
    assert result.returncode != 0
    ended(result.returncode)
    said = next(text for title_, text, _ in widgets.SHOWN if title_ == "Preparing the start")
    lines = said.splitlines()[1:]
    assert len(lines) == 3, said
    assert "see the terminal tab Preparing the start (exit status 1)" in lines[0], said
    for line in lines[1:]:
        assert "not tried" in line and "exit status" not in line, said


# --- Copy for a bug report: the tokens the privacy check knows are masked -----------------------------------------

def test_copy_for_a_bug_report_masks_hugging_face_google_npm_pypi_and_stripe_tokens(monkeypatch, tmp_path):
    """Story: the data-ml preset with a Hugging Face token; a line of app.log held it (a failed download's URL, a
    command's output), and "Copy for a bug report" put it on the clipboard as it was: the app's mask knew GitHub,
    GitLab, OpenAI, Slack and AWS tokens, not the other forms sandbox-ai's privacy check blocks in a push."""
    import string
    from sandbox_ai_app import applog, logview
    tail = (string.ascii_letters + string.digits) * 2
    tokens = ["hf" + "_" + tail[:34], "AI" + "za" + tail[:35], "npm" + "_" + tail[:36], "pypi" + "-AgE" + tail[:60],
              "sk" + "_live_" + tail[:24]]
    log = tmp_path / "app.log"
    log.write_text("".join(f"INFO [command] downloading with {token} failed\n" for token in tokens))
    monkeypatch.setattr(applog, "log_path", lambda: log)
    report = logview.bug_report()
    for token in tokens:
        assert token not in report, token
        assert applog.looks_secret(token), token


# --- database-services: a service whose image is a value of the preset ----------------------------------------------

@pytest.mark.parametrize("later", [False, True])
def test_a_service_whose_image_is_filled_in_is_read_again_and_starts(board_page, user, later, monkeypatch):
    """Story: database-services chosen in the gallery (its images postgres and redis as the form offers them). Start
    said "Not started: The service db cannot be read: --service db: image='postgres' is the name of one of your
    images" - every time: the service was read while its image was still the preset's placeholder, refused, and the
    refusal kept (the placeholder in its text filled in later, so it named the right image as wrong). A service is
    read again once its values are filled in - in the gallery's form or later on the board - and the board starts."""
    page = board_page
    if later:  # the form left empty: filled in on the board when Start asks for it
        preset = PR.catalogue(page.sai).find("sandbox-ai:sai-database-services")
        page.set_board(B.from_argv(page.sai, "claude", [], [], workspaces=[str(user.workspace)]), user.workspace)
        page.new_from_preset(preset, None)
        values = {p.marker: p.default for p in preset.placeholders}
        from sandbox_ai_app import board_view
        monkeypatch.setattr(board_view.BoardPage, "ask_values", lambda self, wanted: values, raising=False)
        B.fill(page.board, values, page.sai)
        page.rebuild()
    else:
        gallery(page, "sandbox-ai:sai-database-services", user.workspace)
    services = page.board.of_kind("service")
    assert {n.props.get("name") for n in services} == {"db", "cache"}
    assert not any(n.props.get("raw") for n in services), [n.props for n in services]
    assert [n.props["image"] for n in services if n.props["name"] == "db"] == ["postgres"]
    assert not [p.text for p in page.errors() if "cannot be read" in p.text], [p.text for p in page.errors()]
    argv = B.launcher_argv(page.sai, page.board)
    db = argv[argv.index("--service") + 1]
    module = page.sai.module("services")
    assert module.parse_spec(db) == module.parse_spec(
        "db:image=postgres,port=5432,user=999:999,env=POSTGRES_USER=dev,env=POSTGRES_PASSWORD=dev,env=POSTGRES_DB=dev,"
        "env=PGDATA=/var/lib/postgresql/data/pgdata,data=/var/lib/postgresql/data,data=/var/run/postgresql,memory=1g")


# --- Start with what the launcher's check refuses ----------------------------------------------------------------

def test_start_of_a_board_the_launcher_refuses_says_why_instead_of_a_terminal_that_fails(board_page, user, monkeypatch):
    """Story: coding-goose chosen in the gallery on a host without an Anthropic key. The Board's line said "The
    launcher refuses: --api-key ...: no secret ANTHROPIC_API_KEY", yet Start opened a terminal tab that printed the
    same and ended - a start known to fail (only refusals about MCP servers and images held Start back). Start now
    checks once more and, refused again, says why ("Not started", the launcher's words) and opens nothing; once the
    key is kept, the same Start starts."""
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    user.add_image("localhost/goose-sandbox:latest")
    page = board_page
    gallery(page, "sandbox-ai:sai-coding-goose", user.workspace, agent="goose")
    page.check()
    assert page.verdict.text().startswith("The launcher refuses"), page.verdict.text()
    widgets.SHOWN.clear()
    assert page.start() is False
    said = [text for title, text, _ in widgets.SHOWN if title == "Not started"]
    assert said and "no secret ANTHROPIC_API_KEY" in said[0], widgets.SHOWN
    assert "sandbox-ai secrets set ANTHROPIC_API_KEY" in said[0]
    assert not page.started
    kept = user.run("secrets", "set", "ANTHROPIC_API_KEY", "--from-env", "TEST_KEY",
                    env={"SANDBOX_AI_SECRETS": "file", "TEST_KEY": "sk-ant-" + "x" * 30})
    assert kept.returncode == 0, kept.stderr
    widgets.SHOWN.clear()
    page.start()
    assert page.started and not [t for t, _, _ in widgets.SHOWN if t == "Not started"], widgets.SHOWN


# --- another preset loaded while the first one's needs are still made --------------------------------------------

def test_a_preset_loaded_while_another_ones_needs_are_made_asks_for_its_own(board_page, user, monkeypatch):
    """Story: forensics chosen in the gallery (its MCP servers sleuthkit, exiftool, yara added in the tab Preparing
    the start, minutes of pulls), then - changed mind - hardware-analysis. Its own server binwalk was never asked
    for, and Start said "Still preparing (1 of 3): adding the MCP server sleuthkit" and waited for forensics: the
    start's context was the board's id(), which Python hands the next board once the old one is gone (here: every
    id() the same, as after such a reuse). Each board has a number of its own now."""
    from sandbox_ai_app import board_view
    monkeypatch.setattr(board_view, "id", lambda thing: 4242, raising=False)
    page = board_page
    gallery(page, "sandbox-ai:sai-forensics", user.workspace)
    assert len(page.tabs) == 1  # forensics' preparation, still running
    first = page.prerequisite_context()
    widgets.SHOWN.clear()
    gallery(page, "sandbox-ai:sai-hardware-analysis", user.workspace)
    assert page.prerequisite_context() != first
    assert any("binwalk" in text for _, text, _ in widgets.SHOWN), (widgets.SHOWN, page.notice.text())
    assert "sleuthkit" not in page.notice.text(), page.notice.text()
