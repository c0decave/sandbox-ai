"""sandbox-ai install (todos 2026-11-24, an Install menu: all, nothing or a part): the list of what can be installed -
agent images, MCP servers of the signed catalogs, toolchains, what presets need - with its state and size, and the
installation through the commands that do it (build, mcp add, toolchain add), signed things only."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import home_guard  # noqa: E402

home_guard.ensure()

SPEC = importlib.util.spec_from_file_location("sandbox_install_cmd", ROOT / "sandbox" / "install.py")
I = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(I)

CATALOG = {"entries": [
    {"entry": "hub:binwalk", "prefix": "hub", "name": "binwalk", "title": "binwalk", "published": True,
     "signature": "good", "env": []},
    {"entry": "mcp:binwalk", "prefix": "mcp", "name": "binwalk", "title": "binwalk", "published": True,
     "signature": "good", "env": []},
    {"entry": "mcp:duckduckgo", "prefix": "mcp", "name": "duckduckgo", "title": "DuckDuckGo", "published": True,
     "signature": "good", "env": []},
    {"entry": "mcp:brave-search", "prefix": "mcp", "name": "brave-search", "title": "Brave", "published": True,
     "signature": "good", "env": [{"name": "BRAVE_API_KEY", "required": True}]},
    {"entry": "hub:nmap", "prefix": "hub", "name": "nmap", "title": "Nmap", "published": False, "signature": "good",
     "env": []},
    {"entry": "own:evil", "prefix": "own", "name": "evil", "title": "unsigned", "published": True,
     "signature": "none", "env": []}], "problems": []}
TOOLCHAINS = {"toolchains": [
    {"name": "go", "version": "1.27.3", "toolchain": "go", "title": "Go", "size": 48296952, "installed": True,
     "signed": True, "requires": []},
    {"name": "rust", "version": "1.99.1", "toolchain": "rust", "title": "Rust", "size": 150935140,
     "installed": False, "signed": True, "requires": ["cc"]},
    {"name": "jdk", "version": "17.0.21+9", "toolchain": "jdk@17", "title": "Java 17", "size": 396208172,
     "installed": False, "signed": True, "requires": []},
    {"name": "bad", "version": "1", "toolchain": "bad", "title": "unsigned", "installed": False, "signed": False}]}
PRESETS = [{"id": "rust", "title": "Rust", "agents": ["claude", "codex"], "options": ["--toolchain", "rust"]},
           {"id": "forensics", "title": "Forensics", "agents": ["codex"], "mcp": ["hub:binwalk", "mcp:duckduckgo"],
            "options": ["--cairn"]},
           {"id": "maze", "title": "Maze", "agents": ["claude"], "options": ["--vpn", "{vpn}", "--toolchain",
                                                                              "{toolchain}"]},
           {"id": "secret", "title": "Needs a key", "agents": ["claude"], "mcp": ["mcp:brave-search"]}]
IMAGES = [{"Names": ["localhost/claude-sandbox:latest"], "Size": 2_000_000_000},
          {"Names": ["localhost/sandbox-ai-cairn:latest"], "Size": 500_000_000}]


def fake_run(argv, **kwargs):
    """sandbox-ai's own listings and podman images, as a runner of subprocess.run."""
    words = argv[3:] if argv[:2] == [sys.executable, "-I"] else argv
    answers = {("mcp", "catalog", "--json"): CATALOG, ("mcp", "list", "--json"): [{"name": "duckduckgo"}],
               ("toolchain", "catalog", "--json"): TOOLCHAINS, ("presets", "list", "--json"): PRESETS,
               ("podman", "images", "--format", "json"): IMAGES}
    return SimpleNamespace(returncode=0, stdout=json.dumps(answers[tuple(words)]), stderr="")


@pytest.fixture
def found(monkeypatch):
    monkeypatch.setattr(I.shutil, "which", lambda name: "/usr/bin/podman" if name == "podman" else None)
    return I.listing(run=fake_run)


def by_id(found):
    return {row["id"]: row for row in found["items"]}


def test_the_list_has_every_kind_with_its_state_and_size(found):
    rows = by_id(found)
    assert rows["image:claude"]["installed"] is True and rows["image:claude"]["size"] == 2_000_000_000
    assert rows["image:codex"]["installed"] is False and rows["image:codex"]["command"] == ["build", "codex"]
    assert rows["image:tor"]["command"] == ["build", "tor"] and rows["image:team"]["installed"] is False
    assert rows["toolchain:go"]["installed"] is True and rows["toolchain:jdk@17"]["size"] == 396208172
    assert rows["toolchain:jdk@17"]["command"] == ["toolchain", "add", "jdk@17"]
    assert "needs cc" in rows["toolchain:rust"]["note"]
    assert rows["mcp:duckduckgo"]["installed"] is True and rows["mcp:duckduckgo"]["command"] == ["mcp", "add",
                                                                                                  "duckduckgo"]
    assert found["problems"] == []


def test_signed_things_only_each_name_once_pending_only_after_its_question(found):
    rows = by_id(found)
    assert "mcp:own:evil" not in rows and "toolchain:bad" not in rows, "an unsigned entry is never offered"
    assert "mcp:binwalk" in rows and "mcp:hub:binwalk" not in rows, "one name once: sandbox-ai's own catalog first"
    nmap = rows["mcp:hub:nmap"]
    assert nmap["pending"] and not nmap["all"] and nmap["command"] == ["mcp", "add", "hub:nmap", "--build"]
    assert "--yes" not in nmap["command"], "the build's own question is asked, never answered for it"
    brave = rows["mcp:brave-search"]
    assert not brave["all"] and "BRAVE_API_KEY" in brave["note"]


def test_a_presets_needs(found):
    rows = by_id(found)
    assert rows["preset:rust"]["needs"] == ["image:claude", "toolchain:rust"], "an image of its agents built: that"
    assert rows["preset:rust"]["installed"] is False and rows["preset:rust"]["size"] == 150935140
    assert rows["preset:forensics"]["needs"] == ["image:codex", "image:cairn", "mcp:binwalk", "mcp:duckduckgo"]
    assert rows["preset:maze"]["needs"] == ["image:claude"], "placeholders are no needs"
    assert rows["preset:maze"]["installed"] is True
    assert not rows["preset:secret"]["all"]


def test_the_plan_of_items_all_and_a_preset(found):
    steps, skipped, unknown = I.plan(found, ["preset:forensics", "image:claude", "toolchain:jdk@17", "mcp:nothing"])
    assert unknown == ["mcp:nothing"]
    assert [s[0] for s in steps] == ["image:codex", "toolchain:jdk@17", "mcp:binwalk"], "images, toolchains, MCP"
    assert ("image:claude", "installed already") in skipped and ("mcp:duckduckgo", "installed already") in skipped
    steps, skipped, _ = I.plan(found, [], every=True, kinds=("mcp",))
    assert [s[0] for s in steps] == ["mcp:binwalk"]
    assert {name for name, _ in skipped} == {"mcp:duckduckgo", "mcp:brave-search", "mcp:hub:nmap"}
    steps, _, _ = I.plan(found, ["mcp:hub:nmap"])
    assert steps == [("mcp:hub:nmap", ["mcp", "add", "hub:nmap", "--build"])], "named: built after its question"


def test_install_runs_each_command_and_goes_on_after_a_failure():
    called, lines = [], []

    def call(argv):
        called.append(argv[3:])
        return 1 if argv[3:5] == ["build", "codex"] else 0
    done, failed = I.install([("image:codex", ["build", "codex"]), ("toolchain:rust", ["toolchain", "add", "rust"])],
                             lines.append, call)
    assert called == [["build", "codex"], ["toolchain", "add", "rust"]]
    assert done == ["toolchain:rust"] and failed == ["image:codex"]
    assert lines[0] == "sandbox-ai install: [1/2] image:codex: sandbox-ai build codex"
    assert "failed (exit status 1)" in lines[1] and lines[-1].endswith("toolchain:rust: done")


def test_main_list_json_dry_run_and_refusals(monkeypatch, capsys):
    monkeypatch.setattr(I.shutil, "which", lambda name: "/usr/bin/podman" if name == "podman" else None)
    lines = []
    assert I.main(["--list", "--json", "--kind", "toolchains"], lines.append, run=fake_run) == 0
    listed = json.loads("\n".join(lines))
    assert {row["kind"] for row in listed["items"]} == {"toolchains"}
    lines = []
    assert I.main(["--all", "--kind", "toolchains", "--dry-run", "--json"], lines.append, run=fake_run) == 0
    assert [s["command"] for s in json.loads("\n".join(lines))["steps"]] == [
        ["sandbox-ai", "toolchain", "add", "rust"], ["sandbox-ai", "toolchain", "add", "jdk@17"]]
    lines = []
    assert I.main(["--list"], lines.append, run=fake_run) == 0
    assert any(line.startswith("  image:claude") and "installed" in line and "2.0 GB" in line for line in lines)
    for argv in ([], ["--list", "--all"], ["--all", "image:claude"], ["claude"], ["--list", "image:claude"]):
        assert I.main(argv, lines.append, run=fake_run) == 2, argv
    calls = []
    assert I.main(["image:codex", "image:claude"], lines.append, run=fake_run,
                  call=lambda argv: calls.append(argv[3:]) or 0) == 0
    assert calls == [["build", "codex"]] and lines[-2:] == ["sandbox-ai install: 1 installed, 1 skipped, 0 failed",
                                                             "  skipped image:claude: installed already"]


def test_the_image_names_are_the_launchers(found):
    launcher = I.launcher()
    assert I.OTHER_IMAGES["team"][0] == launcher.TEAM_IMAGE
    assert I.OTHER_IMAGES["cairn"][0] == launcher.CAIRN_IMAGE and I.OTHER_IMAGES["artex"][0] == launcher.ARTEX_IMAGE
    assert I.OTHER_IMAGES["browser"][0] == launcher.BROWSER_IMAGE
    spec = importlib.util.spec_from_file_location("sandbox_vpn_for_install_test", ROOT / "sandbox" / "vpn.py")
    vpn = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vpn)
    assert (I.OTHER_IMAGES["vpn"][0], I.OTHER_IMAGES["tor"][0]) == (vpn.VPN_IMAGE, vpn.TOR_IMAGE)
    images = importlib.util.spec_from_file_location("sandbox_images_for_install_test", ROOT / "sandbox" / "images.py")
    module = importlib.util.module_from_spec(images)
    images.loader.exec_module(module)
    assert set(module.BUILD_TARGETS) - {"all"} == {r["name"] for r in found["items"] if r["kind"] == "images"}


def test_the_command_through_sandbox_ai(tmp_path):
    """e2e through the real entry point: its help, and a dry run of the images (no catalog is read for them)."""
    # (only python3 and readlink on PATH, no podman: not known whether built, so every image is a step)
    tools = tmp_path / "bin"
    tools.mkdir()
    for name, path in (("python3", sys.executable), ("readlink", shutil.which("readlink"))):
        (tools / name).symlink_to(path)
    env = {**os.environ, "XDG_CONFIG_HOME": str(tmp_path / "cfg"), "TMPDIR": str(tmp_path), "PATH": str(tools)}
    bash = shutil.which("bash")
    run = subprocess.run([bash, str(ROOT / "sandbox-ai"), "install", "--help"], capture_output=True, text=True,
                         timeout=60, env=env)
    assert run.returncode == 0 and "KIND:NAME" in run.stdout
    run = subprocess.run([bash, str(ROOT / "sandbox-ai"), "install", "--all", "--kind", "images", "--dry-run"],
                         capture_output=True, text=True, timeout=120, env=env)
    assert run.returncode == 0, run.stderr
    assert "image:claude: sandbox-ai build claude" in run.stdout and "image:tor: sandbox-ai build tor" in run.stdout
    run = subprocess.run([bash, str(ROOT / "sandbox-ai"), "--help"], capture_output=True, text=True, timeout=60,
                         env=env)
    assert "  install " in run.stdout


def load_webui():
    spec = importlib.util.spec_from_file_location("webui_for_install_cmd", ROOT / "sandbox" / "webui.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_web_lists_there_and_starts_it_in_a_terminal():
    """Web: at least the list and the start - GET /api/hosts/HOST/install-items (install --list --json there) and the
    terminal kind install-items (sandbox-ai install ITEM... there), every item checked, never an option."""
    web = load_webui()
    assert web.tool_command("install-items", {"items": "image:claude,mcp:hub:binwalk,toolchain:jdk@17,preset:rust"}) \
        == ["install", "image:claude", "mcp:hub:binwalk", "toolchain:jdk@17", "preset:rust"]
    for bad in ("", "--all", "image:claude,--all", "claude", "image:-x", ",".join(["image:claude"] * 201)):
        with pytest.raises(web.Refused):
            web.tool_command("install-items", {"items": bad})
    assert web.INSTALL_ITEM.pattern == I.ITEM.pattern, "the web takes what sandbox-ai install takes"
    source = (ROOT / "sandbox" / "webui.py").read_text()
    assert 'H + r"/install-items", self.install_list' in source
    assert '["install", "--list", "--json"]' in source
