"""The kept starts' storage (user, 2026-11-19): transcripts compacted in place and read as before, the storage check's
plan (what, how much, the command, safe while starts run), the automatic rules, the growth history. End to end
through ./sandbox-ai on a fake host (tests/live_fakes.py), and storage.py itself."""
import gzip
import importlib.util
import json
import os
from pathlib import Path
import time

import pytest

from live_fakes import Host, ROOT

SPEC = importlib.util.spec_from_file_location("sandbox_storage_test", ROOT / "sandbox/storage.py")
st = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(st)


@pytest.fixture
def host(tmp_path):
    h = Host(tmp_path)
    h.env.pop("SANDBOX_AI_RUN", None)  # (this suite may run inside a sandbox-ai run)
    return h


def write(path, size=0, text=None, age_days=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text) if text is not None else path.write_bytes(b"x" * size)
    if age_days is not None:
        when = time.time() - age_days * 86400
        os.utime(path, (when, when))
    return path


def line(role, text):
    return json.dumps({"type": role, "message": {"role": role, "content": text}}) + "\n"


def conversation(n, role="user"):
    return "".join(line(role, f"message {i} " + "word " * 40) for i in range(n))


def fill(home):
    """A HOME as an agent leaves it: old and new transcripts, auto memory, caches, build output."""
    write(home / ".claude/projects/p/old-1.jsonl", text=conversation(300), age_days=60)
    write(home / ".claude/projects/p/old-2.jsonl", text=conversation(200), age_days=45)
    write(home / ".claude/projects/p/new.jsonl", text=conversation(50), age_days=1)
    write(home / ".claude/projects/p/memory/MEMORY.md", text="- remember this\n", age_days=200)
    write(home / ".claude/projects/q/only.jsonl", text=conversation(100), age_days=90)  # newest of its folder
    write(home / ".codex/sessions/2026/09/01/rollout-a.jsonl", text=conversation(100), age_days=70)
    write(home / ".codex/sessions/2026/11/15/rollout-b.jsonl", text=conversation(10), age_days=1)
    write(home / ".cache/pip/wheel", 30_000)
    write(home / "work/app/node_modules/x/index.js", 50_000)
    write(home / "notes.md", text="mine\n")
    return home


def kept_session(host, name, last_used_days=40, agent="claude"):
    directory = host.home / ".local/state/sandbox-ai/sessions" / name
    (directory / "tmux").mkdir(parents=True, mode=0o700)
    when = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - last_used_days * 86400))
    data = {"agent": agent, "options": [], "agent_args": [], "backend": "podman", "podman_url": None,
            "workspace": str(host.workspace), "cwd": str(host.workspace), "created": when, "cwd_project": None,
            "ready": True, "name": name, "project": host.workspace.name, "keep": True, "mode": "tmux",
            "pod": "agent-claude-0123abcd", "pid": 2 ** 22 + 17, "pid_start": 1, "started": when}
    (directory / "session.json").write_text(json.dumps(data))
    (directory / "exit.json").write_text(json.dumps({"status": 0, "ended": when}))
    return fill(directory / "home")


def files(home):
    return sorted(str(path.relative_to(home)) for path in home.rglob("*") if path.is_file() or path.is_symlink())


# -- compacting one file, and what is taken -------------------------------------------------------------------------

def test_compact_file_is_verified_and_reads_back_the_same(tmp_path):
    path = write(tmp_path / "a.jsonl", text=conversation(500), age_days=40)
    original, info = path.read_bytes(), path.stat()
    path.chmod(0o640)
    before, after = st.compact_file(path)
    packed = tmp_path / "a.jsonl.gz"
    assert not path.exists() and packed.is_file() and after < before
    assert gzip.decompress(packed.read_bytes()) == original
    assert int(packed.stat().st_mtime) == int(info.st_mtime) and oct(packed.stat().st_mode & 0o777) == "0o640"
    assert not list(tmp_path.glob(".*tmp"))
    fd = os.open(packed, os.O_RDONLY)
    try:
        data, size = st.read_fd(fd, os.fstat(fd), 100, packed.name)
    finally:
        os.close(fd)
    assert data == original[:100] and size == len(original)
    # put back as it was
    assert st.expand_file(packed) == path and path.read_bytes() == original and not packed.exists()
    with pytest.raises(st.CompactError):
        st.expand_file(path)


def test_a_file_written_while_it_is_compacted_stays_as_it_was(tmp_path):
    """The proof for a running start: an agent that appends to the file meanwhile (it would only to the newest of a
    conversation, which is never taken) - the copy goes, the original with what was appended stays."""
    path = write(tmp_path / "live.jsonl", text=conversation(100), age_days=40)
    with pytest.raises(st.CompactError, match="written meanwhile"):
        st.compact_file(path, written=lambda: path.open("a").write(line("assistant", "appended")))
    assert path.read_text().endswith(line("assistant", "appended"))
    assert not (tmp_path / "live.jsonl.gz").exists() and not list(tmp_path.glob(".*tmp"))
    # a compacted one already there is never written over; a link is never followed
    write(tmp_path / "b.jsonl", text="x\n")
    write(tmp_path / "b.jsonl.gz", text="other")
    with pytest.raises(st.CompactError):
        st.compact_file(tmp_path / "b.jsonl")
    (tmp_path / "c.jsonl").symlink_to(tmp_path / "b.jsonl")
    with pytest.raises(st.CompactError):
        st.compact_file(tmp_path / "c.jsonl")
    assert (tmp_path / "b.jsonl.gz").read_text() == "other"


def test_candidates_never_the_newest_nor_memory_nor_through_a_link(tmp_path):
    home = fill(tmp_path / "home")
    outside = write(tmp_path / "outside/rollout-z.jsonl", text="{}\n", age_days=400)
    write(tmp_path / "outside/rollout-y.jsonl", text="{}\n", age_days=300)
    (home / ".codex/archived_sessions").symlink_to(outside.parent)
    found = {os.path.relpath(item["path"], home) for item in st.candidates(home, 30)}
    assert found == {".claude/projects/p/old-1.jsonl", ".claude/projects/p/old-2.jsonl",
                     ".codex/sessions/2026/09/01/rollout-a.jsonl"}
    assert {os.path.relpath(item["path"], home) for item in st.candidates(home, 50)} == {
        ".claude/projects/p/old-1.jsonl", ".codex/sessions/2026/09/01/rollout-a.jsonl"}
    result = st.compact_home(home, 30)
    assert result["files"] == 3 and result["saved"] > 0 and not result["errors"]
    assert (home / ".claude/projects/p/old-1.jsonl.gz").exists() and (home / ".claude/projects/p/new.jsonl").exists()
    assert (home / ".claude/projects/p/memory/MEMORY.md").read_text() == "- remember this\n"
    assert (home / ".claude/projects/q/only.jsonl").exists() and outside.exists()
    assert st.marked(home.parent)["files"] == 3
    # again: nothing more (a compacted one is never taken twice; it still counts for "the newest")
    assert st.compact_home(home, 30)["files"] == 0
    # resume: only a compacted one left as the newest of its group -> put back
    (home / ".claude/projects/p/new.jsonl").unlink()
    assert st.ensure_continuable(home) == [".claude/projects/p/old-2.jsonl"]
    assert (home / ".claude/projects/p/old-2.jsonl").exists()


def test_settings_are_checked(tmp_path):
    config = tmp_path / "config"
    env = {"XDG_CONFIG_HOME": str(config)}
    assert st.settings(env) == {"storage_check": "weekly", "storage_threshold_gb": 5, "storage_growth_gb": 2,
                                "storage_caches_days": None, "storage_compact_days": None, "storage_pack_days": None,
                                "storage_logs_days": None}
    write(config / "sandbox-ai/defaults.json", text=json.dumps(
        {"storage_check": "daily", "storage_threshold_gb": 0.5, "storage_pack_days": 0, "storage_compact_days": 14}))
    found = st.settings(env)
    assert found["storage_check"] == "daily" and found["storage_threshold_gb"] == 0.5
    assert found["storage_pack_days"] is None and found["storage_compact_days"] == 14  # 0: broken, counts as off
    assert st.check_value("storage_check", "monthly") and st.check_value("storage_logs_days", True)
    assert st.check_value("storage_growth_gb", 3) is None


# -- end to end through ./sandbox-ai --------------------------------------------------------------------------------

def test_gc_compact_transcripts_shown_asked_then_read_as_before_end_to_end(host):
    home = kept_session(host, "big")
    before = files(home)
    old = (home / ".claude/projects/p/old-1.jsonl").read_text()
    refused = host.run("gc", "--compact-transcripts", "30")
    assert refused.returncode == 1 and "would compact the transcripts older than 30 days" in refused.stdout
    assert "--yes" in refused.stderr and files(home) == before
    dry = host.run("gc", "--only", "--compact-transcripts", "30", "--dry-run", "--json")
    assert dry.returncode == 0, dry.stderr
    planned = json.loads(dry.stdout)["transcripts"]
    assert [(item["name"], item["files"]) for item in planned] == [("big", 3)] and files(home) == before
    done = host.run("gc", "--only", "--compact-transcripts", "30", "--yes")
    assert done.returncode == 0, done.stderr
    assert "compacted 3 transcript(s) of big in place" in done.stdout
    assert (home / ".claude/projects/p/old-1.jsonl.gz").exists() and not (home / ".claude/projects/p/old-1.jsonl").exists()
    assert (home / ".cache/pip/wheel").exists() and (home / "work/app/node_modules").exists()  # --only: only that
    # read as before: session files lists it, shows it as a conversation, --raw gives it whole
    listed = json.loads(host.run("session", "files", "big", "--json").stdout)
    item = next(item for item in listed["files"] if item["name"] == ".claude/projects/p/old-1.jsonl.gz")
    assert item["compacted"] is True and item["format"] == "claude"
    shown = json.loads(host.run("session", "files", "big", "--json",
                                "--file=.claude/projects/p/old-1.jsonl.gz").stdout)
    assert len(shown["messages"]) == 300 and "message 299" in shown["messages"][-1]["text"]
    raw = host.run("session", "files", "big", "--raw", "--file=.claude/projects/p/old-1.jsonl.gz")
    assert raw.returncode == 0 and raw.stdout == old, raw.stderr
    # the existing removal by kind takes compacted ones too, and never the memory
    gone = host.run("gc", "--only", "--transcripts", "30", "--home", "big", "--yes")
    assert gone.returncode == 0, gone.stderr
    assert not (home / ".claude/projects/p/old-1.jsonl.gz").exists()
    assert (home / ".claude/projects/p/memory/MEMORY.md").exists() and (home / ".claude/projects/p/new.jsonl").exists()


def test_compacting_a_running_start_is_safe_end_to_end(host):
    """A start whose pod still runs (orphaned): its transcripts older than DAYS are compacted, the newest of each
    conversation (what its agent appends to) never; packing and removal by kind leave it alone."""
    home = kept_session(host, "busy")
    (home.parent / "exit.json").unlink()
    host.store.mkdir(parents=True, exist_ok=True)
    (host.store / "fake-pods.json").write_text(json.dumps({"agent-claude-0123abcd": {}}))
    live = {"FAKE_PODMAN_POD_LIVE": "1"}
    newest = (home / ".claude/projects/p/new.jsonl").read_bytes()
    done = host.run("gc", "--only", "--compact-transcripts", "30", "--home", "busy", "--yes", "--json", env=live)
    assert done.returncode == 0, done.stderr
    result = json.loads(done.stdout)["transcripts"]
    assert result[0]["state"] == "orphaned" and result[0]["files"] == 3
    assert (home / ".claude/projects/p/new.jsonl").read_bytes() == newest
    assert (home / ".codex/sessions/2026/11/15/rollout-b.jsonl").exists()
    check = json.loads(host.run("gc", "--check", "--home", "busy", "--json", env=live).stdout)
    kinds = {item["kind"]: item for item in check["plan"]}
    assert "caches" not in kinds and "pack" not in kinds and kinds["compact"]["safe_running"] is True


def test_resume_puts_back_the_newest_when_only_a_compacted_one_is_left(host):
    host.add_image("localhost/claude-sandbox:latest")
    home = kept_session(host, "old")
    assert host.run("gc", "--only", "--compact-transcripts", "30", "--yes").returncode == 0
    (home / ".claude/projects/p/new.jsonl").unlink()
    resumed = host.run("resume", "old")
    assert "put back as it was: .claude/projects/p/old-2.jsonl" in resumed.stderr, resumed.stderr
    assert (home / ".claude/projects/p/old-2.jsonl").exists()


def test_check_plan_apply_and_growth_end_to_end(host):
    home = kept_session(host, "big", last_used_days=40)
    kept_session(host, "fresh", last_used_days=1)
    first = host.run("gc", "--check", "--json")
    assert first.returncode == 0, first.stderr
    result = json.loads(first.stdout)
    by = {(item["kind"], item["start"]): item for item in result["plan"]}
    assert {("caches", "big"), ("build", "big"), ("pack", "big"), ("compact", "big"), ("compact", "fresh"),
            ("caches", "fresh")} <= set(by)
    assert ("pack", "fresh") not in by  # used a day ago: not recommended
    assert by[("caches", "big")]["command"] == "sandbox-ai gc --only --caches --home big --yes"
    assert by[("caches", "big")]["safe_running"] is False and by[("compact", "big")]["safe_running"] is True
    assert result["total"] == sum(item["bytes"] for item in result["plan"]) and result["above"] is False
    assert result["notice"] is None and result["grew"] == []
    assert {entry["name"] for entry in result["starts"]} == {"big", "fresh"}
    # the size of each start at this check, in sandbox-ai list --json
    listed = {entry["name"]: entry for entry in json.loads(host.run("list", "--stopped", "--json").stdout)}
    assert listed["big"]["storage"]["bytes"] > 0 and listed["big"]["storage"]["growth"] is None
    # apply one item: exactly its command, nothing else
    applied = host.run(*by[("caches", "big")]["argv"])
    assert applied.returncode == 0, applied.stderr
    assert not (home / ".cache/pip").exists() and (home / "work/app/node_modules").exists()
    # it grows: named above the growth limit (made small here), the threshold too
    for key, value in (("storage_growth_gb", "0.0001"), ("storage_threshold_gb", "0.00001")):
        assert host.run("defaults", "set", key, value).returncode == 0
    write(home / "work/big.bin", 400_000)
    second = json.loads(host.run("gc", "--check", "--json").stdout)
    assert [entry["name"] for entry in second["grew"]] == ["big"] and second["above"] is True
    assert "could be freed here" in second["notice"] and "big +" in second["notice"]
    grown = next(entry for entry in second["starts"] if entry["name"] == "big")
    assert grown["growth"] > 300_000 and grown["since"] == result["time"]
    listed = {entry["name"]: entry for entry in json.loads(host.run("list", "--stopped", "--json").stdout)}
    assert listed["big"]["storage"]["growth"] == grown["growth"]
    # --dry-run records nothing; the text form is a table with the commands
    text = host.run("gc", "--check", "--dry-run")
    assert text.returncode == 0 and "WHILE RUNNING" in text.stdout and "sandbox-ai gc --pack --home big --yes" in text.stdout
    assert len(json.loads((host.home / ".local/state/sandbox-ai/storage-history.json").read_text())["checks"]) == 2


def test_the_storage_view_of_one_start(host):
    home = kept_session(host, "big")
    found = host.run("gc", "--check", "--home", "big", "--json")
    assert found.returncode == 0, found.stderr
    result = json.loads(found.stdout)
    kinds = {item["kind"] for item in result["plan"]}
    assert kinds == {"caches", "build", "toolchains", "transcripts", "pack", "compact", "logs"}
    assert result["detail"]["starts"][0]["name"] == "big" and result["notice"] is None
    assert result["logs"]["days"] == 14 and "checkpoints" in result
    assert not (host.home / ".local/state/sandbox-ai/storage-history.json").exists()  # a view records nothing
    host.run("gc", "--pack", "--home", "big", "--yes")
    packed = json.loads(host.run("gc", "--check", "--home", "big", "--json").stdout)
    assert [item["kind"] for item in packed["plan"]] == ["logs", "unpack"] or \
        {item["kind"] for item in packed["plan"]} == {"logs", "unpack"}
    assert packed["packed"]["archive"] and not home.exists()
    text = host.run("gc", "--check", "--home", "big")
    assert "Storage of big" in text.stdout and "sandbox-ai gc --unpack big" in text.stdout
    for refused in (["gc", "--check", "--caches"], ["gc", "--check", "--yes"], ["gc", "--check", "--home", "a",
                                                                               "--home", "b"]):
        assert host.run(*refused).returncode == 1


def test_rules_are_off_by_default_then_applied_logged_and_said_once(host):
    home = kept_session(host, "big", last_used_days=40)
    fresh = kept_session(host, "fresh", last_used_days=1)
    host.run("gc", "--check", "--json")
    assert (home / ".cache/pip").exists() and (home / ".claude/projects/p/old-1.jsonl").exists()  # off: nothing
    for key, value in (("storage_caches_days", "30"), ("storage_compact_days", "30"), ("storage_pack_days", "35")):
        set_ = host.run("defaults", "set", key, value)
        assert set_.returncode == 0, set_.stderr
    dry = json.loads(host.run("gc", "--check", "--dry-run", "--json").stdout)
    assert {(a["rule"], a["name"]) for a in dry["rules"]["actions"]} == {
        ("caches", "big"), ("compact", "big"), ("compact", "fresh"), ("pack", "big")}
    assert home.is_dir() and (home / ".cache/pip").exists()  # a dry run applies nothing
    applied = json.loads(host.run("gc", "--check", "--json").stdout)
    assert {(a["rule"], a["name"]) for a in applied["said"]} == {
        ("caches", "big"), ("compact", "big"), ("compact", "fresh"), ("pack", "big")}
    assert "the storage rules" in applied["notice"] and "packed the HOME of big" in applied["notice"]
    assert not home.exists() and (fresh / ".cache/pip").exists() and (fresh / ".claude/projects/p/old-1.jsonl.gz").exists()
    log = (host.home / ".local/state/sandbox-ai/launcher.log").read_text()
    assert "storage rule: packed the HOME of big" in log
    again = json.loads(host.run("gc", "--check", "--json").stdout)
    assert again["said"] == [] and again["notice"] is None  # said once
    # gc applies them too (and says so); a broken value is refused by the launcher's defaults
    assert "storage rule" not in host.run("gc").stdout  # nothing left to do
    refused = host.run("defaults", "set", "storage_check", "monthly")
    assert refused.returncode != 0 and "storage_check is one of off, weekly, daily" in refused.stderr + refused.stdout
    assert host.run("defaults", "set", "storage_logs_days", "120").returncode == 0


def test_only_does_nothing_else(host):
    """gc --only leaves the leftovers, the expiry and the logs alone: what the plan runs is that item alone."""
    directory = host.home / ".local/state/sandbox-ai/sessions/left"
    (directory / "home").mkdir(parents=True)
    (directory / "session.json").write_text(json.dumps({"agent": "claude", "options": [], "keep": False,
                                                       "name": "left", "backend": "podman", "cwd": "/"}))
    (directory / "exit.json").write_text(json.dumps({"status": 0, "ended": "2026-01-01T00:00:00Z"}))
    kept_session(host, "big")
    done = host.run("gc", "--only", "--caches", "--json")
    assert done.returncode == 0, done.stderr
    result = json.loads(done.stdout)
    assert result["leftovers"] == [] and result["compacted"] is None and directory.exists()
    assert [item["name"] for item in result["caches"]] == ["big"]
    assert host.run("gc", "--only").returncode == 1 and host.run("gc", "--only", "--caches", "--older-than",
                                                                 "3").returncode == 1


def test_plain_gc_applies_the_rules_and_says_so_its_dry_run_only_lists_them(host):
    home = kept_session(host, "big", last_used_days=40)
    assert host.run("defaults", "set", "storage_caches_days", "30").returncode == 0
    dry = host.run("gc", "--dry-run", "--json")
    actions = json.loads(dry.stdout)["rules"]["actions"]
    assert [(a["rule"], a["name"]) for a in actions] == [("caches", "big")] and (home / ".cache/pip").exists()
    done = host.run("gc")
    assert done.returncode == 0 and "storage rule: emptied the download caches of big" in done.stdout, done.stdout
    assert not (home / ".cache/pip").exists() and (home / "work/app/node_modules").exists()


def test_the_web_runs_only_the_plans_commands():
    spec = importlib.util.spec_from_file_location("webui_for_storage_test", ROOT / "sandbox/webui.py")
    web = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(web)
    assert web.storage_argv(["gc", "--only", "--caches", "--home", "big", "--yes"])
    assert web.storage_argv(["gc", "--pack", "--home", "big", "--yes"]) and web.storage_argv(["gc"])
    for bad in (["gc", "--older-than", "0"], ["rm"], ["gc", "--only", "--home", "--yes"], "gc", None,
                ["gc", "--pack", "--home", "big"], ["gc", "--only", "--compact-transcripts", "0"]):
        with pytest.raises(web.Refused):
            web.storage_argv(bad)
