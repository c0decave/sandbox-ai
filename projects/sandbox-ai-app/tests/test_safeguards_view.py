"""Safeguards in the app, offscreen: the Safeguards tab (warnings in the theme's warning colour, errors in its error
colour, the record and its fix below), the popups from running sandboxes' new records (grouped, rate-limited,
muted kinds) and the box per kind in the settings; once with fakes, once with the checkout in SANDBOX_AI_HOME."""
import importlib.util
import json
import os
from pathlib import Path

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, i18n, safeguards_view, theme  # noqa: E402
from sandbox_ai_app.safeguards_view import SafeguardAlerts, SafeguardMute, SafeguardsView, popup  # noqa: E402

HOME = os.environ.get("SANDBOX_AI_HOME")
needs_home = pytest.mark.skipif(not HOME, reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")

FIREWALL = {"v": 1, "time": "2026-11-07T10:00:05Z", "run": "claude-1", "kind": "firewall", "level": "warning",
            "what": "tcp to 203.0.113.7:443", "why": "not allowed by the run's network options: the firewall drops it",
            "fix": "sandbox-ai net allow claude-1 --allow-tcp 203.0.113.7:443", "stays": False, "count": 1,
            "process": "curl (pid 41): curl https://x.example", "target": "203.0.113.7", "source": "firewall",
            "id": "a1"}
OOM = {"v": 1, "time": "2026-11-07T10:01:00Z", "run": "claude-1", "kind": "oom", "level": "error",
       "what": "the kernel ended 1 process of claude-1", "why": "the sandbox ran out of memory (peak 7.9 GiB)",
       "fix": "start with a larger --memory (e.g. --memory 16g)", "stays": False, "count": 1, "source": "launcher",
       "id": "b2"}
POLICY = {"v": 1, "time": "2026-11-07T09:59:00Z", "run": "codex-2", "kind": "policy", "level": "warning",
          "what": "sandbox-ai net allow --allow", "why": "--allow is not allowed by the administrator's policy",
          "fix": "the administrator's policy refuses it: it stays refused", "stays": True, "count": 1, "id": "c3"}


class FakeSai:
    def __init__(self, records=(), error=None, has=True):
        self.records, self.error, self.has, self.calls = list(records), error, has, 0

    def has_safeguards(self):
        return self.has

    def safeguards(self, run=None, tail=1000):
        self.calls += 1
        if self.error:
            raise ValueError(self.error)
        return list(self.records)


def test_records_are_listed_newest_first_in_the_themes_colours(qapp, wait):
    view = SafeguardsView(FakeSai([FIREWALL, OOM, POLICY]))
    counted = []
    view.counted.connect(counted.append)
    view.refresh()
    assert wait(lambda: view.tree.topLevelItemCount() == 3)
    rows = [view.tree.topLevelItem(i) for i in range(3)]
    assert [r.text(2) for r in rows] == ["out of memory: the kernel ended a process",
                                         "connection dropped by the firewall", "refused by the administrator's policy"]
    assert rows[0].foreground(2).color() == theme.color("bad") and rows[1].foreground(2).color() == theme.color("warn")
    assert rows[1].text(5) == "sandbox-ai net allow claude-1 --allow-tcp 203.0.113.7:443"
    assert rows[2].text(5).startswith("stays refused: ")
    assert view.status.text() == "3 record(s) in 2 run(s) · 1 error(s)" and counted[-1] == 3
    view.tree.setCurrentItem(rows[1])
    detail = view.detail.toPlainText()
    assert "curl (pid 41)" in detail and "To allow it" in detail and "--allow-tcp 203.0.113.7:443" in detail
    assert view.copy_button.isEnabled()
    view.copy_fix()
    assert QtWidgets.QApplication.clipboard().text() == FIREWALL["fix"]
    view.tree.setCurrentItem(rows[2])
    assert not view.copy_button.isEnabled() and "Stays refused" in view.detail.toPlainText()
    view.kind.setCurrentIndex(view.kind.findData("oom"))
    assert view.tree.topLevelItemCount() == 1 and view.status.text().startswith("1 record(s)")


def test_live_records_errors_an_old_sandbox_ai_and_escapes(qapp, wait):
    view = SafeguardsView(FakeSai([FIREWALL]))
    view.refresh()
    assert wait(lambda: view.tree.topLevelItemCount() == 1)
    view.add_live([FIREWALL, OOM])  # the known one is not shown twice
    assert view.tree.topLevelItemCount() == 2
    broken = SafeguardsView(FakeSai(error="boom"))
    broken.refresh()
    assert wait(lambda: "boom" in broken.status.text())
    old = SafeguardsView(FakeSai(has=False))
    old.refresh()
    assert "no safeguards yet" in old.status.text() and old.sai.calls == 0
    odd = SafeguardsView(FakeSai())
    odd.show_records([{**FIREWALL, "what": "x\x1b[31m\ny"}])
    assert "\x1b" not in odd.tree.topLevelItem(0).text(3)
    empty = SafeguardsView(FakeSai())
    empty.show_records([])
    assert empty.status.text() == "No safeguard refused or ended anything."


def test_the_activity_page_has_a_safeguards_tab_that_counts_them(qapp):
    from sandbox_ai_app.activity_page import ActivityPage
    page = ActivityPage(FakeSai([FIREWALL]))
    page.safeguards.show_records([FIREWALL, OOM])
    index = page.tabs.indexOf(page.safeguards)
    assert page.tabs.tabText(index) == "Safeguards (2)"
    assert page.tabs.tabBar().tabTextColor(index) == theme.color("warn")


def test_popups_say_what_why_and_what_to_do_in_both_languages():
    level, title, text = popup({"kind": "firewall", "count": 1, "run": "claude-1", "level": "warning", "entry": FIREWALL})
    assert level == "warning" and title == "claude-1: connection dropped by the firewall"
    assert text.splitlines() == ["tcp to 203.0.113.7:443", "Why: not allowed by the run's network options: the "
                                 "firewall drops it", "To allow it: sandbox-ai net allow claude-1 --allow-tcp "
                                 "203.0.113.7:443"]
    level, title, text = popup({"kind": "firewall", "count": 12, "run": "claude-1", "level": "warning", "entry": FIREWALL})
    assert title == "claude-1: 12 connections to 203.0.113.7 dropped by the firewall in the last minute"
    assert popup({"kind": "oom", "count": 1, "run": "r", "level": "error", "entry": OOM})[0] == "error"
    assert popup({"kind": "summary", "count": 3, "run": "r", "kinds": ["dns"]})[1] == "r: 3 more safeguard events"
    i18n.set_language("de")
    try:
        _, title, text = popup({"kind": "firewall", "count": 12, "run": "claude-1", "entry": FIREWALL})
        assert title == "claude-1: 12 Verbindungen zu 203.0.113.7 in der letzten Minute von der Firewall verworfen"
        assert "Zum Erlauben: sandbox-ai net allow" in text
        assert popup({"kind": "policy", "count": 1, "run": "r", "entry": POLICY})[2].endswith(
            "Bleibt abgelehnt: the administrator's policy refuses it: it stays refused")
    finally:
        i18n.set_language("en")


def load_safeguards():
    root = Path(__file__).resolve().parents[3] / "sandbox" / "safeguards.py"
    spec = importlib.util.spec_from_file_location("safeguards_for_app_test", root)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now


class LiveSai:
    """The sandbox-ai side of the popups: its safeguards module, one running sandbox whose file the test writes."""

    def __init__(self, directory, muted=()):
        self.S, self.directory, self.muted_kinds = load_safeguards(), directory, set(muted)

    def has_safeguards(self):
        return True

    def module(self, name):
        assert name == "safeguards"
        return self.S

    def runs(self):
        return {"claude-1": {"run": "claude-1", "log_dir": str(self.directory)}}

    def muted_safeguards(self):
        return self.muted_kinds


def test_new_records_of_running_sandboxes_pop_up_grouped(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "setting", lambda name, settings=None: True if name == "notify_safeguards" else
                        backend.SETTINGS[name])
    sai, shown, clock = LiveSai(tmp_path, muted={"dns"}), [], Clock()
    path = tmp_path / "safeguards.jsonl"
    path.write_text(json.dumps({**FIREWALL, "id": "old"}) + "\n")  # there before the app looked: not new
    alerts = SafeguardAlerts(sai, lambda *n: shown.append(n), interval=0, clock=clock)
    arrived = []
    alerts.arrived.connect(arrived.append)
    muted, records = alerts.read()
    alerts.muted = muted
    alerts.take(records)
    assert shown == [] and arrived == []
    with open(path, "a") as stream:
        for index in range(12):
            stream.write(json.dumps({**FIREWALL, "id": f"f{index}"}) + "\n")
        stream.write(json.dumps({**FIREWALL, "kind": "dns", "id": "d1", "target": "pypi.org"}) + "\n")
        stream.write(json.dumps(OOM) + "\n")
    alerts.take(alerts.read()[1])
    assert [(level, title) for level, title, _ in shown] == [
        ("warning", "claude-1: connection dropped by the firewall"),
        ("error", "claude-1: out of memory: the kernel ended a process")]  # dns is muted
    assert len(arrived[0]) == 14  # the tab still gets them all
    clock.now += 60
    alerts.take([])
    assert shown[-1][1] == "claude-1: 12 connections to 203.0.113.7 dropped by the firewall in the last minute"
    monkeypatch.setattr(backend, "setting", lambda name, settings=None: False if name == "notify_safeguards" else
                        backend.SETTINGS[name])
    alerts.take([{**FIREWALL, "id": "x", "target": "198.51.100.1"}])
    assert len(shown) == 3  # popups off: nothing more


class MuteSai:
    def __init__(self, muted=()):
        self.S, self.saved, self.muted_kinds = load_safeguards(), [], set(muted)

    def safeguard_kinds(self):
        return {kind: spec[:2] for kind, spec in self.S.KINDS.items()}

    def muted_safeguards(self):
        return self.muted_kinds

    def save_sandbox_default(self, key, value):
        if value is not None:
            self.S.check_mute(value)
        self.saved.append((key, value))


def test_unticking_a_kind_mutes_it_for_every_start(qapp):
    sai = MuteSai(muted={"dns"})
    mute = SafeguardMute(sai)
    assert not mute.boxes["dns"].isChecked() and mute.boxes["firewall"].isChecked()
    assert len(mute.boxes) == len(safeguards_view.TITLES)
    mute.boxes["firewall"].setChecked(False)
    assert sai.saved[-1] == ("safeguard_mute", ["firewall", "dns"])
    mute.boxes["firewall"].setChecked(True)
    mute.boxes["dns"].setChecked(True)
    assert sai.saved[-1] == ("safeguard_mute", None)  # nothing muted: the key goes


def test_every_kind_has_a_title_in_the_app():
    assert set(safeguards_view.TITLES) == set(load_safeguards().KINDS)
    for kind, spec in load_safeguards().KINDS.items():
        assert safeguards_view.TITLES[kind] == spec[1]


@needs_home
def test_the_real_command_and_the_defaults_file(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    (tmp_path / "runtime").mkdir()
    logs = tmp_path / "state" / "sandbox-ai" / "logs" / "project-0123456789ab"
    logs.mkdir(parents=True)
    (logs / "safeguards.jsonl").write_text(json.dumps(FIREWALL) + "\n" + json.dumps(OOM) + "\n")
    sai = backend.SandboxAI()
    assert sai.has_safeguards() and "firewall" in sai.safeguard_kinds()
    records = sai.safeguards()
    assert [r["id"] for r in records] == ["a1", "b2"]
    assert [r["id"] for r in sai.safeguards("claude-1")] == ["a1", "b2"]
    sai.save_sandbox_default("safeguard_mute", ["dns"])
    assert json.loads(sai.sandbox_defaults_file().read_text()) == {"version": 1, "safeguard_mute": ["dns"]}
    assert sai.muted_safeguards() == {"dns"}
    sai.launcher.load_user_defaults(sai.launcher.parse_args(["claude"]))  # sandbox-ai accepts the file
    with pytest.raises(ValueError):
        sai.save_sandbox_default("safeguard_mute", ["loud"])
    sai.save_sandbox_default("safeguard_mute", None)
    assert json.loads(sai.sandbox_defaults_file().read_text()) == {"version": 1}
