"""The Hosts page (offscreen): the hosts of sandbox-ai hosts with what the Sandboxes and Approvals pages last saw of
them, the way to those pages filtered by a host, editing and removing a host, and a host's defaults, each through
sandbox-ai hosts run (here a stand-in that records what it was asked)."""
import json
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, widgets  # noqa: E402
from sandbox_ai_app.hosts_page import HostsPage  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "background", lambda function, *args, done, **kw: done(function(*args), None))
    import sandbox_ai_app.hosts_page as hp
    monkeypatch.setattr(hp, "background", lambda function, *args, done, **kw: done(function(*args), None))
    monkeypatch.setattr(hp.Task, "call_watched", lambda task, sai, function, done, *a, **kw: done(function(), None)
                        or True)  # an update or installation (its streaming and Cancel: test_responsive.py)
    return tmp_path


def choose(page, name):
    """Select the row of host NAME."""
    page.table.clearSelection()
    page.table.selectRow(widgets.find_row(page.table, name))


def test_the_hosts_list_shows_the_fleet_and_leads_to_its_starts_and_questions(qapp, env, monkeypatch):
    """One row per host with what the other pages last saw: whether it answered (and when), its version, the starts
    running there and the questions waiting; Show starts and Show questions name the host for the Sandboxes and
    Approvals pages; the page itself asks no host for that (no SSH but its defaults when shown)."""
    import time
    sai = backend.SandboxAI()
    if not sai.hosts_supported():
        pytest.skip("this sandbox-ai has no hosts.py")
    sai.host_add("ct1", "admin@192.0.2.10", 2222, "", "Proxmox 101")
    sai.host_add("ct2", "admin@192.0.2.11", 22, "", "")
    asked = []
    monkeypatch.setattr(sai, "host_run", lambda name, *args, timeout=120: asked.append((name, args)) or (0, "{}", ""))
    page = HostsPage(sai, lambda argv, title: None)
    assert page.table.row_keys == ["local", "ct1", "ct2"] and asked == [], "built: no command, no SSH"
    assert page.table.item(widgets.find_row(page.table, "ct1"), 1).text() == "admin@192.0.2.10:2222"
    assert not hasattr(page, "starts") and not hasattr(page, "questions"), "their actions are on their own pages"
    page.set_fleet({"local": {"running": 1, "seen": time.time(), "problem": None},
                    "ct1": {"running": 2, "seen": time.time(), "problem": None},
                    "ct2": {"running": 0, "seen": time.time() - 30, "problem": "ssh: timed out"}})
    page.set_questions({"ct1": 3})
    row = {name: [page.table.item(widgets.find_row(page.table, name), c).text() for c in range(6)]
           for name in ("local", "ct1", "ct2")}
    assert row["ct1"][2] == "yes" and row["ct1"][4] == "2" and row["ct1"][5] == "3"
    assert row["ct2"][2].startswith("no answer (last seen ") and row["ct2"][5] == ""
    assert row["local"][2] == "yes" and row["local"][4] == "1"
    starts, questions, refreshed = [], [], []
    page.show_starts.connect(starts.append)
    page.show_questions.connect(questions.append)
    page.refresh_requested.connect(lambda: refreshed.append(True))
    choose(page, "ct1")
    page.starts_button.click()
    page.questions_button.click()
    page.refresh_button.click()
    assert starts == ["ct1"] and questions == ["ct1"] and refreshed == [True]
    assert all(page.table.accessibleName() and b.accessibleName() for b in (page.starts_button, page.edit_button))
    choose(page, "local")
    assert not page.remove_button.isEnabled() and not page.edit_button.isEnabled(), "this machine stays"
    page.close()


def test_remove_asks_first_and_the_other_pages_hear_of_it(qapp, env, monkeypatch):
    sai = backend.SandboxAI()
    if not sai.hosts_supported():
        pytest.skip("this sandbox-ai has no hosts.py")
    sai.host_add("ct1", "admin@192.0.2.10", 22, "", "")
    monkeypatch.setattr(widgets, "SHOWN", [])
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)  # Cancel
    page = HostsPage(sai, lambda argv, title: None)
    removed = []
    page.host_removed.connect(removed.append)
    choose(page, "ct1")
    page.remove()
    title, text, _ = widgets.SHOWN[-1]
    assert title == "Remove host" and "ct1" in text
    assert [e["name"] for e in sai.host_entries()] == ["local", "ct1"] and removed == [], "declined: still listed"
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    page.remove()
    assert [e["name"] for e in sai.host_entries()] == ["local"] and removed == ["ct1"]
    assert page.table.row_keys == ["local"]
    page.close()


def test_edit_host_changes_what_hosts_set_changes(qapp, env, monkeypatch):
    """Edit… runs `sandbox-ai hosts set NAME --update-sudo on|off`; the address and the key are only shown."""
    import sandbox_ai_app.hosts_page as hp
    sai = backend.SandboxAI()
    if not sai.hosts_supported():
        pytest.skip("this sandbox-ai has no hosts.py")
    sai.host_add("ct1", "admin@192.0.2.10", 22, "", "")
    dialog = hp.EditHostDialog(sai.host_entries()[1])
    assert dialog.windowTitle() == "Edit ct1" and not dialog.sudo.isChecked()
    assert not dialog.findChildren(QtWidgets.QLineEdit), "SSH, port and key are not edited here"
    dialog.deleteLater()

    class Accepting(hp.EditHostDialog):
        def exec(self):
            self.sudo.setChecked(True)
            return QtWidgets.QDialog.DialogCode.Accepted
    monkeypatch.setattr(hp, "EditHostDialog", Accepting)
    page = HostsPage(sai, lambda argv, title: None)
    choose(page, "ct1")
    page.edit()
    assert sai.host_entries()[1].get("update_sudo") is True
    page.close()


def test_a_hosts_defaults_and_a_step_down_in_security_asks_first(qapp, env, monkeypatch):
    """The Defaults tab reads `sandbox-ai defaults show --json` of the chosen host when the page is shown; saving
    writes each change with defaults set/unset there; a lower security mode, fewer canaries, more telemetry or an
    expiry of kept starts is asked first (Cancel the default)."""
    import sandbox_ai_app.hosts_page as hp
    sai = backend.SandboxAI()
    if not sai.hosts_supported():
        pytest.skip("this sandbox-ai has no hosts.py")
    sai.host_add("ct1", "admin@192.0.2.10", 22, "", "")
    asked = []
    shown = {"path": "/home/admin/.config/sandbox-ai/defaults.json",
             "values": {"mode": "high", "canaries": ["credentials", "home"], "telemetry": "off"},
             "telemetry_levels": ["off", "strict", "on"], "modes": list(hp.MODES),
             "canary_categories": ["credentials", "home", "workspace"], "canary_default": ["credentials", "home"]}

    def host_run(name, *args, timeout=120):
        asked.append((name, args))
        return 0, json.dumps(shown) if args[:2] == ("defaults", "show") else "ok", ""
    monkeypatch.setattr(sai, "host_run", host_run)
    monkeypatch.setattr(widgets, "SHOWN", [])
    page = HostsPage(sai, lambda argv, title: None)
    choose(page, "ct1")
    assert asked == [], "not shown yet: nothing asked"
    page.show()
    assert ("ct1", ("defaults", "show", "--json")) in asked
    assert page.mode.currentData() == "high" and page.canaries.currentData() == hp.CANARY_LIST
    assert page.canary_list.text() == "credentials, home" and page.telemetry.currentData() == "off"
    page.mode.setCurrentIndex(page.mode.findData("low"))
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)
    page.save_defaults()
    title, text, _ = widgets.SHOWN[-1]
    assert title.startswith("Defaults of ct1") and "from high down to low" in text
    assert not [a for _, a in asked if a[:2] == ("defaults", "set")], "declined: nothing written"
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    page.save_defaults()
    assert ("ct1", ("defaults", "set", "mode", '"low"')) in asked
    # Stricter: written without a question.
    page.mode.setCurrentIndex(page.mode.findData("paranoid"))
    widgets.SHOWN.clear()
    page.save_defaults()
    assert ("ct1", ("defaults", "set", "mode", '"paranoid"')) in asked and not widgets.SHOWN
    assert hp.security_steps_down({"canaries": ["credentials", "home"]}, {"canaries": False})
    assert hp.security_steps_down({}, {"keep_days": 30}) and not hp.security_steps_down({}, {"mode": "high"})
    assert not hp.security_steps_down({}, {"telemetry": "on"}) and hp.security_steps_down({}, {"mode": "low"})
    assert page.telemetry.findData("on") < 0, "telemetry is always off: on is never offered"
    page.close()


def test_kept_starts_expire_by_default_and_unchecked_is_never(qapp, env, monkeypatch):
    """No keep_days on the host: kept starts go after 90 days unused (sandbox-ai's default), shown checked with 90 and
    left unwritten; unchecked writes keep_days 0 (never), a keep_days 0 there shows unchecked. 90 -> 30 days, or
    never -> unset (90), steps down; never does not."""
    import sandbox_ai_app.hosts_page as hp
    sai = backend.SandboxAI()
    if not sai.hosts_supported():
        pytest.skip("this sandbox-ai has no hosts.py")
    sai.host_add("ct1", "admin@192.0.2.10", 22, "", "")
    asked = []
    shown = {"path": "/x/defaults.json", "values": {"mode": "high"}, "telemetry_levels": ["off", "strict"],
             "modes": list(hp.MODES), "canary_categories": ["credentials", "home"], "canary_default": ["credentials"]}

    def host_run(name, *args, timeout=120):
        asked.append((name, args))
        return 0, json.dumps(shown) if args[:2] == ("defaults", "show") else "ok", ""
    monkeypatch.setattr(sai, "host_run", host_run)
    monkeypatch.setattr(widgets, "SHOWN", [])
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    page = HostsPage(sai, lambda argv, title: None)
    choose(page, "ct1")
    page.show()
    assert page.keep.isChecked() and page.keep_days.value() == 90
    assert "keep_days" not in page.chosen_defaults(), "the default stays unwritten"
    page.keep.setChecked(False)
    page.save_defaults()
    assert ("ct1", ("defaults", "set", "keep_days", "0")) in asked
    shown["values"] = {"mode": "high", "keep_days": 0}
    page.load_defaults()
    assert not page.keep.isChecked()
    assert hp.security_steps_down({}, {"keep_days": 30}) and not hp.security_steps_down({}, {"keep_days": 0})
    assert hp.security_steps_down({"keep_days": 0}, {}) and not hp.security_steps_down({"keep_days": 30}, {})
    page.close()


def test_steps_down_follow_the_webs_order_and_the_hosts_own_canaries_and_a_half_save_is_said(qapp, env, monkeypatch):
    """Review round 3 (A4): telemetry was ranked off > strict > on, so strict -> off saved without a question; the
    canaries were compared with a fixed default, not the host's own; a refused `defaults set` left the others applied
    unsaid."""
    import sandbox_ai_app.hosts_page as hp
    assert hp.security_steps_down({"telemetry": "strict"}, {"telemetry": "off"}), "off is less private than strict"
    assert not hp.security_steps_down({"telemetry": "off"}, {"telemetry": "strict"})
    assert hp.security_steps_down({}, {"canaries": ["credentials", "home"]},
                                  canary_default=["credentials", "home", "workspace"]), "that host plants workspace too"
    assert not hp.security_steps_down({}, {"canaries": ["credentials", "home"]}, canary_default=["credentials", "home"])
    assert hp.security_steps_down({"keep_days": 30}, {"keep_days": 10}) and not hp.security_steps_down(
        {"keep_days": 10}, {"keep_days": 30})
    sai = backend.SandboxAI()
    if not sai.hosts_supported():
        pytest.skip("this sandbox-ai has no hosts.py")
    sai.host_add("ct1", "admin@192.0.2.10", 22, "", "")
    asked = []
    shown = {"path": "/x/defaults.json", "values": {"mode": "high", "telemetry": "strict"},
             "telemetry_levels": ["off", "strict", "on"], "modes": list(hp.MODES),
             "canary_categories": ["credentials", "home", "workspace"], "canary_default": ["credentials", "home"]}

    def host_run(name, *args, timeout=120):
        asked.append((name, args))
        if args[:2] == ("defaults", "show"):
            return 0, json.dumps(shown), ""
        if args[:3] == ("defaults", "unset", "telemetry"):  # off is the default: the key goes
            return 1, "", "sandbox-ai defaults: telemetry levels need a newer agent"
        return 0, "ok", ""
    monkeypatch.setattr(sai, "host_run", host_run)
    monkeypatch.setattr(widgets, "SHOWN", [])
    page = HostsPage(sai, lambda argv, title: None)
    choose(page, "ct1")
    page.show()
    page.mode.setCurrentIndex(page.mode.findData("paranoid"))
    page.telemetry.setCurrentIndex(page.telemetry.findData("off"))
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)
    page.save_defaults()
    assert "telemetry from strict to off" in widgets.SHOWN[-1][1], "asked first"
    assert not [a for _, a in asked if a[:2] == ("defaults", "set")]
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    widgets.SHOWN.clear()
    page.save_defaults()
    title, text, _ = widgets.SHOWN[-1]
    assert ("ct1", ("defaults", "set", "mode", '"paranoid"')) in asked
    assert text.startswith("Saved on ct1") and 'mode = "paranoid"' in text, text
    assert "Refused: telemetry" in text and "newer agent" in text and "Not tried: nothing" in text
    page.close()


def test_versions_update_and_update_all(qapp, env, monkeypatch):
    sai = backend.SandboxAI()
    if not sai.hosts_supported() or not sai.host_update_supported():
        pytest.skip("this sandbox-ai cannot update hosts")
    sai.host_add("ct1", "admin@192.0.2.10", 22, "", "")
    sai.host_add("ct2", "admin@192.0.2.11", 22, "", "")
    monkeypatch.setattr(sai, "host_statuses", lambda refresh=False, cached=True: {
        "ct1": {"version": "0.1.60", "available": "0.1.66", "update": True, "kind": "user"},
        "ct2": {"version": "0.1.66", "available": "0.1.66", "update": False, "kind": "system"}})
    asked, opened = [], []

    def host_update(name, sudo=None):
        asked.append((name, sudo))
        if name == "ct2" and not sudo:
            return {"name": name, "before": "0.1.66", "after": None, "error": "belongs to root", "needs_sudo": True}
        if name == "ct2":
            return {"name": name, "before": "0.1.66", "after": None, "error": "sudo wants a password",
                    "needs_password": True}
        return {"name": name, "before": "0.1.60", "after": "0.1.66", "changed": True, "error": None,
                "fixed": [{"path": "/home/u/.ssh/id_ct1", "was": "0644", "now": "0600"}]}
    monkeypatch.setattr(sai, "host_update", host_update)
    monkeypatch.setattr(sai, "host_update_terminal", lambda name: ["ssh", "-tt", name, "sudo"])
    monkeypatch.setattr(sai, "host_run", lambda name, *args, timeout=120: (0, "[]" if args[0] != "defaults" else "{}", ""))
    page = HostsPage(sai, lambda argv, title: opened.append((argv, title)))
    texts = [page.table.item(r, 3).text() for r in range(page.table.rowCount())]
    assert "0.1.60, update: 0.1.66" in texts[1] and "0.1.66, up to date" in texts[2], texts
    choose(page, "ct1")
    page.update_host()
    assert asked == [("ct1", None)]
    log = page.updates.toPlainText()
    assert "ct1: 0.1.60 -> 0.1.66" in log and "made private (0600)" in log and "1 hosts: 1 updated" in log
    # one host whose installation is root's: asked whether with sudo (yes), then sudo wants a password: a terminal
    choose(page, "ct2")
    page.update_host()
    assert asked[-2:] == [("ct2", None), ("ct2", True)]
    assert opened == [(["ssh", "-tt", "ct2", "sudo"], "ct2: update")]
    assert "not updated: sudo wants a password" in page.updates.toPlainText()
    # update all: one after the other, a summary
    asked.clear()
    page.update_all()
    assert [name for name, _ in asked] == ["ct1", "ct2"]
    assert "2 hosts: 1 updated, 0 up to date, 1 not updated" in page.updates.toPlainText()
    choose(page, "local")
    page.update_host()  # this machine: not over SSH
    page.close()



def test_a_hosts_doctor_is_headed_by_its_sandbox_ai_version(qapp, env, monkeypatch):
    """The Doctor tab names the sandbox-ai that answered: its version, where it is installed, its Python (the doctor's
    first check); for an older host whose doctor lacks it, the version the hosts list knows, said so."""
    sai = backend.SandboxAI()
    if not sai.hosts_supported():
        pytest.skip("this sandbox-ai has no hosts.py")
    sai.host_add("ct1", "admin@192.0.2.10", 22, "", "")
    monkeypatch.setattr(sai, "host_statuses", lambda refresh=False, cached=True: {
        "ct1": {"version": "0.1.60", "available": "0.1.66", "update": True, "kind": "system", "prefix": "/usr/local"}})
    monkeypatch.setattr(sai, "host_run", lambda name, *args, timeout=120: (0, "{}", ""))
    answers = [[{"status": "ok", "check": "Python", "detail": "3.11", "group": "System"}]]
    monkeypatch.setattr(sai, "host_doctor", lambda name, engine="podman": answers[-1])
    page = HostsPage(sai, lambda argv, title: None)
    choose(page, "ct1")
    page.check_host()
    assert page.doctor.version_line.text() == \
        "sandbox-ai 0.1.60 on ct1 — system installation in /usr/local (from the hosts list)"
    answers.append([{"id": "sandbox-ai-version", "status": "ok", "check": "sandbox-ai", "detail": "0.1.80, …",
                     "group": "System", "version": "0.1.80", "kind": "user",
                     "path": "/home/admin/.local/share/sandbox-ai/versions/0.1.80", "python": "3.12.3"}])
    page.check_host()
    assert page.doctor.version_line.text() == ("sandbox-ai 0.1.80 on ct1 — the user's own installation "
                                               "(/home/admin/.local/share/sandbox-ai/versions/0.1.80), Python 3.12.3")
    page.close()

def test_update_from_this_machines_copy_asks_first_and_is_offered_when_no_release_is_published(qapp, env, monkeypatch):
    """User request: next to the release update, `sandbox-ai hosts update NAME --from-here` (this machine's copy, as
    the installation the host has), asked first like an install and with the sudo choice beside Update; an update
    that finds no published release offers it at once; a sudo that wants a password installs in a terminal tab."""
    sai = backend.SandboxAI()
    if not sai.hosts_supported() or not sai.host_update_from_here_supported():
        pytest.skip("this sandbox-ai cannot update hosts from its own copy")
    sai.host_add("ct1", "admin@192.0.2.10", 22, "", "")
    monkeypatch.setattr(sai, "host_statuses", lambda refresh=False, cached=True: {
        "ct1": {"version": "0.1.60", "available": None, "update": None, "kind": "system", "prefix": "/opt/sai"}})
    monkeypatch.setattr(sai, "host_run", lambda name, *args, timeout=120: (0, "{}", ""))
    asked, opened, answers = [], [], []

    def host_update(name, sudo=None, from_here=False):
        asked.append((name, sudo, from_here))
        return answers.pop(0)
    monkeypatch.setattr(sai, "host_update", host_update)
    monkeypatch.setattr(widgets, "SHOWN", [])
    page = HostsPage(sai, lambda argv, title: opened.append((argv, title)))
    choose(page, "ct1")
    assert page.from_here_button.isEnabled() and page.from_here_button.accessibleName()
    # Asked first, naming the host; no: nothing runs.
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)
    page.from_here_button.click()
    assert asked == [] and "ct1" in widgets.SHOWN[-1][1] and "this machine's sandbox-ai" in widgets.SHOWN[-1][1]
    # Yes: hosts update ct1 --from-here, with the sudo chosen beside Update.
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    page.update_sudo.setCurrentIndex(page.update_sudo.findData(True))
    answers.append({"name": "ct1", "before": "0.1.60", "after": "0.1.78", "changed": True, "error": None,
                    "notes": ["from this machine's copy (hosts install --from-here)"]})
    page.from_here_button.click()
    assert asked == [("ct1", True, True)]
    assert "ct1: 0.1.60 -> 0.1.78" in page.updates.toPlainText() and "from this machine's copy" in \
        page.updates.toPlainText()
    # The release update finds none published: this machine's copy is offered at once (asked), then runs.
    asked.clear()
    widgets.SHOWN.clear()
    page.update_sudo.setCurrentIndex(0)
    answers += [{"name": "ct1", "before": "0.1.60", "after": None, "error": "update check: no published release; "
                 "update it with this machine's copy instead: sandbox-ai hosts update ct1 --from-here"},
                {"name": "ct1", "before": "0.1.60", "after": None, "error": "sudo wants a password",
                 "needs_password": True, "kind": "system"}]
    page.update_host()
    assert asked == [("ct1", None, False), ("ct1", None, True)]
    assert len(widgets.SHOWN) == 1 and "No release of sandbox-ai is published" in widgets.SHOWN[0][1]
    log = page.updates.toPlainText()
    assert log.count("no published release") == 1 and "1 hosts: 0 updated, 0 up to date, 1 not updated" in log
    # sudo there wants a password: this machine's copy installed in a terminal tab, as the system installation it is
    assert [argv[3:] for argv, _ in opened] == [["hosts", "install", "ct1", "--system", "--prefix=/opt/sai", "--sudo",
                                                 "--from-here"]]
    # Declined: the failed release update stays as it is.
    asked.clear()
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)
    answers.append({"name": "ct1", "before": "0.1.60", "after": None, "error": "no published release"})
    page.update_host()
    assert asked == [("ct1", None, False)] and page.updates.toPlainText().count("no published release") == 1
    choose(page, "local")
    assert not page.from_here_button.isEnabled(), "this machine updates with sandbox-ai update"
    page.close()


def test_host_update_from_here_is_the_launchers_option():
    sai = backend.SandboxAI()
    if not sai.hosts_supported() or not sai.host_update_from_here_supported():
        pytest.skip("this sandbox-ai cannot update hosts from its own copy")
    calls = []
    sai.call = lambda *args, timeout=120, input=None: calls.append(args) or (0, json.dumps([{"name": "ct1"}]), "")
    assert sai.host_update("ct1", True, from_here=True) == {"name": "ct1"}
    assert sai.host_update("ct1") == {"name": "ct1"}
    assert calls == [("hosts", "update", "ct1", "--json", "--sudo", "--from-here"), ("hosts", "update", "ct1", "--json")]
    with pytest.raises(ValueError):
        sai.host_update("local", from_here=True)


def test_update_all_counts_the_hosts_as_everything_else_does(qapp, env, monkeypatch):
    """Update all took the hosts of kind "ssh" only; every other place counts each host but this machine."""
    from sandbox_ai_app import hosts_page
    assert hosts_page.is_remote({"name": "ct1", "kind": "ssh"}) and hosts_page.is_remote({"name": "ct2", "kind": "lxc"})
    assert not hosts_page.is_remote({"name": "local", "kind": "local"}) and not hosts_page.is_remote({"name": "local"})


def test_install_sandbox_ai_on_a_host_for_the_user_or_the_system_and_a_password_in_a_terminal(qapp, env, monkeypatch):
    """User request: install sandbox-ai on an added host - for the SSH user or every user, as root, with sudo or
    without, from this machine's copy or the signed release - through `sandbox-ai hosts install` on this machine;
    asked first naming the host, where and as whom; sudo that wants a password runs it in a terminal tab. Update
    takes the same sudo choice."""
    import sandbox_ai_app.hosts_page as hp
    sai = backend.SandboxAI()
    if not sai.hosts_supported() or not sai.host_install_supported():
        pytest.skip("this sandbox-ai cannot install on hosts")
    sai.host_add("ct1", "admin@192.0.2.10", 22, "", "")
    calls, opened = [], []
    answers = [{"name": "ct1", "before": None, "after": "0.1.80", "changed": True, "mode": "user", "prefix": None,
                "sudo": False, "root": False, "source": "here", "error": None, "needs_password": False,
                "output": "installed \x1b[2J", "notes": ["add ~/.local/bin to PATH"]},
               {"name": "ct1", "before": None, "after": None, "changed": False, "mode": "system",
                "prefix": "/opt/sai", "sudo": True, "root": False, "source": "download", "error": "sudo wants a password",
                "needs_password": True, "output": "", "notes": []}]

    def call(*args, timeout=120, input=None):
        calls.append(args)
        return (0 if not answers[0].get("error") else 1), json.dumps(answers.pop(0)), ""
    monkeypatch.setattr(sai, "call", call)
    monkeypatch.setattr(sai, "host_run", lambda name, *args, timeout=120: (0, "{}", ""))
    monkeypatch.setattr(sai, "host_statuses", lambda refresh=False, cached=True: {"ct1": {"version": None,
                                                                                           "error": "not found"}})
    monkeypatch.setattr(widgets, "SHOWN", [])
    page = HostsPage(sai, lambda argv, title: opened.append((argv, title)))
    page.versions = {"ct1": {"version": None, "error": "not found"}}
    choose(page, "ct1")
    assert page.install_button.isEnabled() and page.install_button.property("role") == "primary", \
        "prominent where the host has no sandbox-ai that answers"
    choices = []

    def accept_user(dialog):
        choices.append(dialog)
        assert dialog.for_user.isChecked() and "admin" in dialog.for_user.text(), "the SSH user is no root: for it"
        assert [dialog.how.itemData(i) for i in range(dialog.how.count())] == [True, False]
        return QtWidgets.QDialog.DialogCode.Accepted
    monkeypatch.setattr(hp.InstallDialog, "exec", accept_user)
    page.install()
    title, text, _ = widgets.SHOWN[0]
    assert title == "Install sandbox-ai on ct1" and "for the SSH user" in text and "this machine's copy" in text
    assert calls[-1] == ("hosts", "install", "ct1", "--user", "--from-here", "--json")
    title, text, details = widgets.SHOWN[-1]
    assert "ct1: sandbox-ai - -> 0.1.80" in text and "add ~/.local/bin to PATH" in text
    assert details == "installed \\x1b[2J", "the output, escaped, in the details"

    def accept_system(dialog):
        dialog.for_system.setChecked(True)
        dialog.prefix.setText("/opt/sai")
        dialog.how.setCurrentIndex(dialog.how.findData(True))
        dialog.download.setChecked(True)
        dialog.version.setText("0.1.80")
        return QtWidgets.QDialog.DialogCode.Accepted
    monkeypatch.setattr(hp.InstallDialog, "exec", accept_system)
    widgets.SHOWN.clear()
    page.install()
    assert "for every user under /opt/sai with sudo" in widgets.SHOWN[0][1]
    assert calls[-1] == ("hosts", "install", "ct1", "--system", "--prefix=/opt/sai", "--sudo", "--download",
                         "--version=0.1.80", "--json")
    argv, title = opened[-1]
    assert argv[-7:] == ["install", "ct1", "--system", "--prefix=/opt/sai", "--sudo", "--download",
                         "--version=0.1.80"] and "--json" not in argv, "sudo asks for its password there"
    assert title == "ct1: install"
    with pytest.raises(ValueError):
        sai.host_install_args("ct1", "user", prefix="/opt")  # a prefix is for the system
    with pytest.raises(ValueError):
        sai.host_install_args("-x")
    # Update with the sudo chosen beside it.
    updates = []
    monkeypatch.setattr(sai, "host_update", lambda name, sudo=None: updates.append((name, sudo)) or {
        "name": name, "before": "0.1.79", "after": "0.1.80", "changed": True, "error": None})
    page.update_sudo.setCurrentIndex(page.update_sudo.findData(False))
    page.update_host()
    assert updates == [("ct1", False)]
    page.close()


# -- the version after an update, a host's installations, Uninstall… (user reports 2026-11-23) --------------------------

FAKE_SSH = r'''#!PYTHON
import os, subprocess, sys
args = sys.argv[1:]
if "-O" in args or "-G" in args or "--" not in args:
    sys.exit(255)
home = REMOTE
env = {"HOME": home, "PATH": os.path.join(home, "rbin") + ":/usr/bin:/bin"}
sys.exit(subprocess.run(["sh", "-c", args[args.index("--") + 2]], env=env, cwd=home).returncode)
'''
# The host's sandbox-ai (the user's own, ~/.local/bin): its version in ~/version, its update writes ~/available
# there; from 0.1.66 on its claude --help lists --toolchain.
FAKE_SANDBOX_AI = r'''#!PYTHON
import os, sys
args = sys.argv[1:]
home = os.environ["HOME"]
def read(name, default):
    path = os.path.join(home, name)
    return open(path).read().strip() if os.path.exists(path) else default
version = read("version", "0.1.30")
if args == ["--version"]:
    print("sandbox-ai " + version)
elif args == ["version", "--verbose"]:
    print("sandbox-ai " + version)
    print("installed in: " + home + "/.local/share/sandbox-ai/versions/" + version)
    print("updates: must be signed by one of these keys (sandbox-ai update)")
elif args == ["update", "--check"]:
    print("installed: " + version + " (somewhere)")
    print("available: 0.1.66 (https://github.com/c0decave/sandbox-ai)")
elif args[:1] == ["update"]:
    open(os.path.join(home, "version"), "w").write("0.1.66")
    print("install.sh: installed sandbox-ai 0.1.66 (was " + version + ")", file=sys.stderr)
elif args[-1:] == ["--help"]:
    print("usage: sandbox-ai claude [--mode MODE]" + (" [--toolchain NAME]" if version >= "0.1.66" else ""))
else:
    print("{}")
'''


@pytest.fixture
def ct1(env, monkeypatch):
    """The host ct1 through a stand-in ssh: its user's own sandbox-ai (FAKE_SANDBOX_AI), version 0.1.30."""
    import sys
    remote = env / "remote-home"
    (remote / ".local/bin").mkdir(parents=True)
    (remote / "rbin").mkdir()
    bin_dir = env / "bin"
    bin_dir.mkdir()
    (bin_dir / "ssh").write_text(FAKE_SSH.replace("PYTHON", sys.executable, 1).replace("REMOTE", repr(str(remote))))
    (bin_dir / "ssh").chmod(0o755)
    (remote / ".local/bin/sandbox-ai").write_text(FAKE_SANDBOX_AI.replace("PYTHON", sys.executable, 1))
    (remote / ".local/bin/sandbox-ai").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}")
    monkeypatch.setenv("SANDBOX_AI_SSH_MULTIPLEX", "0")
    monkeypatch.setenv("HOME", str(env / "local-home"))
    (env / "local-home").mkdir()
    sai = backend.SandboxAI()
    if not sai.hosts_supported() or not sai.host_uninstall_supported():
        pytest.skip("this sandbox-ai cannot uninstall on hosts")
    sai.host_add("ct1", "admin@ct1.example", 22, "", "")
    return sai, remote


def version_cell(page, name):
    return page.table.item(widgets.find_row(page.table, name), 3).text()


def test_after_an_update_the_column_shows_the_new_version_at_once(qapp, ct1, monkeypatch):
    """The user: "wenn man unter Hosts updated hat, verschwindet die Version in der Spalte" - hosts update forgot the
    host's kept status, and the page then read the kept statuses (no SSH) and found none. Now the update reads the
    version again: the column names it at once. This machine's own row names its version too ("auf dem Host selber
    steht auch keine Version": the row of the machine the app runs on was always empty)."""
    sai, remote = ct1
    page = HostsPage(sai, lambda argv, title: None)
    page.load_versions(refresh=True)
    assert version_cell(page, "ct1").startswith("0.1.30, update: 0.1.66"), version_cell(page, "ct1")
    assert version_cell(page, "local") == sai.version()
    choose(page, "ct1")
    page.update_host()
    assert "ct1: 0.1.30 -> 0.1.66" in page.updates.toPlainText(), page.updates.toPlainText()
    assert version_cell(page, "ct1").startswith("0.1.66"), version_cell(page, "ct1")
    page.close()


def test_a_start_option_is_never_refused_by_the_help_of_the_host_before_its_update(qapp, ct1):
    """coding-101: after its update the Board refused a start with --toolchain ("kennt diese Optionen nicht") - the
    app had kept the host's claude --help of before. An update drops what was kept, and an option still missing
    is asked of the host once more before a start is refused (also when it was updated elsewhere)."""
    sai, remote = ct1
    with pytest.raises(backend.HostTooOld):
        sai.require_options("ct1", ("claude",), ["--toolchain"])
    assert sai.host_update("ct1")["after"] == "0.1.66"
    sai.require_options("ct1", ("claude",), ["--toolchain"])  # (refused before the fix: the old help was kept)
    # updated elsewhere (its own update mode, a terminal there): the kept help is old, the host asked again
    (remote / "version").write_text("0.1.30")
    sai.host_command_options("ct1", "claude", fresh=True)
    assert "--toolchain" not in sai.host_command_options("ct1", "claude")  # (kept: the help of before)
    (remote / "version").write_text("0.1.66")
    sai.require_options("ct1", ("claude",), ["--toolchain"])


TWO = [{"kind": "user", "version": "0.1.188", "path": "/home/u/.local/share/sandbox-ai/versions/0.1.188",
        "executable": "/home/u/.local/share/sandbox-ai/current/sandbox-ai", "terminal": True, "commands": True},
       {"kind": "system", "version": "0.1.234", "path": "/usr/local/lib/sandbox-ai/versions/0.1.234",
        "prefix": "/usr/local", "executable": "/usr/local/lib/sandbox-ai/current/sandbox-ai", "terminal": False,
        "commands": False}]


def test_a_host_with_two_installations_names_both_and_uninstalls_the_one_chosen(qapp, env, monkeypatch):
    """The user: "unter Hosts sollte es auch Uninstall geben", "uninstall user/root sodass man nachher nur noch eine
    systemweite Install hat". The version column names both installations; Uninstall… (a button and in the
    right-click menu) asks which, with the host's own table of what goes, then asks once more; the host stays in the
    list. This machine's row cannot be uninstalled from here (the app runs from it)."""
    import sandbox_ai_app.hosts_page as hp
    sai = backend.SandboxAI()
    if not sai.hosts_supported() or not sai.host_uninstall_supported():
        pytest.skip("this sandbox-ai cannot uninstall on hosts")
    sai.host_add("ct1", "admin@192.0.2.10", 22, "", "")
    statuses = {"ct1": {"version": "0.1.188", "kind": "user", "update": False, "available": "0.1.188",
                        "installations": TWO}}
    monkeypatch.setattr(sai, "host_statuses", lambda refresh=False, cached=True: statuses)
    monkeypatch.setattr(sai, "host_run", lambda name, *args, timeout=120: (0, "{}", ""))
    plans, removed, opened = [], [], []
    monkeypatch.setattr(sai, "host_uninstall_plan", lambda name, **values: plans.append((name, values)) or {
        "plan": "WHAT  SIZE  WHERE\nCommand links  -  ~/.local/bin/sandbox-ai", "sudo": values.get("installation") == "system",
        "error": None})

    def uninstall(name, **values):
        removed.append((name, values))
        statuses["ct1"] = {"version": "0.1.234", "kind": "system", "update": False, "available": "0.1.234",
                           "installations": [dict(TWO[1], commands=True)]}
        return {"name": name, "error": None, "installation": TWO[0], "removed": TWO[0],
                "remaining": [{"kind": "system", "version": "0.1.234"}], "removed_from_list": False}
    monkeypatch.setattr(sai, "host_uninstall", uninstall)

    class Choosing(hp.UninstallDialog):
        def exec(self):
            assert self.ok.isEnabled() is False, "nothing chosen yet: nothing to uninstall"
            self.choices[0][0].setChecked(True)  # the user's own
            assert "Command links" in self.table.toPlainText() and self.ok.isEnabled()
            Choosing.seen = self
            return QtWidgets.QDialog.DialogCode.Accepted
    monkeypatch.setattr(hp, "UninstallDialog", Choosing)
    page = HostsPage(sai, lambda argv, title: opened.append((argv, title)))
    assert version_cell(page, "ct1") == "0.1.188 (user), up to date; system 0.1.234", version_cell(page, "ct1")
    choose(page, "ct1")
    assert page.uninstall_button.isEnabled()
    menu = [action.text() for action in widgets.menu_of(page.table).actions()]
    assert "Uninstall…" in menu, menu
    page.uninstall()
    assert plans[-1] == ("ct1", {"installation": "user", "purge": False, "secrets": False, "mcp_data": False})
    assert removed == [("ct1", {"installation": "user", "purge": False, "secrets": False, "mcp_data": False,
                                "remove": False})]
    assert "ct1: removed the user installation 0.1.188" in page.updates.toPlainText()
    assert "there now: system 0.1.234" in page.updates.toPlainText()
    assert version_cell(page, "ct1") == "0.1.234, up to date" and page.table.row_keys == ["local", "ct1"]
    # sudo there wants a password: the same uninstall in a terminal tab
    monkeypatch.setattr(sai, "host_uninstall", lambda name, **values: {
        "name": name, "error": "sudo on ct1 wants a password", "needs_password": True, "installation": TWO[1]})
    monkeypatch.setattr(sai, "host_uninstall_terminal", lambda name, *args: ["ssh", "-tt", name, "sudo", *map(str, args)])

    class System(hp.UninstallDialog):
        def exec(self):
            self.choices[-1][0].setChecked(True)
            return QtWidgets.QDialog.DialogCode.Accepted
    monkeypatch.setattr(hp, "UninstallDialog", System)
    page.uninstall()
    assert opened and opened[-1][1] == "ct1: uninstall" and opened[-1][0][:4] == ["ssh", "-tt", "ct1", "sudo"]
    choose(page, "local")
    assert not page.uninstall_button.isEnabled(), "this machine: sandbox-ai uninstall in a terminal"
    page.close()


def test_update_asks_which_installation_when_a_host_has_two(qapp, env, monkeypatch):
    """coding-101's Update went to the system installation while a terminal there ran the user's own: with two, the
    page asks which (the one the host's commands run first), and the update takes that one."""
    sai = backend.SandboxAI()
    if not sai.hosts_supported() or not sai.host_uninstall_supported():
        pytest.skip("this sandbox-ai cannot update a host's installation of choice")
    sai.host_add("ct1", "admin@192.0.2.10", 22, "", "")
    monkeypatch.setattr(sai, "host_statuses", lambda refresh=False, cached=True: {
        "ct1": {"version": "0.1.188", "kind": "user", "installations": TWO}})
    monkeypatch.setattr(sai, "host_run", lambda name, *args, timeout=120: (0, "{}", ""))
    asked = []
    monkeypatch.setattr(sai, "host_update", lambda name, sudo=None, *rest, installation=None: asked.append(
        (name, installation)) or {"name": name, "before": "0.1.188", "after": "0.1.236", "changed": True,
                                  "error": None})
    monkeypatch.setattr(widgets, "SHOWN", [])
    page = HostsPage(sai, lambda argv, title: None)
    choose(page, "ct1")
    monkeypatch.setattr(widgets, "AUTO_CHOICE", "system")
    page.update_host()
    assert asked == [("ct1", "system")]
    assert any("has 2 installations" in text for _, text, _ in widgets.SHOWN)
    monkeypatch.setattr(widgets, "AUTO_CHOICE", "all")
    asked.clear()
    page.update_host()
    assert asked == [("ct1", "user"), ("ct1", "system")]
    page.close()
