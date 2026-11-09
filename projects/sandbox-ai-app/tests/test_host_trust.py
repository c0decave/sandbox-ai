"""Review round 5, A7: what the app could not do on another host yet - review and trust a project file there
(`sandbox-ai wizard trust --show --json`, then `--digest` of exactly what was shown) with the same question as for a
file of this machine (every key, what weakens marked, the changes against the version the app saw trusted last), and
the host's web interface (`sandbox-ai web user|cert|vendor|serve`).

The host "ct1" here is this machine's own sandbox-ai behind the app's host runner (its real `wizard trust` answers,
its real trusted list in the test's XDG directories); its --help is the real one too unless a test makes it older."""
import argparse
import json
import os

import pytest

from sandbox_ai_app import backend, project_trust as PT, widgets

needs_sai = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                               reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")

# What an agent that can write the workspace plants: lax mode, no canaries, a kept HOME.
PLANTED = {"version": 1, "agent": "claude", "options": [], "mode": "lax", "canaries": False, "keep": True}
CLEAN = {"version": 1, "agent": "claude", "options": ["--offline"]}


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    return tmp_path


@pytest.fixture
def host(isolated, monkeypatch):
    """A SandboxAI with a host ct1 that is this machine's sandbox-ai (asked through host_run, recorded in
    sai.asked); sai.old_help names the subcommands whose --help there lacks the 0.1.78 options."""
    sai = backend.SandboxAI()
    sai.asked, sai.old_help = [], set()
    real = sai.host_run

    def host_run(name, *args, timeout=120, input=None):
        sai.asked.append((name, *args))
        if name == "ct1" and args and args[-1] == "--help" and tuple(args[:-1]) in sai.old_help:
            return 0, "  -y, --yes  trust without asking\n", ""
        return real("local", *args, timeout=timeout, input=input)
    monkeypatch.setattr(sai, "host_run", host_run)
    monkeypatch.setattr(sai, "host_statuses", lambda refresh=False, cached=True: {"ct1": {"version": "0.1.78"}})
    return sai


def project_file(isolated, document, name="project"):
    folder = isolated / name
    folder.mkdir(exist_ok=True)
    (folder / ".sandbox-ai.json").write_text(json.dumps(document, indent=2) + "\n")
    return str(folder / ".sandbox-ai.json")


def runs(sai, *words):
    return [a for a in sai.asked if a[0] == "ct1" and a[1:1 + len(words)] == words and a[-1] != "--help"]


# -- the backend: shown there, trusted by its digest, the version trusted last kept --

@needs_sai
def test_a_file_on_another_host_is_shown_trusted_by_digest_and_its_change_diffed(host, isolated):
    sai = host
    path = project_file(isolated, CLEAN)
    shown = sai.host_project_show("ct1", path)
    assert shown["path"] == path and shown["trusted"] is False and shown["trusted_digest"] is None
    assert shown["document"] == CLEAN and len(shown["digest"]) == 64 and not shown.get("problem")
    assert runs(sai, "wizard", "trust") == [("ct1", "wizard", "trust", "--show", "--json", "--", path)]
    assert sai.shown_baseline(shown) == ("none", None)
    status, out, err = sai.host_project_trust("ct1", path, shown["digest"], shown["document"])
    assert status == 0, err
    assert runs(sai, "wizard", "trust")[-1] == ("ct1", "wizard", "trust", f"--digest={shown['digest']}", "--", path)
    assert sai.host_project_show("ct1", path)["trusted"] is True
    # The agent writes the folder: the next review is against the version trusted last, never the file there.
    project_file(isolated, PLANTED)
    changed = sai.host_project_show("ct1", path)
    assert changed["trusted"] is False and changed["trusted_digest"] == shown["digest"]
    state, old = sai.shown_baseline(changed)
    assert state == "kept" and json.loads(old) == CLEAN
    question, details = PT.host_question(sai, changed, "Trust?", (state, old), {})
    for key in ("mode: lax", "canaries: false", "keep: true"):
        assert f"{PT.WARN} {key}" in question, (key, question)
    assert "Changes against the version you trusted last:" in details
    assert '+  "mode": "lax",' in details and '-    "--offline"' in details, details


@needs_sai
def test_a_file_changed_after_the_review_is_not_trusted_and_no_copy_is_kept(host, isolated):
    sai = host
    path = project_file(isolated, CLEAN)
    shown = sai.host_project_show("ct1", path)
    project_file(isolated, PLANTED)  # swapped in between the question and the yes
    status, _, err = sai.host_project_trust("ct1", path, shown["digest"], shown["document"])
    assert status != 0, "sandbox-ai there trusts only the version reviewed"
    assert backend.shown_copy(shown["digest"]) is None
    assert sai.host_project_show("ct1", path)["trusted_digest"] is None


@needs_sai
def test_trusted_elsewhere_is_shown_whole_and_an_unreadable_file_says_why(host, isolated):
    sai = host
    path = project_file(isolated, CLEAN)
    digest = sai.host_project_show("ct1", path)["digest"]
    assert sai.host_run("local", "wizard", "trust", f"--digest={digest}", "--", path)[0] == 0  # in a terminal there
    project_file(isolated, PLANTED)
    changed = sai.host_project_show("ct1", path)
    assert sai.shown_baseline(changed) == ("unknown", None), "the app never saw that version"
    _, details = PT.host_question(sai, changed, "Trust?", ("unknown", None), {})
    assert details.startswith("A different version was trusted before") and '"mode": "lax"' in details
    missing = sai.host_project_show("ct1", str(isolated / "nothing" / ".sandbox-ai.json"))
    assert missing["error"] and missing["digest"] is None


@needs_sai
def test_an_older_host_is_told_to_update_and_nothing_is_sent(host, isolated):
    sai = host
    sai.old_help = {("wizard", "trust")}
    path = project_file(isolated, CLEAN)
    with pytest.raises(backend.HostTooOld, match="--show"):
        sai.host_project_show("ct1", path)
    status, _, err = sai.host_project_trust("ct1", path, "a" * 64)
    assert status == 2 and "--digest" in err and "Hosts › Update" in err
    assert runs(sai, "wizard", "trust") == [], "nothing but its --help went there"
    with pytest.raises(ValueError):
        sai.host_project_show("ct1", "relative/.sandbox-ai.json")


def test_weakening_is_measured_against_the_defaults_of_the_files_host(isolated):
    sai = backend.SandboxAI()
    assert "mode" in PT.weakening(sai, {"mode": "medium"}, {"mode": "high"}), "laxer than that host's default"
    assert "mode" not in PT.weakening(sai, {"mode": "medium"}, {"mode": "low"})
    assert "canaries" in PT.weakening(sai, {"canaries": ["credentials"]}, {"canaries": ["credentials", "home"]})
    assert "canaries" not in PT.weakening(sai, {"canaries": ["credentials", "home"]}, {"canaries": ["home"]})
    assert PT.config_path("/srv/api") == "/srv/api/.sandbox-ai.json"
    assert PT.config_path("/srv/api/") == "/srv/api/.sandbox-ai.json"
    assert PT.config_path(" /srv/api/.sandbox-ai.json ") == "/srv/api/.sandbox-ai.json"
    assert PT.config_path("srv/api") is None and PT.config_path("-x") is None and PT.config_path("") is None


def test_a_shown_copy_that_is_no_json_object_is_no_copy(isolated):
    digest = "b" * 64
    backend.keep_shown_copy(digest, {"version": 1, "agent": "codex"})
    assert json.loads(backend.shown_copy(digest)) == {"version": 1, "agent": "codex"}
    (backend.trusted_copies_dir() / "shown" / ("c" * 64 + ".json")).write_text("[1]")
    assert backend.shown_copy("c" * 64) is None and backend.shown_copy("../x") is None


# -- the question in the app: Settings and the Start page with another host chosen --

@pytest.fixture
def synchronous(monkeypatch):
    from sandbox_ai_app import host_trust

    def run(function, *args, done, **kw):
        try:
            result = function(*args)
        except Exception as exc:  # noqa: BLE001 - as ThreadCall hands it on
            done(None, exc)
            return
        done(result, None)
    monkeypatch.setattr(host_trust, "background", run)
    monkeypatch.setattr(widgets, "SHOWN", [])
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    return run


@needs_sai
def test_the_review_on_another_host_asks_like_this_machine_and_trusts_on_yes(qapp, host, isolated, synchronous,
                                                                             monkeypatch):
    from PySide6.QtWidgets import QWidget
    from sandbox_ai_app import host_trust
    sai = host
    path = project_file(isolated, PLANTED)
    parent = QWidget()
    outcomes = []
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)  # No: nothing trusted
    host_trust.review_and_trust(parent, sai, "ct1", path, done=lambda trusted, doc: outcomes.append((trusted, doc)))
    title, text, details = widgets.SHOWN[-1]
    assert title == "Review the project configuration" and text.startswith("On ct1:")
    assert f"{PT.WARN} mode: lax" in text and f"{PT.WARN} keep: true" in text and "is not trusted yet" in text
    assert "Nothing was trusted for this file before" in details and '"mode": "lax"' in details
    assert outcomes == [(False, PLANTED)] and not sai.host_project_show("ct1", path)["trusted"]
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    host_trust.review_and_trust(parent, sai, "ct1", path, done=lambda trusted, doc: outcomes.append((trusted, doc)))
    assert outcomes[-1] == (True, PLANTED) and sai.host_project_show("ct1", path)["trusted"]
    assert widgets.SHOWN[-1][1] == f"Trusted on ct1: {path}"
    asked = len(widgets.SHOWN)
    host_trust.review_and_trust(parent, sai, "ct1", path, done=lambda trusted, doc: outcomes.append((trusted, doc)))
    assert outcomes[-1] == (True, PLANTED) and len(widgets.SHOWN) == asked, "trusted as it is: nothing to ask"
    parent.deleteLater()


@needs_sai
def test_a_file_the_launcher_refuses_is_named_not_asked(qapp, host, isolated, synchronous, monkeypatch):
    from PySide6.QtWidgets import QWidget
    from sandbox_ai_app import host_trust
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    path = project_file(isolated, {"version": 1, "agent": "claude", "options": ["--no-such-option"]})
    outcomes = []
    parent = QWidget()
    host_trust.review_and_trust(parent, host, "ct1", path, done=lambda trusted, doc: outcomes.append(trusted))
    title, text, _ = widgets.SHOWN[-1]
    assert title == "This project configuration cannot be used" and text.startswith("On ct1:")
    assert outcomes == [False] and not runs(host, "wizard", "trust", "--digest")
    host.old_help = {("wizard", "trust")}
    host.__dict__.pop("_net_options", None)  # its --help is read again (kept per host and version otherwise)
    host_trust.review_and_trust(parent, host, "ct1", path, done=lambda trusted, doc: outcomes.append(trusted))
    assert "too old" in widgets.SHOWN[-1][1] and outcomes[-1] is False
    parent.deleteLater()


@needs_sai
def test_the_start_page_opens_a_project_folder_on_another_host(qapp, host, isolated, synchronous, monkeypatch):
    from sandbox_ai_app import ui
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    path = project_file(isolated, CLEAN)
    folder = os.path.dirname(path)
    form = ui.LaunchPage(host, lambda *a, **k: None)
    try:
        monkeypatch.setattr(form, "host", lambda: "ct1")
        assert form.load_project(folder) is None, "decided in the background"
        assert host.host_project_show("ct1", path)["trusted"]
        assert form.form.values().get("--workspace") == [folder], "the form starts in that folder there"
        assert any(f"Trusted: {path}." in text for _, text, _ in widgets.SHOWN)
        widgets.SHOWN.clear()
        form.load_project("relative")
        assert "absolute path" in widgets.SHOWN[-1][1]
    finally:
        form.deleteLater()


@needs_sai
def test_the_settings_list_reviews_a_folder_typed_in(qapp, host, isolated, synchronous, monkeypatch):
    from sandbox_ai_app import settings
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(settings, "background", synchronous)
    path = project_file(isolated, CLEAN)
    box = settings.TrustedProjects(host)
    try:
        monkeypatch.setattr(box, "host", lambda: "ct1")
        box.review(os.path.dirname(path))
        assert host.host_project_show("ct1", path)["trusted"]
        assert box.list.count() == 1 and path in box.list.item(0).text(), "the list is read again"
    finally:
        box.deleteLater()


# -- the web interface of a host: sandbox-ai web user|cert|vendor|serve --

def web_parsed(sai, argv):
    """ARGV (after `sandbox-ai`) as sandbox-ai's own `web` parser takes it; nothing runs."""
    seen = []
    original = argparse.ArgumentParser.parse_args

    class Parsed(Exception):
        pass

    def capture(self, args=None, namespace=None):
        seen.append(original(self, args, namespace))
        raise Parsed
    assert argv[0] == "web"
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(argparse.ArgumentParser, "parse_args", capture)
        with pytest.raises(Parsed):
            sai.module("webui").cli(list(argv[1:]))
    return seen[0]


@needs_sai
def test_the_web_commands_are_the_clis(isolated):
    sai = backend.SandboxAI()
    for action, name, listen, expected in (
            ("add", "ana", None, {"command": "user", "action": "add", "name": "ana", "stdin": False}),
            ("reset", "ana", None, {"command": "user", "action": "reset", "name": "ana"}),
            ("vendor", None, None, {"command": "vendor"}),
            ("serve", None, "0.0.0.0:9443", {"command": "serve", "listen": "0.0.0.0:9443"}),
            ("serve", None, None, {"command": "serve", "listen": backend.WEB_LISTEN})):
        argv = sai.host_web_argv("local", action, name, listen)
        words = argv[argv.index("web"):]
        args = web_parsed(sai, words)
        assert all(getattr(args, key) == value for key, value in expected.items()), (words, args)
    for bad in ("Ana", "-x", "a b", "", "a" * 33):
        with pytest.raises(ValueError):
            sai.host_web_argv("local", "add", bad)
    with pytest.raises(ValueError):
        sai.host_web_argv("local", "serve", listen="localhost:8443")
    assert sai.host_web_user_remove("local", "-x")[0] == 2
    assert backend.web_listen_beyond("0.0.0.0:8443") and backend.web_listen_beyond("[::]:8443")
    assert not backend.web_listen_beyond("127.0.0.1:8443") and not backend.web_listen_beyond("[::1]:8443")
    assert backend.needs_confirm("web-listen", {"beyond": False}) is None
    assert backend.needs_confirm("web-listen", {"beyond": True})


@pytest.fixture
def web(qapp, isolated, monkeypatch):
    """The Hosts page's Web interface tab with a host ct1 that answers from sai.answers; terminals recorded."""
    from sandbox_ai_app import hosts_page
    sai = backend.SandboxAI()
    sai.asked, sai.terminals = [], []
    sai.answers = {("web", "user", "list"): (0, "ana  since 2026-11-01T10:00:00Z\nbob  since ?\n", ""),
                   ("web", "cert"): (0, "/home/u/.config/sandbox-ai/web/cert.pem: SHA-256 AB:CD\n", "")}

    def host_run(name, *args, timeout=120, input=None):
        sai.asked.append((name, *args))
        return sai.answers.get(tuple(args), (0, "", ""))
    monkeypatch.setattr(sai, "host_run", host_run)
    monkeypatch.setattr(sai, "host_terminal", lambda name, *args: ["ssh", name, *args])

    def run(function, *args, done, **kw):
        try:
            result = function(*args)
        except Exception as exc:  # noqa: BLE001
            done(None, exc)
            return
        done(result, None)
    monkeypatch.setattr(hosts_page, "background", run)
    monkeypatch.setattr(widgets, "SHOWN", [])
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    panel = hosts_page.WebPanel(sai, lambda argv, title: sai.terminals.append((argv, title)))
    yield panel, sai
    panel.deleteLater()


def test_the_web_tab_lists_logins_and_asks_before_it_removes_or_resets(web, monkeypatch):
    from sandbox_ai_app.widgets import row_key
    panel, sai = web
    panel.set_host("ct1", load=True)
    assert [row_key(panel.users, r) for r in range(panel.users.rowCount())] == ["ana", "bob"]
    assert not panel.remove_button.isEnabled(), "nothing selected"
    panel.users.selectRow(0)
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)
    panel.remove()
    panel.reset()
    assert ("ct1", "web", "user", "remove", "ana") not in sai.asked and sai.terminals == [], "answered No"
    assert "Remove the login ana" in widgets.SHOWN[0][1] and widgets.SHOWN[0][1].startswith("On ct1:")
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    panel.users.selectRow(0)
    panel.remove()
    assert ("ct1", "web", "user", "remove", "ana") in sai.asked
    panel.users.selectRow(0)
    panel.reset()
    assert sai.terminals[-1] == (["ssh", "ct1", "web", "user", "reset", "ana"], "ct1: web login ana")


def test_the_web_tab_adds_in_a_terminal_and_asks_for_an_open_address(web, monkeypatch):
    from PySide6.QtWidgets import QInputDialog
    panel, sai = web
    panel.set_host("ct1")
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("cara", True)))
    panel.add()
    assert sai.terminals[-1] == (["ssh", "ct1", "web", "user", "add", "cara"], "ct1: web login cara")
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("Cara!", True)))
    panel.add()
    assert len(sai.terminals) == 1 and "lowercase" in widgets.SHOWN[-1][1]
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)
    shown = len(widgets.SHOWN)
    panel.serve()
    assert sai.terminals[-1][0] == ["ssh", "ct1", "web", "serve"] and len(widgets.SHOWN) == shown, \
        "the loopback: nothing to ask"
    panel.listen.setText("0.0.0.0:8443")
    panel.serve()
    assert sai.terminals[-1][0] == ["ssh", "ct1", "web", "serve"] and "0.0.0.0:8443" in widgets.SHOWN[-1][1], \
        "beyond the loopback: asked, and No starts nothing"
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    panel.serve()
    assert sai.terminals[-1][0] == ["ssh", "ct1", "web", "serve", "--listen=0.0.0.0:8443"]
    panel.listen.setText("example.org:8443")
    panel.serve()
    assert len(sai.terminals) == 3 and "ADDRESS:PORT" in widgets.SHOWN[-1][1]
    panel.listen.setText("")
    panel.vendor()
    assert sai.terminals[-1][0] == ["ssh", "ct1", "web", "vendor"]


def test_the_web_tab_shows_the_fingerprint_and_asks_before_a_new_certificate(web, monkeypatch):
    panel, sai = web
    panel.set_host("ct1")
    panel.certificate(False)
    assert "SHA-256 AB:CD" in panel.cert.text() and ("ct1", "web", "cert") in sai.asked
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)
    panel.certificate(True)
    assert ("ct1", "web", "cert", "--renew") not in sai.asked and "fingerprint changes" in widgets.SHOWN[-1][1]
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    panel.certificate(True)
    assert ("ct1", "web", "cert", "--renew") in sai.asked
