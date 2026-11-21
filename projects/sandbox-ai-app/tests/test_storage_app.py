"""Optimize… and Storage… offscreen (user, 2026-11-19): the storage check's plan of this machine or a host and of one
start (`sandbox-ai gc --check`), a checkbox per item and Apply selected - asked first (storage-apply), then each
item's own command; the Size column with the growth on hover; the app's own check at its start and its notice;
Settings › Storage. End to end through the real sandbox-ai of this checkout on a fake kept start whose HOME has
caches, transcripts and build output; the commands of another host's plan are checked before they run."""
import gzip
import json
import os
from pathlib import Path
import time

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6 import QtCore  # noqa: E402
from sandbox_ai_app import backend, i18n, storage_view, widgets  # noqa: E402

needs_sai = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                               reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")
ASKED = []


def at_once(function, *args, done, **kw):
    """widgets.background, but on this thread."""
    try:
        result = function(*args)
    except Exception as exc:  # noqa: BLE001 - as background hands it on
        done(None, exc)
        return
    done(result, None)


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    monkeypatch.setattr(storage_view, "background", at_once)
    ASKED[:] = []

    def ask_first(parent, action, title, text, accept, detail=None, details=None, reject=None):
        assert backend.needs_confirm(action, detail) is not None, action
        ASKED.append((action, text))
        return True
    monkeypatch.setattr(storage_view, "ask_first", ask_first)
    return tmp_path


def write(path, size=0, text=None, age_days=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text) if text is not None else path.write_bytes(b"x" * size)
    if age_days is not None:
        os.utime(path, (time.time() - age_days * 86400,) * 2)
    return path


def talk(n):
    return "".join(json.dumps({"type": "user", "message": {"role": "user", "content": f"line {i} " + "w " * 40}})
                   + "\n" for i in range(n))


def kept_start(env, name="big", days=40):
    """A stopped kept start with download caches, old and new transcripts and build output in its HOME."""
    directory = env / "state" / "sandbox-ai" / "sessions" / name
    home = directory / "home"
    write(home / ".cache/pip/wheel", 200_000)
    write(home / ".claude/projects/p/old.jsonl", text=talk(300), age_days=60)
    write(home / ".claude/projects/p/new.jsonl", text=talk(20), age_days=1)
    write(home / "work/node_modules/x/index.js", 100_000)
    when = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - days * 86400))
    write(directory / "session.json", text=json.dumps(
        {"agent": "claude", "options": [], "backend": "bwrap", "keep": True, "created": when, "workspace": "/srv/app",
         "cwd": "/", "name": name}))
    write(directory / "exit.json", text=json.dumps({"status": 0, "ended": when}))
    return home


def start(name="big", state="stopped", host="local", kind="session", storage=None):
    record = {"storage": storage} if storage else None
    if host != "local":
        return backend.Instance(kind, f"host:{host}:{name}", name, host=host, state=state, agent="claude",
                                record=record)
    return backend.Instance(kind, f"{kind}:{name}", name, session=name if kind == "session" else None, state=state,
                            agent="claude", mcp=name if kind == "mcp" else None, record=record, ident=name)


def test_only_the_plans_commands_are_run():
    for good in (["gc"], ["gc", "--only", "--caches", "--home", "big", "--yes"],
                 ["gc", "--only", "--compact-transcripts", "30", "--home", "big", "--yes"],
                 ["gc", "--only", "--images"], ["gc", "--only", "--logs-older-than", "180"],
                 ["gc", "--pack", "--home", "big", "--yes"], ["gc", "--unpack", "big"]):
        assert backend.storage_argv(good) == good
    for bad in (["rm", "-rf", "/"], ["gc", "--older-than", "0"], ["gc", "--only", "--home", "-x"],
                ["gc", "--only", "--compact-transcripts", "0"], ["gc", "--pack", "--home", "big"],
                ["gc", "--unpack", "--yes"], ["gc", "--only", "--caches", "--", "x"], "gc", [], ["gc", 1],
                ["gc", "--only", "--transcripts", "1e3"], ["gc", "--only", "--home", "a\nb"]):
        with pytest.raises(ValueError):
            backend.storage_argv(bad)


def test_the_buttons_and_the_right_click(qapp, env, monkeypatch):
    from sandbox_ai_app import ui
    sai = backend.SandboxAI()
    ended = backend.Instance("run", "ended:local:x", "x", run="claude-1", state="ended")
    for instance, why in ((start(), None), (start(state="running"), None), (start(host="ct1"), None),
                          (ended, "kept start"), (start("files", "running", kind="mcp"), "MCP server"),
                          (None, "Select a row")):
        page = ui.SandboxesPage(sai, lambda *a, **k: None)
        monkeypatch.setattr(page, "current", lambda instance=instance: instance)
        monkeypatch.setattr(page, "stale", lambda instance: None)
        page.update_buttons()
        button = page.transfer_buttons["storage"]
        entries = widgets.menu_entries(widgets.menu_of(page.table))
        if why is None:
            assert button.isEnabled(), (instance.key, button.toolTip())
            assert any("Storage…" in str(entry) for entry in entries), entries
        else:
            assert not button.isEnabled() and why in button.toolTip(), (why, button.toolTip())
        assert page.optimize_button is page.manage_buttons["optimize"]
        assert any("Optimize…" in str(entry) for entry in entries), entries
        page.close()


def test_the_size_column_and_its_growth_on_hover(qapp, env):
    from sandbox_ai_app import ui
    page = ui.SandboxesPage(backend.SandboxAI(), lambda *a, **k: None)
    row = page.start_row(start(storage={"bytes": 3 << 30, "growth": 2 << 30, "measured": "2026-11-19T10:00:00Z",
                                        "since": "2026-11-12T10:00:00Z"}))
    assert row["cells"][10] == "3 GiB" and row["sort"][10] == 3 << 30
    assert "Size at the storage check of" in row["tip"] and "+2 GiB since the check of" in row["tip"]
    assert page.start_row(start())["cells"][10] == ""
    assert page.start_row(start(storage={"bytes": "x"}))["cells"][10] == ""  # (another host's answer: numbers only)
    page.close()


def test_the_notice_says_what_could_be_freed_who_grew_and_what_the_rules_did():
    from sandbox_ai_app.ui import storage_notice
    assert storage_notice({"above": False, "total": 5}) is None
    text = storage_notice({"above": True, "total": 13 << 30, "grew": [{"name": "big\x1b[2J", "bytes": 3 << 30}],
                           "said": [{"rule": "pack", "name": "old", "bytes": 1 << 30}, {"rule": "x", "name": "y"}]})
    assert "13 GiB could be freed on this machine" in text and "big\\x1b[2J +3 GiB" in text and "\x1b" not in text
    assert "packed the HOME of old (1 GiB)" in text and "acted 2 time(s)" in text
    i18n.set_language("de")
    try:
        assert "könnten auf diesem Rechner frei werden" in storage_notice({"above": True, "total": 1 << 30})
    finally:
        i18n.set_language("en")


@needs_sai
def test_optimize_then_apply_only_what_was_ticked(qapp, env):
    home = kept_start(env)
    dialog = storage_view.StorageDialog(None, backend.SandboxAI(), "local")
    try:
        kinds = [item["kind"] for item in dialog.items]
        assert {"caches", "build", "compact", "pack"} <= set(kinds), (kinds, dialog.status.text())
        assert "could be freed" in dialog.summary.text()
        assert dialog.tree.topLevelItemCount() == len(kinds)
        tip = dialog.tree.topLevelItem(kinds.index("caches")).toolTip(0)
        assert "sandbox-ai gc --only --caches --home big --yes" in tip
        dialog.tick_all(False)
        assert not dialog.apply_button.isEnabled()
        dialog.tree.topLevelItem(kinds.index("caches")).setCheckState(0, QtCore.Qt.CheckState.Checked)
        assert dialog.apply_button.isEnabled()
        dialog.apply()
        assert [action for action, _ in ASKED] == ["storage-apply"] and "gc --only --caches" in ASKED[0][1]
        assert not (home / ".cache/pip").exists(), dialog.status.text()
        assert (home / "work/node_modules/x/index.js").exists() and (home / ".claude/projects/p/old.jsonl").exists()
        assert "caches" not in [item["kind"] for item in dialog.items]  # measured again after it
    finally:
        dialog.close()


@needs_sai
def test_the_storage_of_one_start_and_its_transcripts_compacted(qapp, env):
    home = kept_start(env)
    old = (home / ".claude/projects/p/old.jsonl").read_text()
    dialog = storage_view.StorageDialog(None, backend.SandboxAI(), "local", "big")
    try:
        assert dialog.windowTitle() == "Storage of big on this machine"
        summary = dialog.summary.text()
        assert "Its HOME:" in summary and "by kind:" in summary and "download caches" in summary
        assert "The logs of its project" in summary and "checkpoints of its project" in summary
        kinds = [item["kind"] for item in dialog.items]
        assert {"caches", "build", "toolchains", "transcripts", "pack", "compact", "logs"} == set(kinds)
        dialog.tick_all(False)
        dialog.tree.topLevelItem(kinds.index("compact")).setCheckState(0, QtCore.Qt.CheckState.Checked)
        dialog.apply()
        packed = home / ".claude/projects/p/old.jsonl.gz"
        assert packed.exists() and gzip.decompress(packed.read_bytes()).decode() == old
        assert (home / ".claude/projects/p/new.jsonl").exists() and (home / ".cache/pip/wheel").exists()
        assert "Compacted transcripts: 1" in dialog.summary.text()
    finally:
        dialog.close()


@pytest.fixture
def window(qapp, env, monkeypatch):
    from sandbox_ai_app import terminal
    from sandbox_ai_app.ui import MainWindow
    monkeypatch.setattr(terminal, "available", lambda web=None: (False, "not in this test"))
    w = MainWindow(backend.SandboxAI(), check_updates=False, approvals_interval=0)
    if w.tray is not None:
        w.tray.hide()
        w.tray = None
    yield w
    w.shutdown()
    w.close()
    w.deleteLater()
    QtWidgets.QApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)


@needs_sai
def test_the_apps_own_check_is_due_records_the_sizes_and_says_it(window, env, monkeypatch, wait):
    kept_start(env)
    sai = window.sai
    assert window.storage_due() is True  # never ran
    assert sai.save_sandbox_default("storage_threshold_gb", 0.0001) is None
    said = []
    monkeypatch.setattr(window, "notify", lambda title, text, level="warning", action=None: said.append(
        (title, text, level, action)))
    assert window.storage_tick() is True
    assert wait(lambda: said or not window.storage_running, 120)
    assert said and said[0][0] == "Storage" and "could be freed on this machine" in said[0][1], said
    assert said[0][2] == "warning" and said[0][3][0] == "Optimize…"
    assert window.storage_due() is False  # weekly: not again now
    sizes = sai.storage_sizes()
    assert sizes["big"]["bytes"] > 0 and sizes["big"]["growth"] is None
    sai.save_sandbox_default("storage_check", "off")
    assert window.storage_due(now=time.time() + 30 * 86400) is False
    sai.save_sandbox_default("storage_check", "daily")
    assert window.storage_due(now=time.time() + 2 * 86400) is True


def test_settings_storage(window, env):
    settings = window.settings_dialog()
    try:
        fields = settings.fields
        assert {"storage_check", "storage_threshold_gb", "storage_growth_gb", "storage_caches_days",
                "storage_compact_days", "storage_pack_days", "storage_logs_days"} <= set(fields)
        path = window.sai.sandbox_defaults_file()
        fields["storage_compact_days"].setValue(30)
        fields["storage_check"].setCurrentIndex(fields["storage_check"].findData("daily"))
        saved = json.loads(Path(path).read_text())
        assert saved["storage_compact_days"] == 30 and saved["storage_check"] == "daily"
        fields["storage_compact_days"].setValue(0)
        fields["storage_check"].setCurrentIndex(fields["storage_check"].findData("weekly"))
        saved = json.loads(Path(path).read_text())
        assert "storage_compact_days" not in saved and "storage_check" not in saved  # off, the default: not set
        assert fields["storage_compact_days"].specialValueText() == "off"
    finally:
        settings.close()
