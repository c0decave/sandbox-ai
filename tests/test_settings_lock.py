"""Review 5 (correctness M3): llm.json, git-logins.json, publish-logins.json and identities.json were read, changed
and written back without a lock: 12 parallel `llm add` left 5 servers, and an `llm remove` racing `llm models`
brought the server back. Every read-modify-write now holds the file's lock (filelock.py)."""
import concurrent.futures
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SANDBOX = ROOT / "sandbox"


def load(name):
    spec = importlib.util.spec_from_file_location(f"{name}_for_lock_tests", SANDBOX / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    return {**{k: v for k, v in os.environ.items() if not k.startswith("XDG_")}, "HOME": str(home),
            "XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state"),
            "XDG_DATA_HOME": str(tmp_path / "data"), "XDG_CACHE_HOME": str(tmp_path / "cache"),
            "SANDBOX_AI_SECRETS_BACKEND": "file"}


def cli(env, *argv):
    return subprocess.run([sys.executable, "-I", str(SANDBOX / "main.py"), *argv], env=env, capture_output=True,
                          text=True, timeout=120)


def parallel(env, commands):
    with concurrent.futures.ThreadPoolExecutor(len(commands)) as pool:
        done = list(pool.map(lambda argv: cli(env, *argv), commands))
    assert all(d.returncode == 0 for d in done), [d.stderr for d in done if d.returncode]
    return done


def test_parallel_llm_adds_lose_nothing(env):
    parallel(env, [["llm", "add", f"s{n}", "--kind", "ollama", "--url", f"http://127.0.0.1:1{n:03d}"]
                   for n in range(10)])
    listed = json.loads(cli(env, "llm", "list", "--json").stdout)
    assert sorted(e["name"] for e in listed) == sorted(f"s{n}" for n in range(10))


def test_parallel_git_and_publish_logins_and_identities_lose_nothing(env):
    parallel(env, [["git-logins", "add", f"https://h{n}.example.org", "--secret", f"S{n}"] for n in range(8)]
             + [["identities", "add", f"id{n}", "--name", f"N {n}", "--email", f"n{n}@example.org"]
                for n in range(6)])
    assert len(json.loads(cli(env, "git-logins", "list", "--json").stdout)) == 8
    assert len(json.loads(cli(env, "identities", "list", "--json").stdout)) == 6
    parallel(env, [["identities", "login", f"id{n}", f"https://git{n}.example.org", "--secret", f"T{n}"]
                   for n in range(6)] + [["publish-logins", "add", kind, "--secret", f"P_{kind.upper()}"]
                                         for kind in ("npm", "pypi", "crates")])
    identities = json.loads(cli(env, "identities", "list", "--json").stdout)
    assert all(len(identity["git"]) == 1 for identity in identities.values())
    assert len(json.loads(cli(env, "publish-logins", "list", "--json").stdout)) == 3


def test_a_model_list_never_brings_back_a_removed_server(env, monkeypatch):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    llm = load("llm")
    assert cli(env, "llm", "add", "gone", "--kind", "ollama", "--url", "http://127.0.0.1:11434").returncode == 0
    assert cli(env, "llm", "remove", "gone").returncode == 0
    llm.remember_models("gone", ["m1"])  # what an `llm models gone` that read the server before ends with
    assert "gone" not in llm.load_store()["endpoints"]


def test_the_lock_is_this_users_file_and_waits_in_time(tmp_path):
    filelock = load("filelock")
    target = tmp_path / "x.json"
    with filelock.locked(target):
        with filelock.locked(target):  # reentrant in one thread
            pass
        assert (tmp_path / "x.json.lock").stat().st_mode & 0o777 == 0o600
        other = subprocess.run([sys.executable, "-I", "-c", (
            "import importlib.util, sys; spec = importlib.util.spec_from_file_location('f', sys.argv[1]); "
            "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
            "try:\n    m.locked(sys.argv[2], timeout=0.3).__enter__()\nexcept ValueError as e:\n    print('busy', e)"),
            str(SANDBOX / "filelock.py"), str(target)], capture_output=True, text=True, timeout=60)
        assert other.stdout.startswith("busy") and "another command" in other.stdout
    (tmp_path / "y.json.lock").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(OSError):
        with filelock.locked(tmp_path / "y.json"):
            pass
