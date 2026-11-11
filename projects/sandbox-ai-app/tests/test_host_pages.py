"""The Manage pages, Activity, Statistics, the canary log and the Settings' lists on a host of `sandbox-ai hosts`
(offscreen): with the host chooser on ct1 every command goes through SandboxAI.host_run there (here a stand-in that
records what it was asked and answers from a table), a terminal command through host_terminal, and what widens or
removes something is asked first (widgets.ask_first with its action of backend.NEEDS_CONFIRM)."""
import json
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, widgets  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")
MODULES = ("vpn_page", "canaries_view", "kits_page", "credentials_page", "identities_page", "settings", "llm_page",
           "team_page", "pages", "activity_page")


def synchronous(function, *args, done, **kw):
    try:
        result = function(*args)
    except Exception as exc:  # noqa: BLE001 - as ThreadCall hands it on
        done(None, exc)
        return None
    done(result, None)
    return None


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache"), ("HOME", "home")):
        monkeypatch.setenv(name, str(tmp_path / sub))
        (tmp_path / sub).mkdir()
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    import importlib
    for name in MODULES:
        module = importlib.import_module(f"sandbox_ai_app.{name}")
        if hasattr(module, "background"):
            monkeypatch.setattr(module, "background", synchronous)
    monkeypatch.setattr(widgets, "background", synchronous)
    return tmp_path


@pytest.fixture
def sai(env, monkeypatch):
    """This machine and the host ct1; host_run records (host, args, input) and answers from sai.answers."""
    sai = backend.SandboxAI()
    sai.asked, sai.answers, sai.terminals, sai.kept = [], {}, [], []
    monkeypatch.setattr(sai, "hosts_supported", lambda: True)
    monkeypatch.setattr(sai, "host_entries", lambda: [{"name": "local", "kind": "local"},
                                                      {"name": "ct1", "kind": "ssh", "ssh": "admin@192.0.2.10"}])

    def host_run(name, *args, timeout=120, input=None):
        sai.asked.append((name, args) if input is None else (name, args, input))
        return sai.answers.get((name, args), (0, "", ""))
    monkeypatch.setattr(sai, "host_run", host_run)
    monkeypatch.setattr(sai, "host_terminal", lambda name, *args: ["ssh", "-tt", name, "sandbox-ai", *args])
    return sai


def answer(sai, args, data, host="ct1", status=0):
    sai.answers[(host, tuple(args))] = (status, data if isinstance(data, str) else json.dumps(data), "")


def asked(sai, host="ct1"):
    return [entry[1] for entry in sai.asked if entry[0] == host]


def choose(chooser, host="ct1"):
    chooser.box.setCurrentIndex(chooser.box.findData(host))


def last_question():
    return widgets.SHOWN[-1][:2] if widgets.SHOWN else None


# -- VPN (Credentials › VPN): list, add with Tor and the advanced options, show, check, remove --

def test_vpn_profiles_of_a_host(qapp, sai, monkeypatch):
    from PySide6.QtWidgets import QDialog
    from sandbox_ai_app import vpn_page
    answer(sai, ["vpn", "list", "--json"], [{"profile": "tor1", "type": "tor", "provider": "tor"}])
    box = vpn_page.VpnBox(sai)
    box.set_host("ct1")
    assert asked(sai)[-1] == ("vpn", "list", "--json") and box.profiles.item(0, 0).text() == "tor1"

    def fill_dialog(dialog):
        assert dialog.remote and not dialog.browse.isVisible()
        dialog.name.setText("tor2")
        dialog.kind.setCurrentIndex(dialog.kind.findData("tor"))
        dialog.exit_country.setText("de,nl")
        dialog.bridges.setText("/home/admin/bridges.txt")
        dialog.socks.setChecked(True)
        dialog.dns.setText("10.0.0.1")
        dialog.force.setChecked(True)
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(vpn_page.VpnDialog, "exec", fill_dialog)
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)  # the SOCKS port is declined
    box.add_button.click()
    assert last_question()[0] == "New VPN profile" and "SOCKS port" in last_question()[1]
    assert ("vpn", "add", "tor2") not in [a[:3] for a in asked(sai)]
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    box.add_button.click()
    add = next(a for a in asked(sai) if a[:2] == ("vpn", "add"))
    assert add == ("vpn", "add", "tor2", "--tor", "--exit-country=de,nl", "--bridges=/home/admin/bridges.txt",
                   "--socks", "--dns=10.0.0.1", "--force")
    parsed = sai.module("vpn").parser().parse_args(list(add[1:]))
    assert parsed.tor and parsed.socks and parsed.force and parsed.exit_country == "de,nl"
    box.profiles.selectRow(0)
    answer(sai, ["vpn", "show", "tor1"], "tor1: tor, exits de\x1b[2J")
    box.show_button.click()
    assert widgets.SHOWN[-1][0] == "VPN profile tor1 on ct1" and "\\x1b[2J" in widgets.SHOWN[-1][1]
    box.check_button.click()
    assert asked(sai)[-1] == ("vpn", "check", "tor1")
    box.remove_button.click()
    assert last_question() == ("Remove", "On ct1: Remove the VPN profile tor1?")
    assert ("vpn", "remove", "tor1") in asked(sai)
    box.deleteLater()


def test_a_typed_key_goes_to_the_hosts_secrets_on_standard_input(qapp, sai, monkeypatch):
    from PySide6.QtWidgets import QDialog
    from sandbox_ai_app import vpn_page
    monkeypatch.setattr(sai, "secret_exists", lambda name, host="local": False)

    def fill_dialog(dialog):
        dialog.name.setText("ts")
        dialog.kind.setCurrentIndex(dialog.kind.findData("tailscale"))
        dialog.secret.setText("TS_KEY")
        dialog.value.setText("tskey-auth-typed")
        dialog.hostname.setText("node-1")
        dialog.ephemeral.setChecked(True)
        dialog.tailnet_allow.setText("100.64.0.7/32")
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(vpn_page.VpnDialog, "exec", fill_dialog)
    names = []
    vpn_page.add_profile(QtWidgets.QWidget(), sai, names.append, host="ct1", host_label="ct1")
    add, secret = [a for a in sai.asked if a[0] == "ct1"][-2:]  # the profile first, then its key
    assert secret == ("ct1", ("secrets", "set", "TS_KEY"), "tskey-auth-typed\n")
    assert add == ("ct1", ("vpn", "add", "ts", "--tailscale", "--auth-key-secret=TS_KEY", "--hostname=node-1",
                           "--tailnet-allow=100.64.0.7/32", "--ephemeral"))
    assert "tskey-auth-typed" not in json.dumps(add) and names == ["ts"]


def test_vpn_arguments_are_checked(qapp):
    from sandbox_ai_app.vpn_page import add_args
    assert add_args("mv", "mullvad", {"secret": "MV", "location": "se-sto", "ipv6": "no-ipv6", "provider": "mullvad",
                                      "check_urls": "https://am.i.mullvad.net/json", "no_dns_probe": True}) == \
        ["vpn", "add", "mv", "--mullvad", "--account-secret=MV", "--location=se-sto", "--no-ipv6",
         "--check-url=https://am.i.mullvad.net/json", "--no-dns-probe", "--provider=mullvad"]
    assert add_args("w", "openvpn", {"file": "/x/w.ovpn", "split": True})[-1] == "--split"
    for name, kind, fields in (("Bad", "tor", {}), ("t", "tor", {"exit_country": "Germany"}),
                               ("t", "tor", {"bridges": "/b", "bridge_secret": "B"}), ("t", "tor", {"socks": True,
                                                                                               "split": True}),
                               ("w", "wireguard", {"file": "rel.conf"}), ("w", "wireguard", {"file": "/w", "dns": "x"}),
                               ("t", "tailscale", {"tailnet_allow": "nonsense"}), ("t", "tailscale",
                                                                                  {"hostname": "-x"}),
                               ("m", "mullvad", {"ipv6": "both"}), ("t", "tor", {"check_urls": "http://plain"})):
        with pytest.raises(ValueError):
            add_args(name, kind, fields)


# -- the canary log of a host, one run's canaries (tokens asked first) and Resume (asked first) --

def test_canaries_of_a_host(qapp, sai, monkeypatch):
    from sandbox_ai_app.canaries_view import CanariesView
    answer(sai, ["canaries", "log", "--json"], [{"run": "r1", "events": [{"time": "2026-11-07T10:00:00Z",
                                                                           "run": "r1", "canary": "aws",
                                                                           "event": "read"}]}])
    answer(sai, ["canaries", "list", "--json"], [{"run": "r1", "status": "paused", "events": 1, "alerts": 1}])
    answer(sai, ["canaries", "list", "--tokens", "--json", "--", "r1"], {
        "run": "r1", "status": "paused", "canaries": [{"id": "aws", "sandbox_path": "/root/.aws/credentials",
                                                       "tokens": {"key": "AKIAFULLTOKEN"}}], "events": []})
    view = CanariesView(sai)
    view.set_host("ct1")
    view.refresh()
    assert ("canaries", "log", "--json") in asked(sai) and view.tree.topLevelItemCount() == 1
    assert view.run_box.count() == 1 and view.run_box.currentData() == "r1"
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)
    view.tokens_button.click()
    assert "in full" in last_question()[1] and ("canaries", "list", "--tokens", "--json", "--", "r1") not in asked(sai)
    view.resume_button.click()
    assert last_question()[0] == "Resume" and ("canaries", "resume", "--", "r1") not in asked(sai)
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    view.tokens_button.click()
    assert widgets.SHOWN[-1][0] == "Canaries of r1" and "AKIAFULLTOKEN" in widgets.SHOWN[-1][1]
    view.show_button.click()
    assert asked(sai)[-1] == ("canaries", "list", "--json", "--", "r1")
    view.resume_button.click()
    assert asked(sai)[-1] == ("canaries", "resume", "--", "r1") and "r1 resumed." in view.status.text()
    view.deleteLater()


# -- kits of a host: list, add a skill from a path there, revoke, review in a terminal, plugins --

def test_kits_of_a_host(qapp, sai, monkeypatch):
    from sandbox_ai_app import kits_page
    opened = []
    page = kits_page.KitsPage(sai, lambda argv, title, **kw: opened.append((argv, title)))
    hooks = {"hooks": [{"event": "PreToolUse", "command": "./check.sh"}], "trusted": True, "files": []}
    answer(sai, ["kits", "list", "--json"], {"kits": [
        {"name": "mine", "kind": "user", "path": "/home/admin/.local/share/sandbox-ai/kits/mine",
         "agents": {"claude": {"skills": [], "plugins": [], "hooks": hooks}}}], "kits_dir": "/home/admin/kits"})
    choose(page.hosts)
    page.refresh()
    assert asked(sai)[-1] == ("kits", "list", "--json") and page.table.rowCount() == 1
    page.table.selectRow(0)
    assert not page.use_button.isEnabled(), "the current project is on this machine"
    monkeypatch.setattr(QtWidgets.QInputDialog, "getText", lambda *a, **k: ("/srv/skills/review", True))
    from conftest import wait_until
    page.add_skill()
    # A kit change runs in the page's Task (a thread), one at a time: the next waits until it has ended.
    assert wait_until(qapp, lambda: not page.task.running, 30)
    assert ("kits", "add-skill", "--", "mine", "/srv/skills/review") in asked(sai)
    assert any(text.startswith("On ct1: Add /srv/skills/review to the kit mine?") for _, text, _ in widgets.SHOWN)
    page.revoke_button.click()
    assert wait_until(qapp, lambda: not page.task.running, 30)
    assert ("kits", "trust-hooks", "--revoke", "--", "mine") in asked(sai)
    page.trust_hooks()
    assert opened[-1] == (["ssh", "-tt", "ct1", "sandbox-ai", "kits", "trust-hooks", "--", "mine"],
                          "sandbox-ai kits trust-hooks mine (ct1)")
    page.install(True)
    assert wait_until(qapp, lambda: not page.task.running, 30)
    assert ("plugins", "claude", "--update") in asked(sai), "on ct1, in the background (no terminal)"
    assert opened[-1][0][-3:] == ["trust-hooks", "--", "mine"]
    answer(sai, ["plugins", "--status"], "claude    plugin store /x\n          plugins: none")
    page.status_button.click()
    assert widgets.SHOWN[-1][0] == "Plugins on ct1" and "plugin store" in widgets.SHOWN[-1][1]
    page.deleteLater()


# -- credentials of a host: logins, secrets, Git and publish logins, identities, VPN; login in a terminal --

def test_credentials_of_a_host(qapp, sai, monkeypatch):
    from sandbox_ai_app.credentials_page import CredentialsPage
    opened = []
    answer(sai, ["login", "--status"], "claude    host         no login  (/home/admin/.claude/.credentials.json)\n")
    answer(sai, ["git-logins", "list", "--json"], [{"url": "https://git.example.org", "user": "me", "secret": "G"}])
    answer(sai, ["publish-logins", "list", "--json"], [{"kind": "pypi", "host": "upload.pypi.org", "secret": "P"}])
    answer(sai, ["secrets", "list"], "NAME STORE\nG keyring\n")
    answer(sai, ["identities", "list", "--json"], {"work": {"name": "W", "git": [], "publish": []}})
    answer(sai, ["vpn", "list", "--json"], [{"profile": "home", "type": "wireguard"}])
    page = CredentialsPage(sai, lambda argv, title, **kw: opened.append((argv, title)))
    choose(page.hosts)
    for args in (("login", "--status"), ("git-logins", "list", "--json"), ("publish-logins", "list", "--json"),
                 ("secrets", "list"), ("identities", "list", "--json"), ("vpn", "list", "--json")):
        assert args in asked(sai), args
    cells = lambda table: [table.item(r, 0).text() for r in range(table.rowCount())]  # noqa: E731
    assert cells(page.logins) == ["claude"] and cells(page.git.list) == ["https://git.example.org"]
    assert cells(page.secrets) == ["G"] and cells(page.identities.list) == ["work"]
    assert page.publish.state(page.publish.entries[0]) == "secret P missing"
    page.logins.selectRow(0)
    page.login(False)
    assert opened[-1] == (["ssh", "-tt", "ct1", "sandbox-ai", "login", "claude"], "sandbox-ai login claude (ct1)")
    page.login(True)
    assert last_question()[1].startswith("On ct1: Forget the stored claude login?")
    assert opened[-1][0][-3:] == ["login", "claude", "--logout"]
    answer(sai, ["publish-logins", "kinds"], "pypi      PyPI uploads\x1b[1m")
    page.publish.kinds_button.click()
    assert widgets.SHOWN[-1][0] == "Publish login kinds" and "\\x1b[1m" in widgets.SHOWN[-1][1]
    page.git.list.selectRow(0)
    page.git.remove()
    assert ("git-logins", "remove", "--", "https://git.example.org") in asked(sai)
    page.identities.list.selectRow(0)
    answer(sai, ["identities", "check", "--", "work"], "work: ready", status=0)
    page.identities.check_button.click()
    assert widgets.SHOWN[-1][0] == "Check of work" and widgets.SHOWN[-1][1].startswith("every secret is there")
    page.identities.remove()
    assert ("identities", "remove", "--", "work") in asked(sai)
    page.deleteLater()


# -- Settings: preset folders and trusted projects of a host --

def test_preset_folders_and_trusted_projects_of_a_host(qapp, sai, monkeypatch):
    from sandbox_ai_app import settings
    answer(sai, ["presets", "dirs", "list", "--json"], {"dirs": [{"path": "/srv/presets", "state": "used",
                                                                  "presets": 2}], "yours": "/home/admin/presets"})
    answer(sai, ["wizard", "trust", "--list", "--json"], [{"path": "/srv/p/.sandbox-ai.json", "state": "changed"},
                                                          {"path": "-x"}])
    dirs = settings.PresetDirs(sai)
    choose(dirs.hosts)
    assert dirs.list.count() == 1 and dirs.list.item(0).text() == "/srv/presets  (2 presets)"
    monkeypatch.setattr(QtWidgets.QInputDialog, "getText", lambda *a, **k: ("/srv/more", True))
    dirs.add()
    assert ("presets", "dirs", "add", "--", "/srv/more") in asked(sai)
    trusted = settings.TrustedProjects(sai)
    assert "local" in [e[0] for e in sai.asked]  # this machine's list first
    choose(trusted.hosts)
    assert trusted.list.count() == 1 and "changed since it was trusted" in trusted.list.item(0).text()
    trusted.list.setCurrentRow(0)
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)
    trusted.forget()
    assert "No longer trust /srv/p/.sandbox-ai.json on ct1?" in last_question()[1]
    assert ("wizard", "forget", "--", "/srv/p/.sandbox-ai.json") not in asked(sai)
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    trusted.forget()
    assert ("wizard", "forget", "--", "/srv/p/.sandbox-ai.json") in asked(sai)
    dirs.deleteLater()
    trusted.deleteLater()


# -- LLM servers: the further options of llm add (insecure asked first), show, models --sort --

def test_llm_add_options_show_and_sort(qapp, sai, monkeypatch):
    from PySide6.QtWidgets import QDialog
    from sandbox_ai_app import llm_page
    answer(sai, ["llm", "list", "--json"], [{"name": "lm", "kind": "vllm", "url": "http://203.0.113.5:8000"}])
    page = llm_page.LlmPage(sai)
    page.show()
    page.host_box.setCurrentIndex(page.host_box.findData("ct1"))

    def fill_dialog(dialog):
        dialog.name.setText("gw")
        dialog.kind.setCurrentIndex(dialog.kind.findData("openai-compatible"))
        dialog.url.setText("http://203.0.113.5:8000")
        dialog.headers.setText("X-Gateway-Key=GW_KEY X-Team=TEAM_ID")
        dialog.ca.setText("/etc/ssl/gw.pem")
        dialog.insecure.setChecked(True)
        dialog.replace.setChecked(True)
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(llm_page.LlmDialog, "exec", fill_dialog)
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)
    page.add()
    assert "unencrypted" in last_question()[1] and not [a for a in asked(sai) if a[:2] == ("llm", "add")]
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    page.add()
    add = next(a for a in asked(sai) if a[:2] == ("llm", "add"))
    assert add[-5:] == ("--header-secret=X-Gateway-Key=GW_KEY", "--header-secret=X-Team=TEAM_ID",
                        "--ca=/etc/ssl/gw.pem", "--insecure-http", "--replace")
    from test_llm_page import parsed
    args = parsed(sai, list(add))
    assert args.insecure_http and args.replace and args.ca == "/etc/ssl/gw.pem"
    assert args.header_secret == ["X-Gateway-Key=GW_KEY", "X-Team=TEAM_ID"]
    with pytest.raises(ValueError):
        llm_page.add_argv({"name": "x", "kind": "vllm", "url": "http://h:1", "headers": "Host=S"})
    page.servers.selectRow(0)
    answer(sai, ["llm", "show", "lm"], "lm: vLLM at http://203.0.113.5:8000")
    page.show_button.click()
    assert widgets.SHOWN[-1][0] == "LLM server lm on ct1"
    page.sort.setCurrentIndex(page.sort.findData("size"))
    page.models_button.click()
    assert asked(sai)[-1] == ("llm", "models", "lm", "--sort", "size", "--json")
    page.close()
    page.deleteLater()


# -- Activity and Statistics of a host --

def test_the_activity_of_a_host_is_asked_again_while_following(qapp, sai, monkeypatch):
    from sandbox_ai_app import activity_page
    instance = backend.Instance("session", "ct1:s1", "s1", project="/srv/w", state="running", host="ct1")
    monkeypatch.setattr(sai, "host_instances", lambda host, timeout=60: [instance])
    first = [{"kind": "prompt", "id": "a", "time": "2026-11-07T10:00:00Z", "summary": "hello"}]
    tail = ("activity", "--session", "s1", "--json", "--tail", str(activity_page.REMOTE_TAIL))
    answer(sai, tail, "\n".join(json.dumps(r) for r in first))
    page = activity_page.ActivityPage(sai)
    page.started = True
    choose(page.hosts)
    assert page.source.currentData() == ["--session", "s1"] and asked(sai)[-1] == tail
    assert page.model.rowCount() == 1 and page.poll.isActive()
    second = first + [{"kind": "tool", "id": "b", "time": "2026-11-07T10:00:05Z", "summary": "ls"}]
    answer(sai, tail, "\n".join(json.dumps(r) for r in second))
    page.look_again()
    assert page.model.rowCount() == 2, "only what is new is added"
    assert not page.connections_button.isEnabled()
    page.since.setText("10m")
    page.restart()
    assert asked(sai)[-1] == (*tail, "--since=10m")
    page.since.setText("yesterday-ish")
    page.restart()
    assert "Since:" in page.status.text()
    page.stop()
    assert not page.poll.isActive()
    page.deleteLater()


def test_the_statistics_of_a_folder_on_a_host(qapp, sai):
    from sandbox_ai_app.pages import StatisticsPage
    answer(sai, ["logs", "/srv/logs", "--json"], {"telemetry": {"requests": 7, "processes": 2, "model_calls":
                                                                 {"claude-x": 3}}, "tool_calls": {}})
    page = StatisticsPage(sai)
    choose(page.hosts)
    assert page.directory.isEditable() and not page.project_folder.isHidden()
    page.directory.setEditText("/srv/logs")
    page.load()
    assert asked(sai)[-1] == ("logs", "/srv/logs", "--json") and page.summary.text().startswith("7 HTTP requests")
    page.project_folder.setChecked(True)
    assert asked(sai)[-1] == ("logs", "--project", "/srv/logs", "--json")
    page.deleteLater()


# -- Images of a host: every command through hosts run, builds in a terminal there --

def test_images_of_a_host(qapp, sai, monkeypatch):
    from sandbox_ai_app.images_page import ImagesPage
    opened, started, tasks = [], [], []
    page = ImagesPage(sai, lambda argv, title, **kw: opened.append((argv, title)))
    monkeypatch.setattr(page.list_command, "start", lambda argv, input=None: started.append(argv) or True)
    monkeypatch.setattr(page.task, "run", lambda argv, done, text, source=None, input=None, **kw: tasks.append((argv, input)))
    choose(page.hosts)
    page.loaded = True
    page.refresh()
    prefix = sai.command("hosts", "run", "ct1", "--")
    assert started[-1] == prefix + ["images", "--json"]
    page.show_images([{"agent": "claude", "present": True, "image": "localhost/claude-sandbox:latest",
                       "extended": True, "status": "current"},
                      {"agent": "claude", "variant": "gpu", "target": "gpu", "present": True,
                       "image": "localhost/claude-sandbox-gpu:latest"}])
    page.images.selectRow(0)
    page.build()
    assert opened[-1] == (["ssh", "-tt", "ct1", "sandbox-ai", "build", "claude"], "sandbox-ai build claude (ct1)")
    page.start_build("claude", {"extension_only": False, "pin": "2.1.0", "uv": None, "node": None})
    assert opened[-1][0][-4:] == ["build", "--pin", "2.1.0", "claude"]
    page.build_all()
    assert opened[-1][0][-2:] == ["build", "all"]
    page.show_history()
    assert tasks[-1][0] == prefix + ["images", "history", "claude"]
    page.drop_extension()
    assert tasks[-1][0] == prefix + ["images", "remove", "claude"]
    page.images.selectRow(1)
    page.delete_variant(keep_image=True)
    assert tasks[-1][0] == prefix + ["images", "remove", "gpu", "--keep-image"]
    page.deleteLater()


# -- an answer goes to the host its question named: the chooser moved to another host while it was open --
# (Questions and dialogs block nothing: the host chooser can change while one is open.)

def with_ct2(sai, monkeypatch):
    monkeypatch.setattr(sai, "host_entries", lambda: [{"name": "local", "kind": "local"},
                                                      {"name": "ct1", "kind": "ssh", "ssh": "admin@192.0.2.10"},
                                                      {"name": "ct2", "kind": "ssh", "ssh": "admin@192.0.2.11"}])


def switching(chooser, to="ct2", accepted=True):
    """A question answered after the chooser went to TO meanwhile."""
    def ask(*args, **kwargs):
        choose(chooser, to)
        return accepted
    return ask


def select_rows(view, rows):
    view.clearSelection()
    model = view.selectionModel()
    for row in rows:
        model.select(view.model().index(row, 0), model.SelectionFlag.Select | model.SelectionFlag.Rows)


def test_removals_of_credentials_go_to_the_host_the_question_named(qapp, sai, monkeypatch):
    from sandbox_ai_app import credentials_page, identities_page
    with_ct2(sai, monkeypatch)
    for host in ("ct1", "ct2"):
        answer(sai, ["git-logins", "list", "--json"], [{"url": "https://a.example.org", "user": "", "secret": "G"},
                                                       {"url": "https://b.example.org", "user": "", "secret": "G"}],
               host=host)
        answer(sai, ["identities", "list", "--json"], {"work": {"name": "W", "git": [], "publish": []},
                                                       "home": {"name": "H", "git": [], "publish": []}}, host=host)
    page = credentials_page.CredentialsPage(sai, lambda argv, title, **kw: None)
    monkeypatch.setattr(credentials_page, "ask_first", switching(page.hosts))
    monkeypatch.setattr(identities_page, "ask_first", switching(page.hosts))
    removals = lambda host: [a for a in asked(sai, host) if "remove" in a]  # noqa: E731
    for rows, box in (([0], page.git), ([0, 1], page.git), ([0], page.identities), ([0, 1], page.identities)):
        choose(page.hosts, "ct1")
        assert box.host == "ct1" and box.list.rowCount() == 2
        select_rows(box.list, rows)
        box.remove()
        assert box.host == "ct2"
        assert len(removals("ct1")) == len(rows) and not removals("ct2"), (box, rows)
        sai.asked.clear()
    page.deleteLater()


def test_images_changes_go_to_the_host_the_question_named(qapp, sai, monkeypatch):
    from sandbox_ai_app import images_page
    with_ct2(sai, monkeypatch)
    tasks = []
    page = images_page.ImagesPage(sai, lambda argv, title, **kw: tasks.append((argv, title)))
    monkeypatch.setattr(page.list_command, "start", lambda argv, input=None: True)
    monkeypatch.setattr(page.task, "run", lambda argv, done, text, source=None, input=None, **kw:
                        tasks.append((argv, text)))
    ct1 = sai.command("hosts", "run", "ct1", "--")
    rows = [{"agent": "claude", "present": True, "image": "localhost/claude-sandbox:latest", "extended": True,
             "status": "current"},
            {"agent": "claude", "variant": "gpu", "target": "gpu", "present": True,
             "image": "localhost/claude-sandbox-gpu:latest"}]

    def on_ct1(row):
        choose(page.hosts, "ct1")
        page.show_images(rows)
        page.images.selectRow(row)
    monkeypatch.setattr(images_page, "ask_first", switching(page.hosts))
    on_ct1(1)
    page.delete_variant()
    assert tasks[-1][0] == ct1 + ["images", "remove", "gpu"]
    on_ct1(0)
    page.drop_extension()
    assert tasks[-1][0] == ct1 + ["images", "remove", "claude"]

    def variant(dialog):
        choose(page.hosts, "ct2")
        dialog.name.setText("web")
        return True
    monkeypatch.setattr(images_page.NewVariantDialog, "exec", variant)
    on_ct1(0)
    page.new_variant()
    assert tasks[-1][0][:len(ct1) + 3] == ct1 + ["images", "new", "web"]

    def accepted(dialog):
        choose(page.hosts, "ct2")
        return True
    monkeypatch.setattr(images_page.RecipeDialog, "exec", accepted)
    on_ct1(0)
    page.new_from_recipe()
    assert tasks[-1][0][:len(ct1) + 2] == ct1 + ["images", "recipe"]
    monkeypatch.setattr(images_page.BuildDialog, "exec", accepted)
    on_ct1(0)
    page.build_with_options()
    assert tasks[-1][1].endswith("(ct1)") and tasks[-1][0][:3] == ["ssh", "-tt", "ct1"]
    # A file for the extension edited when the dialogs opened: never added to another one's.
    on_ct1(0)
    page.description, page.target, page.pending_files = {"agent": "claude", "files": []}, "claude", []
    warned = []
    monkeypatch.setattr(images_page, "warn", lambda parent, title, text, **kw: warned.append(text))
    monkeypatch.setattr(QtWidgets.QInputDialog, "getText", lambda *a, **k: ("/srv/tool", True))
    monkeypatch.setattr(images_page.FileTargetDialog, "exec", accepted)
    page.add_file()
    assert page.pending_files == [] and warned == ["Another extension is edited now: /srv/tool was not added."]
    page.deleteLater()


def test_a_confirmed_kits_deletion_is_never_dropped_without_a_word(qapp, sai, monkeypatch):
    """Another kits change started while the question was open: the deletion does not start then - and says so."""
    import threading
    from conftest import wait_until
    from sandbox_ai_app import kits_page
    page = kits_page.KitsPage(sai, lambda argv, title, **kw: None)
    answer(sai, ["kits", "list", "--json"], {"kits": [
        {"name": n, "kind": "user", "path": f"/home/admin/kits/{n}", "agents": {}} for n in ("one", "two")],
        "kits_dir": "/home/admin/kits"})
    choose(page.hosts)
    page.refresh()
    assert page.table.rowCount() == 2
    release = threading.Event()

    def meanwhile(*args, **kwargs):
        page.start_change(lambda: release.wait(10) and (0, "", ""), "Other", "Another change…")
        return True
    monkeypatch.setattr(kits_page, "ask_first", meanwhile)
    page.delete_several([page.kits[f"user:{n}"] for n in ("one", "two")])
    assert "not started" in page.message.text() and "Delete" in page.message.text()
    release.set()
    assert wait_until(qapp, lambda: not page.task.running, 30)
    assert not [a for a in asked(sai) if a[:2] == ("kits", "delete")]
    page.deleteLater()


def test_a_second_remove_waits_for_the_first(qapp, sai, monkeypatch):
    """VPN profiles: Remove… and the Delete key are off while a removal runs (a second one reported false
    failures)."""
    from sandbox_ai_app import vpn_page
    answer(sai, ["vpn", "list", "--json"], [{"profile": "a", "type": "tor"}, {"profile": "b", "type": "tor"}])
    box = vpn_page.VpnBox(sai)
    box.set_host("ct1")
    sources = []
    monkeypatch.setattr(vpn_page, "run_each", lambda work, names, **kw: sources.append(kw.get("source")))
    monkeypatch.setattr(vpn_page, "background", lambda *a, **kw: sources.append(kw.get("source")))
    box.profiles.selectAll()
    box.remove()
    box.profiles.selectRow(0)
    box.remove()
    assert len(sources) == 2 and all(box.remove_button in (s if isinstance(s, list) else [s]) for s in sources)
    widgets._hold(box.remove_button)
    count = len(widgets.SHOWN)
    box.remove()  # the Delete key while one runs
    assert len(widgets.SHOWN) == count and "running" in box.message.text()
    widgets._release(box.remove_button)
    box.deleteLater()
