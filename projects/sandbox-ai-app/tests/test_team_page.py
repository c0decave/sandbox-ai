"""The Teams page (offscreen): the teams of `sandbox-ai team` on this machine or a host, a team's members and board,
start and the lead in a terminal tab there, post, and stop and delete asked first. Once against the real `sandbox-ai
team list` and `team show --json` of the checkout in SANDBOX_AI_HOME (a team record written into a temporary state;
nothing starts)."""
import json
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, team_page, widgets  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")
SHOW = {"name": "review", "members": ["claude", "codex"], "lead": "claude", "workspace": "/srv/work",
        "options": [], "states": {"claude": "running", "codex": "paused"},
        "board": {"next": 3, "tasks": [
            {"id": 1, "from": "claude", "to": "codex", "state": "done", "task": "write the tests\x1b[2J"},
            {"id": 2, "from": "human", "to": "codex", "state": "open", "task": "and the docs"}]}}


def synchronous(function, *args, done, **kw):
    try:
        result = function(*args)
    except Exception as exc:  # noqa: BLE001
        done(None, exc)
        return
    done(result, None)


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache"), ("HOME", "home")):
        monkeypatch.setenv(name, str(tmp_path / sub))
        (tmp_path / sub).mkdir()
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    monkeypatch.setattr(team_page, "background", synchronous)
    return tmp_path


@pytest.fixture
def sai(env, monkeypatch):
    sai = backend.SandboxAI()
    sai.asked, sai.answers = [], {}
    monkeypatch.setattr(sai, "hosts_supported", lambda: True)
    monkeypatch.setattr(sai, "host_entries", lambda: [{"name": "local", "kind": "local"},
                                                      {"name": "ct1", "kind": "ssh", "ssh": "admin@192.0.2.10"}])

    def host_run(name, *args, timeout=120, input=None):
        sai.asked.append((name, args))
        return sai.answers.get((name, args), (0, "", ""))
    monkeypatch.setattr(sai, "host_run", host_run)
    monkeypatch.setattr(sai, "host_terminal", lambda name, *args: ["ssh", "-tt", name, "sandbox-ai", *args])
    for host in ("local", "ct1"):
        sai.answers[(host, ("team", "list"))] = (0, "review               claude, codex            /srv/work\n", "")
        sai.answers[(host, ("team", "show", "--json", "--", "review"))] = (0, json.dumps(SHOW), "")
    return sai


def cells(widget, column=0):
    return [widget.item(r, column).text() for r in range(widget.rowCount())]


@pytest.mark.parametrize("host", ["local", "ct1"])
def test_a_team_its_board_and_every_action(qapp, sai, monkeypatch, host):
    from PySide6.QtWidgets import QDialog
    opened = []
    page = team_page.TeamPage(sai, lambda argv, title, **kw: opened.append((argv, title)))
    assert sai.asked == [], "a page nobody opened asks nothing"
    page.hosts.box.setCurrentIndex(page.hosts.box.findData(host))
    page.refresh()
    assert sai.asked[-1] == (host, ("team", "list")) and cells(page.table) == ["review"]
    page.table.selectRow(0)
    assert sai.asked[-1] == (host, ("team", "show", "--json", "--", "review"))
    assert cells(page.members) == ["review-claude", "review-codex"] and cells(page.members, 1) == ["leads", "member"]
    assert cells(page.board, 4) == ["write the tests\\x1b[2J", "and the docs"], "a member's text is shown printable"
    assert cells(page.board, 3) == ["done", "open"]
    page.attach()
    remote = host != "local"
    assert opened[-1][0][-2:] == ["attach", "review-claude"] and (opened[-1][0][2] == "ct1") == remote

    def post(dialog):
        dialog.agent.setCurrentIndex(dialog.agent.findData("codex"))
        dialog.task.setPlainText("-v: run the linter")
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(team_page.PostDialog, "exec", post)
    page.post()
    assert (host, ("team", "post", "--", "review", "codex", "-v: run the linter")) in sai.asked
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)
    page.stop()
    page.delete()
    assert [t for t, _, _ in widgets.SHOWN[-2:]] == ["Stop", "Delete"]
    assert "their HOMEs and the board are removed" in widgets.SHOWN[-1][1]
    assert not [a for h, a in sai.asked if a[:2] in (("team", "stop"), ("team", "delete"))]
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    page.stop()
    assert (host, ("team", "stop", "--", "review")) in sai.asked
    page.delete()
    assert (host, ("team", "delete", "--yes", "--", "review")) in sai.asked

    def start(dialog):
        dialog.name.setText("trio")
        dialog.lead.setCurrentIndex(dialog.lead.findData("codex"))
        dialog.members["claude"].setChecked(True)
        dialog.members["opencode"].setChecked(True)
        dialog.workspace.setText("/srv/other")
        dialog.options.setText("--allow-pypi --kit egm")
        dialog.accept()
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(team_page.StartDialog, "exec", start)
    page.start()
    argv, title = opened[-1]
    # this machine's members take the workspace as given (sandbox-ai 0.1.78); another host's may not know the option
    tail = ["--allow-pypi", "--kit", "egm", *([] if remote else ["--workspaces-as-given"])]
    assert argv[-8 - len(tail):] == ["team", "start", "--agents", "codex,claude,opencode", "--workspace", "/srv/other",
                                     "trio", "--", *tail]
    assert title == "sandbox-ai team start trio" + (" (ct1)" if remote else "")
    page.deleteLater()


def test_the_start_dialog_checks_what_it_is_given(qapp, env):
    dialog = team_page.StartDialog(None, "local", False)
    for name, workspace, options, text in (("Bad Name", "/w", "", "team's name"), ("t", "relative", "", "absolute"),
                                           ("t", "/w", "'unclosed", "cannot be read")):
        dialog.name.setText(name)
        dialog.members["codex"].setChecked(True)
        dialog.workspace.setText(workspace)
        dialog.options.setText(options)
        with pytest.raises(ValueError, match=text):
            dialog.request()
    dialog.name.setText("t")
    dialog.workspace.setText("/w")
    dialog.options.setText("")
    for box in dialog.members.values():
        box.setChecked(False)
    with pytest.raises(ValueError, match="two or three"):
        dialog.request()
    dialog.lead.setCurrentIndex(dialog.lead.findData("codex"))
    assert not dialog.members["codex"].isEnabled(), "the lead is no member besides itself"
    dialog.deleteLater()


def test_errors_and_the_page_in_german(qapp, sai):
    from sandbox_ai_app import i18n
    sai.answers[("ct1", ("team", "list"))] = (255, "", "ssh: connect to host 192.0.2.10: timed out")
    page = team_page.TeamPage(sai, lambda *a, **k: None)
    page.hosts.box.setCurrentIndex(page.hosts.box.findData("ct1"))
    page.refresh()
    assert "timed out" in page.status.text() and page.table.rowCount() == 0
    assert not page.stop_button.isEnabled()
    page.deleteLater()
    i18n.set_language("de")
    try:
        page = team_page.TeamPage(sai, lambda *a, **k: None)
        assert page.start_button.text() == "Team starten…" and page.post_button.text() == "Aufgabe stellen…"
        page.deleteLater()
    finally:
        i18n.set_language("en")


def test_the_real_team_list_and_show(qapp, env):
    """sandbox-ai's own `team list` and `team show --json` of a team record (no member runs: their state is
    gone), read through the backend."""
    sai = backend.SandboxAI()
    record = {"version": 1, "name": "review", "members": ["claude", "codex"], "lead": "claude",
              "workspace": str(env / "work"), "options": [], "created": "2026-11-08T10:00:00Z"}
    folder = env / "state" / "sandbox-ai" / "teams" / "review"
    (folder / "board").mkdir(parents=True)
    (folder / "team.json").write_text(json.dumps(record))
    assert sai.teams("local") == [{"name": "review", "members": ["claude", "codex"], "workspace": str(env / "work")}]
    shown = sai.team_show("local", "review")
    assert shown["lead"] == "claude" and set(shown["states"]) == {"claude", "codex"}
    assert shown["board"]["tasks"] == []
    status, out, err = sai.team_post("local", "review", "codex", "look at the tests")
    assert status == 0, err
    assert [t["task"] for t in sai.team_show("local", "review")["board"]["tasks"]] == ["look at the tests"]
