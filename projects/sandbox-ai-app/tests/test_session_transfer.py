"""Copy to host… and Move to host… of the Sandboxes page offscreen: a session of this machine to a host of sandbox-ai
hosts (`sandbox-ai session copy|move NAME HOST [--as NAME] [--map=LOCAL=REMOTE]... [--check]`). The dialog offers the
hosts without this machine, the name there and every folder of the session (only a changed one becomes --map), Check
only, Copy and Move (asked first: session-move); its output is shown. Against stand-ins that record what they were
asked: nothing reaches a host, nothing starts."""
import json
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, i18n, widgets  # noqa: E402

from test_accessibility import unnamed  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")
HOSTS = [{"name": "local", "kind": "local"}, {"name": "ct1", "kind": "ssh", "ssh": "admin@192.0.2.10"},
         {"name": "ct2", "kind": "ssh", "ssh": "admin@192.0.2.11"}]


def at_once(function, *args, done, **kw):
    """widgets.background, but on this thread: DONE gets the result or the error at once."""
    try:
        result = function(*args)
    except Exception as exc:  # noqa: BLE001 - as background hands it on
        done(None, exc)
        return
    done(result, None)


@pytest.fixture
def sai(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    sai = backend.SandboxAI()
    sai.calls, sai.answer = [], (0, "", "")

    def call(*args, timeout=120, input=None, argv=None):
        sai.calls.append(args)
        return sai.answer
    monkeypatch.setattr(sai, "call", call)
    monkeypatch.setattr(sai, "hosts_supported", lambda: True)
    monkeypatch.setattr(sai, "host_entries", lambda: [dict(h) for h in HOSTS])
    return sai


@pytest.fixture
def ui(qapp, sai, monkeypatch):
    from sandbox_ai_app import session_transfer, ui as module
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    asked = []

    def ask_first(parent, action, title, text, accept, detail=None, details=None):
        if backend.needs_confirm(action, detail) is None:
            return True
        asked.append((action, text, accept))
        return module.answer_yes
    for target in (module, session_transfer):
        monkeypatch.setattr(target, "ask_first", ask_first)
        monkeypatch.setattr(target, "background", at_once)
    module.asked, module.answer_yes = asked, True
    return module


def make_session(tmp_path, name="web", options=None):
    directory = tmp_path / "state" / "sandbox-ai" / "sessions" / name
    (directory / "home").mkdir(parents=True)
    data = {"agent": "codex", "options": options or ["--workspace", "/srv/w", "--mount", "/srv/data:/mnt/data:ro",
                                                     "--backend", "podman", "--allow-host", "pypi.org"],
            "agent_args": [], "backend": "podman", "workspace": "/srv/w", "cwd": str(tmp_path),
            "created": "2026-11-01T00:00:00Z", "pod": f"agent-session-{name}"}
    (directory / "session.json").write_text(json.dumps(data))


def session(name="web", state="stopped", host="local"):
    if host != "local":
        return backend.Instance("session", f"remote:{host}:{name}", name, host=host, state=state, agent="codex")
    return backend.Instance("session", f"session:{name}", name, session=name, state=state, agent="codex")


def page_on(ui, sai, monkeypatch, instance):
    page = ui.SandboxesPage(sai, lambda *a, **k: None)
    monkeypatch.setattr(page, "current", lambda: instance)
    return page


def dialog_of(ui, sai, monkeypatch, move=False, paths=("/srv/w", "/srv/data"), problems=()):
    from sandbox_ai_app.session_transfer import SessionTransferDialog
    monkeypatch.setattr(sai, "session_transfer_plan", lambda name: (list(paths), list(problems)))
    succeeded = []
    dialog = SessionTransferDialog(None, sai, "web", move=move, succeeded=succeeded.append)
    return dialog, succeeded


# -- the command line it runs --


def test_the_words_of_session_copy_and_move():
    args = backend.session_transfer_args("copy", "web", "ct1", "web", [("/srv", "/srv"), ("/home/me/src/", "/opt/src")])
    assert args == ["session", "copy", "web", "ct1", "--map=/home/me/src=/opt/src"], "only a folder that moves"
    assert backend.session_transfer_args("move", "web", "ct1", "web-2", [], check=True) == [
        "session", "move", "web", "ct1", "--as", "web-2", "--check"]
    for bad in (("copy", "web", "local", None, []), ("copy", "-web", "ct1", None, []), ("copy", "web", "-x", None, []),
                ("delete", "web", "ct1", None, []), ("copy", "web", "ct1", "../x", []),
                ("copy", "web", "ct1", None, [("relative", "/x")]), ("copy", "web", "ct1", None, [("/a=b", "/x")]),
                ("copy", "web", "ct1", None, [("/a", "/x/../y")])):
        with pytest.raises(ValueError):
            backend.session_transfer_args(*bad)


def test_session_copy_and_move_call_sandbox_ai(sai):
    sai.session_copy("web", "ct1", None, [("/srv", "/data/srv")], True)
    sai.session_move("web", "ct2", "web-2")
    assert sai.calls == [("session", "copy", "web", "ct1", "--map=/srv=/data/srv", "--check"),
                         ("session", "move", "web", "ct2", "--as", "web-2")]


def test_the_folders_of_a_session_read_from_its_plan(sai, monkeypatch):
    """`sandbox-ai session plan NAME --json` (a process of its own: no change of this process's directory)."""
    answers = {"web": (0, json.dumps({"agent": "codex", "options": [], "agent_args": [],
                                      "paths": ["/srv/w", "/srv/data"], "problems": ["no rules"]}), ""),
               "gone": (1, "", "sandbox-ai session: No session named gone\n"), "odd": (0, "[]", "")}
    monkeypatch.setattr(sai, "call", lambda *args, timeout=None: answers[args[2]])
    assert sai.session_transfer_plan("web") == (["/srv/w", "/srv/data"], ["no rules"])
    with pytest.raises(ValueError, match="No session named gone"):
        sai.session_transfer_plan("gone")
    with pytest.raises(ValueError, match="not a plan"):
        sai.session_transfer_plan("odd")
    with pytest.raises(ValueError):
        sai.session_transfer_plan("-x")


# -- the Sandboxes page's buttons --


def test_copy_and_move_only_for_a_session_of_this_machine(ui, sai, monkeypatch):
    for instance, why in ((session(), None), (session(state="running"), None),
                          (session(host="ct1"), "A session of ct1"),
                          (backend.Instance("run", "run:x", "x", run="codex-1", state="running"), "Only for sessions"),
                          (None, "Select a row")):
        page = page_on(ui, sai, monkeypatch, instance)
        page.update_buttons()
        for button in (page.copy_to_host_button, page.move_to_host_button):
            if why is None:
                assert button.isEnabled(), button.toolTip()
            else:
                assert not button.isEnabled() and why in button.toolTip(), (why, button.toolTip())
        page.close()


def test_the_page_opens_the_dialog_and_reads_again_after_a_copy(ui, sai, monkeypatch):
    page = page_on(ui, sai, monkeypatch, session())
    page.update_buttons()
    monkeypatch.setattr(sai, "session_transfer_plan", lambda name: (["/srv/w"], []))
    refreshed, opened = [], []
    monkeypatch.setattr(page, "refresh_remote", lambda hosts=None, polled=False: refreshed.append(hosts))

    def run(dialog):
        opened.append((dialog.name, dialog.move, dialog.windowTitle()))
        dialog.run()
        return 0
    from sandbox_ai_app import session_transfer
    monkeypatch.setattr(session_transfer.SessionTransferDialog, "exec", run)
    page.move_to_host_button.click()
    assert opened == [("web", True, "Move session web to a host")]
    assert ui.asked[0][0] == "session-move" and "then stop it here" in ui.asked[0][1]
    assert sai.calls == [("session", "move", "web", "ct1")]
    assert refreshed == [["ct1"]]
    # Without session copy in this sandbox-ai: said, no dialog.
    monkeypatch.setattr(sai, "session_transfer_supported", lambda: False)
    page.copy_to_host_button.click()
    assert len(opened) == 1 and any("cannot copy a session" in text for _, text, _ in widgets.SHOWN)
    page.close()


# -- the dialog --


def test_the_dialog_offers_the_hosts_the_folders_and_runs_copy_with_the_changed_ones(ui, sai, monkeypatch):
    dialog, succeeded = dialog_of(ui, sai, monkeypatch)
    hosts = [dialog.host.box.itemData(i) for i in range(dialog.host.box.count())]
    assert hosts == ["ct1", "ct2"], "this machine is not offered"
    assert list(dialog.edits) == ["/srv/w", "/srv/data"]
    assert [edit.text() for edit in dialog.edits.values()] == ["/srv/w", "/srv/data"], "each the same path there"
    assert dialog.go_button.text() == "Copy" and dialog.output.isHidden()
    assert unnamed(dialog) == []
    dialog.host.set_host("ct2")
    dialog.edits["/srv/data"].setText("/data")
    dialog.new_name.setText("web-2")
    sai.answer = (0, "sandbox-ai session copy: web can start on ct2 (checked, not started)\n", "")
    dialog.check_button.click()
    assert sai.calls[-1] == ("session", "copy", "web", "ct2", "--as", "web-2", "--map=/srv/data=/data", "--check")
    assert "can start on ct2" in dialog.output.toPlainText() and not succeeded
    assert ui.asked == [], "a copy and a check are not asked"
    sai.answer = (0, "sandbox-ai session copy: web runs on ct2 as web-2 (attach: …)\n", "")
    dialog.go_button.click()
    assert sai.calls[-1] == ("session", "copy", "web", "ct2", "--as", "web-2", "--map=/srv/data=/data")
    assert succeeded == ["ct2"] and "Done: it runs on ct2." in dialog.status.text()
    assert not dialog.go_button.isEnabled() and dialog.check_button.isEnabled()
    dialog.host.set_host("ct1")
    assert dialog.go_button.isEnabled(), "a copy may go to another host too"
    dialog.close()


def test_a_failed_dry_run_and_the_problems_are_shown(ui, sai, monkeypatch):
    dialog, succeeded = dialog_of(ui, sai, monkeypatch, problems=["the project defines its own MCP servers (x)"])
    assert "Cannot go along: the project defines its own MCP servers (x)" in dialog.status.text()
    sai.answer = (1, "sandbox-ai session copy: web cannot start on ct1 as it is:\n  workspace /srv/w does not "
                     "exist\n  (folders of this machine that lie elsewhere there: --map LOCAL=REMOTE)\n", "")
    dialog.go_button.click()
    assert "does not exist" in dialog.output.toPlainText() and not succeeded
    assert "It failed (exit 1)" in dialog.status.text() and dialog.go_button.isEnabled()
    # A folder there that is not an absolute path: refused before anything runs.
    calls = len(sai.calls)
    dialog.edits["/srv/w"].setText("srv/w")
    dialog.go_button.click()
    assert len(sai.calls) == calls and "Not started" in dialog.output.toPlainText()
    dialog.close()


def test_move_is_asked_first_and_nothing_runs_when_declined(ui, sai, monkeypatch):
    dialog, succeeded = dialog_of(ui, sai, monkeypatch, move=True)
    assert dialog.go_button.text() == "Move"
    ui.answer_yes = False
    dialog.go_button.click()
    assert [action for action, *_ in ui.asked] == ["session-move"] and sai.calls == [] and not succeeded
    dialog.check_button.click()  # a check stops nothing: not asked
    assert sai.calls == [("session", "move", "web", "ct1", "--check")] and len(ui.asked) == 1
    ui.answer_yes = True
    dialog.go_button.click()
    assert sai.calls[-1] == ("session", "move", "web", "ct1") and succeeded == ["ct1"]
    dialog.close()


def test_without_a_host_nothing_can_run(ui, sai, monkeypatch):
    monkeypatch.setattr(sai, "host_entries", lambda: [{"name": "local", "kind": "local"}])
    dialog, _ = dialog_of(ui, sai, monkeypatch, paths=())
    assert dialog.host.host() is None and not dialog.no_host.isHidden()
    assert not dialog.go_button.isEnabled() and "Choose a host first" in dialog.go_button.toolTip()
    assert not dialog.check_button.isEnabled()
    assert "No folder or file" in dialog.status.text()
    dialog.close()


@pytest.mark.parametrize("language", ["en", "de"])
def test_the_dialog_in_both_languages(ui, sai, monkeypatch, language):
    i18n.set_language(language)
    dialog, _ = dialog_of(ui, sai, monkeypatch, move=True)
    assert unnamed(dialog) == []
    if language == "de":
        assert dialog.windowTitle() == "Sitzung web auf einen Host verschieben"
        assert dialog.go_button.text() == "Verschieben"
    dialog.close()
