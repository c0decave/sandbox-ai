"""The device sign-in of `sandbox-ai login codex` (sandbox/device_signin.py, login.py): Codex's page and one-time code
read from its real output, the code said on a line of its own, the page opened in the human's browser (asked first)
- end to end through ./sandbox-ai on a pseudo-terminal, with only Podman faked (live_fakes.py) - and `sandbox-ai login
--status --json`, which the desktop app reads before a start.

User (2026-11-19): "kannst du für Codex eine Art Auto-Sign-in für Device-Auth bauen, dass bei der Anmeldung für Codex,
wenn nichts anderes konfiguriert ist, direkt Device-Auth aufgerufen wird, diese auch direkt im Systembrowser gecallt
wird und man dann dort auch den Code eingeben kann?"
"""
import importlib.util
import json
import os
from pathlib import Path
import pty
import select
import subprocess
import time

import pytest

from live_fakes import Host

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("device_signin_tests", ROOT / "sandbox/device_signin.py")
d = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(d)

GRAY, BLUE, RESET = "\x1b[90m", "\x1b[94m", "\x1b[0m"
CODEX_HOSTS = ["api.openai.com", "chatgpt.com", "auth.openai.com"]


def codex_device_prompt(code="ABCD-12345", url="https://auth.openai.com/codex/device", version="0.166.0"):
    """What `codex login --device-auth` prints, from Codex's own format string (codex-rs login, the device code
    prompt; read from the strings of the @openai/codex 0.166.0 binary: the version and the hints in gray "\\x1b[90m",
    the link and the code in blue "\\x1b[94m", each ended by "\\x1b[0m")."""
    return (f"\nWelcome to Codex [v{GRAY}{version}{RESET}]\n{GRAY}OpenAI's command-line coding agent{RESET}\n"
            "\nFollow these steps to sign in with ChatGPT using device code authorization:\n"
            f"\n1. Open this link in your browser and sign in to your account\n   {BLUE}{url}{RESET}\n"
            f"\n2. Enter this one-time code {GRAY}(expires in 15 minutes){RESET}\n   {BLUE}{code}{RESET}\n"
            f"\n{GRAY}Continue only if you started this login in Codex. If a website or another person gave you this "
            f"code, cancel.{RESET}\n")


def test_codex_page_and_code_from_its_real_output():
    text = codex_device_prompt().encode()
    for size in (len(text), 64, 7, 1):  # in one piece, and cut anywhere (in an escape sequence, in the link)
        watch = d.Watch(CODEX_HOSTS)
        found = [watch.feed(text[at:at + size]) for at in range(0, len(text), size)]
        assert found.count(True) == 1, size
        assert (watch.url, watch.code) == ("https://auth.openai.com/codex/device", "ABCD-12345")
    crlf = d.Watch(CODEX_HOSTS)  # (through a terminal: CR LF)
    assert crlf.feed(text.replace(b"\n", b"\r\n")) and crlf.code == "ABCD-12345"


def test_only_a_device_page_of_the_agents_own_hosts_counts():
    for url in ("https://evil.example/codex/device", "http://auth.openai.com/codex/device",
                "https://user@auth.openai.com/codex/device", "https://auth.openai.com.evil.example/codex/device",
                "https://auth.openai.com/oauth/authorize?client_id=x", "https://auth.openai.com/" + "a" * 3000):
        watch = d.Watch(CODEX_HOSTS)
        assert not watch.feed(codex_device_prompt(url=url).encode()) and watch.url is None, url
    assert d.device_page("https://auth.openai.com/codex/device", CODEX_HOSTS) == (True, None)
    assert d.device_page("https://sub.auth.openai.com/activate", CODEX_HOSTS) == (True, None)


def test_a_code_in_the_link_is_the_code():
    """Qwen OAuth (and a device login that prints verification_uri_complete): the code is in the link."""
    watch = d.Watch(["chat.qwen.ai"])
    said = ("Please visit the following URL in your browser to authorize:\n\n"
            "https://chat.qwen.ai/authorize?user_code=AB12-CD34&client=qwen-code\n\nWaiting for authorization...\n")
    assert watch.feed(said.encode())
    assert watch.url == "https://chat.qwen.ai/authorize?user_code=AB12-CD34&client=qwen-code"
    assert watch.code == "AB12-CD34"
    # (a "code" that could break a line or an option is none)
    assert d.device_page("https://chat.qwen.ai/authorize?user_code=--x%0Ay", ["chat.qwen.ai"]) == (False, None)


def test_the_code_stands_on_a_line_of_its_own():
    said = d.lines("https://auth.openai.com/codex/device", "ABCD-12345")
    assert "    ABCD-12345" in said and "sandbox-ai login: sign in at https://auth.openai.com/codex/device" in said


def test_which_browser_opens_and_when():
    which = {"xdg-open": "/usr/bin/xdg-open", "firefox": "/usr/bin/firefox"}.get
    assert d.opener({"DISPLAY": ":0"}, "linux", which) == ["/usr/bin/xdg-open"]
    assert d.opener({"WAYLAND_DISPLAY": "wayland-0", "BROWSER": "firefox"}, "linux", which) == ["/usr/bin/firefox"]
    assert d.opener({"DISPLAY": ":0", "BROWSER": "sh -c 'x %s'"}, "linux", which) == ["/usr/bin/xdg-open"], \
        "a $BROWSER that is a shell line is never run"
    assert d.opener({}, "linux", which) is None, "no desktop (a login over SSH): nothing opens there"
    assert d.opener({"SSH_CONNECTION": "1 2 3 4"}, "darwin", lambda name: name) is None
    assert d.opener({}, "darwin", lambda name: name) == ["/usr/bin/open"]
    assert d.choice({}) == "ask" and d.choice({"SANDBOX_AI_LOGIN_BROWSER": "open"}) == "open"
    assert d.choice({"SANDBOX_AI_LOGIN_BROWSER": "no"}) == "no"
    assert d.choice({"SANDBOX_AI_APP_TAB": "abcdef0123456789"}) == "no", "the app's tab: the app opens the page"
    started = []
    assert d.open_page("https://auth.openai.com/codex/device", ["/usr/bin/xdg-open"],
                       lambda argv, **kw: started.append((argv, kw)))
    assert started[0][0] == ["/usr/bin/xdg-open", "https://auth.openai.com/codex/device"]
    assert "shell" not in started[0][1]


# -- end to end: ./sandbox-ai login codex on a terminal ---------------------------------------------------------------

LOGIN = {"tokens": {"access_token": "a" * 40, "refresh_token": "r" * 40, "account_id": "acct-1", "id_token": "x"}}


def on_terminal(host, *args, env=None, keys=None, timeout=120):
    """`sandbox-ai ARGS` on a pseudo-terminal: (exit status, what it said). KEYS: {text it waits for: bytes typed
    then}."""
    controller, terminal = pty.openpty()
    process = subprocess.Popen(host.command(*args), cwd=host.workspace, env={**host.env, **(env or {})},
                               stdin=terminal, stdout=terminal, stderr=terminal, close_fds=True)
    os.close(terminal)
    said, deadline, keys = b"", time.monotonic() + timeout, dict(keys or {})
    while time.monotonic() < deadline:
        ready, _, _ = select.select([controller], [], [], 0.2)
        if ready:
            try:
                more = os.read(controller, 65536)
            except OSError:
                break
            if not more:
                break
            said += more
            for wanted in [w for w in keys if w.encode() in said]:
                os.write(controller, keys.pop(wanted))
        elif process.poll() is not None:
            break
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    os.close(controller)
    return process.returncode, said.decode(errors="replace").replace("\r\n", "\n")


@pytest.fixture
def codex_login(tmp_path):
    """A user whose `sandbox-ai login codex` gets as far as its sandbox, where "Codex" prints its device prompt and
    writes its login; a browser stand-in on PATH that notes what it was asked to open."""
    host = Host(tmp_path)
    host.add_image("localhost/codex-sandbox:latest")
    prompt = tmp_path / "codex-says"
    prompt.write_bytes(codex_device_prompt().encode())
    opened = tmp_path / "opened.txt"
    browser = host.bin / "xdg-open"
    browser.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" >> "{opened}"\n')
    browser.chmod(0o755)
    host.env.update(FAKE_PODMAN_RUN="0:1.5", FAKE_PODMAN_SAYS=str(prompt),
                    FAKE_PODMAN_WRITE_HOME=json.dumps({".codex/auth.json": json.dumps(LOGIN)}))
    for name in ("DISPLAY", "WAYLAND_DISPLAY", "BROWSER", "SANDBOX_AI_APP_TAB", "SANDBOX_AI_LOGIN_BROWSER"):
        host.env.pop(name, None)
    host.opened = lambda: opened.read_text().splitlines() if opened.exists() else []
    return host


def stored(host):
    return host.home / ".local/share/sandbox-ai/auth/codex/.codex/auth.json"


def test_login_codex_says_the_code_and_opens_the_page_after_a_yes(codex_login):
    host = codex_login
    status, said = on_terminal(host, "login", "codex", env={"DISPLAY": ":99"},
                               keys={"open it in your browser now? [Y/n]": b"\r"})
    assert status == 0, said
    assert "sandbox-ai login: sign in at https://auth.openai.com/codex/device" in said, said
    assert "sandbox-ai login: the one-time code to enter there:\n\n    ABCD-12345\n" in said, said
    assert "open it in your browser now? [Y/n] yes" in said and "opened in your browser" in said, said
    assert host.opened() == ["https://auth.openai.com/codex/device"]
    assert json.loads(stored(host).read_text()) == LOGIN, "the login the sandbox made is kept"
    assert "stored the codex login" in said


def test_login_codex_does_not_open_after_a_no_or_without_a_desktop(codex_login):
    host = codex_login
    status, said = on_terminal(host, "login", "codex", env={"DISPLAY": ":99"},
                               keys={"open it in your browser now? [Y/n]": b"n"})
    assert status == 0 and "not opened: open the link yourself and enter the code" in said, said
    assert host.opened() == []
    # A login over SSH (no display): the link and the code are said, nothing is asked or opened there.
    status, said = on_terminal(host, "login", "codex")
    assert status == 0 and "    ABCD-12345\n" in said, said
    assert "no browser opens from here" in said and "[Y/n]" not in said and host.opened() == []


def test_login_codex_in_the_apps_tab_leaves_the_page_to_the_app(codex_login):
    """In a terminal tab of the desktop app (SANDBOX_AI_APP_TAB) the app reads the output itself and opens the page:
    the command line neither asks nor opens it a second time, and relays nothing."""
    host = codex_login
    status, said = on_terminal(host, "login", "codex", env={"DISPLAY": ":99", "SANDBOX_AI_APP_TAB": "0123456789abcdef"})
    assert status == 0 and "[Y/n]" not in said and host.opened() == [], said
    assert "https://auth.openai.com/codex/device" in said and "ABCD-12345" in said  # (Codex's own lines)
    assert json.loads(stored(host).read_text()) == LOGIN


def test_login_codex_opens_without_the_question_when_told_so(codex_login):
    host = codex_login
    status, said = on_terminal(host, "login", "codex", env={"DISPLAY": ":99", "SANDBOX_AI_LOGIN_BROWSER": "open"})
    assert status == 0 and "[Y/n]" not in said and "opened in your browser" in said, said
    assert host.opened() == ["https://auth.openai.com/codex/device"]


def test_login_codex_in_a_script_is_as_before(codex_login):
    host = codex_login
    done = host.run("login", "codex", env={"DISPLAY": ":99"})
    assert done.returncode == 0, done.stderr
    assert "ABCD-12345" in done.stdout and "[Y/n]" not in done.stdout + done.stderr and host.opened() == []


def test_login_status_as_json(codex_login):
    """`sandbox-ai login --status --json`: {agent: where its sandboxes' login comes from, and what is wrong with it} -
    what the app's check before a start reads on the start's host."""
    host = codex_login
    before = json.loads(host.run("login", "--status", "--json").stdout)
    assert set(before["agents"]) == {"claude", "codex", "opencode", "hermes", "qwen", "pi"}
    assert before["agents"]["codex"] == {"where": "host", "problem": "missing", "logged_in": False,
                                         "source": str(host.home / ".codex/auth.json")}
    assert on_terminal(host, "login", "codex")[0] == 0
    after = json.loads(host.run("login", "--status", "--json").stdout)
    assert after["agents"]["codex"] == {"where": "store", "problem": None, "logged_in": True,
                                        "source": str(stored(host))}
    words = host.run("login", "--status").stdout
    assert "codex" in words and "login store" in words, "the table is as it was"


def test_a_codex_start_without_a_login_runs_its_device_sign_in_first(codex_login, tmp_path):
    """"wenn nichts anderes konfiguriert ist, direkt Device-Auth": the command a Codex start's sandbox gets (its
    config.json, as the real ./sandbox-ai writes it) runs Codex's device sign-in before Codex when the sandbox has no
    login; a start with something else configured, and a task, get Codex alone."""
    host = codex_login
    for name in ("FAKE_PODMAN_SAYS", "FAKE_PODMAN_WRITE_HOME"):
        host.env.pop(name, None)

    def command(*options):
        kept = tmp_path / f"kept-{len(list(tmp_path.glob('kept-*')))}"
        done = host.run("codex", "--no-keep", *options, env={"FAKE_PODMAN_RUN": "0", "FAKE_PODMAN_KEEP": str(kept)})
        assert (kept / "session/config.json").is_file(), done.stderr
        return json.loads((kept / "session/config.json").read_text())["command"]
    first = command()
    assert first[:2] == ["/bin/sh", "-c"] and "login --device-auth" in first[2], first
    assert '[ ! -s "$HOME/.codex/auth.json" ]' in first[2] and first[2].rstrip().endswith('exec "$0" "$@"')
    assert os.path.basename(first[3]) == "codex" and "--sandbox" in first, "then Codex itself, as it always started"
    assert "analytics.enabled=false" in first[2], "its telemetry is off for the sign-in too"
    for options in (["--no-auth"], ["--offline"], ["--", "exec", "say hi"]):
        plain = command(*options)
        assert "login --device-auth" not in " ".join(plain) and os.path.basename(plain[0]) == "codex", options
