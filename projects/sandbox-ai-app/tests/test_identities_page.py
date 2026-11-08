"""Identities in the app: the `sandbox-ai identities` steps a change turns into, the dialog's checks, the box on the
Credentials page, and a real identity added, changed and removed through the real command line (names of secrets
only, never a token)."""
import json

import pytest

from sandbox_ai_app import backend

S = backend.SandboxAI


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    return tmp_path


OLD = {"name": "Ada", "email": "ada@example.org", "description": "",
       "git": [{"url": "https://github.com", "user": "x-access-token", "secret": "GH_PUSH"},
               {"url": "http://localhost:3080", "user": "ada", "secret": "GITEA"}],
       "publish": [{"kind": "pypi", "host": "upload.pypi.org", "secret": "PYPI"},
                   {"kind": "npm", "host": "npm.example.org", "secret": "NPM"}]}


def test_a_new_identity_is_added_then_logged_in_and_published():
    steps = S.identity_steps("work", None, {"name": "Ada", "email": "", "description": "",
                                            "git": [{"url": "https://github.com", "user": "", "secret": "GH"}],
                                            "publish": [{"kind": "pypi", "secret": "PYPI"},
                                                        {"kind": "npm", "secret": "NPM", "url": "https://npm.example.org"}]})
    assert steps == [["identities", "add", "--name=Ada", "--", "work"],
                     ["identities", "login", "--secret=GH", "--user=x-access-token", "--", "work", "https://github.com"],
                     ["identities", "publish", "--secret=PYPI", "--", "work", "pypi"],
                     ["identities", "publish", "--secret=NPM", "--url=https://npm.example.org", "--", "work", "npm"]]


def test_an_unchanged_identity_needs_no_step():
    same = {**OLD, "publish": [{"kind": "pypi", "secret": "PYPI"},
                               {"kind": "npm", "secret": "NPM", "url": "https://npm.example.org"}]}
    assert S.identity_steps("work", OLD, same) == []


def test_changes_set_logout_relogin_unpublish_and_reset_a_server():
    new = {"name": "Ada", "email": "ada@example.com", "description": "work",
           "git": [{"url": "https://github.com", "user": "", "secret": "GH_OTHER"}],
           "publish": [{"kind": "npm", "secret": "NPM"}]}
    assert S.identity_steps("work", OLD, new) == [
        ["identities", "set", "--email=ada@example.com", "--description=work", "--", "work"],
        ["identities", "logout", "--", "work", "http://localhost:3080"],
        ["identities", "login", "--secret=GH_OTHER", "--user=x-access-token", "--", "work", "https://github.com"],
        ["identities", "unpublish", "--", "work", "pypi"],
        ["identities", "publish", "--secret=NPM", "--", "work", "npm"]]  # back on registry.npmjs.org


def test_option_looking_values_stay_one_argument():
    steps = S.identity_steps("w", None, {"name": "--help", "git": [], "publish": []})
    assert steps == [["identities", "add", "--name=--help", "--", "w"]]


def test_saving_stops_at_the_first_failed_step(monkeypatch):
    sai = S.__new__(S)
    calls = []

    def call(*args, timeout=120, input=None):
        calls.append(args)
        return (2, "", "no such secret") if args[1] == "login" else (0, "ok", "")
    monkeypatch.setattr(sai, "call", call, raising=False)
    new = {"name": "Ada", "git": [{"url": "https://github.com", "user": "", "secret": "GH"}],
           "publish": [{"kind": "pypi", "secret": "PYPI"}]}
    status, said = sai.save_identity("work", None, new)
    assert status == 2 and "no such secret" in said
    assert [c[1] for c in calls] == ["add", "login"], "nothing after a failure"


def test_the_list_is_empty_when_the_command_fails_or_says_nonsense(monkeypatch):
    sai = S.__new__(S)
    for answer in ((1, "", "boom"), (0, "not json", ""), (0, "[1, 2]", ""), (0, '{"a": 1, "b": {"git": []}}', "")):
        monkeypatch.setattr(sai, "call", lambda *a, timeout=120, input=None, answer=answer: answer, raising=False)
        assert sai.identities() == ({"b": {"git": []}} if answer[1].startswith("{") else {})


def test_the_dialog_checks_names_logins_and_kinds(qapp):
    from sandbox_ai_app.identities_page import IdentityDialog
    dialog = IdentityDialog(None, None, None, ["GH_PUSH"], {"taken"})
    for name, problem in (("Bad Name", "lower-case"), ("default", "lower-case"), ("taken", "already")):
        dialog.name.setText(name)
        dialog.check_and_accept()
        assert problem in dialog.note.text() and dialog.result() == 0
    dialog.name.setText("work")
    dialog.add_git("https://github.com", "", "")
    dialog.check_and_accept()
    assert "server" in dialog.note.text()
    dialog.git.cellWidget(0, 2).setText("GH_PUSH")
    dialog.add_publish("pypi", "PYPI", "")
    dialog.add_publish("pypi", "PYPI2", "")
    dialog.check_and_accept()
    assert "once" in dialog.note.text()
    dialog.publish.removeRow(1)
    dialog.add_publish("npm", "", "")  # no secret: left out
    dialog.check_and_accept()
    assert dialog.result() == 1
    name, identity = dialog.result_identity()
    assert name == "work" and identity["git"] == [{"url": "https://github.com", "user": "", "secret": "GH_PUSH"}]
    assert identity["publish"] == [{"kind": "pypi", "secret": "PYPI"}]


def test_an_existing_identity_shows_its_own_server_only_when_not_the_default(qapp):
    from sandbox_ai_app.identities_page import IdentityDialog
    dialog = IdentityDialog(None, "work", OLD, [], {"work"})
    assert dialog.name.isReadOnly() and dialog.person.text() == "Ada"
    assert dialog.git.rowCount() == 2 and dialog.publish.rowCount() == 2
    assert dialog.publish.item(0, 2).text() == "" and dialog.publish.item(1, 2).text() == "https://npm.example.org"
    name, identity = dialog.result_identity()
    assert S.identity_steps(name, OLD, identity) == [], "opening and saving changes nothing"


def test_a_real_identity_through_the_real_command_line(qapp, env):
    sai = S()
    if not sai.has_identities():
        pytest.skip("this sandbox-ai has no identities")
    for secret in ("GH_PUSH", "PYPI"):
        assert sai.call("secrets", "set", secret, input="value-of-" + secret + "\n")[0] == 0
    new = {"name": "Ada Lovelace", "email": "ada@example.org", "description": "work account",
           "git": [{"url": "https://github.com", "user": "", "secret": "GH_PUSH"}],
           "publish": [{"kind": "pypi", "secret": "PYPI"}]}
    assert sai.save_identity("work", None, new)[0] == 0
    known = sai.identities()
    assert known["work"]["email"] == "ada@example.org"
    assert [e["url"] for e in known["work"]["git"]] == ["https://github.com"]
    assert [(e["kind"], e["host"]) for e in known["work"]["publish"]] == [("pypi", "upload.pypi.org")]
    assert "value-of-" not in json.dumps(known), "never a token"
    assert S.identity_steps("work", known["work"], {**new, "publish": [{"kind": "pypi", "secret": "PYPI"}]}) == []
    changed = {**new, "git": [], "publish": [{"kind": "pypi", "secret": "PYPI", "url": "https://pypi.example.org"}]}
    assert sai.save_identity("work", known["work"], changed)[0] == 0
    known = sai.identities()
    assert known["work"]["git"] == [] and known["work"]["publish"][0]["host"] == "pypi.example.org"

    from sandbox_ai_app.identities_page import IdentitiesBox
    box = IdentitiesBox(sai)
    box.shown((known, ["GH_PUSH", "PYPI"]), None)
    assert box.list.rowCount() == 1 and box.list.item(0, 0).text() == "work"
    assert "ada@example.org" in box.list.item(0, 1).text() and box.list.item(0, 3).text() == "pypi"
    assert sai.call("identities", "remove", "work")[0] == 0
    assert "work" not in sai.identities()
