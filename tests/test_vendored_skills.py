"""The third-party skills sandbox-ai ships as built-in kits (hallmark, superpowers, playwright-skill, hyperframes,
security-audit, design-system, ffuf, tdd; hallmark-hook for hallmark's edit-time lint hook): vendored, reviewed
(docs/skills-review.md), pinned to an upstream commit at least 14 days old, the same files for Claude Code, Codex
and OpenCode, off unless a start names the kit, their hooks off until approved, their telemetry off, and nothing
fetched at run time.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


s = load("sandbox_for_vendored_skills", "sandbox/agent_sandbox.py")
k = load("kits_for_vendored_skills", "sandbox/kits.py")
kitctl = load("kitctl_for_vendored_skills", "sandbox/kitctl.py")
AGENTS = ("claude", "codex", "opencode")
# The latest commit date a pin may have: 14 days before the release that brought these kits (0.1.94, 2026-11-12).
NEWEST_PIN = "2026-10-29"

# kit -> (upstream repository, pinned commit, the skills it brings)
VENDORED = {
    "hallmark": ("https://github.com/Nutlope/hallmark", "8f6969f8922caabde9de1784959eae11ddf68073", ["hallmark"]),
    "superpowers": ("https://github.com/obra/superpowers", "2ce8aa15d05bd57213e6f746f953aafb7ded7a8b",
                    ["brainstorming", "dispatching-parallel-agents", "executing-plans",
                     "finishing-a-development-branch", "receiving-code-review", "requesting-code-review",
                     "subagent-driven-development", "systematic-debugging", "test-driven-development",
                     "using-git-worktrees", "using-superpowers", "verification-before-completion", "writing-plans",
                     "writing-skills"]),
    "playwright-skill": ("https://github.com/lackeyjb/playwright-skill", "dd47a6a023e249eb1b36e9e943eab89d0900865d",
                         ["playwright-skill"]),
    "hyperframes": ("https://github.com/heygen-com/hyperframes", "cfa653af4d0f95d751286fb25c8d38bdb56ef663",
                    ["figma", "hyperframes", "hyperframes-cli", "hyperframes-registry", "media-use",
                     "remotion-to-hyperframes", "slideshow"]),
    "security-audit": ("https://github.com/cloudflare/security-audit-skill",
                       "c1c8a8c1471069fb0e188eeaff69b8e8db6564a8", ["security-audit"]),
    "design-system": ("https://github.com/anthropics/knowledge-work-plugins",
                      "2574e29377c7173d0cecffef29a6ecbd7f8ccb5d", ["design-system"]),
    "ffuf": ("https://github.com/jthack/ffuf_claude_skill", "50fd4af4d8bd60cfc48fd84eda1720fbd718857d",
             ["ffuf-web-fuzzing"]),
    "tdd": ("https://github.com/mattpocock/skills", "59db609eb1e18aaf59b63284d0e52255aff0c63c", ["tdd"]),
}
# Files of sandbox-ai's own in a vendored kit (kit.json lists every upstream file).
OWN = {"kit.json", "README.md", "NOTICE"}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    home = tmp_path / "host"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in ("XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "CODEX_HOME",
                 "CLAUDE_CONFIG_DIR"):
        monkeypatch.delenv(name, raising=False)
    return home


def args(tmp_path, agent="claude", *options):
    a = s.parse_args([agent, "--workspace", str(tmp_path / "work"), "--no-auth", *options])
    s.validate(a)
    return a


def provenance(kit):
    return json.loads((k.BUILTIN_KITS / kit / "kit.json").read_text())


def blob_id(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def front_matter(path):
    text = path.read_text()
    match = re.match(r"---\n(.*?)\n---\n", text, re.S)
    assert match, f"{path}: no front matter"
    return {key: value.strip().strip("\"'") for key, value in re.findall(r"^([A-Za-z][\w-]*):(.*)$", match.group(1),
                                                                        re.M)}


# --- provenance: pinned, unchanged, licensed --------------------------------------------------------------------

@pytest.mark.parametrize("kit", sorted(VENDORED))
def test_the_vendored_files_are_exactly_the_pinned_upstream_files(kit):
    """kit.json names the repository and the full commit; every file it lists is there with its SHA-256 and the
    git blob id of the upstream file at that commit (so `git ls-tree -r COMMIT` upstream proves it unchanged), and
    the kit holds nothing else but sandbox-ai's own README, NOTICE, kit.json and the agents' markers."""
    repository, commit, skills = VENDORED[kit]
    document = provenance(kit)
    assert document["upstream"]["repository"] == repository and document["upstream"]["commit"] == commit
    assert re.fullmatch(r"[0-9a-f]{40}", commit)
    assert document["upstream"]["committed"][:10] <= NEWEST_PIN, "pins are at least 14 days old"
    assert sorted(document["skills"]) == sorted(skills)
    assert document["agents"] == list(AGENTS) and document["default"] == "off"
    root = k.BUILTIN_KITS / kit
    present = set()
    for path in root.rglob("*"):
        if path.is_file():
            present.add(path.relative_to(root).as_posix())
        assert not path.is_symlink(), path
    listed = set(document["files"])
    markers = {f"{agent}/{k.SHARED_SKILLS_MARKER}" for agent in AGENTS}
    assert present == listed | OWN | markers, present ^ (listed | OWN | markers)
    for relative, entry in document["files"].items():
        data = (root / relative).read_bytes()
        assert hashlib.sha256(data).hexdigest() == entry["sha256"], relative
        assert blob_id(data) == entry["blob"], relative
        assert bool((root / relative).stat().st_mode & 0o111) == (entry["mode"] == "100755"), relative


@pytest.mark.parametrize("kit", sorted(VENDORED))
def test_every_vendored_kit_keeps_the_upstream_licence_and_a_notice(kit):
    root = k.BUILTIN_KITS / kit
    document = provenance(kit)
    notice = (root / "NOTICE").read_text()
    assert document["upstream"]["repository"] in notice and document["upstream"]["commit"] in notice
    assert "docs/skills-review.md" in notice
    if kit == "ffuf":
        # Upstream states MIT in its README and ships no licence file: the README is kept as the evidence.
        assert "MIT License" in (root / "UPSTREAM-README.md").read_text()
        assert "no LICENSE file" in notice and "MIT" in document["upstream"]["license"]
    else:
        assert "LICENSE" in document["files"] and document["files"]["LICENSE"]["upstream"] == "LICENSE"
        text = (root / "LICENSE").read_text()
        assert ("Apache License" in text) == (document["upstream"]["license"] == "Apache-2.0")
    assert (root / "README.md").read_text().strip(), "the first line describes the kit (kits list, the app)"


def test_the_pins_are_the_ones_the_review_names():
    review = (ROOT / "docs/skills-review.md").read_text()
    for kit, (repository, commit, _) in VENDORED.items():
        assert commit in review and repository in review, kit
    for heading in ("## hallmark", "## superpowers", "## playwright-skill", "## claude-d3js-skill", "## hyperframes",
                    "## security-audit", "## design-system", "## ffuf", "## tdd"):
        assert heading in review, heading


def test_the_unlicensed_d3js_skill_is_not_vendored():
    """chrisvoncsefalvay/claude-d3js-skill has no licence: nothing of it may be redistributed."""
    assert not (k.BUILTIN_KITS / "d3js").exists()
    assert not list(k.BUILTIN_KITS.glob("*/skills/d3-viz"))


@pytest.mark.parametrize("kit", sorted(VENDORED))
def test_every_skill_is_valid_for_claude_codex_and_opencode(kit):
    """Each skill folder has a SKILL.md whose name is the folder's (OpenCode requires it; ^[a-z0-9]+(-[a-z0-9]+)*$)
    and a description of 1 to 1024 characters (OpenCode's and Codex's limit); no SKILL.md sits deeper (OpenCode
    would take it for another skill)."""
    for skill in VENDORED[kit][2]:
        folder = k.BUILTIN_KITS / kit / "skills" / skill
        meta = front_matter(folder / "SKILL.md")
        assert meta["name"] == skill and re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", skill)
        assert 0 < len(meta["description"]) <= 1024
        assert [p for p in folder.rglob("SKILL.md") if p.parent != folder] == []


# --- every agent sees them, only when the kit is named -----------------------------------------------------------

@pytest.mark.parametrize("agent", AGENTS)
@pytest.mark.parametrize("kit", sorted(VENDORED))
def test_the_kit_brings_its_skills_to_every_agent_in_a_dry_start(tmp_path, kit, agent):
    """--kit NAME: the dry run's kit plan lists the kit's skills for every agent, and a start stages them read-only
    where the agent looks: ~/.claude/skills (Claude Code), ~/.agents/skills (Codex), ~/.config/opencode/skills
    (OpenCode)."""
    a = args(tmp_path, agent, "--kit", kit)
    plan = s.kit_plan(a)
    assert {"kit": f"kit {kit}", "path": str(k.BUILTIN_KITS / kit / agent)} in plan["sources"]
    assert set(VENDORED[kit][2]) <= set(plan["entries"]["skills"])
    home, staging = tmp_path / "home", tmp_path / "staging"
    home.mkdir()
    mounts, notes, _ = s.prepare_kits(a, home, staging)
    skills_mount = next(source for source, target, mode in mounts
                        if target == f"/root/{k.LAYOUT[agent]['skills']}" and mode == "ro")
    for skill in VENDORED[kit][2]:
        staged = Path(skills_mount) / skill / "SKILL.md"
        assert staged.read_bytes() == (k.BUILTIN_KITS / kit / "skills" / skill / "SKILL.md").read_bytes()
    assert "sandbox-ai" in os.listdir(skills_mount)  # sandbox-ai's own skill stays
    assert not any("not used for" in note for note in notes), notes


@pytest.mark.parametrize("agent", AGENTS)
def test_no_vendored_skill_without_the_kit(tmp_path, agent):
    """Off by default: a start that names no kit gets only sandbox-ai's own skill and the default kit's."""
    skills = set(s.kit_plan(args(tmp_path, agent))["entries"].get("skills", []))
    for kit, (_, _, names) in VENDORED.items():
        assert not skills & set(names), kit
    template = json.loads((k.TEMPLATES / "claude/marketplaces.json").read_text())
    assert not [p for p in template.get("plugins", []) if p.split("@")[0] in VENDORED]


def test_a_shared_skills_folder_counts_only_for_sandbox_ais_own_kits(tmp_path):
    """The marker gives a built-in kit's agent folder the kit's shared skills/; in a kit of the user's it means
    nothing (a user's kit holds its skills in each agent folder, as always)."""
    mine = tmp_path / "mine"
    (mine / "skills/sneaky").mkdir(parents=True)
    (mine / "skills/sneaky/SKILL.md").write_text("---\nname: sneaky\ndescription: x\n---\n")
    (mine / "claude").mkdir()
    (mine / "claude" / k.SHARED_SKILLS_MARKER).write_text("")
    assert k.shared_skills(mine / "claude") is None
    plan = k.scan("claude", [("kit-dir mine", mine / "claude")])
    assert "sneaky" not in plan["entries"]["skills"]
    assert k.shared_skills(k.BUILTIN_KITS / "tdd/codex") == k.BUILTIN_KITS / "tdd/skills"
    assert k.shared_skills(k.BUILTIN_KITS / "egm/claude") is None


def test_two_kits_with_the_same_skill_are_refused_and_the_tdd_skills_do_not_collide(tmp_path):
    """superpowers' test-driven-development and Matt Pocock's tdd are separate skills: both kits together work."""
    plan = s.kit_plan(args(tmp_path, "claude", "--kit", "superpowers", "--kit", "tdd"))
    assert {"test-driven-development", "tdd"} <= set(plan["entries"]["skills"])


def test_kits_list_shows_every_vendored_kit_with_its_skills_per_agent():
    inventory = kitctl.inventory()
    found = {kit["name"]: kit for kit in inventory["kits"] if kit["kind"] == "builtin"}
    for kit, (_, _, skills) in VENDORED.items():
        assert set(found[kit]["agents"]) == set(AGENTS), kit
        for agent in AGENTS:
            assert found[kit]["agents"][agent]["skills"] == sorted(skills), (kit, agent)
            assert found[kit]["agents"][agent]["error"] is None
        assert found[kit]["description"] and not found[kit]["editable"]
    assert set(found["hallmark-hook"]["agents"]) == {"claude"}
    assert set(found["egm"]["agents"]) == {"claude"}


# --- hooks off, nothing fetched ----------------------------------------------------------------------------------

def test_no_vendored_kit_has_hooks_and_hallmarks_hook_is_opt_in(tmp_path):
    """No vendored kit runs a hook. hallmark's edit-time lint hook is a kit of its own (hallmark-hook, Claude only):
    named at a start it still runs only after `sandbox-ai kits trust-hooks hallmark-hook`; its installer
    (install-hook.mjs, which rewrites a settings.json) is not vendored."""
    for kit in VENDORED:
        assert not list((k.BUILTIN_KITS / kit).glob("*/hooks")), kit
        assert not list((k.BUILTIN_KITS / kit).glob("**/hooks.json")), kit
    assert not (k.BUILTIN_KITS / "hallmark/skills/hallmark/scripts/install-hook.mjs").exists()
    assert (k.BUILTIN_KITS / "hallmark/skills/hallmark/scripts/lint-hook.mjs").is_file()
    a = args(tmp_path, "claude", "--kit", "hallmark", "--kit", "hallmark-hook")
    plan = s.kit_plan(a)
    hook = next(h for h in plan["hooks"] if h["kit"] == "kit hallmark-hook")
    assert hook["hooks"] == 1 and hook["errors"] == [] and hook["trusted"] is False
    info = k.read_hooks("claude", k.BUILTIN_KITS / "hallmark-hook/claude/hooks")
    assert [(h["event"], h["matcher"]) for h in info["hooks"]] == [("PostToolUse", "Write|Edit|MultiEdit")]
    assert "/root/.claude/skills/hallmark/scripts/lint-hook.mjs" in info["hooks"][0]["command"]
    # Not approved: the start installs no hook of it.
    home, staging = tmp_path / "home", tmp_path / "staging"
    home.mkdir()
    s.prepare_kits(a, home, staging)
    assert not [h for h in (a.kit_hooks or []) if "hallmark" in json.dumps(h, default=str)
                and h.get("trusted")]
    for agent in ("codex", "opencode"):
        with pytest.raises(ValueError, match="only for claude"):
            k.sources(s.parse_args([agent, "--kit", "hallmark-hook"]))


def test_the_vendored_kits_install_nothing_from_the_network(tmp_path):
    """No marketplace, no plugin, no npm plugin: the skills are copied from sandbox-ai's own files at a start."""
    for kit in VENDORED:
        assert not list((k.BUILTIN_KITS / kit).glob(f"*/{k.PLUGINS_FILE}")), kit
    for agent in AGENTS:
        a = args(tmp_path, agent, *[option for kit in VENDORED for option in ("--kit", kit)])
        plan = s.kit_plan(a)
        plugins = plan.get("plugins") or {}
        assert not [p for p in plugins.get("missing", []) + plugins.get("npm", []) if p.split("@")[0] in VENDORED]
        for label, path in k.sources(a):
            assert not str(path).startswith(("http:", "https:"))
    # No hyperframes plugin manifest two folders above its SKILL.md: its plugin launcher (which fetches the CLI
    # through npm) is never chosen; the skills run the hyperframes the image recipe installs.
    for name in ("plugin.json", ".claude-plugin", ".codex-plugin"):
        assert not (k.BUILTIN_KITS / "hyperframes" / name).exists()


def test_the_superpowers_plugin_is_pinned_to_a_commit_at_least_14_days_old():
    """The marketplace entry superpowers@sandbox-ai (for whoever wants the plugin with its SessionStart hook) is
    pinned to the commit the kit vendors, v7.0.1; v7.0.2 (2026-11-03) was younger than 14 days."""
    manifest = json.loads((k.MARKETPLACE / ".claude-plugin/marketplace.json").read_text())
    entry = next(e for e in manifest["plugins"] if e["name"] == "superpowers")
    assert entry["source"]["sha"] == VENDORED["superpowers"][1] and entry["version"] == "7.0.1"


# --- telemetry off -------------------------------------------------------------------------------------------------

TELEMETRY_OFF = {"HYPERFRAMES_NO_TELEMETRY": "1", "DO_NOT_TRACK": "1", "HYPERFRAMES_NO_UPDATE_CHECK": "1",
                 "HYPERFRAMES_NO_AUTO_INSTALL": "1", "HYPERFRAMES_NO_OPTIONAL_INSTALL": "1",
                 "HYPERFRAMES_SKIP_SKILLS": "1", "SUPERPOWERS_DISABLE_TELEMETRY": "1"}


@pytest.mark.parametrize("agent", AGENTS)
def test_the_skills_telemetry_is_off_in_every_sandbox_by_default(tmp_path, agent):
    """hyperframes (its CLI and media-use's scripts: PostHog), its update checks and installs at run time, and
    superpowers' visual companion (a remote logo with its version) are switched off with the telemetry default."""
    env = s.telemetry_environment(args(tmp_path, agent))
    for name, value in TELEMETRY_OFF.items():
        assert env.get(name) == value, name
    assert not set(TELEMETRY_OFF) & set(s.telemetry_environment(args(tmp_path, agent, "--telemetry", "on")))


NODE = shutil.which("node")
PROBE = """
import { pathToFileURL } from "node:url";
let calls = 0;
globalThis.fetch = async (url) => { calls += 1; return { ok: true, status: 200, body: null, text: async () => "" }; };
const telemetry = await import(pathToFileURL(process.argv[2]).href);
await telemetry.track("media_use_resolve", { type: "image" });
console.log(JSON.stringify({ calls, optedOut: telemetry.optedOut() }));
"""


@pytest.mark.skipif(NODE is None, reason="needs node")
@pytest.mark.parametrize("opted_out", [True, False])
def test_the_vendored_hyperframes_telemetry_sends_nothing_with_sandbox_ais_environment(tmp_path, opted_out):
    """The opt-out proven on the vendored code itself: media-use's telemetry.mjs with the variables a sandbox gets
    calls no fetch at all; without them (the control) it would post to PostHog (here a stub, nothing leaves)."""
    script = tmp_path / "probe.mjs"
    script.write_text(PROBE)
    home = tmp_path / "probe-home"
    home.mkdir()
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home)}
    if opted_out:
        env.update(s.telemetry_environment(args(tmp_path, "claude")))
    target = k.BUILTIN_KITS / "hyperframes/skills/media-use/scripts/lib/telemetry.mjs"
    result = subprocess.run([NODE, str(script), str(target)], env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    outcome = json.loads(result.stdout.strip().splitlines()[-1])
    assert outcome == ({"calls": 0, "optedOut": True} if opted_out else {"calls": 1, "optedOut": False})
    assert not (home / ".hyperframes").exists() or not opted_out  # opted out: not even an id is written


# --- the CLIs the skills need, installed when an image is built, never at run time ---------------------------------

im = load("images_for_vendored_skills", "sandbox/images.py")
RECIPE_PINS = {"hyperframes": ("hyperframes", "0.8.214", 125), "playwright": ("playwright", "1.64.1", 2)}


@pytest.mark.parametrize("recipe", sorted(RECIPE_PINS))
def test_the_cli_recipes_pin_every_npm_package_by_its_sha512(recipe):
    """sandbox/recipes/NAME: package.json names one exact version, the lockfile every package with its exact version,
    the registry's tarball and SHA-512, and no package needs an install script except esbuild's (which npm ci
    --ignore-scripts skips: its platform binary comes as an optional package of the lockfile)."""
    package, version, count = RECIPE_PINS[recipe]
    folder = im.RECIPE_FILES / recipe
    manifest = json.loads((folder / "package.json").read_text())
    assert manifest["dependencies"] == {package: version} and manifest["private"] is True
    lock = json.loads((folder / "package-lock.json").read_text())
    assert lock["lockfileVersion"] == 3 and lock["packages"][""]["dependencies"] == {package: version}
    entries = {path: entry for path, entry in lock["packages"].items() if path}
    assert len(entries) == count and entries[f"node_modules/{package}"]["version"] == version
    for path, entry in entries.items():
        assert re.fullmatch(r"\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?", entry["version"]), path
        assert entry["resolved"].startswith("https://registry.npmjs.org/"), path
        assert re.fullmatch(r"sha512-[A-Za-z0-9+/]{86}==", entry["integrity"]), path
        assert not entry.get("hasInstallScript") or path == "node_modules/esbuild", path


@pytest.mark.parametrize("recipe", sorted(RECIPE_PINS))
def test_the_cli_recipes_build_from_the_lockfile_with_no_install_scripts_and_debians_browser(tmp_path, monkeypatch,
                                                                                             recipe):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "state/sandbox-ai").mkdir(parents=True)
    (tmp_path / "state/sandbox-ai/exposed.json").write_text("[]")
    lines = []
    assert im.main(["recipe", recipe, "--from", "claude"], out=lines.append) == 0
    document, fragment, files = im.load(recipe)
    assert im.errors(im.check(document, fragment, files)) == []
    assert "chromium" in document["apt"] and not document.get("npm")  # no `npm install -g` of a range
    assert {e["target"] for e in document["files"]} == {f"/opt/{recipe}/package.json",
                                                        f"/opt/{recipe}/package-lock.json"}
    assert files["package-lock.json"] == (im.RECIPE_FILES / recipe / "package-lock.json").read_bytes()
    text = im.containerfile(recipe, document, fragment)
    assert f"cd /opt/{recipe}" in text and "npm ci --omit=dev --ignore-scripts" in text
    assert "npm install" not in text and "playwright install" not in text and "curl" not in text
    if recipe == "hyperframes":
        assert "HYPERFRAMES_BROWSER_PATH=/usr/bin/chromium" in text and "ffmpeg" in document["apt"]
        assert "/usr/local/bin/hyperframes" in text  # what `npx hyperframes` runs instead of downloading it
    else:
        assert "PW_EXECUTABLE_PATH=/usr/bin/chromium" in text and "/usr/local/lib/node/playwright" in text


def test_the_packaging_ships_the_recipes_lockfiles_and_the_kits_markers():
    tomllib = pytest.importorskip("tomllib")
    patterns = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"]["package-data"]["sandbox_ai"]
    assert "recipes/*/*.json" in patterns and "builtin/kits/**/*" in patterns
    assert "builtin/kits/*/*/.shared-skills" in patterns  # globs leave dot files out
