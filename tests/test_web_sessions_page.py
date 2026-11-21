"""The web interface's starts page as the desktop app's Sessions page (user 2026-11-19, parity of 0.1.141): its rows'
actions as icon buttons - the app's own drawings (button_icons.py), made as SVG elements, each with its tooltip
(its text first, then what it does) and its accessible name -, "Show button texts beside the icons" (kept in this
browser, as the app's setting button_texts), and two tabs: Sessions & sandboxes / Schedules. app.js in node with the
tests' stand-in for the DOM, in English and German; the drawings compared with the app's."""
import json
from pathlib import Path
import re
import shutil
import sys
import xml.etree.ElementTree as ET

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from test_web import FAKE_DOM, run_driver  # noqa: E402

APP_JS = (ROOT / "sandbox" / "web" / "app.js").read_text()
ICONS_PY = ROOT / "projects" / "sandbox-ai-app" / "sandbox_ai_app" / "button_icons.py"

DRIVER = FAKE_DOM + r"""
const stored = {};
context.localStorage = { getItem: k => (k in stored ? stored[k] : null), setItem: (k, v) => { stored[k] = String(v); } };
const byId = (id) => body.all().find(n => n.id === id);
const buttons = (node) => node.all().filter(n => n.tagName === "button");
(async () => {
  const result = {};
  if (process.argv[3] === "de") vm.runInContext('setLanguage("de")', context);
  vm.runInContext('state.me = { user: "admin", csrf: "c", terminals: true }; state.host = "local"; state.hosts = [{ name: "local", kind: "local" }, { name: "ct1", kind: "ssh", ssh: "root@ct1" }]; state.page = "starts"; state.startsHost = "local";', context);
  answer = (method, path) => {
    if (path === "/api/hosts/local/starts") return [
      { name: "web", id: "web", agent: "claude", kind: "session", state: "running", keep: true, home: "kept", directory: "/d", attachable: true },
      { name: "old", id: "old", agent: "codex", kind: "session", state: "stopped", keep: true, home: "kept", directory: "/d2" }];
    if (path === "/api/hosts/local/schedules") return { schedules: [{ name: "nightly", agent: "claude", every: "1d" }], timer: {} };
    return [];
  };
  await vm.runInContext("pages.starts(__main)", context); await settle();
  const row = (name) => main.all().find(n => n.tagName === "tr" && n.attributes["data-name"] === name);
  const icons = (r) => buttons(r).filter(b => b.attributes["data-icon"]).map(b => [b.attributes["data-icon"], b.textContent,
    b.attributes.title.split("\n")[0], b.attributes["aria-label"], b.children[0].tagName,
    b.children[0].children[0].children.map(p => p.tagName).join(",")]);
  result.web = icons(row("web"));
  result.old = icons(row("old"));
  result.plain = buttons(row("web")).filter(b => !b.attributes["data-icon"]).map(b => b.textContent);
  result.tip = buttons(row("web")).find(b => b.attributes["data-icon"] === "pause").attributes.title;
  result.optimize = buttons(main).filter(b => b.attributes["data-icon"] === "optimize").map(b => b.attributes.title.split("\n")[0]);
  // the texts beside the icons: a class of the page, kept in this browser
  const box = byId("button-texts");
  result.textsBefore = [box.checked, body.className];
  box.checked = true; box.fire("change");
  result.textsAfter = [body.className, stored["sandbox-ai-button-texts"]];
  result.textsHelp = box.parent.attributes.title;
  // the tabs
  const tabs = main.all().filter(n => n.attributes.role === "tab");
  result.tabs = tabs.map(t => [t.textContent, t.attributes["aria-selected"]]);
  const panels = () => ["starts-panel", "schedules-panel"].map(id => byId(id).hidden);
  result.panelsFirst = panels();
  result.schedulesThere = byId("schedules-panel").textContent.includes("nightly");
  tabs[1].fire("click");
  result.panelsSchedules = [panels(), tabs.map(t => t.attributes["aria-selected"]), stored["sandbox-ai-starts-tab"]];
  // drawn anew (another visit): the tab chosen stays; every host: the starts (schedules are each host's own)
  await vm.runInContext("pages.starts(__main)", context); await settle();
  result.kept = panels();
  vm.runInContext('state.startsHost = "all";', context);
  answer = (method, path) => (path === "/api/starts" ? { starts: [], errors: {}, seen: {} } : []);
  await vm.runInContext("pages.starts(__main)", context); await settle();
  const allTabs = main.all().filter(n => n.attributes.role === "tab");
  result.all = [panels(), allTabs[1].attributes.disabled !== undefined];
  console.log(JSON.stringify(result));
})().catch(e => { console.error(e.stack); process.exit(1); });
"""


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_the_rows_actions_are_icon_buttons_with_tooltips(tmp_path):
    result = run_driver(tmp_path, DRIVER, "sessions_icons")
    web = {key: rest for key, *rest in result["web"]}
    assert list(web) == ["attach", "shell", "pause", "allow", "limits", "export", "resume-with", "checkpoints",
                         "memory", "files", "storage"]
    assert web["attach"] == ["Attach", "Attach", "Attach web on local", "svg", "rect,path,path"], \
        "its text inside (shown with the texts), first in the tooltip, in the accessible name; the drawing as SVG"
    assert web["allow"][:2] == ["Network…", "Network…"] and web["limits"][0] == "Resources…"
    assert result["tip"] == "Pause\nfreeze it as it is (sandbox-ai pause)"
    old = {key: rest for key, *rest in result["old"]}
    assert list(old)[:2] == ["resume", "delete"] and old["delete"][2] == "Delete old on local"
    assert result["plain"][:2] == ["Stop", "Rename…"], "Stop, Rename… and History keep their words, as in the app"
    assert result["optimize"] == ["Optimize…"]


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_show_button_texts_is_kept_in_this_browser(tmp_path):
    result = run_driver(tmp_path, DRIVER, "sessions_texts")
    assert not result["textsBefore"][0] and result["textsBefore"][1] == "", "off by default: the icons alone"
    assert result["textsAfter"] == ["button-texts", "1"]
    assert result["textsHelp"].startswith("On: the icon buttons of the starts' rows show their texts too.")


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_the_starts_page_has_two_tabs_as_the_apps_sessions_page(tmp_path):
    result = run_driver(tmp_path, DRIVER, "sessions_tabs")
    assert result["tabs"] == [["Sessions & sandboxes", "true"], ["Schedules", "false"]]
    assert result["panelsFirst"] == [False, True] and result["schedulesThere"] is True
    assert result["panelsSchedules"] == [[True, False], ["false", "true"], "schedules"]
    assert result["kept"] == [True, False], "the tab chosen stays when the page is drawn anew"
    assert result["all"] == [[False, True], True], "every host: the starts; Schedules is each host's own"


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_in_german(tmp_path):
    driver = tmp_path / "de.js"
    driver.write_text(DRIVER)
    import subprocess
    run = subprocess.run(["node", str(driver), str(ROOT / "sandbox" / "web" / "app.js"), "de"], capture_output=True,
                         text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    assert result["tabs"] == [["Sitzungen & Sandboxen", "true"], ["Zeitpläne", "false"]]
    web = {key: rest for key, *rest in result["web"]}
    assert web["attach"][:2] == ["Verbinden", "Verbinden"] and web["pause"][0] == "Pausieren"
    assert result["tip"] == "Pausieren\nihn so einfrieren, wie er ist (sandbox-ai pause)"


def app_drawings():
    """The desktop app's drawings (button_icons.SVGS) without Qt: its constants, read from its source."""
    source = ICONS_PY.read_text()
    found = {}
    exec(source[source.index("SIZE = 18"):source.index("ICON_ONLY =")], found)  # noqa: S102 - the repo's own source
    return found["SVGS"]


def test_the_drawings_are_the_apps():
    web = json.loads(re.search(r"^const BUTTON_ICONS = (\{.*\});$", APP_JS, re.M).group(1))
    app = app_drawings()
    for key, parts in web.items():
        drawing = ET.fromstring(app[key])
        group = drawing[0]
        assert drawing.attrib["viewBox"] == "0 0 24 24" and group.attrib["stroke"] == "currentColor"
        assert parts == [[child.tag.split("}")[1], dict(child.attrib)] for child in group], key
    assert set(web) >= {"attach", "shell", "pause", "resume", "delete", "export", "checkpoints", "files", "storage"}
    assert {tag for parts in web.values() for tag, _ in parts} <= {"path", "rect", "circle", "ellipse"}
