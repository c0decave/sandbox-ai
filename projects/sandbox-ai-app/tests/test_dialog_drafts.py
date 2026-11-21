"""What an "add new" dialog keeps of what was typed and not sent (drafts.py; the bug class of 2026-11-16: set, gone
after the dialog was closed and opened again): kept for the same target in this app session's memory, with Empty the
form, dropped once sent - and never a secret's value, a password or a token, nor a box that replaces what exists."""
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtWidgets import QCheckBox, QDialog, QLineEdit, QPlainTextEdit, QVBoxLayout, QWidget  # noqa: E402
from sandbox_ai_app import backend, drafts, widgets  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    return tmp_path


def test_a_draft_never_keeps_a_secret_and_lives_in_memory_only(qapp, env):
    dialog = QDialog()
    QVBoxLayout(dialog)
    name, key, password, note, box = QLineEdit(), QLineEdit(), QLineEdit(), QPlainTextEdit(), QCheckBox()
    row = widgets.secret_value(key)  # a secret's value: never kept, also while "Show value" shows it
    password.setEchoMode(QLineEdit.EchoMode.Password)
    fields = {"name": name, "key": key, "password": password, "note": note, "box": box}
    draft = drafts.Draft(dialog, ("test",), fields)
    assert draft.notice.isHidden() and not draft.restored
    name.setText("home")
    key.setText("wg-private-key")
    key.show_toggle.setChecked(True)
    password.setText("hunter2")
    note.setPlainText("two\nlines")
    box.setChecked(True)
    dialog.reject()
    assert drafts.DRAFTS[("test",)] == {"name": "home", "note": "two\nlines", "box": True}
    assert "wg-private-key" not in repr(drafts.DRAFTS) and "hunter2" not in repr(drafts.DRAFTS)
    again = QDialog()
    QVBoxLayout(again)
    fields2 = {"name": QLineEdit(), "key": QLineEdit(), "note": QPlainTextEdit(), "box": QCheckBox()}
    draft2 = drafts.Draft(again, ("test",), fields2)
    assert fields2["name"].text() == "home" and fields2["box"].isChecked() and draft2.restored
    assert not draft2.notice.isHidden() and draft2.notice.parent() is again
    draft2.empty_button.click()
    assert fields2["name"].text() == "" and not fields2["box"].isChecked() and ("test",) not in drafts.DRAFTS
    again.reject()
    assert ("test",) not in drafts.DRAFTS, "closed empty: nothing to keep"
    for made in (dialog, again, row):
        made.deleteLater()
    for index in range(drafts.LIMIT + 5):
        drafts.keep(("many", index), {"x": str(index)})
    assert len(drafts.DRAFTS) == drafts.LIMIT and ("many", 0) not in drafts.DRAFTS


def test_a_new_vpn_profile_keeps_what_was_typed_until_it_is_added(qapp, env, monkeypatch, wait):
    from sandbox_ai_app import vpn_page
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    sai = backend.SandboxAI()
    answers = [(1, "", "vpn add: no such file: /etc/wg/home.conf"), (0, "", "")]
    calls = []
    monkeypatch.setattr(sai, "call", lambda *args, timeout=120, input=None: calls.append(args) or answers.pop(0))
    parent = QWidget()
    seen = []

    def run(dialog):
        seen.append((dialog.name.text(), dialog.file.text(), dialog.value.text(), dialog.force.isChecked()))
        if not dialog.name.text():
            dialog.name.setText("home")
            dialog.file.setText("/etc/wg/home.conf")
            dialog.secret.setText("WG_KEY")
            dialog.value.setText("wg-private-key")
            dialog.force.setChecked(True)
        dialog.done(QDialog.DialogCode.Accepted)
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(vpn_page.VpnDialog, "exec", run)
    added = []
    vpn_page.add_profile(parent, sai, added.append)
    assert wait(lambda: added == [None], 10), "refused by sandbox-ai"
    kept = drafts.DRAFTS[("vpn", "local")]
    assert kept["name"] == "home" and kept["secret"] == "WG_KEY" and "force" not in kept
    assert "wg-private-key" not in repr(drafts.DRAFTS), "the key typed is never kept"
    vpn_page.add_profile(parent, sai, added.append)
    assert seen[1] == ("home", "/etc/wg/home.conf", "", False), "filled in again: not the key, not Replace"
    assert wait(lambda: added == [None, "home"], 10)
    assert ("vpn", "local") not in drafts.DRAFTS, "added: done with"
    parent.deleteLater()


def test_a_new_llm_server_keeps_what_was_typed_but_an_edit_shows_the_server(qapp, env):
    from sandbox_ai_app import llm_page
    key = ("llm", "local")
    dialog = llm_page.LlmDialog(None, "local", draft=key)
    dialog.name.setText("lab")
    dialog.url.setText("http://192.0.2.7:1234")
    dialog.model.setText("qwen3-coder")
    dialog.secret.setText("LAB_KEY")
    dialog.value.setText("sk-typed-key")
    dialog.insecure.setChecked(True)
    dialog.reject()
    assert "sk-typed-key" not in repr(drafts.DRAFTS) and "insecure" not in drafts.DRAFTS[key]
    again = llm_page.LlmDialog(None, "local", draft=key)
    assert (again.name.text(), again.url.text(), again.model.text(), again.secret.text()) == \
        ("lab", "http://192.0.2.7:1234", "qwen3-coder", "LAB_KEY")
    assert again.value.text() == "" and not again.insecure.isChecked() and not again.draft.notice.isHidden()
    edit = llm_page.LlmDialog(None, "local", existing={"name": "prod", "kind": "openai-compatible",
                                                       "url": "https://llm.example.org"}, draft=key)
    assert edit.draft is None and edit.name.text() == "prod", "an edit shows the server as it is"
    again.finish()  # saved
    for made in (dialog, again, edit):
        made.deleteLater()


def test_a_new_login_host_and_team_task_keep_what_was_typed(qapp, env):
    from sandbox_ai_app import credentials_page as C, hosts_page, team_page
    key = ("git-logins", "local")
    dialog = C.LoginDialog(None, "New Git login", ["GH_PUSH"], (("Git server", "url", ""), ("User", "user", "")),
                           draft=key)
    dialog.edits["url"].setText("https://git.example.org")
    dialog.edits["user"].setText("me")
    dialog.secret.setText("MY_GITEA")
    dialog.value.setText("token-typed-here")
    dialog.reject()
    assert "token-typed-here" not in repr(drafts.DRAFTS)
    again = C.LoginDialog(None, "New Git login", ["GH_PUSH"], (("Git server", "url", ""), ("User", "user", "")),
                          draft=key)
    assert (again.edits["url"].text(), again.edits["user"].text(), again.secret.text(), again.value.text()) == \
        ("https://git.example.org", "me", "MY_GITEA", "")
    again.edits["url"].setText("https://git.example.net")
    assert again.secret.text() == "MY_GITEA", "a name typed by hand stays"
    other = C.LoginDialog(None, "New Git login", [], (("Git server", "url", ""),), draft=("git-logins", "ct1"))
    assert other.edits["url"].text() == "", "another host's dialog starts empty"
    host = hosts_page.AddHostDialog(None, draft=("host-add",))
    host.name.setText("ct101")
    host.ssh.setText("me@192.0.2.10")
    host.port.setValue(2222)
    host.reject()
    host2 = hosts_page.AddHostDialog(None, draft=("host-add",))
    assert (host2.name.text(), host2.ssh.text(), host2.port.value()) == ("ct101", "me@192.0.2.10", 2222)
    post = team_page.PostDialog(None, "alpha", ["claude", "codex"], draft=("team-post", "local", "alpha"))
    post.agent.setCurrentIndex(1)
    post.task.setPlainText("Write the tests for the parser.")
    post.reject()
    post2 = team_page.PostDialog(None, "alpha", ["claude", "codex"], draft=("team-post", "local", "alpha"))
    assert post2.agent.currentData() == "codex" and post2.task.toPlainText() == "Write the tests for the parser."
    for made in (dialog, again, other, host, host2, post, post2):
        made.deleteLater()


def test_the_images_dialogs_keep_what_was_typed_until_sandbox_ai_took_it(qapp, env, monkeypatch):
    """New variant…, Variant from a recipe…, Add an image from a registry… and from a Containerfile… keep what was
    typed (per host; a pull per reference) until sandbox-ai added it - never the boxes that replace an image, let a
    build reach the network or vouch for an agent-written Containerfile."""
    from sandbox_ai_app import images_page
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    sai = backend.SandboxAI()
    page = images_page.ImagesPage(sai, lambda argv, title, **kw: None)
    page.user_entries = []
    runs = []
    monkeypatch.setattr(page.task, "run", lambda argv, done, text, source=None, input=None, **kw:
                        runs.append((argv, done)))
    seen = []

    def opened(cls, fill, answer):
        def run(dialog, *args):
            seen.append(dialog)
            fill(dialog)
            dialog.done(1 if answer else 0)
            return answer
        monkeypatch.setattr(cls, "exec", run)

    def variant(dialog):
        if not dialog.name.text():
            dialog.name.setText("web")
            dialog.copy.setChecked(False)
    opened(images_page.NewVariantDialog, variant, False)
    page.new_variant()
    opened(images_page.NewVariantDialog, lambda dialog: None, True)
    page.new_variant()
    assert seen[-1].name.text() == "web" and not seen[-1].copy.isChecked(), "filled in again"
    argv, done = runs[-1]
    assert argv[-6:-3] == ["images", "new", "web"] or "web" in argv
    done(1, "", "sandbox-ai images: refused")
    assert ("image-variant", "local") in drafts.DRAFTS, "refused: kept"
    page.new_variant()
    runs[-1][1](0, "{}", "")
    assert ("image-variant", "local") not in drafts.DRAFTS, "created: done with"

    def pull(dialog):
        dialog.reference.setText("docker.io/library/redis:7")
        dialog.name.setText("cache")
        dialog.replace.setChecked(True)
    opened(images_page.PullDialog, pull, False)
    page.pull_image()
    opened(images_page.PullDialog, lambda dialog: None, False)
    page.pull_image()
    assert (seen[-1].reference.text(), seen[-1].name.text()) == ("docker.io/library/redis:7", "cache")
    assert not seen[-1].replace.isChecked(), "replacing is chosen anew each time"
    page.pull_image("quay.io/org/app:1")
    assert seen[-1].reference.text() == "quay.io/org/app:1" and seen[-1].name.text() == "", "another reference"

    def build(dialog):
        dialog.name.setText("tools")
        dialog.file.setText("/src/Containerfile")
        dialog.network.setChecked(True)
    opened(images_page.BuildImageDialog, build, False)
    page.build_user_image()
    opened(images_page.BuildImageDialog, lambda dialog: None, False)
    page.build_user_image()
    assert (seen[-1].name.text(), seen[-1].file.text()) == ("tools", "/src/Containerfile")
    assert not seen[-1].network.isChecked(), "the network for a build is chosen anew each time"
    opened(images_page.RecipeDialog, lambda dialog: dialog.name.setText("av"), False)
    page.new_from_recipe()
    opened(images_page.RecipeDialog, lambda dialog: None, False)
    page.new_from_recipe()
    assert seen[-1].name.text() == "av"
    page.deleteLater()


def test_resume_with_team_start_and_new_bridge_keep_what_was_typed(qapp, env):
    """Resume with… (per start and host: the folders, the mounts as rows - a half filled one too -, the MCP
    servers, the options; never the restart), Start a team (per host: the lead before its members, Per member before
    its rows; never the shared OpenVPN confirmation) and New bridge (per pair) keep what was typed and not sent."""
    from sandbox_ai_app import bridges_view, team_page, ui
    key = ("resume", "local", "web")
    resume = ui.ResumeDialog(None, "web", running=True, draft=key)
    resume.workspaces.edit.setPlainText("/srv/docs")
    resume.mounts.add_row("/srv/data", "/mnt/data", "rw")
    resume.mounts.add_row("", "/mnt/half")
    resume.servers.setText("fetch")
    resume.options.setText("--cpus 4")
    resume.restart.setChecked(False)
    resume.reject()
    again = ui.ResumeDialog(None, "web", running=True, draft=key)
    assert again.workspaces.edit.toPlainText() == "/srv/docs" and again.servers.text() == "fetch"
    assert again.mounts.draft_value() == [["/srv/data", "/mnt/data", "rw"], ["", "/mnt/half", "ro"]]
    assert again.options.text() == "--cpus 4" and again.restart.isChecked(), "the restart follows the start"
    assert again.draft.restored and not again.draft.notice.isHidden()
    other = ui.ResumeDialog(None, "api", draft=("resume", "local", "api"))
    assert other.servers.text() == "" and other.mounts.draft_value() == [], "another start's dialog starts empty"
    team_key = ("team-start", "local", "")
    start = team_page.StartDialog(None, "this machine", False, draft=team_key)
    start.name.setText("alpha")
    start.lead.setCurrentIndex(start.lead.findData("codex"))
    start.members["claude"].setChecked(True)
    start.workspace.setText("/srv/project")
    start.per_member.setChecked(True)
    start.member_own["claude"][1].setText("writes the tests first")
    start.shared_openvpn.setChecked(True)
    start.reject()
    start2 = team_page.StartDialog(None, "this machine", False, draft=team_key)
    assert (start2.name.text(), start2.lead.currentData(), start2.workspace.text()) == ("alpha", "codex", "/srv/project")
    assert start2.agents() == ["codex", "claude"] and start2.per_member.isChecked()
    assert start2.member_settings()[0] == {"claude": "writes the tests first"}
    assert not start2.shared_openvpn.isChecked(), "sharing one OpenVPN profile is confirmed anew"
    sessions = [{"name": "alpha", "agent": "claude"}, {"name": "beta", "agent": "codex"},
                {"name": "gamma", "agent": "qwen"}]
    bridge_key = ("bridge-new", "alpha", None)
    bridge = bridges_view.BridgeDialog(None, sessions, first="alpha", draft=bridge_key)
    bridge.b.setCurrentIndex(bridge.b.findData("gamma"))
    bridge.message.setChecked(True)
    bridge.read.edit.setPlainText("docs")
    bridge.until.setText("2h")
    bridge.reject()
    bridge2 = bridges_view.BridgeDialog(None, sessions, first="alpha", draft=bridge_key)
    assert (bridge2.a.currentData(), bridge2.b.currentData()) == ("alpha", "gamma")
    assert bridge2.message.isChecked() and bridge2.read.paths() == ["docs"] and bridge2.until.text() == "2h"
    bridge3 = bridges_view.BridgeDialog(None, sessions, first="beta", draft=("bridge-new", "beta", None))
    assert bridge3.until.text() == "" and not bridge3.message.isChecked(), "another pair starts anew"
    for made in (resume, again, other, start, start2, bridge, bridge2, bridge3):
        made.deleteLater()
