"""End to end through the real ./sandbox-ai (Podman faked, live_fakes.Host): a start checkpoints its workspace on the
host before its sandbox runs; the fake agent deletes everything in /work; `sandbox-ai checkpoint restore NAME`
shows what it changes, asks, and brings every file back - its project's .git and its links included. The store
lies in sandbox-ai's own state and is no mount of the sandbox."""
import json
import os
from pathlib import Path

from live_fakes import Host


def test_an_agent_that_deletes_the_workspace_is_undone(tmp_path):
    host = Host(tmp_path / "host")
    host.add_image("localhost/claude-sandbox:latest")
    ws = host.workspace
    (ws / "src").mkdir()
    (ws / "src/main.py").write_text("print('mine')\n")
    (ws / ".git/refs/heads").mkdir(parents=True)
    (ws / ".git/HEAD").write_text("ref: refs/heads/main\n")
    (ws / "run.sh").write_text("#!/bin/sh\n")
    (ws / "run.sh").chmod(0o755)
    (ws / "latest").symlink_to("src/main.py")
    env = {"FAKE_PODMAN_RUN": "0:0", "FAKE_PODMAN_WIPE_WORK": "1"}
    started = host.run("claude", "--no-tmux", "--name", "wiper", "--no-canaries", env=env)
    assert started.returncode == 0, started.stderr
    assert "checkpoint " in started.stderr and "sandbox-ai checkpoint restore wiper" in started.stderr
    assert list(ws.iterdir()) == [], "the fake agent deleted everything"
    # The sandbox's mounts: the workspace, never the store.
    store = Path(host.home) / ".local/state/sandbox-ai/checkpoints"
    run_call = next(call for call in host.calls() if call[:1] == ["run"] and "--pod" in call)
    assert not any("sandbox-ai/checkpoints" in arg for arg in run_call)
    listed = host.run("checkpoint", "list", "wiper", "--json")
    assert listed.returncode == 0, listed.stderr
    [checkpoint] = json.loads(listed.stdout)
    assert checkpoint["kind"] == "start" and checkpoint["files"] == 3 and checkpoint["name"] == "wiper"
    assert checkpoint["roots"][0]["links"] == 1
    preview = host.run("checkpoint", "restore", "wiper", "--dry-run")
    assert preview.returncode == 0, preview.stderr
    assert "D /work/src/main.py" in preview.stdout and "D /work/.git/HEAD" in preview.stdout
    assert list(ws.iterdir()) == [], "a dry run changes nothing"
    refused = host.run("checkpoint", "restore", "wiper")
    assert refused.returncode == 1 and "--yes" in refused.stderr, "nothing can ask: not without --yes"
    done = host.run("checkpoint", "restore", "wiper", "--yes")
    assert done.returncode == 0, done.stderr
    assert (ws / "src/main.py").read_text() == "print('mine')\n"
    assert (ws / ".git/HEAD").read_text() == "ref: refs/heads/main\n" and (ws / ".git/refs/heads").is_dir()
    assert os.readlink(ws / "latest") == "src/main.py" and os.stat(ws / "run.sh").st_mode & 0o777 == 0o755
    assert "undo it with: sandbox-ai checkpoint restore" in done.stdout
    # The restore's own checkpoint (the state before it) is listed and can be restored in turn.
    kinds = [row["kind"] for row in json.loads(host.run("checkpoint", "list", "--json").stdout)]
    assert kinds == ["restore", "start"]


def test_a_start_with_no_checkpoint_takes_none_and_the_plan_says_where_they_are(tmp_path):
    host = Host(tmp_path / "host")
    host.add_image("localhost/claude-sandbox:latest")
    plan = host.run("claude", "--dry-run", "--checkpoint-every", "15", "--mount", f"{tmp_path}:/mnt/data:rw",
                    "--checkpoint-mounts")
    assert plan.returncode == 0, plan.stderr
    checkpoint = json.loads(plan.stdout)["checkpoint"]
    assert checkpoint["every"] == 15 and checkpoint["start"] is True
    assert [root["sandbox"] for root in checkpoint["roots"]] == ["/work", "/mnt/data"]
    assert checkpoint["store"].startswith(str(Path(host.home) / ".local/state/sandbox-ai/checkpoints/"))
    none = host.run("claude", "--dry-run", "--no-checkpoint")
    assert json.loads(none.stdout)["checkpoint"] is None
    ro = host.run("claude", "--dry-run", "--workspace-ro")
    assert json.loads(ro.stdout)["checkpoint"] is None, "nothing writable: nothing to checkpoint"
    started = host.run("claude", "--no-tmux", "--no-checkpoint", "--no-canaries", env={"FAKE_PODMAN_RUN": "0:0"})
    assert started.returncode == 0, started.stderr
    assert host.run("checkpoint", "list", "--json").stdout.strip() == "[]"
    bad = host.run("claude", "--dry-run", "--checkpoint-every", "9999")
    assert bad.returncode != 0 and "1440" in bad.stderr
