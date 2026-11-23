"""What a start lacks, asked about in the app (user, 2026-11-22: "if something is missing or not working, offer to
help and get it run ... dont block sorry not available/integrated - build it and attach it").

The prerequisites check (the Board's Start here, on the host lab: test_remote_mcp_folder's fixtures, its sandbox-ai
answered by a stand-in) reads the launcher's own plan there (`claude ... --fix-plan`: sandbox/fixes.py) with what it
looks for itself, lists everything in ONE question - Fix all, Choose…, Cancel - or asks each on its own (the setting
What a start lacks: sandbox-ai's default fix_questions), runs the fixes on that host with their progress, says the
outcome, and the start goes on. What is unsigned is named; what sandbox-ai cannot do itself (a package that needs
root, what the security mode refuses) is said with its command and never run; a fix that failed is not offered
twice."""
import json
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")

from sandbox_ai_app import backend, prerequisites, widgets  # noqa: E402
from test_remote_chooser import driven, host, rows, select  # noqa: E402,F401
from test_remote_mcp_folder import added, answer, at_once, lab, on_lab  # noqa: E402,F401
from test_remote_paths import board, open_button, sai, shown_project, workspaces  # noqa: E402,F401

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")

TITLE = "What the start lacks"


def fix(kind, name, missing, title, commands, **more):
    item = {"id": f"{kind}:{name}", "kind": kind, "name": name, "missing": missing, "title": title,
            "commands": commands, "command": " && ".join(" ".join(c) for c in commands), "duration": "",
            "unsigned": False, "security": "", "terminal": False, "needed": True,
            "verdict": "offered" if commands else "manual", "why": ""}
    item.update(more)
    return item


IMAGE = fix("image", "localhost/claude-sandbox:latest", "the image of claude is not built on this host", "build it",
            [["sandbox-ai", "build", "claude"]], duration="~5 min")
GO = fix("toolchain", "go", "the toolchain go is not in the store", "add it from the signed catalog",
         [["sandbox-ai", "toolchain", "add", "go"]], duration="a download")
PULLED = fix("image", "docker.io/x/y:1", "the image docker.io/x/y:1 is not on this host", "pull it from its registry",
             [["sandbox-ai", "images", "pull", "docker.io/x/y:1"]], unsigned=True,
             security="an image nobody reviewed is pulled")
CATATONIT = fix("tool", "catatonit", "catatonit is not installed", "install it",
                [["sudo", "pacman", "-S", "--needed", "catatonit"]], verdict="manual",
                why="it needs root: sandbox-ai never runs sudo for you")
LOGIN = fix("login", "claude", "the host has no claude login", "log in now", [["sandbox-ai", "login", "claude"]],
            terminal=True, needed=False)


@pytest.fixture
def planned(lab, monkeypatch):
    """lab's `claude ... --fix-plan` answers lab.plan (what is still missing); a fix's command run there takes its
    item off (lab.failing: commands that fail instead); lab.questions: its default fix_questions."""
    real = lab.host_run
    lab.plan, lab.failing, lab.questions, lab.plans = [], set(), "all", 0

    def run(name, *args, **kw):
        if args and args[-1] == "--fix-plan":
            lab.plans += 1
            if lab.plan is None:  # (an older sandbox-ai there)
                return 2, "", "sandbox-ai claude: error: unrecognized arguments: --fix-plan"
            return 0, json.dumps({"fixes": lab.plan, "questions": lab.questions, "mode": "medium"}), ""
        for item in list(lab.plan or []):
            if any(tuple(command[1:]) == args for command in item["commands"]):
                if item["kind"] == "mcp":  # (the app's own: lab's `mcp add` adds it, and the plan has it no more)
                    lab.plan.remove(item)
                    break
                lab.ran.append((name, *args))
                if args in lab.failing:
                    return 1, "", "sandbox-ai: error: it broke (fake)"
                lab.plan.remove(item)
                return 0, "done", ""
        return real(name, *args, **kw)
    monkeypatch.setattr(lab, "host_run", run)
    monkeypatch.setattr(lab, "fix_questions", lambda: lab.questions)
    return lab


def ready(on_lab, lab):
    """The Board on lab with an MCP server that is there: only the launcher's plan says what is missing."""
    shown = on_lab(["playwright"])
    lab.lab_servers.append("playwright")
    return shown


def asked(lab, title=TITLE):
    """The questions of TITLE, each once (the fixtures' two recorders of widgets.choose both note it)."""
    found = [(text, tuple(key for key, _ in choices)) for shown, text, choices in lab.asked if shown == title]
    return [(text, list(keys)) for text, keys in dict.fromkeys(found)]


def ran(lab):
    return [args for args in lab.ran if args[1:2] in (("build",), ("toolchain",), ("images",), ("login",))]


def test_one_question_for_everything_then_fixed_there_and_the_start_goes_on(on_lab, planned, monkeypatch):
    shown = ready(on_lab, planned)
    planned.plan = [dict(IMAGE), dict(GO), dict(LOGIN)]
    answer(planned, monkeypatch)
    assert shown.start() is False, "not at once: what it lacks is made first"
    (text, keys), = asked(planned)
    assert keys == ["all", "choose"]
    assert "1. the image of claude is not built on this host → build it: sandbox-ai build claude (~5 min)" in text
    assert "2. the toolchain go is not in the store → add it from the signed catalog: sandbox-ai toolchain add go" in text
    assert "on lab" in text and "claude login" not in text  # (its login: the app's own question, not the plan's)
    assert ran(planned) == [("lab", "build", "claude"), ("lab", "toolchain", "add", "go")]
    assert len(shown.started) == 1, (shown.started, widgets.SHOWN)
    assert not [text for title, text, _ in widgets.SHOWN if title == "Not started"], widgets.SHOWN


def test_cancel_runs_nothing_and_says_what_is_missing_with_its_command(on_lab, planned, monkeypatch):
    shown = ready(on_lab, planned)
    planned.plan = [dict(IMAGE), dict(GO)]
    answer(planned, monkeypatch)
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)
    shown.start()
    assert ran(planned) == [] and shown.started == []
    said = "\n".join(text for title, text, _ in widgets.SHOWN if title == "Not started")
    assert "the image of claude is not built on this host: not done (sandbox-ai build claude)." in said
    assert "the toolchain go is not in the store: not done (sandbox-ai toolchain add go)." in said


def test_choose_asks_each_and_the_start_waits_for_what_was_declined(on_lab, planned, monkeypatch):
    shown = ready(on_lab, planned)
    planned.plan = [dict(IMAGE), dict(GO)]
    answer(planned, monkeypatch)
    monkeypatch.setattr(widgets, "AUTO_CHOICE", "choose")
    each = []

    def confirm(parent, title, text, accept, **kw):
        each.append((title, text, kw.get("destructive")))
        return "toolchain go" not in text
    monkeypatch.setattr(prerequisites, "confirm", confirm)
    shown.start()
    assert [title for title, _, _ in each] == [f"{TITLE} (1 of 2)", f"{TITLE} (2 of 2)"]
    assert ran(planned) == [("lab", "build", "claude")] and shown.started == []
    said = "\n".join(text for title, text, _ in widgets.SHOWN if title == "Not started")
    assert "the toolchain go is not in the store: not done" in said and "the image of claude" not in said


def test_the_setting_each_asks_one_question_per_item_and_marks_what_is_unsigned(on_lab, planned, monkeypatch):
    shown = ready(on_lab, planned)
    planned.plan, planned.questions = [dict(IMAGE), dict(PULLED)], "each"
    answer(planned, monkeypatch)
    each = []

    def confirm(parent, title, text, accept, **kw):
        each.append(text)
        return True
    monkeypatch.setattr(prerequisites, "confirm", confirm)
    monkeypatch.setattr(widgets, "AUTO_CHOICE", "do")
    shown.start()
    assert asked(planned) == [], "no question for everything: each on its own"
    assert len(each) == 1 and "the image of claude" in each[0]
    # what is unsigned is asked with No as its default (the choice dialog: Cancel), never an Enter away
    (text, keys), = asked(planned, title=f"{TITLE} (2 of 2)")
    assert keys == ["do"] and "UNSIGNED" in text and "security: an image nobody reviewed is pulled" in text
    assert ran(planned) == [("lab", "build", "claude"), ("lab", "images", "pull", "docker.io/x/y:1")]
    assert len(shown.started) == 1


def test_what_sandbox_ai_cannot_do_itself_is_said_with_its_command_and_never_run(on_lab, planned, monkeypatch):
    shown = ready(on_lab, planned)
    refused = {**PULLED, "verdict": "refused", "why": "not offered in the security mode high: ... (--mode medium)"}
    planned.plan = [dict(IMAGE), dict(CATATONIT), refused]
    answer(planned, monkeypatch)
    shown.start()
    (text, _keys), = asked(planned)
    assert "catatonit" not in text and "1. the image of claude" in text  # (only what can be done is asked about)
    assert ran(planned) == [("lab", "build", "claude")]
    assert not any("pacman" in " ".join(args) or args[1:3] == ("images", "pull") for args in planned.ran)
    assert shown.started == [], "the start still lacks what only the human can do"
    said = "\n".join(text for title, text, _ in widgets.SHOWN if title == "Not started")
    assert "catatonit is not installed: it needs root: sandbox-ai never runs sudo for you (sudo pacman -S --needed " \
        "catatonit)" in said
    assert "not offered in the security mode high" in said and "--mode medium" in said
    assert "sandbox-ai images pull" not in said  # (what the mode does not take is no command to copy)


def test_a_fix_that_fails_is_said_and_not_offered_a_second_time(on_lab, planned, monkeypatch):
    shown = ready(on_lab, planned)
    planned.plan = [dict(GO)]
    planned.failing = {("toolchain", "add", "go")}
    answer(planned, monkeypatch)
    shown.start()
    assert ran(planned) == [("lab", "toolchain", "add", "go")] and shown.started == []
    said = "\n".join(text for title, text, _ in widgets.SHOWN if title == "Preparing the start")
    assert "it broke (fake)" in said, widgets.SHOWN
    planned.asked.clear()
    widgets.SHOWN.clear()
    shown.start()  # (pressed again: the fix ran and is missing still - said, not asked and run again)
    assert asked(planned) == [] and ran(planned) == [("lab", "toolchain", "add", "go")] and shown.started == []
    said = "\n".join(text for title, text, _ in widgets.SHOWN if title == "Not started")
    assert "the toolchain go is not in the store: still so after its fix ran" in said


def updating(planned, monkeypatch, status=0):
    """`sandbox-ai hosts update lab`, run on this machine: answered STATUS; done, lab knows the fix plan."""
    real, updates = planned.host_run, []

    def run(name, *args, **kw):
        if name == "local" and args == ("hosts", "update", "lab"):
            updates.append(args)
            if status == 0:
                planned.plan = []
            return status, "lab: 0.1.200 -> 0.1.218" if status == 0 else "", "" if status == 0 else "no route"
        return real(name, *args, **kw)
    monkeypatch.setattr(planned, "host_run", run)
    return updates


def test_a_host_too_old_for_the_plan_is_offered_its_update_and_declined_the_start_goes_on(on_lab, planned,
                                                                                         monkeypatch):
    """Round 2: a host whose sandbox-ai does not know --fix-plan (or an option of the start) - its update is a line of
    the one question, run on this machine; declined (Cancel), the start goes on as before, and it is not asked again
    until the app ends."""
    shown = ready(on_lab, planned)
    planned.plan = None
    updates = updating(planned, monkeypatch)
    answer(planned, monkeypatch)
    monkeypatch.setattr(widgets, "AUTO_CHOICE", "nothing")  # (no such key: the question's first answer...)
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)  # (...Cancel: nothing is done)
    shown.start()
    (text, keys), = asked(planned)
    assert "sandbox-ai on lab is too old for this start (it does not know --fix-plan)" in text
    assert "sandbox-ai hosts update lab" in text and updates == []
    assert len(shown.started) == 1, widgets.SHOWN  # (as before: the launcher there says what it does not know)
    assert not [text for title, text, _ in widgets.SHOWN if title == "Not started"], widgets.SHOWN
    shown.start()
    assert len(asked(planned)) == 1 and len(shown.started) == 2  # (not asked again)


def test_a_host_too_old_is_updated_from_this_machine_and_its_plan_read_again(on_lab, planned, monkeypatch):
    shown = ready(on_lab, planned)
    planned.plan = None
    updates = updating(planned, monkeypatch)
    answer(planned, monkeypatch)
    shown.start()
    assert updates == [("hosts", "update", "lab")], widgets.SHOWN
    assert planned.plans == 2 and len(shown.started) == 1, widgets.SHOWN  # (read again: it answers now)


def test_an_update_that_fails_is_said_and_nothing_starts(on_lab, planned, monkeypatch):
    shown = ready(on_lab, planned)
    planned.plan = None
    updates = updating(planned, monkeypatch, status=1)
    answer(planned, monkeypatch)
    shown.start()
    assert updates and shown.started == []
    said = "\n".join(text for title, text, _ in widgets.SHOWN if title == "Preparing the start")
    assert "no route" in said, widgets.SHOWN


def test_nothing_missing_is_no_question(on_lab, planned, monkeypatch):
    shown = ready(on_lab, planned)
    planned.plan = [dict(LOGIN)]  # (what the start goes on without is no reason to ask here)
    answer(planned, monkeypatch)
    shown.start()
    assert asked(planned) == [] and ran(planned) == [] and len(shown.started) == 1


def test_the_plan_joins_the_app_s_own_question_about_mcp_servers(on_lab, planned, monkeypatch):
    """One question for the MCP server the Board found missing and the image the launcher's plan names."""
    shown = on_lab(["playwright"])
    planned.plan = [dict(IMAGE), fix("mcp", "playwright", "the MCP server playwright is not added on this host",
                                     "add it", [["sandbox-ai", "mcp", "add", "playwright"]])]
    answer(planned, monkeypatch)
    shown.start()
    (text, _keys), = asked(planned)
    assert "1. the MCP server playwright is not added on lab → added from its catalog" in text
    assert "2. the image of claude is not built on this host → build it: sandbox-ai build claude" in text
    assert text.count("playwright") == 1, "the launcher's own item for it is not listed a second time"
    assert added(planned) == [("lab", "mcp", "add", "playwright")] and ("lab", "build", "claude") in planned.ran
    assert len(shown.started) == 1, widgets.SHOWN


# --- a host's answer is its text ----------------------------------------------------------------------------------

def test_a_plan_is_checked_before_anything_of_it_is_shown_or_run():
    plan = backend.checked_fix_plan(json.dumps({"fixes": [
        {**IMAGE, "missing": "the image\x1b[31m of\nclaude"},
        {**GO, "commands": [["rm", "-rf", "/"]], "command": "rm -rf /"},
        {**GO, "id": "toolchain:x", "commands": [["sandbox-ai", "net", "allow", "evil.example"]]},
        {**GO, "id": "toolchain:y", "commands": [["sandbox-ai", "toolchain", "add", "go\nrm"]]},
        "junk", {"id": "tool:podman", "verdict": "nonsense"}], "questions": "weird", "mode": "medium"}))
    first, second, third, fourth, fifth = plan["fixes"]
    assert "\x1b" not in first["missing"] and "\n" not in first["missing"] and first["verdict"] == "offered"
    assert first["missing"].startswith("the image") and first["missing"].endswith("of claude")
    # a command that is not sandbox-ai's own, or not one of the commands a fix may be: shown, never run
    assert second["verdict"] == "manual" and third["verdict"] == "manual" and fourth["verdict"] == "manual"
    assert fifth["verdict"] == "manual" and plan["questions"] == "all"
    for text in ("{}", "[]", "not json", json.dumps({"fixes": "x"})):
        with pytest.raises(ValueError, match="not a fix plan"):
            backend.checked_fix_plan(text)


def test_plan_extras_leave_the_app_s_own_kinds_and_what_ran_before():
    variant = fix("image", "localhost/claude-sandbox-media:latest", "the image of claude is not built", "build it",
                  [["sandbox-ai", "build", "--variant", "media"]])
    plan = {"fixes": [IMAGE, GO, LOGIN, variant, fix("mcp", "x", "m", "t", [["sandbox-ai", "mcp", "add", "x"]]),
                      fix("identity", "work", "m", "t", [["sandbox-ai", "identities", "add", "work"]])]}
    extras, again = prerequisites.plan_extras(plan, {"--image-variant": ["media"]}, attempted={"toolchain:go"})
    assert [item["id"] for item in extras] == [IMAGE["id"]] and [item["id"] for item in again] == ["toolchain:go"]
    extras, _ = prerequisites.plan_extras(plan, {})
    assert [item["id"] for item in extras] == [IMAGE["id"], "toolchain:go", variant["id"]]
    assert prerequisites.plan_extras(None, {}) == ([], [])


# --- the setting -------------------------------------------------------------------------------------------------

def test_settings_has_the_choice_and_saves_it_to_sandbox_ai_s_defaults(qapp, sai, monkeypatch, tmp_path):
    from sandbox_ai_app import settings
    saved = []
    monkeypatch.setattr(sai, "save_sandbox_default", lambda key, value: saved.append((key, value)))
    page = settings.SettingsDialog(sai)
    combo = page.fields["fix_questions"]
    assert [combo.itemData(i) for i in range(combo.count())] == ["all", "each"] and combo.currentData() == "all"
    combo.setCurrentIndex(1)
    combo.setCurrentIndex(0)
    assert saved == [("fix_questions", "each"), ("fix_questions", None)]
    assert sai.fix_questions() == "all" and sai.fix_questions_known()
    with pytest.raises(ValueError, match="fix_questions is all or each"):
        backend.SandboxAI.save_sandbox_default(sai, "fix_questions", "sometimes")


# --- a choice between ways to start (the launcher's "options" fixes) ------------------------------------------------

LACKING = "--cairn-workers claude: claude is no agent of this session (the lead codex)"
WITH = fix("option", "with-claude", LACKING, "add claude beside the lead", [], verdict="offered",
           options={"add": ["--with", "claude"], "remove": []}, choice="cairn-workers-claude",
           security="claude runs in the same sandbox", start="sandbox-ai codex --cairn --with claude")
OWN = fix("option", "cairn-workers-codex", LACKING, "let the session's own codex be Cairn's worker", [],
          verdict="offered", options={"add": ["--cairn-workers", "codex"], "remove": ["--cairn-workers"]},
          choice="cairn-workers-claude", start="sandbox-ai codex --cairn --cairn-workers codex")


def test_a_way_to_start_is_asked_and_the_page_that_can_change_its_start_goes_on(on_lab, planned, monkeypatch):
    """User, 2026-11-22 (the app: "Der Launcher lehnt ab: --cairn-workers claude: no agent of this session ..."):
    the two ways are asked about; the page changes its start as chosen and starts again."""
    shown = ready(on_lab, planned)
    planned.plan = [dict(WITH), dict(OWN)]
    answer(planned, monkeypatch)
    monkeypatch.setattr(widgets, "AUTO_CHOICE", "option:cairn-workers-codex")
    applied = []

    def apply(add, remove):
        applied.append((add, remove))
        planned.plan = []  # (the start as it is now lacks nothing)
        return True
    monkeypatch.setattr(shown, "prerequisite_apply_options", apply)
    shown.start()
    (text, keys), = asked(planned)
    assert keys == ["option:with-claude", "option:cairn-workers-codex"] and text.startswith(LACKING)
    assert "a) add claude beside the lead: --with claude - security: claude runs in the same sandbox" in text
    assert "b) let the session's own codex be Cairn's worker: --cairn-workers codex" in text
    assert applied == [(["--cairn-workers", "codex"], ["--cairn-workers"])]
    assert len(shown.started) == 1, (shown.started, widgets.SHOWN)


def test_the_board_puts_the_chosen_way_onto_its_own_parts_and_starts(on_lab, planned, monkeypatch):
    """Round 2: the Board itself takes the way chosen - an agent part for --with claude - as one step of Undo, and
    the start goes on with it."""
    shown = ready(on_lab, planned)
    planned.plan = [dict(WITH), dict(OWN)]
    answer(planned, monkeypatch)
    monkeypatch.setattr(widgets, "AUTO_CHOICE", "option:with-claude")
    before = shown.undo.count()
    real = planned.host_run

    def run(name, *args, **kw):  # (with claude beside the lead the start lacks nothing)
        if args and args[-1] == "--fix-plan" and "claude" in " ".join(args[1:]).split("--with", 1)[-1]:
            planned.plan = []
        return real(name, *args, **kw)
    monkeypatch.setattr(planned, "host_run", run)
    shown.start()
    from sandbox_ai_app import board as B
    agent, argv, _args = B.start_argv(shown.sai, shown.board)
    assert "claude" in " ".join(argv).split("--with", 1)[-1], argv
    assert shown.undo.count() == before + 1  # (one step of Undo)
    assert len(shown.started) == 1, widgets.SHOWN


def test_where_the_page_cannot_change_its_start_both_ways_are_said(on_lab, planned, monkeypatch):
    shown = ready(on_lab, planned)
    planned.plan = [dict(WITH), dict(OWN)]
    answer(planned, monkeypatch)
    monkeypatch.setattr(shown, "prerequisite_apply_options", lambda add, remove: False)  # (a Team part, say)
    shown.start()
    assert shown.started == []
    said = "\n".join(text for title, text, _ in widgets.SHOWN if title == "Not started")
    assert LACKING + ": change the start - add claude beside the lead (--with claude) or let the session's own " \
        "codex be Cairn's worker (--cairn-workers codex)." in said, widgets.SHOWN


def test_an_option_change_of_a_hosts_answer_is_only_launcher_options_as_words():
    def checked(options):
        plan = backend.checked_fix_plan(json.dumps({"fixes": [{**WITH, "options": options}]}))
        return plan["fixes"][0]
    assert checked({"add": ["--with", "claude"], "remove": []})["options"] == {"add": ["--with", "claude"], "remove": []}
    assert checked({"add": ["--with", "claude"], "remove": []})["verdict"] == "offered"
    for bad in ({"add": ["claude"]}, {"add": ["--with", "a\nb"]}, {"add": ["--", "rm"]}, {"remove": ["cairn"]},
                {"add": [], "remove": []}, "x", {"add": ["--with", " claude"]}):
        item = checked(bad)
        assert item["options"] is None and item["verdict"] == "manual", bad
