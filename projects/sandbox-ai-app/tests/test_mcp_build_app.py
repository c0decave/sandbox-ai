"""A start whose MCP server its catalog lists without a signed image yet asks to build it (user report 2026-11-21, the
app in German, a start on the host coding-101 with the preset android-debugging: "MCP-Server jadx: kein Katalog von
coding-101 hat ihn ... warum werden die nicht automatisch gebaut oder gefragt ob sie direkt gebaut werden sollen???").

The Board on another host (lab, test_remote_mcp_folder's fixtures: its sandbox-ai answered by a stand-in for
`sandbox-ai` there) and on this machine: what would be built is read there first (`mcp add ... --build-plan`), the
question names the commit, the source archive's hash and that it is added unsigned; a yes builds exactly that
(`--build --commit --source-sha256 --yes`) and the start goes on, a no or a failed build stops it, said. A server in
no catalog keeps today's words; one with a signed image is added as before."""
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

COMMIT = "1" * 40
ARCHIVE = "a" * 64
JADX = {"prefix": "mcp", "name": "jadx", "mounts": [{"target": "/work", "required": True}], "published": False,
        "signature": "good"}


def plan(entry="mcp:jadx"):
    return {"entry": entry, "name": entry.split(":")[-1], "title": "jadx (Android/Java decompiler, headless)",
            "repository": "https://github.com/c0decave/sandbox-ai-mcp", "commit": COMMIT,
            "path": "servers/" + entry.split(":")[-1], "archive_sha256": ARCHIVE,
            "catalog_label": "sandbox-ai-mcp (github.com/c0decave)", "signature": "good", "image": "x",
            "question": "..."}


@pytest.fixture
def building(lab, monkeypatch):
    """lab's `mcp add ... --build-plan` answers the plan (nothing added), `mcp add ... --build` builds and adds
    (lab.build_fails: it fails as a build does, its log's end said)."""
    real = lab.host_run
    lab.build_fails = False

    def run(name, *args, **kw):
        if args[:2] == ("mcp", "add") and "--build-plan" in args:
            lab.ran.append((name, *args))
            return 0, json.dumps(plan(args[2] if ":" in args[2] else "mcp:" + args[2])), ""
        if args[:2] == ("mcp", "add") and "--build" in args and lab.build_fails:
            lab.ran.append((name, *args))
            return 1, "  | E: Failed to fetch (fake)\n", ("sandbox-ai mcp: the log's end:\nE: Failed to fetch\n"
                                                         "sandbox-ai mcp: jadx is not added: its build failed (exit "
                                                         "status 1); the whole log: /x/jadx.log")
        return real(name, *args, **kw)
    monkeypatch.setattr(lab, "host_run", run)
    return lab


def questions(title="Build MCP server jadx?"):
    return [text for shown, text, _ in widgets.SHOWN if shown == title]


def test_a_start_on_a_host_asks_builds_there_and_goes_on(on_lab, building, monkeypatch):
    board = on_lab(["jadx"], extra_catalog=[JADX])
    answer(building, monkeypatch)
    widgets.SHOWN.clear()
    assert board.start() is False, "not at once: the server is built first"
    project = on_lab.project
    (text,) = questions()
    assert COMMIT in text and ARCHIVE in text and "UNSIGNED" in text and "lab" in text
    assert "https://github.com/c0decave/sandbox-ai-mcp" in text and "servers/jadx" in text and "signed: the entry" in text
    plans = [args for args in building.ran if "--build-plan" in args]
    assert plans == [("lab", "mcp", "add", "jadx", "--mount", f"/work={project}", "--build-plan")]
    assert added(building)[-1] == ("lab", "mcp", "add", "jadx", "--mount", f"/work={project}", "--build", "--commit",
                                   COMMIT, "--source-sha256", ARCHIVE, "--yes")
    assert len(board.started) == 1, (board.started, widgets.SHOWN)
    assert not [text for title, text, _ in widgets.SHOWN if title == "Not started"], widgets.SHOWN


def test_declined_nothing_is_built_and_the_start_stops_said(on_lab, building, monkeypatch):
    board = on_lab(["jadx"], extra_catalog=[JADX])
    answer(building, monkeypatch)
    real, kinds = widgets.confirm, []  # (asked through widgets.ask_first: the app's policy, mcp-build of the table)
    monkeypatch.setattr(widgets, "confirm", lambda parent, title, *a, **k: kinds.append(k.get("destructive")) or False
                        if title.startswith("Build MCP") else real(parent, title, *a, **k))
    board.start()
    assert board.started == [] and not [args for args in added(building) if "--build" in args]
    assert kinds == [True], "asked as what weakens something: Cancel is the default"
    said = "\n".join(text for title, text, _ in widgets.SHOWN if title == "Not started")
    assert "MCP server jadx: not built - its catalog has no signed image of it yet (sandbox-ai mcp add jadx --build" \
        in said, widgets.SHOWN


def test_a_failed_build_is_said_and_the_start_does_not_run(on_lab, building, monkeypatch):
    board = on_lab(["jadx"], extra_catalog=[JADX])
    answer(building, monkeypatch)
    building.build_fails = True
    board.start()
    assert board.started == []
    said = "\n".join(text for title, text, _ in widgets.SHOWN if title == "Preparing the start")
    assert "jadx" in said and "its build failed" in said, widgets.SHOWN


def test_an_unsigned_catalog_is_said_and_passed_on(on_lab, building, monkeypatch):
    board = on_lab(["jadx"], extra_catalog=[{**JADX, "signature": "none"}])
    answer(building, monkeypatch)
    board.start()
    (text,) = questions()
    assert "NOT signed: nobody vouched for the entry" in text
    assert [args for args in building.ran if "--build-plan" in args][0][-2:] == ("--build-plan", "--unsigned")
    assert added(building)[-1][-2:] == ("--yes", "--unsigned")


def test_in_no_catalog_and_signed_ones_are_as_before(on_lab, building, monkeypatch):
    board = on_lab(["nosuch", "playwright"], extra_catalog=[JADX])
    answer(building, monkeypatch)
    board.start()
    assert questions() == [] and not [args for args in building.ran if "--build-plan" in args]
    assert added(building) == [("lab", "mcp", "add", "playwright")]
    said = "\n".join(text for title, text, _ in widgets.SHOWN if title == "Not started")
    assert "MCP server nosuch: no catalog of lab has it" in said


def test_the_helpers():
    entries = [("mcp", {"name": "jadx", "_signature": {"state": "good"}}),
               ("mcp", {"name": "time", "digest": "sha256:" + "a" * 64, "_signature": {"state": "good"}}),
               ("bad", {"name": "evil", "_signature": {"state": "bad"}})]
    assert prerequisites.unpublished_entry(entries, "jadx")["name"] == "jadx"
    assert prerequisites.unpublished_entry(entries, "time") is None  # (published: pulled, signed)
    assert prerequisites.unpublished_entry(entries, "evil") is None  # (a signature that does not verify: never)
    assert prerequisites.unpublished_entry([{"prefix": "mcp", "name": "x", "published": True}], "x") is None
    with pytest.raises(ValueError, match="not a build plan"):
        prerequisites.checked_plan(json.dumps({**plan(), "commit": "main"}))
    shown = prerequisites.checked_plan(json.dumps({**plan(), "repository": "https://x\n\x1b[31mforged"}))
    assert "\n" not in shown["repository"] and "\x1b" not in shown["repository"]
    assert backend.origin_text({"catalog": "sandbox-ai-mcp", "signed": False, "built": {"commit": COMMIT}}) == \
        "sandbox-ai-mcp, built here, not signed"


def test_on_this_machine_the_plan_and_the_build_run_here(qapp, sai, monkeypatch):
    """The Board of this machine: what would be built is read from this machine's sandbox-ai (host_run "local"),
    and the build is its `mcp add --build`."""
    from sandbox_ai_app.board_view import BoardPage
    view = BoardPage(sai, lambda *a, **k: None, lambda: {"board_grid": 20})
    ran = []

    def run(name, *args, **kw):
        ran.append((name, *args))
        return 0, json.dumps(plan()), ""
    monkeypatch.setattr(sai, "host_run", run)
    monkeypatch.setattr(prerequisites, "background", at_once)
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    widgets.SHOWN.clear()
    got = []
    view.prerequisite_context = lambda: "ctx"
    view.ask_mcp_builds("local", ["jadx"], [], [("mcp", {"name": "jadx", "_signature": {"state": "good"}})],
                        {"jadx": ["/work=/srv/apps"]}, set(), "ctx", "start", lambda builds, refused:
                        got.append((builds, refused)))
    assert ran == [("local", "mcp", "add", "jadx", "--mount", "/work=/srv/apps", "--build-plan")]
    assert got == [({"jadx": ("--build", "--commit", COMMIT, "--source-sha256", ARCHIVE, "--yes")}, {})]
    assert "Build it on this machine from its reviewed source" in questions()[0]
    view.close()
    view.deleteLater()
