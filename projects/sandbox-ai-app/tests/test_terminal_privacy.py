"""A link in the terminal tab of a run behind Tor or a VPN (review finding: Ctrl+click opened it in this computer's
browser, which reaches it with this computer's own address, in clear text past Tor): behind Tor never opened here,
only Copy link (for Tor Browser); behind a VPN only after a warning, Open anyway past the VPN; the tab finds its run
by its tag (run.json app_tab, list --json app_tab), the start it attaches to (run.json session or name, list --json
name) or, before the run is registered, its command line's --vpn; a run without either opens as before. Also the
run.json and list --json fields this rests on (sandbox-ai's own register_run and starts.entries)."""
import importlib.util
import json
import os
from pathlib import Path
import shutil

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, widgets  # noqa: E402
from conftest import OPENED_URLS, open_tab  # noqa: E402

HERE = Path(__file__).resolve().parent
URL = "https://example.org/report?id=1"


@pytest.fixture
def terminal(qapp, monkeypatch):
    from sandbox_ai_app import terminal
    if terminal.MISSING:
        pytest.skip(f"QtWebEngine is not installed: {terminal.MISSING}")
    monkeypatch.setattr(terminal, "available", lambda web=None: (True, ""))
    return terminal


@pytest.fixture
def stub_web(tmp_path):
    web = tmp_path / "web"
    (web / "vendor").mkdir(parents=True)
    for name in ("terminal.html", "terminal.js"):
        shutil.copy(HERE.parent / "sandbox_ai_app" / "web" / name, web / name)
    for name in ("xterm.js", "addon-fit.js", "xterm.css"):
        shutil.copy(HERE / "fake_xterm" / name, web / "vendor" / name)
    return web


@pytest.fixture
def sai(tmp_path, monkeypatch):
    """A stand-in sandbox-ai whose commands sleep; live_runs() holds RUNS (the registry), list --json LISTED."""
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    script = tmp_path / "bin" / "sandbox-ai"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\nexec sleep 60\n")
    script.chmod(0o755)
    sai = backend.SandboxAI()
    sai.runs_now, sai.listed = {}, []
    monkeypatch.setattr(sai, "command", lambda *args: [str(script), *args])
    monkeypatch.setattr(sai, "live_runs", lambda: dict(sai.runs_now))
    monkeypatch.setattr(sai, "host_run", lambda name, *args, timeout=120: (0, json.dumps(sai.listed), ""))
    return sai


def bar_text(view):
    return view.bar.label.text() if view.bar.isVisible() or view.bar.label.text() else ""


def choices(view):
    return [button.text() for button in view.bar.buttons]


def press(view, label):
    assert view.bar.choose(label), f"no {label} in {choices(view)}"


def test_a_tor_runs_link_is_never_opened_here_only_copied(terminal, stub_web, sai, qapp):
    page = terminal.TerminalsPage(stub_web, sai=sai)
    view = open_tab(page, sai.command("claude", "--workspace", "/srv/app"), "claude: app")
    try:
        assert view.tab_tag
        sai.runs_now = {"claude-1": {"run": "claude-1", "app_tab": view.tab_tag, "vpn_profile": "anon",
                                     "vpn_type": "tor"}}
        before = len(OPENED_URLS)
        view.open_link(URL)
        assert len(OPENED_URLS) == before, "nothing opened in this computer's browser"
        assert "through Tor" in bar_text(view) and "Tor Browser" in bar_text(view)
        assert choices(view) == ["Copy link", "Cancel"], "no way to open it here"
        press(view, "Copy link")
        assert QtWidgets.QApplication.clipboard().text() == URL
        assert len(OPENED_URLS) == before
    finally:
        page.close_all()
        page.deleteLater()


def test_a_vpn_runs_link_opens_only_after_the_warning(terminal, stub_web, sai, qapp):
    page = terminal.TerminalsPage(stub_web, sai=sai)
    view = open_tab(page, sai.command("attach", "web"), "web")
    try:
        assert view.start_name == "web"
        sai.runs_now = {"claude-2": {"run": "claude-2", "session": "web", "vpn_profile": "office",
                                     "vpn_type": "wireguard"}}
        before = len(OPENED_URLS)
        view.open_link(URL)
        assert len(OPENED_URLS) == before and "VPN office" in bar_text(view) and "past the VPN" in bar_text(view)
        assert choices(view) == ["Copy link", "Open anyway, past the VPN", "Cancel"]
        press(view, "Open anyway, past the VPN")
        assert OPENED_URLS[before].toString() == URL, "asked explicitly: opened"
    finally:
        page.close_all()
        page.deleteLater()


def test_a_run_without_vpn_opens_as_before_and_the_listing_or_command_line_count_too(terminal, stub_web, sai, qapp):
    page = terminal.TerminalsPage(stub_web, sai=sai)
    plain = open_tab(page, sai.command("claude"), "claude")
    early = open_tab(page, sai.command("codex", "--vpn", "home", "--", "--model", "x"), "codex")
    listed = open_tab(page, sai.command("attach", "remote-ish"), "remote-ish")
    try:
        sai.runs_now = {"claude-3": {"run": "claude-3", "app_tab": plain.tab_tag, "vpn_profile": None,
                                     "vpn_type": None}}
        before = len(OPENED_URLS)
        plain.open_link(URL)
        assert OPENED_URLS[before].toString() == URL, "no VPN, no Tor: opened as always"
        early.open_link(URL)  # not registered yet: its --vpn says it, the kind not known (Tor perhaps)
        assert len(OPENED_URLS) == before + 1 and choices(early) == ["Copy link", "Cancel"]
        assert "profile home" in bar_text(early)
        page.entries = [{"name": "remote-ish", "vpn_profile": "anon", "vpn_type": "tor"}]
        listed.open_link(URL)
        assert len(OPENED_URLS) == before + 1 and "through Tor" in bar_text(listed)
        assert terminal.argv_privacy(["ssh", "-t", "h", "exec sandbox-ai claude --vpn=tor1"]) == ("profile", "tor1")
        assert terminal.argv_privacy(["sandbox-ai", "claude", "--", "--vpn", "x"]) is None, "an agent's argument"
        assert terminal.record_privacy("tor", "bad name") is None
    finally:
        page.close_all()
        page.deleteLater()


# -- what sandbox-ai records for it --

def sandbox_module(name):
    home = Path(os.environ.get("SANDBOX_AI_HOME") or "")
    path = home / "sandbox" / f"{name}.py"
    if not path.exists():
        pytest.skip("set SANDBOX_AI_HOME to a sandbox-ai checkout")
    spec = importlib.util.spec_from_file_location(f"sandbox_{name}_privacy_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sandbox_ai_records_the_tab_and_lists_the_vpn(monkeypatch):
    launcher = sandbox_module("agent_sandbox")
    monkeypatch.setenv("SANDBOX_AI_APP_TAB", "0123456789abcdef")
    assert launcher.app_tab() == "0123456789abcdef"
    monkeypatch.setenv("SANDBOX_AI_APP_TAB", "bad tag; rm")
    assert launcher.app_tab() is None
    source = (Path(launcher.__file__)).read_text()
    assert '"app_tab": app_tab(),' in source, "register_run writes it into run.json"
    starts = sandbox_module("starts")
    vpn = sandbox_module("vpn")
    assert starts.VPN_TYPES == vpn.TYPES
