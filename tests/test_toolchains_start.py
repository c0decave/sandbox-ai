"""--toolchain at a start (sandbox/agent_sandbox.py, runtime.py): the plan, the mounts of Podman and Bubblewrap,
the agent's PATH and variables, the registries and the security modes, the administrator's policy, and the dry run
through ./sandbox-ai."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import home_guard  # noqa: E402

home_guard.ensure()

import test_toolchains as tt  # noqa: E402
from test_toolchains import data_home, signing_key  # noqa: E402,F401  (fixtures)

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("agent_sandbox_toolchains_tests", ROOT / "sandbox/agent_sandbox.py")
s = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(s)
T = s.load_toolchains_module()


@pytest.fixture
def store(tmp_path, signing_key, data_home):
    """go (two registries, the preset github), jdk (variables, a cache), and python added unsigned."""
    key, trusted = signing_key
    entries = [tt.entry(tmp_path, allow=["github"]),
               tt.entry(tmp_path, name="jdk", version="21.0.9", members={"bin": None, "bin/java": b"j"},
                        commands={"java": "bin/java"}, hosts=["repo.maven.apache.org"],
                        env={"JAVA_HOME": "{prefix}"}, caches={"GRADLE_USER_HOME": ".gradle"})]
    source = tt.catalog(tmp_path, entries, key)
    for item in entries:
        tt.t.add(item["name"], source, out=lambda line: None, keys=trusted)
    unsigned = tt.catalog(tmp_path, [tt.entry(tmp_path, name="python", version="3.14.0", hosts=[], allow=["pypi"])],
                          name="own.json")
    tt.t.add("python", unsigned, unsigned=True, out=lambda line: None, keys=trusted)
    return tt.t.installed()


def parsed(*options, mode="medium"):
    a = s.parse_args(["claude", *options])
    a.mode = mode
    return a


def test_the_start_plans_its_toolchains_and_opens_their_registries(store):
    a = parsed("--toolchain", "go", "--toolchain", "jdk@21.0.9", "--allow-host", "example.org")
    notes = s.plan_toolchains(a)
    assert [e["name"] for e in s.toolchain_entries(a)] == ["go", "jdk"]
    assert a.allow_host == ["example.org", "proxy.golang.org", "sum.golang.org", "repo.maven.apache.org"]
    assert a.presets == ["github"]
    assert any("the registries of go, jdk are opened: proxy.golang.org, sum.golang.org, repo.maven.apache.org, "
               "--allow-github" in note for note in notes)
    plain = parsed()
    assert s.plan_toolchains(plain) == [] and plain.toolchain_plan is None and s.toolchain_entries(plain) == []


@pytest.mark.parametrize("mode, options, why", [
    ("high", [], "security mode high"), ("paranoid", [], "security mode paranoid"),
    ("medium", ["--toolchain-offline"], "offline"), ("medium", ["--offline"], "offline")])
def test_strict_modes_and_offline_mount_the_toolchain_and_keep_its_registries_closed(store, mode, options, why):
    a = parsed("--toolchain", "go", *options, mode=mode)
    notes = s.plan_toolchains(a)
    assert [e["name"] for e in s.toolchain_entries(a)] == ["go"]
    assert a.allow_host == [] and a.presets == []
    assert any(f"{why}: the registries of go stay closed (proxy.golang.org, sum.golang.org, --allow-github)" in note
               for note in notes)


def test_a_toolchain_is_no_widening_of_a_strict_mode_but_its_hosts_given_by_hand_are(store):
    assert s.MODES.refusal(s, "claude", ["--toolchain", "go"], "paranoid") is None
    assert "--allow-host" in s.MODES.refusal(s, "claude", ["--toolchain", "go", "--allow-host", "proxy.golang.org"],
                                             "high")


def test_a_start_says_what_is_missing_unsigned_or_cannot_run_here(store, monkeypatch):
    with pytest.raises(ValueError, match="zig is not in the store: sandbox-ai toolchain add zig"):
        s.plan_toolchains(parsed("--toolchain", "zig"))
    with pytest.raises(ValueError, match="a toolchain's name is"):
        s.plan_toolchains(parsed("--toolchain", "../go"))
    notes = s.plan_toolchains(parsed("--toolchain", "python"))
    assert any(note.startswith("warning: the toolchain python 3.14.0 was added unsigned") for note in notes)
    monkeypatch.setattr(T, "host_glibc", lambda: (2, 5))
    a = parsed("--toolchain", "go", "--backend", "bwrap")
    with pytest.raises(ValueError, match="--toolchain go: go 1.25.4 needs glibc 2.17 or newer, this host has 2.5"):
        T.start_plan(a.toolchain, a.backend, glibc=T.host_glibc)
    with pytest.raises(ValueError, match="another Podman service"):
        s.plan_toolchains(parsed("--toolchain", "go", "--podman-url", "unix:///run/x.sock"))
    own = parsed("--toolchain", "go", "--image", "registry.example.org/mine:1")
    assert not s.own_image(own) and s.own_image(parsed())
    assert any("an image of your own is not checked" in note for note in s.plan_toolchains(own))


def test_the_administrators_policy_can_forbid_toolchains(store):
    a = parsed("--toolchain", "go")
    with pytest.raises(ValueError, match="--toolchain is not allowed by the administrator's policy"):
        s.check_policy(a, {"forbid": ["--toolchain"]})
    s.check_policy(parsed(), {"forbid": ["--toolchain"]})


@pytest.mark.parametrize("policy, closed", [
    # the ceiling: only the hosts the administrator allows (proxy.golang.org is not among them)
    ({"version": 1, "allow_within": ["10.0.0.0/8"], "allow_hosts": ["pypi.org", "sum.golang.org"]},
     ["proxy.golang.org", "--allow-github"]),
    # --allow-host forbidden: no host of a toolchain either; its preset is another option
    ({"version": 1, "forbid": ["--allow-host"]}, ["proxy.golang.org", "sum.golang.org"]),
    # one preset forbidden
    ({"version": 1, "forbid": ["--allow-github"]}, ["--allow-github"]),
    # one host forbidden by its value
    ({"version": 1, "forbid": ["--allow-host=proxy.golang.org"]}, ["proxy.golang.org"])])
def test_the_administrators_policy_binds_a_toolchains_registries(store, policy, closed):
    """Security review 2026-11-22: the policy is checked before the toolchains are planned, so the registries a
    toolchain names joined --allow-host and the presets past the policy's ceiling (allow_hosts) and its forbidden
    options. What the policy does not take stays closed (the toolchain is mounted all the same, said in a note)."""
    a = parsed("--toolchain", "go")
    s.check_policy(a, policy)
    notes = s.plan_toolchains(a)
    opened = [*a.allow_host, *(f"--allow-{p}" for p in a.presets)]
    assert not set(closed) & set(opened), opened
    assert [e["name"] for e in s.toolchain_entries(a)] == ["go"]
    assert a.toolchain_plan["closed"] == closed
    assert any("the administrator's policy" in note and "stay closed" in note for note in notes), notes
    s.check_ceiling(a)  # what is opened stays inside the ceiling


def test_the_policy_binds_a_toolchain_at_a_start_through_main(store, monkeypatch, capsys, tmp_path):
    """The same through the launcher's main (a dry run): the plan opens nothing the policy's ceiling leaves out."""
    monkeypatch.setattr(s, "load_policy", lambda *args: {"version": 1, "allow_within": ["10.0.0.0/8"],
                                                         "allow_hosts": ["sum.golang.org"]})
    workspace = tmp_path / "project"
    workspace.mkdir()
    assert s.main(["claude", "--toolchain", "go", "--workspace", str(workspace), "--no-config", "--dry-run"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["toolchain_hosts"] == {"opened": ["sum.golang.org"],
                                       "closed": ["proxy.golang.org", "--allow-github"]}


def mounts_of(run):
    return [run[i + 1] for i, value in enumerate(run) if value == "--mount"]


def test_podman_mounts_the_sessions_folder_and_each_toolchain_read_only(store, tmp_path):
    session = tmp_path / "session"
    (session / "home").mkdir(parents=True)
    a = parsed("--toolchain", "go", "--toolchain", "jdk")
    s.plan_toolchains(a)
    assert s.prepare_toolchains(a, session) == []
    _, run = s.pod_commands(a, "pod", session, session / "home")
    found = [m for m in mounts_of(run) if "/opt/toolchains" in m or "profile.d" in m]
    go, jdk = (store[name][version]["sha256"] for name, version in (("go", "1.25.4"), ("jdk", "21.0.9")))
    assert found == [
        f"type=bind,src={session}/toolchains,dst=/opt/toolchains,ro,nodev,nosuid",
        f"type=bind,src={T.store()}/{go},dst=/opt/toolchains/go,ro,nodev,nosuid",
        f"type=bind,src={T.store()}/{jdk},dst=/opt/toolchains/jdk,ro,nodev,nosuid",
        f"type=bind,src={session}/toolchains/profile.sh,dst=/etc/profile.d/sandbox-ai-toolchains.sh,ro,nodev,nosuid"]
    # the mount points and the wrappers are there before the sandbox starts
    assert (session / "toolchains/go").is_dir() and (session / "toolchains/bin/java").is_file()
    assert not any("rw" in m.split(",") for m in found)


def test_bubblewrap_binds_them_read_only_in_that_order(store, tmp_path):
    session = tmp_path / "session"
    (session / "home").mkdir(parents=True)
    a = parsed("--toolchain", "go", "--backend", "bwrap")
    s.plan_toolchains(a)
    s.prepare_toolchains(a, session)
    cmd = s.bwrap_command(a, session, session / "home")
    binds = [(cmd[i], cmd[i + 1], cmd[i + 2]) for i, value in enumerate(cmd)
             if value in ("--ro-bind", "--bind") and "/opt/toolchains" in cmd[i + 2]]
    go = store["go"]["1.25.4"]["sha256"]
    assert binds == [("--ro-bind", f"{session}/toolchains", "/opt/toolchains"),
                     ("--ro-bind", f"{T.store()}/{go}", "/opt/toolchains/go")]
    assert "/etc/profile.d/sandbox-ai-toolchains.sh" not in cmd  # the host's /etc is not the sandbox's


def test_every_start_has_the_folder_so_that_a_toolchain_can_be_attached_later(store, tmp_path):
    session = tmp_path / "session"
    (session / "home").mkdir(parents=True)
    a = parsed()
    s.plan_toolchains(a)
    s.prepare_toolchains(a, session)
    _, run = s.pod_commands(a, "pod", session, session / "home")
    assert f"type=bind,src={session}/toolchains,dst=/opt/toolchains,ro,nodev,nosuid" in mounts_of(run)
    assert list((session / "toolchains/bin").iterdir()) == []
    assert "/opt/toolchains" in s.bwrap_command(parsed("--backend", "bwrap"), session, session / "home")
    # a session directory without it (a caller that prepared none): nothing is mounted, nothing fails
    bare = tmp_path / "bare"
    (bare / "home").mkdir(parents=True)
    assert not [m for m in mounts_of(s.pod_commands(a, "pod", bare, bare / "home")[1])
                if "dst=/opt/toolchains" in m or "profile.d" in m]


def test_the_agents_environment_has_the_toolchains_variables_unless_you_set_them(store, monkeypatch):
    a = parsed("--toolchain", "jdk")
    s.plan_toolchains(a)
    env = s.agent_environment(a)
    assert env["JAVA_HOME"] == "/opt/toolchains/jdk" and env["GRADLE_USER_HOME"] == "/root/.gradle"
    monkeypatch.setenv("JAVA_HOME", "/usr/lib/jvm/mine")
    mine = parsed("--toolchain", "jdk", "--env", "JAVA_HOME")
    s.plan_toolchains(mine)
    assert s.agent_environment(mine)["JAVA_HOME"] == "/usr/lib/jvm/mine"
    assert "JAVA_HOME" not in s.agent_environment(parsed())


def test_the_agents_processes_get_the_wrappers_first_on_path_the_supervisors_do_not():
    r = s.RUNTIME
    assert r.TOOLCHAINS_BIN == T.BIN and r.TOOLCHAINS_ENV == T.ENV_FILE
    base = r.base_env()
    env = r.toolchain_env(dict(base), there=lambda path: True)
    assert env["PATH"] == "/opt/toolchains/bin:" + base["PATH"] and env["BASH_ENV"] == "/opt/toolchains/env.sh"
    assert "/opt/toolchains" not in base["PATH"]  # base_env() is the supervisor's: unchanged
    browser = r.toolchain_env({**base, "PATH": "/sandbox/session/bin:" + base["PATH"], "BASH_ENV": "/root/mine"},
                              there=lambda path: True)
    assert browser["PATH"].split(":")[:2] == ["/opt/toolchains/bin", "/sandbox/session/bin"]
    assert browser["BASH_ENV"] == "/root/mine"  # set by you: kept
    again = r.toolchain_env(dict(env), there=lambda path: True)
    assert again["PATH"] == env["PATH"]
    # a sandbox without the folder (an older start's session): nothing changes
    assert r.toolchain_env(dict(base), there=lambda path: False) == base


def test_the_run_remembers_its_toolchains(store, tmp_path):
    a = parsed("--toolchain", "go")
    s.plan_toolchains(a)
    T.record_start(tmp_path, a.toolchain_plan)
    rows = T.run_listing({"directory": str(tmp_path), "run": "claude-1"})
    assert [(row["name"], row["source"], row["sandbox"]) for row in rows] == [("go", "start", "/opt/toolchains/go")]


# --- through ./sandbox-ai ---------------------------------------------------------------------------------------


def dry_run(*options, env):
    result = subprocess.run([str(ROOT / "sandbox-ai"), "claude", "--dry-run", "--no-config", *options],
                            capture_output=True, text=True, env={**os.environ, **env}, timeout=120)
    return result


def test_the_dry_run_shows_the_toolchains_and_what_they_open(store, data_home, tmp_path):
    env = {"XDG_DATA_HOME": str(data_home)}
    result = dry_run("--toolchain", "go", "-tc", "jdk", env=env)
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert [(e["name"], e["version"], e["sandbox"], e["mode"]) for e in plan["toolchains"]] == [
        ("go", "1.25.4", "/opt/toolchains/go", "ro"), ("jdk", "21.0.9", "/opt/toolchains/jdk", "ro")]
    assert plan["toolchains"][0]["commands"] == ["go", "gofmt"] and plan["toolchains"][0]["signed_by"]
    assert plan["toolchain_hosts"] == {"opened": ["proxy.golang.org", "sum.golang.org", "repo.maven.apache.org",
                                                  "--allow-github"], "closed": []}
    assert "the registries of go, jdk are opened" in result.stderr
    strict = dry_run("--toolchain", "go", "--mode", "high", env=env)
    assert strict.returncode == 0, strict.stderr
    plan = json.loads(strict.stdout)
    assert plan["toolchains"][0]["name"] == "go"
    assert plan["toolchain_hosts"] == {"opened": [], "closed": ["proxy.golang.org", "sum.golang.org",
                                                                "--allow-github"]}
    assert "security mode high: the registries of go stay closed" in strict.stderr
    offline = json.loads(dry_run("--toolchain", "go", "--toolchain-offline", env=env).stdout)
    assert offline["toolchain_hosts"]["opened"] == []
    none = json.loads(dry_run(env=env).stdout)
    assert none["toolchains"] == [] and none["toolchain_hosts"] is None
    missing = dry_run("--toolchain", "zig", env=env)
    assert missing.returncode != 0 and "zig is not in the store: sandbox-ai toolchain add zig" in missing.stderr
    assert "Traceback" not in missing.stderr


def test_the_dry_run_takes_a_version_by_its_major_and_refuses_two_of_one_toolchain(tmp_path, signing_key, data_home):
    """./sandbox-ai claude --toolchain jdk@17: the newest 17.x of the store, beside jdk 21 there; two versions of
    one toolchain at a start are refused with the reason (one mount point)."""
    key, trusted = signing_key
    entries = [tt.entry(tmp_path, name="jdk", version=version, members={"bin": None, "bin/java": b"j",
                                                                       "VERSION": version.encode()},
                        commands={"java": "bin/java"}, hosts=[]) for version in ("21.0.13+9", "17.0.21+9")]
    source = tt.catalog(tmp_path, entries, key)
    for value in ("jdk", "jdk@17"):
        tt.t.add(value, source, out=lambda line: None, keys=trusted)
    env = {"XDG_DATA_HOME": str(data_home)}
    for value, version in (("jdk@17", "17.0.21+9"), ("jdk", "21.0.13+9"), ("jdk@21.0.13+9", "21.0.13+9")):
        result = dry_run("--toolchain", value, env=env)
        assert result.returncode == 0, result.stderr
        assert [(e["name"], e["version"], e["sandbox"]) for e in json.loads(result.stdout)["toolchains"]] == [
            ("jdk", version, "/opt/toolchains/jdk")]
    both = dry_run("--toolchain", "jdk", "--toolchain", "jdk@17", env=env)
    assert both.returncode != 0 and "Traceback" not in both.stderr
    assert "--toolchain jdk (jdk 21.0.13+9) and jdk@17 (jdk 17.0.21+9): one version of a toolchain per sandbox " \
           "(both would be /opt/toolchains/jdk)" in both.stderr
    missing = dry_run("--toolchain", "jdk@11", env=env)
    assert missing.returncode != 0 and "sandbox-ai toolchain add jdk@11" in missing.stderr
