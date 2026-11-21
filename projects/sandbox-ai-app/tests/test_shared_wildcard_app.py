"""Wildcards below a shared service (*.blob.core.windows.net: anyone can rent a name there) in the app (user
2026-11-20): asked first (ask_first "shared-wildcard"), naming the risk and the shared suffix, then passed as
--allow-shared-wildcard - in the network dialog of a running sandbox and at a Board start; the Board's network part
has the option, marks what it reaches as bad; the app never passes --yes."""
import os
import types

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, board as B, labels, list_fields as LF, widgets  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")
WILD = "*.blob.core.windows.net"


@pytest.fixture(scope="module")
def sai():
    return backend.SandboxAI()


def test_the_risk_is_read_from_sandbox_ai(sai):
    found = sai.shared_wildcards([WILD, "*.example.org", "pypi.org", "*.co.uk"])
    assert list(found) == [WILD] and found[WILD][1] == "blob.core.windows.net"
    assert "could open any server anyone rents below blob.core.windows.net" in found[WILD][0]
    assert "shared-wildcard" in backend.NEEDS_CONFIRM and backend.needs_confirm("shared-wildcard")


def test_the_option_is_on_the_boards_network_part_and_yes_never(sai):
    option = next(o for o in sai.options() if o.long == "--allow-shared-wildcard")
    assert option.kind == "list" and option.short == "-sw"
    assert "--allow-shared-wildcard" in backend.GROUPS["Network"] and "--allow-shared-wildcard" in \
        B.KINDS["network"].options and "--allow-shared-wildcard" in backend.LIST_OPTIONS
    assert "--yes" in backend.HIDDEN and "--yes" not in {o.long for o in sai.options()}
    assert labels.OPTION_LABELS["--allow-shared-wildcard"][1].startswith("Wildcards unter")
    assert LF.OPTION_CHECKS["--allow-shared-wildcard"](WILD) is None
    assert LF.OPTION_CHECKS["--allow-shared-wildcard"]("pypi.org") is not None
    board = B.from_argv(sai, "claude", ["-sw", WILD, "-A", "x.example"])
    assert board.first("network").props == {"--allow-shared-wildcard": [WILD], "--allow-host": ["x.example"]}
    argv = B.to_argv(sai, board)
    assert argv[argv.index("--allow-shared-wildcard") + 1] == WILD
    _, badges = B.badges(board)
    assert any(b.level == "bad" and "below a shared service" in b.text for b in badges), [b.text for b in badges]


def run_dialog(monkeypatch, typed, answers, supported=frozenset({"--allow-host", "--allow-shared-wildcard"})):
    """The network dialog of a running sandbox (SandboxesPage.network_dialog) with TYPED as its arguments and the
    questions answered by ANSWERS: (what ran, what was asked)."""
    from sandbox_ai_app import ui
    ran, asked = [], []

    class Recorder(backend.SandboxAI):
        def host_run(self, host, *args, timeout=120, input=None):
            ran.append((host, *args))
            return 0, "ok", ""

        def presets(self):
            return {}

    class Dialog:
        def __init__(self, *a, **k):
            pass

        def exec(self):
            return QtWidgets.QDialog.DialogCode.Accepted

        def arguments(self):
            return list(typed)

        def kept(self):
            return False
    monkeypatch.setattr(ui, "NetworkDialog", Dialog)
    monkeypatch.setattr(widgets, "exec_dropped", lambda dialog, *a: dialog.exec())
    page = types.SimpleNamespace(sai=Recorder(), run_buttons=[], update_buttons=lambda: None,
                                 reported=lambda title, result, error, draft=None: None)
    answers = iter(answers)
    monkeypatch.setattr(ui, "ask_first", lambda parent, action, title, text, accept, **kw: asked.append(
        (action, text, accept)) or next(answers))
    monkeypatch.setattr(ui, "background", lambda function, *args, done=None, **kw: done(function(*args), None))
    ui.SandboxesPage.network_dialog(page, "local", "web", "allow", "Allow for web", set(supported), None)
    return ran, asked


def test_the_network_dialog_asks_by_itself_and_passes_the_confirmation(qapp, monkeypatch):
    typed = ["--allow-host", WILD, "--allow-host", "*.example.org", "--dns"]
    ran, asked = run_dialog(monkeypatch, typed, [True, True])
    assert [a[0] for a in asked] == ["net-allow", "shared-wildcard"]
    assert "blob.core.windows.net" in asked[1][1] and "send data there" in asked[1][1] and asked[1][2] == "Allow anyway"
    assert ran == [("local", "net", "allow", "web", "--allow-shared-wildcard", WILD, "--allow-host", "*.example.org",
                    "--dns")]
    # No to the risk: nothing is sent.
    ran, asked = run_dialog(monkeypatch, typed, [True, False])
    assert ran == [] and [a[0] for a in asked] == ["net-allow", "shared-wildcard"]
    # An own domain's wildcard: no second question.
    ran, asked = run_dialog(monkeypatch, ["--allow-host", "*.example.org"], [True])
    assert [a[0] for a in asked] == ["net-allow"] and ran[0][-2:] == ("--allow-host", "*.example.org")
    # An older sandbox-ai there (no --allow-shared-wildcard): passed as typed, it refuses it itself.
    ran, asked = run_dialog(monkeypatch, ["--allow-host", WILD], [True], supported={"--allow-host"})
    assert [a[0] for a in asked] == ["net-allow"] and ran[0][-2:] == ("--allow-host", WILD)


def test_a_board_start_asks_first_and_again_after_a_change(qapp, monkeypatch, sai):
    from sandbox_ai_app import board_view
    asked, started = [], []
    monkeypatch.setattr(board_view, "ask_first", lambda parent, action, title, text, accept, **kw: asked.append(
        (action, text, accept)) or answers.pop(0))
    monkeypatch.setattr(board_view, "background", lambda function, *args, done=None, **kw: done(function(*args), None))
    view = types.SimpleNamespace(sai=sai, board=B.from_argv(sai, "claude", ["-sw", WILD]), host=lambda: "local")
    view.shared_unasked = types.MethodType(board_view.BoardPage.shared_unasked, view)
    answers = [False]
    assert view.shared_unasked(lambda: started.append(1)) is True and started == []
    assert asked[0][0] == "shared-wildcard" and "blob.core.windows.net" in asked[0][1]
    answers = [True]
    assert view.shared_unasked(lambda: started.append(1)) is True and started == [1]
    assert view.shared_unasked(lambda: started.append(2)) is False  # asked for this very board: goes on
    view.board = B.from_argv(sai, "claude", ["-sw", WILD, "-A", "x.example"])  # changed: asked again
    answers = [True]
    assert view.shared_unasked(lambda: started.append(3)) is True and started == [1, 3] and len(asked) == 3
    plain = types.SimpleNamespace(sai=sai, board=B.from_argv(sai, "claude", ["-A", "*.example.org"]),
                                  host=lambda: "local")
    assert board_view.BoardPage.shared_unasked(plain, lambda: None) is False  # nothing to ask


from test_board_view import page  # noqa: E402,F401 - the fixture: a real BoardPage


def test_e2e_a_board_start_asks_and_starts_with_the_confirmation(page, tmp_path, wait):
    page.directory = str(tmp_path)
    page.add_node("workspace", path=str(tmp_path))
    page.board.first("network").props["--allow-shared-wildcard"] = [WILD]
    assert page.start(embedded=True) is False, "it waits for the question"
    assert wait(lambda: page.started, 15)
    argv = page.started[-1][0]
    assert argv[argv.index("--allow-shared-wildcard") + 1] == WILD and "--yes" not in argv
    question = [text for title, text, _ in widgets.SHOWN if "below a shared service" in text]
    assert question and "blob.core.windows.net" in question[0] and "send data there" in question[0]


def test_allowed_now_marks_it_as_the_risk():
    from sandbox_ai_app import approvals_sections
    rows = approvals_sections.allowed_rows({"host_wildcards": [WILD, "*.example.org"],
                                            "shared_wildcards": {WILD: "blob.core.windows.net"}})
    kinds = {row["destination"]: row["kind"] for row in rows}
    assert "below the shared service blob.core.windows.net" in kinds[WILD] and kinds["*.example.org"] == "host wildcard"
