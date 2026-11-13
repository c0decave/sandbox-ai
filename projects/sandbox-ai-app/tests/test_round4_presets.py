"""Review round 4 (B1-B5): one preset catalogue per host (sandbox-ai's presets of that host merged with the app's own
and yours, keyed by (source, id), sandbox-ai's preferred where ids collide) used by the wizard, the gallery, the fill-in
form and the Start page's preset chooser; the gallery and form read the board's host; the Start page's choosers and
hints follow its host; "Write and trust" and a new preset directory ask first; defaults_lowered as the web's rules."""
import json
import os

import pytest

from sandbox_ai_app import backend, presets as PR

needs_sai = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                               reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


@pytest.fixture
def sai(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    monkeypatch.setattr(PR, "HOST_DOCUMENTS", {})
    sai = backend.SandboxAI()
    sai.asked = []

    def host_run(name, *args, timeout=120, input=None):
        sai.asked.append((name, *args))
        return sai.answers.get(args[:2], (0, "", ""))
    sai.answers = {}
    monkeypatch.setattr(sai, "host_run", host_run)
    return sai


def published(sai):
    """This machine's sandbox-ai presets as `presets list --json` says them (what another host's would say)."""
    return [{k: v for k, v in d.items() if not k.startswith("_")} | {"origin": "built-in"}
            for d in PR.local_documents(sai)[0]]


def at_once(function, *args, done, **kw):
    try:
        result = function(*args)
    except Exception as exc:  # noqa: BLE001
        done(None, exc)
        return
    done(result, None)


# -- the catalogue (no Qt) --


@needs_sai
def test_one_catalogue_has_sandbox_ais_presets_and_the_apps_own_once(sai):
    catalogue = PR.catalogue(sai)
    ids = {p.name for p in catalogue.own()}
    if not {"ollama", "tor", "local-api"} <= ids:
        pytest.skip("this sandbox-ai has not these presets")
    assert {"ollama", "lm-studio", "llama-cpp", "vllm", "local-api", "tor", "code-audit"} <= ids
    keys = [p.key for p in catalogue.presets]
    assert len(keys) == len(set(keys)), "every preset once, by (source, id)"
    assert {"tor", "local-api"} <= set(catalogue.replaced), "sandbox-ai's of the same id wins"
    assert not any(p.source == "app" and p.id in ("tor", "local-api") for p in catalogue.presets)
    titles = [p.text(p.title) for p in catalogue.presets]
    assert titles.count("Everything through Tor") == 1, "not shown twice"
    for wanted in ("ollama", "sai-ollama", "sandbox-ai:sai-ollama", ("sandbox-ai", "sai-ollama")):
        assert catalogue.find(wanted).name == "ollama" and catalogue.find(wanted).source == "sandbox-ai"
    assert catalogue.find("tor").source == "sandbox-ai", "an app's id that sandbox-ai replaced: sandbox-ai's"
    assert catalogue.find("locked").source == "app" and catalogue.find("app:locked").id == "locked"
    with pytest.raises(ValueError):
        catalogue.find("app:tor")
    ollama = catalogue.find("ollama")
    assert ollama.value_names and all(PR.B.PLACEHOLDER.fullmatch(marker) for marker, _ in ollama.value_names)


@needs_sai
def test_a_host_without_sandbox_ais_presets_keeps_the_apps_own(sai):
    catalogue = PR.catalogue(sai, "ct1", documents=[])
    assert catalogue.find("tor").source == "app" and not catalogue.replaced and not catalogue.own()
    assert not PR.catalogue(sai, "ct1").complete, "nothing read from ct1 yet"


@needs_sai
def test_another_hosts_presets_come_from_presets_list_json_there(sai):
    documents = published(sai)
    extra = dict(documents[0], id="team-tool", title="Team tool", origin="extra", directory="/srv/presets",
                 rules="team-tool.rules.json")
    sai.answers[("presets", "list")] = (0, json.dumps(documents + [extra, {"id": "--bad"}]), "")
    found, problems = PR.host_documents(sai, "ct1")
    assert sai.asked[-1] == ("ct1", "presets", "list", "--json") and problems == []
    assert "--bad" not in {d["id"] for d in found}, "an id that would read as an option is dropped"
    assert PR.remembered("ct1")[0] == found
    catalogue = PR.catalogue(sai, "ct1")
    assert catalogue.complete and catalogue.host == "ct1"
    tool = catalogue.find("team-tool")
    assert tool.host == "ct1" and tool.rules is None
    notes = " ".join(tool.notes)
    assert "/srv/presets" in notes and "team-tool.rules.json" in notes and "ct1" in notes
    titles = [title for key, title, _ in catalogue.groups() if key.startswith(PR.SAI_CATEGORY)]
    assert titles and all(title.startswith("sandbox-ai on ct1: ") for title in titles), titles  # by its categories
    sai.answers[("presets", "show")] = (0, json.dumps(extra), "")
    assert sai.host_preset("ct1", "team-tool")["id"] == "team-tool"
    assert sai.asked[-1] == ("ct1", "presets", "show", "--json", "team-tool")
    with pytest.raises(ValueError):
        sai.host_preset("ct1", "--evil")


# -- defaults lowered: the web's rules (webui.lowered_default) --


def test_defaults_lowered_counts_a_new_preset_directory_and_any_subnet_change():
    lowered = backend.defaults_lowered
    assert lowered({"preset_dirs": ["/a"]}, {"preset_dirs": ["/a", "/b"]}) == [("preset_dirs", ["/a"], ["/b"])]
    assert lowered({}, {"preset_dirs": ["/b"]}) == [("preset_dirs", [], ["/b"])]
    assert lowered({"preset_dirs": ["/a", "/b"]}, {"preset_dirs": ["/a"]}) == [], "fewer places: not lowered"
    assert lowered({"preset_dirs": ["/a"]}, {}) == [], "unset: fewer places"
    assert lowered({}, {"subnet": "10.9.0.0/24"}) == [("subnet", None, "10.9.0.0/24")]
    assert lowered({"subnet6": "fd00::/64"}, {}) == [("subnet6", "fd00::/64", None)], "unset changes it too"
    assert lowered({"subnet": "10.9.0.0/24"}, {"subnet": "10.9.0.0/24"}) == []
    assert backend.needs_confirm("defaults-lower", {"old": {}, "new": {"subnet": "10.1.0.0/24"}})
    assert backend.needs_confirm("defaults-lower", {"old": {"subnet": "10.1.0.0/24"},
                                                    "new": {"subnet": "10.1.0.0/24"}}) is None


def test_options_a_host_does_not_know_are_named_not_dropped():
    options = [backend.Option("--workspace-ro", "", "flag", ""), backend.Option("--memory", "", "value", ""),
               backend.Option("--mode", "", "choice", "", choices=["low", "high"])]
    values = {"--workspace-ro": True, "--memory": "", "--mode": "high"}
    assert backend.dropped_options(options, values, {"--mode"}) == ["--workspace-ro"]
    assert backend.dropped_options(options, values, None) == [], "not known: nothing is said to be missing"


@needs_sai
def test_the_start_choices_of_a_host_come_from_its_lists(sai):
    sai.answers.update({
        ("vpn", "list"): (0, json.dumps([{"profile": "mv"}, {"profile": "ts"}]), ""),
        ("kits", "list"): (0, json.dumps({"kits": [{"name": "egm", "kind": "builtin"},
                                                   {"name": "base", "kind": "always"}]}), ""),
        ("identities", "list"): (0, json.dumps({"work": {"email": "w@example.org"}}), ""),
        ("mcp", "list"): (0, json.dumps([{"name": "fetch", "status": "stopped"}]), ""),
        ("images", "--json"): (0, json.dumps([{"target": "tor", "variant": True}, {"target": "claude"}]), ""),
        ("secrets", "list"): (0, "NAME     STORE\nGH_PUSH  file\n", ""),
    })
    found, problems = sai.host_start_choices("ct1")
    assert found == {"--vpn": ["mv", "ts"], "--kit": ["egm"], "--identity": ["work"], "--mcp": ["fetch"],
                     "--image-variant": ["tor"], "--secret-env": ["GH_PUSH"], "--api-key": ["GH_PUSH"],
                     "--git-token-secret": ["GH_PUSH"]} and problems == {}
    assert all(asked[0] == "ct1" for asked in sai.asked), "that host's lists, never this machine's"
    sai.answers[("vpn", "list")] = (1, "", "no vpn here")
    found, problems = sai.host_start_choices("ct1")
    assert "--vpn" not in found and "no vpn here" in problems["--vpn"] and found["--mcp"] == ["fetch"]


# -- Qt: the wizard, the gallery, the form, the Board, the Start page, the settings --


@pytest.fixture
def qt(qapp, sai, monkeypatch):
    from sandbox_ai_app import widgets
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    return sai


@needs_sai
def test_a_sandbox_ai_preset_picked_in_the_gallery_is_the_one_the_wizard_builds(qt, tmp_path, monkeypatch):
    from sandbox_ai_app import board as B, preset_gallery
    from sandbox_ai_app.wizard import SandboxWizard
    sai = qt
    wizard = SandboxWizard(sai, lambda: {"default_template": "locked"})
    (tmp_path / "project").mkdir()
    wizard.directory = str(tmp_path / "project")
    page = wizard.start_page
    page.initializePage()
    items = [page.preset.itemData(i) for i in range(page.preset.count())]
    ollama = page.catalogue.find("ollama") if "sandbox-ai:sai-ollama" in items else None
    if ollama is None:
        pytest.skip("this sandbox-ai has no ollama preset")
    assert "app:tor" not in items and "sandbox-ai:sai-tor" in items, "the wizard lists the same catalogue"

    class Gallery:
        DialogCode = preset_gallery.QDialog.DialogCode

        def __init__(self, *args, host="local", **kw):
            assert host == "local"
            self.result_ = (ollama, {marker: "ollama" for marker, _ in ollama.value_names})

        def exec(self):
            return self.DialogCode.Accepted
    monkeypatch.setattr(preset_gallery, "PresetGallery", Gallery)
    page.open_gallery()
    assert page.chosen_preset().key == ollama.key, "never another preset silently"
    assert page.validatePage()
    argv = B.to_argv(sai, wizard.board)
    assert "--llm" in argv and argv[argv.index("--llm") + 1].startswith("ollama")


@needs_sai
def test_the_gallery_and_form_of_a_remote_board_read_that_host(qt, monkeypatch):
    from sandbox_ai_app import preset_gallery, widgets
    sai = qt
    monkeypatch.setattr(preset_gallery, "background", at_once)
    sai.answers[("presets", "list")] = (0, json.dumps(published(sai)), "")
    for name in ("vpn_profiles", "llm_endpoints", "mcp_definitions"):
        monkeypatch.setattr(sai, name, lambda *a, _n=name: pytest.fail(f"this machine's {_n} read for a host"))
    read = []
    monkeypatch.setattr(sai, "host_vpn_profiles", lambda host: read.append(("vpn", host)) or [{"profile": "far"}])
    monkeypatch.setattr(sai, "host_llm_endpoints", lambda host: read.append(("llm", host)) or [])
    monkeypatch.setattr(sai, "host_mcp_servers", lambda host: read.append(("mcp", host)) or [("browser", "", "", "")])
    gallery = preset_gallery.PresetGallery(sai, None, "claude", {}, None, host="ct1")
    assert ("ct1", "presets", "list", "--json") in sai.asked
    own = [c.preset for c in gallery.cards if c.preset.source == "sandbox-ai"]
    if not own:
        pytest.skip("this sandbox-ai has no presets")
    assert all(p.host == "ct1" for p in own)
    keys = [c.preset.key for c in gallery.cards]
    assert len(keys) == len(set(keys)), "nothing twice"
    form = preset_gallery.PlaceholderForm(sai, PR.find("vpn-all"), gallery, agent="claude", host="ct1")
    assert ("vpn", "ct1") in read and "on ct1" in form.windowTitle()
    assert form.fields["<vpn:wireguard>"].findText("far") >= 0
    preset_gallery.PlaceholderForm(sai, PR.find("browser"), gallery, agent="claude", host="ct1")
    assert ("mcp", "ct1") in read
    llm = preset_gallery.PlaceholderForm(sai, PR.find("local-llm"), gallery, agent="claude", host="ct1")
    assert ("llm", "ct1") in read
    assert not any(b.text() == "Add a server…" for b in llm.findChildren(preset_gallery.QPushButton)), \
        "a server of this machine is not one of that host"
    widgets.SHOWN.clear()


@needs_sai
def test_deleting_an_own_preset_asks_first_and_only_ones_own(qt, monkeypatch, tmp_path):
    from sandbox_ai_app import preset_gallery
    sai = qt
    board = PR.build(sai, PR.find("locked"), str(tmp_path))
    gallery = preset_gallery.PresetGallery(sai, None, "claude", {}, board)
    saved = gallery.save_board("Mine")
    asked = []
    monkeypatch.setattr(preset_gallery, "ask_first", lambda parent, action, *a, **k: asked.append(action) or False)
    gallery.choose(next(c for c in gallery.cards if c.preset.key == saved.key))
    assert not gallery.delete_preset() and asked == ["preset-delete"]
    assert saved.id in {p.id for p in PR.load_mine(sai)[0]}, "declined: kept"
    own = next((c for c in gallery.cards if c.preset.source == "sandbox-ai"), None)
    if own is not None:
        gallery.choose(own)
        assert not gallery.delete_button.isEnabled() and not gallery.delete_preset(), "sandbox-ai's: not yours"


@needs_sai
def test_the_board_finds_a_preset_of_its_hosts_catalogue(qt):
    from sandbox_ai_app.board_view import BoardPage
    sai = qt
    page = BoardPage(sai, lambda *a, **k: None, lambda: {"board_grid": 20})
    catalogue = PR.catalogue(sai)
    if not catalogue.own():
        pytest.skip("this sandbox-ai has no presets")
    name = catalogue.own()[0].name
    board = page.new_from_preset(name)
    assert board is not None


@needs_sai
def test_the_start_page_follows_its_host(qt, monkeypatch):
    from sandbox_ai_app import ui
    sai = qt
    monkeypatch.setattr(ui, "background", at_once)
    monkeypatch.setattr(sai, "hosts_supported", lambda: True)
    monkeypatch.setattr(sai, "host_entries", lambda: [{"name": "local", "kind": "local"},
                                                      {"name": "ct1", "kind": "ssh", "ssh": "a@192.0.2.1"}])
    monkeypatch.setattr(sai, "host_launch_options", lambda name, agent: {
        o for action in sai.parser_actions() for o in action.option_strings})
    monkeypatch.setattr(sai, "host_llm_servers", lambda host: [])
    choices = {"ct1": ({"--vpn": ["far"], "--kit": ["egm"], "--identity": ["work"], "--mcp": ["fetch"],
                        "--image-variant": ["tor"]}, {})}
    monkeypatch.setattr(sai, "host_start_choices", lambda host: choices.get(host, ({}, {})))
    page = ui.LaunchPage(sai, lambda *a, **k: None)
    gpu = page.form.widgets["--gpu"]
    assert page.host_choice.choose("ct1")
    assert gpu.isEnabled() and [gpu.itemText(i) for i in range(gpu.count())] == ["", "nvidia", "dri", "rocm"], \
        "every kind for another host"
    assert "on ct1" in page.form.widgets["--memory"].placeholderText()
    assert "this computer" not in page.form.widgets["--cpus"].placeholderText()
    page.read_host_choices()
    vpn = page.form.widgets["--vpn"]
    assert vpn.isEditable() and vpn.findText("far") >= 0
    assert page.form.widgets["--image-variant"].findText("tor") >= 0
    assert page.form.widgets["--identity"].findText("work") >= 0
    mcp = page.form.widgets["--mcp"]
    assert [a.text() for a in mcp.pick.menu().actions()] == ["fetch"]
    mcp.pick.menu().actions()[0].trigger()
    assert mcp.value() == ["fetch"] and "--mcp" in page.argv()
    # A late answer for the host chosen before is dropped.
    token = page.choices_token
    page.host_choice.choose("local")
    page.host_choices_read(token, "ct1", choices["ct1"], None)
    assert page.form.widgets["--vpn"].findText("far") < 0
    assert "this computer" in page.form.widgets["--memory"].placeholderText() or \
        not page.form.widgets["--memory"].property("detected")


@needs_sai
def test_the_start_pages_preset_chooser_gives_preset_and_its_values(qt, monkeypatch):
    from sandbox_ai_app import ui
    sai = qt
    monkeypatch.setattr(ui, "background", at_once)
    monkeypatch.setattr(sai, "host_llm_servers", lambda host: [])
    monkeypatch.setattr(sai, "host_start_choices", lambda host: ({}, {}))
    page = ui.LaunchPage(sai, lambda *a, **k: None)
    page.refresh_presets()
    keys = [page.preset_choice.itemData(i) for i in range(page.preset_choice.count())]
    assert keys[0] == "" and "app:locked" in keys
    tor = next((p for p in page.preset_catalogue.own() if p.name == "tor"), None)
    if tor is None:
        pytest.skip("this sandbox-ai has no tor preset")
    marker = tor.value_names[0][0]
    assert page.apply_preset(tor, {marker: "anon"})
    values = page.form.values()
    assert values["--preset"] == "tor" and values["--preset-value"] == [f"{tor.value_names[0][1]}=anon"]
    assert "--preset" in page.argv() and "tor" in page.preview.toPlainText()
    assert page.apply_preset(PR.find("locked"), {})
    assert page.form.values()["--offline"] is True and not page.form.values()["--preset"]
    # Another host's sandbox-ai preset is read as it is there now (presets show NAME --json).
    shown = []
    monkeypatch.setattr(sai, "host_preset", lambda host, name: shown.append((host, name)) or
                        dict(PR.local_documents(sai)[0][0], id=name))
    monkeypatch.setattr(page, "host", lambda: "ct1")
    page.preset_catalogue = PR.catalogue(sai, "ct1", published(sai))
    from sandbox_ai_app.preset_gallery import fill_preset_combo
    fill_preset_combo(page.preset_choice, page.preset_catalogue, "sandbox-ai:sai-tor")
    page.preset_picked()
    assert shown == [("ct1", "tor")]


@needs_sai
def test_write_and_trust_on_the_start_page_asks_project_trust(qt, monkeypatch, tmp_path):
    from sandbox_ai_app import ui
    sai = qt
    page = ui.LaunchPage(sai, lambda *a, **k: None)
    page.form.widgets["--workspace"].set_value([str(tmp_path)])
    asked = []
    monkeypatch.setattr(ui, "ask_first", lambda parent, action, *a, **k: asked.append(action) or False)
    monkeypatch.setattr(sai, "write_project", lambda *a: pytest.fail("written without a yes"))
    page.write_project()
    assert asked == ["project-trust"]


@needs_sai
def test_adding_a_preset_directory_asks_defaults_lower(qt, monkeypatch, tmp_path):
    from sandbox_ai_app import settings
    sai = qt
    monkeypatch.setattr(settings, "background", lambda *a, **k: None)
    dirs = settings.PresetDirs(sai)
    dirs.entries = [{"path": "/srv/a"}]
    asked = []

    def ask_first(parent, action, title, text, accept, detail=None, details=None):
        asked.append((action, detail))
        return False
    monkeypatch.setattr(settings, "ask_first", ask_first)
    ran = []
    monkeypatch.setattr(dirs, "run", lambda action, path: ran.append((action, path)))
    assert not dirs.add_path(str(tmp_path))
    assert asked == [("defaults-lower", {"old": {"preset_dirs": ["/srv/a"]},
                                         "new": {"preset_dirs": ["/srv/a", str(tmp_path)]}})] and not ran
    assert backend.needs_confirm("defaults-lower", asked[0][1]), "a new directory lowers the defaults"


def test_the_webs_new_actions_are_asked_where_the_app_does_them():
    """vpn-replace (vpn add --force), llm-replace (llm add --replace), team-stop/-delete, image-file (a host file into
    an extension), image-remove (images remove: a variant or an extension): the web's table, asked by the page."""
    import ast
    from pathlib import Path
    package = Path(backend.__file__).parent
    wanted = {"vpn_page.py": {"vpn-replace"}, "llm_page.py": {"llm-replace"}, "team_page.py": {"team-stop", "team-delete"},
              "images_page.py": {"image-file", "image-remove"}}
    for name, actions in wanted.items():
        asked = {node.args[1].value for node in ast.walk(ast.parse((package / name).read_text()))
                 if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "ask_first" and len(node.args) > 1
                 and isinstance(node.args[1], ast.Constant)}
        assert actions <= asked, (name, actions - asked)
        assert actions <= set(backend.NEEDS_CONFIRM), "the web's own actions, in the same table"


# -- "Show value" beside every field where a secret's value is typed --


def test_no_field_hides_its_value_but_through_the_show_value_toggle():
    """Every echo mode of a secret's value is set by widgets.apply_echo (secret_value / mark_secret), never by hand."""
    import ast
    from pathlib import Path
    package = Path(backend.__file__).parent
    found = []
    for path in sorted(package.glob("*.py")):
        if path.name == "widgets.py":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Attribute) and node.attr == "Password":
                found.append(f"{path.name}:{node.lineno}")
    assert not found, "a Password echo outside widgets.secret_value: " + ", ".join(found)


@pytest.mark.parametrize("which", ["credentials", "llm", "vpn", "wizard-server", "wizard-git"])
def test_every_secret_value_field_has_show_value_on_by_default_and_it_switches(qt, monkeypatch, which):
    from PySide6.QtWidgets import QLineEdit
    from sandbox_ai_app import i18n, state, widgets
    monkeypatch.setattr(state, "_current", None)
    monkeypatch.setattr(widgets, "_values_shown", [True])
    sai = qt
    if which == "credentials":
        from sandbox_ai_app.credentials_page import CredentialsPage
        root = CredentialsPage(sai, lambda *a, **k: None)
    elif which == "llm":
        from sandbox_ai_app.llm_page import LlmDialog
        root = LlmDialog()
    elif which == "vpn":
        from sandbox_ai_app.vpn_page import VpnDialog
        root = VpnDialog()
    elif which == "wizard-server":
        from sandbox_ai_app.wizard import ServerDialog
        root = ServerDialog(sai, ())
    else:
        from sandbox_ai_app.wizard import SandboxWizard
        root = SandboxWizard(sai, lambda: {}).git_page
        root.add_row("https://example.org", "typed", "")
    fields = [e for e in root.findChildren(QLineEdit) if e.property("secret_value")]
    assert fields, "a field for a secret's value"
    for edit in fields:
        toggle = edit.show_toggle
        assert toggle.text() == "Show value" and toggle.accessibleName() == "Show value"
        assert toggle.isChecked() and edit.echoMode() == QLineEdit.EchoMode.Normal, "on by default: you see what you type"
        toggle.setChecked(False)
        assert edit.echoMode() == QLineEdit.EchoMode.Password and not widgets.values_shown()
        toggle.setChecked(True)
        assert edit.echoMode() == QLineEdit.EchoMode.Normal
    for edit in root.findChildren(QLineEdit):
        assert edit.echoMode() != QLineEdit.EchoMode.Password or getattr(edit, "show_toggle", None), \
            "a hidden field without its toggle"
    i18n.set_language("de")
    try:
        assert i18n.tr("Show value") == "Wert anzeigen"
    finally:
        i18n.set_language("en")


def test_the_show_value_choice_is_kept_in_the_apps_state_for_every_field(qt, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QLineEdit
    from sandbox_ai_app import state, widgets
    kept = state.State(tmp_path / "state.json")
    monkeypatch.setattr(state, "_current", kept)
    one, two = QLineEdit(), QLineEdit()
    box_one, box_two = widgets.secret_value(one), widgets.secret_value(two)
    assert one.show_toggle.isChecked()
    one.show_toggle.setChecked(False)
    assert kept.get(widgets.SHOW_VALUES, True, bool) is False, "remembered"
    assert not two.show_toggle.isChecked() and two.echoMode() == QLineEdit.EchoMode.Password, "one choice for all"
    three = QLineEdit()
    box_three = widgets.secret_value(three)  # noqa: F841 - kept: it holds the toggle
    assert not three.show_toggle.isChecked(), "a new field takes the choice kept"
    widgets.mark_secret(three, False)
    assert three.echoMode() == QLineEdit.EchoMode.Normal and not three.show_toggle.isEnabled(), "not a value: shown"
    one.show_toggle.setChecked(True)
    for widget in (box_one, box_two):
        widget.deleteLater()


# -- Git and publish logins: the token typed (kept as a secret first) or a stored secret --


def test_a_typed_token_is_kept_as_its_secret_first_then_the_login_names_it():
    S = backend.SandboxAI
    assert S.token_secret_name(kind="pypi") == "PYPI_TOKEN" and S.token_secret_name(kind="crates") == "CARGO_TOKEN"
    assert [S.token_secret_name(kind=k) for k in ("testpypi", "npm", "gitlab", "github")] == [
        "TESTPYPI_TOKEN", "NPM_TOKEN", "GITLAB_TOKEN", "GITHUB_TOKEN"]
    assert S.token_secret_name(url="https://git.example.org:3000/x") == "GIT_TOKEN_GIT_EXAMPLE_ORG"
    assert S.token_secret_name(url="") == "GIT_TOKEN"
    token = "pypi-" + "A" * 200  # longer than a name may be
    steps = S.login_steps("publish-logins", ["add", "pypi", "--secret", "PYPI_TOKEN"], "PYPI_TOKEN", token)
    assert steps == [(["secrets", "set", "PYPI_TOKEN"], token + "\n"),
                     (["publish-logins", "add", "pypi", "--secret", "PYPI_TOKEN"], None)]
    assert all(token not in args for args, _ in steps), "the token only on standard input"
    assert S.login_steps("git-logins", ["add", "--secret", "G", "--", "https://g"], "G") == [
        (["git-logins", "add", "--secret", "G", "--", "https://g"], None)], "a stored secret: the add alone"


@needs_sai
def test_add_login_keeps_the_secret_then_adds_and_stops_at_a_failure(sai):
    token = "glpat-" + "x" * 120
    sai.answers[("secrets", "set")] = (0, "kept", "")
    assert sai.add_login("ct1", "git-logins", ["add", "--secret", "GIT_TOKEN_G", "--", "https://g"], "GIT_TOKEN_G",
                         token)[0] == 0
    assert sai.asked == [("ct1", "secrets", "set", "GIT_TOKEN_G"),
                         ("ct1", "git-logins", "add", "--secret", "GIT_TOKEN_G", "--", "https://g")]
    sai.asked.clear()
    sai.answers[("secrets", "set")] = (1, "", "no keyring")
    status, _, err = sai.add_login("ct1", "publish-logins", ["add", "npm", "--secret", "NPM_TOKEN"], "NPM_TOKEN", token)
    assert status == 1 and "no keyring" in err and sai.asked == [("ct1", "secrets", "set", "NPM_TOKEN")], \
        "no login that names a secret that is not there"
    assert sai.add_login("ct1", "rm", [], "X", "v")[0] == 2


@needs_sai
def test_the_login_dialog_offers_a_typed_token_or_a_stored_secret(qt, monkeypatch):
    from PySide6.QtWidgets import QDialog, QLineEdit
    from sandbox_ai_app import credentials_page as C
    sai = qt
    box = C.PublishLoginsBox(sai, lambda: None)
    box.host = "ct1"
    box.show_entries([], ["NPM_TOKEN", "OTHER"], known=True)
    dialog = C.LoginDialog(box, "New publish login", box.secret_names, (("Own server", "url", ""),),
                           kinds=tuple(backend.SandboxAI.PUBLISH_HOSTS), host="ct1")
    assert "on ct1" in dialog.windowTitle() and dialog.typed.isChecked()
    assert dialog.secret.text() == "PYPI_TOKEN"
    dialog.kind.setCurrentText("crates")
    assert dialog.secret.text() == "CARGO_TOKEN", "follows the kind until typed by hand"
    assert dialog.value.maxLength() > 1000 and dialog.value.show_toggle.isChecked()
    assert dialog.value.echoMode() == QLineEdit.EchoMode.Normal
    from PySide6.QtWidgets import QLabel
    assert "secret name" in " ".join(lbl.text() for lbl in dialog.findChildren(QLabel))
    dialog.check_and_accept()
    assert dialog.result() != QDialog.DialogCode.Accepted and "Enter the token" in dialog.problem.text()
    long_token = "cio" + "z" * 300
    dialog.value.setText(long_token)
    dialog.check_and_accept()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.values()["value"] == long_token and dialog.values()["secret"] == "CARGO_TOKEN"
    dialog.stored.setChecked(True)
    assert not dialog.form.isRowVisible(dialog.value_row) and dialog.form.isRowVisible(dialog.secret_choice)
    assert dialog.secret_choice.offered() == ["NPM_TOKEN", "OTHER"]
    assert dialog.values()["value"] is None and dialog.values()["secret"] == "NPM_TOKEN"
    # The box: a typed token whose name is kept already is asked about first; declined, nothing runs.
    asked, ran = [], []
    monkeypatch.setattr(C, "ask_first", lambda parent, action, *a, detail=None, **k: asked.append((action, detail))
                        or False)
    monkeypatch.setattr(sai, "add_login", lambda *a: ran.append(a) or (0, "", ""))
    assert not box.add_with_token(["add", "npm", "--secret", "NPM_TOKEN"], {"secret": "NPM_TOKEN", "value": "t"})
    assert asked == [("secret-replace", {"exists": True})] and not ran
    box.show_entries([], [], known=False)
    box.add_with_token(["add", "npm", "--secret", "NPM_TOKEN"], {"secret": "NPM_TOKEN", "value": "t"})
    assert asked[-1] == ("secret-replace", {"exists": None}), "not known: asked"
    assert backend.needs_confirm("secret-replace", {"exists": None})


# -- the project tabs: only where the project has an effect --


@needs_sai
def test_the_project_tabs_show_only_on_the_pages_the_project_changes(qt, tmp_path, monkeypatch):
    from sandbox_ai_app import terminal, ui
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    monkeypatch.setattr(terminal, "available", lambda web=None: (False, "not in this test"))
    window = ui.MainWindow(qt, check_updates=False, approvals_interval=0)
    if window.tray is not None:
        window.tray.hide()
        window.tray = None
    window.show()
    project = tmp_path / "project"
    project.mkdir()
    applied = []
    monkeypatch.setattr(window.team, "set_project", lambda path: applied.append(path))
    window.apply_project(str(project))
    assert applied == [str(project)], "Teams follows the project (a new team is offered its workspace)"
    shown = []
    height = window.tabs.parentWidget().height()
    for key, _title in ui.PAGES:
        window.show_page(key, raise_window=False)
        if window.tabs.isVisibleTo(window):
            shown.append(key)
        assert window.add_tab_button.isVisibleTo(window) == window.tabs.isVisibleTo(window)
        assert window.tabs.parentWidget().height() == height, "the strip keeps its height: nothing below moves"
    assert shown == ["launch", "board", "sandboxes", "approvals", "files", "kits", "team", "connections",
                     "statistics"]
    assert set(ui.PROJECT_TAB_PAGES) == set(shown)
    window.shutdown()


# -- file and folder pickers list hidden entries (.claude, .config) --


def test_the_pickers_list_hidden_folders_and_take_a_typed_dot_path(qapp, tmp_path, monkeypatch):
    from PySide6.QtCore import QDir
    from PySide6.QtWidgets import QFileDialog, QFileSystemModel
    from sandbox_ai_app import state, widgets
    monkeypatch.setattr(widgets, "AUTO_ANSWER", None)
    hidden = tmp_path / ".claude"
    (hidden / "skills").mkdir(parents=True)
    (tmp_path / ".config.json").write_text("{}")
    dialog = state.file_dialog(None, "Folder", str(tmp_path), "folder")
    assert dialog.filter() & QDir.Filter.Hidden and dialog.testOption(QFileDialog.Option.DontUseNativeDialog)
    model = dialog.findChild(QFileSystemModel)
    assert model is not None and model.filter() & QDir.Filter.Hidden
    root = model.index(str(tmp_path))
    from conftest import wait_until
    assert wait_until(qapp, lambda: model.rowCount(root) > 0 or (model.fetchMore(root) or False), 10)
    names = {model.fileName(model.index(row, 0, root)) for row in range(model.rowCount(root))}
    assert ".claude" in names, "a hidden folder is listed"
    dialog.selectFile(str(hidden))
    assert dialog.selectedFiles() == [str(hidden)], "a typed path with a leading dot is taken"
    dialog.deleteLater()
    files = state.file_dialog(None, "File", str(tmp_path), "file")
    files.selectFile(str(tmp_path / ".config.json"))
    assert files.selectedFiles() == [str(tmp_path / ".config.json")]
    files.deleteLater()
    # Through the helpers: what the dialog gives back, and the folder kept for the next one.
    kept = state.State(tmp_path / "state.json")
    monkeypatch.setattr(state, "_current", kept)
    seen = []

    def run(dialog):
        seen.append((dialog.filter() & QDir.Filter.Hidden, dialog.fileMode()))
        return [str(hidden)]
    monkeypatch.setattr(state, "run_dialog", run)
    assert state.choose_folder(None, "Skill", "skill") == str(hidden)
    assert seen[-1] == (QDir.Filter.Hidden, QFileDialog.FileMode.Directory) and kept.folder("skill") == str(hidden)
    assert state.choose_save(None, "Export", "x.json", key="export") == str(hidden)
