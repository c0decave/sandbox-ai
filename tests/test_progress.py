"""What a start does before its agent appears, said as it happens (user report 2026-11-12: "starting a clone of a
session sometimes takes forever, probably because something is built, the containers - that should be seen in the
terminal"): progress.py's steps, the time of a slow one, "ready in N s"; podman's build and pull output bounded and
readable (sandbox-ai build, mcp add); a session's terminal says what its supervisor says while it starts; never a
secret."""
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / "sandbox" / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


P = load("sandbox_ai_progress_test", "progress.py")
GH = "gh" + "p_" + "abcdefghijklmnopqrstuvwxyz0123456789"  # (made here: no token in this file's text)

BUILD = f"""STEP 1/4: FROM docker.io/library/node:22-bookworm-slim
STEP 2/4: RUN apt-get update && apt-get install -y git tmux
Get:1 http://deb.debian.org/debian bookworm InRelease [151 kB]
Get:2 http://deb.debian.org/debian bookworm-updates InRelease [55.4 kB]
Get:3 http://deb.debian.org/debian-security bookworm-security InRelease [48.0 kB]
Get:4 http://deb.debian.org/debian bookworm/main amd64 Packages [8792 kB]
Get:5 http://deb.debian.org/debian bookworm-updates/main amd64 Packages [13.5 kB]
STEP 3/4: RUN npm install -g @anthropic-ai/claude-code@2.0.1 --token=npm_SECRETVALUE1234
--> Using cache 0123456789abcdef
STEP 4/4: ENV GH_TOKEN={GH}
COMMIT localhost/claude-sandbox:base
--> 9f8e7d6c5b4a
Successfully tagged localhost/claude-sandbox:base
"""
PULL = """Trying to pull ghcr.io/c0decave/fetch-mcp:1.2...
Getting image source signatures
Copying blob sha256:aaa
Copying blob sha256:bbb
Copying blob sha256:ccc
Copying config sha256:ddd
Writing manifest to image destination
"""


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class FakeProcess:
    def __init__(self, text, status=0, clock=None, step=0.0):
        self.text, self.status, self.clock, self.step = text, status, clock, step
        self.stdout = self.lines()

    def lines(self):
        for line in io.StringIO(self.text):
            if self.clock is not None:
                self.clock.now += self.step
            yield line

    def wait(self):
        return self.status


class Stdout:
    def __init__(self, lines):
        self.lines = lines

    def __iter__(self):
        return self.lines

    def close(self):
        pass


def fake_popen(text, status=0, clock=None, step=0.0, seen=None):
    def popen(argv, **kwargs):
        if seen is not None:
            seen.append(list(argv))
        process = FakeProcess(text, status, clock, step)
        process.stdout = Stdout(process.lines())
        return process
    return popen


def test_scrub_masks_every_kind_of_credential():
    for text, secret in ((f"GH_TOKEN={GH}", "gh" + "p_"),
                         ("Authorization: Bearer sk-abcdefghijklmnop0123", "sk-abc"),
                         ("--token=npm_SECRETVALUE1234", "SECRETVALUE"),
                         ("cloning https://me:" + "hunter2secret" + "@git.example.org/r.git", "hunter2"),
                         ("api_key: 'abc def'", "abc def"),
                         ("the value is s3cr3t-v4lue", "s3cr3t")):
        masked = P.scrub(text, secrets=["s3cr3t-v4lue"])
        assert secret not in masked and "***" in masked, (text, masked)
    assert P.scrub("STEP 2/4: RUN apt-get update") == "STEP 2/4: RUN apt-get update"
    assert P.shown("a\x1b[31mred‮") == "a\\x1b[31mred\\u202e"  # nothing for the terminal to act on


def test_steps_say_how_long_a_slow_one_took_and_the_end():
    clock, lines = Clock(), []
    progress = P.Progress(out=lines.append, clock=clock)
    progress.step("starting the MCP server fetch")
    clock.now += 2
    progress.step("bringing up the VPN home")
    clock.now += 12.4
    progress.ready()
    assert lines == ["agent-sandbox: starting the MCP server fetch…", "agent-sandbox: bringing up the VPN home…",
                     "agent-sandbox: bringing up the VPN home: done in 12 s", "agent-sandbox: ready in 14 s"]
    quiet = []
    P.Progress(out=quiet.append, clock=Clock()).ready()
    assert quiet == [], "an instant start with no step says nothing"
    off = []
    progress = P.Progress(out=off.append, enabled=False)
    progress.step("x")
    assert off == []


def test_a_build_is_said_step_by_step_bounded_and_without_secrets():
    clock, lines, seen = Clock(), [], []
    status = P.stream(["podman", "build", "--tag", "localhost/claude-sandbox:base", "."], "building the image claude",
                      P.Progress(out=lines.append, clock=clock), popen=fake_popen(BUILD, clock=clock, step=1, seen=seen))
    text = "\n".join(lines)
    assert status == 0 and seen == [["podman", "build", "--tag", "localhost/claude-sandbox:base", "."]]
    assert "agent-sandbox: building the image claude (step 2/4): RUN apt-get update && apt-get install -y git tmux…" \
        in lines
    assert "agent-sandbox:   Get:3 http://deb.debian.org/debian-security bookworm-security InRelease [48.0 kB]" in lines
    assert "Get:4" not in text and "agent-sandbox:   (… 2 more lines)" in lines, "a step's output bounded"
    assert "agent-sandbox: building the image claude: saving the image…" in lines
    assert "ghp_" not in text and "SECRETVALUE" not in text, "never a secret"
    assert lines[-1] == "agent-sandbox: building the image claude: done in 13 s (1 step from the cache)"
    assert "agent-sandbox: building the image claude (step 2/4): RUN apt-get update && apt-get install -y git " \
           "tmux: done in 6.0 s" in lines, "a slow step says how long it took"


def test_a_failed_build_shows_its_end_whole():
    lines = []
    status = P.stream(["podman", "build", "."], "building the image tor", P.Progress(out=lines.append),
                      popen=fake_popen(BUILD.replace("COMMIT", "Error: building at STEP \"RUN x\": exit status 100\n"
                                                                  "COMMIT"), status=125))
    assert status == 125
    assert "agent-sandbox: building the image tor failed (exit status 125); its last lines:" in lines
    assert any("Get:4" in line for line in lines), "the whole end of it, the lines kept back too"
    assert not any("ghp_abc" in line for line in lines)


def test_a_pull_counts_its_layers():
    lines = []
    assert P.stream(["podman", "pull", "x"], "pulling the image of fetch", P.Progress(out=lines.append),
                    popen=fake_popen(PULL)) == 0
    assert "agent-sandbox: pulling the image of fetch: 3 layers fetched" in lines
    assert not any("Copying blob" in line for line in lines)


def test_the_podman_wrapper_picks_build_and_pull_and_passes_everything_else(tmp_path):
    assert P.subcommand(["podman", "--remote", "--url", "unix:///x", "build", "--tag", "a", "."]) == (4, "build")
    assert P.subcommand(["podman", "--cgroup-manager=cgroupfs", "pull", "img"]) == (2, "pull")
    assert P.what_of(["podman", "build", "--tag", "localhost/x:latest", "."], 1, "build") == \
        "building the image localhost/x:latest"
    assert P.what_of(["podman", "pull", "-q", "img:1"], 1, "pull") == "pulling the image img:1"
    # As sandbox-ai build runs it (build_image.sh): a stand-in podman.
    fake = tmp_path / "podman"
    fake.write_text("#!/bin/sh\nif [ \"$1\" = build ]; then printf 'STEP 1/2: FROM x\\nSTEP 2/2: RUN make\\nok\\n';"
                    " exit 3; fi\necho \"exists $*\"\n")
    fake.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}
    done = subprocess.run([sys.executable, "-I", str(ROOT / "sandbox/progress.py"), "podman", "podman", "image",
                           "exists", "x"], capture_output=True, text=True, env=env, timeout=30)
    assert done.returncode == 0 and done.stdout == "exists image exists x\n", "every other command as it is"
    done = subprocess.run([sys.executable, "-I", str(ROOT / "sandbox/progress.py"), "podman", "podman", "build",
                           "--tag", "localhost/claude-sandbox:base", "."], capture_output=True, text=True, env=env,
                          timeout=30)
    assert done.returncode == 3 and done.stdout == ""
    assert "sandbox-ai build: building the image localhost/claude-sandbox:base (step 2/2): RUN make…" in done.stderr
    assert "failed (exit status 3)" in done.stderr
    done = subprocess.run([sys.executable, "-I", str(ROOT / "sandbox/progress.py"), "podman", "podman", "build", "."],
                          capture_output=True, text=True, env={**env, "SANDBOX_AI_PODMAN_OUTPUT": "full"}, timeout=30)
    assert done.stdout.startswith("STEP 1/2: FROM x"), "SANDBOX_AI_PODMAN_OUTPUT=full: podman's own output"


def test_build_image_sh_runs_podman_through_the_progress_wrapper():
    text = (ROOT / "sandbox/build_image.sh").read_text()
    assert 'podman_cmd=("$python" -I "$script_dir/progress.py" podman "${podman_cmd[@]}")' in text
    images = (ROOT / "sandbox/images.py").read_text()
    assert '"progress.py"), "podman", *argv]' in images


def test_mcp_add_says_its_pull(monkeypatch):
    mcp = load("sandbox_ai_mcp_progress_test", "mcp.py")
    lines, seen = [], []
    mcp.pull_image("ghcr.io/c0decave/fetch-mcp:1.2", "fetch", lines.append, popen=fake_popen(PULL, seen=seen))
    assert seen == [["podman", "pull", "ghcr.io/c0decave/fetch-mcp:1.2"]]
    assert lines[0] == "sandbox-mcp: pulling the image of fetch…"
    assert "sandbox-mcp: pulling the image of fetch: 3 layers fetched" in lines
    with pytest.raises(subprocess.CalledProcessError):
        mcp.pull_image("x", "fetch", lines.append, popen=fake_popen("Error: denied\n", status=125))


def test_a_sessions_terminal_says_what_its_supervisor_says_while_it_starts(tmp_path, monkeypatch):
    """A session's sandbox starts in a supervisor of its own (its output in supervisor.log): its terminal waited in
    silence. Now its agent-sandbox lines are said there as they come (scrubbed), a long silence says that it still
    starts, and the end how long it took."""
    sessions = load("sandbox_ai_sessions_progress_test", "sessions.py")
    directory = tmp_path / "s"
    (directory / "tmux").mkdir(parents=True)
    log = directory / "supervisor.log"
    log.write_text("agent-sandbox: an old line of the last run\n")
    follow = log.stat().st_size
    clock, lines = Clock(), []
    script = iter([
        "agent-sandbox: starting the MCP server fetch (its image is pulled once, its sandbox started)…\n",
        "podman: some noise of its own\n",
        f"agent-sandbox: secret GH_TOKEN={GH} given\n",
        "", "", "agent-sandbox: bringing up the VPN home",  # (a line written in two pieces)
        " (its container, its tunnel and kill switch)…\n", "READY"])

    def sleep(_seconds):
        clock.now += 8
        piece = next(script)
        if piece == "READY":
            (directory / "tmux" / "attach.sock").touch()
        elif piece:
            with open(log, "a") as stream:
                stream.write(piece)
    monkeypatch.setattr(sessions.time, "sleep", sleep)

    class Running:
        returncode = None

        def poll(self):
            return None
    sessions.wait_ready(directory, Running(), timeout=600, follow=follow, out=lines.append, clock=clock)
    assert lines[0] == "agent-sandbox: starting the MCP server fetch (its image is pulled once, its sandbox started)…"
    assert not any("old line" in line or "noise" in line or "ghp_" in line for line in lines)
    assert "agent-sandbox: secret GH_TOKEN=*** given" in lines
    assert any(line.startswith("agent-sandbox: still starting (") for line in lines)
    assert "agent-sandbox: bringing up the VPN home (its container, its tunnel and kill switch)…" in lines
    assert lines[-1] == "agent-sandbox: ready in 64 s"


def test_the_launcher_says_each_phase(tmp_path, monkeypatch, capsys):
    """The launcher's steps before the agent: the MCP servers (pulled and started), the VPN, the kits, the sandbox -
    said in the order they run (here up to the first MCP server: the rest needs Podman)."""
    s = load("agent_sandbox_progress_test", "agent_sandbox.py")
    fake = tmp_path / "bin"
    fake.mkdir()
    for tool in ("podman", "tmux"):
        (fake / tool).write_text("#!/bin/sh\nexit 0\n")
        (fake / tool).chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}:{os.environ['PATH']}")
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "run"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))

    class Stop(Exception):
        pass

    class FakeMcp:
        def vpn_for_servers(self, a, names):
            return {}

        def ensure_running(self, name, a, vpn=None):
            raise Stop(name)

        def __getattr__(self, name):
            real = getattr(REAL, name)
            return real
    REAL = s.load_mcp_module()
    monkeypatch.setattr(s.os, "geteuid", lambda: 1000)  # (as an ordinary user: these tests may run as root)
    monkeypatch.setattr(s, "load_mcp_module", lambda: FakeMcp())
    monkeypatch.setattr(s, "check_mcp", lambda a, *args, **kwargs: None, raising=False)
    work = tmp_path / "w"
    work.mkdir()
    try:
        s.launch(["claude", "--workspace", str(work), "--no-auth", "--no-tmux", "--mcp", "demo"])
    except Stop:
        pass
    except ValueError as exc:  # an option this machine cannot check (no MCP server fetch defined): said so
        pytest.skip(f"the launcher stops before its MCP servers here: {exc}")
    err = capsys.readouterr().err
    assert "agent-sandbox: starting the MCP server demo (its image is pulled once, its sandbox started)…" in err
