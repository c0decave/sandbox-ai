"""sandbox-ai kits host-list / host-show / host-import (sandbox/hostkits.py, sandbox/kitctl.py): what the agents on
this machine (or on a host of `sandbox-ai hosts`) have set up themselves, shown read-only, and the parts chosen copied
into a kit of yours or into a project's memory.

Every test runs on a temporary HOME holding realistic agent folders, never this machine's HOME. The permanent
regression tests: a symlink out of the agent's folder is never followed (listed as refused, refused on import); hooks
are shown, never imported, and what an import brings that runs code (a plugin module, a skill's own hooks) is never
trusted by it; secret-looking lines are flagged, masked in the preview and refused unless asked for; nothing is
imported without an explicit selection, and listing changes nothing.
"""
import base64
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


kc = load("kitctl_for_host_tests", "sandbox/kitctl.py")
k = kc.kits()
hk = kc.module("hostkits")
# Assembled at run time, so that no scanner takes the fixture for a real token.
TOKEN = "gh" + "p_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"


def make(root, files):
    for relative, text in files.items():
        path = Path(root) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            path.write_bytes(text)
        else:
            path.write_text(text)
    return Path(root)


def agent_home(home, outside):
    """Realistic agent folders in HOME (and a folder OUTSIDE every agent's folder, the target of an escaping link)."""
    make(outside, {"stolen/SKILL.md": "---\nname: stolen\n---\nsecret notes\n", "notes.md": "private\n"})
    make(home, {
        # Claude Code
        ".claude/skills/pdf/SKILL.md": "---\nname: pdf\ndescription: Fill PDF forms\n---\nRun scripts/fill.py\n",
        ".claude/skills/pdf/scripts/fill.py": "#!/usr/bin/env python3\nprint('fill')\n",
        ".claude/skills/pdf/reference.md": "# Reference\n",
        ".claude/skills/hooked/SKILL.md": ("---\nname: hooked\ndescription: A skill with hooks\nhooks:\n  PreToolUse:\n"
                                           "    - matcher: Bash\n      hooks:\n        - type: command\n"
                                           "          command: ./check.sh\n---\nBody\n"),
        ".claude/skills-real/inside/SKILL.md": "---\nname: inside\ndescription: Linked inside\n---\nok\n",
        ".claude/commands/deploy.md": "---\ndescription: Deploy\nallowed-tools: Bash(git:*)\n---\n!`git status`\nDeploy $ARGUMENTS\n",
        ".claude/commands/frontend/component.md": "---\ndescription: A component\nargument-hint: NAME\n---\nMake $ARGUMENTS\n",
        ".claude/agents/reviewer.md": "---\nname: reviewer\ndescription: Reviews code\ntools: Read, Grep\n---\nReview.\n",
        ".claude/agents/helper.md": "---\nname: helper\ndescription: Helps\nmodel: sonnet\n---\nHelp.\n",
        ".claude/output-styles/terse.md": "---\nname: Terse\ndescription: Short\n---\nBe short.\n",
        ".claude/CLAUDE.md": f"# My notes\nUse tabs.\ntoken {TOKEN}\n",
        ".claude/settings.json": json.dumps({"hooks": {"PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "/home/me/bin/guard.sh"}]}]},
            "model": "opus"}),
        ".claude/plugins/known_marketplaces.json": json.dumps({"tools": {"source": {"source": "github",
                                                                                    "repo": "acme/tools"}}}),
        ".claude/plugins/installed_plugins.json": json.dumps({"version": 2, "plugins": {"lint@tools": [{}]}}),
        ".claude/projects/-home-me-proj/memory/MEMORY.md": ("- [Feedback](feedback.md) — how I like reviews\n"
                                                           "- [Role](user_role.md) — who I am\n"),
        ".claude/projects/-home-me-proj/memory/feedback.md": ("---\nname: feedback\ndescription: how I like reviews\n"
                                                             "---\nShort reviews.\n"),
        ".claude/projects/-home-me-proj/memory/user_role.md": "---\nname: role\n---\nBackend developer.\n",
        ".claude.json": json.dumps({"mcpServers": {"files": {"command": "npx", "args": ["files-mcp"],
                                                             "env": {"API_KEY": "hidden-value"}}}}),
        # Codex
        ".codex/prompts/fix.md": "---\ndescription: Fix a bug\nargument-hint: ISSUE\n---\nFix $1 now\n",
        ".codex/AGENTS.md": "Codex notes\n",
        ".codex/config.toml": '[mcp_servers.docs]\ncommand = "docs-mcp"\nargs = ["--port", "1"]\n',
        ".agents/skills/shared-skill/SKILL.md": "---\nname: shared-skill\ndescription: Shared\n---\nshared\n",
        # OpenCode (1 and 2)
        ".config/opencode/agents/plan.md": "---\ndescription: Plans\nmode: subagent\n---\nPlan.\n",
        ".config/opencode/commands/test.md": "---\ndescription: Run tests\nagent: build\n---\nRun the tests $ARGUMENTS\n",
        ".config/opencode/skills/oc-skill/SKILL.md": "---\nname: oc-skill\ndescription: OC\n---\noc\n",
        ".config/opencode/plugins/notify.js": "export const Notify = async () => ({})\n",
        ".config/opencode/AGENTS.md": "OpenCode notes\n",
        ".config/opencode/opencode.json": json.dumps({"mcp": {"web": {"type": "remote", "url": "https://x.example"}}}),
        # Qwen Code, Gemini CLI
        ".qwen/QWEN.md": "Qwen notes\n",
        ".qwen/commands/review.toml": 'description = "Review"\nprompt = "Review {{args}} carefully"\n',
        ".qwen/agents/qa.md": "---\nname: qa\ndescription: QA\n---\nTest it.\n",
        ".qwen/skills/q-skill/SKILL.md": "---\nname: q-skill\ndescription: Q\n---\nq\n",
        ".gemini/GEMINI.md": "Gemini notes\n",
        ".gemini/commands/explain.toml": 'description = "Explain"\nprompt = """Explain {{args}}"""\n',
        ".gemini/commands/shell.toml": 'description = "Shell"\nprompt = "Run !{ls} for {{args}}"\n',
        # Hermes (skills in category folders), Pi
        ".hermes/skills/devops/deploy-k8s/SKILL.md": "---\nname: deploy-k8s\ndescription: K8s\n---\nk8s\n",
        ".hermes/memories/MEMORY.md": "Hermes memory\n",
        ".hermes/memories/USER.md": "The user likes tea\n",
        ".pi/agent/prompts/pr.md": "---\ndescription: Open a PR\n---\nOpen a PR for $@\n",
        ".pi/agent/skills/pi-skill/SKILL.md": "---\nname: pi-skill\ndescription: Pi\n---\npi\n",
        ".pi/agent/AGENTS.md": "Pi notes\n",
    })
    os.chmod(home / ".claude/skills/pdf/scripts/fill.py", 0o755)
    # A link out of the agent's folder (never followed), a link inside it (followed), a link inside a skill (skipped).
    os.symlink(outside / "stolen", home / ".claude/skills/evil")
    os.symlink(home / ".claude/skills-real/inside", home / ".claude/skills/inside")
    os.symlink(outside / "notes.md", home / ".claude/skills/pdf/leak.md")
    os.symlink(outside / "notes.md", home / ".claude/commands/escape.md")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    home = tmp_path / "host"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in ("XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "CLAUDE_CONFIG_DIR",
                 "CODEX_HOME", "HERMES_HOME", "DSH_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(kc.module("net"), "USER_RUNTIME", tmp_path / "run-user", raising=False)
    agent_home(home, tmp_path / "outside")
    return home


def run(*argv):
    lines = []
    status = kc.main(list(argv), out=lines.append)
    return status, "\n".join(lines)


def listing():
    status, out = run("host-list", "--json")
    assert status == 0, out
    return json.loads(out)


def part(data, id_):
    found = [p for p in data["parts"] if p["id"] == id_]
    assert found, (id_, sorted(p["id"] for p in data["parts"]))
    return found[0]


def kit_root():
    return Path(os.environ["HOME"]) / ".local/share/sandbox-ai/kits"


# --- what is discovered ------------------------------------------------------------------------------------------

def test_lists_every_agents_parts():
    data = listing()
    ids = {p["id"] for p in data["parts"]}
    for id_ in ("claude:skill:pdf", "claude:skill:inside", "claude:command:deploy", "claude:command:frontend/component",
                "claude:agent:reviewer", "claude:output-style:terse", "claude:memory-file:CLAUDE.md",
                "claude:memory:-home-me-proj/feedback.md", "claude:memory:-home-me-proj/user_role.md",
                "claude:hooks:settings.json", "claude:mcp:files", "claude:marketplace:tools", "claude:plugin:lint@tools",
                "codex:prompt:fix", "codex:memory-file:AGENTS.md", "codex:mcp:docs", "agents:skill:shared-skill",
                "opencode:agent:plan", "opencode:command:test", "opencode:skill:oc-skill",
                "opencode:plugin-module:notify.js", "opencode:memory-file:AGENTS.md", "opencode:mcp:web",
                "qwen:memory-file:QWEN.md", "qwen:command:review", "qwen:agent:qa", "qwen:skill:q-skill",
                "gemini:memory-file:GEMINI.md", "gemini:command:explain", "hermes:skill:devops/deploy-k8s",
                "hermes:memory-file:MEMORY.md", "hermes:memory:USER.md", "pi:prompt:pr", "pi:skill:pi-skill",
                "pi:memory-file:AGENTS.md"):
        assert id_ in ids, id_
    assert "claude:memory:-home-me-proj/MEMORY.md" not in ids  # the index: kept up to date by an import
    pdf = part(data, "claude:skill:pdf")
    assert pdf["importable"] and pdf["files"] == 3 and pdf["scripts"] == ["scripts/fill.py"]
    assert pdf["description"] == "Fill PDF forms"
    assert any("leak.md" in note and "symlink" in note for note in pdf["notes"])
    assert part(data, "claude:memory-file:CLAUDE.md")["private"] is True
    assert part(data, "claude:memory:-home-me-proj/feedback.md")["private"] is True
    assert data["home"] == os.environ["HOME"] and data["host"] == "local"


def test_listing_changes_nothing(isolated):
    """Nothing is created by looking: no kit, no state, no memory store."""
    before = sorted(str(p.relative_to(isolated)) for p in isolated.rglob("*"))
    listing()
    run("host-show", "claude:skill:pdf")
    run("host-list")
    assert sorted(str(p.relative_to(isolated)) for p in isolated.rglob("*")) == before


def test_text_listing_groups_by_agent():
    status, out = run("host-list")
    assert status == 0
    assert "Claude Code" in out and "Codex" in out and "claude:skill:pdf" in out
    assert "shown only" in out and "private" in out


# --- regression: symlinks out of the agent's folder ----------------------------------------------------------------

def test_symlink_out_of_the_agent_folder_is_refused():
    data = listing()
    evil = part(data, "claude:skill:evil")
    assert not evil["importable"] and "outside" in evil["why"]
    escape = part(data, "claude:command:escape")
    assert not escape["importable"]
    run("new", "mine")
    status, out = run("host-import", "--kit", "mine", "claude:skill:evil")
    assert status == 1
    assert not (kit_root() / "mine/claude/skills/evil").exists()
    status, out = run("host-show", "claude:skill:evil")
    assert "secret notes" not in out


def test_a_link_inside_the_agent_folder_is_followed_but_links_inside_a_skill_are_not():
    run("new", "mine", "--agent", "claude")
    status, out = run("host-import", "--kit", "mine", "claude:skill:inside", "claude:skill:pdf")
    assert status == 0, out
    assert (kit_root() / "mine/claude/skills/inside/SKILL.md").read_text().startswith("---\nname: inside")
    assert not os.path.lexists(kit_root() / "mine/claude/skills/pdf/leak.md")
    fill = kit_root() / "mine/claude/skills/pdf/scripts/fill.py"
    assert fill.read_text().startswith("#!") and os.stat(fill).st_mode & 0o111
    assert "scripts/fill.py" in out


# --- regression: hooks shown, never imported, never trusted --------------------------------------------------------

def test_hooks_are_shown_never_imported(capsys):
    hooks = part(listing(), "claude:hooks:settings.json")
    assert not hooks["importable"] and any("/home/me/bin/guard.sh" in line for line in hooks["detail"])
    run("new", "mine")
    capsys.readouterr()
    status, out = run("host-import", "--kit", "mine", "claude:hooks:settings.json")
    assert status == 1 and "shown only" in capsys.readouterr().err
    assert not (kit_root() / "mine/claude/hooks").exists()


def test_imported_code_is_never_trusted(capsys):
    run("new", "mine", "--agent", "opencode", "--agent", "claude")
    status, out = run("host-import", "--kit", "mine", "opencode:plugin-module:notify.js", "claude:skill:hooked")
    assert status == 0, out
    module = kit_root() / "mine/opencode/hooks/notify.js"
    assert module.read_text().startswith("export const Notify")
    assert "trust-hooks" in out
    info = k.read_hooks("opencode", kit_root() / "mine/opencode/hooks")
    assert info["plugins"] == ["notify.js"] and not k.hooks_trusted("opencode", info["sha256"], "mine")
    assert not k.trust_file().exists()
    # A skill's own hooks (front matter) are taken out: they would run without any approval.
    skill = (kit_root() / "mine/claude/skills/hooked/SKILL.md").read_text()
    assert "hooks:" not in skill and "./check.sh" not in skill and "name: hooked" in skill
    assert "./check.sh" in (kit_root() / "mine/claude/skills/hooked/hooks.disabled.yaml").read_text()


# --- regression: secrets flagged ------------------------------------------------------------------------------------

def test_secret_lines_are_flagged_masked_and_refused_unless_asked():
    memory = part(listing(), "claude:memory-file:CLAUDE.md")
    assert memory["secrets"] == 1
    status, out = run("host-show", "claude:memory-file:CLAUDE.md", "--json")
    shown = json.loads(out)
    assert TOKEN not in out and shown["secrets"][0]["rule"] == "github-token" and shown["secrets"][0]["line"] == 3
    assert "Use tabs." in shown["text"]
    run("new", "mine", "--agent", "claude")
    status, out = run("host-import", "--kit", "mine", "claude:memory-file:CLAUDE.md")
    assert status == 1 and not (kit_root() / "mine/claude/CLAUDE.md").exists()
    status, out = run("host-import", "--kit", "mine", "claude:memory-file:CLAUDE.md", "--allow-secrets")
    assert status == 0, out
    assert "Use tabs." in (kit_root() / "mine/claude/CLAUDE.md").read_text()
    # an MCP server's environment values are never shown
    status, out = run("host-show", "claude:mcp:files", "--json")
    assert "hidden-value" not in out and "API_KEY" in out


# --- regression: nothing imported without a selection ---------------------------------------------------------------

def test_nothing_is_imported_without_a_selection(capsys):
    run("new", "mine")
    with pytest.raises(SystemExit):
        kc.main(["host-import", "--kit", "mine"], out=lambda _line: None)
    status, out = run("host-import", "--kit", "mine", "claude:skill:nope")
    assert status == 1
    assert sorted(p.name for p in (kit_root() / "mine/claude").iterdir()) == ["skills"]
    assert list((kit_root() / "mine/claude/skills").iterdir()) == []
    # one part that cannot be imported: none of the others is written either (all or nothing)
    status, out = run("host-import", "--kit", "mine", "claude:skill:pdf", "claude:skill:evil")
    assert status == 1 and not (kit_root() / "mine/claude/skills/pdf").exists()


def test_a_kit_that_does_not_exist_needs_new():
    status, _ = run("host-import", "--kit", "fresh", "claude:skill:pdf")
    assert status == 1 and not (kit_root() / "fresh").exists()
    status, out = run("host-import", "--kit", "fresh", "--new", "claude:skill:pdf")
    assert status == 0, out
    assert (kit_root() / "fresh/claude/skills/pdf/SKILL.md").is_file()


# --- conversions ---------------------------------------------------------------------------------------------------

def test_a_command_converts_where_the_formats_map():
    run("new", "mine", "--agent", "claude", "--agent", "codex", "--agent", "opencode")
    status, out = run("host-import", "--kit", "mine", "claude:command:frontend/component", "--agent", "codex",
                      "--agent", "opencode", "--agent", "claude")
    assert status == 0, out
    prompt = (kit_root() / "mine/codex/prompts/component.md").read_text()
    assert "description: A component" in prompt and "argument-hint: NAME" in prompt and "Make $ARGUMENTS" in prompt
    command = (kit_root() / "mine/opencode/commands/component.md").read_text()
    assert "description: A component" in command and "argument-hint" not in command
    assert (kit_root() / "mine/claude/commands/component.md").read_text().startswith("---\ndescription: A component")
    # a command that runs shell commands has no Codex prompt form: Claude-only there
    status, out = run("host-import", "--kit", "mine", "claude:command:deploy", "--agent", "codex")
    assert status == 1 and not (kit_root() / "mine/codex/prompts/deploy.md").exists()
    status, out = run("host-show", "claude:command:deploy", "--json")
    conversions = {c["agent"]: c for c in json.loads(out)["conversions"]}
    assert not conversions["codex"]["ok"] and "shell" in conversions["codex"]["why"]
    assert conversions["claude"]["ok"] and conversions["opencode"]["ok"]


def test_toml_commands_and_agents_convert_or_say_why_not():
    run("new", "mine", "--agent", "claude", "--agent", "opencode")
    status, out = run("host-import", "--kit", "mine", "gemini:command:explain", "--agent", "claude")
    assert status == 0, out
    assert (kit_root() / "mine/claude/commands/explain.md").read_text() == \
        "---\ndescription: Explain\n---\nExplain $ARGUMENTS\n"
    status, _ = run("host-import", "--kit", "mine", "gemini:command:shell", "--agent", "claude")
    assert status == 1
    # an agent restricted to some tools has no faithful OpenCode form (the restriction would be lost)
    status, _ = run("host-import", "--kit", "mine", "claude:agent:reviewer", "--agent", "opencode")
    assert status == 1 and not (kit_root() / "mine/opencode/agents/reviewer.md").exists()
    status, out = run("host-import", "--kit", "mine", "claude:agent:helper", "--agent", "opencode")
    assert status == 0, out
    helper = (kit_root() / "mine/opencode/agents/helper.md").read_text()
    assert "description: Helps" in helper and "mode: subagent" in helper and "model" not in helper


def test_a_part_without_a_default_agent_asks_for_one():
    run("new", "mine", "--agent", "claude")
    status, out = run("host-import", "--kit", "mine", "qwen:command:review")
    assert status == 1
    status, out = run("host-import", "--kit", "mine", "agents:skill:shared-skill")
    assert status == 0, out  # the shared folder's skills go to the kit's agents
    assert (kit_root() / "mine/claude/skills/shared-skill/SKILL.md").is_file()


def test_a_name_the_kit_has_is_refused():
    run("new", "mine", "--agent", "claude")
    assert run("host-import", "--kit", "mine", "claude:skill:pdf")[0] == 0
    status, _ = run("host-import", "--kit", "mine", "claude:skill:pdf")
    assert status == 1


# --- memory --------------------------------------------------------------------------------------------------------

def test_memory_goes_into_a_projects_memory(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    status, out = run("host-import", "--project", str(project), "claude:memory:-home-me-proj/feedback.md")
    assert status == 0, out
    store = k.memory_store(project.resolve(), "claude")
    memory = store / ".claude/projects/-work/memory"
    assert (memory / "feedback.md").read_text().endswith("Short reviews.\n")
    assert (memory / "MEMORY.md").read_text() == "- [Feedback](feedback.md) — how I like reviews\n"
    assert not (memory / "user_role.md").exists()  # only what was chosen
    # again: already there, nothing doubled
    status, out = run("host-import", "--project", str(project), "claude:memory:-home-me-proj/feedback.md")
    assert status == 0 and "already" in out
    assert (memory / "MEMORY.md").read_text().count("feedback.md") == 1
    # the project had no memory yet: its kits' memory template still seeds it at its first start
    template = make(tmp_path / "kit", {"CLAUDE.md": "From the kit\n"}) / "CLAUDE.md"
    home = tmp_path / "sandbox-home"
    home.mkdir()
    k.memory_prepare("claude", project.resolve(), home, ("kit t", template))
    assert (home / ".claude/CLAUDE.md").read_text() == "From the kit\n"
    assert (home / ".claude/projects/-work/memory/feedback.md").is_file()


def test_a_memory_file_is_appended_to_a_projects_memory_file_and_a_kits_template(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    status, out = run("host-import", "--project", str(project), "codex:memory-file:AGENTS.md")
    assert status == 0, out
    assert "Codex notes" in (k.memory_store(project.resolve(), "codex") / ".codex/AGENTS.md").read_text()
    run("new", "mine", "--agent", "hermes")
    status, out = run("host-import", "--kit", "mine", "hermes:memory:USER.md", "hermes:memory-file:MEMORY.md")
    assert status == 0, out
    text = (kit_root() / "mine/hermes/MEMORY.md").read_text()
    assert "The user likes tea" in text and "Hermes memory" in text
    assert "private" in out


# --- a host of `sandbox-ai hosts`: the same over SSH -----------------------------------------------------------------

def test_a_hosts_parts_come_over_ssh(tmp_path, monkeypatch):
    remote = tmp_path / "remote-home"
    remote.mkdir()
    make(remote, {".claude/skills/far/SKILL.md": "---\nname: far\ndescription: From afar\n---\nfar\n"})
    hosts = kc.module("hosts")
    calls = []

    def fake_run(name, args, input=None, timeout=120, **_):
        calls.append((name, args))
        env = {**os.environ, "HOME": str(remote)}
        done = subprocess.run([sys.executable, "-I", str(ROOT / "sandbox/main.py"), *args], env=env,
                              capture_output=True, text=True, timeout=60)
        return done.returncode, done.stdout, done.stderr
    monkeypatch.setattr(hosts, "run", fake_run)
    monkeypatch.setattr(hosts, "load", lambda path=None: {"box": {"ssh": "me@box", "port": 22}})
    status, out = run("host-list", "--host", "box", "--json")
    assert status == 0, out
    data = json.loads(out)
    assert data["host"] == "box" and part(data, "claude:skill:far")["importable"]
    run("new", "mine", "--agent", "claude")
    status, out = run("host-import", "--kit", "mine", "--host", "box", "claude:skill:far")
    assert status == 0, out
    assert (kit_root() / "mine/claude/skills/far/SKILL.md").read_text().endswith("far\n")
    assert calls[-1][1][:2] == ["kits", "host-export"]


def test_a_bundle_from_a_host_is_checked(monkeypatch):
    """What a host sends is data: a path out of the part, or more than asked for, is refused."""
    bad = {"version": 1, "parts": [{"id": "claude:skill:far", "agent": "claude", "kind": "skill", "name": "far",
                                    "format": "skill", "files": [
                                        {"path": "SKILL.md", "mode": 420, "data": base64.b64encode(b"x").decode()},
                                        {"path": "../../escape", "mode": 420, "data": ""}]}]}
    with pytest.raises(ValueError):
        hk.read_bundle(bad, ["claude:skill:far"])
    extra = {"version": 1, "parts": [dict(bad["parts"][0], files=bad["parts"][0]["files"][:1]),
                                     dict(bad["parts"][0], id="claude:skill:other", name="other",
                                          files=bad["parts"][0]["files"][:1])]}
    with pytest.raises(ValueError):
        hk.read_bundle(extra, ["claude:skill:far"])


# --- end to end: ./sandbox-ai with a temporary HOME, and a fake ssh to a host's real ./sandbox-ai ------------------------

SSH = r'''#!PYTHON
import json, os, sys
args = sys.argv[1:]
if "-O" in args or "-G" in args or "--" not in args:
    sys.exit(255)
at = args.index("--")
host = args[at + 1].rpartition("@")[2].split(".")[0]
env_file = os.path.join(os.environ["FAKE_HOSTS"], host + ".json")
if not os.path.exists(env_file):
    print(f"ssh: connect to host {host} port 22: No route to host", file=sys.stderr)
    sys.exit(255)
env = json.load(open(env_file))
os.chdir(env["HOME"])
os.execve("/bin/sh", ["sh", "-c", args[at + 2] if len(args) > at + 2 else "true"], env)
'''


def machine(directory):
    """A user with a HOME of their own (XDG folders below it) and a bin folder with sandbox-ai first on PATH."""
    home, binary = directory / "home", directory / "bin"
    home.mkdir(parents=True)
    binary.mkdir()
    (binary / "sandbox-ai").write_text(f'#!/bin/sh\nexec "{ROOT / "sandbox-ai"}" "$@"\n')
    (binary / "sandbox-ai").chmod(0o755)
    env = {"HOME": str(home), "PATH": f"{binary}:/usr/bin:/bin", "LC_ALL": "C.UTF-8", "TERM": "dumb",
           "SANDBOX_AI_SSH_MULTIPLEX": "0", "TMPDIR": os.environ.get("TMPDIR", "/tmp")}
    return home, binary, env


def test_end_to_end_this_machine_and_a_host(tmp_path):
    laptop, laptop_bin, env = machine(tmp_path / "laptop")
    agent_home(laptop, tmp_path / "laptop-outside")
    remote, _, remote_env = machine(tmp_path / "box")
    make(remote, {".claude/skills/far/SKILL.md": "---\nname: far\ndescription: From afar\n---\nfar\n",
                  ".claude/projects/-srv/memory/MEMORY.md": "- [Deploys](deploys.md) — how we deploy\n",
                  ".claude/projects/-srv/memory/deploys.md": "---\nname: deploys\n---\nBlue-green.\n"})
    registry = tmp_path / "registry"
    registry.mkdir()
    (registry / "box.json").write_text(json.dumps(remote_env))
    (laptop_bin / "ssh").write_text(SSH.replace("PYTHON", sys.executable, 1))
    (laptop_bin / "ssh").chmod(0o755)
    env["FAKE_HOSTS"] = str(registry)
    (laptop / ".config/sandbox-ai").mkdir(parents=True)
    (laptop / ".config/sandbox-ai/hosts.json").write_text(json.dumps(
        {"version": 1, "hosts": {"box": {"ssh": "me@box.example", "port": 22}}}))

    def sai(*args):
        done = subprocess.run([str(ROOT / "sandbox-ai"), *args], env=env, capture_output=True, text=True, timeout=120)
        return done.returncode, done.stdout + done.stderr

    kits = laptop / ".local/share/sandbox-ai/kits"
    status, out = sai("kits", "host-list")
    assert status == 0 and "claude:skill:pdf" in out and "Claude Code (~/.claude)" in out, out
    assert not kits.exists()  # looking changes nothing
    status, out = sai("kits", "host-import", "--kit", "team", "--new", "claude:skill:pdf", "claude:command:deploy")
    assert status == 0, out
    assert (kits / "team/claude/skills/pdf/scripts/fill.py").is_file()
    assert (kits / "team/claude/commands/deploy.md").is_file()
    status, out = sai("kits", "show", "team")
    assert status == 0 and "pdf" in out and "deploy" in out, out
    # the host's agents, over SSH: listed, then a skill into this machine's kit and memory into a project here
    status, out = sai("kits", "host-list", "--host", "box", "--json")
    assert status == 0, out
    assert {p["id"] for p in json.loads(out)["parts"]} >= {"claude:skill:far", "claude:memory:-srv/deploys.md"}
    status, out = sai("kits", "host-import", "--kit", "team", "--host", "box", "claude:skill:far")
    assert status == 0, out
    assert (kits / "team/claude/skills/far/SKILL.md").read_text().endswith("far\n")
    project = tmp_path / "project"
    project.mkdir()
    status, out = sai("kits", "host-import", "--project", str(project), "--host", "box",
                      "claude:memory:-srv/deploys.md")
    assert status == 0, out
    stores = list((laptop / ".local/state/sandbox-ai/memory").glob("project-*/claude/.claude/projects/-work/memory"))
    assert len(stores) == 1 and (stores[0] / "deploys.md").read_text().endswith("Blue-green.\n")
    assert (stores[0] / "MEMORY.md").read_text() == "- [Deploys](deploys.md) — how we deploy\n"
    # the host's own kits are untouched: nothing was written there
    assert not (remote / ".local/share/sandbox-ai/kits").exists()


def test_code_that_only_names_a_secret_is_no_finding():
    files = [("run.py", b'api_key = os.environ["API_KEY"]\ntoken = get_token()\npassword: <yours>\n', 0o755),
             ("SKILL.md", b"Set TOKEN: $GITHUB_TOKEN\nsecret_ref = settings.secret\n", 0o644)]
    assert hk.scan(files) == []
    found = hk.scan([("x.env", b'DB_PASSWORD="correct-horse-battery"\nAPI_KEY=Zm9vYmFyYmF6cXV4MTIzNDU2\n', 0o644)])
    assert [f["line"] for f in found] == [1, 2] and "correct-horse" not in json.dumps(found)


def test_an_import_that_fails_on_the_way_is_undone(monkeypatch):
    """All or nothing also when a write fails: what was written is taken back and nothing is said to be imported."""
    run("new", "mine", "--agent", "claude")
    real, calls = kc.write_skill, []

    def failing(*args):
        calls.append(args)
        if len(calls) == 2:
            raise OSError(28, "No space left on device")
        return real(*args)
    monkeypatch.setattr(kc, "write_skill", failing)
    status, out = run("host-import", "--kit", "mine", "claude:skill:pdf", "claude:skill:inside",
                      "claude:memory-file:CLAUDE.md", "--allow-secrets")
    assert status == 1 and len(calls) == 2 and "->" not in out
    assert list((kit_root() / "mine/claude/skills").iterdir()) == []
    assert not (kit_root() / "mine/claude/CLAUDE.md").exists()


# --- 24h review A (2026-11-17): a secret far into a large file -----------------------------------------------------

def test_a_secret_behind_the_first_mebibyte_of_a_file_is_found_before_an_import(isolated):
    """The check for secret-looking lines read the first MiB of each file (SCAN_LIMIT: what a listing may cost); an
    import took a skill whose reference file held a token further in without a word, into a kit that reaches every
    sandbox that uses it. An import (and host-show) check every byte they are about to copy."""
    filler = "".join(f"line {number}: nothing to see here, only text of a reference\n" for number in range(30000))
    assert len(filler) > hk.SCAN_LIMIT
    make(isolated, {".claude/skills/big/SKILL.md": "---\nname: big\ndescription: A large reference\n---\nRead data.md\n",
                    ".claude/skills/big/data.md": filler + f"token {TOKEN}\n"})
    run("new", "mine", "--agent", "claude")
    status, out = run("host-import", "--kit", "mine", "claude:skill:big")
    assert status == 1 and not (kit_root() / "mine/claude/skills/big").exists(), out
    status, out = run("host-show", "claude:skill:big", "--json")
    shown = json.loads(out)
    assert TOKEN not in out and [item["rule"] for item in shown["secrets"]] == ["github-token"]
    assert shown["secrets"][0]["file"] == "data.md" and shown["secrets"][0]["line"] == 30001
    status, out = run("host-import", "--kit", "mine", "claude:skill:big", "--allow-secrets")
    assert status == 0 and (kit_root() / "mine/claude/skills/big/data.md").exists(), out
    # The listing reads the start of each file only: it says that it does not know (null), never "none".
    assert part(listing(), "claude:skill:big")["secrets"] is None
    assert part(listing(), "claude:skill:pdf")["secrets"] == 0


def test_toml_reads_without_tomllib_use_tomli_or_say_why(monkeypatch):
    """Review 24h B: macOS's python3 is 3.9 (no tomllib). The TOML parts (Codex's config.toml MCP servers, Qwen's and
    Gemini's TOML commands) are read with the tomli package there, and without either the listing says what is
    missing - never "not valid TOML" for a valid file."""
    import builtins
    real_import = builtins.__import__

    def without_tomllib(name, *args, **kwargs):
        if name in ("tomllib", "tomli"):
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", without_tomllib)
    monkeypatch.setattr(hk.importlib, "import_module", lambda name, *a: without_tomllib(name))
    data = listing()
    docs = [p for p in data["parts"] if p["id"].startswith("codex:mcp")]
    assert docs and all("not valid TOML" not in (p.get("why") or "") for p in docs), docs
    assert any("Python 3.11" in (p.get("why") or "") and "tomli" in (p.get("why") or "") for p in docs), docs
