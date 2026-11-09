"""The command-line parity on every host (no Qt): each new SandboxAI method runs `sandbox-ai ...` on this machine or,
for a host of `sandbox-ai hosts`, as `sandbox-ai hosts run HOST -- ...`; values that go back as arguments are checked
first, and every argv is one the real command line takes (run here against the checkout in SANDBOX_AI_HOME, in a
temporary HOME and state: no sandbox starts, nothing of the real HOME is read or written)."""
import json
import os

import pytest

from sandbox_ai_app import backend

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")
REMOTE = ("hosts", "run", "ct1", "--")


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache"), ("HOME", "home")):
        monkeypatch.setenv(name, str(tmp_path / sub))
        (tmp_path / sub).mkdir()
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    return tmp_path


@pytest.fixture
def sai(env, monkeypatch):
    """A SandboxAI whose call() records (args, input) and answers from sai.answers; host_terminal names the host."""
    sai = backend.SandboxAI()
    sai.calls, sai.answers = [], {}

    def call(*args, timeout=120, input=None, argv=None):
        # A host's command may come with the ssh command line `sandbox-ai hosts run HOST -- ARGS` runs (host_run):
        # then only for that form, as ssh (the host and ARGS are checked in ARGS themselves).
        assert argv is None or (args[:2] == ("hosts", "run") and argv[0] == "ssh"), (args, argv)
        sai.calls.append((args, input))
        return sai.answers.get(args, (0, "", ""))
    monkeypatch.setattr(sai, "call", call)
    monkeypatch.setattr(sai, "host_terminal", lambda name, *args: ["ssh", "-tt", name, "sandbox-ai", *args])
    return sai


def on(host, *args):
    return (*(() if host == "local" else REMOTE), *args)


# (call on a host, the arguments of sandbox-ai it runs there)
COMMANDS = (
    (lambda s, h: s.revoke_hooks(h, "mine"), ("kits", "trust-hooks", "--revoke", "--", "mine")),
    (lambda s, h: s.plugins_status(h), ("plugins", "--status")),
    (lambda s, h: s.canary_resume(h, "claude-1"), ("canaries", "resume", "--", "claude-1")),
    (lambda s, h: s.forget_trusted(h, "/srv/project"), ("wizard", "forget", "--", "/srv/project")),
    (lambda s, h: s.team_post(h, "review", "codex", "-x: look at it"),
     ("team", "post", "--", "review", "codex", "-x: look at it")),
    (lambda s, h: s.team_stop(h, "review"), ("team", "stop", "--", "review")),
    (lambda s, h: s.team_delete(h, "review"), ("team", "delete", "--yes", "--", "review")),
)


@pytest.mark.parametrize("host", ["local", "ct1"])
def test_each_command_runs_on_the_host_chosen(sai, host):
    for run, args in COMMANDS:
        sai.calls.clear()
        assert run(sai, host) == (0, "", "")
        assert sai.calls == [(on(host, *args), None)], args
    # The kits of a host: through kits ACTION -- NAME (a value never read as an option).
    sai.calls.clear()
    sai.host_kit_command(host, "add-skill", "mine", "/srv/skills/review", options=["--agent=claude"])
    assert sai.calls[-1][0] == on(host, "kits", "add-skill", "--agent=claude", "--", "mine", "/srv/skills/review")
    # Preset folders of a host.
    sai.host_preset_dir_command(host, "add", "/srv/presets")
    assert sai.calls[-1][0] == on(host, "presets", "dirs", "add", "--", "/srv/presets")


def test_values_are_checked_before_anything_runs(sai):
    for refused in (sai.revoke_hooks("ct1", "-x"), sai.canary_resume("ct1", "--all"),
                    sai.forget_trusted("ct1", "relative"), sai.team_post("ct1", "Bad Name", "codex", "x"),
                    sai.team_post("ct1", "review", "gemini", "x"), sai.team_post("ct1", "review", "codex", "  "),
                    sai.team_stop("ct1", "-r"), sai.team_delete("ct1", "x" * 30),
                    sai.host_kit_command("ct1", "rm", "mine"), sai.host_kit_command("ct1", "add-skill", "-m", "/x"),
                    sai.host_preset_dir_command("ct1", "add", "rel/dir")):
        assert refused[0] == 2
    assert sai.calls == []
    for bad in (lambda: sai.canary_run("ct1", "-x"), lambda: sai.team_show("ct1", "-x"),
                lambda: sai.host_command("-oProxyCommand=x", "list"), lambda: sai.host_statistics("ct1", "rel"),
                lambda: sai.image_build_argv("ct1", "claude", pin="--x"), lambda: sai.image_build_argv("ct1", "-a"),
                lambda: sai.team_start_argv("ct1", "review", ["claude"], "/srv/w"),
                lambda: sai.team_start_argv("ct1", "review", ["claude", "codex"], "relative"),
                lambda: sai.login_argv("ct1", "gemini")):
        with pytest.raises(ValueError):
            bad()


def test_reading_a_host_takes_only_what_looks_right(sai):
    host = "ct1"
    sai.answers[on(host, "login", "--status")] = (0, (
        "claude    login store  logged in, access token valid for 3.2 h  (/home/u/.local/share/sandbox-ai/auth)\n"
        "codex     host         expired, log in again  (/home/u/.codex/auth.json)\n"
        "opencode  host         no login  (/home/u/.local/share/opencode/auth.json)\n"
        "junk line\n"), "")
    assert sai.host_login_status(host) == [
        ("claude", "store", None, "/home/u/.local/share/sandbox-ai/auth"),
        ("codex", "host", "expired", "/home/u/.codex/auth.json"),
        ("opencode", "host", "missing", "/home/u/.local/share/opencode/auth.json")]
    sai.answers[on(host, "team", "list")] = (0, (
        "review               claude, codex            /srv/work\n"
        "trio                 codex, claude, opencode  /srv/other folder\n"
        "BAD                  claude                   /x\n"), "")
    assert sai.teams(host) == [{"name": "review", "members": ["claude", "codex"], "workspace": "/srv/work"},
                               {"name": "trio", "members": ["codex", "claude", "opencode"],
                                "workspace": "/srv/other folder"}]
    sai.answers[on(host, "git-logins", "list", "--json")] = (0, json.dumps([{"url": "https://g", "secret": "S"}, 3]), "")
    assert sai.host_git_logins(host) == [{"url": "https://g", "secret": "S"}]
    sai.answers[on(host, "canaries", "list", "--json")] = (0, json.dumps([{"run": "r1", "events": 2}, {"x": 1}]), "")
    assert sai.canary_runs(host) == [{"run": "r1", "events": 2}]
    sai.answers[on(host, "canaries", "list", "--tokens", "--json", "--", "r1")] = (0, json.dumps({"run": "r1"}), "")
    assert sai.canary_run(host, "r1", tokens=True) == {"run": "r1"}
    sai.answers[on(host, "wizard", "trust", "--list", "--json")] = (0, json.dumps(
        [{"path": "/srv/p/.sandbox-ai.json", "state": "trusted"}, {"path": 5}]), "")
    assert sai.trusted_projects(host) == [{"path": "/srv/p/.sandbox-ai.json", "state": "trusted"}]
    sai.answers[on(host, "logs", "--project", "/srv/p", "--json")] = (0, json.dumps(
        {"telemetry": {"requests": 4}, "tool_calls": {"search": [["t", "find", "allow"]]}}), "")
    assert sai.host_statistics(host, project="/srv/p") == {"requests": 4, "tool_calls": {"search": [["t", "find",
                                                                                                    "allow"]]}}
    sai.answers[on(host, "activity", "--session", "s1", "--json", "--tail", "50", "--since=10m",
                   "--kind=prompt,tool", "--grep=pytest")] = (0, "\n".join([
                       json.dumps({"kind": "prompt", "id": "a"}), "not json", json.dumps({"kind": 3})]), "")
    assert sai.host_activity(host, ["--session", "s1"], 50, "10m", ["prompt", "tool"], "pytest") == \
        [{"kind": "prompt", "id": "a"}]
    sai.answers[on(host, "kits", "list", "--json")] = (0, json.dumps({"kits": []}), "")
    assert sai.host_kits_inventory(host) == {"kits": []}
    sai.answers[on(host, "vpn", "list", "--json")] = (1, "", "ssh: timed out")
    with pytest.raises(ValueError, match="timed out"):
        sai.host_vpn_profiles(host)
    sai.answers[on(host, "identities", "list", "--json")] = (0, json.dumps({"work": {"name": "W"}, "x": 1}), "")
    assert sai.host_identities(host) == {"work": {"name": "W"}}


def test_identity_steps_run_on_the_host(sai):
    status, _ = sai.host_save_identity("ct1", "work", None, {"name": "W", "email": "w@example.org", "git": [
        {"url": "https://git.example.org", "user": "", "secret": "W_GIT"}], "publish": []})
    assert status == 0
    assert [args for args, _ in sai.calls] == [
        (*REMOTE, "identities", "add", "--name=W", "--email=w@example.org", "--", "work"),
        (*REMOTE, "identities", "login", "--secret=W_GIT", "--user=x-access-token", "--", "work",
         "https://git.example.org")]


@pytest.mark.parametrize("host", ["local", "ct1"])
def test_the_terminal_commands(sai, host):
    """A login, a team start, a build, the review of hooks and the plugin installs run in a terminal there."""
    assert sai.login_argv(host, "codex", logout=True)[-3:] == ["login", "codex", "--logout"]
    argv = sai.team_start_argv(host, "review", ["claude", "codex", "claude"], "/srv/work", ["--allow-pypi"])
    # This machine's members take the team's workspace as given (sandbox-ai 0.1.78); another host's may not know it.
    tail = ["--allow-pypi", "--workspaces-as-given"] if host == "local" else ["--allow-pypi"]
    assert argv[-8 - len(tail):] == ["team", "start", "--agents", "claude,codex", "--workspace", "/srv/work", "review",
                                     "--", *tail]
    assert sai.image_build_argv(host, "claude", extension_only=True, pin="2.1.0", uv="0.9.5", node="node:22")[-9:] == \
        ["build", "--extension-only", "--pin", "2.1.0", "--uv", "0.9.5", "--node", "node:22", "claude"]
    if host == "ct1":
        assert sai.host_trust_hooks_argv(host, "mine")[-4:] == ["kits", "trust-hooks", "--", "mine"]
        assert sai.host_plugins_argv(host, update=True, kits=["egm", "-x"])[-5:] == \
            ["plugins", "claude", "--update", "--kit", "egm"]
    else:
        assert sai.host_trust_hooks_argv(host, "mine") == sai.trust_hooks_argv("mine")
    assert sai.host_command(host, "images", "--json")[-2:] == ["images", "--json"]
    if host == "ct1":
        assert sai.host_command(host, "images", "--json")[-6:] == ["hosts", "run", "ct1", "--", "images", "--json"]


def parsed_by(sai, module_name, argv, entry="main"):
    """ARGV as the module's own argparse reads it (the command does not run: parsing ends it)."""
    import argparse
    module = sai.module(module_name)
    seen = []
    original = argparse.ArgumentParser.parse_args

    class Parsed(Exception):
        pass

    def capture(self, args=None, namespace=None):
        seen.append(original(self, args, namespace))
        raise Parsed
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(argparse.ArgumentParser, "parse_args", capture)
        with pytest.raises(Parsed):
            getattr(module, entry)(list(argv))
    return seen[0]


def test_the_command_line_parses_what_the_app_passes(env):
    """The real parsers of sandbox-ai read every argv the way the app means it."""
    sai = backend.SandboxAI()
    team = parsed_by(sai, "team", ["start", "--agents", "claude,codex", "--workspace", "/srv/work", "review", "--",
                                   "--allow-pypi"])
    assert (team.name, team.agents, team.workspace) == ("review", "claude,codex", "/srv/work")
    assert team.options in (["--", "--allow-pypi"], ["--allow-pypi"])  # (team.py takes both)
    post = parsed_by(sai, "team", ["post", "--", "review", "codex", "-x: look"])
    assert (post.name, post.agent, post.task) == ("review", "codex", "-x: look")
    deleted = parsed_by(sai, "team", ["delete", "--yes", "--", "review"])
    assert deleted.yes and deleted.name == "review"
    shown = parsed_by(sai, "team", ["show", "--json", "--", "review"])
    assert shown.json and shown.name == "review"


@pytest.mark.parametrize("args", [
    ("team", "list"), ("team", "show", "--json", "--", "review"), ("team", "post", "--", "review", "codex", "-x"),
    ("team", "stop", "--", "review"), ("team", "delete", "--yes", "--", "review"),
    ("canaries", "list", "--json"), ("canaries", "list", "--tokens", "--json", "--", "claude-1"),
    ("canaries", "resume", "--", "claude-1"), ("canaries", "log", "--json"),
    ("wizard", "trust", "--list", "--json"), ("wizard", "forget", "--", "/srv/none"),
    ("presets", "dirs", "list", "--json"), ("kits", "trust-hooks", "--revoke", "--", "nonexistent"),
    ("plugins", "--status"), ("publish-logins", "kinds"), ("identities", "check"), ("login", "--status"),
    ("llm", "show", "lm"), ("llm", "models", "lm", "--sort", "size", "--json"), ("vpn", "show", "home"),
    ("images", "history"), ("logs", "--json", "/nonexistent"),
    ("activity", "--json", "--tail", "5", "--since=10m", "--kind=prompt,tool", "--grep=x")])
def test_the_real_command_line_takes_each_argv(env, args):
    """Run for real (nothing to act on in the empty state): sandbox-ai may say no such team or run, but never that
    it does not understand the arguments (argparse: exit status 2 with its usage)."""
    sai = backend.SandboxAI()
    status, out, err = sai.call(*args, timeout=120)
    assert not (status == 2 and "usage:" in err), (args, err)
    assert "unrecognized arguments" not in err and "invalid choice" not in err, (args, err)
