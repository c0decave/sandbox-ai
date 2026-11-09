"""sandbox-ai kits (sandbox/kitctl.py): the inventory and the commands that change your own kits.

Built-in kits and sandbox-ai's marketplace are faked (kits.BUILTIN, kits.BUILTIN_KITS, kits.MARKETPLACE), so the
tests hold with and without the built-in kits of the egm branch; one test checks the real kit egm when it exists.
"""
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


kc = load("kitctl_for_tests", "sandbox/kitctl.py")
k = kc.kits()
AGENTS = ("claude", "codex", "opencode")
INVENTORY_KEYS = {"version", "kits_dir", "store", "kits", "marketplace", "installed"}
KIT_KEYS = {"name", "kind", "path", "editable", "replaced", "description", "agents"}
AGENT_KEYS = {"path", "skills", "agents", "commands", "prompts", "memory", "marketplaces", "plugins", "disabled",
              "hooks", "notes", "error"}
PLUGIN_KEYS = {"name", "id", "description", "version", "source", "homepage", "kits"}
SANDBOX_AI = {"source": "directory", "path": "/sandbox/marketplace"}
EGM = {"$comment": "Built-in kit 'egm': enables the Elephant-Goldfish Model plugin.",
       "marketplaces": {"sandbox-ai": {"source": SANDBOX_AI}}, "plugins": ["egm@sandbox-ai"]}
MARKET = {"name": "sandbox-ai", "owner": {"name": "sandbox-ai"}, "plugins": [
    {"name": "sandcastle", "source": "./plugins/sandcastle", "version": "1.0.0", "description": "Demo"},
    {"name": "egm", "source": {"source": "github", "repo": "google/elephant-goldfish-model", "sha": "113a"},
     "description": "Elephant-Goldfish Model", "homepage": "https://egm.dev"}]}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    home = tmp_path / "host"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in ("XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
        monkeypatch.delenv(name, raising=False)
    # Runs of a real login session stay out of the agent-writable check.
    monkeypatch.setattr(kc.module("net"), "USER_RUNTIME", tmp_path / "run-user", raising=False)
    return home


@pytest.fixture
def shipped(tmp_path, monkeypatch):
    """A fake sandbox-ai tree: the built-in skill, a built-in kit egm and sandbox-ai's marketplace."""
    builtin = make(tmp_path / "builtin", {
        "claude/skills/sandbox-ai/SKILL.md": "---\nname: sandbox-ai\n---\n",
        "codex/skills/sandbox-ai/SKILL.md": "---\nname: sandbox-ai\n---\n",
        "kits/egm/claude/marketplaces.json": json.dumps(EGM)})
    market = make(tmp_path / "marketplace", {".claude-plugin/marketplace.json": json.dumps(MARKET)})
    monkeypatch.setattr(k, "BUILTIN", builtin)
    monkeypatch.setattr(k, "BUILTIN_KITS", builtin / "kits", raising=False)
    monkeypatch.setattr(k, "MARKETPLACE", market)
    return builtin


def data():
    return Path(os.environ["HOME"]) / ".local/share/sandbox-ai"


def kits_dir():
    return data() / "kits"


def make(root, files):
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            path.write_bytes(text)
        else:
            path.write_text(text)
    return root


def run(*argv):
    """(exit status, output) of `sandbox-ai kits ARGV`; usage errors exit 2."""
    lines = []
    try:
        status = kc.main(list(argv), out=lines.append)
    except SystemExit as exit_:
        status = exit_.code
    return status, "\n".join(lines)


def tree(root):
    """{relative path: (mode, content or None)} below ROOT, symlinks not followed."""
    found = {}
    for directory, dirs, files in os.walk(root):
        for name in dirs + files:
            path = Path(directory) / name
            info = path.lstat()
            content = path.read_bytes() if stat.S_ISREG(info.st_mode) else None
            found[str(path.relative_to(root))] = (stat.S_IMODE(info.st_mode), content)
    return found


def skill(root, name="demo", files=None):
    return make(root / name, {"SKILL.md": f"---\nname: {name}\ndescription: demo\n---\n", **(files or {})})


def declaration(name, agent="claude"):
    return json.loads((kits_dir() / name / agent / "marketplaces.json").read_text())


# --- the inventory ---------------------------------------------------------------------------------

def user_kits(tmp_path):
    make(kits_dir(), {
        "review/README.md": "\n# Review kit\n\nmore\n",
        "review/claude/skills/check/SKILL.md": "---\nname: check\n---\n",
        "review/claude/agents/critic.md": "critic",
        "review/claude/CLAUDE.md": "memory",
        "review/claude/marketplaces.json": json.dumps({"marketplaces": {"sandbox-ai": {"source": SANDBOX_AI}},
                                                       "plugins": ["sandcastle@sandbox-ai", "x@elsewhere"]}),
        "review/codex/prompts/fix.md": "fix",
        "review/opencode/marketplaces.json": json.dumps({"plugins": ["oc-plugin@1.2.3"]}),
        # A kit of yours named like the built-in kit replaces it for claude.
        "egm/claude/skills/mine/SKILL.md": "---\nname: mine\n---\n"})


def test_inventory_json_lists_every_kit_in_order(shipped, tmp_path):
    user_kits(tmp_path)
    make(data(), {"home/claude/skills/sandbox-example/SKILL.md": "x", "home/claude/marketplaces.json":
                  json.dumps({"$comment": "Default kit plugins", "plugins": []})})
    status, text = run("list", "--json")
    assert status == 0
    inventory = json.loads(text)
    assert inventory == kc.inventory()
    assert set(inventory) == INVENTORY_KEYS and inventory["version"] == 1
    assert inventory["kits_dir"] == str(kits_dir()) and inventory["store"] == str(data() / "plugins/claude")
    assert [(kit["name"], kit["kind"]) for kit in inventory["kits"]] == [
        ("built-in", "always"), ("default", "default"), ("egm", "builtin"), ("egm", "user"), ("review", "user")]
    for kit in inventory["kits"]:
        assert set(kit) == KIT_KEYS
        for info in kit["agents"].values():
            assert set(info) == AGENT_KEYS
    always, default, egm, own_egm, review = inventory["kits"]
    assert always["path"] == str(shipped) and list(always["agents"]) == ["claude", "codex"]
    assert always["agents"]["claude"]["skills"] == ["sandbox-ai"] and not always["editable"]
    assert default["path"] == str(data() / "home") and list(default["agents"]) == ["claude"]
    assert default["description"] == "Default kit plugins" and not default["editable"]
    assert egm["replaced"] == ["claude"] and egm["description"].startswith("Built-in kit 'egm'")
    assert egm["agents"]["claude"]["plugins"] == ["egm@sandbox-ai"] and not egm["editable"]
    assert egm["agents"]["claude"]["marketplaces"] == {"sandbox-ai": SANDBOX_AI}
    assert own_egm["editable"] and own_egm["replaced"] == [] and own_egm["agents"]["claude"]["skills"] == ["mine"]
    assert review["description"] == "Review kit" and review["editable"] and list(review["agents"]) == list(AGENTS)
    claude = review["agents"]["claude"]
    assert claude["skills"] == ["check"] and claude["agents"] == ["critic.md"] and claude["memory"] == "CLAUDE.md"
    assert claude["plugins"] == ["sandcastle@sandbox-ai", "x@elsewhere"] and claude["error"] is None
    assert claude["marketplaces"] == {"sandbox-ai": SANDBOX_AI}
    assert any("does not declare the marketplace elsewhere" in note for note in claude["notes"])
    assert review["agents"]["codex"]["prompts"] == ["fix.md"] and review["agents"]["codex"]["memory"] is None
    assert review["agents"]["opencode"]["plugins"] == ["oc-plugin@1.2.3"]
    market = inventory["marketplace"]
    assert market["name"] == "sandbox-ai" and market["target"] == "/sandbox/marketplace"
    assert market["path"] == str(tmp_path / "marketplace")
    assert all(set(plugin) == PLUGIN_KEYS for plugin in market["plugins"])
    assert market["plugins"][0] == {"name": "sandcastle", "id": "sandcastle@sandbox-ai", "description": "Demo",
                                    "version": "1.0.0", "source": "local", "homepage": None, "kits": ["review"]}
    assert market["plugins"][1]["source"] == "github:google/elephant-goldfish-model"
    assert market["plugins"][1]["kits"] == ["egm"] and market["plugins"][1]["homepage"] == "https://egm.dev"
    assert inventory["installed"] == {"marketplaces": [], "plugins": []}
    # One agent: only that part, only kits that have one.
    codex = kc.inventory("codex")
    assert [kit["name"] for kit in codex["kits"]] == ["built-in", "review"]
    assert all(list(kit["agents"]) == ["codex"] for kit in codex["kits"])
    assert json.loads(run("--json", "--agent", "codex")[1]) == codex


def test_show_resolves_your_kit_then_built_in_then_default(shipped, tmp_path):
    user_kits(tmp_path)
    inventory = kc.inventory()
    status, text = run("show", "review", "--json")
    assert status == 0 and json.loads(text) == inventory["kits"][4]
    assert json.loads(run("show", "egm", "--json")[1])["kind"] == "user"
    assert json.loads(run("show", "default", "--json")[1])["kind"] == "default"
    assert json.loads(run("show", "built-in", "--json")[1])["kind"] == "always"
    (kits_dir() / "egm/claude/skills/mine/SKILL.md").unlink()
    for path in ((kits_dir() / "egm/claude/skills/mine"), kits_dir() / "egm/claude/skills", kits_dir() / "egm/claude",
                 kits_dir() / "egm"):
        path.rmdir()
    assert json.loads(run("show", "egm", "--json")[1])["kind"] == "builtin"
    # A kit of yours named default wins over the default kit.
    make(kits_dir(), {"default/codex/prompts/p.md": "p"})
    assert json.loads(run("show", "default", "--json")[1])["kind"] == "user"
    status, text = run("show", "review")
    assert status == 0 and "review: your kit" in text and "use: sandbox-ai claude --kit review" in text
    assert "note: plugin x@elsewhere: this kit does not declare" in text and "claude path:" in text
    assert run("show", "nope")[0] == 1
    assert run("show", "../x")[0] == 1


def test_list_text_names_how_to_use_and_install(shipped, tmp_path, monkeypatch):
    user_kits(tmp_path)
    status, text = run("list")
    assert status == 0
    assert "review: your kit" in text and "use: sandbox-ai claude --kit review" in text
    assert "plugins: sandcastle@sandbox-ai (not installed), x@elsewhere (not installed)" in text
    assert "install its plugins: sandbox-ai plugins claude" in text
    assert "egm: built-in kit of sandbox-ai, read-only" in text and "replaced by your kit egm for: claude" in text
    assert "replaces sandbox-ai's built-in kit egm for: claude" in text
    assert "opencode  npm plugins: oc-plugin@1.2.3" in text and "1 note(s): sandbox-ai kits show review" in text
    assert "egm@sandbox-ai (github:google/elephant-goldfish-model; listed by egm, not installed)" in text
    assert run()[1] == text  # `sandbox-ai kits` alone lists
    # A built-in kit nobody replaced: its plugins are installed with --kit NAME where `plugins` knows it.
    import shutil
    shutil.rmtree(kits_dir() / "egm")
    monkeypatch.setattr(kc, "plugins_take_kit_option", lambda: True)
    assert "install its plugins: sandbox-ai plugins claude --kit egm" in run("list")[1]
    monkeypatch.setattr(kc, "plugins_take_kit_option", lambda: False)
    assert "does not install a built-in kit's plugins" in run("list")[1]
    # Installed plugins are no longer marked.
    make(data(), {"plugins/claude/.claude/plugins/known_marketplaces.json": json.dumps({"sandbox-ai": {}}),
                  "plugins/claude/.claude/plugins/installed_plugins.json":
                      json.dumps({"plugins": {"sandcastle@sandbox-ai": [{}], "egm@sandbox-ai": [{}]}})})
    text = run("list")[1]
    assert "sandcastle@sandbox-ai, x@elsewhere (not installed)" in text and "plugins claude --kit egm" not in text
    assert kc.inventory()["installed"] == {"marketplaces": ["sandbox-ai"],
                                           "plugins": ["egm@sandbox-ai", "sandcastle@sandbox-ai"]}


def test_damaged_declaration_is_reported_per_agent(shipped, tmp_path):
    make(kits_dir(), {"bad/claude/marketplaces.json": "{not json",
                      "bad/codex/skills/x/SKILL.md": "x",
                      "odd/claude/marketplaces.json": json.dumps({"plugins": ["no-marketplace"]}),
                      "odd/opencode/skills": "a file, not a directory",
                      "ok/claude/skills/y/SKILL.md": "y"})
    status, text = run("list", "--json")
    assert status == 0
    kits = {kit["name"]: kit for kit in json.loads(text)["kits"] if kit["kind"] == "user"}
    assert "must hold a JSON object" in kits["bad"]["agents"]["claude"]["error"]
    assert kits["bad"]["agents"]["codex"] == dict(kits["bad"]["agents"]["codex"], error=None, skills=["x"])
    assert "must be NAME@MARKETPLACE" in kits["odd"]["agents"]["claude"]["error"]
    assert "must be a directory" in kits["odd"]["agents"]["opencode"]["error"]
    assert kits["ok"]["agents"]["claude"]["skills"] == ["y"]
    status, text = run("list")
    assert status == 0 and "error: kit bad:" in text and "ok: your kit" in text


def test_real_built_in_kit_egm_is_listed():
    """After the merge with the built-in kits, the real kit egm and its marketplace entry show up."""
    if not (ROOT / "sandbox/builtin/kits/egm").is_dir():
        pytest.skip("this sandbox-ai has no built-in kit egm")
    inventory = kc.inventory()
    egm = [kit for kit in inventory["kits"] if kit["name"] == "egm" and kit["kind"] == "builtin"]
    assert egm and egm[0]["agents"]["claude"]["plugins"] == ["egm@sandbox-ai"] and egm[0]["agents"]["claude"]["error"] is None
    entry = [plugin for plugin in inventory["marketplace"]["plugins"] if plugin["id"] == "egm@sandbox-ai"]
    assert entry and entry[0]["kits"] == ["egm"] and entry[0]["source"].startswith("github:")


# --- new, delete -----------------------------------------------------------------------------------

def test_new_creates_the_missing_agent_directories(shipped):
    status, text = run("new", "review")
    assert status == 0 and "use it with: sandbox-ai claude --kit review" in text
    assert all((kits_dir() / "review" / agent / "skills").is_dir() for agent in AGENTS)
    assert stat.S_IMODE(kits_dir().stat().st_mode) == 0o700
    assert run("new", "review")[0] == 1
    (kits_dir() / "review/codex/skills").rmdir()
    (kits_dir() / "review/codex").rmdir()
    status, text = run("new", "review")
    assert status == 0 and "created codex/skills" in text and "(claude, opencode existed)" in text
    status, text = run("new", "solo", "--agent", "opencode")
    assert status == 0 and os.listdir(kits_dir() / "solo") == ["opencode"]
    status, text = run("new", "egm")
    assert status == 0 and "replaces sandbox-ai's built-in kit egm for claude" in text
    for name in ("-x", "a/b", ".hidden", "default", "built-in"):
        assert run("new", "--", name)[0] == 1
    assert run("new", "-x")[0] == 2 and run("new", "x", "--agent", "gemini")[0] == 2


def test_kits_dir_may_be_a_symlink_but_nothing_below_it(tmp_path, capsys):
    real = tmp_path / "real-kits"
    real.mkdir()
    data().mkdir(parents=True)
    kits_dir().symlink_to(real)
    assert run("new", "review")[0] == 0 and (real / "review/claude/skills").is_dir()
    outside = make(tmp_path / "outside", {"claude/skills/keep/SKILL.md": "keep", "codex/x": "x"})
    (kits_dir() / "linked").symlink_to(outside)
    before = tree(outside)
    source = skill(tmp_path / "src")
    for argv in (("new", "linked"), ("add-skill", "linked", str(source)), ("new-skill", "linked", "x"),
                 ("add-plugin", "linked", "sandcastle@sandbox-ai"), ("remove-skill", "linked", "keep")):
        assert run(*argv)[0] == 1
        assert "change it where it lives" in capsys.readouterr().err
    # An agent directory or skills directory that is a symlink: the same.
    (real / "half").mkdir()
    (real / "half/claude").symlink_to(outside / "claude")
    (real / "deep/codex").mkdir(parents=True)
    (real / "deep/codex/skills").symlink_to(outside / "claude/skills")
    for argv in (("add-skill", "half", str(source), "--agent", "claude"), ("new-skill", "half", "x"),
                 ("add-plugin", "half", "sandcastle@sandbox-ai"), ("add-skill", "deep", str(source)),
                 ("new-skill", "deep", "x"), ("remove-skill", "deep", "keep"),
                 ("add-marketplace", "half", "m", "o/r")):
        assert run(*argv)[0] == 1
        assert "is a symlink" in capsys.readouterr().err
    assert tree(outside) == before
    assert json.loads(run("show", "linked", "--json")[1])["editable"] is False


def test_delete_needs_yes_and_unlinks_a_symlinked_kit(shipped, tmp_path, capsys):
    run("new", "review")
    assert run("delete", "review")[0] == 1 and "repeat with --yes" in capsys.readouterr().err
    assert (kits_dir() / "review").is_dir()
    assert run("delete", "review", "--yes")[0] == 0 and not (kits_dir() / "review").exists()
    outside = make(tmp_path / "outside", {"claude/skills/keep/SKILL.md": "keep"})
    (kits_dir() / "linked").symlink_to(outside)
    assert run("delete", "linked")[0] == 1
    status, text = run("delete", "linked", "--yes")
    assert status == 0 and "its target is untouched" in text
    assert not os.path.lexists(kits_dir() / "linked") and (outside / "claude/skills/keep/SKILL.md").is_file()
    assert run("delete", "review", "--yes")[0] == 1 and "no kit review" in capsys.readouterr().err


def test_built_in_default_and_always_on_kits_are_read_only(shipped, tmp_path, capsys):
    make(data(), {"home/claude/skills/sandbox-example/SKILL.md": "x"})
    source = skill(tmp_path / "src")
    before = tree(shipped), tree(data())
    plugin_commands = (("add-plugin", "sandcastle@sandbox-ai"), ("remove-plugin", "egm@sandbox-ai"),
                       ("enable-plugin", "egm@sandbox-ai"), ("disable-plugin", "egm@sandbox-ai"),
                       ("add-marketplace", "m", "o/r"), ("remove-marketplace", "m"), ("import-marketplace", "m"))
    for name, reason in (("egm", "built-in kit of sandbox-ai"), ("default", "edit it by hand in"),
                         ("built-in", "ships with sandbox-ai and is read-only")):
        for argv in (("add-skill", name, str(source)), ("new-skill", name, "x"), ("remove-skill", name, "sandbox-ai"),
                     ("delete", name, "--yes"),
                     # the default kit's plugins and marketplaces change (test_default_kit_plugins_and_marketplaces)
                     *([] if name == "default" else [(command, name, *rest) for command, *rest in plugin_commands])):
            assert run(*argv)[0] == 1
            assert reason in capsys.readouterr().err
    assert run("add-skill", "default", str(source))[0] == 1
    assert str(data() / "home") in capsys.readouterr().err
    assert (tree(shipped), tree(data())) == before


# --- skills ----------------------------------------------------------------------------------------

def test_add_skill_copies_a_snapshot(shipped, tmp_path):
    run("new", "review", "--agent", "claude", "--agent", "codex")
    source = skill(tmp_path / "src", "demo", {"scripts/run.sh": "#!/bin/sh\necho hi\n", "refs/a/b.md": "b"})
    (source / "scripts/run.sh").chmod(0o750)
    (source / "refs/a/b.md").chmod(0o600)
    status, text = run("add-skill", "review", str(source) + "/")
    assert status == 0 and "skill demo copied for claude, codex" in text and "3 file(s)" in text
    for agent in ("claude", "codex"):
        copied = tree(kits_dir() / "review" / agent / "skills")
        assert copied["demo/scripts/run.sh"] == (0o755, b"#!/bin/sh\necho hi\n")
        assert copied["demo/refs/a/b.md"] == (0o644, b"b")
        assert copied["demo/SKILL.md"][0] == 0o644 and copied["demo"][0] == 0o755
        assert not any(name.startswith(".") for name in copied)
    assert not (kits_dir() / "review/opencode").exists()  # default: the agent directories the kit has
    # The launcher takes it like any kit entry.
    plan = k.scan("claude", [("kit review", kits_dir() / "review/claude")])
    assert sorted(plan["entries"]["skills"]) == ["demo"] and plan["notes"] == []
    status, text = run("add-skill", "review", str(source), "--as", "other", "--agent", "opencode")
    assert status == 0 and (kits_dir() / "review/opencode/skills/other/SKILL.md").is_file()
    assert run("add-skill", "review", str(source))[0] == 1  # demo exists
    assert run("add-skill", "review", str(source), "--as=-bad")[0] == 1
    assert run("add-skill", "nope", str(source))[0] == 1


def test_add_skill_never_follows_a_symlink_or_opens_a_fifo(shipped, tmp_path, capsys):
    """Through main.py in a subprocess: a FIFO that were opened would block it (the timeout catches that)."""
    run("new", "review", "--agent", "claude")
    secret = make(tmp_path / "private", {"secret.txt": "TOP-SECRET", "dir/key": "TOP-SECRET"})
    source = skill(tmp_path / "src", "demo", {"ok.md": "fine"})
    (source / "leak.txt").symlink_to(secret / "secret.txt")
    (source / "leakdir").symlink_to(secret / "dir")
    (source / "SKILL-link.md").symlink_to(source / "SKILL.md")
    os.mkfifo(source / "pipe")
    result = subprocess.run([sys.executable, "-I", str(ROOT / "sandbox/main.py"), "kits", "add-skill", "review",
                             str(source)], capture_output=True, text=True, timeout=60, env=dict(os.environ))
    assert result.returncode == 0, result.stderr
    assert "leak.txt skipped: a symlink (not followed)" in result.stdout and "pipe skipped" in result.stdout
    copied = tree(kits_dir() / "review/claude/skills/demo")
    assert sorted(copied) == ["SKILL.md", "ok.md"]
    assert all(b"TOP-SECRET" not in (content or b"") for _, content in tree(kits_dir()).values())
    # PATH itself: the folder, not a link to it; with a regular SKILL.md at its top.
    (tmp_path / "link").symlink_to(source)
    for value in (tmp_path / "link", f"{tmp_path / 'link'}/"):
        assert run("add-skill", "review", str(value), "--as", "x")[0] == 1
        assert "is a symlink; give the folder itself" in capsys.readouterr().err
    make(tmp_path / "nomd", {"README.md": "x"})
    assert run("add-skill", "review", str(tmp_path / "nomd"))[0] == 1
    assert "has no SKILL.md at its top" in capsys.readouterr().err
    linked_md = make(tmp_path / "linkedmd", {"real.md": "x"})
    (linked_md / "SKILL.md").symlink_to(linked_md / "real.md")
    assert run("add-skill", "review", str(linked_md))[0] == 1
    assert run("add-skill", "review", str(tmp_path / "missing"))[0] == 1
    assert run("add-skill", "review", str(source / "ok.md"))[0] == 1
    assert sorted(os.listdir(kits_dir() / "review/claude/skills")) == ["demo"]


@pytest.mark.parametrize("limit, value, message", [
    ("KIT_FILES", 3, "more than 3 files"), ("KIT_BYTES", 10, "more than 0 MiB"),
    ("KIT_DEPTH", 3, "nested deeper than 3")])
def test_add_skill_limits_leave_nothing_behind(shipped, tmp_path, monkeypatch, capsys, limit, value, message):
    run("new", "review", "--agent", "claude", "--agent", "codex")
    source = skill(tmp_path / "src", "demo", {f"f{i}.md": "0123456789" for i in range(4)})
    make(source, {"a/b/c/d.md": "x"})
    monkeypatch.setattr(k, limit, value)
    assert run("add-skill", "review", str(source))[0] == 1
    assert message in capsys.readouterr().err
    for agent in ("claude", "codex"):
        assert os.listdir(kits_dir() / "review" / agent / "skills") == []


def test_add_skill_rolls_back_the_agents_already_done(shipped, tmp_path, monkeypatch):
    run("new", "review", "--agent", "claude", "--agent", "codex")
    source = skill(tmp_path / "src")
    real = kc.copy_skill

    def failing(root, name, agent, *rest):
        if agent == "codex":
            raise ValueError("disk full")
        return real(root, name, agent, *rest)
    monkeypatch.setattr(kc, "copy_skill", failing)
    assert run("add-skill", "review", str(source))[0] == 1
    assert os.listdir(kits_dir() / "review/claude/skills") == []


def test_add_skill_from_an_agent_writable_folder_notes_the_snapshot(shipped, tmp_path):
    run("new", "review", "--agent", "claude")
    project = tmp_path / "project"
    skill(project / "skills")
    state = Path(os.environ["HOME"]) / ".local/state/sandbox-ai"
    make(state, {"exposed.json": json.dumps([str(project)])})
    status, text = run("add-skill", "review", str(project / "skills/demo"))
    assert status == 0 and "a folder sandboxes could write in an earlier run" in text and "review it" in text
    # The workspace of a running sandbox, also when the skill folder holds it.
    work = tmp_path / "work"
    skill(work, "other")
    runs = kc.module("net").launcher().runs_dir() / "claude-1"
    make(runs, {"run.json": json.dumps({"run": "claude-1", "pid": os.getpid(), "workspace": str(work / "other")})})
    status, text = run("add-skill", "review", str(work / "other"))
    assert status == 0 and "the workspace of the running sandbox claude-1" in text
    quiet = skill(tmp_path / "quiet", "calm")
    assert "agent may have written" not in run("add-skill", "review", str(quiet))[1]


def test_new_skill_writes_a_valid_template(shipped, capsys):
    run("new", "review")
    status, text = run("new-skill", "review", "lint-fix", "--description", 'Fixes "lint" – ü; use after edits')
    assert status == 0 and text.count("wrote") == 3
    content = (kits_dir() / "review/claude/skills/lint-fix/SKILL.md").read_text()
    front = content.split("---\n")[1].splitlines()
    assert front[0] == "name: lint-fix"
    assert json.loads(front[1].split("description: ", 1)[1]) == 'Fixes "lint" – ü; use after edits'
    assert "# lint-fix" in content and "Write here what the agent" in content
    assert k.scan("codex", [("kit review", kits_dir() / "review/codex")])["notes"] == []
    status, text = run("new-skill", "review", "plain", "--agent", "claude")
    assert status == 0 and "replace the TODO description" in text
    assert "TODO" in (kits_dir() / "review/claude/skills/plain/SKILL.md").read_text()
    assert not (kits_dir() / "review/codex/skills/plain").exists()
    for name in ("Bad", "-x", "a_b", "a.b", "x" * 65, ""):
        assert run("new-skill", "review", "--", name)[0] == 1
    for description in ("two\nlines", "esc\x1b[2J", "sep x", "bidi‮x", "   ", "x" * 1025):
        assert run("new-skill", "review", "ok", "--description", description)[0] == 1
    assert run("new-skill", "review", "lint-fix")[0] == 1 and "already has the skill" in capsys.readouterr().err
    assert run("new-skill", "review", "ok", "--description", "x" * 1024)[0] == 0


def test_remove_skill(shipped, tmp_path, capsys):
    run("new", "review")
    run("new-skill", "review", "a")
    run("new-skill", "review", "b", "--agent", "codex")
    status, text = run("remove-skill", "review", "a", "--agent", "codex")
    assert status == 0 and not (kits_dir() / "review/codex/skills/a").exists()
    assert (kits_dir() / "review/claude/skills/a").is_dir()
    status, text = run("remove-skill", "review", "a")
    assert status == 0 and text.count("removed") == 2
    assert run("remove-skill", "review", "a")[0] == 1
    assert run("remove-skill", "review", "b", "--agent", "claude")[0] == 1
    assert run("remove-skill", "review", "..")[0] == 1
    # A skill that is a symlink (to a checkout): only the link goes.
    checkout = skill(tmp_path / "checkout", "linked")
    (kits_dir() / "review/claude/skills/linked").symlink_to(checkout)
    status, text = run("remove-skill", "review", "linked")
    assert status == 0 and "only the link" in text and (checkout / "SKILL.md").is_file()


# --- plugins and marketplaces ----------------------------------------------------------------------

def test_add_plugin_claude(shipped, tmp_path, capsys):
    run("new", "review")
    make(kits_dir(), {"review/claude/marketplaces.json": json.dumps({
        "$comment": "mine", "marketplaces": {"team": {"source": {"source": "git", "url": "https://git.example.org/m"}}},
        "plugins": ["tool@team"]})})
    status, text = run("add-plugin", "review", "sandcastle@sandbox-ai")
    assert status == 0 and "declared the marketplace sandbox-ai" in text and "sandbox-ai plugins claude" in text
    document = declaration("review")
    assert list(document) == ["$comment", "marketplaces", "plugins"] and document["$comment"] == "mine"
    assert document["marketplaces"] == {"team": {"source": {"source": "git", "url": "https://git.example.org/m"}},
                                        "sandbox-ai": {"source": SANDBOX_AI}}
    assert document["plugins"] == ["tool@team", "sandcastle@sandbox-ai"]
    path = kits_dir() / "review/claude/marketplaces.json"
    assert k.declarations("claude", [("kit review", path)])[1] == ["tool@team", "sandcastle@sandbox-ai"]
    status, text = run("add-plugin", "review", "sandcastle@sandbox-ai")
    assert status == 0 and "nothing changed" in text
    assert run("add-plugin", "review", "egm@sandbox-ai")[0] == 0
    assert run("add-plugin", "review", "nope@sandbox-ai")[0] == 1
    assert "has no plugin nope; it has: sandcastle, egm" in capsys.readouterr().err
    # A marketplace the kit does not declare: refused, with the source another kit uses.
    make(data(), {"home/claude/marketplaces.json": json.dumps({"marketplaces": {
        "claude-plugins-official": {"source": {"source": "github", "repo": "anthropics/claude-plugins-official"}}}})})
    assert run("add-plugin", "review", "x@claude-plugins-official")[0] == 1
    assert "--source anthropics/claude-plugins-official" in capsys.readouterr().err
    status, text = run("add-plugin", "review", "x@claude-plugins-official", "--source", "anthropics/claude-plugins-official")
    assert status == 0
    assert declaration("review")["marketplaces"]["claude-plugins-official"] == {
        "source": {"source": "github", "repo": "anthropics/claude-plugins-official"}}
    assert run("add-plugin", "review", "y@team", "--source", "other/repo")[0] == 1
    assert "with another source" in capsys.readouterr().err
    assert run("add-plugin", "review", "z@team")[0] == 0
    for bad in ("tool", "a@b@c", "-x@team"):
        assert run("add-plugin", "review", "--", bad)[0] == 1
    assert run("add-plugin", "review", "codex-thing", "--agent", "codex")[0] == 1
    assert "not supported yet" in capsys.readouterr().err


def test_add_plugin_opencode_and_damaged_files(shipped, tmp_path, capsys):
    run("new", "review")
    status, text = run("add-plugin", "review", "@scope/oc-plugin@1.2.3", "--agent", "opencode")
    assert status == 0 and "registry.npmjs.org" in text
    assert declaration("review", "opencode") == {"plugins": ["@scope/oc-plugin@1.2.3"]}
    assert run("add-plugin", "review", "Bad Name", "--agent", "opencode")[0] == 1
    assert run("add-plugin", "review", "oc", "--agent", "opencode", "--source", "o/r")[0] == 1
    path = kits_dir() / "review/claude/marketplaces.json"
    for damaged in ("{not json", json.dumps({"plugins": "x"}), json.dumps({"extra": 1})):
        path.write_text(damaged)
        assert run("add-plugin", "review", "sandcastle@sandbox-ai")[0] == 1
        assert "by hand" in capsys.readouterr().err and path.read_text() == damaged
    path.unlink()
    target = make(tmp_path / "elsewhere", {"m.json": "{}"})
    path.symlink_to(target / "m.json")
    assert run("add-plugin", "review", "sandcastle@sandbox-ai")[0] == 1
    assert "symlink" in capsys.readouterr().err and (target / "m.json").read_text() == "{}"


@pytest.mark.parametrize("value, source", [
    ("owner/repo", {"source": "github", "repo": "owner/repo"}),
    ("github:owner/repo", {"source": "github", "repo": "owner/repo"}),
    ("https://git.example.org/team/plugins.git", {"source": "git", "url": "https://git.example.org/team/plugins.git"}),
    ("https://example.org/x/marketplace.json", {"source": "url", "url": "https://example.org/x/marketplace.json"}),
])
def test_add_marketplace_sources(shipped, value, source):
    run("new", "review")
    status, text = run("add-marketplace", "review", "team", value)
    assert status == 0 and "add-plugin review PLUGIN@team" in text
    assert declaration("review")["marketplaces"]["team"] == {"source": source}
    assert run("add-marketplace", "review", "team", value) == (0, "kit review already declares the marketplace team "
                                                                  "with that source; nothing changed")


def test_add_marketplace_refusals(shipped, tmp_path, capsys):
    run("new", "review")
    for value in ("sandbox-ai", "/sandbox/marketplace"):
        assert run("add-marketplace", "review", "sandbox-ai", value)[0] == 0
    assert declaration("review")["marketplaces"] == {"sandbox-ai": {"source": SANDBOX_AI}}
    assert run("add-marketplace", "review", "other", "sandbox-ai")[0] == 1
    folder = make(tmp_path / "local-market", {".claude-plugin/marketplace.json": "{}"})
    os.chdir(tmp_path)
    for value in (str(folder), "./local-market", "local-market", "~/x", "http://example.org/m.git",
                  "git@github.com:o/r.git", "file:///tmp/m"):
        assert run("add-marketplace", "review", "local", value)[0] == 1
        error = capsys.readouterr().err
        assert "only https://" in error or "publish others on a Git server or GitHub" in error
    assert run("add-marketplace", "review", "sandbox-ai", "o/r")[0] == 1
    assert "with another source" in capsys.readouterr().err
    assert run("add-marketplace", "review", "--", "-bad", "o/r")[0] == 1
    assert run("add-marketplace", "review", "m", "o/..")[0] == 1
    assert run("add-marketplace", "review", "m", "o/r", "--agent", "opencode")[0] == 1
    assert "only Claude has plugin marketplaces" in capsys.readouterr().err
    assert list(declaration("review")["marketplaces"]) == ["sandbox-ai"]


def test_remove_plugin(shipped, capsys):
    run("new", "review")
    run("add-plugin", "review", "sandcastle@sandbox-ai")
    run("add-plugin", "review", "oc@1.0.0", "--agent", "opencode")
    status, text = run("remove-plugin", "review", "sandcastle@sandbox-ai")
    assert status == 0 and "its marketplace stays declared" in text
    assert declaration("review") == {"marketplaces": {"sandbox-ai": {"source": SANDBOX_AI}}, "plugins": []}
    assert run("remove-plugin", "review", "sandcastle@sandbox-ai")[0] == 1
    assert run("remove-plugin", "review", "oc@1.0.0", "--agent", "claude")[0] == 1
    # A damaged file of another agent does not stand in the way.
    (kits_dir() / "review/codex/marketplaces.json").write_text("{damaged")
    assert run("remove-plugin", "review", "oc@1.0.0")[0] == 0
    assert declaration("review", "opencode") == {"plugins": []}
    assert run("remove-plugin", "review", "oc@1.0.0", "--agent", "codex")[0] == 1
    assert "must hold a JSON object" in capsys.readouterr().err


# --- the command line ------------------------------------------------------------------------------

def test_main_py_runs_the_command(tmp_path):
    env = dict(os.environ)

    def sandbox_ai(*argv):
        return subprocess.run([sys.executable, "-I", str(ROOT / "sandbox/main.py"), "kits", *argv],
                              capture_output=True, text=True, timeout=60, env=env)
    result = sandbox_ai("new", "review", "--agent", "claude")
    assert result.returncode == 0 and (kits_dir() / "review/claude/skills").is_dir()
    result = sandbox_ai("list", "--json")
    assert result.returncode == 0
    inventory = json.loads(result.stdout)
    assert inventory["kits_dir"] == str(kits_dir()) and ("review", "user") in [(x["name"], x["kind"]) for x in inventory["kits"]]
    assert sandbox_ai().returncode == 0 and "review: your kit" in sandbox_ai().stdout
    result = sandbox_ai("show")
    assert result.returncode == 2 and "usage: sandbox-ai kits show" in result.stderr
    result = sandbox_ai("show", "nope")
    assert result.returncode == 1 and result.stderr.startswith("sandbox-ai kits: no kit nope")
    assert "see and change kits" in subprocess.run([sys.executable, "-I", str(ROOT / "sandbox/main.py"), "--help"],
                                                    capture_output=True, text=True, timeout=60).stdout
