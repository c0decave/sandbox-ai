"""LLM servers for all hosts on the app's LLM servers page (offscreen; sandbox-ai llm share): the column For all hosts
with each host's state, the dialog For all hosts… (the flag, one key tick per host, each host's state) and the
commands it gives - a key newly sent to a host and unsharing asked first. A copy another machine shares is shown as
managed from there and not shared on. Then the whole way: the page against the real sandbox-ai, whose hosts an ssh
stand-in reaches (the core's tests/test_llm_share.py World: other HOMEs, their own llm.json and secret stores)."""
import argparse
import importlib.util
import json
import os
from pathlib import Path

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, widgets  # noqa: E402
from sandbox_ai_app import i18n, llm_page, llm_share_view  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")

HOSTS = [{"host": "ct1", "copied": True, "in_sync": True, "key": "sent", "key_allowed": True, "keys_sent": ["LLM_KEY"],
          "keys_missing": [], "reachable": True, "http": 200, "reach_error": None, "error": None, "offline": False,
          "pending": None},
         {"host": "ct2", "copied": False, "in_sync": False, "key": "unknown", "key_allowed": False, "keys_sent": [],
          "keys_missing": [], "reachable": None, "http": None, "reach_error": None,
          "error": "ct2 did not answer over SSH (255)", "offline": True, "pending": "copy"}]
SHARED = {"name": "lm", "kind": "lmstudio", "url": "http://192.168.1.20:1234", "api_key_secret": "LLM_KEY",
          "headers": {}, "ca": None, "vpn": None, "model": None, "anthropic": False, "insecure_http": False,
          "models": [], "checked": None, "agents": ["claude"], "shared_from": None,
          "shared": {"key_to": ["ct1"], "since": "2026-11-22T10:00:00Z", "hosts": HOSTS}}
PLAIN = dict(SHARED, name="ol", kind="ollama", url="http://192.168.1.30:11434", api_key_secret=None, shared=None)
COPY = dict(SHARED, name="gpu", shared=None, shared_from="box", key_here="missing")


class Parsed(Exception):
    pass


def parsed(sai, argv):
    """ARGV as `sandbox-ai llm` parses it (nothing of the command runs)."""
    module = sai.module("llm")
    seen = []
    original = argparse.ArgumentParser.parse_args

    def capture(self, args=None, namespace=None):
        seen.append(original(self, args, namespace))
        raise Parsed
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(argparse.ArgumentParser, "parse_args", capture)
        with pytest.raises(Parsed):
            module.main(list(argv[1:]))
    return seen[0]


@pytest.fixture
def sai(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_DATA_HOME", "data"),
                      ("XDG_CACHE_HOME", "cache")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])

    def synchronous(function, *args, done, **kw):
        try:
            result = function(*args)
        except Exception as exc:  # noqa: BLE001 - as ThreadCall hands it on
            done(None, exc)
            return
        done(result, None)
    monkeypatch.setattr(llm_page, "background", synchronous)
    sai = backend.SandboxAI()
    sai.asked = []
    sai.answers = {("local", ("llm", "list", "--json")): (0, json.dumps([SHARED, PLAIN, COPY]), "")}
    monkeypatch.setattr(sai, "hosts_supported", lambda: True)
    monkeypatch.setattr(sai, "host_entries", lambda: [{"name": "local", "kind": "local"},
                                                      {"name": "ct1", "kind": "ssh", "ssh": "admin@ct1"},
                                                      {"name": "ct2", "kind": "ssh", "ssh": "admin@ct2"}])

    def host_run(name, *args, timeout=120):
        if args[:2] != ("llm", "status"):
            sai.asked.append((name, args))
        return sai.answers.get((name, args), (0, "", ""))
    monkeypatch.setattr(sai, "host_run", host_run)
    return sai


def row_of(page, name):
    return next(r for r in range(page.servers.rowCount()) if llm_page.row_key(page.servers, r) == name)


def chosen(page, name):
    page.servers.clearSelection()
    page.servers.selectRow(row_of(page, name))


def test_the_column_says_per_server_and_host_whether_it_is_for_all_hosts(qapp, sai):
    page = llm_page.LlmPage(sai)
    page.show()
    column = [page.servers.horizontalHeaderItem(c).text() for c in range(page.servers.columnCount())].index(
        "For all hosts")
    cell = {name: page.servers.item(row_of(page, name), column) for name in ("lm", "ol", "gpu")}
    assert cell["lm"].text() == "yes: 1 of 2 hosts ready"
    assert cell["ol"].text() == "" and cell["gpu"].text() == "from box"
    tip = cell["lm"].toolTip()
    assert "Key to: ct1" in tip
    assert "ct1: copied, in sync, key LLM_KEY sent, reachable from there" in tip
    assert "ct2: not copied yet: ct2 did not answer over SSH (255)" in tip
    assert "Managed from box" in cell["gpu"].toolTip()
    assert "Its key is not kept on this host: keep it as the secret LLM_KEY here" in cell["gpu"].toolTip()
    page.close()
    page.deleteLater()


def test_the_dialog_shares_with_a_key_tick_per_host_asked_first(qapp, sai, monkeypatch):
    page = llm_page.LlmPage(sai)
    page.show()
    seen = []

    def run(dialog):
        seen.append(dialog)
        assert dialog.flag.isChecked(), "it is shared now"
        assert set(dialog.ticks) == {"ct1", "ct2"} and dialog.ticks["ct1"].isChecked()
        assert not dialog.ticks["ct2"].isChecked()
        assert "not copied yet: ct2 did not answer" in dialog.states["ct2"].text()
        dialog.ticks["ct2"].setChecked(True)
        return QtWidgets.QDialog.DialogCode.Accepted
    monkeypatch.setattr(llm_page.ShareDialog, "exec", run)
    chosen(page, "lm")
    sai.asked.clear()
    page.share_button.click()
    assert seen and sai.asked[0] == ("local", ("llm", "share", "lm", "--key-to", "ct1,ct2"))
    title, text, _ = widgets.SHOWN[-1]
    assert "Send the key of lm (LLM_KEY) to ct2?" in text, "a key newly sent to a host is asked first"
    args = parsed(sai, ["llm", "share", "lm", "--key-to", "ct1,ct2"])
    assert (args.command, args.name, args.key_to) == ("share", "lm", "ct1,ct2")
    # Declined: nothing runs.
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)
    sai.asked.clear()
    page.share_button.click()
    assert sai.asked == []
    # Off: unshare, asked first.
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)

    def off(dialog):
        dialog.flag.setChecked(False)
        assert not any(tick.isEnabled() for tick in dialog.ticks.values())
        return QtWidgets.QDialog.DialogCode.Accepted
    monkeypatch.setattr(llm_page.ShareDialog, "exec", off)
    page.share_button.click()
    assert sai.asked[0] == ("local", ("llm", "unshare", "lm"))
    assert "Take lm away from every host it is shared with?" in widgets.SHOWN[-1][1]
    assert parsed(sai, ["llm", "unshare", "lm"]).command == "unshare"
    page.close()
    page.deleteLater()


def test_a_server_without_a_key_is_shared_without_ticks_and_a_copy_is_not_shared_on(qapp, sai, monkeypatch):
    page = llm_page.LlmPage(sai)
    page.show()

    def run(dialog):
        assert not dialog.flag.isChecked() and not any(t.isEnabled() for t in dialog.ticks.values())
        assert dialog.states["ct1"].text() == "gets the copy when it is shared"
        dialog.flag.setChecked(True)
        assert not any(t.isEnabled() for t in dialog.ticks.values()), "no key: nothing to tick"
        return QtWidgets.QDialog.DialogCode.Accepted
    monkeypatch.setattr(llm_page.ShareDialog, "exec", run)
    chosen(page, "ol")
    sai.asked.clear()
    page.share_button.click()
    assert sai.asked[0] == ("local", ("llm", "share", "ol", "--no-key"))
    chosen(page, "gpu")
    sai.asked.clear()
    page.share_button.click()
    assert sai.asked == [] and "managed from box" in page.status.text()
    page.close()
    page.deleteLater()


def test_the_edit_dialog_has_the_flag_and_applies_it_once_saved(qapp, sai, monkeypatch):
    """The server's properties (Edit…, Add…) have the box For all hosts: changed alone it shares or unshares (asked
    first), with a change of the server after llm add accepted it; a copy another machine manages has none."""
    sai.answers[("local", ("llm", "show", "lm", "--json"))] = (0, json.dumps(SHARED), "")
    sai.answers[("local", ("llm", "show", "gpu", "--json"))] = (0, json.dumps(COPY), "")
    page = llm_page.LlmPage(sai)
    page.show()
    boxes = []

    def unshare(dialog):
        boxes.append(dialog.share_box)
        assert dialog.share_box is not None and dialog.share_box.isChecked()
        dialog.share_box.setChecked(False)
        return QtWidgets.QDialog.DialogCode.Accepted
    monkeypatch.setattr(llm_page.LlmDialog, "exec", unshare)
    chosen(page, "lm")
    sai.asked.clear()
    page.edit()
    assert ("local", ("llm", "unshare", "lm")) in sai.asked and not any(a[1][:2] == ("llm", "add") for a in sai.asked)
    assert "Take lm away from every host" in widgets.SHOWN[-1][1]

    def none(dialog):
        boxes.append(dialog.share_box)
        return QtWidgets.QDialog.DialogCode.Rejected
    monkeypatch.setattr(llm_page.LlmDialog, "exec", none)
    chosen(page, "gpu")
    page.edit()
    assert boxes[-1] is None, "a copy managed from another machine: not shared on from here"

    def new(dialog):
        assert not dialog.share_box.isChecked()
        dialog.name.setText("lm3")
        dialog.kind.setCurrentIndex(dialog.kind.findData("ollama"))
        dialog.url.setText("http://192.168.1.40:11434")
        dialog.share_box.setChecked(True)
        return QtWidgets.QDialog.DialogCode.Accepted
    monkeypatch.setattr(llm_page.LlmDialog, "exec", new)
    sai.asked.clear()
    page.add()
    commands = [a[1] for a in sai.asked if a[1][:2] in (("llm", "add"), ("llm", "share"))]
    assert commands == [("llm", "add", "lm3", "--kind", "ollama", "--url", "http://192.168.1.40:11434"),
                        ("llm", "share", "lm3", "--no-key")], "shared once llm add took it"
    page.close()
    page.deleteLater()


def test_the_commands_take_only_names_that_can_be_passed_on():
    assert llm_share_view.share_argv("lm", ["ct2", "ct1", "ct1"]) == ["llm", "share", "lm", "--key-to", "ct1,ct2"]
    for name, hosts in (("-x", []), ("lm", ["-oProxyCommand=x"]), ("lm", ["local"]), ("lm", ["a,b"]), ("a b", [])):
        with pytest.raises(ValueError):
            llm_share_view.share_argv(name, hosts)
    with pytest.raises(ValueError):
        llm_share_view.unshare_argv("--all")


def test_every_text_is_in_german_too():
    i18n.set_language("de")
    try:
        assert llm_share_view.share_cell(SHARED) == "ja: 1 von 2 Hosts bereit"
        assert "von dort erreichbar" in llm_share_view.share_tooltip(SHARED)
    finally:
        i18n.set_language("en")


# --- the whole way: the page, the real sandbox-ai, two hosts reached over the ssh stand-in -------------------------

def core_world():
    path = Path(os.environ.get("SANDBOX_AI_HOME") or Path(__file__).resolve().parents[3]) / "tests/test_llm_share.py"
    if not path.is_file():
        pytest.skip("needs sandbox-ai's tests (SANDBOX_AI_HOME: a checkout)")
    spec = importlib.util.spec_from_file_location("core_llm_share_for_app", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_page_shares_a_server_with_real_hosts_end_to_end(qapp, tmp_path, monkeypatch):
    core = core_world()
    world = core.World(tmp_path)
    server = core.Server()
    try:
        core.keep_key(world)
        core.add_server(world, server)
        world.host("ct1")
        world.host("ct2")
        for key, value in world.env.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
        monkeypatch.setattr(widgets, "SHOWN", [])
        sai = backend.SandboxAI()
        page = llm_page.LlmPage(sai)

        def run(dialog):
            dialog.flag.setChecked(True)
            dialog.ticks["ct1"].setChecked(True)
            return QtWidgets.QDialog.DialogCode.Accepted
        monkeypatch.setattr(llm_page.ShareDialog, "exec", run)
        page.show()
        wait(qapp, lambda: page.servers.rowCount() == 1)
        page.servers.selectRow(0)
        page.share_button.click()
        wait(qapp, lambda: "ready" in (page.servers.item(0, 7).text() if page.servers.item(0, 7) else ""), 120)
        assert page.servers.item(0, 7).text() == "yes: 2 of 2 hosts ready"
        tip = page.servers.item(0, 7).toolTip()
        assert "ct1: copied, in sync, key LMS_KEY sent, reachable from there" in tip
        assert "ct2: copied, in sync, key missing there, reachable from there" in tip
        assert world.secret_file("ct1", "LMS_KEY").read_text().strip() == core.KEY
        assert not world.secret_file("ct2", "LMS_KEY").exists()
        assert world.store("ct2")["lms"]["shared_from"] == world.shares()["origin"]
        assert core.KEY not in world.ssh_lines() and core.KEY not in page.status.text()
        page.close()
        page.deleteLater()
    finally:
        server.close()


def wait(qapp, condition, seconds=60):
    import time
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.02)
    assert condition()
