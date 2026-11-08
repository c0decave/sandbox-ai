"""Review round 4, the wizard, the Board and the Hosts page (offscreen): Write and trust asks through the one
confirmation policy (project-trust, Cancel the default); the wizard's Model choice is per folder, a project's own server
is let go for the cloud only after a yes, and a server with several models and no default is offered as NAME:MODEL
only; the wizard says it is for this machine and leads to the Board for a host; on another host the Board offers an
untrusted board's start after a yes, reads that host's options again after a failed read, and runs one dry run over
SSH at a time; the Hosts page's defaults take subnet and subnet6, a change asked first."""
import ast
import json
import os
from pathlib import Path

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, board as B, widgets, wizard_llm as WL  # noqa: E402

PACKAGE = Path(__file__).resolve().parents[1] / "sandbox_ai_app"
needs_sai = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                               reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")
HOSTS = [{"name": "local", "kind": "local"}, {"name": "ct1", "kind": "ssh", "ssh": "admin@192.0.2.10"}]


@pytest.fixture
def env(qapp, tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    return tmp_path


def recorder(answer):
    asked = []

    def ask_first(parent, action, title, text, accept, detail=None, details=None):
        asked.append((action, title, text, detail))
        return answer
    return asked, ask_first


# -- 1. Write and trust: the policy's project-trust, never a bare confirm --

def test_no_write_and_trust_is_a_bare_confirm():
    """The Board's Apply and the wizard's Finish ask through widgets.ask_first("project-trust"): a bare confirm()
    defaulted to accept and bypassed the policy."""
    for name in ("board_view.py", "wizard.py"):
        tree = ast.parse((PACKAGE / name).read_text())
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call) or getattr(call.func, "id", None) not in ("confirm", "ask_first"):
                continue
            words = ast.unparse(call)
            if "Write and trust" in words:
                assert call.func.id == "ask_first" and "'project-trust'" in words, f"{name}: {words[:120]}"


@needs_sai
def test_the_boards_apply_asks_project_trust_and_writes_nothing_declined(env, monkeypatch):
    from sandbox_ai_app import board_view
    from sandbox_ai_app.board_view import BoardPage
    asked, ask_first = recorder(False)
    monkeypatch.setattr(board_view, "ask_first", ask_first)
    page = BoardPage(backend.SandboxAI(), lambda *a, **k: None, lambda: {"board_grid": 20})
    project = env / "project"
    project.mkdir()
    assert page.open_project(str(project))
    page.set_prop(page.board.first("network").id, "--allow-pypi", True)
    assert page.apply() is False
    assert [a[0] for a in asked] == ["project-trust"] and not (project / ".sandbox-ai.json").exists()
    page.deleteLater()


@needs_sai
def test_the_wizards_finish_asks_project_trust(env, monkeypatch):
    from sandbox_ai_app import wizard as module
    asked, ask_first = recorder(False)
    monkeypatch.setattr(module, "ask_first", ask_first)
    wizard = module.SandboxWizard(backend.SandboxAI(), lambda: {"default_agent": "claude"})
    (env / "p").mkdir()
    wizard.directory = str(env / "p")
    wizard.board = B.from_argv(wizard.sai, "claude", [], workspaces=[str(env / "p")])
    assert wizard.write() is False
    assert [a[0] for a in asked] == ["project-trust"] and not (env / "p" / ".sandbox-ai.json").exists()
    wizard.deleteLater()


# -- 2. the wizard's Model choice --

def test_a_bare_server_name_only_where_it_starts():
    """With several models and no default model the launcher refuses --llm NAME: only NAME:MODEL is offered then."""
    several = {"name": "lm", "kind": "lmstudio", "models": ["a", "b"]}
    assert WL.placeholder_choices(several) == ["lm:a", "lm:b"]
    assert WL.placeholder_choices({**several, "model": "a"}) == ["lm", "lm:a", "lm:b"], "a default model"
    assert WL.placeholder_choices({**several, "models": ["a"]}) == ["lm", "lm:a"], "its only model"
    assert WL.placeholder_choices({**several, "models": []}) == ["lm"], "none listed: the launcher tells"
    values = [value for _, value, _ in WL.model_choices([several], "codex")]
    assert values == ["", "lm:a", "lm:b"]


@needs_sai
def test_the_preset_form_preselects_no_bare_name_of_a_server_with_several_models(env, monkeypatch):
    from sandbox_ai_app import presets as PR, preset_gallery
    sai = backend.SandboxAI()
    monkeypatch.setattr(preset_gallery, "background", lambda function, *a, done, **k: done(function(*a), None))
    monkeypatch.setattr(sai, "llm_endpoints", lambda: [{"name": "lm", "kind": "lmstudio", "models": ["a", "b"]}])
    monkeypatch.setattr(sai, "host_llm_endpoints", lambda host: sai.llm_endpoints(), raising=False)
    form = preset_gallery.PlaceholderForm(sai, PR.find("local-llm"), agent="opencode")
    combo = form.fields["<llm:local>"]
    assert [combo.itemText(i) for i in range(combo.count())] == ["", "lm:a", "lm:b"] and combo.currentText() == ""
    monkeypatch.setattr(sai, "llm_endpoints", lambda: [{"name": "lm", "kind": "lmstudio", "models": ["a"]}])
    single = preset_gallery.PlaceholderForm(sai, PR.find("local-llm"), agent="opencode")
    assert single.fields["<llm:local>"].currentText() == "lm", "one model: the name alone starts"
    form.deleteLater()
    single.deleteLater()


@pytest.fixture
def wizard(env, monkeypatch):
    from sandbox_ai_app import wizard as module
    monkeypatch.setattr(module, "background", lambda function, *a, done, **k: done(function(*a), None))
    endpoints = [{"name": "lm", "kind": "lmstudio", "url": "http://127.0.0.1:1234", "model": "q", "models": ["q"],
                  "agents": ["claude", "codex", "opencode"]}]
    sai = backend.SandboxAI()
    monkeypatch.setattr(sai, "call", lambda *args, timeout=120, input=None: (0, json.dumps(endpoints), "")
                        if args[:2] == ("llm", "list") else (0, "[]", ""))
    w = module.SandboxWizard(sai, lambda: {"default_agent": "opencode", "default_template": "development"})
    for name in ("one", "two"):
        (env / name).mkdir()
    yield w
    w.deleteLater()


def to_folder(wizard, path):
    wizard.folder_page.path.edit.setText(str(path))
    assert wizard.folder_page.validatePage()
    wizard.model_page.initializePage()


@needs_sai
def test_the_model_choice_is_made_per_folder(wizard, env):
    """Regression: the server chosen for one folder carried over to the next folder's project."""
    page = wizard.model_page
    to_folder(wizard, env / "one")
    page.own.setChecked(True)
    widgets.select_keys(page.servers, ["lm"])
    assert page.validatePage() and wizard.llm_choice["name"] == "lm"
    to_folder(wizard, env / "two")
    assert page.cloud.isChecked() and wizard.llm_choice is None and not page.servers.isEnabled()
    assert page.validatePage() and wizard.llm_choice is None, "the agent's provider, unless chosen for this folder"
    to_folder(wizard, env / "two")  # the same folder again (Back, Next): the choice made for it stays
    page.own.setChecked(True)
    widgets.select_keys(page.servers, ["lm"])
    to_folder(wizard, env / "two")
    assert page.own.isChecked()


@needs_sai
def test_cloud_takes_the_projects_own_server_off_only_after_a_yes_and_never_a_presets(wizard, env, monkeypatch):
    """An existing project with --llm lm --no-provider-defaults; the Model page says cloud: the Start page asks
    before the server goes (declined: it stays, the page waits). A preset's own server is never stripped."""
    from sandbox_ai_app import wizard as module
    (env / "one" / ".sandbox-ai.json").write_text(json.dumps({"version": 1, "agent": "opencode",
                                                               "options": ["--llm", "lm", "--no-provider-defaults"]}))
    to_folder(wizard, env / "one")
    assert wizard.existing is not None and wizard.model_page.existing_choice["name"] == "lm"
    wizard.model_page.cloud.setChecked(True)
    assert wizard.model_page.validatePage() and wizard.model_page.dropped_existing()
    start = wizard.start_page
    start.initializePage()
    start.buttons["existing"].setChecked(True)
    questions = []
    monkeypatch.setattr(module, "confirm", lambda parent, title, text, accept, **kw: questions.append(text) or False)
    assert start.validatePage() is False
    assert "lm" in questions[-1] and "--no-provider-defaults" in questions[-1]
    monkeypatch.setattr(module, "confirm", lambda parent, title, text, accept, **kw: questions.append(text) or True)
    assert start.validatePage()
    argv = B.to_argv(wizard.sai, wizard.board)
    assert "--llm" not in argv and "--no-provider-defaults" not in argv
    # A preset that is local-only keeps its server (to fill in), no question.
    questions.clear()
    assert start.select_preset("local-llm")
    assert start.validatePage() and not questions
    argv = B.to_argv(wizard.sai, wizard.board)
    assert argv[argv.index("--llm") + 1] == "<llm:local>" and "--no-provider-defaults" in argv, \
        "the preset's server stays, to fill in"


# -- 3. the wizard is for this machine; the Board for a host --

@needs_sai
def test_the_wizard_says_it_is_for_this_machine_and_leads_to_the_board(env, monkeypatch):
    from sandbox_ai_app import wizard as module
    from sandbox_ai_app.board_view import BoardPage
    wizard = module.SandboxWizard(backend.SandboxAI(), lambda: {}, board_host="ct1")
    assert wizard.host() == "local"
    text = wizard.folder_page.where.text()
    assert "ct1" in text and "this machine" in text and wizard.folder_page.board_button.text() == \
        "Use the Board for a host"
    wanted = []
    wizard.board_wanted.connect(lambda: wanted.append(True))
    wizard.folder_page.board_button.click()
    assert wanted == [True]
    plain = module.SandboxWizard(backend.SandboxAI(), lambda: {})
    assert "another host" in plain.folder_page.where.text()
    page = BoardPage(backend.SandboxAI(), lambda *a, **k: None, lambda: {"board_grid": 20})
    page.board_for_host()
    assert "host" in page.notice.text() and page.notice.isVisibleTo(page)
    for widget in (wizard, plain, page):
        widget.deleteLater()


# -- 4. the Board on another host --

@pytest.fixture
def remote(env, monkeypatch):
    """A BoardPage with the host ct1 chosen; background runs at once unless the test holds it."""
    from sandbox_ai_app import board_view
    from sandbox_ai_app.board_view import BoardPage
    sai = backend.SandboxAI()
    monkeypatch.setattr(sai, "hosts_supported", lambda: True)
    monkeypatch.setattr(sai, "host_entries", lambda: [dict(h) for h in HOSTS])
    monkeypatch.setattr(sai, "host_terminal", lambda name, *args: ["ssh", "-tt", name, *args])
    held = []
    monkeypatch.setattr(board_view, "background", lambda function, *a, done, **k: held.append((function, a, done)))
    started = []
    page = BoardPage(sai, lambda argv, title, embedded=None, cwd=None: started.append(argv),
                     lambda: {"board_grid": 20})
    page.started, page.held = started, held
    assert page.host_choice.choose("ct1")
    page.host_changed()
    yield page
    page.deleteLater()


def run_held(page, name=None):
    """Run the held background calls (those of function NAME), in order; the others stay held."""
    keep = []
    for function, args, done in list(page.held):
        if name is None or getattr(function, "__name__", "") == name:
            try:
                done(function(*args), None)
            except Exception as exc:  # noqa: BLE001
                done(None, exc)
        else:
            keep.append((function, args, done))
    page.held[:] = keep


@needs_sai
def test_one_dry_run_over_ssh_at_a_time_and_the_last_edit_after_it(remote, monkeypatch):
    runs = []
    monkeypatch.setattr(remote.sai, "host_launch_options", lambda host, agent: {"--mode", "--no-config"})

    def host_dry_run(host, agent, argv):
        runs.append(argv)
        return {"on": host}, ""
    monkeypatch.setattr(remote.sai, "host_dry_run", host_dry_run)
    remote.held.clear()
    remote.check()
    remote.check()
    remote.check()
    dry = [h for h in remote.held if h[0] is host_dry_run]
    assert len(dry) == 1 and remote.check_pending, "one in flight, the edits meanwhile wait for it"
    assert remote.check_delay() >= 2000, "a longer pause after an edit for SSH"
    run_held(remote, "host_dry_run")
    assert not remote.check_running and not remote.check_pending and len(runs) == 1
    assert remote.check_timer.isActive(), "the board as it is now is checked after the one that ran"
    # An answer for another host is dropped.
    remote.check()
    assert remote.host_choice.choose("local")
    remote.host_changed()
    verdict = remote.verdict.text()
    run_held(remote, "host_dry_run")
    assert remote.verdict.text() == verdict or "accepts" not in remote.verdict.text()


@needs_sai
def test_a_hosts_options_are_read_again_after_a_failed_read(remote, monkeypatch):
    calls = []

    def fails(host, agent):
        calls.append(host)
        raise ValueError("ssh: timed out")
    monkeypatch.setattr(remote.sai, "host_launch_options", fails)
    monkeypatch.setattr(remote.sai, "host_dry_run", lambda host, agent, argv: ({}, ""))
    remote.held.clear()
    remote.check()
    run_held(remote)
    assert calls == ["ct1"] and remote.host_known[("ct1", "claude")] is None
    remote.check()
    run_held(remote)
    assert calls == ["ct1"], "not at every edit"
    remote.dry_run()  # on demand: at once
    run_held(remote, "fails")
    assert calls == ["ct1", "ct1"]
    from sandbox_ai_app import board_view
    remote.host_failed[("ct1", "claude")] -= board_view.HOST_RETRY + 1  # a while later
    monkeypatch.setattr(remote.sai, "host_launch_options", lambda host, agent: calls.append(host) or {"--mode"})
    remote.check()
    run_held(remote)
    assert calls[-1] == "ct1" and remote.host_known[("ct1", "claude")] == {"--mode"}


@needs_sai
def test_an_untrusted_board_starts_on_a_host_after_a_yes(remote, env, monkeypatch):
    """Apply (write and trust) is this machine's and disabled for a host: Start was a dead end. Now it asks
    (project-trust, Cancel the default) whether exactly the board shown may start there (--no-config)."""
    from sandbox_ai_app import board_view
    project = env / "project"
    project.mkdir()
    (project / ".sandbox-ai.json").write_text(json.dumps({"version": 1, "agent": "claude", "options": ["--tmux"]}))
    remote.held.clear()
    assert remote.host_choice.choose("local")
    remote.host_changed()
    assert remote.open_project(str(project))
    assert remote.untrusted
    assert remote.host_choice.choose("ct1")
    remote.host_changed()
    remote.host_known[("ct1", "claude")] = {o for a in remote.sai.parser_actions() for o in a.option_strings}
    asked, ask_first = recorder(False)
    monkeypatch.setattr(board_view, "ask_first", ask_first)
    assert remote.start(embedded=True) is False and remote.started == []
    assert asked[-1][0] == "project-trust" and "ct1" in asked[-1][2] and "--no-config" in asked[-1][2]
    asked, ask_first = recorder(True)
    monkeypatch.setattr(board_view, "ask_first", ask_first)
    assert remote.start(embedded=True)
    assert remote.started[-1][:3] == ["ssh", "-tt", "ct1"] and "--no-config" in remote.started[-1]
    assert not remote.apply_button.isEnabled()


# -- 5. the Hosts page's defaults: subnet and subnet6 --

@needs_sai
def test_the_defaults_editor_takes_subnets_and_asks_before_a_change(env, monkeypatch):
    import sandbox_ai_app.hosts_page as hp
    sai = backend.SandboxAI()
    if not sai.hosts_supported():
        pytest.skip("this sandbox-ai has no hosts.py")
    monkeypatch.setattr(hp, "background", lambda function, *args, done, **kw: done(function(*args), None))
    sai.host_add("ct1", "admin@192.0.2.10", 22, "", "")
    shown = {"path": "/home/admin/.config/sandbox-ai/defaults.json",
             "values": {"mode": "high", "subnet": "10.77.0.0/24"}, "telemetry_levels": ["off", "strict", "on"],
             "modes": list(hp.MODES), "canary_categories": ["credentials", "home", "workspace"],
             "canary_default": ["credentials", "home"]}
    asked = []

    def host_run(name, *args, timeout=120, input=None):
        asked.append((name, args))
        return 0, json.dumps(shown) if args[:2] == ("defaults", "show") else "ok", ""
    monkeypatch.setattr(sai, "host_run", host_run)
    questions = []

    def ask_first(parent, action, title, text, accept, detail=None, details=None):
        questions.append((action, detail))
        return False
    monkeypatch.setattr(hp, "ask_first", ask_first)
    page = hp.HostsPage(sai, lambda argv, title: None)
    page.table.clearSelection()
    page.table.selectRow(widgets.find_row(page.table, "ct1"))
    page.show()
    assert page.subnet.text() == "10.77.0.0/24" and page.subnet6.text() == ""
    page.subnet.setText("10.88.0.0/24")
    page.subnet6.setText("fd88::/64")
    page.save_defaults()
    action, detail = questions[-1]
    assert action == "defaults-lower" and detail["old"]["subnet"] == "10.77.0.0/24"
    assert detail["new"]["subnet"] == "10.88.0.0/24" and detail["new"]["subnet6"] == "fd88::/64"
    assert not [a for _, a in asked if a[:2] == ("defaults", "set")], "declined: nothing written"
    monkeypatch.setattr(hp, "ask_first", lambda *a, **k: True)
    page.save_defaults()
    assert ("ct1", ("defaults", "set", "subnet", '"10.88.0.0/24"')) in asked
    assert ("ct1", ("defaults", "set", "subnet6", '"fd88::/64"')) in asked
    page.subnet.setText("not a net")
    widgets.SHOWN.clear()
    page.save_defaults()
    assert widgets.SHOWN and "IPv4" in widgets.SHOWN[-1][1]
    page.subnet.setText("fd00::/64")  # IPv6 in the IPv4 field
    with pytest.raises(ValueError):
        page.chosen_defaults()
    page.subnet.setText("")  # sandbox-ai's default again: unset
    shown["values"] = {"subnet": "10.88.0.0/24"}
    page.load_defaults()
    page.subnet.setText("")
    asked.clear()
    page.save_defaults()
    assert ("ct1", ("defaults", "unset", "subnet")) in asked
    assert hp.subnet_value("10.77.0.1/24", 4) == "10.77.0.0/24"
    words = hp.security_steps_down({"subnet": "10.77.0.0/24"}, {"subnet": "10.88.0.0/24"})
    if any(key == "subnet" for key, *_ in backend.defaults_lowered({"subnet": "a"}, {"subnet": "b"})):
        assert words and "10.88.0.0/24" in words[0]
    page.close()
