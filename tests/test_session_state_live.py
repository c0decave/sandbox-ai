"""A session with its state to another system through the real entry point (./sandbox-ai), as users run it: two
users (two machines) with only Podman, tmux and ssh faked (live_fakes.py) - export on one, import on the other, the
agent's conversation continued at its resume there; a move over SSH in one command (the archive streamed to
`session import -` there); a duplicate here; a hostile archive refused."""
import io
import json
import subprocess
import tarfile

import pytest

from live_fakes import Host

CONVERSATION = {".claude/projects/-work/abc.jsonl": '{"type": "user", "message": "hello"}\n',
                ".claude/.credentials.json": '{"claudeAiOauth": {"accessToken": "sk-ant-oat-real"}}'}


@pytest.fixture
def two(tmp_path):
    here, there = Host(tmp_path / "here"), Host(tmp_path / "there")
    started = here.run("claude", "--name", "s1", env={"FAKE_PODMAN_RUN": "0:0",
                                                      "FAKE_PODMAN_WRITE_HOME": json.dumps(CONVERSATION)})
    assert started.returncode == 0, started.stderr
    return here, there


def sessions(host):
    listed = host.run("list", "--all", "--json")
    assert listed.returncode == 0, listed.stderr
    return {entry["name"]: entry for entry in json.loads(listed.stdout)}


def test_export_on_one_machine_import_on_another_and_the_agent_continues(two, tmp_path):
    here, there = two
    archive = tmp_path / "s1.tar.gz"
    exported = here.run("session", "export", "s1", str(archive))
    assert exported.returncode == 0, exported.stderr
    assert "with: its configuration, its HOME and the agent's session files" in exported.stdout
    assert b"sk-ant-oat-real" not in archive.read_bytes(), "a login only when asked"
    checked = there.run("session", "import", str(archive), f"--map={here.workspace}={there.workspace}", "--check")
    assert checked.returncode == 0, checked.stderr
    assert "nothing was kept" in checked.stdout and "s1" not in sessions(there)
    imported = there.run("session", "import", str(archive), f"--map={here.workspace}={there.workspace}", "--json")
    assert imported.returncode == 0, imported.stderr
    result = json.loads(imported.stdout)
    assert result["imported"] and result["workspace"] == str(there.workspace)
    assert str(here.workspace) not in json.dumps(json.loads((there.home / ".local/state/sandbox-ai/sessions/s1/"
                                                             "session.json").read_text()))
    entry = sessions(there)["s1"]
    assert entry["state"] == "stopped" and entry["workspace"] == str(there.workspace)
    home = there.home / ".local/state/sandbox-ai/sessions/s1/home"
    assert (home / ".claude/projects/-work/abc.jsonl").read_text() == CONVERSATION[".claude/projects/-work/abc.jsonl"]
    assert not (home / ".claude/.credentials.json").exists()
    assert not (home / ".aws/credentials").exists(), "the canaries of the other machine stay there"
    keep = tmp_path / "keep"
    resumed = there.run("resume", "s1", env={"FAKE_PODMAN_RUN": "0:0", "FAKE_PODMAN_KEEP": str(keep)})
    assert "the agent continues its last conversation" in resumed.stderr, resumed.stderr
    assert "--continue" in (keep / "session" / "agent-restart.sh").read_text()
    listed = there.run("checkpoint", "list", "s1", "--json")
    assert listed.returncode == 0, listed.stderr
    found = json.loads(listed.stdout)
    found = found["checkpoints"] if isinstance(found, dict) else found
    assert len(found) == 2, found  # the one it brought, and its own at this start


def test_move_over_ssh_in_one_command(two):
    here, there = two
    here.route_ssh("user@192.0.2.9", there)
    assert here.run("hosts", "add", "ct1", "user@192.0.2.9").returncode == 0
    moved = here.run("session", "move", "s1", "ct1", f"--map={here.workspace}={there.workspace}", timeout=600)
    text = moved.stdout + moved.stderr
    # The fakes' tmux never runs a terminal: its start there fails at the end, after the import there.
    assert "the state of s1" in text and "was imported on ct1 as s1" in text, text
    assert "start it there: sandbox-ai hosts run ct1 -- resume s1; s1 stays here as it was" in text, text
    assert sessions(there)["s1"]["workspace"] == str(there.workspace)
    assert (there.home / ".local/state/sandbox-ai/sessions/s1/home/.claude/projects/-work/abc.jsonl").exists()
    assert sessions(here)["s1"]["state"] == "stopped"
    again = here.run("session", "copy", "s1", "ct1", f"--map={here.workspace}={there.workspace}", timeout=600)
    assert again.returncode == 1 and "there is a session s1 here already" in again.stdout + again.stderr
    assert "--replace" in again.stdout + again.stderr
    only = here.run("session", "copy", "s1", "ct1", "--config-only", "--with-workspace")
    assert only.returncode != 0 and "--config-only copies no state" in only.stderr


def test_duplicate_here_and_both_are_kept(two):
    here, _ = two
    made = here.run("session", "duplicate", "s1", "s1-b", "--with-workspace")
    assert made.returncode == 0, made.stderr
    found = sessions(here)
    assert {"s1", "s1-b"} <= set(found)
    assert found["s1-b"]["workspace"] == f"{here.workspace}-s1-b" and found["s1"]["workspace"] == str(here.workspace)
    assert "sandbox-ai resume s1-b" in made.stdout


def test_a_hostile_archive_is_refused_through_the_entry_point(two, tmp_path):
    _, there = two
    path = tmp_path / "evil.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        info = tarfile.TarInfo("evil/home/.bashrc")
        info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
        tar.addfile(info)
        info = tarfile.TarInfo("evil/../../escape")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    refused = there.run("session", "import", str(path))
    assert refused.returncode == 1 and "is a link" in refused.stderr, refused.stderr
    assert "evil" not in sessions(there) and not (tmp_path / "escape").exists()
    piped = subprocess.run(there.command("session", "import", "-"), input=path.read_bytes(), capture_output=True,
                           env=there.env, cwd=there.workspace, timeout=120)
    assert piped.returncode == 1 and b"is a link" in piped.stderr
