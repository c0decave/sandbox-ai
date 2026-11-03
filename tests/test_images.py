"""sandbox-ai images, with podman replaced by recorded answers."""
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("images_tests", ROOT / "sandbox/images.py")
im = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(im)


def fake(argv, **kwargs):
    if argv[1:3] == ["image", "inspect"]:
        if "claude" in argv[-1]:
            return subprocess.CompletedProcess(argv, 0, json.dumps([{
                "Id": "sha256abcdef0123456789", "Created": "2026-11-03T10:00:00.123Z", "Size": 2_100_000_000,
                "Config": {"Labels": {"sandbox-ai.agent-version": "2.9.1"}}}]), "")
        return subprocess.CompletedProcess(argv, 125, "", "image not known")
    if argv[1] == "run":
        assert "--network=none" in argv and "--cap-drop=ALL" in argv and "--read-only" in argv
        return subprocess.CompletedProcess(argv, 0, "deb\tgit\t1:2.47\nnpm\t@anthropic-ai/claude-code\t2.9.1\n"
                                                    "tool\tgh\tgh version 2.80\nnoise\n", "")
    raise AssertionError(argv)


def test_images_and_packages(capsys):
    found = im.images(run=fake)
    assert found[0] == {"agent": "claude", "image": "localhost/claude-sandbox:latest", "present": True,
                        "id": "sha256abcdef", "created": "2026-11-03T10:00:00", "size": 2_100_000_000,
                        "agent_version": "2.9.1"}
    assert not found[1]["present"] and found[1]["agent"] == "codex"
    assert im.packages("claude", run=fake) == [("deb", "git", "1:2.47"), ("npm", "@anthropic-ai/claude-code", "2.9.1"),
                                               ("tool", "gh", "gh version 2.80")]
    with pytest.raises(ValueError):
        im.packages("other", run=fake)
    assert im.main([], run=fake) == 0
    out = capsys.readouterr().out
    assert "codex     not built (sandbox-ai build codex)" in out and "2.9.1" in out and "2.1 GB" in out
    assert im.main(["packages", "claude", "--json"], run=fake) == 0
    assert json.loads(capsys.readouterr().out)[0] == {"kind": "deb", "name": "git", "version": "1:2.47"}
