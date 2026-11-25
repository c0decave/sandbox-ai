"""The web interface's start form asks what the start lacks before it opens the start's terminal: POST
/api/hosts/HOST/fix-plan runs `sandbox-ai AGENT ... --fix-plan` there (nothing is made), the page lists the fixes -
all at once with Fix all, Choose…, Cancel, or a tick per fix (the host's default fix_questions) - names what is
unsigned, lists what is not offered with its command, and the terminal it opens then does exactly what was chosen
(--fix-only ID). End to end too: the service against the real ./sandbox-ai with Podman faked."""
import json
import shutil
import subprocess

import pytest

import live_fakes as F
import test_web as tw
from test_web import FAKE_DOM, run_driver

w = tw.w
CLAUDE = "localhost/claude-sandbox:latest"


def through(host):
    """Api.runner's form: `sandbox-ai ARGS` as HOST's user runs it."""
    def runner(name, args, timeout=120, input=None):
        run = subprocess.run(host.command(*args), capture_output=True, text=True, env=host.env, timeout=timeout,
                             cwd=host.workspace, stdin=subprocess.DEVNULL)
        return run.returncode, run.stdout, run.stderr
    return runner


def test_end_to_end_the_plan_is_asked_and_the_start_does_what_was_chosen(tmp_path):
    host = F.Host(tmp_path / "h")
    api = w.Api(through(host))
    form = {"agent": "claude", "workspace": str(host.workspace), "options": ["--identity", "work"]}
    plan = api.dispatch("POST", "/api/hosts/local/fix-plan", form)
    found = {item["id"]: item for item in plan["fixes"]}
    assert plan["questions"] == "all" and plan["mode"] == "medium"
    assert found["image:claude"]["verdict"] == "offered" and found["image:claude"]["command"] == "sandbox-ai build claude"
    assert found["identity:work"]["needed"] and "commands" not in found["identity:work"]  # (words for the page only)
    assert host.builds() == [] and host.stopped_at() is None  # (nothing was made for the question)
    # the terminal the page opens then: exactly the chosen fixes, done by the start before it goes on
    query = {"agent": "claude", "workspace": str(host.workspace), "options": json.dumps(form["options"]),
             "fix": json.dumps(["image:claude", "identity:work"])}
    args = w.start_args("local", query, through(host))
    assert args[-6:] == ["--fix-only", "image:claude", "--fix-only", "identity:work", "--identity", "work"]
    done = host.run(*args)
    assert CLAUDE in host.images() and host.stopped_at() is not None, done.stderr[-1500:]
    # nothing missing any more: an empty plan (the login the start goes on without is no question of the page's)
    again = api.dispatch("POST", "/api/hosts/local/fix-plan", form)
    assert [item for item in again["fixes"] if item["needed"]] == []


def test_the_form_never_passes_fix_and_only_ids_of_a_plan():
    query = {"agent": "claude", "workspace": "/w", "options": json.dumps([])}
    for bad in (["all"], ["image:a b"], ["image:x;rm"], [":x"], ["IMAGE:x"]):
        with pytest.raises(w.Refused, match="fix is a list of fix ids"):
            w.start_args("local", {**query, "fix": json.dumps(bad)})
    for options in (["--fix"], ["-fx"], ["--fix-only", "image:x"], ["--fix-only=image:x"]):
        with pytest.raises(w.Refused, match="not options of the form"):
            w.start_args("local", {**query, "options": json.dumps(options)})
    # after "--" it is the agent's own argument
    assert w.start_args("local", {**query, "options": json.dumps(["--", "--fix"])})[-2:] == ["--", "--fix"]


def test_a_host_that_has_no_fix_plan_yet_or_refuses_is_an_error_the_page_starts_without(tmp_path):
    def runner(name, args, timeout=120, input=None):
        return 2, "", "usage: sandbox-ai claude ...\nsandbox-ai claude: error: unrecognized arguments: --fix-plan\n"
    with pytest.raises(w.Refused):
        w.Api(runner).dispatch("POST", "/api/hosts/local/fix-plan", {"agent": "claude", "workspace": "/w", "options": []})

    def not_a_plan(name, args, timeout=120, input=None):
        return 0, json.dumps({"agent": "claude"}), ""
    with pytest.raises(w.Refused, match="not a fix plan"):
        w.Api(not_a_plan).dispatch("POST", "/api/hosts/local/fix-plan", {"agent": "claude", "workspace": "/w", "options": []})


def test_a_hosts_answer_is_text_for_the_page_never_markup_or_a_command_to_run():
    plan = {"fixes": [{"id": "image:x", "kind": "image", "missing": "the image\nof x <b>", "title": "build it",
                       "command": "sandbox-ai build x", "commands": [["rm", "-rf", "/"]], "verdict": "offered",
                       "needed": True},
                      {"id": "weird id", "kind": "mcp", "missing": "m", "title": "t", "command": "c",
                       "verdict": "offered"},
                      {"id": "tool:podman", "missing": "podman is not installed", "verdict": "nonsense"}, "junk"],
            "questions": "sometimes", "mode": "medium"}

    def runner(name, args, timeout=120, input=None):
        assert args[-1] == "--fix-plan"
        return 0, json.dumps(plan), ""
    answer = w.Api(runner).dispatch("POST", "/api/hosts/local/fix-plan", {"agent": "claude", "workspace": "/w",
                                                                         "options": []})
    first, second, third = answer["fixes"]
    assert first["missing"] == "the image of x <b>" and "commands" not in first and first["verdict"] == "offered"
    assert second["verdict"] == "manual"  # (an id the start's --fix-only would not take: never asked for)
    assert third["verdict"] == "manual" and answer["questions"] == "all"


PAGE = FAKE_DOM + r"""
const PLAN = __PLAN__;
(async () => {
  const result = {};
  vm.runInContext('state.me = { user: "admin", csrf: "c", terminals: true }; state.host = "ct1"; state.hosts = [{ name: "ct1", kind: "ssh" }];', context);
  let plan = PLAN;
  answer = (method, path, sent) => {
    if (path === "/api/hosts/ct1/fix-plan") { if (plan === null) throw new Error("unrecognized arguments: --fix-plan"); return plan; }
    return method === "GET" ? [] : { output: "" };
  };
  const query = { agent: "claude", name: "", workspace: "/w", options: "[]" };
  const ask = () => vm.runInContext('askFixPlan(__main, "ct1", ' + JSON.stringify(query) + ')', context);
  const texts = () => main.all().filter(n => n.tagName === "li").map(n => n.textContent);
  const buttons = () => main.all().filter(n => n.tagName === "button" && !n.hidden).map(n => n.textContent);
  // one question for everything: Fix all
  let asked = ask(); await settle();
  result.sent = JSON.parse(calls.find(c => c.path.endsWith("/fix-plan")).body);
  result.lines = texts(); result.buttons = buttons();
  result.marked = main.all().filter(n => n.tagName === "li" && n.className.includes("marked")).length;
  button(main, "Fix all and start").fire("click");
  result.all = await asked;
  result.closed = texts().length;
  // Choose…: a tick per fix, then the ticked ones
  asked = ask(); await settle();
  button(main, "Choose…").fire("click");
  result.chooseButtons = buttons();
  const boxes = main.all().filter(n => n.tagName === "input");
  result.ticks = boxes.map(b => [Boolean(b.checked), Boolean(b.attributes.disabled !== undefined)]);
  boxes[1].checked = false;
  button(main, "Do the ticked ones and start").fire("click");
  result.chosen = await asked;
  // Cancel
  asked = ask(); await settle();
  button(main, "Cancel").fire("click");
  result.cancelled = await asked;
  // the host's default each: the ticks at once
  plan = Object.assign({}, PLAN, { questions: "each" });
  asked = ask(); await settle();
  result.eachButtons = buttons();
  button(main, "Do the ticked ones and start").fire("click");
  result.each = await asked;
  // nothing the start needs is offered (only what needs root), nothing missing, no plan at all: no question
  plan = { fixes: PLAN.fixes.filter(f => f.verdict !== "offered"), questions: "all" };
  result.onlyManual = await ask();
  plan = { fixes: [], questions: "all" };
  result.nothing = await ask();
  plan = null;
  result.noPlan = await ask();
  vm.runInContext('setLanguage("de")', context);
  result.german = vm.runInContext('[t("Fix all and start"), t("Choose…"), t("What the start lacks")]', context);
  console.log(JSON.stringify(result));
})().catch(e => { console.error(e.stack); process.exit(1); });
"""

PLAN = {"questions": "all", "mode": "medium", "fixes": [
    {"id": "image:localhost/claude-sandbox:latest", "kind": "image", "missing": "the image of claude is not built",
     "title": "build it", "command": "sandbox-ai build claude", "duration": "~5 min", "unsigned": False,
     "security": "", "terminal": False, "needed": True, "verdict": "offered", "why": ""},
    {"id": "mcp:jadx", "kind": "mcp", "missing": "the MCP server jadx is not added", "title": "build it here",
     "command": "sandbox-ai mcp add jadx --build --yes", "duration": "minutes", "unsigned": True,
     "security": "nobody signed it", "terminal": False, "needed": True, "verdict": "offered", "why": ""},
    {"id": "tool:catatonit", "kind": "tool", "missing": "catatonit is not installed", "title": "install it",
     "command": "sudo apt install -y catatonit", "duration": "", "unsigned": False, "security": "",
     "terminal": False, "needed": True, "verdict": "manual", "why": "it needs root"},
    {"id": "login:claude", "kind": "login", "missing": "the host has no claude login", "title": "log in",
     "command": "sandbox-ai login claude", "duration": "", "unsigned": False, "security": "", "terminal": True,
     "needed": False, "verdict": "offered", "why": ""}]}


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_the_page_asks_once_or_each_and_gives_the_terminal_what_was_chosen(tmp_path):
    result = run_driver(tmp_path, PAGE.replace("__PLAN__", json.dumps(PLAN)), "fix-plan")
    assert result["sent"] == {"agent": "claude", "name": "", "workspace": "/w", "options": []}
    lines = result["lines"]
    assert len(lines) == 3  # (the login in a terminal of its own is no part of the page's question)
    assert lines[0].strip() == "the image of claude is not built → build it: sandbox-ai build claude (~5 min)"
    assert "UNSIGNED" in lines[1] and "security: nobody signed it" in lines[1] and result["marked"] == 1
    assert "not done for you: it needs root — sudo apt install -y catatonit" in lines[2]
    assert result["buttons"] == ["Fix all and start", "Choose…", "Cancel"]
    assert result["all"] == ["image:localhost/claude-sandbox:latest", "mcp:jadx"] and result["closed"] == 0
    assert result["chooseButtons"] == ["Fix all and start", "Do the ticked ones and start", "Cancel"]
    assert result["ticks"] == [[True, False], [True, False], [False, True]]  # (what is not offered cannot be ticked)
    assert result["chosen"] == ["image:localhost/claude-sandbox:latest"]
    assert result["cancelled"] is None
    assert result["eachButtons"] == ["Do the ticked ones and start", "Cancel"]
    assert result["each"] == ["image:localhost/claude-sandbox:latest", "mcp:jadx"]
    assert result["onlyManual"] == [] and result["nothing"] == [] and result["noPlan"] == []
    assert result["german"] == ["Alles beheben und starten", "Wählen…", "Was dem Start fehlt"]


CHOICE_PAGE = FAKE_DOM + r"""
const PLAN = __PLAN__;
(async () => {
  const result = {};
  vm.runInContext('state.me = { user: "admin", csrf: "c", terminals: true }; state.host = "ct1"; state.hosts = [{ name: "ct1", kind: "ssh" }];', context);
  answer = (method, path) => (path === "/api/hosts/ct1/fix-plan" ? PLAN : method === "GET" ? [] : { output: "" });
  const asked = vm.runInContext('askFixPlan(__main, "ct1", { agent: "codex", name: "", workspace: "/w", options: "[]" })', context);
  await settle();
  result.lines = main.all().filter(n => n.tagName === "li").map(n => n.textContent);
  result.buttons = main.all().filter(n => n.tagName === "button" && !n.hidden).map(n => n.textContent);
  const boxes = main.all().filter(n => n.tagName === "input");
  result.before = boxes.map(b => Boolean(b.checked));
  boxes[1].checked = true; boxes[1].fire("change");
  boxes[2].checked = true; boxes[2].fire("change");
  result.after = boxes.map(b => Boolean(b.checked));
  button(main, "Do the ticked ones and start").fire("click");
  result.chosen = await asked;
  console.log(JSON.stringify(result));
})().catch(e => { console.error(e.stack); process.exit(1); });
"""


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_a_choice_between_ways_to_start_is_one_tick_of_several_never_made_for_you(tmp_path):
    lacking = "--cairn-workers claude: claude is no agent of this session (the lead codex)"
    plan = {"questions": "all", "mode": "medium", "fixes": [
        PLAN["fixes"][0],
        {"id": "option:with-claude", "kind": "option", "missing": lacking, "title": "add claude beside the lead",
         "command": "sandbox-ai codex --cairn --with claude", "start": "sandbox-ai codex --cairn --with claude",
         "duration": "", "unsigned": False, "security": "claude runs in the same sandbox", "terminal": False,
         "needed": True, "verdict": "offered", "why": "", "choice": "cairn-workers-claude"},
        {"id": "option:cairn-workers-codex", "kind": "option", "missing": lacking, "title": "let codex be the worker",
         "command": "sandbox-ai codex --cairn --cairn-workers codex", "start": "sandbox-ai codex --cairn --cairn-workers codex",
         "duration": "", "unsigned": False, "security": "", "terminal": False, "needed": True, "verdict": "offered",
         "why": "", "choice": "cairn-workers-claude"}]}
    result = run_driver(tmp_path, CHOICE_PAGE.replace("__PLAN__", json.dumps(plan)), "fix-choice")
    assert "one way: add claude beside the lead; security: claude runs in the same sandbox — sandbox-ai codex --cairn --with claude" in result["lines"][1]
    assert result["buttons"] == ["Do the ticked ones and start", "Cancel"]  # (a choice: the ticks at once)
    assert result["before"] == [True, False, False]  # (what the start needs is ticked; no way is chosen for you)
    assert result["after"] == [True, False, True]  # (one way of the two)
    assert result["chosen"] == ["image:localhost/claude-sandbox:latest", "option:cairn-workers-codex"]


LICENSE_PAGE = FAKE_DOM + r"""
const PLAN = __PLAN__;
(async () => {
  const result = {};
  vm.runInContext('state.me = { user: "admin", csrf: "c", terminals: true }; state.host = "ct1"; state.hosts = [{ name: "ct1", kind: "ssh" }];', context);
  answer = (method, path) => (path === "/api/hosts/ct1/fix-plan" ? PLAN : method === "GET" ? [] : { output: "" });
  const asked = vm.runInContext('askFixPlan(__main, "ct1", { agent: "codex", name: "", workspace: "/w", options: "[]" })', context);
  await settle();
  result.lines = main.all().filter(n => n.tagName === "li").map(n => n.textContent);
  result.buttons = main.all().filter(n => n.tagName === "button" && !n.hidden).map(n => n.textContent);
  const boxes = main.all().filter(n => n.tagName === "input");
  result.before = boxes.map(b => Boolean(b.checked));
  if (__TICK__) { boxes[1].checked = true; boxes[1].fire("change"); }
  button(main, "Do the ticked ones and start").fire("click");
  result.chosen = await asked;
  console.log(JSON.stringify(result));
})().catch(e => { console.error(e.stack); process.exit(1); });
"""


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_a_fix_that_accepts_a_license_is_a_tick_of_yours_never_fix_all(tmp_path):
    """The toolchain android-sdk (2026-11-24): built on the host from Google's downloads, under their license. Its fix
    comes as one answer of a question of its own (fixes.py "choice", "terms"): the page shows the ticks at once (no
    "Fix all and start"), says the license in its line, and leaves it unticked - only your tick hands it to the
    start's terminal (--fix-only)."""
    license_fix = {"id": "toolchain:android-sdk", "kind": "toolchain",
                   "missing": "the toolchain android-sdk is not in the store",
                   "title": "accept the Android Software Development Kit License Agreement and build it here from "
                            "Google's downloads",
                   "command": "sandbox-ai toolchain add android-sdk --accept-android-sdk-license", "start": "",
                   "duration": "a download of about 1 GB", "unsigned": False,
                   "security": "by this you accept the Android Software Development Kit License Agreement "
                               "(https://developer.android.com/studio/terms)", "terminal": False, "needed": True,
                   "verdict": "offered", "why": "", "choice": "license:toolchain:android-sdk"}
    plan = {"questions": "all", "mode": "medium", "fixes": [PLAN["fixes"][0], license_fix]}
    page = LICENSE_PAGE.replace("__PLAN__", json.dumps(plan))
    result = run_driver(tmp_path, page.replace("__TICK__", "false"), "fix-license")
    assert "https://developer.android.com/studio/terms" in result["lines"][1]
    assert "--accept-android-sdk-license" in result["lines"][1]
    assert result["buttons"] == ["Do the ticked ones and start", "Cancel"], "no Fix all: a license is yours"
    assert result["before"] == [True, False]
    assert result["chosen"] == ["image:localhost/claude-sandbox:latest"]
    ticked = run_driver(tmp_path, page.replace("__TICK__", "true"), "fix-license-ticked")
    assert ticked["chosen"] == ["image:localhost/claude-sandbox:latest", "toolchain:android-sdk"]
    # The service hands the launcher's "choice" on (what makes it a tick of yours) and takes the id for --fix-only.
    assert w.FIX_ID.fullmatch("toolchain:android-sdk") and w.FIX_ID.fullmatch("toolchain:android-sdk@36")
    assert w.FIX_ID.fullmatch("service:android-emulator")


UPDATE_PAGE = FAKE_DOM + r"""
(async () => {
  const result = {};
  vm.runInContext('state.me = { user: "admin", csrf: "c", terminals: true }; state.host = "ct1";', context);
  answer = (method, path, sent) => (path === "/api/hosts/ct1/update"
    ? { name: "ct1", before: "0.1.200", after: "0.1.218", changed: true, notes: [] } : { __status: 404 });
  const box = vm.runInContext('errorNotice(new ApiError(409, "sandbox-ai 0.1.200 on ct1 does not know the fix plan", null, "host-too-old", { host: "ct1", version: "0.1.200", what: "the fix plan of a start" }))', context);
  main.append(box);
  result.buttons = box.all().filter(n => n.tagName === "button").map(n => n.textContent);
  button(box, "Update ct1 now").fire("click"); await settle();
  result.sent = calls.filter(c => c.method === "POST").map(c => [c.path, JSON.parse(c.body)]);
  result.text = box.textContent;
  const local = vm.runInContext('errorNotice(new ApiError(409, "x", null, "host-too-old", { host: "local" }))', context);
  const other = vm.runInContext('errorNotice(new ApiError(409, "x", null, "busy", { host: "ct1" }))', context);
  result.others = [local, other].map(b => b.all().filter(n => n.tagName === "button").length);
  vm.runInContext('setLanguage("de")', context);
  result.german = vm.runInContext('t("Update {host} now", { host: "ct1" })', context);
  console.log(JSON.stringify(result));
})().catch(e => { console.error(e.stack); process.exit(1); });
"""


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_a_host_too_old_is_offered_its_update_right_in_the_refusal(tmp_path):
    """Round 2: every refusal "too old" (host-too-old: the fix plan, an option the host does not know) carries the
    host's update - sandbox-ai hosts update HOST, its own signed one - and says to do it again then."""
    result = run_driver(tmp_path, UPDATE_PAGE, "update-offer")
    assert result["buttons"] == ["Update ct1 now"]
    assert result["sent"] == [["/api/hosts/ct1/update", {}]]
    assert "ct1: 0.1.200 → 0.1.218" in result["text"] and "Then do it again: the host knows it now." in result["text"]
    assert result["others"] == [0, 0]  # (this machine updates itself; another refusal has no update to offer)
    assert result["german"] == "ct1 jetzt aktualisieren"
