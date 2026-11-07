"""The Hosts page (offscreen): the hosts of sandbox-ai hosts, and for one of them its starts, approvals, doctor and
the agents' telemetry, each through sandbox-ai hosts run (here a stand-in that records what it was asked)."""
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
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "background", lambda function, *args, done, **kw: done(function(*args), None))
    import sandbox_ai_app.hosts_page as hp
    monkeypatch.setattr(hp, "background", lambda function, *args, done, **kw: done(function(*args), None))
    return tmp_path


def test_hosts_starts_approvals_and_telemetry(qapp, env, monkeypatch):
    sai = backend.SandboxAI()
    if not sai.hosts_supported():
        pytest.skip("this sandbox-ai has no hosts.py")
    sai.host_add("ct1", "admin@192.0.2.10", 2222, "", "Proxmox 101")
    asked = []
    answers = {("list", "--json"): [{"name": "web", "agent": "claude", "state": "running", "attachable": True,
                                     "workspace": "/srv/app", "started": "2026-11-05T10:00:00Z"}],
               ("approvals", "list", "--json"): [{"id": "1-2-3", "question": "connect to 192.0.2.7:443",
                                                  "sandbox": "claude-1"}],
               ("defaults", "show", "--json"): {"path": "/home/admin/.config/sandbox-ai/defaults.json",
                                                "values": {"telemetry": "strict"},
                                                "telemetry_levels": ["off", "strict", "on"]}}

    def host_run(name, *args, timeout=120):
        asked.append((name, args))
        return 0, json.dumps(answers.get(tuple(args), {})), ""
    monkeypatch.setattr(sai, "host_run", host_run)
    opened = []
    page = HostsPage(sai, lambda argv, title: opened.append((argv, title)))
    assert [page.list.item(i).data(0x0100) for i in range(page.list.count())] == ["local", "ct1"]
    page.list.setCurrentRow(1)
    assert asked == [], "a page nobody opened asks no host (no command, no SSH at every start of the app)"
    page.show()
    assert asked and asked[0][0] == "ct1", "shown: the selected host is asked"
    assert page.starts.rowCount() == 1 and page.starts.item(0, 0).text() == "web"
    assert page.questions.rowCount() == 1 and page.telemetry.currentData() == "strict"
    page.starts.selectRow(0)
    page.attach()
    argv, title = opened[-1]
    assert argv[0] == "ssh" and "-tt" in argv and argv[-1].endswith("attach web; fi") and title == "ct1: web"
    page.start_action("stop")
    assert ("ct1", ("stop", "web")) in asked
    page.questions.selectRow(0)
    page.scope.setCurrentIndex(page.scope.findData("run"))
    page.answer("allow")
    assert ("ct1", ("approvals", "allow", "1-2-3", "--scope", "run")) in asked
    page.telemetry.setCurrentIndex(page.telemetry.findData("off"))
    page.set_telemetry()
    assert ("ct1", ("defaults", "unset", "telemetry")) in asked
    page.list.setCurrentRow(1)
    page.remove()
    page.close()
    assert [e["name"] for e in sai.host_entries()] == ["local"]
