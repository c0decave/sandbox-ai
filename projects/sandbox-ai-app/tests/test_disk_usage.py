"""The Sandboxes page's Disk usage…: what sandbox-ai keeps on this machine or a host (sandbox-ai gc --usage), then
Clean up… with or without the download caches of the kept HOMEs; the clean-up's list names the logs it compresses.
The real `sandbox-ai gc --usage` of the checkout in SANDBOX_AI_HOME, on a state folder written here."""
import gzip
import json
import os
from pathlib import Path
import time

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6 import QtCore  # noqa: E402
from sandbox_ai_app import backend, i18n, widgets  # noqa: E402
from sandbox_ai_app.ui import detail_lines, gc_lines, usage_lines  # noqa: E402

needs_sai = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                               reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    return tmp_path


@pytest.fixture
def window(qapp, env, monkeypatch):
    from sandbox_ai_app import terminal
    from sandbox_ai_app.ui import MainWindow
    monkeypatch.setattr(terminal, "available", lambda web=None: (False, "not in this test"))
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    w = MainWindow(backend.SandboxAI(), check_updates=False, approvals_interval=0)
    if w.tray is not None:
        w.tray.hide()
        w.tray = None
    yield w
    w.shutdown()
    w.close()
    w.deleteLater()
    QtWidgets.QApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)


def write(path, size=0, text=None, age=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text) if text is not None else path.write_bytes(b"x" * size)
    if age is not None:
        os.utime(path, (time.time() - age,) * 2)
    return path


def kept_start(env):
    """A stopped kept start with download caches and scratch work in its HOME, and an ended run's logs."""
    state = env / "state" / "sandbox-ai"
    home = state / "sessions" / "claude-big-1234" / "home"
    write(home / ".cache" / "pip" / "wheel", 300_000)
    write(home / "scratch" / "data.bin", 600_000)
    write(home / ".claude" / "projects" / "t.jsonl", 100_000)
    write(state / "sessions" / "claude-big-1234" / "session.json", text=json.dumps(
        {"agent": "claude", "options": [], "backend": "bwrap", "keep": True, "created": "2026-10-01T10:00:00Z",
         "workspace": "/srv/app", "cwd": "/", "name": "claude-big"}))
    logs = state / "logs" / "proj-0123456789ab"
    write(logs / "connections-20261101-100000-run-a.jsonl",
          text="".join(json.dumps({"time": "2026-11-01T10:00:00Z", "event": "http", "n": i}) + "\n"
                       for i in range(2000)), age=7200)
    return home, logs


USAGE = {"total": 3 << 30, "scan": {"partial": False},
         "parts": {"starts": {"bytes": 2 << 30, "count": 1, "items": [
             {"name": "claude-big\x1b[2J", "state": "stopped", "keep": True, "last_used": "2026-10-01T10:00:00Z",
              "bytes": 2 << 30, "home": {"biggest": [{"path": "pt-rev8", "bytes": 1 << 30}], "caches": {}}}]},
                   "logs": {"bytes": 1 << 30, "count": 1, "items": [
                       {"name": "proj-1", "bytes": 1 << 30, "kinds": {"files.jsonl": {"bytes": 1 << 30}}}]},
                   "mcp": {"bytes": 1 << 20}},
         "podman": {"images": {"bytes": 9 << 30, "reclaimable": 4 << 30}},
         "would_free": {"leftovers": {"count": 0, "bytes": 0}, "compress": {"bytes": 1 << 30},
                        "caches": {"count": 1, "bytes": 300 << 20},
                        "older_than": [{"days": 30, "count": 1, "bytes": 2 << 30}],
                        "images": {"count": 2, "bytes": 3 << 30}}}


def test_usage_lines_escape_and_name_what_would_free():
    lines = usage_lines(USAGE)
    text = "\n".join(lines)
    assert lines[0] == "What sandbox-ai keeps there: 3 GiB"
    assert "claude-big\\x1b[2J (stopped, kept, last used 2026-10-01 10:00)" in text and "\x1b" not in text
    assert "HOME: pt-rev8 1 GiB" in text and "proj-1: files.jsonl 1 GiB" in text
    assert "images 9 GiB (4 GiB reclaimable)" in text
    assert "Clean up…: 1 GiB of logs compressed" in text and "Clean up with the caches…" in text
    assert "keep_days 30: 1 kept start(s)" in text and "gc --images: 2 image(s)" in text
    assert usage_lines(None)[0] == "What sandbox-ai keeps there: 0 B"  # nothing said: nothing breaks
    i18n.set_language("de")
    try:
        assert usage_lines(USAGE)[0] == "Was sandbox-ai dort aufbewahrt: 3 GiB"
    finally:
        i18n.set_language("en")


def test_gc_lines_name_caches_and_compressed_logs():
    lines = gc_lines({"caches": [{"name": "claude-big", "path": ".cache/pip", "bytes": 300 << 20}],
                      "compacted": {"compressed": [{"path": "/x", "bytes": 1 << 30}], "split": []}})
    assert lines == ["the download caches in 1 kept HOME(s), 300 MiB: claude-big .cache/pip",
                     "compressed, nothing removed: 1 log(s) of ended runs and the older part of 0 large shared "
                     "log(s), 1 GiB"]


def test_usage_lines_say_the_default_expiry_and_old_log_folders():
    """The default expiry (no keep_days: kept starts unused for 90 days go, 14 days after it was first said) and how to
    switch it off; the log folders gc --logs-older-than would remove."""
    usage = json.loads(json.dumps(USAGE))
    usage["would_free"]["expiry"] = {"days": 90, "origin": "default", "count": 2, "bytes": 5 << 30,
                                     "names": ["old-one", "old-two"], "removed_from": "2026-12-01T10:00:00Z",
                                     "off": "sandbox-ai defaults set keep_days 0"}
    usage["would_free"]["logs_older_than"] = [{"days": 90, "count": 3, "bytes": 7 << 30, "folders": ["gone-1"]},
                                              {"days": 180, "count": 0, "bytes": 0, "folders": []}]
    text = "\n".join(usage_lines(usage))
    assert "2 kept start(s) unused for more than 90 days, with their HOME, 5 GiB" in text
    assert "from 2026-12-01 10:00" in text and "keep_days 0" in text and "old-one, old-two" in text
    assert "sandbox-ai gc --logs-older-than 90: 3 log folder(s)" in text and "--logs-older-than 180" not in text
    usage["would_free"]["expiry"] = {"days": None, "origin": "off", "count": 0, "bytes": 0, "names": []}
    assert "Kept starts never expire there (keep_days 0)" in "\n".join(usage_lines(usage))


def test_gc_lines_say_what_goes_later_apart():
    from sandbox_ai_app.ui import gc_pending_lines
    found = {"removed": [], "expiring": [{"name": "old\x1b", "last_used": "2026-01-01T10:00:00Z",
                                          "removed_from": "2026-12-01T10:00:00Z"}],
             "expiry": {"days": 90, "origin": "default", "off": "sandbox-ai defaults set keep_days 0"},
             "log_folders": [{"name": "gone-1", "bytes": 1 << 20, "last_written": "2026-01-01T10:00:00Z",
                              "project": "/srv/gone", "project_gone": True}]}
    assert gc_lines(found) == ["the log folder gone-1 (last written 2026-01-01 10:00, its project /srv/gone is gone), "
                               "1 MiB"]
    pending = gc_pending_lines(found)
    assert pending[0] == "not yet: the kept start old\\x1b (last used 2026-01-01 10:00) goes from 2026-12-01 10:00 on"
    assert "unused for more than 90 days" in pending[-1] and "keep_days 0" in pending[-1]
    assert gc_pending_lines({}) == []
    assert gc_lines({"rootfs": [{"sha256": "ab" * 32, "bytes": 2 << 30}]}) == [
        "the MCP root file system abababababab (no MCP server uses it), 2 GiB"]


def test_disk_usage_is_a_button_and_on_right_click(window):
    page = window.sandboxes
    assert "usage" in page.manage_buttons
    assert page.manage_buttons["usage"] in page.table.menu_entries()


@needs_sai
def test_disk_usage_then_clean_up_with_the_caches(window, env, monkeypatch, wait):
    home, logs = kept_start(env)
    page = window.sandboxes
    page.host_filter.setCurrentIndex(page.host_filter.findData("local"))
    monkeypatch.setattr(widgets, "AUTO_CHOICE", None)
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)  # shown, then Close
    widgets.SHOWN.clear()
    page.disk_usage()
    assert wait(lambda: any(title == "Disk usage on this machine" for title, *_ in widgets.SHOWN), 60)
    text = next(text for title, text, _ in widgets.SHOWN if title == "Disk usage on this machine")
    assert "claude-big (stopped, kept" in text and "proj-0123456789ab" in text and "pip" not in text.split("HOME:")[0]
    assert (home / ".cache" / "pip").exists() and not list(logs.glob("*.gz")), "only measured"
    # Clean up with the caches…: the preview names the caches and the logs, then they go
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "AUTO_CHOICE", "caches")
    widgets.SHOWN.clear()
    page.disk_usage()
    assert wait(lambda: not (home / ".cache" / "pip").exists(), 90)
    assert (home / "scratch" / "data.bin").exists() and (home / ".claude" / "projects" / "t.jsonl").exists()
    assert wait(lambda: list(logs.glob("*.jsonl.gz")), 30)
    shown = "\n".join(text for _, text, _ in widgets.SHOWN)
    assert "the download caches in 1 kept HOME(s)" in shown and "compressed, nothing removed: 1 log(s)" in shown
    packed = next(logs.glob("*.jsonl.gz"))
    assert len(gzip.decompress(packed.read_bytes()).splitlines()) == 2000


def test_usage_lines_overview_and_each_homes_kinds():
    """sandbox-ai 0.1.98's overview (biggest first, the share and what Clean up with the caches… frees) and each
    HOME by kind; an older sandbox-ai's answer without them still reads."""
    usage = json.loads(json.dumps(USAGE))
    usage["summary"] = {"total": 12 << 30, "reclaimable": 1 << 30, "rows": [
        {"key": "podman_images", "title": "Podman images", "bytes": 9 << 30, "share": 75.0, "reclaimable": 3 << 30},
        {"key": "starts", "title": "Starts (kept HOMEs)", "bytes": 2 << 30, "share": 16.7, "reclaimable": 0},
        {"key": "new\x1bpart", "title": "Some\x1b[2Jpart", "bytes": 1, "share": 0.0, "reclaimable": 0}]}
    usage["parts"]["starts"]["items"][0]["home"]["kinds"] = {"other": 1 << 30, "caches": 2 << 30, "bogus": 5,
                                                              "agent": "x"}
    text = "\n".join(usage_lines(usage))
    assert text.splitlines()[0] == "What sandbox-ai keeps there: 12 GiB"
    assert "  Podman images 9 GiB (75 %), 3 GiB reclaimable" in text
    assert "  Starts (kept HOMEs) 2 GiB (17 %)" in text and "\x1b" not in text
    assert "by kind: download caches 2 GiB, other (the agent's work) 1 GiB" in text
    i18n.set_language("de")
    try:
        german = "\n".join(usage_lines(usage))
        assert "Podman-Images 9 GiB (75 %), 3 GiB freigebbar" in german and "Download-Caches 2 GiB" in german
    finally:
        i18n.set_language("en")


DETAIL = {"name": None, "kinds": {"caches": 3 << 30, "agent": 1 << 30}, "scan": {"partial": True}, "starts": [
    {"name": "big\x1b[2J", "state": "stopped", "bytes": 4 << 30, "busy": None,
     "kinds": {"caches": 3 << 30, "agent": 1 << 30},
     "items": [{"kind": "caches", "what": "pip", "path": ".cache/pip", "bytes": 3 << 30}],
     "other": [{"path": "scratch", "bytes": 5 << 20}]},
    {"name": "held", "state": "stopped", "bytes": 0, "busy": "being started", "kinds": {}, "items": [], "other": []}]}


def test_detail_lines():
    lines = detail_lines(DETAIL)
    text = "\n".join(lines)
    assert lines[0] == "The HOMEs by kind: 4 GiB in 2 start(s)" and "measured only in part" in text
    assert "All together: download caches 3 GiB, agent state 1 GiB" in text
    assert "big\\x1b[2J (stopped): 4 GiB" in text and "\x1b" not in text
    assert "biggest: .cache/pip 3 GiB, scratch 5 MiB" in text
    assert "not read: another command" in text
    assert detail_lines(None)[0] == "The HOMEs by kind: 0 B in 0 start(s)"
    assert detail_lines({"starts": ["junk", {"name": 1, "bytes": "x", "items": "y"}]})


@needs_sai
def test_disk_usage_then_the_homes_in_depth(window, env, monkeypatch, wait):
    """Disk usage… then HOMEs in depth…: the real sandbox-ai gc --usage --detail of a kept start, by kind."""
    home, _ = kept_start(env)
    write(home / "work" / "node_modules" / "x" / "index.js", 200_000)
    page = window.sandboxes
    page.host_filter.setCurrentIndex(page.host_filter.findData("local"))
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "AUTO_CHOICE", "detail")
    widgets.SHOWN.clear()
    page.disk_usage()
    assert wait(lambda: any(title == "HOMEs in depth on this machine" for title, *_ in widgets.SHOWN), 60)
    usage = next(text for title, text, _ in widgets.SHOWN if title == "Disk usage on this machine")
    assert "by kind: " in usage and "download caches" in usage
    text = next(text for title, text, _ in widgets.SHOWN if title == "HOMEs in depth on this machine")
    assert "claude-big (stopped):" in text and "build output and temp" in text and ".cache/pip" in text
    assert (home / ".cache" / "pip").exists(), "only measured"


def test_usage_lines_name_podmans_images_by_group():
    usage = json.loads(json.dumps(USAGE))
    usage["podman"]["groups"] = {"count": 3, "bytes": 9 << 30, "groups": {
        "other": {"count": 2, "bytes": 1 << 30}, "sandbox_unused": {"count": 1, "bytes": 8 << 30},
        "in_use": {"count": 0, "bytes": 0}, "bogus\x1b": {"count": 5, "bytes": 5}, "intermediate": "junk"}}
    text = "\n".join(usage_lines(usage))
    assert "Podman's images by what they are to sandbox-ai:" in text
    lines = text.splitlines()
    first = lines.index("Podman's images by what they are to sandbox-ai:")
    assert lines[first + 1] == "  sandbox-ai's, no kept start uses them (sandbox-ai gc --images=old): 1, 8 GiB"
    assert lines[first + 2] == "  other software's (base images, yours): 2, 1 GiB" and "bogus" not in text
    i18n.set_language("de")
    try:
        assert "anderer Software (Basis-Images, Ihre eigenen): 2, 1 GiB" in "\n".join(usage_lines(usage))
    finally:
        i18n.set_language("en")
