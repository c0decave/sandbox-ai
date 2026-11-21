"""Colour on a terminal by default, in every help and in what sandbox-ai says - and nowhere else: not in a pipe, a
file or --json, not with NO_COLOR or TERM=dumb, not with --no-color (before the command, `sandbox-ai --no-color net
--help`, or after it, `sandbox-ai net --help --no-color`); --color=always forces it. End to end through ./sandbox-ai
with a pseudo-terminal where it depends on one (sandbox/main.py, helpstyle.py, textout.py)."""
import importlib.util
import os
from pathlib import Path
import pty
import re
import select
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
ESC = "\033["


def load(name):
    spec = importlib.util.spec_from_file_location(f"sandbox_{name}_for_colour_test", ROOT / "sandbox" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


main = load("main")


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_DATA_HOME", "data"),
                      ("XDG_RUNTIME_DIR", "run")):
        (tmp_path / sub).mkdir(mode=0o700)
        monkeypatch.setenv(name, str(tmp_path / sub))
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in ("NO_COLOR", "FORCE_COLOR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("COLUMNS", "100")
    return tmp_path


def on_terminal(*words, env=None, timeout=120):
    """(exit status, everything said) of ./sandbox-ai WORDS with standard output and error on a pseudo-terminal."""
    leader, follower = pty.openpty()
    process = subprocess.Popen([str(ROOT / "sandbox-ai"), *words], stdin=subprocess.DEVNULL, stdout=follower,
                               stderr=follower, env={**os.environ, **(env or {})}, close_fds=True)
    os.close(follower)
    chunks = []
    while True:
        ready, _, _ = select.select([leader], [], [], timeout)
        if not ready:
            process.kill()
            break
        try:
            chunk = os.read(leader, 65536)
        except OSError:  # (EIO: the other side closed)
            break
        if not chunk:
            break
        chunks.append(chunk)
    os.close(leader)
    return process.wait(timeout=timeout), b"".join(chunks).decode("utf-8", "replace").replace("\r\n", "\n")


def in_a_pipe(*words, env=None):
    return subprocess.run([str(ROOT / "sandbox-ai"), *words], capture_output=True, text=True, timeout=120,
                          env={**os.environ, **(env or {})})


def plain(text):
    return re.sub(r"\033\[[0-9;]*m", "", text)


def test_the_help_is_coloured_on_a_terminal_and_reads_the_same():
    status, said = on_terminal("--help")
    assert status == 0 and ESC in said
    assert plain(said).rstrip("\n") == main.help_text()
    assert "\033[1;4mUsage:\033[0m" in said and "\033[1;36mclaude\033[0m" in said
    assert "--no-color" in plain(said) and "--color WHEN" in plain(said)


@pytest.mark.parametrize("words, env", [
    (("--no-color", "--help"), None), (("--help", "--no-color"), None), (("-nc", "--help"), None),
    (("--color", "never", "--help"), None), (("--color=never", "--help"), None),
    (("--help",), {"NO_COLOR": "1"}), (("--help",), {"TERM": "dumb"}),
    (("--no-color", "net", "--help"), None), (("net", "--help", "--no-color"), None),
    (("net", "allow", "--no-color", "--help"), None), (("claude", "--no-color", "--help"), None),
    (("--no-color", "claude", "--help"), None), (("net", "--help"), {"NO_COLOR": "1"}),
    (("version", "--no-color"), None), (("--no-color", "version"), None),
    (("build", "--no-color", "--help"), None), (("--no-color", "build", "--help"), None),
])
def test_no_colour_when_asked_on_a_terminal(words, env):
    status, said = on_terminal(*words, env=env)
    assert status == 0, said
    assert ESC not in said, said[:300]
    assert "Usage:" in said or "sandbox-ai " in said


@pytest.mark.parametrize("words", [("net", "--help"), ("net", "allow", "--help"), ("claude", "--help"),
                                   ("list", "--help"), ("update", "--help"), ("version", "--help"), ("help", "net"),
                                   ("build", "--help"), ("completion", "--help")])
def test_every_help_is_coloured_on_a_terminal(words):
    status, said = on_terminal(*words)
    assert status == 0 and "\033[1;4mUsage:\033[0m" in said, said[:300]
    assert "\033[36m-h\033[0m" in said or "\033[36m--help\033[0m" in said


def test_never_in_a_pipe_but_always_when_forced():
    for words in (("--help",), ("net", "--help"), ("claude", "--help"), ("version",)):
        done = in_a_pipe(*words)
        assert done.returncode == 0 and ESC not in done.stdout + done.stderr, words
    forced = in_a_pipe("--color=always", "--help")
    assert forced.returncode == 0 and "\033[1;4mUsage:\033[0m" in forced.stdout
    forced = in_a_pipe("net", "--help", "--color", "always")
    assert forced.returncode == 0 and ESC in forced.stdout
    assert ESC in in_a_pipe("--help", env={"FORCE_COLOR": "1"}).stdout
    # --color=always over NO_COLOR: the option is the later, more specific choice
    assert ESC in in_a_pipe("--color=always", "--help", env={"NO_COLOR": "1"}).stdout


def test_errors_red_on_a_terminal_plain_in_a_pipe():
    status, said = on_terminal("no-such-command")
    assert status == 2 and "\033[31m" in said and "unknown command" in plain(said)
    status, said = on_terminal("net", "allow", "--no-such-option")
    assert status == 2 and "\033[31m" in said and "unrecognized arguments" in plain(said)
    status, said = on_terminal("--no-color", "no-such-command")
    assert status == 2 and ESC not in said
    done = in_a_pipe("no-such-command")
    assert done.returncode == 2 and ESC not in done.stderr and "unknown command" in done.stderr
    done = in_a_pipe("net", "allow", "--color", "always", "--no-such-option")
    assert done.returncode == 2 and "\033[31m" in done.stderr


def test_json_is_never_coloured():
    done = in_a_pipe("--color=always", "net", "list", "--json")
    assert done.returncode == 0 and ESC not in done.stdout and done.stdout.strip() == "[]"


def test_colour_options_are_taken_out_before_the_agents_own_arguments():
    take = main.colour_options
    assert take(["--no-color", "net", "list"]) == (["net", "list"], "never")
    assert take(["-nc", "list"]) == (["list"], "never")
    assert take(["net", "list", "--no-color"]) == (["net", "list"], "never")
    assert take(["net", "list", "--color", "always"]) == (["net", "list"], "always")
    assert take(["net", "list", "--color=auto"]) == (["net", "list"], "auto")
    assert take(["net", "list", "--color", "purple"]) == (["net", "list", "--color", "purple"], None)
    assert take(["--color"]) == (["--color"], None)
    # the agent's arguments after -- are its own
    assert take(["claude", "--no-color", "--", "--no-color", "--color", "never"]) == \
        (["claude", "--", "--no-color", "--color", "never"], "never")
    # -nc after an agent is its --no-config; after list or doctor their own -nc (the same: no colour)
    assert take(["claude", "-nc"]) == (["claude", "-nc"], None)
    assert take(["list", "-nc"]) == (["list", "-nc"], None)
    # what runs elsewhere keeps its words: a command on another host, in a sandbox's shell
    assert take(["hosts", "run", "box", "list", "--no-color"]) == (["hosts", "run", "box", "list", "--no-color"], None)
    assert take(["--no-color", "hosts", "run", "box", "ls", "--color=never"]) == \
        (["hosts", "run", "box", "ls", "--color=never"], "never")
    assert take(["shell", "web", "--", "ls", "--color=always"]) == (["shell", "web", "--", "ls", "--color=always"], None)
    assert take([]) == ([], None)


def test_the_choice_reaches_every_part_through_the_environment(monkeypatch):
    monkeypatch.setenv("FORCE_COLOR", "1")
    main.apply_colour("never")
    assert os.environ.get("NO_COLOR") == "1" and "FORCE_COLOR" not in os.environ
    main.apply_colour("always")
    assert os.environ.get("FORCE_COLOR") == "1" and "NO_COLOR" not in os.environ
    before = dict(os.environ)
    main.apply_colour("auto")
    main.apply_colour(None)
    assert dict(os.environ) == before


def test_messages_on_standard_error_marked_on_a_terminal(tmp_path):
    """A command's own error ("sandbox-ai net: ...") red, a warning yellow: what logged_command's standard error
    passes on."""
    class Terminal:
        def __init__(self):
            self.text = ""

        def write(self, text):
            self.text += text
            return len(text)

        def isatty(self):
            return True

        def flush(self):
            pass
    out = Terminal()
    tee = main.LastLines(out)
    print("sandbox-ai net: no run named 'web'", file=tee)
    print("sandbox-ai mcp: warning: the server is old", file=tee)
    tee.write("partial ")
    tee.write("sandbox-ai x: failed\n")  # (not at a line's start: as it is)
    assert "\033[31m" in out.text and "\033[33m" in out.text
    assert plain(out.text) == ("sandbox-ai net: no run named 'web'\nsandbox-ai mcp: warning: the server is old\n"
                               "partial sandbox-ai x: failed\n")
    assert tee.last(3) == ("sandbox-ai net: no run named 'web' | sandbox-ai mcp: warning: the server is old | "
                           "partial sandbox-ai x: failed"), "launcher.log gets the text without colour"
    os.environ["NO_COLOR"] = "1"
    try:
        out = Terminal()
        print("sandbox-ai net: no run named 'web'", file=main.LastLines(out))
        assert ESC not in out.text
    finally:
        del os.environ["NO_COLOR"]
