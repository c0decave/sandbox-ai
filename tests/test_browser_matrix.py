"""--browser in whole starts of every agent on both backends (the feature matrix's fixture `m`): the plan on Podman,
the refusal on Bubblewrap, a project file and a preset that carry it, the security modes by name, the
administrator's policy, and what the agent's sandbox and its pod get."""
import json
from pathlib import Path

import pytest

import test_feature_matrix as fm
from test_feature_matrix import m  # noqa: F401  (fixture)

s = fm.s
IMAGE_ID = "e" * 64


@pytest.fixture(autouse=True)
def built(monkeypatch):
    """sandbox-ai's browser image is there (sandbox-ai build browser): its ID, as Podman would name it."""
    browser = s.load_browser_module()
    monkeypatch.setattr(browser, "image_id", lambda a, l=None, run=None: IMAGE_ID)
    monkeypatch.setattr(s, "load_browser_module", lambda: browser)
    return browser


def test_the_plan_on_podman_and_the_refusal_on_bubblewrap(m):
    if m.backend == "bwrap":
        m.refused("--browser", match="--browser needs the Podman backend")
        return
    plan = m.plan("--browser")
    assert plan["browser"]["control"] == "human" and plan["browser"]["tcp_ports"] == []
    plan = m.plan("-bw", "--browser-control", "both")
    assert plan["browser"]["cdp"] == "http://127.0.0.1:9222"


def test_a_start_with_the_browser_beside_the_agent(m, built):
    if m.backend == "bwrap":
        return
    seen = m.run("--browser", "--browser-control", "agent")
    assert seen.config["services"] == [{"name": "browser", "ready": 9222, "timeout": 90, "ca": True}]
    assert seen.env["SANDBOX_AI_BROWSER"] == "chromium" and seen.env["SANDBOX_AI_BROWSER_CDP"].endswith(":9222")
    assert seen.run_record["browser"]["control"] == "agent" and seen.run_record["browser"]["vnc"] is None
    assert json.loads(seen.files["browser.json"])["cdp_port"] == 9222
    assert "--shm-size=1g" in seen.create
    # The agent's own container gets nothing of the browser: no VNC folder, no profile, no CA copy of its own.
    assert ".browser-vnc" not in seen.joined and "/sandbox-browser" not in seen.joined
    assert seen.a.browser_container["record"]["id"] == IMAGE_ID
    seen = m.run()
    assert "SANDBOX_AI_BROWSER" not in seen.env and seen.run_record["browser"] is None
    assert "services" not in seen.config


def test_a_project_file_and_a_preset_take_it(m):
    if m.backend == "bwrap":
        return
    m.project_file({"options": ["--browser", "--browser-control", "agent"]})
    assert m.plan()["browser"]["control"] == "agent"
    m.project_file({"options": ["--browser", "firefox", "--browser-control", "agent"]})
    m.refused(match="needs Chromium")
    m.project_file({"options": ["--browser", "--gui"]})  # its optional KIND never swallows the next option
    plan = m.plan()
    assert plan["browser"]["kind"] == "chromium"
    m.project_file({"options": []})
    presets = Path.home() / ".config" / "sandbox-ai" / "presets"
    presets.mkdir(parents=True)
    (presets / "web.json").write_text(json.dumps({
        "format": "sandbox-ai preset", "version": 1, "id": "web", "title": "Web tester",
        "description": "the agent's own browser", "options": ["--browser", "--browser-control", "agent"]}))
    assert s.load_presets_module().problems(json.loads((presets / "web.json").read_text()), "web.json") == []
    plan = m.plan("--preset", "web")
    assert plan["browser"]["control"] == "agent" and plan["browser"]["cdp"] == "http://127.0.0.1:9222"


def test_the_modes_by_name(m):
    if m.backend == "bwrap":
        return
    m.refused("--mode", "paranoid", "--browser", match=r"^--browser chromium widens the security mode paranoid")
    assert m.plan("--mode", "high", "--browser")["browser"]["control"] == "human"
    m.refused("--mode", "high", "--browser", "--browser-control", "agent", "--browser-profile", str(m.tmp / "p"),
              match="folder of yours through its profile")


def test_the_policy_can_forbid_it(m):
    if m.backend == "bwrap":
        return
    m.policy(forbid=["--browser"])
    m.refused("--browser", match="--browser is not allowed by the administrator's policy")
    m.policy(forbid=["--browser-control=agent"])
    m.refused("--browser", "--browser-control", "agent",
              match="--browser-control=agent is not allowed by the administrator's policy")
    assert m.plan("--browser")["browser"]["control"] == "human"
