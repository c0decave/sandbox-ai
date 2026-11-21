"""The automatic storage check for the configured hosts too (user 2026-11-19): the app runs `sandbox-ai gc --check`
on each host of `sandbox-ai hosts` in the background on the same schedule as this machine's (the host's own
storage_check once its check said it), its notice names the host, the Hosts page shows each host's last result; a
setting per host, on by default only for the hosts the app uses (the Sandboxes page found starts there).

Unit tests of storage_hosts.py, the window offscreen with the host's check stood in for, then end to end: the real
./sandbox-ai of another host (fake ssh, its own HOME with a kept start: tests/live_fakes.py) checked by the window."""
import json
import os
from pathlib import Path
import time

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, hosts_page, storage_hosts  # noqa: E402
from test_remote_session_start import fleet, window  # noqa: E402,F401 - the fixtures

HOME = os.environ.get("SANDBOX_AI_HOME")
pytestmark = pytest.mark.skipif(not HOME, reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")
GB = 1 << 30


def test_on_by_default_only_for_the_hosts_the_app_uses():
    assert storage_hosts.is_on("ct1", ["ct1"], {}) is True
    assert storage_hosts.is_on("ct2", ["ct1"], {}) is False
    settings = {"storage_hosts": {"ct1": False, "ct2": True, "bad": "yes"}}
    assert storage_hosts.is_on("ct1", ["ct1"], settings) is False and storage_hosts.is_on("ct2", [], settings)
    assert storage_hosts.choices(settings) == {"ct1": False, "ct2": True}, "a damaged value is no choice"
    assert storage_hosts.choices({"storage_hosts": ["x"]}) == {}


def test_the_schedule_is_the_hosts_own_else_this_machines():
    now = 1_000_000_000
    assert storage_hosts.due(None, "weekly", now) is True and storage_hosts.due(None, "off", now) is False
    week = {"time": now - 3 * 86400, "settings": {}}
    assert storage_hosts.due(week, "weekly", now) is False and storage_hosts.due(week, "daily", now) is True
    daily_there = {"time": now - 2 * 86400, "settings": {"storage_check": "daily"}}
    assert storage_hosts.due(daily_there, "weekly", now) is True, "the host's own storage_check"
    assert storage_hosts.due({"time": now - 30 * 86400, "settings": {"storage_check": "off"}}, "daily", now) is False
    failed = {"time": now - 3600, "error": "ssh: timed out"}
    assert storage_hosts.due(failed, "weekly", now) is False
    assert storage_hosts.due({**failed, "time": now - storage_hosts.RETRY_SECONDS}, "weekly", now) is True


def test_what_is_kept_of_a_check_is_numbers_and_printable_names():
    kept = storage_hosts.kept({"total": 6 * GB, "threshold": 5 * GB, "above": True, "settings": {
        "storage_check": "daily", "storage_threshold_gb": 5}, "grew": [{"name": "big\x1b[2J", "bytes": 3 * GB}],
        "said": [{"rule": "pack"}], "plan": [{"argv": ["gc"]}]}, now=5)
    assert kept == {"time": 5, "total": 6 * GB, "threshold": 5 * GB, "above": True, "said": 1,
                    "grew": [{"name": "big\\x1b[2J", "bytes": 3 * GB}], "settings": {"storage_check": "daily"}}
    assert storage_hosts.kept(None, ValueError("ct1: ssh: no route"), now=7) == {"time": 7,
                                                                                "error": "ct1: ssh: no route"}


def hosts_file(window, names):
    path = Path(os.environ.get("XDG_CONFIG_HOME") or Path(os.environ["HOME"]) / ".config") / "sandbox-ai" / "hosts.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "hosts": {n: {"ssh": f"me@{n}.example", "port": 22} for n in names}}))


@pytest.fixture
def checked(window, monkeypatch):
    """The window with ct1 and ct2 in its hosts list, the hosts' gc --check answered here at once."""
    hosts_file(window, ["ct1", "ct2"])
    asked = []

    def storage_check(host, name=None, dry_run=False):
        asked.append(host)
        if host == "ct2":
            raise ValueError("ct2: ssh: connect to host ct2.example port 22: No route to host")
        return {"plan": [], "total": 7 * GB, "threshold": 5 * GB, "above": True, "grew": [{"name": "big",
                "bytes": 3 * GB}], "said": [], "settings": {"storage_check": "weekly"}}
    monkeypatch.setattr(window.sai, "storage_check", storage_check)
    from sandbox_ai_app import ui
    monkeypatch.setattr(ui, "background", at_once)
    said = []
    monkeypatch.setattr(window, "notify", lambda title, text, level="warning", action=None: said.append(
        (title, text, level, action)))
    window.asked, window.said = asked, said
    return window


def at_once(function, *args, done, **_kw):
    try:
        result = function(*args)
    except Exception as exc:  # noqa: BLE001
        return done(None, exc)
    return done(result, None)


def test_the_window_checks_the_hosts_it_uses_and_names_the_host(checked):
    window = checked
    assert window.storage_hosts_on() == [], "a host the app does not use: off by default"
    window.sandboxes.remote["ct1"] = [backend.Instance("session", "host:ct1:big", "big", host="ct1", state="stopped")]
    window.fleet_changed(window.sandboxes.fleet())
    assert window.state.get(storage_hosts.USED, [], list) == ["ct1"], "remembered: it holds after a restart"
    assert window.storage_hosts_on() == ["ct1"]
    window.storage_hosts_tick()
    assert window.asked == ["ct1"]
    (title, text, level, action), = window.said
    assert title == "Storage of ct1" and "could be freed on ct1: Optimize… shows how." in text and level == "warning"
    assert "Grew since the last check: big +3" in text and action[0] == "Optimize…"
    assert window.storage_hosts_due() == [], "weekly: not again now"
    assert storage_hosts.due(window.storage_hosts_checked()["ct1"], "weekly", time.time() + 8 * 86400)


def test_the_hosts_page_shows_the_last_result_and_takes_the_setting(checked):
    window = checked
    window.state.set(storage_hosts.USED, ["ct1"])
    window.show_page("hosts")
    page = window.hosts
    page.load_hosts()  # (the list file was written after the page was made: its watch would read it in a moment)
    window.storage_hosts_tick()
    row = page.row_of("ct1")
    column = [page.table.horizontalHeaderItem(c).text() for c in range(page.table.columnCount())].index("Storage")
    assert page.table.item(row, column).text() == "7 GiB to free"
    assert "could be freed (above the threshold of 5 GiB); grew: big +3 GiB" in page.table.item(row, 0).toolTip()
    assert page.table.item(page.row_of("ct2"), column).text() == "off"
    page.table.selectRow(page.row_of("ct2"))
    assert page.storage_box.isEnabled() and not page.storage_box.isChecked()
    assert page.storage_box.toolTip(), "the setting has its hover help"
    page.storage_box.setChecked(True)
    assert backend.app_settings()["storage_hosts"] == {"ct2": True}
    window.storage_hosts_tick()
    assert window.asked[-1] == "ct2", "on now: checked"
    assert page.table.item(page.row_of("ct2"), column).text() == "no answer"
    assert "Storage check: no answer" in page.storage_note.text() and "No route to host" in page.storage_note.text()
    assert not window.storage_hosts_due(), "a host that did not answer: asked again later, not at once"
    page.table.selectRow(page.row_of("local"))
    assert not page.storage_box.isEnabled() and page.storage_note.text() == ""


def test_a_host_switched_off_is_not_checked(checked):
    window = checked
    window.state.set(storage_hosts.USED, ["ct1"])
    window.sai.save_setting("storage_hosts", {"ct1": False})
    window.storage_hosts_tick()
    assert window.asked == []


# --- end to end: the real sandbox-ai of ct1 over (fake) ssh -------------------------------------------------------------

def test_e2e_the_window_checks_a_hosts_storage_over_ssh(fleet, window, wait, monkeypatch):
    there = fleet.there
    home = there.home / ".local/state/sandbox-ai/sessions/big/home"
    (home / ".cache/pip").mkdir(parents=True)
    (home / ".cache/pip/wheel").write_bytes(b"x" * 300_000)
    (home.parent / "session.json").write_text(json.dumps({"agent": "claude", "options": [], "backend": "bwrap",
                                                          "keep": True, "workspace": str(there.workspace), "cwd": "/",
                                                          "name": "big", "created": "2026-10-01T00:00:00Z"}))
    defaults = there.home / ".config/sandbox-ai/defaults.json"
    defaults.parent.mkdir(parents=True, exist_ok=True)
    defaults.write_text(json.dumps({"storage_threshold_gb": 0.0001, "storage_check": "daily"}))
    said = []
    monkeypatch.setattr(window, "notify", lambda title, text, level="warning", action=None: said.append((title, text)))
    window.state.set(storage_hosts.USED, ["ct1"])
    assert window.storage_hosts_tick() == "ct1"
    assert wait(lambda: not getattr(window, "storage_host_running", None), 180)
    found = window.storage_hosts_checked()["ct1"]
    assert "error" not in found, found
    assert found["above"] is True and found["total"] > 0 and found["settings"] == {"storage_check": "daily"}
    assert said and said[0][0] == "Storage of ct1" and "could be freed on ct1" in said[0][1], said
    history = there.home / ".local/state/sandbox-ai/storage-history.json"
    assert history.exists(), "the check ran there and recorded the sizes there"
    assert storage_hosts.due(found, "weekly", time.time() + 2 * 86400), "the host's own schedule: daily"
    window.show_page("hosts")
    page = window.hosts
    page.table.selectRow(page.row_of("ct1"))
    assert page.storage_box.isChecked() and "could be freed" in page.storage_note.text()
    assert hosts_page.storage_cell(found).endswith("to free")
