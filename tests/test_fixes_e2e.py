"""Helpful, not blocking - end to end: the real ./sandbox-ai as a user runs it, on a terminal and in a script, with
only Podman, tmux and npm faked (tests/live_fakes.py). User, 2026-11-22: "if something is missing or not working,
offer to help and get it run. like missing container, not added mcp server... dont block sorry not
available/integrated - build it and attach it."

A start that lacks something lists everything it lacks and what it would do for each, asks once on a terminal (Fix
all / Choose / Cancel; your default fix_questions each: one question per fix), does it with its output and goes on;
--fix does it without asking; without a terminal and without --fix it is refused as before, now always with the
commands to copy. The security mode is never widened by an offer."""
import json
import os
from pathlib import Path
import pty
import select
import subprocess
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import home_guard  # noqa: E402

home_guard.ensure()

import live_fakes as F  # noqa: E402
from test_mcp_build import host as build_host  # noqa: E402,F401  (a fixture: a host with MCP catalogs to build from)

CLAUDE = "localhost/claude-sandbox:latest"
CODEX = "localhost/codex-sandbox:latest"
QUESTION = "[Y]es, all / [c]hoose / [n]o:"


@pytest.fixture
def host(tmp_path):
    return F.Host(tmp_path / "h")


def on_terminal(host, *args, answers=(), env=None, timeout=120):
    """`sandbox-ai ARGS` on a pseudo-terminal: (exit status, what it said). ANSWERS: [(what it must have said,
    what is typed then)], each once, in order."""
    controller, terminal = pty.openpty()
    process = subprocess.Popen(host.command(*args), cwd=host.workspace, env={**host.env, **(env or {})},
                               stdin=terminal, stdout=terminal, stderr=terminal, close_fds=True)
    os.close(terminal)
    said, answers, deadline = b"", list(answers), time.monotonic() + timeout
    seen = 0
    while time.monotonic() < deadline:
        ready, _, _ = select.select([controller], [], [], 0.2)
        if ready:
            try:
                more = os.read(controller, 65536)
            except OSError:
                break
            if not more:
                break
            said += more
            while answers and answers[0][0].encode() in said[seen:]:
                seen = said.index(answers[0][0].encode(), seen) + len(answers[0][0])
                os.write(controller, answers.pop(0)[1].encode() + b"\n")
        elif process.poll() is not None:
            break
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    os.close(controller)
    return process.returncode, said.decode(errors="replace").replace("\r\n", "\n")


def flat(text):
    return " ".join(text.split())


def identities(host):
    path = host.home / ".config/sandbox-ai/identities.json"
    return json.loads(path.read_text())["identities"] if path.exists() else {}


def login(host, agent="claude"):
    """A login of AGENT in sandbox-ai's store, so that only what the test is about is missing."""
    folder = host.home / ".local/share/sandbox-ai/auth" / agent
    folder.mkdir(parents=True, exist_ok=True)
    target = {"claude": ".claude/.credentials.json", "codex": ".codex/auth.json"}[agent]
    (folder / target).parent.mkdir(parents=True, exist_ok=True)
    (folder / target).write_text(json.dumps({"claudeAiOauth": {"accessToken": "a", "refreshToken": "r",
                                                               "expiresAt": 4102444800000}}
                                            if agent == "claude" else {"tokens": {"access_token": "a"}}))


# --- the agent's image -------------------------------------------------------------------------------------------

def test_a_missing_image_is_asked_about_built_and_the_start_goes_on(host):
    status, said = on_terminal(host, "claude", "--identity", "work", answers=[(QUESTION, "")])
    text = flat(said)
    assert "this start needs what is not there yet:" in text
    assert f"the image of claude ({CLAUDE}) is not built on this host -> build it: sandbox-ai build claude (~5 min)" in text
    assert "the identity work is not on this host -> make it, empty" in text
    assert "fix 1/" in text and "done in" in text and "what the start lacked is there now: it goes on" in text
    assert CLAUDE in host.images() and "work" in identities(host)
    assert host.stopped_at() is not None, said[-1500:]  # (the start went on: as far as its pod)
    # asked once, about everything
    assert said.count(QUESTION) == 1


def test_declined_it_is_refused_with_the_commands_and_nothing_is_made(host):
    status, said = on_terminal(host, "claude", "--identity", "work", answers=[(QUESTION, "n")])
    assert status != 0 and host.builds() == [] and "work" not in identities(host) and host.stopped_at() is None
    lines = [line.strip() for line in said.splitlines()]
    assert "What fixes it (copy these lines), then start again:" in lines
    at = lines.index("What fixes it (copy these lines), then start again:")
    assert lines[at + 1] == "sandbox-ai build claude"
    assert lines[at + 2] == "sandbox-ai identities add '--description=made for a start (no logins)' -- work"
    assert "Or start again with --fix: sandbox-ai does it first, without asking." in lines


def test_choose_takes_some_and_the_start_waits_for_the_rest(host):
    login(host)
    status, said = on_terminal(host, "claude", "--identity", "work", "--kit", "house",
                               answers=[(QUESTION, "c"), ("build it: sandbox-ai build claude (~5 min)? [Y/n]", "y"),
                                        ("? [Y/n]", "n"), ("? [Y/n]", "y")])
    assert status != 0 and CLAUDE in host.images() and "work" not in identities(host)
    assert (host.home / ".local/share/sandbox-ai/kits/house/claude").is_dir()  # (the third: chosen, made)
    text = flat(said)
    assert "Not started: the identity work is not on this host" in text and host.stopped_at() is None
    assert "sandbox-ai identities add" in text.split("Not started:")[1]
    assert "sandbox-ai build claude" not in text.split("Not started:")[1]  # (done: no longer to do)


def test_the_setting_each_asks_one_question_per_fix(host):
    login(host)
    assert host.run("defaults", "set", "fix_questions", "each").returncode == 0
    status, said = on_terminal(host, "claude", "--identity", "work",
                               answers=[("build it: sandbox-ai build claude (~5 min)? [Y/n]", ""),
                                        ("-- work (seconds)? [Y/n]", "y")])
    assert QUESTION not in said and said.count("? [Y/n]") == 2
    assert CLAUDE in host.images() and "work" in identities(host) and host.stopped_at() is not None, said[-1500:]
    refused = host.run("defaults", "set", "fix_questions", "sometimes")
    assert refused.returncode != 0 and "fix_questions is one of all, each" in refused.stderr


# --- no terminal: scripts, CI, background -------------------------------------------------------------------------

def test_without_a_terminal_it_is_refused_with_the_complete_block_and_nothing_is_made(host):
    host.add_image(CLAUDE)
    done = host.run("claude", "--identity", "work", "--kit", "house")
    assert done.returncode == 1 and host.stopped_at() is None and "work" not in identities(host)
    lines = [line.strip() for line in done.stderr.splitlines()]
    assert any(line.startswith("agent-sandbox: --identity work: no such identity") for line in lines)  # (as before)
    at = lines.index("What fixes it (copy these lines), then start again:")
    assert lines[at + 1:at + 3] == [
        "sandbox-ai identities add '--description=made for a start (no logins)' -- work",
        "sandbox-ai kits new house --agent claude"]
    assert "Or start again with --fix: sandbox-ai does it first, without asking." in lines
    assert QUESTION not in done.stderr


def test_the_block_really_is_what_fixes_it(host):
    """Copied and run, line by line, the commands of the refusal make the start go on."""
    host.add_image(CLAUDE)
    lines = [line.strip() for line in host.run("claude", "--identity", "work", "--kit", "house").stderr.splitlines()]
    at = lines.index("What fixes it (copy these lines), then start again:")
    for line in lines[at + 1:at + 3]:
        done = subprocess.run(["bash", "-c", line.replace("sandbox-ai", str(F.ROOT / "sandbox-ai"), 1)],
                              env=host.env, cwd=host.workspace, capture_output=True, text=True, timeout=120)
        assert done.returncode == 0, done.stderr
    again = host.run("claude", "--identity", "work", "--kit", "house")
    assert host.stopped_at() is not None, again.stderr[-1500:]


def test_fix_does_it_without_asking_and_starts(host):
    done = host.run("claude", "--identity", "work", "--kit", "house", "--fix")
    text = flat(done.stderr)
    assert "this start needs what is not there yet (--fix: done now, without asking):" in text
    assert CLAUDE in host.images() and "work" in identities(host)
    assert (host.home / ".local/share/sandbox-ai/kits/house/claude").is_dir()
    assert host.stopped_at() is not None, done.stderr[-1500:]
    assert QUESTION not in done.stderr and "sandbox-ai login" not in text.split("without asking")[1].split("fix 1/")[1]


def test_fix_only_does_what_it_names_and_refuses_the_rest(host):
    host.add_image(CLAUDE)
    done = host.run("claude", "--identity", "work", "--kit", "house", "--fix-only", "kit:house")
    assert done.returncode == 1 and (host.home / ".local/share/sandbox-ai/kits/house/claude").is_dir()
    assert "work" not in identities(host) and host.stopped_at() is None
    assert "Not started: the identity work is not on this host" in flat(done.stderr)
    done = host.run("claude", "--identity", "work", "--kit", "house", "--fix-only", "identity:work")
    assert host.stopped_at() is not None, done.stderr[-1500:]
    unknown = host.run("claude", "--identity", "other", "--fix-only", "image:nope")
    assert unknown.returncode == 1 and "--fix-only image:nope: no fix of that id is offered here" in unknown.stderr


def test_a_script_without_fix_still_gets_its_own_image_built_as_before(host):
    """What a start did by itself before there were offers stays: sandbox-ai's own image, built from its pinned
    Containerfile, is built without a question where nobody can be asked (and with SANDBOX_AI_NO_AUTO_BUILD=1 the
    refusal names the command, as before)."""
    done = host.run("claude")
    assert CLAUDE in host.images() and host.stopped_at() is not None, done.stderr[-1500:]
    assert "building it now (sandbox-ai build claude" in done.stderr
    other = F.Host(host.directory / "other")
    refused = other.run("codex", env={"SANDBOX_AI_NO_AUTO_BUILD": "1"})
    assert refused.returncode == 1 and other.builds() == [] and "sandbox-ai build codex" in refused.stderr


def test_a_dry_run_makes_nothing_asks_nothing_and_says_what_it_said_before(host):
    """The app's and the web interface's checks are dry runs: their refusal stays the one line it was (they read
    it); --fix-plan is the plan for them."""
    host.add_image(CLAUDE)
    done = host.run("claude", "--identity", "work", "--dry-run")
    assert done.returncode == 1 and "work" not in identities(host)
    assert done.stderr.strip().splitlines()[-1].startswith("agent-sandbox: --identity work: no such identity")
    assert "What fixes it" not in done.stderr
    assert on_terminal(host, "claude", "--identity", "work", "--dry-run")[1].count(QUESTION) == 0


# --- a fix that fails --------------------------------------------------------------------------------------------

def test_a_failing_fix_stops_the_start_with_the_end_of_its_log_and_the_command_to_retry(host):
    done = host.run("claude", "--fix", env={"FAKE_PODMAN_BUILD_FAIL": "1"})
    text = done.stderr
    assert done.returncode == 1 and host.stopped_at() is None and CLAUDE not in host.images()
    assert "fix 1/1 failed: sandbox-ai build claude ended with exit status" in text
    assert "Its last lines:\n  | " in text
    assert "Try it again with: sandbox-ai build claude" in text and "nothing was started" in text


def test_a_toolchain_that_cannot_be_added_says_why_and_how_to_retry(host):
    """The toolchain is not in the store, and the catalog of this (test) host is not signed (a local one: no test
    reads the published catalog over the network): `toolchain add` refuses it - the fix failed, its words are the
    refusal's, the start did not go on."""
    host.add_image(CLAUDE)
    catalogs = host.directory / "catalogs"
    (catalogs / "sandbox-ai-mcp").mkdir(parents=True)
    (catalogs / "sandbox-ai-mcp" / "toolchains.json").write_text(json.dumps(
        {"schema": "sandbox-ai-toolchains", "version": 1, "toolchains": []}))
    host.env["SANDBOX_AI_LOCAL_CATALOGS"] = str(catalogs)
    refused = host.run("claude", "--toolchain", "go")
    lines = [line.strip() for line in refused.stderr.splitlines()]
    assert refused.returncode == 1 and "sandbox-ai toolchain add go" in lines
    assert any("the toolchain go is not in the store" in line for line in lines)
    done = host.run("claude", "--toolchain", "go", "--fix")
    assert done.returncode == 1 and host.stopped_at() is None
    assert "fix 1/1 failed: sandbox-ai toolchain add go ended with exit status" in done.stderr
    assert "Try it again with: sandbox-ai toolchain add go" in done.stderr


# --- the plan as JSON: what the app and the web interface ask about ------------------------------------------------

def test_fix_plan_lists_everything_and_makes_nothing(host):
    done = host.run("claude", "--identity", "work", "--kit", "house", "--toolchain", "go", "--fix-plan")
    assert done.returncode == 0, done.stderr
    plan = json.loads(done.stdout)
    assert plan["questions"] == "all" and plan["mode"] == "medium"
    found = {item["id"]: item for item in plan["fixes"]}
    assert set(found) >= {"image:claude", "identity:work", "kit:house", "toolchain:go", "login:claude"}
    assert found["image:claude"]["command"] == "sandbox-ai build claude"
    assert found["image:claude"]["verdict"] == "offered" and found["image:claude"]["duration"] == "~5 min"
    assert found["toolchain:go"]["commands"] == [["sandbox-ai", "toolchain", "add", "go"]]
    assert found["login:claude"]["needed"] is False and found["login:claude"]["terminal"] is True
    # the order they are done in: the image before what runs in it
    order = [item["kind"] for item in plan["fixes"]]
    assert order.index("image") < order.index("identity") < order.index("login")
    assert host.builds() == [] and "work" not in identities(host) and host.stopped_at() is None
    # nothing missing that the start needs: an empty plan (the login it goes on without, when there is none)
    host.add_image(CLAUDE)
    login(host)
    assert json.loads(host.run("claude", "--fix-plan").stdout)["fixes"] == []
    assert host.run("defaults", "set", "fix_questions", "each").returncode == 0
    assert json.loads(host.run("claude", "--fix-plan").stdout)["questions"] == "each"


def test_a_project_file_cannot_ask_for_fixes_without_a_question(host):
    host.add_image(CLAUDE)
    (host.workspace / ".sandbox-ai.json").write_text(json.dumps({"version": 1, "agent": "claude",
                                                                 "options": ["--fix"]}))
    trusted = host.run("wizard", "trust", str(host.workspace / ".sandbox-ai.json"))
    assert trusted.returncode != 0
    assert "option --fix is not allowed in a project configuration" in trusted.stdout + trusted.stderr
    done = host.run("claude", "--identity", "work")
    assert done.returncode == 1 and "work" not in identities(host)


# --- MCP servers: added from their catalog, built here unsigned; the security modes ---------------------------------

START = ("codex", "--backend", "podman", "--mcp", "jadx")


def test_an_mcp_server_not_added_is_offered_named_unsigned_built_and_the_start_goes_on(build_host):
    host = build_host
    host.add_image(CODEX)
    login(host, "codex")
    status, said = on_terminal(host, *START, "--mcp-mount", f"jadx:/work={host.apps}", answers=[(QUESTION, "")])
    text = flat(said)
    assert "the MCP server jadx is not added on this host" in text
    assert "build it here from its reviewed source" in text and "UNSIGNED" in text
    assert "SECURITY: nobody signed the image that is built here" in text
    assert f"sandbox-ai mcp add jadx --build --yes --unsigned --mount /work={host.apps}" in text
    assert "smoke: list_files: ok" in said and host.definitions()["jadx"]["signed_by"] is None
    assert "Unknown MCP server" not in said and host.stopped_at() is not None, said[-1500:]
    assert host.run("mcp", "stop", "jadx").returncode == 0  # (the server the start started: ended)


def test_declined_the_server_is_not_built_and_the_refusal_names_its_command(build_host):
    host = build_host
    host.add_image(CODEX)
    status, said = on_terminal(host, *START, "--mcp-mount", f"jadx:/work={host.apps}", answers=[(QUESTION, "n")])
    assert status != 0 and host.build_calls() == [] and "jadx" not in host.definitions()
    assert "Unknown MCP server: jadx" in said
    assert f"sandbox-ai mcp add jadx --build --yes --unsigned --mount /work={host.apps}" in said


def test_a_folder_the_start_does_not_give_is_the_servers_default_and_said(build_host):
    """Since the MCP folder defaults (0.1.21x) a server needs no --mount: /work is the start's workspace, any other
    folder one of the server's own. The fix says where its folders are and adds it so - no folder is asked for or
    made up, and the start goes on."""
    host = build_host
    host.add_image(CODEX)
    login(host, "codex")
    plan = json.loads(host.run(*START, "--fix-plan").stdout)
    (item,) = [item for item in plan["fixes"] if item["id"] == "mcp:jadx"]
    assert item["verdict"] == "offered" and item["unsigned"]
    assert item["commands"] == [["sandbox-ai", "mcp", "add", "jadx", "--build", "--yes", "--unsigned"]]
    assert "its folders - /work: the start's workspace" in item["title"]
    done = host.run(*START, "--fix")
    assert "/work=FOLDER" not in done.stderr and "/work: the start's workspace" in flat(done.stderr)
    mounts = host.definitions()["jadx"]["mounts"]
    assert [(mount["target"], mount["host"]) for mount in mounts] == [("/work", "@workspace")]
    assert host.stopped_at() is not None, done.stderr[-1500:]
    assert host.run("mcp", "stop", "jadx").returncode == 0


@pytest.mark.parametrize("mode", ["high", "medium"])
def test_a_strict_mode_is_never_offered_an_unsigned_server(build_host, mode):
    host = build_host
    host.add_image(CODEX)
    login(host, "codex")
    options = (*START, "--mcp-mount", f"jadx:/work={host.apps}:ro", "--mode", mode)
    done = host.run(*options, "--fix")
    text = flat(done.stderr)
    if mode == "high":
        assert done.returncode == 1 and host.build_calls() == [] and "jadx" not in host.definitions()
        assert "not offered in the security mode high: it would add something nobody signed" in text
        assert "the mode medium takes it (--mode medium)" in text
        assert "sandbox-ai mcp add jadx" not in text and "--fix:" not in text  # (no command of it, no way around)
        status, said = on_terminal(host, *options)
        assert QUESTION not in said and status != 0 and host.build_calls() == []
        plan = json.loads(host.run(*options, "--fix-plan").stdout)
        item = next(item for item in plan["fixes"] if item["id"] == "mcp:jadx")
        assert item["verdict"] == "refused" and item["unsigned"] and "--mode medium" in item["why"]
    else:
        assert "jadx" in host.definitions() and host.stopped_at() is not None, done.stderr[-1500:]
        assert host.run("mcp", "stop", "jadx").returncode == 0


def test_a_server_no_catalog_has_keeps_its_words_and_is_never_a_question(build_host):
    host = build_host
    host.add_image(CODEX)
    status, said = on_terminal(host, "codex", "--backend", "podman", "--mcp", "nowhere")
    assert status != 0 and QUESTION not in said and "Unknown MCP server: nowhere" in said
    assert "no catalog lists it" in said


# --- sandbox-ai doctor --------------------------------------------------------------------------------------------

def test_doctor_ends_with_the_commands_and_fix_runs_those_that_are_sandbox_ais_own(host):
    done = host.run("doctor", "--plain", "--no-color", env={"SANDBOX_AI_INIT_BINARY": "none"})
    lines = [line.strip() for line in done.stdout.splitlines()]
    at = lines.index("What fixes it (copy these lines):")
    block = lines[at + 1:]
    assert "sandbox-ai build claude" in block and "sandbox-ai build codex" in block
    assert any(line.startswith("sandbox-ai doctor --fix runs the ") and "without asking" in line for line in block)
    assert any("a login, the plugin installer and what needs root are yours to run" in line for line in block)
    assert host.builds() == []  # (the checks change nothing)
    fixed = host.run("doctor", "--plain", "--no-color", "--fix")
    assert "fix 1/" in fixed.stderr and "done; run it again" in fixed.stderr, fixed.stderr[-800:]
    assert {CLAUDE, CODEX} <= set(host.images())
    # (a login runs in a terminal: never from a script; a host package is never installed)
    assert not any("login" in " ".join(call) for call in host.calls())
    found = {row["check"]: row for row in json.loads(host.run("doctor", "--json").stdout)}
    assert found["Podman init"]["status"] == "ok" and "fix" not in found["Podman init"]


def test_a_host_package_is_in_the_plan_with_its_command_and_never_run(tmp_path):
    """Podman's init binary for an MCP image server whose image has no init (sandbox/hostinit.py): the plan lists
    it as what only the human can do - the distribution's package command, or words where the distribution is not
    known - and --fix does not go on without it."""
    host = F.Host(tmp_path / "h")
    image = "ghcr.io/example/server@sha256:" + "a" * 64
    host.add_image(CLAUDE, image)
    config = host.home / ".config/sandbox-ai"
    config.mkdir(parents=True, exist_ok=True)
    (config / "mcp.json").write_text(json.dumps({"files": {"image": image, "network": "none"}}))
    (config / "mcp.json").chmod(0o600)
    env = {"SANDBOX_AI_INIT_BINARY": "none"}
    plan = json.loads(host.run("claude", "--mcp", "files", "--fix-plan", env=env).stdout)
    (item,) = [item for item in plan["fixes"] if item["kind"] == "tool"]
    assert item["id"] == "tool:catatonit" and item["verdict"] == "manual" and item["needed"]
    assert "catatonit, the init binary Podman's --init takes from this host, is not installed" in item["missing"]
    assert all(command[0] != "sandbox-ai" for command in item["commands"])  # (sudo ..., or none: never ours to run)
    done = host.run("claude", "--mcp", "files", "--fix", env=env)
    assert done.returncode == 1 and host.stopped_at() is None
    assert "needs an init process as its PID 1" in done.stderr and "not done for you" in done.stderr


# --- a choice between ways to start (the start's own options) -------------------------------------------------------

CAIRN, BROWSER, TEAM = "localhost/sandbox-ai-cairn:latest", "localhost/sandbox-ai-browser:latest", "localhost/team-sandbox:latest"


@pytest.fixture
def cairn_host(host):
    host.add_image(CODEX, "localhost/opencode-sandbox:latest", TEAM)
    host.add_image(CAIRN, labels={"sandbox-ai.role": "cairn"})
    host.add_image(BROWSER, labels={"sandbox-ai.role": "browser"})
    login(host, "codex")
    return host


def test_a_worker_that_is_no_agent_of_the_session_is_a_choice_not_only_a_refusal(cairn_host):
    """User, 2026-11-22 (the app: "Der Launcher lehnt ab:"): "agent-sandbox: --cairn-workers claude: no agent of
    this session (the lead opencode); add --with claude". Now: the agent beside the lead, or - where the session has
    one of Cairn's workers - that one instead; asked on a terminal, and the start goes on with the choice."""
    host = cairn_host
    status, said = on_terminal(host, "codex", "--cairn", "--cairn-workers", "claude",
                               answers=[("which one? [a/b], or [n]one:", "b")])
    text = flat(said)
    assert "--cairn-workers claude: claude is no agent of this session (the lead codex) -> one of:" in text
    assert "a) add claude beside the lead" in text and ": --with claude" in text
    assert "b) let the session's own codex be Cairn's worker: --cairn-workers codex" in text
    assert "going on as: sandbox-ai codex --cairn --cairn-workers codex" in text
    assert host.stopped_at() is not None, said[-1500:]
    assert QUESTION not in said  # (nothing else was missing: the choice is the only question)


def test_without_a_terminal_the_refusal_names_both_whole_command_lines(cairn_host):
    host = cairn_host
    for options in ([], ["--fix"]):  # (a choice is the human's: --fix does not guess it)
        done = host.run("codex", "--cairn", "--cairn-workers", "claude", *options)
        lines = [line.strip() for line in done.stderr.splitlines()]
        assert done.returncode == 1 and host.stopped_at() is None
        assert any(line.startswith("agent-sandbox: --cairn-workers claude: no agent of this session (the lead codex)")
                   for line in lines)  # (the refusal as before)
        at = lines.index("Start it one of these ways instead:")
        assert lines[at + 1].startswith("sandbox-ai codex --cairn --cairn-workers claude") and \
            "--with claude" in lines[at + 1]
        assert lines[at + 2].startswith("sandbox-ai codex --cairn") and "--cairn-workers codex" in lines[at + 2] and \
            "--cairn-workers claude" not in lines[at + 2]
    # the user's start (the lead opencode, since 0.1.216 one of Cairn's workers itself): the agent beside it, or the
    # lead as Cairn's worker - both whole command lines
    done = host.run("opencode", "--cairn", "--cairn-workers", "claude")
    lines = [line.strip() for line in done.stderr.splitlines()]
    assert any(line.startswith("agent-sandbox: --cairn-workers claude: no agent of this session (the lead opencode); "
                               "add --with claude") for line in lines)
    at = lines.index("Start it one of these ways instead:")
    assert lines[at + 1:at + 3] == ["sandbox-ai opencode --cairn --cairn-workers claude --with claude",
                                    "sandbox-ai opencode --cairn --cairn-workers opencode"]
    plan = json.loads(host.run("opencode", "--cairn", "--cairn-workers", "claude", "--fix-plan").stdout)
    add, own = [item for item in plan["fixes"] if item["kind"] == "option"]
    assert add["id"] == "option:with-claude" and add["options"] == {"add": ["--with", "claude"], "remove": []}
    assert own["id"] == "option:cairn-workers-opencode" and own["options"] == {
        "add": ["--cairn-workers", "opencode"], "remove": ["--cairn-workers"]}
    assert add["choice"] == own["choice"] == "cairn-workers-claude" and add["verdict"] == own["verdict"] == "offered"
    # ... and on a terminal the lead is taken as the worker, and the start goes on
    status, said = on_terminal(host, "opencode", "--cairn", "--cairn-workers", "claude",
                               answers=[("which one? [a/b], or [n]one:", "b")])
    assert "going on as: sandbox-ai opencode --cairn --cairn-workers opencode" in flat(said)
    assert host.stopped_at() is not None, said[-1500:]


def test_named_the_choice_is_taken_and_fix_does_what_else_the_start_then_needs(host):
    """--fix-only names the way to go on (the web interface's page does, after its question); with --fix the team
    image the start needs then is built too, and it starts."""
    host.add_image("localhost/opencode-sandbox:latest")
    host.add_image(CAIRN, labels={"sandbox-ai.role": "cairn"})
    host.add_image(BROWSER, labels={"sandbox-ai.role": "browser"})
    done = host.run("opencode", "--cairn", "--cairn-workers", "claude", "--fix-only", "option:with-claude", "--fix")
    text = flat(done.stderr)
    assert "going on as: sandbox-ai opencode --cairn --cairn-workers claude" in text and "--with claude" in text
    assert host.builds(), "the team image the start needs with --with claude was built"
    assert host.stopped_at() is not None, done.stderr[-2000:]


def test_an_agents_feature_without_that_agent_is_the_same_kind_of_choice(host):
    host.add_image(CLAUDE)
    done = host.run("claude", "--hermes-enable", "web")
    lines = [line.strip() for line in done.stderr.splitlines()]
    assert done.returncode == 1 and any("--hermes-enable is for Hermes" in line for line in lines)
    at = lines.index("Start it one of these ways instead:")
    assert "--with hermes" in lines[at + 1] and lines[at + 2] == "sandbox-ai claude"
    login(host)
    status, said = on_terminal(host, "claude", "--hermes-enable", "web",
                               answers=[("which one? [a/b], or [n]one:", "b")])
    assert "going on as: sandbox-ai claude" in flat(said) and host.stopped_at() is not None, said[-1500:]


def test_a_wrong_mcp_name_is_said_before_anything_is_fixed(host):
    """Journeys round 1: `claude --mcp mcp:calculator --identity work` (the catalog's id, as `mcp add` takes it) asked
    to build the image and make the identity, built for minutes, and only then said "--mcp NAME: 1-32 letters ...:
    'mcp:calculator'". A name no start takes is refused first, nothing made, with the name it takes."""
    status, said = on_terminal(host, "claude", "--mcp", "mcp:calculator", "--identity", "work",
                               answers=[(QUESTION, "")], timeout=60)
    assert status != 0 and QUESTION not in said, said
    assert host.builds() == [] and "work" not in identities(host), said
    text = flat(said)
    assert "--mcp NAME: 1-32 letters, digits, '_' or '-': 'mcp:calculator'" in text, text
    assert "--mcp calculator" in text and "sandbox-ai mcp add mcp:calculator" in text, text


def test_one_lack_with_two_ways_is_one_item_and_the_question_names_what_it_does(host):
    """Journeys round 1: `claude --identity work --vpn office` listed "the VPN profile office is not on this host"
    twice under two numbers (the Tor profile only you choose, and your own profile not done for you), then asked "Do
    these 2 now" of 4 numbered items without saying which. One lack is one item with both ways below it; a question
    that does not take every item names the ones it does."""
    status, said = on_terminal(host, "claude", "--identity", "work", "--vpn", "office",
                               answers=[("[c]hoose / [n]o:", "n")], timeout=60)
    lines = [line.strip() for line in said.splitlines()]
    listed = [line for line in lines if line[:3] in {f"{n}. " for n in range(1, 10)}]
    assert [line for line in listed if "VPN profile office" in line] == \
        ["3. the VPN profile office is not on this host"], listed
    at = lines.index("3. the VPN profile office is not on this host")
    assert lines[at + 1].startswith("-> not done for you (") and lines[at + 2].startswith(
        "-> or, if you want it so: make office a Tor profile"), lines[at:at + 3]
    assert "Do these 2 (1, 2) now and go on? [Y]es, all / [c]hoose / [n]o:" in flat(said), said


def test_paranoid_offers_a_server_without_network_that_is_not_added_yet(host):
    """Journeys round 1: `claude --mode paranoid --mcp calculator` (not added yet; its catalog's entry has no
    network) was refused as "--mcp calculator widens the security mode paranoid: the MCP server reaches the network"
    - a server not defined was taken for a command server with network. It is offered as any lack is; once added,
    its definition is what the mode judges."""
    login(host)
    host.add_image(CLAUDE)
    run = host.run("claude", "--mode", "paranoid", "--mcp", "calculator", "--fix-plan")
    assert "widens the security mode paranoid" not in run.stderr, run.stderr
    plan = json.loads(run.stdout)
    assert [item["id"] for item in plan["fixes"]] == ["mcp:calculator"], plan
