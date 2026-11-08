"""Starts on another host and the rest of the command line's start features in the app: the words of every command
(non-Qt: a start, its dry run and options on a host, resume, stop --wait, mcp add/start, net keep, privacy-check,
update, demo-device, hosts add), then the dialogs and pages offscreen against stand-ins that record what they were
asked (nothing reaches a host, nothing starts)."""
import json
import os
import shlex

import pytest

from sandbox_ai_app import backend

needs_sai = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                               reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")

HELP = """usage: sandbox-ai claude|codex|opencode [options] [-- agent arguments]

Security mode:
  -mo, --mode MODE                    Security mode; in lax every destination (--egress-all-allow) is open
  -w DIR, --workspace DIR             A folder (repeatable)
  --gpu KIND                          A GPU
  -h, --help                          show this help message and exit
                                      --not-an-option (a help text's continuation line that starts with one)
"""


# -- the words of the commands (no Qt) --


def test_a_hosts_launcher_options_come_from_its_option_lines():
    assert backend.help_options(HELP) == {"--mode", "--workspace", "--gpu", "--help"}


def test_options_a_host_does_not_know_are_left_out_or_named():
    options = [backend.Option("--mode", "", "choice", "", choices=["low", "high"]),
               backend.Option("--workspace", "", "list", ""), backend.Option("--gpu", "", "value", "")]
    values = {"--mode": "high", "--workspace": ["/srv/x"], "--gpu": "nvidia"}
    assert backend.argv_for_host(options, values) == ["--mode", "high", "--workspace", "/srv/x", "--gpu", "nvidia"]
    assert backend.argv_for_host(options, values, {"--mode", "--workspace"}) == ["--mode", "high", "--workspace",
                                                                                   "/srv/x"]
    argv = ["--mode", "--gpu", "--gpu=amd", "--workspace", "/srv/x", "--new", "--", "--gpu"]
    assert backend.unknown_options(options, argv, {"--mode", "--workspace"}) == ["--gpu", "--new"]


def test_resume_stop_and_the_mcp_commands_are_checked():
    assert backend.resume_args("api", True, ["/srv/data"], [("/srv/a", "/mnt/a", "rw")], ["fetch"]) == [
        "resume", "--workspace=/srv/data", "--mount=/srv/a:/mnt/a:rw", "--mcp=fetch", "--restart", "api"]
    assert backend.resume_args("api") == ["resume", "api"]
    for bad in ({"workspaces": ["rel/dir"]}, {"workspaces": ["/srv/../etc"]}, {"mounts": [("/a", "/b", "x")]},
                {"mounts": [("/a:b", "/b", "ro")]}, {"mcp": ["--evil"]}):
        with pytest.raises(ValueError):
            backend.resume_args("api", **bad)
    with pytest.raises(ValueError):
        backend.resume_args("-x")
    assert backend.stop_args("api", 90) == ["stop", "api", "--wait=90"] and backend.stop_args("api") == ["stop", "api"]
    with pytest.raises(ValueError):
        backend.stop_args("api", 0)
    args, writable = backend.mcp_add_args("hub:binwalk", "bw", [("/data", "/srv/fw", "rw"), ("/in", "/srv/in", "ro")],
                                          ["TOKEN"], "none", "1g", 64, ["https://example.org/c.json"], True, "bwrap",
                                          True, "mv")
    assert args == ["mcp", "add", "--as=bw", "--mount=/data=/srv/fw:rw", "--mount=/in=/srv/in:ro", "--env=TOKEN",
                    "--network=none", "--memory=1g", "--pids=64", "--catalog=https://example.org/c.json", "--no-pull",
                    "--backend=bwrap", "--vpn=mv", "--unsigned", "hub:binwalk"] and writable == ["/srv/fw"]
    for bad in ({"entry": "--x"}, {"engine": "docker"}, {"vpn": "Bad Name"}, {"catalogs": ["relative.json"]},
                {"env": ["1X"]}, {"memory": "lots"}):
        with pytest.raises(ValueError):
            backend.mcp_add_args(**{"entry": "fetch", **bad})
    assert backend.mcp_start_args("fetch", "podman", "mv", "api", True) == [
        "mcp", "start", "--backend=podman", "--vpn=mv", "--for=api", "--json", "--restart", "fetch"]
    with pytest.raises(ValueError):
        backend.mcp_start_args("fetch", restart=True)  # a restart goes with a start


def test_privacy_update_and_demo_device_words():
    assert backend.privacy_check_args(["/srv/repo"], "/srv/rules", "web", True) == [
        "privacy-check", "--json", "--rules-dir=/srv/rules", "--project=web", "--all", "--", "/srv/repo"]
    with pytest.raises(ValueError):
        backend.privacy_check_args([])
    with pytest.raises(ValueError):
        backend.privacy_check_args(["/srv/repo"], project="../x")
    assert backend.update_args("0.1.76") == ["update", "--version=0.1.76"] and backend.update_args() == ["update"]
    with pytest.raises(ValueError):
        backend.update_args("latest")
    assert backend.demo_device_args("start", 4100) == ["demo-device", "start", "--port=4100"]
    assert backend.demo_device_args("stop") == ["demo-device", "stop"]
    with pytest.raises(ValueError):
        backend.demo_device_args("start", 80)


@pytest.fixture
def sai(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    sai = backend.SandboxAI()
    sai.asked = []

    def host_run(name, *args, timeout=120, input=None):
        sai.asked.append((name, *args))
        return sai.answers.get(args[:2], (0, "", ""))
    sai.answers = {}
    monkeypatch.setattr(sai, "host_run", host_run)
    monkeypatch.setattr(sai, "host_terminal", lambda name, *args: ["ssh", "-tt", name, shlex.join(args)])
    monkeypatch.setattr(sai, "host_statuses", lambda refresh=False, cached=True: {"ct1": {"version": "0.1.78"}})
    return sai


@needs_sai
def test_a_start_its_dry_run_and_options_on_a_host(sai):
    assert sai.host_launch_argv("local", "claude", ["--mode", "high"]) == sai.launch_argv("claude", ["--mode", "high"])
    assert sai.host_launch_argv("ct1", "claude", ["--workspace", "/srv/x"], ["-p", "hi"], "api") == [
        "ssh", "-tt", "ct1", "claude --workspace /srv/x --session api -- -p hi"]
    with pytest.raises(ValueError):
        sai.host_launch_argv("ct1", "claude", [], session="--x")
    sai.answers[("claude", "--workspace")] = (0, json.dumps({"backend": "podman"}), "note")
    assert sai.host_dry_run("ct1", "claude", ["--workspace", "/srv/x"]) == ({"backend": "podman"}, "note")
    assert sai.asked[-1] == ("ct1", "claude", "--workspace", "/srv/x", "--dry-run")
    sai.answers[("claude", "--help")] = (0, HELP, "")
    assert sai.host_launch_options("ct1", "claude") == {"--mode", "--workspace", "--gpu", "--help"}
    sai.host_launch_options("ct1", "claude")
    assert sum(1 for asked in sai.asked if asked[1:] == ("claude", "--help")) == 1, "kept per host and version"
    assert "--workspace" in sai.host_launch_options("local", "claude")


@needs_sai
def test_net_keep_privacy_check_doctor_and_the_mcp_catalog_on_a_host(sai):
    sai.net_keep("ct1", "api", every=True)
    assert sai.asked[-1] == ("ct1", "net", "show", "api", "--keep-all")
    sai.answers[("privacy-check", "--json")] = (1, json.dumps([{"repository": "/srv/r", "findings": [
        {"where": "diff", "path": "a.env", "id": "token", "severity": "block", "reason": "a token", "line": "x"}]}]),
        "sandbox-ai privacy-check: REFUSED: /srv/other is not a Git repository\n")
    results, problems = sai.privacy_check("ct1", ["/srv/r", "/srv/other"])
    assert results[0]["findings"][0]["id"] == "token" and "REFUSED" in problems[0]
    sai.answers[("doctor", "--backend=bwrap")] = (0, json.dumps([{"name": "bwrap", "status": "ok"}]), "")
    assert sai.host_doctor("ct1", "bwrap") == [{"name": "bwrap", "status": "ok"}]
    sai.answers[("mcp", "catalog")] = (0, "hub:binwalk  [none]  binwalk  unsigned\n"
                                          "hub:crtsh  [allowlist crt.sh:443]  crt.sh  (not published yet)  signed\n", "")
    entries, problems = sai.host_mcp_catalog("ct1", ["/srv/catalog.json"])
    assert sai.asked[-1] == ("ct1", "mcp", "catalog", "--catalog=/srv/catalog.json") and not problems
    assert entries == [("hub", {"name": "binwalk", "network": "none", "hosts": [], "title": "binwalk", "digest":
                                "published", "_signature": {"state": "unsigned"}, "_listed": True}),
                       ("hub", {"name": "crtsh", "network": "allowlist", "hosts": ["crt.sh:443"], "title": "crt.sh",
                                "digest": None, "_signature": {"state": "good"}, "_listed": True})]
    sai.answers[("mcp", "list")] = (0, json.dumps([{"name": "demo", "status": "running", "command": "python3 x",
                                                    "serves": ["api"]}, {"name": "--bad"}]), "")
    assert sai.host_mcp_servers("ct1") == [("demo", "running", "api", "python3 x")]


@needs_sai
def test_hosts_add_with_its_command_and_sudo_and_profiles_with_their_host(sai, monkeypatch):
    said = []
    monkeypatch.setattr(sai, "checked", lambda *args, timeout=120: said.append(args) or "")
    sai.host_add("ct1", "admin@192.0.2.10", 22, "", "", command="/opt/sai/bin/sandbox-ai", update_sudo=True)
    assert said[-1][-2:] == ("--command=/opt/sai/bin/sandbox-ai", "--update-sudo")
    with pytest.raises(ValueError):
        sai.host_add("ct1", "admin@192.0.2.10", command="sandbox-ai")
    sai.save_profile("far", "claude", ["--mode", "high"], host="ct1")
    sai.save_profile("near", "claude", [], host="local")
    assert sai.profiles()["far"]["host"] == "ct1" and "host" not in sai.profiles()["near"]


# -- the pages (offscreen) --


QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import widgets  # noqa: E402

HOSTS = [{"name": "local", "kind": "local"}, {"name": "ct1", "kind": "ssh", "ssh": "admin@192.0.2.10"}]


def at_once(function, *args, done, **kw):
    """widgets.background, but on this thread: DONE gets the result or the error at once."""
    try:
        result = function(*args)
    except Exception as exc:  # noqa: BLE001 - as background hands it on
        done(None, exc)
        return
    done(result, None)


def ran(sai, *words):
    """The last command asked of a host that begins with WORDS (host first)."""
    return next(asked for asked in reversed(sai.asked) if asked[:len(words)] == words)


@pytest.fixture
def ui(qapp, sai, monkeypatch):
    from sandbox_ai_app import board_view, preset_gallery, ui as module
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    asked = []

    def ask_first(parent, action, title, text, accept, detail=None, details=None):
        asked.append((action, text))
        return True
    for target in (module, board_view):
        monkeypatch.setattr(target, "ask_first", ask_first)
    for target in (module, board_view, preset_gallery):
        monkeypatch.setattr(target, "background", at_once)
    monkeypatch.setattr(sai, "hosts_supported", lambda: True)
    monkeypatch.setattr(sai, "host_entries", lambda: [dict(h) for h in HOSTS])
    module.asked, module.sai = asked, sai
    return module


def chosen(combo, host):
    assert combo.choose(host), [combo.itemData(i) for i in range(combo.count())]


@needs_sai
def test_the_start_page_starts_on_a_host_with_its_launchers_options(ui, sai, monkeypatch):
    started = []
    page = ui.LaunchPage(sai, lambda argv, title, embedded=None, cwd=None: started.append((argv, title, cwd)))
    monkeypatch.setattr(sai, "host_launch_options", lambda name, agent: {
        long for long in page.form.widgets if long != "--memory"})
    page.set_project("/home/me/project")
    assert page.form.widgets["--workspace"].value() == ["/home/me/project"]
    assert page.host_choice.isVisibleTo(page) and page.host_choice.current() == "local"
    chosen(page.host_choice, "ct1")
    assert page.form.widgets["--workspace"].value() == [], "this machine's folder is no folder there"
    memory = page.form.widgets["--memory"]
    assert not memory.isEnabled() and "does not know --memory" in memory.toolTip()
    assert not page.form.widgets["--workspace"].add_button.isEnabled(), "that host's paths: typed in"
    assert not page.write_button.isEnabled() and "ct1" in page.host_note.text()
    page.form.widgets["--workspace"].set_value(["/srv/work"])
    memory.setText("8g")
    assert "--memory" not in page.argv() and page.argv()[:2] == ["--workspace", "/srv/work"]
    assert page.preview.toPlainText().startswith("sandbox-ai hosts run ct1 -- claude --workspace /srv/work")
    page.start_in_app()
    assert started[-1] == (["ssh", "-tt", "ct1", "claude --workspace /srv/work"], "ct1: claude", None)
    monkeypatch.setattr(sai, "host_dry_run", lambda host, agent, argv: ({"host": host, "argv": argv}, ""))
    page.dry_run()
    assert '"host": "ct1"' in widgets.SHOWN[-1][1]
    monkeypatch.setattr(ui.QInputDialog, "getText", lambda *a, **k: ("far", True))
    page.save_profile()
    assert sai.profiles()["far"]["host"] == "ct1"
    chosen(page.host_choice, "local")
    assert page.form.widgets["--workspace"].value() == ["/srv/work"] and memory.isEnabled()
    assert "--memory" in page.argv(), "this machine's launcher knows it"
    page.profile.setCurrentText("far")
    page.load_profile()
    assert page.host() == "ct1", "a profile saved for a host starts there again"


@needs_sai
def test_resume_with_and_stop_with_wait_on_a_host(ui, sai, monkeypatch):
    page = ui.SandboxesPage(sai, lambda *a, **k: None)
    instance = backend.Instance("session", "remote:ct1:api", "api", host="ct1", state="running", agent="claude")

    def fill(dialog):
        dialog.workspaces.set_value(["/srv/more"])
        dialog.mounts.setPlainText("/srv/data:/mnt/data:rw\n/srv/ro:/mnt/ro")
        dialog.servers.setText("fetch")
        assert dialog.restart.isChecked(), "a running start is restarted for it"
        assert not dialog.workspaces.add_button.isEnabled()
        return QtWidgets.QDialog.DialogCode.Accepted
    monkeypatch.setattr(ui.ResumeDialog, "exec", fill)
    page.resume_with(instance)
    assert ran(sai, "ct1", "resume") == ("ct1", "resume", "--workspace=/srv/more", "--mount=/srv/data:/mnt/data:rw",
                             "--mount=/srv/ro:/mnt/ro:ro", "--mcp=fetch", "--restart", "api")
    assert [action for action, _ in ui.asked] == ["start-restart", "workspace-writable", "mcp-attach"]
    assert all(text.startswith("On ct1: ") for _, text in ui.asked)
    page.stop_wait.setValue(120)
    page.stop(instance)
    assert ran(sai, "ct1", "stop") == ("ct1", "stop", "api", "--wait=120") and ui.asked[-1][0] == "start-stop"
    page.stop_wait.setValue(30)
    page.stop(instance)
    assert ran(sai, "ct1", "stop") == ("ct1", "stop", "api"), "sandbox-ai's own 30 s: no --wait"


@needs_sai
def test_the_network_dialog_takes_nameservers_and_keeps_for_later_starts(ui, sai, monkeypatch):
    dialog = ui.NetworkDialog(None, {}, "Allow", {"--allow-host", "--dns", "--nameserver", "--keep"})
    dialog.nameservers.setText("192.0.2.53 192.0.2.54")
    dialog.keep.setChecked(True)
    assert dialog.arguments() == ["--nameserver", "192.0.2.53", "--nameserver", "192.0.2.54"] and dialog.kept()
    older = ui.NetworkDialog(None, {}, "Allow", {"--allow-host"})
    older.keep.setChecked(True)
    assert not older.kept(), "a sandbox-ai without --keep is not asked to keep"
    page = ui.SandboxesPage(sai, lambda *a, **k: None)
    allowed = ui.AllowedDialog(None, "api", "", lambda: None, keep=lambda every: page.keep_network("ct1", "api", every))
    assert allowed.keep_button.isVisibleTo(allowed) and allowed.keep_all_button.isVisibleTo(allowed)
    allowed.keep_all_button.click()
    assert sai.asked[-1] == ("ct1", "net", "show", "api", "--keep-all") and ui.asked[-1][0] == "net-keep"
    allowed.keep_button.click()
    assert sai.asked[-1] == ("ct1", "net", "show", "api", "--keep")
    assert not ui.AllowedDialog(None, "api", "", lambda: None).keep_button.isVisibleTo(allowed)
    monkeypatch.setattr(ui.NetworkDialog, "exec", lambda self: (self.hosts.setText("pypi.org"), self.keep.setChecked(
        True), QtWidgets.QDialog.DialogCode.Accepted)[-1])
    page.network_dialog("ct1", "api", "allow", "Allow", {"--allow-host", "--keep"}, None)
    assert ran(sai, "ct1", "net", "allow") == ("ct1", "net", "allow", "api", "--allow-host", "pypi.org", "--keep")
    assert ui.asked[-1][0] == "net-allow" and "later starts" in ui.asked[-1][1]


@needs_sai
def test_mcp_servers_on_a_host_listed_started_and_added_from_its_catalog(ui, sai, monkeypatch):
    sai.answers[("mcp", "list")] = (0, json.dumps([{"name": "demo", "status": "stopped", "command": "x"}]), "")
    sai.answers[("mcp", "catalog")] = (0, "hub:binwalk  [none]  binwalk  unsigned\n", "")
    sai.answers[("list", "--json")] = (0, json.dumps([{"name": "api", "kind": "session", "state": "running",
                                                       "agent": "claude"}]), "")
    page = ui.McpPage(sai)
    chosen(page.host_choice, "ct1")
    assert page.servers.item(0, 0).text() == "demo"
    page.servers.selectRow(0)

    def start(dialog):
        dialog.engine.setCurrentIndex(dialog.engine.findData("bwrap"))
        dialog.vpn.setText("mv")
        dialog.start.setCurrentIndex(dialog.start.findData("api"))
        dialog.restart.setChecked(True)
        return QtWidgets.QDialog.DialogCode.Accepted
    monkeypatch.setattr(ui.McpStartDialog, "exec", start)
    page.server("start")
    assert ran(sai, "ct1", "mcp", "start") == ("ct1", "mcp", "start", "--backend=bwrap", "--vpn=mv", "--for=api", "--json", "--restart",
                             "demo") and ui.asked[-1][0] == "start-restart"
    page.catalog_source.setText("/srv/catalog.json")
    page.load_catalog()
    assert sai.asked[-1] == ("ct1", "mcp", "catalog", "--catalog=/srv/catalog.json")
    page.catalog.selectRow(0)

    def add(dialog):
        assert dialog.unsigned.isVisibleTo(dialog) and dialog.mount_lines is not None
        dialog.unsigned.setChecked(True)
        dialog.no_pull.setChecked(True)
        dialog.mount_lines.setPlainText("/data=/srv/fw:rw")
        return QtWidgets.QDialog.DialogCode.Accepted
    monkeypatch.setattr(ui.AddServerDialog, "exec", add)
    page.add()
    assert ran(sai, "ct1", "mcp", "add") == ("ct1", "mcp", "add", "--as=binwalk", "--mount=/data=/srv/fw:rw", "--network=none",
                             "--memory=512m", "--pids=128", "--catalog=/srv/catalog.json", "--no-pull", "--unsigned",
                             "hub:binwalk")
    assert [action for action, _ in ui.asked[-2:]] == ["mcp-unsigned", "mcp-writable-mount"]


@needs_sai
def test_privacy_check_version_update_and_demo_device_on_the_system_page(ui, sai, monkeypatch):
    terminals = []
    page = ui.SystemPage(sai, lambda argv, title: terminals.append(argv))

    def fill(dialog):
        chosen(dialog.host_choice, "ct1")
        assert not dialog.paths.add_button.isEnabled()
        dialog.paths.set_value(["/srv/repo"])
        dialog.project.setText("web")
        dialog.every.setChecked(True)
        return QtWidgets.QDialog.DialogCode.Accepted
    monkeypatch.setattr(ui.PrivacyDialog, "exec", fill)
    sai.answers[("privacy-check", "--json")] = (1, json.dumps([{"repository": "/srv/repo", "scanned": 3, "findings": [
        {"where": "staged", "path": ".env", "id": "aws-key", "severity": "block", "reason": "an AWS key",
         "line": "AKIA…"}]}]), "")
    page.privacy()
    assert sai.asked[-1] == ("ct1", "privacy-check", "--json", "--project=web", "--all", "--", "/srv/repo")
    title, text, _ = widgets.SHOWN[-1]
    assert title == "Privacy check on ct1: /srv/repo" and "aws-key" in text and "block" in text
    results = ui.PrivacyResults(None, title, [{"repository": "/r", "scanned": 2, "findings": []}], ["refused"])
    assert results.table.rowCount() == 1 and "nothing found" in results.table.item(0, 5).text()
    page.version_details()
    assert sai.asked[-1] == ("local", "version", "--verbose")
    page.update_version.setText("0.1.76")
    page.update_sandbox_ai()
    assert terminals[-1][-2:] == ["update", "--version=0.1.76"]
    page.update_version.setText("newest")
    page.update_sandbox_ai()
    assert len(terminals) == 1, "not a version: nothing runs"
    chosen(page.demo_host, "ct1")
    page.demo_port.setValue(4100)
    page.demo_device("start")
    assert sai.asked[-1] == ("ct1", "demo-device", "start", "--port=4100")
    page.demo_device("status")
    assert sai.asked[-1] == ("ct1", "demo-device", "status")


@needs_sai
def test_the_board_starts_on_a_host_and_the_gallery_starts_the_emulated_device(ui, sai, monkeypatch):
    from sandbox_ai_app import presets as PR
    from sandbox_ai_app.board_view import BoardPage
    from sandbox_ai_app.preset_gallery import PresetGallery, needs_demo_device
    started = []
    page = BoardPage(sai, lambda argv, title, embedded=None, cwd=None: started.append((argv, title, cwd)),
                     lambda: {"board_grid": 20})
    monkeypatch.setattr(sai, "host_launch_options", lambda name, agent: {"--mode", "--no-config", "--session"})
    monkeypatch.setattr(sai, "host_dry_run", lambda host, agent, argv: ({"on": host}, ""))
    chosen(page.host_choice, "ct1")
    page.check()
    assert not page.apply_button.isEnabled() and "ct1" in page.host_note.text()
    page.start(embedded=True)
    if started:  # the board's options are all known there
        assert started[-1][0][:3] == ["ssh", "-tt", "ct1"] and started[-1][2] is None
    else:  # an option that host's launcher does not know: said, not started
        assert any("does not know these options" in text for _, text, *_ in widgets.SHOWN)
    monkeypatch.setattr(sai, "host_launch_options", lambda name, agent: backend.help_options(HELP) | {
        option for action in sai.parser_actions() for option in action.option_strings})
    page.host_known.clear()
    page.check()
    page.start(embedded=True)
    assert started and started[-1][0][:3] == ["ssh", "-tt", "ct1"] and started[-1][1].startswith("ct1: ")
    own, _ = PR.sandbox_ai_presets(sai)
    hardware = next((p for p in own if p.id == "sai-hardware-analysis"), None)
    if hardware is None:
        pytest.skip("this sandbox-ai has no hardware-analysis preset")
    assert needs_demo_device(hardware) and not needs_demo_device(PR.find("development"))
    gallery = PresetGallery(sai, None, "claude", {}, None, host="ct1")
    card = next(c for c in gallery.cards if c.preset.id == "sai-hardware-analysis")
    gallery.choose(card)
    assert gallery.demo_button.isVisibleTo(gallery)
    gallery.demo_button.click()
    assert sai.asked[-1] == ("ct1", "demo-device", "start") and "ct1" in gallery.demo_status.text()
    gallery.choose(next(c for c in gallery.cards if c.preset.id == "development"))
    assert not gallery.demo_button.isVisibleTo(gallery)


@needs_sai
def test_the_hosts_page_adds_with_command_and_sudo_and_checks_a_backend(ui, sai, monkeypatch):
    from sandbox_ai_app import hosts_page
    monkeypatch.setattr(hosts_page, "background", at_once)
    added = []
    monkeypatch.setattr(sai, "host_add", lambda *args, **kw: added.append((args, kw)) or "")
    page = hosts_page.HostsPage(sai, lambda *a, **k: None)

    def fill(dialog):
        dialog.name.setText("ct2")
        dialog.ssh.setText("admin@192.0.2.11")
        dialog.command.setText("/opt/sai/bin/sandbox-ai")
        dialog.update_sudo.setChecked(True)
        return QtWidgets.QDialog.DialogCode.Accepted
    monkeypatch.setattr(hosts_page.AddHostDialog, "exec", fill)
    page.add()
    assert added[-1] == (("ct2", "admin@192.0.2.11", 22, "", ""), {"command": "/opt/sai/bin/sandbox-ai",
                                                                     "update_sudo": True})
    monkeypatch.setattr(page, "host", lambda: "ct1")
    monkeypatch.setattr(sai, "host_doctor", lambda name, engine: [{"name": f"{name} {engine}", "status": "ok",
                                                                   "detail": ""}])
    page.doctor_backend.setCurrentText("bwrap")
    page.check_host()
    assert "ct1: checked for bwrap" in page.status.text()


def test_a_start_on_another_host_detaches_like_one_here(qapp):
    from sandbox_ai_app.terminal import detaches, remote_words
    command = ("if command -v sandbox-ai >/dev/null 2>&1; then exec sandbox-ai claude --mode high; else exec "
               "\"$HOME/.local/bin/sandbox-ai\" claude --mode high; fi")
    assert remote_words(command) == ["sandbox-ai", "claude", "--mode", "high"]
    assert detaches(["ssh", "-tt", "admin@192.0.2.10", command]), "its tmux on that host keeps it"
    assert detaches(["ssh", "-tt", "h", "exec /opt/sai claude --session api"])
    assert not detaches(["ssh", "-tt", "h", "exec /opt/sai claude --dry-run"])
    assert not detaches(["ssh", "-tt", "h", "exec /opt/sai doctor"]) and remote_words("echo hi") is None
