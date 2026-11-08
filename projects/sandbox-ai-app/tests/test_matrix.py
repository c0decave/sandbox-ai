"""Workflow matrix of the window, offscreen: each real workflow walked end to end through the real code (pages,
board model, wizard, the SandboxAI front end), parametrised over the dimensions that change what it does - the
agent (claude, codex, opencode), the backend (podman, bwrap), the language and the theme where it shows text.

Only what lies outside the app is faked: the sandbox-ai commands (SandboxAI.call's runner records the argv and
the standard input and answers like sandbox-ai), the terminal (terminal.open records), the run registry
(SandboxAI.instances) and the network. Every test asserts the exact sandbox-ai command line the app runs and what
the user sees."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import time

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6 import QtCore  # noqa: E402
from sandbox_ai_app import backend, board as B, i18n, theme, widgets  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")
AGENTS = ("claude", "codex", "opencode")
BACKENDS = ("podman", "bwrap")


# -- the fakes --


class Commands:
    """The runner of SandboxAI.call: every sandbox-ai command the app runs, as (arguments after main.py, standard
    input), answered by REPLIES ({first arguments: (status, out, err) or a function of the arguments})."""

    def __init__(self, replies=None):
        self.log, self.replies = [], dict(replies or {})

    def __call__(self, argv, capture_output=True, text=True, timeout=None, input=None, stdin=None, **_):
        args = list(argv[3:])
        self.log.append((args, input))
        reply = (0, "", "")
        for length in range(len(args), 0, -1):
            if tuple(args[:length]) in self.replies:
                reply = self.replies[tuple(args[:length])]
                break
        if callable(reply):
            reply = reply(args)
        return subprocess.CompletedProcess(argv, *reply)

    def argv(self, *prefix):
        """The commands that begin with PREFIX."""
        return [args for args, _ in self.log if args[:len(prefix)] == list(prefix)]


def instance(kind, name, project, agent="claude", engine="podman", state="running"):
    run = name if kind == "run" else f"{agent}-{name}-1a2b"
    return backend.Instance(kind, f"{kind}:{name}" if kind == "session" else f"run:{name}", name, run=run,
                            session=name if kind == "session" else None, project=str(project), agent=agent,
                            backend=engine, started="2026-11-06T10:00:00Z", state=state, proxy=True, pid=4242)


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    (tmp_path / "project").mkdir()
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)  # confirm() says yes, messages are collected
    monkeypatch.setattr(widgets, "SHOWN", [])
    return tmp_path


def make_window(qapp, monkeypatch, language="en", mode=None):
    """The main window with the terminal faked: tabs of the app's terminal are recorded (window.opened), terminal
    windows too (window.started); the sandbox-ai commands go to window.commands."""
    from sandbox_ai_app import terminal
    from sandbox_ai_app.ui import MainWindow
    i18n.set_language(language)
    monkeypatch.setattr(terminal, "available", lambda web=None: (True, ""))
    if mode:
        theme.apply(mode)
    w = MainWindow(backend.SandboxAI(), check_updates=False, approvals_interval=0)
    if w.tray is not None:
        w.tray.hide()
        w.tray = None
    w.opened, w.started = [], []
    w.commands = Commands()
    w.sai.run = w.commands
    monkeypatch.setattr(w.terminals, "open", lambda argv, title, cwd=None: w.opened.append((list(argv), title, cwd)))
    w.sai.in_terminal = lambda argv, title="": ["term", *argv]
    monkeypatch.setattr("sandbox_ai_app.ui.QProcess.startDetached",
                        lambda program, args: w.started.append([program, *args]) or True)
    for name in ("question", "information", "warning"):
        monkeypatch.setattr(QtWidgets.QMessageBox, name, lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Yes)
    return w


def close_window(w):
    w.shutdown()
    w.close()
    w.deleteLater()
    QtWidgets.QApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)


@pytest.fixture
def window(qapp, env, monkeypatch):
    w = make_window(qapp, monkeypatch)
    yield w
    close_window(w)
    i18n.set_language("en")


def launcher_args(sai, argv):
    """ARGV (a full sandbox-ai command line of the app) as the launcher's own parser reads it: (agent, namespace,
    agent arguments)."""
    assert argv[:3] == sai.command(), "run with this Python, isolated, sandbox-ai's own main.py"
    rest = argv[3:]
    agent_args = rest[rest.index("--") + 1:] if "--" in rest else []
    options = rest[:rest.index("--")] if "--" in rest else rest
    return options[0], sai.launcher.build_parser().parse_args(options), agent_args


def shown_titles():
    return [title for title, *_ in widgets.SHOWN]


# -- a) the Start page: project, agent, profile, options -> dry run -> start in the app -> the run -> stop --

# A few options of each group of the form, and what the launcher's parser must read from them.
FORM_CHOICES = (
    ("--allow-pypi", True, "presets", ["pypi", "github"]),  # Network (--allow-github comes from the profile)
    ("--allow-host", ["docs.example.org", "api.example.org"], "allow_host", ["docs.example.org", "api.example.org"]),
    ("--privacy-check", "always", "privacy_check", "always"),  # Proxy & privacy
    ("--git-identity", True, "git_identity", True),  # Git
    ("--env", ["LANG=C.UTF-8"], "env", ["LANG=C.UTF-8"]),  # Agent
    ("--memory", "4g", "memory", "4g"),  # Limits
    ("--workspace-ro", True, "workspace_ro", True),  # Workspace
    ("--tmux", True, "tmux", True),  # Display & terminal
)
START_MATRIX = [(agent, engine, language) for agent in AGENTS for engine in BACKENDS
                for language in (("de",) if (agent, engine) == ("codex", "bwrap") else ("en",))]


@pytest.mark.parametrize("agent,engine,language", START_MATRIX)
def test_start_page_workflow(qapp, env, monkeypatch, wait, agent, engine, language):
    w = make_window(qapp, monkeypatch, language=language)
    try:
        project = env / "project"
        sai, launch = w.sai, w.launch
        assert w.set_project(project)
        assert launch.workspace() == str(project)
        # The agent, a profile of it (saved earlier), then options of each group.
        launch.agent.setCurrentText(agent)
        sai.save_profile("fast", agent, ["--allow-github"], ["--verbose"])
        launch.refresh_profiles()
        launch.profile.setCurrentText("fast")
        launch.load_profile()
        assert launch.agent.currentText() == agent and launch.agent_args.text() == "--verbose"
        assert launch.workspace() == str(project), "a profile without a workspace keeps the project's"
        form = launch.form.widgets
        form["--backend"].setCurrentText(engine)
        for option, value, *_ in FORM_CHOICES:
            widget = form[option]
            if isinstance(widget, QtWidgets.QCheckBox):
                widget.setChecked(value)
            elif isinstance(widget, QtWidgets.QComboBox):
                widget.setCurrentText(value)
            elif isinstance(widget, QtWidgets.QLineEdit):
                widget.setText(value)
            else:
                widget.set_value(value)
        launch.agent_args.setText("--verbose 'two words'")
        order = [o.long for o in sai.options()]
        chosen = {"--workspace": [str(project)], "--allow-github": True, **{o: v for o, v, *_ in FORM_CHOICES}}
        if engine != "podman":  # podman is the launcher's default: not written
            chosen["--backend"] = engine
        expected = []
        for option in sorted(chosen, key=order.index):
            value = chosen[option]
            expected += [option] if value is True else [t for v in value for t in (option, v)] \
                if isinstance(value, list) else [option, value]
        assert launch.argv() == expected
        preview = launch.preview.toPlainText()
        assert preview == shlex.join(["sandbox-ai", agent, *expected, "--", "--verbose", "two words"])
        # The dry run: sandbox-ai AGENT OPTIONS --dry-run, its plan shown.
        plan = {"agent": agent, "backend": engine, "workspace": str(project)}
        w.commands.replies[(agent,)] = (0, json.dumps(plan), "")
        launch.dry_run()
        title = i18n.tr("Plan (dry run)")
        assert wait(lambda: title in shown_titles(), 10)
        assert w.commands.argv(agent) == [[agent, *expected, "--dry-run"]]
        assert json.loads(widgets.SHOWN[-1][1]) == plan
        # Start in the app: one terminal tab running exactly that command line, in the project folder.
        launch.start_in_app()
        assert len(w.opened) == 1 and w.started == []
        argv, tab_title, cwd = w.opened[0]
        assert tab_title == agent and cwd == str(project)
        parsed_agent, parsed, agent_args = launcher_args(sai, argv)
        assert parsed_agent == agent and agent_args == ["--verbose", "two words"]
        assert argv[3:] == [agent, *expected, "--", "--verbose", "two words"]
        assert parsed.backend == engine and parsed.workspace == [str(project)]
        for option, _, dest, value in FORM_CHOICES:
            assert getattr(parsed, dest) == value, option
        assert w.pages.currentWidget() is w.terminals
        # The run shows up in the registry: a row on the Sandboxes page, no second tab for it.
        tabs = w.tabs.count()
        run = instance("run", f"{agent}-project-9f8e", project, agent, engine)
        monkeypatch.setattr(sai, "instances", lambda: ([run], []))
        monkeypatch.setattr(sai, "net_commands", lambda: {"allow", "revoke", "show", "stop"})
        w.sandboxes.stoppable = None
        w.sandboxes.refresh()
        assert w.tabs.count() == tabs, "its terminal tab shows it"
        table = w.sandboxes.table
        assert table.rowCount() == 1
        cells = [table.item(0, c).text() for c in range(table.columnCount())]
        assert cells[1:5] == [run.name, str(project), agent, engine]
        assert cells[6] == i18n.tr("running") and table.item(0, 0).font().bold(), "the current project's: bold"
        assert i18n.tr("{count} running on this account", count=1) in w.sandboxes.summary.text()
        # Stop: asked (destructive), then sandbox-ai net stop RUN.
        table.selectRow(0)
        assert w.sandboxes.stop_button.isEnabled()
        w.sandboxes.stop()
        assert shown_titles()[-1] == i18n.tr("Stop") and run.name in widgets.SHOWN[-1][1]
        assert wait(lambda: w.commands.argv("net", "stop"), 10)
        assert w.commands.argv("net", "stop") == [["net", "stop", run.name]]
    finally:
        close_window(w)
        i18n.set_language("en")


# -- b) the Board: a preset of the gallery -> placeholders filled -> no problems -> Apply -> read back -> Start --

VPN_LIST = [{"profile": "mv", "type": "wireguard", "provider": "mullvad"},
            {"profile": "ts", "type": "tailscale", "provider": "tailscale"}]
LLM_LIST = [{"name": "gpu", "kind": "vllm", "url": "http://100.100.1.5:8000", "vpn": "ts"}]
# (preset, agent of the settings, backend of the settings, {placeholder: value typed}, the agent it runs)
PRESET_MATRIX = [
    ("development", "claude", "podman", {}, "claude"),
    ("binary-analysis", "codex", "bwrap", {"<folder:samples>": "{root}/samples", "<mcp:unpack>": "unpack",
                                           "<mcp:scan>": "scan"}, "codex"),
    ("remote-llm", "opencode", "podman", {"<llm:remote>": "gpu", "<vpn:tailnet>": "ts"}, "opencode"),
    ("gpu", "claude", "bwrap", {"<gpu:kind>": "dri", "<text:variant>": "cuda"}, "claude"),
    ("kept-session", "codex", "podman", {"<folder:logs>": "{root}/logs"}, "codex"),
    ("sai-time-capsule", "opencode", "bwrap", {"<text:question>": "why don't --we 'merge'"}, "opencode"),
    ("sai-network-maze", "claude", "podman", {"<text:tcp>": "192.0.2.10:5432", "<vpn:vpn>": "mv"}, "claude"),
    ("sai-forensics", "codex", "bwrap", {"<folder:evidence>": "{root}/evidence"}, "codex"),
    ("sai-sand-chess", "codex", "podman", {}, "claude"),  # not for codex: its first agent
]


def choose_in_gallery(monkeypatch, preset_id, typed):
    """The gallery as the user drives it: the card of PRESET_ID chosen, Create, the placeholder form filled in."""
    from sandbox_ai_app import preset_gallery
    real_values = preset_gallery.PlaceholderForm.values

    def values(form):
        for marker, value in typed.items():
            form.set_value(marker, value)
        return real_values(form)

    def run(gallery):
        card = next(c for c in gallery.cards if c.preset.id == preset_id)
        gallery.choose(card)
        assert gallery.create()
        return QtWidgets.QDialog.DialogCode.Accepted
    monkeypatch.setattr(preset_gallery.PlaceholderForm, "values", values)
    monkeypatch.setattr(preset_gallery.PresetGallery, "exec", run)


@pytest.mark.parametrize("preset_id,agent,engine,typed,runs", PRESET_MATRIX, ids=[m[0] for m in PRESET_MATRIX])
def test_board_from_a_preset_to_apply_and_start(window, env, monkeypatch, wait, preset_id, agent, engine, typed,
                                                 runs):
    w, sai, page = window, window.sai, window.board
    project = env / "project"
    typed = {marker: value.format(root=env) for marker, value in typed.items()}
    for value in typed.values():
        if value.startswith(str(env)):
            os.makedirs(value, exist_ok=True)
    sai.save_settings({"default_agent": agent, "default_backend": engine})
    w.commands.replies.update({("vpn", "list"): (0, json.dumps(VPN_LIST), ""),
                               ("llm", "list"): (0, json.dumps(LLM_LIST), "")})
    assert w.set_project(project)
    assert page.directory == str(project) and not page.dirty()
    choose_in_gallery(monkeypatch, preset_id, typed)
    board = page.open_gallery()
    assert board is page.board
    # Filled in: no placeholder left, nothing the launcher or the policy would refuse.
    assert B.placeholders(board) == []
    assert [p.text for p in B.problems(sai, board, page.policy()) if p.level == "error"] == []
    findings = [page.findings.item(i).text() for i in range(page.findings.count())]
    assert not any(text.startswith("✘") for text in findings), findings
    for value in typed.values():
        assert value in B.command_line(sai, board) or shlex.quote(value) in B.command_line(sai, board) or \
            any(value in str(a) for a in B.values(board, sai)[2]), value
    started_agent, found, agent_args = B.values(board, sai)
    assert started_agent == runs and found["--workspace"] == [str(project)]
    assert (found.get("--backend") or "podman") == engine
    assert page.preview.toPlainText() == B.command_line(sai, board)
    # The launcher's verdict: a dry run of exactly the board (--no-config: no project file merged in).
    w.commands.replies[(runs,)] = (0, json.dumps({"agent": runs}), "")
    page.check()
    assert wait(lambda: page.launcher_ok is True, 10)
    assert w.commands.argv(runs)[-1] == [runs, *B.launcher_argv(sai, board), "--dry-run"]
    # Apply: written, trusted (exactly the bytes written), the layout next to it; read back it is the same board.
    shown = len(widgets.SHOWN)
    assert page.apply()
    title = i18n.tr("Project configuration")
    assert wait(lambda: title in shown_titles()[shown:], 10), widgets.SHOWN[shown:]
    assert shown_titles()[shown] == i18n.tr("Write and trust") and not page.dirty()
    review = sai.review_project(project)
    assert review.trusted and review.problems == [] and (project / B.LAYOUT_NAME).is_file()
    document, main = B.project_document(sai, board)
    assert json.loads((project / ".sandbox-ai.json").read_text()) == document and main == str(project)
    trusted_note = i18n.tr("The project's trusted .sandbox-ai.json is applied by the launcher on top of these options.")
    assert wait(lambda: w.launch.project_note.text().endswith(trusted_note), 10)  # the state is read in the background
    before = (B.command_line(sai, board), B.project_document(sai, board))
    assert page.open_project(str(project))
    assert page.board is not board and (B.command_line(sai, page.board), B.project_document(sai, page.board)) == before
    # Start from the board: one tab of the app's terminal, the board's command line, in the project.
    assert page.start(embedded=True)
    assert len(w.opened) == 1
    argv, tab_title, cwd = w.opened[0]
    assert tab_title == runs and cwd == str(project)
    assert argv == sai.launch_argv(runs, B.launcher_argv(sai, page.board), agent_args)
    parsed_agent, parsed, parsed_args = launcher_args(sai, argv)
    assert parsed_agent == runs and parsed.backend == engine and parsed.no_config and parsed_args == agent_args
    assert parsed.workspace == [str(project)]


def test_a_vpn_preset_on_bwrap_is_refused_before_anything_is_written(window, env, monkeypatch, wait):
    """The negative corner of the matrix: a VPN needs Podman; with bwrap as the default backend the board says so,
    Apply writes nothing and Start starts nothing."""
    w, sai, page = window, window.sai, window.board
    project = env / "project"
    sai.save_settings({"default_backend": "bwrap"})
    w.commands.replies[("vpn", "list")] = (0, json.dumps(VPN_LIST), "")
    assert w.set_project(project)
    choose_in_gallery(monkeypatch, "vpn-all", {"<vpn:wireguard>": "mv"})
    board = page.open_gallery()
    errors = [p.text for p in B.problems(sai, board, page.policy()) if p.level == "error"]
    assert errors == [i18n.tr("A VPN needs the Podman backend.")]
    assert not page.apply() and shown_titles()[-1] == i18n.tr("Not written")
    assert not (project / ".sandbox-ai.json").exists()
    assert not page.start(embedded=True) and shown_titles()[-1] == i18n.tr("Not started") and w.opened == []


# -- c) the wizard end to end: every page as the Next button goes, Finish, the board --

# (agent, backend of the settings, start from, model typed or chosen, effort, language)
WIZARD_MATRIX = [
    ("claude", "podman", "development", "opus", "high", "en"),
    ("claude", "bwrap", "custom", "", "", "en"),
    ("codex", "podman", "custom", "gpt-5-codex", "low", "en"),
    ("codex", "bwrap", "development", "", "xhigh", "en"),
    ("opencode", "podman", "development", "anthropic/claude-x", "", "en"),
    ("opencode", "bwrap", "custom", "", "", "de"),
]


@pytest.mark.parametrize("agent,engine,start,model,effort,language", WIZARD_MATRIX)
def test_wizard_end_to_end_to_the_board(qapp, env, monkeypatch, wait, agent, engine, start, model, effort, language):
    from conftest import wait_until
    from sandbox_ai_app import wizard as wizard_module
    w = make_window(qapp, monkeypatch, language=language)
    sai, page = w.sai, w.board
    project = env / "project"
    sai.save_settings({"default_backend": engine, "default_agent": "claude"})
    monkeypatch.setattr(sai, "mcp_definitions", lambda: {"fetch": {"command": ["uvx", "mcp-server-fetch"]},
                                                         "search": {"command": ["uvx", "duckduckgo-mcp-server"]}})
    w.commands.replies[("secrets", "list")] = (0, "", "")
    monkeypatch.setattr(sai, "secrets", lambda: ("file", [("GH_PUSH", "file")]))
    writes = []
    real_write = sai.write_project
    monkeypatch.setattr(sai, "write_project", lambda directory, document: writes.append(document) or
                        real_write(directory, document))
    typed_token = "glpat" + "-very-secret-token-1234"  # made up; split so no scanner takes it for real
    received, seen = [], {}

    def drive(wizard):
        """The human at the wizard: Next page by page, Finish once."""
        wizard.written.connect(lambda board, directory: received.append((board, directory)))
        wizard.show()
        wizard.folder_page.path.edit.setText(str(project))
        wizard.next()
        assert wizard.currentPage() is wizard.agent_page
        wizard.agent_page.agent.setCurrentText(agent)
        wizard.next()
        models = wizard.model_page  # where the model runs: the agent's provider here
        assert wizard.currentPage() is models and models.cloud.isChecked()
        if model:
            index = models.model.findData(model)
            if index >= 0:
                models.model.setCurrentIndex(index)
            else:
                models.model.setEditText(model)
        if effort:
            models.effort.setCurrentIndex(models.effort.findData(effort))
        assert models.effort.isEnabled() == (agent != "opencode"), "OpenCode sets it per model"
        wizard.next()
        assert wizard.currentPage() is wizard.start_page
        if start == "custom":
            wizard.start_page.buttons["custom"].setChecked(True)
        else:
            assert wizard.start_page.select_preset(start)
        wizard.next()
        assert wizard.currentPage() is wizard.network_page
        net = wizard.network_page
        net.presets["--allow-pypi"].setChecked(True)
        net.hosts.setPlainText("\n".join([*net.hosts.values(), "docs.example.org"]))  # added below what is there
        if engine == "podman":
            net.ports.setPlainText("5432")
        wizard.next()
        assert wizard.currentPage() is wizard.proxy_page
        wizard.proxy_page.rules.add_row("api.github.com", "GET,HEAD", "/**")
        wizard.proxy_page.privacy.setCurrentIndex(wizard.proxy_page.privacy.findData("always"))
        wizard.next()
        git = wizard.git_page
        assert wizard.currentPage() is git
        assert wait_until(qapp, lambda: git.stored == ["GH_PUSH"], 10)
        git.table.setRowCount(0)
        git.add_row("https://github.com", "secret", "GH_PUSH")
        git.add_row("https://gitlab.com", "typed", typed_token)
        assert git.table.cellWidget(1, 2).echoMode() == QtWidgets.QLineEdit.EchoMode.Password
        git.identity.setChecked(True)
        wizard.next()
        assert wizard.currentPage() is wizard.mcp_page, git.note.text()
        assert git.table.cellWidget(1, 2).text() != typed_token, "the typed token is no longer held in the page"
        servers = wizard.mcp_page.servers
        names = [servers.item(i).data(QtCore.Qt.ItemDataRole.UserRole) for i in range(servers.count())]
        assert names == ["fetch", "search"]
        servers.item(0).setCheckState(QtCore.Qt.CheckState.Checked)
        wizard.next()
        assert wizard.currentPage() is wizard.hardware_page
        wizard.hardware_page.memory.setText("6g")
        wizard.hardware_page.cpus.setText("2")
        wizard.hardware_page.pids.setText("512")
        w.commands.replies[(agent,)] = (0, json.dumps({"agent": agent}), "")
        wizard.next()
        review = wizard.review_page
        assert wizard.currentPage() is review
        assert wait_until(qapp, lambda: review.verdict_ok is True, 10), review.verdict.text()
        seen["command"] = review.command.toPlainText()
        seen["findings"] = [review.findings.item(i).text() for i in range(review.findings.count())]
        seen["texts"] = [child.text() for child in wizard.findChildren(QtWidgets.QLineEdit)] + \
            [child.toPlainText() for child in wizard.findChildren(QtWidgets.QPlainTextEdit)]
        finish = wizard.button(QtWidgets.QWizard.WizardButton.FinishButton)
        finish.click()
        finish.click()  # a second Finish while it is written waits, it does not ask again
        assert wait_until(qapp, lambda: received and not wizard.isVisible(), 10)
        wait_until(qapp, lambda: False, 0.3)  # QWizard.accept() validates the last page again: no new question
        return wizard.result()
    monkeypatch.setattr(wizard_module.SandboxWizard, "exec", drive)
    try:
        wizard = w.new_project()
        # Asked once, written once, closed.
        assert shown_titles().count(i18n.tr("Write and trust")) == 1 and len(writes) == 1 and len(received) == 1
        assert wizard.result() == QtWidgets.QDialog.DialogCode.Accepted and not wizard.isVisible()
        document = writes[0]
        assert typed_token not in json.dumps(document) and not any(typed_token in t for t in seen["texts"])
        assert not any(typed_token in text for text in seen["findings"] + [seen["command"]])
        assert not [t for t in seen["findings"] if t.startswith("✘")]
        options = document["options"]
        token_file = next(v for v in options if v.startswith("https://gitlab.com=")).partition("=")[2]
        assert Path(token_file).read_text().strip() == typed_token and not token_file.startswith(str(project))
        assert oct(os.stat(token_file).st_mode & 0o777) == "0o600"
        assert document["agent"] == agent
        expected = {"--allow-pypi", "--allow-host", "docs.example.org", "--allow-http", "api.github.com GET,HEAD /**",
                    "--privacy-check", "always", "--git-token-secret", "https://github.com=GH_PUSH", "--git-identity",
                    "--mcp", "fetch", "--memory", "6g", "--cpus", "2", "--pids", "512"}
        if engine != "podman":
            expected |= {"--backend", engine}
        else:
            expected |= {"--allow-host-port", "5432"}
        if start == "development":
            expected |= {"--allow-github", "--token-proxy", "registry.npmjs.org"}
        if model:
            expected |= {"--model", model}
        if effort:
            expected |= {"--effort", effort}
        assert expected <= set(options), sorted(expected - set(options))
        assert ("--backend" in options) == (engine != "podman")
        # The board shows it: the project's, trusted, exactly what the wizard reviewed.
        assert w.pages.currentWidget() is page and page.directory == str(project) and not page.dirty()
        assert page.board is received[0][0] and page.preview.toPlainText() == seen["command"]
        assert B.project_document(sai, page.board)[0] == document
        assert sai.review_project(project).trusted and (project / B.LAYOUT_NAME).is_file()
        assert w.tabs.entry(w.tabs.currentIndex())["path"] == str(project)
        parsed_agent, parsed, _ = launcher_args(sai, sai.launch_argv(agent, B.launcher_argv(sai, page.board)))
        assert parsed_agent == agent and parsed.backend == engine and parsed.memory == "6g"
    finally:
        close_window(w)
        i18n.set_language("en")


# -- d) sessions: start one, pause, resume, attach, stop, delete; the tabs follow its state --

SESSION_MATRIX = [("claude", "podman", "en"), ("codex", "bwrap", "en"), ("opencode", "podman", "de")]


@pytest.mark.parametrize("agent,engine,language", SESSION_MATRIX)
def test_session_lifecycle_and_tab_states(qapp, env, monkeypatch, wait, agent, engine, language):
    w = make_window(qapp, monkeypatch, language=language)
    try:
        sai, launch, page = w.sai, w.launch, w.sandboxes
        project = env / "project"
        assert w.set_project(project)
        launch.agent.setCurrentText(agent)
        launch.form.widgets["--backend"].setCurrentText(engine)
        launch.session.setText("work")
        launch.start_session()  # the terminal setting says "app": a tab of the app
        assert len(w.opened) == 1
        argv, title, cwd = w.opened[0]
        assert title == f"{agent} (work)" and cwd == str(project)
        parsed_agent, parsed, _ = launcher_args(sai, argv)
        assert parsed_agent == agent and parsed.session == "work" and parsed.backend == engine
        assert parsed.workspace == [str(project)]
        # The session shows up, and a one-off run of the same agent and project started on the command line.
        registry = {"session": instance("session", "work", project, agent, engine),
                    "cli": instance("run", f"{agent}-project-c11", project, agent, engine)}
        monkeypatch.setattr(sai, "instances", lambda: (list(registry.values()), []))
        monkeypatch.setattr(sai, "net_commands", lambda: {"stop"})

        def refresh(state=None):
            if state:
                registry["session"] = instance("session", "work", project, agent, engine, state)
            page.refresh()

        def tab(key):
            index = w.tabs.find(key)
            return (w.tabs.entry(index), w.tabs.tabText(index)) if index >= 0 else (None, None)

        def actions():
            return {key: enabled for key, _, enabled, _ in w.tab_actions(w.tabs.find("session:work"))}
        refresh()
        entry, text = tab("session:work")
        assert entry["state"] == "running" and text == f"work ({agent}, {engine})"
        assert tab(f"run:{agent}-project-c11")[0] is not None, "a run started elsewhere gets its tab"
        assert actions() == {"attach-app": True, "pause": True, "resume": False, "stop": True, "show": True,
                             "close": True, "close-others": True}
        row = widgets.find_row(page.table, "session:work")
        page.table.selectRow(row)
        assert page.table.item(row, 0).text() == i18n.tr("Session")
        buttons = page.session_buttons
        assert [a for a, b in buttons.items() if b.isEnabled()] == ["attach", "attach-app", "pause"]

        def done(*command):
            assert wait(lambda: sai.run.argv(*command) and not widgets.busy(page), 10), command
        # Pause -> paused: Resume only, the tab says so.
        page.session("pause")
        done("session", "pause")
        refresh("paused")
        assert tab("session:work")[0]["state"] == "paused"
        assert [a for a, b in buttons.items() if b.isEnabled()] == ["resume"]
        assert actions()["resume"] and not actions()["pause"] and not actions()["attach-app"] and actions()["stop"]
        page.session("resume")
        done("session", "resume")
        refresh("running")
        assert tab("session:work")[0]["state"] == "running"
        # Attach: in the app (a terminal tab) and in a terminal window.
        page.session("attach-app")
        assert w.opened[-1][0][3:] == ["session", "attach", "work"]
        assert w.opened[-1][1] == i18n.tr("session {name}", name="work")
        page.session("attach")
        assert w.started[-1][0] == "term" and w.started[-1][4:] == ["session", "attach", "work"]
        # Stop: asked first, then sandbox-ai session stop; the tab stays, stopped; Resume and Delete remain.
        page.stop()
        assert i18n.tr("Stop {what}? It ends; its HOME is kept and Resume starts it again.",
                       what=i18n.tr("session {name}", name="work")) == widgets.SHOWN[-1][1]
        done("session", "stop")
        refresh("stopped")
        assert tab("session:work")[0]["state"] == "stopped"
        assert actions()["resume"] and not actions()["stop"]
        assert [a for a, b in buttons.items() if b.isEnabled()] == ["resume", "delete"]
        page.session("delete")
        done("session", "delete")
        del registry["session"]
        page.refresh()
        assert tab("session:work")[0]["state"] == "stopped", "a session that is gone stays as a stopped tab"
        assert [args for args, _ in sai.run.log if args[0] == "session"] == [
            ["session", "pause", "work"], ["session", "resume", "work"], ["session", "stop", "work"],
            ["session", "delete", "work", "--yes"]]
    finally:
        close_window(w)
        i18n.set_language("en")


# -- e) approvals: a question of each kind -> answered once / for the run / always -> sandbox-ai approvals --

def fake_run(env, agent="claude", engine="podman"):
    """A running sandbox in the run registry (this process stands in for its launcher), its approvals folder."""
    name = f"{agent}-project-1a2b"
    directory = env / "runtime" / "sandbox-ai" / "runs" / name
    (directory / "approvals").mkdir(parents=True)
    (directory / "run.json").write_text(json.dumps({
        "run": name, "agent": agent, "backend": engine, "workspace": str(env / "project"), "pid": os.getpid(),
        "session": None, "started": "2026-11-06T10:00:00Z", "proxy": True}))
    return name, directory


QUESTIONS = {
    "connect": ({"destination": "192.0.2.7:5432", "address": "192.0.2.7", "port": 5432, "host": "db.example"},
                ["192.0.2.7:5432", "db.example"]),
    "dns": ({"name": "pypi.example.org"}, ["pypi.example.org"]),
    "push": ({"url": "https://github.com/me/repo.git", "refs": ["refs/heads/main"]},
             ["refs/heads/main", "https://github.com/me/repo.git"]),
    "browser": ({"url": "https://login.example/device?code=AB-12"}, ["https://login.example/device?code=AB-12"]),
    "mcp": ({"server": "fetch", "tool": "fetch_page", "arguments": {"url": "https://example.org"}, "rule": "ask-fetch",
             "ruleset": "research"}, ["fetch_page", "fetch"]),
}
# (kind, decision, scope chosen, the agent of the run): every kind once, for the run and always (a URL has no
# scope: it is opened once), a deny and a block.
APPROVAL_MATRIX = [(kind, "allow", scope, AGENTS[i % 3])
                   for i, (kind, scope) in enumerate((k, s) for k in QUESTIONS for s in ("once", "run", "always")
                                                     if k != "browser" or s == "once")]
APPROVAL_MATRIX += [("push", "deny", "run", "codex"), ("dns", "block", "always", "opencode")]


@pytest.mark.parametrize("kind,decision,scope,agent", APPROVAL_MATRIX)
def test_an_approval_of_each_kind_and_scope(window, env, monkeypatch, wait, kind, decision, scope, agent):
    w, sai, page = window, window.sai, window.approvals
    run, directory = fake_run(env, agent)
    fields, visible = QUESTIONS[kind]
    ident = "1700000000-1-1"
    asked = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    (directory / "approvals" / f"{ident}.json").write_text(json.dumps({"id": ident, "time": asked, "timeout": 60,
                                                                       "kind": kind, **fields}))
    notes = []
    w.notify = lambda title, text, **_: notes.append((title, text))
    w.commands.replies[("approvals",)] = (0, f"{run}: {decision} ({scope})", "")
    assert w.set_project(env / "project")
    page.poll()
    # What the user sees: the sandbox, the kind and what it asks, in the table and in the notification.
    assert page.open.rowCount() == 1 and page.current() is not None, "the one question is ready to answer"
    sandbox, kind_text, question = (page.open.item(0, c).text() for c in range(3))
    assert sandbox == f"{run} ({env / 'project'})" and page.open.item(0, 0).font().bold()
    assert kind_text == i18n.tr({"connect": "connection", "dns": "name lookup", "push": "push", "browser": "open a URL",
                                 "mcp": "MCP tool call"}[kind])
    for text in visible:
        assert text in question, (text, question)
        assert text in notes[0][1], (text, notes)
    assert notes[0][0] == i18n.tr("sandbox-ai asks: {sandbox}", sandbox=sandbox)
    assert w.nav_item("approvals").text() == i18n.tr("Approvals") + "  (1)"
    assert page.url.isHidden() == (kind != "browser")
    assert page.scope.isEnabled() == (kind != "browser")
    assert page.block_button.isEnabled() == (kind in ("connect", "dns"))
    # The answer: Allow asks first (Cancel the default for "always"), Block always too; Deny takes one click.
    shown = len(widgets.SHOWN)
    page.scope.setCurrentIndex(page.scope.findData(scope))
    if decision == "allow":
        page.answer("allow")
        title, text, _ = widgets.SHOWN[shown]
        assert title == i18n.tr("Allow this request?") and sandbox in text
        assert (i18n.tr("Scope: {scope}", scope=page.scope.itemText(page.scope.findData(scope))) in text) == \
            (kind != "browser")
    elif decision == "deny":
        page.answer("deny")
        assert len(widgets.SHOWN) == shown, "no question for a deny"
    else:
        page.block_button.click()
    assert not page.allow_button.isEnabled(), "one answer at a time"
    assert wait(lambda: page.answering is None, 10)
    sent = {"allow": ("allow", scope), "deny": ("deny", "run"),  # Deny is for the run (Deny once: once)
            "block": ("deny", "always")}[decision]
    assert w.commands.argv("approvals") == [["approvals", sent[0], ident, "--scope", sent[1]]], "answered once"
    assert page.message.text() == f"{run}: {decision} ({scope})"
    assert page.scope.currentData() == "once", "a wider scope is for one answer only"
    # "Always" for a name or an address is listed per project (sandbox-ai remembered it), and can be forgotten.
    if sent[1] == "always" and kind in ("connect", "dns"):
        approvals = sai.module("approvals")
        entry = "pypi.example.org" if kind == "dns" else "192.0.2.7:5432"
        approvals.remember(env / "project", **({"host": entry} if kind == "dns" else {"tcp": entry}),
                           blocked=decision == "block")
        page.load_always()
        assert wait(lambda: page.always.rowCount() == 1, 10)
        row = [page.always.item(0, c).text() for c in range(3)]
        prefix = "blocked" if decision == "block" else "allowed"
        assert row[:2] == [str(env / "project"), entry] and row[2].startswith(i18n.tr(prefix))
        page.always.selectRow(0)
        page.forget()
        assert widgets.SHOWN[-1][0] == i18n.tr("Forget") and entry in widgets.SHOWN[-1][1]
        assert wait(lambda: w.commands.argv("approvals", "forget") and not widgets.busy(page), 10)
        assert w.commands.argv("approvals", "forget") == [["approvals", "forget", str(env / "project"), entry]]
        assert wait(lambda: page.always_message.text() == i18n.tr("Forgotten."), 10)


# -- f) files: copy out of and into a sandbox's workspace and HOME; links never followed; replace off and on --

def fake_session_home(env, name="work", agent="claude"):
    session = env / "state" / "sandbox-ai" / "sessions" / name
    (session / "home").mkdir(parents=True)
    (session / "session.json").write_text(json.dumps({"agent": agent, "options": [], "backend": "podman",
                                                      "workspace": str(env / "project"), "cwd": str(env / "project"),
                                                      "created": "2026-11-06T10:00:00Z"}))
    return session / "home"


def copy(page, wait, start):
    assert start() is True
    assert wait(lambda: not page.task.running, 10)
    return page.last_report


@pytest.mark.parametrize("where", ("workspace", "home"))
@pytest.mark.parametrize("replace", (False, True), ids=("keep", "replace"))
def test_files_in_and_out_of_a_sandbox(window, env, tmp_path, wait, where, replace):
    run, _ = fake_run(env, "codex")
    home = fake_session_home(env, agent="codex")
    root = env / "project" if where == "workspace" else home
    (root / "src").mkdir()
    (root / "src" / "main.py").write_text("print(1)\n")
    (root / "notes.txt").write_text("hello")
    precious = tmp_path / "precious.txt"  # a host file the agent points links at
    precious.write_text("the host's own")
    (root / "outside").symlink_to("/etc")
    (root / "planted").symlink_to(precious)
    page = window.files
    page.refresh_roots()
    titles = {page.roots.itemData(i): page.roots.itemText(i) for i in range(page.roots.count())}
    assert titles[str(env / "project")].startswith(f"Running sandbox {run}: workspace")
    assert titles[str(home)].startswith("Session work: HOME")
    page.roots.setCurrentIndex(page.roots.findData(str(root)))
    kinds = {page.table.item(r, 0).text(): page.table.item(r, 1).text() for r in range(page.table.rowCount())}
    assert kinds == {"src": "folder", "notes.txt": "file", "outside": "symbolic link", "planted": "symbolic link"}
    page.open_row(widgets.find_row(page.table, "planted"))
    assert "not followed" in page.message.text() and page.relative == ""
    assert page.table.item(widgets.find_row(page.table, "planted"), 4).text() == f"→ {precious}"
    # Out to the host: files and folders, never a link (nor what it points to).
    out = tmp_path / "out"
    out.mkdir()
    report = copy(page, wait, lambda: page.download(list(page.entries), out))
    assert sorted(p.name for p in out.iterdir()) == ["notes.txt", "src"]
    assert (out / "src" / "main.py").read_text() == "print(1)\n" and not (out / "planted").exists()
    skipped = " ".join(line for line in report if "symbolic link" in line)
    assert "outside" in skipped and "planted" in skipped, report
    # In from the host: a file over the planted link, and a folder holding a link of its own.
    upload = tmp_path / "planted"
    upload.write_text("from the host")
    folder = tmp_path / "data"
    folder.mkdir()
    (folder / "a.csv").write_text("1,2\n")
    (folder / "passwd").symlink_to("/etc/passwd")
    page.replace.setChecked(replace)
    report = copy(page, wait, lambda: page.upload([str(upload), str(folder)]))
    assert precious.read_text() == "the host's own", "never written through the agent's link"
    if replace:
        assert not (root / "planted").is_symlink() and (root / "planted").read_text() == "from the host"
    else:
        assert (root / "planted").is_symlink() and any("already exists" in line for line in report)
    assert (root / "data" / "a.csv").read_text() == "1,2\n" and not os.path.lexists(root / "data" / "passwd")
    assert any("passwd" in line and "symbolic link" in line for line in report)
    assert "data" in [page.table.item(r, 0).text() for r in range(page.table.rowCount())], "listed again"
    # A host link itself is refused; a second copy of the folder needs Replace.
    link = tmp_path / "link"
    link.symlink_to(precious)
    report = copy(page, wait, lambda: page.upload([str(link)]))
    assert any("links are not copied" in line for line in report)
    report = copy(page, wait, lambda: page.upload([str(folder)]))
    assert any("already exists" in line for line in report) != replace
    with pytest.raises(ValueError):
        page.go("../..")


# -- g) credentials: a secret kept (standard input, never an argument) and deleted; a VPN profile; logins --

# (agent to log in, VPN kind, its fields, the value typed into the dialog, the sandbox-ai commands expected)
CREDENTIAL_MATRIX = [
    ("claude", "wireguard", {"file": "{root}/home.conf"}, None,
     [["vpn", "add", "home", "--wireguard={root}/home.conf"]]),
    ("codex", "mullvad", {"secret": "MULLVAD", "location": "se-sto"}, "1234567890123456",
     [["secrets", "set", "MULLVAD"],
      ["vpn", "add", "home", "--mullvad", "--account-secret=MULLVAD", "--location=se-sto"]]),
    ("opencode", "tailscale", {"secret": "TS_KEY", "exit_node": "exit-1", "login_server": "https://hs.example.org"},
     "tskey-auth-kVERYSECRET",
     [["secrets", "set", "TS_KEY"], ["vpn", "add", "home", "--tailscale", "--auth-key-secret=TS_KEY",
                                     "--exit-node=exit-1", "--login-server=https://hs.example.org"]]),
]


@pytest.mark.parametrize("agent,kind,fields,typed,expected", CREDENTIAL_MATRIX, ids=[m[1] for m in CREDENTIAL_MATRIX])
def test_credentials_secrets_vpn_and_logins(window, env, monkeypatch, wait, agent, kind, fields, typed, expected):
    from sandbox_ai_app import applog, state, vpn_page
    w, sai, page = window, window.sai, window.credentials
    store = sai.module("secret_store").FileStore()

    def keep(args):  # sandbox-ai secrets set NAME: the value from standard input
        store.set(args[2], w.commands.log[-1][1].rstrip("\n"))
        return 0, f"kept {args[2]}", ""

    def delete(args):
        store.delete(args[2])
        return 0, f"deleted {args[2]}", ""
    w.commands.replies.update({("secrets", "set"): keep, ("secrets", "delete"): delete,
                               ("vpn", "list"): (0, json.dumps([{"profile": "home", "type": kind}]), "")})
    secret = "sk-live-0123456789abcdef"
    page.refresh()
    assert wait(lambda: not widgets.busy(page), 10)
    assert [page.logins.item(r, 0).text() for r in range(page.logins.rowCount())] == list(sai.agents)
    # A secret: the value is cleared from the field at once and goes on standard input only.
    page.name.setText("DEMO_TOKEN")
    page.value.setText(secret)
    page.set_secret()
    assert page.value.text() == ""
    assert wait(lambda: not widgets.busy(page) and page.secrets.rowCount() == 1, 10)
    assert [entry for entry in w.commands.log if entry[0][0] == "secrets"] == [
        (["secrets", "set", "DEMO_TOKEN"], secret + "\n")]
    assert page.secrets.item(0, 0).text() == "DEMO_TOKEN" and page.message.text() == "kept DEMO_TOKEN"
    page.secrets.selectRow(0)
    page.delete_secret()
    assert wait(lambda: not widgets.busy(page) and page.secrets.rowCount() == 0, 10)
    assert w.commands.argv("secrets", "delete") == [["secrets", "delete", "DEMO_TOKEN"]]
    # A VPN profile, through its dialog; a typed key becomes a secret first.
    fields = {k: v.format(root=env) for k, v in fields.items()}
    if "file" in fields:
        Path(fields["file"]).write_text("[Interface]\nPrivateKey = x\n")

    def dialog_exec(dialog):
        dialog.name.setText("home")
        dialog.kind.setCurrentIndex(dialog.kind.findData(kind))
        for field, value in fields.items():
            getattr(dialog, field).setText(value)
        if typed:
            dialog.value.setText(typed)
        visible = {f for f, (_, widget) in dialog.rows.items() if dialog.form.isRowVisible(widget)}
        assert visible == set(vpn_page.FIELDS[kind])
        return QtWidgets.QDialog.DialogCode.Accepted
    monkeypatch.setattr(vpn_page.VpnDialog, "exec", dialog_exec)
    start = len(w.commands.log)
    page.vpn.add_button.click()
    assert wait(lambda: w.commands.argv("vpn", "list") and not widgets.busy(page.vpn), 10)
    assert wait(lambda: page.vpn.profiles.rowCount() == 1, 10)
    sent = [(args, given) for args, given in w.commands.log[start:]
            if args[0] in ("secrets", "vpn") and args[:2] != ["vpn", "list"]]  # not the board's dry runs
    assert [args for args, _ in sent] == [[part.format(root=env) for part in command] for command in expected]
    if typed:
        assert sent[0][1] == typed + "\n" and sent[1][1] is None
    # Logins: the agent's own login in a sandbox, in a tab of the app; log out asks first.
    page.logins.selectRow(next(r for r in range(page.logins.rowCount()) if page.logins.item(r, 0).text() == agent))
    page.login(False)
    page.login(True)
    assert [argv[3:] for argv, _, _ in w.opened] == [["login", agent], ["login", agent, "--logout"]]
    # No value was on a command line, on the screen, in the app's log or in its saved state.
    w.state.flush()
    for value in filter(None, (secret, typed)):
        assert not any(value in " ".join(args) for args, _ in w.commands.log)
        assert not any(value in text for text in all_texts(w))
        for path in (applog.log_path(), state.state_file(), backend.settings_file()):
            assert not path.exists() or value not in path.read_text(errors="replace"), path


def all_texts(widget):
    found = []
    for child in widget.findChildren(QtWidgets.QWidget):
        for getter in ("text", "toPlainText", "title", "placeholderText", "toolTip", "windowTitle"):
            if hasattr(child, getter):
                try:
                    found.append(str(getattr(child, getter)()))
                except TypeError:
                    pass
    return found


# -- h) every setting through the Settings dialog, the window started again from what was saved --

def changed_settings(env):
    """A value other than the default for every setting the app knows."""
    return {"theme": "dark", "language": "de", "terminal": "window", "tray": False, "notify": False,
            "notify_seconds": 30, "notify_style": "system", "approvals_interval": 500, "sandboxes_interval": 5000, "connections_interval": 4000,
            "sandbox_ai_home": str(env / "checkout"), "default_agent": "codex", "default_backend": "bwrap",
            "default_template": "research", "board_grid": 40, "auto_tabs": False, "guide_nodes": False,
            "guide_tour": False, "intro": False, "debug": True, "help_buttons": False}


def field_value(widget):
    if isinstance(widget, QtWidgets.QComboBox):
        return widget.currentData()
    if isinstance(widget, QtWidgets.QCheckBox):
        return widget.isChecked()
    if isinstance(widget, QtWidgets.QSpinBox):
        return widget.value()
    return widget.edit.text()  # PathEdit


def set_field(widget, value):
    if isinstance(widget, QtWidgets.QComboBox):
        index = widget.findData(value)
        assert index >= 0, value
        widget.setCurrentIndex(index)
    elif isinstance(widget, QtWidgets.QCheckBox):
        widget.setChecked(value)
    elif isinstance(widget, QtWidgets.QSpinBox):
        widget.setValue(value)
    else:
        widget.edit.setText(value)
        widget.edit.editingFinished.emit()  # typed, then Enter or the next field


@pytest.mark.parametrize("agent", AGENTS)
def test_every_setting_round_trips_through_a_restart(qapp, env, monkeypatch, agent):
    from sandbox_ai_app import applog
    # A theme's style sheet makes every widget built afterwards five times slower to build: the theme is recorded
    # as the app applies it (test_ui applies them for real), and the restart applies it as main() would.
    applied = []
    monkeypatch.setattr(theme, "apply", lambda mode, app=None: applied.append(mode) or mode)
    values = changed_settings(env)
    (env / "checkout").mkdir()
    assert set(values) == set(backend.SETTINGS), "a new setting: give it a value here"
    assert all(values[key] != default for key, default in backend.SETTINGS.items())
    values["default_agent"] = agent if agent != "claude" else "opencode"
    monkeypatch.delenv("SANDBOX_AI_APP_DEBUG", raising=False)
    w = make_window(qapp, monkeypatch)
    try:
        dialog = w.settings_dialog()
        assert set(backend.SETTINGS) <= set(dialog.fields), "every setting has its field"
        saved = []
        dialog.changed.connect(saved.append)
        for key, value in values.items():
            set_field(dialog.fields[key], value)
        qapp.processEvents()  # the theme is applied after the combo box's own event
        assert applied == ["dark"]
        set_field(dialog.fields["telemetry"], "strict")
        assert set(saved) == set(values) | {"telemetry"}
        assert {key: w.sai.settings()[key] for key in values} == values
        assert oct(backend.settings_file().stat().st_mode & 0o777) == "0o600"
        assert w.sai.sandbox_defaults() == ({"version": 1, "telemetry": "strict"}, None)
        # The Start page keeps the agent and its options for the next start.
        w.launch.agent.setCurrentText(agent)
        w.launch.form.widgets["--allow-pypi"].setChecked(True)
        w.launch.agent_args.setText("--verbose --api-key=sk-not-kept")
    finally:
        close_window(w)
        applog.set_debug(False)
    # Started again, as main() starts it: the language and theme of the settings first.
    settings = w.sai.settings()
    assert theme.apply(settings["theme"]) == "dark"
    w = make_window(qapp, monkeypatch, language=settings["language"])
    try:
        assert w.nav.item(2).text() == "Tafel"
        dialog = w.settings_dialog()
        assert {key: field_value(dialog.fields[key]) for key in values} == values
        assert {key: backend.setting(key) for key in values} == values
        assert field_value(dialog.fields["telemetry"]) == "strict"
        assert w.board.default_agent() == values["default_agent"] and w.board.grid() == 40
        assert w.launch.agent.currentText() == agent and w.launch.form.widgets["--allow-pypi"].isChecked()
        assert w.launch.agent_args.text() == "--verbose", "an argument that may be a secret is not kept"
        assert "sk-not-kept" not in (env / "config" / "sandbox-ai" / "app" / "state.json").read_text()
        # The settings that act at once do: commands in a terminal window.
        w.system.tool(("build", "all"))
        assert w.opened == [] and w.started[-1][-2:] == ["build", "all"]
        from sandbox_ai_app import help_view
        assert not help_view.buttons_shown() and not w.page_help.isVisibleTo(w), "the (?)s stay off after a restart"
    finally:
        close_window(w)
        i18n.set_language("en")
        from sandbox_ai_app import help_view
        help_view.set_shown(True)
