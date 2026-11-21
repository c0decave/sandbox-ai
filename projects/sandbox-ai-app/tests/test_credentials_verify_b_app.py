"""Independent verification B (2026-11-19), the app: do credentials given in the app's window really reach a session
- on this machine and on another host - and nothing else?

The window is the real one (MainWindow, offscreen); its backend runs the real sandbox-ai: this machine's as a
subprocess, another host's through `ssh` exactly as the app builds that command line. What stands in for a live
container is the core test's (tests/test_credentials_verify_b.py: a user's host with Podman faked, the run's
supervisor as the real control server in a thread, an ssh that runs the remote command as the other host's user).
Only the dialogs' answers are given here; every question the app asks first is answered yes.

Names resolve without DNS only on the other host (its sandbox-ai runs with the test's python3); this machine's
sandbox-ai is the app's own Python, so what is given here goes to a Git server on the loopback (no lookup)."""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import applog, backend, board as B, credentials_page as C, widgets  # noqa: E402

REPO = Path(os.environ.get("SANDBOX_AI_HOME") or Path(__file__).resolve().parents[3])
needs_checkout = pytest.mark.skipif(not (REPO / "tests" / "test_credentials_verify_b.py").is_file()
                                    or not sys.platform.startswith("linux"),
                                    reason="needs the sandbox-ai checkout with its tests (SANDBOX_AI_HOME) on Linux")
if (REPO / "tests" / "test_credentials_verify_b.py").is_file():
    sys.path.append(str(REPO / "tests"))
    import test_credentials_verify_b as core  # noqa: E402
    from test_credentials_verify_b import pki  # noqa: E402,F401 - a fixture

from test_projects import close, make_window  # noqa: E402

pytestmark = needs_checkout
TOKEN = "gho_" + "L0c4lG1t" * 4 + "here"
FAR_TOKEN = "gho_" + "F4rG1tT0" * 4 + "away"
FAR_KEY = "sk-far-SECRET-0123456789abcdef"
VALUES = (TOKEN, FAR_TOKEN, FAR_KEY)


@pytest.fixture
def world(tmp_path, monkeypatch, qapp):
    """This machine (the app's: its environment is this user's host) and the host lab of `sandbox-ai hosts`, each
    with one running start whose supervisor answers; the window."""
    here = core.user_host(tmp_path / "here")
    far = core.user_host(tmp_path / "far", name="far")
    started = here.started = far.started = []
    (far.bin / "sandbox-ai").symlink_to(REPO / "sandbox-ai")
    (tmp_path / "far-env.json").write_text(json.dumps(far.env))
    (here.bin / "ssh").write_text(core.SSH.replace("PYTHON", sys.executable, 1))
    (here.bin / "ssh").chmod(0o755)
    here.env["CVB_REMOTE_ENV"] = str(tmp_path / "far-env.json")
    added = here.run("hosts", "add", "lab", "dev@lab.example.org", "--port", "2222")
    assert added.returncode == 0, added.stderr
    for key, value in here.env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Yes)
    # (One proxy run directory for both runs: the two supervisors are threads of this one process, and the proxy's
    # directory is the process's - what either run is given during its run lands here.)
    local = core.start(here)
    (tmp_path / "proxy-far").mkdir(mode=0o700)
    (tmp_path / "proxy-far" / "allow.json").write_text("{}")
    local.supervise(tmp_path / "proxy-far", monkeypatch)
    remote = core.start(far)
    remote.supervise(tmp_path / "proxy-far", monkeypatch)
    window = make_window(monkeypatch)
    terminals = []
    window.terminals_asked = terminals
    yield {"here": here, "far": far, "local": local, "remote": remote, "window": window, "tmp": tmp_path,
           "proxy_far": tmp_path / "proxy-far"}
    close(window)
    widgets.ThreadCall.wait_all(30)
    for run in started:
        run.abandon()


def answer(monkeypatch, dialog_class, fill):
    """The next dialogs of DIALOG_CLASS are filled by FILL(dialog) and accepted through their own check."""
    seen = []

    def run_dialog(dialog):
        fill(dialog)
        seen.append(dialog)
        if hasattr(dialog, "check_and_accept"):
            dialog.check_and_accept()
            assert dialog.result() == QtWidgets.QDialog.DialogCode.Accepted, dialog.problem.text()
        else:
            assert dialog.problem() is None, dialog.problem()
        return QtWidgets.QDialog.DialogCode.Accepted
    monkeypatch.setattr(dialog_class, "exec", run_dialog)
    return seen


def rows(table):
    return [[table.item(r, c).text() if table.item(r, c) else "" for c in range(table.columnCount())]
            for r in range(table.rowCount())]


def settled(label):
    return bool(label.text()) and not label.text().endswith("…")


def menu_action(view, text):
    """The entry TEXT of VIEW's right-click menu (the same command as the button beside the list)."""
    menu = widgets.menu_of(view)
    found = [action for action in menu.actions() if action.text().replace("&", "") == text]
    assert found, [action.text() for action in menu.actions()]
    return found[0]


def app_log():
    path = applog.log_path()
    return path.read_text(errors="replace") if path.exists() else ""


def test_a_git_login_added_and_given_in_the_window_reaches_the_running_session(world, wait, monkeypatch):
    window, local, here = world["window"], world["local"], world["here"]
    page = window.credentials
    window.show_page("credentials")
    box = page.git
    url = "http://localhost:3080"  # (a Gitea on this host's loopback)
    # Add…: the token typed in the dialog is kept as a secret (on standard input), the login names it.
    answer(monkeypatch, C.LoginDialog, lambda dialog: (dialog.edits["url"].setText(url),
                                                       dialog.edits["user"].setText("alice"),
                                                       dialog.value.setText(TOKEN)))
    box.add_button.click()
    assert wait(lambda: rows(box.list) and rows(box.list)[0][:3] == [url, "alice", "GIT_TOKEN_LOCALHOST"], 90), \
        (rows(box.list), box.message.text())
    listed = json.loads((here.home / ".config/sandbox-ai/git-logins.json").read_text())
    assert listed["logins"] == [{"url": url, "user": "alice", "secret": "GIT_TOKEN_LOCALHOST"}]
    assert (here.home / ".local/share/sandbox-ai/secrets/GIT_TOKEN_LOCALHOST").read_text().strip() == TOKEN
    # Every button of the tab is on the list's right-click menu too.
    box.list.selectRow(0)
    menu = [action.text().replace("&", "") for action in widgets.menu_of(box.list).actions()]
    for button in (box.add_button, box.remove_button, box.check_button, box.give_button):
        assert button.text() in menu, (button.text(), menu)
    # Give to a running sandbox… from the right-click menu: this machine's run is offered, by its name and run id.
    offered = []
    answer(monkeypatch, C.GiveDialog, lambda dialog: offered.extend(
        dialog.choice.itemData(i) for i in range(dialog.choice.count())))
    box.message.setText("")
    menu_action(box.list, box.give_button.text()).trigger()
    assert wait(lambda: settled(box.message), 90), box.message.text()
    assert offered == [local.run], "this machine's running start, and only it (the other host's is not offered here)"
    text = box.message.text()
    assert f"{local.run} ({local.record['name']})" in text and "has the Git login for" in text, text
    assert "the token is not in the sandbox" in text
    # With the run's connection proxy (the supervisor's file for it), not in the sandbox's session directory.
    assert core.given_logins(world["proxy_far"]) == [("localhost", 3080, "alice", TOKEN)]
    assert not (local.session / "git-tokens.json").exists() and core.files_with([local.session], [TOKEN]) == {}
    # The token is in no text of the window, not in the app's log, not on the other host.
    assert TOKEN not in text and TOKEN not in json.dumps(rows(box.list)) and TOKEN not in app_log()
    assert core.files_with([world["far"].directory], [TOKEN]) == {}
    # Remove… (right-click menu): the login goes, its secret stays; the run keeps what it was given until it ends.
    box.list.selectRow(0)
    menu_action(box.list, box.remove_button.text()).trigger()
    assert wait(lambda: rows(box.list) == [], 90), rows(box.list)
    assert json.loads((here.home / ".config/sandbox-ai/git-logins.json").read_text())["logins"] == []
    assert (here.home / ".local/share/sandbox-ai/secrets/GIT_TOKEN_LOCALHOST").exists()
    assert core.given_logins(world["proxy_far"])


def test_credentials_for_a_session_on_another_host_are_kept_and_given_there(world, wait, monkeypatch, tmp_path, pki):
    window, remote, here, far = world["window"], world["remote"], world["here"], world["far"]
    page = window.credentials
    window.show_page("credentials")
    name = remote.record["name"]
    page.hosts.box.setCurrentIndex(page.hosts.box.findData("lab"))  # the page's host chooser: lab
    assert wait(lambda: page.host() == "lab", 30), page.host()
    page.tabs.setCurrentIndex(page.tab_of("secrets"))
    # A secret kept on lab: its value goes over ssh's standard input.
    page.name.setText("FAR_KEY")
    page.value.setText(FAR_KEY)
    page.add_button.click()
    assert wait(lambda: any(row[0] == "FAR_KEY" for row in rows(page.secrets)), 120), \
        (rows(page.secrets), page.message.text())
    assert (far.home / ".local/share/sandbox-ai/secrets/FAR_KEY").read_text().strip() == FAR_KEY
    assert not (here.home / ".local/share/sandbox-ai/secrets").exists(), "nothing is kept on this machine"
    # Give to a running sandbox… (the Secrets tab's button): lab's running start, as an API key for api.creds.test.
    page.secrets.selectRow(next(i for i, row in enumerate(rows(page.secrets)) if row[0] == "FAR_KEY"))
    offered = []

    def as_key(dialog):
        offered.extend(dialog.choice.itemData(i) for i in range(dialog.choice.count()))
        dialog.host.setText("api.creds.test")
        dialog.variable.setText("OR_KEY")
    answer(monkeypatch, C.GiveDialog, as_key)
    page.message.setText("")
    page.give_key_button.click()
    assert wait(lambda: settled(page.message) and "Giving" not in page.message.text(), 120), page.message.text()
    assert offered == [name], "lab's running start by its name; this machine's run is not offered for lab"
    text = page.message.text()
    assert "lab" in text and "OR_KEY" in text and "api.creds.test" in text and "has the key" in text, text
    added = json.loads((world["proxy_far"] / "api-keys-added.json").read_text())["api_keys"]
    assert [(e["host"], e["name"], e["key"]) for e in added] == [("api.creds.test", "OR_KEY", FAR_KEY)]
    assert "192.0.2.77 api.creds.test" in (remote.session / "hosts").read_text()
    # A Git login for lab: added there (the token typed here), then given to its running start.
    page.tabs.setCurrentIndex(page.tab_of("git"))
    box = page.git
    answer(monkeypatch, C.LoginDialog, lambda dialog: (dialog.edits["url"].setText("https://git.creds.test"),
                                                       dialog.edits["user"].setText("alice"),
                                                       dialog.value.setText(FAR_TOKEN)))
    box.add_button.click()
    assert wait(lambda: rows(box.list) and rows(box.list)[0][0] == "https://git.creds.test", 120), \
        (rows(box.list), box.message.text())
    assert json.loads((far.home / ".config/sandbox-ai/git-logins.json").read_text())["logins"] == [
        {"url": "https://git.creds.test", "user": "alice", "secret": "GIT_TOKEN_GIT_CREDS_TEST"}]
    assert not (here.home / ".config/sandbox-ai/git-logins.json").exists()
    box.list.selectRow(0)
    answer(monkeypatch, C.GiveDialog, lambda dialog: None)
    box.message.setText("")
    box.give_button.click()
    assert wait(lambda: settled(box.message) and "Giving" not in box.message.text(), 120), box.message.text()
    assert "has the Git login for" in box.message.text() and "lab" in box.message.text(), box.message.text()
    # lab's run's proxy has it (named in that run's own list), and nothing of it is in what its sandbox sees.
    assert core.given_logins(world["proxy_far"]) == [("git.creds.test", 443, "alice", FAR_TOKEN)]
    assert [entry["url"] for entry in json.loads((remote.run_dir / "git-logins.json").read_text())["logins"]] == [
        "https://git.creds.test"]
    assert core.files_with([remote.session], [FAR_TOKEN]) == {}
    # The right commands went to the right host, and no value in any command line, text or log; nothing on this
    # machine's disk.
    calls = [call for call in core.ssh_calls(here) if "-N" not in call and "-O" not in call]
    assert calls and all(call[call.index("--") + 1] == "dev@lab.example.org" for call in calls)
    commands = "\n".join(call[-1] for call in calls)
    assert f"sandbox-ai secrets give {name} FAR_KEY --api-key api.creds.test --as OR_KEY;" in commands
    assert f"sandbox-ai git-logins give --secret GIT_TOKEN_GIT_CREDS_TEST --user alice -- {name} " \
           "https://git.creds.test;" in commands
    assert "sandbox-ai secrets set FAR_KEY;" in commands
    assert not any(value in core.argv_text(here) + core.argv_text(far) for value in VALUES)
    assert not any(value in app_log() for value in VALUES)
    assert not any(value in page.message.text() + box.message.text() for value in VALUES)
    assert core.files_with([here.directory], VALUES) == {}
    # This machine's own run got none of it.
    assert not (world["local"].session / "git-tokens.json").exists()
    assert not (world["local"].run_dir / "git-logins.json").exists()


def test_a_board_start_with_credentials_runs_with_them_here_and_on_another_host(world, wait, monkeypatch):
    """The Board's parts (Agent: secrets as variables, API keys; Git: a token from a secret; HOME and login: none)
    make the launcher options; the command line the app hands its terminal is run here as it is - on this machine,
    and for lab through ssh - and the sandbox that starts has the credentials (by reference: no value in a command
    line)."""
    window, here, far = world["window"], world["here"], world["far"]
    for host, key, token in ((here, "sk-here-SECRET-0123456789ab", TOKEN), (far, FAR_KEY, FAR_TOKEN)):
        core.keep_secret(host, "AGENT_KEY", key)
        core.keep_secret(host, "GT", token)
    sai = window.sai
    options = ["--secret-env", "AGENT_KEY", "--api-key", "api.creds.test=API_KEY=AGENT_KEY", "--git-token-secret",
               "https://git.creds.test=GT", "--token-proxy", "--no-auth", "--no-tmux", "--no-checkpoint",
               "--workspace", str(here.workspace)]
    board = B.from_argv(sai, "claude", options)
    props = {kind: board.first(kind).props for kind in ("agent", "git", "home")}
    assert "AGENT_KEY" in json.dumps(props["agent"]) and "--token-proxy" in props["git"] and \
        B.login_choice(props["home"]) == "none"
    argv = B.launcher_argv(sai, board)
    for option in ("--secret-env", "--api-key", "--git-token-secret", "--token-proxy", "--no-auth"):
        assert option in " ".join(argv), (option, argv)
    assert not any(value in " ".join(argv) for value in (*VALUES, "sk-here-SECRET-0123456789ab"))

    def run_started(host, command, env):
        """COMMAND (what the app's terminal runs) as a process; the session directory and the proxy's directory its
        sandbox got (from the `podman run` it reached)."""
        release = Path(host.env["CVB_RELEASE"])
        release.unlink(missing_ok=True)
        before = len(host.calls())
        process = subprocess.Popen(command, env=env, cwd=here.workspace, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        call = None
        try:
            assert wait(lambda: process.poll() is not None or any(
                c[:1] == ["run"] and "--pod" in c for c in host.calls()[before:]), 120)
            call = next((c for c in host.calls()[before:] if c[:1] == ["run"] and "--pod" in c), None)
            assert call is not None, process.communicate()
            mounts = {v.split(",dst=", 1)[1].split(",", 1)[0]: Path(v[len("type=bind,src="):].split(",dst=", 1)[0])
                      for v in call if v.startswith("type=bind,src=")}
            config = json.loads((mounts["/sandbox/session"] / "config.json").read_text())
            proxy = json.loads((mounts["/sandbox/proxy"] / "proxy.json").read_text())
            return config, proxy
        finally:
            release.touch()
            out, err = process.communicate(timeout=120)
            assert not any(value in out + err for value in VALUES)

    # On this machine: the app's own command line (its Python, sandbox-ai's main.py).
    local_argv = sai.launch_argv("claude", argv)
    assert local_argv[:3] == [sys.executable, "-I", str(REPO / "sandbox" / "main.py")]
    if os.geteuid() == 0:  # (the launcher starts nothing as root: these tests' python3 says uid 1000, as live_fakes')
        local_argv = [str(here.bin / "python3"), *local_argv[1:]]
    config, proxy = run_started(here, local_argv, {**here.env, "FAKE_PODMAN_RUN": "0:120"})
    env = config["env"]
    assert env["AGENT_KEY"] == "sk-here-SECRET-0123456789ab" and env["API_KEY"].endswith("placeholder-API_KEY")
    assert TOKEN not in json.dumps(config), "--token-proxy: the Git token is the proxy's"
    assert [t["token"] for t in proxy["git_tokens"]] == [TOKEN]
    assert [(k["host"], k["key"]) for k in proxy["api_keys"]] == [("api.creds.test", "sk-here-SECRET-0123456789ab")]
    # On lab: ssh -tt to its target with the same options; the secrets are lab's own (the same names there).
    (world["tmp"] / "far-env.json").write_text(json.dumps({**far.env, "FAKE_PODMAN_RUN": "0:120"}))
    far_options = [far.workspace if option == str(here.workspace) else option for option in options]
    far_board = B.from_argv(sai, "claude", [str(o) for o in far_options])
    remote_argv = sai.host_launch_argv("lab", "claude", B.launcher_argv(sai, far_board, local=False))
    assert remote_argv[0] == "ssh" and "-tt" in remote_argv and "dev@lab.example.org" in remote_argv
    assert not any(value in " ".join(remote_argv) for value in VALUES)
    config, proxy = run_started(far, remote_argv, dict(os.environ))
    env = config["env"]
    assert env["AGENT_KEY"] == FAR_KEY and env["API_KEY"].endswith("placeholder-API_KEY")
    assert [t["token"] for t in proxy["git_tokens"]] == [FAR_TOKEN] and FAR_TOKEN not in json.dumps(config)
    assert not any(value in core.argv_text(here) + core.argv_text(far) for value in VALUES)


def test_secrets_and_logins_of_a_start_on_another_host_opens_that_hosts_credentials(world, wait):
    """The Sandboxes page's "Secrets and logins…" of a start on lab: the Credentials page with lab chosen, so that
    what is given next goes to that host."""
    window = world["window"]
    window.show_credentials_of("lab")
    assert window.current_page() == "credentials"
    assert wait(lambda: window.credentials.host() == "lab", 30)
    window.show_credentials_of("local")
    assert wait(lambda: window.credentials.host() == "local", 30)


def test_a_sessions_agent_login_is_used_for_a_new_start_from_the_window(world, wait, monkeypatch, tmp_path, qapp):
    """Agent logins tab › Logins of the sessions › Use for a new start…: the Board takes "Login from: a session", and
    the start the Board makes really has that session's login in its HOME. Log in… on another host is that host's
    `sandbox-ai login AGENT` in a terminal (ssh -tt)."""
    window, here = world["window"], world["here"]
    here.add_image("localhost/codex-sandbox:latest")
    login = tmp_path / "auth.json"
    login.write_text(core.codex_login())
    login.chmod(0o600)
    first = core.start(here, "--auth-file", str(login), agent="codex")
    name = first.record["name"]
    assert first.end()[0] == 0  # (its HOME is kept, with the login)
    page = window.credentials
    window.show_page("credentials")
    box = page.session_logins
    box.refresh()
    assert wait(lambda: any(row[0] == name for row in rows(box.list)), 120), (rows(box.list), box.message.text())
    assert core.LOGIN_SECRET not in json.dumps(rows(box.list))
    box.list.selectRow(next(i for i, row in enumerate(rows(box.list)) if row[0] == name))
    # (the same command on the list's right-click menu)
    menu_action(box.list, box.use_button.text()).trigger()
    qapp.processEvents()
    board = window.board
    assert window.current_page() == "board"
    assert board.board.first("agent").props.get("agent") == "codex"
    assert board.board.first("home").props.get("--auth-from-session") == name
    argv = B.launcher_argv(board.sai, board.board)
    assert argv[argv.index("--auth-from-session") + 1] == name
    # The Board's start, run as the app's terminal would run it: the new sandbox's HOME has that session's login.
    command = window.sai.launch_argv("codex", [*argv, "--no-tmux", "--no-checkpoint", "--workspace",
                                               str(here.workspace)])
    if os.geteuid() == 0:
        command = [str(here.bin / "python3"), *command[1:]]
    release = Path(here.env["CVB_RELEASE"])
    release.unlink(missing_ok=True)
    before = len(here.calls())
    process = subprocess.Popen(command, env={**here.env, "FAKE_PODMAN_RUN": "0:120"}, cwd=here.workspace,
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert wait(lambda: process.poll() is not None or any(
            c[:1] == ["run"] and "--pod" in c for c in here.calls()[before:]), 120)
        call = next((c for c in here.calls()[before:] if c[:1] == ["run"] and "--pod" in c), None)
        assert call is not None, process.communicate()
        home = next(Path(v[len("type=bind,src="):].split(",dst=", 1)[0]) for v in call
                    if v.startswith("type=bind,src=") and ",dst=/root," in v)
        assert json.loads((home / ".codex/auth.json").read_text()) == json.loads(login.read_text())
    finally:
        release.touch()
        out, err = process.communicate(timeout=120)
    assert process.returncode == 0, err
    assert core.LOGIN_SECRET not in out + err and core.LOGIN_SECRET not in app_log()
    # Log in… with lab chosen: that host's own login command, in a terminal over ssh.
    asked = []
    monkeypatch.setattr(page, "terminal", lambda argv, title="", **kw: asked.append((list(argv), title)))
    page.hosts.box.setCurrentIndex(page.hosts.box.findData("lab"))
    assert wait(lambda: page.host() == "lab", 30)
    assert wait(lambda: any(row[0] == "codex" for row in rows(page.logins)), 120), rows(page.logins)
    page.logins.selectRow(next(i for i, row in enumerate(rows(page.logins)) if row[0] == "codex"))
    page.login(False)
    assert asked, "the login goes to a terminal"
    argv, title = asked[-1]
    assert argv[0] == "ssh" and "-tt" in argv and argv[argv.index("--") + 1] == "dev@lab.example.org"
    assert "exec sandbox-ai login codex;" in argv[-1] and "lab" in title


def test_what_a_run_was_given_can_be_taken_back_in_the_window():
    """Finding 5 (fixed 2026-11-19; a strict xfail until then): what a run was given during the run - a Git login,
    an API key - was not among the rows the window can take back (Approvals › Allowed now); an API key could not be
    taken back in the window at all, a Git login only by typing its URL into Sandboxes › Revoke…."""
    from sandbox_ai_app import approvals_sections as A
    shown = {"run": "claude-1a2b3c4d", "https": [], "tcp": [], "all_ports": [], "dns": [], "host_ports": [],
             "host_wildcards": [], "git": [{"url": "https://git.example.org", "user": "alice"}],
             "api_keys": [{"name": "OR_KEY", "host": "openrouter.ai", "port": 443,
                           "placeholder": "sandbox-ai-api-key-placeholder-OR_KEY", "secret": "OPENROUTER"}]}
    takes_back = [row["revoke"] for row in A.allowed_rows(shown) if row.get("revoke")]
    assert any("--api-key" in options for options in takes_back)
    assert any(any(option.startswith("--git-token-") for option in options) for options in takes_back)


def test_the_question_before_a_git_login_is_given_says_that_the_agent_can_read_its_token(world, wait, monkeypatch):
    """Finding 6 (a strict xfail until 2026-11-19): the Git logins tab says "no token enters a sandbox", but Give to a
    running sandbox… wrote the token into the run's session directory, where the agent can read it - and the question
    asked before did not say so. Decided (security first): the login goes to the run's connection proxy, so the tab's
    sentence holds for a give too and the question says where the token is. Only a run whose proxy cannot log in
    gets a second question - and that one says that the agent can read its token (the next test)."""
    window, local, here = world["window"], world["local"], world["here"]
    core.keep_secret(here, "GT", TOKEN)
    assert here.run("git-logins", "add", "http://localhost:3080", "--secret", "GT").returncode == 0
    page = window.credentials
    window.show_page("credentials")
    box = page.git
    page.refresh()
    assert wait(lambda: rows(box.list) != [], 90), box.message.text()
    box.list.selectRow(0)
    answer(monkeypatch, C.GiveDialog, lambda dialog: None)
    box.message.setText("")
    del widgets.SHOWN[:]
    box.give_button.click()
    assert wait(lambda: settled(box.message) and "Giving" not in box.message.text(), 90), box.message.text()
    assert core.given_logins(world["proxy_far"]) == [("localhost", 3080, "x-access-token", TOKEN)]
    assert not (local.session / "git-tokens.json").exists(), "the token is NOT in what the sandbox sees"
    asked = " ".join(text for _title, text, _details in widgets.SHOWN)
    assert "Give the Git login" in asked
    assert "connection proxy logs in with it" in asked and "the token does not enter the sandbox" in asked
    assert "agent can read" not in asked, "one question: nothing goes where the agent can read it"


def test_a_git_login_for_a_run_without_the_proxy_is_asked_about_saying_that_the_agent_can_read_it(world, wait,
                                                                                                   monkeypatch,
                                                                                                   tmp_path):
    """The other half of finding 6: a run without the connection proxy (--no-proxy). The login cannot go to a proxy;
    the window asks a second question that says so - "the agent can read it" - before anything is put into the
    sandbox, and what it reports afterwards says it again."""
    window, here = world["window"], world["here"]
    world["local"].abandon()
    bare = core.start(here, "--no-proxy")
    bare.supervise(world["proxy_far"], monkeypatch)
    core.keep_secret(here, "GT", TOKEN)
    assert here.run("git-logins", "add", "http://localhost:3080", "--secret", "GT").returncode == 0
    page = window.credentials
    window.show_page("credentials")
    box = page.git
    page.refresh()
    assert wait(lambda: rows(box.list) != [], 90), box.message.text()
    box.list.selectRow(0)
    answer(monkeypatch, C.GiveDialog, lambda dialog: None)
    box.message.setText("")
    del widgets.SHOWN[:]
    box.give_button.click()
    assert wait(lambda: settled(box.message) and "Giving" not in box.message.text(), 120), box.message.text()
    questions = [text for _title, text, _details in widgets.SHOWN]
    second = [text for text in questions if "the agent can read it" in text]
    assert len(second) == 1 and "cannot be given the Git login" in second[0] and bare.record["name"] in second[0]
    assert TOKEN in (bare.session / "git-tokens.json").read_text(), "after that yes: in what the sandbox sees"
    text = box.message.text()
    assert "in the sandbox" in text and "the agent can read the token" in text and TOKEN not in text
    assert core.given_logins(world["proxy_far"]) == []
