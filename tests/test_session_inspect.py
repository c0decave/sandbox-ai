"""sandbox-ai session memory|files|export (session_inspect.py): a start's agent memory, the agents' own session files
in its HOME, and its folder as one archive - read only, the HOME untrusted. Fixture HOMEs per agent layout, hostile
ones (links out, a FIFO, a huge file, escape sequences, HTML, deep nesting, broken JSON lines), and the real entry
point (./sandbox-ai session ...)."""
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sandbox_session_inspect_test", ROOT / "sandbox/session_inspect.py")
si = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(si)
WORKSPACE = "/srv/project"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    (tmp_path / "runtime").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    (tmp_path / "user").mkdir()


def state(tmp_path):
    return tmp_path / "state" / "sandbox-ai"


def kept(tmp_path, key, agent="claude", options=(), running=False):
    """A kept start KEY of AGENT: stopped, or RUNNING (its run registered with this test's PID)."""
    directory = state(tmp_path) / "sessions" / key
    (directory / "home").mkdir(parents=True)
    os.chmod(directory / "home", 0o700)
    data = {"agent": agent, "options": ["--workspace", WORKSPACE, *options], "agent_args": [], "backend": "podman",
            "workspace": WORKSPACE, "cwd": "/", "created": "2026-11-18T00:00:00Z", "keep": True}
    (directory / "supervisor.log").write_text("agent-sandbox: started\n")
    if running:
        run = f"{agent}-0123abcd"
        data.update(run=run)
        runs = tmp_path / "runtime" / "sandbox-ai" / "runs" / run
        runs.mkdir(parents=True)
        (runs / "run.json").write_text(json.dumps({"run": run, "agent": agent, "pid": os.getpid(), "start": key,
                                                   "workspace": WORKSPACE, "agent_home": str(directory / "home"),
                                                   "name": key}))
        data["pid"] = os.getpid()
    (directory / "session.json").write_text(json.dumps(data))
    return directory / "home"


def put(root, relative, data):
    path = Path(root) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode())
    return path


def store(tmp_path, agent="claude"):
    return si.module("kits").memory_store(WORKSPACE, agent)


@pytest.fixture
def running(monkeypatch):
    """locate() of a start taken as running: starts.find and the run record are what the test says (a real running
    supervisor needs Podman)."""
    def make(home, agent="claude", state_="running", workspace=WORKSPACE, directory=None, options=()):
        entry = {"name": "web", "id": "web", "kind": "session", "agent": agent, "state": state_, "keep": True,
                 "run": "r1", "workspace": workspace, "directory": directory}
        monkeypatch.setattr(si.module("starts"), "find", lambda name, found=None: dict(entry))
        monkeypatch.setattr(si.module("net"), "running", lambda orphans=None: {"r1": {"agent_home": str(home)}})
        return entry
    return make


# --- memory -----------------------------------------------------------------------------------------------------------

def test_memory_of_a_running_start_is_its_live_copy(tmp_path, running):
    home = tmp_path / "home"
    put(home, ".claude/CLAUDE.md", "# notes\nbuild with make\n")
    put(home, ".claude/projects/-work/memory/MEMORY.md", "- [one](one.md)\n")
    put(home, ".claude/projects/-work/memory/one.md", "first\n")
    put(store(tmp_path), ".claude/CLAUDE.md", "the store's\n")
    running(home)
    result = si.memory("web")
    assert result["source"] == "home" and result["path"] == str(home)
    assert [f["name"] for f in result["files"]] == [".claude/CLAUDE.md", ".claude/projects/-work/memory/MEMORY.md",
                                                    ".claude/projects/-work/memory/one.md"]
    assert all(isinstance(f["size"], int) and f["modified"].endswith("Z") for f in result["files"])
    lines = si.memory_lines(result)
    assert "its memory as the agent has it now, in its HOME" in lines[0]
    shown = si.memory("web", file=".claude/CLAUDE.md")
    assert [f["text"] for f in shown["files"]] == ["# notes\nbuild with make\n"]
    assert si.memory_lines(shown, file=".claude/CLAUDE.md", raw=True) == ["# notes\nbuild with make\n"]
    every = si.memory("web", every=True)
    assert len([f for f in every["files"] if f.get("text") is not None]) == 3


def test_memory_of_a_stopped_start_is_what_went_back_to_the_store(tmp_path):
    home = kept(tmp_path, "web")
    put(home, ".claude/CLAUDE.md", "the HOME's copy\n")
    put(store(tmp_path), ".claude/CLAUDE.md", "the store's\n")
    result = si.memory("web", file=".claude/CLAUDE.md")
    assert result["source"] == "store" and result["files"][0]["text"] == "the store's\n"
    assert "what went back to the project's memory store" in si.memory_lines(result)[0]
    assert si.memory("web", file=".claude/CLAUDE.md", choice="home")["files"][0]["text"] == "the HOME's copy\n"


def test_memory_sad_paths(tmp_path, running):
    home = kept(tmp_path, "web", options=("--no-memory",))
    put(home, ".claude/CLAUDE.md", "x")
    with pytest.raises(ValueError, match="without project memory"):
        si.memory("web", choice="store")
    assert si.memory("web")["source"] == "home"  # (auto: no store to show, the HOME's copy)
    with pytest.raises(ValueError, match="not among the memory files"):
        si.memory("web", file="../../etc/passwd")
    with pytest.raises(ValueError, match="nothing named"):
        si.memory("nobody")
    kept(tmp_path, "fresh")
    with pytest.raises(ValueError, match="no memory store"):
        si.memory("fresh", choice="store")


def test_memory_never_follows_a_link_out_of_the_home(tmp_path, running):
    home, outside = tmp_path / "home", tmp_path / "outside"
    put(outside, "secret.md", "the host's secret")
    put(home, ".claude/projects/-work/memory/MEMORY.md", "index")
    os.symlink(outside / "secret.md", home / ".claude/projects/-work/memory/leak.md")
    os.symlink(outside, home / ".claude/projects/-work/memory/dir")
    os.symlink(outside / "secret.md", home / ".claude/CLAUDE.md")
    running(home)
    result = si.memory("web", every=True)
    assert [f["name"] for f in result["files"]] == [".claude/projects/-work/memory/MEMORY.md"]
    assert "the host's secret" not in json.dumps(result)
    assert any("symlink" in note for note in result["notes"])


def test_memory_text_is_escaped_for_a_terminal(tmp_path, running):
    home = tmp_path / "home"
    put(home, ".claude/CLAUDE.md", "title\x1b]0;owned\x07 \x1b[31mred\u202e<b>x</b>\n")
    running(home)
    text = "\n".join(si.memory_lines(si.memory("web", file=".claude/CLAUDE.md"), file=".claude/CLAUDE.md"))
    assert "\x1b" not in text and "\x07" not in text and "\u202e" not in text
    assert "\\x1b]0;owned" in text and "<b>x</b>" in text  # (markup is shown as it is: a terminal has none)


def test_memory_of_each_agent_layout(tmp_path, running):
    kits = si.module("kits")
    for agent, (_, relative) in kits.MEMORY_FILE.items():
        home = tmp_path / agent
        put(home, relative, f"{agent}'s memory")
        running(home, agent=agent)
        result = si.memory("web")
        assert [f["name"] for f in result["files"]] == [relative], agent
        assert result["memory_file"] == relative


# --- session files ----------------------------------------------------------------------------------------------------

CLAUDE_LINES = [
    {"type": "user", "timestamp": "2026-11-18T10:00:00Z", "message": {"role": "user", "content": "list the files"}},
    {"type": "assistant", "timestamp": "2026-11-18T10:00:02Z", "message": {"role": "assistant", "content": [
        {"type": "thinking", "thinking": "use ls"}, {"type": "text", "text": "Listing."},
        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]}},
    {"type": "user", "timestamp": "2026-11-18T10:00:03Z", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "a\n" * 3000, "is_error": False}]}},
]
CODEX_LINES = [
    {"timestamp": "2026-11-18T10:00:00Z", "type": "session_meta", "payload": {"id": "c1", "cwd": "/work"}},
    {"timestamp": "2026-11-18T10:00:01Z", "type": "response_item", "payload": {
        "type": "message", "role": "user", "content": [{"type": "input_text", "text": "fix the bug"}]}},
    {"timestamp": "2026-11-18T10:00:02Z", "type": "response_item", "payload": {
        "type": "function_call", "name": "shell", "arguments": "{\"command\": [\"make\"]}", "call_id": "x"}},
    {"timestamp": "2026-11-18T10:00:03Z", "type": "response_item", "payload": {
        "type": "function_call_output", "call_id": "x", "output": "ok"}},
    {"timestamp": "2026-11-18T10:00:04Z", "type": "response_item", "payload": {
        "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Fixed."}]}},
]


def jsonl(lines):
    return "".join(json.dumps(line) + "\n" for line in lines)


def test_claude_transcript_as_a_conversation(tmp_path, running):
    home = tmp_path / "home"
    put(home, ".claude/projects/-work/s1.jsonl", jsonl(CLAUDE_LINES))
    put(home, ".claude/projects/-work/memory/MEMORY.md", "not a session")
    running(home)
    listed = si.session_files("web")
    assert [f["name"] for f in listed["files"]] == [".claude/projects/-work/s1.jsonl"]
    assert listed["files"][0]["verified"] is True and listed["files"][0]["format"] == "claude"
    shown = si.session_files("web", file=".claude/projects/-work/s1.jsonl")
    roles = [m["role"] for m in shown["messages"]]
    assert roles == ["user", "reasoning", "assistant", "tool-call", "tool-result"]
    assert shown["messages"][3]["tool"] == "Bash" and '"command": "ls"' in shown["messages"][3]["text"]
    assert shown["messages"][4]["shortened"] and "--full shows all" in shown["messages"][4]["text"]
    assert shown["messages"][0]["time"] == "2026-11-18T10:00:00Z"
    full = si.session_files("web", file=".claude/projects/-work/s1.jsonl", full=True)
    assert full["messages"][4]["text"] == "a\n" * 3000 and not full["messages"][4]["shortened"]
    text = "\n".join(si.files_lines(shown))
    assert "== 2026-11-18 10:00:03  tool result" in text and "tool call: Bash" in text


def test_codex_rollout_as_a_conversation(tmp_path, running):
    home = tmp_path / "home"
    put(home, ".codex/sessions/2026/11/18/rollout-1.jsonl", jsonl(CODEX_LINES))
    running(home, agent="codex")
    name = ".codex/sessions/2026/11/18/rollout-1.jsonl"
    shown = si.session_files("web", file=name)
    assert [m["role"] for m in shown["messages"]] == ["event", "user", "tool-call", "tool-result", "assistant"]
    assert shown["messages"][2]["tool"] == "shell" and shown["messages"][4]["text"] == "Fixed."


def test_every_agent_place_is_listed(tmp_path, running):
    home = tmp_path / "home"
    files = {".claude/projects/-work/a.jsonl": "claude", ".codex/sessions/2026/01/02/rollout-a.jsonl": "codex",
             ".codex/archived_sessions/rollout-b.jsonl": "codex", ".local/share/opencode/opencode.db": "opencode",
             ".pi/agent/sessions/--work--/s.jsonl": "pi", ".dsh/sessions/s1.json": "dsh",
             ".nanobot/sessions/web.jsonl": "nanobot", ".letta/lc-local-backend/conversations/c.json": "letta",
             ".openhands/conversations/abc/events.json": "openhands", ".hermes/sessions/s.jsonl": "hermes",
             ".qwen/tmp/abc/chats/session.json": "qwen", ".local/share/goose/sessions/s.jsonl": "goose"}
    for relative in files:
        put(home, relative, "{}\n")
    put(home, ".letta/lc-local-backend/providers/auth.json", "{\"key\": \"sk-secret\"}")
    put(home, ".hermes/auth.json", "{}")
    running(home)
    listed = {f["name"]: f for f in si.session_files("web")["files"]}
    # Letta's provider keys: listed, marked as keys (by default nothing that holds a login or key is hidden)
    assert listed.pop(".letta/lc-local-backend/providers/auth.json")["secret"] == "an agent's login"
    assert set(listed) == set(files)
    assert {f["secret"] for f in listed.values()} == {None}
    assert {name: f["agent"] for name, f in listed.items()} == files
    assert {f["verified"] for name, f in listed.items() if files[name] in ("claude", "codex", "opencode", "pi")} == {True}
    assert {f["verified"] for name, f in listed.items() if files[name] not in ("claude", "codex", "opencode", "pi")} \
        == {False}
    lines = si.files_lines(si.session_files("web"))
    assert any("not verified" in line and ".hermes" in line for line in lines)


def test_generic_json_and_jsonl_shapes(tmp_path, running):
    home = tmp_path / "home"
    put(home, ".pi/agent/sessions/w/s.jsonl", jsonl([
        {"type": "session", "id": "p1", "timestamp": "2026-11-18T09:00:00Z"},
        {"type": "message", "timestamp": "2026-11-18T09:00:01Z", "message": {"role": "user", "content": [
            {"type": "text", "text": "hi"}]}},
        {"type": "message", "message": {"role": "assistant", "content": [
            {"type": "toolCall", "name": "read", "arguments": {"path": "a"}}]}},
        {"type": "message", "message": {"role": "toolResult", "content": [{"type": "text", "text": "body"}]}}]))
    put(home, ".dsh/sessions/s.json", json.dumps({"messages": [{"role": "user", "content": "q"},
                                                               {"role": "assistant", "content": "a"}]}))
    running(home, agent="pi")
    pi = si.session_files("web", file=".pi/agent/sessions/w/s.jsonl")["messages"]
    assert [m["role"] for m in pi] == ["event", "user", "tool-call", "tool-result"]
    assert pi[2]["tool"] == "read" and pi[3]["text"] == "body"
    dsh = si.session_files("web", file=".dsh/sessions/s.json")["messages"]
    assert [(m["role"], m["text"]) for m in dsh] == [("user", "q"), ("assistant", "a")]


def test_hostile_transcripts(tmp_path, running):
    home, outside = tmp_path / "home", tmp_path / "outside"
    put(outside, "x.jsonl", jsonl(CLAUDE_LINES))
    put(home, ".claude/projects/-work/ok.jsonl", "\n".join([
        "{broken json", "[" * 100000 + "]" * 100000,
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "<script>alert(1)</script>\x1b[2J\x1b]52;c;aGk=\x07"}]}}),
        json.dumps({"type": "user", "message": {"content": 5}}), ""]))
    os.symlink(outside / "x.jsonl", home / ".claude/projects/-work/link.jsonl")
    os.symlink(outside, home / ".claude/projects/-out")
    os.mkfifo(home / ".claude/projects/-work/fifo.jsonl")
    deep = home / ".claude/projects"
    for index in range(40):
        deep = deep / f"d{index}"
    put(deep, "s.jsonl", "{}\n")
    running(home)
    names = [f["name"] for f in si.session_files("web")["files"]]
    assert names == [".claude/projects/-work/ok.jsonl"]
    shown = si.session_files("web", file=".claude/projects/-work/ok.jsonl")
    assert [m["text"] for m in shown["messages"]] == ["<script>alert(1)</script>\x1b[2J\x1b]52;c;aGk=\x07"]
    assert any("2 line(s) are no JSON object" in note for note in shown["notes"])
    text = "\n".join(si.files_lines(shown))
    assert "\x1b" not in text and "\x07" not in text and "<script>" in text
    for bad in (".claude/projects/-work/link.jsonl", ".claude/projects/-work/fifo.jsonl", "/etc/passwd",
                ".claude/projects/-out/x.jsonl", "../outside/x.jsonl"):
        with pytest.raises(ValueError, match="not among the session files"):
            si.session_files("web", file=bad)


def test_a_huge_transcript_is_read_bounded(tmp_path, running, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(si, "FILE_LIMIT", 4096)
    put(home, ".claude/projects/-work/big.jsonl", jsonl(CLAUDE_LINES[:1]) * 200)
    running(home)
    shown = si.session_files("web", file=".claude/projects/-work/big.jsonl")
    assert any("only its first 4.0 KiB" in note for note in shown["notes"])
    assert 0 < len(shown["messages"]) < 200
    out = io.BytesIO()
    si.raw_file("web", ".claude/projects/-work/big.jsonl", out)
    assert out.getvalue() == (home / ".claude/projects/-work/big.jsonl").read_bytes()


def test_session_files_need_a_home(tmp_path):
    directory = kept(tmp_path, "gone").parent
    (directory / "home").rmdir()
    with pytest.raises(ValueError, match="HOME of gone is gone"):
        si.session_files("gone")


def test_auth_targets_are_the_launchers():
    spec = importlib.util.spec_from_file_location("sandbox_launcher_for_inspect_test", ROOT / "sandbox/agent_sandbox.py")
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    assert si.auth_targets() == set(launcher.AUTH_TARGETS.values())


# --- logins and keys: listed and marked by default, shown only when asked, hidden by the setting ---------------------

def defaults(tmp_path, **values):
    """Your defaults file (XDG_CONFIG_HOME) with VALUES."""
    folder = tmp_path / "config" / "sandbox-ai"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "defaults.json").write_text(json.dumps({"version": 1, **values}))
    os.environ["XDG_CONFIG_HOME"] = str(tmp_path / "config")


@pytest.fixture(autouse=True)
def own_config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-config"))


def secret_home(tmp_path):
    home = tmp_path / "home"
    put(home, ".claude/projects/-work/s1.jsonl", jsonl(CLAUDE_LINES))
    put(home, ".claude/projects/-work/token-dump.jsonl", jsonl(CLAUDE_LINES))
    put(home, ".letta/lc-local-backend/providers/openai.json", "{\"key\": \"sk-x\"}")
    return home


def test_logins_and_keys_are_listed_and_marked_by_default(tmp_path, running):
    running(secret_home(tmp_path), agent="claude")
    result = si.session_files("web")
    marks = {f["name"]: f["secret"] for f in result["files"]}
    assert marks == {".claude/projects/-work/s1.jsonl": None,
                     ".claude/projects/-work/token-dump.jsonl": "a login, token or key (by its name)",
                     ".letta/lc-local-backend/providers/openai.json": "an agent's provider keys"}
    assert result["hidden"] == 0 and result["hide_secrets"] is False
    text = "\n".join(si.files_lines(result))
    assert "token-dump.jsonl" in text and "LOGIN OR KEY" in text and "(by its name)" in text


def test_the_setting_hides_them_as_before(tmp_path, running):
    running(secret_home(tmp_path))
    hidden = si.session_files("web", hide_secrets=True)
    assert [f["name"] for f in hidden["files"]] == [".claude/projects/-work/s1.jsonl"]
    assert hidden["hidden"] == 2 and hidden["hide_secrets"] is True
    assert any("2 files that hold a login or key are not listed" in line for line in si.files_lines(hidden))
    with pytest.raises(ValueError, match="not among the session files"):
        si.session_files("web", file=".claude/projects/-work/token-dump.jsonl", show_secret=True, hide_secrets=True)
    defaults(tmp_path, hide_secrets=True)  # the defaults key: the same
    assert si.session_files("web")["hidden"] == 2
    defaults(tmp_path, hide_secrets=False)
    assert si.session_files("web")["hidden"] == 0


def test_a_login_or_key_is_shown_only_when_asked(tmp_path, running):
    running(secret_home(tmp_path))
    name = ".claude/projects/-work/token-dump.jsonl"
    with pytest.raises(ValueError, match="--show-secret"):
        si.session_files("web", file=name)
    with pytest.raises(ValueError, match="--show-secret"):
        si.raw_file("web", name, io.BytesIO())
    shown = si.session_files("web", file=name, show_secret=True)
    assert shown["file"]["secret"] and [m["role"] for m in shown["messages"]][0] == "user"
    assert any("holds a login or key" in line for line in si.files_lines(shown))
    out = io.BytesIO()
    si.raw_file("web", name, out, show_secret=True)
    assert out.getvalue() == jsonl(CLAUDE_LINES).encode()


def test_memory_marks_logins_and_keys_by_name_and_content(tmp_path, running):
    home = tmp_path / "home"
    begin = "-----BEGIN"
    put(home, ".claude/CLAUDE.md", "# notes\n")
    put(home, ".claude/projects/-work/memory/MEMORY.md", "index\n")
    put(home, ".claude/projects/-work/memory/api-token.md", "tok\n")
    put(home, ".claude/projects/-work/memory/deploy.md", f"{begin} OPENSSH PRIVATE KEY-----\nxx\n")
    running(home)
    result = si.memory("web")
    marks = {f["name"]: f["secret"] for f in result["files"]}
    assert marks[".claude/CLAUDE.md"] is None
    assert marks[".claude/projects/-work/memory/api-token.md"] == "a login, token or key (by its name)"
    assert marks[".claude/projects/-work/memory/deploy.md"] == "holds a private key"
    with pytest.raises(ValueError, match="--show-secret"):
        si.memory("web", file=".claude/projects/-work/memory/deploy.md")
    assert si.memory("web", file=".claude/projects/-work/memory/deploy.md", show_secret=True)["files"][0]["text"] \
        .startswith(begin)
    every = {f["name"]: f for f in si.memory("web", every=True)["files"]}
    assert every[".claude/projects/-work/memory/deploy.md"]["text"] is None
    assert every[".claude/CLAUDE.md"]["text"] == "# notes\n"
    assert any("--show-secret" in note for note in si.memory("web", every=True)["notes"])
    assert si.memory("web", every=True, show_secret=True)["files"][2]["text"] == "tok\n"
    hidden = si.memory("web", hide_secrets=True)
    assert [f["name"] for f in hidden["files"]] == [".claude/CLAUDE.md", ".claude/projects/-work/memory/MEMORY.md"]
    assert hidden["hidden"] == 2


# --- export -----------------------------------------------------------------------------------------------------------

def test_export_writes_the_folder_and_leaves_out_secrets(tmp_path):
    home = kept(tmp_path, "web")
    put(home, ".claude/projects/-work/s.jsonl", jsonl(CLAUDE_LINES))
    put(home, ".claude/.credentials.json", "{\"token\": \"sk-real\"}")
    begin = "-----BEGIN"  # (a key's first line, made here: the privacy check rightly flags one in a file)
    put(home, ".ssh/id_ed25519", f"{begin} OPENSSH PRIVATE KEY-----\nxx\n")
    put(home, ".ssh/known_hosts", "host key\n")
    put(home, "notes/ca.pem", f"{begin} RSA PRIVATE KEY-----\nabc\n")
    put(home, ".config/tool.json", "{\"apiKey\": \"sandbox-ai-api-key-placeholder-OPENAI_API_KEY\"}")
    put(home, "work/readme.txt", "hello")
    put(home, ".config/gh/hosts.yml", "github.com:\n  oauth_token: gho_x\n")
    (tmp_path / "outside").mkdir()
    os.symlink(tmp_path / "outside", home / "out")
    os.mkfifo(home / "pipe")
    out = tmp_path / "web.tar.gz"
    result = si.export("web", str(out))
    assert result["path"] == str(out)
    with tarfile.open(out) as tar:
        names = tar.getnames()
        members = {m.name: m for m in tar.getmembers()}
        report = json.loads(tar.extractfile("web/export.json").read())
        assert tar.extractfile("web/home/work/readme.txt").read() == b"hello"
    assert "web/session.json" in names and "web/supervisor.log" in names
    assert "web/home/.claude/projects/-work/s.jsonl" in names and "web/home/.ssh/known_hosts" in names
    for gone in (".claude/.credentials.json", ".ssh/id_ed25519", "notes/ca.pem", ".config/tool.json", "out", "pipe",
                 ".config/gh/hosts.yml"):
        assert f"web/home/{gone}" not in names
    left = {item["path"]: item["why"] for item in result["left_out"]}
    assert left[".claude/.credentials.json"] == "an agent's login"
    assert left["notes/ca.pem"] == "holds a private key"
    assert "placeholder" in left[".config/tool.json"] and "link" in left["out"] and "regular" in left["pipe"]
    assert report["left_out"] == result["left_out"]
    assert all(not name.startswith(("/", "..")) and ".." not in name.split("/") for name in names)
    assert all(m.isfile() or m.isdir() for m in members.values())
    assert b"sk-real" not in out.read_bytes()
    # the summary names what was left out as a login, key or secret, and how to take it in
    assert sorted(result["secrets_left_out"]) == sorted([".claude/.credentials.json", ".ssh/id_ed25519",
                                                        "notes/ca.pem", ".config/tool.json", ".config/gh/hosts.yml"])
    assert result["include_secrets"] is False and report["include_secrets"] is False
    text = "\n".join(si.export_lines(result))
    assert "logins, keys and secrets left out (5)" in text and "--include-secrets" in text
    assert ".config/gh/hosts.yml" in text


def test_export_with_include_secrets_takes_them_in(tmp_path):
    home = kept(tmp_path, "web")
    put(home, ".claude/.credentials.json", "{\"token\": \"sk-real\"}")
    put(home, ".git-credentials", "https://u:tok@example.org\n")
    put(home, "work/readme.txt", "hello")
    (tmp_path / "outside").mkdir()
    os.symlink(tmp_path / "outside", home / "out")
    out = tmp_path / "web.tar.gz"
    result = si.export("web", str(out), include_secrets=True)
    with tarfile.open(out) as tar:
        names = tar.getnames()
        report = json.loads(tar.extractfile("web/export.json").read())
        assert tar.extractfile("web/home/.git-credentials").read() == b"https://u:tok@example.org\n"
    assert "web/home/.claude/.credentials.json" in names
    assert result["secrets_left_out"] == [] and result["include_secrets"] is True and report["include_secrets"] is True
    assert [item["path"] for item in result["left_out"]] == ["out"]  # (a link is still never followed)
    assert "logins, keys and secrets are in it" in "\n".join(si.export_lines(result))


def test_export_refusals(tmp_path):
    home = kept(tmp_path, "web")
    (tmp_path / "there.tar.gz").write_text("mine")
    with pytest.raises(ValueError, match="is there already"):
        si.export("web", str(tmp_path / "there.tar.gz"))
    assert (tmp_path / "there.tar.gz").read_text() == "mine"
    with pytest.raises(ValueError, match="lies in the folder that is exported"):
        si.export("web", str(home / "x.tar.gz"))
    with pytest.raises(ValueError, match="no folder"):
        si.export("web", str(tmp_path / "missing" / "x.tar.gz"))


def test_export_over_the_limit_is_refused_and_leaves_nothing(tmp_path, monkeypatch):
    home = kept(tmp_path, "web")
    put(home, "big.bin", b"x" * 5000)
    monkeypatch.setattr(si, "EXPORT_TOTAL", 1000)
    with pytest.raises(ValueError, match="too large"):
        si.export("web", str(tmp_path / "x.tar.gz"))
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.endswith((".gz", "partial"))) == []
    assert not list(tmp_path.glob(".x.tar.gz.partial-*"))


# --- the real entry point ---------------------------------------------------------------------------------------------

def cli(*args, env=None):
    return subprocess.run([str(ROOT / "sandbox-ai"), "session", *args], capture_output=True,
                          text=True, env=env or os.environ, timeout=120)


def test_cli_end_to_end(tmp_path):
    home = kept(tmp_path, "web", running=True)
    put(home, ".claude/CLAUDE.md", "remember \x1b[31mthis\n")
    put(home, ".claude/projects/-work/s1.jsonl", jsonl(CLAUDE_LINES))
    listed = cli("memory", "web")
    assert listed.returncode == 0, listed.stderr
    assert "in its HOME" in listed.stdout and ".claude/CLAUDE.md" in listed.stdout
    shown = cli("memory", "web", "--file", ".claude/CLAUDE.md")
    assert "remember \\x1b[31mthis" in shown.stdout and "\x1b" not in shown.stdout
    raw = cli("memory", "web", "--file", ".claude/CLAUDE.md", "--raw")
    assert raw.stdout == "remember \\x1b[31mthis\n\n"
    data = json.loads(cli("memory", "web", "--json").stdout)
    assert data["source"] == "home" and data["files"][0]["name"] == ".claude/CLAUDE.md"
    files = json.loads(cli("files", "web", "--json").stdout)
    assert [f["name"] for f in files["files"]] == [".claude/projects/-work/s1.jsonl"]
    talk = cli("files", "web", "--file", ".claude/projects/-work/s1.jsonl")
    assert talk.returncode == 0 and "tool call: Bash" in talk.stdout and "--full shows all" in talk.stdout
    as_is = cli("files", "web", "--file", ".claude/projects/-work/s1.jsonl", "--raw")
    assert as_is.stdout == jsonl(CLAUDE_LINES)  # (into a pipe: the bytes as they are)
    refused = cli("files", "web", "--file", "/etc/passwd")
    assert refused.returncode == 1 and "not among the session files of web" in refused.stderr
    assert cli("files", "web", "--full").returncode != 0
    archive = tmp_path / "out.tar.gz"
    done = cli("export", "web", str(archive), "--json")
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["path"] == str(archive) and tarfile.is_tarfile(archive)
    again = cli("export", "web", str(archive))
    assert again.returncode == 1 and "is there already" in again.stderr


def test_cli_logins_and_keys_end_to_end(tmp_path):
    home = kept(tmp_path, "web", running=True)
    put(home, ".claude/projects/-work/s1.jsonl", jsonl(CLAUDE_LINES))
    put(home, ".claude/projects/-work/secret-notes.jsonl", jsonl(CLAUDE_LINES))
    put(home, ".git-credentials", "https://u:tok@example.org\n")
    env = {**os.environ, "XDG_CONFIG_HOME": str(tmp_path / "cfg")}
    listed = json.loads(cli("files", "web", "--json", env=env).stdout)
    assert {f["name"]: bool(f["secret"]) for f in listed["files"]} == {
        ".claude/projects/-work/s1.jsonl": False, ".claude/projects/-work/secret-notes.jsonl": True}
    text = cli("files", "web", env=env).stdout
    assert "secret-notes.jsonl" in text and "LOGIN OR KEY" in text and "(by its name)" in text
    hidden = json.loads(cli("files", "web", "--json", "--hide-secrets", env=env).stdout)
    assert [f["name"] for f in hidden["files"]] == [".claude/projects/-work/s1.jsonl"] and hidden["hidden"] == 1
    refused = cli("files", "web", "--file", ".claude/projects/-work/secret-notes.jsonl", env=env)
    assert refused.returncode == 1 and "--show-secret" in refused.stderr
    shown = cli("files", "web", "--file", ".claude/projects/-work/secret-notes.jsonl", "--show-secret", env=env)
    assert shown.returncode == 0 and "tool call: Bash" in shown.stdout and "holds a login or key" in shown.stdout
    (tmp_path / "cfg" / "sandbox-ai").mkdir(parents=True)
    (tmp_path / "cfg" / "sandbox-ai" / "defaults.json").write_text(json.dumps({"version": 1, "hide_secrets": True}))
    assert json.loads(cli("files", "web", "--json", env=env).stdout)["hidden"] == 1
    (tmp_path / "cfg" / "sandbox-ai" / "defaults.json").unlink()
    # export: left out and said; --include-secrets is asked first (no terminal here: --yes), then taken in
    archive = tmp_path / "out.tar.gz"
    done = cli("export", "web", str(archive), env=env)
    assert done.returncode == 0 and ".git-credentials" in done.stdout and "--include-secrets" in done.stdout
    asked = cli("export", "web", str(tmp_path / "with.tar.gz"), "--include-secrets", env=env)
    assert asked.returncode == 1 and "--yes" in asked.stderr and not (tmp_path / "with.tar.gz").exists()
    taken = cli("export", "web", str(tmp_path / "with.tar.gz"), "--include-secrets", "--yes", "--json", env=env)
    assert taken.returncode == 0, taken.stderr
    assert json.loads(taken.stdout)["include_secrets"] is True
    with tarfile.open(tmp_path / "with.tar.gz") as tar:
        assert "web/home/.git-credentials" in tar.getnames()


def test_remote_host_gets_the_choices(monkeypatch):
    calls = []

    def run(host, args, timeout=120, **kwargs):
        calls.append(args)
        if "--hide-secrets" in args and len(calls) == 1:
            return 2, "", "sandbox-ai: error: unrecognized arguments: --hide-secrets"
        return 0, json.dumps({"name": "web", "id": "web", "agent": "claude", "state": "running", "home": "/h",
                              "files": []}), ""
    monkeypatch.setattr(si.module("hosts"), "run", run)
    args = type("A", (), {"name": "web", "file": None, "full": False, "raw": False, "json": True, "host": "ct1",
                          "command": "files", "hide_secrets": True, "show_secret": False})()
    assert si.run(args, lambda line: None) == 0
    # an older sandbox-ai there knows no --hide-secrets: it never lists them anyway, asked again without it
    assert calls == [["session", "files", "--json", "web", "--hide-secrets"], ["session", "files", "--json", "web"]]
    calls.clear()
    args.hide_secrets, args.file, args.show_secret = False, ".claude/projects/-w/token.jsonl", True
    monkeypatch.setenv("XDG_CONFIG_HOME", "/nonexistent")
    si.run(args, lambda line: None)
    assert calls == [["session", "files", "--json", "web", "--file=.claude/projects/-w/token.jsonl", "--show-secret"]]


def test_remote_host_reads_json_there(monkeypatch):
    calls = []

    def run(host, args, timeout=120, **kwargs):
        calls.append((host, args))
        return 0, json.dumps({"name": "web", "id": "web", "agent": "claude", "state": "running", "source": "home",
                              "path": "/h", "memory_file": ".claude/CLAUDE.md", "files": [], "notes": [],
                              "complete": True}), ""
    monkeypatch.setattr(si.module("hosts"), "run", run)
    lines = []
    args = type("A", (), {"name": "web", "file": None, "all": False, "source": "auto", "raw": False, "json": False,
                          "host": "ct1", "command": "memory", "hide_secrets": False, "show_secret": False})()
    assert si.run(args, lines.append) == 0
    assert calls == [("ct1", ["session", "memory", "--json", "web", "--from=auto"])]
    assert "No memory files yet" in "\n".join(lines)
    monkeypatch.setattr(si.module("hosts"), "run", lambda *a, **k: (2, "", "invalid choice: 'memory'"))
    with pytest.raises(ValueError, match="older"):
        si.run(args, lines.append)
